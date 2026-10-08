from fractions import Fraction
import json
import socket
import time

import av
import numpy as np
import pytest

from video_demo.capture_time import GlobalTimestampMap, HostPtsClock, sensor_latency_fields
from video_demo.protocol import Meta, Packetizer, START, parse_packet
from video_demo.timing import ClockMap

WALL = 1_790_000_000_000_000_000
MONO = 40_000_000_000


def sample(mapper, n, **changes):
    tick = n * 100_000_000
    values = dict(device_us=1_000_000 + tick // 1000,
                  global_us=(WALL + tick - 30_000_000) // 1000,
                  system_us=(WALL + tick - 10_000_000) // 1000,
                  wall_ns=WALL + tick, mono_before=MONO + tick,
                  mono_after=MONO + tick + 200, received_ns=MONO + tick)
    values.update(changes)
    return mapper.update(**values)


def ready_map():
    mapper = GlobalTimestampMap()
    for n in range(21):
        result = sample(mapper, n)
        assert result['sensor_status'] == ('ready' if n == 20 else 'warming_up')
    return mapper


def test_global_time_maps_without_subtracting_camera_or_usb_delay():
    result = sample(ready_map(), 21)
    assert result['sensor_status'] == 'ready'
    assert result['sensor_capture_ns'] == MONO + 2_100_000_000 - 30_000_000 + 100
    assert result['sensor_clock_uncertainty_us'] == 2


def test_wall_clock_step_invalidates_measurement_and_requires_new_warmup():
    mapper = ready_map()
    result = sample(mapper, 21, wall_ns=WALL + 3_100_000_000)
    assert result['sensor_status'] == 'clock_jump' and result['sensor_capture_ns'] == 0
    assert sample(mapper, 22)['sensor_status'] == 'clock_jump'
    assert sample(mapper, 23)['sensor_status'] == 'warming_up'


