"""RetroBoot guest preparation for a stopped 68k Mac disk (D044: Mac OS 7.5.5 to 8.1
on the emulated Quadra 800).

68k Macs start up from HFS (Mac OS Standard) volumes only, so this is the HFS
counterpart of ``prep`` (Mac OS 9, HFS+).  hostd calls it for a Mac whose
machine.json says ``family: "quadra800"`` (``guestprep.MODULE_68K``):

    prep68k.apply(image) -> report dict      # idempotent; raises PrepError
    prep68k.fsck(image, repair) -> (ok, output)
    prep68k.inspect(image) -> dict            # read-only, no root needed
    prep68k.PREP_VERSION, prep68k.PrepError

``apply`` (root; the guest stopped; run on a reflink copy and swap it in):

1. Checks and repairs the volume with ``fsck.hfs -f -y`` (Debian hfsprogs, the
   same package and binary as ``fsck.hfsplus``: Apple's fsck_hfs checks HFS
   volumes too).  A forced stop leaves the volume marked in use; the repair
   marks it unmounted cleanly again.
2. Removes the invisible root file "Shutdown Check" (type pref, creator MACS).
   General Controls creates it at every startup and deletes it at Shut Down;
   finding it at the next startup is what shows "This computer may not have
   been shut down properly".  (The volume's unmounted bit does not decide it:
   a forced-stop disk with the bit set still shows the alert.)
3. Turns General Controls' "Warn me if computer was shut down improperly" off:
   resource 'Smrt' 0 bit 0x40 of System Folder:Preferences:General Controls
   Prefs, as the 7.5.5 (0x40 -> 0x00) and 8.1 (0x42 -> 0x02) control panels
   write it.
4. Installs or updates RetroBoot Idle (guest/rbidle, INIT/RBid) in the
   blessed System Folder's Extensions.  A copy the user moved to "Extensions
   (Disabled)" (Extensions Manager) is the user's choice and is kept
   (``disabled-by-user``).
5. With Open Transport as the network stack (Mac OS 8, or 'neta' -16400 =
   'OTOn' in AppleTalk Preferences, written by 7.5.x's Network Software
   Selector), TCP/IP by DHCP on the built-in Ethernet and AppleTalk inactive,
   under the same ownership rule as the Mac OS 9 templates: RetroBoot installs
   a file only when it is missing or empty, keeps one that is exactly its own
   template, and never changes anything else.  The templates are the 7.5.5
   (Open Transport 1.1) and 8.1 (1.3) control panels' own files recorded on
   the emulated machine (guest/reports/quadra-guest-2026-10-24.md).
6. Verifies with ``fsck.hfs -f -n``.

Files are created by the Linux ``hfs`` driver (module ``hfs``; Debian's
generic kernel has it), which cannot write resource forks: as in ``prep``, a
temporary file's data fork receives the bytes and its fork descriptor moves
into the target's resource-fork descriptor (``hfs.Volume.set_fork_record``);
Finder info is written into the catalog record the same way.

Things kept in PRAM are not on the disk (virtual memory, 32-bit addressing,
the disk cache, AppleTalk's classic SPConfig, the startup device): see
``pram68k``.
"""
from __future__ import annotations

import contextlib
import os
import struct
import subprocess
import tempfile
import uuid
from pathlib import Path

from . import hfs, hfsplus
from .prep import PrepError as _PrepError

# What apply() does to a disk; bump it whenever apply() gains a change that disks
# prepared earlier lack (hostd re-runs apply() on a stopped guest whose recorded
# version is older).
#   1  fsck, Shutdown Check removed, warning off, RetroBoot Idle, OT TCP/IP DHCP,
#      AppleTalk inactive
PREP_VERSION = 1

SHUTDOWN_CHECK = "Shutdown Check"
SMRT_CHECK_DISK = 0x40
IDLE_NAME = "RetroBoot Idle"
IDLE_TYPE_CREATOR = (b"INIT", b"RBid")
EXTENSIONS, EXTENSIONS_OFF = "Extensions", "Extensions (Disabled)"
TCPIP_NAME, APPLETALK_NAME = "TCP/IP Preferences", "AppleTalk Preferences"
FLAG_HAS_BEEN_INITED = 0x0100
FLAG_HAS_NO_INITS = 0x0080

