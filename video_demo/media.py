from __future__ import annotations

from fractions import Fraction
import platform
import threading
import time

import av
import numpy as np
from PIL import Image, ImageDraw, ImageFont


def font(size=20):
    for path in ('/System/Library/Fonts/Supplemental/Arial.ttf',
                 'C:/Windows/Fonts/arial.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default(size=size)


class Synthetic:
    def __init__(self, width, height, stream):
        self.width, self.height, self.stream = width, height, stream
        self.label_font = font(max(20, width // 38))
        self.small_font = font(max(14, width // 65))
        # Deterministic detail chart plus continuous motion; not a camera-quality benchmark.
        y, x = np.mgrid[:height, :width]
        self.base = np.stack((22 + x * 20 // width, 31 + y * 25 // height,
                              49 + (x + y) * 20 // (width + height)), axis=-1).astype(np.uint8)

    def frame(self, sequence, elapsed):
        w, h = self.width, self.height
        im = Image.fromarray(self.base.copy())
        d = ImageDraw.Draw(im)
        for x in range(0, w, max(24, w // 20)):
            d.line((x, 0, x, h), fill=(46, 61, 81))
        for y in range(0, h, max(24, h // 12)):
            d.line((0, y, w, y), fill=(46, 61, 81))
        accent = [(70, 224, 196), (88, 160, 255), (229, 161, 83), (193, 129, 247)][self.stream % 4]
        cx = int(w * (.5 + .33 * np.sin(elapsed * 1.6)))
        cy = int(h * (.5 + .22 * np.cos(elapsed * 2.1)))
        r = max(16, h // 13)
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=accent)
        d.line((w // 2, h // 2, cx, cy), fill=accent, width=2)
        for j in range(64):
            x = int(w * .08) + j * max(2, w // 100)
            d.rectangle((x, int(h * .75), x + max(1, w // 200), int(h * .88)),
                        fill=(220, 227, 239) if j % 2 else (10, 18, 29))
        # The same sequence/flash phase is generated for all streams in a tick.
        d.rectangle((w - 75, 26, w - 30, 71), fill=(250, 250, 250) if sequence % 60 < 30 else (10, 14, 22))
        d.text((30, 25), f'SOURCE {self.stream + 1:02d} / SYNTHETIC', font=self.label_font, fill=(226, 235, 245))
        d.text((30, 65), f'FRAME {sequence:07d}    T+{elapsed:08.3f}s', font=self.small_font, fill=accent)
        d.text((30, h - 42), f'{w} x {h}  |  SHARED SOURCE CLOCK', font=self.small_font, fill=(150, 169, 191))
        return np.asarray(im)


class LatestSlot:
    def __init__(self):
        self.cond = threading.Condition()
        self.item = None
        self.replaced = 0

    def put(self, item):
        with self.cond:
            if self.item is not None:
                self.replaced += 1
            self.item = item
            self.cond.notify()

    def take(self, stop):
        with self.cond:
            while self.item is None and not stop.is_set():
                self.cond.wait(.1)
            item, self.item = self.item, None
            return item


def make_encoder(name, width, height, fps, bitrate):
    c = av.CodecContext.create(name, 'w')
    c.width, c.height = width, height
    c.pix_fmt = 'nv12' if name in ('h264_videotoolbox', 'h264_qsv') else 'yuv420p'
    c.colorspace = c.color_primaries = c.color_trc = c.color_range = 1  # BT.709, limited YUV
    c.time_base = Fraction(1, fps)
    c.framerate = Fraction(fps, 1)
    c.bit_rate = bitrate
    c.max_b_frames = 0
    c.gop_size = fps
    c.thread_count = 2
    c.flags |= av.codec.context.Flags.low_delay
    if name == 'libx264':
        c.options = {'preset': 'veryfast', 'tune': 'zerolatency', 'forced-idr': '1',
            'x264-params': f'repeat-headers=1:scenecut=0:bframes=0:rc-lookahead=0:sync-lookahead=0:vbv-maxrate={bitrate // 1000}:vbv-bufsize={max(1, bitrate // fps // 1000)}'}
    elif name == 'h264_videotoolbox':
        c.options = {'realtime': '1', 'allow_sw': '0', 'prio_speed': '1', 'max_ref_frames': '1'}
    elif name == 'h264_nvenc':
        c.options = {'preset': 'p2', 'tune': 'ull', 'rc': 'cbr', 'rc-lookahead': '0', 'zerolatency': '1'}
    elif name == 'h264_qsv':
        c.options = {'preset': 'veryfast', 'async_depth': '1', 'look_ahead': '0'}
    c.open()
    if c.options:
        raise RuntimeError(f'Unconsumed encoder options: {c.options}')
    return c


def encoder_candidates(requested):
    if requested != 'auto':
        return [requested]
    # Prefer the measured lower-latency libx264 baseline on this Mac.
    # Hardware behavior varies with format/resolution; keep explicit HW comparisons available.
    return (['libx264', 'h264_videotoolbox'] if platform.system() == 'Darwin'
            else ['h264_nvenc', 'h264_qsv', 'libx264'])


def prepare_frame(image, pts, encoder, reformatter=None):
    # Preserve captured YUV/NV12 surfaces in the raw queue. No RGB ndarray round trip.
    source = image if isinstance(image, av.VideoFrame) else av.VideoFrame.from_ndarray(image, format='rgb24')
    from av.video.reformatter import VideoReformatter
    matrix = ('ITU709' if source.colorspace == 1 else 'ITU601' if source.colorspace in (5, 6)
              else 'ITU709' if source.height >= 720 else 'ITU601')
    if source.format.name.startswith(('rgb', 'bgr')):
        matrix = 'ITU709'
    # One context per encoder thread reuses libswscale's conversion setup.
    reformatter = reformatter if reformatter is not None else VideoReformatter()
    frame = reformatter.reformat(source, width=encoder.width, height=encoder.height,
        format=encoder.pix_fmt, src_colorspace=matrix, dst_colorspace='ITU709',
        src_color_range=source.color_range, dst_color_range=1, threads=1)
    frame.pts = pts
    frame.time_base = encoder.time_base
    frame.colorspace = frame.color_range = 1
    return frame


def make_decoder(requested='auto'):
    candidates = [requested] if requested != 'auto' else (
        ['videotoolbox', 'software'] if platform.system() == 'Darwin' else ['software'])
    errors = []
    for name in candidates:
        try:
            kwargs = {}
            if name != 'software':
                from av.codec.hwaccel import HWAccel
                kwargs['hwaccel'] = HWAccel(device_type=name, allow_software_fallback=False)
            c = av.CodecContext.create('h264', 'r', **kwargs)
            c.thread_count = 1
            c.flags |= av.codec.context.Flags.low_delay
            c.open()
            return c, name
        except Exception as exc:
            errors.append(f'{name}: {exc}')
    raise RuntimeError('; '.join(errors))


def open_camera(camera, width, height, fps):
    from .cameras import capture_options
    if isinstance(camera, str):
        camera = {'device': camera}
    fmt, url, options = capture_options(camera, width, height, fps)
    try:
        return av.open(url, format=fmt, options=options)
    except av.error.FFmpegError as exc:
        raise RuntimeError(f'摄像头“{camera["device"]}”打开失败：'
                           f'{options["video_size"]}@{options["framerate"]} '
                           f'{options.get("pixel_format", options.get("input_format", "default"))}，'
                           f'后端 {fmt}。请检查设备是否被占用、摄像头权限和连接。原始错误：{exc}') from exc
