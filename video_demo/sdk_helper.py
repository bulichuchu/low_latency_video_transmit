"""Explicit macOS SDK helper: local Unix socket, fixed SDK, no web/UDP listener.

Only the launching user's kernel-reported UID may query cameras or request
frames. Requests cannot run commands, choose a library, or read/write files.
The ordinary sender still performs encoding, transport and all report writes.
"""
from __future__ import annotations

from contextlib import closing
import ctypes
import errno
import json
import mmap
import os
from pathlib import Path
import platform
import secrets
import select
import socket
import stat
import struct
import threading
import time

from .sdk import ROOT, parse_device, validate_camera

SOCKET_PATH = ROOT / '.local' / 'orbbec-helper.sock'
HELPER_REVISION = 'shared-memory-v5'
PROTOCOL_VERSION = 2  # 2: frames may cross a shared FrameBuffer; restart the helper after updating
MAX_HEADER = 128 * 1024
MAX_FRAME = 64 * 1024 * 1024
HEADER = struct.Struct('!I')
CAMERA_KEYS = {'device', 'width', 'height', 'fps', 'pixel_format', 'depth_min_mm', 'depth_max_mm'}


class HelperError(RuntimeError):
    pass


def peer_uid(connection):
    """Authenticate the OS process on the other end, not a client-supplied UID."""
    if platform.system() == 'Darwin':
        lib = ctypes.CDLL(None, use_errno=True)
        fn = lib.getpeereid
        fn.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
        fn.restype = ctypes.c_int
        uid, gid = ctypes.c_uint(), ctypes.c_uint()
        if fn(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)):
            raise OSError(ctypes.get_errno(), 'getpeereid failed')
        return uid.value
    if hasattr(socket, 'SO_PEERCRED'):
        return struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
    raise HelperError('This platform cannot authenticate Unix socket peers')


def read_exact(connection, size, stop=None, timeout=10, fds=None):
    """Read size bytes; with fds (a list), also collect descriptors sent with them."""
    result = bytearray()
    deadline = time.monotonic() + timeout
    while len(result) < size:
        if stop is not None and stop.is_set():
            raise InterruptedError('SDK capture stopped')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('SDK 助手超时；请检查助手终端及 USB 连接')
        if not select.select([connection], [], [], min(.1, remaining))[0]:
            continue
        want = min(size - len(result), 1024 * 1024)
        if fds is None:
            chunk = connection.recv(want)
        else:
            chunk, received, flags, _ = socket.recv_fds(connection, want, 4)
            fds.extend(received)
            if flags & getattr(socket, 'MSG_CTRUNC', 0):
                raise ValueError('SDK descriptor message truncated')
        if not chunk:
            raise EOFError('SDK 助手连接已关闭；请检查助手终端')
        result.extend(chunk)
    return result


def send_message(connection, message, fds=()):
    data = json.dumps(message, ensure_ascii=False, allow_nan=False).encode()
    if len(data) > MAX_HEADER:
        raise ValueError('SDK message too large')
    data = HEADER.pack(len(data)) + data
    if fds:
        # Descriptors ride on the first bytes; any remainder follows normally.
        data = data[socket.send_fds(connection, [data], list(fds)):]
    connection.sendall(data)


def receive_message(connection, stop=None, timeout=10, fds=None):
    size = HEADER.unpack(read_exact(connection, HEADER.size, stop, timeout, fds))[0]
    if not 0 < size <= MAX_HEADER:
        raise ValueError('Invalid SDK message length')
    message = json.loads(read_exact(connection, size, stop, timeout))
    if not isinstance(message, dict):
        raise ValueError('SDK message must be an object')
    if message.get('type') == 'error':
        raise HelperError(message.get('error', 'SDK helper failed'))
    return message


