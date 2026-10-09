import { videoUrl } from "../api.js";

// WebRTC transport: the sender (aiortc) sends the same H.264 frames straight
// to this page; the receiver service only relays offer/answer over the page's
// WebSocket. Each frame's timing comes over the "meta" data channel, keyed by
// the RTP timestamp that requestVideoFrameCallback reports for it.

export const PLAYOUT_DELAY_URI =
  "http://www.webrtc.org/experiments/rtp-hdrext/playout-delay";
// The x264 stream is High profile; the rest only for a browser without it.
const PROFILE_RANK = {
  "640c1f": 0,
  "64001f": 1,
  "4d001f": 2,
  "42e01f": 3,
  "42001f": 4,
};

export function h264Preferences(codecs) {
  const fmtp = (c) => c.sdpFmtpLine || "";
  const profile = (c) =>
    /profile-level-id=([0-9a-f]{6})/i.exec(fmtp(c))?.[1]?.toLowerCase();
  const h264 = codecs.filter(
    (c) =>
      c.mimeType.toLowerCase() === "video/h264" &&
      /packetization-mode=1/.test(fmtp(c)),
  );
  h264.sort(
    (a, b) => (PROFILE_RANK[profile(a)] ?? 9) - (PROFILE_RANK[profile(b)] ?? 9),
  );
  const rtx = codecs.filter((c) => c.mimeType.toLowerCase() === "video/rtx");
  return h264.length ? [...h264, ...rtx] : [];
}

// "Browser default" leaves Chrome's adaptive jitter buffer in charge: the
// sender then never sends playout-delay 0/0.
export function stripPlayoutDelay(sdp) {
  return sdp
    .split("\r\n")
    .filter(
      (l) => !(l.startsWith("a=extmap:") && l.includes(PLAYOUT_DELAY_URI)),
    )
    .join("\r\n");
}

// Sender clock (ms) minus page clock, from data channel ping/pong; the
// lowest-RTT sample of the last 5 s wins, its RTT/2 bounds the error.
export class ClockFilter {
  constructor() {
    this.samples = [];
    this.offset = this.uncertainty = this.at = null;
  }
  add({ t1, t2, t3 }, now) {
    if (![t1, t2, t3].every(Number.isFinite) || now < t1 || t3 < t2) return;
    const rtt = now - t1 - (t3 - t2);
    if (rtt >= 0 && rtt < 200)
      this.samples.push({ now, rtt, offset: (t2 - t1 + t3 - now) / 2 });
    this.samples = this.samples.filter((s) => now - s.now < 5000).slice(-32);
    if (this.samples.length < 3) {
      this.offset = this.uncertainty = this.at = null;
      return;
    }
    const best = this.samples.reduce((a, b) => (a.rtt < b.rtt ? a : b));
    this.offset = best.offset;
    this.uncertainty = best.rtt / 2;
    // Fresh as of this evaluation. Keying it to the best sample's own age
    // would drop the clock whenever that sample nears 5 s old.
    this.at = now;
  }
  ready(now) {
    return this.offset != null && now - this.at < 5000;
  }
}

