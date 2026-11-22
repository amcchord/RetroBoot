"""Battery passthrough: the host's battery in Mac OS (PRODUCT §16, ARCHITECTURE §12).

On a laptop RetroBoot runs Mac OS on a PowerBook-style Power Manager: QEMU's
mac99 with ``via=pmu-adb`` and ``via-pmu.battery=on`` (package feature
``pmu-battery-1``).  Mac OS's own Power Manager then sees one smart battery
and drives the Control Strip Battery Monitor, the menu-bar battery, Energy
Saver and its own low-battery alerts, with no guest software installed.  This
module owns hostd's side of the power bridge ``/run/retroboot/power.sock``:
it reads ``/sys/class/power_supply`` (``laptop.PowerSupplies``) and sends the
battery to QEMU, and it follows Mac OS's sleep and wake.

Desktops and VMs (no host battery) keep the Cuda machine.  The setting
``system.battery_passthrough`` (capability ``battery``: a host battery and a
capable package) turns it off on a laptop; ``hostd.json``
``power_controller`` (``auto`` | ``cuda`` | ``pmu``) overrides it for the lab.

Sleep: Mac OS's own Sleep (Special menu, its reserve-battery sleep; guest
prep sets Energy Saver's portable timers to Never, PREP_VERSION 3, so it does
not sleep when idle) halts the emulated processor; QEMU says ``sleep`` and hostd turns the
displays off (the presenter keeps routing input).  A key or a click wakes Mac
OS (QEMU's own wake input handler), and hostd wakes it for the host power button and
before any clean shutdown or restart it asks for, then turns the displays on
again.  The host itself never sleeps.

Low battery, with passthrough active: Mac OS shows its own "reserve battery
power" alert (at 15 %), so hostd shows none of its own at
``battery_warn_percent``; Mac OS puts itself to sleep when no reserve remains
(5 %), so hostd's critical clean shutdown (laptop.py) starts at
``battery_passthrough_critical_percent`` (8) and wakes a sleeping Mac OS
first (``ensure_awake``).  Without passthrough, RetroBoot's caution alert is
the only warning and the critical level is ``battery_critical_percent``.

Integration points elsewhere are deliberately small: ``schema.py`` (the
setting), ``machines.build_argv`` (``LaunchOptions.power_socket``),
``supervisor`` (``launch_socket``, ``ensure_awake`` before the power key,
power button), ``freeze.py`` (no frozen-guest check while asleep), ``laptop.py``
(warning suppressed while active) and ``hostd.py`` (start/stop, status,
capabilities).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .hostd import Hostd

log = logging.getLogger("retroboot.battery")

FEATURE = "pmu-battery-1"
SOCKET_NAME = "power.sock"
SETTING = "system.battery_passthrough"
PROTOCOL_VERSION = 1
LINE_MAX = 4096
CONTROLLERS = ("auto", "cuda", "pmu")


# -- pure helpers (tested in tests/test_battery.py) ------------------------------------
def package_capable(package_root: Path) -> bool:
    """The emulator package has the PMU battery (and its firmware gate)."""
    try:
        m = json.loads((Path(package_root) / "manifest.json").read_text())
    except (OSError, ValueError):
        return False
    return FEATURE in (m.get("features") or [])


def host_has_battery(power: dict | None) -> bool:
    return bool((power or {}).get("batteries"))


def use_pmu(settings: dict, *, capable: bool, has_battery: bool, controller: str = "auto") -> bool:
    """Whether the next disk boot runs on the PMU machine with a battery bay."""
    if not capable or controller == "cuda":
        return False
    if controller == "pmu":
        return True
    return has_battery and bool(settings.get(SETTING, True))


def guest_battery(power: dict | None) -> dict:
    """The bridge's ``battery`` message from ``PowerSupplies.read()``."""
    power = power or {}
    bat = power.get("battery")
    if not bat or bat.get("percent") is None:
        ac = power.get("ac")
        return {"type": "battery", "present": False, "ac": True if ac is None else bool(ac)}
    status = bat.get("status") or "Unknown"
    ac = power.get("ac")
    if ac is None:
        # No Mains supply listed: infer the adapter from the battery's status.
        ac = status in ("Charging", "Full", "Not charging")
    pct = max(0, min(100, int(round(bat["percent"]))))
    msg: dict[str, Any] = {"type": "battery", "present": True, "ac": bool(ac),
                           "charging": bool(ac) and status == "Charging", "percent": pct}
    if not ac and bat.get("time_to_empty_min"):
        msg["time_to_empty_min"] = int(bat["time_to_empty_min"])
    if msg["charging"] and bat.get("time_to_full_min"):
        msg["time_to_full_min"] = int(bat["time_to_full_min"])
    if bat.get("voltage_v"):
        msg["voltage_mv"] = int(round(float(bat["voltage_v"]) * 1000))
    return msg


