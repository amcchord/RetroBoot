#!/usr/bin/env python3
"""Turn Retro68's MacBinary outputs for RetroBoot Display into one rbapps
canonical tree.

    make_init_tree.py --out TREE BIN=MAC NAME [BIN=MAC NAME ...]

Each input is either the 68k system extension (file type INIT, an empty data
fork and exactly one 'INIT' resource of flat 68k code, no 'CODE' or 'cfrg')
or the PowerPC background-only application (type appe, 'cfrg' and a PEF data
fork; research/gfx/guestapps/make_tree.py's checks).  Each file keeps its type
and creator; kHasBeenInited is cleared so the Finder registers it, and so is
kHasNoINITs (0x0080: Mac OS sets it on a file it found without 'INIT'
resources, and the INIT loader then skips the file for good).  The tree
(apps/rbapps/tree.py format) goes into the guest's System Folder:Extensions
with rbguestprep (``rbguestprep ati install --helper TREE``).
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "apps"))
sys.path.insert(0, str(ROOT / "research" / "gfx" / "guestapps"))
from rbapps import tree  # noqa: E402
from rbapps.resfork import parse_resource_fork  # noqa: E402
from make_tree import MacBinaryError, check_ppc_app, read_macbinary  # noqa: E402

FLAG_HAS_NO_INITS = 0x0080


def check_init(label: str, mb: dict) -> dict:
    if mb["type"] != b"INIT":
        raise MacBinaryError(f"{label}: type {mb['type']!r} is not INIT")
    if mb["data"]:
        raise MacBinaryError(f"{label}: unexpected data fork ({len(mb['data'])} bytes)")
    res = parse_resource_fork(mb["rsrc"])
    kinds = sorted(f"{t}:{i}" for t, i, _n, _o, _d in res)
    inits = [d for t, _i, _n, _o, d in res if t == "INIT"]
    if len(inits) != 1 or len(inits[0]) < 16:
        raise MacBinaryError(f"{label}: expected exactly one 'INIT' resource")
    if any(t in ("CODE", "cfrg") for t, _i, _n, _o, _d in res):
        raise MacBinaryError(f"{label}: has 'CODE' or 'cfrg' resources; expected a flat 68k INIT")
    return {"resources": kinds, "init_bytes": len(inits[0])}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, type=Path, help="tree directory (must be empty or absent)")
    ap.add_argument("inputs", nargs="+", help="MacBinary file=Mac name")
    a = ap.parse_args(argv)
    files = []
    for item in a.inputs:
        src, _, mac_name = item.partition("=")
        path = Path(src)
        mb = read_macbinary(path)
        info = check_init(path.name, mb) if mb["type"] == b"INIT" else check_ppc_app(path.name, mb, (b"appe",))
        files.append((path, mac_name or mb["name"], mb, info))
    source = {"kind": "retro68-macbinary", "tool": "guest/rbdisplay/init/make_init_tree.py",
              "inputs": [{"file": path.name, "name": name, "format": mb["format"],
                          "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), **info}
                         for path, name, mb, info in files]}
    writer = tree.TreeWriter(a.out, source)
    for path, name, mb, info in files:
        flags = mb["flags"] & ~(tree.FLAG_HAS_BEEN_INITED | FLAG_HAS_NO_INITS)
        writer.file((name,), mb["data"], mb["rsrc"], tree.file_finder_info(mb["type"], mb["creator"], flags),
                    mb["created"], mb["modified"])
        print(f"{name!r}: {tree.code(mb['type'])}/{tree.code(mb['creator'])} flags=0x{flags:04x} "
              f"data={len(mb['data'])} rsrc={len(mb['rsrc'])}")
    writer.finish()
    problems = tree.Tree(a.out).verify()
    if problems:
        for p in problems:
            print("verify:", p, file=sys.stderr)
        return 1
    print(f"-> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
