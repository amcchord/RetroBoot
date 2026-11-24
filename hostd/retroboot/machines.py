"""Machine registry and QEMU argument construction.

Machines live under ``/var/lib/retroboot/machines/<id>/`` with ``machine.json``
and a sparse raw ``disk.img``; ``state.json`` records the last exit so
checkpoints and disk replacement only run after a verified clean shutdown.

``build_argv`` ports the validation rules of ``emulator/scripts/machine.py``
(B's qualified Power Mac contract) onto appliance settings: structured argv
arrays only, absolute validated paths without commas/newlines, manifest-
verified package assets, no arbitrary extra arguments.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .audio import qemu_audiodev, screamer_args
from .config import Paths
from .display import GUEST_MAX, GUEST_MIN, guest_geometry, guest_limits, suggested_scale  # noqa: F401 (re-exported)
from .schema import MACHINE_ID_RE
from .util import atomic_write_json, read_json, wall

CRITICAL_PACKAGE_FILES = ("bin/qemu-system-ppc", "share/qemu/openbios-ppc",
                          "share/qemu/qemu_vga.ndrv", "share/qemu/ndrvloader")

log = logging.getLogger("retroboot.machines")

# Removing a machine (MachineRegistry.trash/purge): the machine directory, then its
# checkpoints, are first renamed to ``.trash-<id>-<stamp>`` beside them, then deleted.
# Dot names are never listed, so the rename of the machine directory is the commit
# point; recover_removals() finishes whatever a power cut interrupted at the next start.
TRASH_PREFIX = ".trash-"
STAGING_PREFIX = ".staging-"      # catalog.install_machine's extraction directory
UPLOAD_PREFIX = "upload-"         # web upload of a machine archive (downloads/)

class MachineError(ValueError):
    pass


class MachineUnavailable(MachineError):
    """A Mac that can't start on this computer as it is (its emulator package or its ROM is
    missing): the message is a plain sentence for the panel and the web."""


# --- Machine families (D044) ------------------------------------------------
# machine.json "family": the emulated Mac.  A machine without it is a Power Mac G4
# (QEMU mac99, qemu-system-ppc) exactly as before; "quadra800" is a Quadra 800
# (QEMU q800, qemu-system-m68k, quadra.py) with its own ROM and PRAM files.
POWERMAC = "powermac"
QUADRA800 = "quadra800"
FAMILIES = (POWERMAC, QUADRA800)
CLASSIC = "classic"
OSX_PROFILES = ("macosx-10.2", "macosx-10.3", "macosx-10.4")
GUEST_OS_CHOICES = {CLASSIC: "Mac OS 8.5–9.2.2", "macosx-10.2": "Mac OS X 10.2 Jaguar",
                    "macosx-10.3": "Mac OS X 10.3 Panther", "macosx-10.4": "Mac OS X 10.4 Tiger"}
# Catalog and template (ClassicMac config.json) machineFamily values -> family.
OSX_TEMPLATE_FAMILY = "powerMacOSX"
OSX_CATALOG_FAMILY = "powermac-osx"   # capability-filtered catalog kind; machine family stays powermac
TEMPLATE_FAMILIES = {"powerMacG4": POWERMAC, OSX_TEMPLATE_FAMILY: POWERMAC, "quadra800": QUADRA800}
FAMILY_LABELS = {POWERMAC: "PowerPC", QUADRA800: "68k"}
# What each family runs, for New Blank Disk (panel and web).
FAMILY_CHOICES = {POWERMAC: "PowerPC: Mac OS 8.5–9.2.2", QUADRA800: "68k Quadra: System 7.1–Mac OS 8.1"}
ROM_FILE = "Quadra800.rom"           # a Quadra Mac's ROM and PRAM, in its machine directory
PRAM_FILE = "pram.img"
_FILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def family_of_template(template) -> str | None:
    """The family a template's (config.json) machineFamily names; POWERMAC when it names
    none; None for a family RetroBoot does not know (or a value that is no name at all)."""
    if not isinstance(template, dict) or "machineFamily" not in template:
        return POWERMAC
    fam = template.get("machineFamily")
    return TEMPLATE_FAMILIES.get(fam) if isinstance(fam, str) else None


# machine.json schema_version: 1 for Power Macs, as always; 2 for 68k Macs, so a RetroBoot
# from before 68k Macs (Go Back to the Previous Version), which reads only 1, leaves them out
# instead of starting them as Power Macs. Schema 3 is OS X (also hidden from
# pre-OS-X versions); this hostd reads all three.
SCHEMA_VERSIONS = (1, 2, 3)
QUADRA_SCHEMA = 2
OSX_SCHEMA = 3


def guest_os_of_template(template) -> str:
    """An explicit OS profile: never infer OS X from a display name or download URL.

    Older exports omit guestOS and retain the original classic launch route. A new
    value must fail closed instead of silently installing classic startup extensions.
    """
    value = template.get("guestOS", CLASSIC) if isinstance(template, dict) else CLASSIC
    if not isinstance(value, str) or value not in GUEST_OS_CHOICES:
        raise MachineError("This Mac's operating system is not supported by this RetroBoot.")
    tag = template.get("machineFamily", "powerMacG4") if isinstance(template, dict) else "powerMacG4"
    if (value in OSX_PROFILES) != (tag == OSX_TEMPLATE_FAMILY):
        raise MachineError("Mac OS X needs its own machine type and operating-system version.")
    if value != CLASSIC and family_of_template(template) != POWERMAC:
        raise MachineError("Mac OS X needs a PowerPC Mac.")
    return value


def family_fields(template) -> dict:
    """machine.json fields for a new Mac made from ``template`` (catalog import, the
    installer, image builds): nothing for a Power Mac (machine.json stays as before)."""
    if family_of_template(template) == QUADRA800:
        guest_os_of_template(template)
        return {"schema_version": QUADRA_SCHEMA, "family": QUADRA800, "rom": ROM_FILE, "pram": PRAM_FILE}
    profile = guest_os_of_template(template)
    if profile in OSX_PROFILES:
        # Versions before OS X support read only schemas 1/2. Go Back must hide an
        # OS X Mac rather than start it with classic drivers and disk preparation.
        return {"schema_version": OSX_SCHEMA, "family": POWERMAC, "guest_os": profile, "nvram": "nvram.img"}
    return {}


def direct_framebuffer(template: Any) -> bool:
    """A machine may opt in to scanning direct framebuffer writes each frame.

    Some classic games update screen memory without marking its pages dirty.
    The compatibility option belongs to the imported machine, not the host's
    display settings, and is off for every existing machine.
    """
    if not isinstance(template, dict) or "directFramebuffer" not in template:
        return False
    value = template["directFramebuffer"]
    if type(value) is not bool:
        raise MachineError("The Mac's display compatibility option must be true or false.")
    return value


def safe_path(value: str | Path, label: str, kind: str = "file") -> str:
    s = str(value)
    p = Path(s)
    if not p.is_absolute() or any(c in s for c in (",", "\n", "\r", "\0")):
        raise MachineError(f"{label} requires an absolute path without commas or newlines")
    if kind == "file" and not p.is_file():
        raise MachineError(f"Missing file: {label}")
    if kind == "dir" and not p.is_dir():
        raise MachineError(f"Missing directory: {label}")
    if kind == "device" and not p.exists():
        raise MachineError(f"Missing drive: {label}")
    return str(p.resolve()) if kind in ("file", "dir") else s


# --- Package --------------------------------------------------------------

def sha256_file(path: Path, chunk: int = 4 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def verify_package(root: Path, files: tuple[str, ...] = CRITICAL_PACKAGE_FILES) -> dict:
    """Verify critical assets against manifest.json; returns {name: sha256}."""
    root = Path(root)
    manifest_path = root / "manifest.json"
    try:
        manifest = read_json(manifest_path, 16 * 1024 * 1024)
    except (OSError, ValueError) as exc:
        raise MachineError(f"Emulator package manifest unreadable: {exc}") from exc
    out = {}
    for name in files:
        p = root / name
        if not p.is_file():
            raise MachineError(f"Emulator package is missing {name}")
        expected = manifest.get("files", {}).get(name, {}).get("sha256")
        actual = sha256_file(p)
        if not expected or actual != expected:
            raise MachineError(f"Package hash mismatch: {name}")
        out[name] = actual
    return out


NDRV_FILE = "qemu_vga.ndrv"


def verify_ndrv_overlay(directory: Path | None) -> dict | None:
    """The RetroBoot video driver variant (emulator/ndrv), installed beside the
    emulator package instead of over it, so the package manifest stays exactly
    as built.  ``<dir>/qemu_vga.ndrv`` must match ``<dir>/manifest.json``
    ``sha256``; QEMU finds it first because hostd passes ``-L <dir>`` before the
    package's ``-L``.  Returns None when no variant is installed."""
    if directory is None:
        return None
    d = Path(directory)
    f = d / NDRV_FILE
    if not f.exists():
        return None
    try:
        manifest = read_json(d / "manifest.json", 65536)
    except (OSError, ValueError) as exc:
        raise MachineError(f"Video driver manifest unreadable: {exc}") from exc
    expected = manifest.get("sha256")
    actual = sha256_file(f)
    if not expected or actual != expected:
        raise MachineError(f"Video driver hash mismatch: {NDRV_FILE}")
    return {"dir": str(d), "file": NDRV_FILE, "sha256": actual,
            "replaces_sha256": manifest.get("pinned_unmodified_sha256"),
            "source": manifest.get("source"), "define": manifest.get("define")}


