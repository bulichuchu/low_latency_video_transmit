# Vue 发送端 / 接收端

默认启动入口现在是 Vue 3 页面。前端负责配置、监看和浏览器解码显示，Python 继续负责真实摄像头采集、SDK、H.264 编码、RTP/UDP、重传和校时。普通运行使用 `frontend/dist/`，不从 CDN 下载脚本。

## 运行与部署

```bash
cd /Users/fushuai/Work/low_latency_video_demo
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python demo.py web --page sender
# 接收机
.venv/bin/python demo.py web --page receiver
```

两端可位于两台电脑，也可同机联调。每台电脑只需一个本机 Web 服务，两个导航页面共享服务；重复启动同一端口会复用已运行的服务。同机联调会启动接收后再启动发送；后续两端可独立停止。接收先启动，发送机填接收机 LAN IP 与 UDP 端口，路数一致。跨机选择“自动估计”时钟；“共享时钟”只能用于同机。

Web 服务默认监听 `127.0.0.1`，端口默认 8765；`--port` 调整的是 HTTP(S) 端口，页面中的端口是视频 UDP 端口。可用 `--bind 0.0.0.0 --allow-host 域名` 显式开放受信任网络访问；用 `--tls-cert` / `--tls-key` 加载浏览器信任的证书启用 HTTPS。远程 HTTP 可配置及启停，但浏览器视频解码需要安全上下文，详见 [LAN_SETUP.md](LAN_SETUP.md)。当前没有账号登录、WebRTC 公网穿透和浏览器直接接收 UDP；UI token 用于请求校验，不是身份认证。

macOS：`start_demo.command` / `start_sender.command` / `start_receiver.command`。Windows：对应 `.bat`。原 `demo/send/receive` 命令保留 Qt/无头路径。发送 UI 只列真实摄像头；`source=synthetic` 只用于开发测试。

macOS 的 Orbbec SDK 返回 `uvc_open -3` 时，先由用户在接相机的电脑上运行 `./start_sdk_helper.command` 并完成 sudo 授权。普通用户网页会自动使用已运行的本机助手；更新代码前已启动的网页服务需重启。助手不启动采集、不新增网络端口；实际采集仍由“开始发送”触发。详见 [SDK_CAMERAS.md](SDK_CAMERAS.md)。

关闭标签页不会停掉服务或发送程序。按钮停止对应端；Ctrl+C 退出 Web 服务并关闭本机所有收发任务。每个角色每次启动创建独立 `runs/*-web-sender-*` / `runs/*-web-receiver-*`，不覆盖旧结果。页面不持久化 RTSP 地址；传给发送子进程的凭据放在环境变量里，日志与状态返回值脱敏。

## 数据路径及取舍

```mermaid
flowchart LR
  V[Vue 发送页面] -->|本机 HTTP 配置| C[Controller / Session]
  C --> TX[Python sender 子进程]
  U[USB / RTSP / SDK] --> TX
  TX -->|H.264 RTP / UDP 局域网| RX[Python receiver 收包 / 重排]
  RX --> B[BrowserBridge 每路最多 3 个压缩帧]
  B -->|原 H.264 / 本机 WebSocket| W[WebCodecs 每路独立 VideoDecoder]
  W --> M[浏览器 FrameMatcher 每路最多 6 个 VideoFrame]
  M --> D[Canvas 同一 rAF 提交多路]
  D -->|逐帧计时批量回报| J[Journal / CSV / JSON]
  W -. 请求关键帧 .-> B
  B -. PLI .-> RX
  RX -. 校时 / NACK / PLI .-> TX
```

接收 Web 路径复用 `receiver.py` 的 RTP 重组、重排序和缺帧后等待 IDR 逻辑；重排好的压缩帧交给 `web_sink.offer()`，跳过 Python H.264 解码、RGB 数组转换及 Qt 绘制。浏览器直接解码 Annex B H.264，`VideoFrame` 画到 Canvas 后及时 `close()`。没有 JPEG 重编码、base64 或 HLS 分段。

Vue 不参与逐帧像素响应式更新。媒体类管理 Canvas / 原生帧，统计每 500ms 更新一次 Vue。浏览器请求 `optimizeForLatency` 与硬件优先，但硬件是否实际采用由浏览器决定，页面不会虚构硬件命中结果。H.264 配置字符串从实际 SPS 提取，关键帧必须含参数集。

