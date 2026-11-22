"""QuickDraw pictures ('PICT') made of bit images, decoded into an Image.

Mac OS keeps its startup screen artwork as pictures in the System file
(rbplatinum's ``startup`` assets, ``bundle.py``): each is one copy of a
packed pixel map (PackBitsRect).  This decoder understands version 1 and
version 2 pictures whose drawing is bit images (BitsRect, BitsRgn,
PackBitsRect, PackBitsRgn, DirectBitsRect, DirectBitsRgn) plus the opcodes
that do not draw (header, clip, comments, colours, hilite, font and pen
state); anything else raises ValueError, so a picture that needs real
QuickDraw rendering is never half drawn.  Imaging With QuickDraw, appendix A.

Pixels outside every drawn image stay transparent.  The *Rgn opcodes' mask
regions are applied as QuickDraw stores them (``region_rows``): a rectangle,
or scan lines of inversion points (Monitors' display pictures).
"""
from __future__ import annotations

import struct

from .icons import parse_clut
from .image import Image

WHITE = (255, 255, 255)
BLACK = (0, 0, 0)

# Opcodes that carry fixed-size data and draw nothing (version 2 numbering;
# version 1 uses the same values as single bytes).
_SKIP = {
    0x0000: 0,  # NOP
    0x0003: 2, 0x0004: 1, 0x0005: 2, 0x0006: 4, 0x0007: 4, 0x0008: 2,  # TxFont TxFace TxMode SpExtra PnSize PnMode
    0x0009: 8, 0x000A: 8, 0x000B: 4, 0x000C: 4, 0x000D: 2, 0x000E: 4, 0x000F: 4,  # pen/fill pats, OvSize, Origin, TxSize, colours
    0x0010: 8, 0x0011: 1,  # TxRatio, VersionOp (v1)
    0x0015: 2, 0x0016: 2,  # PnLocHFrac, ChExtra
    0x001A: 6, 0x001B: 6, 0x001C: 0, 0x001D: 6, 0x001E: 0, 0x001F: 6,  # RGB colours, HiliteMode/Colour, DefHilite, OpColor
    0x00A0: 2,  # ShortComment
}


def _unpack_bits(src: bytes, pos: int, n: int) -> tuple[bytes, int]:
    out = bytearray()
    while len(out) < n and pos < len(src):
        c = src[pos]
        pos += 1
        if c < 128:
            out += src[pos:pos + c + 1]
            pos += c + 1
        elif c > 128:
            out += bytes([src[pos]]) * (257 - c)
            pos += 1
    if len(out) < n:
        raise ValueError("PackBits row ends early")
    return bytes(out[:n]), pos


def _unpack_words(src: bytes, pos: int, nwords: int) -> tuple[bytes, int]:
    """packType 3: PackBits on 16-bit words."""
    out = bytearray()
    while len(out) < nwords * 2 and pos < len(src):
        c = src[pos]
        pos += 1
        if c < 128:
            out += src[pos:pos + (c + 1) * 2]
            pos += (c + 1) * 2
        elif c > 128:
            out += src[pos:pos + 2] * (257 - c)
            pos += 2
    if len(out) < nwords * 2:
        raise ValueError("PackBits row ends early")
    return bytes(out[:nwords * 2]), pos


