"""Image conversion for old browsers."""
import asyncio
import io
import os
import signal
import unittest

from PIL import Image

import support  # noqa: F401
from rbwebproxy import images


def encoded(fmt: str, size=(100, 50), mode="RGB", color=(200, 30, 30), **kw) -> bytes:
    img = Image.new(mode, size, color if mode != "RGBA" else color + (128,))
    if mode == "RGB" and size[0] * size[1] > 10_000:
        # some detail so the JPEG is a photo (many colours)
        for x in range(0, size[0], 3):
            for y in range(0, size[1], 3):
                img.putpixel((x, y), ((x * 7) % 256, (y * 5) % 256, (x + y) % 256))
    buf = io.BytesIO()
    img.save(buf, fmt, **kw)
    return buf.getvalue()


def opened(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


class ConvertTests(unittest.TestCase):
    def test_webp_becomes_gif_or_jpeg(self):
        flat = images.convert(encoded("WEBP", lossless=True), 640)
        self.assertEqual(flat.content_type, "image/gif")            # one colour: GIF
        self.assertEqual(opened(flat.body).format, "GIF")
        photo = images.convert(encoded("WEBP", size=(900, 600), quality=80), 640)
        im = opened(photo.body)
        self.assertEqual((im.format, im.width), ("JPEG", 640))
        self.assertFalse(im.info.get("progressive"))
        self.assertTrue(photo.note.startswith("converted:webp"))

    def test_transparency_flattened_on_white(self):
        res = images.convert(encoded("WEBP", mode="RGBA", lossless=True), 640)
        px = opened(res.body).convert("RGB").getpixel((5, 5))
        self.assertTrue(all(c > 100 for c in px))                  # blended with white, not black

    def test_progressive_and_wide_jpeg(self):
        prog = images.convert(encoded("JPEG", size=(300, 200), progressive=True), 640)
        im = opened(prog.body)
        self.assertEqual(im.format, "JPEG")
        self.assertFalse(im.info.get("progressive") or im.info.get("progression"))
        wide = images.convert(encoded("JPEG", size=(1600, 1200)), 640)
        self.assertEqual(opened(wide.body).size, (640, 480))
        small = encoded("JPEG", size=(300, 200))
        self.assertEqual(images.convert(small, 640).body, small)   # baseline and narrow: unchanged

    def test_png_rules(self):
        small = encoded("PNG", size=(64, 64))
        self.assertEqual(images.convert(small, 640).note, "passed")
        big = images.convert(encoded("PNG", size=(1200, 300)), 640)
        self.assertEqual(opened(big.body).width, 640)
        self.assertNotEqual(big.content_type, "image/png")

    def test_gif_passes(self):
        g = encoded("GIF", size=(500, 100))
        self.assertEqual(images.convert(g, 640).body, g)

    def test_bomb_and_garbage(self):
        hdr = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (100000).to_bytes(4, "big") * 2 + b"\x08\x02\x00\x00\x00"
        res = images.convert(hdr + b"\x00" * 64, 640)
        self.assertTrue(res.note.startswith("unsupported"))
        self.assertEqual(res.body, images.BLANK_GIF)
        self.assertEqual(images.convert(b"not an image", 640).note, "passed:unknown")

    def test_sniff(self):
        self.assertEqual(images.sniff(b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg"/>'), "svg")
        self.assertEqual(images.sniff(b"\x00\x00\x00\x1cftypavif"), "avif")
        self.assertEqual(images.sniff(encoded("WEBP")), "webp")


    def test_white_logo_stays_visible(self):
        img = Image.new("RGBA", (200, 60), (0, 0, 0, 0))
        for x in range(20, 180):
            for y in range(20, 40):
                img.putpixel((x, y), (255, 255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        res = images.convert_png_any(buf.getvalue(), 640, 72)
        out = opened(res.body).convert("RGB")
        self.assertNotEqual(out.getpixel((5, 5)), out.getpixel((100, 30)))   # logo differs from background


class SvgTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await images._worker.kill()

    async def test_worker_is_reused_and_replaced_when_hung(self):
        try:
            import cairosvg  # noqa: F401
        except ImportError:
            self.skipTest("CairoSVG not installed")
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40"><circle cx="20" cy="20" r="15"/></svg>'
        await images.rasterize(svg, 640, 72)
        pid = images._worker.proc.pid
        await images.rasterize(svg, 640, 72)
        self.assertEqual(images._worker.proc.pid, pid)                    # one process for many SVGs
        os.kill(pid, signal.SIGSTOP)                                      # a hung renderer
        old = images.SVG_TIMEOUT_S
        images.SVG_TIMEOUT_S = 1.0
        try:
            res = await images.rasterize(svg, 640, 72)
        finally:
            images.SVG_TIMEOUT_S = old
        self.assertTrue(res.note.startswith("unsupported"))
        res = await images.rasterize(svg, 640, 72)                         # a fresh worker takes over
        self.assertTrue(res.note.startswith("converted:svg"))
        self.assertNotEqual(images._worker.proc.pid, pid)

    async def test_svg_rasterized(self):
        try:
            import cairosvg  # noqa: F401
        except ImportError:
            self.skipTest("CairoSVG not installed")
        svg = (b'<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="300">'
               b'<rect width="1200" height="300" fill="#336699"/><text x="10" y="200" font-size="120">Hi</text></svg>')
        res = await images.rasterize(svg, 640, 72)
        im = opened(res.body)
        self.assertEqual(im.width, 640)
        self.assertIn(res.content_type, ("image/gif", "image/jpeg"))

    async def test_svg_external_entities_off(self):
        try:
            import cairosvg  # noqa: F401
        except ImportError:
            self.skipTest("CairoSVG not installed")
        svg = (b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
               b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><text>&x;</text>'
               b'<image href="file:///etc/hostname" width="5" height="5"/></svg>')
        res = await asyncio.wait_for(images.rasterize(svg, 640, 72), 15)
        self.assertIn(res.content_type, ("image/gif", "image/jpeg"))   # rendered or blank, never the file


if __name__ == "__main__":
    unittest.main()
