"""Tile encoding for the web Screen page (Python standard library only).

Tiles are lossless.  ``zlib`` (the default) is the tap buffer's own BGRX rows
compressed with zlib level 1; the browser inflates them with
DecompressionStream and swaps the channels.  ``png`` is a filter-0 RGB PNG for
browsers without DecompressionStream.  Measured on the lab builder with a
1024 x 768 Mac OS 9.2.1 Finder frame: zlib 4.5 ms (60 KB), PNG 8.8 ms
(56 KB); a 16 x 16 pointer tile is about 20 bytes.

Frame message (binary WebSocket message, little-endian):

    u8 'F', u8 version (1), u16 tiles, u32 seq, u32 gen, u32 flags
    per tile: u16 x, y, w, h; u8 encoding (1 zlib-bgrx, 2 png); u8 pad[3]; u32 length; data
"""
from __future__ import annotations

import functools
import struct
import zlib

ENC_ZLIB = 1
ENC_PNG = 2
ENCODINGS = {"zlib": ENC_ZLIB, "png": ENC_PNG}
BAND = 128            # big rectangles go out as bands of this many rows
BIG = 256 * 1024      # pixels

Rect = tuple[int, int, int, int]


def clip(r: Rect, w: int, h: int) -> Rect | None:
    x0, y0 = max(0, r[0]), max(0, r[1])
    x1, y1 = min(w, r[0] + r[2]), min(h, r[1] + r[3])
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1 - x0, y1 - y0)


def _union(a: Rect, b: Rect) -> Rect:
    x0, y0 = min(a[0], b[0]), min(a[1], b[1])
    x1, y1 = max(a[0] + a[2], b[0] + b[2]), max(a[1] + a[3], b[1] + b[3])
    return (x0, y0, x1 - x0, y1 - y0)


def _area(r: Rect) -> int:
    return r[2] * r[3]


def _touches(a: Rect, b: Rect) -> bool:
    if a[0] - 1 > b[0] + b[2] or b[0] > a[0] + a[2] + 1 or a[1] - 1 > b[1] + b[3] or b[1] > a[1] + a[3] + 1:
        return False
    u = _union(a, b)
    return _area(u) <= _area(a) + _area(b) + (_area(u) >> 3)


def _contains(a: Rect, b: Rect) -> bool:
    return a[0] <= b[0] and a[1] <= b[1] and a[0] + a[2] >= b[0] + b[2] and a[1] + a[3] >= b[1] + b[3]


class Region:
    """A short list of rectangles (conservative; merges when it fills up)."""

    MAX = 32

    def __init__(self) -> None:
        self.rects: list[Rect] = []

    def __bool__(self) -> bool:
        return bool(self.rects)

    def clear(self) -> None:
        self.rects = []

    def add(self, r: Rect) -> None:
        if r[2] <= 0 or r[3] <= 0:
            return
        while True:
            for i, e in enumerate(self.rects):
                if _contains(e, r):
                    return
                if _touches(e, r):
                    r = _union(e, r)
                    del self.rects[i]
                    break
            else:
                break
        if len(self.rects) < self.MAX:
            self.rects.append(r)
            return
        best = min(range(len(self.rects)), key=lambda i: _area(_union(self.rects[i], r)) - _area(self.rects[i]))
        u = _union(self.rects.pop(best), r)
        self.add(u)

    def area(self) -> int:
        return sum(_area(r) for r in self.rects)


def rect_bytes(buf, stride: int, r: Rect) -> bytes:
    x, y, w, h = r
    mv = memoryview(buf)
    if x == 0 and w * 4 == stride:
        return mv[y * stride:(y + h) * stride].tobytes()
    o, n = x * 4, w * 4
    return b"".join([mv[(y + i) * stride + o:(y + i) * stride + o + n] for i in range(h)])


