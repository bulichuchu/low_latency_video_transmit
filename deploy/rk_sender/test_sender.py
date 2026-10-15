"""Protocol/recovery/packaging tests; no RK, camera, or GI required.

The last test uses real H.264, UDP and the unmodified desktop receiver. Its
generated pictures are test fixtures only, never a production capture input.
"""
import ast
import errno
from fractions import Fraction
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import zipfile

import pytest

from video_demo import rk_sender
from video_demo.protocol import Assembler, START, nack, nals, parse_packet, pli
from video_demo.rk_sender import (MAX_KEY_REQUESTS, MPP_SONAME, CameraPipeline, CaptureTimes, IrqCounts, RtpOutput,
                                  Syncer, _leading_sum, bundled_mpp_lib, check_mpp_library, configure_rga,
                                  drain_frames, find_irqs, health_check, mapped_files, mpp_build_info, parse_args,
                                  pipeline_description, preload_mpp, resolve_mpp_lib, start_tracer, stop_tracer)

ROOT = Path(__file__).resolve().parents[2]
PEER = ('127.0.0.1', 5004)
SPS, PPS, IDR, P = b'\x67\x42\x00\x1f', b'\x68\xce\x32', b'\x65abc', b'\x41abc'


def au(*units):
    return b''.join(START + n for n in units)


class Clock:
    def __init__(self):
        self.now = 1_000_000_000

    def __call__(self):
        return self.now


class Socket:
    def __init__(self):
        self.sent = []
        self.fail_after = None

    def sendto(self, data, peer):
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise OSError(errno.ENETUNREACH, 'test: network unavailable')
        self.sent.append((data, peer))
        return len(data)

    def close(self):
        pass


def output():
    clock, sock = Clock(), Socket()
    return RtpOutput(PEER, ['RK A'], sock=sock, clock=clock, mtu=256), sock, clock


def reassemble(sent):
    assembler, unit = Assembler(), None
    for data, peer in sent:
        assert peer == PEER
        assert len(data) <= 256
        unit = assembler.add(parse_packet(data), 2_000_000_000) or unit
    assert unit is not None
    return unit


def test_mpp_au_goes_through_original_receiver_protocol_without_reencoding():
    tx, sock, clock = output()
    bits = au(SPS, PPS, IDR + b'x' * 3000)
    assert tx.submit(0, bits, clock() - 2_000_000)
    unit = reassemble(sock.sent)
    assert unit.bitstream == bits
    assert unit.meta.key and unit.meta.frame_id == 0
    assert unit.meta.capture_ns == clock() - 2_000_000
    assert unit.meta.encode_us == 2000
    assert unit.meta.sensor_status == 'unavailable'


def test_parameter_only_buffers_do_not_consume_frame_ids_and_headers_repeat_on_idr():
    tx, sock, clock = output()
    assert not tx.submit(0, au(SPS, PPS), None)
    assert tx.submit(0, au(IDR), clock() - 1_000_000)
    assert nals(reassemble(sock.sent).bitstream) == [SPS, PPS, IDR]
    assert tx.frame_ids == [1]


def test_partial_send_requires_fresh_idr_and_keeps_frame_gap_visible():
    tx, sock, clock = output()
    assert tx.submit(0, au(SPS, PPS, IDR), clock() - 1_000_000)
    sock.sent.clear()
    sock.fail_after = 1
    assert not tx.submit(0, au(P + b'x' * 2000), clock() - 1_000_000)
    assert len(sock.sent) == 1 and tx.key_requests[0].is_set()
    sock.sent.clear()
    sock.fail_after = None
    assert not tx.submit(0, au(P), clock() - 1_000_000)
    assert sock.sent == []
    assert tx.submit(0, au(IDR), clock() - 1_000_000)
    unit = reassemble(sock.sent)
    assert unit.meta.frame_id == 3 and unit.meta.key
    assert nals(unit.bitstream)[:2] == [SPS, PPS]


def test_expired_and_missing_pts_cannot_masquerade_as_fresh_low_latency_frames():
    tx, sock, clock = output()
    clock.now += 2_000_000_000  # room for a capture time older than the 1 s max age
    assert not tx.submit(0, au(SPS, PPS, IDR), clock() - 1_001_000_000)
    assert not tx.submit(0, au(IDR), None)
    assert not tx.submit(0, au(IDR), clock() + 1)
    assert sock.sent == []
    stats = tx.stats[0].snapshot()
    assert stats['expired'] == 1 and stats['missing_capture_time'] == 2
    assert tx.submit(0, au(IDR), clock() - 1_000_000)


def test_initial_p_frames_and_idr_without_headers_are_not_sent():
    tx, sock, clock = output()
    assert not tx.submit(0, au(P), clock() - 1)
    assert not tx.submit(0, au(IDR), clock() - 1)
    assert sock.sent == [] and tx.key_requests[0].is_set()


def test_nack_limits_retries_and_age_and_pli_targets_matching_ssrc():
    tx, sock, clock = output()
    assert tx.submit(0, au(SPS, PPS, IDR), clock() - 1_000_000)
    first = sock.sent[0][0]
    packet = parse_packet(first)
    sock.sent.clear()
    request = nack(123, packet.ssrc, [packet.sequence])
    for _ in range(4):
        tx.handle_control(request, PEER)
    assert [p for p, _ in sock.sent] == [first, first]
    clock.now += 31_000_000
    tx.handle_control(request, PEER)
    assert len(sock.sent) == 2
    assert tx.key_requests[0].is_set()  # First valid feedback also prompts an IDR.
    tx.key_requests[0].clear()
    tx.handle_control(pli(123, packet.ssrc + 1), PEER)
    assert not tx.key_requests[0].is_set()
    tx.handle_control(pli(123, packet.ssrc), ('127.0.0.2', 5004))
    assert not tx.key_requests[0].is_set()
    tx.handle_control(pli(123, packet.ssrc), PEER)
    assert tx.key_requests[0].is_set()


def test_late_frames_are_still_sent_and_only_stale_frames_cut_the_chain():
    tx, sock, clock = output()
    clock.now += 2_000_000_000  # room for a capture time older than the 1 s max age
    assert tx.submit(0, au(SPS, PPS, IDR), clock() - 1_000_000)
    tx.key_requests[0].clear()
    # 120 and 600 ms late: sending costs far less than a freeze until the next IDR.
    assert tx.submit(0, au(P), clock() - 120_000_000)
    assert tx.submit(0, au(P), clock() - 600_000_000)
    assert not tx.waiting_idr[0] and not tx.key_requests[0].is_set()
    stats = tx.stats[0].snapshot()
    assert stats['late'] == 2 and stats['sent'] == 3 and 'expired' not in stats
    assert not tx.submit(0, au(P), clock() - 1_001_000_000)
    assert tx.waiting_idr[0] and tx.key_requests[0].is_set()
    assert tx.stats[0].snapshot()['expired'] == 1


def test_late_idr_is_not_cut_short_by_the_old_75_ms_frame_age():
    # Field case (10-14): an IDR already 53 ms old when sending began was cut at
    # 75 ms frame age after 22 ms, 218 of 243 packets out, forcing another IDR.
    tx, sock, clock = output()
    accept = sock.sendto
    blocked_until = clock() + 30_000_000

    def draining(data, peer):
        if clock() < blocked_until:
            clock.now += 5_000_000
            raise BlockingIOError(errno.EAGAIN, 'full')
        return accept(data, peer)

    sock.sendto = draining
    assert tx.submit(0, au(SPS, PPS, IDR + b'x' * 3000), clock() - 60_000_000)
    assert reassemble(sock.sent).meta.key
    stats = tx.stats[0].snapshot()
    assert stats['idr_sent'] == 1 and 'send_timeout' not in stats
    assert stats['idr_send_ms_max'] >= 30


def test_default_gop_is_one_second_and_explicit_gop_is_kept():
    # Key requests are not yet proven to work on mpph264enc: the GOP is the guaranteed recovery.
    assert parse_args(['--host', '127.0.0.1']).gop == 30
    assert parse_args(['--host', '127.0.0.1', '--fps', '60']).gop == 60
    assert parse_args(['--host', '127.0.0.1', '--gop', '300']).gop == 300
    assert parse_args(['--host', '127.0.0.1']).max_frame_age_ms == 1000


def test_idr_earlier_than_the_gop_counts_as_forced():
    clock, sock = Clock(), Socket()
    tx = RtpOutput(PEER, ['RK A'], sock=sock, clock=clock, mtu=256, gop=30)
    assert tx.submit(0, au(SPS, PPS, IDR), clock() - 1_000_000)  # first IDR: start of stream
    for _ in range(5):
        assert tx.submit(0, au(P), clock() - 1_000_000)
    assert tx.submit(0, au(IDR), clock() - 1_000_000)  # 6 frames in: answers a key request
    for _ in range(29):
        assert tx.submit(0, au(P), clock() - 1_000_000)
    assert tx.submit(0, au(IDR), clock() - 1_000_000)  # 30 frames in: the periodic one
    stats = tx.stats[0].snapshot()
    assert stats['idr_encoded'] == 3 and stats['idr_forced'] == 1


