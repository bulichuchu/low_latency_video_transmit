from contextlib import contextmanager
import ctypes as ct
import json
import threading
import subprocess
import sys
import time
from pathlib import Path

import av
import numpy as np
import pytest

from video_demo import sdk, orbbec
from video_demo.cameras import configure_camera_inputs, read_profile
from video_demo.cli import parser, validate
from video_demo.protocol import Meta, Packetizer, Assembler, START, parse_packet


def record(stream='color'):
    return dict(name='Example camera · ' + stream, device=sdk.device_uri('ABC', stream),
                backend='orbbec', sdk_stream=stream, serial='ABC', formats=[
                    dict(width=640, height=480, pixel_format='rgb24' if stream == 'color' else 'gray16le',
                         frame_rates=[dict(min=15, max=15), dict(min=30, max=30)])])


def test_sdk_profile_negotiation_and_no_local_camera_probe(tmp_path, monkeypatch):
    monkeypatch.setattr(sdk, 'inventory', lambda: dict(devices=[record()]))
    monkeypatch.setattr('video_demo.cameras.inventory', lambda **kw: pytest.fail('SDK input must not query UVC modes'))
    p = parser()
    args = p.parse_args(['demo', '--cameras', 'orbbec://ABC/color', '--headless', '--duration', '1',
                        '--fps', '15', '--output', str(tmp_path / 'run')])
    validate(p, args)
    configure_camera_inputs(args)
    chosen = args.camera_settings[0]
    assert (chosen['width'], chosen['height'], chosen['fps'], chosen['pixel_format']) == (640, 480, 15, 'rgb24')
    assert args.camera_modes[0]['status'] == 'sdk_capabilities_selected'
    path = tmp_path / 'profile.json'
    path.write_text(json.dumps(dict(version=1, cameras=args.camera_settings)))
    assert read_profile(path)['cameras'] == args.camera_settings


def test_sdk_modes_refuse_missing_sensor_and_duplicate_device(monkeypatch):
    monkeypatch.setattr(sdk, 'inventory', lambda: dict(devices=[record(), record('depth')]))
    with pytest.raises(ValueError, match='未提供'):
        sdk.configure_inputs([dict(device='orbbec://ABC/ir')], 640, 480, 30)
    with pytest.raises(ValueError, match='一次选择'):
        sdk.configure_inputs([dict(device='orbbec://ABC/color'), dict(device='orbbec://ABC/depth')], 640, 480, 30)
    with pytest.raises(ValueError, match='不支持'):
        sdk.configure_inputs([dict(device='orbbec://ABC/color', fps=25)], 640, 480, 30)


@pytest.mark.parametrize('camera', [dict(device='orbbec:///color'), dict(device='orbbec://ABC/unknown'),
    dict(device='orbbec://ABC/color', fps=29.5), dict(device='orbbec://ABC/color', rtsp_transport='udp'),
    dict(device='orbbec://ABC/color', depth_min_mm=0),
    dict(device='orbbec://ABC/depth', depth_min_mm=500, depth_max_mm=200),
    dict(device='orbbec://ABC/depth', depth_max_mm=float('nan'))])
def test_invalid_sdk_settings(camera):
    with pytest.raises(ValueError):
        sdk.validate_camera(camera)


@pytest.mark.parametrize('sdk_format,pixel', [(22, 'rgb24'), (23, 'bgr24'), (25, 'bgra'), (31, 'rgba'),
                                           (0, 'yuyv422'), (2, 'uyvy422'), (3, 'nv12'), (4, 'nv21'), (15, 'yuv420p')])
def test_sdk_color_formats_keep_rgb_channels(sdk_format, pixel):
    expected = np.full((24, 32, 3), (200, 40, 90), dtype=np.uint8)
    source = av.VideoFrame.from_ndarray(expected, format='rgb24').reformat(format=pixel)
    if pixel == 'uyvy422':
        data = np.frombuffer(source.planes[0], dtype=np.uint8).reshape(24, -1)[:, :64].tobytes()
    elif pixel == 'nv21':
        data = b''.join(np.frombuffer(p, dtype=np.uint8).reshape(p.height, -1)[:, :32].tobytes() for p in source.planes)
    else:
        data = source.to_ndarray().tobytes()
    converted = orbbec.convert_image(data, 32, 24, sdk_format).to_ndarray(format='rgb24')
    assert np.abs(converted.astype(float) - expected).mean() < 4


