#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python_bin="${PYTHON_BIN:-python3.12}"
"$python_bin" -c 'import sys; assert sys.version_info[:2] == (3, 12), "Please use Python 3.12"'
if [[ -d .venv ]] && ! .venv/bin/python -c 'import sys; assert sys.version_info[:2] == (3, 12)' >/dev/null 2>&1; then
  mv .venv ".venv.backup-$(date +%Y%m%d-%H%M%S)"
fi
[[ -d .venv ]] || "$python_bin" -m venv .venv
.venv/bin/python -m pip install --upgrade pip==26.2.1
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check
if [[ ! -f .env ]]; then
  (umask 077; cp .env.example .env)
fi
printf '%s\n' 'Environment ready. Enter new cTrader credentials in .env; start with ./start.sh.'
