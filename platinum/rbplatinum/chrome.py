"""Platinum chrome as metric tables and drawing rules (Mac OS 9.2.1, Appearance 1.1).

Every rule here was measured from 1x frames of the user's own Mac OS 9.2.1 guest
(platinum/lab), the way presenter/ui's tables were measured from Mac OS 8.1.
Colors are palette letters (canvas.py); the variation ramp and highlight color
come from the guest's theme file when a bundle is built.  The functions return
canvases; bundle.py turns them into the sprite PNGs the web client slices.
"""
from __future__ import annotations

from .canvas import Canvas, Palette

# ---- Document windows -------------------------------------------------------
TITLE_H = 20          # title bar rows inside the outline (outline row excluded)
FRAME_L, FRAME_R = 7, 8   # left slice (outline..content highlight), right slice incl. drop shadow
FRAME_T, FRAME_B = 23, 8  # top slice (outline..content highlight), bottom slice incl. drop shadow


def window(w: int, h: int, pal: Palette, active: bool = True, title_bar: bool = True) -> Canvas:
    """A document window frame w x h (outline box, plus the 1-px drop shadow outside).

    The content area inside the content line is left transparent; its inner
    W/a bevel is the content's own (content_bevel).  Pinstripes, close/zoom/
    collapse boxes and the title are separate pieces.
    """
    c = Canvas(w + 1, h + 1, pal)
    x1, y1 = w - 1, h - 1
    top = TITLE_H if title_bar else 4
    cx0, cy0, cx1, cy1 = 5, top + 1, x1 - 5, y1 - 5
    cw, ch = cx1 - cx0 + 1, cy1 - cy0 + 1
    if active:
        c.fill(1, 1, w - 2, h - 2, "c")
        c.frame(0, 0, w, h, "K")
        c.vline(x1 + 1, 2, h - 1, "K")   # drop shadow, offset 2 from the corners
        c.hline(2, y1 + 1, w - 1, "K")
        c.bevel(1, 1, w - 2, h - 2, "W", "9")
        c.bevel(cx0 - 1, cy0 - 1, cw + 2, ch + 2, "9", "W")
        c.frame(cx0, cy0, cw, ch, "K")
    else:
        c.fill(1, 1, w - 2, h - 2, "d")
        c.frame(0, 0, w, h, "5")
        c.vline(x1 + 1, 2, h - 1, "5")
        c.hline(2, y1 + 1, w - 1, "5")
        c.frame(cx0, cy0, cw, ch, "5")
    c.fill(cx0 + 1, cy0 + 1, cw - 2, ch - 2, " ")
    return c


def content_bevel(w: int, h: int, pal: Palette, face: str = " ") -> Canvas:
    """The 1-px W/a bevel just inside a window's content line (dialog content)."""
    c = Canvas(w, h, pal, face)
    c.bevel(0, 0, w, h, "W", "a")
    return c


# Title bar widgets: 13 x 13 including their etched well (8 above/left, W below/right).
_RAMP = "99aabbccddeeW"   # interior gradient, indexed by x + y


def title_box(kind: str, pal: Palette, pressed: bool = False) -> Canvas:
    """'close', 'zoom' or 'collapse' box of an active window."""
    c = Canvas(13, 13, pal, "c")
    c.hline(0, 0, 12, "8")
    c.vline(0, 0, 12, "8")
    c.hline(1, 12, 12, "W")
    c.vline(12, 1, 12, "W")
    c.frame(1, 1, 11, 11, "2")
    c.bevel(2, 2, 9, 9, "c", "8")
    c.px(2, 2, "W")
    for j in range(7):
        for i in range(7):
            c.px(3 + i, 3 + j, _RAMP[i + j])
    if kind == "zoom":
        c.vline(7, 2, 6, "2")
        c.hline(2, 7, 6, "2")
    elif kind == "collapse":
        c.hline(1, 5, 11, "2")
        c.hline(1, 7, 11, "2")
    return c


