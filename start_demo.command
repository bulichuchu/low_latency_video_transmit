#!/bin/zsh
set -eu
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements.txt
fi
if ! .venv/bin/python -c 'import aiohttp' >/dev/null 2>&1; then
  .venv/bin/python -m pip install -r requirements.txt
fi
if [ "$#" -gt 0 ]; then
  exec .venv/bin/python demo.py demo "$@"
fi
exec .venv/bin/python demo.py web
