from __future__ import annotations

from collections import deque, OrderedDict
import ipaddress
import json
import os
from pathlib import Path
import queue
import random
import secrets
import socket
import struct
import threading
import time
import traceback

import av

from . import webrtc_signal
from .capture_time import HostPtsClock
from .congestion import rate_switch_due
from .media import LatestSlot, encoder_candidates, make_encoder, open_camera, prepare_frame
from .metrics import Journal
from .protocol import Meta, Packetizer, parse_pli, parse_nack
from .network import camera_display_names, is_rtsp, network_frames, redact
from .sdk import is_sdk, parse_device

CPU_SAMPLE_S = 2


def mark_sending(host_ip):
    """Tell tools/awdl_guard.sh that this process is sending to another computer.

    The guard (macOS) exports VIDEO_DEMO_SENDING_DIR and keeps AWDL off while a
    marker there names a live PID. Without it, or for a loopback peer, nothing
    is written. Returns the marker to remove when sending stops.
    """
    directory = os.environ.get('VIDEO_DEMO_SENDING_DIR')
    address = ipaddress.ip_address(host_ip)
    if not directory or address.is_loopback or address.is_unspecified:
        return None
    marker = Path(directory) / str(os.getpid())
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f'{host_ip}\n')
    except OSError as exc:
        print(f'[awdl] 无法写入发送标记，AWDL 保持不变：{exc}', flush=True)
        return None
    return marker


def _remote(host_ip):
    try:
        address = ipaddress.ip_address(host_ip)
    except ValueError:
        return False
    return not (address.is_loopback or address.is_unspecified)


class SendingMarker:
    """This process's marker for the AWDL guard (see mark_sending), kept while
    any video goes to another computer: RTP to the receiver, or WebRTC straight
    to the browser. The latter can be remote while RTP targets a receiver
    service on this computer, as in the 10-09 phone and VPN runs."""

    def __init__(self):
        self.lock = threading.Lock()
        self.hosts = {}  # 'rtp' / 'webrtc' -> destination IP
        self.marker = None
        self.closed = False

    def update(self, path, host_ip):
        with self.lock:
            if self.closed:
                return
            if host_ip:
                self.hosts[path] = host_ip
            else:
                self.hosts.pop(path, None)
            remote = next((h for h in self.hosts.values() if _remote(h)), None)
            if remote and self.marker is None:
                self.marker = mark_sending(remote)
            elif not remote and self.marker is not None:
                self.marker.unlink(missing_ok=True)
                self.marker = None

    def close(self):
        with self.lock:
            self.closed = True
            if self.marker is not None:
                self.marker.unlink(missing_ok=True)
                self.marker = None


