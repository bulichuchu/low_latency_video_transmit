from __future__ import annotations

from collections import deque, OrderedDict
from dataclasses import asdict
import json
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

from .media import LatestSlot, Synthetic, encoder_candidates, make_encoder, open_camera, prepare_frame
from .metrics import Journal
from .protocol import Meta, Packetizer, parse_pli, parse_nack
from .network import is_rtsp, network_frames, redact
from .sdk import is_sdk, parse_device


def run_sender(args):
    if args.source == 'camera':
        from .cameras import configure_camera_inputs
        configure_camera_inputs(args)
    stop = threading.Event()
    errors = queue.Queue()
    epoch = secrets.randbits(32)
    peer = (socket.gethostbyname(args.host), args.port)
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

    def guarded(fn, *items):
        try:
            fn(*items)
        except Exception:
            error = redact(traceback.format_exc())
            errors.put(error)
            journal.log('error', message=error)
            stop.set()

    def generate():
        sources = [Synthetic(args.width, args.height, i) for i in range(args.streams)]
        sequence, tick = 0, time.perf_counter()
        while not stop.is_set():
            remaining = tick - time.perf_counter()
            if remaining > 0 and stop.wait(remaining):
                break
            capture_ns = time.perf_counter_ns()
            elapsed = time.perf_counter() - started
            for i, source in enumerate(sources):
                image = source.frame(sequence, elapsed)
                slots[i].put((capture_ns, image))
                source_counts[i] += 1
            sequence += 1
            tick += 1 / args.fps
            if tick < time.perf_counter() - 1 / args.fps:
                tick = time.perf_counter() + 1 / args.fps

    def capture(stream, settings):
        # Host dequeue timestamp: expressly not a sensor exposure timestamp.
        last_sample = time.perf_counter_ns()
        last_count = 0

        def accept_frame(frame, origin, capture_ns=None, source_meta=None):
            nonlocal last_sample, last_count
            ts = capture_ns if capture_ns is not None else time.perf_counter_ns()
            source_meta = source_meta or {}
            if source_counts[stream] == 0:
                journal.log('camera_opened', stream=stream, device=settings['device'],
                    actual_width=frame.width, actual_height=frame.height,
                    actual_pixel_format=frame.format.name, colorspace=frame.colorspace,
                    color_range=frame.color_range, timestamp_origin=origin, requested=settings, **source_meta)
                print(f'[camera {stream}] {redact(settings["device"])}: {frame.width}x{frame.height} '
                      f'{frame.format.name}; timestamp={origin}', flush=True)
            if settings.get('colorspace') in ('bt709', 'bt601'):
                frame.colorspace = 1 if settings['colorspace'] == 'bt709' else 5
            pts_value = frame.pts
            pts_base = str(frame.time_base) if frame.time_base else None
            slots[stream].put((ts, frame))
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

        with open_camera(settings, args.width, args.height, args.fps) as camera:
            # Live avfoundation inputs can transiently return EAGAIN while the
            # device has no decoded frame ready. Re-enter decode after a short
            # wait instead of treating that normal condition as a fatal error.
            while not stop.is_set():
                try:
                    for frame in camera.decode(video=0):
                        if stop.is_set():
                            break
                        accept_frame(frame, 'host_dequeue')
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
        depth_preview = (args.source == 'camera' and is_sdk(args.camera_settings[stream]['device']) and
                         parse_device(args.camera_settings[stream]['device'])[1] == 'depth')
        limit_capture_rate = (args.source == 'camera' and
                              (is_rtsp(args.camera_settings[stream]['device']) or
                               float(args.camera_settings[stream].get('fps', args.fps)) > args.fps))
        while not stop.is_set():
            if limit_capture_rate:
                if stop.wait(max(0, next_capture_submit - time.perf_counter())):
                    break
            item = slots[stream].take(stop)
            if item is None:
                continue
            capture_ns, image = item
            next_capture_submit = max(next_capture_submit + 1 / args.fps, time.perf_counter())
            encode_in_ns = time.perf_counter_ns()
            frame = prepare_frame(image, pts, encoder, reformatter)
            converted_ns = time.perf_counter_ns()
            if pts == 0 or (requests[stream].is_set() and time.perf_counter() - last_key_request > .1):
                frame.pict_type = av.video.frame.PictureType.I
                requests[stream].clear()
                last_key_request = time.perf_counter()
                journal.log('keyframe_requested', stream=stream)
            pending[pts] = (capture_ns, encode_in_ns, converted_ns)
            for packet in encoder.encode(frame):
                if packet.pts not in pending:
                    raise RuntimeError('Encoder did not preserve PTS')
                original_ns, encode_in, converted = pending.pop(packet.pts)
                encoded_ns = time.perf_counter_ns()
                encode_us = (encoded_ns - encode_in) // 1000
                meta = Meta(stream, epoch, packet.pts, original_ns, encode_us, depth_preview=depth_preview)
                journal.log('encode', meta, raw_queue_ms=(encode_in - original_ns) / 1e6,
                            prepare_ms=(converted - encode_in) / 1e6,
                            codec_encode_ms=(encoded_ns - converted) / 1e6)
                packets = packetizers[stream].packetize(bytes(packet), meta)
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
            pts += 1
            if len(pending) > 16:
                raise RuntimeError('Encoder buffered over 16 frames; use --encoder libx264')
        journal.log('sender_totals', stream=stream, captured=source_counts[stream],
                    raw_overwritten=slots[stream].replaced, encoder_pending_at_stop=len(pending))

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
                sock.sendto(data, peer)
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
            try:
                msg = json.loads(data)
                if (isinstance(msg, dict) and msg.get('v') == 1 and msg.get('type') == 'clock_ping'
                        and isinstance(msg.get('t1'), int)):
                    reply = dict(v=1, type='clock_pong', t1=msg['t1'], t2=received,
                                 t3=time.perf_counter_ns())
                    sock.sendto(json.dumps(reply).encode(), addr)
            except (ValueError, TypeError):
                continue

    jobs = [(encode, (i,)) for i in range(args.streams)] + [(transmit, ()), (controls, ())]
    if args.source == 'synthetic':
        jobs.append((generate, ()))
    else:
        jobs.extend((capture, (i, settings)) for i, settings in enumerate(args.camera_settings))
    threads = [threading.Thread(target=guarded, args=(fn, *items), daemon=True) for fn, items in jobs]
    for thread in threads:
        thread.start()
    print(f'[sender] {args.streams} streams -> {peer[0]}:{peer[1]}, source={args.source}', flush=True)
    journal.log('start')
    try:
        while not stop.wait(.1):
            if (Path(args.output) / 'STOP').exists():
                break
            if journal.error:
                raise RuntimeError(journal.error)
            if args.duration and time.perf_counter() - started >= args.duration:
                break
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
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
