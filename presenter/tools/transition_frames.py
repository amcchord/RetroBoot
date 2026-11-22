#!/usr/bin/env python3
"""Frame-accurate check of the startup cut-over and guest mode switches in an
ati_lab.py run (presented.rbfl, guest.rbfl, events.jsonl; lab tool).

    transition_frames.py RUN [--png] [--json]

Cut-over (ARCHITECTURE 4.4.1), from the first guest content to 10 s after it:
every presented frame's icon screen (where the indicator's bar and Mac OS's
face are, from the presenter's status) and a background point inside Mac OS's
picture are classified:
  icon:  indicator  -- the indicator (before the reveal on the checkerboard;
                       after it, unchanged over Mac OS's gray)
         macos      -- exactly what Mac OS shows there (its own pixels)
         blend      -- neither: a fade in progress
  bg:    pattern (the checkerboard, or a dither Mac OS draws) / flat
and the sequence of (icon, bg) runs with times is reported, plus when Mac OS's
own Happy Mac appeared in the guest frames.

Guest mode switch (4.5), for every guest_surface after the first content:
every presented frame from 0.5 s before to 2.5 s after is compared, inside
the union of the old and new pictures, with the last frame before the switch
("old") and the last frame of the window ("final", the settled desktop). Any
other frame is "partial" (Mac OS's clear, a half-drawn desktop or a stale
stride); the report lists them with their times and how long they showed.

--png writes OUT RUN/transition/*.png crops for the report. Python standard
library only (framelog.py next to it).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import framelog  # noqa: E402


def load(run: Path):
    res = json.loads((run / "result.json").read_text())
    ev = [json.loads(l) for l in open(run / "events.jsonl")]
    return res, ev


def pres_events(ev, name):
    return [e for e in ev if e.get("presenter", {}).get("event") == name]


def crop(canvas, w, rect):
    x, y, cw, ch = rect
    out = bytearray()
    for yy in range(y, y + ch):
        o = (yy * w + x) * 4
        out += canvas[o:o + cw * 4]
    return bytes(out)


def px(canvas, w, x, y):
    o = (y * w + x) * 4
    return canvas[o] | canvas[o + 1] << 8 | canvas[o + 2] << 16


def guest_rect_scaled(gcanvas, gw, rect_g, k):
    """The guest's rect (guest pixels) as the presenter shows it at factor k."""
    x, y, cw, ch = rect_g
    out = bytearray()
    for yy in range(y, y + ch):
        row = bytearray()
        o = (yy * gw + x) * 4
        src = gcanvas[o:o + cw * 4]
        for i in range(cw):
            row += src[i * 4:i * 4 + 4] * k
        out += bytes(row) * k
    return bytes(out)


def cutover(run: Path, res, ev, png_dir=None):
    import bisect
    content = pres_events(ev, "guest_content")
    if not content:
        return {"error": "no guest content"}
    t_c = content[0]["presenter"]["mono_ms"] / 1000.0
    status = res.get("status") or {}
    st = status.get("startup") or {}
    geom = st.get("geometry") or [0, 0, 1, 0, 0]
    ix, iy, q = geom[0], geom[1], geom[2]
    cs = st.get("checker_scale") or 1
    icon_rect = (ix, iy, 32 * q, 32 * q)
    # the icon's screen (the indicator's bar, Mac OS's face): opaque in both, so
    # a lighter repaint of Mac OS's gray around them does not change it
    screen = (ix + 7 * q, iy + 4 * q, 17 * q, 13 * q)
    revealed = pres_events(ev, "guest_revealed")
    t_rev = revealed[0]["presenter"]["mono_ms"] / 1000.0 if revealed else None
    # the guest's frames in the window, and when Mac OS's own Happy Mac is drawn
    gframes, g_icon = [], []
    for fr in framelog.read(run / "guest.rbfl"):
        if fr.t > t_c + 10:
            break
        if fr.t < t_c - 0.5:
            gframes = [(fr.t, bytes(fr.canvas), fr.w, fr.h)]
            continue
        gframes.append((fr.t, bytes(fr.canvas), fr.w, fr.h))
        gx, gy = (fr.w - 32) // 2, (fr.h - 32) // 2
        bg = px(fr.canvas, fr.w, gx, gy)
        n = sum(1 for yy in range(gy, gy + 32) for xx in range(gx, gx + 32) if px(fr.canvas, fr.w, xx, yy) != bg)
        if not g_icon or (g_icon[-1]["nonbg_px"] >= 48) != (n >= 48):
            g_icon.append({"t_ms": round((fr.t - t_c) * 1000, 1), "nonbg_px": n})
    gtimes = [g[0] for g in gframes]
    runs, shots = [], []
    indicator = None   # the icon rectangle at the reveal (the indicator over Mac OS, if it stayed)
    prev = None
    for fr in framelog.read(run / "presented.rbfl"):
        if fr.t < t_c - 0.3:
            continue
        if fr.t > t_c + 10:
            break
        cur = crop(fr.canvas, fr.w, screen)
        bx, by = icon_rect[0] - 4 * cs, icon_rect[1]
        bg = "pattern" if px(fr.canvas, fr.w, bx, by) != px(fr.canvas, fr.w, bx + cs, by) else "flat"
        macos = False
        i = bisect.bisect_right(gtimes, fr.t) - 1
        if i >= 0:
            _, gc, gw, gh = gframes[i]
            k = (status.get("layout") or {}).get("k") or q
            gx, gy = (gw - 32) // 2, (gh - 32) // 2
            macos = cur == guest_rect_scaled(gc, gw, (gx + 7, gy + 4, 17, 13), k)
        if t_rev is None or fr.t < t_rev - 0.0005:
            icls = "indicator"            # on the checkerboard, the bar filling
        elif macos:
            icls = "macos"                # exactly Mac OS's pixels
        else:
            if indicator is None:
                indicator = cur
            icls = "indicator" if cur == indicator else "blend"
        cls = (icls, bg)
        if cls != prev:
            runs.append({"t_ms": round((fr.t - t_c) * 1000, 1), "icon": icls, "bg": bg, "frames": 0})
            prev = cls
        runs[-1]["frames"] += 1
        if png_dir is not None and (not shots or shots[-1][1] != cur or shots[-1][6] != bg or icls == "blend"):
            shots.append((fr.t - t_c, cur, bytes(fr.canvas), fr.w, fr.h, icls, bg))
    if png_dir is not None:
        png_dir.mkdir(parents=True, exist_ok=True)
        for t, _, canvas, w, h, icls, bg in shots[:200]:
            framelog.write_png(png_dir / f"cut_{t * 1000:+08.0f}ms_{icls}_{bg}.png",
                               framelog.Frame(0, 0, 0, w, h, 0, 0, canvas, []))
    return {"content_s": round(t_c - res["t0"], 3),
            "revealed_after_content_ms": round((t_rev - t_c) * 1000) if t_rev else None,
            "icon_rect": icon_rect, "guest_icon": g_icon, "presented": runs}


