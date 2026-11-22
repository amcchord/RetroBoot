"""RetroBoot guest preparation for a stopped Mac OS 9 disk image.

Applied once per imported machine (idempotent) and only while the guest is
stopped. Requires root, util-linux losetup/mount, the hfsplus kernel module
and fsck.hfsplus (Debian hfsprogs).

* Turns off General Controls' "warn me if the computer was shut down
  improperly" check (resource 'Smrt' 0, bit 0x40). RetroBoot repairs the
  volume on the host instead, invisibly, after any unclean stop.
* Sets Energy Saver's system, display and disk sleep to Never. The emulated
  Mac cannot sleep usefully; the host owns display power management.
* Turns off Software Update's scheduled automatic check ('scpp' 1756, bit
  0x8000 of the first long; "Ask me before installing" is left alone).
  Apple's update servers are gone: a scheduled check that starts after its
  weekly time (the fixture's is Sunday 1:28 PM) keeps a network task alive
  and Mac OS then accepts Shut Down or Restart but never completes it.
* Installs a TCP/IP Preferences file configured by DHCP for the guest's
  network adapter: "Ethernet" (the emulated Sungem) or "AirPort" (the
  emulated AirPort card, only when the guest has the AirPort software).
  Ownership rule: RetroBoot replaces only a configuration it could have
  written itself -- a missing or empty file, or one whose configuration
  resources are exactly one of RetroBoot's DHCP templates (the DHCP lease
  cache, window position and version stamps are ignored).  Anything else is
  the user's configuration and is never changed; the report says so.
"""
from __future__ import annotations

import contextlib
import os
import struct
import subprocess
import tempfile
import uuid

from . import hfsplus

# What apply() does to a disk; bump it whenever apply() gains a change that disks
# prepared earlier lack. hostd records it in machine.json and re-runs apply() on a
# stopped guest whose recorded version is older.
#   1  fsck, improper-shutdown warning off, sleep never, Ethernet/DHCP TCP/IP
#   2  Software Update scheduled check off (the stuck Shut Down)
#   3  Energy Saver's portable (PowerBook) sleep timers never, for the PMU
#      machine with a battery (battery passthrough)
#   4  AppleTalk inactive (appletalk.py; ~5 s of every startup)
#   5  AirPort's Software Base Station (AirPort AP) off with the Ethernet adapter,
#      back on with the AirPort card (basestation.py; ~1–1.6 s of busy vCPU after the Finder)
PREP_VERSION = 5

SMRT_CHECK_DISK = 0x40

# TCP/IP control panel configuration "Default": Ethernet (built-in Sungem,
# interface enet0 "gmac"), Using DHCP Server, no manual addresses. Recorded
# from the 9.2.1 control panel's own output on RetroBoot's emulated machine;
# the DHCP lease cache ('dclt') is deliberately omitted.
_IITF = bytes.fromhex(
    "0001030000000000000000012a05656e6574300000000000000000000000000000000000"
    "0000000000000000000000000004676d6163000000000000000000000000000000000000"
    "00000000000000000000000001")
TCPIP_DHCP_RESOURCES = [
    ("ccfg", 1, "", bytes.fromhex("0080")),
    ("cnam", 128, "Default", b""),
    ("cvrs", 128, "", bytes.fromhex("0001")),
    ("dcid", 128, "", bytes.fromhex("00")),
    ("dtyp", 128, "", bytes.fromhex("000a")),
    ("idns", 128, "", bytes.fromhex("0000")),
    ("ihst", 128, "", bytes.fromhex("010000")),
    ("iitf", 128, "", _IITF),
    ("irte", 128, "", bytes.fromhex("0000")),
    ("isdm", 128, "", bytes.fromhex("0000")),
    ("port", 128, "", b"\x08Ethernet"),
    ("prot", 128, "", b"tcp\x00"),
    ("pwrd", 128, "", bytes.fromhex("00")),
    ("stng", 128, "", bytes(27)),
    ("ulvl", 128, "", bytes.fromhex("0001")),
    ("unld", 128, "", bytes.fromhex("0001")),
    ("wpos", 128, "Default window position", bytes.fromhex("00320032")),
]
TCPIP_ETHERNET_DHCP_RESOURCES = TCPIP_DHCP_RESOURCES

