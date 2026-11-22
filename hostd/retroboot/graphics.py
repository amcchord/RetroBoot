"""The guest's graphics card (ARCHITECTURE §4.9; research/gfx).

``standard`` (default) is QEMU's std-VGA with RetroBoot's NDRV, host resize
and the accelerated-boot hand-off.  ``ati-radeon``, offered in the classic
panel and the web as "3D acceleration (ATI Radeon)", is the emulated ATI
Radeon R100 that Mac OS 9's own ATI drivers drive: hardware 3D through RAVE
and OpenGL, rendered on this computer's GPU (EGL surfaceless); QuickDraw
draws in software.  It applies at the next Mac OS start.

The card is used only when all of these hold, otherwise the guest gets
``standard``, status says why and a notice tells people (never a Mac OS
whose 3D draws nothing):

* the setting asks for it;
* the emulator package lists the feature ``ati-r100-1`` (the R100 model and
  the firmware that names it ATY,Rage6p);
* this computer has a working hardware host GL: ``libEGL.so.1`` and
  ``libGLESv2.so.2`` load, a GPU render node (``/dev/dri/renderD*``) exists,
  and a GLES 3 context comes up through EGL surfaceless on it.
  ``probe_host_gl`` checks that once at hostd's start in a child process, as
  the guest user with the guest's groups, exactly as QEMU will use it;
  hostd.json ``graphics_allow_software`` (lab) accepts Mesa's software
  renderer;
* the Mac's disk has ATI's software in its Extensions folder (``ATI Driver
  Update`` and the rest of rbguestprep.ati's ``radeon-3d`` set: the default
  base os922-full-v3 has it; ``graphics_install_job`` adds it to other disks
  from the user's Mac OS 9.2.1 CD in the media library).  The check is
  read-only and runs before each launch (``prepare_launch``).

The panel and the web offer the choice only when the first three hold and
the active Mac has ATI's software (``availability``); otherwise it is shown
disabled with the reason.

Mac screen size with the card (ARCHITECTURE §4.9 resolution policy).  ATI's
driver offers its own fixed modes (``ATI_SIZES``) plus the detailed timing of
the monitor's EDID, which it reads over DDC.  With a package that lists
``ati-edid-modes`` (the emulated monitor's EDID honours ati-vga's
``xres``/``yres``), hostd gives the card a monitor whose detailed timing is
exactly the Mac screen RetroBoot would give the standard card (the display at
the current pixel scale, or the fixed ``display.guest_size``), so Mac OS gets
that size, widescreen included (``EXACT_MIN``..``EXACT_MAX``, landscape;
research/gfx/reports/gfxcard-2026-10-15.md).  Otherwise, and outside those
limits, hostd picks the largest fixed mode that fits that screen (every
classic size is an ATI mode) and the presenter scales or letterboxes it with
the 4.5 rules.  ATI's driver starts every session at 640 x 480 (the
emulated NVRAM is empty), so QEMU's boot framebuffer (``-g``), the startup
indicator and the checkerboard anchor all use ``STARTUP_SIZE`` and the cut-over
to Mac OS's startup screen lines up; ``RetroBoot Display`` (guest/rbdisplay, in
Extensions) sets the chosen size from ``-prom-env retroboot-display=WxH``: its
startup extension during the extension parade (and Mac OS restores the saved
size by itself on later startups), its app as the Finder starts on disks that
only have the app.  Without the helper Mac OS stays at 640 x 480.  Host-side resizes have no effect on the card; a size
chosen in Monitors lasts until Mac OS restarts.

Status reports ``mac_screen``, the screen Mac OS has (the presenter's latest
guest surface while it runs, else the size the next start gives it with the
card it will use), and ``ati_screen``, the size the card would give it.

The Control Strip (``place_control_strip``).  Switching the card, like a
pixel size or display chosen while Mac OS is shut down, starts Mac OS on a
screen of another size.  Mac OS moves the Control Strip with the screen when
the resolution changes while it runs, but at startup it puts the strip at its
saved absolute place: 96 pixels above the bottom of a 1080 x 720 screen after
an 832 x 624 one.  Before each launch hostd gives the saved place the screen
Mac OS will start with, the way Mac OS itself moves it (rbguestprep.controlstrip,
hostd/reports/cardswitch-2026-10-13.md).
"""
from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("retroboot.graphics")

FEATURE = "ati-r100-1"
# The emulated monitor's EDID carries ati-vga's xres/yres as its detailed timing
# (emulator/patches/retroboot-ati-r100.patch), which ATI's driver offers as a mode.
EDID_FEATURE = "ati-edid-modes"
SETTING = "display.graphics"
STANDARD, ATI = "standard", "ati-radeon"
ATI_DEVICE = "ati-vga,model=r100,vgamem_mb=32,romfile=,guest_hwcursor=on"
STANDARD_ARGS = ["-vga", "std", "-global", "VGA.vgamem_mb=64", "-global", "VGA.host-resize=on",
                 "-global", "VGA.packed-lowbpp=on", "-global", "VGA.hardware-cursor=off",
                 "-global", "VGA.untracked-vram=off"]
