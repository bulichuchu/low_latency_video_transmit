# RK3588 轻量发送端

RK 上只复制 `rk_sender.pyz` 和放在它旁边的 `mpp-pkt/`（打了补丁的 MPP 库，两路时必需，见下文）。pyz 包含发送端和本项目的原版 RTP 协议代码；没有桌面界面、PyAV、NumPy、ROS2、RTSP、JPEG 解码或软件 H.264 编码依赖。电脑继续运行现有 video 项目的接收页。

```text
RK V4L2 selfpath → NV12 / DMA-BUF → 最新原始帧队列 → MPP H.264
  → appsink → 原项目 RTP/UDP + 元数据 → 电脑接收服务 → WebCodecs 显示
```

## 已知设备与验证边界

本次 D1 Max 的 RK3588（`robot@192.168.168.168`）实测（10-14）：

- `/dev/video23`（前摄）和 `/dev/video32`（后摄）两条 selfpath 都能采集；`v4l2src io-mode=dmabuf → mpph264enc` 输出 1280×720、30fps、Annex B / AU 对齐的 H.264，一个进程可同时发两路。
- `/dev/video22`、`/dev/video31` 被原相机服务占用。NX 没有相应的 `/dev/video*`。
- 单路：修正接收端 NACK 后，46 秒内超过 100ms 的画面间隔 10 次（最长 233ms）。
- 双路：
  - 0.4.0 起发送端不再主动丢帧。
  - 用板载 MPP 时，每隔几秒有一路在编码器里卡 150–600 ms，另一路照常出帧。原因是板载 mpph264enc 每 1 ms 向 MPP 要一次包，MPP 在两次之间睡觉时一直拿着输出包队列的锁，编码线程编完一帧却放不进队列（见下文“编码器排查”）。
  - 打了补丁的 MPP（`mpp-pkt/`）消除了这些停顿，0.4.3 起放在 pyz 旁边就自动使用。10-15 两次各 75 秒（带调试日志）：编码超过 150 ms 的帧为 0（板载 MPP 为 30–34）；接收端每路显示 29.7–29.9 fps，没有 200 ms 以上的停顿，延迟 p50 / p95 / p99 为 20 / 29–31 / 37–41 ms。
  - 0.4.3 不开调试日志（10-15 11:27，四次各 75 秒）：编码最长 16–22 ms，每路显示 30 fps，没有 100 ms 以上的画面间隔，延迟 p50 / p95 / p99 为 18–19 / 28–29 / 32–38 ms。P 帧约 18 / 27 ms，每秒一个的 IDR 约 30 / 40 ms，是剩下的尾部。
  - 硬件编码器同时在跑机器狗 `robot_camera_node` 的两路 1080p25 H.264；RGA 和 ISP 也与它共用。
  - ISP 给我们的 NV12 是两个内存平面（NM12），mpph264enc 不能直接导入，每帧都要申请一块新缓冲再拷贝。YUY2 在 selfpath 上协商不过；用 RGA 还是 CPU 拷贝、zero-copy-pkt 开或关，对停顿都没有明显影响。
- 强制 IDR：0.3.0 中每个进程只有第一次请求真正产生了 IDR，之后的 IDR 都落在 GOP 周期上。0.4.0 改了请求方式并默认 1 秒 GOP，见下文。
- 分辨率：相机是 4K（3840×2160），这两条 selfpath 把整幅画面缩小后给我们，最宽 1920（驱动限制），所以最大用 `--width 1920 --height 1088`（或正好 16:9 的 1792×1008）。宽高必须是 16 的倍数，见下文“分辨率与画面裁剪”。10-15 用 1920×1080 时只看到画面左上角的四分之一；3840×2160 直接 `not-negotiated`。
- 10-14 11:24 的一次双路运行（58 秒）后板卡整机死机，需断电重启；没有持久日志，原因未明。之后三次双路运行（70–172 秒）未复现。

桌面测试只验证独立包、真实 H.264 通过现有接收器解码、协议、反馈与恢复逻辑，不替代 RK 真机测试。

厂商把 RK 定位为运控主板并要求不要二开部署。此程序只增加一个可 Ctrl+C 退出的发送进程；它会配置所选 selfpath 的格式，退出会释放设备，不改网络、运控配置或系统服务。不要为腾相机而停止 `robot_camera` 或 `rkaiq_3A_server`。

**测试时让机器狗趴下或架空。** 死机原因查明前，双路每次只跑几分钟，另开一个 SSH 窗口运行 `sudo dmesg -w` 观察。长期部署需结合厂商支持范围及真机资源测试决定。

## 1. 在电脑打包并复制

在当前项目根目录运行（不需要安装额外打包工具）：

```bash
python3 tools/build_rk_sender.py
scp -r .local/rk-sender/rk_sender.pyz .local/rk-sender/mpp-pkt deploy/rk_sender/rk_diag.sh deploy/rk_sender/rk_ab.sh robot@192.168.168.168:~/Public/fzh/
# 只在跑 rk_ab.sh 的 fix、base 对照时需要：
scp -r .local/rk-sender/mpp-fix .local/rk-sender/mpp-base robot@192.168.168.168:~/Public/fzh/
```

