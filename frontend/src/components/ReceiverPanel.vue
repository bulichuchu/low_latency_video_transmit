<script setup>
import { computed, nextTick, onMounted, onUnmounted, reactive, ref } from "vue";
import { api } from "../api";
import { VideoPlayer } from "../media/player";
import { WebRtcPlayer } from "../media/webrtc";
import { streamTitles } from "../media/stream-names";
const status = ref({ state: "idle" }),
  busy = ref(false),
  error = ref(""),
  playerError = ref("");
const view = ref({ streams: {}, connected: false, skew: null }),
  config = reactive({
    streams: 1,
    bind: "0.0.0.0",
    port: 5004,
    fps: 30,
    clock_mode: "estimated",
    sync_mode: "latest",
    sync_tolerance_ms: 18,
    sync_wait_ms: 8,
    max_age_ms: 100,
    display_fps: 60,
    transport: "rtp",
    webrtc_playout: "min",
  });
const secureContext = window.isSecureContext;
const webcodecsCapable = typeof VideoDecoder !== "undefined" && secureContext;
const webrtcCapable = typeof RTCPeerConnection !== "undefined";
const running = computed(() => status.value.state === "running");
const streamCount = computed(() =>
  running.value ? status.value.config.streams : config.streams,
);
// WebRTC: the sender sends video straight to this page (see media/webrtc.js).
const webrtc = computed(
  () =>
    (running.value ? status.value.config.transport : config.transport) ===
    "webrtc",
);
const capable = computed(() =>
  webrtc.value ? webrtcCapable : webcodecsCapable,
);
const titles = computed(() =>
  streamTitles(
    webrtc.value ? view.value.streamNames : status.value.receiver?.stream_names,
    streamCount.value,
  ),
);
const totalBitrate = computed(() =>
  webrtc.value
    ? Object.values(view.value.streams).reduce((a, s) => a + (s.mbps || 0), 0)
    : (status.value.receiver?.streams || []).reduce((a, s) => a + s.mbps, 0),
);
const canvases = new Map(),
  videos = new Map();
let player,
  timer,
  disposed = false,
  sessionDirectory = "";
const metric = (value, digits = 1) =>
  Number.isFinite(value) ? value.toFixed(digits) : "—";
// WebRTC: the sender adapts bitrate and frame rate to the browser's feedback.
const sendRate = (s) =>
  webrtc.value && Number.isFinite(s?.sendKbps)
    ? ` · ${(s.sendKbps / 1000).toFixed(1)} Mbps · ${s.sendFps} fps`
    : "";
// In chain order; the values add up to the total latency of the same frame.
const rtpSteps = [
  ["camera", "相机采集 → 应用取帧", "曝光、读出、USB 与驱动；需要相机时间戳"],
  ["sender", "发送端处理", "应用取帧 → 编码完成：传帧、格式转换与 H.264 编码"],
  ["network", "网络 → 接收端", "编码完成 → 接收端转发：发包、网络与组帧"],
  ["viewer", "接收端 → 浏览器", "预览连接，经 SSH 访问时包含隧道"],
  ["decode", "浏览器解码", "WebCodecs 解码"],
  [
    "display",
    "等待刷新与绘制",
    "解码完成到下一次刷新时绘制；对齐模式含等待其他路",
  ],
];
// Step 3 here equals RTP steps 3 + 4: encoded -> this page has the frame.
const webrtcSteps = [
  rtpSteps[0],
  rtpSteps[1],
  [
    "delivery",
    "网络 → 浏览器",
    "编码完成 → 浏览器收齐该帧：aiortc 打包加密、网络与浏览器收包",
  ],
  ["decode", "浏览器解码", "WebRTC 解码耗时（processingDuration）"],
  [
    "display",
    "缓冲与等待刷新",
    "收齐 → 该帧首次出现在页面刷新中：抖动缓冲、解码排队与等待刷新",
  ],
];
const latencySteps = computed(() => (webrtc.value ? webrtcSteps : rtpSteps));
const timestampStatus = (value) =>
  ({
    ready: "相机采集时钟已映射 · 延迟为估计值",
    warming_up: "相机时钟映射稳定性检查中",
    clock_sync: "等待发送端、接收端与浏览器校时",
    unsupported: "设备或 SDK 不支持采集时钟映射",
    sdk_error: "SDK 时间戳读取失败，详见发送日志",
    invalid: "时间戳异常，暂不计算采集延迟",
    clock_jump: "主机时钟发生跳变，正在重新校准",
    device_reset: "设备时间戳发生回退，正在重新校准",
    legacy_helper: "SDK 助手版本较旧，请重启助手",
    unavailable: "此视频源未提供可映射的采集时间戳",
  })[value] || "等待相机时间戳";
