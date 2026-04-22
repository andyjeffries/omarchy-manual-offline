#!/usr/bin/env python3
"""Scrape the Omarchy manual + GitHub release changelogs, build epub + PDF."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import logging
import mimetypes
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
import markdown as md_lib
from ebooklib import epub
from PIL import Image, UnidentifiedImageError
import io

MANUAL_ROOT = "https://learn.omacom.io/2/the-omarchy-manual"
RELEASES_API = "https://api.github.com/repos/basecamp/omarchy/releases"
USER_AGENT = "omarchy-manual-offline/1.0 (+https://github.com/)"

log = logging.getLogger("build")


# ---------------------------------------------------------------------------
# HTTP session with retries
# ---------------------------------------------------------------------------

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        s.headers["Authorization"] = f"Bearer {token}"
    return s


def get(session: requests.Session, url: str, *, accept: str | None = None, retries: int = 3) -> requests.Response:
    headers = {}
    if accept:
        headers["Accept"] = accept
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            resp = session.get(url, headers=headers, timeout=30)
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                raise requests.HTTPError(f"{resp.status_code} for {url}")
            resp.raise_for_status()
            return resp
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            wait = 2**attempt
            log.warning("GET %s failed (%s), retrying in %ds", url, exc, wait)
            time.sleep(wait)
    assert last_exc is not None
    raise last_exc


# ---------------------------------------------------------------------------
# TOC scraping
# ---------------------------------------------------------------------------

@dataclass
class TocEntry:
    kind: str  # "section" or "page"
    id: str
    title: str
    url: str  # absolute
    slug: str  # stable id for epub


def scrape_toc(session: requests.Session) -> list[TocEntry]:
    """Scrape the manual root to get the ordered list of sections and pages."""
    resp = get(session, MANUAL_ROOT)
    soup = BeautifulSoup(resp.text, "html.parser")
    entries: list[TocEntry] = []
    for li in soup.select("li.toc__leaf"):
        classes = li.get("class", [])
        if "toc__leaf--section" in classes:
            kind = "section"
        elif "toc__leaf--page" in classes:
            kind = "page"
        else:
            continue
        title_a = li.select_one("a.toc__title")
        if not title_a or not title_a.get("href"):
            continue
        href = title_a["href"]
        title = title_a.get_text(strip=True)
        url = urljoin(MANUAL_ROOT, href)
        leaf_id = (li.get("id") or "").removeprefix("leaf_") or hashlib.md5(href.encode()).hexdigest()[:8]
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or f"leaf-{leaf_id}"
        entries.append(TocEntry(kind=kind, id=leaf_id, title=title, url=url, slug=f"{slug}-{leaf_id}"))
    if not entries:
        raise RuntimeError("Could not find any TOC entries — site structure may have changed")
    return entries


# ---------------------------------------------------------------------------
# Chapter fetching
# ---------------------------------------------------------------------------

FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n", re.DOTALL)


def fetch_chapter_markdown(session: requests.Session, entry: TocEntry) -> str:
    """Fetch the .md alternate for a chapter and strip the YAML frontmatter."""
    md_url = entry.url + ".md"
    resp = get(session, md_url, accept="text/markdown")
    text = resp.text
    return FRONTMATTER_RE.sub("", text, count=1).strip()


# ---------------------------------------------------------------------------
# GitHub releases
# ---------------------------------------------------------------------------

@dataclass
class Release:
    tag: str
    name: str
    published_at: str
    body: str
    url: str


def fetch_all_releases(session: requests.Session) -> list[Release]:
    releases: list[Release] = []
    page = 1
    while True:
        resp = get(session, f"{RELEASES_API}?per_page=100&page={page}", accept="application/vnd.github+json")
        batch = resp.json()
        if not batch:
            break
        for r in batch:
            if r.get("draft"):
                continue
            releases.append(
                Release(
                    tag=r["tag_name"],
                    name=r.get("name") or r["tag_name"],
                    published_at=r.get("published_at") or r.get("created_at") or "",
                    body=r.get("body") or "",
                    url=r.get("html_url", ""),
                )
            )
        if len(batch) < 100:
            break
        page += 1
    return releases


# ---------------------------------------------------------------------------
# Markdown → HTML with syntax highlighting
# ---------------------------------------------------------------------------

MD_EXTENSIONS = [
    "fenced_code",
    "codehilite",
    "tables",
    "attr_list",
    "sane_lists",
    "md_in_html",
]

MD_EXTENSION_CONFIGS = {
    "codehilite": {"guess_lang": False, "css_class": "codehilite"},
}


def render_markdown(text: str) -> str:
    return md_lib.markdown(text, extensions=MD_EXTENSIONS, extension_configs=MD_EXTENSION_CONFIGS)


# ---------------------------------------------------------------------------
# Image downloading and rewriting
# ---------------------------------------------------------------------------

@dataclass
class ImageCache:
    session: requests.Session
    cache_dir: Path
    max_width: int = 1000
    jpeg_quality: int = 82
    # maps original URL → local filename (inside cache_dir)
    index: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _optimize(self, content: bytes, suggested_ext: str) -> tuple[bytes, str, str]:
        """Downscale and re-encode images. Returns (bytes, ext, mime)."""
        try:
            im = Image.open(io.BytesIO(content))
            im.load()
        except (UnidentifiedImageError, OSError):
            return content, suggested_ext, mimetypes.guess_type("x" + suggested_ext)[0] or "application/octet-stream"

        # Skip animated / SVG / unusual modes — keep original.
        if getattr(im, "is_animated", False) or im.format == "SVG":
            return content, suggested_ext, Image.MIME.get(im.format or "", "application/octet-stream")

        # Downscale if wider than max_width.
        if im.width > self.max_width:
            new_h = int(im.height * self.max_width / im.width)
            im = im.resize((self.max_width, new_h), Image.LANCZOS)

        buf = io.BytesIO()
        fmt = im.format or "PNG"
        # Re-encode photos as JPEG, keep PNG for graphics with transparency.
        if im.mode in ("RGBA", "LA") or fmt in ("PNG", "GIF", "WEBP") and self._looks_like_graphic(im):
            save_fmt = "PNG"
            im.save(buf, format=save_fmt, optimize=True)
            return buf.getvalue(), ".png", "image/png"
        save_fmt = "JPEG"
        if im.mode != "RGB":
            im = im.convert("RGB")
        im.save(buf, format=save_fmt, quality=self.jpeg_quality, optimize=True, progressive=True)
        return buf.getvalue(), ".jpg", "image/jpeg"

    @staticmethod
    def _looks_like_graphic(im: Image.Image) -> bool:
        # Heuristic: small palette or few unique colors → graphic/UI screenshot
        # Keep as PNG. For large photographic images, prefer JPEG.
        if im.mode == "P":
            return True
        if im.width * im.height < 200_000:
            return True
        return False

    def fetch(self, url: str) -> tuple[str, bytes, str] | None:
        """Return (local_filename, bytes, mime_type) or None on failure."""
        if url in self.index:
            fname = self.index[url]
            data = (self.cache_dir / fname).read_bytes()
            mime, _ = mimetypes.guess_type(fname)
            return fname, data, mime or "application/octet-stream"
        try:
            resp = get(self.session, url, retries=2)
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to fetch image %s: %s", url, exc)
            return None
        content = resp.content
        header_mime = resp.headers.get("Content-Type", "application/octet-stream").split(";")[0].strip()
        suggested_ext = mimetypes.guess_extension(header_mime) or Path(urlparse(url).path).suffix or ".bin"

        optimized, ext, mime = self._optimize(content, suggested_ext)
        digest = hashlib.sha1(url.encode()).hexdigest()[:16]
        fname = f"img_{digest}{ext}"
        (self.cache_dir / fname).write_bytes(optimized)
        self.index[url] = fname
        return fname, optimized, mime


def rewrite_image_urls(html: str, base_url: str, images: ImageCache, local_prefix: str = "images/") -> str:
    """Rewrite all <img src> to absolute (if relative) then to local cached paths."""
    soup = BeautifulSoup(html, "html.parser")
    for img in soup.find_all("img"):
        src = img.get("src")
        if not src:
            continue
        absolute = urljoin(base_url, src)
        result = images.fetch(absolute)
        if result is None:
            # Leave as absolute so at least it's reachable online
            img["src"] = absolute
            continue
        fname, _, _ = result
        img["src"] = local_prefix + fname
    return str(soup)


# ---------------------------------------------------------------------------
# Building the content
# ---------------------------------------------------------------------------

@dataclass
class RenderedChapter:
    entry: TocEntry
    html_body: str  # already rewritten to local image paths


@dataclass
class RenderedRelease:
    release: Release
    html_body: str


def build_all(session: requests.Session, images: ImageCache) -> tuple[list[TocEntry], list[RenderedChapter], list[RenderedRelease], str]:
    toc = scrape_toc(session)
    log.info("Found %d TOC entries", len(toc))

    chapters: list[RenderedChapter] = []
    for entry in toc:
        if entry.kind != "page":
            continue
        log.info("Fetching chapter: %s", entry.title)
        md_text = fetch_chapter_markdown(session, entry)
        html = render_markdown(md_text)
        html = rewrite_image_urls(html, entry.url, images)
        chapters.append(RenderedChapter(entry=entry, html_body=html))

    log.info("Fetching GitHub releases")
    releases = fetch_all_releases(session)
    log.info("Got %d releases", len(releases))
    latest_tag = releases[0].tag if releases else "unknown"

    rendered_releases: list[RenderedRelease] = []
    for rel in releases:
        html = render_markdown(rel.body) if rel.body else "<p><em>No description.</em></p>"
        html = rewrite_image_urls(html, rel.url or "https://github.com/", images)
        rendered_releases.append(RenderedRelease(release=rel, html_body=html))

    return toc, chapters, rendered_releases, latest_tag


# ---------------------------------------------------------------------------
# EPUB building
# ---------------------------------------------------------------------------

EPUB_WRAPPER = """<html xmlns="http://www.w3.org/1999/xhtml">
<head>
<meta charset="utf-8" />
<title>{title}</title>
<link rel="stylesheet" type="text/css" href="../styles/epub.css" />
</head>
<body>
{body}
</body>
</html>"""


def wrap_epub_html(title: str, body: str) -> str:
    return EPUB_WRAPPER.format(title=escape_html(title), body=body)


def escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def format_release_date(iso: str) -> str:
    if not iso:
        return ""
    try:
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except ValueError:
        return iso


IMG_REF_RE = re.compile(r'src=["\']images/([^"\']+)["\']')


def referenced_images(html_bodies: Iterable[str]) -> set[str]:
    refs: set[str] = set()
    for body in html_bodies:
        refs.update(IMG_REF_RE.findall(body))
    return refs


def build_epub(
    *,
    toc: list[TocEntry],
    chapters: list[RenderedChapter],
    releases: list[RenderedRelease],
    images: ImageCache,
    version: str,
    build_date: dt.date,
    css_path: Path,
    out_path: Path,
    include_changelog: bool = True,
) -> None:
    suffix = "" if include_changelog else " (manual only)"
    desc = (
        f"Offline build of the Omarchy manual and release changelogs. Version {version}, built {build_date.isoformat()}."
        if include_changelog
        else f"Offline build of the Omarchy manual. Version {version}, built {build_date.isoformat()}."
    )

    book = epub.EpubBook()
    variant = "full" if include_changelog else "manual-only"
    book.set_identifier(f"omarchy-manual-{variant}-{version}-{build_date.isoformat()}")
    book.set_title(f"The Omarchy Manual ({version}){suffix}")
    book.set_language("en")
    book.add_author("DHH")
    book.add_metadata("DC", "publisher", "Basecamp (unofficial build)")
    book.add_metadata("DC", "description", desc)
    book.add_metadata("DC", "date", build_date.isoformat())
    book.add_metadata(None, "meta", "", {"name": "omarchy-version", "content": version})
    book.add_metadata(None, "meta", "", {"name": "build-date", "content": build_date.isoformat()})

    css_content = css_path.read_text(encoding="utf-8")
    css_item = epub.EpubItem(uid="style_main", file_name="styles/epub.css", media_type="text/css", content=css_content)
    book.add_item(css_item)

    # Only embed images referenced by the actually-included HTML bodies.
    included_bodies: list[str] = [c.html_body for c in chapters]
    if include_changelog:
        included_bodies.extend(r.html_body for r in releases)
    needed_images = referenced_images(included_bodies)
    for fname in sorted(needed_images):
        path = images.cache_dir / fname
        if not path.exists():
            continue
        data = path.read_bytes()
        mime, _ = mimetypes.guess_type(fname)
        book.add_item(
            epub.EpubImage(
                uid=f"img_{fname}",
                file_name=f"images/{fname}",
                media_type=mime or "application/octet-stream",
                content=data,
            )
        )

    # Title page
    title_body = f"""
    <div class="title-page">
      <h1>The Omarchy Manual</h1>
      <p class="subtitle">Offline edition</p>
      <p class="meta">
        Version <strong>{escape_html(version)}</strong><br/>
        Built {build_date.isoformat()}<br/>
        Source: <a href="{MANUAL_ROOT}">{MANUAL_ROOT}</a>
      </p>
    </div>
    """
    title_chapter = epub.EpubHtml(title="Cover", file_name="title.xhtml", lang="en")
    title_chapter.content = wrap_epub_html("The Omarchy Manual", title_body)
    title_chapter.add_item(css_item)
    book.add_item(title_chapter)

    # Build chapters: iterate TOC preserving section groupings
    chapter_by_id = {c.entry.id: c for c in chapters}
    epub_items: list[epub.EpubHtml] = [title_chapter]
    spine: list = ["nav", title_chapter]

    # Group pages under their preceding section entries
    current_section: TocEntry | None = None
    sections: list[tuple[TocEntry | None, list[epub.EpubHtml]]] = [(None, [])]

    for entry in toc:
        if entry.kind == "section":
            current_section = entry
            sections.append((current_section, []))
            continue
        rc = chapter_by_id.get(entry.id)
        if not rc:
            continue
        item = epub.EpubHtml(
            title=entry.title,
            file_name=f"chapters/{entry.slug}.xhtml",
            lang="en",
        )
        item.content = wrap_epub_html(entry.title, rc.html_body)
        item.add_item(css_item)
        book.add_item(item)
        epub_items.append(item)
        spine.append(item)
        sections[-1][1].append(item)

    # Changelog section (optional)
    release_items: list[epub.EpubHtml] = []
    changelog_index: epub.EpubHtml | None = None
    if include_changelog:
        changelog_index_body = "<h1>Changelog</h1><p>All Omarchy releases, newest first.</p>"
        changelog_index = epub.EpubHtml(title="Changelog", file_name="chapters/changelog.xhtml", lang="en")
        changelog_index.content = wrap_epub_html("Changelog", changelog_index_body)
        changelog_index.add_item(css_item)
        book.add_item(changelog_index)
        spine.append(changelog_index)
    else:
        releases = []

    for rr in releases:
        title = f"{rr.release.name}"
        date_str = format_release_date(rr.release.published_at)
        slug = re.sub(r"[^a-z0-9]+", "-", rr.release.tag.lower()).strip("-") or rr.release.tag
        body = f"""
        <article class="release">
          <h1>{escape_html(title)}</h1>
          <p class="release-meta">Released {escape_html(date_str)} · <a href="{escape_html(rr.release.url)}">View on GitHub</a></p>
          {rr.html_body}
        </article>
        """
        item = epub.EpubHtml(title=title, file_name=f"changelog/{slug}.xhtml", lang="en")
        item.content = wrap_epub_html(title, body)
        item.add_item(css_item)
        book.add_item(item)
        release_items.append(item)
        spine.append(item)

    # Build nested TOC: sections → chapters, then Changelog → releases
    nav_toc: list = []
    for sect, items in sections:
        if not items:
            continue
        if sect is None:
            nav_toc.extend(items)
        else:
            nav_toc.append((epub.Section(sect.title), items))
    if changelog_index is not None:
        nav_toc.append((epub.Section("Changelog"), [changelog_index, *release_items]))

    book.toc = nav_toc
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = spine

    out_path.parent.mkdir(parents=True, exist_ok=True)
    epub.write_epub(str(out_path), book)
    log.info("Wrote %s", out_path)


# ---------------------------------------------------------------------------
# PDF building (WeasyPrint)
# ---------------------------------------------------------------------------

PDF_WRAPPER = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>{title}</title>
<meta name="author" content="DHH" />
<meta name="description" content="{description}" />
<meta name="keywords" content="omarchy, arch linux, hyprland, {version}" />
<meta name="generator" content="omarchy-manual-offline" />
<meta name="dcterms.created" content="{created}" />
<meta name="dcterms.modified" content="{created}" />
<meta name="omarchy-version" content="{version}" />
<meta name="build-date" content="{created}" />
<style>
  @page {{
    size: A4;
    margin: 2cm 1.8cm;
    @top-left {{ content: "The Omarchy Manual"; font-family: sans-serif; font-size: 9pt; color: #888; }}
    @top-right {{ content: "{version}"; font-family: sans-serif; font-size: 9pt; color: #888; }}
    @bottom-right {{ content: counter(page) " / " counter(pages); font-family: sans-serif; font-size: 9pt; color: #888; }}
  }}
  @page :first {{
    @top-left {{ content: ""; }}
    @top-right {{ content: ""; }}
    @bottom-right {{ content: ""; }}
  }}
{css}
</style>
</head>
<body>
{body}
</body>
</html>"""