class FrameBuffer:
    """Unnamed shared memory carrying one camera's decoded planes.

    Inline, a 720p frame (1.8 MB) crossed the socket in small chunks (macOS
    Unix sockets buffer 8 KB) and was copied four times. The sender creates
    this buffer, so it is owned by the user, unlinks its name at once and
    hands only the descriptor to the helper with the frames request: root
    writes camera planes into memory the sender gave it and nothing else.
    The pull protocol makes one buffer enough: the helper fills it only after
    'next', and the sender copies the previous frame out before asking.
    """
    def __init__(self, fd, size):
        self.fd, self.size = fd, size
        try:
            self.map = mmap.mmap(fd, size)
        except BaseException:
            os.close(fd)
            raise

    @classmethod
    def create(cls, size):
        # CPython's POSIX shm primitive (multiprocessing.shared_memory) without
        # its resource tracker: the name never outlives this call.
        import _posixshmem
        for _ in range(8):
            name = '/vd-' + secrets.token_hex(8)  # macOS: at most 31 bytes
            try:
                fd = _posixshmem.shm_open(name, os.O_CREAT | os.O_EXCL | os.O_RDWR, mode=0o600)
            except FileExistsError:
                continue
            try:
                _posixshmem.shm_unlink(name)
                os.ftruncate(fd, size)
            except BaseException:
                os.close(fd)
                raise
            return cls(fd, size)
        raise FileExistsError('No unused shared memory name')

    @classmethod
    def attach(cls, fd, size):
        """Helper side: map the descriptor that came with a frames request."""
        try:
            if type(size) is not int or not 0 < size <= MAX_FRAME:
                raise ValueError('Invalid SDK frame buffer size')
            if os.fstat(fd).st_size < size:
                raise ValueError('SDK frame buffer is smaller than declared')
        except BaseException:
            os.close(fd)
            raise
        return cls(fd, size)

    def close(self):
        try:
            self.map.close()
        except BufferError:
            pass  # a view kept alive by an exception traceback; unmapped with it
        os.close(self.fd)


def frame_buffer_size(camera):
    """Largest decoded layout the helper sends: 4 bytes per pixel (RGBA/BGRA)."""
    width, height = camera.get('width'), camera.get('height')
    if type(width) is not int or type(height) is not int or not (0 < width <= 4096 and 0 < height <= 2160):
        raise ValueError('SDK camera size unknown')
    return 4 * width * height


def send_frame(connection, frame, capture_ns, metadata, buffer=None):
    """Copy native AV planes without an additional RGB conversion or codec.

    With a FrameBuffer only the header crosses the socket; otherwise (no
    buffer, or a frame that does not fit) the planes follow it inline.
    """
    import numpy as np
    rows = plane_row_bytes(frame)
    # Omit allocator padding, which differs between MJPEG decoder and AVFrame
    # allocations even in the same venv (e.g. an 848-pixel image).
    planes = [np.frombuffer(plane, dtype=np.uint8, count=plane.height * plane.line_size)
              .reshape(plane.height, plane.line_size)[:, :row]
              for plane, row in zip(frame.planes, rows)]
    size = sum(p.size for p in planes)
    if size > MAX_FRAME:
        raise ValueError('SDK frame too large')
    shared = buffer is not None and size <= buffer.size
    if shared:
        offset = 0
        for data in planes:
            np.frombuffer(buffer.map, dtype=np.uint8, count=data.size, offset=offset).reshape(data.shape)[:] = data
            offset += data.size
    send_message(connection, dict(type='frame', width=frame.width, height=frame.height,
        format=frame.format.name, colorspace=int(frame.colorspace), color_range=int(frame.color_range),
        capture_ns=capture_ns, metadata=metadata, size=size, transport='shared' if shared else 'inline',
        planes=[dict(stride=row, height=p.height, size=data.size) for p, row, data in zip(frame.planes, rows, planes)]))
    if not shared:
        for data in planes:
            connection.sendall(data.tobytes())


def plane_row_bytes(frame):
    pixel = frame.format.name
    packed = {'rgb24': 3, 'bgr24': 3, 'rgba': 4, 'bgra': 4, 'gray16le': 2, 'yuyv422': 2, 'uyvy422': 2}
    return [p.width * (2 if pixel in ('nv12', 'nv21') and index == 1 else packed.get(pixel, 1))
            for index, p in enumerate(frame.planes)]


