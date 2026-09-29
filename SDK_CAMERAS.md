# 专用 SDK 摄像头接入

当前提供 **Orbbec SDK v2** 适配器，和普通 USB/内置相机、RTSP 输入共同使用现有 H.264 / RTP / UDP 链路。代码按 SDK 枚举的序列号、传感器和模式工作，没有写死 Gemini 305 的 PID、分辨率或深度单位。其他厂商需要增加相应适配器，不能仅凭安装任意 SDK 就自动兼容。

## 输入图像与原示例的区别

用户提供的 `read_gemini305.cpp` 只启用 `OB_STREAM_DEPTH`，读取 Y16 距离值并保存 raw 文件。要显示普通彩色画面，必须启用设备实际提供的彩色流；不能把深度 raw 当作 RGB。

本次适配器枚举并提供：彩色、左/右彩色、红外、左/右红外、深度预览。窗口只列出当前 SDK/固件/工作模式实际返回、且当前转换器支持的图像模式；没有彩色流时不会伪造彩色或静默改用深度。Gemini 305 官方资料包含彩色成像能力，但实际可选流还受固件和当前相机工作模式影响。[Gemini 305 产品资料](https://www.orbbec.com/gemini-305/)

支持原生 RGB/BGR/RGBA/BGRA、YUYV/UYVY、NV12/NV21/I420、MJPEG、Y8/Y16 等图像。暂不接入 SDK 的 H.264/H.265 输入、点云、IMU，以及未明确识别的 Bayer/打包格式；无法确认的格式不进入可选列表。

深度预览依据每帧 `value_scale` 将 Y16 转为距离，再映射成 8 位灰度图：近亮、远暗、无效值 0 显示黑色。窗口可设置近端/远端距离。经 H.264 传输后它只是可视化图像，**不能还原精确深度或用于测距**。RTP 元数据携带深度预览标志，接收窗口会显示“深度预览”；两端需使用新版代码。

## 安装与启动

1. 从官方页面取得匹配操作系统、架构、设备型号与固件的 Orbbec SDK v2，并完整解压，保留 `lib`/`bin`、`extensions` 和配置文件。[官方下载](https://www.orbbec.com/developers/orbbec-sdk/)
2. 双击 `start_demo.command` / `start_demo.bat`。首次点击“选择 Orbbec SDK 目录…”；以后点击“查询 SDK 摄像头”。查询只读取元数据，不启动视频流，但 SDK 可能需要打开 USB 控制接口。
3. 勾选所需的 `… · 彩色 · SDK [序列号]` 等条目。自动选择或手动调整采集尺寸/FPS/格式，在下方调整输出画质，再点击“开始传输”。

当前每个物理 SDK 设备一次选择一种图像流；不同设备可并行使用。不要同时选择同一台相机的普通 UVC 和 SDK 入口，USB 接口可能被互斥占用。当前没有自动切换相机固件预设或改写固件。

本机已登记用户提供的 2.9.3 SDK 目录，路径保存在 `.local/camera-sdks.json`（Git 忽略）。移动目录后重新选择即可。也可使用环境变量 `ORBBEC_SDK_ROOT`，它优先于本机配置，适合部署及自动测试。无需额外安装厂商 Python wheel。

```bash
cd /Users/fushuai/Work/low_latency_video_demo
.venv/bin/python demo.py sdk --orbbec-root '/实际/OrbbecSDK_v2目录'
.venv/bin/python demo.py cameras --sdk
.venv/bin/python demo.py demo --cameras 'orbbec://实际序列号/color'
# SDK 相机与普通摄像头混用；流类型以查询结果为准
.venv/bin/python demo.py demo --cameras 'orbbec://实际序列号/color,普通摄像头名称'
```

逐路配置示例：

```json
{
  "version": 1,
  "output": {"width": 1280, "height": 720, "fps": 30, "bitrate_kbps": 3000},
  "cameras": [
    {"device": "orbbec://实际序列号/color"}
  ]
}
```

可指定 `width/height/fps/pixel_format` 限制输入；只在 SDK 确认支持时通过。`depth` 条目另可指定 `depth_min_mm/depth_max_mm`。网络摄像机仍使用 `rtsp://`；本适配器没有启用 SDK 自动网络设备发现。

## 权限及当前实测边界

### macOS 的可执行解决办法：本机 SDK 助手

遇到 `uvc_open ... Return Code: -3` 时，在**连接摄像头的 Mac** 上运行：

```bash
cd /Users/fushuai/Work/low_latency_video_demo
./start_sdk_helper.command
```

脚本会由系统 `sudo` 在终端提示管理员密码（输入时不会显示字符），只启动本机 SDK 助手。看到“SDK 助手已就绪”后保持终端开启。它尚未开始采集；需要实际查询和取帧才能确认设备访问成功。不要再给整个网页服务加 sudo。

如果原网页服务在更新代码前已启动，先在它原来的终端按 Ctrl+C，然后以普通用户重新运行：

```bash
.venv/bin/python demo.py web --page sender --no-browser
```

在发送页面展开“厂商 SDK · Orbbec”，点击“查询 SDK 摄像头”。列表有实际模式、且没有该设备的 `unavailable` 错误，才说明设备打开成功；出现“SDK 已通过本机管理员助手连接”只说明连接方式。Gemini 305 可能列出左彩色 / 右彩色，选择实际存在的一项并点击“开始发送”，收到帧后才算完成真机取图验证。不要同时用其他应用或系统 UVC 入口采集同一台相机。

普通用户终端也可仅查询（不启动图像流）：

```bash
.venv/bin/python demo.py sdk
```

结束时先停止发送，再在助手终端按 Ctrl+C。助手释放 SDK 设备并删除本机套接字；没有安装开机服务。设备重新插拔后可重新查询；SDK 路径修改后需重启助手。助手运行期会占用设备的 SDK 接口，不能当成一次性永久授权。若管理员助手仍然返回 USB 错误，需要继续检查设备占用、连接及 SDK/固件兼容性，不能把启动成功视为设备修复成功。

实现使用 `.local/orbbec-helper.sock`，没有新增 TCP/UDP 监听端口。目录 700、套接字 600，服务端根据内核提供的 UID 只接受启动用户；客户端验证助手进程为 root。SDK 目录在终端启动时固定，网页请求不能指定另一个库、执行命令或读写任意文件。密码只交给系统 sudo，程序不接收或保存密码；不修改 SIP、系统驱动、UVCAssistant 或 sudoers。

每次仅请求一帧，以原生图像平面传递（移除内存对齐填充），不额外压缩或转成 RGB。保留助手取帧时刻和 SDK 元数据，所以本机进程间传递耗时仍计入现有软件延迟；它增加了内存复制成本，需要在真实目标画质下评估。SDK 仍使用单帧队列；连接断开或停止时关闭生成器和设备。活跃采集期间查询复用能力缓存，不重复打开正在使用的 USB 设备。

2026-09-29 在 MacBook Air 上重新确认 Gemini 305（`CV2L761000CH`）和 335L 接口的 `UsbExclusiveOwner` 为 `pid 294, UVCAssistant`，普通权限 SDK 2.9.3 查询均复现 `-3`。用户手动启动并授权助手后，普通用户客户端通过助手成功枚举两台设备，`unavailable=[]`，Gemini 305 的 SDK 彩色流成功交付 60 帧：640×480、MJPEG 输入，解码图像为 YUVJ422P，实际交付约 30.05fps，设备时间戳递增。助手取帧至客户端收到图像的 P50/P95 为 4.31/5.34ms（含 MJPEG 解码和本机 IPC，不是物理端到端延迟）。本次未保存图像，短测结束已关闭采集连接。详见 [真机验证报告](docs/results/sdk-20260929/permission-fix.json)。

助手相关软件测试覆盖本机 IPC、双向身份校验、格式/模式校验、MJPEG 行对齐、停止释放、重复占用、启动器 sudo 参数边界，以及原有 SDK→H.264→UDP 回归；前端构建及测试通过。本次真机修复验证的是 SDK 设备访问和取图，不是光学延迟验收。

### 双路同时启动时助手退出（2026-09-29）

原先的单路验证没有覆盖多相机同时初始化。用户双路 1280×720@30 YUYV 启动时，助手进程触发 SIGABRT，发送端因此只看到 EOF。两次 macOS 崩溃报告的触发栈均为 `ob_enable_net_device_enumeration → DeviceManager::enableNetDeviceEnumeration → NetDeviceEnumerator::~NetDeviceEnumerator → std::terminate`。这发生在取到第一帧之前，不是 UDP 目标或视频码率导致的断开。

修正后，同一进程内、同一 SDK 目录的查询和采集共享一个引用计数上下文；助手在运行期一直保留它，最后一个使用者退出后才销毁。网络枚举在临时 XML 的 `Device/EnumerateNetDevice=false` 中预先关闭，不再调用运行期切换 API。只串行化上下文的创建和销毁，多路帧读取继续并行。SDK 原文件不变，RTSP 摄像机入口不受影响。助手启动提示包含 `shared-context-v2`，用于确认已加载修正后的实现；更新后必须重新启动旧助手。

新增并发初始化、先停止一路、重复启动、初始化失败释放和双路 IPC 测试。真机双路结果另行记录，不能由单路 640×480 的结果推断双路稳定性。手动回归命令如下（必须先启动更新后的助手；无网页，无图像保存）：

```bash
.venv/bin/python tools/verify_sdk_capture.py \
  --serial CV2L761000CH --serial CP2636300028 \
  --width 1280 --height 720 --fps 30 --duration 10 \
  --output runs/sdk-dual-check
```

在当前 macOS 普通用户权限下，官方 2.9.3 库已成功加载，并识别到 Gemini 305、Gemini 335L；SDK 打开设备控制接口返回 `uvc_open … Return Code: -3`。窗口会显示每台设备的具体错误，仍可使用普通 USB 与 RTSP 输入。这与“应用允许使用摄像头”权限不是同一层，不能通过改分辨率修复。

macOS 上需在用户明确启动并通过系统 sudo 授权的助手中验证 SDK USB 访问；普通网页与发送端不会自行提权。Linux 应按官方 SDK 文档安装 USB/udev 权限规则，Windows 应检查对应 SDK 的驱动要求。[SDK 平台说明](https://github.com/orbbec/OrbbecSDK_v2)

### Issue #124 与本机证据

[OrbbecSDK_v2 #124](https://github.com/orbbec/OrbbecSDK_v2/issues/124) 的报告者分析：macOS 的 `UVCAssistant` 会独占 UVC 设备，系统应用通过 CoreMedia 获取视频，而 SDK 直接打开 USB 时会受阻。项目协作者于 2026-06-22 回复，暂时可用 sudo，免提权改善方案仍需进一步评估；本次查询 issue 仍为 Open，讨论中未提供已发布的免 sudo 修复。[协作者回复](https://github.com/orbbec/OrbbecSDK_v2/issues/124#issuecomment-4764178178)

2026-09-28 只读检查本机 IORegistry，Gemini 305 和 335L 的接口均出现 `UsbExclusiveOwner = pid 294, UVCAssistant`，与 SDK 的 `uvc_open -3` 现象一致。[本机摘要](docs/results/sdk-20260928/usb-ownership.json)。2026-09-29 的助手对照测试进一步确认此权限路径可以打开设备并取得图像。

只回传普通 RGB 图像时，可优先选启动窗口中的系统相机入口，走 AVFoundation；原生深度、SDK 设备控制或 SDK 时间戳等需求才选择 SDK 路径。两条路径的能力不同，不静默替换。管理员授权应覆盖实际 SDK 采集进程的运行期，不能把一次成功测试当成后续所有普通用户进程永久获权。

原先仅完成原生库加载、普通权限设备识别、软件转换与资源释放测试，以及界面/协议集成。现已补充管理员助手下的 Gemini 305 真机取图；其他流、分辨率、长时间运行及真实端到端 100ms 仍需分别验证。

验证结果：完整回归 **89 passed in 38.17s**。其中 SDK 分支的进程测试使用模拟 SDK 图像，经真实 H.264、UDP 和独立接收进程，验证深度标志、时间戳来源和 CSV；它不是硬件实测。布局修正后单独复测 Qt 窗口，确认多路列表可滚动且控件没有压扁。

准备了可审阅的 `tools/verify_sdk_capture.py`：必须显式指定序列号和输出目录，`--serial` 可重复以并行验证多台设备；只选彩色流，默认执行 5 秒无窗口 SDK→UDP 测试，支持指定尺寸、帧率和时长，输出能力列表与传输指标，不保存图像。它不会自行提权；没有彩色流时直接报错，不用 IR/深度代替。

## 时间戳与低延迟处理

SDK 取帧返回时记录本机 `perf_counter_ns`，作为 `sdk_host_dequeue` 时间戳；此后图像转换、编码、UDP、接收解码仍进入软件计时。SDK 内部等待和设备曝光之前的时间不在此计时中。

SDK 设备时间戳、SDK 系统时间戳（均为微秒）额外写入 `camera_opened/camera_sample` 及 `camera_metrics.csv`。不同设备的 SDK 时钟未经校准，不能直接相减，也没有把这些值冒充曝光同步证明。

适配器使用临时 SDK 配置将管线及内部处理队列限制为 1 帧，原厂配置不被修改；后面继续使用单帧 `LatestSlot`。按 100ms 超时轮询，连续 5 秒没有图像则明确报错，退出关闭 pipeline 并按逆序释放原生资源。SDK 断连目前结束该次运行，尚无 RTSP 那样的自动重连。