LOADER_FILE = "ndrvloader"


def verify_loader_overlay(directory: Path | None) -> dict | None:
    """The RetroBoot classicvirtio loader variant (emulator/classicvirtio: the
    shared folder's guest driver with directory caching and Finder refresh),
    installed beside the package like the video driver variant.
    ``<dir>/ndrvloader`` must match ``<dir>/manifest.json`` ``sha256``; hostd
    then loads it instead of the package's ``share/qemu/ndrvloader`` (the
    package and its manifest stay as built).  Returns None when none is
    installed."""
    if directory is None:
        return None
    d = Path(directory)
    f = d / LOADER_FILE
    if not f.exists():
        return None
    try:
        manifest = read_json(d / "manifest.json", 65536)
    except (OSError, ValueError) as exc:
        raise MachineError(f"Shared folder driver manifest unreadable: {exc}") from exc
    expected = manifest.get("sha256")
    actual = sha256_file(f)
    if not expected or actual != expected:
        raise MachineError(f"Shared folder driver hash mismatch: {LOADER_FILE}")
    return {"dir": str(d), "file": LOADER_FILE, "sha256": actual,
            "replaces_sha256": manifest.get("pinned_unmodified_sha256"),
            "source": manifest.get("source"), "features": manifest.get("features")}


class PackageCache:
    """Verifies once per (size, mtime) tuple so repeated starts stay fast.  Thread-safe:
    hostd starts the first verification early in a worker thread (hostboot report), and
    the launch's own call waits for it instead of hashing the files a second time."""

    def __init__(self, root: Path, ndrv_dir: Path | None = None, loader_dir: Path | None = None,
                 files: tuple[str, ...] = CRITICAL_PACKAGE_FILES):
        self.root = Path(root)
        self.ndrv_dir = Path(ndrv_dir) if ndrv_dir else None
        self.loader_dir = Path(loader_dir) if loader_dir else None
        self.files = tuple(files)            # the 68k package (quadra.py) has its own
        self._key: tuple | None = None
        self.hashes: dict | None = None
        self.ndrv: dict | None = None
        self.loader: dict | None = None
        self._lock = threading.Lock()

    def _stat_key(self) -> tuple:
        paths = [self.root / n for n in self.files]
        if self.ndrv_dir:
            paths += [self.ndrv_dir / NDRV_FILE, self.ndrv_dir / "manifest.json"]
        if self.loader_dir:
            paths += [self.loader_dir / LOADER_FILE, self.loader_dir / "manifest.json"]
        return tuple((str(p), *(lambda s: (s.st_size, s.st_mtime_ns))(p.stat())) for p in paths if p.exists())

    def verify(self) -> dict:
        with self._lock:
            key = self._stat_key()
            if self.hashes is None or key != self._key:
                self.hashes = verify_package(self.root, self.files)
                self.ndrv = verify_ndrv_overlay(self.ndrv_dir)
                self.loader = verify_loader_overlay(self.loader_dir)
                self._key = key
            return self.hashes

    def summary(self) -> dict:
        try:
            m = read_json(self.root / "manifest.json", 16 * 1024 * 1024)
        except (OSError, ValueError):
            return {"present": False}
        src = m.get("sources", {})
        ndrv = loader = None
        if self.ndrv_dir and (self.ndrv_dir / NDRV_FILE).exists():
            try:
                ndrv = verify_ndrv_overlay(self.ndrv_dir)
            except MachineError as exc:
                ndrv = {"error": str(exc)}
        if self.loader_dir and (self.loader_dir / LOADER_FILE).exists():
            try:
                loader = verify_loader_overlay(self.loader_dir)
            except MachineError as exc:
                loader = {"error": str(exc)}
        return {"present": True, "ndrv": ndrv, "classicvirtio": loader,
                "qemu": src.get("qemu", {}).get("tag"),
                "qemu_commit": src.get("qemu", {}).get("commit"),
                "classicmac_commit": src.get("classicmac", {}).get("commit"),
                "files": {k: v.get("sha256") for k, v in m.get("files", {}).items()}}


# --- Display policy (ARCHITECTURE 4.5): retroboot/display.py ---------------
# suggested_scale and guest_geometry (the presenter's guest target for the
# display, display.guest_size and display.pixel_scale) are re-exported here.


