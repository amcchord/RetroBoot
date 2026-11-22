"""Images for 1999-era browsers.

* WebP, AVIF (when Pillow can read it), BMP, TIFF, ICO and SVG become a
  baseline JPEG (photos) or GIF (few colours: logos, icons, diagrams).
* JPEGs wider than the limit, progressive or CMYK JPEGs are re-encoded as
  baseline JPEGs at the limit.
* PNGs wider than the limit or larger than 200 KB become JPEG or GIF; small
  PNGs pass unchanged (both browsers show them).
* GIFs pass unchanged unless far too wide (animated GIFs always pass).

Transparency is flattened onto white (pages are shown on white).  Pillow's
decompression-bomb limit applies; SVG is rasterised by CairoSVG in a
separate, time- and memory-limited process with external resources off.
"""
from __future__ import annotations

import asyncio
import io
import os
import struct
import sys
from dataclasses import dataclass

from PIL import Image, ImageOps, ImageStat

Image.MAX_IMAGE_PIXELS = 40_000_000
PNG_PASS_BYTES = 200 * 1024
SVG_MAX_BYTES = 2 * 1024 * 1024
SVG_TIMEOUT_S = 8.0
PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# A 1x1 transparent GIF for images that cannot be shown.
BLANK_GIF = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00"
             b",\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;")


@dataclass
class Converted:
    body: bytes
    content_type: str
    note: str            # passed | converted:<from>-><to> | unsupported:<fmt>


def sniff(data: bytes) -> str | None:
    head = data[:64]
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"avif", b"avis"):
            return "avif"
        if brand in (b"heic", b"heix", b"mif1", b"msf1", b"hevc"):
            return "heic"
    if head.startswith(b"BM"):
        return "bmp"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return "tiff"
    if head.startswith(b"\x00\x00\x01\x00"):
        return "ico"
    if head.startswith((b"\xff\x0a", b"\x00\x00\x00\x0cJXL ")):
        return "jxl"
    text = data[:1024].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if text.startswith((b"<?xml", b"<svg", b"<!doctype svg", b"<!--")) and b"<svg" in data[:4096].lower():
        return "svg"
    return None


def _flatten(img: Image.Image) -> Image.Image:
    """Transparency onto the white the pages are shown on, except light artwork on a
    transparent background (white logos made for dark headers), which would vanish:
    that goes onto dark gray."""
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        alpha = rgba.split()[3]
        bg_color = (255, 255, 255)
        hist = alpha.histogram()
        if sum(hist[:16]) > 0.1 * rgba.width * rgba.height:            # really uses transparency
            mask = alpha.point(lambda a: 255 if a > 128 else 0)
            if mask.getbbox():
                stat = ImageStat.Stat(rgba.convert("L"), mask)
                if stat.count[0] and stat.mean[0] > 225:
                    bg_color = (64, 64, 64)
        bg = Image.new("RGB", rgba.size, bg_color)
        bg.paste(rgba, mask=alpha)
        return bg
    if img.mode not in ("RGB", "L"):
        return img.convert("RGB")
    return img


def _shrink(img: Image.Image, max_width: int) -> Image.Image:
    if img.width > max_width:
        h = max(1, round(img.height * max_width / img.width))
        img = img.resize((max_width, h), Image.Resampling.LANCZOS, reducing_gap=2.0)
    return img


def _encode(img: Image.Image, quality: int) -> tuple[bytes, str]:
    """GIF when the image has few colours, else baseline JPEG."""
    buf = io.BytesIO()
    colors = img.getcolors(256) if img.width * img.height <= 1_500_000 else None
    if colors is not None:
        pal = img.convert("RGB").quantize(colors=max(2, len(colors)), method=Image.Quantize.MEDIANCUT,
                                          dither=Image.Dither.NONE)
        pal.save(buf, "GIF", optimize=True)
        return buf.getvalue(), "image/gif"
    img.convert("RGB").save(buf, "JPEG", quality=quality, optimize=False, progressive=False,
                            subsampling=2)
    return buf.getvalue(), "image/jpeg"


