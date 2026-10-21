#!/usr/bin/env python3
"""Build a small deployable zipapp using ONLY the sender's explicit allowlist."""
import argparse
import hashlib
from pathlib import Path
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
MODULES = ('__init__.py', 'rk_sender.py', 'protocol.py', 'capture_time.py', 'webrtc_signal.py',
           'congestion.py')


def build(destination):
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Construct separately, so an interrupted build cannot corrupt the last bundle.
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        archive = Path(temporary) / 'rk_sender.pyz'
        with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as z:
            entries = {'__main__.py': b'from video_demo.rk_sender import main\nraise SystemExit(main())\n'}
            for name in MODULES:
                entries[f'video_demo/{name}'] = (ROOT / 'video_demo' / name).read_bytes()
            entries['README.md'] = (ROOT / 'deploy' / 'rk_sender' / 'README.md').read_bytes()
            for name, data in sorted(entries.items()):
                info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(info, data)
        archive.replace(destination)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    destination.with_suffix('.pyz.sha256').write_text(f'{digest}  {destination.name}\n')
    print(f'{destination} ({destination.stat().st_size} bytes)\nsha256: {digest}')
    return destination


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, default=ROOT / '.local' / 'rk-sender' / 'rk_sender.pyz')
    build(p.parse_args().output)
