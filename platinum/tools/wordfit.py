#!/usr/bin/env python3
"""Measure how a font sets the name in Mac OS 9's startup box (lab tool).

The startup box (System 'PICT' -16506, rbplatinum ``startup``) carries
"Mac OS 9.2" as anti-aliased artwork.  Given that artwork (a private PNG of
the decoded picture), the rectangle holding the name and an outline font,
this fits the setting -- pixels per em, horizontal scale, stroke change,
baseline and every glyph's position at 1/16 pixel -- by coordinate descent
on the coverage error, and reports how close the font comes: mean absolute
coverage error and the share of pixels that are exactly the artwork's gray
after the sixteen-gray reduction (rbplatinum.wordmark).  The result's
``style`` is what ``rbplatinum build --wordmark-style`` takes for RetroBoot's
own name.

    wordfit.py --box BOX.png --rect 70,165,280,70 --text "Mac OS 9.2" --font FONT
        [--only "Mac OS"] [--png OUT.png] [--fix hscale=1.0,...]

``--only`` limits the fit to glyphs the font has (the rest of the rectangle
right of them is ignored).  Standard library only; FreeType through
rbplatinum.  Inputs and outputs are Apple-derived: keep them private (D012).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rbplatinum.image import Image, decode_png  # noqa: E402
from rbplatinum.wordmark import OutlineFont, Style  # noqa: E402


class Fit:
    def __init__(self, font: OutlineFont, target: list[list[int]], text: str, only: str | None):
        self.font, self.T, self.text = font, target, text
        self.h, self.w = len(target), len(target[0])
        self.glyphs = [(i, ch) for i, ch in enumerate(text) if ch != " "]
        if only is not None:
            self.glyphs = [(i, ch) for i, ch in self.glyphs if i < len(only)]
        self.cache: dict = {}
        self.limit_x = self.w

    def glyph(self, ch, st: Style, fx: float, fy: float):
        key = (ch, round(st.size, 3), round(st.hscale, 4), round(st.bold, 3), round(fx * 16), round(fy * 16))
        g = self.cache.get(key)
        if g is None:
            g = self.font.glyph(ch, st, round(fx * 16) / 16, round(fy * 16) / 16)
            self.cache[key] = g
        return g

    def advance(self, ch, st):
        return self.glyph(ch, st, 0.0, 0.0)[3]

    def render(self, p: dict) -> list[list[int]]:
        st = Style(size=p["size"], hscale=p["hscale"], bold=p["bold"])
        R = [[0] * self.w for _ in range(self.h)]
        for i, ch in self.glyphs:
            x = p["x"][i]
            ix, iy = int(x // 1), int(p["base"] // 1)
            rows, left, top, _ = self.glyph(ch, st, x - ix, -(p["base"] - iy))
            ox, oy = ix + left, iy - top
            for yy, row in enumerate(rows):
                y = oy + yy
                if not 0 <= y < self.h:
                    continue
                Ry = R[y]
                for xx, v in enumerate(row):
                    if v:
                        X = ox + xx
                        if 0 <= X < self.w:
                            Ry[X] = min(255, Ry[X] + v)
        return R

    def loss(self, p: dict) -> float:
        R = self.render(p)
        s = 0
        for y in range(self.h):
            Ty, Ry = self.T[y], R[y]
            for x in range(self.limit_x):
                s += abs(Ty[x] - Ry[x])
        return s / (self.h * self.limit_x * 255.0)

    def initial(self) -> dict:
        """Scale from the first glyph's ink height (a capital standing on the
        baseline), then every glyph placed greedily left to right where it
        fits the artwork best."""
        cols = [x for x in range(self.w) if any(self.T[y][x] > 64 for y in range(self.h))]
        first = range(cols[0], cols[0] + 8)
        rows = [y for y in range(self.h) if any(self.T[y][x] > 128 for x in first)]
        i0, ch0 = self.glyphs[0]
        _, _, top100, _ = self.font.glyph(ch0, Style(size=100.0), 0, 0)
        size = 100.0 * (rows[-1] - rows[0] + 1) / top100
        p = {"size": size, "hscale": 1.0, "bold": 0.0, "base": rows[-1] + 1.0, "x": {}}
        st = Style(size=size)
        pen = float(cols[0])
        placed = []
        for i, ch in enumerate(self.text):
            if ch == " ":
                pen += self.font.glyph(" ", st, 0, 0)[3] if self.font.has(" ") else size * 0.25
                continue
            if i not in dict(self.glyphs):
                break
            _, left, _, adv = self.font.glyph(ch, st, 0, 0)
            guess = pen - (left if not placed else 0)
            best = None
            for d in range(-20, 21):
                q = {**p, "x": {**p["x"], i: guess + d}}
                saved = self.glyphs
                self.glyphs = placed + [(i, ch)]
                lq = self.loss(q)
                self.glyphs = saved
                if best is None or lq < best[0]:
                    best = (lq, guess + d)
            p["x"][i] = best[1]
            placed.append((i, ch))
            pen = best[1] + adv
        return p

    def descend(self, p: dict, fixed: dict, rounds: int = 6) -> tuple[dict, float]:
        best = self.loss(p)
        steps = {"size": 1.0, "hscale": 0.02, "bold": 0.2, "base": 0.5, "x": 1.0}
        for _ in range(rounds):
            improved = True
            while improved:
                improved = False
                for k in ("size", "hscale", "bold", "base"):
                    if k in fixed:
                        continue
                    for d in (steps[k], -steps[k]):
                        q = dict(p)
                        q[k] = p[k] + d
                        lq = self.loss(q)
                        if lq < best - 1e-9:
                            p, best, improved = q, lq, True
                            break
                for i, _ in self.glyphs:
                    for d in (steps["x"], -steps["x"]):
                        q = dict(p)
                        q["x"] = dict(p["x"])
                        q["x"][i] = p["x"][i] + d
                        lq = self.loss(q)
                        if lq < best - 1e-9:
                            p, best, improved = q, lq, True
                            break
            for k in steps:
                steps[k] /= 2
        return p, best

    def exact_share(self, p: dict) -> float:
        R = self.render(p)
        same = n = 0
        for y in range(self.h):
            for x in range(self.limit_x):
                a = 255 - self.T[y][x]
                b = 255 - ((R[y][x] * 15 + 127) // 255) * 17
                same += a == b
                n += 1
        return same / n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--box", required=True)
    ap.add_argument("--rect", required=True, help="X,Y,W,H of the name in the box")
    ap.add_argument("--text", required=True)
    ap.add_argument("--font", required=True)
    ap.add_argument("--only", help="fit only this prefix of --text")
    ap.add_argument("--fix", default="", help="e.g. hscale=1.0,bold=0")
    ap.add_argument("--png", help="write artwork | fit | difference side by side")
    a = ap.parse_args(argv)
    img = decode_png(Path(a.box).read_bytes())
    X, Y, W, H = (int(v) for v in a.rect.split(","))
    target = [[255 - img.get(X + x, Y + y)[0] for x in range(W)] for y in range(H)]
    fit = Fit(OutlineFont(Path(a.font).read_bytes()), target, a.text, a.only)
    missing = fit.font.missing(a.only or a.text)
    if missing:
        raise SystemExit(f"the font lacks {missing!r}")
    p = fit.initial()
    if a.only:
        # ignore the artwork right of the last fitted glyph
        last = max(i for i, _ in fit.glyphs)
        st = Style(size=p["size"])
        fit.limit_x = min(W, int(p["x"][last] + fit.advance(a.text[last], st) + 2))
    fixed = {}
    for kv in filter(None, a.fix.split(",")):
        k, v = kv.split("=")
        fixed[k] = float(v)
        p[k] = float(v)
    p, best = fit.descend(p, fixed)
    st = Style(size=p["size"], hscale=p["hscale"], bold=p["bold"])
    gl = [i for i, _ in fit.glyphs]
    gaps = []
    for j in range(1, len(gl)):
        i0, i1 = gl[j - 1], gl[j]
        if i1 != i0 + 1:
            continue
        gaps.append(round(p["x"][i1] - p["x"][i0] - fit.advance(a.text[i0], st), 3))
    out = {"font": a.font, "text": a.text, "fitted": "".join(ch for _, ch in fit.glyphs),
           "style": {"size": round(p["size"], 4), "hscale": round(p["hscale"], 4), "bold": round(p["bold"], 4)},
           "baseline": round(p["base"] + Y, 3), "x": {str(i): round(v + X, 3) for i, v in p["x"].items()},
           "pair_gaps_px": gaps, "tracking_px": round(sum(gaps) / len(gaps), 3) if gaps else 0.0,
           "mean_abs_coverage_error": round(best, 5), "exact_gray_share": round(fit.exact_share(p), 4),
           "columns": fit.limit_x}
    print(json.dumps(out, indent=1))
    if a.png:
        R = fit.render(p)
        lim = fit.limit_x
        im = Image(lim * 3 + 4, H, (255, 255, 255, 255))
        for y in range(H):
            for x in range(lim):
                t = 255 - target[y][x]
                r = 255 - ((R[y][x] * 15 + 127) // 255) * 17
                im.set(x, y, (t, t, t))
                im.set(lim + 2 + x, y, (r, r, r))
                d = min(255, abs(t - r) * 2)
                im.set(2 * lim + 4 + x, y, (255, 255 - d, 255 - d))
        Path(a.png).write_bytes(im.png())
    return 0


if __name__ == "__main__":
    sys.exit(main())
