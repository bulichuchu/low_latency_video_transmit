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
from .feedback import BestEffortSender, KeyRequestPacer
from .metrics import Journal, LiveStats
from .protocol import Assembler, ReceptionReport, parse_packet, pli, nack, reassembly_windows_ms
from .timing import PING_KEEP_NS, ClockMap, Decoded, FrameMatcher


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
    # Feedback never stops reception: an unreachable sender (hung or off the
    # network) makes sendto fail, and the stream must resume when it is back.
    control = BestEffortSender(sock, journal)
    queues = [queue.Queue(maxsize=8) for _ in range(args.streams)]
    # Large hardware IDRs may take up to 40 ms to send. Complete frames are
    # still delivered immediately; only partial IDRs get the longer lifetime.
    # On a long path both windows grow with the round trip (tune_windows).
    assemblers = [Assembler(args.reorder_ms, key_timeout_ms=50) for _ in queues]
    windows = [None] * args.streams  # (P, IDR) ms last applied, for the journal
    path_rtt = [None]  # ms: the median clock-ping round trip, updated with every ping
    recent_rtt = [None]  # ms: the latest one (or the unanswered wait), for the sender's congestion control
    last_pong = [0, 0]  # when the last pong arrived, and the t1 it answered
    long_path = [False]  # pongs' own round trip >= 12 ms (until < 8 ms): only then does a wait count
    reports = [ReceptionReport() for _ in queues]
    # Held while a packet goes through Assembler.add and a completed frame into its
    # queue, so the decode thread sees a frame in one of the two (still_coming).
    transit = [threading.Lock() for _ in queues]
    ssrcs = [None] * args.streams
    epochs = [None] * args.streams
    retired = [set() for _ in queues]
    receiver_ssrc = secrets.randbits(32)
    peer = [None]
    last_packet = [0]
    key_pacers = [KeyRequestPacer() for _ in range(args.streams)]
    sent_pings = {}
    last_rates = [None]
    control_lock = threading.Lock()
    current_frames = {}
    group_info = {'complete': 0, 'total': 0, 'skew': None}
    directory = Path(args.output)

    def request_key(stream):
        now = time.perf_counter_ns()
        with control_lock:
            if (peer[0] and ssrcs[stream] is not None
                    and key_pacers[stream].due(now, int((path_rtt[0] or 0) * 1e6))):
                if control.send(pli(receiver_ssrc, ssrcs[stream]), peer[0], 'pli'):
                    journal.log('rtcp_pli_sent', stream=stream, unanswered=key_pacers[stream].failures - 1)

    def waiting_since(now):
        """When the oldest clock ping still unanswered (and newer than the last
        answered one) was sent, once no pong has come for a second on a long path:
        congested, its replies stop, and the round trip is at least that long. Not
        on a LAN (where a lost burst of pongs or a stall of this process is not a
        queue on the path, and 0.5 behaviour is kept) nor before any answer (perhaps
        a one-way path)."""
        if not peer[0] or not long_path[0] or not last_pong[1] or now - last_pong[0] <= 1_000_000_000:
            return None
        waiting = [t for t in sent_pings if t > last_pong[1]]
        return min(waiting) if waiting else None

    def sender_rates(msg):
        """What the sender says it currently sends per stream (RK 0.6: bitrate and
        frame rate lowered on a poor link); shown on the page, journaled on change."""
        rates = msg.get('stream_rates')
        if not isinstance(rates, list):
            return
        clean = []
        for rate in rates[:args.streams]:
            ok = isinstance(rate, dict) and all(
                type(rate.get(k)) is int and 0 <= rate[k] <= 10_000_000 for k in ('kbps', 'fps', 'max_kbps', 'max_fps'))
            clean.append(dict(kbps=rate['kbps'], fps=rate['fps'], max_kbps=rate['max_kbps'], max_fps=rate['max_fps'],
                              reason=rate['reason'] if isinstance(rate.get('reason'), str) else '')
                         if ok else None)
        if clean != last_rates[0]:
            last_rates[0] = clean
            journal.log('sender_rate', rates=clean)
        if web_sink:
            web_sink.set_sender_rates(clean)

    def age_limit_ms():
        """--max-age-ms plus two round trips (as on the page): the time the widened
        reassembly windows may have spent on a retransmission."""
        return args.max_age_ms + 2 * min(path_rtt[0] or 0, 150)

    def tune_windows(stream):
        p_ms, key_ms = reassembly_windows_ms(args.reorder_ms, path_rtt[0])
        assemblers[stream].tune(p_ms, key_ms, path_rtt[0])
        # Room for every frame a widened IDR window can span (and a few more).
        assemblers[stream].max_pending = max(8, int(args.fps * key_ms / 1000) + 4)
        if windows[stream] is None or abs(p_ms - windows[stream][0]) >= 5 or abs(key_ms - windows[stream][1]) >= 5:
            windows[stream] = (p_ms, key_ms)
            journal.log('assembly_window', stream=stream, rtt_ms=path_rtt[0], p_ms=round(p_ms, 1),
                        key_ms=round(key_ms, 1))

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

    def adopt(address, now):
        """A new sender address: its clock, round trips and rates start over."""
        peer[0] = address
        last_packet[0] = now
        clock.clear()
        sent_pings.clear()
        last_pong[0] = last_pong[1] = 0
        long_path[0], last_rates[0] = False, None

    def receive():
        next_ping = next_report = time.perf_counter()
        while not stop.is_set():
            try:
                data, address = sock.recvfrom(65535)
            except socket.timeout:
                data, address = b'', None
            except OSError as exc:  # e.g. an ICMP error reported on the socket
                control.failed('recv', exc)
                data, address = b'', None
                stop.wait(.001)
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
                        last_pong[0], last_pong[1] = now, max(last_pong[1], t1)
                        names = msg.get('stream_names')
                        if web_sink and isinstance(names, list):
                            web_sink.set_stream_names([
                                names[i] if i < len(names) and e is not None and e[0] == msg.get('epoch') else ''
                                for i, e in enumerate(epochs)])
                        sender_rates(msg)
                    elif isinstance(msg, dict) and msg.get('v') == 1 and msg.get('type') == 'rk_beacon':
                        # RK 0.7 holds video back while it hears nothing from us (this receiver
                        # was started meanwhile, say) and says so: our pings bring it back.
                        # From our sender it shows that sender is still there.
                        if address == peer[0]:
                            last_packet[0] = now
                        elif not peer[0] or now - last_packet[0] >= 2_000_000_000:
                            adopt(address, now)
                            journal.log('sender_beacon')
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
                        adopt(address, now)
                    last_packet[0] = now
                    identity = (packet.meta.epoch, packet.ssrc)
                    if identity in retired[stream]:
                        continue
                    if epochs[stream] != identity:
                        last_rates[0] = None  # a new sender process: journal its rates again
                        if web_sink:
                            web_sink.set_stream_names([])
                            web_sink.set_sender_rates([])
                        if epochs[stream] is not None:
                            retired[stream].add(epochs[stream])
                            if len(retired[stream]) > 16:
                                retired[stream].pop()
                        epochs[stream] = identity
                        assemblers[stream] = Assembler(args.reorder_ms, key_timeout_ms=50)
                        windows[stream] = None
                        tune_windows(stream)
                        with control_lock:
                            key_pacers[stream] = KeyRequestPacer()
                        reports[stream] = ReceptionReport()
                        ssrcs[stream] = packet.ssrc
                        journal.log('stream_start', packet.meta, ssrc=packet.ssrc)
                    stats.add(stream, 'wire_bytes', len(data))
                    reports[stream].observe(packet, now)
                    full = False
                    revived = assemblers[stream].revived
                    with transit[stream]:
                        unit = assemblers[stream].add(packet, now)
                        if unit is not None:
                            try:
                                queues[stream].put_nowait(unit)
                            except queue.Full:
                                full = True
                    if assemblers[stream].revived != revived:
                        journal.log('frame_revived', packet.meta)
                    if unit is not None:
                        if unit.meta.key:
                            with control_lock:
                                key_pacers[stream].key_frame()
                        if full:
                            drop(unit.meta, 'compressed_decode_queue_full')
                            request_key(stream)
                except ValueError as exc:
                    journal.log('malformed_packet', reason=str(exc))
            for stream, assembler in enumerate(assemblers):
                if peer[0]:
                    for packet, missing in assembler.missing(now):
                        reports[stream].asked(missing)
                        if control.send(nack(receiver_ssrc, packet.ssrc, missing), peer[0], 'nack'):
                            journal.log('rtcp_nack_sent', packet.meta, packet_count=len(missing))
                for meta in assembler.expire(now):
                    drop(meta, 'incomplete_rtp_frame')
                    if not assembler.key_after(meta):  # else the decoder starts again from that IDR
                        request_key(stream)
            current = time.perf_counter()
            if peer[0] and current >= next_ping:
                t1 = time.perf_counter_ns()
                sent_pings[t1] = t1
                sent_pings_copy = list(sent_pings)
                for old in sent_pings_copy:
                    if t1 - old > PING_KEEP_NS:
                        del sent_pings[old]
                # rtt_ms: this receiver's view of the path for the sender (RK 0.5: pacing,
                # forced-IDR spacing, retransmission cache, bitrate); rtt_now_ms the latest,
                # for its congestion control (RK 0.6). Older senders ignore both.
                answered = clock.round_trip_ms(now)  # from pongs alone
                if answered is not None:
                    long_path[0] = answered >= (8 if long_path[0] else 12)  # as the RK sender decides
                waiting = waiting_since(now)
                path_rtt[0] = clock.round_trip_ms(now, waiting)  # every ping: known within ~0.4 s
                recent_rtt[0] = clock.recent_rtt_ms(now, waiting)
                if web_sink:
                    web_sink.set_path_rtt(path_rtt[0])
                for stream in range(args.streams):
                    tune_windows(stream)
                matcher.max_age = int(age_limit_ms() * 1e6)
                ping = dict(v=1, type='clock_ping', t1=t1)
                if path_rtt[0] is not None:
                    ping['rtt_ms'] = round(path_rtt[0], 1)
                if recent_rtt[0] is not None:
                    ping['rtt_now_ms'] = round(recent_rtt[0], 1)
                # Per stream, packets lost before retransmission, the sender's loss signal
                # (RK 0.7): resent (asked for again and not here 3/4 of a round trip later,
                # counted then) and missing (never asked for and not here half a second
                # after newer ones; not those skipped across an outage); on a short path
                # it takes unrepaired (asked for and never came) instead of resent. A
                # requested packet that came sooner than a resend could was only reordered
                # or delayed (10-21: most of them); older senders counted those as lost.
                ping['resent'] = [a.repaired for a in assemblers]
                ping['missing'] = [r.settle(now) for r in reports]
                ping['unrepaired'] = [r.unrepaired for r in reports]  # (of resent: never came)
                control.send(json.dumps(ping).encode(), peer[0], 'clock_ping')
                next_ping = current + .2
            if current >= next_report:
                offset, uncertainty = clock.estimate(now)
                for stream, report in enumerate(reports):
                    if peer[0] and ssrcs[stream] is not None:
                        control.send(report.packet(receiver_ssrc, ssrcs[stream]), peer[0], 'rtcp_rr')
                    s = stats.snapshot()[stream]
                    assembler = assemblers[stream]
                    journal.log('sample', stream=stream, fps=s['submit_fps'] if web_sink else s['fps'], rtp_mbps=s['mbps'],
                                fps_measurement='browser_reported_submit' if web_sink else 'native_decode',
                                decoded=s['decoded'], drops=s['drops'], clock_offset_ns=offset,
                                clock_uncertainty_ms=uncertainty, wire_bytes=s['wire_bytes'],
                                path_rtt_ms=path_rtt[0], resent=assembler.repaired,
                                nack_late=assembler.late, missing=report.lost_for_good,
                                revived=assembler.revived)
                next_report = current + 1

    def still_coming(stream, ssrc, epoch, missing, earliest):
        """The missing frame, or an IDR between it and the earliest complete one, is
        still being reassembled, or has just been completed and queued."""
        with transit[stream]:
            assembler = assemblers[stream]
            return (assembler.assembling(ssrc, epoch, missing)
                    or assembler.assembling_key(ssrc, epoch, missing, earliest)
                    or not queues[stream].empty())

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
                elif len(pending) < 8 and (
                        now - pending[earliest].complete_ns < args.reorder_ms * 1e6
                        or still_coming(stream, pending[earliest].ssrc, epoch, expected, earliest)):
                    # Reordering, or the missing frame (or an IDR after it) is still being
                    # reassembled: its window, widened on a long path, may still bring a
                    # retransmission.
                    continue
                else:
                    jump = earliest
                if jump > expected:
                    journal.log('reference_gap', stream=stream, first_missing=expected, next_available=jump)
                    waiting_key = True
                    if not pending[jump].meta.key:
                        request_key(stream)  # an IDR to jump to heals the gap by itself
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
                    if latency is not None and latency > age_limit_ms():
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
        control.flush()
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
