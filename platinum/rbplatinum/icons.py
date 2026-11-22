"""Classic Mac OS icon, cursor, pattern and color-table decoders.

Formats: ICN# ics# ICON SICN CURS (1-bit), icl4 ics4 (4-bit, standard clut 4),
icl8 ics8 (8-bit, standard clut 8), cicn, crsr, ppat (PixMap based, own
color table), icns (il32/is32/ih32/it32 RLE RGB with l8mk/s8mk/h8mk/t8mk
masks) and 'clut'.  Inside Macintosh: Imaging With QuickDraw and More
Macintosh Toolbox describe the layouts.
"""
from __future__ import annotations

import struct

from .image import Image

WHITE, BLACK = (255, 255, 255, 255), (0, 0, 0, 255)
CLEAR = (0, 0, 0, 0)


def system_clut8() -> list[tuple[int, int, int]]:
    """The standard 8-bit Macintosh palette (clut 8), computed."""
    lv = [0xFF, 0xCC, 0x99, 0x66, 0x33, 0x00]
    out = [(r, g, b) for r in lv for g in lv for b in lv][:215]
    ramp = [0xEE, 0xDD, 0xBB, 0xAA, 0x88, 0x77, 0x55, 0x44, 0x22, 0x11]
    out += [(v, 0, 0) for v in ramp] + [(0, v, 0) for v in ramp] + [(0, 0, v) for v in ramp]
    out += [(v, v, v) for v in ramp] + [(0, 0, 0)]
    return out


CLUT4 = [(0xFF, 0xFF, 0xFF), (0xFC, 0xF3, 0x05), (0xFF, 0x64, 0x02), (0xDD, 0x08, 0x06),
         (0xF2, 0x08, 0x84), (0x46, 0x00, 0xA5), (0x00, 0x00, 0xD4), (0x02, 0xAB, 0xEA),
         (0x1F, 0xB7, 0x14), (0x00, 0x64, 0x11), (0x56, 0x2C, 0x05), (0x90, 0x71, 0x3A),
         (0xC0, 0xC0, 0xC0), (0x80, 0x80, 0x80), (0x40, 0x40, 0x40), (0x00, 0x00, 0x00)]
CLUT8 = system_clut8()


def parse_clut(data: bytes, offset: int = 0) -> list[tuple[int, int, int]]:
    """A ColorTable: returns colors indexed by value (or by position for device tables)."""
    _seed, flags, n = struct.unpack(">IHH", data[offset:offset + 8])
    entries = []
    for i in range(n + 1):
        o = offset + 8 + 8 * i
        if o + 8 > len(data):
            break
        v, r, g, b = struct.unpack(">HHHH", data[o:o + 8])
        entries.append((v, (r >> 8, g >> 8, b >> 8)))
    if flags & 0x8000:  # device table: index is the position
        return [c for _v, c in entries]
    size = max([v for v, _ in entries] + [len(entries) - 1]) + 1
    out = [(0, 0, 0)] * size
    for v, c in entries:
        if v < size:
            out[v] = c
    return out


