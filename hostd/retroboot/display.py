"""Display policy (ARCHITECTURE 4.5): scale, pixel scale, guest geometry.

The same rules as the presenter (presenter/src/scale.c) and the image's
rb-bootdisplay, so the presenter's guest target, QEMU's ``-g`` and GRUB's
checkerboard agree.  Pure functions, no imports from the rest of hostd.

* ``suggested_scale``: the automatic scale, the largest s with W/s >= 720 and
  H/s >= 540 (minimum 1).
* ``display.pixel_scale`` s (1-4): every Mac pixel is an s x s block.  The Mac
  screen at s is (floor(W/s/8)*8, floor(H/s)); s is offered when that lies
  within the emulator's -g range, GUEST_MIN..GUEST_MAX.  An unavailable stored
  request falls back to the nearest available scale (smaller first).
* Fixed classic sizes (``display.guest_size``) are drawn at the pixel scale
  when they fit, otherwise at the largest integer that fits (``fit``
  scaling stays the only non-integer option).
* Portrait displays (a rotated panel, ``display.rotation``): the same rules
  on the display's side.  The automatic scale uses the long and short sides,
  so turning the display keeps it; the -g range is GUEST_PMIN..GUEST_PMAX
  (512 wide at least: the video driver's smallest live resize), and a
  classic size turns on its side when that is a valid portrait size
  (1024x768 -> 768x1024; 640x480 and 1152x870 stay).
"""
from __future__ import annotations

GUEST_MIN = (640, 480)
# Largest Mac OS screen verified with the r3 package and the RetroBoot video
# driver (Mac OS 9.2 to the Finder at 3840x2160x32; the presenter has the same
# limits in scale.h).
GUEST_MAX = (3840, 2160)
GUEST_PMIN = (512, 640)
GUEST_PMAX = (2160, 3840)
PIXEL_SCALES = (1, 2, 3, 4)


def portrait(width: int, height: int) -> bool:
    return height > width


def guest_limits(width: int, height: int) -> tuple[tuple[int, int], tuple[int, int]]:
    """(min, max) Mac screen for a display of this orientation."""
    return (GUEST_PMIN, GUEST_PMAX) if portrait(width, height) else (GUEST_MIN, GUEST_MAX)


def suggested_scale(width: int, height: int) -> int:
    long, short = max(width, height), min(width, height)
    s = 1
    while long // (s + 1) >= 720 and short // (s + 1) >= 540:
        s += 1
    return s


def scaled_guest_size(width: int, height: int, s: int) -> tuple[int, int]:
    s = max(1, int(s))
    return (width // s // 8) * 8, height // s


def clamp_guest(gw: int, gh: int, limits: tuple = (GUEST_MIN, GUEST_MAX)) -> tuple[int, int]:
    lo, hi = limits
    return (max(lo[0], min(hi[0], gw)), max(lo[1], min(hi[1], gh)))


def pixel_scale_valid(width: int, height: int, s: int) -> bool:
    if s not in PIXEL_SCALES:
        return False
    gw, gh = scaled_guest_size(width, height, s)
    lo, hi = guest_limits(width, height)
    return lo[0] <= gw <= hi[0] and lo[1] <= gh <= hi[1]


def requested_scale(value) -> int:
    """Setting value ("auto", "1".."4" or an int) -> 0 (auto) or 1..4."""
    if value in (None, "auto", 0):
        return 0
    try:
        s = int(value)
    except (TypeError, ValueError):
        return 0
    return s if s in PIXEL_SCALES else 0


def effective_scale(width: int, height: int, pixel_scale="auto") -> int:
    req = requested_scale(pixel_scale)
    if not req:
        return suggested_scale(width, height)
    for s in list(range(req, 0, -1)) + list(range(req + 1, PIXEL_SCALES[-1] + 1)):
        if pixel_scale_valid(width, height, s):
            return s
    return suggested_scale(width, height)


def auto_guest_size(width: int, height: int, s: int) -> tuple[int, int]:
    return clamp_guest(*scaled_guest_size(width, height, s), guest_limits(width, height))


def oriented_size(width: int, height: int, gw: int, gh: int) -> tuple[int, int]:
    """A configured classic size on a display of this orientation: on its side on
    a portrait display when that is a valid portrait size (presenter
    rb_guest_size_for)."""
    if portrait(width, height) and gw > gh and gh % 8 == 0 and gh >= GUEST_PMIN[0] and gw >= GUEST_PMIN[1]:
        return gh, gw
    return gw, gh


def unavailable_reason(width: int, height: int, s: int) -> str | None:
    gw, gh = scaled_guest_size(width, height, s)
    lo, hi = guest_limits(width, height)
    if gw < lo[0] or gh < lo[1]:
        return (f"The Mac screen would be {gw} × {gh}; Mac OS needs at least "
                f"{lo[0]} × {lo[1]}.")
    if gw > hi[0] or gh > hi[1]:
        return (f"The Mac screen would be {gw} × {gh}; RetroBoot supports up to "
                f"{hi[0]} × {hi[1]}.")
    return None


def pixel_scale_choices(width: int, height: int) -> list[dict]:
    """Every numeric pixel scale for a display: Mac screen size, availability."""
    out = []
    for s in PIXEL_SCALES:
        gw, gh = scaled_guest_size(width, height, s)
        reason = unavailable_reason(width, height, s)
        out.append({"scale": s, "size": [gw, gh], "available": reason is None, "reason": reason})
    return out


def integer_factor(width: int, height: int, gw: int, gh: int, pixel_scale="auto", scale: int | None = None) -> int:
    """The factor a guest of gw x gh is drawn at with integer scaling: the
    pixel scale when it fits, else (and for auto) the largest that fits; 0
    when even 1x does not fit (the presenter then fits it smoothly)."""
    k = min(width // gw, height // gh) if gw and gh else 0
    if requested_scale(pixel_scale) and k > 0:
        s = scale or effective_scale(width, height, pixel_scale)
        k = min(k, s)
    return k


def guest_geometry(display: dict | None, guest_size: str, pixel_scale="auto") -> tuple[int, int]:
    """Boot framebuffer size for ``-g``: the presenter's guest target for this
    display and these settings (presenter/src/scale.c rb_auto_guest_size with
    rb_effective_scale), so the guest starts at its final size."""
    mode = display.get("mode") if display else None
    if guest_size and guest_size != "auto":
        gw, gh = (int(v) for v in guest_size.split("x"))
        return oriented_size(int(mode[0]), int(mode[1]), gw, gh) if mode else (gw, gh)
    if not mode:
        return 1024, 768
    w, h = int(mode[0]), int(mode[1])
    return auto_guest_size(w, h, effective_scale(w, h, pixel_scale))
