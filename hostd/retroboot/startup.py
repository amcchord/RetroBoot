"""Startup progress (ARCHITECTURE 4.4.1): times the presenter's startup indicator.

While the checkerboard waits for Mac OS, the presenter shows a faceless
compact Mac whose screen is a progress bar of RetroBoot's own startup. hostd
drives it with real milestones and a per-machine timing model learned from
previous startups; the presenter animates the bar between them, never moves
it backwards (a new attempt restarts it at 0) and completes it only at the
genuine Mac OS cut-over. Nothing here claims Mac OS is ready.

Milestones (``startup_progress`` with the expected time left until the cut-over):

* presenter connected on a cold boot: the learned kernel time of the cut-over
  (the splash already timed the bar from the same number on the command line);
* a start (disk checks, prep): ``prepare`` time + launch time; relaunches,
  retries and user starts begin again at 0 (``reset``);
* QEMU launched (``guest_start``): the learned launch time of this machine;
* resumed after the startup options: what was left of it.

Model, ``state/startup-timing.json`` (exponential moving averages, an unusual
startup moves them only a little)::

    {"version": 1, "cold_ms": kernel time (CLOCK_BOOTTIME) of the cut-over on cold boots,
     "launch_ms": {machine: QEMU launch -> cut-over}, "prep_ms": start -> QEMU launch,
     "samples": n}

rb-bootdisplay reads ``cold_ms`` into GRUB's display.cfg (``retroboot.startup_ms``)
so the initramfs splash can move the bar before hostd runs.
"""
from __future__ import annotations

import asyncio
import logging
import statistics
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .util import atomic_write_json, mono, read_json

if TYPE_CHECKING:
    from .hostd import Hostd

log = logging.getLogger("retroboot.startup")

DEFAULT_LAUNCH_MS = 9000.0   # no model yet (lab VM ~7.5 s, Ryzen ~6.9 s, N4100 ~25 s)
DEFAULT_PREP_MS = 300.0
MAX_SAMPLE_MS = 600000.0
MAX_PREP_SAMPLE_MS = 10000.0  # a disk check or the first guest prep is not the usual start
BOOTDISPLAY_DELAY_S = 45.0    # rewrite GRUB's timing once Mac OS has settled
COLD_HOSTD_MAX_MS = 90000.0   # hostd started this late in a boot: not the boot's own startup
OUTLIER = 4.0                 # a sample this far from an established model is not learned


def machine_directory_style(directory: Path) -> str:
    """Appearance only, from canonical validated metadata; never infer it from a name."""
    from .machines import validate_meta, OSX_PROFILES
    try:
        meta = validate_meta(read_json(directory / "machine.json", 1024 * 1024), directory.name)
        return "macosx" if meta.get("guest_os") in OSX_PROFILES else "classic"
    except (OSError, ValueError, TypeError):
        return "classic"


def embedded_style(embedded: Path) -> str:
    """Only media containing exclusively canonical OS X Macs has a known X style."""
    many = embedded / "machines"
    try:
        dirs = [p for p in many.iterdir() if p.is_dir() and not p.name.startswith(".")]
    except OSError:
        dirs = []
    if (embedded / "machine").is_dir():
        dirs.append(embedded / "machine")
    # An archive has no validated per-Mac identity until import.
    try:
        archives = any(p.name.endswith((".tar.gz", ".tgz")) for p in embedded.iterdir() if p.is_file())
    except OSError:
        archives = False
    return "macosx" if dirs and not archives and all(machine_directory_style(p) == "macosx" for p in dirs) else "classic"


def boot_ms() -> float:
    """CLOCK_BOOTTIME in ms: the clock of the kernel command line's startup_ms."""
    try:
        return time.clock_gettime(time.CLOCK_BOOTTIME) * 1000.0
    except (AttributeError, OSError):
        return time.monotonic() * 1000.0


