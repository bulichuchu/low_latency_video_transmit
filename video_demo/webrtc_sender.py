"""WebRTC transport for the sender: the same H.264 frames, sent by aiortc to a browser.

Capture, encoder and timing are those of the RTP transport. aiortc only
packetizes, encrypts (DTLS-SRTP) and answers NACK/PLI; the browser receives
with its native WebRTC stack. The receiver service relays offer and answer over
the existing UDP control path (webrtc_signal). While a browser is connected,
frames go to WebRTC instead of RTP; when it leaves, RTP resumes.

The browser's RTCP (loss, round trip, REMB) drives a RateController
(congestion.py); the encode threads read rate() and reopen the encoder at the
new bitrate and frame rate. Without it a path slower than the configured
bitrate queues and drops (10-09 VPN run).

aiortc is optional and installed without its PyAV pin (requirements-webrtc.txt).
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
import contextvars
from dataclasses import replace
from fractions import Fraction
import json
import re
import threading
import time

import av
from aiortc import MediaStreamTrack, RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiortc.rtp import RTCP_PSFB_APP, RtcpPsfbPacket, RtcpRrPacket, RtcpSrPacket, unpack_remb_fci

from .congestion import RateController
from .protocol import nals
from .webrtc_signal import Reassembler, datagrams

CLOCK = 90000
TIME_BASE = Fraction(1, CLOCK)
# The x264 stream is High profile. Baseline entries only matter for a browser
# that offers nothing else; the decoder follows the bitstream either way.
H264_PROFILES = ('640c1f', '64001f', '42e01f', '42001f')
PLAYOUT_DELAY_URI = 'http://www.webrtc.org/experiments/rtp-hdrext/playout-delay'
# min = max = 0 (10 ms units): Chrome decodes and renders each frame at once
# instead of holding it in its adaptive jitter buffer. The page leaves this
# extension out of its offer to measure the browser's default buffering.
PLAYOUT_DELAY_ZERO = b'\x00\x00\x00'
MAX_QUEUED_FRAMES = 3
# Sent packets kept per stream for NACK. aiortc keeps 128, about 0.27 s at
# 720p60 5 Mbps: once the round trip grows, requests come too late. 4096 covers
# 2 s up to ~16 Mbps per stream (~5 MB); it must divide 65536 (seq % size).
RTP_HISTORY = 4096
# aiortc resends every NACKed packet it still has, each time it is asked. With
# a long history that turns loss into a retransmission storm (a closed-loop
# test reached 10x the video rate), so retransmissions are limited: frames
# older than MAX_RTX_AGE_S are past showing at playout delay 0; a packet goes
# at most RTX_TRIES times and not twice within a round trip (browsers re-NACK
# every round trip); and per stream they may use RTX_SHARE of its target.
MAX_RTX_AGE_S = 1.0
RTX_TRIES = 2
RTX_MIN_GAP_S = .1
RTX_SHARE = .25
FEEDBACK_LOG_S = 1.0
# Sender reports remembered per stream for round trips: aiortc keeps only its
# last one, so a receiver report answering an older one (round trip longer
# than the 0.5-1.5 s report interval) gave none, just when the queue is long.
SR_HISTORY = 16
SENT_WINDOW_S = 1.0  # what we sent lately, for the browser's first REMB
MDNS_TIMEOUT_S = .5
FIRST_CHECK_WAIT_S = 10
START = b'\x00\x00\x00\x01'
CANDIDATE = re.compile(r'a=candidate:\S+ \d+ \S+ \d+ (\S+) ')

_from_zero = contextvars.ContextVar('rtp_timestamp_from_zero', default=False)
_prepared = False


def pts90k(capture_ns):
    """RTP clock (90 kHz) on the sender's capture clock."""
    return capture_ns * 9 // 100_000


