#!/bin/zsh
set -eu
cd "$(dirname "$0")"
# The sender also needs the optional WebRTC part (installed once, see start_receiver.command).
export VIDEO_DEMO_WEBRTC=1
exec /bin/sh tools/awdl_guard.sh ./start_receiver.command --page sender "$@"