`mpp-pkt/` 要和 `rk_sender.pyz` 放在同一个目录：发送端启动时自动加载它（见下文“MPP 库”）。它由 `deploy/rk_sender/build_mpp.sh` 编出，不在 Git 里。

构建使用明确的文件白名单，且打印 SHA-256。包内不包含本机配置、凭据、图像、`.venv` 或其他项目内容。默认包和校验文件放在被 Git 忽略的 `.local/rk-sender/`。每次更新源代码后重新打包、复制，并重启发送进程。

## 2. 在 RK 上检查运行环境

使用 **RK 的系统 Python 3.8 或更高版本**，无需安装桌面项目的 `requirements.txt`，也不需要建立虚拟环境：

```bash
/usr/bin/python3 --version
/usr/bin/python3 ~/Public/fzh/rk_sender.pyz --version
/usr/bin/python3 ~/Public/fzh/rk_sender.pyz --doctor --device /dev/video23
```

`--doctor` 只检查 GI、插件、参数和节点读写权限，并打印将使用的 MPP 库，不启动采集；通过不代表设备空闲或实际出帧。

如果仅缺 Python 绑定，在 RK 上安装发行版对应的软件包：

```bash
sudo apt-get update
sudo apt-get install --no-install-recommends \
  python3-gi python3-gst-1.0 gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0
```

保留已经验证可用的 Rockchip MPP/GStreamer 驱动和插件，不执行系统整体升级或替换为桌面 FFmpeg。若提示缺少 `appsink`，再检查发行版的 `gstreamer1.0-plugins-base`。`mpph264enc` 由板卡镜像提供，不能用通用插件包替代。

权限错误时先用 `id` 和 `ls -l /dev/video23` 确认设备组。通常将用户加入 `video` 组并重新登录即可，无需长期以 root 运行：

```bash
sudo usermod -aG video robot
```

## 3. 电脑启动现有接收页

电脑在项目根目录运行：

```bash
.venv/bin/python demo.py web --page receiver --no-browser
```

在电脑本机浏览器打开 `http://127.0.0.1:8765/#/receiver`，选择：

| 设置 | 值 |
|---|---|
| 传输方式 | RTP / UDP（本轻量端不提供 WebRTC） |
| 监听地址 | `0.0.0.0` |
| UDP 端口 | `5004` |
| 路数 | `1` |
| 显示策略 | `latest` / 最新帧 |
| 校时方式 | 两台设备 / 自动估计（estimated） |
| 输出尺寸 / 帧率（如显示此项） | `1280×720` / `30` |

点击“开始接收”。接收机须允许 UDP 5004 入站，双方须允许反馈流量返回 RK 的临时 UDP 端口。`--host` 填 **电脑从 RK 可达的 IPv4 地址**，不是 RK 地址，也不是 `127.0.0.1`。SSH 能连上不等于这条 UDP 路径已互通。媒体应在可信局域网中传输，沿用项目现有的无加密 RTP 协议。

## 4. RK 启动发送

本次接收 Mac 的有线地址为 `192.168.168.10`；更换网络后需要重新确认。

先做 30 秒单路验收。日志写到 `~/Public/fzh/run/`（`/tmp` 重启后会清空，死机后就丢了）：

```bash
mkdir -p ~/Public/fzh/run
t=$(date +%Y%m%d-%H%M%S)
/usr/bin/python3 ~/Public/fzh/rk_sender.pyz \
  --host 192.168.168.10 --port 5004 \
  --device /dev/video23 --label '前摄像头' \
  --width 1280 --height 720 --fps 30 --bitrate-kbps 4000 \
  --duration 30 --stats-file ~/Public/fzh/run/rk-sender-$t.jsonl \
  --frame-log ~/Public/fzh/run/rk-frames-$t.jsonl
```

画面正常后，去掉 `--duration 30` 即持续运行，Ctrl+C 或 SIGTERM 会停止发送并释放采集节点。默认不保存图像；`--stats-file` 只记录每两秒一次的统计及退出计数，`--frame-log` 每帧一行计时（见下文）。

两路在 **同一个发送进程** 里：

```bash
t=$(date +%Y%m%d-%H%M%S)
/usr/bin/python3 ~/Public/fzh/rk_sender.pyz \
  --host 192.168.168.10 --port 5004 \
  --device /dev/video23 --label '前摄像头' \
  --device /dev/video32 --label '后摄像头' \
  --width 1280 --height 720 --fps 30 --bitrate-kbps 3000 \
  --duration 120 --stats-file ~/Public/fzh/run/rk-sender-$t.jsonl \
  --frame-log ~/Public/fzh/run/rk-frames-$t.jsonl
```

电脑对应设置路数 `2`。码率是每路的值，两路 3000 kbps 是约 6 Mbps 视频负载，另有 RTP/UDP/IP 开销。不要用两个独立发送进程向同一个接收端口发送，现有接收器每会话只接受一个发送源地址。

要约 1080p，把尺寸换成 `--width 1920 --height 1088`，码率提到每路约 6000 kbps。

### 分辨率与画面裁剪

