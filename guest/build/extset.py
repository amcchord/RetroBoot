#!/usr/bin/env python3
"""Turn Mac OS 9 startup items off or on in a stopped guest disk, the way
Extensions Manager does it: by moving them between ``FOLDER`` and
``FOLDER (Disabled)`` inside the blessed System Folder.

    extset.py IMAGE --off "Extensions:Time Synchronizer" ... [--on ITEM ...] [--list]
    extset.py IMAGE --set guest/sets/retroboot.json [--report FILE]

ITEM is ``FOLDER:NAME`` relative to the System Folder, for FOLDER one of
Extensions, Control Panels, Control Strip Modules, Startup Items, Shutdown
Items, System Extensions.  A set file is JSON ``{"name": ..., "off": [ITEM,
...]}``; applying a set turns exactly those items off (and turns back on any
other item found in a "(Disabled)" folder).  Missing items are reported, not
created.  Extensions Manager, at the next start, shows the result as its
current state, so an Extensions Manager set saved from it ("Duplicate Set")
records the same choice.

Needs root, the hfsplus module and fsck.hfsplus (rb-dev).  Work on a copy.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'guestprep'))
from rbguestprep import hfsplus, prep  # noqa: E402

FOLDERS = ('Extensions', 'Control Panels', 'Control Strip Modules', 'Startup Items', 'Shutdown Items',
           'System Extensions')


def lname(mac: str) -> str:
    return mac.replace('/', ':')


def system_folder(image: str) -> list[str]:
    with hfsplus.Volume(image) as vol:
        return prep._system_folder(vol)


def state(mnt: str, sysf: list[str]) -> dict:
    base = os.path.join(mnt, *map(lname, sysf))
    out = {}
    for f in FOLDERS:
        for suffix, on in (('', True), (' (Disabled)', False)):
            d = os.path.join(base, f + suffix)
            if os.path.isdir(d):
                for n in os.listdir(d):
                    if n == 'Icon\r':
                        continue
                    out[f'{f}:{n.replace(":", "/")}'] = on
    return out


def apply(image: str, off: set[str], on: set[str], exclusive: bool) -> dict:
    sysf = system_folder(image)
    report = {'off': [], 'on': [], 'missing': []}
    with prep.mounted(image) as mnt:
        base = os.path.join(mnt, *map(lname, sysf))
        cur = state(mnt, sysf)
        want = {}
        for item in off:
            want[item] = False
        for item in on:
            want[item] = True
        if exclusive:
            for item, is_on in cur.items():
                if not is_on and item not in want:
                    want[item] = True
        for item, target in sorted(want.items()):
            if item not in cur:
                report['missing'].append(item)
                continue
            if cur[item] == target:
                continue
            folder, name = item.split(':', 1)
            src = os.path.join(base, folder + ('' if cur[item] else ' (Disabled)'), lname(name))
            dst_dir = os.path.join(base, folder + ('' if target else ' (Disabled)'))
            os.makedirs(dst_dir, exist_ok=True)
            os.rename(src, os.path.join(dst_dir, lname(name)))
            report['on' if target else 'off'].append(item)
    ok, out, _ = prep.fsck_verify(image)
    if not ok:
        raise SystemExit('volume check failed: ' + out[-300:])
    report['fsck_after'] = 'ok'
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('image')
    ap.add_argument('--off', action='append', default=[])
    ap.add_argument('--on', action='append', default=[])
    ap.add_argument('--set', type=Path)
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--report', type=Path)
    a = ap.parse_args()
    if a.list:
        sysf = system_folder(a.image)
        with prep.mounted(a.image) as mnt:
            for k, v in sorted(state(mnt, sysf).items()):
                print(('on  ' if v else 'off ') + k)
        return
    off, on, exclusive = set(a.off), set(a.on), False
    if a.set:
        spec = json.loads(a.set.read_text())
        off |= {i for g in spec['off'].values() for i in g} if isinstance(spec['off'], dict) else set(spec['off'])
        exclusive = True
    r = apply(a.image, off, on, exclusive)
    print(json.dumps(r, indent=1, ensure_ascii=False))
    if a.report:
        a.report.write_text(json.dumps(r, indent=1, ensure_ascii=False) + '\n')


if __name__ == '__main__':
    main()
