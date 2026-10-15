"""Standalone RK3588 V4L2/MPP sender. Runtime: stdlib + system PyGObject/GStreamer.

Build the deployable zipapp with tools/build_rk_sender.py. No PyAV, ROS, web
server, JPEG decode, or raw pixel copies in Python. The wire protocol is the
same module used by the desktop receiver, not a second implementation.
"""
from __future__ import annotations

import argparse
from collections import Counter, OrderedDict, deque
import errno
import json
import math
import os
from pathlib import Path
import re
import secrets
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time

from .protocol import MAX_FRAME_BYTES, Meta, Packetizer, START, nals, parse_nack, parse_pli
from . import webrtc_signal

VERSION = '0.4.4'
# Frames older than this when sent are still sent (and counted as `late`):
# on a LAN a late P frame costs far less than cutting the reference chain.
LATE_MS = 75
# Per-frame stages (ms), each from one timestamp to the next:
#   driver  V4L2 buffer timestamp -> v4l2src hands the frame to the pipeline
#   queue   -> the encoder accepts it (the one-frame leaky queue)
#   encode  -> the encoder outputs the access unit
#   handoff -> the Python callback runs (appsink, GIL)
#   send    socket send of all packets
# queue/encode/handoff need encoder pad probes, installed only with --frame-log.
# The callback (and its send) runs on the encoder's output thread while it holds
# the encoder's stream lock, so the next frame's queue/encode include it.
STAGES = ('driver', 'queue', 'encode', 'handoff', 'send')
# Forced IDR requests (see CameraPipeline.poll):
KEY_REQUEST_INTERVAL_MS = 150  # at most one request per stream this often
KEY_REQUEST_GRACE_MS = 100  # frames captured this soon after a request may still be P frames
MAX_KEY_REQUESTS = 10000  # per stream and process; GstVideoEncoder keeps every timed request queued
# Staggered IDRs (see RtpOutput._stagger_idr):
STAGGER_GAP_FRAMES = 2  # IDRs of two streams closer than this many frame intervals collide
STAGGER_HOLDOFF_MS = 5000  # at most one staggering request per stream this often