ATI_ARGS = ["-vga", "none", "-device", ATI_DEVICE]
# Every size ATI's display driver (ATI Driver Update 1.9.5, .Display_RADEON)
# offers on the emulated R100 (RB ModeSwitch's mode list, research/gfx): its own
# timing table plus the EDID's standard and detailed timings (1280 x 800 is
# QEMU's EDID default).  No portrait sizes.  Used as is by packages without
# ``ati-edid-modes``, and by every package outside EXACT_MIN..EXACT_MAX.
ATI_SIZES = ((640, 480), (800, 600), (832, 624), (1024, 768), (1152, 870), (1280, 800), (1280, 960),
             (1280, 1024), (1600, 1200), (1792, 1344), (1856, 1392), (1920, 1080), (1920, 1200), (1920, 1440))
# The Mac screens given to the card exactly through the monitor's EDID: landscape,
# a width in 8-pixel steps (the CRTC's unit; the standard card's widths are too),
# up to ATI's own largest mode (3D needs front, back and Z buffers in 32 MB of VRAM)
# and within the pixel clock below (1920 x 1440 itself is a fixed mode).
EXACT_MIN = (640, 480)
EXACT_MAX = (1920, 1440)
# ATI's driver ignores the whole EDID when its detailed timing needs a pixel clock
# above about 165 MHz (lab: 1680 x 1050 accepted at 162.6 MHz, ignored at 172.4 MHz;
# a single-link TMDS limit).  QEMU's timing has generous blanking, so the monitor's
# refresh is the highest of EDID_REFRESH_HZ that stays within EDID_MAX_CLOCK_KHZ (the
# refresh means nothing to the emulation: the card's vblank is x-vblank-hz).  Keep 75
# Hz first: with 960 x 540 at 60 Hz (43.4 MHz) ATI's driver offered no 960 x 540 mode
# and Mac OS stayed at 640 x 480 (Ryzen mini PC, RetroBoot 1.6.2).
EDID_MAX_CLOCK_KHZ = 162000
EDID_REFRESH_HZ = (75, 60, 50)       # QEMU's EDID range limits allow 50-125 Hz
# The driver's own startup mode with an empty NVRAM: Mac OS's startup screen and
# extension parade with the card (QEMU -g, startup indicator, checkerboard anchor).
STARTUP_SIZE = (640, 480)
DISPLAY_OPTION = "retroboot-display"     # -prom-env option read by RetroBoot Display
HINT_FILE = "graphics.json"              # state/: the card of the last launch, for rb-bootdisplay
GUEST_GROUPS = ("audio", "rbdata", "render", "video")   # launcher.py gives QEMU the same

# The host GL found by probe_host_gl (None until probed).
_host_gl: dict | None = None

REASONS = {
    "osx": "Mac OS X graphics acceleration has not been qualified",
    "package": "the emulator does not include the ATI Radeon card",
    "host-gl": "this computer's graphics (OpenGL ES through EGL) is not available",
    "guest-software": "Mac OS does not have ATI's graphics software installed",
    "family": "this Mac is a 68k Mac",
}

# Runs in a child process (python3 -I -c): loads EGL/GLES like QEMU's ati_gl.c,
# creates a GLES 3 context on EGL surfaceless and prints one JSON line.
PROBE = r"""
import ctypes, json, sys
out = {"ok": False}
try:
    egl = ctypes.CDLL("libEGL.so.1")
except OSError as e:
    print(json.dumps({"ok": False, "reason": "no-libegl", "detail": str(e)[:200]})); sys.exit(0)
try:
    gles = ctypes.CDLL("libGLESv2.so.2")
except OSError as e:
    print(json.dumps({"ok": False, "reason": "no-libgles", "detail": str(e)[:200]})); sys.exit(0)
P = ctypes.c_void_p
egl.eglGetProcAddress.restype = P; egl.eglGetProcAddress.argtypes = [ctypes.c_char_p]
addr = egl.eglGetProcAddress(b"eglGetPlatformDisplayEXT") or egl.eglGetProcAddress(b"eglGetPlatformDisplay")
if not addr:
    print(json.dumps({"ok": False, "reason": "egl-platform"})); sys.exit(0)
get_dpy = ctypes.CFUNCTYPE(P, ctypes.c_uint, P, P)(addr)
dpy = get_dpy(0x31DD, None, None)          # EGL_PLATFORM_SURFACELESS_MESA
egl.eglInitialize.argtypes = [P, P, P]; egl.eglInitialize.restype = ctypes.c_uint
if not dpy or not egl.eglInitialize(dpy, None, None):
    print(json.dumps({"ok": False, "reason": "egl-init"})); sys.exit(0)
egl.eglBindAPI.argtypes = [ctypes.c_uint]; egl.eglBindAPI(0x30A0)   # EGL_OPENGL_ES_API
attrs = (ctypes.c_int * 3)(0x3040, 0x40, 0x3038)                      # RENDERABLE_TYPE ES3, NONE
cfg = P(); n = ctypes.c_int()
egl.eglChooseConfig.argtypes = [P, P, P, ctypes.c_int, P]; egl.eglChooseConfig.restype = ctypes.c_uint
egl.eglChooseConfig(dpy, attrs, ctypes.byref(cfg), 1, ctypes.byref(n))
cattrs = (ctypes.c_int * 3)(0x3098, 3, 0x3038)                        # CONTEXT_MAJOR_VERSION 3
egl.eglCreateContext.argtypes = [P, P, P, P]; egl.eglCreateContext.restype = P
ctx = egl.eglCreateContext(dpy, cfg if n.value else None, None, cattrs)
egl.eglMakeCurrent.argtypes = [P, P, P, P]; egl.eglMakeCurrent.restype = ctypes.c_uint
if not ctx or not egl.eglMakeCurrent(dpy, None, None, ctx):
    print(json.dumps({"ok": False, "reason": "gles-context"})); sys.exit(0)
gles.glGetString.restype = ctypes.c_char_p; gles.glGetString.argtypes = [ctypes.c_uint]
r = (gles.glGetString(0x1F01) or b"").decode("latin-1")               # GL_RENDERER
v = (gles.glGetString(0x1F02) or b"").decode("latin-1")               # GL_VERSION
print(json.dumps({"ok": True, "renderer": r, "version": v}))
"""


