"""Optional vendor adapters. Device URIs select the adapter, never a model name."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import quote, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
LOCAL_CONFIG = ROOT / '.local' / 'camera-sdks.json'
STREAMS = {'color': (2, 2, '彩色'), 'color_left': (11, 13, '左彩色'),
           'color_right': (12, 14, '右彩色'), 'ir': (1, 1, '红外'),
           'ir_left': (6, 8, '左红外'), 'ir_right': (7, 9, '右红外'),
           'depth': (3, 3, '深度预览（非彩色）')}


def is_sdk(device):
    return isinstance(device, str) and device.lower().startswith('orbbec://')


def device_uri(serial, stream):
    return f'orbbec://{quote(serial, safe="")}/{stream}'


def parse_device(device):
    uri = urlsplit(device)
    stream = uri.path.removeprefix('/')
    serial = unquote(uri.netloc)
    if uri.scheme.lower() != 'orbbec' or not serial or stream not in STREAMS or uri.query or uri.fragment or any(c.isspace() for c in serial):
        raise ValueError('SDK 输入格式：orbbec://设备序列号/color（或 ir、ir_left、ir_right、depth、color_left、color_right）')
    return serial, stream


def validate_camera(camera):
    import math
    _, stream = parse_device(camera['device'])
    forbidden = {'input_format', 'rtsp_transport', 'open_timeout_s', 'read_timeout_s', 'retry_delay_s'} & camera.keys()
    if forbidden:
        raise ValueError(f'SDK 相机不接受这些参数：{sorted(forbidden)}')
    for key, lo, hi in [('width', 64, 4096), ('height', 64, 2160), ('fps', 1, 120)]:
        value = camera.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or
                                  not math.isfinite(value) or not lo <= value <= hi or int(value) != value):
            raise ValueError(f'SDK {key} 必须是 {lo}–{hi} 的整数')
    near, far = camera.get('depth_min_mm', 200), camera.get('depth_max_mm', 6000)
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (near, far)) or not 0 <= near < far <= 100000:
        raise ValueError('深度预览需要 0 ≤ 最近距离 < 最远距离 ≤ 100000 mm')
    if stream != 'depth' and {'depth_min_mm', 'depth_max_mm'} & camera.keys():
        raise ValueError('深度显示范围只适用于 depth 输入')


def configured_root():
    override = os.environ.get('ORBBEC_SDK_ROOT')
    if override:
        return Path(override).expanduser().resolve()
    if LOCAL_CONFIG.exists():
        value = json.loads(LOCAL_CONFIG.read_text()).get('orbbec_root')
        if value:
            return Path(value).expanduser().resolve()
    return None


def configure_root(path):
    from .orbbec import library_path
    root = Path(path).expanduser().resolve()
    library_path(root)  # Validate the package, don't download or change system drivers.
    LOCAL_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(LOCAL_CONFIG.read_text()) if LOCAL_CONFIG.exists() else {}
    data['orbbec_root'] = str(root)
    LOCAL_CONFIG.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n')
    return root


def inventory():
    """Isolate native enumeration and bound failures; never start image streams."""
    root = configured_root()
    if root is None:
        return {'backend': 'orbbec', 'devices': [], 'status': 'not_configured'}
    result = subprocess.run([sys.executable, '-m', 'video_demo.orbbec', '--inventory', str(root)],
                            cwd=ROOT, capture_output=True, text=True, timeout=45)
    if result.returncode:
        raise RuntimeError('Orbbec SDK 查询失败：' + (result.stderr[-1500:] or f'退出码 {result.returncode}'))
    # A native dependency may print diagnostics. Only our explicit marker is JSON.
    marker = 'VIDEO_DEMO_SDK_JSON='
    records = [line[len(marker):] for line in result.stdout.splitlines() if line.startswith(marker)]
    if not records:
        raise RuntimeError('Orbbec SDK 未返回有效的设备列表')
    return json.loads(records[-1])


def configure_inputs(settings, width, height, fps, exact=False):
    from .cameras import capture_modes, select_capture_mode
    info = inventory()
    records = {record['device']: record for record in info['devices']}
    result = []
    serials = set()
    for camera in settings:
        validate_camera(camera)
        serial, stream = parse_device(camera['device'])
        if serial in serials:
            raise ValueError('当前每台 SDK 相机一次选择一种图像流；请勿重复选择同一序列号。')
        serials.add(serial)
        record = records.get(camera['device'])
        if not record:
            errors = '; '.join(d.get('error', '') for d in info.get('unavailable', []))
            raise ValueError(f'SDK 未提供 {serial} 的 {stream} 图像流。{errors or "请检查 SDK 路径、设备连接和当前工作模式。"}')
        chosen = select_capture_mode(camera, capture_modes(record), width, height, fps, exact=exact)
        chosen.setdefault('label', record['name'])
        result.append(chosen)
    return result


def frames(camera, stop):
    from .orbbec import camera_frames
    root = configured_root()
    if root is None:
        raise RuntimeError('请先在设置窗口选择 Orbbec SDK 目录，或设置 ORBBEC_SDK_ROOT')
    yield from camera_frames(root, camera, stop)
