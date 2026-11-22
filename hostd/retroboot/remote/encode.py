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
