from __future__ import annotations

import math
from pathlib import Path
import time

import numpy as np
from PIL import Image, ImageDraw

from .media import font

BG = '#0b111c'
PANEL = '#131e2d'
MUTED = '#8b9bb1'
TEXT = '#edf3fc'
ACCENT = '#5fe0bd'
COLORS = ['#5fe0bd', '#75acff', '#f1b77d', '#c99cf6']


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
        self.text(d, (951, base + 47), 'S  snapshot     Q / Esc  exit', 11, MUTED)
        self.text(d, (24, height - 24), 'Metrics exclude sensor exposure and screen scanout. Synthetic timestamps do not prove physical camera synchronization.', 11, MUTED)
        self.last_image = im
        return im

    def text(self, draw, xy, text, size, color):
        draw.text(xy, text, font=self.fonts[size], fill=color)

    def save(self, path):
        if self.last_image is not None:
            self.last_image.save(path)


class Window:
    def __init__(self, on_snapshot):
        from PySide6 import QtCore, QtGui, QtWidgets
        self.QtCore, self.QtGui, self.QtWidgets = QtCore, QtGui, QtWidgets
        self.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(['VideoLink'])
        self.closed = False
        owner = self

        class Host(QtWidgets.QWidget):
            def closeEvent(self, event):
                owner.closed = True
                event.accept()

        class VideoSurface(QtWidgets.QWidget):
            """Keep RGB memory alive and scale once while painting, without a QPixmap copy."""
            def __init__(self):
                super().__init__()
                self.frame = None
                self.image = None
                self.setAttribute(QtCore.Qt.WidgetAttribute.WA_OpaquePaintEvent)

            def set_frame(self, frame):
                self.frame = frame
                rgb = frame.image
                self.image = QtGui.QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0],
                                         QtGui.QImage.Format.Format_RGB888)
                self.update()

            def paintEvent(self, _event):
                painter = QtGui.QPainter(self)
                painter.fillRect(self.rect(), QtGui.QColor('#101927'))
                if self.image is None:
                    painter.setPen(QtGui.QColor('#8b9bb1'))
                    painter.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter, '等待关键帧…')
                    return
                size = self.image.size().scaled(self.size(), QtCore.Qt.AspectRatioMode.KeepAspectRatio)
                target = QtCore.QRect(QtCore.QPoint((self.width() - size.width()) // 2,
                                                   (self.height() - size.height()) // 2), size)
                painter.drawImage(target, self.image)

        class Plot(QtWidgets.QWidget):
            history = []

            def paintEvent(self, _event):
                painter = QtGui.QPainter(self)
                painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
                width, height = self.width() - 45, self.height() - 8
                painter.setPen(QtGui.QColor('#745a41'))
                painter.drawLine(0, 4, width, 4)
                painter.drawText(width + 5, 15, '100ms')
                painter.setPen(QtGui.QColor('#29364a'))
                painter.drawLine(0, height, width, height)
                for i, values in enumerate(self.history):
                    if len(values) > 1:
                        path = QtGui.QPainterPath()
                        for j, value in enumerate(values[-120:]):
                            x = j * width / 119
                            y = 4 + (height - 4) * (1 - min(max(value, 0), 100) / 100)
                            if j == 0:
                                path.moveTo(x, y)
                            else:
                                path.lineTo(x, y)
                        painter.setPen(QtGui.QPen(QtGui.QColor(COLORS[i % 4]), 1.5))
                        painter.drawPath(path)

        self.root = Host()
        self.root.setWindowTitle('Video / Link · 低延迟多路视频 Demo')
        self.root.setStyleSheet('''
            QWidget { background: #0b111c; color: #edf3fc; font-size: 13px; }
            QFrame#card { background: #131e2d; border-radius: 10px; }
            QFrame#card QLabel { background: transparent; }
            QLabel#muted { color: #8b9bb1; font-size: 12px; }
            QLabel#metric { font-size: 28px; font-weight: 600; }
            QLabel#video { background: #101927; border-radius: 5px; }
            QPushButton { background: #1c3440; color: #5fe0bd; border: 1px solid #2e555c;
                          padding: 8px 18px; border-radius: 6px; }
            QPushButton:hover { background: #284854; }
        ''')
        layout = QtWidgets.QVBoxLayout(self.root)
        layout.setContentsMargins(24, 20, 24, 18)
        layout.setSpacing(16)
        header = QtWidgets.QHBoxLayout()
        heading = QtWidgets.QVBoxLayout()
        brand = QtWidgets.QLabel('VIDEO / LINK')
        brand.setStyleSheet('color: #5fe0bd; font-size: 13px; font-weight: 600;')
        title = QtWidgets.QLabel('低延迟多路视频')
        title.setStyleSheet('font-size: 28px; font-weight: 600;')
        self.subtitle = QtWidgets.QLabel('H.264  /  RTP + UDP  /  真实摄像头回传')
        self.subtitle.setObjectName('muted')
        for widget in (brand, title, self.subtitle):
            heading.addWidget(widget)
        header.addLayout(heading)
        header.addStretch()
        self.status = QtWidgets.QLabel('等待视频流')
        self.status.setStyleSheet('color: #5fe0bd;')
        header.addWidget(self.status)
        button = QtWidgets.QPushButton('保存截图  S')
        button.clicked.connect(on_snapshot)
        header.addWidget(button)
        layout.addLayout(header)
        optical = QtWidgets.QLabel('真实场景 → 屏幕出光延迟：待光学测量    ·    下方数值只表示软件链路耗时')
        optical.setStyleSheet('background: #1c2938; color: #f1b77d; padding: 10px; border-radius: 6px;')
        layout.addWidget(optical)
        metrics = QtWidgets.QHBoxLayout()
        self.values = []
        for label, note in [
            ('软件链路耗时 · P95', '应用取帧/源打戳 → RGB 就绪'),
            ('解码帧率 · 合计', '最近 1 秒的唯一帧'),
            ('RTP 接收码率', '包含 RTP 头和重传'),
            ('应用帧时间偏差', '最近完整组；非曝光同步')]:
            card = QtWidgets.QFrame()
            card.setObjectName('card')
            box = QtWidgets.QVBoxLayout(card)
            box.setContentsMargins(16, 12, 16, 12)
            top, value, bottom = QtWidgets.QLabel(label), QtWidgets.QLabel('--'), QtWidgets.QLabel(note)
            top.setObjectName('muted')
            value.setObjectName('metric')
            bottom.setObjectName('muted')
            for widget in (top, value, bottom):
                box.addWidget(widget)
            metrics.addWidget(card, 1)
            self.values.append(value)
        self.values[0].setStyleSheet('color: #5fe0bd;')
        layout.addLayout(metrics)
        self.grid = QtWidgets.QGridLayout()
        self.grid.setSpacing(14)
        layout.addLayout(self.grid, 1)
        bottom = QtWidgets.QHBoxLayout()
        self.plot = Plot()
        self.plot.setFixedHeight(68)
        plot_label = QtWidgets.QLabel('最近解码延迟')
        plot_label.setObjectName('muted')
        bottom.addWidget(plot_label)
        bottom.addWidget(self.plot, 1)
        self.groups = QtWidgets.QLabel('等待配帧')
        self.groups.setMinimumWidth(220)
        bottom.addWidget(self.groups)
        layout.addLayout(bottom)
        foot = QtWidgets.QLabel('总延迟需外部高速录像/光电测量；软件时间戳不代表曝光时刻或屏幕出光。    Q / Esc 退出')
        foot.setObjectName('muted')
        layout.addWidget(foot)
        self.shortcuts = []
        for key, action in [('Q', self.root.close), ('Escape', self.root.close), ('S', on_snapshot)]:
            shortcut = QtGui.QShortcut(QtGui.QKeySequence(key), self.root)
            shortcut.activated.connect(action)
            self.shortcuts.append(shortcut)
        self.tiles = []
        self.VideoSurface = VideoSurface
        self.last_frames = {}
        self.stats_at = 0

    def show_frames(self, frames, stats, group_info, now, args):
        QtCore, QtGui, QtWidgets = self.QtCore, self.QtGui, self.QtWidgets
        if not self.tiles:
            cols = 1 if args.streams == 1 else 2 if args.streams <= 4 else 4
            for i in range(args.streams):
                card = QtWidgets.QFrame()
                card.setObjectName('card')
                box = QtWidgets.QVBoxLayout(card)
                box.setContentsMargins(12, 10, 12, 10)
                row = QtWidgets.QHBoxLayout()
                title = QtWidgets.QLabel(f'视频流 {i + 1:02d}')
                title.setStyleSheet(f'color: {COLORS[i % 4]}; font-weight: 600;')
                state = QtWidgets.QLabel('等待')
                row.addWidget(title)
                row.addStretch()
                row.addWidget(state)
                box.addLayout(row)
                video = self.VideoSurface()
                video.setObjectName('video')
                video.setMinimumSize(180, 140)
                video.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Expanding)
                box.addWidget(video, 1)
                detail = QtWidgets.QLabel('-- fps  |  -- Mbps')
                note = QtWidgets.QLabel('')
                note.setObjectName('muted')
                note.setToolTip('原“画面年龄” = 当前时间 − 当前保留帧的应用取帧时间（经时钟映射）。\n'
                                '本地相机从图像交给发送程序开始计时；RTSP 从发送端输入解码完成开始计时。\n'
                                'SDK 图像从应用取出 SDK 帧开始计时；设备时间戳另行记录，未作为曝光或共同时间源。\n'
                                '画面停住时仍会增长；跨机尚未校时则显示未知。\n'
                                '不包含相机内部等待或屏幕出光时间，不等于真实场景到屏幕的总延迟。')
                box.addWidget(detail)
                box.addWidget(note)
                self.grid.addWidget(card, i // cols, i % cols)
                self.tiles.append((video, detail, note, state))
            screen = self.app.primaryScreen().availableGeometry()
            self.root.resize(min(1280, screen.width() - 60), min(790 if args.streams <= 2 else 950, screen.height() - 70))
            self.root.show()
            self.app.processEvents()
            self.subtitle.setText(f'{args.streams} 路  /  {args.width} × {args.height} @ {args.fps} fps  /  H.264 · RTP/UDP')
        for stream, frame in frames.items():
            key = (frame.meta.epoch, frame.meta.frame_id)
            video = self.tiles[stream][0]
            if self.last_frames.get(stream) == key:
                continue
            video.set_frame(frame)
            self.last_frames[stream] = key
        if now - self.stats_at < 200_000_000:
            return
        self.stats_at = now
        history = [v for s in stats for v in s['history']]
        labels = [f'{np.percentile(history, 95):.1f} ms' if history else '--',
                  f'{sum(s["fps"] for s in stats):.0f} fps', f'{sum(s["mbps"] for s in stats):.2f} Mbps',
                  f'{group_info["skew"]:.2f} ms' if group_info.get('skew') is not None else '--']
        for widget, text in zip(self.values, labels):
            widget.setText(text)
        live = 0
        for i, s in enumerate(stats):
            _, detail, note, state = self.tiles[i]
            frame = frames.get(i)
            age = (now - frame.local_capture_ns) / 1e6 if frame and frame.local_capture_ns else None
            old = not frame or now - frame.decoded_ns > 200_000_000 or (age is not None and age > args.max_age_ms)
            live += int(not old)
            state.setText('等待' if not frame else '旧帧' if old else '保留上一帧' if i in group_info.get('missing', []) else '实时')
            state.setStyleSheet(f'color: {"#f1b77d" if old else "#5fe0bd"};')
            latency = f'{s["latency_ms"]:.1f} ms' if s['latency_ms'] is not None else '校时中'
            kind = '深度预览 · ' if frame and frame.meta.depth_preview else ''
            detail.setText(f'{kind}{s["fps"]:.0f} fps    |    {s["mbps"]:.2f} Mbps    |    {latency}')
            age_label = f'{age:.0f} ms' if age is not None else '未知'
            note.setText(f'距应用取帧 {age_label}    ·    丢弃 {s["drops"]}    ·    {s["decoder"]}')
        self.status.setText(f'●  {live}/{args.streams} 路接收中')
        mode = '逐路最新画面（不等待对齐）' if args.sync_mode == 'latest' else '按时间戳配帧'
        self.groups.setText(f'{mode}\n完整组 {group_info.get("complete", 0)} / {group_info.get("total", 0)}  ·  {args.clock_mode}')
        self.plot.history = [s['history'] for s in stats]
        self.plot.update()

    def save(self, path):
        self.root.grab().save(str(path))

    def pump(self):
        if not self.closed:
            self.app.processEvents()

    def destroy(self):
        self.root.close()
        self.app.processEvents()
