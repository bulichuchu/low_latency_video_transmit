# 延迟方案评估与改进记录 · 2026-09-27

## 结论

目前方案不是可证明的“最优解”，也没有通过真实场景到屏幕稳定小于 100ms 的光学验收。本次已经改进当前 Demo 的转换、统计、配帧与显示路径，并增加逐路最新画面的可选模式；保留同一批设备的改前/改后数据和硬编码失败记录。

对于当前“可信局域网、网络延迟很小、几路真实摄像头、允许反馈”的条件，先优化媒体处理与显示等待比直接更换网络协议更有依据。长期产品化可评估 GStreamer/C++ 媒体管线及原生 GPU 显示；这条路线有减少 Python 调度和 CPU/GPU 拷贝的潜力，但尚未在本项目中实测，不能给出毫秒承诺。

## 已落实的修改

1. **转换器复用**：发送端和接收端每路独立持有 VideoReformatter，避免每帧重新建立转换上下文；接收端 RGB 转换使用一个转换线程，避免与多路解码竞争大量线程。
2. **减少显示图像复制**：VideoSurface 持有原始 RGB 数组引用，QImage 直接引用其内存，绘制时等比缩放；移除每帧 QPixmap 创建与预缩放。这仍是 CPU 图像显示，不叫零拷贝硬件视频。
3. **新帧驱动提交**：有新帧时尽早更新，空闲状态更新不占用下一帧额度；刷新间隔按开始时间计算，绘制耗时不再额外重复等待。
4. **减少统计和队列额外工作**：LiveStats 用滚动计数替代每次快照重新累加所有 RTP 包；已有连续待解码帧时不再额外阻塞等下一个队列项；初次收流不重复创建解码器。
5. **提交前再检查过期**：帧经过配帧/UI 等待后再次检查年龄，过期时保留此前画面并明确标为旧帧。Qt 内部仍可能阻塞，因此这不是硬实时截止保证。
6. **两个明确模式**：默认 aligned 仍按时间戳配帧；新选项 `--sync-mode latest` 逐路取最新帧，不等待慢摄像头。latest 会放宽同屏画面时间对齐，不能和 strict-sync 同时使用。
7. **可定位的指标**：新增 10 项阶段分布、同屏包含旧帧的时间戳跨度；继续保留晚帧、丢弃、未解码帧及超 100ms 次数，不只展示成功快帧。

没有降低公共输出分辨率、目标码率或修改摄像头品牌逻辑；测试所用的逐路帧率固定配置仅用于公平比较，不会强制写入默认配置。

## 固定条件下的真实摄像头测试

平台：本机 macOS 27 / Apple Silicon，Python 3.13.15、PyAV 18.1.0、Qt 6.11.2。三路均是真实摄像头；libx264 编码、VideoToolbox 解码；发送/接收独立进程、UDP 回环、带 Qt 窗口，每次请求运行 20 秒，默认每路 3000kbps，公共输出目标 1280×720@30。期间不保存相机图像。

固定输入为：S0 内置相机 1280×720@30 NV12；S1 外接 RGB 相机 1280×720@30 NV12；S2 外接视频预览设备 1280×800@15 NV12。之前使用 S2 声明的 25fps 模式时出现过实际 15/25fps 波动，所以这里显式固定 15fps。实际采集 FPS 中位数在各次测试约为 30、30、15。

下表是**应用取帧 → 窗口提交 P95**，包含本次记录中全部提交帧，不是场景到屏幕出光延迟。

| 模式 | S0 | S1 | S2 |
|---|---:|---:|---:|
| 改前，默认对齐 | 53.12ms | 52.69ms | 52.54ms |
| 改后，默认 aligned | 48.98ms | 46.91ms | 49.37ms |
| 改后，可选 latest | 40.37ms | 36.93ms | 42.97ms |

相对改前，这次 aligned 的提交 P95 降低约 3.2–5.8ms；latest 降低约 9.6–15.8ms。latest 中配帧等待 P95 约 4.6–5.0ms，aligned 约 19.0–24.4ms；即使 latest 不主动配帧等待，也仍有主线程被绘制占用的时间。

