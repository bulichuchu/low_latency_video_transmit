from video_demo.capture_time import GlobalTimestampMap, sensor_latency_fields
from video_demo.protocol import Meta
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
