"""The name this computer actually has on the local network (mDNS), and a unique default.

avahi-daemon publishes ``<hostname>.local``.  When another device already uses that
name (several RetroBoot computers on one network all start as ``retroboot``), avahi
publishes ``retroboot-2.local`` instead, and which computer gets which name depends
on the order they started.  RetroBoot used to show ``retroboot.local`` regardless,
which pointed people at another computer.

``MdnsWatch`` reads the published name from avahi (D-Bus, through ``busctl``) so the
panel, the web pages and the status show the real one.  When avahi had to rename
this computer and its name is still the factory default, RetroBoot gives it a
stable unique name, ``retroboot-<last four hex digits of its network hardware
address>``, stores it in ``management.hostname`` and tells avahi.  A name the user
chose is never changed; ``retroboot.local`` stays whenever it is free.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger("retroboot.mdns")

AVAHI = ["org.freedesktop.Avahi", "/", "org.freedesktop.Avahi.Server"]
DEFAULT_NAME = "retroboot"

Runner = Callable[..., Awaitable[tuple[int, str, str]]]


async def published_name(run: Runner) -> str | None:
    """avahi's current host name FQDN (e.g. ``retroboot-2.local``), or None without avahi."""
    rc, out, _ = await run(["busctl", "--system", "--json=short", "call", *AVAHI, "GetHostNameFqdn"], timeout=5)
    if rc != 0:
        return None
    try:
        data = json.loads(out)
        name = data["data"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    return name if isinstance(name, str) and name.endswith(".local") and len(name) <= 255 else None


async def tell_avahi(run: Runner, name: str) -> bool:
    rc, _, err = await run(["busctl", "--system", "call", *AVAHI, "SetHostName", "s", name], timeout=5)
    if rc != 0:
        log.info("avahi SetHostName %s: %s", name, err.strip()[:200])
    return rc == 0


def hardware_suffix(sysfs: Path = Path("/sys")) -> str | None:
    """Last four hex digits of a stable network hardware address: the first wired
    interface with a device behind it, else the first wireless one (sorted by name)."""
    base = Path(sysfs) / "class" / "net"
    wired, wireless = [], []
    try:
        names = sorted(p.name for p in base.iterdir())
    except OSError:
        return None
    for name in names:
        d = base / name
        if not (d / "device").exists():
            continue                     # lo, bridges, tunnels, virtual devices
        try:
            if (d / "type").read_text().strip() != "1":
                continue                 # not Ethernet-framed
            addr = (d / "address").read_text().strip().lower()
        except OSError:
            continue
        hexes = addr.replace(":", "")
        if len(hexes) != 12 or hexes == "0" * 12:
            continue
        (wireless if (d / "wireless").exists() or (d / "phy80211").exists() else wired).append(hexes)
    pick = (wired or wireless or [None])[0]
    return pick[-4:] if pick else None


def unique_default(suffix: str | None) -> str | None:
    return f"{DEFAULT_NAME}-{suffix}" if suffix else None


class MdnsWatch:
    """Keeps ``name`` (the published FQDN) current and resolves default-name conflicts once."""

    def __init__(self, app, run: Runner, interval: float = 30.0, sysfs: Path = Path("/sys")):
        self.app = app
        self.run = run
        self.interval = interval
        self.sysfs = sysfs
        self.name: str | None = None
        self._task: asyncio.Task | None = None

    def display_name(self, hostname: str) -> str:
        """What to show people: avahi's published name, else ``<hostname>.local``."""
        return self.name or f"{hostname}.local"

    async def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        await asyncio.sleep(5)           # avahi probes for a few seconds after start
        while True:
            try:
                await self.check()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a status helper must never stop hostd
                log.exception("mDNS check failed")
            await asyncio.sleep(self.interval)

    async def check(self) -> str | None:
        name = await published_name(self.run)
        if name != self.name:
            if name:
                log.info("published on the local network as %s", name)
            self.name = name
        if not name:
            return None
        hostname = self.app.settings.applied.get("management.hostname")
        if hostname != DEFAULT_NAME or name == f"{DEFAULT_NAME}.local":
            return name                  # the user's own name, or the default is free: nothing to do
        new = unique_default(hardware_suffix(self.sysfs))
        if not new or new == hostname:
            return name
        try:
            self.app.settings.set_desired_and_applied({"management.hostname": new}, self.app.validation_context())
        except Exception as e:  # noqa: BLE001
            log.warning("could not store the unique name %s: %s", new, e)
            return name
        await self.app.network.set_hostname(new)
        await tell_avahi(self.run, new)
        self.app.lifecycle.event("hostname_unique", old=hostname, published=name, new=new)
        log.info("%s.local is used by another device; this computer is now %s.local", DEFAULT_NAME, new)
        self.name = await published_name(self.run) or f"{new}.local"
        return self.name
