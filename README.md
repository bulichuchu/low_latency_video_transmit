# Video / Link：通用真实摄像头低延迟回传

当前默认界面为 **Vue 3 发送端 / 接收端**，默认输入是真实摄像头。支持本地 USB/内置相机、提供 RTSP 流的 PoE/网络摄像机，以及通过可选 Orbbec SDK v2 接入的图像流。多路输入统一经 H.264 / RTP / UDP 回传，并回传限时重传、关键帧请求和校时反馈。普通输入保持通用后端，专用 SDK 使用独立适配器。

## Vue 界面快速启动

macOS 双击 `start_demo.command`（或 `start_sender.command`）；接收机可双击 `start_receiver.command`。Windows 使用对应 `.bat`。已附带前端构建文件，正常使用不需要安装 Node.js。

```bash
cd /Users/fushuai/Work/low_latency_video_demo
.venv/bin/python demo.py web
# 仅启动服务、不自动打开浏览器：
.venv/bin/python demo.py web --no-browser
```

- 发送页面：<http://127.0.0.1:8765/#/sender>。选择真实摄像头，设置输出宽高、帧率、码率及接收机 IP，点击“开始发送”。“本机联调”会先启动本机接收端，再启动发送。
- 接收页面：<http://127.0.0.1:8765/#/receiver>。设置与发送端一致的 UDP 端口、路数，点击“开始接收”。支持最新帧 / 多路对齐、全屏、缓冲阈值、时钟设置和停止后的报告下载。
- 两机各运行一份程序，各自浏览器打开本机地址；跨机视频仍通过 UDP。HTTP 控制服务只监听回环地址。
- 接收页面需要浏览器支持 H.264 WebCodecs。已在本机 Codex 内置浏览器验证。缺少支持时页面会明确提示；不会自动切换为增加缓冲的 JPEG/HLS 预览。
- 关闭网页不停止传输；使用两端各自的停止按钮，或在服务终端按 Ctrl+C 停止本机所有任务。画质修改在停止后重新启动生效。默认不保存相机画面。
- 同一接收会话支持一个视频预览页面，刷新页面会重新请求关键帧。SDK 的系统权限要求保持不变，不自动提权。

详细架构、API、开发步骤和实测数据见 [Vue 界面开发与使用说明](WEB_UI.md)。以下 `demo/send/receive` 命令保留原有 CLI/Qt 路径，便于对照测试。

当前代码的真实摄像头验证见 [GENERIC_CAMERA_VALIDATION.md](GENERIC_CAMERA_VALIDATION.md)。

开发人员从 [代码结构与重要类/函数导读](DEVELOPER_GUIDE.md) 开始；当前方案的边界、替代技术路径和改进实测见 [延迟优化报告](OPTIMIZATION_REPORT.md)。

**需要验收的是“真实场景变化 → 接收屏幕实际出光”的差值。** 窗口里的应用计时仅用于定位编码、传输、解码等内部开销，真实总延迟由外部光学测量取得。窗口明确显示“待光学测量”，不会拿软件耗时填入这个指标。

## 原有 CLI / Qt 入口

执行 `.venv/bin/python demo.py demo` 可进入原 Qt 设置及预览窗口；默认输出目标为 720p30、每路 3000kbps。`start_demo.command` 携带媒体参数时也保留这一路径；无参数时打开 Vue 界面。

1. 勾选本地摄像头。设备模式在后台读取，读取期间不会采集画面。
2. 默认“自动匹配输出目标”；取消勾选后，可为每路分别选择**采集分辨率、像素格式、采集帧率**。选项依据设备能力与采集接口联动；二者均支持连续帧率范围时也可手动输入。不支持的组合会在窗口中提示，修正后再启动。
3. 在“传输画质”里设置所有流共用的**输出分辨率、帧率上限和每路目标码率**。可用分辨率预设或填写自定义宽、高（必须为偶数）。例如 1800kbps = 1.8Mbps/路；实际码率随内容变化。输出设置不会补出相机没有的帧或细节。
4. 网络摄像机在 RTSP 输入框每行填写一个地址，可与本地相机混用。其源画质需要在相机后台配置；“传输画质”调整的是本程序解码后重新编码的输出。
5. 点击“开始传输”应用设置。当前设置在启动时生效，运行中修改需关闭窗口后重新启动。

