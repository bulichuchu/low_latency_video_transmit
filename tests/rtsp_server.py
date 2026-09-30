"""Loopback-only RTSP test fixture: real H.264 over TCP interleaving or UDP.

Not a production RTSP server. The application under test uses FFmpeg's RTSP client.
"""
import base64
import socket
import struct
import threading
import time

import av
import numpy as np

from video_demo.media import make_encoder, prepare_frame
from video_demo.protocol import HEADER_SIZE, Meta, Packetizer, nals


class RtspCamera:
    def __init__(self, *, credentials=None, disconnect_once=False, stall=False):
        encoder = make_encoder('libx264', 320, 180, 30, 400_000)
        self.frames = []
        for i in range(30):
            rgb = np.full((180, 320, 3), (120 + i, 50, 180), dtype=np.uint8)
            frame = av.VideoFrame.from_ndarray(rgb, format='rgb24')
            self.frames.append(bytes(encoder.encode(prepare_frame(frame, i, encoder))[0]))
        units = nals(self.frames[0])
        sps = next(n for n in units if n[0] & 31 == 7)
        pps = next(n for n in units if n[0] & 31 == 8)
        self.parameters = ','.join(base64.b64encode(n).decode() for n in (sps, pps))
        self.credentials, self.disconnect_once, self.stall = credentials, disconnect_once, stall
        self.stop = threading.Event()
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen()
        self.listener.settimeout(.1)
        self.port = self.listener.getsockname()[1]
        self.clients, self.workers, self.failures = [], [], []
        self.play_count = 0

    @property
    def url(self):
        auth = self.credentials + '@' if self.credentials else ''
        return f'rtsp://{auth}127.0.0.1:{self.port}/camera'

    def __enter__(self):
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.listener.close()
        self.thread.join(timeout=1)
        for client in self.clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            client.close()
        for worker in self.workers:
            worker.join(timeout=1)

    def _accept(self):
        while not self.stop.is_set():
            try:
                client, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.clients.append(client)
            worker = threading.Thread(target=self._client, args=(client,), daemon=True)
            self.workers.append(worker)
            worker.start()

    def _client(self, client):
        lock = threading.Lock()
        done = threading.Event()
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.bind(('127.0.0.1', 0))
        destination, channel = None, 0
        player = None

        def respond(cseq, headers='', body=b'', status='200 OK'):
            data = (f'RTSP/1.0 {status}\r\nCSeq: {cseq}\r\n{headers}'
                    f'Content-Length: {len(body)}\r\n\r\n').encode() + body
            with lock:
                client.sendall(data)

        def play(disconnect):
            packetizer = Packetizer(mtu=1100)
            seq, tick = 0, time.monotonic()
            try:
                while not self.stop.is_set() and not done.is_set():
                    if disconnect and seq == 25:
                        client.shutdown(socket.SHUT_RDWR)
                        break
                    if not self.stall:
                        stamp = 1_000_000_000 + seq * 1_000_000_000 // 30
                        for data in packetizer.packetize(self.frames[seq % 30], Meta(0, 1, seq, stamp)):
                            # Strip the application's extension; advertise ordinary H.264 RTP.
                            packet = bytes([0x80]) + data[1:12] + data[HEADER_SIZE:]
                            if destination:
                                udp.sendto(packet, destination)
                            else:
                                with lock:
                                    client.sendall(b'$' + bytes([channel]) + struct.pack('!H', len(packet)) + packet)
                    seq += 1
                    tick += 1 / 30
                    done.wait(max(0, tick - time.monotonic()))
            except OSError:
                pass

        try:
            client.settimeout(.2)
            buffer = b''
            while not self.stop.is_set() and not done.is_set():
                try:
                    data = client.recv(65536)
                except socket.timeout:
                    continue
                if not data:
                    break
                buffer += data
                while buffer:
                    if buffer.startswith(b'$'):
                        if len(buffer) < 4:
                            break
                        size = 4 + struct.unpack_from('!H', buffer, 2)[0]
                        if len(buffer) < size:
                            break
                        buffer = buffer[size:]  # Ignore interleaved receiver reports.
                        continue
                    end = buffer.find(b'\r\n\r\n')
                    if end < 0:
                        break
                    lines = buffer[:end].decode().split('\r\n')
                    headers = dict(line.split(':', 1) for line in lines[1:])
                    headers = {k.lower(): v.strip() for k, v in headers.items()}
                    size = end + 4 + int(headers.get('content-length', 0))
                    if len(buffer) < size:
                        break
                    buffer = buffer[size:]
                    method = lines[0].split()[0]
                    cseq = headers['cseq']
                    if self.credentials:
                        expected = 'Basic ' + base64.b64encode(self.credentials.encode()).decode()
                        if headers.get('authorization') != expected:
                            respond(cseq, 'WWW-Authenticate: Basic realm="test camera"\r\n', status='401 Unauthorized')
                            continue
                    if method == 'OPTIONS':
                        respond(cseq, 'Public: OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN, GET_PARAMETER\r\n')
                    elif method == 'DESCRIBE':
                        body = ('v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=Test camera\r\n'
                                'c=IN IP4 127.0.0.1\r\nt=0 0\r\na=control:*\r\n'
                                'm=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n'
                                f'a=fmtp:96 packetization-mode=1;sprop-parameter-sets={self.parameters}\r\n'
                                'a=control:trackID=0\r\n').encode()
                        respond(cseq, f'Content-Type: application/sdp\r\nContent-Base: rtsp://127.0.0.1:{self.port}/camera/\r\n', body)
                    elif method == 'SETUP':
                        transport = headers['transport']
                        if 'interleaved=' in transport:
                            channel = int(transport.split('interleaved=')[1].split('-')[0])
                        else:
                            port = int(transport.split('client_port=')[1].split('-')[0])
                            destination = ('127.0.0.1', port)
                            transport += f';server_port={udp.getsockname()[1]}-{udp.getsockname()[1]+1}'
                        respond(cseq, f'Session: 123456;timeout=30\r\nTransport: {transport}\r\n')
                    elif method == 'PLAY':
                        self.play_count += 1
                        respond(cseq, 'Session: 123456\r\nRange: npt=0.000-\r\n')
                        player = threading.Thread(target=play,
                            args=(self.disconnect_once and self.play_count == 1,), daemon=True)
                        player.start()
                    elif method == 'TEARDOWN':
                        respond(cseq)
                        done.set()
                    else:
                        respond(cseq, 'Session: 123456\r\n')
        except OSError:
            pass
        except Exception as exc:
            self.failures.append(repr(exc))
        finally:
            done.set()
            if player:
                player.join(timeout=1)
            udp.close()
            client.close()
