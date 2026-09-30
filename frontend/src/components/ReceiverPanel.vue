<script setup>
import { computed, nextTick, onMounted, onUnmounted, reactive, ref } from "vue";
import { api } from "../api";
import { VideoPlayer } from "../media/player";
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
  });
const secureContext = window.isSecureContext;
const capable = typeof VideoDecoder !== "undefined" && secureContext;
const running = computed(() => status.value.state === "running");
const streamCount = computed(() =>
  running.value ? status.value.config.streams : config.streams,
);
const totalBitrate = computed(() =>
  (status.value.receiver?.streams || []).reduce((a, s) => a + s.mbps, 0),
);
const canvases = new Map();
let player,
  timer,
  disposed = false,
  sessionDirectory = "";
const metric = (value, digits = 1) =>
  Number.isFinite(value) ? value.toFixed(digits) : "—";
const timingOrigin = (origin) =>
  ({
    camera: "相机采集时间戳",
    application: "应用取帧（仅应用之后的链路）",
  })[origin] || "等待有效时间戳与校时";
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
      capable &&
      (!player || sessionDirectory !== status.value.directory)
    ) {
      player?.close();
      await nextTick();
      sessionDirectory = status.value.directory;
      for (const key of Object.keys(config))
        if (key in status.value.config) config[key] = status.value.config[key];
      view.value = { streams: {}, connected: false, skew: null };
      player = new VideoPlayer(
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
    if (status.value.receiver) status.value.receiver.udp_rtt_ms = null;
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
  <div v-if="!secureContext" class="notice">
    当前远程 HTTP 地址不支持视频预览。请使用浏览器信任的 HTTPS 地址，或通过 SSH
    转发后用 localhost 访问。仍可在此配置和启停服务端接收。
  </div>
  <div v-else-if="!capable" class="notice">
    此浏览器不提供 WebCodecs，无法预览视频。请换用支持 H.264 WebCodecs
    的浏览器； 当前页面仍可配置和启停服务端接收。
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
    <div class="metric-card">
      <span>真实场景 → 屏幕</span><strong class="word">待光学测量</strong>
    </div>
  </div>
  <section class="network-latency" aria-label="网络往返时延">
    <div class="metrics-row network-metrics">
      <div
        class="metric-card"
        title="发送机与接收机之间最近一次 UDP 探测的往返耗时，已扣除对端处理时间"
      >
        <span>发送机 ↔ 接收机 · UDP RTT</span>
        <strong
          >{{ metric(running ? status.receiver?.udp_rtt_ms : null)
          }}<small> ms</small></strong
        >
      </div>
      <div
        class="metric-card"
        title="接收服务与当前浏览器之间最近一次预览连接探测的往返耗时；通过 SSH 访问时包含隧道路径"
      >
        <span>接收服务 ↔ 浏览器 · 预览 RTT</span>
        <strong
          >{{ metric(running && view.connected ? view.previewRtt : null)
          }}<small> ms</small></strong
        >
      </div>
    </div>
    <p class="hint">
      网络往返时延（RTT），含系统调度开销，不是单程视频延迟，也不与视频延迟相加。等待回应或超过
      5 秒未更新时显示 —。
    </p>
  </section>
  <div class="section-heading">
    <div>
      <h2>实时画面</h2>
      <span class="muted"
        >{{ streamCount }} 路 ·
        {{
          (running ? status.config.sync_mode : config.sync_mode) === "aligned"
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
        <span
          ><i
            class="signal"
            :class="{ active: (view.streams[n - 1]?.fps || 0) > 0 }"
          ></i
          >STREAM {{ String(n - 1).padStart(2, "0") }}</span
        ><span>{{
          view.streams[n - 1]?.depth ? "深度灰度预览" : "H.264"
        }}</span>
      </div>
      <div class="video-surface">
        <canvas
          :ref="(el) => (el ? canvases.set(n - 1, el) : canvases.delete(n - 1))"
          :aria-label="`视频流 ${n - 1}`"
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
        <div
          title="最近提交帧从下方标注的计时起点，到浏览器提交画面的估计耗时；不含屏幕实际出光"
        >
          <span>视频延迟 · 估计</span>
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
        <div
          title="当前画面从同一计时起点到统计这一刻的时间；等于视频延迟加提交后经过的时间，停帧时继续增长"
        >
          <span>画面年龄</span
          ><b
            >{{ metric(running ? view.streams[n - 1]?.timing?.age : null) }}
            <small>ms</small></b
          >
        </div>
      </div>
      <p class="timestamp-status" role="status">
        计时起点：{{ timingOrigin(view.streams[n - 1]?.timing?.origin) }}
      </p>
      <details class="latency-details">
        <summary>延迟分段与计时说明</summary>
        <dl>
          <div>
            <dt>相机采集 → 应用取帧</dt>
            <dd>
              {{ metric(view.streams[n - 1]?.timing?.cameraToApplication) }} ms
            </dd>
          </div>
          <div>
            <dt>应用取帧 → 画面提交</dt>
            <dd>
              {{ metric(view.streams[n - 1]?.timing?.applicationToSubmit) }} ms
            </dd>
          </div>
          <div>
            <dt>提交后经过</dt>
            <dd>
              {{
                metric(
                  running ? view.streams[n - 1]?.timing?.sinceSubmit : null,
                )
              }}
              ms
            </dd>
          </div>
        </dl>
        <p>{{ timestampStatus(view.streams[n - 1]?.sensorStatus) }}</p>
        <p v-if="view.streams[n - 1]?.timing?.origin">
          主机/网络校时误差估计
          {{ metric(view.streams[n - 1]?.timing?.uncertainty) }} ms；相机 SDK
          时钟映射误差未标定。
        </p>
        <p>
          延迟对应最近提交的一帧，非平均值。画面年龄与延迟使用同一计时起点，每约
          0.5 秒更新。无相机时间戳时，仅统计应用之后的链路。
        </p>
      </details>
    </article>
  </div>
  <div class="measurement-note">
    <span>i</span>
    <p>
      视频延迟 = 画面提交时刻 − 计时起点；画面年龄 = 统计时刻 − 同一起点。
      优先使用相机采集时间戳，不可用时明确标注应用取帧起点。终点为浏览器提交画面，真实场景到屏幕出光的延迟仍需光学验证。网页隐藏时暂停预览。
    </p>
  </div>
  <details v-if="status.directory" class="panel">
    <summary>测试记录与丢帧</summary>
    <p class="mono">{{ status.directory }}</p>
    <p>
      网页桥接丢帧 {{ status.receiver?.bridge_drops || 0 }} · 显示匹配丢帧
      {{ view.matcherDrops || 0 }}
    </p>
    <p v-for="(s, i) in status.receiver?.streams || []" :key="i">
      S{{ i }} · RTP {{ metric(s.mbps, 2) }} Mbps · 接收链路丢帧 {{ s.drops }} ·
      浏览器收到 {{ metric(view.streams[i]?.receiveFps) }} fps · 解码
      {{ metric(view.streams[i]?.decodeFps) }} fps · 显示
      {{ metric(view.streams[i]?.fps) }} fps · 浏览器重置
      {{ view.streams[i]?.dropped || 0 }}
    </p>
    <p class="hint">
      “收到”统计到达本页的压缩视频帧。收到低时检查转发/恢复链路；解码低时检查解码积压；显示低时检查配帧等待和页面刷新。
    </p>
    <a v-if="status.report_ready" href="/api/receiver/report" class="text-link"
      >下载测试报告 ↓</a
    >
    <p v-else class="hint">停止接收后生成汇总 JSON、逐帧 CSV 和指标 CSV。</p>
  </details>
</template>
