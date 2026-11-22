#!/usr/bin/env python3
"""Startup and guest mode-switch lab for the presenter (ARCHITECTURE 4.4.1, 4.9).

Runs the real rb-presenter on a virtual display (``--device virtual:WxH``, no
DRM device needed, so it works on the builder) against a real Mac OS 9 guest
started with hostd's own QEMU arguments (hostd/retroboot/machines.build_argv),
acts as hostd on the presenter's control socket (startup indicator, display
config, scenes) and keeps both frame logs (presenter/tools/framelog.py):

  OUT/presented.rbfl   every frame the presenter showed, at the refresh it showed
  OUT/guest.rbfl       every guest frame as it arrived (Mac OS's own pixels)
  OUT/events.jsonl     presenter events and the lab's steps (CLOCK_MONOTONIC)
  OUT/result.json      launch time, argv, presenter status/timeline at the end
  OUT/remote.jsonl     with --tap: the remote screen's surfaces and pictures (SHA-256 of each)

    ati_lab.py run OUT --presenter BIN (--disk BASE --pkg DIR [--ndrv DIR] [--loader DIR] | --client CMD)
        [--card ati|std] [--display 1080x720] [--pixel-scale auto|1..4] [--startup WxH] [--switch-to WxH]
        [--art FILE.rbi] [--happy-mac auto|prompt|late|omit] [--seconds 55] [--rtc 2026-10-12T09:00:00]

``--card ati`` is hostd's ATI Radeon launch (-g 640x480, -prom-env
retroboot-display=<the policy size>); the presenter gets ``guest_size
[640, 480]`` as hostd sends it.  ``--card std --startup 640x480 --switch-to
800x600`` starts the standard card at 640 x 480 with RetroBoot Display's
option, so Mac OS switches the standard card itself (a guest-initiated
switch).  ``--happy-mac`` is sent in ``startup_config`` as hostd does (auto:
"late" with the ATI Radeon, "prompt" with the standard card; omit: as an
older hostd).  ``--pixel-scale`` is the display.pixel_scale setting (hostd's
policy and the presenter's display_config; for example 1 on a 1920x1080
display gives the card 1920 x 1080).  BASE is copied sparsely and never written.  Every child
(QEMU, the presenter) is stopped on every exit path.  Lab tool (root, builder).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve()
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO / "hostd"))
sys.path.insert(0, str(REPO / "guestprep"))

CHILDREN: list[subprocess.Popen] = []


def kill_children():
    for p in CHILDREN:
        if p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except OSError:
                pass
    deadline = time.monotonic() + 5
    for p in CHILDREN:
        try:
            p.wait(max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
            p.wait()


class Control:
    """hostd's end of the presenter control socket (newline-delimited JSON)."""

    def __init__(self, path: Path, events):
        self.events = events
        deadline = time.monotonic() + 10
        while True:
            try:
                self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.s.connect(str(path))
                break
            except OSError:
                self.s.close()
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)
        self.f = self.s.makefile("rw", buffering=1)
        self.lock = threading.Condition()
        self.replies: dict[int, dict] = {}
        self.seen: list[dict] = []
        self.next_id = 1
        threading.Thread(target=self.reader, daemon=True).start()

    def reader(self):
        for line in self.f:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            now = time.monotonic()
            if "id" in msg:
                with self.lock:
                    self.replies[msg["id"]] = msg
                    self.lock.notify_all()
                continue
            self.events.write(json.dumps({"mono": round(now, 6), "presenter": msg}) + "\n")
            with self.lock:
                self.seen.append({"mono": now, **msg})
                self.lock.notify_all()

    def request(self, cmd: str, timeout: float = 10.0, **kw) -> dict:
        with self.lock:
            rid = self.next_id
            self.next_id += 1
        self.f.write(json.dumps({"id": rid, "cmd": cmd, **kw}) + "\n")
        self.f.flush()
        deadline = time.monotonic() + timeout
        with self.lock:
            while rid not in self.replies:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError(cmd)
                self.lock.wait(left)
            r = self.replies.pop(rid)
        self.events.write(json.dumps({"mono": round(time.monotonic(), 6), "request": cmd, "args": kw,
                                      "ok": r.get("ok"), "error": r.get("error")}) + "\n")
        return r

    def wait_event(self, name: str, timeout: float, pred=lambda e: True):
        deadline = time.monotonic() + timeout
        with self.lock:
            while True:
                for e in self.seen:
                    if e.get("event") == name and pred(e):
                        return e
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self.lock.wait(left)


