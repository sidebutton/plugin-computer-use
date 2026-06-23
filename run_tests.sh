#!/usr/bin/env bash
# Run the full test suite. The stdio screenshot round-trip (AC3) needs an X
# display: if DISPLAY is already set (e.g. the runner's :10) we use it; otherwise
# we wrap the run in a headless Xvfb via xvfb-run so AC3 still exercises.
set -euo pipefail
cd "$(dirname "$0")"

# Regenerate the manifest + schema doc and, inside a git checkout, fail if they
# were committed stale.
python3 scripts/build_manifest.py >/dev/null
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  if ! git diff --quiet -- plugin.json docs/computer-use-mcp-tools-schema.md; then
    echo "ERROR: plugin.json / schema doc are stale — run scripts/build_manifest.py and commit." >&2
    git --no-pager diff -- plugin.json docs/computer-use-mcp-tools-schema.md >&2 || true
    exit 1
  fi
fi

run() { python3 -m unittest discover -s tests -v; }

if [ -n "${DISPLAY:-}" ]; then
  echo "Using existing DISPLAY=$DISPLAY"
  run
elif command -v xvfb-run >/dev/null 2>&1; then
  echo "No DISPLAY; running under xvfb-run"
  xvfb-run -a --server-args="-screen 0 1920x1080x24" bash -c "$(declare -f run); run"
else
  echo "No DISPLAY and no xvfb-run; the screenshot round-trip (AC3) will be skipped." >&2
  run
fi
