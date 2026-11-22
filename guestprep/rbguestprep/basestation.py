"""AirPort's Software Base Station (``AirPort AP``) off on Ethernet guests (boot time).

AirPort 2.0.4 installs ``AirPort AP`` 1.3 (type ``appe``, creator ``IPnl``) in
System Folder:Extensions: the background application behind AirPort's
Software Base Station, which lets a Mac share its connection with other
AirPort computers.  Mac OS launches it at every startup, right after the
Finder.  On RetroBoot's Ethernet machine (no AirPort card) it keeps the vCPU
busy after the Finder's menus while the desktop is still being drawn: without
it that phase is about 1.6 vCPU s shorter with SuperDeluxe v2 and 0.8 s with
the standard image (emulator/reports/boot-editions-2026-10-24.md, lab).  RetroBoot never
offers a software base station: the guest's network is QEMU's user-mode NAT,
for Ethernet and the AirPort card alike.

``apply(image, port)``:

* ``ethernet``: moves ``Extensions:AirPort AP`` to ``Extensions (Disabled)``,
  what Extensions Manager does when the item is unchecked (the user can turn
  it back on there).
* ``airport``: puts back a copy RetroBoot moved (still in ``Extensions
  (Disabled)``, no enabled copy), so the AirPort card's setup keeps AirPort's
  complete installation, as qualified.

Ownership rule: only AirPort's own file (``appe``/``IPnl``) is moved, only
between those two folders, and never when both folders hold a copy.  A copy
the user disabled stays disabled on Ethernet; switching to the AirPort card
turns it on again (documented; the guest's AirPort setup expects it).
"""
from __future__ import annotations

from pathlib import Path

from . import hfsplus

NAME = "AirPort AP"
TYPE_CREATOR = (b"appe", b"IPnl")
ENABLED, DISABLED = "Extensions", "Extensions (Disabled)"


def _record(vol: hfsplus.Volume, path: list[str]):
    try:
        return vol.lookup(path)
    except FileNotFoundError:
        return None


def state(image: str) -> str:
    """Read-only: "enabled", "disabled", "both", "absent" or "other" (a file of that
    name that is not AirPort's)."""
    from .prep import _system_folder
    with hfsplus.Volume(image) as vol:
        sysf = _system_folder(vol)
        found = {}
        for folder in (ENABLED, DISABLED):
            rec = _record(vol, sysf + [folder, NAME])
            if rec is not None and rec.is_file:
                found[folder] = (rec.data[48:52], rec.data[52:56]) == TYPE_CREATOR
    if not found:
        return "absent"
    if not all(found.values()):
        return "other"
    if len(found) == 2:
        return "both"
    return "enabled" if ENABLED in found else "disabled"


def plan(current: str, port: str) -> str | None:
    """The move apply() makes: "disable", "enable" or None."""
    if port == "ethernet" and current == "enabled":
        return "disable"
    if port == "airport" and current == "disabled":
        return "enable"
    return None


def apply(image: str, port: str) -> str:
    """Disable (Ethernet) or restore (AirPort card) the Software Base Station.  Root,
    guest stopped; the caller runs fsck afterwards.  Returns the report value:
    "disabled", "enabled", or the unchanged state ("already-disabled", "enabled-kept",
    "absent", "other", "both")."""
    from .prep import _linux_name, _system_folder, mounted
    current = state(image)
    move = plan(current, port)
    if move is None:
        return {"enabled": "enabled-kept", "disabled": "already-disabled"}.get(current, current)
    with hfsplus.Volume(image) as vol:
        sysf = _system_folder(vol)
    src_folder, dst_folder = (ENABLED, DISABLED) if move == "disable" else (DISABLED, ENABLED)
    with mounted(image) as mnt:
        root = Path(mnt).joinpath(*[_linux_name(p) for p in sysf])
        (root / dst_folder).mkdir(exist_ok=True)
        (root / src_folder / NAME).rename(root / dst_folder / NAME)
    return "disabled" if move == "disable" else "enabled"