# The same configuration with "Connect via: AirPort", recorded from the 9.2.1
# TCP/IP control panel on RetroBoot's emulated machine with only the AirPort
# card present (October 11 2026, AirPort 2.0.4): Open Transport port "enet0",
# module "radio".  The DHCP lease cache ('dclt') is omitted as for Ethernet.
_IITF_AIRPORT = bytes.fromhex(
    "0001030000000000000000012a05656e6574300000000000000000000000000000000000"
    "0000000000000000000000000005726164696f0000000000000000000000000000000000"
    "00000000000000000000000001")
TCPIP_AIRPORT_DHCP_RESOURCES = [
    (t, i, n, _IITF_AIRPORT if t == "iitf" else (b"\x07AirPort" if t == "port" else d))
    for t, i, n, d in TCPIP_DHCP_RESOURCES]
TCPIP_TEMPLATES = {"ethernet": TCPIP_ETHERNET_DHCP_RESOURCES, "airport": TCPIP_AIRPORT_DHCP_RESOURCES}
# Resources that define a configuration; others (dclt lease cache, wpos,
# vers/ftag stamps added by installers, ...) do not decide ownership.
TCPIP_CONFIG_TYPES = frozenset({"ccfg", "cnam", "cvrs", "dcid", "dtyp", "idns", "ihst", "iitf", "irte", "isdm",
                                "port", "prot", "pwrd", "stng", "ulvl", "unld"})
NETWORK_PORTS = ("ethernet", "airport")
AIRPORT_DRIVER = "AirPort Driver"
TCPIP_NAME = "TCP/IP Preferences"
FINDER_INFO_XATTR = "osx.com.apple.FinderInfo"  # Linux hfsplus namespace


class PrepError(Exception):
    pass


def _run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


@contextlib.contextmanager
def loop_device(image: str, offset: int, length: int):
    dev = _run(["losetup", "-f", "--show", "-o", str(offset), "--sizelimit", str(length), image]).stdout.strip()
    try:
        yield dev
    finally:
        subprocess.run(["losetup", "-d", dev], check=False)


@contextlib.contextmanager
def mounted(image: str):
    offset, length = hfsplus.locate_volume(image)
    with loop_device(image, offset, length) as dev, tempfile.TemporaryDirectory(prefix="rbprep-") as mnt:
        _run(["mount", "-t", "hfsplus", "-o", "rw,nodev,nosuid,noexec", dev, mnt])
        try:
            yield mnt
        finally:
            _run(["sync"])
            _run(["umount", mnt])


def fsck(image: str, repair: bool) -> tuple[bool, str]:
    offset, length = hfsplus.locate_volume(image)
    with loop_device(image, offset, length) as dev:
        res = subprocess.run(["fsck.hfsplus", "-f", "-y" if repair else "-n", dev],
                             capture_output=True, text=True)
    out = (res.stdout + res.stderr).strip()
    return res.returncode == 0 and "appears to be OK" in out, out


# fsck.hfsplus findings that only mean "a B-tree node the Linux driver freed was
# not zero-filled" (Apple's fsck_hfs E_UnusedNodeNotZeroed, a minor repair). The
# hfsplus driver never erases freed catalog nodes, so deleting files through it
# (install_tree's temporary resource-fork carriers) can leave one behind.
_COSMETIC_FSCK = ("Unused node is not erased",)


def fsck_verify(image: str) -> tuple[bool, str, bool]:
    """``fsck -fn``; when its only findings are unerased free B-tree nodes,
    repair those (``-fy``) and check again.  Returns (ok, output, repaired)."""
    ok, out = fsck(image, repair=False)
    if ok:
        return True, out, False
    findings = [l.strip() for l in out.splitlines()
                if l.strip() and not l.strip().startswith(("**", "Executing fsck_hfs", "The volume name is"))]
    if not findings or not all(any(c in f for c in _COSMETIC_FSCK) for f in findings):
        return False, out, False
    fsck(image, repair=True)
    ok, out = fsck(image, repair=False)
    return ok, out, True


def _linux_name(mac_name: str) -> str:
    # The Linux hfsplus driver presents Mac '/' as ':' in path components.
    return mac_name.replace("/", ":")


