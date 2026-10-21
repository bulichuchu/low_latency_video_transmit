from dataclasses import replace
import random
import struct

import pytest

from video_demo.protocol import (Assembler, Meta, Packetizer, ReceptionReport, START,
                                 join_payloads, nals, parse_packet, parse_pli, pli)
from video_demo.protocol import nack, parse_nack, reassembly_windows_ms, MAX_STALL_NS, STALL_NS


def sample():
    return START + b'\x67\x42\x01\x20' + START + b'\x68\xce\x32' + START + b'\x65' + bytes(range(1, 255)) * 20


def test_fragment_reorder_duplicates_and_sequence_wrap():
    p = Packetizer(ssrc=123, mtu=400)
    p.sequence = 65533
    data = p.packetize(sample(), Meta(0, 90, 22, 123456789))
    assert all(len(d) <= 400 for d in data)
    assert parse_packet(data[3]).sequence == 0
    packets = [parse_packet(d) for d in data]
    random.Random(1).shuffle(packets)
    packets.insert(2, packets[0])
    assembler = Assembler()
    results = [u for packet in packets if (u := assembler.add(packet, 1_000_000))]
    assert len(results) == 1
    assert results[0].bitstream == sample()
    assert assembler.add(packets[-1], 2_000_000) is None


def test_lost_fragment_times_out_instead_of_decoding_partial_frame():
    data = Packetizer(mtu=400).packetize(sample(), Meta(1, 7, 4, 500))
    a = Assembler(timeout_ms=10)
    for raw in data[:-1]:
        assert a.add(parse_packet(raw), 1_000_000) is None
    expired = a.expire(12_000_000)
    assert [m.frame_id for m in expired] == [4]
    # Its last fragment after all, with nothing later complete: the whole frame.
    unit = a.add(parse_packet(data[-1]), 13_000_000)
    assert unit.bitstream == sample() and a.revived == 1
    assert a.add(parse_packet(data[-1]), 14_000_000) is None


def test_metadata_inconsistency_is_rejected():
    data = Packetizer(mtu=400).packetize(sample(), Meta(0, 8, 4, 500))
    a = Assembler()
    a.add(parse_packet(data[0]), 100)
    p = parse_packet(data[1])
    p.meta = replace(p.meta, capture_ns=501)
    with pytest.raises(ValueError, match='inconsistent'):
        a.add(p, 101)


def test_sensor_timestamp_extension_survives_fragmentation_and_v1_still_decodes():
    meta = Meta(0, 7, 30, 900_000_000, sensor_capture_ns=870_000_000,
                sdk_device_timestamp_us=777, sdk_global_timestamp_us=1_790_000_000_000_000,
                sensor_status='ready', sensor_clock_uncertainty_us=3)
    raw = Packetizer(mtu=256).packetize(sample(), meta)
    assert all(len(p) <= 256 for p in raw)
    assembler = Assembler()
    unit = None
    for p in reversed(raw):
        unit = assembler.add(parse_packet(p), 1) or unit
    assert unit.bitstream == sample()
    assert unit.meta == replace(meta, key=True, frame_bytes=len(sample()))
    old = parse_packet(Packetizer().packetize(START + b'\x65abc', Meta(0, 1, 0, 999))[0])
    assert old.meta.sensor_status == 'unavailable' and old.meta.sensor_capture_ns == 0
    with pytest.raises(ValueError):
        parse_packet(raw[0][:70])
    bad = bytearray(raw[0]); bad[16] = 1
    with pytest.raises(ValueError):
        parse_packet(bad)


def test_sensor_timestamp_status_cannot_disguise_future_or_fallback_time():
    meta = Meta(0, 1, 0, 100, sensor_capture_ns=101, sensor_status='ready',
                sdk_device_timestamp_us=10, sdk_global_timestamp_us=20)
    for invalid in (meta, replace(meta, sensor_status='unsupported'), replace(meta, sdk_global_timestamp_us=0)):
        raw = Packetizer().packetize(START + b'\x65abc', invalid)[0]
        with pytest.raises(ValueError, match='sensor timestamp'):
            parse_packet(raw)