class StreamStats:
    def __init__(self):
        self.lock = threading.Lock()
        self.counts = Counter()
        self.processing_ms = deque(maxlen=180)
        self.stages = {name: deque(maxlen=180) for name in STAGES}
        self.last_capture_ns = self.last_encoded_ns = self.last_sent_ns = 0
        self.last_matched_ns = 0
        self.last_send_error = None
        self.last_send_ms = None

    def add(self, **counts):
        with self.lock:
            self.counts.update(counts)

    def health(self):
        """The timestamps health_check needs; cheap enough for every poll."""
        with self.lock:
            return dict(last_capture_ns=self.last_capture_ns, last_encoded_ns=self.last_encoded_ns,
                        last_matched_ns=self.last_matched_ns, last_sent_ns=self.last_sent_ns,
                        last_send_error=self.last_send_error)

    def snapshot(self):
        with self.lock:
            values = sorted(self.processing_ms)
            stages = {}
            for name, samples in self.stages.items():
                ordered = sorted(samples)
                if ordered:  # [p50, p95, max] over the last 180 frames
                    stages[name] = [round(ordered[len(ordered) // 2], 2),
                                    round(ordered[min(len(ordered) - 1, int(len(ordered) * .95))], 2),
                                    round(ordered[-1], 2)]
            return dict(self.counts, sender_ms_p50=values[len(values) // 2] if values else None,
                        sender_ms_p95=values[min(len(values) - 1, int(len(values) * .95))] if values else None,
                        stage_ms=stages,
                        last_capture_ns=self.last_capture_ns, last_encoded_ns=self.last_encoded_ns,
                        last_sent_ns=self.last_sent_ns, last_matched_ns=self.last_matched_ns,
                        last_send_error=self.last_send_error, last_send_ms=self.last_send_ms)


class CaptureTimes:
    """Match encoder PTS to application capture, retaining no raw buffers.

    Entries lost at the leaky RAW queue are bounded. PTS is only an identity;
    it is not confused with an absolute host/sensor exposure timestamp.
    """
    def __init__(self, limit=128, strict=True):
        self.limit = limit
        self.strict = strict  # Timing-only maps never stop the sender.
        self.pending = OrderedDict()
        self.lock = threading.Lock()

    def put(self, pts, now):
        with self.lock:
            if pts in self.pending:
                if self.strict:
                    raise RuntimeError('Camera produced duplicate pending PTS')
                del self.pending[pts]
            self.pending[pts] = now
            while len(self.pending) > self.limit:
                self.pending.popitem(last=False)

    def take(self, pts):
        with self.lock:
            return self.pending.pop(pts, None)


class RtpOutput:
    """One feedback socket for all cameras, and no Python encoded-frame queue.

    Each streaming callback sends one AU synchronously within a deadline.
    If sending fails or a frame is stale (older than max_age_ms), dependent P
    frames are suppressed until an IDR with SPS/PPS arrives. Dropping
    compressed P frames and carrying on with their dependents would corrupt the
    receiver's reference chain, so merely late frames are still sent: each
    drop costs a freeze until the next IDR plus that IDR's burst.
    """
    def __init__(self, peer, names, *, mtu=1200, max_age_ms=1000, send_budget_ms=15,
                 idr_send_budget_ms=45, gop=None, fps=None, stagger=True, frame_log=False,
                 sock=None, stop=None, clock=time.perf_counter_ns):
        self.peer, self.names = peer, names
        self.clock = clock
        self.stop = stop or threading.Event()
        self.epoch = secrets.randbits(32)
        self.packetizers = [Packetizer(mtu=mtu) for _ in names]
        self.stats = [StreamStats() for _ in names]
        self.key_requests = [threading.Event() for _ in names]
        # When each stream's request was last set, and when the encoder was last
        # asked for an IDR (None once an IDR answered it). Guarded by request_lock.
        self.request_lock = threading.Lock()
        self.request_set_ns = [0 for _ in names]
        self.requested_ns = [None for _ in names]
        self.request_grace_ns = int(KEY_REQUEST_GRACE_MS * 1e6)
        self.waiting_idr = [True for _ in names]
        self.headers = [{} for _ in names]
        self.frame_ids = [0 for _ in names]
        self.gop = gop
        self.since_key = [None for _ in names]  # encoded pictures since the last IDR
        # Keeping the streams' periodic IDRs apart (_stagger_idr); guarded by request_lock.
        self.frame_ns = 1e9 / fps if fps else None
        self.stagger = stagger
        self.last_idr_ns = [None for _ in names]  # capture time of each stream's latest IDR
        self.stagger_at_ns = [None for _ in names]  # CameraPipeline.poll forces an IDR from then on
        self.stagger_set_ns = [None for _ in names]  # when that was last decided
        # Per-frame timing records for --frame-log, drained by the main loop.
        self.frame_records = deque(maxlen=20000) if frame_log else None
        self.max_age_ns = int(max_age_ms * 1e6)
        self.late_ns = int(LATE_MS * 1e6)
        self.send_budget_ns = int(send_budget_ms * 1e6)
        self.idr_send_budget_ns = int(idr_send_budget_ms * 1e6)
        self.cache = OrderedDict()
        self.cache_lock = threading.Lock()
        self.feedback_thread = None
        self.feedback_lock = threading.Lock()
        self.last_feedback_ns = 0
        self.feedback_count = 0
        self.last_control_error = None
        self.sock = sock or socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if sock is None:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 64 * 1024)
            self.sock.bind(('0.0.0.0', 0))
            self.sock.setblocking(False)

    def start(self):
        self.feedback_thread = threading.Thread(target=self._controls, name='rk-feedback', daemon=True)
        self.feedback_thread.start()

    def close(self):
        self.stop.set()
        if self.feedback_thread:
            self.feedback_thread.join(timeout=1)
        self.sock.close()

    def _stream_for(self, ssrc):
        return next((i for i, p in enumerate(self.packetizers) if p.ssrc == ssrc), None)

    def _drop(self, stream, reason, note=None, capture_ns=None):
        self.stats[stream].add(**{reason: 1})
        self.waiting_idr[stream] = True
        self._want_key(stream, capture_ns)
        if note is not None:
            note['r'] = reason
        return False

    def request_key(self, stream):
        """Ask CameraPipeline.poll to request an IDR (PLI, reconnect, broken chain)."""
        with self.request_lock:
            self.request_set_ns[stream] = self.clock()
            self.key_requests[stream].set()

    def begin_key_request(self, stream):
        """CameraPipeline.poll is about to ask the encoder for an IDR. Clear the flag
        and note the time in one step, so a P frame submitted while the request is
        being sent cannot re-arm it. Returns the previous time for a rejection.

        The new IDR restarts this stream's GOP, so a pending staggering IDR is
        dropped; _stagger_idr looks again when the new one comes out."""
        with self.request_lock:
            previous = self.requested_ns[stream]
            self.key_requests[stream].clear()
            self.requested_ns[stream] = self.clock()
            self.stagger_at_ns[stream] = self.stagger_set_ns[stream] = None
            return previous

    def key_request_rejected(self, stream, previous):
        with self.request_lock:
            self.requested_ns[stream] = previous

    def _want_key(self, stream, capture_ns):
        """Frames that were already captured when the encoder was asked for an IDR
        still come out as P frames; they must not ask again (each extra request
        costs another ~300 KB IDR). Ask again only if a frame captured well after
        the request is still a P frame, i.e. the encoder ignored it."""
        with self.request_lock:
            requested = self.requested_ns[stream]
            if requested is None or capture_ns is None or capture_ns - requested > self.request_grace_ns:
                self.request_set_ns[stream] = self.clock()
                self.key_requests[stream].set()

    def _stagger_idr(self, stream, capture_ns):
        """Keep the streams' periodic IDRs apart. All streams start with an IDR at
        the same moment, and a forced IDR (PLI, receiver reconnect) restarts a
        stream's GOP, so their IDRs (~180 KB each at 720p, 3 Mbps) tend to go out
        together every GOP. On 10-15, once no frame was dropped any more, both
        streams' IDRs were within 3 ms of each other and 15 of 16 NACKs were on IDRs.

        When this IDR comes within STAGGER_GAP_FRAMES of another stream's, ask for
        one IDR on this stream in the middle of the largest gap between the other
        streams' IDRs (half a GOP away with two streams); the encoder restarts this
        stream's GOP there. That costs one forced IDR, which MPP answers with two."""
        n = len(self.names)
        if not self.stagger or n < 2 or not self.frame_ns or not self.gop or self.gop < 4 * n:
            return
        period = self.gop * self.frame_ns
        with self.request_lock:
            self.last_idr_ns[stream] = capture_ns
            decided = self.stagger_set_ns[stream]
            if (self.stagger_at_ns[stream] is not None
                    or decided is not None and capture_ns - decided < STAGGER_HOLDOFF_MS * 1_000_000):
                return
            # Where the other streams' IDRs fall, as offsets from this one within a
            # GOP. A stream that is about to move counts where it is going: else
            # MPP's second IDR on another stream sees it here and moves too, and
            # both end up together again (every hold-off, for the whole run).
            phases = sorted(((t if at is None else at) - capture_ns) % period
                            for i, (t, at) in enumerate(zip(self.last_idr_ns, self.stagger_at_ns))
                            if i != stream and t is not None)
            if not phases:
                return
            nearest = min(min(p, period - p) for p in phases)
            if nearest >= min(STAGGER_GAP_FRAMES * self.frame_ns, period / (2 * n)):
                return
            ends = phases + [phases[0] + period]
            gap, start = max((b - a, a) for a, b in zip(ends, ends[1:]))
            self.stagger_at_ns[stream] = capture_ns + int((start + gap / 2) % period)
            self.stagger_set_ns[stream] = capture_ns

    def take_stagger(self, stream, now):
        """True once when it is time to force this stream's staggering IDR."""
        with self.request_lock:
            due = self.stagger_at_ns[stream]
            if due is None or now < due:
                return False
            self.stagger_at_ns[stream] = None
            return True

    def receiver_active(self, now=None):
        now = self.clock() if now is None else now
        with self.feedback_lock:
            return bool(self.last_feedback_ns) and now - self.last_feedback_ns < 2_000_000_000

    def _send(self, data, deadline):
        last_errno = None
        while not self.stop.is_set() and self.clock() < deadline:
            try:
                if self.sock.sendto(data, self.peer) == len(data):
                    return None
                return ('send_short_write', None)
            except OSError as exc:
                last_errno = errno.errorcode.get(exc.errno, str(exc.errno))
                if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK, errno.ENOBUFS, errno.EINTR):
                    return ('send_socket_error', last_errno)
                # Do not spin on ENOBUFS (the socket can still report writable).
                self.stop.wait(min(.001, max(0, (deadline - self.clock()) / 1e9)))
        return ('send_stopped' if self.stop.is_set() else 'send_timeout', last_errno)

    def _remember(self, ssrc, packet, capture_ns):
        now = self.clock()
        with self.cache_lock:
            self.cache[(ssrc, struct.unpack_from('!H', packet, 2)[0])] = [now, packet, capture_ns, 0]
            while self.cache and (len(self.cache) > 2048 or now - next(iter(self.cache.values()))[0] > 30_000_000):
                self.cache.popitem(last=False)

    def submit(self, stream, data, capture_ns, encoded_ns=None, timing=None):
        """Called serially for each stream; different streams may run in parallel.

        timing: optional {'driver_ms', 'enc_in_ns', 'enc_out_ns'} from the
        pipeline probes. It only feeds stage statistics and --frame-log.
        """
        encoded_ns = self.clock() if encoded_ns is None else encoded_ns
        note = {}
        try:
            return self._submit(stream, data, capture_ns, encoded_ns, timing or {}, note)
        finally:
            if self.frame_records is not None and 'f' in note:
                note['w'] = self.clock()  # callback done: the encoder's output thread is free again
                self.frame_records.append(note)

    def _stages(self, stats, note, capture_ns, encoded_ns, timing):
        enc_in, enc_out = timing.get('enc_in_ns'), timing.get('enc_out_ns')
        values = {'driver': timing.get('driver_ms')}
        if enc_in is not None and capture_ns <= enc_in <= encoded_ns:
            values['queue'] = (enc_in - capture_ns) / 1e6
            if enc_out is not None and enc_in <= enc_out <= encoded_ns:
                values['encode'] = (enc_out - enc_in) / 1e6
                values['handoff'] = (encoded_ns - enc_out) / 1e6
        values = {name: value for name, value in values.items() if value is not None}
        with stats.lock:
            for name, value in values.items():
                stats.stages[name].append(value)
        for name, value in values.items():
            note[name[0]] = round(value, 2)  # d, q, e, h

    def _submit(self, stream, data, capture_ns, encoded_ns, timing, note):
        if not data or len(data) > MAX_FRAME_BYTES:
            return self._drop(stream, 'invalid_au')
        try:
            units = nals(data)
        except ValueError:
            return self._drop(stream, 'invalid_au')
        for nal in units:
            kind = nal[0] & 31
            if kind in (7, 8):
                self.headers[stream][kind] = nal
        if not any(1 <= (nal[0] & 31) <= 5 for nal in units):
            return False  # Parameter-set-only output is not a video frame.
        fid = self.frame_ids[stream]
        self.frame_ids[stream] += 1
        if fid >= 2**32:
            raise RuntimeError('Frame counter exhausted; restart the sender')
        # The tested MPP plugin flags even IDRs as DELTA_UNIT: read H.264 NALs.
        key = any(nal[0] & 31 == 5 for nal in units)
        note.update(s=stream, f=fid, k=int(key), b=len(data))
        if timing.get('pts') is not None:
            note['p'] = timing['pts']
        if key:
            with self.request_lock:
                self.requested_ns[stream] = None  # answered, or re-requested below if this IDR fails
        stats = self.stats[stream]
        with stats.lock:
            stats.counts['encoded'] += 1
            stats.counts['encoded_bytes'] += len(data)
            stats.counts['encoded_frame_bytes_max'] = max(stats.counts['encoded_frame_bytes_max'], len(data))
            stats.counts['idr_encoded'] += int(key)
            stats.last_encoded_ns = encoded_ns
        # An IDR earlier than the GOP would produce one answers a key request.
        since = self.since_key[stream]
        if key:
            if since is not None and self.gop and since + 1 < self.gop:
                stats.add(idr_forced=1)
            self.since_key[stream] = 0
        elif since is not None:
            self.since_key[stream] = since + 1
        if capture_ns is None or not 0 < capture_ns <= encoded_ns:
            return self._drop(stream, 'missing_capture_time', note)
        note['c'] = capture_ns
        if key:  # the encoder's GOP restarts here whether or not this frame gets sent
            self._stagger_idr(stream, capture_ns)
        with stats.lock:
            stats.last_matched_ns = encoded_ns
            stats.processing_ms.append((encoded_ns - capture_ns) / 1e6)
        self._stages(stats, note, capture_ns, encoded_ns, timing)
        age = self.clock() - capture_ns
        note['a'] = round(age / 1e6, 2)
        if age > self.max_age_ns:
            return self._drop(stream, 'expired', note, capture_ns)
        if self.waiting_idr[stream] and not key:
            self._want_key(stream, capture_ns)
            stats.add(awaiting_idr=1)
            note['r'] = 'awaiting_idr'
            return False
        if key:
            if not all(k in self.headers[stream] for k in (7, 8)):
                return self._drop(stream, 'missing_headers', note, capture_ns)
            present = {nal[0] & 31 for nal in units}
            prefix = [self.headers[stream][k] for k in (7, 8) if k not in present]
            if prefix:
                data = b''.join(START + nal for nal in prefix) + data
        meta = Meta(stream, self.epoch, fid, capture_ns, (encoded_ns - capture_ns) // 1000)
        try:
            packets = self.packetizers[stream].packetize(data, meta)
        except ValueError:
            return self._drop(stream, 'invalid_au', note, capture_ns)
        send_started = self.clock()
        # MPP IDRs can exceed 300 KB even at a 4 Mbps average bitrate. The send
        # budgets match the receiver's reassembly windows (P 20 ms, IDR 50 ms):
        # a frame that cannot arrive within them is useless to the receiver.
        budget = self.idr_send_budget_ns if key else self.send_budget_ns
        deadline = min(capture_ns + self.max_age_ns, send_started + budget)
        sent_packets = sent_bytes = 0
        error = None
        for packet in packets:
            error = self._send(packet, deadline)
            if error is not None:
                break
            self._remember(self.packetizers[stream].ssrc, packet, capture_ns)
            sent_packets += 1
            sent_bytes += len(packet)
        send_ms = (self.clock() - send_started) / 1e6
        note['t'] = round(send_ms, 2)
        with stats.lock:
            stats.counts.update(wire_bytes=sent_bytes, packets=sent_packets)
            stats.last_send_ms = send_ms
            stats.stages['send'].append(send_ms)
            if key:
                stats.counts['idr_send_ms_max'] = max(stats.counts['idr_send_ms_max'], round(send_ms, 3))
            if error is not None:
                reason, errno_name = error
                stats.counts[reason] += 1
                if errno_name:
                    stats.counts['socket_' + errno_name] += 1
                stats.last_send_error = dict(reason=reason, errno=errno_name,
                    frame_id=fid, idr=key, frame_bytes=len(data),
                    sent_packets=sent_packets, total_packets=len(packets),
                    budget_ms=budget / 1e6,
                    elapsed_ms=send_ms, frame_age_ms=(self.clock() - capture_ns) / 1e6)
        if error is not None:
            return self._drop(stream, 'send_failed', note, capture_ns)
        self.waiting_idr[stream] = False
        if key:
            # This IDR answers every request set before it started going out
            # (the receiver asked before it could have received it). Requests
            # set since then are kept: they may report a newer loss.
            with self.request_lock:
                if self.request_set_ns[stream] <= send_started:
                    self.key_requests[stream].clear()
        with stats.lock:
            stats.counts['sent'] += 1
            stats.counts['idr_sent'] += int(key)
            stats.counts['late'] += int(age > self.late_ns)
            stats.last_sent_ns = self.clock()
        note['r'] = 'sent'
        return True

    def _feedback(self, now):
        with self.feedback_lock:
            reconnect = not self.last_feedback_ns or now - self.last_feedback_ns > 2_000_000_000
            self.last_feedback_ns = now
            self.feedback_count += 1
        if reconnect:
            # Also recover when the receiver was started/restarted after us.
            for stream in range(len(self.key_requests)):
                self.request_key(stream)

    def receiver_status(self):
        with self.feedback_lock:
            age = ((self.clock() - self.last_feedback_ns) / 1e6) if self.last_feedback_ns else None
            return dict(state='waiting' if age is None else 'active' if age < 2000 else 'stale',
                        feedback_packets=self.feedback_count, feedback_age_ms=age,
                        last_control_error=self.last_control_error)

    def _control_error(self, exc):
        # Linux can deliver asynchronous UDP errors on recvfrom as well as sendto.
        with self.feedback_lock:
            self.last_control_error = errno.errorcode.get(exc.errno, str(exc.errno))

    def handle_control(self, data, address):
        if address != self.peer:
            return
        now = self.clock()
        missing = parse_nack(data)
        if missing is not None:
            ssrc, sequences = missing
            stream = self._stream_for(ssrc)
            if stream is None:
                return
            self._feedback(now)
            self.stats[stream].add(nack=1)
            for sequence in sequences:
                with self.cache_lock:
                    cached = self.cache.get((ssrc, sequence))
                    if (cached is None or now - cached[0] > 30_000_000
                            or now - cached[2] > self.max_age_ns or cached[3] >= 2):
                        continue
                    cached[3] += 1
                    packet = cached[1]
                try:
                    self.sock.sendto(packet, self.peer)
                    self.stats[stream].add(retransmitted=1)
                except OSError as exc:
                    self._control_error(exc)  # Never build a retransmit queue.
            return
        target = parse_pli(data)
        if target is not None:
            stream = self._stream_for(target)
            if stream is not None:
                self._feedback(now)
                self.request_key(stream)
                self.stats[stream].add(pli=1)
            return
        if len(data) == 32 and data[:4] == b'\x81\xc9\x00\x07':
            stream = self._stream_for(struct.unpack_from('!I', data, 8)[0])
            if stream is not None:
                self._feedback(now)
                with self.stats[stream].lock:
                    self.stats[stream].counts['receiver_loss_fraction'] = data[12] / 256
            return
        msg = webrtc_signal.parse(data)
        if msg is not None and msg['type'] == 'webrtc_offer' and msg['i'] == 0:
            self._feedback(now)
            for reply in webrtc_signal.datagrams('webrtc_error', msg['id'],
                    'RK 轻量发送端使用 RTP/UDP；请在接收页选择 RTP，再重新开始接收。'):
                self._reply(reply)
            return
        try:
            msg = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            return
        if (isinstance(msg, dict) and msg.get('v') == 1 and msg.get('type') == 'clock_ping'
                and type(msg.get('t1')) is int):
            self._feedback(now)
            reply = dict(v=1, type='clock_pong', t1=msg['t1'], t2=now, t3=self.clock(),
                         epoch=self.epoch, stream_names=self.names)
            self._reply(json.dumps(reply, ensure_ascii=False, separators=(',', ':')).encode())

    def _reply(self, data):
        try:
            self.sock.sendto(data, self.peer)
        except OSError as exc:
            self._control_error(exc)

    def _controls(self):
        while not self.stop.is_set():
            try:
                if not select.select([self.sock], [], [], .05)[0]:
                    continue
                data, address = self.sock.recvfrom(8192)
                self.handle_control(data, address)
            except OSError as exc:
                if not self.stop.is_set():
                    if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR):
                        self._control_error(exc)
                    self.stop.wait(.01)


def pipeline_description(args, device, stream=0):
    """Device paths are validated before interpolation; labels never enter this string.

    Element names carry the stream index, so streaming threads are named per
    camera (camera0:src, raw_latest0:src, encoder0:src) in ps, top and --trace.
    """
    bps = args.bitrate_kbps * 1000
    i = int(stream)
    return (
        f'v4l2src name=camera{i} device={device} io-mode={args.io_mode} ! '
        f'video/x-raw,format={args.pixel_format},width={args.width},height={args.height},framerate={args.fps}/1 ! '
        f'queue name=raw_latest{i} leaky=downstream max-size-buffers=1 max-size-bytes=0 max-size-time=0 ! '
        f'mpph264enc name=encoder{i} rc-mode=cbr bps={bps} bps-min={bps * 3 // 4} '
        f'bps-max={bps * 9 // 8} gop={args.gop} profile={args.profile} '
        'header-mode=each-idr max-reenc=0 ! '
        'video/x-h264,stream-format=byte-stream,alignment=au ! '
        f'appsink name=encoded{i} emit-signals=true sync=false max-buffers=1 drop=false '
        'enable-last-sample=false wait-on-eos=false')


# ------------------------------------------------------------- camera crop
# When the encoder pads frames (MPP aligns width and height to 16), v4l2src asks
# the capture node to crop away the padding (VIDIOC_S_SELECTION, CROP). The
# Rockchip ISP applies a selfpath crop to the camera image instead: 1920x1080
# (padded to 1088) showed only the top-left 1920x1080 of the 3840x2160 sensor on
# 10-15. Sizes must therefore be multiples of 16 (parse_args). The driver also
# keeps a crop until the next reboot, so every run first resets it to the full
# image of its node; the robot's own services use other nodes.
V4L2_BUF_TYPE_VIDEO_CAPTURE, V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE = 1, 9
V4L2_SEL_TGT_CROP, V4L2_SEL_TGT_CROP_BOUNDS = 0, 2
_SELECTION = struct.Struct('=IIIiiII36x')  # struct v4l2_selection
VIDIOC_G_SELECTION = (3 << 30) | (_SELECTION.size << 16) | (ord('V') << 8) | 94
VIDIOC_S_SELECTION = (3 << 30) | (_SELECTION.size << 16) | (ord('V') << 8) | 95


def _selection(fd, request, buf_type, target, rect, ioctl):
    data = bytearray(_SELECTION.pack(buf_type, target, 0, *rect))
    ioctl(fd, request, data)
    return tuple(_SELECTION.unpack(data)[3:7])


def camera_crop(device, reset=False, ioctl=None):
    """(crop, bounds, crop afterwards) of a capture node as (left, top, width, height).
    With reset, a crop smaller than the bounds is set back to the full image."""
    if ioctl is None:
        import fcntl
        ioctl = fcntl.ioctl
    fd = os.open(device, os.O_RDWR | os.O_NONBLOCK)
    try:
        for buf_type in (V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE, V4L2_BUF_TYPE_VIDEO_CAPTURE):
            try:
                bounds = _selection(fd, VIDIOC_G_SELECTION, buf_type, V4L2_SEL_TGT_CROP_BOUNDS, (0, 0, 0, 0), ioctl)
                break
            except OSError as exc:  # a single-planar node rejects the multi-planar type
                if exc.errno != errno.EINVAL or buf_type == V4L2_BUF_TYPE_VIDEO_CAPTURE:
                    raise
        crop = after = _selection(fd, VIDIOC_G_SELECTION, buf_type, V4L2_SEL_TGT_CROP, (0, 0, 0, 0), ioctl)
        if reset and crop != bounds:
            after = _selection(fd, VIDIOC_S_SELECTION, buf_type, V4L2_SEL_TGT_CROP, bounds, ioctl)
        return crop, bounds, after
    finally:
        os.close(fd)


def _rect(r):
    return f'({r[0]},{r[1]}) {r[2]}x{r[3]}'


def check_camera_crops(devices, reset, crop=camera_crop):
    """Report (and with reset, undo) a crop of the camera image on each node."""
    for device in devices:
        try:
            before, bounds, after = crop(device, reset=reset)
        except OSError as exc:
            print(f'[rk] {device}: cannot read the camera crop ({exc}); the picture may show only part of '
                  'the camera image', file=sys.stderr, flush=True)
            continue
        if before != after:
            print(f'[rk] {device}: camera crop {_rect(before)} reset to the full {_rect(after)}', flush=True)
        elif after != bounds:
            what = 'could not be reset' if reset else 'reset when sending'
            print(f'[rk] {device}: camera crop {_rect(after)} of {_rect(bounds)} ({what})',
                  file=sys.stderr if reset else sys.stdout, flush=True)
        else:
            print(f'[rk] {device}: camera crop full {_rect(after)}', flush=True)


def configure_rga(use_rga, environ=os.environ):
    """mpph264enc copies frames it cannot import (the ISP's NV12 comes as two
    memory planes) with RGA unless GST_MPP_NO_RGA is set. RGA is shared with the
    robot's camera service, and the CPU copy cost less CPU than the RGA path; on
    10-14 neither made a clear difference to the encoder stalls.
    Returns whether RGA may be used."""
    if use_rga:
        environ.pop('GST_MPP_NO_RGA', None)
    else:
        environ.setdefault('GST_MPP_NO_RGA', '1')
    return 'GST_MPP_NO_RGA' not in environ


# ------------------------------------------------------------- MPP library
# The board's mpph264enc (gst-rockchip of early 2024) polls MPP for each packet
# in non-blocking mode, and MPP's non-blocking get_packet sleeps 1 ms while
# holding the packet list lock. The encoder thread needs that lock to queue the
# packet it has just encoded, so a stream stalled for 150-600 ms every few
# seconds with two cameras. MPP built with deploy/rk_sender/mpp-pkt-poll-fix.patch
# (mpp-pkt/ next to the .pyz, used automatically) removed the stalls on 10-15.
# It is loaded into this process only; the system library and the robot's own
# services are not changed. --mpp-lib picks another build or the system one.
MPP_SONAME = 'librockchip_mpp.so.1'
MPP_BUNDLED = 'mpp-pkt'  # directory next to rk_sender.pyz


def bundle_dir(module_file=__file__):
    """The directory of the .pyz this code runs from (through any symlink to it),
    or None when it runs from the source tree."""
    archive = os.path.dirname(os.path.dirname(os.path.realpath(module_file)))
    return Path(archive).parent if os.path.isfile(archive) else None


def bundled_mpp_lib(module_file=__file__):
    """mpp-pkt/librockchip_mpp.so.1 next to the .pyz this code runs from, if any."""
    here = bundle_dir(module_file)
    candidate = None if here is None else here / MPP_BUNDLED / MPP_SONAME
    return candidate if candidate is not None and candidate.is_file() else None


def resolve_mpp_lib(path):
    """--mpp-lib: a librockchip_mpp file, or a directory holding librockchip_mpp.so.1."""
    candidate = Path(path).expanduser()
    if candidate.is_dir():
        candidate = candidate / MPP_SONAME
    if not candidate.is_file():
        raise RuntimeError(f'MPP library not found: {candidate}')
    return Path(os.path.realpath(candidate))


def preload_mpp(path):
    """Load this MPP build before GStreamer loads mpph264enc. The plugin asks for
    librockchip_mpp.so.1, and the dynamic linker reuses an already loaded object
    with that soname instead of opening the system file."""
    import ctypes
    try:
        ctypes.CDLL(str(path), mode=os.RTLD_NOW | os.RTLD_GLOBAL)
    except OSError as exc:
        raise RuntimeError(f'Cannot load MPP library {path}: {exc}') from exc


def mapped_files(name, maps='/proc/self/maps'):
    """Real paths of the files mapped into this process whose name contains name."""
    found = []
    try:
        with open(maps) as f:
            for line in f:
                parts = line.split(None, 5)
                if len(parts) < 6 or name not in os.path.basename(parts[5]):
                    continue
                path = os.path.realpath(parts[5].rstrip('\n').replace(' (deleted)', ''))
                if path not in found:
                    found.append(path)
    except OSError:
        pass
    return found


def mpp_build_info(path):
    """(sha256 prefix, MPP's own version line such as 'c3b2ece author: ... 2024-11-15 ...')."""
    import hashlib
    digest = hashlib.sha256()
    version = None
    with open(path, 'rb') as f:
        data = f.read()
    digest.update(data)
    at = data.find(b' author: ')
    if at >= 0:
        start = data.rfind(b'\0', 0, at) + 1
        end = data.find(b'\0', at)
        version = ' '.join(data[start:end if end >= 0 else len(data)].decode('utf-8', 'replace').split())[:200]
    return digest.hexdigest()[:16], version


def check_mpp_library(expected=None, maps='/proc/self/maps'):
    """Which librockchip_mpp this process runs, once mpph264enc has been loaded.
    When one was preloaded (mpp-pkt/ or --mpp-lib) it must be exactly that file."""
    libs = mapped_files('librockchip_mpp', maps)
    if expected is not None and libs != [str(expected)]:
        raise RuntimeError(f'MPP library: expected only {expected} in this process, found {libs or "none"}')
    if len(libs) > 1:
        raise RuntimeError(f'More than one MPP library in this process: {libs}')
    if not libs:
        return dict(mpp_lib=None, mpp_sha256=None, mpp_version=None)
    try:
        digest, version = mpp_build_info(libs[0])
    except OSError:
        digest = version = None
    print(f'[rk] MPP {libs[0]} sha256 {digest}: {version or "no version string"}', flush=True)
    return dict(mpp_lib=libs[0], mpp_sha256=digest, mpp_version=version)


def load_gst():
    try:
        import gi
        gi.require_version('Gst', '1.0')
        gi.require_version('GstVideo', '1.0')
        from gi.repository import Gst, GstVideo
    except (ImportError, ValueError) as exc:
        raise RuntimeError('缺少系统 GStreamer Python 绑定。请用 /usr/bin/python3；'
                           '依赖见 deploy/rk_sender/README.md（python3-gi、'
                           'gir1.2-gstreamer-1.0、gir1.2-gst-plugins-base-1.0）。') from exc
    Gst.init(None)
    return Gst, GstVideo


def check_runtime(Gst, args):
    if sys.platform != 'linux':
        raise RuntimeError('RK sender requires Linux on the camera/MPP board')
    for name in ('v4l2src', 'queue', 'mpph264enc', 'appsink'):
        if Gst.ElementFactory.find(name) is None:
            raise RuntimeError(f'Missing GStreamer element: {name}')
    encoder = Gst.ElementFactory.make('mpph264enc', None)
    if encoder is None:
        raise RuntimeError('Cannot instantiate mpph264enc; check the board MPP installation')
    for prop in ('rc-mode', 'bps', 'bps-min', 'bps-max', 'gop', 'profile', 'header-mode', 'max-reenc'):
        if encoder.find_property(prop) is None:
            raise RuntimeError(f'This MPP plugin lacks required property: {prop}')
    for device in args.device:
        try:
            mode = os.stat(device).st_mode
        except FileNotFoundError as exc:
            raise RuntimeError(f'Camera node does not exist: {device}') from exc
        if not stat.S_ISCHR(mode) or not os.access(device, os.R_OK | os.W_OK):
            raise RuntimeError(f'Need read/write permission to camera character device: {device}')
    print(f'[rk] GStreamer {Gst.version_string()}; MPP and camera permissions OK', flush=True)


class CameraPipeline:
    def __init__(self, Gst, GstVideo, args, device, stream, output):
        self.Gst, self.GstVideo = Gst, GstVideo
        self.stream, self.output = stream, output
        self.times = CaptureTimes()
        # Timing only (best effort): these maps never stop the pipeline.
        self.driver_ms = CaptureTimes(strict=False)
        self.encoder_in = CaptureTimes(strict=False)
        self.encoder_out = CaptureTimes(strict=False)
        self.error = None
        self.last_key_ns = self.key_count = 0
        self.last_send_warning_ns = 0
        self.started_ns = time.perf_counter_ns()
        try:
            self.pipeline = Gst.parse_launch(pipeline_description(args, device, stream))
        except Exception as exc:
            raise RuntimeError(f'Cannot create camera {stream} pipeline: {exc}') from exc
        self.encoder = self.pipeline.get_by_name(f'encoder{stream}')
        # Output packets in buffers the plugin allocates (on) or copied out of
        # MPP's own buffer (off); 'auto' keeps the plugin's default.
        self.zero_copy_pkt = None
        if self.encoder.find_property('zero-copy-pkt') is not None:
            if args.zero_copy_pkt != 'auto':
                self.encoder.set_property('zero-copy-pkt', args.zero_copy_pkt == 'on')
            self.zero_copy_pkt = bool(self.encoder.get_property('zero-copy-pkt'))
        elif args.zero_copy_pkt != 'auto':
            raise RuntimeError('This mpph264enc has no zero-copy-pkt property; run without --zero-copy-pkt')
        self.bus = self.pipeline.get_bus()
        self.pipeline.get_by_name(f'camera{stream}').get_static_pad('src').add_probe(
            Gst.PadProbeType.BUFFER, self._capture)
        if args.frame_log:
            # Encoder input/output times (queue, encode, handoff stages). Only for
            # diagnosis runs: the input probe makes the queue->encoder thread take
            # the Python GIL for every frame, which it otherwise never needs.
            self.encoder.get_static_pad('sink').add_probe(Gst.PadProbeType.BUFFER, self._mark, self.encoder_in)
            self.encoder.get_static_pad('src').add_probe(Gst.PadProbeType.BUFFER, self._mark, self.encoder_out)
        self.pipeline.get_by_name(f'raw_latest{stream}').connect('overrun', self._overrun)
        self.pipeline.get_by_name(f'encoded{stream}').connect('new-sample', self._encoded)

    def _overrun(self, _queue):
        self.output.stats[self.stream].add(raw_queue_overruns=1)

    def _running_time(self):
        clock = self.pipeline.get_clock()
        if clock is None:
            return None
        return clock.get_time() - self.pipeline.get_base_time()

    def _mark(self, _pad, info, times):
        try:
            buffer = info.get_buffer()
            if buffer is not None and buffer.pts != self.Gst.CLOCK_TIME_NONE:
                times.put(buffer.pts, time.perf_counter_ns())
        except Exception:
            pass  # Timing is diagnostic only.
        return self.Gst.PadProbeReturn.OK

    def _capture(self, _pad, info):
        try:
            if self.output.stop.is_set():
                return self.Gst.PadProbeReturn.DROP
            buffer = info.get_buffer()
            if buffer.pts == self.Gst.CLOCK_TIME_NONE:
                raise RuntimeError('V4L2 buffer has no PTS; cannot match capture to encoding')
            now = time.perf_counter_ns()
            self.times.put(buffer.pts, now)
            try:  # V4L2 buffer timestamp -> now, both as pipeline running time.
                running = self._running_time()
                if running is not None:
                    self.driver_ms.put(buffer.pts, (running - buffer.pts) / 1e6)
            except Exception:
                pass
            stats = self.output.stats[self.stream]
            with stats.lock:
                stats.counts['captured'] += 1
                stats.last_capture_ns = now
            return self.Gst.PadProbeReturn.OK
        except Exception as exc:
            self.error = f'capture: {exc}'
            self.output.stop.set()
            return self.Gst.PadProbeReturn.DROP

    def _encoded(self, sink):
        try:
            if self.output.stop.is_set():
                return self.Gst.FlowReturn.FLUSHING
            sample = sink.emit('pull-sample')
            if sample is None:
                return self.Gst.FlowReturn.EOS
            buffer = sample.get_buffer()
            now = time.perf_counter_ns()
            if buffer.get_size() > MAX_FRAME_BYTES:
                raise RuntimeError('MPP access unit exceeds protocol frame size limit')
            # Only compressed bytes cross into Python; NV12 stays in GStreamer.
            data = buffer.extract_dup(0, buffer.get_size())
            has_picture = any(1 <= (n[0] & 31) <= 5 for n in nals(data))
            captured = timing = None
            if has_picture:
                captured = self.times.take(buffer.pts)
                timing = dict(driver_ms=self.driver_ms.take(buffer.pts),
                              enc_in_ns=self.encoder_in.take(buffer.pts),
                              enc_out_ns=self.encoder_out.take(buffer.pts),
                              pts=None if buffer.pts == self.Gst.CLOCK_TIME_NONE else buffer.pts)
            self.output.submit(self.stream, data, captured, now, timing)
            return self.Gst.FlowReturn.OK
        except Exception as exc:
            self.error = f'encode/send: {exc}'
            self.output.stop.set()
            return self.Gst.FlowReturn.ERROR

    def start(self):
        if self.pipeline.set_state(self.Gst.State.PLAYING) == self.Gst.StateChangeReturn.FAILURE:
            self.poll()
            raise RuntimeError('GStreamer could not enter PLAYING')

    def poll(self):
        Gst = self.Gst
        while True:
            message = self.bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.WARNING | Gst.MessageType.EOS)
            if message is None:
                break
            if message.type == Gst.MessageType.ERROR:
                error, debug = message.parse_error()
                raise RuntimeError(f'camera {self.stream}: {error.message}; {debug}')
            if message.type == Gst.MessageType.EOS:
                raise RuntimeError(f'camera {self.stream}: unexpected EOS')
            warning, _ = message.parse_warning()
            print(f'[rk:{self.stream}] warning: {warning.message}', file=sys.stderr, flush=True)
        if self.error:
            raise RuntimeError(self.error)
        now = self.output.clock()  # perf_counter_ns, as for all frame timing
        if now - self.last_key_ns >= KEY_REQUEST_INTERVAL_MS * 1_000_000:
            request = self.output.key_requests[self.stream]
            # The receiver's requests only while it listens: periodic IDRs (GOP)
            # cover the rest, and every request stays queued inside GstVideoEncoder
            # for the whole run. Staggering requests are rare and need no receiver.
            if request.is_set() and self.output.receiver_active():
                self._request_key(now, request)
            elif self.output.take_stagger(self.stream, now):
                self._request_key(now, None)
        health = self.output.stats[self.stream].health()
        warning = health_check(health, self.started_ns, now)
        if warning and now - self.last_send_warning_ns >= 5_000_000_000:
            self.last_send_warning_ns = now
            print(f'[rk:{self.stream}] {warning}; receiver={self.output.receiver_status()}; '
                  f'last_send_error={health["last_send_error"]}', file=sys.stderr, flush=True)

    def _request_key(self, now, request):
        """Ask the encoder for an IDR at the first frame captured from now on, for
        the receiver (request is its flag) or to stagger this stream's GOP (None)."""
        stats = self.output.stats[self.stream]
        self.last_key_ns = now
        if self.key_count >= MAX_KEY_REQUESTS:
            # GstVideoEncoder never drops a timed request here (mpph264enc never
            # flags sync points), and walks them all for every frame.
            if request is not None:
                request.clear()
                stats.add(key_request_capped=1)
            if self.key_count == MAX_KEY_REQUESTS:
                self.key_count += 1
                print(f'[rk:{self.stream}] {MAX_KEY_REQUESTS} key requests in this run: '
                      'only periodic IDRs (GOP) from now on; restart the sender to reset',
                      file=sys.stderr, flush=True)
            return
        self.key_count += 1
        # A running time, not "as soon as possible" (NONE): GstVideoEncoder
        # ignores further ASAP requests until the encoder marks an output as a
        # keyframe, and mpph264enc never does, so only the first ASAP request of a
        # run produced an IDR (10-14 logs). A timed request makes the first frame
        # captured at or after that time an IDR.
        previous = self.output.begin_key_request(self.stream) if request is not None else None
        running = self._running_time()
        running = running if running is not None and running >= 0 else self.Gst.CLOCK_TIME_NONE
        event = self.GstVideo.video_event_new_upstream_force_key_unit(running, True, self.key_count)
        accepted = bool(self.encoder.get_static_pad('src').send_event(event))
        if request is not None:
            if not accepted:
                self.output.key_request_rejected(self.stream, previous)
            stats.add(key_requests=1, key_request_rejected=int(not accepted))
        else:
            stats.add(idr_staggers=1, idr_stagger_rejected=int(not accepted))
        if self.output.frame_records is not None:  # request -> IDR delay in --frame-log
            self.output.frame_records.append(dict(s=self.stream, r='key_request', n=self.key_count, at=now,
                                                  rt=running, ok=int(accepted),
                                                  why='receiver' if request is not None else 'stagger'))

    def close(self):
        self.pipeline.set_state(self.Gst.State.NULL)


