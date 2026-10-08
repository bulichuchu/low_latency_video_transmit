"""Vue control plane, default loopback; opt-in LAN and direct HTTPS support."""
from __future__ import annotations

import argparse
import asyncio
from collections import deque
import contextlib
import json
import math
import os
from pathlib import Path
import secrets
import ipaddress
import ssl
import subprocess
import sys
import threading
import time
import webbrowser
import urllib.request
from urllib.parse import urlsplit

from aiohttp import web, WSMsgType

from .cli import ROOT, parser, stop_child, validate
from .network import child_camera_profile, redact
from .webbridge import BrowserBridge

DIST = ROOT / 'frontend' / 'dist'
VIEWER_STATE_CHECK_S = .1  # an idle viewer loop re-checks its session this often


class InputParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def media_args(role, data, directory):
    """Whitelist web controls, then reuse CLI ranges. Never accept shell arguments."""
    if not isinstance(data, dict):
        raise ValueError('Configuration must be an object')
    p = parser()
    argv = ['send' if role == 'sender' else 'receive']
    if role == 'sender' and data.get('source', 'camera') != 'camera':
        raise ValueError('Only camera inputs are supported; select local, SDK or RTSP cameras')
    fields = ['width', 'height', 'fps', 'streams']
    fields += (['host', 'port', 'encoder', 'bitrate_kbps'] if role == 'sender' else
               ['bind', 'port', 'clock_mode', 'sync_mode', 'sync_wait_ms', 'sync_tolerance_ms',
                'max_age_ms', 'display_fps', 'reorder_ms'])
    for key in fields:
        if key in data:
            value = data[key]
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise ValueError(f'Invalid {key}')
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError(f'Invalid {key}')
            argv += ['--' + key.replace('_', '-'), str(value)]
    argv += ['--output', str(directory)]
    # argparse subparsers also call error, so translate SystemExit without exiting the server.
    try:
        args = p.parse_args(argv)
        profile = dict(version=1, cameras=data.get('cameras')) if role == 'sender' else None
        validate(InputParser(), args, camera_profile=profile)
    except SystemExit as exc:
        raise ValueError('Invalid configuration or codec choice') from exc
    if role == 'receiver':
        args.ui_backend = 'webcodecs'
    return args


