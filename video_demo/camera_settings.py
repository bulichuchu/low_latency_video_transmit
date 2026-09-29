"""Startup-only camera/output controls. Import Qt only when opening the picker."""
from __future__ import annotations

import math
import queue
import re
import threading

from PySide6 import QtCore, QtGui, QtWidgets

from .cameras import capture_modes, inventory, select_capture_mode
from .network import validate_network_camera
from .sdk import is_sdk, parse_device, validate_camera as validate_sdk_camera
from .sdk import inventory as sdk_inventory, configure_root, configured_root


def device_identifier(record, devices):
    duplicate = sum(d['name'] == record['name'] for d in devices) > 1
    return record.get('device', str(record['index_hint']) if duplicate and 'index_hint' in record else record['name'])


class CameraRow(QtWidgets.QGroupBox):
    """One input: automatic negotiation or a validated, explicit mode tuple."""

    def __init__(self, record, identifier, output, capture_format=None, exact=False, backend=None):
        super().__init__(record['name'])
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Fixed)
        self.setCheckable(True)
        self.setChecked(False)
        self.identifier, self.output = identifier, output
        self.capture_format, self.exact = capture_format, exact
        self.backend = backend
        self.depth_fields = {}
        self.modes = []
        layout = QtWidgets.QVBoxLayout(self)
        self.auto = QtWidgets.QCheckBox('自动匹配输出目标')
        self.auto.setChecked(True)
        self.auto.toggled.connect(self.update_enabled)
        layout.addWidget(self.auto)
        self.controls = QtWidgets.QWidget()
        form = QtWidgets.QGridLayout(self.controls)
        form.setContentsMargins(0, 0, 0, 0)
        self.size = QtWidgets.QComboBox()
        self.pixel = QtWidgets.QComboBox()
        self.rate = QtWidgets.QComboBox()
        self.rate.setEditable(True)
        validator = QtGui.QDoubleValidator(1, 120, 6, self.rate)
        validator.setNotation(QtGui.QDoubleValidator.Notation.StandardNotation)
        validator.setLocale(QtCore.QLocale.c())
        self.rate.lineEdit().setValidator(validator)
        for col, (label, widget, name) in enumerate([
                ('采集分辨率', self.size, 'captureSize'),
                ('像素格式', self.pixel, 'capturePixel'),
                ('采集帧率 (fps)', self.rate, 'captureFps')]):
            widget.setObjectName(name)
            form.addWidget(QtWidgets.QLabel(label), 0, col)
            form.addWidget(widget, 1, col)
        self.size.currentTextChanged.connect(self.update_pixels)
        self.pixel.currentTextChanged.connect(self.update_rates)
        self.rate.currentTextChanged.connect(self.update_hint)
        layout.addWidget(self.controls)
        self.hint = QtWidgets.QLabel()
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        if record.get('sdk_stream') == 'depth':
            depth = QtWidgets.QHBoxLayout()
            for key, label, value in [('depth_min_mm', '近端 (mm)', 200), ('depth_max_mm', '远端 (mm)', 6000)]:
                field = QtWidgets.QSpinBox()
                field.setRange(0, 100000)
                field.setValue(value)
                field.setObjectName(key)
                self.depth_fields[key] = field
                depth.addWidget(QtWidgets.QLabel(label))
                depth.addWidget(field)
            layout.addLayout(depth)
            note = QtWidgets.QLabel('深度转灰度预览：近亮远暗，0 为无效黑色；不传输可测距的原始深度。')
            note.setWordWrap(True)
            layout.addWidget(note)
        self.set_record(record)
        self.update_enabled()

    def set_record(self, record):
        # Only offer modes representable by the generic capture profile.
        self.modes = [m for m in capture_modes(record, self.backend) if
                      64 <= m['width'] <= 4096 and 64 <= m['height'] <= 2160 and
                      m['min_fps'] <= 120.001 and m['max_fps'] >= .999]
        previous = self.size.currentData()
        sizes = sorted({(m['width'], m['height']) for m in self.modes})
        self.size.blockSignals(True)
        self.size.clear()
        self.size.setEditable(not bool(sizes))
        if sizes:
            for width, height in sizes:
                self.size.addItem(f'{width} × {height}', [width, height])
            try:
                chosen = self.automatic_settings()
                preferred = previous or [chosen['width'], chosen['height']]
            except ValueError:
                preferred = list(sizes[0])
            index = self.size.findData(preferred)
            self.size.setCurrentIndex(max(0, index))
        else:
            self.size.addItem(f'{self.output["width"]} × {self.output["height"]}')
        self.size.blockSignals(False)
        self.update_pixels()

    def dimensions(self):
        if self.modes:
            size = self.size.currentData()
            if size is None:
                raise ValueError('请选择设备支持的采集分辨率。')
            return tuple(size)
        match = re.fullmatch(r'\s*(\d+)\s*[x×X]\s*(\d+)\s*', self.size.currentText())
        if not match or not (64 <= int(match[1]) <= 4096 and 64 <= int(match[2]) <= 2160):
            raise ValueError('采集分辨率请填写 宽 × 高，宽 64–4096，高 64–2160。')
        return int(match[1]), int(match[2])

    def matching_modes(self):
        size = self.dimensions()
        return [m for m in self.modes if (m['width'], m['height']) == size]

    def update_pixels(self):
        previous = self.pixel.currentText() or self.capture_format or 'nv12'
        self.pixel.blockSignals(True)
        self.pixel.clear()
        self.pixel.setEditable(not bool(self.modes))
        pixels = sorted({m['pixel_format'] for m in self.matching_modes()}) if self.modes else ['后端默认', 'nv12', 'yuyv422', 'uyvy422', 'mjpeg']
        self.pixel.addItems(pixels)
        self.pixel.setCurrentIndex(max(0, self.pixel.findText(previous)))
        self.pixel.blockSignals(False)
        self.update_rates()

    def update_rates(self):
        previous = self.rate.currentText()
        rates = {1., 5., 10., 15., 20., 24., 25., 29.97, 30., 48., 50., 59.94, 60., 90., 120.}
        rates.add(float(self.output['fps']))
        matching = [m for m in self.matching_modes() if m['pixel_format'] == self.pixel.currentText()] if self.modes else []
        if matching:
            for mode in matching:
                for key in ('min_fps', 'max_fps'):
                    value = mode[key]
                    rates.add(float(round(value)) if abs(value - round(value)) < .001 else value)
            rates = {r for r in rates if 1 <= r <= 120 and any(m['min_fps'] - .001 <= r <= m['max_fps'] + .001 for m in matching)}
        self.rate.blockSignals(True)
        self.rate.clear()
        self.rate.addItems([f'{r:.6f}'.rstrip('0').rstrip('.') for r in sorted(rates)])
        try:
            old = float(previous)
            valid = 1 <= old <= 120 and (not matching or any(m['min_fps'] - .001 <= old <= m['max_fps'] + .001 for m in matching))
        except ValueError:
            valid = False
        selected = ''
        if valid:
            selected = previous
        elif rates:
            selected = f'{min(rates, key=lambda r: abs(r - self.output["fps"])):.6f}'.rstrip('0').rstrip('.')
        self.rate.setCurrentText(selected)
        self.rate.blockSignals(False)
        self.update_hint()

    def automatic_settings(self):
        settings = {'device': self.identifier}
        if self.capture_format:
            settings['pixel_format'] = self.capture_format
        if self.modes:
            return select_capture_mode(settings, self.modes, self.output['width'], self.output['height'],
                                       self.output['fps'], exact=self.exact)
        return settings

    def settings(self):
        settings = self.capture_settings()
        if is_sdk(self.identifier):
            settings['label'] = self.title()
            settings.update({key: field.value() for key, field in self.depth_fields.items()})
            validate_sdk_camera(settings)
        return settings

    def capture_settings(self):
        if self.auto.isChecked():
            return self.automatic_settings()
        width, height = self.dimensions()
        try:
            rate = float(self.rate.currentText())
        except ValueError:
            raise ValueError('请输入有效采集帧率。') from None
        if not math.isfinite(rate) or not 1 <= rate <= 120:
            raise ValueError('采集帧率范围为 1–120 fps。')
        settings = dict(device=self.identifier, width=width, height=height, fps=rate)
        pixel = self.pixel.currentText().strip()
        if pixel and pixel != '后端默认':
            settings['pixel_format'] = pixel
        if self.modes:
            return select_capture_mode(settings, self.modes, width, height, rate, exact=True)
        return settings

    def update_enabled(self):
        self.controls.setEnabled(not self.auto.isChecked())
        if self.auto.isChecked():
            self.sync_automatic_controls()
        self.update_hint()

    def sync_automatic_controls(self):
        if self.modes:
            try:
                chosen = self.automatic_settings()
            except ValueError:
                return
            self.size.setCurrentIndex(self.size.findData([chosen['width'], chosen['height']]))
            self.pixel.setCurrentText(chosen['pixel_format'])
            self.rate.setCurrentText(f'{chosen["fps"]:.6f}'.rstrip('0').rstrip('.'))

    def update_hint(self):
        if not self.modes:
            self.hint.setText('未取得可用模式列表；可手动填写，但设备是否支持需启动时验证。')
            return
        try:
            chosen = self.settings()
            ranges = sorted({(f'{m["min_fps"]:g}' if m['min_fps'] == m['max_fps'] else
                              f'{m["min_fps"]:g}–{m["max_fps"]:g}') for m in self.modes
                             if (m['width'], m['height'], m['pixel_format']) ==
                             (chosen['width'], chosen['height'], chosen['pixel_format'])})
            note = ('当前采集接口仅开放各范围最高帧率；更低传输帧率可在下方设置。' if self.backend == 'avfoundation'
                    else '连续范围内可手动输入。')
            self.hint.setText(f'采集 {chosen["width"]} × {chosen["height"]} @ {chosen["fps"]:g} fps · {chosen["pixel_format"]}'
                              f'；可选帧率：{" / ".join(ranges)} fps。{note}')
        except ValueError as exc:
            self.hint.setText(str(exc))