def test_stage_timings_and_frame_log_cover_sent_and_dropped_frames():
    clock, sock = Clock(), Socket()
    tx = RtpOutput(PEER, ['RK A'], sock=sock, clock=clock, mtu=256, frame_log=True)
    cap = clock() - 20_000_000
    timing = dict(driver_ms=3.5, enc_in_ns=cap + 2_000_000, enc_out_ns=cap + 12_000_000, pts=4_000_000_000)
    assert tx.submit(0, au(SPS, PPS, IDR), cap, cap + 15_000_000, timing)
    tx.waiting_idr[0] = True
    assert not tx.submit(0, au(P), clock() - 5_000_000, clock(), None)
    sent, waiting = tx.frame_records
    assert sent['r'] == 'sent' and sent['k'] == 1 and sent['f'] == 0
    assert (sent['d'], sent['q'], sent['e'], sent['h']) == (3.5, 2.0, 10.0, 3.0)
    assert sent['a'] == 20.0 and 't' in sent and sent['c'] == cap
    assert sent['p'] == 4_000_000_000 and sent['w'] == clock()  # PTS; callback end
    assert waiting['r'] == 'awaiting_idr' and waiting['f'] == 1 and 'q' not in waiting
    stages = tx.stats[0].snapshot()['stage_ms']
    assert stages['encode'][0] == 10.0 and stages['queue'][0] == 2.0 and 'send' in stages
    assert not RtpOutput(PEER, ['RK A'], sock=Socket(), clock=clock).frame_records  # off by default


class FakeGst:
    CLOCK_TIME_NONE = 2**64 - 1

    class MessageType:
        ERROR, WARNING, EOS = 1, 2, 4

    class PadProbeReturn:
        OK, DROP = 1, 2


class FakeGstVideo:
    events = []

    @staticmethod
    def video_event_new_upstream_force_key_unit(running_time, all_headers, count):
        FakeGstVideo.events.append((running_time, all_headers, count))
        return ('force-key-unit', running_time)


class FakePad:
    def __init__(self):
        self.events = []

    def send_event(self, event):
        self.events.append(event)
        return True


def camera_with_fakes(tx):
    camera = object.__new__(CameraPipeline)
    camera.Gst, camera.GstVideo = FakeGst, FakeGstVideo
    camera.stream, camera.output, camera.error = 0, tx, None
    camera.last_key_ns = camera.key_count = camera.last_send_warning_ns = 0
    camera.started_ns = time.perf_counter_ns()
    pad = FakePad()
    camera.encoder = type('Encoder', (), {'get_static_pad': lambda self, name: pad})()
    camera.pipeline = type('Pipeline', (), {
        'get_clock': lambda self: type('Clock', (), {'get_time': lambda self: 5_000_000_000})(),
        'get_base_time': lambda self: 1_000_000_000})()
    camera.bus = type('Bus', (), {'pop_filtered': lambda self, mask: None})()
    return camera, pad


def test_key_requests_carry_running_time_and_wait_for_a_listening_receiver():
    FakeGstVideo.events.clear()
    tx, sock, clock = output()
    camera, pad = camera_with_fakes(tx)
    tx.key_requests[0].set()
    camera.poll()
    assert pad.events == [] and tx.key_requests[0].is_set()  # nobody listens yet: keep it pending
    tx.handle_control(json.dumps(dict(v=1, type='clock_ping', t1=1)).encode(), PEER)
    camera.poll()
    # Not "as soon as possible" (NONE): GstVideoEncoder drops repeated ASAP requests
    # when the encoder never flags its IDRs as keyframes.
    assert FakeGstVideo.events == [(4_000_000_000, True, 1)]
    assert pad.events == [('force-key-unit', 4_000_000_000)] and not tx.key_requests[0].is_set()
    tx.key_requests[0].set()
    camera.poll()
    assert len(pad.events) == 1  # at most one request per 150 ms
    assert tx.stats[0].snapshot()['key_requests'] == 1


def test_key_requests_are_recorded_in_the_frame_log():
    clock, sock = Clock(), Socket()
    tx = RtpOutput(PEER, ['RK A'], sock=sock, clock=clock, mtu=256, frame_log=True)
    camera, pad = camera_with_fakes(tx)
    tx.handle_control(json.dumps(dict(v=1, type='clock_ping', t1=1)).encode(), PEER)
    tx.key_requests[0].set()
    camera.poll()
    (record,) = tx.frame_records
    assert record['r'] == 'key_request' and record['n'] == 1 and record['ok'] == 1 and record['s'] == 0
    assert record['rt'] == 4_000_000_000  # the answer is the first IDR with PTS >= rt


def recovering_sender():
    """A sender whose receiver listens and whose encoder accepts key requests."""
    clock, sock = Clock(), Socket()
    tx = RtpOutput(PEER, ['RK A'], sock=sock, clock=clock, mtu=256)
    camera, pad = camera_with_fakes(tx)
    tx.handle_control(json.dumps(dict(v=1, type='clock_ping', t1=1)).encode(), PEER)  # also asks for an IDR
    assert tx.submit(0, au(SPS, PPS, IDR), clock() - 1_000_000)  # which this one answers
    assert not tx.key_requests[0].is_set()
    return tx, camera, pad, clock


def poll_now(camera):
    camera.last_key_ns = 0  # skip the 150 ms spacing between requests
    camera.poll()


def cut_chain(tx, clock):
    clock.now += 1_500_000_000
    assert not tx.submit(0, au(P), clock() - 1_001_000_000)  # older than the 1 s frame age


def test_frames_already_in_the_encoder_do_not_request_a_second_idr():
    # Once requests work, P frames captured before the request re-armed it and
    # every recovery would cost two ~300 KB IDRs.
    tx, camera, pad, clock = recovering_sender()
    cut_chain(tx, clock)
    poll_now(camera)
    assert len(pad.events) == 1 and not tx.key_requests[0].is_set()
    request = clock()
    for offset in (-60, -27, 6):  # captured before, or just after, the request: still P frames
        clock.now += 33_000_000
        assert not tx.submit(0, au(P), request + offset * 1_000_000)
    assert not tx.key_requests[0].is_set()
    clock.now += 33_000_000
    assert tx.submit(0, au(IDR), request + 39_000_000)
    poll_now(camera)
    assert len(pad.events) == 1 and tx.stats[0].snapshot()['awaiting_idr'] == 3


def test_a_p_frame_submitted_while_the_request_is_being_sent_does_not_re_arm_it():
    tx, camera, pad, clock = recovering_sender()
    cut_chain(tx, clock)
    in_flight = clock() - 20_000_000
    send_event = pad.send_event

    def meanwhile(event):  # the streaming thread runs while poll() is inside send_event
        assert not tx.submit(0, au(P), in_flight)
        return send_event(event)

    pad.send_event = meanwhile
    poll_now(camera)
    assert len(pad.events) == 1 and not tx.key_requests[0].is_set()


def test_a_rejected_request_is_asked_again_by_the_next_p_frame():
    tx, camera, pad, clock = recovering_sender()
    cut_chain(tx, clock)
    pad.send_event = lambda event: False
    poll_now(camera)
    assert tx.stats[0].snapshot()['key_request_rejected'] == 1
    clock.now += 33_000_000
    assert not tx.submit(0, au(P), clock() - 5_000_000)
    assert tx.key_requests[0].is_set()


def test_a_p_frame_captured_well_after_the_request_asks_again():
    tx, camera, pad, clock = recovering_sender()
    cut_chain(tx, clock)
    poll_now(camera)
    request = clock()
    clock.now += 150_000_000
    assert not tx.submit(0, au(P), request + 120_000_000)  # the encoder ignored the request
    assert tx.key_requests[0].is_set()
    poll_now(camera)
    assert len(pad.events) == 2


def test_an_idr_that_fails_to_send_asks_again():
    tx, camera, pad, clock = recovering_sender()
    cut_chain(tx, clock)
    poll_now(camera)
    tx.sock.fail_after = len(tx.sock.sent)
    clock.now += 40_000_000
    assert not tx.submit(0, au(IDR + b'x' * 2000), clock() - 1_000_000)
    assert tx.key_requests[0].is_set()
    poll_now(camera)
    assert len(pad.events) == 2


def test_a_pli_that_arrives_while_an_idr_is_going_out_is_kept():
    tx, camera, pad, clock = recovering_sender()
    accept, ssrc, start = tx.sock.sendto, tx.packetizers[0].ssrc, len(tx.sock.sent)

    def pli_midway(data, peer):
        if len(tx.sock.sent) == start + 2:
            clock.now += 1_000_000
            tx.handle_control(pli(123, ssrc), PEER)
        return accept(data, peer)

    tx.sock.sendto = pli_midway
    assert tx.submit(0, au(IDR + b'x' * 3000), clock() - 1_000_000)
    assert tx.key_requests[0].is_set()  # it may report a loss newer than this IDR


