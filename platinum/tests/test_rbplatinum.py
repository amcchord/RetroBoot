"""rbplatinum unit tests (standard library only; no Apple data needed).

    python3 -m unittest discover -s platinum/tests -v
    RB_PLATINUM_DISK=/private/copy/disk.img also runs the fixture test.
"""
from base64 import b64decode
import json
import os
import re
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rbplatinum import bitfont, chrome, icons, rbfont, ttfwriter  # noqa: E402
from rbplatinum.bitfont import BitFont, Glyph  # noqa: E402
from rbplatinum.canvas import Palette  # noqa: E402
from rbplatinum.image import Image, decode_png, encode_png  # noqa: E402
from rbplatinum.rsrc import ResourceFile  # noqa: E402
from rbplatinum.textdraw import blend  # noqa: E402


def tables(font: bytes) -> dict:
    n = struct.unpack(">H", font[4:6])[0]
    out = {}
    for i in range(n):
        tag, _cs, off, ln = struct.unpack(">4sIII", font[12 + 16 * i:28 + 16 * i])
        out[tag.decode()] = font[off:off + ln]
    return out


def synthetic_nfnt() -> bytes:
    """NFNT with 'A' (a 2x2 block) and a 1-pixel-wide missing glyph; ascent 3."""
    first = last = 0x41
    rowwords, height = 1, 3
    bits = bytes([0b11100000, 0, 0b11100000, 0, 0b00100000, 0])
    loc = [0, 2, 3]                               # A, missing, end
    ow = [(0 << 8) | 3, (0 << 8) | 2, -1]          # (offset << 8) | width, terminator
    hdr = struct.pack(">Hhhhhhhhhhhhh", 0x9000, first, last, 3, 0, -1, 3, height, 0, 3, 0, 0, rowwords)
    return hdr + bits + struct.pack(">3H", *loc) + struct.pack(">3h", *ow)


class Basics(unittest.TestCase):
    def test_png_roundtrip(self):
        img = Image(3, 2)
        img.set(0, 0, (255, 0, 0))
        img.set(2, 1, (1, 2, 3, 128))
        back = decode_png(encode_png(img))
        self.assertEqual(back.px, img.px)

    def test_clut8_is_the_mac_palette(self):
        self.assertEqual(len(icons.CLUT8), 256)
        self.assertEqual(icons.CLUT8[0], (255, 255, 255))
        self.assertEqual(icons.CLUT8[255], (0, 0, 0))
        self.assertEqual(icons.CLUT8[245], (0xEE, 0xEE, 0xEE))

    def test_mac_blend(self):
        # Measured on Mac OS 9.2.1: black on #dddddd, white on #333399 and on #666666.
        self.assertEqual(ttfwriter.mac_blend(0xDD, 0, 1), 0xCE)
        self.assertEqual(ttfwriter.mac_blend(0xDD, 0, 14), 0x0F)
        self.assertEqual(ttfwriter.mac_blend(0x33, 255, 15), 0xFE)
        self.assertEqual(ttfwriter.mac_blend(0x66, 255, 5), 152)
        self.assertEqual(ttfwriter.mac_blend(0x66, 255, 16), 255)
        self.assertEqual(blend((0xDD,) * 3 + (255,), (0, 0, 0), 8)[:3], (0x67,) * 3)

    def test_nfnt_and_bold(self):
        f = bitfont.parse_nfnt(synthetic_nfnt(), "T", 3)
        g = f.glyphs[0x41]
        self.assertEqual(g.advance, 3)
        self.assertEqual(g.rows[0], [16, 16])
        self.assertIsNotNone(f.missing)
        b = bitfont.embolden(f)
        self.assertEqual(b.glyphs[0x41].rows[0], [16, 16, 16])
        self.assertEqual(b.glyphs[0x41].advance, 4)


