#!/usr/bin/env python3
"""Finder lab: when does each Finder detector fire, against the frames? (hostd/reports/finderdet-2026-10-13.md)

Boots one prepared disk (an Appearance theme, a desktop picture, a window left open) in
place, ``--boots`` times, with hostd's own QEMU arguments behind the presenter stub (as
``powerkey_lab.py``).  Every ``--cadence`` s it takes the guest frame and saves each frame
that changed, from the Mac OS startup gray on, as RUN/bNN/fNNNNN-<ms>.png, and evaluates
the Finder detectors on it:

* ``main``: ``finder_menu_bar`` of the hostd tree given with ``--main-src`` (main's probe);
* ``branch``: ``finder.finder_state`` of this tree (the Platinum rule, then the menu-bar layout).

A boot ends 12 s after the later detection once the screen has been still for 2 s (or
``--timeout`` s after the gray); the power key and R restart Mac OS for the next boot (Return
shuts it down after the last).  ``finder_lab.py analyze RUN`` re-evaluates the saved frames
with this tree's detectors, so a detector change needs no new boots.

    finder_lab.py run --out RUN --disk THEME.img --boots 4 [--main-src DIR] [--card std|ati]
    finder_lab.py analyze RUN [RUN ...]

Lab tool for the Linux builder (root).  Frames stay in RUN (private: Apple's pixels).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent))
import powerkey_lab as pk  # noqa: E402  (sets up the repository paths)

from retroboot import finder as fdet  # noqa: E402
from retroboot.qmp import finder_menu_bar  # noqa: E402

stub = pk.stub


def load_main_detector(src: str | None):
    """main's finder_menu_bar from another hostd tree (the code before this change)."""
    if not src:
        return finder_menu_bar
    path = Path(src) / "retroboot" / "qmp.py"
    spec = importlib.util.spec_from_file_location("main_qmp", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["main_qmp"] = mod
    spec.loader.exec_module(mod)
    return mod.finder_menu_bar


class FinderLab(pk.Lab):
    def fresh_disk(self):
        # Boot the prepared disk in place (no copy: builder disk space), as RUN/machine/disk.img.
        if self.disk.is_symlink() or self.disk.exists():
            self.disk.unlink()
        self.disk.symlink_to(Path(self.a.seed).resolve())
        from rbguestprep import macloc
        self.cities = list(macloc.read_cities(str(self.disk))) or None

    def boot(self, boot: int, last: bool, main_det) -> dict:
        a = self.a
        t0 = self.start(boot)
        bdir = self.run / f"b{boot:02d}"
        bdir.mkdir(exist_ok=True)
        r = {"boot": boot, "load1": round(__import__("os").getloadavg()[0], 2)}
        writer_q: queue.Queue = queue.Queue()

        def writer():
            while True:
                item = writer_q.get()
                if item is None:
                    return
                path, fr = item
                pk.save(fr, path)
        wt = threading.Thread(target=writer, daemon=True)
        wt.start()
        gray = None
        first = {"main": None, "branch": None}
        prev_rgb = None
        still_since = None
        n = 0
        rows = []
        deadline = t0 + a.timeout
        while time.monotonic() < deadline and self.proc.poll() is None:
            now = time.monotonic()
            el = now - t0
            try:
                with self.p.lock:
                    cur_s = self.p.surface
                if not cur_s or cur_s["connection"] <= self.conn:
                    raise RuntimeError("no surface yet")
                fr = self.p.frame_raw()
                cur = pk.rgb_frame(self.p, fr)
            except RuntimeError:
                time.sleep(a.cadence)
                continue
            w, h, rgb = cur
            changed = prev_rgb is None or rgb != prev_rgb
            prev_rgb = rgb
            if gray is None:
                an = stub.analyze_frame(*fr)
                if an.get("gray_fraction", 0) >= 0.97:
                    gray = el
                    r["gray_s"] = round(el, 2)
            if changed:
                still_since = now
                if gray is not None:
                    n += 1
                    writer_q.put((bdir / f"f{n:05d}-{int(el * 1000):06d}.png", cur))
                    m = bool(main_det(w, rgb))
                    st = fdet.finder_state(w, h, rgb)
                    b = st["finder"]
                    rows.append({"n": n, "t": round(el, 3), "main": m, "branch": b, "why": st.get("why")})
                    for k, v in (("main", m), ("branch", b)):
                        if v and first[k] is None:
                            first[k] = round(el, 3)
            # done: 12 s after the later detection, screen still for 2 s
            fired = [v for v in first.values() if v is not None]
            if fired and el >= max(fired) + a.after and now - still_since >= 2.0 and \
                    (first["branch"] is not None or el >= gray + a.timeout_branch):
                break
            time.sleep(a.cadence)
        writer_q.put(None)
        wt.join()
        r.update({"main_s": first["main"], "branch_s": first["branch"], "frames": n,
                  "main_rel_gray": round(first["main"] - gray, 2) if first["main"] and gray else None,
                  "branch_rel_gray": round(first["branch"] - gray, 2) if first["branch"] and gray else None})
        (bdir / "frames.json").write_text(json.dumps(rows))
        # restart (or shut down after the last boot) through Mac OS's own dialog
        outcome = None
        for attempt in range(3):
            self.key("power", 150)
            time.sleep(3.0)
            self.key("ret" if last else "r")
            try:
                self.proc.wait(60)
                outcome = self.shutdown_reason()
                break
            except subprocess.TimeoutExpired:
                continue
        r["outcome"] = outcome or "no-exit"
        self.qe.close()
        self.qc.close()
        self.kill()
        self.res.write(json.dumps(r) + "\n")
        print(json.dumps(r), flush=True)
        return r


def run(a):
    main_det = load_main_detector(a.main_src)
    lab = FinderLab(a)
    lab.fresh_disk()
    try:
        for i in range(a.boots):
            r = lab.boot(i, i == a.boots - 1, main_det)
            if r["outcome"] not in ("guest-reset", "guest-shutdown"):
                print("unclean stop: the disk needs a check; stopping", flush=True)
                break
    finally:
        lab.kill()
        try:
            lab.disk.unlink()
        except FileNotFoundError:
            pass


def analyze(paths: list[str]):
    """Re-evaluate the saved frames with this tree's finder_state: first firing per boot."""
    out = []
    for p in paths:
        for bdir in sorted(Path(p).glob("b[0-9][0-9]")):
            rows = json.loads((bdir / "frames.json").read_text()) if (bdir / "frames.json").exists() else []
            first = None
            for f in sorted(bdir.glob("f*.png")):
                n, ms = f.stem[1:].split("-")
                w, h, rgb = read_png(f)
                st = fdet.finder_state(w, h, rgb)
                if st["finder"]:
                    first = {"frame": f.name, "t": int(ms) / 1000, "why": st.get("why")}
                    break
            main = next((x for x in rows if x["main"]), None)
            out.append({"run": Path(p).name, "boot": bdir.name, "branch": first,
                        "main": {"frame": f"f{main['n']:05d}", "t": main["t"]} if main else None})
            print(json.dumps(out[-1]), flush=True)
    return out


def read_png(path: Path) -> tuple[int, int, bytes]:
    """8-bit RGB PNGs as stub.png_bytes writes them (filter 0 rows)."""
    import struct
    import zlib
    data = path.read_bytes()
    pos = 8
    w = h = 0
    idat = b""
    while pos < len(data):
        ln = struct.unpack(">I", data[pos:pos + 4])[0]
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + ln]
        if kind == b"IHDR":
            w, h = struct.unpack(">II", body[:8])
        elif kind == b"IDAT":
            idat += body
        pos += 12 + ln
    raw = zlib.decompress(idat)
    row = w * 3
    rgb = b"".join(raw[y * (row + 1) + 1:(y + 1) * (row + 1)] for y in range(h))
    return w, h, rgb


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="mode", required=True)
    r = sub.add_parser("run")
    r.add_argument("--out", required=True)
    r.add_argument("--disk", required=True, dest="seed")
    r.add_argument("--boots", type=int, default=4)
    r.add_argument("--main-src")
    r.add_argument("--card", choices=("std", "ati"), default="std")
    r.add_argument("--pmu", action="store_true")
    r.add_argument("--size", type=pk.parse_size, default=(1024, 768))
    r.add_argument("--pkg", default="/srv/retroboot/wave2/artifacts/qemu/linux-ppc-r17")
    r.add_argument("--ndrv", default="/srv/retroboot/wave2/artifacts/ndrv-r17")
    r.add_argument("--loader", default="/srv/retroboot/wave2/artifacts/classicvirtio-4")
    r.add_argument("--cadence", type=float, default=0.15)
    r.add_argument("--after", type=float, default=12.0)
    r.add_argument("--timeout", type=float, default=150.0)
    r.add_argument("--timeout-branch", type=float, default=60.0,
                   help="with only main's detection, wait this long after the gray for the branch's")
    an = sub.add_parser("analyze")
    an.add_argument("runs", nargs="+")
    a = ap.parse_args()
    if a.mode == "analyze":
        analyze(a.runs)
        return
    import signal

    def bye(*_):
        sys.exit(2)
    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)
    run(a)


if __name__ == "__main__":
    main()