def test_requests_wait_while_the_receiver_is_silent():
    tx, camera, pad, clock = recovering_sender()
    tx.request_key(0)
    clock.now += 2_000_000_000
    poll_now(camera)
    assert pad.events == [] and tx.key_requests[0].is_set()
    tx.handle_control(json.dumps(dict(v=1, type='clock_ping', t1=2)).encode(), PEER)
    poll_now(camera)
    assert len(pad.events) == 1


def test_key_requests_stop_at_the_cap_and_leave_recovery_to_the_gop(capsys):
    tx, camera, pad, clock = recovering_sender()
    camera.key_count = MAX_KEY_REQUESTS
    tx.request_key(0)
    poll_now(camera)
    assert pad.events == [] and not tx.key_requests[0].is_set()
    assert 'periodic IDRs' in capsys.readouterr().err
    tx.request_key(0)
    poll_now(camera)
    assert pad.events == [] and capsys.readouterr().err == ''  # warned once
    assert tx.stats[0].snapshot()['key_request_capped'] == 2


def streams(n=2, gop=30, fps=30, stagger=True, frame_log=False):
    clock, sock = Clock(), Socket()
    clock.now = 10_000_000_000
    tx = RtpOutput(PEER, [f'RK {i}' for i in range(n)], sock=sock, clock=clock, mtu=256, gop=gop, fps=fps,
                   stagger=stagger, frame_log=frame_log)
    return tx, clock


def idr_at(tx, clock, stream, capture_ns):
    clock.now = max(clock.now, capture_ns + 1_000_000)
    assert tx.submit(stream, au(SPS, PPS, IDR), capture_ns)


MS = 1_000_000


def test_streams_that_start_together_get_their_idrs_half_a_gop_apart():
    tx, clock = streams()
    t0 = clock()
    idr_at(tx, clock, 0, t0)
    assert tx.stagger_at_ns == [None, None]  # nothing to compare with yet
    idr_at(tx, clock, 1, t0 + 3 * MS)  # 3 ms later: the same moment on the wire
    due = tx.stagger_at_ns[1]
    assert tx.stagger_at_ns[0] is None and abs(due - (t0 + 500 * MS)) < MS
    assert not tx.take_stagger(1, due - 1) and tx.take_stagger(1, due) and not tx.take_stagger(1, due)
    for at in (due + 2 * MS, due + 35 * MS):  # MPP answers a forced IDR with two
        idr_at(tx, clock, 1, at)
    t1 = t0 + 6000 * MS  # GOPs later, past the hold-off: half a GOP apart, nothing to do
    idr_at(tx, clock, 0, t1)
    idr_at(tx, clock, 1, t1 + 535 * MS)
    assert tx.stagger_at_ns == [None, None]


def test_only_idrs_within_two_frames_of_each_other_are_moved():
    for offset_ms, moved in ((60, True), (70, False), (930, False), (940, True)):
        tx, clock = streams()
        t0 = clock()
        idr_at(tx, clock, 0, t0)
        idr_at(tx, clock, 1, t0 + offset_ms * MS)
        assert (tx.stagger_at_ns[1] is not None) == moved, offset_ms


def test_a_stream_is_moved_at_most_once_per_hold_off():
    tx, clock = streams()
    t0 = clock()
    idr_at(tx, clock, 0, t0)
    idr_at(tx, clock, 1, t0 + 3 * MS)
    assert tx.take_stagger(1, tx.stagger_at_ns[1])
    idr_at(tx, clock, 1, t0 + 4003 * MS)  # the encoder ignored it: still on top of stream 0
    assert tx.stagger_at_ns == [None, None]
    idr_at(tx, clock, 1, t0 + 6003 * MS)
    assert tx.stagger_at_ns[0] is None and tx.stagger_at_ns[1] is not None


def test_one_meeting_moves_one_stream_even_with_mpps_second_idrs():
    tx, clock = streams()
    t0 = clock()
    # Both streams forced at once (a receiver reconnect); MPP answers each with two IDRs.
    for stream, at in ((0, 0), (1, 2), (0, 33), (1, 35)):
        idr_at(tx, clock, stream, t0 + at * MS)
    assert tx.stagger_at_ns[0] is None and abs(tx.stagger_at_ns[1] - (t0 + 500 * MS)) < MS


class FakeEncoder:
    """mpph264enc + MPP as measured on the board: an IDR every gop frames; a forced
    request makes the first frame captured from then on and the next one IDRs, and
    the GOP restarts at the second."""
    def __init__(self, gop):
        self.gop, self.count, self.forced_at, self.second = gop, None, None, False

    def is_idr(self, capture_ns):
        if self.count is None or self.second:
            self.count, self.second = 0, False
            return True
        if self.forced_at is not None and capture_ns >= self.forced_at:
            self.count, self.forced_at, self.second = 0, None, True
            return True
        self.count += 1
        if self.count == self.gop:
            self.count = 0
            return True
        return False


def simulate(starts_ms, seconds, pings_from_ms=None):
    """Cameras at 30 fps starting at starts_ms, polled every 10 ms; with pings_from_ms a
    receiver whose first feedback (a reconnect: an IDR on every stream) comes then.
    Returns the sender and each stream's IDR capture times."""
    n = len(starts_ms)
    tx, clock = streams(n=n)
    encoders = [FakeEncoder(30) for _ in range(n)]
    cameras = []
    for s in range(n):
        camera, pad = camera_with_fakes(tx)
        camera.stream = s
        pad.send_event = lambda event, s=s: setattr(encoders[s], 'forced_at', clock()) or True
        cameras.append(camera)
    t0, end, frame = clock(), clock() + seconds * 1000 * MS, 1e9 / 30
    events = [(t, 'poll', None, None) for t in range(t0, end, 10 * MS)]
    if pings_from_ms is not None:
        events += [(t, 'ping', None, None) for t in range(t0 + pings_from_ms * MS, end, 500 * MS)]
    for s, start in enumerate(starts_ms):
        k = 0
        while t0 + start * MS + k * frame < end:
            capture = int(t0 + start * MS + k * frame)
            events.append((capture + 5 * MS, 'frame', s, capture))
            k += 1
    idrs = [[] for _ in range(n)]
    for at, kind, s, capture in sorted(events, key=lambda e: e[:2]):
        clock.now = at
        if kind == 'frame':
            key = encoders[s].is_idr(capture)
            assert tx.submit(s, au(SPS, PPS, IDR) if key else au(P), capture)
            if key:
                idrs[s].append(capture)
        elif kind == 'poll':
            for camera in cameras:
                camera.poll()
        else:
            tx.handle_control(json.dumps(dict(v=1, type='clock_ping', t1=1)).encode(), PEER)
    return tx, idrs


def settled_gaps(idrs, since):
    """Smallest distance (ms) between IDRs of different streams captured after since."""
    late = [(t, s) for s, times in enumerate(idrs) for t in times if t >= since]
    return min(abs(a - b) for a, s in late for b, r in late if s != r) / MS


def test_staggering_settles_after_the_start_and_a_receiver_reconnect():
    tx, idrs = simulate([0, 2], 40, pings_from_ms=150)
    staggers = [tx.stats[s].snapshot().get('idr_staggers', 0) for s in range(2)]
    assert sum(staggers) == 1  # the start's plan was replaced by the reconnect; one move after it
    assert all(len(times) <= 45 for times in idrs)  # 40 periodic + 2 at the reconnect + 2 for the move
    assert settled_gaps(idrs, idrs[0][0] + 2000 * MS) > 400  # about half a GOP apart


def test_three_cameras_that_start_together_spread_out_once():
    tx, idrs = simulate([0, 1, 2], 30)
    assert sum(tx.stats[s].snapshot().get('idr_staggers', 0) for s in range(3)) == 2
    assert settled_gaps(idrs, idrs[0][0] + 2000 * MS) > 150


def test_three_streams_fill_the_largest_gap():
    tx, clock = streams(n=3)
    t0 = clock()
    idr_at(tx, clock, 0, t0)
    idr_at(tx, clock, 1, t0 + 333 * MS)
    assert tx.stagger_at_ns == [None, None, None]
    idr_at(tx, clock, 2, t0 + 2 * MS)  # on top of stream 0
    assert abs(tx.stagger_at_ns[2] - (t0 + 666 * MS)) < MS  # between stream 1 and stream 0's next


@pytest.mark.parametrize('n, gop, fps, stagger', [(1, 30, 30, True), (2, 30, 30, False), (2, 7, 30, True),
                                                 (2, 30, None, True)])
def test_no_staggering_for_one_stream_when_off_or_without_a_usable_gop(n, gop, fps, stagger):
    tx, clock = streams(n=n, gop=gop, fps=fps, stagger=stagger)
    t0 = clock()
    for i in range(n):
        idr_at(tx, clock, i, t0 + i * MS)
    assert tx.stagger_at_ns == [None] * n