class Fonts(unittest.TestCase):
    def strike(self, smooth=False):
        f = BitFont("T", 12, "plain", 12, 3, 1, smooth=smooth)
        ring = [[16, 16, 16], [16, 0, 16], [16, 16, 16]]       # a hole: outer and inner contour
        f.glyphs[0x41] = Glyph(ring if not smooth else [[16, 8, 1], [16, 0, 16], [4, 16, 16]], 1, 9, 5)
        f.glyphs[0x20] = Glyph([], 0, 0, 4)
        f.glyphs[0x12] = Glyph([[16]], 0, 5, 6)                # check mark symbol
        f.missing = Glyph([[16]], 0, 5, 6)
        return f

    def test_aliases(self):
        f = self.strike()
        f.glyphs[0x78] = Glyph([[16]], 0, 5, 5)
        t = tables(ttfwriter.build(f, "RB Test"))
        cmap = t["cmap"]
        self.assertIn(struct.pack(">H", 0x78), cmap)
        self.assertIn(struct.pack(">H", 0xD7), cmap)         # x serves the multiplication sign
        b = rbfont.encode(f)
        self.assertIn(struct.pack("<I", 0xD7), b)

    def test_contours_keep_holes(self):
        loops = ttfwriter._contours({(x, y) for y in range(3) for x in range(3) if (x, y) != (1, 1)})
        self.assertEqual(len(loops), 2)
        self.assertEqual(sorted(len(l) for l in loops), [4, 4])

    def test_truetype_tables(self):
        data = ttfwriter.build(self.strike(), "RB Test")
        t = tables(data)
        for tag in ("head", "hhea", "maxp", "OS/2", "hmtx", "cmap", "loca", "glyf", "name", "post"):
            self.assertIn(tag, t)
        self.assertEqual(struct.unpack(">H", t["head"][18:20])[0], 12 * ttfwriter.UNITS)   # unitsPerEm
        self.assertEqual(struct.unpack(">h", t["hhea"][4:6])[0], 12 * ttfwriter.UNITS)     # ascent
        self.assertEqual(struct.unpack(">h", t["hhea"][6:8])[0], -4 * ttfwriter.UNITS)     # descent incl. leading
        self.assertNotIn("COLR", t)

    def test_colr_layers_and_palettes(self):
        pal = [((0, 0, 0), [17 * k for k in range(1, 16)] + [255]),
               ((255, 255, 255), [17] * 14 + [((254, 254, 254), 255), 255])]
        t = tables(ttfwriter.build(self.strike(True), "RB Test", palettes=pal))
        self.assertIn("COLR", t)
        _v, entries, npal, nrec, off = struct.unpack(">HHHHI", t["CPAL"][:12])
        self.assertEqual((entries, npal, nrec), (16, 2, 32))
        # palette 1, level 15: the per-level color override (#fefefe, alpha 255)
        base = off + 4 * (16 + 14)
        self.assertEqual(t["CPAL"][base:base + 4], bytes([254, 254, 254, 255]))
        _v, nbase, _o1, _o2, nlayers = struct.unpack(">HHIIH", t["COLR"][:14])
        self.assertEqual(nbase, 2)      # 'A' and the check mark have smoothed layers
        self.assertGreaterEqual(nlayers, 4)

    def test_rbf(self):
        b = rbfont.encode(self.strike(True))
        self.assertEqual(b[:4], b"RBF1")
        count = struct.unpack("<H", b[12:14])[0]
        cps = []
        pos = 14
        for _ in range(count):
            cp, _adv, _left, _top, w, h = struct.unpack("<IbbbBB", b[pos:pos + 9])
            cps.append(cp)
            pos += 9 + w * h
        self.assertEqual(pos, len(b))
        self.assertEqual(cps, sorted(cps))
        self.assertIn(0x2713, cps)      # Mac Roman 0x12 is the check mark
        f = self.strike(True)
        f.glyphs[0x14] = Glyph([[16]], 0, 5, 6)                # menu Apple logo ...
        f.glyphs[0xF0] = Glyph([[16, 16]], 0, 5, 7)            # ... and Mac Roman's: one U+F8FF
        b2 = rbfont.encode(f)
        glyphs, pos = [], 14
        for _ in range(struct.unpack("<H", b2[12:14])[0]):
            cp, adv, _l, _t, w, h = struct.unpack("<IbbbBB", b2[pos:pos + 9])
            glyphs.append((cp, adv))
            pos += 9 + w * h
        self.assertEqual([c for c, _a in glyphs], sorted({c for c, _a in glyphs}))
        self.assertIn((0xF8FF, 7), glyphs)
        self.assertIn(0xFFFD, cps)


