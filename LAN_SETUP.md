# 两台机器运行

常见部署：A 机连接摄像头并发送，B 机接收并显示。两台机器各运行一份本程序，各自浏览器打开本机 Vue 页面；视频在两机之间经 UDP 传输。

## 1. 从 GitHub 同步项目并准备环境

两台机器通过同一个 GitHub 远程仓库同步项目：首次在目标机器克隆仓库，后续拉取本机已提交并推送的改动。仓库包含 Vue 构建文件 `frontend/dist/`，正常运行不需要 Node.js。Python 环境和 SDK 路径在各机器分别配置，`.venv`、`node_modules` 和 `.local` 不通过仓库同步。

建议使用本项目已测试的 Python 3.13。第一次安装依赖需要能访问 Python 包源。

macOS / Linux，在克隆后的项目目录中运行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python demo.py web --page receiver --no-browser
```

Windows PowerShell，在解压后的项目目录中运行：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe demo.py web --page receiver --no-browser
```

以后启动只需最后一条命令；按 Ctrl+C 停止本机服务及其收发任务。这里提供的是启动方法，不会自动启动另一台机器。

## 2. 先启动 B 机接收

浏览器打开 <http://127.0.0.1:8765/#/receiver>，填写：

| 设置 | 值 |
|---|---|
| 监听地址 | `0.0.0.0`，监听本机 IPv4 网络接口 |
| UDP 端口 | `5004`，可自行改为其他未占用端口 |
| 视频路数 | 与 A 机实际选择的摄像头路数一致 |
| 显示策略 | 首先用“低延迟 · 最新帧”调通 |
| 时钟映射 | “两台设备 · 自动估计” |
| 发送端帧率 | 与 A 机配置的输出帧率一致 |

点击“开始接收”。`--page receiver` 只是选择默认页面，并不会自动开始视频接收。

## 3. 在 A 机发送

A 机运行 `demo.py web --no-browser`，浏览器打开 <http://127.0.0.1:8765/#/sender>。选择摄像头，设置输出画质。在“接收机 IP”填写 **B 机的局域网 IPv4 地址**（例如 `192.168.1.20`，实际以 B 机地址为准），UDP 端口填写 `5004`，点击“开始发送”。

跨机不要填写 `127.0.0.1`，也不要点“本机联调”。`127.0.0.1` 始终指当前这台机器；“本机联调”会将视频发往本机。

两机可以同接有线交换机或同一路由器，先确认网络互通。若防火墙拦截，为本程序放行局域网 UDP 流量；尤其 B 机需要接收 UDP 5004，双方还需要允许校时、关键帧请求和重传反馈返回。无需关闭整台机器的防火墙。

## 4. 端口和 SDK 的区别

- `8765/TCP` 是网页服务端口，默认只绑定 `127.0.0.1`。默认启动不能从另一台电脑访问；远程域名访问使用下面的显式监听或 SSH 转发方式。
- `5004/UDP` 是跨机视频接收端口。若改了 B 机接收端口，A 机发送目标端口也要一致。
- B 机只接收视频，不需要安装 Orbbec SDK。只有通过 SDK 采集的发送机需要安装其操作系统/CPU 对应的 SDK，再在发送页面登记路径。macOS SDK 包不能当成 Windows 或 Linux SDK 使用。
- 普通 USB 相机用系统采集接口；网络摄像机仍填写设备的 RTSP 地址。原有 SDK 系统权限要求不因迁移改变。
- 浏览器需要支持 H.264 WebCodecs。当前 macOS 内置浏览器已验证；其他机器及浏览器需实际验证，缺少能力时页面会提示。

## 5. 无画面时按顺序检查

1. A/B 页面是否分别显示“正在发送”“正在接收”；若有错误，先看页面错误及对应 `runs` 目录。
2. A 机填写的是 B 机真实 LAN 地址，双方 UDP 端口和路数一致。
3. A 机有发送帧率/码率，而 B 机接收码率为 0：优先检查地址、网络、防火墙。
4. B 机接收码率非 0 但没有画面：查看浏览器解码错误，确认只打开一个活动接收预览页面。
5. 跨机时钟估计尚未建立时，应用延迟显示 `—`；不要改成“共享时钟”来强行显示数值。

页面的应用计时不是“真实场景到屏幕出光”延迟，物理延迟验收仍按 [OPTICAL_MEASUREMENT.md](OPTICAL_MEASUREMENT.md) 进行。

同步前端改动时，应先在本机执行 `cd frontend`、`npm run build`，将源码和最新 `frontend/dist/` 一起提交并推送。接收机拉取后即可使用构建结果；SDK 二进制、虚拟环境和本机私有配置分别管理。

## 6. 从你的电脑访问 Mac mini 的域名

示例域名：`qnbot-macmini.qnbot.net`。2026-09-29 本机查询解析为 `100.67.5.203`，向 8765 发起连接时返回 connection refused；此结果说明当前连接被拒绝，不能单独区分未运行、仅回环监听、主机/网络主动拒绝。无需据此关闭整个防火墙。

随后 Mac mini 的实际查询显示 `Python 84365 qnbot ... TCP 127.0.0.1:8765 (LISTEN)`，已确认该服务仅监听回环接口，这是目前无法直接从另一台电脑访问的原因。调整监听后是否还受防火墙影响，需再测试。

先在 **Mac mini** 上查询实际监听：

```bash
lsof -nP -iTCP:8765 -sTCP:LISTEN
```

