#!/usr/bin/env python3
"""Wrap host files, unexpanded, as a canonical tree so the guest expands them.

    rawtree.py --out TREE [--folder NAME] FILE:TYPE:CREATOR[:MACNAME] ...

For archives that are better opened inside Mac OS than on the host (for
example a StuffIt archive whose entries ``unar`` cannot place, such as a file
and a folder with the same name), the file goes into the guest as a single
data fork with the given type and creator (``SITD``/``SIT!`` opens in StuffIt
Expander), inside FOLDER when given.  Nothing is executed or parsed on the
host: the bytes are copied and hashed.  Run with ``python3 -I``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'apps'))
from rbapps import tree  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--folder')
    ap.add_argument('files', nargs='+', help='FILE:TYPE:CREATOR[:MACNAME]')
    a = ap.parse_args()
    specs = []
    for s in a.files:
        parts = s.split(':')
        if len(parts) not in (3, 4) or len(parts[1]) != 4 or len(parts[2]) != 4:
            ap.error(f'bad spec {s!r} (FILE:TYPE:CREATOR[:MACNAME], codes of four characters)')
        path = Path(parts[0])
        specs.append((path, parts[1], parts[2], parts[3] if len(parts) == 4 else path.name))
    sources = []
    for path, _t, _c, _n in specs:
        data = path.read_bytes()
        sources.append({'name': path.name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
    w = tree.TreeWriter(a.out, {'kind': 'raw-files', 'files': sources})
    prefix = (a.folder,) if a.folder else ()
    for path, t, c, name in specs:
        fi = tree.file_finder_info(tree.code_bytes(t), tree.code_bytes(c), 0)
        w.file(prefix + (name,), path.read_bytes(), b'', fi)
    m = w.finish()
    print(json.dumps({'tree': str(a.out), 'summary': m['summary'], 'top': m['top']}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