def machine_args(power_socket: Path | None) -> tuple[str, list[str]]:
    """``via=`` value and extra QEMU arguments for build_argv."""
    if power_socket is None:
        return "cuda", []
    sock = str(power_socket)
    if "," in sock:
        raise ValueError("power socket path must not contain commas")
    return "pmu-adb", ["-global", "via-pmu.battery=on",
                       "-chardev", f"socket,id=power0,path={sock},reconnect-ms=1000",
                       "-global", "via-pmu.chardev=power0"]


def pmu_in_argv(argv: list[str]) -> bool:
    """A running session's argv (argv.json) is the PMU machine with the battery."""
    return any("via=pmu-adb" in a for a in argv) and "via-pmu.battery=on" in argv


# -- the bridge --------------------------------------------------------------------------
class Battery:
    """hostd integration object (``app.battery``)."""

    POLL_S = 5.0
    WAKE_WAIT_S = 5.0
    WAKE_SETTLE_S = 8.0     # Mac OS redraws and re-initialises its devices after a wake

    def __init__(self, app: "Hostd"):
        self.app = app
        self.server: asyncio.AbstractServer | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.connections = 0
        self.hello: dict | None = None
        self.asleep = False
        self.sleeps = 0
        self.last_sent: dict | None = None
        self.session_pmu: str | None = None     # supervisor session launched with the battery
        self._woke = asyncio.Event()
        self._task: asyncio.Task | None = None

    # -- policy ------------------------------------------------------------------------
    @property
    def socket_path(self) -> Path:
        return self.app.paths.run / SOCKET_NAME

    def controller(self) -> str:
        c = getattr(self.app.cfg, "power_controller", "auto")
        return c if c in CONTROLLERS else "auto"

    def capable(self) -> bool:
        """Capability ``battery``: a host battery and a package that can show it."""
        return package_capable(self.app.paths.qemu_package) and host_has_battery(self.app.laptop.power)

    def launch_socket(self, settings: dict, boot_cd: bool = False) -> Path | None:
        """supervisor: the power socket for this launch, or None for the Cuda machine."""
        if boot_cd or self.app.mode != "normal":
            return None
        if use_pmu(settings, capable=package_capable(self.app.paths.qemu_package),
                   has_battery=host_has_battery(self.app.laptop.power), controller=self.controller()):
            return self.socket_path
        return None

    def launched(self, session: str, pmu: bool, adopted: bool = False) -> None:
        """supervisor: a session started (or, adopted, found running after a hostd restart)."""
        self.session_pmu = session if pmu else None
        if not adopted:
            self.asleep = False   # an adopted Mac OS may be asleep: its bridge hello says so
        osx = bool(getattr(getattr(getattr(self.app, "supervisor", None), "machine", None), "osx", False))
        self.app.lifecycle.event("power_machine", session=session, machine="pmu" if osx else
                                 "pmu-battery" if pmu else "cuda",
                                 adopted=adopted)

    def passthrough_active(self) -> bool:
        """Mac OS runs with the battery (so it shows its own battery warnings)."""
        sup = self.app.supervisor
        return bool(self.session_pmu) and self.session_pmu == sup.session and sup.active

    def public(self) -> dict:
        return {"capable": self.capable(), "controller": self.controller(),
                "requested": bool(self.app.settings.applied.get(SETTING, True)),
                "active": self.passthrough_active(), "bridge": self.writer is not None,
                "guest_asleep": self.asleep, "sleeps": self.sleeps, "sent": self.last_sent}

    # -- lifecycle ---------------------------------------------------------------------
    async def start(self) -> None:
        if self.app.mode != "normal":
            return
        path = self.socket_path
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        try:
            self.server = await asyncio.start_unix_server(self._client, path=str(path), limit=LINE_MAX)
        except OSError as exc:
            log.error("power bridge not started: %s", exc)
            self.app.lifecycle.event("power_bridge_failed", error=str(exc)[:200])
            return
        self._socket_permissions(path)
        self._task = asyncio.get_running_loop().create_task(self._poll_loop())

    def _socket_permissions(self, path: Path) -> None:
        # QEMU runs as rbguest: owner root, group rbguest, no access for others.
        user = getattr(self.app.cfg, "run_as_user", None)
        try:
            if user and os.geteuid() == 0:
                import pwd
                os.chown(path, 0, pwd.getpwnam(user).pw_gid)
                os.chmod(path, 0o660)
            elif os.geteuid() != 0:
                os.chmod(path, 0o600)
        except (OSError, KeyError) as exc:
            log.warning("power socket permissions: %s", exc)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self.writer:
            self.writer.close()
        if self.server:
            self.server.close()
            try:
                await asyncio.wait_for(self.server.wait_closed(), 2)
            except asyncio.TimeoutError:
                pass
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass

    async def _poll_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.POLL_S)
                if self.writer is not None:
                    self._send_battery(force=False)
        except asyncio.CancelledError:
            pass

    def _send(self, obj: dict) -> bool:
        w = self.writer
        if w is None or w.is_closing():
            return False
        try:
            w.write(json.dumps(obj, separators=(",", ":")).encode() + b"\n")
        except (ConnectionError, RuntimeError):
            return False
        return True

    def _send_battery(self, force: bool) -> None:
        try:
            # the laptop's estimate: the discharge slope's time to empty when the gauge lies
            power = self.app.laptop.estimated(self.app.laptop.supplies.read())
        except Exception:
            log.exception("power supply read failed")
            return
        msg = guest_battery(power)
        if force or msg != self.last_sent:
            if self._send(msg):
                self.last_sent = msg

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        if self.writer is not None:
            self.writer.close()
        self.writer = writer
        self.hello = None
        self._send({"type": "hello", "version": PROTOCOL_VERSION})
        self._send_battery(force=True)
        try:
            while True:
                try:
                    line = await reader.readline()
                except (asyncio.LimitOverrunError, ValueError, ConnectionError):
                    break
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    await self._message(msg)
        finally:
            if self.writer is writer:
                self.writer = None
            writer.close()

    async def _message(self, msg: dict) -> None:
        t = msg.get("type")
        if t == "hello":
            self.hello = {"version": msg.get("version"), "battery": bool(msg.get("battery")),
                          "backlight": bool(msg.get("backlight"))}
            sup = self.app.supervisor
            if self.hello["battery"] and sup.active and sup.session and self.session_pmu != sup.session:
                # QEMU with the battery says hello for the running session (a hostd
                # restart whose adoption could not read the launch arguments).
                self.session_pmu = sup.session
                self.app.lifecycle.event("power_machine", session=sup.session, machine="pmu-battery",
                                         adopted=True, via="bridge-hello")
            if msg.get("sleeping") and not self.asleep:
                await self._on_sleep()
        elif t == "sleep":
            await self._on_sleep()
        elif t == "wake":
            await self._on_wake(str(msg.get("reason") or "")[:16])
        elif t == "backlight":
            # Mac OS's backlight driver (via-pmu.backlight, keys.py): its level and lamp.
            await self.app.keys.on_backlight(msg)

    # -- sleep and wake ----------------------------------------------------------------
    async def _on_sleep(self) -> None:
        self.asleep = True
        self.sleeps += 1
        self._woke.clear()
        self.app.lifecycle.event("guest_sleep", count=self.sleeps)
        await self.app.presenter.try_request("display_power", output="all", on=False)
        cp = getattr(self.app, "cpuplace", None)
        if cp is not None:
            await cp.guest_sleep(True)       # no performance governor for a halted processor

    async def _on_wake(self, reason: str) -> None:
        was = self.asleep
        self.asleep = False
        self._woke.set()
        self.app.lifecycle.event("guest_wake", reason=reason)
        cp = getattr(self.app, "cpuplace", None)
        if cp is not None:
            await cp.guest_sleep(False)       # the vCPU policy first, then the displays
        if was:
            await self._displays_on()

    async def _displays_on(self) -> None:
        await self.app.presenter.try_request("display_power", output="external", on=True)
        if self.app.laptop.lid_open is not False:
            await self.app.presenter.try_request("display_power", output="internal", on=True)

    async def wake(self, why: str) -> bool:
        """Wake a sleeping Mac OS; True once QEMU reports it awake."""
        if not self.asleep:
            return True
        self.app.lifecycle.event("guest_wake_request", why=why)
        if not self._send({"type": "wake"}):
            return False
        try:
            await asyncio.wait_for(self._woke.wait(), self.WAKE_WAIT_S)
        except asyncio.TimeoutError:
            return False
        return True

    async def ensure_awake(self, why: str) -> None:
        """Before hostd talks to Mac OS (power key): wake it and let it settle."""
        if self.asleep and await self.wake(why):
            await asyncio.sleep(self.WAKE_SETTLE_S)