# Where apply() finds RetroBoot Idle's canonical tree (guest/rbidle/build.sh):
# $RB_RBIDLE_TREE, the image (/usr/lib/retroboot/guest/rbidle), the source
# checkout's build output.
IDLE_TREES = (os.environ.get("RB_RBIDLE_TREE") or "",
              "/usr/lib/retroboot/guest/rbidle",
              str(Path(__file__).resolve().parents[2] / "guest" / "rbidle" / "build" / "tree"))

# TCP/IP control panel "Default": Connect via Ethernet (Open Transport port
# "enet0", the Quadra's built-in SONIC through its .ENET driver "enetDRVR"),
# Configure Using DHCP Server, TCP/IP active and loaded only when needed
# ('unld' 1; the CD installers leave 3, inactive).  Recorded from the 7.5.5
# (OT 1.1) and 8.1 (OT 1.3) control panels on the emulated Quadra 800: the
# configuration resources are identical (only 'vers' 2 differs).
_IITF_68K = bytes.fromhex(
    "0001030000000000000000012a05656e65743000000000000000000000000000000000000000000000000000"
    "000000000008656e657444525652000000000000000000000000000000000000000000000000000001")
TCPIP_68K_RESOURCES = [
    ("unld", 128, "", bytes.fromhex("0001")),
    ("ulvl", 128, "", bytes.fromhex("0001")),
    ("wpos", 128, "Default window position", bytes.fromhex("00510099")),
    ("ccfg", 1, "", bytes.fromhex("0080")),
    ("cnam", 128, "Default", b""),
    ("idns", 128, "", bytes.fromhex("0000")),
    ("ihst", 128, "", bytes.fromhex("010000")),
    ("irte", 128, "", bytes.fromhex("0000")),
    ("isdm", 128, "", bytes.fromhex("0000")),
    ("cvrs", 128, "", bytes.fromhex("0001")),
    ("dtyp", 128, "", bytes.fromhex("000a")),
    ("port", 128, "", b"\x08Ethernet"),
    ("pwrd", 128, "", bytes.fromhex("00")),
    ("stng", 128, "", bytes(25)),
    ("prot", 128, "", b"tcp\x00"),
    ("iitf", 128, "", _IITF_68K),
]
TCPIP_68K_CONFIG = frozenset({"unld", "ccfg", "cnam", "cvrs", "dtyp", "idns", "ihst", "iitf", "irte", "isdm",
                              "port", "prot", "pwrd", "stng", "ulvl"})

# AppleTalk control panel "Default" after Connect via: Ethernet, Options... >
# Inactive and Save (user level Basic).  'atpf' 128 (274 bytes) carries in-memory
# pointers (bytes 12-43 and 124-147) and the last node/network hint (93-95)
# that differ at every save; byte 92 holds the state (0 inactive, 5 active on
# the installer's file: swapping in only that byte made a saved-inactive file
# active again).  Bytes 112-115 differ between OT 1.1 and 1.3, so each has its
# template.  'neta' -16400 is the stack selector (Network Software Selector:
# 'OTOn' or 'CLOn'), kept as found.
_ATPF_MASK = tuple(range(12, 44)) + (93, 94, 95) + tuple(range(124, 148))
_ATPF_755 = bytes.fromhex(
    "00000000040a00000000000007ab3a2407ab39d407ab39bc07ab39a407ab398c07ab395c07ab394407ab3934"
    "000100280000000800002710000000040000000600000064000000c80000000a000000c80000000800010044"
    "0000024a0001ff00000300c8000000fa00009c400000c350000003e8000000000000003007ab0ac0000007ab"
    "2df807ab2df007ab2decffff07ab2e00aa55aa5500010012000002480000032000000003000000010012000007"
    "d0000007d00fa0040400000001001200000242000007d00000000800000001002a000000100000023c000002"
    "3c00000bb800007530000000640000006400006c0003030430000000010010000007d000003a980008080000"
    "01000a000075300808")
_ATPF_81 = bytes.fromhex(
    "00000000040a00000000000007c6853407c684e407c684cc07c684b407c6849c07c6846c07c6845407c68444"
    "000100280000000800002710000000040000000600000064000000c80000000a000000c80000000800010044"
    "0000024a0075ffa7000300c8000000fa00009c400000c3500000100000000000000000300106a02000000106"
    "c3580106c3500106c34cffff0106c360aa55aa5500010012000002480000032000000003000000010012000007"
    "d0000007d00fa0040400000001001200000242000007d00000000800000001002a000000100000023c000002"
    "3c00000bb800007530000000640000006400006c0003030430000000010010000007d000003a980008080000"
    "01000a000075300808")


