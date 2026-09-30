"""Exercise the helper through local socketpairs; no privilege or cameras."""
from contextlib import contextmanager
import os
import socket
import tempfile
import threading
import time

import av
import numpy as np
import pytest

from video_demo import sdk, sdk_helper as helper


def sample_record():
    return dict(device='orbbec://TEST/color', formats=[dict(width=64, height=64,
        pixel_format='rgb24', frame_rates=[dict(min=30, max=30)])])


def camera():
    return dict(device='orbbec://TEST/color', width=64, height=64, fps=30, pixel_format='rgb24')


@contextmanager
def connection_to(broker, handshake=True):
    client, server = socket.socketpair()
    broker.connections.add(server)
    thread = threading.Thread(target=broker.handle, args=(server,), daemon=True)
    thread.start()
    try:
        if handshake:
            helper.send_message(client, dict(op='hello', version=1, sdk_root=str(broker.root)))
            assert helper.receive_message(client) == {'type': 'ready'}
        yield client
    finally:
        client.close()
        thread.join(2)
        assert not thread.is_alive(), 'Helper worker leaked after client disconnected'


@pytest.fixture
def socket_directory():
    # Darwin sockaddr_un cannot hold pytest's normal long temporary directory.
    with tempfile.TemporaryDirectory(prefix='vsdk-', dir='/tmp') as directory:
        from pathlib import Path
        yield Path(directory)


@pytest.fixture
def broker(tmp_path):
    counters = dict(queries=0, closed=0)
    def inventory(root, *, exclude_serials=()):
        counters['queries'] += 1
        return dict(devices=[] if 'TEST' in exclude_serials else [sample_record()], status='ready')
    def frames(root, setting, stop):
        try:
            while not stop.is_set():
                frame = av.VideoFrame.from_ndarray(np.full((64, 64, 3), (30, 150, 240), dtype=np.uint8), format='rgb24')
                yield frame, 123456789, dict(sdk_device_timestamp_us=42, sdk_system_timestamp_us=99)
        finally:
            counters['closed'] += 1
    result = helper.Broker(tmp_path, os.getuid(), inventory, frames)
    result.counters = counters
    return result


@pytest.mark.parametrize('pixel', ['rgb24', 'bgr24', 'bgra', 'rgba', 'yuyv422', 'uyvy422', 'nv12', 'nv21', 'yuv420p', 'gray', 'gray16le', 'yuvj420p'])
def test_native_frame_roundtrip_preserves_pixels_metadata_and_timestamp(pixel):
    original = av.VideoFrame.from_ndarray(np.full((64, 80, 3), (35, 120, 210), dtype=np.uint8), format='rgb24').reformat(format=pixel)
    with _pair() as (client, server):
        errors = []
        def send():
            try:
                helper.send_frame(server, original, 999999, {'sdk_device_timestamp_us': 123, 'depth_preview': True,
                    'sensor_capture_ns': 900000, 'sdk_global_timestamp_us': 1790000000000000,
                    'sensor_status': 'ready', 'sensor_clock_uncertainty_us': 2})
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=send)
        thread.start()
        frame, capture_ns, metadata = helper.receive_frame(client, helper.receive_message(client), threading.Event())
        thread.join(2)
        assert not thread.is_alive() and not errors
        assert capture_ns == 999999 and frame.pts == 123
        assert frame.format.name == pixel and metadata['depth_preview'] is True
        assert metadata['sdk_access'] == 'local_admin_helper'
        assert metadata['sensor_capture_ns'] == 900000 and metadata['sensor_status'] == 'ready'
        assert metadata['sdk_global_timestamp_us'] == 1790000000000000
        assert np.array_equal(frame.to_ndarray(format='rgb24'), original.to_ndarray(format='rgb24'))


def test_decoded_mjpeg_allocator_padding_is_not_transmitted():
    from fractions import Fraction
    encoder = av.CodecContext.create('mjpeg', 'w')
    encoder.width, encoder.height, encoder.pix_fmt, encoder.time_base = 80, 64, 'yuvj420p', Fraction(1, 30)
    image = av.VideoFrame.from_ndarray(np.full((64, 80, 3), (30, 150, 240), dtype=np.uint8), format='rgb24')
    decoded = av.CodecContext.create('mjpeg', 'r').decode(encoder.encode(image.reformat(format='yuvj420p'))[0])[0]
    with _pair() as (client, server):
        thread = threading.Thread(target=helper.send_frame, args=(server, decoded, 1234, {}))
        thread.start()
        info = helper.receive_message(client)
        assert info['size'] == 80 * 64 * 3 // 2
        received, _, _ = helper.receive_frame(client, info, threading.Event())
        thread.join(2)
        assert not thread.is_alive()
        assert np.array_equal(decoded.to_ndarray(), received.to_ndarray())


