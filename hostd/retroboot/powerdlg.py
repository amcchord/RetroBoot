"""Mac OS's power-key dialog: when Mac OS can take the key, and whether its dialog is up.

hostd asks Mac OS to shut down or restart the way a user does: the ADB power
key opens "Are you sure you want to shut down your computer now?" (Restart,
Sleep, Cancel, Shut Down) and Return or R answers it.  What Mac OS 9.2.2 does
with the key (hostd/reports/powerdlg-2026-10-12.md, lab and hardware):

* While it starts (extension parade, then about 4.5 s of Finder startup work
  in the lab) the key waits in its event queue; the dialog appears only when
  Mac OS goes idle, up to 12 s after the key.  A Return or R sent before the
  dialog is drawn goes to the Finder, and the request then fails.
* Once idle, it draws the dialog 0.4-1 s after the key.
* A second power key while the dialog is up, or before a busy Mac OS has
  drawn it, closes it again.  Keys have been lost on hardware too.

So hostd waits until Mac OS is idle (``GuestLoad``: QEMU's TCG thread mostly
napping), presses the key once, and verifies the dialog by its content
(``find_power_dialog``): a framed light-gray box with text and a row of push
buttons that was not on the screen before the key, or whose arrival dimmed
the menu titles.  Finder drawing, a screen-size change or a window never
qualifies.  A key is pressed again only when Mac OS stayed idle for a while
after it with nothing dialog-like appearing, i.e. when it dropped the key.

Frames are ``(width, height, rgb)`` tuples as QMP screendumps give them
(``qmp.read_ppm``): guest pixels, independent of the presenter's scale.
Stdlib only; the scans use bytes operations so a 1920x1440 frame takes a few
tens of milliseconds.
"""
from __future__ import annotations

import os
from collections import Counter
from pathlib import Path

Frame = tuple[int, int, bytes]

MENU_BAR = 22            # rows of the menu bar (and its bottom line) left out of every check
DARK = 80                # a pixel is dark when all three channels are below this
EDGE_MIN = 0.85          # share of dark pixels along a frame edge
BOX_W = (240, 760)       # Mac OS 9's Shut Down dialog is 402 x 104 (incl. shadow) in English
BOX_H = (64, 320)
BUTTON_W = (40, 150)     # push-button outlines (Restart, Sleep, Cancel, Shut Down: about 80 px)

_DARK_TABLE = bytes(1 if v < DARK else 0 for v in range(256))


def dark_mask(frame: Frame) -> bytes:
    """One byte per pixel: 1 where the pixel is dark (r, g and b all below DARK)."""
    w, h, rgb = frame
    n = w * h
    rgb = rgb[:n * 3]
    acc = None
    for c in range(3):
        m = int.from_bytes(rgb[c::3].translate(_DARK_TABLE), "little")
        acc = m if acc is None else acc & m
    return acc.to_bytes(n, "little")


def _runs(mask: bytes, start: int, end: int, min_len: int) -> list[tuple[int, int]]:
    """Runs of 1 bytes of at least min_len in mask[start:end] as (first, last) offsets."""
    out = []
    pat = b"\x01" * min_len
    i = mask.find(pat, start, end)
    while i >= 0:
        j = mask.find(b"\x00", i, end)
        j = end if j < 0 else j
        out.append((i, j - 1))
        i = mask.find(pat, j, end)
    return out


def _col_dark(mask: bytes, w: int, x: int, y0: int, y1: int) -> float:
    if y1 <= y0:
        return 0.0
    col = mask[y0 * w + x:y1 * w + x:w]
    return col.count(1) / len(col) if col else 0.0


def _row_dark(mask: bytes, w: int, y: int, x0: int, x1: int) -> float:
    if x1 <= x0:
        return 0.0
    seg = mask[y * w + x0:y * w + x1]
    return seg.count(1) / len(seg) if seg else 0.0


