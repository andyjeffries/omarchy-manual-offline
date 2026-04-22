# Omarchy Manual — Offline Edition

Unofficial offline build of [The Omarchy Manual](https://learn.omacom.io/2/the-omarchy-manual) and the [Omarchy GitHub release changelogs](https://github.com/basecamp/omarchy/releases), packaged as an `.epub` and a `.pdf`.

A scheduled GitHub Actions workflow keeps the files up to date with new Omarchy releases. Each build produces two variants in both epub and PDF:

- **Full** (`omarchy-manual-*`) — the manual followed by every release changelog since v1.
- **Manual only** (`omarchy-manual-only-*`) — just the manual, no changelog. Noticeably lighter (~20–25% smaller) because release screenshots aren't embedded.

Grab them from the [Releases page](../../releases/latest) or directly from the stable URLs:

- `https://github.com/<owner>/<repo>/releases/download/latest/omarchy-manual-latest.epub`
- `https://github.com/<owner>/<repo>/releases/download/latest/omarchy-manual-latest.pdf`
- `https://github.com/<owner>/<repo>/releases/download/latest/omarchy-manual-only-latest.epub`
- `https://github.com/<owner>/<repo>/releases/download/latest/omarchy-manual-only-latest.pdf`

## What this repo does

1. Scrapes the manual's table of contents from the published site.
2. Fetches each chapter via its `text/markdown` alternate (the site exposes `.md` next to each page).
3. Fetches every GitHub release for `basecamp/omarchy` and appends them as a Changelog section, newest first.
4. Renders everything to HTML with Pygments syntax highlighting.
5. Builds an epub with a nested TOC (sections → chapters, then Changelog → each release) and a matching PDF via WeasyPrint.
6. Embeds all images locally so the output is fully offline.

Output metadata (title, author, description, date) includes the current Omarchy version and the build date.

## Running locally

Requires Python 3.11+.

### System packages for WeasyPrint

WeasyPrint needs Pango/Cairo system libs.

- **macOS:** `brew install pango cairo libffi gdk-pixbuf`
- **Debian/Ubuntu:** `sudo apt install libpango-1.0-0 libpangoft2-1.0-0 libcairo2 libgdk-pixbuf-2.0-0 libffi-dev`

### Install + build

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python build.py
```

Outputs:

- `out/omarchy-manual-YYYY-MM-DD.epub`
- `out/omarchy-manual-YYYY-MM-DD.pdf`
- `.last_release` — the latest release tag seen (used by CI to detect new releases)

Useful flags:

```bash
python build.py --skip-pdf            # epub only (no WeasyPrint needed)
python build.py --skip-epub           # PDF only
python build.py --skip-full           # only the manual-only variant
python build.py --skip-manual-only    # only the full (with changelog) variant
python build.py -v                    # verbose logging
python build.py --output-dir dist     # custom output directory
```

Set `GITHUB_TOKEN` if you hit the GitHub API rate limit.

## How the CI works

The workflow lives at [`.github/workflows/build.yml`](.github/workflows/build.yml) and fires on three triggers:

1. **Weekly cron** — `0 6 * * 1` (Mondays, 06:00 UTC).
2. **Manual dispatch** — run it from the Actions tab.
3. **Release polling** — a lightweight second job runs hourly, compares the `basecamp/omarchy` latest release tag against `.last_release` in this repo, and triggers the full build only when a new release is detected.

On each build:

1. Install Python + system deps for WeasyPrint.
2. Run `python build.py`.
3. Commit `.last_release` back to `main` if it changed (tiny diff).
4. Recreate the rolling `latest` GitHub Release with eight assets attached:
   - `omarchy-manual-<version>-<date>.{epub,pdf}` — full, dated
   - `omarchy-manual-only-<version>-<date>.{epub,pdf}` — manual-only, dated
   - `omarchy-manual-latest.{epub,pdf}` — full, stable URL
   - `omarchy-manual-only-latest.{epub,pdf}` — manual-only, stable URL

Binary files live on the Releases page rather than in git history so the repo stays lean.

## File layout

```
.
├── build.py                 # the whole scraper + builder
├── requirements.txt
├── styles/epub.css          # shared styling for epub + PDF
├── .github/workflows/build.yml
├── .last_release            # last release tag seen (written by build.py, committed by CI)
└── out/                     # generated outputs (gitignored; CI uploads them to GitHub Releases)
```

## Caveats

- This is an unofficial build. The Omarchy manual is written by DHH and published at https://learn.omacom.io. If the maintainers ask for it to be taken down, it will be.
- The manual site's HTML structure can change; if scraping breaks, check the selectors in `scrape_toc()` inside `build.py`.
- Images are fetched once per build and embedded; some older release bodies contain large GitHub-hosted screenshots which bulk out the PDF.

## License

The code in this repository (the scraper/builder) is MIT-licensed. The manual content and release notes remain the property of their respective authors.