def health_check(stats, started_ns, now):
    """Fail on a dead capture/codec, but keep a live sender recoverable offline."""
    for field, message in (
            ('last_capture_ns', 'no camera frames; check device ownership/capture mode'),
            ('last_encoded_ns', 'no encoded frames; check MPP/PTS'),
            ('last_matched_ns', 'cannot match encoded PTS to application capture')):
        if now - (stats[field] or started_ns) > 5_000_000_000:
            raise RuntimeError(f'{message} for 5 seconds')
    if now - (stats['last_sent_ns'] or started_ns) > 5_000_000_000:
        return ('No complete frame sent for 5s; capture/encoder are alive. '
                'Check receiver RTP listener, destination IP and UDP path; continuing recovery')
    return None


def parse_args(argv=None):
    p = argparse.ArgumentParser(description='RK V4L2 → MPP H.264 → video_demo RTP/UDP (no ROS/RTSP)')
    p.add_argument('--version', action='version', version='rk-sender ' + VERSION)
    p.add_argument('--host', help='Receiver IPv4 address or hostname (not the robot IP)')
    p.add_argument('--port', type=int, default=5004)
    p.add_argument('--device', action='append', help='Repeat for multiple cameras; default /dev/video23')
    p.add_argument('--label', action='append', help='Repeat in device order; shown by the desktop receiver')
    p.add_argument('--width', type=int, default=1280, help='Multiple of 16 (see --height)')
    p.add_argument('--height', type=int, default=720,
                   help='Multiple of 16, or the ISP crops the camera image; about 1080p is 1920x1088')
    p.add_argument('--fps', type=int, default=30)
    p.add_argument('--bitrate-kbps', type=int, default=4000, help='Per-camera target, not total link capacity')
    p.add_argument('--gop', type=int, default=None,
                   help='IDR interval in frames; default one second. Periodic IDRs are the guaranteed '
                        'recovery; key requests (PLI) are answered within a frame or two, each with two '
                        'IDRs from MPP')
    p.add_argument('--profile', choices=['baseline', 'main', 'high'], default='baseline')
    p.add_argument('--io-mode', choices=['dmabuf', 'mmap'], default='dmabuf')
    p.add_argument('--pixel-format', choices=['NV12', 'YUY2'], default='NV12',
                   help='Camera output format. The ISP delivers NV12 as two memory planes, which '
                        'mpph264enc copies into a new buffer every frame; YUY2 is a single plane the '
                        'encoder may use directly. The robot\'s selfpaths do not negotiate YUY2 (10-14)')
    p.add_argument('--mpp-lib', metavar='PATH',
                   help="librockchip_mpp for this process only (a file, or a directory with "
                        "librockchip_mpp.so.1); the system library and other programs are unaffected. "
                        "Default: mpp-pkt/ next to rk_sender.pyz when present (MPP with "
                        "mpp-pkt-poll-fix.patch, without the two-camera encoder stalls), else the "
                        "system library. 'system' always uses the system library")
    p.add_argument('--idr-stagger', choices=['on', 'off'], default='on',
                   help="With several cameras, keep their periodic IDRs about GOP/cameras apart instead of "
                        "sending them all at once: one extra forced IDR whenever two streams' IDRs "
                        "meet (at start, after a receiver reconnects)")
    p.add_argument('--rga', action='store_true',
                   help='Let mpph264enc make that copy with RGA, which the robot camera service also '
                        'uses; by default it copies with the CPU (GST_MPP_NO_RGA=1)')
    p.add_argument('--zero-copy-pkt', choices=['auto', 'on', 'off'], default='auto',
                   help="mpph264enc zero-copy-pkt: 'on' puts each packet in a buffer allocated per frame, "
                        "'off' copies it out of MPP's own buffer; 'auto' keeps the plugin default")
    p.add_argument('--mtu', type=int, default=1200)
    p.add_argument('--max-frame-age-ms', type=float, default=1000,
                   help='Drop frames older than this (app capture to send) and wait for the next IDR; '
                        'younger late frames are still sent and counted as late. On a LAN a late frame '
                        'is a short hitch, a dropped one a freeze until the next IDR')
    p.add_argument('--send-budget-ms', type=float, default=15,
                   help='Maximum socket send time per non-IDR frame (receiver reassembles P frames for 20 ms)')
    p.add_argument('--idr-send-budget-ms', type=float, default=45,
                   help='Maximum socket send time per IDR frame (receiver reassembles IDRs for 50 ms)')
    p.add_argument('--duration', type=float, default=0, help='Seconds; 0 runs until Ctrl+C/SIGTERM')
    p.add_argument('--stats-file', help='Optional compact JSONL stats; never saves camera images')
    p.add_argument('--trace', help='Diagnostics: JSONL from a separate read-only sampler process: '
                                   'encoder/RGA interrupts every 5 ms, our threads\' state and wait '
                                   'point every 20 ms (see deploy/rk_sender/README.md)')
    p.add_argument('--frame-log', help='Optional JSONL with one line per encoded frame (stage timings '
                                       'driver/queue/encode/handoff/send, outcome) and per key request. '
                                       'Also installs the encoder probes behind queue/encode/handoff')
    p.add_argument('--doctor', action='store_true', help='Check dependencies and device permissions without streaming')
    p.add_argument('--print-pipeline', action='store_true', help='Print pipeline without loading GI or opening devices')
    args = p.parse_args(argv)
    args.device = args.device or ['/dev/video23']
    args.gop = args.fps if args.gop is None else args.gop
    if not 1 <= len(args.device) <= 8 or len(set(args.device)) != len(args.device):
        p.error('Specify 1–8 distinct camera nodes')
    if any(not re.fullmatch(r'/dev/video[0-9]+', d) for d in args.device):
        p.error('Devices must be explicit /dev/videoN nodes')
    if args.label and (len(args.label) != len(args.device) or any(not s.strip() or len(s) > 100 for s in args.label)):
        p.error('Provide one nonempty --label (at most 100 characters) per device')
    args.label = args.label or [f'RK {d}' for d in args.device]
    for name, low, high in (('port', 1, 65535), ('width', 96, 3840), ('height', 64, 2160),
                           ('fps', 1, 60), ('bitrate_kbps', 100, 50000), ('gop', 1, 3600),
                           ('mtu', 256, 1400), ('max_frame_age_ms', 10, 5000), ('send_budget_ms', 1, 50),
                           ('idr_send_budget_ms', 1, 100)):
        value = getattr(args, name)
        if not math.isfinite(value) or not low <= value <= high:
            p.error(f'{name} must be in {low}..{high}')
    if args.width % 16 or args.height % 16:
        p.error('RK capture width and height must be multiples of 16: MPP pads frames to 16, and GStreamer '
                'then has the ISP crop the camera image instead of the padding (1920x1080 showed only the '
                'top-left quarter). For about 1080p use --width 1920 --height 1088, or 1792x1008 for '
                'exactly 16:9')
    if not math.isfinite(args.duration) or args.duration < 0:
        p.error('duration must be finite and >= 0')
    if args.send_budget_ms > args.max_frame_age_ms:
        p.error('send-budget-ms must not exceed max-frame-age-ms')
    if not (args.doctor or args.print_pipeline):
        if not args.host or not args.host.strip() or any(c in args.host for c in '/:'):
            p.error('--host must be the receiver IPv4 address or hostname')
        args.host = args.host.strip()
    return args


