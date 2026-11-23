"""rbplatinum command line.

    python3 -m rbplatinum build --source DISK.img|FOLDER --out DIR [--force] [--wordmark-kit DIR]
    python3 -m rbplatinum builtin [--out DIR] [--web-css FILE]
    python3 -m rbplatinum startup --source DISK.img --out FILE.rbis [--wordmark-kit DIR] [--previews DIR]
    python3 -m rbplatinum list  --source DISK.img PATH          # resources in a file
    python3 -m rbplatinum extract --source DISK.img PATH TYPE ID OUT   # raw resource data
"""
import argparse
import json
import sys
from pathlib import Path

from . import builtin


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="rbplatinum", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--source", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--force", action="store_true")
    b.add_argument("--wordmark-kit", help="private folder with kit.json (startup.py); default $RB_STARTUP_WORDMARK_KIT")
    bi = sub.add_parser("builtin", help="build RetroBoot chrome without a Mac OS disk")
    bi.add_argument("--out", type=Path, help="local panel asset directory")
    bi.add_argument("--web-css", type=Path, help="web stylesheet")
    st = sub.add_parser("startup", help="only the installer's startup box (installer-startup.rbis)")
    st.add_argument("--source", required=True)
    st.add_argument("--out", required=True)
    st.add_argument("--wordmark-kit")
    st.add_argument("--previews", help="write welcome.png and starting.png here")
    li = sub.add_parser("list")
    li.add_argument("--source", required=True)
    li.add_argument("path")
    ex = sub.add_parser("extract")
    ex.add_argument("--source", required=True)
    ex.add_argument("path")
    ex.add_argument("type")
    ex.add_argument("id", type=int)
    ex.add_argument("out")
    a = ap.parse_args(argv)
    if a.cmd == "builtin":
        if not (a.out or a.web_css):
            bi.error("specify --out, --web-css, or both")
        builtin.build(a.out, a.web_css)
        return 0
    if a.cmd == "startup":
        return startup_only(a)
    if a.cmd == "build":
        from .bundle import build

        m = build(a.source, a.out, force=a.force, wordmark_kit=a.wordmark_kit)
        print(json.dumps({"fonts": sorted(m["fonts"]), "icons": sorted(m["icons"]), "chrome": len(m["chrome"]),
                          "missing": m["missing"]}, indent=1))
        return 0
    from .macdisk import open_disk
    from .rsrc import ResourceFile

    disk = open_disk(a.source)
    rf = ResourceFile(disk.read_fork(a.path, "rsrc"))
    if a.cmd == "list":
        for (t, rid), r in sorted(rf.res.items()):
            print(f"{t!r:8} {rid:7d} {len(r.data):8d} {'compressed ' if r.compressed else ''}{r.name}")
        return 0
    r = rf.get(a.type, a.id)
    if r is None:
        print("no such resource", file=sys.stderr)
        return 1
    with open(a.out, "wb") as f:
        f.write(r.data)
    return 0


def startup_only(a) -> int:
    """The installer ISO's startup box (image/scripts/make-iso.sh)."""
    import os
    import tempfile
    from pathlib import Path

    from .bundle import Bundle
    kit = a.wordmark_kit or os.environ.get("RB_STARTUP_WORDMARK_KIT") or None
    with tempfile.TemporaryDirectory() as tmp:
        b = Bundle(a.source, Path(tmp), kit)
        try:
            pal = b.palette()
            b.fonts()
            b.startup(pal)
        finally:
            b.disk.close()
        rbis = Path(tmp) / "startup" / "installer-startup.rbis"
        if not rbis.exists():
            print(json.dumps({"startup": None, "missing": b.manifest["missing"]}))
            return 1
        out = Path(a.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp_out = out.with_name(out.name + ".tmp")
        tmp_out.write_bytes(rbis.read_bytes())
        tmp_out.replace(out)
        if a.previews:
            Path(a.previews).mkdir(parents=True, exist_ok=True)
            for name in ("welcome", "starting"):
                (Path(a.previews) / f"{name}.png").write_bytes((Path(tmp) / "startup" / f"{name}.png").read_bytes())
    print(json.dumps({"startup": str(out), "wordmark": b.manifest["startup"]["wordmark"] is not None}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
