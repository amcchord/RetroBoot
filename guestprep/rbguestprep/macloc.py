"""Date & Time's city list on a stopped guest disk (read-only).

Mac OS 9's Date & Time control panel offers cities from a ``loc#`` resource; the
same list (byte-identical on 9.2.1 and 9.2.2) is in the System file (``loc#``
-20874) and in the control panel (``loc#`` 128).  Time Synchronizer (9.2.2)
accepts Mac OS's location (``MachineLocation``) only when it is at, or within a
few hundredths of a degree of, one of these cities; otherwise it reports "Your
Macintosh's time zone is not valid" and Restart/Shut Down stalls for about 100 s
(emulator/reports/tz2-20261012.md).  hostd therefore seeds the coordinates of a
city from this list (retroboot/macclock.py).

``loc#`` layout (big-endian): u16 count, then per city

    u16 length (of this entry)       i32 latitude, i32 longitude (Fracts of 90 degrees)
    u32 gmtDelta (standard time)     u8 region, u8 flags
    pstring name, pad to even        pstring country, pad to even
    u16 rule count, then per rule 12 bytes:
        4 ASCII "MMDD" (earliest day; zeros: the 1st of the month), u8 month,
        u8 week (1: first, 0xff: last), u8 weekday (1: Sunday), u8 0x80 when daylight
        saving starts, u16 seconds after midnight, u16 seconds added (0 when it ends)

    python3 -m rbguestprep cities IMAGE      # JSON, read-only
"""
from __future__ import annotations

import struct

from . import hfsplus

SOURCES = (("System", "loc#", -20874), ("Control Panels:Date & Time", "loc#", 128))


def _s24(v: int) -> int:
    v &= 0xFFFFFF
    return v - 0x1000000 if v & 0x800000 else v


def _s32(v: int) -> int:
    return v - (1 << 32) if v & 0x80000000 else v


def parse(blob: bytes) -> list[dict]:
    """Cities of a ``loc#`` resource."""
    if len(blob) < 2:
        return []
    count = struct.unpack(">H", blob[:2])[0]
    out = []
    p = 2
    for _ in range(count):
        if p + 2 > len(blob):
            break
        length = struct.unpack(">H", blob[p:p + 2])[0]
        e = blob[p:p + length]
        p += length
        if length < 18 or len(e) < length:
            break
        lat, lon, delta = struct.unpack(">III", e[2:14])
        q = 16
        name = e[q + 1:q + 1 + e[q]].decode("mac_roman", "replace")
        q += 1 + e[q]
        q += q & 1
        country = e[q + 1:q + 1 + e[q]].decode("mac_roman", "replace") if q < len(e) else ""
        q += 1 + (e[q] if q < len(e) else 0)
        q += q & 1
        rules = []
        if q + 2 <= len(e):
            n = struct.unpack(">H", e[q:q + 2])[0]
            q += 2
            for _ in range(n):
                r = e[q:q + 12]
                if len(r) < 12:
                    break
                tag = r[:4].decode("ascii", "replace") if r[:4] != b"\0\0\0\0" else ""
                month, week, wday, flag = r[4], r[5], r[6], r[7]
                at, save = struct.unpack(">HH", r[8:12])
                rules.append({"from": tag, "month": month, "week": week, "weekday": wday,
                              "dst": bool(flag & 0x80), "at": at, "save": save})
                q += 12
        out.append({"name": name, "country": country, "latitude": _s32(lat), "longitude": _s32(lon),
                    "std_offset": _s24(delta), "region": e[14], "flags": e[15], "rules": rules})
    return out


def _resource(vol: hfsplus.Volume, rec: hfsplus.CatalogRecord, rtype: str, rid: int) -> bytes | None:
    """One resource, reading only the fork's header, map and that resource."""
    fork = rec.fork("rsrc")
    if fork.logical_size < 16:
        return None
    doff, moff, _dlen, mlen = struct.unpack(">IIII", vol._read_fork(fork, 0, 16))
    rmap = vol._read_fork(fork, moff, mlen)
    tl_off = struct.unpack(">H", rmap[24:26])[0]
    tlist = rmap[tl_off:]
    ntypes = (struct.unpack(">H", tlist[:2])[0] + 1) & 0xFFFF
    for i in range(ntypes):
        t, cnt, ref = struct.unpack(">4sHH", tlist[2 + 8 * i:10 + 8 * i])
        if t.decode("mac_roman") != rtype:
            continue
        for j in range(cnt + 1):
            r_id, _noff, attr_off = struct.unpack(">hHI", tlist[ref + 12 * j:ref + 12 * j + 8])
            if r_id == rid:
                pos = doff + (attr_off & 0xFFFFFF)
                length = struct.unpack(">I", vol._read_fork(fork, pos, 4))[0]
                return vol._read_fork(fork, pos + 4, length)
    return None


def read_cities(image: str) -> list[dict]:
    """Date & Time's cities from the blessed System Folder (System file, else the
    control panel); [] when neither has the list."""
    from .prep import _system_folder
    with hfsplus.Volume(image) as vol:
        sysf = _system_folder(vol)
        for path, rtype, rid in SOURCES:
            try:
                rec = vol.lookup(sysf + path.split(":"))
            except FileNotFoundError:
                continue
            if not rec.is_file:
                continue
            blob = _resource(vol, rec, rtype, rid)
            if blob:
                cities = parse(blob)
                if cities:
                    return cities
    return []
