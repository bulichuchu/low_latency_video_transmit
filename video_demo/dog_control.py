"""Drive the D1 Max robot dog from the receiver page over its UDP protocol 1.2.0.

The robot's motion board runs the server on UDP 8082: 192.168.168.168 on the
wired network, 192.168.234.1 on the robot's Wi-Fi. Speaking the protocol here
means the receiver needs no vendor SDK (which ships Linux libraries only).

Each datagram is a 16-byte header (b'ZSKJ', the JSON length and a message ID
as little-endian uint16, 8 zero bytes; replies echo the ID) followed by
{"head": {"type", "time" (ms), "src"}, "data": {...}}. The messages below are
the ones the vendor SDK 0.1.1 library sends, captured against a stand-in robot:

  1000  handshake, first. Reply status_code 0 ok, 10 protocol mismatch,
        20 another terminal (usually the remote-control app) has the robot
  1001  heartbeat at 5 Hz, head only; the robot echoes it
  1002  {"cmd": "action/stand_up" | "mode/general" | "speed/low" | ...}, acked
  1003  {"lx", "ly", "rx", "ry", "body": {"turn", "high_low"}}. The SDK's
        Move(left_right, forward_back, yaw) sends lx = left (+), ly = forward
        (+) and rx = turn left (+); the protocol PDF's table swaps lx and ly.
        In in-place mode rx leans the head left (+), ry looks up (+), body.turn
        "left" / "right" turns the body and body.high_low "up" / "down" raises or
        lowers it (the remote's H + right stick); while either is not "none" the
        axes are sent as 0. The robot keeps the latest 1003 for 1 s
  1004  status at 1 Hz: standing in general mode is "sport" in the PDF and the
        SDK, "walk" on robot firmware 0.3.2 (which also has no "mode"); 1005 faults;
        1013 take / 1015 release control; 1016 / 1017 control taken / released by
        another terminal; 1018 the robot's own camera bitrate; 1050 goodbye
  1023  charging dock info, not in the PDF; firmware 0.3.2 sends it after the
        handshake: name, dock_connected, has_contact, charging_active, dock
        version, error_code / error_message, dock light. Autonomous recharge is
        an optional product with its own API on the NX (192.168.168.100:10010),
        not part of this protocol
  1028  task status, not in the PDF either; seen on 10-16 while the robot
        recharged on its dock: {"task_type": 4, "status": 2, "phase":
        "recharge_charging" | "recharge_completed", "error_code": 0}. Shown, not
        interpreted further

Commands only go out from the states the SDK guide's state chart (2.8) allows;
others "may make the robot fall". Postures and modes also wait until the robot
has had no motion command for 1 s. Motion needs a fresh status that allows it,
stops when the page stops refreshing it, and after any command or change of
state needs the keys released and pressed again.

A session the robot stops serving (no data for 5 s, or heartbeats echoed but no
1004) is renewed the way the SDK's auto_reconnect does it: a new socket and a new
handshake, repeated until the robot answers, with motion held back meanwhile.
"""
from __future__ import annotations

import asyncio
import contextlib
import errno
import ipaddress
import json
import math
from pathlib import Path
import platform
import socket
import struct
import sys
import time

from aiohttp import WSCloseCode, WSMsgType

MAGIC = b'ZSKJ'
HEADER = struct.Struct('<4sHH8x')
PROTOCOL_VERSION = '1.2.0'
DEFAULT_HOST, DEFAULT_PORT = '192.168.168.168', 8082
SRC_SDK, SRC_EXTERNAL = 3, 4
OWN_CTRL_SOURCE = {SRC_SDK: 2, SRC_EXTERNAL: 3}  # 1004 ctrl_source while this client has control
APP_CTRL_SOURCE = 1  # the remote-control app has the robot
IDENTITY = {
    # What the vendor SDK 0.1.1 sends, so the robot sees a known client.
    SRC_SDK: dict(version='0.1.1', protocol_version=PROTOCOL_VERSION, device='arm',
                  platform='linux', package_name='robot_sdk'),
    SRC_EXTERNAL: dict(version='1.0', protocol_version=PROTOCOL_VERSION,
                       device=platform.machine() or 'pc', platform=sys.platform,
                       package_name='video_flow'),
}
HANDSHAKE, HEARTBEAT, COMMAND, REMOTE, STATUS, FAULTS = 1000, 1001, 1002, 1003, 1004, 1005
TAKE_CONTROL, RELEASE_CONTROL, CONTROL_TAKEN, CONTROL_RELEASED = 1013, 1015, 1016, 1017
CAMERA_BITRATE, DOCK_INFO, TASK_STATUS, GOODBYE = 1018, 1023, 1028, 1050

HEARTBEAT_S = .2
MOVING_S = .02  # 1003 at 50 Hz while any axis is non-zero, as the protocol asks
STOPPING_S, STOP_TAIL_S = .1, 1.  # then zeros at 10 Hz for longer than the robot's 1 s hold
MOTION_HOLD_S = .35  # the page resends held keys every 0.1 s; older motion is dropped
STATUS_FRESH_S = 3.  # 1004 comes at 1 Hz
SETTLE_S = 1.1  # after a command, only a status received this much later counts
LINK_STALE_S = 1.5
# The robot drops a session it has not heard from for a few seconds: on 10-16,
# after a 5.2 s outage on a weak Wi-Fi link, it never sent 1004 again although it
# kept echoing heartbeats. SDK 0.1.1 with auto_reconnect handshakes again from a
# new socket after ~5 s without any data (checked against a stand-in robot); this
# client does the same, and also when heartbeats come back but 1004 does not.
RENEW_SILENT_S = RENEW_STATUS_S = 5.
# Each attempt on a new socket: handshakes every RENEW_RESEND_S for RENEW_TRY_S,
# doubling per attempt up to RENEW_TRY_MAX_S, and a reply still accepted for
# RENEW_PAUSE_S after. Replies were seconds late in that outage (the SDK gives up
# on a socket after ~1 s).
RENEW_TRY_S, RENEW_TRY_MAX_S, RENEW_RESEND_S, RENEW_PAUSE_S = 3., 12., .5, .5
ESTOP_RESENDS = 10  # an e-stop pressed while renewing: tries on the new session, 1 s apart
LINK_STATS_S = 10.  # heartbeats sent / answered and their round trips, logged this often
RENEWING_TEXT = '正在重新连接机器狗，稍候再试'
HANDSHAKE_TIMEOUT_S, REPLY_TIMEOUT_S = 3., 1.
PUSH_S = .5
STILL = (0., 0., 0., 0, 0)  # keys held on the page: forward, left, turn, twist, height
IDLE = (0., 0., 0., 0., 'none', 'none')  # sent: lx, ly, rx, ry, body.turn, body.high_low

