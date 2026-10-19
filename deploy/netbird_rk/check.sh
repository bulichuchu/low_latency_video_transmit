#!/bin/bash
set -euo pipefail
export LC_ALL=C
echo "Host: $(hostname)"
rk_uuid=$(nmcli -g GENERAL.CON-UUID device show eth0)
nmcli -f connection.id,connection.autoconnect,ipv4.method,ipv4.addresses,ipv4.routes,ipv4.route-metric \
    connection show uuid "$rk_uuid"
ip -4 route get 100.67.196.150
ip -4 route get 223.5.5.5
sysctl net.ipv4.ip_forward
systemctl is-enabled netfilter-persistent.service
systemctl is-active netfilter-persistent.service
iptables -w 5 -C FORWARD -i eth0 -j ACCEPT
iptables -w 5 -t nat -C POSTROUTING -j MASQUERADE
echo 'RK factory FORWARD and MASQUERADE rules present.'
