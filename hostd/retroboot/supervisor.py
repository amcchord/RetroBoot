"""Guest supervisor (ARCHITECTURE sections 4 and 6).

Policy:

* Start the guest as soon as the presenter reports its display; never wait for
  the network.  Scene ``splash`` then ``guest`` with ``reveal: first-content``.
* guest-reset (Mac OS Restart) -> relaunch immediately behind the checkerboard.
* guest-shutdown (Mac OS Shut Down) -> ``black`` scene, sync, power off.
* No ``guest_content`` in time, or a crash -> retry once; then a classic failure
  alert with an evidence ID.  "In time" is learned per machine (content_deadline):
  three times its usual time to the Mac OS gray (at least twice its slowest
  recent one, at least 20 s), 60 s until it has a history.  A launch that ends
  before any guest content with the disk file unchanged leaves the machine's
  clean state as it was (no repair for a guest that never wrote).
* Host requests use the ADB power key and Mac OS's own dialog (Return = Shut
  Down, R = Restart), are verified by the QMP SHUTDOWN reason, and never fall
  back to a force stop on their own.  The key is pressed once the Finder is up
  (a busy Mac OS holds it), the answer goes only to a dialog recognized by its
  content (powerdlg.py), and a key is repeated only when an idle Mac OS dropped it;
  requests while Mac OS starts wait for it, and a Shut Down before QEMU runs
  cancels the start.  Force actions are separate, labeled and logged with an
  evidence ID.
* Escape during the splash pauses (QMP ``stop``) the starting guest and shows
  the boot options; a guest that may have mounted its disk is never killed for
  that.
* Before each launch a dirty machine (no verified clean exit) gets
  ``fsck.hfsplus``; each machine gets the one-time General Controls prep.
* Mac OS Shut Down: the scene goes ``black`` on QMP SHUTDOWN (guest-shutdown),
  before QEMU exits; the presenter holds Mac OS's last frame until then.
* Responsiveness: QMP must answer (emulator) and a frozen framebuffer is
  checked with a pointer nudge (freeze.py); never restarts on its own.
"""
from __future__ import annotations

import asyncio
import inspect
import re
import hashlib
import json
import logging
import os
import shutil
import signal
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from . import audio as hostaudio, finder, pointer, powerdlg, power_osx
from .freeze import FrozenGuestCheck
from .guestprep import UNAVAILABLE
from .jobs import Job, JobFailed
from .machines import (POWERMAC, QUADRA800, LaunchOptions, MachineError, MachineUnavailable, build_argv,
                       guest_geometry, launch_spec)
from .qmp import (NONBLACK_FRACTION, SETTLED_CHANGE, QMPClient, QMPError, encode_png, frame_samples,
                  genuine_content, read_ppm, samples_changed)
from .util import atomic_write_json, mono, read_json, run as run_cmd, wall

if TYPE_CHECKING:
    from .hostd import Hostd

log = logging.getLogger("retroboot.supervisor")

CD_REQUEST_TTL = 600.0     # Start Up from CD waits this long for its start (a slow Shut Down)


def needs_cd_parts(name: str, blank: bool, osx: bool = False) -> tuple[str, str]:
    if osx:
        if blank:
            return (f"There's no Mac OS X on “{name}” yet.", "Put a Mac OS X install disc in a connected CD or DVD drive, "
                    "or add a disc image, then choose Start Up from Disc in Startup Disk.")
        return ("The install disc isn't available anymore.",
                "Reconnect the disc drive or choose a disc image and try again.")
    if blank:
        return (f"There's no Mac OS on “{name}” yet.", "Put a Mac OS CD in this computer's CD-ROM drive, "
                "or add a CD image, then choose Start Up from CD in Startup Disk.")
    return ("The CD to start up from isn't available anymore.", "Put it in the CD-ROM drive and try again.")


def needs_cd_text(name: str, blank: bool, osx: bool = False) -> str:
    return " ".join(needs_cd_parts(name, blank, osx))

CONTENT_HISTORY = 5     # first-content times kept per machine (state.json "content_s")


def content_deadline(history, cap: float, factor: float, floor: float) -> float:
    """Seconds to wait for genuine guest content (the Mac OS gray) before a retry.

    A cold boot shows the gray after a nearly constant time on a given host
    (7-9 s on the loaded lab builder, 6.6-7.0 s on the Ryzen mini PC, ~25-30 s
    on the N4100 laptop); the rare boots that hang before it never show it
    (emulator/reports/pregray-hang-20261011.md).  So wait factor x the median
    of this machine's recent times, and at least twice the slowest of them and
    floor; without a history (first boot on a new host or disk) wait cap, the
    old fixed deadline.  Never more than cap."""
    vals = sorted(float(v) for v in (history or [])
                  if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v < cap)
    if not vals:
        return cap
    n = len(vals)
    median = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
    return round(min(cap, max(floor, factor * median, 2 * vals[-1])), 1)