def frame_info(meta, pts, rate=None):
    """Timing of one frame for the browser (sender clock, ms), keyed by the RTP
    timestamp that requestVideoFrameCallback reports for it. `rate` is the
    encoder's (bps, fps) for this frame, shown by the page."""
    ready = meta.sensor_status == 'ready'
    info = dict(type='frame', s=meta.stream, p=pts % 2**32, e=meta.epoch, f=meta.frame_id,
                c=meta.capture_ns / 1e6, x=meta.encode_us / 1000, k=int(meta.key), d=int(meta.depth_preview),
                ss=meta.sensor_status, sc=meta.sensor_capture_ns / 1e6 if ready else None,
                su=meta.sensor_clock_uncertainty_us / 1000 if ready else None)
    if rate is not None:
        info.update(rb=round(rate[0] / 1000), rf=rate[1])
    return info


def _prepare_aiortc():
    """Narrow adjustments to aiortc/aioice, applied once per process.

    * RTP timestamps start at 0 instead of a random origin, so the browser's
      rtpTimestamp is our capture-clock pts and maps each shown frame to its
      metadata exactly. Only the RTP task's own random32() call is affected
      (a context variable set in that task); SSRCs stay random.
    * Offer H.264 High and Constrained High; aiortc lists only baseline.
    * Send the playout-delay header extension when the browser offers it.
    * Keep RTP_HISTORY sent packets per stream for retransmission.
    * Wait for the browser's first connectivity check when none of its
      candidates is usable (Chrome hides host addresses behind mDNS names).
    """
    global _prepared
    if _prepared:
        return
    from aioice import ice
    from aiortc import rtcrtpsender, rtp
    from aiortc.codecs import CODECS, HEADER_EXTENSIONS
    from aiortc.rtcrtpparameters import (RTCRtcpFeedback, RTCRtpCodecParameters,
                                         RTCRtpHeaderExtensionParameters)

    # Read by RTCRtpSender._run_rtp (store) and _retransmit (look-up) at call time.
    rtcrtpsender.RTP_HISTORY_SIZE = RTP_HISTORY
    random32 = rtcrtpsender.random32
    rtcrtpsender.random32 = lambda: 0 if _from_zero.get() else random32()
    run_rtp = rtcrtpsender.RTCRtpSender._run_rtp

    async def run_rtp_from_zero(self, codec):
        _from_zero.set(True)
        return await run_rtp(self, codec)
    rtcrtpsender.RTCRtpSender._run_rtp = run_rtp_from_zero

    payload_type = 121
    for profile in ('640c1f', '64001f'):
        CODECS['video'] += [
            RTCRtpCodecParameters(
                mimeType='video/H264', clockRate=CLOCK, payloadType=payload_type,
                rtcpFeedback=[RTCRtcpFeedback(type='nack'), RTCRtcpFeedback(type='nack', parameter='pli'),
                              RTCRtcpFeedback(type='goog-remb')],
                parameters={'level-asymmetry-allowed': '1', 'packetization-mode': '1',
                            'profile-level-id': profile}),
            RTCRtpCodecParameters(mimeType='video/rtx', clockRate=CLOCK, payloadType=payload_type + 1,
                                  parameters={'apt': payload_type}),
        ]
        payload_type += 2

    HEADER_EXTENSIONS['video'].append(RTCRtpHeaderExtensionParameters(id=6, uri=PLAYOUT_DELAY_URI))

    class ExtensionsMap(rtp.HeaderExtensionsMap):
        playout_delay_id = None

        def configure(self, parameters):
            super().configure(parameters)
            self.playout_delay_id = next((x.id for x in parameters.headerExtensions
                                          if x.uri == PLAYOUT_DELAY_URI), None)

        def set(self, values):
            profile, value = super().set(values)
            if not self.playout_delay_id:
                return profile, value
            return rtp.pack_header_extensions(rtp.unpack_header_extensions(profile, value)
                                              + [(self.playout_delay_id, PLAYOUT_DELAY_ZERO)])
    rtp.HeaderExtensionsMap = ExtensionsMap

    connect, close = ice.Connection.connect, ice.Connection.close

    async def connect_after_first_check(self):
        # aioice gives up at once with no candidate pair. The browser starts its
        # checks as soon as it has our answer; each reveals its address.
        deadline = time.monotonic() + FIRST_CHECK_WAIT_S
        while not self._remote_candidates and not self._early_checks:
            if self._remote_candidates_end or time.monotonic() > deadline:
                raise ConnectionError('no connectivity check from the browser')
            await asyncio.sleep(.02)
        await connect(self)

    async def close_and_end_checks(self):
        # Without end-of-candidates connect() keeps polling for more remote
        # candidates, and with no candidate pair close() never ends it. Mark
        # the candidates complete so a closed connection stops polling.
        self._remote_candidates_end = True
        await close(self)
    ice.Connection.connect = connect_after_first_check
    ice.Connection.close = close_and_end_checks
    _prepared = True


