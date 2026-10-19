#!/bin/bash
# Run on orin-nx, not on the Mac or RK. No network/package downloads.
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

if [[ $EUID != 0 ]]; then
    echo 'Run on NX: sudo bash ~/netbird_nx/install.sh' >&2
    exit 1
fi
if [[ $(uname -s) != Linux || $(hostname) != orin-nx ]]; then
    echo 'This deployment is for the NX host orin-nx only.' >&2
    exit 1
fi
for program in ip iptables iptables-save systemctl sysctl install awk; do
    command -v "$program" >/dev/null
done
ip -4 addr show dev enP8p1s0 | grep -Fq '192.168.168.100/24'
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
for file in qnbot-rk-video qnbot-rk-video.service qnbot-rk-video.timer check.sh; do
    test -f "$source_dir/$file"
done
bash -n "$source_dir/qnbot-rk-video"

install -d -m 700 /var/backups/qnbot-rk-video
backup_dir=$(mktemp -d /var/backups/qnbot-rk-video/install-XXXXXXXX)
iptables-save >"$backup_dir/iptables-before.v4"
for target in /usr/local/sbin/qnbot-rk-video \
    /etc/systemd/system/qnbot-rk-video.service \
    /etc/systemd/system/qnbot-rk-video.timer \
    /etc/sysctl.d/90-qnbot-rk-video.conf; do
    if [[ -e "$target" ]]; then cp -a -- "$target" "$backup_dir/"; fi
done
echo "Backup: $backup_dir"

# Stop only our own timer/service during installation or an update.
if systemctl is-active --quiet qnbot-rk-video.timer; then
    systemctl stop qnbot-rk-video.timer
fi
systemctl stop qnbot-rk-video.service 2>/dev/null || true
install -d -m 755 /usr/local/sbin /etc/sysctl.d /etc/systemd/system
install -m 755 "$source_dir/qnbot-rk-video" /usr/local/sbin/qnbot-rk-video
install -m 644 "$source_dir/qnbot-rk-video.service" /etc/systemd/system/
install -m 644 "$source_dir/qnbot-rk-video.timer" /etc/systemd/system/
printf '%s\n' 'net.ipv4.ip_forward=1' >/etc/sysctl.d/90-qnbot-rk-video.conf
chmod 644 /etc/sysctl.d/90-qnbot-rk-video.conf
sysctl -p /etc/sysctl.d/90-qnbot-rk-video.conf
systemctl daemon-reload
systemctl start qnbot-rk-video.service
systemctl enable --now qnbot-rk-video.timer
bash "$source_dir/check.sh"
echo 'Installed. Check the timer and the three qnbot-rk-video rules.'