- 宽高必须是 16 的倍数（0.4.4 起不合要求直接报错）。
- 原因：MPP 把帧补齐到 16 的倍数，1080 要补 8 行。GStreamer 发现有补齐的行，会让采集节点“裁掉”它们（`VIDIOC_S_SELECTION`）。Rockchip ISP 却把这个裁剪用在相机原图上：从 3840×2160 里只取左上角的 1920×1080。
- 10-15 的 1080p 运行，`/proc/rkisp0-vir2` 里 selfpath 一行是 `Size:1920x1088 Dcrop(0,0|1920x1080)`；正常应是 `Dcrop(0,0|3840x2160)`。
- 这个裁剪会一直留在驱动里，直到重启或被重新设置，之后的 720p 运行也会只看到左上角。0.4.4 每次启动先把所用节点的裁剪恢复成整幅画面，恢复过会打印 `camera crop … reset to the full …`。机器狗自己的服务用的是另外的节点，不受影响。
- 启动行和 `--doctor` 都会显示每个节点当前的裁剪（`camera crop full (0,0) 3840x2160` 即整幅画面）。也可以在发送时运行 `sudo grep -E 'Input|Output' /proc/rkisp0-vir2 /proc/rkisp1-vir0`，看 selfpath 一行的 `Dcrop`。
- 1920×1088 比 16:9 高 0.7%，画面会被纵向拉伸这么多，肉眼看不出。要严格 16:9 用 1792×1008。
- 宽度最多 1920：RK3588 ISP 驱动把 selfpath 的输出宽度限制在 1920（`STREAM_MAX_SP_RSZ_OUTPUT_WIDTH`），要 3840 时驱动给 1920，v4l2src 协商失败，报 `not-negotiated`（10-15 实测）。能出 4K 的是 mainpath（`/dev/video22`、`/dev/video31`），被机器狗的相机服务占着，不要去动。

## 低延迟行为与调参

- 默认 NV12 → MPP，Python 不读取/转换原始像素；只复制压缩字节用于现有 RTP 封包。`dmabuf` 和插件的 `zero-copy-pkt` 不等于已经证明从相机到网卡全程零拷贝。
- 编码前只有一帧可等待，满时丢最旧的原始帧；MPP 内部仍可能有缓冲，需要真机测量。编码后 `appsink` 不随意丢帧，不另建 Python 压缩帧队列。
- 使用插件已确认支持的 CBR、默认 Baseline、`max-reenc=0`、每个 IDR 附带 SPS/PPS。
- MPP 库（0.4.3）：
  - pyz 同目录有 `mpp-pkt/` 时，自动加载其中的 `librockchip_mpp.so.1`，启动时打印 `[rk] using …/mpp-pkt/… next to the pyz`。没有时用板载 MPP，并打印一行提示（两路时会卡顿）。
  - `--mpp-lib system` 强制用板载 MPP，`--mpp-lib 路径` 换用别的编译。
  - 这个库只加载进发送进程：在 GStreamer 加载 mpph264enc 之前先载入，插件就用它，不再打开系统库。系统库、机器狗的服务和其他程序都不受影响；删掉 `mpp-pkt/` 即还原。
  - 启动行接着打印实际在用的 MPP 文件、SHA-256 前 16 位和版本行（`688e611 author: build_mpp.sh 2026-10-15 mpp-pkt-poll-fix`）。预先载入的库与进程里实际用的不一致时，直接报错退出。
- 两路 IDR 错开（0.4.3，`--idr-stagger on|off`，默认 on）：
  - 两路同时启动、同时被强制 IDR（接收页重连）后，两路的 IDR 会在同一时刻发出。10-15 换用 `mpp-pkt` 后两路不再丢帧，两路的 IDR 几乎都相差不到 3 ms，16 次 NACK 里 15 次落在 IDR 上；IDR 帧延迟 p50 约 35 ms、p95 约 45 ms（P 帧 20 / 29 ms），是延迟尾部的主要来源。
  - 某一路的 IDR 落在另一路的 IDR 两帧以内时，发送端给这一路补一次强制 IDR，放在两路 IDR 之间的正中（两路时相差半个 GOP）。MPP 每次强制会连出两个 IDR，所以每次错开多两个 IDR；每路最多每 5 秒一次。
  - 统计里的 `idr_staggers` 是错开请求的次数；`--frame-log` 里这类请求带 `"why":"stagger"`。
  - 10-15 11:27 实测（stagger / aligned 各两次）：错开后两路 IDR 相差 0.45–0.49 s，不错开时相差 13–21 ms。但在有线局域网上看不出好处：IDR 帧延迟 p50 / p95 为 29–32 / 38–41 ms，不错开时 29–31 / 40–41 ms；NACK 两边都只有 0–3 次。保留默认开：它只在启动和接收页重连后各多花两个 IDR，在 Wi-Fi 等窄一些的链路上可能有用（未验证）。
- 编码器输入（0.4.1）：
  - `--pixel-format`：默认 `NV12`。ISP 给出的 NV12 是两个内存平面，mpph264enc 每帧都要拷进一块新缓冲。`YUY2` 是单平面，编码器有可能直接使用相机缓冲；但机器狗的两条 selfpath 不接受 YUY2（10-14：`not-negotiated`，启动即退出）。
  - 拷贝默认用 CPU（等同 `GST_MPP_NO_RGA=1`），`--rga` 改回用 RGA。RGA 与机器狗的相机服务共用；CPU 拷贝时我们的拷贝线程 CPU 反而更少。10-14 17:28 那次 56 秒的对照里长停顿约减半，但 18:13 两次 75 秒的运行（都不用 RGA）与 16:21（用 RGA）相当，所以对停顿没有明显影响。
  - `--zero-copy-pkt on|off`：编码结果放在每帧新申请的缓冲里（on），或从 MPP 自己的缓冲里拷出来（off）；默认 `auto`，保持插件原样。插件没有这个属性时，指定 on/off 会直接报错退出。
  - 启动行会打印实际的格式、RGA 和 zero-copy-pkt 设置；`--frame-log` 的第一行也会记录。
