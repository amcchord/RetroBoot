#!/usr/bin/env python3
"""Set the preferred memory size of an application or control panel in a
stopped guest disk (its 'SIZE' -1 resource, rewritten in place).

    appsize.py IMAGE "System Folder:Control Panels:Appearance" --preferred-k 4096 [--min-k N]

Equivalent to raising "Preferred Size" in the Finder's Get Info window.  The
Appearance control panel needs it: with the theme collections installed it
runs out of its 1280 K partition while listing them and quits with error 2.
Needs root (rb-dev); the hfsplus helpers write the fixed-size field only.
"""
import argparse
import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'guestprep'))
from rbguestprep import hfsplus, prep  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('image')
    ap.add_argument('path', help='Mac path below the volume root')
    ap.add_argument('--preferred-k', type=int, required=True)
    ap.add_argument('--min-k', type=int)
    a = ap.parse_args()
    with hfsplus.Volume(a.image, writable=True) as vol:
        rec = vol.lookup(a.path.split(':'))
        fork = vol.read_fork(rec, 'rsrc')
        for rtype, rid, _name, pos, data in hfsplus.parse_resource_fork(fork):
            if rtype == 'SIZE' and rid == -1:
                flags, pref, minimum = struct.unpack('>HII', data[:10])
                new_pref = a.preferred_k * 1024
                new_min = (a.min_k * 1024) if a.min_k else minimum
                if new_min > new_pref:
                    raise SystemExit('minimum above preferred')
                vol.write_fork_in_place(rec, 'rsrc', pos + 2, struct.pack('>II', new_pref, new_min))
                vol.flush()
                print(json.dumps({'path': a.path, 'preferred_k': [pref // 1024, new_pref // 1024],
                                  'min_k': [minimum // 1024, new_min // 1024]}))
                break
        else:
            raise SystemExit('no SIZE -1 resource')
    ok, out, _ = prep.fsck_verify(a.image)
    if not ok:
        raise SystemExit('volume check failed: ' + out[-300:])


if __name__ == '__main__':
    main()