def test_poll_forces_the_staggering_idr_without_a_receiver():
    FakeGstVideo.events.clear()
    tx, clock = streams(frame_log=True)
    camera, pad = camera_with_fakes(tx)
    camera.stream = 1
    t0 = clock()
    idr_at(tx, clock, 0, t0)
    idr_at(tx, clock, 1, t0 + 3 * MS)
    camera.poll()
    assert pad.events == []  # not due yet
    clock.now = tx.stagger_at_ns[1]
    camera.poll()
    assert pad.events == [('force-key-unit', 4_000_000_000)] and tx.stagger_at_ns[1] is None
    stats = tx.stats[1].snapshot()
    assert stats['idr_staggers'] == 1 and 'key_requests' not in stats
    (record,) = [r for r in tx.frame_records if r.get('r') == 'key_request']
    assert record['why'] == 'stagger' and record['s'] == 1 and record['ok'] == 1
    assert not tx.key_requests[1].is_set()  # not a receiver request
    clock.now += 200 * MS
    camera.poll()
    assert len(pad.events) == 1  # once


def test_a_receiver_request_replaces_a_pending_staggering_idr():
    tx, clock = streams(frame_log=True)
    camera, pad = camera_with_fakes(tx)
    camera.stream = 1
    t0 = clock()
    idr_at(tx, clock, 0, t0)
    idr_at(tx, clock, 1, t0 + 3 * MS)
    due = tx.stagger_at_ns[1]
    tx.handle_control(json.dumps(dict(v=1, type='clock_ping', t1=1)).encode(), PEER)  # asks every stream for an IDR
    camera.poll()
    assert len(pad.events) == 1 and tx.stagger_at_ns[1] is None
    clock.now = due + 200 * MS
    camera.poll()
    assert len(pad.events) == 1  # no second forced IDR for the old plan
    whys = [r['why'] for r in tx.frame_records if r.get('r') == 'key_request']
    assert whys == ['receiver']
    t1 = clock()
    for stream, at in ((0, 0), (1, 2), (0, 33), (1, 35)):  # the receiver's IDRs come out together...
        idr_at(tx, clock, stream, t1 + at * MS)
    assert tx.stagger_at_ns[0] is None and tx.stagger_at_ns[1] is not None  # ...and one is moved


class FakeInfo:
    def __init__(self, pts):
        self.buffer = type('Buffer', (), {'pts': pts})()

    def get_buffer(self):
        return self.buffer


def test_capture_probe_measures_driver_delay_and_encoder_probes_never_raise():
    tx, sock, clock = output()
    camera, _ = camera_with_fakes(tx)
    camera.times, camera.driver_ms, camera.encoder_in = CaptureTimes(), CaptureTimes(strict=False), CaptureTimes(strict=False)
    # Running time 4.000 s (clock 5 s - base 1 s); V4L2 timestamp 3.990 s: 10 ms in the driver.
    assert camera._capture(None, FakeInfo(3_990_000_000)) == FakeGst.PadProbeReturn.OK
    assert camera.driver_ms.take(3_990_000_000) == 10.0 and camera.times.take(3_990_000_000)
    for _ in range(2):  # a repeated PTS replaces the timing entry instead of stopping the pipeline
        assert camera._mark(None, FakeInfo(7), camera.encoder_in) == FakeGst.PadProbeReturn.OK
    assert len(camera.encoder_in.pending) == 1
    assert camera._mark(None, FakeInfo(FakeGst.CLOCK_TIME_NONE), camera.encoder_in) == FakeGst.PadProbeReturn.OK
    assert camera._mark(None, None, camera.encoder_in) == FakeGst.PadProbeReturn.OK  # diagnostic only
    assert len(camera.encoder_in.pending) == 1 and tx.stats[0].snapshot()['captured'] == 1


def test_frame_log_is_written_as_json_lines_and_synced_in_the_background(tmp_path):
    tx = RtpOutput(PEER, ['RK A'], sock=Socket(), clock=Clock(), frame_log=True)
    tx.frame_records.extend([dict(s=0, f=1), dict(s=0, r='key_request', n=1)])
    path = tmp_path / 'frames.jsonl'
    with path.open('a') as handle:
        drain_frames(tx, handle)
        syncer = Syncer(handle, None)
        syncer.sync()
        syncer.join()
    assert not tx.frame_records
    assert [json.loads(line) for line in path.read_text().splitlines()] == [
        dict(s=0, f=1), dict(s=0, r='key_request', n=1)]
    Syncer(handle).sync()  # a closed file is skipped, not an error


def test_clock_reply_is_original_four_timestamp_protocol_with_names_and_epoch():
    tx, sock, clock = output()
    ping = json.dumps(dict(v=1, type='clock_ping', t1=123)).encode()
    tx.handle_control(ping, PEER)
    msg = json.loads(sock.sent[-1][0])
    assert msg == dict(v=1, type='clock_pong', t1=123, t2=clock(), t3=clock(),
                       epoch=tx.epoch, stream_names=['RK A'])
    before = len(sock.sent)
    for bad in (b'{', b'\xff', b'[]', b'{"v":1,"type":"clock_ping","t1":true}'):
        tx.handle_control(bad, PEER)
    assert len(sock.sent) == before


def test_webrtc_viewer_gets_actionable_rtp_error():
    from video_demo.webrtc_signal import datagrams, parse
    tx, sock, _ = output()
    tx.handle_control(datagrams('webrtc_offer', 'abc', 'v=0')[0], PEER)
    reply = parse(sock.sent[-1][0])
    assert reply['type'] == 'webrtc_error' and reply['id'] == 'abc'
    assert 'RTP' in reply['d']


def test_capture_timestamp_map_survives_raw_drops_with_bounded_memory():
    times = CaptureTimes(limit=3)
    for pts in range(10):
        times.put(pts, 100 + pts)
    assert len(times.pending) == 3
    assert times.take(5) is None
    assert times.take(8) == 108
    with pytest.raises(RuntimeError, match='duplicate'):
        times.put(9, 1000)


@pytest.mark.parametrize('options', [
    ['--device', '/dev/video23 ! fakesrc'], ['--device', '/dev/video-enc0'],
    ['--device', '/dev/video23', '--device', '/dev/video23'],
    ['--fps', '0'], ['--width', '1279'], ['--width', '1000'], ['--height', '1080'], ['--duration', 'nan'],
    ['--max-frame-age-ms', 'nan'], ['--mtu', '9000'], ['--gop', '0'],
    ['--label', 'a', '--label', 'b'], ['--host', 'rtsp://camera'],
])
def test_invalid_arguments_rejected_before_device_access(options):
    with pytest.raises(SystemExit):
        parse_args(['--host', '127.0.0.1'] + options)


def test_sizes_must_be_multiples_of_16_and_1080p_gets_a_hint(capsys):
    # MPP pads to 16; GStreamer then has the ISP crop the camera image (10-15: top-left quarter).
    with pytest.raises(SystemExit):
        parse_args(['--host', '127.0.0.1', '--width', '1920', '--height', '1080'])
    assert '--width 1920 --height 1088' in capsys.readouterr().err
    for width, height in ((1920, 1088), (1792, 1008), (1280, 720), (960, 544)):
        args = parse_args(['--host', '127.0.0.1', '--width', str(width), '--height', str(height)])
        assert (args.width, args.height) == (width, height)


class FakeISP:
    """VIDIOC_[GS]_SELECTION on a Rockchip ISP selfpath node (multi-planar)."""
    def __init__(self, crop=(0, 0, 1920, 1080), bounds=(0, 0, 3840, 2160), buf_type=9, error=None):
        self.crop, self.bounds, self.buf_type, self.error = crop, bounds, buf_type, error
        self.calls = []

    def __call__(self, fd, request, data):
        buf_type, target, flags, *rect = rk_sender._SELECTION.unpack(data)
        self.calls.append((request, buf_type, target, tuple(rect)))
        if self.error is not None:
            raise OSError(self.error, os.strerror(self.error))
        if buf_type != self.buf_type:
            raise OSError(errno.EINVAL, 'wrong buffer type')
        if request == rk_sender.VIDIOC_S_SELECTION:
            self.crop = tuple(rect)
        else:
            rect = self.bounds if target == rk_sender.V4L2_SEL_TGT_CROP_BOUNDS else self.crop
        data[:] = rk_sender._SELECTION.pack(buf_type, target, flags, *rect)


def test_selection_ioctls_match_the_kernel_abi():
    assert rk_sender._SELECTION.size == 64  # struct v4l2_selection
    assert (rk_sender.VIDIOC_G_SELECTION, rk_sender.VIDIOC_S_SELECTION) == (0xC040565E, 0xC040565F)


