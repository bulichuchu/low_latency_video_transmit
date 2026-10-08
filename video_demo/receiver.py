from __future__ import annotations

from fractions import Fraction
import json
from pathlib import Path
import queue
import secrets
import socket
import threading
import time
import traceback

import av

from .media import make_decoder
from .capture_time import sensor_latency_fields
from .metrics import Journal, LiveStats
from .protocol import Assembler, ReceptionReport, parse_packet, pli, nack
from .timing import ClockMap, Decoded, FrameMatcher


def run_receiver(args, web_sink=None):
    # Python only receives/decodes; live presentation belongs to the browser.
    args.headless = True
    stop = threading.Event()
    errors = queue.Queue()
    journal = Journal(args.output, vars(args))
    stats = LiveStats(args.streams)
    matcher = FrameMatcher(args.streams, args.sync_tolerance_ms, args.sync_wait_ms, args.max_age_ms,
                           args.strict_sync, args.sync_mode)
    clock = ClockMap(shared=args.clock_mode == 'shared')
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 512 * 1024)
    try:
        sock.bind((args.bind, args.port))
    except OSError:
        sock.close()
        journal.close()
        raise
    sock.settimeout(.003)
    queues = [queue.Queue(maxsize=8) for _ in range(args.streams)]
    assemblers = [Assembler(args.reorder_ms) for _ in queues]
    reports = [ReceptionReport() for _ in queues]
    ssrcs = [None] * args.streams
    epochs = [None] * args.streams
    retired = [set() for _ in queues]
    receiver_ssrc = secrets.randbits(32)
    peer = [None]
    last_packet = [0]
    last_pli = [0] * args.streams
    sent_pings = {}
    control_lock = threading.Lock()
    current_frames = {}
    group_info = {'complete': 0, 'total': 0, 'skew': None}
    directory = Path(args.output)

    def request_key(stream):
        now = time.perf_counter_ns()
        with control_lock:
            if peer[0] and ssrcs[stream] is not None and now - last_pli[stream] > 150_000_000:
                sock.sendto(pli(receiver_ssrc, ssrcs[stream]), peer[0])
                last_pli[stream] = now
                journal.log('rtcp_pli_sent', stream=stream)

    def drop(meta, reason, count=1):
        stats.add(meta.stream, 'drops', count)
        journal.log('drop', meta, reason=reason, count=count)

    def guarded(fn, *items):
        try:
            fn(*items)
        except Exception:
            error = traceback.format_exc()
            errors.put(error)
            journal.log('error', message=error)
            stop.set()

    def receive():
        next_ping = next_report = time.perf_counter()
        while not stop.is_set():
            try:
                data, address = sock.recvfrom(65535)
            except socket.timeout:
                data, address = b'', None
            now = time.perf_counter_ns()
            if data and data.startswith(b'{'):
                try:
                    msg = json.loads(data)
                    if (address == peer[0] and isinstance(msg, dict) and msg.get('v') == 1
                            and msg.get('type') == 'clock_pong'
                            and isinstance(msg.get('t1'), int) and msg['t1'] in sent_pings):
                        t1 = msg['t1']
                        del sent_pings[t1]
                        clock.update(t1, msg.get('t2'), msg.get('t3'), now)
                except (ValueError, TypeError):
                    pass
            elif data:
                try:
                    packet = parse_packet(data)
                    stream = packet.meta.stream
                    if stream >= args.streams:
                        continue
                    if address != peer[0]:
                        if peer[0] and now - last_packet[0] < 2_000_000_000:
                            continue  # One sender process, with multiple streams, per receiver.
                        peer[0] = address
                        clock.clear()
                        sent_pings.clear()
                    last_packet[0] = now
                    identity = (packet.meta.epoch, packet.ssrc)
                    if identity in retired[stream]:
                        continue
                    if epochs[stream] != identity:
                        if epochs[stream] is not None:
                            retired[stream].add(epochs[stream])
                            if len(retired[stream]) > 16:
                                retired[stream].pop()
                        epochs[stream] = identity
                        assemblers[stream] = Assembler(args.reorder_ms)
                        reports[stream] = ReceptionReport()
                        ssrcs[stream] = packet.ssrc
                        journal.log('stream_start', packet.meta, ssrc=packet.ssrc)
                    stats.add(stream, 'wire_bytes', len(data))
                    reports[stream].observe(packet, now)
                    unit = assemblers[stream].add(packet, now)
                    if unit is not None:
                        try:
                            queues[stream].put_nowait(unit)
                        except queue.Full:
                            drop(unit.meta, 'compressed_decode_queue_full')
                            request_key(stream)
                except ValueError as exc:
                    journal.log('malformed_packet', reason=str(exc))
            for stream, assembler in enumerate(assemblers):
                if peer[0]:
                    for packet, missing in assembler.missing(now):
                        sock.sendto(nack(receiver_ssrc, packet.ssrc, missing), peer[0])
                        journal.log('rtcp_nack_sent', packet.meta, packet_count=len(missing))
                for meta in assembler.expire(now):
                    drop(meta, 'incomplete_rtp_frame')
                    request_key(stream)
            current = time.perf_counter()
            if peer[0] and current >= next_ping:
                t1 = time.perf_counter_ns()
                sent_pings[t1] = t1
                sent_pings_copy = list(sent_pings)
                for old in sent_pings_copy:
                    if t1 - old > 2_000_000_000:
                        del sent_pings[old]
                sock.sendto(json.dumps(dict(v=1, type='clock_ping', t1=t1)).encode(), peer[0])
                next_ping = current + .2
            if current >= next_report:
                offset, uncertainty = clock.estimate(now)
                for stream, report in enumerate(reports):
                    if peer[0] and ssrcs[stream] is not None:
                        sock.sendto(report.packet(receiver_ssrc, ssrcs[stream]), peer[0])
                    s = stats.snapshot()[stream]
                    journal.log('sample', stream=stream, fps=s['submit_fps'] if web_sink else s['fps'], rtp_mbps=s['mbps'],
                                fps_measurement='browser_reported_submit' if web_sink else 'native_decode',
                                decoded=s['decoded'], drops=s['drops'], clock_offset_ns=offset,
                                clock_uncertainty_ms=uncertainty, wire_bytes=s['wire_bytes'])
                next_report = current + 1

    def decode(stream):
        from av.video.reformatter import VideoReformatter
        reformatter = VideoReformatter()
        decoder, decoder_name = make_decoder(args.decoder) if web_sink is None else (None, 'webcodecs')
        pending = {}
        origins = {}
        expected = 0
        epoch = None
        waiting_key = True

        def reset():
            nonlocal decoder, decoder_name
            if web_sink is None:
                decoder, decoder_name = make_decoder(args.decoder)
            origins.clear()

        while not stop.is_set():
            try:
                unit = queues[stream].get(timeout=0 if expected in pending else .002)
                if unit.meta.epoch != epoch:
                    pending.clear()
                    if epoch is not None:
                        reset()
                    epoch = unit.meta.epoch
                    expected = 0
                    waiting_key = True
                if unit.meta.frame_id >= expected:
                    pending[unit.meta.frame_id] = unit
                else:
                    drop(unit.meta, 'late_complete_frame')
            except queue.Empty:
                pass
            now = time.perf_counter_ns()
            if not pending:
                continue
            if expected not in pending:
                earliest = min(pending)
                keyframes = [k for k, u in pending.items() if u.meta.key]
                if keyframes:
                    jump = min(keyframes)
                elif now - pending[earliest].complete_ns < args.reorder_ms * 1e6 and len(pending) < 8:
                    continue
                else:
                    jump = earliest
                if jump > expected:
                    journal.log('reference_gap', stream=stream, first_missing=expected, next_available=jump)
                    waiting_key = True
                    request_key(stream)
                    reset()
                    for old in [k for k in pending if k < jump]:
                        drop(pending.pop(old).meta, 'superseded_before_idr')
                    expected = jump
            unit = pending.pop(expected)
            expected += 1
            if waiting_key and not unit.meta.key:
                drop(unit.meta, 'awaiting_idr')
                request_key(stream)
                continue
            if unit.meta.key:
                waiting_key = False
            if web_sink is not None:
                # Deliver ordered original access units; the browser owns decode,
                # frame matching and presentation. No Python RGB conversion.
                web_sink.offer(unit, clock)
                continue
            start = time.perf_counter_ns()
            try:
                packet = av.Packet(unit.bitstream)
                packet.pts = packet.dts = unit.meta.frame_id
                packet.time_base = Fraction(1, args.fps)
                origins[unit.meta.frame_id] = (unit, start)
                decoded_frames = decoder.decode(packet)
                codec_done = time.perf_counter_ns()
                for frame in decoded_frames:
                    if frame.pts not in origins:
                        raise RuntimeError('Decoder did not preserve frame PTS')
                    original, decode_start = origins.pop(frame.pts)
                    if frame.width > 4096 or frame.height > 2160:
                        raise RuntimeError('Frame dimensions exceed demo limit')
                    rgb_start = time.perf_counter_ns()
                    rgb = reformatter.reformat(frame, format='rgb24', src_colorspace='ITU709',
                                               threads=1).to_ndarray()
                    end = time.perf_counter_ns()
                    offset, uncertainty = clock.estimate(end)
                    local_capture = original.meta.capture_ns - offset if offset is not None else None
                    latency = (end - local_capture) / 1e6 if local_capture is not None else None
                    if latency is not None and latency < 0:
                        journal.log('clock_invalid_sample', original.meta, latency_ms=latency)
                        local_capture = latency = None
                    actual_decoder = decoder_name if decoder.is_hwaccel else 'software'
                    # Log ALL decoded frames, including late ones, before display filtering.
                    journal.log('decode', original.meta, latency_ms=latency,
                                clock_uncertainty_ms=uncertainty, decode_ms=(end - decode_start) / 1e6,
                                rx_first_ns=original.first_rx_ns, rx_complete_ns=original.complete_ns,
                                assembly_ms=(original.complete_ns - original.first_rx_ns) / 1e6,
                                decode_queue_ms=(decode_start - original.complete_ns) / 1e6,
                                codec_decode_ms=(codec_done - decode_start) / 1e6,
                                rgb_convert_ms=(end - rgb_start) / 1e6,
                                decoder=actual_decoder, **sensor_latency_fields(original.meta, clock, end))
                    stats.add(stream, 'decoded', latency_ms=latency, decoder=actual_decoder)
                    if latency is not None and latency > args.max_age_ms:
                        drop(original.meta, 'display_deadline_expired')
                        continue
                    matcher.add(Decoded(original.meta, rgb, end, local_capture, latency,
                                        uncertainty, actual_decoder))
                if len(origins) > 16:
                    raise RuntimeError('Decoder buffered over 16 frames')
            except (av.error.FFmpegError, RuntimeError) as exc:
                drop(unit.meta, 'decode_error')
                journal.log('decode_error_detail', unit.meta, message=str(exc))
                waiting_key = True
                request_key(stream)
                reset()

    if web_sink is not None:
        web_sink.bind(request_key, journal, stats, clock=clock)
    threads = [threading.Thread(target=guarded, args=(receive,), daemon=True)]
    threads += [threading.Thread(target=guarded, args=(decode, i), daemon=True) for i in range(args.streams)]
    started = time.perf_counter()
    next_status = started
    for thread in threads:
        thread.start()
    journal.log('start')
    ready = dict(port=sock.getsockname()[1], streams=args.streams, headless=args.headless)
    (directory / 'ready.json').write_text(json.dumps(ready))
    print(f'[receiver] ready {args.bind}:{ready["port"]}, clock={args.clock_mode}', flush=True)
    try:
        while not stop.is_set():
            now = time.perf_counter_ns()
            if (directory / 'STOP').exists():
                break
            if journal.error:
                raise RuntimeError(journal.error)
            group = matcher.poll(now)
            if group is not None:
                chosen, complete, skew = group
                group_info['total'] += 1
                group_info['complete'] += int(complete)
                group_info['missing'] = [i for i in range(args.streams) if i not in chosen]
                if complete:
                    group_info['skew'] = skew
                journal.log('group', complete=complete, sync_skew_ms=skew,
                            present_streams=list(chosen), group_id=group_info['total'])
                current_frames.update(chosen)
                for stream, frame in chosen.items():
                    latency = (now - frame.local_capture_ns) / 1e6 if frame.local_capture_ns else None
                    journal.log('selected', frame.meta, latency_ms=latency,
                                match_wait_ms=(now - frame.decoded_ns) / 1e6,
                                clock_uncertainty_ms=frame.uncertainty_ms)
            current = time.perf_counter()
            if current >= next_status:
                samples = stats.snapshot()
                if web_sink:
                    values = ' | '.join(f'S{i}: browser-submit={s["submit_fps"]:.0f}fps, '
                                       f'RTP={s["mbps"]:.2f}Mbps, drop={s["drops"]}' for i, s in enumerate(samples))
                else:
                    values = ' | '.join(f'S{i}: {s["fps"]:.0f}fps, {s["latency_ms"] or 0:.1f}ms, drop={s["drops"]}' for i, s in enumerate(samples))
                print(f'[receiver] {values}', flush=True)
                next_status = current + 2
            if args.duration and current - started >= args.duration:
                break
            stop.wait(.001)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=2)
        if web_sink is not None:
            web_sink.unbind()
        sock.close()
        journal.log('matcher_totals', discarded=matcher.dropped)
        journal.log('stop')
        # Save camera imagery only when explicitly requested, after media stops.
        if args.save_preview and web_sink is None:
            from .preview import Dashboard
            dashboard = Dashboard(args)
            dashboard.draw(current_frames, stats.snapshot(), group_info, time.perf_counter_ns())
            dashboard.save(directory / 'final-state.png')
            dashboard.save(directory / 'preview.png')
        summary = journal.close()
        print(f'[receiver] report: {args.output}/summary.json', flush=True)
    if not errors.empty():
        raise RuntimeError(errors.get())
    return summary