@contextmanager
def _pair():
    left, right = socket.socketpair()
    try:
        yield left, right
    finally:
        left.close()
        right.close()


def test_query_capture_duplicate_and_cleanup(broker):
    with connection_to(broker) as query:
        helper.send_message(query, {'op': 'inventory'})
        assert helper.receive_message(query)['data']['devices'] == [sample_record()]
    with connection_to(broker) as client:
        helper.send_message(client, {'op': 'frames', 'camera': camera()})
        helper.send_message(client, {'op': 'next'})
        frame, stamp, meta = helper.receive_frame(client, helper.receive_message(client), threading.Event())
        assert stamp == 123456789 and meta['sdk_system_timestamp_us'] == 99
        assert frame.to_ndarray()[0, 0].tolist() == [30, 150, 240]
        with connection_to(broker) as query:
            helper.send_message(query, {'op': 'inventory'})
            info = helper.receive_message(query)['data']
            assert info['capabilities_cached'] is True
            assert info['cached_serials'] == ['TEST']
            assert info['devices'] == [sample_record()]
        assert broker.counters['queries'] == 2
        with connection_to(broker) as duplicate:
            helper.send_message(duplicate, {'op': 'frames', 'camera': camera()})
            with pytest.raises(helper.HelperError, match='正在采集'):
                helper.receive_message(duplicate)
    assert broker.active == set() and broker.counters['closed'] == 1


def test_failed_idle_camera_is_rediscovered_without_reopening_live_camera(broker):
    second = {**sample_record(), 'device': 'orbbec://SECOND/color'}
    state = ['unavailable']
    exclusions = []

    def inventory(root, *, exclude_serials=()):
        exclusions.append(set(exclude_serials))
        if state[0] == 'query_error':
            raise RuntimeError('USB enumeration failed')
        records = [] if 'TEST' in exclude_serials else [sample_record()]
        if state[0] == 'ready':
            records.append(second)
        return dict(devices=records, unavailable=[dict(serial='SECOND', error='uvc_open -3')]
                    if state[0] == 'unavailable' else [], status='ready')

    broker.inventory_fn = inventory
    assert broker.inventory()['unavailable'][0]['serial'] == 'SECOND'
    with connection_to(broker) as client:
        helper.send_message(client, {'op': 'frames', 'camera': camera()})
        helper.send_message(client, {'op': 'next'})
        helper.receive_frame(client, helper.receive_message(client), threading.Event())
        state[0] = 'ready'
        info = broker.inventory()
        assert info['devices'] == [sample_record(), second]
        assert info['unavailable'] == [] and info['cached_serials'] == ['TEST']
        assert exclusions == [set(), {'TEST'}]
        state[0] = 'query_error'
        with pytest.raises(RuntimeError, match='USB enumeration failed'):
            broker.inventory()
        assert broker.cached['devices'] == [sample_record(), second]
        state[0] = 'disconnected'
        assert broker.inventory()['devices'] == [sample_record()]
        # Refreshes, failures and unplugging the idle camera do not stop the live stream.
        helper.send_message(client, {'op': 'next'})
        frame, _, _ = helper.receive_frame(client, helper.receive_message(client), threading.Event())
        assert frame.width == 64 and broker.active == {'TEST'}
    assert not broker.active and broker.counters['closed'] == 1
    state[0] = 'ready'
    assert broker.inventory()['devices'] == [sample_record(), second]
    assert exclusions[-1] == set()


def test_reject_peer_uid_before_parsing_request(broker, monkeypatch):
    monkeypatch.setattr(helper, 'peer_uid', lambda sock: os.getuid() + 1)
    with connection_to(broker, handshake=False) as client:
        with pytest.raises(helper.HelperError, match='launching user'):
            helper.receive_message(client)


@pytest.mark.parametrize('operation', [dict(op='exec', command='id'), dict(op='inventory', sdk_root='/tmp/other'),
    dict(op='frames', camera={**camera(), 'filename': '/tmp/file'}),
    dict(op='frames', camera={**camera(), 'width': 128})])
def test_reject_arbitrary_operations_and_unsupported_modes(broker, operation):
    with connection_to(broker) as client:
        helper.send_message(client, operation)
        with pytest.raises(helper.HelperError):
            helper.receive_message(client)
    assert not broker.active


