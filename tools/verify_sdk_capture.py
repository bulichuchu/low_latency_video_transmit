"""Explicit, bounded SDK camera test. No automatic elevation or image files."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from video_demo import sdk
from video_demo.cli import main


def verify():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    info = sdk.inventory()
    directory = Path(args.output).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'sdk-inventory.json').write_text(json.dumps(info, indent=2, ensure_ascii=False))
    # Require a color image stream; do not claim depth/IR is an RGB test.
    inputs = [r for r in info['devices'] if r['serial'] == args.serial and
              r['sdk_stream'] in ('color', 'color_left', 'color_right')]
    if not inputs:
        raise RuntimeError('该设备没有可用彩色 SDK 流。请检查 sdk-inventory.json 中的权限错误和当前模式。')
    return main(['demo', '--cameras', inputs[0]['device'], '--headless', '--duration', '5',
                 '--width', '640', '--height', '400', '--fps', '30', '--bitrate-kbps', '1500',
                 '--decoder', 'software', '--output', str(directory / 'transmission')])


if __name__ == '__main__':
    try:
        sys.exit(verify())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
