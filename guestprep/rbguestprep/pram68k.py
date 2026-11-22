"""The Quadra 800's parameter RAM (pram.img of a 68k Mac, D044): what matters to
RetroBoot and the one setting that must never be off.

QEMU's q800 keeps the 256-byte XPRAM in the machine's ``pram.img``
(``-drive file=pram.img,format=raw,if=mtd``) and writes every change back.
Recorded on the emulated Quadra 800 with Mac OS 7.5.5 and 8.1:

* 0x0C-0x0F "NuMc": the ROM's validity mark.  A blank (all-zero) file makes
  the ROM initialize the XPRAM with its defaults, and those select 24-bit
  addressing (below): a Quadra Mac must never start from a blank pram.img;
  hostd seeds it from the emulator package instead.
* 0x8A, bits 0 and 2 (mask 0x05): 32-bit addressing (Memory control panel;
  Mac OS sets bit 6 itself).  With them clear the Quadra ROM in QEMU never
  leaves its startup code (it spins in its VIA/RTC routines at
  0x408046xx-0x408048xx with interrupts masked; the screen stays at the gray
  pattern), so a Mac whose user turned 32-bit addressing off in 7.5.5's
  Memory control panel would never start again.  ``ensure_32bit`` puts it
  back before a launch.
* 0x78-0x7B: the default startup device (Startup Disk).  SCSI ID n is driver
  reference number -(33 + n): 0xFFDF for the hard disk at ID 0, 0xFFDC for
  the CD at ID 3 (hostd's CD starts).
* 0x0A-0x0B: the disk cache (Memory > Use Defaults on 128 MB, 4096K, changed
  0x0A 0x00 -> 0x80 and 0x0B 0xCC -> 0xEC).
* 0x13: SPConfig (classic AppleTalk on the serial ports); Open Transport's
  "AppleTalk inactive" is kept in AppleTalk Preferences instead (prep68k).
* 0xF0-0xFF: rewritten by the ROM at every start (13 13 13 13 00 00 00 00
  00 01 00 00 02 EE 00 EC here); a seed may leave them zero.
* Virtual memory is not in PRAM but in the System file ('vm  ' 0, first
  long 1 = on) plus a "VM Storage" file at the volume root (prep68k.inspect
  reports it).
"""
from __future__ import annotations

import os
import stat
import tempfile

SIZE = 256
VALID = (0x0C, b"NuMc")
ADDRESSING, MASK_32BIT = 0x8A, 0x05
STARTUP = 0x78
SPCONFIG = 0x13


def _read(path: str) -> bytearray:
    # Never through a link: the Mac's directory belongs to the guest user, this may run as root.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise ValueError(f"{path}: not a regular file")
        data = bytearray(f.read(4096))
    if len(data) < SIZE:
        raise ValueError(f"{path}: {len(data)} bytes, expected at least {SIZE}")
    return data


def inspect(path: str) -> dict:
    p = _read(path)
    ref = int.from_bytes(p[STARTUP + 2:STARTUP + 4], "big")
    scsi = (-33 - (ref - 0x10000)) if 0xFFD9 <= ref <= 0xFFDF else None
    return {"valid": bytes(p[VALID[0]:VALID[0] + 4]) == VALID[1],
            "addressing_32bit": (p[ADDRESSING] & MASK_32BIT) == MASK_32BIT,
            "addressing_byte": hex(p[ADDRESSING]),
            "startup_device": {"raw": p[STARTUP:STARTUP + 4].hex(), "scsi_id": scsi},
            "spconfig": hex(p[SPCONFIG])}


def ensure_32bit(path: str) -> str:
    """Turn 32-bit addressing back on in a stopped Mac's PRAM file.  Returns
    "already-on", "turned-on (0x.. -> 0x..)" or "not-initialized" (no NuMc mark:
    the ROM rewrites everything at the next start; give it a seed instead)."""
    p = _read(path)
    if bytes(p[VALID[0]:VALID[0] + 4]) != VALID[1]:
        return "not-initialized"
    old = p[ADDRESSING]
    if (old & MASK_32BIT) == MASK_32BIT:
        return "already-on"
    p[ADDRESSING] = old | MASK_32BIT
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), prefix=".rb-")
    try:
        os.fchmod(fd, stat.S_IMODE(os.stat(path, follow_symlinks=False).st_mode))
        with os.fdopen(fd, "wb") as f:
            f.write(p)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        raise
    return f"turned-on (0x{old:02x} -> 0x{p[ADDRESSING]:02x})"