def run(args, Gst, GstVideo):
    peer = (socket.gethostbyname(args.host), args.port)
    output = RtpOutput(peer, args.label, mtu=args.mtu, max_age_ms=args.max_frame_age_ms,
                       send_budget_ms=args.send_budget_ms, idr_send_budget_ms=args.idr_send_budget_ms,
                       gop=args.gop, fps=args.fps, stagger=args.idr_stagger == 'on',
                       frame_log=bool(args.frame_log))
    cameras = []
    stats_file = frame_file = syncer = tracer = None
    handlers = {}
    started = time.perf_counter()
    cpu_started = time.process_time()
    previous_time, previous_cpu = started, cpu_started
    previous_counts = [dict() for _ in args.device]
    try:
        if args.stats_file:
            stats_file = open(Path(args.stats_file).expanduser(), 'a', encoding='utf-8', buffering=1)
        for sig in (signal.SIGINT, signal.SIGTERM):
            handlers[sig] = signal.signal(sig, lambda *_: output.stop.set())
        check_camera_crops(args.device, reset=True)  # before v4l2src opens the nodes
        for i, device in enumerate(args.device):
            cameras.append(CameraPipeline(Gst, GstVideo, args, device, i, output))
        settings = dict(pixel_format=args.pixel_format, rga='GST_MPP_NO_RGA' not in os.environ,
                        zero_copy_pkt=cameras[0].zero_copy_pkt, idr_stagger=output.stagger,
                        **getattr(args, 'mpp', None) or {})
        if args.frame_log:
            frame_file = open(Path(args.frame_log).expanduser(), 'a', encoding='utf-8')
            frame_file.write(json.dumps(dict(event='start', version=VERSION, epoch=output.epoch,
                devices=args.device, labels=args.label, fps=args.fps, gop=args.gop, **settings,
                clock='perf_counter_ns', fields=FRAME_FIELDS), ensure_ascii=False) + '\n')
        syncer = Syncer(stats_file, frame_file)
        if args.trace:  # before the pipelines start, so their threads are seen as they appear
            tracer = start_tracer(os.getpid(), str(Path(args.trace).expanduser()))
        output.start()
        for camera in cameras:
            camera.start()
        print(f'[rk {VERSION}] {args.device} → {peer[0]}:{peer[1]} RTP; {args.width}x{args.height}@{args.fps} '
              f'{args.pixel_format}; RGA {"on" if settings["rga"] else "off"}; '
              f'zero-copy-pkt {settings["zero_copy_pkt"]}; {args.bitrate_kbps} kbps/camera; GOP {args.gop}; '
              f'IDR stagger {"on" if output.stagger and len(cameras) > 1 else "off"}; '
              f'feedback={output.sock.getsockname()[1]}', flush=True)
        print('[rk] timing starts at V4L2 source-pad delivery; sensor/exposure latency is unavailable', flush=True)
        next_status = started + 2
        while not output.stop.wait(.01):
            for camera in cameras:
                camera.poll()
            if frame_file:
                drain_frames(output, frame_file)
            now = time.perf_counter()
            if args.duration and now - started >= args.duration:
                break
            if now >= next_status:
                cpu = time.process_time()
                elapsed = now - previous_time
                rows = []
                for i, stats in enumerate(output.stats):
                    row = stats.snapshot()
                    prior = previous_counts[i]
                    row.update(stream=i, label=args.label[i],
                               sent_fps=round((row.get('sent', 0) - prior.get('sent', 0)) / elapsed, 2),
                               wire_mbps=round((row.get('wire_bytes', 0) - prior.get('wire_bytes', 0)) * 8 / elapsed / 1e6, 3))
                    previous_counts[i] = row.copy()
                    rows.append(row)
                record = dict(event='sample', elapsed_s=round(now - started, 2),
                              process_cpu_percent=round(100 * (cpu - previous_cpu) / elapsed, 1),
                              receiver=output.receiver_status(), streams=rows)
                line = json.dumps(record, ensure_ascii=False)
                print(line, flush=True)
                if stats_file:
                    stats_file.write(line + '\n')
                # Survive a board hang: these logs are how a crash gets diagnosed.
                syncer.sync()
                previous_time, previous_cpu, next_status = now, cpu, now + 2
        # A callback can set stop before the main loop next polls its errors.
        for camera in cameras:
            if camera.error:
                raise RuntimeError(camera.error)
    finally:
        output.stop.set()
        for camera in cameras:
            try:
                camera.close()
            except Exception as exc:
                print(f'[rk] cleanup: {exc}', file=sys.stderr, flush=True)
        output.close()
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
        record = dict(event='stop', receiver=output.receiver_status(), streams=[s.snapshot() for s in output.stats])
        print(json.dumps(record, ensure_ascii=False), flush=True)
        if stats_file:
            stats_file.write(json.dumps(record, ensure_ascii=False) + '\n')
        if frame_file:
            drain_frames(output, frame_file)
        if syncer:
            syncer.join()
        durable(stats_file, frame_file)
        for handle in (stats_file, frame_file):
            if handle:
                handle.close()
        if tracer:
            stop_tracer(tracer)


