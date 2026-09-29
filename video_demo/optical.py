"""Optical scene-to-screen measurement from an external high-speed recording.

The original scene indicator and its received image must be visible together.
We analyze actual recorded pixels, not sender/receiver application timestamps.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
import time

import av
import numpy as np

from .metrics import distribution


def roi(value, width, height):
    try:
        x, y, w, h = (int(n) for n in value.split(','))
    except (ValueError, AttributeError):
        raise ValueError('ROI must be x,y,width,height in decoded video pixels') from None
    if x < 0 or y < 0 or w < 2 or h < 2 or x + w > width or y + h > height:
        raise ValueError(f'ROI outside {width}x{height} frame')
    return x, y, w, h


def select_rois(rgb):
    from PySide6 import QtCore, QtGui, QtWidgets
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(['Optical delay measurement'])
    dialog = QtWidgets.QDialog()
    dialog.setWindowTitle('选择光学测量区域：原始指示灯 → 屏幕中的指示灯')
    layout = QtWidgets.QVBoxLayout(dialog)
    label = QtWidgets.QLabel('先框选真实 LED/指示区域，再框选接收屏幕内对应区域；两处尽量处于同一水平线。')
    layout.addWidget(label)
    rectangles = []

    class Canvas(QtWidgets.QWidget):
        start = None
        current = None

        def __init__(self):
            super().__init__()
            scale = min(1, 1100 / rgb.shape[1], 650 / rgb.shape[0])
            self.setFixedSize(int(rgb.shape[1] * scale), int(rgb.shape[0] * scale))
            image = QtGui.QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0], QtGui.QImage.Format.Format_RGB888)
            self.pixmap = QtGui.QPixmap.fromImage(image).scaled(self.size())

        def paintEvent(self, _):
            p = QtGui.QPainter(self)
            p.drawPixmap(0, 0, self.pixmap)
            for i, rect in enumerate(rectangles + ([self.current] if self.current else [])):
                p.setPen(QtGui.QPen(QtGui.QColor('#54edb9' if i == 0 else '#ffbd69'), 2))
                p.drawRect(rect)

        def mousePressEvent(self, e):
            if len(rectangles) >= 2:
                rectangles.clear()
            self.start = e.position().toPoint()

        def mouseMoveEvent(self, e):
            if self.start is not None:
                self.current = QtCore.QRect(self.start, e.position().toPoint()).normalized().intersected(self.rect())
                self.update()

        def mouseReleaseEvent(self, e):
            self.mouseMoveEvent(e)
            if self.current and self.current.width() >= 2 and self.current.height() >= 2:
                rectangles.append(self.current)
            self.start = self.current = None
            label.setText('已选原始指示灯，请选择屏幕内对应区域。' if len(rectangles) == 1 else
                          '已选两处区域，确认后开始分析；重新拖动可重选。')
            self.update()

    canvas = Canvas()
    layout.addWidget(canvas)
    buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok |
                                        QtWidgets.QDialogButtonBox.StandardButton.Cancel)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted or len(rectangles) != 2:
        raise ValueError('Select exactly two ROIs, or provide --source-roi and --screen-roi')
    sx, sy = rgb.shape[1] / canvas.width(), rgb.shape[0] / canvas.height()
    return [f'{int(r.x()*sx)},{int(r.y()*sy)},{int(r.width()*sx)},{int(r.height()*sy)}' for r in rectangles]


def transitions(values, settle=2, min_contrast=15):
    """50% edge crossing confirmed by a stable high/low region, without adding debounce delay."""
    values = np.asarray(values, dtype=float)
    if len(values) < 4:
        raise ValueError('Recording too short')
    low, high = np.percentile(values, [10, 90])
    if high - low < min_contrast:
        raise ValueError('ROI contrast too low; use a clear LED/black-white indicator and several on/off cycles')
    norm = (values - low) / (high - low)
    state = int(norm[0] >= .5)
    pending = None
    stable = 0
    events = []
    for index in range(1, len(norm)):
        new_state = int(norm[index] >= .5)
        if new_state == state:
            pending, stable = None, 0
            continue
        if pending is None:
            pending = index
        confident = norm[index] >= .8 if new_state else norm[index] <= .2
        stable = stable + 1 if confident else 0
        if stable >= settle:
            events.append({'frame': pending, 'polarity': 'rising' if new_state else 'falling'})
            state = new_state
            pending, stable = None, 0
    return events, {'low_luminance': float(low), 'high_luminance': float(high),
                    'threshold_luminance': float((low + high) / 2)}


def match_events(source, screen, capture_fps, max_delay_ms):
    if not np.isfinite(capture_fps) or capture_fps <= 0 or not np.isfinite(max_delay_ms) or max_delay_ms <= 0:
        raise ValueError('Physical capture FPS and max delay must be positive finite values')
    period = 1000 / capture_fps
    candidates = {}
    for i, a in enumerate(source):
        candidates[i] = [j for j, b in enumerate(screen)
                         if a['polarity'] == b['polarity'] and
                         0 <= (b['frame'] - a['frame']) * period <= max_delay_ms]
    matches, used = [], set()
    for i, indices in candidates.items():
        if len(indices) != 1:
            continue
        j = indices[0]
        if sum(j in other for other in candidates.values()) != 1:
            continue  # Ambiguous repeating flashes are not silently paired.
        a, b = source[i], screen[j]
        delay = (b['frame'] - a['frame']) * period
        matches.append({'source_frame': a['frame'], 'screen_frame': b['frame'],
                        'polarity': a['polarity'], 'delay_ms': delay,
                        'sampling_lower_ms': max(0, delay - period), 'sampling_upper_ms': delay + period})
        used.add(j)
    return matches, len(source) - len(matches), len(screen) - len(used)


def run_optical(args):
    if not np.isfinite(args.capture_fps) or not 10 <= args.capture_fps <= 2000:
        raise ValueError('--capture-fps must be the actual recording rate (10..2000)')
    if not np.isfinite(args.max_delay_ms) or not 1 <= args.max_delay_ms <= 10000:
        raise ValueError('--max-delay-ms must be 1..10000')
    directory = Path(args.output or f'runs/optical-{time.strftime("%Y%m%d-%H%M%S")}').resolve()
    if directory.exists() and any(directory.iterdir()):
        raise ValueError('Choose an empty optical result directory')
    source_values, screen_values, pts_values, time_quanta = [], [], [], []
    with av.open(args.video) as container:
        rate = str(container.streams.video[0].average_rate)
        for index, frame in enumerate(container.decode(video=0)):
            if index == 0:
                if args.select_rois:
                    args.source_roi, args.screen_roi = select_rois(frame.to_ndarray(format='rgb24'))
                source_roi = roi(args.source_roi, frame.width, frame.height)
                screen_roi = roi(args.screen_roi, frame.width, frame.height)
                shape = (frame.width, frame.height)
                x, y, w, h = source_roi
                a, b, c, d = screen_roi
                if max(x, a) < min(x + w, a + c) and max(y, b) < min(y + h, b + d):
                    raise ValueError('Source and screen ROIs must not overlap')
            if (frame.width, frame.height) != shape:
                raise ValueError('Video dimensions changed during recording')
            gray = frame.to_ndarray(format='gray')
            for region, values in [(source_roi, source_values), (screen_roi, screen_values)]:
                x, y, w, h = region
                values.append(float(gray[y:y+h, x:x+w].mean()))
            if frame.pts is not None and frame.time_base is not None:
                pts_values.append(float(frame.pts * frame.time_base))
                time_quanta.append(float(frame.time_base))
    if len(source_values) < 4:
        raise ValueError('No usable video frames')
    # Mixed-speed exports / missing frames make index / physical FPS invalid.
    if len(pts_values) == len(source_values):
        steps = np.diff(pts_values)
        median = np.median(steps)
        tolerance = max(median * .05, 1.5 * max(time_quanta))
        if median <= 0 or np.any(steps <= 0) or np.any(np.abs(steps - median) > tolerance):
            raise ValueError('Nonuniform video timestamps: use an original constant-rate high-speed recording without speed edits or dropped frames')
    source_edges, source_levels = transitions(source_values)
    screen_edges, screen_levels = transitions(screen_values)
    matched, source_unmatched, screen_unmatched = match_events(source_edges, screen_edges, args.capture_fps, args.max_delay_ms)
    report = {
        'measurement': 'optical scene-to-screen indicator transition (50% luminance crossing)',
        'input': str(Path(args.video).resolve()), 'physical_capture_fps': args.capture_fps,
        'container_playback_fps': rate, 'frames': len(source_values),
        'timebase': 'frame index / user-confirmed physical recording FPS; assumes no frame interpolation/duplication/deletion',
        'source_roi': source_roi, 'screen_roi': screen_roi,
        'source_levels': source_levels, 'screen_levels': screen_levels,
        'source_events': len(source_edges), 'screen_events': len(screen_edges),
        'matched_events': len(matched), 'unmatched_source_events': source_unmatched,
        'unmatched_screen_events': screen_unmatched,
        'match_ratio': len(matched) / len(source_edges) if source_edges else None,
        'delay_ms': distribution([m['delay_ms'] for m in matched]),
        'sampling_half_interval_ms': 1000 / args.capture_fps,
        'observed_over_100ms': sum(m['delay_ms'] > 100 for m in matched),
        'upper_sampling_bound_over_100ms': sum(m['sampling_upper_ms'] > 100 for m in matched),
        'acceptance_status': 'review_required',
        'limitations': ['Sampling bounds exclude recorder exposure/rolling-shutter and threshold bias.',
                       'Place both ROIs at the same recorder scanline; use short recorder exposure.',
                       'Review unmatched and ambiguous events; successful matches alone are not an acceptance pass.',
                       'Manual capture FPS must reflect real sensor rate, not slow-motion playback rate.'],
    }
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'luminance.csv').open('w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['frame', 'physical_time_ms', 'source_luminance', 'screen_luminance'])
        for i, (a, b) in enumerate(zip(source_values, screen_values)):
            writer.writerow([i, i * 1000 / args.capture_fps, a, b])
    with (directory / 'matches.csv').open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['source_frame', 'screen_frame', 'polarity', 'delay_ms',
                                             'sampling_lower_ms', 'sampling_upper_ms'])
        writer.writeheader()
        writer.writerows(matched)
    (directory / 'measurement.json').write_text(json.dumps(report, indent=2, ensure_ascii=False))
    (directory / 'events.json').write_text(json.dumps({'source': source_edges, 'screen': screen_edges}, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f'Optical results: {directory}')
    return 0 if matched else 1
