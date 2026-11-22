#!/usr/bin/env python3
"""Build a machine catalog archive (thin by default) from a machine directory.

    catalog_archive.py MACHINE_DIR OUT.tar.gz [--classic] [--level N] [--preview]
                       [--url URL] [--verify DIR] [--json FILE]

MACHINE_DIR holds ``config.json`` and ``disk.img`` (a registered fixture is
only read).  A 68k Mac's directory (config.json ``machineFamily``
``quadra800``, D044; guest/build/quadra_machine.py) also contributes its
``Quadra800.rom`` and ``pram.img``, which hostd's import keeps with the Mac
(it checks the ROM's SHA-256); such an archive is private like the disk.  The default output is a RetroBoot *thin archive*: gzip of a POSIX
tar whose ``disk.img`` is a GNU PAX sparse 1.0 member carrying only the disk's
non-zero 4 KiB blocks (``hostd/retroboot/sparsetar.py``; ARCHITECTURE
"Machine archives and storage").  hostd imports it in seconds and writes only
the data.  ``--classic`` writes the ClassicMac schema-1 archive instead (every
disk byte stored, regular members), for ClassicMac and hostd before thin
archives; it takes minutes for a 32 GiB disk.

Both are deterministic (mtime 0, uid/gid 0, gzip without name or time).  The
archive is written as OUT.tar.gz.part and renamed; ``OUT.tar.gz.sha256``
(``<sha256>  <name>``) is written beside it, and the catalog values are
printed as JSON (``--json`` also saves them):

* thin: ``{"thinArchive": {"format", "archiveURL", "archiveBytes", "sha256"},
  "installedBytes", "diskCapacityBytes", "requiredStorageBytes", ...}``; merge
  ``thinArchive`` into the catalog entry of the same machine (ClassicMac
  ignores it) or, in a RetroBoot-only catalog, use its URL/bytes/SHA-256 as
  the entry's ``archiveURL``/``archiveBytes``/``sha256``;
* classic: ``{"archiveURL", "archiveBytes", "sha256", "installedBytes",
  "diskCapacityBytes", "requiredStorageBytes"}``.

``installedBytes``/``diskCapacityBytes``/``requiredStorageBytes`` are the same
for both formats of one machine (ClassicMac's 1 MiB-chunk accounting).

``--verify DIR`` extracts the result with this checkout's hostd
``catalog.extract_archive`` under those catalog limits into a temporary
directory in DIR (use an XFS filesystem like RBDATA) and compares the disk's
SHA-256 with the source (``fixture.json``'s ``disk_sha256`` when present).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# --verify needs a hostd that knows the archive's members (a 68k Mac's ROM and
# PRAM: the quadra-hostd branch and later); RB_HOSTD_SRC points at its hostd/.
sys.path.insert(0, os.environ.get('RB_HOSTD_SRC') or str(ROOT / 'hostd'))
from retroboot import sparsetar  # noqa: E402


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while chunk := f.read(4 << 20):
            h.update(chunk)
    return h.hexdigest()


def verify(archive: Path, facts: dict, src_disk: Path, want: str | None, where: Path) -> dict:
    from retroboot import catalog
    work = Path(tempfile.mkdtemp(prefix='.verify-', dir=where))
    try:
        t0 = time.monotonic()
        info = catalog.extract_archive(archive, work / 'm', catalog.ExtractLimits(
            facts['installedBytes'], facts['requiredStorageBytes']))
        took = time.monotonic() - t0
        got = sha256_file(work / 'm' / 'disk.img')
        want = want or sha256_file(src_disk)
        st = (work / 'm' / 'disk.img').stat()
        return {'ok': got == want, 'disk_sha256': got, 'expected_sha256': want, 'extract_s': round(took, 2),
                'allocated_bytes': st.st_blocks * 512, 'stored_bytes': info['stored_bytes']}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('machine_dir', type=Path)
    ap.add_argument('out', type=Path)
    ap.add_argument('--classic', action='store_true', help='ClassicMac schema-1 archive (every disk byte stored)')
    ap.add_argument('--level', type=int, help='gzip level (default 9 thin, 6 classic)')
    ap.add_argument('--preview', action='store_true', help='include preview.png')
    ap.add_argument('--url', help='archive URL to put in the printed catalog values')
    ap.add_argument('--verify', type=Path, metavar='DIR', help='extract with hostd into DIR and compare hashes')
    ap.add_argument('--json', type=Path)
    a = ap.parse_args()
    if not a.out.name.endswith('.tar.gz'):
        ap.error('OUT must end in .tar.gz (hostd and its archive sources look for that name)')
    names = ['config.json', 'disk.img'] + (['preview.png'] if a.preview else [])
    try:
        family = json.loads((a.machine_dir / 'config.json').read_text()).get('machineFamily')
    except (OSError, ValueError):
        family = None
    if family == 'quadra800':
        names += [n for n in ('Quadra800.rom', 'pram.img') if (a.machine_dir / n).is_file()]
    files = []
    for n in names:
        p = a.machine_dir / n
        if not p.is_file() or p.is_symlink():
            ap.error(f'{p} is not a regular file')
        files.append((n, p))
    part = a.out.with_name(a.out.name + '.part')
    t0 = time.monotonic()
    with open(part, 'xb') as f:
        if a.classic:
            facts = sparsetar.write_classic(f, files, level=a.level or 6)
        else:
            facts = sparsetar.write_thin(f, files, level=a.level or 9)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(part, 0o444)
    os.replace(part, a.out)
    side = a.out.with_name(a.out.name + '.sha256')
    side.write_text(f"{facts['sha256']}  {a.out.name}\n")
    keep = {k: facts[k] for k in ('installedBytes', 'diskCapacityBytes', 'requiredStorageBytes')}
    arc = {'archiveBytes': facts['archiveBytes'], 'sha256': facts['sha256']}
    if a.url:
        arc = {'archiveURL': a.url, **arc}
    if a.classic:
        out = {**arc, **keep}
    else:
        out = {'thinArchive': {'format': facts['format'], **arc}, **keep,
               'storedBytes': facts['storedBytes'], 'extents': facts['extents']}
    out['build_s'] = round(time.monotonic() - t0, 1)
    if a.verify:
        fixture = a.machine_dir / 'fixture.json'
        want = None
        if fixture.is_file():
            want = json.loads(fixture.read_text()).get('disk_sha256')
        out['verify'] = verify(a.out, keep,
                               a.machine_dir / 'disk.img', want, a.verify)
    text = json.dumps(out, indent=1)
    print(text)
    if a.json:
        a.json.write_text(text + '\n')
    return 0 if out.get('verify', {}).get('ok', True) else 1


if __name__ == '__main__':
    sys.exit(main())
