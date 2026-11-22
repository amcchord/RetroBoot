#!/usr/bin/env python3
"""Pixel comparison of the installer startup screen with Mac OS's own (lab).

ARCHITECTURE 4.4.2: with the private art (rbplatinum's installer-startup.rbis)
RetroBoot's box must be Mac OS 9.2's startup box pixel for pixel everywhere
outside RetroBoot's name and the welcome line, at every guest size.  Inputs:

* guest frames of the real Mac OS 9.2.2 startup at 1x, one folder per size
  (framelog.py png of an ati_lab.py run's guest.rbfl, with index.jsonl);
* our boxes as the presenter draws them (isplash-render --box-only --art
  FILE: welcome.png and starting-NNN.png for every fill NNN);
* the RBIS file (the name's rectangle, the "WORD" entry).

For every frame in which Mac OS's box is complete (Mac OS's placement
((w - 422) / 2, round((h - 323) * 3 / 10))), the phase is read from the
frame (the "Starting Up..." line is the same text in both) and the bar's
fill is the one of ours that matches best; that box is compared.  A frame
whose only differences lie inside the bar is counted as caught while Mac OS
was drawing its bar (the guest's frame between two of its drawing calls:
the fill extended but its end not yet drawn, or the track not yet shaded);
any difference elsewhere fails.  The result is one JSON summary plus, with
--png, side-by-side images (Mac OS | ours | differences in red).  The frames and images contain Apple pixels: keep
them private (D012).  Needs Pillow and NumPy (private/presenter-core/venv).

    isplash_compare.py --rbis FILE --ours DIR --refs DIR [DIR...] [--png OUTDIR]
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

import numpy as np
from PIL import Image

BOX_W, BOX_H = 422, 323
STATUS = (4, 262, 414, 26)
BAR = (130, 290, 162, 14)
BAR_STEPS = BAR[2] - 4


def rbis_entries(path: Path) -> dict:
    d = path.read_bytes()
    if d[:4] != b"RBIS":
        raise SystemExit(f"{path}: not an RBIS file")
    n = struct.unpack("<H", d[6:8])[0]
    out = {}
    for i in range(n):
        tag, x, y, off, ln = struct.unpack("<4shhII", d[8 + 16 * i:24 + 16 * i])
        w, h = struct.unpack("<HH", d[off + 4:off + 8])
        out[tag.decode()] = (x, y, w, h)
    return out


def box_origin(w: int, h: int) -> tuple[int, int]:
    return max(0, (w - BOX_W) // 2), max(0, ((h - BOX_H) * 3 + 5) // 10)


def load(p: Path) -> np.ndarray:
    return np.asarray(Image.open(p).convert("RGB"), dtype=np.int16)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rbis", required=True)
    ap.add_argument("--ours", required=True)
    ap.add_argument("--refs", nargs="+", required=True)
    ap.add_argument("--png")
    a = ap.parse_args(argv)
    ents = rbis_entries(Path(a.rbis))
    nx, ny, nw, nh = ents.get("WORD", (0, 0, 0, 0))
    ours = Path(a.ours)
    welcome = load(ours / "welcome.png")
    starting = {n: load(ours / f"starting-{n:03d}.png") for n in range(5, BAR_STEPS + 1)}
    sx, sy, sw, sh = STATUS
    strt = starting[BAR_STEPS][sy:sy + sh, sx:sx + sw]
    bx, by, bw, bh = BAR
    summary = {"rbis": a.rbis, "name_rect": [nx, ny, nw, nh], "sizes": []}
    total_bad = 0
    for ref in a.refs:
        ref = Path(ref)
        idx = [json.loads(l) for l in (ref / "index.jsonl").read_text().splitlines()]
        W, H = idx[-1]["size"]
        X, Y = box_origin(W, H)
        res = {"refs": str(ref), "guest": [W, H], "box": [X, Y], "frames": 0, "welcome": 0, "starting": 0,
               "starting_without_bar": 0, "fills": [], "pixels_compared": 0, "pixels_differing": 0,
               "worst_frame": None, "skipped_incomplete": 0, "exact": 0, "bar_mid_draw": []}
        shown = None
        for r in idx:
            if tuple(r["size"]) != (W, H) or not r.get("changed_px", 1) and res["frames"]:
                continue
            f = load(ref / r["png"])
            boxf = f[Y:Y + BOX_H, X:X + BOX_W]
            # complete: Mac OS's frame and panel outline are there (the box draws top-down)
            if not (np.all(boxf[0, :] == 0) and np.all(boxf[BOX_H - 1, :] == 0) and np.all(boxf[248, 36:385] == 0)
                    and tuple(boxf[1, 5]) == (255, 255, 255) and tuple(boxf[10, 10]) == (221, 221, 221)
                    and tuple(boxf[100, 40]) == (255, 255, 255)):
                res["skipped_incomplete"] += 1
                continue
            mask = np.ones((BOX_H, BOX_W), bool)
            mask[ny:ny + nh, nx:nx + nw] = False          # RetroBoot's name
            is_starting = np.array_equal(boxf[sy:sy + sh, sx:sx + sw], strt)
            bar_f = boxf[by:by + bh, bx:bx + bw]
            has_bar = not np.array_equal(bar_f, welcome[by:by + bh, bx:bx + bw])
            if has_bar:
                fill = min(starting, key=lambda n: int(np.any(starting[n][by:by + bh, bx:bx + bw] != bar_f, axis=2).sum()))
                ourb = starting[fill]
                res["fills"].append(fill)
            else:
                ourb = welcome.copy()
                if is_starting:
                    ourb[sy:sy + sh, sx:sx + sw] = strt
            if is_starting:
                res["starting" if has_bar else "starting_without_bar"] += 1
            else:
                res["welcome"] += 1
                mask[sy:sy + sh, sx:sx + sw] = False      # "Welcome to RetroBoot" vs "Welcome to Mac OS"
            diff = np.any(boxf != ourb, axis=2) & mask
            bad = int(diff.sum())
            in_bar = int(diff[by:by + bh, bx:bx + bw].sum())
            res["frames"] += 1
            res["pixels_compared"] += int(mask.sum())
            if bad == 0:
                res["exact"] += 1
            elif bad == in_bar:
                res["bar_mid_draw"].append([r["png"], bad])
                bad = 0
            res["pixels_differing"] += bad
            if res["worst_frame"] is None or bad > res["worst_frame"][1]:
                res["worst_frame"] = [r["png"], bad]
            if a.png and (shown is None or (has_bar and shown != "bar") or diff.any()):
                shown = "bar" if has_bar else "text"
                side = np.full((BOX_H, BOX_W * 3 + 8, 3), 255, np.uint8)
                side[:, :BOX_W] = boxf
                side[:, BOX_W + 4:2 * BOX_W + 4] = ourb
                d = np.full((BOX_H, BOX_W, 3), 255, np.uint8)
                d[~mask] = (200, 200, 255)
                d[diff] = (255, 0, 0)
                side[:, 2 * BOX_W + 8:] = d
                out = Path(a.png)
                out.mkdir(parents=True, exist_ok=True)
                Image.fromarray(side).save(out / f"{W}x{H}-{r['png']}")
        res["fills"] = sorted(set(res["fills"]))
        total_bad += res["pixels_differing"]
        summary["sizes"].append(res)
    summary["pixels_differing"] = total_bad
    summary["pass"] = total_bad == 0 and all(s["frames"] for s in summary["sizes"])
    print(json.dumps(summary, indent=1))
    return 0 if summary["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
