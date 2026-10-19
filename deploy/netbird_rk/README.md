# RK 与 NX 的 NetBird 视频转发固化

本目录负责 RK（`robot`）；NX（`orin-nx`）用同级 `netbird_nx`。2026-10-16 已验证手动规则下 RK → NX → NetBird → Mac 的 UDP 5004 小包及回复。部署脚本的安装、实际视频和重启验收仍须在两块板上完成。

## 固化范围

| 位置 | 持久设置 |
| --- | --- |
| RK | 给正在使用的 eth0 NetworkManager 配置追加 `100.67.196.150/32 via 192.168.168.100 metric 50 src 192.168.168.168`。NetworkManager 在开机和连接恢复时加载。 |
| RK | 写入 `net.ipv4.ip_forward=1`；检查原来的 `/etc/iptables/rules.v4` 已有 eth0 转发放行及全局 MASQUERADE，并确保原有 netfilter-persistent 开机启用。 |
| NX | 两条 RK↔Mac UDP 5004 转发规则、一条发往 wt0 的源 NAT，以及 `ip_forward=1`。定时服务在开机后及每约 15 秒检查并恢复三条规则。 |

RK 的 CPE DHCP、两个固定内网地址、默认网关、DNS 和 Wi-Fi 配置沿用现有设置；安装器仅追加一条主机路由。不会执行 `nmcli connection down/up`、重启网络服务、清空防火墙或重启设备。RK 的已知原厂转发/NAT 如果不符合记录，安装器会停在修改前，避免覆盖其他设置。

## 从 Mac 安装两端

```bash
bash /Users/fushuai/Work/low_latency_video_demo/deploy/install_robot_netbird.sh
```

脚本依次复制并安装 NX、RK，SSH 和 sudo 如需密码会在终端提示。安装器可重复运行。任何一步失败都会停止后续步骤；先处理输出，不要直接重启。

也可分别操作：

```bash
# Mac
scp -r /Users/fushuai/Work/low_latency_video_demo/deploy/netbird_nx robot@100.67.34.120:~/
ssh -t robot@100.67.34.120 'sudo bash ~/netbird_nx/install.sh'
scp -r /Users/fushuai/Work/low_latency_video_demo/deploy/netbird_rk robot@192.168.168.168:~/
ssh -t robot@192.168.168.168 'sudo bash ~/netbird_rk/install.sh'
```

## 检查

NX：`sudo bash ~/netbird_nx/check.sh`。预期 timer 为 enabled、active，oneshot 结果 success / 0，FORWARD 前两条和 POSTROUTING 第一条带 qnbot-rk-video 注释。

RK：`sudo bash ~/netbird_rk/check.sh`。预期 profile 的 ipv4.routes 已含 Mac /32，经 NX；实际 `route get` 同样经 NX 且源地址为 `192.168.168.168`。公网路由应经 CPE `192.168.66.1`，转发为 1，netfilter-persistent 为 enabled、active。旧的临时同目标路由可能仍存在；确认配置内的持久路由后，重启验收其自动加载。

随后启动 Mac 视频接收与 RK 视频发送，验证画面。重启验收分开做：先 NX，等其 NetBird SSH 和视频恢复，再检查 RK；RK 重启会暂时中断 NX 上网，需等 CPE DHCP、RK 路由和 NX NetBird 恢复后检查。不要同时重启两块板。若要严格只测公网，RK 重启后另查 `iw dev wlan0 link`；此前 `robot_sta.sh stop` 是临时断开，重启后 Qnbot 可能自动重连。

## RK 回退

每次安装前备份在 `/var/backups/qnbot-rk-route/install-*`，含 NM 连接文件、原始路由和原防火墙配置。备份可能含原有连接凭据，仅 root 可读。不要将全部备份直接覆盖回运行系统。

若仅撤销本方案的持久主机路由，在 RK 执行：

```bash
rk_uuid=$(nmcli -g GENERAL.CON-UUID device show eth0)
sudo nmcli connection modify uuid "$rk_uuid" \
  -ipv4.routes '100.67.196.150/32 192.168.168.100 50 src=192.168.168.168'
sudo nmcli device reapply eth0
```

这会影响 RK 主动发往 Mac 的视频以及可能使用此路由的 SSH 返回流量，应从 RK 本地或其他可用连接操作。旧的临时路由若还存在会保留到被移除或重启。原厂 netfilter-persistent 和 IPv4 转发用于 NX 上网，保持启用；不要为回退视频将转发设为 0。NX 的三条规则移除方法见同级 `netbird_nx/README.md`。

## 参考

- [NetworkManager 1.36 路由属性](https://networkmanager.dev/docs/api/1.36.0/nm-settings-nmcli.html)：支持路由 metric 和 src 属性。
- [nmcli reapply](https://networkmanager.dev/docs/api/latest/nmcli.html)：将活动连接配置变更应用到设备。
- [NetBird Site-to-VPN](https://docs.netbird.io/use-cases/remote-access/site-to-vpn)：无 NetBird 客户端的 RK 通过路由节点 NX 访问 Mac，需要静态路由和出站源 NAT。
