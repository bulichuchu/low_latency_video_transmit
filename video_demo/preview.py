"""Optional static PNG report for command-line receive measurements."""
from __future__ import annotations

import math

import numpy as np
from PIL import Image, ImageDraw, ImageFont

BG = '#0b111c'
PANEL = '#131e2d'
MUTED = '#8b9bb1'
TEXT = '#edf3fc'
ACCENT = '#5fe0bd'
COLORS = ['#5fe0bd', '#75acff', '#f1b77d', '#c99cf6']


def font(size=20):
    for path in ('/System/Library/Fonts/Supplemental/Arial.ttf',
                 'C:/Windows/Fonts/arial.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default(size=size)


class Dashboard:
    def __init__(self, args):
        self.args = args
        self.fonts = {s: font(s) for s in (11, 12, 14, 16, 20, 26, 30, 34)}
        self.width = 1280
        self.last_image = None

    def draw(self, frames, stats, group_info, now):
        count = self.args.streams
        cols = 1 if count == 1 else 2 if count <= 4 else 4
        rows = math.ceil(count / cols)
        tile_w = (self.width - 48 - 16 * (cols - 1)) // cols
        tile_h = int(tile_w * self.args.height / self.args.width)
        # Keep 4/8-stream views within a normal laptop window.
        tile_h = min(tile_h, 344 if rows == 1 else 220)
        height = 238 + rows * (tile_h + 62) + (rows - 1) * 16 + 122
        im = Image.new('RGB', (self.width, height), BG)
        d = ImageDraw.Draw(im)
        self.text(d, (24, 20), 'VIDEO / LINK', 14, ACCENT)
        self.text(d, (24, 43), 'Low-latency video lab', 30, TEXT)
        self.text(d, (24, 84), f'{count} STREAMS   /   H.264 + RTP/UDP   /   {self.args.width} x {self.args.height} @ {self.args.fps}', 12, MUTED)
        active = sum(bool(s['last_frame_ns'] and now - s['last_frame_ns'] < 250_000_000) for s in stats)
        d.ellipse((1091, 32, 1100, 41), fill=ACCENT if active else '#f1b77d')
        self.text(d, (1112, 25), f'{active}/{count} RECEIVING', 12, TEXT)
        self.text(d, (982, 57), 'APPLICATION TIMING', 14, MUTED)
        all_lat = [value for s in stats for value in s['history']]
        p95 = float(np.percentile(all_lat, 95)) if all_lat else None
        skew = group_info.get('skew')
        values = [f'{p95:.1f} ms' if p95 is not None else '--',
                  f'{sum(s["fps"] for s in stats):.0f} fps',
                  f'{sum(s["mbps"] for s in stats):.2f} Mbps',
                  f'{skew:.2f} ms' if skew is not None else '--']
        labels = ['DECODE LATENCY / P95', 'DECODED FRAMES / TOTAL', 'RTP RECEIVE BITRATE', 'CAPTURE SKEW / LAST FULL GROUP']
        notes = ['Source stamp -> RGB ready', 'Unique frames / last 1 second', 'Includes RTP headers', 'Source timestamps, not exposure proof']
        cw = (self.width - 48 - 3 * 12) // 4
        for i in range(4):
            x = 24 + i * (cw + 12)
            d.rounded_rectangle((x, 115, x + cw, 217), radius=10, fill=PANEL)
            self.text(d, (x + 16, 128), labels[i], 11, MUTED)
            self.text(d, (x + 16, 149), values[i], 26, ACCENT if i == 0 else TEXT)
            self.text(d, (x + 16, 187), notes[i], 11, MUTED)
        for i in range(count):
            x = 24 + (i % cols) * (tile_w + 16)
            y = 238 + (i // cols) * (tile_h + 78)
            d.rounded_rectangle((x, y, x + tile_w, y + tile_h + 62), radius=10, fill=PANEL)
            frame = frames.get(i)
            stale = True
            if frame is not None:
                source = Image.fromarray(frame.image)
                source.thumbnail((tile_w, tile_h), Image.Resampling.BILINEAR)
                im.paste(source, (x + (tile_w - source.width) // 2, y + (tile_h - source.height) // 2))
                age = (now - frame.local_capture_ns) / 1e6 if frame.local_capture_ns else None
                stale = now - frame.decoded_ns > 200_000_000 or (age is not None and age > self.args.max_age_ms)
            else:
                age = None
                self.text(d, (x + 24, y + tile_h // 2), 'Waiting for an IDR frame...', 16, MUTED)
            accent = COLORS[i % len(COLORS)]
            d.rounded_rectangle((x + 12, y + 12, x + 116, y + 40), radius=5, fill=BG)
            self.text(d, (x + 22, y + 18), f'STREAM {i + 1:02d}', 12, accent)
            badge = 'STALE' if frame and stale else 'HELD' if frame and i in group_info.get('missing', []) else 'LIVE' if frame else 'WAIT'
            d.rounded_rectangle((x + tile_w - 74, y + 12, x + tile_w - 12, y + 40), radius=5, fill=BG)
            self.text(d, (x + tile_w - 63, y + 18), badge, 12, '#f1b77d' if stale else ACCENT)
            s = stats[i]
            if frame and frame.meta.depth_preview:
                self.text(d, (x + 14, y + tile_h - 24), 'DEPTH PREVIEW / NOT RAW DEPTH', 11, '#f1b77d')
            latency = f'{s["latency_ms"]:.1f}ms' if s['latency_ms'] is not None else 'uncalibrated'
            self.text(d, (x + 14, y + tile_h + 11), f'{s["fps"]:2.0f} fps   |   {s["mbps"]:.2f} Mbps   |   {latency}', 12, TEXT)
            age_label = f'{age:.0f}ms' if age is not None else 'unknown'
            self.text(d, (x + 14, y + tile_h + 34), f'Since app capture {age_label}   /   Drops {s["drops"]}   /   {s["decoder"]}', 11, MUTED)
        base = height - 104
        self.text(d, (24, base), 'RECENT DECODE LATENCY', 11, MUTED)
        gx, gy, gw, gh = 250, base + 2, 570, 64
        d.line((gx, gy + gh, gx + gw, gy + gh), fill='#29364a')
        d.line((gx, gy, gx + gw, gy), fill='#795941')
        self.text(d, (gx + gw + 10, gy - 5), '100 ms', 11, '#f1b77d')
        self.text(d, (gx + gw + 10, gy + gh - 7), '0', 11, MUTED)
        for i, s in enumerate(stats):
            history = s['history'][-120:]
            if len(history) > 1:
                points = [(gx + j * gw / 119, gy + gh * (1 - min(v, 100) / 100)) for j, v in enumerate(history)]
                d.line(points, fill=COLORS[i % 4], width=2)
        full, total = group_info.get('complete', 0), group_info.get('total', 0)
        self.text(d, (951, base), f'FULL GROUPS  {full}/{total}', 12, TEXT)
        self.text(d, (951, base + 24), f'CLOCK  {self.args.clock_mode.upper()}', 11, MUTED)
        self.text(d, (951, base + 47), 'OFFLINE PREVIEW', 11, MUTED)
        self.text(d, (24, height - 24), 'Metrics exclude sensor exposure and screen scanout. Application timestamps do not prove physical camera synchronization.', 11, MUTED)
        self.last_image = im
        return im

    def text(self, draw, xy, text, size, color):
        draw.text(xy, text, font=self.fonts[size], fill=color)

    def save(self, path):
        if self.last_image is not None:
            self.last_image.save(path)
