import json
from fractions import Fraction

import av
import numpy as np
import pytest

from video_demo.cameras import capture_options, read_profile, capture_modes, select_capture_mode, configure_camera_inputs
from video_demo.media import make_encoder, make_decoder, prepare_frame
from video_demo.cli import parser, validate


def test_camera_nv12_to_encoder_preserves_color_without_rgb_queue():
    from av.video.reformatter import VideoReformatter
    reformatter = VideoReformatter()
    pixels = np.full((180, 320, 3), (50, 160, 210), dtype=np.uint8)
    camera = av.VideoFrame.from_ndarray(pixels, format='rgb24').reformat(format='nv12', dst_colorspace='ITU709')
    camera.colorspace = camera.color_range = 1
    encoder = make_encoder('libx264', 320, 180, 30, 1_000_000)
    decoder, _ = make_decoder('software')
    converted = prepare_frame(camera, 0, encoder)
    decoded = decoder.decode(encoder.encode(converted)[0])[0]
    result = decoded.reformat(format='rgb24', src_colorspace='ITU709').to_ndarray()
    assert np.abs(result.astype(float) - pixels).mean() < 5
    # A camera's microsecond PTS must not rescale encoder frame ids back to zero.
    for i in range(1, 4):
        camera.time_base = Fraction(1, 1_000_000)
        camera.pts = 100_000 + i * 33_333
        packet = encoder.encode(prepare_frame(camera, i, encoder, reformatter))[0]
        assert packet.pts == i


def test_macos_camera_requests_nv12_and_drops_late_frames(monkeypatch):
    monkeypatch.setattr('video_demo.cameras.platform.system', lambda: 'Darwin')
    fmt, url, options = capture_options({'device': 'USB Camera', 'fps': 60}, 1280, 720, 30)
    assert fmt == 'avfoundation' and url == 'USB Camera:none'
    assert options['drop_late_frames'] == '1' and options['pixel_format'] == 'nv12'
    assert options['framerate'] == '60'


def test_invalid_profile_rejected(tmp_path):
    p = tmp_path / 'camera.json'
    p.write_text(json.dumps({'version': 1, 'cameras': [{'device': '0', 'fps': -1}]}))
    with pytest.raises(ValueError, match='fps'):
        read_profile(p)


def test_stream_count_follows_cameras(tmp_path):
    p = parser()
    args = p.parse_args(['demo', '--cameras', '0,1', '--headless', '--duration', '1', '--output', str(tmp_path)])
    validate(p, args)
    assert args.streams == 2 and args.fps == 30
    assert args.sync_wait_ms == pytest.approx(1000 / 30 / 2)
    assert [c['device'] for c in args.camera_settings] == ['0', '1']


@pytest.mark.parametrize('command', ['send', 'demo'])
def test_generated_source_option_is_rejected(command):
    with pytest.raises(SystemExit):
        parser().parse_args([command, '--source', 'synthetic'])


@pytest.mark.parametrize('command', ['send', 'demo'])
def test_sender_requires_camera_inputs(tmp_path, command):
    p = parser()
    argv = [command, '--duration', '1', '--output', str(tmp_path)]
    if command == 'demo':
        argv.append('--headless')
    with pytest.raises(SystemExit):
        validate(p, p.parse_args(argv))


def test_gui_settings_propagate_to_run_and_cli_values_initialize_controls(tmp_path, monkeypatch):
    def choose(output, capture_format, exact):
        assert output == dict(width=1920, height=1080, fps=60, bitrate_kbps=5000)
        assert capture_format == 'nv12' and not exact
        return dict(version=1, output=dict(width=960, height=540, fps=25, bitrate_kbps=1800),
                    cameras=[dict(device='A', width=640, height=480, fps=15, pixel_format='yuyv422'),
                             dict(device='rtsp://host/video')])
    monkeypatch.setattr('video_demo.cameras.select_camera_profile', choose)
    p = parser()
    args = p.parse_args(['demo', '--width', '1920', '--height', '1080', '--fps', '60',
                        '--bitrate-kbps', '5000', '--capture-format', 'nv12', '--output', str(tmp_path)])
    validate(p, args)
    assert (args.width, args.height, args.fps, args.bitrate_kbps) == (960, 540, 25, 1800)
    assert args.streams == 2 and args.sync_wait_ms == 20
    assert args.camera_settings[0] == dict(device='A', width=640, height=480, fps=15, pixel_format='yuyv422')
    assert args.camera_settings[1] == dict(device='rtsp://host/video')