class Chrome(unittest.TestCase):
    def test_sizes_and_slices(self):
        p = Palette(extra=chrome.ALERT_COLORS)
        self.assertEqual(chrome.window(64, 64, p).w, 65)
        self.assertEqual(chrome.button(40, 20, p, "default").h, 26)
        self.assertEqual(chrome.slices(chrome.BUTTON, 4), (4, 4, 4, 4))
        self.assertEqual(chrome.popup(48, p).h, 20)
        self.assertEqual(chrome.progress_fill_px(188, 0.0), 0)
        self.assertEqual(chrome.progress_fill_px(188, 1.0), 184)
        for rows in list(chrome.CHECKBOX.values()) + list(chrome.RADIO.values()):
            c = chrome.sprite(rows, p)
            self.assertIn(c.h, (12,))
        # every table row has the same total width once stretched
        for rows, rep in ((chrome.BUTTON_DEFAULT, 7), (chrome.ALERT, 3), (chrome.MOVABLE, 24), (chrome.MENU, 1)):
            c = chrome.stretch(rows, rep, 50, 40, p)
            self.assertEqual((c.w, c.h), (50, 40))

    def test_resource_fork_parser(self):
        import rbplatinum.macdisk  # noqa: F401  (puts rbguestprep on sys.path)
        from rbguestprep.hfsplus import build_resource_fork
        fork = build_resource_fork([("clut", 208, "Lavender", struct.pack(">IHH", 0, 0, 0) + struct.pack(">HHHH", 0, 0xEEEE, 0xEEEE, 0xEEEE))])
        rf = ResourceFile(fork)
        self.assertEqual(icons.parse_clut(rf.named("clut", "Lavender").data), [(0xEE, 0xEE, 0xEE)])


class BundleTests(unittest.TestCase):
    def test_builtin_chrome_matches_the_web_stylesheet(self):
        from rbplatinum import builtin

        css = Path(__file__).resolve().parents[2] / "web" / "css" / "platinum-theme.css"
        browser_sprites = dict(re.findall(r'--pt-([\w-]+): url\("data:image/png;base64,([^"]+)"\)', css.read_text()))
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "panel"
            builtin.build(out, None)
            manifest = json.loads((out / "manifest.json").read_text())
            self.assertEqual(manifest["format"], 1)
            sprites = manifest["chrome"]
            self.assertIn("button-default", sprites)
            self.assertEqual(browser_sprites.keys(), sprites.keys())
            for name, entry in sprites.items():
                with self.subTest(sprite=name):
                    img = decode_png(b64decode(browser_sprites[name]))
                    raw = (out / entry["rbi"]).read_bytes()
                    self.assertEqual(struct.unpack("<4sHH", raw[:8]), (b"RBI1", img.w, img.h))
                    self.assertEqual(raw[8:], bytes(channel for pixel in img.px for channel in pixel))

    def test_build_without_apple_data(self):
        from rbplatinum import bundle
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "disk"
            src.mkdir()
            out = Path(td) / "assets" / "platinum"
            m = bundle.build(str(src), out)
            self.assertTrue((out / "manifest.json").is_file())
            self.assertIn("button-default", m["chrome"])
            self.assertTrue((out / "chrome" / "window.png").is_file())
            self.assertIn("icon caution", m["missing"])
            self.assertEqual(json.loads((out / "manifest.json").read_text())["format"], 1)
            again = bundle.build(str(src), out)          # existing bundle: kept
            self.assertEqual(again["created"], m["created"])

    def test_nfnt_exact_owtable_length(self):
        # Apple Inside Macintosh NFNT has n locTable words and n - 1 owTable words.
        raw = synthetic_nfnt()[:-2]
        f = bitfont.parse_nfnt(raw, "T", 3)
        self.assertEqual(f.glyphs[0x41].advance, 3)

    def test_hfs_standard_disk_and_icon_fallback(self):
        import rbplatinum.macdisk as md
        from rbguestprep.hfsplus import build_resource_fork
        from rbguestprep import hfs

        # Synthetic minimal HFS Standard volume with System + Control Panels:Monitors & Sound
        with tempfile.TemporaryDirectory() as td:
            img = Path(td) / "hfs81.img"
            with open(img, "wb") as f:
                f.truncate(2 * 1024 * 1024)
            # Write HFS MDB signature so locate_volume finds offset=0 when initialized via hfs.
            img_dir = Path(td) / "folder"
            (img_dir / "System Folder" / "Extensions").mkdir(parents=True)
            sys_rsrc = build_resource_fork([])
            (img_dir / "System Folder" / "System.rsrc").write_bytes(sys_rsrc)
            # Fallback icon in Extensions:Appearance Extension cicn 2 works via FolderDisk too.
            cicn = (
                struct.pack(">IHhhhhHHIIIHHHHIII", 0, 0x8004, 0, 0, 2, 2, 0, 0, 0, 72 << 16, 72 << 16, 0, 8, 1, 8, 0, 0, 0)
                + bytes(4)
                + struct.pack(">Hhhhh", 2, 0, 0, 2, 2) + bytes(4)
                + struct.pack(">Hhhhh", 2, 0, 0, 2, 2) + bytes(4)
                + bytes([0xC0, 0, 0xC0, 0])
                + bytes(4)
                + struct.pack(">IHH", 0, 0, 0) + struct.pack(">4H", 0, 0xFFFF, 0, 0)
                + bytes([0, 0, 0, 0, 0, 0, 0, 0])
            )
            (img_dir / "System Folder" / "Extensions" / "Appearance Extension.rsrc").write_bytes(
                build_resource_fork([("cicn", 2, "", cicn)])
            )
            out = Path(td) / "out"
            m = md.open_disk(img_dir)
            m.close()
            from rbplatinum import bundle
            manifest = bundle.build(str(img_dir), out, force=True)
            self.assertIn("caution", manifest["icons"])
            self.assertTrue((out / "icons" / "caution-32.rbi").is_file())
            self.assertFalse(hfs.is_hfs_standard(str(img)))

    @unittest.skipUnless(os.environ.get("RB_PLATINUM_DISK"), "RB_PLATINUM_DISK not set")
    def test_fixture_disk(self):
        from rbplatinum import bundle
        with tempfile.TemporaryDirectory() as td:
            m = bundle.build(os.environ["RB_PLATINUM_DISK"], Path(td) / "p", force=True)
            for f in ("geneva-10", "geneva-9", "monaco-9", "chicago-12"):
                self.assertIn(f, m["fonts"])
            for i in ("caution", "stop", "note", "folder"):
                self.assertIn(i, m["icons"])
            self.assertIn("desktop", m)


