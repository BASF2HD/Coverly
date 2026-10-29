#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "Run setup.command first."
  exit 1
fi
.venv/bin/python -c 'import sys; sys.exit(sys.version_info < (3,12))'
echo "Open http://localhost:8000 after startup."
exec .venv/bin/python -m coverly run