// Ordered steps of one shown frame; they add up to its total. Only delivery
// and the totals cross machines (sender clock -> page clock via `offset`);
// with no clock yet (offset null) or no metadata (info null) the remaining
// same-clock parts are still returned.
//
// End point: `shown`, when this frame's requestVideoFrameCallback runs, i.e.
// the page's rendering update that first shows it. That matches the RTP page
// (drawImage done inside requestAnimationFrame): both include the wait for the
// next refresh. Chrome stamps presentationTime when the decoder hands the
// frame to the compositor, before that wait, so it is recorded separately.
export function webrtcTiming(info, md, offset, shown) {
  if (!Number.isFinite(shown)) return null;
  const received =
    Number.isFinite(md.receiveTime) && md.receiveTime > 0
      ? md.receiveTime
      : null;
  const decode = Number.isFinite(md.processingDuration)
    ? md.processingDuration * 1000
    : null;
  const presented = Number.isFinite(md.presentationTime)
    ? md.presentationTime
    : null;
  const sender = Number.isFinite(info?.x) ? info.x : null;
  const sensorReady = info?.ss === "ready" && Number.isFinite(info.sc);
  const clocked = Number.isFinite(offset) && Number.isFinite(info?.c);
  const capture = clocked ? info.c - offset : null;
  const sensor = clocked && sensorReady ? info.sc - offset : null;
  return {
    origin: sensor != null ? "camera" : capture != null ? "application" : null,
    latency: capture == null ? null : shown - (sensor ?? capture),
    applicationLatency: capture == null ? null : shown - capture,
    sensorLatency: sensor == null ? null : shown - sensor,
    shown,
    // Frame complete in the browser -> shown: compares across transports.
    browser: received == null ? null : shown - received,
    steps: {
      camera: sensorReady && Number.isFinite(info.c) ? info.c - info.sc : null,
      sender,
      delivery:
        capture != null && received != null
          ? received - capture - (sender ?? 0)
          : null,
      decode,
      display: received == null ? null : shown - received - (decode ?? 0),
    },
    // Jitter buffer and hand-off only: received -> handed to the compositor.
    buffer:
      received != null && presented != null
        ? presented - received - (decode ?? 0)
        : null,
    expected: Number.isFinite(md.expectedDisplayTime)
      ? md.expectedDisplayTime - shown
      : null,
  };
}

function iceComplete(pc, timeout) {
  if (pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => {
      clearTimeout(timer);
      pc.removeEventListener("icegatheringstatechange", check);
      resolve();
    };
    const check = () => pc.iceGatheringState === "complete" && done();
    const timer = setTimeout(done, timeout);
    pc.addEventListener("icegatheringstatechange", check);
  });
}