STRIPES_Y = 4          # first pinstripe row below the outline
STRIPES_N = 6          # W/7 pairs


def pinstripes(w: int, pal: Palette) -> Canvas:
    """The title-bar pinstripes as drawn between x and x+w: each white line starts
    one pixel left of its shadow and ends one pixel earlier."""
    c = Canvas(w, STRIPES_N * 2, pal)
    for i in range(STRIPES_N):
        c.hline(0, 2 * i, w - 1, "W")
        c.hline(1, 2 * i + 1, w - 1, "7")
    return c


# ---- Stretchable edge-row tables ---------------------------------------------
# A table is a list of (left, fill, right) rows; the row at index `repeat`
# stretches vertically and each row's fill stretches horizontally.  '.' marks
# pixels outside the control's shape (transparent).

def stretch(rows, repeat: int, w: int, h: int, pal: Palette) -> Canvas:
    c = Canvas(w, h, pal)
    n_top = repeat
    n_bot = len(rows) - repeat - 1
    for y in range(h):
        if y < n_top:
            r = rows[y]
        elif y >= h - n_bot:
            r = rows[len(rows) - (h - y)]
        else:
            r = rows[repeat]
        left, fill, right = r
        line = left + fill * max(0, w - len(left) - len(right)) + right
        for x, ch in enumerate(line[:w]):
            c.px(x, y, ch)
    return c


def slices(rows, repeat: int) -> tuple[int, int, int, int]:
    """CSS border-image slice (top, right, bottom, left) for a table."""
    return (repeat, max(len(r[2]) for r in rows), len(rows) - repeat - 1, max(len(r[0]) for r in rows))


# ---- Push buttons (20 px tall in dialogs) --------------------------------------
BUTTON = [
    ("..2K", "K", "K2.."),
    (".Kbd", "d", "dbK."),
    ("2bWW", "W", "Wdb2"),
    ("KdWW", "d", "da7K"),
    ("KdWd", "d", "da7K"),   # repeat
    ("KdWd", "d", "aa7K"),
    ("2bda", "a", "a772"),
    (".Kb7", "7", "77K."),
    ("..2K", "K", "K2.."),
]
BUTTON_PRESSED = [
    ("..2K", "K", "K2.."),
    (".K44", "4", "44K."),
    ("2445", "5", "5672"),
    ("K455", "6", "678K"),
    ("K456", "6", "678K"),   # repeat
    ("K456", "6", "778K"),
    ("2467", "7", "7882"),
    (".K78", "8", "88K."),
    ("..2K", "K", "K2.."),
]
BUTTON_DISABLED = [
    ("..88", "8", "88.."),
    (".8dd", "d", "dd8."),
    ("8ddd", "d", "ddd8"),   # repeat
    (".8dd", "d", "dd8."),
    ("..88", "8", "88.."),
]
# The default button: the 3-px ring and the button in one table (CSS border-image-outset 3px).
BUTTON_DEFAULT = [
    ("...2KKKK", "K", "KKKK2..."),
    ("..Kddddd", "d", "ddddcK.."),
    (".Kddaaaa", "a", "aaaaabK."),
    ("2dda72KK", "K", "KK27aa82"),
    ("Kda7Kbdd", "d", "ddbK7a7K"),
    ("Kda2bWWW", "W", "WWdb2a7K"),
    ("KdaKdWWd", "d", "dda7Ka7K"),
    ("KdaKdWdd", "d", "dda7Ka7K"),   # repeat
    ("KdaKdWdd", "d", "daa7Ka7K"),
    ("Kda2bdaa", "a", "aa772a7K"),
    ("Kda7Kb77", "7", "777K7a7K"),
    ("2caa72KK", "K", "KK27a872"),
    (".Kbaaaaa", "a", "aaaa87K."),
    ("..K87777", "7", "77777K.."),
    ("...2KKKK", "K", "KKKK2..."),
]
BUTTON_DEFAULT_PRESSED = [
    ("...2KKKK", "K", "KKKK2..."),
    ("..Kddddd", "d", "ddddcK.."),
    (".Kddaaaa", "a", "aaaaabK."),
    ("2dda72KK", "K", "KK27aa82"),
    ("Kda7K444", "4", "444K7a7K"),
    ("Kda24455", "5", "55672a7K"),
    ("KdaK4556", "6", "6678Ka7K"),
    ("KdaK4566", "6", "6678Ka7K"),   # repeat
    ("KdaK4566", "6", "6778Ka7K"),
    ("Kda24677", "7", "77882a7K"),
    ("Kda7K788", "8", "888K7a7K"),
    ("2caa72KK", "K", "KK27a872"),
    (".Kbaaaaa", "a", "aaaa87K."),
    ("..K87777", "7", "77777K.."),
    ("...2KKKK", "K", "KKKK2..."),
]