def test_a_crop_left_by_an_earlier_run_is_reset_to_the_full_image(tmp_path):
    node = tmp_path / 'video23'
    node.write_bytes(b'')
    isp = FakeISP()
    full, quarter = (0, 0, 3840, 2160), (0, 0, 1920, 1080)
    assert rk_sender.camera_crop(str(node), ioctl=isp) == (quarter, full, quarter)  # reading only
    assert isp.crop == quarter and rk_sender.VIDIOC_S_SELECTION not in [c[0] for c in isp.calls]
    assert rk_sender.camera_crop(str(node), reset=True, ioctl=isp) == (quarter, full, full)
    assert isp.crop == full
    isp.calls.clear()
    assert rk_sender.camera_crop(str(node), reset=True, ioctl=isp) == (full, full, full)
    assert [c[0] for c in isp.calls] == [rk_sender.VIDIOC_G_SELECTION] * 2  # nothing to change
    single = FakeISP(buf_type=1)  # a single-planar node
    assert rk_sender.camera_crop(str(node), reset=True, ioctl=single)[2] == full and single.crop == full
    with pytest.raises(OSError):
        rk_sender.camera_crop(str(node), reset=True, ioctl=FakeISP(error=errno.ENOTTY))


def test_crop_report_names_each_node(capsys):
    states = {'/dev/video23': ((0, 0, 1920, 1080), (0, 0, 3840, 2160), (0, 0, 3840, 2160)),
              '/dev/video32': ((0, 0, 3840, 2160),) * 3,
              '/dev/video40': ((0, 0, 1920, 1080), (0, 0, 3840, 2160), (0, 0, 1920, 1080))}

    def crop(device, reset):
        if device == '/dev/video9':
            raise OSError(errno.ENOENT, 'No such file or directory')
        return states[device]

    rk_sender.check_camera_crops(['/dev/video23', '/dev/video32', '/dev/video40', '/dev/video9'], True, crop)
    out, err = capsys.readouterr()
    assert out == ('[rk] /dev/video23: camera crop (0,0) 1920x1080 reset to the full (0,0) 3840x2160\n'
                   '[rk] /dev/video32: camera crop full (0,0) 3840x2160\n')
    assert '/dev/video40: camera crop (0,0) 1920x1080 of (0,0) 3840x2160 (could not be reset)' in err
    assert '/dev/video9: cannot read the camera crop' in err
    rk_sender.check_camera_crops(['/dev/video40'], False, crop)
    assert '(reset when sending)' in capsys.readouterr().out


def test_run_resets_the_crop_before_opening_the_cameras(monkeypatch):
    order = []
    monkeypatch.setattr(rk_sender, 'check_camera_crops', lambda devices, reset: order.append(('crop', reset)))

    def pipeline(*args):
        order.append('pipeline')
        raise RuntimeError('stop here')

    monkeypatch.setattr(rk_sender, 'CameraPipeline', pipeline)
    args = parse_args(['--host', '127.0.0.1', '--device', '/dev/video23', '--device', '/dev/video32'])
    with pytest.raises(RuntimeError, match='stop here'):
        rk_sender.run(args, None, None)
    assert order == [('crop', True), 'pipeline']


def test_pipeline_only_drops_before_encoder_and_uses_actual_supported_properties():
    args = parse_args(['--host', '127.0.0.1'])
    pipeline = pipeline_description(args, args.device[0])
    assert pipeline.index('leaky=downstream') < pipeline.index('mpph264enc')
    assert 'max-size-buffers=1' in pipeline and 'drop=false' in pipeline
    assert 'max-reenc=0' in pipeline and 'header-mode=each-idr' in pipeline
    assert 'profile=baseline' in pipeline and 'gop=30 ' in pipeline
    assert 'videoconvert' not in pipeline and 'x264enc' not in pipeline
    assert 'format=NV12,' in pipeline and 'zero-copy-pkt' not in pipeline  # plugin default unless asked


def test_each_camera_names_its_elements_so_threads_are_told_apart():
    args = parse_args(['--host', '127.0.0.1', '--pixel-format', 'YUY2'])
    pipeline = pipeline_description(args, '/dev/video32', 1)
    for name in ('camera1', 'raw_latest1', 'encoder1', 'encoded1'):
        assert f'name={name} ' in pipeline
    assert len('raw_latest1:src') <= 15  # Linux thread names keep 15 characters
    assert 'format=YUY2,' in pipeline


def test_new_options_have_conservative_defaults():
    args = parse_args(['--host', '127.0.0.1'])
    assert (args.pixel_format, args.rga, args.zero_copy_pkt, args.trace) == ('NV12', False, 'auto', None)
    for bad in (['--pixel-format', 'I420'], ['--zero-copy-pkt', 'yes']):
        with pytest.raises(SystemExit):
            parse_args(['--host', '127.0.0.1'] + bad)


def test_rga_is_off_unless_asked_and_a_user_setting_is_kept():
    env = {}
    assert configure_rga(False, env) is False and env == {'GST_MPP_NO_RGA': '1'}
    assert configure_rga(True, env) is True and env == {}
    env = {'GST_MPP_NO_RGA': 'yes'}
    assert configure_rga(False, env) is False and env == {'GST_MPP_NO_RGA': 'yes'}


class FakeProbePad:
    def __init__(self):
        self.probes = 0

    def add_probe(self, *args):
        self.probes += 1


class FakeElement:
    def __init__(self, props=None):
        self.props = dict(props or {})
        self.pads = {'sink': FakeProbePad(), 'src': FakeProbePad()}
        self.signals = []

    def find_property(self, name):
        return object() if name in self.props else None

    def set_property(self, name, value):
        self.props[name] = value

    def get_property(self, name):
        return self.props[name]

    def get_static_pad(self, name):
        return self.pads[name]

    def connect(self, signal, callback):
        self.signals.append(signal)


def fake_gst(encoder_props):
    elements = {}

    class Pipeline:
        def get_by_name(self, name):
            return elements.setdefault(name, FakeElement(encoder_props if name.startswith('encoder') else None))

        def get_bus(self):
            return None

    class Gst(FakeGst):
        class PadProbeType:
            BUFFER = 1

        @staticmethod
        def parse_launch(description):
            assert 'name=encoder1 ' in description
            return Pipeline()

    return Gst, elements


@pytest.mark.parametrize('choice, props, expected', [
    ('auto', {'zero-copy-pkt': True}, True), ('off', {'zero-copy-pkt': True}, False),
    ('on', {'zero-copy-pkt': False}, True), ('auto', {}, None)])
def test_zero_copy_pkt_is_set_only_when_asked_and_reported(choice, props, expected):
    Gst, elements = fake_gst(props)
    args = parse_args(['--host', '127.0.0.1', '--zero-copy-pkt', choice])
    camera = CameraPipeline(Gst, FakeGstVideo, args, '/dev/video32', 1, output()[0])
    assert camera.zero_copy_pkt is expected
    assert elements['raw_latest1'].signals == ['overrun'] and elements['encoded1'].signals == ['new-sample']


def test_missing_zero_copy_pkt_property_fails_clearly_when_asked():
    Gst, _ = fake_gst({})
    args = parse_args(['--host', '127.0.0.1', '--zero-copy-pkt', 'off'])
    with pytest.raises(RuntimeError, match='zero-copy-pkt'):
        CameraPipeline(Gst, FakeGstVideo, args, '/dev/video32', 1, output()[0])


def test_encoder_probes_only_with_frame_log(tmp_path):
    for frame_log, probes in ((None, 0), (str(tmp_path / 'f.jsonl'), 1)):
        Gst, elements = fake_gst({})
        argv = ['--host', '127.0.0.1'] + (['--frame-log', frame_log] if frame_log else [])
        CameraPipeline(Gst, FakeGstVideo, parse_args(argv), '/dev/video32', 1, output()[0])
        encoder = elements['encoder1']
        assert (encoder.pads['sink'].probes, encoder.pads['src'].probes) == (probes, probes)
        assert elements['camera1'].pads['src'].probes == 1  # capture time is always needed


MPP_VERSION = b'c3b2ece author: Chandler Chen  2024-11-15 feat[hal_avsd]: enable hw dec timeout  (grafted, HEAD)'


def fake_mpp(path, blob=MPP_VERSION):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'\x7fELF\x02\x01\x01\0' + b'\0' * 8 + blob + b'\0tail')
    return path


def maps_line(path, perms='r-xp'):
    return f'7f0000000000-7f0000001000 {perms} 00000000 b3:06 1234       {path}\n'


def test_mapped_files_lists_each_mpp_library_once(tmp_path):
    system = '/usr/lib/aarch64-linux-gnu/librockchip_mpp.so.0'
    ours = str(tmp_path / 'mpp fix' / MPP_SONAME)
    maps = tmp_path / 'maps'
    maps.write_text(maps_line('/usr/bin/python3.8') + maps_line(system, 'r--p') + maps_line(system)
                    + '7f02-7f03 rw-p 00000000 00:00 0\n' + maps_line(ours + ' (deleted)')
                    + maps_line('/usr/lib/aarch64-linux-gnu/librga.so.2'))
    assert mapped_files('librockchip_mpp', str(maps)) == [os.path.realpath(system), os.path.realpath(ours)]
    assert mapped_files('librockchip_mpp', str(tmp_path / 'missing')) == []