- 默认 GOP 为 1 秒（等于 `--fps`），这是可靠的恢复手段。
  - 0.3.0 曾改为 10 秒，前提是 PLI 能立即触发 IDR。10-14 日志显示每个进程只有第一次请求生效，10 秒 GOP 因而造成最长 4.9 秒和 8.7 秒的冻结。
  - 代价：4 Mbps 下 IDR 约 28–34 万字节、P 帧 6–7 KB，每秒一个 IDR 占掉约六成码率，并带来 300 KB 的突发。
  - 0.4.0 实测强制 IDR 生效，每次请求 MPP 连出两个 IDR。IDR 现在是延迟尾部的主要来源（见上），可以再评估 `--gop 60` 或更长。
- 接收页开始接收、发现丢帧或解码恢复时都会发 PLI。RK 向编码器发强制关键帧请求，最多每 150ms 一次，且只在接收端在线（两秒内有反馈，统计里 `receiver.state=active`）时发送；离线期间由周期 IDR 兜底。
- 一次恢复只要一个 IDR：
  - 请求发出时已经在编码器里的帧仍是 P 帧，它们不再重复请求；只有请求后 100ms 以上采集的帧仍是 P 帧（编码器没理会）才再请求。
  - IDR 发出后，在它开始发送前收到的请求都算已答复；发送过程中新到的 PLI 保留，它可能报告的是更新的丢包。
  - 连续丢包时强制 IDR 仍可能每 150ms 一个（每个约 300 KB）。有线局域网很少遇到；在无线或 VPN 上测试时留意 `key_requests`。
- GStreamer 1.20 的编码器基类会一直保留带时间的请求（mpph264enc 不标关键帧，请求永不出队），每帧都要遍历一遍。每路每次运行最多发 10000 个请求，之后只靠周期 IDR，并打印一次提示（`key_request_capped`）；重启发送端即清零。
- 0.4.0 的请求带上当前运行时间，不再用“尽快”（10-14 16:21 实测生效）。原因：
  - GStreamer 1.20 的编码器基类会挂起第一个“尽快”请求，直到编码器把某个输出标成关键帧；
  - mpph264enc 输出的 IDR 仍带 DELTA_UNIT 标志，所以挂起的请求永远不结束，之后的“尽快”请求都被跳过；
  - 带时间的请求不受影响：第一帧采集时间不早于请求时间的画面会被编成 IDR。
- 验证：`idr_forced` 统计早于 GOP 周期出现的 IDR（由请求或错开触发）。每次强制请求 MPP 连出两个 IDR（第 N、N+1 帧），GOP 从 N+1 重新计，所以 `idr_forced` 约为请求数的两倍；以 `--frame-log` 里请求与 IDR 的对应为准（见下文）。插件拒绝的请求计入 `key_request_rejected`（错开请求计入 `idr_stagger_rejected`）。
- 帧龄按应用取帧 → 发送计算：
  - 超过 75ms 的帧仍然发送，只计入 `late`；
  - 超过 1000ms（`--max-frame-age-ms`）才丢弃，并暂停发送依赖旧参考的 P 帧，直至新的完整 IDR。
  - 0.3.0 的 250ms 上限在双路 250ms 以上的停顿中会断链；断链的代价是冻结到下一个 IDR，局域网里晚到的帧更划算。
- 发包预算与电脑接收器的重组窗口对齐：普通帧最多 15ms（接收端 20ms），IDR 最多 45ms（接收端 50ms）（`--send-budget-ms`、`--idr-send-budget-ms`）。超出窗口的帧，接收端也用不上。
- 帧龄不再在 75ms 处截断正在发送的帧。10-14 那次被放弃的 IDR，就是发到 218/243 包时撞上了 75ms 帧龄。
- 为什么放宽：10-14 实测中，旧的 75ms 规则让 35–44 帧超龄，又连带 156–189 帧等 IDR，约三成时间画面冻结。有线局域网里，晚到几十毫秒的 P 帧远比断链便宜。
- 这些是应用队列和发包预算，不保证内核、无线网或屏幕上的延迟。
- 电脑接收器：
  - 允许不完整的 IDR 最多重组 50ms，普通帧保持原有窗口；收齐后立即交给后续处理，不固定等待。
  - 只对真正的缺口发 NACK：低于已收到最高分片号的空洞。
  - 帧尾要等这一帧的发送 6ms 没有进展（最高分片号不再增长；重组窗口短时相应缩短），才算丢失。
  - 下一帧开始不作为依据，因为桌面发送端会交错发送多帧。
  - 不再把还在路上的大 IDR 尾部当成丢包。10-14 实测中，旧逻辑让每个 IDR 都多重传 128 个包，相当于原始包数的 27–31%。
  - 发送端不可达（死机、断网）时接收端不再退出。10-14 RK 死机后，Mac 的下一次校时发送报 `OSError 65 No route to host`，接收服务随之结束。现在 PLI、NACK、接收报告和校时的发送失败只记入 `summary.json` 的 `udp_control_errors`，发送端重启（新 epoch）后自动继续。
  - 更新电脑代码后需重启接收服务。
