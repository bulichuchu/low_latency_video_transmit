#!/bin/sh
# Usage: tools/awdl_guard.sh COMMAND [ARGS...]
#
# macOS: run COMMAND (normally the web service) and keep AWDL off only while a
# sender is sending to another computer. AWDL is the peer-to-peer Wi-Fi link
# behind AirDrop, Handoff, Universal Control and Sidecar; while it is up the
# radio leaves the network channel on a fixed schedule. On 2026-10-08 that
# caused ~100 ms stalls every 524 ms on the sender -> receiver path, and they
# disappeared with AWDL off.
#
# The guard exports VIDEO_DEMO_SENDING_DIR=.local/sending. A sender sending to
# a non-loopback host writes <dir>/<pid> there and removes it when it stops. A
# root watchdog (local password file or sudo prompt) keeps AWDL off while a marker names a live
# process and turns it back on afterwards, also when COMMAND exits, the
# Terminal window closes or the sender crashes. It only turns on what it
# turned off. Set KEEP_AWDL=1 to leave AWDL alone; other systems run COMMAND
# unchanged. AWDL_GUARD_IFCONFIG replaces /sbin/ifconfig in tests.

[ $# -gt 0 ] || { echo "usage: $0 COMMAND [ARGS...]" >&2; exit 2; }
root=$(cd "$(dirname "$0")/.." && pwd)
ifconfig=${AWDL_GUARD_IFCONFIG:-/sbin/ifconfig}
markers=$root/.local/sending

if [ "$(uname)" != Darwin ] || [ -n "${KEEP_AWDL:-}" ] || ! "$ifconfig" awdl0 >/dev/null 2>&1; then
    exec "$@"
fi
echo '[awdl] 发送到其他电脑期间会临时关闭 AWDL（隔空投送、接力、通用控制、随航随之暂停），停止发送或退出后自动恢复。'
authorize() {
    password_file=$root/.local/sudo-password
    if [ -s "$password_file" ]; then
        if [ -L "$password_file" ] || [ ! -f "$password_file" ] || [ ! -O "$password_file" ]; then
            echo '[awdl] 密码文件必须是当前用户拥有的普通文件，改为手动授权。' >&2
        elif chmod 600 "$password_file" && sudo -S -p '' -v < "$password_file"; then
            return 0
        else
            echo '[awdl] 本地密码未能完成授权，改为手动输入。' >&2
        fi
    fi
    echo '[awdl] 需要本机管理员密码；不想关闭时用 KEEP_AWDL=1 启动。'
    sudo -v
}
if ! authorize; then
    echo '[awdl] 未获得管理员授权，AWDL 保持不变。' >&2
    exec "$@"
fi
export VIDEO_DEMO_SENDING_DIR="$markers"
# Markers left by a session that ended abruptly (Terminal closed while
# sending); their PIDs may since belong to unrelated processes.
rm -f "$markers"/*

# stdin from /dev/null: with sudo's use_pty, a terminal stdin would put the
# user's terminal in raw mode and swallow Ctrl+C meant for COMMAND.
{ sudo -n /bin/sh -c '
parent=$1 ifconfig=$2 markers=$3 ours=0 failed=0
# Exited but not yet reaped (zombie) counts as gone; kill -0 would still succeed.
running() { case $(/bin/ps -o stat= -p "$1" 2>/dev/null) in ""|*Z*) return 1 ;; esac; }
restore() {
    if [ "$ours" = 1 ]; then
        "$ifconfig" awdl0 up && echo "[awdl] 已恢复 AWDL"
        ours=0
    fi
}
trap "" INT QUIT
trap "restore; exit 0" HUP TERM
while running "$parent"; do
    sending=0
    for marker in "$markers"/*; do
        pid=${marker##*/}
        case $pid in ""|*[!0-9]*) continue ;; esac
        if running "$pid"; then sending=1; else rm -f "$marker"; fi
    done
    if [ "$sending" = 0 ]; then
        failed=0
        restore
    elif [ "$failed" = 0 ] && "$ifconfig" awdl0 | head -n 1 | grep -q "<UP"; then
        if "$ifconfig" awdl0 down; then
            [ "$ours" = 1 ] || echo "[awdl] 正在发送，已关闭 AWDL"
            ours=1
        else
            failed=1
        fi
    fi
    sleep 0.5
done
restore
' awdl-guard "$$" "$ifconfig" "$markers" ||
    echo '[awdl] 管理员授权已失效，AWDL 保持不变。' >&2; } </dev/null &

"$@"
