#!/usr/bin/env python3
"""Shut Down / Restart / power-off requests at controlled moments through real hostd
(hostd/reports/powerdlg-2026-10-12.md).

Real hostd, real guest-launcher and REAL QEMU on a disposable Mac OS 9 disk, with the
fake presenter (tests/e2e/real_qemu.py's setup); requests go through the web API like
the browser's.  hostd's lab QMP socket gives this harness its own screendumps, so the
moment the Finder's menu bar first appears is known independently of hostd's probe.

    power_series.py --work DIR --disk COPY.img --qemu-package PKG --ndrv DIR --loader DIR
        [--hostd-src DIR] [--card std|ati] [--controller cuda|pmu]
        --plan restart@f0,restart@f1,shutdown@f2,hostoff@f5,restart@p5,restart@a1,button@f0

KIND: window (open the Macintosh HD window at a settled Finder, so the Finder also
reopens it while it starts, and Restart 4 s later: measured as restart@window4), restart (guest.restart), shutdown (guest.shutdown, RetroBoot stays on; the next
step starts Mac OS again), hostoff (host.poweroff with hostd's dev power hook: nothing
is turned off; the next step starts Mac OS again), button (the host power button: Mac
OS's dialog must come up; this harness answers it with R).
WHEN: fN = N s after the Finder's menu bar first appeared; pN = N s after the Mac OS
gray (extension parade); aN = N s after an application (Sherlock 2) was chosen from the
Apple menu at a settled Finder.

``--hostd-src`` runs another hostd tree (the code before a change) with the same
harness.  Evidence: DIR/evidence (results.json, steps.jsonl, lifecycle log, frames).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent))
import real_qemu  # noqa: E402
from retroboot import powerdlg  # noqa: E402
from retroboot.qmp import finder_menu_bar, read_ppm  # noqa: E402

try:
    sys.path.insert(0, str(HERE.parents[2] / "apps"))
    from rbapps import guestio  # noqa: E402
except ImportError:  # pragma: no cover
    guestio = None


class Series(real_qemu.Run):
    def setup(self):
        super().setup()
        cfgp = self.root / "etc/retroboot/hostd.json"
        cfg = json.loads(cfgp.read_text())
        cfg.update({"lab_qmp": True, "power_controller": self.a.controller,
                    "graphics_allow_software": self.a.card == "ati",
                    "finder_wait_timeout": 240.0 if self.a.finder_after is None else 150.0,   # 150: the product's
                    "clean_stop_timeout": 90.0})
        cfgp.write_text(json.dumps(cfg, indent=2))
        if self.a.card == "ati":
            data = self.root / "var/lib/retroboot/config"
            data.mkdir(parents=True, exist_ok=True)
            vals = {"display.graphics": "ati-radeon", "machine.active": "mac-os-9-2-1"}
            from retroboot import schema
            (data / "settings.json").write_text(json.dumps({
                "schema": schema.SCHEMA_VERSION, "revision": 1, "desired": vals, "applied": vals,
                "applied_revision": 1}))
        self.steps_f = (self.ev / "steps.jsonl").open("a", buffering=1)

    def start_hostd(self):
        import subprocess
        log = open(self.ev / "hostd.stderr", "ab")
        src = str(Path(self.a.hostd_src).resolve()) if self.a.hostd_src else str(real_qemu.SRC)
        env = dict(os.environ, PYTHONPATH=src)
        p = subprocess.Popen([sys.executable, "-m", "retroboot.hostd", "--root", str(self.root)], env=env,
                             stdout=log, stderr=log)
        self.procs.append(p)
        self.hostd = p
        return p

    # -- the harness's own view of the guest -------------------------------------------
    def lab(self):
        if getattr(self, "_lab", None) is None or self._lab_session != self.session():
            try:
                self._lab = guestio.QMP(str(self.root / "run/retroboot/qmp-lab.sock"), timeout=10)
                self._lab_session = self.session()
            except Exception:
                self._lab = None
        return self._lab

    def session(self):
        starts = [e for e in self.events() if e["event"] == "guest_start"]
        return starts[-1]["session"] if starts else None

    def frame(self):
        q = self.lab()
        if q is None:
            return None
        path = self.work / "lab-shot.ppm"
        try:
            q.cmd("screendump", filename=str(path), timeout=10)
            return read_ppm(path.read_bytes())
        except Exception:
            self._lab = None
            return None

    def pair(self):
        self.inject({"event": "chord", "name": "recovery"})
        self.wait_doc("recovery-main")
        self.inject({"click": "network"})
        import re
        m = self.wait(lambda: re.search(r'"text": "(\d) (\d) (\d) (\d) (\d) (\d)"', json.dumps(self.doc())), 20,
                      "pairing code")
        self.inject({"click": "cancel"})
        self.wait_doc("recovery-main")
        self.inject({"click": "cancel"})
        c, pr = self.curl("POST", "/api/v1/pair", {"code": "".join(m.groups()), "device_name": "power series"},
                          auth=False)
        self.csrf = pr.get("csrf") if isinstance(pr, dict) else None
        if not self.csrf:
            raise RuntimeError(f"pairing failed: {c} {pr}")

    def latest_session(self, n_start: int):
        starts = [e for e in self.events() if e["event"] == "guest_start"]
        return starts[-1]["session"] if len(starts) >= n_start else None

    def wait_boot(self, n_start: int, timeout=300):
        """Until boot n_start (or hostd's retry of it, after a hang before the gray) has its
        Finder on the lab screendumps: (session, t_content, t_finder) in monotonic time."""
        self.wait(lambda: self.count("guest_start") >= n_start, 120, f"guest_start {n_start}")
        sess = self.latest_session(n_start)
        t_content = None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            latest = self.latest_session(n_start)
            if latest != sess:                      # hostd retried the start
                sess, t_content = latest, None
            if t_content is None and self.count("guest_content", session=sess):
                t_content = time.monotonic()
            if t_content is not None and self.a.finder_after is not None:
                if time.monotonic() >= t_content + self.a.finder_after:   # a theme's menu bar: by time
                    return sess, t_content, t_content + self.a.finder_after
                time.sleep(0.1)
                continue
            f = self.frame() if t_content is not None else None
            if f and finder_menu_bar(f[0], f[2]):
                return sess, t_content, time.monotonic()
            time.sleep(0.25)
        raise TimeoutError("no Finder")

    def launch_app(self):
        """Apple menu > Sherlock 2, by pointer through the presenter (press, drag, release)."""
        self.inject({"guest_pointer": [16, 10]})
        time.sleep(0.2)
        self.inject({"guest_button": True})
        time.sleep(0.6)
        for k in range(1, 9):
            self.inject({"guest_pointer": [16 + (70 - 16) * k // 8, 10 + (248 - 10) * k // 8]})
            time.sleep(0.04)
        time.sleep(0.3)
        self.inject({"guest_button": False})

    def settled(self, timeout=40):
        """A settled Finder for the app step: the screen unchanged for 2 s."""
        prev = None
        still = None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            f = self.frame()
            if f and prev and f[2] == prev[2]:
                still = still or time.monotonic()
                if time.monotonic() - still >= 2.0:
                    return True
            else:
                still = None
            prev = f
            time.sleep(0.5)
        return False

    def post_job(self, method, path, body):
        code, data = self.curl(method, path, body)
        if code not in (200, 202):
            raise RuntimeError(f"{path} -> {code} {data}")
        return data["job"]["id"] if "job" in data else data["id"]

    def job_state(self, jid):
        _, j = self.curl("GET", f"/api/v1/jobs/{jid}")
        return j

    # -- one step -----------------------------------------------------------------------
    def open_window(self, n_start: int) -> int:
        """Setup step: open the Macintosh HD window at a settled Finder (double-click its icon,
        top right) and restart cleanly; from then on the Finder reopens the window while it
        starts up, drawing it in the middle of the screen during its busy seconds."""
        sess, _, _ = self.wait_boot(n_start)
        self.settled()
        f = self.frame()
        x, y = (f[0] - 48, 52) if f else (976, 52)
        self.inject({"guest_pointer": [x, y]})
        time.sleep(0.3)
        for _ in range(2):
            self.inject({"guest_button": True})
            time.sleep(0.08)
            self.inject({"guest_button": False})
            time.sleep(0.12)
        time.sleep(4)
        self.guest_png(f"window-{n_start}")
        self.note("window-opened", session=sess)
        # measured too: a Restart 4 s after the window opened (the case the window series found)
        rec = {"i": f"w{n_start}", "spec": "restart@window4", "session": sess, "boot": n_start,
               "load1": round(os.getloadavg()[0], 2)}
        rec, n_next = self.request(rec, "restart", None, n_start, sess)
        return n_next

    def step(self, i: int, spec: str, n_start: int) -> tuple[dict, int]:
        kind, when = spec.split("@")
        sess, t_content, t_finder = self.wait_boot(n_start)
        rec = {"i": i, "spec": spec, "session": sess, "boot": n_start,
               "content_to_finder_s": round(t_finder - t_content, 2), "load1": round(os.getloadavg()[0], 2)}
        w0, n = when[0], float(when[1:] or 0)
        if w0 == "p":
            raise RuntimeError("use pN only as the first step after a start (see run())")
        if w0 == "a":
            rec["settled"] = self.settled()
            self.launch_app()
            t_ref = time.monotonic()
        else:
            t_ref = t_finder
        while time.monotonic() < t_ref + n:
            time.sleep(0.02)
        return self.request(rec, kind, t_finder, n_start, sess)

    def step_parade(self, i: int, spec: str, n_start: int) -> tuple[dict, int]:
        kind, when = spec.split("@")
        n = float(when[1:] or 0)
        self.wait(lambda: self.count("guest_start") >= n_start, 120, f"guest_start {n_start}")

        def content():
            nonlocal sess
            sess = self.latest_session(n_start)
            return self.count("guest_content", session=sess)
        sess = None
        self.wait(content, 120, "content")
        t_content = time.monotonic()
        rec = {"i": i, "spec": spec, "session": sess, "boot": n_start, "load1": round(os.getloadavg()[0], 2)}
        while time.monotonic() < t_content + n:
            time.sleep(0.02)
        return self.request(rec, kind, None, n_start, sess, t_content=t_content)

    def request(self, rec, kind, t_finder, n_start, sess, t_content=None):
        t_req = time.monotonic()
        rec["t_req_after_finder_s"] = round(t_req - t_finder, 2) if t_finder else None
        rec["t_req_after_content_s"] = round(t_req - t_content, 2) if t_content else None
        jid = None
        if kind == "restart":
            jid = self.post_job("POST", "/api/v1/guest/restart", {})
        elif kind == "shutdown":
            jid = self.post_job("POST", "/api/v1/guest/shutdown", {})
        elif kind == "hostoff":
            jid = self.post_job("POST", "/api/v1/host/poweroff", {})
        elif kind == "button":
            before = self.frame()
            self.inject({"event": "power_button"})
            last_box = None
        n_exit_before = self.count("guest_exited")
        exited = None
        job = None
        deadline = t_req + 260      # main waits up to finder_wait_timeout (150 s) for a Finder it may not see
        answered = False
        while time.monotonic() < deadline:
            if self.count("guest_exited") > n_exit_before and exited is None:
                exited = time.monotonic()
            if jid:
                job = self.job_state(jid)
                if job["state"] in ("succeeded", "failed") and (exited or job["state"] == "failed"):
                    break
            elif kind == "button":
                if not answered:
                    res = powerdlg.find_power_dialog(before, self.frame())
                    if res["found"] and res["box"] == last_box:
                        rec["button_dialog_s"] = round(time.monotonic() - t_req, 2)
                        time.sleep(0.3)
                        self.inject({"guest_keys": ["r"]})
                        answered = True
                    last_box = res["box"] if res["found"] else None
                if exited:
                    break
            time.sleep(0.25)
        evs = [e for e in self.events() if e.get("session") == sess]
        rec.update({
            "job": None if not job else {"state": job["state"], "error": (job.get("error") or {}).get("code"),
                                         "message": job.get("message") if job["state"] == "failed" else None},
            "exit_after_s": round(exited - t_req, 2) if exited else None,
            "exit": ([e for e in self.events() if e["event"] == "guest_exited"][-1:] or [{}])[-1].get("reason")
            if exited else None,
            "keys": sum(1 for e in evs if e["event"] == "power_key_sent"),
            "detected": sum(1 for e in evs if e["event"] == "power_dialog_detected"),
            "ignored": sum(1 for e in evs if e["event"] == "power_key_ignored"),
            "confirmed": sum(1 for e in evs if e["event"] == "power_dialog_confirmed"),
            "ready": [{k: e.get(k) for k in ("idle", "waited_s", "attempt")} for e in evs if e["event"] == "power_ready"],
        })
        ok = bool(exited) and (job is None or job["state"] == "succeeded")
        rec["first_try"] = ok
        if not ok:
            self.guest_png(f"fail-{rec['i']}")
        print(json.dumps(rec), flush=True)
        self.steps_f.write(json.dumps(rec) + "\n")
        self.results.setdefault("series", []).append(rec)
        # get the next boot going
        if not exited:
            j = self.post_job("POST", "/api/v1/guest/force-restart", {})
            self.wait(lambda: self.job_state(j)["state"] in ("succeeded", "failed"), 240, "force restart")
        elif rec["exit"] == "guest-shutdown":
            self.wait(lambda: not self.job_running(), 60, "jobs idle")
            time.sleep(1.0)
            j = self.post_job("POST", "/api/v1/guest/start", {})
            self.wait(lambda: self.job_state(j)["state"] in ("succeeded", "failed"), 300, "start")
        return rec, self.count("guest_start") + (0 if self.latest_session(1) != sess else 1)

    def job_running(self):
        _, js = self.curl("GET", "/api/v1/jobs")
        return any(j["state"] in ("queued", "running") for j in (js.get("jobs") or []))

    def run_plan(self):
        self.start_presenter()
        self.start_hostd()
        self.wait_event("presenter_display", 1, 30)
        if self.a.finder_after is None:
            self.wait_event("finder_detected", 1, 300)
        else:
            self.wait_event("guest_content", 1, 300)
            time.sleep(self.a.finder_after + 5)
        self.pair()
        n_start = self.count("guest_start")
        if self.a.card == "ati":
            argv = self.launch_argv()
            self.note("card", ati=any("ati-vga" in x for x in argv))
        # the first boot is the setup boot: restart it at a settled Finder
        time.sleep(15)
        j = self.post_job("POST", "/api/v1/guest/restart", {})
        self.wait(lambda: self.job_state(j)["state"] in ("succeeded", "failed"), 400, "setup restart")
        if self.job_state(j)["state"] != "succeeded":
            self.note("setup-restart-failed", job=self.job_state(j))
            f = self.post_job("POST", "/api/v1/guest/force-restart", {})
            self.wait(lambda: self.job_state(f)["state"] in ("succeeded", "failed"), 240, "setup force restart")
        n_start += 1
        for i, spec in enumerate(self.a.plan.split(",")):
            if spec.startswith("window@"):
                n_start = self.open_window(n_start)
            elif spec.split("@")[1].startswith("p"):
                _, n_start = self.step_parade(i, spec, n_start)
            else:
                _, n_start = self.step(i, spec, n_start)
        ser = self.results.get("series", [])
        self.note("summary", first_try=sum(1 for r in ser if r["first_try"]), total=len(ser))


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
    ap.add_argument("--plan", required=True)
    ap.add_argument("--run-as", default="")
    ap.add_argument("--https-port", type=int, default=8443)
    ap.add_argument("--http-port", type=int, default=0)
    ap.add_argument("--display-mode", default="1024x768")
    ap.add_argument("--handoff-deadline", type=float, default=60.0)
    ap.add_argument("--mark-prepared", action="store_true")
    ap.add_argument("--finder-after", type=float, default=None,
                    help="themes whose menu bar is not recognized: the Finder is taken to appear this long after "
                         "guest_content (lab: about 14 s)")
    a = ap.parse_args()
    import signal

    def on_term(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")
    signal.signal(signal.SIGTERM, on_term)
    r = Series(a)
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
