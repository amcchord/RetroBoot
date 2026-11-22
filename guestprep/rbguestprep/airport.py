"""AirPort 2.0.4's remembered network (``System Folder:Preferences:AirPort Prefs``).

At every start the AirPort driver initialises the card, probes it twice with
an empty desired network, and then joins the network named in its preferences,
unless the user turned AirPort off.  Without a remembered network it leaves
the card disabled: the Control Strip says "AirPort: On", nothing is checked
and Mac OS has no network until a network is picked
(research/airport/reports/airstart-2026-10-12.md).  A freshly imported base
has no ``AirPort Prefs`` file at all, so hostd names the host's network here
before each launch with the AirPort card (``hostd/retroboot/airport``).

Layout, from the files AirPort 2.0.4 writes on RetroBoot's emulated machine
(a fresh file after one join; after "Turn AirPort Off"; after a second join):
the settings are resource ``'1ap0'`` 150, 14,800 bytes, in a file of type
``pref``, creator ``1wdr`` (the AirPort Driver's).

====== ====================================================================
0x000  u16 format, 6
0x004  u16 1 when the user turned AirPort off (Control Strip, AirPort app)
0x36A  C string: the computer-to-computer network name ("AirPort Station")
0x38C  C string: the network AirPort joins at startup (the last one chosen;
       AirPort overwrites only the new name and its terminating zero)
====== ====================================================================

The other bytes the driver writes for a fresh file (``_HEADER``) are kept as
it writes them; everything else is zero.  RetroBoot changes only the network
name, and never the on/off choice.
"""
from __future__ import annotations

import sys

from . import hfsplus

PREFS_NAME = "AirPort Prefs"
TYPE_CREATOR = (b"pref", b"1wdr")
RES_TYPE, RES_ID = "1ap0", 150
SETTINGS_LEN = 14800
FORMAT = 6
OFF_AT = 0x04
IBSS_NAME_AT = 0x36A
NETWORK_AT = 0x38C
NETWORK_FIELD = 33           # an SSID of up to 32 bytes and its terminating zero
NAME_MAX = 32

# The first 30 bytes of a new settings resource as AirPort 2.0.4 writes it
# (format 6, AirPort on), identical on the 9.2.1 and 9.2.2 disks it was
# recorded from, and the default computer-to-computer network name.
_HEADER = bytes.fromhex("00060001000000000001ffffffff0003000a01000000000000000001ffff")
_IBSS_NAME = b"AirPort Station"


class LayoutError(ValueError):
    """Settings in a layout RetroBoot does not know; left alone."""


def check_name(name: bytes) -> bytes:
    name = bytes(name)
    if not 1 <= len(name) <= NAME_MAX or b"\0" in name:
        raise ValueError("network name must be 1 to 32 bytes without a zero byte")
    return name


def _cstr(data: bytes, at: int, size: int) -> bytes:
    field = data[at:at + size]
    return field.split(b"\0", 1)[0]


def parse(data: bytes) -> dict:
    """{"airport_on": bool, "network": bytes} of a ``'1ap0'`` 150 resource."""
    if len(data) != SETTINGS_LEN or int.from_bytes(data[0:2], "big") != FORMAT:
        raise LayoutError(f"unexpected AirPort settings (length {len(data)}, format {data[:2].hex()})")
    off = int.from_bytes(data[OFF_AT:OFF_AT + 2], "big")
    if off not in (0, 1):
        raise LayoutError(f"unexpected AirPort on/off value {off}")
    return {"airport_on": off == 0, "network": _cstr(data, NETWORK_AT, NETWORK_FIELD)}


def template(name: bytes) -> bytes:
    """New settings: AirPort on, joining ``name`` at startup."""
    data = bytearray(SETTINGS_LEN)
    data[:len(_HEADER)] = _HEADER
    data[IBSS_NAME_AT:IBSS_NAME_AT + len(_IBSS_NAME)] = _IBSS_NAME
    return with_network(bytes(data), name)


def with_network(data: bytes, name: bytes) -> bytes:
    """``data`` with ``name`` as the startup network; everything else unchanged."""
    name = check_name(name)
    parse(data)
    out = bytearray(data)
    out[NETWORK_AT:NETWORK_AT + NETWORK_FIELD] = name + bytes(NETWORK_FIELD - len(name))
    return bytes(out)


