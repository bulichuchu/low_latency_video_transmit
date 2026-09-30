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
        self.browser = {}

    def bind(self, request_key, journal, stats, clock=None):
        with self.lock:
            self.request_key, self.journal, self.stats = request_key, journal, stats
            self.clock = clock

    def unbind(self):
        with self.lock:
            self.request_key = lambda stream: None
            self.journal = self.stats = self.clock = None
            self.queue.clear()

    def attach(self, connected):
        with self.lock:
            self.connected = connected
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

    def offer(self, unit, clock):
        with self.lock:
            if not self.connected:
                return
            stream = unit.meta.stream
            if sum(item[0].stream == stream for item in self.queue) >= 3:
                self.drops += 1
                if self.journal:
                    self.journal.log('drop', unit.meta, reason='browser_bridge_backpressure')
                self.recover(stream)
            if stream in self.waiting:
                if not unit.meta.key:
                    self.request_key(stream)
                    return
                self.waiting.remove(stream)
            now = time.perf_counter_ns()
            offset, uncertainty = clock.estimate(now)
            header = dict(stream=stream, epoch=unit.meta.epoch, frame_id=unit.meta.frame_id,
                          key=unit.meta.key, depth_preview=unit.meta.depth_preview,
                          capture_ms=unit.meta.capture_ns / 1e6,
                          host_capture_ms=(unit.meta.capture_ns - offset) / 1e6 if offset is not None else None,
                          uncertainty_ms=uncertainty,
                          sensor_status=unit.meta.sensor_status,
                          host_sensor_capture_ms=(unit.meta.sensor_capture_ns - offset) / 1e6
                          if unit.meta.sensor_status == 'ready' and offset is not None else None,
                          sensor_clock_uncertainty_ms=(uncertainty + unit.meta.sensor_clock_uncertainty_us / 1000)
                          if unit.meta.sensor_status == 'ready' and offset is not None else None,
                          sdk_device_timestamp_us=unit.meta.sdk_device_timestamp_us or None,
                          sdk_global_timestamp_us=unit.meta.sdk_global_timestamp_us or None)
            self.queue.append((unit.meta, header, unit.bitstream))

    def pop(self):
        with self.lock:
            if not self.queue:
                return None
            meta, header, data = self.queue.popleft()
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
                for name in ('latency_ms', 'browser_decode_ms', 'browser_wait_ms',
                             'browser_draw_ms', 'clock_uncertainty_ms', 'sync_skew_ms', 'browser_submit_ms',
                             'sensor_latency_ms', 'sensor_clock_uncertainty_ms'):
                    value = row.get(name)
                    fields[name] = value if (isinstance(value, (int, float)) and not isinstance(value, bool)
                        and math.isfinite(value) and 0 <= value <= (1e12 if name == 'browser_submit_ms' else 60000)) else None
                if meta.sensor_status != 'ready':
                    fields['sensor_latency_ms'] = fields['sensor_clock_uncertainty_ms'] = None
                status = row.get('sensor_measurement_status')
                fields['sensor_measurement_status'] = (meta.sensor_status if meta.sensor_status != 'ready' else
                    status if status in (*TIMESTAMP_STATUSES, 'clock_sync') else
                    'ready' if fields['sensor_latency_ms'] is not None else 'clock_sync')
                self.journal.log('browser_submit', meta, **fields, decoder='webcodecs')
                if self.stats:
                    self.stats.add(meta.stream, 'presented', latency_ms=fields['latency_ms'], decoder='webcodecs')
                self.browser[str(meta.stream)] = fields

    def snapshot(self):
        with self.lock:
            return dict(connected=self.connected, queued=len(self.queue), bridge_drops=self.drops,
                        udp_rtt_ms=self.clock.network_rtt(time.perf_counter_ns()) if self.clock else None,
                        streams=self.stats.snapshot() if self.stats else [], browser=dict(self.browser))
