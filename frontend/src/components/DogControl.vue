<script setup>
import { computed, onMounted, onUnmounted, reactive, ref, watch } from "vue";
import { dogUrl } from "../api";
import {
  BODY_KEYS,
  COMMAND_LABELS,
  COMMAND_ROWS,
  CONTROL_SOURCES,
  DRIVE_KEYS,
  FAULT_LEVELS,
  HEAD_DIRECTIONS,
  KNEES,
  MODES,
  MOTION_STATES,
  NO_KEYS,
  PAD_KEYS,
  POWER_SUPPLY,
  RISKY,
  SPEED_LEVELS,
  isTyping,
  motionFromKeys,
  speedLimits,
} from "../dog";

// Server snapshot of the robot link (video_demo/dog_control.py DogClient.snapshot).
const IDLE = { state: "idle", allowed: [], status: null };
const dog = ref(IDLE),
  form = reactive({ host: "192.168.168.168", port: 8082, src: 3 }),
  link = ref("connecting"), // this page's socket: connecting, open, closed, replaced
  armed = ref(false),
  scale = ref(0.3),
  keys = reactive(new Set()),
  notice = ref({ ok: true, text: "" }),
  pending = ref(null),
  estopButtons = ref(null),
  estopInView = ref(true),
  confirming = ref(""), // a risky command waiting for its second click
  cameraMbps = reactive({ camera_front: 2, camera_back: 2 });
let socket,
  confirmTimer,
  confirmAt = 0,
  retry,
  refresh,
  disposed = false,
  nextId = 1,
  formTouched = false,
  takeover = true; // on load and on request; plain reconnects only resume
const pageId = globalThis.crypto?.randomUUID?.() || String(Math.random());
// A heartbeat round trip this long makes driving lag noticeably (10-21: seconds
// on the internet path while the video filled the dog's uplink); the warning
// stays 3 s after the last slow one, the snapshot only comes about once a second.
const SLOW_LINK_MS = 300;
const slowLink = ref(null);
let slowTimer;
watch(
  () => dog.value.rtt_ms,
  (rtt) => {
    if (!(rtt >= SLOW_LINK_MS)) return;
    slowLink.value = rtt;
    clearTimeout(slowTimer);
    slowTimer = setTimeout(() => (slowLink.value = null), 3000);
  },
);