边界：默认 WebSocket 用于同机浏览器最后一跳；远程域名访问会额外经过“接收服务 → 浏览器”的网络路径。HTTPS 自动使用 WSS，它仍是 TCP，会受网络、进程调度及浏览器缓冲影响。桥接队列满时清掉该流等待新 IDR；单次 WS 写等待超过 250ms 则断开重连。浏览器解码队列超过 3、待输出超过 8 或出现序号断裂则重建解码器；若当前包已是 IDR，直接从该包恢复，否则才请求关键帧。每路浏览器请求至少间隔 200ms，避免重复请求清掉刚到达的恢复帧。解码后过期帧不显示。这里不是对稳定物理 <100ms 的保证。

一个接收会话只允许一个活动预览页面。第二个页面会提示重连/占用；关闭前一个即可接管。页面刷新、发送进程重启或回到前台后重新请求关键帧。隐藏标签页期间停止解码/显示，发送和 UDP 接收继续工作。操作系统或浏览器主动限制后台程序时，恢复时间取决于平台。

## 关键类与函数

| 文件 / 类或函数 | 作用 |
|---|---|
| `webapp.media_args(role, data, directory)` | 将白名单配置转为原 CLI Namespace，复用范围/枚举校验；摄像头 profile 在内存验证，不落盘明文 URL |
| `webapp.Session.launch()` | 发送端创建独立 Python 子进程；接收端创建媒体工作线程，传入 BrowserBridge；原生 SDK 崩溃仍隔离在发送子进程 |
| `Session.stop()` | 写 STOP 文件，按原 stop_child 逻辑优雅等待发送端；接收线程结束后可重用 UDP 端口 |
| `Session.snapshot()` | 增量读取已有事件日志，返回脱敏错误、设备实际参数、发送帧率/码率与浏览器连接情况；速率按最后一条发送事件之前的两秒计算，避免异步刷盘造成假性低帧率；返回 sample_lag_ms，超过两秒没有新记录则速率置零 |
| `Controller.start()/stop()` | 异步锁串行化生命周期，拒绝覆盖运行中的会话，等待接收 ready 后才允许联调发送 |
| `normalized_hostname()` / `web_settings()` | 验证域名/IP 白名单、监听 IP、TLS 证书/私钥成对配置，创建 SSLContext 和对外访问 URL；不启动监听 |
| `create_app()` | 注册控制与视频接口；拒绝白名单之外的 Host、跨 Origin 请求；Origin 根据实际 HTTP/HTTPS 校验；修改接口与 WS 要求每次服务启动生成的 UI token |
| `run_web()` | 默认启动 localhost 服务；可显式开放 LAN/HTTPS。仅默认本地模式复用旧服务，改变监听或 TLS 时需要停止旧进程再启动 |
| `BrowserBridge.bind()/unbind()` | 连接/断开 receiver 的关键帧请求、日志和实时统计；锁保护媒体线程与事件循环之间的访问 |
| `BrowserBridge.offer()/pop()` | 非阻塞接收原压缩帧；有界队列、慢消费者恢复；封装二进制包及实际时钟映射结果 |
| `BrowserBridge.attach()/recover()` | 连接改变或解码失步时清掉受影响的旧压缩数据，从 IDR 恢复，保持 P 帧参考依赖 |
| `BrowserBridge.browser_recover()` | 验证路号及原因，单独记录 browser_recovery，然后进入 recover；便于区分浏览器重置与 UDP 丢包 |
| `BrowserBridge.telemetry()` | 根据已发送的 `(stream, epoch, frame_id)` 关联浏览器回报；限制大小、过滤非有限值、去重；不会把网页回报记成原生 decode |
| `App.vue` | 两个路由页面及共享布局；首次访问时才挂载相应页面，导航不停止运行任务 |
| `SenderPanel.vue` | 设备/SDK 异步枚举、RTSP 输入、统一输出质量、目标 IP、启动/停止、同机联调和发送状态 |
| `CameraInput.vue` | 一路设备的选择、自动模式、实际模式组合与 FPS 范围、无模式列表时手动配置、深度预览量程 |
| `ReceiverPanel.vue` | 接收配置、生命周期状态同步、Canvas 引用、统计、全屏和测试报告 |
| `api.js` | 同源 HTTP API、启动 token，按页面协议选择 WS/WSS、保留域名端口；不把密码写 localStorage |
| `VideoPlayer.connect()/control()` | WS 建连、接收当前会话配置、ping/pong 估计浏览器 performance.now 与 Python 单调时钟偏移 |
| `VideoPlayer.packet()/dropStream()` | 从二进制包读元数据与 H.264；按路维护 decoder、epoch、PTS 映射、依赖连续性与有界待输出表 |
| `VideoPlayer.key()` | 按路限制恢复请求频率；已有可用 IDR 时不再请求，避免桥接暂停与序号断裂互相触发 |
| `DisplayPacer.due()/submitted()` | 显示节拍沿固定时间轴推进，容忍 0.5ms 的 rAF 时间戳取整；跳过错过的时隙，不补播积压帧；仅实际绘制才消耗额度 |
| `VideoPlayer.tick()` | rAF 内按显示 FPS 上限取匹配组，过期检查、直接绘制 VideoFrame 并释放资源；全组绘制后计算可见集合的时间偏差 |
| `VideoPlayer.report()/close()` | 每 500ms 发布速率与计时，批量回传帧记录；关闭时清理计时器、WS 和原生解码/图像对象 |
| JS `FrameMatcher.add()/poll()/clear()` | 最近帧模式立即选择各路最新值；对齐模式按容差和有限等待选组；每路最多六帧，丢弃和清理均释放原生帧 |
| `avcCodec()` | 从 Annex B SPS 获取 profile/constraints/level，构造实际 `avc1.xxxxxx` |
| `cameras.validate_profile()` | 文件与 HTTP 共用 profile 校验；保留原 `read_profile()` 文件入口 |
| `metrics.summarize()` | 分开汇总原生 `decode/submit` 与 `browser_submit`；新增浏览器分段、帧间隔和可见取帧偏差 |

