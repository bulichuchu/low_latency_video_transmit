"""RFC 3550 RTP, RFC 6184 single NAL/FU-A, RTCP PLI and receiver reports.

The private RTP extension 0x4c56 carries per-frame metadata. Ordinary H.264
RTP depayloaders may ignore it; this demo requires it for reassembly/metrics.
No pickle or platform-endian structures are accepted on the wire.
"""
from __future__ import annotations

from collections import deque
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
KEY_WINDOW_MS = 50            # LAN: a hardware IDR's burst takes tens of ms to arrive
MAX_WINDOW_MS, MAX_KEY_WINDOW_MS = 150., 200.
# Nothing of a stream for this long: the link has stalled (10-21: 1-2 s on the robot's
# uplink, everything delivered afterwards). Frame windows stop running then.
# Shorter than the IDR window on such a path (165 ms at a 45 ms round trip); three
# frame intervals (at most MAX_STALL_NS) at a lower frame rate (the RK's lowest is
# 10 fps: there a gap between frames is about 100 ms), see Assembler.add.
STALL_NS = 100_000_000
MAX_STALL_NS = 400_000_000
# Sequence numbers skipped across a silence this long: an outage, not loss (settle).
OUTAGE_NS = 1_000_000_000
# A frame given up (expire) whose packets come again this soon after, with no later
# frame completed in between, is reassembled after all (Assembler._revive).
REVIVE_NS = 3_000_000_000
SETTLE_NS = 500_000_000       # a sequence number not here this long after newer ones: lost


def reassembly_windows_ms(base_ms: float, rtt_ms: float | None = None) -> tuple[float, float]:
    """(P frame, IDR) reassembly windows for a path with this round trip.

    The LAN defaults are base_ms and 50 ms. Over a longer path (NetBird across
    the internet on 10-16: 30-50 ms) a retransmission cannot arrive within them,
    so one lost packet cost the whole frame and then an IDR. The windows grow so
    that a P frame can be asked for and resent once (2 RTT + 10 ms) and an IDR,
    which the RK sender then paces over up to ~45 ms, twice (3 RTT + 30 ms).
    The RK sender uses the same function to size its send budgets."""
    rtt = max(0., float(rtt_ms or 0))
    p = max(float(base_ms), min(MAX_WINDOW_MS, 2 * rtt + 10))
    key = max(float(KEY_WINDOW_MS), p, min(MAX_KEY_WINDOW_MS, 3 * rtt + 30))
    return p, key


