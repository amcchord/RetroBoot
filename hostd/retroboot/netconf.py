"""Network settings on the installation media (``retroboot/network.conf``).

For setting up a computer without typing a Wi-Fi password on it (headless or
lab installs, like the Wi-Fi file on a Raspberry Pi's boot partition), the
installer looks for ``retroboot/network.conf`` on its own boot medium:

* in the ISO 9660 file system of the installer image (images built with one), or
* on the medium's small FAT partition labelled ``RBINSTEFI``, which macOS,
  Windows and Linux can write to after the installer was copied to a USB stick.

The file is plain ``KEY=VALUE`` lines (``#`` comments, optional quotes)::

    WIFI_SSID=My Network
    WIFI_PASSWORD=correct horse battery staple
    # WIFI_SECURITY=wpa-psk     (default with a password; "sae" for WPA3-only, "open")
    # WIFI_HIDDEN=no

The installer imports it as a NetworkManager profile (a 0600 keyfile, through
:meth:`NetworkAdapter.save_profile`), joins it when it is in range, and the
profile is copied to the installed system like a network joined on screen.
The password is never logged, shown or echoed; parse errors name the line
number and the key, never the value. ``docs/guide/installing.md`` documents the
format for users.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .util import run as run_cmd

log = logging.getLogger("retroboot.netconf")

REL_PATH = "retroboot/network.conf"
ESP_LABEL = "RBINSTEFI"
MAX_BYTES = 4096
KEYS = {"WIFI_SSID", "WIFI_PASSWORD", "WIFI_SECURITY", "WIFI_HIDDEN"}
SECURITY = {"wpa-psk": "wpa-psk", "wpa": "wpa-psk", "wpa2": "wpa-psk", "psk": "wpa-psk",
            "sae": "sae", "wpa3": "sae", "open": "open", "none": "open"}


class NetConfError(Exception):
    """A problem with the file; the message never contains a value from it."""


@dataclass
class MediaNetwork:
    ssid: str
    password: str | None
    key_mgmt: str          # "wpa-psk", "sae" or "open"
    hidden: bool
    source: str            # "iso" or "fat" (where it was found)

    def __repr__(self) -> str:   # never show the password in logs or tracebacks
        return f"MediaNetwork(ssid={self.ssid!r}, key_mgmt={self.key_mgmt!r}, hidden={self.hidden}, " \
               f"source={self.source!r})"


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse(text: str, source: str = "iso") -> MediaNetwork:
    values: dict[str, str] = {}
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip().lstrip("﻿")
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip().upper()
        if not sep or not re.fullmatch(r"[A-Z_]+", key):
            raise NetConfError(f"line {n} is not KEY=VALUE")
        if key not in KEYS:
            raise NetConfError(f"line {n}: unknown setting {key}")
        values[key] = _unquote(value)
    ssid = values.get("WIFI_SSID", "")
    if not ssid:
        raise NetConfError("WIFI_SSID is missing")
    if len(ssid.encode()) > 32 or "\0" in ssid:
        raise NetConfError("WIFI_SSID must be 1 to 32 bytes")
    password = values.get("WIFI_PASSWORD") or None
    sec = values.get("WIFI_SECURITY", "").strip().lower()
    if sec and sec not in SECURITY:
        raise NetConfError("WIFI_SECURITY must be wpa-psk, sae or open")
    key_mgmt = SECURITY[sec] if sec else ("wpa-psk" if password else "open")
    if key_mgmt == "open":
        if password:
            raise NetConfError("WIFI_PASSWORD is set for an open network")
    elif not password:
        raise NetConfError("WIFI_PASSWORD is missing")
    elif not (8 <= len(password) <= 63 or (len(password) == 64 and re.fullmatch(r"[0-9a-fA-F]{64}", password))):
        raise NetConfError("WIFI_PASSWORD must be 8 to 63 characters")
    hidden = values.get("WIFI_HIDDEN", "no").strip().lower() in ("1", "yes", "true", "on")
    return MediaNetwork(ssid, password, key_mgmt, hidden, source)


def read_file(path: Path, source: str) -> MediaNetwork | None:
    try:
        st = path.stat()
        if not path.is_file() or path.is_symlink():
            return None
        if st.st_size > MAX_BYTES:
            raise NetConfError("the file is larger than 4 KB")
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise NetConfError(f"the file could not be read ({type(exc).__name__})") from None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise NetConfError("the file is not UTF-8 text") from None
    return parse(text, source)


async def _fat_partition(medium: Path, runner) -> str | None:
    """The boot medium's FAT partition labelled RBINSTEFI (USB sticks), if any."""
    rc, out, _ = await runner(["findmnt", "-n", "-o", "SOURCE", "--target", str(medium)], timeout=5)
    src = out.strip().splitlines()[0] if rc == 0 and out.strip() else ""
    if not src.startswith("/dev/"):
        return None
    rc, out, _ = await runner(["lsblk", "-n", "-r", "-o", "PKNAME", src], timeout=5)
    parent = out.strip().splitlines()[0].strip() if rc == 0 and out.strip() else ""
    disk = f"/dev/{parent}" if parent else src
    rc, out, _ = await runner(["lsblk", "-n", "-r", "-o", "PATH,LABEL,FSTYPE", disk], timeout=5)
    for line in out.splitlines() if rc == 0 else []:
        parts = line.split(" ")
        if len(parts) >= 3 and parts[1] == ESP_LABEL and parts[2] == "vfat":
            return parts[0]
    return None