`receiver.run_receiver(args, web_sink=None)` 的默认 `None` 完全保留原生对照路径。Web 接收传入 bridge 并设置 `ui_backend=webcodecs`；原生 `FrameMatcher` 不产生帧，真正的配帧工作在 JS 中完成。因此 Web 报告的 `decode_latency_ms` 为零样本是正常的，应读取 `browser_submit_latency_ms`。

## 控制 API 与 WebSocket

| 接口 | 内容 |
|---|---|
| `GET /api/bootstrap` | 应用标识、UI token、已登记 SDK 路径 |
| `GET /api/cameras` / `?sdk=1` | 本地 / SDK 能力枚举，不启动采集 |
| `POST /api/sdk` | `{path}` 登记 SDK 目录，原系统权限规则不变 |
| `POST /api/sender/start` | `{cameras:[...], width,height,fps,bitrate_kbps,encoder,host,port}`；支持原 SDK/RTSP profile 字段 |
| `POST /api/receiver/start` | `{streams,bind,port,clock_mode,sync_mode,sync_wait_ms,sync_tolerance_ms,max_age_ms,display_fps}` |
| `GET /api/{sender|receiver}/status` | 当前会话状态、运行目录、错误及指标 |
| `POST /api/{sender|receiver}/stop` | 优雅停止并生成 CSV / summary |
| `GET /api/{sender|receiver}/report` | 下载当前会话停止后生成的 summary.json |
| `WS /api/video?token=…` | 本机 H.264 预览与 ping/key/telemetry 反馈 |

修改接口携带 `X-Video-Token`。开放 LAN 后，能访问此服务的客户端可获取 token 并控制收发，因此仅用于受信任网络，不作为公网管理后台。SDK 目录填写服务所在机器的路径，不是浏览器文件上传，不会上传 SDK。TLS 直接由 Python 服务处理；不默认信任客户端提供的 X-Forwarded-* 头。

视频二进制消息：`4 字节大端 JSON 长度 | UTF-8 JSON | 一帧 Annex B H.264`。JSON 包含 stream、epoch、frame_id、key、depth_preview、capture_ms（发送端取帧时钟）、host_capture_ms（映射到接收端时钟，可能 null）、host_sent_ms、uncertainty_ms。

客户端 JSON：`ping` 携带 t1，服务端 pong 回 t1/t2/t3；`key` 携带 stream 及可选 reason 请求 IDR，原因按白名单写入 browser_recovery；`telemetry` 携带最多 1024 条逐帧提交记录。浏览器时钟至少三次有效回包且样本新鲜后才报告映射，未知/负值不伪造为 0ms。发送端与接收端的时钟估计仍走原 UDP 校时，两段不确定度相加记录。

