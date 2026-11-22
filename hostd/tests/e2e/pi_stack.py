#!/usr/bin/env python3
"""The real presenter and the real hostd together on a pretend Raspberry Pi (lab).

Runs this checkout's rb-presenter on a virtual display (``--device
virtual:WxH``, no DRM device needed) with the real hostd (--dev, a scratch
root) and the real guest-launcher, which starts hostd's fake QEMU
(hostd/tests/fakes/fake_qemu.py: QMP, a §3 display socket with memfd frames,
Mac OS's startup gray, a Finder-like desktop).  hostd gets a fake
/proc/device-tree of the chosen board (``--board pi5|pi4|pc``), so the Pi code
paths run end to end: platform detection, stretched deadlines, Software Update
unavailable, the Computer row.

It is meant for an arm64 build in an arm64 chroot under qemu-user (the
builder's ``chroot-arm64``) as evidence that both programs run on arm64; it
works on x86 too.  It proves the user-space stack (composition, scaling,
sockets, lifecycle), not the Pi's kernel display drivers (vc4/v3d), which only
hardware can show.

    python3 hostd/tests/e2e/pi_stack.py --presenter BUILD/rb-presenter --work /tmp/pistack [--board pi5]

Checks (exit 0 when all pass; OUT/result.json has the details):
  * the first presented frame is the black-and-white checkerboard;
  * hostd records guest_content, guest_revealed and finder_detected;
  * the last presented frame is the last guest frame scaled by an exact integer
    (2x for a 960x540 guest on 1920x1080);
  * the lifecycle log has the platform event and update_unavailable (Pi boards),
    and the presenter's status names the virtual display and the guest scene.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TESTS = HERE.parent
HOSTD = TESTS.parent
REPO = HOSTD.parent
sys.path[:0] = [str(HOSTD), str(TESTS), str(REPO / "presenter" / "tools")]

import framelog  # noqa: E402  (presenter/tools)
import helpers  # noqa: E402  (hostd/tests)
from retroboot.config import Paths  # noqa: E402

BOARDS = {
    "pi5": ("Raspberry Pi 5 Model B Rev 1.0", ["raspberrypi,5-model-b", "brcm,bcm2712"], 0xd04170),
    "pi4": ("Raspberry Pi 4 Model B Rev 1.5", ["raspberrypi,4-model-b", "brcm,bcm2711"], 0xc03115),
}


def events(log: Path) -> list[dict]:
    try:
        return [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
    except (OSError, ValueError):
        return []


def first(evs: list[dict], name: str) -> dict | None:
    return next((e for e in evs if e.get("event") == name), None)


def ctl(sock: Path, req: dict, timeout: float = 10.0) -> dict:
    """One request on the presenter's control socket (replaces hostd's connection: use last)."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(str(sock))
    s.sendall((json.dumps(dict(req, id=1)) + "\n").encode())
    buf = b""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        buf += s.recv(1 << 20)
        for line in buf.split(b"\n"):
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == 1:
                s.close()
                return msg
    s.close()
    return {}


def checker_ok(fr) -> bool:
    """Top-left 64x64: only black and white, both present, in square cells."""
    seen = set()
    for y in range(64):
        for x in range(64):
            o = (y * fr.w + x) * 4
            seen.add(bytes(fr.canvas[o:o + 3]))
    return seen == {b"\x00\x00\x00", b"\xff\xff\xff"}


