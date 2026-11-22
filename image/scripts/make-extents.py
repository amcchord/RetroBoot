#!/usr/bin/env python3
"""Write the data map of a sparse guest disk for the installer's copy.

    make-extents.py DISK HOSTD_LIB_DIR        # writes DISK.extents

make-iso.sh embeds the guest disk in a squashfs, which stores zero blocks
sparsely but reports no holes when read, so without a map install-to-disk
would read all 32 GiB of a Mac OS disk to copy its few hundred MiB of data.
The map lists the disk's non-zero 4 KiB blocks (hostd's
``sparsetar.data_extents``: SEEK_DATA/SEEK_HOLE plus a zero scan) and is bound
to the file by size and whole-second mtime, which squashfs keeps:
``{"format": "retroboot-extents-1", "size", "mtime", "extents": [[offset, length], ...]}``.
install-to-disk ignores a map that does not match and never copies it.
"""
import json
import os
import sys


def main(disk, hostd_lib):
    sys.path.insert(0, hostd_lib)
    from retroboot.sparsetar import data_extents
    with open(disk, 'rb') as f:
        st = os.fstat(f.fileno())
        ext = data_extents(f.fileno(), st.st_size)
        after = os.fstat(f.fileno())
    if (after.st_size, after.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
        sys.exit(f'make-extents: {disk} changed while it was read')
    doc = {'format': 'retroboot-extents-1', 'size': st.st_size, 'mtime': int(st.st_mtime),
           'extents': [list(e) for e in ext]}
    with open(disk + '.extents', 'w') as f:
        json.dump(doc, f, separators=(',', ':'))
        f.write('\n')
    print(f'make-extents: {len(ext)} extents, {sum(n for _, n in ext)} data bytes of {st.st_size}', file=sys.stderr)


if __name__ == '__main__':
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