def disk_stamp(path) -> tuple[int, int] | None:
    """(mtime_ns, size) of a disk image: unchanged means the guest never wrote it."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size

STOPPED, STARTING, RUNNING, PAUSED, STOPPING = "stopped", "starting", "running", "paused", "stopping"


def _near(a, b, tol: int = 12) -> bool:
    """Two boxes (x0, y0, x1, y1) within tol pixels on every side."""
    return a is not None and b is not None and all(abs(int(p) - int(q)) <= tol for p, q in zip(a, b))


def kill_qemu(pid: int | None) -> bool:
    """SIGKILL the emulator process of this session (verified by its command line)."""
    if not pid:
        return False
    try:
        cmdline = Path(f"/proc/{int(pid)}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    if not any(b"qemu-system" in part for part in cmdline[:2]):
        return False
    try:
        os.kill(int(pid), signal.SIGKILL)
        return True
    except OSError:
        return False


def changed_fraction(a: tuple, b: tuple, box: tuple[float, float, float, float] = (0.2, 0.2, 0.8, 0.8)) -> float:
    """Fraction of sampled pixels in a centered box that differ between two frames."""
    (w1, h1, p1), (w2, h2, p2) = a, b
    if (w1, h1) != (w2, h2):
        return 1.0
    x0, y0, x1, y1 = int(box[0] * w1), int(box[1] * h1), int(box[2] * w1), int(box[3] * h1)
    changed = total = 0
    for y in range(y0, y1, 4):
        for x in range(x0, x1, 4):
            o = (y * w1 + x) * 3
            total += 1
            if abs(p1[o] - p2[o]) > 24 or abs(p1[o + 1] - p2[o + 1]) > 24 or abs(p1[o + 2] - p2[o + 2]) > 24:
                changed += 1
    return changed / total if total else 0.0


class Supervisor:
    def __init__(self, app: "Hostd"):
        self.app = app
        self.cfg = app.cfg
        self.paths = app.paths
        self.log = app.lifecycle
        self.state = STOPPED
        self.handle = None
        self.session: str | None = None
        self.machine = None
        self.t_start: float | None = None
        self.started_wall: str | None = None
        self.content_seen = False
        self.revealed = False
        self.finder_seen = False
        self.osx_finder_visible = False
        # Mac OS counted as started without its Finder recognized (_started_watch): the
        # last resort for the dependents of mac_os_up.
        self.started_seen = False
        self.paused = False
        self.qmp: QMPClient | None = None
        self.intent: str | None = None
        self.attempts = 0
        self.relaunching = False
        self.failure: dict | None = None
        self.boot_hold = False
        self.booted = False
        self.boot_pending = False      # boot() is deciding or launching (Escape must hold it)
        self.start_cancel = False      # a Shut Down came while RetroBoot prepared Mac OS's start
        self.last_exit: dict | None = None
        self.boot_speed: str | None = None
        self.handoff: dict | None = None
        self._tasks: list[asyncio.Task] = []
        self._content_timer: asyncio.TimerHandle | None = None
        self._kg_timer: asyncio.TimerHandle | None = None
        self.content_deadline: float = self.cfg.guest_content_timeout
        self.launch_disk: tuple[int, int] | None = None   # disk_stamp() just before QEMU starts
        self.launch_clean: tuple[bool, str | None] = (True, None)   # clean, dirty_since before it
        self.paused_before_content = False                 # boot options: not a usable content time
        self.exit_event = asyncio.Event()
        self.content_event = asyncio.Event()
        self.finder_event = asyncio.Event()
        self.up_event = asyncio.Event()           # mac_os_up: the Finder, or Mac OS started without it
        self.launch_lock = asyncio.Lock()
        self.probe_until: float = 0.0
        self.finder_timeout_logged = False
        # Responsiveness: qmp_ok (QMP answers) and display_ok (frozen-guest
        # check); None = not known for this session.
        self.qmp_ok: bool | None = None
        self.display_ok: bool | None = None
        self.unresponsive_since: str | None = None
        self.unresponsive_evidence: str | None = None
        self.freeze_check: FrozenGuestCheck | None = None
        self.last_frame_png: bytes | None = None
        # The last ADB power key hostd sent: {session, mono, before (frame), origin, n, box}.
        self.power_key: dict | None = None
        self.power_lock = asyncio.Lock()          # one power-key dialog at a time
        self.load: powerdlg.GuestLoad | None = None   # how busy Mac OS keeps the emulated CPU
        self.load_session: str | None = None
        self.finder_mono: float | None = None
        self.finder_frame: tuple | None = None       # the first Finder frame of the session (menus enabled)
        self.content_mono: float | None = None       # when this session's genuine content was first seen
        self.power_failure: str | None = None     # why the last dialog attempt failed (busy, no-dialog)
        self.guest_surface: list | None = None    # this session's Mac OS screen size (presenter guest_surface)
        # Start Up from CD (request_cd_start): the next start of that Mac starts up from the CD once.
        self.cd_request: dict | None = None
        self.cd_session: dict | None = None       # this session's CDs: {boot, drive} (cd_plan)
        self.needs_cd: dict | None = None         # the last start refused for want of a CD
        # The kind of Mac of this session (machines.FAMILIES): a 68k Mac (quadra.py) has no PMU,
        # AirPort, ATI or USB, and its Mac OS keeps the CPU busy unless RetroBoot Idle runs.
        self.family: str = POWERMAC
        self.chime_played: bool = False
        self.unavailable: str | None = None       # why the last start was refused (MachineUnavailable)
        self.corrupt_disk_recovery: dict | None = None  # failed pre-launch disk repair state (#48)
        self.manual_resize_notice: str | None = None    # Monitors mode blocking live RESIZE (#19)
        self._last_osx_frame: tuple[int, int, bytes] | None = None
        # A 68k Mac's Restart is hostd's Shut Down and a new start (_restart_by_shutdown): set
        # when hostd's own Return goes to the verified dialog, {session, mono}.  Independent of
        # the request's intent, so a Shut Down Mac OS finishes after the request gave up (an
        # application asked to save) still restarts.
        self.restart_by_shutdown: dict | None = None
        # A clean request that gave up while Mac OS was still shutting down (an application
        # asked to save; job_clean "not-finished"): {session, mono, purpose, then, resume}.  A
        # Shut Down Mac OS finishes later, within late_stop_ttl_s, completes that request
        # (_finish_late_stop) instead of counting as the user's own Shut Down, which powers off.
        self.late_stop: dict | None = None

    # -- helpers -------------------------------------------------------------
    @property
    def responsive(self) -> bool | None:
        if self.qmp_ok is False or self.display_ok is False:
            return False
        if self.qmp_ok is None and self.display_ok is None:
            return None
        return True

    @property
    def unresponsive_reason(self) -> str | None:
        if self.qmp_ok is False:
            return "emulator"
        if self.display_ok is False:
            return "display"
        return None

    def _set_responsive(self, which: str, ok: bool | None, session: str | None, **extra) -> None:
        before = self.responsive
        setattr(self, which, ok)
        after = self.responsive
        check = {"qmp_ok": "qmp query-status", "display_ok": "frozen display"}[which]
        if before is not False and after is False:
            self.unresponsive_since = wall()
            self.log.event("guest_unresponsive", session=session, check=check, **extra)
            self._spawn(self._unresponsive_evidence(session, check), "unresponsive-evidence")
        elif before is False and after is not False:
            self.log.event("guest_responsive_again", session=session, check=check, **extra)
            self.unresponsive_since = None
            self.unresponsive_evidence = None
        if before != after and self.app.panel.visible:
            self._spawn(self.app.panel.refresh(), "panel-refresh")

    def set_display_responsive(self, ok: bool, session: str | None, **extra) -> None:
        self._set_responsive("display_ok", ok, session, **extra)

    async def _unresponsive_evidence(self, session: str | None, check: str) -> None:
        eid = await self.app.diagnostics.capture_evidence(
            "guest_unresponsive", f"Mac OS is not responding ({check})", {"session": session})
        if session == self.session and self.responsive is False:
            self.unresponsive_evidence = eid
            self.log.event("guest_unresponsive_evidence", session=session, evidence=eid)

    @property
    def active(self) -> bool:
        return self.state in (STARTING, RUNNING, PAUSED, STOPPING)

    def active_machine(self):
        return self.app.machines.get(self.app.settings.applied.get("machine.active"))

    def _spawn(self, coro, name: str) -> asyncio.Task:
        t = asyncio.get_running_loop().create_task(coro, name=name)
        self._tasks.append(t)
        t.add_done_callback(lambda tt: self._tasks.remove(tt) if tt in self._tasks else None)
        return t

    def _cancel_session(self) -> None:
        for t in list(self._tasks):
            if t is not asyncio.current_task():
                t.cancel()
        for h in (self._content_timer, self._kg_timer):
            if h:
                h.cancel()
        self._content_timer = self._kg_timer = None

    # -- boot decision ---------------------------------------------------------
    async def boot(self) -> None:
        """Called once the presenter reported its display (or the fallback elapsed)."""
        if self.booted:
            return
        self.booted = True
        self.boot_pending = True
        try:
            await self._boot_decide()
        finally:
            self.boot_pending = False

    async def _boot_decide(self) -> None:
        if await self.try_adopt():
            return
        if self.boot_hold or not self.app.settings.applied.get("startup.autostart", True):
            await self.app.panel.show_startup_options()
            return
        if self.active_machine() is None:
            if not self.app.machines.list():
                await self.app.panel.show_setup()
            else:
                await self.app.panel.show_startup_options(notice="The selected Mac could not be found. Choose another in Startup Disk.")
            return
        await self.start_guest("boot")

    async def try_adopt(self) -> bool:
        try:
            handle = await self.app.runner.adopt()
        except Exception:
            handle = None
        if handle is None:
            return False
        self.handle = handle
        self.session = handle.session
        st = handle.status() or {}
        self.machine = self.app.machines.get(st.get("machine"))
        self.state = RUNNING
        self.content_seen = True
        # A guest adopted soon after its launch may still be starting up: a power key
        # then reaches Mac OS before it can show the Shut Down dialog, and the
        # Finder drawing in looks like the dialog (seen on the N5105 after a hostd
        # restart 57 s into a boot: the confirming "r" selected an icon). Only an
        # older session counts as being at the Finder; a younger one is probed.
        started = st.get("started_mono")
        age = mono() - started if isinstance(started, (int, float)) else None
        self.finder_seen = not (self.machine and self.machine.osx) and (age is None or age >= self.cfg.adopt_settled_s)
        self.osx_finder_visible = False
        self.started_seen = False
        self.finder_event = asyncio.Event()
        self.up_event = asyncio.Event()
        if self.finder_seen:
            self.finder_event.set()
            self.up_event.set()
        self.t_start = started if age is not None else mono()
        self.content_mono = self.t_start
        self.started_wall = wall()
        self.exit_event = asyncio.Event()
        self.log.event("guest_adopted", session=self.session, pid=handle.pid)
        self._adopt_hardware(self.session)
        self._place(self.session, handle, None)
        await self.app.show_scene("guest", reveal="first-content")
        self._spawn(self._monitor(handle), "guest-monitor")
        self._spawn(self._qmp_session(self.session), "qmp-session")
        return True

    def _place(self, session: str, handle, audio_out: dict | None) -> None:
        """Board hooks of a running guest: QEMU's threads on the fast cores (cpuplace.py).
        A no-op on PCs; never stops a launch."""
        def qemu_pid():
            st = handle.status() or {}
            return st.get("pid") if st.get("state") in ("starting", "running") else None
        try:
            self.app.cpuplace.guest_started(session, qemu_pid)
        except Exception:  # noqa: BLE001
            log.exception("board hooks at launch")

    def _adopt_hardware(self, session: str) -> None:
        """After a hostd restart: the battery bridge and the keys follow what the
        adopted session was started with (its argv.json), not this hostd's defaults."""
        from .battery import pmu_in_argv
        from .keys import buttons_in_argv
        from .machines import verify_ndrv_overlay
        from .quadra import in_argv as m68k_in_argv
        argv: list[str] = []
        ndrv = None
        try:
            spec = read_json(self.paths.argv_json, 1 << 20)
            if spec.get("session") == session:
                argv = [str(a) for a in spec.get("argv") or []]
                overlay = (spec.get("package") or {}).get("overlay") or {}
                if overlay.get("dir"):
                    ndrv = verify_ndrv_overlay(Path(overlay["dir"]))
        except Exception as exc:
            log.info("adopted session's launch arguments unreadable: %s", exc)
        # A running q800 (a 68k Mac): no PMU or buttons in its argv, so the host keeps its keys.
        self.family = QUADRA800 if m68k_in_argv(argv) else \
            (self.machine.family if self.machine is not None and not argv else POWERMAC)
        self.app.battery.launched(session, pmu_in_argv(argv), adopted=True)
        self.app.keys.launched(session, buttons_in_argv(argv), ndrv)

    # -- disk preparation --------------------------------------------------------
    def _prep(self, machine) -> dict:
        """The guest preparation calls for ``machine``'s kind: the Mac OS 9 (HFS+) ones, or a
        68k Mac's (rbguestprep's 68k module through guestprep.GuestPrep.for_family)."""
        gp = self.app.guestprep
        if getattr(machine, "osx", False):
            async def unavailable(_image):
                return {"result": UNAVAILABLE, "reason": "Mac OS X manages its own disk and drivers"}
            return {"apply": unavailable, "repair": unavailable, "version": lambda: None}
        if getattr(machine, "quadra", False):
            if hasattr(gp, "for_family"):
                return gp.for_family(QUADRA800)

            async def unavailable(_image):
                return {"result": UNAVAILABLE, "reason": "no 68k preparation"}
            return {"apply": unavailable, "repair": unavailable, "version": lambda: None}
        return {"apply": gp.apply, "repair": gp.repair, "version": gp.version}

    async def run_prep(self, machine, at: str) -> dict:
        """rbguestprep.apply on a copy that replaces the disk only on success."""
        prev = machine.meta.get("prep") or {}
        attempts = int(prev.get("attempts", 0)) + 1
        calls = self._prep(machine)
        version = calls["version"]()
        res = await calls["apply"](machine.disk)
        self.log.event("guest_prep", machine=machine.id, at=at, attempt=attempts, version=version, **res)
        if res["result"] == UNAVAILABLE:
            return res
        machine.update_meta(prep={"result": res["result"], "at": wall(), "attempts": attempts,
                                  "version": version if res["result"] == "applied" else None,
                                  "report": res.get("report"), "error": res.get("error")})
        if res["result"] == "applied":
            # apply() starts with fsck -fy, so the volume is consistent again.
            machine.set_state(repaired_at=wall())
            self._learn_os_version(machine, res)
        return res

    @staticmethod
    def _learn_os_version(machine, res: dict) -> None:
        """A Mac without an OS version (a blank disk Mac OS was installed on from a CD, an
        uploaded archive) takes the one preparation read from its System file (68k Macs:
        rbguestprep.prep68k's report), so Startup Disk can name it."""
        osv = (res.get("report") or {}).get("os_version")
        if machine.meta.get("os_version") or not isinstance(osv, str) or not re.fullmatch(r"\d{1,2}(\.\d{1,2}){0,2}", osv):
            return
        machine.update_meta(os_version=osv)

    async def upgrade_prep(self, machine, have: int, want: int) -> dict:
        """Re-run rbguestprep.apply on a disk prepared by an older library (apply is
        idempotent; the guest is stopped). A failure keeps the earlier preparation."""
        prep = dict(machine.meta.get("prep") or {})
        res = await self._prep(machine)["apply"](machine.disk)
        self.log.event("guest_prep", machine=machine.id, at="upgrade", from_version=have, version=want, **res)
        if res["result"] == "applied":
            machine.update_meta(prep={"result": "applied", "at": wall(), "attempts": 1, "version": want,
                                      "upgraded_from": have, "report": res.get("report"), "error": None})
            machine.set_state(repaired_at=wall())
            self._learn_os_version(machine, res)
        elif res["result"] != UNAVAILABLE:
            up = prep.get("upgrade") or {}
            tries = (int(up.get("attempts", 0)) if up.get("version") == want else 0) + 1
            prep["upgrade"] = {"version": want, "attempts": tries, "result": res["result"],
                               "error": res.get("error")}
            machine.update_meta(prep=prep)
        return res

    async def prepare_disk(self, machine, no_system: bool = False) -> dict | None:
        """Before a launch (guest stopped).  Returns a prep failure to tell the user about once.
        A blank disk without Mac OS yet (``no_system``: it starts up from a CD) has nothing
        to prepare: its preparation runs at the first start after Mac OS is installed."""
        if getattr(machine, "osx", False):
            # OS X owns its HFS+/journal recovery and native drivers. The classic
            # preparation library must never mount, repair or personalize its volume.
            await self.app.graphics.prepare_launch(machine)
            await self.app.ensure_guest_isolation()
            return None
        if no_system:
            return None
        notice = None
        just_prepared = False
        prep = machine.meta.get("prep") or {}
        if prep.get("result") != "applied" and int(prep.get("attempts", 0)) < self.cfg.prep_max_attempts:
            res = await self.run_prep(machine, "launch")
            # A fresh preparation (it repairs the volume itself) still needs the launch
            # hooks below; returning here skipped them on a disk's first launch.
            just_prepared = res["result"] == "applied"
            if res["result"] == "failed" and int(prep.get("attempts", 0)) == 0:
                notice = res
        elif prep.get("result") == "applied":
            # Prepared by an older rbguestprep (a disk imported before an update, e.g. the
            # Software Update check that left Shut Down stuck): bring it up to date once.
            want = self._prep(machine)["version"]()
            have = int(prep.get("version") or 1)
            up = prep.get("upgrade") or {}
            if want is not None and have < want and not (
                    up.get("version") == want and int(up.get("attempts", 0)) >= self.cfg.prep_max_attempts):
                await self.upgrade_prep(machine, have, want)
        st = machine.state()
        if not just_prepared and not st.get("clean", True):
            pre_fsck = await self._create_pre_fsck_backup(machine, st)
            res = await self._prep(machine)["repair"](machine.disk)
            self.log.event("disk_repair", machine=machine.id, why="previous session did not end cleanly",
                           last_exit=(st.get("last_exit") or {}).get("reason"),
                           pre_fsck=pre_fsck, **res)
            if res["result"] == "ok":
                machine.set_state(repaired_at=wall())
            elif res["result"] == "failed":
                recovery = self._build_corrupt_disk_recovery(machine, res, pre_fsck)
                self.corrupt_disk_recovery = recovery
                log_fields = {("machine_name" if k == "name" else k): v for k, v in recovery.items()}
                self.log.event("corrupt_disk_recovery", **log_fields)
                return {"corrupt_disk_recovery": recovery}
        if getattr(machine, "quadra", False):
            # A 68k Mac: none of the Mac OS 9 hooks (AirPort, ATI, Control Strip, Platinum
            # bundle from an HFS+ disk); the presenter's startup geometry is the standard card's.
            await self.app.graphics.sync_startup(machine)
            await self.app.ensure_guest_isolation()      # Mac OS can't reach this computer's services
            return notice
        await self.app.airport.prepare_launch(machine)   # AirPort hook: TCP/IP matches the adapter
        await self.app.graphics.prepare_launch(machine)  # ATI Radeon: Mac OS has ATI's drivers
        await self.app.graphics.place_control_strip(machine)   # the strip follows a new Mac screen
        await self.app.ensure_guest_isolation()          # Mac OS can't reach this computer's services
        await self.app.platinum.ensure(machine.disk)     # Platinum web/panel assets, once, disk idle
        if not getattr(machine, "osx", False) and not getattr(machine, "quadra", False):
            applied = getattr(getattr(self.app, "settings", None), "applied", {}) or {}
            if applied.get("printer.enabled", False) and applied.get("printer.mac_auto_setup", True):
                gp = getattr(self.app, "guestprep", None)
                if gp is not None and hasattr(gp, "ensure_printer"):
                    await gp.ensure_printer(machine.disk)
        return notice

    async def _copy_pre_fsck_disk(self, src: Path, dst: Path) -> str:
        """Create a reflink (or sparse/regular fallback) disk snapshot before fsck."""
        from .util import run as run_cmd
        for flags, method in ((["--reflink=always"], "reflink"),
                              (["--reflink=auto", "--sparse=always"], "reflink-auto"),
                              (["--reflink=auto"], "reflink-auto")):
            rc, _, _ = await run_cmd(["cp", *flags, str(src), str(dst)], timeout=3600)
            if rc == 0:
                return method
            try:
                dst.unlink()
            except FileNotFoundError:
                pass
        await asyncio.to_thread(shutil.copy2, src, dst)
        return "copy"

    async def _create_pre_fsck_backup(self, machine, state: dict) -> dict | None:
        """Checkpoint-compatible ``pre-fsck`` reflink snapshot before running repair() (#48)."""
        cp_root = getattr(self.paths, "checkpoints", None)
        if cp_root is None or not getattr(machine, "disk", None) or not Path(machine.disk).is_file():
            return None
        from .util import atomic_write_json
        base = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        d = Path(cp_root) / machine.id
        d.mkdir(parents=True, exist_ok=True)
        cp_id, n = base, 1
        while (d / cp_id).exists():
            n += 1
            cp_id = f"{base}-{n}"
        staging = d / f".{cp_id}.tmp"
        staging.mkdir(parents=True, exist_ok=True)
        try:
            method = await self._copy_pre_fsck_disk(Path(machine.disk), staging / "disk.img")
            mj = Path(machine.dir) / "machine.json"
            if mj.is_file():
                shutil.copy2(mj, staging / "machine.json")
            st_disk = (staging / "disk.img").stat()
            settings = dict(getattr(getattr(self.app, "settings", None), "applied", {}) or {})
            rev = int(getattr(getattr(self.app, "settings", None), "revision", 0) or 0)
            meta = {
                "schema": 1,
                "id": cp_id,
                "label": "Pre-repair backup",
                "kind": "pre-fsck",
                "created": wall(),
                "machine": machine.id,
                "disk_bytes": st_disk.st_size,
                "allocated_bytes": getattr(st_disk, "st_blocks", 0) * 512,
                "method": method,
                "settings": settings,
                "settings_revision": rev,
                "package": None,
                "last_exit": state.get("last_exit"),
            }
            atomic_write_json(staging / "checkpoint.json", meta)
            os.rename(staging, d / cp_id)
            return {"id": cp_id, "label": "Pre-repair backup", "kind": "pre-fsck",
                    "method": method, "path": str(d / cp_id)}
        except Exception as exc:
            log.warning("pre-fsck backup skipped for %s: %s", machine.id, exc)
            return None
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    def _build_corrupt_disk_recovery(self, machine, res: dict, pre_fsck: dict | None) -> dict:
        last_clean = None
        fresh_install = None
        cps = getattr(self.app, "checkpoints", None)
        entries: list[dict] = []
        if cps is not None and hasattr(cps, "list"):
            try:
                entries = list(cps.list(machine.id))
            except Exception:
                entries = []
        elif getattr(self.paths, "checkpoints", None):
            from .util import read_json
            d = Path(self.paths.checkpoints) / machine.id
            if d.is_dir():
                for c in sorted(d.iterdir(), reverse=True):
                    try:
                        entries.append(read_json(c / "checkpoint.json", 1024 * 1024))
                    except (OSError, ValueError):
                        continue
        for item in entries:
            label = str(item.get("label") or "")
            low = label.strip().lower()
            if low in ("pre-fsck", "pre-repair backup") or item.get("kind") == "pre-fsck":
                continue
            if last_clean is None:
                last_clean = item.get("id")
            if fresh_install is None and low == "fresh install":
                fresh_install = item.get("id")
        return {
            "machine": machine.id,
            "name": getattr(machine, "name", machine.id),
            "repair": res,
            "pre_fsck_checkpoint": pre_fsck,
            "last_clean_checkpoint": last_clean,
            "fresh_install_checkpoint": fresh_install,
        }

    async def _start_startup_chime(self, machine=None, audio_out: dict | None = None):
        """Start the configured Macintosh startup chime asynchronously and return its player handle."""
        if not self.app.audio_capable():
            return None
        settings = dict(getattr(getattr(self.app, "settings", None), "applied", {}) or {})
        if not settings.get("audio.enabled", True):
            return None
        if getattr(getattr(self.app, "laptop", None), "muted", False):
            return None
        m = machine if machine is not None else self.active_machine()
        family = getattr(m, "family", POWERMAC) if m is not None else POWERMAC
        chime_setting = str(settings.get("audio.startup_chime", "auto") or "auto")
        output_pref = str(settings.get("audio.output", "auto") or "auto")
        vol = settings.get("audio.volume")
        out = audio_out
        if out is None and hasattr(self.app, "choose_audio_output"):
            out = await asyncio.to_thread(self.app.choose_audio_output)
        prefer = "analog" if getattr(self.app.platform, "is_laptop", False) else "hdmi"
        start_fn = getattr(hostaudio, "start_startup_chime", None)
        if callable(start_fn):
            proc = await asyncio.to_thread(
                start_fn,
                family,
                chime_setting,
                output=output_pref,
                audio_out=out,
                prefer=prefer,
                volume=vol,
                muted=False,
            )
            if proc is not None:
                self.chime_played = True
            return proc
        ok = await asyncio.to_thread(
            hostaudio.play_startup_chime,
            family,
            chime_setting,
            output=output_pref,
            audio_out=out,
            prefer=prefer,
            volume=vol,
            muted=False,
        )
        if ok:
            self.chime_played = True
        return None

    async def _finish_startup_chime(self, proc, timeout: float = 6.0) -> None:
        """Let an active startup chime finish playing before QEMU opens the audio device."""
        if proc is None:
            return
        wait_fn = getattr(proc, "wait", None)
        if not callable(wait_fn):
            return
        def _wait():
            try:
                wait_fn(timeout=timeout)
            except Exception:  # noqa: BLE001 -- never block or fail guest launch on chime drain
                pass
        await asyncio.to_thread(_wait)

    async def play_startup_chime(self, machine=None, audio_out: dict | None = None, *, wait: bool = False) -> bool:
        """Play the configured Macintosh startup chime on the active output."""
        proc = await self._start_startup_chime(machine, audio_out=audio_out)
        if proc is None:
            return self.chime_played
        if wait:
            await self._finish_startup_chime(proc)
        return True

    async def test_sound(self, output: str | None = None) -> dict:
        """Play a short test sound on the specified or active audio output."""
        settings = dict(getattr(getattr(self.app, "settings", None), "applied", {}) or {})
        m = self.active_machine()
        family = getattr(m, "family", POWERMAC) if m is not None else POWERMAC
        chime_setting = str(settings.get("audio.startup_chime", "auto") or "auto")
        req_output = output if output is not None else str(settings.get("audio.output", "auto") or "auto")
        muted = bool(getattr(getattr(self.app, "laptop", None), "muted", False))
        prefer = "analog" if getattr(self.app.platform, "is_laptop", False) else "hdmi"
        out = None
        if output is None and hasattr(self.app, "choose_audio_output"):
            out = await asyncio.to_thread(self.app.choose_audio_output)
        return await asyncio.to_thread(
            hostaudio.test_sound,
            req_output,
            audio_out=out,
            prefer=prefer,
            chime_setting=chime_setting,
            family=family,
            muted=muted,
            run_dir=getattr(self.paths, "run", None),
        )

    async def usb_pointer(self, machine) -> tuple[bool, int | None]:
        """pointer.py: a USB mouse for the right button and wheel when the disk has
        USB Overdrive (read-only catalog check, about 10 ms; the guest is stopped)."""
        mode = getattr(self.cfg, "usb_mouse", "auto")
        mode = mode if mode in pointer.MODES else "auto"
        present = None
        if mode == "auto" and hasattr(self.app.guestprep, "usb_overdrive_present"):
            present = await asyncio.to_thread(self.app.guestprep.usb_overdrive_present, machine.disk)
        use = pointer.wanted(mode, present)
        wheel = pointer.WHEEL_PER_REPORT if use and pointer.package_wheel_per_report(self.paths.qemu_package) \
            else None
        self.log.event("pointer_devices", machine=machine.id, usb_mouse=use, mode=mode,
                       usb_overdrive=present, wheel_per_report=wheel)
        return use, wheel

    async def mac_cities(self, machine, settings: dict) -> list | None:
        """macclock.py: Date & Time's cities on the stopped disk (read-only, about 20 ms), so
        Mac OS's location is one of them (Time Synchronizer accepts nothing else)."""
        from . import macclock
        cities = None
        if hasattr(self.app.guestprep, "mac_cities"):
            cities = await asyncio.to_thread(self.app.guestprep.mac_cities, machine.disk)
        zone = settings.get("system.timezone") or "UTC"
        city = macclock.choose_city(cities, zone) if cities else None
        self.log.event("mac_location", machine=machine.id, zone=zone, cities=len(cities) if cities else 0,
                       city=city["name"] if city else None)
        return cities

    # -- CDs -------------------------------------------------------------------
    def request_cd_start(self, machine_id: str, source: str | None) -> None:
        """Start Up from CD: the next start of ``machine_id`` (normally the one this request
        causes) starts up from ``source`` (a CD source id, cd_sources), once."""
        self.cd_request = {"machine": machine_id, "source": source, "at": mono()}

    def cd_sources(self, drives: list[dict] | None = None) -> list[dict]:
        """CDs Mac OS can start up from: discs in this computer's CD-ROM drives, then the
        media library's CD images.  ``{id, label, kind: drive|image, path, drive}``"""
        if drives is None:
            drives = self.app.optical.list()
        out = [{"id": f"drive:{d['id']}", "label": d["label"], "title": d["volume_name"] or "Untitled CD",
                "where": d["kind"], "kind": "drive", "path": d["path"], "drive": True, "usb": d["usb"]}
               for d in drives if d["disc"]]
        for it in self.app.media.list():
            p = self.app.media.path(it["id"]) if it["can_insert"] else None
            if p is not None:
                out.append({"id": it["id"], "label": it["label"], "title": it["label"], "where": "CD image",
                            "kind": "image", "path": str(p), "drive": False})
        return out

    def resolve_cd(self, source: str | None, drives: list[dict]) -> dict | None:
        """A CD source id, as it is now: a drive id finds that drive's disc (or, if that drive
        is gone or empty, the first disc in any drive: drives renumber when replugged); a
        library name its CD image.  None: no such CD."""
        found = self.cd_sources(drives)
        if source and source.startswith("drive:"):
            return next((c for c in found if c["id"] == source), None) or \
                next((c for c in found if c["drive"]), None)
        return next((c for c in found if c["id"] == source and not c["drive"]), None) if source else None

    async def cd_plan(self, machine) -> dict:
        """The CDs of the start of ``machine``: {want (it must start up from a CD), boot (the
        CD it starts up from, or None), drive (this computer's disc for Mac OS's second
        CD-ROM drive), no_system (a blank disk without Mac OS), why}."""
        opt = self.app.optical
        drives = await asyncio.to_thread(opt.list, True) if opt.present() else []
        req, self.cd_request = self.cd_request, None
        if req and (req["machine"] != machine.id or mono() - req["at"] > CD_REQUEST_TTL):
            self.log.event("cd_start_request_dropped", machine=machine.id, requested_for=req["machine"])
            req = None
        no_system = await asyncio.to_thread(machine.needs_cd)
        want, boot, why = False, None, None
        if req:
            want, why = True, "requested"
            boot = self.resolve_cd(req["source"], drives)
        elif no_system:
            want, why = True, "no-system"
            boot = self.resolve_cd(machine.meta.get("cd_start"), drives) if machine.meta.get("cd_start") else None
            boot = boot or self.resolve_cd("drive:", drives)
        disc = next((d for d in drives if d["disc"]), None)
        drive = None
        if disc and not (boot and boot["drive"] and boot["path"] == disc["path"]):
            drive = Path(disc["path"])
        return {"want": want, "boot": boot, "drive": drive, "no_system": no_system, "why": why,
                "drives": [{k: d[k] for k in ("id", "usb", "status")} for d in drives]}

    # -- start -------------------------------------------------------------------
    async def start_guest(self, reason: str) -> bool:
        async with self.launch_lock:
            if self.active:
                return True
            if reason in ("boot", "user", "force-restart", "guest-reset", "settings"):
                self.attempts = 0
            self.relaunching = True
            try:
                return await self._start_locked(reason)
            finally:
                self.relaunching = False

    @property
    def launch_pending(self) -> bool:
        """RetroBoot is getting Mac OS's start ready (launch hooks: disk check, first-launch
        preparation) and QEMU is not running yet."""
        return not self.active and (self.boot_pending or self.launch_lock.locked())

    async def _cancel_start(self, job: Job) -> bool:
        """Shut Down while RetroBoot prepares Mac OS's start: the hooks finish (they may be
        writing the disk), then Mac OS is not started.  False if it started meanwhile (the
        ordinary clean path follows)."""
        self.start_cancel = True
        self.log.event("guest_start_cancel_requested", job=job.id)
        job.update(0.2, "Mac OS hasn't started yet. Stopping its start…")
        while self.launch_pending:
            await asyncio.sleep(0.2)
        if self.active:
            self.start_cancel = False
            return False
        self.start_cancel = False
        return True

    async def _start_locked(self, reason: str) -> bool:
        self.unavailable = None                    # only ever about the start being made now
        self.corrupt_disk_recovery = None
        self.manual_resize_notice = None
        if self.start_cancel:
            self.start_cancel = False
            self.log.event("guest_start_cancelled", reason=reason, why="shut down requested before Mac OS started")
            return False
        engine = getattr(self.app, "apply_engine", None)
        if engine is not None:
            try:   # restart-class changes made while Mac OS ran (e.g. Startup Disk's Use This Mac)
                await engine.apply_pending_restart(reason)
            except Exception as exc:   # never let it stop Mac OS from starting
                self.log.event("pending_restart_apply_failed", reason=reason, error=str(exc)[:200])
        machine = self.active_machine()
        problems = self.app.machines.validate(machine)
        if machine is not None:
            why = self.app.platform.machine_unavailable(machine.family)
            if why:
                problems.insert(0, why)
            if machine.quadra and self.app.platform.is_zero2w \
                    and self.app.settings.applied.get("machine.quadra_ram_mb", 128) > 128:
                problems.insert(0, "Choose 128 MB or less of Mac memory for the Raspberry Pi Zero 2 W image.")
        if problems:
            self.log.event("guest_start_refused", reason=reason, problems=problems)
            await self.app.panel.show_setup() if not self.app.machines.list() else \
                await self.app.panel.show_failure("Mac OS could not start.", problems[0], None)
            return False
        plan = await self.cd_plan(machine)
        self.needs_cd = None
        if plan["want"] and plan["boot"] is None:
            # A blank disk without Mac OS and no CD to install it from (or Start Up from CD
            # with the CD gone): say how to go on instead of a firmware prompt.
            self.needs_cd = {"machine": machine.id, "name": machine.name, "blank": plan["no_system"]}
            self.log.event("guest_start_refused", reason=reason, machine=machine.id,
                           problems=["no Mac OS on the disk and no CD" if plan["no_system"] else "the CD is gone"],
                           drives=plan["drives"])
            await self.app.panel.show_needs_cd(machine.name, plan["no_system"])
            return False
        self.attempts += 1
        self.failure = None
        self.chime_played = False
        # A start from a CD has its own timing: it never teaches the disk's startup model.
        await self.app.startup.on_start(reason, self.attempts, f"{machine.id}@cd" if plan["boot"] else machine.id)
        await self.app.show_scene("splash")
        self.app.panel.forget()
        notice = await self.prepare_disk(machine, no_system=plan["no_system"])
        if notice is not None and notice.get("corrupt_disk_recovery"):
            recovery = notice["corrupt_disk_recovery"]
            self.corrupt_disk_recovery = recovery
            self.failure = {"reason": "corrupt_disk_recovery",
                            "message": "Mac OS's startup disk could not be repaired safely.",
                            "recovery": recovery}
            handler = getattr(self.app, "on_corrupt_disk_recovery", None)
            if handler is not None:
                res_cb = handler(recovery)
                if inspect.isawaitable(res_cb):
                    await res_cb
            else:
                show = getattr(self.app.panel, "show_corrupt_disk_recovery", None)
                if show is not None:
                    res_cb = show(recovery)
                    if inspect.isawaitable(res_cb):
                        await res_cb
                else:
                    await self.app.panel.show_failure(
                        "Mac OS disk repair failed.",
                        "Restore Last Clean Checkpoint or Reset to Fresh Install before starting Mac OS.",
                        None,
                    )
            return False
        if self.start_cancel:
            # Shut Down was asked for while the launch hooks ran (disk check, first-launch
            # preparation): they finished, and Mac OS is not started at all.
            self.start_cancel = False
            self.log.event("guest_start_cancelled", reason=reason, why="shut down requested before Mac OS started")
            return False
        if self.boot_hold and reason == "boot":
            self.boot_hold = False
            # Escape was pressed while RetroBoot was still getting ready (before QEMU
            # started): show the startup options instead of starting Mac OS.
            self.log.event("guest_start_held", reason=reason, why="escape during startup")
            await self.app.panel.show_startup_options()
            return False
        if notice is not None:
            eid = await self.app.diagnostics.capture_evidence("guest_prep_failed", notice.get("error") or "",
                                                             {"machine": machine.id})
            go = asyncio.Event()

            async def proceed():
                go.set()
            await self.app.panel.message(
                "caution", "RetroBoot could not prepare Mac OS's disk.",
                f"Mac OS will start normally, but may check its disk after an unexpected stop. Evidence {eid}.",
                then=proceed, button="Start Mac OS", auto_seconds=20)
            await go.wait()
            self.app.panel.forget()
            await self.app.show_scene("splash")
        from . import graphics as hostgraphics
        pkg_dir = self.paths.qemu_osx_package if machine.osx else self.paths.qemu_package
        settings = hostgraphics.machine_settings(self.app.settings.applied, machine, pkg_dir)
        boot = plan["boot"]
        quadra = getattr(machine, "quadra", False)
        if boot and not quadra and settings.get("machine.cpu") != "7400":
            settings["machine.cpu"] = "7400"     # the CD route is qualified on the G4 only
        display = self.app.presenter.display
        try:
            if quadra:
                # A 68k Mac (quadra.py): its package, ROM and PRAM, and where it starts up from.
                from . import quadra as q
                if not q.package_present(self.paths):
                    raise MachineUnavailable(q.NO_PACKAGE)
                try:
                    await asyncio.to_thread(self.app.package_m68k.verify)
                except MachineError as exc:
                    log.error("68k emulator package: %s", exc)
                    raise MachineUnavailable(q.BAD_PACKAGE) from None
                ndrv = loader = None
                prepared = await asyncio.to_thread(q.prepare, machine, self.paths.qemu_m68k_package, self.app.roms)
                startup = await asyncio.to_thread(q.point_startup, machine, bool(boot))
                if prepared.get("rom") or prepared.get("pram") or startup:
                    self.log.event("quadra_prepared", machine=machine.id, rom=prepared.get("rom"),
                                   pram=prepared.get("pram"), startup_device=startup)
            else:
                await asyncio.to_thread(self.app.package_for(machine).verify)
                ndrv = None if machine.osx else self.app.package.ndrv
                loader = None if machine.osx else self.app.package.loader
                if machine.osx:
                    from . import osx, osx_identity
                    identity = await asyncio.to_thread(osx_identity.refresh, machine, self.paths.apps_lib)
                    self.log.event("osx_identity", machine=machine.id, identity=identity,
                                   source="stopped SystemVersion.plist")
                    supplied = await asyncio.to_thread(osx.prepare, machine, self.paths.qemu_osx_package)
                    if supplied:
                        self.log.event("osx_nvram_created", machine=machine.id)
            lab_qmp = self.paths.lab_qmp if self.cfg.lab_qmp else None
            await self.app.wait_for_sound()      # a sound card still being set up at boot
            audio_capable = self.app.audio_capable()
            audio_out = await asyncio.to_thread(self.app.choose_audio_output) if audio_capable else None
            screamer = (hostaudio.screamer_options(self.app.platform.profile.get("screamer"),
                                                   getattr(self.cfg, "audio_host_lead_ms", -1),
                                                   hostaudio.package_features(self.paths.qemu_package))
                        if audio_capable and not quadra else {})
            if audio_out:
                extra = ({"connector": audio_out.get("connector"), "alsa": audio_out.get("alsa"),
                          "softvol": audio_out.get("softvol")}
                         if audio_out.get("vc4") or audio_out.get("softvol") is not None or audio_out["kind"] == "configured" else {})
                if screamer:
                    extra["screamer"] = screamer
                self.log.event("audio_output", kind=audio_out["kind"], card=audio_out["card_id"],
                               device=audio_out["device"], sink=audio_out.get("sink"),
                               warmed=audio_out.get("warmed"), stage=audio_out.get("stage"), **extra)
                want_mixer = audio_out.get("mixer")
                if (audio_out.get("card_id") != self.app.mixer.card
                        or (want_mixer and want_mixer != self.app.mixer.control)
                        or audio_out.get("pw_sink") != getattr(self.app.mixer, "pw_sink", None)
                        or not self.app.mixer.available):
                    # Re-probe whenever the chosen output card/control changes (including x86
                    # laptops falling back from HDMI to built-in analog speakers, Pi, Gemini,
                    # and explicit audio.output switches).
                    pw_sink = audio_out.get("pw_sink")
                    kw = {"pw_sink": pw_sink} if pw_sink else {}
                    await self.app.mixer.probe(audio_out.get("card_id"), want_mixer, detect_switch=True, **kw)
                    if self.app.mixer.available:
                        if getattr(getattr(self.app, "laptop", None), "muted", False):
                            vol = int(settings.get("audio.volume", 70))
                            await self.app.mixer.set_volume(vol, unmute=False)
                            await self.app.mixer.set_mute(True)
                        else:
                            await self.app.apply_engine.push_volume()
            chime_proc = None
            if not plan["no_system"]:
                if type(self).play_startup_chime is Supervisor.play_startup_chime:
                    chime_proc = await self._start_startup_chime(machine, audio_out=audio_out)
                else:
                    await self.play_startup_chime(machine, audio_out=audio_out)
            self.chime_played = True
            if quadra or machine.osx:   # OS X uses native drivers; no OS9 device hooks
                power_socket = buttons = cities = None
                usb_mouse, wheel = False, None
            else:
                power_socket = self.app.battery.launch_socket(settings, boot_cd=bool(boot))   # battery passthrough
                buttons = self.app.keys.launch_buttons(settings, pmu=power_socket is not None,
                                                       boot_cd=bool(boot))   # keys.py hook
                usb_mouse, wheel = (False, None) if boot else await self.usb_pointer(machine)   # right button, wheel
                cities = await self.mac_cities(machine, settings)        # Mac OS's location (macclock)
            osx_48k = (getattr(machine, "guest_os", None) == "macosx-10.4"
                       and (hostaudio.supports_48khz(audio_out) or machine.id == self.cfg.osx_48k_machine))
            argv = build_argv(machine, settings, self.paths, display,
                              LaunchOptions(display_backend=self.cfg.display_backend,
                                            audio_capable=audio_capable,
                                            osx_48k_output=osx_48k,
                                            audio_device=audio_out["alsa"] if audio_out else None,
                                            ndrv_dir=Path(ndrv["dir"]) if ndrv else None,
                                            loader_dir=Path(loader["dir"]) if loader else None,
                                            lab_qmp=lab_qmp,
                                            cd_image=self.app.media.cd_for_launch(settings, self.log.event),
                                            boot_cd=Path(boot["path"]) if boot else None,
                                            boot_cd_drive=bool(boot and boot["drive"]),
                                            cd_drive=plan["drive"],
                                            power_socket=power_socket, buttons=buttons,
                                            usb_mouse=usb_mouse, usb_wheel_per_report=wheel,
                                            mac_cities=cities,
                                            tb_size_mb=self.app.platform.tcg_tb_size_mb,
                                            screamer=screamer))
        except MachineUnavailable as exc:
            # A 68k Mac without its emulator package or its ROM: say so plainly.
            self.unavailable = str(exc)
            eid = await self.app.diagnostics.capture_evidence("launch_config", str(exc))
            self.log.event("guest_start_failed", reason=reason, error=str(exc), evidence=eid,
                           family=machine.family, unavailable=True)
            await self.app.panel.show_failure("Mac OS could not start.", str(exc), eid)
            return False
        except (MachineError, OSError) as exc:
            eid = await self.app.diagnostics.capture_evidence("launch_config", str(exc))
            self.log.event("guest_start_failed", reason=reason, error=str(exc), evidence=eid)
            await self.app.panel.show_failure("Mac OS could not start.",
                                              "RetroBoot's emulator files or machine settings are not valid.", eid)
            return False
        session = str(uuid.uuid4())
        spec = launch_spec(machine, argv, self.paths, session, settings,
                           self.cfg.run_as_user, self.cfg.display_backend, ndrv=ndrv, lab_qmp=lab_qmp,
                           loader=loader)
        if machine.id == self.cfg.osx_fast_cores_machine and spec.get("egl_render_node") and \
                (spec.get("guest_os"), spec.get("os_build")) == ("macosx-10.4", "8S165") and \
                not self.app.cpuplace.plan.enabled:
            spec["osx_fast_cores"] = True
        self.paths.share.mkdir(parents=True, exist_ok=True)
        if settings.get("share.enabled"):
            # A Finder folder an earlier share driver left visible gets Mac OS's own flag
            from .share import SharedFolder
            try:
                hidden = SharedFolder(self.paths.share).hide_finder_folders()
            except OSError as exc:
                hidden = []
                log.warning("share: Finder folders not checked: %s", exc)
            if hidden:
                self.log.event("share_finder_folders_hidden", names=hidden)
            # The share driver's resource fork caches of earlier boots (never read again)
            try:
                folders, freed = SharedFolder(self.paths.share).clear_driver_cache()
            except OSError as exc:
                folders, freed = 0, 0
                log.warning("share: driver cache not cleared: %s", exc)
            if folders:
                self.log.event("share_driver_cache_cleared", folders=folders, bytes=freed)
        atomic_write_json(self.paths.argv_json, spec)
        before = machine.state()
        self.launch_clean = (bool(before.get("clean", True)), before.get("dirty_since"))
        self.paused_before_content = False
        self.content_deadline = content_deadline(
            before.get("content_s"), self.cfg.guest_content_timeout, self.cfg.guest_content_factor,
            self.cfg.guest_content_floor)
        self.launch_disk = disk_stamp(machine.disk)   # after prep and the AirPort hook wrote it
        machine.set_state(clean=False, dirty_since=wall(), running_session=session)
        self.machine = machine
        self.session = session
        self.family = machine.family
        self.app.battery.launched(session, power_socket is not None)
        self.app.keys.launched(session, buttons, ndrv)
        self.app.graphics.launched("ati-radeon" if any("ati-vga" in a for a in argv) else "standard")
        self.cd_session = {"boot": boot, "drive": str(plan["drive"]) if plan["drive"] else None}
        if boot:
            self.log.event("guest_cd_start", session=session, machine=machine.id, why=plan["why"],
                           cd=boot["id"], drive=boot["drive"])
        self.state = STARTING
        self.content_seen = self.revealed = self.finder_seen = self.started_seen = self.paused = False
        self.osx_finder_visible = False
        self.finder_mono = None
        self.finder_frame = None
        self.content_mono = None
        self.guest_surface = None
        self.finder_timeout_logged = False
        self.handoff = None
        # The accelerated boot (icount) is a Power Mac std-VGA feature: never on a 68k Mac.
        self.boot_speed = "realtime" if quadra or machine.osx else settings["machine.boot_speed"]
        self.exit_event = asyncio.Event()
        self.content_event = asyncio.Event()
        self.finder_event = asyncio.Event()
        self.up_event = asyncio.Event()
        self.t_start = mono()
        self.started_wall = wall()
        self.probe_until = self.t_start + self.cfg.finder_wait_timeout
        await self._finish_startup_chime(chime_proc)
        try:
            handle = await self.app.runner.start(session)
        except Exception as exc:
            self.state = STOPPED
            eid = await self.app.diagnostics.capture_evidence("launch_failed", str(exc))
            self.log.event("guest_start_failed", reason=reason, error=str(exc), evidence=eid)
            await self.app.panel.show_failure("Mac OS could not start.", "The emulator could not be started.", eid)
            return False
        self.handle = handle
        self._place(session, handle, audio_out)
        g = guest_geometry(display, settings["display.guest_size"], settings.get("display.pixel_scale", "auto"))
        if "-g" in argv:      # the boot framebuffer actually used (ATI's startup mode with the card)
            g = tuple(int(v) for v in argv[argv.index("-g") + 1].split("x")[:2])
        self.log.event("guest_start", session=session, machine=machine.id, reason=reason, attempt=self.attempts,
                       boot_speed=self.boot_speed, geometry=f"{g[0]}x{g[1]}",
                       display=(display or {}).get("mode"),
                       ndrv="native-package" if machine.osx else (ndrv or {}).get("sha256", "package")[:16],
                       lab_qmp=bool(lab_qmp) or None,
                       ndrvloader=None if machine.osx else (loader or {}).get("sha256", "package")[:16],
                       cd=self.app.media.inserted, content_deadline_s=self.content_deadline,
                       start_cd=boot["kind"] if boot else None, cd_drive=bool(plan["drive"]) or None,
                       argv_sha256=hashlib.sha256(json.dumps(argv).encode()).hexdigest()[:16],
                       **({"family": QUADRA800} if quadra else {}))
        await self.app.show_scene("guest", reveal="first-content")
        await self.app.startup.on_launched()
        self._spawn(self._monitor(handle), "guest-monitor")
        self._spawn(self._qmp_session(session), "qmp-session")
        self._arm_content_timer()
        if self.boot_hold and reason == "boot":
            # Escape arrived while the launch was under way (after the disk prep check):
            # pause the new guest at once and show the boot options.
            self.boot_hold = False
            self._spawn(self._hold_after_launch(session), "boot-hold")
        return True

    async def _release_cd_drives(self) -> None:
        """QEMU locks a drive's door while Mac OS has its disc mounted; after QEMU the drive's
        own eject button works again."""
        cds, self.cd_session = self.cd_session, None
        if not cds:
            return
        boot = cds.get("boot") or {}
        paths = {p for p in (cds.get("drive"), boot.get("path") if boot.get("drive") else None) if p}
        for p in paths:
            try:
                await asyncio.to_thread(self.app.optical.unlock, p)
            except Exception:  # noqa: BLE001 -- never in the way of the exit
                log.exception("CD-ROM door not released")

    async def _hold_after_launch(self, session: str) -> None:
        self.log.event("guest_start_held", reason="boot", why="escape during launch", session=session)
        if session == self.session and await self.pause_for_options():
            await self.app.panel.show_boot_options()

    def _arm_content_timer(self) -> None:
        if self._content_timer:
            self._content_timer.cancel()
        session = self.session
        self._content_timer = asyncio.get_running_loop().call_later(
            self.content_deadline,
            lambda: self._spawn(self._content_timeout(session), "content-timeout"))

    async def _content_timeout(self, session: str) -> None:
        if session != self.session or self.content_seen or self.state != STARTING:
            return
        eid = await self.app.diagnostics.capture_evidence(
            "no_guest_content", f"no genuine guest content within {self.content_deadline:.0f} s",
            {"attempt": self.attempts, "deadline_s": self.content_deadline})
        self.log.event("guest_content_timeout", session=session, attempt=self.attempts, evidence=eid,
                       deadline_s=self.content_deadline)
        self.app.startup.on_failed()
        self.intent = "retry" if self.attempts < 2 else "failed"
        self.failure = {"evidence": eid, "message": "Mac OS did not start in time."}
        if self.handle:
            await self.handle.stop()

    # -- monitoring ----------------------------------------------------------------
    async def _monitor(self, handle) -> None:
        try:
            exit_info = await handle.wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            exit_info = {"code": 1, "reason": "crash", "detail": f"monitor error: {exc}"}
        if handle is not self.handle:
            return
        await self._on_exit(exit_info)

    async def _qmp_session(self, session: str) -> None:
        q = QMPClient(self.paths.qmp_control)
        try:
            await q.connect(timeout=20.0)
        except (QMPError, OSError, ValueError, asyncio.TimeoutError) as exc:
            self.log.event("qmp_unavailable", session=session, error=str(exc))
            return
        if session != self.session:
            await q.close()
            return
        self.qmp = q
        q.event_handlers.append(lambda msg: self._on_qmp_event(session, msg))
        self.log.event("qmp_connected", session=session, t_since_start=round(mono() - (self.t_start or mono()), 3))
        if self.boot_speed == "accelerated" and not self.content_seen:
            self._spawn(self._handoff_fallback(session), "handoff-fallback")
        if self.cfg.content_probe or self.cfg.finder_probe:
            self._spawn(self._probe(session), "guest-probe")
        if self.content_seen:          # adopted while starting: the last-resort start rule too
            self._watch_started()
        self._spawn(self._watchdog(session, q), "qmp-watchdog")
        self._spawn(self._load_sampler(session), "guest-load")
        if self.cfg.frozen_check and self.cfg.display_backend == "retroboot":
            self.freeze_check = FrozenGuestCheck(self)
            self._spawn(self.freeze_check.run(session), "frozen-check")
        try:
            await q.closed.wait()
        finally:
            if self.qmp is q:
                self.qmp = None

    def _on_qmp_event(self, session: str, msg: dict) -> None:
        """hostd's own QMP connection sees SHUTDOWN before QEMU exits."""
        if msg.get("event") != "SHUTDOWN" or session != self.session:
            return
        reason = (msg.get("data") or {}).get("reason")
        if reason == "guest-shutdown" and self._restart_by_shutdown(session):
            # A 68k Mac's Restart (Shut Down, then a new start): no black, as a guest-reset.
            self.log.event("shutdown_seen", session=session, reason=reason, restart=True)
        elif reason == "guest-shutdown" and (self._late_stop(session) or {}).get("then") == "hold":
            # A Shut Down that keeps RetroBoot on, finished late: the panel follows, no black.
            self.log.event("shutdown_seen", session=session, reason=reason, late=True)
        elif reason == "guest-shutdown" and self.intent != "shutdown-hold" and not self.app.stopping:
            # Mac OS Shut Down (or a host request that powers off): go black now,
            # straight from Mac OS's last frame, instead of after QEMU exits.
            self.log.event("shutdown_seen", session=session, reason=reason, scene="black")
            self._spawn(self.app.show_scene("black"), "scene-black")
        elif reason:
            self.log.event("shutdown_seen", session=session, reason=reason)

    async def _watchdog(self, session: str, q: QMPClient) -> None:
        """QEMU-level responsiveness: QMP query-status must answer within 5 s.

        This covers a wedged emulator main loop (screens, input, QMP all stall);
        a frozen Mac OS whose emulator still runs is the frozen-guest check's job."""
        while session == self.session and not q.closed.is_set():
            try:
                await q.execute("query-status", timeout=self.cfg.qmp_watchdog_timeout_s)
                if self.qmp_ok is not True:
                    self._set_responsive("qmp_ok", True, session)
            except QMPError:
                if q.closed.is_set():
                    return
                if self.qmp_ok is not False:
                    self._set_responsive("qmp_ok", False, session)
            await asyncio.sleep(self.cfg.qmp_watchdog_s)

    async def _handoff_status(self, trigger: str) -> None:
        if not self.qmp:
            return
        try:
            out = await self.qmp.hmp("classicmac-boot-complete")
        except QMPError as exc:
            self.log.event("boot_handoff", session=self.session, trigger=trigger, error=str(exc))
            return
        status = "unknown"
        for line in (out or "").splitlines():
            if "classicmac clock:" in line:
                status = line.split("classicmac clock:", 1)[1].strip()
        self.handoff = {"trigger": trigger, "status": status, "t_since_start": round(mono() - self.t_start, 3)}
        self.log.event("boot_handoff", session=self.session, **self.handoff)

    async def _handoff_fallback(self, session: str) -> None:
        delay = self.cfg.handoff_deadline - (mono() - self.t_start)
        if delay > 0:
            await asyncio.sleep(delay)
        while self.paused and session == self.session:
            await asyncio.sleep(0.5)
        if session == self.session and self.handoff is None and self.active:
            await self._handoff_status("deadline")

    async def dump_frame(self) -> tuple[int, int, bytes] | None:
        if not self.qmp:
            return None
        tmp = self.paths.screens / f"probe-{uuid.uuid4().hex[:8]}.ppm"
        try:
            await self.qmp.screendump(str(tmp), "ppm")
            data = await asyncio.to_thread(tmp.read_bytes)
            return read_ppm(data)
        except (QMPError, OSError, ValueError):
            return None
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    async def _probe(self, session: str) -> None:
        prev_samples: bytes | None = None
        while session == self.session and self.active:
            if self.paused:
                await asyncio.sleep(0.5)
                continue
            need_content = self.cfg.content_probe and not self.content_seen
            osx = bool(self.machine and self.machine.osx)
            seen = self.osx_finder_visible if osx else self.finder_seen
            need_finder = self.cfg.finder_probe and not seen and mono() < self.probe_until
            if not need_content and not need_finder:
                if self.cfg.finder_probe and not seen and self.content_seen and \
                        not self.finder_timeout_logged:
                    # Diagnostics only: Mac OS may show a dialog or a system error; the user decides.
                    self.finder_timeout_logged = True
                    eid = await self.app.diagnostics.capture_evidence(
                        "finder_not_detected", f"no Finder within {self.cfg.finder_wait_timeout:.0f} s of start")
                    self.log.event("finder_not_detected", session=session, evidence=eid,
                                   t_since_start=round(mono() - self.t_start, 3))
                return
            if not need_content and not self.content_seen:
                await asyncio.sleep(0.5)
                continue
            frame = await self.dump_frame()
            if frame:
                w, h, rgb = frame
                if need_content:
                    gray, gfrac, nb = await asyncio.to_thread(genuine_content, w, h, rgb)
                    samples = frame_samples(w, h, rgb)
                    changed = samples_changed(prev_samples, samples)
                    prev_samples = samples
                    # 4.3 on 1 s probes: the frame must have stopped changing
                    # (Mac OS paints the startup gray top-down).
                    if (gray and changed == 0.0) or (nb >= NONBLACK_FRACTION and changed <= SETTLED_CHANGE):
                        await self.on_guest_content(int((mono() - self.t_start) * 1000), "qmp-probe",
                                                    gray_fraction=round(gfrac, 3), nonblack_fraction=round(nb, 3))
                if self.cfg.finder_probe and self.content_seen:
                    if osx and not self.osx_finder_visible:
                        from .finder_osx import finder_visible
                        rule = await asyncio.to_thread(finder_visible, self.machine.guest_os,
                                                       self.machine.meta.get("os_version"), w, h, rgb,
                                                       build=self.machine.meta.get("os_build"))
                        if rule:
                            await self.on_osx_finder_visible(w, h, rule)
                    elif not osx and not self.finder_seen:
                        rule = await asyncio.to_thread(finder.finder_on_screen, w, h, rgb)
                        if rule:
                            self.finder_frame = frame       # enabled menus for powerdlg.menu_state
                            await self.on_finder(w, h, rule)
            await asyncio.sleep(1.0 if need_content else 2.0)

    async def on_guest_content(self, t_ms, source: str, **extra) -> None:
        if self.content_seen or not self.session:
            return
        self.content_seen = True
        self.content_mono = mono()
        if self.state == STARTING:
            self.state = RUNNING
        if self._content_timer:
            self._content_timer.cancel()
            self._content_timer = None
        since = round(mono() - self.t_start, 3)
        self.log.event("guest_content", session=self.session, via=source, presenter_t_ms=t_ms,
                       t_since_start=since, **extra)
        if self.machine and not self.paused and not self.paused_before_content:
            try:
                hist = [v for v in (self.machine.state().get("content_s") or []) if isinstance(v, (int, float))]
                self.machine.set_state(content_s=(hist + [since])[-CONTENT_HISTORY:])
            except OSError as exc:
                log.warning("content history not saved: %s", exc)
        self.content_event.set()
        self._watch_started()
        session = self.session
        if self._kg_timer:
            self._kg_timer.cancel()
        self._kg_timer = asyncio.get_running_loop().call_later(
            self.cfg.known_good_after, lambda: self._known_good(session))

    def _known_good(self, session: str) -> None:
        if session == self.session and self.state in (RUNNING, STOPPING) and not self.paused:
            self.attempts = 0
            if self.app.settings.mark_known_good():
                self.log.event("known_good_advanced", session=session, revision=self.app.settings.revision)

    async def on_osx_finder_visible(self, w: int, h: int, reference: str) -> None:
        if self.osx_finder_visible:
            return
        self.osx_finder_visible = True
        self.log.event("osx_finder_visible", session=self.session, reference=reference,
                       t_since_start=round(mono() - self.t_start, 3), frame=f"{w}x{h}")

    async def on_finder(self, w: int, h: int, via: str = "platinum") -> None:
        if self.finder_seen:
            return
        self.finder_seen = True
        self.finder_mono = mono()
        self.log.event("finder_detected", session=self.session, t_since_start=round(mono() - self.t_start, 3),
                       frame=f"{w}x{h}", via=via)
        self.finder_event.set()
        self.up_event.set()
        if self.boot_speed == "accelerated" and self.handoff is None:
            await self._handoff_status("finder")

    @property
    def mac_os_up(self) -> bool:
        """Mac OS is past its startup: the Finder was recognized on screen (finder.py: any
        Appearance theme by its menu-bar layout), or, the last resort, Mac OS counted as
        started without it (_started_watch).  The power-key flow, the laptop keys, the
        Bluetooth keyboard assistant and the AirPort password window wait for it."""
        return self.finder_seen or self.started_seen

    def _watch_started(self) -> None:
        if self.machine is not None and self.machine.osx:
            return     # a still login/installer/boot screen is not a responsive Finder
        if self.cfg.finder_probe and not self.mac_os_up and self.active and \
                not any(t.get_name() == "started-watch" for t in self._tasks):
            self._spawn(self._started_watch(self.session), "started-watch")

    async def _started_watch(self, session: str) -> None:
        """The last resort when the Finder is not recognized (a theme finder.py does not
        know, a screen it cannot read): Mac OS counts as started once it has shown content
        for power_started_after_s and then stayed idle for power_started_idle_s.  It is busy
        all through the extension parade and the Finder's first seconds, and idles at its
        desktop.  Logged as mac_os_started; the probe keeps looking for the Finder."""
        idle_since = None
        prev = None
        while session == self.session and self.active and not self.mac_os_up:
            now = mono()
            t_content = self.content_mono
            idle = False
            if self.paused or self.state not in (STARTING, RUNNING):
                idle_since = None
            elif t_content and now - t_content >= self.cfg.power_started_after_s:
                idle = self._idle_now()
                if idle is None:                   # not measurable: a screen that stays still
                    cur = await self.dump_frame()
                    idle = cur is not None and prev is not None and powerdlg.screen_changed(prev, cur) == 0.0
                    prev = cur
                idle_since = (idle_since or now) if idle else None
                if idle_since and now - idle_since >= self.cfg.power_started_idle_s and not self.mac_os_up:
                    self.started_seen = True
                    self.log.event("mac_os_started", session=session, why="idle without a recognized Finder menu bar",
                                   since_content_s=round(now - t_content, 1),
                                   t_since_start=round(now - self.t_start, 3))
                    self.up_event.set()
                    if self.boot_speed == "accelerated" and self.handoff is None:
                        await self._handoff_status("started")
                    return
            await asyncio.sleep(0.5)

    # -- exit -------------------------------------------------------------------------
    def _restart_by_shutdown(self, session: str | None) -> bool:
        """Mac OS 7.5.5 and 8.1 take no key for their dialog's Restart (R closes it; measured
        with the q800), so hostd restarts a 68k Mac by its Shut Down and a new start: this
        session's guest-shutdown follows hostd's own Return to the dialog (not the user's own
        Shut Down), within restart_by_shutdown_ttl_s (Mac OS may wait for an application)."""
        rb = self.restart_by_shutdown
        return bool(rb) and session is not None and rb["session"] == session and \
            mono() - rb["mono"] <= float(getattr(self.cfg, "restart_by_shutdown_ttl_s", 600.0))

    def _late_stop(self, session: str | None) -> dict | None:
        """The request this session's Shut Down finishes, when hostd asked for it and gave up
        waiting (an application asked to save) at most late_stop_ttl_s ago."""
        ls = self.late_stop
        if ls and session is not None and ls["session"] == session and \
                mono() - ls["mono"] <= float(getattr(self.cfg, "late_stop_ttl_s", 600.0)):
            return ls
        return None

    async def _finish_late_stop(self, session: str | None, late: dict) -> None:
        """Mac OS finished the Shut Down of a request that had given up: do what was asked
        (VM169, October 24 2026: Restart to Update stopped at Setup Assistant's "Are you sure
        you want to quit?", and its Quit then powered the computer off)."""
        then = late["then"]
        self.log.event("late_stop_finished", session=session, purpose=late["purpose"], then=then,
                       after_s=round(mono() - late["mono"], 1))
        if then == "hold":
            await self.app.panel.show_stopped("Mac OS is shut down.")
            return
        if then == "resume":
            try:
                if late["resume"]() is not None:
                    return                          # the request again; Mac OS is off, so it goes on
            except Exception:  # noqa: BLE001
                log.exception("resuming %s after a late shut down", late["purpose"])
            then = "reboot"
        if then == "reboot":
            await self.reboot_host(f"late {late['purpose']}")
        else:
            await self.poweroff("guest-shutdown")

    async def _on_exit(self, exit_info: dict) -> None:
        reason = exit_info.get("reason", "crash")
        code = exit_info.get("code")
        session = self.session
        self._cancel_session()
        if self.qmp:
            await self.qmp.close()
            self.qmp = None
        clean = reason in ("guest-shutdown", "guest-reset")
        runtime = round(mono() - self.t_start, 3) if self.t_start else None
        now_wall = wall()
        self.last_exit = {"reason": reason, "code": code, "wall": now_wall, "at": now_wall, "session": session,
                          "runtime_s": runtime, "forced": bool(self.intent and self.intent.startswith("force"))}
        if self.machine:
            fields = {"last_exit": self.last_exit, "running_session": None}
            if clean:
                fields.update(clean=True, dirty_since=None)
            elif (not self.content_seen and self.launch_disk is not None and
                  disk_stamp(self.machine.disk) == self.launch_disk):
                # Stopped before Mac OS showed anything and never wrote its disk (a hang
                # before the gray): the disk is as it was, so is its clean state.
                was_clean, dirty_since = self.launch_clean
                fields.update(clean=was_clean, dirty_since=None if was_clean else dirty_since)
                self.log.event("disk_untouched", session=session, reason=reason, clean=was_clean)
            self.machine.set_state(**fields)
        intent = self.intent
        self.intent = None
        restart = reason == "guest-shutdown" and self._restart_by_shutdown(session)
        self.restart_by_shutdown = None
        if restart:
            self.last_exit["restart_by_shutdown"] = True
        late = self._late_stop(session) if reason == "guest-shutdown" and not restart and not intent else None
        self.late_stop = None
        if late:
            self.last_exit["late_stop"] = late["purpose"]
        # Waiters must never observe a gap between this exit and a planned relaunch.
        self.relaunching = (intent in ("retry", "force-restart") or restart or
                            (intent not in ("failed", "force-stop-poweroff", "force-stop-hold") and
                             (reason == "guest-reset" or (reason not in ("guest-shutdown",) and code != 20
                                                          and self.attempts < 2))))
        self.state = STOPPED
        self.paused = False
        self.handle = None
        self.qmp_ok = self.display_ok = None
        self.unresponsive_since = None
        self.unresponsive_evidence = None
        self.freeze_check = None
        self.log.event("guest_exited", session=session, reason=reason, code=code, intent=intent, runtime_s=runtime)
        await self._release_cd_drives()
        try:
            self.app.cpuplace.guest_stopped(session)
        except Exception:  # noqa: BLE001
            log.exception("board hooks at exit")
        # The keys are the host's again; a lamp Mac OS turned off comes back -- unless this
        # exit ends in the host powering off or restarting, where Mac OS's own Shut Down had
        # darkened the panel and lighting it for a black frame was a visible flash (hostboot2).
        host_off = (intent in ("force-stop-poweroff", "shutdown-host") or
                    (reason == "guest-shutdown" and intent not in ("shutdown-hold", "force-stop-hold")
                     and not restart and not self.app.stopping and (late or {}).get("then") != "hold"))
        await self.app.keys.guest_stopped(host_power_off=host_off)
        self.exit_event.set()
        if self.app.stopping:
            return
        if intent == "retry":
            self.log.event("guest_retry", previous=session, evidence=(self.failure or {}).get("evidence"))
            await self.start_guest("retry")
        elif intent == "failed":
            await self._show_failure()
        elif intent == "force-restart":
            await self.start_guest("force-restart")
        elif intent == "force-stop-poweroff":
            await self.poweroff("force-stop")
        elif intent == "force-stop-hold":
            await self.app.panel.show_stopped("Mac OS was stopped.")
        elif reason == "guest-reset" or restart:
            await self.start_guest("guest-reset")
        elif reason == "guest-shutdown":
            if intent == "shutdown-hold":
                await self.app.panel.show_stopped("Mac OS is shut down.")
            elif intent == "shutdown-host":
                await self.app.show_scene("black")      # the host job powers off or restarts next
            elif late:
                await self._finish_late_stop(session, late)
            else:
                await self.poweroff("guest-shutdown")
        elif code == 20:
            await self.app.panel.show_stopped("Mac OS was stopped.")
        else:
            if self.attempts < 2:
                eid = await self.app.diagnostics.capture_evidence("guest_crash", f"emulator exited ({reason})",
                                                                  {"exit": exit_info})
                self.failure = {"evidence": eid, "message": "Mac OS stopped unexpectedly."}
                self.log.event("guest_retry", previous=session, evidence=eid)
                await self.start_guest("retry")
            else:
                eid = await self.app.diagnostics.capture_evidence("guest_crash", f"emulator exited ({reason})",
                                                                  {"exit": exit_info})
                self.failure = {"evidence": eid, "message": "Mac OS stopped unexpectedly."}
                await self._show_failure()

    async def _show_failure(self) -> None:
        f = self.failure or {}
        self.log.event("failure_alert", evidence=f.get("evidence"), message=f.get("message"))
        await self.app.panel.show_failure("Mac OS could not start.",
                                          f.get("message") or "The emulator stopped unexpectedly.", f.get("evidence"))

    async def poweroff(self, why: str) -> None:
        self.log.event("poweroff", why=why, dev_hook=not bool(self.cfg.poweroff_cmd))
        await self.app.show_scene("black")
        await asyncio.sleep(0.3)
        if self.cfg.sync_cmd:
            await run_cmd(self.cfg.sync_cmd, timeout=60)
        self.app.powered_off = True
        if self.cfg.poweroff_cmd:
            await run_cmd(self.cfg.poweroff_cmd, timeout=60)

    async def reboot_host(self, why: str, arg: str | None = None) -> bool:
        """Restart the computer. ``arg`` goes to reboot(2) (``systemctl reboot
        --reboot-argument=ARG``; systemd 257 refuses it as a positional argument): a Raspberry
        Pi's ``0 tryboot`` starts the installer kept on its card once (rpiboot.py). False when
        the restart command failed (logged as ``host_reboot_failed``)."""
        if arg:
            self.log.event("host_reboot", why=why, dev_hook=not bool(self.cfg.reboot_cmd), arg=arg)
        else:
            self.log.event("host_reboot", why=why, dev_hook=not bool(self.cfg.reboot_cmd))
        await self.app.show_scene("black")
        if self.cfg.sync_cmd:
            await run_cmd(self.cfg.sync_cmd, timeout=60)
        if self.cfg.reboot_cmd:
            rc, _, err = await run_cmd(self.cfg.reboot_cmd + ([f"--reboot-argument={arg}"] if arg else []), timeout=60)
            if rc != 0:
                self.log.event("host_reboot_failed", why=why, rc=rc, error=err.strip()[-200:])
                return False
        return True

    # -- pause / resume (boot options) ------------------------------------------------------
    async def pause_for_options(self) -> bool:
        if self.state != STARTING or self.paused:
            return self.paused
        for _ in range(50):
            if self.qmp:
                break
            await asyncio.sleep(0.1)
        if not self.qmp:
            return False
        try:
            await self.qmp.stop()
        except QMPError:
            return False
        self.paused = True
        if not self.content_seen:
            self.paused_before_content = True
        self.state = PAUSED
        self.app.startup.on_paused()
        if self._content_timer:
            self._content_timer.cancel()
            self._content_timer = None
        self.log.event("guest_paused", session=self.session, why="boot-options",
                       t_since_start=round(mono() - self.t_start, 3))
        return True

    async def resume(self, reveal: str = "first-content") -> None:
        if not self.paused or not self.qmp:
            return
        try:
            await self.qmp.cont()
        except QMPError:
            pass
        self.paused = False
        self.state = RUNNING if self.content_seen else STARTING
        self.log.event("guest_resumed", session=self.session)
        await self.app.show_scene("guest", reveal=reveal)
        await self.app.startup.on_resumed()
        if not self.content_seen:
            self._arm_content_timer()

    # -- events ------------------------------------------------------------------------------
    async def on_presenter_event(self, msg: dict) -> None:
        ev = msg.get("event")
        if ev == "guest_content":
            if self.state in (STARTING, RUNNING) and self.session:
                await self.on_guest_content(msg.get("t_ms"), "presenter")
        elif ev == "guest_revealed":
            self.revealed = True
            self.log.event("guest_revealed", session=self.session, presenter_t_ms=msg.get("t_ms"),
                           t_since_start=round(mono() - self.t_start, 3) if self.t_start else None)
            self.app.startup.on_revealed(msg)
        elif ev == "startup_icon":
            await self.app.startup.on_icon(msg)
        elif ev in ("guest_connected", "guest_disconnected"):
            self.log.event("presenter_" + ev, session=msg.get("session"), pid=msg.get("pid"))
        elif ev == "guest_surface":
            self.log.event("guest_surface", size=msg.get("size"))
            self.guest_surface = msg.get("size")      # Mac OS's own screen size (status display)
            self.manual_resize_notice = None
        elif ev == "relayout":
            detail = str(msg.get("detail") or msg.get("message") or "")
            if msg.get("timed_out") or "no new guest surface" in detail:
                self._spawn(self._handle_relayout_timeout(), "relayout-nudge")
        elif ev == "preboot_key":
            self.log.event("preboot_key", key=msg.get("key"), state=self.state)
            await self.on_preboot_escape()
        elif ev == "chord":
            self.log.event("recovery_chord", name=msg.get("name"), state=self.state)
            await self.app.panel.show_recovery()
        elif ev == "power_button":
            await self.on_power_button()
        elif ev == "lid":
            self.log.event("lid", open=msg.get("open"))

    async def on_preboot_escape(self) -> None:
        if self.app.mode == "installer":
            return
        if self.state == STARTING and not self.content_seen:
            await self.pause_for_options()
            await self.app.panel.show_boot_options()
        elif self.state == STOPPED and (not self.booted or self.boot_pending):
            # Before QEMU runs (hostd still deciding, preparing the disk): hold the
            # automatic start; boot()/the launch shows the startup options.
            self.boot_hold = True
            self.log.event("boot_hold", state=self.state)
        elif self.state == STOPPED:
            await self.app.panel.show_startup_options()

    async def on_power_button(self) -> None:
        self.log.event("power_button", state=self.state)
        if self.app.battery.asleep and self.state == RUNNING and not self.app.panel.visible:
            # A sleeping PowerBook wakes on its power button (battery.py).
            await self.app.battery.wake("power-button")
            return
        if self.state in (STARTING, RUNNING) and not self.paused and self.qmp and not self.app.panel.visible:
            pk = self._power_key_pending()
            if pk and (pk.get("confirmed") or mono() - pk["mono"] < self.cfg.power_button_repeat_s):
                # Mac OS's Shut Down dialog is up (a second power key would close it again)
                # or its shutdown is under way.
                self.log.event("power_button_ignored", session=self.session, why="power key already sent",
                               origin=pk["origin"], age_s=round(mono() - pk["mono"], 1))
                return
            if self.power_lock.locked():
                # A request (or an earlier press still waiting for Mac OS to start) is
                # already bringing up the dialog.
                self.log.event("power_button_ignored", session=self.session, why="power dialog under way")
                return
            self._spawn(self._button_dialog(self.session), "power-button")
            return
        await self.app.panel.show_power_alert()

    # -- lifecycle requests (job bodies) ---------------------------------------------------
    async def wait_started(self, job: Job | None, timeout: float) -> dict:
        deadline = mono() + timeout
        while mono() < deadline:
            if self.content_seen and self.state in (RUNNING, STOPPING):
                return {"session": self.session, "content_after_s": None if self.t_start is None
                        else round(mono() - self.t_start, 1)}
            if self.state == STOPPED and not self.relaunching and self.failure:
                raise JobFailed(f"Mac OS could not start. Evidence {self.failure.get('evidence')}.",
                                {"evidence": self.failure.get("evidence")}, "start_failed")
            if self.state == STOPPED and not self.relaunching and not self.launch_lock.locked():
                raise JobFailed("Mac OS is not running.", None, "stopped")
            if job:
                job.update(None, "Starting Mac OS…" if self.state != PAUSED else "Waiting at startup options…")
            await asyncio.sleep(0.25)
        raise JobFailed("Mac OS did not finish starting in time.", None, "timeout")

    async def job_start(self, job: Job) -> dict:
        if self.state == PAUSED:
            await self.app.panel.close()
            await self.resume()
        elif not self.active:
            self.app.panel.forget()
            if not await self.start_guest("user"):
                if self.needs_cd:
                    machine = self.app.machines.get(self.needs_cd["machine"])
                    raise JobFailed(needs_cd_text(self.needs_cd["name"], self.needs_cd["blank"],
                                                  bool(machine and machine.osx)), None, "needs_cd")
                if self.unavailable:
                    raise JobFailed(self.unavailable, None, "machine_unavailable")
                raise JobFailed("Mac OS could not start.", {"evidence": (self.failure or {}).get("evidence")},
                                "start_failed")
        job.update(0.2, "Starting Mac OS…")
        return await self.wait_started(job, self.cfg.guest_content_timeout * 2 + 30)

    async def wait_ready(self, job: Job | None) -> bool:
        """Wait until Mac OS is past its startup (mac_os_up) so it handles the power key."""
        if self.machine is not None and self.machine.osx:
            # This allows an attempt to open the native Apple menu. Positive UI
            # recognition separately gates each Return; pixels never mean ready.
            if self.paused:
                await self.resume(reveal="now")
            if not self.content_seen:
                try:
                    await asyncio.wait_for(self.content_event.wait(), self.cfg.finder_wait_timeout)
                except asyncio.TimeoutError:
                    return False
            return self.qmp is not None and self.active
        if self.mac_os_up:
            return True
        if self.paused:
            await self.resume(reveal="now")
        self.probe_until = mono() + self.cfg.finder_wait_timeout
        if self.cfg.finder_probe and self.qmp and not any(t.get_name() == "guest-probe" for t in self._tasks):
            self._spawn(self._probe(self.session), "guest-probe")
        if job:
            job.update(0.05, "Waiting for Mac OS to finish starting…")
        if not self.cfg.finder_probe:
            await asyncio.wait_for(self.content_event.wait(), self.cfg.finder_wait_timeout)
            await asyncio.sleep(20)
            return True
        # The Finder in any Appearance theme (finder.py), or the last resort: Mac OS counts as
        # started once it idled after its content (_started_watch, mac_os_started).
        deadline = mono() + self.cfg.finder_wait_timeout
        while mono() < deadline:
            if self.mac_os_up:
                return True
            if self.state not in (STARTING, RUNNING, STOPPING) or self.paused:
                return False
            if self.content_seen:
                self._watch_started()
            try:
                await asyncio.wait_for(self.up_event.wait(), 0.5)
            except asyncio.TimeoutError:
                pass
        return self.mac_os_up

    # -- Mac OS's power-key dialog (powerdlg.py) ------------------------------------------
    @property
    def power_pending(self) -> bool:
        """A Shut Down/Restart request or a power-button press is getting Mac OS's dialog
        on screen (other hooks keep their keys away from Mac OS meanwhile)."""
        return self.power_lock.locked()

    def _guest_load(self) -> powerdlg.GuestLoad:
        """This session's QEMU (its pid from the launcher's status.json, read until known)."""
        if self.load is None or self.load_session != self.session or not self.load.pid:
            pid = None
            if self.handle is not None:
                pid = (self.handle.status() or {}).get("pid") or self.handle.pid
            if self.load is None or self.load_session != self.session or pid != self.load.pid:
                self.load = powerdlg.GuestLoad(pid)
                self.load_session = self.session
        return self.load

    async def _load_sampler(self, session: str) -> None:
        """How busy Mac OS keeps the emulated CPU, sampled twice a second for the whole
        session (a few /proc reads), so a power request knows at once whether it is idle."""
        while session == self.session and self.active:
            self._guest_load().sample(mono())
            await asyncio.sleep(0.5)

    def _idle_now(self) -> bool | None:
        """Mac OS has been idle for power_ready_quiet_s (None: the CPU is not measurable).
        A 68k Mac OS halts the CPU only with RetroBoot Idle installed (D044), so on a 68k Mac a
        busy CPU means nothing: None, and the callers judge by the screen as without /proc;
        an idle one (RetroBoot Idle working) still counts."""
        peak = self._guest_load().peak(mono(), self.cfg.power_ready_quiet_s)
        if peak is None:
            return None
        idle = peak < self.cfg.power_idle_busy
        if not idle and self.family == QUADRA800:
            return None
        return idle

    async def _wait_idle(self, job: Job | None, deadline: float) -> dict:
        """Until Mac OS is idle or ``deadline`` (before a second key, and before the first
        with power_idle_first): then it shows the dialog within a second of the key.  After
        the Finder appears it stays busy for about 4.5 s in the lab and 13.5 s on the GPD.
        Without a CPU measurement, a screen that stayed still below the menu bar for
        power_ready_quiet_s counts as idle."""
        t0 = mono()
        prev = None
        still_since = None
        told = False
        session = self.session
        while True:
            if not self._session_alive(session):
                return {"idle": False, "waited_s": round(mono() - t0, 1), "gone": True}
            idle = self._idle_now()
            if idle is None:
                cur = await self.dump_frame()
                now = mono()
                if cur is not None and prev is not None and powerdlg.screen_changed(prev[1], cur) == 0.0:
                    still_since = still_since or prev[0]
                else:
                    still_since = None
                prev = (now, cur) if cur is not None else None
                idle = still_since is not None and now - still_since >= self.cfg.power_ready_quiet_s
            if idle or mono() >= deadline:
                return {"idle": bool(idle), "waited_s": round(mono() - t0, 1)}
            if job and not told and mono() - t0 >= 1.0:
                told = True
                job.update(None, "Waiting for Mac OS to be ready…")
            await asyncio.sleep(0.5)

    def _power_key_pending(self) -> dict | None:
        """The ADB power key hostd sent moments ago in this session, if any."""
        pk = self.power_key
        if pk and pk["session"] == self.session and mono() - pk["mono"] < self.cfg.power_key_pending_s:
            return pk
        return None

    def _session_alive(self, session: str | None) -> bool:
        """The session a power request is for still runs (Mac OS may have shut down by itself)."""
        return session is not None and session == self.session and self.active and \
            not self.exit_event.is_set() and self.qmp is not None

    async def _send_power_key(self, origin: str, n: int = 1) -> dict:
        before = await self.dump_frame()
        if self.qmp is None:
            raise QMPError("QMP not connected")
        await self.qmp.send_key("power", hold_ms=150)
        self.power_key = {"session": self.session, "mono": mono(), "before": before, "origin": origin, "n": n}
        busy = self._guest_load().busy(mono())
        self.log.event("power_key_sent", session=self.session, origin=origin, attempt=n,
                       busy=None if busy is None else round(busy, 2),
                       since_finder_s=round(mono() - self.finder_mono, 1) if self.finder_mono else None)
        return self.power_key

    async def _dialog_now(self, pk: dict) -> dict | None:
        """The power dialog of key ``pk`` as on screen now (None: not there, or no screen):
        recognized by its content, or (another Appearance theme) the same modal box."""
        cur = await self.dump_frame()
        if cur is None or pk.get("before") is None:
            return None
        res = await asyncio.to_thread(powerdlg.find_power_dialog, pk["before"], cur)
        if res["found"]:
            return res
        mc = await asyncio.to_thread(powerdlg.modal_change, pk["before"], cur)
        if mc["modal"] and pk.get("box") and _near(mc["box"], pk["box"]):
            return {"found": True, "box": mc["box"], "via": "modal"}
        return None

    async def _watch_dialog(self, pk: dict, until: float) -> str:
        """After power key ``pk``: "dialog" once Mac OS's dialog is on screen (the same box
        in two consecutive screendumps: it finished drawing); "dropped" once Mac OS has been
        idle for power_retry_idle_s without anything dialog-like appearing (it ignored the
        key, so another key is safe); "timeout" at ``until`` (busy, or something appeared
        that is not recognizably the dialog: never press again then, it would close it)."""
        before = pk["before"]
        if before is None:
            await asyncio.sleep(1.0)          # cannot verify without a screendump
            return "dialog"
        last_box = None
        idle_since = None
        prev = before
        unknown = False
        modal_box = modal_since = None
        session = pk.get("session")
        while mono() < until:
            await asyncio.sleep(0.3)
            if not self._session_alive(session):
                return "gone"
            cur = await self.dump_frame()
            now = mono()
            if cur is None:
                continue
            res = await asyncio.to_thread(powerdlg.find_power_dialog, before, cur)
            if res["found"]:
                if res["box"] == last_box:
                    pk["box"] = res["box"]
                    self.log.event("power_dialog_detected", session=self.session, attempt=pk.get("n"),
                                   after_s=round(now - pk["mono"], 1), box=list(res["box"]),
                                   buttons=res["buttons"], via=res.get("via"), menus_dimmed=res["menus_dimmed"])
                    return "dialog"
                last_box = res["box"]
                idle_since = None
                continue
            last_box = None
            unknown = unknown or res.get("new_box", False)
            still = powerdlg.screen_changed(prev, cur) == 0.0
            idle = self._idle_now()
            if idle is None:
                idle = still
            prev = cur
            idle_since = (idle_since or now) if idle else None
            # Any Appearance theme: a box that appeared with the menus changing state (a modal
            # dialog), unchanged for power_fallback_s while Mac OS idles, is the dialog.
            mc = await asyncio.to_thread(powerdlg.modal_change, before, cur)
            if mc["modal"]:
                unknown = True
                if modal_box is not None and _near(mc["box"], modal_box) and still:
                    modal_since = modal_since or now
                    if idle and now - modal_since >= self.cfg.power_fallback_s:
                        pk["box"] = mc["box"]
                        self.log.event("power_dialog_detected", session=self.session, attempt=pk.get("n"),
                                       after_s=round(now - pk["mono"], 1), box=list(mc["box"]), buttons=None,
                                       via="modal", menus=mc["menus"])
                        return "dialog"
                else:
                    modal_since = None
                modal_box = mc["box"]
                continue
            modal_box = modal_since = None
            if (not unknown and idle_since and now - idle_since >= self.cfg.power_retry_idle_s
                    and now - pk["mono"] >= self.cfg.power_retry_min_s):
                ms = await asyncio.to_thread(powerdlg.menu_state, self.finder_frame, cur)
                if (ms and ms["changed"]) or await asyncio.to_thread(powerdlg.alert_on_screen, cur):
                    # An alert was already up (an application's message): the key waits behind
                    # it, and another key would close the dialog it opens later.
                    self.power_failure = "alert"
                    self.log.event("power_dialog_blocked", session=self.session, attempt=pk.get("n"),
                                   why="another alert is on screen (or the menus are disabled)")
                    return "timeout"
                self.log.event("power_key_ignored", session=self.session, attempt=pk.get("n"),
                               after_s=round(now - pk["mono"], 1), changed=round(powerdlg.screen_changed(before, cur), 3))
                return "dropped"
        self.log.event("power_dialog_not_seen", session=self.session, attempt=pk.get("n"),
                       after_s=round(mono() - pk["mono"], 1), something_else_appeared=unknown)
        return "timeout"

    async def _wake_screen(self, origin: str) -> None:
        """Before hostd's first power key: end a screen saver.  After Dark's Flying Toasters
        (Deluxe and SuperDeluxe, after 30 idle minutes) keep Mac OS busy, cover its Shut Down
        dialog and are not ended by the ADB power key, so Restart to Update and Shut Down
        failed after a minute ("Mac OS stayed busy").  When the menu bar is not on screen
        (a saver, a full-screen game, a dark Appearance theme), the pointer moves 8 pixels
        for half a second and back, which ends the saver, and hostd waits until the screen
        holds still (Mac OS redraws what the saver covered), at most power_wake_settle_s, so
        the key's before-frame shows the desktop.  With the menu bar showing nothing
        changes: the first key goes at once (a busy Mac OS holds it)."""
        cfg = self.cfg
        if self.qmp is None or not cfg.power_wake_nudge:
            return
        session = self.session
        t0 = mono()
        prev = await self.dump_frame()
        if powerdlg.menu_bar_light(prev) is not False:
            return
        try:
            await self.qmp.pointer_nudge()
        except QMPError as exc:
            self.log.event("screen_wake_failed", session=session, origin=origin, error=str(exc)[:200])
            return
        changed = False
        still_since = None
        while mono() - t0 < cfg.power_wake_settle_s and self._session_alive(session):
            await asyncio.sleep(0.25)
            cur = await self.dump_frame()
            if cur is None or prev is None:
                break
            if powerdlg.screen_changed(prev, cur) == 0.0:
                still_since = still_since or mono()
                if mono() - still_since >= 0.5:
                    break
            else:
                changed = True
                still_since = None
            prev = cur
        self.log.event("screen_woken", session=session, origin=origin, changed=changed,
                       menu_bar=powerdlg.menu_bar_light(prev), waited_s=round(mono() - t0, 1))

    async def _open_power_dialog(self, origin: str, job: Job | None = None) -> dict | None:
        """Get Mac OS's power dialog on screen with as few power keys as possible; the key
        record (with the dialog's ``box``) or None.  Caller holds power_lock.

        A dialog that a recent key (the power button) opened is reused when it is still on
        screen.  Otherwise: press once, watch for the dialog for as long as Mac OS is busy
        (it holds the key until it can show the dialog), and press again only when an idle
        Mac OS dropped the key (nothing appeared).  A second key while the dialog is up or
        still coming closes it (measured on Mac OS 9.2.1)."""
        cfg = self.cfg
        self.power_failure = None
        await self.app.battery.ensure_awake("power-dialog")   # a sleeping Mac OS ignores the dialog
        await self._wake_screen(origin)                        # a screen saver hides the dialog
        end = mono() + cfg.power_request_budget_s
        pk = self._power_key_pending()
        if pk and not pk.get("confirmed"):
            age = mono() - pk["mono"]
            self.log.event("power_key_reused", session=self.session, origin=pk["origin"], age_s=round(age, 1))
            if pk.get("box") and await self._dialog_now(pk):
                return pk
            if not pk.get("box"):
                # its dialog may still be coming: watch out the rest of that key's window
                if await self._watch_dialog(pk, mono() + max(2.0, cfg.power_dialog_wait_s - age)) == "dialog":
                    return pk
        n = 0
        session = self.session
        while n < cfg.power_key_attempts and mono() < end:
            if n or cfg.power_idle_first:
                ready = await self._wait_idle(job, min(end, mono() + cfg.power_ready_max_s))
            else:
                # The first key at once: a busy Mac OS (its startup work after the Finder, an
                # application launching) holds it and draws the dialog when it can.
                ready = {"idle": self._idle_now(), "waited_s": 0.0}
            if not self._session_alive(session):
                return None                       # Mac OS shut down or restarted meanwhile
            n += 1
            if job:
                job.update(None, {"shutdown": "Asking Mac OS to shut down…",
                                  "restart": "Asking Mac OS to restart…"}.get(origin, "Asking Mac OS…"))
            self.log.event("power_ready", session=self.session, origin=origin, attempt=n, **ready)
            pk = await self._send_power_key(origin, n)
            out = await self._watch_dialog(pk, end)
            if out == "dialog":
                return pk
            if out != "dropped":
                break
        if self.power_failure is None:
            self.power_failure = "busy" if self._idle_now() is False else "no-dialog"
        return None

    async def _power_dialog(self, confirm_key: str, origin: str = "request", job: Job | None = None,
                            restart_by_shutdown: bool = False) -> bool:
        """Open Mac OS's Shut Down dialog (``_open_power_dialog``), then answer it: Return
        (Shut Down) or R (Restart).  The answer goes only to a dialog verified on screen.
        ``restart_by_shutdown`` (a 68k Mac's Restart): this Return's Shut Down is a restart."""
        pk = await self._open_power_dialog(origin, job)
        if pk is None:
            return False
        await asyncio.sleep(0.3)
        if restart_by_shutdown:
            self.restart_by_shutdown = {"session": self.session, "mono": mono()}
        try:
            await self.qmp.send_key(confirm_key, hold_ms=100)
        except BaseException:
            if restart_by_shutdown:
                self.restart_by_shutdown = None
            raise
        pk.update(mono=mono(), confirmed=True)  # Mac OS is now shutting down: no second key
        self.log.event("power_dialog_confirmed", session=self.session, key=confirm_key, attempt=pk.get("n"))
        return True

    async def _button_dialog(self, session: str) -> None:
        """The host power button: Mac OS's own dialog, as on a Mac.  While Mac OS is still
        starting the press waits (queued) and the dialog comes once Mac OS can take the key;
        the user answers it."""
        async with self.power_lock:
            if session != self.session or not self.active or self.intent:
                return
            if not self.mac_os_up:
                self.log.event("power_button_queued", session=session, state=self.state)
                if not await self.wait_ready(None):
                    self.log.event("power_button_dropped", session=session, why="Mac OS did not finish starting")
                    return
            if session != self.session or not self.active or self.intent or self.app.panel.visible:
                return
            self.restart_by_shutdown = None       # the user answers this dialog: their choice counts
            self.late_stop = None
            if self.machine is not None and self.machine.osx:
                if not await self._osx_shutdown_ui("button", confirm=False):
                    self.log.event("power_button_no_dialog", session=session, why="unrecognized OS X UI")
                return
            pk = await self._open_power_dialog("button")
            if pk is None:
                self.log.event("power_button_no_dialog", session=session)

    async def _nudge_host_resize(self, width: int, height: int) -> bool:
        """Re-arm QEMU's VGA/nubus-qfb host-resize-request latch over QMP after Monitors edits (#19)."""
        qmp = self.qmp
        if qmp is None or self.machine is None or getattr(self.machine, "osx", False):
            return False
        if width < 512 or height < 384:
            return False
        for parent in ("/machine/peripheral-anon", "/machine/peripheral", "/machine/unattached"):
            try:
                nodes = await qmp.execute("qom-list", {"path": parent})
            except Exception:
                continue
            for item in (nodes or []):
                name = item.get("name") if isinstance(item, dict) else None
                if not name:
                    continue
                path = f"{parent}/{name}"
                try:
                    props = await qmp.execute("qom-list", {"path": path})
                except Exception:
                    continue
                if any(isinstance(p, dict) and p.get("name") == "host-resize-request" for p in (props or [])):
                    try:
                        await qmp.execute("qom-set", {"path": path, "property": "host-resize-request",
                                                      "value": f"{width}x{height + 1}"})
                        await qmp.execute("qom-set", {"path": path, "property": "host-resize-request",
                                                      "value": f"{width}x{height}"})
                        self.log.event("host_resize_nudged", path=path, size=[width, height])
                        return True
                    except Exception:
                        return False
        return False

    async def _handle_relayout_timeout(self) -> None:
        if not self.active or self.machine is None or getattr(self.machine, "osx", False):
            return
        if self.app.graphics.running == "ati-radeon":
            return
        display = getattr(self.app.presenter, "display", None)
        if not display or not display.get("mode"):
            return
        settings = dict(self.app.settings.applied)
        tw, th = guest_geometry(display, settings.get("display.guest_size", "auto"),
                                settings.get("display.pixel_scale", "auto"))
        if self.guest_surface and list(self.guest_surface) == [tw, th]:
            self.manual_resize_notice = None
            return
        session = self.session
        nudged = await self._nudge_host_resize(tw, th)
        if nudged:
            await asyncio.sleep(1.5)
        if session == self.session and self.guest_surface and list(self.guest_surface) != [tw, th]:
            self.manual_resize_notice = (
                "Mac OS kept a resolution picked in Monitors; restart Mac OS or switch back in Monitors to use host scaling."
            )
            self.log.event("display_resize_blocked", target=[tw, th], surface=self.guest_surface)

    async def _osx_power_state(self) -> str | None:
        frame = await self.dump_frame()
        if frame is None or self.machine is None:
            return None
        self._last_osx_frame = frame
        return await asyncio.to_thread(power_osx.state, self.machine.guest_os,
                                       self.machine.meta.get("os_version"), *frame,
                                       build=self.machine.meta.get("os_build"))

    async def _osx_wait_power_state(self, session: str, states: set[str]) -> str | None:
        """Require two positive frames. Unknown dialogs never authorize a key."""
        until = mono() + 8.0
        previous = None
        while mono() < until and self._session_alive(session):
            current = await self._osx_power_state()
            if current in states and current == previous:
                reference = power_osx.reference(self.machine.guest_os, self.machine.meta.get("os_version"),
                                                self.machine.meta.get("os_build"))
                self.log.event("osx_power_ui", session=session, ui=current, reference=reference)
                return current
            previous = current
            await asyncio.sleep(0.2)
        return None

    async def _osx_shutdown_ui(self, origin: str, job: Job | None = None, *, confirm: bool = True,
                               restart_by_shutdown: bool = False) -> bool:
        """Use the native Apple menu; both Returns require verified UI (Aqua or keyboard-diff fallback).

        A real guest SHUTDOWN/exit still decides success. Application save questions
        remain for the user. Supports measured Aqua references plus Jaguar 10.2.8 /
        non-Aqua themes (e.g. Graphite) via verified menu/modal visual diffs (#45).
        """
        session = self.session
        self.power_failure = "osx-ui"
        if self.machine is None:
            return False
        profile = self.machine.guest_os
        version = self.machine.meta.get("os_version")
        build = self.machine.meta.get("os_build")
        aqua_known = power_osx.supported(profile, version, build)
        fallback_ok = power_osx.fallback_supported(profile, version, build)
        if not aqua_known and not fallback_ok:
            return False
        reference = power_osx.reference(profile, version, build)
        if not self._session_alive(session):
            return False
        mice = await self.qmp.execute("query-mice")
        abs_mice = (sum(bool(m.get("current") and m.get("absolute")) for m in mice if isinstance(m, dict))
                    if isinstance(mice, list) else 0)
        if aqua_known and abs_mice != 1:
            return False
        if abs_mice == 1:
            await self.qmp.execute("input-send-event", {"events": [
                {"type": "abs", "data": {"axis": "x", "value": 24575}},
                {"type": "abs", "data": {"axis": "y", "value": 24575}}]})
            await asyncio.sleep(0.2)  # keep the software cursor out of the measured menu/alert
        self._last_osx_frame = None
        current = await self._osx_power_state() if aqua_known else None
        before_frame = self._last_osx_frame
        if before_frame is None and not aqua_known:
            before_frame = await self.dump_frame()
        via_fallback = False
        menu_frame = None
        if current in {"apple-menu", "shutdown-selected", "shutdown-confirmation"}:
            current = await self._osx_wait_power_state(session, {current})
        else:
            # OS X keyboard polling needs the modifier visible before F2.
            await self.qmp.key_event("ctrl", True)
            try:
                await asyncio.sleep(0.05)
                await self.qmp.key_event("f2", True)
                await asyncio.sleep(0.05)
            finally:
                try:
                    await self.qmp.key_event("f2", False)
                    await asyncio.sleep(0.05)
                finally:
                    await self.qmp.key_event("ctrl", False)
            await asyncio.sleep(0.2)
            if not self._session_alive(session):
                return False
            await self.qmp.send_key("down", hold_ms=100)
            if aqua_known:
                current = await self._osx_wait_power_state(session, {"apple-menu", "shutdown-selected",
                                                                    "shutdown-confirmation"})
                menu_frame = self._last_osx_frame
            else:
                await asyncio.sleep(0.25)
                menu_frame = await self.dump_frame()
            if current is None and fallback_ok and before_frame is not None and menu_frame is not None:
                bw, bh, brgb = before_frame
                mw, mh, mrgb = menu_frame
                if (bw, bh) == (mw, mh) and power_osx.detect_menu_opened(mw, mh, brgb, mrgb):
                    current = "apple-menu"
                    via_fallback = True
        if current == "apple-menu":
            await self.qmp.send_key("end", hold_ms=100)
            await asyncio.sleep(0.15)
            if not self._session_alive(session):
                return False
            await self.qmp.send_key("up", hold_ms=100)
            if aqua_known and not via_fallback:
                current = await self._osx_wait_power_state(session, {"shutdown-selected"})
                sel_frame = self._last_osx_frame
            else:
                await asyncio.sleep(0.25)
                sel_frame = await self.dump_frame()
                current = None
            if current is None and fallback_ok and menu_frame is not None and sel_frame is not None:
                mw, mh, mrgb = menu_frame
                sw, sh, srgb = sel_frame
                if (mw, mh) == (sw, sh) and power_osx.detect_menu_selection_moved(sw, sh, mrgb, srgb):
                    current = "shutdown-selected"
                    via_fallback = True
        if current == "shutdown-selected":
            if not self._session_alive(session):
                return False
            await self.qmp.send_key("ret", hold_ms=100)
            if aqua_known and not via_fallback:
                current = await self._osx_wait_power_state(session, {"shutdown-confirmation"})
                conf_frame = self._last_osx_frame
            else:
                await asyncio.sleep(0.35)
                conf_frame = await self.dump_frame()
                current = None
            if current is None and fallback_ok and before_frame is not None and conf_frame is not None:
                bw, bh, brgb = before_frame
                cw, ch, crgb = conf_frame
                if (bw, bh) == (cw, ch) and power_osx.detect_confirmation_dialog(cw, ch, brgb, crgb):
                    current = "shutdown-confirmation"
                    via_fallback = True
        if current != "shutdown-confirmation" or not self._session_alive(session):
            return False
        if not confirm:
            self.power_failure = None
            return True
        if job:
            job.update(0.4, "Confirming Mac OS X Shut Down…")
        if restart_by_shutdown:
            self.restart_by_shutdown = {"session": session, "mono": mono()}
        try:
            await self.qmp.send_key("ret", hold_ms=100)
        except BaseException:
            if restart_by_shutdown:
                self.restart_by_shutdown = None
            raise
        self.power_failure = None
        self.log.event("osx_shutdown_confirmed", session=session, origin=origin,
                       restart=restart_by_shutdown,
                       reference=reference or (f"{profile}-keyboard-fallback" if via_fallback else None))
        return True

    FORCE_HINT = {
        "restart": "If it is frozen, you can force it to restart; unsaved changes will be lost.",
        "shutdown": "If it is frozen, you can force it to stop; unsaved changes will be lost.",
        "host-poweroff": "If Mac OS is frozen, you can force RetroBoot to turn off; unsaved changes in Mac OS "
                         "will be lost.",
        "host-reboot": "If Mac OS is frozen, you can force RetroBoot to restart; unsaved changes in Mac OS "
                       "will be lost.",
    }

    def _clean_failure(self, purpose: str, what: str, poweroff: bool = False) -> JobFailed:
        """User-facing failure of a clean request; the force action is always offered."""
        hint = self.FORCE_HINT.get(purpose, self.FORCE_HINT["shutdown"])
        ask = "restart" if purpose == "restart" else "shut down"
        still = {"host-poweroff": " RetroBoot is still on.", "host-reboot": " RetroBoot did not restart."}.get(purpose, "")
        if what == "not-ready":
            msg, code = f"Mac OS has not finished starting.{still} Try again in a moment. {hint}", "guest_not_ready"
        elif what == "not-finished":
            # Mac OS is still shutting down: when it finishes, the request completes (late_stop).
            doing = "restarting" if purpose == "restart" else "shutting down"
            after = {"restart": "it starts again", "host-reboot": "RetroBoot restarts",
                     "host-poweroff": "RetroBoot turns off"}.get(
                purpose, "RetroBoot turns off" if poweroff else "RetroBoot stays on")
            msg, code = (f"Mac OS did not finish {doing}; an application may be asking to save changes. "
                         f"When Mac OS finishes, {after}. {hint}"), "guest_unresponsive"
        elif what == "alert":
            msg, code = (f"Mac OS is showing a message that needs an answer, so it can't {ask} yet.{still} Answer "
                         f"the message in Mac OS, then try again. {hint}"), "guest_alert"
        elif what == "busy":
            msg, code = (f"Mac OS stayed busy and didn't respond to the request to {ask}; an application may be "
                         f"working.{still} Try again in a moment. {hint}"), "guest_busy"
        elif what == "osx-ui":
            msg, code = (f"Mac OS X's Shut Down controls could not be verified.{still} "
                         "Choose Shut Down from the Apple menu in Mac OS X, and answer any save questions. "
                         f"{hint}"), "guest_control_unavailable"
        else:
            msg, code = f"Mac OS didn't respond to the request to {ask}.{still} {hint}", "guest_unresponsive"
        return JobFailed(msg, {"force_available": True}, code)

    async def job_clean(self, job: Job, kind: str, poweroff: bool, purpose: str | None = None,
                        resume: Callable[[], object] | None = None) -> dict:
        """``resume`` (a host restart: Restart RetroBoot, Restart to Update, Go Back) starts the
        request again when Mac OS finishes a Shut Down after this job gave up waiting; it
        returns None when there is nothing left to do, and the computer restarts then."""
        purpose = purpose or kind
        if kind != "restart" and self.launch_pending and await self._cancel_start(job):
            job.outcome = "Mac OS was not started."
            if poweroff:
                await self.poweroff("shutdown-request")
                return {"powered_off": True, "start_cancelled": True}
            if not (purpose or "").startswith("host-"):
                await self.app.panel.show_stopped("Mac OS is shut down.")
            return {"state": STOPPED, "start_cancelled": True}
        if not self.active:
            if kind == "restart":
                return await self.job_start(job)
            if poweroff:
                await self.poweroff("shutdown-request")
                return {"powered_off": True}
            return {"state": STOPPED}
        if not self.qmp or self.qmp_ok is False:
            # The emulator does not answer QMP: Mac OS cannot be asked, so fail at once.
            raise self._clean_failure(purpose, "no-response")
        ready = await self.wait_ready(job)
        if not ready:
            raise self._clean_failure(purpose, "not-ready")
        # A host power action (web "Restart/Power off RetroBoot") goes black like a Mac OS
        # Shut Down and must not show "Mac OS is shut down." on the way: on the mini PC
        # that panel flashed for one frame before the reboot went black.
        host_action = (purpose or "").startswith("host-")
        intent = {"restart": "restart"}.get(kind) or ("shutdown-poweroff" if poweroff else
                                                      "shutdown-host" if host_action else "shutdown-hold")
        await self.app.panel.close(to_guest=True)
        await self.app.show_scene("guest", reveal="now")
        osx = bool(self.machine and self.machine.osx)
        by_shutdown = kind == "restart" and (self.family == QUADRA800 or osx)
        if by_shutdown:
            self.log.event("clean_request", session=self.session, kind=kind, poweroff=poweroff, job=job.id,
                           how="shut down, then start")
        else:
            self.log.event("clean_request", session=self.session, kind=kind, poweroff=poweroff, job=job.id)
        job.update(0.3, "Asking Mac OS to shut down…" if kind == "shutdown" else "Asking Mac OS to restart…")
        exit_event = self.exit_event
        session = self.session
        self.intent = intent
        # A new request decides what Mac OS's next Shut Down means (an earlier 68k restart that
        # Mac OS never finished no longer counts), and so does one that gave up on Mac OS.
        self.restart_by_shutdown = None
        self.late_stop = None
        prev_state = self.state
        self.state = STOPPING
        try:
            try:
                async with self.power_lock:
                    if osx:
                        dialog = await self._osx_shutdown_ui(kind, job, restart_by_shutdown=by_shutdown)
                    else:
                        dialog = await self._power_dialog("r" if kind == "restart" and not by_shutdown else "ret",
                                                          kind, job, restart_by_shutdown=by_shutdown)
            except QMPError as exc:
                # QMP stopped answering (a frozen emulator): never an internal error.
                self.log.event("clean_request_failed", session=self.session, why="qmp", error=str(exc))
                raise self._clean_failure(purpose, "no-response")
            if not dialog and not exit_event.is_set() and not self._session_alive(session):
                # Mac OS is stopping by itself (QMP already gone): its exit follows.
                try:
                    await asyncio.wait_for(exit_event.wait(), 10)
                except asyncio.TimeoutError:
                    pass
            if not dialog and not exit_event.is_set():
                raise self._clean_failure(purpose, self.power_failure if self.power_failure in ("busy", "alert", "osx-ui")
                                          else "no-response")
            job.update(0.5, "Waiting for Mac OS to finish…")
            try:
                await asyncio.wait_for(exit_event.wait(), self.cfg.clean_stop_timeout)
            except asyncio.TimeoutError:
                if kind != "restart":
                    # Mac OS took the Shut Down and waits for an application: when it finishes,
                    # this request completes (a restart finishes as a guest-reset anyway).
                    then = ("resume" if resume else "poweroff" if poweroff or purpose == "host-poweroff"
                            else "reboot" if purpose == "host-reboot" else "hold")
                    self.late_stop = {"session": session, "mono": mono(), "purpose": purpose, "then": then,
                                      "resume": resume}
                    self.log.event("late_stop_armed", session=session, purpose=purpose, then=then)
                raise self._clean_failure(purpose, "not-finished", poweroff)
        except JobFailed:
            if self.state == STOPPING:
                self.state = prev_state
                self.intent = None
            raise
        reason = (self.last_exit or {}).get("reason")
        if by_shutdown:
            # A 68k restart: hostd's Return (then a new start), or the user's own Restart in the
            # dialog hostd opened.  The user's own Shut Down is a shut down, as on a Power Mac.
            ok = reason == "guest-reset" or (reason == "guest-shutdown" and
                                             bool((self.last_exit or {}).get("restart_by_shutdown")))
            expected = reason if ok else "guest-reset"
        else:
            expected = "guest-reset" if kind == "restart" else "guest-shutdown"
        if reason != expected:
            raise JobFailed(f"Mac OS stopped unexpectedly ({reason}).", {"exit": self.last_exit}, "unexpected_exit")
        if kind == "restart":
            job.update(0.7, "Starting Mac OS again…")
            await asyncio.sleep(0.5)
            started = await self.wait_started(job, self.cfg.guest_content_timeout * 2 + 30)
            job.outcome = "Mac OS restarted."
            return {"exit": self.last_exit, **started}
        job.outcome = ("Mac OS shut down. RetroBoot is turning off." if poweroff else
                       "Mac OS shut down. RetroBoot is still on.")
        return {"exit": self.last_exit, "powered_off": poweroff}

    async def job_force(self, job: Job, kind: str, poweroff: bool, origin: str) -> dict:
        if not self.active:
            if kind == "restart":
                return await self.job_start(job)
            if poweroff:
                await self.poweroff("force-stop")
            return {"state": STOPPED}
        job.update(0.1, "Saving evidence…")
        # Bounded: a frozen emulator must not delay the explicit escape hatch.
        eid = await self.app.diagnostics.capture_evidence(f"force_{kind}", f"requested from {origin}",
                                                         {"session": self.session, "state": self.state},
                                                         budget_s=3.0)
        self.log.event("force_request", session=self.session, kind=kind, poweroff=poweroff, origin=origin,
                       evidence=eid, warning="unsaved changes in Mac OS may be lost", job=job.id)
        self.intent = "force-restart" if kind == "restart" else ("force-stop-poweroff" if poweroff else "force-stop-hold")
        exit_event = self.exit_event
        job.update(0.3, "Stopping the emulator…")
        pid = (self.handle.status() or {}).get("pid") if self.handle else None
        await self.handle.stop()
        try:
            await asyncio.wait_for(exit_event.wait(), self.cfg.force_kill_after_s)
        except asyncio.TimeoutError:
            # SIGTERM is not enough for a frozen (or stopped) emulator: kill it.
            killed = kill_qemu(pid)
            self.log.event("force_kill", session=self.session, pid=pid, killed=killed)
            try:
                await asyncio.wait_for(exit_event.wait(), 30)
            except asyncio.TimeoutError:
                raise JobFailed("The emulator did not stop.", {"evidence": eid}, "stop_timeout")
        if kind == "restart":
            job.update(0.6, "Starting Mac OS again…")
            await asyncio.sleep(0.5)
            started = await self.wait_started(job, self.cfg.guest_content_timeout * 2 + 30)
            job.outcome = f"Mac OS was restarted. Evidence {eid}."
            return {"evidence": eid, "evidence_id": eid, **started}
        job.outcome = f"Mac OS was stopped. Evidence {eid}."
        return {"evidence": eid, "evidence_id": eid, "exit": self.last_exit}

    async def ensure_stopped_clean(self, job: Job) -> bool:
        """For checkpoint/restore/import: hold-mode clean shutdown.  Returns True if it was running."""
        if not self.active:
            return False
        await self.job_clean(job, "shutdown", poweroff=False)
        return True

    # -- screenshots / status ------------------------------------------------------------
    async def guest_screenshot(self, dest: str, timeout: float = 30.0) -> bool:
        if not self.qmp:
            return False
        tmp = self.paths.screens / f"shot-{uuid.uuid4().hex[:8]}.png"
        try:
            try:
                await self.qmp.execute("screendump", {"filename": str(tmp), "format": "png"}, timeout=timeout)
            except QMPError:
                ppm = tmp.with_suffix(".ppm")
                await self.qmp.screendump(str(ppm), "ppm")
                w, h, rgb = read_ppm(ppm.read_bytes())
                tmp.write_bytes(encode_png(w, h, rgb))
                ppm.unlink()
            await asyncio.to_thread(shutil.move, str(tmp), dest)
            try:
                if Path(dest).stat().st_size < 16 * 1024 * 1024:
                    self.last_frame_png = Path(dest).read_bytes()
            except OSError:
                pass
            return True
        except (QMPError, OSError, ValueError):
            return False
        finally:
            if tmp.exists():
                tmp.unlink()

    def timeline(self) -> list[dict]:
        """This session's startup steps: first occurrence of each, for the active machine only."""
        if self.t_start is None:
            return []
        keep = ("guest_start", "qmp_connected", "guest_running", "guest_content", "guest_revealed", "boot_handoff",
                "finder_detected", "osx_finder_visible", "mac_os_started", "guest_paused", "guest_resumed", "clean_request", "power_key_sent",
                "guest_exited", "disk_repair", "guest_prep")
        machine = self.machine.id if self.machine else None
        out, seen = [], set()
        for r in self.app.lifecycle.since(self.t_start - 5):
            ev = r["event"]
            if ev not in keep or ev in seen:
                continue
            if r.get("session") and self.session and r["session"] != self.session:
                continue
            if r.get("machine") and machine and r["machine"] != machine:
                continue
            seen.add(ev)
            out.append({"event": ev, "t": round(r["t_mono"] - self.t_start, 3), "wall": r["t_wall"]})
        return out

    def web_state(self) -> str:
        if self.state == PAUSED:
            return STARTING
        if self.state == STOPPED and self.failure and not self.relaunching:
            return "failed"
        return self.state

    def detail(self) -> str | None:
        if self.state == STOPPING:
            return "restart" if self.intent in ("restart", "force-restart") else "shutdown"
        if self.state == PAUSED:
            return "Mac OS is paused at the startup options on this computer."
        if self.web_state() == "failed":
            return (self.failure or {}).get("message")
        return None

    def public(self) -> dict:
        return {"state": self.web_state(), "phase": self.state, "detail": self.detail(),
                "responsive": self.responsive if self.state in (RUNNING, STOPPING) else None,
                "unresponsive_since": self.unresponsive_since,
                "unresponsive_reason": self.unresponsive_reason if self.state in (RUNNING, STOPPING) else None,
                "unresponsive_evidence": self.unresponsive_evidence,
                "since": self.started_wall if self.active else (self.last_exit or {}).get("at"),
                "evidence_id": (self.failure or {}).get("evidence"),
                "session": self.session, "machine": self.machine.id if self.machine else None,
                "pid": self.handle.pid if self.handle else None, "paused": self.paused,
                "content": self.content_seen, "revealed": self.revealed, "finder": self.finder_seen,
                "finder_visible": self.osx_finder_visible if self.machine and self.machine.osx else self.finder_seen,
                "mac_os_up": self.mac_os_up,
                "started": self.started_wall,
                "uptime_s": round(mono() - self.t_start, 1) if self.t_start and self.active else None,
                "boot_speed": self.boot_speed, "handoff": self.handoff, "last_exit": self.last_exit,
                "failure": self.failure, "corrupt_disk_recovery": self.corrupt_disk_recovery,
                "timeline": self.timeline()}
