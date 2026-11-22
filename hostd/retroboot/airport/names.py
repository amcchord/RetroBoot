"""How host Wi-Fi networks appear to the original AirPort software.

The guest sees at most 32 bytes of SSID per network, shows them as Mac OS
Roman text, knows only a single "privacy" bit, and joins by SSID alone.  The
host keeps the real SSID bytes, BSSIDs and security; this module translates
between the two (PRODUCT §11 "legacy field limits"):

* **Grouping and stable IDs.**  Access points with the same SSID bytes and the
  same security class are one network (roaming); its ID is
  ``n`` + 16 hex digits of SHA-256(security class, SSID bytes) and never
  depends on scan order.
* **Representable names.**  UTF-8 SSIDs become Mac OS Roman.  Characters
  outside Mac OS Roman lose their accents if that makes them representable
  (``ş`` → ``s``); anything else, control characters and invalid UTF-8 bytes
  become ``?``.  A name with nothing representable left is "Wi-Fi Network".
  Names are cut to 32 bytes.
* **Duplicates.**  Two networks whose guest names collide (same SSID with
  different security, or different SSIDs that translate alike) get " 2", " 3"…
  in the order RetroBoot first saw them.  Assignments are sticky and stored in
  ``state/airport-names.json`` so the guest's remembered network keeps
  meaning the same host network across scans and restarts.
* **Security indicator.**  Every network RetroBoot can join is shown without
  the privacy bit: the guest never asks for a WEP key and never claims a WPA
  network is WEP.  The real security (WPA2/WPA3 …) is shown in RetroBoot's own
  panel and web page.  Networks RetroBoot cannot join are left out: hidden
  SSIDs (join them with "Other…"), and enterprise or WEP networks without a
  host profile.
"""
from __future__ import annotations

import hashlib
import json
import logging
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("retroboot.airport")

SSID_MAX = 32
FALLBACK_NAME = "Wi-Fi Network"
MAX_REMEMBERED = 256

# Security classes, from NetworkManager's SECURITY column or a fixture.
OPEN, OWE, WEP, PSK, SAE, ENTERPRISE = "open", "owe", "wep", "psk", "sae", "enterprise"
SECURITY_LABELS = {OPEN: "None", OWE: "Enhanced Open", WEP: "WEP", PSK: "WPA2 Personal",
                   SAE: "WPA3 Personal", ENTERPRISE: "WPA Enterprise"}
SECRET_CLASSES = (WEP, PSK, SAE)


def security_class(text: str | None) -> str:
    """Map NetworkManager's SECURITY text ("WPA1 WPA2", "WPA3", "802.1X", "OWE", "--")."""
    t = (text or "").upper().replace("-", " ")
    words = set(t.split())
    if "802.1X" in words or "EAP" in words or "ENTERPRISE" in words:
        return ENTERPRISE
    if "WPA3" in words or "SAE" in words:
        # transition networks ("WPA2 WPA3") take a WPA2 passphrase
        return PSK if ("WPA2" in words or "WPA1" in words or "WPA" in words) else SAE
    if words & {"WPA", "WPA1", "WPA2", "PSK"}:
        return PSK
    if "WEP" in words:
        return WEP
    if "OWE" in words:
        return OWE
    return OPEN


def network_id(ssid: bytes, security: str) -> str:
    return "n" + hashlib.sha256(security.encode() + b":" + bytes(ssid)).hexdigest()[:16]


def _char_to_mac(ch: str) -> bytes:
    if ch in "\t\r\n" or unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Cn"):
        return b"?"
    try:
        return ch.encode("mac_roman")
    except UnicodeEncodeError:
        pass
    base = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
    try:
        enc = base.encode("mac_roman")
        if enc and all(0x20 <= b != 0x7f for b in enc):
            return enc
    except UnicodeEncodeError:
        pass
    return b"?"


def guest_name(ssid: bytes) -> bytes:
    """The Mac OS Roman bytes the guest shows for a host SSID (before de-duplication)."""
    ssid = bytes(ssid).rstrip(b"\0")
    try:
        text = ssid.decode("utf-8")
        out = b"".join(_char_to_mac(ch) for ch in text)
    except UnicodeDecodeError:
        out = bytes(b if 0x20 <= b < 0x7f else 0x3f for b in ssid)
    out = out[:SSID_MAX].strip(b" ")
    if not out.strip(b"?"):
        return FALLBACK_NAME.encode("mac_roman")
    return out


def host_ssid_from_guest(name: bytes) -> bytes:
    """A name typed in the guest ("Other…") as host SSID bytes (Mac OS Roman → UTF-8)."""
    return bytes(name).decode("mac_roman").encode("utf-8")


def with_suffix(base: bytes, n: int) -> bytes:
    suffix = f" {n}".encode()
    return base[:SSID_MAX - len(suffix)].rstrip(b" ") + suffix


@dataclass
class HostNetwork:
    """One access point as the host sees it."""
    ssid: bytes
    bssid: str
    channel: int
    signal: int                 # dBm
    security: str               # security class
    known: bool = False         # a host profile exists (no password needed)
    active: bool = False        # the host is connected to this AP

    @property
    def id(self) -> str:
        return network_id(self.ssid, self.security)


