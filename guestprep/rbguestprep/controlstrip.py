"""The Control Strip's place (``System Folder:Preferences:Control Strip Preferences``).

Mac OS 9's Control Strip (``Control Strip Extension``, a background
application the Process Manager starts after the extensions) remembers where
the strip is: the strip's corner as an absolute screen point, together with
the main screen's bounds when it saved them.  While Mac OS runs it follows the
screen: after a live resolution change (Monitors, the Display Manager, a
RetroBoot pixel-size change or rotation) a strip on the bottom edge stays on
the bottom edge and one elsewhere keeps its share of the height below the
menu bar.  At startup it does not compare the saved screen with the current
one; it uses the saved point as it is, and only when that point is off every
screen does it put the strip in the bottom corner on its side.

RetroBoot changes the Mac screen between Mac OS sessions (3D acceleration on
or off, a pixel size or display chosen while Mac OS was shut down), so the
strip that sat on the bottom edge of an 832 x 624 ATI screen came back
96 pixels above the bottom of a 1080 x 720 one, and a strip on the right edge
of a larger screen fell to the bottom corner of a smaller one
(hostd/reports/cardswitch-2026-10-13.md).  Before each launch hostd makes the
saved place what Mac OS itself would have made of it had it seen the change
(``fit``): the saved screen becomes the screen Mac OS will start with, a strip
near the bottom edge keeps its distance from it, any other strip keeps its
share of the height, and a strip on the right edge stays on the right edge.
Its side, collapsed or expanded state, modules and everything else are kept.

Layout, from the files the Control Strip of Mac OS 9.2.1 and 9.2.2 (the same
``Control Strip Extension``) writes on RetroBoot's emulated machine (a strip
on the bottom edge and moved to the right edge, expanded and collapsed, at
1080 x 720 and 832 x 624): resource ``'pref'`` (ID 3114 on every disk seen,
named "Control Strip Preferences") in a file of type ``pref``, creator
``sdev``.

====== ====================================================================
0x00   u16 format, 0x0203
0x04   u16 flags (3 on the left edge, 2 on the right edge)
0x06   u16 the strip's width (456 expanded with the default modules, 16
       collapsed)
0x0C   Rect top, left, bottom, right: the main screen when saved
0x14   Point v, h: the strip's top and its edge (h 1 on the left edge, the
       screen's right - 1 on the right edge)
====== ====================================================================

Only the screen and the point change (12 bytes in place, no allocation).
"""
from __future__ import annotations

import sys

from . import hfsplus

PREFS_NAME = "Control Strip Preferences"
RES_TYPE = "pref"
FORMAT = 0x0203
SCREEN_AT = 0x0C
WHERE_AT = 0x14
MIN_LEN = WHERE_AT + 4
MENU_BAR = 20            # Mac OS 9's menu bar height: the strip is never moved above it
STRIP_H = 23             # the strip's height (saved top on the bottom edge = screen bottom - 23)
NEAR_BOTTOM = 32         # a strip whose bottom is this close to the screen's bottom keeps that gap
SIZE_RANGE = (128, 8192)


class LayoutError(ValueError):
    """Settings in a layout RetroBoot does not know; left alone."""


def _u16(data: bytes, at: int) -> int:
    return int.from_bytes(data[at:at + 2], "big")


def _s16(data: bytes, at: int) -> int:
    return int.from_bytes(data[at:at + 2], "big", signed=True)