# motion_status values. Standing in general mode is 'sport' in protocol 1.2.0 and
# SDK 0.1.1 but 'walk' on robot firmware 0.3.2 (seen 10-15). The narrow-door,
# gait and rat-board postures are ways to walk. Unknown states allow nothing but
# the e-stop, speeds and lights.
GENERAL = frozenset({'sport', 'walk'})
POSTURES = frozenset({'slim', 'gait_walk', 'dsb'})
WALK_STATES = GENERAL | POSTURES | {'stair'}
HEAD_STATES = frozenset({'in_place'})
STANDING = WALK_STATES | HEAD_STATES
OTHER_MODES = frozenset({'nav', 'follow', 'track'})  # no motion from this page
STUNTS = frozenset({'climb', 'new1new', 'tuoluo', 'jump', 'handstand', 'backflip'})
STATUS_KEYS = ('motion_status', 'current_state', 'mode', 'ctrl_source', 'estop')  # a change is logged
# Command -> the motion_status values it may be sent from (SDK guide 2.8): stand
# up from lying, crawling, locked (unlocks) or a special posture; crawl from lying
# or standing; lie down only from standing; postures and stunts from standing in
# general mode (the guide lists Climb, Gait and Slim there); modes from standing.
FROM_STATE = {
    'action/stand_up': frozenset({'lie_down', 'crawl', 'locked'}) | POSTURES,
    'action/crawl': STANDING | {'lie_down', 'locked'},
    'action/lie_down': STANDING | {'locked'},
    'action/locked': STANDING | {'lie_down', 'crawl'},
    'action/slim': GENERAL,
    'action/gait_walk': GENERAL,
    'action/dsb': GENERAL,
    'action/climb': GENERAL,
    'action/new1new': GENERAL,
    'reverse_head_tail': STANDING | {'lie_down'},
    'knee_mode/same_direction': STANDING | {'lie_down'},
    'knee_mode/medial_facing': STANDING | {'lie_down'},
    **{f'mode/{mode}': STANDING | OTHER_MODES
       for mode in ('general', 'in_place', 'stair', 'navigation', 'follow', 'track')},
}
ESTOP = frozenset({'emergency/stop', 'emergency/recover'})  # stop: always; recover: needs control
SPEEDS = frozenset({'speed/low', 'speed/medium', 'speed/high'})
LIGHTS = frozenset(f'fill_light/{name}' for name in (
    'light_auto_work_on', 'light_auto_work_off', 'front_light_on', 'front_light_off',
    'back_light_on', 'back_light_off'))
ANYTIME = SPEEDS | LIGHTS  # whenever this page has control
COMMANDS = frozenset(FROM_STATE) | ESTOP | ANYTIME
# Protocol 1.2.0: follow / track not on the base models, knee modes not on point-foot ones.
UNSUPPORTED = {
    'mode/follow': frozenset({'ZSM-1', 'ZSM-1F'}),
    'mode/track': frozenset({'ZSM-1', 'ZSM-1F'}),
    'knee_mode/same_direction': frozenset({'ZSM-1F', 'ZSM-1F-Pro', 'ZSM-1F-Ultra'}),
    'knee_mode/medial_facing': frozenset({'ZSM-1F', 'ZSM-1F-Pro', 'ZSM-1F-Ultra'}),
}
CAMERAS = ('camera_front', 'camera_back')


def encode(kind, msg_id, data=None, src=SRC_SDK, time_ms=None):
    body = {'head': {'type': kind, 'time': int(time.time() * 1000) if time_ms is None else time_ms,
                     'src': src}}
    if data is not None:
        body['data'] = data
    payload = json.dumps(body, separators=(',', ':'), sort_keys=True).encode()
    if len(payload) > 0xFFFF:
        raise ValueError('Message too long')
    return HEADER.pack(MAGIC, len(payload), msg_id & 0xFFFF) + payload


def decode(datagram):
    """(message ID, type, data) of one datagram; ValueError if it is not one."""
    if len(datagram) < HEADER.size:
        raise ValueError('Short datagram')
    magic, length, msg_id = HEADER.unpack_from(datagram)
    if magic != MAGIC or len(datagram) < HEADER.size + length:
        raise ValueError('Not a ZSKJ datagram')
    body = json.loads(datagram[HEADER.size:HEADER.size + length])
    head = body.get('head') if isinstance(body, dict) else None
    if not isinstance(head, dict) or not isinstance(head.get('type'), int):
        raise ValueError('No message type')
    data = body.get('data')
    return msg_id, head['type'], data if isinstance(data, dict) else {}


def remote_data(lx, ly, rx, ry, turn='none', high_low='none'):
    return dict(lx=lx, ly=ly, rx=rx, ry=ry, body=dict(turn=turn, high_low=high_low))


def robot_address(host, port, src):
    host = host.strip() if isinstance(host, str) else ''
    if not host or len(host) > 253:
        raise ValueError('请填写机器狗 IP（有线 192.168.168.168，机器狗 Wi-Fi 192.168.234.1）')
    try:
        host = str(ipaddress.ip_address(host))
    except ValueError:
        if host.startswith('-') or not all(c.isascii() and (c.isalnum() or c in '.-') for c in host):
            raise ValueError('机器狗地址只填 IP 或主机名，不带 http://、端口或路径') from None
    port = DEFAULT_PORT if port in (None, '') else port
    if isinstance(port, str) and port.strip().isdigit():
        port = int(port)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError('机器狗端口应为 1–65535，UDP 默认 8082')
    src = SRC_SDK if src in (None, '') else src
    if src not in IDENTITY:
        raise ValueError('协议身份只能是 3（SDK）或 4（EXTERNAL）')
    return host, port, src


def _axis(value):
    """One motion axis from the page: a finite number clamped to [-1, 1], else 0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.
    try:
        value = float(value)
    except OverflowError:
        return 0.
    if not math.isfinite(value):
        return 0.
    return round(min(1., max(-1., value)), 3) + 0.  # + 0. turns -0.0 into 0.0


def _step(value):
    """An in-place body control from the page: -1, 0 or +1."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return 0
    return (value > 0) - (value < 0)


