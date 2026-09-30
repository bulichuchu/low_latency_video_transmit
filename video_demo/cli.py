from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import signal
import socket
import subprocess
import sys
import time

from .network import child_camera_profile, is_rtsp, redact, validate_network_camera
from .sdk import is_sdk

ROOT = Path(__file__).resolve().parents[1]


def output_path(kind):
    return str(ROOT / 'runs' / f'{time.strftime("%Y%m%d-%H%M%S")}-{kind}-{os.getpid()}')


def parser():
    p = argparse.ArgumentParser(description='H.264/RTP LAN demo: measured multi-stream delivery')
    sub = p.add_subparsers(dest='command', required=True)
    a = sub.add_parser('web', help='Vue sender/receiver UI; loopback by default, optional LAN/HTTPS')
    a.add_argument('--bind', default='127.0.0.1', help='HTTP listen IP; use 0.0.0.0 for LAN IPv4')
    a.add_argument('--allow-host', action='append', default=[],
                   help='Allowed UI hostname or IP without port; repeat for multiple names')
    a.add_argument('--tls-cert', help='HTTPS certificate chain in PEM format')
    a.add_argument('--tls-key', help='HTTPS private key in PEM format')
    a.add_argument('--port', type=int, default=8765)
    a.add_argument('--page', choices=['sender', 'receiver'], default='sender')
    a.add_argument('--no-browser', action='store_true')
    sub.add_parser('doctor', help='Check codec initialization and runtime without capturing or generating video')
    a = sub.add_parser('cameras', help='List real cameras and supported capture modes without capturing images')
    a.add_argument('--output', help='Optional inventory JSON file')
    a.add_argument('--sdk', action='store_true', help='Also query configured vendor SDK image modes')
    a = sub.add_parser('sdk', help='Configure/query an optional Orbbec SDK v2 installation')
    a.add_argument('--orbbec-root', help='SDK package root; saved locally for the launcher and child processes')
    a = sub.add_parser('optical', help='Measure scene-to-screen delay from an external high-speed recording')
    a.add_argument('video')
    a.add_argument('--capture-fps', type=float, required=True,
                   help='Actual physical recording FPS, NOT slow-motion playback FPS; original constant-rate recording required')
    a.add_argument('--source-roi', help='Original light/scene ROI: x,y,width,height')
    a.add_argument('--screen-roi', help='Corresponding light on receiver screen: x,y,width,height')
    a.add_argument('--select-rois', action='store_true', help='Select both regions on the first video frame')
    a.add_argument('--max-delay-ms', type=float, default=500)
    a.add_argument('--output', default=None)
    a = sub.add_parser('analyze', help='Regenerate CSV and summary from events.jsonl')
    a.add_argument('directory')
    for name in ('demo', 'send', 'receive'):
        a = sub.add_parser(name)
        a.add_argument('--streams', type=int, default=None)
        a.add_argument('--width', type=int, default=None)
        a.add_argument('--height', type=int, default=None)
        a.add_argument('--fps', type=int, default=None)
        a.add_argument('--duration', '--seconds', type=float, default=0)
        a.add_argument('--output', default=None)
        if name != 'receive':
            a.add_argument('--cameras', default=None, help='Comma-separated device names/indices or orbbec://SERIAL/color; GUI picker when omitted')
            a.add_argument('--rtsp-url', action='append', default=[], help='Network/PoE camera RTSP URL; repeat for multiple cameras')
            a.add_argument('--rtsp-transport', choices=['tcp', 'udp'], default=None,
                           help='RTSP media transport (default tcp); applies to network inputs only')
            a.add_argument('--camera-profile', help='Optional generic JSON with per-camera capture settings')
            a.add_argument('--capture-format', help='Native pixel format (macOS/Windows) or V4L2 input format')
            a.add_argument('--capture-mode', choices=['auto', 'exact'], default='auto',
                           help='auto selects each device supported mode; exact requires requested capture size/rate')
            a.add_argument('--encoder', choices=['auto', 'libx264', 'h264_videotoolbox', 'h264_nvenc', 'h264_qsv'], default='auto')
            a.add_argument('--bitrate-kbps', type=int, default=None, help='Per-stream target, default 3000')
            a.add_argument('--mtu', type=int, default=1200)
            a.add_argument('--loss', type=float, default=0, help='Deliberately discard this fraction of RTP packets')
            a.add_argument('--seed', type=int, default=7)
            a.add_argument('--link-mbps', type=float, default=0, help='Optional total RTP send pacing limit; 0 disables')
        if name != 'send':
            a.add_argument('--headless', action='store_true')
            a.add_argument('--save-preview', action='store_true', help='Explicitly save received camera images on exit')
            a.add_argument('--decoder', choices=['auto', 'software', 'videotoolbox', 'cuda', 'd3d11va'], default='auto')
            a.add_argument('--reorder-ms', type=float, default=20)
            a.add_argument('--sync-tolerance-ms', type=float, default=18)
            a.add_argument('--sync-wait-ms', type=float, default=None,
                           help='Pairing wait budget; default half a frame, capped at 20ms (0 for one stream)')
            a.add_argument('--max-age-ms', type=float, default=100)
            a.add_argument('--strict-sync', action='store_true')
            a.add_argument('--sync-mode', choices=['aligned', 'latest'], default='aligned',
                           help='aligned waits briefly for matching cameras; latest presents each camera immediately')
            a.add_argument('--display-fps', type=int, default=60)
        if name == 'receive':
            a.add_argument('--bind', default='0.0.0.0')
            a.add_argument('--port', type=int, default=5004)
            a.add_argument('--clock-mode', choices=['estimated', 'shared'], default='estimated',
                           help='shared ONLY for processes on the same computer')
        elif name == 'send':
            a.add_argument('--host', default='127.0.0.1')
            a.add_argument('--port', type=int, default=5004)
    return p


