#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
  echo "Python environment is missing or broken. See README.md (Local start)." >&2
  exit 1
fi
exec .venv/bin/python live_quant_engine.py
