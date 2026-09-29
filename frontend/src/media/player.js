import { FrameMatcher, avcCodec } from "./matcher";
import { videoUrl } from "../api";

export class VideoPlayer {
  constructor(canvasFor, onStats, onError) {
    this.canvasFor = canvasFor;
    this.onStats = onStats;
    this.onError = onError;
    this.decoders = new Map();
    this.clock = [];
    this.telemetry = [];
    this.counters = {};
    this.visible = {};
    this.stopped = false;
    this.connected = false;
    this.clockOffset = null;
    this.clockUncertainty = null;
    this.connect();
    this.timer = setInterval(() => this.report(), 500);
    this.tick = this.tick.bind(this);
    this.raf = requestAnimationFrame(this.tick);
    this.visibility = () => {
      if (document.hidden) this.reset();
      else for (const s of this.decoders.keys()) this.key(s);
    };
    document.addEventListener("visibilitychange", this.visibility);
  }
  send(data) {
    if (this.ws?.readyState === WebSocket.OPEN)
      this.ws.send(JSON.stringify(data));
  }
  connect() {
    if (this.stopped) return;
    const ws = (this.ws = new WebSocket(videoUrl()));
    ws.binaryType = "arraybuffer";
    ws.onopen = () => {
      this.connected = true;
      this.onError("");
      this.send({ type: "ping", t1: performance.now() });
    };
    ws.onmessage = (e) => {
      try {
        typeof e.data === "string"
          ? this.control(JSON.parse(e.data))
          : this.packet(e.data);
      } catch (error) {
        this.onError(error.message);
        this.reset();
      }
    };
    ws.onclose = () => {
      this.connected = false;
      this.reset();
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
      this.reset();
      this.config = data;
      this.matcher = new FrameMatcher(data);
    } else if (data.type === "pong") {
      const now = performance.now(),
        rtt = now - data.t1 - (data.t3 - data.t2);
      if (rtt >= 0 && rtt < 200)
        this.clock.push({
          now,
          rtt,
          offset: (data.t2 - data.t1 + data.t3 - now) / 2,
        });
      this.clock = this.clock.filter((s) => now - s.now < 5000).slice(-32);
      if (this.clock.length >= 3) {
        const best = this.clock.reduce((a, b) => (a.rtt < b.rtt ? a : b));
        this.clockOffset = best.offset;
        this.clockUncertainty = best.rtt / 2;
      }
    }
  }
  key(stream) {
    this.send({ type: "key", stream });
  }
  reset() {
    for (const d of this.decoders.values())
      if (d.decoder.state !== "closed") d.decoder.close();
    this.decoders.clear();
    this.matcher?.clear();
    this.clock = [];
    this.clockOffset = null;
    this.clockUncertainty = null;
    this.visible = {};
    this.telemetry = [];
  }
  dropStream(stream) {
    const d = this.decoders.get(stream);
    if (d && d.decoder.state !== "closed") d.decoder.close();
    this.decoders.delete(stream);
    this.key(stream);
  }
  packet(buffer) {
    if (!this.config || document.hidden) return;
    const length = new DataView(buffer).getUint32(0);
    if (length > 4096 || length + 4 >= buffer.byteLength)
      throw new Error("无效的视频数据包");
    const meta = JSON.parse(
      new TextDecoder().decode(new Uint8Array(buffer, 4, length)),
    );
    const bytes = new Uint8Array(buffer, 4 + length),
      s = meta.stream,
      now = performance.now();
    if (s < 0 || s >= this.config.streams) return;
    this.counters[s] ??= {
      frames: 0,
      interval: 0,
      dropped: 0,
      latency: null,
      decode: null,
    };
    let state = this.decoders.get(s);
    if (
      state &&
      (state.epoch !== meta.epoch ||
        state.next !== meta.frame_id ||
        state.decoder.decodeQueueSize > 3 ||
        state.pending.size > 8)
    ) {
      this.counters[s].dropped++;
      this.dropStream(s);
      state = null;
    }
    if (!state) {
      if (!meta.key) {
        this.key(s);
        return;
      }
      const codec = avcCodec(bytes);
      if (!codec)
        throw new Error("关键帧缺少 H.264 SPS，无法初始化浏览器解码器。");
      state = { epoch: meta.epoch, next: meta.frame_id, pending: new Map() };
      const current = state;
      state.decoder = new VideoDecoder({
        output: (frame) => {
          const item = current.pending.get(frame.timestamp);
          current.pending.delete(frame.timestamp);
          if (!item || this.stopped || document.hidden) {
            frame.close();
            return;
          }
          item.decoded = performance.now();
          item.frame = frame;
          this.matcher.add(item);
        },
        error: (error) => {
          this.onError(`浏览器 H.264 解码失败：${error.message}`);
          this.dropStream(s);
        },
      });
      state.decoder.configure({
        codec,
        optimizeForLatency: true,
        hardwareAcceleration: "prefer-hardware",
      });
      this.decoders.set(s, state);
    }
    // Clock mapping is unavailable until both UDP and browser clocks converge.
    let capture =
      meta.host_capture_ms != null && this.clockOffset != null
        ? meta.host_capture_ms - this.clockOffset
        : null;
    if (capture != null && capture > now) capture = null;
    const uncertainty =
      capture != null ? meta.uncertainty_ms + this.clockUncertainty : null;
    state.pending.set(meta.frame_id, {
      meta,
      capture,
      uncertainty,
      received: now,
    });
    state.next = meta.frame_id + 1;
    state.decoder.decode(
      new EncodedVideoChunk({
        type: meta.key ? "key" : "delta",
        timestamp: meta.frame_id,
        data: bytes,
      }),
    );
  }
  tick(now) {
    if (this.stopped) return;
    if (!document.hidden && this.matcher && now >= (this.nextDraw || 0)) {
      const frames = this.matcher.poll(now);
      const batch = [];
      if (frames.length) this.nextDraw = now + 1000 / this.config.display_fps;
      for (const f of frames) {
        const canvas = this.canvasFor(f.meta.stream);
        try {
          if (
            !canvas ||
            now - (f.capture ?? f.decoded) > this.config.max_age_ms
          )
            continue;
          if (
            canvas.width !== f.frame.displayWidth ||
            canvas.height !== f.frame.displayHeight
          ) {
            canvas.width = f.frame.displayWidth;
            canvas.height = f.frame.displayHeight;
          }
          const started = performance.now();
          canvas
            .getContext("2d", { alpha: false, desynchronized: true })
            .drawImage(f.frame, 0, 0);
          const end = performance.now(),
            c = this.counters[f.meta.stream];
          c.frames++;
          c.interval++;
          c.latency = f.capture == null ? null : end - f.capture;
          c.decode = f.decoded - f.received;
          c.depth = f.meta.depth_preview;
          c.lastDraw = end;
          this.visible[f.meta.stream] = {
            capture: f.capture,
            source: f.meta.capture_ms,
            epoch: f.meta.epoch,
          };
          batch.push({
            stream: f.meta.stream,
            epoch: f.meta.epoch,
            frame_id: f.meta.frame_id,
            latency_ms: c.latency,
            browser_decode_ms: c.decode,
            browser_wait_ms: started - f.decoded,
            browser_draw_ms: end - started,
            browser_submit_ms: end,
            clock_uncertainty_ms: f.uncertainty,
          });
        } finally {
          f.frame.close();
        }
      }
      // All canvases are composited after this animation callback returns.
      // Measure the resulting visible set, not intermediate partial updates.
      const visible = Object.values(this.visible);
      const skew =
        visible.length === this.config.streams &&
        new Set(visible.map((x) => x.epoch)).size === 1 &&
        visible.length > 1
          ? Math.max(...visible.map((x) => x.source)) -
            Math.min(...visible.map((x) => x.source))
          : null;
      for (const row of batch)
        this.telemetry.push({ ...row, sync_skew_ms: skew });
      this.telemetry = this.telemetry.slice(-1024);
    }
    this.raf = requestAnimationFrame(this.tick);
  }
  report() {
    const now = performance.now(),
      seconds = (now - (this.lastReport || now - 500)) / 1000;
    this.lastReport = now;
    this.send({ type: "ping", t1: now });
    if (this.telemetry.length)
      this.send({ type: "telemetry", frames: this.telemetry.splice(0) });
    const streams = {};
    for (const [s, c] of Object.entries(this.counters)) {
      streams[s] = {
        ...c,
        fps: c.interval / seconds,
        age:
          this.visible[s]?.capture != null
            ? now - this.visible[s].capture
            : null,
      };
      c.interval = 0;
    }
    const visible = Object.values(this.visible);
    const skew =
      this.config &&
      visible.length === this.config.streams &&
      visible.length > 1 &&
      new Set(visible.map((v) => v.epoch)).size === 1
        ? Math.max(...visible.map((v) => v.source)) -
          Math.min(...visible.map((v) => v.source))
        : null;
    this.onStats({
      streams,
      connected: this.connected,
      skew,
      matcherDrops: this.matcher?.drops || 0,
    });
  }
  close() {
    this.report();
    this.stopped = true;
    clearInterval(this.timer);
    clearTimeout(this.retry);
    cancelAnimationFrame(this.raf);
    document.removeEventListener("visibilitychange", this.visibility);
    this.ws?.close();
    this.reset();
  }
}
