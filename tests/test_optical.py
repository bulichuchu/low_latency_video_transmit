from argparse import Namespace
from fractions import Fraction
import json

import av
import numpy as np
import pytest

from video_demo.optical import match_events, roi, run_optical, transitions


def test_known_physical_delay_and_sampling_bounds():
    original = np.tile(np.r_[np.zeros(80), np.ones(100)], 4)
    screen = np.r_[np.zeros(18), original[:-18]]
    a, _ = transitions(original * 180 + 20)
    b, _ = transitions(screen * 140 + 40)
    matches, missing_a, missing_b = match_events(a, b, 240, 250)
    assert len(matches) == 7 and missing_a == missing_b == 0
    assert all(m['delay_ms'] == 75 for m in matches)
    assert matches[0]['sampling_lower_ms'] == pytest.approx(70.833333)
    assert matches[0]['sampling_upper_ms'] == pytest.approx(79.166667)


def test_missing_or_ambiguous_events_not_silently_matched():
    a = [{'frame': 10, 'polarity': 'rising'}, {'frame': 12, 'polarity': 'rising'}]
    b = [{'frame': 20, 'polarity': 'rising'}]
    matches, missing_a, missing_b = match_events(a, b, 240, 500)
    assert matches == [] and missing_a == 2 and missing_b == 1


def test_noisy_low_contrast_and_invalid_roi():
    with pytest.raises(ValueError, match='contrast'):
        transitions(np.arange(20) % 4 + 100)
    with pytest.raises(ValueError, match='outside'):
        roi('90,0,20,20', 100, 100)


def test_recording_playback_rate_is_not_physical_capture_rate(tmp_path):
    # Lossless test fixture only: 240fps capture exported at 30fps playback.
    # This is not a camera benchmark or a replacement for the user's real footage.
    path = tmp_path / 'slow-motion-fixture.mkv'
    with av.open(str(path), 'w') as container:
        stream = container.add_stream('ffv1', rate=30)
        stream.width, stream.height, stream.pix_fmt = 128, 64, 'gray'
        for i in range(360):
            image = np.zeros((64, 128), dtype=np.uint8)
            image[:, :64] = 220 if 80 <= i < 220 else 20
            image[:, 64:] = 220 if 98 <= i < 238 else 20
            frame = av.VideoFrame.from_ndarray(image, format='gray')
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    args = Namespace(video=str(path), capture_fps=240, max_delay_ms=500,
                     source_roi='10,10,30,30', screen_roi='80,10,30,30',
                     select_rois=False, output=str(tmp_path / 'result'))
    assert run_optical(args) == 0
    report = json.loads((tmp_path / 'result/measurement.json').read_text())
    assert report['delay_ms']['p95'] == 75
    assert report['matched_events'] == 2
    assert report['acceptance_status'] == 'review_required'
