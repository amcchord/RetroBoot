"""Brightness, volume and mute keys: Mac OS's own where it has them (PRODUCT §16).

Mac OS 9.2.1 has a Button Manager (System file) for the special-function
buttons of Apple's keyboards, which reach it from an ADB device at address 7
(research/keys/reports).  QEMU's ``adb-buttons`` device (package feature
``adb-buttons-1``) is that device, and hostd presses its buttons over QMP:

* On the PMU machine (a laptop with battery passthrough) it is a PowerBook's
  button device (handler 0x1f; ``via-pmu.backlight`` gives Mac OS the
  backlight node and the feature bit its driver wants): volume, mute and
  brightness.  The Cuda machine (desktops, VMs) gets no buttons: Mac OS 9.2.1
  never loads the address-7 button driver there (the AppleDesign Keyboard's
  handler 0x02 works only where the Button Manager is installed, as on the
  PMU machine), so its keys stay the host's, with the presenter's indicator.
* Volume and mute: Mac OS changes its own volume, shows its own "Volume" /
  "Mute On" window and plays its feedback sound at the new level.  Mac OS's
  volume scales the guest's sound (Screamer); the host mixer stays at
  ``audio.volume``, the speaker level the web page and the keys set while Mac
  OS is not running.  If the host is muted, the first volume-up or mute key
  that goes to Mac OS unmutes it, so a mute set while Mac OS was stopped never
  leaves Mac OS silent.
* Brightness (PMU): the screen is the PowerBook's built-in LCD
  (``VGA.builtin-panel`` and RetroBoot's video driver variant,
  emulator/ndrv), so Mac OS's LCD brightness engine (System Monitor Plugins)
  attaches to it: the keys show Mac OS's own "Brightness" window and the
  Monitors control panel ("Color LCD") gets its Brightness slider.  Both drive
  Mac OS's backlight driver, which sets a PMU backlight level that QEMU reports
  over the power bridge (battery.py); hostd follows on the host backlight
  without ever jumping to Mac OS's idea of it.  Mac OS's level scale depends
  on the panel table its driver picks (12, 14, ... on the GPD MicroPC; 25,
  26, ... in the lab), so a brightness key moves the host one of its own
  perceptual steps in the key's direction (Mac OS answered, or is at its own
  limit), and any other change (the Monitors slider, Mac OS dimming by itself)
  moves the host by the change over LEVEL_SPAN levels on its curve.  Mac OS
  sets its brightness while it starts; hostd only remembers that level.
  ``on: false`` (Mac OS switched the lamp off) blanks the internal panel until
  it is switched on again or Mac OS stops.  No report within
  BRIGHTNESS_WAIT_S (Mac OS busy) and hostd steps the panel itself (a late
  report for that key is then only remembered).  With a
  video driver without the built-in panel, Mac OS still changes the level but
  shows nothing, and the presenter draws the indicator.

Everything else is hostd's (laptop.py ``on_host_key``) with the presenter's
indicator (presenter/src/osd.h, the same layout as Mac OS 9's own window):
Mac OS not running, starting (before its Finder appears), asleep or paused,
the panel on screen, the installer, the CD route, a package or setting
without the buttons.  Nothing is drawn before the Finder of a session: the
presenter draws only over a revealed Mac OS or the panel, and hostd asks only
once the Finder was seen.  While Mac OS is not running, volume keys also play
a short click at the new level (the host's own feedback sound).

Setting ``input.mac_os_keys`` (default on, applies at the next start of Mac
OS) turns the buttons device off: every key is then the host's.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import struct
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .hostd import Hostd

log = logging.getLogger("retroboot.keys")

FEATURE = "adb-buttons-1"
SETTING = "input.mac_os_keys"
QOM_PATH = "/machine/peripheral/keys0"
POWERBOOK, DESKTOP = "powerbook", "desktop"
HANDLERS = {POWERBOOK: "0x1f", DESKTOP: "0x02"}
KEYS = ("brightness_up", "brightness_down", "volume_up", "volume_down", "mute")
BUTTON = {"brightness_up": "brightness-up", "brightness_down": "brightness-down",
          "volume_up": "volume-up", "volume_down": "volume-down", "mute": "mute"}
CLICK_NAME = "key-click.wav"


# -- pure helpers (tests/test_keys.py) ----------------------------------------------------
def package_capable(package_root: Path) -> bool:
    """The emulator package has the ADB buttons, the PMU backlight and their firmware."""
    try:
        m = json.loads((Path(package_root) / "manifest.json").read_text())
    except (OSError, ValueError):
        return False
    return FEATURE in (m.get("features") or [])


def buttons_for(settings: dict, *, capable: bool, pmu: bool) -> str | None:
    """The keyboard Mac OS gets for this launch: powerbook (the PMU machine) or None.
    (DESKTOP, the AppleDesign buttons, stays available to the lab: Mac OS 9.2.1 on
    the Cuda machine never loads their driver.)"""
    if not capable or not pmu or not bool(settings.get(SETTING, True)):
        return None
    return POWERBOOK


# Mac OS's PMU backlight levels (lower is brighter): a slider move of this many
# levels is the host's whole range (the default LCD table spans 25..70).
LEVEL_SPAN = 45
# Mac OS's lamp-off level: its Shut Down (and display sleep) set the PMU's
# dimmest value before switching the backlight off; never a user's brightness.
LEVEL_OFF = 0x7f
NDRV_BUILTIN_PANEL = "RETROBOOT_BUILTIN_PANEL"


def machine_args(buttons: str | None, via: str) -> list[str]:
    """Extra QEMU arguments for build_argv (after the power-bridge arguments)."""
    if buttons is None:
        return []
    if buttons == POWERBOOK:
        if via != "pmu-adb":
            raise ValueError("the PowerBook buttons need the PMU machine")
        return ["-global", "via-pmu.backlight=on", "-global", "VGA.builtin-panel=on",
                "-device", f"adb-buttons,bus=adb.0,id=keys0,handler={HANDLERS[POWERBOOK]}"]
    if buttons == DESKTOP:
        return ["-device", f"adb-buttons,bus=adb.0,id=keys0,handler={HANDLERS[DESKTOP]}"]
    raise ValueError(f"unknown buttons {buttons!r}")


def buttons_in_argv(argv: list[str]) -> str | None:
    """The buttons a running session was started with (argv.json; hostd restart)."""
    for a in argv:
        if a.startswith("adb-buttons,"):
            return POWERBOOK if f"handler={HANDLERS[POWERBOOK]}" in a else DESKTOP
    return None


def follow(host_percent: float, prev_level: int, level: int, min_percent: float, span: float = LEVEL_SPAN) -> float:
    """The host backlight after Mac OS moved from prev_level to level (lower is
    brighter): the same fraction of the range on the host's perceptual
    (square-law) curve, from where the host is."""
    u = math.sqrt(max(0.0, min(100.0, host_percent)) / 100.0)
    u = max(0.0, min(1.0, u + (prev_level - level) / float(span)))
    return max(min_percent, 100.0 * u * u)


def ndrv_builtin_panel(ndrv: dict | None) -> bool:
    """The installed video driver variant reports a laptop's built-in LCD."""
    return NDRV_BUILTIN_PANEL in str((ndrv or {}).get("define") or "")


