"""AppleTalk inactive for RetroBoot guests (boot time).

With AppleTalk active (Mac OS's default when there is no "AppleTalk
Preferences" file), Open Transport spends about 5 s of every startup in the
extension parade looking for an AppleTalk network: ten AARP probes 200 ms
apart for a provisional node address, six ZIP GetNetInfo requests 500 ms
apart for a router, then NBP lookups for its own name.  The guest is idle the
whole time (emulator/reports/boottime-20261011.md).  RetroBoot's guest network
(QEMU user-mode NAT, for Ethernet and the AirPort card alike) carries IP only,
so nothing can ever answer, and nothing AppleTalk-only is reachable.

The template is the AppleTalk control panel's own "Default" configuration
after Options... > Inactive > OK and Save, recorded from the 9.2.1 panel on
RetroBoot's emulated machine (October 11 2026); only the user level is Basic,
as on a fresh install (the recording was made in Advanced mode).  A fresh
QEMU process boots with AppleTalk inactive from this file alone (the panel
then says "AppleTalk is currently inactive"; no AARP, ZIP or NBP frames), and
Mac OS rewrites the file when the user makes AppleTalk active again (Chooser
or panel), so the user can always undo it.

Ownership rule, as for TCP/IP: RetroBoot installs the file only when it is
missing or empty, and keeps one that is exactly its own template.  Any other
"AppleTalk Preferences" is the user's and is never changed.
"""
from __future__ import annotations

import os
import struct
import uuid

from . import hfsplus

APPLETALK_NAME = "AppleTalk Preferences"
APPLETALK_TYPE_CREATOR = (b"pref", b"atdv")

_ATPF = bytes.fromhex(
    "00000000040a0000000000001f4a66cc1f4a667c1f4a66641f4a664c1f4a66341f4a6604"
    "1f4a65ec1f4a65dc0001002800000008000027100000000400000006000000c8000000c8"
    "0000000a000000c800000008000100440000024a00c3ff6a000300c8000000fa00009c40"
    "0000c35000001000000000c8ffff00013c3f00000000f03fffff00013d0100000000f04c"
    "ffff00013db7000000010012000002480000032000000003000000010012000007d00000"
    "07d00fa0040400000001001200000242000007d00000000800000001002a000000100000"
    "023c0000023c00000bb800007530000000640000006400006c0003030430000000010010"
    "000007d000003a98000808000001000a000075300808")

APPLETALK_INACTIVE_RESOURCES = [
    ("atpf", 128, "", _ATPF),
    ("ccfg", 1, "", bytes.fromhex("0080")),
    ("cnam", 128, "Default", b""),
    ("cvrs", 128, "", bytes.fromhex("0001")),
    ("lcks", 128, "", bytes.fromhex("0000")),
    ("port", 128, "", b"\x08Ethernet"),
    ("prot", 128, "", b"ddp\x00"),
    ("ptfm", 128, "", bytes.fromhex("000a")),
    ("pwrd", 128, "", bytes.fromhex("00")),
    ("ulvl", 1, "", bytes.fromhex("0001")),
    ("wpos", 1, "", bytes.fromhex("0032000a")),
]
# Resources that decide ownership (the window position does not).
APPLETALK_CONFIG_TYPES = frozenset({"atpf", "ccfg", "cnam", "cvrs", "lcks", "port", "prot", "ptfm", "pwrd", "ulvl"})


def _config_of(resources) -> dict:
    return {(t, i): d for t, i, *_rest, d in resources if t in APPLETALK_CONFIG_TYPES}


def classify(fork: bytes) -> str:
    """"retroboot-inactive" for RetroBoot's own template, else "user"."""
    try:
        cfg = _config_of(hfsplus.parse_resource_fork(fork))
    except (hfsplus.HFSError, ValueError, IndexError, struct.error):
        return "user"
    return "retroboot-inactive" if cfg == _config_of(APPLETALK_INACTIVE_RESOURCES) else "user"


def state(image: str) -> tuple[list[str], str]:
    """(Preferences folder path, "missing" | "empty" | "retroboot-inactive" | "user")."""
    from .prep import _system_folder
    with hfsplus.Volume(image) as vol:
        prefs = _system_folder(vol) + ["Preferences"]
        try:
            rec = vol.lookup(prefs + [APPLETALK_NAME])
        except FileNotFoundError:
            return prefs, "missing"
        if rec.fork("rsrc").logical_size == 0:
            return prefs, "empty"
        return prefs, classify(vol.read_fork(rec, "rsrc"))


def install_inactive(image: str) -> str:
    """Make AppleTalk inactive when the configuration is RetroBoot's to change.

    Same mechanism as prep.install_tcpip_dhcp: the Linux driver creates the
    file and a temporary file whose data fork holds the resource-fork bytes,
    the fork descriptor moves offline, the driver removes the temporary file,
    and the result is read back."""
    from .prep import FINDER_INFO_XATTR, PrepError, _linux_name, mounted
    prefs, st = state(image)
    if st == "user":
        return "existing-configuration-kept"
    if st == "retroboot-inactive":
        return "kept-retroboot-inactive"
    fork = hfsplus.build_resource_fork(APPLETALK_INACTIVE_RESOURCES)
    tmp_name = f"rbprep-{uuid.uuid4().hex[:8]}"
    folder = os.path.join(*[_linux_name(p) for p in prefs])
    with mounted(image) as mnt:
        target = os.path.join(mnt, folder, _linux_name(APPLETALK_NAME))
        if os.path.exists(target):
            os.unlink(target)
        with open(target, "wb"):
            pass
        os.setxattr(target, FINDER_INFO_XATTR, b"".join(APPLETALK_TYPE_CREATOR) + bytes(24))
        with open(os.path.join(mnt, folder, tmp_name), "wb") as f:
            f.write(fork)
            f.flush()
            os.fsync(f.fileno())
    with hfsplus.Volume(image, writable=True) as vol:
        target_rec = vol.lookup(prefs + [APPLETALK_NAME])
        tmp_rec = vol.lookup(prefs + [tmp_name])
        moved = tmp_rec.fork("data")
        if moved.logical_size != len(fork) or moved.covered_blocks() < moved.total_blocks:
            raise PrepError("temporary fork allocation is not usable")
        if target_rec.fork("rsrc").total_blocks or target_rec.fork("data").total_blocks:
            raise PrepError("target file unexpectedly has allocated forks")
        vol.set_fork_record(target_rec, "rsrc", moved)
        vol.set_fork_record(tmp_rec, "data", hfsplus.Fork.empty())
        vol.flush()
    with mounted(image) as mnt:
        os.unlink(os.path.join(mnt, folder, tmp_name))
    with hfsplus.Volume(image) as vol:
        rec = vol.lookup(prefs + [APPLETALK_NAME])
        if vol.read_fork(rec, "rsrc") != fork or rec.type_creator != APPLETALK_TYPE_CREATOR:
            raise PrepError("AppleTalk Preferences verification failed")
    return "installed-inactive" if st == "missing" else "replaced-empty-file"
