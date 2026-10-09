"""Send-rate control for the WebRTC transport (sender side, no aiortc needed).

The sender hands aiortc frames that x264 already encoded at a fixed bitrate, so
nothing reacts to the browser's feedback the way a browser's own WebRTC sender
would. On 10-09 that showed over a VPN path: two 720p60 streams at 5 Mbps each
met 3.5-9 Mbps of capacity, round trips rose from 52 ms to seconds, 4-50 % of
packets were lost and the page showed 5-24 fps.

RateController follows the two halves of WebRTC's congestion control (GCC) in
simplified form, fed by the browser's RTCP:

* loss (receiver reports, counted over at least LOSS_PACKETS packets): above
  10 % the rate drops by half the loss; below 2 % it may grow, 8 % per second,
  or up to 50 % per second while the browser's REMB says there is room (after
  a cut it waits REMB_PROBE_S at the REMB before probing past it); in
  between it holds. Reports that show nothing arriving while we send count as
  total loss;
* delay: a standing queue (the lowest round trip of the last 1.5 s, or of the
  last three reports, well above the 30 s minimum) cuts the rate before loss
  shows, deeper the longer the queue; no round trips while reports arrive
  means a long one: no growth. The
  browser's REMB (Chrome's delay-based estimate) is followed when it cuts, or
  when its first value is already below what we send.

It starts at the configured bitrate, so a LAN keeps today's behaviour, and
never exceeds it. All streams of a session share the path and get an equal
share. When a share falls below half (a quarter) of the configured bitrate the
frame rate halves (quarters) too, so each frame keeps enough bits. The encode
threads apply a new rate by reopening the encoder (rate_switch_due).
"""
from __future__ import annotations

from collections import deque

MIN_STREAM_BPS = 300_000   # per stream; below this 720p is mostly blocks
MIN_FPS = 10
LOSS_HIGH, LOSS_LOW = .10, .02
LOSS_PACKETS = 50          # decide on at least this many packets (or EVAL_MAX_S)
EVAL_MIN_S, EVAL_MAX_S = .25, 1.0
GROWTH_PER_S = .08
FAST_GROWTH_PER_S = .5     # toward the browser's REMB after a cut
REMB_FRESH_S = 5.0
REMB_PROBE_S = 5.0         # held at the REMB this long: probe past it
HOLD_S = 2.0               # no growth this long after a cut
CUT_INTERVAL_S = 1.0       # at most one cut per feedback round (~ the RTCP interval)
QUEUE_MS = 60              # round-trip floor this far above its minimum: a queue
QUEUE_WINDOW_S = 1.5       # the floor: lowest sample of this window (>= 3 samples), so
                           # a few late reports (Wi-Fi/AWDL stalls) are not a queue
QUEUE_STALE_S = 4.0        # fewer there: the last three, if this recent
RTT_FRESH_S = 2.5          # growth needs a round trip this recent (reports come ~1/s)
QUEUE_RECHECK_S = 3.0      # a queue still there this long after a cut: cut again
RTT_WINDOW_S = 30
BASE_RTT_CAP_S = 1.0       # a minimum above this already holds a queue (congested start)
REMB_DROP = .97            # a REMB this far below the previous one is the browser's cut
REMB_BELOW_SENT = .9       # a first REMB this far below what we send is one too
# Encoder reopen pacing (each reopen starts with an IDR).
SWITCH_STEP = .10
SWITCH_DOWN_S, SWITCH_UP_S = .5, 2.0


def _sequence_delta(highest, previous):
    """Packets between two receiver reports. Chrome reports the extended
    highest sequence; aiortc only the 16-bit one, which wraps."""
    if max(highest, previous) < 1 << 16:
        delta = (highest - previous) % (1 << 16)
    else:
        delta = highest - previous
    return delta if 0 <= delta < 1 << 15 else None


