"""RFC 3550 RTP, RFC 6184 single NAL/FU-A, RTCP PLI and receiver reports.

The private RTP extension 0x4c56 carries per-frame metadata. Ordinary H.264
RTP depayloaders may ignore it; this demo requires it for reassembly/metrics.
No pickle or platform-endian structures are accepted on the wire.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import re
import secrets
import struct
from .capture_time import TIMESTAMP_STATUSES

RTP = struct.Struct('!BBHII')
EXT = struct.Struct('!BBHIIQIIHH')
SENSOR_EXT = struct.Struct('!QQQII')
PROFILE = 0x4C56
HEADER_SIZE = RTP.size + 4 + EXT.size
MAX_FRAME_BYTES = 4 * 1024 * 1024
MAX_PACKETS = 4096
START = b'\x00\x00\x00\x01'


@dataclass(frozen=True)
class Meta:
    stream: int
    epoch: int
    frame_id: int
    capture_ns: int
    encode_us: int = 0  # app capture -> encoded on the sender (slot wait, conversion, codec)
    frame_bytes: int = 0
    key: bool = False
    depth_preview: bool = False
    sensor_capture_ns: int = 0
    sdk_device_timestamp_us: int = 0
    sdk_global_timestamp_us: int = 0
    sensor_status: str = 'unavailable'
    sensor_clock_uncertainty_us: int = 0


@dataclass
class Packet:
    meta: Meta
    ssrc: int
    sequence: int
    timestamp: int
    index: int
    count: int
    payload: bytes


@dataclass
class Unit:
    meta: Meta
    ssrc: int
    bitstream: bytes
    first_rx_ns: int
    complete_ns: int


def nals(data: bytes) -> list[bytes]:
    """Accept Annex B or four-byte-length AVCC (VideoToolbox output)."""
    if data.startswith((START, b'\x00\x00\x01')):
        result = [p for p in re.split(b'\x00\x00\x00\x01|\x00\x00\x01', data) if p]
    else:
        result, offset = [], 0
        while offset + 4 <= len(data):
            size = int.from_bytes(data[offset:offset + 4], 'big')
            offset += 4
            if not size or offset + size > len(data):
                raise ValueError('invalid AVCC NAL length')
            result.append(data[offset:offset + size])
            offset += size
        if offset != len(data):
            raise ValueError('truncated AVCC')
    if not result or any(not 1 <= (n[0] & 31) <= 23 or n[0] & 128 for n in result):
        raise ValueError('invalid H.264 NAL')
    return result


def is_idr(data: bytes) -> bool:
    return any(n[0] & 31 == 5 for n in nals(data))


class Packetizer:
    def __init__(self, ssrc: int | None = None, mtu: int = 1200):
        if not 256 <= mtu <= 1400:
            raise ValueError('RTP packet size must be 256..1400 bytes')
        self.ssrc = ssrc if ssrc is not None else secrets.randbits(32)
        self.sequence = secrets.randbits(16)
        self.timestamp_offset = secrets.randbits(32)
        self.mtu = mtu

    def packetize(self, bitstream: bytes, meta: Meta) -> list[bytes]:
        units = nals(bitstream)
        size = sum(len(n) + 4 for n in units)
        if size > MAX_FRAME_BYTES:
            raise ValueError('encoded frame exceeds size limit')
        meta = replace(meta, frame_bytes=size, key=any(n[0] & 31 == 5 for n in units))
        sensor = meta.sensor_status != 'unavailable'
        if meta.sensor_status not in TIMESTAMP_STATUSES:
            raise ValueError('invalid sensor timestamp status')
        extra = (SENSOR_EXT.pack(meta.sensor_capture_ns, meta.sdk_device_timestamp_us, meta.sdk_global_timestamp_us,
                    TIMESTAMP_STATUSES.index(meta.sensor_status), meta.sensor_clock_uncertainty_us) if sensor else b'')
        limit = self.mtu - HEADER_SIZE - len(extra)
        payloads = []
        for nal in units:
            if len(nal) <= limit:
                payloads.append(nal)
                continue
            chunks = [nal[i:i + limit - 2] for i in range(1, len(nal), limit - 2)]
            for i, chunk in enumerate(chunks):
                flags = (0x80 if i == 0 else 0) | (0x40 if i == len(chunks) - 1 else 0)
                payloads.append(bytes([(nal[0] & 0xE0) | 28, flags | (nal[0] & 31)]) + chunk)
        if len(payloads) > MAX_PACKETS:
            raise ValueError('too many fragments')
        packets = []
        timestamp = (meta.capture_ns * 90000 // 1_000_000_000 + self.timestamp_offset) & 0xFFFFFFFF
        for index, payload in enumerate(payloads):
            marker = 0x80 if index == len(payloads) - 1 else 0
            header = RTP.pack(0x90, 96 | marker, self.sequence, timestamp, self.ssrc)
            ext = EXT.pack(2 if sensor else 1, int(meta.key) | (int(meta.depth_preview) << 1), meta.stream, meta.epoch, meta.frame_id,
                           meta.capture_ns, meta.encode_us, size, index, len(payloads))
            packets.append(header + struct.pack('!HH', PROFILE, (EXT.size + len(extra)) // 4) + ext + extra + payload)
            self.sequence = (self.sequence + 1) & 0xFFFF
        return packets


def parse_packet(data: bytes) -> Packet:
    if len(data) <= HEADER_SIZE:
        raise ValueError('short RTP packet')
    first, second, seq, timestamp, ssrc = RTP.unpack_from(data)
    if first != 0x90 or second & 127 != 96:
        raise ValueError('unsupported RTP header')
    profile, words = struct.unpack_from('!HH', data, RTP.size)
    if profile != PROFILE or words * 4 not in (EXT.size, EXT.size + SENSOR_EXT.size):
        raise ValueError('missing demo timing extension')
    version, flags, stream, epoch, fid, cap, enc, size, index, count = EXT.unpack_from(data, 16)
    if (version not in (1, 2) or words * 4 != EXT.size + (SENSOR_EXT.size if version == 2 else 0)
            or flags & ~3 or not 0 <= index < count <= MAX_PACKETS):
        raise ValueError('invalid frame extension')
    if not 0 < size <= MAX_FRAME_BYTES or not cap or stream >= 16:
        raise ValueError('invalid stream/frame size')
    if bool(second & 128) != (index == count - 1):
        raise ValueError('invalid RTP marker')
    header_size = RTP.size + 4 + words * 4
    if len(data) <= header_size:
        raise ValueError('short RTP timestamp extension')
    meta = Meta(stream, epoch, fid, cap, enc, size, bool(flags & 1), bool(flags & 2))
    if version == 2:
        sensor, device, global_us, status, uncertainty = SENSOR_EXT.unpack_from(data, HEADER_SIZE)
        if (status >= len(TIMESTAMP_STATUSES) or (status == 2 and
                (not sensor or not device or not global_us or sensor > cap)) or (status != 2 and sensor)):
            raise ValueError('invalid sensor timestamp extension')
        meta = replace(meta, sensor_capture_ns=sensor, sdk_device_timestamp_us=device,
                       sdk_global_timestamp_us=global_us, sensor_status=TIMESTAMP_STATUSES[status],
                       sensor_clock_uncertainty_us=uncertainty)
    return Packet(meta, ssrc, seq, timestamp, index, count, data[header_size:])


def join_payloads(payloads: list[bytes]) -> bytes:
    output, fragmented = bytearray(), None
    for payload in payloads:
        if not payload:
            raise ValueError('empty NAL')
        kind = payload[0] & 31
        if 1 <= kind <= 23:
            if fragmented is not None:
                raise ValueError('unfinished FU-A')
            output.extend(START + payload)
        elif kind == 28 and len(payload) > 2:
            indicator, header = payload[:2]
            nal_type = header & 31
            if not 1 <= nal_type <= 23 or header & 32 or header & 192 == 192:
                raise ValueError('invalid FU-A header')
            if header & 128:
                if fragmented is not None:
                    raise ValueError('nested FU-A')
                fragmented = (indicator & 0xE0) | nal_type
                output.extend(START + bytes([fragmented]))
            elif fragmented != ((indicator & 0xE0) | nal_type):
                raise ValueError('missing/mismatched FU-A start')
            output.extend(payload[2:])
            if header & 64:
                fragmented = None
        else:
            raise ValueError('unsupported RTP NAL type')
    if fragmented is not None:
        raise ValueError('missing FU-A end')
    return bytes(output)


NACK_SETTLE_NS = 4_000_000    # a frame's first NACK, after its first packet
NACK_INTERVAL_NS = 4_000_000  # between NACK rounds for one frame
NACK_TAIL_STALL_NS = 6_000_000  # a frame's transmission made no progress this long: tail lost
NACK_TRIES = 2                # per fragment


class Assembler:
    def __init__(self, timeout_ms: float = 20, max_pending: int = 8, *, key_timeout_ms: float | None = None):
        self.timeout_ns = int(timeout_ms * 1e6)
        self.key_timeout_ns = max(self.timeout_ns, int(key_timeout_ms * 1e6)) if key_timeout_ms is not None else self.timeout_ns
        self.max_pending = max_pending
        # key -> [first packet, first rx, chunks, bytes, highest fragment, when it last advanced]
        self.pending = {}
        self.finished = {}  # bounded duplicate suppression
        self.retries = {}  # key -> (last NACK time, {fragment index: requests})

    def add(self, p: Packet, now: int) -> Unit | None:
        key = (p.ssrc, p.meta.epoch, p.meta.frame_id)
        if key in self.finished:
            return None
        if key not in self.pending:
            if len(self.pending) >= self.max_pending:
                raise ValueError('assembly capacity exceeded')
            self.pending[key] = [p, now, {}, 0, -1, now]
        entry = self.pending[key]
        first, started, chunks, total = entry[:4]
        if p.meta != first.meta or p.count != first.count or p.timestamp != first.timestamp:
            raise ValueError('inconsistent frame metadata')
        if p.index in chunks:
            if chunks[p.index] != p.payload:
                raise ValueError('conflicting duplicate fragment')
            return None
        total += len(p.payload)
        if total > MAX_FRAME_BYTES + MAX_PACKETS * 2:
            del self.pending[key]
            raise ValueError('assembly byte limit exceeded')
        chunks[p.index] = p.payload
        entry[3] = total
        if p.index > entry[4]:
            # Progress of the original transmission. A retransmission filling
            # a hole below it is not, so it does not postpone the tail check.
            entry[4], entry[5] = p.index, now
        if len(chunks) != p.count:
            return None
        del self.pending[key]
        self.retries.pop(key, None)
        self.finished[key] = now
        if len(self.finished) > 64:
            del self.finished[next(iter(self.finished))]
        data = join_payloads([chunks[i] for i in range(p.count)])
        if len(data) != p.meta.frame_bytes or is_idr(data) != p.meta.key:
            raise ValueError('encoded frame length/IDR mismatch')
        return Unit(p.meta, p.ssrc, data, started, now)

    def missing(self, now: int) -> list[tuple[Packet, list[int]]]:
        """Early NACKs for fragments that are evidently lost, within the reassembly budget.

        A large frame (a hardware IDR is often 200+ fragments) is still arriving
        long after its first packet, so not-yet-received is not lost. Request
        holes below the highest fragment received; request the tail only after
        the frame's transmission stopped advancing (6 ms, less for short
        reassembly windows). A later frame starting is no evidence: senders
        may interleave frames. Each fragment is requested at most twice,
        rounds at least 4 ms apart.
        """
        requests = []
        for key, (packet, started, chunks, _, highest, advanced) in self.pending.items():
            if now - started < NACK_SETTLE_NS:
                continue
            last, tries = self.retries.get(key, (None, {}))
            if last is not None and now - last < NACK_INTERVAL_NS:
                continue
            timeout = self.key_timeout_ns if packet.meta.key else self.timeout_ns
            if now - started >= timeout - 2_000_000:
                continue
            # Leave room for a request and its retransmission before the window closes.
            stall = min(NACK_TAIL_STALL_NS, max(1_000_000, (timeout - 2_000_000) // 2))
            if now - advanced >= stall:
                end = packet.count  # Transmission stopped: the tail is lost too.
            elif len(chunks) <= highest:
                end = highest  # Holes below the highest fragment received.
            else:
                continue  # A contiguous prefix still arriving: called per packet, keep it cheap.
            wanted = [i for i in range(end) if i not in chunks and tries.get(i, 0) < NACK_TRIES][:64]
            if not wanted:
                continue
            tries = dict(tries)
            for i in wanted:
                tries[i] = tries.get(i, 0) + 1
            self.retries[key] = (now, tries)
            base = (packet.sequence - packet.index) & 0xFFFF
            requests.append((packet, [(base + i) & 0xFFFF for i in wanted]))
        return requests

    def expire(self, now: int) -> list[Meta]:
        expired = [k for k, value in self.pending.items()
                   if now - value[1] > (self.key_timeout_ns if value[0].meta.key else self.timeout_ns)]
        result = []
        for key in expired:
            p = self.pending.pop(key)[0]
            self.retries.pop(key, None)
            result.append(p.meta)
            self.finished[key] = now
        while len(self.finished) > 64:
            del self.finished[next(iter(self.finished))]
        return result


def pli(sender_ssrc: int, media_ssrc: int) -> bytes:
    return struct.pack('!BBHII', 0x81, 206, 2, sender_ssrc, media_ssrc)


def parse_pli(data: bytes) -> int | None:
    if len(data) == 12 and data[:4] == b'\x81\xce\x00\x02':
        return struct.unpack_from('!I', data, 8)[0]
    return None


def nack(sender_ssrc: int, media_ssrc: int, sequences: list[int]) -> bytes:
    """RFC 4585 Generic NACK; one PID per entry, BLP=0."""
    if not 1 <= len(sequences) <= 64:
        raise ValueError('NACK supports 1..64 sequence numbers')
    return (struct.pack('!BBHII', 0x81, 205, 2 + len(sequences), sender_ssrc, media_ssrc)
            + b''.join(struct.pack('!HH', seq & 65535, 0) for seq in sequences))


def parse_nack(data: bytes) -> tuple[int, list[int]] | None:
    if len(data) < 16 or data[:2] != b'\x81\xcd' or len(data) > 268 or len(data) % 4:
        return None
    if (int.from_bytes(data[2:4], 'big') + 1) * 4 != len(data):
        return None
    media_ssrc = struct.unpack_from('!I', data, 8)[0]
    sequences = []
    for offset in range(12, len(data), 4):
        pid, mask = struct.unpack_from('!HH', data, offset)
        sequences.append(pid)
        sequences.extend((pid + bit + 1) & 65535 for bit in range(16) if mask & (1 << bit))
    return media_ssrc, list(dict.fromkeys(sequences))[:64]


class ReceptionReport:
    """RTP sequence statistics with wrap handling and duplicate suppression."""
    def __init__(self):
        self.first = self.highest = None
        self.received = 0
        self.seen = set()
        self.prev_expected = self.prev_received = 0
        self.transit = None
        self.jitter = 0.0

    def observe(self, p: Packet, now: int):
        seq = p.sequence
        if self.highest is None:
            self.first = self.highest = seq
        else:
            base = self.highest & ~0xFFFF
            seq = min((base + seq - 65536, base + seq, base + seq + 65536),
                      key=lambda n: abs(n - self.highest))
            self.highest = max(self.highest, seq)
        if seq not in self.seen:
            self.received += 1
            self.seen.add(seq)
        if len(self.seen) > 8192:
            self.seen = {n for n in self.seen if n > self.highest - 4096}
        # Subtract RTP timestamps modulo 2^32 before applying arrival delta.
        stamp = (now * 90000 // 1_000_000_000 - p.timestamp) & 0xFFFFFFFF
        if self.transit is not None:
            delta = ((stamp - self.transit + 2**31) & 0xFFFFFFFF) - 2**31
            self.jitter += (abs(delta) - self.jitter) / 16
        self.transit = stamp

    def packet(self, receiver_ssrc: int, media_ssrc: int) -> bytes:
        expected = 0 if self.first is None else self.highest - self.first + 1
        interval = expected - self.prev_expected
        lost_interval = interval - (self.received - self.prev_received)
        fraction = min(255, max(0, int(256 * lost_interval / interval))) if interval > 0 else 0
        lost = max(-0x800000, min(0x7FFFFF, expected - self.received)) & 0xFFFFFF
        self.prev_expected, self.prev_received = expected, self.received
        return (struct.pack('!BBHII', 0x81, 201, 7, receiver_ssrc, media_ssrc)
                + bytes([fraction]) + lost.to_bytes(3, 'big')
                + struct.pack('!IIII', self.highest or 0, int(self.jitter), 0, 0))
