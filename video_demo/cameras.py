"""Real camera configuration and read-only device/mode inventory."""
from __future__ import annotations

import json
import math
from pathlib import Path
import platform
import re
import shutil
import subprocess

from .network import is_rtsp, redact, resolve_device, validate_network_camera
from .sdk import is_sdk


def inventory(include_modes=True):
    if platform.system() == 'Darwin':
        if not include_modes or not shutil.which('swift'):
            import av
            old_level = av.logging.get_level()
            try:
                av.logging.set_level(av.logging.INFO)
                with av.logging.Capture(local=False) as logs:
                    try:
                        av.open('', format='avfoundation', options={'list_devices': 'true'})
                    except av.error.FFmpegError:
                        pass
            finally:
                av.logging.set_level(old_level)
            devices, video = [], False
            for _, _, line in logs:
                if 'video devices:' in line:
                    video = True
                elif 'audio devices:' in line:
                    video = False
                elif video:
                    match = re.search(r'\[(\d+)\] (.+)', line.strip())
                    if match and not match.group(2).startswith('Capture screen'):
                        devices.append({'index_hint': int(match.group(1)), 'name': match.group(2)})
            return {'backend': 'avfoundation', 'metadata_only': True, 'formats_available': False,
                    'devices': devices}
        helper = Path(__file__).resolve().parents[1] / 'tools/camera_inventory.swift'
        result = subprocess.run(['swift', str(helper)], capture_output=True, text=True, timeout=180)
        if result.returncode:
            raise RuntimeError(result.stderr)
        return json.loads(result.stdout)
    if platform.system() == 'Linux':
        devices = []
        for dev in sorted(Path('/sys/class/video4linux').glob('video*')):
            record = {'name': (dev / 'name').read_text().strip(), 'device': f'/dev/{dev.name}'}
            if shutil.which('v4l2-ctl'):
                record['formats_text'] = subprocess.run(
                    ['v4l2-ctl', '-d', record['device'], '--list-formats-ext'],
                    capture_output=True, text=True, timeout=10).stdout
            devices.append(record)
        return {'backend': 'v4l2', 'metadata_only': True, 'devices': devices}
    if platform.system() == 'Windows':
        import av
        level = av.logging.get_level()
        try:
            av.logging.set_level(av.logging.INFO)
            with av.logging.Capture(local=False) as logs:
                try:
                    av.open('', format='dshow', options={'list_devices': 'true'})
                except av.error.FFmpegError:
                    pass
        finally:
            av.logging.set_level(level)
        devices = []
        video = False
        for _, _, line in logs:
            found = re.search(r'"(.*?)" \(video\)', line)
            if found:
                devices.append({'name': found.group(1), 'device': found.group(1)})
                video = True
            elif '(audio)' in line:
                video = False
            elif video and 'Alternative name' in line:
                alternate = re.search(r'"(.*?)"', line)
                if alternate:
                    devices[-1]['device'] = alternate.group(1)
        return {'backend': 'dshow', 'metadata_only': True, 'devices': devices}
    raise RuntimeError('Unsupported camera platform')


def read_profile(path):
    return validate_profile(json.loads(Path(path).read_text()))


def validate_profile(data):
    """Validate an in-memory profile as well as a CLI JSON file."""
    if not isinstance(data, dict):
        raise ValueError('Camera profile must be an object')
    if data.get('version') != 1 or not isinstance(data.get('cameras'), list) or not 1 <= len(data['cameras']) <= 8:
        raise ValueError('Camera profile requires version=1 and 1..8 cameras')
    allowed = {'device', 'device_env', 'label', 'width', 'height', 'fps', 'pixel_format', 'input_format', 'colorspace',
               'rtsp_transport', 'open_timeout_s', 'read_timeout_s', 'retry_delay_s', 'depth_min_mm', 'depth_max_mm'}
    for index, original in enumerate(data['cameras']):
        if not isinstance(original, dict):
            raise ValueError('Each camera must be an object')
        camera = resolve_device(original)
        data['cameras'][index] = camera
        if set(camera) - allowed:
            raise ValueError(f'Unknown camera settings: {set(camera) - allowed}')
        if not isinstance(camera.get('device'), str) or not camera['device'].strip():
            raise ValueError('Each camera needs a device name/path')
        for key, low, high in [('width', 64, 4096), ('height', 64, 2160), ('fps', 1, 120)]:
            if key in camera and (not isinstance(camera[key], (int, float)) or not low <= camera[key] <= high):
                raise ValueError(f'Invalid camera {key}')
        if camera.get('colorspace', 'auto') not in ('auto', 'bt709', 'bt601'):
            raise ValueError('Camera colorspace must be auto, bt709 or bt601')
        if is_sdk(camera['device']):
            from .sdk import validate_camera
            validate_camera(camera)
        elif is_rtsp(camera['device']):
            validate_network_camera(camera)
        elif '://' in camera['device']:
            raise ValueError('Network camera inputs currently require RTSP/RTSPS')
    if set(data.get('output', {})) - {'width', 'height', 'fps', 'bitrate_kbps'}:
        raise ValueError('Unknown output setting in camera profile')
    return data


