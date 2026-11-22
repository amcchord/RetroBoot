#!/usr/bin/env python3
"""Append an original HFS media volume to a stopped disposable APM guest disk.

The original filesystem bytes are copied unchanged into a read-only partition.
This keeps classic volume-name-based media self-contained in a two-file machine
archive. Never use against an active disk; preserve the source machine first.
"""
import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'apps'))
from rbapps import volume


def append(image: Path, media: Path) -> dict:
    if image.is_symlink() or media.is_symlink() or image.resolve() == media.resolve():
        raise ValueError('use separate regular disk and media files')
    old_size = image.stat().st_size
    if old_size % 512:
        raise ValueError('guest disk is not sector aligned')
    with media.open('rb') as src:
        parts = [p for p in volume.partitions(src, media.stat().st_size) if p.type == 'Apple_HFS']
        if len(parts) != 1:
            raise ValueError('media must contain exactly one Apple_HFS volume')
        p = parts[0]
        if p.offset < 0 or p.length <= 2048 or p.offset + p.length > media.stat().st_size:
            raise ValueError('media partition extends beyond its file')
        if p.length % 512:
            raise ValueError('media volume is not sector aligned')
        src.seek(p.offset + 1024)
        if src.read(2) != b'BD':
            raise ValueError('expected original HFS media')
        with image.open('r+b') as dst:
            header = bytearray(dst.read(512))
            first = bytearray(dst.read(512))
            if header[:2] != b'ER' or struct.unpack_from('>H', header, 2)[0] != 512 or first[:2] != b'PM':
                raise ValueError('guest disk must have a 512-byte Apple partition map')
            count, map_start, map_blocks = struct.unpack_from('>III', first, 4)
            if map_start != 1 or not 1 <= count < min(map_blocks, 63):
                raise ValueError('no free partition-map entry')
            records = [first]
            for _ in range(1, count):
                records.append(bytearray(dst.read(512)))
            for r in records:
                if r[:2] != b'PM' or struct.unpack_from('>I', r, 4)[0] != count:
                    raise ValueError('inconsistent partition map')
                start, blocks = struct.unpack_from('>II', r, 8)
                if (start + blocks) * 512 > old_size:
                    raise ValueError('partition extends beyond source disk')
            new_start = old_size // 512
            blocks = p.length // 512
            if new_start + blocks >= 2**32:
                raise ValueError('disk exceeds Apple partition-map limit')
            record = bytearray(512)
            record[:2] = b'PM'
            struct.pack_into('>III', record, 4, count + 1, new_start, blocks)
            record[16:23] = b'Myst CD'
            record[48:57] = b'Apple_HFS'
            # Match Mac OS's mounted HFS data-partition attributes, clearing the
            # writable bit (0x20). Merely valid/allocated/readable (0x13) is not
            # enumerated by the classic disk driver. Qualified with original
            # Myst media, including a save/reload across a cold boot.
            struct.pack_into('>III', record, 80, 0, blocks, 0x4400035f)
            src.seek(p.offset)
            dst.seek(old_size)
            digest = hashlib.sha256()
            remaining = p.length
            while remaining:
                data = src.read(min(4 << 20, remaining))
                if not data:
                    raise ValueError('truncated media')
                dst.write(data)
                digest.update(data)
                remaining -= len(data)
            dst.flush()
            dst.seek(old_size)
            copied = hashlib.sha256()
            remaining = p.length
            while remaining:
                data = dst.read(min(4 << 20, remaining))
                copied.update(data)
                remaining -= len(data)
            if copied.digest() != digest.digest():
                raise ValueError('media readback mismatch')
            for i, r in enumerate(records, 1):
                struct.pack_into('>I', r, 4, count + 1)
                dst.seek(i * 512)
                dst.write(r)
            dst.seek((count + 1) * 512)
            dst.write(record)
            struct.pack_into('>I', header, 4, new_start + blocks)
            dst.seek(0)
            dst.write(header)
    return {'media_bytes': p.length, 'media_sha256': digest.hexdigest(),
            'disk_bytes': old_size + p.length, 'partition': count + 1, 'readonly_partition': True}


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('image', type=Path)
    ap.add_argument('media', type=Path)
    a = ap.parse_args()
    print(json.dumps(append(a.image, a.media), indent=2))
