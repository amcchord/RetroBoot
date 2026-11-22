"""This computer's CD-ROM drives (a USB CD-ROM drive, or a PC's built-in one).

Linux names optical drives ``sr0``, ``sr1``…; ``/sys/block/srN`` says whether a drive
hangs off USB and who made it, and ``CDROM_DRIVE_STATUS`` on ``/dev/srN`` whether a
disc is in it.  Mac OS gets a drive that holds a disc when it starts (machines.py:
QEMU's ``host_cdrom`` in an emulated IDE CD-ROM drive): Mac OS 9 notices discs only
when it starts, so a disc inserted later needs a Mac OS restart, as for a CD image
(media.py).  Mac OS can also start up from such a disc (Startup Disk: Start Up from
CD; a blank disk with a disc in the drive), which is how Mac OS is installed on a
blank disk.

While Mac OS runs, QEMU locks the drive's door when Mac OS mounts the disc and
ejects it when Mac OS puts the disc away.  ``unlock`` releases a door a stopped
QEMU left locked, so the drive's own eject button works again.
"""
from __future__ import annotations

import errno
import fcntl
import logging
import os
import re
import stat
import struct
import time
from pathlib import Path

log = logging.getLogger("retroboot.optical")

# <linux/cdrom.h>
CDROM_LOCKDOOR = 0x5329
CDROM_DRIVE_STATUS = 0x5326
CDSL_CURRENT = 0x7FFFFFFF
CDS_NO_INFO, CDS_NO_DISC, CDS_TRAY_OPEN, CDS_DRIVE_NOT_READY, CDS_DISC_OK = range(5)
BLKGETSIZE64 = 0x80081272     # _IOR(0x12, 114, size_t) on x86-64 and arm64

NAME_RE = re.compile(r"^sr[0-9]{1,3}$")
STATUS = {CDS_NO_INFO: "unknown", CDS_NO_DISC: "empty", CDS_TRAY_OPEN: "open",
          CDS_DRIVE_NOT_READY: "not-ready", CDS_DISC_OK: "disc"}
# The setting media.cd / a CD start source naming "the disc in this computer's CD-ROM
# drive": a colon never appears in a media-library name (media.NAME_RE).
DRIVE = "drive:cd"


def _read_attr(path: Path) -> str:
    try:
        return path.read_text(errors="replace").strip()
    except OSError:
        return ""


