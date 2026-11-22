"""68k Macs: Mac OS 7.5.5 to 8.1 on an emulated Quadra 800 (D044, LEGACY-01/02).

A Mac whose ``machine.json`` says ``"family": "quadra800"`` runs on QEMU's ``q800``
(Motorola 68040) from the 68k emulator package ``linux-m68k-rN``, installed beside the
PowerPC one at ``/usr/lib/retroboot/qemu-m68k/`` (``Paths.qemu_m68k_package``).  Power
Macs (no ``family``) are untouched: their argv, package and lifecycle stay as before.

The machine directory holds, besides ``disk.img`` (Apple partition map, HFS standard: a
68k Mac can't start up from HFS+):

* ``Quadra800.rom``: the Quadra 610/650/800 ROM, Apple code.  Never in Git or the host
  image; it travels with each Quadra Mac.  hostd refuses to start a Mac whose ROM is not
  ``ROM_SHA256`` (and guest-launcher checks it again).  A Mac without one gets the ROM
  store's copy (``RomStore``: a verified ROM kept by hash when a Quadra Mac is imported, or
  added on the web page).
* ``pram.img``: the Mac's own PRAM (startup disk, sound, mouse and screen choices), made
  from the package's ``q800-pram-seed.img`` when missing.

The launch (``build_argv``, the contract's item 2)::

    qemu-system-m68k -accel tcg,tb-size=N -M q800,fb=none,audiodev=snd0 -m <RAM MiB>
      -bios <dir>/Quadra800.rom -L <pkg>/share/qemu -drive file=<dir>/pram.img,format=raw,if=mtd
      -device nubus-virtio-mmio,romfile=<pkg>/share/qemu/classicvirtio-68k.rom
      -device nubus-qfb,width=W,height=H,depth=D -device virtio-tablet-device
      [-device virtio-9p-device,fsdev=share0,mount_tag=NAME -fsdev local,id=share0,...]
      -device scsi-hd,scsi-id=0,drive=hd0 -drive file=<disk>,format=raw,if=none,id=hd0
      -device scsi-cd,scsi-id=3,drive=cd0 -drive if=none,id=cd0,media=cdrom,readonly=on[,file=..]
      [-device scsi-cd,scsi-id=4,drive=cd1 -drive ...this computer's CD-ROM drive...]
      -nic user,model=dp83932 | -nic none   -name NAME   -audiodev alsa,id=snd0,... | none,id=snd0
      -display retroboot,socket=... -qmp ... -qmp ... -action reboot=shutdown -rtc base=localtime
      -serial file:...

The virtio card comes before the framebuffer card (NuBus slot order); qfb takes any size
up to 3840 x 2160 (its rows are padded to 4 bytes by the device itself, so the presenter's
8-pixel widths need nothing more).  None of the PowerPC pieces apply: no OpenBIOS,
``-prom-env``, NDRV, PMU (battery, brightness keys, lid sleep), AirPort, ATI, USB or icount.
Networking is the Quadra's own SONIC Ethernet on QEMU's user network, which hostd keeps away
from this computer's services exactly as for the Power Mac's (guestnet.py: the rules apply
to the guest user, whatever the card).  Sound is the Apple Sound Chip on the ALSA device
hostd picks for every launch (audio.py).

What still waits for the lab (see hostd/reports/quadra-hostd-2026-10-24.md): the SCSI
identity strings Apple's disk utilities want (``SCSI_HD_IDENTITY``, ``SCSI_CD_IDENTITY``,
QEMU's own strings until then).  A start from the CD on a disk that already has Mac OS
points the default startup device in the Mac's PRAM at the CD (``STARTUP_FROM_CD``,
``XPRAM_STARTUP``; measured with 8.1 on ClassicMac's binary, not yet with the package).
"""
from __future__ import annotations

import errno
import hashlib
import logging
import os
import re
import stat
import struct
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from .audio import qemu_audiodev
from .display import guest_geometry, guest_limits
from .machines import QUADRA800, MachineError, MachineUnavailable, safe_path
from .schema import QUADRA_RAM_CHOICES, QUADRA_RAM_DEFAULT

if TYPE_CHECKING:
    from .config import Paths
    from .machines import LaunchOptions, Machine

log = logging.getLogger("retroboot.quadra")

