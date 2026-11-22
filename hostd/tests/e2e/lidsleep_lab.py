#!/usr/bin/env python3
"""Lid-sleep lab: does Mac OS 9's power-key dialog take S (Sleep) and Escape (Cancel)?

Boots a disposable copy of a Mac OS disk on the PMU machine with hostd's own QEMU
arguments (``machines.build_argv`` through ``powerkey_lab.Lab``: the battery bridge
socket, as on a laptop or the Gemini PDA), behind the presenter stub, and plays a
minimal power bridge (hostd's side of ``power.sock``: hello, a battery report, and
``{"type": "wake"}`` on request).  Once the Finder has settled:

1. cancel: the power key, the dialog verified (``powerdlg.find_power_dialog``, the
   same box in two frames), Escape; the dialog must disappear and the screen return
   to the frame before the key;
2. sleep: the power key again once idle, the dialog verified, S; the bridge must say
   ``sleep`` (QEMU's PMU sleep command);
3. wake: ``{"type": "wake"}`` on the bridge; it must say ``wake`` and the Finder must
   come back;
4. shut down: the power key, the dialog, Return; QEMU must exit with guest-shutdown.

    lidsleep_lab.py --out RUN --seed DISK [--rounds N] [--pkg DIR] [--ndrv DIR] [--loader DIR]

Lab tool for the Linux builder (root).  Frames stay in RUN (private: Apple's pixels).
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parent))

import powerkey_lab as pkl  # noqa: E402
from powerkey_lab import rgb_frame, save, stub  # noqa: E402
from retroboot import powerdlg  # noqa: E402
from retroboot.battery import guest_battery  # noqa: E402
from retroboot.qmp import finder_menu_bar  # noqa: E402


class Bridge:
    """hostd's side of the power bridge, just enough for QEMU's PMU."""

    def __init__(self, path: Path):
        self.path = path
        path.unlink(missing_ok=True)
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(str(path))
        self.srv.listen(2)
        self.conn = None
        self.msgs: list[tuple[float, dict]] = []
        self.lock = threading.Lock()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            self.conn = c
            self.send({"type": "hello", "version": 1})
            self.send(guest_battery({"ac": False, "battery": {"percent": 80, "status": "Discharging",
                                                              "time_to_empty_min": 300}, "batteries": 1}))
            threading.Thread(target=self._read, args=(c,), daemon=True).start()

    def _read(self, c):
        buf = b""
        while True:
            try:
                d = c.recv(4096)
            except OSError:
                return
            if not d:
                return
            buf += d
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    m = json.loads(line)
                except ValueError:
                    continue
                with self.lock:
                    self.msgs.append((time.monotonic(), m))

    def send(self, obj):
        if self.conn is not None:
            self.conn.sendall(json.dumps(obj).encode() + b"\n")

    def wait(self, kind: str, after: float, timeout: float):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self.lock:
                for t, m in self.msgs:
                    if t >= after and m.get("type") == kind:
                        return t, m
            time.sleep(0.1)
        return None


class LidLab(pkl.Lab):
    def frame(self):
        return rgb_frame(self.p)

    def wait_idle(self, quiet_s: float = 2.0, timeout: float = 60.0) -> bool:
        prev, since = None, None
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            now = time.monotonic()
            self.load.sample(now)
            cur = self.frame()
            busy = self.load.busy(now)
            still = prev is not None and powerdlg.screen_changed(prev, cur) == 0.0
            prev = cur
            since = (since or now) if still and (busy is None or busy < self.a.idle_busy) else None
            if since and now - since >= quiet_s:
                return True
            time.sleep(0.2)
        return False

    def dialog(self, before, timeout: float = 20.0):
        """The power dialog verified on screen (same box in two consecutive frames)."""
        last = None
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            time.sleep(0.3)
            cur = self.frame()
            res = powerdlg.find_power_dialog(before, cur)
            if res["found"] and res["box"] == last:
                return res, cur
            last = res["box"] if res["found"] else None
        return None, None

    def round(self, n: int, rdir: Path) -> dict:
        r = {"round": n}
        rdir.mkdir(exist_ok=True)
        # 1. cancel with Escape
        if not self.wait_idle():
            r["error"] = "not idle before cancel"
            return r
        before = self.frame()
        self.key("power", 150)
        t_key = time.monotonic()
        res, dlg = self.dialog(before)
        r["cancel_dialog"] = None if res is None else {"after_s": round(time.monotonic() - t_key, 2),
                                                        "buttons": res["buttons"], "box": res["box"]}
        if res is None:
            r["error"] = "no dialog for the cancel check"
            return r
        save(dlg, rdir / "dialog-cancel.png")
        time.sleep(0.3)
        self.key("esc", 100)
        time.sleep(1.5)
        after = self.frame()
        gone = not powerdlg.find_power_dialog(before, after)["found"]
        r["cancel"] = {"dialog_gone": gone, "changed_vs_before": round(powerdlg.screen_changed(before, after), 4)}
        save(after, rdir / "after-cancel.png")
        # 2. sleep with S
        if not self.wait_idle():
            r["error"] = "not idle before sleep"
            return r
        before = self.frame()
        self.key("power", 150)
        t_key = time.monotonic()
        res, dlg = self.dialog(before)
        if res is None:
            r["error"] = "no dialog for the sleep check"
            return r
        r["sleep_dialog"] = {"after_s": round(time.monotonic() - t_key, 2), "buttons": res["buttons"]}
        save(dlg, rdir / "dialog-sleep.png")
        time.sleep(0.3)
        t_s = time.monotonic()
        self.key("s", 100)
        got = self.bridge.wait("sleep", t_s, 20.0)
        r["sleep"] = {"bridge_sleep_after_s": None if got is None else round(got[0] - t_s, 2)}
        time.sleep(1.0)
        save(self.frame(), rdir / "asleep.png")
        if got is None:
            r["error"] = "no sleep message"
            return r
        # 3. wake through the bridge
        time.sleep(3.0)
        t_w = time.monotonic()
        self.bridge.send({"type": "wake"})
        got = self.bridge.wait("wake", t_w, 15.0)
        r["wake"] = {"bridge_wake_after_s": None if got is None else round(got[0] - t_w, 2),
                     "reason": None if got is None else got[1].get("reason")}
        settled = self.wait_idle(quiet_s=2.0, timeout=40.0)
        cur = self.frame()
        save(cur, rdir / "woken.png")
        r["wake"].update(settled=settled, finder=bool(finder_menu_bar(cur[0], cur[2])),
                         changed_vs_before_sleep=round(powerdlg.screen_changed(before, cur), 4))
        return r

    def run_all(self) -> list:
        self.bridge = Bridge(self.run / "power.sock")
        out = []
        self.fresh_disk()
        t0 = self.start(0)
        # the Finder, then its startup work
        deadline = t0 + self.a.timeout
        finder = None
        while time.monotonic() < deadline and self.proc.poll() is None:
            try:
                cur = self.frame()
            except RuntimeError:
                time.sleep(0.3)
                continue
            if finder_menu_bar(cur[0], cur[2]):
                finder = time.monotonic() - t0
                break
            time.sleep(0.3)
        hello = self.bridge.wait("hello", 0, 1.0)
        out.append({"finder_s": None if finder is None else round(finder, 1),
                    "bridge_hello": None if hello is None else hello[1]})
        if finder is None:
            return out
        for n in range(self.a.rounds):
            r = self.round(n, self.run / f"round{n}")
            out.append(r)
            print(json.dumps(r), flush=True)
            if r.get("error"):
                break
        # 4. shut down through the dialog
        if self.wait_idle():
            before = self.frame()
            self.key("power", 150)
            res, _ = self.dialog(before)
            if res is not None:
                time.sleep(0.3)
                self.key("ret", 100)
                try:
                    self.proc.wait(90)
                    out.append({"shutdown": self.shutdown_reason()})
                except Exception:
                    out.append({"shutdown": "no-exit"})
        return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", required=True)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--pkg", default="/srv/retroboot/wave2/artifacts/qemu/linux-ppc-r18")
    ap.add_argument("--ndrv", default="/srv/retroboot/wave2/artifacts/ndrv-r18")
    ap.add_argument("--loader", default="/srv/retroboot/wave2/artifacts/classicvirtio-6")
    ap.add_argument("--size", type=pkl.parse_size, default=(1024, 768))
    ap.add_argument("--timeout", type=float, default=240)
    ap.add_argument("--idle-busy", type=float, default=0.5)
    a = ap.parse_args()
    a.card, a.pmu = "std", True
    lab = LidLab(a)
    try:
        res = lab.run_all()
        (lab.run / "results.json").write_text(json.dumps(res, indent=1))
        print(json.dumps(res, indent=1))
    finally:
        lab.kill()
        try:
            lab.disk.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    main()
