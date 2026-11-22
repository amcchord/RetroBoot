"""Is the Finder on screen?  In any Appearance theme (hostd/reports/finderdet-2026-10-13.md).

hostd's startup probe (``Supervisor._probe``, a QMP screendump every 2 s) decides when Mac
OS has reached the Finder.  The startup timeline's "Finder ready", the power-key flow, the
laptop keys, the Bluetooth keyboard assistant, the AirPort password window and the
accelerated boot's clock hand-off all wait for it.

What Mac OS 9.2.2 draws (lab frames of ten looks: Platinum and its Black & White
variation, a desktop picture, a window open at startup, Aqua Theme Beta, Gamma, Aerial,
Hi-Tech Neon, Drawing Board on Parchment, Gizmo in the Zone):
1. the startup gray, then the desktop (pattern or picture) for the extension parade;
2. an empty menu bar (the Process Manager's), then the menu-bar clock;
3. in one frame, the Finder's menus: the Apple menu at the left, File Edit View Window
   Special Help, and the application menu (the Finder's icon and name) at the right edge.
Themes change the bar's colours, texture, font, border and height (20 rows, 22 with Drawing
Board, Gizmo and Hi-Tech, which also adds a line in row 22), not this layout.

``finder_on_screen`` returns the rule that recognized the Finder, or None:

* ``platinum``: ``qmp.finder_menu_bar``, ClassicMac's rule as before (a bright bar with dark
  titles spread over a third of the width).  It goes first, so Platinum is decided on
  exactly the same frames, at the same cost, as before.
* ``layout``: the menu bar's layout, in any theme.  Ink is a sharp step in brightness
  between neighbouring pixels of rows 3-16, either way (dark titles on a light bar or
  light on dark); a theme's texture or gradient changes gradually.  The 6 columns at each
  end are left out (Mac OS rounds the screen's top corners in black, in every frame).
  Ink less than 8 px apart is one item, and items less than 24 px apart one group (a
  bar's titles; Hi-Tech draws them as adjoining buttons).  The bar is the Finder's when
  - an item ends within 40 px of the right edge (the application menu);
  - the first group (the menus) begins within 34 px of the left edge (the Apple menu),
    reaches x = 180 and has ink in half the columns from 34 to 180 (the Finder's six
    titles end near x = 300 in every theme measured);
  - after it comes a stretch without ink of at least a fifth of the width (up to the
    clock or the application menu);
  - one of rows 17-24 is a single colour across the width: the bar's bottom edge or the
    desktop right below it, not a desktop picture behind the top rows.
  The empty bar, the clock, the watch cursor and the desktop never pass; the Finder's
  first frame does.  It runs only when ``platinum`` did not match, and stops at the first
  rule that fails (the right edge is checked first: empty until the Finder).

Standard library only.
"""
from __future__ import annotations

from .qmp import finder_menu_bar

INK_ROWS = (3, 17)          # the titles' rows (bars are 20-23 rows high)
INK_STEP = 48               # brightness step between neighbouring pixels that counts as ink (0-255)
ITEM_GAP = 7                # ink columns this close belong to one item
CORNER = 6                  # Mac OS rounds the screen's top corners in black: no item there
GROUP_GAP = 24              # a menu bar's titles are closer than this (13-16 px apart)
APPLE_X = 34                # the menus begin left of this (the Apple menu)
TITLES_END = 180            # the Finder's titles (File to Help) reach at least this far
TITLES_INK = 0.5            # with ink in at least this share of the columns from APPLE_X to there
APP_EDGE = 40               # the application menu ends this close to the right edge
EDGE_ROWS = range(17, 25)   # the bar's bottom edge (row 19, 21 or 22) or the desktop below it
EDGE_TOLERANCE = 16
EDGE_UNIFORM = 0.9


def _lum(rgb: bytes, o: int) -> int:
    return rgb[o] * 3 + rgb[o + 1] * 6 + rgb[o + 2]          # x10, 0-2550


