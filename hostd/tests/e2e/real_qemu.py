#!/usr/bin/env python3
"""End-to-end run: real hostd + real guest-launcher + REAL QEMU on a disposable OS 9 disk.

The presenter is the fake (tests/fakes/fake_presenter.py) but it implements the
section 3 display socket, so QEMU's ``-display retroboot`` backend delivers the
real Mac framebuffer and the section 4.3 content rule runs on genuine pixels.
User input (power key, R) travels presenter -> QEMU over the display socket.
Web steps use curl against hostd's HTTPS listener on 127.0.0.1.

Usage (on the builder, as root):

    python3 tests/e2e/real_qemu.py --work /srv/retroboot/wave2/hostd/e2e/run1 \
        --disk /path/to/DISPOSABLE/disk.img --qemu-package /path/to/linux-ppc \
        [--guestprep /path/to/guestprep] [--web /path/to/web] [--run-as rbguest] [--steps all]

The disk is MOVED into the scratch machine directory (it must be a disposable copy).
Evidence (lifecycle log, screenshots, API transcripts, results.json) lands in
<work>/evidence.  Never point --disk at an original fixture.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent.parent
FAKES = HERE.parent / "fakes"


class Run:
    def __init__(self, a):
        self.a = a
        self.work = Path(a.work).resolve()
        self.root = self.work / "root"
        self.ev = self.work / "evidence"
        self.lifecycle = self.root / "var/lib/retroboot/logs/lifecycle.jsonl"
        self.inject_path = self.work / "inject.sock"
        self.jar = self.work / "cookies.txt"
        self.results: dict = {"steps": [], "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        self.procs: list[subprocess.Popen] = []
        self.port = a.https_port
        self.csrf = None
        self.t0 = time.monotonic()

    # -- setup -----------------------------------------------------------------
    def setup(self):
        if self.work.exists():
            raise SystemExit(f"{self.work} exists; use a fresh work directory")
        (self.root / "etc/retroboot").mkdir(parents=True)
        self.ev.mkdir(parents=True)
        mdir = self.root / "var/lib/retroboot/machines/mac-os-9-2-1"
        mdir.mkdir(parents=True)
        disk = Path(self.a.disk).resolve()
        if str(disk).startswith("/srv/retroboot/fixtures"):
            raise SystemExit("refusing to use an original fixture")
        shutil.move(str(disk), mdir / "disk.img")
        (mdir / "machine.json").write_text(json.dumps({
            "schema_version": 1, "id": "mac-os-9-2-1", "name": "Mac OS 9.2.1", "os_version": "Mac OS 9.2.1",
            "disk": "disk.img", "source": {"type": "fixture-copy", "fixture_id": "classicmac-os9-921-v2"}}))
        (mdir / "state.json").write_text(json.dumps({"clean": True, "last_exit": None}))
        if self.a.mark_prepared:
            meta = json.loads((mdir / "machine.json").read_text())
            meta["prep"] = {"result": "applied", "at": "pre-run", "attempts": 1,
                            "report": {"shutdown_warning": "cleared before the run (see report)"}}
            (mdir / "machine.json").write_text(json.dumps(meta))
        # QEMU runs as rbguest: install the package like the image does (readable, executable).
        pkg = self.root / "usr/lib/retroboot/qemu"
        pkg.mkdir(parents=True)
        subprocess.run(["cp", "-a", str(Path(self.a.qemu_package).resolve()) + "/.", str(pkg)], check=True)
        # The video driver variant and the classicvirtio loader beside the package, as images install them.
        for opt, name in ((getattr(self.a, "ndrv", None), "qemu-ndrv"),
                          (getattr(self.a, "loader", None), "qemu-classicvirtio")):
            if opt:
                (self.root / "usr/lib/retroboot" / name).mkdir(parents=True, exist_ok=True)
                subprocess.run(["cp", "-a", str(Path(opt).resolve()) + "/.", str(self.root / "usr/lib/retroboot" / name)],
                               check=True)
        subprocess.run(["chmod", "-R", "a+rX", str(self.root / "usr")], check=True)
        self.pkg = pkg
        cfg = {"dev": True, "guest_runner": "subprocess", "display_backend": "retroboot",
               "qemu_package": str(pkg), "https_port": self.port, "https_bind": "127.0.0.1",
               "http_port": self.a.http_port, "run_as_user": self.a.run_as, "journal": False,
               "allow_network_tools": True, "poweroff_cmd": [], "reboot_cmd": [], "hostname_cmd": [],
               "timezone_cmd": [], "guest_content_timeout": 90.0, "finder_wait_timeout": 240.0,
               "clean_stop_timeout": 120.0, "handoff_deadline": self.a.handoff_deadline,
               "presenter_fallback_start": 0}
        if self.a.guestprep:
            cfg["guestprep_path"] = self.a.guestprep
        if getattr(self.a, "allow_software_gl", False):
            cfg["graphics_allow_software"] = True
        if self.a.web:
            cfg["web_static"] = self.a.web
        (self.root / "etc/retroboot/hostd.json").write_text(json.dumps(cfg, indent=2))
        self.note("setup", disk=str(mdir / "disk.img"), config=cfg)

    def start_presenter(self):
        run = self.root / "run/retroboot"
        run.mkdir(parents=True, exist_ok=True)
        log = open(self.ev / "fake-presenter.stderr", "ab")
        p = subprocess.Popen([sys.executable, str(FAKES / "fake_presenter.py"), "--socket", str(run / "presenter.sock"),
                              "--display-socket", str(run / "display.sock"), "--inject", str(self.inject_path),
                              "--log", str(self.ev / "presenter.jsonl"), "--mode", self.a.display_mode],
                             stdout=log, stderr=log)
        self.procs.append(p)
        self.wait(lambda: self.inject_path.exists(), 10, "fake presenter")
        return p

    def start_hostd(self):
        log = open(self.ev / "hostd.stderr", "ab")
        env = dict(os.environ, PYTHONPATH=str(SRC))
        p = subprocess.Popen([sys.executable, "-m", "retroboot.hostd", "--root", str(self.root)], env=env,
                             stdout=log, stderr=log)
        self.procs.append(p)
        self.hostd = p
        return p

    def stop_hostd(self):
        self.hostd.terminate()
        try:
            self.hostd.wait(40)
        except subprocess.TimeoutExpired:
            self.hostd.kill()

    # -- helpers ---------------------------------------------------------------
    def note(self, step, **kw):
        rec = {"step": step, "t": round(time.monotonic() - self.t0, 2), **kw}
        self.results["steps"].append(rec)
        print(json.dumps(rec)[:400], flush=True)

    def wait(self, pred, timeout, what):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            r = pred()
            if r:
                return r
            time.sleep(0.25)
        raise TimeoutError(f"timed out waiting for {what}")

    def events(self):
        try:
            return [json.loads(l) for l in self.lifecycle.read_text().splitlines() if l.strip()]
        except OSError:
            return []

    def count(self, name, **match):
        return len([e for e in self.events() if e["event"] == name and all(e.get(k) == v for k, v in match.items())])

    def wait_event(self, name, n=1, timeout=240, **match):
        self.wait(lambda: self.count(name, **match) >= n, timeout, f"{n}x {name}")
        return [e for e in self.events() if e["event"] == name and all(e.get(k) == v for k, v in match.items())][n - 1]

    def inject(self, msg):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(20)
        s.connect(str(self.inject_path))
        s.sendall((json.dumps(msg) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
        s.close()
        return json.loads(data)

    def doc(self):
        return (self.inject({"query": "state"}).get("doc") or {})

    def wait_doc(self, doc_id, timeout=30):
        return self.wait(lambda: self.doc().get("id") == doc_id and self.doc(), timeout, f"doc {doc_id}")

    def guest_png(self, name):
        path = self.ev / f"{name}.png"
        r = self.inject({"guest_png": str(path)})
        return str(path) if r.get("ok") else None

    def curl(self, method, path, body=None, out=None, extra=None, auth=True):
        argv = ["curl", "-sS", "-k", "-o", str(out) if out else "-", "-w", "\n%{http_code}", "-X", method,
                "-b", str(self.jar), "-c", str(self.jar)]
        if auth and self.csrf:
            argv += ["-H", f"X-RetroBoot-CSRF: {self.csrf}"]
        if body is not None:
            argv += ["-H", "Content-Type: application/json", "--data", json.dumps(body)]
        argv += extra or []
        argv.append(f"https://127.0.0.1:{self.port}{path}")
        r = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        text, _, code = r.stdout.rpartition("\n")
        transcript = {"cmd": " ".join(a if "CSRF" not in a else "X-RetroBoot-CSRF: [csrf]" for a in argv[:-1])
                      + " " + argv[-1], "status": int(code or 0)}
        with open(self.ev / "api-transcript.jsonl", "a") as f:
            f.write(json.dumps(transcript) + "\n")
        try:
            data = json.loads(text) if text and not out else text
        except ValueError:
            data = text
        return int(code or 0), data

    def job(self, method, path, body=None, timeout=420):
        code, data = self.curl(method, path, body)
        if code not in (200, 202):
            raise RuntimeError(f"{path} -> {code} {data}")
        jid = data["job"]["id"]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            _, j = self.curl("GET", f"/api/v1/jobs/{jid}")
            if j["state"] in ("succeeded", "failed"):
                return j
            time.sleep(1)
        raise TimeoutError(f"job {jid}")

    def booted(self, n, label):
        c = self.wait_event("guest_content", n, 240)
        f = self.wait_event("finder_detected", n, 300)
        start = [e for e in self.events() if e["event"] == "guest_start"][n - 1]
        shot = self.guest_png(f"finder-{label}")
        self.note(f"finder-{label}", session=start["session"], boot_speed=start.get("boot_speed"),
                  geometry=start.get("geometry"), content_after_s=c.get("t_since_start"),
                  finder_after_s=f.get("t_since_start"), screenshot=shot)
        return f

    # -- scenario ------------------------------------------------------------------
    def scenario(self):
        self.start_presenter()
        self.start_hostd()
        # 1 Autostart straight to Finder.
        self.wait_event("presenter_display", 1, 30)
        self.booted(1, "autostart")
        prep = [e for e in self.events() if e["event"] in ("guest_prep", "disk_repair")]
        self.note("prep-events", events=prep)
        # 2 Pair a browser with the code shown on the local panel.
        self.inject({"event": "chord", "name": "recovery"})
        self.wait_doc("recovery-main")
        self.inject({"click": "network"})
        d = self.wait_doc("network-web")
        flat = json.dumps(d)
        import re
        m = re.search(r'"text": "(\d) (\d) (\d) (\d) (\d) (\d)"', flat)
        code = "".join(m.groups()) if m else None
        self.note("panel-pairing", code_shown=bool(code), has_qr='"qr"' in flat,
                  fingerprint_shown="Certificate SHA-256" in flat)
        self.inject({"click": "cancel"})
        self.wait_doc("recovery-main")
        self.inject({"click": "cancel"})
        c, s = self.curl("GET", "/api/v1/status", auth=False)
        self.note("unpaired-status", status=c)
        c, pr = self.curl("POST", "/api/v1/pair", {"code": code, "device_name": "builder curl"}, auth=False)
        self.csrf = pr.get("csrf") if isinstance(pr, dict) else None
        cookie_flags = [l for l in self.jar.read_text().splitlines() if "rb_session" in l]
        self.note("pair", status=c, csrf=bool(self.csrf), cookie_secure="TRUE" in (cookie_flags[0] if cookie_flags else ""))
        c, st = self.curl("GET", "/api/v1/status")
        self.note("status", status=c, guest=st["guest"]["state"], timeline=st["guest"]["timeline"],
                  presenter=st["presenter"], capabilities=st["capabilities"])
        c, _ = self.curl("GET", "/api/v1/screen.png?what=guest", out=self.ev / "api-screen-guest.png")
        c2, _ = self.curl("GET", "/api/v1/screen.png?what=composed", out=self.ev / "api-screen-composed.png")
        self.note("screenshots", guest=c, composed=c2, guest_bytes=(self.ev / "api-screen-guest.png").stat().st_size)
        c, _ = self.curl("PATCH", "/api/v1/settings", {"revision": 0, "changes": {"audio.volume": 5}})
        self.note("stale-or-invalid-patch", status=c)
        # 3 User restarts Mac OS from the keyboard: power key -> Mac OS dialog -> R.
        self.wait(lambda: self.count("known_good_advanced") >= 1, 60, "known good")
        n_start = self.count("guest_start")
        self.inject({"guest_keys": ["power"]})
        time.sleep(3)
        self.guest_png("power-dialog-user")
        self.inject({"guest_keys": ["r"]})
        ex = self.wait_event("guest_exited", 1, 120)
        self.note("guest-restart", reason=ex["reason"], intent=ex.get("intent"))
        self.booted(n_start + 1, "after-guest-restart")
        # 4 Settings that need a restart: accelerated boot + 768 MB.
        _, snap = self.curl("GET", "/api/v1/settings")
        c, p = self.curl("PATCH", "/api/v1/settings", {"revision": snap["revision"], "changes": {
            "machine.boot_speed": "accelerated", "machine.ram_mb": 768, "system.timezone": "America/New_York"}})
        j = self.job("POST", "/api/v1/settings/apply", {"revision": p["revision"], "restart": False})
        self.note("apply-no-restart", state=j["state"], pending_restart=j["result"]["pending_restart"])
        _, snap = self.curl("GET", "/api/v1/settings")
        j = self.job("POST", "/api/v1/settings/apply", {"revision": snap["revision"], "restart": True})
        self.note("apply-and-restart", state=j["state"], result=j.get("result"), error=j.get("error"))
        f = self.booted(n_start + 2, "accelerated")
        argv = json.loads((self.root / "run/retroboot/guest/argv.json").read_text())["argv"]
        hand = [e for e in self.events() if e["event"] == "boot_handoff"]
        self.note("accelerated-argv", icount="-icount" in argv, ram=argv[argv.index("-m") + 1], handoff=hand[-1:])
        # 5 Checkpoints need a clean stop: refused while running, then web Shut Down (RetroBoot stays on).
        c, blocked = self.curl("POST", "/api/v1/machines/mac-os-9-2-1/checkpoints", {"label": "E2E clean"})
        self.note("checkpoint-while-running", status=c, code=(blocked.get("error") or {}).get("code"))
        j = self.job("POST", "/api/v1/guest/shutdown", {})
        ex = [e for e in self.events() if e["event"] == "guest_exited"][-1]
        d = self.wait_doc("stopped", 60)
        self.note("web-shutdown-stays-on", state=j["state"], exit=ex["reason"], intent=ex.get("intent"),
                  panel=[i.get("text") for i in d["items"] if i.get("type") == "text"],
                  poweroff_events=self.count("poweroff"))
        self.guest_png("stopped-panel")
        j = self.job("POST", "/api/v1/machines/mac-os-9-2-1/checkpoints", {"label": "E2E clean"}, timeout=600)
        self.note("checkpoint", state=j["state"], result=j.get("result"), error=j.get("error"))
        cp = j["result"]["id"]
        j = self.job("POST", "/api/v1/guest/start", {})
        self.booted(n_start + 3, "after-checkpoint")
        # 6 Force stop: labeled, logged with evidence; next start repairs the disk.
        j = self.job("POST", "/api/v1/guest/force-stop", {})
        fr = [e for e in self.events() if e["event"] == "force_request"][-1]
        self.note("force-stop", state=j["state"], evidence=j["result"].get("evidence"), logged=fr)
        self.wait_doc("stopped")
        j = self.job("POST", "/api/v1/guest/start", {})
        rep = self.wait_event("disk_repair", 1, 60)
        self.note("dirty-repair", result=rep.get("result"), seconds=rep.get("seconds"), output=rep.get("output"))
        self.booted(n_start + 4, "after-force-stop")
        # 7 Restore the checkpoint after a forced stop (dirty disk replaced by the clean checkpoint).
        j = self.job("POST", "/api/v1/guest/force-stop", {})
        self.note("force-stop-2", state=j["state"], evidence=(j.get("result") or {}).get("evidence_id"))
        j = self.job("POST", f"/api/v1/machines/mac-os-9-2-1/checkpoints/{cp}/restore", {}, timeout=600)
        self.note("restore", state=j["state"], result=j.get("result"), error=j.get("error"))
        st = json.loads((self.root / "var/lib/retroboot/machines/mac-os-9-2-1/state.json").read_text())
        self.note("restored-state", clean=st.get("clean"), restored_from=st.get("restored_from"))
        if self.count("guest_start") < n_start + 5:
            self.job("POST", "/api/v1/guest/start", {})
        self.booted(n_start + 5, "after-restore")
        repairs = self.count("disk_repair")
        self.note("no-repair-after-restore", disk_repairs_total=repairs)
        # 8 Diagnostics bundle.
        j = self.job("POST", "/api/v1/diagnostics/bundle", {})
        c, _ = self.curl("GET", f"/api/v1/diagnostics/bundle/{j['result']['id']}", out=self.ev / "bundle.tar.gz")
        self.note("bundle", state=j["state"], download=c, bytes=(self.ev / "bundle.tar.gz").stat().st_size)
        # 9 Host-requested clean Shut Down with power_off (power key + Return) -> poweroff (dev hook).
        j = self.job("POST", "/api/v1/guest/shutdown", {"power_off": True})
        po = self.wait_event("poweroff", 1, 60)
        ex = [e for e in self.events() if e["event"] == "guest_exited"][-1]
        self.note("host-shutdown", state=j["state"], exit=ex["reason"], poweroff=po)
        time.sleep(1)
        st = self.inject({"query": "state"})
        self.note("final-scene", scene=st["scene"])

    def scenario_dirty(self):
        """Forced stops followed by host repair: Mac OS must reach the Finder with no Disk First Aid."""
        self.start_presenter()
        self.start_hostd()
        self.wait_event("presenter_display", 1, 30)
        self.booted(1, "dirty-autostart")
        c, pr = self.curl("POST", "/api/v1/pair", {"code": self.pairing_code(), "device_name": "dirty"}, auth=False)
        self.csrf = pr["csrf"]
        for i in range(2, 2 + self.a.cycles):
            time.sleep(5)
            j = self.job("POST", "/api/v1/guest/force-stop", {})
            self.note(f"force-stop-{i}", state=j["state"], evidence=(j.get("result") or {}).get("evidence_id"))
            self.wait_doc("stopped", 60)
            self.job("POST", "/api/v1/guest/start", {})
            rep = self.wait_event("disk_repair", i - 1, 60)
            self.note(f"repair-{i}", result=rep.get("result"), seconds=rep.get("seconds"))
            self.booted(i, f"dirty-{i}")
            frames = []
            for k in range(4):           # watch for a Disk First Aid / improper shutdown dialog
                time.sleep(10)
                frames.append(self.guest_png(f"dirty-{i}-plus{(k + 1) * 10}s"))
            self.note(f"dirty-{i}-frames", frames=frames)
        j = self.job("POST", "/api/v1/guest/shutdown", {"power_off": True})
        self.note("final-shutdown", state=j["state"], poweroff=self.count("poweroff"))

    def scenario_graphics(self):
        """3D acceleration (ARCHITECTURE 4.9) through the product path on a disk with ATI's
        software and RetroBoot Display (os922-full-v3): the card and the Mac screen size
        are changed from the web and from the classic panel, Mac OS restarts into each,
        and the guest's own framebuffer must have the size the resolution policy chose
        (ATI's 640x480 boot frame, RetroBoot Display's switch before the Finder)."""
        import struct
        sys.path.insert(0, str(HERE))
        from cardswitch_lab import find_strip, read_png

        def png_size(path):
            with open(path, "rb") as f:
                return list(struct.unpack(">II", f.read(24)[16:24]))

        def settled(label, want, timeout=120):
            """The Finder's framebuffer at the expected size (RetroBoot Display switches
            just before the Finder; give it a moment after finder_detected).  Then let the
            Finder finish drawing before the next request: hostd's power-dialog check
            takes a Finder that is still drawing for the Shut Down dialog."""
            time.sleep(10)
            deadline = time.monotonic() + timeout
            size = None
            while time.monotonic() < deadline:
                shot = self.guest_png(f"size-{label}")
                size = png_size(shot) if shot else None
                if size == want:
                    break
                time.sleep(3)
            a = self.launch_argv()
            card = "ati-radeon" if any("ati-vga" in x for x in a) else "standard"
            opt = next((x for x in a if x.startswith("retroboot-display=")), None)
            g = a[a.index("-g") + 1] if "-g" in a else None
            _, st = self.curl("GET", "/api/v1/status")
            gfx = st.get("graphics") or {}
            ok = size == want
            # The Control Strip (hostd/reports/cardswitch-2026-10-13.md): Mac OS's default strip
            # stays on the bottom edge across card and size changes; status names this screen.
            time.sleep(5)
            strip_png = self.guest_png(f"strip-{label}")
            strip = find_strip(read_png(strip_png)) if strip_png else None
            strip_ok = bool(strip) and strip["from_bottom"] == 0
            screen_ok = gfx.get("mac_screen") == size
            self.note(f"graphics-{label}", ok=ok, guest_size=size, want=want, card=card, g=g, option=opt,
                      running=gfx.get("running"), mac_screen=gfx.get("mac_screen"), ati_screen=gfx.get("ati_screen"),
                      screen_ok=screen_ok, strip=strip, strip_ok=strip_ok,
                      available=gfx.get("available"), reason=gfx.get("unavailable_reason"))
            self.results.setdefault("graphics", []).append({"label": label, "ok": ok, "size": size, "want": want,
                                                            "card": card, "g": g, "option": opt,
                                                            "mac_screen": gfx.get("mac_screen"),
                                                            "screen_ok": screen_ok, "strip": strip,
                                                            "strip_ok": strip_ok})
            return card, size

        def patch(changes):
            _, sset = self.curl("GET", "/api/v1/settings")
            code, data = self.curl("PATCH", "/api/v1/settings", {"revision": sset["revision"], "changes": changes})
            if code != 200:
                raise RuntimeError(f"PATCH {changes} -> {code} {data}")
            return data["revision"]

        def apply_restart(rev, n):
            j = self.job("POST", "/api/v1/settings/apply", {"revision": rev, "restart": True}, timeout=600)
            res = j.get("result") or {}
            self.note("apply-restart", state=j["state"], applied=res.get("applied"), restarting=res.get("restarting"))
            if not res.get("restarting"):     # live keys only (Mac screen size): restart Mac OS for the card
                self.job("POST", "/api/v1/guest/restart", {}, timeout=600)
            self.booted(n, f"boot-{n}")

        def panel_display(values, button):
            self.inject({"event": "chord", "name": "recovery"})
            self.wait_doc("recovery-main")
            self.inject({"click": "display"})
            d = self.wait_doc("display-sound")
            box = None
            for g in d.get("items") or []:
                for i in (g.get("items") or []) if g.get("type") == "group" else [g]:
                    if i and i.get("id") == "display.graphics":
                        box = i
            self.note("panel-3d-checkbox", label=(box or {}).get("label"), value=(box or {}).get("value"),
                      disabled=(box or {}).get("disabled"),
                      note=[i.get("text") for g in d.get("items") or [] for i in (g.get("items") or [])
                            if isinstance(i, dict) and i.get("type") == "text" and "ATI" in (i.get("text") or "")])
            self.inject({"click": button, "values": values})

        self.start_presenter()
        self.start_hostd()
        self.wait_event("presenter_display", 1, 30)
        self.booted(1, "standard")
        c, pr = self.curl("POST", "/api/v1/pair", {"code": self.pairing_code(), "device_name": "graphics"},
                          auth=False)
        self.csrf = pr["csrf"]
        w, h = (int(v) for v in self.a.display_mode.split("x"))
        sys.path.insert(0, str(SRC))
        from retroboot import graphics as G
        from retroboot.display import guest_geometry

        exact = G.edid_modes(Path(self.a.qemu_package))      # the package's EDID sizes (ati-edid-modes)

        def ati(gs="auto"):
            return list(G.mac_screen({"mode": [w, h, 60]}, {"display.pixel_scale": "auto", "display.guest_size": gs},
                                     exact))
        std = list(guest_geometry({"mode": [w, h, 60]}, "auto", "auto"))
        settled("standard", std)
        # A live Mac screen change, as after a pixel-size change: Mac OS moves the Control Strip
        # and from then on keeps its place in Control Strip Preferences (it writes none before).
        for size in ([832, 624], std):
            self.inject({"guest_resize": size})
            time.sleep(8)
        live = self.guest_png("strip-live")
        self.note("live-resize", strip=find_strip(read_png(live)) if live else None)
        _, sset = self.curl("GET", "/api/v1/settings")
        d = next(x for x in sset["schema"] if x["key"] == "display.graphics")
        self.note("schema-graphics", available=d.get("available"), choices=d.get("choices"), help=d.get("help"))
        # 1 web: turn 3D acceleration on, Apply and Restart
        apply_restart(patch({"display.graphics": "ati-radeon"}), 2)
        settled("web-ati-on", ati())
        # 2 web: a fixed Mac screen size (live for the presenter, the card's size from the next start)
        apply_restart(patch({"display.guest_size": "800x600"}), 3)
        settled("web-800x600", ati("800x600"))
        # 3 panel: back to the size for this display (Apply), then Restart from the web
        panel_display({"display.guest_size": "auto"}, "apply")
        self.wait(lambda: self.curl("GET", "/api/v1/settings")[1]["applied"]["display.guest_size"] == "auto", 60,
                  "panel apply")
        j = self.job("POST", "/api/v1/guest/restart", {}, timeout=600)
        self.booted(4, "boot-4")
        settled("panel-auto", ati())
        # 4 panel: untick 3D acceleration, Apply and Restart
        panel_display({"display.graphics": False}, "restart")
        self.booted(5, "boot-5")
        settled("panel-ati-off", std)
        # 5 time zones (macclock seeds a Date & Time city; with Time Synchronizer on, an
        # unknown location used to hold Restart and Shut Down for about 100 s): Boise
        apply_restart(patch({"system.timezone": "America/Boise"}), 6)
        settled("boise", std)
        j = self.job("POST", "/api/v1/guest/shutdown", {"power_off": True})
        self.note("final-shutdown", state=j["state"])
        # how long each clean Restart/Shut Down took (request -> QEMU exit)
        ev = self.events()
        spans = []
        for i, e in enumerate(ev):
            if e["event"] == "clean_request":
                end = next((x for x in ev[i + 1:] if x["event"] == "guest_exited"), None)
                loc = next((x for x in reversed(ev[:i]) if x["event"] == "mac_location"), None)
                if end:
                    spans.append({"kind": e.get("kind"), "seconds": round(end["t_mono"] - e["t_mono"], 1),
                                  "zone": (loc or {}).get("zone"), "city": (loc or {}).get("city")})
        self.note("clean-stop-times", spans=spans)
        self.results["clean_stops"] = spans
        moves = [{k: e.get(k) for k in ("screen", "result", "from", "to")} for e in ev
                 if e["event"] == "control_strip"]
        self.note("control-strip-moves", moves=moves)
        self.results["control_strip"] = moves

    def launch_argv(self):
        for p in (self.root / "run/retroboot/guest/argv.json", self.root / "var/lib/retroboot/run/argv.json"):
            if p.exists():
                return json.loads(p.read_text())["argv"]
        hits = list(self.root.rglob("argv.json"))
        return json.loads(hits[0].read_text())["argv"] if hits else []

    def pairing_code(self):
        import re
        self.inject({"event": "chord", "name": "recovery"})
        self.wait_doc("recovery-main")
        self.inject({"click": "network"})
        self.wait_doc("network-web")
        # the window first renders before the network address is known: wait for the code
        m = self.wait(lambda: self.doc().get("id") == "network-web" and
                      re.search(r'"text": "(\d) (\d) (\d) (\d) (\d) (\d)"', json.dumps(self.doc())), 20,
                      "pairing code on the panel")
        self.inject({"click": "cancel"})
        self.wait_doc("recovery-main")
        self.inject({"click": "cancel"})
        return "".join(m.groups())

    def finish(self, error=None):
        if error:
            self.results["error"] = repr(error)
            self.guest_png("failure") if self.inject_path.exists() else None
        for p in reversed(self.procs):
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(30)
                except subprocess.TimeoutExpired:
                    p.kill()
        shutil.copy2(self.lifecycle, self.ev / "lifecycle.jsonl") if self.lifecycle.exists() else None
        for name in ("guest-serial.log", "qemu-stderr.log", "launcher.log", "hostd.log"):
            p = self.root / "var/lib/retroboot/logs" / name
            if p.exists():
                shutil.copy2(p, self.ev / name)
        self.results["finished"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        (self.ev / "results.json").write_text(json.dumps(self.results, indent=2))
        leftovers = subprocess.run(["pgrep", "-f", f"^{self.root}/usr/lib/retroboot/qemu/bin/qemu-system-ppc"],
                                   capture_output=True, text=True).stdout.split()
        print(json.dumps({"leftover_qemu": leftovers, "error": repr(error) if error else None}))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--disk", required=True)
    ap.add_argument("--qemu-package", required=True)
    ap.add_argument("--guestprep")
    ap.add_argument("--web")
    ap.add_argument("--run-as", default="")
    ap.add_argument("--https-port", type=int, default=8443)
    ap.add_argument("--http-port", type=int, default=0)
    ap.add_argument("--display-mode", default="1024x768")
    ap.add_argument("--handoff-deadline", type=float, default=60.0)
    ap.add_argument("--scenario", choices=("full", "dirty", "graphics"), default="full")
    ap.add_argument("--ndrv", help="video driver variant directory (installed as usr/lib/retroboot/qemu-ndrv)")
    ap.add_argument("--loader", help="classicvirtio loader directory (usr/lib/retroboot/qemu-classicvirtio)")
    ap.add_argument("--allow-software-gl", action="store_true",
                    help="hostd.json graphics_allow_software (lab VMs without a GPU: Mesa llvmpipe)")
    ap.add_argument("--cycles", type=int, default=2, help="forced-stop cycles for --scenario dirty")
    ap.add_argument("--mark-prepared", action="store_true",
                    help="record the disk as already prepared (skip rbguestprep.apply)")
    a = ap.parse_args()
    import signal

    def on_term(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")
    signal.signal(signal.SIGTERM, on_term)   # still clean up hostd/QEMU/presenter children
    r = Run(a)
    r.setup()
    err = None
    try:
        {"full": r.scenario, "dirty": r.scenario_dirty, "graphics": r.scenario_graphics}[a.scenario]()
    except BaseException as exc:  # keep evidence on any failure
        err = exc
    finally:
        r.finish(err)
    sys.exit(1 if err else 0)


if __name__ == "__main__":
    main()
