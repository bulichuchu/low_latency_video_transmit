from __future__ import annotations

from collections import Counter, defaultdict, deque
from dataclasses import asdict
import csv
import json
from pathlib import Path
import queue
import threading
import time

import numpy as np

from .network import redact


class Journal:
    """Disk I/O stays off the media threads; lost telemetry is explicitly counted."""
    def __init__(self, directory, config):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / 'config.json').write_text(json.dumps(redact(config), indent=2, ensure_ascii=False))
        self.queue = queue.Queue(maxsize=50000)
        self.lost = 0
        self.error = None
        self.thread = threading.Thread(target=self._write, name='metrics-writer', daemon=True)
        self.thread.start()

    def log(self, kind, meta=None, **fields):
        row = {'event': kind, 'time_ns': time.perf_counter_ns()}
        if meta is not None:
            row.update(asdict(meta))
        row.update(fields)
        try:
            self.queue.put_nowait(redact(row))
        except queue.Full:
            self.lost += 1

    def _write(self):
        try:
            with (self.directory / 'events.jsonl').open('w', buffering=1024 * 1024) as f:
                while True:
                    try:
                        row = self.queue.get(timeout=.5)
                    except queue.Empty:
                        f.flush()
                        continue
                    if row is None:
                        break
                    f.write(json.dumps(row, ensure_ascii=False) + '\n')
                    if time.perf_counter() - getattr(self, '_flushed', 0) > 1:
                        f.flush()
                        self._flushed = time.perf_counter()
                f.write(json.dumps({'event': 'telemetry', 'lost_events': self.lost}) + '\n')
        except Exception as exc:
            self.error = str(exc)

    def close(self):
        # A failed writer must not deadlock shutdown on a full queue.
        while self.thread.is_alive():
            try:
                self.queue.put(None, timeout=0.1)
                break
            except queue.Full:
                continue
        self.thread.join(timeout=5)
        if self.thread.is_alive() or self.error:
            raise RuntimeError(f'metrics writer failed: {self.error or "shutdown timeout"}')
        return summarize(self.directory)


class LiveStats:
    def __init__(self, streams):
        self.lock = threading.Lock()
        self.rows = [dict(decoded=0, presented=0, drops=0, wire_bytes=0,
                          latency_ms=None, decoder='waiting', last_frame_ns=0,
                          history=deque(maxlen=120), events=deque(), counts=Counter()) for _ in range(streams)]

    def add(self, stream, event, value=1, **fields):
        now = time.perf_counter_ns()
        with self.lock:
            row = self.rows[stream]
            if event in row:
                row[event] += value
            row.update(fields)
            if event in ('decoded', 'presented', 'wire_bytes'):
                row['events'].append((now, event, value))
                row['counts'][event] += value
            if event == 'decoded':
                row['last_frame_ns'] = now
                if row['latency_ms'] is not None:
                    row['history'].append(row['latency_ms'])

    def snapshot(self):
        now = time.perf_counter_ns()
        with self.lock:
            result = []
            for row in self.rows:
                while row['events'] and now - row['events'][0][0] > 1_000_000_000:
                    _, event, value = row['events'].popleft()
                    row['counts'][event] -= value
                counts = row['counts']
                snap = {k: v for k, v in row.items() if k not in ('events', 'counts')}
                snap['history'] = list(row['history'])
                snap['fps'] = counts['decoded']
                snap['submit_fps'] = counts['presented']
                snap['mbps'] = counts['wire_bytes'] * 8 / 1e6
                result.append(snap)
            return result


def distribution(values):
    if not values:
        return {'samples': 0, 'mean': None, 'p50': None, 'p95': None, 'p99': None, 'max': None}
    return {'samples': len(values), 'mean': float(np.mean(values)),
            **{f'p{p}': float(np.percentile(values, p)) for p in (50, 95, 99)},
            'max': float(max(values))}


