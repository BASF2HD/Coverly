#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
PYTHON_BIN="${COVERLY_PYTHON:-}"
if [ -z "$PYTHON_BIN" ]; then
  for candidate in python3.14 python3.13 python3.12 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3,12))' 2>/dev/null; then
      PYTHON_BIN="$(command -v "$candidate")"
      break
    fi
  done
fi
if [ -z "$PYTHON_BIN" ] || ! "$PYTHON_BIN" -c 'import sys; sys.exit(sys.version_info < (3,12))'; then
  echo "Install Python 3.12 or newer from python.org, then run setup.command again."
  exit 1
fi
"$PYTHON_BIN" -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m coverly migrate
echo "Coverly is ready. Double-click start.command."
echo "Optional Tamil OCR: brew install tesseract tesseract-lang"
echo "Optional JavaScript galleries: .venv/bin/python -m pip install -e '.[browser]' && .venv/bin/python -m playwright install chromium"
