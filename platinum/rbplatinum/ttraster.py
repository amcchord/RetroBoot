"""TrueType glyphs as Mac OS 9 draws them, through libfreetype (ctypes, no Python deps).

Mac OS 9 hints TrueType outlines with Apple's interpreter at the pixel size
(FreeType's v35 interpreter reproduces it) and, for "Smooth all fonts on
screen" sizes, scan-converts the hinted outline on a 4x4 sample grid per pixel:
coverage = covered samples, 16 clamped to 15, blended linearly between the
background and the text color.  Without smoothing the hinted outline is
scan-converted with the TrueType rules (FreeType's monochrome rasterizer).

Needs libfreetype.so.6 (Debian libfreetype6) or RB_FREETYPE_LIB.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import math
import os

from .bitfont import BitFont, Glyph

FT_LOAD_NO_BITMAP = 0x8
FT_LOAD_RENDER = 0x4
FT_LOAD_MONOCHROME = 0x1000
FT_LOAD_TARGET_MONO = 2 << 16
FT_RENDER_MODE_MONO = 2
FT_ENCODING_APPLE_ROMAN = int.from_bytes(b"armn", "big")


class FT_Vector(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class FT_Generic(ctypes.Structure):
    _fields_ = [("data", ctypes.c_void_p), ("finalizer", ctypes.c_void_p)]


class FT_BBox(ctypes.Structure):
    _fields_ = [("xMin", ctypes.c_long), ("yMin", ctypes.c_long), ("xMax", ctypes.c_long), ("yMax", ctypes.c_long)]


class FT_Glyph_Metrics(ctypes.Structure):
    _fields_ = [(n, ctypes.c_long) for n in ("width", "height", "horiBearingX", "horiBearingY", "horiAdvance",
                                             "vertBearingX", "vertBearingY", "vertAdvance")]


class FT_Bitmap(ctypes.Structure):
    _fields_ = [("rows", ctypes.c_uint), ("width", ctypes.c_uint), ("pitch", ctypes.c_int),
                ("buffer", ctypes.POINTER(ctypes.c_ubyte)), ("num_grays", ctypes.c_ushort),
                ("pixel_mode", ctypes.c_ubyte), ("palette_mode", ctypes.c_ubyte), ("palette", ctypes.c_void_p)]


class FT_Outline(ctypes.Structure):
    _fields_ = [("n_contours", ctypes.c_short), ("n_points", ctypes.c_short),
                ("points", ctypes.POINTER(FT_Vector)), ("tags", ctypes.POINTER(ctypes.c_ubyte)),
                ("contours", ctypes.POINTER(ctypes.c_short)), ("flags", ctypes.c_int)]


class FT_GlyphSlotRec(ctypes.Structure):
    _fields_ = [("library", ctypes.c_void_p), ("face", ctypes.c_void_p), ("next", ctypes.c_void_p),
                ("glyph_index", ctypes.c_uint), ("generic", FT_Generic), ("metrics", FT_Glyph_Metrics),
                ("linearHoriAdvance", ctypes.c_long), ("linearVertAdvance", ctypes.c_long),
                ("advance", FT_Vector), ("format", ctypes.c_uint), ("bitmap", FT_Bitmap),
                ("bitmap_left", ctypes.c_int), ("bitmap_top", ctypes.c_int), ("outline", FT_Outline)]


class FT_FaceRec(ctypes.Structure):
    _fields_ = [("num_faces", ctypes.c_long), ("face_index", ctypes.c_long), ("face_flags", ctypes.c_long),
                ("style_flags", ctypes.c_long), ("num_glyphs", ctypes.c_long), ("family_name", ctypes.c_char_p),
                ("style_name", ctypes.c_char_p), ("num_fixed_sizes", ctypes.c_int), ("available_sizes", ctypes.c_void_p),
                ("num_charmaps", ctypes.c_int), ("charmaps", ctypes.c_void_p), ("generic", FT_Generic),
                ("bbox", FT_BBox), ("units_per_EM", ctypes.c_ushort), ("ascender", ctypes.c_short),
                ("descender", ctypes.c_short), ("height", ctypes.c_short), ("max_advance_width", ctypes.c_short),
                ("max_advance_height", ctypes.c_short), ("underline_position", ctypes.c_short),
                ("underline_thickness", ctypes.c_short), ("glyph", ctypes.POINTER(FT_GlyphSlotRec))]


_lib = None


def library():
    """(ctypes lib, FT_Library handle); raises OSError when FreeType is missing."""
    global _lib
    if _lib is None:
        names = [os.environ.get("RB_FREETYPE_LIB"), "libfreetype.so.6", ctypes.util.find_library("freetype")]
        lib = None
        for n in names:
            if not n:
                continue
            try:
                lib = ctypes.CDLL(n)
                break
            except OSError:
                continue
        if lib is None:
            raise OSError("libfreetype not found (install libfreetype6 or set RB_FREETYPE_LIB)")
        handle = ctypes.c_void_p()
        if lib.FT_Init_FreeType(ctypes.byref(handle)):
            raise OSError("FT_Init_FreeType failed")
        lib.FT_New_Memory_Face.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_long,
                                           ctypes.POINTER(ctypes.POINTER(FT_FaceRec))]
        lib.FT_Set_Char_Size.argtypes = [ctypes.POINTER(FT_FaceRec), ctypes.c_long, ctypes.c_long, ctypes.c_uint, ctypes.c_uint]
        lib.FT_Select_Charmap.argtypes = [ctypes.POINTER(FT_FaceRec), ctypes.c_uint]
        lib.FT_Get_Char_Index.argtypes = [ctypes.POINTER(FT_FaceRec), ctypes.c_ulong]
        lib.FT_Get_Char_Index.restype = ctypes.c_uint
        lib.FT_Load_Glyph.argtypes = [ctypes.POINTER(FT_FaceRec), ctypes.c_uint, ctypes.c_int32]
        lib.FT_Render_Glyph.argtypes = [ctypes.POINTER(FT_GlyphSlotRec), ctypes.c_int]
        lib.FT_Property_Set.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p]
        v35 = ctypes.c_uint(35)
        lib.FT_Property_Set(handle, b"truetype", b"interpreter-version", ctypes.byref(v35))
        _lib = (lib, handle)
    return _lib


def available() -> bool:
    try:
        library()
        return True
    except OSError:
        return False


# ---- exact 4x4-sample scan conversion of a hinted outline --------------------

def _segments(pts, tags, ends):
    start = 0
    for e in ends:
        P = pts[start:e + 1]
        T = [t & 1 for t in tags[start:e + 1]]
        start = e + 1
        n = len(P)
        if n < 2:
            continue
        seq = []
        for i in range(n):
            seq.append((P[i], T[i]))
            j = (i + 1) % n
            if not T[i] and not T[j]:
                seq.append((((P[i][0] + P[j][0]) / 2, (P[i][1] + P[j][1]) / 2), 1))
        if not any(t for _p, t in seq):
            continue
        k = next(i for i, (_p, t) in enumerate(seq) if t)
        seq = seq[k:] + seq[:k]
        m = len(seq)
        cur = seq[0][0]
        i = 1
        while i <= m:
            p, t = seq[i % m]
            if t:
                yield ("L", cur, p)
                cur = p
                i += 1
            else:
                p2 = seq[(i + 1) % m][0]
                yield ("Q", cur, p, p2)
                cur = p2
                i += 2


def _monotone(segs):
    """Split into y-monotone pieces: ('L', p0, p1) or ('Q', p0, c, p1, t0, t1)."""
    out = []
    for s in segs:
        if s[0] == "L":
            if s[1][1] != s[2][1]:
                out.append(s)
            continue
        (x0, y0), (cx, cy), (x1, y1) = s[1], s[2], s[3]
        a = y0 - 2 * cy + y1
        ts = [0.0]
        if a != 0:
            te = (y0 - cy) / a
            if 0 < te < 1:
                ts.append(te)
        ts.append(1.0)
        for ta, tb in zip(ts, ts[1:]):
            out.append(("Q", s[1], s[2], s[3], ta, tb))
    return out


def _cross(seg, y):
    if seg[0] == "L":
        (x0, y0), (x1, y1) = seg[1], seg[2]
        if (y0 <= y < y1) or (y1 <= y < y0):
            return x0 + (y - y0) / (y1 - y0) * (x1 - x0), (1 if y1 > y0 else -1)
        return None
    (x0, y0), (cx, cy), (x1, y1), ta, tb = seg[1], seg[2], seg[3], seg[4], seg[5]
    a, b, c = y0 - 2 * cy + y1, 2 * (cy - y0), y0
    ya, yb = a * ta * ta + b * ta + c, a * tb * tb + b * tb + c
    if ya == yb or not ((ya <= y < yb) or (yb <= y < ya)):
        return None
    if a == 0:
        t = (y - c) / b
    else:
        disc = max(b * b - 4 * a * (c - y), 0.0)
        r = math.sqrt(disc)
        cands = [(-b + r) / (2 * a), (-b - r) / (2 * a)]
        t = min(cands, key=lambda t: 0 if ta - 1e-9 <= t <= tb + 1e-9 else abs(t - (ta + tb) / 2) + 1)
    x = (1 - t) ** 2 * x0 + 2 * (1 - t) * t * cx + t * t * x1
    return x, (1 if yb > ya else -1)


CURVE_STEPS = 8   # chords per quadratic: the closest match to Mac OS 9 found so far (see README)


def _flatten(segs):
    out = []
    for s in segs:
        if s[0] == "L":
            out.append(s)
            continue
        (x0, y0), (cx, cy), (x1, y1) = s[1], s[2], s[3]
        prev = (x0, y0)
        for k in range(1, CURVE_STEPS + 1):
            u = k / CURVE_STEPS
            p = ((1 - u) ** 2 * x0 + 2 * (1 - u) * u * cx + u * u * x1, (1 - u) ** 2 * y0 + 2 * (1 - u) * u * cy + u * u * y1)
            out.append(("L", prev, p))
            prev = p
    return out


def coverage4(pts, tags, ends) -> tuple[list[list[int]], int, int]:
    """16-level coverage of an outline in pixel units; returns (rows, left, top)."""
    if not pts:
        return [], 0, 0
    segs = _monotone(_flatten(_segments(pts, tags, ends)))
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x0, x1 = math.floor(min(xs)), math.ceil(max(xs))
    y0, y1 = math.floor(min(ys)), math.ceil(max(ys))
    W, H = max(1, x1 - x0), max(1, y1 - y0)
    cov = [[0] * W for _ in range(H)]
    for j in range(H * 4):
        ys_ = y1 - (j + 0.5) / 4
        cr = sorted(c for c in (_cross(s, ys_) for s in segs) if c)
        if not cr:
            continue
        row = cov[j // 4]
        for i in range(W * 4):
            xs_ = x0 + (i + 0.5) / 4
            w = 0
            for x, d in cr:
                if x < xs_:
                    w += d
                else:
                    break
            if w:
                row[i // 4] += 1
    # Mac OS keeps full coverage (16 of 16 samples) distinct from 15: level 16 draws the
    # text color itself, 1..15 blend (ttfwriter.mac_blend).
    rows = [[min(v, 16) for v in r] for r in cov]
    # trim empty borders
    while rows and not any(rows[0]):
        rows.pop(0)
        y1 -= 1
    while rows and not any(rows[-1]):
        rows.pop()
    if not rows:
        return [], 0, 0
    left = 0
    while all(r[left] == 0 for r in rows):
        left += 1
    right = W
    while all(r[right - 1] == 0 for r in rows):
        right -= 1
    return [r[left:right] for r in rows], x0 + left, y1


class TrueTypeFace:
    def __init__(self, sfnt: bytes):
        self.lib, self.handle = library()
        self._buf = ctypes.create_string_buffer(sfnt, len(sfnt))
        self.face = ctypes.POINTER(FT_FaceRec)()
        if self.lib.FT_New_Memory_Face(self.handle, self._buf, len(sfnt), 0, ctypes.byref(self.face)):
            raise ValueError("FreeType could not open the font")
        self.roman = self.lib.FT_Select_Charmap(self.face, FT_ENCODING_APPLE_ROMAN) == 0

    def strike(self, family: str, size: int, smooth: bool) -> BitFont:
        lib, face = self.lib, self.face
        if lib.FT_Set_Char_Size(face, size * 64, 0, 72, 72):
            raise ValueError("FT_Set_Char_Size failed")
        f = face.contents
        ppem_scale = size / f.units_per_EM
        ascent = round(f.ascender * ppem_scale)
        descent = round(-f.descender * ppem_scale)
        font = BitFont(family, size, "plain", ascent, descent, max(0, round(f.height * ppem_scale) - ascent - descent),
                       smooth=smooth)
        for code in [0x11, 0x12, 0x13, 0x14] + list(range(0x20, 0x100)):
            if code == 0x7F:
                continue
            if self.roman:
                gi = lib.FT_Get_Char_Index(face, code)
            else:
                from .bitfont import mac_to_unicode
                gi = lib.FT_Get_Char_Index(face, mac_to_unicode(code))
            if gi == 0:
                continue
            if lib.FT_Load_Glyph(face, gi, FT_LOAD_NO_BITMAP | FT_LOAD_TARGET_MONO):
                continue
            slot = f.glyph.contents
            adv = slot.advance.x >> 6
            o = slot.outline
            pts = [(o.points[i].x / 64.0, o.points[i].y / 64.0) for i in range(o.n_points)]
            tags = [o.tags[i] for i in range(o.n_points)]
            ends = [o.contours[i] for i in range(o.n_contours)]
            if smooth:
                rows, left, top = coverage4(pts, tags, ends)
            else:
                rows, left, top = self._mono(slot)
            font.glyphs[code] = Glyph(rows, left, top, adv)
        return font

    def _mono(self, slot):
        if self.lib.FT_Render_Glyph(ctypes.byref(slot), FT_RENDER_MODE_MONO):
            return [], 0, 0
        bm = slot.bitmap
        rows = [[16 if bm.buffer[y * bm.pitch + x // 8] & (0x80 >> (x % 8)) else 0 for x in range(bm.width)]
                for y in range(bm.rows)]
        return rows, slot.bitmap_left, slot.bitmap_top