const connected = computed(() => dog.value.state === "connected");
const connecting = computed(() => dog.value.state === "connecting");
const status = computed(() => dog.value.status || {});
const allowed = computed(() => new Set(dog.value.allowed || []));
const drive = computed(() => (connected.value ? dog.value.drive : null));
// Raw keys, not filtered by the robot's state: the server needs to know a key is
// still held when moving becomes possible again, to wait for a fresh press.
const motion = computed(() =>
  armed.value ? motionFromKeys(keys, scale.value) : { ...NO_KEYS },
);
const moving = computed(() => Object.values(motion.value).some((v) => v));
const ownControl = computed(
  () => status.value.ctrl_source === dog.value.own_ctrl_source,
);
const needControl = computed(
  () =>
    connected.value &&
    (dog.value.control_lost ||
      dog.value.released ||
      (Number.isInteger(status.value.ctrl_source) && !ownControl.value)),
);
const unsupported = computed(() => new Set(dog.value.unsupported || []));
// Firmware 0.3.2 reports no "mode"; the standing state says which one it is.
const mode = computed(
  () =>
    status.value.mode ||
    {
      sport: "general",
      walk: "general",
      in_place: "in_place",
      stair: "stair",
      nav: "nav",
      follow: "follow",
      track: "track",
    }[status.value.motion_status],
);
const lights = computed(() => {
  const light = status.value.fill_light || {};
  return [
    ["front_light", "前灯", light.front],
    ["back_light", "后灯", light.back],
    ["light_auto_work", "自动补光", light.auto_work],
  ].map(([name, text, on]) => ({
    cmd: `fill_light/${name}_${on ? "off" : "on"}`,
    text,
    on: Boolean(on),
  }));
});
const direction = computed(
  () =>
    [HEAD_DIRECTIONS[status.value.head_direction], KNEES[status.value.knee]]
      .filter(Boolean)
      .join(" · ") || "—",
);
const estop = computed(() =>
  status.value.estop?.hardware
    ? "硬急停"
    : status.value.estop?.software
      ? "软急停"
      : "",
);
const stateLabel = computed(
  () =>
    ({
      connected: dog.value.renewing
        ? "重新握手中"
        : dog.value.link_stale
          ? "链路无数据"
          : "已连接",
      connecting: "连接中",
      error: "连接失败",
    })[dog.value.state] || "未连接",
);
const packs = computed(() => {
  const b = status.value.battery || {};
  return [1, 2]
    .filter((i) => (i === 1 ? b.present1 !== false : b.present2))
    .map((i) => ({
      i,
      power: b[`power${i}`],
      supply: b[`power_supply_status${i}`],
      volts: b[`voltage${i}`],
      amps: b[`current${i}`],
      temp: b[`temperature${i}`],
    }));
});
const battery = computed(() => {
  const text = packs.value
    .filter((p) => Number.isFinite(p.power))
    .map((p) => `${Math.round(p.power)}%`);
  const supply = packs.value.map((p) => p.supply);
  const state = supply.includes(1)
    ? " · 充电中"
    : supply.length && supply.every((x) => x === 4)
      ? " · 已充满"
      : "";
  return text.length ? text.join(" / ") + state : "—";
});
const batteryTitle = computed(() =>
  packs.value
    .map(
      (p) =>
        `电池 ${p.i}：${POWER_SUPPLY[p.supply] ?? "—"} · ${p.volts ?? "—"} V · ${p.amps ?? "—"} A · ${p.temp ?? "—"} °C`,
    )
    .join("\n"),
);
// Charging dock: 1004 charging_pile plus the 1023 dock message of firmware 0.3.2.
const dock = computed(() => {
  const info = dog.value.dock,
    pile = status.value.charging_pile;
  if (!info && !pile) return null;
  const connected = info?.dock_connected ?? pile?.connect;
  const parts = [connected ? "已连接" : "未连接"];
  if (info) {
    if ("has_contact" in info)
      parts.push(info.has_contact ? "已对接触点" : "未对接");
    if ("charging_active" in info)
      parts.push(info.charging_active ? "正在充电" : "未充电");
    if ("light_switch_status" in info)
      parts.push(
        `桩灯${info.light_switch_status ? "开" : "关"}` +
          (Number.isFinite(info.fill_light_brightness)
            ? `（亮度 ${info.fill_light_brightness}）`
            : ""),
      );
  }
  const version = info?.dock_version || dog.value.robot?.dock_version;
  return {
    name: [info?.name, version && `v${version}`].filter(Boolean).join(" · "),
    text: parts.join(" · "),
    error: info?.error_code
      ? `故障 ${info.error_code}${info.error_message ? `：${info.error_message}` : ""}`
      : "",
  };
});
// 1028 task status (not in the protocol PDF): seen while recharging on the dock.
const TASK_PHASES = {
  recharge_charging: "回充：充电中",
  recharge_completed: "回充：已完成",
};
const task = computed(() => {
  const info = dog.value.task;
  if (!info || typeof info !== "object") return null;
  const phase = typeof info.phase === "string" ? info.phase : "";
  return {
    text:
      TASK_PHASES[phase] ??
      (phase ? `任务：${phase}` : `任务 ${info.task_type ?? "—"}`),
    error: info.error_code ? `错误码 ${info.error_code}` : "",
  };
});
const limitText = computed(() => {
  const level = status.value.speed?.level;
  const limit = speedLimits(level, scale.value);
  return limit
    ? `${SPEED_LEVELS[level]}档 × ${Math.round(scale.value * 100)}%：前后 ${limit.forward.toFixed(2)} m/s · 平移 ${limit.left.toFixed(2)} m/s · 转向 ${limit.turn.toFixed(2)} rad/s`
    : "等待速度档";
});
const driveHint = computed(() => {
  if (!connected.value) return "";
  if (dog.value.need_release) return "松开所有按键后重新按下，才会继续移动。";
  if (drive.value === "walk")
    return "按住 W/S 前后、A/D 平移、Q/E 转向，松开即停；空格停止，Esc 退出遥控。";
  if (drive.value === "head")
    return "原地模式：W/S 抬头/低头，A/D 或 Q/E 左右探头，R/F 升高/降低，Z/C 左右转身；空格停止，Esc 退出遥控。";
  // The server says which check holds motion back (dog_control.py _drive_and_reason).
  return `不能移动：${dog.value.blocked || "机器狗当前状态不允许"}`;
});
const logName = computed(() => dog.value.log_path?.split(/[\\/]/).pop() || "");
// The server renews a session the robot stopped serving (dog_control.py _renew).
const renewText = computed(() => {
  const r = dog.value.renewing;
  if (!r) return "";
  const why =
    r.reason === "no_status" ? "机器狗不再上报状态" : "收不到机器狗的数据";
  return `${why}，正在自动重新握手（第 ${r.attempts} 次，已 ${Math.round(r.for_s)} 秒${r.error ? `：${r.error}` : ""}）。恢复前不发送移动，恢复后需重新按键。`;
});

