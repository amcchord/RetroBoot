"""QuickTime Player 6's brushed-metal window, from the user's own copy of the application.

QuickTime Player 6.0.3 for Mac OS 9 (``Applications (Mac OS 9):QuickTime:QuickTime
Player``) draws its player window ("the device") itself; nothing of it comes from
the Appearance Manager.  This module reads the pieces from the application on the
user's disk and redraws the window the way the application does.  Everything it
reads is Apple's and stays in the private bundle; the code here is RetroBoot's.
platinum/reports/qtplayer-metal-2026-10-24.md has the measurements and the proof.

How QuickTime Player draws the window (RetroBoot's description of its behaviour):

* **Body.**  A private image decompressor ('imdc' 'TVOD', registered by the
  application) renders the metal for the window size.  Every gray is
  ``(texture16 + gradient) * 50 / 100`` plus a 2x2 dither, >> 8.  ``texture16``
  is the 16-bit gray of the 128 x 128 texture ``PICT 1024`` (tiled from the window's
  top-left corner).  ``gradient`` is a horizontal "V": 0.6 x 65535 at the edges,
  0.9 x 65535 at the centre, with an integer slope.  The bottom 40 rows carry the
  "swoop": a degree-11 Bezier curve drawn as a 2 px anti-aliased line into a
  W x 40 layer.  Below it the metal is darker (a constant per-row gradient), with
  the texture anchored at the bottom edge.
* **Re-rendering.**  Each control re-renders the body behind itself with its own
  frame as the band origin.  The dither phase and the integer gradient then follow
  the control's origin, so a band can differ from the whole-window body by a few
  levels.
* **Bevel** ('DevB' attachment, PPob 1024).  Four 12 x 22 corner images are
  composited over a re-rendered band.  Then lines are blended in QuickDraw's blend
  mode: white on rows 0-2 at 90/85/25 %, 50 % gray on the side columns at 20 %,
  black on the two bottom rows at 65/40 %.
* **Shape.**  The window region is ``RoundRect(13, 13)``: CUT pixels are
  missing at each corner row.
* **Pieces** (title buttons, LCD caps and fill, grow box, Apple logo, transport
  buttons) are PNG strips (``PNGf``) indexed by ``imTB`` tables.  They are
  composited with straight alpha, ``(s*a + d*(255-a) + 127) // 255``.
"""
from __future__ import annotations

import hashlib
import math
import struct
from dataclasses import dataclass, field

from . import bitfont, pict
from .image import Image, decode_png
from .rsrc import ResourceFile, pstring

PLAYER_PATHS = ("Applications (Mac OS 9):QuickTime:QuickTime Player",
                "Applications:QuickTime:QuickTime Player")
VERSION = "6.0.3"
TEXTURE_PICT = 1024
PLAYER_PPOB = 1024

# Constant blocks of QuickTime Player 6.0.3's PEF fragment (section, offset, length).
# They are read from the user's copy at build time; DIGEST guards the offsets
# (another build of the application leaves the body out).
CONSTANTS = {
    "swoop": ("code", 0xBE1B4, 24),      # 12 x i16: the swoop's control values
    "dither": ("code", 0xBE1DC, 16),     # 4 x i32: 2x2 dither in 1/256 gray levels
    "k": ("code", 0xBE1EC, 72),          # 9 doubles used by DrawBand
    "lower": ("code", 0x24098, 4),       # 'li r9,N': the darker metal's base gradient
    "cov": ("data", 0xCA7C, 44),         # line coverage by distance (1/16 px steps)
    "oct": ("data", 0xCAA8, 16),         # octant -> step set
    "step": ("data", 0xCAB8, 64),        # 8 step sets of 4 (dx, dy) pixel steps
    "lcd_fill": ("code", 0xBE4F8, 6),    # RGBColor
    "lcd_shadow": ("code", 0xBE500, 6),
    "lcd_head": ("code", 0xBE508, 6),
    "lcd_ink": ("code", 0xBE518, 6),
}
DIGEST = "eb76bcafd30bd8b7d5e65f7b2e0cb287b6293e6d9782f05a1566b91f219c45dd"

# Bundle name -> imTB image id.  The 'DevB' corner ids come from PPob 1024; the
# inactive corners are those + 5.  Title boxes: normal, pressed, disabled.
PIECES = {
    "corner-tl": 3470, "corner-tr": 3471, "corner-br": 3473, "corner-bl": 3472,
    "corner-tl-inactive": 3475, "corner-tr-inactive": 3476, "corner-br-inactive": 3478, "corner-bl-inactive": 3477,
    "close": 3001, "close-pressed": 3002, "close-disabled": 3004,
    "zoom": 3020, "zoom-pressed": 3021, "zoom-disabled": 3023,
    "collapse": 3010, "collapse-pressed": 3011, "collapse-disabled": 3013,
    "lcd-left": 2101, "lcd-fill": 2100, "lcd-right": 2102, "lcd-shine": 2260,
    "grow": 3005, "logo": 3007,
    "back": 2214, "back-pressed": 2213, "back-disabled": 2221,
    "rewind": 2215, "rewind-pressed": 2217, "rewind-disabled": 2223,
    "play": 2200, "play-pressed": 2202, "play-disabled": 2205,
    "forward": 2218, "forward-pressed": 2220, "forward-disabled": 2219,
    "end": 2240, "end-pressed": 2241, "end-disabled": 2242,
    "quicktime": 2230, "quicktime-pressed": 2231, "quicktime-disabled": 2233,
    "speaker": 3483, "volume-track": 3450, "volume-thumb": 3451,
}

