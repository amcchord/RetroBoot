"""Command line: python3 -m rbapps {identify,expand,verify,ls} ..."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import expand, tree, volume


def _ls(args) -> int:
    with open(args.image, "rb") as f:
        size = Path(args.image).stat().st_size
        for p in volume.partitions(f, size):
            print(f"# partition {p.index} {p.type} {p.name!r} offset={p.offset} length={p.length}")
        vol = volume.open_volume(f, size, args.volume)
        print(f"# {vol.kind} volume {vol.name!r} block={vol.block_size} blocks={vol.total_blocks} free={vol.free_blocks}")
        for e in vol.entries():
            name = ":".join(e.path)
            if e.kind == "folder":
                print(f"D {'':9} {'':9} {name}:")
            else:
                tc = (e.type + e.creator).decode("mac_roman")
                print(f"F {tc:9} {e.data_size:>9} {e.rsrc_size:>9} flags={e.flags:04x} {name}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="rbapps", description="Classic Mac media -> RetroBoot canonical tree")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("identify", help="classify a media file (archive, disk image, CD image)")
    i.add_argument("file", type=Path)
    e = sub.add_parser("expand", help="archive or disk image -> canonical tree")
    e.add_argument("source", type=Path)
    e.add_argument("--out", type=Path, required=True)
    e.add_argument("--keep-images", action="store_true", help="keep disk images inside archives as files")
    v = sub.add_parser("verify", help="re-hash a canonical tree against its manifest")
    v.add_argument("tree", type=Path)
    s = sub.add_parser("ls", help="list an HFS/HFS+ volume, floppy or CD image")
    s.add_argument("image", type=Path)
    s.add_argument("--volume", type=int, default=0)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "identify":
            print(json.dumps(expand.identify(args.file), indent=1))
        elif args.cmd == "expand":
            m = expand.expand(args.source, args.out, images="keep" if args.keep_images else "extract")
            print(json.dumps({"tree": str(args.out), "source": m["source"], "summary": m["summary"],
                              "top": m["top"], "skipped": m["skipped"]}, indent=1, ensure_ascii=False))
        elif args.cmd == "verify":
            t = tree.Tree(args.tree)
            problems = t.verify()
            print(json.dumps({"tree": str(args.tree), "entries": len(t.entries), "ok": not problems,
                              "problems": problems[:50]}, indent=1, ensure_ascii=False))
            return 0 if not problems else 1
        else:
            return _ls(args)
    except (expand.ExpandError, tree.TreeError, volume.VolumeError, OSError) as exc:
        print(f"rbapps: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
