#!/usr/bin/env python3
"""Lab driver for the shared folder through real hostd's API, on a session that
share_real.py started (``share_real.py up``): what the web Files page does, timed
against the Finder window that shows the folder (emulator/reports/sharefix-20261012.md).

    share_refresh.py pair   --work W                     pair this tool (cookie jar + CSRF in W)
    share_refresh.py drop   --work W --name N [--path P] [--file F | --text T] [--mtime MS]
                            [--region X0,Y0,X1,Y1] [--timeout 30]
        upload one file (POST /api/v1/share/upload, as the Files page does) and,
        with --region, the seconds until that part of the guest screen changes
    share_refresh.py get    --work W --path P --out F [--format auto|macbinary|data]
    share_refresh.py list   --work W [--path P]
    share_refresh.py api    --work W METHOD PATH [JSON]

Run as root on the lab builder, next to share_real.py.  Python standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
import time
import urllib.parse
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import share_real  # noqa: E402


def png_rgb(path: Path) -> tuple[int, int, int, bytes]:
    """Width, height, bytes per pixel and unfiltered rows of an 8-bit RGB/RGBA PNG."""
    data = path.read_bytes()
    pos, idat, w = 8, b"", 0
    while pos < len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        if kind == b"IHDR":
            w, h, depth, ctype = struct.unpack(">IIBB", body[:10])
            bpp = {2: 3, 6: 4}[ctype]
        elif kind == b"IDAT":
            idat += body
        pos += 12 + n
    raw = zlib.decompress(idat)
    stride = w * bpp
    out = bytearray()
    prev = bytearray(stride)
    i = 0
    for _ in range(h):
        f = raw[i]
        line = bytearray(raw[i + 1:i + 1 + stride])
        i += 1 + stride
        for x in (range(stride) if f else ()):
            a = line[x - bpp] if x >= bpp else 0
            b = prev[x]
            c = prev[x - bpp] if x >= bpp else 0
            if f == 1:
                line[x] = (line[x] + a) & 255
            elif f == 2:
                line[x] = (line[x] + b) & 255
            elif f == 3:
                line[x] = (line[x] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        out += line
        prev = line
    return w, h, bpp, bytes(out)


def region_sig(r, region, n: int) -> str:
    path = Path(r.guest_png(f".probe-{n % 2}"))
    w, h, bpp, px = png_rgb(path)
    x0, y0, x1, y1 = region
    m = hashlib.md5()
    for y in range(y0, min(y1, h)):
        m.update(px[(y * w + x0) * bpp:(y * w + x1) * bpp])
    return m.hexdigest()


def session(work: Path):
    r = share_real.run_for(work)
    s = work / "session.json"
    if s.exists():
        r.csrf = json.loads(s.read_text()).get("csrf")
    return r


def pairing_code(r) -> str:
    """The code the local panel shows (Recovery > Network); the panel is closed again."""
    import re
    r.inject({"event": "chord", "name": "recovery"})
    r.wait_doc("recovery-main")
    r.inject({"click": "network"})
    m = r.wait(lambda: re.search(r'"text": "(\d) (\d) (\d) (\d) (\d) (\d)"', json.dumps(r.doc())), 20,
               "pairing code")
    r.inject({"click": "cancel"})
    r.wait_doc("recovery-main")
    r.inject({"click": "cancel"})
    return "".join(m.groups())


def pair(a):
    work = Path(a.work).resolve()
    r = share_real.run_for(work)
    code = pairing_code(r)
    c, pr = r.curl("POST", "/api/v1/pair", {"code": code, "device_name": "sharefix lab"}, auth=False)
    if c != 200 or not isinstance(pr, dict):
        raise SystemExit(f"pairing failed: {c} {pr}")
    (work / "session.json").write_text(json.dumps({"csrf": pr.get("csrf")}))
    print(json.dumps({"paired": True}))


def drop(a):
    work = Path(a.work).resolve()
    r = session(work)
    src = Path(a.file) if a.file else work / ".drop-body"
    if not a.file:
        src.write_text(a.text if a.text is not None else f"RetroBoot drop {time.time()}\n")
    region = tuple(int(v) for v in a.region.split(",")) if a.region else None
    before = region_sig(r, region, 0) if region else None
    params = {"path": a.path, "name": a.name, "conflict": a.conflict}
    if a.mtime is not None:
        params["mtime"] = a.mtime          # the browser's File.lastModified, milliseconds (UTC)
    q = urllib.parse.urlencode(params)
    t0 = time.monotonic()
    c, data = r.curl("POST", f"/api/v1/share/upload?{q}",
                     extra=["-H", "Content-Type: application/octet-stream", "--data-binary", f"@{src}"])
    t_up = time.monotonic()
    res = {"status": c, "upload_s": round(t_up - t0, 2),
           "items": data.get("items") if isinstance(data, dict) else data, "refresh_s": None}
    if region and c in (200, 201):
        n = 1
        while time.monotonic() - t_up < a.timeout:
            if region_sig(r, region, n) != before:
                res["refresh_s"] = round(time.monotonic() - t_up, 2)
                break
            n += 1
            time.sleep(a.poll)
    print(json.dumps(res))


def get(a):
    r = session(Path(a.work).resolve())
    q = urllib.parse.urlencode({"path": a.path, "format": a.format})
    c, _ = r.curl("GET", f"/api/v1/share/download?{q}", out=Path(a.out))
    print(json.dumps({"status": c, "out": a.out, "bytes": Path(a.out).stat().st_size if c == 200 else None}))


def listing(a):
    r = session(Path(a.work).resolve())
    c, data = r.curl("GET", "/api/v1/share/list?" + urllib.parse.urlencode({"path": a.path}))
    print(json.dumps(data, indent=1) if c == 200 else json.dumps({"status": c, "body": data}))


def api(a):
    r = session(Path(a.work).resolve())
    c, data = r.curl(a.method, a.apipath, json.loads(a.body) if a.body else None)
    print(json.dumps({"status": c, "body": data}))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("pair", pair), ("drop", drop), ("get", get), ("list", listing), ("api", api)):
        s = sub.add_parser(name)
        s.add_argument("--work", required=True)
        s.set_defaults(fn=fn)
        if name == "drop":
            s.add_argument("--name", required=True)
            s.add_argument("--path", default="")
            s.add_argument("--file")
            s.add_argument("--text")
            s.add_argument("--conflict", default="rename")
            s.add_argument("--mtime", type=int, help="File.lastModified in ms, as the Files page sends it")
            s.add_argument("--region")
            s.add_argument("--timeout", type=float, default=30)
            s.add_argument("--poll", type=float, default=0.25)
        elif name == "get":
            s.add_argument("--path", required=True)
            s.add_argument("--out", required=True)
            s.add_argument("--format", default="auto")
        elif name == "list":
            s.add_argument("--path", default="")
        elif name == "api":
            s.add_argument("method")
            s.add_argument("apipath")
            s.add_argument("body", nargs="?")
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