def _robot_still(status):
    """The reported body speed is about zero (standing still reads ~0.03 m/s)."""
    speed = status.get('speed')
    if not isinstance(speed, dict):
        return False
    try:
        return abs(speed['x']) < .1 and abs(speed['y']) < .1 and abs(speed['yaw']) < .2
    except (KeyError, TypeError):
        return False


def _estop(status):
    stop = status.get('estop')
    return isinstance(stop, dict) and bool(stop.get('software') or stop.get('hardware'))


def _handshake_problem(reply):
    """None if the handshake reply accepts this client, else why not."""
    code = reply.get('status_code')
    if code == 0:
        return None
    if code == 10:
        return f'协议版本不匹配：机器狗不接受协议 {PROTOCOL_VERSION}'
    if code == 20:
        return '机器狗正被其他终端控制：退出遥控器 APP（包括后台）后重试；刚断开的连接需等几秒'
    return f'机器狗拒绝握手（status_code {code}）'


def _network_error(exc, host, port):
    if isinstance(exc, socket.gaierror):
        return f'无法解析机器狗地址 {host}：直接填 IP'
    if isinstance(exc, ConnectionRefusedError):
        return f'{host}:{port} 端口不可达：这个地址上没有机器狗的 UDP 服务（端口默认 8082）'
    if getattr(exc, 'errno', None) in (errno.EHOSTDOWN, errno.EHOSTUNREACH, errno.ENETUNREACH):
        return (f'到 {host} 的网络不通（{errno.errorcode.get(exc.errno, exc.errno)}）：确认网线或 Wi-Fi，'
                '有线连接时本机 IP 需在 192.168.168.x 网段')
    return f'网络错误：{exc}'


class _Link(asyncio.DatagramProtocol):
    def __init__(self, client):
        self.client = client

    def datagram_received(self, data, addr):
        self.client._received(data)

    def error_received(self, exc):
        self.client._socket_error(exc)


