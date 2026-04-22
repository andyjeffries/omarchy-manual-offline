#!/usr/bin/env bash
# Publish the locally-built epub/PDF outputs as assets on the rolling
# "latest" GitHub Release of this repo. Assumes `gh` is authenticated and
# `python build.py` has already produced files in out/.
set -euo pipefail

cd "$(dirname "$0")/.."

OUT_DIR="${OUT_DIR:-out}"

VERSION=$(tr -d '[:space:]' < .last_release)
DATE=$(date -u +%Y-%m-%d)

FULL_EPUB="${OUT_DIR}/omarchy-manual-${DATE}.epub"
FULL_PDF="${OUT_DIR}/omarchy-manual-${DATE}.pdf"
MANUAL_ONLY_EPUB="${OUT_DIR}/omarchy-manual-only-${DATE}.epub"
MANUAL_ONLY_PDF="${OUT_DIR}/omarchy-manual-only-${DATE}.pdf"

for f in "$FULL_EPUB" "$FULL_PDF" "$MANUAL_ONLY_EPUB" "$MANUAL_ONLY_PDF"; do
  if [ ! -f "$f" ]; then
    echo "Missing: $f" >&2
    echo "Run 'python build.py' first." >&2
    exit 1
  fi
done

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

cp "$FULL_EPUB"         "${WORK}/omarchy-manual-${VERSION}-${DATE}.epub"
cp "$FULL_PDF"          "${WORK}/omarchy-manual-${VERSION}-${DATE}.pdf"
cp "$MANUAL_ONLY_EPUB"  "${WORK}/omarchy-manual-only-${VERSION}-${DATE}.epub"
cp "$MANUAL_ONLY_PDF"   "${WORK}/omarchy-manual-only-${VERSION}-${DATE}.pdf"
cp "$FULL_EPUB"         "${WORK}/omarchy-manual-latest.epub"
cp "$FULL_PDF"          "${WORK}/omarchy-manual-latest.pdf"
cp "$MANUAL_ONLY_EPUB"  "${WORK}/omarchy-manual-only-latest.epub"
cp "$MANUAL_ONLY_PDF"   "${WORK}/omarchy-manual-only-latest.pdf"

NOTES=$(cat <<EOF
Built from upstream Omarchy **${VERSION}** on ${DATE}.

Two variants per format:
- \`omarchy-manual-*\` — manual + full changelog (every release since v1)
- \`omarchy-manual-only-*\` — manual only (no changelog)

Stable URLs:
- \`omarchy-manual-latest.epub\` / \`.pdf\`
- \`omarchy-manual-only-latest.epub\` / \`.pdf\`

Source: https://learn.omacom.io/2/the-omarchy-manual
Upstream: https://github.com/basecamp/omarchy/releases/tag/${VERSION}
EOF
)

echo "Replacing 'latest' release with ${VERSION} / ${DATE}..."
gh release delete latest --yes --cleanup-tag 2>/dev/null || true

gh release create latest \
  --title  "Latest build (${VERSION}, ${DATE})" \
  --notes  "$NOTES" \
  --latest \
  "${WORK}/omarchy-manual-${VERSION}-${DATE}.epub" \
  "${WORK}/omarchy-manual-${VERSION}-${DATE}.pdf" \
  "${WORK}/omarchy-manual-only-${VERSION}-${DATE}.epub" \
  "${WORK}/omarchy-manual-only-${VERSION}-${DATE}.pdf" \
  "${WORK}/omarchy-manual-latest.epub" \
  "${WORK}/omarchy-manual-latest.pdf" \
  "${WORK}/omarchy-manual-only-latest.epub" \
  "${WORK}/omarchy-manual-only-latest.pdf"

echo "Done. See: $(gh release view latest --json url --jq .url)"