class Assembler:
    def __init__(self, timeout_ms: float = 20, max_pending: int = 8, *, key_timeout_ms: float | None = None):
        self.timeout_ns = int(timeout_ms * 1e6)
        self.key_timeout_ns = max(self.timeout_ns, int(key_timeout_ms * 1e6)) if key_timeout_ms is not None else self.timeout_ns
        self.max_pending = max_pending
        self.rtt_ns = 0
        self.nack_settle_ns, self.nack_interval_ns = NACK_SETTLE_NS, NACK_INTERVAL_NS
        self.tail_stall_ns = NACK_TAIL_STALL_NS
        # key -> [first packet, first rx, chunks, bytes, highest fragment, when it last advanced]
        self.pending = {}
        self.finished = {}  # bounded duplicate suppression: key -> completed (else given up)
        # key -> (last NACK time, {fragment index: (requests, last request time, first request
        # time, counted in `repaired`)})
        self.retries = {}
        self.asked = deque()  # [first request time, key, fragment index], oldest first (_judge)
        self.last_rx = None  # when the stream's latest packet arrived
        self.resumed = None  # when packets came again after a stall
        self.stall_ns = STALL_NS  # silence that counts as a stall at this frame rate (add)
        self.frame_gaps = deque(maxlen=16)  # between consecutive frames' first packets
        self.last_start = None  # ((ssrc, epoch), frame id, when its first packet came)
        # Requested fragments by how they came: the sender's loss signal (_judge, add).
        self.repaired = 0  # not here 3/4 of a round trip after the request: lost, a resend needed
        self.late = 0  # sooner: the original itself, reordered or delayed (10-21: most of them)
        # Frames given up that may still come (_revive): key -> (entry, retries, when expired).
        self.expired = {}
        self.newest_done = None  # (ssrc, epoch, frame id) of the newest frame completed
        self.newest_key = None  # ... and of the newest IDR completed
        self.revived = 0

    def tune(self, timeout_ms: float, key_timeout_ms: float, rtt_ms: float | None = None):
        """Windows (reassembly_windows_ms) and NACK timing for the path's round trip.
        Pending frames keep their times, so a wider window helps them too.

        On a long path a hole is often only reordering (on 10-16 half of the frames
        NACKed over the internet completed within 5 ms of the request, long before a
        retransmission could come): the first request waits a quarter round trip
        (4-15 ms), the tail counts as lost after that long without progress (at
        least 6 ms), a fragment is asked again only a round trip after its first
        request, and no request goes out when its retransmission could not arrive
        within the window. Without a round trip this is the LAN timing."""
        self.timeout_ns = int(timeout_ms * 1e6)
        self.key_timeout_ns = max(self.timeout_ns, int(key_timeout_ms * 1e6))
        self.rtt_ns = rtt = int(max(0., rtt_ms or 0) * 1e6)
        self.nack_settle_ns = max(NACK_SETTLE_NS, min(rtt // 4, 15_000_000))
        self.tail_stall_ns = max(NACK_TAIL_STALL_NS, self.nack_settle_ns)
        self.nack_interval_ns = max(NACK_INTERVAL_NS, rtt * 6 // 5)

    def assembling(self, ssrc: int, epoch: int, frame_id: int) -> bool:
        """Packets of this frame arrived and its window is still open (a single dict
        lookup, so the decode threads may ask while the receive thread works)."""
        return (ssrc, epoch, frame_id) in self.pending

    def assembling_key(self, ssrc: int, epoch: int, first: int, end: int) -> bool:
        """An IDR with first <= frame id < end is being reassembled (MPP's second IDR
        of a forced pair, say). list() copies the dict in one step under the GIL."""
        return any(k[0] == ssrc and k[1] == epoch and first <= k[2] < end and v[0].meta.key
                   for k, v in list(self.pending.items()))

    def _clock(self, now: int) -> int:
        """now, less the part of a running silence beyond stall_ns: while nothing of the
        stream arrives the link has stalled, and pending frames keep their windows
        (10-21: an IDR in flight when the uplink stopped for 1.4 s timed out although
        its packets came right after the stall, and the 29 frames behind it waited
        for the next IDR)."""
        last = self.last_rx
        if last is not None and now - last > self.stall_ns:
            return last + self.stall_ns
        return now

    def _soon(self) -> int:
        """How soon after a request its retransmission could come at the earliest."""
        return max(2_000_000, self.rtt_ns * 3 // 4)

    def _judge(self):
        """Requested fragments whose original has not come 3/4 of a round trip after
        the first request needed a resend: lost, counted in `repaired` at once rather
        than when the resend comes (in congestion that is often lost too, and then
        only the receiver report would count it, a second later: too late for the
        sender's controller). Judged once a packet came that long after the request
        (an original held up by a stall is not lost) and not within that long after
        a stall (its burst brings the originals); an outage voids them (add)."""
        asked, soon, last = self.asked, self._soon(), self.last_rx
        if not asked or last is None or self.resumed is not None and last - self.resumed < soon:
            return
        while asked and last - asked[0][0] >= soon:
            _, key, index = asked.popleft()
            entry, retry = self.pending.get(key), self.retries.get(key)
            if entry is None:
                gone = self.expired.get(key)
                if gone is None:
                    if self.finished.get(key) is False:  # given up and no longer kept
                        self.repaired += 1
                    continue
                entry, retry = gone[0], (None, gone[1][1]) if gone[1] else None
            if retry is None or index not in retry[1] or index in entry[2] or retry[1][index][3]:
                continue  # came (late or resent: add) or already counted
            n, r, f, _ = retry[1][index]
            retry[1][index] = (n, r, f, True)
            self.repaired += 1
        while len(asked) > 4096:
            asked.popleft()

    def _void_asked(self):
        """After an outage what was asked for before or during it is the outage's
        loss, not congestion (ReceptionReport leaves the numbers skipped across it
        out too): none of it counts, nor do resends of it that come later."""
        for _, key, index in self.asked:
            retry = self.retries.get(key)
            if retry is None and key in self.expired:
                retry = self.expired[key][1]
            if retry is not None and index in retry[1]:
                n, r, f, _ = retry[1][index]
                retry[1][index] = (n, r, f, True)
        self.asked.clear()

    def key_after(self, meta: Meta) -> bool:
        """An IDR after this frame has completed: the decoder starts again from it,
        no other is needed (after an outage a frame frozen in reassembly may expire
        only once the reconnect's IDR is there)."""
        newest = self.newest_key
        return newest is not None and newest[1] == meta.epoch and newest[2] > meta.frame_id

    def _revive(self, key, now: int, index: int) -> bool:
        """Reinstate a frame given up whose packet came again: within REVIVE_NS of its
        expiry and with no later frame completed since (expire keeps it only then;
        add drops it once one completes), so the decoder still waits for it rather
        than for an IDR. A window ending just as the link stalled (before stall_ns of
        silence stops the clock) or a retransmission held up somewhere expires a
        frame whose packets still come. It gets a fresh window from now; a packet it
        already has (a duplicate) does not bring it back."""
        gone = self.expired.get(key)
        if (gone is None or now - gone[2] > REVIVE_NS or len(self.pending) >= self.max_pending
                or index in gone[0][2]):  # a duplicate brings nothing new
            return False
        del self.expired[key]
        entry, retries, _ = gone
        entry[5] = now
        self.pending[key] = entry
        if retries is not None:
            self.retries[key] = retries
        self.finished.pop(key, None)
        self.revived += 1
        return True

    def add(self, p: Packet, now: int) -> Unit | None:
        last = self.last_rx
        if last is not None and now - last > self.stall_ns:
            # Packets again after a stall: the silence beyond stall_ns did not count.
            # (The first packet's time stays: Unit.first_rx_ns is when it came.)
            shift = now - last - self.stall_ns
            for entry in self.pending.values():
                entry[5] += shift
            self.retries = {k: (t + shift, {i: (n, r + shift, f + shift, c) for i, (n, r, f, c) in tries.items()})
                            for k, (t, tries) in self.retries.items()}
            if now - last > OUTAGE_NS:
                self._void_asked()
            self.resumed = now
        self.last_rx = now
        key = (p.ssrc, p.meta.epoch, p.meta.frame_id)
        if (key in self.finished or key in self.expired) and not self._revive(key, now, p.index):
            return None
        if key not in self.pending:
            if len(self.pending) >= self.max_pending:
                raise ValueError('assembly capacity exceeded')
            self.pending[key] = [p, now, {}, 0, -1, now]
            start = self.last_start
            if start is not None and start[0] == key[:2] and key[2] == start[1] + 1 and now > start[2]:
                # A stall is a silence of three frame intervals, at least STALL_NS: at
                # a low frame rate the gaps between frames are no stall.
                self.frame_gaps.append(now - start[2])
                if len(self.frame_gaps) >= 4:
                    gaps = sorted(self.frame_gaps)
                    self.stall_ns = max(STALL_NS, min(MAX_STALL_NS, 3 * gaps[len(gaps) // 2]))
            self.last_start = (key[:2], key[2], now)
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
        retry = self.retries.get(key)
        if retry is not None and p.index in retry[1] and not retry[1][p.index][3]:
            # A requested fragment not yet judged (_judge). A retransmission needs a round
            # trip; the original comes sooner (reordered or delayed), or with the burst
            # after a stall.
            soon = self._soon()
            if now - retry[1][p.index][2] >= soon and (self.resumed is None or now - self.resumed >= soon):
                self.repaired += 1
            else:
                self.late += 1
            n, r, f, _ = retry[1][p.index]
            retry[1][p.index] = (n, r, f, True)
        if p.index > entry[4]:
            # Progress of the original transmission. A retransmission filling
            # a hole below it is not, so it does not postpone the tail check.
            entry[4], entry[5] = p.index, now
        if len(chunks) != p.count:
            return None
        del self.pending[key]
        self.retries.pop(key, None)
        self.finished[key] = True
        if len(self.finished) > 64:
            del self.finished[next(iter(self.finished))]
        newest = self.newest_done
        if newest is None or newest[:2] != key[:2] or newest[2] < key[2]:
            self.newest_done = key
        newest = self.newest_key
        if p.meta.key and (newest is None or newest[:2] != key[:2] or newest[2] < key[2]):
            self.newest_key = key
        for old in [k for k in self.expired if k[:2] == key[:2] and k[2] < key[2]]:
            del self.expired[old]  # the decoder moves on past it now
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
        may interleave frames. Each fragment is requested at most twice; rounds
        for a frame at least 4 ms apart, a fragment again no sooner than that
        after its last request (a round trip on a long path, see tune).
        """
        requests = []
        now = self._clock(now)  # no new requests into a stalled link
        self._judge()
        for key, (packet, started, chunks, _, highest, advanced) in self.pending.items():
            if now - started < self.nack_settle_ns:
                continue
            last, tries = self.retries.get(key, (None, {}))
            if last is not None and now - last < NACK_INTERVAL_NS:
                continue
            timeout = self.key_timeout_ns if packet.meta.key else self.timeout_ns
            if now - advanced >= timeout - max(2_000_000, self.rtt_ns):
                continue  # a retransmission could not arrive within the window (expire)
            # Leave room for a request and its retransmission before the window closes.
            stall = min(self.tail_stall_ns, max(1_000_000, (timeout - 2_000_000) // 2))
            if now - advanced >= stall:
                end = packet.count  # Transmission stopped: the tail is lost too.
            elif len(chunks) <= highest:
                end = highest  # Holes below the highest fragment received.
            else:
                continue  # A contiguous prefix still arriving: called per packet, keep it cheap.
            again = now - self.nack_interval_ns
            wanted = [i for i in range(end) if i not in chunks
                      and (i not in tries or tries[i][0] < NACK_TRIES and tries[i][1] <= again)][:64]
            if not wanted:
                continue
            tries = dict(tries)
            for i in wanted:
                if i in tries:
                    tries[i] = (tries[i][0] + 1, now, tries[i][2], tries[i][3])
                else:
                    tries[i] = (1, now, now, False)
                    self.asked.append([now, key, i])
            self.retries[key] = (now, tries)
            base = (packet.sequence - packet.index) & 0xFFFF
            requests.append((packet, [(base + i) & 0xFFFF for i in wanted]))
        return requests

    def expire(self, now: int) -> list[Meta]:
        """Frames whose window has passed since their transmission last advanced
        (the highest fragment received; at first, the first packet). On a narrow
        link a large frame takes longer than the window just to arrive (10-21
        emulation: a 50 KB IDR needs 270 ms at 1.5 Mbps, the IDR window is at
        most 200 ms, so no IDR ever completed and the picture never came back);
        it still gets the whole window after its last fragment for a resend. While
        the stream is silent the windows do not run (_clock). A frame given up is
        kept for a while in case its packets still come (_revive)."""
        clock = self._clock(now)
        expired = [k for k, value in self.pending.items()
                   if clock - value[5] > (self.key_timeout_ns if value[0].meta.key else self.timeout_ns)]
        result = []
        newest = self.newest_done
        for key in expired:
            entry = self.pending.pop(key)
            retries = self.retries.pop(key, None)
            result.append(entry[0].meta)
            self.finished[key] = False
            if newest is None or newest[:2] != key[:2] or newest[2] < key[2]:
                self.expired[key] = (entry, retries, now)  # nothing after it is complete
        while len(self.finished) > 64:
            del self.finished[next(iter(self.finished))]
        for key in [k for k, gone in self.expired.items() if now - gone[2] > REVIVE_NS]:
            del self.expired[key]
        while len(self.expired) > self.max_pending:  # memory: as much as pending frames
            del self.expired[next(iter(self.expired))]
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
        self.marks = deque()  # (time, highest) at each settle()
        self.settled = None  # sequence numbers up to here are judged
        self.lost_for_good = 0  # of those, never received
        self.arrived = None  # when the latest packet came
        self.outages = deque()  # (first, last) sequence numbers skipped across a stall
        self.requested = set()  # asked for again: Assembler counts those (repaired), settle does not
        self.unrepaired = 0  # ... except into this: asked for and never came (the short-path loss)

    def observe(self, p: Packet, now: int):
        seq = p.sequence
        if self.highest is None:
            self.first = self.highest = seq
        else:
            base = self.highest & ~0xFFFF
            seq = min((base + seq - 65536, base + seq, base + seq + 65536),
                      key=lambda n: abs(n - self.highest))
            if seq > self.highest + 1 and now - self.arrived > OUTAGE_NS:
                # Skipped across a silence: the link was down (10-21: up to 40 s on the
                # robot's uplink). An outage, not congestion: settle leaves them out.
                self.outages.append((self.highest + 1, seq - 1))
            self.highest = max(self.highest, seq)
        self.arrived = now
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

    def asked(self, sequences: list[int]):
        """Sequence numbers the receiver asked for again (NACK)."""
        if self.highest is None:
            return
        base = self.highest & ~0xFFFF
        for seq in sequences:
            self.requested.add(min((base + seq - 65536, base + seq, base + seq + 65536),
                                   key=lambda n: abs(n - self.highest)))

    def settle(self, now: int, horizon_ns: int = SETTLE_NS) -> int:
        """Count the sequence numbers that never arrived once newer ones have been
        here horizon_ns (time for any reordering, retransmission or stall burst):
        packets lost for good, except those skipped across an outage (observe).
        Those asked for count apart, in `unrepaired` (Assembler counted them as
        soon as a resend was needed). Called with every clock ping; returns the
        count of those not asked for."""
        if self.highest is None:
            return self.lost_for_good
        self.marks.append((now, self.highest))
        target = None
        while self.marks and now - self.marks[0][0] >= horizon_ns:
            target = self.marks.popleft()[1]
        if target is not None:
            start = self.first if self.settled is None else self.settled + 1
            start = max(start, self.highest - 4095)  # older ones may have left `seen`
            outages, requested, seen = self.outages, self.requested, self.seen
            for n in range(start, target + 1):
                if n in seen or any(a <= n <= b for a, b in outages):
                    continue
                if n in requested:
                    self.unrepaired += 1
                else:
                    self.lost_for_good += 1
            self.settled = max(self.settled if self.settled is not None else target, target)
            while outages and outages[0][1] <= self.settled:
                outages.popleft()
            if requested:
                self.requested = {n for n in requested if n > self.settled}
        return self.lost_for_good

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