# QuickDraw's RoundRect(13, 13) region: pixels missing at each end of rows 0..3
# from the top and bottom edges (measured on Mac OS 9.2.2).
CUT = (4, 2, 1, 1)
BEVEL_GRAY = (0x7F, 0x7F, 0x7F)      # RGBColor 0x7FFF on a 32-bit port
SWOOP_ROWS = 40
SWOOP_STEPS = 128
LCD_HEIGHT = 29
LCD_CAP = 5

# Window geometry from PPob 1024 (the 357 px layout with a 250 px movie); negative x
# is from the right edge, negative y from the bottom.  An empty player is 352 x 106.
LAYOUT = {
    "title": [37, 2, -74, 17],          # x, y, width (W - 74), height
    "close": [6, 6], "zoom": [-35, 6], "collapse": [-19, 6],
    "lcd": [6, 22, -12, LCD_HEIGHT],    # x, y, width (W - 12), height
    "logo": [5, -20], "grow": [-19, -20],
    "quicktime": [-55, -52],
    "transport": [-201, -51, 201, 42],  # centred: x = W // 2 - 201 // 2
    "speaker": [7, -49], "volume": [23, -48],
}


class MetalError(Exception):
    pass


def s32(v: int) -> int:
    v &= 0xFFFFFFFF
    return v - (1 << 32) if v & 0x80000000 else v


def _fma_exact(a: float, b: float, c: float) -> float:
    """a * b + c rounded once (PowerPC fmadd), for Pythons before 3.13."""
    from fractions import Fraction
    return float(Fraction(a) * Fraction(b) + Fraction(c))


fma = getattr(math, "fma", _fma_exact)


def _tdiv(n: int, d: int) -> int:
    """C-style integer division (toward zero)."""
    q = abs(n) // abs(d)
    return q if (n >= 0) == (d > 0) else -q


def fix_mul(a: int, b: int) -> int:
    """Toolbox FixMul: 16.16 product, rounded."""
    return s32((a * b + 0x8000) >> 16)


def frac_mul(a: int, b: int) -> int:
    """Toolbox FracMul: 2.30 product, rounded."""
    return s32((a * b + 0x20000000) >> 30)


def fix_div(a: int, b: int) -> int:
    """Toolbox FixDiv as the swoop needs it: 16.16 quotient, truncated."""
    if b == 0:
        return 0x7FFFFFFF if a >= 0 else -0x80000000
    return s32(_tdiv(a << 16, b))


def isqrt62(v: int) -> int:
    """The codec's square root: 31 restoring steps on v << 30 (sqrt(v) * 2**15)."""
    v &= 0xFFFFFFFF
    root = rem = 0
    for _ in range(31):
        root = (root << 1) & 0xFFFFFFFF
        trial = (root << 1) + 1
        rem = ((rem << 2) | (v >> 30)) & 0xFFFFFFFF
        v = (v << 2) & 0xFFFFFFFF
        if rem >= trial:
            rem -= trial
            root += 1
    return s32(root)


# -- reading the application ---------------------------------------------------
def pef_sections(fork: bytes) -> dict[str, bytes]:
    """The code and (unpacked) data sections of a PowerPC PEF container."""
    if fork[:12] != b"Joy!peffpwpc":
        raise MetalError("not a PowerPC PEF fragment")
    nsec = struct.unpack(">H", fork[32:34])[0]
    out: dict[str, bytes] = {}
    for i in range(nsec):
        (_name, _addr, _total, _unpacked, packed, off, kind, _share, _align,
         _r) = struct.unpack(">iIIIIIBBBB", fork[40 + 28 * i:68 + 28 * i])
        raw = fork[off:off + packed]
        if kind == 0 and "code" not in out:
            out["code"] = raw
        elif kind == 1 and "data" not in out:
            out["data"] = raw
        elif kind == 2 and "data" not in out:
            out["data"] = unpack_pidata(raw)
    return out


def _arg(d: bytes, p: int) -> tuple[int, int]:
    v = 0
    while True:
        b = d[p]
        p += 1
        v = (v << 7) | (b & 0x7F)
        if not b & 0x80:
            return v, p


def unpack_pidata(d: bytes) -> bytes:
    """PEF pattern-initialized data (Mac OS Runtime Architectures, 8-21)."""
    out = bytearray()
    p = 0
    while p < len(d):
        b = d[p]
        p += 1
        op, cnt = b >> 5, b & 31
        if cnt == 0:
            cnt, p = _arg(d, p)
        if op == 0:
            out += bytes(cnt)
        elif op == 1:
            out += d[p:p + cnt]
            p += cnt
        elif op == 2:
            rep, p = _arg(d, p)
            out += d[p:p + cnt] * (rep + 1)
            p += cnt
        elif op in (3, 4):
            ccnt, p = _arg(d, p)
            rep, p = _arg(d, p)
            common = b""
            if op == 3:
                common = d[p:p + cnt]
                p += cnt
            else:
                common = bytes(cnt)
            for _ in range(rep):
                out += common + d[p:p + ccnt]
                p += ccnt
            out += common
        else:
            raise MetalError(f"pidata opcode {op}")
    return bytes(out)