def test_per_camera_settings_can_have_different_rates(tmp_path):
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'version': 1, 'output': {'fps': 30}, 'cameras': [
        {'device': 'A', 'fps': 60}, {'device': 'B', 'fps': 30}]}))
    p = parser()
    args = p.parse_args(['send', '--camera-profile', str(path), '--output', str(tmp_path / 'run')])
    validate(p, args)
    assert args.streams == 2 and args.fps == 30
    assert [c['fps'] for c in args.camera_settings] == [60, 30]


def modes(width, height, rates, pixel='420v'):
    return capture_modes({'formats': [dict(width=width, height=height, media_subtype=pixel,
                          frame_rates=[{'min': fps, 'max': fps} for fps in rates])]})


def test_auto_selects_supported_size_and_rate_for_nonstandard_camera():
    available = modes(640, 400, [15, 25]) + modes(1280, 800, [15, 25])
    choice = select_capture_mode({'device': 'Camera A'}, available, 1280, 720, 30)
    assert (choice['width'], choice['height'], choice['fps']) == (1280, 800, 25)
    assert choice['pixel_format'] == 'nv12'


def test_avfoundation_modes_respect_ffmpeg_max_rate_restriction():
    record = {'formats': [dict(width=640, height=480, media_subtype='420v',
                              frame_rates=[dict(min=15, max=30)])]}
    native = capture_modes(record)
    assert select_capture_mode({'device': 'A', 'fps': 15}, native, 640, 480, 15)['fps'] == 15
    backend = capture_modes(record, 'avfoundation')
    assert select_capture_mode({'device': 'A'}, backend, 640, 480, 15)['fps'] == 30
    with pytest.raises(ValueError, match='不支持'):
        select_capture_mode({'device': 'A', 'fps': 15}, backend, 640, 480, 15)
    assert record['formats'][0]['frame_rates'] == [dict(min=15, max=30)]


def test_exact_mode_reports_capabilities_instead_of_silently_changing_input():
    available = modes(1280, 800, [25])
    with pytest.raises(ValueError, match='Camera A.*1280x720@30.*1280x800'):
        select_capture_mode({'device': 'Camera A'}, available, 1280, 720, 30, exact=True)


def test_explicit_per_camera_rate_is_not_overridden():
    available = modes(1280, 800, [15, 25])
    choice = select_capture_mode({'device': 'Camera A', 'fps': 15}, available, 1280, 720, 30)
    assert choice['fps'] == 15
    with pytest.raises(ValueError, match='Camera A'):
        select_capture_mode({'device': 'Camera A', 'fps': 30}, available, 1280, 720, 30)


def test_uvc_fractional_rate_rounding_does_not_reject_nominal_60fps():
    available = modes(1280, 720, [30.00003, 60.00024])
    choice = select_capture_mode({'device': 'Camera A'}, available, 1280, 720, 60, exact=True)
    assert choice['fps'] == 60


def test_capture_plan_keeps_each_camera_own_rate_and_roundtrips_to_child_profile(tmp_path, monkeypatch):
    data = {'devices': [
        {'name': 'A', 'index_hint': 0, 'formats': [{'width': 1280, 'height': 720, 'media_subtype': '420v',
          'frame_rates': [{'min': 15, 'max': 30}]}]},
        {'name': 'B', 'index_hint': 1, 'formats': [{'width': 1280, 'height': 800, 'media_subtype': '420v',
          'frame_rates': [{'min': 25, 'max': 25}]}]},
    ]}
    monkeypatch.setattr('video_demo.cameras.inventory', lambda include_modes: data)
    p = parser()
    args = p.parse_args(['send', '--cameras', 'A,B', '--output', str(tmp_path / 'run')])
    validate(p, args)
    configure_camera_inputs(args)
    assert args.fps == 30  # Common encoding target, not the second device's capture rate.
    assert [s['fps'] for s in args.camera_settings] == [30, 25]
    path = tmp_path / 'selected.json'
    path.write_text(json.dumps(dict(version=1, cameras=args.camera_settings)))
    child = p.parse_args(['send', '--camera-profile', str(path), '--capture-mode', 'exact',
                          '--output', str(tmp_path / 'child')])
    validate(p, child)
    configure_camera_inputs(child)
    assert child.camera_settings == args.camera_settings