def _rows(data: bytes, pos: int, row_bytes: int, h: int, pack_type: int, words: bool = False) -> tuple[list[bytes], int]:
    rows = []
    if row_bytes < 8 or pack_type == 1:
        for _ in range(h):
            rows.append(data[pos:pos + row_bytes])
            pos += row_bytes
        return rows, pos
    for _ in range(h):
        if row_bytes > 250:
            n = struct.unpack(">H", data[pos:pos + 2])[0]
            pos += 2
        else:
            n = data[pos]
            pos += 1
        packed = data[pos:pos + n]
        pos += n
        if words:
            row, _ = _unpack_words(packed, 0, row_bytes // 2)
        else:
            row, _ = _unpack_bits(packed, 0, row_bytes)
        rows.append(row)
    return rows, pos


def region_rows(data: bytes) -> tuple[tuple[int, int, int, int], dict[int, list[tuple[int, int]]] | None]:
    """A QuickDraw region: its bounding box (top, left, bottom, right) and, for a
    non-rectangular one, the spans inside it on each row ({y: [(x0, x1), ...]}).

    After the size and box, each scan line is a y, then x positions where the
    region's edges invert, then 0x7FFF; a final 0x7FFF ends the region.  A row's
    inversion points are XORed into the running set, which then holds from that
    row to the next listed one; inside is between alternate points."""
    size = struct.unpack(">H", data[:2])[0]
    box = struct.unpack(">4h", data[2:10])
    if size <= 10:
        return box, None
    pos, edges, lines = 10, set(), []
    while pos + 2 <= size:
        y = struct.unpack(">h", data[pos:pos + 2])[0]
        pos += 2
        if y == 0x7FFF:
            break
        xs = []
        while True:
            x = struct.unpack(">h", data[pos:pos + 2])[0]
            pos += 2
            if x == 0x7FFF:
                break
            xs.append(x)
        edges ^= set(xs)
        lines.append((y, sorted(edges)))
    spans: dict[int, list[tuple[int, int]]] = {}
    for i, (y, xs) in enumerate(lines):
        end = lines[i + 1][0] if i + 1 < len(lines) else box[2]
        row = [(xs[k], xs[k + 1]) for k in range(0, len(xs) - 1, 2)]
        for yy in range(y, end):
            spans[yy] = row
    return box, spans


def _index(row: bytes, x: int, depth: int) -> int:
    if depth == 8:
        return row[x]
    if depth == 4:
        b = row[x // 2]
        return b >> 4 if x % 2 == 0 else b & 15
    if depth == 2:
        return (row[x // 4] >> (6 - 2 * (x % 4))) & 3
    if depth == 1:
        return (row[x // 8] >> (7 - x % 8)) & 1
    raise ValueError(f"unsupported depth {depth}")


class _Picture:
    def __init__(self, data: bytes):
        self.d = data
        top, left, bottom, right = struct.unpack(">4h", data[2:10])
        self.frame = (left, top, right, bottom)
        self.img = Image(right - left, bottom - top)
        self.fg, self.bg = BLACK, WHITE

    def put(self, x: int, y: int, c) -> None:
        self.img.set(x - self.frame[0], y - self.frame[1], c)

    def bits(self, op: int, pos: int) -> int:
        d = self.d
        direct = op in (0x9A, 0x9B)
        if direct:
            pos += 4  # baseAddr
        rb_raw, top, left, bottom, right = struct.unpack(">Hhhhh", d[pos:pos + 10])
        is_pixmap = bool(rb_raw & 0x8000) or direct
        row_bytes = rb_raw & 0x3FFF
        pos += 10
        pack_type, depth, cmp_count, pal = 0, 1, 1, [self.bg, self.fg]
        if is_pixmap:
            (_ver, pack_type, _psize, _hres, _vres, _ptype, depth, cmp_count, _csize, _plane, _tab,
             _res) = struct.unpack(">HHIIIHHHHIII", d[pos:pos + 36])
            pos += 36
            if not direct:
                pal = parse_clut(d, pos)
                n = struct.unpack(">H", d[pos + 6:pos + 8])[0]
                pos += 8 + 8 * (n + 1)
        src = struct.unpack(">4h", d[pos:pos + 8])
        dst = struct.unpack(">4h", d[pos + 8:pos + 16])
        pos += 18  # + mode
        clip = spans = None
        if op in (0x91, 0x99, 0x9B):
            rsize = struct.unpack(">H", d[pos:pos + 2])[0]
            clip, spans = region_rows(d[pos:pos + rsize])
            pos += rsize
        h, w = bottom - top, right - left
        if (src[2] - src[0], src[3] - src[1]) != (dst[2] - dst[0], dst[3] - dst[1]):
            raise ValueError("scaled CopyBits")
        if direct and depth == 32:
            if pack_type == 4:
                rows = []
                for _ in range(h):
                    if row_bytes > 250:
                        n = struct.unpack(">H", d[pos:pos + 2])[0]
                        pos += 2
                    else:
                        n = d[pos]
                        pos += 1
                    plane, _ = _unpack_bits(d[pos:pos + n], 0, w * cmp_count)
                    pos += n
                    rows.append(plane)
            else:
                rows, pos = _rows(d, pos, row_bytes, h, pack_type)
        elif direct and depth == 16:
            rows, pos = _rows(d, pos, row_bytes, h, pack_type, words=pack_type == 3)
        else:
            rows, pos = _rows(d, pos, row_bytes, h, pack_type)
        for yy in range(src[2] - src[0]):
            sy = src[0] - top + yy
            row = rows[sy]
            for xx in range(src[3] - src[1]):
                sx = src[1] - left + xx
                dx, dy = dst[1] + xx, dst[0] + yy
                if clip and not (clip[1] <= dx < clip[3] and clip[0] <= dy < clip[2]):
                    continue
                if spans is not None and not any(a <= dx < b for a, b in spans.get(dy, ())):
                    continue
                if direct and depth == 32:
                    if pack_type == 4:
                        o = cmp_count - 3
                        c = (row[(o + 0) * w + sx], row[(o + 1) * w + sx], row[(o + 2) * w + sx])
                    else:
                        c = tuple(row[sx * 4 + 1:sx * 4 + 4])
                elif direct and depth == 16:
                    v = struct.unpack(">H", row[sx * 2:sx * 2 + 2])[0]
                    c = tuple(((v >> sh) & 31) * 255 // 31 for sh in (10, 5, 0))
                else:
                    i = _index(row, sx, depth)
                    c = pal[i] if i < len(pal) else BLACK
                self.put(dx, dy, c)
        return pos

    def run(self) -> Image:
        d = self.d
        pos = 10
        if d[pos:pos + 2] == b"\x11\x01":
            v2, pos = False, pos + 2
        elif d[pos:pos + 4] == b"\x00\x11\x02\xff":
            v2, pos = True, pos + 4
        else:
            raise ValueError("not a PICT")
        while pos < len(d):
            if v2:
                pos += pos & 1
                op = struct.unpack(">H", d[pos:pos + 2])[0]
                pos += 2
            else:
                op = d[pos]
                pos += 1
            if op == 0x00FF:
                return self.img
            if op == 0x0C00:
                pos += 24
            elif op == 0x0001:  # Clip
                pos += struct.unpack(">H", d[pos:pos + 2])[0]
            elif op == 0x00A1:  # LongComment
                pos += 4 + struct.unpack(">H", d[pos + 2:pos + 4])[0]
            elif op == 0x001A:
                self.fg = tuple(d[pos + i] for i in (0, 2, 4))
                pos += 6
            elif op == 0x001B:
                self.bg = tuple(d[pos + i] for i in (0, 2, 4))
                pos += 6
            elif op in (0x90, 0x91, 0x98, 0x99, 0x9A, 0x9B):
                pos = self.bits(op, pos)
            elif op in _SKIP:
                pos += _SKIP[op]
            else:
                raise ValueError(f"PICT opcode {op:#06x} is not a bit image")
        raise ValueError("PICT has no end")


def decode(data: bytes) -> Image:
    """The picture's frame as an RGBA image (undrawn pixels transparent)."""
    if len(data) < 12:
        raise ValueError("PICT too short")
    return _Picture(data).run()