## 指标定义

- **发送机 ↔ 接收机 · UDP RTT**：复用两端 UDP 校时探测，显示最近一次往返耗时 `(t4 − t1) − (t3 − t2)`，扣除对端处理区间；仍包含系统队列与调度开销。由接收端 API 返回 `receiver.udp_rtt_ms`，不要求浏览器已收到视频，也不要求时钟偏移已收敛。共享时钟模式照样测量实际 RTT，没有样本时不填 0。
- **接收服务 ↔ 浏览器 · 预览 RTT**：使用 WebSocket ping/pong 的同一公式；从当前浏览器测量，经过 SSH 隧道时包含该路径。每约 0.5 秒更新，UDP 指标随接收状态每约一秒刷新。两项均展示最近样本，不取校时用的最小值；高于 200ms、但在两秒探测范围内的慢回复仍显示，时钟拟合筛选不变。超过五秒未收到有效回应显示 `—`，更换发送对端、关闭接收或预览重连会清除对应旧样本。
- 两项 RTT 为全会话指标，多路共用同一传输连接。它们不是逐帧单程传输耗时，不直接除二冒充实测值，也不能加到“视频延迟”上；相应的单程传输已包含在应用取帧到提交的链路中。目前这两项用于实时显示，原视频延迟报告的统计口径不变。
- “测试记录与丢帧”显示每路浏览器收到 / 解码 / 显示 FPS：分别计压缩帧到达、VideoDecoder 输出、Canvas 提交，每 500ms 更新。它们不是相机采集 FPS，也不是屏幕实际出光 FPS。
- **视频延迟 · 估计**：最近提交帧的 `Canvas 提交时刻 − 计时起点`，不是平均值。优先使用该帧已映射的相机采集时间戳；不可用时使用应用取帧时间，并在主指标下明确标注“应用取帧（仅应用之后的链路）”。校时无效或尚未提交帧时显示 `—`。不同起点的数值不可视作同一测量范围。
- **画面年龄**：`统计时刻 − 同一帧的同一计时起点`，等于视频延迟加提交后经过的时间。停帧时增长，停止会话后显示未知。SDK 示例：视频延迟 60.5ms、应用链路 35.2ms、提交后经过 5.8ms，则画面年龄 66.3ms，相机采集→应用取帧为 25.3ms。未提交的新帧不会更换旧画面的计时起点。
- **延迟分段与计时说明**：折叠显示相机采集→应用取帧、应用取帧→画面提交、提交后经过的时间，以及 SDK 状态和校时误差。无相机时间戳时相机前段为 `—`，不填 0；RTSP 应用起点之前的相机编码、输入网络和输入解码不计入。SDK 曝光参考点与拟合误差未标定，所有指标的提交终点均不等于屏幕出光。
- **应用帧过期阈值**：继续按应用取帧时间判断，不随 UI 计时起点切换而改变丢帧策略。原始 `latency_ms`/`sensor_latency_ms` 及报告仍分别记录应用/相机范围，不混合汇总。
- **可见画面取帧时间偏差**：同一发送会话中，当前各路可见帧的应用取帧时间最大减最小；不是相机硬件曝光同步误差。
- `browser_submit_latency_ms` 统计的是已回报显示提交帧，过期被丢弃的帧不会混入；需结合丢帧、帧率、空窗和原始事件判断稳定性。不能只看低 P95 就宣称满足物理延迟验收。
- `browser_decode_ms` 包括浏览器输入到解码输出的排队/解码；`browser_wait_ms` 是解码后至绘制前等待；`browser_draw_ms` 是 Canvas 调用时间。真实屏幕出光仍为 `glass_to_glass_latency_ms: null`。
- 浏览器帧间隔由页面单调时钟计算；刷新页面会重置该时钟，跨页面的间隔不作为连续测量。后台暂停、启动和断流要结合事件时间线分析。统计回传有批量延迟，页面突然崩溃可能丢失最后一批记录。
- `metrics.csv` 的 `fps_measurement=browser_reported_submit` 表示服务端最近一秒收到的浏览器提交回报数量，会受 500ms 批量回报影响；页面实时 FPS 用浏览器实际提交计数。原生路径仍标记 `native_decode`，不会混为同一测量。

## 前端开发

Node 22.12+ 或当前兼容版本，已提供 package-lock。

