"""Exercise actual Qt painting and RGB ownership without opening a camera."""
import os
from pathlib import Path
import subprocess
import sys


def test_video_surface_keeps_pixels_and_aspect_ratio_after_resize():
    script = '''
import gc
from types import SimpleNamespace
import numpy as np
from video_demo.ui import Window
w = Window(lambda: None)
surface = w.VideoSurface()
surface.resize(320, 240)
surface.show()
surface.set_frame(SimpleNamespace(image=np.full((90, 160, 3), [220, 30, 70], dtype=np.uint8)))
gc.collect()
w.pump()
image = surface.grab().toImage()
assert image.pixelColor(160, 120).getRgb()[:3] == (220, 30, 70)
assert image.pixelColor(160, 1).getRgb()[:3] == (16, 25, 39)
surface.resize(160, 90)
surface.set_frame(SimpleNamespace(image=np.full((90, 160, 3), [20, 180, 90], dtype=np.uint8)))
gc.collect()
w.pump()
assert surface.grab().toImage().pixelColor(80, 45).getRgb()[:3] == (20, 180, 90)
surface.close()
w.destroy()
'''
    result = subprocess.run([sys.executable, '-c', script],
        cwd=Path(__file__).resolve().parents[1], env=dict(os.environ, QT_QPA_PLATFORM='offscreen'),
        capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_camera_picker_accepts_rtsp_without_local_camera():
    script = '''
from PySide6 import QtCore, QtWidgets
from video_demo import camera_settings
app = QtWidgets.QApplication([])
camera_settings.inventory = lambda **kw: {'devices': []}
def choose():
    dialog = app.activeModalWidget()
    box = dialog.findChild(QtWidgets.QPlainTextEdit, 'rtspUrls')
    box.setPlainText('rtsp://host/a,b\\nrtsp://host/second')
    dialog.accept()
QtCore.QTimer.singleShot(50, choose)
profile = camera_settings.select_camera_profile(dict(width=1280, height=720, fps=30, bitrate_kbps=3000))
assert profile['cameras'] == [{'device': 'rtsp://host/a,b'}, {'device': 'rtsp://host/second'}]
assert profile['output']['fps'] == 30
'''
    result = subprocess.run([sys.executable, '-c', script],
        cwd=Path(__file__).resolve().parents[1], env=dict(os.environ, QT_QPA_PLATFORM='offscreen'),
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_camera_quality_controls_validate_modes_and_keep_each_input_independent():
    script = '''
import time
from PySide6 import QtWidgets
from video_demo import camera_settings as ui
app = QtWidgets.QApplication([])
def fmt(w, h, pixel, low, high):
    return dict(width=w, height=h, pixel_format=pixel, frame_rates=[dict(min=low, max=high)])
ui.inventory = lambda **kw: {'devices': [
    {'name': 'Camera A', 'formats': [fmt(640, 480, 'nv12', 15, 30),
        fmt(640, 480, 'yuyv422', 15, 15), fmt(1920, 1080, 'nv12', 60, 60)]},
    {'name': 'Camera B', 'formats': [fmt(1280, 800, 'nv12', 25, 25)]}]}
dialog = ui.CameraSettingsDialog(dict(width=1280, height=720, fps=30, bitrate_kbps=3000))
dialog.show()
deadline = time.monotonic() + 5
while dialog.loading and time.monotonic() < deadline:
    app.processEvents()
    time.sleep(.01)
assert len(dialog.rows) == 2
a, b = dialog.rows
a.setChecked(True)
b.setChecked(True)
dialog.preset.setCurrentIndex(dialog.preset.findData([640, 480]))
dialog.fields['fps'].setValue(15)
assert a.size.currentData() == [640, 480] and a.rate.currentText() == '15'
a.auto.setChecked(False)
a.size.setCurrentIndex(a.size.findData([640, 480]))
a.pixel.setCurrentText('nv12')
a.rate.setCurrentText('27.5')
assert a.settings()['fps'] == 27.5  # A continuous supported range accepts a custom rate.
a.pixel.setCurrentText('yuyv422')
assert a.rate.currentText() == '15'  # Format changes cannot retain an unsupported FPS.
a.size.setCurrentIndex(a.size.findData([1920, 1080]))
assert a.pixel.count() == 1 and a.pixel.currentText() == 'nv12'
assert a.rate.currentText() == '60'
a.rate.setCurrentText('30')
dialog.accept()
assert dialog.profile is None and '不支持' in dialog.error.text()
a.rate.setCurrentText('60')
dialog.preset.setCurrentIndex(dialog.preset.findData([960, 540]))
dialog.fields['fps'].setValue(25)
dialog.fields['bitrate_kbps'].setValue(1800)
dialog.fields['width'].setValue(961)
dialog.accept()
assert dialog.profile is None and '偶数' in dialog.error.text()
dialog.fields['width'].setValue(960)
dialog.urls.setPlainText('rtsp://host/stream')
dialog.accept()
profile = dialog.profile
assert profile['cameras'] == [dict(device='Camera A', width=1920, height=1080, pixel_format='nv12', fps=60),
    dict(device='Camera B', width=1280, height=800, pixel_format='nv12', fps=25), dict(device='rtsp://host/stream')]
assert profile['output'] == dict(width=960, height=540, fps=25, bitrate_kbps=1800)
'''
    result = subprocess.run([sys.executable, '-c', script],
        cwd=Path(__file__).resolve().parents[1], env=dict(os.environ, QT_QPA_PLATFORM='offscreen'),
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_missing_camera_modes_allow_explicit_unverified_settings():
    script = '''
from PySide6 import QtWidgets
from video_demo.camera_settings import CameraRow
app = QtWidgets.QApplication([])
row = CameraRow({'name': 'Unknown USB'}, '/dev/video0', dict(width=1280, height=720, fps=30))
row.setChecked(True)
assert row.settings() == {'device': '/dev/video0'}
row.auto.setChecked(False)
row.size.setCurrentText('800x600')
row.pixel.setCurrentText('mjpeg')
row.rate.setCurrentText('25')
assert row.settings() == dict(device='/dev/video0', width=800, height=600, fps=25, pixel_format='mjpeg')
assert '启动时验证' in row.hint.text()
mac = CameraRow({'name': 'Mac', 'formats': [dict(width=640, height=480, pixel_format='nv12',
    frame_rates=[dict(min=15, max=30)])]}, 'Mac', dict(width=640, height=480, fps=15), backend='avfoundation')
mac.setChecked(True)
assert mac.settings()['fps'] == 30
mac.auto.setChecked(False)
assert mac.rate.count() == 1 and mac.rate.currentText() == '30'
assert '当前采集接口' in mac.hint.text()
'''
    result = subprocess.run([sys.executable, '-c', script],
        cwd=Path(__file__).resolve().parents[1], env=dict(os.environ, QT_QPA_PLATFORM='offscreen'),
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def test_selected_output_quality_reaches_real_sender_and_receiver(tmp_path):
    script = '''
import json
from pathlib import Path
import sys
from PySide6 import QtCore, QtWidgets
sys.path.insert(0, 'tests')
from rtsp_server import RtspCamera
from video_demo import camera_settings as ui
from video_demo.cli import parser, validate, run_demo
app = QtWidgets.QApplication([])
ui.inventory = lambda **kw: {'devices': []}
with RtspCamera() as camera:
    def choose():
        dialog = app.activeModalWidget()
        dialog.urls.setPlainText(camera.url)
        for key, value in dict(width=160, height=90, fps=12, bitrate_kbps=700).items():
            dialog.fields[key].setValue(value)
        dialog.accept()
    QtCore.QTimer.singleShot(50, choose)
    p = parser()
    args = p.parse_args(['demo', '--duration', '3', '--decoder', 'software', '--output', sys.argv[1]])
    validate(p, args)
    args.headless = True  # The startup dialog was exercised; don't open a video window in this test.
    assert run_demo(args) == 0
directory = Path(sys.argv[1])
for relative in ['config.json', 'sender/config.json', 'receiver/config.json']:
    config = json.loads((directory / relative).read_text())
    assert (config['width'], config['height'], config['fps']) == (160, 90, 12)
assert json.loads((directory / 'sender/config.json').read_text())['bitrate_kbps'] == 700
report = json.loads((directory / 'report.json').read_text())
assert report['delivery']['0']['decoded'] > 10
assert report['glass_to_glass_latency_ms'] is None
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1], env=dict(os.environ, QT_QPA_PLATFORM='offscreen'),
        capture_output=True, text=True, timeout=25)
    assert result.returncode == 0, result.stdout + result.stderr


def test_sdk_picker_selects_real_mode_and_rejects_duplicate_physical_device():
    script = '''
import time
from pathlib import Path
from PySide6 import QtWidgets
from video_demo import camera_settings as ui
app = QtWidgets.QApplication([])
ui.inventory = lambda **kw: {'devices': []}
ui.configured_root = lambda: Path('/fixture')
def record(kind):
    return dict(name='SDK camera ' + kind, serial='ABC', sdk_stream=kind, device='orbbec://ABC/' + kind,
        formats=[dict(width=640, height=480, pixel_format='rgb24' if kind == 'color' else 'gray16le',
            frame_rates=[dict(min=15, max=15), dict(min=30, max=30)])])
ui.sdk_inventory = lambda: {'devices': [record('color'), record('depth')], 'unavailable': []}
d = ui.CameraSettingsDialog(dict(width=1280, height=720, fps=30, bitrate_kbps=3000))
d.show()
d.start_sdk_query()
deadline = time.monotonic() + 5
while (d.loading or d.sdk_loading) and time.monotonic() < deadline:
    app.processEvents()
    time.sleep(.01)
assert len(d.rows) == 2
color, depth = d.rows
color.setChecked(True)
color.auto.setChecked(False)
color.rate.setCurrentText('15')
depth.setChecked(True)
d.accept()
assert d.profile is None and '重复序列号' in d.error.text()
color.setChecked(False)
depth.depth_fields['depth_min_mm'].setValue(800)
depth.depth_fields['depth_max_mm'].setValue(400)
d.accept()
assert d.profile is None and '最近距离' in d.error.text()
depth.setChecked(False)
color.setChecked(True)
d.accept()
assert d.profile['cameras'] == [dict(device='orbbec://ABC/color', width=640, height=480, fps=15,
    pixel_format='rgb24', label='SDK camera color')]
'''
    result = subprocess.run([sys.executable, '-c', script],
        cwd=Path(__file__).resolve().parents[1], env=dict(os.environ, QT_QPA_PLATFORM='offscreen'),
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
