"""RTSP camera input, bounded network waits and credential-safe diagnostics."""
from __future__ import annotations

import math
import os
import re
from urllib.parse import urlsplit, urlunsplit

import av


def is_rtsp(value):
    return isinstance(value, str) and value.lower().startswith(('rtsp://', 'rtsps://'))


def redact(value):
    """Sanitize nested run metadata, including URLs embedded in exception messages."""
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [redact(v) for v in value]
    if not isinstance(value, str):
        return value
    if 'rtsp://' not in value.lower() and 'rtsps://' not in value.lower():
        return value

    def clean(match):
        try:
            parts = urlsplit(match.group())
            host = parts.netloc.rsplit('@', 1)[-1]
            # Stream paths and query parameters can themselves contain access tokens.
            return urlunsplit((parts.scheme, host, '/<stream>', '', ''))
        except ValueError:
            return 'rtsp://<redacted>'
    return re.sub(r'rtsps?://\S+', clean, value, flags=re.IGNORECASE)


def resolve_device(camera):
    """Allow profiles to reference an environment variable instead of storing a URL."""
    if 'device_env' not in camera:
        return dict(camera)
    key = camera['device_env']
    if 'device' in camera or not isinstance(key, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
        raise ValueError('Use exactly one device or valid device_env in each camera')
    value = os.environ.get(key, '').strip()
    if not value:
        raise ValueError(f'Camera environment variable {key} is missing or empty')
    return {**{k: v for k, v in camera.items() if k != 'device_env'}, 'device': value}


def camera_display_names(cameras):
    """Small public labels for remote viewers; never send camera credentials."""
    names = []
    for index, camera in enumerate(cameras):
        label = camera.get('label')
        if not isinstance(label, str) or not label.strip():
            device = camera.get('device', '')
            if is_rtsp(device):
                label = f'网络摄像头 · {urlsplit(device).hostname or "未知主机"}'
            else:
                label = device if isinstance(device, str) and not device.isdecimal() else ''
        names.append(' '.join(redact(label).split())[:120] or f'摄像头 {index + 1}')
    return names


def validate_network_camera(camera):
    url = camera['device']
    try:
        p = urlsplit(url)
        if not is_rtsp(url) or not p.hostname or p.fragment or any(c.isspace() for c in url):
            raise ValueError()
        if p.port is not None and not 1 <= p.port <= 65535:
            raise ValueError()
    except ValueError:
        raise ValueError('网络摄像机需要有效的 rtsp://主机[:端口]/路径 地址；账号中的特殊字符请 URL 编码。') from None
    if camera.get('rtsp_transport', 'tcp') not in ('tcp', 'udp'):
        raise ValueError('rtsp_transport must be tcp or udp')
    for key, default, low, high in [('open_timeout_s', 3, .2, 10),
                                    ('read_timeout_s', 2, .2, 10),
                                    ('retry_delay_s', 1, .1, 30)]:
        n = camera.get(key, default)
        if isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) or not low <= n <= high:
            raise ValueError(f'{key} must be {low}..{high} seconds')
    unsupported = {'width', 'height', 'fps', 'pixel_format', 'input_format'} & camera.keys()
    if unsupported:
        raise ValueError('RTSP 输入尺寸/FPS/编码由摄像机设置；请把输出 width/height/fps 放入 profile.output，'
                         f'不要写在 RTSP 单路配置中：{sorted(unsupported)}')


def rtsp_options(camera):
    transport = camera.get('rtsp_transport', 'tcp')
    options = {'rtsp_transport': transport, 'allowed_media_types': 'video',
            'fflags': 'discardcorrupt', 'probesize': '131072', 'analyzeduration': '100000',
            'fpsprobesize': '2', 'max_delay': '20000',
            'reorder_queue_size': '32' if transport == 'udp' else '0'}
    if camera.get('device', '').lower().startswith('rtsps://'):
        options['tls_verify'] = '1'
    return options


def open_rtsp(camera):
    validate_network_camera(camera)
    # Keep FFmpeg's potentially credential-bearing diagnostics out of stderr.
    with av.logging.Capture():
        return av.open(camera['device'], format='rtsp', options=rtsp_options(camera),
                       timeout=(camera.get('open_timeout_s', 3), camera.get('read_timeout_s', 2)))


def network_frames(camera, stop, on_event):
    """Reconnect independently; never promote post-network timestamps to exposure time."""
    validate_network_camera(camera)
    connection = 0
    while not stop.is_set():
        container = None
        try:
            on_event('network_connecting', connection=connection + 1)
            container = open_rtsp(camera)
            if not container.streams.video:
                raise RuntimeError('RTSP stream has no video track')
            video = container.streams.video[0]
            video.codec_context.thread_count = 1
            decoder = iter(container.decode(video=0))
            first = True
            while not stop.is_set():
                with av.logging.Capture():
                    frame = next(decoder)
                if stop.is_set():
                    break
                if first:
                    connection += 1
                    on_event('network_connected', connection=connection,
                             input_codec=video.codec_context.name,
                             input_rate=str(video.average_rate), actual_width=frame.width,
                             actual_height=frame.height, timestamp_origin='network_decode')
                    first = False
                yield frame
        except (av.error.FFmpegError, OSError, StopIteration, RuntimeError) as exc:
            if not stop.is_set():
                # Include a useful error code, but never echo the raw FFmpeg URL/log.
                code = getattr(exc, 'errno', None)
                on_event('network_disconnected', connection=connection,
                         error_type=type(exc).__name__, error_code=code,
                         message='网络流结束或读取失败；请检查网络、RTSP 路径和账号权限。',
                         retry_delay_s=camera.get('retry_delay_s', 1))
        finally:
            if container is not None:
                with av.logging.Capture():
                    container.close()
        if not stop.is_set():
            stop.wait(camera.get('retry_delay_s', 1))


def child_camera_profile(cameras):
    """Transfer RTSP URLs through child environment, never through saved run files."""
    entries, env = [], {}
    for index, camera in enumerate(cameras):
        if is_rtsp(camera['device']):
            key = f'VIDEO_DEMO_RTSP_INPUT_{index}'
            env[key] = camera['device']
            entries.append({**{k: v for k, v in camera.items() if k != 'device'}, 'device_env': key})
        else:
            entries.append(dict(camera))
    return dict(version=1, cameras=entries), env