class RemoteTap:
    """hostd's end of the presenter's remote screen socket (ARCHITECTURE 4.6), as the web
    Screen page uses it: one REQUEST at a time, and after every UPDATE the SHA-256 of the
    whole tap buffer, i.e. the picture a browser would show (OUT/remote.jsonl)."""

    HELLO, REQUEST, SURFACE, UPDATE = 1, 2, 4, 5

    def __init__(self, path: Path, out: Path):
        import mmap as _mmap
        self._mmap = _mmap
        deadline = time.monotonic() + 10
        while True:
            try:
                self.s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
                self.s.connect(str(path))
                break
            except OSError:
                self.s.close()
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)
        self.f = out.open("w", buffering=1)
        self.records: list[dict] = []
        self.stop = threading.Event()
        self._send(self.HELLO, 1, 0)                  # version 1, source composed
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def _send(self, typ, *vals):
        self.s.send(struct.pack("=II", typ, 4 * len(vals)) + struct.pack(f"={len(vals)}I", *vals))

    def _note(self, rec):
        rec["mono"] = round(time.monotonic(), 6)
        self.records.append(rec)
        self.f.write(json.dumps(rec) + "\n")

    def run(self):
        import hashlib
        mm, w, h, gen, seq = None, 0, 0, 0, 1
        self.s.settimeout(0.5)
        self._send(self.REQUEST, seq, 200)
        while not self.stop.is_set():
            try:
                data, anc, _flags, _addr = self.s.recvmsg(65536, socket.CMSG_SPACE(4))
            except socket.timeout:
                continue
            except OSError:
                break
            if len(data) < 8:
                break
            typ = struct.unpack_from("=I", data)[0]
            if typ == self.SURFACE:
                g, kind, sw, sh, _stride, _fmt, size = struct.unpack_from("=6IQ", data, 8)
                view = data[8 + 32:8 + 48].split(b"\0", 1)[0].decode()
                if mm is not None:
                    mm.close()
                    mm = None
                for lvl, t, fd_bytes in anc:
                    if lvl == socket.SOL_SOCKET and t == socket.SCM_RIGHTS:
                        fd = struct.unpack("=i", fd_bytes[:4])[0]
                        mm = self._mmap.mmap(fd, size, prot=self._mmap.PROT_READ)
                        os.close(fd)
                gen, w, h = g, (sw if mm else 0), (sh if mm else 0)
                self._note({"surface": g, "kind": kind, "w": sw, "h": sh, "view": view})
            elif typ == self.UPDATE:
                s_, g, count = struct.unpack_from("=3I", data, 8)
                area = sum(struct.unpack_from("=iiii", data, 20 + 16 * i)[2] * struct.unpack_from("=iiii", data, 20 + 16 * i)[3]
                           for i in range(count))
                rec = {"update": s_, "gen": g, "rects": count, "area": area}
                if count and mm is not None and g == gen:
                    rec.update(w=w, h=h, sha=hashlib.sha256(mm[:w * h * 4]).hexdigest()[:16])
                self._note(rec)
                seq += 1
                try:
                    self._send(self.REQUEST, seq, 200)
                except OSError:
                    break
        if mm is not None:
            mm.close()

    def close(self):
        self.stop.set()
        self.thread.join(3)
        self.s.close()
        self.f.close()