def test_mpp_build_info_reads_the_version_line_mpp_embeds(tmp_path):
    digest, version = mpp_build_info(fake_mpp(tmp_path / 'a' / MPP_SONAME))
    assert version == 'c3b2ece author: Chandler Chen 2024-11-15 feat[hal_avsd]: enable hw dec timeout (grafted, HEAD)'
    assert re.fullmatch('[0-9a-f]{16}', digest)
    assert mpp_build_info(fake_mpp(tmp_path / 'b' / MPP_SONAME, b'no version here'))[1] is None


def test_resolve_mpp_lib_accepts_the_file_or_its_directory(tmp_path):
    lib = fake_mpp(tmp_path / 'mpp-fix' / MPP_SONAME)
    assert resolve_mpp_lib(tmp_path / 'mpp-fix') == resolve_mpp_lib(lib) == Path(os.path.realpath(lib))
    for missing in (tmp_path / 'nothing', tmp_path):  # tmp_path is a directory without the library
        with pytest.raises(RuntimeError, match='not found'):
            resolve_mpp_lib(missing)


def test_preload_mpp_reports_a_file_that_is_not_a_library(tmp_path):
    with pytest.raises(RuntimeError, match='Cannot load MPP library'):
        preload_mpp(fake_mpp(tmp_path / MPP_SONAME))


def test_check_mpp_library_insists_on_the_requested_build(tmp_path, capsys):
    lib = fake_mpp(tmp_path / 'fix' / MPP_SONAME)
    expected = Path(os.path.realpath(lib))
    system = '/usr/lib/aarch64-linux-gnu/librockchip_mpp.so.0'
    maps = tmp_path / 'maps'
    for text in (maps_line(system), maps_line(lib) + maps_line(system), ''):
        maps.write_text(text)
        with pytest.raises(RuntimeError, match='expected only'):
            check_mpp_library(expected, str(maps))
    maps.write_text(maps_line(lib) + maps_line(system))
    with pytest.raises(RuntimeError, match='More than one'):
        check_mpp_library(None, str(maps))
    maps.write_text(maps_line(lib))
    info = check_mpp_library(expected, str(maps))
    assert info == dict(mpp_lib=str(expected), mpp_sha256=mpp_build_info(lib)[0],
                        mpp_version=mpp_build_info(lib)[1])
    assert f'[rk] MPP {expected} sha256 ' in capsys.readouterr().out
    assert check_mpp_library(None, str(tmp_path / 'none')) == dict(mpp_lib=None, mpp_sha256=None, mpp_version=None)


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux dynamic linker and /proc')
def test_preloaded_build_is_the_one_this_process_reports(tmp_path):
    import ctypes
    import ctypes.util
    name = ctypes.util.find_library('z')
    if not name:
        pytest.skip('no libz to stand in for MPP')
    ctypes.CDLL(name)
    real = next(p for p in mapped_files('libz.so') if os.path.basename(p).startswith('libz.so'))
    lib = tmp_path / 'mpp-fix' / MPP_SONAME
    lib.parent.mkdir()
    lib.write_bytes(Path(real).read_bytes() + b'\0abc1234 author: build  2026-10-14 fix\0')  # trailing data is ignored
    expected = resolve_mpp_lib(lib.parent)
    preload_mpp(expected)
    info = check_mpp_library(expected)
    assert info['mpp_lib'] == str(expected) and 'abc1234 author: build 2026-10-14 fix' in info['mpp_version']


def test_main_loads_the_requested_mpp_before_gstreamer(tmp_path, monkeypatch):
    monkeypatch.setenv('GST_MPP_NO_RGA', '1')
    lib = fake_mpp(tmp_path / 'mpp-fix' / MPP_SONAME)
    calls = []
    monkeypatch.setattr(rk_sender, 'preload_mpp', lambda path: calls.append(('preload', path)))
    monkeypatch.setattr(rk_sender, 'load_gst', lambda: calls.append(('gst',)) or ('Gst', 'GstVideo'))
    monkeypatch.setattr(rk_sender, 'check_runtime', lambda Gst, args: calls.append(('check',)))
    monkeypatch.setattr(rk_sender, 'check_mpp_library', lambda expected: calls.append(('mpp', expected)) or {})
    assert rk_sender.main(['--doctor', '--mpp-lib', str(lib.parent)]) == 0
    real = Path(os.path.realpath(lib))
    assert calls == [('preload', real), ('gst',), ('check',), ('mpp', real)]
    calls.clear()
    assert rk_sender.main(['--doctor']) == 0
    assert calls == [('gst',), ('check',), ('mpp', None)]
    calls.clear()
    assert rk_sender.main(['--doctor', '--mpp-lib', str(tmp_path / 'none')]) == 1  # a clear error, nothing loaded
    assert calls == []


def test_bundled_mpp_is_looked_for_only_next_to_a_pyz(tmp_path):
    pyz = tmp_path / 'fzh' / 'rk_sender.pyz'
    pyz.parent.mkdir()
    pyz.write_bytes(b'PK\x05\x06' + b'\0' * 18)
    inside = pyz / 'video_demo' / 'rk_sender.py'  # how zipimport names this module
    assert bundled_mpp_lib(str(inside)) is None  # no mpp-pkt/ there
    lib = Path(os.path.realpath(fake_mpp(tmp_path / 'fzh' / 'mpp-pkt' / MPP_SONAME)))
    assert bundled_mpp_lib(str(inside)) == lib
    link = tmp_path / 'bin' / 'rk_sender.pyz'  # run through a symlink to the .pyz
    link.parent.mkdir()
    link.symlink_to(pyz)
    assert bundled_mpp_lib(str(link / 'video_demo' / 'rk_sender.py')) == lib
    source = tmp_path / 'fzh' / 'video_demo' / 'rk_sender.py'  # the source tree, even with mpp-pkt/ around
    assert bundled_mpp_lib(str(source)) is None


def test_main_uses_the_bundled_mpp_unless_told_otherwise(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('GST_MPP_NO_RGA', '1')
    bundled = fake_mpp(tmp_path / 'mpp-pkt' / MPP_SONAME)
    other = fake_mpp(tmp_path / 'mpp-fix' / MPP_SONAME)
    calls = []
    monkeypatch.setattr(rk_sender, 'bundled_mpp_lib', lambda: bundled)
    monkeypatch.setattr(rk_sender, 'preload_mpp', lambda path: calls.append(('preload', path)))
    monkeypatch.setattr(rk_sender, 'load_gst', lambda: calls.append(('gst',)) or ('Gst', 'GstVideo'))
    monkeypatch.setattr(rk_sender, 'check_runtime', lambda Gst, args: calls.append(('check',)))
    monkeypatch.setattr(rk_sender, 'check_mpp_library', lambda expected: calls.append(('mpp', expected)) or {})
    assert rk_sender.main(['--doctor']) == 0
    real = Path(os.path.realpath(bundled))
    assert calls == [('preload', real), ('gst',), ('check',), ('mpp', real)]
    assert f'[rk] using {bundled} next to the pyz' in capsys.readouterr().out
    calls.clear()
    assert rk_sender.main(['--doctor', '--mpp-lib', 'system']) == 0
    assert calls == [('gst',), ('check',), ('mpp', None)]
    assert 'next to the pyz' not in capsys.readouterr().out
    calls.clear()
    assert rk_sender.main(['--doctor', '--mpp-lib', str(other.parent)]) == 0
    assert calls[0] == ('preload', Path(os.path.realpath(other)))


def test_the_pyz_picks_up_mpp_pkt_next_to_it(tmp_path):
    spec = importlib.util.spec_from_file_location('rk_builder', ROOT / 'tools/build_rk_sender.py')
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    bundle = builder.build(tmp_path / 'fzh' / 'rk_sender.pyz')
    bare = builder.build(tmp_path / 'bare' / 'rk_sender.pyz')
    lib = os.path.realpath(fake_mpp(tmp_path / 'fzh' / 'mpp-pkt' / MPP_SONAME))  # not a loadable library

    def run(pyz, *extra):
        return subprocess.run([sys.executable, '-I', '-S', str(pyz), '--doctor', *extra],
                              cwd=tmp_path, capture_output=True, text=True, timeout=20)

    picked = run(bundle)
    assert picked.returncode == 1 and f'[rk] using {lib} next to the pyz' in picked.stdout
    assert 'Cannot load MPP library' in picked.stderr and 'Traceback' not in picked.stderr
    # Both go on to GStreamer (absent here) without touching an MPP library first.
    for done in (run(bundle, '--mpp-lib', 'system'), run(bare)):
        assert 'using' not in done.stdout and 'MPP library' not in done.stderr
        assert 'Traceback' not in done.stderr
    assert 'no mpp-pkt/ next to the pyz' in run(bare).stdout


PROC_INTERRUPTS = '''           CPU0       CPU1
 13:     1166       1147     GICv3  26 Level     arch_timer
 34:        0         94     GICv3 146 Level     fdb60f00.iommu, rga3_core0
 48:        0         77     GICv3 133 Level     fdbd0000.rkvenc-core
 51:        3         22     GICv3 136 Level     fdbe0000.rkvenc-core
IPI0:    3180       4282       Rescheduling interrupts
Err:        0
'''


def test_trace_finds_encoder_and_rga_interrupts():
    irqs = find_irqs(PROC_INTERRUPTS)
    assert sorted(irqs) == [34, 48, 51]
    assert 'rkvenc' in irqs[48] and 'rga3_core0' in irqs[34]
    assert _leading_sum('0 77 GICv3 133 Level'.split()) == 77  # the hardware irq number is not a count
    assert IrqCounts({}).read() == {}


def read_trace(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='reads Linux /proc')
def test_tracer_follows_a_live_process_and_stops_cleanly(tmp_path):
    child = subprocess.Popen([sys.executable, '-c', 'import threading, time; '
                              'threading.Thread(target=time.sleep, args=(30,), daemon=True).start(); time.sleep(30)'])
    path = tmp_path / 'trace.jsonl'
    try:
        tracer = start_tracer(child.pid, str(path))
        time.sleep(.8)
        stop_tracer(tracer)
        assert tracer.returncode == 0
    finally:
        child.kill()
        child.wait()
    records = read_trace(path)
    assert records[0]['event'] == 'trace_start' and records[0]['pid'] == child.pid
    states = [row for r in records if 'th' in r for row in r['th']]
    assert {row[0] for row in states} >= {child.pid} and all(row[1] in 'RSDTtIZ' for row in states)
    assert any(child.pid == int(tid) for r in records if 'names' in r for tid in r['names'])
    assert records[-1]['event'] == 'trace_stop' and records[-1]['reason'] == 'stopped'


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='reads Linux /proc')
def test_tracer_ends_by_itself_when_the_sender_exits(tmp_path):
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(.3)'])
    path = tmp_path / 'trace.jsonl'
    tracer = start_tracer(child.pid, str(path))
    child.wait()
    tracer.wait(timeout=5)
    records = read_trace(path)
    assert records[-1]['event'] == 'trace_stop' and records[-1]['reason'] == 'process exited'