# --- Registry ---------------------------------------------------------------

@dataclass
class Machine:
    id: str
    dir: Path
    meta: dict

    @property
    def disk(self) -> Path:
        return self.dir / self.meta.get("disk", "disk.img")

    @property
    def name(self) -> str:
        return self.meta.get("name") or self.id

    @property
    def family(self) -> str:
        """POWERMAC (the default: machine.json without "family") or QUADRA800."""
        return self.meta.get("family") or POWERMAC

    @property
    def quadra(self) -> bool:
        return self.family == QUADRA800

    @property
    def guest_os(self) -> str:
        return self.meta.get("guest_os") or guest_os_of_template(self.meta.get("template"))

    @property
    def osx(self) -> bool:
        return self.guest_os in OSX_PROFILES

    @property
    def nvram(self) -> Path:
        return self.dir / "nvram.img"

    @property
    def rom(self) -> Path:
        """A Quadra Mac's ROM file (Quadra800.rom; Apple code, never in Git or the image)."""
        return self.dir / self.meta.get("rom", ROM_FILE)

    @property
    def pram(self) -> Path:
        """A Quadra Mac's own PRAM (startup disk, sound, mouse and screen choices)."""
        return self.dir / self.meta.get("pram", PRAM_FILE)

    @property
    def blank(self) -> bool:
        """Made as a blank disk (New Blank Disk): Mac OS is installed on it from a CD."""
        return (self.meta.get("source") or {}).get("type") == "blank"

    def startup_volume(self) -> str:
        return startup_volume(self.disk, osx=self.osx)

    def needs_cd(self) -> bool:
        """A blank disk that has no Mac OS yet: it starts up from a CD."""
        return self.blank and self.startup_volume() != "system"

    def state(self) -> dict:
        try:
            return read_json(self.dir / "state.json", 65536)
        except (OSError, ValueError):
            return {"clean": True, "last_exit": None}

    def set_state(self, **fields: Any) -> dict:
        st = self.state()
        st.update(fields)
        atomic_write_json(self.dir / "state.json", st)
        return st

    def update_meta(self, **fields: Any) -> dict:
        self.meta.update(fields)
        self._write_meta()
        return self.meta

    def drop_meta(self, *keys: str) -> dict:
        if any(k in self.meta for k in keys):
            for k in keys:
                self.meta.pop(k, None)
            self._write_meta()
        return self.meta

    def _write_meta(self) -> None:
        if self.quadra:
            self.meta["schema_version"] = QUADRA_SCHEMA     # an earlier build may have written 1
        elif self.osx:
            self.meta["schema_version"] = OSX_SCHEMA
        atomic_write_json(self.dir / "machine.json", self.meta)

    def settings(self) -> dict:
        raw = self.meta.get("settings")
        return dict(raw) if isinstance(raw, dict) else {}

    def cpu_label(self, active_cpu: str | None = None) -> str:
        if self.quadra:
            return "Motorola 68040"
        if self.osx:
            return "PowerPC G4 (7400)"
        cpu = self.settings().get("cpu") or active_cpu or "7400"
        return "PowerPC G3" if cpu == "g3" else "PowerPC G4 (7400)"

    def public(self) -> dict:
        st = self.state()
        try:
            stat = self.disk.stat()
            disk = {"bytes": stat.st_size, "allocated_bytes": stat.st_blocks * 512}
        except OSError:
            disk = None
        return {"id": self.id, "name": self.name, "os_version": self.meta.get("os_version"),
                "created": self.meta.get("created"), "source": self.meta.get("source"),
                "disk": disk, "clean": st.get("clean", True), "last_exit": st.get("last_exit"),
                "prep": self.meta.get("prep"), "blank": self.blank,
                "startup_volume": self.startup_volume() if self.blank else "system",
                "family": self.family, "family_label": FAMILY_LABELS[self.family], "guest_os": self.guest_os,
                "settings": self.settings(), "cpu_label": self.cpu_label()}


_CLASSIC_RAM_CHOICES = (256, 384, 512, 768, 896)
_OSX_RAM_CHOICES = (128, 256, 384, 512, 768, 896, 1024, 1536, 2048)
_QUADRA_RAM_CHOICES = (64, 128, 256)
_VALID_GRAPHICS = ("vga", "ati-radeon")
_VALID_ADAPTERS = ("ethernet", "airport")
_VALID_CPUS = ("7400", "g3")


def _snap_ram(val: int, choices: tuple[int, ...], minimum: int = 0) -> int:
    eligible = [c for c in choices if c >= minimum]
    if not eligible:
        return choices[-1]
    if val in eligible:
        return val
    above = [c for c in eligible if c >= val]
    return above[0] if above else eligible[-1]


def default_machine_settings(machine: Machine, *, recommended_settings: dict | None = None,
                             host_ram_mb: int | None = None, gl_available: bool = False,
                             quadra_max_mb: int | None = None) -> dict:
    """Initial per-Mac hardware settings derived from catalog recommendedSettings,
    machine.json['template'], and host RAM / OpenGL capabilities (#44)."""
    rs = dict(recommended_settings or machine.meta.get("recommended_settings") or {})
    tpl = machine.meta.get("template") if isinstance(machine.meta.get("template"), dict) else {}
    out: dict[str, Any] = {}
    if machine.osx:
        tpl_id = str((machine.meta.get("source") or {}).get("template_id") or "")
        is_deluxe = "deluxe" in machine.id.lower() or "deluxe" in machine.name.lower() or "deluxe" in tpl_id.lower()
        req_ram = rs.get("osx_ram_mb") or rs.get("ram_mb") or tpl.get("ramMB")
        req_ram = int(req_ram) if isinstance(req_ram, int) and not isinstance(req_ram, bool) else None
        floor = 256 if machine.guest_os == "macosx-10.4" else 128
        if is_deluxe or (req_ram and req_ram >= 2048):
            if host_ram_mb is None or host_ram_mb >= 3500:
                target = 2048
            elif host_ram_mb >= 1800:
                target = 1024
            else:
                target = max(floor, 512)
        elif machine.guest_os == "macosx-10.4" and (host_ram_mb is None or host_ram_mb >= 3500):
            target = max(req_ram or 0, 1024)
        else:
            target = req_ram or (512 if machine.guest_os == "macosx-10.4" else 256)
        out["osx_ram_mb"] = _snap_ram(target, _OSX_RAM_CHOICES, floor)
        want_gfx = rs.get("graphics") or tpl.get("graphics")
        if want_gfx == "ati-radeon" and gl_available:
            out["graphics"] = "ati-radeon"
        else:
            out["graphics"] = "vga"
        out["guest_adapter"] = "ethernet"
    elif machine.quadra:
        req_ram = rs.get("ram_mb") or tpl.get("ramMB")
        choices = tuple(c for c in _QUADRA_RAM_CHOICES if quadra_max_mb is None or c <= quadra_max_mb) or (64,)
        if isinstance(req_ram, int) and not isinstance(req_ram, bool) and req_ram in choices:
            out["ram_mb"] = req_ram
        else:
            out["ram_mb"] = 128 if 128 in choices and (host_ram_mb is None or host_ram_mb >= 1024) else choices[0]
    else:
        req_ram = rs.get("ram_mb") or tpl.get("ramMB")
        if isinstance(req_ram, int) and not isinstance(req_ram, bool):
            out["ram_mb"] = _snap_ram(req_ram, _CLASSIC_RAM_CHOICES)
        if rs.get("graphics") in _VALID_GRAPHICS:
            out["graphics"] = rs["graphics"] if rs["graphics"] != "ati-radeon" or gl_available else "vga"
        if rs.get("guest_adapter") in _VALID_ADAPTERS:
            out["guest_adapter"] = rs["guest_adapter"]
        if tpl.get("useG4CPU") is False:
            out["cpu"] = "g3"
        elif rs.get("cpu") in _VALID_CPUS:
            out["cpu"] = rs["cpu"]
    return out