- 没有输出：没有查到该端口的监听进程，先查看启动终端。
- `127.0.0.1:8765`：仅本机可访问。放行防火墙不会改变监听地址。
- `*:8765` / `0.0.0.0:8765` / 对应网络接口的 IP：已有网络监听，再检查到该接口的连通性和防火墙规则。

### 方式 A：按域名直接访问

先通过 GitHub 将更新后的源码和 `frontend/dist` 同步到 Mac mini。在 Mac mini 停止旧服务后，于项目目录运行：

```bash
.venv/bin/python demo.py web \
  --bind 0.0.0.0 \
  --allow-host qnbot-macmini.qnbot.net \
  --port 8765 --page receiver --no-browser
```

然后在你的电脑打开 <http://qnbot-macmini.qnbot.net:8765/#/receiver>。这是打开**控制页面**的方式；远程域名的 HTTP 不满足 WebCodecs 安全上下文要求，因此可以配置/启停，但不能视频预览。即使域名属于局域网或映射到 127.0.0.1，也不要假定普通域名 HTTP 被浏览器视为 localhost。

需要直接通过域名看视频时，使用这个域名的有效证书，并确保浏览器信任其颁发者。在 Mac mini 上运行以下命令，替换证书和私钥的实际路径：

```bash
.venv/bin/python demo.py web \
  --bind 0.0.0.0 \
  --allow-host qnbot-macmini.qnbot.net \
  --tls-cert /path/to/fullchain.pem \
  --tls-key /path/to/privkey.pem \
  --port 8765 --page receiver --no-browser
```

此时打开 <https://qnbot-macmini.qnbot.net:8765/#/receiver>，视频连接自动用 WSS。证书必须覆盖该域名、在有效期内且被浏览器信任。程序不会安装证书、修改信任或跳过浏览器证书检查。不能只给原 HTTP 服务的 URL 换成 https，必须启用 TLS。

`--allow-host` 可重复指定其他域名/IP，填写时不要带协议、路径或端口。它防止不在白名单中的 Host 被接受，不是账号登录。只有可信网络客户端应能访问该控制服务。

### 方式 B：暂不配置证书，用 SSH 转发调试

Mac mini 保持原有本机服务：

```bash
.venv/bin/python demo.py web --page receiver --no-browser
```

在**你的电脑**另开终端，使用 Mac mini 上的 `qnbot` 用户（需要该机已允许 SSH 登录）：

```bash
ssh -N -o ExitOnForwardFailure=yes \
  -L 127.0.0.1:18765:127.0.0.1:8765 \
  qnbot@qnbot-macmini.qnbot.net
```

保持 SSH 终端运行，浏览器打开 <http://127.0.0.1:18765/#/receiver>。此时访问的是 Mac mini 的服务，浏览器地址为可信的本机回环地址；原有版本也能使用此方法，不必开放远端 8765。18765 是本机转发端口，避免与你自己电脑的 8765 服务冲突。

远程页面上的摄像头枚举、SDK 路径和收发进程都属于 Mac mini。视频经“发送机 → Mac mini UDP 接收 → 你的浏览器”显示，额外经过一段网络；与在接收机本地显示的延迟不同。

### 仍无法连接时

先确认 `lsof` 的监听地址，再从你的电脑检查：

```bash
nc -vz qnbot-macmini.qnbot.net 8765
```

若主机确实监听了网络接口但仍不可达，再检查 Mac mini 防火墙是否允许 Python 接收入站连接、TCP 8765 是否被网络规则拦截、域名对应的网络/VPN 是否在线。视频源到服务端还需 UDP 5004，这是另一个端口。不要用关闭整个防火墙来替代定位。

浏览器限制依据：[WebCodecs 的 SecureContext 要求](https://www.w3.org/TR/webcodecs/#videodecoder-interface)、[MDN 可信上下文与 localhost 例外](https://developer.mozilla.org/en-US/docs/Web/Security/Defenses/Secure_Contexts)。

## 7. 更新双路低帧率修复

本次改动直接保留在项目源码和 `frontend/dist/` 中，通过 GitHub 同步。先在 Air 提交并推送改动；确认 Mac mini 跟踪对应分支。在 Mac mini 的原 Web 服务终端按 Ctrl+C 停止服务，然后在 Mac mini 终端运行：

```bash
cd /Users/qnbot/Public/fzh/low_latency_video_transmit
git pull --ff-only
.venv/bin/python demo.py web --page receiver --no-browser
```

若拉取因本地改动或分支分叉而失败，先处理相应改动再继续，不要强制覆盖接收机代码。

保留 Air 上的 SSH 转发终端，在 `http://127.0.0.1:18765/#/receiver` 按 Cmd+Shift+R 强制刷新，然后重新开始接收。SDK 助手不需要因这次前端修复而重启。Air 的发送端速率统计修复则在它的 Web 服务下次重启后生效；不改变实际采集和编码速率。

先设置 2 路、最新帧模式、发送端帧率与显示上限均为 60、画面过期阈值 100ms。在“测试记录与丢帧”对照每路的收到 / 解码 / 显示 FPS 和浏览器重置次数。观察至少 30 秒，停止接收生成报告；之后再以相同输入测试多路对齐模式。不要把显示过期阈值调大来掩盖低帧率。

SSH 页面的视频实际经过 Air → Mac mini（UDP）→ Air 浏览器（SSH/TCP）；有条件时再与 Mac mini 本机浏览器作对照，可单独判断最后一跳的影响。这次修复没有替换传输协议，也未在更新后的真实跨机链路上验证最终帧率。
