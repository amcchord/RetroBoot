#!/usr/bin/env python3
"""Build RetroBoot's bitmap fonts from their .glyphs sources.

The .glyphs files under assets/fonts/src/ are the editable source of truth
(original pixel art drawn for RetroBoot). This tool turns each one into a
standard BDF font (assets/fonts/*.bdf, the distributable form), then turns the
BDF files into C tables for the rbui library (src/fonts_data.c). Optional PNG
previews help review glyph art.

Usage:
    fontgen.py [--check] [--preview DIR] SRC.glyphs...

Source format (UTF-8 text):

    # comment (outside glyph blocks only)
    font RetroBoot-System-12          XLFD-ish font name
    family RetroBoot System           FAMILY_NAME property
    weight Bold                       WEIGHT_NAME property
    cname system12                    C identifier suffix (rbui_font_<cname>)
    pixel_size 12
    ascent 12                         rows above the baseline
    descent 4                         rows below the baseline (line = ascent+descent)
    cap_height 9
    x_height 7
    spacing 1                         default blank columns after a glyph's art
    accent_gap 1                      blank rows between a base glyph and its accent

    glyph A                           one literal character, U+XXXX, or a component
    ..##..                            name (letters/digits/underscore, not emitted)
    .####.                            '#' = ink, '.' = blank. A row of '-' marks the
    ##..##                            baseline: rows above it sit on the baseline,
                                      rows below are descenders. Without a marker
                                      the last row sits on the baseline. A blank
                                      line ends the glyph.

    glyph space adv=4                 options: adv=N (total advance; default is
                                      art width + spacing), empty art allowed
    compose À A grave [dx=N] [dy=N] [below]
                                      accent drawn above (or below) the base glyph,
                                      centred on the base's ink
    alias U+00A0 space                copy another glyph
"""

import argparse
import os
import re
import struct
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.dirname(HERE)


class Glyph:
    """Bitmap with the baseline at row `base` (rows [0, base) are above it)."""

    def __init__(self, rows, base, adv=None):
        self.rows = rows          # list of str of '#'/'.' all the same width
        self.base = base
        self.width = len(rows[0]) if rows else 0
        self.adv = adv

    def ink(self):
        """(x0, y0, x1, y1) of ink relative to the art; None if empty.
        y is relative to the baseline (negative = above)."""
        xs, ys = [], []
        for r, row in enumerate(self.rows):
            for c, ch in enumerate(row):
                if ch == '#':
                    xs.append(c)
                    ys.append(r - self.base)
        if not xs:
            return None
        return min(xs), min(ys), max(xs), max(ys)

    def pixels(self):
        """Set of (x, y) ink pixels, y relative to the baseline."""
        out = set()
        for r, row in enumerate(self.rows):
            for c, ch in enumerate(row):
                if ch == '#':
                    out.add((c, r - self.base))
        return out


def glyph_from_pixels(pixels, width, adv):
    if not pixels:
        return Glyph([], 0, adv if adv is not None else width)
    y0 = min(y for _, y in pixels)
    y1 = max(y for _, y in pixels)
    w = max(width, max(x for x, _ in pixels) + 1)
    rows = []
    for y in range(y0, y1 + 1):
        rows.append(''.join('#' if (x, y) in pixels else '.' for x in range(w)))
    g = Glyph(rows, -y0, adv)
    g.width = w
    return g