def button(w: int, h: int, pal: Palette, state: str = "normal") -> Canvas:
    """state: normal | pressed | disabled | default | default-pressed (w, h exclude the ring)."""
    if state.startswith("default"):
        rows = BUTTON_DEFAULT_PRESSED if state == "default-pressed" else BUTTON_DEFAULT
        return stretch(rows, 7, w + 6, h + 6, pal)
    rows = {"normal": BUTTON, "pressed": BUTTON_PRESSED, "disabled": BUTTON_DISABLED}[state]
    return stretch(rows, 4 if state != "disabled" else 2, w, h, pal)


def sprite(rows: str, pal: Palette) -> Canvas:
    lines = rows.strip("\n").split("\n")
    c = Canvas(max(len(r) for r in lines), len(lines), pal)
    c.table(0, 0, lines)
    return c


# ---- Checkboxes (12 x 12 box; the check overhangs 2 px to the right) -----------
CHECKBOX = {
    "off": """
KKKKKKKKKKKK..
KWWWWWWWWWdK..
KWdddddddd8K..
KWdddddddd8K..
KWdddddddd8K..
KWdddddddd8K..
KWdddddddd8K..
KWdddddddd8K..
KWdddddddd8K..
KWdddddddd8K..
Kd888888888K..
KKKKKKKKKKKK..
""",
    "on": """
KKKKKKKKKKKK..
KWWWWWWWWWdKK.
KWddddddddKK7a
KWdddddddKKKa.
KWddddddKK5K..
KWKKdddKK77K..
KWdKKdKK7a8K..
KWdaKKK7ad8K..
KWddaK7add8K..
KWddd7addd8K..
Kd888888888K..
KKKKKKKKKKKK..
""",
    "off-pressed": """
KKKKKKKKKKKK..
K5555555557K..
K5777777779K..
K5777777779K..
K5777777779K..
K5777777779K..
K5777777779K..
K5777777779K..
K5777777779K..
K5777777779K..
K7999999999K..
KKKKKKKKKKKK..
""",
    "on-pressed": """
KKKKKKKKKKKK..
K5555555557KK.
K577777777KK57
K57777777KKK7.
K5777777KK4K..
K5KK777KK45K..
K57KK7KK459K..
K575KKK4579K..
K5775K45779K..
K5777457779K..
K7999999999K..
KKKKKKKKKKKK..
""",
    "on-disabled": """
888888888888..
8dddddddddd88.
8ddddddddd88..
8dddddddd888..
8ddddddd88d8..
8d88ddd88dd8..
8dd88d88ddd8..
8ddd888dddd8..
8dddd8ddddd8..
8dddddddddd8..
8dddddddddd8..
888888888888..
""",
    "off-disabled": """
888888888888..
8dddddddddd8..
8dddddddddd8..
8dddddddddd8..
8dddddddddd8..
8dddddddddd8..
8dddddddddd8..
8dddddddddd8..
8dddddddddd8..
8dddddddddd8..
8dddddddddd8..
888888888888..
""",
}

