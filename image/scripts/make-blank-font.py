#!/usr/bin/env python3
"""Write a GRUB PF2 font whose glyphs are all blank.

RetroBoot uses gfxterm only to draw the checkerboard background. If GRUB ever
prints anything (an error on a broken disk, for example), this font makes the
text invisible on the product display instead of drawing Linux-looking text;
diagnostics come from the serial console and the presenter's own recovery path.
Codepoints outside the covered ranges would fall back to GRUB's built-in
"unknown glyph" box, so the ranges include everything GRUB's own messages use.

usage: make-blank-font.py OUT.pf2
"""
import struct
import sys

RANGES = [(0x0000, 0x07FF), (0x2000, 0x2BFF), (0xFFFD, 0xFFFD)]


def section(name, data):
    return name + struct.pack('>I', len(data)) + data


def main(out):
    codepoints = [cp for lo, hi in RANGES for cp in range(lo, hi + 1)]
    header = b''.join([
        section(b'FILE', b'PFF2'),
        section(b'NAME', b'RetroBoot Blank Regular 16\0'),
        section(b'FAMI', b'RetroBoot Blank\0'),
        section(b'WEIG', b'normal\0'),
        section(b'SLAN', b'normal\0'),
        section(b'PTSZ', struct.pack('>H', 16)),
        section(b'MAXW', struct.pack('>H', 8)),
        section(b'MAXH', struct.pack('>H', 16)),
        section(b'ASCE', struct.pack('>H', 12)),
        section(b'DESC', struct.pack('>H', 4)),
    ])
    chix_len = 9 * len(codepoints)
    data_offset = len(header) + 8 + chix_len + 8   # CHIX header+entries, DATA header
    # One shared glyph: 1x1 bitmap with the pixel off, 8 pixels advance.
    glyph = struct.pack('>HHhhh', 1, 1, 0, 0, 8) + b'\x00'
    chix = b''.join(struct.pack('>IBI', cp, 0, data_offset) for cp in codepoints)
    blob = header + b'CHIX' + struct.pack('>I', chix_len) + chix + b'DATA' + struct.pack('>I', 0xFFFFFFFF) + glyph
    assert blob.index(glyph, len(header) + 8 + chix_len) == data_offset
    with open(out, 'wb') as f:
        f.write(blob)


if __name__ == '__main__':
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
