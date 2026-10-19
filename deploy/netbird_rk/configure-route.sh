#!/bin/bash
# Called by install.sh on RK. Add only the single Mac route to the active profile.
set -euo pipefail
export LC_ALL=C
rk_uuid=${1:?Missing eth0 connection UUID}
rk_route='100.67.196.150/32 192.168.168.100 50 src=192.168.168.168'

# NetworkManager keeps this in the connection profile and de-duplicates an
# identical route. The + prefix preserves other static routes and DHCP settings.
nmcli connection modify uuid "$rk_uuid" +ipv4.routes "$rk_route"
# Reapply supports route changes without disconnecting/reconnecting eth0.
nmcli --wait 20 device reapply eth0

rk_actual=$(ip -4 route get 100.67.196.150)
printf '%s\n' "$rk_actual"
if [[ "$rk_actual" != *'via 192.168.168.100 dev eth0'* ||
      "$rk_actual" != *'src 192.168.168.168'* ]]; then
    echo 'Route saved, but current route/source differs; stop here and inspect the output.' >&2
    exit 1
fi