def validate(p, args, *, camera_profile=None):
    """Validate CLI options or an in-memory camera profile supplied by the web UI."""
    if args.command in ('doctor', 'analyze', 'cameras', 'optical', 'sdk', 'web'):
        return
    if args.command == 'send':
        args.host = args.host.strip()
        if not args.host:
            p.error('接收机地址不能为空，请填写 IP 或域名。')
    defaults = dict(width=1280, height=720, fps=30, bitrate_kbps=3000)
    if args.command in ('send', 'demo'):
        from .cameras import read_profile, select_camera_profile, validate_profile
        picked = False
        if camera_profile is not None or args.camera_profile:
            if args.cameras or args.rtsp_url:
                p.error('Use --camera-profile or --cameras/--rtsp-url, not both')
            if camera_profile is not None:
                profile = validate_profile(camera_profile)
            else:
                args.camera_profile = str(Path(args.camera_profile).resolve())
                profile = read_profile(args.camera_profile)
            defaults.update(profile.get('output', {}))
            args.camera_settings = profile['cameras']
        else:
            if not args.cameras and not args.rtsp_url:
                if args.command != 'demo' or args.headless:
                    p.error('Specify --cameras, --rtsp-url or --camera-profile for real camera input')
                initial_output = {key: getattr(args, key) if getattr(args, key) is not None else value
                                  for key, value in defaults.items()}
                profile = select_camera_profile(initial_output, args.capture_format, args.capture_mode == 'exact')
                args.camera_settings = profile['cameras']
                # CLI output values initialize the visible controls. The user's
                # final choices must be the values passed to BOTH child processes.
                for key, value in profile['output'].items():
                    setattr(args, key, value)
                picked = True
            else:
                devices = [d.strip() for d in (args.cameras or '').split(',') if d.strip()]
                for url in args.rtsp_url:
                    validate_network_camera({'device': url})
                devices.extend(args.rtsp_url)
                args.camera_settings = [{'device': device} for device in devices]
        if not args.camera_settings:
            p.error('No cameras configured')
        if args.capture_format and not picked:
            for camera in args.camera_settings:
                if not is_rtsp(camera['device']):
                    camera['pixel_format'] = args.capture_format
        for camera in args.camera_settings:
            if is_sdk(camera['device']):
                from .sdk import validate_camera
                validate_camera(camera)
            elif is_rtsp(camera['device']):
                if args.rtsp_transport:
                    camera['rtsp_transport'] = args.rtsp_transport
                validate_network_camera(camera)
            elif '://' in camera['device']:
                p.error('Camera URI must use rtsp://, rtsps:// or orbbec://')
        args.streams = len(args.camera_settings) if args.streams is None else args.streams
        if len(args.camera_settings) != args.streams:
            p.error('Camera count must match --streams')
    args.streams = 1 if args.streams is None else args.streams
    for key, value in defaults.items():
        if hasattr(args, key) and getattr(args, key) is None:
            setattr(args, key, value)
    if not 1 <= args.streams <= 8:
        p.error('--streams must be 1..8')
    if not 64 <= args.width <= 3840 or not 64 <= args.height <= 2160 or args.width % 2 or args.height % 2:
        p.error('dimensions must be even, width 64..3840 and height 64..2160')
    if not 1 <= args.fps <= 120 or args.duration < 0:
        p.error('fps must be 1..120 and duration must be nonnegative')
    if getattr(args, 'headless', False) and not args.duration:
        p.error('--headless requires a positive --duration')
    if hasattr(args, 'loss'):
        if not 0 <= args.loss < 1 or not 256 <= args.mtu <= 1400 or args.bitrate_kbps < 100 or args.link_mbps < 0:
            p.error('invalid loss, MTU, bitrate, or pacing limit')
    if hasattr(args, 'reorder_ms'):
        if args.sync_mode == 'latest' and args.strict_sync:
            p.error('--sync-mode latest cannot be combined with --strict-sync')
        if args.sync_wait_ms is None:
            args.sync_wait_ms = min(20, 500 / args.fps) if args.streams > 1 else 0
        if not 1 <= args.reorder_ms <= 100 or not 0 <= args.sync_wait_ms <= 50 or not 0 <= args.sync_tolerance_ms <= 100:
            p.error('invalid reordering/synchronization budget')
        if not 10 <= args.max_age_ms <= 1000 or not 1 <= args.display_fps <= 120:
            p.error('invalid frame age/display rate')
    if hasattr(args, 'port') and not (0 if args.command == 'receive' else 1) <= args.port <= 65535:
        p.error('invalid UDP port')
    args.output = str(Path(args.output or output_path(args.command)).resolve())
    if (Path(args.output) / 'config.json').exists() or (Path(args.output) / 'receiver' / 'config.json').exists():
        p.error('output already contains a run; choose a new directory')


