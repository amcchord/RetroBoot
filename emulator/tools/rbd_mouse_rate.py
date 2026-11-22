#!/usr/bin/env python3
"""How often Mac OS takes a new pointer position, through one rbsession run's lab QMP (lab).

    rbd_mouse_rate.py RUN [--abs] [--turns 2] [--seconds 4] [--screen 960x540]

Moves the pointer in a circle at 125 Hz (relative events reach the USB mouse
as a real mouse on the presenter does; ``--abs`` uses the tablet) and polls
Mac OS's low-memory RawMouse (0x82C) and Mouse (0x830) through the CPU's
logical mapping (HMP ``x``).  Reads taken while the CPU is in real mode return
physical memory instead; only samples whose CrsrPin (0x838) is the screen
rectangle count.  Mouse changes once per VBL that saw new motion, so its rate
is the guest's pointer rate (below a pixel per frame the acceleration curve
holds slow motion back).  RUN is an apps/lab/rbsession.py run directory whose
lab QMP no other client holds.
"""
import argparse
import json
import math
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[2] / "apps"))
from rbapps import guestio  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run")
    ap.add_argument("--abs", action="store_true")
    ap.add_argument("--turns", type=float, default=2.0)
    ap.add_argument("--seconds", type=float, default=4.0)
    ap.add_argument("--screen", default="960x540")
    a = ap.parse_args()
    w, h = (int(v) for v in a.screen.split("x"))
    pin = "0x%04x%04x" % (h, w)
    sess = json.loads((Path(a.run) / "session.json").read_text())
    g = guestio.Guest(sess["qmp_lab"])
    g.screenshot()
    lock = threading.Lock()

    def cmd(name, **kw):
        with lock:
            return g.qmp.cmd(name, **kw)

    cmd("input-send-event", events=[{"type": "abs", "data": {"axis": "x", "value": 16384}},
                                    {"type": "abs", "data": {"axis": "y", "value": 16384}}])
    time.sleep(0.5)
    stop = threading.Event()

    def mover():
        x = y = 0.0
        t0 = time.monotonic()
        i = 0
        while not stop.is_set():
            t = 2 * math.pi * i * a.turns / 125
            if a.abs:
                ev = [{"type": "abs", "data": {"axis": "x", "value": int(16384 + 1200 * math.cos(t))}},
                      {"type": "abs", "data": {"axis": "y", "value": int(16384 + 1200 * math.sin(t))}}]
            else:
                nx, ny = 24 * math.cos(t), 24 * math.sin(t)
                dx, dy = round(nx - x), round(ny - y)
                x, y = x + dx, y + dy
                ev = [{"type": "rel", "data": {"axis": "x", "value": dx}},
                      {"type": "rel", "data": {"axis": "y", "value": dy}}]
            cmd("input-send-event", events=ev)
            i += 1
            d = t0 + i / 125 - time.monotonic()
            if d > 0:
                time.sleep(d)

    th = threading.Thread(target=mover)
    th.start()
    time.sleep(0.5)
    samples = []
    end = time.monotonic() + a.seconds
    try:
        while time.monotonic() < end:
            out = cmd("human-monitor-command", **{"command-line": "x /5wx 0x828"})
            t = time.monotonic()
            words = " ".join(ln.split(":", 1)[1] for ln in out.splitlines() if ":" in ln).split()
            if len(words) == 5 and words[4] == pin:
                samples.append((t, words[1], words[2]))
    finally:
        stop.set()
        th.join()
        g.close()

    def rate(i):
        ch = [samples[k][0] for k in range(1, len(samples)) if samples[k][i] != samples[k - 1][i]]
        gaps = sorted(round((b - c) * 1000, 1) for c, b in zip(ch, ch[1:]))
        dur = samples[-1][0] - samples[0][0]
        return {"changes_per_s": round(len(ch) / dur, 1), "median_gap_ms": gaps[len(gaps) // 2] if gaps else None}

    dur = samples[-1][0] - samples[0][0]
    print(json.dumps({"input": "tablet" if a.abs else "usb-mouse", "turns_per_s": a.turns,
                      "reads_per_s": round(len(samples) / dur), "RawMouse": rate(1), "Mouse": rate(2)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
