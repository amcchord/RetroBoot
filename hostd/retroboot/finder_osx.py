"""Narrow OS X Finder-presence references, measured from private native frames.

Only the static left menu titles are hashed. No Apple pixels are stored here.
Unknown versions, localization, appearance or menu contents remain unrecognized.
A match establishes visible Finder menus; it does not establish responsiveness.
"""
from __future__ import annotations

import hashlib

# Exact native build/language, bounds and RGB SHA-256; private captures remain private.
REFERENCES = (
    ("macosx-10.4", "10.4.6", "8I128", "tiger-8I128-en", (48, 2, 396, 20),
     "042da3652e67503da12a7373f7dbe110aa7baf60495218bedae2d6c6d9776ee4"),
    ("macosx-10.4", "10.4.11", "8S165", "tiger-8S165-en", (48, 2, 396, 20),
     "042da3652e67503da12a7373f7dbe110aa7baf60495218bedae2d6c6d9776ee4"),
    ("macosx-10.3", "10.3.9", "7W98", "panther-7W98-en", (48, 2, 396, 20),
     "5de4e536841086f39d1afc4c0d65f05575e25edac7b566f725feefbbaacbb618"),
)


def finder_visible(profile: str, version: str | None, width: int, height: int, rgb: bytes, *, build: str | None = None) -> str | None:
    if width <= 0 or height <= 0 or len(rgb) != width * height * 3:
        return None
    for selected, os_version, os_build, reference, (x0, y0, x1, y1), expected in REFERENCES:
        if profile != selected or version != os_version or build != os_build or width < x1 or height < y1:
            continue
        digest = hashlib.sha256()
        for y in range(y0, y1):
            digest.update(rgb[(y * width + x0) * 3:(y * width + x1) * 3])
        if digest.hexdigest() == expected:
            return reference
    return None