- NACK 只重传最近 30ms 内的包，且不超过帧龄上限，每包最多两次。超出期限由接收器的 PLI 或周期 IDR 恢复。
- 初始 720p30 / 4Mbps 仅用于联调。通道窄时优先降码率或分辨率，例如 `--width 960 --height 544 --bitrate-kbps 2000`。短 GOP、全 I 帧可能放大码率突发，不作为默认低延迟手段。
- 尽量用接收机本地浏览器、有线直连或稳定的局域网。自动校时需要短暂收敛，不能用 `shared` 把两台机器假设成同一时钟。

每两秒输出 `sent_fps`、`wire_mbps`、`sender_ms_p50/p95`、`stage_ms`、`raw_queue_overruns`、`expired`、`late`、`send_failed`、`awaiting_idr`、`pli`、`key_requests`、`idr_staggers`、`idr_encoded`、`idr_forced`、`idr_sent`、`idr_send_ms_max`、`retransmitted` 等。各计数累计，FPS/码率是最近两秒；处理时延分位数来自最近最多 180 个有有效采集时间的编码帧（包括未发送的帧），CPU 100% 代表一个核。`sent` 只说明 UDP 交给本机内核，实际收到和显示仍看电脑。

持续 `expired`/`send_timeout` 时再评估码率、帧大小和实际通道；不要在没有区分 socket 错误之前放宽预算。`awaiting_idr` 表示正在保护解码参考链，不是相机没出帧。

计时起点是 **GStreamer V4L2 源节点交帧给应用**，包含之后的原始帧等待、MPP 编码、发送、接收和浏览器处理，不包含传感器曝光、ISP 和此前的驱动等待（`driver` 段单独给出 V4L2 时间戳到交帧的这一段，但不计入端到端时延）。保留源 PTS 用于逐帧关联，但不把它伪装成曝光时间；接收页相机阶段可能显示未知。`sender_ms` 也不是端到端时延。真实相机到屏幕延迟用同画面时钟/LED 等光学方式验证。

## 逐帧计时（定位停顿）

每帧分五段计时（毫秒）：

| 段 | 从 → 到 | 偏大说明 |
|---|---|---|
| `driver` | V4L2 缓冲时间戳 → v4l2src 把帧交给流水线 | 驱动/ISP 交帧晚 |
| `queue` | → 编码器接收这一帧（经过一帧的 leaky 队列） | 编码器忙或被阻塞，常伴随 `raw_queue_overruns` |
| `encode` | → 编码器输出 | MPP 编码慢、硬件被其他进程占用，或在等上一帧的发送（见下） |
| `handoff` | → Python 回调开始处理 | Python/GIL 或线程调度 |
| `send` | 所有包交给内核 | 网络或 socket 缓冲 |

- `queue`、`encode`、`handoff` 需要在编码器两侧加探针，只在 `--frame-log` 时启用：编码器输入探针让“队列 → 编码器”线程每帧都要拿 Python 的 GIL，平时不需要。不开时 `stage_ms` 只有 `driver` 和 `send`。
- **上一帧的发送会算进下一帧的 `queue`/`encode`。** 回调（含同步发包）运行在编码器的输出线程上，并且持有编码器的流锁；回调返回前，下一帧进不了编码器。所以先对照上一帧的 `t`（发包耗时）和 `w`（回调结束时刻），再判断是不是 MPP 本身慢。
- `driver` 依赖驱动给的时间戳：取的是帧开始还是帧结束，因驱动而异；v4l2src 认定时间戳不可信时，这一段会固定在约一个帧周期。
- 统计行里的 `stage_ms` 给出每段最近 180 帧的 `[p50, p95, max]`。
- `--frame-log 文件` 每帧写一行 JSON：
  - `s` 路号，`f` 帧号，`k` 是否 IDR，`b` 字节数，`p` PTS（流水线运行时间，ns）；
  - `c` 取帧时刻，`w` 回调结束时刻（都是 `perf_counter_ns`；缺采集时间的帧没有 `c`）；
  - `d`/`q`/`e`/`h` 上表四段，`a` 发送决定时的帧龄，`t` 发包耗时；
  - `r` 结果：`sent`、`expired`、`awaiting_idr`、`send_failed` 等。
- 关键帧请求也写一行：`{"s":0,"r":"key_request","n":3,"at":…,"rt":…,"ok":1,"why":"receiver"}`，`rt` 是请求的运行时间，`why` 是 `receiver`（PLI、重连、断链）或 `stagger`（错开两路 IDR）。该路第一个 `k=1` 且 `p ≥ rt` 的帧就是对它的响应，由此算出请求到 IDR 的延迟。
- 文件第一行是 `{"event":"start",…}`，含版本、设备、GOP 和字段说明。两路 30fps 约每小时 30 MB，只在测试时打开。
- 统计文件和逐帧日志每两秒 `fsync` 一次（在后台线程，慢存储不拖住主循环）。10-14 死机后统计文件末尾是 NUL 字节，说明数据还在缓存里没落盘。