def test_send_deadline_stops_socket_congestion_without_busy_loop():
    tx, sock, clock = output()

    def congested(data, peer):
        clock.now += 5_000_000
        raise BlockingIOError(errno.EAGAIN, 'full')

    sock.sendto = congested
    assert not tx.submit(0, au(SPS, PPS, IDR), clock() - 1_000_000)
    assert clock.now <= 1_050_000_000  # IDR budget 45 ms: the receiver reassembles IDRs for 50 ms
    assert tx.waiting_idr[0]
    stats = tx.stats[0].snapshot()
    assert stats['send_timeout'] == 1
    assert stats['last_send_error']['errno'] == 'EAGAIN'
    assert stats['last_send_error']['reason'] == 'send_timeout'


def test_closed_receiver_is_distinct_from_timeout_and_can_recover_after_five_seconds():
    tx, sock, clock = output()
    started = clock()
    original_send = sock.sendto

    def refused(data, peer):
        raise OSError(errno.ECONNREFUSED, 'test: no receiver')

    sock.sendto = refused
    for _ in range(8):
        assert not tx.submit(0, au(SPS, PPS, IDR), clock() - 1_000_000)
        stats = tx.stats[0].snapshot()
        stats['last_capture_ns'] = clock()
        warning = health_check(stats, started, clock())
        if clock() - started > 5_000_000_000:
            assert 'continuing recovery' in warning
        clock.now += 1_000_000_000
    assert stats['send_socket_error'] == 8 and 'send_timeout' not in stats
    assert stats['last_send_error']['errno'] == 'ECONNREFUSED'
    assert tx.receiver_status()['state'] == 'waiting'
    sock.sendto = original_send
    tx.handle_control(json.dumps(dict(v=1, type='clock_ping', t1=42)).encode(), PEER)
    assert tx.receiver_status()['state'] == 'active'
    assert tx.key_requests[0].is_set()
    sock.sent.clear()
    assert tx.submit(0, au(IDR), clock() - 1_000_000)
    assert reassemble(sock.sent).meta.key
    assert not tx.waiting_idr[0]
    assert tx.stats[0].snapshot()['sent'] == 1


def test_stalled_capture_or_encoding_still_fails_fast():
    fields = dict(last_capture_ns=0, last_encoded_ns=0, last_matched_ns=0, last_sent_ns=0)
    with pytest.raises(RuntimeError, match='no camera frames'):
        health_check(fields, 1, 6_000_000_001)
    fields['last_capture_ns'] = 6_000_000_001
    with pytest.raises(RuntimeError, match='no encoded frames'):
        health_check(fields, 1, 6_000_000_001)
    fields['last_encoded_ns'] = 6_000_000_001
    with pytest.raises(RuntimeError, match='cannot match encoded PTS'):
        health_check(fields, 1, 6_000_000_001)


def test_feedback_state_requires_valid_peer_and_recovers_on_reconnect():
    tx, sock, clock = output()
    ping = json.dumps(dict(v=1, type='clock_ping', t1=123)).encode()
    tx.handle_control(ping, ('127.0.0.2', 5004))
    tx.handle_control(b'{"v":1}', PEER)
    assert tx.receiver_status()['state'] == 'waiting'
    tx.handle_control(ping, PEER)
    assert tx.receiver_status()['state'] == 'active'
    tx.key_requests[0].clear()
    clock.now += 3_000_000_000
    assert tx.receiver_status()['state'] == 'stale'
    tx.handle_control(ping, PEER)
    assert tx.key_requests[0].is_set()
    tx._control_error(OSError(errno.EHOSTUNREACH, 'host unavailable'))
    assert tx.receiver_status()['last_control_error'] == 'EHOSTUNREACH'


def test_processing_measurement_updates_while_waiting_for_idr():
    tx, sock, clock = output()
    for _ in range(10):
        assert not tx.submit(0, au(P), clock() - 8_000_000)
        clock.now += 33_000_000
    assert tx.stats[0].snapshot()['sender_ms_p50'] == 8


