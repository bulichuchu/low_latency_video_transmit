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

在当前 macOS 普通用户权限下，官方 2.9.3 库已成功加载，并识别到 Gemini 305、Gemini 335L；SDK 打开设备控制接口返回 `uvc_open … Return Code: -3`。窗口会显示每台设备的具体错误，仍可使用普通 USB 与 RTSP 输入。这与“应用允许使用摄像头”权限不是同一层，不能通过改分辨率修复。

macOS 上需在用户明确授权的管理员环境中验证 SDK USB 访问；本程序不会自动提权、索取密码或修改系统驱动。Linux 应按官方 SDK 文档安装 USB/udev 权限规则，Windows 应检查对应 SDK 的驱动要求。[SDK 平台说明](https://github.com/orbbec/OrbbecSDK_v2)

### Issue #124 与本机证据

[OrbbecSDK_v2 #124](https://github.com/orbbec/OrbbecSDK_v2/issues/124) 的报告者分析：macOS 的 `UVCAssistant` 会独占 UVC 设备，系统应用通过 CoreMedia 获取视频，而 SDK 直接打开 USB 时会受阻。项目协作者于 2026-06-22 回复，暂时可用 sudo，免提权改善方案仍需进一步评估；本次查询 issue 仍为 Open，讨论中未提供已发布的免 sudo 修复。[协作者回复](https://github.com/orbbec/OrbbecSDK_v2/issues/124#issuecomment-4764178178)

2026-09-28 只读检查本机 IORegistry，Gemini 305 和 335L 的接口均出现 `UsbExclusiveOwner = pid 294, UVCAssistant`，与 SDK 的 `uvc_open -3` 现象一致。[本机摘要](docs/results/sdk-20260928/usb-ownership.json)。这支持 issue 的解释，但尚未通过本机管理员/普通用户采集对照证明所有失败原因均已排除。

只回传普通 RGB 图像时，可优先选启动窗口中的系统相机入口，走 AVFoundation；原生深度、SDK 设备控制或 SDK 时间戳等需求才选择 SDK 路径。两条路径的能力不同，不静默替换。管理员授权应覆盖实际 SDK 采集进程的运行期，不能把一次成功测试当成后续所有普通用户进程永久获权。

目前完成的是原生库加载、普通权限设备识别、软件转换与资源释放测试，以及界面/协议集成；在取得 USB 访问权限并实际取到帧之前，不宣称 SDK 真机取图或真实端到端 100ms 达标。

验证结果：完整回归 **89 passed in 38.17s**。其中 SDK 分支的进程测试使用模拟 SDK 图像，经真实 H.264、UDP 和独立接收进程，验证深度标志、时间戳来源和 CSV；它不是硬件实测。布局修正后单独复测 Qt 窗口，确认多路列表可滚动且控件没有压扁。

准备了可审阅的 `tools/verify_sdk_capture.py`：必须显式指定序列号和输出目录，只选彩色流，执行 5 秒无窗口 SDK→UDP 测试，输出能力列表与传输指标，不保存图像。它不会自行提权；没有彩色流时直接报错，不用 IR/深度代替。

## 时间戳与低延迟处理

SDK 取帧返回时记录本机 `perf_counter_ns`，作为 `sdk_host_dequeue` 时间戳；此后图像转换、编码、UDP、接收解码仍进入软件计时。SDK 内部等待和设备曝光之前的时间不在此计时中。

SDK 设备时间戳、SDK 系统时间戳（均为微秒）额外写入 `camera_opened/camera_sample` 及 `camera_metrics.csv`。不同设备的 SDK 时钟未经校准，不能直接相减，也没有把这些值冒充曝光同步证明。

适配器使用临时 SDK 配置将管线及内部处理队列限制为 1 帧，原厂配置不被修改；后面继续使用单帧 `LatestSlot`。按 100ms 超时轮询，连续 5 秒没有图像则明确报错，退出关闭 pipeline 并按逆序释放原生资源。SDK 断连目前结束该次运行，尚无 RTSP 那样的自动重连。