class Session:
    """One independent sender process and one receiver worker per local service."""
    def __init__(self, role, args):
        self.role, self.args = role, args
        self.directory = Path(args.output)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.process = self.thread = self.logfile = None
        self.error = None
        self.finished = False
        self.bridge = BrowserBridge(args.streams) if role == 'receiver' else None
        self.websocket = None
        self.rows = deque(maxlen=60)
        self.counters = {}
        self.offset = 0
        self.events_tail = b''
        self.events = deque()
        self.first_tx_ns = self.latest_tx_ns = None
        self.config = redact(vars(args))
        self.stopping = False

    def launch(self):
        if self.role == 'sender':
            argv = [sys.executable, '-u', '-m', 'video_demo', 'send']
            env = dict(os.environ)
            for key in ('host', 'port', 'streams', 'width', 'height', 'fps', 'encoder', 'bitrate_kbps', 'output'):
                argv += ['--' + key.replace('_', '-'), str(getattr(self.args, key))]
            profile, camera_env = child_camera_profile(self.args.camera_settings)
            profile_path = self.directory / 'camera-inputs.json'
            profile_path.write_text(json.dumps(redact(profile), ensure_ascii=False))
            env.update(camera_env)
            argv += ['--camera-profile', str(profile_path)]
            self.logfile = (self.directory / 'process.log').open('w')
            self.process = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=self.logfile,
                                            stderr=subprocess.STDOUT,
                                            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)
            self.process._stop_file = self.directory / 'STOP'
            self.process._stop_timeout = 15
        else:
            from .receiver import run_receiver
            def worker():
                try:
                    run_receiver(self.args, web_sink=self.bridge)
                except Exception as exc:
                    self.error = redact(str(exc))
                finally:
                    self.finished = True
            self.thread = threading.Thread(target=worker, name='vue-receiver', daemon=True)
            self.thread.start()

    @property
    def running(self):
        return self.process.poll() is None if self.process else bool(self.thread and self.thread.is_alive())

    def stop(self):
        self.stopping = True
        (self.directory / 'STOP').touch()
        if self.process:
            stop_child(self.process)
            if self.logfile:
                self.logfile.close()
        if self.thread:
            self.thread.join(timeout=10)
            if self.thread.is_alive():
                raise RuntimeError('接收端仍在退出，请稍后重试。')
        self.finished = True

    def snapshot(self):
        path = self.directory / 'events.jsonl'
        if path.exists():
            with path.open('rb') as f:
                f.seek(self.offset)
                chunk = f.read(2 * 1024 * 1024)
                self.offset = f.tell()
            parts = (self.events_tail + chunk).split(b'\n')
            self.events_tail = parts.pop()
            for line in parts:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                kind = row.get('event')
                if kind in ('error', 'camera_opened', 'network_disconnected', 'encoder'):
                    self.rows.append(redact(row))
                if kind == 'tx':
                    self.events.append((row['time_ns'], row['stream'], row.get('wire_bytes', 0)))
                    self.counters[row['stream']] = self.counters.get(row['stream'], 0) + 1
                    if self.first_tx_ns is None:
                        self.first_tx_ns = row['time_ns']
                    self.latest_tx_ns = row['time_ns']
        now = time.perf_counter_ns()
        # Journal flushes asynchronously (up to 1s behind wall time). Measure
        # the event-time window, otherwise every flush looks like a FPS dip.
        end = self.latest_tx_ns if self.latest_tx_ns is not None else now
        start = max(self.first_tx_ns if self.first_tx_ns is not None else end, end - 2_000_000_000)
        lag_ms = (now - end) / 1e6 if self.latest_tx_ns is not None else None
        stale = lag_ms is None or lag_ms > 2000
        while self.events and self.events[0][0] <= start:
            self.events.popleft()
        seconds = max((end - start) / 1e9, 1e-9)
        samples = [dict(stream=i, fps=0 if stale else sum(s == i for _, s, _ in self.events) / seconds,
                        mbps=0 if stale else sum(b for _, s, b in self.events if s == i) * 8 / (seconds * 1e6),
                        sample_lag_ms=lag_ms,
                        frames=self.counters.get(i, 0)) for i in range(self.args.streams)]
        running = self.running
        if self.process and not running and self.process.returncode and not self.stopping:
            with (self.directory / 'process.log').open('rb') as f:
                f.seek(max(0, f.seek(0, 2) - 3500))
                self.error = redact(f.read().decode(errors='replace'))
        return dict(state='error' if self.error else 'running' if running else 'stopped',
                    error=self.error, directory=str(self.directory), config=self.config,
                    samples=samples, events=list(self.rows),
                    receiver=self.bridge.snapshot() if self.bridge else None,
                    report_ready=(self.directory / 'summary.json').exists())


class Controller:
    def __init__(self, output_root=None):
        self.sessions = {}
        self.lock = asyncio.Lock()
        self.output_root = Path(output_root) if output_root else ROOT / 'runs'

    async def start(self, role, data):
        async with self.lock:
            previous = self.sessions.get(role)
            if previous and previous.running:
                raise ValueError('该端已在运行，请先停止后再修改配置。')
            if previous:
                if previous.websocket:
                    await previous.websocket.close()
                await asyncio.to_thread(previous.stop)
            directory = self.output_root / f'{time.strftime("%Y%m%d-%H%M%S")}-web-{role}-{secrets.token_hex(3)}'
            args = media_args(role, data, directory)
            session = Session(role, args)
            self.sessions[role] = session
            session.launch()
            if role == 'receiver':
                for _ in range(400):
                    if (directory / 'ready.json').exists():
                        break
                    if not session.running:
                        raise ValueError(session.error or 'Receiver failed to start')
                    await asyncio.sleep(.05)
                else:
                    await asyncio.to_thread(session.stop)
                    raise ValueError('Receiver startup timed out')
            return session.snapshot()

    async def stop(self, role):
        async with self.lock:
            session = self.sessions.get(role)
            if session:
                if session.websocket:
                    await session.websocket.close()
                await asyncio.to_thread(session.stop)
            return session.snapshot() if session else {'state': 'idle'}


def normalized_hostname(value):
    """Validate one explicit hostname/IP; never accept a URL, port or wildcard."""
    if not isinstance(value, str) or not value or any(c.isspace() for c in value):
        raise ValueError('Invalid allowed hostname')
    try:
        return str(ipaddress.ip_address(value.strip('[]')))
    except ValueError:
        pass
    name = value.rstrip('.').encode('idna').decode().lower()
    labels = name.split('.')
    if len(name) > 253 or any(not label or len(label) > 63 or label[0] == '-' or label[-1] == '-'
        or not all(c.isascii() and (c.isalnum() or c == '-') for c in label) for label in labels):
        raise ValueError('--allow-host needs a hostname or IP without http://, path, wildcard or port')
    return name


