#!/usr/bin/env python3
"""Lab only: a qcow2 overlay of a released Mac disk with startup items turned off.

    overlay_disable.py BASE.img OUT.qcow2 [ITEM ...]

Each ITEM is a path inside the blessed System Folder, for example
"Extensions:AirPort AP" or "Control Panels:After Dark 4.94"; it is moved into
the matching "(Disabled)" folder ("Extensions (Disabled)", "Control Panels
(Disabled)", "Startup Items (Disabled)"), which is exactly what Extensions
Manager does.  With no ITEM the overlay is empty (the A/B base arm, so both
arms read through qcow2); with the single ITEM "-" the volume is only mounted
and unmounted through the Linux driver (the control arm for the mount itself).

BASE is never written: QEMU's qcow2 overlay takes every change, exported
through qemu-nbd and changed with the Linux hfsplus driver (guestprep's mount
helpers), then fsck.hfsplus -fn must pass.  Needs root, the nbd and hfsplus
modules (rb-lab's generic kernel; not the builder) and qemu-img/qemu-nbd.
Boot the overlay with rbd_boottime.py boot --disk OUT.qcow2 --snapshot
--probe-disk BASE.img; after copying it to another host, point it at that
host's identical BASE with ``qemu-img rebase -u -F raw -b BASE OUT.qcow2``.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'guestprep'))
from rbguestprep import hfsplus, prep  # noqa: E402

DISABLED = {'Extensions': 'Extensions (Disabled)', 'Control Panels': 'Control Panels (Disabled)',
            'Startup Items': 'Startup Items (Disabled)', 'Control Strip Modules': 'Control Strip Modules (Disabled)'}


def run(*cmd):
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def main() -> int:
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    base, out, items = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(), sys.argv[3:]
    if out.exists():
        sys.exit(f'{out} exists')
    run('modprobe', 'nbd', 'max_part=0')
    run('qemu-img', 'create', '-q', '-f', 'qcow2', '-F', 'raw', '-b', str(base), str(out))
    dev = next(f'/dev/nbd{i}' for i in range(16)
               if Path(f'/sys/block/nbd{i}/pid').exists() is False)
    run('qemu-nbd', '-c', dev, '-f', 'qcow2', str(out))
    report = {'base': str(base), 'overlay': str(out), 'moved': [], 'device': dev}
    try:
        for _ in range(50):          # the device answers once qemu-nbd has connected
            if Path(f'/sys/block/{Path(dev).name}/size').read_text().strip() != '0':
                break
            time.sleep(0.1)
        with hfsplus.Volume(dev) as vol:
            sysf = prep._system_folder(vol)
        if items:
            with prep.mounted(dev) as mnt:
                root = Path(mnt).joinpath(*[prep._linux_name(p) for p in sysf])
                for item in [i for i in items if i != '-']:
                    parts = item.split(':')
                    src = root.joinpath(*[prep._linux_name(p) for p in parts])
                    dst_dir = root / DISABLED[parts[0]]
                    dst_dir.mkdir(exist_ok=True)
                    dst = dst_dir / prep._linux_name(parts[-1])
                    if not src.exists():
                        raise SystemExit(f'missing: {item}')
                    src.rename(dst)
                    report['moved'].append([item, f'{DISABLED[parts[0]]}:{parts[-1]}'])
            ok, text, repaired = prep.fsck_verify(dev)
            report['fsck_ok'], report['fsck_repaired'] = ok, repaired
            if not ok:
                raise SystemExit('fsck failed:\n' + text)
    finally:
        subprocess.run(['sync'])
        subprocess.run(['qemu-nbd', '-d', dev], capture_output=True)
    report['overlay_bytes'] = out.stat().st_blocks * 512
    print(json.dumps(report))
    return 0


if __name__ == '__main__':
    sys.exit(main())