def run_sender(args):
    args.host = args.host.strip()
    try:
        peer = (socket.gethostbyname(args.host), args.port)
    except socket.gaierror as exc:
        raise ValueError(f'无法解析接收机地址「{args.host}」；请填写有效的 IP 或域名，'
                         '不要包含 http://、路径或端口，并检查本机 DNS。') from exc
    from .cameras import configure_camera_inputs
    configure_camera_inputs(args)
    stop = threading.Event()
    errors = queue.Queue()
    epoch = secrets.randbits(32)
    stream_names = camera_display_names(args.camera_settings)
    journal = Journal(args.output, vars(args))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 256 * 1024)
    sock.bind(('0.0.0.0', 0))
    sock.settimeout(.1)
    slots = [LatestSlot() for _ in range(args.streams)]
    packetizers = [Packetizer(mtu=args.mtu) for _ in slots]
    requests = [threading.Event() for _ in slots]
    outbound = queue.Queue(maxsize=args.streams)
    source_counts = [0] * args.streams
    enc_names = [None] * args.streams
    started = time.perf_counter()
    retransmit_cache = OrderedDict()
    cache_lock = threading.Lock()
    rng = random.Random(args.seed)
    # Created on the first WebRTC offer, so RTP-only runs never import aiortc.
    webrtc = [None]
    webrtc_unavailable = []
    sending = SendingMarker()

    def webrtc_signal_received(msg):
        if webrtc[0] is None and not webrtc_unavailable:
            try:
                from .webrtc_sender import WebRTCPublisher
                webrtc[0] = WebRTCPublisher(args.streams, send=lambda data: sock.sendto(data, peer),
                                            request_keyframe=lambda stream: requests[stream].set(),
                                            journal=journal, mtu=args.mtu, stream_names=stream_names,
                                            bitrate=args.bitrate_kbps * 1000, fps=args.fps,
                                            on_route=lambda host: sending.update('webrtc', host))
            except ImportError as exc:
                webrtc_unavailable.append(f'发送端未安装 WebRTC 组件 aiortc（{exc}）。请用 start_sender.command '
                                          '重启发送端，或按 README 安装 requirements-webrtc.txt。')
            except Exception as exc:  # e.g. aiortc vs a newer PyAV: RTP must keep running
                webrtc_unavailable.append(f'发送端无法启用 WebRTC：{type(exc).__name__}: {exc}')
            if webrtc_unavailable:
                journal.log('webrtc_error', error=webrtc_unavailable[0])
                print(f'[webrtc] {webrtc_unavailable[0]}', flush=True)
        if webrtc[0] is not None:
            webrtc[0].handle(msg)
        elif msg['type'] == 'webrtc_offer' and msg['i'] == 0:
            for data in webrtc_signal.datagrams('webrtc_error', msg['id'], webrtc_unavailable[0][:2000]):
                sock.sendto(data, peer)

    def guarded(fn, *items):
        try:
            fn(*items)
        except Exception:
            error = redact(traceback.format_exc())
            errors.put(error)
            journal.log('error', message=error)
            stop.set()

    def capture(stream, settings):
        # Host dequeue timestamp: expressly not a sensor exposure timestamp.
        last_sample = time.perf_counter_ns()
        last_count = 0
        timestamp_status = None

        def accept_frame(frame, origin, capture_ns=None, source_meta=None):
            nonlocal last_sample, last_count, timestamp_status
            ts = capture_ns if capture_ns is not None else time.perf_counter_ns()
            source_meta = source_meta or {}
            if source_counts[stream] == 0:
                journal.log('camera_opened', stream=stream, device=settings['device'],
                    actual_width=frame.width, actual_height=frame.height,
                    actual_pixel_format=frame.format.name, colorspace=frame.colorspace,
                    color_range=frame.color_range, timestamp_origin=origin, requested=settings, **source_meta)
                camera_clock = source_meta.get('camera_timestamp_source')
                print(f'[camera {stream}] {redact(settings["device"])}: {frame.width}x{frame.height} '
                      f'{frame.format.name}; timestamp={origin}'
                      + (f', camera_timestamp={camera_clock}' if camera_clock else ''), flush=True)
            if settings.get('colorspace') in ('bt709', 'bt601'):
                frame.colorspace = 1 if settings['colorspace'] == 'bt709' else 5
            pts_value = frame.pts
            pts_base = str(frame.time_base) if frame.time_base else None
            timing = {k: source_meta[k] for k in ('sensor_capture_ns', 'sdk_device_timestamp_us',
                'sdk_global_timestamp_us', 'sensor_status', 'sensor_clock_uncertainty_us') if k in source_meta}
            if timing.get('sensor_status') != timestamp_status:
                timestamp_status = timing.get('sensor_status')
                journal.log('sdk_timestamp_state', stream=stream, **timing,
                            camera_timestamp_source=source_meta.get('camera_timestamp_source'),
                            detail=source_meta.get('sdk_timestamp_error'))
            slots[stream].put((ts, frame, timing))
            source_counts[stream] += 1
            if ts - last_sample >= 1_000_000_000:
                journal.log('camera_sample', stream=stream,
                    capture_fps=(source_counts[stream] - last_count) * 1e9 / (ts - last_sample),
                    captured=source_counts[stream], raw_overwritten=slots[stream].replaced,
                    device_pts=pts_value, device_time_base=pts_base, **source_meta)
                last_sample, last_count = ts, source_counts[stream]

        if is_sdk(settings['device']):
            from .sdk import frames
            for frame, capture_ns, metadata in frames(settings, stop):
                accept_frame(frame, 'sdk_host_dequeue', capture_ns, metadata)
            return

        if is_rtsp(settings['device']):
            def network_event(kind, **fields):
                journal.log(kind, stream=stream, device=settings['device'], **fields)
                if kind == 'network_connected':
                    requests[stream].set()  # Resume with an IDR after a network outage.
                    print(f'[camera {stream}] 网络流已连接：{redact(settings["device"])} '
                          f'{fields["input_codec"]} {fields["actual_width"]}x{fields["actual_height"]}', flush=True)
                elif kind == 'network_disconnected':
                    print(f'[camera {stream}] 网络流断开/连接失败，{fields["retry_delay_s"]}s 后重试；'
                          f'{fields["error_type"]} ({fields["error_code"]})；请检查地址、网络和账号。', flush=True)
            for frame in network_frames(settings, stop, network_event):
                accept_frame(frame, 'network_decode')
            return

        from .cameras import capture_options
        # AVFoundation/V4L2 frame PTS are camera/driver times on this host's
        # monotonic clock; report them as the camera timestamp when plausible.
        device_clock = HostPtsClock(capture_options(settings, args.width, args.height, args.fps)[0])
        with open_camera(settings, args.width, args.height, args.fps) as camera:
            # Live avfoundation inputs can transiently return EAGAIN while the
            # device has no decoded frame ready. Re-enter decode after a short
            # wait instead of treating that normal condition as a fatal error.
            while not stop.is_set():
                try:
                    for frame in camera.decode(video=0):
                        if stop.is_set():
                            break
                        dequeued = time.perf_counter_ns()
                        accept_frame(frame, 'host_dequeue', dequeued, device_clock.fields(frame, dequeued))
                except av.error.BlockingIOError:
                    stop.wait(.001)

    def encode(stream):
        from av.video.reformatter import VideoReformatter
        reformatter = VideoReformatter()
        failures = []
        encoder = None
        for name in encoder_candidates(args.encoder):
            try:
                encoder = make_encoder(name, args.width, args.height, args.fps, args.bitrate_kbps * 1000)
                break
            except Exception as exc:
                failures.append(f'{name}: {exc}')
        if encoder is None:
            raise RuntimeError('Cannot open encoder: ' + '; '.join(failures))
        enc_names[stream] = name
        journal.log('encoder', stream=stream, name=name, fallbacks=failures)
        print(f'[sender] stream={stream} encoder={name}', flush=True)
        pending = {}
        pts = 0
        last_key_request = 0
        next_capture_submit = 0
        depth_preview = (is_sdk(args.camera_settings[stream]['device']) and
                         parse_device(args.camera_settings[stream]['device'])[1] == 'depth')
        limit_capture_rate = (is_rtsp(args.camera_settings[stream]['device']) or
                              float(args.camera_settings[stream].get('fps', args.fps)) > args.fps)
        # (bps, fps) the encoder runs at: the configured rate, or what the
        # WebRTC session's rate control asks for while a browser is connected.
        configured = current = (args.bitrate_kbps * 1000, args.fps)
        switched = time.perf_counter()
        retry_at = 0
        last_kept = None
        rate_skipped = 0

        def emit(packet, rate):
            if packet.pts not in pending:
                raise RuntimeError('Encoder did not preserve PTS')
            original_ns, encode_in, converted, source_timing = pending.pop(packet.pts)
            encoded_ns = time.perf_counter_ns()
            # Sender's own share of the chain: app capture -> encoded (slot
            # wait, conversion, codec), shown as one step by the receiver.
            encode_us = (encoded_ns - original_ns) // 1000
            meta = Meta(stream, epoch, packet.pts, original_ns, encode_us,
                        depth_preview=depth_preview, **source_timing)
            journal.log('encode', meta, raw_queue_ms=(encode_in - original_ns) / 1e6,
                        prepare_ms=(converted - encode_in) / 1e6,
                        codec_encode_ms=(encoded_ns - converted) / 1e6)
            # A connected WebRTC viewer takes the frame instead of RTP (it
            # logs tx once the frame is actually queued for sending).
            publisher = webrtc[0]
            if publisher is not None and publisher.push(stream, bytes(packet), meta, rate=rate) is not None:
                return
            packets = packetizers[stream].packetize(bytes(packet), meta) # H264->bytes->MTU split
            # Read back the normalized size/IDR flag generated by packetization.
            from .protocol import parse_packet
            meta = parse_packet(packets[0]).meta
            job = [packets, 0, meta, 0, 0]
            while not stop.is_set():
                try:
                    outbound.put(job, timeout=.1)
                    break
                except queue.Full:
                    pass

        while not stop.is_set():
            if limit_capture_rate:
                if stop.wait(max(0, next_capture_submit - time.perf_counter())):
                    break
            item = slots[stream].take(stop)
            if item is None:
                continue
            capture_ns, image, timing = item
            next_capture_submit = max(next_capture_submit + 1 / args.fps, time.perf_counter())
            publisher = webrtc[0]
            wanted = (publisher.rate(stream) if publisher is not None else None) or configured
            now = time.perf_counter()
            if now >= retry_at and rate_switch_due(current, wanted, now - switched, configured):
                # A new encoder at the new bitrate/frame rate; it starts with an
                # IDR. Only swap once it opened, then drain the old one.
                try:
                    replacement = make_encoder(name, args.width, args.height, wanted[1], wanted[0])
                except Exception as exc:
                    journal.log('encoder_rate_error', stream=stream, bitrate_kbps=wanted[0] / 1000,
                                fps=wanted[1], error=f'{type(exc).__name__}: {exc}')
                    retry_at = now + 5  # keep the running encoder; try again later
                else:
                    for packet in encoder.encode(None):
                        emit(packet, current)
                    encoder, current, last_kept = replacement, wanted, None
                    requests[stream].clear()  # the new encoder's first frame is an IDR anyway
                    journal.log('encoder_rate', stream=stream, bitrate_kbps=wanted[0] / 1000, fps=wanted[1],
                                reopen_ms=(time.perf_counter() - now) * 1000)
                switched = time.perf_counter()
            # Below the configured frame rate, skip frames by capture time
            # (10 % slack for capture jitter: 60 -> 30 keeps every other one).
            if current[1] < args.fps and last_kept is not None and capture_ns - last_kept < .9e9 / current[1]:
                rate_skipped += 1
                continue
            last_kept = capture_ns
            encode_in_ns = time.perf_counter_ns()
            frame = prepare_frame(image, pts, encoder, reformatter)
            converted_ns = time.perf_counter_ns()
            if pts == 0 or (requests[stream].is_set() and time.perf_counter() - last_key_request > .1):
                frame.pict_type = av.video.frame.PictureType.I  # Instantaneous Decoder Refresh
                requests[stream].clear()
                last_key_request = time.perf_counter()
                journal.log('keyframe_requested', stream=stream)
            pending[pts] = (capture_ns, encode_in_ns, converted_ns, timing)
            for packet in encoder.encode(frame):
                emit(packet, current)
            pts += 1
            if len(pending) > 16:
                raise RuntimeError('Encoder buffered over 16 frames; use --encoder libx264')
        journal.log('sender_totals', stream=stream, captured=source_counts[stream],
                    raw_overwritten=slots[stream].replaced, encoder_pending_at_stop=len(pending),
                    rate_skipped=rate_skipped)

    def transmit():
        active = deque()
        next_send = time.perf_counter()
        rate = args.link_mbps * 1e6 / 8
        while not stop.is_set():
            while len(active) < args.streams * 2:
                try:
                    active.append(outbound.get(timeout=.02 if not active else 0))
                except queue.Empty:
                    break
            if not active:
                continue
            job = active.popleft()
            packets, index, meta, sent_bytes, injected = job
            data = packets[index]
            if rate:
                remaining = next_send - time.perf_counter()
                if remaining > 0 and stop.wait(remaining):
                    break
                next_send = max(next_send, time.perf_counter()) + len(data) / rate
            sequence = struct.unpack_from('!H', data, 2)[0]
            with cache_lock:
                retransmit_cache[(packetizers[meta.stream].ssrc, sequence)] = (time.perf_counter_ns(), data, meta)
                while len(retransmit_cache) > 4096:
                    retransmit_cache.popitem(last=False)
            if rng.random() < args.loss:
                injected += 1
            else:
                sock.sendto(data, peer) #UDP send
                sent_bytes += len(data)
            index += 1
            if index < len(packets):
                active.append([packets, index, meta, sent_bytes, injected])
            else:
                journal.log('tx', meta, wire_bytes=sent_bytes,
                            injected_packet_drops=injected, packet_count=len(packets),
                            tx_done_ns=time.perf_counter_ns())

    def controls():
        while not stop.is_set():
            try:
                data, addr = sock.recvfrom(8192)
            except socket.timeout:
                continue
            if addr != peer:
                continue
            received = time.perf_counter_ns()
            missing = parse_nack(data)
            if missing is not None:
                media_ssrc, sequences = missing
                for sequence in sequences:
                    with cache_lock:
                        cached = retransmit_cache.get((media_ssrc, sequence))
                    if cached is None:
                        continue
                    original_time, packet, meta = cached
                    # No unbounded retries or stale retransmissions. Loss injection applies to RTX too.
                    if received - original_time > 30_000_000 or received - meta.capture_ns > 75_000_000:
                        continue
                    lost = rng.random() < args.loss
                    if not lost:
                        sock.sendto(packet, peer)
                    journal.log('rtx', meta, wire_bytes=0 if lost else len(packet), injected_packet_drops=int(lost))
                journal.log('rtcp_nack', media_ssrc=media_ssrc, requested=len(sequences))
                continue
            target = parse_pli(data)
            if target is not None:
                for stream, p in enumerate(packetizers):
                    if p.ssrc == target:
                        requests[stream].set()
                        journal.log('rtcp_pli', stream=stream)
                continue
            if len(data) == 32 and data[:4] == b'\x81\xc9\x00\x07':
                ssrc = struct.unpack_from('!I', data, 8)[0]
                fraction = data[12] / 256
                lost = int.from_bytes(data[13:16], 'big', signed=True)
                journal.log('rtcp_rr', media_ssrc=ssrc, fraction_lost=fraction, cumulative_lost=lost)
                continue
            signal = webrtc_signal.parse(data)
            if signal is not None:
                webrtc_signal_received(signal)
                continue
            try:
                msg = json.loads(data)
                if (isinstance(msg, dict) and msg.get('v') == 1 and msg.get('type') == 'clock_ping'
                        and isinstance(msg.get('t1'), int)):
                    reply = dict(v=1, type='clock_pong', epoch=epoch, stream_names=stream_names,
                                 t1=msg['t1'], t2=received,
                                 t3=time.perf_counter_ns())
                    sock.sendto(json.dumps(reply, ensure_ascii=False).encode(), addr)
            except (ValueError, TypeError):
                continue

    jobs = [(encode, (i,)) for i in range(args.streams)] + [(transmit, ()), (controls, ())]
    jobs.extend((capture, (i, settings)) for i, settings in enumerate(args.camera_settings))
    threads = [threading.Thread(target=guarded, args=(fn, *items), daemon=True) for fn, items in jobs]
    sending.update('rtp', peer[0])
    for thread in threads:
        thread.start()
    print(f'[sender] {args.streams} camera streams -> {peer[0]}:{peer[1]}', flush=True)
    journal.log('start')
    cpu_sample = (time.process_time(), time.perf_counter())
    try:
        while not stop.wait(.1):
            if (Path(args.output) / 'STOP').exists():
                break
            if journal.error:
                raise RuntimeError(journal.error)
            now = time.perf_counter()
            if args.duration and now - started >= args.duration:
                break
            if now - cpu_sample[1] >= CPU_SAMPLE_S:
                # All threads of this process (capture, encode, RTP or aiortc);
                # 100 = one core. The SDK helper is a separate process.
                cpu = time.process_time()
                journal.log('process_cpu', percent=100 * (cpu - cpu_sample[0]) / (now - cpu_sample[1]),
                            transport='webrtc' if webrtc[0] is not None and webrtc[0].connected else 'rtp')
                cpu_sample = (cpu, now)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        sending.close()
        if webrtc[0] is not None:
            webrtc[0].stop()
        network_timeout = max((max(s.get('open_timeout_s', 3), s.get('read_timeout_s', 2))
                               for s in getattr(args, 'camera_settings', []) if is_rtsp(s['device'])), default=0)
        join_deadline = time.perf_counter() + network_timeout + 2
        for thread in threads:
            thread.join(timeout=max(.1, join_deadline - time.perf_counter()) if network_timeout else 1)
        sock.close()
        journal.log('stop')
        summary = journal.close()
        print(f'[sender] report: {args.output}/summary.json', flush=True)
    if not errors.empty():
        raise RuntimeError(errors.get())
    return summary