def _appletalk_template(atpf: bytes, neta_flag: int) -> list:
    return [
        ("neta", -16400, "", struct.pack(">I", neta_flag) + b"OTOn"),
        ("ulvl", 1, "", bytes.fromhex("0001")),
        ("ccfg", 1, "", bytes.fromhex("0080")),
        ("cnam", 128, "Default", b""),
        ("atpf", 128, "", atpf),
        ("port", 128, "", b"\x08Ethernet"),
        ("ptfm", 128, "", bytes.fromhex("000a")),
        ("pwrd", 128, "", bytes.fromhex("00")),
        ("lcks", 128, "", bytes.fromhex("0000")),
        ("cvrs", 128, "", bytes.fromhex("0001")),
        ("prot", 128, "", b"ddp\x00"),
        ("wpos", 1, "", bytes.fromhex("0032000a")),
    ]


APPLETALK_68K_TEMPLATES = {"7": _appletalk_template(_ATPF_755, 1), "8": _appletalk_template(_ATPF_81, 0)}
APPLETALK_68K_CONFIG = frozenset({"atpf", "ccfg", "cnam", "cvrs", "lcks", "port", "prot", "ptfm", "pwrd", "ulvl"})


class PrepError(_PrepError):
    pass


def _run(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


@contextlib.contextmanager
def loop_device(image: str):
    offset, length = hfs.locate_volume(image)
    dev = _run(["losetup", "-f", "--show", "-o", str(offset), "--sizelimit", str(length), image]).stdout.strip()
    try:
        yield dev
    finally:
        subprocess.run(["losetup", "-d", dev], check=False)


@contextlib.contextmanager
def mounted(image: str):
    """The HFS volume mounted read-write through the Linux hfs driver (Mac OS Roman
    names as UTF-8 when nls_macroman is available)."""
    # Debian builds the Mac OS Roman codepage as module mac-roman; the hfs driver's
    # request for "nls_macroman" does not find it by itself.
    subprocess.run(["modprobe", "-q", "mac-roman"], check=False, capture_output=True)
    with loop_device(image) as dev, tempfile.TemporaryDirectory(prefix="rbprep68k-") as mnt:
        base = "rw,nodev,nosuid,noexec"
        try:
            _run(["mount", "-t", "hfs", "-o", base + ",codepage=macroman,iocharset=utf8", dev, mnt])
        except subprocess.CalledProcessError:
            _run(["mount", "-t", "hfs", "-o", base, dev, mnt])
        try:
            if not os.access(mnt, os.W_OK) or "ro" in _mount_options(mnt):
                raise PrepError("the Linux hfs driver mounted the volume read-only")
            yield mnt
        finally:
            _run(["sync"])
            _run(["umount", mnt])


def _mount_options(mnt: str) -> list[str]:
    with open("/proc/mounts", encoding="utf-8") as f:
        for line in f:
            parts = line.split()
            if len(parts) > 3 and parts[1] == mnt:
                return parts[3].split(",")
    return []


def fsck(image: str, repair: bool) -> tuple[bool, str]:
    """``fsck.hfs -f -y`` (repair) or ``-f -n`` (check) on the HFS volume."""
    with loop_device(image) as dev:
        res = subprocess.run(["fsck.hfs", "-f", "-y" if repair else "-n", dev], capture_output=True, text=True)
    out = (res.stdout + res.stderr).strip()
    ok = res.returncode == 0 and ("appears to be OK" in out or "repaired successfully" in out)
    return ok, out


def _linux_name(mac_name: str) -> str:
    return mac_name.replace("/", ":")


# ---------------------------------------------------------------- read-only
def _system_folder(vol: hfs.Volume, index: dict) -> list[str]:
    blessed = vol.blessed_folder_id
    try:
        if blessed:
            return vol.path_of(blessed, index)
    except FileNotFoundError:
        pass
    rec = index.get((hfs.ROOT_FOLDER_ID, hfs.fold("System Folder")))
    if rec is None or not rec.is_folder:
        raise PrepError("no blessed System Folder")
    return ["System Folder"]


def _find(vol: hfs.Volume, index: dict, path: list[str]):
    try:
        return vol.lookup(path, index)
    except FileNotFoundError:
        return None


def _resources(vol: hfs.Volume, rec) -> list:
    try:
        return hfsplus.parse_resource_fork(vol.read_fork(rec, "rsrc"))
    except (hfs.HFSError, hfsplus.HFSError, ValueError, IndexError, struct.error):
        return []


def os_version(vol: hfs.Volume, index: dict, sysf: list[str]) -> str | None:
    """The System file's 'vers' 1 short version string ("7.5.5", "8.1")."""
    rec = _find(vol, index, sysf + ["System"])
    if rec is None:
        return None
    for rtype, rid, _n, _p, data in _resources(vol, rec):
        if rtype == "vers" and rid == 1 and len(data) > 7:
            return data[7:7 + data[6]].decode("mac_roman")
    return None


def virtual_memory(vol: hfs.Volume, index: dict, sysf: list[str]) -> str:
    """"on"/"off" from the System file's 'vm  ' 0 (first long 1 = on, as the
    Memory control panel writes it), else "unknown".  RetroBoot Idle installs
    nothing while virtual memory is on."""
    rec = _find(vol, index, sysf + ["System"])
    for rtype, rid, _n, _p, data in (_resources(vol, rec) if rec is not None else []):
        if rtype == "vm  " and rid == 0 and len(data) >= 4:
            return "on" if int.from_bytes(data[:4], "big") & 1 else "off"
    return "unknown"


def _major(version: str | None) -> str:
    return (version or "?").split(".")[0]


def _idle_state(vol: hfs.Volume, index: dict, sysf: list[str], tree_rsrc: bytes | None) -> str:
    on = _find(vol, index, sysf + [EXTENSIONS, IDLE_NAME])
    off = _find(vol, index, sysf + [EXTENSIONS_OFF, IDLE_NAME])
    if on is None:
        return "disabled-by-user" if off is not None else "absent"
    if on.type_creator != IDLE_TYPE_CREATOR:
        return "foreign-file"
    if tree_rsrc is not None and vol.read_fork(on, "rsrc") != tree_rsrc:
        return "other-version"
    return "present"


def _smrt(vol: hfs.Volume, index: dict, sysf: list[str]):
    rec = _find(vol, index, sysf + ["Preferences", "General Controls Prefs"])
    if rec is None:
        return None, None, None
    for rtype, rid, _n, pos, data in _resources(vol, rec):
        if rtype == "Smrt" and rid == 0 and len(data) == 4:
            return rec, pos, int.from_bytes(data, "big")
    return rec, None, None


def _neta(resources) -> bytes | None:
    for rtype, rid, _n, _p, data in resources:
        if rtype == "neta" and rid == -16400 and len(data) == 8:
            return data[4:8]
    return None


def classify_tcpip(fork: bytes) -> str:
    """"retroboot-dhcp" for RetroBoot's Ethernet/DHCP template, else "user"."""
    try:
        res = hfsplus.parse_resource_fork(fork)
    except (hfsplus.HFSError, ValueError, IndexError, struct.error):
        return "user"
    cfg = {(t, i): d for t, i, _n, _p, d in res if t in TCPIP_68K_CONFIG}
    want = {(t, i): d for t, i, _n, d in TCPIP_68K_RESOURCES if t in TCPIP_68K_CONFIG}
    return "retroboot-dhcp" if cfg == want else "user"


def _masked_atpf(data: bytes) -> bytes:
    if len(data) != 274:
        return data
    b = bytearray(data)
    for i in _ATPF_MASK:
        b[i] = 0
    return bytes(b)


def classify_appletalk(fork: bytes) -> str:
    """"retroboot-inactive" for one of RetroBoot's AppleTalk templates, else "user"."""
    try:
        res = hfsplus.parse_resource_fork(fork)
    except (hfsplus.HFSError, ValueError, IndexError, struct.error):
        return "user"
    cfg = {(t, i): (_masked_atpf(d) if t == "atpf" else d) for t, i, _n, _p, d in res if t in APPLETALK_68K_CONFIG}
    for template in APPLETALK_68K_TEMPLATES.values():
        want = {(t, i): (_masked_atpf(d) if t == "atpf" else d) for t, i, _n, d in template
                if t in APPLETALK_68K_CONFIG}
        if cfg == want:
            return "retroboot-inactive"
    return "user"


def _pref_state(vol: hfs.Volume, index: dict, prefs: list[str], name: str, classify) -> str:
    rec = _find(vol, index, prefs + [name])
    if rec is None:
        return "missing"
    if rec.fork("rsrc").logical_size == 0:
        return "empty"
    return classify(vol.read_fork(rec, "rsrc"))


def network_stack(vol: hfs.Volume, index: dict, sysf: list[str], version: str | None) -> str:
    """"open-transport", "classic" (MacTCP/classic AppleTalk) or "unknown"."""
    rec = _find(vol, index, sysf + ["Preferences", APPLETALK_NAME])
    neta = _neta(_resources(vol, rec)) if rec is not None else None
    if neta == b"OTOn":
        return "open-transport"
    if neta == b"CLOn":
        return "classic"
    if _major(version).isdigit() and int(_major(version)) >= 8:
        return "open-transport"           # Mac OS 8 has only Open Transport
    return "unknown"


def idle_tree(explicit: str | None = None) -> str | None:
    for p in ([explicit] if explicit else list(IDLE_TREES)):
        if p and os.path.isfile(os.path.join(p, "manifest.json")):
            return p
    if explicit:
        raise PrepError(f"RetroBoot Idle's files are not at {explicit}")
    return None


def _idle_files(tree_root: str) -> tuple[bytes, bytes]:
    """(resource fork, 16-byte FInfo) of RetroBoot Idle in a canonical tree."""
    from .install import Tree
    t = Tree(tree_root)
    files = [e for e in t.entries if e["kind"] == "file"]
    if len(files) != 1 or files[0]["path"] != [IDLE_NAME]:
        raise PrepError(f"{tree_root} is not RetroBoot Idle's tree")
    e = files[0]
    fi = bytearray(bytes.fromhex(e["finder_info"])[:16])
    if bytes(fi[:8]) != b"".join(IDLE_TYPE_CREATOR):
        raise PrepError("RetroBoot Idle's tree has another type/creator")
    flags = struct.unpack(">H", fi[8:10])[0] & ~(FLAG_HAS_BEEN_INITED | FLAG_HAS_NO_INITS)
    fi[8:10] = struct.pack(">H", flags)
    fi[10:16] = bytes(6)                  # Finder places it
    return t.rsrc(e), bytes(fi)


def inspect(image: str, tree: str | None = None) -> dict:
    """Read-only report (no root needed)."""
    tree_root = idle_tree(tree) if tree != "none" else None
    tree_rsrc = _idle_files(tree_root)[0] if tree_root else None
    with hfs.Volume(image) as vol:
        index = vol.index()
        sysf = _system_folder(vol, index)
        version = os_version(vol, index, sysf)
        _rec, _pos, smrt = _smrt(vol, index, sysf)
        prefs = sysf + ["Preferences"]
        report = {
            "filesystem": "hfs", "volume": vol.name, "volume_offset": vol.offset, "block_size": vol.block_size,
            "clean": vol.clean, "attributes": hex(vol.attributes), "system_folder": sysf, "os_version": version,
            "shutdown_check": "present" if _find(vol, index, [SHUTDOWN_CHECK]) else "absent",
            "shutdown_warning": "unknown" if smrt is None else ("on" if smrt & SMRT_CHECK_DISK else "off"),
            "retroboot_idle": _idle_state(vol, index, sysf, tree_rsrc),
            "virtual_memory": virtual_memory(vol, index, sysf),
            "network_stack": network_stack(vol, index, sysf, version),
            "tcpip": _pref_state(vol, index, prefs, TCPIP_NAME, classify_tcpip),
            "appletalk": _pref_state(vol, index, prefs, APPLETALK_NAME, classify_appletalk),
            "free_bytes": vol.free_blocks * vol.block_size,
        }
    return report


# ---------------------------------------------------------------- changes
def remove_shutdown_check(image: str) -> str:
    with hfs.Volume(image) as vol:
        rec = _find(vol, vol.index(), [SHUTDOWN_CHECK])
        if rec is None:
            return "absent"
        if not rec.is_file or rec.type_creator != (b"pref", b"MACS"):
            return "foreign-file-kept"
    with mounted(image) as mnt:
        os.unlink(os.path.join(mnt, SHUTDOWN_CHECK))
    return "removed"


def disable_shutdown_warning(image: str) -> str:
    with hfs.Volume(image, writable=True) as vol:
        index = vol.index()
        rec, pos, value = _smrt(vol, index, _system_folder(vol, index))
        if rec is None:
            return "general-controls-prefs-missing"
        if value is None:
            return "smrt-resource-missing"
        if not value & SMRT_CHECK_DISK:
            return "already-disabled"
        new = value & ~SMRT_CHECK_DISK
        vol.write_fork_in_place(rec, "rsrc", pos, new.to_bytes(4, "big"))
        vol.flush()
        return f"disabled (0x{value:08x} -> 0x{new:08x})"


def _write_file(image: str, folder: list[str], name: str, rsrc: bytes, finfo: bytes, replace: bool) -> None:
    """Create ``folder:name`` with ``rsrc`` as its resource fork (no data fork) and
    ``finfo`` (16-byte FInfo): the Linux driver creates the records and allocates the
    blocks through a temporary file's data fork, whose descriptor then moves."""
    tmp_name = f"rbprep-{uuid.uuid4().hex[:8]}"
    linux_folder = os.path.join(*[_linux_name(p) for p in folder])
    with mounted(image) as mnt:
        target = os.path.join(mnt, linux_folder, _linux_name(name))
        if os.path.exists(target):
            if not replace:
                raise PrepError(f"{':'.join(folder + [name])} already exists")
            os.unlink(target)
        with open(target, "wb"):
            pass
        with open(os.path.join(mnt, linux_folder, tmp_name), "wb") as f:
            f.write(rsrc)
            f.flush()
            os.fsync(f.fileno())
    with hfs.Volume(image, writable=True) as vol:
        index = vol.index()
        target_rec = vol.lookup(folder + [name], index)
        tmp_rec = vol.lookup(folder + [tmp_name], index)
        moved = tmp_rec.fork("data")
        need = -(-moved.physical_size // vol.block_size)
        if moved.logical_size != len(rsrc) or moved.covered_blocks() < need:
            raise PrepError("temporary fork allocation is not usable (fragmented volume)")
        if target_rec.fork("rsrc").physical_size or target_rec.fork("data").physical_size:
            raise PrepError("target file unexpectedly has allocated forks")
        vol.set_fork_record(target_rec, "rsrc", moved)
        vol.set_fork_record(tmp_rec, "data", hfs.Fork.empty())
        vol.set_finder_info(target_rec, finfo)
        vol.flush()
    with mounted(image) as mnt:
        os.unlink(os.path.join(mnt, linux_folder, tmp_name))
    with hfs.Volume(image) as vol:
        rec = vol.lookup(folder + [name])
        if vol.read_fork(rec, "rsrc") != rsrc or rec.data[hfs.F_FINFO:hfs.F_FINFO + 16] != finfo:
            raise PrepError(f"{name} verification failed")


def install_idle(image: str, tree: str | None = None) -> str:
    """Install or update RetroBoot Idle in the blessed System Folder's Extensions."""
    tree_root = idle_tree(tree)
    if tree_root is None:
        return "tree-missing"
    rsrc, finfo = _idle_files(tree_root)
    with hfs.Volume(image) as vol:
        index = vol.index()
        sysf = _system_folder(vol, index)
        state = _idle_state(vol, index, sysf, rsrc)
        if _find(vol, index, sysf + [EXTENSIONS]) is None:
            return "no-extensions-folder"
    if state in ("present", "disabled-by-user", "foreign-file"):
        return state
    _write_file(image, sysf + [EXTENSIONS], IDLE_NAME, rsrc, finfo, replace=(state == "other-version"))
    return "updated" if state == "other-version" else "installed"


def _install_pref(image: str, prefs: list[str], name: str, resources: list, finfo: bytes, state: str) -> str:
    if state in ("missing", "empty"):
        _write_file(image, prefs, name, hfsplus.build_resource_fork(resources), finfo, replace=(state == "empty"))
        return "installed" if state == "missing" else "replaced-empty-file"
    return "kept-retroboot" if state.startswith("retroboot") else "existing-configuration-kept"


def network(image: str, *, dhcp: bool = True, appletalk_off: bool = True) -> dict:
    with hfs.Volume(image) as vol:
        index = vol.index()
        sysf = _system_folder(vol, index)
        version = os_version(vol, index, sysf)
        stack = network_stack(vol, index, sysf, version)
        prefs = sysf + ["Preferences"]
        tcp = _pref_state(vol, index, prefs, TCPIP_NAME, classify_tcpip)
        at = _pref_state(vol, index, prefs, APPLETALK_NAME, classify_appletalk)
    report = {"network_stack": stack}
    if stack != "open-transport":
        return report
    if dhcp:
        report["tcpip"] = _install_pref(image, prefs, TCPIP_NAME, TCPIP_68K_RESOURCES,
                                        b"prefztcp" + bytes(8), tcp)
    if appletalk_off:
        template = APPLETALK_68K_TEMPLATES["8" if _major(version) == "8" else "7"]
        report["appletalk"] = _install_pref(image, prefs, APPLETALK_NAME, template,
                                            b"prefatdv" + bytes(8), at)
    return report


def apply(image: str, *, idle: bool = True, idle_tree_path: str | None = None, shutdown_warning_off: bool = True,
          dhcp: bool = True, appletalk_off: bool = True) -> dict:
    """Prepare a stopped 68k Mac disk.  Returns a report; raises PrepError."""
    if not hfs.is_hfs_standard(image):
        raise PrepError("no HFS (Mac OS Standard) volume found: not a 68k Mac disk")
    report = {"image": os.path.abspath(image), "prep_version": PREP_VERSION}
    ok, out = fsck(image, repair=True)
    report["fsck_before"] = "ok" if ok else out.splitlines()[-1:]
    if not ok:
        raise PrepError("volume could not be repaired: " + out[-400:])
    with hfs.Volume(image) as vol:
        index = vol.index()
        report["os_version"] = os_version(vol, index, _system_folder(vol, index))
    report["shutdown_check"] = remove_shutdown_check(image)
    if shutdown_warning_off:
        report["shutdown_warning"] = disable_shutdown_warning(image)
    if idle:
        report["retroboot_idle"] = install_idle(image, idle_tree_path)
    report.update(network(image, dhcp=dhcp, appletalk_off=appletalk_off))
    ok, out = fsck(image, repair=False)
    report["fsck_after"] = "ok" if ok else out.splitlines()[-1:]
    if not ok:
        raise PrepError("volume check failed after preparation: " + out[-400:])
    return report


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json
    import sys
    ap = argparse.ArgumentParser(prog="rbguestprep 68k", description="Prepare a stopped 68k Mac (HFS) disk.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("inspect", help="read-only report")
    i.add_argument("image")
    i.add_argument("--tree", help="RetroBoot Idle tree to compare with ('none' to skip)")
    a = sub.add_parser("apply", help="check/repair and prepare (root; guest stopped)")
    a.add_argument("image")
    a.add_argument("--tree", help="RetroBoot Idle tree (default: $RB_RBIDLE_TREE, the image, the checkout)")
    a.add_argument("--no-idle", action="store_true")
    a.add_argument("--keep-shutdown-warning", action="store_true")
    a.add_argument("--no-dhcp", action="store_true")
    a.add_argument("--keep-appletalk", action="store_true")
    r = sub.add_parser("repair", help="fsck.hfs -f -y (root; guest stopped)")
    r.add_argument("image")
    c = sub.add_parser("check", help="fsck.hfs -f -n (root)")
    c.add_argument("image")
    p = sub.add_parser("pram", help="inspect a PRAM file; --ensure-32bit turns 32-bit addressing back on")
    p.add_argument("pram")
    p.add_argument("--ensure-32bit", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "inspect":
        report = inspect(args.image, args.tree)
    elif args.cmd == "apply":
        report = apply(args.image, idle=not args.no_idle, idle_tree_path=args.tree,
                       shutdown_warning_off=not args.keep_shutdown_warning, dhcp=not args.no_dhcp,
                       appletalk_off=not args.keep_appletalk)
    elif args.cmd in ("repair", "check"):
        ok, out = fsck(args.image, repair=args.cmd == "repair")
        report = {"ok": ok, "output": out.splitlines()[-3:]}
    else:
        from . import pram68k
        report = pram68k.inspect(args.pram)
        if args.ensure_32bit:
            report["ensure_32bit"] = pram68k.ensure_32bit(args.pram)
    json.dump(report, sys.stdout, indent=2)
    print()
    return 0 if report.get("ok", True) else 1
