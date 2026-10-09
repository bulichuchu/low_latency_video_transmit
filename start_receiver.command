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
# WebRTC transport: only the sender needs aiortc (the receiver relays signaling).
# aiortc declares av<18; it gets encoded frames only, so it is installed without deps.
if [ -n "${VIDEO_DEMO_WEBRTC:-}" ] && ! .venv/bin/python -c 'import aiortc' >/dev/null 2>&1; then
  echo '[webrtc] 首次安装 WebRTC 发送组件（aiortc）…'
  if ! { .venv/bin/python -m pip install -r requirements-webrtc.txt &&
         .venv/bin/python -m pip install --no-deps aiortc==1.15.0; }; then
    echo '[webrtc] 安装失败：WebRTC 传输暂不可用，UDP/RTP 不受影响；联网后重新运行本脚本即可重试。' >&2
  fi
fi
exec .venv/bin/python demo.py web --page receiver "$@"