def read_constants(data_fork: bytes) -> dict[str, bytes]:
    secs = pef_sections(data_fork)
    blocks = {}
    for name, (sec, off, n) in CONSTANTS.items():
        b = secs.get(sec, b"")[off:off + n]
        if len(b) != n:
            raise MetalError(f"QuickTime Player {sec} section too short")
        blocks[name] = b
    h = hashlib.sha256(b"".join(blocks[k] for k in CONSTANTS)).hexdigest()
    if h != DIGEST:
        raise MetalError("QuickTime Player's drawing code is not version " + VERSION)
    return blocks


def image_table(rf: ResourceFile) -> dict[int, tuple[int, tuple[int, int, int, int]]]:
    """imTB tables: image id -> (PNGf id, (left, top, width, height))."""
    out = {}
    for r in rf.of_type("imTB"):
        d = r.data
        if d[:4] != b"PNGf" or len(d) < 8:
            continue
        pid, n = struct.unpack(">HH", d[4:8])
        for i in range(n):
            if 18 + 10 * i > len(d):
                break
            iid, t, l, b, rr = struct.unpack(">Hhhhh", d[8 + 10 * i:18 + 10 * i])
            out[iid] = (pid, (l, t, rr - l, b - t))
    return out


def pieces(rf: ResourceFile, names=None) -> dict[str, Image]:
    table = image_table(rf)
    strips: dict[int, Image] = {}
    out = {}
    for name, iid in PIECES.items():
        if names is not None and name not in names:
            continue
        if iid not in table:
            continue
        pid, (l, t, w, h) = table[iid]
        if pid not in strips:
            data = rf.data("PNGf", pid)
            if not data:
                continue
            strips[pid] = decode_png(data)
        out[name] = strips[pid].crop(l, t, w, h)
    return out


def ppob_objects(data: bytes) -> list[tuple[int, bytes | None, bytes, bytes]]:
    """PowerPlant view stream: (depth, class from 'dopl', object type, object data)."""
    pos, depth, cls, out = 2, 0, None, []
    while pos + 4 <= len(data):
        tag = data[pos:pos + 4]
        pos += 4
        if tag == b"dopl":
            cls = data[pos:pos + 4]
            pos += 4
        elif tag == b"objd":
            n = struct.unpack(">I", data[pos:pos + 4])[0]
            out.append((depth, cls, data[pos + 4:pos + 8], data[pos + 8:pos + 4 + n]))
            pos += 4 + n
            cls = None
        elif tag == b"begs":
            depth += 1
        elif tag == b"ends":
            depth -= 1
        else:
            break
    return out


@dataclass
class Bevel:
    """The 'DevB' attachment of PPob 1024 (the stream fields; 25 % and 0 are constants)."""
    lines: tuple[bool, bool, bool, bool] = (True, True, True, True)   # left, top, right, bottom
    corners: tuple[int, int, int, int] = (3470, 3471, 3473, 3472)      # tl, tr, br, bl
    top: tuple[int, int, int] = (90, 85, 25)
    side: int = 20
    bottom: tuple[int, int] = (65, 40)


def bevel_from_ppob(data: bytes) -> Bevel:
    for _depth, _cls, typ, body in ppob_objects(data):
        if typ == b"DevB" and len(body) >= 34:
            # LAttachment: message (4), execute host, owner host; then the bevel's fields.
            flags = tuple(bool(b) for b in body[6:10])
            ids = struct.unpack(">4h", body[10:18])
            v = struct.unpack(">8h", body[18:34])   # top0, top1, side, (unused), bottom0, bottom1, (unused), extra
            return Bevel(flags, ids, (v[0], v[1], 25), v[2], (v[4], v[5]))
    raise MetalError("PPob 1024 has no 'DevB' bevel")


def title_traits(rf: ResourceFile) -> dict:
    """The title bar ('TBrT' in PPob 1024): its TextTraits and emboss colours."""
    def traits(rid):
        d = rf.data("Txtr", rid)
        if not d or len(d) < 17:
            return None
        size, style, just, mode = struct.unpack(">hhhh", d[:8])
        r, g, b = struct.unpack(">HHH", d[8:14])
        return {"size": size, "style": style, "just": just, "mode": mode, "color": "#%02x%02x%02x" % (r >> 8, g >> 8, b >> 8),
                "font": pstring(d, 16) if struct.unpack(">h", d[14:16])[0] == -1 else struct.unpack(">h", d[14:16])[0]}
    data = rf.data("PPob", PLAYER_PPOB) or b""
    for _depth, cls, typ, body in ppob_objects(data):
        if b"TBrT" in (cls, typ) and len(body) > 30 and len(body) >= 31 + body[30] + 19:
            p = 30 + 1 + body[30]                    # SPaneInfo (30 bytes), then the title string
            active, flag, offset = struct.unpack(">hBh", body[p:p + 5])
            c1 = struct.unpack(">HHH", body[p + 5:p + 11])
            c2 = struct.unpack(">HHH", body[p + 11:p + 17])
            inactive = struct.unpack(">h", body[p + 17:p + 19])[0]
            return {"active": traits(active), "inactive": traits(inactive), "emboss": "#%02x%02x%02x" % tuple(c >> 8 for c in c1),
                    "emboss_alt": "#%02x%02x%02x" % tuple(c >> 8 for c in c2),
                    "emboss_offset": [offset, offset] if flag else [-offset, -offset]}
    return {}


def rgb16(b: bytes) -> tuple[int, int, int]:
    """An RGBColor as a 16-bit (thousands) offscreen stores it, read back at 8 bits."""
    out = []
    for i in range(3):
        v = b[2 * i] >> 3
        out.append((v << 3) | (v >> 2))
    return tuple(out)