def receive_frame(connection, message, stop, buffer=None):
    import av
    import numpy as np
    from .orbbec import FORMATS
    width, height = message.get('width'), message.get('height')
    if (message.get('type') != 'frame' or type(width) is not int or type(height) is not int
        or not 1 <= width <= 4096 or not 1 <= height <= 2160
        or message.get('format') not in (set(FORMATS.values()) | {'yuvj420p', 'yuvj422p', 'yuvj444p'})):
        raise ValueError('Invalid SDK frame dimensions/format')
    size = message.get('size')
    if type(size) is not int or not 0 < size <= MAX_FRAME:
        raise ValueError('Invalid SDK frame size')
    frame = av.VideoFrame(width, height, message['format'])
    layout = message.get('planes')
    if not isinstance(layout, list) or len(layout) != len(frame.planes):
        raise ValueError('Invalid SDK plane count')
    total = 0
    for info, plane, row in zip(layout, frame.planes, plane_row_bytes(frame)):
        if (not isinstance(info, dict) or any(type(info.get(k)) is not int for k in ('stride', 'height', 'size'))
            or info['height'] != plane.height or info['stride'] != row
            or info['size'] != row * plane.height):
            raise ValueError('Invalid SDK plane layout')
        total += info['size']
    if total != size:
        raise ValueError('SDK payload size mismatch')
    if type(message.get('capture_ns')) is not int or message['capture_ns'] <= 0 or not isinstance(message.get('metadata'), dict):
        raise ValueError('Invalid SDK timestamp/metadata')
    transport = message.get('transport', 'inline')
    if transport not in ('inline', 'shared') or (transport == 'shared' and (buffer is None or size > buffer.size)):
        raise ValueError('Invalid SDK frame transport')
    offset = 0
    for plane, info in zip(frame.planes, layout):
        if transport == 'shared':
            data = np.frombuffer(buffer.map, dtype=np.uint8, count=info['size'], offset=offset)
            offset += info['size']
        else:
            data = read_exact(connection, info['size'], stop)
        if info['stride'] == plane.line_size:
            plane.update(data)
        else:
            padded = np.zeros((plane.height, plane.line_size), dtype=np.uint8)
            padded[:, :info['stride']] = np.frombuffer(data, dtype=np.uint8).reshape(plane.height, info['stride'])
            plane.update(padded)
    frame.colorspace = message.get('colorspace', 2)
    frame.color_range = message.get('color_range', 0)
    metadata = message['metadata']
    metadata.setdefault('sensor_status', 'legacy_helper')
    if isinstance(metadata.get('sdk_device_timestamp_us'), int):
        from fractions import Fraction
        frame.pts, frame.time_base = metadata['sdk_device_timestamp_us'], Fraction(1, 1_000_000)
    return frame, message['capture_ns'], {**metadata, 'sdk_access': 'local_admin_helper', 'sdk_transport': transport}


def connect_helper(root, *, path=None, required_uid=0):
    """Absent helper selects direct SDK; a broken/running helper is never hidden."""
    path = Path(path or SOCKET_PATH)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISSOCK(info.st_mode):
        raise HelperError('SDK 助手路径不是套接字，请检查 .local/orbbec-helper.sock')
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(5)
    try:
        connection.connect(str(path))
        if peer_uid(connection) != required_uid:
            raise HelperError('SDK 助手没有管理员身份；请使用 start_sdk_helper.command 启动')
        send_message(connection, {'op': 'hello', 'version': PROTOCOL_VERSION, 'sdk_root': str(Path(root).resolve())})
        if receive_message(connection).get('type') != 'ready':
            raise HelperError('Unexpected SDK helper handshake')
        return connection
    except (ConnectionRefusedError, FileNotFoundError):
        connection.close()
        raise HelperError('SDK 助手已停止；请重新运行 start_sdk_helper.command') from None
    except BaseException:
        connection.close()
        raise


def helper_inventory(root):
    connection = connect_helper(root)
    if connection is None:
        return None
    with connection:
        send_message(connection, {'op': 'inventory'})
        response = receive_message(connection, timeout=45)
        if response.get('type') != 'inventory':
            raise HelperError('Unexpected SDK inventory response')
        return {**response['data'], 'access': 'local_admin_helper'}