function send(message) {
  if (socket?.readyState !== WebSocket.OPEN) return false;
  socket.send(JSON.stringify(message));
  return true;
}
function request(type, extra = {}) {
  const label = COMMAND_LABELS[extra.cmd || type];
  if (!send({ type, id: nextId, ...extra })) {
    notice.value = {
      ok: false,
      text: `${label}：未发出，本页与服务的连接已断开`,
    };
    return;
  }
  pending.value = { id: nextId++, label };
}
const command = (cmd) => request("command", { cmd });
// Risky commands go out on a second mouse click 0.5–3 s after the first: not
// on a double-click, nor on Space / Enter reaching the focused button.
function act(cmd, event) {
  if (RISKY[cmd]) {
    if (event?.detail === 0) return; // keyboard activation
    event?.currentTarget?.blur?.();
    const now = performance.now();
    if (confirming.value === cmd && now - confirmAt < 500) return;
    if (confirming.value !== cmd) {
      confirming.value = cmd;
      confirmAt = now;
      clearTimeout(confirmTimer);
      confirmTimer = setTimeout(() => (confirming.value = ""), 3000);
      notice.value = {
        ok: true,
        text: `${COMMAND_LABELS[cmd]}：${RISKY[cmd]}。3 秒内再点一次执行。`,
      };
      return;
    }
  }
  confirming.value = "";
  command(cmd);
}
function why(cmd) {
  if (unsupported.value.has(cmd)) return "这台机器狗不支持（协议 1.2.0）";
  if (!allowed.value.has(cmd)) return "机器狗当前状态下不能执行";
  return RISKY[cmd] || "";
}
function setBitrate(camera) {
  request("camera_bitrate", { camera, mbps: Number(cameraMbps[camera]) });
}
function connect() {
  notice.value = { ok: true, text: "" };
  request("connect", { host: form.host, port: form.port, src: form.src });
}
function emergencyStop() {
  confirming.value = "";
  disarm();
  send({ type: "stop" });
  request("command", { cmd: "emergency/stop" });
}
function arm() {
  if (!connected.value) return;
  armed.value = true;
  document.activeElement?.blur?.(); // Space must not press the focused button
}
function disarm() {
  armed.value = false;
  keys.clear();
}
function press(event, code) {
  if (event.button !== 0) return;
  event.currentTarget.setPointerCapture?.(event.pointerId);
  keys.add(code);
}
function sendMotion() {
  send({ type: "move", ...motion.value });
}

