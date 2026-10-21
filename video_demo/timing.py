from collections import deque
from dataclasses import dataclass
import threading


PING_KEEP_NS = 10_000_000_000  # unanswered clock pings are kept (and round trips accepted) this long
# Without fresh clock samples (round trips over 200 ms: a congested or stalled path)
# the last estimate stays in use this long, its uncertainty growing by DRIFT_PPM.
CLOCK_HOLD_NS = 60_000_000_000
DRIFT_PPM = 100


class ClockMap:
    """NTP-style four-timestamp estimate; no claim of hardware/PTP precision."""
    def __init__(self, shared=False):
        self.shared = shared
        self.samples = deque(maxlen=32)
        self.latest_rtt = None
        self.held = None  # (time of the sample, offset, uncertainty ms) of the last estimate
        self.lock = threading.Lock()

    def update(self, t1, t2, t3, t4):
        if not all(type(t) is int and t > 0 for t in (t1, t2, t3, t4)):
            return False
        rtt = (t4 - t1) - (t3 - t2)
        # The receiver keeps unanswered probes for PING_KEEP_NS. Keep slow replies
        # for the path's round trip even when unsuitable for clock fitting.
        if t4 < t1 or t3 < t2 or not 0 <= rtt <= PING_KEEP_NS:
            return False
        with self.lock:
            self.latest_rtt = (t4, rtt)
            if rtt > 200_000_000:
                return False
            self.samples.append((t4, rtt, ((t2 - t1) + (t3 - t4)) // 2))
        return True

    def clear(self):
        with self.lock:
            self.samples.clear()
            self.latest_rtt = self.held = None

    def network_rtt(self, now):
        """Latest application-level round trip, not half-RTT or clock fit error."""
        with self.lock:
            sample = self.latest_rtt
        if sample is None or not 0 <= now - sample[0] < 5_000_000_000:
            return None
        return sample[1] / 1e6

    def round_trip_ms(self, now, waiting_since=None):
        """The path's typical round trip (median of the last 5 s of pings), or None.
        Sizes the reassembly windows and NACK timing, and goes to the sender.

        waiting_since: when the oldest ping still unanswered (and newer than the
        last answered one) was sent. When the path is congested its replies come
        seconds late or not at all; the round trip is then at least that long,
        instead of unknown, which would make everything fall back to the LAN
        timing (10-21: queues of 2-9 s, and the sender back at full rate)."""
        with self.lock:
            fresh = sorted(s[1] for s in self.samples if now - s[0] < 5_000_000_000)
            latest = self.latest_rtt
        rtt = None
        if len(fresh) >= 3:
            rtt = fresh[len(fresh) // 2] / 1e6
        elif latest is not None and 0 <= now - latest[0] < 5_000_000_000:
            rtt = latest[1] / 1e6  # only long round trips lately (> 200 ms): no clock samples
        return self._at_least_waiting(rtt, now, waiting_since)

    def recent_rtt_ms(self, now, waiting_since=None):
        """The latest round trip (or the unanswered wait, if longer), or None: what
        the sender's congestion control compares with the path's minimum."""
        with self.lock:
            latest = self.latest_rtt
        rtt = latest[1] / 1e6 if latest is not None and 0 <= now - latest[0] < 5_000_000_000 else None
        return self._at_least_waiting(rtt, now, waiting_since)

    @staticmethod
    def _at_least_waiting(rtt, now, waiting_since):
        if waiting_since is not None and 0 <= now - waiting_since <= PING_KEEP_NS:
            waited = (now - waiting_since) / 1e6
            if rtt is None or waited > rtt:
                return waited
        return rtt

    def estimate(self, now):
        """(sender clock - ours in ns, uncertainty in ms), or (None, None).

        From the best of at least three clock samples of the last 5 s; without
        them (10-21: round trips of seconds on the robot's congested link, for
        tens of seconds) the last such estimate for up to CLOCK_HOLD_NS, its
        uncertainty growing by DRIFT_PPM of its age. Without it every frame's
        latency was unknown then, just when the picture fell seconds behind."""
        if self.shared:
            return 0, 0.0
        with self.lock:
            fresh = [s for s in self.samples if now - s[0] < 5_000_000_000]
            if len(fresh) >= 3:
                at, rtt, offset = min(fresh, key=lambda s: s[1])
                self.held = (at, offset, rtt / 2e6)
                return offset, rtt / 2e6
            held = self.held
        if held is None or not 0 <= now - held[0] <= CLOCK_HOLD_NS:
            return None, None
        return held[1], held[2] + (now - held[0]) * DRIFT_PPM / 1e12


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