def web_settings(args):
    """Validate launch settings before loading an app or starting a listener."""
    if not 1 <= args.port <= 65535:
        raise ValueError('Web port must be 1..65535')
    try:
        bind_ip = ipaddress.ip_address(args.bind)
    except ValueError:
        raise ValueError('--bind must be a local listen IP, for example 127.0.0.1 or 0.0.0.0') from None
    names = [normalized_hostname(value) for value in args.allow_host]
    hosts = {'localhost', '127.0.0.1', '::1', *names}
    if bind_ip.is_unspecified and not names:
        raise ValueError('监听所有接口时，请用 --allow-host 指定要访问的域名或 IP。')
    if not bind_ip.is_unspecified:
        hosts.add(str(bind_ip))
    if bool(args.tls_cert) != bool(args.tls_key):
        raise ValueError('--tls-cert and --tls-key must be supplied together')
    context = None
    if args.tls_cert:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(args.tls_cert, args.tls_key)
    scheme = 'https' if context else 'http'
    display_host = names[0] if names else str(bind_ip)
    if ':' in display_host:
        display_host = f'[{display_host}]'
    url = f'{scheme}://{display_host}:{args.port}/#/{args.page}'
    return hosts, context, url


def create_app(output_root=None, allowed_hosts=None):
    controller = Controller(output_root)
    token = secrets.token_urlsafe(32)
    hosts = {normalized_hostname(host) for host in (allowed_hosts or ('127.0.0.1', 'localhost', '::1'))}

    @web.middleware
    async def permitted_origin(request, handler):
        host = request.host
        try:
            parsed = urlsplit('//' + host)
            hostname = normalized_hostname(parsed.hostname)
            port = parsed.port
            if parsed.username is not None or parsed.password is not None or parsed.path or parsed.query or parsed.fragment:
                raise ValueError('Invalid Host')
            if port is not None and not 1 <= port <= 65535:
                raise ValueError('Invalid Host port')
        except (ValueError, UnicodeError):
            raise web.HTTPForbidden(text='Invalid Host') from None
        if hostname not in hosts:
            raise web.HTTPForbidden(text='Host not allowed; configure --allow-host on the server')
        origin = request.headers.get('Origin')
        if origin and origin != f'{request.scheme}://{host}':
            raise web.HTTPForbidden(text='Cross-origin request rejected')
        if request.method != 'GET' and request.headers.get('X-Video-Token') != token:
            raise web.HTTPForbidden(text='Missing UI token')
        try:
            response = await handler(request)
        except (ValueError, RuntimeError, OSError) as exc:
            response = web.json_response({'error': redact(str(exc))}, status=400)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    app = web.Application(middlewares=[permitted_origin], client_max_size=128 * 1024)

    async def bootstrap(request):
        from .sdk import configured_root
        return web.json_response(dict(app='video-flow', token=token, sdk_root=str(configured_root() or '')))

    async def inventory(request):
        from .cameras import inventory as camera_inventory, capture_modes
        from .sdk import inventory as sdk_inventory
        info = await asyncio.to_thread(sdk_inventory if request.query.get('sdk') == '1' else camera_inventory)
        for record in info['devices']:
            duplicate = sum(d['name'] == record['name'] for d in info['devices']) > 1
            record['device'] = record.get('device', str(record['index_hint']) if duplicate and 'index_hint' in record else record['name'])
            record['modes'] = capture_modes(record, info.get('backend'))
        return web.json_response(info)

    async def sdk_root(request):
        from .sdk import configure_root
        data = await request.json()
        if not isinstance(data.get('path'), str):
            raise ValueError('SDK path is required')
        root = await asyncio.to_thread(configure_root, data['path'])
        return web.json_response({'path': str(root)})

    def role_of(request):
        role = request.match_info['role']
        if role not in ('sender', 'receiver'):
            raise web.HTTPNotFound()
        return role

    async def status(request):
        session = controller.sessions.get(role_of(request))
        return web.json_response(session.snapshot() if session else {'state': 'idle'})

    async def start(request):
        return web.json_response(await controller.start(role_of(request), await request.json()))

    async def stop(request):
        return web.json_response(await controller.stop(role_of(request)))

    async def report(request):
        session = controller.sessions.get(role_of(request))
        if not session or not (session.directory / 'summary.json').exists():
            raise web.HTTPNotFound(text='Stop the session to generate its report')
        return web.FileResponse(session.directory / 'summary.json', headers={
            'Content-Disposition': f'attachment; filename="{session.role}-summary.json"'})

    async def video(request):
        if request.query.get('token') != token:
            raise web.HTTPForbidden()
        session = controller.sessions.get('receiver')
        if not session or not session.running:
            raise web.HTTPConflict(text='Start the receiver first')
        if session.websocket is not None:
            raise web.HTTPConflict(text='Only one active video viewer is supported')
        ws = web.WebSocketResponse(heartbeat=10, max_msg_size=512 * 1024, compress=False)
        session.websocket = ws  # Reserve before prepare yields.
        bridge = session.bridge
        loop = asyncio.get_running_loop()
        queued = asyncio.Event()

        def wake():
            # Receiver thread, once per queued frame.
            with contextlib.suppress(RuntimeError):  # loop already closed at shutdown
                loop.call_soon_threadsafe(queued.set)

        async def transmit():
            while not ws.closed and session.running:
                packet = bridge.pop()
                if packet:
                    # Bound the TCP path. A slow viewer reconnects at an IDR.
                    await asyncio.wait_for(ws.send_bytes(packet), timeout=.25)
                    continue
                # No polling: bridge.offer() wakes this at once. A set() scheduled
                # before clear() runs only after this coroutine yields, so no
                # frame is missed. The timeout only re-checks the session.
                queued.clear()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(queued.wait(), timeout=VIEWER_STATE_CHECK_S)
        sender_task = None
        try:
            await ws.prepare(request)
            bridge.attach(True, notify=wake)
            await ws.send_json(dict(type='config', **session.config))
            sender_task = asyncio.create_task(transmit())
            def finished(task):
                if not task.cancelled():
                    with contextlib.suppress(Exception):
                        task.result()
                    asyncio.create_task(ws.close())
            sender_task.add_done_callback(finished)
            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    data = json.loads(message.data)
                    if not isinstance(data, dict):
                        continue
                    if data.get('type') == 'ping':
                        now = time.perf_counter_ns() / 1e6
                        await ws.send_json(dict(type='pong', t1=data.get('t1'), t2=now,
                                               t3=time.perf_counter_ns() / 1e6))
                    elif data.get('type') == 'key':
                        bridge.browser_recover(data.get('stream'), data.get('reason'))
                    elif data.get('type') == 'telemetry':
                        bridge.telemetry(data.get('frames'))
        finally:
            bridge.attach(False)
            if sender_task:
                sender_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await sender_task
            session.websocket = None
        return ws

    async def index(request):
        if not (DIST / 'index.html').exists():
            raise web.HTTPServiceUnavailable(text='Build UI first: cd frontend && npm ci && npm run build')
        return web.FileResponse(DIST / 'index.html')

    async def cleanup(app):
        for role in list(controller.sessions):
            await controller.stop(role)

    app.router.add_get('/api/bootstrap', bootstrap)
    app.router.add_get('/api/cameras', inventory)
    app.router.add_post('/api/sdk', sdk_root)
    app.router.add_get('/api/{role}/status', status)
    app.router.add_post('/api/{role}/start', start)
    app.router.add_post('/api/{role}/stop', stop)
    app.router.add_get('/api/{role}/report', report)
    app.router.add_get('/api/video', video)
    if (DIST / 'assets').exists():
        app.router.add_static('/assets/', DIST / 'assets')
    app.router.add_get('/', index)
    app.on_cleanup.append(cleanup)
    return app


