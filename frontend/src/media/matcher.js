// VideoFrames are native resources: every dropped or consumed frame must close.
export class FrameMatcher {
  constructor(config) {
    this.config = config;
    this.queues = Array.from({ length: config.streams }, () => []);
    this.pending = null;
    this.drops = 0;
  }
  clear() {
    for (const q of this.queues) for (const f of q.splice(0)) f.frame.close();
    this.pending = null;
  }
  add(item) {
    const q = this.queues[item.meta.stream];
    if (q.length && q.at(-1).meta.epoch !== item.meta.epoch) {
      for (const f of q.splice(0)) f.frame.close();
      this.pending = null;
    }
    if (q.length >= 6) {
      q.shift().frame.close();
      this.drops++;
    }
    q.push(item);
  }
  poll(now) {
    const c = this.config;
    for (const q of this.queues)
      while (q.length && now - (q[0].capture ?? q[0].decoded) > c.max_age_ms) {
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
    if (c.sync_mode === "latest")
      return this.queues
        .filter((q) => q.length)
        .map((q) => {
          const chosen = q.pop();
          for (const f of q.splice(0)) {
            f.frame.close();
            this.drops++;
          }
          return chosen;
        });
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