# --- The ROM ------------------------------------------------------------------------------
# The ROMs a 68k Mac may start with (SHA-256 -> facts).  Only hashes: the bytes are Apple's
# and are never logged, shown or shipped.
ROM_SHA256 = "05ad753fb594e656cf078023ec189e09e2a7655a780de993b75b8c51ed6b09ca"
ROMS = {ROM_SHA256: {"family": QUADRA800, "bytes": 1024 * 1024, "label": "Quadra 800 ROM",
                     "models": "Quadra 610, 650 and 800"}}
ROM_MAX_BYTES = 4 * 1024 * 1024

# --- The emulator package (linux-m68k-rN) --------------------------------------------------
PACKAGE_BIN = "bin/qemu-system-m68k"
QFB_ROM = "share/qemu/mac_qfb.rom"                 # nubus-qfb's declaration ROM (ClassicMac, original code)
VIRTIO_ROM = "share/qemu/classicvirtio-68k.rom"    # classicvirtio's 68k NuBus declaration ROM
PRAM_SEED = "share/qemu/q800-pram-seed.img"
PACKAGE_FILES = (PACKAGE_BIN, QFB_ROM, VIRTIO_ROM, PRAM_SEED)
PRAM_SIZES = (256, 512)

# --- The machine -------------------------------------------------------------------------
QFB_MAX = (3840, 2160)
DEPTHS = (1, 2, 4, 8, 16, 24, 32)
DEFAULT_DEPTH = 8              # a Mac's template (config.json "depth") may ask for another
SCSI_HD_ID, SCSI_CD_ID, SCSI_CD2_ID = 0, 3, 4
# INQUIRY identity of the emulated SCSI devices (QEMU scsi-hd/scsi-cd properties vendor,
# product, ver): empty keeps QEMU's own ("QEMU", "QEMU HARDDISK"/"QEMU CD-ROM").  Apple's HD SC
# Setup and Drive Setup initialize only drives they know; the guest workstream finds which
# strings they and the ROM's CD start need.  Change them here (validated by scsi_props).
SCSI_HD_IDENTITY: dict[str, str] = {}
SCSI_CD_IDENTITY: dict[str, str] = {}
_SCSI_LIMITS = {"vendor": 8, "product": 16, "ver": 4}
_SCSI_TEXT = re.compile(r"^[A-Za-z0-9 ._-]+$")

# Start Up from CD on a 68k Mac whose disk already has Mac OS: the ROM starts up from the
# default startup device in PRAM (Start Manager DefStartRec at XPRAM $78: two bytes, then the
# SCSI driver's reference number, -(33 + ID)), falling back to the other SCSI devices only
# when that one has no System (startup_record: measured with Mac OS 8.1).  "pram": hostd
# points the Mac's PRAM at the CD (SCSI ID 3) for that start and puts back what was there at
# the next start without the CD.  "none": leave PRAM alone (only a disk without Mac OS then
# starts up from the CD).
STARTUP_FROM_CD = "pram"
XPRAM_STARTUP = 0x78

NO_PACKAGE = ("This Mac is a 68k Mac, and this RetroBoot doesn't have its 68k emulator. Install the "
              "latest RetroBoot update, or choose another Mac in Startup Disk.")
BAD_PACKAGE = ("RetroBoot's 68k emulator files aren't valid. Install the latest RetroBoot update, or choose "
               "another Mac in Startup Disk.")
NO_ROM = ("This 68k Mac has no Quadra 800 ROM, so it can't start. Add the ROM on the web page (Storage & "
          "Media), or choose another Mac in Startup Disk.")
WRONG_ROM = ("This 68k Mac's ROM file isn't a Quadra 800 ROM RetroBoot can use, so it can't start. Add the "
             "right ROM on the web page (Storage & Media); RetroBoot then gives it to this Mac. Or choose "
             "another Mac in Startup Disk.")


class RomError(MachineError):
    """A file offered as a ROM is not one RetroBoot can use (the message says so plainly)."""


# --- Files in a machine directory -----------------------------------------------------------
# A machine directory belongs to the guest user while QEMU runs (guest-launcher), so hostd,
# as root, never follows a link there or writes through a name the guest could have made:
# files are read and written only as regular files opened without following links, and
# new ones are made under a fresh name (mkstemp, O_EXCL) and renamed into place.

