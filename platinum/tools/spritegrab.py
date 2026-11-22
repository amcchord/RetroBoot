"""Grab a small control sprite from a reference: outside pixels (background
color connected to the crop border) become '.' (developer tool for chrome.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.tablegrab import grab  # noqa: E402


def spritegrab(ref, x, y, w, h, bg="d"):
    rows, ex = grab(ref, x, y, w, h)
    g = [list(r) for r in rows]
    stack = [(i, j) for i in range(w) for j in (0, h - 1)] + [(i, j) for j in range(h) for i in (0, w - 1)]
    while stack:
        i, j = stack.pop()
        if 0 <= i < w and 0 <= j < h and g[j][i] == bg:
            g[j][i] = "."
            stack += [(i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1)]
    return ["".join(r) for r in g], ex


if __name__ == "__main__":
    rows, ex = spritegrab(sys.argv[1], *map(int, sys.argv[2:6]), bg=sys.argv[6] if len(sys.argv) > 6 else "d")
    print("\n".join(rows))
    if ex:
        print("# extra:", ex)