# ---- Radio buttons (12 x 12) ---------------------------------------------------
RADIO = {
    "off": """
....4KK4....
..K5dddb4K..
.KbdeWWWd8K.
.5deWWeedb4.
4deWWeeddb84
KdWWeeddbb8K
KdWeeddbba8K
4bWeddbbaa84
.5dddbbaa84.
.K8bbbaa88K.
..K588884K..
....4KK4....
""",
    "on": """
....2KK2....
..2K45554K..
.245777889K.
.K57KKKK994.
247KKKKKK9b4
K57KKKKKKbbK
K57KKKKKKbdK
258KKKKKKdW4
.289KKKKdW4.
.2999bbdWWK.
..24bbdW4K..
....4KK4....
""",
    "off-pressed": """
....2KK2....
..2K44444K..
.244556667K.
.K455667774.
245566777894
K4566777889K
K4667778899K
2467778899b4
.26778899b5.
.4778899bbK.
..24999b5K..
....4KK4....
""",
    "on-pressed": """
....2KK2....
..2K44444K..
.244556667K.
.K45KKKK774.
245KKKKKK894
K45KKKKKK89K
K46KKKKKK99K
246KKKKKK9b4
.267KKKK9b5.
.4778899bbK.
..24999b5K..
....4KK4....
""",
}


def _disable_radio(rows: str, on: bool) -> str:
    # Not captured yet: outline pixels in 8, face in d, dot in 8 (as the disabled checkbox).
    out = []
    for r in rows.strip("\n").split("\n"):
        out.append("".join("." if ch == "." else "8" for ch in r))
    base = RADIO["off"].strip("\n").split("\n")
    res = []
    for j, (r, b) in enumerate(zip(out, base)):
        line = ""
        for i, (ch, bc) in enumerate(zip(r, b)):
            if ch == ".":
                line += "."
            elif bc in "K42" and (i in (0, 11) or j in (0, 11) or (i, j) in {(1, 2), (2, 1), (9, 1), (10, 2), (1, 9), (2, 10), (9, 10), (10, 9), (1, 3), (3, 1), (8, 1), (10, 3), (1, 8), (3, 10), (8, 10), (10, 8)}):
                line += "8"
            else:
                line += "8" if (on and 3 <= i <= 8 and 3 <= j <= 8 and (i, j) not in {(3, 3), (8, 3), (3, 8), (8, 8)}) else "d"
        res.append(line)
    return "\n".join(res)


RADIO["off-disabled"] = _disable_radio(RADIO["off"], False)
RADIO["on-disabled"] = _disable_radio(RADIO["off"], True)

# ---- Pop-up menu buttons (20 px tall): left cap 4, stretching fill, arrow cap 22 ----
POPUP_LEFT = ["..2K", ".KWW", "2Wdd"] + ["KWdd"] * 14 + ["2Wdd", ".Kaa", "..2K"]
POPUP_FILL = ["K", "W"] + ["d"] * 16 + ["a", "K"]
POPUP_RIGHT = """
KKKKKKKKKKKKKKKKKKK2..
dddddddddddddddddddbK.
adWWWWWWWWWWWWWWWWWda2
adWdddddddddddddddda7K
adWdddddddddddddddda7K
adWdddddddKdddddddda7K
adWddddddKKKddddddda7K
adWdddddKKKKKdddddda7K
adWddddKKKKKKKddddda7K
adWdddddddddddddddda7K
adWdddddddddddddddda7K
adWddddKKKKKKKddddda7K
adWdddddKKKKKdddddda7K
adWddddddKKKddddddda7K
adWdddddddKdddddddda7K
adWdddddddddddddddda7K
adWdddddddddddddddda7K
addaaaaaaaaaaaaaaaa772
ab777777777777777777K.
KKKKKKKKKKKKKKKKKKK2..
"""
POPUP_DIS_LEFT = ["..88", ".8dd"] + ["8ddd"] * 16 + [".8dd", "..88"]
POPUP_DIS_FILL = ["8"] + ["d"] * 18 + ["8"]
POPUP_DIS_RIGHT = """
88888888888888888888..
addddddddddddddddddd8.
adddddddddddddddddddd8
adddddddddddddddddddd8
adddddddddddddddddddd8
addddddddd8dddddddddd8
adddddddd888ddddddddd8
addddddd88888dddddddd8
adddddd8888888ddddddd8
adddddddddddddddddddd8
adddddddddddddddddddd8
adddddd8888888ddddddd8
addddddd88888dddddddd8
adddddddd888ddddddddd8
addddddddd8dddddddddd8
adddddddddddddddddddd8
adddddddddddddddddddd8
adddddddddddddddddddd8
addddddddddddddddddd8.
88888888888888888888..
"""