def ink_columns(width: int, rgb: bytes, x0: int = 0, x1: int | None = None) -> list[int]:
    """Columns x0..x1-1 of the title rows with a sharp brightness step (ink edges), as a count per column."""
    x1 = width - CORNER if x1 is None else min(width - CORNER, x1)
    x0 = max(CORNER, x0)
    cols = [0] * width
    step = INK_STEP * 10
    for y in range(*INK_ROWS):
        base = y * width * 3
        prev = _lum(rgb, base + (x0 - 1) * 3)
        for x in range(x0, x1):
            cur = _lum(rgb, base + x * 3)
            if cur - prev >= step or prev - cur >= step:
                cols[x] += 1
                cols[x - 1] += 1
            prev = cur
    return cols


def items(cols: list[int], gap: int = ITEM_GAP) -> list[tuple[int, int]]:
    """Runs of ink columns, first and last column of each (columns closer than gap join)."""
    out: list[tuple[int, int]] = []
    start = last = None
    for x, c in enumerate(cols):
        if not c:
            continue
        if start is None:
            start = x
        elif x - last > gap:
            out.append((start, last))
            start = x
        last = x
    if start is not None:
        out.append((start, last))
    return out


def _uniform_row(width: int, rgb: bytes, y: int) -> bool:
    base = y * width * 3
    px = [(rgb[base + x * 3], rgb[base + x * 3 + 1], rgb[base + x * 3 + 2]) for x in range(0, width, 2)]
    med = [sorted(c)[len(c) // 2] for c in zip(*px)]
    near = sum(1 for p in px if abs(p[0] - med[0]) <= EDGE_TOLERANCE and abs(p[1] - med[1]) <= EDGE_TOLERANCE
               and abs(p[2] - med[2]) <= EDGE_TOLERANCE)
    return near >= EDGE_UNIFORM * len(px)


def layout(width: int, height: int, rgb: bytes) -> dict:
    """The menu-bar layout rule, with the measurements (for the lab and the tests)."""
    out: dict = {"finder": False}
    if width < 320 or height < 200 or len(rgb) < width * height * 3:
        out["why"] = "frame"
        return out
    split = width - 3 * APP_EDGE          # the right edge first: empty until the Finder's menus
    right = ink_columns(width, rgb, split, width)
    right_items = items(right)
    if not right_items or right_items[-1][1] < width - APP_EDGE:
        out["why"] = "no application menu"
        return out
    cols = ink_columns(width, rgb, 0, split)[:split] + right[split:]
    groups = items(cols, GROUP_GAP)
    menus = groups[0]
    out["menus"] = menus
    if menus[0] >= APPLE_X:
        out["why"] = "no Apple menu"
        return out
    covered = sum(1 for x in range(APPLE_X, TITLES_END) if cols[x]) / (TITLES_END - APPLE_X)
    out["titles_ink"] = round(covered, 2)
    if menus[1] < TITLES_END or covered < TITLES_INK:
        out["why"] = "too few titles"
        return out
    gap = (groups[1][0] if len(groups) > 1 else width) - menus[1]
    out["gap"] = gap
    if gap < width // 5:
        out["why"] = "no empty stretch"
        return out
    if not any(_uniform_row(width, rgb, y) for y in EDGE_ROWS if y < height):
        out["why"] = "no bar edge"
        return out
    out.update(finder=True, why="layout")
    return out


def finder_on_screen(width: int, height: int, rgb: bytes) -> str | None:
    """"platinum" or "layout" when this frame shows the Finder's menu bar, else None."""
    if finder_menu_bar(width, rgb):
        return "platinum"
    return "layout" if layout(width, height, rgb)["finder"] else None


def finder_state(width: int, height: int, rgb: bytes) -> dict:
    """finder_on_screen with the layout measurements (lab tools)."""
    if finder_menu_bar(width, rgb):
        return {"finder": True, "why": "platinum"}
    return layout(width, height, rgb)