def test_bundle_is_standalone_and_contains_only_allowlisted_sources(tmp_path):
    spec = importlib.util.spec_from_file_location('rk_builder', ROOT / 'tools/build_rk_sender.py')
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    bundle = builder.build(tmp_path / 'rk_sender.pyz')
    original = bundle.read_bytes()
    builder.build(bundle)
    assert bundle.read_bytes() == original
    with zipfile.ZipFile(bundle) as archive:
        assert set(archive.namelist()) == {'__main__.py', 'README.md'} | {
            f'video_demo/{m}' for m in builder.MODULES}
        assert archive.read('video_demo/protocol.py') == (ROOT / 'video_demo/protocol.py').read_bytes()
        for name in archive.namelist():
            if name.endswith('.py'):
                ast.parse(archive.read(name), filename=name, feature_version=(3, 8))
    # -I -S removes the checkout and all installed Python packages from imports.
    for option in ('--help', '--print-pipeline', '--version'):
        result = subprocess.run([sys.executable, '-I', '-S', str(bundle), option],
                                cwd=tmp_path, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert 'Traceback' not in result.stderr


DIAG = ROOT / 'deploy/rk_sender/rk_diag.sh'


def test_rk_diag_stays_read_only():
    # It runs on the robot's motion-control board: reading only, by construction.
    text = DIAG.read_text()
    assert not re.search(r'>>?\s*/(proc|sys|dev)/(?!null)', text)
    for word in ('chrt', 'taskset', 'renice', 'ionice', 'sysctl -w', 'systemctl stop', 'systemctl restart',
                 'kill', ' rm ', 'tee ',
                 # These open devices: the MPP plugin's init opens the encoder, a GStreamer
                 # registry rescan opens every /dev/video*, media-ctl opens each subdev.
                 'gst-inspect', 'gst-launch', 'gst-device-monitor', 'v4l2-ctl', 'media-ctl',
                 'debug/dma_buf',  # holds the global dma-buf lock while printing
                 '<<<'):  # bash 5.0 backs here-strings with temp files
        assert word not in text, word
    assert 'set -e' not in text  # one failing probe must not hide the rest


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='reads Linux /proc and /sys')
def test_rk_diag_writes_exactly_one_report(tmp_path):
    env = dict(os.environ, RK_DIAG_SUDO='0', RK_DIAG_SECONDS='1', RK_DIAG_DIR=str(tmp_path))
    done = subprocess.run(['bash', str(DIAG), 'test'], env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    (report,) = tmp_path.iterdir()
    assert report.name.startswith('rk-diag-') and report.name.endswith('-test.txt')
    body = report.read_text()
    for heading in ('real-time threads', 'interrupts per second', 'threads waiting for a CPU',
                    'open camera / encoder', 'kernel log'):
        assert heading in body, heading
    assert 'report: ' + str(report) in done.stderr
    bad = subprocess.run(['bash', str(DIAG), '../x'], env=env, capture_output=True, text=True, timeout=10)
    assert bad.returncode == 2 and len(list(tmp_path.iterdir())) == 1


AB = ROOT / 'deploy/rk_sender/rk_ab.sh'


def ab_env(tmp_path, libs=('base', 'fix', 'pkt'), **extra):
    fake = tmp_path / 'python'
    fake.write_text('#!/bin/sh\necho "[rk] args $* | GST_DEBUG=${GST_DEBUG:-} mpp_enc_debug=${mpp_enc_debug:-} '
                    'perror=${mpp_syslog_perror:-}"\n'
                    '[ "${*#*mpp-fix}" = "$*" ] || echo "[rk] ERROR: cannot load"\n')
    fake.chmod(0o755)
    for name in libs:
        (tmp_path / f'mpp-{name}').mkdir()
        (tmp_path / f'mpp-{name}' / 'librockchip_mpp.so.1').write_bytes(b'\x7fELF')
    base = {k: v for k, v in os.environ.items() if k not in ('GST_DEBUG', 'mpp_enc_debug', 'mpp_syslog_perror')}
    return dict(base, RK_PYTHON=str(fake), RK_SENDER_PYZ=str(tmp_path / 'rk_sender.pyz'),
                RK_AB_DIR=str(tmp_path / 'run'), RK_AB_PAUSE='0', **extra)


def ab_logs(tmp_path):
    (run_dir,) = (tmp_path / 'run').iterdir()
    return run_dir, {p.stem: p.read_text() for p in run_dir.glob('*.log')}


def ab_order(done):
    return [line.split()[3] for line in done.stdout.splitlines() if ' run ' in line and 'exit' not in line]


def test_ab_script_compares_staggered_and_aligned_idrs_by_default(tmp_path):
    env = ab_env(tmp_path)
    done = subprocess.run(['bash', str(AB), '192.168.168.10', '3'], env=env, capture_output=True, text=True,
                          timeout=30)
    assert done.returncode == 0, done.stderr
    run_dir, logs = ab_logs(tmp_path)
    assert sorted(logs) == ['aligned', 'aligned2', 'stagger', 'stagger2']  # the default runs
    for name, log in logs.items():
        assert f'{tmp_path}/rk_sender.pyz --host 192.168.168.10 ' in log and '--duration 3 ' in log
        for kind in ('stats', 'frames', 'trace'):
            assert f'{run_dir}/{name}.{kind}.jsonl' in log
        assert 'GST_DEBUG= mpp_enc_debug= ' in log  # no debug log
        # the fixed MPP named explicitly: a missing mpp-pkt/ must not fall back to the stalling one
        assert f'--mpp-lib {tmp_path}/mpp-pkt/librockchip_mpp.so.1' in log
        assert ('--idr-stagger off' in log) == name.startswith('aligned')
    assert ab_order(done) == ['stagger:', 'aligned:', 'stagger2:', 'aligned2:']
    for bad in ([], ['192.168.168.10', '1x'], ['192.168.168.10', '0'], ['192.168.168.10', '00']):
        assert subprocess.run(['bash', str(AB)] + bad, env=env, capture_output=True).returncode == 2


def test_ab_script_board_and_patched_mpp_runs_with_the_plugin_log(tmp_path):
    env = ab_env(tmp_path, RK_AB_RUNS='gst pkt gst pkt')
    done = subprocess.run(['bash', str(AB), '192.168.168.10', '3'], env=env, capture_output=True, text=True,
                          timeout=30)
    assert done.returncode == 0, done.stderr
    _, logs = ab_logs(tmp_path)
    assert sorted(logs) == ['gst', 'gst2', 'pkt', 'pkt2']
    for log in logs.values():
        assert 'GST_DEBUG=mppenc:5,mpph264enc:5 mpp_enc_debug= ' in log  # plugin log, no MPP log
        assert '--idr-stagger' not in log
    for name in ('gst', 'gst2'):  # the board's MPP even when mpp-pkt/ sits next to the pyz
        assert '--mpp-lib system' in logs[name]
    for name in ('pkt', 'pkt2'):
        assert f'--mpp-lib {tmp_path}/mpp-pkt/librockchip_mpp.so.1' in logs[name]
    assert ab_order(done) == ['gst:', 'pkt:', 'gst2:', 'pkt2:']


def test_ab_script_debug_and_control_runs(tmp_path):
    env = ab_env(tmp_path, RK_AB_RUNS='mpp fix base')
    done = subprocess.run(['bash', str(AB), '192.168.168.10', '3'], env=env, capture_output=True, text=True,
                          timeout=30)
    assert done.returncode == 0, done.stderr
    _, logs = ab_logs(tmp_path)
    assert sorted(logs) == ['base', 'fix', 'mpp']
    assert 'mpp_enc_debug=0x90 perror=1' in logs['mpp'] and '--mpp-lib system' in logs['mpp']
    assert 'mpp_enc_debug= ' in logs['fix'] + logs['base']
    assert f'--mpp-lib {tmp_path}/mpp-fix/librockchip_mpp.so.1' in logs['fix']
    assert f'--mpp-lib {tmp_path}/mpp-base/librockchip_mpp.so.1' in logs['base']
    assert 'ERROR: cannot load' in done.stdout  # a failing run is shown, and the rest still run
    assert done.stdout.index('run mpp') < done.stdout.index('run fix') < done.stdout.index('run base')


def test_ab_script_skips_missing_libraries_and_numbers_repeated_runs(tmp_path):
    env = ab_env(tmp_path, libs=('fix',), RK_AB_RUNS='fix sys base fix sys stagger bogus')
    done = subprocess.run(['bash', str(AB), '192.168.168.10', '2'], env=env, capture_output=True, text=True,
                          timeout=30)
    assert done.returncode == 0, done.stderr
    _, logs = ab_logs(tmp_path)
    assert sorted(logs) == ['fix', 'fix2', 'sys', 'sys2']
    assert 'skip base:' in done.stdout and 'skip unknown run: bogus' in done.stdout
    assert 'skip stagger:' in done.stdout  # without mpp-pkt/ it would not test what it says
    assert f'--mpp-lib {tmp_path}/mpp-fix/' in logs['fix2'] and '--mpp-lib system' in logs['sys2']
    assert 'GST_DEBUG= mpp_enc_debug= ' in logs['sys'] + logs['sys2']  # sys runs without any debug log


def test_real_h264_udp_two_streams_decode_in_unmodified_desktop_receiver(tmp_path):
    av = pytest.importorskip('av')
    receiver_path = tmp_path / 'receiver'
    receiver = subprocess.Popen([
        sys.executable, str(ROOT / 'demo.py'), 'receive', '--bind', '127.0.0.1', '--port', '0',
        '--streams', '2', '--fps', '30', '--width', '160', '--height', '96', '--decoder', 'software',
        '--clock-mode', 'estimated', '--sync-mode', 'latest', '--output', str(receiver_path),
    ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    tx = None
    try:
        ready = receiver_path / 'ready.json'
        deadline = time.monotonic() + 10
        while not ready.exists() and receiver.poll() is None and time.monotonic() < deadline:
            time.sleep(.03)
        if not ready.exists():
            if receiver.poll() is not None:
                log, _ = receiver.communicate()
                pytest.fail(log)
            pytest.fail('Receiver did not become ready')
        port = json.loads(ready.read_text())['port']
        tx = RtpOutput(('127.0.0.1', port), ['RK A', 'RK B'])
        tx.start()
        encoders = []
        for _ in range(2):
            enc = av.CodecContext.create('libx264', 'w')
            enc.width, enc.height, enc.pix_fmt = 160, 96, 'yuv420p'
            enc.time_base = Fraction(1, 30)
            enc.framerate = Fraction(30, 1)
            enc.max_b_frames, enc.gop_size = 0, 15
            enc.options = {'preset': 'ultrafast', 'tune': 'zerolatency',
                           'x264-params': 'repeat-headers=1:scenecut=0:bframes=0'}
            enc.open()
            encoders.append(enc)
        for index in range(65):
            for stream, enc in enumerate(encoders):
                captured = time.perf_counter_ns()
                frame = av.VideoFrame(160, 96, 'yuv420p')
                for plane in frame.planes:
                    plane.update(bytes([80 + stream * 40]) * plane.buffer_size)
                frame.pts = index
                if tx.key_requests[stream].is_set():
                    tx.key_requests[stream].clear()
                    frame.pict_type = av.video.frame.PictureType.I
                packets = enc.encode(frame)
                assert packets
                for packet in packets:
                    assert tx.submit(stream, bytes(packet), captured)
            time.sleep(1 / 30)
        time.sleep(.1)
        (receiver_path / 'STOP').touch()
        log, _ = receiver.communicate(timeout=10)
        assert receiver.returncode == 0, log
        summary = json.loads((receiver_path / 'summary.json').read_text())
        assert not summary['errors']
        for stream in summary['streams'].values():
            assert stream['events']['decode'] >= 40
            assert stream['decode_latency_ms']['samples'] >= 20
        assert summary['telemetry_lost_events'] == 0
    finally:
        if tx:
            tx.close()
        if receiver.poll() is None:
            receiver.terminate()
            receiver.communicate(timeout=10)