def render_nodes(dev: str = "/dev/dri") -> list[str]:
    return sorted(glob.glob(os.path.join(dev, "renderD*")))


def is_software(renderer: str | None) -> bool:
    r = (renderer or "").lower()
    return any(s in r for s in ("llvmpipe", "softpipe", "swrast", "lavapipe"))


def probe_host_gl(run_as: str | None = None, allow_software: bool = False,
                  dev: str = "/dev/dri", timeout: float = 20.0) -> dict:
    """Check the host GL QEMU's ATI card needs (blocking; seconds at most).

    Returns {ok, reason, renderer, version, software, render_nodes, checked_at}.
    ``reason`` (when not ok): no-libegl, no-libgles, no-render-node, egl-platform,
    egl-init, gles-context, software-only, probe-failed."""
    nodes = render_nodes(dev)
    info: dict[str, Any] = {"ok": False, "render_nodes": nodes, "checked_at": time.time()}
    kw: dict[str, Any] = {}
    if run_as and os.geteuid() == 0:
        import grp
        import pwd
        try:
            pw = pwd.getpwnam(run_as)
            groups = []
            for g in GUEST_GROUPS:
                try:
                    groups.append(grp.getgrnam(g).gr_gid)
                except KeyError:
                    pass
            kw = {"user": pw.pw_uid, "group": pw.pw_gid, "extra_groups": groups}
        except KeyError:
            pass
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "EGL_PLATFORM": "surfaceless"}
    for k in ("LD_LIBRARY_PATH", "LIBGL_DRIVERS_PATH", "MESA_LOADER_DRIVER_OVERRIDE",
              "__EGL_VENDOR_LIBRARY_DIRS"):
        if os.environ.get(k):
            env[k] = os.environ[k]          # as QEMU inherits them from the launcher
    res = None
    try:
        res = subprocess.run([sys.executable, "-I", "-c", PROBE], capture_output=True, text=True,
                             timeout=timeout, env=env, **kw)
        got = json.loads(res.stdout.strip().splitlines()[-1]) if res.stdout.strip() else {}
    except (OSError, ValueError, subprocess.TimeoutExpired, IndexError) as exc:
        got = {"ok": False, "reason": "probe-failed", "detail": str(exc)[:200]}
    if not got:
        got = {"ok": False, "reason": "probe-failed",
               "detail": ((res.stderr if res is not None else "") or "")[-200:]}
    info.update({k: v for k, v in got.items() if k != "ok"})
    info["software"] = is_software(got.get("renderer"))
    if not got.get("ok"):
        info.setdefault("reason", "probe-failed")
        return info
    if not nodes and not allow_software:
        info["reason"] = "no-render-node"
        return info
    if info["software"] and not allow_software:
        info["reason"] = "software-only"     # a render node, but Mesa fell back (permissions?)
        return info
    info["ok"] = True
    info.pop("reason", None)
    return info


def set_host_gl(info: dict | None) -> None:
    global _host_gl
    _host_gl = info


def host_gl() -> dict | None:
    return _host_gl


def host_gl_ok() -> bool:
    return bool(_host_gl and _host_gl.get("ok"))


def package_features(package_root: Path | None) -> list[str]:
    """The emulator package's manifest features ([] when unreadable)."""
    if package_root is None:
        return []
    try:
        m = json.loads((Path(package_root) / "manifest.json").read_text())
    except (OSError, ValueError):
        return []
    return list(m.get("features") or [])


def package_capable(package_root: Path) -> bool:
    """The emulator package has the R100 card and its firmware."""
    return FEATURE in package_features(package_root)


def edid_modes(package_root: Path | None) -> bool:
    """The package's ATI card offers the EDID's detailed timing (xres/yres) as a mode,
    so the card can be given the exact Mac screen (``ati_size(exact=True)``)."""
    feats = package_features(package_root)
    return FEATURE in feats and EDID_FEATURE in feats


def capable(package_root: Path) -> bool:
    """Capability ``ati_radeon``: the package has the card and the host GL works."""
    return package_capable(package_root) and host_gl_ok()


def guest_software(machine) -> str | None:
    """"present" | "absent" | None (not checked yet), from the last check or guest prep."""
    if machine is None:
        return None
    seen = (machine.meta.get("graphics") or {}).get("ati_software")
    if seen in ("present", "absent"):
        return seen
    prep = machine.meta.get("prep") or {}
    if prep.get("result") == "applied":
        return (prep.get("report") or {}).get("ati_software")
    return None


def requested(settings: dict) -> str:
    return ATI if settings.get(SETTING, STANDARD) == ATI else STANDARD


