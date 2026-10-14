#!/bin/bash
# Double-click on macOS/Linux (Terminal will open). Localhost setup wizard only.
set -e
cd "$(dirname "$0")/.."
echo "GalleryEdge setup wizard"
echo "========================"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
. .venv/bin/activate
pip install -q -e ".[panel]" || pip install -e ".[panel]"
echo ""
echo "Starting localhost wizard (Ctrl+C to stop)…"
exec ge-panel --setup --setup-dir "${GALLERYEDGE_DATA:-$HOME/galleryedge-data}"
