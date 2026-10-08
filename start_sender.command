#!/bin/zsh
set -eu
cd "$(dirname "$0")"
exec /bin/sh tools/awdl_guard.sh ./start_receiver.command --page sender "$@"
