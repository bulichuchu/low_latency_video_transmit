# NX 的 NetBird 视频转发持久化

适用现场：RK `192.168.168.168` → NX `enP8p1s0` / `192.168.168.100` → `wt0` → Mac `100.67.196.150:5004/UDP`。NX 主机名 `orin-nx`，NetBird IP `100.67.34.120`。2026-10-16 手动三条规则后，RK 的 UDP 小包及 Mac 回复已通过公网路径验证；这不等于真实视频或重启验收已经通过。

安装器持久化 IPv4 转发，安装一个 oneshot 服务和开机启用的 timer。每约 15 秒检查两条 FORWARD 规则和一条 MASQUERADE 规则。规则齐全且位于链首时不写防火墙；规则丢失或被其他服务插入的规则挡在后面时重新放到链首。依赖 NX 的正常 NetBird 配置及 NX→Mac 的 UDP 5004 策略，不能修复 NetBird 离线。

这是按当前设备和端口固定的配置。安装器不改 NetBird / Docker 服务，不保存整套运行时防火墙作为开机恢复配置，不改 Wi-Fi，也不启动相机或机器人控制。原来的三条同范围、无注释的手动规则会被带 `qnbot-rk-video-*` 注释的规则接管。防火墙快照只用于排查，保存在 `/var/backups/qnbot-rk-video/`，不要直接整体还原到正在运行的 NetBird / Docker 上。

## 安装

Mac：

```bash
scp -r /Users/fushuai/Work/low_latency_video_demo/deploy/netbird_nx robot@100.67.34.120:~/
ssh robot@100.67.34.120
```

NX：

```bash
hostname
sudo bash ~/netbird_nx/install.sh
systemctl is-enabled qnbot-rk-video.timer
systemctl is-active qnbot-rk-video.timer
systemctl show qnbot-rk-video.service -p Result -p ExecMainStatus
sudo iptables -nvxL FORWARD --line-numbers
sudo iptables -t nat -nvxL POSTROUTING --line-numbers
```

预期 timer 为 `enabled`、`active`，服务结果 `Result=success`、`ExecMainStatus=0`，FORWARD 前两条和 POSTROUTING 第一条出现 `qnbot-rk-video-*` 注释。oneshot 每次完成后显示 `inactive (dead)` 是正常的；保持 active 的是 timer。若失败，查看 `sudo journalctl -u qnbot-rk-video.service -n 40 --no-pager`。

验证顺序：先检查安装和当前视频，再单独重启 NX、通过 NetBird SSH 重新登录，重复上述检查并发送视频。RK 的路由持久化由同级 `netbird_rk` 安装器处理；两端统一入口为 `deploy/install_robot_netbird.sh`。不要同时重启 RK 和 NX。

NetBird 或 Docker 重载后，在下一次检查周期内恢复规则；不承诺重载期间视频完全无中断。不要为了测试而清空整张防火墙。

## RK 的独立前提

RK 上 `ip -4 route get 100.67.196.150` 必须是 `via 192.168.168.100 dev eth0 src 192.168.168.168`。`ip route replace` 是临时的，使用同级 `netbird_rk` 安装器将主机路由写入 NetworkManager 配置。CPE 默认路由应继续走 `192.168.66.1`。Wi-Fi 断开与否需在 RK 上另查 `iw dev wlan0 link`，本安装器不改变测试的上网方式。

## 停用和移除

在 NX 执行；这会移除本方案的视频规则：

```bash
sudo systemctl disable --now qnbot-rk-video.timer
sudo systemctl stop qnbot-rk-video.service
sudo /usr/local/sbin/qnbot-rk-video remove
sudo rm /etc/systemd/system/qnbot-rk-video.service /etc/systemd/system/qnbot-rk-video.timer
sudo rm /usr/local/sbin/qnbot-rk-video /etc/sysctl.d/90-qnbot-rk-video.conf
sudo systemctl daemon-reload
```

不把当前 `ip_forward` 改为 0，因为 NX 的既有 NetBird 路由还需要它。

## 参考

- [NetBird Site-to-VPN](https://docs.netbird.io/use-cases/remote-access/site-to-vpn)：无客户端设备主动访问覆盖网络，需要路由节点做源 NAT。
- [systemd v249 timer 文档](https://github.com/systemd/systemd/blob/v249/man/systemd.timer.xml)：开机与周期触发；重复运行的 oneshot 不使用 RemainAfterExit。
