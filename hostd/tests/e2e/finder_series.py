#!/usr/bin/env python3
"""Finder detection through real hostd (hostd/reports/finderdet-2026-10-13.md).

Real hostd, guest-launcher and REAL QEMU on a disposable Mac OS 9 disk (power_series.py's
setup), ``--boots`` boots in a row.  For each boot it records what hostd reports
(``finder_detected`` with its rule, or ``mac_os_started``, from the lifecycle log, and the
web status's ``mac_os_up``) against the frame truth: this harness's own lab-QMP
screendumps every ``--cadence`` s from ``guest_content``, the first one that shows the
Finder's menus (``finder.finder_on_screen``, checked frame by frame against the saved
frames with finder_lab.py).  The truth frame and the one before it are saved.  Between
boots: guest.restart through the web API, about 8 s after hostd's detection.

    finder_series.py --work DIR --disk COPY.img --qemu-package PKG --ndrv DIR --loader DIR
        [--hostd-src DIR] [--boots 8] [--cadence 0.25]

``--hostd-src`` runs another hostd tree (main, before this change) with the same harness.
Evidence: DIR/evidence (results.json, boots.jsonl, lifecycle log, frames).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import power_series  # noqa: E402  (sets up the paths)

from retroboot import finder  # noqa: E402


def save_png(path: Path, frame) -> None:
    w, h, rgb = frame
    sys.path.insert(0, str(HERE.parents[2] / "emulator" / "tools"))
    import rbd_presenter_stub as stub
    path.write_bytes(stub.png_bytes(w, h, rgb))


class FinderSeries(power_series.Series):
    def setup(self):
        super().setup()
        self.csrf = None
        self.boots_f = (self.ev / "boots.jsonl").open("a", buffering=1)

    def ev_for(self, name, sess):
        return [e for e in self.events() if e["event"] == name and e.get("session") == sess]

    def observe(self, n_start: int) -> dict:
        self.wait(lambda: self.count("guest_start") >= n_start, 180, f"guest_start {n_start}")
        sess = self.latest_session(n_start)
        truth = prev = None
        deadline = time.monotonic() + 150
        hostd_done_at = None
        while time.monotonic() < deadline:
            latest = self.latest_session(n_start)
            if latest != sess:                         # hostd retried the start
                sess, truth, prev = latest, None, None
            if truth is None and self.ev_for("guest_content", sess):
                t = time.monotonic()
                f = self.frame()
                if f and finder.finder_on_screen(*f):
                    truth = t
                    save_png(self.ev / f"b{n_start:02d}-truth.png", f)
                    if prev:
                        save_png(self.ev / f"b{n_start:02d}-before.png", prev)
                elif f:
                    prev = f
            up = self.ev_for("finder_detected", sess) or self.ev_for("mac_os_started", sess)
            if up and hostd_done_at is None:
                hostd_done_at = time.monotonic()
            if truth is not None and hostd_done_at is not None:
                break
            if hostd_done_at is not None and time.monotonic() > hostd_done_at + 60:
                break                                  # hostd decided; no truth frame (unexpected)
            time.sleep(self.a.cadence)
        start = self.ev_for("guest_start", sess)[0]
        t0 = start["t_mono"]
        content = (self.ev_for("guest_content", sess) or [{}])[0]
        fd = (self.ev_for("finder_detected", sess) or [None])[0]
        ms = (self.ev_for("mac_os_started", sess) or [None])[0]
        _, st = self.curl("GET", "/api/v1/status") if self.csrf else (None, {})
        guest = (st or {}).get("guest") or {}
        rec = {"boot": n_start, "session": sess,
               "content_s": round(content["t_mono"] - t0, 2) if content else None,
               "truth_s": round(truth - t0, 2) if truth else None,
               "finder_detected_s": round(fd["t_mono"] - t0, 2) if fd else None,
               "via": fd.get("via", "platinum") if fd else None,
               "mac_os_started_s": round(ms["t_mono"] - t0, 2) if ms else None,
               "status_finder": guest.get("finder"), "status_mac_os_up": guest.get("mac_os_up")}
        hit = rec["finder_detected_s"] if rec["finder_detected_s"] is not None else rec["mac_os_started_s"]
        rec["lag_s"] = round(hit - rec["truth_s"], 2) if hit is not None and rec["truth_s"] is not None else None
        rec["early"] = rec["lag_s"] is not None and rec["lag_s"] < -self.a.cadence
        return rec

    def run_plan(self):
        self.start_presenter()
        self.start_hostd()
        self.wait_event("presenter_display", 1, 30)
        n_start = 1
        for i in range(self.a.boots):
            rec = self.observe(n_start)
            if not self.csrf:
                time.sleep(3)
                self.pair()
                _, st = self.curl("GET", "/api/v1/status")
                rec["status_finder"] = ((st or {}).get("guest") or {}).get("finder")
                rec["status_mac_os_up"] = ((st or {}).get("guest") or {}).get("mac_os_up")
            rec["load1"] = round(__import__("os").getloadavg()[0], 2)
            print(json.dumps(rec), flush=True)
            self.boots_f.write(json.dumps(rec) + "\n")
            self.results.setdefault("boots", []).append(rec)
            if i == self.a.boots - 1:
                break
            time.sleep(self.a.settle)
            starts = self.count("guest_start")
            j = self.post_job("POST", "/api/v1/guest/restart", {})
            self.wait(lambda: self.job_state(j)["state"] in ("succeeded", "failed"), 300, "restart")
            if self.job_state(j)["state"] != "succeeded":
                self.note("restart-failed", job=self.job_state(j))
                f = self.post_job("POST", "/api/v1/guest/force-restart", {})
                self.wait(lambda: self.job_state(f)["state"] in ("succeeded", "failed"), 240, "force restart")
            n_start = starts + 1
        j = self.post_job("POST", "/api/v1/guest/shutdown", {})
        self.wait(lambda: self.job_state(j)["state"] in ("succeeded", "failed"), 300, "shutdown")
        b = self.results.get("boots", [])
        self.note("summary", boots=len(b), early=sum(1 for r in b if r["early"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--disk", required=True)
    ap.add_argument("--qemu-package", required=True)
    ap.add_argument("--ndrv")
    ap.add_argument("--loader")
    ap.add_argument("--guestprep")
    ap.add_argument("--web")
    ap.add_argument("--hostd-src")
    ap.add_argument("--card", choices=("std", "ati"), default="std")
    ap.add_argument("--controller", choices=("cuda", "pmu"), default="cuda")
    ap.add_argument("--boots", type=int, default=8)
    ap.add_argument("--cadence", type=float, default=0.25)
    ap.add_argument("--settle", type=float, default=8.0)
    ap.add_argument("--run-as", default="")
    ap.add_argument("--https-port", type=int, default=8443)
    ap.add_argument("--http-port", type=int, default=0)
    ap.add_argument("--display-mode", default="1024x768")
    ap.add_argument("--handoff-deadline", type=float, default=60.0)
    ap.add_argument("--mark-prepared", action="store_true")
    a = ap.parse_args()
    a.finder_after = None
    a.plan = ""
    import signal

    def on_term(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")
    signal.signal(signal.SIGTERM, on_term)
    r = FinderSeries(a)
    r.setup()
    err = None
    try:
        r.run_plan()
    except BaseException as exc:
        err = exc
    finally:
        r.finish(err)
    sys.exit(1 if err else 0)


if __name__ == "__main__":
    main()