def packbits(row: bytes) -> bytes:
    """Literal-run PackBits (enough for the decoder's tests)."""
    out = b""
    for i in range(0, len(row), 128):
        chunk = row[i:i + 128]
        out += bytes([len(chunk) - 1]) + chunk
    return out


def pict8(rows: list[bytes], clut: list[tuple[int, int, int]], extra_ops: bytes = b"") -> bytes:
    """A version 2 PICT: header, clip, one 8-bit PackBitsRect, end."""
    h, w = len(rows), len(rows[0])
    rb = w + (w & 1)
    if rb < 8:
        rb = 8
    body = struct.pack(">HH", 0x0011, 0x02FF) + struct.pack(">H", 0x0C00) + bytes(24)
    body += struct.pack(">HH4h", 0x0001, 10, 0, 0, h, w) + extra_ops
    pm = struct.pack(">H4h", 0x8000 | rb, 0, 0, h, w)
    pm += struct.pack(">HHIIIHHHHIII", 0, 0, 0, 72 << 16, 72 << 16, 0, 8, 1, 8, 0, 0, 0)
    ct = struct.pack(">IHH", 0, 0, len(clut) - 1) + b"".join(
        struct.pack(">4H", i, r * 257, g * 257, b * 257) for i, (r, g, b) in enumerate(clut))
    data = b""
    for row in rows:
        p = packbits(row.ljust(rb, b"\0"))
        data += (struct.pack(">H", len(p)) if rb > 250 else bytes([len(p)])) + p
    op = struct.pack(">H", 0x0098) + pm + ct + struct.pack(">4h4hH", 0, 0, h, w, 0, 0, h, w, 0) + data
    if len(op) & 1:
        op += b"\0"
    body += op + struct.pack(">H", 0x00FF)
    return struct.pack(">H4h", 0, 0, 0, h, w) + body