def scenario_frames(src: tuple[int, int], dst: tuple[int, int]) -> dict:
    """SHA-256 prefixes of switch_scenario.py's complete desktops before and after its switch."""
    import hashlib
    sys.path.insert(0, str(HERE.parent))
    import switch_scenario as sc

    class Canvas:
        def __init__(self, w, h):
            self.w, self.h, self.mm = w, h, bytearray(w * h * 4)

        def rows(self, y0, y1, row):
            for y in range(y0, y1):
                self.mm[y * self.w * 4:(y + 1) * self.w * 4] = row(y)
    out = {}
    for name, (w, h) in (("before", src), ("after", dst)):
        c = Canvas(w, h)
        sc.desktop(c, strip=True, icons=True)
        out[name] = hashlib.sha256(bytes(c.mm)).hexdigest()[:16]
    return out


def tap_summary(records: list[dict], dst: tuple[int, int], expect: dict) -> dict:
    """What the remote screen showed at the new size: only the complete new desktop, or not."""
    surfaces = [r for r in records if "surface" in r]
    shown = [r for r in records if r.get("sha") and (r["w"], r["h"]) == tuple(dst)]
    return {"surfaces": [{k: r[k] for k in ("surface", "kind", "w", "h", "view")} for r in surfaces],
            "switching_surface": any(r["view"] == "switching" for r in surfaces),
            "pictures_at_new_size": len(shown),
            "incomplete_pictures_at_new_size": sum(1 for r in shown if r["sha"] != expect["after"]),
            "complete_desktop_before_switch": any(r.get("sha") == expect["before"] for r in records),
            "expected": expect}


def qmp(sock: Path, *cmds, timeout=10.0):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(str(sock))
    f = s.makefile("rw", buffering=1)
    json.loads(f.readline())
    out = []
    for c in ({"execute": "qmp_capabilities"},) + cmds:
        f.write(json.dumps(c) + "\n")
        f.flush()
        while True:
            r = json.loads(f.readline())
            if "event" in r:
                continue
            out.append(r)
            break
    s.close()
    return out[1:]


def send_key(sock: Path, key: str, hold_ms: int = 100):
    return qmp(sock, {"execute": "send-key", "arguments": {"keys": [{"type": "qcode", "data": key}],
                                                           "hold-time": hold_ms}})


def size_arg(v: str) -> tuple[int, int]:
    w, h = v.lower().split("x")
    return int(w), int(h)


def build(a, out: Path):
    from retroboot import graphics, schema
    from retroboot.config import Paths
    from retroboot.machines import LaunchOptions, Machine, build_argv

    root = out / "root"
    paths = Paths(root=root, qemu_package_override=Path(a.pkg), ndrv_override=Path(a.ndrv) if a.ndrv else None)
    paths.ensure()
    disk = out / "machine" / "disk.img"
    machine = Machine("lab", disk.parent, {"schema_version": 1, "id": "lab", "disk": disk.name, "name": "Mac OS 9"})
    settings = schema.defaults()
    settings.update({"share.enabled": False, "system.timezone": "UTC", "display.pixel_scale": a.pixel_scale})
    if a.card == "ati":
        from rbguestprep import prep as rbprep
        present = rbprep.ati_software_present(str(disk))
        machine.meta["graphics"] = {"ati_software": "present" if present else "absent"}
        settings["display.graphics"] = graphics.ATI
        gl = graphics.probe_host_gl(allow_software=True)      # lab: llvmpipe (hostd.json graphics_allow_software)
        graphics.set_host_gl(gl)
    cities = None
    try:
        from rbguestprep import macloc
        cities = list(macloc.read_cities(str(disk))) or None
    except Exception:
        pass
    from retroboot.display import effective_scale, suggested_scale
    w, h = a.display
    # the presenter's scale (4.5): automatic, the largest s with w/s >= 720 and h/s >= 540
    display = {"mode": [w, h, 60], "suggested_scale": suggested_scale(w, h),
               "scale": effective_scale(w, h, a.pixel_scale)}
    argv = build_argv(machine, settings, paths, display,
                      LaunchOptions(display_backend="retroboot", audio_capable=True,
                                    ndrv_dir=Path(a.ndrv) if a.ndrv else None, lab_qmp=paths.lab_qmp,
                                    loader_dir=Path(a.loader) if a.loader else None, mac_cities=cities))
    argv[argv.index("-audiodev") + 1] = "none,id=snd0"
    argv[argv.index("-rtc") + 1] = f"base={a.rtc}"
    if a.startup:
        argv[argv.index("-g") + 1] = "%dx%dx32" % a.startup
    if a.switch_to:
        argv += ["-prom-env", "retroboot-display=%dx%d" % a.switch_to]
    return paths, argv, machine