def popup(w: int, pal: Palette, state: str = "normal") -> Canvas:
    """A 20-px-tall pop-up button w wide: normal | disabled."""
    c = Canvas(w, 20, pal)
    if state == "disabled":
        left, fill, right = POPUP_DIS_LEFT, POPUP_DIS_FILL, POPUP_DIS_RIGHT.strip("\n").split("\n")
    else:
        left, fill, right = POPUP_LEFT, POPUP_FILL, POPUP_RIGHT.strip("\n").split("\n")
    for y in range(20):
        line = left[y] + fill[y] * (w - len(left[y]) - len(right[y])) + right[y]
        c.table(0, y, [line])
    return c


# ---- Text fields (edit text) and the keyboard focus ring -------------------------
FIELD = [
    ("88", "8", "8."),
    ("8K", "K", "KW"),
    ("8K", " ", "KW"),   # repeat (content is the field's own background)
    ("8K", "K", "KW"),
    (".W", "W", "WW"),
]
# Focused: a 2-px ring in variation color D replaces the etch (1 px further out).
FIELD_FOCUS = [
    (".DD", "D", "DD."),
    ("DDD", "D", "DDD"),
    ("DDK", "K", "KDD"),
    ("DDK", " ", "KDD"),  # repeat
    ("DDK", "K", "KDD"),
    ("DDD", "D", "DDD"),
    (".DD", "D", "DD."),
]


def field(w: int, h: int, pal: Palette, focused: bool = False) -> Canvas:
    """Edit-text frame around a w x h box measured on the K line."""
    if focused:
        return stretch(FIELD_FOCUS, 3, w + 4, h + 4, pal)
    return stretch(FIELD, 2, w + 2, h + 2, pal)


# ---- Separator and group boxes (etched lines: 8, then W one pixel down/right) ----
GROUP = [
    ("88", "8", "8."),
    ("8W", "W", "8W"),
    ("8W", " ", "8W"),   # repeat
    ("88", "8", "8W"),
    (".W", "W", "WW"),
]


def group_box(w: int, h: int, pal: Palette) -> Canvas:
    return stretch(GROUP, 2, w, h, pal)


def separator(w: int, pal: Palette) -> Canvas:
    c = Canvas(w, 2, pal)
    c.hline(0, 0, w - 1, "8")
    c.hline(1, 1, w - 1, "W")
    return c


# ---- Progress bars (14 px tall including the etch) ----------------------------------
PROG_FILL_COL2 = "DCBAAABCDE"     # second column of the fill, rows 2..11
PROG_FILL = "EDCBABCDEG"          # body of the fill
PROG_END = ["EEEK5", "DDGK5", "CEGK5", "BEGK5", "BEGK5", "BEGK5", "CEGK5", "DEGK5", "EEGK5", "GGGK5"]
PROG_TRACK = "8bbbbbbbbd"         # empty part, rows 2..11
PROG_TRACK_END = "bddddddddd"     # its last column before the frame


