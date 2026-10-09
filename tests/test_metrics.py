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


def test_summary_reports_webrtc_rate_control(tmp_path):
    (tmp_path / 'config.json').write_text(json.dumps(dict(streams=1, command='send')))
    rows = [dict(event='webrtc_feedback', time_ns=1, session='a', loss=0.0, rtt_ms=52.0, queue_ms=0.0,
                 target_kbps=10000, rtx_sent=3, rtx_old=0, rtx_repeat=1, rtx_budget=0),
            dict(event='webrtc_feedback', time_ns=2, session='a', loss=0.3, rtt_ms=640.0, queue_ms=588.0,
                 target_kbps=10000, rtx_sent=40, rtx_old=5, rtx_repeat=9, rtx_budget=2),
            dict(event='webrtc_feedback', time_ns=2, session='b', loss=0.0, rtt_ms=60.0, queue_ms=0.0,
                 target_kbps=10000, rtx_sent=1, rtx_old=0, rtx_repeat=0, rtx_budget=0),
            dict(event='webrtc_rate', time_ns=3, stream_kbps=2500.0, fps=60, reason='queue', total_kbps=5000),
            dict(event='webrtc_rate', time_ns=4, stream_kbps=1062.5, fps=30, reason='loss', total_kbps=2125),
            dict(event='encoder_rate', time_ns=5, stream=0, bitrate_kbps=2500.0, fps=60, reopen_ms=3),
            dict(event='encoder_rate', time_ns=6, stream=0, bitrate_kbps=1062.5, fps=30, reopen_ms=3)]
    (tmp_path / 'events.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))
    report = summarize(tmp_path)
    control = report['webrtc_rate_control']
    assert control['changes'] == 2 and control['reasons'] == {'queue': 1, 'loss': 1}
    assert control['min_stream_kbps'] == 1062.5 and control['min_fps'] == 30
    assert control['rtt_ms']['max'] == 640 and control['loss']['samples'] == 3
    assert control['retransmissions'] == dict(sent=41, old=5, repeat=9, budget=2)
    assert report['streams']['0']['encoder_rate'] == dict(changes=2, min_bitrate_kbps=1062.5,
                                                          last_bitrate_kbps=1062.5, min_fps=30, last_fps=30)
