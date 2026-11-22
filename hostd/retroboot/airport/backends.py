"""Host Wi-Fi backends for the AirPort bridge.

A backend answers four questions for the bridge service: which access points
are visible, what the host is connected to (and whether its radio is on),
whether a network needs a password RetroBoot does not have, and how to join
one.  The service owns translation, prompting and the guest protocol.

* ``VirtualBackend`` — VMs and machines without Wi-Fi: one open network
  (default name "RetroBoot") that stands for the host's wired uplink.  It is
  in range exactly while the uplink has carrier and a default route, so a
  real link change on the host reaches the guest.
* ``FixtureBackend`` — deterministic lab networks from a JSON file that tests
  edit while the guest runs (signal, range, radio, passwords).  Labelled
  simulated in every report.
* ``NetworkManagerBackend`` (``nm.py``) — the real adapter.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .names import OPEN, HostNetwork, security_class

log = logging.getLogger("retroboot.airport")


@dataclass
class HostLink:
    radio: bool = True
    connected: HostNetwork | None = None


class JoinError(Exception):
    """A join the host could not complete; ``code`` is wrong-password | not-found | failed."""

    def __init__(self, message: str, code: str = "failed"):
        super().__init__(message)
        self.code = code


class Backend:
    name = "none"
    poll_interval = 2.0

    def __init__(self) -> None:
        self.on_change: Callable[[], None] | None = None

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def scan(self, fresh: bool = False) -> list[HostNetwork]:
        return []

    async def link(self) -> HostLink:
        return HostLink()

    async def join(self, net: HostNetwork, secret: str | None = None, hidden: bool = False) -> None:
        raise JoinError("No Wi-Fi.", "failed")

    def describe(self) -> dict:
        return {"backend": self.name}


# --- virtual: the wired uplink as one network -------------------------------

def default_route_iface(proc_route: Path = Path("/proc/net/route")) -> str | None:
    try:
        for line in proc_route.read_text().splitlines()[1:]:
            f = line.split()
            if len(f) >= 4 and f[1] == "00000000" and int(f[3], 16) & 0x2:   # RTF_GATEWAY
                return f[0]
    except (OSError, ValueError):
        pass
    return None


def iface_up(name: str, sysfs: Path = Path("/sys/class/net")) -> bool:
    try:
        oper = (sysfs / name / "operstate").read_text().strip()
        carrier = (sysfs / name / "carrier").read_text().strip()
    except OSError:
        return False
    return carrier == "1" and oper in ("up", "unknown")


class VirtualBackend(Backend):
    name = "virtual"
    poll_interval = 1.0

    def __init__(self, ssid: str = "RetroBoot", uplink: str | None = None,
                 bssid: str = "02:52:42:4e:45:54", channel: int = 6, signal: int = -42,
                 proc_route: Path = Path("/proc/net/route"), sysfs: Path = Path("/sys/class/net")):
        super().__init__()
        self.ssid = ssid.encode("utf-8")[:32]
        self.uplink = uplink
        self.bssid, self.channel, self.signal = bssid, channel, signal
        self.proc_route, self.sysfs = proc_route, sysfs

    def uplink_state(self) -> tuple[str | None, bool]:
        if self.uplink:
            # A named uplink stands alone (lab: a dummy interface the test toggles).
            return self.uplink, iface_up(self.uplink, self.sysfs)
        dev = default_route_iface(self.proc_route)
        return dev, bool(dev) and iface_up(dev, self.sysfs)

    def _network(self, active: bool) -> HostNetwork:
        return HostNetwork(self.ssid, self.bssid, self.channel, self.signal, OPEN, known=True, active=active)

    def network(self) -> HostNetwork:
        """The one network this backend offers (in range or not)."""
        return self._network(True)

    async def scan(self, fresh: bool = False) -> list[HostNetwork]:
        _, up = self.uplink_state()
        return [self._network(True)] if up else []

    async def link(self) -> HostLink:
        _, up = self.uplink_state()
        return HostLink(True, self._network(True) if up else None)

    async def join(self, net: HostNetwork, secret: str | None = None, hidden: bool = False) -> None:
        _, up = self.uplink_state()
        if net.ssid != self.ssid:
            raise JoinError("This computer has no Wi-Fi; only the RetroBoot network is available.", "not-found")
        if not up:
            raise JoinError("The computer's network connection is down.", "not-found")

    def describe(self) -> dict:
        dev, up = self.uplink_state()
        return {"backend": self.name, "ssid": self.ssid.decode("utf-8", "replace"), "uplink": dev, "uplink_up": up}


# --- fixture: deterministic lab networks ------------------------------------

class FixtureBackend(Backend):
    """Networks from a JSON file, re-read when it changes::

        {"radio": true,
         "networks": [{"ssid": "Home", "bssid": "02:..", "channel": 6, "signal": -50,
                       "security": "WPA2", "password": "...", "known": false}]}

    ``ssid`` may be ``ssid_hex`` for arbitrary bytes; ``"hidden": true``
    keeps the SSID out of scans (join it by name).  A join of a secured
    network succeeds with its ``password`` (or when ``known``); successful
    joins mark the network known for the rest of the session, like a saved
    host profile.  ``"connected": true`` on one network makes the host start
    out connected to it (as NetworkManager's autoconnect would), until a join
    or ``disconnect()``.  Simulated: no radio is involved.
    """
    name = "fixture"
    poll_interval = 1.0

    def __init__(self, path: Path):
        super().__init__()
        self.path = Path(path)
        self._stamp: tuple | None = None
        self._data: dict = {"radio": True, "networks": []}
        self.known: set[str] = set()
        self.connected_id: str | None = None
        self._initial_done = False                      # "connected": true applied once
        self.joins: list[dict] = []                     # audit for tests (no secrets)

    def _load(self) -> dict:
        # Writers replace the file (new inode).  The mtime alone misses a
        # replacement within one tick of the kernel's coarse file clock.
        try:
            with self.path.open("rb") as f:
                st = os.fstat(f.fileno())
                stamp = (st.st_ino, st.st_size, st.st_mtime_ns)
                if stamp != self._stamp:
                    self._data = json.loads(f.read())
                    self._stamp = stamp
        except (OSError, ValueError) as exc:
            log.warning("airport fixture unreadable: %s", exc)
        return self._data

    def _aps(self) -> list[tuple[HostNetwork, dict]]:
        d = self._load()
        if not self._initial_done:
            self._initial_done = True
            for n in d.get("networks", []):
                if n.get("connected") and self.connected_id is None:
                    try:
                        ssid = bytes.fromhex(n["ssid_hex"]) if "ssid_hex" in n else n["ssid"].encode("utf-8")
                        self.connected_id = HostNetwork(ssid[:32], "", 0, 0, security_class(n.get("security"))).id
                    except (KeyError, ValueError, TypeError, AttributeError):
                        pass
        if not d.get("radio", True):
            return []
        out = []
        for n in d.get("networks", []):
            try:
                ssid = bytes.fromhex(n["ssid_hex"]) if "ssid_hex" in n else n["ssid"].encode("utf-8")
                sec = security_class(n.get("security"))
                net = HostNetwork(ssid[:32], n["bssid"].lower(), int(n.get("channel", 6)), int(n.get("signal", -50)),
                                  sec, known=bool(n.get("known")))
            except (KeyError, ValueError, TypeError):
                continue
            net.known = net.known or net.id in self.known or sec == OPEN
            net.active = net.id == self.connected_id
            out.append((net, n))
        return out

    async def scan(self, fresh: bool = False) -> list[HostNetwork]:
        out = []
        for n, raw in self._aps():
            if raw.get("hidden"):
                n = HostNetwork(b"", n.bssid, n.channel, n.signal, n.security, n.known, n.active)
            out.append(n)
        return out

    async def link(self) -> HostLink:
        radio = bool(self._load().get("radio", True))
        best = None
        for net, _ in self._aps():
            if net.id == self.connected_id and (best is None or net.signal > best.signal):
                best = net
        return HostLink(radio, best)

    async def join(self, net: HostNetwork, secret: str | None = None, hidden: bool = False) -> None:
        await asyncio.sleep(0.2)
        match = [(n, raw) for n, raw in self._aps() if n.ssid == net.ssid and (hidden or n.id == net.id)]
        self.joins.append({"ssid": net.ssid.hex(), "hidden": hidden, "with_secret": secret is not None})
        if not match:
            raise JoinError(f"“{net.ssid.decode('utf-8', 'replace')}” was not found.", "not-found")
        n, raw = max(match, key=lambda m: m[0].signal)
        if n.security != OPEN and not n.known:
            if secret is None:
                raise JoinError("A password is required.", "needs-secret")
            if secret != raw.get("password"):
                raise JoinError("The password was not accepted.", "wrong-password")
            self.known.add(n.id)
        self.connected_id = n.id

    async def disconnect(self) -> None:
        self.connected_id = None

    def describe(self) -> dict:
        return {"backend": self.name, "fixture": str(self.path), "simulated": True}