async function poll() {
  try {
    status.value = await api("/receiver/status");
    if (
      running.value &&
      !busy.value &&
      capable.value &&
      (!player || sessionDirectory !== status.value.directory)
    ) {
      player?.close();
      await nextTick();
      sessionDirectory = status.value.directory;
      for (const key of Object.keys(config))
        if (key in status.value.config) config[key] = status.value.config[key];
      view.value = { streams: {}, connected: false, skew: null };
      await nextTick(); // tiles switch between <canvas> and <video>
      player =
        status.value.config.transport === "webrtc"
          ? new WebRtcPlayer(
              (s) => videos.get(s),
              (s) => (view.value = s),
              (e) => (playerError.value = e),
            )
          : new VideoPlayer(
              (s) => canvases.get(s),
              (s) => (view.value = s),
              (e) => (playerError.value = e),
            );
    } else if (!running.value && player) {
      player.close();
      player = null;
      view.value.connected = false;
    }
  } catch (e) {
    error.value = e.message;
  } finally {
    if (!disposed) timer = setTimeout(poll, 1000);
  }
}
async function start() {
  busy.value = true;
  error.value = "";
  try {
    status.value = await api("/receiver/start", config);
  } catch (e) {
    error.value = e.message;
  } finally {
    busy.value = false;
  }
}
async function stop() {
  busy.value = true;
  error.value = "";
  player?.close();
  player = null;
  view.value.connected = false;
  try {
    status.value = await api("/receiver/stop", {});
  } catch (e) {
    error.value = e.message;
  } finally {
    busy.value = false;
  }
}
function fullscreen() {
  document
    .getElementById("video-grid")
    ?.requestFullscreen?.()
    .catch((e) => (error.value = e.message));
}
onMounted(poll);
onUnmounted(() => {
  disposed = true;
  clearTimeout(timer);
  player?.close();
});
</script>
<template>
  <div class="page-heading">
    <div>
      <span class="eyebrow">RECEIVE & MONITOR</span>
      <h1>接收端</h1>
      <p>多路实时预览，观察帧率、码率与画面同步情况。</p>
    </div>
    <span class="status" :class="{ live: running }"
      ><i></i
      >{{
        running ? "正在接收" : status.state === "error" ? "接收异常" : "待启动"
      }}</span
    >
  </div>
  <div v-if="webrtc && !capable" class="notice">
    此浏览器不提供 WebRTC，无法预览视频；当前页面仍可配置和启停服务端接收。
  </div>
  <div v-else-if="!webrtc && !secureContext" class="notice">
    当前远程 HTTP 地址不支持视频预览。请使用浏览器信任的 HTTPS 地址，或通过 SSH
    转发后用 localhost 访问。仍可在此配置和启停服务端接收。
  </div>
  <div v-else-if="!capable" class="notice">
    此浏览器不提供 WebCodecs，无法预览视频。请换用支持 H.264 WebCodecs
    的浏览器； 当前页面仍可配置和启停服务端接收。
  </div>
  <div v-if="webrtc && running && view.route?.same_host" class="notice">
    本页与发送端在同一台电脑，WebRTC 视频走本机回环，不能与跨机的 RTP
    结果比较。请在接收机上打开本页。
  </div>
  <div v-if="error || status.error" class="error" role="alert">
    {{ error || status.error }}
  </div>
  <div v-if="playerError && running" class="notice" role="status">
    {{ playerError }}
  </div>
  <section class="panel receiver-controls">
    <div class="fields receiver-fields">
      <label>监听地址<input v-model="config.bind" :disabled="running" /></label
      ><label
        >UDP 端口<input
          v-model.number="config.port"
          :disabled="running"
          type="number"
          min="1"
          max="65535" /></label
      ><label
        >视频路数<input
          v-model.number="config.streams"
          :disabled="running"
          type="number"
          min="1"
          max="8" /></label
      ><label
        >传输方式<select v-model="config.transport" :disabled="running">
          <option value="rtp">UDP/RTP · 当前方案</option>
          <option value="webrtc">WebRTC · 发送端直连浏览器</option>
        </select></label
      ><label v-if="webrtc"
        >WebRTC 渲染<select v-model="config.webrtc_playout" :disabled="running">
          <option value="min">最低延迟 · playout-delay 0</option>
          <option value="default">浏览器默认 · 自适应缓冲</option>
        </select></label
      ><label v-else
        >显示策略<select v-model="config.sync_mode" :disabled="running">
          <option value="latest">低延迟 · 最新帧</option>
          <option value="aligned">多路对齐 · 限时等待</option>
        </select></label
      ><button v-if="!running" class="primary" :disabled="busy" @click="start">
        {{ busy ? "启动中…" : "开始接收" }}</button
      ><button v-else class="danger" :disabled="busy" @click="stop">
        {{ busy ? "正在停止…" : "停止接收" }}
      </button>
    </div>
    <details>
      <summary>时钟与缓冲设置</summary>
      <div class="fields four">
        <label
          >发送端帧率 / fps<input
            v-model.number="config.fps"
            :disabled="running"
            type="number"
            min="1"
            max="120" /></label
        ><label
          >时钟映射<select v-model="config.clock_mode" :disabled="running">
            <option value="estimated">两台设备 · 自动估计</option>
            <option value="shared">同一台电脑 · 共享时钟</option>
          </select></label
        ><label
          >应用帧过期阈值 / ms<input
            v-model.number="config.max_age_ms"
            :disabled="running"
            type="number"
            min="10"
            max="1000" /></label
        ><label
          >显示帧率上限<input
            v-model.number="config.display_fps"
            :disabled="running"
            type="number"
            min="1"
            max="120" /></label
        ><label
          >对齐容差 / ms<input
            v-model.number="config.sync_tolerance_ms"
            :disabled="running"
            type="number"
            min="0"
            max="100" /></label
        ><label
          >对齐等待 / ms<input
            v-model.number="config.sync_wait_ms"
            :disabled="running"
            type="number"
            min="0"
            max="50"
        /></label>
      </div>
      <p class="hint">
        “共享时钟”仅限同一台电脑。多机模式建立时钟估计前，应用延迟显示为 —。
        过期阈值从应用取帧起算，不包含相机内部耗时。
      </p>
    </details>
  </section>
  <div class="metrics-row">
    <div class="metric-card">
      <span>接收总码率</span
      ><strong>{{ metric(totalBitrate, 2) }}<small> Mbps</small></strong>
    </div>
    <div class="metric-card">
      <span>可见画面取帧时间偏差</span
      ><strong>{{ metric(view.skew) }}<small> ms</small></strong>
    </div>
    <div class="metric-card">
      <span>预览连接</span
      ><strong class="word">{{ view.connected ? "已连接" : "未连接" }}</strong>
    </div>
  </div>
  <div class="section-heading">
    <div>
      <h2>实时画面</h2>
      <span class="muted"
        >{{ streamCount }} 路 ·
        {{
          webrtc
            ? "WebRTC 直连"
            : (running ? status.config.sync_mode : config.sync_mode) ===
                "aligned"
              ? "多路对齐"
              : "最新帧优先"
        }}</span
      >
    </div>
    <button class="secondary small" @click="fullscreen">全屏预览 ↗</button>
  </div>
  <div
    id="video-grid"
    class="video-grid"
    :class="{ single: streamCount === 1 }"
  >
    <article v-for="n in streamCount" :key="n" class="video-tile">
      <div class="tile-top">
        <span class="tile-name" :title="titles[n - 1]"
          ><i
            class="signal"
            :class="{ active: (view.streams[n - 1]?.fps || 0) > 0 }"
          ></i
          >{{ titles[n - 1] }}</span
        ><span
          :title="
            webrtc ? '发送端当前编码码率与帧率，按浏览器反馈自动调整' : ''
          "
          >{{ view.streams[n - 1]?.depth ? "深度灰度预览" : "H.264"
          }}{{ sendRate(view.streams[n - 1]) }}</span
        >
      </div>
      <div class="video-surface">
        <video
          v-if="webrtc"
          :ref="(el) => (el ? videos.set(n - 1, el) : videos.delete(n - 1))"
          :aria-label="titles[n - 1]"
          muted
          autoplay
          playsinline
        ></video>
        <canvas
          v-else
          :ref="(el) => (el ? canvases.set(n - 1, el) : canvases.delete(n - 1))"
          :aria-label="titles[n - 1]"
        ></canvas>
        <div v-if="!view.streams[n - 1]?.frames" class="video-empty">
          <span class="camera-symbol">▣</span
          ><b>{{ running ? "等待视频信号" : "准备接收图像" }}</b>
          <p>
            {{
              running
                ? "确认发送地址、端口及视频路数一致"
                : "启动接收后，在发送端开始回传"
            }}
          </p>
        </div>
        <span
          v-else-if="!running || (view.streams[n - 1]?.fps || 0) === 0"
          class="stale-badge"
          >{{ running ? "暂无新帧 · 画面已停留" : "已停止 · 最后一帧" }}</span
        >
      </div>
      <div class="tile-metrics">
        <div title="最近提交的一帧从计时起点到浏览器提交画面，等于下方各段之和">
          <span>总延迟</span>
          <b
            >{{ metric(view.streams[n - 1]?.timing?.latency) }}
            <small>ms</small></b
          >
        </div>
        <div>
          <span>显示帧率</span
          ><b
            >{{ metric(running ? view.streams[n - 1]?.fps : 0) }}
            <small>fps</small></b
          >
        </div>
      </div>
      <ol class="latency-steps" aria-label="分段延迟">
        <li
          v-for="[key, label, detail] in latencySteps"
          :key="key"
          :title="detail"
        >
          <span>{{ label }}</span
          ><b
            >{{ metric(view.streams[n - 1]?.timing?.steps?.[key]) }}
            <small>ms</small></b
          >
        </li>
      </ol>
      <p class="timestamp-status" role="status">
        {{
          view.streams[n - 1]?.timing?.origin === "camera"
            ? "从相机采集时间戳起算"
            : view.streams[n - 1]?.timing?.origin === "application"
              ? `从应用取帧起算 · ${timestampStatus(view.streams[n - 1]?.sensorStatus)}`
              : timestampStatus(view.streams[n - 1]?.sensorStatus)
        }}
      </p>
    </article>
  </div>
  <div class="measurement-note">
    <span>i</span>
    <p v-if="webrtc">
      总延迟 = 该帧首次出现在页面刷新中的时刻（requestVideoFrameCallback 回调）−
      相机采集时刻（无相机时间戳时为应用取帧），与 RTP
      方案的终点（刷新回调中画完该帧）同口径，取最近显示的一帧，约每 0.5
      秒更新。发送端时钟经 WebRTC 数据通道直接校时，只有“网络 →
      浏览器”一段跨机器。与 RTP 方案比较时，两者都在接收机本机打开本页。
    </p>
    <p v-else>
      总延迟 = 浏览器提交画面时刻 −
      相机采集时刻（无相机时间戳时为应用取帧），取最近提交的一帧，约每 0.5
      秒更新。跨机器的两段按时钟估计拆分，合计可靠。网页隐藏时暂停预览。
    </p>
  </div>
  <details v-if="status.directory" class="panel">
    <summary>测试记录与丢帧</summary>
    <p class="mono">{{ status.directory }}</p>
    <template v-if="webrtc">
      <p>
        发送端 {{ status.receiver?.sender || "尚未收到数据" }} · 连接
        {{ view.state || "—" }}
        <template v-if="view.route"> · 路径 {{ view.route.remote }}</template>
        · 校时误差 ±{{ metric(view.clockUncertainty, 2) }} ms
      </p>
      <p v-for="(s, i) in Object.values(view.streams)" :key="i">
        S{{ i }} · {{ metric(s.mbps, 2) }} Mbps<template
          v-if="Number.isFinite(s.sendKbps)"
        >
          （发送端编码 {{ metric(s.sendKbps / 1000, 2) }} Mbps ·
          {{ s.sendFps }} fps）</template
        >
        · 解码器
        {{ s.decoder || "—" }} · 丢包 {{ s.packets_lost ?? "—" }} · NACK
        {{ s.nack_count ?? "—" }} · PLI {{ s.pli_count ?? "—" }} · 卡顿
        {{ s.freeze_count ?? "—" }} 次 · 抖动缓冲
        {{ metric(s.jitter_buffer_ms) }} ms · 显示 {{ metric(s.fps) }} fps
        <template v-if="s.superseded">
          · 同一刷新内被新帧覆盖 {{ s.superseded }} 帧</template
        ><template v-if="s.unmapped">
          · 未对上计时 {{ s.unmapped }} 帧</template
        >
      </p>
      <p class="hint">
        数字来自浏览器 getStats()；抖动缓冲为每帧平均。发送端按本页的 RTCP
        反馈（丢包、往返时延、REMB）自动降低码率和帧率，网络恢复后逐步回到发送端设置。发送端未连上浏览器时仍走
        RTP（当前 {{ metric(status.receiver?.sender_rtp_mbps, 2) }} Mbps）。
      </p>
    </template>
    <p v-if="!webrtc">
      网页桥接丢帧 {{ status.receiver?.bridge_drops || 0 }} · 显示匹配丢帧
      {{ view.matcherDrops || 0 }}
    </p>
    <p v-for="(s, i) in webrtc ? [] : status.receiver?.streams || []" :key="i">
      S{{ i }} · RTP {{ metric(s.mbps, 2) }} Mbps · 接收链路丢帧 {{ s.drops }} ·
      浏览器收到 {{ metric(view.streams[i]?.receiveFps) }} fps · 解码
      {{ metric(view.streams[i]?.decodeFps) }} fps · 显示
      {{ metric(view.streams[i]?.fps) }} fps · 浏览器重置
      {{ view.streams[i]?.dropped || 0 }}
    </p>
    <p v-if="!webrtc" class="hint">
      “收到”统计到达本页的压缩视频帧。收到低时检查转发/恢复链路；解码低时检查解码积压；显示低时检查配帧等待和页面刷新。
    </p>
    <a v-if="status.report_ready" href="/api/receiver/report" class="text-link"
      >下载测试报告 ↓</a
    >
    <p v-else class="hint">停止接收后生成汇总 JSON、逐帧 CSV 和指标 CSV。</p>
  </details>
</template>