def limit_packet_size(mtu):
    """UDP payload no larger than the RTP path's packets (--mtu, default 1200):
    RTP header 12 + header extensions 16 + SRTP tag (AES-GCM) 16 + RTX 2."""
    from aiortc.codecs import h264
    h264.PACKET_MAX = max(256, mtu - 46)


def h264_preferences():
    from aiortc.codecs import CODECS
    from aiortc.rtcrtpparameters import RTCRtpCodecCapability
    local = {c.parameters.get('profile-level-id'): c for c in CODECS['video'] if c.mimeType == 'video/H264'}
    preferences = [RTCRtpCodecCapability(mimeType=c.mimeType, clockRate=c.clockRate, parameters=c.parameters)
                   for c in (local.get(p) for p in H264_PROFILES) if c]
    return preferences + [RTCRtpCodecCapability(mimeType='video/rtx', clockRate=CLOCK)]


async def usable_offer(sdp):
    """Resolve the browser's mDNS host candidates briefly; drop the rest.

    Chrome replaces host addresses with random <uuid>.local names. aioice would
    resolve each one in turn with a 1 s timeout; resolve them all at once here.
    A name that does not resolve (multicast filtered) costs nothing: the
    browser's own checks reveal its address as a peer-reflexive candidate.
    """
    lines = sdp.split('\r\n')
    names = sorted({m.group(1) for line in lines if (m := CANDIDATE.match(line)) and m.group(1).endswith('.local')})
    resolved = {}
    if names:
        from aioice import mdns
        try:
            protocol = await mdns.create_mdns_protocol()
        except OSError:
            protocol = None
        if protocol is not None:
            try:
                answers = await asyncio.gather(*(protocol.resolve(name, timeout=MDNS_TIMEOUT_S) for name in names))
                resolved = {name: ip for name, ip in zip(names, answers) if ip}
            finally:
                await protocol.close()
    kept = []
    for line in lines:
        m = CANDIDATE.match(line)
        if m and m.group(1).endswith('.local'):
            if m.group(1) not in resolved:
                continue
            line = line[:m.start(1)] + resolved[m.group(1)] + line[m.end(1):]
        # With candidates dropped, end-of-candidates would make aioice prune
        # the component and gather nothing for the answer (Firefox sends it).
        if line == 'a=end-of-candidates' and len(resolved) < len(names):
            continue
        kept.append(line)
    return '\r\n'.join(kept), dict(mdns_candidates=len(names), mdns_resolved=len(resolved))


class EncodedTrack(MediaStreamTrack):
    """Hands encoded av.Packets to aiortc, which packs them without re-encoding."""
    kind = 'video'

    def __init__(self, stream, journal):
        super().__init__()
        self.stream, self.journal = stream, journal
        self.queue = asyncio.Queue()
        self.awaiting_key = True  # every connection starts at an IDR
        self.pulled = None

    async def recv(self):
        if self.pulled is not None:
            # Since the previous recv() returned, aiortc has packetized,
            # encrypted and sent that frame.
            frame_id, pushed, pulled = self.pulled
            self.journal.log('webrtc_tx', stream=self.stream, frame_id=frame_id,
                             webrtc_queue_ms=(pulled - pushed) / 1e6,
                             webrtc_send_ms=(time.perf_counter_ns() - pulled) / 1e6)
            self.pulled = None
        packet, frame_id, pushed = await self.queue.get()
        self.pulled = (frame_id, pushed, time.perf_counter_ns())
        return packet