def encode_zlib(buf, stride: int, r: Rect, level: int = 1) -> bytes:
    return zlib.compress(rect_bytes(buf, stride, r), level)


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def encode_png(buf, stride: int, r: Rect, level: int = 1) -> bytes:
    _, _, w, h = r
    src = rect_bytes(buf, stride, r)
    rgb = bytearray(w * h * 3)
    rgb[0::3] = src[2::4]
    rgb[1::3] = src[1::4]
    rgb[2::3] = src[0::4]
    rw = w * 3
    raw = b"".join([b"\0" + rgb[i * rw:(i + 1) * rw] for i in range(h)])
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(raw, level)) + _chunk(b"IEND", b""))


def split(r: Rect) -> list[Rect]:
    if _area(r) <= BIG:
        return [r]
    x, y, w, h = r
    return [(x, yy, w, min(BAND, y + h - yy)) for yy in range(y, y + h, BAND)]


def encode_tiles(buf, stride: int, rects: list[Rect], enc: int) -> list[tuple[Rect, int, bytes]]:
    """Encode rectangles (already clipped to the surface) into tiles."""
    fn = encode_png if enc == ENC_PNG else encode_zlib
    out = []
    for r in rects:
        for t in split(r):
            out.append((t, enc, fn(buf, stride, t)))
    return out


HEADER = struct.Struct("<BBHIII")
TILE = struct.Struct("<HHHHBxxxI")


def pack_frame(seq: int, gen: int, tiles: list[tuple[Rect, int, bytes]], flags: int = 0) -> bytes:
    parts = [HEADER.pack(0x46, 1, len(tiles), seq & 0xFFFFFFFF, gen & 0xFFFFFFFF, flags)]
    for (x, y, w, h), enc, data in tiles:
        parts.append(TILE.pack(x, y, w, h, enc, len(data)))
        parts.append(data)
    return b"".join(parts)


def unpack_frame(data: bytes) -> tuple[int, int, int, list[tuple[Rect, int, bytes]]]:
    """Inverse of pack_frame (tests and lab tools)."""
    magic, ver, n, seq, gen, flags = HEADER.unpack_from(data, 0)
    if magic != 0x46 or ver != 1:
        raise ValueError("not a frame")
    o = HEADER.size
    tiles = []
    for _ in range(n):
        x, y, w, h, enc, length = TILE.unpack_from(data, o)
        o += TILE.size
        tiles.append(((x, y, w, h), enc, data[o:o + length]))
        o += length
    return seq, gen, flags, tiles


_FONT_5X7 = {
    "A": (0x0E, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11),
    "B": (0x1E, 0x11, 0x11, 0x1E, 0x11, 0x11, 0x1E),
    "C": (0x0E, 0x11, 0x10, 0x10, 0x10, 0x11, 0x0E),
    "D": (0x1C, 0x12, 0x11, 0x11, 0x11, 0x12, 0x1C),
    "E": (0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x1F),
    "F": (0x1F, 0x10, 0x10, 0x1E, 0x10, 0x10, 0x10),
    "G": (0x0E, 0x11, 0x10, 0x17, 0x11, 0x11, 0x0F),
    "H": (0x11, 0x11, 0x11, 0x1F, 0x11, 0x11, 0x11),
    "I": (0x0E, 0x04, 0x04, 0x04, 0x04, 0x04, 0x0E),
    "K": (0x11, 0x12, 0x14, 0x18, 0x14, 0x12, 0x11),
    "L": (0x10, 0x10, 0x10, 0x10, 0x10, 0x10, 0x1F),
    "M": (0x11, 0x1B, 0x15, 0x15, 0x11, 0x11, 0x11),
    "N": (0x11, 0x19, 0x15, 0x13, 0x11, 0x11, 0x11),
    "O": (0x0E, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E),
    "P": (0x1E, 0x11, 0x11, 0x1E, 0x10, 0x10, 0x10),
    "R": (0x1E, 0x11, 0x11, 0x1E, 0x14, 0x12, 0x11),
    "S": (0x0F, 0x10, 0x10, 0x0E, 0x01, 0x01, 0x1E),
    "T": (0x1F, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04),
    "U": (0x11, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0E),
    "W": (0x11, 0x11, 0x11, 0x15, 0x15, 0x1B, 0x11),
    "Y": (0x11, 0x11, 0x0A, 0x04, 0x04, 0x04, 0x04),
    ".": (0x00, 0x00, 0x00, 0x00, 0x00, 0x0C, 0x0C),
    " ": (0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00),
}