def test_sdk_mjpeg_decode():
    encoder = av.CodecContext.create('mjpeg', 'w')
    encoder.width, encoder.height, encoder.pix_fmt = 32, 24, 'yuvj420p'
    from fractions import Fraction
    encoder.time_base = Fraction(1, 30)
    image = av.VideoFrame.from_ndarray(np.full((24, 32, 3), (30, 180, 90), dtype=np.uint8), format='rgb24')
    encoded = encoder.encode(image.reformat(format='yuvj420p'))[0]
    decoded = orbbec.convert_image(bytes(encoded), 32, 24, 5)
    assert np.abs(decoded.to_ndarray(format='rgb24').astype(float) - (30, 180, 90)).mean() < 6


def test_depth_preview_uses_actual_scale_and_preserves_invalid_black():
    raw = np.array([[0, 1000, 5500, 10000]], dtype='<u2')
    frame = orbbec.convert_image(raw.tobytes(), 4, 1, 8, stream='depth', scale_mm=.1, near_mm=100, far_mm=1000)
    values = frame.to_ndarray()
    assert list(values[0]) == [0, 255, 128, 1]
    assert raw[0, 2] == 5500
    ir = orbbec.convert_image(np.array([[0, 1023]], dtype='<u2').tobytes(), 2, 1, 8, stream='ir', valid_bits=10)
    assert list(ir.to_ndarray()[0]) == [0, 255]


def test_sdk_short_buffer_rejected_before_numpy_reshape():
    with pytest.raises(ValueError, match='length'):
        orbbec.convert_image(b'\x00', 640, 480, 22)
    with pytest.raises(ValueError, match='Unsupported'):
        orbbec.convert_image(b'\x00', 640, 480, 6)


def test_depth_flag_survives_rtp_and_does_not_turn_p_frame_into_idr():
    payload = START + b'\x41' + bytes(range(1, 255)) * 6
    packets = Packetizer(mtu=400).packetize(payload, Meta(0, 1, 1, 123, depth_preview=True))
    assembler = Assembler()
    result = None
    for raw in reversed(packets):
        packet = parse_packet(raw)
        assert packet.meta.depth_preview and not packet.meta.key
        result = assembler.add(packet, 100) or result
    assert result.meta.depth_preview and result.bitstream == payload
    corrupt = bytearray(packets[0])
    corrupt[17] = 4
    with pytest.raises(ValueError, match='extension'):
        parse_packet(bytes(corrupt))


class FakeNative:
    """Exercise session ownership and copying without claiming a hardware test."""
    def __init__(self, short=False):
        self.handles = []
        self.calls = []
        self.raw = ct.create_string_buffer(b'\x10\x30\x70' * (64 * 64))
        self.short = short

    @contextmanager
    def context(self):
        yield 1

    @contextmanager
    def owned(self, kind, pointer):
        self.handles.append((kind, pointer))
        try:
            yield pointer
        finally:
            self.handles.remove((kind, pointer))
            if kind == 'frame' and pointer == 8:
                ct.memset(self.raw, 0, len(self.raw))  # SDK storage becomes invalid at delete.

    def profile_info(self, _):
        return dict(width=64, height=64, fps=30, sdk_format=22)

    def call(self, name, *args):
        self.calls.append(name)
        return {'ob_query_device_list': 2, 'ob_device_list_get_device_by_serial_number': 3,
                'ob_create_pipeline_with_device': 4, 'ob_pipeline_get_stream_profile_list': 5,
                'ob_stream_profile_list_get_count': 1, 'ob_stream_profile_list_get_profile': 6,
                'ob_create_config': 9, 'ob_pipeline_wait_for_frameset': 7, 'ob_frameset_get_frame': 8,
                'ob_video_frame_get_width': 64, 'ob_video_frame_get_height': 64,
                'ob_frame_get_format': 22, 'ob_frame_get_data_size': 1 if self.short else 64 * 64 * 3,
                'ob_frame_get_data': ct.addressof(self.raw), 'ob_frame_get_timestamp_us': 1234,
                'ob_frame_get_system_timestamp_us': 5678}.get(name)


