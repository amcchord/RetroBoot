#!/usr/bin/env python3
"""Cold boot, idle CPU and key-to-screen latency of a 68k Mac, with or without
RetroBoot Idle (lab tool).

    measure.py OUTDIR --disk DISK --pram PRAM --keys b,t [--runs 3] [--idle-seconds 30]
               [--fb mac|qfb] [--nic none|user] [--ram 128]

Each run starts QEMU on a snapshot of DISK (nothing is written back) and a
copy of PRAM through guest/lab/q800.py, then measures:

* boot: seconds from QEMU's start to the first frame with a Mac OS menu bar
  (screendump polled about every 0.1 s), and to the last screen change of the
  boot (the settled desktop);
* idle CPU: the QEMU process's CPU time over --idle-seconds, 10 s after the
  desktop settled, as % of one host core;
* key latency: for each of 10 keys (alternating --keys, Finder type-to-select
  between two desktop icons) the time from QMP send-key to the first changed
  screendump;
* RetroBoot Idle's own counters (rbidle_check.py) when it is installed.

Results: OUTDIR/runs.json and a one-line summary per run.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve()
REPO = HERE.parents[3]
sys.path.insert(0, str(REPO / "apps"))
sys.path.insert(0, str(REPO / "guest" / "lab"))
sys.path.insert(0, str(HERE.parent))
from rbapps import guestio  # noqa: E402
import q800  # noqa: E402
import rbidle_check  # noqa: E402


def one_run(out: Path, i: int, a) -> dict:
    run = out / f"run{i}"
    if run.exists():
        shutil.rmtree(run)
    run.mkdir(parents=True)
    pram = run / "pram.img"
    shutil.copyfile(a.pram, pram)
    argv = ["start", str(run), "--disk", str(a.disk), "--pram", str(pram), "--snapshot", "--fb", a.fb,
            "--nic", a.nic, "--ram", str(a.ram)]
    q800.main(argv)
    info = q800.session(run)
    g = guestio.Guest(str(run / "qmp.sock"))
    res: dict = {"run": i}
    try:
        t_menu = t_last = None
        last = None
        deadline = info["start_time"] + 90
        while time.time() < deadline:
            img = g.screenshot()
            t = time.time() - info["start_time"]
            sig = img.signature(step=3)
            if sig != last:
                last, t_last = sig, t
                if t_menu is None and guestio.menu_bar_ready(img):
                    t_menu = t
            if t_menu is not None and t - t_last > 4:
                break
            time.sleep(0.1)
        res["boot_menubar_s"] = round(t_menu, 2) if t_menu else None
        res["boot_settled_s"] = round(t_last, 2) if t_last else None
        time.sleep(10)
        c0, w0 = q800.cpu_seconds(info["pid"]), time.monotonic()
        time.sleep(a.idle_seconds)
        c1, w1 = q800.cpu_seconds(info["pid"]), time.monotonic()
        res["idle_cpu_pct"] = round((c1 - c0) / (w1 - w0) * 100, 1)
        lat = []
        keys = a.keys.split(",")
        for k in range(10):
            time.sleep(1.2)
            base = g.screenshot().signature(step=3)
            t0 = time.monotonic()
            g.qmp.cmd("send-key", keys=[{"type": "qcode", "data": keys[k % len(keys)]}])
            got = None
            while time.monotonic() - t0 < 2.0:
                if g.screenshot().signature(step=3) != base:
                    got = round((time.monotonic() - t0) * 1000)
                    break
            lat.append(got)
        res["key_latency_ms"] = lat
        g.screenshot().save(run / "final.png")
        path = run / "ram.bin"
        g.qmp.cmd("pmemsave", val=0, size=a.ram << 20, filename=str(path), timeout=120)
        chk = rbidle_check.check(path.read_bytes())
        path.unlink()
        res["rbidle"] = {k: chk.get(k) for k in ("active", "idle_calls", "stops", "min_run")}
        try:
            g.qmp.cmd("quit")
        except guestio.QMPError:
            pass
    finally:
        g.close()
    for _ in range(50):
        if not q800.alive(info["pid"]):
            break
        time.sleep(0.1)
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out", type=Path)
    ap.add_argument("--disk", required=True, type=Path)
    ap.add_argument("--pram", required=True, type=Path)
    ap.add_argument("--keys", default="m,t", help="two keys that select different desktop icons")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--idle-seconds", type=float, default=30)
    ap.add_argument("--fb", default="mac", choices=("mac", "qfb"))
    ap.add_argument("--nic", default="none", choices=("none", "user"))
    ap.add_argument("--ram", type=int, default=128)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    runs = []
    for i in range(a.runs):
        r = one_run(a.out, i, a)
        runs.append(r)
        lat = [x for x in r["key_latency_ms"] if x is not None]
        print(json.dumps({"run": i, "boot_menubar_s": r["boot_menubar_s"], "idle_cpu_pct": r["idle_cpu_pct"],
                          "key_ms_median": statistics.median(lat) if lat else None,
                          "key_ms": r["key_latency_ms"], "rbidle": r["rbidle"]}), flush=True)
    (a.out / "runs.json").write_text(json.dumps(runs, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
