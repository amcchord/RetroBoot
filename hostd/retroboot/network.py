"""Host network state and Wi-Fi provisioning through NetworkManager (nmcli).

The adapter is the single authority for host connection state used by the
local panel and the web API (and later by the AirPort host bridge).  All
operations are serialized by one lock.  Wi-Fi secrets never travel in argv:
profiles are written as NetworkManager keyfiles (0600) and loaded with
``nmcli connection load``.  Without Wi-Fi hardware (VMs) every Wi-Fi call
reports ``available: false`` instead of failing.

Keyfiles live on the data partition, ``/var/lib/retroboot/network/connections``
(NetworkManager's keyfile ``path``, set by the image's
``/etc/NetworkManager/conf.d/50-retroboot-keyfile.conf``), so host network
profiles survive A/B root updates and the installer can carry a network joined
during installation to the installed system.  Profiles from older releases in
``/etc/NetworkManager/system-connections`` are moved there once at startup
(:meth:`NetworkAdapter.migrate_legacy_profiles`).
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import socket
import uuid
from pathlib import Path
from typing import Awaitable, Callable

from .util import run as run_cmd

log = logging.getLogger("retroboot.network")

Runner = Callable[..., Awaitable[tuple[int, str, str]]]

SSID_MAX = 32
NM_CONNECTIONS = Path("/var/lib/retroboot/network/connections")
# cfg80211 registers every Wi-Fi radio here as soon as its driver binds, before
# NetworkManager lists the device (hostboot: hostd now starts ahead of NetworkManager).
IEEE80211 = Path("/sys/class/ieee80211")
LEGACY_NM_CONNECTIONS = Path("/etc/NetworkManager/system-connections")


class NetworkError(Exception):
    def __init__(self, message: str, code: str = "network"):
        super().__init__(message)
        self.code = code


def _nm_split(line: str) -> list[str]:
    """Split nmcli terse output (':' separated, '\\:' escaped)."""
    out, cur, esc = [], [], False
    for ch in line:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


def keyfile_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")


class NetworkAdapter:
    def __init__(self, enabled: bool = True, runner: Runner | None = None,
                 nm_connections_dir: Path = NM_CONNECTIONS,
                 hostname_cmd: list[str] | None = None,
                 ieee80211_dir: Path = IEEE80211):
        self.enabled = enabled
        self.ieee80211_dir = Path(ieee80211_dir)
        self.run = runner or run_cmd
        self.lock = asyncio.Lock()
        self.nm_dir = Path(nm_connections_dir)
        self.hostname_cmd = hostname_cmd if hostname_cmd is not None else ["hostnamectl", "set-hostname"]
        self.last_scan: list[dict] = []
        self.scanned_at: str | None = None
        self._wifi_cache: tuple[float, bool, str | None] | None = None

    # -- addresses ---------------------------------------------------------
    async def interfaces(self) -> list[dict]:
        if not self.enabled:
            return []
        rc, out, _ = await self.run(["ip", "-j", "addr", "show"], timeout=5)
        if rc != 0:
            return self._fallback_interfaces()
        try:
            data = json.loads(out or "[]")
        except ValueError:
            return []
        default_ifaces = await self._default_route_ifaces()
        result = []
        for it in data:
            name = it.get("ifname", "")
            if name == "lo" or it.get("link_type") == "loopback":
                continue
            addrs = []
            for a in it.get("addr_info", []):
                fam = a.get("family")
                local = a.get("local")
                if not local or fam not in ("inet", "inet6"):
                    continue
                if fam == "inet6" and a.get("scope") != "global":
                    continue
                addrs.append({"family": "ipv4" if fam == "inet" else "ipv6", "address": local,
                              "prefix": a.get("prefixlen")})
            result.append({"name": name, "state": (it.get("operstate") or "UNKNOWN").lower(),
                           "mac": it.get("address"), "addresses": addrs,
                           "default_route": name in default_ifaces,
                           "type": "wifi" if name.startswith(("wl", "wlan")) else "ethernet"})
        return result

    async def _default_route_ifaces(self) -> set[str]:
        rc, out, _ = await self.run(["ip", "-j", "route", "show", "default"], timeout=5)
        try:
            return {r.get("dev") for r in json.loads(out or "[]") if r.get("dev")} if rc == 0 else set()
        except ValueError:
            return set()

    def _fallback_interfaces(self) -> list[dict]:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("192.0.2.1", 9))
            addr = s.getsockname()[0]
            s.close()
            return [{"name": "default", "state": "up", "mac": None, "default_route": True, "type": "ethernet",
                     "addresses": [{"family": "ipv4", "address": addr, "prefix": None}]}]
        except OSError:
            return []

    async def default_gateway(self) -> str | None:
        if not self.enabled:
            return None
        rc, out, _ = await self.run(["ip", "-j", "route", "show", "default"], timeout=5)
        try:
            for r in json.loads(out or "[]") if rc == 0 else []:
                if r.get("gateway"):
                    return r["gateway"]
        except ValueError:
            pass
        return None

    async def address_records(self) -> list[dict]:
        out = []
        for it in await self.interfaces():
            for a in it["addresses"]:
                out.append({"interface": it["name"], "family": a["family"], "address": a["address"]})
        return out

    async def primary_address(self) -> str | None:
        ifs = await self.interfaces()
        ifs.sort(key=lambda i: (not i["default_route"], i["name"]))
        for it in ifs:
            for a in it["addresses"]:
                if a["family"] == "ipv4":
                    return a["address"]
        for it in ifs:
            for a in it["addresses"]:
                return a["address"]
        return None

    async def all_addresses(self) -> list[str]:
        out = []
        for it in await self.interfaces():
            out += [a["address"] for a in it["addresses"]]
        return out

    # -- hostname ------------------------------------------------------------
    async def set_hostname(self, name: str) -> bool:
        if not self.hostname_cmd or not self.enabled:
            return False
        rc, _, err = await self.run(self.hostname_cmd + [name], timeout=10)
        if rc != 0:
            log.warning("hostname change failed: %s", err.strip())
        return rc == 0

    # -- Wi-Fi --------------------------------------------------------------
    async def wifi_device(self) -> str | None:
        if not self.enabled:
            return None
        rc, out, _ = await self.run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device"], timeout=5)
        if rc != 0:
            return None
        for line in out.splitlines():
            parts = _nm_split(line)
            if len(parts) >= 2 and parts[1] == "wifi":
                return parts[0]
        return None

    def wifi_radio_registered(self) -> bool:
        """True when the kernel has registered a Wi-Fi radio, whether or not NetworkManager
        lists it yet: early in a boot hostd asks before NetworkManager has the device."""
        if not self.enabled:
            return False
        try:
            return any(self.ieee80211_dir.iterdir())
        except OSError:
            return False

    async def wifi_state(self) -> dict:
        dev = await self.wifi_device()
        if not dev:
            return {"available": False, "reason": "No Wi-Fi hardware", "device": None,
                    "connected": None, "profiles": [], "networks": [], "radio": False, "state": "unavailable",
                    "ssid": None, "scanned_at": None}
        rc, out, _ = await self.run(["nmcli", "-t", "-f", "GENERAL.STATE,GENERAL.CONNECTION",
                                     "device", "show", dev], timeout=5)
        connected = None
        for line in out.splitlines():
            if line.startswith("GENERAL.CONNECTION:"):
                v = line.split(":", 1)[1]
                connected = v if v and v != "--" else None
        profiles = await self.profiles()
        known = {p["ssid"] for p in profiles}
        nets = [dict(n, known=n["ssid"] in known) for n in self.last_scan]
        return {"available": True, "device": dev, "connected": connected, "profiles": profiles,
                "radio": True, "state": "connected" if connected else "disconnected",
                "ssid": connected[len("RetroBoot "):] if connected and connected.startswith("RetroBoot ") else connected,
                "networks": nets, "scanned_at": self.scanned_at}

    async def profiles(self) -> list[dict]:
        rc, out, _ = await self.run(["nmcli", "-t", "-f", "NAME,UUID,TYPE", "connection", "show"], timeout=5)
        res = []
        for line in out.splitlines() if rc == 0 else []:
            p = _nm_split(line)
            if len(p) >= 3 and p[2] in ("802-11-wireless", "wifi"):
                ssid = p[0][len("RetroBoot "):] if p[0].startswith("RetroBoot ") else p[0]
                res.append({"name": p[0], "uuid": p[1], "id": p[1], "ssid": ssid})
        return res

    async def scan(self) -> list[dict]:
        async with self.lock:
            dev = await self.wifi_device()
            if not dev:
                raise NetworkError("No Wi-Fi hardware", "no_wifi")
            await self.run(["nmcli", "device", "wifi", "rescan", "ifname", dev], timeout=20)
            rc, out, err = await self.run(["nmcli", "-t", "-f", "IN-USE,SSID,BSSID,SIGNAL,SECURITY,CHAN",
                                           "device", "wifi", "list", "ifname", dev], timeout=20)
            if rc != 0:
                raise NetworkError("Scanning for networks failed.", "scan_failed")
            nets: dict[str, dict] = {}
            for line in out.splitlines():
                p = _nm_split(line)
                if len(p) < 6 or not p[1]:
                    continue
                ssid = p[1]
                try:
                    signal = int(p[3])
                except ValueError:
                    signal = 0
                cur = nets.get(ssid)
                entry = {"id": ssid, "ssid": ssid, "signal": signal, "security": p[4] or "open",
                         "in_use": p[0].strip() == "*", "active": p[0].strip() == "*", "channel": p[5]}
                if cur is None or signal > cur["signal"]:
                    nets[ssid] = entry
            self.last_scan = sorted(nets.values(), key=lambda n: (-n["in_use"], -n["signal"], n["ssid"]))
            from .util import wall
            self.scanned_at = wall()
            return self.last_scan

    @staticmethod
    def validate(ssid: str, psk: str | None) -> None:
        if not isinstance(ssid, str) or not 1 <= len(ssid.encode()) <= SSID_MAX or "\n" in ssid or "\0" in ssid:
            raise NetworkError("Network names are 1 to 32 bytes.", "invalid")
        if psk is not None and (not isinstance(psk, str) or not (8 <= len(psk) <= 63 or len(psk) == 64)
                                or "\n" in psk or "\0" in psk):
            raise NetworkError("Wi-Fi passwords are 8 to 63 characters.", "invalid")

    @staticmethod
    def keyfile(con_uuid: str, ssid: str, psk: str | None, hidden: bool = False, key_mgmt: str = "wpa-psk") -> str:
        """NetworkManager keyfile text for one RetroBoot Wi-Fi profile (autoconnect)."""
        name = "RetroBoot " + re.sub(r"[^A-Za-z0-9 _.-]", "_", ssid)[:40]
        lines = ["[connection]", f"id={keyfile_escape(name)}", f"uuid={con_uuid}", "type=wifi",
                 "autoconnect=true", "", "[wifi]", "mode=infrastructure",
                 f"ssid={keyfile_escape(ssid)}"]
        if hidden:
            lines.append("hidden=true")
        if psk:
            # key_mgmt: "wpa-psk" (WPA/WPA2, WPA2/WPA3 transition) or "sae" (WPA3-only; AirPort bridge)
            km = key_mgmt if key_mgmt in ("wpa-psk", "sae") else "wpa-psk"
            lines += ["", "[wifi-security]", f"key-mgmt={km}", f"psk={keyfile_escape(psk)}"]
        lines += ["", "[ipv4]", "method=auto", "", "[ipv6]", "method=auto", ""]
        return "\n".join(lines)

    def _ensure_dir(self) -> None:
        self.nm_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.nm_dir, 0o700)
        except OSError:
            pass

    async def _write_and_load(self, con_uuid: str, text: str) -> Path:
        """Write a keyfile (0600, never replacing one) and have NetworkManager load it."""
        path = self.nm_dir / f"retroboot-{con_uuid}.nmconnection"
        self._ensure_dir()
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        rc, _, _ = await self.run(["nmcli", "connection", "load", str(path)], timeout=10)
        if rc != 0:
            path.unlink(missing_ok=True)
            raise NetworkError("The network profile could not be saved.", "profile_failed")
        return path

    async def save_profile(self, ssid: str, psk: str | None, hidden: bool = False,
                           key_mgmt: str = "wpa-psk") -> str:
        """Store a Wi-Fi profile without waiting for the network (installation media
        settings: the network may be in range only where the computer is used).
        NetworkManager joins it by itself whenever it is in range. Returns the uuid."""
        self.validate(ssid, psk)
        async with self.lock:
            if not await self.wifi_device():
                raise NetworkError("No Wi-Fi hardware", "no_wifi")
            con_uuid = str(uuid.uuid4())
            await self._write_and_load(con_uuid, self.keyfile(con_uuid, ssid, psk, hidden, key_mgmt))
            return con_uuid

    async def wifi_hardware(self) -> dict | None:
        """The Wi-Fi adapter NetworkManager manages (for the installer's hardware check)."""
        dev = await self.wifi_device()
        if not dev:
            return None
        rc, out, _ = await self.run(["nmcli", "-t", "-f", "GENERAL.VENDOR,GENERAL.PRODUCT,GENERAL.DRIVER",
                                     "device", "show", dev], timeout=5)
        info = {"device": dev, "vendor": "", "product": "", "driver": ""}
        for line in out.splitlines() if rc == 0 else []:
            key, _, value = line.partition(":")
            field = key.split(".", 1)[-1].lower()
            if field in info and value.strip() not in ("", "--"):
                info[field] = value.strip()
        vendor = re.sub(r"\b(Corporation|Corp\.?|Inc\.?|Co\.,? Ltd\.?|Ltd\.?|Semiconductor|Technologies|"
                        r"Technology|Communications)\b", "", info["vendor"]).strip(" ,")
        # "RTL8821CE 802.11ac PCIe Wireless Network Adapter" -> "RTL8821CE 802.11ac"
        product = re.sub(r"(\s+(PCIe|PCI Express|USB|Dual Band|Wireless|WLAN|Network|LAN|Adapter|Controller|Card))+$",
                         "", info["product"], flags=re.I).strip()
        label = " ".join(x for x in (vendor.split(" ")[0] if vendor else "", product) if x)
        if not label:
            label = info["driver"] or dev
        info["label"] = label if len(label) <= 48 else label[:47].rstrip() + "…"
        return info

    def migrate_legacy_profiles(self, legacy_dir: Path = LEGACY_NM_CONNECTIONS) -> int:
        """Move keyfiles of an older release (``/etc/NetworkManager/system-connections``)
        into the data partition directory; NetworkManager no longer reads the old one.
        Existing files are never replaced. Returns the number moved (names and
        contents are never logged: they hold network names and secrets)."""
        legacy_dir = Path(legacy_dir)
        try:
            if not legacy_dir.is_dir() or legacy_dir.resolve() == self.nm_dir.resolve():
                return 0
            files = sorted(legacy_dir.glob("*.nmconnection"))
        except OSError:
            return 0
        moved = 0
        for src in files:
            try:
                if src.is_symlink() or not src.is_file():
                    continue
                self._ensure_dir()
                dst = self.nm_dir / src.name
                if dst.exists():
                    continue
                data = src.read_bytes()
                fd = os.open(str(dst), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                src.unlink()
                moved += 1
            except OSError as exc:
                log.warning("could not move a legacy network profile: %s", type(exc).__name__)
        return moved

    async def connect(self, ssid: str, psk: str | None, hidden: bool = False,
                      key_mgmt: str = "wpa-psk") -> dict:
        self.validate(ssid, psk)
        async with self.lock:
            dev = await self.wifi_device()
            if not dev:
                raise NetworkError("No Wi-Fi hardware", "no_wifi")
            con_uuid = str(uuid.uuid4())
            path = await self._write_and_load(con_uuid, self.keyfile(con_uuid, ssid, psk, hidden, key_mgmt))
            rc, _, err = await self.run(["nmcli", "--wait", "45", "connection", "up", "uuid", con_uuid,
                                         "ifname", dev], timeout=60)
            if rc != 0:
                msg = "RetroBoot could not connect to this network. Move closer to it or try again."
                # A WPA2/WPA3 network that rejects the password often surfaces as
                # "network could not be found" (SAE authentication rejected; seen on the
                # first hardware tested), so a failure with a password for a network the
                # last scan saw is reported as a password problem.
                seen = any(n.get("ssid") == ssid for n in self.last_scan)
                if ("Secrets were required" in err or "802-1x" in err or "password" in err.lower()
                        or (psk and seen)):
                    msg = "The password was not accepted. Check it and try again."
                # Keep no broken profile behind.
                await self.run(["nmcli", "connection", "delete", "uuid", con_uuid], timeout=10)
                path.unlink(missing_ok=True)
                raise NetworkError(msg, "join_failed")
            return {"ssid": ssid, "profile": con_uuid}

    async def forget(self, profile: str) -> None:
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", profile or ""):
            raise NetworkError("Unknown network profile.", "invalid")
        async with self.lock:
            known = {p["uuid"] for p in await self.profiles()}
            if profile not in known:
                raise NetworkError("Unknown network profile.", "not_found")
            rc, _, _ = await self.run(["nmcli", "connection", "delete", "uuid", profile], timeout=15)
            if rc != 0:
                raise NetworkError("The network could not be forgotten.", "forget_failed")


def is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


_SCONTROL_RE = re.compile(r"^Simple mixer control '([^']+)',(\d+)\s*$", re.M)
_CAPABILITIES_RE = re.compile(r"^\s*Capabilities:\s*(.*)$", re.M)


class AudioMixer:
    """amixer adapter for the live audio.volume setting."""

    def __init__(self, enabled: bool = True, runner: Runner | None = None, detect_switch: bool = True):
        self.enabled = enabled
        self.run = runner or run_cmd
        self.detect_switch = detect_switch
        self.control: str | None = None
        self.card: str | None = None     # ALSA card id of the chosen output (audio.choose); None: default
        self.available = False
        self.mapped = False
        self.has_switch = True
        self.muted = False
        self.last_percent: int | None = None

    def _card_args(self) -> list[str]:
        return ["-c", self.card] if self.card else []

    @staticmethod
    def _parse_has_switch(info: str, *, explicit: bool) -> bool | None:
        caps = _CAPABILITIES_RE.findall(info or "")
        if caps:
            tokens = {tok for line in caps for tok in line.split()}
            return "pswitch" in tokens
        return False if explicit and "pswitch" not in (info or "") else None

    async def probe(self, card: str | None = None, control: str | None = None,
                    detect_switch: bool | None = None) -> bool:
        """Pick the volume control. ``control`` (an ASoC card's UCM playback master,
        e.g. the ES8336's "DAC") wins when it exists; it is set on the mapped
        (perceptual) scale, since its raw steps span 96 dB.  ``detect_switch`` asks
        whether the chosen control has a mute switch: vc4 softvol and USB DACs
        without pswitch fall back to 0 % cleanly."""
        self.available = False
        self.card = card
        self.mapped = False
        self.has_switch = True
        self.muted = False
        self.last_percent: int | None = None
        if not self.enabled:
            return False
        explicit_detect = bool(detect_switch) if detect_switch is not None else False
        should_detect = self.detect_switch if detect_switch is None else bool(detect_switch)
        if control:
            rc, out, _ = await self.run(["amixer", *self._card_args(), "sget", control], timeout=5)
            if rc == 0 and "pvolume" in out:
                self.control, self.mapped = control, True
                parsed = self._parse_has_switch(out, explicit=True)
                self.has_switch = bool(parsed) if parsed is not None else ("pswitch" in out)
                self.available = True
                return True
        rc, out, _ = await self.run(["amixer", *self._card_args(), "scontrols"], timeout=5)
        if rc != 0:
            return False
        controls = {m.group(1) for m in _SCONTROL_RE.finditer(out or "")}
        for name in ("Master", "PCM", "Speaker", "Headphone"):
            if name in controls:
                self.control = name
                self.available = True
                if should_detect:
                    rc, info, _ = await self.run(["amixer", *self._card_args(), "sget", name], timeout=5)
                    if rc == 0:
                        parsed = self._parse_has_switch(info, explicit=explicit_detect)
                        if parsed is not None:
                            self.has_switch = parsed
                    elif explicit_detect:
                        self.has_switch = False
                return True
        return False

    def _sset(self, *values: str) -> list[str]:
        return ["amixer", *self._card_args(), *(["-M"] if self.mapped else []), "-q", "sset", self.control, *values]

    async def set_volume(self, percent: int, unmute: bool = True) -> bool:
        if not self.available or self.control is None:
            return False
        self.last_percent = int(percent)
        if not unmute and not self.has_switch:
            return True
        values = [f"{int(percent)}%"] + (["unmute"] if (self.has_switch and unmute) else [])
        rc, _, _ = await self.run(self._sset(*values), timeout=5)
        if rc == 0 and unmute:
            self.muted = False
        return rc == 0

    async def set_mute(self, mute: bool) -> bool:
        """Mute key: the control's switch, or 0 % and back for a control without one."""
        if not self.available or self.control is None:
            return False
        if self.has_switch:
            values = ["mute" if mute else "unmute"]
        else:
            values = ["0%" if mute else f"{self.last_percent if self.last_percent is not None else 70}%"]
        rc, _, _ = await self.run(self._sset(*values), timeout=5)
        if rc == 0:
            self.muted = bool(mute)
        return rc == 0
