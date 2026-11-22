#!/usr/bin/env python3
"""Pixel-exact check of a composed/outer frame against the pixel scale (lab).

Given a frame of the whole display (presenter `screenshot what=composed`, or an
outer_capture.py PNG) and the guest layout (k, dst) it verifies that

* every guest pixel is one uniform k x k block (no seams, no off-by-one rows or
  columns): the frame inside dst equals the block-replicated top-left pixels;
* the block grid starts on the shared s grid (dst.x, dst.y multiples of s) and
  the margins (the remainder < s per axis plus the width rounding) are black;
* optionally, the de-scaled picture equals the raw guest surface (`screenshot
  what=guest`, taken right after: a changed clock or cursor shows up as a small
  mismatch rectangle, reported with its bounds; at least 99 % must match);
* `--checker S`: the frame is exactly the 4.4 checkerboard at scale S, at
  any anchor (RetroBoot anchors it at Mac OS's picture; the anchor found is
  reported, `--anchor X,Y` requires one: black at that pixel's cell).

  scale_check.py --frame composed.png --k 2 --dst 0,0,1920,1080 --s 2 [--guest guest.png]
  scale_check.py --frame composed.png --status status.json [--guest guest.png]
  scale_check.py --frame outer.png --checker 3 [--anchor 0,0]

Prints one JSON object; exit status 0 when every check passes.
"""
import argparse
import json
import sys

import numpy as np
from PIL import Image


def load(path):
    return np.asarray(Image.open(path).convert('RGB'))


def bbox(mask):
    ys, xs = np.nonzero(mask)
    if not len(ys):
        return None
    return [int(xs.min()), int(ys.min()), int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1)]


def check_checker(fb, s, anchor=None):
    h, w, _ = fb.shape
    y, x = np.mgrid[0:h, 0:w]
    if anchor is not None:
        cands = [(anchor[0] % s, anchor[1] % s, (anchor[0] // s + anchor[1] // s) % 2)]
    else:
        cands = [(ox, oy, ph) for oy in range(s) for ox in range(s) for ph in (0, 1)]
    best = None
    for ox, oy, ph in cands:
        black = ((((x - ox) // s) + ((y - oy) // s) + ph) % 2) == 0
        want = np.where(black[..., None], 0, 255).astype(np.uint8)
        bad = np.any(fb != want, axis=2)
        if best is None or bad.sum() < best[0].sum():
            best = (bad, (ox, oy, ph))
    bad, (ox, oy, ph) = best
    # the anchor found, as the frame pixel of a black cell nearest the origin
    ax, ay = ox + (s if ph else 0), oy
    return {'checker_scale': s, 'checker_anchor': [ax, ay], 'checker_mismatch_px': int(bad.sum()),
            'checker_mismatch_bbox': bbox(bad), 'ok': not bad.any()}


def check_blocks(fb, k, dst, s, guest=None):
    h, w, _ = fb.shape
    x0, y0, dw, dh = dst
    out = {'frame': [w, h], 'k': k, 'dst': dst, 's': s}
    gw, gh = dw // k, dh // k
    ok = dw == gw * k and dh == gh * k and x0 + dw <= w and y0 + dh <= h
    out['geometry_ok'] = bool(ok)
    if not ok:
        out['ok'] = False
        return out
    inner = fb[y0:y0 + dh, x0:x0 + dw].astype(np.int32)
    blocks = inner.reshape(gh, k, gw, k, 3)
    top = blocks[:, :1, :, :1, :]
    nonuniform = np.any(np.any(blocks != top, axis=4), axis=(1, 3))
    out['blocks'] = [gw, gh]
    out['nonuniform_blocks'] = int(nonuniform.sum())
    out['nonuniform_bbox_guest'] = bbox(nonuniform)
    margin = np.ones((h, w), bool)
    margin[y0:y0 + dh, x0:x0 + dw] = False
    nonblack = margin & np.any(fb != 0, axis=2)
    out['margins'] = {'left': x0, 'top': y0, 'right': w - x0 - dw, 'bottom': h - y0 - dh}
    out['margin_nonblack_px'] = int(nonblack.sum())
    out['on_grid'] = x0 % s == 0 and y0 % s == 0
    # centered within s - 1 pixels of the exact center
    out['centered'] = abs((w - dw) / 2 - x0) < s and abs((h - dh) / 2 - y0) < s
    ok = out['nonuniform_blocks'] == 0 and out['margin_nonblack_px'] == 0 and out['on_grid'] and out['centered']
    if guest is not None:
        descaled = top[:, 0, :, 0, :].astype(np.uint8)
        if guest.shape[:2] != (gh, gw):
            out['guest_size_mismatch'] = [guest.shape[1], guest.shape[0]]
            ok = False
        else:
            diff = np.any(descaled != guest, axis=2)
            out['guest_mismatch_px'] = int(diff.sum())
            out['guest_mismatch_bbox'] = bbox(diff)
            out['guest_match'] = round(1 - diff.mean(), 6)
            # a ticking clock or a moved cursor between the two screenshots is fine;
            # a shifted, stale or black picture is not
            ok = ok and out['guest_match'] >= 0.99
    out['ok'] = bool(ok)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--frame', required=True)
    ap.add_argument('--guest')
    ap.add_argument('--status', help='presenter status JSON (layout and display scale)')
    ap.add_argument('--k', type=int)
    ap.add_argument('--dst')
    ap.add_argument('--s', type=int)
    ap.add_argument('--checker', type=int, help='expect the checkerboard at this scale')
    ap.add_argument('--anchor', help='X,Y: the pattern is black at this pixel (Mac OS\'s picture origin)')
    a = ap.parse_args()
    fb = load(a.frame)
    if a.checker:
        res = check_checker(fb, a.checker, [int(v) for v in a.anchor.split(',')] if a.anchor else None)
    else:
        k, dst, s = a.k, None, a.s
        if a.dst:
            dst = [int(v) for v in a.dst.split(',')]
        if a.status:
            st = json.load(open(a.status))
            k = k or st['layout']['k']
            dst = dst or st['layout']['dst']
            s = s or st['display']['scale']
        res = check_blocks(fb, k, dst, s or 1, load(a.guest) if a.guest else None)
    print(json.dumps(res))
    return 0 if res['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