class PictTests(unittest.TestCase):
    def test_packbits_rect(self):
        from rbplatinum import pict
        clut = [(255, 255, 255), (0, 0, 0), (0x33, 0x66, 0xCC)]
        rows = [bytes([(x + y) % 3 for x in range(300)]) for y in range(5)]   # rowBytes > 250
        img = pict.decode(pict8(rows, clut, extra_ops=struct.pack(">H", 0x001E)))
        self.assertEqual((img.w, img.h), (300, 5))
        for y in range(5):
            for x in (0, 1, 2, 299):
                self.assertEqual(img.get(x, y)[:3], clut[(x + y) % 3])
        small = pict.decode(pict8([b"\x01\x02\x00"], clut))                  # rowBytes < 8: unpacked
        self.assertEqual([small.get(x, 0)[:3] for x in range(3)], [clut[1], clut[2], clut[0]])

    def test_mask_regions(self):
        """QuickDraw regions: a rectangle has no spans; scan lines of inversion points XOR
        into the running edges (Monitors' display picture uses one)."""
        from rbplatinum import pict
        box, spans = pict.region_rows(struct.pack(">H4h", 10, 0, 0, 4, 6))
        self.assertEqual((box, spans), ((0, 0, 4, 6), None))
        words = [1, 1, 4, 0x7FFF, 3, 2, 5, 0x7FFF, 4, 1, 2, 4, 5, 0x7FFF, 0x7FFF]
        data = struct.pack(">H4h", 10 + 2 * len(words), 0, 0, 4, 6) + struct.pack(f">{len(words)}H", *words)
        box, spans = pict.region_rows(data)
        self.assertEqual(spans, {1: [(1, 4)], 2: [(1, 4)], 3: [(1, 2), (4, 5)]})

    def test_v1_bitmap_and_refusals(self):
        from rbplatinum import pict
        # version 1: VersionOp 0x11 0x01, BitsRect 0x90 with an 8-pixel 1-bit row, EndPic 0xFF
        bm = struct.pack(">H4h", 2, 0, 0, 1, 8) + struct.pack(">4h4hH", 0, 0, 1, 8, 0, 0, 1, 8, 0) + bytes([0b10100000, 0])
        v1 = struct.pack(">H4h", 0, 0, 0, 1, 8) + b"\x11\x01" + b"\x90" + bm + b"\xff"
        img = pict.decode(v1)
        self.assertEqual([img.get(x, 0)[:3] for x in range(4)], [(0, 0, 0), (255, 255, 255), (0, 0, 0), (255, 255, 255)])
        bad = pict8([b"\x00" * 8], [(0, 0, 0)], extra_ops=struct.pack(">H", 0x0031) + bytes(8))  # PaintRect: drawing
        with self.assertRaises(ValueError):
            pict.decode(bad)
        with self.assertRaises(ValueError):
            pict.decode(b"\0" * 20)


