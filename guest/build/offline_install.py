#!/usr/bin/env python3
"""Install canonical trees into a stopped guest disk (one fsck-verified step each).

    offline_install.py IMAGE [TREE=DEST ...] [--move SRC=DEST ...] [--remove MACPATH ...]
                       [--replace] [--report FILE]

DEST is a Mac folder path with ':' separators below the startup volume, for
example ``System Folder:Extensions``; it is created when missing.  Each pair is
one ``rbguestprep.install.install_tree`` call (fsck before, fork-by-fork
read-back and ``fsck.hfsplus -fn`` after).  ``--move`` renames or moves an
item inside the volume (both Mac paths below the root; DEST is the new full
path, its folder must exist): a catalog rename through the driver, so forks
and Finder info stay as they are.  Moves run before removals.  ``--remove`` deletes items (a Mac
path below the volume root, e.g. ``Desktop Folder:Register with Apple``)
through the hfsplus driver first, then checks the volume.  Needs root, the hfsplus module and
hfsprogs (rb-dev, generic kernel).  Run it on a copy of the disk.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'guestprep'))
from rbguestprep import install, prep  # noqa: E402


def remove(image: str, paths: list[str]) -> list[str]:
    removed = []
    with prep.mounted(image) as mnt:
        for mac in paths:
            p = os.path.join(mnt, *[install.linux_name(x) for x in mac.split(':') if x])
            if os.path.lexists(p):
                shutil.rmtree(p) if os.path.isdir(p) and not os.path.islink(p) else os.unlink(p)
                removed.append(mac)
    ok, out, _ = prep.fsck_verify(image)
    if not ok:
        raise SystemExit('volume check failed after removing: ' + out[-300:])
    return removed


def move(image: str, pairs: list[str]) -> list[list[str]]:
    done = []
    with prep.mounted(image) as mnt:
        for pair in pairs:
            src, dst = pair.split('=', 1)
            sp = os.path.join(mnt, *[install.linux_name(x) for x in src.split(':') if x])
            dp = os.path.join(mnt, *[install.linux_name(x) for x in dst.split(':') if x])
            if not os.path.lexists(sp):
                raise SystemExit(f'move: {src} does not exist')
            if os.path.lexists(dp):
                raise SystemExit(f'move: {dst} already exists')
            os.rename(sp, dp)
            done.append([src, dst])
    ok, out, _ = prep.fsck_verify(image)
    if not ok:
        raise SystemExit('volume check failed after moving: ' + out[-300:])
    return done


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('image')
    ap.add_argument('pairs', nargs='*', help='TREE=DEST')
    ap.add_argument('--move', action='append', default=[], help='SRC=DEST Mac paths (rename/move in the volume)')
    ap.add_argument('--remove', action='append', default=[], help='Mac path to delete')
    ap.add_argument('--replace', action='store_true')
    ap.add_argument('--report', type=Path)
    a = ap.parse_args()
    reports = []
    if a.move:
        print(json.dumps({'moved': move(a.image, a.move)}, ensure_ascii=False), flush=True)
    if a.remove:
        print(json.dumps({'removed': remove(a.image, a.remove)}, ensure_ascii=False), flush=True)
    for pair in a.pairs:
        tree, dest = pair.split('=', 1)
        t0 = time.monotonic()
        r = install.install_tree(a.image, dest, tree, replace=a.replace)
        r['seconds'] = round(time.monotonic() - t0, 2)
        r['tree'] = tree
        reports.append(r)
        print(json.dumps({k: r.get(k) for k in ('tree', 'dest', 'top', 'fsck_before', 'fsck_after', 'seconds')}
                         | {'files': r.get('files'), 'rsrc_forks': r.get('resource_forks_moved'), 'verified': r.get('verified_items')}), flush=True)
    if a.report:
        a.report.write_text(json.dumps(reports, indent=1, ensure_ascii=False) + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
