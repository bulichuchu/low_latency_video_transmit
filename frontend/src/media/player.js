import { FrameMatcher, avcCodec } from "./matcher.js";
import { DisplayPacer } from "./pacer.js";
import { videoUrl } from "../api.js";

export class VideoPlayer {
  constructor(canvasFor, onStats, onError) {
    this.canvasFor = canvasFor;
    this.onStats = onStats;
    this.onError = onError;
    this.decoders = new Map();
    this.clock = [];
    this.previewRttSample = null;
    this.telemetry = [];
    this.counters = {};
    this.visible = {};
    this.keyRequests = new Map();
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
      this.pacer = new DisplayPacer(data.display_fps);
    } else if (data.type === "pong") {
      const now = performance.now(),
        rtt = now - data.t1 - (data.t3 - data.t2);
      if (
        [data.t1, data.t2, data.t3].every(Number.isFinite) &&
        now >= data.t1 &&
        data.t3 >= data.t2 &&
        rtt >= 0 &&
        rtt <= 2000
      ) {
        // RTT diagnostics keep slow replies; only low-RTT samples fit clocks.
        this.previewRttSample = { now, rtt };
        if (rtt < 200)
          this.clock.push({
            now,
            rtt,
            offset: (data.t2 - data.t1 + data.t3 - now) / 2,
          });
      }
      this.clock = this.clock.filter((s) => now - s.now < 5000).slice(-32);
      if (this.clock.length >= 3) {
        const best = this.clock.reduce((a, b) => (a.rtt < b.rtt ? a : b));
        this.clockOffset = best.offset;
        this.clockUncertainty = best.rtt / 2;
        this.clockSampleAt = best.now;
      } else {
        this.clockOffset = this.clockUncertainty = this.clockSampleAt = null;
      }
    }
  }
  key(stream, reason = "awaiting_key") {
    const now = performance.now();
    if (now - (this.keyRequests.get(stream) ?? -Infinity) < 200) return;
    this.keyRequests.set(stream, now);
    this.send({ type: "key", stream, reason });
  }
  reset() {
    for (const d of this.decoders.values())
      if (d.decoder.state !== "closed") d.decoder.close();
    this.decoders.clear();
    this.matcher?.clear();
    this.clock = [];
    this.previewRttSample = null;
    this.clockOffset = null;
    this.clockUncertainty = null;
    this.clockSampleAt = null;
    this.visible = {};
    this.telemetry = [];
    this.keyRequests.clear();
    if (this.config) this.pacer = new DisplayPacer(this.config.display_fps);
  }
  dropStream(stream, requestKey = true, reason = "decode_error") {
    const d = this.decoders.get(stream);
    if (d && d.decoder.state !== "closed") d.decoder.close();
    this.decoders.delete(stream);
    if (requestKey) this.key(stream, reason);
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
      receivedInterval: 0,
      decodedInterval: 0,
      resets: {},
    };
    this.counters[s].receivedInterval++;
    let state = this.decoders.get(s);
    const resetReason = !state
      ? null
      : state.epoch !== meta.epoch
        ? "epoch_changed"
        : state.next !== meta.frame_id
          ? "frame_gap"
          : state.decoder.decodeQueueSize > 3
            ? "decode_queue_full"
            : state.pending.size > 8
              ? "decode_output_stalled"
              : null;
    if (resetReason) {
      this.counters[s].dropped++;
      const resets = this.counters[s].resets;
      resets[resetReason] = (resets[resetReason] || 0) + 1;
      // This IDR already repairs the reference chain. Requesting another one
      // here would make the bridge discard its following P frames, creating
      // another gap at the next IDR and an endless recovery loop.
      this.dropStream(s, !meta.key, resetReason);
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
          if (
            !item ||
            this.decoders.get(s) !== current ||
            this.stopped ||
            document.hidden
          ) {
            frame.close();
            return;
          }
          item.decoded = performance.now();
          this.counters[s].decodedInterval++;
          item.frame = frame;
          this.matcher.add(item);
        },
        error: (error) => {
          if (this.decoders.get(s) !== current) return;
          this.onError(`浏览器 H.264 解码失败：${error.message}`);
          const c = this.counters[s];
          c.dropped++;
          c.resets.decode_error = (c.resets.decode_error || 0) + 1;
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
    const clockReady =
      this.clockOffset != null &&
      this.clockSampleAt != null &&
      now - this.clockSampleAt < 5000;
    let capture =
      meta.host_capture_ms != null && clockReady
        ? meta.host_capture_ms - this.clockOffset
        : null;
    if (capture != null && capture > now) capture = null;
    const uncertainty =
      capture != null ? meta.uncertainty_ms + this.clockUncertainty : null;
    let sensorStatus = meta.sensor_status || "unavailable";
    let sensorCapture = null,
      sensorUncertainty = null;
    if (sensorStatus === "ready") {
      if (!clockReady || meta.host_sensor_capture_ms == null)
        sensorStatus = "clock_sync";
      else {
        const mapped = meta.host_sensor_capture_ms - this.clockOffset;
        if (
          !Number.isFinite(mapped) ||
          mapped > now ||
          (capture != null && mapped > capture)
        )
          sensorStatus = "invalid";
        else {
          sensorCapture = mapped;
          sensorUncertainty = Number.isFinite(meta.sensor_clock_uncertainty_ms)
            ? meta.sensor_clock_uncertainty_ms + this.clockUncertainty
            : null;
        }
      }
    }
    this.counters[s].sensorStatus = sensorStatus;
    if (sensorStatus !== "ready") this.counters[s].sensorLatency = null;
    state.pending.set(meta.frame_id, {
      meta,
      capture,
      uncertainty,
      sensorCapture,
      sensorUncertainty,
      sensorStatus,
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
    if (!document.hidden && this.matcher && this.pacer.due(now)) {
      const frames = this.matcher.poll(now);
      const batch = [];
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
          c.sensorLatency =
            f.sensorCapture == null ? null : end - f.sensorCapture;
          c.sensorUncertainty = f.sensorUncertainty;
          c.sensorStatus = f.sensorStatus;
          c.decode = f.decoded - f.received;
          c.depth = f.meta.depth_preview;
          c.lastDraw = end;
          this.visible[f.meta.stream] = {
            capture: f.capture,
            sensorCapture: f.sensorCapture,
            sensorStatus: f.sensorStatus,
            uncertainty: f.uncertainty,
            sensorUncertainty: f.sensorUncertainty,
            submitted: end,
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
            sensor_latency_ms: c.sensorLatency,
            sensor_clock_uncertainty_ms: f.sensorUncertainty,
            sensor_measurement_status: f.sensorStatus,
          });
        } finally {
          f.frame.close();
        }
      }
      if (batch.length) this.pacer.submitted(now);
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
      // All displayed durations belong to the same submitted frame. A newly
      // received packet must not change the origin of the still-visible frame.
      const frame = this.visible[s];
      const clockReady =
        this.clockSampleAt != null && now - this.clockSampleAt < 5000;
      const elapsed = (end, start) =>
        Number.isFinite(end) && Number.isFinite(start) && end >= start
          ? end - start
          : null;
      const applicationLatency = clockReady
        ? elapsed(frame?.submitted, frame?.capture)
        : null;
      const sensorLatency =
        clockReady && frame?.sensorStatus === "ready"
          ? elapsed(frame?.submitted, frame?.sensorCapture)
          : null;
      const origin =
        sensorLatency != null
          ? "camera"
          : applicationLatency != null
            ? "application"
            : null;
      const originTime =
        origin === "camera" ? frame.sensorCapture : frame?.capture;
      streams[s] = {
        ...c,
        latency: applicationLatency,
        sensorLatency,
        sensorStatus:
          frame?.sensorStatus === "ready" && !clockReady
            ? "clock_sync"
            : frame?.sensorStatus || c.sensorStatus,
        timing: {
          origin,
          latency: sensorLatency ?? applicationLatency,
          age: origin == null ? null : elapsed(now, originTime),
          cameraToApplication:
            sensorLatency == null || applicationLatency == null
              ? null
              : elapsed(frame.capture, frame.sensorCapture),
          applicationToSubmit: applicationLatency,
          sinceSubmit: elapsed(now, frame?.submitted),
          uncertainty:
            origin === "camera"
              ? frame.sensorUncertainty
              : origin === "application"
                ? frame.uncertainty
                : null,
        },
        fps: c.interval / seconds,
        receiveFps: c.receivedInterval / seconds,
        decodeFps: c.decodedInterval / seconds,
        age:
          this.visible[s]?.capture != null
            ? now - this.visible[s].capture
            : null,
      };
      c.interval = 0;
      c.receivedInterval = 0;
      c.decodedInterval = 0;
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
      previewRtt:
        this.previewRttSample &&
        now >= this.previewRttSample.now &&
        now - this.previewRttSample.now < 5000
          ? this.previewRttSample.rtt
          : null,
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