def capture_options(camera, width, height, fps):
    system = platform.system()
    device = camera['device']
    options = {'video_size': f'{camera.get("width", width)}x{camera.get("height", height)}',
               'framerate': str(camera.get('fps', fps))}
    pixel = camera.get('pixel_format')
    if system == 'Darwin':
        fmt, url = 'avfoundation', f'{device}:none'
        options.update(pixel_format=pixel or 'nv12', drop_late_frames='1')
    elif system == 'Windows':
        fmt, url = 'dshow', f'video={device}'
        # Small capture queue. A full queue drops frames instead of adding seconds of delay.
        options['rtbufsize'] = '4M'
        if pixel:
            options['pixel_format'] = pixel
    else:
        fmt, url = 'v4l2', device if device.startswith('/') else f'/dev/video{device}'
        if camera.get('input_format'):
            options['input_format'] = camera['input_format']
        elif pixel:
            options['input_format'] = pixel
    return fmt, url, options


# Advertised raw formats, with no camera-vendor names or model rules.
PIXEL_FORMATS = {'420v': 'nv12', '420f': 'nv12', '2vuy': 'uyvy422', 'yuvs': 'yuyv422',
                'BGRA': 'bgra', 'ARGB': 'argb', '24RG': 'rgb24', 'L008': 'gray'}


def capture_modes(record, backend=None):
    result = []
    for fmt in record.get('formats', []):
        pixel = fmt.get('pixel_format') or PIXEL_FORMATS.get(fmt.get('media_subtype'))
        if not pixel:
            continue
        for rate in fmt.get('frame_rates', []):
            low, high = float(rate['min']), float(rate['max'])
            if (fmt['width'] > 0 and fmt['height'] > 0 and 0 < low <= high and
                    all(math.isfinite(v) for v in (low, high))):
                # FFmpeg's AVFoundation configure_video_device matches only
                # maxFrameRate and sets both durations to minFrameDuration.
                # A native 15..30 range therefore offers 30 through this backend.
                # Keep inventory()'s original hardware ranges unchanged.
                if backend == 'avfoundation':
                    low = high
                result.append(dict(width=fmt['width'], height=fmt['height'],
                                   pixel_format=pixel, min_fps=low, max_fps=high))
    return result


def describe_modes(modes):
    return ', '.join(sorted({f'{m["width"]}x{m["height"]} @{m["min_fps"]:g}..{m["max_fps"]:g} {m["pixel_format"]}'
                             for m in modes}))


def select_capture_mode(camera, modes, width, height, fps, exact=False):
    """Respect explicit per-camera constraints; negotiate the common output target otherwise."""
    requested = dict(width=camera.get('width', width), height=camera.get('height', height),
                     fps=camera.get('fps', fps))
    candidates = []
    for mode in modes:
        if any((exact or key in camera) and mode[key] != requested[key] for key in ('width', 'height')):
            continue
        if camera.get('pixel_format') and mode['pixel_format'] != camera['pixel_format']:
            continue
        low, high = mode['min_fps'], mode['max_fps']
        rate = requested['fps']
        # UVC intervals such as 333333*100ns advertise 30.00003 instead of 30.
        if exact or 'fps' in camera:
            if not low - .001 <= rate <= high + .001:
                continue
        else:
            rate = min(high, max(low, rate))
            if abs(rate - round(rate)) < .001:
                rate = int(round(rate))
        geometry = abs(math.log(mode['width'] / requested['width'])) + abs(math.log(mode['height'] / requested['height']))
        rate_error = abs(math.log(rate / requested['fps']))
        pixel_rank = ['nv12', 'yuv420p', 'yuyv422', 'uyvy422', 'bgra', 'argb', 'rgb24', 'gray']
        rank = pixel_rank.index(mode['pixel_format']) if mode['pixel_format'] in pixel_rank else 100
        selected = dict(camera, width=mode['width'], height=mode['height'], fps=rate,
                        pixel_format=mode['pixel_format'])
        candidates.append(((geometry, rate_error, rank), selected))
    if not candidates:
        raise ValueError(f'摄像头“{camera["device"]}”不支持请求的采集模式：'
                         f'{requested["width"]}x{requested["height"]}@{requested["fps"]} '
                         f'{camera.get("pixel_format", "auto")}。可用模式：{describe_modes(modes)}')
    return min(candidates, key=lambda item: item[0])[1]


