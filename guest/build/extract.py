#!/usr/bin/env python3
"""Copy files and folders out of a classic Mac disk image into a canonical tree.

    extract.py IMAGE --out TREE PATH [PATH ...] [--strip N] [--exclude NAME]

PATH is a Mac path below the volume root with ':' separators (for example
``System Folder:Extensions:AirPort Driver`` or a folder such as
``Applications (Mac OS 9):Apple Extras:AirPort``).  Folders are copied with
everything inside.  In the tree each item keeps its path minus the first
``--strip`` components, so ``--strip 2`` turns ``System Folder:Extensions:X``
into ``X`` (ready for ``rbguestprep.install.install_tree`` into that folder).
Finder info is copied with kHasBeenInited cleared (the Finder places the item
and registers its bundle).  Alias files are skipped and listed: an alias
records the source volume's identity and would not resolve on another disk
(make it again in the guest).  Read-only on the image; pure Python (rbapps).
"""
from __future__ import annotations

import argparse
import json
import os
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'apps'))
from rbapps import tree as rtree, volume  # noqa: E402


def extract(image: Path, out: Path, paths: list[str], strip: int, exclude: set[str]) -> dict:
    size = image.stat().st_size
    with open(image, 'rb') as f:
        vol = volume.open_volume(f, size, 0)
        entries = vol.entries()
        want = [tuple(p.split(':')) for p in paths]
        found = {w: False for w in want}
        w = rtree.TreeWriter(out, {'kind': 'disk-image-extract', 'image': image.name, 'paths': paths})
        for e in entries:
            hit = next((p for p in want if e.path[:len(p)] == p), None)
            if hit is None or any(part in exclude for part in e.path):
                continue
            found[hit] = True
            rel = e.path[strip:]
            if not rel:
                continue
            fi = bytearray(e.finder_info.ljust(32, b'\0')[:32])
            flags = struct.unpack('>H', fi[8:10])[0] & ~rtree.FLAG_HAS_BEEN_INITED
            fi[8:10] = struct.pack('>H', flags)
            if e.kind == 'file' and flags & 0x8000:      # kIsAlias
                w.skip(rel, 'alias')
                continue
            if e.kind == 'folder':
                w.folder(rel, bytes(fi))
            else:
                w.file(rel, vol.read_fork(e, 'data'), vol.read_fork(e, 'rsrc'), bytes(fi))
        missing = [':'.join(p) for p, ok in found.items() if not ok]
        if missing:
            raise SystemExit(f'not found on {image.name}: {missing}')
        return w.finish()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('image', type=Path)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--strip', type=int, default=0)
    ap.add_argument('--exclude', action='append', default=[])
    ap.add_argument('paths', nargs='+')
    a = ap.parse_args()
    m = extract(a.image, a.out, a.paths, a.strip, set(a.exclude))
    print(json.dumps(m['summary']))


if __name__ == '__main__':
    main()
