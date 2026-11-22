"""Laptop integration (PRODUCT §16): backlight and volume keys, battery, lid
and the low-battery policy.

Linux owns these devices; Mac OS never sees them.  The presenter reports the
keys and the lid (``host_key``, ``lid`` events) and hostd decides:

* Keys go to Mac OS's own buttons whenever it can take them (keys.py, which
  calls ``on_host_key`` only for the keys it leaves to the host and draws the
  presenter's indicator from the result).
* Brightness keys step the host backlight (``/sys/class/backlight``, firmware
  > platform > raw interface) along a perceptual curve.
* Volume keys step ``audio.volume`` (the same setting the web page shows) and
  mute toggles the host mixer.
* Lid closed blanks the internal panel (DPMS off) and opening restores it.
  Nothing is suspended (HW-02 is later work); the guest keeps running.
* Battery state is read from ``/sys/class/power_supply`` and reported in
  ``GET /api/v1/status`` (``power``).  While discharging, a classic caution
  alert warns once at ``battery_warn_percent``; at ``battery_critical_percent``
  RetroBoot asks Mac OS to Shut Down (the same clean path as the web's
  power-off: ADB power key, verified guest shutdown, then host poweroff).  Only
  if that clean shutdown fails and the battery is at or below
  ``battery_floor_percent`` is the guest force-stopped so the host can still
  power off before the battery cuts out.
* Gauges lie (``DrainEstimate``): the time to empty is the observed
  percent-per-minute slope's when it disagrees with ``power_now`` (and that is
  what Mac OS is told), the critical shutdown also starts when the slope
  projects fewer than ``battery_critical_minutes`` to the floor, and a failed
  clean shutdown is forced when the slope projects ``battery_force_minutes``
  to the floor or ``battery_force_after_s`` after the critical level, whichever
  comes first.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING

from .jobs import Busy, FAILED

if TYPE_CHECKING:
    from .hostd import Hostd

log = logging.getLogger("retroboot.laptop")


def _read(p: Path) -> str | None:
    try:
        return p.read_text().strip()
    except OSError:
        return None


def _int(p: Path) -> int | None:
    v = _read(p)
    try:
        return int(v) if v is not None else None
    except ValueError:
        return None


def _lid_open(root: Path) -> bool | None:
    """ACPI lid state at start (the presenter reports only changes)."""
    for p in sorted((root / "proc/acpi/button/lid").glob("*/state")):
        v = (_read(p) or "").split(":")[-1].strip()
        if v in ("open", "closed"):
            return v == "open"
    return None


EVIOCGSW_8 = (2 << 30) | (8 << 16) | (ord("E") << 8) | 0x1B     # EVIOCGSW(8): the switch states
SW_LID = 0


def _lid_evdev(root: Path) -> bool | None:
    """The lid switch's state at start from its input device (a board without an ACPI lid:
    the Gemini PDA's Hall sensor, SW_LID on gpio-keys): EVIOCGSW on the first event device
    whose ``capabilities/sw`` has SW_LID.  None when unreadable (not root, no device)."""
    try:
        import fcntl
    except ImportError:
        return None
    for cap in sorted((root / "sys/class/input").glob("event*/device/capabilities/sw")):
        words = (_read(cap) or "").split()
        try:
            bits = int(words[-1], 16) if words else 0
        except ValueError:
            continue
        if not bits & (1 << SW_LID):
            continue
        try:
            fd = os.open(root / "dev/input" / cap.parent.parent.parent.name, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            continue
        try:
            buf = bytearray(8)
            fcntl.ioctl(fd, EVIOCGSW_8, buf)
            return not (buf[0] >> SW_LID) & 1        # the switch is on while the lid is closed
        except OSError:
            continue
        finally:
            os.close(fd)
    return None


class Backlight:
    """The panel backlight (sysfs).  Levels follow a square-law curve so each
    key press changes perceived brightness by a similar amount."""

    TYPES = ("firmware", "platform", "raw")
    STEPS = 16
    MIN_PERCENT = 2.0       # the keys never switch the panel fully dark

    def __init__(self, root: Path):
        self.base = root / "sys/class/backlight"

    def device(self) -> Path | None:
        try:
            devs = [d for d in sorted(self.base.iterdir()) if (d / "max_brightness").exists()]
        except OSError:
            return None
        if not devs:
            return None
        devs.sort(key=lambda d: self.TYPES.index(_read(d / "type")) if _read(d / "type") in self.TYPES else 9)
        return devs[0]

    def get(self) -> tuple[int, int] | None:
        d = self.device()
        if not d:
            return None
        cur, mx = _int(d / "brightness"), _int(d / "max_brightness")
        if cur is None or not mx:
            return None
        return cur, mx

    def percent(self) -> float | None:
        v = self.get()
        return None if v is None else round(100.0 * v[0] / v[1], 1)

    def set_percent(self, pct: float) -> float | None:
        d = self.device()
        v = self.get()
        if not d or v is None:
            return None
        pct = max(self.MIN_PERCENT, min(100.0, pct))
        raw = max(1, min(v[1], int(round(pct / 100.0 * v[1]))))
        try:
            (d / "brightness").write_text(str(raw))
        except OSError as exc:
            log.warning("backlight write failed: %s", exc)
            return None
        return round(100.0 * raw / v[1], 1)

    def step(self, direction: int) -> float | None:
        pct = self.percent()
        if pct is None:
            return None
        cur = self.get()
        # Snap to the nearest step of the curve, then move one step; skip steps
        # that round to the current raw level (coarse max_brightness).
        idx = int(round(math.sqrt(max(pct, 0.0) / 100.0) * (self.STEPS - 1)))
        result = pct
        for _ in range(self.STEPS):
            idx = max(0, min(self.STEPS - 1, idx + (1 if direction > 0 else -1)))
            result = self.set_percent(100.0 * (idx / (self.STEPS - 1)) ** 2)
            now = self.get()
            if result is None or now is None or cur is None or now[0] != cur[0] or idx in (0, self.STEPS - 1):
                break
        return result


class DrainEstimate:
    """Time to empty from the observed discharge slope (percent per minute).

    Some battery gauges report placeholder power and voltage (the N5105's EC: a
    constant 3.8 W and 7.6 V, so ``power_now`` promised 222 minutes when about
    25 remained).  While discharging, readings (monotonic seconds, percent)
    are kept for WINDOW_S; the slope is a least-squares fit once they span
    MIN_SPAN_S and MIN_DROP percent.  Any change of power source starts over.
    """

    WINDOW_S = 900.0
    MIN_SPAN_S = 180.0
    MIN_DROP = 2.0
    DISAGREE = 0.25           # relative difference at which the slope wins over power_now
    DISAGREE_MIN = 5.0        # minutes

    def __init__(self):
        self.samples: deque[tuple[float, float]] = deque()

    def reset(self) -> None:
        self.samples.clear()

    def add(self, t: float, pct: float) -> None:
        if self.samples and t <= self.samples[-1][0]:
            return
        self.samples.append((t, float(pct)))
        while self.samples and t - self.samples[0][0] > self.WINDOW_S:
            self.samples.popleft()

    def rate(self) -> float | None:
        """Percent per minute while draining (positive), or None (not enough data)."""
        s = self.samples
        if len(s) < 3 or s[-1][0] - s[0][0] < self.MIN_SPAN_S or s[0][1] - s[-1][1] < self.MIN_DROP:
            return None
        n = len(s)
        mt = sum(t for t, _ in s) / n
        mp = sum(p for _, p in s) / n
        var = sum((t - mt) ** 2 for t, _ in s)
        if var <= 0:
            return None
        slope = sum((t - mt) * (p - mp) for t, p in s) / var     # percent per second
        r = -slope * 60.0
        return r if r > 0 else None

    def minutes_to(self, pct: float, level: float) -> float | None:
        r = self.rate()
        if r is None:
            return None
        return max(0.0, (pct - level) / r)

    def effective(self, bat: dict) -> tuple[int | None, str | None]:
        """The time to empty to report and where it comes from (gauge | slope)."""
        gauge = bat.get("time_to_empty_min")
        pct = bat.get("percent")
        slope = self.minutes_to(pct, 0.0) if pct is not None else None
        if slope is None:
            return (gauge, "gauge") if gauge is not None else (None, None)
        if gauge is None or abs(gauge - slope) > max(self.DISAGREE_MIN, self.DISAGREE * slope):
            return int(slope), "slope"
        return gauge, "gauge"


class PowerSupplies:
    """AC adapter and battery from /sys/class/power_supply.

    ``ac_names``/``battery_names`` (the board profile, platform.py; empty on PCs) are
    name prefixes of supplies that count whatever their type or scope: a Gemini PDA's
    charger is a "USB" supply (bq25890-charger-0) and its fuel gauge a battery whose
    scope is "Device" unless mt6351_gauge was loaded with system=1."""

    def __init__(self, root: Path, ac_names: tuple = (), battery_names: tuple = ()):
        self.base = root / "sys/class/power_supply"
        self.ac_names = tuple(ac_names or ())
        self.battery_names = tuple(battery_names or ())

    def read(self) -> dict:
        ac: bool | None = None
        batteries = []
        try:
            devs = sorted(self.base.iterdir())
        except OSError:
            devs = []
        for d in devs:
            kind = _read(d / "type")
            named_ac = bool(self.ac_names) and d.name.startswith(self.ac_names)
            named_bat = bool(self.battery_names) and d.name.startswith(self.battery_names)
            if kind == "Mains" or (named_ac and kind != "Battery"):
                online = _read(d / "online") == "1"
                ac = bool(ac) or online
            elif kind == "Battery" and (named_bat or _read(d / "scope") != "Device") and _read(d / "present") != "0":
                batteries.append(self._battery(d))
        if self.battery_names:
            # the board's gauge before anything else (bq25890's voltage-derived battery, if enabled)
            batteries.sort(key=lambda b: not b["name"].startswith(self.battery_names))
        bat = batteries[0] if batteries else None
        return {"ac": ac, "battery": bat, "batteries": len(batteries)}

    def reading(self, name: str | None) -> dict:
        """The battery's present current and voltage (mA, mV; the driver's sign), for
        comparing power draw (lid_sleep_done, lid_wake); {} when unreadable."""
        if not name or "/" in name:
            return {}
        d = self.base / name
        out = {}
        i, v = _int(d / "current_now"), _int(d / "voltage_now")
        if i is not None:
            out["battery_ma"] = int(round(i / 1000))
        if v is not None:
            out["battery_mv"] = int(round(v / 1000))
        return out

    @staticmethod
    def _battery(d: Path) -> dict:
        status = _read(d / "status") or "Unknown"
        uv = _int(d / "voltage_now")
        e_now, e_full = _int(d / "energy_now"), _int(d / "energy_full")
        if e_now is None:
            c_now, c_full = _int(d / "charge_now"), _int(d / "charge_full")
            if c_now is not None and uv:
                e_now = c_now * uv // 1_000_000
                e_full = c_full * uv // 1_000_000 if c_full else None
        p_now = _int(d / "power_now")
        if p_now is None:
            i_now = _int(d / "current_now")
            if i_now is not None and uv:
                p_now = abs(i_now) * uv // 1_000_000
        cap = _int(d / "capacity")
        if cap is None and e_now is not None and e_full:
            cap = int(round(100 * e_now / e_full))
        out = {"name": d.name, "percent": cap, "status": status,
               "power_w": round(abs(p_now) / 1e6, 2) if p_now is not None else None,
               "energy_wh": round(e_now / 1e6, 2) if e_now is not None else None,
               "energy_full_wh": round(e_full / 1e6, 2) if e_full else None,
               "voltage_v": round(uv / 1e6, 2) if uv else None,
               "capacity_level": _read(d / "capacity_level")}
        if status == "Discharging" and p_now and e_now is not None:
            out["time_to_empty_min"] = int(e_now / abs(p_now) * 60)
        elif status == "Charging" and p_now and e_now is not None and e_full:
            out["time_to_full_min"] = int(max(0, e_full - e_now) / abs(p_now) * 60)
        return out


class Laptop:
    def __init__(self, app: "Hostd"):
        self.app = app
        cfg = app.cfg
        root = Path(cfg.root)
        self.backlight = Backlight(root)
        prof = app.platform.profile if getattr(app, "platform", None) is not None else {}
        self.supplies = PowerSupplies(root, prof.get("ac_supplies", ()), prof.get("system_batteries", ()))
        self.warn_pct = cfg.battery_warn_percent
        self.critical_pct = cfg.battery_critical_percent
        self.floor_pct = cfg.battery_floor_percent
        self.poll_s = cfg.battery_poll_s
        self.lid_open: bool | None = _lid_open(root)
        if self.lid_open is None and prof.get("lid_evdev"):
            self.lid_open = _lid_evdev(root)
        self.muted = False
        self.power: dict = {"ac": None, "battery": None, "batteries": 0}
        self.battery_state = "ok"          # ok | warned | critical | forced
        self.drain = DrainEstimate()
        self.crit_minutes = float(getattr(cfg, "battery_critical_minutes", 3.0))
        self.force_minutes = float(getattr(cfg, "battery_force_minutes", 1.0))
        self.force_after_s = float(getattr(cfg, "battery_force_after_s", 180.0))
        self._critical_since: float | None = None
        self._now = time.monotonic       # tests drive a clock of their own
        self._low_count = 0
        self._task: asyncio.Task | None = None
        self._critical_task: asyncio.Task | None = None

    # -- lifecycle ---------------------------------------------------------------------
    async def start(self) -> None:
        self.power = self.supplies.read()
        if self.power["battery"] or self.backlight.device():
            self.app.lifecycle.event("laptop", ac=self.power["ac"], battery=(self.power["battery"] or {}).get("percent"),
                                     backlight=self.backlight.percent())
        self._task = asyncio.get_running_loop().create_task(self._poll_loop())

    async def stop(self) -> None:
        for t in (self._task, self._critical_task):
            if t:
                t.cancel()

    def estimated(self, power: dict) -> dict:
        """``power`` (PowerSupplies.read) with the time to empty this laptop believes:
        the slope's when it disagrees with the gauge (sent to Mac OS too, battery.py)."""
        bat = power.get("battery")
        if not bat or bat.get("status") != "Discharging" or power.get("ac"):
            return power
        tte, source = self.drain.effective(bat)
        bat = dict(bat, time_to_empty_source=source)
        if source == "slope":
            bat["gauge_time_to_empty_min"] = bat.get("time_to_empty_min")
            bat["time_to_empty_min"] = tte
        rate = self.drain.rate()
        if rate is not None:
            bat["drain_percent_per_min"] = round(rate, 2)
        return dict(power, battery=bat)

    def public(self) -> dict:
        out = {"ac": self.power.get("ac"), "battery": self.power.get("battery"),
               "backlight_percent": self.backlight.percent(), "lid": None if self.lid_open is None else
               ("open" if self.lid_open else "closed"), "low_battery": self.battery_state, "muted": self.muted,
               "thresholds": {"warn": self.warn_pct, "critical": self.critical_pct, "floor": self.floor_pct}}
        ls = getattr(self.app, "lidsleep", None)
        lp = getattr(self.app, "lidpower", None)
        if ls is not None and (ls.action == "sleep" or (lp is not None and lp.enabled)):
            out["lid_sleep"] = ls.public()       # lidsleep.py (the Gemini PDA; PCs blank only)
        return out

    # -- presenter events --------------------------------------------------------------
    async def on_host_key(self, key: str, repeat: bool = False) -> dict | None:
        """Handle a brightness/volume/mute key on the host. Returns what changed,
        for the indicator: {"kind": brightness|volume|mute, "level": 0..1,
        "muted": bool}, or None (no backlight, unknown key)."""
        if key in ("brightness_up", "brightness_down"):
            pct = self.backlight.step(1 if key == "brightness_up" else -1)
            if not repeat:
                self.app.lifecycle.event("brightness", key=key, percent=pct)
            return None if pct is None else {"kind": "brightness", "level": pct / 100.0}
        elif key in ("volume_up", "volume_down"):
            settings = self.app.settings
            cur = int(settings.applied.get("audio.volume", 70))
            new = max(0, min(100, cur + (6 if key == "volume_up" else -6)))
            if self.muted and key == "volume_up":
                await self._set_mute(False)
            if new != cur:
                try:
                    settings.set_desired_and_applied({"audio.volume": new}, self.app.validation_context())
                except Exception:
                    log.exception("volume setting update failed")
                    return None
                await self.app.apply_engine.push_volume()
            if not repeat:
                self.app.lifecycle.event("volume", key=key, percent=new)
            return {"kind": "volume", "level": new / 100.0, "muted": self.muted}
        elif key == "mute":
            if not repeat:
                await self._set_mute(not self.muted)
                self.app.lifecycle.event("volume", key=key, muted=self.muted)
            vol = int(self.app.settings.applied.get("audio.volume", 70))
            return {"kind": "mute", "level": vol / 100.0, "muted": self.muted}
        return None

    @property
    def can_mute(self) -> bool:
        return bool(self.app.mixer.available and self.app.mixer.control)

    async def set_mute(self, mute: bool, origin: str = "panel") -> None:
        """Mute or unmute this computer's sound at once (the classic panel's Mute, as Sound's)."""
        if mute != self.muted:
            await self._set_mute(mute)
            self.app.lifecycle.event("volume", key="mute", muted=self.muted, origin=origin)

    async def unmute(self) -> None:
        """keys.py: the host mixer back on before Mac OS takes over its own mute."""
        if self.muted:
            await self._set_mute(False)
            self.app.lifecycle.event("volume", key="unmute-for-mac-os", muted=self.muted)

    async def _set_mute(self, mute: bool) -> None:
        mixer = self.app.mixer
        if not mixer.available or not mixer.control:
            return
        if await mixer.set_mute(mute):
            self.muted = mute

    async def on_lid(self, open_: bool) -> None:
        self.lid_open = open_
        self.app.lifecycle.event("lid", open=open_)
        ls = getattr(self.app, "lidsleep", None)
        if open_ and ls is not None and await ls.on_open():
            return       # Mac OS slept and was woken: its wake turns the displays on (battery.py)
        # Blank only the internal panel; an external display keeps showing Mac OS.
        await self.app.presenter.try_request("display_power", output="internal", on=bool(open_))
        if not open_ and ls is not None:
            ls.on_close()      # lid_action "sleep": put Mac OS to sleep (lidsleep.py)

    def battery_reading(self) -> dict:
        bat = (self.power or {}).get("battery") or {}
        try:
            return self.supplies.reading(bat.get("name"))
        except Exception:  # noqa: BLE001
            return {}

    # -- battery -----------------------------------------------------------------------
    def discharging(self) -> bool:
        bat = self.power.get("battery")
        if not bat or self.power.get("ac"):
            return False
        return bat.get("status") == "Discharging"

    async def _poll_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._interval())
                await self.poll()
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("battery poll failed")

    def _interval(self) -> float:
        bat = self.power.get("battery") or {}
        pct = bat.get("percent")
        if self.discharging() and pct is not None and pct <= self.warn_pct + 5:
            return max(5.0, self.poll_s / 3)
        return self.poll_s

    async def poll(self) -> None:
        prev_ac = self.power.get("ac")
        power = self.supplies.read()
        pbat = power.get("battery") or {}
        if pbat.get("percent") is not None and pbat.get("status") == "Discharging" and not power.get("ac"):
            self.drain.add(self._now(), pbat["percent"])
        else:
            self.drain.reset()
        self.power = self.estimated(power)
        bat = self.power.get("battery")
        if prev_ac is not None and self.power.get("ac") != prev_ac:
            self.app.lifecycle.event("ac", online=self.power.get("ac"),
                                     battery=(bat or {}).get("percent"))
        if not bat or bat.get("percent") is None or self.app.mode != "normal":
            return  # the installer reports the battery but never powers off by itself
        pct = bat["percent"]
        if not self.discharging():
            self._low_count = 0
            if self.battery_state == "warned" and (self.power.get("ac") or pct > self.warn_pct + 3):
                self.battery_state = "ok"
            return
        # Two consecutive readings below a threshold (a single low sample at a
        # load step is not acted on).  The slope can bring the critical level
        # forward on a fast drain or a gauge that dies above 0 %.
        to_floor = self.drain.minutes_to(pct, self.floor_pct)
        if pct <= self._critical_threshold() or (to_floor is not None and to_floor < self.crit_minutes):
            self._low_count += 1
            if self._low_count >= 2 and self.battery_state not in ("critical", "forced"):
                await self._critical(pct)
        elif pct <= self.warn_pct:
            self._low_count = 0
            if self.battery_state == "ok":
                await self._warn(pct)
        else:
            self._low_count = 0

    def _critical_threshold(self) -> int:
        battery = getattr(self.app, "battery", None)
        if battery and battery.passthrough_active():
            # Mac OS force-sleeps at 5 %; shut it down cleanly before (battery.py).
            return max(self.critical_pct, int(getattr(self.app.cfg, "battery_passthrough_critical_percent", 8)))
        return self.critical_pct

    async def _warn(self, pct: int) -> None:
        self.battery_state = "warned"
        battery = getattr(self.app, "battery", None)
        passthrough = bool(battery and battery.passthrough_active())
        self.app.lifecycle.event("battery_low", percent=pct, mac_os_warns=passthrough)
        panel = self.app.panel
        if self.app.mode != "normal" or panel.visible or passthrough:
            # With battery passthrough Mac OS shows its own reserve-power alert (battery.py).
            return
        await panel.message("caution", "Your battery is running low.",
                            f"{pct}% remains. Connect the power adapter, or save your work. "
                            "Mac OS will shut down by itself before the battery runs out.")

    async def _critical(self, pct: int) -> None:
        self.battery_state = "critical"
        self._critical_since = self._now()
        to_floor = self.drain.minutes_to(pct, self.floor_pct)
        if pct > self._critical_threshold() and to_floor is not None:
            self.app.lifecycle.event("battery_critical", percent=pct, minutes_to_floor=round(to_floor, 1))
        else:
            self.app.lifecycle.event("battery_critical", percent=pct)
        sup = self.app.supervisor
        panel = self.app.panel
        if self.app.mode == "normal" and sup.active:
            # A short notice over Mac OS, then back to Mac OS so its own Shut
            # Down screen is what the user sees.
            async def go():
                await panel.close()
                self._start_shutdown(pct)
            await panel.message("caution", "The battery is almost empty.",
                                "Mac OS will shut down now and RetroBoot will turn off.", then=go,
                                button="Shut Down", auto_seconds=8)
        else:
            self._start_shutdown(pct)

    RETRY_S = 10.0

    def _force_reason(self, bat: dict) -> str | None:
        """Force the guest off (a clean shutdown failed): at the floor, when the slope
        projects the floor within battery_force_minutes, or battery_force_after_s
        after the critical level (a gauge that dies above its floor)."""
        pct = bat.get("percent") or 0
        if pct <= self.floor_pct:
            return "floor"
        to_floor = self.drain.minutes_to(pct, self.floor_pct)
        if to_floor is not None and to_floor <= self.force_minutes:
            return "slope"
        if self._critical_since is not None and self._now() - self._critical_since >= self.force_after_s:
            return "time"
        return None

    def _start_shutdown(self, pct: int) -> None:
        if self._critical_task and not self._critical_task.done():
            return
        self._critical_task = asyncio.get_running_loop().create_task(self._shutdown(pct))

    async def _shutdown(self, pct: int) -> None:
        try:
            job = self.app.host_job("poweroff", force=False, origin="battery")
        except Busy as exc:
            job = exc.job
        await job.done.wait()
        if job.state != FAILED:
            return
        # The clean shutdown did not complete.  Keep trying cleanly while there
        # is charge; force only at the hard floor so the host can power off.
        self.app.lifecycle.event("battery_shutdown_failed", job=job.id, percent=pct)
        while True:
            await asyncio.sleep(self.RETRY_S)
            power = self.supplies.read()
            pbat = power.get("battery") or {}
            if pbat.get("percent") is not None and pbat.get("status") == "Discharging" and not power.get("ac"):
                self.drain.add(self._now(), pbat["percent"])
            self.power = self.estimated(power)
            bat = self.power.get("battery") or {}
            if not self.discharging():
                self.battery_state = "ok"
                self._critical_since = None
                return
            why = self._force_reason(bat)
            if why:
                self.battery_state = "forced"
                self.app.lifecycle.event("battery_force_poweroff", percent=bat.get("percent"), why=why)
                try:
                    forced = self.app.host_job("poweroff", force=True, origin="battery-floor")
                    await forced.done.wait()
                except Busy:
                    pass
                return
            try:
                job = self.app.host_job("poweroff", force=False, origin="battery")
            except Busy as exc:
                job = exc.job
            await job.done.wait()
            if job.state != FAILED:
                return