def configure_camera_inputs(args):
    """Read capabilities before media workers start; never silently skip a device."""
    info = {'devices': []}
    if any(not is_rtsp(s['device']) and not is_sdk(s['device']) for s in args.camera_settings):
        print('[camera] 正在查询设备支持的采集模式…', flush=True)
        try:
            info = inventory(include_modes=True)
        except (RuntimeError, subprocess.SubprocessError, OSError, ValueError) as exc:
            info = {'devices': [], 'enumeration_error': str(exc)}
    sdk_settings = [s for s in args.camera_settings if is_sdk(s['device'])]
    if sdk_settings:
        from .sdk import configure_inputs
        sdk_settings = configure_inputs(sdk_settings, args.width, args.height, args.fps,
                                        exact=args.capture_mode == 'exact')
    sdk_selected = {s['device']: s for s in sdk_settings}
    resolved, reports = [], []
    for settings in args.camera_settings:
        device = settings['device']
        if is_sdk(device):
            chosen = sdk_selected[device]
            resolved.append(chosen)
            reports.append(dict(device=device, requested=dict(settings), selected=chosen, status='sdk_capabilities_selected'))
            print(f'[camera] {chosen["label"]}: {chosen["width"]}x{chosen["height"]}@{chosen["fps"]:g} '
                  f'{chosen["pixel_format"]} → 输出 {args.width}x{args.height}，上限 {args.fps}fps', flush=True)
            continue
        if is_rtsp(device):
            validate_network_camera(settings)
            chosen = dict(settings)
            resolved.append(chosen)
            reports.append(dict(device=device, requested=dict(settings), selected=chosen,
                                status='rtsp_camera_managed_mode'))
            print(f'[camera] {redact(device)}: RTSP/{chosen.get("rtsp_transport", "tcp")} '
                  f'→ 输出 {args.width}x{args.height}，上限 {args.fps}fps；相机端模式由设备配置决定', flush=True)
            continue
        identifier = f'/dev/video{device}' if platform.system() == 'Linux' and device.isdecimal() else device
        records = [d for d in info['devices'] if identifier in (d['name'], d.get('device')) or
                   (device.isdecimal() and d.get('index_hint') == int(device))]
        if len(records) > 1:
            raise ValueError(f'摄像头名称“{device}”不唯一，请使用设备编号或唯一设备路径。')
        if info['devices'] and not records:
            raise ValueError(f'找不到摄像头“{device}”；请重新运行 cameras 查询设备。')
        modes = capture_modes(records[0], info.get('backend')) if records else []
        if modes:
            chosen = select_capture_mode(settings, modes, args.width, args.height, args.fps,
                                         exact=args.capture_mode == 'exact')
            status = 'capabilities_selected'
        else:
            chosen = dict(settings, width=settings.get('width', args.width),
                          height=settings.get('height', args.height), fps=settings.get('fps', args.fps))
            status = 'unverified_requested_mode'
            print(f'[camera] {device}: 无可用的结构化模式列表，按请求尝试；可用 --camera-profile 指定采集参数。', flush=True)
        if records and not chosen.get('label'):
            chosen['label'] = records[0]['name']
        resolved.append(chosen)
        reports.append(dict(device=device, requested=dict(settings), selected=chosen, status=status))
        print(f'[camera] {device}: 采集 {chosen["width"]}x{chosen["height"]}@{chosen["fps"]:g} '
              f'{chosen.get("pixel_format", "backend default")} → 输出 {args.width}x{args.height}，目标 {args.fps}fps', flush=True)
    args.camera_settings, args.camera_modes = resolved, reports
