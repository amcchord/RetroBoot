#!/usr/bin/env python3
"""Black frames in a presenter stub frame log (RBD_FRAME_LOG, rbd_presenter_stub.py).

    rbd_black_frames.py FRAMES.jsonl [--from N]

A frame is black when at least 97 % of the stub's 32 x 24 samples are near black.
Prints how many frames were black, and how many of those were single black frames
between two pictures: the flicker of a display that shows a buffer it should not
(scene changes and fades are longer runs).
"""
from __future__ import annotations

import argparse
import json


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("frames")
    ap.add_argument("--from", dest="start", type=int, default=0, help="first line to count (0-based)")
    a = ap.parse_args()
    rows = [json.loads(line) for line in open(a.frames)][a.start:]
    if len(rows) < 3:
        print("too few frames")
        return 1
    black = [r["black"] >= 0.97 for r in rows]
    single = sum(1 for k in range(1, len(rows) - 1) if black[k] and not black[k - 1] and not black[k + 1])
    secs = rows[-1]["mono"] - rows[0]["mono"]
    print(f"{len(rows)} frames in {secs:.0f} s: {sum(black)} black ({100 * sum(black) / len(rows):.1f} %), "
          f"{single} single black frames between pictures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
