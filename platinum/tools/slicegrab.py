"""Derive a stretchable edge-row table from a reference control (developer tool).

    slicegrab(ref, x, y, w, h, left, right, top, bottom, mask=[(x,y,w,h)])

Rows are (left, fill, right) strings: `top` rows, one repeating middle row, then
`bottom` rows.  The fill of each row must be uniform outside masked rectangles
(text, cursor); the middle rows must all be equal.  Prints code for chrome.py.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.refcheck import load, letter  # noqa: E402


def slicegrab(ref, x, y, w, h, left, right, top, bottom, mask=(), name="ROWS", bg=None):
    R = load(ref)
    ex = {}
    masked = set()
    for (mx, my, mw, mh) in mask:
        masked |= {(i, j) for i in range(mx, mx + mw) for j in range(my, my + mh)}

    def L(i, j):
        ch = letter(R.get(x + i, y + j), ex)
        return "_" if bg and ch == bg else ch

    rows = []
    for j in range(h):
        lft = "".join(L(i, j) for i in range(left))
        rgt = "".join(L(i, j) for i in range(w - right, w))
        mids = [L(i, j) for i in range(left, w - right) if (x + i, y + j) not in masked]
        fills = sorted(set(mids))
        rows.append((lft, fills[0] if len(fills) == 1 else "?" + "".join(fills), rgt))
    mid = rows[top:h - bottom]
    problems = [r for r in mid if r != mid[0]]
    out = rows[:top] + [mid[0]] + rows[h - bottom:]
    print(f"{name} = [  # {w}x{h} reference; middle row repeats" + (f"; {len(problems)} middle rows differ!" if problems else ""))
    for k, r in enumerate(out):
        tag = "  # repeat" if k == top else ""
        print(f"    {r!r},{tag}")
    print("]")
    for p in problems[:5]:
        print("#  differs:", p)
    if ex:
        print("# extra:", "  ".join(f"{v}={k[0]:02x}{k[1]:02x}{k[2]:02x}" for k, v in ex.items()))
    return out
