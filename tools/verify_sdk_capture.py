"""Explicit, bounded SDK camera test. No automatic elevation or image files."""
import argparse
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from video_demo import sdk
from video_demo.cli import main


def verify():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial', required=True, action='append', help='Repeat to verify multiple cameras together')
    parser.add_argument('--output', required=True)
    parser.add_argument('--width', type=int, default=640)
    parser.add_argument('--height', type=int, default=400)
    parser.add_argument('--fps', type=int, default=30)
    parser.add_argument('--duration', type=float, default=5)
    args = parser.parse_args()
    if not math.isfinite(args.duration) or not 0 < args.duration <= 120:
        parser.error('--duration must be in (0, 120] seconds')
    if not 1 <= len(args.serial) <= 8 or len(set(args.serial)) != len(args.serial):
        parser.error('Choose 1..8 distinct serial numbers')
    info = sdk.inventory()
    directory = Path(args.output).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'sdk-inventory.json').write_text(json.dumps(info, indent=2, ensure_ascii=False))
    # Require a color image stream; do not claim depth/IR is an RGB test.
    inputs = []
    for serial in args.serial:
        choices = [r for r in info['devices'] if r['serial'] == serial and
                   r['sdk_stream'] in ('color', 'color_left', 'color_right')]
        if not choices:
            raise RuntimeError(f'{serial} 没有可用彩色 SDK 流。请检查 sdk-inventory.json 中的权限错误和当前模式。')
        inputs.append(choices[0]['device'])
    return main(['demo', '--cameras', ','.join(inputs), '--headless', '--duration', str(args.duration),
                 '--width', str(args.width), '--height', str(args.height), '--fps', str(args.fps), '--bitrate-kbps', '1500',
                 '--decoder', 'software', '--output', str(directory / 'transmission')])


if __name__ == '__main__':
    try:
        sys.exit(verify())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
