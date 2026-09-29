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
