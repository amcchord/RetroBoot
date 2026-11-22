"""Print a reference region as a palette-letter table (developer tool for chrome.py).

    python3 tools/tablegrab.py REF.png X Y W H [--bg LETTER]

Colors outside the canvas palette are listed after the table; --bg maps a
background letter to '_' (unchanged) so tables can be stamped over any face.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.refcheck import load, letter  # noqa: E402


def grab(path, x, y, w, h, bg=None):
    R = load(path)
    ex = {}
    rows = []
    for j in range(h):
        row = ""
        for i in range(w):
            ch = letter(R.get(x + i, y + j), ex)
            row += "_" if bg and ch == bg else ch
        rows.append(row)
    return rows, ex


if __name__ == "__main__":
    a = sys.argv[1:]
    bg = None
    if "--bg" in a:
        k = a.index("--bg")
        bg = a[k + 1]
        del a[k:k + 2]
    rows, ex = grab(a[0], *map(int, a[1:5]), bg=bg)
    print('"""')
    print("\n".join(rows))
    print('"""')
    if ex:
        print("# extra:", "  ".join(f"{v}={k[0]:02x}{k[1]:02x}{k[2]:02x}" for k, v in ex.items()))