export class WebRtcPlayer {
  constructor(videoFor, onStats, onError) {
    this.videoFor = videoFor;
    this.onStats = onStats;
    this.onError = onError;
    this.clock = new ClockFilter();
    this.meta = new Map();
    this.counters = {};
    this.visible = {};
    this.telemetry = [];
    this.stats = {};
    this.fps = {};
    this.sendRate = {}; // stream -> { kbps, fps } the sender encodes at (rate control)
    this.waiting = new Map();
    this.previous = {};
    this.watched = new Map();
    this.generation = 0;
    this.route = null;
    this.streamNames = [];
    this.stopped = false;
    this.connect();
    this.timer = setInterval(() => this.report(), 500);
    this.statsTimer = setInterval(() => this.collectStats(), 1000);
  }
  send(data) {
    if (this.ws?.readyState === WebSocket.OPEN)
      this.ws.send(JSON.stringify(data));
  }
  connect() {
    if (this.stopped) return;
    const ws = (this.ws = new WebSocket(videoUrl()));
    ws.onmessage = (e) => {
      try {
        this.control(JSON.parse(e.data));
      } catch (error) {
        this.onError(error.message);
      }
    };
    ws.onclose = () => {
      this.closePeer();
      if (!this.stopped) {
        this.onError(
          "预览连接断开，正在重连；另一个页面占用预览时请先关闭它。",
        );
        this.retry = setTimeout(() => this.connect(), 1000);
      }
    };
    ws.onerror = () => this.onError("无法连接视频预览，请确认接收端已启动。");
  }
  control(data) {
    if (data.type === "config") {
      this.config = data;
      this.start();
    } else if (data.type === "webrtc_answer") {
      this.pc
        ?.setRemoteDescription({ type: "answer", sdp: data.sdp })
        .catch((e) => this.fail(`无法使用发送端的应答：${e.message}`));
    } else if (data.type === "webrtc_error") {
      this.fail(data.message, data.retry ? 2000 : 5000);
    }
  }
  fail(message, delay = 2000) {
    this.onError(message);
    this.closePeer();
    clearTimeout(this.restartTimer);
    this.restartTimer = setTimeout(() => this.start(), delay);
  }
  async start() {
    clearTimeout(this.restartTimer);
    this.closePeer();
    if (this.stopped || !this.config) return;
    const generation = ++this.generation;
    const preferences = h264Preferences(
      RTCRtpReceiver.getCapabilities?.("video")?.codecs || [],
    );
    if (!preferences.length) {
      this.onError("此浏览器的 WebRTC 不能接收 H.264，请改用 Chrome 或 Edge。");
      return;
    }
    const low = this.config.webrtc_playout !== "default";
    const pc = (this.pc = new RTCPeerConnection({ iceServers: [] }));
    this.transceivers = [];
    this.media = [];
    for (let s = 0; s < this.config.streams; s++) {
      const t = pc.addTransceiver("video", { direction: "recvonly" });
      t.setCodecPreferences?.(preferences);
      if (low && "jitterBufferTarget" in t.receiver)
        t.receiver.jitterBufferTarget = 0;
      this.transceivers.push(t);
      this.media.push(new MediaStream([t.receiver.track]));
    }
    this.attach();
    // Unordered: one lost packet must not hold back every later frame's
    // metadata for an SCTP retransmission (aiortc waits at least 1 s).
    const channel = (this.channel = pc.createDataChannel("meta", {
      ordered: false,
    }));
    channel.onopen = () => {
      for (let i = 0; i < 4; i++) setTimeout(() => this.ping(), i * 60);
    };
    channel.onmessage = (e) => this.message(JSON.parse(e.data));
    pc.onconnectionstatechange = () => {
      if (pc !== this.pc) return;
      const state = pc.connectionState;
      clearTimeout(this.disconnectTimer);
      if (state === "connected") this.onError("");
      else if (state === "failed")
        this.fail(
          "WebRTC 连接失败：检查发送端与本机之间 UDP 是否互通（防火墙、VPN、访客网络隔离）。",
          1500,
        );
      else if (state === "disconnected")
        this.disconnectTimer = setTimeout(
          () =>
            pc === this.pc && this.fail("WebRTC 连接中断，正在重新连接。", 0),
          4000,
        );
    };
    try {
      await pc.setLocalDescription(await pc.createOffer());
      await iceComplete(pc, 2000);
      if (generation !== this.generation) return;
      const sdp = pc.localDescription.sdp;
      this.send({
        type: "webrtc_offer",
        sdp: low ? sdp : stripPlayoutDelay(sdp),
      });
    } catch (e) {
      if (generation === this.generation)
        this.fail(`无法创建 WebRTC 连接：${e.message}`, 5000);
    }
  }
  // Vue may replace a <video>; keep each showing its stream and watched.
  attach() {
    (this.media || []).forEach((media, s) => {
      const video = this.videoFor(s);
      if (!video) return;
      if (video.srcObject !== media) {
        video.srcObject = media;
        video.play?.().catch(() => {});
      }
      if (this.watched.get(s) !== video) {
        this.watched.set(s, video);
        const generation = this.generation;
        const onFrame = (now, md) => {
          if (
            this.stopped ||
            generation !== this.generation ||
            this.watched.get(s) !== video
          )
            return;
          this.presented(s, md);
          video.requestVideoFrameCallback(onFrame);
        };
        video.requestVideoFrameCallback?.(onFrame);
      }
    });
  }
  message(data) {
    if (data.type === "frame") {
      if (Number.isFinite(data.rb) && Number.isFinite(data.rf))
        this.sendRate[data.s] = { kbps: data.rb, fps: data.rf };
      if (!this.meta.has(data.s)) this.meta.set(data.s, new Map());
      const frames = this.meta.get(data.s);
      frames.set(data.p, data);
      if (frames.size > 600) frames.delete(frames.keys().next().value);
      const key = `${data.s}:${data.p}`,
        waiting = this.waiting.get(key);
      if (waiting) {
        this.waiting.delete(key);
        this.record(data.s, data, waiting.md, waiting.shown);
      }
    } else if (data.type === "pong") {
      this.clock.add(data, performance.now());
      if (Array.isArray(data.stream_names))
        this.streamNames = data.stream_names.slice(
          0,
          this.config?.streams ?? 8,
        );
    } else if (data.type === "route") this.route = data;
  }
  ping() {
    if (this.channel?.readyState === "open")
      this.channel.send(
        JSON.stringify({ type: "ping", t1: performance.now() }),
      );
  }
  counter(s) {
    return (this.counters[s] ??= {
      frames: 0,
      interval: 0,
      unmapped: 0,
      superseded: 0,
      lastPresented: null,
    });
  }
  presented(s, md) {
    const shown = performance.now(); // same point as the RTP page's draw end
    const c = this.counter(s);
    // One callback per page refresh that shows a new frame. Frames handed to
    // the compositor within the same refresh are superseded, never visible:
    // the RTP page likewise draws only the newest decoded frame per refresh.
    const count = Number.isFinite(md.presentedFrames)
      ? md.presentedFrames
      : null;
    if (count != null && c.lastPresented != null && count > c.lastPresented + 1)
      c.superseded += count - c.lastPresented - 1;
    c.lastPresented = count;
    c.frames++;
    c.interval++;
    const info = this.meta.get(s)?.get(md.rtpTimestamp);
    if (info) this.record(s, info, md, shown);
    else {
      // The video can overtake its data channel message (a retransmitted
      // packet takes at least 1 s): wait for it, dropping only the oldest.
      this.waiting.set(`${s}:${md.rtpTimestamp}`, { s, md, shown });
      if (this.waiting.size > 512)
        this.expire(-Infinity, this.waiting.keys().next().value);
    }
  }
  expire(now, only) {
    for (const [key, w] of this.waiting)
      if (key === only || now - w.shown > 2000) {
        this.waiting.delete(key);
        this.counter(w.s).unmapped++;
        this.record(w.s, null, w.md, w.shown);
      }
  }
  // Every shown frame gets a row, like the RTP page: values that need the
  // metadata or the clock stay null rather than the frame going missing.
  record(s, info, md, shown) {
    const clocked = info != null && this.clock.ready(shown);
    const timing = webrtcTiming(
      info,
      md,
      clocked ? this.clock.offset : null,
      shown,
    );
    if (!timing) return;
    if (timing.origin != null && !(shown < this.visible[s]?.shown))
      this.visible[s] = { ...timing, info };
    const ready = info?.ss === "ready";
    this.telemetry.push({
      stream: s,
      epoch: info?.e ?? null,
      frame_id: info?.f ?? null,
      latency_ms: timing.applicationLatency,
      sensor_latency_ms: timing.sensorLatency,
      camera_ms: timing.steps.camera,
      sender_ms: timing.steps.sender,
      delivery_ms: timing.steps.delivery,
      browser_decode_ms: timing.steps.decode,
      browser_wait_ms: timing.steps.display,
      browser_buffer_ms: timing.buffer,
      browser_ms: timing.browser,
      browser_expected_display_ms: timing.expected,
      browser_submit_ms: timing.shown,
      clock_uncertainty_ms: clocked ? this.clock.uncertainty : null,
      sensor_clock_uncertainty_ms:
        clocked && ready && Number.isFinite(info.su)
          ? info.su + this.clock.uncertainty
          : null,
      sensor_status: info?.ss || "unavailable",
      sensor_measurement_status: !ready
        ? info?.ss || "unavailable"
        : clocked
          ? "ready"
          : "clock_sync",
      sync_skew_ms: this.skew(),
    });
    this.telemetry = this.telemetry.slice(-1024);
  }
  skew() {
    const shown = Object.values(this.visible).map((v) => v.info);
    return this.config &&
      shown.length === this.config.streams &&
      shown.length > 1 &&
      new Set(shown.map((x) => x.e)).size === 1
      ? Math.max(...shown.map((x) => x.c)) - Math.min(...shown.map((x) => x.c))
      : null;
  }
  async collectStats() {
    const pc = this.pc;
    if (!pc || pc.connectionState !== "connected") return;
    let report;
    try {
      report = await pc.getStats();
    } catch {
      return;
    }
    if (pc !== this.pc) return; // restarted meanwhile: do not seed the new one
    const now = performance.now();
    let rtt = null;
    const rows = [];
    report.forEach((r) => {
      if (r.type === "candidate-pair" && r.nominated && r.state === "succeeded")
        rtt = Number.isFinite(r.currentRoundTripTime)
          ? r.currentRoundTripTime * 1000
          : rtt;
    });
    report.forEach((r) => {
      if (r.type !== "inbound-rtp" || r.kind !== "video") return;
      const s = this.transceivers.findIndex(
        (t) => t.mid === r.mid || t.receiver.track.id === r.trackIdentifier,
      );
      if (s < 0) return;
      const last = this.previous[s];
      this.previous[s] = { ...r, at: now };
      if (!last) return;
      const seconds = (now - last.at) / 1000;
      const delta = (k) => (r[k] ?? 0) - (last[k] ?? 0);
      const per = (a, b) =>
        delta(b) > 0 ? (delta(a) / delta(b)) * 1000 : null;
      const row = {
        stream: s,
        mbps: seconds > 0 ? (delta("bytesReceived") * 8) / seconds / 1e6 : null,
        fps: this.fps[s] ?? null,
        unmapped: this.counter(s).unmapped,
        superseded: this.counter(s).superseded,
        packets_lost: Math.max(0, r.packetsLost ?? 0),
        nack_count: r.nackCount ?? null,
        pli_count: r.pliCount ?? null,
        frames_received: r.framesReceived ?? null,
        frames_decoded: r.framesDecoded ?? null,
        frames_dropped: r.framesDropped ?? null,
        freeze_count: r.freezeCount ?? null,
        freeze_ms: Number.isFinite(r.totalFreezesDuration)
          ? r.totalFreezesDuration * 1000
          : null,
        jitter_buffer_ms: per("jitterBufferDelay", "jitterBufferEmittedCount"),
        decode_ms: per("totalDecodeTime", "framesDecoded"),
        rtt_ms: rtt,
        send_kbps: this.sendRate[s]?.kbps ?? null,
        send_fps: this.sendRate[s]?.fps ?? null,
        width: r.frameWidth ?? null,
        height: r.frameHeight ?? null,
        decoder: r.decoderImplementation ?? null,
      };
      this.stats[s] = row;
      rows.push(row);
    });
    if (rows.length) this.send({ type: "webrtc_stats", streams: rows });
  }
  report() {
    const now = performance.now(),
      seconds = (now - (this.lastReport || now - 500)) / 1000;
    this.lastReport = now;
    this.expire(now);
    this.attach();
    this.ping();
    if (this.telemetry.length)
      this.send({ type: "telemetry", frames: this.telemetry.splice(0) });
    const streams = {};
    const clockReady = this.clock.ready(now);
    for (const [s, c] of Object.entries(this.counters)) {
      const v = clockReady ? this.visible[s] : null;
      const fps = (this.fps[s] = c.interval / seconds);
      c.interval = 0;
      streams[s] = {
        ...(this.stats[s] || {}),
        frames: c.frames,
        fps,
        unmapped: c.unmapped,
        superseded: c.superseded,
        sendKbps: this.sendRate[s]?.kbps ?? null,
        sendFps: this.sendRate[s]?.fps ?? null,
        depth: Boolean(v?.info.d),
        sensorStatus: v?.info.ss || (clockReady ? "unavailable" : "clock_sync"),
        timing: v
          ? { origin: v.origin, latency: v.latency, steps: v.steps }
          : { origin: null, latency: null, steps: null },
      };
    }
    this.onStats({
      streams,
      streamNames: this.streamNames,
      connected: this.pc?.connectionState === "connected",
      state: this.pc?.connectionState || "new",
      route: this.route,
      skew: clockReady ? this.skew() : null,
      clockUncertainty: this.clock.uncertainty,
    });
  }
  closePeer() {
    this.generation++;
    clearTimeout(this.disconnectTimer);
    this.channel = null;
    this.pc?.close();
    this.pc = null;
    this.route = null;
    this.streamNames = [];
    this.meta.clear();
    this.waiting.clear();
    this.sendRate = {};
    this.visible = {};
    this.previous = {};
    this.watched.clear();
    this.clock = new ClockFilter();
  }
  close() {
    this.report();
    this.stopped = true;
    clearInterval(this.timer);
    clearInterval(this.statsTimer);
    clearTimeout(this.retry);
    clearTimeout(this.restartTimer);
    this.send({ type: "webrtc_close" });
    this.closePeer();
    this.ws?.close();
  }
}