class FontSource:
    def __init__(self, path):
        self.path = path
        self.props = {
            'font': 'RetroBoot', 'family': 'RetroBoot', 'weight': 'Medium',
            'cname': 'font', 'pixel_size': '12', 'ascent': '12', 'descent': '4',
            'cap_height': '9', 'x_height': '7', 'spacing': '1', 'accent_gap': '1',
            'copyright': 'Copyright (c) 2026 RetroBoot contributors',
            'notice': 'Original pixel art for RetroBoot. MIT License; see LICENSE-fonts.txt.',
        }
        self.named = {}       # key -> Glyph (all, including components)
        self.order = []       # emitted codepoints in definition order
        self.errors = []
        self._parse()

    def num(self, key):
        return int(self.props[key])

    def err(self, lineno, msg):
        self.errors.append('%s:%d: %s' % (self.path, lineno, msg))

    @staticmethod
    def key_codepoint(key):
        if re.fullmatch(r'U\+[0-9A-Fa-f]{4,6}', key):
            return int(key[2:], 16)
        if len(key) == 1:
            return ord(key)
        return None

    def _register(self, key, glyph, lineno):
        if key in self.named:
            self.err(lineno, 'duplicate glyph %r' % key)
            return
        self.named[key] = glyph
        cp = self.key_codepoint(key)
        if cp is not None:
            self.order.append((cp, key))
        elif not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
            self.err(lineno, 'bad glyph key %r' % key)

    def _parse(self):
        with open(self.path, encoding='utf-8') as f:
            lines = f.read().split('\n')
        i = 0
        while i < len(lines):
            line = lines[i].rstrip()
            lineno = i + 1
            i += 1
            if not line or line.startswith('#'):
                continue
            parts = line.split()
            word = parts[0]
            if word == 'glyph':
                if len(parts) < 2:
                    self.err(lineno, 'glyph needs a key')
                    continue
                key = parts[1]
                opts = dict(p.split('=', 1) for p in parts[2:] if '=' in p)
                art = []
                while i < len(lines) and lines[i].strip():
                    art.append(lines[i].strip())
                    i += 1
                rows, base = [], None
                for r in art:
                    if set(r) == {'-'}:
                        if base is not None:
                            self.err(lineno, 'two baseline markers in %r' % key)
                        base = len(rows)
                        continue
                    if not set(r) <= {'#', '.'}:
                        self.err(lineno, 'bad art row %r in %r' % (r, key))
                        continue
                    rows.append(r)
                if rows and len({len(r) for r in rows}) != 1:
                    self.err(lineno, 'ragged art in %r' % key)
                    continue
                if base is None:
                    base = len(rows)
                adv = int(opts['adv']) if 'adv' in opts else None
                g = Glyph(rows, base, adv)
                self._register(key, g, lineno)
            elif word == 'compose':
                if len(parts) < 4:
                    self.err(lineno, 'compose KEY BASE ACCENT [dx=] [dy=] [below]')
                    continue
                key, base_key, acc_key = parts[1], parts[2], parts[3]
                opts = dict(p.split('=', 1) for p in parts[4:] if '=' in p)
                below = 'below' in parts[4:]
                base = self.named.get(base_key)
                acc = self.named.get(acc_key)
                if base is None or acc is None:
                    self.err(lineno, 'compose %r: unknown %r' % (key, base_key if base is None else acc_key))
                    continue
                g = self._compose(base, acc, int(opts.get('dx', 0)), int(opts.get('dy', 0)), below)
                self._register(key, g, lineno)
            elif word == 'alias':
                if len(parts) != 3 or parts[2] not in self.named:
                    self.err(lineno, 'alias KEY EXISTING')
                    continue
                src = self.named[parts[2]]
                self._register(parts[1], Glyph(list(src.rows), src.base, src.adv), lineno)
            elif word in self.props:
                value = line[len(word):].strip()
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]
                self.props[word] = value
            else:
                self.err(lineno, 'unknown directive %r' % word)

    def _compose(self, base, acc, dx, dy, below):
        bi = base.ink()
        ai = acc.ink()
        pixels = set(base.pixels())
        if bi is None or ai is None:
            return glyph_from_pixels(pixels, base.width, self.advance(base))
        bx0, by0, bx1, by1 = bi
        ax0, ay0, ax1, ay1 = ai
        bw, aw = bx1 - bx0 + 1, ax1 - ax0 + 1
        ox = bx0 + (bw - aw + 1) // 2 - ax0 + dx
        gap = self.num('accent_gap')
        if below:
            oy = 0 - ay0 + dy                 # accent's top row just below the baseline
        else:
            oy = (by0 - gap - 1) - ay1 + dy   # accent's bottom row above the base
        for x, y in acc.pixels():
            pixels.add((x + ox, y + oy))
        return glyph_from_pixels(pixels, base.width, self.advance(base))

    def advance(self, g):
        if g.adv is not None:
            return g.adv
        return g.width + self.num('spacing')

    def glyphs(self):
        """[(cp, Glyph)] sorted by codepoint."""
        return sorted(((cp, self.named[key]) for cp, key in self.order), key=lambda t: t[0])

    def validate(self):
        asc, desc = self.num('ascent'), self.num('descent')
        for cp, g in self.glyphs():
            ink = g.ink()
            if ink is None:
                continue
            if -ink[1] > asc:
                self.errors.append('%s: U+%04X rises %d rows above the baseline (ascent %d)' % (self.path, cp, -ink[1], asc))
            if ink[3] >= desc:
                self.errors.append('%s: U+%04X drops %d rows below the baseline (descent %d)' % (self.path, cp, ink[3] + 1, desc))