def progress(w: int, pal: Palette, frac: float | None, fill_px: int | None = None) -> Canvas:
    """Determinate bar w wide (outer, including the etch); frac None draws an empty track."""
    c = Canvas(w, 14, pal)
    c.hline(0, 0, w - 1, "a")
    c.vline(0, 0, 13, "a")
    c.px(0, 13, "d")
    c.px(w - 1, 0, "d")
    c.frame(1, 1, w - 2, 12, "K")
    c.hline(1, 13, w - 1, "W")
    c.vline(w - 1, 1, 13, "W")
    inner = w - 4                     # interior columns 2 .. w-3
    for j in range(10):
        c.hline(2, 2 + j, inner, PROG_TRACK[j])
        c.px(w - 3, 2 + j, PROG_TRACK_END[j])
        c.px(2, 2 + j, "8")
    n = fill_px if fill_px is not None else progress_fill_px(w, frac)
    if n:
        for j in range(10):
            c.px(2, 2 + j, "D")
            c.px(3, 2 + j, PROG_FILL_COL2[j])
            c.hline(4, 2 + j, max(0, n - 2), PROG_FILL[j])
            if n < inner:
                c.table(2 + n - 3, 2 + j, [PROG_END[j] + "8"])
    return c


def progress_fill_px(w: int, frac: float | None) -> int:
    """Interior columns covered by the fill, its K boundary included (0 = empty)."""
    if frac is None or frac <= 0:
        return 0
    inner = w - 4
    return max(5, min(inner, round(inner * min(1.0, frac))))


# ---- Disclosure triangles (12 x 14 cell) ------------------------------------------
# Both states share one cell, as in a Finder list view (listview_open): the open
# triangle is wider and lower than the closed one.
DISCLOSURE = {
    "right": """
............
............
....K.......
....KK......
....KBK.....
....KBCK....
....KBCCK...
....KBCCDK..
....KBCDK8b.
....KBDK8b..
....KCK8b...
....KK8b....
....K8b.....
.....b......
""",
    "down": """
............
............
............
............
............
............
KKKKKKKKKKK.
.KBBBBBBCK8b
..KCCCCDK8b.
...KCCDK8b..
....KDK8b...
.....K8b....
......b.....
............
""",
}


# ---- Alerts and movable modal dialogs --------------------------------------------------
# Alert frame: black outline, a light red line inside it, a white bevel, the dialog face,
# and a red line and black outline plus drop shadow at the right and bottom.
ALERT_COLORS = {"q": (0xFF, 0x99, 0x99), "Q": (0xFF, 0x66, 0x66)}
ALERT = [
    ("KKKK", "K", "KKKK."),
    ("Kqqq", "q", "qqqK."),
    ("KqWW", "W", "WqQKK"),
    ("KqWd", "d", "d9QKK"),   # repeat
    ("Kqq9", "9", "99QKK"),
    ("KqQQ", "Q", "QQQKK"),
    ("KKKK", "K", "KKKKK"),
    ("..KK", "K", "KKKKK"),
]


def alert(w: int, h: int, pal: Palette) -> Canvas:
    """Alert window frame, w x h outline box plus the 1-px shadow."""
    return stretch(ALERT, 3, w + 1, h + 1, pal)


# Movable modal dialog: a document-window title bar without boxes over a raised face.
MOVABLE = [
    ("KKKKKKKK", "K", "KKKKKKK."),
    ("KWWWWWWW", "W", "WWWWWcK."),
] + [("KWcccccc", "c", "ccccc9KK")] * 18 + [
    ("Kc999999", "9", "999999KK"),
    ("KKKKKKKK", "K", "KKKKKKKK"),
    ("Kddddddd", "d", "ddddddKK"),
    ("KdWWWWWW", "W", "WWWWd5KK"),
    ("KdWddddd", "d", "dddd95KK"),   # repeat (index 24)
    ("Kdd99999", "9", "999995KK"),
    ("Kd555555", "5", "555555KK"),
    ("KKKKKKKK", "K", "KKKKKKKK"),
    ("..KKKKKK", "K", "KKKKKKKK"),
]


