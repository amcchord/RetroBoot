"""NetworkManager backend (nmcli) for the AirPort bridge.

Read-only queries run without the adapter lock (nmcli is safe to call
concurrently); everything that changes NetworkManager — radio, activation,
new profiles — runs under ``NetworkAdapter.lock``, the one lock the panel and
the web API also use, so AirPort requests and settings changes are serialized
by the same host authority.  New profiles are written by
``NetworkAdapter.connect`` as 0600 keyfiles; secrets never travel in argv.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

from ..network import NetworkAdapter, NetworkError, _nm_split
from .backends import Backend, HostLink, JoinError
from .names import ENTERPRISE, OPEN, OWE, PSK, SAE, WEP, HostNetwork, security_class

log = logging.getLogger("retroboot.airport")

LIST_FIELDS = "IN-USE,SSID-HEX,BSSID,CHAN,SIGNAL,SECURITY"
# A profile's 802-11-wireless-security.key-mgmt as the security class its scan shows.
KEY_MGMT_CLASS = {"": OPEN, "wpa-psk": PSK, "sae": SAE, "owe": OWE, "none": WEP, "ieee8021x": WEP,
                  "wpa-eap": ENTERPRISE, "wpa-eap-suite-b-192": ENTERPRISE}
PROFILE_TTL = 10.0


def signal_dbm(percent: int) -> int:
    """NetworkManager reports 0-100 (2 x (dBm + 100), clamped); invert it."""
    return int(max(0, min(100, percent)) / 2 - 100)


def parse_wifi_list(out: str) -> list[dict]:
    rows = []
    for line in out.splitlines():
        p = _nm_split(line)
        if len(p) < 6:
            continue
        try:
            ssid = bytes.fromhex(p[1]) if p[1] not in ("", "--") else b""
            chan = int(p[3]) if p[3].isdigit() else 0
            sig = int(p[4]) if p[4].isdigit() else 0
        except ValueError:
            continue
        rows.append({"in_use": p[0].strip() == "*", "ssid": ssid, "bssid": p[2].lower(), "channel": chan,
                     "signal": sig, "security": security_class(p[5] if p[5] != "--" else "")})
    return rows


class NetworkManagerBackend(Backend):
    name = "networkmanager"
    poll_interval = 2.0

    def __init__(self, adapter: NetworkAdapter):
        super().__init__()
        self.adapter = adapter
        self.run = adapter.run
        self._profiles: tuple[float, dict[bytes, dict]] | None = None
        self._dev: str | None = None

    async def device(self) -> str | None:
        if self._dev is None:
            self._dev = await self.adapter.wifi_device()
        return self._dev

    async def radio(self) -> bool:
        rc, out, _ = await self.run(["nmcli", "-t", "radio", "wifi"], timeout=5)
        return rc == 0 and out.strip() == "enabled"

    async def radio_on(self) -> bool:
        async with self.adapter.lock:
            rc, _, err = await self.run(["nmcli", "radio", "wifi", "on"], timeout=10)
        if rc != 0:
            log.warning("could not turn the Wi-Fi radio on: %s", err.strip())
            return False
        for _ in range(10):                      # the device needs a moment to come back
            await asyncio.sleep(0.5)
            if await self.radio():
                return True
        return False

    async def profiles(self, refresh: bool = False) -> dict[bytes, dict]:
        """Wi-Fi profiles by SSID bytes: {"uuid", "name", "key_mgmt"}."""
        now = time.monotonic()
        if not refresh and self._profiles and now - self._profiles[0] < PROFILE_TTL:
            return self._profiles[1]
        rc, out, _ = await self.run(["nmcli", "-t", "-f", "NAME,UUID,TYPE", "connection", "show"], timeout=10)
        found: dict[bytes, dict] = {}
        for line in out.splitlines() if rc == 0 else []:
            p = _nm_split(line)
            if len(p) < 3 or p[2] not in ("802-11-wireless", "wifi"):
                continue
            rc2, out2, _ = await self.run(["nmcli", "-t", "-g", "802-11-wireless.ssid,802-11-wireless-security.key-mgmt",
                                           "connection", "show", "uuid", p[1]], timeout=10)
            if rc2 != 0:
                continue
            vals = out2.splitlines() + ["", ""]
            ssid = vals[0].encode("utf-8")
            if ssid:
                found.setdefault(ssid, {"uuid": p[1], "name": p[0], "key_mgmt": vals[1].strip()})
        self._profiles = (now, found)
        return found

    async def recent_profile(self) -> HostNetwork | None:
        """The saved Wi-Fi profile NetworkManager used last (the network it will most likely
        connect to on its own), or None.  For AirPort's startup network when the host is not
        connected yet and hostd has never seen it connected (a first boot)."""
        rc, out, _ = await self.run(["nmcli", "-t", "-f", "NAME,UUID,TYPE,TIMESTAMP", "connection", "show"],
                                    timeout=10)
        rows = []
        for line in out.splitlines() if rc == 0 else []:
            p = _nm_split(line)
            if len(p) >= 4 and p[2] in ("802-11-wireless", "wifi"):
                rows.append((int(p[3]) if p[3].isdigit() else 0, p[1]))
        for _ts, uuid in sorted(rows, reverse=True):
            rc2, out2, _ = await self.run(["nmcli", "-t", "-g", "802-11-wireless.ssid,802-11-wireless-security.key-mgmt",
                                           "connection", "show", "uuid", uuid], timeout=10)
            vals = out2.splitlines() + ["", ""] if rc2 == 0 else ["", ""]
            ssid = vals[0].encode("utf-8")
            if ssid:
                return HostNetwork(ssid, "", 0, 0, KEY_MGMT_CLASS.get(vals[1].strip(), PSK), known=True)
        return None

    async def _list(self, fresh: bool) -> list[dict]:
        dev = await self.device()
        if not dev:
            return []
        rc, out, _ = await self.run(["nmcli", "-t", "-f", LIST_FIELDS, "device", "wifi", "list", "ifname", dev,
                                     "--rescan", "yes" if fresh else "no"], timeout=30 if fresh else 10)
        return parse_wifi_list(out) if rc == 0 else []

    async def scan(self, fresh: bool = False) -> list[HostNetwork]:
        rows = await self._list(fresh)
        profiles = await self.profiles()
        return [HostNetwork(r["ssid"], r["bssid"], r["channel"], signal_dbm(r["signal"]), r["security"],
                            known=r["ssid"] in profiles or r["security"] in (OPEN, OWE), active=r["in_use"])
                for r in rows]

    async def link(self) -> HostLink:
        dev = await self.device()
        if not dev:
            return HostLink(False, None)
        radio = await self.radio()
        if not radio:
            return HostLink(False, None)
        rc, out, _ = await self.run(["nmcli", "-t", "-f", "GENERAL.STATE", "device", "show", dev], timeout=5)
        state = out.split(":", 1)[1] if rc == 0 and ":" in out else ""
        m = re.match(r"\s*(\d+)", state)
        if not m or int(m.group(1)) != 100:          # NM_DEVICE_STATE_ACTIVATED
            return HostLink(True, None)
        active = [r for r in await self._list(False) if r["in_use"]]
        if not active:
            return HostLink(True, None)
        r = active[0]
        return HostLink(True, HostNetwork(r["ssid"], r["bssid"], r["channel"], signal_dbm(r["signal"]),
                                          r["security"], known=True, active=True))

    async def join(self, net: HostNetwork, secret: str | None = None, hidden: bool = False) -> None:
        dev = await self.device()
        if not dev:
            raise JoinError("This computer has no Wi-Fi.", "failed")
        profile = (await self.profiles(refresh=True)).get(net.ssid)
        if profile is not None and secret is None:
            async with self.adapter.lock:
                args = ["nmcli", "--wait", "45", "connection", "up", "uuid", profile["uuid"], "ifname", dev]
                rc, _, err = await self.run(args, timeout=60)
            if rc == 0:
                return
            if "Secrets were required" in err or "secrets" in err.lower():
                # The saved password no longer works: ask again (a new profile replaces it).
                raise JoinError("The saved password was not accepted.", "needs-secret")
            raise JoinError("Could not join the network.", "not-found" if "No network with SSID" in err else "failed")
        if net.security == ENTERPRISE or (net.security == WEP and profile is None):
            raise JoinError("RetroBoot cannot join this kind of network.", "failed")
        needs = hidden or net.security not in (OPEN, OWE)
        if needs and secret is None:
            raise JoinError("A password is required.", "needs-secret")
        try:
            ssid = net.ssid.decode("utf-8")
        except UnicodeDecodeError:
            raise JoinError("RetroBoot cannot save a profile for this network name.", "failed")
        psk = secret or None
        key_mgmt = "sae" if net.security == SAE else "wpa-psk"
        try:
            await self.adapter.connect(ssid, psk, hidden=hidden, key_mgmt=key_mgmt)
        except NetworkError as exc:
            self._profiles = None
            # A WPA2/WPA3 (SAE) network that rejects the password often fails as
            # "network could not be found" (NetworkManager reason ssid-not-found;
            # seen on the Intel AC 7265 and AX200).  The network was just in
            # range (this join comes from a scan), so with a password that reads
            # as a wrong password and the window asks again.
            if psk and ("password" in str(exc).lower() or (not hidden and net.bssid)):
                raise JoinError("The password was not accepted.", "wrong-password")
            raise JoinError(str(exc), "not-found" if hidden else "failed")
        self._profiles = None
        if profile is not None and profile.get("uuid"):
            # The replaced profile (old password) would compete with the new one.
            async with self.adapter.lock:
                await self.run(["nmcli", "connection", "delete", "uuid", profile["uuid"]], timeout=15)

    def describe(self) -> dict:
        return {"backend": self.name, "device": self._dev}
