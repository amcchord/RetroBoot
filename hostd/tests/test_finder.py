"""The Finder in any Appearance theme (retroboot/finder.py, hostd/reports/finderdet-2026-10-13.md).

Synthetic frames after the lab's measurements of Mac OS 9.2.2 at 1024x768: the menu bar is
rows 0-19 with its bottom edge in row 19 (or a shadow below it); the Apple menu at x 14-29;
File Edit View Window Special Help from x 40 to 296; the clock at x 884-930; the
application menu ("Finder" and its icon) at x 946-1006; black rounded screen corners in
rows 0-4.  No Apple pixels: the tests draw their own bars and glyph blocks.
"""
import random
import unittest

import helpers  # noqa: F401  (sys.path)
from retroboot import finder
from retroboot.qmp import finder_menu_bar

W, H = 1024, 768
APPLE = [(14, 29)]
TITLES = [(40, 62), (76, 100), (113, 140), (154, 200), (213, 255), (270, 296)]
CLOCK = [(884, 930)]
APP_MENU = [(946, 1006)]
DESKTOP = (99, 99, 156)


class Frame:
    def __init__(self, w=W, h=H, desktop=DESKTOP):
        self.w, self.h = w, h
        self.px = bytearray(bytes(desktop) * (w * h))

    def put(self, x, y, c):
        o = (y * self.w + x) * 3
        self.px[o:o + 3] = bytes(c)

    def bar(self, row_colour, edge=(0, 0, 0), rows=19):
        """The menu bar: row_colour(x, y) for rows 0..rows-1, then its bottom edge row."""
        for y in range(rows):
            for x in range(self.w):
                self.put(x, y, row_colour(x, y))
        for x in range(self.w):
            self.put(x, rows, edge)
        return self

    def corners(self):
        for y in range(5):
            for x in range(5 - y):
                self.put(x, y, (0, 0, 0))
                self.put(self.w - 1 - x, y, (0, 0, 0))
        return self

    def ink(self, spans, colour, rows=(5, 15)):
        """Glyph-like ink: two columns of three in each span, rows 5-14."""
        for x0, x1 in spans:
            for x in range(x0, min(self.w, x1)):
                if (x - x0) % 3 != 2:
                    for y in range(*rows):
                        self.put(x, y, colour)
        return self

    def watch(self, x0=5, y0=3, colour=(0, 0, 0)):
        for y in range(y0, y0 + 13):
            for x in range(x0, x0 + 12):
                if (x + y) % 2:
                    self.put(x, y, colour)
        return self

    def bytes(self):
        return bytes(self.px)


def finder_bar(row_colour, ink, edge=(0, 0, 0), clock=True, app=True, apple=True, titles=TITLES, w=W, h=H):
    f = Frame(w, h).bar(row_colour, edge).corners()
    spans = (APPLE if apple else []) + titles + (CLOCK if clock else []) + (APP_MENU if app else [])
    if w != W:
        spans = [(a, b) for a, b in (APPLE if apple else []) + titles] + \
                ([(w - 140, w - 94)] if clock else []) + ([(w - 78, w - 18)] if app else [])
    return f.ink(spans, ink)


def platinum(x, y):
    return (221, 221, 221)


def gamma(x, y):           # a vertical gradient: light blue at the top, purple below
    return (max(0, 153 - 6 * y), max(0, 213 - 12 * y), 255 - 3 * y)


def dark(x, y):            # Hi-Tech: a dark bar with light titles
    return (51, 51, 51)


_rng = random.Random(7)
_CLOUD = [[_rng.randint(-14, 14) for _ in range(W)] for _ in range(20)]


def clouds(x, y):          # Aerial: a gray texture that changes gradually
    v = 153 + (_CLOUD[y][x] + _CLOUD[y][x - 1 if x else 0]) // 2 + ((x // 37) % 3) * 6
    return (v, v, v)


