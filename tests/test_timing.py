from video_demo.protocol import Meta
from video_demo.timing import ClockMap, Decoded, DisplayScheduler, FrameMatcher, frame_expired
import pytest


def frame(stream, fid, capture_ns, arrival_ns=0, epoch=1):
    return Decoded(Meta(stream, epoch, fid, capture_ns), None, arrival_ns or capture_ns,
                   capture_ns, 1, 0, 'software')


def test_clock_estimate_and_expiry():
    c = ClockMap()
    # Sender clock is +50 ms; network 2 ms each way, processing 1 ms.
    for i in range(3):
        t = 1_000_000_000 + i * 10_000_000
        assert c.update(t, t + 52_000_000, t + 53_000_000, t + 5_000_000)
    offset, error = c.estimate(t + 5_000_000)
    assert offset == 50_000_000
    assert error == 2
    assert c.estimate(t + 6_000_000_000) == (None, None)
    assert not c.update(10, 20, 19, 30)


def test_network_rtt_is_latest_round_trip_not_best_clock_error():
    c = ClockMap()
    t = 1_000_000_000
    for i in range(3):
        start = t + i * 10_000_000
        c.update(start, start + 52_000_000, start + 53_000_000, start + 5_000_000)
    assert c.network_rtt(start + 5_000_000) == 4  # 5ms total minus 1ms processing.
    assert c.estimate(start + 5_000_000) == (50_000_000, 2)
    start = t + 100_000_000
    c.update(start, start + 56_000_000, start + 57_000_000, start + 13_000_000)
    assert c.network_rtt(start + 13_000_000) == 12
    assert c.estimate(start + 13_000_000) == (50_000_000, 2)
    assert not c.update(start, start + 250_000_000, start + 251_000_000, start + 501_000_000)
    assert c.network_rtt(start + 501_000_000) == 500
    assert c.network_rtt(start + 5_501_000_000) is None
    c.clear()
    assert c.network_rtt(start + 502_000_000) is None
    assert c.estimate(start + 502_000_000) == (None, None)


def test_shared_clock_still_measures_real_rtt_and_rejects_bad_samples():
    c = ClockMap(shared=True)
    assert c.network_rtt(1_000_000_000) is None
    assert c.estimate(1_000_000_000) == (0, 0)
    for times in [(True, 2, 3, 4), (100, 110, 109, 120), (100, 101, 121, 110),
                  (100, 110, 111, 99), (1, 2, 3, 3_000_000_000)]:
        assert not c.update(*times)
        assert c.network_rtt(4_000_000_000) is None
    c.update(1_000_000_000, 1_001_000_000, 1_003_000_000, 1_004_000_000)
    assert c.network_rtt(1_004_000_000) == 2


def test_full_group_no_frame_reuse():
    m = FrameMatcher(2, tolerance_ms=2)
    m.add(frame(0, 1, 1_000_000_000))
    m.add(frame(1, 1, 1_001_000_000))
    chosen, full, skew = m.poll(1_003_000_000)
    assert full and skew == 1 and len(chosen) == 2
    assert m.poll(1_004_000_000) is None


def test_missing_camera_cannot_block_others():
    m = FrameMatcher(2, wait_ms=5)
    m.add(frame(0, 1, 1_000_000_000))
    assert m.poll(1_001_000_000) is None
    chosen, full, skew = m.poll(1_007_000_000)
    assert not full and list(chosen) == [0] and skew is None


def test_strict_sync_discards_partial_group():
    m = FrameMatcher(2, wait_ms=0, strict=True)
    m.add(frame(0, 1, 1_000_000_000))
    chosen, full, _ = m.poll(1_001_000_000)
    assert not full and not chosen
    assert m.dropped == 1


def test_superseded_frames_are_counted():
    m = FrameMatcher(1)
    m.add(frame(0, 1, 1_000_000_000))
    m.add(frame(0, 2, 1_010_000_000))
    chosen, full, _ = m.poll(1_012_000_000)
    assert full and chosen[0].meta.frame_id == 2
    assert m.dropped == 1


def test_expired_frames_and_new_epoch():
    m = FrameMatcher(1, max_age_ms=10)
    m.add(frame(0, 1, 1_000_000_000))
    assert m.poll(1_020_000_000) is None
    assert m.dropped == 1
    m.add(frame(0, 100, 2_000_000_000, epoch=1))
    m.add(frame(0, 0, 2_001_000_000, epoch=2))
    chosen, _, _ = m.poll(2_003_000_000)
    assert chosen[0].meta.epoch == 2


def test_three_stream_pairwise_skew_cannot_exceed_tolerance():
    m = FrameMatcher(3, tolerance_ms=5, wait_ms=0)
    for i, ms in enumerate((0, 5, 10)):
        m.add(frame(i, 1, 1_000_000_000 + ms * 1_000_000))
    chosen, full, skew = m.poll(1_012_000_000)
    assert not full


def test_latest_delivers_without_waiting_for_slow_camera_and_never_reuses():
    m = FrameMatcher(3, wait_ms=20, mode='latest')
    m.add(frame(0, 1, 1_000_000_000))
    m.add(frame(0, 2, 1_010_000_000))
    m.add(frame(2, 9, 1_009_000_000))
    chosen, full, skew = m.poll(1_011_000_000)
    assert {i: f.meta.frame_id for i, f in chosen.items()} == {0: 2, 2: 9}
    assert not full and skew == 1 and m.dropped == 1
    assert m.poll(1_012_000_000) is None
    with pytest.raises(ValueError):
        FrameMatcher(2, mode='latest', strict=True)


def test_display_idle_refresh_does_not_delay_first_frame_but_caps_busy_submissions():
    s = DisplayScheduler(60)
    t = 1_000_000_000
    assert s.due(t, False)
    s.submitted(t, False)
    assert s.due(t + 1, True)
    s.submitted(t + 1, True)
    assert not s.due(t + 5_000_000, True)
    assert s.due(t + 17_000_000, True)
    assert not s.due(t + 17_000_000, False)
    assert s.due(t + 201_000_000, False)


def test_deadline_rechecked_after_matching_and_uses_decode_time_without_clock():
    f = frame(0, 1, 1_000_000_000, arrival_ns=1_040_000_000)
    assert not frame_expired(f, 1_099_000_000, 100_000_000)
    assert frame_expired(f, 1_101_000_000, 100_000_000)
    f.local_capture_ns = None
    assert not frame_expired(f, 1_101_000_000, 100_000_000)
    assert frame_expired(f, 1_141_000_000, 100_000_000)