def _system_folder(vol: hfsplus.Volume) -> list[str]:
    """Path of the blessed System Folder (volume finder info word 0)."""
    vh = vol._read_abs(vol.offset + 1024, 512)
    blessed = int.from_bytes(vh[0x50:0x54], "big")
    names = {}
    for rec in vol.records():
        if rec.is_folder:
            names[rec.cnid] = (rec.parent_id, rec.name)
    if blessed not in names:
        blessed = next((c for c, (p, n) in names.items() if p == hfsplus.ROOT_FOLDER_ID and n == "System Folder"), 0)
    if not blessed:
        raise PrepError("no blessed System Folder")
    path = []
    cnid = blessed
    while cnid != hfsplus.ROOT_FOLDER_ID:
        parent, name = names[cnid]
        path.insert(0, name)
        cnid = parent
    return path


def disable_shutdown_warning(image: str) -> str:
    with hfsplus.Volume(image, writable=True) as vol:
        prefs = _system_folder(vol) + ["Preferences", "General Controls Prefs"]
        try:
            rec = vol.lookup(prefs)
        except FileNotFoundError:
            return "general-controls-prefs-missing"
        fork = vol.read_fork(rec, "rsrc")
        for rtype, rid, _name, pos, data in hfsplus.parse_resource_fork(fork):
            if rtype == "Smrt" and rid == 0 and len(data) == 4:
                value = int.from_bytes(data, "big")
                if not value & SMRT_CHECK_DISK:
                    return "already-disabled"
                vol.write_fork_in_place(rec, "rsrc", pos, (value & ~SMRT_CHECK_DISK).to_bytes(4, "big"))
                vol.flush()
                return f"disabled (0x{value:08x} -> 0x{value & ~SMRT_CHECK_DISK:08x})"
        return "smrt-resource-missing"


ENERGY_NEVER = 0x00FF
ENERGY_NAME = "Energy Saver Preferences"


def disable_guest_sleep(image: str) -> str:
    """Energy Saver 'wnkl' -300: u16 timers at 32/34/36 (system, display,
    disk; 0x00FF = Never) and enable flags at 40..42, as written by the 9.2.1
    control panel when its sliders are set to Never."""
    with hfsplus.Volume(image, writable=True) as vol:
        path = _system_folder(vol) + ["Preferences", ENERGY_NAME]
        try:
            rec = vol.lookup(path)
        except FileNotFoundError:
            return "energy-saver-prefs-missing"
        fork = vol.read_fork(rec, "rsrc")
        for rtype, rid, _name, pos, data in hfsplus.parse_resource_fork(fork):
            if rtype != "wnkl" or rid != -300:
                continue
            if len(data) != 206:
                return "unexpected-layout"
            timers = [int.from_bytes(data[o:o + 2], "big") for o in (32, 34, 36)]
            flags = list(data[40:43])
            if any(t > 0xFF for t in timers) or any(f not in (0, 1) for f in flags):
                return "unexpected-layout"
            if timers == [ENERGY_NEVER] * 3 and flags == [0, 0, 0]:
                return "already-never"
            vol.write_fork_in_place(rec, "rsrc", pos + 32, ENERGY_NEVER.to_bytes(2, "big") * 3)
            vol.write_fork_in_place(rec, "rsrc", pos + 40, bytes(3))
            vol.flush()
            return f"sleep-never (timers {timers} -> 255)"
        return "wnkl-resource-missing"


# Energy Saver on a portable.  On RetroBoot's PMU machine with a battery
# (battery passthrough, hostd/retroboot/battery.py) Mac OS 9.2.1 is a PowerBook
# and keeps its portable settings in 'wnkl' -360 (206 bytes), which it creates
# at its first start on that machine; the desktop's -300 above is not used
# there.  Two settings blocks, Power Adapter at 32 and Battery at 60, hold u16
# system/display/disk timers in 15-second units and the three enable flags
# 8 bytes later; the 9.2.1 panel writes 0x0040 with the flag cleared for
# Never.  Mac OS's own defaults sleep the system after 15 minutes on the
# adapter and after 3 minutes on battery (display 5/2, disk 8/1 minutes).
# The template is what Mac OS writes at its first start on the PMU machine,
# recorded on the emulated machine (research/battery/reports), with both
# blocks set to Never.
PORTABLE_ENERGY_ID = -360
PORTABLE_BLOCKS = (("adapter", 32), ("battery", 60))
PORTABLE_NEVER = 0x0040
PORTABLE_ENERGY_DEFAULTS = bytes.fromhex(
    "0000000400000000000000000000000000000000000000000000000000000000003c0014002000050101010101000000000100000101000000000000000c0008000400010101010101000000000100000100000100010000000c00080004000101010101"
    "0100000000010000010000010001000000000000000001c2000001c2000001c2000001c2000001c2000001c2000001c2000001c2001f00000000041a0000041a0000041a0000041a0000041a0000041a0000041a0000041a001f00000000000000000000000000000000")


