#!/usr/bin/env python3
"""Write a GRUB PF2 font from a RetroBoot BDF font, every pixel scaled up.

The installer's startup options (hold Shift at power-on, grub/grub-iso.cfg.in)
are the one place GRUB shows text; they use RetroBoot's own MIT bitmap font
(presenter/ui/assets/fonts) at twice its size so the menu stays readable on
dense laptop panels (GRUB does not scale fonts). The box-drawing characters
GRUB draws the menu frame with are added (the BDF has none), sized to a full
cell so the frame lines join. Standard library only.

usage: make-menu-font.py FONT.bdf SCALE OUT.pf2 [NAME]
"""
import struct
import sys


def parse_bdf(path):
    glyphs = {}
    ascent = descent = 0
    cur = None
    bitmap = None
    with open(path, encoding='ascii', errors='replace') as f:
        for line in f:
            parts = line.split()
            if not parts:
                continue
            key = parts[0]
            if key == 'FONT_ASCENT':
                ascent = int(parts[1])
            elif key == 'FONT_DESCENT':
                descent = int(parts[1])
            elif key == 'STARTCHAR':
                cur = {}
            elif key == 'ENCODING' and cur is not None:
                cur['cp'] = int(parts[1])
            elif key == 'DWIDTH' and cur is not None:
                cur['dw'] = int(parts[1])
            elif key == 'BBX' and cur is not None:
                cur['w'], cur['h'], cur['x'], cur['y'] = (int(v) for v in parts[1:5])
            elif key == 'BITMAP' and cur is not None:
                bitmap = []
            elif key == 'ENDCHAR' and cur is not None:
                cur['rows'] = bitmap or []
                if cur.get('cp', -1) >= 0:
                    glyphs[cur['cp']] = cur
                cur = bitmap = None
            elif bitmap is not None:
                bitmap.append(int(key, 16) if key else 0)
    return glyphs, ascent, descent


def pixels(g):
    """The glyph as rows of 0/1 (BDF rows are padded to whole bytes, MSB first)."""
    nbytes = (g['w'] + 7) // 8
    out = []
    for r in g['rows'][:g['h']]:
        out.append([(r >> (8 * nbytes - 1 - x)) & 1 for x in range(g['w'])])
    while len(out) < g['h']:
        out.append([0] * g['w'])
    return out


def box_glyphs(cell_w, ascent, descent):
    """U+2500.. light box drawing: lines through the middle of a full cell."""
    h = ascent + descent
    mx, my = cell_w // 2, h // 2

    def make(cp, left, right, up, down):
        px = [[0] * cell_w for _ in range(h)]
        for x in range(cell_w):
            if (left and x <= mx) or (right and x >= mx):
                px[my][x] = 1
        for y in range(h):
            if (up and y <= my) or (down and y >= my):
                px[y][mx] = 1
        return {'cp': cp, 'w': cell_w, 'h': h, 'x': 0, 'y': -descent, 'dw': cell_w, 'px': px}

    spec = {0x2500: (1, 1, 0, 0), 0x2502: (0, 0, 1, 1), 0x250C: (0, 1, 0, 1), 0x2510: (1, 0, 0, 1),
            0x2514: (0, 1, 1, 0), 0x2518: (1, 0, 1, 0), 0x251C: (0, 1, 1, 1), 0x2524: (1, 0, 1, 1)}
    return [make(cp, *v) for cp, v in spec.items()]


def section(name, data):
    return name + struct.pack('>I', len(data)) + data


def main(src, scale, out, name):
    glyphs, ascent, descent = parse_bdf(src)
    for g in glyphs.values():
        g['px'] = pixels(g)
    cell = max(g['dw'] for g in glyphs.values())
    for g in box_glyphs(cell, ascent, descent):
        glyphs.setdefault(g['cp'], g)
    s = scale
    items = []
    for cp in sorted(glyphs):
        g = glyphs[cp]
        w, h = g['w'] * s, g['h'] * s
        bits = []
        for row in g['px']:
            line = [b for b in row for _ in range(s)]
            for _ in range(s):
                bits.extend(line)
        data = bytearray((len(bits) + 7) // 8)
        for i, b in enumerate(bits):
            if b:
                data[i // 8] |= 0x80 >> (i % 8)
        items.append((cp, struct.pack('>HHhhh', w, h, g['x'] * s, g['y'] * s, g['dw'] * s) + bytes(data)))
    maxw = max(glyphs[cp]['w'] for cp in glyphs) * s
    maxh = max(glyphs[cp]['h'] for cp in glyphs) * s
    header = b''.join([
        section(b'FILE', b'PFF2'),
        section(b'NAME', name.encode() + b'\0'),
        section(b'FAMI', b'RetroBoot Menu\0'),
        section(b'WEIG', b'normal\0'),
        section(b'SLAN', b'normal\0'),
        section(b'PTSZ', struct.pack('>H', 12 * s)),
        section(b'MAXW', struct.pack('>H', maxw)),
        section(b'MAXH', struct.pack('>H', maxh)),
        section(b'ASCE', struct.pack('>H', ascent * s)),
        section(b'DESC', struct.pack('>H', descent * s)),
    ])
    chix_len = 9 * len(items)
    offset = len(header) + 8 + chix_len + 8
    chix, blob = b'', b''
    for cp, data in items:
        chix += struct.pack('>IBI', cp, 0, offset + len(blob))
        blob += data
    with open(out, 'wb') as f:
        f.write(header + b'CHIX' + struct.pack('>I', chix_len) + chix + b'DATA' + struct.pack('>I', 0xFFFFFFFF) + blob)


if __name__ == '__main__':
    if len(sys.argv) not in (4, 5):
        sys.exit(__doc__)
    main(sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4] if len(sys.argv) == 5 else 'RetroBoot Menu Regular 24')
