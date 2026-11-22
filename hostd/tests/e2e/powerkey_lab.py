#!/usr/bin/env python3
"""Power-key lab: when does Mac OS 9 take the ADB power key? (hostd/reports/powerdlg-2026-10-12.md)

Boots a disposable disk with hostd's own QEMU arguments (``machines.build_argv``, as
hostd and apps/lab/rbsession.py build them: standard card or the ATI Radeon, Cuda or
the PMU machine) behind the presenter stub, and for every step of ``--plan`` presses
the power key once at a controlled moment.  Every 0.2 s it records the frame (change
below the menu bar, the Finder menu bar, ``powerdlg.find_power_dialog`` against the
frame before the key) and the TCG thread's busy share (``powerdlg.GuestLoad``).  When
the dialog shows, R restarts Mac OS and QEMU is relaunched for the next step, like
hostd; with no dialog the key is pressed again once Mac OS is idle.

Steps (one per boot):
  fN   the key N s after the first Finder menu bar (f0: at once)
  pN   the key N s after the Mac OS gray (extension parade)
  aN   Finder settled, then an application is launched from the Apple menu; the key N s
       after the click
  oN   Finder settled, then the Macintosh HD window is opened (double-click); the key N s later
  nN   no key for N s after the Finder (detector false-positive survey), then a key

    powerkey_lab.py --out RUN --seed DISK --plan f0,f1,f2,p5 [--card std|ati] [--pmu]
        [--size 1024x768] [--pkg DIR] [--ndrv DIR] [--loader DIR]

Lab tool for the Linux builder (root).  Frames stay in RUN (private: Apple's pixels).
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import signal
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve()
REPO = HERE.parents[3]
sys.path.insert(0, str(REPO / "hostd"))
sys.path.insert(0, str(REPO / "apps"))
sys.path.insert(0, str(REPO / "emulator" / "tools"))
sys.path.insert(0, str(REPO / "guestprep"))

import rbd_presenter_stub as stub  # noqa: E402
from rbapps import guestio  # noqa: E402
from retroboot import powerdlg  # noqa: E402
from retroboot import schema  # noqa: E402
from retroboot.config import Paths  # noqa: E402
from retroboot.machines import LaunchOptions, Machine, build_argv  # noqa: E402
from retroboot.qmp import finder_menu_bar  # noqa: E402


def rgb_frame(p: stub.Presenter, fr=None):
    w, h, stride, raw = fr or p.frame_raw()
    return w, h, stub.xrgb_to_rgb(raw, w, h, stride)


def save(frame, path: Path):
    w, h, rgb = frame
    path.write_bytes(stub.png_bytes(w, h, rgb))


class Lab:
    def __init__(self, a):
        self.a = a
        self.run = Path(a.out).resolve()
        self.run.mkdir(parents=True, exist_ok=True)
        self.disk = self.run / "machine" / "disk.img"
        self.disk.parent.mkdir(exist_ok=True)
        self.paths = Paths(root=self.run, qemu_package_override=Path(a.pkg),
                           ndrv_override=Path(a.ndrv) if a.ndrv else None)
        self.paths.ensure()
        self.p = stub.Presenter(self.paths.display_sock, None)
        threading.Thread(target=self.p.accept_loop, daemon=True).start()
        self.res = (self.run / "steps.jsonl").open("a", buffering=1)
        self.proc = None
        self.machine = Machine("lab", self.disk.parent, {"schema_version": 1, "id": "lab", "disk": "disk.img",
                                                         "name": "Mac OS 9"})
        self.settings = schema.defaults()
        self.settings.update({"share.enabled": False, "system.timezone": "UTC"})
        if a.card == "ati":
            from retroboot import graphics
            self.settings["display.graphics"] = "ati-radeon"
            self.machine.meta["graphics"] = {"ati_software": "present"}
            graphics.set_host_gl(graphics.probe_host_gl(allow_software=True))

    def fresh_disk(self):
        subprocess.run(["cp", "--sparse=always", self.a.seed, str(self.disk)], check=True)
        # Mac OS's location at one of its Date & Time cities, as hostd sets it before each
        # launch (supervisor.mac_cities): otherwise Time Synchronizer's "Network Time Error"
        # note comes up after the Finder and holds a Restart.
        from rbguestprep import macloc
        self.cities = list(macloc.read_cities(str(self.disk))) or None

    def argv(self, boot: int) -> list[str]:
        w, h = self.a.size
        display = {"mode": [w, h, 60], "suggested_scale": 1}
        argv = build_argv(self.machine, self.settings, self.paths, display,
                          LaunchOptions(display_backend="retroboot", audio_capable=True,
                                        ndrv_dir=Path(self.a.ndrv) if self.a.ndrv else None,
                                        loader_dir=Path(self.a.loader) if self.a.loader else None,
                                        power_socket=self.run / "power.sock" if self.a.pmu else None,
                                        mac_cities=self.cities))
        argv[argv.index("-audiodev") + 1] = "none,id=snd0"
        argv[argv.index("-rtc") + 1] = "base=2026-10-12T09:00:00"     # before any scheduled task
        return argv

    def start(self, boot: int):
        argv = self.argv(boot)
        for s in (self.paths.qmp_control, self.paths.qmp_events):
            s.unlink(missing_ok=True)
        (self.run / f"argv-{boot}.json").write_text(json.dumps(argv, indent=1))
        with self.p.lock:
            conn_before = self.p.conn_id
        t0 = time.monotonic()
        self.proc = subprocess.Popen(argv, stdout=open(self.run / f"qemu-{boot}.out", "w"),
                                     stderr=subprocess.STDOUT, cwd=str(self.run), start_new_session=True,
                                     env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root",
                                          "LANG": "C.UTF-8", "TZ": ":UTC"})
        self.qe = guestio.QMP(str(self.paths.qmp_events), timeout=20)
        self.qc = guestio.QMP(str(self.paths.qmp_control), timeout=20)
        hello = self.p.wait_for(lambda: self.p.hello if self.p.hello and
                                self.p.hello["connection"] > conn_before else None, 30)
        if hello:
            w, h = self.a.size
            try:
                self.p.send(stub.REFRESH_RATE, struct.pack("=I", 16))
                self.p.send(stub.RESIZE, struct.pack("=II", w, h))
            except (RuntimeError, OSError):
                pass
        self.conn = conn_before
        self.load = powerdlg.GuestLoad(self.proc.pid)
        return t0

    def key(self, name: str, hold_ms: int = 100):
        self.qc.cmd("send-key", keys=[{"type": "qcode", "data": name}], **{"hold-time": hold_ms})

    def shutdown_reason(self):
        with self.qe.cond:
            sd = [e for e in self.qe.events if e.get("event") == "SHUTDOWN"]
        return (sd[-1].get("data") or {}).get("reason") if sd else None

    def kill(self):
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.proc.wait(10)

    # -- one boot, one step --------------------------------------------------------
    def step(self, boot: int, step: str, last: bool) -> dict:
        a = self.a
        kind, n = step[0], float(step[1:] or 0)
        t0 = self.start(boot)
        bdir = self.run / f"b{boot:02d}-{step}"
        bdir.mkdir(exist_ok=True)
        r = {"boot": boot, "step": step, "card": a.card, "machine": "pmu" if a.pmu else "cuda",
             "load1": round(os.getloadavg()[0], 2)}
        timeline = []          # (t, busy, changed, finder, dialog)
        gray = finder = key_at = None
        prev = None
        before = None
        keys = []
        dialog_at = None
        last_found = None
        idle_since = None
        app_clicked = None
        settled_at = None
        fp_boxes = []
        recent = collections.deque(maxlen=5)
        finder_frame = None
        deadline = t0 + a.timeout
        while time.monotonic() < deadline and self.proc.poll() is None:
            now = time.monotonic()
            el = now - t0
            self.load.sample(now)
            try:
                with self.p.lock:
                    cur_s = self.p.surface
                if not cur_s or cur_s["connection"] <= self.conn:
                    raise RuntimeError("no surface yet")
                fr = self.p.frame_raw()
                cur = rgb_frame(self.p, fr)
            except RuntimeError:
                time.sleep(0.2)
                continue
            busy = self.load.busy(now)
            changed = powerdlg.screen_changed(prev, cur) if prev else 1.0
            prev = cur
            w, h, rgb = cur
            if gray is None:
                an = stub.analyze_frame(*fr)
                if an.get("gray_fraction", 0) >= 0.97:
                    gray = el
                    r["gray_s"] = round(el, 2)
            is_finder = gray is not None and finder_menu_bar(w, rgb)
            if finder is None and is_finder:
                finder = el
                r["finder_s"] = round(el, 2)
                save(cur, bdir / "finder.png")
                finder_frame = cur
            quiet = changed == 0.0 and (busy is None or busy < a.idle_busy)
            idle_since = (idle_since or now) if quiet else None
            if finder is not None and settled_at is None and idle_since and now - idle_since >= a.settle_quiet:
                settled_at = el
                r["settled_s"] = round(el - (finder or 0), 2)
            found = None
            if before is not None:
                res = powerdlg.find_power_dialog(before, cur)
                found = res["found"]
                if found:
                    if last_found == res["box"] and dialog_at is None:
                        dialog_at = el
                        r["dialog"] = {"after_key_s": round(el - key_at, 2), "box": res["box"],
                                       "buttons": res["buttons"], "via": res.get("via"),
                                       "menus_dimmed": res["menus_dimmed"], "keys": len(keys)}
                        save(cur, bdir / "dialog.png")
                    last_found = res["box"]
                else:
                    last_found = None
            elif kind == "n" and finder is not None:
                # false-positive survey: compare against the first Finder frame and 1 s ago
                for ref_name, ref in (("finder", finder_frame), ("prev", recent[0] if len(recent) == 5 else None)):
                    if ref is None:
                        continue
                    res = powerdlg.find_power_dialog(ref, cur)
                    if res["found"]:
                        fp_boxes.append({"t": round(el - finder, 2), "ref": ref_name, "box": res["box"]})
                        save(cur, bdir / f"fp-{len(fp_boxes)}.png")
            timeline.append((round(el, 2), None if busy is None else round(busy, 3), round(changed, 4),
                             bool(is_finder), bool(found)))
            recent.append(cur)
            if len(timeline) > 2000:
                timeline.pop(0)
            # actions
            want_key = False
            if not keys:
                if kind == "p" and gray is not None and el >= gray + n:
                    want_key = True
                elif kind == "f" and finder is not None and el >= finder + n:
                    want_key = True
                elif kind == "n" and finder is not None and el >= finder + n:
                    want_key = True
                elif kind == "o" and finder is not None:
                    if app_clicked is None and settled_at is not None:
                        self.open_disk(bdir, w)
                        app_clicked = time.monotonic() - t0
                        r["app_click_s"] = round(app_clicked - finder, 2)
                    elif app_clicked is not None and el >= app_clicked + n:
                        want_key = True
                elif kind == "a" and finder is not None:
                    if app_clicked is None and settled_at is not None:
                        self.launch_app(bdir)
                        app_clicked = time.monotonic() - t0
                        r["app_click_s"] = round(app_clicked - finder, 2)
                    elif app_clicked is not None and el >= app_clicked + n:
                        want_key = True
            elif dialog_at is None and len(keys) < a.max_keys:
                # no dialog: press again once Mac OS has been idle for retry_idle s after the key
                if now - (t0 + keys[-1]) >= a.min_retry and idle_since and now - idle_since >= a.retry_idle:
                    want_key = True
                elif now - (t0 + keys[-1]) >= a.dialog_wait:
                    want_key = True
            if want_key:
                before = cur
                save(cur, bdir / f"before-{len(keys) + 1}.png")
                self.key("power", 150)
                key_at = time.monotonic() - t0
                keys.append(key_at)
                last_found = None
            if dialog_at is not None and el >= dialog_at + 0.5:
                break
            if keys and dialog_at is None and len(keys) >= a.max_keys and el >= keys[-1] + a.dialog_wait:
                break
            time.sleep(0.2)
        r["keys_rel_finder"] = [round(k - finder, 2) if finder is not None else None for k in keys]
        r["keys_rel_gray"] = [round(k - gray, 2) if gray is not None else None for k in keys]
        r["false_positives"] = fp_boxes
        # busy share and screen changes around each key
        def window(t_a, t_b):
            rows = [x for x in timeline if t_a <= x[0] < t_b]
            bs = [x[1] for x in rows if x[1] is not None]
            return {"busy_mean": round(sum(bs) / len(bs), 3) if bs else None,
                    "busy_max": max(bs) if bs else None, "changed_rows": sum(1 for x in rows if x[2] > 0)}
        r["around_keys"] = [{"before_1s": window(k - 1.0, k), "after_3s": window(k, k + 3.0)} for k in keys]
        if finder is not None:
            r["after_finder"] = [window(finder + i, finder + i + 1) for i in range(0, 15)]
        (bdir / "timeline.json").write_text(json.dumps([x[:5] for x in timeline]))
        # finish the boot: R (restart) through the dialog, or Return on the last step
        outcome = None
        if dialog_at is not None:
            self.key("ret" if last else "r")
            try:
                self.proc.wait(90)
                outcome = self.shutdown_reason()
            except subprocess.TimeoutExpired:
                outcome = "no-exit"
                save(rgb_frame(self.p), bdir / "no-exit.png")
        else:
            outcome = "no-dialog"
            try:
                save(rgb_frame(self.p), bdir / "no-dialog.png")
            except RuntimeError:
                pass
        r["outcome"] = outcome
        self.qe.close()
        self.qc.close()
        self.kill()
        self.res.write(json.dumps(r) + "\n")
        print(json.dumps({k: r.get(k) for k in ("boot", "step", "finder_s", "settled_s", "keys_rel_finder",
                                                "dialog", "outcome", "false_positives")}), flush=True)
        return r

    def open_disk(self, bdir: Path, w: int):
        """Double-click the Macintosh HD icon (top right): the Finder opens its window."""
        self.p.pointer(w - 48, 52)
        time.sleep(0.3)
        for _ in range(2):
            self.p.button("left", True)
            time.sleep(0.08)
            self.p.button("left", False)
            time.sleep(0.12)

    def launch_app(self, bdir: Path):
        """Apple menu > the --app item (press, drag, release like a user)."""
        x, y = self.a.app_xy
        self.p.pointer(16, 10)
        time.sleep(0.2)
        self.p.button("left", True)
        time.sleep(0.6)
        save(rgb_frame(self.p), bdir / "apple-menu.png")
        for k in range(1, 9):
            self.p.pointer(16 + (x - 16) * k // 8, 10 + (y - 10) * k // 8)
            time.sleep(0.05)
        time.sleep(0.3)
        self.p.button("left", False)


def parse_size(s):
    w, h = s.lower().split("x")[:2]
    return int(w), int(h)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", required=True)
    ap.add_argument("--plan", required=True)
    ap.add_argument("--card", choices=("std", "ati"), default="std")
    ap.add_argument("--pmu", action="store_true")
    ap.add_argument("--size", type=parse_size, default=(1024, 768))
    ap.add_argument("--pkg", default="/srv/retroboot/wave2/artifacts/qemu/linux-ppc-r16")
    ap.add_argument("--ndrv", default="/srv/retroboot/wave2/artifacts/ndrv-r16")
    ap.add_argument("--loader", default="/srv/retroboot/wave2/artifacts/classicvirtio-4")
    ap.add_argument("--timeout", type=float, default=200)
    ap.add_argument("--dialog-wait", type=float, default=20.0)
    ap.add_argument("--max-keys", type=int, default=3)
    ap.add_argument("--min-retry", type=float, default=4.0)
    ap.add_argument("--retry-idle", type=float, default=3.0)
    ap.add_argument("--idle-busy", type=float, default=0.5)
    ap.add_argument("--settle-quiet", type=float, default=1.5)
    ap.add_argument("--app-xy", type=lambda v: tuple(int(x) for x in v.split(",")), default=(60, 92))
    a = ap.parse_args()
    lab = Lab(a)

    def bye(*_):
        lab.kill()
        sys.exit(2)
    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)
    steps = a.plan.split(",")
    lab.fresh_disk()
    try:
        for i, s in enumerate(steps):
            r = lab.step(i, s, i == len(steps) - 1)
            if r["outcome"] not in ("guest-reset", "guest-shutdown"):
                lab.fresh_disk()          # unclean stop: start the next step from the seed again
    finally:
        lab.kill()
        try:
            lab.disk.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    main()