def summarize(directory):
    directory = Path(directory)
    config = json.loads((directory / 'config.json').read_text())
    summary = {'measurement': 'application timestamps; NOT camera-to-photon latency',
               'role': config.get('command'), 'headless': config.get('headless', False), 'streams': {},
               'groups': {'total': 0, 'complete': 0}, 'errors': [], 'telemetry_lost_events': 0}
    stats = defaultdict(lambda: {'counts': Counter(), 'drops': Counter(),
        'decode': [], 'submit': [], 'selected': [], 'browser_submit': [], 'browser_times': [], 'encode': [], 'uncertainty': [],
        'decode_times': [], 'presentation_times': [], 'wire_bytes': 0, 'rtx_bytes': 0, 'encoded_bytes': 0,
        'decoders': set(), 'encoders': set(), 'injected_packet_drops': 0,
        'camera': None, 'capture_fps': [], 'sender_totals': None, 'stages': defaultdict(list),
        'sensor_latencies': defaultdict(list), 'sensor_uncertainty': [], 'sensor_states': Counter()})
    stage_fields = ['raw_queue_ms', 'prepare_ms', 'codec_encode_ms', 'assembly_ms',
                    'decode_queue_ms', 'codec_decode_ms', 'rgb_convert_ms',
                    'match_wait_ms', 'ui_wait_ms', 'ui_work_ms',
                    'browser_decode_ms', 'browser_wait_ms', 'browser_draw_ms']
    fields = ['event', 'time_ns', 'stream', 'epoch', 'frame_id', 'capture_ns', 'encode_us',
              'latency_ms', 'clock_uncertainty_ms', 'decode_ms', 'frame_bytes', 'wire_bytes',
              'key', 'depth_preview', 'reason', 'sync_skew_ms', 'complete', 'decoder', 'browser_submit_ms',
              'sensor_capture_ns', 'sdk_device_timestamp_us', 'sdk_global_timestamp_us', 'sensor_status',
              'sensor_clock_uncertainty_us', 'sensor_latency_ms', 'sensor_clock_uncertainty_ms',
              'sensor_measurement_status', *stage_fields]
    skew_values = []
    visible_skew = []
    browser_skew = []
    visible_groups = visible_stale = 0
    times = []
    sample_fields = ['time_ns', 'stream', 'fps', 'fps_measurement', 'rtp_mbps', 'decoded', 'drops',
                     'wire_bytes', 'clock_offset_ns', 'clock_uncertainty_ms']
    with (directory / 'events.jsonl').open() as source, (directory / 'frames.csv').open('w', newline='') as dest, \
            (directory / 'metrics.csv').open('w', newline='') as samples, \
            (directory / 'camera_metrics.csv').open('w', newline='') as camera_samples:
        writer = csv.DictWriter(dest, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        sample_writer = csv.DictWriter(samples, fieldnames=sample_fields, extrasaction='ignore')
        sample_writer.writeheader()
        camera_writer = csv.DictWriter(camera_samples, fieldnames=['time_ns', 'stream', 'capture_fps',
            'captured', 'raw_overwritten', 'device_pts', 'device_time_base', 'sdk_device_timestamp_us',
            'sdk_system_timestamp_us', 'sdk_global_timestamp_us', 'sensor_capture_ns', 'sensor_status',
            'sensor_clock_uncertainty_us', 'sdk_format', 'sdk_stream', 'depth_scale_mm', 'depth_preview',
            'depth_range_mm'], extrasaction='ignore')
        camera_writer.writeheader()
        for line in source:
            row = json.loads(line)
            event = row['event']
            if 'time_ns' in row:
                times.append(row['time_ns'])
            if event == 'telemetry':
                summary['telemetry_lost_events'] = row['lost_events']
            if event == 'error':
                summary['errors'].append(row)
            if event == 'matcher_totals':
                summary['matcher_discarded_frames'] = row['discarded']
            if event == 'sample':
                sample_writer.writerow(row)
            if event == 'group':
                summary['groups']['total'] += 1
                summary['groups']['complete'] += int(row['complete'])
                if row['complete'] and row.get('sync_skew_ms') is not None:
                    skew_values.append(row['sync_skew_ms'])
                writer.writerow(row)
            if event == 'visible_group':
                visible_groups += 1
                visible_stale += bool(row.get('stale_streams'))
                if row['complete'] and row.get('sync_skew_ms') is not None:
                    visible_skew.append(row['sync_skew_ms'])
                writer.writerow(row)
            if event == 'browser_submit' and row.get('sync_skew_ms') is not None:
                browser_skew.append(row['sync_skew_ms'])
            if 'stream' not in row:
                continue
            s = stats[row['stream']]
            s['counts'][event] += 1
            for name in stage_fields:
                if row.get(name) is not None:
                    s['stages'][name].append(row[name])
            if event == 'camera_opened':
                s['camera'] = row
            elif event == 'camera_sample':
                s['capture_fps'].append(row['capture_fps'])
                camera_writer.writerow(row)
            elif event == 'sender_totals':
                s['sender_totals'] = row
            if event in ('decode', 'submit', 'selected', 'browser_submit'):
                if event != 'selected':
                    s['sensor_states'][row.get('sensor_measurement_status', row.get('sensor_status', 'unavailable'))] += 1
                    if row.get('sensor_status') == 'ready' and row.get('sensor_latency_ms') is not None:
                        s['sensor_latencies'][event].append(row['sensor_latency_ms'])
                        if row.get('sensor_clock_uncertainty_ms') is not None:
                            s['sensor_uncertainty'].append(row['sensor_clock_uncertainty_ms'])
                if row.get('latency_ms') is not None:
                    s[event].append(row['latency_ms'])
                if row.get('clock_uncertainty_ms') is not None:
                    s['uncertainty'].append(row['clock_uncertainty_ms'])
                if event == 'decode':
                    s['decoders'].add(row['decoder'])
                    s['decode_times'].append(row['time_ns'])
                    s['encode'].append(row['encode_us'] / 1000)
                elif event == 'submit':
                    s['presentation_times'].append(row['time_ns'])
                elif event == 'browser_submit' and row.get('browser_submit_ms') is not None:
                    s['browser_times'].append(row['browser_submit_ms'])
            if event == 'drop':
                s['drops'][row['reason']] += row.get('count', 1)
            if event == 'tx':
                s['encode'].append(row['encode_us'] / 1000)
                s['wire_bytes'] += row.get('wire_bytes', 0)
                s['encoded_bytes'] += row.get('frame_bytes', 0)
            if event == 'encoder':
                s['encoders'].add(row['name'])
            if event in ('tx', 'rtx'):
                s['injected_packet_drops'] += row.get('injected_packet_drops', 0)
            if event == 'rtx':
                s['rtx_bytes'] += row.get('wire_bytes', 0)
            if event != 'sample':
                writer.writerow(row)
    duration = (max(times) - min(times)) / 1e9 if len(times) > 1 else 0
    summary['recording_seconds'] = duration
    for stream in range(config.get('streams', 1)):
        s = stats[stream]
        selected_event = 'decode' if config.get('headless') else 'submit'
        ft = s['decode_times'] if config.get('headless') else s['presentation_times']
        # Include startup and trailing outage, not only intervals between successful frames.
        bounds = ([min(times)] + ft + [max(times)]) if times else []
        gaps = [(b - a) / 1e6 for a, b in zip(bounds, bounds[1:])]
        exceeds = sum(v > 100 for v in s[selected_event])
        summary['streams'][str(stream)] = {
            'events': dict(s['counts']), 'drop_reasons': dict(s['drops']),
            'decode_latency_ms': distribution(s['decode']),
            'decode_over_100ms': sum(v > 100 for v in s['decode']),
            'decode_latency_unknown': s['counts']['decode'] - len(s['decode']),
            'render_submit_latency_ms': distribution(s['submit']),
            'browser_submit_latency_ms': distribution(s['browser_submit']),
            'sensor_to_browser_submit_ms': distribution(s['sensor_latencies']['browser_submit']),
            'sensor_to_native_decode_ms': distribution(s['sensor_latencies']['decode']),
            'sensor_to_native_submit_ms': distribution(s['sensor_latencies']['submit']),
            'sensor_timestamp_states': dict(s['sensor_states']),
            'sensor_host_network_clock_uncertainty_ms': distribution(s['sensor_uncertainty']),
            'sensor_sdk_fit_error_ms': None,
            'browser_submit_fps_over_recording': s['counts']['browser_submit'] / duration if duration else 0,
            'browser_inter_submit_ms': distribution([b - a for a, b in zip(s['browser_times'], s['browser_times'][1:]) if b >= a]),
            'selection_latency_ms': distribution(s['selected']),
            'encode_pipeline_ms': distribution(s['encode']),
            'clock_uncertainty_ms': distribution(s['uncertainty']),
            'decoded_fps_over_recording': s['counts']['decode'] / duration if duration else 0,
            'encoded_fps_over_recording': s['counts']['tx'] / duration if duration else 0,
            'unique_render_submit_fps_over_recording': s['counts']['submit'] / duration if duration else 0,
            'deadline_over_100ms_observed': exceeds,
            'over_100ms_denominator': len(s[selected_event]),
            'gap_measurement': 'decode delivery' if config.get('headless') else 'UI submission',
            'gaps_over_200ms': sum(g > 200 for g in gaps),
            'longest_gap_ms': max(gaps, default=0),
            'gap_total_over_200ms': sum(g for g in gaps if g > 200),
            'rtp_bytes_sent': s['wire_bytes'], 'encoded_bytes_sent': s['encoded_bytes'],
            'rtp_retransmit_bytes_sent': s['rtx_bytes'],
            'mean_rtp_send_mbps': (s['wire_bytes'] + s['rtx_bytes']) * 8 / 1e6 / duration if duration else 0,
            'injected_packet_drops': s['injected_packet_drops'],
            'encoders': sorted(s['encoders']), 'decoders': sorted(s['decoders']),
            'camera_input': s['camera'], 'capture_fps_samples': distribution(s['capture_fps']),
            'sender_totals': s['sender_totals'],
            'stages_ms': {name: distribution(s['stages'][name]) for name in stage_fields},
        }
        if config.get('ui_backend') == 'webcodecs':
            # Native decode never ran here. Do not let headless bookkeeping
            # turn the lack of native events into fabricated gaps/deadlines.
            summary['streams'][str(stream)].update(
                gap_measurement='browser intervals only; startup/outage requires event timeline',
                longest_gap_ms=max((b - a for a, b in zip(s['browser_times'], s['browser_times'][1:]) if b >= a), default=None),
                gaps_over_200ms=sum(b - a > 200 for a, b in zip(s['browser_times'], s['browser_times'][1:])),
                gap_total_over_200ms=sum(b - a for a, b in zip(s['browser_times'], s['browser_times'][1:]) if b - a > 200),
                deadline_over_100ms_observed=sum(v > 100 for v in s['browser_submit']),
                over_100ms_denominator=len(s['browser_submit']))
    total = summary['groups']['total']
    summary['groups']['complete_ratio'] = summary['groups']['complete'] / total if total else None
    summary['groups']['capture_skew_ms'] = distribution(skew_values)
    summary['visible_groups'] = {'total': visible_groups, 'with_stale_frames': visible_stale,
                                'capture_skew_ms': distribution(visible_skew)}
    summary['browser_visible_skew_ms'] = distribution(browser_skew)
    summary['glass_to_glass_latency_ms'] = None
    (directory / 'summary.json').write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    return summary
