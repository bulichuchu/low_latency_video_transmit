#!/bin/zsh
set -e
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
  print '请先准备项目的 .venv Python 环境。'
  exit 1
fi
exec .venv/bin/python -B tools/start_sdk_helper.py "$@"