class StartupTests(unittest.TestCase):
    def synthetic_box(self):
        from rbplatinum import startup
        img = Image(startup.BOX_W, startup.BOX_H, (221, 221, 221, 255))
        x0, y0, w, h = startup.PANEL
        img.fill(x0, y0, w, h, (0, 0, 0, 255))
        img.fill(x0 + 1, y0 + 1, w - 2, h - 2, (255, 255, 255, 255))
        img.fill(156, 60, 109, 85, (0x33, 0x66, 0xCC, 255))   # a "logo"
        img.fill(80, 175, 260, 50, (0, 0, 0, 255))            # a "name"
        img.fill(280, 225, 6, 6, (17, 17, 17, 255))           # its descender
        return img

    def test_name_erased(self):
        from rbplatinum import startup
        box = self.synthetic_box()
        self.assertEqual(startup.name_block(box), (175, 230))
        rect = startup.erase_name(box)
        self.assertEqual(rect, (37, 173, 347, 75))
        self.assertEqual(box.get(100, 200)[:3], (255, 255, 255))
        self.assertEqual(box.get(200, 100)[:3], (0x33, 0x66, 0xCC))   # the logo stays
        self.assertEqual(box.get(36, 200)[:3], (0, 0, 0))              # and the panel's outline

    def test_rbis_and_bar_composition(self):
        from rbplatinum import startup

        def solid(w, h, c):
            return Image(w, h, c + (255,))
        cap = Image(startup.CAP_W, startup.BAR[3] - 4)
        for y in range(cap.h):
            for x in range(cap.w):
                cap.set(x, y, (0x70, 0x70, x))
        e = {"BOX ": (0, 0, solid(422, 323, (1, 1, 1))), "WELC": (4, 262, solid(414, 26, (2, 2, 2))),
             "STRT": (4, 262, solid(414, 26, (3, 3, 3))), "BARE": (130, 290, solid(162, 14, (4, 4, 4))),
             "BARF": (130, 290, solid(162, 14, (5, 5, 5))), "BCAP": (0, 0, cap)}
        data = startup.pack([(t, *e[t]) for t in e])
        self.assertEqual(data[:4], b"RBIS")
        version, count = struct.unpack("<HH", data[4:8])
        self.assertEqual((version, count), (1, 6))
        tag, x, y, off, ln = struct.unpack("<4shhII", data[8:24])
        self.assertEqual((tag, x, y), (b"BOX ", 0, 0))
        self.assertEqual(data[off:off + 4], b"RBI1")
        self.assertEqual(struct.unpack("<HH", data[off + 4:off + 8]), (422, 323))
        self.assertEqual(ln, 8 + 422 * 323 * 4)
        for n in (5, 9, 80, 158):
            img = startup.compose(e, "starting", n)
            row = [img.get(130 + x, 295)[:3] for x in range(162)]
            cap0 = 2 + n - 3
            for x in range(2, 160):
                want = (5, 5, 5) if x < cap0 else (0x70, 0x70, x - cap0) if x < cap0 + 6 else (4, 4, 4)
                self.assertEqual(row[x], want, (n, x))
            self.assertEqual(row[0], (4, 4, 4))                      # the frame: always the empty bar's
            self.assertEqual(img.get(130 + 5, 290)[:3], (4, 4, 4))
        self.assertEqual(startup.compose(e, "welcome").get(200, 295)[:3], (1, 1, 1))   # no bar yet

    def test_status_centred_as_mac_os(self):
        from rbplatinum import startup
        font = BitFont("T", 12, "plain", 9, 3, 0)
        for code in range(0x20, 0xD0):
            font.glyphs[code] = Glyph([[16] * 4] * 9, 0, 9, 6)
        box = Image(422, 323, (221, 221, 221, 255))
        welcome = startup.status_strip(box, font, startup.WELCOME, True)
        starting = startup.status_strip(box, font, startup.STARTING, False)
        def first_ink(img):
            return min(x for x in range(img.w) for y in range(img.h) if img.get(x, y)[:3] == (0, 0, 0))
        adv_w, adv_s = 6 * len(startup.WELCOME), 6 * len("Starting Up") + 6
        self.assertEqual(first_ink(welcome) + startup.STATUS[0], (420 - adv_w) // 2)
        self.assertEqual(first_ink(starting) + startup.STATUS[0], (422 - adv_s) // 2)
        bottom = max(y for y in range(welcome.h) for x in range(welcome.w) if welcome.get(x, y)[:3] == (0, 0, 0))
        self.assertEqual(bottom + startup.STATUS[1], startup.STATUS_BASELINE - 1)

    def test_build_from_a_synthetic_system(self):
        from rbplatinum import startup
        box = self.synthetic_box()
        clut, rows = [], []
        index = {}
        for y in range(box.h):
            r = bytearray()
            for x in range(box.w):
                c = box.get(x, y)[:3]
                if c not in index:
                    index[c] = len(clut)
                    clut.append(c)
                r.append(index[c])
            rows.append(bytes(r))
        data = pict8(rows, clut)

        class System:
            def data(self, t, rid):
                return data if (t, rid) == ("PICT", startup.BOX_PICT) else None
        font = BitFont("T", 12, "plain", 9, 3, 0)
        for code in range(0x20, 0xD0):
            font.glyphs[code] = Glyph([[16] * 3] * 9, 0, 9, 5)
        rbis, previews, meta = startup.build(System(), font, Palette(), None)
        self.assertEqual(rbis[:4], b"RBIS")
        self.assertIsNone(meta["wordmark"])
        self.assertEqual(meta["name_rect"], [37, 173, 347, 75])
        self.assertEqual(previews["welcome"].get(100, 200)[:3], (255, 255, 255))   # the name erased
        with self.assertRaises(startup.StartupError):
            startup.build(None, font, Palette(), None)


if __name__ == "__main__":
    unittest.main()