class RateController:
    """One WebRTC session's target: (bitrate bps, fps) per stream."""

    def __init__(self, streams, max_bps, fps, min_bps=MIN_STREAM_BPS):
        self.streams, self.max_bps, self.fps = streams, int(max_bps), int(fps)
        self.min_bps = min(int(min_bps), self.max_bps)
        self.estimate = float(self.max_bps * streams)  # all streams together
        self.last_cut = self.last_eval = None
        self.queue_cut = None      # queue (ms) at the last queue cut, until it clears
        self.rtts = deque()        # (time, rtt s)
        self.rtt_seen = False      # the browser's reports do give round trips
        self.counters = {}         # stream -> (highest sequence, cumulative lost, packets we sent)
        self.expected = self.lost = self.sent = 0.0  # since the last decision
        self.fraction = 0.0        # latest reported fraction lost (fallback)
        self.remb = self.remb_at = None
        self.remb_cut = False      # a REMB cut waiting for CUT_INTERVAL_S
        self.capped_at = None      # since when growth waits at the REMB
        self.loss = self.queue = None
        self.divider = 1
        self.reason = 'start'
        self.target = self._target()

    @property
    def total_bps(self):
        return self.target[0] * self.streams

    def latest_rtt(self):
        return self.rtts[-1][1] if self.rtts else None

    def reports(self, blocks, now):
        """Receiver-report blocks of one RTCP packet, as (stream, fraction lost
        0..1, cumulative lost, highest sequence, round trip in s or None,
        packets we have sent on the stream or None). Browsers send these from
        once a second to dozens of times a second (with NACKs); decisions are
        taken every EVAL_MIN_S-EVAL_MAX_S over all packets since the last
        one. True when the target changed."""
        for stream, fraction, cumulative, highest, rtt, sent in blocks:
            previous = self.counters.get(stream)
            self.counters[stream] = (highest, cumulative, sent)
            if previous is not None:
                delta = _sequence_delta(highest, previous[0])
                if delta:
                    self.expected += delta
                    self.lost += min(delta, max(0, cumulative - previous[1]))
                if sent is not None and previous[2] is not None:
                    self.sent += max(0, sent - previous[2])
            self.fraction = fraction
            if rtt is not None and 0 < rtt < 30:
                self.rtts.append((now, rtt))
                self.rtt_seen = True
        if self.last_eval is None:
            self.last_eval = now
            return False
        since = now - self.last_eval
        if since < EVAL_MIN_S or (self.expected < LOSS_PACKETS and since < EVAL_MAX_S):
            return False
        dt = min(1.0, since)
        self.last_eval = now
        if self.expected:
            self.loss = self.lost / self.expected
        elif self.sent >= LOSS_PACKETS:
            self.loss = 1.0  # we sent, and the reports show nothing arriving
        else:
            self.loss = None  # nothing sent, nothing to judge
        self.expected = self.lost = self.sent = 0.0
        while self.rtts and now - self.rtts[0][0] > RTT_WINDOW_S:
            self.rtts.popleft()
        self.queue = self._queue_ms(now)
        if self.queue is None or self.queue < QUEUE_MS:
            self.queue_cut = None
        if self._remb_cut(now):
            return self._update()
        cut = None
        if self.loss is not None and self.loss > LOSS_HIGH:
            cut = (1 - .5 * self.loss, 'loss')
        if self.queue is not None and self.queue >= QUEUE_MS and (
                self.queue_cut is None or self.queue > self.queue_cut + QUEUE_MS / 2
                or now - self.last_cut >= QUEUE_RECHECK_S):
            # A queue still draining after our cut is not a new signal; one
            # that grows, or outlasts QUEUE_RECHECK_S, is.
            factor = max(.5, 1 - self.queue / 1000)
            if cut is None or factor < cut[0]:
                cut = (factor, 'queue')
        # Round trips stopped while reports arrive: they are too long to match
        # our sender reports, or the reports themselves are late. Hold.
        if self.rtt_seen:
            delay_clear = (self.queue is not None and self.queue < QUEUE_MS / 2
                           and now - self.rtts[-1][0] <= RTT_FRESH_S)
        else:
            delay_clear = True  # this browser gives no round trips: loss and REMB only
        if cut is not None:
            if self._may_cut(now):
                self._set(self.estimate * cut[0], cut[1], now)
                if cut[1] == 'queue':
                    self.queue_cut = self.queue
        elif (self.loss is not None and self.loss < LOSS_LOW and delay_clear
              and (self.last_cut is None or now - self.last_cut >= HOLD_S)):
            remb = self.remb if self.remb is not None and now - self.remb_at <= REMB_FRESH_S else None
            if remb is not None and remb > self.estimate * 1.1:
                grown = min(remb, self.estimate * (1 + FAST_GROWTH_PER_S * dt))
                self.capped_at = None
            else:
                grown = self.estimate * (1 + GROWTH_PER_S * dt)
                if remb is not None and self.last_cut is not None and grown > remb:
                    # After a cut the browser's estimate is the better guide:
                    # probing past it at the bottleneck only builds a queue
                    # (closed-loop test, 3 Mbps path). But near its last cut
                    # Chrome raises it a few kbps/s, so if the path got wider
                    # we would stay low: after REMB_PROBE_S at the cap, probe.
                    if self.capped_at is None:
                        self.capped_at = now
                    if now - self.capped_at < REMB_PROBE_S:
                        grown = max(remb, self.estimate)
            grown = min(self.max_bps * self.streams, grown)
            if grown > self.estimate:
                self.estimate, self.reason = grown, 'probe'
        return self._update()

    def remb_report(self, bps, now, sent_bps=None):
        """The browser's REMB for our streams (bps, all of them; sent_bps: what
        we sent lately). It lowers the rate when it falls, or when its first
        value is already well below what we send (libwebrtc sends none until
        its first overuse or after 5 s). Otherwise it does not: with static
        content Chrome's estimate stays near 1.5x what it received, which says
        nothing about the path. Above our rate it lets growth after a cut go
        faster (reports()). A cut that comes within CUT_INTERVAL_S of another
        waits for the next chance instead of being lost."""
        previous, self.remb, self.remb_at = self.remb, bps, now
        if previous is not None:
            falling = bps < previous * REMB_DROP
        else:
            falling = bool(sent_bps) and bps < sent_bps * REMB_BELOW_SENT
        if falling and bps < self.estimate:
            self.remb_cut = True
        return self._remb_cut(now) and self._update()

    def _remb_cut(self, now):
        if not self.remb_cut or not self._may_cut(now):
            return False
        self.remb_cut = False
        if self.remb is None or now - self.remb_at > REMB_FRESH_S or self.remb >= self.estimate:
            return False  # the browser's estimate has caught up meanwhile
        self._set(self.remb, 'remb', now)
        return True

    def _may_cut(self, now):
        return self.last_cut is None or now - self.last_cut >= CUT_INTERVAL_S

    def _set(self, estimate, reason, now):
        self.estimate = max(self.min_bps * self.streams, estimate)
        self.reason, self.last_cut, self.capped_at = reason, now, None

    def _queue_ms(self, now):
        recent = [rtt for t, rtt in self.rtts if now - t <= QUEUE_WINDOW_S]
        if len(recent) < 3:
            # One stream: about one report a second. Take its last three.
            recent = [rtt for t, rtt in list(self.rtts)[-3:] if now - t <= QUEUE_STALE_S]
            if len(recent) < 3:
                return None
        base = min(min(rtt for _, rtt in self.rtts), BASE_RTT_CAP_S)
        return max(0.0, min(recent) - base) * 1000

    def _target(self):
        share = max(self.min_bps, min(self.max_bps, self.estimate / self.streams))
        ratio = share / self.max_bps
        # Steps with hysteresis: down below 1/2 and 1/4, back up at 0.6 and 0.3.
        if self.divider == 1 and ratio < .5:
            self.divider = 2
        if self.divider == 2 and ratio < .25:
            self.divider = 4
        if self.divider == 4 and ratio >= .3:
            self.divider = 2
        if self.divider == 2 and ratio >= .6:
            self.divider = 1
        fps = self.fps if self.divider == 1 else max(min(self.fps, MIN_FPS), round(self.fps / self.divider))
        return int(share), fps

    def _update(self):
        target = self._target()
        changed, self.target = target != self.target, target
        return changed


def rate_switch_due(current, wanted, since, configured):
    """Encode threads: reopen the encoder for `wanted` (bps, fps) now?

    The configured rate (no viewer, or fully recovered) and frame-rate changes
    apply at once; bitrate cuts of 10 % or more after SWITCH_DOWN_S since the
    last reopen, raises after SWITCH_UP_S; smaller changes wait."""
    if wanted == current:
        return False
    if wanted == configured or wanted[1] != current[1]:
        return True
    change = wanted[0] / current[0] - 1
    if change <= -SWITCH_STEP:
        return since >= SWITCH_DOWN_S
    if change >= SWITCH_STEP:
        return since >= SWITCH_UP_S
    return False