def test_reject_changed_sdk_path(broker):
    with connection_to(broker, handshake=False) as client:
        helper.send_message(client, dict(op='hello', version=1, sdk_root='/different/sdk'))
        with pytest.raises(helper.HelperError, match='路径或协议'):
            helper.receive_message(client)


def test_stop_interrupts_partial_frame_read():
    with _pair() as (client, server):
        stop = threading.Event()
        timer = threading.Timer(.05, stop.set)
        timer.start()
        started = time.monotonic()
        with pytest.raises(InterruptedError):
            helper.read_exact(client, 100, stop)
        timer.join()
        assert time.monotonic() - started < .5


def test_helper_shutdown_releases_active_camera(broker):
    with connection_to(broker) as client:
        helper.send_message(client, {'op': 'frames', 'camera': camera()})
        helper.send_message(client, {'op': 'next'})
        helper.receive_frame(client, helper.receive_message(client), threading.Event())
        broker.shutdown()
    assert not broker.active and broker.counters['closed'] == 1


def test_two_camera_connections_stream_and_stop_independently(broker):
    second = {**sample_record(), 'device': 'orbbec://SECOND/color'}
    broker.inventory_fn = lambda root: {'devices': [sample_record(), second]}
    with connection_to(broker) as first:
        helper.send_message(first, {'op': 'frames', 'camera': camera()})
        helper.send_message(first, {'op': 'next'})
        helper.receive_frame(first, helper.receive_message(first), threading.Event())
        with connection_to(broker) as other:
            helper.send_message(other, {'op': 'frames', 'camera': {**camera(), 'device': second['device']}})
            for _ in range(3):
                helper.send_message(other, {'op': 'next'})
                helper.receive_frame(other, helper.receive_message(other), threading.Event())
                helper.send_message(first, {'op': 'next'})
                helper.receive_frame(first, helper.receive_message(first), threading.Event())
            assert broker.active == {'TEST', 'SECOND'}
        assert broker.active == {'TEST'}
        helper.send_message(first, {'op': 'next'})
        helper.receive_frame(first, helper.receive_message(first), threading.Event())
    assert not broker.active and broker.counters['closed'] == 2


def test_unexpected_helper_exit_reports_camera_identity():
    with _pair() as (client, server):
        def crash():
            helper.receive_message(server)
            helper.receive_message(server)
            server.close()
        worker = threading.Thread(target=crash)
        worker.start()
        frames = helper.helper_frames(client, camera(), threading.Event())
        with pytest.raises(helper.HelperError, match='orbbec://TEST/color.*意外断开'):
            next(frames)
        worker.join(2)
        assert not worker.is_alive()


def test_reject_oversized_message_and_plane_layout():
    with _pair() as (client, server):
        server.sendall(helper.HEADER.pack(helper.MAX_HEADER + 1))
        with pytest.raises(ValueError, match='length'):
            helper.receive_message(client)
        bad = dict(type='frame', width=64, height=64, format='rgb24', size=100,
                   planes=[dict(stride=1, height=64, size=64)])
        with pytest.raises(ValueError, match='layout'):
            helper.receive_frame(client, bad, threading.Event())


def test_absent_or_stale_helper_does_not_masquerade_as_success(socket_directory):
    tmp_path = socket_directory
    path = tmp_path / 'sdk.sock'
    assert helper.connect_helper(tmp_path, path=path) is None
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path))
    with pytest.raises(helper.HelperError, match='已停止'):
        helper.connect_helper(tmp_path, path=path)
    path.unlink()
    path.symlink_to(tmp_path / 'other')
    with pytest.raises(helper.HelperError, match='不是套接字'):
        helper.connect_helper(tmp_path, path=path)


