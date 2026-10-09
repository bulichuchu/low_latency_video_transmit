"""Receiver service side of the WebRTC transport: signaling and records only.

Video goes from the sender (aiortc) straight to the browser that opened the
receiver page; nothing here touches it. The relay binds the receiver's UDP port
so the sender's control path is unchanged: it learns the sender's address from
the sender's RTP (sent until a browser connects), forwards the browser's offer,
resends it until answered and returns the answer. Browser telemetry is
journaled like the RTP receiver's, so summary.json compares across transports.
"""
from __future__ import annotations

from collections import deque
import concurrent.futures
import json
import math
from pathlib import Path
import socket
import threading
import time

from .capture_time import TIMESTAMP_STATUSES
from .metrics import Journal
from .protocol import parse_packet
from .webrtc_signal import MAX_TEXT, Reassembler, datagrams, new_session, parse

RESEND_S = .5
OFFER_TIMEOUT_S = 15
SENDER_STICKY_NS = 2_000_000_000  # one sender process per receiver port, as in RTP mode
PLAYOUT_DELAY_URI = 'http://www.webrtc.org/experiments/rtp-hdrext/playout-delay'
# Per presented frame, all on the browser clock except the sender's own share.
LATENCY_FIELDS = ('latency_ms', 'sensor_latency_ms')
STEP_FIELDS = ('camera_ms', 'sender_ms', 'delivery_ms', 'browser_ms', 'browser_decode_ms', 'browser_wait_ms',
               'browser_buffer_ms', 'browser_expected_display_ms', 'clock_uncertainty_ms',
               'sensor_clock_uncertainty_ms', 'sync_skew_ms')
STATS_FIELDS = ('mbps', 'fps', 'packets_lost', 'nack_count', 'pli_count', 'frames_received', 'frames_decoded',
                'frames_dropped', 'freeze_count', 'freeze_ms', 'jitter_buffer_ms', 'decode_ms', 'rtt_ms',
                'width', 'height', 'superseded', 'unmapped', 'send_kbps', 'send_fps')


def settle(future, result):
    try:
        future.set_result(result)
    except concurrent.futures.InvalidStateError:
        pass  # the page went away and cancelled its wait


def number(value, low, high):
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and low <= value <= high
    return value if ok else None


