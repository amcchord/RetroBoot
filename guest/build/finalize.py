#!/usr/bin/env python3
"""Last offline stage of the base disk: add-ons, cleanup, guest prep, sparse.

    finalize.py IMAGE [--addon 'COMMAND {image}' ...] [--report FILE]

Runs on a copy of the base after the in-guest steps (guest/README.md, step
8) and before registration:

1. add-ons, in order: each ``--addon`` is a shell command with ``{image}``
   replaced by the disk's absolute path (for example the mouse workstream's USB
   Overdrive step); a failing add-on stops the run.  The add-on must leave
   the volume consistent (it is checked next);
2. usage traces from building and testing are removed: the Apple menu's
   Recent Applications/Documents/Servers, Internet Explorer's history,
   page cache, download cache and temporary files, Netscape's cache, history
   and cookies, iCab's cache and history, Classilla's history, cookies and
   cache (the browsers recreate these files on launch); so are
   the installer's own preference files that guest prep only fills in when
   they are missing (``RESET_PREFS``: AppleTalk Preferences, which the Mac OS
   installer leaves with AppleTalk active), so prep installs RetroBoot's;
3. ``rbguestprep apply`` (Ethernet/DHCP TCP/IP, Software Update check
   off, sleep never on desktops and portables, AppleTalk inactive, no Disk
   First Aid prompt);
4. free HFS+ blocks are punched out of the host file (sparsify.py) and
   ``fsck.hfsplus -fn`` must pass.

Needs root, the hfsplus module and hfsprogs (rb-dev).
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / 'guestprep'))
from rbguestprep import prep  # noqa: E402

TRACE_FOLDERS = ('Apple Menu Items/Recent Applications', 'Apple Menu Items/Recent Documents',
                 'Apple Menu Items/Recent Servers', 'Preferences/Explorer/Temporary Files',
                 'Preferences/Netscape Users/RetroBoot/Cache ƒ', 'Preferences/iCab Preferences/iCab Cache')
TRACE_FILES = ('Preferences/Explorer/History.html', 'Preferences/Explorer/Download Cache',
               'Preferences/MS Internet Cache/cache.waf', 'Preferences/MS Internet Cache/IE Cache.waf',
               'Preferences/Netscape Users/RetroBoot/Netscape History',
               'Preferences/Netscape Users/RetroBoot/MagicCookie',
               'Preferences/iCab Preferences/iCab Global History')
# Classilla keeps its profile under Documents:Mozilla:Profiles:<name>:<salt>.slt
CLASSILLA_PROFILES = 'Documents/Mozilla/Profiles'
CLASSILLA_TRACES = ('history.dat', 'cookies.txt', 'formhistory.dat')
RESET_PREFS = ('Preferences/AppleTalk Preferences',)


def traces(image: str) -> list[str]:
    found = []
    with prep.mounted(image) as m:
        sf = os.path.join(m, 'System Folder')
        for d in TRACE_FOLDERS:
            p = os.path.join(sf, d)
            if os.path.isdir(p):
                found += ['System Folder:' + d.replace('/', ':') + ':' + n for n in sorted(os.listdir(p))
                          if n != 'Icon\r']
        found += ['System Folder:' + f.replace('/', ':') for f in TRACE_FILES + RESET_PREFS
                  if os.path.exists(os.path.join(sf, f))]
        prof = os.path.join(m, CLASSILLA_PROFILES)
        if os.path.isdir(prof):
            for name in sorted(os.listdir(prof)):
                pdir = os.path.join(prof, name)
                for salt in sorted(os.listdir(pdir)) if os.path.isdir(pdir) else []:
                    base = CLASSILLA_PROFILES.replace('/', ':') + ':' + name + ':' + salt
                    sdir = os.path.join(pdir, salt)
                    found += [base + ':' + f for f in CLASSILLA_TRACES if os.path.exists(os.path.join(sdir, f))]
                    cache = os.path.join(sdir, 'Cache')
                    if os.path.isdir(cache):
                        found += [base + ':Cache:' + n for n in sorted(os.listdir(cache))]
    return found


def run(cmd: list[str]) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('image')
    ap.add_argument('--addon', action='append', default=[])
    ap.add_argument('--report', type=Path)
    a = ap.parse_args()
    report: dict = {'image': a.image, 'addons': []}
    for cmd in a.addon:
        line = cmd.replace('{image}', shlex.quote(os.path.abspath(a.image)))
        res = subprocess.run(line, shell=True, capture_output=True, text=True)
        report['addons'].append({'command': cmd, 'rc': res.returncode, 'tail': (res.stdout + res.stderr)[-400:]})
        if res.returncode:
            print(json.dumps(report, indent=1))
            return 1
    gone = traces(a.image)
    if gone:
        args = [sys.executable, str(HERE / 'offline_install.py'), a.image]
        for g in gone:
            args += ['--remove', g]
        run(args)
    report['removed_traces'] = gone
    report['guestprep'] = json.loads(subprocess.run(
        [sys.executable, '-m', 'rbguestprep', 'apply', os.path.abspath(a.image)], cwd=str(ROOT / 'guestprep'),
        check=True, capture_output=True, text=True).stdout)
    report['sparsify'] = json.loads(run([sys.executable, str(HERE / 'sparsify.py'), a.image]))
    print(json.dumps(report, indent=1, ensure_ascii=False))
    if a.report:
        a.report.write_text(json.dumps(report, indent=1, ensure_ascii=False) + '\n')
    return 0 if report['sparsify'].get('fsck') == 'ok' else 1


if __name__ == '__main__':
    sys.exit(main())