@pytest.mark.parametrize('short', [False, True])
def test_sdk_frame_ownership_and_cleanup_on_stop_or_conversion_error(monkeypatch, short):
    fake = FakeNative(short)
    monkeypatch.setattr(orbbec, 'NativeSDK', lambda root: fake)
    camera = dict(device='orbbec://ABC/color', width=64, height=64, fps=30, pixel_format='rgb24')
    frames = orbbec.camera_frames('/unused', camera, threading.Event())
    if short:
        with pytest.raises(ValueError, match='length'):
            next(frames)
    else:
        frame, stamp, metadata = next(frames)
        assert (frame.to_ndarray()[0, 0] == [16, 48, 112]).all()
        assert stamp > 0 and metadata['sdk_device_timestamp_us'] == 1234
        frames.close()
    assert not fake.handles
    assert fake.calls.count('ob_pipeline_stop') == 1


def test_sdk_capture_branch_to_independent_udp_receiver(tmp_path):
    """Simulated SDK only; real sender, H.264, UDP and separate receiver process."""
    root = Path(__file__).resolve().parents[1]
    receiver_dir = tmp_path / 'receiver'
    receiver = subprocess.Popen([sys.executable, str(root / 'demo.py'), 'receive', '--headless', '--duration', '15',
        '--bind', '127.0.0.1', '--port', '0', '--clock-mode', 'shared', '--decoder', 'software',
        '--width', '64', '--height', '64', '--fps', '15', '--output', str(receiver_dir)],
        cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    script = '''
import sys, time
from video_demo import sdk, orbbec
from video_demo.cli import main
import numpy as np
sdk.inventory = lambda: {'devices': [dict(name='SDK test depth', device='orbbec://FIXTURE/depth',
    formats=[dict(width=64, height=64, pixel_format='gray16le', frame_rates=[dict(min=15, max=15)])])]}
def frames(camera, stop):
    raw = np.full((64, 64), 2000, dtype='<u2').tobytes()
    while not stop.is_set():
        stamp = time.perf_counter_ns()
        frame = orbbec.convert_image(raw, 64, 64, 8, stream='depth', scale_mm=.1)
        yield frame, stamp, dict(sdk_device_timestamp_us=1234, sdk_system_timestamp_us=5678,
            sdk_stream='depth', sdk_format=8, depth_scale_mm=.1, depth_preview=True)
        stop.wait(1 / 15)
sdk.frames = frames
sys.exit(main(sys.argv[1:]))
'''
    try:
        ready = receiver_dir / 'ready.json'
        deadline = time.monotonic() + 8
        while not ready.exists() and receiver.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        assert ready.exists()
        port = json.loads(ready.read_text())['port']
        sender_dir = tmp_path / 'sender'
        result = subprocess.run([sys.executable, '-c', script, 'send', '--cameras', 'orbbec://FIXTURE/depth',
            '--width', '64', '--height', '64', '--fps', '15', '--port', str(port), '--duration', '2',
            '--output', str(sender_dir)], cwd=root, capture_output=True, text=True, timeout=12)
        assert result.returncode == 0, result.stdout + result.stderr
        (receiver_dir / 'STOP').touch()
        output, _ = receiver.communicate(timeout=8)
        assert receiver.returncode == 0, output
        events = [json.loads(line) for line in (receiver_dir / 'events.jsonl').read_text().splitlines()]
        decoded = [e for e in events if e['event'] == 'decode']
        assert len(decoded) >= 10 and all(e['depth_preview'] for e in decoded)
        report = json.loads((sender_dir / 'summary.json').read_text())
        assert report['streams']['0']['camera_input']['timestamp_origin'] == 'sdk_host_dequeue'
        assert report['streams']['0']['camera_input']['sdk_device_timestamp_us'] == 1234
        assert 'sdk_system_timestamp_us' in (sender_dir / 'camera_metrics.csv').read_text()
    finally:
        if receiver.poll() is None:
            receiver.terminate()
            receiver.communicate(timeout=10)