## 板卡诊断（只读）

`rk_diag.sh` 收集定位停顿和死机需要的板卡状态，写成一个文本报告 `~/Public/fzh/run/rk-diag-时间-标签.txt`：

```bash
bash ~/Public/fzh/rk_diag.sh off   # 发送端停止时：机器狗自身的基线
bash ~/Public/fzh/rk_diag.sh on    # 双路发送运行约 10 秒后，在另一个 SSH 窗口执行
```

- 内容：
  - CPU 频率与温度、内存和 CMA；
  - 实时线程及其可用 CPU，谁打开了相机 / MPP 编码器 / RGA 设备；
  - 5 秒窗口内的中断与软中断速率，各线程的运行时间和“可运行但等不到 CPU”的时间；
  - UDP 与网卡计数，GStreamer/MPP 软件包版本，内核日志末尾；
  - 最后读取 Rockchip 相机与编码器的 procfs 状态。
- 开始时 `sudo -v` 一次，由你自己输入密码，脚本不保存。root 只用于读取：哪些进程打开了设备文件、`dmesg`、UDP socket 的所属进程（`ss -p`）、需要 root 的 Rockchip procfs 文件。`RK_DIAG_SUDO=0` 跳过这些部分。
- 不打开相机、编码器或 RGA 设备，也不运行任何 GStreamer 程序（加载 MPP 插件会打开编码器，插件缓存重扫会打开每个 `/dev/video*`）。不写 `/proc`、`/sys`，不改优先级、亲和性、频率、网络或服务，除报告外不写文件。全程约 15 秒。
- procfs 状态由相机/编码器驱动自己的回调生成，放在最后读；驱动若已卡死，读取可能卡住，但前面的内容已经写进报告。`RK_DIAG_PROCFS=0` 跳过这一部分。
- 报告和其他日志一起用原来的 `rsync` 传回电脑。

## 编码器排查（`--trace` 与 `rk_ab.sh`）

`--trace 文件` 另起一个只读的采样进程，与发送端不共用 Python 解释器，大约占一个核的 3–10%：

- 每 5 ms：硬件编码器两个核和 RGA 各完成了多少个任务。每个中断是一个任务，机器狗的和我们的都算。
- 每 20 ms：
  - 我们每个线程的状态（R 运行、S 睡眠、D 不可中断），以及睡着时停在哪个内核函数（wchan）；
  - 这些设备的中断线程和 `dmabuf-deferred` 的状态。
- 每秒：各线程的 CPU 时间。
- 时间戳与 `--frame-log` 是同一个时钟。某一帧卡在编码器里时，可以对出：
  - 那段时间硬件是在给别的会话干活，还是空着；
  - 我们的编码线程在等什么。
- 两路 GStreamer 元素按路编号（`camera0`、`raw_latest0`、`encoder0`…），线程名能区分前后摄。

10-14 18:13 的 trace 结果（两次 75 秒，共 44 次一路编码超过 200 ms）：

- 卡住期间两个硬件编码器核完成的任务数是平时的 69–72%，正好等于只少了卡住这一路（机器狗约 40 个/秒，我们两路约 58 个/秒）：硬件没有停，只是这一路不再提交任务。
- 卡住那一路的 MPP 编码线程 97–100% 的时间睡在用户态的条件变量上（`futex_wait_queue_me`），平时它约 5% 的时间在等硬件（`rkvenc2_wait_result`）；另一路的 MPP 线程照常工作。
- 这一路的 GStreamer 输出线程照常每 1 ms 轮询一次要包，CPU 用量和平时相同，所以不是 CPU 被抢。它的输入线程被挡住，后来的帧在 leaky 队列被丢。

10-15 的调试日志（gst、mpp、fix 三次，各 75 秒；一路编码超过 150 ms 的帧分别有 43、50、36 个）：

- 这些帧约六成是交给 MPP 之后过了 90–440 ms 才拿回包；交给 MPP 这一步本身通常不到 1 ms。其余约四成是在等编码器的流锁：插件的输出线程拿着流锁等前一帧的包，被前一帧拖住。
- mpp 运行里，交给 MPP 后超过 85 ms 才拿回包的 74 帧中，有 54 帧的 MPP 编码线程在“开始处理”之后，几乎到包出来时才做下一次决定；硬件编一帧只要几毫秒。trace 里这段时间它睡在 futex 上，插件的输出线程则一直睡在 `hrtimer_nanosleep`（下面说的 1 ms 睡眠）。
- 按代码和日志推断，原因在 MPP 的非阻塞要包函数（2024-11 版 `mpp/mpp.cpp` 的 `get_packet_async`）。板载 mpph264enc（2024 年初的版本）把 MPP 的输出设成非阻塞，每 1 ms 要一次包。队列空时这个函数睡 1 ms 再看，睡的时候一直拿着输出包队列的锁，两次之间只放开几微秒。编码线程编完一帧，要拿同一把锁才能把包放进队列，抢不到就接着睡。
- 上游 2026-07 的等待条件修复（321a8ae1，fix 运行）没有改变：三次的停顿次数和长度相当。按代码推演，它在 RK3588 的 H.264 上本来就不起作用。

