"""Native AirPort: hostd's side of the emulated AirPort card (PRODUCT §11).

The card (QEMU ``retroboot-airport``) connects to ``/run/retroboot/airport.sock``
and speaks the bridge protocol in ARCHITECTURE §10.  This package owns that
socket, translates host networks for the original AirPort software
(``names.py``), joins through a backend (NetworkManager on hardware with
Wi-Fi, the virtual "RetroBoot" network elsewhere, or a lab fixture), asks for
missing passwords with RetroBoot's own panel (``panel.py``) and keeps the
guest's TCP/IP configuration matched to the chosen adapter (guest prep).

Integration points in the rest of hostd are deliberately small:

* ``schema.py`` — setting ``network.guest_adapter`` (``ethernet`` | ``airport``,
  capability ``airport``).
* ``hostd.py`` — ``AirPort(app)``; ``start()``/``stop()``; ``capable()`` in
  ``capabilities()``.
* ``machines.build_argv`` — ``guest_nic_args()`` replaces the fixed Sungem NIC.
* ``supervisor.prepare_disk`` — ``await app.airport.prepare_launch(machine)``:
  TCP/IP for the adapter, and AirPort's startup network (the host's).
* ``guestprep.GuestPrep`` — ``options_provider`` supplies ``network_port``.
"""
from __future__ import annotations

import asyncio
import grp
import json
import logging
import pwd
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .backends import Backend, FixtureBackend, VirtualBackend
from .names import NameTable
from .service import AirPortService

if TYPE_CHECKING:
    from ..hostd import Hostd

log = logging.getLogger("retroboot.airport")

FEATURE = "airport-bridge-1"
ETHERNET, AIRPORT = "ethernet", "airport"
SOCKET_NAME = "airport.sock"


def package_capable(package_root: Path) -> bool:
    """The emulator package has the card, its bridge and the gated radio node."""
    try:
        m = json.loads((Path(package_root) / "manifest.json").read_text())
    except (OSError, ValueError):
        return False
    return FEATURE in (m.get("features") or [])


def requested_port(settings: dict, package_root: Path) -> str:
    """The adapter the user asked for, if this package can provide it."""
    if settings.get("network.guest_adapter") == AIRPORT and package_capable(package_root):
        return AIRPORT
    return ETHERNET


def prep_report(machine) -> dict:
    prep = (machine.meta.get("prep") or {}) if machine is not None else {}
    return (prep.get("report") or {}) if prep.get("result") == "applied" else {}


def effective_adapter(settings: dict, machine, package_root: Path) -> str:
    """What the guest gets at the next launch: "none", "airport" or "ethernet".

    AirPort needs the setting, a capable package and a guest that has the
    AirPort software (as last seen by guest prep).  Without it the guest keeps
    Ethernet, so Mac OS never boots without a working network adapter.
    """
    if settings.get("network.guest", "nat") != "nat":
        return "none"
    if getattr(machine, "osx", False):
        return ETHERNET         # OS X AirPort drivers have a separate, unqualified contract
    if requested_port(settings, package_root) != AIRPORT:
        return ETHERNET
    if prep_report(machine).get("airport_software") == "absent":
        return ETHERNET
    return AIRPORT


def guest_nic_args(settings: dict, paths, machine) -> list[str]:
    """QEMU network arguments for build_argv (replaces the fixed Sungem NIC)."""
    adapter = effective_adapter(settings, machine, paths.qemu_package)
    if adapter == "none":
        return ["-nic", "none"]
    if adapter == ETHERNET:
        return ["-nic", "user,model=sungem"]
    sock = Path(paths.run) / SOCKET_NAME
    if "," in str(sock):
        raise ValueError("AirPort socket path must not contain commas")
    return ["-nic", "none", "-netdev", "user,id=apnet",
            "-global", "macio-newworld.airport=true", "-global", "retroboot-airport.netdev=apnet",
            "-chardev", f"socket,id=airport0,path={sock},reconnect-ms=1000",
            "-global", "retroboot-airport.chardev=airport0"]


def prep_options(settings: dict, package_root: Path) -> dict:
    """Keyword arguments for rbguestprep.prep.apply()."""
    return {"network_port": requested_port(settings, package_root)}


def make_backend(kind: str, *, wifi: bool, adapter=None, fixture: str | None = None,
                 ssid: str = "RetroBoot", uplink: str | None = None) -> Backend:
    if kind == "auto":
        kind = "networkmanager" if wifi and adapter is not None else "virtual"
    if kind == "networkmanager":
        from .nm import NetworkManagerBackend
        return NetworkManagerBackend(adapter)
    if kind == "fixture":
        if not fixture:
            raise ValueError("airport_backend=fixture needs airport_fixture")
        return FixtureBackend(Path(fixture))
    if kind == "virtual":
        return VirtualBackend(ssid=ssid, uplink=uplink)
    raise ValueError(f"unknown AirPort backend {kind!r}")


def _guest_gid(user: str | None) -> int | None:
    if not user:
        return None
    try:
        return pwd.getpwnam(user).pw_gid
    except KeyError:
        try:
            return grp.getgrnam(user).gr_gid
        except KeyError:
            return None