def validate_machine_settings(machine: Machine, changes: dict, *, host_ram_mb: int | None = None,
                              quadra_max_mb: int | None = None, gl_available: bool = True) -> dict:
    """Validate and merge per-machine settings for ``machine.json['settings']``."""
    if not isinstance(changes, dict):
        raise ValueError("Settings must be a JSON object.")
    cur = machine.settings()
    allowed = {"graphics", "guest_adapter"}
    if machine.osx:
        allowed |= {"osx_ram_mb", "ram_mb"}
    elif machine.quadra:
        allowed |= {"ram_mb"}
    else:
        allowed |= {"ram_mb", "cpu"}
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"Unsupported per-machine setting(s): {', '.join(sorted(unknown))}.")
    merged = dict(cur)
    for k, v in changes.items():
        if k == "osx_ram_mb" or (machine.osx and k == "ram_mb"):
            if type(v) is not int or v not in _OSX_RAM_CHOICES:
                raise ValueError(f"Mac OS X memory must be one of {_OSX_RAM_CHOICES} MB.")
            if machine.guest_os == "macosx-10.4" and v < 256:
                raise ValueError("Mac OS X 10.4 Tiger needs at least 256 MB of memory.")
            merged["osx_ram_mb"] = v
        elif k == "ram_mb" and machine.quadra:
            choices = tuple(c for c in _QUADRA_RAM_CHOICES if quadra_max_mb is None or c <= quadra_max_mb)
            if type(v) is not int or v not in choices:
                raise ValueError(f"68k Mac memory must be one of {choices} MB.")
            merged["ram_mb"] = v
        elif k == "ram_mb":
            if type(v) is not int or v not in _CLASSIC_RAM_CHOICES:
                raise ValueError(f"Classic Mac memory must be one of {_CLASSIC_RAM_CHOICES} MB.")
            merged["ram_mb"] = v
        elif k == "graphics":
            val = "vga" if v == "standard" else v
            if val not in _VALID_GRAPHICS:
                raise ValueError("Graphics must be vga or ati-radeon.")
            if val == "ati-radeon" and not gl_available:
                raise ValueError("ATI Radeon 3D acceleration is not available on this computer.")
            if machine.quadra and val != "vga":
                raise ValueError("68k Macs use Standard VGA display.")
            merged["graphics"] = val
        elif k == "guest_adapter":
            if v not in _VALID_ADAPTERS:
                raise ValueError("Network adapter must be ethernet or airport.")
            if (machine.osx or machine.quadra) and v != "ethernet":
                raise ValueError("AirPort is only available for Classic PowerPC Mac OS.")
            merged["guest_adapter"] = v
        elif k == "cpu":
            if v not in _VALID_CPUS:
                raise ValueError("Processor must be 7400 or g3.")
            merged["cpu"] = v
    return merged


def startup_volume(disk: Path, *, osx: bool = False) -> str:
    """What a disk holds for Mac OS's startup (read-only, a few sectors):

    * ``system``: an HFS or HFS+ volume with a blessed System Folder (Mac OS starts from it);
    * ``volume``: initialized (Apple partition map or a volume) but no System Folder yet;
    * ``empty``: nothing at all, a new blank disk.

    Mac OS's installer initializes a blank disk with Drive Setup (an Apple partition map
    and an HFS+ volume inside an HFS wrapper) and blesses the System Folder it installs:
    the volume header's first Finder info word (HFS ``drFndrInfo[0]``, HFS+
    ``finderInfo[0]``) then names that folder, as OpenBIOS expects.
    """
    try:
        with open(disk, "rb") as f:
            def at(off: int, n: int) -> bytes:
                f.seek(off)
                return f.read(n)
            head = at(0, 512)
            starts = []
            if head[:2] == b"ER":
                for i in range(1, 64):
                    e = at(512 * i, 512)
                    if e[:2] != b"PM":
                        break
                    if e[48:80].split(b"\0")[0] == b"Apple_HFS":
                        starts.append(struct.unpack(">I", e[8:12])[0] * 512)
            else:
                starts.append(0)
            found = head[:2] == b"ER"
            for start in starts:
                hdr = at(start + 1024, 512)
                if hdr[:2] == b"BD" and hdr[0x7C:0x7E] == b"H+":      # HFS wrapper around HFS+
                    al_size = struct.unpack(">I", hdr[0x14:0x18])[0]
                    al_start = struct.unpack(">H", hdr[0x1C:0x1E])[0]
                    emb_start = struct.unpack(">H", hdr[0x7E:0x80])[0]
                    hdr = at(start + al_start * 512 + emb_start * al_size + 1024, 512)
                if hdr[:2] in (b"H+", b"HX"):
                    found = True
                    if struct.unpack(">I", hdr[0x50:0x54])[0] or \
                            (osx and struct.unpack(">I", hdr[0x64:0x68])[0]):
                        return "system"
                elif hdr[:2] == b"BD":
                    found = True
                    if struct.unpack(">I", hdr[0x5C:0x60])[0]:
                        return "system"
            return "volume" if found else "empty"
    except (OSError, struct.error):
        return "empty"