function onKeyDown(event) {
  if (event.code === "Escape" || event.code === "Space") confirming.value = "";
  if (!armed.value) return;
  if (event.metaKey || event.ctrlKey || event.altKey) {
    keys.clear(); // macOS drops key-ups while Cmd is held
    return;
  }
  if (event.code === "Escape") return disarm();
  if (event.code === "Space") {
    if (!isTyping(event.target)) event.preventDefault();
    keys.clear();
    send({ type: "stop" });
    return;
  }
  if (isTyping(event.target)) return;
  if (!DRIVE_KEYS.has(event.code)) return;
  event.preventDefault();
  // Auto-repeat must not bring back a key that Space, blur or a command cleared.
  if (!event.repeat) keys.add(event.code);
}
const onKeyUp = (event) => keys.delete(event.code);
const onBlur = () => keys.clear();
const onHidden = () => document.hidden && disarm();
const onRoute = () => !location.hash.includes("receiver") && disarm();

function open() {
  socket = new WebSocket(dogUrl());
  link.value = "connecting";
  socket.onopen = () => {
    socket.send(JSON.stringify({ type: "hello", page: pageId, takeover }));
    takeover = false;
    link.value = "open";
    pending.value = null;
  };
  socket.onmessage = (event) => {
    const data = JSON.parse(event.data);
    if (data.type === "dog") {
      dog.value = data;
      if (data.state !== "connected") disarm();
      if (!formTouched && data.state !== "idle")
        Object.assign(form, {
          host: data.host,
          port: data.port,
          src: data.src,
        });
    } else if (data.type === "result") {
      const label = COMMAND_LABELS[data.cmd || data.kind] || "";
      notice.value = { ok: data.ok, text: `${label}：${data.message}` };
      if (pending.value?.id === data.id) pending.value = null;
    } else if (data.type === "replaced") {
      link.value = "replaced";
    }
  };
  socket.onclose = (event) => {
    disarm();
    pending.value = null;
    dog.value = IDLE; // this page can no longer reach the robot, not even to stop it
    if (event.code === 4000) link.value = "replaced";
    if (disposed || link.value === "replaced") return;
    link.value = "closed";
    retry = setTimeout(open, 1000);
  };
}
function takeOver() {
  clearTimeout(retry);
  takeover = true;
  open();
}

// Every change goes out at once; held keys are refreshed every 0.1 s and the
// server stops the robot when the refresh stops.
watch(motion, sendMotion);
// A floating e-stop appears once the panel's own is scrolled away (watching video).
const observer =
  typeof IntersectionObserver === "undefined"
    ? null
    : new IntersectionObserver(([entry]) => {
        estopInView.value = entry.isIntersecting;
      });