def portable_never(data: bytes) -> tuple[bytes, list[list[int]]] | None:
    """(data with both portable blocks at Never, the timers before) or None."""
    if len(data) != len(PORTABLE_ENERGY_DEFAULTS):
        return None
    out = bytearray(data)
    before = []
    for _name, off in PORTABLE_BLOCKS:
        timers = [int.from_bytes(data[off + 2 * k:off + 2 * k + 2], "big") for k in range(3)]
        flags = list(data[off + 8:off + 11])
        if any(t > 0xFF for t in timers) or any(f not in (0, 1) for f in flags):
            return None
        before.append(timers)
        out[off:off + 6] = PORTABLE_NEVER.to_bytes(2, "big") * 3
        out[off + 8:off + 11] = bytes(3)
    return bytes(out), before


def disable_portable_sleep(image: str) -> str:
    """Set Energy Saver's portable (PowerBook) sleep timers to Never.

    Returns "already-never", "sleep-never (...)", "created-never" (the
    portable settings did not exist yet: written from the template so that
    Mac OS's first start on the PMU machine does not sleep after 3 idle
    minutes on battery), "energy-saver-prefs-missing" or "unexpected-layout".
    Run on a stopped guest only."""
    with hfsplus.Volume(image, writable=True) as vol:
        prefs = _system_folder(vol) + ["Preferences"]
        try:
            rec = vol.lookup(prefs + [ENERGY_NAME])
        except FileNotFoundError:
            return "energy-saver-prefs-missing"
        fork = vol.read_fork(rec, "rsrc")
        resources = hfsplus.parse_resource_fork(fork)
        type_creator = rec.type_creator
        for rtype, rid, _name, pos, data in resources:
            if rtype != "wnkl" or rid != PORTABLE_ENERGY_ID:
                continue
            res = portable_never(data)
            if res is None:
                return "unexpected-layout"
            new, before = res
            if new == data:
                return "already-never"
            vol.write_fork_in_place(rec, "rsrc", pos, new)
            vol.flush()
            return f"sleep-never (adapter {before[0]}, battery {before[1]} -> never)"
    new, _ = portable_never(PORTABLE_ENERGY_DEFAULTS)
    keep = [(t, i, n, d) for t, i, n, _p, d in resources]
    _replace_resource_fork(image, prefs, ENERGY_NAME, hfsplus.build_resource_fork(
        keep + [("wnkl", PORTABLE_ENERGY_ID, "", new)]), type_creator)
    return "created-never"


def _replace_resource_fork(image: str, folder: list[str], name: str, fork: bytes,
                           type_creator: tuple[bytes, bytes]) -> None:
    """Recreate ``folder:name`` with ``fork`` as its resource fork (no data
    fork), the way install_tcpip_dhcp writes TCP/IP Preferences: Linux
    allocates the blocks through a temporary file's data fork, then the fork
    descriptor moves into the target's catalog record."""
    tmp_name = f"rbprep-{uuid.uuid4().hex[:8]}"
    linux_folder = os.path.join(*[_linux_name(p) for p in folder])
    with mounted(image) as mnt:
        target = os.path.join(mnt, linux_folder, _linux_name(name))
        if os.path.exists(target):
            os.unlink(target)
        with open(target, "wb"):
            pass
        os.setxattr(target, FINDER_INFO_XATTR, type_creator[0] + type_creator[1] + bytes(24))
        with open(os.path.join(mnt, linux_folder, tmp_name), "wb") as f:
            f.write(fork)
            f.flush()
            os.fsync(f.fileno())
    with hfsplus.Volume(image, writable=True) as vol:
        target_rec = vol.lookup(folder + [name])
        tmp_rec = vol.lookup(folder + [tmp_name])
        moved = tmp_rec.fork("data")
        if moved.logical_size != len(fork) or moved.covered_blocks() < moved.total_blocks:
            raise PrepError("temporary fork allocation is not usable")
        if target_rec.fork("rsrc").total_blocks or target_rec.fork("data").total_blocks:
            raise PrepError("target file unexpectedly has allocated forks")
        vol.set_fork_record(target_rec, "rsrc", moved)
        vol.set_fork_record(tmp_rec, "data", hfsplus.Fork.empty())
        vol.flush()
    with mounted(image) as mnt:
        os.unlink(os.path.join(mnt, linux_folder, tmp_name))
    with hfsplus.Volume(image) as vol:
        rec = vol.lookup(folder + [name])
        if vol.read_fork(rec, "rsrc") != fork or rec.type_creator != type_creator:
            raise PrepError(f"{name} verification failed")