def validate_meta(meta: Any, expected_id: str) -> dict:
    if not isinstance(meta, dict) or type(meta.get("schema_version")) is not int \
            or meta.get("schema_version") not in SCHEMA_VERSIONS:
        raise MachineError("Unsupported machine.json")
    if meta.get("id") != expected_id or not MACHINE_ID_RE.fullmatch(expected_id):
        raise MachineError("Machine identifier mismatch")
    disk = meta.get("disk", "disk.img")
    if not isinstance(disk, str) or "/" in disk or disk.startswith("."):
        raise MachineError("Invalid disk file name")
    family = meta.get("family", POWERMAC)
    if family not in FAMILIES:
        raise MachineError("Unknown kind of Mac")
    profile = guest_os_of_template(meta.get("template"))
    stored_profile = meta.get("guest_os", profile)
    if not isinstance(stored_profile, str) or stored_profile not in GUEST_OS_CHOICES:
        raise MachineError("Unknown operating system")
    if stored_profile != profile:
        raise MachineError("The Mac's operating system does not match its settings.")
    if profile in OSX_PROFILES and (family != POWERMAC or meta["schema_version"] != OSX_SCHEMA):
        raise MachineError("Mac OS X requires the OS X machine format.")
    if meta["schema_version"] == OSX_SCHEMA and profile not in OSX_PROFILES:
        raise MachineError("The OS X machine format requires an OS X profile.")
    if "nvram" in meta and (profile not in OSX_PROFILES or meta["nvram"] != "nvram.img"):
        raise MachineError("Invalid OS X startup-memory file name")
    if profile in OSX_PROFILES:
        from .osx_identity import validate
        try:
            validate(profile, meta.get("os_version"), meta.get("os_build"))
        except ValueError as exc:
            raise MachineError(str(exc)) from None
    for key, default in (("rom", ROM_FILE), ("pram", PRAM_FILE)):
        if key not in meta:
            continue
        name = meta[key]
        if family != QUADRA800 or not isinstance(name, str) or not _FILE_NAME_RE.fullmatch(name) \
                or name in (disk, "machine.json", "state.json"):
            raise MachineError(f"Invalid {key} file name")
    direct_framebuffer(meta.get("template"))
    return meta


BLANK_MIN_BYTES = 1024 ** 3
BLANK_MAX_BYTES = 128 * 1024 ** 3      # Mac OS 9's ATA driver addresses 28-bit LBAs: 128 GiB
BLANK_SIZES_GB = (2, 4, 8, 16, 32, 64)
# A 68k Mac starts up only from HFS (standard) volumes: up to 2 GB with System 7.5.x and up
# to 4 GB with Mac OS 7.6 to 8.1 (the disk's partition map and drivers take a little of it).
QUADRA_BLANK_SIZES_GB = (1, 2, 4)
QUADRA_BLANK_DEFAULT_GB = 2
QUADRA_BLANK_NOTE = "System 7.5 can use up to 2 GB of a disk; Mac OS 7.6 to 8.1 up to 4 GB."


def blank_sizes_gb(family: str = POWERMAC) -> tuple[int, ...]:
    return QUADRA_BLANK_SIZES_GB if family == QUADRA800 else BLANK_SIZES_GB


def clean_machine_name(name: object) -> str:
    """A Mac's name as typed: printable, single-spaced, at most 63 characters."""
    if not isinstance(name, str):
        raise MachineError("Type a name for the new Mac.")
    name = " ".join("".join(c if c.isprintable() else " " if c.isspace() else "" for c in name).split())[:63].strip()
    if not name:
        raise MachineError("Type a name for the new Mac.")
    return name


def machine_slug(base: str) -> str:
    """A machine id made from a name ("Mac OS 9.2.2 Deluxe v4" -> "mac-os-9-2-2-deluxe-v4")."""
    slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")[:40] or "mac"
    if not slug[0].isalnum():
        slug = "m" + slug
    return slug


def unique_machine_id(base: str, machines: Path, checkpoints: Path | None = None,
                      taken: set[str] | frozenset = frozenset()) -> str:
    """A new machine id from ``base`` that names no machine directory in ``machines``, no
    checkpoint directory (left by an interrupted removal) and nothing in ``taken``.  Also used
    by the installer for the disk it is installing on (its own ``machines`` directory)."""
    slug = machine_slug(base)
    candidate = slug
    n = 2
    while candidate in taken or (Path(machines) / candidate).exists() or \
            (checkpoints is not None and (Path(checkpoints) / candidate).exists()):
        candidate = f"{slug[:44]}-{n}"
        n += 1
    return candidate