def helper_frames(connection, camera, stop):
    with connection:
        request = {'op': 'frames', 'camera': {k: v for k, v in camera.items() if k in CAMERA_KEYS}}
        try:
            buffer = FrameBuffer.create(frame_buffer_size(camera))
        except (ImportError, OSError, ValueError):
            buffer = None  # planes then follow each header over the socket
        try:
            if buffer:
                send_message(connection, {**request, 'buffer': buffer.size}, fds=[buffer.fd])
            else:
                send_message(connection, request)
            while not stop.is_set():
                send_message(connection, {'op': 'next'})
                yield receive_frame(connection, receive_message(connection, stop), stop, buffer)
        except EOFError as exc:
            if not stop.is_set():
                raise HelperError(f'{camera["device"]}：SDK 助手意外断开（可能已退出或原生库崩溃）。'
                                  '请查看助手终端，更新代码后重新运行 start_sdk_helper.command。') from exc
        except (InterruptedError, OSError):
            if not stop.is_set():
                raise
        finally:
            if buffer:
                buffer.close()


class Broker:
    """Serve bounded local requests. A serial can have only one capture owner."""
    def __init__(self, root, owner_uid, inventory_fn=None, frames_fn=None):
        from .orbbec import inventory, camera_frames
        self.root = Path(root).resolve()
        self.owner_uid = owner_uid
        self.inventory_fn = inventory_fn or inventory
        self.frames_fn = frames_fn or camera_frames
        self.lock = threading.RLock()
        self.cached = None
        self.active = set()
        self.stop = threading.Event()
        self.connections = set()
        self.connections_lock = threading.Lock()

    def inventory(self):
        with self.lock:
            # Reopen only idle devices. A failed idle camera must not stay
            # missing just because another camera is still streaming.
            if self.active:
                retained = [r for r in (self.cached or {}).get('devices', [])
                            if parse_device(r['device'])[0] in self.active]
                fresh = self.inventory_fn(self.root, exclude_serials=frozenset(self.active))
                self.cached = {**fresh, 'devices': retained + fresh['devices']}
            else:
                self.cached = self.inventory_fn(self.root)
            return {**self.cached, 'capabilities_cached': bool(self.active),
                    'cached_serials': sorted(self.active), 'helper_revision': HELPER_REVISION}

    def reserve(self, camera):
        if not isinstance(camera, dict) or set(camera) - CAMERA_KEYS:
            raise ValueError('Only camera acquisition settings are accepted')
        validate_camera(camera)
        serial, _ = parse_device(camera['device'])
        with self.lock:
            if serial in self.active:
                raise ValueError('该 SDK 设备正在采集；请先停止已有发送任务')
            info = self.cached if self.cached is not None else self.inventory()
            record = next((r for r in info['devices'] if r['device'] == camera['device']), None)
            if record is None:
                raise ValueError('SDK 未提供所选图像流，请重新查询设备')
            supported = any((m['width'], m['height'], m['pixel_format']) ==
                (camera.get('width'), camera.get('height'), camera.get('pixel_format')) and
                any(r['min'] <= camera.get('fps', 0) <= r['max'] for r in m['frame_rates'])
                for m in record['formats'])
            if not supported:
                raise ValueError('SDK 不支持所选采集模式，请重新查询设备')
            self.active.add(serial)
        return serial

    def handle(self, connection):
        serial = None
        fds, buffer = [], None
        try:
            connection.settimeout(5)
            if peer_uid(connection) != self.owner_uid:
                raise PermissionError('SDK helper only accepts its launching user')
            hello = receive_message(connection, timeout=5)
            if hello != {'op': 'hello', 'version': PROTOCOL_VERSION, 'sdk_root': str(self.root)}:
                raise ValueError('SDK 路径或协议不一致；更改 SDK 目录或更新代码后请重启助手')
            send_message(connection, {'type': 'ready'})
            request = receive_message(connection, fds=fds)
            if request == {'op': 'inventory'} and not fds:
                send_message(connection, {'type': 'inventory', 'data': self.inventory()})
            elif (request.get('op') == 'frames' and set(request) in ({'op', 'camera'}, {'op', 'camera', 'buffer'})
                  and len(fds) == (1 if 'buffer' in request else 0)):
                if fds:
                    try:
                        buffer = FrameBuffer.attach(fds.pop(), request['buffer'])
                    except (OSError, ValueError) as exc:  # frames still flow inline
                        print(f'[sdk-helper] 共享内存不可用，改为经 socket 传帧：{exc}', flush=True)
                camera = request['camera']
                serial = self.reserve(camera)
                # Pull exactly one frame at a time. A paused reader cannot build
                # a FIFO here; the SDK itself retains only its latest frame.
                with closing(self.frames_fn(self.root, camera, self.stop)) as frames:
                    while not self.stop.is_set():
                        if receive_message(connection, timeout=5) != {'op': 'next'}:
                            raise ValueError('Expected next frame request')
                        send_frame(connection, *next(frames), buffer=buffer)
            else:
                raise ValueError('SDK helper accepts only inventory or frames')
        except (EOFError, BrokenPipeError, ConnectionResetError, InterruptedError, StopIteration):
            pass
        except Exception as exc:
            print(f'[sdk-helper] {serial or "request"}: {type(exc).__name__}: {exc}', flush=True)
            try:
                send_message(connection, {'type': 'error', 'error': str(exc)[:2000]})
            except OSError:
                pass
        finally:
            for fd in fds:
                os.close(fd)
            if buffer:
                buffer.close()
            connection.close()
            with self.connections_lock:
                self.connections.discard(connection)
            with self.lock:
                if serial is not None:
                    self.active.discard(serial)

    def shutdown(self):
        self.stop.set()
        with self.connections_lock:
            for connection in list(self.connections):
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