def movable(w: int, h: int, pal: Palette) -> Canvas:
    """Movable modal frame (title stripes are a separate piece), w x h plus shadow."""
    return stretch(MOVABLE, 24, w + 1, h + 1, pal)


# ---- Menu bar and menus ------------------------------------------------------------------
MENUBAR_LEFT = ["KKKKK5adW", "KKK5aWWWd", "KK5dWdddd", "K5dWddddd", "KaWdddddd", "5Wddddddd", "aWddddddd",
                "dWddddddd"] + ["Wdddddddd"] * 10 + ["d99999999", "KKKKKKKKK"]
MENUBAR_FILL = "W" + "d" * 17 + "9K"
MENUBAR_RIGHT = ["WWWWWWda52KKKK", "dddddddcb85KKK", "dddddddddb85KK", "ddddddddddb85K", "dddddddddddb82",
                 "dddddddddddda5", "ddddddddddddb7", "ddddddddddddc8"] + ["ddddddddddddd9"] * 10 + \
                ["99999999999999", "KKKKKKKKKKKKKK"]


def menubar(w: int, pal: Palette) -> Canvas:
    c = Canvas(w, 20, pal)
    for y in range(20):
        line = MENUBAR_LEFT[y] + MENUBAR_FILL[y] * (w - 9 - 14) + MENUBAR_RIGHT[y]
        c.table(0, y, [line])
    return c


# A pull-down menu below the menu bar: black sides and bottom, a white/9 bevel, a #222
# shadow one pixel right and below (starting 1 px down and 2 px in).
MENU = [
    ("KW", "W", "dK."),
    ("KW", "d", "9K2"),   # repeat
    ("Kd", "9", "9K2"),
    ("KK", "K", "KK2"),
    ("..", "2", "222"),
]


def menu(w: int, h: int, pal: Palette) -> Canvas:
    """Menu panel w x h (outline box, its top row is just below the menu bar) plus shadow."""
    return stretch(MENU, 1, w + 1, h + 1, pal)


# ---- Sliders (General Controls) -------------------------------------------------------------
SLIDER_TRACK = [
    (".a", "a", "a.."),
    ("aa", "2", "2a."),
    ("a2", "a", "a2W"),   # repeat
    ("a2", "a", "a2W"),
    ("a2", "a", "a2W"),
    (".W", "2", "2WW"),
    ("..", "W", "WW."),
]
SLIDER_THUMB = """
.KKKKKKKKKKKKK.
KABBBBBBBBBBBCK
KBCCCCCCCCCCCDK
KBCCACACACCCCDK
KBCCBEBEBECCCDK
KBCCBEBEBECCCDK
KBCCBEBEBECCCDK
KBCCBEBEBECCCDK
KBCCBEBEBECCCDK
KBCCCECECECCCDK
.KDCCCCCCCCCDK.
..KDCCCCCCCDK..
...KDCCCCCDK...
....KDCCCDK....
.....KDDDK.....
......KKK......
"""
# Pressed: every ramp color one step darker (E -> G); verified where the pointer
# didn't cover the capture.
SLIDER_THUMB_PRESSED = SLIDER_THUMB.translate(str.maketrans("ABCDE", "BCDEG"))


# The non-directional thumb (Sound's Main Volume, Mac OS 9.2.2): 13 x 16, its top 4 rows
# above the track's.
SLIDER_THUMB_PLAIN = """
.KKKKKKKKKKK.
KABBBBBBBBBCK
KBCCCCCCCCCDK
KBCCCCCCCCCDK
KBCACACACCCDK
KBCBEBEBECCDK
KBCBEBEBECCDK
KBCBEBEBECCDK
KBCBEBEBECCDK
KBCBEBEBECCDK
KBCBEBEBECCDK
KBCCECECECCDK
KBCCCCCCCCCDK
KBCCCCCCCCCDK
KCDDDDDDDDDDK
.KKKKKKKKKKK.
"""
SLIDER_THUMB_PLAIN_PRESSED = SLIDER_THUMB_PLAIN.translate(str.maketrans("ABCDE", "BCDEG"))


