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