class WebRTCRelay:
    def __init__(self, args):
        self.args, self.streams = args, args.streams
        self.lock = threading.Lock()
        self.journal = self.sock = None
        self.peer = None
        self.last_rtp = 0
        self.rtp = deque()  # (time_ns, bytes): the sender's RTP while no browser takes the video
        self.signals = Reassembler()
        self.pending = None
        self.session = None
        self.state = 'waiting_sender'
        self.error = None
        self.attached = False
        self.latest = {}
        self.stream_stats = {}

    # -- relay thread -----------------------------------------------------------
    def run(self):
        args = self.args
        directory = Path(args.output)
        journal = Journal(args.output, vars(args))
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 512 * 1024)
        try:
            sock.bind((args.bind, args.port))
        except OSError:
            sock.close()
            journal.close()
            raise
        sock.settimeout(.05)
        with self.lock:
            self.journal, self.sock = journal, sock
        journal.log('start', transport='webrtc')
        port = sock.getsockname()[1]
        (directory / 'ready.json').write_text(json.dumps(dict(port=port, streams=args.streams, transport='webrtc')))
        print(f'[receiver] WebRTC 信令中继 {args.bind}:{port}：视频由发送端直连打开接收页的浏览器', flush=True)
        started = time.perf_counter()
        try:
            while True:
                if (directory / 'STOP').exists():
                    break
                if journal.error:
                    raise RuntimeError(journal.error)
                if args.duration and time.perf_counter() - started >= args.duration:
                    break
                try:
                    data, address = sock.recvfrom(65535)
                except socket.timeout:
                    data = None
                now = time.perf_counter_ns()
                if data:
                    self._datagram(data, address, now)
                self._resend(now)
        finally:
            with self.lock:
                self._cancel_pending('接收端已停止')
                self._close_session()
                self.journal = self.sock = None
            sock.close()
            journal.log('stop')
            summary = journal.close()
            print(f'[receiver] report: {args.output}/summary.json', flush=True)
        return summary

    def _datagram(self, data, address, now):
        msg = parse(data)
        if msg is not None:
            with self.lock:
                if address != self.peer:
                    return
                text = self.signals.add(msg)
                pending = self.pending
                if text is None or pending is None or msg['id'] != pending['session']:
                    return
                if msg['type'] == 'webrtc_answer':
                    self.pending, self.session, self.state, self.error = None, msg['id'], 'answered', None
                    result = ('answer', text, False)
                elif msg['type'] == 'webrtc_error':
                    self.pending, self.state, self.error = None, 'error', text
                    result = ('error', f'发送端无法建立 WebRTC：{text}', False)
                else:
                    return
                self.journal.log('webrtc_' + result[0], session=msg['id'],
                                 wait_ms=(now - pending['created']) / 1e6, **({'error': text} if result[0] == 'error' else {}))
            settle(pending['future'], result)
            return
        try:
            parse_packet(data)  # only the sender's RTP identifies the sender
        except ValueError:
            return
        with self.lock:
            if address != self.peer:
                if self.peer and now - self.last_rtp < SENDER_STICKY_NS:
                    return
                self.peer = address
                if self.state == 'waiting_sender':
                    self.state = 'idle'
                self.journal.log('webrtc_sender', address=f'{address[0]}:{address[1]}')
            self.last_rtp = now
            self.rtp.append((now, len(data)))
            while self.rtp and now - self.rtp[0][0] > 1_000_000_000:
                self.rtp.popleft()

    def _resend(self, now):
        with self.lock:
            pending = self.pending
            if pending is None:
                return
            if now < pending['deadline']:
                if now >= pending['next_send']:
                    pending['next_send'] = now + int(RESEND_S * 1e9)
                    for data in pending['data']:
                        self.sock.sendto(data, self.peer)
                return
            self.pending, self.state = None, 'error'
            self.error = f'发送端 {OFFER_TIMEOUT_S} 秒内没有应答'
            self.journal.log('webrtc_error', session=pending['session'], error='answer timeout')
        settle(pending['future'], ('error', f'发送端 {OFFER_TIMEOUT_S} 秒内没有应答 WebRTC 请求：确认发送端仍在运行，'
                                               '且已更新到支持 WebRTC 的版本。', True))

    # -- web handler (any thread) ----------------------------------------------
    def offer(self, sdp):
        """Forward the browser's offer; the future yields (kind, text, retry)."""
        future = concurrent.futures.Future()
        if not isinstance(sdp, str) or not sdp.startswith('v=0') or len(sdp) > MAX_TEXT:
            settle(future, ('error', '无效的 WebRTC offer', False))
            return future
        with self.lock:
            if self.sock is None:
                settle(future, ('error', '接收端尚未就绪', True))
                return future
            if self.peer is None:
                settle(future, ('error', f'尚未收到发送端数据：请在发送端开始回传，目标为本机 IP 和端口 '
                                            f'{self.sock.getsockname()[1]}。', True))
                return future
            self._cancel_pending('已被新的连接请求替代')
            self._close_session()  # one viewer: a new offer replaces the previous session
            session = new_session()
            now = time.perf_counter_ns()
            self.pending = dict(session=session, data=datagrams('webrtc_offer', session, sdp), future=future,
                                created=now, next_send=0, deadline=now + OFFER_TIMEOUT_S * 1_000_000_000)
            self.state, self.error = 'offering', None
            self.journal.log('webrtc_offer', session=session, video_sections=sdp.count('m=video'),
                             playout_delay_zero=PLAYOUT_DELAY_URI in sdp,
                             mdns_candidates=sum('.local ' in line for line in sdp.split('\n') if line.startswith('a=candidate')))
        return future

    def close_session(self):
        with self.lock:
            self._cancel_pending('浏览器已断开')
            self._close_session()

    def attach(self, connected):
        with self.lock:
            self.attached = connected
            if not connected:
                self._cancel_pending('浏览器已断开')
                self._close_session()

    def telemetry(self, records):
        """Shown frames reported by the page, journaled like RTP mode. Frames
        whose metadata never arrived keep their row with null ids/latencies,
        so frame rate and gaps count every shown frame, as in RTP mode."""
        if not isinstance(records, list) or len(records) > 1024:
            raise ValueError('Invalid browser telemetry batch')
        with self.lock:
            journal = self.journal
            if journal is None:
                return
            for row in records:
                if not isinstance(row, dict):
                    continue
                ids = [row.get(k) for k in ('stream', 'epoch', 'frame_id')]
                if (type(ids[0]) is not int or not 0 <= ids[0] < self.streams
                        or not all(v is None or type(v) is int for v in ids[1:])):
                    continue
                fields = {name: number(row.get(name), 0, 60000) for name in LATENCY_FIELDS}
                fields.update({name: number(row.get(name), -1000, 60000) for name in STEP_FIELDS})
                fields['browser_submit_ms'] = number(row.get('browser_submit_ms'), 0, 1e12)
                status = row.get('sensor_status')
                status = status if status in TIMESTAMP_STATUSES else 'unavailable'
                measured = row.get('sensor_measurement_status')
                if status != 'ready':
                    fields['sensor_latency_ms'] = fields['sensor_clock_uncertainty_ms'] = None
                    measured = status
                elif measured not in (*TIMESTAMP_STATUSES, 'clock_sync'):
                    measured = 'ready' if fields['sensor_latency_ms'] is not None else 'clock_sync'
                journal.log('browser_submit', stream=ids[0], epoch=ids[1], frame_id=ids[2],
                            sensor_status=status, sensor_measurement_status=measured,
                            decoder='webrtc', transport='webrtc', **fields)
                self.latest[str(ids[0])] = fields

    def stats(self, records):
        """Per-stream getStats() deltas from the page, about once a second."""
        if not isinstance(records, list) or len(records) > 64:
            raise ValueError('Invalid WebRTC stats batch')
        with self.lock:
            journal = self.journal
            if journal is None:
                return
            for row in records:
                if not isinstance(row, dict) or type(row.get('stream')) is not int or not 0 <= row['stream'] < self.streams:
                    continue
                fields = {name: number(row.get(name), 0, 1e9) for name in STATS_FIELDS}
                decoder = row.get('decoder')
                fields['decoder'] = decoder[:80] if isinstance(decoder, str) else None
                journal.log('webrtc_stats', stream=row['stream'], **fields)
                journal.log('sample', stream=row['stream'], fps=fields['fps'], rtp_mbps=fields['mbps'],
                            fps_measurement='browser_webrtc_presented')
                self.stream_stats[row['stream']] = fields

    def snapshot(self):
        with self.lock:
            now = time.perf_counter_ns()
            while self.rtp and now - self.rtp[0][0] > 1_000_000_000:
                self.rtp.popleft()
            return dict(transport='webrtc', connected=self.attached, state=self.state, error=self.error,
                        session=self.session,
                        sender=f'{self.peer[0]}:{self.peer[1]}' if self.peer else None,
                        sender_rtp_mbps=sum(size for _, size in self.rtp) * 8 / 1e6,
                        streams=[dict(stream=i, mbps=(self.stream_stats.get(i) or {}).get('mbps') or 0, drops=0,
                                      **{k: v for k, v in (self.stream_stats.get(i) or {}).items() if k != 'mbps'})
                                 for i in range(self.streams)],
                        browser=dict(self.latest))

    # -- with self.lock held ----------------------------------------------------
    def _cancel_pending(self, reason):
        pending, self.pending = self.pending, None
        if pending is not None:
            settle(pending['future'], ('error', reason, False))

    def _close_session(self):
        if self.session and self.peer and self.sock:
            for data in datagrams('webrtc_close', self.session) * 2:
                self.sock.sendto(data, self.peer)
            self.journal.log('webrtc_close', session=self.session)
        self.session = None
        if self.state in ('answered', 'offering'):
            self.state = 'idle'
