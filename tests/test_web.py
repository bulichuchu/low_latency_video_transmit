import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer
import av
import pytest

from video_demo.protocol import Meta
from video_demo.timing import ClockMap
from video_demo.webapp import Session, create_app, media_args
from video_demo.webbridge import BrowserBridge


def test_web_validation_and_camera_secrets(tmp_path):
    args = media_args('sender', {'cameras': [{'device': 'rtsp://user:secret@127.0.0.1/live'}]}, tmp_path)
    assert args.source == 'camera' and args.streams == 1
    assert 'secret' in args.camera_settings[0]['device']
    assert not list(tmp_path.iterdir())  # Validation doesn't persist credentials.
    for data in ({'width': 333}, {'fps': -1}, {'encoder': 'bad'}, {'bitrate_kbps': 2}, {'host': []}):
        with pytest.raises(ValueError):
            media_args('sender', {**data, 'source': 'synthetic'}, tmp_path)
    with pytest.raises(ValueError):
        media_args('sender', {'cameras': []}, tmp_path)
    with pytest.raises(ValueError):
        media_args('receiver', {'sync_wait_ms': float('nan')}, tmp_path)


def test_web_bridge_bounded_recovery_and_clock():
    bridge = BrowserBridge(1)
    keys = []
    bridge.request_key = keys.append
    bridge.attach(True)
    def unit(n, key=False):
        return SimpleNamespace(meta=Meta(0, 22, n, 123_000_000, 2, key=key), bitstream=b'bytes')
    clock = ClockMap(shared=True)
    bridge.offer(unit(0), clock)
    assert bridge.pop() is None
    bridge.offer(unit(1, True), clock)
    bridge.offer(unit(2), clock)
    bridge.offer(unit(3), clock)
    bridge.offer(unit(4), clock)
    assert len(bridge.queue) == 0 and bridge.drops == 1
    bridge.offer(unit(5, True), clock)
    packet = bridge.pop()
    size = int.from_bytes(packet[:4], 'big')
    header = json.loads(packet[4:4 + size])
    assert header['host_capture_ms'] == 123 and header['frame_id'] == 5
    assert packet[4 + size:] == b'bytes'
    bridge.attach(False)
    bridge.offer(unit(6, True), clock)
    assert not bridge.queue and len(keys) >= 2


def test_sender_fps_uses_flushed_event_window_and_expires_on_outage(tmp_path, monkeypatch):
    now = [3_800_000_000]
    monkeypatch.setattr('video_demo.webapp.time.perf_counter_ns', lambda: now[0])
    session = Session('sender', SimpleNamespace(output=str(tmp_path), streams=2))
    rows = [dict(event='tx', time_ns=1_000_000_000 + round(n * 1e9 / 60),
                 stream=s, wire_bytes=1000) for n in range(121) for s in range(2)]
    (tmp_path / 'events.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))
    for sample in session.snapshot()['samples']:
        assert sample['fps'] == 60
        assert sample['mbps'] == .48
        assert sample['frames'] == 121
        assert sample['sample_lag_ms'] == 800
    now[0] += 300_000_000
    assert session.snapshot()['samples'][0]['fps'] == 60
    now[0] += 1_000_000_000
    assert session.snapshot()['samples'][0]['fps'] == 0


def test_browser_recovery_records_reason_without_affecting_other_stream():
    bridge = BrowserBridge(2)
    events, keys = [], []
    bridge.journal = SimpleNamespace(log=lambda kind, **fields: events.append((kind, fields)))
    bridge.request_key = keys.append
    bridge.waiting.clear()
    bridge.browser_recover(0, 'frame_gap')
    assert bridge.waiting == {0} and keys == [0]
    assert events == [('browser_recovery', dict(stream=0, reason='frame_gap'))]
    bridge.browser_recover(True, 'frame_gap')
    bridge.browser_recover(2, 'frame_gap')
    assert len(events) == 1
    bridge.browser_recover(1, ['invalid'])
    assert events[-1][1]['reason'] == 'unspecified'


def test_web_http_and_actual_h264_lifecycle(tmp_path):
    async def exercise():
        client = TestClient(TestServer(create_app(tmp_path)))
        await client.start_server()
        ws = None
        try:
            bootstrap = await (await client.get('/api/bootstrap')).json()
            headers = {'X-Video-Token': bootstrap['token']}
            response = await client.post('/api/sender/start', json={'source': 'synthetic'})
            assert response.status == 403
            response = await client.get('/api/bootstrap', headers={'Host': 'evil.example'})
            assert response.status == 403
            response = await client.post('/api/receiver/start', json={}, headers={**headers, 'Origin': 'https://evil.example'})
            assert response.status == 403
            async def post(path, body):
                response = await client.post('/api' + path, json=body, headers=headers)
                result = await response.json()
                assert response.status == 200, result
                return result
            rx = await post('/receiver/start', dict(streams=2, width=320, height=180, fps=30,
                bind='127.0.0.1', port=0, clock_mode='shared', sync_mode='latest'))
            ready = json.loads((Path(rx['directory']) / 'ready.json').read_text())
            ws = await client.ws_connect('/api/video?token=' + bootstrap['token'])
            assert (await ws.receive_json())['type'] == 'config'
            await post('/sender/start', dict(source='synthetic', streams=2, width=320, height=180,
                fps=30, encoder='libx264', host='127.0.0.1', port=ready['port']))
            decoders = [av.CodecContext.create('h264', 'r') for _ in range(2)]
            counts = [0, 0]
            last = None
            deadline = asyncio.get_running_loop().time() + 10
            while min(counts) < 12 and asyncio.get_running_loop().time() < deadline:
                message = await ws.receive(timeout=5)
                assert message.type == WSMsgType.BINARY
                data = message.data
                length = int.from_bytes(data[:4], 'big')
                meta = json.loads(data[4:4 + length])
                counts[meta['stream']] += len(decoders[meta['stream']].decode(av.Packet(data[4 + length:])))
                last = meta
            assert min(counts) >= 12
            await ws.send_json(dict(type='telemetry', frames=[dict(stream=last['stream'], epoch=last['epoch'], frame_id=last['frame_id'],
                latency_ms=12, browser_decode_ms=3, browser_submit_ms=1000, browser_wait_ms=4, browser_draw_ms=1)]))
            # Exercise a new viewer joining a running sender: keyframe recovery.
            await ws.close()
            await asyncio.sleep(.1)
            ws = await client.ws_connect('/api/video?token=' + bootstrap['token'])
            await ws.receive_json()
            message = await ws.receive(timeout=5)
            length = int.from_bytes(message.data[:4], 'big')
            meta = json.loads(message.data[4:4 + length])
            assert meta['key']
            await ws.close()
            await post('/sender/stop', {})
            stopped = await post('/receiver/stop', {})
            assert stopped['state'] == 'stopped' and stopped['report_ready']
            report = await (await client.get('/api/receiver/report')).json()
            assert not report['errors']
            assert sum(s['events'].get('browser_submit', 0) for s in report['streams'].values()) == 1
            assert all(s['decode_latency_ms']['samples'] == 0 for s in report['streams'].values())
            # Start a second receiver session after all sockets/threads have stopped.
            again = await post('/receiver/start', dict(bind='127.0.0.1', port=ready['port']))
            assert again['directory'] != rx['directory']
            await post('/receiver/stop', {})
        finally:
            if ws:
                await ws.close()
            await client.close()
    asyncio.run(exercise())