@dataclass
class GuestNetwork:
    """One network as the guest sees it."""
    id: str
    name: bytes                 # guest-visible SSID (Mac OS Roman, <= 32 bytes)
    ssid: bytes                 # real host SSID
    security: str
    bssid: str
    channel: int
    signal: int
    known: bool
    active: bool
    aps: int = 1

    @property
    def needs_secret(self) -> bool:
        return self.security in SECRET_CLASSES and not self.known

    def wire(self, noise: int = -95) -> dict:
        return {"ssid": self.name.hex(), "bssid": self.bssid, "channel": max(1, min(14, self.channel or 1)),
                "signal": max(-110, min(-10, int(self.signal))), "noise": noise, "privacy": False}

    def public(self) -> dict:
        return {"id": self.id, "guest_name": self.name.decode("mac_roman"),
                "ssid": self.ssid.decode("utf-8", "replace"), "security": SECURITY_LABELS.get(self.security, self.security),
                "signal_dbm": self.signal, "known": self.known, "active": self.active, "access_points": self.aps}


def joinable(net: HostNetwork) -> bool:
    if not net.ssid.strip(b"\0"):
        return False                       # hidden: "Other…" only
    if net.security in (ENTERPRISE, WEP) and not net.known:
        return False
    return True


@dataclass
class NameTable:
    """Sticky guest names per network ID, optionally persisted."""
    path: Path | None = None
    names: dict[str, str] = field(default_factory=dict)      # id -> guest name (hex)
    order: list[str] = field(default_factory=list)            # least recently seen first
    # The network the host was last connected to, per backend ({"networkmanager": id}):
    # the guest's startup network when the host is not connected yet as Mac OS
    # launches (a cold boot races NetworkManager).
    last_host: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None) -> "NameTable":
        t = cls(path)
        if path and Path(path).is_file():
            try:
                data = json.loads(Path(path).read_text())
                for item in data.get("names", [])[-MAX_REMEMBERED:]:
                    nid, name = item["id"], bytes.fromhex(item["name"])
                    if isinstance(nid, str) and len(name) <= SSID_MAX:
                        t.names[nid] = name.hex()
                        t.order.append(nid)
                last = data.get("last_host") or {}
                if isinstance(last, dict):
                    t.last_host = {k: v for k, v in last.items() if isinstance(k, str) and isinstance(v, str)}
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                log.warning("airport name table unreadable; starting fresh")
        return t

    def save(self) -> None:
        if not self.path:
            return
        from ..util import atomic_write_json
        try:
            atomic_write_json(Path(self.path), {"schema": 1, "names": [{"id": i, "name": self.names[i]}
                                                                    for i in self.order if i in self.names],
                                                "last_host": dict(self.last_host)}, 0o600)
        except OSError:
            log.warning("could not save the airport name table")

    def remember_host(self, backend: str, nid: str) -> None:
        if self.last_host.get(backend) != nid:
            self.last_host[backend] = nid
            self.save()

    def last_host_name(self, backend: str) -> tuple[str, bytes] | None:
        nid = self.last_host.get(backend)
        if nid and nid in self.names:
            return nid, bytes.fromhex(self.names[nid])
        return None

    def _touch(self, nid: str) -> None:
        if nid in self.order:
            self.order.remove(nid)
        self.order.append(nid)
        while len(self.order) > MAX_REMEMBERED:
            old = self.order.pop(0)
            self.names.pop(old, None)

    def assign(self, nid: str, base: bytes) -> bytes:
        if nid in self.names:
            self._touch(nid)
            return bytes.fromhex(self.names[nid])
        taken = {v for k, v in self.names.items() if k != nid}
        name, n = base, 1
        while name.hex() in taken:
            n += 1
            name = with_suffix(base, n)
        self.names[nid] = name.hex()
        self._touch(nid)
        return name

    def lookup(self, name: bytes) -> str | None:
        h = bytes(name).hex()
        for nid, v in self.names.items():
            if v == h:
                return nid
        return None

    def guest_view(self, aps: list[HostNetwork], limit: int = 16) -> list[GuestNetwork]:
        """Group access points into guest networks (strongest AP first), at most ``limit``;
        the network the host is connected to is always included."""
        groups: dict[str, list[HostNetwork]] = {}
        for ap in aps:
            if joinable(ap):
                groups.setdefault(ap.id, []).append(ap)
        nets = []
        changed = False
        for nid, members in groups.items():
            members.sort(key=lambda a: (not a.active, -a.signal, a.bssid))
            best = members[0]
            before = len(self.names)
            name = self.assign(nid, guest_name(best.ssid))
            changed |= len(self.names) != before
            nets.append(GuestNetwork(nid, name, best.ssid, best.security, best.bssid, best.channel, best.signal,
                                     any(m.known for m in members), any(m.active for m in members), len(members)))
        nets.sort(key=lambda g: (not g.active, -g.signal, g.name))
        if changed:
            self.save()
        return nets[:limit]