def _locate(vol: hfsplus.Volume):
    """(Preferences folder path, AirPort Prefs record or None) in one catalog pass."""
    from .prep import PrepError
    vh = vol._read_abs(vol.offset + 1024, 512)
    blessed = int.from_bytes(vh[0x50:0x54], "big")
    folders: dict[int, tuple[int, str]] = {}
    prefs_dirs: dict[int, int] = {}          # parent cnid -> Preferences cnid
    files: dict[int, hfsplus.CatalogRecord] = {}
    for rec in vol.records():
        if rec.is_folder:
            folders[rec.cnid] = (rec.parent_id, rec.name)
            if rec.name.casefold() == "preferences":
                prefs_dirs[rec.parent_id] = rec.cnid
        elif rec.name.casefold() == PREFS_NAME.casefold():
            files[rec.parent_id] = rec
    if blessed not in folders:
        blessed = next((c for c, (p, n) in folders.items()
                        if p == hfsplus.ROOT_FOLDER_ID and n == "System Folder"), 0)
    if not blessed:
        raise PrepError("no blessed System Folder")
    path, cnid = [], blessed
    while cnid != hfsplus.ROOT_FOLDER_ID:
        parent, name = folders[cnid]
        path.insert(0, name)
        cnid = parent
    prefs = prefs_dirs.get(blessed)
    if prefs is None:
        raise PrepError("no Preferences folder in the System Folder")
    return path + [folders[prefs][1]], files.get(prefs)


def _settings(vol: hfsplus.Volume, rec) -> tuple[list, tuple | None]:
    """(all resources, the '1ap0' 150 entry or None)."""
    resources = hfsplus.parse_resource_fork(vol.read_fork(rec, "rsrc"))
    entry = next((r for r in resources if r[0] == RES_TYPE and r[1] == RES_ID), None)
    return resources, entry


def state(image: str) -> dict:
    """Read-only: {"prefs": missing | no-settings | present | unexpected-layout,
    "airport_on": bool | None, "network": str | None (Mac OS Roman)}."""
    with hfsplus.Volume(image) as vol:
        _folder, rec = _locate(vol)
        if rec is None:
            return {"prefs": "missing", "airport_on": None, "network": None}
        _res, entry = _settings(vol, rec)
        if entry is None:
            return {"prefs": "no-settings", "airport_on": None, "network": None}
        try:
            info = parse(entry[4])
        except LayoutError:
            return {"prefs": "unexpected-layout", "airport_on": None, "network": None}
        return {"prefs": "present", "airport_on": info["airport_on"],
                "network": info["network"].decode("mac_roman") or None}


def set_network(image: str, name: bytes, create: bool = False) -> str:
    """Make AirPort join ``name`` (guest bytes) at the next start.  Guest stopped.

    Returns "unchanged", "set" (the name was changed in place: 33 bytes of an
    existing resource, no allocation), "created" (new ``AirPort Prefs``),
    "added" (settings added to an existing file, other resources kept),
    "unexpected-layout" (left alone), or without ``create`` "missing" /
    "no-settings" (the caller runs ``create=True`` on a copy: creating a file
    allocates blocks and needs root, the hfsplus module and fsck.hfsplus)."""
    name = check_name(name)
    with hfsplus.Volume(image, writable=True) as vol:
        folder, rec = _locate(vol)
        if rec is not None:
            resources, entry = _settings(vol, rec)
            if entry is not None:
                try:
                    info = parse(entry[4])
                except LayoutError:
                    return "unexpected-layout"
                if info["network"] == name:
                    return "unchanged"
                field = name + bytes(NETWORK_FIELD - len(name))
                vol.write_fork_in_place(rec, "rsrc", entry[3] + NETWORK_AT, field)
                vol.flush()
                return "set"
            if rec.fork("data").logical_size:
                return "unexpected-layout"          # never rewrite a file with a data fork
            type_creator = rec.type_creator
        if not create:
            return "missing" if rec is None else "no-settings"
    from . import prep
    ok, out = prep.fsck(image, repair=True)
    if not ok:
        raise prep.PrepError("volume could not be repaired: " + out[-300:])
    keep = [] if rec is None else [(t, i, n, d) for t, i, n, _p, d in resources]
    fork = hfsplus.build_resource_fork(keep + [(RES_TYPE, RES_ID, "", template(name))])
    prep._replace_resource_fork(image, folder, PREFS_NAME, fork, TYPE_CREATOR if rec is None else type_creator)
    ok, out, _ = prep.fsck_verify(image)
    if not ok:
        raise prep.PrepError("volume check failed after writing AirPort Prefs: " + out[-300:])
    return "created" if rec is None else "added"


def main(argv: list[str]) -> int:
    import argparse
    import json
    ap = argparse.ArgumentParser(prog="rbguestprep airport", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status", help="read-only")
    s.add_argument("image")
    w = sub.add_parser("set", help="root; guest stopped")
    w.add_argument("image")
    w.add_argument("--network", required=True, help="the name Mac OS shows (Mac OS Roman)")
    w.add_argument("--create", action="store_true", help="create AirPort Prefs when missing (in place)")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        out = state(a.image)
    else:
        out = {"result": set_network(a.image, a.network.encode("mac_roman"), create=a.create)}
    json.dump(out, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
