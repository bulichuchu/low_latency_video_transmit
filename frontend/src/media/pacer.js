// Keep a fixed cadence across rAF timestamp rounding/jitter. A delayed callback
// skips missed slots; it never queues catch-up draws or waits a fresh full period.
export class DisplayPacer {
  constructor(fps) {
    this.period = 1000 / fps;
    this.next = null;
  }
  due(now) {
    return this.next == null || now + 0.5 >= this.next;
  }
  submitted(now) {
    if (this.next == null) this.next = now + this.period;
    else
      this.next +=
        Math.max(1, Math.floor((now + 0.5 - this.next) / this.period) + 1) *
        this.period;
  }
}