def test_zero_future_reset_and_unstable_sdk_fit_never_become_latency():
    assert sample(GlobalTimestampMap(), 0, global_us=0)['sensor_status'] == 'warming_up'
    assert sample(GlobalTimestampMap(), 0, global_us=(WALL + 1_000_000) // 1000)['sensor_status'] == 'invalid'
    assert sample(ready_map(), 21, device_us=1)['sensor_status'] == 'device_reset'
    mapper = GlobalTimestampMap()
    for n in range(50):
        # A fit oscillating by 10ms is not a stable common clock.
        result = sample(mapper, n, global_us=(WALL + n * 100_000_000 - (30_000_000 if n % 2 else 40_000_000)) // 1000)
        assert result['sensor_capture_ns'] == 0


def test_receiver_applies_sender_offset_once_and_rejects_unmapped_time():
    clock = ClockMap()
    for n in range(3):
        # sender clock is 100ms ahead; symmetric 2ms RTT.
        t = 1_000_000_000 + n * 1_000_000
        clock.update(t, t + 101_000_000, t + 101_000_000, t + 2_000_000)
    meta = Meta(0, 1, 1, 1_190_000_000, sensor_capture_ns=1_170_000_000,
                sensor_status='ready', sensor_clock_uncertainty_us=5)
    values = sensor_latency_fields(meta, clock, 1_100_000_000)
    assert values['sensor_latency_ms'] == 30
    assert values['sensor_clock_uncertainty_ms'] == 1.005
    assert sensor_latency_fields(meta, ClockMap(), 1_100_000_000)['sensor_latency_ms'] is None
    assert sensor_latency_fields(meta, clock, 9_000_000_000)['sensor_latency_ms'] is None


def camera_frame(pts_us):
    frame = av.VideoFrame(16, 16, 'nv12')
    frame.pts, frame.time_base = pts_us, Fraction(1, 1_000_000)
    return frame


def test_system_camera_pts_on_host_clock_becomes_camera_timestamp():
    dequeued = 5_000_000_000_000
    stamp = dequeued - 55_000_000
    fields = HostPtsClock('avfoundation').fields(camera_frame(stamp // 1000), dequeued)
    assert fields == dict(sensor_capture_ns=stamp, sdk_device_timestamp_us=stamp // 1000,
                          sdk_global_timestamp_us=stamp // 1000, sensor_status='ready',
                          sensor_clock_uncertainty_us=0, camera_timestamp_source='avfoundation_pts')
    # Same RTP v2 fields as SDK timestamps, so existing receivers accept them unchanged.
    meta = Meta(0, 1, 0, dequeued, **{k: v for k, v in fields.items() if k != 'camera_timestamp_source'})
    parsed = parse_packet(Packetizer().packetize(START + b'\x65abc', meta)[0]).meta
    assert parsed.sensor_status == 'ready' and dequeued - parsed.sensor_capture_ns == 55_000_000


def test_implausible_or_backward_system_camera_pts_never_becomes_latency():
    now = 5_000_000_000_000
    future = HostPtsClock('v4l2').fields(camera_frame(now // 1000 + 1_000), now)
    stale = HostPtsClock('v4l2').fields(camera_frame((now - 2_000_000_000) // 1000), now)
    missing = camera_frame(0)
    missing.pts = None
    unknown = HostPtsClock('avfoundation').fields(missing, now)
    for fields in (future, stale, unknown):
        assert fields['sensor_status'] == 'invalid' and fields['sensor_capture_ns'] == 0
    clock = HostPtsClock('avfoundation')
    assert clock.fields(camera_frame((now - 40_000_000) // 1000), now)['sensor_status'] == 'ready'
    assert clock.fields(camera_frame((now - 80_000_000) // 1000), now + 33_000_000)['sensor_status'] == 'device_reset'
    # DirectShow PTS are stream-relative: no camera timestamp, no v2 extension.
    assert HostPtsClock('dshow').fields(camera_frame(1_000), now) == {}


class FakeCaptureDevice:
    """Frames as FFmpeg capture delivers them: tagged I, PTS on the host clock."""
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def decode(self, video=0):
        rng = np.random.default_rng(5)
        while True:
            time.sleep(1 / 30)
            frame = av.VideoFrame.from_ndarray(rng.integers(0, 256, (64, 64, 3), dtype=np.uint8), format='rgb24')
            frame.pict_type = av.video.frame.PictureType.I
            frame.pts, frame.time_base = (time.perf_counter_ns() - 40_000_000) // 1000, Fraction(1, 1_000_000)
            yield frame


@pytest.mark.parametrize('system, expected', [('Darwin', 'ready'), ('Windows', 'unavailable')])
def test_sender_sends_system_camera_timestamp_and_p_frames(tmp_path, monkeypatch, system, expected):
    from video_demo import cli, sender
    monkeypatch.setattr('video_demo.cameras.platform.system', lambda: system)
    monkeypatch.setattr('video_demo.cameras.inventory', lambda include_modes=True: {'devices': []})
    monkeypatch.setattr(sender, 'open_camera', lambda *args: FakeCaptureDevice())
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind(('127.0.0.1', 0))
    packets = []
    try:
        p = cli.parser()
        args = p.parse_args(['send', '--host', '127.0.0.1', '--port', str(receiver.getsockname()[1]),
                             '--cameras', 'Fake Camera', '--width', '64', '--height', '64', '--fps', '30',
                             '--encoder', 'libx264', '--duration', '0.8', '--output', str(tmp_path / 'sender')])
        cli.validate(p, args)
        sender.run_sender(args)
        receiver.setblocking(False)
        while True:
            try:
                packets.append(parse_packet(receiver.recv(65535)))
            except BlockingIOError:
                break
    finally:
        receiver.close()
    frames = {packet.meta.frame_id: packet.meta for packet in packets}
    assert len(frames) >= 5
    assert [i for i, meta in sorted(frames.items()) if meta.key] == [0]
    assert {meta.sensor_status for meta in frames.values()} == {expected}
    if expected == 'ready':
        assert all(39 <= (m.capture_ns - m.sensor_capture_ns) / 1e6 < 100 for m in frames.values())
    # encode_us carries the sender's whole share: capture -> encoded.
    events = [json.loads(line) for line in (tmp_path / 'sender' / 'events.jsonl').read_text().splitlines()]
    encodes = [e for e in events if e['event'] == 'encode']
    assert encodes and all(abs(e['encode_us'] / 1000 - e['raw_queue_ms'] - e['prepare_ms'] - e['codec_encode_ms']) < .002
                           for e in encodes)