watch(estopButtons, (element, previous) => {
  if (previous) observer?.unobserve(previous);
  if (element) observer?.observe(element);
});
onMounted(() => {
  open();
  refresh = setInterval(() => moving.value && sendMotion(), 100);
  window.addEventListener("keydown", onKeyDown);
  window.addEventListener("keyup", onKeyUp);
  window.addEventListener("blur", onBlur);
  window.addEventListener("hashchange", onRoute);
  document.addEventListener("visibilitychange", onHidden);
});
onUnmounted(() => {
  disposed = true;
  clearTimeout(retry);
  clearTimeout(confirmTimer);
  clearTimeout(slowTimer);
  clearInterval(refresh);
  window.removeEventListener("keydown", onKeyDown);
  window.removeEventListener("keyup", onKeyUp);
  window.removeEventListener("blur", onBlur);
  window.removeEventListener("hashchange", onRoute);
  document.removeEventListener("visibilitychange", onHidden);
  observer?.disconnect();
  socket?.close();
});
</script>
<template>
  <section class="panel dog-panel">
    <div class="section-heading">
      <div>
        <h2>机器狗运动控制</h2>
        <span class="muted">D1 Max · UDP 协议直连，与视频接收互不影响</span>
      </div>
      <span
        class="status"
        :class="{ live: connected && !dog.link_stale && !dog.renewing }"
        ><i></i>{{ stateLabel }}</span
      >
    </div>
    <div v-if="link === 'replaced'" class="notice dog-line">
      机器狗由另一个页面控制，本页不能控制或急停。
      <button class="secondary small" @click="takeOver">在此页面控制</button>
    </div>
    <div v-else-if="link === 'closed'" class="error">
      本页与服务的连接已断开，正在重连；此时本页不能控制或急停机器狗，需要时按机身急停。
    </div>
    <div v-if="dog.state === 'error' && dog.error" class="error" role="alert">
      {{ dog.error }}
    </div>
    <div class="fields dog-connect">
      <label
        >机器狗 IP<input
          v-model.trim="form.host"
          :disabled="connected || connecting"
          placeholder="192.168.168.168"
          @input="formTouched = true" /></label
      ><label
        >UDP 端口<input
          v-model.number="form.port"
          :disabled="connected || connecting"
          type="number"
          min="1"
          max="65535"
          @input="formTouched = true" /></label
      ><label
        >协议身份<select
          v-model.number="form.src"
          :disabled="connected || connecting"
          @change="formTouched = true"
        >
          <option :value="3">SDK · 与官方 SDK 相同</option>
          <option :value="4">EXTERNAL · 协议直连</option>
        </select></label
      ><button
        v-if="!connected"
        class="primary"
        :disabled="connecting || link !== 'open'"
        @click="connect"
      >
        {{ connecting ? "连接中…" : "连接机器狗" }}</button
      ><button v-else class="secondary" @click="request('disconnect')">
        断开
      </button>
    </div>
    <template v-if="connected">
      <p class="hint dog-robot">
        {{ dog.robot?.model || "机器狗" }}
        <template v-if="dog.robot?.sn"> · SN {{ dog.robot.sn }}</template
        ><template v-if="dog.robot?.system">
          · 系统 {{ dog.robot.system }}</template
        >
        · {{ dog.host }}:{{ dog.port }} · 心跳往返 {{ dog.rtt_ms ?? "—" }} ms
        <span v-if="logName" :title="dog.log_path">
          · 记录 runs/dog/{{ logName }}</span
        >
        <span v-if="dog.socket_error" class="dog-bad">
          · {{ dog.socket_error }}</span
        >
      </p>
      <div v-if="dog.renewing" class="notice dog-line" role="status">
        {{ renewText }}
      </div>
      <div
        v-else-if="slowLink && !dog.link_stale"
        class="notice dog-line"
        role="status"
      >
        控制链路慢：心跳往返
        {{ slowLink }}
        ms，指令和状态都会晚到。请放慢动作，必要时停下等网络恢复。
      </div>
      <div v-if="dog.estop_pending" class="error dog-line" role="alert">
        急停还没送达：重新握手成功后立即重发；紧急时按机身急停。
      </div>
      <div
        v-else-if="
          dog.last_command?.cmd === 'emergency/stop' &&
          dog.last_command.acked === false &&
          !status.estop?.software
        "
        class="error dog-line"
        role="alert"
      >
        急停没有得到机器狗确认，状态里也没有软急停：确认机器狗状态，必要时按机身急停。
      </div>
      <div class="dog-status">
        <div class="dog-chip" :class="{ warn: needControl }">
          <span>控制权</span
          ><b
            >{{ CONTROL_SOURCES[status.ctrl_source] ?? "—"
            }}{{ ownControl ? " · 本页" : "" }}</b
          >
        </div>
        <div class="dog-chip" :title="`motion_status: ${status.motion_status}`">
          <span>运动状态</span
          ><b>{{
            MOTION_STATES[status.motion_status] || status.motion_status || "—"
          }}</b>
        </div>
        <div class="dog-chip">
          <span>模式</span><b>{{ MODES[mode] || mode || "—" }}</b>
        </div>
        <div class="dog-chip">
          <span>速度档 · 实际</span
          ><b
            >{{ SPEED_LEVELS[status.speed?.level] || "—" }} ·
            {{ Number(status.speed?.x ?? 0).toFixed(2) }} m/s</b
          >
        </div>
        <div class="dog-chip" :class="{ bad: estop }">
          <span>急停</span><b>{{ estop || "未触发" }}</b>
        </div>
        <div class="dog-chip" :title="batteryTitle">
          <span>电量</span><b>{{ battery }}</b>
        </div>
        <div class="dog-chip">
          <span>朝向 · 膝关节</span><b>{{ direction }}</b>
        </div>
      </div>
      <p v-if="dock" class="hint dog-dock">
        充电桩<template v-if="dock.name">（{{ dock.name }}）</template>：{{
          dock.text
        }}<span v-if="dock.error" class="dog-bad"> · {{ dock.error }}</span>
      </p>
      <p v-if="task" class="hint dog-dock">
        {{ task.text
        }}<span v-if="task.error" class="dog-bad"> · {{ task.error }}</span>
      </p>
      <div v-if="needControl" class="notice dog-line">
        {{
          dog.control_lost && !dog.control_free
            ? "控制权已被遥控器 APP 接管，APP 释放后才能取回。"
            : dog.released
              ? "本页已释放控制权。"
              : "本页当前没有控制权。"
        }}
        <button
          class="secondary small"
          :disabled="Boolean(pending)"
          @click="request('take_control')"
        >
          获取控制权
        </button>
      </div>
      <div class="dog-actions">
        <div class="dog-rows">
          <template v-for="[title, buttons] in COMMAND_ROWS" :key="title">
            <span>{{ title }}</span>
            <div>
              <button
                v-for="[cmd, text, rowMode] in buttons"
                :key="cmd"
                class="secondary small"
                :class="{
                  selected: rowMode && mode === rowMode,
                  confirm: confirming === cmd,
                }"
                :title="why(cmd)"
                :disabled="!allowed.has(cmd) || Boolean(pending)"
                @click="act(cmd, $event)"
              >
                {{ confirming === cmd ? `确认${text}？` : text }}
              </button>
            </div>
          </template>
          <span>速度档</span>
          <div>
            <button
              v-for="[cmd, level] in [
                ['speed/low', 'slow'],
                ['speed/medium', 'medium'],
                ['speed/high', 'high'],
              ]"
              :key="cmd"
              class="secondary small"
              :class="{ selected: status.speed?.level === level }"
              :disabled="!allowed.has(cmd) || Boolean(pending)"
              @click="command(cmd)"
            >
              {{ SPEED_LEVELS[level] }}
            </button>
          </div>
          <span>补光灯</span>
          <div>
            <button
              v-for="light in lights"
              :key="light.text"
              class="secondary small"
              :class="{ selected: light.on }"
              :title="light.on ? '已开，点击关闭' : '已关，点击打开'"
              :disabled="!allowed.has(light.cmd) || Boolean(pending)"
              @click="command(light.cmd)"
            >
              {{ light.text }} {{ light.on ? "开" : "关" }}
            </button>
          </div>
        </div>
        <div ref="estopButtons" class="dog-estop">
          <button class="danger" @click="emergencyStop">急停</button
          ><button
            class="secondary small"
            :disabled="
              !estop || !allowed.has('emergency/recover') || Boolean(pending)
            "
            @click="command('emergency/recover')"
          >
            解除急停</button
          ><button
            class="secondary small"
            :disabled="!ownControl || dog.released || Boolean(pending)"
            title="把控制权交还，遥控器 APP 可以接管；用“获取控制权”收回"
            @click="request('release_control')"
          >
            释放控制权
          </button>
        </div>
      </div>
      <p v-if="!dog.still" class="hint dog-hold">
        移动中：停下 1
        秒后才能切换姿态、模式和特殊动作；改速度档会先让机器狗停下。
      </p>
      <div class="dog-drive">
        <div>
          <button
            :class="armed ? 'danger' : 'primary'"
            @click="armed ? disarm() : arm()"
          >
            {{ armed ? "停用键盘遥控（Esc）" : "启用键盘遥控" }}
          </button>
          <label class="dog-scale"
            >速度上限 {{ Math.round(scale * 100) }}%<input
              v-model.number="scale"
              type="range"
              min="0.1"
              max="1"
              step="0.05"
          /></label>
          <p class="hint">{{ limitText }}</p>
          <p
            class="hint"
            :class="{ 'dog-bad': !drive || (armed && dog.need_release) }"
          >
            {{
              armed || !drive
                ? driveHint
                : "启用后按住按键或屏幕按钮移动，松开即停。"
            }}
          </p>
        </div>
        <div class="dog-pads" @contextmenu.prevent>
          <div
            class="dog-pad"
            :class="{ live: armed && drive && !dog.need_release }"
          >
            <button
              v-for="key in PAD_KEYS"
              :key="key.code"
              type="button"
              class="secondary"
              :class="{ held: keys.has(key.code) }"
              :disabled="!armed"
              @pointerdown.prevent="press($event, key.code)"
              @pointerup="keys.delete(key.code)"
              @pointercancel="keys.delete(key.code)"
              @lostpointercapture="keys.delete(key.code)"
            >
              <b>{{ key.label }}</b
              ><small>{{ drive === "head" ? key.head : key.walk }}</small>
            </button>
          </div>
          <div
            class="dog-pad body"
            :class="{ live: armed && drive === 'head' && !dog.need_release }"
            title="原地模式下可用"
          >
            <button
              v-for="key in BODY_KEYS"
              :key="key.code"
              type="button"
              class="secondary"
              :class="{ held: keys.has(key.code) }"
              :disabled="!armed || drive !== 'head'"
              @pointerdown.prevent="press($event, key.code)"
              @pointerup="keys.delete(key.code)"
              @pointercancel="keys.delete(key.code)"
              @lostpointercapture="keys.delete(key.code)"
            >
              <b>{{ key.label }}</b
              ><small>{{ key.head }}</small>
            </button>
          </div>
        </div>
      </div>
      <ul v-if="dog.faults?.length" class="dog-faults">
        <li v-for="(fault, i) in dog.faults" :key="i">
          <b>{{ FAULT_LEVELS[fault.level] ?? fault.level }}</b>
          {{ fault.fault }}
          <span class="muted">{{ fault.module }} {{ fault.submodule }}</span>
        </li>
      </ul>
      <details class="dog-more">
        <summary>机器狗自带相机的码率</summary>
        <div class="fields dog-bitrate">
          <template
            v-for="[camera, text] in [
              ['camera_front', '前摄'],
              ['camera_back', '后摄'],
            ]"
            :key="camera"
          >
            <label
              >{{ text }} / Mbps<input
                v-model.number="cameraMbps[camera]"
                type="number"
                min="0.2"
                max="20"
                step="0.1" /></label
            ><button
              class="secondary small"
              :disabled="Boolean(pending)"
              @click="setBitrate(camera)"
            >
              设置{{ text }}
            </button>
          </template>
        </div>
        <p class="hint">
          改的是机器狗自己的相机服务（robot_camera）推流的码率，不是本项目的 RK
          发送端；它与发送端共用硬件编码器。
        </p>
      </details>
    </template>
    <p
      v-if="pending || notice.text"
      class="hint"
      :class="{ 'dog-bad': !pending && !notice.ok }"
      role="status"
    >
      {{ pending ? `${pending.label}：发送中…` : notice.text }}
    </p>
    <p class="hint">
      使用前确认机器狗周围留有空地，遥控器 APP 已完全退出（APP
      连着时机器狗拒绝本页连接），机身急停按钮随时可用。动作按机器狗上报的状态开放（SDK
      状态跳转图）；网页失焦、切到后台、断开或停止刷新按键时机器狗自动停下。
    </p>
  </section>
  <div v-if="connected && !estopInView" class="dog-float">
    <span v-if="armed" class="dog-armed"><i></i>键盘遥控中</span
    ><button class="danger" @click="emergencyStop">急停</button>
  </div>
</template>
