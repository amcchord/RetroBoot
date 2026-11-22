"""Tiny RGBA image type and a PNG encoder/decoder (standard library only)."""
from __future__ import annotations

import struct
import zlib


class Image:
    """RGBA pixels as a flat list of (r, g, b, a) tuples, row-major."""

    def __init__(self, w: int, h: int, fill=(0, 0, 0, 0)):
        self.w, self.h = w, h
        self.px = [fill] * (w * h)

    def get(self, x: int, y: int):
        return self.px[y * self.w + x]

    def set(self, x: int, y: int, c) -> None:
        if 0 <= x < self.w and 0 <= y < self.h:
            if len(c) == 3:
                c = (c[0], c[1], c[2], 255)
            self.px[y * self.w + x] = c

    def fill(self, x: int, y: int, w: int, h: int, c) -> None:
        for yy in range(max(0, y), min(self.h, y + h)):
            for xx in range(max(0, x), min(self.w, x + w)):
                self.set(xx, yy, c)

    def paste(self, other: "Image", x: int, y: int) -> None:
        for yy in range(other.h):
            for xx in range(other.w):
                self.set(x + xx, y + yy, other.get(xx, yy))

    def crop(self, x: int, y: int, w: int, h: int) -> "Image":
        out = Image(w, h)
        for yy in range(h):
            for xx in range(w):
                if 0 <= x + xx < self.w and 0 <= y + yy < self.h:
                    out.px[yy * w + xx] = self.get(x + xx, y + yy)
        return out

    def scaled(self, k: int) -> "Image":
        out = Image(self.w * k, self.h * k)
        for y in range(self.h):
            row = [c for c in self.px[y * self.w:(y + 1) * self.w] for _ in range(k)]
            for j in range(k):
                out.px[(y * k + j) * out.w:(y * k + j + 1) * out.w] = row
        return out

    def opaque(self) -> bool:
        return all(c[3] == 255 for c in self.px)

    def png(self) -> bytes:
        return encode_png(self)


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def encode_png(img: Image) -> bytes:
    rgba = not img.opaque()
    raw = bytearray()
    for y in range(img.h):
        raw.append(0)
        for c in img.px[y * img.w:(y + 1) * img.w]:
            raw.extend(c if rgba else c[:3])
    ihdr = struct.pack(">IIBBBBB", img.w, img.h, 8, 6 if rgba else 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + _chunk(b"IEND", b"")


def decode_png(data: bytes) -> Image:
    """Decode 8-bit RGB/RGBA/gray/palette non-interlaced PNGs (enough for tests and references)."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, idat, plte, trns = 8, b"", None, None
    while pos < len(data):
        n = struct.unpack(">I", data[pos:pos + 4])[0]
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + n]
        if kind == b"IHDR":
            w, h, depth, ctype, _c, _f, interlace = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
        elif kind == b"PLTE":
            plte = [tuple(body[i:i + 3]) for i in range(0, len(body), 3)]
        elif kind == b"tRNS":
            trns = body
        pos += 12 + n
    if depth != 8 or interlace:
        raise ValueError("unsupported PNG")
    bpp = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    raw = zlib.decompress(idat)
    stride = w * bpp
    prev = bytearray(stride)
    img = Image(w, h)
    p = 0
    for y in range(h):
        f = raw[p]
        line = bytearray(raw[p + 1:p + 1 + stride])
        p += 1 + stride
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if f == 1:
                line[i] = (line[i] + a) & 255
            elif f == 2:
                line[i] = (line[i] + b) & 255
            elif f == 3:
                line[i] = (line[i] + (a + b) // 2) & 255
            elif f == 4:
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 255
        prev = line
        for x in range(w):
            s = line[x * bpp:(x + 1) * bpp]
            if ctype == 6:
                c = tuple(s)
            elif ctype == 2:
                c = (s[0], s[1], s[2], 255)
            elif ctype == 0:
                c = (s[0], s[0], s[0], 255)
            elif ctype == 4:
                c = (s[0], s[0], s[0], s[1])
            else:
                r, g, b = plte[s[0]]
                c = (r, g, b, trns[s[0]] if trns and s[0] < len(trns) else 255)
            img.px[y * w + x] = c
    return img