def doctor():
    import av
    from .media import encoder_candidates, make_encoder, make_decoder
    result = dict(python=sys.version.split()[0], platform=platform.platform(), pyav=av.__version__,
                  ffmpeg_libraries=av.library_versions, probe_scope='codec_initialization_only',
                  encoder_probes=[])
    for name in encoder_candidates('auto'):
        entry = {'name': name}
        try:
            make_encoder(name, 320, 180, 60, 1_000_000)
            decoder, selected = make_decoder('auto')
            entry.update(ok=True, decoder=selected, decoder_hardware=bool(decoder.is_hwaccel))
        except Exception as exc:
            entry.update(ok=False, error=str(exc))
        result['encoder_probes'].append(entry)
    try:
        from PySide6 import __version__
        result['qt_version'] = __version__
    except ImportError:
        result['qt_version'] = None
    print(json.dumps(result, indent=2))
    return 0 if any(e['ok'] for e in result['encoder_probes']) else 1


def stop_child(process):
    if process.poll() is not None:
        return
    stop_file = getattr(process, '_stop_file', None)
    if stop_file:
        stop_file.parent.mkdir(parents=True, exist_ok=True)
        stop_file.touch()
        try:
            process.wait(timeout=getattr(process, '_stop_timeout', 5))
            return
        except subprocess.TimeoutExpired:
            pass
    try:
        process.send_signal(signal.CTRL_BREAK_EVENT if os.name == 'nt' else signal.SIGINT)
        process.wait(timeout=8)
    except (subprocess.TimeoutExpired, OSError):
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def run_demo(args):
    from .cameras import configure_camera_inputs
    configure_camera_inputs(args)
    directory = Path(args.output)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'config.json').write_text(json.dumps(redact(vars(args)), indent=2))
    # Pass selected inputs to the child, not the original common output settings.
    profile, camera_env = child_camera_profile(args.camera_settings)
    (directory / 'camera-inputs.json').write_text(json.dumps(
        redact(profile), indent=2, ensure_ascii=False))
    children = []
    logs = []

    def launch(role, arguments):
        f = (directory / f'{role}.log').open('w')
        logs.append(f)
        proc = subprocess.Popen([sys.executable, '-u', '-m', 'video_demo', role, *arguments],
                                cwd=ROOT, stdout=f, stderr=subprocess.STDOUT,
                                env={**os.environ, **(camera_env if role == 'send' else {})},
                                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)
        children.append(proc)
        proc._stop_file = directory / ('receiver' if role == 'receive' else 'sender') / 'STOP'
        if role == 'send':
            network_timeout = max((max(s.get('open_timeout_s', 3), s.get('read_timeout_s', 2))
                for s in getattr(args, 'camera_settings', []) if is_rtsp(s['device'])), default=0)
            proc._stop_timeout = max(5, network_timeout + 3)
        return proc

    common = ['--streams', str(args.streams), '--width', str(args.width), '--height', str(args.height), '--fps', str(args.fps)]
    rx_options = ['--bind', '127.0.0.1', '--port', '0', '--clock-mode', 'shared',
                  '--output', str(directory / 'receiver')]
    for key in ('decoder', 'reorder_ms', 'sync_tolerance_ms', 'sync_wait_ms', 'sync_mode', 'max_age_ms', 'display_fps'):
        rx_options += ['--' + key.replace('_', '-'), str(getattr(args, key))]
    if args.headless:
        rx_options += ['--headless']
    if args.strict_sync:
        rx_options += ['--strict-sync']
    if args.save_preview:
        rx_options += ['--save-preview']
    if args.duration:
        rx_options += ['--duration', str(args.duration + 30)]  # Parent stops after sender flush/tail.
    codes = []
    try:
        receiver = launch('receive', common + rx_options)
        ready = directory / 'receiver' / 'ready.json'
        deadline = time.perf_counter() + 30
        while not ready.exists():
            if receiver.poll() is not None or time.perf_counter() > deadline:
                raise RuntimeError(f'Receiver did not start. See {directory}/receive.log')
            time.sleep(.05)
        port = json.loads(ready.read_text())['port']
        tx_options = ['--host', '127.0.0.1', '--port', str(port), '--output', str(directory / 'sender')]
        for key in ('encoder', 'bitrate_kbps', 'mtu', 'loss', 'seed', 'link_mbps', 'duration'):
            if getattr(args, key) is None:
                continue
            tx_options += ['--' + key.replace('_', '-'), str(getattr(args, key))]
        tx_options += ['--camera-profile', str(directory / 'camera-inputs.json'), '--capture-mode', 'exact']
        sender = launch('send', common + tx_options)
        print(f'Demo running: {args.streams} x {args.width}x{args.height}@{args.fps}, UDP 127.0.0.1:{port}', flush=True)
        print(f'Logs and results: {directory}', flush=True)
        print('Close the window or press Ctrl+C to stop both processes.', flush=True)
        while receiver.poll() is None and sender.poll() is None:
            time.sleep(.1)
        if sender.poll() is not None and sender.returncode == 0:
            time.sleep(.15)
        for child in children:
            if child.poll() is not None and child.returncode:
                codes.append(child.returncode)
    except KeyboardInterrupt:
        pass
    finally:
        for child in reversed(children):
            stop_child(child)
        for f in logs:
            f.close()
    if codes:
        detail = '\n'.join((directory / f'{role}.log').read_text(errors='replace')[-1600:]
                           for role in ('send', 'receive') if (directory / f'{role}.log').exists())
        raise RuntimeError(f'A child process failed ({codes}). Check camera mode/permission and logs in {directory}\n{detail}')
    if any(child.returncode != 0 for child in children):
        raise RuntimeError(f'A child did not exit cleanly; see logs in {directory}')
    summaries = {}
    for role in ('sender', 'receiver'):
        path = directory / role / 'summary.json'
        if path.exists():
            summaries[role] = json.loads(path.read_text())
    comparisons = {}
    for stream in range(args.streams):
        keys = {}
        for role, event in [('sender', 'tx'), ('receiver', 'decode')]:
            path = directory / role / 'events.jsonl'
            found = set()
            if path.exists():
                for line in path.open():
                    row = json.loads(line)
                    if row['event'] == event and row.get('stream') == stream:
                        found.add((row['epoch'], row['frame_id']))
            keys[role] = found
        missing = keys['sender'] - keys['receiver']
        comparisons[str(stream)] = dict(sent=len(keys['sender']), decoded=len(keys['receiver']),
                                        sent_not_decoded=len(missing),
                                        delivery_ratio=(len(keys['sender'] & keys['receiver']) / len(keys['sender'])
                                                        if keys['sender'] else None))
    result = dict(config=redact(vars(args)), results=summaries, delivery=comparisons,
                  glass_to_glass_latency_ms=None,
                  note='Camera-dequeue application measurement, not optical end-to-end acceptance.')
    (directory / 'report.json').write_text(json.dumps(result, indent=2))
    for stream, values in comparisons.items():
        rx = summaries.get('receiver', {}).get('streams', {}).get(stream, {})
        latency = rx.get('decode_latency_ms', {})
        print(f'Stream {stream}: {values}; decode P95={latency.get("p95")} ms', flush=True)
    print(f'Combined report: {directory}/report.json', flush=True)
    return 0 if summaries.get('receiver') and all(v['decoded'] > 0 for v in comparisons.values()) else 1


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if hasattr(signal, 'SIGTERM'):
        def terminate(_sig, _frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, terminate)
    try:
        validate(p, args)
        if args.command == 'web':
            from .webapp import run_web
            return run_web(args)
        if args.command == 'doctor':
            return doctor()
        if args.command == 'cameras':
            from .cameras import inventory
            info = inventory()
            if args.sdk:
                from .sdk import inventory as sdk_inventory
                info['sdk'] = sdk_inventory()
            result = json.dumps(info, ensure_ascii=False, indent=2)
            if args.output:
                Path(args.output).write_text(result)
            print(result)
            return 0
        if args.command == 'sdk':
            from .sdk import configure_root, inventory
            if args.orbbec_root:
                configure_root(args.orbbec_root)
            print(json.dumps(inventory(), ensure_ascii=False, indent=2))
            return 0
        if args.command == 'optical':
            from .optical import run_optical
            return run_optical(args)
        if args.command == 'analyze':
            from .metrics import summarize
            print(json.dumps(summarize(args.directory), indent=2))
        elif args.command == 'demo':
            return run_demo(args)
        elif args.command == 'send':
            from .sender import run_sender
            run_sender(args)
        else:
            from .receiver import run_receiver
            run_receiver(args)
        return 0
    except Exception as exc:
        print(f'ERROR: {redact(str(exc))}', file=sys.stderr)
        return 1
