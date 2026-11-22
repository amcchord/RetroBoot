#!/usr/bin/env python3
"""Card-switch lab: where Mac OS 9's Control Strip ends up across Mac screen changes.

Starts a real Mac OS disk again and again with hostd's QEMU arguments
(apps/lab/rbsession.py: build_argv, std-VGA or the ATI Radeon, a display mode
and pixel size), and before each start, with ``--fix``, runs hostd's own
launch hook for the Control Strip (``Graphics.start_screen`` and
``GuestPrep.control_strip``, i.e. rbguestprep.controlstrip.fit on the stopped
disk), as hostd's ``supervisor.prepare_disk`` does.  At the Finder it saves
the screen, finds the strip on it and can move it, change the screen size
live (the presenter's RESIZE after a pixel-size change), then shuts Mac OS
down with the power key and reads the saved Control Strip Preferences.

    cardswitch_lab.py run WORK --disk BASE [--fix] --display 1080x720 --steps STEPS
        [--pkg DIR --ndrv DIR --loader DIR]
    cardswitch_lab.py find PNG...          # the strip on saved screens (also real_qemu.py's)

STEPS is a comma-separated list, one Mac OS start each:
``std`` or ``ati`` (the card), optionally ``@N`` for the pixel size (``std@2``),
and ``+right`` (Option-drag the strip to the right edge, then collapse it) or
``+live=WxH/WxH`` (live resizes after the Finder).  BASE is copied sparsely to
WORK/disk.img and never written.  Lab tool (builder, root; guest screenshots
stay in WORK, never in Git).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import struct
import subprocess
import sys
import time
import zlib
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve()
REPO = HERE.parents[3]
sys.path.insert(0, str(REPO / "hostd"))
sys.path.insert(0, str(REPO / "guestprep"))
sys.path.insert(0, str(REPO / "apps"))

A = "/srv/retroboot/wave2/artifacts"
DEFAULTS = {"pkg": f"{A}/qemu/linux-ppc-r18", "ndrv": f"{A}/ndrv-r18", "loader": f"{A}/classicvirtio-4"}
MENU_BAR = 20


# -- the strip on a screen ----------------------------------------------------------
def read_png(path) -> tuple[int, int, bytes]:
    """(width, height, rgb) of an 8-bit RGB or RGBA PNG (QMP screendumps, the fake presenter)."""
    data = Path(path).read_bytes()
    pos, idat, w = 8, b"", None
    while pos < len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        if kind == b"IHDR":
            w, h, depth, ctype = struct.unpack(">IIBB", body[:10])
            if depth != 8 or ctype not in (2, 6):
                raise ValueError("unsupported PNG")
            bpp = 3 if ctype == 2 else 4
        elif kind == b"IDAT":
            idat += body
        pos += 12 + n
    raw = zlib.decompress(idat)
    stride = w * bpp
    out, prev = bytearray(), bytearray(stride)
    for y in range(h):
        f, line = raw[y * (stride + 1)], bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if f == 1:
                line[i] = (line[i] + a) & 255
            elif f == 2:
                line[i] = (line[i] + b) & 255
            elif f == 3:
                line[i] = (line[i] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        out += line if bpp == 3 else b"".join(bytes(line[i:i + 3]) for i in range(0, stride, 4))
        prev = line
    return w, h, bytes(out)


def find_strip(frame) -> dict | None:
    """The Control Strip on a Platinum desktop: its dark frame on the left or right screen
    edge, 18-30 rows tall below the menu bar.  {side, top, bottom, from_bottom, x0, x1}."""
    w, h, rgb = frame

    def dark(x, y):
        o = (y * w + x) * 3
        return rgb[o] < 60 and rgb[o + 1] < 60 and rgb[o + 2] < 60

    for side, xs in (("left", (0, 1, 2)), ("right", (w - 1, w - 2, w - 3))):
        for x in xs:
            y = MENU_BAR + 2
            while y < h:
                if dark(x, y):
                    y0 = y
                    while y < h and dark(x, y):
                        y += 1
                    if 18 <= y - y0 <= 30:
                        y1 = y - 1
                        ext, step = x, (1 if side == "left" else -1)
                        while True:               # along the top edge, over 1-pixel gaps
                            nxt = next((ext + k * step for k in (1, 2)
                                        if 0 <= ext + k * step < w and dark(ext + k * step, y0)), None)
                            if nxt is None:
                                break
                            ext = nxt
                        x0, x1 = (0, ext) if side == "left" else (ext, w - 1)
                        return {"side": side, "top": y0, "bottom": y1, "from_bottom": h - 1 - y1,
                                "x0": x0, "x1": x1, "screen": [w, h]}
                else:
                    y += 1
    return None


# -- one Mac OS start --------------------------------------------------------------
class Lab:
    def __init__(self, a):
        self.a = a
        self.work = Path(a.work).resolve()
        self.disk = self.work / "disk.img"
        self.results = {"display": a.display, "fix": a.fix, "steps": [], "pkg": a.pkg, "ndrv": a.ndrv}

    def log(self, **kw):
        print(json.dumps(kw), flush=True)

    def guest(self, run):
        from rbapps import guestio
        deadline = time.monotonic() + 90
        while True:
            try:
                return guestio.Guest(json.loads((run / "session.json").read_text())["qmp_lab"])
            except Exception:
                if time.monotonic() > deadline:
                    raise
                time.sleep(1)

    def settle(self, g, extra=8.0):
        g.wait_still(3.0, timeout=90)
        time.sleep(extra)
        return g.wait_still(3.0, timeout=90)

    def shot(self, g, name):
        img = self.settle(g, 4.0)
        img.save(self.work / f"{name}.png")
        strip = find_strip((img.width, img.height, img.rgb))
        self.log(shot=name, size=[img.width, img.height], strip=strip)
        return {"name": name, "size": [img.width, img.height], "strip": strip}

    def hook(self, card: str, scale: str) -> dict:
        """hostd's prepare_disk Control Strip hook, with hostd's own code."""
        from retroboot import graphics
        from retroboot.guestprep import GuestPrep
        w, h = (int(v) for v in self.a.display.split("x"))
        settings = {"display.graphics": card, "display.pixel_scale": scale, "display.guest_size": "auto"}
        app = SimpleNamespace(presenter=SimpleNamespace(display={"mode": [w, h, 60]}),
                              settings=SimpleNamespace(applied=settings, desired=settings),
                              paths=SimpleNamespace(qemu_package=Path(self.a.pkg)))   # ati-edid-modes
        size = graphics.Graphics(app).start_screen(card)
        res = asyncio.run(GuestPrep(REPO / "guestprep").control_strip(self.disk, size))
        return {"screen": list(size), **res}

    def prefs(self) -> dict:
        from rbguestprep import controlstrip
        return controlstrip.state(str(self.disk))

    def step(self, i: int, spec: str) -> dict:
        card_part, *actions = spec.split("+")
        card, _, scale = card_part.partition("@")
        card = {"std": "standard", "ati": "ati-radeon"}[card]
        scale = scale or "auto"
        name = f"{i:02d}-{card_part.replace('@', 'x')}"
        out = {"step": spec, "card": card, "pixel_scale": scale, "prefs_before": self.prefs()}
        if self.a.fix:
            out["hook"] = self.hook(card, scale)
        run = self.work / name
        run.mkdir(exist_ok=True)
        argv = ["python3", str(REPO / "apps/lab/rbsession.py"), "run", str(run), "--disk", str(self.disk),
                "--size", self.a.display, "--pixel-scale", scale, "--graphics", card, "--pkg", self.a.pkg,
                "--ndrv", self.a.ndrv, "--loader", self.a.loader, "--audio", "none", "--no-share",
                "--nic", "none", "--rtc", "2026-10-13T09:00:00"]
        env = dict(os.environ, RB_LAB_PKG=self.a.pkg, RB_LAB_NDRV=self.a.ndrv)
        proc = subprocess.Popen(argv, stdout=open(run / "rbsession.log", "w"), stderr=subprocess.STDOUT,
                                start_new_session=True, env=env)
        try:
            time.sleep(3)
            g = self.guest(run)
            t0 = time.monotonic()
            if g.wait_menu_bar(400) is None:
                raise RuntimeError(f"{name}: no Finder menu bar")
            out["finder_s"] = round(time.monotonic() - t0, 1)
            out["finder"] = self.shot(g, f"{name}-finder")
            for act in actions:
                if act == "right":
                    s = out["finder"]["strip"]
                    w, h = out["finder"]["size"]
                    tab = (s["x1"] - 3, (s["top"] + s["bottom"]) // 2) if s and s["side"] == "left" else None
                    if tab:
                        g.key("option", True)
                        time.sleep(0.2)
                        try:
                            g.drag(tab[0], tab[1], w - 5, h // 2, steps=24, step_delay=0.06)
                        finally:
                            time.sleep(0.2)
                            g.key("option", False)
                        for k in range(3):                      # the pointer off the strip's edge (the
                            time.sleep(0.5)                     # first move after a strip drag can be lost)
                            g.move(w // 2 + 10 * k, h // 3)
                        moved = self.shot(g, f"{name}-right")
                        ms = moved["strip"]
                        if ms and ms["side"] == "right":
                            g.click(ms["x0"] + 2, (ms["top"] + ms["bottom"]) // 2)     # the tab: collapse
                            for k in range(3):
                                time.sleep(0.5)
                                g.move(w // 2 + 10 * k, h // 3)
                        out["moved"] = moved
                        out["collapsed"] = self.shot(g, f"{name}-collapsed")
                elif act.startswith("live="):
                    out.setdefault("live", [])
                    for size in act[5:].split("/"):
                        lw, lh = (int(v) for v in size.split("x"))
                        (run / "resize.req.tmp").write_text(f"{lw} {lh}")
                        os.replace(run / "resize.req.tmp", run / "resize.req")
                        time.sleep(6)
                        out["live"].append(self.shot(g, f"{name}-live-{lw}x{lh}"))
            out["shutdown"] = self.shutdown(g, proc, run)
            try:
                g.close()
            except OSError:                             # QEMU has exited
                pass
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, 15)
                proc.wait(20)
        out["prefs_after"] = self.prefs()
        self.results["steps"].append(out)
        (self.work / "results.json").write_text(json.dumps(self.results, indent=1))
        return out

    def shutdown(self, g, proc, run) -> dict:
        for attempt in range(3):
            g.wait_still(2.0, timeout=30)
            g.tap("power", hold=0.15)
            time.sleep(4)
            g.tap("ret")
            try:
                proc.wait(90)
                ev = [json.loads(x) for x in (run / "events.jsonl").read_text().splitlines()]
                ex = [e for e in ev if e.get("kind") == "guest_exit"]
                return {"attempt": attempt, "reason": ex[-1].get("reason") if ex else None}
            except subprocess.TimeoutExpired:
                continue
        raise RuntimeError("Mac OS did not shut down")

    def run(self) -> int:
        self.work.mkdir(parents=True, exist_ok=False)
        subprocess.run(["cp", "--sparse=always", self.a.disk, str(self.disk)], check=True)
        try:
            for i, spec in enumerate(self.a.steps.split(",")):
                s = self.step(i, spec.strip())
                self.log(step=s["step"], hook=s.get("hook"), finder=(s["finder"] or {}).get("strip"))
        finally:
            if not self.a.keep_disk:
                self.disk.unlink(missing_ok=True)
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("work")
    r.add_argument("--disk", required=True)
    r.add_argument("--display", default="1080x720")
    r.add_argument("--steps", required=True)
    r.add_argument("--fix", action="store_true", help="run hostd's Control Strip hook before each start")
    r.add_argument("--keep-disk", action="store_true")
    for k, v in DEFAULTS.items():
        r.add_argument(f"--{k}", default=v)
    f = sub.add_parser("find")
    f.add_argument("png", nargs="+")
    a = ap.parse_args()
    if a.cmd == "find":
        for p in a.png:
            print(json.dumps({"file": p, "strip": find_strip(read_png(p))}))
        return 0
    return Lab(a).run()


if __name__ == "__main__":
    sys.exit(main())
