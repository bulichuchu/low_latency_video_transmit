"""Small, explicit binding to the official Orbbec SDK v2 C ABI.

The SDK remains an optional local installation. No vendor Python wheel or model
specific PID is required. Native frame memory is copied before its handle dies.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
import ctypes as ct
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import platform
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET

import av
import numpy as np

from .sdk import STREAMS, device_uri, parse_device, validate_camera
from .capture_time import GlobalTimestampMap

# Values come from SDK v2 ObTypes.h; keep raw ABI constants separate from AV formats.
FORMATS = {0: 'yuyv422', 1: 'yuyv422', 2: 'uyvy422', 3: 'nv12', 4: 'nv21', 5: 'mjpeg',
           8: 'gray16le', 9: 'gray', 10: 'gray16le', 11: 'gray16le', 12: 'gray16le',
           15: 'yuv420p', 22: 'rgb24', 23: 'bgr24', 24: 'gray16le', 25: 'bgra', 28: 'gray16le', 31: 'rgba'}

# The native DeviceManager/enumerators are shared within the SDK process.
# Serialize context lifetime changes, never frame acquisition or conversion.
_CONTEXT_LOCK = threading.RLock()
_CONTEXTS = {}


def library_path(root):
    root = Path(root)
    patterns = (['lib/libOrbbecSDK.dylib', 'lib/libOrbbecSDK.*.dylib'] if platform.system() == 'Darwin' else
                ['bin/OrbbecSDK.dll', 'lib/OrbbecSDK.dll'] if platform.system() == 'Windows' else
                ['lib/libOrbbecSDK.so', 'lib/libOrbbecSDK.so.*'])
    for pattern in patterns:
        for path in sorted(root.glob(pattern)):
            if path.is_file():
                return path.resolve()
    raise ValueError('请选择已解压的 Orbbec SDK v2 根目录（包含 lib / bin 目录）')


class OrbbecError(RuntimeError):
    pass


class NativeSDK:
    """Bind exact signatures, own error objects, and release handles in reverse order."""
    def __init__(self, root):
        self.root = Path(root)
        path = library_path(root)
        self.dll_directory = os.add_dll_directory(str(path.parent)) if os.name == 'nt' else None
        self.lib = ct.CDLL(str(path))
        ptr, uint, integer = ct.c_void_p, ct.c_uint32, ct.c_int
        self.error_type = ct.POINTER(ptr)
        signatures = {
            'ob_create_context_with_config': (ptr, [ct.c_char_p]),
            'ob_query_device_list': (ptr, [ptr]),
            'ob_device_list_get_count': (uint, [ptr]),
            'ob_device_list_get_device_name': (ct.c_char_p, [ptr, uint]),
            'ob_device_list_get_device_serial_number': (ct.c_char_p, [ptr, uint]),
            'ob_device_list_get_device_by_serial_number': (ptr, [ptr, ct.c_char_p]),
            'ob_device_get_sensor_list': (ptr, [ptr]),
            'ob_sensor_list_get_count': (uint, [ptr]),
            'ob_sensor_list_get_sensor_type': (integer, [ptr, uint]),
            'ob_create_pipeline_with_device': (ptr, [ptr]),
            'ob_pipeline_get_stream_profile_list': (ptr, [ptr, integer]),
            'ob_stream_profile_list_get_count': (uint, [ptr]),
            'ob_stream_profile_list_get_profile': (ptr, [ptr, integer]),
            'ob_stream_profile_get_format': (integer, [ptr]),
            'ob_video_stream_profile_get_width': (uint, [ptr]),
            'ob_video_stream_profile_get_height': (uint, [ptr]),
            'ob_video_stream_profile_get_fps': (uint, [ptr]),
            'ob_create_config': (ptr, []), 'ob_config_disable_all_stream': (None, [ptr]),
            'ob_config_enable_stream_with_stream_profile': (None, [ptr, ptr]),
            'ob_pipeline_start_with_config': (None, [ptr, ptr]),
            'ob_pipeline_stop': (None, [ptr]), 'ob_pipeline_wait_for_frameset': (ptr, [ptr, uint]),
            'ob_frameset_get_frame': (ptr, [ptr, integer]),
            'ob_frame_get_data': (ptr, [ptr]), 'ob_frame_get_data_size': (uint, [ptr]),
            'ob_frame_get_format': (integer, [ptr]), 'ob_video_frame_get_width': (uint, [ptr]),
            'ob_video_frame_get_height': (uint, [ptr]),
            'ob_video_frame_get_pixel_available_bit_size': (ct.c_uint8, [ptr]),
            'ob_frame_get_timestamp_us': (ct.c_uint64, [ptr]),
            'ob_frame_get_system_timestamp_us': (ct.c_uint64, [ptr]),
            'ob_depth_frame_get_value_scale': (ct.c_float, [ptr]),
        }
        for kind in ('context', 'device_list', 'device', 'sensor_list', 'pipeline', 'stream_profile_list', 'stream_profile', 'config', 'frame'):
            signatures['ob_delete_' + kind] = (None, [ptr])
        for name, (result, args) in signatures.items():
            fn = getattr(self.lib, name)
            fn.restype, fn.argtypes = result, args + [self.error_type]
        # Timestamp support is optional: older SDK/device combinations may
        # still capture images even when this measurement is unavailable.
        for name, result, args in [
                ('ob_device_is_global_timestamp_supported', ct.c_bool, [ptr]),
                ('ob_device_enable_global_timestamp', None, [ptr, ct.c_bool]),
                ('ob_frame_get_global_timestamp_us', ct.c_uint64, [ptr])]:
            fn = getattr(self.lib, name, None)
            if fn is not None:
                fn.restype, fn.argtypes = result, args + [self.error_type]
        for name, result, args in [('ob_error_get_message', ct.c_char_p, [ptr]),
                                   ('ob_delete_error', None, [ptr]), ('ob_get_version', integer, []),
                                   ('ob_get_major_version', integer, [])]:
            fn = getattr(self.lib, name)
            fn.restype, fn.argtypes = result, args
        if self.lib.ob_get_major_version() != 2:
            raise ValueError('此适配器需要 Orbbec SDK v2')

    def call(self, name, *args):
        error = ct.c_void_p()
        result = getattr(self.lib, name)(*args, ct.byref(error))
        if error:
            try:
                message = self.lib.ob_error_get_message(error).decode('utf-8', 'replace')
            finally:
                self.lib.ob_delete_error(error)
            if 'uvc_open' in message and ('-3' in message or 'access' in message.lower()):
                if platform.system() == 'Darwin':
                    if os.geteuid() == 0:
                        message += ('；SDK 已有管理员权限，但 USB 接口仍无法打开。'
                                    '请停止同一设备的系统 UVC/其他应用采集后重新查询；'
                                    '仍失败时重新连接该相机并检查助手终端。')
                    else:
                        message += ('；SDK 直接访问 USB 被拒绝，可能与 macOS UVCAssistant 独占接口有关（OrbbecSDK_v2 issue #124）。'
                                    '请在连接摄像头的 Mac 上运行项目的 start_sdk_helper.command，'
                                    '在终端完成管理员授权并保持助手运行，再查询 SDK 摄像头。')
                elif platform.system() == 'Linux':
                    message += '；请检查 USB 访问权限、官方 udev 规则与设备占用。'
                else:
                    message += '；请检查 USB 访问权限、驱动与设备占用。'
            raise OrbbecError(f'{name}: {message}')
        return result

    @contextmanager
    def owned(self, kind, pointer):
        if not pointer:
            raise OrbbecError(f'SDK returned an empty {kind}')
        try:
            yield pointer
        finally:
            self.call('ob_delete_' + kind, pointer)

    @contextmanager
    def context(self):
        """Lease one shared context; destroy it only after its last user exits."""
        key = self.root.resolve()
        with _CONTEXT_LOCK:
            entry = _CONTEXTS.get(key)
            if entry is None:
                stack = ExitStack()
                try:
                    pointer = stack.enter_context(self._create_context())
                except BaseException:
                    stack.close()
                    raise
                entry = {'pointer': pointer, 'users': 0, 'stack': stack}
                _CONTEXTS[key] = entry
            entry['users'] += 1
        try:
            yield entry['pointer']
        finally:
            with _CONTEXT_LOCK:
                entry['users'] -= 1
                if entry['users'] == 0:
                    del _CONTEXTS[key]
                    entry['stack'].close()

    @contextmanager
    def _create_context(self):
        # Use an ephemeral copy of the SDK configuration; don't edit vendor files.
        source = self.root / 'lib' / 'OrbbecSDKConfig.xml'
        if not source.exists():
            source = self.root / 'shared' / 'OrbbecSDKConfig.xml'
        tree = ET.parse(source) if source.exists() else ET.ElementTree(ET.Element('Config'))
        root = tree.getroot()
        for group, key, value in [('Device', 'EnumerateNetDevice', 'false'),
                                  ('Device', 'ClockSource', 'Realtime'),
                                  ('Log', 'FileLogLevel', '5'), ('Log', 'ConsoleLogLevel', '5'),
                                  ('Memory', 'PipelineFrameQueueSize', '1'),
                                  ('Memory', 'FrameProcessingBlockQueueSize', '1')]:
            section = root.find(group)
            if section is None:
                section = ET.SubElement(root, group)
            item = section.find(key)
            if item is None:
                item = ET.SubElement(section, key)
            item.text = value
        with tempfile.TemporaryDirectory(prefix='video-orbbec-') as temporary:
            config = Path(temporary) / 'OrbbecSDKConfig.xml'
            tree.write(config, encoding='utf-8')
            with self.owned('context', self.call('ob_create_context_with_config', os.fsencode(config))) as context:
                yield context

    def profile_info(self, profile):
        return dict(width=self.call('ob_video_stream_profile_get_width', profile),
                    height=self.call('ob_video_stream_profile_get_height', profile),
                    fps=self.call('ob_video_stream_profile_get_fps', profile),
                    sdk_format=self.call('ob_stream_profile_get_format', profile))

    def global_timestamp_supported(self, device):
        if not all(hasattr(self.lib, name) for name in ('ob_device_is_global_timestamp_supported',
                'ob_device_enable_global_timestamp', 'ob_frame_get_global_timestamp_us')):
            return False
        return bool(self.call('ob_device_is_global_timestamp_supported', device))


def inventory(root, *, exclude_serials=()):
    """Query idle devices only; do not reopen interfaces owned by live captures."""
    excluded = set(exclude_serials)
    sdk = NativeSDK(root)
    records, unavailable = [], []
    with sdk.context() as ctx, sdk.owned('device_list', sdk.call('ob_query_device_list', ctx)) as devices:
        for index in range(sdk.call('ob_device_list_get_count', devices)):
            name = sdk.call('ob_device_list_get_device_name', devices, index).decode('utf-8', 'replace')
            serial = sdk.call('ob_device_list_get_device_serial_number', devices, index).decode('utf-8', 'replace')
            if serial in excluded:
                continue
            try:
                with ExitStack() as stack:
                    dev = stack.enter_context(sdk.owned('device', sdk.call('ob_device_list_get_device_by_serial_number', devices, serial.encode())))
                    try:
                        global_supported = sdk.global_timestamp_supported(dev)
                    except OrbbecError:
                        global_supported = None
                    sensors = stack.enter_context(sdk.owned('sensor_list', sdk.call('ob_device_get_sensor_list', dev)))
                    pipeline = stack.enter_context(sdk.owned('pipeline', sdk.call('ob_create_pipeline_with_device', dev)))
                    types = {sdk.call('ob_sensor_list_get_sensor_type', sensors, n) for n in range(sdk.call('ob_sensor_list_get_count', sensors))}
                    for stream, (sensor_type, _, label) in STREAMS.items():
                        if sensor_type not in types:
                            continue
                        formats = []
                        with sdk.owned('stream_profile_list', sdk.call('ob_pipeline_get_stream_profile_list', pipeline, sensor_type)) as profiles:
                            for n in range(sdk.call('ob_stream_profile_list_get_count', profiles)):
                                with sdk.owned('stream_profile', sdk.call('ob_stream_profile_list_get_profile', profiles, n)) as profile:
                                    mode = sdk.profile_info(profile)
                                    pixel = FORMATS.get(mode['sdk_format'])
                                    if pixel and (stream != 'depth' or pixel == 'gray16le'):
                                        formats.append(dict(width=mode['width'], height=mode['height'], pixel_format=pixel,
                                                            frame_rates=[dict(min=mode['fps'], max=mode['fps'])]))
                        if formats:
                            records.append(dict(name=f'{name} · {label} · SDK [{serial}]', device=device_uri(serial, stream),
                                                backend='orbbec', serial=serial, sdk_stream=stream, formats=formats,
                                                global_timestamp_supported=global_supported))
            except OrbbecError as exc:
                unavailable.append(dict(name=name, serial=serial, error=str(exc)))
    return dict(backend='orbbec', sdk_version=sdk.lib.ob_get_version(), metadata_only=True,
                devices=records, unavailable=unavailable, status='ready')


def convert_image(data, width, height, sdk_format, *, stream='color', scale_mm=1., valid_bits=16,
                  near_mm=200., far_mm=6000., decoder=None):
    """Convert a copied SDK image; depth becomes an explicitly lossy gray preview."""
    pixel = FORMATS.get(sdk_format)
    if not pixel or not 1 <= width <= 4096 or not 1 <= height <= 2160:
        raise ValueError('Unsupported SDK image format/dimensions')
    if pixel == 'mjpeg':
        codec = decoder or av.CodecContext.create('mjpeg', 'r')
        frames = codec.decode(av.Packet(data))
        if len(frames) != 1 or (frames[0].width, frames[0].height) != (width, height):
            raise ValueError('SDK MJPEG frame dimensions/count mismatch')
        return frames[0]
    bytes_per_pixel = {'rgb24': 3, 'bgr24': 3, 'bgra': 4, 'rgba': 4, 'gray16le': 2,
                       'yuyv422': 2, 'uyvy422': 2, 'gray': 1}.get(pixel)
    if (pixel in ('nv12', 'nv21', 'yuv420p') and (width % 2 or height % 2)) or (pixel in ('yuyv422', 'uyvy422') and width % 2):
        raise ValueError('Subsampled SDK pixel format requires even dimensions')
    expected = width * height * bytes_per_pixel if bytes_per_pixel else width * height * 3 // 2
    if len(data) != expected:
        raise ValueError(f'SDK frame length {len(data)} != {expected}; unsupported padding/packing')
    if pixel == 'gray16le':
        values = np.frombuffer(data, dtype='<u2').reshape(height, width)
        if stream == 'depth':
            if not math.isfinite(scale_mm) or scale_mm <= 0 or not 0 <= near_mm < far_mm:
                raise ValueError('Invalid depth scale/display range')
            distance = values.astype(np.float32) * scale_mm
            pixels = np.clip(255 - (distance - near_mm) * 254 / (far_mm - near_mm), 1, 255).astype(np.uint8)
            pixels[values == 0] = 0
        else:
            if not 1 <= valid_bits <= 16:
                raise ValueError('Invalid SDK grayscale bit count')
            pixels = np.clip(values.astype(np.float32) * (255 / ((1 << valid_bits) - 1)), 0, 255).astype(np.uint8)
        return av.VideoFrame.from_ndarray(pixels, format='gray')
    # Fill AV planes explicitly: from_ndarray does not support every native YUV
    # format (notably UYVY/NV21). Respect AVFrame alignment, not a guessed stride.
    frame = av.VideoFrame(width, height, pixel)
    layout = ([(height, width * bytes_per_pixel)] if bytes_per_pixel else
              [(height, width), (height // 2, width)] if pixel in ('nv12', 'nv21') else
              [(height, width), (height // 2, width // 2), (height // 2, width // 2)])
    offset = 0
    for plane, (rows, row_bytes) in zip(frame.planes, layout):
        size = rows * row_bytes
        source = np.frombuffer(data, dtype=np.uint8, count=size, offset=offset).reshape(rows, row_bytes)
        target = np.zeros((rows, plane.line_size), dtype=np.uint8)
        target[:, :row_bytes] = source
        plane.update(target)
        offset += size
    return frame


def camera_frames(root, camera, stop):
    """One physical device, one selected image stream, one-frame SDK queues."""
    validate_camera(camera)
    serial, stream = parse_device(camera['device'])
    sensor_type, frame_type, _ = STREAMS[stream]
    sdk = NativeSDK(root)
    with ExitStack() as stack:
        ctx = stack.enter_context(sdk.context())
        devices = stack.enter_context(sdk.owned('device_list', sdk.call('ob_query_device_list', ctx)))
        dev = stack.enter_context(sdk.owned('device', sdk.call('ob_device_list_get_device_by_serial_number', devices, serial.encode())))
        timestamp_status, timestamp_error = 'unsupported', None
        try:
            if sdk.global_timestamp_supported(dev):
                sdk.call('ob_device_enable_global_timestamp', dev, True)
                timestamp_status = 'warming_up'
        except OrbbecError as exc:
            timestamp_status, timestamp_error = 'sdk_error', str(exc)
        timestamp_map = GlobalTimestampMap()
        pipeline = stack.enter_context(sdk.owned('pipeline', sdk.call('ob_create_pipeline_with_device', dev)))
        profiles = stack.enter_context(sdk.owned('stream_profile_list', sdk.call('ob_pipeline_get_stream_profile_list', pipeline, sensor_type)))
        selected = None
        for n in range(sdk.call('ob_stream_profile_list_get_count', profiles)):
            profile = sdk.call('ob_stream_profile_list_get_profile', profiles, n)
            with sdk.owned('stream_profile', profile):
                mode = sdk.profile_info(profile)
                if (mode['width'], mode['height'], mode['fps'], FORMATS.get(mode['sdk_format'])) == (
                        camera['width'], camera['height'], camera['fps'], camera['pixel_format']):
                    config = stack.enter_context(sdk.owned('config', sdk.call('ob_create_config')))
                    sdk.call('ob_config_disable_all_stream', config)
                    sdk.call('ob_config_enable_stream_with_stream_profile', config, profile)
                    selected = mode
                    break
        if selected is None:
            raise ValueError('SDK 当前模式列表与所选配置不匹配，请重新查询')
        decoder = av.CodecContext.create('mjpeg', 'r') if camera['pixel_format'] == 'mjpeg' else None
        if decoder:
            decoder.thread_count = 1
        sdk.call('ob_pipeline_start_with_config', pipeline, config)
        try:
            last_frame = time.monotonic()
            while not stop.is_set():
                if time.monotonic() - last_frame > 5:
                    raise OrbbecError('SDK 连续 5 秒未交付图像，请检查设备连接、USB 占用和采集模式')
                frameset = sdk.call('ob_pipeline_wait_for_frameset', pipeline, 100)
                if not frameset:
                    continue
                with sdk.owned('frame', frameset):
                    frame = sdk.call('ob_frameset_get_frame', frameset, frame_type)
                    if not frame:
                        continue
                    with sdk.owned('frame', frame):
                        received = time.perf_counter_ns()
                        width, height = sdk.call('ob_video_frame_get_width', frame), sdk.call('ob_video_frame_get_height', frame)
                        fmt = sdk.call('ob_frame_get_format', frame)
                        size = sdk.call('ob_frame_get_data_size', frame)
                        if size > 64 * 1024 * 1024:
                            raise ValueError('SDK frame exceeds memory limit')
                        pointer = sdk.call('ob_frame_get_data', frame)
                        if not pointer or not size:
                            raise ValueError('SDK returned an empty image buffer')
                        data = ct.string_at(pointer, size)
                        meta = dict(sdk_device_timestamp_us=sdk.call('ob_frame_get_timestamp_us', frame),
                                    sdk_system_timestamp_us=sdk.call('ob_frame_get_system_timestamp_us', frame),
                                    sdk_format=fmt, sdk_stream=stream)
                        meta.update(sdk_global_timestamp_us=0, sensor_capture_ns=0,
                                    sensor_status=timestamp_status, sensor_clock_uncertainty_us=0)
                        if timestamp_error:
                            meta['sdk_timestamp_error'] = timestamp_error
                        if timestamp_status == 'warming_up':
                            try:
                                global_us = sdk.call('ob_frame_get_global_timestamp_us', frame)
                                before = time.perf_counter_ns()
                                wall = time.time_ns()
                                after = time.perf_counter_ns()
                                meta['sdk_global_timestamp_us'] = global_us
                                meta.update(timestamp_map.update(meta['sdk_device_timestamp_us'], global_us,
                                    meta['sdk_system_timestamp_us'], wall, before, after, received))
                            except OrbbecError as exc:
                                # Do not terminate image capture for a failed measurement API.
                                timestamp_status, timestamp_error = 'sdk_error', str(exc)
                                meta.update(sensor_status=timestamp_status, sdk_timestamp_error=timestamp_error)
                        scale = sdk.call('ob_depth_frame_get_value_scale', frame) if stream == 'depth' else 1.
                        bits = sdk.call('ob_video_frame_get_pixel_available_bit_size', frame) if FORMATS.get(fmt) == 'gray16le' else 8
                image = convert_image(data, width, height, fmt, stream=stream, scale_mm=scale,
                                      valid_bits=bits or 16, near_mm=camera.get('depth_min_mm', 200),
                                      far_mm=camera.get('depth_max_mm', 6000), decoder=decoder)
                image.pts, image.time_base = meta['sdk_device_timestamp_us'], Fraction(1, 1_000_000)
                if stream == 'depth':
                    meta.update(depth_scale_mm=scale, depth_preview=True,
                                depth_range_mm=[camera.get('depth_min_mm', 200), camera.get('depth_max_mm', 6000)])
                last_frame = time.monotonic()
                yield image, received, meta
        finally:
            sdk.call('ob_pipeline_stop', pipeline)


if __name__ == '__main__':
    try:
        if len(sys.argv) != 3 or sys.argv[1] != '--inventory':
            raise ValueError('Usage: python -m video_demo.orbbec --inventory SDK_ROOT')
        print('VIDEO_DEMO_SDK_JSON=' + json.dumps(inventory(sys.argv[2]), ensure_ascii=False))
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