def _interior(frame: Frame, mask: bytes, box: tuple[int, int, int, int]) -> dict:
    """The box's inside: its commonest colour, that colour's share, and the dark share."""
    w, h, rgb = frame
    x0, y0, x1, y1 = box
    xa, xb, ya, yb = x0 + 4, x1 - 4, y0 + 4, y1 - 4
    counts: Counter = Counter()
    total = dark = 0
    for y in range(ya, yb, 2):
        row = rgb[(y * w + xa) * 3:(y * w + xb) * 3]
        px = [row[i:i + 3] for i in range(0, len(row), 6)]
        counts.update(px)
        total += len(px)
        dark += mask[y * w + xa:y * w + xb:2].count(1)
    if not total:
        return {"share": 0.0, "color": None, "dark": 0.0}
    color, n = counts.most_common(1)[0]
    return {"share": n / total, "color": tuple(color), "dark": dark / total}


def _buttons(mask: bytes, w: int, box: tuple[int, int, int, int]) -> int:
    """Push buttons in a row: the most button-wide dark runs on one line of the box's lower half."""
    x0, y0, x1, y1 = box
    best = 0
    for y in range(y1 - 5, (y0 + y1) // 2, -1):
        runs = _runs(mask, y * w + x0 + 3, y * w + x1 - 3, BUTTON_W[0])
        n = sum(1 for a, b in runs if b - a + 1 <= BUTTON_W[1])
        best = max(best, n)
    return best


def button_boxes(frame: Frame, box: tuple[int, int, int, int], mask: bytes | None = None) -> list[tuple]:
    """The push buttons of a dialog ``box``, left to right, as (x0, y0, x1, y1): the dark
    outlines of button width on the row of the lower half that has the most of them,
    extended up and down over the rows where the same outline continues."""
    w, h, _ = frame
    mask = mask if mask is not None else dark_mask(frame)
    x0, y0, x1, y1 = box
    best, best_y = [], None
    for y in range(y1 - 5, (y0 + y1) // 2, -1):
        runs = [(a - y * w, b - y * w) for a, b in _runs(mask, y * w + x0 + 3, y * w + x1 - 3, BUTTON_W[0])
                if b - a + 1 <= BUTTON_W[1]]
        if len(runs) > len(best):
            best, best_y = runs, y
    out = []
    for a, b in best:
        def same(y):
            return any(abs(c - y * w - a) <= 3 and abs(d - y * w - b) <= 3
                       for c, d in _runs(mask, y * w + max(x0, a - 4), y * w + min(x1, b + 5), (b - a) // 2))
        top = bottom = best_y
        for y in range(best_y - 1, max(y0, best_y - 40), -1):
            if same(y):
                top = y
        for y in range(best_y + 1, min(y1, best_y + 40)):
            if same(y):
                bottom = y
        out.append((a, top, b + 1, bottom + 1))
    return out


def _neutral_light(color, white: bool = False) -> bool:
    if not color:
        return False
    r, g, b = color
    return min(r, g, b) >= 150 and max(r, g, b) - min(r, g, b) <= 16 and (white or max(r, g, b) <= 245)


MAX_LINES = 600          # more long dark lines than this: a busy picture, use the change path only


def _hlines(mask: bytes, w: int, h: int) -> list[tuple[int, int, int]] | None:
    """Dark horizontal lines at least an alert wide below the menu bar: (y, x0, x1), by y.
    A line may run on past the box, merged with another window's edge on the same row."""
    out = []
    for y in range(MENU_BAR, h - 2):
        for a, b in _runs(mask, y * w, (y + 1) * w, BOX_W[0] - 8):
            out.append((y, a - y * w, b - y * w))
        if len(out) > MAX_LINES:
            return None
    return out


def _candidate(frame: Frame, mask: bytes, box: tuple[int, int, int, int], via: str) -> dict:
    return {"box": box, "interior": _interior(frame, mask, box), "buttons": _buttons(mask, frame[0], box),
            "via": via}


def find_boxes(frame: Frame, mask: bytes | None = None) -> list[dict]:
    """Framed boxes of alert size below the menu bar: a dark 1-pixel frame on all four
    sides (a drop shadow may follow it), with interior statistics and push buttons.
    The top or the bottom line may continue past the box where another window's edge
    lies on the same row (seen with the Shut Down dialog over a Finder window); the
    box then takes its width from the other one."""
    w, h, rgb = frame
    if w < BOX_W[0] or h < MENU_BAR + BOX_H[0]:
        return []
    mask = mask if mask is not None else dark_mask(frame)
    lines = _hlines(mask, w, h)
    if not lines:
        return []
    found = []
    for i, (top, tx0, tx1) in enumerate(lines):
        for bottom, bx0, bx1 in lines[i + 1:]:
            height = bottom - top + 1
            if height < BOX_H[0]:
                continue
            if height > BOX_H[1]:
                break
            if abs(bx0 - tx0) <= 6 and abs(bx1 - tx1) <= 6:
                lo, hi = max(tx0, bx0), min(tx1, bx1)
            elif bx0 <= tx0 + 6 and bx1 >= tx1 - 6:
                lo, hi = tx0, tx1                 # the bottom line runs on: the top one is the box's
            elif tx0 <= bx0 + 6 and tx1 >= bx1 - 6:
                lo, hi = bx0, bx1                 # the top line runs on
            else:
                continue
            if not (BOX_W[0] <= hi - lo + 1 <= BOX_W[1] + 8):
                continue
            left = next((x for x in (lo, lo + 1, lo + 2, lo + 3, lo - 1)
                         if 0 <= x < w and _col_dark(mask, w, x, top + 2, bottom - 1) >= EDGE_MIN), None)
            right = next((x for x in (hi, hi - 1, hi - 2, hi - 3, hi + 1)
                          if 0 <= x < w and _col_dark(mask, w, x, top + 2, bottom - 1) >= EDGE_MIN), None)
            if left is None or right is None or right - left + 1 < BOX_W[0]:
                continue
            found.append(_candidate(frame, mask, (left, top, right + 1, bottom + 1), "frame"))
            break               # the first bottom edge (a shadow line below it is not another box)
    return found


def changed_boxes(before: Frame, after: Frame, mask: bytes | None = None, cw: int = 8, step: int = 2) -> list[dict]:
    """Regions below the menu bar that changed between two frames of one size, as boxes of
    alert size (bounding boxes of 8x2-pixel cells), for a dialog whose frame does not stand
    out (a dark desktop picture)."""
    (w, h, p), (w2, h2, q) = before, after
    if (w, h) != (w2, h2) or w < BOX_W[0]:
        return []
    gw = (w + cw - 1) // cw
    cells = set()
    for y in range(MENU_BAR, h, step):
        o = y * w * 3
        a, b = p[o:o + w * 3], q[o:o + w * 3]
        if a == b:
            continue
        gy = y // step
        for gx in range(gw):
            s = gx * cw * 3
            if a[s:s + cw * 3] != b[s:s + cw * 3]:
                cells.add((gx, gy))
    if len(cells) > gw * (h // step) // 3:     # a third of the screen changed: a redraw, not an alert
        return []
    mask = mask if mask is not None else dark_mask(after)
    out = []
    while cells:
        stack = [cells.pop()]
        xs, ys = [], []
        while stack:
            gx, gy = stack.pop()
            xs.append(gx)
            ys.append(gy)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    c = (gx + dx, gy + dy)
                    if c in cells:
                        cells.discard(c)
                        stack.append(c)
        box = (min(xs) * cw, min(ys) * step, min(w, (max(xs) + 1) * cw), min(h, (max(ys) + 1) * step))
        bw, bh = box[2] - box[0], box[3] - box[1]
        if BOX_W[0] <= bw <= BOX_W[1] + 16 and BOX_H[0] <= bh <= BOX_H[1] + 8:
            out.append(_candidate(after, mask, box, "change"))
    return out


def changed_share(a: Frame, b: Frame, box: tuple[int, int, int, int], step: int = 3) -> float:
    """Share of 8-pixel chunks inside box that differ between two frames of one size."""
    (w, h, p), (w2, h2, q) = a, b
    if (w, h) != (w2, h2):
        return 1.0
    x0, y0, x1, y1 = box
    diff = total = 0
    for y in range(y0, y1, step):
        for x in range(x0, x1, 8):
            o, e = (y * w + x) * 3, (y * w + min(x + 8, x1)) * 3
            total += 1
            if p[o:e] != q[o:e]:
                diff += 1
    return diff / total if total else 0.0


def menu_titles_dark(frame: Frame) -> int:
    """Dark pixels among the menu titles (left of the screen's middle, past the Apple menu)."""
    w, h, rgb = frame
    if h < MENU_BAR:
        return 0
    mask = dark_mask((w, MENU_BAR, rgb[:w * MENU_BAR * 3]))
    return sum(mask[y * w + 30:y * w + w * 45 // 100].count(1) for y in range(2, 17))


# On a black-and-white screen (a 68k Mac at 1 bit) Mac OS dims menu titles with a 50 % pattern,
# so about half their dark pixels stay, and Mac OS 8.1 leaves Help black while its power-key
# alert is up: 580 -> 351 dark pixels on VM165 (October 24 2026).  Platinum's gray keeps none.
MONO_DIMMED = 0.75


def monochrome(frame: Frame) -> bool:
    """True when the menu bar is only black and white (Mac OS at 1 bit)."""
    w, h, rgb = frame
    return h >= MENU_BAR and not rgb[:w * MENU_BAR * 3].translate(None, b"\x00\xff")


def menus_dimmed(before: Frame | None, after: Frame) -> bool | None:
    """True when the menu titles turned gray (a modal dialog is up); None when unknown."""
    if not before or before[:2] != after[:2]:
        return None
    b, a = menu_titles_dark(before), menu_titles_dark(after)
    if b < 60:
        return None
    return a <= b * (MONO_DIMMED if monochrome(after) else 0.6)


def find_power_dialog(before: Frame | None, after: Frame | None, mask: bytes | None = None) -> dict:
    """Mac OS's power-key dialog in ``after`` that was not in ``before``.

    found: a box of alert size whose inside is mostly one light gray, with some text
    (dark pixels) and either a row of three or more push buttons (Restart, [Sleep,]
    Cancel, Shut Down) or a menu bar that dimmed with it.  The box is found by its
    dark frame or, given ``before``, as a region that changed; with ``before`` it must
    also be new (its area changed)."""
    out = {"found": False, "box": None, "buttons": 0, "menus_dimmed": None, "reason": "no-frame"}
    if not after:
        return out
    mask = mask if mask is not None else dark_mask(after)
    same = before is not None and before[:2] == after[:2]
    dim = menus_dimmed(before, after) if same else None
    out["menus_dimmed"] = dim
    boxes = find_boxes(after, mask) + (changed_boxes(before, after, mask) if same else [])
    out["reason"] = "no-box" if not boxes else "not-a-dialog"
    # A modal dialog dims the menu titles whatever it looks like (another Appearance theme):
    # something is up, so a retry key could close it.
    out["new_box"] = bool(dim)
    for cand in sorted(boxes, key=lambda c: -c["buttons"]):
        inner = cand["interior"]
        fresh = not same or changed_share(before, after, cand["box"]) >= 0.3
        if fresh and inner["share"] >= 0.45 and _neutral_light(inner["color"]):
            out["new_box"] = True       # something alert-like appeared (not a white Finder window)
        # White inside (Platinum's Black & White variation) only with a row of buttons and dimmed
        # menus: a Finder window is white too.
        white = cand["buttons"] >= 3 and bool(dim)
        if not (inner["share"] >= 0.45 and _neutral_light(inner["color"], white) and 0.004 <= inner["dark"] <= 0.35):
            continue
        if cand["buttons"] < 3 and not dim:
            continue
        if not fresh:
            out["reason"] = "box-was-there"
            continue
        out.update(found=True, box=cand["box"], buttons=cand["buttons"], reason="dialog", via=cand["via"],
                   color=inner["color"])
        return out
    return out


def _far(a: bytes, b: bytes) -> bool:
    return abs(a[0] - b[0]) + abs(a[1] - b[1]) + abs(a[2] - b[2]) > 60


def menu_state(before: Frame | None, after: Frame | None) -> dict | None:
    """Whether the menu titles changed state between two frames, in any Appearance theme:
    the same titles (ink in the same columns, against each row's own background) drawn
    in other colours, as when Mac OS disables the menus under a modal dialog or enables
    them again.  Another application's menus are other titles (the columns differ).
    None when there is nothing to compare (other sizes, no titles).

    A row's background is its most common colour among the titles (x 30 to 45 % of the
    width) or, when that finds no change, along the whole row: Hi-Tech draws the titles
    as adjoining buttons, whose fill is the most common colour among them
    (hostd/reports/finderdet-2026-10-13.md)."""
    if not before or not after or before[:2] != after[:2]:
        return None
    w, h, _ = before
    if h < MENU_BAR or w < 200:
        return None
    out = _menu_state(before, after, False)
    if out is not None and not out["changed"]:
        whole = _menu_state(before, after, True)
        if whole is not None and whole["changed"]:
            return dict(whole, background="row")
    return out


def _menu_state(before: Frame, after: Frame, whole_row: bool) -> dict | None:
    w, h, p = before
    q = after[2]
    x0, x1 = 30, w * 45 // 100
    ink_b = recoloured = 0
    cols_b: set[int] = set()
    cols_a: set[int] = set()
    for y in range(2, 17):
        rb = p[(y * w + x0) * 3:(y * w + x1) * 3]
        ra = q[(y * w + x0) * 3:(y * w + x1) * 3]
        pix_b = [rb[i:i + 3] for i in range(0, len(rb), 3)]
        pix_a = [ra[i:i + 3] for i in range(0, len(ra), 3)]
        if whole_row:
            row_b = p[y * w * 3:(y + 1) * w * 3]
            row_a = q[y * w * 3:(y + 1) * w * 3]
            bg_b = Counter(row_b[i:i + 3] for i in range(0, len(row_b), 6)).most_common(1)[0][0]
            bg_a = Counter(row_a[i:i + 3] for i in range(0, len(row_a), 6)).most_common(1)[0][0]
        else:
            bg_b = Counter(pix_b).most_common(1)[0][0]
            bg_a = Counter(pix_a).most_common(1)[0][0]
        for i, (pb, pa) in enumerate(zip(pix_b, pix_a)):
            if _far(pb, bg_b):
                ink_b += 1
                cols_b.add(i // 4)
                if _far(pb, pa):
                    recoloured += 1
            if _far(pa, bg_a):
                cols_a.add(i // 4)
    if ink_b < 40:
        return None
    iou = len(cols_b & cols_a) / max(1, len(cols_b | cols_a))
    share = recoloured / ink_b
    return {"changed": iou >= 0.7 and share >= 0.4, "same_titles": round(iou, 2), "recoloured": round(share, 2)}


def modal_change(before: Frame | None, after: Frame | None) -> dict:
    """A modal dialog appeared, by structure alone (any Appearance theme): the menu titles
    changed state and an alert-sized region below the menu bar changed (not a redraw of
    the screen, not a size change).  The caller adds that it stopped changing."""
    out = {"modal": False, "box": None, "menus": None}
    if not before or not after or before[:2] != after[:2]:
        return out
    ms = menu_state(before, after)
    out["menus"] = ms
    if not ms or not ms["changed"]:
        return out
    boxes = [c["box"] for c in changed_boxes(before, after)]
    if boxes:
        out.update(modal=True, box=max(boxes, key=lambda b: (b[2] - b[0]) * (b[3] - b[1])))
    return out


def alert_on_screen(frame: Frame | None) -> bool:
    """Some alert or dialog box is on screen (whatever its buttons): a power key would then
    wait behind it in Mac OS's event queue rather than be dropped."""
    if not frame:
        return False
    for cand in find_boxes(frame):
        inner = cand["interior"]
        if inner["share"] >= 0.45 and _neutral_light(inner["color"]) and 0.004 <= inner["dark"] <= 0.35:
            return True
    return False


def menu_bar_light(frame: Frame | None) -> bool | None:
    """The top of the screen is mostly light, like Mac OS's Platinum menu bar.  False when
    something covers it: a screen saver (After Dark's Flying Toasters fly over black), a
    full-screen game, or a dark Appearance theme's bar.  None without a frame."""
    if not frame:
        return None
    w, h, rgb = frame
    if h < MENU_BAR:
        return None
    light = total = 0
    for y in range(2, MENU_BAR - 2, 3):
        row = rgb[y * w * 3:(y + 1) * w * 3]
        for x in range(0, w * 3, 24):
            total += 1
            if row[x] > 170 and row[x + 1] > 170 and row[x + 2] > 170:
                light += 1
    return light * 2 >= total


def screen_changed(prev: Frame | None, cur: Frame | None) -> float:
    """Share of the screen below the menu bar that changed (1.0 for a size change or no frame).
    The menu bar is left out: its clock changes every minute while Mac OS idles."""
    if not prev or not cur:
        return 1.0
    (w, h, p), (w2, h2, q) = prev, cur
    if (w, h) != (w2, h2):
        return 1.0
    rows = range(MENU_BAR, h, 4)
    diff = total = 0
    for y in rows:
        a, b = p[y * w * 3:(y + 1) * w * 3], q[y * w * 3:(y + 1) * w * 3]
        if a == b:
            total += (w + 15) // 16
            continue
        for x in range(0, w * 3, 48):
            total += 1
            if a[x:x + 48] != b[x:x + 48]:
                diff += 1
    return diff / total if total else 0.0


# --- How busy the emulated CPU is --------------------------------------------------
def _tcg_threads(pid: int) -> list[Path]:
    out = []
    try:
        for t in Path(f"/proc/{int(pid)}/task").iterdir():
            try:
                if "TCG" in (t / "comm").read_text():
                    out.append(t)
            except OSError:
                continue
    except OSError:
        return []
    return out


def _thread_ns(t: Path) -> int | None:
    """Time a thread ran or waited to run (schedstat)."""
    try:
        run, wait = (int(v) for v in (t / "schedstat").read_text().split()[:2])
        return run + wait
    except (OSError, ValueError):
        return None


def _process_ns(pid: int) -> int | None:
    """User plus system time of all of a process's threads (stat)."""
    try:
        fields = Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()
        return (int(fields[11]) + int(fields[12])) * (1_000_000_000 // os.sysconf("SC_CLK_TCK"))
    except (OSError, ValueError, IndexError):
        return None


class GuestLoad:
    """How busy Mac OS keeps the emulated CPU: the share of wall time QEMU's TCG
    thread ran or waited to run (so a loaded host does not look idle).  An idle
    Mac OS naps between events (about 20-30 % on the lab builder), against nearly
    100 % while it loads extensions, opens its startup items or launches an
    application.  Without a TCG thread (a test fake, or before QEMU created it) the
    whole process's CPU time is measured; without /proc nothing is (``busy`` and
    ``peak`` return None and callers rely on the screen alone)."""

    def __init__(self, pid: int | None):
        self.pid = pid
        self.paths: list[Path] | None = None
        self.source: str | None = None
        self.samples: list[tuple[float, int]] = []

    def _read(self) -> tuple[str, int] | None:
        if not self.pid:
            return None
        if not self.paths:
            self.paths = _tcg_threads(self.pid)
        if self.paths:
            total = 0
            for p in self.paths:
                v = _thread_ns(p)
                if v is None:
                    self.paths = None
                    return None
                total += v
            return "tcg", total
        v = _process_ns(self.pid)
        return None if v is None else ("process", v)

    def sample(self, now: float) -> None:
        r = self._read()
        if r is None:
            return
        source, v = r
        if source != self.source:
            self.samples.clear()
            self.source = source
        self.samples.append((now, v))
        while len(self.samples) > 2 and self.samples[1][0] < now - 30.0:
            self.samples.pop(0)

    def busy(self, now: float, window: float = 1.0) -> float | None:
        """Busy share over about the last ``window`` seconds (None when not measurable)."""
        if len(self.samples) < 2:
            return None
        t1, v1 = self.samples[-1]
        older = [s for s in self.samples[:-1] if t1 - s[0] >= window * 0.8]
        t0, v0 = older[-1] if older else self.samples[0]
        if t1 - t0 <= 0.05 or now - t1 > max(2.0, window * 2):
            return None
        return max(0.0, (v1 - v0) / 1e9 / (t1 - t0))

    def peak(self, now: float, span: float, step: float = 0.9) -> float | None:
        """The highest busy share over steps of about ``step`` seconds within the last
        ``span`` seconds; None when the samples do not cover the span."""
        pts = [s for s in self.samples if s[0] >= now - span - 0.6]
        if len(pts) < 2 or pts[-1][0] - pts[0][0] < span * 0.66 or now - pts[-1][0] > 1.5:
            return None
        worst, i = 0.0, 0
        while i < len(pts) - 1:
            j = i + 1
            while j < len(pts) - 1 and pts[j][0] - pts[i][0] < step:
                j += 1
            dt = pts[j][0] - pts[i][0]
            if dt > 0:
                worst = max(worst, (pts[j][1] - pts[i][1]) / 1e9 / dt)
            i = j
        return worst
