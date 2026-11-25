"""Build the Platinum asset bundle from the user's own Mac OS 9 disk.

    python3 -m rbplatinum build --source DISK.img|FOLDER --out DIR [--force]

Output (DIR, e.g. /var/lib/retroboot/assets/platinum):
  manifest.json            what is present, metrics, slices, palettes, provenance
  fonts/*.ttf              pixel-square TrueType (COLR v0 for smoothed Charcoal)
  fonts/*.rbf              the same strikes for the classic panel (rbui)
  icons/<name>-32.png, -16.png, icons/<name>-32.rbi   icons and alert icons
  cursors/*.png            I-beam and watch cursors (hot spots in the manifest)
  chrome/*.png             Platinum control sprites (border-image sources)
  desktop.png              the "Mac OS Default" desktop pattern
  startup/installer-startup.rbis   the installer's startup box (startup.py), with
                           startup/welcome.png and starting.png previews
  metal/                   QuickTime Player 6's brushed-metal window (metal.py): pieces,
                           texture, rendered bodies, the Sydnie LCD font, title font

Everything is derived from Apple software on the user's disk: keep it on the
device or in private build output; never commit or redistribute it.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from . import __version__, bitfont, chrome, icons, metal, pict, rbfont, startup, ttfwriter
from .canvas import Palette, LAVENDER, PURPLE
from .image import Image, rbi
from .macdisk import open_disk
from .rsrc import ResourceFile

SYSTEM = "System Folder:System"
SYSRES = "System Folder:System Resources"
FONTS = "System Folder:Fonts"
THEME = "System Folder:Appearance:Theme Files:Apple platinum"
CP = "System Folder:Control Panels"
CSM = "System Folder:Control Strip Modules"

# Web/panel icon name -> source.  ("file", path, file type) uses the file's own BNDL icon;
# ("icns"|"fam"|"cicn", path, id) takes a resource.  A cicn is 32 px only; an optional
# fourth element names the 16 px source: an icon family id in the same file, or
# ("icns", path, id) for an icns's is32 image with its s8mk mask.  "fam16" is a small
# icon family only (16 px).
ICONS = {
    # The Happy Mac; 16 px: the Mac OS face as About This Computer draws it (the icns in
    # System Resources, rbplatinum 5; the System file's icon family differs in 35 pixels).
    "overview": ("cicn", SYSTEM, -20024, ("icns", SYSRES, -16482)),
    "machine": ("file", CP + ":Startup Disk", "APPC"),
    "display": ("file", CP + ":Monitors", "APPC"),
    "sound": ("file", CP + ":Sound", "APPC"),
    "storage": ("icns", SYSRES, -3995),
    "network": ("file", CP + ":TCP/IP", "cdev"),
    "diagnostics": ("file", "System Folder:Apple Menu Items:Apple System Profiler", "APPD"),
    "recovery": ("file", CP + ":Energy Saver", "APPC"),
    "devices": ("file", CP + ":Keychain Access", "APPL"),
    "general": ("file", CP + ":General Controls", "APPC"),
    "appearance": ("file", CP + ":Appearance", "APPC"),
    "memory": ("file", CP + ":Memory", "APPC"),
    # The classic panel's RetroBoot window (rbplatinum 4): Software Update, Date & Time, Printers (7).
    "update": ("file", CP + ":Software Update", "APPC"),
    "datetime": ("file", CP + ":Date & Time", "APPC"),
    "printer": ("file", "System Folder:Extensions:LaserWriter 8", "PRER"),
    "folder": ("icns", SYSRES, -3999),
    "document": ("icns", SYSRES, -4000),
    "disk": ("icns", SYSRES, -3995),
    "cd": ("icns", SYSRES, -3987),
    "trash": ("fam", SYSTEM, -3993),
    "floppy": ("icns", SYSRES, -3998),
    "stop": ("cicn", SYSTEM, 0),
    "note": ("cicn", SYSTEM, 1),
    "caution": ("cicn", SYSTEM, 2),
    # Secured Wi-Fi networks: the Keychain control strip's "Locked Keychain Status" padlock.
    "lock": ("fam16", CSM + ":Keychain Strip", 257),
    # The classic panel's Display & Sound (rbplatinum 6): its Keyboard page.
    "keyboard": ("file", CP + ":Keyboard", "APPC"),
}

# Icons at one other size, as the control panels draw them (rbplatinum 6), for the classic
# panel's Display & Sound laid out like the Monitors control panel:
#   name -> (size, kind, path, id); kind "cicn" or "pict" (a masked picture).
# Monitors' bevel button icons are 40 x 40 cicns drawn at the button's origin; its display
# is a 64 x 64 picture with a region mask; Sound's volume slider ends are 16 px cicns.
SIZED_ICONS = {
    "monitor-page": (40, "cicn", CP + ":Monitors", 1550),    # the sun: Monitors' Monitor button
    "sound-page": (40, "cicn", CP + ":Monitors", 1610),      # a monitor with a speaker
    "display": (64, "pict", CP + ":Monitors", 1585),
    "speaker-low": (16, "cicn", CP + ":Sound", 128),
    "speaker-high": (16, "cicn", CP + ":Sound", 129),
}
# Text colors of smoothed Charcoal (CPAL palettes, in order).  QuickDraw blends smoothed
# text per channel as bg + (((fg - bg) * 17 * level) >> 8) for levels 1..15 and draws
# level 16 (fully covered) in the text color (ttfwriter.mac_blend).  White text at level
# 15 is #fefefe in every channel, which one alpha cannot reach on a colored background,
# so that entry carries its own color.  The
# per-level alphas below make Chromium 140 and WebKit 26 composite each COLR layer onto
# the named background to exactly that value; they were fitted by rendering every
# level/alpha combination in both engines (web/dev/platinum_check.py, platinum/README.md).
# They are RetroBoot's own measurements of browser behaviour, not Apple data.
PALETTE_SPEC = [
    ("black", (0x00, 0x00, 0x00), "dialog face #dddddd",
     [17, 35, 52, 68, 85, 103, 119, 136, 153, 170, 187, 204, 220, 238, 255, 255]),
    ("white-menu", (0xFF, 0xFF, 0xFF), "menu highlight #333399",
     [16, 34, 50, 68, 84, 101, 118, 135, 151, 169, 186, 203, 220, 236, ((0xFE, 0xFE, 0xFE), 255), 255]),
    ("disabled", (0x77, 0x77, 0x77), "#dddddd and #eeeeee",
     [17, 33, 53, 69, 87, 103, 120, 136, 154, 170, 188, 204, 221, 238, 255, 255]),
    ("dim", (0x88, 0x88, 0x88), "#dddddd (disabled menu items)",
     [17, 36, 51, 68, 85, 102, 119, 137, 153, 171, 188, 204, 221, 239, 255, 255]),
    ("white-button", (0xFF, 0xFF, 0xFF), "pressed button #666666",
     [17, 34, 50, 67, 84, 100, 119, 135, 152, 169, 185, 202, 220, 237, ((0xFE, 0xFE, 0xFE), 255), 255]),
    ("black-pane", (0x00, 0x00, 0x00), "tab pane #eeeeee",
     [17, 34, 51, 69, 86, 102, 119, 136, 153, 170, 186, 204, 221, 238, 255, 255]),
    ("black-white", (0x00, 0x00, 0x00), "white lists and fields",
     [17, 34, 51, 68, 85, 102, 119, 136, 153, 170, 187, 204, 221, 238, 255, 255]),
    ("black-title", (0x00, 0x00, 0x00), "active title bar #cccccc",
     [17, 35, 51, 69, 85, 102, 119, 136, 153, 170, 187, 204, 221, 238, 255, 255]),
    ("dim-title", (0x88, 0x88, 0x88), "inactive title bar #eeeeee",
     [17, 34, 51, 70, 85, 102, 119, 137, 153, 170, 187, 205, 222, 238, 255, 255]),
]
TEXT_PALETTES = [(c, alphas) for _n, c, _bg, alphas in PALETTE_SPEC]

CURSORS = {"ibeam": ("CURS", 1), "crosshair": ("CURS", 2), "plus": ("CURS", 3), "watch": ("CURS", 4)}


class Bundle:
    def __init__(self, source: str, out: Path, wordmark_kit: str | None = None):
        self.source = source
        self.out = out
        self.wordmark_kit = wordmark_kit
        self.strikes: dict = {}
        self.disk = open_disk(source)
        self._rf: dict[str, ResourceFile | None] = {}
        self.manifest: dict = {
            "format": 1, "generator": f"rbplatinum {__version__}", "created": int(time.time()),
            "notice": "Derived from Apple software on the user's own Mac OS disk. Private: do not redistribute.",
            "sources": {}, "fonts": {}, "icons": {}, "cursors": {}, "chrome": {}, "palette": {}, "missing": [],
        }

    def rf(self, path: str) -> ResourceFile | None:
        if path not in self._rf:
            try:
                fork = self.disk.read_fork(path, "rsrc")
                self._rf[path] = ResourceFile(fork)
                self.manifest["sources"][path] = hashlib.sha256(fork).hexdigest()
            except (FileNotFoundError, OSError, ValueError):
                self._rf[path] = None
        return self._rf[path]

    def write(self, rel: str, data: bytes) -> None:
        p = self.out / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    # -- palette -----------------------------------------------------------
    def palette(self) -> Palette:
        ramp, name = LAVENDER, "Lavender (built-in)"
        theme = self.rf(THEME)
        if theme is not None:
            r = theme.named("clut", "Lavender")
            if r is not None and not r.compressed:
                ramp = icons.parse_clut(r.data)[:8]
                name = "Lavender"
        self.manifest["palette"] = {
            "variation": name, "ramp": ["#%02x%02x%02x" % c for c in ramp],
            "highlight": "#%02x%02x%02x" % PURPLE,
        }
        return Palette(ramp, PURPLE, extra=chrome.ALERT_COLORS)

    # -- fonts ---------------------------------------------------------------
    def fonts(self) -> None:
        files = [f for f in (self.rf(f"{FONTS}:Geneva"), self.rf(f"{FONTS}:Monaco"), self.rf(f"{FONTS}:Chicago"),
                             self.rf(SYSTEM)) if f is not None]
        strikes = {}
        for fam, size in (("Geneva", 9), ("Geneva", 10), ("Geneva", 12), ("Monaco", 9), ("Monaco", 12), ("Chicago", 12)):
            s = bitfont.find_strike(files, fam, size)
            if s is None:
                self.manifest["missing"].append(f"font {fam} {size}")
                continue
            strikes[f"{fam.lower()}-{size}"] = (s, None)
        if "geneva-10" in strikes:
            strikes["geneva-10-bold"] = (bitfont.embolden(strikes["geneva-10"][0]), None)
        if "geneva-9" in strikes:
            strikes["geneva-9-bold"] = (bitfont.embolden(strikes["geneva-9"][0]), None)
        charcoal = self.rf(f"{FONTS}:Charcoal")
        sf = charcoal.of_type("sfnt") if charcoal else []
        if sf:
            try:
                from .ttraster import TrueTypeFace
                face = TrueTypeFace(sf[0].data)
                for size in (12, 24):
                    mono = face.strike("Charcoal", size, False)
                    smooth = face.strike("Charcoal", size, True)
                    strikes[f"charcoal-{size}"] = (smooth, mono)
                    strikes[f"charcoal-{size}-mono"] = (mono, None)
            except OSError as exc:   # libfreetype missing
                self.manifest["missing"].append(f"font Charcoal ({exc})")
        else:
            self.manifest["missing"].append("font Charcoal")
        self.strikes = {name: s for name, (s, _base) in strikes.items()}
        for name, (s, base) in strikes.items():
            family = "RB " + name.replace("-", " ").title()
            pals = TEXT_PALETTES if s.smooth else None
            self.write(f"fonts/{name}.ttf", ttfwriter.build(s, family, "Bold" if name.endswith("bold") else "Regular",
                                                           base=base, palettes=pals))
            self.write(f"fonts/{name}.rbf", rbfont.encode(s))
            self.manifest["fonts"][name] = {
                "family": family, "size": s.size, "ascent": s.ascent, "descent": s.descent + s.leading,
                "line": s.ascent + s.descent + s.leading, "smooth": s.smooth,
                "palettes": [n for n, *_rest in PALETTE_SPEC] if s.smooth else None,
                "files": [f"fonts/{name}.ttf", f"fonts/{name}.rbf"],
            }

    # -- icons -----------------------------------------------------------------
    def icons(self) -> None:
        for name, spec in ICONS.items():
            kind, path, arg = spec[:3]
            rf = self.rf(path)
            img32 = img16 = None
            if rf is not None:
                try:
                    if kind == "file":
                        img32, img16 = icons.file_icon(rf, arg, 32), icons.file_icon(rf, arg, 16)
                    elif kind == "icns":
                        r = rf.get("icns", arg)
                        if r is not None and not r.compressed:
                            els = icons.icns_elements(r.data)
                            img32, img16 = icons.icns_image(els, "il32"), icons.icns_image(els, "is32")
                    elif kind == "fam":
                        img32, img16 = icons.icon_family(rf, arg, 32), icons.icon_family(rf, arg, 16)
                    elif kind == "fam16":
                        img16 = icons.icon_family(rf, arg, 16)
                    elif kind == "cicn":
                        d = rf.data("cicn", arg)
                        img32 = icons.cicn(d) if d else None
                        img16 = self.small_icon(rf, spec[3]) if len(spec) > 3 else None
                except Exception as exc:  # a damaged resource must not stop the bundle
                    print(f"icon {name}: {exc}", file=sys.stderr)
            if img32 is None and (kind != "fam16" or img16 is None):
                if not name.endswith("-alt"):
                    self.manifest["missing"].append(f"icon {name}")
                continue
            base = name[:-4] if name.endswith("-alt") else name
            if base in self.manifest["icons"]:
                continue
            files = {}
            if img32 is not None:
                self.write(f"icons/{base}-32.png", img32.png())
                self.write(f"icons/{base}-32.rbi", rbi(img32))
                files["32"] = f"icons/{base}-32.png"
            if img16 is not None:
                self.write(f"icons/{base}-16.png", img16.png())
                self.write(f"icons/{base}-16.rbi", rbi(img16))
                files["16"] = f"icons/{base}-16.png"
            self.manifest["icons"][base] = files
        for name, (size, kind, path, rid) in SIZED_ICONS.items():
            rf = self.rf(path)
            img = None
            try:
                d = rf.data("cicn" if kind == "cicn" else "PICT", rid) if rf is not None else None
                if d:
                    img = icons.cicn(d) if kind == "cicn" else pict.decode(d)
            except Exception as exc:  # a damaged resource must not stop the bundle
                print(f"icon {name}-{size}: {exc}", file=sys.stderr)
            if img is None or (img.w, img.h) != (size, size):
                self.manifest["missing"].append(f"icon {name}-{size}")
                continue
            self.write(f"icons/{name}-{size}.png", img.png())
            self.write(f"icons/{name}-{size}.rbi", rbi(img))
            self.manifest["icons"].setdefault(name, {})[str(size)] = f"icons/{name}-{size}.png"
        system = self.rf(SYSTEM)
        for name, (t, rid) in CURSORS.items():
            d = system.data(t, rid) if system else None
            if not d:
                self.manifest["missing"].append(f"cursor {name}")
                continue
            img, hot = icons.curs(d)
            self.write(f"cursors/{name}.png", img.png())
            self.manifest["cursors"][name] = {"file": f"cursors/{name}.png", "hotspot": list(hot)}
        if system is not None:
            r = system.get("ppat", 16)
            if r is not None and not r.compressed:
                pat = icons.ppat(r.data)
                self.write("desktop.png", pat.png())
                self.manifest["desktop"] = {"file": "desktop.png", "size": [pat.w, pat.h], "name": r.name}
            else:
                self.manifest["missing"].append("desktop pattern")

    def small_icon(self, rf: ResourceFile, src) -> Image | None:
        """A cicn's 16 px companion: an icon family id in the same file, or an icns's
        is32 image with its s8mk mask as alpha."""
        if not isinstance(src, tuple):
            return icons.icon_family(rf, src, 16)
        _kind, path, rid = src
        other = self.rf(path)
        r = other.get("icns", rid) if other is not None else None
        if r is None or r.compressed:
            return None
        return icons.icns_image(icons.icns_elements(r.data), "is32")

    # -- chrome ------------------------------------------------------------------
    def chrome(self, pal: Palette) -> None:
        m = self.manifest["chrome"]

        def put(name: str, canvas, **meta):
            img = canvas.image()
            self.write(f"chrome/{name}.png", img.png())
            self.write(f"chrome/{name}.rbi", rbi(img))      # the classic panel draws these (presenter/ui)
            m[name] = {"file": f"chrome/{name}.png", "rbi": f"chrome/{name}.rbi", "size": [img.w, img.h], **meta}

        chrome.draw_theme(pal, put)

    # -- the installer's startup box ---------------------------------------------
    def startup(self, pal: Palette) -> None:
        try:
            kit = startup.Kit(self.wordmark_kit) if self.wordmark_kit else None
            data, previews, meta = startup.build(self.rf(SYSTEM), self.strikes.get("charcoal-12-mono"), pal, kit)
        except (startup.StartupError, ValueError, OSError, KeyError) as exc:
            self.manifest["missing"].append(f"startup box ({exc})")
            return
        self.write("startup/installer-startup.rbis", data)
        for name, img in previews.items():
            self.write(f"startup/{name}.png", img.png())
        self.manifest["startup"] = meta

    # -- QuickTime Player's metal window (metal.py, rbplatinum 5) ------------------
    def metal(self) -> None:
        try:
            metal.build(self, rbi)
        except Exception as exc:  # never let the metal pieces stop the bundle
            self.manifest["missing"].append(f"metal ({type(exc).__name__}: {exc})")

    def run(self) -> dict:
        try:
            pal = self.palette()
            self.fonts()
            self.icons()
            self.chrome(pal)
            self.startup(pal)
            self.metal()
        finally:
            self.disk.close()
        self.write("manifest.json", json.dumps(self.manifest, indent=1, sort_keys=True).encode())
        return self.manifest


def build(source: str, out: str | os.PathLike, force: bool = False, wordmark_kit: str | None = None) -> dict:
    """Build atomically: write to a temporary sibling, then swap it in.  wordmark_kit
    (or $RB_STARTUP_WORDMARK_KIT): the private folder RetroBoot's name in the
    startup box is set from (startup.py)."""
    wordmark_kit = wordmark_kit or os.environ.get("RB_STARTUP_WORDMARK_KIT") or None
    out = Path(out)
    if (out / "manifest.json").exists() and not force:
        return json.loads((out / "manifest.json").read_text())
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".platinum-", dir=out.parent))
    try:
        manifest = Bundle(source, tmp, wordmark_kit).run()
        os.chmod(tmp, 0o755)
        old = None
        if out.exists():
            old = out.with_name(out.name + ".old")
            if old.exists():
                shutil.rmtree(old)
            out.rename(old)
        tmp.rename(out)
        if old is not None:
            shutil.rmtree(old, ignore_errors=True)
        return manifest
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