@dataclass
class Device:
    """Everything the body renderer needs, all read from the user's QuickTime Player."""
    texture: list[list[int]]                 # 128 rows of 128 16-bit grays (G << 8 | B)
    swoop: tuple[int, ...]                   # 12 control values
    dither: tuple[int, int, int, int]
    k: tuple[float, ...]
    lower: int
    cov: bytes
    oct: bytes
    step: bytes
    _swoops: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_blocks(cls, texture_img: Image, b: dict[str, bytes]) -> "Device":
        if (texture_img.w, texture_img.h) != (128, 128):
            raise MetalError("texture PICT 1024 is not 128 x 128")
        tex = [[(c[1] << 8) | c[2] for c in texture_img.px[y * 128:(y + 1) * 128]] for y in range(128)]
        word = struct.unpack(">I", b["lower"])[0]
        if word >> 16 != 0x3920:                      # li r9, N
            raise MetalError("unexpected DrawBand instruction")
        return cls(tex, struct.unpack(">12h", b["swoop"]), struct.unpack(">4i", b["dither"]),
                   struct.unpack(">9d", b["k"]), s32(word << 16) >> 16, b["cov"], b["oct"], b["step"])

    def swoop_layer(self, W: int) -> list[int]:
        if W not in self._swoops:
            self._swoops[W] = swoop_layer(self, W)
        return self._swoops[W]


# -- the swoop ------------------------------------------------------------------
def swoop_points(dev: Device, W: int) -> list[tuple[int, int]]:
    s = dev.swoop
    h = W // 2
    pts = [(s[1], s[0]), (s[1] + s[3], s[0] + s[2]), (s[1] + s[5], s[0] + s[4]), (s[1] + s[7], s[0] + s[6]),
           (s[1] + s[9], s[8]), (h - s[11], s[10]), (h + s[11], s[10]), (W - s[9], s[8]),
           (W - s[7], s[0] + s[6]), (W - s[5], s[0] + s[4]), (W - s[3], s[0] + s[2]), (W - s[1] - 1, s[0])]
    return [(x << 16, y << 16) for x, y in pts]


def _bezier(vals: list[int], t: int) -> int:
    lvl = list(vals)
    while len(lvl) > 1:
        lvl = [s32(lvl[i] + frac_mul(t, s32(lvl[i + 1] - lvl[i]))) for i in range(len(lvl) - 1)]
    return lvl[0]


def _plot(px: list[int], p: int, cov: int, perp: int) -> None:
    old = px[p]
    shade = s32(cov * -92)
    shade = (s32(shade + (shade >> 8)) + 128 >> 8) + 176
    if shade >= (old >> 8) & 0xFF:
        return
    px[p] = (0 if perp > 0 else 0xFF000000) | (shade << 8) | (cov ^ 255)