class MachineRegistry:
    def __init__(self, paths: Paths):
        self.paths = paths

    def list(self) -> list[Machine]:
        out = []
        root = self.paths.machines
        if not root.is_dir():
            return out
        for d in sorted(root.iterdir()):
            if d.name.startswith(".") or not d.is_dir():
                continue
            m = self.get(d.name)
            if m:
                out.append(m)
        return out

    def ids(self) -> set[str]:
        return {m.id for m in self.list()}

    def get(self, machine_id: str | None) -> Machine | None:
        if not machine_id or not MACHINE_ID_RE.fullmatch(machine_id):
            return None
        d = self.paths.machines / machine_id
        try:
            meta = validate_meta(read_json(d / "machine.json", 1024 * 1024), machine_id)
        except (OSError, ValueError):
            return None
        m = Machine(machine_id, d, meta)
        if not m.disk.is_file():
            return None
        return m

    def unique_id(self, base: str) -> str:
        return unique_machine_id(base, self.paths.machines, self.paths.checkpoints)

    def create_blank(self, name: str, size_bytes: int, family: str = POWERMAC,
                     rom: Path | None = None, guest_os: str = CLASSIC) -> Machine:
        """A new Mac with an empty sparse disk (New Blank Disk).  Mac OS is installed on it
        from a CD (Startup Disk: Start Up from CD); until then it starts up from the CD.
        Staged under a dot name and registered by one rename, like an import.  A 68k Mac
        (``family`` QUADRA800) gets a copy of ``rom``, a verified Quadra 800 ROM (the ROM
        store's, quadra.RomStore), and its PRAM from the emulator package at its first start."""
        name = clean_machine_name(name)
        if family not in FAMILIES:
            raise MachineError("Choose the kind of Mac.")
        tag = OSX_TEMPLATE_FAMILY if guest_os in OSX_PROFILES else "powerMacG4" if family == POWERMAC else family
        template = {"machineFamily": tag, "guestOS": guest_os}
        if family != POWERMAC and guest_os != CLASSIC:
            raise MachineError("Mac OS X needs a PowerPC Mac.")
        guest_os_of_template(template)
        top = max(blank_sizes_gb(family)) * 1024 ** 3 if family == QUADRA800 else BLANK_MAX_BYTES
        if type(size_bytes) is not int or not BLANK_MIN_BYTES <= size_bytes <= top \
                or size_bytes % (1024 * 1024):
            raise MachineError(f"Choose a disk size from 1 GB to {top // 1024 ** 3} GB.")
        if family == QUADRA800 and (rom is None or not Path(rom).is_file()):
            raise MachineError("A 68k Mac needs a Quadra 800 ROM, and there is none on this computer yet.")
        root = self.paths.machines
        root.mkdir(parents=True, exist_ok=True)
        mid = self.unique_id(name)
        staging = root / f"{STAGING_PREFIX}{os.getpid()}-{int(time.time() * 1000)}"
        staging.mkdir()
        try:
            with open(staging / "disk.img", "wb") as f:
                f.truncate(size_bytes)            # sparse: grows as Mac OS fills it
            meta = {"schema_version": 1, "id": mid, "name": name, "os_version": None, "disk": "disk.img",
                    "created": wall(), "source": {"type": "blank", "size_bytes": size_bytes}, "template": {}}
            if family == QUADRA800:
                meta.update(family_fields({"machineFamily": QUADRA800}), template={"machineFamily": QUADRA800})
                shutil.copyfile(rom, staging / ROM_FILE)
                os.chmod(staging / ROM_FILE, 0o644)
            elif guest_os in OSX_PROFILES:
                meta.update(family_fields(template), template=template)
                from .osx import prepare as osx_prepare
                osx_prepare(Machine(mid, staging, meta), self.paths.qemu_osx_package)
            atomic_write_json(staging / "machine.json", meta)
            atomic_write_json(staging / "state.json", {"clean": True, "last_exit": None})
            os.chmod(staging, 0o770)
            os.rename(staging, root / mid)       # fails if it exists: never replaces a machine
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        m = self.get(mid)
        if m is None:
            raise MachineError("The new disk could not be made.")
        return m

    def validate(self, machine: Machine | None) -> list[str]:
        problems = []
        if machine is None:
            return ["No Mac OS machine is installed."]
        try:
            size = machine.disk.stat().st_size
            if size < 512 or size % 512:
                problems.append("The machine's disk is not a valid raw disk.")
        except OSError:
            problems.append("The machine's disk is missing.")
        return problems

    # -- removal -----------------------------------------------------------------
    @staticmethod
    def trash_id(name: str) -> str | None:
        """The machine id in a trash name ``.trash-<id>-<ms>-<hex>`` (None: not one)."""
        if not name.startswith(TRASH_PREFIX):
            return None
        parts = name[len(TRASH_PREFIX):].rsplit("-", 2)
        if len(parts) != 3 or not MACHINE_ID_RE.fullmatch(parts[0]) or not parts[1].isdigit() \
                or not re.fullmatch(r"[0-9a-f]{6}", parts[2]):
            return None
        return parts[0]

    def trash(self, machine: Machine) -> list[Path]:
        """Take a machine out of the registry: rename its directory, then its checkpoints,
        to trash names (the first rename is the commit point; both are instant).  The
        caller deletes the returned paths (``purge``), normally at once; a power cut
        before that leaves them for ``recover_removals`` at the next start."""
        name = f"{TRASH_PREFIX}{machine.id}-{int(time.time() * 1000)}-{secrets.token_hex(3)}"
        out = [self.paths.machines / name]
        os.rename(machine.dir, out[0])
        cps = self.paths.checkpoints / machine.id
        if cps.is_dir():
            try:
                os.rename(cps, self.paths.checkpoints / name)
                out.append(self.paths.checkpoints / name)
            except OSError as exc:    # recover_removals() moves them at the next start
                log.error("checkpoints of the removed machine %s stay in place: %s", machine.id, exc)
        return out

    def recover_removals(self) -> list[Path]:
        """At hostd start, before any import or removal: finish removals a power cut
        interrupted (checkpoints still under the id of a machine already in the trash
        are moved to the trash too) and return everything left to delete: trashed
        machines and checkpoints, extraction directories of imports that never
        finished (``.staging-*``, thin or classic archives) and unfinished web uploads."""
        machines, cps, downloads = self.paths.machines, self.paths.checkpoints, self.paths.downloads
        out: list[Path] = []

        def entries(d: Path) -> list[Path]:
            try:
                return sorted(d.iterdir())
            except OSError:
                return []
        for d in entries(machines):
            mid = self.trash_id(d.name)
            if mid and (cps / mid).is_dir() and not (machines / mid).exists():
                try:
                    os.rename(cps / mid, cps / d.name)
                except OSError as exc:
                    log.error("checkpoints of the removed machine %s: %s", mid, exc)
        out += [d for d in entries(machines) if d.name.startswith((TRASH_PREFIX, STAGING_PREFIX))]
        out += [d for d in entries(cps) if d.name.startswith(TRASH_PREFIX)]
        out += [f for f in entries(downloads) if f.name.startswith(UPLOAD_PREFIX) and f.name.endswith(".tar.gz")]
        return out

    @staticmethod
    def allocated(paths: list[Path]) -> int:
        """Bytes the files under ``paths`` occupy (reflinked extents are counted for every file)."""
        total = 0
        for p in paths:
            if p.is_file() and not p.is_symlink():
                total += p.lstat().st_blocks * 512
                continue
            for root, _dirs, files in os.walk(p):
                for n in files:
                    try:
                        total += os.lstat(os.path.join(root, n)).st_blocks * 512
                    except OSError:
                        pass
        return total

    @staticmethod
    def purge(paths: list[Path]) -> int:
        """Delete trashed directories and leftover files (blocking: run in a worker thread).
        Returns how many were removed completely."""
        done = 0
        for p in paths:
            try:
                if p.is_dir() and not p.is_symlink():
                    shutil.rmtree(p)
                else:
                    p.unlink(missing_ok=True)
                done += 1
            except OSError as exc:
                log.error("could not delete %s: %s", p, exc)
        return done


# --- Argument builder -------------------------------------------------------

@dataclass
class LaunchOptions:
    display_backend: str = "retroboot"   # retroboot | vnc
    audio_capable: bool = True
    tools_media: Path | None = None
    boot_cd: Path | None = None          # start up from this CD: a CD image, or a drive (boot_cd_drive)
    boot_cd_drive: bool = False          # boot_cd is this computer's CD-ROM drive (optical.py)
    cd_image: Path | None = None         # user disc from the media library (media.py); read-only
    cd_drive: Path | None = None         # this computer's CD-ROM drive with a disc, in the second CD-ROM drive
    ndrv_dir: Path | None = None         # verified video driver variant directory (-L before the package)
    loader_dir: Path | None = None       # verified classicvirtio loader variant directory (shared folder driver)
    audio_device: str | None = None      # explicit ALSA playback device (audio.choose); None: ALSA default
    lab_qmp: Path | None = None          # LAB ONLY third QMP socket
    power_socket: Path | None = None     # battery passthrough: PMU machine fed over this socket (battery.py)
    buttons: str | None = None           # Mac OS's special-function buttons: powerbook | desktop (keys.py)
    usb_mouse: bool = False              # USB mouse for buttons and wheel next to the tablet (pointer.py)
    usb_wheel_per_report: int | None = None   # usb-mouse wheel-per-report (package feature hid-wheel-per-report)
    mac_cities: list | None = None       # the guest's Date & Time cities (macclock.choose_city), read before launch
    tb_size_mb: int = 512                # TCG translation buffer (MB); smaller on small Raspberry Pis (platform.py)
    screamer: dict | None = None         # screamer properties (audio.screamer_options; Gemini PDA: the host lead)
    osx_48k_output: bool = False         # verified 48 kHz host sink for this Tiger machine