FRAME_FIELDS = dict(
    s='stream', f='frame id', k='1 = IDR', b='encoded bytes', c='app capture (perf_counter_ns)',
    d='driver ms: V4L2 timestamp -> v4l2src', q='queue ms: -> encoder input',
    e='encode ms: -> encoder output', h='handoff ms: -> Python callback',
    a='age ms at send decision', t='send ms', r='outcome',
    p='PTS: pipeline running time ns of the V4L2 timestamp',
    w='callback done (perf_counter_ns): the encoder output thread is free again; until then the '
      'next frame cannot enter the encoder, so its q/e include this frame\'s send',
    key_request='records with r=key_request: n = request number, at = time sent (perf_counter_ns), '
                'rt = requested running time ns, ok = accepted by the encoder pad, why = receiver '
                '(PLI, reconnect, broken chain) or stagger (moves this stream\'s IDRs away from the '
                'other streams\'); the answer is the first k=1 frame of that stream with p >= rt')


def drain_frames(output, handle):
    records = output.frame_records
    while records:
        handle.write(json.dumps(records.popleft(), separators=(',', ':')) + '\n')


def durable(*handles):
    for handle in handles:
        if handle:
            try:
                handle.flush()
                os.fsync(handle.fileno())
            except (OSError, ValueError):
                pass


