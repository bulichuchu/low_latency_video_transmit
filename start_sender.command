#!/bin/zsh
set -eu
cd "$(dirname "$0")"
exec ./start_receiver.command --page sender "$@"