`mpp-pkt-poll-fix.patch` 只改这一处：睡之前放开锁，睡完再拿回来。用它编译的库在 `mpp-pkt/`。上游 MPP 2026-09-17 的 develop 仍是同样的写法，换新版 MPP 解决不了；gst-rockchip 2024-04-17 之后的插件改为等 1 ms 的条件变量（等待时放锁），不走这条路。

10-15 10:36 的对照（gst、pkt 交替两轮，各 75 秒，每路的数）：

| | 板载 MPP（gst、gst2） | mpp-pkt（pkt、pkt2） |
|---|---|---|
| 编码 p50 / p99 / 最长 | 10–11 / 122–143 / 290–571 ms | 5 / 13–16 / 22–46 ms |
| 其中 MPP 收下 → 拿回包 p50 / p99 | 5–7 / 82–93 ms | 2.6 / 6–9 ms |
| 编码超过 150 ms 的帧（两路合计） | 34、30 | 0、0 |
| 编码前丢帧 | 47–63 | 0 |
| 接收端 200 ms 以上的停顿 | 7–11 次，最长 0.37–0.72 s | 0，最长间隔 68–101 ms |
| 接收端显示帧率 | 26.9–27.7 fps | 29.7–29.9 fps |
| 延迟 p50 / p95 / p99（交帧 → 浏览器提交） | 26 / 54–61 / 76–86 ms | 20 / 29–31 / 37–41 ms |

- 发送进程 CPU（22–24%）、硬件编码器每秒任务数（约 99 个）、码率和帧大小都与板载 MPP 相同。
- 板载 MPP 那两次接收端有 6–8% 的帧没显示（太旧或被更新的帧替代），所以它的延迟尾部比表里更差；mpp-pkt 约 1%。

`rk_ab.sh` 默认交替跑 stagger、aligned 各两次两路，每次 75 秒，中间停 10 秒，共约 5.5 分钟。先打开接收页（RTP、2 路），并让它一直开着：

```bash
bash ~/Public/fzh/rk_ab.sh 192.168.168.10        # 可加第二个参数改每次的秒数
RK_AB_RUNS="gst pkt" bash ~/Public/fzh/rk_ab.sh 192.168.168.10   # 自选运行、次序和次数
```

| 运行 | 内容 |
|---|---|
| stagger | 0.4.3 默认：`mpp-pkt/`，两路 IDR 错开，不开调试日志 |
| aligned | 同上，加 `--idr-stagger off`：两路 IDR 同时发出（同 10-15 的 pkt） |
| gst | 板载 MPP（`--mpp-lib system`）。GStreamer 记录编码插件对每一帧的处理：进入 handle_frame、交给 MPP、拿回结果（`GST_DEBUG=mppenc:5,mpph264enc:5`）（默认不跑，下同） |
| pkt | `~/Public/fzh/mpp-pkt/`，带 GStreamer 日志 |
| mpp | 同 gst，再加 MPP 编码线程自己的日志：每次睡下或干活的决定，以及收到的通知（`mpp_enc_debug=0x90`）。MPP 同时把这些行写进系统日志，每次约 4 万行 |
| fix | `~/Public/fzh/mpp-fix/`：上游 2024-11 版加 `mpp-async-wait-fix.patch`，带 GStreamer 日志；10-15 无变化 |
| base | `~/Public/fzh/mpp-base/`：上游 2024-11 版原样编译，带 GStreamer 日志 |
| sys | 板载 MPP，不开调试日志 |

- 调试日志都在 `运行名.log` 里。GStreamer 的行带时间戳；MPP 的行没有时间戳，按顺序夹在其间。同一种运行跑第二次时，文件名加序号（`stagger2`、`gst2`）。
- 每次运行都显式指定 MPP：stagger、aligned、pkt 用 `mpp-pkt/`，fix、base 用各自的目录，其余用 `--mpp-lib system`。缺这个库的运行会跳过，不会悄悄换成别的库。
- `--frame-log` 第一行记录 `mpp_lib`、`mpp_sha256`、`mpp_version` 和 `idr_stagger`。
- 每次运行都带 `--stats-file`、`--frame-log`、`--trace`，文件在 `~/Public/fzh/run/ab-时间/`。某次运行缺库或启动失败，会记录日志并跳过。Ctrl+C 停止当前运行，并跳过其余的。
- 这些库由 `deploy/rk_sender/build_mpp.sh` 在 Ubuntu 22.04 arm64（Mac 的 Linux 虚拟机，glibc 与 RK 相同）上编译，来源、补丁和 SHA-256 见 `.local/rk-sender/mpp-BUILD.txt`。不要在 RK 上编译：它是运控主板。

## 版本变化

0.4.4：

- 宽高必须是 16 的倍数（原来是 8）：1920×1080 会让 ISP 只取相机画面左上角的四分之一，改用 1920×1088。
- 启动时把所用节点上残留的裁剪恢复成整幅画面；`--doctor` 显示当前裁剪。

0.4.3：

- pyz 同目录有 `mpp-pkt/` 时自动使用其中的 MPP（`--mpp-lib system` 可关），两路编码停顿随之消失。
- 两路 IDR 错开（`--idr-stagger`，默认 on）；新增统计 `idr_staggers`，`--frame-log` 的请求记录带 `why`。
- `rk_ab.sh` 默认 stagger / aligned 交替两轮；用板载 MPP 的运行显式加 `--mpp-lib system`。