```bash
cd frontend
npm ci
npm run build
npm test
# 先在另一终端启动 Python web 服务，再启动热更新开发界面：
npm run dev
```

Vite 开发服务仅绑定 127.0.0.1，并代理 `/api` 的 HTTP 和 WS 到 8765；变更后自动更新。生产页面在重新 `npm run build` 后刷新即可。`frontend/dist/` 随项目提供，前端源码修改后必须同时重新构建。

Python：`.venv/bin/python -m pytest -q`。`tests/test_web.py` 覆盖配置校验、凭据不落盘、本机接口防护、有界桥接/IDR 恢复、真实两进程 H.264 UDP 到 WebSocket、回连、报告和再次启动。JS 测试覆盖配帧、有限等待、过期和资源释放、SPS 解析。

## 2026-09-29 双路低帧率修复

Air 的两路 1280×720@60 SDK 摄像头发送日志，在所取的连续 10 秒内各有 600 帧。Mac mini 的 `20260929-115739-web-receiver-42c13c` 报告记录了 13.13 秒：S0/S1 显示平均 10.05/26.04fps，PLI 请求分别 68/2 次，浏览器解码均值 3.71/1.47ms。这些数据包含启动与恢复阶段，并非物理延迟验收。

发现并修复了两处可独立复现的代码问题：

1. 浏览器收到序号不连续的 IDR 时，无条件再次请求 IDR。此请求会使桥接清理后续 P 帧；下一 IDR 又有序号间隙，从而可能持续重复恢复。现在直接使用已经收到的 IDR，并限制重复恢复请求频率。
2. 显示截止时间每次设为 `now + 1000/fps`，rAF 时间取整后会漏过可显示的刷新。改用固定节拍，在停顿后跳过过时显示时隙，不补播旧帧。

旧代码运行新增的两项回归测试均失败：关键帧恢复产生 1 个多余请求；10 秒、取整到 0.1ms 的 60Hz 双路调度，每路只提交 400 帧。修复后分别为 0 个多余请求及每路 600 帧。此处是模拟浏览器调度/解码回调的测试，不是实机达到 60fps 的声明。11 项 JS 测试、23 项 Python Web/指标测试及 Vue 构建通过。

