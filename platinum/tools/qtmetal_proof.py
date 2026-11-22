"""Compose an empty QuickTime Player window from the extracted pieces and compare it
with a capture of Mac OS 9 (platinum/reports/qtplayer-metal-2026-10-24.md).

    python3 tools/qtmetal_proof.py --source DISK.img --ref CAPTURE.png --at 24 50 --out DIR
    python3 tools/qtmetal_proof.py --rsrc QTP.rsrc --data QTP.data --ref ... --at X Y --out DIR

The window is an empty player ("Untitled 1", 352 x 106) drawn the way QuickTime
Player 6.0.3 draws it: the body, then each control re-rendering the body behind
itself.  The time display shows 00:00:00 and the volume is at its maximum.  The
title's glyphs come from the approximate Arial strike (FreeType) when FreeType is
available; they are counted separately.  DIR gets composite.png and diff.png
(Apple-derived: keep DIR private).
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rbplatinum import metal  # noqa: E402
from rbplatinum.image import Image, decode_png  # noqa: E402
from rbplatinum.macdisk import open_disk  # noqa: E402
from rbplatinum.rsrc import ResourceFile  # noqa: E402

W, H = 352, 106


def hexrgb(s):
    return tuple(int(s[i:i + 2], 16) for i in (1, 3, 5))


def compose(p: metal.Player, arial=None) -> tuple[Image, tuple[int, int, int, int]]:
    dev, parts = p.device, p.parts
    img = metal.window(dev, parts, W, H, p.bevel)
    lx, ly, lw, lh = metal.layout("lcd", W, H)
    metal.lcd(img, dev, parts, W, H, lx, ly, lw, lh)
    c = {k: hexrgb(v) for k, v in p.colors.items()}
    # The time slider (ALCs, 'LCDS'): its frame erased to the LCD fill, the track,
    # the playhead at 0, then the time.
    for y in range(26, 46):
        for x in range(8, 343):
            img.px[y * W + x] = c["fill"] + (255,)
    metal.lcd_track(img, 61, 338, 31, 39, c["ink"], c["shadow"])
    metal.lcd_playhead(img, 60, 28, 39, c["head"], c["shadow"], c["fill"])
    for x in (12, 28, 44):
        metal.lcd_text(img, p.sydnie, x, 40, "00", c["ink"], c["shadow"])
    for x in (25, 41):                       # the colons: 2 x 2 squares, no shadow
        for y in (34, 35, 37, 38):
            for xx in (x, x + 1):
                img.px[y * W + xx] = c["ink"] + (255,)
    tx, ty, tw, th = metal.layout("title", W, H)
    metal.put_band(img, dev, W, H, tx, ty, tw, th)
    title_box = (tx, ty, tx + tw, ty + th)
    if arial is not None:
        draw_title(img, arial, p.title, "Untitled 1", tx, ty, tw)
    for name in ("close", "zoom", "collapse", "logo", "grow"):
        x, y = metal.layout(name, W, H)
        metal.control(img, dev, W, H, parts[name], x, y)
    gx, gy, _gw, _gh = metal.layout("transport", W, H)
    for name, (dx, dy) in (("back", (8, 2)), ("rewind", (40, 1)), ("play", (80, 0)), ("forward", (124, 1)), ("end", (164, 2))):
        metal.control(img, dev, W, H, parts[name], gx + dx, gy + dy)
    qx, qy = metal.layout("quicktime", W, H)
    metal.control(img, dev, W, H, parts["quicktime"], qx, qy)
    sx, sy = metal.layout("speaker", W, H)
    metal.control(img, dev, W, H, parts["speaker"], sx, sy)
    vx, vy = metal.layout("volume", W, H)
    track = parts["volume-track"]
    metal.control(img, dev, W, H, track, vx, vy)
    thumb = parts["volume-thumb"]
    metal.over(img, thumb, vx + track.w - thumb.w, vy)          # volume at its maximum
    return img, title_box


def draw_title(img, strike, title, text, x, y, w):
    """Approximation: Mac OS 9 smooths Arial differently from RetroBoot's FreeType strike."""
    from rbplatinum.ttfwriter import mac_blend
    traits = title.get("active") or {}
    ink = hexrgb(traits.get("color", "#000000"))
    emb = hexrgb(title.get("emboss", "#d7d7d7"))
    ox, oy = title.get("emboss_offset", [1, 1])
    width = sum(strike.glyph(ord(ch)).advance for ch in text if strike.glyph(ord(ch)))
    pen0 = x + (w - width) // 2
    base = y + 13
    for dx, dy, color in ((ox, oy, emb), (0, 0, ink)):
        pen = pen0 + dx
        for ch in text.encode("mac_roman"):
            g = strike.glyph(ch)
            if g is None:
                continue
            for r, row in enumerate(g.rows):
                for cc, v in enumerate(row):
                    xx, yy = pen + g.left + cc, base + dy - g.top + r
                    if v and 0 <= xx < img.w and 0 <= yy < img.h:
                        d = img.px[yy * img.w + xx]
                        img.px[yy * img.w + xx] = tuple(mac_blend(d[i], color[i], v) for i in range(3)) + (d[3],)
            pen += g.advance


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source")
    ap.add_argument("--rsrc")
    ap.add_argument("--data")
    ap.add_argument("--ref", required=True)
    ap.add_argument("--at", nargs=2, type=int, required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    arial = None
    if a.source:
        disk = open_disk(a.source)
        path = next(c for c in metal.PLAYER_PATHS if disk.exists(c))
        rf = ResourceFile(disk.read_fork(path, "rsrc"))
        data = disk.read_fork(path, "data")
        try:
            from rbplatinum.ttraster import TrueTypeFace
            sf = ResourceFile(disk.read_fork("System Folder:Fonts:Arial", "rsrc")).of_type("sfnt")
            arial = TrueTypeFace(sf[0].data).strike("Arial", 13, True)
        except (OSError, FileNotFoundError, IndexError, ValueError):
            arial = None
    else:
        rf = ResourceFile(Path(a.rsrc).read_bytes())
        data = Path(a.data).read_bytes()
    p = metal.load(rf, data)
    img, title = compose(p, arial)
    ref = decode_png(Path(a.ref).read_bytes())
    X0, Y0 = a.at
    total = same = tbox = tsame = 0
    worst = 0
    diff = Image(W, H)
    for y in range(H):
        for x in range(W):
            if not metal.inside(W, H, x, y):
                diff.px[y * W + x] = (80, 80, 255, 255)
                continue
            r = ref.get(X0 + x, Y0 + y)[:3]
            o = img.px[y * W + x][:3]
            d = max(abs(i - j) for i, j in zip(r, o))
            in_title = title[0] <= x < title[2] and title[1] <= y < title[3] and 145 <= x < 210 and 5 <= y < 18
            total += 1
            same += d == 0
            if in_title:
                tbox += 1
                tsame += d == 0
            else:
                worst = max(worst, d)
            diff.px[y * W + x] = (0, 200, 0, 255) if d == 0 else ((255, 0, 0, 255) if d <= 2 else (0, 0, 0, 255))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "composite.png").write_bytes(img.png())
    (out / "diff.png").write_bytes(diff.scaled(3).png())
    res = {"window_pixels": total, "identical": same, "different": total - same,
           "title_glyph_box": {"pixels": tbox, "identical": tsame}, "outside_title_max_diff": worst,
           "title_strike": "FreeType Arial 13 (approximate)" if arial else None}
    print(json.dumps(res, indent=1))
    return 0 if worst == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