def slider_track(w: int, pal: Palette) -> Canvas:
    return stretch(SLIDER_TRACK, 2, w, 7, pal)


# ---- Scroll bars (Appearance control panel list, 1x) -------------------------------
# A vertical bar is 15 px wide: its own left K line and 14 inner columns; the list
# frame supplies the K on the other three sides.  Both arrows sit at the bottom.
# Horizontal parts are the transposes (light comes from the top left either way),
# checked against a Finder list view.
SCROLL_TRACK = """
K7777777777777c
K788888888888bc
K78aaaaaaaaaabc
"""
# The thumb sprite carries the two track shadow rows under it: browsers split the
# track at the thumb's middle, so the track piece after the thumb can't draw them.
SCROLL_THUMB = """
KKKKKKKKKKKKKKK
KeBBBBBBBBBBBBC
KBCCCCCCCCCCCCD
KCDDDDDDDDDDDDD
KKKKKKKKKKKKKKK
K7777777777777c
K788888888888bc
"""
SCROLL_GRIP = """
KBCCeBBBBBBCCCD
KBCCCEEEEEEECCD
KBCCeBBBBBBCCCD
KBCCCEEEEEEECCD
KBCCeBBBBBBCCCD
KBCCCEEEEEEECCD
KBCCeBBBBBBCCCD
KBCCCEEEEEEECCD
"""
SCROLL_UP = """
KKKKKKKKKKKKKKK
KWWWWWWWWWWWWWd
KWddddddddddddb
KWddddddddddddb
KWddddddddddddb
KWddddddddddddb
KWdddddKKdddddb
KWddddKKKKddddb
KWdddKKKKKKdddb
KWddKKKKKKKKddb
KWddddddddddddb
KWddddddddddddb
KWddddddddddddb
KWddddddddddddb
Kdbbbbbbbbbbbbb
"""
SCROLL_DOWN = """
KKKKKKKKKKKKKKK
KWWWWWWWWWWWWWd
KWddddddddddddb
KWddddddddddddb
KWddddddddddddb
KWddddddddddddb
KWddKKKKKKKKddb
KWdddKKKKKKdddb
KWddddKKKKddddb
KWdddddKKdddddb
KWddddddddddddb
KWddddddddddddb
KWddddddddddddb
KWddddddddddddb
Kdbbbbbbbbbbbbb
"""


def transpose(rows: str) -> str:
    lines = rows.strip("\n").split("\n")
    return "\n" + "\n".join("".join(line[i] for line in lines) for i in range(len(lines[0]))) + "\n"


def scroll_sprites(pal: Palette) -> dict[str, Canvas]:
    out = {}
    for axis, f in (("v", lambda r: r), ("h", transpose)):
        out[f"scroll-track-{axis}"] = sprite(f(SCROLL_TRACK), pal)
        out[f"scroll-thumb-{axis}"] = sprite(f(SCROLL_THUMB), pal)
        out[f"scroll-grip-{axis}"] = sprite(f(SCROLL_GRIP), pal)
        out[f"scroll-thumb-mid-{axis}"] = sprite(f("\nKBCCCCCCCCCCCCD\n"), pal)
    out["scroll-up"] = sprite(SCROLL_UP, pal)
    out["scroll-down"] = sprite(SCROLL_DOWN, pal)
    out["scroll-left"] = sprite(transpose(SCROLL_UP), pal)
    out["scroll-right"] = sprite(transpose(SCROLL_DOWN), pal)
    return out