真实双机 + SSH 预览的修复后帧率待用户更新 Mac mini 并复测。建议先使用最新帧模式、发送及显示上限 60fps、100ms 过期阈值，保持原分辨率/码率做前后对比，再评估多路对齐的额外等待。更新步骤见 [LAN_SETUP.md](LAN_SETUP.md#7-更新双路低帧率修复)。

## 2026-09-28 验证记录

macOS Apple M4、PyAV 18.1、Vue 3、Vite 7、Codex 内置浏览器。前端构建通过；Python 回归 92 项通过，JS 4 项通过。GUI 的 SDK/RTSP 配置入口迁移已完成；本次未新增管理员 SDK 实拍或独立 PoE 实机验证。

1. **真实内置摄像头**：在 Vue 页面选择 MacBook Air 相机，640×360 输出、30fps 上限、1000kbps、libx264，同机 UDP → WebCodecs。发送 281 帧，记录到 274 次浏览器提交；界面稳定时约 30fps，应用取帧→提交 P50 14.11ms、P95 23.33ms、最大 64.05ms。发送与接收日志无错误。此短测非物理延迟验收，未保存摄像头画面。报告：[发送](docs/results/vue-20260928/real-camera-sender.json)、[接收](docs/results/vue-20260928/real-camera-receiver.json)。
2. **双路协议/浏览器测试**：开发测试合成源，每路640×360@30；对齐模式、8ms 等待、18ms 容差。浏览器每路记录573次提交，已知时钟样本547个；P95约27.30/27.40ms，已记录可见取帧偏差为0ms。测试包含主动停流、刷新页面、发送进程重启，不能拿其整个会话平均帧率作稳定性结论；一处重组不完整丢帧被记录。已确认重启后两路重新显示约30fps。报告：[双路浏览器](docs/results/vue-20260928/synthetic-browser-receiver.json)。这些图像仅用于测试，产品界面默认选择真实摄像头。
3. 通过页面停止接收，并通过 API 测试验证报告下载；枚举查询、输出质量预设、前端路由、断流停帧提示均验证。原Qt/无头测试继续通过。

实现依据：[Vue 官方快速开始](https://vuejs.org/guide/quick-start)、[WebCodecs](https://www.w3.org/TR/webcodecs/)、[H.264 Annex B 与 SPS/PPS 约定](https://www.w3.org/TR/webcodecs-avc-codec-registration/)。

## SDK 采集时间戳链路

SDK 入口先检测 `ob_device_is_global_timestamp_supported`，在启动采集前启用 `ob_device_enable_global_timestamp`。每帧读设备、主机收到和 Global Timestamp；SDK 设备时钟无需与另一台相机重置到相同数值，使用各自映射后的主机时间即可比较。硬件曝光同步仍是另一项能力。

`capture_time.GlobalTimestampMap` 用相邻两次 `perf_counter_ns()` 夹住 `time.time_ns()`，将 SDK 的 epoch 微秒转换为本机单调纳秒。SDK Context 的临时配置固定 `Device/ClockSource=Realtime`，不修改厂商文件，也不在采集中切换时钟。映射不从 USB 到达时间反推采集时间，因此不会扣掉要测量的相机/USB 延迟。

开始后至少检查两秒、最近八个有效样本，并检查相邻设备/Global Timestamp 单调性、拟合偏移变化、未来时间和主机时钟跳变。零值、不支持、API 失败、异常与旧助手分别显示明确状态，新指标为 `—`；图像和原应用计时继续工作。这些检查只能发现异常与不稳定，不能证明 SDK 拟合绝对误差。主机/网络校时误差估计不包含 SDK 拟合误差。

发送端把源时间戳保存在原始帧槽和编码 PTS 对应表中，RTP v2 扩展逐分片携带 `sensor_capture_ns`、`sdk_device_timestamp_us`、`sdk_global_timestamp_us`、状态及主机映射采样误差。`capture_ns` 继续表示程序取帧时刻，原延迟、过期阈值及取帧对齐语义不变。新版接收端兼容 v1；**旧接收端不支持新的 SDK v2 数据包，因此两台机器必须通过 GitHub 同步本次代码后再发送**。

WebSocket 增加 `host_sensor_capture_ms`、`sensor_status`、`sensor_clock_uncertainty_ms` 及 SDK 原时间戳；网页将其映射到浏览器时钟，分别计算采集延迟和应用延迟。UDP 或浏览器校时样本超过五秒未更新时，采集延迟不继续显示旧校时下的数值。当前多路配帧与同步偏差仍使用应用取帧时间。

报告增加 `sensor_to_browser_submit_ms`、`sensor_to_native_decode_ms`、`sensor_to_native_submit_ms` 和 `sensor_timestamp_states`。逐帧 CSV/JSONL 保留原时间戳、状态、采集延迟及主机/网络校时误差。`sensor_sdk_fit_error_ms` 与 `glass_to_glass_latency_ms` 均保持 `null`，不填入无法验证的精度或物理延迟。前端会显示每路时间戳状态；详情字段见 `capture_time.py`、`protocol.Meta`、`BrowserBridge.offer/telemetry` 和 `VideoPlayer.packet/tick`。

更新后重启两端 Web 服务并强制刷新页面。在发送机原 SDK 助手终端停止旧助手，重新运行 `start_sdk_helper.command`，确认出现 `global-timestamp-v4`。旧助手无法热加载新逻辑，会显示“SDK 助手版本较旧”；接收机不需要 SDK 助手。正常开启 SDK 采集后自动协商和校时，无需手动填写时钟偏移。

参考：[Orbbec 帧时间戳 API](https://orbbec.github.io/docs/OrbbecSDKv2/_frame_8h.html)；当前安装的 SDK 2.9.3 头文件规定 `OB_CLOCK_TYPE_REALTIME=0` 为 epoch 主机时钟。是否支持和能否稳定输出 Global Timestamp，以具体设备/固件实测为准。

2026-09-29 软件验证：154 项 Python 测试、13 项 JS 测试及 Vue 构建通过。包括设备 API 启用顺序及失败回退、SDK 助手元数据保真、带采集时间戳的真实 H.264/UDP 独立进程链路、时钟跳变/回退/未收敛、协议分片与 v1 兼容、浏览器时钟过期、两类延迟独立报告。本机 SDK 2.9.3 动态库确认具有所需三个 Global Timestamp API 符号。用户选择暂不进行实机验证，因此 Gemini 305/335L 的能力返回、收敛时间、实际采集延迟与映射误差均仍待实测；软件测试未用于证明真实采集精度。
