#!/usr/bin/env python3
"""Restart and app-quit cycles on one apps/lab/rbsession.py run (lab; started with --relaunch).

    rbd_restart_soak.py RUN CYCLES [--no-audion] [--play SECONDS]

Each cycle, at the Finder: Start -> Audion, Play, Visualizer (Cmd-=), --play
seconds of the stream, Cmd-Q, a few seconds at the Finder, then the power key
and Restart (as hostd does: the dialog's R).  rbsession relaunches QEMU after
Mac OS's restart; the next cycle starts when the new session shows the
Finder's menu bar.  These busy transitions are where the ThinkPad X1 froze
with the 60 Hz video driver's fixed timer phase (emulator/reports/vbl-60hz-
2026-10-22.md).

A Restart that doesn't end the session within 90 s, a new session without a
menu bar within 180 s, or a screen that doesn't change across two pointer
circles is a hang: ``info registers`` x5 and a screendump go to RUN, and the
soak stops with the guest left running.  One JSON line per step goes to
stdout and RUN/restart-soak.jsonl.  Coordinates are for SuperDeluxe/Deluxe's
GoMac Start menu at 960x540.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[2] / "apps"))
from rbapps import guestio  # noqa: E402


class Soak:
    def __init__(self, run: Path):
        self.run = run
        self.out = (run / "restart-soak.jsonl").open("a", buffering=1)
        self.g = None
        self.pid = None

    def log(self, **kw):
        kw = {"t": round(time.time(), 3), **kw}
        line = json.dumps(kw)
        self.out.write(line + "\n")
        print(line, flush=True)

    def connect(self, timeout=180.0):
        """Attach to the current session's lab QMP (a new one after each relaunch)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                sess = json.loads((self.run / "session.json").read_text())
                if sess["pid"] != self.pid:
                    g = guestio.Guest(sess["qmp_lab"])
                    g.screenshot()
                    self.g, self.pid = g, sess["pid"]
                    return sess
            except (OSError, ValueError, KeyError, guestio.QMPError):
                pass
            time.sleep(1)
        return None

    def circle(self, seconds=1.5):
        x = y = 0.0
        t0 = time.monotonic()
        for i in range(int(seconds * 125)):
            a = 2 * math.pi * i * 2 / 125
            nx, ny = 30 * math.cos(a), 30 * math.sin(a)
            dx, dy = round(nx - x), round(ny - y)
            x, y = x + dx, y + dy
            self.g.qmp.cmd("input-send-event", events=[{"type": "rel", "data": {"axis": "x", "value": dx}},
                                                       {"type": "rel", "data": {"axis": "y", "value": dy}}])
            d = t0 + (i + 1) / 125 - time.monotonic()
            if d > 0:
                time.sleep(d)

    def alive(self) -> bool:
        self.g.move(480, 330)
        a = self.g.screenshot().signature()
        self.circle()
        if self.g.screenshot().signature() != a:
            return True
        time.sleep(3)
        self.circle()
        return self.g.screenshot().signature() != a

    def hang(self, where, **kw):
        regs = []
        for _ in range(5):
            try:
                regs.append(self.g.qmp.cmd("human-monitor-command", **{"command-line": "info registers"}))
            except guestio.QMPError as exc:
                regs.append(f"error: {exc}")
            time.sleep(1)
        (self.run / "hang-registers.txt").write_text("\n\n".join(regs))
        try:
            self.g.screenshot().save(self.run / "hang.png")
        except Exception as exc:  # noqa: BLE001 - evidence only
            kw["screenshot_error"] = str(exc)[:200]
        nips = [next((ln.split()[1] for ln in r.splitlines() if ln.startswith("NIP")), None) for r in regs]
        self.log(kind="hang", where=where, nip=nips, **kw)
        sys.exit(1)

    def audion(self, play: float):
        g = self.g
        g.move(480, 539)
        time.sleep(1.2)
        g.move(28, 527)
        time.sleep(0.3)
        g.button(True)
        time.sleep(0.8)
        g.move(84, 309)
        time.sleep(0.4)
        g.button(False)
        time.sleep(12)
        g.click(216, 127)                   # Play
        time.sleep(3)
        g.tap("command", "equal")           # Visualizer Window
        time.sleep(play)
        if not self.alive():
            self.hang("audion-playing")
        g.tap("command", "q")
        time.sleep(5)
        if not self.alive():
            self.hang("after-audion-quit")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run")
    ap.add_argument("cycles", type=int)
    ap.add_argument("--no-audion", action="store_true")
    ap.add_argument("--play", type=float, default=20.0)
    a = ap.parse_args()
    s = Soak(Path(a.run).resolve())
    for n in range(1, a.cycles + 1):
        sess = s.connect()
        if sess is None:
            s.log(kind="no_session", n=n)
            return 2
        t0 = time.monotonic()
        if s.g.wait_menu_bar(180) is None:
            s.hang("startup", n=n)
        s.log(kind="finder", n=n, pid=s.pid, after_s=round(time.time() - sess["started"], 1))
        time.sleep(10)
        if not a.no_audion:
            s.audion(a.play)
            s.log(kind="audion_quit", n=n)
        s.g.tap("power", hold=0.15)
        time.sleep(2)
        s.g.tap("r")
        t_r = time.monotonic()
        old = s.pid
        while time.monotonic() - t_r < 90:
            try:
                if json.loads((s.run / "session.json").read_text())["pid"] != old:
                    break
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(1)
        else:
            s.hang("restart", n=n)
        s.log(kind="restarted", n=n, seconds=round(time.monotonic() - t_r, 1), cycle_s=round(time.monotonic() - t0))
    sess = s.connect()
    ok = sess is not None and s.g.wait_menu_bar(180) is not None
    s.log(kind="done", cycles=a.cycles, final_finder=ok)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