def run_web(args):
    hosts, context, url = web_settings(args)
    if not (DIST / 'index.html').exists():
        raise RuntimeError('请先在 frontend 目录运行 npm ci && npm run build')
    # A local-only service must never silently absorb an explicit LAN/TLS launch.
    # Configuration changes require the user to stop the old process first.
    if args.bind == '127.0.0.1' and not args.allow_host and context is None:
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{args.port}/api/bootstrap', timeout=.5) as response:
                existing = json.load(response)
            if existing.get('app') == 'video-flow':
                print(f'复用已运行的本机服务：{url}', flush=True)
                if not args.no_browser:
                    webbrowser.open(url)
                return 0
        except (OSError, ValueError):
            pass
    print(f'Vue UI: {url}\nCtrl+C 停止服务及本机收发任务。关闭网页不会停止传输。', flush=True)
    if context is None and (args.allow_host or not ipaddress.ip_address(args.bind).is_loopback):
        print('远程域名 HTTP 可访问控制页面；视频预览需浏览器信任的 HTTPS，或 SSH 转发后用 localhost 访问。', flush=True)
    if not args.no_browser:
        threading.Timer(.8, lambda: webbrowser.open(url)).start()
    web.run_app(create_app(allowed_hosts=hosts), host=args.bind, port=args.port, ssl_context=context, access_log=None)
    return 0
