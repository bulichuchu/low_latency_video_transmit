<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from "vue";
import { api } from "../api";
import CameraInput from "./CameraInput.vue";
const props = defineProps({ sdkRoot: String });
const emit = defineEmits(["receiver-started"]);
const devices = ref([]),
  sdkDevices = ref([]),
  sdkAccess = ref(""),
  sdkCacheNote = ref(""),
  unavailable = ref([]),
  selections = reactive({});
const loading = ref(false),
  sdkLoading = ref(false),
  busy = ref(false),
  error = ref(""),
  rtsp = ref(""),
  transport = ref("tcp"),
  sdkPath = ref(props.sdkRoot);
const status = ref({ state: "idle" }),
  output = reactive({
    width: 1280,
    height: 720,
    fps: 30,
    bitrate_kbps: 3000,
    encoder: "auto",
    host: "qnbot-macmini.qnbot.net",
    port: 5004,
  });
const running = computed(() => status.value.state === "running");
const count = computed(() =>
  running.value
    ? status.value.config.streams
    : Object.keys(selections).length +
      rtsp.value.split("\n").filter((x) => x.trim()).length,
);
const bitrate = computed(() =>
  ((output.bitrate_kbps * count.value) / 1000).toFixed(1),
);
let timer,
  disposed = false,
  currentSession = "";
