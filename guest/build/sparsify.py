#!/usr/bin/env python3
"""Punch holes in a stopped guest disk image where the HFS+ volume is free.

    sparsify.py IMAGE [--dry-run]

Mac OS leaves the old contents of deleted files (installer temporaries, the
VM Storage file, removed themes) in free allocation blocks; the raw image
keeps them allocated on the host.  This reads the volume's allocation file
and deallocates every free run of at least 64 KiB in the host file
(fallocate PUNCH_HOLE | KEEP_SIZE), so a sparse copy of the disk carries only
what the volume uses.  The volume's own structures are not touched; a free
block reads back as zeros, which no HFS+ implementation relies on either way.
Linux only (fallocate); run on a stopped guest's disk.
"""
import argparse
import ctypes
import ctypes.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'guestprep'))
from rbguestprep import hfsplus, prep  # noqa: E402

FALLOC_FL_KEEP_SIZE = 0x01
FALLOC_FL_PUNCH_HOLE = 0x02
MIN_RUN = 64 * 1024


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('image')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    libc = ctypes.CDLL(ctypes.util.find_library('c'), use_errno=True)
    libc.fallocate.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_longlong, ctypes.c_longlong]
    before = os.stat(a.image).st_blocks * 512
    with hfsplus.Volume(a.image) as vol:
        runs = vol.free_runs()
        bs, base = vol.block_size, vol.offset
    punched = 0
    fd = os.open(a.image, os.O_RDWR)
    try:
        for start, count in runs:
            length = count * bs
            if length < MIN_RUN:
                continue
            if not a.dry_run and libc.fallocate(fd, FALLOC_FL_PUNCH_HOLE | FALLOC_FL_KEEP_SIZE,
                                                base + start * bs, length) != 0:
                raise OSError(ctypes.get_errno(), 'fallocate punch hole failed')
            punched += length
        os.fsync(fd)
    finally:
        os.close(fd)
    after = os.stat(a.image).st_blocks * 512
    ok, out, _ = prep.fsck_verify(a.image)
    print(json.dumps({'free_runs': len(runs), 'punched_bytes': punched, 'allocated_before': before,
                      'allocated_after': after, 'fsck': 'ok' if ok else out[-200:]}))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
