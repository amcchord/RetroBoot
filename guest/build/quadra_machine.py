#!/usr/bin/env python3
"""Make a Quadra 800 (68k) machine directory from a finished base disk (D044).

    quadra_machine.py OUT_DIR --id mac-os-8-1-v1 --name "Mac OS 8.1" --os-version 8.1
                      --disk DISK --pram PRAM --rom ROM [--ram 128] [--size 800x600x8]
                      [--fixture-id mac-os-8.1-v1] [--source-json FILE]

OUT_DIR (must not exist) receives what hostd keeps for a Quadra Mac and what
its catalog import reads:

* ``disk.img``: a sparse copy of DISK (only non-zero 64 KiB blocks written);
* ``Quadra800.rom``: ROM, which must be the Quadra 610/650/800 ROM hostd
  accepts (SHA-256 05ad753f...09ca); ``pram.img``: PRAM (256 or 512 bytes);
* ``config.json``: the ClassicMac-style template (``machineFamily``
  ``quadra800``, ``ramMB``, ``width``, ``height``, ``depth``, ...), the first
  member of the catalog archive (guest/build/catalog_archive.py);
* ``machine.json``: hostd's machine record (schema 1, ``family``
  ``quadra800``, ``rom``, ``pram``, ``os_version``, ``template``, ``files``)
  as hostd's catalog import would write it, so the directory can also be
  copied into /var/lib/retroboot/machines/<id>/ as is;
* ``fixture.json``: SHA-256 and sizes of every file, the volume facts from
  ``rbguestprep 68k inspect`` and the source description.

Everything here is Apple software or made by it: private, never in Git.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "guestprep"))
from rbguestprep import pram68k, prep68k  # noqa: E402

ROM_SHA256 = "05ad753fb594e656cf078023ec189e09e2a7655a780de993b75b8c51ed6b09ca"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")      # hostd's MACHINE_ID_RE
CHUNK = 64 * 1024


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(4 << 20):
            h.update(chunk)
    return h.hexdigest()


def sparse_copy(src: Path, dst: Path) -> int:
    zero = bytes(CHUNK)
    written = 0
    size = src.stat().st_size
    with open(src, "rb") as fi, open(dst, "xb") as fo:
        pos = 0
        while pos < size:
            buf = fi.read(CHUNK)
            if not buf:
                break
            if buf != zero[:len(buf)]:
                fo.seek(pos)
                fo.write(buf)
                written += len(buf)
            pos += len(buf)
        fo.truncate(size)
        fo.flush()
        os.fsync(fo.fileno())
    return written


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out", type=Path)
    ap.add_argument("--id", required=True)
    ap.add_argument("--fixture-id")
    ap.add_argument("--name", required=True)
    ap.add_argument("--os-version", required=True)
    ap.add_argument("--disk", required=True, type=Path)
    ap.add_argument("--pram", required=True, type=Path)
    ap.add_argument("--rom", required=True, type=Path)
    ap.add_argument("--ram", type=int, default=128)
    ap.add_argument("--size", default="800x600x8")
    ap.add_argument("--source-json", type=Path)
    a = ap.parse_args()
    if not ID_RE.fullmatch(a.id):
        ap.error("--id must match hostd's machine id pattern ^[a-z0-9][a-z0-9-]{0,47}$")
    if sha256(a.rom) != ROM_SHA256:
        ap.error("--rom is not the Quadra 800 ROM hostd accepts")
    if a.pram.stat().st_size not in (256, 512):
        ap.error("--pram must be 256 or 512 bytes")
    p = pram68k.inspect(str(a.pram))
    if not (p["valid"] and p["addressing_32bit"]):
        ap.error(f"--pram is not usable on QEMU's q800 (needs NuMc and 32-bit addressing): {p}")
    facts = prep68k.inspect(str(a.disk), "none")
    if not facts["clean"] or facts["shutdown_check"] != "absent":
        ap.error(f"--disk was not shut down cleanly: {facts}")
    w, h, d = (int(v) for v in a.size.split("x"))
    a.out.mkdir(parents=True)
    stored = sparse_copy(a.disk, a.out / "disk.img")
    shutil.copyfile(a.rom, a.out / "Quadra800.rom")
    shutil.copyfile(a.pram, a.out / "pram.img")
    template = {"name": a.name, "machineFamily": "quadra800", "ramMB": a.ram, "width": w, "height": h, "depth": d,
                "diskImageName": "disk.img", "diskSizeGB": round(a.disk.stat().st_size / 1024 ** 3),
                "sound": True, "networking": True, "tabletInput": True, "bootFromCD": False}
    (a.out / "config.json").write_text(json.dumps(template, indent=2) + "\n")
    files = {}
    for n in ("config.json", "disk.img", "Quadra800.rom", "pram.img"):
        st = (a.out / n).stat()
        files[n] = {"bytes": st.st_size, "sha256": sha256(a.out / n), "allocated_bytes": st.st_blocks * 512}
    source = json.loads(a.source_json.read_text()) if a.source_json else {}
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    keep = {k: template[k] for k in ("name", "width", "height", "depth", "ramMB", "sound", "networking",
                                     "tabletInput", "diskSizeGB", "machineFamily")}
    machine = {"schema_version": 1, "id": a.id, "name": a.name, "os_version": a.os_version, "disk": "disk.img",
               "created": now, "source": {"kind": "fixture", "fixture": a.fixture_id or a.id},
               "template": keep, "files": {k: v for k, v in files.items() if k != "config.json"},
               "family": "quadra800", "rom": "Quadra800.rom", "pram": "pram.img"}
    (a.out / "machine.json").write_text(json.dumps(machine, indent=2) + "\n")
    fixture = {"id": a.fixture_id or a.id, "machine_id": a.id, "name": a.name, "os_version": a.os_version,
               "created": now, "family": "quadra800", "disk_sha256": files["disk.img"]["sha256"],
               "disk_bytes": files["disk.img"]["bytes"], "disk_data_bytes": stored, "files": files,
               "volume": facts, "pram": p, "source": source}
    (a.out / "fixture.json").write_text(json.dumps(fixture, indent=2) + "\n")
    print(json.dumps({"out": str(a.out), "disk_sha256": files["disk.img"]["sha256"],
                      "disk_data_bytes": stored}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