不能据此声称所有指标都改善：本次改后解码 P95 有上升，且仍有启动/运行中的尖峰和丢帧。

| 运行 | S0/S1/S2 解码 P95，ms | 发送 / 解码（合计） | 超 100ms 的解码样本 | 超 100ms 的提交样本 |
|---|---|---:|---:|---:|
| 改前 | 20.26 / 18.62 / 23.49 | 1422 / 1422 | 6 | 1 |
| aligned | 21.52 / 19.13 / 29.16 | 1418 / 1415 | 13 | 2 |
| latest | 21.50 / 19.44 / 28.93 | 1432 / 1431 | 6 | 2 |

aligned 的 3 个未解码帧均记录为 incomplete_rtp_frame；latest 为 1 个。各路 RTP 平均发送码率约 2.7–2.8、2.7、1.4Mbps，含 RTP 头和重传，按完整记录时长计算；第三路实际 15fps，不会因为输出目标 30fps 就生成缺失帧。本次未做独立主观画质评分，保留原编码参数不等于已经完成画质验收。

这是同机相继进行的短时实验，不是固定画面、随机顺序、多轮重复的统计试验；光照、相机相位、启动开销、后台负载和系统合成均可能影响结果。数据支持“减少配帧/显示等待有效”，不支持“尾延迟和可靠性已经全面更优”。后续仍需要长时间、固定场景、两机 LAN、不同路数和码率的测试。

latest 运行的同屏画面应用时间戳跨度 P95 为 **61.65ms**，这是包含保留上一帧的真实显示候选统计。仅看 latest 偶尔形成的完整匹配组，其偏差 P95 为 9.23ms，会严重低估同屏时间差，不能这样解释同步质量。aligned 的完整匹配组偏差 P95 为 17.36ms；该值同样不能替代可见画面与曝光同步的测量。

## 对照实验与未采用的路径

### 显示复制对照

保持本次代码和 latest 模式，仅恢复旧 QPixmap 显示路径，提交 P95 为 42.16 / 37.37 / 44.59ms，UI 处理 P95 约 19.2–19.8ms；新 VideoSurface 对应为 40.37 / 36.93 / 42.97ms，UI 处理 P95 约 17.2–17.8ms。单次结果显示小幅收益，不能据此宣称消除了系统显示等待。对照结果没有被从报告中删除。

### VideoToolbox 编码

`doctor` 的小尺寸色块探测显示 libx264 和 VideoToolbox 都能在输入第 0 帧后输出。单路内置相机取出的真实 NV12 帧内存测试也能编码。但三路真实摄像头同时运行时，VideoToolbox 出现超过 16 个输入帧未返回压缩包，程序按保护规则终止。原因尚未确定，不能归咎于相机型号，也不能据此断言所有硬件编码都不适合低延迟。

因此默认继续使用当前能完成三路传输的 libx264，显式硬件选项仍保留用于研究。当前 factory 的打开成功检测只验证创建，不能代替真实输入、多实例持续运行验证；运行时没有自动换编码器功能。