class Syncer:
    """Flush in the main loop, fsync in the background: a slow eMMC write must
    not delay poll(), which sends key requests and checks pipeline errors."""
    def __init__(self, *handles):
        self.handles = [h for h in handles if h]
        self.thread = None

    def sync(self):
        fds = []
        for handle in self.handles:
            try:
                handle.flush()
                fds.append(handle.fileno())
            except (OSError, ValueError):
                pass
        if fds and (self.thread is None or not self.thread.is_alive()):
            self.thread = threading.Thread(target=self._fsync, args=(fds,), name='rk-fsync', daemon=True)
            self.thread.start()

    @staticmethod
    def _fsync(fds):
        for fd in fds:
            try:
                os.fsync(fd)
            except OSError:
                pass

    def join(self, timeout=5):
        if self.thread is not None:
            self.thread.join(timeout)


# --------------------------------------------------------------------- --trace
# A separate sampler process (no GIL shared with the sender) that only reads
# /proc and /sys:
#   * every 5 ms, how many hardware jobs the encoder cores and RGA finished
#     (each interrupt is one job, the robot's or ours);
#   * every 20 ms, the state of each of our threads and the kernel function a
#     sleeping one waits in (wchan), plus the interrupt threads of those
#     devices and dmabuf-deferred;
#   * every second, CPU time per thread.
# Lined up with --frame-log (same perf_counter_ns clock) it shows, for a frame
# stuck in the encoder, whether the hardware was busy with other jobs or idle,
# and where our encoder threads were waiting.