def serve(root, owner_uid, owner_gid):
    if platform.system() != 'Darwin' or os.geteuid() != 0 or owner_uid <= 0:
        raise HelperError('请在 macOS 普通用户终端运行 start_sdk_helper.command，并由 sudo 授权')
    from .orbbec import library_path, NativeSDK
    library_path(root)
    path = SOCKET_PATH
    if len(os.fsencode(path)) > 103:
        raise HelperError('macOS 本机套接字路径过长，请把项目移动到较短的目录后重试')
    directory = path.parent.lstat()
    if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != owner_uid or directory.st_mode & 0o077:
        raise HelperError('SDK 助手目录须由启动用户拥有且权限为 700；请使用启动脚本')
    try:
        previous = path.lstat()
    except FileNotFoundError:
        previous = None
    if previous:
        if not stat.S_ISSOCK(previous.st_mode) or previous.st_uid != owner_uid:
            raise HelperError('SDK socket path is occupied by an unexpected file')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            try:
                probe.connect(str(path))
            except OSError as exc:
                if exc.errno != errno.ECONNREFUSED:
                    raise
            else:
                raise HelperError('SDK 助手已运行，请保持原助手终端开启')
        path.unlink()
    broker = Broker(root, owner_uid)
    workers = []
    # Keep the native context alive across queries and stream restarts. All
    # camera threads borrow it; none may toggle process-wide enumeration.
    with NativeSDK(root).context(), socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        inode = path.lstat().st_ino
        try:
            os.chown(path, owner_uid, owner_gid)
            os.chmod(path, 0o600)
            listener.listen(8)
            listener.settimeout(.5)
            print(f'SDK 助手已就绪（{HELPER_REVISION}；仅本机连接，尚未开始采集）。\n'
                  '保持此终端开启，在 Vue 发送页面点击“查询 SDK 摄像头”。Ctrl+C 停止助手。', flush=True)
            while not broker.stop.is_set():
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    continue
                with broker.connections_lock:
                    if len(broker.connections) >= 16:
                        connection.close()
                        continue
                    broker.connections.add(connection)
                worker = threading.Thread(target=broker.handle, args=(connection,), daemon=True)
                workers = [w for w in workers if w.is_alive()]
                workers.append(worker)
                worker.start()
        except KeyboardInterrupt:
            pass
        finally:
            broker.shutdown()
            deadline = time.monotonic() + 6
            for worker in workers:
                worker.join(max(0, deadline - time.monotonic()))
            if path.exists() and path.lstat().st_ino == inode:
                path.unlink()
    return 0
