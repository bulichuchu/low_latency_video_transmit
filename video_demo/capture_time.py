"""Camera capture timestamps in our monotonic clock.

SDK epoch timestamps are mapped without fitting device time to USB arrival time
(which would hide the camera/USB delay). The SDK does not expose its fit error;
the guard below checks plausibility and stability, not absolute accuracy.
Reported clock error covers only host mapping.

System cameras (AVFoundation, V4L2) already deliver frame PTS on the host
monotonic clock; HostPtsClock only validates and forwards them.
"""
from collections import deque

TIMESTAMP_STATUSES = ('unavailable', 'warming_up', 'ready', 'unsupported', 'sdk_error',
                      'invalid', 'clock_jump', 'device_reset', 'legacy_helper')


class GlobalTimestampMap:
    def __init__(self):
        self.offset = self.device_us = self.global_us = self.started = None
        self.fit_offsets = deque(maxlen=8)

    def update(self, device_us, global_us, system_us, wall_ns, mono_before, mono_after, received_ns):
        fields = dict(sensor_capture_ns=0, sensor_status='warming_up', sensor_clock_uncertainty_us=0)
        midpoint = (mono_before + mono_after) // 2
        offset = midpoint - wall_ns
        reason = None
        if self.offset is not None and abs(offset - self.offset) > 5_000_000:
            reason = 'clock_jump'
        elif self.device_us is not None and device_us <= self.device_us:
            reason = 'device_reset'
        self.offset, self.device_us = offset, device_us
        mapped = global_us * 1000 + offset
        if reason is None and (not device_us or not global_us or not system_us):
            reason = 'warming_up'
        if reason is None and (mono_after < mono_before or mono_after - mono_before > 2_000_000
                or not 0 <= received_ns - mapped <= 5_000_000_000
                or not 0 <= wall_ns - system_us * 1000 <= 5_000_000_000
                or global_us > system_us
                or (self.global_us is not None and global_us <= self.global_us)):
            reason = 'invalid'
        if reason:
            self.started = None
            self.fit_offsets.clear()
            self.global_us = None
            fields['sensor_status'] = reason
            return fields
        self.global_us = global_us
        if self.started is None:
            self.started = midpoint
        self.fit_offsets.append(global_us - device_us)
        if len(self.fit_offsets) == 8 and max(self.fit_offsets) - min(self.fit_offsets) > 2000:
            self.started = midpoint
        if (len(self.fit_offsets) == 8 and midpoint - self.started >= 2_000_000_000
                and max(self.fit_offsets) - min(self.fit_offsets) <= 2000):
            fields.update(sensor_capture_ns=mapped, sensor_status='ready',
                          sensor_clock_uncertainty_us=(mono_after - mono_before + 1999) // 2000 + 1)
        return fields


HOST_PTS_BACKENDS = ('avfoundation', 'v4l2')
MAX_HOST_PTS_AGE_NS = 1_000_000_000


class HostPtsClock:
    """Frame timestamps that a system camera backend already gives in host time.

    FFmpeg passes AVFoundation sample-buffer presentation times (macOS) and V4L2
    monotonic buffer times (Linux) as microsecond PTS on the same clock as
    time.perf_counter_ns(), so no fitting is needed. Which instant they mark
    (exposure, readout or USB arrival) depends on the device and driver and is
    not calibrated. DirectShow PTS are stream-relative: nothing is reported.
    """
    def __init__(self, backend):
        self.source = f'{backend}_pts' if backend in HOST_PTS_BACKENDS else None
        self.last_ns = None

    def fields(self, frame, dequeued_ns):
        if self.source is None:
            return {}
        fields = dict(sensor_capture_ns=0, sdk_device_timestamp_us=0, sdk_global_timestamp_us=0,
                      sensor_status='invalid', sensor_clock_uncertainty_us=0,
                      camera_timestamp_source=self.source)
        if frame.pts is None or frame.time_base is None:
            return fields
        stamp_ns = int(frame.pts * frame.time_base * 1_000_000_000)
        previous, self.last_ns = self.last_ns, stamp_ns
        if previous is not None and stamp_ns <= previous:
            fields['sensor_status'] = 'device_reset'
        elif 0 < dequeued_ns - stamp_ns <= MAX_HOST_PTS_AGE_NS:
            # The RTP v2 extension requires non-zero device/global values for
            # 'ready'; both carry the same host-clock microseconds here.
            stamp_us = stamp_ns // 1000
            fields.update(sensor_capture_ns=stamp_ns, sdk_device_timestamp_us=stamp_us,
                          sdk_global_timestamp_us=stamp_us, sensor_status='ready')
        return fields


def sensor_latency_fields(meta, clock, now):
    """Native receiver measurements; WebCodecs adds its own host/browser mapping."""
    offset, uncertainty = clock.estimate(now)
    latency = ((now - (meta.sensor_capture_ns - offset)) / 1e6
               if meta.sensor_status == 'ready' and meta.sensor_capture_ns and offset is not None else None)
    if latency is not None and latency < 0:
        latency = None
    return dict(sensor_latency_ms=latency,
                sensor_clock_uncertainty_ms=(uncertainty + meta.sensor_clock_uncertainty_us / 1000)
                if latency is not None else None)