def _line(dev: Device, px: list[int], rw: int, x0: int, y0: int, x1: int, y1: int,
          width: int = 0x20000, mode: int = 2) -> None:
    """An anti-aliased wide line in 16.16 coordinates (the codec's swoop pen)."""
    cov_t = dev.cov
    dy, dx = s32(y1 - y0), s32(x1 - x0)
    length = isqrt62(s32(frac_mul(dy, dy) + frac_mul(dx, dx)))
    if length <= 0:
        return
    ux, uy = fix_div(dx, length), fix_div(dy, length)
    nuy = s32(-uy)
    a = s32(nuy ^ ux)
    a = s32((a >> 1) - (a & nuy))
    b = s32(uy ^ ux)
    b = s32((b >> 1) - (b & uy))
    octant = (2 if uy < 0 else 0) | (1 if ux < 0 else 0) | (4 if b < 0 else 0) | (8 if a < 0 else 0)
    ent = 8 * dev.oct[octant]

    def sb(v):
        return v - 256 if v > 127 else v
    steps = []
    for i in range(4):
        b0, b1 = sb(dev.step[ent + 2 * i]), sb(dev.step[ent + 2 * i + 1])
        dp = da = off = 0
        if b0 < 0:
            dp, da, off = uy, -ux, -1
        elif b0 > 0:
            dp, da, off = nuy, ux, 1
        if b1 < 0:
            dp, da, off = dp - ux, da - uy, off - rw
        elif b1 > 0:
            dp, da, off = dp + ux, da + uy, off + rw
        steps.append((off, s32(dp), s32(da)))
    hw = width - 0x10000
    hw = hw >> 1 if hw >= 0 else 0
    outer = hw + 0x2A000
    if mode == 1:
        back = outer + 0x8000
    elif mode == 2:
        back = outer - (outer >> 2)
    else:
        back = outer + (outer >> 1)
    b2, b3 = sb(dev.step[ent + 2]), sb(dev.step[ent + 3])
    sx = x0 + (back if b2 > 0 else -back if b2 < 0 else 0)
    sy = y0 + (back if b3 > 0 else -back if b3 < 0 else 0)
    sx, sy = (sx + 0x8000) & ~0xFFFF, (sy + 0x8000) & ~0xFFFF
    perp = s32(fix_mul(nuy, sx - x0) + fix_mul(ux, sy - y0))
    along = s32(fix_mul(ux, sx - x0) + fix_mul(uy, sy - y0))
    ptr = (sx >> 16) + (sy >> 16) * rw
    s0 = steps[0]
    if perp < 0:
        while perp < 0:
            perp, ptr, along = perp + s0[1], ptr + s0[0], along + s0[2]
    elif perp > 0:
        while perp > 0:
            perp, ptr, along = perp - s0[1], ptr - s0[0], along - s0[2]
    n = len(px)
    state = {"flag": 0}

    def visit(p, pe, al, cov):
        go = False
        r28 = -al
        if r28 > 0:
            state["flag"] = 1
            go = True
        else:
            r28 = al - length
            go = r28 > 0
        if go:
            if mode == 1:
                r28 = isqrt62(fix_mul(r28, r28) + fix_mul(pe, pe)) >> 7
                if r28 <= hw:
                    cov = 255
                elif r28 >= outer:
                    return
                else:
                    cov = cov_t[(r28 - hw) >> 12]
            elif mode == 0:
                if r28 > outer:
                    return
                if r28 > hw:
                    t = cov * cov_t[(r28 - hw) >> 12]
                    cov = (t + (t >> 8) + 128 >> 8) & 0xFF
            elif mode == 2:
                if (r28 & 0xFFFFFFFF) > 0x2A000:
                    return
                t = cov_t[r28 >> 12] * cov
                cov = (t + (t >> 8) + 128 >> 8) & 0xFF
        state["flag"] = 1
        if 0 <= p < n:
            _plot(px, p, cov, pe)

    while True:
        state["flag"] = 0
        sptr, salong, sperp = ptr, along, perp
        if perp < 0:
            if -perp <= hw:
                visit(ptr, perp, along, 255)
            elif -perp <= outer:
                visit(ptr, perp, along, cov_t[-(perp + hw) >> 12])
            ptr, perp, along = ptr + s0[0], perp + s0[1], along + s0[2]
        while perp <= outer:
            if perp <= hw:
                visit(ptr, perp, along, 255)
            else:
                visit(ptr, perp, along, cov_t[(perp - hw) >> 12])
            ptr, perp, along = ptr + s0[0], perp + s0[1], along + s0[2]
        ptr, perp, along = sptr - s0[0], sperp - s0[1], salong - s0[2]
        while -perp <= outer:
            if -perp <= hw:
                visit(ptr, perp, along, 255)
            else:
                visit(ptr, perp, along, cov_t[-(perp + hw) >> 12])
            ptr, perp, along = ptr - s0[0], perp - s0[1], along - s0[2]
        off, dp, da = steps[3 if sperp < 0 else 2]
        ptr, perp, along = sptr + off, sperp + dp, salong + da
        if not state["flag"]:
            return


def swoop_layer(dev: Device, W: int) -> list[int]:
    """The W x 40 swoop layer, one 32-bit word per pixel: flag (0xFF000000 = upper metal),
    shade << 8, metal weight (255 = all metal)."""
    rw = W
    px = [0x00FFFFFF] * (rw * SWOOP_ROWS)
    pts = swoop_points(dev, W)
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    step = 0x40000000 // SWOOP_STEPS
    t, (lx, ly) = step, pts[0]
    for _ in range(SWOOP_STEPS - 1):
        x, y = _bezier(xs, t), _bezier(ys, t)
        _line(dev, px, rw, lx, ly, x, y)
        lx, ly, t = x, y, t + step
    _line(dev, px, rw, lx, ly, xs[-1], ys[-1])
    for cols in ((0, 1, 2, 3), (W - 1, W - 2, W - 3, W - 4)):      # lighter shade at the ends
        floor = 160
        for i, c in enumerate(cols):
            for r in range(SWOOP_ROWS):
                p = r * rw + c
                v = px[p]
                if r == 0 and i == 0:
                    v |= 0xFF000000
                if v != 0x00FFFFFF:
                    g = max((v >> 8) & 0xFF, floor)
                    px[p] = (v & 0xFF0000FF) | (g << 8)
            floor = max(0, (160 if i == 0 else floor) - 40)
    for c in range(W):                                             # upper metal down to the line
        for r in range(SWOOP_ROWS):
            p = r * rw + c
            v = px[p]
            px[p] = v | 0xFF000000
            if v != 0x00FFFFFF:
                break
    for p in (0, rw, W - 1, rw + W - 1):
        px[p] &= 0x00FFFFFF
    return px