def _bits(data: bytes, w: int, h: int, row_bytes: int | None = None) -> list[list[int]]:
    rb = row_bytes or (w + 7) // 8
    return [[(data[y * rb + x // 8] >> (7 - x % 8)) & 1 for x in range(w)] for y in range(h)]


def mono_icon(data: bytes, size: int, masked: bool = True) -> Image:
    """ICN#/ics# (image then mask), or ICON/SICN without mask."""
    n = size * size // 8
    img = _bits(data[:n], size, size)
    mask = _bits(data[n:2 * n], size, size) if masked and len(data) >= 2 * n else None
    out = Image(size, size)
    for y in range(size):
        for x in range(size):
            if mask is None or mask[y][x]:
                out.set(x, y, BLACK if img[y][x] else WHITE)
            elif img[y][x]:
                out.set(x, y, BLACK)
    return out


def mask_of(data: bytes, size: int) -> list[list[int]]:
    n = size * size // 8
    return _bits(data[n:2 * n], size, size)


def color_icon(data: bytes, size: int, depth: int, mask: list[list[int]] | None) -> Image:
    pal = CLUT8 if depth == 8 else CLUT4
    out = Image(size, size)
    for y in range(size):
        for x in range(size):
            if depth == 8:
                v = data[y * size + x]
            else:
                b = data[(y * size + x) // 2]
                v = (b >> 4) if x % 2 == 0 else (b & 15)
            if mask is None or mask[y][x]:
                out.set(x, y, pal[v])
    return out


def _rle(data: bytes, pos: int, count: int) -> tuple[bytes, int]:
    out = bytearray()
    while len(out) < count and pos < len(data):
        n = data[pos]
        if n < 0x80:
            out += data[pos + 1:pos + 2 + n]
            pos += 2 + n
        else:
            out += bytes([data[pos + 1]]) * (n - 0x80 + 3)
            pos += 2
    return bytes(out[:count]), pos


ICNS_RGB = {"is32": ("s8mk", 16), "il32": ("l8mk", 32), "ih32": ("h8mk", 48), "it32": ("t8mk", 128)}


def icns_elements(data: bytes) -> dict[str, bytes]:
    if data[:4] != b"icns":
        return {}
    total = struct.unpack(">I", data[4:8])[0]
    pos, out = 8, {}
    while pos + 8 <= min(total, len(data)):
        t = data[pos:pos + 4].decode("mac_roman")
        n = struct.unpack(">I", data[pos + 4:pos + 8])[0]
        if n < 8:
            break
        out[t] = data[pos + 8:pos + n]
        pos += n
    return out


def icns_image(els: dict[str, bytes], kind: str) -> Image | None:
    if kind not in els:
        return None
    mk, size = ICNS_RGB[kind]
    raw = els[kind]
    if kind == "it32":
        raw = raw[4:]
    n = size * size
    if len(raw) == 3 * n:
        chans = [raw[0:n], raw[n:2 * n], raw[2 * n:3 * n]]
    else:
        chans, pos = [], 0
        for _ in range(3):
            c, pos = _rle(raw, pos, n)
            chans.append(c.ljust(n, b"\0"))
    alpha = els.get(mk)
    out = Image(size, size)
    for i in range(n):
        out.px[i] = (chans[0][i], chans[1][i], chans[2][i], alpha[i] if alpha and len(alpha) >= n else 255)
    return out


def _pixmap(data: bytes, off: int) -> dict:
    (_base, rb, top, left, bottom, right, _ver, _pt, _ps, _hr, _vr, _ptype, psize, ccount, csize,
     _pb, table, _res) = struct.unpack(">IHhhhhHHIIIHHHHIII", data[off:off + 50])
    return {"row_bytes": rb & 0x3FFF, "w": right - left, "h": bottom - top, "depth": psize, "table": table}


def _pixels(data: bytes, off: int, pm: dict, pal) -> list[list[tuple]]:
    rows = []
    d = pm["depth"]
    for y in range(pm["h"]):
        row = []
        base = off + y * pm["row_bytes"]
        for x in range(pm["w"]):
            if d == 8:
                v = data[base + x]
            elif d == 4:
                b = data[base + x // 2]
                v = b >> 4 if x % 2 == 0 else b & 15
            elif d == 2:
                v = (data[base + x // 4] >> (6 - 2 * (x % 4))) & 3
            elif d == 1:
                v = (data[base + x // 8] >> (7 - x % 8)) & 1
            else:
                raise ValueError(f"unsupported depth {d}")
            row.append(pal[v] if v < len(pal) else (0, 0, 0))
        rows.append(row)
    return rows


def cicn(data: bytes) -> Image:
    pm = _pixmap(data, 0)
    mrb, mt, ml, mb, mr = struct.unpack(">Hhhhh", data[54:64])
    irb, it, il, ib, ir = struct.unpack(">Hhhhh", data[68:78])
    h = mb - mt
    pos = 82
    mask = _bits(data[pos:pos + mrb * h], pm["w"], h, mrb) if mrb else None
    pos += mrb * h
    pos += irb * (ib - it)
    pal = parse_clut(data, pos)
    _s, _f, n = struct.unpack(">IHH", data[pos:pos + 8])
    pos += 8 + 8 * (n + 1)
    rows = _pixels(data, pos, pm, pal)
    out = Image(pm["w"], pm["h"])
    for y in range(pm["h"]):
        for x in range(pm["w"]):
            if mask is None or mask[y][x]:
                out.set(x, y, rows[y][x])
    return out


def curs(data: bytes) -> tuple[Image, tuple[int, int]]:
    img = _bits(data[0:32], 16, 16)
    mask = _bits(data[32:64], 16, 16)
    v, hh = struct.unpack(">hh", data[64:68])
    out = Image(16, 16)
    for y in range(16):
        for x in range(16):
            if mask[y][x]:
                out.set(x, y, BLACK if img[y][x] else WHITE)
            elif img[y][x]:  # inverted pixel: shown as black on light backgrounds
                out.set(x, y, BLACK)
    return out, (hh, v)


def crsr(data: bytes) -> tuple[Image, tuple[int, int]]:
    _type, map_off, data_off = struct.unpack(">HII", data[0:10])
    mask = _bits(data[52:84], 16, 16)
    img1 = _bits(data[20:52], 16, 16)
    v, hh = struct.unpack(">hh", data[84:88])
    pm = _pixmap(data, map_off)
    pal = parse_clut(data, pm["table"]) if pm["table"] else CLUT8
    rows = _pixels(data, data_off, pm, pal)
    out = Image(16, 16)
    for y in range(16):
        for x in range(16):
            if mask[y][x]:
                out.set(x, y, rows[y][x])
            elif img1[y][x]:
                out.set(x, y, BLACK)
    return out, (hh, v)


def ppat(data: bytes) -> Image:
    ptype, map_off, data_off = struct.unpack(">HII", data[0:10])
    if ptype == 0:  # old-style 8x8 1-bit pattern only
        bits = _bits(data[20:28], 8, 8)
        out = Image(8, 8)
        for y in range(8):
            for x in range(8):
                out.set(x, y, BLACK if bits[y][x] else WHITE)
        return out
    pm = _pixmap(data, map_off)
    pal = parse_clut(data, pm["table"])
    rows = _pixels(data, data_off, pm, pal)
    out = Image(pm["w"], pm["h"])
    for y in range(pm["h"]):
        for x in range(pm["w"]):
            out.set(x, y, rows[y][x])
    return out


def pat(data: bytes) -> Image:
    bits = _bits(data[:8], 8, 8)
    out = Image(8, 8)
    for y in range(8):
        for x in range(8):
            out.set(x, y, BLACK if bits[y][x] else WHITE)
    return out


def icon_family(rf, rid: int, size: int = 32) -> Image | None:
    """Best image of a classic icon family (8-bit, then 4-bit, then 1-bit)."""
    if size == 32:
        mono, c8, c4 = rf.data("ICN#", rid), rf.data("icl8", rid), rf.data("icl4", rid)
    else:
        mono, c8, c4 = rf.data("ics#", rid), rf.data("ics8", rid), rf.data("ics4", rid)
    mask = mask_of(mono, size) if mono else None
    if c8:
        return color_icon(c8, size, 8, mask)
    if c4:
        return color_icon(c4, size, 4, mask)
    if mono:
        return mono_icon(mono, size)
    return None


def bundle_icon_id(rf, file_type: str) -> int | None:
    """Icon family ID a file's own BNDL/FREF assigns to its file type."""
    for b in rf.of_type("BNDL"):
        d = b.data
        if len(d) < 8:
            continue
        ntypes = struct.unpack(">h", d[6:8])[0] + 1
        pos = 8
        maps: dict[str, dict[int, int]] = {}
        for _ in range(ntypes):
            t = d[pos:pos + 4].decode("mac_roman")
            n = struct.unpack(">h", d[pos + 4:pos + 6])[0] + 1
            pos += 6
            m = {}
            for _ in range(n):
                local, rid = struct.unpack(">hh", d[pos:pos + 4])
                m[local] = rid
                pos += 4
            maps[t] = m
        for local, fref_id in maps.get("FREF", {}).items():
            fr = rf.data("FREF", fref_id)
            if fr and fr[:4].decode("mac_roman") == file_type:
                icon_local = struct.unpack(">h", fr[4:6])[0]
                return maps.get("ICN#", {}).get(icon_local)
    return None


def file_icon(rf, file_type: str, size: int = 32) -> Image | None:
    """A file's own icon: icns (32-bit with alpha) if present, else its classic family."""
    rid = bundle_icon_id(rf, file_type)
    if rid is None:
        return None
    r = rf.get("icns", rid)
    if r is not None and not r.compressed:
        img = icns_image(icns_elements(r.data), "il32" if size == 32 else "is32")
        if img is not None:
            return img
    return icon_family(rf, rid, size)
