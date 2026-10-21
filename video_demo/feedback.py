"""Best-effort UDP control traffic from a receiver to its sender.

PLI, NACK, receiver reports, clock pings and WebRTC signalling all help, but
none is worth stopping reception for. When the sender hangs or leaves the
network, sendto fails (macOS: EHOSTUNREACH "No route to host" once ARP gives
up; seen on 10-14 when the robot froze) and the receiver must keep listening,
so the stream resumes as soon as the sender is back.
"""
from __future__ import annotations

from collections import Counter
import errno
import threading
import time


def error_name(exc):
    return errno.errorcode.get(exc.errno, str(exc.errno)) if exc.errno is not None else type(exc).__name__


class BestEffortSender:
    """sendto that never raises. Failures are counted per channel (pli, nack,
    rtcp_rr, clock_ping, webrtc_signal, recv) and journaled as
    `udp_control_error` at most once per second per channel."""

    def __init__(self, sock, journal, *, clock=time.perf_counter_ns, interval_ns=1_000_000_000):
        self.sock, self.journal, self.clock = sock, journal, clock
        self.interval_ns = interval_ns
        self.lock = threading.Lock()
        self.unlogged = Counter()
        self.last_errno = {}
        self.logged_ns = {}

    def send(self, data, address, channel):
        try:
            self.sock.sendto(data, address)
            return True
        except OSError as exc:
            self.failed(channel, exc)
            return False

    def failed(self, channel, exc):
        name, now = error_name(exc), self.clock()
        with self.lock:
            self.unlogged[channel] += 1
            self.last_errno[channel] = name
            last = self.logged_ns.get(channel)
            if last is not None and now - last < self.interval_ns:
                return
            self.logged_ns[channel] = now
            count = self.unlogged.pop(channel)
        self.journal.log('udp_control_error', channel=channel, errno=name, count=count, message=str(exc))

    def flush(self):
        """Journal the failures still held back by the rate limit; call before closing the journal."""
        with self.lock:
            pending, self.unlogged = self.unlogged, Counter()
            names = dict(self.last_errno)
        for channel, count in pending.items():
            self.journal.log('udp_control_error', channel=channel, errno=names.get(channel), count=count)


class KeyRequestPacer:
    """When a stream may send its next PLI (request for an IDR).

    At most one per interval: 150 ms, plus two round trips on a long path (the
    requested IDR needs that long to arrive). Each further request while none of
    the requested IDRs has arrived complete doubles the interval, up to 2 s: the
    sender's periodic IDRs (every second on the RK) still come, and a failed IDR
    asking for another only adds bursts. On 10-16 over the internet one stream
    went up to 9.5 IDRs a second, 85-94 % of its bytes, and fewer of them made it.
    A key frame that arrives complete resets the interval."""

    BASE_NS, CAP_NS = 150_000_000, 2_000_000_000

    def __init__(self):
        self.last = None   # when the last request went out
        self.failures = 0  # requests since the last complete key frame

    def due(self, now, rtt_ns=0):
        """True (and counted) if a request may go out now."""
        if self.last is not None:
            base = self.BASE_NS + 2 * max(0, int(rtt_ns))
            wait = min(self.CAP_NS, base << min(max(0, self.failures - 1), 5))
            if now - self.last < wait:
                return False
        self.last = now
        self.failures += 1
        return True

    def key_frame(self):
        self.failures = 0