def test_sdk_routes_inventory_and_frames_through_helper(monkeypatch, tmp_path):
    monkeypatch.setattr(sdk.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(sdk, 'configured_root', lambda: tmp_path)
    monkeypatch.setattr(helper, 'helper_inventory', lambda root: {'devices': [sample_record()], 'access': 'local_admin_helper'})
    monkeypatch.setattr(sdk.subprocess, 'run', lambda *a, **kw: pytest.fail('Do not query SDK directly when helper is running'))
    assert sdk.inventory()['access'] == 'local_admin_helper'
    monkeypatch.setattr(helper, 'connect_helper', lambda root: 'connection')
    monkeypatch.setattr(helper, 'helper_frames', lambda *a: iter([('frame', 42, {})]))
    assert list(sdk.frames(camera(), threading.Event())) == [('frame', 42, {})]


def test_client_authenticates_server_before_sending_request(socket_directory):
    tmp_path = socket_directory
    path = tmp_path / 'sdk.sock'
    received = []
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        listener.listen()
        def accept():
            with listener.accept()[0] as connection:
                received.append(connection.recv(4096))
        thread = threading.Thread(target=accept)
        thread.start()
        with pytest.raises(helper.HelperError, match='管理员身份'):
            helper.connect_helper(tmp_path, path=path, required_uid=os.getuid() + 1)
        thread.join(2)
        assert not thread.is_alive() and received == [b'']


def test_real_client_pull_protocol_and_stop(broker, socket_directory):
    path = socket_directory / 'sdk.sock'
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(path))
        listener.listen()
        def accept():
            server = listener.accept()[0]
            broker.connections.add(server)
            broker.handle(server)
        thread = threading.Thread(target=accept, daemon=True)
        thread.start()
        connection = helper.connect_helper(broker.root, path=path, required_uid=os.getuid())
        stop = threading.Event()
        stream = helper.helper_frames(connection, camera(), stop)
        try:
            assert next(stream)[1] == 123456789
            assert next(stream)[2]['sdk_access'] == 'local_admin_helper'
            stop.set()
            with pytest.raises(StopIteration):
                next(stream)
        finally:
            stream.close()
        thread.join(2)
        assert not thread.is_alive() and broker.counters['closed'] == 1


def test_launcher_limits_sudo_to_fixed_helper_and_preserves_user_identity(tmp_path, monkeypatch):
    from tools import start_sdk_helper as launcher
    from video_demo import orbbec
    monkeypatch.setattr(launcher, 'ROOT', tmp_path)
    monkeypatch.setattr(launcher.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(launcher.os, 'geteuid', lambda: 501)
    monkeypatch.setattr(launcher.os, 'getuid', lambda: 501)
    monkeypatch.setattr(launcher.os, 'getgid', lambda: 20)
    monkeypatch.setattr(launcher.sys, 'argv', ['start_sdk_helper.py'])
    monkeypatch.setattr(sdk, 'configured_root', lambda: tmp_path / 'sdk')
    monkeypatch.setattr(orbbec, 'library_path', lambda root: root / 'lib')
    directory = tmp_path / '.local'
    directory.mkdir(mode=0o755)
    # The fixture directory belongs to this test runner's UID on every OS.
    monkeypatch.setattr(launcher.os, 'getuid', lambda: directory.stat().st_uid)
    invoked = []
    def execv(path, args):
        invoked.append((path, args))
        raise SystemExit(0)
    monkeypatch.setattr(launcher.os, 'execv', execv)
    with pytest.raises(SystemExit) as result:
        launcher.main()
    assert result.value.code == 0
    executable, args = invoked[0]
    assert executable == '/usr/bin/sudo' and args[:2] == ['sudo', '--']
    assert args[3:5] == ['-I', '-B'] and args[6] == '--serve'
    assert args[-4:] == ['--owner-uid', str(directory.stat().st_uid), '--owner-gid', '20']
    assert args[7:9] == ['--sdk-root', str(tmp_path / 'sdk')]
    assert directory.stat().st_mode & 0o777 == 0o700


def test_launcher_refuses_direct_root_and_symlink_directory(tmp_path, monkeypatch):
    from tools import start_sdk_helper as launcher
    from video_demo import orbbec
    monkeypatch.setattr(launcher, 'ROOT', tmp_path)
    monkeypatch.setattr(launcher.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(launcher.sys, 'argv', ['start_sdk_helper.py'])
    monkeypatch.setattr(launcher.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(launcher.os, 'execv', lambda *a: pytest.fail('Must not run sudo'))
    with pytest.raises(RuntimeError, match='普通用户'):
        launcher.main()
    monkeypatch.setattr(launcher.os, 'geteuid', lambda: 501)
    monkeypatch.setattr(sdk, 'configured_root', lambda: tmp_path / 'sdk')
    monkeypatch.setattr(orbbec, 'library_path', lambda root: root / 'lib')
    target = tmp_path / 'other'
    target.mkdir(mode=0o755)
    (tmp_path / '.local').symlink_to(target)
    with pytest.raises(RuntimeError, match='真实目录'):
        launcher.main()
    assert target.stat().st_mode & 0o777 == 0o755