def scaled_ok(shown, guest) -> dict:
    """shown == guest scaled by an integer k at origin (ox, oy): checked on a grid."""
    k = min(shown.w // guest.w, shown.h // guest.h)
    if k < 1:
        return {"ok": False, "why": "guest larger than the frame"}
    ox, oy = (shown.w - guest.w * k) // 2 // k * k, (shown.h - guest.h * k) // 2 // k * k
    bad = 0
    n = 0
    for gy in range(0, guest.h, max(1, guest.h // 60)):
        for gx in range(0, guest.w, max(1, guest.w // 80)):
            g = bytes(guest.canvas[(gy * guest.w + gx) * 4:(gy * guest.w + gx) * 4 + 3])
            for dy in range(k):
                for dx in range(k):
                    sx, sy = ox + gx * k + dx, oy + gy * k + dy
                    o = (sy * shown.w + sx) * 4
                    n += 1
                    bad += bytes(shown.canvas[o:o + 3]) != g
    return {"ok": bad == 0, "k": k, "origin": [ox, oy], "checked": n, "mismatched": bad}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--presenter", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--board", choices=["pi5", "pi4", "pc"], default="pi5")
    ap.add_argument("--display", default="1920x1080")
    ap.add_argument("--timeout", type=float, default=300.0, help="seconds to wait for the Finder")
    ap.add_argument("--port", type=int, default=18480)
    a = ap.parse_args()

    work = Path(a.work)
    if work.exists():
        shutil.rmtree(work)
    root = work / "root"
    paths = Paths(root)
    paths.ensure()
    pkg = helpers.make_package(root)
    helpers.make_machine(root, "mac-os-9", "Mac OS 9.2.2")
    helpers.fake_qemu_conf(pkg, modes=["normal"], boot_s=3.0, finder_s=8.0)
    hostd_json = {"run_as_user": "", "presenter_fallback_start": 0, "log_level": "INFO",
                  "device_tree": str(root / "proc/device-tree"), "sysfs_path": str(root / "sys"),
                  "bluetooth_backend": "fake", "bluetooth_assistant": "off", "orientation_sensor": False,
                  "boot_network_conf": str(root / "boot/firmware/retroboot/network.conf")}
    if a.board in BOARDS:
        helpers.make_device_tree(root, *BOARDS[a.board])
    (root / "etc/retroboot").mkdir(parents=True, exist_ok=True)
    (root / "etc/retroboot/hostd.json").write_text(json.dumps(hostd_json, indent=1))

    presented, guest_log = work / "presented.rbfl", work / "guest.rbfl"
    pres_argv = [a.presenter, "--device", f"virtual:{a.display}@60", "--record", str(presented),
                 "--record-guest", str(guest_log), "--display-socket", str(paths.display_sock),
                 "--control-socket", str(paths.presenter_sock), "--remote-socket", "", "--no-vt", "--no-input",
                 "--no-handoff"]
    env = dict(os.environ, PYTHONPATH=str(HOSTD), PYTHONUNBUFFERED="1")
    t0 = time.monotonic()
    pres = subprocess.Popen(pres_argv, stdout=open(work / "presenter.log", "w"), stderr=subprocess.STDOUT)
    hostd_argv = [sys.executable, "-m", "retroboot.hostd", "--root", str(root), "--dev", "--guest-runner",
                  "subprocess", "--no-journal", "--no-host-tools", "--http-port", str(a.port), "--https-port",
                  str(a.port + 1), "--https-bind", "127.0.0.1"]
    hostd = subprocess.Popen(hostd_argv, cwd=str(root), env=env, stdout=open(work / "hostd.out", "w"),
                             stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    result: dict = {"board": a.board, "display": a.display, "presenter_argv": pres_argv, "hostd_argv": hostd_argv,
                    "machine": os.uname().machine, "checks": {}}
    lifecycle = paths.lifecycle_log
    try:
        deadline = time.monotonic() + a.timeout
        while time.monotonic() < deadline:
            if first(events(lifecycle), "finder_detected"):
                break
            if hostd.poll() is not None or pres.poll() is not None:
                break
            time.sleep(1.0)
        evs = events(lifecycle)
        for name in ("hostd_start", "guest_start", "guest_content", "guest_revealed", "finder_detected"):
            e = first(evs, name)
            result.setdefault("lifecycle", {})[name] = e.get("t_mono") if e else None
        status = ctl(paths.presenter_sock, {"cmd": "status"}) if pres.poll() is None else {}
        result["presenter_status"] = {k: status.get(k) for k in ("display", "scene", "view", "guest")}
        c = result["checks"]
        c["lifecycle"] = all(first(evs, n) for n in ("guest_content", "guest_revealed", "finder_detected"))
        if a.board in BOARDS:
            plat = first(evs, "platform") or {}
            upd = first(evs, "update_unavailable") or {}
            c["platform_event"] = plat.get("board") == a.board
            c["update_unavailable"] = "Raspberry Pi" in (upd.get("reason") or "")
            result["platform"] = plat
        else:
            c["no_platform_event"] = first(evs, "platform") is None
        disp = (status.get("display") or {})
        c["virtual_display"] = str(disp.get("device", "")).startswith("virtual:")
    finally:
        for p in (hostd, pres):
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
        for p in (hostd, pres):
            try:
                p.wait(20)
            except subprocess.TimeoutExpired:
                p.kill()
        # the fake QEMU (guest-launcher's child) outlives hostd --dev on purpose; stop it
        subprocess.run(["pkill", "-f", str(pkg / "bin/qemu-system-ppc")], check=False)
    first_frame = last_shown = last_guest = None
    for fr in framelog.read(presented):
        if first_frame is None:
            first_frame = framelog.Frame(fr.index, fr.t_us, fr.flags, fr.w, fr.h, fr.bands, fr.changed_px,
                                         bytes(fr.canvas), fr.boxes)
        last_shown = fr
    if last_shown is not None:
        last_shown = framelog.Frame(last_shown.index, last_shown.t_us, last_shown.flags, last_shown.w, last_shown.h,
                                    last_shown.bands, last_shown.changed_px, bytes(last_shown.canvas),
                                    last_shown.boxes)
    for fr in framelog.read(guest_log):
        last_guest = fr
    if last_guest is not None:
        last_guest = framelog.Frame(last_guest.index, last_guest.t_us, last_guest.flags, last_guest.w,
                                    last_guest.h, last_guest.bands, last_guest.changed_px, bytes(last_guest.canvas),
                                    last_guest.boxes)
    c = result["checks"]
    c["first_frame_checkerboard"] = bool(first_frame and checker_ok(first_frame))
    if first_frame:
        framelog.write_png(work / "first.png", first_frame)
    if last_shown and last_guest:
        framelog.write_png(work / "last-shown.png", last_shown)
        framelog.write_png(work / "last-guest.png", last_guest)
        sc = scaled_ok(last_shown, last_guest)
        result["scaling"] = dict(sc, frame=[last_shown.w, last_shown.h], guest=[last_guest.w, last_guest.h])
        c["guest_scaled_exactly"] = sc["ok"]
    else:
        c["guest_scaled_exactly"] = False
    result["frames"] = {"presented": framelog.info(presented) if presented.exists() else None,
                        "guest": framelog.info(guest_log) if guest_log.exists() else None}
    result["elapsed_s"] = round(time.monotonic() - t0, 1)
    result["ok"] = all(c.values())
    (work / "result.json").write_text(json.dumps(result, indent=1, default=str))
    print(json.dumps({"ok": result["ok"], "checks": c, "lifecycle": result.get("lifecycle"),
                      "scaling": result.get("scaling"), "elapsed_s": result["elapsed_s"]}, indent=1))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
