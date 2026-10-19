#!/bin/bash
# Run only on RK. Preserve the existing, already persistent factory firewall.
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
export LC_ALL=C

if [[ $EUID != 0 || $(uname -s) != Linux || $(hostname) != robot ]]; then
    echo 'Run on RK (hostname robot): sudo bash ~/netbird_rk/install.sh' >&2
    exit 1
fi
for program in ip nmcli iptables iptables-save systemctl sysctl install awk; do
    command -v "$program" >/dev/null
done
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
bash -n "$source_dir/configure-route.sh"

rk_uuid=$(nmcli -g GENERAL.CON-UUID device show eth0)
[[ "$rk_uuid" =~ ^[0-9a-fA-F-]{36}$ ]]
rk_addresses=$(nmcli -g ipv4.addresses connection show uuid "$rk_uuid")
if [[ "$rk_addresses" != *192.168.168.168/24* ||
      "$rk_addresses" != *192.168.144.144/24* ||
      $(nmcli -g ipv4.method connection show uuid "$rk_uuid") != auto ||
      $(nmcli -g connection.autoconnect connection show uuid "$rk_uuid") != yes ]]; then
    echo 'The saved eth0 profile differs from the verified CPE setup; no changes made.' >&2
    exit 1
fi

# Do not synthesize a new broad firewall or save transient state. Validate the
# factory rules already found in /etc/iptables/rules.v4 and in the live tables.
if ! grep -Fxq -- '-A POSTROUTING -j MASQUERADE' /etc/iptables/rules.v4 ||
   ! grep -Fxq -- '-A FORWARD -i eth0 -j ACCEPT' /etc/iptables/rules.v4 ||
   ! iptables -w 5 -t nat -C POSTROUTING -j MASQUERADE ||
   ! iptables -w 5 -C FORWARD -i eth0 -j ACCEPT ||
   ! systemctl is-active --quiet netfilter-persistent.service; then
    echo 'Existing RK forwarding/NAT persistence differs; no changes made.' >&2
    exit 1
fi

install -d -m 700 /var/backups/qnbot-rk-route
backup_dir=$(mktemp -d /var/backups/qnbot-rk-route/install-XXXXXXXX)
cp -a /etc/NetworkManager/system-connections "$backup_dir/"
cp -a /etc/iptables/rules.v4 "$backup_dir/rules.v4"
iptables-save >"$backup_dir/iptables-before.v4"
ip -4 route show table all >"$backup_dir/routes-before.txt"
nmcli -f connection.id,connection.uuid,connection.autoconnect,ipv4 \
    connection show uuid "$rk_uuid" >"$backup_dir/profile-before.txt"
printf '%s\n' "$rk_uuid" >"$backup_dir/eth0-uuid"
if [[ -f /etc/sysctl.d/90-qnbot-rk-forward.conf ]]; then
    cp -a /etc/sysctl.d/90-qnbot-rk-forward.conf "$backup_dir/"
fi
echo "Backup: $backup_dir"

# This was already 1 on RK; keep an explicit persistent setting for this setup.
install -d -m 755 /etc/sysctl.d
printf '%s\n' 'net.ipv4.ip_forward=1' >/etc/sysctl.d/90-qnbot-rk-forward.conf
chmod 644 /etc/sysctl.d/90-qnbot-rk-forward.conf
sysctl -p /etc/sysctl.d/90-qnbot-rk-forward.conf
# Enable the existing loader without restarting it or reloading its tables.
systemctl enable netfilter-persistent.service

bash "$source_dir/configure-route.sh" "$rk_uuid"
bash "$source_dir/check.sh"
echo 'RK installed: Mac host route saved; existing forwarding/NAT stays persistent.'