0.4.2：

- 新增 `--mpp-lib`：只在发送进程里换用另一个 MPP 库；启动时打印实际在用的 MPP 库及其版本。
- `rk_ab.sh` 改为带调试日志的运行，默认 gst / pkt 交替两轮（10-15 那次是 gst / mpp / fix）；新增 `build_mpp.sh`、`mpp-async-wait-fix.patch`、`mpp-pkt-poll-fix.patch`。
- `--pixel-format YUY2` 保留，但机器狗的 selfpath 不接受。

0.4.1：

- 不用 RGA 拷贝改为默认，`--rga` 可改回。
- 新增 `--pixel-format`（NV12 / YUY2）、`--zero-copy-pkt`、`--trace`。
- 两路 GStreamer 元素按路编号。
- 新增 `rk_ab.sh`。

0.4.0：

- 默认 GOP 改回 1 秒（等于帧率）。
- 关键帧请求带当前运行时间，只在接收端在线时发送；一次恢复只发一个请求；每路最多 10000 个。新增统计 `idr_forced`、`key_request_capped`，配合 `key_requests` 验证请求是否生效。
- 帧龄上限 1000ms（原 250ms），超过 75ms 照发并计入 `late`。
- 逐帧五段计时：统计里的 `stage_ms`，以及 `--frame-log`（编码器两侧的探针只在此时启用）。
- 统计文件和逐帧日志每两秒在后台 `fsync`。
- 新增只读诊断脚本 `rk_diag.sh`。
- 配套的电脑接收器在发送端不可达时继续运行。

0.3.0（10-14 实测后；GOP 与帧龄已由 0.4.0 取代）需配合更新后的电脑接收器（NACK 只请求真正的缺口）：

- 默认 GOP 改为 10 秒。
- 帧龄超过 75ms 照发并计入 `late`，超过 250ms 才断链。
- 发包预算：普通帧 15ms（不变），IDR 45ms，与接收端重组窗口对齐。
- 新增统计 `late`、`idr_send_ms_max`。
- 启动行显示 GOP。

0.2.0 针对实机出现的约 28–33 万字节 IDR，将其发包预算从 15ms 调整为 40ms，普通帧仍为 15ms，总帧年龄上限仍为 75ms，并增加 `--version` 和以下诊断：

- `receiver.state=waiting`：尚未收到有效接收反馈，先检查接收页是否点击“开始接收”、是否选 RTP、目标 IP 与端口。`active` 表示最近两秒收到反馈，`stale` 表示反馈中断；这不能单独证明浏览器已显示画面。
- `send_timeout`：发包预算耗尽；`send_socket_error`：socket 错误。`last_send_error` 保留最近一次失败的 errno、帧大小、IDR 标记、已发/总包数、耗时和帧年龄。`ECONNREFUSED` 通常表示目标端口没有监听；`EHOSTUNREACH`/`ENETUNREACH` 需要检查网络；不能把所有 `send_failed` 都归为带宽不足。
- `last_send_ms` 表示最近一次尝试的发包耗时；`last_send_error` 是历史最后一次错误，即便后来恢复也会保留。UDP 异步错误也可能在读取反馈时出现，见 `receiver.last_control_error`。
- 五秒没有完整发送，但采集和编码仍正常时会提示并继续恢复，不因接收端尚未上线而退出；接收端上线/恢复反馈时会补发 IDR 请求。发送端不需要为接收端晚启动而重启。

## 常见问题

- `No module named gi`：使用 `/usr/bin/python3` 并检查上面的发行版 GI 包；不要安装桌面项目的 PyAV 依赖。
- 无权限/设备忙：检查节点权限、占用和所选节点，停止**自己的测试进程**后重试。不要停止机器狗原有相机/运控服务。
- DMA-BUF 导出/导入失败：记录原错误，可以显式试 `--io-mode mmap`。这可能增加拷贝，不能宣称零拷贝；MPP 失败不会自动回退到 CPU 编码。
- 接收无画面：先选 RTP，再检查 IP、UDP 5004、防火墙、路数、`sent_fps` 与接收码率。若出现 `missing_headers`/`missing_capture_time`，保留日志，表示实际 MPP 输出与已验证的 AU/PTS 假设不一致。
- 超过五秒没有采集、编码或无法匹配采集时间会报错退出；仅发送失败时继续恢复并输出具体诊断。完整重启会生成新 epoch，现有接收端会清理旧流状态。
- 需要查看实际流水线：`/usr/bin/python3 ~/Public/fzh/rk_sender.pyz --print-pipeline`，该命令不会加载 GI、打开相机或发送数据。

## 开发验证

```bash
.venv/bin/python -m pytest -q deploy/rk_sender/test_sender.py tests/test_receiver_resilience.py
python3 tools/build_rk_sender.py
python3 -S .local/rk-sender/rk_sender.pyz --help
```

`test_sender.py` 也检查 `rk_diag.sh` 保持只读（不写 `/proc`、`/sys`、`/dev`，不调用改变系统状态的命令），并在 Linux 上实际运行一次。

实现位于 `video_demo/rk_sender.py`，打包器只纳入 `rk_sender.py`、`protocol.py`、`capture_time.py`、`webrtc_signal.py`、包初始化文件和本说明。它不改现有桌面发送器和接收器的协议。