def run(a) -> int:
    out = Path(a.out).resolve()
    if out.exists():
        raise SystemExit(f"{out} exists")
    (out / "machine").mkdir(parents=True)
    events = (out / "events.jsonl").open("w", buffering=1)

    def step(what, **kw):
        events.write(json.dumps({"mono": round(time.monotonic(), 6), "lab": what, **kw}) + "\n")

    if a.client:   # a scripted display client instead of QEMU (switch_scenario.py)
        from retroboot.config import Paths
        paths = Paths(root=out / "root")
        paths.ensure()
        argv = ["/bin/sh", "-c", a.client]
    else:
        if not (a.disk and a.pkg):
            raise SystemExit("--disk and --pkg are needed without --client")
        base = Path(a.disk).resolve()
        disk = out / "machine" / "disk.img"
        subprocess.run(["cp", "--sparse=always", "--reflink=auto", str(base), str(disk)], check=True)
        paths, argv, machine = build(a, out)
        if a.card == "ati" and machine.meta.get("graphics", {}).get("ati_software") != "present":
            raise SystemExit("the disk has no ATI software")
        if a.card == "ati" and "ati-vga" not in " ".join(argv):
            raise SystemExit("build_argv did not give the ATI card (package feature? host GL?)")
    ctl_path = out / "presenter.sock"
    remote_path = out / "remote.sock" if a.tap else None
    pres_argv = [a.presenter, "--device", "virtual:%dx%d@60" % a.display, "--record", str(out / "presented.rbfl"),
                 "--record-guest", str(out / "guest.rbfl"), "--display-socket", str(paths.display_sock),
                 "--control-socket", str(ctl_path), "--remote-socket", str(remote_path or ""), "--socket-group", "root",
                 "--no-vt", "--no-input", "--no-handoff"]
    result = {"argv": argv, "presenter_argv": pres_argv, "card": a.card, "display": list(a.display)}
    pres = subprocess.Popen(pres_argv, stdout=open(out / "presenter.log", "w"), stderr=subprocess.STDOUT,
                            start_new_session=True)
    CHILDREN.append(pres)
    ctl = Control(ctl_path, events)
    hello = ctl.request("hello")
    result["hello"] = hello
    tap = RemoteTap(remote_path, out / "remote.jsonl") if remote_path else None
    startup = {"enabled": True, "art": a.art, "capture": None}
    if a.happy_mac != "omit":
        startup["happy_mac"] = a.happy_mac if a.happy_mac != "auto" else ("late" if a.card == "ati" else "prompt")
    ctl.request("startup_config", **startup)
    guest_size = [640, 480] if a.card == "ati" else (list(a.startup) if a.startup else None)
    ps = a.pixel_scale if a.pixel_scale == "auto" else int(a.pixel_scale)
    ctl.request("display_config", scaling="integer", pixel_scale=ps, guest_size=guest_size, host_mode=None,
                primary="internal", rotation=0)
    # The presenter latches startup_config when it enters the checkerboard; GRUB's
    # retroboot.progress=on (absent on the builder) would have turned it on at start.
    ctl.request("set_scene", scene="black")
    ctl.request("set_scene", scene="splash")
    ctl.request("startup_progress", remaining_ms=int(a.expect_ms), phase="lab", reset=True)
    time.sleep(0.5)
    (out / "argv.json").write_text(json.dumps(argv, indent=1))
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root", "LANG": "C.UTF-8", "TZ": ":UTC",
           "RETROBOOT_SESSION": "atipolish", "RB_DISPLAY_SOCKET": str(paths.display_sock)}
    t0 = time.monotonic()
    qemu = subprocess.Popen(argv, stdout=open(out / "qemu.out", "w"), stderr=subprocess.STDOUT, env=env,
                            cwd=str(out), start_new_session=True)
    CHILDREN.append(qemu)
    step("qemu_launched", pid=qemu.pid)
    result["t0"] = t0
    ctl.request("set_scene", scene="guest", reveal="first-content")
    ctl.request("startup_progress", remaining_ms=int(a.expect_ms), phase="launch")
    deadline = t0 + a.seconds
    while time.monotonic() < deadline and qemu.poll() is None:
        time.sleep(0.5)
    if qemu.poll() is None and a.client:
        os.killpg(qemu.pid, signal.SIGTERM)
        qemu.wait(10)
    elif qemu.poll() is None:
        step("shutdown_request")
        try:
            send_key(paths.qmp_control, "power")
            time.sleep(1.5)
            send_key(paths.qmp_control, "ret")
        except OSError as exc:
            step("qmp_error", error=str(exc))
        try:
            qemu.wait(90)
            step("qemu_exit", code=qemu.returncode)
        except subprocess.TimeoutExpired:
            step("qemu_kill")
            os.killpg(qemu.pid, signal.SIGTERM)
            qemu.wait(10)
    result["qemu_code"] = qemu.returncode
    try:
        ctl.request("set_scene", scene="black")
        time.sleep(0.3)
        st = ctl.request("status")
        result["status"] = st
    except (OSError, TimeoutError) as exc:
        result["status_error"] = str(exc)
    result["events"] = [e for e in ctl.seen if e.get("event") in
                        ("guest_connected", "guest_content", "guest_revealed", "guest_surface", "guest_disconnected")]
    if tap is not None:
        tap.close()
        if a.client and a.switch_from and a.switch_to:
            result["remote"] = tap_summary(tap.records, a.switch_to, scenario_frames(a.switch_from, a.switch_to))
    (out / "result.json").write_text(json.dumps(result, indent=1, default=str))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("out")
    r.add_argument("--presenter", required=True)
    r.add_argument("--disk")
    r.add_argument("--pkg")
    r.add_argument("--client", help="a display client command instead of QEMU (RB_DISPLAY_SOCKET in its environment)")
    r.add_argument("--ndrv")
    r.add_argument("--loader")
    r.add_argument("--card", choices=("ati", "std"), default="ati")
    r.add_argument("--display", type=size_arg, default=(1080, 720))
    r.add_argument("--pixel-scale", choices=("auto", "1", "2", "3", "4"), default="auto")
    r.add_argument("--startup", type=size_arg)
    r.add_argument("--switch-to", type=size_arg)
    r.add_argument("--switch-from", type=size_arg, help="with --client switch_scenario.py: its --from (for --tap's check)")
    r.add_argument("--tap", action="store_true",
                   help="be the web Screen page too: record every remote picture (OUT/remote.jsonl); with a "
                        "switch_scenario client, result.json 'remote' says whether any incomplete picture was streamed")
    r.add_argument("--art")
    r.add_argument("--happy-mac", choices=("auto", "prompt", "late", "omit"), default="auto")
    r.add_argument("--seconds", type=float, default=55.0)
    r.add_argument("--expect-ms", type=float, default=22000.0)
    r.add_argument("--rtc", default="2026-10-12T09:00:00")
    a = ap.parse_args()

    def stop(*_):
        kill_children()
        os._exit(1)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        return run(a)
    finally:
        kill_children()


if __name__ == "__main__":
    sys.exit(main())