def build_argv(machine: Machine, settings: dict, paths: Paths, display: dict | None,
               options: LaunchOptions | None = None) -> list[str]:
    options = options or LaunchOptions()
    if machine.osx:
        from .osx import build_argv as osx_argv
        return osx_argv(machine, settings, paths, display, options)
    if machine.family == QUADRA800:          # a 68k Mac: QEMU q800 (quadra.py)
        from .quadra import build_argv as quadra_argv
        return quadra_argv(machine, settings, paths, display, options)
    per = machine.settings()
    cpu = per.get("cpu") or settings["machine.cpu"]
    if cpu not in ("g3", "7400"):
        raise MachineError("Unsupported CPU")
    ram = per.get("ram_mb") if per.get("ram_mb") is not None else settings["machine.ram_mb"]
    if type(ram) is not int or not 128 <= ram <= 1024:
        raise MachineError("RAM outside prototype bounds")
    eff_settings = dict(settings)
    if per.get("graphics") in _VALID_GRAPHICS:
        eff_settings["display.graphics"] = per["graphics"]
    if per.get("guest_adapter") in _VALID_ADAPTERS:
        eff_settings["network.guest_adapter"] = per["guest_adapter"]
    package = safe_path(paths.qemu_package, "qemu package", "dir")
    bios = Path(package) / "share/qemu"
    qemu = safe_path(Path(package) / "bin/qemu-system-ppc", "qemu")
    for name in ("openbios-ppc", "qemu_vga.ndrv", "ndrvloader"):
        safe_path(bios / name, name)
    disk = safe_path(machine.disk, "disk")
    width, height = guest_geometry(display, settings["display.guest_size"], settings.get("display.pixel_scale", "auto"))
    lo, hi = guest_limits(width, height)   # a portrait Mac screen on a rotated display has its own range
    if not (lo[0] <= width <= hi[0] and lo[1] <= height <= hi[1]):
        raise MachineError("Display dimensions outside prototype bounds")
    run = paths.run
    serial = safe_path(paths.serial_log, "serial log", "output")
    audio_on = bool(settings["audio.enabled"]) and options.audio_capable
    audio = qemu_audiodev(options.audio_device) if audio_on else "none,id=snd0"
    try:
        sound_globals = screamer_args(options.screamer) if audio_on else []
    except ValueError as exc:
        raise MachineError(str(exc)) from None
    boot_cd = options.boot_cd is not None
    if boot_cd and cpu != "7400":
        raise MachineError("G3 optical compatibility route is not qualified")
    accelerated = settings["machine.boot_speed"] == "accelerated" and not boot_cd
    sharing = bool(settings["share.enabled"]) and not boot_cd
    tag = settings["share.volume_name"]
    if not re.fullmatch(r"[A-Za-z0-9 _-]{1,27}", tag):
        raise MachineError("Invalid classic volume name")
    tools = options.tools_media is not None and not boot_cd
    name = re.sub(r"[^A-Za-z0-9 ._-]", "", machine.name)[:40] or machine.id

    data_dirs = []
    if options.ndrv_dir is not None:
        ndrv = safe_path(options.ndrv_dir, "video driver directory", "dir")
        safe_path(Path(ndrv) / "qemu_vga.ndrv", "qemu_vga.ndrv (RetroBoot variant)")
        data_dirs += ["-L", ndrv]   # searched first: QEMU loads qemu_vga.ndrv from here
    data_dirs += ["-L", str(bios)]
    # Battery passthrough (battery.py): a PowerBook-style PMU with a battery bay, else Cuda.
    from .battery import machine_args as power_machine_args
    via, power_args = power_machine_args(None if boot_cd else options.power_socket)
    # The graphics card (graphics.py): std-VGA, or the experimental ATI Radeon when it is
    # requested, the package has it and Mac OS has ATI's drivers.  Never for the CD route.
    from . import graphics
    card = graphics.STANDARD if boot_cd else graphics.effective(eff_settings, machine, paths.qemu_package)
    if card != graphics.STANDARD:
        accelerated = False      # the accelerated boot's hand-off is a std-VGA feature
    # With the card Mac OS starts at ATI's own startup mode (-g) and RetroBoot Display
    # sets the Mac screen hostd chose (-prom-env retroboot-display=WxH) before the Finder;
    # with a package that has ati-edid-modes the card's monitor (EDID) offers exactly it.
    exact = card == graphics.ATI and graphics.edid_modes(paths.qemu_package)
    startup, card_args = graphics.launch_args(card, display, eff_settings, exact)
    if startup:
        width, height = startup
    tb = options.tb_size_mb
    if type(tb) is not int or tb not in (128, 256, 512):
        raise MachineError("Unsupported translation buffer size")
    args = [qemu, "-accel", f"tcg,tb-size={tb}", "-M", f"mac99,via={via},audiodev=snd0",
            "-cpu", cpu, "-m", str(ram), *data_dirs,
            *graphics.launch_device_args(card, display, eff_settings, exact,
                                         direct_framebuffer=direct_framebuffer(machine.meta.get("template"))),
            "-global", f"macio-ide.dma-completion-delay-ns={1000000 if boot_cd else 0}",
            # Disk boots: finish an in-flight PIO read before a status read, so OpenBIOS's
            # polling no longer stalls (Mac OS gray ~7 s after launch instead of ~16 s, and
            # no extension-parade stalls in qemu-display's qualification). Never for the
            # CD/installer route (its 1 ms DMA completion safeguard stays unchanged).
            *([] if boot_cd else ["-global", "macio-ide.pio-status-drain=on"]),
            "-prom-env", "output-device=ttya", "-g", f"{width}x{height}x32",
            *card_args,
            "-name", name, "-audiodev", audio, *sound_globals,
            "-action", "reboot=shutdown", "-rtc", "base=localtime",
            "-qmp", f"unix:{safe_path(paths.qmp_control, 'qmp control', 'output')},server=on,wait=off",
            "-qmp", f"unix:{safe_path(paths.qmp_events, 'qmp events', 'output')},server=on,wait=off",
            "-serial", f"file:{serial}",
            "-drive", f"file={disk},format=raw,media=disk,index=0"]
    # Mac OS's own time zone (macclock.py): the zone whose local time the clock runs on
    # (guest-launcher's TZ), applied by RetroBoot's tablet driver while Mac OS starts.
    from .macclock import prom_env as mac_location_env
    location = mac_location_env(settings.get("system.timezone") or "UTC", cities=options.mac_cities)
    if location:
        args += ["-prom-env", location]
    if options.lab_qmp is not None:
        args += ["-qmp", f"unix:{safe_path(options.lab_qmp, 'lab qmp', 'output')},server=on,wait=off"]
    args += power_args
    # The volume/mute/brightness buttons Mac OS's Button Manager takes (keys.py); never the CD route.
    from .keys import machine_args as keys_machine_args
    args += keys_machine_args(None if boot_cd else options.buttons, via)
    if options.display_backend == "retroboot":
        args += ["-display", f"retroboot,socket={safe_path(paths.display_sock, 'display socket', 'output')}"]
    elif options.display_backend == "vnc":
        args += ["-display", "none", "-vnc", f"unix:{safe_path(paths.vnc_sock, 'vnc socket', 'output')}"]
    else:
        raise MachineError("Unsupported display backend")
    if accelerated:
        args += ["-icount", "shift=4,sleep=off", "-global", "VGA.classicmac-boot-handoff=on"]
    def cd_medium(path: Path, drive: bool, label: str) -> str:
        if drive:   # this computer's CD-ROM drive: QEMU locks its door and ejects for Mac OS
            return f",file.driver=host_cdrom,file.filename={safe_path(path, label, 'device')},format=raw"
        return ",file=" + safe_path(path, label) + ",format=raw"
    user_cd = f"if=ide,index={2 if boot_cd else 3},media=cdrom,id=cd0,readonly=on"
    if boot_cd:
        user_cd += cd_medium(options.boot_cd, options.boot_cd_drive, "cd")
    elif options.cd_image is not None:
        # Mac OS 9 does not notice IDE media changes while it runs, so a disc is
        # inserted for a whole session (setting media.cd, applied at guest start).
        user_cd += cd_medium(options.cd_image, False, "cd image")
    # The second drive holds the disc in this computer's CD-ROM drive (optical.py), unless
    # Mac OS starts up from that very disc; otherwise it stays empty.
    second_cd = f"if=ide,index={3 if boot_cd else 2},media=cdrom,id=cd1,readonly=on"
    if options.cd_drive is not None and not (boot_cd and options.boot_cd_drive):
        second_cd += cd_medium(options.cd_drive, True, "cd drive")
    args += ["-drive", user_cd, "-drive", second_cd]
    if boot_cd:
        args += ["-boot", "d"]
    if tools:
        t = safe_path(options.tools_media, "tools")
        args += ["-blockdev", json.dumps({"driver": "file", "node-name": "tools-file", "filename": t, "read-only": True}),
                 "-blockdev", json.dumps({"driver": "raw", "node-name": "tools-disk", "file": "tools-file", "read-only": True}),
                 "-device", "virtio-blk-pci,drive=tools-disk"]
    # Guest network adapter (AirPort hook): Sungem Ethernet, the AirPort card, or none.
    from .airport import guest_nic_args
    args += guest_nic_args(settings, paths, machine)
    # The appliance always uses the virtio tablet (absolute pointer from the presenter).
    # The classicvirtio loader (tablet and shared-folder drivers) is RetroBoot's variant
    # when one is installed and verified (verify_loader_overlay), else the package's.
    loader = f"{bios}/ndrvloader"
    if options.loader_dir is not None:
        loader = safe_path(Path(safe_path(options.loader_dir, "shared folder driver directory", "dir")) / LOADER_FILE,
                           "ndrvloader (RetroBoot variant)")
    args += ["-device", f"loader,addr=0x4000000,file={loader}",
             "-prom-env", "boot-command=init-program go", "-device", "virtio-tablet-pci"]
    if options.usb_mouse and not boot_cd:
        # Buttons and wheel for USB Overdrive (Control-click, scrolling); the tablet
        # keeps the absolute position (pointer.py).
        from .pointer import device_arg
        args += ["-device", device_arg(options.usb_wheel_per_report)]
    if sharing:
        share = safe_path(paths.share, "share path", "dir")
        args += ["-device", f"virtio-9p-pci,fsdev=share0,mount_tag={tag}",
                 "-fsdev", f"local,id=share0,security_model=none,path={share}"]
    return args