macOS 已验证结构化模式查询。其他本地后端没有模式列表时，窗口会标注“需启动时验证”，允许手动填写采集参数，不能将这些值当成设备已声明支持的模式。深度设备只作视频预览，不传原始深度数据。

当前 macOS 的 FFmpeg 采集接口只接受每个原生帧率范围的最高帧率。例如相机声明 15–30fps，当前接口只开放 30fps，窗口会据此限制采集选项；仍可把传输帧率设为 15fps，程序只编码发送限额内的最新帧。依据：[FFmpeg AVFoundation 实现](https://ffmpeg.org/doxygen/trunk/avfoundation_8m_source.html)。

```bash
cd /Users/fushuai/Work/low_latency_video_demo
.venv/bin/python demo.py cameras
.venv/bin/python demo.py demo --cameras "摄像头名称" --width 1280 --height 720 --fps 30
```

macOS 可用设备名称或编号，Windows 使用 DirectShow 名称，Linux 使用 `/dev/video0` 等设备路径。`--cameras` 逗号分隔多个设备，流数默认按设备数量确定；同名设备需用可区分的编号/设备路径。没有指定设备时，带窗口的 `demo` 会弹出选择框；无窗口和独立发送要求明确指定设备。

使用选择窗口时，命令行的 `--width/--height/--fps/--bitrate-kbps` 用作控件初值，以最后确认的界面设置为准。显式 `--cameras/--rtsp-url/--camera-profile` 仍走无选择窗口的启动路径；配置文件输出参数仍可用命令行覆盖。

```bash
# 两路真实摄像头，编号以本次 cameras 查询为准
.venv/bin/python demo.py demo --cameras 0,1 --width 1280 --height 720 --fps 30

# 摄像头支持时测试 720p60
.venv/bin/python demo.py demo --cameras "摄像头名称" --width 1280 --height 720 --fps 60

# 纯软件解码对照，固定时长并生成应用链路数据
.venv/bin/python demo.py demo --cameras "摄像头名称" --decoder software --headless --duration 30
```

`Q / Esc / 关闭窗口` 退出并保存指标；`S` 手动保存图像。默认不自动保存摄像头画面，需要时加 `--save-preview`，退出后才生成截图。每次运行打印新的 `runs/日期时间-demo-PID/` 目录，不覆盖旧测试。

### “画面年龄”是什么意思？

窗口已将它改名为 **“距应用取帧”**，并增加悬停说明。公式为：`当前时间 − 当前保留帧的应用取帧时间（经时钟映射）`。例如该帧在 12:00:00.000 被程序取到，当前是 12:00:00.080，则显示约 80ms；停在这一帧时数值会继续增长。它用于观察画面是否变旧，不是对显示器实际出光的测量。

本地相机从帧交给发送程序开始计时；RTSP 输入从发送端完成输入解码开始计时。相机曝光、ISP、此前的缓冲，以及屏幕实际出光均不在这个时间定义内；RTSP 的相机编码、输入网络和输入解码也不在内。跨机尚未校时时显示“未知”。因此不能用它宣称已达到真实场景到屏幕 100ms 的目标，仍需 [光学测量](OPTICAL_MEASUREMENT.md)。

## PoE / 网络摄像机

使用 PoE 交换机/供电器给摄像机供电，并取得相机实际提供的 RTSP 地址。PoE 仅说明供电方式，设备仍需支持 RTSP。详见 [网络摄像机接入说明](NETWORK_CAMERAS.md) 和 [配置示例](docs/examples/network-cameras.json)。

```bash
.venv/bin/python demo.py demo --rtsp-url 'rtsp://192.168.1.100:554/实际视频路径'
# 可多次指定 --rtsp-url；也可以和本地摄像头一起使用
.venv/bin/python demo.py demo --cameras 0 --rtsp-url 'rtsp://192.168.1.100:554/实际视频路径'
```

默认 TCP，可用 `--rtsp-transport udp` 对照；每路有独立超时和自动重连。支持 URL 账号认证及 `device_env` 环境变量配置，生成的日志/报告隐藏 URL 凭据。当前先解码 RTSP 输入，再进入现有 H.264 编码链路，尚未实现压缩码流直通。网络输入的软件时间戳始于发送主机收到并解码出图像之后，**不包含摄像机编码、前段网络与输入解码之前的延迟**。

## 专用 SDK 摄像头

Vue 发送页面展开“厂商 SDK · Orbbec”，填写 SDK 路径后点击“保存目录并查询”；已登记时可直接“查询 SDK 摄像头”。原 Qt 入口仍可通过“选择 Orbbec SDK 目录…”登记。本机已登记用户提供的 2.9.3 安装目录。选择实际列出的彩色、红外或深度预览条目后，可继续调整输入/输出画质；不同物理设备可混用，同一台 SDK 设备一次选择一种图像流。

详细步骤、深度图和 RGB 的区别、平台权限及实测边界见 [SDK_CAMERAS.md](SDK_CAMERAS.md)。当前 macOS 普通用户可识别 Gemini 305/335L，但 SDK 打开 USB 控制接口返回访问拒绝，需要管理员授权后才能继续 SDK 真机取图；程序不会自动提权或把普通 UVC 采集当作 SDK 验证成功。

## 安装与平台范围

本机 `.venv` 已准备好。新机器建议 Python 3.11～3.13：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python demo.py doctor
```

Windows 使用 `py -3` 与 `.venv\Scripts\python.exe`。PyAV wheel 自带媒体库，无需额外 FFmpeg 命令行程序。

- macOS：AVFoundation 采集；模式查询使用系统 Swift / Apple Command Line Tools，仅读设备元数据，不开启采集。
- Windows：DirectShow 采集与设备名称查询。摄像头模式需要按设备能力指定；未在当前环境实机验证。
- Linux：V4L2；安装 `v4l2-ctl` 后 `cameras` 会附带设备模式列表。Qt 窗口需要桌面环境；未在当前环境实机验证。
- 本次真实采集验证平台是 macOS，其他平台提供通用后端入口，不代表所有设备和模式已验证。

## 不同摄像头使用通用配置

可用 `--capture-format nv12` 等指定本机支持的采集格式；Linux 对应 V4L2 input format，例如 `mjpeg`。不支持的格式会由设备后端报错，不能仅因配置写了 60fps 就认为相机实际产生 60fps。

需要各摄像头不同采集参数时，用通用 JSON，不按品牌写代码：

```json
{
  "version": 1,
  "output": {"width": 1280, "height": 720, "fps": 30, "bitrate_kbps": 3000},
  "cameras": [
    {"device": "第一个设备名称", "width": 1280, "height": 720, "fps": 60, "pixel_format": "nv12", "colorspace": "auto"},
    {"device": "第二个设备名称", "width": 1280, "height": 720, "fps": 30, "pixel_format": "nv12", "colorspace": "auto"}
  ]
}
```

```bash
.venv/bin/python demo.py demo --camera-profile cameras.json
```

默认 `--capture-mode auto`：优先接近输出尺寸，再选择接近目标 FPS 的设备模式，优先 NV12 等原生 YUV 格式。例如某设备只提供 1280×800 的 15/25fps 时，输出目标 720p30 会选择采集 1280×800@25fps，再转换到输出尺寸，不会继续向设备请求不支持的 720p30。

采集模式选择只依据设备能力，不包含厂商/型号判断。`--capture-mode exact` 要求采集尺寸和帧率与请求一致，不兼容时在启动前列出该设备支持的模式。配置文件中显式设置的逐相机尺寸、FPS 和像素格式同样不会被自动覆盖。

每次自动 Demo 的 `camera-inputs.json` 保存协商后的各路配置，并把这些参数原样传给发送子进程；共同输出参数不会覆盖它们。`config.json` 记录请求与选择结果。模式声明支持某个 FPS 不等于设备一定达到该 FPS，实际应用取帧率仍以 `camera_metrics.csv` 和窗口为准。

输出 FPS 是目标值，不会凭空补出相机没有采到的帧；配置采集 FPS 更高时通过最新原始帧槽降采样，相同帧率时按设备到帧实时编码，实际交付帧率会单独记录。不同输入尺寸会转换为共同输出尺寸，优先选择相同比例避免拉伸。`colorspace` 可用 `auto / bt709 / bt601`；auto 优先设备元数据，未知时按图像高度推定，不确定时需用色卡确认。

原始深度/红外数值的无损传输不属于本彩色视频工具范围；不要把 H.264 压缩后的显示图当作原始深度数据。

## 局域网两机

先启动接收端，再启动发送端。替换接收端 IPv4 地址，并让输出路数、尺寸和帧率一致：

```bash
# 接收端
.venv/bin/python demo.py receive --bind 0.0.0.0 --port 5004 --streams 1 \
  --width 1280 --height 720 --fps 30 --clock-mode estimated

# 发送端
.venv/bin/python demo.py send --host 192.168.1.20 --port 5004 \
  --cameras "摄像头名称" --width 1280 --height 720 --fps 30
```

允许接收端 UDP 5004 入站及控制反馈返回发送端临时端口。媒体单向，反馈双向；接收端同时接受一个发送进程的多路流。此原型用于可信局域网，没有身份认证、SRTP、NAT 穿透或完整拥塞控制。

`estimated` 的四时间戳校时仅供应用耗时分析；`shared` 只能用于同一台电脑。两者都不能推断曝光之前的等待或屏幕出光时刻，不能取代光学测量。

## 测真实场景到屏幕的延迟

完整操作见 [OPTICAL_MEASUREMENT.md](OPTICAL_MEASUREMENT.md)。需要一段外部高速相机/手机拍摄的原始录像，其中同时能看到：

1. 被传摄像头视野里的实体 LED/指示区域；
2. 接收屏幕中这个指示区域的图像。

原始指示灯每隔约 0.8～1.5 秒切换一次，录像中两处变化的时间差就是该位置的场景到屏幕延迟，包含曝光、相机缓冲、传输、处理和实际显示。

```bash
# 240 必须是真实拍摄速率，不能直接填慢动作文件的播放 FPS
.venv/bin/python demo.py optical /path/to/original-slow-motion.mov \
  --capture-fps 240 --select-rois --output runs/optical-test-01
```

弹窗先框选实体指示灯，再框选屏幕中对应区域。也可通过 `--source-roi x,y,w,h --screen-roi x,y,w,h` 指定像素坐标。工具读取真实录像中的亮度变化，输出 `measurement.json`、`matches.csv`、`luminance.csv`、`events.json`，包括分位数、最大值、匹配率、未匹配事件和采样界限。

软件不能凭空知道物理场景真正发生或屏幕出光的时刻。没有外部录像或光电测量设备时，总延迟保持“未测量”，不填估计的伪实测值。

## 通用延迟优化

已实现：原始帧槽只留最新一帧；原生 YUV/NV12 进入编码前的格式转换，去掉采集 → RGB 数组 → YUV 的往返；摄像头时间基准与编码时间基准正确转换；macOS 丢弃晚到采集帧；EAGAIN 约 1ms 重试；无 B 帧/前瞻、短 VBV；有界解码/重组/配帧队列；限时 NACK 和 PLI 恢复；同窗口显示。

还需在真实设备上测：曝光时长、驱动缓存、设备原生格式、USB 带宽、ISP 降噪/HDR、显示刷新率与系统合成。通用后端并不能统一控制所有摄像头的曝光或设备内部缓存，也没有保证 GPU 全链路零拷贝。

```bash
# 编解码路径对照
.venv/bin/python demo.py demo --cameras "摄像头名称" --encoder libx264 --decoder software
.venv/bin/python demo.py demo --cameras "摄像头名称" --encoder h264_videotoolbox

# 丢包与配帧取舍
.venv/bin/python demo.py demo --cameras 0,1 --loss 0.01
.venv/bin/python demo.py demo --cameras 0,1 --sync-wait-ms 20 --sync-tolerance-ms 18
```

每次只改一个条件，用相同光学测量方法比较总延迟、实际帧率、画质、码率及完整配帧率。码率、分辨率和 FPS 应由结果决定，不承诺所有摄像头套同一组参数都达标。

默认多路配帧最多等待半个帧周期，并限制在 20ms 内（30fps 约 16.7ms，60fps 约 8.3ms；单路为 0）。普通相机没有共同曝光触发，固定 8ms 不一定等得到相位不同的另一台相机。可用 `--sync-wait-ms` 覆盖；更短等待通常意味着更多不完整组，软件配帧也不会消除真实曝光相位差。

默认 `--sync-mode aligned` 保留上述配帧逻辑。若更关心每路画面的新鲜程度，可以选择 `latest`，立即提交每路当前最新帧，不等待较慢的摄像头；代价是多路同时可见画面的时间偏差可能增大，不能与 `--strict-sync` 同用。它不同于仅把等待设成 0，后者仍会按共同目标时间筛选帧。

```bash
# 仍弹出真实相机选择框，优先逐路显示最新画面
.venv/bin/python demo.py demo --sync-mode latest
```

当前版本还复用每路格式转换器、减少图像复制、避免空闲刷新阻挡新帧，并在窗口提交前再次检查帧年龄。接收端状态刷新与图像刷新分开。这里的 display-fps 是应用提交频率上限，不会改变显示器物理刷新率。

## 应用指标与测试

自动本机 Demo 的 `report.json` 对比双方发送/解码帧号。各端保存 `events.jsonl`、`frames.csv`、`metrics.csv` 和 `summary.json`；采集端另有 `camera_opened / camera_sample` 事件，记录实际输入尺寸、格式、应用取帧速率和驱动 PTS。

- `decode_latency_ms`：应用取到帧 → 解码 RGB 就绪，包含所有已解码样本，晚帧不从分位数中删除。
- `render_submit_latency_ms`：应用取帧 → Qt 提交更新，**不是屏幕出光**。
- `capture_skew_ms`：完整配帧组内应用时间戳差，**不是曝光同步证明**。
- `visible_groups.capture_skew_ms`：提交时同屏全部画面（包含保留的上一帧）的应用时间戳跨度；latest 模式尤其应同时观察此项。
- `stages_ms`：原始帧排队、格式准备、编解码、组包、解码排队、RGB 转换、配帧、UI 等待和处理的分布。字段含义见开发导读；不可将各阶段 P95 直接相加。
- `decode_over_100ms`、丢弃原因、发送未解码数与缺路组均独立保留；只看成功显示帧的 P95 会有偏差。
- FPS 按完整记录时长计算，窗口是最近 1 秒解码 FPS；启动/停止空隙单独记录。
- RTP 码率含 RTP 头与重传，不含 UDP/IP、链路层和控制反馈开销。

`TEST_REPORT.md` 是此前合成源版本的历史基线，不是当前真实相机的光学验收报告。`docs/results/20260924/` 保留对应旧版证据。合成源仅作为协议自动测试选项保留，正常演示不再默认生成画面。

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
# 仅调试协议时，显式选择测试源
.venv/bin/python demo.py demo --source synthetic --streams 2 --headless --duration 3
```

传输封包参照 [RFC 6184](https://www.rfc-editor.org/rfc/rfc6184)，反馈参照 [RFC 4585](https://www.rfc-editor.org/rfc/rfc4585)。此原型依赖私有帧元数据扩展，双方均需使用同一项目，不是完整通用 RTP/RTCP 会话实现。