def open_regular(path: Path, flags: int = os.O_RDONLY) -> int:
    """A descriptor of the regular file ``path`` (OSError for a link, FIFO, device, ...)."""
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "not a regular file", str(path))
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_regular(path: Path, limit: int) -> bytes:
    fd = open_regular(path)
    try:
        out = bytearray()
        while True:
            b = os.read(fd, 1 << 20)
            if not b:
                return bytes(out)
            out += b
            if len(out) > limit:
                raise RomError("That file is too large to be a Mac ROM.")
    finally:
        os.close(fd)


def write_new(directory: Path, dest: Path, data: bytes, mode: int) -> None:
    """``data`` at ``dest`` in ``directory``: written under a new private name, synced, then
    renamed over ``dest`` (a link there is replaced, never followed)."""
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".rb-")
    try:
        os.fchmod(fd, mode)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(tmp, dest)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def sha256_file(path: Path, limit: int = ROM_MAX_BYTES) -> str:
    """SHA-256 of a ROM-sized regular file (RomError for anything larger than ``limit``)."""
    return hashlib.sha256(read_regular(path, limit)).hexdigest()


def rom_info(sha: str | None) -> dict | None:
    return ROMS.get(sha or "")


def check_rom(path: Path) -> str:
    """The ROM's SHA-256 when it is one a 68k Mac may start with; MachineUnavailable otherwise
    (NO_ROM when there is none, WRONG_ROM for anything else, a link included)."""
    if not os.path.lexists(path):
        raise MachineUnavailable(NO_ROM)
    try:
        sha = sha256_file(Path(path))
    except (RomError, OSError):
        raise MachineUnavailable(WRONG_ROM) from None
    info = rom_info(sha)
    if info is None or info["family"] != QUADRA800:
        raise MachineUnavailable(WRONG_ROM)
    return sha


