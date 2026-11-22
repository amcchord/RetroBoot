"""Palette canvas for drawing Platinum chrome from measured metric tables.

Colors are named by one character so tables read like the screen:
  K 1 2 3 4 5 6 7 8 9 a b c d e W   the 16 grays 00,11,...,EE,FF (all in the
                                     standard 8-bit Macintosh palette)
  A B C D E F G H                   the Appearance variation ramp (theme 'clut'
                                     200-219, 8 entries light to dark)
  h                                 the highlight color (selected text)
  ' ' or '.'                        transparent
"""
from __future__ import annotations

from .image import Image

GRAYS = {k: (v, v, v) for k, v in zip("K123456789abcdeW", range(0, 256, 17))}

# Defaults match Mac OS 9.2.1 "Mac OS Default": Lavender variation, Purple highlight.
LAVENDER = [(0xEE, 0xEE, 0xEE), (0xCC, 0xCC, 0xFF), (0x99, 0x99, 0xFF), (0x66, 0x66, 0xCC),
            (0x33, 0x33, 0x99), (0x00, 0x00, 0x88), (0x00, 0x00, 0x55), (0x00, 0x00, 0x00)]
PURPLE = (0xCC, 0xCC, 0xFF)


class Palette:
    def __init__(self, ramp=None, highlight=None, extra=None):
        self.colors = dict(GRAYS)
        for k, c in zip("ABCDEFGH", ramp or LAVENDER):
            self.colors[k] = tuple(c)
        self.colors["h"] = tuple(highlight or PURPLE)
        if extra:
            self.colors.update(extra)

    def rgba(self, ch: str):
        if ch in (" ", "."):
            return (0, 0, 0, 0)
        c = self.colors[ch]
        return (c[0], c[1], c[2], c[3] if len(c) > 3 else 255)


class Canvas:
    def __init__(self, w: int, h: int, pal: Palette, fill: str = " "):
        self.w, self.h, self.pal = w, h, pal
        self.cells = [[fill] * w for _ in range(h)]

    def px(self, x: int, y: int, c: str) -> None:
        if 0 <= x < self.w and 0 <= y < self.h and c not in ("", "_"):
            self.cells[y][x] = c

    def hline(self, x: int, y: int, n: int, c: str) -> None:
        for i in range(n):
            self.px(x + i, y, c)

    def vline(self, x: int, y: int, n: int, c: str) -> None:
        for i in range(n):
            self.px(x, y + i, c)

    def fill(self, x: int, y: int, w: int, h: int, c: str) -> None:
        for yy in range(h):
            self.hline(x, y + yy, w, c)

    def frame(self, x: int, y: int, w: int, h: int, c: str) -> None:
        self.hline(x, y, w, c)
        self.hline(x, y + h - 1, w, c)
        self.vline(x, y, h, c)
        self.vline(x + w - 1, y, h, c)

    def bevel(self, x: int, y: int, w: int, h: int, tl: str, br: str, corner: str | None = None) -> None:
        """One-pixel bevel: top/left in tl, bottom/right in br; optional off-corner color."""
        self.hline(x, y, w - 1, tl)
        self.vline(x, y, h - 1, tl)
        self.hline(x + 1, y + h - 1, w - 1, br)
        self.vline(x + w - 1, y + 1, h - 1, br)
        if corner:
            self.px(x + w - 1, y, corner)
            self.px(x, y + h - 1, corner)

    def table(self, x: int, y: int, rows: list[str] | str) -> None:
        """Stamp a letter table; '_' leaves the canvas unchanged."""
        if isinstance(rows, str):
            rows = [r for r in rows.strip("\n").split("\n")]
        for j, row in enumerate(rows):
            for i, c in enumerate(row):
                if c != "_":
                    self.px(x + i, y + j, c)

    def image(self) -> Image:
        img = Image(self.w, self.h)
        img.px = [self.pal.rgba(c) for row in self.cells for c in row]
        return img

    def text(self) -> str:
        return "\n".join("".join(r) for r in self.cells)
