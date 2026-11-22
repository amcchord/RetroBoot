#!/usr/bin/env python3
"""Lab driver: real hostd + the real rb-presenter on a virtual display + real QEMU, for
documentation captures of the Screen page's stream and of the computer's own screen.

share_real.py runs hostd's fake presenter; this runs rb-presenter itself (``--device
virtual:WxH``: no DRM device, so it works on the builder) with the Platinum bundle built
from the disk copy (RBUI_ASSETS, as on a device), and acts as the Screen page on the
presenter's remote socket with hostd's own tap client (retroboot/remote/tap.py):

1. at the Finder, a remote viewer with control connects (the presenter then shows its
   indicator in the Mac OS menu bar on the computer's own screen);
2. the key bar's **Recovery…** combination (hostd's keymap: the configured recovery
   chord, pressed in order and released in reverse, as RemoteHub._combo sends it);
3. the streamed picture with RetroBoot Recovery over the dimmed Mac.

The stream's tiles are lossless (remote/encode.py), so the saved tap pictures are the
pixels the Screen page draws at Actual Size.  The virtual display records every frame it
shows (``--record``), so the computer's own screen is exact too.

    screen_real.py run --work W --disk COPY.img --qemu-package PKG --presenter BIN
                       [--ndrv DIR] [--classicvirtio DIR] [--guestprep LIB] [--platinum LIB]
                       [--machine "Mac OS 9.2.2" --fixture os922-full-v5]
                       [--display 1024x768] [--settle 20]

Writes W/evidence/: presented.rbfl, steps.jsonl (CLOCK_MONOTONIC times of each step),
stream-finder.png, stream-recovery.png, outer-finder.png, outer-recovery.png (the frame
on screen at those times), the lifecycle log, presenter and hostd logs.  Run as root on
the lab builder.  The disk is MOVED into W (copy it with cp --sparse=always first, never
a fixture); it is deleted at the end unless --keep-disk.  Every child (hostd, the
presenter, QEMU) is stopped on every exit path.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import real_qemu  # noqa: E402

sys.path.insert(0, str(real_qemu.SRC))
from retroboot.remote import keymap, tap  # noqa: E402

REPO = real_qemu.SRC.parent
PROCS: list[subprocess.Popen] = []


def step(ev: Path, name: str, **kw) -> dict:
    rec = {"step": name, "mono": round(time.monotonic(), 6), **kw}
    with open(ev / "steps.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")
    print(json.dumps(rec)[:300], flush=True)
    return rec


def spawn(argv, log: Path, env=None) -> subprocess.Popen:
    p = subprocess.Popen(argv, stdout=open(log, "ab"), stderr=subprocess.STDOUT, env=env, start_new_session=True)
    PROCS.append(p)
    return p


def stop_all(root: Path) -> list[str]:
    for p in reversed(PROCS):          # hostd first (it stops its launch), then the presenter
        if p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                p.wait(30)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait()
    left = subprocess.run(["pgrep", "-f", f"^{root}/usr/lib/retroboot/qemu/bin/qemu-system-ppc"],
                          capture_output=True, text=True).stdout.split()
    for pid in left:
        try:
            os.kill(int(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
    return left


def save_tap(s: tap.Surface, path: Path) -> dict:
    from PIL import Image
    img = Image.frombuffer("RGB", (s.w, s.h), bytes(s.buf[: s.stride * s.h]), "raw", "BGRX", s.stride, 1)
    img.save(path)
    return {"file": path.name, "kind": s.kind, "view": s.view, "size": [s.w, s.h]}


async def stream_steps(run_dir: Path, ev: Path, chord: str) -> None:
    surfaces: list[tap.Surface] = []
    t = tap.TapClient(str(run_dir / "remote.sock"), surfaces.append, lambda: None)
    await t.connect("composed")
    try:
        t.viewers(1, 1)                                  # a browser watching, with control
        await asyncio.sleep(3.0)
        await t.update(0)
        step(ev, "stream-finder", **save_tap(t.surface, ev / "stream-finder.png"))
        keys = keymap.combo_keys("recovery", chord)
        step(ev, "recovery-combo", keys=keys)
        for code in keys:                                # RemoteHub._combo
            t.key(code, True)
            await asyncio.sleep(0.03)
        await asyncio.sleep(0.08)
        for code in reversed(keys):
            t.key(code, False)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            await t.update(500)
            if t.surface and t.surface.view == "panel":
                break
        await asyncio.sleep(2.5)                         # let the panel finish drawing
        for _ in range(3):
            await t.update(200)
        step(ev, "stream-recovery", **save_tap(t.surface, ev / "stream-recovery.png"))
        t.key(keymap.EVDEV["ESC"], True)                 # Return to Mac OS is the default: leave by Escape
        await asyncio.sleep(0.05)
        t.key(keymap.EVDEV["ESC"], False)
        await asyncio.sleep(2.0)
        t.viewers(0, 0)
        await asyncio.sleep(1.0)
    finally:
        t.close("done")


def run(a) -> None:
    work = Path(a.work).resolve()
    w, h = (int(v) for v in a.display.split("x"))
    r = real_qemu.Run(SimpleNamespace(
        work=str(work), disk=a.disk, qemu_package=a.qemu_package, guestprep=a.guestprep, web=None, run_as="",
        https_port=a.https_port, http_port=a.http_port, display_mode=a.display, handoff_deadline=60.0,
        mark_prepared=False, scenario="full", cycles=0, ndrv=a.ndrv, loader=a.classicvirtio))
    r.setup()
    ev, root = r.ev, r.root
    mdir = root / "var/lib/retroboot/machines/mac-os-9-2-1"
    meta = json.loads((mdir / "machine.json").read_text())
    meta.update(name=a.machine, os_version=a.machine)
    if a.fixture:
        meta["source"] = {"type": "fixture-copy", "fixture_id": a.fixture}
    (mdir / "machine.json").write_text(json.dumps(meta))
    cfg_path = root / "etc/retroboot/hostd.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["platinum_path"] = a.platinum
    cfg["http_bind"] = "127.0.0.1"
    cfg_path.write_text(json.dumps(cfg, indent=2))
    # The Platinum bundle from this disk (hostd would build the same one before the launch;
    # the presenter loads it once at start, as on a device that has one).
    assets = root / "var/lib/retroboot/assets/platinum"
    sys.path.insert(0, a.platinum)
    from rbplatinum import bundle
    t0 = time.monotonic()
    man = bundle.build(str(mdir / "disk.img"), str(assets), True)
    step(ev, "platinum", seconds=round(time.monotonic() - t0, 1), missing=man.get("missing", []),
         generator=man.get("generator"))
    run_dir = root / "run/retroboot"
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        pres = [a.presenter, "--device", f"virtual:{w}x{h}@60", "--record", str(ev / "presented.rbfl"),
                "--display-socket", str(run_dir / "display.sock"), "--control-socket", str(run_dir / "presenter.sock"),
                "--remote-socket", str(run_dir / "remote.sock"), "--socket-group", "root",
                "--no-vt", "--no-input", "--no-handoff"]
        spawn(pres, ev / "presenter.log", env=dict(os.environ, RBUI_ASSETS=str(assets)))
        r.wait(lambda: (run_dir / "presenter.sock").exists(), 10, "presenter control socket")
        step(ev, "presenter", argv=pres)
        spawn([sys.executable, "-m", "retroboot.hostd", "--root", str(root)], ev / "hostd.log",
              env=dict(os.environ, PYTHONPATH=str(real_qemu.SRC)))
        r.wait_event("presenter_display", 1, 60)
        f = r.wait_event("finder_detected", 1, 400)
        step(ev, "finder", t_since_start=f.get("t_since_start"))
        time.sleep(a.settle)
        asyncio.run(stream_steps(run_dir, ev, "ctrl+alt+delete"))     # the default input.recovery_chord
    finally:
        left = stop_all(root)
        step(ev, "stopped", killed_qemu=left)
        shutil.copy2(r.lifecycle, ev / "lifecycle.jsonl") if r.lifecycle.exists() else None
        if not a.keep_disk:
            for d in (root / "var/lib/retroboot/machines").glob("*/disk.img"):
                d.unlink()
    steps = {json.loads(l)["step"]: json.loads(l) for l in (ev / "steps.jsonl").read_text().splitlines()}
    fl = REPO / "presenter/tools/framelog.py"
    for name in ("finder", "recovery"):
        s = steps.get(f"stream-{name}")
        if s:
            subprocess.run([sys.executable, str(fl), "at", str(ev / "presented.rbfl"), str(s["mono"]),
                            str(ev / f"outer-{name}.png")], check=True)
    print(json.dumps({"evidence": str(ev)}))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["run"])
    ap.add_argument("--work", required=True)
    ap.add_argument("--disk", required=True)
    ap.add_argument("--qemu-package", required=True)
    ap.add_argument("--presenter", required=True)
    ap.add_argument("--ndrv")
    ap.add_argument("--classicvirtio")
    ap.add_argument("--guestprep", default=str(REPO / "guestprep"))
    ap.add_argument("--platinum", default=str(REPO / "platinum"))
    ap.add_argument("--machine", default="Mac OS 9.2.2")
    ap.add_argument("--fixture")
    ap.add_argument("--display", default="1024x768")
    ap.add_argument("--settle", type=float, default=20.0)
    ap.add_argument("--https-port", type=int, default=18543)
    ap.add_argument("--http-port", type=int, default=18190)
    ap.add_argument("--keep-disk", action="store_true")
    a = ap.parse_args()
    run(a)


if __name__ == "__main__":
    main()