def test_reassembly_memory_is_bounded():
    a = Assembler(max_pending=2)
    p = Packetizer(mtu=400)
    for fid in (1, 2):
        a.add(parse_packet(p.packetize(sample(), Meta(0, 1, fid, 500))[0]), 1)
    with pytest.raises(ValueError, match='capacity'):
        a.add(parse_packet(p.packetize(sample(), Meta(0, 1, 3, 500))[0]), 1)


@pytest.mark.parametrize('raw', [b'', b'\x00' * 100, b'\x90\x60' + b'\x00' * 100])
def test_malformed_rtp(raw):
    with pytest.raises(ValueError):
        parse_packet(raw)


def test_fua_missing_start_not_concealed():
    with pytest.raises(ValueError, match='start'):
        join_payloads([b'\x7c\x05abc'])


def test_rtcp_pli_and_receiver_report_wrap_and_loss():
    assert parse_pli(pli(13, 44)) == 44
    assert parse_pli(b'fake') is None
    data = Packetizer().packetize(START + b'\x65abcd', Meta(0, 3, 1, 123456789))
    p = parse_packet(data[0])
    report = ReceptionReport()
    for seq in (65534, 65535, 1, 1):  # seq=0 lost; one duplicate
        p.sequence = seq
        report.observe(p, 123456789)
    raw = report.packet(10, p.ssrc)
    assert raw[:4] == b'\x81\xc9\x00\x07'
    assert raw[12] == 64  # 1/4 lost
    assert int.from_bytes(raw[13:16], 'big') == 1
    assert struct.unpack_from('!I', raw, 16)[0] == 65537


def test_avcc_to_annexb():
    units = [b'\x67abc', b'\x65data']
    data = b''.join(len(n).to_bytes(4, 'big') + n for n in units)
    assert nals(data) == units


def test_generic_nack_and_bounded_retries():
    p = Packetizer(mtu=400)
    p.sequence = 65535
    packets = p.packetize(sample(), Meta(0, 4, 1, 100))
    a = Assembler(timeout_ms=20)
    for i, data in enumerate(packets):
        if i != 1:
            a.add(parse_packet(data), 1_000_000)
    request = a.missing(6_000_000)
    assert len(request) == 1 and request[0][1] == [0]
    raw = nack(123, p.ssrc, request[0][1])
    assert parse_nack(raw) == (p.ssrc, [0])
    assert a.missing(7_000_000) == []
    assert len(a.missing(11_000_000)) == 1
    assert a.missing(16_000_000) == []
    recovered = a.add(parse_packet(packets[1]), 17_000_000)
    assert recovered.bitstream == sample()
    assert not a.pending and not a.retries