def parse(data: bytes) -> dict:
    """{"screen": (width, height), "top": v, "edge": h, "side": "left"|"right"} of a 'pref' resource."""
    if len(data) < MIN_LEN or _u16(data, 0) != FORMAT:
        raise LayoutError(f"unexpected Control Strip settings (length {len(data)}, format {data[:2].hex()})")
    top, left, bottom, right = (_s16(data, SCREEN_AT + 2 * i) for i in range(4))
    if (top, left) != (0, 0) or not all(SIZE_RANGE[0] <= v <= SIZE_RANGE[1] for v in (bottom, right)):
        raise LayoutError(f"unexpected Control Strip screen {(top, left, bottom, right)}")
    v, h = _s16(data, WHERE_AT), _s16(data, WHERE_AT + 2)
    if not (-SIZE_RANGE[1] <= v <= SIZE_RANGE[1] and -SIZE_RANGE[1] <= h <= SIZE_RANGE[1]):
        raise LayoutError(f"unexpected Control Strip position {(v, h)}")
    return {"screen": (right, bottom), "top": v, "edge": h, "side": "right" if h > right // 2 else "left"}


def place(screen: tuple[int, int], top: int, edge: int, size: tuple[int, int]) -> tuple[int, int]:
    """(top, edge) on a ``size`` screen for a strip at (top, edge) on ``screen``: what Mac OS
    does on a live resolution change (lab, Display Manager switches 1080 x 720 <-> 832 x 624)."""
    (ow, oh), (w, h) = screen, size
    lowest = h - STRIP_H
    gap = oh - (top + STRIP_H)                # from the strip's bottom to the screen's bottom
    if gap <= NEAR_BOTTOM:
        v = h - STRIP_H - max(0, gap)
    else:
        v = MENU_BAR + round((top - MENU_BAR) * (h - MENU_BAR) / max(1, oh - MENU_BAR))
    v = max(MENU_BAR, min(lowest, v))
    x = w - 1 if edge > ow // 2 else max(0, min(w - 1, edge))
    return v, x


def fitted(data: bytes, size: tuple[int, int]) -> bytes | None:
    """``data`` for a Mac OS that starts with a ``size`` (width, height) main screen, or None
    when it was saved on a screen of that size (nothing to do)."""
    info = parse(data)
    w, h = int(size[0]), int(size[1])
    if not all(SIZE_RANGE[0] <= v <= SIZE_RANGE[1] for v in (w, h)):
        raise ValueError(f"unexpected screen size {size}")
    if info["screen"] == (w, h):
        return None
    v, x = place(info["screen"], info["top"], info["edge"], (w, h))
    out = bytearray(data)
    out[SCREEN_AT:SCREEN_AT + 8] = b"".join(n.to_bytes(2, "big") for n in (0, 0, h, w))
    out[WHERE_AT:WHERE_AT + 4] = v.to_bytes(2, "big", signed=True) + x.to_bytes(2, "big", signed=True)
    return bytes(out)


def _record(vol: hfsplus.Volume):
    """The Control Strip Preferences record in the blessed System Folder, or None."""
    from .prep import _system_folder
    try:
        return vol.lookup(_system_folder(vol) + ["Preferences", PREFS_NAME])
    except FileNotFoundError:
        return None


def _entries(vol: hfsplus.Volume, rec) -> list[tuple]:
    return [r for r in hfsplus.parse_resource_fork(vol.read_fork(rec, "rsrc")) if r[0] == RES_TYPE]


def state(image: str) -> dict:
    """Read-only: {"prefs": missing | no-settings | present | unexpected-layout, "strips": [...]}."""
    with hfsplus.Volume(image) as vol:
        rec = _record(vol)
        if rec is None:
            return {"prefs": "missing", "strips": []}
        entries = _entries(vol, rec)
        if not entries:
            return {"prefs": "no-settings", "strips": []}
        strips = []
        try:
            for _t, rid, _n, _pos, data in entries:
                info = parse(data)
                strips.append({"id": rid, "screen": list(info["screen"]), "top": info["top"],
                               "edge": info["edge"], "side": info["side"], "width": _u16(data, 6)})
        except LayoutError:
            return {"prefs": "unexpected-layout", "strips": strips}
        return {"prefs": "present", "strips": strips}


def fit(image: str, size: tuple[int, int]) -> dict:
    """Put the strip where Mac OS would after a change to a ``size`` (width, height) screen.
    Guest stopped; changes 12 bytes in place.

    {"result": "moved" | "unchanged" | "missing" | "no-settings" | "unexpected-layout",
     "from": [screen w, h, top, edge], "to": [...]} ("from"/"to" when moved).  A file in a
    layout RetroBoot does not know is left alone; nothing is ever created."""
    with hfsplus.Volume(image, writable=True) as vol:
        rec = _record(vol)
        if rec is None:
            return {"result": "missing"}
        entries = _entries(vol, rec)
        if not entries:
            return {"result": "no-settings"}
        writes = []
        try:
            for _t, _rid, _n, pos, data in entries:
                new = fitted(data, size)
                if new is not None:
                    writes.append((pos, data, new))
        except LayoutError:
            return {"result": "unexpected-layout"}
        if not writes:
            return {"result": "unchanged"}
        for pos, _data, new in writes:
            vol.write_fork_in_place(rec, "rsrc", pos + SCREEN_AT, new[SCREEN_AT:MIN_LEN])
        vol.flush()
    pos, old, new = writes[0]
    a, b = parse(old), parse(new)
    return {"result": "moved", "from": [*a["screen"], a["top"], a["edge"]],
            "to": [*b["screen"], b["top"], b["edge"]]}


def main(argv: list[str]) -> int:
    import argparse
    import json
    ap = argparse.ArgumentParser(prog="rbguestprep control-strip", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status", help="read-only")
    s.add_argument("image")
    f = sub.add_parser("fit", help="root; guest stopped")
    f.add_argument("image")
    f.add_argument("--screen", required=True, help="the Mac screen Mac OS will start with, WIDTHxHEIGHT")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        out = state(a.image)
    else:
        w, h = (int(v) for v in a.screen.lower().split("x"))
        out = fit(a.image, (w, h))
    json.dump(out, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
