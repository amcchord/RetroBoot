"""rbplatinum.metal tests: synthetic fixtures only (no QuickTime Player data).

    python3 -m unittest discover -s platinum/tests -v
"""
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import rbplatinum  # noqa: E402
from rbplatinum import metal  # noqa: E402
from rbplatinum.image import Image, decode_png, encode_png  # noqa: E402
from rbplatinum.rsrc import ResourceFile  # noqa: E402

# Synthetic codec parameters (deliberately not QuickTime Player's).
K = (0.0, 1.0, 50000.5, -10000.5, -0.0, 20000.0, 40.0, 255.0, 4503601774854144.0)
DITHER = (-256, 256, 512, -512)
SWOOP = (0, -1, 4, 2, 8, 7, 12, 11, 30, 50, 30, 10)
COV = bytes(max(0, 255 - 6 * i) for i in range(44))
OCT = bytes(16)                                       # every octant -> step set 0
STEP = bytes([0, 1, 255, 0, 1, 0, 1, 0]) + bytes(56)   # perp +y, back -x, forward +x, +x


def device(gray=0x80, lower=20000, dither=DITHER):
    return metal.Device([[gray * 257] * 128 for _ in range(128)], SWOOP, dither, K, lower, COV, OCT, STEP)


def solid(w, h, rgba):
    img = Image(w, h)
    img.px = [rgba] * (w * h)
    return img


def pidata_literal(b: bytes) -> bytes:
    out = b""
    for i in range(0, len(b), 31):
        chunk = b[i:i + 31]
        out += bytes([(1 << 5) | len(chunk)]) + chunk
    return out


def pef(code: bytes, data_pidata: bytes) -> bytes:
    """A minimal PowerPC PEF container: code section and pattern-initialized data."""
    hdr = b"Joy!peffpwpc" + struct.pack(">IIIII HHI", 1, 0, 0, 0, 0, 2, 2, 0)
    off = 40 + 2 * 28
    secs = struct.pack(">iIIIIIBBBB", -1, 0, len(code), len(code), len(code), off, 0, 1, 4, 0)
    secs += struct.pack(">iIIIIIBBBB", -1, 0, 0, 0, len(data_pidata), off + len(code), 2, 1, 4, 0)
    return hdr + secs + code + data_pidata


def imtb(png_id, entries):
    return b"PNGf" + struct.pack(">HH", png_id, len(entries)) + b"".join(
        struct.pack(">Hhhhh", iid, t, l, t + h, l + w) for iid, (l, t, w, h) in entries)


def vers(s):
    raw = s.encode()
    return bytes([6, 0, 0x80, 0, 0, 0]) + bytes([len(raw)]) + raw + bytes([len(raw)]) + raw


class FixedMath(unittest.TestCase):
    def test_toolbox_math(self):
        self.assertEqual(metal.fix_mul(0x10000, 0x10000), 0x10000)
        self.assertEqual(metal.fix_mul(0x18000, 0x8000), 0xC000)
        self.assertEqual(metal.fix_mul(1, 0x8000), 1)                 # rounded
        self.assertEqual(metal.frac_mul(0x40000000, 12345), 12345)
        self.assertEqual(metal.frac_mul(0x20000000, 3), 2)             # 1.5 rounds up
        self.assertEqual(metal.fix_div(1 << 16, 2 << 16), 0x8000)
        self.assertEqual(metal.fix_div(-1 << 16, 3 << 16), -21845)     # truncated
        self.assertEqual(metal.fix_div(5, 0), 0x7FFFFFFF)

    def test_fused_multiply_add(self):
        a, b = 1 + 2 ** -27, 1 - 2 ** -27
        self.assertEqual(a * b - 1.0, 0.0)                    # two roundings
        self.assertEqual(metal._fma_exact(a, b, -1.0), -2.0 ** -54)
        self.assertEqual(metal.fma(a, b, -1.0), -2.0 ** -54)

    def test_isqrt(self):
        for k in (0, 1, 7, 255, 4096, 46340):
            self.assertEqual(metal.isqrt62(k * k), k << 15)
        self.assertEqual(metal.isqrt62(2), int(2 ** 0.5 * 2 ** 15))

    def test_pidata_and_pef(self):
        packed = bytes([0x03]) + bytes([0x21, 0xAA]) + bytes([0x42, 0x02]) + b"xy"   # zero 3, copy 1, repeat "xy" x3
        self.assertEqual(metal.unpack_pidata(packed), b"\0\0\0\xaa" + b"xy" * 3)
        interleave = bytes([0x62, 1, 2]) + b"ab" + b"1" + b"2"                       # 'ab' common, 1-byte customs x2
        self.assertEqual(metal.unpack_pidata(interleave), b"ab1ab2ab")
        secs = metal.pef_sections(pef(b"\x60\x00\x00\x00" * 4, pidata_literal(b"hello")))
        self.assertEqual(secs["code"], b"\x60\x00\x00\x00" * 4)
        self.assertEqual(secs["data"], b"hello")
        with self.assertRaises(metal.MetalError):
            metal.pef_sections(b"\0" * 80)

    def test_other_builds_are_refused(self):
        fork = pef(bytes(0xC0000), pidata_literal(bytes(0xCB00)))
        with self.assertRaises(metal.MetalError):
            metal.read_constants(fork)
        with self.assertRaises(metal.MetalError):
            metal.read_constants(pef(b"", b""))