def frame_packets(frame_id, fragments, key=True, epoch=4, packetizer=None):
    """One encoded frame split into `fragments` RTP packets (mtu 400: 350-byte FU-A chunks)."""
    p = packetizer or Packetizer(ssrc=77, mtu=400)
    nal = (b'\x65' if key else b'\x41') + bytes(range(1, 251)) * (fragments * 350 // 250)
    packets = [parse_packet(d) for d in p.packetize(START + nal, Meta(0, epoch, frame_id, 100))]
    assert len(packets) == fragments
    return packets


def requested(requests, packets):
    """Fragment indices a list of NACK requests asks for."""
    by_sequence = {p.sequence: p.index for p in packets}
    return [[by_sequence[s] for s in sequences] for _, sequences in requests]


def test_nack_does_not_request_the_tail_of_a_large_frame_still_arriving():
    # A 300 KB hardware IDR takes tens of ms to arrive; not-yet-received is not lost.
    packets = frame_packets(1, 30)
    a = Assembler(timeout_ms=20, key_timeout_ms=50)
    t = 1_000_000
    for packet in packets[:20]:
        a.add(packet, t)
        t += 500_000
        assert a.missing(t) == []
    last = t - 500_000
    assert a.missing(last + 5_900_000) == []  # brief pause in the burst
    request = a.missing(last + 6_000_000)    # stalled: the tail is lost
    assert requested(request, packets) == [list(range(20, 30))]
    assert a.missing(last + 9_000_000) == []  # rounds at least 4 ms apart
    assert requested(a.missing(last + 10_000_000), packets) == [list(range(20, 30))]
    assert a.missing(last + 14_000_000) == []  # each fragment at most twice
    for packet in packets[20:]:
        unit = a.add(packet, last + 15_000_000)
    assert unit is not None and not a.pending and not a.retries


def test_nack_requests_holes_below_the_highest_fragment_without_waiting_for_the_tail():
    packets = frame_packets(1, 30)
    a = Assembler(timeout_ms=20, key_timeout_ms=50)
    for i, packet in enumerate(packets[:13]):
        if i not in (4, 7):
            a.add(packet, 1_000_000 + i * 100_000)
    assert a.missing(4_900_000) == []  # first NACK no earlier than 4 ms after the first packet
    assert requested(a.missing(5_000_000), packets) == [[4, 7]]
    a.add(packets[4], 6_000_000)
    for packet in packets[13:16]:
        a.add(packet, 6_500_000)
    # Round two: the remaining hole once more; the tail (16..29) is still arriving.
    assert requested(a.missing(9_000_000), packets) == [[7]]
    assert a.missing(12_900_000) == []  # rounds at least 4 ms apart
    # No packet for over 6 ms: the tail counts as lost; hole 7 is not asked a third time.
    assert requested(a.missing(13_000_000), packets) == [list(range(16, 30))]


def test_nack_tail_needs_a_stall_not_just_time_since_the_first_packet():
    packets = frame_packets(1, 30)
    a = Assembler(timeout_ms=20, key_timeout_ms=50)
    for i, packet in enumerate(packets[:16]):
        a.add(packet, 1_000_000 + i * 600_000)  # steady arrival, last at 10 ms
    assert a.missing(15_900_000) == []
    assert requested(a.missing(16_000_000), packets) == [list(range(16, 30))]


def test_nack_ignores_a_later_frame_beginning_because_senders_interleave_frames():
    # The desktop sender interleaves packets of several frames when pacing, so
    # the next frame starting says nothing about the previous frame's tail.
    sender = Packetizer(ssrc=77, mtu=400)
    first = frame_packets(1, 10, key=False, packetizer=sender)
    second = frame_packets(2, 3, key=False, packetizer=sender)
    a = Assembler(timeout_ms=20)
    for packet in first[:6]:
        a.add(packet, 1_000_000)
    a.add(second[0], 3_000_000)
    assert a.missing(6_900_000) == []
    assert requested(a.missing(7_000_000), first) == [[6, 7, 8, 9]]  # 6 ms without progress


def test_nack_retransmitted_hole_does_not_postpone_the_tail_request():
    packets = frame_packets(1, 6, key=False)
    a = Assembler(timeout_ms=20)
    for i in (0, 1, 3, 4):  # 2 lost in the middle, 5 lost at the end
        a.add(packets[i], 1_000_000)
    assert requested(a.missing(5_000_000), packets) == [[2]]
    a.add(packets[2], 5_300_000)  # the retransmission is not progress of the original send
    assert requested(a.missing(9_000_000), packets) == [[5]]
    assert a.add(packets[5], 9_300_000) is not None


def test_short_reassembly_window_still_requests_a_lost_tail():
    packets = frame_packets(1, 3, key=False)
    a = Assembler(timeout_ms=8)
    for packet in packets[:2]:
        a.add(packet, 1_000_000)
    # 6 ms would leave no room before the window closes; the stall shrinks to 3 ms.
    assert requested(a.missing(5_000_000), packets) == [[2]]


def test_windows_and_nack_timing_follow_the_round_trip():
    # LAN (4-8 ms) keeps 20 / 50 ms; NetBird over the internet (30-50 ms) widens them
    # so a lost packet can be asked for and resent before the frame is given up.
    assert reassembly_windows_ms(20, None) == reassembly_windows_ms(20, 4.5) == (20, 50)
    assert reassembly_windows_ms(20, 35) == (80, 135)
    assert reassembly_windows_ms(20, 400) == (150, 200)
    assert reassembly_windows_ms(60, 0) == (60, 60)
    packets = frame_packets(1, 6)
    a = Assembler(timeout_ms=20, key_timeout_ms=50)
    a.tune(*reassembly_windows_ms(20, 40), 40)  # IDR 150 ms
    for i in (0, 1, 3, 4, 5):
        a.add(packets[i], 1_000_000)
    assert a.missing(10_900_000) == []  # a quarter round trip: often only reordering
    assert requested(a.missing(11_000_000), packets) == [[2]]
    assert a.missing(58_000_000) == []  # the retransmission needs a round trip
    assert requested(a.missing(59_000_000), packets) == [[2]]
    p_frame = frame_packets(2, 6, key=False)
    b = Assembler(timeout_ms=20)
    b.tune(*reassembly_windows_ms(20, 40), 40)  # P 90 ms: one round fits
    for i in (0, 1, 3, 4, 5):
        b.add(p_frame[i], 1_000_000)
    assert requested(b.missing(11_000_000), p_frame) == [[2]]
    assert b.missing(59_000_000) == []  # less than a round trip left in the window
    assert b.expire(80_000_000) == [] and b.assembling(77, 4, 2)  # still open at 80 ms (LAN: 20 ms)
    assert b.add(p_frame[2], 80_000_000) is not None
    # A paced IDR arrives over ~36 ms: a hole that shows up after a request round
    # is asked for at once, not a round trip later; the tail waits 10 ms of silence.
    idr = frame_packets(3, 30)
    c = Assembler(timeout_ms=20, key_timeout_ms=50)
    c.tune(*reassembly_windows_ms(20, 40), 40)
    for i in range(12):
        if i != 3:
            c.add(idr[i], 1_000_000 + i * 1_200_000)
    assert requested(c.missing(14_000_000), idr) == [[3]]
    for i in range(12, 20):
        if i != 15:
            c.add(idr[i], 1_000_000 + i * 1_200_000)
    assert requested(c.missing(23_000_000), idr) == [[15]]  # new hole: 9 ms after the last round
    last = 1_000_000 + 19 * 1_200_000
    assert c.missing(last + 9_900_000) == []
    assert requested(c.missing(last + 10_000_000), idr) == [list(range(20, 30))]
    assert c.assembling_key(77, 4, 3, 4) and not c.assembling_key(77, 4, 0, 3)
    assert not c.assembling_key(77, 5, 0, 9) and not c.assembling_key(78, 4, 0, 9)


def test_a_frame_still_arriving_on_a_narrow_link_is_not_given_up():
    # 10-21 emulation: at 1.5 Mbps a 50 KB IDR takes 270 ms to arrive; the IDR window
    # (at most 200 ms) used to close on it every time, so the picture never came back.
    idr = frame_packets(1, 40)
    a = Assembler(timeout_ms=20, key_timeout_ms=50)
    a.tune(*reassembly_windows_ms(20, 400), 400)  # 150 / 200 ms
    t = 1_000_000
    for i, packet in enumerate(idr[:-1]):
        if i != 30:
            a.add(packet, t)
        t += 7_000_000  # one fragment every 7 ms: 273 ms in all
        assert a.expire(t) == []
    # The last fragment is lost too: the window runs from the last progress (while
    # the next frames keep coming: a silent stream is a stall, see the next test).
    last = t - 7_000_000
    later = frame_packets(2, 12, key=False)
    for i in range(7):
        a.add(later[i], last + i * 30_000_000)
    assert a.expire(last + 200_000_000) == [] and a.assembling(77, 4, 1)
    expired = a.expire(last + 201_000_000)
    assert [m.frame_id for m in expired] == [1]
    # The same frame with its two missing fragments arriving late completes.
    b = Assembler(timeout_ms=20, key_timeout_ms=50)
    b.tune(*reassembly_windows_ms(20, 400), 400)
    t = 1_000_000
    for i, packet in enumerate(idr[:-1]):
        if i != 30:
            b.add(packet, t)
        t += 7_000_000
    assert b.add(idr[30], t + 150_000_000) is None
    assert b.add(idr[-1], t + 160_000_000) is not None


def test_a_hole_in_a_slowly_arriving_frame_is_still_requested():
    # The cutoff for requests runs from the frame's last progress too: a hole found
    # 80 ms after the first packet of a 90 ms window still has time for a resend.
    packets = frame_packets(1, 12, key=False)
    a = Assembler(timeout_ms=20)
    a.tune(*reassembly_windows_ms(20, 40), 40)  # P 90 ms, round trip 40 ms
    for i in range(9):
        if i != 7:
            a.add(packets[i], 1_000_000 + i * 10_000_000)  # one fragment every 10 ms
    assert requested(a.missing(86_000_000), packets) == [[7]]
    assert a.expire(170_000_000) == []  # 90 ms from the last fragment (81 ms)
    assert [m.frame_id for m in a.expire(172_000_000)] == [1]


def test_frames_keep_their_window_while_the_link_stalls():
    # 10-21 on the robot's uplink: an IDR was arriving when the link stopped
    # for 1.4 s; its window ran out during the stall, its packets came right after it
    # and the 29 frames behind it were dropped waiting for the next IDR.
    idr = frame_packets(1, 30)
    a = Assembler(timeout_ms=20, key_timeout_ms=50)
    a.tune(*reassembly_windows_ms(20, 45), 45)  # IDR 165 ms
    for i in range(10):
        a.add(idr[i], 1_000_000 + i * 1_000_000)
    stalled = 10_000_000
    assert a.missing(stalled + 15_000_000)  # the tail is asked for (early in the stall)
    for t in range(20, 1500, 10):  # 1.5 s without any packet: nothing expires,
        assert a.expire(stalled + t * 1_000_000) == []
        a.missing(stalled + t * 1_000_000)  # and what was asked for is not judged lost
    resume = stalled + 1_500_000_000
    for i in range(10, 29):
        assert a.add(idr[i], resume + (i - 10) * 300_000) is None
    unit = a.add(idr[29], resume + 6_000_000)
    assert unit is not None and unit.meta.frame_id == 1 and a.revived == 0
    assert unit.first_rx_ns == 1_000_000  # when its first packet came, stall or not
    # What was asked for before a silence of over a second is the outage's: no loss.
    assert a.repaired == 0 and a.late == 0
    # After a shorter stall the requested fragments that come with the burst are not
    # lost either: their originals were only held up.
    short = Assembler(timeout_ms=20, key_timeout_ms=50)
    short.tune(*reassembly_windows_ms(20, 45), 45)
    for i in range(10):
        short.add(idr[i], 1_000_000 + i * 1_000_000)
    assert short.missing(25_000_000)
    for t in range(30, 600, 10):
        short.missing(t * 1_000_000)
    for i in range(10, 29):
        short.add(idr[i], 600_000_000 + (i - 10) * 300_000)
        short.missing(600_000_000 + (i - 10) * 300_000)
    assert short.add(idr[29], 606_000_000) is not None
    assert short.repaired == 0 and short.late == 20
    # A frame still missing a fragment after the stall gets the rest of its window
    # (here 65 ms of 165), measured while packets flow again.
    b = Assembler(timeout_ms=20, key_timeout_ms=50)
    b.tune(*reassembly_windows_ms(20, 45), 45)
    idr = frame_packets(2, 4)
    b.add(idr[0], 1_000_000)
    b.add(idr[1], 2_000_000)
    b.add(frame_packets(3, 2, key=False)[0], 1_002_000_000)  # 1 s later the stream resumes
    assert b.expire(1_066_000_000) == []
    assert [m.frame_id for m in b.expire(1_068_000_000)] == [2]


def test_a_frame_given_up_as_the_link_stalled_is_revived_when_its_packets_come():
    # A P frame's window (100 ms at a 45 ms round trip) ends in the first 100 ms of a
    # stall, before the clock stops: it expires, then its packets come after all.
    a = Assembler(timeout_ms=20, key_timeout_ms=50)
    a.tune(*reassembly_windows_ms(20, 45), 45)  # P 100 ms
    first = frame_packets(1, 4, key=False)
    second = frame_packets(2, 2, key=False)
    for i in range(3):
        a.add(first[i], 1_000_000 + i * 1_000_000)  # the last fragment is held up
    a.add(second[0], 30_000_000)
    assert a.expire(103_000_000) == []
    assert [m.frame_id for m in a.expire(104_000_000)] == [1]
    assert not a.assembling(77, 4, 1)
    for t in range(110, 1000, 10):  # the stall: the next frame keeps its window
        assert a.expire(t * 1_000_000) == []
    unit = a.add(first[3], 1_000_000_000)
    assert unit is not None and unit.meta.frame_id == 1 and a.revived == 1
    assert a.add(second[1], 1_001_000_000).meta.frame_id == 2
    # A revived frame still incomplete gets a whole window from then on.
    e = Assembler(timeout_ms=20, key_timeout_ms=50)
    e.tune(*reassembly_windows_ms(20, 45), 45)
    third = frame_packets(3, 5, key=False)
    for i in (0, 1, 3):
        e.add(third[i], 1_000_000 + i * 1_000_000)
    e.add(third[1], 30_000_000)  # (a duplicate: the stream's last packet before the stall)
    assert [m.frame_id for m in e.expire(105_000_000)] == [3]
    assert e.add(third[2], 1_000_000_000) is None and e.assembling(77, 4, 3)  # a hole: no progress
    assert e.expire(1_099_000_000) == [] and e.add(third[4], 1_099_000_000).meta.frame_id == 3
    # Not once a later frame has completed: the decoder has moved on to an IDR.
    b = Assembler(timeout_ms=20, key_timeout_ms=50)
    b.tune(*reassembly_windows_ms(20, 45), 45)
    for i in range(3):
        b.add(first[i], 1_000_000 + i * 1_000_000)
    b.add(second[0], 30_000_000)
    assert [m.frame_id for m in b.expire(104_000_000)] == [1]
    assert b.add(second[1], 1_000_000_000) is not None
    assert b.add(first[3], 1_001_000_000) is None and b.revived == 0
    assert not b.assembling(77, 4, 1)
    # Nor after REVIVE_NS, nor when a later frame had completed before it expired.
    c = Assembler(timeout_ms=20, key_timeout_ms=50)
    c.tune(*reassembly_windows_ms(20, 45), 45)
    for i in range(3):
        c.add(first[i], 1_000_000 + i * 1_000_000)
    c.add(second[0], 30_000_000)
    assert [m.frame_id for m in c.expire(104_000_000)] == [1]
    assert c.add(first[3], 3_105_000_000) is None and c.revived == 0
    d = Assembler(timeout_ms=20, key_timeout_ms=50)
    d.tune(*reassembly_windows_ms(20, 45), 45)
    for i in range(3):
        d.add(first[i], 1_000_000 + i * 1_000_000)
    d.add(second[0], 30_000_000)
    d.add(second[1], 31_000_000)
    assert [m.frame_id for m in d.expire(104_000_000)] == [1]
    assert d.add(first[3], 120_000_000) is None and d.revived == 0


def test_at_a_low_frame_rate_the_gaps_between_frames_are_no_stall():
    # At the RK's 10 fps a frame comes every 100 ms: a stall is three such intervals.
    a = Assembler(timeout_ms=20, key_timeout_ms=50)
    a.tune(*reassembly_windows_ms(20, 45), 45)
    t = 1_000_000
    for n in range(1, 7):
        for packet in frame_packets(n, 2, key=n == 1):
            assert a.add(packet, t) is not None or packet.index == 0
        t += 100_000_000 + (n % 2) * 30_000_000  # 100-130 ms apart
    assert a.stall_ns == 3 * 130_000_000
    resumed = a.resumed  # (before four gaps were known: a stall at 100 ms)
    held = frame_packets(7, 3, key=False)
    a.add(held[0], t)
    a.add(held[1], t + 1_000_000)
    assert a.resumed == resumed  # 130 ms after the last frame: no stall
    assert [m.frame_id for m in a.expire(t + 1_000_000 + 101_000_000)] == [7]  # its 100 ms window ran
    fast = Assembler(timeout_ms=20, key_timeout_ms=50)
    t = 1_000_000
    for n in range(1, 30):  # 30 fps: the floor
        for packet in frame_packets(n, 2, key=n == 1):
            fast.add(packet, t)
        t += 33_000_000
    assert fast.stall_ns == STALL_NS and MAX_STALL_NS == 400_000_000


def test_an_expired_frame_needs_no_key_request_once_a_later_idr_completed():
    a = Assembler(timeout_ms=20, key_timeout_ms=50)
    a.tune(*reassembly_windows_ms(20, 45), 45)
    old = frame_packets(1, 3, key=False)
    a.add(old[0], 1_000_000)
    assert not a.key_after(old[0].meta)
    for packet in frame_packets(2, 2):
        a.add(packet, 2_000_000)
    assert a.key_after(old[0].meta) and not a.key_after(frame_packets(3, 2)[0].meta)
    # A duplicate of what an expired frame already has does not revive it.
    a.add(old[1], 3_000_000)
    b = Assembler(timeout_ms=20, key_timeout_ms=50)
    for packet in old[:2]:
        b.add(packet, 1_000_000)
    assert [m.frame_id for m in b.expire(30_000_000)] == [1]
    assert b.add(old[1], 31_000_000) is None and b.revived == 0 and not b.assembling(77, 4, 1)
    assert b.add(old[2], 32_000_000) is not None and b.revived == 1


def test_requested_fragments_count_as_repaired_only_when_a_resend_could_have_come():
    # 10-21: about 3/4 of the frames with a NACK (57-89 % per 100 s) were complete within
    # half a round trip of it, before any retransmission could arrive: reordering, not
    # loss. The sender's loss signal (pings' `resent`) counts only fragments still
    # missing 3/4 of a round trip after the first request.
    packets = frame_packets(1, 8, key=False)
    a = Assembler(timeout_ms=20)
    a.tune(*reassembly_windows_ms(20, 40), 40)  # first request 10 ms after the hole
    for i in (0, 1, 3, 5, 6, 7):
        a.add(packets[i], 1_000_000 + i * 100_000)
    assert requested(a.missing(12_000_000), packets) == [[2, 4]]
    a.add(packets[2], 20_000_000)  # 8 ms after the request: the original, late
    a.add(packets[4], 45_000_000)  # 33 ms (>= 3/4 of the 40 ms round trip): resent
    assert (a.late, a.repaired) == (1, 1)


def test_a_requested_fragment_counts_as_lost_as_soon_as_a_resend_was_needed():
    # In congestion the resend is often lost too; counting only resends that came
    # left the loss to the receiver report a second or two later, and the sender's
    # controller reacted to each burst late and only in part (emulated 6 Mbps link).
    packets = frame_packets(1, 8, key=False)
    a = Assembler(timeout_ms=20)
    a.tune(*reassembly_windows_ms(20, 40), 40)  # P 90 ms; a resend 30 ms after the request at the earliest
    for i in (0, 1, 3, 4, 5, 6, 7):
        a.add(packets[i], 1_000_000 + i * 100_000)
    assert requested(a.missing(12_000_000), packets) == [[2]]
    later = frame_packets(2, 3, key=False)
    a.add(later[0], 41_000_000)  # packets keep coming
    a.missing(45_000_000)
    assert a.repaired == 0  # the latest packet came 29 ms after the request
    a.add(later[1], 42_000_000)
    a.missing(42_000_000)
    assert a.repaired == 1 and a.late == 0  # one came 30 ms after it: lost, resent or not
    assert a.add(packets[2], 60_000_000) is not None and a.repaired == 1  # the resend: counted once
    # A requested fragment of a frame given up is judged as well, once packets flow.
    b = Assembler(timeout_ms=20)
    b.tune(*reassembly_windows_ms(20, 40), 40)
    for i in (0, 1, 3):
        b.add(packets[i], 1_000_000 + i * 100_000)
    assert requested(b.missing(12_000_000), packets) == [[2, 4, 5, 6, 7]]  # (the tail stopped too)
    b.add(later[0], 13_000_000)
    b.add(later[1], 13_100_000)  # (the stream's last packet for a while)
    assert [m.frame_id for m in b.expire(92_000_000)] == [1]
    b.missing(92_000_000)
    assert b.repaired == 0  # nothing has come for 79 ms: a stall, perhaps
    b.add(later[2], 95_000_000)
    b.missing(96_000_000)
    assert b.repaired == 5 and b.late == 0


def test_a_hole_asked_for_just_before_a_stall_is_not_judged_during_it():
    # Review: judged while packets seemed to flow (a packet within 3/4 RTT), a hole
    # asked for 20 ms before the stall counted as lost although it came after it.
    packets = frame_packets(1, 12, key=False)
    a = Assembler(timeout_ms=20)
    a.tune(*reassembly_windows_ms(20, 40), 40)  # a resend 30 ms after the request at the earliest
    for i, t in ((0, 1), (1, 1), (3, 1), (4, 5), (5, 11)):
        a.add(packets[i], t * 1_000_000)
    assert requested(a.missing(12_000_000), packets) == [[2]]
    for i, t in ((6, 20), (7, 26), (8, 32)):  # until 32 ms (20 ms after the request), then nothing
        a.add(packets[i], t * 1_000_000)
    for t in range(33, 300, 3):
        a.missing(t * 1_000_000)
    assert a.repaired == 0
    a.add(packets[2], 300_000_000)  # its original, with the burst after the stall
    assert (a.late, a.repaired) == (1, 0)


def test_receiver_report_settles_packets_that_never_came():
    data = Packetizer().packetize(START + b'\x65abcd', Meta(0, 3, 1, 123456789))
    p = parse_packet(data[0])
    report = ReceptionReport()
    s = 1_000_000_000

    def arrive(seq, t):
        p.sequence = seq
        report.observe(p, t)

    def settle(t):
        return report.settle(t, horizon_ns=1_000_000_000)

    for seq in (100, 101, 103, 105, 102):  # 102 reordered; 104 lost
        arrive(seq, s)
    assert settle(s) == 0  # nothing is old enough yet
    for i, seq in enumerate((106, 107, 110)):  # 108, 109 lost
        arrive(seq, s + 400_000_000 + i * 50_000_000)
    assert settle(s + 500_000_000) == 0
    assert settle(s + 1_000_000_000) == 1  # up to 105, a second old: 104 never came
    assert settle(s + 1_400_000_000) == 1
    assert settle(s + 1_500_000_000) == 3  # up to 110 (the highest at +0.5 s)
    # Numbers skipped across a stall (the link was down for 1.5 s): an outage, which
    # the round trips show, not loss; a gap right after it still is.
    arrive(150, s + 2_000_000_000)
    arrive(153, s + 2_050_000_000)
    assert settle(s + 2_100_000_000) == 3
    # A shorter silence is no outage (at 10 fps frames come 100 ms apart): loss.
    arrive(160, s + 2_350_000_000)
    assert settle(s + 3_100_000_000) == 5 and not report.outages
    assert settle(s + 3_500_000_000) == 5
    assert settle(s + 4_100_000_000) == 11
    # Numbers asked for again are the Assembler's to count (as soon as a resend was
    # needed): not here too. By default a number is judged half a second on.
    asked = ReceptionReport()
    for seq in (10, 11, 15):
        p.sequence = seq
        asked.observe(p, s)
    asked.asked([12, 13])
    assert asked.settle(s) == 0 and asked.settle(s + 500_000_000) == 1  # 14
    assert asked.unrepaired == 2 and not asked.requested  # 12, 13: asked for, never came
    # Across the 16-bit wrap.
    wrap = ReceptionReport()
    for seq in (65533, 65534, 2, 3):  # 65535, 0, 1 lost
        p.sequence = seq
        wrap.observe(p, s)
    wrap.asked([65535, 1])
    assert wrap.settle(s) == 0 and wrap.settle(s + 500_000_000) == 1 and wrap.unrepaired == 2