def guest_handles(key: str, buttons: str | None) -> bool:
    """Whether this keyboard has the button (before asking whether Mac OS can take it now)."""
    if buttons is None or key not in KEYS:
        return False
    return buttons == POWERBOOK or not key.startswith("brightness")


def click_wav(rate: int = 44100) -> bytes:
    """A short, soft click (16-bit mono WAV): 25 ms of a decaying 1.6 kHz tone."""
    n = int(rate * 0.025)
    frames = bytearray()
    for i in range(n):
        env = math.exp(-i / (rate * 0.005))
        frames += struct.pack("<h", int(12000 * env * math.sin(2 * math.pi * 1600 * i / rate)))
    hdr = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + len(frames), b"WAVE", b"fmt ", 16, 1, 1, rate,
                      rate * 2, 2, 16, b"data", len(frames))
    return hdr + bytes(frames)


# -- hostd integration -------------------------------------------------------------------
class FunctionKeys:
    """``app.keys``: one route per key press (Mac OS's buttons or the host)."""

    TAP_S = 0.12               # Mac OS shows its window only for a press it sees held
    BRIGHTNESS_WAIT_S = 1.5    # Mac OS's backlight report after a brightness button (first use: ~1 s)
    LATE_S = 3.0               # a report this late after the host stepped is only remembered
    OSD_KINDS = {"brightness": "brightness", "volume": "volume", "mute": "mute"}

    def __init__(self, app: "Hostd"):
        self.app = app
        self.buttons: str | None = None        # launched with this keyboard
        self.session: str | None = None
        self.routed = 0
        self.handled = 0
        self.last: dict | None = None
        self.guest_level: int | None = None    # Mac OS's backlight level (PMU, lower is brighter)
        self.native_brightness = False         # Mac OS shows its own Brightness window (built-in LCD)
        self.lamp_off = False                  # Mac OS switched the backlight off
        self._busy: set[str] = set()
        self._expect: list | None = None   # [direction, deadline, key, host_stepped] of a routed brightness key

    # -- launch ------------------------------------------------------------------------
    def capable(self) -> bool:
        return package_capable(self.app.paths.qemu_package)

    def launch_buttons(self, settings: dict, pmu: bool, boot_cd: bool = False) -> str | None:
        if boot_cd or self.app.mode != "normal":
            return None
        return buttons_for(settings, capable=self.capable(), pmu=pmu)

    def launched(self, session: str, buttons: str | None, ndrv: dict | None = None) -> None:
        self.session = session
        self.buttons = buttons
        self.guest_level = None
        self._expect = None
        self.native_brightness = buttons == POWERBOOK and ndrv_builtin_panel(ndrv)
        self.app.lifecycle.event("keys_machine", session=session, buttons=buttons or "none",
                                 builtin_panel=self.native_brightness)

    async def guest_stopped(self, host_power_off: bool = False) -> None:
        """supervisor: Mac OS has stopped; give the panel its lamp back -- not when the
        host powers off or restarts next: the panel stays as dark as Mac OS left it."""
        self.buttons = None
        self.guest_level = None
        self._expect = None
        if self.lamp_off:
            self.lamp_off = False
            if host_power_off:
                self.app.lifecycle.event("guest_backlight_kept_off", why="host power-off")
            elif self.app.laptop.lid_open is not False:
                await self.app.presenter.try_request("display_power", output="internal", on=True)

    def public(self) -> dict:
        return {"buttons": self.buttons, "route": "mac-os" if self._guest_ready() else "host",
                "builtin_panel": self.native_brightness, "guest_level": self.guest_level,
                "routed": self.routed, "handled": self.handled, "last": self.last,
                "requested": bool(self.app.settings.applied.get(SETTING, True)), "capable": self.capable()}

    # -- routing -----------------------------------------------------------------------
    def _guest_running(self) -> bool:
        sup = self.app.supervisor
        return bool(getattr(sup, "active", False)) and getattr(sup, "session", None) == self.session

    UP_AFTER_S = 60.0          # a revealed Mac OS this long after its start counts as up without the probe

    def _mac_os_up(self) -> bool:
        """Mac OS is past its startup: the supervisor's mac_os_up (its Finder recognized in
        any Appearance theme, or Mac OS started without it), or it has been on screen for
        UP_AFTER_S (e.g. a guest adopted after a hostd restart)."""
        sup = self.app.supervisor
        if getattr(sup, "mac_os_up", getattr(sup, "finder_seen", False)):
            return True
        t0 = getattr(sup, "t_start", None)
        return bool(getattr(sup, "revealed", False) or getattr(sup, "content_seen", False)) and \
            isinstance(t0, (int, float)) and time.monotonic() - t0 >= self.UP_AFTER_S

    def _guest_ready(self) -> bool:
        """Mac OS can take a button now: running this session, its Finder seen,
        awake, not paused and on screen (no panel over it)."""
        sup = self.app.supervisor
        if self.buttons is None or not self._guest_running():
            return False
        if not self._mac_os_up() or getattr(sup, "paused", False) or not getattr(sup, "qmp", None):
            return False
        if getattr(self.app.battery, "asleep", False):
            return False
        if getattr(self.app.panel, "visible", False):
            return False
        scene = (getattr(self.app, "scene", None) or {}).get("scene")
        return scene in (None, "guest")

    def _stopping(self) -> bool:
        """Mac OS is being shut down or restarted (hostd's request or its own)."""
        sup = self.app.supervisor
        return getattr(sup, "state", None) == "stopping" or bool(getattr(sup, "intent", None))

    def _osd_allowed(self) -> bool:
        """The presenter's indicator: never over Mac OS before its Finder (startup and
        hand-over); otherwise the presenter decides (only over the panel or a revealed
        Mac OS, never the checkerboard or a held frame)."""
        if getattr(self.app.panel, "visible", False):
            return True
        if self._guest_running():
            return self._mac_os_up()
        return True

    async def on_host_key(self, key: str, repeat: bool = False) -> None:
        if key not in KEYS:
            return
        if guest_handles(key, self.buttons) and self._guest_ready():
            if key in self._busy:
                return            # a held key: one press at a time (repeats are Mac OS's own presses)
            self._busy.add(key)
            try:
                if await self._route(key):
                    return
            finally:
                self._busy.discard(key)
        await self._host(key, repeat)

    async def _route(self, key: str) -> bool:
        """Press the button in Mac OS; False if Mac OS could not take it (hostd handles the key)."""
        qmp = self.app.supervisor.qmp
        if key in ("volume_up", "mute") and getattr(self.app.laptop, "muted", False):
            await self.app.laptop.unmute()       # Mac OS's own mute from now on
        if key.startswith("brightness"):
            self._expect = [1 if key == "brightness_up" else -1, time.monotonic() + self.BRIGHTNESS_WAIT_S, key, False]
        name = BUTTON[key]
        try:
            await qmp.execute("qom-set", {"path": QOM_PATH, "property": "press", "value": name + ":down"}, timeout=2)
            try:
                await asyncio.sleep(self.TAP_S)
            finally:
                await qmp.execute("qom-set", {"path": QOM_PATH, "property": "press", "value": name + ":up"}, timeout=2)
        except Exception as exc:     # QMP gone, QEMU stopping: the host takes the key
            log.info("button %s not delivered: %s", name, exc)
            self._expect = None
            return False
        self.routed += 1
        self.last = {"key": key, "route": "mac-os"}
        self.app.lifecycle.event("key", key=key, route="mac-os")
        if key.startswith("brightness"):
            asyncio.get_running_loop().create_task(self._brightness_watchdog(key))
        return True

    async def _brightness_watchdog(self, key: str) -> None:
        exp = self._expect
        if not exp:
            return
        await asyncio.sleep(max(0.0, exp[1] - time.monotonic()))
        if self._expect is exp and not exp[3]:   # Mac OS said nothing: the host steps the panel
            exp[3] = True
            exp[1] = time.monotonic() + self.LATE_S
            self.app.lifecycle.event("key", key=key, route="host", why="no-backlight-report")
            await self._host(key, False)
            await asyncio.sleep(self.LATE_S)
            if self._expect is exp:
                self._expect = None

    async def _host(self, key: str, repeat: bool) -> None:
        result = await self.app.laptop.on_host_key(key, repeat)
        self.handled += 1
        self.last = {"key": key, "route": "host"}
        if not result:
            return
        await self._show(result)
        panel_open = bool(getattr(self.app.panel, "visible", False))
        if result.get("kind") in ("volume", "mute") and not repeat and (not self._guest_running() or panel_open):
            self._click(result)

    async def _show(self, result: dict) -> None:
        if not self._osd_allowed():
            return
        await self.app.presenter.try_request("osd", kind=self.OSD_KINDS[result["kind"]],
                                             level=max(0.0, min(1.0, float(result.get("level") or 0.0))),
                                             muted=bool(result.get("muted")))

    def _click(self, result: dict) -> None:
        """The host's feedback sound at the new level (Mac OS stopped, or panel open)."""
        if result.get("muted") or not (result.get("level") or 0) > 0:
            return
        try:
            from . import audio as hostaudio
            out = self.app.choose_audio_output()
            path = self.app.paths.run / CLICK_NAME
            if not path.exists():
                path.write_bytes(click_wav())
            cmd = hostaudio.playback_command(path, out)
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except Exception as exc:      # no aplay, no device: the indicator alone
            log.info("key click not played: %s", exc)

    # -- Mac OS's backlight (battery.py bridge) --------------------------------------------
    async def on_backlight(self, msg: dict) -> None:
        level = msg.get("level")
        on = msg.get("on")
        if on is False and not self.lamp_off:
            self.lamp_off = True
            self.app.lifecycle.event("guest_backlight", on=False)
            await self.app.presenter.try_request("display_power", output="internal", on=False)
        elif on is True and self.lamp_off:
            self.lamp_off = False
            self.app.lifecycle.event("guest_backlight", on=True)
            if self.app.laptop.lid_open is not False:
                await self.app.presenter.try_request("display_power", output="internal", on=True)
        if not isinstance(level, int) or isinstance(level, bool):
            return
        prev, self.guest_level = self.guest_level, level
        if level >= LEVEL_OFF or (prev is not None and prev >= LEVEL_OFF):
            self.guest_level = None if level >= LEVEL_OFF else level
            return      # Mac OS darkening the lamp (Shut Down, display sleep), or back from it: no move
        if msg.get("why") == "hello" or not self._mac_os_up() or self._stopping():
            return      # a reconnect repeating the state, Mac OS starting or shutting down: only remembered
        exp = self._expect
        if exp is not None:
            self._expect = None
            if exp[3]:
                return  # the host already stepped for this key (Mac OS answered late)
        elif prev is None or level == prev:
            return      # no baseline, or the same level again (the Monitors slider repeats it)
        bl = self.app.laptop.backlight
        if exp is not None:
            pct = bl.step(exp[0])       # one host step per key, also at Mac OS's own limit
        else:
            cur = bl.percent()
            pct = None if cur is None else bl.set_percent(follow(cur, prev, level, bl.MIN_PERCENT))
        self.app.lifecycle.event("brightness", key="mac-os", percent=pct, guest_level=level)
        if pct is not None and exp is not None and not self.native_brightness:
            await self._show({"kind": "brightness", "level": pct / 100.0})
