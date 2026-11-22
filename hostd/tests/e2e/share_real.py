#!/usr/bin/env python3
"""Lab driver: real hostd + real QEMU + a disposable Mac OS 9 disk, for the shared folder.

Builds on real_qemu.py (same scratch root, fake presenter with a real display socket,
guest input over that socket) but keeps the session running between commands, so a
browser (through an SSH tunnel to --http-port) and these commands can work on the
same Mac OS:

    share_real.py up    --work W --disk COPY.img --qemu-package PKG [--guestprep LIB]
                        [--web WEB] [--platinum LIB] [--apps LIB] [--http-port 18090]
                        [--ndrv DIR] [--classicvirtio DIR]   driver variants, installed as the image does
                        [--machine "Mac OS 9.2.2" --fixture os922-full-v5]   the machine's name and source
                        [--remote]   the fake presenter also serves the remote screen socket
                                     (the web Screen page streams the guest surface)
    share_real.py code  --work W              a pairing code from the panel
    share_real.py shot  --work W NAME         guest framebuffer -> W/evidence/NAME.png
    share_real.py click --work W X Y          (dbl X Y, drag X Y X2 Y2, menu X Y X2 Y2)
    share_real.py keys  --work W a b c ...    QEMU key names, tapped
    share_real.py type  --work W "text"       lower-case text, digits and . , - space return
    share_real.py events --work W [NAME]      lifecycle events
    share_real.py restart-hostd --work W      restart hostd (shut Mac OS down cleanly first)
    share_real.py down  --work W

Run as root on the lab builder.  The disk is MOVED into the scratch root; never point
--disk at a fixture or seed image (copy it with cp --sparse=always first).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import real_qemu  # noqa: E402

KEYS = {" ": "spc", ".": "dot", ",": "comma", "-": "minus", "\n": "ret", "/": "slash", "'": "apostrophe",
        ";": "semicolon", "=": "equal"}


def run_for(work: Path, **kw) -> real_qemu.Run:
    saved = work / "lab.json"
    opts = json.loads(saved.read_text()) if saved.exists() else {}
    opts.update({k: v for k, v in kw.items() if v is not None})
    a = SimpleNamespace(work=str(work), disk=opts.get("disk", ""), qemu_package=opts.get("qemu_package", ""),
                        guestprep=opts.get("guestprep"), web=opts.get("web"), run_as=opts.get("run_as", ""),
                        https_port=opts.get("https_port", 18443), http_port=opts.get("http_port", 18090),
                        display_mode=opts.get("display_mode", "1024x768"), handoff_deadline=60.0,
                        mark_prepared=False, scenario="full", cycles=0)
    r = real_qemu.Run(a)
    r.opts = opts
    return r


def spawn(argv, log: Path, env=None) -> int:
    f = open(log, "ab")
    p = subprocess.Popen(argv, stdout=f, stderr=f, env=env, start_new_session=True)
    return p.pid


def up(a) -> None:
    work = Path(a.work).resolve()
    r = run_for(work, disk=a.disk, qemu_package=a.qemu_package, guestprep=a.guestprep, web=a.web,
                http_port=a.http_port, https_port=a.https_port, display_mode=a.display_mode)
    r.setup()
    if a.machine:                       # real_qemu names every lab machine "Mac OS 9.2.1"
        meta_path = r.root / "var/lib/retroboot/machines/mac-os-9-2-1/machine.json"
        meta = json.loads(meta_path.read_text())
        meta.update(name=a.machine, os_version=a.machine)
        if a.fixture:
            meta["source"] = {"type": "fixture-copy", "fixture_id": a.fixture}
        meta_path.write_text(json.dumps(meta))
    # Driver variants beside the package, where the image installs them (hostd verifies both)
    for src, sub in ((a.ndrv, "qemu-ndrv"), (a.classicvirtio, "qemu-classicvirtio")):
        if src:
            subprocess.run(["cp", "-a", str(Path(src).resolve()) + "/.", str(r.root / "usr/lib/retroboot" / sub)],
                           check=True)
            subprocess.run(["chmod", "-R", "a+rX", str(r.root / "usr/lib/retroboot" / sub)], check=True)
    cfg_path = r.root / "etc/retroboot/hostd.json"
    cfg = json.loads(cfg_path.read_text())
    cfg.update({k: v for k, v in (("platinum_path", a.platinum), ("apps_path", a.apps)) if v})
    cfg["http_bind"] = "127.0.0.1"
    cfg_path.write_text(json.dumps(cfg, indent=2))
    (work / "lab.json").write_text(json.dumps(r.opts | {"platinum": a.platinum, "apps": a.apps}, indent=2))
    run = r.root / "run/retroboot"
    run.mkdir(parents=True, exist_ok=True)
    pids = {"presenter": spawn([sys.executable, str(real_qemu.FAKES / "fake_presenter.py"), "--socket",
                                str(run / "presenter.sock"), "--display-socket", str(run / "display.sock"),
                                "--inject", str(r.inject_path), "--log", str(r.ev / "presenter.jsonl"),
                                "--mode", a.display_mode]
                               + (["--remote-socket", str(run / "remote.sock")] if a.remote else []),
                               r.ev / "fake-presenter.stderr")}
    r.wait(lambda: r.inject_path.exists(), 10, "fake presenter")
    pids["hostd"] = spawn([sys.executable, "-m", "retroboot.hostd", "--root", str(r.root)], r.ev / "hostd.stderr",
                          env=dict(os.environ, PYTHONPATH=str(real_qemu.SRC)))
    (work / "pids.json").write_text(json.dumps(pids))
    r.wait_event("presenter_display", 1, 60)
    f = r.wait_event("finder_detected", 1, 400)
    print(json.dumps({"finder_after_s": f.get("t_since_start"), "http": f"http://127.0.0.1:{a.http_port}/",
                      "share": str(r.root / "var/lib/retroboot/share")}))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd")
    ap.add_argument("args", nargs="*")
    ap.add_argument("--work", required=True)
    ap.add_argument("--disk")
    ap.add_argument("--qemu-package")
    ap.add_argument("--guestprep")
    ap.add_argument("--web")
    ap.add_argument("--platinum")
    ap.add_argument("--apps")
    ap.add_argument("--http-port", type=int, default=18090)
    ap.add_argument("--https-port", type=int, default=18443)
    ap.add_argument("--display-mode", default="1024x768")
    ap.add_argument("--ndrv")
    ap.add_argument("--classicvirtio")
    ap.add_argument("--machine", help="the machine's name and Mac OS version, e.g. 'Mac OS 9.2.2'")
    ap.add_argument("--fixture", help="the fixture the disk was copied from")
    ap.add_argument("--remote", action="store_true", help="serve the remote screen socket (Screen page)")
    a = ap.parse_args()
    work = Path(a.work).resolve()
    if a.cmd == "up":
        return up(a)
    r = run_for(work)
    x = [int(v) for v in a.args if v.lstrip("-").isdigit()]
    if a.cmd == "code":
        print(r.pairing_code())
    elif a.cmd == "shot":
        print(r.guest_png(a.args[0]))
    elif a.cmd == "click":
        r.inject({"guest_click": x[:2]})
    elif a.cmd == "dbl":
        r.inject({"guest_pointer": x[:2]})
        time.sleep(0.15)
        for _ in range(2):
            r.inject({"guest_button": True})
            time.sleep(0.06)
            r.inject({"guest_button": False})
            time.sleep(0.08)
    elif a.cmd in ("drag", "menu"):
        r.inject({"guest_pointer": x[:2]})
        time.sleep(0.15)
        r.inject({"guest_button": True})
        time.sleep(0.4 if a.cmd == "menu" else 0.2)
        steps = 8
        for i in range(1, steps + 1):
            r.inject({"guest_pointer": [x[0] + (x[2] - x[0]) * i // steps, x[1] + (x[3] - x[1]) * i // steps]})
            time.sleep(0.05)
        time.sleep(0.3)
        r.inject({"guest_button": False})
    elif a.cmd == "keys":
        r.inject({"guest_keys": a.args})
    elif a.cmd == "type":
        names = [KEYS.get(c, c) for c in " ".join(a.args)]
        r.inject({"guest_keys": names})
    elif a.cmd == "events":
        evs = r.events()
        if a.args:
            evs = [e for e in evs if e["event"] in a.args]
        for e in evs[-40:]:
            print(json.dumps(e)[:300])
    elif a.cmd == "restart-hostd":         # after a clean Mac OS shut down: load changed hostd code
        pids = json.loads((work / "pids.json").read_text())
        try:
            os.killpg(pids["hostd"], signal.SIGTERM)
        except ProcessLookupError:
            pass
        time.sleep(4)
        n = r.count("finder_detected")
        pids["hostd"] = spawn([sys.executable, "-m", "retroboot.hostd", "--root", str(r.root)], r.ev / "hostd.stderr",
                              env=dict(os.environ, PYTHONPATH=str(real_qemu.SRC)))
        (work / "pids.json").write_text(json.dumps(pids))
        f = r.wait_event("finder_detected", n + 1, 400)
        print(json.dumps({"finder_after_s": f.get("t_since_start")}))
    elif a.cmd == "down":
        pids = json.loads((work / "pids.json").read_text()) if (work / "pids.json").exists() else {}
        for name in ("hostd", "presenter"):
            pid = pids.get(name)
            if pid:
                try:
                    os.killpg(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        time.sleep(3)
        left = subprocess.run(["pgrep", "-f", f"^{r.root}/usr/lib/retroboot/qemu/bin/qemu-system-ppc"],
                              capture_output=True, text=True).stdout.split()
        for pid in left:
            os.kill(int(pid), signal.SIGKILL)
        print(json.dumps({"stopped": list(pids), "killed_qemu": left}))
    else:
        raise SystemExit(f"unknown command {a.cmd}")


if __name__ == "__main__":
    main()
