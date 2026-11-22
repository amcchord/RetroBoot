"""SVG rasteriser process: ``python3 -m rbwebproxy.svgworker``.

A long-lived child of the helper, so a page with twenty SVG icons does not
start twenty Pythons.  It renders with CairoSVG with external resources off
(``unsafe=False``: no file or network access, no XML entities) under an
address-space limit; the helper kills and replaces it when one rendering
takes too long, and recycles it after a number of renderings.

Protocol on stdin/stdout, big-endian:
  request  = u32 length, u32 max width, length bytes of SVG
  response = u8 status (0 ok, 1 failed), u32 length, length bytes of PNG
"""
from __future__ import annotations

import io
import resource
import struct
import sys


def render(data: bytes, max_width: int) -> bytes:
    import cairosvg
    from PIL import Image
    png = cairosvg.svg2png(bytestring=data, unsafe=False)
    w = Image.open(io.BytesIO(png)).width
    if w > max_width or w < 16:
        png = cairosvg.svg2png(bytestring=data, unsafe=False,
                               output_width=max_width if w > max_width else min(max_width, 64))
    return png


def main() -> int:
    resource.setrlimit(resource.RLIMIT_AS, (768 << 20, 768 << 20))
    inp, out = sys.stdin.buffer, sys.stdout.buffer
    while True:
        head = inp.read(8)
        if len(head) < 8:
            return 0
        length, width = struct.unpack(">II", head)
        data = inp.read(length)
        try:
            png = render(data, width)
            out.write(struct.pack(">BI", 0, len(png)) + png)
        except Exception as e:                 # malformed SVG, MemoryError...
            msg = type(e).__name__.encode()
            out.write(struct.pack(">BI", 1, len(msg)) + msg)
        out.flush()


if __name__ == "__main__":
    sys.exit(main())
