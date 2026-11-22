"""Bitmap fonts: NFNT/FONT decoding, FOND lookup and QuickDraw style synthesis.

A BitFont holds one strike: per character code (Mac Roman) a glyph bitmap with
its left offset from the pen and its advance, plus ascent/descent/leading.
Glyph values are coverage levels 0..16 (1-bit strikes use 0 and 16, "fully
covered"), so the same structure carries Mac OS 9's smoothed text (1..15 blend
with the background, 16 is the text color).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

from .rsrc import ResourceFile


@dataclass
class Glyph:
    rows: list[list[int]]      # coverage 0..16, rows[0] is the top row
    left: int                  # x of column 0 relative to the pen
    top: int                   # y of row 0 relative to the baseline (positive = above)
    advance: int

    @property
    def width(self) -> int:
        return len(self.rows[0]) if self.rows else 0


@dataclass
class BitFont:
    family: str
    size: int
    style: str                 # 'plain' | 'bold' ...
    ascent: int
    descent: int
    leading: int
    glyphs: dict[int, Glyph] = field(default_factory=dict)   # Mac Roman code -> glyph
    smooth: bool = False       # glyphs carry 16-level coverage
    missing: Glyph | None = None

    def glyph(self, code: int) -> Glyph | None:
        return self.glyphs.get(code) or self.missing


def parse_nfnt(data: bytes, family: str, size: int) -> BitFont:
    (ftype, first, last, _widmax, kernmax, _ndesc, frw, frh, _owtloc, ascent, descent, leading,
     rowwords) = struct.unpack(">hhhhhhhhhhhhh", data[:26])
    row_bytes = rowwords * 2
    bits = data[26:26 + row_bytes * frh]
    n = last - first + 3
    loc_off = 26 + row_bytes * frh
    loc = struct.unpack(f">{n}H", data[loc_off:loc_off + 2 * n])
    ow = struct.unpack(f">{n}h", data[loc_off + 2 * n:loc_off + 4 * n])
    font = BitFont(family, size, "plain", ascent, descent, leading)

    def col(x: int, y: int) -> int:
        return (bits[y * row_bytes + x // 8] >> (7 - x % 8)) & 1

    for i in range(n - 1):
        if ow[i] == -1:
            continue
        offset, width = (ow[i] >> 8) & 0xFF, ow[i] & 0xFF
        x0, x1 = loc[i], loc[i + 1]
        rows = [[16 if col(x, y) else 0 for x in range(x0, x1)] for y in range(frh)]
        g = Glyph(rows, kernmax + offset, ascent, width)
        if i == n - 2:
            font.missing = g
        else:
            font.glyphs[first + i] = g
    return font


def fond_strikes(fond: bytes) -> list[tuple[int, int, int]]:
    """(size, style, NFNT id) entries of a FOND's font association table."""
    n = struct.unpack(">h", fond[52:54])[0] + 1
    return [struct.unpack(">hhh", fond[54 + 6 * i:60 + 6 * i]) for i in range(n)]


def find_strike(files: list[ResourceFile], family: str, size: int, style: int = 0) -> BitFont | None:
    for rf in files:
        fond = rf.named("FOND", family)
        if fond is None:
            continue
        for sz, st, rid in fond_strikes(fond.data):
            if sz == size and st == style:
                for t in ("NFNT", "FONT"):
                    for rf2 in files:
                        d = rf2.data(t, rid)
                        if d:
                            return parse_nfnt(d, family, size)
    return None


def embolden(font: BitFont, extra: int = 1) -> BitFont:
    """QuickDraw synthetic bold: each glyph ORed with itself shifted right, advance + extra."""
    out = BitFont(font.family, font.size, "bold", font.ascent, font.descent, font.leading, smooth=font.smooth)

    def smear(g: Glyph) -> Glyph:
        rows = []
        for r in g.rows:
            nr = list(r) + [0] * extra
            for k in range(1, extra + 1):
                for x, v in enumerate(r):
                    nr[x + k] = max(nr[x + k], v)
            rows.append(nr)
        return Glyph(rows, g.left, g.top, g.advance + extra)

    out.glyphs = {c: smear(g) for c, g in font.glyphs.items()}
    out.missing = smear(font.missing) if font.missing else None
    return out


# Mac Roman control codes the system fonts draw as symbols (menus use them).
MAC_SYMBOLS = {0x11: 0x2318, 0x12: 0x2713, 0x13: 0x25C6, 0x14: 0xF8FF}


# Characters outside Mac Roman that RetroBoot's text uses (or that browsers produce, like
# the narrow no-break space in formatted times), drawn with the glyph Mac OS itself would
# have used: Mac OS 9 writes "1024 x 768" and plain hyphens and spaces.
ALIASES = {0x00D7: 0x78, 0x2010: 0x2D, 0x2011: 0x2D, 0x2212: 0x2D, 0x2009: 0x20, 0x202F: 0x20}


def mac_to_unicode(code: int) -> int:
    return MAC_SYMBOLS.get(code) or ord(bytes([code]).decode("mac_roman"))


def text_width(font: BitFont, text: str) -> int:
    w = 0
    for ch in text.encode("mac_roman", "replace"):
        g = font.glyph(ch)
        w += g.advance if g else 0
    return w
