"""WebRTC offer/answer over the existing sender <-> receiver UDP control path.

SDP is a few kilobytes, so it is split into small JSON datagrams (each well
under a 1280-byte VPN MTU). The receiver resends a whole offer until it gets
the answer; the sender answers each session id once and resends that answer
for duplicates, so a lost datagram only costs one resend interval.
"""
from __future__ import annotations

import json
import secrets
import time

CHUNK = 800             # SDP characters per datagram; JSON escaping stays < 1200 bytes
MAX_TEXT = 64 * 1024    # a few streams' SDP is ~5-10 KB
MAX_PARTS = MAX_TEXT // CHUNK + 1
KINDS = ('webrtc_offer', 'webrtc_answer', 'webrtc_error', 'webrtc_close')


def new_session():
    return secrets.token_hex(6)


def datagrams(kind, session, text=''):
    if kind not in KINDS or not isinstance(text, str) or len(text) > MAX_TEXT:
        raise ValueError('Invalid WebRTC signaling message')
    parts = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK)] or ['']
    return [json.dumps(dict(v=1, type=kind, id=session, i=i, n=len(parts), d=part),
                       separators=(',', ':')).encode() for i, part in enumerate(parts)]


def parse(data):
    """Return a validated signaling dict, or None for any other datagram."""
    if not data[:1] == b'{':
        return None
    try:
        msg = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return None
    if not (isinstance(msg, dict) and msg.get('v') == 1 and msg.get('type') in KINDS):
        return None
    if not (isinstance(msg.get('id'), str) and 0 < len(msg['id']) <= 32
            and type(msg.get('i')) is int and type(msg.get('n')) is int
            and 0 <= msg['i'] < msg['n'] <= MAX_PARTS and isinstance(msg.get('d'), str)
            and len(msg['d']) <= CHUNK):
        return None
    return msg


class Reassembler:
    """Collect the parts of each (type, session); stale partial messages expire."""
    def __init__(self, ttl=10.0, limit=8):
        self.ttl, self.limit = ttl, limit
        self.pending = {}

    def add(self, msg):
        now = time.monotonic()
        self.pending = {k: v for k, v in self.pending.items() if now - v[0] < self.ttl}
        key = (msg['type'], msg['id'])
        if key not in self.pending:
            if len(self.pending) >= self.limit:
                return None
            self.pending[key] = (now, msg['n'], {})
        started, count, parts = self.pending[key]
        if count != msg['n']:
            del self.pending[key]
            return None
        parts[msg['i']] = msg['d']
        if len(parts) < count:
            return None
        del self.pending[key]
        return ''.join(parts[i] for i in range(count))
