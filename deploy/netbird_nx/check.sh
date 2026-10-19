#!/bin/bash
set -euo pipefail
echo "Host: $(hostname)"
systemctl is-enabled qnbot-rk-video.timer
systemctl is-active qnbot-rk-video.timer
systemctl show qnbot-rk-video.service -p Result -p ExecMainStatus
sysctl net.ipv4.ip_forward
ip -4 route get 100.67.196.150
iptables -w 5 -S FORWARD
iptables -w 5 -t nat -S POSTROUTING