# -- the body -------------------------------------------------------------------
def band(dev: Device, W: int, H: int, x0: int = 0, y0: int = 0, bw: int | None = None,
         bh: int | None = None, swoop: bool = True) -> list[list[int]]:
    """Grays of the W x H body for the band (x0, y0, bw, bh), as QuickTime Player's
    codec renders it with that band origin (DrawBand, 32-bit destination)."""
    bw = W - x0 if bw is None else bw
    bh = H - y0 if bh is None else bh
    k = dev.k
    tex = dev.texture
    h2 = W // 2
    f0 = float(h2)
    center = h2 - x0
    row_a = y0 % 128
    row_b = (y0 + 128 - H % 128) % 128
    layer = dev.swoop_layer(W) if swoop else None
    d00, d01, d10, d11 = dev.dither
    dith = ((d00, d01), (d10, d11))
    out = []
    for y in range(bh):
        dist = H - (y0 + y)
        srow = SWOOP_ROWS - dist if layer is not None and dist < SWOOP_ROWS else None
        past = False
        f1 = float(h2 - x0)
        if f1 < 0.0:
            f1, past = float(x0 - h2), True
        if f1 == 0.0:
            f1 = 1.0
        a_cen = int(k[2] + k[4] / f0)
        a_edge = int(k[2] + (k[3] * f1) / f0)
        slope = int(float(a_cen - a_edge) / f1)
        lower = int(float(dev.lower) - (k[5] * float(SWOOP_ROWS - dist)) / k[6])
        a = a_edge
        if past:
            a, slope = a + slope, -slope
        col = x0 % 128
        ta, tb = tex[(row_a + y) % 128], tex[(row_b + y) % 128]
        dy = dith[y & 1]
        row = []
        flipped = past
        base = None if srow is None else srow * W + x0
        for x in range(bw):
            if base is not None:
                s = layer[base + x]
                v = _tdiv(((ta[col] if s & 0xFF000000 else tb[col]) + (a if s & 0xFF000000 else lower)) * 50, 100)
                wgt = s & 0xFF
                if wgt != 255:
                    f5 = float(wgt) / k[7]
                    v = int(fma(k[1] - f5, float(((s >> 8) & 0xFF) * 257), f5 * float(v)))
            else:
                v = _tdiv((ta[col] + a) * 50, 100)
            g = (v + dy[x & 1]) >> 8
            row.append(0 if g < 0 else 255 if g > 255 else g)
            col = col + 1 if col < 127 else 0
            a += slope
            if x == center and not flipped:
                slope, flipped = -slope, True
        out.append(row)
    return out


def put_band(img: Image, dev: Device, W: int, H: int, x0: int, y0: int, bw: int, bh: int) -> None:
    """Re-render the body behind a control (band origin = the control's frame)."""
    for y, row in enumerate(band(dev, W, H, x0, y0, bw, bh)):
        base = (y0 + y) * img.w + x0
        for x, g in enumerate(row):
            img.px[base + x] = (g, g, g, 255)