SWUPDATE_AUTO = 0x8000


def disable_software_update(image: str) -> str:
    """Clear Software Update's "Update software automatically" flag, as the
    9.2.1 Software Update control panel writes it when the box is unchecked
    (verified by diffing the panel's own change: 0x0000c000 -> 0x00004000)."""
    with hfsplus.Volume(image, writable=True) as vol:
        path = _system_folder(vol) + ["Preferences", "Software Update Preferences"]
        try:
            rec = vol.lookup(path)
        except FileNotFoundError:
            return "software-update-prefs-missing"
        fork = vol.read_fork(rec, "rsrc")
        for rtype, rid, _name, pos, data in hfsplus.parse_resource_fork(fork):
            if rtype != "scpp":
                continue
            if len(data) != 20:
                return "unexpected-layout"
            flags = int.from_bytes(data[0:4], "big")
            if not flags & SWUPDATE_AUTO:
                return "already-off"
            vol.write_fork_in_place(rec, "rsrc", pos, (flags & ~SWUPDATE_AUTO).to_bytes(4, "big"))
            vol.flush()
            return f"automatic-check-off (0x{flags:08x} -> 0x{flags & ~SWUPDATE_AUTO:08x})"
        return "scpp-resource-missing"


def _config_of(resources) -> dict:
    return {(t, i): d for t, i, *_rest, d in resources if t in TCPIP_CONFIG_TYPES}


def classify_tcpip(fork: bytes) -> str:
    """"ethernet" / "airport" for RetroBoot's own DHCP configurations, else "user"."""
    try:
        cfg = _config_of(hfsplus.parse_resource_fork(fork))
    except (hfsplus.HFSError, ValueError, IndexError, struct.error):
        return "user"
    for port, template in TCPIP_TEMPLATES.items():
        if cfg == _config_of(template):
            return port
    return "user"


def _tcpip_state(image: str) -> tuple[list[str], str]:
    """(Preferences folder path, "missing" | "empty" | "ethernet" | "airport" | "user")."""
    with hfsplus.Volume(image) as vol:
        prefs = _system_folder(vol) + ["Preferences"]
        try:
            rec = vol.lookup(prefs + [TCPIP_NAME])
        except FileNotFoundError:
            return prefs, "missing"
        if rec.fork("rsrc").logical_size == 0:
            return prefs, "empty"
        return prefs, classify_tcpip(vol.read_fork(rec, "rsrc"))


def airport_software_present(image: str) -> bool:
    """The original AirPort driver is installed in the blessed System Folder."""
    with hfsplus.Volume(image) as vol:
        try:
            vol.lookup(_system_folder(vol) + ["Extensions", AIRPORT_DRIVER])
            return True
        except FileNotFoundError:
            return False


ATI_DRIVER = "ATI Driver Update"


def ati_software_present(image: str) -> bool:
    """ATI's display driver (Mac OS 9.2.1's "ATI Driver Update") is in the blessed
    System Folder's Extensions: the emulated Radeon (research/gfx) needs it."""
    with hfsplus.Volume(image) as vol:
        try:
            vol.lookup(_system_folder(vol) + ["Extensions", ATI_DRIVER])
            return True
        except FileNotFoundError:
            return False


