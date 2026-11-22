"""Anti-aliased wordmarks, set the way Mac OS 9's startup box has its name.

Mac OS 9.2's startup box (System 'PICT' -16506, ``startup.py``) carries its
"Mac OS 9.2" as pre-rendered artwork: black anti-aliased type on white,
reduced to the 8-bit system palette's sixteen grays (#000000, #111111 ...
#FFFFFF).  RetroBoot's installer shows the same box with "RetroBoot" in that
place (ARCHITECTURE 4.4.2); this module sets such a line from an outline
font through FreeType (ctypes, no Python dependencies): unhinted outlines,
an optional horizontal scale (condensing) and stroke adjustment (weight),
per-glyph placement at 1/64 pixel, exact-area coverage, then the same
sixteen-gray reduction.

The font program comes from the user's own media or a free font; this module
holds no font data.  ``tools/wordfit.py`` measures the parameters against the
startup box (lab).
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass, field

from .image import Image
from .ttraster import FT_FaceRec, FT_GlyphSlotRec, FT_Outline, FT_Vector, library

FT_LOAD_NO_HINTING = 0x2
FT_LOAD_NO_BITMAP = 0x8
FT_RENDER_MODE_NORMAL = 0


class FT_Matrix(ctypes.Structure):
    _fields_ = [("xx", ctypes.c_long), ("xy", ctypes.c_long), ("yx", ctypes.c_long), ("yy", ctypes.c_long)]


_bound = False


def _lib():
    global _bound
    lib, handle = library()
    if not _bound:
        lib.FT_Set_Transform.argtypes = [ctypes.POINTER(FT_FaceRec), ctypes.POINTER(FT_Matrix), ctypes.POINTER(FT_Vector)]
        lib.FT_Set_Transform.restype = None
        lib.FT_Outline_EmboldenXY.argtypes = [ctypes.POINTER(FT_Outline), ctypes.c_long, ctypes.c_long]
        lib.FT_Set_Char_Size.argtypes = [ctypes.POINTER(FT_FaceRec), ctypes.c_long, ctypes.c_long, ctypes.c_uint,
                                         ctypes.c_uint]
        _bound = True
    return lib, handle


@dataclass
class Style:
    """How a wordmark is set.  size: pixels per em; hscale: horizontal scale;
    bold: stroke change in pixels (negative thins), applied to x and y with
    bold_y; tracking: pixels added after every glyph; kern: extra pixels
    before glyph i (by index)."""
    size: float
    hscale: float = 1.0
    bold: float = 0.0
    bold_y: float | None = None
    tracking: float = 0.0
    kern: dict[int, float] = field(default_factory=dict)


class OutlineFont:
    """An outline font program (Type 1, bare CFF, TrueType or OpenType)."""

    def __init__(self, data: bytes):
        self.lib, self.handle = _lib()
        self._buf = ctypes.create_string_buffer(data, len(data))
        self.face = ctypes.POINTER(FT_FaceRec)()
        if self.lib.FT_New_Memory_Face(self.handle, self._buf, len(data), 0, ctypes.byref(self.face)):
            raise ValueError("FreeType could not open the font")

    def has(self, text: str) -> bool:
        return all(ch == " " or self.lib.FT_Get_Char_Index(self.face, ord(ch)) for ch in text)

    def missing(self, text: str) -> str:
        return "".join(sorted({ch for ch in text if ch != " " and not self.lib.FT_Get_Char_Index(self.face, ord(ch))}))

    def glyph(self, ch: str, st: Style, frac_x: float, frac_y: float = 0.0):
        """Coverage rows (0..255), left, top (pixels, y up) and advance (pixels)
        of one glyph whose origin is at (frac_x, frac_y) within its pixel."""
        lib, face = self.lib, self.face
        if lib.FT_Set_Char_Size(face, int(round(st.size * 64)), 0, 72, 72):
            raise ValueError("FT_Set_Char_Size failed")
        m = FT_Matrix(int(round(st.hscale * 65536)), 0, 0, 65536)
        d = FT_Vector(int(round(frac_x * 64)), int(round(frac_y * 64)))
        lib.FT_Set_Transform(face, ctypes.byref(m), ctypes.byref(d))
        gi = lib.FT_Get_Char_Index(face, ord(ch))
        if lib.FT_Load_Glyph(face, gi, FT_LOAD_NO_HINTING | FT_LOAD_NO_BITMAP):
            raise ValueError(f"cannot load {ch!r}")
        slot = face.contents.glyph.contents
        adv = slot.advance.x / 64.0
        bx = st.bold
        by = st.bold if st.bold_y is None else st.bold_y
        if bx or by:
            lib.FT_Outline_EmboldenXY(ctypes.byref(slot.outline), int(round(bx * 64)), int(round(by * 64)))
            adv += bx
        if lib.FT_Render_Glyph(ctypes.byref(slot), FT_RENDER_MODE_NORMAL):
            raise ValueError(f"cannot render {ch!r}")
        bm = slot.bitmap
        rows = [[bm.buffer[y * bm.pitch + x] for x in range(bm.width)] for y in range(bm.rows)]
        return rows, slot.bitmap_left, slot.bitmap_top, adv


def set_line(font: OutlineFont, text: str, st: Style) -> tuple[list[list[int]], float, float, float]:
    """Coverage of a line at origin (0, 0) on a canvas with some margin:
    returns (rows, origin_x, baseline_y, width) where the pen starts at
    (origin_x, baseline_y) in canvas pixels and width is the advance sum."""
    pen = 0.0
    placed = []
    for i, ch in enumerate(text):
        pen += st.kern.get(i, 0.0)
        if ch == " ":
            if font.lib.FT_Get_Char_Index(font.face, 32):
                pen += font.glyph(" ", st, 0.0)[3]
            else:
                pen += st.size * 0.25 * st.hscale
            pen += st.tracking
            continue
        ix = int(pen // 1)
        rows, left, top, adv = font.glyph(ch, st, pen - ix)
        placed.append((rows, ix + left, top))
        pen += adv + st.tracking
    width = pen - st.tracking
    if not placed:
        return [], 0.0, 0.0, width
    x0 = min(x for _, x, _ in placed) - 2
    x1 = max(x + (len(r[0]) if r else 0) for r, x, _ in placed) + 2
    top = max(t for _, _, t in placed) + 2
    bottom = min(t - len(r) for r, _, t in placed) - 2
    W, H = x1 - x0, top - bottom
    canvas = [[0] * W for _ in range(H)]
    for rows, x, t in placed:
        for yy, row in enumerate(rows):
            cy = top - t + yy
            for xx, v in enumerate(row):
                if v:
                    cx = x - x0 + xx
                    canvas[cy][cx] = min(255, canvas[cy][cx] + v)
    return canvas, float(-x0), float(top), width


def quantize16(v: int) -> int:
    """A coverage 0..255 as one of the sixteen grays' levels (0..15)."""
    return min(15, (v * 15 + 127) // 255)


def to_image(rows: list[list[int]], fg=(0, 0, 0), bg=(255, 255, 255), levels: int = 16) -> Image:
    """Black-on-white artwork, reduced to `levels` coverage steps (16: the
    system palette's grays, as the startup box's own name is)."""
    h = len(rows)
    w = len(rows[0]) if rows else 0
    img = Image(w, h)
    for y in range(h):
        for x in range(w):
            v = rows[y][x]
            if levels:
                q = (v * (levels - 1) + 127) // 255
                a = q / (levels - 1)
            else:
                a = v / 255.0
            img.px[y * w + x] = tuple(int(round(bg[i] + (fg[i] - bg[i]) * a)) for i in range(3)) + (255,)
    return img