def over(img: Image, piece: Image, x: int, y: int, src: tuple[int, int, int, int] | None = None) -> None:
    """Straight-alpha composite (QuickTime's graphicsModeStraightAlpha on 32-bit)."""
    sx, sy, w, h = src or (0, 0, piece.w, piece.h)
    for j in range(h):
        yy = y + j
        if not 0 <= yy < img.h:
            continue
        for i in range(w):
            xx = x + i
            if not 0 <= xx < img.w:
                continue
            r, g, b, a = piece.px[(sy + j) * piece.w + sx + i]
            if a == 0:
                continue
            d = img.px[yy * img.w + xx]
            if a == 255:
                img.px[yy * img.w + xx] = (r, g, b, d[3])
            else:
                img.px[yy * img.w + xx] = ((r * a + d[0] * (255 - a) + 127) // 255, (g * a + d[1] * (255 - a) + 127) // 255,
                                           (b * a + d[2] * (255 - a) + 127) // 255, d[3])


def blend_pixel(d: tuple, c: tuple, percent: int) -> tuple:
    """QuickDraw blend mode with OpColor gray int(65535 * float32(percent / 100))."""
    f32 = struct.unpack(">f", struct.pack(">f", percent / 100.0))[0]
    w = int(65535.0 * f32)
    return tuple((s * w + v * (65535 - w) + 32767) // 65535 for s, v in zip(c, d[:3])) + (d[3],)


def hline(img: Image, x0: int, x1: int, y: int, color, percent: int) -> None:
    for x in range(min(x0, x1), max(x0, x1) + 1):
        img.px[y * img.w + x] = blend_pixel(img.px[y * img.w + x], color, percent)


def vline(img: Image, x: int, y0: int, y1: int, color, percent: int) -> None:
    for y in range(min(y0, y1), max(y0, y1) + 1):
        img.px[y * img.w + x] = blend_pixel(img.px[y * img.w + x], color, percent)


def inside(W: int, H: int, x: int, y: int) -> bool:
    for i, c in enumerate(CUT):
        if (y == i or y == H - 1 - i) and (x < c or x >= W - c):
            return False
    return True


def window(dev: Device, parts: dict[str, Image], W: int, H: int, bevel: Bevel | None = None,
           active: bool = True) -> Image:
    """The empty metal window: body, corners, bevel lines and the RoundRect(13, 13)
    shape (pixels outside it are transparent)."""
    bevel = bevel or Bevel()
    img = Image(W, H)
    put_band(img, dev, W, H, 0, 0, W, H)
    suffix = "" if active else "-inactive"
    for name, (cx, cy) in (("corner-tl", (0, 0)), ("corner-tr", (W - 12, 0)), ("corner-br", (W - 12, H - 22)),
                           ("corner-bl", (0, H - 22))):
        piece = parts.get(name + suffix) or parts.get(name)
        if piece is None:
            continue
        cx = cx if name.endswith("l") else W - piece.w
        cy = cy if name[7] == "t" else H - piece.h
        put_band(img, dev, W, H, cx, cy, piece.w, piece.h)
        over(img, piece, cx, cy)
    cw, ch = (parts["corner-tl"].w, parts["corner-tl"].h) if "corner-tl" in parts else (12, 22)
    left, top, right, bottom = bevel.lines
    if left:
        vline(img, 0, H - 1 - ch, ch, BEVEL_GRAY, bevel.side)
    if right:
        vline(img, W - 1, ch, H - 1 - ch, BEVEL_GRAY, bevel.side)
    if top:
        for i, p in enumerate(bevel.top):
            hline(img, cw, W - 1 - cw, i, (255, 255, 255), p)
    if bottom:
        for i, p in enumerate(bevel.bottom):
            hline(img, W - 1 - cw, cw, H - 1 - i, (0, 0, 0), p)
    for y in range(H):
        for x in range(W):
            if not inside(W, H, x, y):
                img.px[y * W + x] = (0, 0, 0, 0)
    return img


def lcd(img: Image, dev: Device, parts: dict[str, Image], W: int, H: int, x: int, y: int, w: int,
        h: int = LCD_HEIGHT) -> None:
    """The LCD panel's background (LCDV/LCDC DrawSelf): band, left cap, the fill
    tiled (last tile cropped), right cap, the shine centred on the bottom edge."""
    put_band(img, dev, W, H, x, y, w, h)
    left, fill, right, shine = (parts.get(n) for n in ("lcd-left", "lcd-fill", "lcd-right", "lcd-shine"))
    if left:
        over(img, left, x, y)
    if fill:
        cx = x + (left.w if left else LCD_CAP)
        end = x + w - (right.w if right else LCD_CAP)
        while cx < end:
            tw = min(fill.w, end - cx)
            over(img, fill, cx, y, (0, 0, tw, fill.h))
            cx += fill.w
    if right:
        over(img, right, x + w - right.w, y)
    if shine:
        over(img, shine, x + w // 2 - shine.w // 2, y + h - shine.h)


def control(img: Image, dev: Device, W: int, H: int, piece: Image, x: int, y: int) -> None:
    """A metal control (GWBt and friends): its own band, then the image."""
    put_band(img, dev, W, H, x, y, piece.w, piece.h)
    over(img, piece, x, y)


def lcd_text(img: Image, font: bitfont.BitFont, x: int, baseline: int, text: str, ink, shadow) -> int:
    """LCD text as the time display draws it: the shadow at (+1, +1), then the ink
    (1-bit Sydnie strike).  Returns the pen position after the text."""
    for dx, color in ((1, shadow), (0, ink)):
        pen = x + dx
        for ch in text.encode("mac_roman", "replace"):
            g = font.glyph(ch)
            if g is None:
                continue
            for r, row in enumerate(g.rows):
                yy = baseline + dx - g.top + r
                for c, v in enumerate(row):
                    xx = pen + g.left + c
                    if v and 0 <= xx < img.w and 0 <= yy < img.h:
                        img.px[yy * img.w + xx] = tuple(color[:3]) + (img.px[yy * img.w + xx][3],)
            pen += g.advance
    return pen


def _set(img: Image, x: int, y: int, color) -> None:
    if 0 <= x < img.w and 0 <= y < img.h:
        img.px[y * img.w + x] = tuple(color[:3]) + (img.px[y * img.w + x][3],)


def lcd_track(img: Image, x0: int, x1: int, top: int, bottom: int, ink, shadow) -> None:
    """The time slider's track (measured): shadow lines one pixel right and down of the
    ink lines; top and bottom from x0 to x1, right edge at x1, no left edge."""
    for x in range(x0 + 1, x1 + 2):
        _set(img, x, top + 1, shadow)
        _set(img, x, bottom + 1, shadow)
    for y in range(top + 1, bottom + 2):
        _set(img, x1 + 1, y, shadow)
    for x in range(x0, x1 + 1):
        _set(img, x, top, ink)
        _set(img, x, bottom, ink)
    for y in range(top, bottom + 1):
        _set(img, x1, y, ink)


def lcd_playhead(img: Image, x: int, top: int, stem_bottom: int, head, shadow, fill) -> None:
    """The time slider's playhead (measured): an 11 px wide, 5 row triangle and a stem
    at column x, shadow at (+1, +1); the pixel right of the triangle's 4th row is
    reset to the LCD fill colour, as QuickTime Player does."""
    for dx, color in ((1, shadow), (0, head)):
        for i in range(5):
            for xx in range(x - 5 + i, x + 6 - i):
                _set(img, xx + dx, top + i + dx, color)
        for y in range(top + 5, stem_bottom + 1):
            _set(img, x + dx, y + dx, color)
    _set(img, x + 3, top + 3, fill)


def layout(name: str, W: int, H: int) -> list[int]:
    """A LAYOUT entry resolved for a W x H window (x, y[, w, h])."""
    v = list(LAYOUT[name])
    if name == "transport":
        return [W // 2 - v[2] // 2, H + v[1], v[2], v[3]]
    if name in ("title", "lcd"):
        return [v[0], v[1], W + v[2], v[3]]
    return [W + v[0] if v[0] < 0 else v[0], H + v[1] if v[1] < 0 else v[1]]


# -- bundle ---------------------------------------------------------------------
@dataclass
class Player:
    """QuickTime Player 6.0.3 as read from a disk."""
    rf: ResourceFile
    device: Device | None
    parts: dict[str, Image]
    bevel: Bevel
    title: dict
    colors: dict
    sydnie: bitfont.BitFont | None
    missing: list[str]


def load(rf: ResourceFile, data_fork: bytes | None) -> Player:
    vers = rf.data("vers", 1)
    if not vers or len(vers) < 7 or pstring(vers, 6) != VERSION:
        raise MetalError("QuickTime Player is not version " + VERSION)
    missing = []
    parts = pieces(rf)
    for name in PIECES:
        if name not in parts:
            missing.append(f"metal piece {name}")
    try:
        bevel = bevel_from_ppob(rf.data("PPob", PLAYER_PPOB) or b"")
    except MetalError as exc:
        missing.append(f"metal bevel ({exc})")
        bevel = Bevel()
    device, colors = None, {}
    try:
        blocks = read_constants(data_fork or b"")
        tex = rf.data("PICT", TEXTURE_PICT)
        if not tex:
            raise MetalError("no texture PICT 1024")
        device = Device.from_blocks(pict.decode(tex), blocks)
        colors = {n: "#%02x%02x%02x" % rgb16(blocks["lcd_" + n]) for n in ("fill", "ink", "shadow", "head")}
    except (MetalError, ValueError, struct.error, IndexError) as exc:
        missing.append(f"metal body ({exc})")
    sydnie = bitfont.find_strike([rf], "Sydnie", 12)
    if sydnie is None:
        missing.append("metal font Sydnie 12")
    return Player(rf, device, parts, bevel, title_traits(rf), colors, sydnie, missing)


# Bodies pre-rendered into the bundle (W, H); 352 x 106 is an empty player.
BODY_SIZES = ((352, 106),)


def build(bundle, rbi) -> None:
    """Add metal/ to a Bundle (bundle.py): pieces, texture, rendered bodies, fonts and
    the codec's parameters, or one 'missing' entry when the disk has no
    QuickTime Player 6.0.3."""
    from . import rbfont
    rf = path = None
    for cand in PLAYER_PATHS:
        rf = bundle.rf(cand)
        if rf is not None and rf.data("vers", 1):
            path = cand
            break
    if path is None:
        bundle.manifest["missing"].append("metal (no QuickTime Player)")
        return
    try:
        data_fork = bundle.disk.read_fork(path, "data")
        bundle.manifest["sources"][path + " (data)"] = hashlib.sha256(data_fork).hexdigest()
    except (FileNotFoundError, OSError):
        data_fork = None
    try:
        player = load(rf, data_fork)
    except MetalError as exc:
        bundle.manifest["missing"].append(f"metal ({exc})")
        return
    m = {"source": f"QuickTime Player {VERSION}", "pieces": {}, "layout": LAYOUT, "cut": list(CUT),
         "bevel": {"lines": list(player.bevel.lines), "top": list(player.bevel.top), "side": player.bevel.side,
                   "bottom": list(player.bevel.bottom), "gray": "#%02x%02x%02x" % BEVEL_GRAY},
         "title": player.title, "lcd": {"height": LCD_HEIGHT, "colors": player.colors}, "fonts": {}, "bodies": {}}

    def put(name, img, **meta):
        bundle.write(f"metal/{name}.png", img.png())
        bundle.write(f"metal/{name}.rbi", rbi(img))
        return {"file": f"metal/{name}.png", "rbi": f"metal/{name}.rbi", "size": [img.w, img.h], **meta}

    table = image_table(rf)
    for name, img in player.parts.items():
        m["pieces"][name] = put(name, img, source=f"imTB {PIECES[name]} (PNGf {table[PIECES[name]][0]})")
    dev = player.device
    if dev is not None:
        tex = Image(128, 128)
        tex.px = [((v >> 8) & 0xFF, (v >> 8) & 0xFF, v & 0xFF, 255) for row in dev.texture for v in row]
        m["texture"] = put("texture", tex, source=f"PICT {TEXTURE_PICT}")
        m["codec"] = {"dither": list(dev.dither), "k": list(dev.k), "lower": dev.lower, "swoop": list(dev.swoop),
                      "swoop_rows": SWOOP_ROWS, "swoop_steps": SWOOP_STEPS, "cov": list(dev.cov),
                      "oct": list(dev.oct), "step": list(dev.step)}
        for W, H in BODY_SIZES:
            m["bodies"][f"{W}x{H}"] = put(f"body-{W}x{H}", window(dev, player.parts, W, H, player.bevel))
            layer = dev.swoop_layer(W)
            sw = Image(W, SWOOP_ROWS)
            sw.px = [(0, (v >> 8) & 0xFF, v & 0xFF, (v >> 24) & 0xFF) for v in layer]   # G shade, B weight, A upper
            m["bodies"][f"{W}x{H}"]["swoop"] = put(f"swoop-{W}", sw)["rbi"]
    if player.sydnie is not None:
        bundle.write("metal/sydnie-12.rbf", rbfont.encode(player.sydnie))
        s = player.sydnie
        m["fonts"]["sydnie-12"] = {"file": "metal/sydnie-12.rbf", "size": 12, "ascent": s.ascent,
                                   "descent": s.descent + s.leading, "smooth": False}
    title = (player.title.get("active") or {})
    fonts = bundle.rf("System Folder:Fonts:" + str(title.get("font", "Arial")))
    sf = fonts.of_type("sfnt") if fonts else []
    if sf and title.get("size"):
        try:
            from .ttraster import TrueTypeFace
            strike = TrueTypeFace(sf[0].data).strike(str(title["font"]), title["size"], True)
            name = f"{str(title['font']).lower()}-{title['size']}"
            bundle.write(f"metal/{name}.rbf", rbfont.encode(strike))
            m["fonts"][name] = {"file": f"metal/{name}.rbf", "size": strike.size, "ascent": strike.ascent,
                                "descent": strike.descent + strike.leading, "smooth": True, "approximate": True}
        except (OSError, ValueError) as exc:
            player.missing.append(f"metal title font ({exc})")
    else:
        player.missing.append("metal title font")
    bundle.manifest["missing"].extend(player.missing)
    bundle.manifest["metal"] = m