TRACE_IRQS = ('rkvenc', 'rga')
TRACE_KTHREADS = ('dmabuf-deferred',)
TRACE_FIELDS = dict(
    irq='{irq: hardware jobs finished since the previous record}, irq numbers in trace_start.irqs',
    th='[[tid, state, wchan, cpu]] for our threads whose state or wait point changed; '
       'state R running/runnable, S sleeping, D uninterruptible; wchan 0 when running',
    kt='the same for the watched kernel threads (trace_start.kthreads)',
    names='{tid: thread name} when a thread appears or is renamed',
    cpu='{tid: CPU ms during the last second}, ours and the watched kernel threads')


def find_irqs(text, patterns=TRACE_IRQS):
    """{irq: description} for /proc/interrupts lines mentioning one of patterns."""
    found = {}
    for line in text.splitlines()[1:]:
        head, sep, rest = line.partition(':')
        if sep and head.strip().isdigit() and any(p in rest for p in patterns):
            found[int(head)] = ' '.join(w for w in rest.split() if not w.isdigit())
    return found


def _leading_sum(words):
    total = 0
    for word in words:
        if not word.isdigit():
            break
        total += int(word)
    return total


class IrqCounts:
    def __init__(self, irqs):
        self.irqs = sorted(irqs)
        self.sysfs = bool(self.irqs) and all(
            os.path.exists(f'/sys/kernel/irq/{n}/per_cpu_count') for n in self.irqs)

    def read(self):
        counts = {}
        if not self.irqs:
            return counts
        if self.sysfs:
            for n in self.irqs:
                try:
                    with open(f'/sys/kernel/irq/{n}/per_cpu_count') as f:
                        counts[n] = sum(int(x) for x in f.read().split(',') if x.strip())
                except (OSError, ValueError):
                    pass
            return counts
        wanted = set(self.irqs)
        try:
            with open('/proc/interrupts') as f:
                for line in f:
                    head, sep, rest = line.partition(':')
                    if sep and head.strip().isdigit() and int(head) in wanted:
                        counts[int(head)] = _leading_sum(rest.split())
        except OSError:
            pass
        return counts


