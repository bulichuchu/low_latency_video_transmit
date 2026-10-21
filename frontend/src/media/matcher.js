// How old a frame may be when shown in aligned mode. On a long path the receiver
// may have spent up to two round trips on a retransmission (its reassembly
// windows grow the same way), so the limit grows by that much; net_rtt_ms comes
// with every frame. Latest mode shows late frames: the picture they replace is
// older still (10-21: dropping them froze the picture on the internet path).
export function ageLimit(config, meta) {
  const rtt = Number(meta?.net_rtt_ms);
  return (
    config.max_age_ms +
    (Number.isFinite(rtt) && rtt > 0 ? 2 * Math.min(rtt, 150) : 0)
  );
}

export const PLAYOUT_MAX_MS = 100;

// Playout buffer for a jittery path (RTP over the internet, 10-21). After a
// short stall the frames arrive in a burst; shown as they come, the burst
// flashes by in one refresh (each newer frame replaces the older at once) and
// the motion jerks. Here each frame waits until its capture time plus the
// lowest delay of the last 4 s plus a margin: the 90th percentile of the extra
// delay over those 4 s, at most PLAYOUT_MAX_MS. The margin rises at once and
// falls by 20 ms a second; a frame later than that is shown when it comes.
// Only differences of (arrival - capture) count, so the sender's clock needs
// no mapping. On only on a long path (round trip >= 12 ms, off below 8 ms, as
// on the RK sender); the receiver measures it (net_rtt_ms with every frame).
export class Playout {
  constructor(maxMs = PLAYOUT_MAX_MS, windowMs = 4000, quantile = 0.9) {
    this.maxMs = maxMs;
    this.windowMs = windowMs;
    this.quantile = quantile;
    this.fallPerMs = 0.02;
    this.reset();
  }
  reset() {
    this.history = [];
    this.delay = 0;
    this.base = null;
    this.last = null;
    this.on = false;
  }
  // now: page time the frame was decoded; source: its capture time on the
  // sender's clock (meta.capture_ms). Returns when to show it.
  observe(now, source, rttMs) {
    if (Number.isFinite(rttMs)) this.on = rttMs >= (this.on ? 8 : 12);
    if (!Number.isFinite(source) || !Number.isFinite(now)) return now;
    const history = this.history;
    history.push([now, now - source]);
    while (now - history[0][0] > this.windowMs) history.shift();
    const transits = history.map((h) => h[1]).sort((a, b) => a - b);
    this.base = transits[0];
    const target = Math.min(
      this.maxMs,
      transits[Math.floor(this.quantile * (transits.length - 1))] - this.base,
    );
    this.delay =
      target >= this.delay || this.last == null
        ? target
        : Math.max(target, this.delay - this.fallPerMs * (now - this.last));
    this.last = now;
    // Never later than now + delay: no transit in the window is below base.
    return this.on ? source + this.base + this.delay : now;
  }
}

// VideoFrames are native resources: every dropped or consumed frame must close.
export class FrameMatcher {
  constructor(config) {
    this.config = config;
    this.queues = Array.from({ length: config.streams }, () => []);
    this.playouts = Array.from({ length: config.streams }, () => new Playout());
    this.epochs = [];
    // Latest mode only; config.rtp_playout "min" turns it off.
    this.buffered =
      config.sync_mode === "latest" && config.rtp_playout !== "min";
    this.pending = null;
    this.drops = 0;
  }
  clear() {
    for (const q of this.queues) for (const f of q.splice(0)) f.frame.close();
    for (const p of this.playouts) p.reset();
    this.epochs = [];
    this.pending = null;
  }
  // The margin a stream's frames are held for now, or null when not buffering.
  buffer(stream) {
    const p = this.playouts[stream];
    return this.buffered && p?.on ? p.delay : null;
  }
  add(item) {
    const s = item.meta.stream,
      q = this.queues[s];
    if (q.length && q.at(-1).meta.epoch !== item.meta.epoch) {
      for (const f of q.splice(0)) f.frame.close();
      this.pending = null;
    }
    if (this.buffered) {
      if (this.epochs[s] !== item.meta.epoch) {
        this.playouts[s].reset(); // a new sender process: another capture clock
        this.epochs[s] = item.meta.epoch;
      }
      const rtt = item.meta.net_rtt_ms; // null: not known now, keep the state
      item.due = this.playouts[s].observe(
        item.decoded,
        item.meta.capture_ms,
        rtt == null ? NaN : Number(rtt),
      );
    }
    // Held frames (at most PLAYOUT_MAX_MS) need a little more room.
    if (q.length >= (this.buffered ? 8 : 6)) {
      q.shift().frame.close();
      this.drops++;
    }
    q.push(item);
  }
  poll(now) {
    const c = this.config;
    if (c.sync_mode === "latest") {
      // The newest frame that is due (within half a refresh); older ones are
      // superseded, newer ones wait in the playout buffer.
      const margin = 500 / (c.display_fps || 60),
        chosen = [];
      for (const q of this.queues) {
        let i = q.length - 1;
        while (i >= 0 && (q[i].due ?? -Infinity) > now + margin) i--;
        if (i < 0) continue;
        for (const f of q.splice(0, i)) {
          f.frame.close();
          this.drops++;
        }
        chosen.push(q.shift());
      }
      return chosen;
    }
    for (const q of this.queues)
      while (
        q.length &&
        now - (q[0].capture ?? q[0].decoded) > ageLimit(c, q[0].meta)
      ) {
        q.shift().frame.close();
        this.drops++;
      }
    const latest = this.queues
      .filter((q) => q.length)
      .map((q) => q.at(-1).meta.capture_ms);
    if (!latest.length) {
      this.pending = null;
      return [];
    }
    this.pending ??= { target: Math.max(...latest), started: now };
    const target = this.pending.target;
    const chosen = this.queues
      .map((q) =>
        q.reduce(
          (best, f) =>
            !best ||
            Math.abs(f.meta.capture_ms - target) <
              Math.abs(best.meta.capture_ms - target)
              ? f
              : best,
          null,
        ),
      )
      .filter(
        (f) => f && Math.abs(f.meta.capture_ms - target) <= c.sync_tolerance_ms,
      );
    const times = chosen.map((f) => f.meta.capture_ms);
    const complete =
      chosen.length === c.streams &&
      Math.max(...times) - Math.min(...times) <= c.sync_tolerance_ms;
    if (!complete && now - this.pending.started < c.sync_wait_ms) return [];
    this.pending = null;
    for (const q of this.queues) {
      const selected = chosen.find((f) => q.includes(f));
      const cutoff = selected
        ? selected.meta.capture_ms
        : target - c.sync_tolerance_ms;
      while (q.length && q[0].meta.capture_ms <= cutoff) {
        const f = q.shift();
        if (f !== selected) {
          f.frame.close();
          this.drops++;
        }
      }
    }
    return chosen;
  }
}

export function avcCodec(bytes) {
  for (let i = 0; i < bytes.length - 6; i++) {
    let p = -1;
    if (bytes[i] === 0 && bytes[i + 1] === 0 && bytes[i + 2] === 1) p = i + 3;
    if (
      bytes[i] === 0 &&
      bytes[i + 1] === 0 &&
      bytes[i + 2] === 0 &&
      bytes[i + 3] === 1
    )
      p = i + 4;
    if (p >= 0 && (bytes[p] & 31) === 7)
      return (
        "avc1." +
        [...bytes.slice(p + 1, p + 4)]
          .map((x) => x.toString(16).padStart(2, "0"))
          .join("")
      );
  }
  return null;
}
