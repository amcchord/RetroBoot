"""Draw text with a BitFont the way QuickDraw does (srcOr with 16-level blending)."""
from __future__ import annotations

from .bitfont import BitFont
from .image import Image
from .ttfwriter import mac_blend


def blend(bg, fg, level: int):
    """Mac OS 9 smoothed text: per channel bg + (((fg - bg) * 17 * level) >> 8); 16 = fg."""
    return tuple(mac_blend(bg[i], fg[i], level) for i in range(3)) + (255,)


def draw_text(img: Image, font: BitFont, x: int, baseline: int, text: str, color=(0, 0, 0)) -> int:
    """Draw at pen x and baseline y; returns the pen x after the text."""
    pen = x
    for ch in text.encode("mac_roman", "replace"):
        g = font.glyph(ch)
        if g is None:
            continue
        for r, row in enumerate(g.rows):
            y = baseline - g.top + r
            for c, v in enumerate(row):
                if v:
                    px = pen + g.left + c
                    if 0 <= px < img.w and 0 <= y < img.h:
                        img.set(px, y, blend(img.get(px, y), color, v))
        pen += g.advance
    return pen
