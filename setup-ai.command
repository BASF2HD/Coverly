#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
.venv/bin/python -m pip install -e '.[ai]'
.venv/bin/python tools/setup_ai.py