def why_not(settings: dict, machine, package_root: Path) -> str | None:
    """Why the requested card is not used (None when it is, or nothing was asked)."""
    if requested(settings) != ATI:
        return None
    if getattr(machine, "osx", False):
        from . import osx_graphics
        why = osx_graphics.why_not(machine, package_root)
        return "host-gl" if why == "checking" else why
    if getattr(machine, "quadra", False):
        return "family"             # a 68k Mac (quadra.py): the Quadra's own framebuffer card only
    if not package_capable(package_root):
        return "package"            # the emulator package lacks the card
    if not host_gl_ok():
        return "host-gl"            # no working EGL/GLES on this computer (see host_gl())
    if guest_software(machine) != "present":
        return "guest-software"     # Mac OS lacks ATI's drivers (or not checked yet)
    return None


def effective(settings: dict, machine, package_root: Path) -> str:
    """The card the guest gets at the next launch."""
    if requested(settings) == ATI and why_not(settings, machine, package_root) is None:
        return ATI
    return STANDARD


def device_args(card: str, screen: tuple[int, int] | None = None, *,
                direct_framebuffer: bool = False) -> list[str]:
    """QEMU display-device arguments for build_argv.  ``screen`` (the Mac screen, with a
    package that has ``ati-edid-modes``) becomes the emulated monitor's EDID detailed
    timing, which ATI's driver offers as a mode, at the refresh ``edid_refresh`` picks.
    ``direct_framebuffer``: the standard card's opt-in compatibility path for a Mac
    whose games write screen memory without marking it dirty."""
    if card != ATI:
        args = list(STANDARD_ARGS)
        if direct_framebuffer is True:
            args[args.index("VGA.untracked-vram=off")] = "VGA.untracked-vram=on"
        return args
    hz = edid_refresh(*screen) if screen is not None else None
    if hz is None:
        return list(ATI_ARGS)
    return ["-vga", "none", "-device", "%s,xres=%d,yres=%d,refresh_rate=%d"
            % (ATI_DEVICE, int(screen[0]), int(screen[1]), hz * 1000)]


def edid_clock_khz(w: int, h: int, hz: int) -> int:
    """The pixel clock of QEMU's EDID detailed timing for w x h at hz
    (hw/display/edid-generate.c generate_timings, in its integer arithmetic)."""
    xblank, yblank = w * 35 // 100, h * 35 // 1000
    return hz * 1000 * (w + xblank) * (h + yblank) // 10000000 * 10


def edid_refresh(w: int, h: int) -> int | None:
    """The monitor refresh (Hz) for an exact w x h, or None when no allowed refresh
    keeps the pixel clock within what ATI's driver accepts."""
    for hz in EDID_REFRESH_HZ:
        if edid_clock_khz(int(w), int(h), hz) <= EDID_MAX_CLOCK_KHZ:
            return hz
    return None


# --- Mac screen size with the card (resolution policy) ----------------------------
def exact_size(bound: tuple[int, int]) -> tuple[int, int] | None:
    """``bound`` as a Mac screen the card can be given exactly through the EDID (width
    rounded down to 8 pixels), or None outside EXACT_MIN..EXACT_MAX, when portrait, or
    when its timing would need too high a pixel clock (``edid_refresh``)."""
    w, h = int(bound[0]) // 8 * 8, int(bound[1])
    if w < h or not (EXACT_MIN[0] <= w <= EXACT_MAX[0] and EXACT_MIN[1] <= h <= EXACT_MAX[1]):
        return None
    if edid_refresh(w, h) is None:
        return None
    return w, h


def ati_size(bound: tuple[int, int], exact: bool = False) -> tuple[int, int]:
    """The Mac screen with the card for the standard card's screen ``bound``: ``bound``
    itself when ``exact`` (a package with ``ati-edid-modes``) and it is within the EDID
    limits; else the largest fixed ATI mode (area, then width) within it; STARTUP_SIZE
    if none fits."""
    if exact:
        size = exact_size(bound)
        if size is not None:
            return size
    bw, bh = int(bound[0]), int(bound[1])
    fits = [s for s in ATI_SIZES if s[0] <= bw and s[1] <= bh]
    return max(fits, key=lambda s: (s[0] * s[1], s[0])) if fits else STARTUP_SIZE


def mac_screen(display: dict | None, settings: dict, exact: bool = False) -> tuple[int, int]:
    """The Mac screen hostd asks for with the card: the Mac screen the standard card
    would get (display at the pixel scale, or the fixed ``display.guest_size``),
    exactly when ``exact`` allows it, else the largest ATI mode within it."""
    from .display import guest_geometry
    return ati_size(guest_geometry(display, settings.get("display.guest_size", "auto"),
                                   settings.get("display.pixel_scale", "auto")), exact)


def launch_args(card: str, display: dict | None, settings: dict,
                exact: bool = False) -> tuple[tuple[int, int] | None, list[str]]:
    """(boot framebuffer size or None to keep the standard one, extra QEMU arguments).
    ``exact``: the package has ``ati-edid-modes`` (``mac_screen``)."""
    if card != ATI:
        return None, []
    w, h = mac_screen(display, settings, exact)
    return STARTUP_SIZE, ["-prom-env", f"{DISPLAY_OPTION}={w}x{h}"]