def launch_spec(machine: Machine, argv: list[str], paths: Paths, session: str, settings: dict,
                run_as: str | None, display_backend: str, ndrv: dict | None = None,
                lab_qmp: Path | None = None, loader: dict | None = None) -> dict:
    """The argv.json document consumed by guest-launcher."""
    writable = [str(machine.dir), str(machine.disk), str(paths.share)]
    if machine.family == QUADRA800:
        # The 68k package; the launcher checks the Mac's ROM again (``rom``) and gives
        # QEMU its PRAM file to write.
        from .quadra import PACKAGE_FILES, ROM_SHA256
        package = {"root": str(paths.qemu_m68k_package), "verify": list(PACKAGE_FILES),
                   "rom": {"dir": str(machine.dir), "files": {machine.rom.name: ROM_SHA256}}}
        writable.insert(2, str(machine.pram))
    else:
        package = {"root": str(paths.qemu_package), "verify": list(CRITICAL_PACKAGE_FILES)}
        if machine.osx:
            from .osx import PACKAGE_FILES as OSX_PACKAGE_FILES
            package["root"] = str(paths.qemu_osx_package)
            package["verify"] = list(OSX_PACKAGE_FILES)
            writable.insert(2, str(machine.nvram))
    if ndrv:
        # The launcher re-verifies the variant it is about to load.
        package["overlay"] = {"dir": ndrv["dir"], "files": {ndrv["file"]: ndrv["sha256"]}}
    if loader:
        package["loader_overlay"] = {"dir": loader["dir"], "files": {loader["file"]: loader["sha256"]}}
    spec = {
        "schema": 1,
        "session": session,
        "machine": machine.id,
        "created": wall(),
        "argv": argv,
        "package": package,
        "qmp_events": str(paths.qmp_events),
        "qmp_control": str(paths.qmp_control),
        "sockets": [str(paths.qmp_events), str(paths.qmp_control)] +
                   ([str(paths.vnc_sock)] if display_backend == "vnc" else []) +
                   ([str(lab_qmp)] if lab_qmp else []),
        "serial_log": str(paths.serial_log),
        "status": str(paths.status_json),
        "lifecycle_log": str(paths.lifecycle_log),
        "stderr_log": str(paths.logs / "qemu-stderr.log"),
        "run_as": run_as,
        "writable": writable,
        "cwd": str(machine.dir),
        "boot_speed": "realtime" if machine.family == QUADRA800 or machine.osx else settings["machine.boot_speed"],
        "timezone": settings.get("system.timezone", "UTC"),
    }
    if machine.family == QUADRA800:
        spec["family"] = QUADRA800
    if machine.osx:
        spec["guest_os"] = machine.guest_os
        spec["os_build"] = machine.meta.get("os_build")
        if any(a.startswith("ati-vga,") for a in argv):
            from . import osx_graphics
            if not osx_graphics.hardware_ready():
                raise MachineUnavailable("This computer's graphics are not available for Mac OS X.")
            spec["egl_render_node"] = osx_graphics.host_gl()["render_node"]
            spec["shader_cache"] = str(paths.data / "cache" / "mesa-osx")
    return spec