def convert(data: bytes, max_width: int = 640, quality: int = 72) -> Converted:
    """Synchronous conversion (run it in a worker thread)."""
    fmt = sniff(data)
    if fmt is None:
        return Converted(data, "", "passed:unknown")
    if fmt == "svg":
        raise ValueError("svg needs rasterize()")
    try:
        img = Image.open(io.BytesIO(data))
    except (Image.UnidentifiedImageError, OSError, Image.DecompressionBombError):
        return Converted(BLANK_GIF, "image/gif", f"unsupported:{fmt}")
    try:
        if fmt == "gif":
            if getattr(img, "is_animated", False) or img.width <= max_width * 3 // 2:
                return Converted(data, "image/gif", "passed")
            img.seek(0)
            out, ct = _encode(_shrink(_flatten(img.convert("RGBA")), max_width), quality)
            return Converted(out, ct, "converted:gif->" + ct.split("/")[1])
        if fmt == "jpeg":
            progressive = bool(img.info.get("progressive") or img.info.get("progression"))
            if img.width <= max_width and not progressive and img.mode in ("RGB", "L"):
                return Converted(data, "image/jpeg", "passed")
            img.draft("RGB", (max_width, max(1, img.height * max_width // max(1, img.width))))
            img = ImageOps.exif_transpose(img) or img
            buf = io.BytesIO()
            _shrink(img.convert("RGB"), max_width).save(buf, "JPEG", quality=quality, progressive=False)
            return Converted(buf.getvalue(), "image/jpeg", "converted:jpeg->jpeg")
        if fmt == "png" and img.width <= max_width and len(data) <= PNG_PASS_BYTES:
            return Converted(data, "image/png", "passed")
        try:
            img.seek(0)
        except EOFError:
            pass
        img.load()
        img = _shrink(_flatten(img), max_width)
        out, ct = _encode(img, quality)
        return Converted(out, ct, f"converted:{fmt}->{ct.split('/')[1]}")
    except (OSError, ValueError, Image.DecompressionBombError, SyntaxError):
        return Converted(BLANK_GIF, "image/gif", f"unsupported:{fmt}")


class SvgWorker:
    """The svgworker child (one at a time): started on demand, killed when a rendering
    overruns SVG_TIMEOUT_S, recycled after RECYCLE renderings."""

    RECYCLE = 200

    def __init__(self):
        self.proc: asyncio.subprocess.Process | None = None
        self.lock: asyncio.Lock | None = None
        self.loop = None
        self.count = 0

    async def _spawn(self) -> None:
        env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/nonexistent", "PYTHONPATH": PKG_PARENT,
               "PYTHONDONTWRITEBYTECODE": "1"}
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "rbwebproxy.svgworker", stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=env)
        self.count = 0

    async def kill(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()
        self.proc = None

    async def render(self, data: bytes, max_width: int) -> bytes | None:
        loop = asyncio.get_running_loop()
        if self.loop is not loop:                 # one worker per event loop (tests run several)
            self.loop, self.lock, self.proc = loop, asyncio.Lock(), None
        async with self.lock:
            if self.proc is None or self.proc.returncode is not None or self.count >= self.RECYCLE:
                await self.kill()
                await self._spawn()
            self.count += 1
            try:
                self.proc.stdin.write(struct.pack(">II", len(data), max_width) + data)
                await self.proc.stdin.drain()
                head = await asyncio.wait_for(self.proc.stdout.readexactly(5), SVG_TIMEOUT_S)
                status, length = struct.unpack(">BI", head)
                body = await asyncio.wait_for(self.proc.stdout.readexactly(length), SVG_TIMEOUT_S)
            except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, OSError):
                await self.kill()                # hung or crashed: the next SVG gets a fresh worker
                return None
            return body if status == 0 else None


_worker = SvgWorker()


async def rasterize(data: bytes, max_width: int, quality: int) -> Converted:
    """SVG -> GIF/JPEG through the CairoSVG worker process."""
    if len(data) > SVG_MAX_BYTES:
        return Converted(BLANK_GIF, "image/gif", "unsupported:svg-too-large")
    try:
        png = await _worker.render(data, max_width)
    except OSError:
        png = None
    if not png:
        return Converted(BLANK_GIF, "image/gif", "unsupported:svg")
    res = await asyncio.to_thread(convert_png_any, png, max_width, quality)
    res.note = "converted:svg->" + res.content_type.split("/")[1]
    return res


def convert_png_any(png: bytes, max_width: int, quality: int) -> Converted:
    try:
        img = Image.open(io.BytesIO(png))
        img.load()
    except (OSError, Image.DecompressionBombError):
        return Converted(BLANK_GIF, "image/gif", "unsupported:png")
    out, ct = _encode(_shrink(_flatten(img), max_width), quality)
    return Converted(out, ct, "converted:png->" + ct.split("/")[1])
