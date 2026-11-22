"""guest-launcher (ARCHITECTURE section 6).

Reads ``/run/retroboot/guest/argv.json`` written by hostd, verifies the
emulator package manifest, runs QEMU (as ``rbguest`` when started as root),
follows ``qmp-events.sock`` and writes ``status.json``::

    {"session":"uuid","state":"starting|running|exited","pid":n,"started_mono":n,
     "exit":{"code":n,"reason":"guest-shutdown|guest-reset|host-qmp-quit|host-signal|crash","mono":n}}

Exit codes: 0 guest-shutdown, 10 guest-reset, 20 host-requested stop, 1 otherwise.
"""
from __future__ import annotations

import argparse
import asyncio
import grp
import json
import logging
import os
import pwd
import re
import signal
import stat
import sys
import time
from pathlib import Path

from .config import Config
from .lifecycle import LifecycleLog
from .machines import MachineError, sha256_file, verify_package
from .qmp import QMPClient, QMPError
from .util import atomic_write_json, mono, read_json

log = logging.getLogger("retroboot.launcher")

EXIT_CODES = {"guest-shutdown": 0, "guest-reset": 10, "host-qmp-quit": 20, "host-signal": 20,
              "host-qmp-system-reset": 20, "host-ui": 20, "crash": 1}

HOST_REASONS = ("host-qmp-quit", "host-signal", "host-qmp-system-reset", "host-ui")


def classify(shutdown_reason: str | None, returncode: int | None, host_requested: bool) -> str:
    """Map QMP SHUTDOWN reason + process result to the status.json reason."""
    if shutdown_reason == "guest-shutdown":
        return "guest-shutdown"
    if shutdown_reason == "guest-reset":
        return "guest-reset"
    if shutdown_reason in HOST_REASONS:
        return shutdown_reason
    if shutdown_reason == "guest-panic":
        return "crash"
    if host_requested:
        return "host-signal"
    return "crash"


def verify_overlay(overlay: dict | None) -> None:
    """Files hostd placed in front of the package's data directory (the RetroBoot
    video driver variant) must still have the hashes hostd verified."""
    if not overlay:
        return
    d = Path(overlay.get("dir") or "")
    for name, digest in (overlay.get("files") or {}).items():
        if "/" in name or name.startswith("."):
            raise MachineError(f"invalid overlay file name {name!r}")
        f = d / name
        if not f.is_file() or sha256_file(f) != digest:
            raise MachineError(f"Overlay hash mismatch: {name}")


def valid_osx_egl_selection(spec: dict) -> bool:
    """Recheck the qualified disk profile at the final process boundary."""
    from . import osx, osx_graphics
    from .graphics import package_features

    if (spec.get("guest_os"), spec.get("os_build")) not in {
            ("macosx-10.3", "7W98"), ("macosx-10.4", "8S165")}:
        return False
    package = Path(spec["package"]["root"])
    required = {osx_graphics.FEATURE, osx_graphics.EGL_FEATURE,
                osx_graphics.COHERENT_FEATURE, osx_graphics.I8_FEATURE} | osx.SLEEP_FEATURES
    features = package_features(package)
    if not osx_graphics.node_path(spec.get("egl_render_node")) or \
            not osx.package_capable(package) or not required.issubset(features):
        return False
    argv = spec["argv"]
    # The generated native disk launch has exactly this boot selection. A
    # boot-CD or legacy profile cannot acquire EGL access by adding a node.
    if [argv[i + 1] for i, value in enumerate(argv[:-1]) if value == "-boot"] != ["c"]:
        return False
    devices = [value.split(",") for value in argv if value.startswith("ati-vga,")]
    if len(devices) != 1:
        return False
    options = devices[0][1:]
    for name, value in (("model", "r100"), ("x-agp", "on"),
                        ("x-coherent-vram", "on"), ("x-i8-byte-order", "on")):
        if [x for x in options if x.startswith(name + "=")] != [name + "=" + value]:
            return False
    selective = [x for x in options if x.startswith("x-selective-2d-sync=")]
    if selective:
        if selective != ["x-selective-2d-sync=on"] or \
                (spec.get("guest_os"), spec.get("os_build")) != ("macosx-10.4", "8S165") or \
                not {osx_graphics.SELECTIVE_2D_FEATURE, osx_graphics.DEPTH_MEMORY_FEATURE}.issubset(features) or \
                [x for x in options if x.startswith("x-depth-memory=")] != ["x-depth-memory=on"]:
            return False
    ram_source = [x for x in options if x.startswith("x-selective-2d-ram=")]
    if ram_source:
        if ram_source != ["x-selective-2d-ram=on"] or \
                selective != ["x-selective-2d-sync=on"] or \
                not {osx_graphics.RAM_SOURCE_FEATURE,
                     osx_graphics.TEXTURE_PAGE_FEATURE}.issubset(features):
            return False
    yuv_ram = [x for x in options if x.startswith("x-yuv-ram-byte-order=")]
    if yuv_ram:
        if yuv_ram != ["x-yuv-ram-byte-order=on"] or \
                (spec.get("guest_os"), spec.get("os_build")) != ("macosx-10.4", "8S165") or \
                not {osx_graphics.YUV_RAM_FEATURE, osx_graphics.YUV_VIEW_FEATURE,
                     osx_graphics.TEXTURE_PAGE_FEATURE}.issubset(features) or \
                [x for x in options if x.startswith("x-yuv16-aperture-view=")] != \
                ["x-yuv16-aperture-view=on"]:
            return False
    if "x-texture-sampling=on" in options:
        return False
    globals_ = [argv[i + 1] for i, value in enumerate(argv[:-1]) if value == "-global"]
    for name in ("via-pmu.osx-sleep", "uni-north-pci-pcihost.x-agp-compat"):
        if [x for x in globals_ if x.startswith(name + "=")] != [name + "=on"]:
            return False
    return True


