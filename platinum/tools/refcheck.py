"""Compare generated chrome against private reference captures (developer tool).

    from tools.refcheck import check
    check(ref_png, x, y, generated_image, mask=[(x,y,w,h), ...], name='close box')

Transparent generated pixels and masked rectangles are not compared.  Prints
the number of differing pixels and, on mismatch, the two letter grids.
References are private guest captures and are never committed.
"""
from __future__ import annotations

import functools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rbplatinum.image import decode_png  # noqa: E402
from rbplatinum.canvas import GRAYS, LAVENDER, PURPLE  # noqa: E402

NAMES = {v: k for k, v in GRAYS.items()}
for k, c in zip("ABCDEFGH", LAVENDER):
    NAMES.setdefault(c, k)
NAMES.setdefault(PURPLE, "h")


@functools.lru_cache(None)
def load(path: str):
    return decode_png(Path(path).read_bytes())


def letter(c, extra: dict) -> str:
    c = tuple(c[:3])
    if c in NAMES:
        return NAMES[c]
    if c not in extra:
        extra[c] = "?" if len(extra) > 25 else "nopqrstuvwxyzNOPQRSTUVXYZ"[len(extra)]
    return extra[c]


def check(ref: str, x: int, y: int, gen, mask=(), name: str = "", show: bool = True, quiet=False,
          width: int = 60, max_rows: int = 30) -> int:
    R = load(ref)
    diffs = 0
    masked = set()
    for (mx, my, mw, mh) in mask:  # reference (screen) coordinates
        for yy in range(my, my + mh):
            for xx in range(mx, mx + mw):
                masked.add((xx - x, yy - y))
    bad = [[False] * gen.w for _ in range(gen.h)]
    for j in range(gen.h):
        for i in range(gen.w):
            g = gen.get(i, j)
            if g[3] == 0 or (i, j) in masked:
                continue
            r = R.get(x + i, y + j)
            if tuple(r[:3]) != tuple(g[:3]):
                diffs += 1
                bad[j][i] = True
    if not quiet:
        print(f"{name or 'region'} @{x},{y} {gen.w}x{gen.h}: {diffs} differing pixels")
    if diffs and show:
        ex: dict = {}
        cols = [i for j in range(gen.h) for i in range(gen.w) if bad[j][i]]
        lo = max(0, min(cols) - 4)
        hi = min(gen.w, max(cols) + 5, lo + width)
        shown = 0
        print(f"    columns {lo}..{hi - 1} (ref | generated | diff)")
        for j in range(gen.h):
            if not any(bad[j][lo:hi]) or shown >= max_rows:
                continue
            shown += 1
            a = "".join(letter(R.get(x + i, y + j), ex) for i in range(lo, hi))
            b = "".join((" " if gen.get(i, j)[3] == 0 else letter(gen.get(i, j), ex)) for i in range(lo, hi))
            m = "".join("^" if bad[j][i] else " " for i in range(lo, hi))
            print(f"{j:3d} {a} | {b} | {m}")
        print("   ", "  ".join(f"{v}={k[0]:02x}{k[1]:02x}{k[2]:02x}" for k, v in ex.items()))
    return diffs