def install_tcpip_dhcp(image: str, port: str = "ethernet") -> tuple[str, str]:
    """Make TCP/IP use ``port`` by DHCP when the configuration is RetroBoot's to change.

    Returns (action, resulting port: "ethernet" | "airport" | "user")."""
    if port not in NETWORK_PORTS:
        raise PrepError(f"unknown network port {port!r}")
    prefs, state = _tcpip_state(image)
    if state == "user":
        return "existing-configuration-kept", "user"
    if state == port:
        return f"kept-retroboot-{port}", port
    fork = hfsplus.build_resource_fork(TCPIP_TEMPLATES[port])
    tmp_name = f"rbprep-{uuid.uuid4().hex[:8]}"
    folder = os.path.join(*[_linux_name(p) for p in prefs])
    # 1. Linux creates both catalog records (replacing RetroBoot's own or an
    #    empty file); the temporary file's data fork receives the resource
    #    fork bytes so the driver allocates the blocks.
    with mounted(image) as mnt:
        target = os.path.join(mnt, folder, _linux_name(TCPIP_NAME))
        if os.path.exists(target):
            os.unlink(target)
        with open(target, "wb"):
            pass
        os.setxattr(target, FINDER_INFO_XATTR, b"prefztcp" + bytes(24))
        with open(os.path.join(mnt, folder, tmp_name), "wb") as f:
            f.write(fork)
            f.flush()
            os.fsync(f.fileno())
    # 2. Move the allocated extents from the temporary file's data fork to the
    #    target's resource fork. Only fixed-size fork descriptors change.
    with hfsplus.Volume(image, writable=True) as vol:
        target_rec = vol.lookup(prefs + [TCPIP_NAME])
        tmp_rec = vol.lookup(prefs + [tmp_name])
        moved = tmp_rec.fork("data")
        if moved.logical_size != len(fork) or moved.covered_blocks() < moved.total_blocks:
            raise PrepError("temporary fork allocation is not usable")
        if target_rec.fork("rsrc").total_blocks or target_rec.fork("data").total_blocks:
            raise PrepError("target file unexpectedly has allocated forks")
        vol.set_fork_record(target_rec, "rsrc", moved)
        vol.set_fork_record(tmp_rec, "data", hfsplus.Fork.empty())
        vol.flush()
    # 3. Linux removes the now-empty temporary record.
    with mounted(image) as mnt:
        os.unlink(os.path.join(mnt, folder, tmp_name))
    with hfsplus.Volume(image) as vol:
        rec = vol.lookup(prefs + [TCPIP_NAME])
        if vol.read_fork(rec, "rsrc") != fork or rec.type_creator != (b"pref", b"ztcp"):
            raise PrepError("TCP/IP Preferences verification failed")
    if state == "missing":
        return f"installed-{port}-dhcp", port
    if state == "empty":
        return "replaced-empty-file", port
    return f"switched-to-{port}", port


def apply(image: str, *, dhcp: bool = True, shutdown_warning_off: bool = True, sleep_never: bool = True,
          network_port: str = "ethernet", software_update_off: bool = True, appletalk_off: bool = True,
          base_station_follows_port: bool = True) -> dict:
    """Prepare a stopped guest disk. Returns a report; raises on failure.

    ``network_port`` is the adapter hostd will give the guest ("ethernet" or
    "airport").  AirPort falls back to Ethernet when the guest lacks the
    AirPort software (report: airport_software "absent")."""
    if network_port not in NETWORK_PORTS:
        raise PrepError(f"unknown network port {network_port!r}")
    report = {"image": os.path.abspath(image), "network_port": network_port}
    ok, out = fsck(image, repair=True)
    report["fsck_before"] = "ok" if ok else out.splitlines()[-1:]
    if not ok:
        raise PrepError("volume could not be repaired: " + out[-400:])
    if shutdown_warning_off:
        report["shutdown_warning"] = disable_shutdown_warning(image)
    if sleep_never:
        report["energy_saver"] = disable_guest_sleep(image)
        report["energy_saver_portable"] = disable_portable_sleep(image)
    if software_update_off:
        report["software_update"] = disable_software_update(image)
    report["airport_software"] = "present" if airport_software_present(image) else "absent"
    report["ati_software"] = "present" if ati_software_present(image) else "absent"
    port = network_port if network_port != "airport" or report["airport_software"] == "present" else "ethernet"
    if dhcp:
        report["tcpip"], report["tcpip_port"] = install_tcpip_dhcp(image, port)
    if appletalk_off:
        from . import appletalk
        report["appletalk"] = appletalk.install_inactive(image)
    if base_station_follows_port:
        from . import basestation
        report["airport_base_station"] = basestation.apply(image, port)
    ok, out = fsck(image, repair=False)
    report["fsck_after"] = "ok" if ok else out.splitlines()[-1:]
    if not ok:
        raise PrepError("volume check failed after preparation: " + out[-400:])
    return report