class Launcher:
    def __init__(self, spec: dict, lifecycle: LifecycleLog, verify: bool = True):
        self.spec = spec
        self.lifecycle = lifecycle
        self.verify = verify
        self.session = spec["session"]
        self.status_path = Path(spec["status"])
        self.proc: asyncio.subprocess.Process | None = None
        self.host_requested = False
        self.shutdown_reason: str | None = None
        self.started_mono = mono()
        self.qmp: QMPClient | None = None
        self.events_connected = False

    def write_status(self, state: str, **extra) -> None:
        st = {"session": self.session, "state": state,
              "pid": self.proc.pid if self.proc else None,
              "started_mono": round(self.started_mono, 6), "machine": self.spec.get("machine")}
        st.update(extra)
        atomic_write_json(self.status_path, st)

    def _user(self):
        run_as = self.spec.get("run_as")
        if not run_as or os.geteuid() != 0:
            return None
        try:
            pw = pwd.getpwnam(run_as)
        except KeyError:
            log.warning("run_as user %s missing; running QEMU as root is refused", run_as)
            raise SystemExit(1)
        groups = []
        # render/video: the GPU for the ATI card; cdrom: this computer's CD-ROM drive (optical.py)
        for g in ("audio", "cdrom", "rbdata", "render", "video"):
            try:
                groups.append(grp.getgrnam(g).gr_gid)
            except KeyError:
                pass
        return pw, groups

    def _prepare_paths(self, user) -> None:
        for s in self.spec.get("sockets", []):
            try:
                os.unlink(s)
            except FileNotFoundError:
                pass
        serial = Path(self.spec["serial_log"])
        serial.parent.mkdir(parents=True, exist_ok=True)
        if serial.exists() and serial.stat().st_size > 0:
            os.replace(serial, serial.with_name(serial.name + ".1"))
        serial.touch()
        if user is not None:
            pw, _ = user
            run_dir = Path(self.spec["qmp_events"]).parent
            os.chown(run_dir, 0, pw.pw_gid)
            os.chmod(run_dir, 0o1775)
            os.chown(serial, pw.pw_uid, pw.pw_gid)
            for w in self.spec.get("writable", []):
                p = Path(w)
                # Never through a link: the machine directory belongs to the guest user, who
                # could have put one there (quadra.py makes the 68k Mac's files regular again).
                if p.exists() and not p.is_symlink():
                    os.chown(p, pw.pw_uid, pw.pw_gid, follow_symlinks=False)
                    os.chmod(p, (0o770 if p.is_dir() else 0o660))

    def _shader_cache_environment(self, user) -> dict[str, str]:
        """Give a qualified OS X renderer its own bounded persistent cache."""
        value = self.spec.get("shader_cache")
        if value is None:
            return {}
        try:
            path = Path(value)
            cwd = Path(self.spec["cwd"])
            expected = cwd.parent.parent / "cache" / "mesa-osx"
            if not cwd.is_absolute() or cwd.parent.name != "machines" or path != expected:
                raise ValueError("Unexpected graphics cache location")
            path.parent.mkdir(parents=True, mode=0o755, exist_ok=True)
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            parent_fd = os.open(path.parent, flags)
            try:
                parent_st = os.fstat(parent_fd)
                if parent_st.st_uid != os.geteuid() or parent_st.st_mode & 0o022:
                    raise ValueError("Unexpected graphics cache parent")
                try:
                    os.mkdir(path.name, mode=0o700, dir_fd=parent_fd)
                except FileExistsError:
                    pass
                # Anchor ownership changes to the validated parent descriptor.
                fd = os.open(path.name, flags, dir_fd=parent_fd)
                try:
                    st = os.fstat(fd)
                    uid, gid = (user[0].pw_uid, user[0].pw_gid) if user else (os.geteuid(), os.getegid())
                    if not stat.S_ISDIR(st.st_mode) or st.st_uid not in (0, uid):
                        raise ValueError("Unexpected graphics cache owner")
                    if (st.st_uid, st.st_gid) != (uid, gid):
                        os.fchown(fd, uid, gid)
                    os.fchmod(fd, 0o700)
                finally:
                    os.close(fd)
            finally:
                os.close(parent_fd)
        except (OSError, KeyError, TypeError, ValueError) as exc:
            self.lifecycle.event("graphics_cache_unavailable", session=self.session,
                                 error=type(exc).__name__)
            return {}
        return {"MESA_SHADER_CACHE_DIR": str(path), "MESA_SHADER_CACHE_MAX_SIZE": "256M"}

    async def _follow_events(self) -> None:
        path = self.spec["qmp_events"]
        deadline = time.monotonic() + 20.0
        while self.proc and self.proc.returncode is None:
            q = QMPClient(path)
            try:
                await q.connect(timeout=max(0.2, deadline - time.monotonic()))
            except (QMPError, OSError, asyncio.TimeoutError, ValueError) as exc:
                if time.monotonic() >= deadline:
                    self.lifecycle.event("launcher_qmp_unavailable", session=self.session, error=str(exc))
                    return
                await asyncio.sleep(0.1)
                continue
            self.qmp = q
            self.events_connected = True
            self.write_status("running", events=True)
            self.lifecycle.event("guest_running", session=self.session, pid=self.proc.pid,
                                 t_since_spawn=round(mono() - self.started_mono, 3))

            def on_event(msg: dict) -> None:
                name = msg.get("event")
                data = msg.get("data") or {}
                if name == "SHUTDOWN":
                    self.shutdown_reason = data.get("reason")
                if name in ("SHUTDOWN", "RESET", "STOP", "RESUME", "POWERDOWN", "GUEST_PANICKED"):
                    self.lifecycle.event("qmp_" + name.lower(), session=self.session,
                                         reason=data.get("reason"), guest=data.get("guest"))

            q.event_handlers.append(on_event)
            await q.closed.wait()
            return

    async def run(self) -> int:
        user = self._user()
        if self.verify:
            try:
                verify_package(Path(self.spec["package"]["root"]), tuple(self.spec["package"]["verify"]))
                verify_overlay(self.spec["package"].get("overlay"))
                verify_overlay(self.spec["package"].get("loader_overlay"))
                verify_overlay(self.spec["package"].get("rom"))       # a 68k Mac's ROM (quadra.py)
            except MachineError as exc:
                self.lifecycle.event("launcher_package_rejected", session=self.session, error=str(exc))
                self.write_status("exited", exit={"code": 1, "reason": "crash", "mono": mono(),
                                                  "detail": str(exc)})
                return 1
        self._prepare_paths(user)
        self.write_status("starting")
        stderr_path = Path(self.spec.get("stderr_log") or os.devnull)
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        err = open(stderr_path, "ab", buffering=0)
        err.write(f"--- session {self.session} {time.strftime('%Y-%m-%dT%H:%M:%S')} ---\n".encode())
        kwargs = {}
        if user is not None:
            pw, groups = user
            kwargs = {"user": pw.pw_uid, "group": pw.pw_gid, "extra_groups": groups}
        env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "RETROBOOT_SESSION": self.session,
               "HOME": "/nonexistent" if user else os.environ.get("HOME", "/"), "LANG": "C.UTF-8"}
        node = self.spec.get("egl_render_node")
        if node is not None:
            if not valid_osx_egl_selection(self.spec):
                err.close()
                self.write_status("exited", exit={"code": 1, "reason": "crash", "mono": mono(),
                                                  "detail": "Invalid Mac OS X graphics device selection."})
                return 1
            env["RETROBOOT_EGL_RENDER_NODE"] = node
            env.update(self._shader_cache_environment(user))
        tz = self.spec.get("timezone")
        if isinstance(tz, str) and re.fullmatch(r"[A-Za-z0-9_+\-]+(/[A-Za-z0-9_+\-]+)*", tz) and \
                Path("/usr/share/zoneinfo", tz).is_file():
            # -rtc base=localtime: the Mac clock follows the configured zone.
            env["TZ"] = ":" + tz
        if os.environ.get("XDG_RUNTIME_DIR") and user is None:
            env["XDG_RUNTIME_DIR"] = os.environ["XDG_RUNTIME_DIR"]
        argv = self.spec["argv"]
        place_tiger = (node is not None and self.spec.get("osx_fast_cores") is True and
                       (self.spec.get("guest_os"), self.spec.get("os_build")) == ("macosx-10.4", "8S165"))
        if place_tiger:
            # The OS X QEMU build leaves its workers unnamed unless explicitly requested.
            argv = argv.copy()
            if "-name" in argv:
                index = argv.index("-name") + 1
                argv[index] += ",debug-threads=on"
            else:
                argv.extend(("-name", "debug-threads=on"))
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.DEVNULL, stdout=err, stderr=err,
                cwd=self.spec.get("cwd") or "/", env=env, start_new_session=True, umask=0o007, **kwargs)
        except OSError as exc:
            err.close()
            self.lifecycle.event("launcher_spawn_failed", session=self.session, error=str(exc))
            self.write_status("exited", exit={"code": 1, "reason": "crash", "mono": mono(), "detail": str(exc)})
            return 1
        self.lifecycle.event("qemu_spawned", session=self.session, pid=self.proc.pid, machine=self.spec.get("machine"))
        self.write_status("starting")

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            loop.add_signal_handler(sig, self._on_signal, sig)
        follower = loop.create_task(self._follow_events())
        placement = None
        if place_tiger:
            from .tiger_cpu import place_when_ready
            placement = loop.create_task(place_when_ready(self.proc, self.lifecycle, self.session))
        rc = await self.proc.wait()
        if placement is not None:
            placement.cancel()
            await asyncio.gather(placement, return_exceptions=True)
        err.close()
        try:
            await asyncio.wait_for(follower, 2.0)
        except (asyncio.TimeoutError, Exception):
            follower.cancel()
        if self.qmp:
            await self.qmp.close()
        reason = classify(self.shutdown_reason, rc, self.host_requested)
        code = EXIT_CODES.get(reason, 1)
        self.write_status("exited", exit={"code": code, "reason": reason, "mono": round(mono(), 6),
                                          "qemu_returncode": rc, "qmp_reason": self.shutdown_reason})
        self.lifecycle.event("guest_exit", session=self.session, reason=reason, code=code,
                             qemu_returncode=rc, qmp_reason=self.shutdown_reason,
                             runtime_s=round(mono() - self.started_mono, 3))
        return code

    def _on_signal(self, sig) -> None:
        self.host_requested = True
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                return
            asyncio.get_running_loop().call_later(15.0, self._kill)

    def _kill(self) -> None:
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.kill()
            except ProcessLookupError:
                pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="guest-launcher", description="RetroBoot guest launcher")
    ap.add_argument("--root", default=os.environ.get("RETROBOOT_ROOT", "/"))
    ap.add_argument("--spec", help="argv.json path (default /run/retroboot/guest/argv.json)")
    ap.add_argument("--no-verify", action="store_true", help="skip package manifest verification (tests only)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="guest-launcher: %(message)s")
    cfg = Config(root=args.root)
    spec_path = Path(args.spec) if args.spec else cfg.paths.argv_json
    try:
        spec = read_json(spec_path, 1024 * 1024)
        if spec.get("schema") != 1 or not isinstance(spec.get("argv"), list) or not spec["argv"]:
            raise ValueError("invalid launch specification")
        if not all(isinstance(a, str) for a in spec["argv"]):
            raise ValueError("argv must be strings")
    except (OSError, ValueError) as exc:
        log.error("cannot read launch specification: %s", exc)
        return 1
    lifecycle = LifecycleLog(Path(spec.get("lifecycle_log") or cfg.paths.lifecycle_log), source="launcher")
    launcher = Launcher(spec, lifecycle, verify=not args.no_verify)
    return asyncio.run(launcher.run())


if __name__ == "__main__":
    sys.exit(main())