class CameraSettingsDialog(QtWidgets.QDialog):
    """Discover modes asynchronously; return a profile only after validation."""

    def __init__(self, output, capture_format=None, exact=False):
        super().__init__()
        self.setWindowTitle('选择摄像头与画质')
        self.resize(850, 720)
        self.output = dict(output)
        self.capture_format, self.exact = capture_format, exact
        self.rows = []
        self.profile = None
        self.loading = True
        layout = QtWidgets.QVBoxLayout(self)
        intro = QtWidgets.QLabel('先勾选摄像头，再设置采集模式与传输画质。点击“开始传输”后生效。\n深度设备在这里仅作视频预览，不代表原始深度数据传输。')
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.status = QtWidgets.QLabel('正在读取设备支持的采集模式…（尚未采集画面）')
        layout.addWidget(self.status)
        sdk_bar = QtWidgets.QHBoxLayout()
        self.sdk_button = QtWidgets.QPushButton('查询 SDK 摄像头')
        self.sdk_button.clicked.connect(self.start_sdk_query)
        self.sdk_path_button = QtWidgets.QPushButton('选择 Orbbec SDK 目录…')
        self.sdk_path_button.clicked.connect(self.select_sdk_root)
        sdk_bar.addWidget(self.sdk_button)
        sdk_bar.addWidget(self.sdk_path_button)
        sdk_bar.addStretch()
        layout.addLayout(sdk_bar)
        self.sdk_status = QtWidgets.QLabel('SDK 图像支持彩色 / 红外 / 深度预览；同一台 SDK 相机一次选择一种图像流。')
        self.sdk_status.setWordWrap(True)
        layout.addWidget(self.sdk_status)
        self.sdk_loading = False
        self.sdk_widgets = []
        self.sdk_results = queue.Queue(maxsize=1)
        self.sdk_timer = QtCore.QTimer(self)
        self.sdk_timer.timeout.connect(self.poll_sdk)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        holder = QtWidgets.QWidget()
        self.camera_layout = QtWidgets.QVBoxLayout(holder)
        self.camera_layout.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetMinAndMaxSize)
        self.camera_layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignTop)
        scroll.setWidget(holder)
        layout.addWidget(scroll, 1)
        network = QtWidgets.QLabel('网络 / PoE 摄像机：每行一个 RTSP 地址。源分辨率、帧率和码率在相机后台设置；下面调整本程序转发画质。')
        network.setWordWrap(True)
        layout.addWidget(network)
        self.urls = QtWidgets.QPlainTextEdit()
        self.urls.setObjectName('rtspUrls')
        self.urls.setPlaceholderText('rtsp://192.168.1.100:554/实际视频路径\nrtsp://用户名:密码@192.168.1.101:554/实际视频路径')
        self.urls.setMaximumHeight(75)
        layout.addWidget(self.urls)
        group = QtWidgets.QGroupBox('传输画质 · 所有视频流共用')
        form = QtWidgets.QGridLayout(group)
        self.preset = QtWidgets.QComboBox()
        self.preset.setObjectName('outputPreset')
        for label, size in [('640 × 480', (640, 480)), ('960 × 540', (960, 540)),
                            ('1280 × 720', (1280, 720)), ('1920 × 1080', (1920, 1080)),
                            ('2560 × 1440', (2560, 1440)), ('3840 × 2160', (3840, 2160)), ('自定义', None)]:
            self.preset.addItem(label, list(size) if size else None)
        self.fields = {}
        for col, (key, label, low, high, step) in enumerate([
                ('width', '输出宽', 64, 3840, 2), ('height', '输出高', 64, 2160, 2),
                ('fps', '帧率上限 (fps)', 1, 120, 1), ('bitrate_kbps', '每路目标码率 (kbps)', 100, 1_000_000, 100)]):
            field = QtWidgets.QSpinBox()
            field.setObjectName('output_' + key)
            field.setRange(low, high)
            field.setSingleStep(step)
            field.setValue(output[key])
            field.valueChanged.connect(self.output_changed)
            self.fields[key] = field
            form.addWidget(QtWidgets.QLabel(label), 1, col)
            form.addWidget(field, 2, col)
        form.addWidget(QtWidgets.QLabel('分辨率预设'), 0, 0)
        form.addWidget(self.preset, 0, 1)
        self.preset.currentIndexChanged.connect(self.apply_preset)
        self.output_note = QtWidgets.QLabel()
        self.output_note.setWordWrap(True)
        form.addWidget(self.output_note, 3, 0, 1, 4)
        layout.addWidget(group)
        self.error = QtWidgets.QLabel()
        self.error.setObjectName('settingsError')
        self.error.setStyleSheet('color: #c04434;')
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok |
                                            QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText('开始传输')
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).setText('取消')
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.output_changed()
        # The worker owns no Qt objects. A closed dialog can leave it to finish
        # its bounded metadata query without touching deleted widgets.
        self.results = queue.Queue(maxsize=1)
        results = self.results

        def discover():
            try:
                results.put((inventory(include_modes=True), None))
            except Exception:
                try:
                    data = inventory(include_modes=False)
                except Exception:
                    data = {'devices': []}
                results.put((data, '完整模式查询失败，可填写采集参数或使用 RTSP。'))

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.poll_inventory)
        self.timer.start(50)
        threading.Thread(target=discover, name='camera-inventory', daemon=True).start()

    def select_sdk_root(self):
        path = QtWidgets.QFileDialog.getExistingDirectory(self, '选择 Orbbec SDK v2 根目录')
        if path:
            try:
                configure_root(path)
            except (ValueError, OSError) as exc:
                self.sdk_status.setText(str(exc))
                return
            self.start_sdk_query()

    def start_sdk_query(self):
        if self.sdk_loading:
            return
        if configured_root() is None:
            self.sdk_status.setText('尚未配置 SDK。请点击“选择 Orbbec SDK 目录”，选择包含 lib / bin 的 SDK v2 根目录。')
            return
        self.sdk_loading = True
        self.sdk_button.setEnabled(False)
        self.sdk_path_button.setEnabled(False)
        self.sdk_status.setText('正在读取 SDK 设备和图像流能力…尚未开始采集。')
        results = self.sdk_results

        def discover_sdk():
            try:
                results.put((sdk_inventory(), None))
            except Exception as exc:
                results.put((None, str(exc)))

        self.sdk_timer.start(50)
        threading.Thread(target=discover_sdk, name='sdk-inventory', daemon=True).start()

    def poll_sdk(self):
        try:
            data, error = self.sdk_results.get_nowait()
        except queue.Empty:
            return
        self.sdk_timer.stop()
        self.sdk_loading = False
        self.sdk_button.setEnabled(True)
        self.sdk_path_button.setEnabled(True)
        for widget in self.sdk_widgets:
            if widget in self.rows:
                self.rows.remove(widget)
            self.camera_layout.removeWidget(widget)
            widget.deleteLater()
        self.sdk_widgets.clear()
        if error:
            self.sdk_status.setText('SDK 查询失败：' + error)
            return
        for record in data['devices']:
            row = CameraRow(record, record['device'], self.output, self.capture_format, self.exact, backend='orbbec')
            self.rows.append(row)
            self.sdk_widgets.append(row)
            self.camera_layout.addWidget(row)
        for record in data.get('unavailable', []):
            notice = QtWidgets.QLabel(f'{record["name"]} [{record["serial"]}] · SDK 暂不可用\n{record["error"]}')
            notice.setWordWrap(True)
            notice.setStyleSheet('color: #b35b27; padding: 8px;')
            self.sdk_widgets.append(notice)
            self.camera_layout.addWidget(notice)
        self.sdk_status.setText(f'SDK 可用图像流：{len(data["devices"])}；无法访问的设备：{len(data.get("unavailable", []))}。'
                               '请勿同时勾选同一物理相机的普通 USB 和 SDK 入口。')

    def poll_inventory(self):
        try:
            data, error = self.results.get_nowait()
        except queue.Empty:
            return
        self.timer.stop()
        self.loading = False
        devices = data['devices']
        for record in devices:
            row = CameraRow(record, device_identifier(record, devices), self.output, self.capture_format, self.exact,
                            backend=data.get('backend'))
            self.rows.append(row)
            self.camera_layout.addWidget(row)
        available = sum(bool(row.modes) for row in self.rows)
        self.status.setText(error or f'发现 {len(devices)} 个本地摄像头，其中 {available} 个有可用模式列表。尚未采集画面。')

    def output_changed(self):
        self.output.update({key: widget.value() for key, widget in self.fields.items()})
        self.preset.blockSignals(True)
        index = self.preset.findData([self.output['width'], self.output['height']])
        self.preset.setCurrentIndex(index if index >= 0 else self.preset.count() - 1)
        self.preset.blockSignals(False)
        self.output_note.setText(f'每路目标 {self.output["bitrate_kbps"] / 1000:g} Mbps，实际码率随内容变化。'
                                '输出不会补出相机没有的帧或细节；提高分辨率和帧率会增加处理负载。')
        for row in self.rows:
            if row.auto.isChecked():
                row.sync_automatic_controls()
            row.update_hint()

    def apply_preset(self):
        size = self.preset.currentData()
        if size:
            # Avoid intermediate sizes resetting the preset and source hints.
            for key, value in zip(('width', 'height'), size):
                self.fields[key].blockSignals(True)
                self.fields[key].setValue(value)
                self.fields[key].blockSignals(False)
            self.output_changed()

    def accept(self):
        self.output_changed()
        try:
            if self.output['width'] % 2 or self.output['height'] % 2:
                raise ValueError('输出宽、高必须是偶数。')
            cameras = [row.settings() for row in self.rows if row.isChecked()]
            serials = [parse_device(c['device'])[0] for c in cameras if is_sdk(c['device'])]
            if len(serials) != len(set(serials)):
                raise ValueError('同一台 SDK 相机一次选择一种图像流，请取消重复序列号的勾选。')
            for url in self.urls.toPlainText().splitlines():
                if url.strip():
                    camera = {'device': url.strip()}
                    validate_network_camera(camera)
                    cameras.append(camera)
            if not 1 <= len(cameras) <= 8:
                raise ValueError('请选择或填写 1–8 路摄像头。' + ('本地设备仍在查询中。' if self.loading else ''))
        except ValueError as exc:
            self.error.setText(str(exc))
            return
        self.profile = {'version': 1, 'cameras': cameras, 'output': dict(self.output)}
        super().accept()


def select_camera_profile(output, capture_format=None, exact=False):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(['VideoLink camera settings'])
    dialog = CameraSettingsDialog(output, capture_format, exact)
    print('[camera] 请在窗口选择摄像头、采集模式与输出画质；尚未开始采集。', flush=True)
    if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
        raise ValueError('Camera selection cancelled')
    return dialog.profile
