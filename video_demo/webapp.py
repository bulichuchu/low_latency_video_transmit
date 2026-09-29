"""Local Vue control plane; LAN video continues to use the existing UDP protocol."""
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
import subprocess
import sys
import threading
import time
import webbrowser
import urllib.request

from aiohttp import web, WSMsgType

from .cli import ROOT, parser, stop_child, validate
from .network import child_camera_profile, redact
from .webbridge import BrowserBridge

DIST = ROOT / 'frontend' / 'dist'


class InputParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def media_args(role, data, directory):
    """Whitelist web controls, then reuse CLI ranges. Never accept shell arguments."""
    if not isinstance(data, dict):
        raise ValueError('Configuration must be an object')
    p = parser()
    argv = ['send' if role == 'sender' else 'receive']
    if role == 'sender':
        argv += ['--source', 'synthetic']  # Validate common fields without opening a GUI.
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
        if role == 'sender' and data.get('source', 'camera') != 'synthetic':
            from .cameras import validate_profile
            profile = validate_profile(dict(version=1, cameras=data.get('cameras')))
            args.source = 'camera'
            args.camera_settings = profile['cameras']
            if args.streams is not None and args.streams != len(args.camera_settings):
                raise ValueError('Camera count must match streams')
            args.streams = len(args.camera_settings)
            # validate() would reload a profile or open the picker. Validate the
            # common output parameters as synthetic, then restore camera inputs.
            args.source = 'synthetic'
            validate(InputParser(), args)
            args.source = 'camera'
        else:
            validate(InputParser(), args)
    except SystemExit as exc:
        raise ValueError('Invalid configuration or codec choice') from exc
    if role == 'receiver':
        args.headless = True  # run_receiver's main loop has no Qt surface.
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
        self.config = redact(vars(args))
        self.stopping = False

    def launch(self):
        if self.role == 'sender':
            argv = [sys.executable, '-u', '-m', 'video_demo', 'send']
            env = dict(os.environ)
            for key in ('host', 'port', 'streams', 'width', 'height', 'fps', 'encoder', 'bitrate_kbps', 'output'):
                argv += ['--' + key.replace('_', '-'), str(getattr(self.args, key))]
            if self.args.source == 'camera':
                profile, camera_env = child_camera_profile(self.args.camera_settings)
                profile_path = self.directory / 'camera-inputs.json'
                profile_path.write_text(json.dumps(redact(profile), ensure_ascii=False))
                env.update(camera_env)
                argv += ['--camera-profile', str(profile_path)]
            else:
                argv += ['--source', 'synthetic']
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
        now = time.perf_counter_ns()
        while self.events and now - self.events[0][0] > 2_000_000_000:
            self.events.popleft()
        samples = [dict(stream=i, fps=sum(s == i for _, s, _ in self.events) / 2,
                        mbps=sum(b for _, s, b in self.events if s == i) * 8 / 2e6,
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


def create_app(output_root=None):
    controller = Controller(output_root)
    token = secrets.token_urlsafe(32)

    @web.middleware
    async def local_only(request, handler):
        host = request.host
        if host.split(':')[0] not in ('127.0.0.1', 'localhost'):
            raise web.HTTPForbidden(text='Local UI only')
        origin = request.headers.get('Origin')
        if origin and origin != f'http://{host}':
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

    app = web.Application(middlewares=[local_only], client_max_size=128 * 1024)

    async def bootstrap(request):
        from .sdk import configured_root
        return web.json_response(dict(app='video-flow', token=token, sdk_root=str(configured_root() or ''),
                                      glass_to_glass_latency_ms=None))

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
        async def transmit():
            while not ws.closed and session.running:
                packet = bridge.pop()
                if packet:
                    # Bound the TCP path. A slow viewer reconnects at an IDR.
                    await asyncio.wait_for(ws.send_bytes(packet), timeout=.25)
                else:
                    await asyncio.sleep(.002)
        sender_task = None
        try:
            await ws.prepare(request)
            bridge.attach(True)
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
                        bridge.recover(data.get('stream'))
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
    if not 1 <= args.port <= 65535:
        raise ValueError('Web port must be 1..65535')
    if not (DIST / 'index.html').exists():
        raise RuntimeError('请先在 frontend 目录运行 npm ci && npm run build')
    url = f'http://127.0.0.1:{args.port}/#/{args.page}'
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
    if not args.no_browser:
        threading.Timer(.8, lambda: webbrowser.open(url)).start()
    web.run_app(create_app(), host='127.0.0.1', port=args.port, access_log=None)
    return 0