class RomStore:
    """Verified ROMs on RBDATA (``Paths.rom_store``), one file per hash (``<sha256>.rom``,
    root-only).  A Quadra Mac imported with its ROM leaves a copy here; New Blank Disk and a
    Quadra Mac imported without one (or with a wrong one) take theirs from here.  The web page
    can add one. A stored file counts only while its bytes match its name. ROMs are small;
    check their bytes every time, since an in-place rewrite can retain the same stat data."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def path(self, sha: str) -> Path:
        return self.root / f"{sha}.rom"

    def _good(self, sha: str) -> bool:
        p = self.path(sha)
        try:
            st = os.lstat(p)
        except OSError:
            return False
        try:
            ok = stat.S_ISREG(st.st_mode) and sha256_file(p) == sha
        except (OSError, RomError):
            ok = False
        if not ok:
            log.warning("ROM store: %s does not match its name; ignored", p.name)
        return ok

    def list(self) -> list[dict]:
        return [{"sha256": sha, "family": info["family"], "label": info["label"], "models": info["models"],
                 "bytes": info["bytes"]} for sha, info in ROMS.items() if self._good(sha)]

    def find(self, family: str = QUADRA800) -> Path | None:
        """A stored ROM for ``family`` whose bytes match its hash, or None."""
        for sha, info in ROMS.items():
            if info["family"] == family and self._good(sha):
                return self.path(sha)
        return None

    def available(self, family: str = QUADRA800) -> bool:
        return self.find(family) is not None

    def _prepare_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    def add_file(self, src: Path) -> dict:
        """Keep a verified copy of ``src`` (RomError when it is not a ROM RetroBoot can use),
        replacing a stored copy that no longer matches.  {"sha256", "label", "family", "added"}."""
        try:
            data = read_regular(Path(src), ROM_MAX_BYTES)
        except OSError as exc:
            raise RomError("The ROM file couldn't be read.") from exc
        sha = hashlib.sha256(data).hexdigest()
        info = rom_info(sha)
        if info is None:
            raise RomError("That file isn't a Mac ROM RetroBoot can use. RetroBoot's 68k Macs need the "
                           "Quadra 800 ROM (the same ROM as the Quadra 610 and 650).")
        added = False
        if not self._good(sha):
            self._prepare_root()
            write_new(self.root, self.path(sha), data, 0o600)
            added = True
        return {"sha256": sha, "label": info["label"], "family": info["family"], "added": added}

    def add_bytes(self, data: bytes) -> dict:
        """add_file for bytes received (a web upload): written only inside the 0700 store, 0600."""
        if len(data) > ROM_MAX_BYTES:
            raise RomError("That file is too large to be a Mac ROM.")
        self._prepare_root()
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".upload-")
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.close(fd)
            fd = -1
            return self.add_file(Path(tmp))
        finally:
            if fd >= 0:
                os.close(fd)
            Path(tmp).unlink(missing_ok=True)

    def remove(self, sha: str) -> bool:
        if sha not in ROMS:
            return False
        try:
            self.path(sha).unlink()
            return True
        except FileNotFoundError:
            return False


# --- Before a launch -----------------------------------------------------------------------
# The qfb slot's screen mode in PRAM (emulator workstream, October 24 2026): Mac OS takes its
# colour depth from there, not from the argv.  The package's seed has 0x83 (8 bits).
PRAM_DEPTH = 0x68
# The ROM's mark of an initialized PRAM, and 32-bit addressing (Memory control panel, bits 0
# and 2).  A PRAM without the mark is reset to the ROM's defaults at the next start, and those
# leave 32-bit addressing off: the Quadra then stays at the gray screen for good (guest
# workstream, October 24 2026; rbguestprep.pram68k reads the same bytes offline).
PRAM_MARK_AT, PRAM_MARK = 0x0C, b"NuMc"
# Slot D's slot PRAM record, the nubus-qfb card's (8 bytes: board ID, then the driver's mode
# byte at PRAM_DEPTH).  A PRAM made with the Quadra's built-in framebuffer has no record there;
# the ROM then initializes the card in black and white.  Such a record comes from the seed.
QFB_SLOT_AT, QFB_SLOT_LEN = 0x66, 8
PRAM_ADDRESSING, PRAM_32BIT = 0x8A, 0x05
DEPTH_CODES = {1: 0x80, 2: 0x81, 4: 0x82, 8: 0x83, 16: 0x84, 24: 0x85, 32: 0x85}
CODE_DEPTHS = {0x80: 1, 0x81: 2, 0x82: 4, 0x83: 8, 0x84: 16, 0x85: 24}


def supply_rom(machine: "Machine", roms: RomStore | None) -> str | None:
    """The store's verified ROM into the Mac's directory, replacing what is there (none, a
    wrong one, or a link).  "supplied"/"replaced", or None when the store has none."""
    src = roms.find(QUADRA800) if roms is not None else None
    if src is None:
        return None
    data = read_regular(src, ROM_MAX_BYTES)
    if hashlib.sha256(data).hexdigest() not in ROMS:
        return None
    had = os.path.lexists(machine.rom)
    write_new(machine.dir, machine.rom, data, 0o644)
    return "replaced" if had else "supplied"


def prepare(machine: "Machine", package: Path, roms: RomStore | None) -> dict:
    """With the guest stopped, before ``build_argv``: the Mac's ROM (the ROM store's copy when
    it has none or a wrong one) and its PRAM (the package's seed when it has none, with the
    template's colour depth).  Raises MachineUnavailable with a sentence for the user.  Returns
    what it did, for the log."""
    out: dict = {}
    try:
        out["rom_sha256"] = check_rom(machine.rom)
    except MachineUnavailable:
        done = supply_rom(machine, roms)
        if done is None:
            raise
        out["rom"] = done
        out["rom_sha256"] = check_rom(machine.rom)
    try:
        st = os.lstat(machine.pram)
    except FileNotFoundError:
        st = None
    if st is not None and not stat.S_ISREG(st.st_mode):
        os.unlink(machine.pram)              # a link (or anything else) is no PRAM: made again
        st = None
    if st is not None:
        if st.st_size not in PRAM_SIZES:
            raise MachineError("This Mac's PRAM file isn't valid.")
        if _pram_bytes(machine, PRAM_MARK_AT, len(PRAM_MARK)) != PRAM_MARK:
            st = None                        # never written by the ROM: made again from the seed
            out["pram"] = "reseeded"
    if st is None:
        try:
            seed = bytearray(read_regular(Path(package) / PRAM_SEED, 4096))
        except (OSError, RomError):
            raise MachineUnavailable(NO_PACKAGE) from None
        if len(seed) not in PRAM_SIZES:
            raise MachineUnavailable(BAD_PACKAGE)
        template = machine.meta.get("template")
        depth = template.get("depth") if isinstance(template, dict) else None
        if type(depth) is int and depth in DEPTH_CODES:
            seed[PRAM_DEPTH] = DEPTH_CODES[depth]     # only now: Mac OS keeps the user's later choice
        write_new(machine.dir, machine.pram, bytes(seed), 0o660)
        out.setdefault("pram", "created")
    done = ensure_qfb_slot(machine, package)
    if done:
        out["pram_qfb"] = done
    done = ensure_32bit(machine)
    if done:
        out["pram_32bit"] = done
    return out


def ensure_qfb_slot(machine: "Machine", package: Path) -> str | None:
    """Before each start: the qfb card's slot record in the Mac's PRAM.  When it isn't the card's
    (a Mac made on the built-in framebuffer, or elsewhere), the seed's record goes in, with the
    template's colour depth, so Mac OS starts in colour; a record that is the card's is the Mac's
    own (the depth chosen in Monitors) and is kept.  Returns what it did, or None."""
    try:
        seed = read_regular(Path(package) / PRAM_SEED, 4096)
    except (OSError, RomError):
        return None
    want = bytearray(seed[QFB_SLOT_AT:QFB_SLOT_AT + QFB_SLOT_LEN])
    if len(want) != QFB_SLOT_LEN or not any(want[:2]):
        return None
    try:
        fd = open_regular(machine.pram, os.O_RDWR)
    except OSError as exc:
        log.warning("PRAM screen record not checked: %s", exc)
        return None
    try:
        cur = os.pread(fd, QFB_SLOT_LEN, QFB_SLOT_AT)
        if len(cur) != QFB_SLOT_LEN or cur[:2] == want[:2]:
            return None
        template = machine.meta.get("template")
        depth = template.get("depth") if isinstance(template, dict) else None
        if type(depth) is int and depth in DEPTH_CODES:
            want[PRAM_DEPTH - QFB_SLOT_AT] = DEPTH_CODES[depth]
        os.pwrite(fd, bytes(want), QFB_SLOT_AT)
        os.fsync(fd)
        return f"set ({CODE_DEPTHS.get(want[PRAM_DEPTH - QFB_SLOT_AT], '?')}-bit)"
    except OSError as exc:
        log.warning("PRAM screen record not changed: %s", exc)
        return None
    finally:
        os.close(fd)


def _pram_bytes(machine: "Machine", offset: int, size: int) -> bytes | None:
    try:
        fd = open_regular(machine.pram)
    except OSError:
        return None
    try:
        return os.pread(fd, size, offset)
    finally:
        os.close(fd)


def ensure_32bit(machine: "Machine") -> str | None:
    """Before each start, with the guest stopped: 32-bit addressing on in the Mac's PRAM.  7.5.5's
    Memory control panel can turn it off, and the ROM then never gets past the gray screen.
    Returns what it did ("turned-on (0x.. -> 0x..)") or None.  Opened only as a regular file."""
    try:
        fd = open_regular(machine.pram, os.O_RDWR)
    except OSError as exc:
        log.warning("PRAM addressing not checked: %s", exc)
        return None
    try:
        old = os.pread(fd, 1, PRAM_ADDRESSING)
        if len(old) != 1 or (old[0] & PRAM_32BIT) == PRAM_32BIT:
            return None
        new = old[0] | PRAM_32BIT
        os.pwrite(fd, bytes([new]), PRAM_ADDRESSING)
        os.fsync(fd)
        return f"turned-on (0x{old[0]:02x} -> 0x{new:02x})"
    except OSError as exc:
        log.warning("PRAM addressing not changed: %s", exc)
        return None
    finally:
        os.close(fd)


def pram_depth(machine: "Machine") -> int | None:
    """The colour depth Mac OS will start with (the qfb slot's PRAM mode), or None."""
    try:
        fd = open_regular(machine.pram)
    except OSError:
        return None
    try:
        b = os.pread(fd, 1, PRAM_DEPTH)
    finally:
        os.close(fd)
    return CODE_DEPTHS.get(b[0]) if b else None


def startup_record(scsi_id: int, current: bytes = b"\xff\xff") -> bytes:
    """The Start Manager's default startup device for a SCSI device (XPRAM_STARTUP): the
    SCSI driver's reference number in the last two bytes.  The first two are kept from the
    current record when that names a SCSI device too (the package's seed has ``ffff``),
    else ``ffff``.  Measured on the q800 with Mac OS 8.1 (ClassicMac's binary, October 24
    2026): ``ffffffdc`` and ``0000ffdc`` start up from the CD at SCSI ID 3, ``ffffffdf``
    from the disk at ID 0, with the CD in its drive either way."""
    refnum = struct.unpack(">H", current[2:4])[0] if len(current) == 4 else 0
    head = current[:2] if 0xFFD8 <= refnum <= 0xFFDF else b"\xff\xff"      # -33..-40: SCSI IDs 0-7
    return head + struct.pack(">H", (-(33 + int(scsi_id))) & 0xFFFF)


def point_startup(machine: "Machine", cd: bool) -> str | None:
    """STARTUP_FROM_CD "pram": before a start from the CD, the Mac's PRAM names the CD as its
    startup device (what was there is kept in machine.json ``pram_startup``); before the next
    start without the CD, it gets it back.  Returns what it did ("cd", "restored") or None.
    The PRAM is opened only as a regular file, never through a link."""
    if STARTUP_FROM_CD != "pram":
        return None
    saved = machine.meta.get("pram_startup")
    if not cd and not isinstance(saved, str):
        return None
    try:
        fd = open_regular(machine.pram, os.O_RDWR)
    except OSError as exc:
        log.warning("PRAM startup device not changed: %s", exc)
        return None
    try:
        cur = os.pread(fd, 4, XPRAM_STARTUP)
        if len(cur) != 4:
            return None
        if cd:
            want = startup_record(SCSI_CD_ID, cur)
            if cur == want:
                return None
            if not isinstance(saved, str):
                machine.update_meta(pram_startup=cur.hex())
        else:
            want = bytes.fromhex(saved) if re.fullmatch(r"[0-9a-f]{8}", saved or "") else cur
        os.pwrite(fd, want, XPRAM_STARTUP)
        os.fsync(fd)
    except OSError as exc:
        log.warning("PRAM startup device not changed: %s", exc)
        return None
    finally:
        os.close(fd)
    if not cd:
        machine.drop_meta("pram_startup")
        return "restored"
    return "cd"


# --- The launch ----------------------------------------------------------------------------
def scsi_props(identity: dict) -> str:
    """``,vendor=..,product=..`` for a scsi-hd/scsi-cd device (validated)."""
    out = ""
    for key, value in identity.items():
        limit = _SCSI_LIMITS.get(key)
        if limit is None or not isinstance(value, str) or not value or len(value) > limit \
                or not _SCSI_TEXT.fullmatch(value):
            raise MachineError(f"Invalid SCSI {key}")
        out += f",{key}={value}"
    return out


def depth_of(template) -> int:
    d = template.get("depth") if isinstance(template, dict) else None
    return d if type(d) is int and d in DEPTHS else DEFAULT_DEPTH


def in_argv(argv: list[str]) -> bool:
    """A session's argv (argv.json) starts the 68k emulator."""
    return bool(argv) and os.path.basename(str(argv[0])) == "qemu-system-m68k"


def package_present(paths: "Paths") -> bool:
    return (Path(paths.qemu_m68k_package) / PACKAGE_BIN).exists()


def build_argv(machine: "Machine", settings: dict, paths: "Paths", display: dict | None,
               options: "LaunchOptions") -> list[str]:
    """QEMU q800 arguments for a 68k Mac (structured argv only; every path absolute and
    checked, as for the Power Mac)."""
    root = Path(paths.qemu_m68k_package)
    if not (root / PACKAGE_BIN).exists():
        raise MachineUnavailable(NO_PACKAGE)
    package = safe_path(root, "68k emulator package", "dir")
    share = Path(package) / "share/qemu"
    qemu = safe_path(Path(package) / PACKAGE_BIN, "qemu")
    virtio_rom = safe_path(Path(package) / VIRTIO_ROM, "classicvirtio-68k.rom")
    safe_path(Path(package) / QFB_ROM, "mac_qfb.rom")
    check_rom(machine.rom)                   # MachineUnavailable: no ROM, or not the right one
    rom = safe_path(machine.rom, "ROM")
    pram = safe_path(machine.pram, "PRAM")
    disk = safe_path(machine.disk, "disk")
    ram = settings.get("machine.quadra_ram_mb", QUADRA_RAM_DEFAULT)
    if type(ram) is not int or ram not in QUADRA_RAM_CHOICES:
        raise MachineError("Unsupported memory size for a 68k Mac")
    width, height = guest_geometry(display, settings["display.guest_size"], settings.get("display.pixel_scale", "auto"))
    lo, hi = guest_limits(width, height)
    if not (lo[0] <= width <= min(hi[0], QFB_MAX[0]) and lo[1] <= height <= min(hi[1], QFB_MAX[1])):
        raise MachineError("Display dimensions outside the 68k Mac's limits")
    # The depth Mac OS starts with is PRAM's (prepare() gave a new PRAM the template's); the
    # boot framebuffer matches it.
    depth = pram_depth(machine) or depth_of(machine.meta.get("template"))
    serial = safe_path(paths.serial_log, "serial log", "output")
    audio_on = bool(settings["audio.enabled"]) and options.audio_capable
    audio = qemu_audiodev(options.audio_device) if audio_on else "none,id=snd0"
    boot_cd = options.boot_cd is not None
    sharing = bool(settings["share.enabled"]) and not boot_cd
    tag = settings["share.volume_name"]
    if not re.fullmatch(r"[A-Za-z0-9 _-]{1,27}", tag):
        raise MachineError("Invalid classic volume name")
    name = re.sub(r"[^A-Za-z0-9 ._-]", "", machine.name)[:40] or machine.id
    tb = options.tb_size_mb
    if type(tb) is not int or tb not in (64, 128, 256, 512):
        raise MachineError("Unsupported translation buffer size")
    args = [qemu, "-accel", f"tcg,tb-size={tb}", "-M", "q800,fb=none,audiodev=snd0", "-m", str(ram),
            "-bios", rom, "-L", str(share),
            "-drive", f"file={pram},format=raw,if=mtd",
            # NuBus slot order: the virtio card (tablet, shared folder) before the framebuffer card.
            "-device", f"nubus-virtio-mmio,romfile={virtio_rom}",
            "-device", f"nubus-qfb,width={width},height={height},depth={depth}",
            "-device", "virtio-tablet-device"]
    if sharing:
        share_dir = safe_path(paths.share, "share path", "dir")
        args += ["-device", f"virtio-9p-device,fsdev=share0,mount_tag={tag}",
                 "-fsdev", f"local,id=share0,security_model=none,path={share_dir}"]
    args += ["-device", f"scsi-hd,scsi-id={SCSI_HD_ID},drive=hd0{scsi_props(SCSI_HD_IDENTITY)}",
             "-drive", f"file={disk},format=raw,if=none,id=hd0"]

    def cd_medium(path: Path, drive: bool, label: str) -> str:
        if drive:   # this computer's CD-ROM drive: QEMU locks its door and ejects for Mac OS
            return f",file.driver=host_cdrom,file.filename={safe_path(path, label, 'device')},format=raw"
        return ",file=" + safe_path(path, label) + ",format=raw"
    cd = "if=none,id=cd0,media=cdrom,readonly=on"
    if boot_cd:
        cd += cd_medium(options.boot_cd, options.boot_cd_drive, "cd")
    elif options.cd_image is not None:
        cd += cd_medium(options.cd_image, False, "cd image")
    args += ["-device", f"scsi-cd,scsi-id={SCSI_CD_ID},drive=cd0{scsi_props(SCSI_CD_IDENTITY)}", "-drive", cd]
    if options.cd_drive is not None and not (boot_cd and options.boot_cd_drive):
        # The disc in this computer's CD-ROM drive, in the second CD-ROM drive.
        args += ["-device", f"scsi-cd,scsi-id={SCSI_CD2_ID},drive=cd1{scsi_props(SCSI_CD_IDENTITY)}",
                 "-drive", "if=none,id=cd1,media=cdrom,readonly=on" + cd_medium(options.cd_drive, True, "cd drive")]
    args += ["-nic", "user,model=dp83932" if settings.get("network.guest", "nat") == "nat" else "none",
             "-name", name, "-audiodev", audio]
    if options.display_backend == "retroboot":
        args += ["-display", f"retroboot,socket={safe_path(paths.display_sock, 'display socket', 'output')}"]
    elif options.display_backend == "vnc":
        args += ["-display", "none", "-vnc", f"unix:{safe_path(paths.vnc_sock, 'vnc socket', 'output')}"]
    else:
        raise MachineError("Unsupported display backend")
    args += ["-qmp", f"unix:{safe_path(paths.qmp_control, 'qmp control', 'output')},server=on,wait=off",
             "-qmp", f"unix:{safe_path(paths.qmp_events, 'qmp events', 'output')},server=on,wait=off"]
    if options.lab_qmp is not None:
        args += ["-qmp", f"unix:{safe_path(options.lab_qmp, 'lab qmp', 'output')},server=on,wait=off"]
    args += ["-action", "reboot=shutdown", "-rtc", "base=localtime", "-serial", f"file:{serial}"]
    return args