def build_pdf(
    *,
    toc: list[TocEntry],
    chapters: list[RenderedChapter],
    releases: list[RenderedRelease],
    images: ImageCache,
    version: str,
    build_date: dt.date,
    css_path: Path,
    out_path: Path,
    include_changelog: bool = True,
) -> None:
    # Import here so the epub path works even if weasyprint deps are missing.
    from weasyprint import HTML  # type: ignore

    css = css_path.read_text(encoding="utf-8")

    chapter_by_id = {c.entry.id: c for c in chapters}

    parts: list[str] = []
    parts.append(f"""
    <section class="title-page">
      <h1>The Omarchy Manual</h1>
      <p class="subtitle">Offline edition</p>
      <p class="meta">
        Version <strong>{escape_html(version)}</strong><br/>
        Built {build_date.isoformat()}<br/>
        Source: <a href="{MANUAL_ROOT}">{MANUAL_ROOT}</a>
      </p>
    </section>
    """)

    current_section_title: str | None = None
    for entry in toc:
        if entry.kind == "section":
            current_section_title = entry.title
            parts.append(
                f'<section class="part-page"><h1>{escape_html(entry.title)}</h1></section>'
            )
            continue
        rc = chapter_by_id.get(entry.id)
        if not rc:
            continue
        parts.append(f'<section class="chapter" id="ch-{escape_html(entry.slug)}">')
        parts.append(rc.html_body)
        parts.append("</section>")

    if include_changelog:
        parts.append('<section class="part-page"><h1>Changelog</h1><p>All Omarchy releases, newest first.</p></section>')
        for rr in releases:
            date_str = format_release_date(rr.release.published_at)
            parts.append('<section class="chapter release">')
            parts.append(f"<h1>{escape_html(rr.release.name)}</h1>")
            parts.append(f'<p class="release-meta">Released {escape_html(date_str)} · <a href="{escape_html(rr.release.url)}">View on GitHub</a></p>')
            parts.append(rr.html_body)
            parts.append("</section>")

    suffix = "" if include_changelog else " (manual only)"
    desc = (
        f"Offline build of the Omarchy manual and release changelogs. Version {version}, built {build_date.isoformat()}."
        if include_changelog
        else f"Offline build of the Omarchy manual. Version {version}, built {build_date.isoformat()}."
    )
    html_doc = PDF_WRAPPER.format(
        title=escape_html(f"The Omarchy Manual ({version}){suffix}"),
        description=escape_html(desc),
        version=escape_html(version),
        created=build_date.isoformat(),
        css=css,
        body="\n".join(parts),
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # base_url points to the cache dir so relative "images/..." paths resolve.
    base_url = str(images.cache_dir.parent.resolve()) + "/"
    HTML(string=html_doc, base_url=base_url).write_pdf(str(out_path))
    log.info("Wrote %s", out_path)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("out"))
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache"))
    parser.add_argument("--css", type=Path, default=Path("styles/epub.css"))
    parser.add_argument("--skip-pdf", action="store_true", help="Skip PDF generation")
    parser.add_argument("--skip-epub", action="store_true", help="Skip epub generation")
    parser.add_argument("--skip-manual-only", action="store_true", help="Skip the manual-only variant (no changelog)")
    parser.add_argument("--skip-full", action="store_true", help="Skip the full variant (manual + changelog)")
    parser.add_argument("--last-release-file", type=Path, default=Path(".last_release"))
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    session = make_session()
    image_dir = args.cache_dir / "images"
    images = ImageCache(session=session, cache_dir=image_dir)

    toc, chapters, releases, latest_tag = build_all(session, images)

    build_date = dt.date.today()
    datestamp = build_date.strftime("%Y-%m-%d")

    args.output_dir.mkdir(parents=True, exist_ok=True)

    variants: list[tuple[str, str, bool]] = []
    if not args.skip_full:
        variants.append(("full", f"omarchy-manual-{datestamp}", True))
    if not args.skip_manual_only:
        variants.append(("manual_only", f"omarchy-manual-only-{datestamp}", False))

    outputs: dict[str, Path] = {}
    for key, base, include_changelog in variants:
        epub_path = args.output_dir / f"{base}.epub"
        pdf_path = args.output_dir / f"{base}.pdf"
        if not args.skip_epub:
            build_epub(
                toc=toc,
                chapters=chapters,
                releases=releases,
                images=images,
                version=latest_tag,
                build_date=build_date,
                css_path=args.css,
                out_path=epub_path,
                include_changelog=include_changelog,
            )
            outputs[f"{key}_epub"] = epub_path
        if not args.skip_pdf:
            build_pdf(
                toc=toc,
                chapters=chapters,
                releases=releases,
                images=images,
                version=latest_tag,
                build_date=build_date,
                css_path=args.css,
                out_path=pdf_path,
                include_changelog=include_changelog,
            )
            outputs[f"{key}_pdf"] = pdf_path

    args.last_release_file.write_text(latest_tag + "\n", encoding="utf-8")
    log.info("Latest release: %s", latest_tag)
    print(f"version={latest_tag}")
    print(f"date={datestamp}")
    for key, path in outputs.items():
        print(f"{key}={path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