def photo(x, y):           # a desktop picture behind the top rows: busy everywhere
    v = _rng.randint(0, 255)
    return (v, v // 2, 40)


class LayoutRule(unittest.TestCase):
    def check(self, frame, rule, w=W, h=H):
        self.assertEqual(finder.finder_on_screen(w, h, frame.bytes()), rule, finder.layout(w, h, frame.bytes()))

    def test_platinum_is_decided_by_the_old_rule(self):
        f = finder_bar(platinum, (0, 0, 0), edge=(0, 0, 0))
        self.assertTrue(finder_menu_bar(W, f.bytes()))
        self.check(f, "platinum")
        self.assertTrue(finder.layout(W, H, f.bytes())["finder"])     # and by the layout as well

    def test_themes_by_their_layout(self):
        self.check(finder_bar(gamma, (0, 0, 0), edge=(53, 0, 73)), "layout")
        self.check(finder_bar(dark, (230, 230, 230), edge=(20, 20, 20)), "layout")
        self.check(finder_bar(clouds, (0, 0, 0), edge=(85, 85, 85)), "layout")
        self.check(finder_bar(lambda x, y: (240, 230, 200), (200, 0, 0), edge=(120, 100, 80)), "layout")

    def test_hi_tech_buttons_and_its_taller_bar(self):
        # Hi-Tech: the Apple menu and the titles are adjoining buttons on a 22-row bar, its
        # bottom edge a blue line in row 22
        f = Frame()
        for y in range(22):
            for x in range(W):
                f.put(x, y, (120 + 3 * y,) * 3)
        for x in range(W):
            f.put(x, 22, (80, 151, 255))
        buttons = [(8, 34), (34, 66), (66, 98), (98, 136), (136, 194), (194, 240), (240, 271), (869, 940),
                   (945, 1013)]
        for a, b in buttons:
            for y in range(1, 22):
                for x in range(a, b):
                    f.put(x, y, (10, 10, 10) if x in (a, b - 1) or y in (1, 21) else (57, 57, 57))
        f.ink([(a + 6, b - 6) for a, b in buttons], (230, 230, 230)).corners()
        self.check(f, "layout")
        self.assertEqual(finder.layout(W, H, f.bytes())["menus"], (7, 271))   # one group: the buttons
        # before the Finder: the clock's button only
        g = Frame()
        for y in range(23):
            for x in range(W):
                g.put(x, y, f.px[(y * W + x) * 3:(y * W + x) * 3 + 3] if x > 860 and x < 941 or y == 22 else
                      (120 + 3 * y,) * 3)
        self.check(g.corners(), None)

    def test_platinum_black_and_white_and_aqua(self):
        self.check(finder_bar(lambda x, y: (255, 255, 255), (0, 0, 0)), "platinum")
        # Aqua Theme Beta: a white bar whose edge is a shadow below it (rows 19-21)
        f = finder_bar(lambda x, y: (255, 255, 255), (0, 0, 0), edge=(255, 255, 247))
        self.check(f, "platinum")
        self.assertTrue(finder.layout(W, H, f.bytes())["finder"])

    def test_startup_frames_are_not_the_finder(self):
        corners = Frame().corners()
        self.check(corners, None)                                       # the desktop, corners only
        empty = Frame().bar(gamma, (53, 0, 73)).corners()
        self.check(empty, None)                                         # the Process Manager's empty bar
        self.check(Frame().bar(gamma, (53, 0, 73)).corners().ink(CLOCK, (0, 0, 0)), None)   # and its clock
        watch = Frame().bar(clouds, (85, 85, 85)).corners().ink(CLOCK, (0, 0, 0)).watch()
        self.check(watch, None)                                         # the watch cursor over the Apple menu
        self.assertEqual(finder.layout(W, H, watch.bytes())["why"], "no application menu")

    def test_each_part_of_the_layout_is_needed(self):
        self.assertEqual(finder.layout(W, H, finder_bar(gamma, (0, 0, 0), app=False).bytes())["why"],
                         "no application menu")
        self.assertEqual(finder.layout(W, H, finder_bar(gamma, (0, 0, 0), apple=False).bytes())["why"],
                         "no Apple menu")
        self.assertEqual(finder.layout(W, H, finder_bar(gamma, (0, 0, 0), titles=TITLES[:2]).bytes())["why"],
                         "too few titles")
        # ink all along the bar (no empty stretch): not a menu bar
        full = finder_bar(gamma, (0, 0, 0), titles=[(x, x + 22) for x in range(40, 880, 35)])
        self.assertEqual(finder.layout(W, H, full.bytes())["why"], "no empty stretch")

    def test_a_desktop_picture_behind_the_top_rows_is_not_a_bar(self):
        f = Frame()
        for y in range(H // 4):
            for x in range(W):
                f.put(x, y, photo(x, y))
        self.check(f, None)
        # even a smooth stretch of it with a Finder-like ink layout: its rows 17-24 are no bar edge
        for y in range(0, 17):
            for x in range(W):
                f.put(x, y, (30, 15, 40))
        f.ink(APPLE + TITLES + APP_MENU, (230, 230, 230))
        self.assertEqual(finder.layout(W, H, f.bytes())["why"], "no bar edge")

    def test_a_window_below_the_bar_and_the_cursor_in_it(self):
        f = finder_bar(gamma, (0, 0, 0), edge=(53, 0, 73))
        for y in range(22, 150):
            for x in range(5, 210):
                f.put(x, y, (255, 255, 255) if y > 40 else (200, 200, 200))
        f.watch(600, 2)                                     # the cursor in the bar's empty stretch
        self.check(f, "layout")

    def test_other_screen_sizes(self):
        for w, h in ((640, 480), (800, 600), (1280, 720), (1920, 1080)):
            f = finder_bar(gamma, (0, 0, 0), edge=(53, 0, 73), w=w, h=h)
            self.check(f, "layout", w, h)
            self.check(Frame(w, h).bar(gamma, (53, 0, 73)).corners(), None, w, h)

    def test_short_or_tiny_frames(self):
        self.assertIsNone(finder.finder_on_screen(W, H, b""))
        self.assertIsNone(finder.finder_on_screen(100, 80, bytes(100 * 80 * 3)))


if __name__ == "__main__":
    unittest.main()
