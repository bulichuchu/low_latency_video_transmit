"""Bounded, thread-safe H.264 handoff to one local browser viewer.

No transcoding, image files or base64. Browser measurements are explicitly
separate from native decode/submission and from optical scene-to-screen delay.
"""
from collections import OrderedDict, deque
import json
import math
import struct
import threading
import time
from .capture_time import TIMESTAMP_STATUSES

# Ordered steps of a presented frame, as the page computes them (player.js).
STEP_FIELDS = ('camera_ms', 'sender_ms', 'network_ms', 'viewer_ms', 'delivery_ms', 'browser_ms')
# A stream's oldest queued frame waiting this long for the viewer (on a LAN, where
# the picture must stay within ~0.1 s, shorter), or this many frames queued: the
# page is not keeping up; they go and the stream restarts at an IDR. 0.6 did so at
# three frames, which every burst after a network stall reached (10-21: a second or
# two of frames arriving at once). A burst itself queues nothing for long.
BACKLOG_NS, LAN_BACKLOG_NS = 300_000_000, 150_000_000
MAX_QUEUED = 64


class BrowserBridge:
    def __init__(self, streams):
        self.streams = streams
        self.lock = threading.RLock()
        self.queue = deque()
        self.waiting = set(range(streams))
        self.connected = False
        self.request_key = lambda stream: None
        self.journal = self.stats = self.clock = None
        self.sent = OrderedDict()
        self.drops = 0
        self.superseded = 0  # queued frames an IDR made unnecessary (offer)
        self.browser = {}
        self.notify = None
        self.stream_names = []
        self.sender_rates = []  # per stream: what the sender says it sends (RK 0.6), or None
        self.path_rtt_ms = None  # the receiver's median round trip, set with every clock ping

    def set_path_rtt(self, rtt_ms):
        self.path_rtt_ms = rtt_ms

    def set_sender_rates(self, rates):
        with self.lock:
            self.sender_rates = list(rates[:self.streams])

    def set_stream_names(self, names):
        with self.lock:
            self.stream_names = [name.strip()[:120] if isinstance(name, str) else ''
                                 for name in names[:self.streams]] if isinstance(names, list) else []

    def bind(self, request_key, journal, stats, clock=None):
        with self.lock:
            self.request_key, self.journal, self.stats = request_key, journal, stats
            self.clock = clock

    def unbind(self):
        with self.lock:
            self.request_key = lambda stream: None
            self.journal = self.stats = self.clock = None
            self.queue.clear()

    def attach(self, connected, notify=None):
        """notify() runs on the receiver thread after each queued frame (it must
        only hand off, e.g. loop.call_soon_threadsafe), so the viewer need not poll."""
        with self.lock:
            self.connected = connected
            self.notify = notify if connected else None
            self.queue.clear()
            self.sent.clear()
            self.waiting = set(range(self.streams))
            self.browser = {}
            if connected:
                for stream in range(self.streams):
                    self.request_key(stream)

    def recover(self, stream):
        with self.lock:
            if isinstance(stream, int) and 0 <= stream < self.streams:
                self.queue = deque(item for item in self.queue if item[0].stream != stream)
                self.waiting.add(stream)
                self.request_key(stream)

    def browser_recover(self, stream, reason):
        """Keep browser recovery visible separately from UDP packet loss."""
        if type(stream) is not int or not 0 <= stream < self.streams:
            return
        allowed = ('awaiting_key', 'epoch_changed', 'frame_gap', 'decode_queue_full',
                   'decode_output_stalled', 'decode_error')
        with self.lock:
            if self.journal:
                self.journal.log('browser_recovery', stream=stream,
                                 reason=reason if reason in allowed else 'unspecified')
            self.recover(stream)

    def _supersede(self, stream):
        """An IDR needs none of its stream's frames still queued: they go, and the
        newest picture comes sooner (after a stall a burst queues up here). The page
        sees a gap ending at an IDR and carries on from it."""
        old = [item for item in self.queue if item[0].stream == stream]
        if old:
            self.queue = deque(item for item in self.queue if item[0].stream != stream)
            self.superseded += len(old)
            if self.journal:
                for item in old:
                    self.journal.log('drop', item[0], reason='bridge_superseded_by_idr')

    def offer(self, unit, clock):
        with self.lock:
            if not self.connected:
                return
            stream = unit.meta.stream
            now = time.perf_counter_ns()
            if unit.meta.key:
                self._supersede(stream)
            queued = [item for item in self.queue if item[0].stream == stream]
            limit = BACKLOG_NS if (self.path_rtt_ms or 0) >= 12 else LAN_BACKLOG_NS
            if queued and (now - queued[0][3] > limit or len(queued) >= MAX_QUEUED):
                self.drops += 1
                if self.journal:
                    self.journal.log('drop', unit.meta, reason='browser_bridge_backpressure')
                self.recover(stream)
            if stream in self.waiting:
                if not unit.meta.key:
                    self.request_key(stream)
                    return
                self.waiting.remove(stream)
            offset, uncertainty = clock.estimate(now)
            header = dict(stream=stream, epoch=unit.meta.epoch, frame_id=unit.meta.frame_id,
                          key=unit.meta.key, depth_preview=unit.meta.depth_preview,
                          capture_ms=unit.meta.capture_ns / 1e6,
                          sender_ms=unit.meta.encode_us / 1000,
                          host_capture_ms=(unit.meta.capture_ns - offset) / 1e6 if offset is not None else None,
                          uncertainty_ms=uncertainty,
                          sensor_status=unit.meta.sensor_status,
                          host_sensor_capture_ms=(unit.meta.sensor_capture_ns - offset) / 1e6
                          if unit.meta.sensor_status == 'ready' and offset is not None else None,
                          sensor_clock_uncertainty_ms=(uncertainty + unit.meta.sensor_clock_uncertainty_us / 1000)
                          if unit.meta.sensor_status == 'ready' and offset is not None else None,
                          sdk_device_timestamp_us=unit.meta.sdk_device_timestamp_us or None,
                          sdk_global_timestamp_us=unit.meta.sdk_global_timestamp_us or None)
            # The page's frame-age limit grows by two round trips on a long path, the
            # time the reassembly windows may have spent on a retransmission; its
            # playout buffer turns on there too.
            rtt = self.path_rtt_ms if self.path_rtt_ms is not None else clock.round_trip_ms(now)
            header['net_rtt_ms'] = round(rtt, 1) if rtt is not None else None
            self.queue.append((unit.meta, header, unit.bitstream, now))
            wake = self.notify
        if wake:
            wake()

    def pop(self):
        with self.lock:
            if not self.queue:
                return None
            meta, header, data, _ = self.queue.popleft()
            header['host_sent_ms'] = time.perf_counter_ns() / 1e6
            key = (meta.stream, meta.epoch, meta.frame_id)
            self.sent[key] = meta
            while len(self.sent) > 2048:
                self.sent.popitem(last=False)
            if self.journal:
                self.journal.log('browser_forward', meta)
            encoded = json.dumps(header, separators=(',', ':')).encode()
            return struct.pack('!I', len(encoded)) + encoded + data

    def telemetry(self, records):
        if not isinstance(records, list) or len(records) > 1024:
            raise ValueError('Invalid browser telemetry batch')
        with self.lock:
            for row in records:
                if not isinstance(row, dict):
                    continue
                key = tuple(row.get(k) for k in ('stream', 'epoch', 'frame_id'))
                if not all(isinstance(k, int) for k in key):
                    continue
                meta = self.sent.pop(key, None)
                if meta is None or self.journal is None:
                    continue
                fields = {}
                for name in ('latency_ms', 'browser_decode_ms', 'browser_wait_ms', 'browser_buffer_ms',
                             'browser_draw_ms', 'clock_uncertainty_ms', 'sync_skew_ms', 'browser_submit_ms',
                             'sensor_latency_ms', 'sensor_clock_uncertainty_ms', *STEP_FIELDS):
                    value = row.get(name)
                    # Steps that cross clocks may come out slightly negative.
                    low = -1000 if name in STEP_FIELDS else 0
                    fields[name] = value if (isinstance(value, (int, float)) and not isinstance(value, bool)
                        and math.isfinite(value) and low <= value <= (1e12 if name == 'browser_submit_ms' else 60000)) else None
                if meta.sensor_status != 'ready':
                    fields['sensor_latency_ms'] = fields['sensor_clock_uncertainty_ms'] = None
                status = row.get('sensor_measurement_status')
                fields['sensor_measurement_status'] = (meta.sensor_status if meta.sensor_status != 'ready' else
                    status if status in (*TIMESTAMP_STATUSES, 'clock_sync') else
                    'ready' if fields['sensor_latency_ms'] is not None else 'clock_sync')
                self.journal.log('browser_submit', meta, **fields, decoder='webcodecs', transport='rtp')
                if self.stats:
                    self.stats.add(meta.stream, 'presented', latency_ms=fields['latency_ms'], decoder='webcodecs')
                self.browser[str(meta.stream)] = fields

    def snapshot(self):
        with self.lock:
            return dict(connected=self.connected, queued=len(self.queue), bridge_drops=self.drops,
                        bridge_superseded=self.superseded,
                        stream_names=list(self.stream_names), sender_rates=list(self.sender_rates),
                        udp_rtt_ms=self.clock.network_rtt(time.perf_counter_ns()) if self.clock else None,
                        streams=self.stats.snapshot() if self.stats else [], browser=dict(self.browser))