async def find_media_file(rel: str, reader, medium: Path = Path("/run/retroboot/live/medium"), runner=None,
                          mount_root: Path = Path("/run/retroboot"), fat: bool = True, prefix: str = "media-"):
    """``reader(path, source)`` on ``rel`` in the installer's boot medium: its ISO tree
    first, then (``fat``) its RBINSTEFI FAT partition, mounted read-only for the read.
    The first non-None result wins; also used for ``retroboot/lab-auth.env`` (labauth.py)."""
    runner = runner or run_cmd
    if not Path(medium).is_dir():
        return None
    found = reader(Path(medium) / rel, "iso")
    if found or not fat:
        return found
    part = await _fat_partition(Path(medium), runner)
    if not part:
        return None
    mnt = Path(tempfile.mkdtemp(prefix=prefix, dir=str(mount_root)))
    try:
        rc, _, _ = await runner(["mount", "-t", "vfat", "-o", "ro,nosuid,nodev,noexec", part, str(mnt)], timeout=15)
        if rc != 0:
            return None
        try:
            return reader(mnt / rel, "fat")
        finally:
            await runner(["umount", str(mnt)], timeout=15)
    finally:
        try:
            os.rmdir(mnt)
        except OSError:
            pass


async def find_on_media(medium: Path = Path("/run/retroboot/live/medium"), runner=None,
                        mount_root: Path = Path("/run/retroboot")) -> MediaNetwork | None:
    """Look for retroboot/network.conf on the installer's boot medium (ISO tree
    first, then its RBINSTEFI FAT partition, mounted read-only for the read)."""
    return await find_media_file(REL_PATH, read_file, medium, runner, mount_root, prefix="netconf-")


async def import_network(adapter, net: MediaNetwork, connect_timeout: float = 45.0) -> dict:
    """Store the media network as a profile and wait (bounded) for NetworkManager
    to join it. Returns {"ssid", "profile", "connected"}; raises NetworkError when
    there is no Wi-Fi hardware or the profile cannot be stored."""
    psk = net.password if net.key_mgmt != "open" else None
    key_mgmt = "sae" if net.key_mgmt == "sae" else "wpa-psk"
    # The installer imports the file at every start (hostd restarts included): a
    # profile already stored for this network is replaced, never duplicated.
    name = "RetroBoot " + re.sub(r"[^A-Za-z0-9 _.-]", "_", net.ssid)[:40]   # NetworkAdapter.keyfile's id
    for old in await adapter.profiles():
        if old.get("name") == name:
            await adapter.run(["nmcli", "connection", "delete", "uuid", old["uuid"]], timeout=15)
            for f in adapter.nm_dir.glob("*.nmconnection"):
                try:
                    if f"uuid={old['uuid']}" in f.read_text(errors="replace"):
                        f.unlink()
                except OSError:
                    pass
    profile = await adapter.save_profile(net.ssid, psk, hidden=net.hidden, key_mgmt=key_mgmt)
    connected = False
    dev = await adapter.wifi_device()
    if dev:
        rc, _, _ = await adapter.run(["nmcli", "--wait", str(int(connect_timeout)), "connection", "up", "uuid",
                                      profile, "ifname", dev], timeout=connect_timeout + 15)
        connected = rc == 0
    return {"ssid": net.ssid, "profile": profile, "connected": connected}


def describe(result: dict | None, error: str | None) -> str | None:
    """Installer wording for what happened to the media network file."""
    if error:
        return f"The network settings on the installation media were not used: {error}."
    if not result:
        return None
    if result.get("connected"):
        return f"Joined “{result['ssid']}” using the network settings on the installation media."
    return (f"Saved “{result['ssid']}” from the network settings on the installation media. "
            "RetroBoot joins it when it is in range.")


__all__ = ["MediaNetwork", "NetConfError", "parse", "read_file", "find_on_media", "find_media_file",
           "import_network", "describe", "REL_PATH", "ESP_LABEL"]


if __name__ == "__main__":  # pragma: no cover - lab check of a file: python3 -m retroboot.netconf FILE
    import sys
    try:
        print(repr(read_file(Path(sys.argv[1]), "iso")))
    except NetConfError as exc:
        print(f"invalid: {exc}")
        sys.exit(1)