@functools.lru_cache(maxsize=8)
def pairing_masked_png(width: int = 800, height: int = 600) -> bytes:
    """Crisp Platinum privacy screen returned instead of local pairing codes/QRs."""
    w = max(320, min(int(width), 1920))
    h = max(240, min(int(height), 1080))
    light, dark = b"\xb8\xb8\xb8", b"\x68\x68\x68"
    even_row = bytearray().join(light if (x & 1) == 0 else dark for x in range(w))
    odd_row = bytearray().join(dark if (x & 1) == 0 else light for x in range(w))
    buf = bytearray(w * h * 3)
    rw = w * 3
    for y in range(h):
        buf[y * rw:(y + 1) * rw] = even_row if (y & 1) == 0 else odd_row

    def fill(x0: int, y0: int, ww: int, hh: int, rgb: tuple[int, int, int]) -> None:
        r, g, b = rgb
        px = bytes((r, g, b)) * max(0, min(ww, w - x0))
        if not px or y0 >= h:
            return
        xs = max(0, x0)
        for yy in range(max(0, y0), min(h, y0 + hh)):
            o = yy * rw + xs * 3
            buf[o:o + len(px)] = px

    bx, by = max(16, (w - 420) // 2), max(16, (h - 140) // 2)
    bw, bh = min(420, w - 32), min(140, h - 32)
    fill(bx + 2, by + 2, bw, bh, (0x33, 0x33, 0x33))
    fill(bx, by, bw, bh, (0x00, 0x00, 0x00))
    fill(bx + 1, by + 1, bw - 2, bh - 2, (0xee, 0xee, 0xee))
    fill(bx + 2, by + 2, bw - 4, 2, (0xff, 0xff, 0xff))
    fill(bx + 2, by + 2, 2, bh - 4, (0xff, 0xff, 0xff))
    fill(bx + 2, by + bh - 4, bw - 4, 2, (0x88, 0x88, 0x88))
    fill(bx + bw - 4, by + 2, 2, bh - 4, (0x88, 0x88, 0x88))
    for py in range(by + 6, by + 20, 2):
        fill(bx + 8, py, bw - 16, 1, (0x77, 0x77, 0x77))
    fill(bx + 8, by + 24, bw - 16, 1, (0x22, 0x22, 0x22))
    # Draw crisp 2x lock badge + uppercase privacy notice
    lx, ly = bx + bw // 2 - 12, by + 36
    fill(lx + 4, ly, 16, 4, (0x22, 0x22, 0x22))
    fill(lx + 4, ly + 4, 4, 8, (0x22, 0x22, 0x22))
    fill(lx + 16, ly + 4, 4, 8, (0x22, 0x22, 0x22))
    fill(lx, ly + 12, 24, 18, (0x22, 0x22, 0x22))
    fill(lx + 10, ly + 18, 4, 6, (0xee, 0xee, 0xee))

    def draw_text(text: str, cy: int, scale: int = 2) -> None:
        text = text.upper()
        tw = len(text) * 6 * scale
        tx = max(bx + 12, (w - tw) // 2)
        for ch in text:
            rows = _FONT_5X7.get(ch, _FONT_5X7[" "])
            for ry, bits in enumerate(rows):
                for rx in range(5):
                    if bits & (1 << (4 - rx)):
                        fill(tx + rx * scale, cy + ry * scale, scale, scale, (0x11, 0x11, 0x11))
            tx += 6 * scale

    draw_text("PAIRING CODE HIDDEN", by + 78, 2)
    draw_text("CHECK THE RETROBOOT SCREEN", by + 104, 1)
    raw = b"".join(b"\0" + buf[y * rw:(y + 1) * rw] for y in range(h))
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(raw, 4)) + _chunk(b"IEND", b""))
