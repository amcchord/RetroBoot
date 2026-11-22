#!/usr/bin/env python3
"""Quadra lab: a 68k Mac (quadra.py) from power-on to the Finder, then Restart or Shut Down.

Starts QEMU with hostd's own q800 arguments (``quadra.prepare`` + ``machines.build_argv``)
for a machine directory under a RetroBoot root (``ROOT/var/lib/retroboot/machines/ID``,
package ``ROOT/usr/lib/retroboot/qemu-m68k`` unless ``--pkg``), and for every step of
``--plan`` (one boot each):

* polls QMP screendumps until hostd's Finder rule (``finder.finder_on_screen``) matches,
  then waits ``--settle`` seconds;
* presses the ADB power key once, polls for Mac OS's dialog with hostd's own detector
  (``powerdlg.find_power_dialog`` against the frame before the key, then
  ``powerdlg.modal_change``), as Supervisor._watch_dialog does;
* answers it and waits for QMP ``SHUTDOWN``, recording its reason.  Steps: ``shutdown``
  (Return: what hostd sends, also for a 68k Mac's Restart, which hostd turns into Shut Down
  and a new start), ``restart`` (R), ``restart-cmdr`` (Command-R) and ``restart-click``
  (the dialog's leftmost button with the absolute pointer) — on 7.5.5 and 8.1 none of the
  three restarts (October 24 2026) — and ``none`` (stop at the Finder).

Results go to OUT/steps.jsonl; the Finder and dialog frames to OUT/*.png (Apple's pixels:
private).  ``--display none`` runs without a display backend (QMP screendumps only; e.g.
ClassicMac's macOS qemu-system-m68k, which has no ``-display retroboot``); the default
``retroboot`` needs a RetroBoot package and connects emulator/tools' presenter stub.

    quadra_lab.py --root ROOT --machine ID --out OUT --plan restart,shutdown [--display none]
        [--pkg DIR] [--size 800x600] [--ram 128] [--settle 3] [--share] [--builtin-fb]

Disks are booted in place: give it a copy (``cp -c``/``cp --reflink``), never a fixture.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve()
REPO = HERE.parents[3]
sys.path.insert(0, str(REPO / "hostd"))

from retroboot import finder, powerdlg, quadra  # noqa: E402
from retroboot.config import Paths  # noqa: E402
from retroboot.machines import LaunchOptions, MachineRegistry, build_argv  # noqa: E402
from retroboot.qmp import QMPClient, encode_png, read_ppm  # noqa: E402

ANSWER = {"restart": "r", "shutdown": "ret"}
EXPECT = {"restart": "guest-reset", "restart-click": "guest-reset", "restart-cmdr": "guest-reset",
          "shutdown": "guest-shutdown"}


def settings(a) -> dict:
    return {"machine.quadra_ram_mb": a.ram, "display.guest_size": a.size, "display.pixel_scale": "1",
            "audio.enabled": False, "share.enabled": a.share, "share.volume_name": "RetroBoot Share",
            "network.guest": "nat"}


class Lab:
    def __init__(self, a):
        self.a = a
        self.out = Path(a.out).resolve()
        self.out.mkdir(parents=True, exist_ok=True)
        self.paths = Paths(root=Path(a.root).resolve(),
                           qemu_m68k_package_override=Path(a.pkg).resolve() if a.pkg else None)
        self.paths.ensure()
        self.machine = MachineRegistry(self.paths).get(a.machine)
        if self.machine is None or not self.machine.quadra:
            sys.exit(f"{a.machine}: no 68k Mac of that id under {self.paths.machines}")
        self.res = (self.out / "steps.jsonl").open("a", buffering=1)
        self.stub = None
        if a.display == "retroboot":
            sys.path.insert(0, str(REPO / "emulator" / "tools"))
            import rbd_presenter_stub as stub
            self.stub = stub.Presenter(self.paths.display_sock, None)
            threading.Thread(target=self.stub.accept_loop, daemon=True).start()

    def argv(self) -> list[str]:
        prepared = quadra.prepare(self.machine, self.paths.qemu_m68k_package, quadra.RomStore(self.paths.rom_store))
        argv = build_argv(self.machine, settings(self.a), self.paths, None,
                          LaunchOptions(display_backend="retroboot" if self.a.display == "retroboot" else "vnc",
                                        audio_capable=False))
        if self.a.display == "none":   # no display backend at all (QMP screendumps only)
            i = argv.index("-display")
            argv[i:i + 4] = ["-display", "none"]
        if self.a.builtin_fb:
            # Smoke tests on emulators whose nubus-qfb hangs at the startup gray (ClassicMac's
            # macOS builds of October 2026): the Quadra's own framebuffer at the same size.
            argv[argv.index("q800,fb=none,audiodev=snd0")] = "q800,audiodev=snd0"
            i = next(n for n, v in enumerate(argv) if v.startswith("nubus-qfb,"))
            w, h = (argv[i].split("width=")[1].split(",")[0], argv[i].split("height=")[1].split(",")[0])
            argv[i - 1:i + 1] = ["-g", f"{w}x{h}x8"]
        if self.a.snapshot:
            argv.append("-snapshot")      # nothing written to the disk or the PRAM
        self.log("argv", prepared=prepared, argv=argv)
        return argv

    def log(self, event: str, **kw) -> None:
        rec = {"t": round(time.time(), 3), "event": event, **kw}
        self.res.write(json.dumps(rec) + "\n")
        print(json.dumps(rec)[:400], flush=True)

    async def frame(self, q: QMPClient, name: str = "probe"):
        p = self.out / f".{name}.ppm"
        try:
            await q.screendump(str(p), "ppm")
            return read_ppm(p.read_bytes())
        except Exception:  # noqa: BLE001
            return None
        finally:
            p.unlink(missing_ok=True)

    def save(self, frame, name: str) -> None:
        if frame:
            (self.out / f"{name}.png").write_bytes(encode_png(*frame))

    async def step(self, n: int, what: str) -> dict:
        argv = self.argv()
        for s in (self.paths.qmp_control, self.paths.qmp_events):
            s.unlink(missing_ok=True)
        err = open(self.out / f"qemu-{n}.log", "wb")
        t0 = time.monotonic()
        proc = subprocess.Popen(argv, stdout=err, stderr=subprocess.STDOUT, cwd=self.machine.dir,
                                env={**os.environ, "TZ": os.environ.get("TZ", ":UTC")})
        q = QMPClient(self.paths.qmp_control)
        out = {"step": n, "what": what}
        shutdown = asyncio.get_running_loop().create_future()

        def on_event(msg):
            if msg.get("event") == "SHUTDOWN" and not shutdown.done():
                shutdown.set_result(((msg.get("data") or {}).get("reason"), round(time.monotonic() - t0, 2)))
        try:
            await q.connect(timeout=20)
            q.event_handlers.append(on_event)
            out["qmp_s"] = round(time.monotonic() - t0, 2)
            finder_at = None
            frame = None
            while time.monotonic() - t0 < self.a.finder_timeout:
                frame = await self.frame(q)
                if frame and finder.finder_on_screen(*frame):
                    finder_at = round(time.monotonic() - t0, 2)
                    out["finder_rule"] = finder.finder_on_screen(*frame)
                    break
                await asyncio.sleep(0.5)
            out["finder_s"] = finder_at
            self.save(frame, f"step{n}-finder")
            if finder_at is None:
                out["result"] = "no-finder"
                return out
            await asyncio.sleep(self.a.settle)
            if what == "none":
                out["result"] = "finder"
                return out
            before = await self.frame(q)
            self.save(before, f"step{n}-before")
            await q.send_key("power", hold_ms=150)
            tk = time.monotonic()
            found = last = None
            while time.monotonic() - tk < 20 and found is None:
                await asyncio.sleep(0.3)
                cur = await self.frame(q)
                if cur is None:
                    continue
                res = powerdlg.find_power_dialog(before, cur)
                if res["found"] and last is not None and res["box"] == last["box"]:
                    found = dict(res, after_s=round(time.monotonic() - tk, 2), via=res.get("via"))
                    self.save(cur, f"step{n}-dialog")
                    break
                last = res if res["found"] else None
                mc = powerdlg.modal_change(before, cur)
                out.setdefault("modal_seen", bool(mc["modal"]))
                if mc["modal"]:
                    self.save(cur, f"step{n}-modal")
            out["dialog"] = {k: v for k, v in (found or {}).items() if k in ("box", "buttons", "menus_dimmed", "via",
                                                                             "after_s", "color")} or None
            if found is None:
                self.save(await self.frame(q), f"step{n}-nodialog")
                out["result"] = "no-dialog"
                return out
            await asyncio.sleep(0.3)
            if what == "restart-click":
                # Mac OS 7.5.5 and 8.1 take no R for Restart (the dialog closes): click the
                # dialog's leftmost push button with the absolute pointer instead.
                btn = powerdlg.button_boxes(cur, found["box"])
                out["buttons_at"] = btn
                x, y = (btn[0][0] + btn[0][2]) // 2, (btn[0][1] + btn[0][3]) // 2
                w, h = cur[0], cur[1]
                await q.execute("input-send-event", {"events": [
                    {"type": "abs", "data": {"axis": "x", "value": x * 0x7FFF // (w - 1)}},
                    {"type": "abs", "data": {"axis": "y", "value": y * 0x7FFF // (h - 1)}}]})
                await asyncio.sleep(0.2)
                for down in (True, False):
                    await q.execute("input-send-event", {"events": [
                        {"type": "btn", "data": {"down": down, "button": "left"}}]})
                    await asyncio.sleep(0.15)
            elif what == "restart-cmdr":
                await q.send_key("meta_l", "r", hold_ms=150)
            else:
                await q.send_key(ANSWER[what], hold_ms=100)
            ta = time.monotonic()
            try:
                reason, at = await asyncio.wait_for(shutdown, 60)
                out.update(shutdown_reason=reason, shutdown_after_answer_s=round(time.monotonic() - ta, 2))
                out["result"] = "ok" if reason == EXPECT[what] else "unexpected"
            except asyncio.TimeoutError:
                self.save(await self.frame(q), f"step{n}-stuck")
                out["result"] = "no-shutdown"
            return out
        finally:
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            out["exit_code"] = proc.returncode
            await q.close()
            err.close()
            self.log("step", **out)

    async def run(self) -> int:
        bad = 0
        for n, what in enumerate(self.a.plan.split(","), 1):
            r = await self.step(n, what)
            bad += r.get("result") not in ("ok", "finder")
        return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", required=True)
    ap.add_argument("--machine", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--plan", default="restart,shutdown")
    ap.add_argument("--pkg")
    ap.add_argument("--display", choices=("retroboot", "none"), default="retroboot")
    ap.add_argument("--size", default="800x600")
    ap.add_argument("--ram", type=int, default=128)
    ap.add_argument("--share", action="store_true")
    ap.add_argument("--snapshot", action="store_true", help="QEMU -snapshot: leave the disk and PRAM as they are")
    ap.add_argument("--builtin-fb", action="store_true",
                    help="the Quadra's own framebuffer instead of nubus-qfb (smoke tests on older emulators)")
    ap.add_argument("--settle", type=float, default=3.0)
    ap.add_argument("--finder-timeout", type=float, default=120.0)
    return asyncio.run(Lab(ap.parse_args()).run())


if __name__ == "__main__":
    sys.exit(main())
