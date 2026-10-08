"""Exercise actual separate processes, UDP, H.264 and telemetry end to end."""
import csv
import json
from pathlib import Path
import subprocess
import sys
import time
import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('sync_mode', ['aligned', 'latest'])
def test_two_stream_process_demo(tmp_path, sync_mode, two_rtsp_cameras):
    result = subprocess.run([
        sys.executable, str(ROOT / 'demo.py'), 'demo',
        '--rtsp-url', two_rtsp_cameras[0].url, '--rtsp-url', two_rtsp_cameras[1].url,
        '--duration', '3', '--save-preview',
        '--width', '320', '--height', '180', '--fps', '30',
        '--encoder', 'libx264', '--decoder', 'software', '--output', str(tmp_path),
        '--sync-mode', sync_mode,
    ], capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((tmp_path / 'report.json').read_text())
    assert report['glass_to_glass_latency_ms'] is None
    for delivery in report['delivery'].values():
        assert delivery['decoded'] >= 10
        assert delivery['decoded'] <= delivery['sent']
    assert (tmp_path / 'receiver' / 'preview.png').is_file()
    assert json.loads((tmp_path / 'receiver' / 'config.json').read_text())['headless'] is True
    receiver = report['results']['receiver']
    assert json.loads((tmp_path / 'receiver' / 'config.json').read_text())['sync_mode'] == sync_mode
    assert not receiver['errors'] and receiver['telemetry_lost_events'] == 0
    for stream in receiver['streams'].values():
        assert stream['decode_latency_ms']['samples'] == stream['events']['decode']
        assert stream['render_submit_latency_ms']['samples'] == 0
        assert stream['decode_latency_unknown'] == 0
        assert stream['stages_ms']['rgb_convert_ms']['samples'] == stream['events']['decode']
        assert stream['stages_ms']['match_wait_ms']['samples'] > 0
    if sync_mode == 'aligned':
        assert receiver['groups']['complete'] > 0
    with (tmp_path / 'receiver' / 'metrics.csv').open() as file:
        assert any(float(r['fps']) > 0 for r in csv.DictReader(file))
    with (tmp_path / 'receiver' / 'frames.csv').open() as file:
        assert any(r['event'] == 'decode' for r in csv.DictReader(file))


def test_estimated_clock_over_udp(tmp_path, rtsp_camera):
    receiver_path = tmp_path / 'receiver'
    receiver = subprocess.Popen([
        sys.executable, str(ROOT / 'demo.py'), 'receive',
        '--bind', '127.0.0.1', '--port', '0', '--streams', '1', '--fps', '30',
        '--width', '320', '--height', '180', '--decoder', 'software',
        '--clock-mode', 'estimated', '--output', str(receiver_path),
    ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        ready = receiver_path / 'ready.json'
        deadline = time.monotonic() + 10
        while not ready.exists() and receiver.poll() is None and time.monotonic() < deadline:
            time.sleep(.05)
        assert ready.exists(), 'Receiver did not become ready'
        port = json.loads(ready.read_text())['port']
        sender = subprocess.run([
            sys.executable, str(ROOT / 'demo.py'), 'send', '--rtsp-url', rtsp_camera.url, '--duration', '3',
            '--host', '127.0.0.1', '--port', str(port), '--streams', '1', '--fps', '30',
            '--width', '320', '--height', '180', '--encoder', 'libx264',
            '--output', str(tmp_path / 'sender'),
        ], capture_output=True, text=True, timeout=20)
        assert sender.returncode == 0, sender.stdout + sender.stderr
        (receiver_path / 'STOP').touch()
        output, _ = receiver.communicate(timeout=10)
        assert receiver.returncode == 0, output
        data = json.loads((receiver_path / 'summary.json').read_text())
        stats = data['streams']['0']
        # RTSP startup can finish after calibration, so early unknown samples are optional.
        assert stats['decode_latency_ms']['samples'] > 10
        assert stats['clock_uncertainty_ms']['p50'] > 0
        assert data['telemetry_lost_events'] == 0 and not data['errors']
    finally:
        if receiver.poll() is None:
            receiver.terminate()
            receiver.communicate(timeout=10)