# ---------------------------------------------------------------------------
# BDF
# ---------------------------------------------------------------------------

def write_bdf(src, path):
    asc, desc = src.num('ascent'), src.num('descent')
    glyphs = src.glyphs()
    out = []
    p = src.props
    size = src.num('pixel_size')
    name = '-RetroBoot-%s-%s-R-Normal--%d-%d-72-72-P-%d-ISO10646-1' % (
        p['family'].replace(' ', ''), p['weight'], asc + desc, size * 10, 70)
    maxw = max([g.width for _, g in glyphs] + [1])
    out.append('STARTFONT 2.1')
    out.append('COMMENT Generated by presenter/ui/tools/fontgen.py from %s' % os.path.basename(src.path))
    out.append('COMMENT %s' % p['notice'])
    out.append('FONT %s' % name)
    out.append('SIZE %d 72 72' % size)
    out.append('FONTBOUNDINGBOX %d %d 0 %d' % (maxw, asc + desc, -desc))
    props = [
        ('FAMILY_NAME', '"%s"' % p['family']),
        ('WEIGHT_NAME', '"%s"' % p['weight']),
        ('SLANT', '"R"'),
        ('SPACING', '"P"'),
        ('PIXEL_SIZE', str(asc + desc)),
        ('POINT_SIZE', str(size * 10)),
        ('RESOLUTION_X', '72'),
        ('RESOLUTION_Y', '72'),
        ('FONT_ASCENT', str(asc)),
        ('FONT_DESCENT', str(desc)),
        ('CAP_HEIGHT', p['cap_height']),
        ('X_HEIGHT', p['x_height']),
        ('CHARSET_REGISTRY', '"ISO10646"'),
        ('CHARSET_ENCODING', '"1"'),
        ('DEFAULT_CHAR', '65533'),
        ('COPYRIGHT', '"%s"' % p['copyright']),
        ('NOTICE', '"%s"' % p['notice']),
    ]
    out.append('STARTPROPERTIES %d' % len(props))
    for k, v in props:
        out.append('%s %s' % (k, v))
    out.append('ENDPROPERTIES')
    out.append('CHARS %d' % len(glyphs))
    for cp, g in glyphs:
        adv = src.advance(g)
        ink = g.ink()
        out.append('STARTCHAR uni%04X' % cp)
        out.append('ENCODING %d' % cp)
        out.append('SWIDTH %d 0' % (adv * 1000 // size))
        out.append('DWIDTH %d 0' % adv)
        if ink is None:
            out.append('BBX 0 0 0 0')
            out.append('BITMAP')
        else:
            x0, y0, x1, y1 = ink
            w, h = x1 - x0 + 1, y1 - y0 + 1
            out.append('BBX %d %d %d %d' % (w, h, x0, -y1 - 1))
            out.append('BITMAP')
            px = g.pixels()
            nbytes = (w + 7) // 8
            for y in range(y0, y1 + 1):
                v = 0
                for x in range(w):
                    if (x0 + x, y) in px:
                        v |= 1 << (nbytes * 8 - 1 - x)
                out.append('%0*X' % (nbytes * 2, v))
        out.append('ENDCHAR')
    out.append('ENDFONT')
    with open(path, 'w', encoding='ascii') as f:
        f.write('\n'.join(out) + '\n')


def read_bdf(path):
    """Minimal BDF reader: returns (props, [(cp, adv, w, h, xoff, yoff, rows[int])])."""
    props, glyphs = {}, []
    with open(path, encoding='ascii') as f:
        lines = [l.rstrip('\n') for l in f]
    i = 0
    in_props = False
    while i < len(lines):
        parts = lines[i].split()
        i += 1
        if not parts:
            continue
        if parts[0] == 'STARTPROPERTIES':
            in_props = True
            continue
        if parts[0] == 'ENDPROPERTIES':
            in_props = False
            continue
        if in_props:
            props[parts[0]] = ' '.join(parts[1:]).strip('"')
            continue
        if parts[0] == 'STARTCHAR':
            cp = adv = None
            bbx = (0, 0, 0, 0)
            rows = []
            while i < len(lines):
                p = lines[i].split()
                i += 1
                if not p:
                    continue
                if p[0] == 'ENCODING':
                    cp = int(p[1])
                elif p[0] == 'DWIDTH':
                    adv = int(p[1])
                elif p[0] == 'BBX':
                    bbx = tuple(int(v) for v in p[1:5])
                elif p[0] == 'BITMAP':
                    for _ in range(bbx[1]):
                        rows.append(int(lines[i].strip(), 16))
                        i += 1
                elif p[0] == 'ENDCHAR':
                    break
            glyphs.append((cp, adv, bbx[0], bbx[1], bbx[2], bbx[3], rows))
    return props, glyphs


def c_font_block(cname, bdf_path):
    props, glyphs = read_bdf(bdf_path)
    glyphs.sort(key=lambda g: g[0])
    bits = []
    recs = []
    for cp, adv, w, h, xoff, yoff, rows in glyphs:
        off = len(bits)
        nbytes = (w + 7) // 8
        for r in rows:
            for b in range(nbytes):
                bits.append((r >> (8 * (nbytes - 1 - b))) & 0xFF)
        top = -(yoff + h)       # first bitmap row relative to the baseline
        recs.append('    {0x%04X, %d, %d, %d, %d, %d, %d},' % (cp, adv, xoff, top, w, h, off))
    out = []
    out.append('/* %s (%s) */' % (props.get('FAMILY_NAME', cname), os.path.basename(bdf_path)))
    out.append('static const uint8_t bits_%s[] = {' % cname)
    for k in range(0, len(bits), 16):
        out.append('    ' + ', '.join('0x%02X' % b for b in bits[k:k + 16]) + ',')
    if not bits:
        out.append('    0')
    out.append('};')
    out.append('static const rbf_glyph glyphs_%s[] = {' % cname)
    out.extend(recs)
    out.append('};')
    out.append('static const rbf_font rbui_font_%s = {' % cname)
    out.append('    "%s", %s, %s, %s, %s, %d, glyphs_%s, bits_%s, 0,' % (
        cname, props['FONT_ASCENT'], props['FONT_DESCENT'],
        props.get('CAP_HEIGHT', '0'), props.get('X_HEIGHT', '0'), len(recs), cname, cname))
    out.append('};')
    return '\n'.join(out)


# ---------------------------------------------------------------------------
# PNG preview
# ---------------------------------------------------------------------------

def write_png(path, w, h, pix):
    """pix: bytearray of RGB, row-major."""
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw += pix[y * w * 3:(y + 1) * w * 3]

    def chunk(t, d):
        return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xFFFFFFFF)
    data = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
    data += chunk(b'IDAT', zlib.compress(bytes(raw), 9)) + chunk(b'IEND', b'')
    with open(path, 'wb') as f:
        f.write(data)


class Canvas:
    def __init__(self, w, h, bg):
        self.w, self.h = w, h
        self.pix = bytearray(bytes(bg) * (w * h))

    def put(self, x, y, c):
        if 0 <= x < self.w and 0 <= y < self.h:
            o = (y * self.w + x) * 3
            self.pix[o:o + 3] = bytes(c)

    def rect(self, x, y, w, h, c):
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                self.put(xx, yy, c)


def preview(src, path):
    """Glyph table (each glyph magnified with guide lines) plus sample text at 1x and 2x."""
    asc, desc = src.num('ascent'), src.num('descent')
    cap, xh = src.num('cap_height'), src.num('x_height')
    line = asc + desc
    glyphs = src.glyphs()
    lookup = dict(glyphs)
    Z = 4
    cols = 16
    cell_w = (max([src.advance(g) for _, g in glyphs] + [8]) + 2) * Z
    cell_h = (line + 2) * Z
    rows_n = (len(glyphs) + cols - 1) // cols
    samples = [
        'The quick brown fox jumps over the lazy dog. 0123456789',
        'THE QUICK BROWN FOX JUMPS OVER THE LAZY DOG! (#$%&*@)',
        'RetroBoot needs to restart. Mac OS could not start.',
        'Wi-Fi Password: Disk "Macintosh HD" 512 GB [OK] {Cancel}',
        'Pairing code 482 913 - https://retroboot.local/ ~ a_b|c\\d/e',
        'ÀÉÎÕÜÇÑ àéîõüçñ ßæøå '
        '©®™ …• “quoted” ‘it’s’ ¿¡ «» ±×÷ €£¥',
    ]

    def text_w(s):
        return sum(src.advance(lookup[ord(ch)]) if ord(ch) in lookup else 6 for ch in s)
    sample_w = max(text_w(s) for s in samples)
    W = max(cols * cell_w + 16, sample_w * 2 + 16, sample_w + 16)
    H = rows_n * cell_h + 16 + len(samples) * (line + 2) + 8 + len(samples) * (line + 2) * 2 + 16
    cv = Canvas(W, H, (0xE6, 0xE6, 0xE6))

    def draw_glyph(g, ox, base_y, scale, color):
        for (x, y) in g.pixels():
            cv.rect(ox + x * scale, base_y + y * scale, scale, scale, color)

    for n, (cp, g) in enumerate(glyphs):
        cx = 8 + (n % cols) * cell_w
        cy = 8 + (n // cols) * cell_h
        cv.rect(cx, cy, cell_w - 2, cell_h - 2, (0xFF, 0xFF, 0xFF))
        by = cy + Z + asc * Z
        adv = src.advance(g)
        cv.rect(cx + Z, cy + Z, adv * Z, line * Z, (0xF0, 0xF0, 0xFF))
        for yy, col in ((0, (0x99, 0xCC, 0x99)), (-cap, (0xFF, 0xCC, 0xCC)), (-xh, (0xCC, 0xCC, 0xFF))):
            cv.rect(cx, by + yy * Z, cell_w - 2, 1, col)
        draw_glyph(g, cx + Z, by, Z, (0, 0, 0))
    y = 8 + rows_n * cell_h + 8

    def draw_text(s, x, y_top, scale):
        for ch in s:
            g = lookup.get(ord(ch))
            if g is None:
                cv.rect(x, y_top + (asc - cap) * scale, 5 * scale, cap * scale, (0xFF, 0, 0))
                x += 6 * scale
                continue
            draw_glyph(g, x, y_top + asc * scale, scale, (0, 0, 0))
            x += src.advance(g) * scale
    for s in samples:
        draw_text(s, 8, y, 1)
        y += line + 2
    y += 8
    for s in samples:
        draw_text(s, 8, y, 2)
        y += (line + 2) * 2
    write_png(path, cv.w, cv.h, cv.pix)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('sources', nargs='*')
    ap.add_argument('--preview', metavar='DIR', help='write <cname>.png glyph sheets here')
    ap.add_argument('--check', action='store_true', help='validate only, write nothing')
    ap.add_argument('--c-out', default=os.path.join(UI_DIR, 'src', 'fonts_data.c'))
    ap.add_argument('--bdf-dir', default=os.path.join(UI_DIR, 'assets', 'fonts'))
    args = ap.parse_args()
    sources = args.sources or sorted(
        os.path.join(UI_DIR, 'assets', 'fonts', 'src', f)
        for f in os.listdir(os.path.join(UI_DIR, 'assets', 'fonts', 'src')) if f.endswith('.glyphs'))
    fonts = [FontSource(p) for p in sources]
    errors = []
    for f in fonts:
        f.validate()
        errors += f.errors
    if errors:
        print('\n'.join(errors), file=sys.stderr)
        return 1
    if args.check:
        return 0
    blocks = []
    for f in fonts:
        bdf = os.path.join(args.bdf_dir, f.props['font'] + '.bdf')
        write_bdf(f, bdf)
        blocks.append(c_font_block(f.props['cname'], bdf))
        if args.preview:
            os.makedirs(args.preview, exist_ok=True)
            preview(f, os.path.join(args.preview, f.props['cname'] + '.png'))
        print('%s: %d glyphs -> %s' % (os.path.basename(f.path), len(f.glyphs()), os.path.relpath(bdf)))
    if not args.sources:
        with open(args.c_out, 'w', encoding='utf-8') as out:
            out.write('/* Generated by tools/fontgen.py from the BDF files in assets/fonts. Do not edit.\n'
                      ' * Fonts: original RetroBoot pixel art, MIT License (assets/fonts/LICENSE-fonts.txt). */\n')
            out.write('#include "font.h"\n\n')
            out.write('\n\n'.join(blocks) + '\n\n')
            out.write('const rbf_font *const rbui_font_table[] = {\n')
            for f in fonts:
                out.write('    &rbui_font_%s,\n' % f.props['cname'])
            out.write('    0\n};\n')
        print('wrote', os.path.relpath(args.c_out))
    return 0


if __name__ == '__main__':
    sys.exit(main())
