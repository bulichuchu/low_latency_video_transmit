import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from video_demo.cameras import configure_camera_inputs, read_profile
from video_demo.cli import parser, validate
from video_demo.network import camera_display_names, child_camera_profile, network_frames, redact, rtsp_options, validate_network_camera
from rtsp_server import RtspCamera

ROOT = Path(__file__).resolve().parents[1]


def test_camera_display_names_keep_order_and_never_expose_rtsp_credentials():
    names = camera_display_names([
        {'device': '0', 'label': '  前门相机  '},
        {'device': 'MacBook Air的相机'},
        {'device': 'rtsp://admin:secret@192.168.1.20/private?token=secret'},
        {'device': '3'},
        {'device': 'sdk://device', 'label': 'Orbbec 彩色'},
        {'device': '0', 'label': 'rtsp://admin:secret@host/private?token=secret'},
    ])
    assert names[:5] == ['前门相机', 'MacBook Air的相机', '网络摄像头 · 192.168.1.20', '摄像头 4', 'Orbbec 彩色']
    assert not any('secret' in name or 'admin' in name or 'private' in name for name in names)
    assert len(camera_display_names([{'device': '0', 'label': '长' * 500}])[0]) == 120


def test_rtsp_only_configuration_does_not_enumerate_local_devices(tmp_path, monkeypatch):
    def forbidden(**kw):
        raise AssertionError('Network cameras must not query local device capabilities')
    monkeypatch.setattr('video_demo.cameras.inventory', forbidden)
    p = parser()
    args = p.parse_args(['send', '--rtsp-url', 'rtsp://127.0.0.1/first,a',
                        '--rtsp-url', 'rtsp://127.0.0.1/second', '--output', str(tmp_path)])
    validate(p, args)
    configure_camera_inputs(args)
    assert args.streams == 2
    assert args.camera_settings[0] == {'device': 'rtsp://127.0.0.1/first,a'}
    assert all(m['status'] == 'rtsp_camera_managed_mode' for m in args.camera_modes)


def test_mixed_inputs_and_transport_override(tmp_path):
    p = parser()
    args = p.parse_args(['send', '--cameras', '0', '--rtsp-url', 'rtsp://host/stream',
                        '--rtsp-transport', 'udp', '--capture-format', 'nv12', '--output', str(tmp_path)])
    validate(p, args)
    assert args.camera_settings == [{'device': '0', 'pixel_format': 'nv12'},
        {'device': 'rtsp://host/stream', 'rtsp_transport': 'udp'}]


@pytest.mark.parametrize('settings', [
    {'device': 'http://host/video'}, {'device': 'rtsp://'}, {'device': 'rtsp://host:99999/a'},
    {'device': 'rtsp://host/a#fragment'}, {'device': 'rtsp://host/a', 'read_timeout_s': float('nan')},
    {'device': 'rtsp://host/a', 'rtsp_transport': 'invalid'}, {'device': 'rtsp://host/a', 'fps': 30},
])
def test_invalid_network_settings_rejected(settings):
    with pytest.raises(ValueError):
        validate_network_camera(settings)


def test_profile_credentials_roundtrip_without_saved_secret(tmp_path, monkeypatch):
    raw = 'rtsp://viewer:private-password@host/path-token?key=private-query'
    camera = {'device': raw, 'rtsp_transport': 'udp', 'read_timeout_s': .5}
    profile, env = child_camera_profile([camera, {'device': '0'}])
    path = tmp_path / 'profile.json'
    path.write_text(json.dumps(profile))
    assert raw not in path.read_text() and 'private-password' not in path.read_text()
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert read_profile(path)['cameras'] == [camera, {'device': '0'}]
    safe = json.dumps(redact({'nested': [raw], 'error': f'Failed: {raw}'}))
    assert all(secret not in safe for secret in ['viewer', 'private-password', 'path-token', 'private-query'])
    assert 'host' in safe
    assert redact(redact(raw)) == redact(raw)
    assert 'password' not in redact("URL: rtsp://viewer:private'password@host/stream")
    monkeypatch.delenv(next(iter(env)))
    with pytest.raises(ValueError, match='missing or empty'):
        read_profile(path)


def test_rtsp_options_do_not_request_capture_device_size_or_rate():
    options = rtsp_options({'rtsp_transport': 'udp'})
    assert options['rtsp_transport'] == 'udp' and options['allowed_media_types'] == 'video'
    assert not {'video_size', 'framerate', 'pixel_format'} & options.keys()
    assert options['max_delay'] == '20000'
    assert rtsp_options({'device': 'rtsps://host/video'})['tls_verify'] == '1'


@pytest.mark.parametrize('transport', ['tcp', 'udp'])
def test_real_rtsp_to_udp_demo_and_tcp_reconnect(tmp_path, transport):
    with RtspCamera(credentials='viewer:fixture-secret-z91', disconnect_once=transport == 'tcp') as camera:
        result = subprocess.run([
            sys.executable, str(ROOT / 'demo.py'), 'demo', '--rtsp-url', camera.url + '?token=fixture-query-z91',
            '--rtsp-transport', transport, '--headless', '--duration', '5', '--sync-mode', 'latest',
            '--width', '320', '--height', '180', '--encoder', 'libx264', '--decoder', 'software',
            '--output', str(tmp_path)], capture_output=True, text=True, timeout=35)
        assert result.returncode == 0, result.stdout + result.stderr
        assert not camera.failures
    report = json.loads((tmp_path / 'report.json').read_text())
    assert report['delivery']['0']['decoded'] >= 20
    assert report['glass_to_glass_latency_ms'] is None
    sender = report['results']['sender']['streams']['0']
    assert sender['camera_input']['timestamp_origin'] == 'network_decode'
    assert sender['events']['network_connected'] >= (2 if transport == 'tcp' else 1)
    if transport == 'tcp':
        assert sender['events']['network_disconnected'] >= 1
    assert not report['results']['sender']['errors']
    combined = result.stdout + result.stderr
    for path in tmp_path.rglob('*'):
        if path.is_file():
            combined += path.read_text()
    assert 'fixture-secret-z91' not in combined and 'fixture-query-z91' not in combined


def test_stalled_rtsp_is_interruptible_and_retries():
    events = []
    stop = threading.Event()
    timer = threading.Timer(1.5, stop.set)
    with RtspCamera(stall=True) as camera:
        timer.start()
        start = time.monotonic()
        try:
            frames = list(network_frames({'device': camera.url, 'open_timeout_s': .3,
                'read_timeout_s': .3, 'retry_delay_s': .1}, stop,
                lambda kind, **fields: events.append(kind)))
        finally:
            stop.set()
            timer.cancel()
        assert time.monotonic() - start < 4
        assert not frames
        assert events.count('network_connecting') >= 2
        assert 'network_disconnected' in events


def test_two_network_cameras_use_independent_connections(tmp_path):
    with RtspCamera() as first, RtspCamera() as second:
        result = subprocess.run([sys.executable, str(ROOT / 'demo.py'), 'demo',
            '--rtsp-url', first.url, '--rtsp-url', second.url, '--headless', '--duration', '3',
            '--width', '320', '--height', '180', '--decoder', 'software',
            '--output', str(tmp_path)], capture_output=True, text=True, timeout=25)
        assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((tmp_path / 'report.json').read_text())
    assert len(report['delivery']) == 2
    assert all(s['decoded'] > 10 for s in report['delivery'].values())