def ema(old: float | None, sample: float, alpha: float = 0.35) -> float:
    """Moving average; a sample far from the model (a hang, a cold disk) moves it a little."""
    if old is None or old <= 0:
        return float(sample)
    if sample > old * 2.5 or sample < old / 2.5:
        alpha = 0.1
    return old + alpha * (sample - old)


class StartupProgress:
    def __init__(self, app: "Hostd"):
        self.app = app
        self.path = app.paths.state / "startup-timing.json"
        self.model = self._load()
        # A cold boot's startup: hostd runs early in this kernel boot and no earlier
        # hostd showed Mac OS in it (a marker in /run, which a boot clears). A hostd
        # restarted later never teaches the cold-boot model.
        self.cold_marker = app.paths.run / "startup-cold-done"
        self.cold = boot_ms() < COLD_HOSTD_MAX_MS and not self.cold_marker.exists()
        self.attempt: dict | None = None
        self.last: dict | None = None  # the last startup's timings (status)
        self._bootdisplay: asyncio.TimerHandle | None = None

    # -- model ---------------------------------------------------------------------
    def _load(self) -> dict:
        model = {"version": 1, "cold_ms": None, "launch_ms": {}, "prep_ms": None, "samples": 0}
        try:
            data = read_json(self.path, 65536)
        except (OSError, ValueError):
            return model
        if not isinstance(data, dict) or data.get("version") != 1:
            return model
        for key in ("cold_ms", "prep_ms"):
            v = data.get(key)
            if isinstance(v, (int, float)) and 0 < v < MAX_SAMPLE_MS:
                model[key] = float(v)
        launch = data.get("launch_ms")
        if isinstance(launch, dict):
            model["launch_ms"] = {str(k): float(v) for k, v in launch.items()
                                  if isinstance(v, (int, float)) and 0 < v < MAX_SAMPLE_MS}
        if isinstance(data.get("samples"), int):
            model["samples"] = data["samples"]
        return model

    def _save(self) -> None:
        try:
            atomic_write_json(self.path, self.model)
        except OSError as exc:
            log.warning("cannot save the startup timing model: %s", exc)

    def launch_estimate(self, machine: str | None) -> float:
        launch = self.model["launch_ms"]
        if machine and machine in launch:
            return launch[machine]
        if launch:
            return statistics.median(launch.values())
        plat = getattr(self.app, "platform", None)
        return (plat.launch_ms_default if plat is not None else None) or DEFAULT_LAUNCH_MS

    def prep_estimate(self) -> float:
        return self.model.get("prep_ms") or DEFAULT_PREP_MS

    # -- presenter -------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self.style() != "macosx" and bool(self.app.settings.applied.get("startup.progress_indicator", True))

    def style(self) -> str:
        if getattr(self.app, "mode", None) == "installer":
            plan = getattr(getattr(getattr(self.app, "panel", None), "st", None), "plan", None)
            if plan:
                selected = next((m for m in plan if m.get("startup")), plan[0])
                choice = selected.get("choice")
                if getattr(choice, "kind", None) == "dir" and getattr(choice, "path", None):
                    return machine_directory_style(choice.path)
                entry = getattr(choice, "entry", None) or {}
                if entry.get("machineFamily") == "powerMacOSX" and entry.get("guestOS") in \
                        ("macosx-10.2", "macosx-10.3", "macosx-10.4"):
                    return "macosx"
                return "classic"
            return embedded_style(self.app.paths.installer_dir / "embedded")
        registry = getattr(self.app, "machines", None)
        if registry is not None:
            sup = getattr(self.app, "supervisor", None)
            machine = getattr(sup, "machine", None) if getattr(sup, "active", False) else None
            if machine is None:
                wanted = getattr(self.app.settings, "desired", None) or self.app.settings.applied
                machine = registry.get(wanted.get("machine.active"))
            if machine is not None and machine.osx:
                return "macosx"
        return "classic"

    def art(self) -> str | None:
        """Mac OS's own Happy Mac (the presenter removes its face): the copy the
        presenter captured from the guest's startup screen, else the Platinum
        bundle's identical icon; None: RetroBoot's fallback art."""
        for icon in (self.app.paths.startup_icon, self.app.paths.platinum_icon):
            if icon.is_file():
                return str(icon)
        return None

    def happy_mac(self) -> str:
        """When Mac OS draws its Happy Mac at this startup.  "late" with the ATI Radeon:
        its startup screen shows it only 1.5-3.5 s after the gray (ATI's driver is not
        in the ROM), so the presenter reveals the gray at once and keeps the indicator
        where the Happy Mac will appear until it does (ARCHITECTURE 4.4.1); "prompt"
        with the standard card."""
        graphics = getattr(self.app, "graphics", None)
        try:
            return "late" if graphics is not None and graphics.startup_guest_size() else "prompt"
        except Exception:          # never break the startup indicator over this
            return "prompt"

    def config(self) -> dict:
        osx = self.style() == "macosx"
        return {"enabled": self.enabled, "style": self.style(), "art": None if osx else self.art(),
                "capture": None if osx else str(self.app.paths.startup_icon),
                "happy_mac": "prompt" if osx else self.happy_mac()}

    async def on_icon(self, msg: dict) -> None:
        """The presenter saved (or confirmed) Mac OS's Happy Mac."""
        self.app.lifecycle.event("startup_icon", valid=msg.get("valid"), saved=msg.get("saved"),
                                 written=msg.get("written"))
        if msg.get("saved") and msg.get("written"):
            await self.push_config()       # the exact art from the next startup on
            self._schedule_bootdisplay()   # ... and in GRUB's background for the next boot

    async def push_config(self) -> None:
        await self.app.presenter.try_request("startup_config", **self.config())

    async def _send(self, remaining_ms: float, phase: str, reset: bool = False) -> None:
        if not self.enabled:
            return
        remaining = max(0, int(round(remaining_ms)))
        self.app.lifecycle.event("startup_progress", phase=phase, remaining_ms=remaining, reset=reset or None)
        await self.app.presenter.try_request("startup_progress", remaining_ms=remaining, phase=phase, reset=reset)

    def _machine(self) -> str | None:
        return self.app.settings.applied.get("machine.active")

    # -- milestones --------------------------------------------------------------------
    async def on_presenter_connect(self) -> None:
        """After hostd's push_presenter (which includes startup_config)."""
        sup = self.app.supervisor
        if self.app.mode != "normal" or not self.cold or sup.active or sup.booted:
            return
        # Cold boot, before the launch: the learned cut-over (what the splash used),
        # unless that leaves less than a launch.
        est = self.prep_estimate() + self.launch_estimate(self._machine())
        cold = self.model.get("cold_ms")
        left = cold - boot_ms() if cold else None
        await self._send(left if left and left > est else est, "host")

    async def on_start(self, reason: str, attempt: int, machine: str | None) -> None:
        """A start is under way (scene splash): disk checks, then the launch."""
        cold = self.cold and reason == "boot" and attempt == 1
        self.attempt = {"reason": reason, "attempt": attempt, "machine": machine, "t_start": mono(),
                        "t_launch": None, "paused": False, "t_pause": None, "cold": cold}
        await self.push_config()
        # Relaunches, retries and user starts are new startups: the bar starts again.
        await self._send(self.prep_estimate() + self.launch_estimate(machine),
                         "prepare" if cold else reason, reset=not cold)

    async def on_launched(self) -> None:
        a = self.attempt
        if not a:
            return
        a["t_launch"] = mono()
        prep = (a["t_launch"] - a["t_start"]) * 1000.0
        if 0 < prep < MAX_PREP_SAMPLE_MS:
            self.model["prep_ms"] = ema(self.model.get("prep_ms"), prep, 0.2)
        await self._send(self.launch_estimate(a["machine"]), "launch")

    def on_paused(self) -> None:
        a = self.attempt
        if a:
            a["paused"] = True
            a["t_pause"] = mono()

    async def on_resumed(self) -> None:
        a = self.attempt
        if not a or not a["t_launch"]:
            return
        ran = ((a["t_pause"] or mono()) - a["t_launch"]) * 1000.0
        await self._send(max(self.launch_estimate(a["machine"]) - ran, 1500.0), "resume")

    def on_failed(self) -> None:
        """No genuine content (timeout or crash): nothing is learned from it."""
        self.attempt = None

    def on_revealed(self, msg: dict) -> None:
        """The cut-over: Mac OS's own startup screen is on the display."""
        a, self.attempt = self.attempt, None
        was_cold, self.cold = self.cold, False
        if was_cold:
            try:
                self.cold_marker.parent.mkdir(parents=True, exist_ok=True)
                self.cold_marker.touch()
            except OSError:
                pass
        if not a or not a["t_launch"]:
            return
        now = msg.get("mono_ms")
        now = now / 1000.0 if isinstance(now, (int, float)) and now > 0 else mono()
        launch = (now - a["t_launch"]) * 1000.0
        at_boot = msg.get("boot_ms") if isinstance(msg.get("boot_ms"), (int, float)) else boot_ms()
        self.last = {"reason": a["reason"], "attempt": a["attempt"], "launch_ms": round(launch),
                     "boot_ms": round(at_boot) if a["cold"] else None, "learned": not a["paused"]}
        if a["paused"] or not 0 < launch < MAX_SAMPLE_MS:
            return
        machine = a["machine"] or "default"
        old_launch = self.model["launch_ms"].get(machine)
        if self._plausible(old_launch, launch):
            self.model["launch_ms"][machine] = ema(old_launch, launch)
        learned_cold = (a["cold"] and was_cold and 0 < at_boot < MAX_SAMPLE_MS and
                        self._plausible(self.model.get("cold_ms"), at_boot))
        if learned_cold:
            self.model["cold_ms"] = ema(self.model.get("cold_ms"), at_boot)
        self.model["samples"] = int(self.model.get("samples", 0)) + 1
        self._save()
        self.app.lifecycle.event("startup_learned", machine=machine, launch_ms=round(launch),
                                 model_launch_ms=round(self.model["launch_ms"][machine]),
                                 cold_ms=round(at_boot) if learned_cold else None,
                                 model_cold_ms=round(self.model["cold_ms"]) if self.model.get("cold_ms") else None)
        if learned_cold:
            self._schedule_bootdisplay()

    def _plausible(self, model: float | None, sample: float) -> bool:
        """With an established model (two or more samples), a sample more than
        OUTLIER times off is a different situation (a hang, a lab restart), not
        a slower or faster machine."""
        if not model or int(self.model.get("samples", 0)) < 2:
            return True
        return model / OUTLIER <= sample <= model * OUTLIER

    def _schedule_bootdisplay(self) -> None:
        """GRUB's display.cfg carries the cut-over time for the next boot's splash;
        rb-bootdisplay rewrites it only when it moved noticeably (hysteresis)."""
        loop = self.app.loop
        if loop is None:
            return
        if self._bootdisplay:
            self._bootdisplay.cancel()
        self._bootdisplay = loop.call_later(
            BOOTDISPLAY_DELAY_S,
            lambda: loop.create_task(self.app.apply_engine.update_boot_display(why="startup timing")))

    def public(self) -> dict:
        return {"enabled": self.enabled,
                "model": {"cold_ms": round(self.model["cold_ms"]) if self.model.get("cold_ms") else None,
                          "launch_ms": {k: round(v) for k, v in self.model["launch_ms"].items()},
                          "prep_ms": round(self.model["prep_ms"]) if self.model.get("prep_ms") else None,
                          "samples": self.model.get("samples", 0)},
                "last": self.last}