class Body(unittest.TestCase):
    def expected_upper(self, dev, W, x, y, x0=0, y0=0):
        """The documented formula for a pixel above the swoop (band origin x0, y0)."""
        h2 = W // 2
        f1 = float(abs(h2 - x0)) or 1.0
        a_cen = int(K[2] + K[4] / h2)
        a_edge = int(K[2] + K[3] * f1 / h2)
        slope = int((a_cen - a_edge) / f1)
        a = a_edge
        sgn = 1
        for i in range(x - x0):
            a += slope * sgn
            if i == h2 - x0 and sgn == 1:
                sgn = -1
        v = (dev.texture[y % 128][x % 128] + a) * 50 // 100 + dev.dither[((y - y0) & 1) * 2 + ((x - x0) & 1)]
        return max(0, min(255, v >> 8))

    def test_upper_metal_formula(self):
        dev = device()
        W, H = 64, 60
        rows = metal.band(dev, W, H)
        for y in (0, 1, 7):
            for x in range(W):
                self.assertEqual(rows[y][x], self.expected_upper(dev, W, x, y), (x, y))
        # brighter at the centre than at the edges: the "V" gradient
        plain = metal.band(device(dither=(0, 0, 0, 0)), W, H)
        self.assertGreater(plain[0][W // 2], plain[0][0])
        self.assertGreater(plain[0][W // 2], plain[0][W - 1])

    def test_band_origin_changes_dither_phase(self):
        dev = device()
        W, H = 64, 60
        whole = metal.band(dev, W, H)
        sub = metal.band(dev, W, H, 2, 3, 10, 4)          # even x0, odd y0
        for y in range(4):
            for x in range(10):
                self.assertEqual(sub[y][x], self.expected_upper(dev, W, 2 + x, 3 + y, 2, 3))
        flipped = sum(1 for y in range(4) for x in range(10) if sub[y][x] != whole[3 + y][2 + x])
        self.assertGreater(flipped, 0)

    def test_swoop_layer(self):
        dev = device()
        W = 96
        layer = metal.swoop_layer(dev, W)
        self.assertEqual(len(layer), W * metal.SWOOP_ROWS)
        mid = W // 2
        col = [layer[r * W + mid] for r in range(metal.SWOOP_ROWS)]
        line = [r for r, v in enumerate(col) if v & 0xFF != 0xFF]
        self.assertTrue(line, "the swoop crosses the middle column")
        first = line[0]
        self.assertTrue(all(v & 0xFF000000 for v in col[:first + 1]))      # upper metal above the line
        self.assertFalse(any(v & 0xFF000000 for v in col[line[-1] + 1:]))  # darker metal below
        darkest = min((v >> 8) & 0xFF for v in col)
        self.assertGreaterEqual(darkest, 176 - 92)
        self.assertEqual(layer[0] & 0xFF000000, 0)                          # corner fix-ups
        # the left four columns are never darker than 160, 120, 80, 40
        for c, floor in enumerate((160, 120, 80, 40)):
            for r in range(metal.SWOOP_ROWS):
                v = layer[r * W + c]
                if v & 0x00FFFFFF != 0x00FFFFFF:
                    self.assertGreaterEqual((v >> 8) & 0xFF, floor)
        rows = metal.band(dev, W, 70)
        self.assertLess(rows[69][mid], rows[20][mid])                       # the lower metal is darker

    def test_window_shape_bevel_and_corners(self):
        dev = device(dither=(0, 0, 0, 0))
        W, H = 80, 60
        corner = solid(12, 22, (255, 0, 0, 0))
        corner.px[0] = (255, 0, 0, 255)
        parts = {n: corner for n in ("corner-tl", "corner-tr", "corner-br", "corner-bl")}
        img = metal.window(dev, parts, W, H)
        body = metal.band(dev, W, H)
        for y, cut in enumerate(metal.CUT):
            self.assertEqual(img.get(cut - 1, y)[3], 0)
            self.assertEqual(img.get(W - cut, H - 1 - y)[3], 0)
            self.assertEqual(img.get(cut, y)[3], 255)
        g = body[0][30]
        w = int(65535.0 * struct.unpack(">f", struct.pack(">f", 0.9))[0])
        self.assertEqual(img.get(30, 0)[0], (255 * w + g * (65535 - w) + 32767) // 65535)
        g = body[H - 1][30]
        w = int(65535.0 * struct.unpack(">f", struct.pack(">f", 0.65))[0])
        self.assertEqual(img.get(30, H - 1)[0], (g * (65535 - w) + 32767) // 65535)
        self.assertEqual(img.get(30, 30)[:3], (body[30][30],) * 3)            # interior untouched
        self.assertNotEqual(img.get(0, 30)[0], body[30][0])                   # side line
        self.assertEqual(img.get(0, 10)[:3], (body[10][0],) * 3)              # no side line beside a corner

    def test_straight_alpha(self):
        img = solid(1, 1, (100, 100, 100, 255))
        metal.over(img, solid(1, 1, (200, 0, 50, 64)), 0, 0)
        self.assertEqual(img.get(0, 0), ((200 * 64 + 100 * 191 + 127) // 255, (100 * 191 + 127) // 255,
                                         (50 * 64 + 100 * 191 + 127) // 255, 255))

    def test_lcd_tiles_and_shine(self):
        dev = device()
        W, H = 120, 80
        img = Image(W, H)
        parts = {"lcd-left": solid(5, 27, (1, 1, 1, 255)), "lcd-fill": solid(40, 27, (2, 2, 2, 255)),
                 "lcd-right": solid(5, 27, (3, 3, 3, 255)), "lcd-shine": solid(20, 5, (4, 4, 4, 255))}
        metal.lcd(img, dev, parts, W, H, 6, 22, 108)
        self.assertEqual(img.get(6, 30)[0], 1)
        self.assertEqual(img.get(11, 30)[0], 2)
        self.assertEqual(img.get(108, 30)[0], 2)            # last tile cropped before the right cap
        self.assertEqual(img.get(109, 30)[0], 3)
        self.assertEqual(img.get(6 + 54 - 10, 22 + 28)[0], 4)
        self.assertEqual(metal.layout("transport", 352, 106), [76, 55, 201, 42])
        self.assertEqual(metal.layout("grow", 352, 106), [333, 86])
        self.assertEqual(metal.layout("lcd", 352, 106), [6, 22, 340, 29])


class Resources(unittest.TestCase):
    def strip(self):
        s = Image(12, 40)
        for y in range(40):
            for x in range(12):
                s.set(x, y, (x, y, 7, 255))
        return encode_png(s)

    def test_image_table_and_pieces(self):
        import rbplatinum.macdisk  # noqa: F401  (rbguestprep on sys.path)
        from rbguestprep.hfsplus import build_resource_fork
        fork = build_resource_fork([("PNGf", 10000, "", self.strip()),
                                    ("imTB", 10000, "", imtb(10000, [(3001, (0, 0, 12, 12)), (3005, (0, 12, 12, 17))]))])
        rf = ResourceFile(fork)
        self.assertEqual(metal.image_table(rf)[3005], (10000, (0, 12, 12, 17)))
        parts = metal.pieces(rf)
        self.assertEqual(sorted(parts), ["close", "grow"])
        self.assertEqual(parts["grow"].get(3, 0), (3, 12, 7, 255))

    def test_ppob_bevel_and_title(self):
        pane = lambda pid, w, h: pid + struct.pack(">hhBB4BiiIi", w, h, 1, 1, 0, 0, 0, 0, 0, 0, 0, -1)  # noqa: E731
        devb = struct.pack(">IBB", 811, 0, 1) + bytes([1, 1, 0, 1]) + struct.pack(">4h8h", 1, 2, 3, 4, 70, 60, 15, 0, 50, 30, 0, 0)
        title = pane(b"Titl", 200, 17) + bytes([5]) + b"Hello" + struct.pack(">hBh3H3Hh", 136, 1, 1, 0xC0C0, 0xC0C0, 0xC0C0,
                                                                              0x4444, 0x4444, 0x4444, 138)
        stream = b"\x00\x02" + b"objd" + struct.pack(">I", 4 + len(devb)) + b"DevB" + devb
        stream += b"objd" + struct.pack(">I", 4 + len(title)) + b"TBrT" + title + b"end."
        bev = metal.bevel_from_ppob(stream)
        self.assertEqual((bev.lines, bev.corners, bev.top, bev.side, bev.bottom),
                         ((True, True, False, True), (1, 2, 3, 4), (70, 60, 25), 15, (50, 30)))
        traits = struct.pack(">hhhh3Hh", 13, 0x40, 1, 1, 0, 0, 0, -1) + bytes([5]) + b"Arial"
        import rbplatinum.macdisk  # noqa: F401
        from rbguestprep.hfsplus import build_resource_fork
        rf = ResourceFile(build_resource_fork([("PPob", 1024, "", stream), ("Txtr", 136, "", traits)]))
        t = metal.title_traits(rf)
        self.assertEqual(t["active"]["font"], "Arial")
        self.assertEqual(t["emboss"], "#c0c0c0")
        self.assertEqual(t["emboss_offset"], [1, 1])
        self.assertIsNone(t["inactive"])
        with self.assertRaises(metal.MetalError):
            metal.bevel_from_ppob(b"\x00\x02end.")

    def test_rgb16(self):
        self.assertEqual(metal.rgb16(bytes.fromhex("efeff7f7dede")), (0xEF, 0xF7, 0xDE))
        self.assertEqual(metal.rgb16(bytes.fromhex("484848484848")), (0x4A, 0x4A, 0x4A))


class BundleMetal(unittest.TestCase):
    def player_disk(self, root: Path, version: str):
        import rbplatinum.macdisk  # noqa: F401
        from rbguestprep.hfsplus import build_resource_fork
        strip = Image(12, 40)
        strip.px = [(200, 200, 200, 255)] * (12 * 40)
        fork = build_resource_fork([("vers", 1, "", vers(version)), ("PNGf", 10000, "", encode_png(strip)),
                                    ("imTB", 10000, "", imtb(10000, [(3001, (0, 0, 12, 12)), (3007, (0, 12, 12, 17))]))])
        d = root / "Applications (Mac OS 9)" / "QuickTime"
        d.mkdir(parents=True)
        (d / "QuickTime Player").write_bytes(b"not a PEF")
        (d / "QuickTime Player.rsrc").write_bytes(fork)

    def test_no_player(self):
        from rbplatinum import bundle
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "disk").mkdir()
            m = bundle.build(str(Path(td) / "disk"), Path(td) / "out")
            self.assertIn("metal (no QuickTime Player)", m["missing"])
            self.assertNotIn("metal", m)
            self.assertEqual(m["generator"], f"rbplatinum {rbplatinum.__version__}")

    def test_other_version(self):
        from rbplatinum import bundle
        with tempfile.TemporaryDirectory() as td:
            self.player_disk(Path(td) / "disk", "7.0")
            m = bundle.build(str(Path(td) / "disk"), Path(td) / "out")
            self.assertIn("metal (QuickTime Player is not version 6.0.3)", m["missing"])
            self.assertNotIn("metal", m)

    def test_pieces_without_the_drawing_code(self):
        from rbplatinum import bundle
        with tempfile.TemporaryDirectory() as td:
            self.player_disk(Path(td) / "disk", "6.0.3")
            out = Path(td) / "out"
            m = bundle.build(str(Path(td) / "disk"), out)
            mm = m["metal"]
            self.assertEqual(sorted(mm["pieces"]), ["close", "logo"])
            rbi = (out / mm["pieces"]["logo"]["rbi"]).read_bytes()
            self.assertEqual(rbi[:4], b"RBI1")
            self.assertEqual(struct.unpack("<HH", rbi[4:8]), (12, 17))
            self.assertEqual(decode_png((out / mm["pieces"]["close"]["file"]).read_bytes()).w, 12)
            self.assertFalse(mm["bodies"])
            self.assertTrue(any(s.startswith("metal body (") for s in m["missing"]))
            self.assertIn("metal piece play", m["missing"])
            self.assertEqual(json.loads((out / "manifest.json").read_text())["metal"]["cut"], list(metal.CUT))


class OverviewIcon(unittest.TestCase):
    def test_small_icon_from_icns_is32_and_s8mk(self):
        import rbplatinum.macdisk  # noqa: F401
        from rbguestprep.hfsplus import build_resource_fork
        from rbplatinum import bundle
        rgb = bytes([10]) * 256 + bytes([20]) * 256 + bytes([30]) * 256          # uncompressed is32
        mask = bytes(range(256))
        els = b"is32" + struct.pack(">I", 8 + len(rgb)) + rgb + b"s8mk" + struct.pack(">I", 8 + len(mask)) + mask
        icns = b"icns" + struct.pack(">I", 8 + len(els)) + els
        with tempfile.TemporaryDirectory() as td:
            sf = Path(td) / "disk" / "System Folder"
            sf.mkdir(parents=True)
            (sf / "System Resources.rsrc").write_bytes(build_resource_fork([("icns", -16482, "", icns)]))
            b = bundle.Bundle(str(Path(td) / "disk"), Path(td) / "out")
            img = b.small_icon(None, ("icns", bundle.SYSRES, -16482))
            self.assertEqual((img.w, img.h), (16, 16))
            self.assertEqual(img.get(3, 2), (10, 20, 30, 35))
            self.assertIsNone(b.small_icon(None, ("icns", bundle.SYSRES, 1)))
            self.assertEqual(bundle.ICONS["overview"][3], ("icns", bundle.SYSRES, -16482))


if __name__ == "__main__":
    unittest.main()
