"""User-invoked macOS launcher with optional local sudo password input."""
import argparse
import os
from pathlib import Path
import platform
import signal
import stat
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def authorize_from_file():
    """Validate sudo using a private local file; leave command stdin unchanged."""
    password_file = ROOT / '.local' / 'sudo-password'
    try:
        info = password_file.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        print('[sdk] 密码文件必须是当前用户拥有的普通文件，改为手动授权。', file=sys.stderr)
        return
    if not info.st_size:
        return
    try:
        password_file.chmod(0o600)
        with password_file.open('rb') as password_input:
            result = subprocess.run(['/usr/bin/sudo', '-S', '-p', '', '-v'], stdin=password_input)
        if result.returncode == 0:
            return
    except OSError:
        pass
    print('[sdk] 本地密码未能完成授权，改为手动输入。', file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serve', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--sdk-root', help=argparse.SUPPRESS)
    parser.add_argument('--owner-uid', type=int, help=argparse.SUPPRESS)
    parser.add_argument('--owner-gid', type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if platform.system() != 'Darwin':
        raise RuntimeError('此管理员助手只用于 macOS 的 SDK USB 访问问题')
    if args.serve:
        if os.geteuid() != 0 or not args.sdk_root or args.owner_uid is None or args.owner_gid is None:
            raise RuntimeError('Missing explicit sudo launch parameters')
        from video_demo.sdk_helper import serve
        import faulthandler
        faulthandler.enable(all_threads=True)
        def stop(_signal, _frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, stop)
        return serve(args.sdk_root, args.owner_uid, args.owner_gid)
    if os.geteuid() == 0:
        raise RuntimeError('请以普通用户运行启动脚本，它会单独为 SDK 助手请求 sudo 授权')
    from video_demo.sdk import configured_root
    from video_demo.orbbec import library_path
    root = configured_root()
    if root is None:
        raise RuntimeError('请先在 Vue 页面保存 Orbbec SDK 目录')
    library_path(root)
    directory = ROOT / '.local'
    directory.mkdir(exist_ok=True, mode=0o700)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError('.local 必须是当前用户拥有的真实目录')
    directory.chmod(0o700)
    print('即将由 sudo 为本机 Orbbec SDK 助手请求管理员授权。\n'
          '助手仅提供相机查询和采集；网页、编码和网络传输仍由普通用户进程运行。\n'
          '可从 .local/sudo-password 读取本机密码；结束时按 Ctrl+C。', flush=True)
    authorize_from_file()
    os.execv('/usr/bin/sudo', ['sudo', '--', sys.executable, '-I', '-B', str(Path(__file__).resolve()),
        '--serve', '--sdk-root', str(root), '--owner-uid', str(os.getuid()), '--owner-gid', str(os.getgid())])


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        raise SystemExit(1)