async function poll() {
  try {
    status.value = await api("/sender/status");
    if (running.value && status.value.directory !== currentSession) {
      currentSession = status.value.directory;
      for (const key of Object.keys(output))
        if (key in status.value.config) output[key] = status.value.config[key];
    }
  } catch (e) {
    error.value = e.message;
  } finally {
    if (!disposed) timer = setTimeout(poll, 1000);
  }
}
async function discover(sdk = false) {
  const indicator = sdk ? sdkLoading : loading;
  indicator.value = true;
  error.value = "";
  try {
    const info = await api("/cameras" + (sdk ? "?sdk=1" : ""));
    if (sdk) {
      sdkDevices.value = info.devices;
      sdkAccess.value = info.access || "direct";
      unavailable.value = info.unavailable || [];
      sdkCacheNote.value = info.capabilities_cached
        ? Array.isArray(info.cached_serials)
          ? "正在采集的 SDK 相机使用已有能力信息，其他 SDK 设备已重新查询。"
          : "旧版助手正在返回整份缓存列表。请停止发送后重新查询，或重启更新后的 SDK 助手。"
        : "";
      if (info.status === "not_configured")
        error.value = "请先填写并保存 Orbbec SDK 目录。";
    } else devices.value = info.devices;
    const existing = new Set(
      [...devices.value, ...sdkDevices.value].map((d) => d.device),
    );
    for (const key of Object.keys(selections))
      if (!existing.has(key)) delete selections[key];
  } catch (e) {
    error.value = e.message;
  } finally {
    indicator.value = false;
  }
}
function refreshDevices() {
  discover();
  if (sdkPath.value?.trim()) discover(true);
}
function choose(device, value) {
  if (value) selections[device] = value;
  else delete selections[device];
}
async function saveSdk() {
  try {
    await api("/sdk", { path: sdkPath.value });
    await discover(true);
  } catch (e) {
    error.value = e.message;
  }
}
async function start(loopback = false) {
  error.value = "";
  busy.value = true;
  let ownReceiver = false;
  try {
    const cameras = [
      ...Object.values(selections),
      ...rtsp.value
        .split("\n")
        .map((s) => s.trim())
        .filter(Boolean)
        .map((device) => ({ device, rtsp_transport: transport.value })),
    ];
    if (!cameras.length || cameras.length > 8)
      throw new Error("请选择或填写 1–8 路真实摄像头。");
    if (loopback) {
      await api("/receiver/start", {
        streams: cameras.length,
        width: output.width,
        height: output.height,
        fps: output.fps,
        bind: "127.0.0.1",
        port: output.port,
        clock_mode: "shared",
        sync_mode: "latest",
      });
      ownReceiver = true;
    }
    status.value = await api("/sender/start", {
      ...output,
      host: loopback ? "127.0.0.1" : output.host.trim(),
      cameras,
    });
    if (loopback) emit("receiver-started");
  } catch (e) {
    error.value = e.message;
    if (ownReceiver) await api("/receiver/stop", {}).catch(() => {});
  } finally {
    busy.value = false;
  }
}
async function stop() {
  busy.value = true;
  try {
    status.value = await api("/sender/stop", {});
  } catch (e) {
    error.value = e.message;
  } finally {
    busy.value = false;
  }
}
function preset(event) {
  const [width, height, fps, kbps] = event.target.value.split(",").map(Number);
  Object.assign(output, { width, height, fps, bitrate_kbps: kbps });
}
onMounted(() => {
  poll();
  refreshDevices();
});
onUnmounted(() => {
  disposed = true;
  clearTimeout(timer);
});
</script>
<template>
  <div class="page-heading">
    <div>
      <span class="eyebrow">CAPTURE & TRANSMIT</span>
      <h1>发送端</h1>
      <p>选择真实图像源，设置画质，回传至接收端。</p>
    </div>
    <span class="status" :class="{ live: running }"
      ><i></i
      >{{
        running ? "正在发送" : status.state === "error" ? "发送异常" : "待启动"
      }}</span
    >
  </div>
  <div v-if="error || status.error" class="error" role="alert">
    {{ error || status.error }}
  </div>
  <div class="sender-layout">
    <div>
      <section class="panel">
        <div class="section-heading">
          <div>
            <span class="step">01</span>
            <h2>图像来源</h2>
            <span class="muted">已选 {{ count }} 路</span>
          </div>
          <button
            class="secondary small"
            :disabled="loading || sdkLoading || busy"
            @click="refreshDevices"
          >
            {{ loading || sdkLoading ? "正在查询…" : "刷新设备" }}
          </button>
        </div>
        <div v-if="running" class="notice">
          当前运行输入：{{
            status.config?.camera_settings
              ?.map((c) => c.label || c.device)
              .join("；") || "摄像头信息暂不可用"
          }}。停止后可调整。
        </div>
        <div class="camera-list">
          <CameraInput
            v-for="record in [...devices, ...sdkDevices]"
            :key="record.device"
            :record="record"
            :disabled="running || busy"
            @change="choose"
          />
        </div>
        <p v-if="sdkLoading" class="hint">正在查询 SDK 图像源…</p>
        <p v-if="sdkCacheNote" class="notice">{{ sdkCacheNote }}</p>
        <div
          v-for="item in unavailable"
          :key="item.serial"
          class="notice"
          role="status"
        >
          {{ item.name || item.serial }}：{{ item.error }}
        </div>
        <div v-if="!devices.length && !loading" class="empty-small">
          未发现本地摄像头。可连接 USB 摄像头，或使用下方网络 / SDK 输入。
        </div>
        <p v-if="loading" class="hint">
          正在查询设备支持的画质，仅枚举能力，不采集图像。
        </p>
        <details>
          <summary>网络摄像机 · RTSP / PoE</summary>
          <label
            >流地址，每行一路<textarea
              v-model="rtsp"
              :disabled="running || busy"
              rows="3"
              placeholder="rtsp://192.168.1.64:554/stream"
            ></textarea>
          </label>
          <div class="fields two">
            <label
              >输入传输<select v-model="transport" :disabled="running">
                <option value="tcp">TCP · 稳定优先</option>
                <option value="udp">UDP · 低延迟优先</option>
              </select></label
            >
            <p class="hint">
              摄像机端分辨率 / FPS
              在设备后台设置。地址中的账号信息不会保存到运行日志。
            </p>
          </div>
        </details>
        <details>
          <summary>厂商 SDK · Orbbec</summary>
          <p class="hint">
            macOS 遇到 USB 访问被拒绝时，在连接相机的电脑上运行项目的
            start_sdk_helper.command，完成终端授权并保持运行，再查询 SDK
            摄像头。
          </p>
          <p v-if="sdkAccess === 'local_admin_helper'" class="notice">
            SDK 已通过本机管理员助手连接。停止助手会中断 SDK 采集。
          </p>
          <label
            >本机 SDK 目录<input
              v-model="sdkPath"
              :disabled="running"
              placeholder="包含 lib 和 include 的 Orbbec SDK v2 目录"
          /></label>
          <div class="actions">
            <button
              class="secondary small"
              :disabled="running || sdkLoading"
              @click="saveSdk"
            >
              保存目录并查询</button
            ><button
              class="secondary small"
              :disabled="busy || sdkLoading"
              @click="discover(true)"
            >
              {{ sdkLoading ? "查询中…" : "查询 SDK 摄像头" }}
            </button>
          </div>
          <p class="hint">
            同一台 SDK 相机选择一种图像流；避免同时选择该相机的 UVC 入口。
          </p>
        </details>
      </section>
      <section class="panel">
        <div class="section-heading">
          <div>
            <span class="step">02</span>
            <h2>输出画质</h2>
          </div>
          <select
            aria-label="画质预设"
            class="preset"
            :disabled="running"
            @change="preset"
          >
            <option value="1280,720,30,3000">均衡 · 720p / 30</option>
            <option value="640,360,30,1000">轻量 · 360p / 30</option>
            <option value="1280,720,60,5000">流畅 · 720p / 60</option>
            <option value="1920,1080,30,6000">清晰 · 1080p / 30</option>
          </select>
        </div>
        <div class="fields four">
          <label
            >输出宽度<input
              v-model.number="output.width"
              :disabled="running"
              type="number"
              min="64"
              max="3840"
              step="2" /></label
          ><label
            >输出高度<input
              v-model.number="output.height"
              :disabled="running"
              type="number"
              min="64"
              max="2160"
              step="2" /></label
          ><label
            >帧率上限 / fps<input
              v-model.number="output.fps"
              :disabled="running"
              type="number"
              min="1"
              max="120" /></label
          ><label
            >每路码率 / kbps<input
              v-model.number="output.bitrate_kbps"
              :disabled="running"
              type="number"
              min="100"
              step="100"
          /></label>
        </div>
        <div class="fields two">
          <label
            >H.264 编码器<select v-model="output.encoder" :disabled="running">
              <option value="auto">自动选择</option>
              <option value="libx264">软件 · x264</option>
              <option value="h264_videotoolbox">Apple · VideoToolbox</option>
              <option value="h264_nvenc">NVIDIA · NVENC</option>
              <option value="h264_qsv">Intel · QSV</option>
            </select></label
          >
          <p class="hint">
            设置在下次启动时生效。实际采集能力以设备协商结果为准。
          </p>
        </div>
      </section>
    </div>
    <aside>
      <section class="panel destination">
        <div class="section-heading">
          <div>
            <span class="step">03</span>
            <h2>传输目标</h2>
          </div>
        </div>
        <label
          >接收机 IP / 域名<input
            v-model.trim="output.host"
            :disabled="running"
            placeholder="192.168.1.100" /></label
        ><label
          >UDP 端口<input
            v-model.number="output.port"
            :disabled="running"
            type="number"
            min="1"
            max="65535"
        /></label>
        <div class="estimate">
          <span>目标总码率</span
          ><strong>{{ bitrate }} <small>Mbps</small></strong>
          <p>
            {{ count }} 路 · {{ output.width }} × {{ output.height }} ·
            {{ output.fps }} fps
          </p>
        </div>
        <button
          v-if="!running"
          class="primary wide"
          :disabled="busy || !count"
          @click="start(false)"
        >
          {{ busy ? "启动中…" : "开始发送 →" }}</button
        ><button v-else class="danger wide" :disabled="busy" @click="stop">
          {{ busy ? "正在停止…" : "停止发送" }}</button
        ><button
          class="secondary wide"
          :disabled="busy || running || !count"
          @click="start(true)"
        >
          本机联调 · 启动收发两端
        </button>
        <p class="hint">
          两机传输时，先在接收机启动接收，再填写接收机的局域网 IP。
        </p>
      </section>
      <section class="panel">
        <h2>发送状态</h2>
        <div v-if="!status.samples?.length" class="hint">
          启动后显示各路帧率与码率。
        </div>
        <div v-for="s in status.samples" :key="s.stream" class="stream-summary">
          <span>S{{ s.stream }}</span
          ><b>{{ s.fps.toFixed(1) }} <small>fps</small></b
          ><b>{{ s.mbps.toFixed(2) }} <small>Mbps</small></b>
        </div>
        <a
          v-if="status.report_ready"
          href="/api/sender/report"
          class="text-link"
          >下载测试报告 ↓</a
        >
      </section>
    </aside>
  </div>
  <details v-if="status.directory" class="panel">
    <summary>运行记录</summary>
    <p class="mono">{{ status.directory }}</p>
    <pre>{{
      status.events?.map((e) => JSON.stringify(e, null, 2)).join("\n") ||
      "等待设备启动…"
    }}</pre>
  </details>
</template>
