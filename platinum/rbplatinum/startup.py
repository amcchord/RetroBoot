"""Mac OS 9.2's startup box for RetroBoot's installer (ARCHITECTURE 4.4.2).

While the installer loads, the initramfs splash and the presenter draw a
replica of Mac OS 9's startup screen box with RetroBoot's name and loading
progress (presenter/src/installer_splash.c).  This module builds its private
art, ``installer-startup.rbis``, from the user's own disk:

* the box: System 'PICT' -16506 (422 x 323, 8-bit colour; -16505 and -16504
  are its gray and 1-bit forms, -16503..-16501 a 321 x 227 one), with the
  name "Mac OS 9.2" erased from its white panel.  Verified against frames of
  the real Mac OS 9.2.2 guest at six sizes: the box is exactly this picture;
* the name: "RetroBoot", pre-rendered from a *wordmark kit* (fonts and the
  measured setting, below), or none (the C code then draws RetroBoot's own);
* the status lines "Welcome to RetroBoot" and "Starting Up...": Charcoal 12
  without smoothing, the font Mac OS draws its own lines in, centred as it
  centres them (baseline 279);
* the progress bar (162 x 14 at 130, 290): the measured Appearance bar of
  chrome.py, empty, full and the end of a fill, in the theme's colours.

The wordmark kit is a private folder (never committed) with ``kit.json``::

    {"text": "RetroBoot",
     "fonts": {"agl": "AppleGaramond-Light.cff", ...},
     "style": {"font": "agl", "size": 78.37, "hscale": 0.996, "bold": 0.15, "tracking": -3.54},
     "glyphs": {"B": {"font": "itc", "size": 78.24, "hscale": 0.8925, "bold": -0.9}},
     "baseline": 224.25, "center": 210.0}

``style`` and ``baseline``/``center`` come from ``tools/wordfit.py`` against
the box's own name; ``glyphs`` overrides fonts per character.

RBIS (little-endian; presenter/src/installer_splash.h reads it): "RBIS",
u16 version 1, u16 count, then per entry char tag[4], i16 x, i16 y, u32
offset, u32 length of an RBI1 image ('RBI1', u16 w, u16 h, RGBA rows).
"""
from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

from . import chrome, pict, wordmark
from .bitfont import BitFont
from .canvas import Canvas, Palette
from .image import Image
from .textdraw import draw_text

BOX_PICT = -16506
BOX_W, BOX_H = 422, 323
PANEL = (36, 27, 349, 222)          # the white panel's black outline (x, y, w, h)
STATUS = (4, 262, 414, 26)          # the status line's strip
STATUS_BASELINE = 279
BAR = (130, 290, 162, 14)
BAR_STEPS = BAR[2] - 4
CAP_W = 6
WELCOME = "Welcome to RetroBoot"
STARTING = "Starting Up…"
NAME = "RetroBoot"


class StartupError(ValueError):
    pass


def box_picture(system) -> Image:
    data = system.data("PICT", BOX_PICT) if system else None
    if not data:
        raise StartupError("no startup box picture (System PICT -16506)")
    img = pict.decode(data)
    if (img.w, img.h) != (BOX_W, BOX_H):
        raise StartupError(f"startup box is {img.w}x{img.h}, not {BOX_W}x{BOX_H}")
    return img


def name_block(box: Image) -> tuple[int, int]:
    """Rows (first, last) of the name: the lowest block of ink in the panel,
    below at least three blank rows."""
    x0, y0, w, h = PANEL
    rows = [any(box.get(x, y)[:3] != (255, 255, 255) for x in range(x0 + 1, x0 + w - 1))
            for y in range(y0 + 1, y0 + h - 1)]
    last = max(i for i, r in enumerate(rows) if r)
    first = last
    while first > 0 and (rows[first - 1] or any(rows[max(0, first - 4):first - 1])):
        first -= 1
    return y0 + 1 + first, y0 + 1 + last


def erase_name(box: Image) -> tuple[int, int, int, int]:
    """Paint the name's rows of the panel white; returns that rectangle."""
    first, _last = name_block(box)
    x0, y0, w, h = PANEL
    rect = (x0 + 1, first - 2, w - 2, y0 + h - 1 - (first - 2))
    box.fill(rect[0], rect[1], rect[2], rect[3], (255, 255, 255, 255))
    return rect


