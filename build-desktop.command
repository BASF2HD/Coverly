#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
PYTHON_BIN="${COVERLY_BUILD_PYTHON:-}"
if [ -z "$PYTHON_BIN" ] && [ -x .venv/bin/python ]; then
  if .venv/bin/python -c 'import sys; sys.exit(sys.version_info < (3,12))'; then
    PYTHON_BIN=.venv/bin/python
  fi
fi
if [ -z "$PYTHON_BIN" ]; then
  for candidate in python3.13 python3.12 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3,12))' 2>/dev/null; then
      PYTHON_BIN="$candidate"
      break
    fi
  done
fi
if [ -z "$PYTHON_BIN" ]; then
  echo 'Install 64-bit Python 3.12 or later, or set COVERLY_BUILD_PYTHON to its executable.'
  exit 1
fi
"$PYTHON_BIN" -c 'import sys; sys.exit(sys.version_info < (3,12))'
BUILD_ENV="output/desktop-build/venv-$(uname -m)"
"$PYTHON_BIN" -m venv "$BUILD_ENV"
"$BUILD_ENV/bin/python" -m pip install -e '.[desktop,build,browser]'
"$BUILD_ENV/bin/python" scripts/build_desktop.py "$@"