def _task_state(path):
    """(state, wchan, cpu, ticks) of one /proc task directory, or None."""
    try:
        with open(path + '/stat') as f:
            stat = f.read()
        with open(path + '/wchan') as f:
            wchan = f.read().strip() or '0'
    except OSError:
        return None
    rest = stat[stat.rfind(')') + 2:].split()
    if len(rest) < 37:
        return None
    return rest[0], wchan, int(rest[36]), int(rest[11]) + int(rest[12])


def _comm(path):
    try:
        with open(path + '/comm') as f:
            return f.read().strip()
    except OSError:
        return None


def find_kthreads(irqs, names=TRACE_KTHREADS):
    """{pid: name} of the interrupt threads of irqs (PREEMPT_RT) and named kernel threads."""
    prefixes = tuple(f'irq/{n}-' for n in irqs)
    found = {}
    for entry in os.listdir('/proc'):
        if entry.isdigit():
            name = _comm(f'/proc/{entry}')
            if name and (name.startswith(prefixes) or name in names):
                found[int(entry)] = name
    return found


def trace_main(argv):
    """Sample until process argv[0] exits (or SIGINT/SIGTERM); append JSONL to argv[1]."""
    pid, path = int(argv[0]), argv[1]
    stopping = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopping.append(1))
    try:
        with open('/proc/interrupts') as f:
            irqs = find_irqs(f.read())
    except OSError:
        irqs = {}
    counter = IrqCounts(irqs)
    kthreads = find_kthreads(irqs)
    hz = os.sysconf('SC_CLK_TCK')
    out = open(path, 'a', encoding='utf-8')

    def write(record):
        out.write(json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n')

    write(dict(event='trace_start', version=VERSION, pid=pid, clock='perf_counter_ns', irq_period_ms=5,
               thread_period_ms=20, irq_source='sysfs' if counter.sysfs else 'proc',
               irqs={str(k): v for k, v in irqs.items()},
               kthreads={str(k): v for k, v in kthreads.items()}, fields=TRACE_FIELDS))
    task_dir = f'/proc/{pid}/task'
    last_irq = counter.read()
    last_th, last_kt, names, ticks = {}, {}, {}, {}
    tick, reason = 0, 'stopped'
    next_wake = time.monotonic()
    next_cpu = next_sync = next_wake + 1
    try:
        while not stopping:
            now = time.perf_counter_ns()
            counts = counter.read()
            delta = {str(n): c - last_irq[n] for n, c in counts.items() if n in last_irq and c != last_irq[n]}
            last_irq.update(counts)
            if delta:
                write(dict(t=now, irq=delta))
            if tick % 4 == 0:
                try:
                    tids = os.listdir(task_dir)
                except OSError:
                    reason = 'process exited'
                    break
                changed, seen = [], {}
                for tid in tids:
                    state = _task_state(f'{task_dir}/{tid}')
                    if state is None:
                        continue
                    seen[tid] = state
                    if last_th.get(tid, (None, None))[:2] != state[:2]:
                        changed.append([int(tid), state[0], state[1], state[2]])
                    if tid not in names:
                        names[tid] = None
                last_th = seen
                if changed:
                    write(dict(t=now, th=changed))
                kchanged = []
                for kpid in kthreads:
                    state = _task_state(f'/proc/{kpid}')
                    if state is None:
                        continue
                    if last_kt.get(kpid, (None, None))[:2] != state[:2]:
                        kchanged.append([kpid, state[0], state[1], state[2]])
                    last_kt[kpid] = state
                if kchanged:
                    write(dict(t=now, kt=kchanged))
            if tick % 50 == 0:  # thread names: GStreamer renames its threads once they start
                renamed = {}
                for tid in list(names):
                    name = _comm(f'{task_dir}/{tid}')
                    if name is not None and name != names[tid]:
                        names[tid] = name
                        renamed[tid] = name
                if renamed:
                    write(dict(t=now, names=renamed))
            current = time.monotonic()
            if current >= next_cpu:
                cpu = {}
                for key, state in list(last_th.items()) + [(str(k), v) for k, v in last_kt.items()]:
                    used = state[3] - ticks.get(key, state[3])
                    ticks[key] = state[3]
                    if used:
                        cpu[key] = round(used * 1000 / hz)
                if cpu:
                    write(dict(t=now, cpu=cpu))
                next_cpu = current + 1
            if current >= next_sync:
                durable(out)
                next_sync = current + 2
            if os.getppid() != pid and not os.path.isdir(task_dir):
                reason = 'process exited'
                break
            tick += 1
            next_wake += .005
            delay = next_wake - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_wake = time.monotonic()
    finally:
        write(dict(event='trace_stop', t=time.perf_counter_ns(), reason=reason))
        durable(out)
        out.close()
    return 0


def start_tracer(pid, path):
    """Run trace_main in a fresh interpreter (works from the source tree and from the .pyz)."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = ('import sys; sys.path.insert(0, sys.argv[1]); '
            'from video_demo.rk_sender import trace_main; sys.exit(trace_main(sys.argv[2:]))')
    return subprocess.Popen([sys.executable, '-c', code, root, str(pid), path], stdin=subprocess.DEVNULL)


def stop_tracer(process, timeout=3):
    try:
        process.send_signal(signal.SIGTERM)
        process.wait(timeout)
    except subprocess.TimeoutExpired:
        process.kill()
    except OSError:
        pass


def main(argv=None):
    args = parse_args(argv)
    if args.print_pipeline:
        for i, device in enumerate(args.device):
            print(pipeline_description(args, device, i))
        return 0
    configure_rga(args.rga)  # before the MPP plugin loads and reads it
    try:
        expected_mpp = None
        choice = args.mpp_lib
        if choice is None:
            bundled = bundled_mpp_lib()
            if bundled is not None:
                print(f'[rk] using {bundled} next to the pyz (--mpp-lib system for the board\'s MPP)',
                      flush=True)
            elif bundle_dir() is not None:
                print(f'[rk] no {MPP_BUNDLED}/ next to the pyz: the board\'s MPP, which stalls with two '
                      'cameras (deploy/rk_sender/README.md)', flush=True)
            choice = 'system' if bundled is None else str(bundled)
        if choice != 'system':  # before GStreamer loads mpph264enc and, with it, an MPP library
            expected_mpp = resolve_mpp_lib(choice)
            preload_mpp(expected_mpp)
        Gst, GstVideo = load_gst()
        check_runtime(Gst, args)  # instantiates mpph264enc
        args.mpp = check_mpp_library(expected_mpp)
        if args.doctor:
            check_camera_crops(args.device, reset=False)
        else:
            run(args, Gst, GstVideo)
    except (RuntimeError, OSError) as exc:
        print(f'[rk] ERROR: {exc}', file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