class DogClient:
    """One UDP session with the robot. Every method runs on the event loop."""

    def __init__(self, clock=time.monotonic, log_dir=None):
        self._clock = clock
        self._lock = asyncio.Lock()
        self.changed = asyncio.Event()  # set whenever the page's view changes
        self.state, self.error = 'idle', None
        self.host, self.port, self.src = DEFAULT_HOST, DEFAULT_PORT, SRC_SDK
        self._log_dir = Path(log_dir) if log_dir else None  # one JSONL per connection
        self._log_file = self.log_path = None
        self._reset()

    def _reset(self):
        self._transport = None
        self._tasks = []
        self._waiters = {}  # message ID -> (type, future, cmd)
        self._msg_id = -1
        self._beats = {}  # heartbeat message ID -> send time
        self._wake = asyncio.Event()
        self.robot = self.status = self.status_at = self.rtt_ms = self.last_rx = None
        self.link_stale = self.control_lost = self.control_free = self.released = False
        self.faults, self.last_command, self.socket_error = [], None, None
        self.dock = None  # latest 1023
        self.task = None  # latest 1028
        self._motion, self._motion_at, self._sent = STILL, 0., IDLE
        self._zero_until = self._settle_until = 0.
        self._drive = None  # what the motion meant when last computed
        self._need_release = False  # held keys must be released before moving again
        self._ids_echo = None  # replies echo message IDs (known after the first reply)
        self._state_since = None  # when the reported motion_status last changed
        self._logged_status, self._logged_status_at, self._logged_types = None, 0., set()
        self.renewing = None  # while renewing the session: reason, since, attempts, error
        self.estop_pending = False  # pressed while renewing, not acked yet: sent first once renewed
        self._estop_task = None
        self._session_at = None  # the robot's latest accepted handshake
        self._stats = self._new_stats(0.)

    # -- page API --------------------------------------------------------------
    async def connect(self, host=DEFAULT_HOST, port=DEFAULT_PORT, src=SRC_SDK):
        host, port, src = robot_address(host, port, src)
        async with self._lock:
            if self.state in ('connected', 'connecting'):
                raise ValueError('已连接机器狗；换地址前先断开')
            self._close()
            self._reset()
            self.host, self.port, self.src = host, port, src
            self._open_log()
            self._log('connect', host=host, port=port, src=src)
            self._set_state('connecting')
            loop = asyncio.get_running_loop()
            try:
                self._transport, _ = await loop.create_datagram_endpoint(
                    lambda: _Link(self), remote_addr=(host, port))
                reply = await self._request(HANDSHAKE, IDENTITY[src], HANDSHAKE_TIMEOUT_S)
                if reply is None:
                    raise ConnectionError(f'{host}:{port} 没有回应握手：确认地址（有线 192.168.168.168，'
                                          '机器狗 Wi-Fi 192.168.234.1）、端口和本机网段')
                self._log('handshake', reply=reply, local_port=self._local_port())
                problem = _handshake_problem(reply)
                if problem:
                    raise ConnectionError(problem)
            except OSError as exc:  # includes ConnectionError
                text = str(exc) if type(exc) is ConnectionError else _network_error(exc, host, port)
                self._send(GOODBYE)  # drop a session the robot may have opened
                self._log('error', message=text)
                self._close()
                self._set_state('error', text)
                raise ValueError(text) from None
            version = reply.get('version')
            self.robot = {key: str(value) for key, value in dict(
                sn=reply.get('sn'), model=reply.get('model'), device_type=reply.get('device_type'),
                system=version.get('system') if isinstance(version, dict) else None,
                dock_version=version.get('charging_pile') if isinstance(version, dict) else None).items()
                if value not in (None, '')}
            self.last_rx = self._session_at = self._clock()
            self._stats = self._new_stats(self.last_rx)
            self._tasks = [loop.create_task(self._heartbeat_loop()), loop.create_task(self._remote_loop())]
            self._set_state('connected')

    async def close(self):
        """Stop moving, say goodbye and drop the session."""
        async with self._lock:
            if self.state == 'connected':
                self.renewing = None  # stop and goodbye on whatever socket there is
                self._send(REMOTE, remote_data(0., 0., 0., 0.))
                self._send(GOODBYE)
                self._log_stats(self._clock())
                self._log('close')
            self._close()
            self._set_state('idle')
        await asyncio.sleep(0)  # let the cancelled loops finish

    async def command(self, cmd):
        """Send one whitelisted 1002 command; True once the robot acks it."""
        if self.state != 'connected':
            raise ValueError('未连接机器狗')
        if cmd not in COMMANDS:
            raise ValueError('不支持的命令')
        if self.renewing is not None and cmd != 'emergency/stop':
            raise ValueError(RENEWING_TEXT)
        if cmd in self._unsupported():
            raise ValueError(f"这台机器狗（{self.robot.get('device_type')}）不支持这个命令（协议 1.2.0）")
        if cmd != 'emergency/stop' and cmd not in self.allowed():
            raise ValueError('机器狗当前状态下不能执行这个动作（SDK 状态跳转图，姿态和模式要停稳 1 秒）；'
                             '等状态更新后再试')
        now = self._clock()
        if cmd not in LIGHTS:  # lights change nothing about motion
            self._hold_back()
        if cmd not in ANYTIME:
            # Until the robot reports the state this leads to: no motion, no
            # other action (a stale "lying down" would allow another one).
            self._settle_until = now + SETTLE_S
        if cmd == 'emergency/stop':
            if self.renewing is not None:
                self.estop_pending = True  # likely unheard now: repeated first thing once renewed
            msg_id, future = self._ask(COMMAND, {'cmd': cmd}, cmd)
            self._stop_now(now)
        elif cmd in LIGHTS:
            msg_id, future = self._ask(COMMAND, {'cmd': cmd}, cmd)
        else:  # postures and modes are only allowed when still; a speed change stops first
            self._stop_now(now)
            msg_id, future = self._ask(COMMAND, {'cmd': cmd}, cmd)
        reply = await self._answer(msg_id, future, REPLY_TIMEOUT_S)
        if cmd == 'emergency/stop' and reply is not None:
            self.estop_pending = False
        self.last_command = dict(cmd=cmd, acked=reply is not None)
        self._log('command', cmd=cmd, acked=reply is not None)
        self._changed()
        return reply is not None

    async def take_control(self):
        self._require_session()
        reply = await self._request(TAKE_CONTROL, None, 2 * REPLY_TIMEOUT_S)
        self._log('take_control', reply=reply)
        if reply is None:
            raise ValueError('机器狗没有回应获取控制权的请求')
        if reply.get('error_code', 0) != 0:
            raise ValueError(f"获取控制权失败：{reply.get('reason') or reply.get('error_code')}"
                             '（遥控器 APP 在控制时只能由 APP 释放）')
        self.control_lost = self.control_free = self.released = False
        self._changed()

    async def release_control(self):
        """Hand control back (1015), e.g. to the remote-control app; take_control() undoes it."""
        self._require_session()
        self._hold_back()
        self._stop_now(self._clock())
        reply = await self._request(RELEASE_CONTROL, None, 2 * REPLY_TIMEOUT_S)
        self._log('release_control', reply=reply)
        if reply is None:
            raise ValueError('机器狗没有回应释放控制权的请求')
        if reply.get('error_code', 0) != 0:
            raise ValueError(f"释放控制权失败：{reply.get('reason') or reply.get('error_code')}")
        self.released = True
        self._changed()
        self._wake.set()

    async def camera_bitrate(self, camera, mbps):
        """Set the bitrate of one of the robot's own camera streams (1018)."""
        self._require_session()
        if camera not in CAMERAS:
            raise ValueError('相机只能是 camera_front 或 camera_back')
        if isinstance(mbps, bool) or not isinstance(mbps, (int, float)) or not .2 <= mbps <= 20:
            raise ValueError('码率范围 0.2–20 Mbps')
        bps = int(round(mbps * 1e6))
        reply = await self._request(CAMERA_BITRATE, dict(camera_name=camera, camera_bps=bps),
                                    2 * REPLY_TIMEOUT_S)
        self._log('camera_bitrate', camera=camera, bps=bps, reply=reply)
        if reply is None:
            raise ValueError('机器狗没有回应码率设置')
        return reply

    def set_motion(self, forward=0., left=0., turn=0., twist=0, height=0):
        """The keys held on the page, whatever the robot's state: forward, left and
        turn (left) as fractions of the speed level's maximum, twist (+1 left,
        -1 right) and height (+1 up, -1 down). Walking uses forward, left and turn;
        in place forward pitches the head, left (or else turn) leans it, and twist
        and height move the body. Expires after MOTION_HOLD_S."""
        self._motion = (*(_axis(value) for value in (forward, left, turn)), _step(twist), _step(height))
        self._motion_at = self._clock()
        if self._motion == STILL:
            self._released()
        self._wake.set()

    def stop_motion(self):
        """Stop; a new page or Space counts as every key released."""
        self._motion = STILL
        self._released()
        self._wake.set()

    def drive(self, now=None):
        """'walk' (move and turn), 'head' (in-place: lean and pitch) or None."""
        return self._drive_and_reason(self._clock() if now is None else now)[0]

    def _drive_and_reason(self, now):
        """(drive, None) or (None, why the page may not move the robot now)."""
        status, reason = self._gate(now)
        if status is None:
            return None, reason
        if _estop(status):
            return None, '急停中：解除急停后才能移动'
        state = status.get('motion_status')
        if state in WALK_STATES:
            return 'walk', None
        if state in HEAD_STATES:
            return 'head', None
        held = '' if self._state_since is None else f'，已持续 {now - self._state_since:.0f} 秒'
        if state in ('lie_down', 'crawl'):
            return None, f'机器狗没有站立（{state}{held}）：先点“站立”'
        if state == 'locked':
            return None, '机器狗已锁定：点“站立”“匍匐”或“卧倒”解锁'
        if state in OTHER_MODES:
            return None, f'{state} 模式下本页不发移动：切回通用模式后可用'
        if state in STUNTS:
            return None, f'机器狗正在做特殊动作（{state}{held}），做完再操作'
        if state == 'stand_up':
            return None, f'机器狗正在站起来（stand_up{held}），站稳（sport）后才能移动'
        return None, (f'机器狗上报的运动状态是 “{state}”{held}：本页只在站立（sport / walk / stair、'
                      '过窄门、步态、挡鼠板）时走，in_place 时动头' if state is not None
                      else '机器狗的状态里没有 motion_status')

    def allowed(self, now=None):
        now = self._clock() if now is None else now
        if self.state != 'connected':
            return set()
        if self.renewing is not None or self.control_lost or self.released or self._app_has_control():
            return {'emergency/stop'}
        allowed = set(ESTOP) | set(ANYTIME)
        status = self._usable_status(now)
        if status is not None and not _estop(status) and self._still(now):
            state = status.get('motion_status')
            if state in OTHER_MODES and not _robot_still(status):
                state = None  # navigating or following on its own: wait until it stops
            allowed |= {cmd for cmd, states in FROM_STATE.items() if state in states}
        return allowed - self._unsupported()

    def _unsupported(self):
        device = (self.robot or {}).get('device_type')
        return {cmd for cmd, models in UNSUPPORTED.items() if device in models}

    def snapshot(self):
        now = self._clock()
        connected = self.state == 'connected'
        drive, blocked = self._drive_and_reason(now) if connected else (None, None)
        return dict(
            state=self.state, error=self.error, host=self.host, port=self.port, src=self.src,
            own_ctrl_source=OWN_CTRL_SOURCE[self.src], robot=self.robot, rtt_ms=self.rtt_ms,
            link_stale=self.link_stale, socket_error=self.socket_error,
            status=self.status,
            status_age_s=None if self.status_at is None else round(now - self.status_at, 1),
            settling=connected and (self.status_at is None or self.status_at < self._settle_until),
            faults=self.faults, control_lost=self.control_lost, control_free=self.control_free,
            allowed=sorted(self.allowed(now)), drive=drive, blocked=blocked,
            link_age_s=None if self.last_rx is None else round(now - self.last_rx, 1),
            log_path=self.log_path, still=self._still(now), need_release=self._need_release,
            released=self.released, unsupported=sorted(self._unsupported()), dock=self.dock,
            task=self.task,
            sending=dict(zip(('lx', 'ly', 'rx', 'ry', 'turn', 'high_low'), self._sent)),
            last_command=self.last_command,
            renewing=None if self.renewing is None else dict(
                reason=self.renewing['reason'], for_s=round(now - self.renewing['since'], 1),
                attempts=self.renewing['attempts'], error=self.renewing['error']),
            estop_pending=self.estop_pending)

    # -- internals -------------------------------------------------------------
    def _usable_status(self, now):
        return self._gate(now)[0]

    def _gate(self, now):
        """(the latest status, None) if it is fresh, newer than the last command,
        and link and control allow this client to act; else (None, why not)."""
        if self.state != 'connected':
            return None, '未连接'
        if self.renewing is not None:
            renewing = self.renewing
            why = ('机器狗不再上报状态' if renewing['reason'] == 'no_status'
                   else f'{now - self.last_rx:.0f} 秒没有收到机器狗的数据')
            return None, (f"{why}，正在重新握手（第 {renewing['attempts']} 次"
                          f"{'：' + renewing['error'] if renewing['error'] else ''}）")
        if self.control_lost:
            return None, '控制权已被遥控器 APP 接管（1016），APP 释放后点“获取控制权”'
        if self.released:
            return None, '本页已释放控制权，点“获取控制权”收回'
        if self._app_has_control():
            return None, '机器狗状态显示遥控器 APP 在控制（ctrl_source 1）：完全退出 APP，再点“获取控制权”'
        if now - self.last_rx > LINK_STALE_S:
            return None, f'{now - self.last_rx:.1f} 秒没有收到机器狗的数据'
        if self.status is None:
            return None, '还没有收到机器狗的状态（1004）'
        if now - self.status_at > STATUS_FRESH_S:
            return None, f'机器狗的状态 {now - self.status_at:.1f} 秒没有更新（应每秒一次）'
        if self.status_at < self._settle_until:
            return None, '等待机器狗上报命令之后的新状态'
        return self.status, None

    def _app_has_control(self):
        """The latest status says the app controls the robot (also if its 1016 was lost)."""
        return isinstance(self.status, dict) and self.status.get('ctrl_source') == APP_CTRL_SOURCE

    def _still(self, now):
        """No motion sent, the last one longer ago than the robot holds it (1 s)."""
        return self._sent == IDLE and now >= self._zero_until

    def _released(self):
        if self._need_release:
            self._need_release = False
            self._changed()

    def _hold_back(self):
        """Keys held now must be released and pressed again before anything moves."""
        if self._motion != STILL and not self._need_release:
            self._need_release = True
            self._changed()
        self._motion = STILL

    def _stop_now(self, now):
        """A zero 1003 right away, then the usual zero tail."""
        if self._sent != IDLE:
            self._send(REMOTE, remote_data(*IDLE))
            self._sent = IDLE
            self._log('remote', **dict(zip(('lx', 'ly', 'rx', 'ry', 'turn', 'high_low'), IDLE)))
            self._zero_until = now + STOP_TAIL_S
            self._changed()
        self._wake.set()

    def _changed(self):
        self.changed.set()

    def _set_state(self, state, error=None):
        self.state, self.error = state, error
        self._changed()

    def _next_id(self):
        self._msg_id = (self._msg_id + 1) & 0xFFFF
        return self._msg_id

    def _send(self, kind, data=None, msg_id=None):
        msg_id = self._next_id() if msg_id is None else msg_id
        if self.renewing is not None and kind not in (HANDSHAKE, COMMAND, GOODBYE):
            return msg_id  # like the SDK: only handshakes until one is accepted; an e-stop is still tried
        if self._transport is not None and not self._transport.is_closing():
            self._transport.sendto(encode(kind, msg_id, data, self.src))
        return msg_id

    def _local_port(self):
        with contextlib.suppress(Exception):
            return self._transport.get_extra_info('sockname')[1]
        return None

    def _require_session(self):
        if self.state != 'connected':
            raise ValueError('未连接机器狗')
        if self.renewing is not None:
            raise ValueError(RENEWING_TEXT)

    def _ask(self, kind, data=None, cmd=None):
        """Send a request now; _answer() awaits its reply."""
        msg_id = self._next_id()
        future = asyncio.get_running_loop().create_future()
        self._waiters[msg_id] = (kind, future, cmd)
        self._send(kind, data, msg_id)
        return msg_id, future

    async def _answer(self, msg_id, future, timeout):
        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._waiters.pop(msg_id, None)

    async def _request(self, kind, data, timeout, cmd=None):
        return await self._answer(*self._ask(kind, data, cmd), timeout)

    def _close(self):
        current = asyncio.current_task()
        for task in self._tasks:
            if task is not current:
                task.cancel()
        self._tasks = []
        for _, future, _ in self._waiters.values():
            if not future.done():
                future.set_exception(ConnectionError('连接已断开'))
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        self._motion, self._sent = STILL, IDLE
        if self._log_file is not None:
            with contextlib.suppress(OSError):
                self._log_file.close()
            self._log_file = None

    def _open_log(self):
        self.log_path = None
        if self._log_dir is None:
            return
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            path = self._log_dir / f'{time.strftime("%Y%m%d-%H%M%S")}-dog.jsonl'
            self._log_file = path.open('a', encoding='utf-8', buffering=1)
            self.log_path = str(path)
        except OSError:
            self._log_file = None

    def _log(self, event, **fields):
        """What the robot reported and what was sent, for diagnosing a session."""
        if self._log_file is not None:
            with contextlib.suppress(OSError, ValueError, TypeError):
                self._log_file.write(json.dumps(dict(t=round(time.time(), 3), event=event, **fields),
                                                ensure_ascii=False) + '\n')

    def _log_status(self, data, now):
        speed, battery = data.get('speed'), data.get('battery')
        key = [data.get(name) for name in STATUS_KEYS] + [
            speed.get('level') if isinstance(speed, dict) else None, data.get('charging_pile'),
            [battery.get(f'power_supply_status{i}') for i in (1, 2)] if isinstance(battery, dict) else None]
        if key != self._logged_status or now - self._logged_status_at >= 10:
            self._logged_status, self._logged_status_at = key, now
            self._log('status', data=data)

    def _received(self, datagram):
        try:
            msg_id, kind, data = decode(datagram)
        except ValueError:
            return
        now = self.last_rx = self._clock()
        self._stats['rx'] += 1
        if self.socket_error is not None:
            self.socket_error = None
            self._changed()
        waiter = self._waiters.get(msg_id)
        if waiter is not None and waiter[0] == kind:
            self._ids_echo = True
        elif self._ids_echo is not True:  # a robot that does not echo IDs: match by type
            waiter = next((w for w in self._waiters.values() if w[0] == kind and not w[1].done()
                           and (w[2] is None or w[2] == data.get('cmd'))), None)
            if waiter is not None:
                self._ids_echo = False
        else:
            waiter = None  # e.g. a late ack for a request that already timed out
        if waiter is not None and not waiter[1].done():
            waiter[1].set_result(data)
        if kind == HEARTBEAT:
            sent = self._beats.pop(msg_id, None)
            if sent is not None:
                self.rtt_ms = round((now - sent) * 1000, 1)
                self._stats['answered'] += 1
                self._stats['rtt'].append(self.rtt_ms)
        elif kind == STATUS:
            self._stats['status'] += 1
            if not isinstance(data.get('motion_status'), str):
                data = {**data, 'motion_status': None}  # only strings are states
            if self.status is None or data.get('motion_status') != self.status.get('motion_status'):
                self._state_since = now
            self.status, self.status_at = data, now
            self._log_status(data, now)
            self._changed()
            self._wake.set()
        elif kind == FAULTS:
            faults = data.get('faults')
            self.faults = [f for f in faults if isinstance(f, dict)][:20] if isinstance(faults, list) else []
            self._log('faults', data=data)
            self._changed()
        elif kind == CONTROL_TAKEN:
            self.control_lost, self.control_free = True, False
            self._motion = STILL
            self._log('control_taken', data=data)
            self._changed()
            self._wake.set()
        elif kind == CONTROL_RELEASED:
            self.control_free = True
            self._log('control_released', data=data)
            self._changed()
        elif kind == DOCK_INFO:
            if data != self.dock:
                self._log('dock', data=data)
                self.dock = data
                self._changed()
        elif kind == TASK_STATUS:
            if data != self.task:
                self._log('task', data=data)
                self.task = data
                self._changed()
        elif (kind not in (HANDSHAKE, COMMAND, TAKE_CONTROL, RELEASE_CONTROL, CAMERA_BITRATE, GOODBYE)
              and kind not in self._logged_types):
            self._logged_types.add(kind)  # anything else: its first one only
            self._log('message', type=kind, data=data)

    def _socket_error(self, exc):
        self.socket_error = _network_error(exc, self.host, self.port)
        for _, future, _ in self._waiters.values():
            if not future.done():
                future.set_exception(exc)
        self._changed()

    async def _heartbeat_loop(self):
        while True:
            now = self._clock()
            if now - self._stats['start'] >= LINK_STATS_S:
                self._log_stats(now)
            reason = self._renew_reason(now)
            if reason is not None:
                await self._renew(reason)
                continue
            silent = now - self.last_rx
            if (silent > LINK_STALE_S) != self.link_stale:
                self.link_stale = not self.link_stale
                self._log('link', stale=self.link_stale, silent_s=round(silent, 2), rtt_ms=self.rtt_ms)
                self._changed()
                self._wake.set()
            msg_id = self._send(HEARTBEAT)
            self._beats[msg_id] = now
            self._stats['sent'] += 1
            if len(self._beats) > 50:
                self._beats.pop(next(iter(self._beats)))
            await asyncio.sleep(HEARTBEAT_S)

    def _renew_reason(self, now):
        """Why the session needs a new handshake now, or None."""
        silent = now - self.last_rx
        # 1004 comes at 1 Hz from the handshake on: none since then counts as well.
        last_status = self._session_at if self.status_at is None else max(self.status_at, self._session_at)
        no_status = now - last_status > RENEW_STATUS_S
        if silent > RENEW_SILENT_S or (no_status and silent > LINK_STALE_S):
            return 'silent'  # nothing arrives
        if no_status:
            return 'no_status'  # heartbeats are echoed, but the robot stopped pushing 1004
        return None

    async def _renew(self, reason):
        """A new session, the way SDK 0.1.1's auto_reconnect opens one: a new socket
        (so a new local port) and handshakes until the robot accepts one. Motion is
        held back meanwhile; keys held across it must be pressed again."""
        now = self._clock()
        self._log('renew', reason=reason, silent_s=round(now - self.last_rx, 2),
                  status_age_s=None if self.status_at is None else round(now - self.status_at, 1))
        self._log_stats(now)
        self._hold_back()
        if self._transport is not None:  # the old session may still be heard: stop, goodbye
            self._send(REMOTE, remote_data(*IDLE))
            self._send(GOODBYE)
        if self._sent != IDLE:
            self._zero_until = now + STOP_TAIL_S  # postures still wait 1 s after the last motion
        self._sent = IDLE
        self.renewing = dict(reason=reason, since=now, attempts=0, error=None)
        self.link_stale = True
        self._changed()
        self._wake.set()
        loop = asyncio.get_running_loop()
        while True:
            self.renewing['attempts'] += 1
            self._changed()
            reply, waited = None, False
            try:
                transport, _ = await loop.create_datagram_endpoint(
                    lambda: _Link(self), remote_addr=(self.host, self.port))
                old, self._transport = self._transport, transport  # never without a socket (e-stop)
                if old is not None:
                    old.close()
                window = min(RENEW_TRY_S * 2 ** min(self.renewing['attempts'] - 1, 3), RENEW_TRY_MAX_S)
                reply = await self._handshake_once(window)
                waited = reply is None  # it listened RENEW_PAUSE_S past the last handshake
                error = '没有回应握手' if reply is None else _handshake_problem(reply)
            except OSError as exc:
                error = _network_error(exc, self.host, self.port)
            except Exception as exc:  # keep trying: this loop is the session's only way back
                error = f'内部错误：{exc!r}'
            if error is None:
                break
            if error != self.renewing['error']:
                self._log('renew_error', attempt=self.renewing['attempts'], message=error)
                self.renewing['error'] = error
            # The robot may have accepted a handshake whose reply is still on its way:
            # drop that session (as connect() does) so it does not refuse the next socket.
            self._send(GOODBYE)
            if not waited:
                await asyncio.sleep(RENEW_PAUSE_S)
        now = self._clock()
        self._log('renewed', attempts=self.renewing['attempts'], took_s=round(now - self.renewing['since'], 2),
                  local_port=self._local_port(), reply=reply)
        self.renewing = None
        self.last_rx = self._session_at = now
        # Only a status of the new session counts, and none from before a settle window
        # that started meanwhile (an e-stop pressed while renewing).
        self._settle_until = max(self._settle_until, now)
        self._beats.clear()
        if self.estop_pending and (self._estop_task is None or self._estop_task.done()):
            self._settle_until = max(self._settle_until, now + SETTLE_S)
            first = self._ask(COMMAND, {'cmd': 'emergency/stop'}, 'emergency/stop')  # before anything else
            self._estop_task = loop.create_task(self._resend_estop(first))
            self._tasks.append(self._estop_task)
        self._changed()
        self._wake.set()

    async def _handshake_once(self, window):
        """A handshake on the current socket, resent every RENEW_RESEND_S for `window`
        and its reply still accepted RENEW_PAUSE_S after: the reply, or None."""
        msg_id, future = self._ask(HANDSHAKE, IDENTITY[self.src])
        start = self._clock()
        try:
            while True:
                elapsed = self._clock() - start
                if elapsed >= window + RENEW_PAUSE_S:
                    return None
                wait = RENEW_RESEND_S if elapsed < window else RENEW_PAUSE_S
                done, _ = await asyncio.wait({future}, timeout=min(wait, window + RENEW_PAUSE_S - elapsed))
                if done:
                    return future.result()  # OSError if the socket reported one
                if self._clock() - start < window:
                    self._send(HANDSHAKE, IDENTITY[self.src], msg_id)  # same ID: any of them answers
        finally:
            self._waiters.pop(msg_id, None)
            if future.done() and not future.cancelled():
                future.exception()  # retrieved: close() fails the future while we wait

    async def _resend_estop(self, first):
        """An e-stop pressed while renewing: sent first on the new session, then once a
        second until the robot acks it, at most ESTOP_RESENDS times on a live session."""
        request, tries, acked = first, 0, False
        while self.estop_pending and tries < ESTOP_RESENDS:
            if request is None:
                if self.renewing is not None:  # the link went again: wait for the next session
                    await asyncio.sleep(.1)
                    continue
                request = self._ask(COMMAND, {'cmd': 'emergency/stop'}, 'emergency/stop')
            tries += 1
            acked = await self._answer(*request, REPLY_TIMEOUT_S) is not None
            request = None
            self._log('command', cmd='emergency/stop', acked=acked, after_renew=True)
            if acked:
                break
        if request is not None:  # sent, but the page's own e-stop was acked first
            self._waiters.pop(request[0], None)
        if self.estop_pending:  # acked, or given up after ESTOP_RESENDS tries: the page shows which
            self.estop_pending = False
            self.last_command = dict(cmd='emergency/stop', acked=acked)
            self._changed()

    @staticmethod
    def _new_stats(now):
        return dict(start=now, sent=0, answered=0, rtt=[], status=0, rx=0)

    def _log_stats(self, now):
        """One line per LINK_STATS_S: how many heartbeats came back and how fast."""
        stats, self._stats = self._stats, self._new_stats(now)
        rtt = sorted(stats['rtt'])
        self._log('link_stats', window_s=round(now - stats['start'], 1), heartbeats=stats['sent'],
                  answered=stats['answered'], status=stats['status'], received=stats['rx'],
                  rtt_ms=dict(min=rtt[0], p50=rtt[len(rtt) // 2], max=rtt[-1]) if rtt else None)

    def _remote_values(self, now):
        if self._motion != STILL and now - self._motion_at > MOTION_HOLD_S:
            self._hold_back()  # the page stopped refreshing held keys
        drive = self.drive(now)
        if drive != self._drive:
            # Stood up, e-stopped, lost the link or control...: keys held across
            # the change (the page reports them whatever the state) must not
            # start or resume motion by themselves.
            if self._motion != STILL:
                self._hold_back()
            self._log('drive', drive=drive)
            self._drive = drive
        if self._need_release:
            return IDLE
        forward, left, turn, twist, height = self._motion
        if drive == 'walk':
            return (left, forward, turn, 0., 'none', 'none')
        if drive == 'head':
            if twist or height:  # body moves: the protocol wants the axes at 0
                return (0., 0., 0., 0., {1: 'left', -1: 'right'}.get(twist, 'none'),
                        {1: 'up', -1: 'down'}.get(height, 'none'))
            return (0., 0., left or turn, forward, 'none', 'none')
        return IDLE

    async def _remote_loop(self):
        next_send = 0.
        while True:
            now = self._clock()
            try:
                values = self._remote_values(now)
            except Exception as exc:  # never leave the robot on its last command
                self._log('error', message=f'remote: {exc!r}')
                values = IDLE
            moving = values != IDLE
            if values != self._sent:
                due = True
                if not moving:
                    self._zero_until = now + STOP_TAIL_S
            else:
                due = now >= next_send and (moving or now < self._zero_until)
            if due:
                self._send(REMOTE, remote_data(*values))
                if values != self._sent:
                    self._sent = values
                    self._log('remote', **dict(zip(('lx', 'ly', 'rx', 'ry', 'turn', 'high_low'), values)))
                    self._changed()
                next_send = now + (MOVING_S if moving else STOPPING_S)
            if moving:
                timeout = min(next_send, self._motion_at + MOTION_HOLD_S) - now
            elif now < self._zero_until:
                timeout = next_send - now
            else:
                timeout = .25  # idle: wake-ups come from the page and from statuses
            self._wake.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), max(timeout, .001))


class DogConsole:
    """The receiver page's side: one page at a time drives and gets status pushes.

    A page opens with {"type": "hello", "page": <id per page load>, "takeover":
    bool}. It takes over when it loads and when its user asks; a page that only
    reconnects gets the robot back only if no other page has taken it since.
    """

    AXES = ('forward', 'left', 'turn', 'twist', 'height')
    HELLO_TIMEOUT_S = 5.

    def __init__(self, client=None):
        self.client = client or DogClient()
        self.page = self.page_id = None
        self._jobs = set()

    async def serve(self, ws):
        """Run one page's prepared WebSocket until it closes."""
        hello = await self._hello(ws)
        if hello is None:
            return
        page_id, takeover = hello
        current = self.page
        if current is not None and not current.closed:
            if not takeover and page_id != self.page_id:
                await self._retire(ws)  # another page has the robot now
                return
            # In the background: a frozen page would hold up its close handshake.
            self._start(self._retire(current))
        self.page, self.page_id = ws, page_id
        self.client.stop_motion()
        pusher = asyncio.create_task(self._push(ws))
        try:
            async for message in ws:
                if message.type != WSMsgType.TEXT or ws is not self.page:
                    continue
                try:
                    data = json.loads(message.data)
                except ValueError:
                    continue
                if isinstance(data, dict):
                    self._handle(ws, data)
        finally:
            pusher.cancel()
            if self.page is ws:
                self.page = None
                self.client.stop_motion()

    async def close(self):
        await self.client.close()

    async def shutdown(self):
        """Server stopping: stop the robot now, then let the page go."""
        await self.client.close()
        page, self.page = self.page, None
        if page is not None and not page.closed:
            with contextlib.suppress(Exception):
                await page.close(code=WSCloseCode.GOING_AWAY, message=b'server stopping')

    async def _hello(self, ws):
        with contextlib.suppress(asyncio.TimeoutError, ValueError):
            message = await ws.receive(timeout=self.HELLO_TIMEOUT_S)
            data = json.loads(message.data) if message.type == WSMsgType.TEXT else None
            if isinstance(data, dict) and data.get('type') == 'hello':
                return str(data.get('page') or '')[:64], bool(data.get('takeover'))
        with contextlib.suppress(Exception):
            await ws.close(code=4001, message=b'hello first')
        return None

    def _start(self, coroutine):
        job = asyncio.create_task(coroutine)
        self._jobs.add(job)
        job.add_done_callback(self._jobs.discard)

    @staticmethod
    async def _retire(ws):
        with contextlib.suppress(Exception):
            await ws.send_json({'type': 'replaced'})
            await ws.close(code=4000, message=b'replaced')

    def _handle(self, ws, data):
        kind = data.get('type')
        if kind == 'move':
            self.client.set_motion(*(data.get(axis) for axis in self.AXES))
        elif kind == 'stop':
            self.client.stop_motion()
        elif kind in ('connect', 'disconnect', 'command', 'take_control', 'release_control',
                      'camera_bitrate'):
            self._start(self._run(ws, kind, data))

    async def _run(self, ws, kind, data):
        try:
            if kind == 'connect':
                await self.client.connect(data.get('host'), data.get('port'), data.get('src'))
                text = '已连接'
            elif kind == 'disconnect':
                await self.client.close()
                text = '已断开'
            elif kind == 'take_control':
                await self.client.take_control()
                text = '已获取控制权'
            elif kind == 'release_control':
                await self.client.release_control()
                text = '已释放控制权，遥控器 APP 可以接管'
            elif kind == 'camera_bitrate':
                reply = await self.client.camera_bitrate(data.get('camera'), data.get('mbps'))
                bps = reply.get('camera_bps')
                text = (f'机器狗已确认 {bps / 1e6:.1f} Mbps' if isinstance(bps, (int, float))
                        and not isinstance(bps, bool) else '机器狗已确认')
            else:
                acked = await self.client.command(data.get('cmd'))
                text = ('机器狗已确认' if acked
                        else '链路中断：急停已记下，重新握手成功后立即发送；紧急时按机身急停'
                        if data.get('cmd') == 'emergency/stop' and self.client.estop_pending
                        else '已发送，1 秒内没有收到确认')
            result = dict(ok=True, message=text)
        except (ValueError, OSError) as exc:
            result = dict(ok=False, message=str(exc))
        except Exception as exc:  # a bug must still answer the page
            self.client._log('error', message=f'{kind}: {exc!r}')
            result = dict(ok=False, message=f'内部错误：{exc!r}')
        with contextlib.suppress(Exception):
            await ws.send_json(dict(type='result', id=data.get('id'), kind=kind, cmd=data.get('cmd'), **result))

    async def _push(self, ws):
        changed = self.client.changed
        while not ws.closed:
            changed.clear()
            try:
                snapshot = self.client.snapshot()
            except Exception as exc:  # the page must not keep showing a stale state
                self.client._log('error', message=f'snapshot: {exc!r}')
                snapshot = dict(state='error', error=f'内部错误：{exc!r}', allowed=[], status=None)
            try:
                await ws.send_json(dict(type='dog', **snapshot))
            except Exception:
                return
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(changed.wait(), PUSH_S)