class Kit:
    def __init__(self, path: str | Path):
        self.dir = Path(path)
        self.spec = json.loads((self.dir / "kit.json").read_text())
        self.fonts = {k: wordmark.OutlineFont((self.dir / v).read_bytes()) for k, v in self.spec["fonts"].items()}
        self.hashes = {v: hashlib.sha256((self.dir / v).read_bytes()).hexdigest() for v in self.spec["fonts"].values()}

    def style(self, ch: str) -> tuple[wordmark.OutlineFont, wordmark.Style]:
        s = dict(self.spec["style"])
        s.update(self.spec.get("glyphs", {}).get(ch, {}))
        font = self.fonts[s.pop("font")]
        s.pop("tracking", None)
        return font, wordmark.Style(**s)

    def render(self, rect: tuple[int, int, int, int]) -> Image:
        """The name on white, filling rect (box coordinates), its ink centred
        on the kit's centre and standing on its baseline."""
        text = self.spec.get("text", NAME)
        tracking = self.spec["style"].get("tracking", 0.0)
        # pen positions (fractional), then the ink extent to centre it
        pens, pen = [], 0.0
        for ch in text:
            font, st = self.style(ch)
            pens.append(pen)
            pen += font.glyph(ch, st, 0.0)[3] + tracking
        ink = []
        for ch, p in zip(text, pens):
            font, st = self.style(ch)
            rows, left, top, _ = font.glyph(ch, st, p - int(p // 1))
            cols = [x for x in range(len(rows[0]) if rows else 0) if any(r[x] for r in rows)]
            if cols:
                ink += [int(p // 1) + left + cols[0], int(p // 1) + left + cols[-1] + 1]
        dx = self.spec["center"] - (min(ink) + max(ink)) / 2.0
        X, Y, W, H = rect
        cov = [[0] * W for _ in range(H)]
        base = self.spec["baseline"] - Y
        by = int(base // 1)
        for ch, p in zip(text, pens):
            font, st = self.style(ch)
            x = p + dx - X
            ix = int(x // 1)
            rows, left, top, _ = font.glyph(ch, st, x - ix, -(base - by))
            for yy, row in enumerate(rows):
                y = by - top + yy
                if not 0 <= y < H:
                    continue
                for xx, v in enumerate(row):
                    cx = ix + left + xx
                    if v and 0 <= cx < W:
                        cov[y][cx] = min(255, cov[y][cx] + v)
        return wordmark.to_image(cov)


def status_strip(box: Image, font: BitFont, text: str, welcome: bool) -> Image:
    x0, y0, w, h = STATUS
    strip = box.crop(x0, y0, w, h)
    adv = sum(font.glyph(c).advance for c in text.encode("mac_roman") if font.glyph(c))
    # Mac OS centres "Welcome to Mac OS" one pixel left of the box's centre,
    # "Starting Up..." on it (both measured on the guest).
    pen = (BOX_W - 2 - adv) // 2 if welcome else (BOX_W - adv) // 2
    draw_text(strip, font, pen - x0, STATUS_BASELINE - y0, text)
    return strip


def bar_pieces(pal: Palette) -> tuple[Image, Image, Image]:
    empty = chrome.progress(BAR[2], pal, None)
    full = chrome.progress(BAR[2], pal, None, fill_px=BAR_STEPS)
    cap = Canvas(CAP_W, BAR[3] - 4, pal)
    for j in range(BAR[3] - 4):
        for i, ch in enumerate(chrome.PROG_END[j] + "8"):
            cap.px(i, j, ch)
    return empty.image(), full.image(), cap.image()


def _rbi(img: Image) -> bytes:
    return b"RBI1" + struct.pack("<HH", img.w, img.h) + bytes(v for c in img.px for v in c)


def pack(entries: list[tuple[str, int, int, Image]]) -> bytes:
    head = b"RBIS" + struct.pack("<HH", 1, len(entries))
    off = len(head) + 16 * len(entries)
    table, blobs = b"", b""
    for tag, x, y, img in entries:
        blob = _rbi(img)
        table += tag.encode("ascii") + struct.pack("<hhII", x, y, off + len(blobs), len(blob))
        blobs += blob
    return head + table + blobs


def compose(entries: dict, phase: str, fill: int | None = None) -> Image:
    """The box as the C code draws it (rb_isplash_compose), for previews."""
    out = Image(BOX_W, BOX_H)
    out.paste(entries["BOX "][2], 0, 0)
    if "WORD" in entries:
        x, y, img = entries["WORD"]
        out.paste(img, x, y)
    x, y, img = entries["WELC" if phase == "welcome" else "STRT"]
    out.paste(img, x, y)
    if phase == "welcome":
        return out
    n = max(5, min(BAR_STEPS, fill if fill is not None else BAR_STEPS))
    e, f, c = entries["BARE"][2], entries["BARF"][2], entries["BCAP"][2]
    cap0 = 2 + n - 3
    for yy in range(BAR[3]):
        for xx in range(BAR[2]):
            src = e.get(xx, yy)
            if 2 <= yy < BAR[3] - 2 and 2 <= xx < BAR[2] - 2:
                if xx < cap0:
                    src = f.get(xx, yy)
                elif xx < cap0 + CAP_W:
                    src = c.get(xx - cap0, yy - 2)
            out.set(BAR[0] + xx, BAR[1] + yy, src)
    return out


def build(system, charcoal12: BitFont | None, pal: Palette, kit: Kit | None) -> tuple[bytes, dict, dict]:
    """(installer-startup.rbis, previews {name: Image}, manifest entry)."""
    if charcoal12 is None:
        raise StartupError("no Charcoal 12 strike")
    box = box_picture(system)
    rect = erase_name(box)
    e, f, c = bar_pieces(pal)
    entries = {"BOX ": (0, 0, box),
               "WELC": (STATUS[0], STATUS[1], status_strip(box, charcoal12, WELCOME, True)),
               "STRT": (STATUS[0], STATUS[1], status_strip(box, charcoal12, STARTING, False)),
               "BARE": (BAR[0], BAR[1], e), "BARF": (BAR[0], BAR[1], f), "BCAP": (0, 0, c)}
    meta = {"file": "startup/installer-startup.rbis", "box": [BOX_W, BOX_H], "name_rect": list(rect),
            "status_rect": list(STATUS), "bar": list(BAR), "wordmark": None}
    if kit is not None:
        entries["WORD"] = (rect[0], rect[1], kit.render(rect))
        meta["wordmark"] = {"text": kit.spec.get("text", NAME), "fonts": kit.hashes,
                            "style": kit.spec["style"], "glyphs": kit.spec.get("glyphs", {})}
    order = ["BOX ", "WORD", "WELC", "STRT", "BARE", "BARF", "BCAP"]
    data = pack([(t, *entries[t]) for t in order if t in entries])
    previews = {"welcome": compose(entries, "welcome"), "starting": compose(entries, "starting", BAR_STEPS * 2 // 5)}
    return data, previews, meta
