#!/usr/bin/env python3
"""Prepare a stopped private Deluxe clone for on-demand original Myst media.

Keep frozen masters intact. The launcher and ISO are installed in Games:Myst;
run the native setup-menu.applescript afterward to replace only the menu alias.
"""
import argparse
import hashlib
import json
import os
import shutil
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'apps'), str(ROOT / 'guestprep')]
from rbapps.volume import partitions
from rbguestprep import prep
from rbguestprep.hfsplus import Volume
from rbguestprep.install import install_tree


def digest_region(file, offset, length):
    file.seek(offset)
    result = hashlib.sha256()
    while length:
        chunk = file.read(min(length, 4 << 20))
        if not chunk:
            raise ValueError('Truncated media region')
        result.update(chunk)
        length -= len(chunk)
    return result.hexdigest()


def prepare(image, media, launcher_tree):
    if image.is_symlink() or media.is_symlink():
        raise ValueError('Use regular files, not symlinked inputs')
    image, media = image.resolve(), media.resolve()
    if image == media or not image.is_file() or not media.is_file():
        raise ValueError('Use separate regular files and a stopped disposable clone')
    for process in Path('/proc').iterdir():
        if not process.name.isdigit():
            continue
        try:
            command = (process / 'cmdline').read_bytes()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if b'qemu-system-ppc' in command and os.fsencode(image) in command:
            raise ValueError('The guest disk is still in use')
    with Volume(str(image)) as startup:
        if not startup.clean:
            raise ValueError('The startup filesystem must be clean')
        startup.lookup(('Games', 'Myst', ' Myst'))
    with media.open('rb') as source, image.open('rb') as disk:
        header = disk.read(8)
        if header[:2] != b'ER' or struct.unpack_from('>H', header, 2)[0] != 512:
            raise ValueError('Expected a 512-byte Apple disk partition map')
        source_parts = [p for p in partitions(source, media.stat().st_size) if p.type == 'Apple_HFS']
        all_parts = partitions(disk, image.stat().st_size)
        if len(source_parts) != 1 or not all_parts:
            raise ValueError('Expected the original HFS media and an Apple partition map')
        last, original = all_parts[-1], source_parts[0]
        if (last.index < 2 or last.index != len(all_parts)
                or last.name != 'Myst CD' or last.type != 'Apple_HFS'
                or last.length != original.length or last.offset + last.length != image.stat().st_size
                or any(p.offset + p.length > last.offset for p in all_parts[:-1])):
            raise ValueError('The last partition is not the isolated embedded Myst volume')
        embedded_hash = digest_region(disk, last.offset, last.length)
        if embedded_hash != digest_region(source, original.offset, original.length):
            raise ValueError('Embedded media differs from the original ISO')
        iso_hash = digest_region(source, 0, media.stat().st_size)
    # Install before dropping the redundant partition. A failed installation
    # stays a disposable, unqualified copy with the original media still intact.
    with prep.mounted(str(image)) as mount:
        target = Path(mount) / 'Games/Myst/Myst CD.iso'
        if target.exists():
            raise ValueError('A local Myst image already exists')
        shutil.copyfile(media, target)
        os.setxattr(target, 'osx.com.apple.FinderInfo', b'GImgVCDM' + bytes(24))
        with target.open('rb') as check:
            if digest_region(check, 0, target.stat().st_size) != iso_hash:
                raise ValueError('Local ISO verification failed')
    installed = install_tree(str(image), ['Games', 'Myst'], str(launcher_tree), replace=False)
    with image.open('r+b') as disk:
        for index in range(1, last.index):
            disk.seek(index * 512 + 4)
            if struct.unpack('>I', disk.read(4))[0] != last.index:
                raise ValueError('Partition map changed during preparation')
        for index in range(1, last.index):
            disk.seek(index * 512 + 4)
            disk.write(struct.pack('>I', last.index - 1))
        disk.seek(last.index * 512)
        disk.write(bytes(512))
        disk.seek(4)
        disk.write(struct.pack('>I', last.offset // 512))
        disk.truncate(last.offset)
    return {'disk_bytes': image.stat().st_size, 'original_iso_bytes': media.stat().st_size,
            'original_iso_sha256': iso_hash, 'original_hfs_sha256': embedded_hash,
            'automatic_partition_removed': True, 'launcher_install': installed}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('--media', required=True, type=Path)
    parser.add_argument('--launcher-tree', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    facts = prepare(args.image, args.media, args.launcher_tree)
    args.out.write_text(json.dumps(facts, indent=1) + '\n')
    print(json.dumps(facts))