Apple 提供低延迟 VideoToolbox 模式，FFmpeg 实现也会在 H.264 low_delay 标志下设置相关开关。当前项目已设置该标志；“再加 realtime”不是对本次问题的充分修复。[Apple 低延迟编码说明](https://developer.apple.com/videos/play/wwdc2021/10158/)、[FFmpeg VideoToolbox 实现](https://ffmpeg.org/doxygen/8.1/videotoolboxenc_8c_source.html)。

## 是否换方案：按使用条件选择

| 路线 | 适合条件 | 对当前项目的判断 |
|---|---|---|
| 当前 H.264/RTP/UDP + 有界反馈 | 可控 LAN，配套发送/接收程序，希望清楚掌握每级耗时 | 保留为可验证原型；已证明网络组包通常不是本轮最大等待来源 |
| GStreamer/C++ 原生管线 | 要扩展路数、持续运行、选择平台硬件编解码和显示后端 | 优先评估的产品化路线；逐级限制缓存并保留时间戳，不是换框架后天然低延迟 |
| 原生硬件表面 → 编码/解码 → GPU 显示 | 平台已确定，CPU 拷贝/显示是实测瓶颈 | 有潜力进一步降耗时与 CPU；需要各操作系统后端，不能冒充本次已完成的零拷贝优化 |
| WebRTC | 需要浏览器、跨网、NAT 穿透、拥塞适应及安全会话 | 能补产品能力，但仍需要管理抖动缓冲和显示链；当前 LAN 条件下没有证据证明它一定比 RTP 更快 |
| 相机原生 H.264 直接转发 | 相机确实可输出低缓存、无 B 帧的 H.264，且码流参数可用 | 可省一次主机重编码；相机内部编码缓存和时间戳来源必须测量，当前并未实现直通 |
| MJPEG / raw | 极低处理成本优先，网络带宽充裕 | 可作基准，但码率和画质代价需要评估；720p60 8bit NV12 原始像素约 664Mbps/路，三路约 1.99Gbps，未计协议开销 |

GStreamer 官方说明 x264 的缓冲与 zerolatency 的画质取舍；RTP jitterbuffer 的等待预算也是显式延迟来源。由此推断，迁移时应重点控制编码、队列和 sink 的行为，而不是照抄默认管线。[x264enc](https://gstreamer.freedesktop.org/documentation/x264/index.html)、[rtpjitterbuffer](https://gstreamer.freedesktop.org/documentation/rtpmanager/rtpjitterbuffer.html)。WebRTC 的原生 API 可承载完整实时通信应用，但该能力本身不提供本项目的物理延迟保证。[WebRTC 原生 API](https://webrtc.github.io/webrtc-org/native-code/native-apis/)。

要追求更低的真实总延迟，还需测曝光/帧周期、ISP/USB/驱动缓存、系统合成和显示扫描。15fps 的采样周期约 66.7ms，30fps 约 33.3ms，60fps 约 16.7ms；这说明提升实际相机采样率可能重要，但不能把这些周期直接当作每帧曝光时长或实测总延迟。需要严格多路曝光对齐时，应选择共同硬触发或经验证的硬件时间戳方案，软件配帧不能替代。

## 使用与复现

默认启动方式不变，仍为真实摄像头和 aligned。测试逐路低等待模式：

```bash
cd /Users/fushuai/Work/low_latency_video_demo
.venv/bin/python demo.py demo --sync-mode latest
```

已保存本机测试配置（设备名称在其他机器需替换）：

```bash
.venv/bin/python demo.py demo \
  --camera-profile docs/results/optimization-20260927/inputs.json \
  --sync-mode aligned --duration 20
.venv/bin/python demo.py demo \
  --camera-profile docs/results/optimization-20260927/inputs.json \
  --sync-mode latest --duration 20
```

报告包含启动/结束阶段，默认不剔除慢样本。全量事件在本机 `runs/optimization-20260927-*`；长期保留的汇总为 [比较与源码指纹](docs/results/optimization-20260927/comparison.json)、[改前](docs/results/optimization-20260927/before.json)、[aligned](docs/results/optimization-20260927/aligned.json)、[latest](docs/results/optimization-20260927/latest.json)、[旧显示路径对照](docs/results/optimization-20260927/pixmap-control.json)、[硬编码失败日志](docs/results/optimization-20260927/hardware-failed-send.log)。探索性、未固定实际帧率的早期测试仍在 runs 中，不混入正式对照表。

类、函数、协议字段、线程、状态机、阶段指标和排错入口见 [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md)。最终自动回归 **42 passed in 13.91s**，覆盖 aligned/latest 两种独立进程链路、Qt 实际绘制、颜色/PTS、阶段统计和其他既有功能；`git diff --check`、Python 语法和文档本地链接检查通过。外部高速录像尚未提供，真实总延迟仍为未测量。