def switches(run: Path, res, ev, png_dir=None):
    content = pres_events(ev, "guest_content")
    if not content:
        return []
    t_c = content[0]["presenter"]["mono_ms"] / 1000.0
    out = []
    for e in pres_events(ev, "guest_surface"):
        t_s = e["mono"]
        if t_s < t_c:
            continue
        window = []
        for fr in framelog.read(run / "presented.rbfl"):
            if fr.t < t_s - 0.5:
                window = [(fr.t, bytes(fr.canvas), fr.w, fr.h)]  # the last frame before the window
                continue
            if fr.t > t_s + 2.5:
                break
            window.append((fr.t, bytes(fr.canvas), fr.w, fr.h))
        if len(window) < 2:
            continue
        before = [w for w in window if w[0] < t_s]
        old = before[-1][1] if before else window[0][1]
        final = window[-1][1]
        rows = []
        for t, canvas, w, h in window:
            if t < t_s - 0.5:
                continue
            kind = "old" if canvas == old else "final" if canvas == final else "partial"
            if rows and rows[-1]["kind"] == kind and kind != "partial":
                rows[-1]["frames"] += 1
                continue
            rows.append({"t_ms": round((t - t_s) * 1000, 1), "kind": kind, "frames": 1})
        for i, r in enumerate(rows):
            nxt = rows[i + 1]["t_ms"] if i + 1 < len(rows) else None
            r["shown_ms"] = round(nxt - r["t_ms"], 1) if nxt is not None else None
        partial = [r for r in rows if r["kind"] == "partial" and r["t_ms"] >= 0]
        first_new = next((r["t_ms"] for r in rows if r["kind"] != "old" and r["t_ms"] >= 0), None)
        out.append({"at_s": round(t_s - res["t0"], 3), "size": e["presenter"]["size"], "frames": rows,
                    "partial_frames": len(partial),
                    "partial_ms": round(sum(r["shown_ms"] or 0 for r in partial), 1),
                    "first_new_frame_ms": first_new})
        if png_dir is not None:
            png_dir.mkdir(parents=True, exist_ok=True)
            k = len(out) - 1
            last = None
            if before:
                t, canvas, w, h = before[-1]
                framelog.write_png(png_dir / f"switch{k}_{(t - t_s) * 1000:+08.1f}ms_before.png",
                                   framelog.Frame(0, 0, 0, w, h, 0, 0, canvas, []))
                last = canvas
            for t, canvas, w, h in window:
                if t < t_s or canvas == last:
                    continue
                last = canvas
                kind = "old" if canvas == old else "final" if canvas == final else "partial"
                framelog.write_png(png_dir / f"switch{k}_{(t - t_s) * 1000:+08.1f}ms_{kind}.png",
                                   framelog.Frame(0, 0, 0, w, h, 0, 0, canvas, []))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run")
    ap.add_argument("--png", action="store_true")
    a = ap.parse_args()
    run = Path(a.run)
    res, ev = load(run)
    png = run / "transition" if a.png else None
    out = {"run": str(run), "card": res.get("card"), "cutover": cutover(run, res, ev, png),
           "switches": switches(run, res, ev, png)}
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