class WebRTCPublisher:
    """One browser viewer at a time, on an asyncio loop of its own.

    handle(), push() and rate() are called from the sender's threads; everything
    that touches aiortc runs on the loop.

    bitrate/fps (the configured per-stream rate) enable congestion control:
    rate() then tells the encode threads what to encode while a browser is
    connected. on_route(host) hears where the video goes: the browser's IP, or
    None when it runs on this computer or the session ended (AWDL guard).
    """
    def __init__(self, streams, send, request_keyframe, journal, mtu=1200, stream_names=(),
                 bitrate=None, fps=None, on_route=None):
        _prepare_aiortc()
        limit_packet_size(mtu)
        self.streams, self.send = streams, send
        self.stream_names = list(stream_names)[:streams]
        self.request_keyframe, self.journal = request_keyframe, journal
        self.bitrate, self.fps, self.on_route = bitrate, fps, on_route
        self.signals = Reassembler()
        self.answers = {}
        self.seen = OrderedDict()  # offered sessions; only the latest is answered
        self.latest = None
        self.answer_lock = asyncio.Lock()
        self.pc = self.tracks = self.channel = self.session = None
        self.route = {}
        self.connected = False  # read by the encode threads: frames go here, not to RTP
        self.control = None     # RateController of the current session
        self.rates = None       # [(bps, fps)] per WebRTC stream, read by the encode threads
        self.ssrcs = {}         # our SSRC -> (stream, RTCRtpSender)
        self.last_rtcp = None
        self.logged_at = 0.0
        self.rtx = self.rtx_tries = self.rtx_tokens = None  # per session, see _rtx_allowed
        self.sr_sent = {}      # our SSRC -> {LSR: wall time we sent that sender report}
        self.sent_log = deque()  # (monotonic time, bytes) handed to aiortc, all streams
        self.route_host = None
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name='webrtc', daemon=True)
        self.thread.start()

    # -- sender threads -------------------------------------------------------
    def handle(self, msg):
        """A validated signaling datagram from the receiver (control thread)."""
        text = self.signals.add(msg)
        if text is not None:
            self.loop.call_soon_threadsafe(self._signal, msg['type'], msg['id'], text)

    def push(self, stream, data, meta, rate=None):
        """Encode thread: send one encoded frame to the browser.

        Returns its Meta with size and IDR flag filled in, or None when no
        browser is connected; the caller then sends the frame over RTP.
        `rate` is the encoder's (bps, fps), passed on to the page.
        """
        tracks = self.tracks
        if not self.connected or tracks is None or stream >= len(tracks):
            return None  # no viewer, or the page asked for fewer streams: RTP
        units = nals(data)
        bitstream = data if data.startswith((START, b'\x00\x00\x01')) else b''.join(START + n for n in units)
        meta = replace(meta, frame_bytes=sum(len(n) + 4 for n in units),
                       key=any(n[0] & 31 == 5 for n in units))
        self.loop.call_soon_threadsafe(self._push, stream, bitstream, meta, time.perf_counter_ns(), rate)
        return meta

    def rate(self, stream):
        """Encode thread: (bps, fps) to encode this stream at, or None to use
        the configured rate (no viewer, or the stream is not sent over WebRTC)."""
        rates = self.rates
        if not self.connected or rates is None or stream >= len(rates):
            return None
        return rates[stream]

    def stop(self):
        future = asyncio.run_coroutine_threadsafe(self._close(), self.loop)
        try:
            future.result(timeout=5)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)

    # -- loop -------------------------------------------------------------------
    def _signal(self, kind, session, text):
        if kind == 'webrtc_close':
            if session == self.session:
                self.journal.log('webrtc_closed_by_viewer', session=session)
                asyncio.ensure_future(self._close())
        elif kind == 'webrtc_offer':
            if session in self.answers:  # the receiver resent: our answer was lost
                for data in self.answers[session]:
                    self.send(data)
            elif session not in self.seen:  # resends of older sessions are stale
                self.seen[session] = True
                while len(self.seen) > 32:
                    self.seen.popitem(last=False)
                self.latest = session
                asyncio.ensure_future(self._answer(session, text))

    async def _answer(self, session, offer):
        # One at a time: a second offer arriving during the mDNS wait or SDP
        # handling must not interleave with this one's setup and teardown.
        async with self.answer_lock:
            if session != self.latest:
                return  # superseded while waiting; the receiver no longer asks
            reply = await self._build_answer(session, offer)
            self.answers = {session: reply}
            for data in reply:
                self.send(data)

    async def _build_answer(self, session, offer):
        started = time.perf_counter()
        pc = None
        try:
            await self._close()
            offer, candidates = await usable_offer(offer)
            # One transceiver per video section offered, at most our streams:
            # streams the page did not ask for stay on RTP (the receiver drops
            # them, as in RTP mode). An extra transceiver would fail the answer.
            offered = sum(line.startswith('m=video') for line in offer.split('\r\n'))
            count = min(self.streams, offered)
            if not count:
                raise ValueError('接收页的 offer 没有视频轨道')
            pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
            self.pc, self.session, self.route = pc, session, {}
            tracks, ssrcs = [], {}
            for stream in range(count):
                track = EncodedTrack(stream, self.journal)
                transceiver = pc.addTransceiver(track, direction='sendonly')
                transceiver.setCodecPreferences(h264_preferences())
                sender = transceiver.sender
                # PLI/FIR from the browser -> IDR from our encoder.
                sender._send_keyframe = lambda stream=stream: self._keyframe(stream)
                # Receiver reports and REMB also feed the rate controller.
                ssrcs[sender._ssrc] = (stream, sender)

                async def handle_rtcp(packet, handle=sender._handle_rtcp_packet, pc=pc):
                    if pc is self.pc:
                        try:
                            self._feedback(packet)
                        except Exception as exc:  # never break aiortc's own RTCP handling
                            self.journal.log('webrtc_error', session=session,
                                             error=f'rate control: {type(exc).__name__}: {exc}')
                    await handle(packet)

                async def retransmit(seq, resend=sender._retransmit, sender=sender, stream=stream, pc=pc):
                    if pc is self.pc and self._rtx_allowed(stream, sender, seq):
                        await resend(seq)

                async def send_rtcp(packets, send=sender._send_rtcp, ssrc=sender._ssrc, pc=pc):
                    if pc is self.pc:
                        for packet in packets:
                            if isinstance(packet, RtcpSrPacket):
                                sent = self.sr_sent.setdefault(ssrc, OrderedDict())
                                sent[(packet.sender_info.ntp_timestamp >> 16) & 0xFFFFFFFF] = time.time()
                                while len(sent) > SR_HISTORY:
                                    sent.popitem(last=False)
                    await send(packets)
                sender._handle_rtcp_packet = handle_rtcp
                sender._retransmit = retransmit
                sender._send_rtcp = send_rtcp
                tracks.append(track)
            self.tracks, self.ssrcs, self.last_rtcp = tracks, ssrcs, None
            self.control = RateController(count, self.bitrate, self.fps) if self.bitrate and self.fps else None
            self.rates = [self.control.target] * count if self.control else None
            self.rtx = dict(sent=0, old=0, repeat=0, budget=0)
            self.rtx_tries, self.rtx_tokens = OrderedDict(), {}
            self.sr_sent, self.sent_log = {}, deque()

            @pc.on('datachannel')
            def on_datachannel(channel):
                channel.on('message', lambda message: self._message(channel, message))
                if pc is self.pc:
                    self.channel = channel
                    if self.route:
                        channel.send(json.dumps(dict(type='route', **self.route)))

            @pc.on('connectionstatechange')
            def on_state():
                state = pc.connectionState
                current = pc is self.pc
                if state == 'connected' and current:
                    self.route = self._route(pc)
                self.journal.log('webrtc_state', session=session, state=state, **(self.route if current else {}))
                if not current:
                    return
                if state == 'connected':
                    self.connected = True
                    print(f'[webrtc] 浏览器已连接：{self.route.get("remote", "?")}，视频改走 WebRTC', flush=True)
                    self._report_route(self.route)
                    if self.channel is not None and self.channel.readyState == 'open':
                        self.channel.send(json.dumps(dict(type='route', **self.route)))
                    for stream in range(count):
                        self.request_keyframe(stream)
                elif state in ('failed', 'closed'):
                    self.connected = False
                    self._report_route(None)
                    print(f'[webrtc] 连接{"失败" if state == "failed" else "关闭"}，视频恢复走 RTP', flush=True)
                    if state == 'failed':
                        asyncio.ensure_future(self._close(pc))

            await pc.setRemoteDescription(RTCSessionDescription(sdp=offer, type='offer'))
            await pc.setLocalDescription(await pc.createAnswer())
            transceivers = pc.getTransceivers()
            codecs = [t._codecs[0].mimeType + ' ' + t._codecs[0].parameters.get('profile-level-id', '')
                      for t in transceivers if t._codecs]
            playout = all(any(x.uri == PLAYOUT_DELAY_URI for x in t._headerExtensions) for t in transceivers)
            self.journal.log('webrtc_answer', session=session, codecs=codecs, playout_delay_zero=playout,
                             streams=count, answer_ms=(time.perf_counter() - started) * 1000, **candidates)
            print(f'[webrtc] 已应答浏览器：{", ".join(codecs)}；playout-delay=0：{"是" if playout else "否"}', flush=True)
            if count < self.streams:
                print(f'[webrtc] 接收页只要 {count} 路，发送端有 {self.streams} 路：多出的路仍走 RTP（接收端丢弃）', flush=True)
            return datagrams('webrtc_answer', session, pc.localDescription.sdp)
        except Exception as exc:
            message = f'{type(exc).__name__}: {exc}'
            self.journal.log('webrtc_error', session=session, error=message)
            print(f'[webrtc] 无法应答浏览器：{message}', flush=True)
            if pc is not None:
                await self._close(pc)
            return datagrams('webrtc_error', session, message[:2000])

    @staticmethod
    def _route(pc):
        """The nominated ICE pair; same_host means the page runs on the sender."""
        try:
            connection = pc.getTransceivers()[0].sender.transport.transport._connection
            pair = connection._nominated[1]
            local = {c.host for c in connection.local_candidates}
            return dict(local=f'{pair.local_addr[0]}:{pair.local_addr[1]}',
                        remote=f'{pair.remote_addr[0]}:{pair.remote_addr[1]}',
                        same_host=pair.remote_addr[0] in local)
        except (AttributeError, IndexError, KeyError, TypeError):
            return {}

    def _keyframe(self, stream):
        self.journal.log('webrtc_pli', stream=stream)
        self.request_keyframe(stream)

    def _report_route(self, route):
        """Tell the sender which host the video goes to (None: no remote viewer)."""
        host = None
        if route and not route.get('same_host') and route.get('remote'):
            host = route['remote'].rsplit(':', 1)[0]
        if host == self.route_host or self.on_route is None:
            return
        self.route_host = host
        try:
            self.on_route(host)
        except Exception as exc:
            self.journal.log('webrtc_error', error=f'route callback: {type(exc).__name__}: {exc}')

    def _feedback(self, packet):
        """Browser RTCP for our senders -> rate controller. Each sender named in
        a packet receives the same object; handle it once."""
        control = self.control
        if control is None or packet is self.last_rtcp:
            return
        self.last_rtcp = packet
        now = time.monotonic()
        if isinstance(packet, (RtcpRrPacket, RtcpSrPacket)):
            blocks = []
            for report in packet.reports:
                entry = self.ssrcs.get(report.ssrc)
                if entry is not None:
                    stream, sender = entry
                    blocks.append((stream, report.fraction_lost / 256, report.packets_lost,
                                   report.highest_sequence, self._rtt(sender, report),
                                   getattr(sender, '_RTCRtpSender__packet_count', None)))
            if not blocks:
                return
            changed = control.reports(blocks, now)
            if now - self.logged_at >= FEEDBACK_LOG_S:
                self.logged_at = now
                rtt = control.latest_rtt()
                self.journal.log('webrtc_feedback', session=self.session, loss=control.loss,
                                 rtt_ms=rtt * 1000 if rtt is not None else None, queue_ms=control.queue,
                                 remb_kbps=control.remb / 1000 if control.remb else None,
                                 target_kbps=control.total_bps / 1000,
                                 **{f'rtx_{k}': v for k, v in (self.rtx or {}).items()})
        elif isinstance(packet, RtcpPsfbPacket) and packet.fmt == RTCP_PSFB_APP:
            try:
                bitrate, ssrcs = unpack_remb_fci(packet.fci)
            except ValueError:
                return
            if not any(ssrc in self.ssrcs for ssrc in ssrcs):
                return
            while self.sent_log and now - self.sent_log[0][0] > SENT_WINDOW_S:
                self.sent_log.popleft()
            changed = control.remb_report(bitrate, now, sum(b for _, b in self.sent_log) * 8 / SENT_WINDOW_S)
        else:
            return
        if changed:
            bps, fps = control.target
            self.rates = [control.target] * len(self.ssrcs)
            self.journal.log('webrtc_rate', session=self.session, stream_kbps=bps / 1000, fps=fps,
                             total_kbps=control.total_bps / 1000, reason=control.reason, loss=control.loss,
                             queue_ms=control.queue, remb_kbps=control.remb / 1000 if control.remb else None)
            why = (f'丢包 {control.loss:.0%}' if control.reason == 'loss'
                   else f'排队 {control.queue:.0f} ms' if control.reason == 'queue'
                   else {'remb': '浏览器带宽估计下降', 'probe': '网络恢复'}.get(control.reason, control.reason))
            print(f'[webrtc] 码率 {bps / 1e6:.2f} Mbps/路 · {fps} fps（{why}）', flush=True)

    def _rtx_allowed(self, stream, sender, seq):
        """Retransmit a NACKed packet? See MAX_RTX_AGE_S .. RTX_SHARE."""
        history = getattr(sender, '_RTCRtpSender__rtp_history', None)
        packet = history.get(seq % RTP_HISTORY) if history is not None else None
        if packet is None or packet.sequence_number != seq:
            return False  # no longer kept: aiortc would skip it too
        # RTP timestamps are the capture clock (origin 0), so this is the
        # frame's age; mod 2**32 for the 13 h wrap.
        if (pts90k(time.perf_counter_ns()) - packet.timestamp) % 2**32 > MAX_RTX_AGE_S * CLOCK:
            self.rtx['old'] += 1
            return False
        now = time.monotonic()
        key = (stream, seq, packet.timestamp)  # sequence numbers wrap every 65536 packets
        tries, last = self.rtx_tries.get(key, (0, None))
        rtt = self.control.latest_rtt() if self.control is not None else None
        if tries >= RTX_TRIES or (last is not None and now - last < max(RTX_MIN_GAP_S, rtt or 0)):
            self.rtx['repeat'] += 1
            return False
        target = (self.rates[stream][0] if self.rates and stream < len(self.rates) else self.bitrate)
        if target:
            rate = RTX_SHARE * target / 8  # bytes per second
            tokens, filled = self.rtx_tokens.get(stream, (rate * .25, now))
            tokens = min(rate * .25, tokens + (now - filled) * rate)
            size = len(packet.payload) + 60  # + RTP/RTX/SRTP/UDP/IP headers
            if tokens < size:
                self.rtx_tokens[stream] = (tokens, now)
                self.rtx['budget'] += 1
                return False
            self.rtx_tokens[stream] = (tokens - size, now)
        self.rtx_tries[key] = (tries + 1, now)
        self.rtx_tries.move_to_end(key)
        while len(self.rtx_tries) > 2 * RTP_HISTORY:
            self.rtx_tries.popitem(last=False)
        self.rtx['sent'] += 1
        return True

    def _rtt(self, sender, report):
        """Round trip from a receiver report (RFC 3550 LSR/DLSR) against the
        wall time we sent that sender report."""
        if not report.lsr:
            return None
        sent = self.sr_sent.get(sender._ssrc, {}).get(report.lsr)
        if sent is None:  # sent before our wrapper saw it: aiortc's own last one
            if report.lsr != getattr(sender, '_RTCRtpSender__lsr', None):
                return None
            sent = getattr(sender, '_RTCRtpSender__lsr_time', None)
            if sent is None:
                return None
        return time.time() - sent - report.dlsr / 65536

    def _message(self, channel, message):
        received = time.perf_counter_ns() / 1e6
        try:
            msg = json.loads(message)
        except (TypeError, ValueError):
            return
        if isinstance(msg, dict) and msg.get('type') == 'ping' and isinstance(msg.get('t1'), (int, float)):
            channel.send(json.dumps(dict(type='pong', stream_names=self.stream_names,
                                         t1=msg['t1'], t2=received,
                                         t3=time.perf_counter_ns() / 1e6), ensure_ascii=False))

    def _push(self, stream, bitstream, meta, pushed, rate=None):
        if not self.connected or not self.tracks or stream >= len(self.tracks):
            return  # the session ended after push(): this frame is lost
        track = self.tracks[stream]
        if track.queue.qsize() >= MAX_QUEUED_FRAMES:
            # aiortc fell behind: drop the backlog and resume at the next IDR.
            while not track.queue.empty():
                track.queue.get_nowait()
            track.awaiting_key = True
            self.journal.log('webrtc_backlog', stream=stream)
        if track.awaiting_key and not meta.key:
            self.request_keyframe(stream)
            return
        track.awaiting_key = False
        pts = pts90k(meta.capture_ns)
        packet = av.Packet(bitstream)
        packet.pts, packet.time_base = pts, TIME_BASE
        track.queue.put_nowait((packet, meta.frame_id, pushed))
        self.sent_log.append((time.monotonic(), len(bitstream)))
        while self.sent_log and self.sent_log[-1][0] - self.sent_log[0][0] > SENT_WINDOW_S:
            self.sent_log.popleft()
        # Logged here, not in encode(): frames dropped above were never sent.
        self.journal.log('tx', meta, transport='webrtc', wire_bytes=meta.frame_bytes,
                         tx_done_ns=time.perf_counter_ns())
        if self.channel is not None and self.channel.readyState == 'open':
            self.channel.send(json.dumps(frame_info(meta, pts, rate), separators=(',', ':')))

    async def _close(self, pc=None):
        """Close the current connection (or only `pc`, if it is still current)."""
        if pc is not None and pc is not self.pc:
            return
        pc, self.pc, self.tracks, self.channel, self.session = self.pc, None, None, None, None
        self.connected = False
        self.control = self.rates = self.last_rtcp = None
        self.rtx = self.rtx_tries = self.rtx_tokens = None
        self.ssrcs, self.sr_sent = {}, {}
        self.sent_log.clear()
        self._report_route(None)
        if pc is not None:
            await pc.close()