class OpticalDrives:
    """Finds the drives under ``sysfs/block`` and probes them under ``dev``.  Probing a
    drive spins its disc up, so results are kept for ``ttl`` seconds."""

    def __init__(self, sysfs: str | Path = "/sys", dev: str | Path = "/dev", ttl: float = 4.0):
        self.sysfs = Path(sysfs)
        self.dev = Path(dev)
        self.ttl = ttl
        self._cache: tuple[float, list[dict]] | None = None

    # -- discovery ----------------------------------------------------------------
    def names(self) -> list[str]:
        try:
            return sorted((p.name for p in (self.sysfs / "block").iterdir() if NAME_RE.fullmatch(p.name)),
                          key=lambda n: int(n[2:]))
        except OSError:
            return []

    def present(self) -> bool:
        return bool(self.names())

    def list(self, fresh: bool = False) -> list[dict]:
        """Every optical drive: ``{id, path, usb, label, status, disc, volume_name, bytes}``
        (blocking: a few ms per drive, up to a second while a new disc spins up)."""
        now = time.monotonic()
        if not fresh and self._cache is not None and now - self._cache[0] < self.ttl:
            return [dict(d) for d in self._cache[1]]
        out = [self._probe(n) for n in self.names()]
        self._cache = (now, out)
        return [dict(d) for d in out]

    def first_disc(self, fresh: bool = False) -> dict | None:
        """The first drive with a disc in it (None: no drive, or every drive is empty)."""
        return next((d for d in self.list(fresh) if d["disc"]), None)

    def forget(self) -> None:
        self._cache = None

    def _probe(self, name: str) -> dict:
        block = self.sysfs / "block" / name
        try:
            usb = "/usb" in os.path.realpath(block)
        except OSError:
            usb = False
        vendor = _read_attr(block / "device" / "vendor")
        model = _read_attr(block / "device" / "model")
        path = self.dev / name
        status, volume, size = self.status(path)
        kind = "USB CD-ROM drive" if usb else "CD-ROM drive"
        return {"id": name, "path": str(path), "usb": usb, "kind": kind,
                "product": " ".join(x for x in (vendor, model) if x) or None,
                "status": status, "disc": status == "disc", "volume_name": volume, "bytes": size,
                "label": f"{volume} ({kind})" if volume else kind}

    @staticmethod
    def status(path: Path) -> tuple[str, str | None, int | None]:
        """(status, volume name, bytes) for one drive.  A path that is not a block device
        (tests, a lab fake) reads as a disc image file."""
        try:
            fd = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK)
        except OSError as exc:
            return ("unavailable" if exc.errno in (errno.ENOENT, errno.ENXIO, errno.ENODEV) else "unknown",
                    None, None)
        try:
            st = os.fstat(fd)
            if stat.S_ISBLK(st.st_mode):
                try:
                    code = fcntl.ioctl(fd, CDROM_DRIVE_STATUS, CDSL_CURRENT)
                except OSError:
                    code = CDS_NO_INFO
                state = STATUS.get(code, "unknown")
                if state != "disc":
                    return state, None, None
                try:
                    size = struct.unpack("Q", fcntl.ioctl(fd, BLKGETSIZE64, b"\0" * 8))[0]
                except OSError:
                    size = None
            elif stat.S_ISREG(st.st_mode):
                size = st.st_size
                if size == 0:
                    return "empty", None, None
            else:
                return "unknown", None, None
            try:
                head = os.pread(fd, 64 * 1024, 0)
            except OSError:
                head = b""
            return "disc", volume_name(head, fd), size
        finally:
            os.close(fd)

    # -- after QEMU ---------------------------------------------------------------
    def unlock(self, path: str | Path) -> bool:
        """Release the door a stopped emulator may have left locked (the drive's eject
        button works again).  Harmless on an unlocked drive."""
        try:
            fd = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            return False
        try:
            if not stat.S_ISBLK(os.fstat(fd).st_mode):
                return False
            fcntl.ioctl(fd, CDROM_LOCKDOOR, 0)
            return True
        except OSError as exc:
            log.info("CD-ROM door of %s not unlocked: %s", path, exc)
            return False
        finally:
            os.close(fd)
            self.forget()


def volume_name(head: bytes, fd: int | None = None) -> str | None:
    """The disc's name: the HFS volume of an Apple partition map (a Mac OS CD), a bare
    HFS volume, else the ISO 9660 volume identifier."""
    from .media import _hfs_name
    if head[:2] == b"ER" and head[512:514] == b"PM":
        n = struct.unpack(">I", head[516:520])[0]
        for i in range(1, min(n, 63) + 1):
            e = head[512 * i:512 * i + 512]
            if e[:2] != b"PM":
                break
            if e[48:80].split(b"\0")[0] == b"Apple_HFS":
                start = struct.unpack(">I", e[8:12])[0] * 512
                if fd is None:
                    return None
                try:
                    return _hfs_name(os.pread(fd, 2048, start), 0)
                except OSError:
                    return None
        return None
    if len(head) > 1026 and head[1024:1026] == b"BD":
        return _hfs_name(head, 0)
    pvd = head[0x8000:0x8800] if len(head) >= 0x8800 else b""
    if not pvd and fd is not None:
        try:
            pvd = os.pread(fd, 2048, 0x8000)
        except OSError:
            pvd = b""
    if pvd[1:6] == b"CD001":
        return pvd[40:72].decode("ascii", "replace").strip() or None
    return None
