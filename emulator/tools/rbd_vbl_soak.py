#!/usr/bin/env python3
"""Pointer cadence and responsiveness soak for one apps/lab/rbsession.py run (lab, root).

    rbd_vbl_soak.py RUN --minutes 30 [--every 30] [--motion 3] [--at 480,300]

Waits for the Finder's menu bar through RUN's lab QMP socket, then every
``--every`` seconds parks the pointer at ``--at`` (tablet), circles it with
relative motion at 125 Hz for ``--motion`` seconds (the USB mouse, as a real
mouse on the presenter reaches Mac OS) and samples the standard card's VRAM
around that point in QEMU's memory at ~300 Hz.  Mac OS redraws its software
cursor at the video driver's VBL, so changed frames per second during the
motion is the guest's VBL rate while input keeps up.

A cycle in which the screen does not change at all while the pointer moves is
a hang: the soak records ``info registers`` five times a second apart, a
screendump, and stops (the guest is left running for inspection).  One JSON
line per cycle goes to stdout and RUN/soak.jsonl.  Needs the guest at
960x540x32 (``--size 1920x1080 --pixel-scale 2``) on the standard card.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[2] / "apps"))
from rbapps import guestio  # noqa: E402

W, H, BPP = 960, 540, 4
BOX = 64                     # rows sampled above and below the parking point


def vram_addr(pid: int) -> int:
    """The 64 MiB anonymous mapping (VGA.vgamem_mb=64): the card's VRAM."""
    for line in open(f"/proc/{pid}/maps"):
        f = line.split()
        a, b = (int(v, 16) for v in f[0].split("-"))
        if b - a == 64 << 20 and f[1] == "rw-p" and len(f) == 5:
            return a
    raise RuntimeError("no VRAM mapping")


def circle(g: guestio.Guest, seconds: float, stop: threading.Event):
    period, steps, r = 1 / 125, 125, 24.0
    x = y = 0.0
    t0 = time.monotonic()
    for i in range(int(seconds * 125)):
        if stop.is_set():
            break
        a = 2 * math.pi * i / steps
        nx, ny = r * math.cos(a), r * math.sin(a)
        dx, dy = round(nx - x), round(ny - y)
        x, y = x + dx, y + dy
        g.qmp.cmd("input-send-event", events=[{"type": "rel", "data": {"axis": "x", "value": dx}},
                                              {"type": "rel", "data": {"axis": "y", "value": dy}}])
        d = t0 + (i + 1) * period - time.monotonic()
        if d > 0:
            time.sleep(d)


def cycle(g, pid, at, seconds):
    vram = vram_addr(pid)
    y0 = max(0, at[1] - BOX)
    n = (min(H, at[1] + BOX) - y0) * W * BPP
    mem = os.open(f"/proc/{pid}/mem", os.O_RDONLY)
    changes = []
    stop = threading.Event()

    def sample():
        prev = os.pread(mem, n, vram + y0 * W * BPP)
        while not stop.is_set():
            cur = os.pread(mem, n, vram + y0 * W * BPP)
            if cur != prev:
                changes.append(time.monotonic())
                prev = cur
            time.sleep(0.003)

    g.move(*at)
    time.sleep(0.3)
    th = threading.Thread(target=sample)
    th.start()
    time.sleep(0.1)
    t_start = time.monotonic()
    try:
        circle(g, seconds, stop)
    finally:
        t_end = time.monotonic()
        stop.set()
        th.join()
        os.close(mem)
    frames = []
    for t in changes:
        if t_start <= t <= t_end + 0.05 and (not frames or t - frames[-1] > 0.005):
            frames.append(t)
    gaps = sorted(round((b - a) * 1000, 1) for a, b in zip(frames, frames[1:]))
    hist = {}
    for v in gaps:
        k = int(round(v / 4) * 4)
        hist[k] = hist.get(k, 0) + 1
    dur = t_end - t_start
    return {"frames": len(frames), "fps": round(len(frames) / dur, 1) if dur else None,
            "gap_median_ms": gaps[len(gaps) // 2] if gaps else None,
            "gap_max_ms": gaps[-1] if gaps else None,
            "gaps_16": sum(1 for v in gaps if v < 24), "gaps_long": sum(1 for v in gaps if v >= 24),
            "hist": hist}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run")
    ap.add_argument("--minutes", type=float, default=30)
    ap.add_argument("--every", type=float, default=30)
    ap.add_argument("--motion", type=float, default=3)
    ap.add_argument("--at", default="480,300")
    a = ap.parse_args()
    run = Path(a.run).resolve()
    at = tuple(int(v) for v in a.at.split(","))
    out = (run / "soak.jsonl").open("a", buffering=1)

    def log(**kw):
        kw = {"t": round(time.time(), 3), **kw}
        line = json.dumps(kw)
        out.write(line + "\n")
        print(line, flush=True)

    deadline = time.monotonic() + 120
    while True:
        try:
            sess = json.loads((run / "session.json").read_text())
            g = guestio.Guest(sess["qmp_lab"])
            break
        except (OSError, ValueError, KeyError, guestio.QMPError):
            if time.monotonic() > deadline:
                raise
            time.sleep(1)
    t_boot = sess["started"]
    if g.wait_menu_bar(240) is None:
        log(kind="no_menu_bar", error=getattr(g, "last_error", None))
        return 2
    log(kind="menu_bar", after_s=round(time.time() - t_boot, 1))
    time.sleep(20)                              # let startup items settle
    end = time.monotonic() + a.minutes * 60
    n = 0
    while time.monotonic() < end:
        t_next = time.monotonic() + a.every
        n += 1
        r = cycle(g, sess["pid"], at, a.motion)
        log(kind="cycle", n=n, uptime_s=round(time.time() - t_boot), **r)
        if r["frames"] == 0:
            regs = []
            for _ in range(5):
                try:
                    regs.append(g.qmp.cmd("human-monitor-command", **{"command-line": "info registers"}))
                except guestio.QMPError as exc:
                    regs.append(f"error: {exc}")
                time.sleep(1)
            (run / "hang-registers.txt").write_text("\n\n".join(regs))
            try:
                g.screenshot().save(run / "hang.png")
            except Exception as exc:  # noqa: BLE001 - evidence only
                log(kind="hang_screenshot_failed", error=str(exc)[:200])
            log(kind="hang", n=n, uptime_s=round(time.time() - t_boot),
                nip=[next((ln.split()[1] for ln in r_.splitlines() if ln.startswith("NIP")), None)
                     for r_ in regs])
            return 1
        time.sleep(max(0.0, t_next - time.monotonic()))
    log(kind="done", cycles=n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
