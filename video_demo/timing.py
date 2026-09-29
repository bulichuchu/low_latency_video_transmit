from collections import deque
from dataclasses import dataclass
import threading


class ClockMap:
    """NTP-style four-timestamp estimate; no claim of hardware/PTP precision."""
    def __init__(self, shared=False):
        self.shared = shared
        self.samples = deque(maxlen=32)
        self.lock = threading.Lock()

    def update(self, t1, t2, t3, t4):
        if not all(isinstance(t, int) and t > 0 for t in (t1, t2, t3, t4)):
            return False
        rtt = (t4 - t1) - (t3 - t2)
        if t4 < t1 or t3 < t2 or not 0 <= rtt <= 200_000_000:
            return False
        with self.lock:
            self.samples.append((t4, rtt, ((t2 - t1) + (t3 - t4)) // 2))
        return True

    def estimate(self, now):
        if self.shared:
            return 0, 0.0
        with self.lock:
            fresh = [s for s in self.samples if now - s[0] < 5_000_000_000]
        if len(fresh) < 3:
            return None, None
        _, rtt, offset = min(fresh, key=lambda s: s[1])
        return offset, rtt / 2e6


@dataclass
class Decoded:
    meta: object
    image: object
    decoded_ns: int
    local_capture_ns: int | None
    latency_ms: float | None
    uncertainty_ms: float | None
    decoder: str


def frame_expired(frame, now, max_age_ns):
    origin = frame.local_capture_ns if frame.local_capture_ns is not None else frame.decoded_ns
    return now - origin > max_age_ns


class DisplayScheduler:
    """Limit image submissions, without making newly arrived frames wait behind idle ticks."""
    def __init__(self, fps):
        self.interval_ns = int(1e9 / fps)
        self.next_frame_ns = 0
        self.next_status_ns = 0

    def due(self, now, has_frames):
        return (has_frames and now >= self.next_frame_ns) or (not has_frames and now >= self.next_status_ns)

    def submitted(self, now, had_frames):
        if had_frames:
            self.next_frame_ns = now + self.interval_ns
        self.next_status_ns = now + 200_000_000


class FrameMatcher:
    """Bounded nearest-time pairing. Never reuses a frame in a fresh group."""
    def __init__(self, streams, tolerance_ms=18, wait_ms=8, max_age_ms=100, strict=False, mode='aligned'):
        if mode not in ('aligned', 'latest') or (mode == 'latest' and strict):
            raise ValueError('latest display cannot require strict synchronization')
        self.buffers = [deque(maxlen=6) for _ in range(streams)]
        self.lock = threading.Lock()
        self.tolerance = int(tolerance_ms * 1e6)
        self.wait = int(wait_ms * 1e6)
        self.max_age = int(max_age_ms * 1e6)
        self.strict = strict
        self.mode = mode
        self.pending = None
        self.dropped = 0

    def add(self, frame):
        with self.lock:
            q = self.buffers[frame.meta.stream]
            if q and q[-1].meta.epoch != frame.meta.epoch:
                self.dropped += len(q)
                q.clear()
                self.pending = None
            if len(q) == q.maxlen:
                self.dropped += 1
            q.append(frame)

    def poll(self, now):
        with self.lock:
            for q in self.buffers:
                while q and frame_expired(q[0], now, self.max_age):
                    q.popleft()
                    self.dropped += 1
            if self.mode == 'latest':
                # No cross-camera wait; take each available stream's freshest frame.
                chosen = {i: q[-1] for i, q in enumerate(self.buffers) if q}
                if not chosen:
                    return None
                for q in self.buffers:
                    self.dropped += max(0, len(q) - 1)
                    q.clear()
                times = [f.meta.capture_ns for f in chosen.values()]
                skew = (max(times) - min(times)) / 1e6 if len(times) > 1 else None
                complete = len(chosen) == len(self.buffers) and (skew is None or skew * 1e6 <= self.tolerance)
                return chosen, complete, skew
            latest = [q[-1].meta.capture_ns for q in self.buffers if q]
            if not latest:
                self.pending = None
                return None
            if self.pending is None:
                self.pending = (max(latest), now)
            target, started = self.pending
            chosen = {}
            for stream, q in enumerate(self.buffers):
                if q:
                    frame = min(q, key=lambda f: abs(f.meta.capture_ns - target))
                    if abs(frame.meta.capture_ns - target) <= self.tolerance:
                        chosen[stream] = frame
            times = [f.meta.capture_ns for f in chosen.values()]
            skew = (max(times) - min(times)) / 1e6 if len(times) > 1 else None
            complete = len(chosen) == len(self.buffers) and (skew is None or skew * 1e6 <= self.tolerance)
            if not complete and now - started < self.wait:
                return None
            self.pending = None
            # Remove selected and older frames, including unusable expired groups.
            for stream, q in enumerate(self.buffers):
                cutoff = chosen[stream].meta.capture_ns if stream in chosen else target - self.tolerance
                while q and q[0].meta.capture_ns <= cutoff:
                    removed = q.popleft()
                    if removed is not chosen.get(stream) or (self.strict and not complete):
                        self.dropped += 1
            if not chosen:
                return None
            return ({ } if self.strict and not complete else chosen), complete, skew
