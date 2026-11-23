#!/usr/bin/env python3
"""Generate the web fallback fonts from RetroBoot's original presenter BDFs."""
import argparse
from pathlib import Path
import sys


WEB = Path(__file__).resolve().parents[1]
REPO = WEB.parent
UI = REPO / "presenter/ui"
sys.path[:0] = [str(REPO / "platinum"), str(UI / "tools")]

from fontgen import read_bdf
from rbplatinum import ttfwriter
from rbplatinum.bitfont import BitFont, Glyph


def build(source: Path, license_text: str) -> bytes:
    props, rows = read_bdf(source)
    family = props["FAMILY_NAME"]
    strike = BitFont(family, int(props["POINT_SIZE"]) // 10, "plain",
                     int(props["FONT_ASCENT"]), int(props["FONT_DESCENT"]), 0)
    for code, advance, width, height, left, bottom, bitmap in rows:
        bits = (width + 7) // 8 * 8
        pixels = [[16 * ((row >> (bits - 1 - x)) & 1) for x in range(width)] for row in bitmap]
        strike.glyphs[code] = Glyph(pixels, left, bottom + height, advance)
    strike.missing = strike.glyphs[int(props["DEFAULT_CHAR"])]
    return ttfwriter.build(strike, family, unicode_codepoints=True, notice=props["COPYRIGHT"],
                           license_text=license_text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="check the committed fonts against their sources")
    args = parser.parse_args()
    fonts = UI / "assets/fonts"
    output = WEB / "assets/fonts"
    license_text = (fonts / "LICENSE-fonts.txt").read_text().split("MIT License\n\n", 1)[1].strip()
    output.mkdir(parents=True, exist_ok=True)
    for name in ("RetroBoot-System-12", "RetroBoot-Small-10"):
        target = output / f"{name}.ttf"
        data = build(fonts / f"{name}.bdf", license_text)
        if args.check:
            if not target.is_file() or target.read_bytes() != data:
                parser.exit(1, f"{target} differs from its source\n")
        else:
            target.write_bytes(data)


if __name__ == "__main__":
    main()
