import json

from video_demo.metrics import LiveStats, summarize


def test_rolling_stats_expire_bytes_and_frames_without_resetting_totals(monkeypatch):
    now = [1_000_000_000]
    monkeypatch.setattr('video_demo.metrics.time.perf_counter_ns', lambda: now[0])
    stats = LiveStats(1)
    for _ in range(20):
        stats.add(0, 'wire_bytes', 1200)
    stats.add(0, 'decoded', latency_ms=8)
    assert stats.snapshot()[0]['mbps'] == .192
    now[0] += 1_000_000_001
    stats.add(0, 'wire_bytes', 500)
    stats.add(0, 'presented')
    a = stats.snapshot()[0]
    assert a['fps'] == 0 and a['submit_fps'] == 1 and a['mbps'] == .004
    assert a['decoded'] == 1 and a['wire_bytes'] == 24500
    assert stats.snapshot() == [a]


def test_summary_keeps_stage_samples_and_visible_held_frame_skew(tmp_path):
    (tmp_path / 'config.json').write_text(json.dumps(dict(streams=1, command='receive')))
    rows = [dict(event='decode', time_ns=1, stream=0, latency_ms=140, encode_us=2000,
                 decoder='software', codec_decode_ms=3, rgb_convert_ms=1),
            dict(event='drop', time_ns=2, stream=0, reason='display_deadline_expired'),
            dict(event='visible_group', time_ns=3, complete=True, sync_skew_ms=55, stale_streams=[0])]
    (tmp_path / 'events.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))
    report = summarize(tmp_path)
    s = report['streams']['0']
    assert s['decode_over_100ms'] == 1 and s['render_submit_latency_ms']['samples'] == 0
    assert s['stages_ms']['codec_decode_ms']['p95'] == 3
    assert report['visible_groups']['capture_skew_ms']['p95'] == 55
    assert report['visible_groups']['with_stale_frames'] == 1


def test_sensor_latency_is_separate_from_application_latency_and_has_no_fake_fallback(tmp_path):
    (tmp_path / 'config.json').write_text(json.dumps(dict(streams=1, command='receive', ui_backend='webcodecs')))
    rows = [dict(event='browser_submit', time_ns=1, stream=0, sensor_status='ready',
                 latency_ms=10, sensor_latency_ms=40, sensor_clock_uncertainty_ms=2,
                 sensor_capture_ns=123, sdk_global_timestamp_us=456),
            dict(event='browser_submit', time_ns=2, stream=0, sensor_status='unsupported', latency_ms=12)]
    (tmp_path / 'events.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))
    report = summarize(tmp_path)
    stream = report['streams']['0']
    assert stream['sensor_to_browser_submit_ms']['samples'] == 1
    assert stream['sensor_to_browser_submit_ms']['p95'] == 40
    assert stream['browser_submit_latency_ms']['samples'] == 2
    assert stream['sensor_timestamp_states'] == {'ready': 1, 'unsupported': 1}
    assert stream['sensor_sdk_fit_error_ms'] is None
    assert report['glass_to_glass_latency_ms'] is None
    assert 'sdk_global_timestamp_us' in (tmp_path / 'frames.csv').read_text()