class AirPort:
    """hostd integration object (``app.airport``)."""

    def __init__(self, app: "Hostd"):
        self.app = app
        self.service: AirPortService | None = None
        self.error: str | None = None

    @property
    def socket_path(self) -> Path:
        return self.app.paths.run / SOCKET_NAME

    def capable(self) -> bool:
        return package_capable(self.app.paths.qemu_package)

    async def start(self) -> None:
        cfg = self.app.cfg
        if cfg.mode != "normal" or getattr(cfg, "airport_backend", "auto") == "off":
            return
        try:
            backend = make_backend(getattr(cfg, "airport_backend", "auto"), wifi=bool(self.app._wifi_available),
                                   adapter=self.app.network if cfg.allow_network_tools else None,
                                   fixture=getattr(cfg, "airport_fixture", None),
                                   ssid=getattr(cfg, "airport_virtual_ssid", "RetroBoot"),
                                   uplink=getattr(cfg, "airport_uplink", None))
            from .panel import PanelPrompter, mac_os_starting
            self.service = AirPortService(
                backend, self.socket_path, NameTable.load(self.app.paths.state / "airport-names.json"),
                PanelPrompter(self.app), event=self.app.lifecycle.event,
                group_gid=_guest_gid(cfg.run_as_user), redact=self.app.redactor.add,
                startup=lambda: mac_os_starting(self.app))
            await self.service.start()
        except Exception as exc:
            self.error = str(exc)
            self.service = None
            log.exception("AirPort bridge not started")
            self.app.lifecycle.event("airport_bridge_failed", error=str(exc)[:200])

    async def stop(self) -> None:
        if self.service:
            await self.service.stop()

    async def wifi_appeared(self) -> None:
        """hostd found Wi-Fi after the bridge started: a bridge that chose the RetroBoot
        network because there was no Wi-Fi yet restarts on NetworkManager. The card
        reconnects to the new bridge by itself (QEMU's chardev reconnect)."""
        cfg = self.app.cfg
        if (getattr(cfg, "airport_backend", "auto") != "auto" or not cfg.allow_network_tools
                or self.service is None or self.service.backend.name != "virtual"):
            return
        await self.service.stop()
        self.service = None
        await self.start()
        self.app.lifecycle.event("airport_backend_changed",
                                 backend=self.service.backend.name if self.service else None)

    def public(self) -> dict[str, Any]:
        settings = self.app.settings.applied
        m = self.app.supervisor.active_machine()
        out = {"capable": self.capable(), "requested": settings.get("network.guest_adapter", ETHERNET),
               "effective": effective_adapter(settings, m, self.app.paths.qemu_package),
               "guest_software": prep_report(m).get("airport_software"),
               "tcpip": prep_report(m).get("tcpip_port"), "error": self.error}
        if self.service:
            out.update(self.service.public())
        return out

    async def prepare_launch(self, machine) -> None:
        """supervisor.prepare_disk hook: before a launch (guest stopped), re-run guest prep
        when the guest's TCP/IP was prepared for another adapter, or when AirPort was
        requested for a guest that lacked the software and now has it; then, with the
        AirPort card, name the host's network as AirPort's startup network."""
        if machine is None or getattr(machine, "osx", False):
            return
        try:
            await self._prepare_tcpip(machine)
        finally:
            await self.seed_startup_network(machine)

    async def seed_startup_network(self, machine) -> None:
        """AirPort 2.0.4 joins the network in its preferences when Mac OS starts and leaves
        the card off when there is none (a freshly imported base has no AirPort Prefs).
        Before each launch with the card, make that the network the host is on, so Mac OS
        comes up joined (rbguestprep.airport; AirPort's own on/off choice is kept).  Never
        blocks a launch."""
        if self.service is None or effective_adapter(self.app.settings.applied, machine,
                                                     self.app.paths.qemu_package) != AIRPORT:
            return
        if not hasattr(self.app.guestprep, "airport_network"):
            return
        try:
            want = await self.service.startup_network()
        except Exception as exc:
            log.warning("AirPort startup network unknown: %s", exc)
            want = None
        if want is None:
            self.app.lifecycle.event("airport_startup_network", machine=machine.id, result="no-host-network")
            return
        res = await self.app.guestprep.airport_network(machine.disk, want["name"])
        # ("source" is the lifecycle record's own field: hostd)
        self.app.lifecycle.event("airport_startup_network", machine=machine.id, network=want["network"],
                                 basis=want["source"], **res)

    async def _prepare_tcpip(self, machine) -> None:
        prep = machine.meta.get("prep") or {}
        if prep.get("result") != "applied":
            return                                   # supervisor's first-time prep uses prep_options()
        want = requested_port(self.app.settings.applied, self.app.paths.qemu_package)
        report = prep.get("report") or {}
        have = report.get("network_port", ETHERNET)
        if have == want and not (want == AIRPORT and report.get("airport_software") == "absent"):
            return
        if have == want:
            present = await asyncio.to_thread(self.app.guestprep.airport_software_present, machine.disk) \
                if hasattr(self.app.guestprep, "airport_software_present") else None
            if not present:
                return
        state = machine.meta.get("airport_prep") or {}
        if state.get("want") == want and int(state.get("failures", 0)) >= 2:
            return
        self.app.lifecycle.event("airport_prep", machine=machine.id, want=want, have=have)
        res = await self.app.supervisor.run_prep(machine, "network-adapter")
        failures = 0 if res.get("result") == "applied" else int(state.get("failures", 0)) + 1
        machine.update_meta(airport_prep={"want": want, "failures": failures})