def launch_device_args(card: str, display: dict | None, settings: dict, exact: bool = False, *,
                       direct_framebuffer: bool = False) -> list[str]:
    """build_argv's display device: with ``exact`` the ATI card's monitor reports the
    Mac screen of ``launch_args`` as its EDID detailed timing; ``direct_framebuffer``
    as in ``device_args`` (standard card only)."""
    if card != ATI or not exact:
        return device_args(card, direct_framebuffer=direct_framebuffer)
    return device_args(card, mac_screen(display, settings, exact))


def read_hint(state_dir: Path) -> dict:
    try:
        d = json.loads((Path(state_dir) / HINT_FILE).read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


AVAILABILITY = {
    "osx": "Mac OS X graphics acceleration has not been qualified.",
    "package": "This version of RetroBoot's emulator does not include the ATI Radeon.",
    "host-gl": "This computer's graphics chip can't be used for 3D (OpenGL ES through EGL is not available).",
    "checking": "RetroBoot is still checking this computer's graphics.",
    "no-machine": "No Mac is installed.",
    "guest-software": "This Mac's disk doesn't have ATI's graphics software. Install it from the Mac OS 9.2.1 CD.",
    "family": "3D acceleration needs a PowerPC Mac; this Mac is a 68k Mac.",
}


class Graphics:
    """hostd's view: the host GL probe, capability, availability, the card of the
    next launch, status, notice, the pre-launch software check and installing
    ATI's software."""

    def __init__(self, app):
        self.app = app
        self._probe_task: asyncio.Task | None = None
        self.running: str | None = None          # the card of the running session
        self._pushed_startup: tuple | None = None

    def begin(self) -> None:
        """Start the host GL probe beside hostd's start (only with a capable package)."""
        from . import osx_graphics
        native = osx_graphics.EGL_FEATURE in package_features(getattr(getattr(self.app, "paths", None), "qemu_osx_package", None))
        if self._probe_task is None and (package_capable(self.app.paths.qemu_package) or native):
            self._probe_task = asyncio.get_running_loop().create_task(self.start(), name="graphics-probe")

    async def wait_probe(self, timeout: float = 30.0) -> None:
        if self._probe_task is not None and not self._probe_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._probe_task), timeout)
            except Exception:
                pass

    # The GPU driver may register its render node just after hostd starts (on the Ryzen
    # HX 370 amdgpu initialised 2 ms after the first probe): wait this long for one.
    RENDER_NODE_WAIT_S = 20.0

    async def start(self) -> None:
        """Probe the host GL (in a thread; the probe is a child process).  Without a render
        node yet, wait for the GPU driver (RENDER_NODE_WAIT_S) and probe again."""
        cfg = self.app.cfg
        dev = getattr(cfg, "graphics_dri_dir", None) or "/dev/dri"

        async def probe():
            return await asyncio.to_thread(probe_host_gl, getattr(cfg, "run_as_user", None),
                                           bool(getattr(cfg, "graphics_allow_software", False)), dev)
        info = await probe()
        if not info.get("ok") and not info.get("render_nodes"):
            deadline = time.monotonic() + self.RENDER_NODE_WAIT_S
            while time.monotonic() < deadline and not render_nodes(dev):
                await asyncio.sleep(0.1)
            if render_nodes(dev):
                self.app.lifecycle.event("graphics_render_node", nodes=render_nodes(dev),
                                         waited_s=round(self.RENDER_NODE_WAIT_S - (deadline - time.monotonic()), 2))
                info = await probe()
        set_host_gl(info)
        self.app.lifecycle.event("graphics_host_gl", ok=info.get("ok"), reason=info.get("reason"),
                                 renderer=info.get("renderer"), render_nodes=len(info.get("render_nodes") or []))
        if not info.get("ok"):
            log.info("ATI Radeon card unavailable: host GL %s", info.get("reason"))
        from . import osx_graphics
        osx_package = getattr(getattr(self.app, "paths", None), "qemu_osx_package", None)
        if osx_graphics.EGL_FEATURE in package_features(osx_package):
            native = await asyncio.to_thread(osx_graphics.probe_host_gl,
                                            getattr(cfg, "run_as_user", None), dev)
            osx_graphics.set_host_gl(native)
            self.app.lifecycle.event("osx_graphics_host_gl", ok=native.get("ok"),
                                     reason=native.get("reason"), renderer=native.get("renderer"),
                                     render_node=native.get("render_node"))

    async def reprobe_if_changed(self) -> None:
        """Before a launch: probe again when the last probe failed and the render nodes
        changed since (a GPU driver that came up late, a hot-plugged eGPU)."""
        from . import osx_graphics
        native = osx_graphics.host_gl()
        cfg = getattr(self.app, "cfg", None)
        dev = getattr(cfg, "graphics_dri_dir", None) or "/dev/dri"
        if osx_graphics.EGL_FEATURE in package_features(getattr(getattr(self.app, "paths", None), "qemu_osx_package", None)) \
                and not osx_graphics.hardware_ready() \
                and render_nodes(dev) != list((native or {}).get("render_nodes") or []):
            osx_graphics.set_host_gl(await asyncio.to_thread(osx_graphics.probe_host_gl,
                                                           getattr(cfg, "run_as_user", None), dev))
        gl = host_gl()
        if gl is None or gl.get("ok") or (self._probe_task is not None and not self._probe_task.done()):
            return
        dev = getattr(self.app.cfg, "graphics_dri_dir", None) or "/dev/dri"
        if render_nodes(dev) and render_nodes(dev) != list(gl.get("render_nodes") or []):
            await self.start()

    def capable(self) -> bool:
        m = self.app.supervisor.active_machine()
        if getattr(m, "osx", False):
            from . import osx_graphics
            return osx_graphics.qualified(m, self.app.paths.qemu_osx_package) and osx_graphics.hardware_ready()
        return capable(self.app.paths.qemu_package)

    def exact(self) -> bool:
        """The package gives the card the exact Mac screen (``edid_modes``)."""
        paths = getattr(self.app, "paths", None)
        if getattr(self.app.supervisor.active_machine(), "osx", False):
            from . import osx_graphics
            return osx_graphics.qualified(self.app.supervisor.active_machine(), paths.qemu_osx_package)
        return edid_modes(getattr(paths, "qemu_package", None))

    def availability(self, machine=None) -> dict:
        """Whether 3D acceleration can be offered for the active Mac, and why not."""
        pkg = self.app.paths.qemu_package
        m = machine if machine is not None else self.app.supervisor.active_machine()
        if getattr(m, "osx", False):
            from . import osx_graphics
            why = osx_graphics.why_not(m, self.app.paths.qemu_osx_package)
        elif getattr(m, "quadra", False):
            why = "family"
        elif not package_capable(pkg):
            why = "package"
        elif host_gl() is None:
            why = "checking"
        elif not host_gl_ok():
            why = "host-gl"
        elif m is None:
            why = "no-machine"
        elif guest_software(m) != "present":
            why = "guest-software"
        else:
            why = None
        return {"available": why is None, "reason": why, "message": AVAILABILITY.get(why)}

    def ui_context(self) -> dict:
        """For the settings schema (ValidationContext.graphics), the panel and the web.
        ``by_machine`` has the same answer for every installed Mac, so a change that
        switches the startup Mac and turns 3D acceleration on together is judged by
        the Mac that will start."""
        out = self.availability()
        out["osx"] = bool(getattr(self.app.supervisor.active_machine(), "osx", False))
        machines = getattr(self.app, "machines", None)
        try:
            out["by_machine"] = {m.id: self.availability(m)
                                 for m in (machines.list() if machines is not None else [])}
        except Exception:                    # never break the settings page over this
            out["by_machine"] = {}
        display = self.app.presenter.display if getattr(self.app, "presenter", None) else None
        if display and display.get("mode"):
            out["ati_screen"] = list(mac_screen(display, self.app.settings.desired, self.exact()))
        else:
            out["ati_screen"] = None
        return out

    def next_card(self) -> str:
        """The card of the next launch (applied settings).  Until the host GL probe
        has finished, the card of the last launch (state hint) stands in, so the
        presenter's startup geometry does not jump while hostd starts."""
        settings = self.app.settings.applied
        if requested(settings) != ATI:
            return STANDARD
        m = self.app.supervisor.active_machine()
        if getattr(m, "osx", False):
            return effective(settings, m, self.app.paths.qemu_osx_package)
        if host_gl() is None:
            hint = read_hint(self.app.paths.state)
            return ATI if hint.get("card") == ATI and m is not None and hint.get("machine") == m.id else STANDARD
        return effective(settings, m, self.app.paths.qemu_package)

    def startup_guest_size(self) -> list[int] | None:
        """display_config guest_size override: ATI's startup mode while the card is the
        one Mac OS starts (or runs) with; None keeps the standard target."""
        if getattr(self.app.supervisor.active_machine(), "osx", False):
            return None    # Native OS X owns its saved desktop mode; no classic 640×480 handoff.
        card = self.running if self.app.supervisor.active and self.running else self.next_card()
        return list(STARTUP_SIZE) if card == ATI else None

    # --- the Mac screen and the Control Strip -------------------------------------------
    def _display(self) -> dict | None:
        display = self.app.presenter.display if getattr(self.app, "presenter", None) else None
        return display if display and display.get("mode") else None

    def start_screen(self, card: str | None = None) -> tuple[int, int] | None:
        """The Mac screen the next launch starts Mac OS with, when the Control Strip and the
        Finder start: with the ATI Radeon hostd's ATI size (RetroBoot Display's startup
        extension sets it during the extension parade), with the standard card its boot
        framebuffer (``-g``, the display at the pixel size, or the fixed Mac screen size).
        None without a display mode (the guest then gets its size by a live resize)."""
        display = self._display()
        if display is None:
            return None
        settings = self.app.settings.applied
        if getattr(self.app.supervisor.active_machine(), "osx", False):
            from .display import guest_geometry
            return guest_geometry(display, settings.get("display.guest_size", "auto"),
                                  settings.get("display.pixel_scale", "auto"))
        if (card or self.next_card()) == ATI:
            return mac_screen(display, settings, self.exact())
        from .display import guest_geometry
        return guest_geometry(display, settings.get("display.guest_size", "auto"),
                              settings.get("display.pixel_scale", "auto"))

    def current_screen(self) -> list[int] | None:
        """The Mac screen for status: Mac OS's own while it runs (the presenter's latest guest
        surface of this session), else what the next start gives it."""
        sup = self.app.supervisor
        if sup.active and self.running:
            surface = getattr(sup, "guest_surface", None)
            if surface:
                return [int(surface[0]), int(surface[1])]
            screen = self.start_screen(self.running)
        else:
            screen = self.start_screen()
        return list(screen) if screen else None

    async def place_control_strip(self, machine) -> None:
        """supervisor.prepare_disk hook (guest stopped, after prepare_launch): when the Mac
        screen of this start differs from the one the Control Strip saved its place on
        (the card was switched, or the pixel size or display changed while Mac OS was shut
        down), move the saved place as Mac OS does when the screen changes while it runs:
        a strip on the bottom edge stays there, its side and state are kept.  12 bytes in
        place, about 25 ms; nothing is created."""
        fit = getattr(self.app.guestprep, "control_strip", None)
        size = self.start_screen() if machine is not None and fit is not None else None
        if size is None:
            return
        res = await fit(machine.disk, size)
        if res.get("result") not in ("unchanged", "missing", "no-settings", "unavailable"):
            self.app.lifecycle.event("control_strip", machine=machine.id, screen="%dx%d" % tuple(size), **res)

    def public(self) -> dict[str, Any]:
        settings = self.app.settings.applied
        m = self.app.supervisor.active_machine()
        pkg = self.app.paths.qemu_package
        gl = host_gl() or {}
        if getattr(m, "osx", False):
            from . import osx_graphics
            pkg = self.app.paths.qemu_osx_package
            gl = osx_graphics.host_gl() or {}
        avail = self.availability(m)
        display = self._display()
        ati = mac_screen(display, settings, self.exact()) if display else None
        return {"capable": self.capable(), "requested": requested(settings),
                "family": m.family if m is not None else None,
                "effective": effective(settings, m, pkg), "running": self.running if self.app.supervisor.active
                else None, "guest_software": guest_software(m),
                "helper": (m.meta.get("graphics") or {}).get("helper") if m else None,
                "reason": why_not(settings, m, pkg), "available": avail["available"],
                "unavailable_reason": avail["reason"], "unavailable_message": avail["message"],
                "mac_screen": self.current_screen(), "ati_screen": list(ati) if ati else None,
                "startup_screen": (list(self.start_screen()) if self.start_screen() else None)
                                  if getattr(m, "osx", False) else list(STARTUP_SIZE),
                "experimental": bool(getattr(m, "osx", False)),
                "host_gl": {"ok": bool(gl.get("ok")), "reason": gl.get("reason"),
                            "renderer": gl.get("renderer"), "software": gl.get("software"),
                            "render_nodes": len(gl.get("render_nodes") or []),
                            "render_node": gl.get("render_node"),
                            "checked": (osx_graphics.host_gl() if getattr(m, "osx", False)
                                        else host_gl()) is not None}}

    def notices(self) -> list[dict]:
        """The card was asked for but Mac OS gets the standard one: say why."""
        settings = self.app.settings.applied
        m = self.app.supervisor.active_machine()
        pkg = self.app.paths.qemu_osx_package if getattr(m, "osx", False) else self.app.paths.qemu_package
        why = why_not(settings, m, pkg)
        from . import osx_graphics
        gl = osx_graphics.host_gl() if getattr(m, "osx", False) else host_gl()
        if why in (None, "family") or (why == "host-gl" and gl is None):
            return []                # a 68k Mac never has the card: nothing to tell
        return [{"id": "graphics-fallback", "level": "warning",
                 "message": f"Mac OS uses the standard graphics card because {REASONS[why]}."}]

    def check_disk(self, machine) -> dict | None:
        """Read-only: ATI's software and RetroBoot Display on the (stopped) disk."""
        mod = self._ati_module()
        if mod is not None:
            st = mod.status(str(machine.disk))
            return {"ati_software": "present" if st["items"].get(mod.DRIVER) == "enabled" else "absent",
                    "helper": bool(st["helper"])}
        check = getattr(self.app.guestprep, "ati_software_present", None)
        present = check(machine.disk) if check else None
        return None if present is None else {"ati_software": "present" if present else "absent"}

    async def prepare_launch(self, machine) -> None:
        """supervisor.prepare_disk hook (guest stopped): look for ATI's software on the
        disk and remember the answer (also when the card is not requested, so the
        panel and the web know whether to offer it); then make the presenter's and
        GRUB's startup geometry match the card this launch will use."""
        if machine is None:
            return
        if requested(self.app.settings.applied) == ATI:
            await self.wait_probe()          # the first launch may come before the probe ends
            await self.reprobe_if_changed()
        if getattr(machine, "osx", False):
            # Original native ATI drivers belong to OS X. Never run OS9 tools
            # or look for the classic Extensions folder on this disk.
            await self.sync_startup(machine)
            return
        try:
            seen = await asyncio.to_thread(self.check_disk, machine)
        except Exception as exc:          # never block a launch on the check
            log.warning("ATI software check failed: %s", exc)
            seen = None
        if seen is not None:
            old = machine.meta.get("graphics") or {}
            if any(old.get(k) != v for k, v in seen.items()):
                machine.update_meta(graphics={**old, **seen})
                self.app.lifecycle.event("graphics_check", machine=machine.id, **seen)
        await self.sync_startup(machine)

    async def sync_startup(self, machine=None) -> None:
        """Record the next launch's card (state hint for rb-bootdisplay) and push the
        startup geometry to the presenter and GRUB when it changed."""
        card = self.next_card()
        m = machine or self.app.supervisor.active_machine()
        native = getattr(m, "osx", False)
        hint = {"card": card, "machine": m.id if m else None,
                "startup_guest_size": "%dx%d" % STARTUP_SIZE if card == ATI and not native else None}
        path = Path(self.app.paths.state) / HINT_FILE
        old = read_hint(self.app.paths.state)
        # no hint means the standard card to rb-bootdisplay: write only when that changes
        if old.get("card", STANDARD) != card or (card == ATI and old != hint):
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(hint) + "\n")
                os.replace(tmp, path)
            except OSError as exc:
                log.warning("graphics hint not written: %s", exc)
            await self.app.apply_engine.update_boot_display(why="display.graphics")
        startup = tuple(STARTUP_SIZE) if card == ATI and not native else None
        if startup != self._pushed_startup:
            self._pushed_startup = startup
            # when Mac OS draws its Happy Mac (startup_config happy_mac), then the geometry
            if getattr(self.app, "startup", None) is not None:
                await self.app.startup.push_config()
            await self.app.apply_engine.push_display()

    def launched(self, card: str) -> None:
        self.running = card

    # --- installing ATI's software (rbguestprep.ati) ---------------------------------
    def _ati_module(self):
        gp = self.app.guestprep
        if getattr(gp, "module", None) is None or gp.module() is None:
            return None
        try:
            apps = Path(self.app.paths.apps_lib)
            if apps.is_dir() and str(apps) not in sys.path:
                sys.path.insert(0, str(apps))
            import importlib
            return importlib.import_module("rbguestprep.ati")
        except Exception as exc:
            log.debug("rbguestprep.ati unavailable: %s", exc)
            return None

    def install_available(self) -> tuple[bool, str | None]:
        ok, why = self.app.guestprep.available()
        if not ok:
            return False, why or "RetroBoot's disk tools are not available."
        if self._ati_module() is None:
            return False, "RetroBoot's ATI installer is not available on this computer."
        return True, None

    def install_job(self, cd_name: str, restart: bool, origin: str = "web"):
        """Install ATI's software (and RetroBoot Display) from the Mac OS 9.2.1 CD image
        in the media library into the active Mac's disk.  Mac OS must be shut down;
        with ``restart`` the job shuts it down cleanly first and starts it again
        afterwards.  The disk is replaced only after the copy verifies."""
        from .jobs import JobFailed
        from .util import run as run_cmd

        async def body(job):
            app = self.app
            sup = app.supervisor
            machine = sup.active_machine()
            if machine is None:
                raise JobFailed("No Mac OS machine is installed.", None, "not_found")
            if machine.osx:
                raise JobFailed("Mac OS X uses its own graphics drivers. The Mac OS 9 ATI installer cannot be used.",
                                None, "unsupported")
            cd = app.media.path(cd_name)
            if cd is None:
                raise JobFailed("That disc image isn't in the media library.", None, "not_found")
            ok, why = self.install_available()
            if not ok:
                raise JobFailed(why, None, "capability-missing")
            mod = self._ati_module()
            was_running = sup.active and sup.machine is not None and sup.machine.id == machine.id
            if was_running:
                if not restart:
                    raise JobFailed("Shut down Mac OS first.", None, "guest_running")
                job.update(0.05, "Shutting down Mac OS…")
                await sup.ensure_stopped_clean(job)
            disk = Path(machine.disk)
            copy = disk.with_name(f".{disk.name}.ati")
            try:
                job.update(0.2, "Copying Mac OS's disk…")
                copy.unlink(missing_ok=True)
                rc, _, err = await run_cmd(["cp", "--reflink=auto", "--sparse=always", str(disk), str(copy)],
                                           timeout=3600)
                if rc != 0:
                    raise JobFailed("Mac OS's disk could not be copied: " + err.strip()[:200], None, "copy-failed")
                job.update(0.45, "Installing ATI's graphics software from the CD…")
                try:
                    report = await asyncio.to_thread(mod.install_set, str(copy), cd=str(cd))
                except mod.ATIError as exc:
                    app.lifecycle.event("graphics_install_failed", machine=machine.id, code=exc.code, origin=origin)
                    raise JobFailed(str(exc), None, exc.code)
                except Exception as exc:
                    app.lifecycle.event("graphics_install_failed", machine=machine.id, code="error", origin=origin)
                    raise JobFailed(f"ATI's software could not be installed: {str(exc)[:200]}", None, "install-failed")
                st = disk.stat()
                os.chmod(copy, st.st_mode & 0o7777)
                try:
                    os.chown(copy, st.st_uid, st.st_gid)
                except PermissionError:
                    pass
                os.replace(copy, disk)
                status = report.get("status") or {}
                machine.update_meta(graphics={**(machine.meta.get("graphics") or {}), "ati_software": "present",
                                              "helper": bool(status.get("helper"))})
                app.lifecycle.event("graphics_installed", machine=machine.id, items=report.get("items"),
                                    helper=bool(status.get("helper")), origin=origin)
                job.outcome = "ATI's graphics software is installed. Turn on 3D acceleration in Display settings."
                result = {"machine": machine.id, "items": report.get("items"), "helper": bool(status.get("helper"))}
            finally:
                copy.unlink(missing_ok=True)
                if was_running:
                    job.update(0.9, "Starting Mac OS again…")
                    await sup.start_guest("user")
            if was_running:
                await sup.wait_started(job, app.cfg.guest_content_timeout * 2 + 30)
            return result
        return self.app.jobs.start("graphics.install", body, resources=("guest",), params={"cd": cd_name})
