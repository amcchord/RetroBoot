"""Compact binary bitmap-font format for rbui (presenter/ui/src/assetfont.c).

    "RBF1" u8 size, u8 ascent, u8 descent, u8 leading, u8 smooth, u8 reserved[3],
    u16 count, then per glyph (sorted by code point):
    u32 code point (Unicode), i8 advance, i8 left, i8 top, u8 w, u8 h,
    w*h bytes coverage 0..16 (row-major, top row first): 0 nothing, 1..15 blend
bg + (((fg - bg) * 17 * c) >> 8) per channel, 16 the text color.
All integers little-endian.  'top' is the distance from the baseline up to row 0.
"""
from __future__ import annotations

import struct

from .bitfont import ALIASES, BitFont, mac_to_unicode


def encode(font: BitFont) -> bytes:
    # One glyph per code point (rbui requires strictly increasing code points): Charcoal
    # has the Apple logo both at Mac Roman 0xF0 and at the menu symbol 0x14 (U+F8FF);
    # the Mac Roman glyph wins.
    have: dict[int, object] = {}
    for code, g in sorted(font.glyphs.items(), key=lambda t: (t[0] < 0x20, t[0])):
        have.setdefault(mac_to_unicode(code), g)
    for alias, target in ALIASES.items():
        if target in have and alias not in have:
            have[alias] = have[target]
    if font.missing is not None and 0xFFFD not in have:
        have[0xFFFD] = font.missing
    items = sorted(have.items(), key=lambda t: t[0])
    out = bytearray(b"RBF1")
    out += struct.pack("<BBBBBxxx", font.size, font.ascent, font.descent, font.leading, 1 if font.smooth else 0)
    out += struct.pack("<H", len(items))
    for u, g in items:
        h = len(g.rows)
        w = g.width
        out += struct.pack("<IbbbBB", u, max(-128, min(127, g.advance)), max(-128, min(127, g.left)),
                           max(-128, min(127, g.top)), w, h)
        for row in g.rows:
            out += bytes(row)
    return bytes(out)
