from dataclasses import replace
import random
import struct

import pytest

from video_demo.protocol import (Assembler, Meta, Packetizer, ReceptionReport, START,
                                 join_payloads, nals, parse_packet, parse_pli, pli)
from video_demo.protocol import nack, parse_nack


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
    assert a.add(parse_packet(data[-1]), 13_000_000) is None


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
