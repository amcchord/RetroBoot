#!/usr/bin/env python3
"""Fake qemu-system-ppc (and qemu-system-m68k: helpers.make_m68k_package) for fast lifecycle tests (stdlib only).

Understands the argv hostd builds: two ``-qmp unix:PATH,server=on,wait=off``
servers, ``-display retroboot,socket=PATH`` (connects, sends HELLO + SURFACE
with a memfd + DAMAGE like the real backend), ``-serial file:PATH``.

Guest model:
* after ``boot_s`` the surface turns Mac OS startup gray (genuine content);
* after ``finder_s`` screendumps show a Finder-like menu bar;
* ADB ``power`` opens the shut-down dialog (a white box in screendumps);
  ``ret`` then emits SHUTDOWN guest-shutdown, ``r`` emits SHUTDOWN guest-reset;
* SIGTERM -> SHUTDOWN host-signal; QMP ``quit`` -> SHUTDOWN host-qmp-quit.

Behavior comes from ``<package>/fake-qemu.json`` (the launcher passes a clean
environment): ``{"modes": ["crash", "normal"], "boot_s": 0.5, "finder_s": 1.0}``;
``modes`` are consumed one per launch (the last one repeats).  Modes:
``normal``, ``crash`` (exit 3 after 0.3 s), ``nocontent`` (stays black),
``nocontent-write`` (stays black after writing to its ``-drive file=`` disk),
``hang`` (ignores the power key), ``selfreset:<s>`` / ``selfshutdown:<s>``
(Mac OS Restart / Shut Down chosen inside the guest after s seconds),
``slowdialog:<s>`` (a busy Mac: the shut-down dialog appears s seconds after
the power key). As measured on Mac OS 9.2.1 (r3 package), a second power key
closes the dialog (or cancels it before it appears), so a following Return/R
reaches the Finder and nothing shuts down; during the shutdown it is ignored.
The dialog is drawn like Mac OS 9's (a framed light-gray box with text and four
push buttons; the menu titles dim), so hostd's powerdlg recognizes it.
Mac OS's startup after the Finder appears (hostd/reports/powerdlg-2026-10-12.md):
``finder_busy_s`` keeps it busy that long (a thread named like QEMU's "ALL
CPUs/TCG" spins, as do the boot and a pending slow dialog); a power key then
waits in the event queue and its dialog appears when Mac OS goes idle, while a
Return/R sent before the dialog is drawn reaches the Finder.  ``busy_until`` (a
path) keeps it busy after the Finder until that file exists, so a test lets Mac OS
go idle when it chooses rather than at a time a loaded host may miss.  ``drop_power_s``
drops power keys that long after the Finder; ``alert_s`` puts up an application's
alert that long after the Finder (power keys then wait behind it); ``saver_s`` starts a screen
saver (After Dark's Flying Toasters) that long after the Finder: it animates the whole screen and
keeps Mac OS busy, a power key opens the dialog underneath it (covered), and only a pointer
that stays moved for 0.2 s ends it (QMP ``rel`` events; After Dark polls the pointer, so a move
and its return within 50 ms do not, as in the lab); ``icons_s`` draws desktop icons in
the middle of the screen that long after the Finder; ``switch_s`` (with
``switch_size`` "WxH") changes the screen size then, like RetroBoot Display.
``theme: "aqua"`` draws the dialog the way the Aqua Theme Beta Appearance theme
does (a light-gray frame, a white pinstriped body, no dark button outlines);
``bar: "gamma"`` a purple menu bar with light titles (as with the Gamma theme: not
Platinum, recognized by the Finder's menu-bar layout); ``bar: "unknown"`` the same
colours with titles in no Finder layout (a theme hostd does not recognize).
PMU power bridge (``-global via-pmu.battery=on`` with ``-chardev
socket,...,path=P`` and ``-global via-pmu.chardev=ID``): connects to P like
QEMU's PMU (hello with ``sleeping``), logs every host message, and models
Mac OS's sleep: ``"sleep_s": N`` puts it to sleep N s after the Finder (a
``sleep`` message); asleep, the first key wakes it and is swallowed (QEMU's
wake input handler), and a host ``wake`` wakes it (``wake`` reason host).
S in the power dialog is its Sleep button (Mac OS 9.2.2, lab: lidsleep_lab.py):
the dialog closes and Mac OS sleeps ``s_sleep_s`` later (0.4); ``no_sleep_button``
draws three buttons and S does nothing.  ``dialog_busy`` keeps Mac OS busy while
the dialog is up (its modal loop spins, as measured on the Gemini PDA);
``alert_busy`` and ``alert_dims`` do the same and dim the menu titles while the
``alert_s`` alert is up (a modal alert).
``shutdown_delay_s`` delays the SHUTDOWN after Return (an application asking to save first);
``dialog_user`` ("restart" or "shutdown") has the user answer the power dialog
``dialog_user_s`` after it appears, before hostd does.
Display: POINTER_ABS from the presenter makes the guest redraw its cursor
(a small DAMAGE) unless QMP ``stop`` paused it (then the redraw happens at
``cont``); ``"clock_s": N`` adds a menu-bar clock DAMAGE every N running
seconds.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import struct
import sys
import threading
import time
import zlib
from pathlib import Path

ARGV0 = Path(os.path.realpath(sys.argv[0]))
PKG = ARGV0.parent.parent
CONF = PKG / "fake-qemu.json"
STATE = PKG / "fake-qemu.state"


def load_mode() -> dict:
    try:
        conf = json.loads(CONF.read_text())
    except (OSError, ValueError):
        conf = {}
    modes = conf.get("modes") or ["normal"]
    try:
        n = int(STATE.read_text())
    except (OSError, ValueError):
        n = 0
    STATE.write_text(str(n + 1))
    conf["mode"] = modes[min(n, len(modes) - 1)]
    conf["launch"] = n + 1
    return conf


def parse(argv: list[str]) -> dict:
    out = {"qmp": [], "display": None, "serial": None, "width": 1024, "height": 768, "args": argv,
           "disk": None, "chardevs": {}, "pmu_chardev": None, "battery": False}
    i = 0
    while i < len(argv):
        a = argv[i]
        v = argv[i + 1] if i + 1 < len(argv) else ""
        if a == "-qmp" and v.startswith("unix:"):
            out["qmp"].append(v[5:].split(",")[0])
        elif a == "-display" and v.startswith("retroboot,"):
            for part in v.split(",")[1:]:
                if part.startswith("socket="):
                    out["display"] = part[7:]
        elif a == "-serial" and v.startswith("file:"):
            out["serial"] = v[5:]
        elif a == "-drive" and v.startswith("file=") and out["disk"] is None and ",if=mtd" not in v:
            out["disk"] = v[5:].split(",")[0]       # the q800's PRAM (if=mtd) is not the disk
        elif a == "-g":
            w, h, _ = v.split("x")
            out["width"], out["height"] = int(w), int(h)
        elif a == "-device" and v.startswith("nubus-qfb,"):     # a 68k Mac's screen (quadra.py)
            opts = dict(p.split("=", 1) for p in v.split(",")[1:] if "=" in p)
            out["width"], out["height"] = int(opts["width"]), int(opts["height"])
        elif a == "-chardev" and v.startswith("socket,"):
            opts = dict(p.split("=", 1) for p in v.split(",")[1:] if "=" in p)
            out["chardevs"][opts.get("id")] = opts.get("path")
        elif a == "-global" and v.startswith("via-pmu.chardev="):
            out["pmu_chardev"] = v.split("=", 1)[1]
        elif a == "-global" and v == "via-pmu.battery=on":
            out["battery"] = True
        i += 1
    return out


def _rect(px: bytearray, w: int, x: int, y: int, rw: int, rh: int, color: bytes) -> None:
    for yy in range(y, y + rh):
        o = (yy * w + x) * 3
        px[o:o + rw * 3] = color * rw


def _dialog(px: bytearray, w: int, h: int, buttons: int = 4, y0: int | None = None,
            theme: str = "platinum") -> None:
    """Mac OS 9's power-key dialog, roughly: 402 x 104 with a 1-pixel black frame and a
    shadow, Platinum gray inside, two lines of text and four push buttons."""
    bw, bh = 402, 102
    x0, y0 = max(0, (w - bw) // 2), max(24, h // 5) if y0 is None else y0
    if theme == "aqua":
        _rect(px, w, x0, y0, bw, bh, b"\xad\xad\xad")
        for yy in range(y0 + 1, y0 + bh - 1):
            _rect(px, w, x0 + 1, yy, bw - 2, 1, b"\xff\xff\xff" if yy % 2 else b"\xf7\xf7\xf7")
        for line in range(2):
            for i in range(30):
                _rect(px, w, x0 + 84 + i * 9, y0 + 18 + line * 16, 5, 9, b"\x00\x00\x00")
        for i in range(4):
            bx, by = x0 + 24 + i * 94, y0 + 62
            _rect(px, w, bx, by, 80, 20, b"\x9a\x9a\x9a")
            _rect(px, w, bx + 1, by + 1, 78, 18, b"\xe0\xe0\xe8")
        return
    _rect(px, w, x0, y0, bw, bh, b"\x00\x00\x00")
    _rect(px, w, x0 + 1, y0 + 1, bw - 2, bh - 2, b"\xdd\xdd\xdd")
    _rect(px, w, x0 + 2, y0 + bh, bw, 2, b"\x00\x00\x00")          # shadow
    _rect(px, w, x0 + bw, y0 + 2, 2, bh, b"\x00\x00\x00")
    for line in range(2):                                          # text
        for i in range(30):
            _rect(px, w, x0 + 84 + i * 9, y0 + 18 + line * 16, 5, 9, b"\x00\x00\x00")
    for i in range(4 - buttons, 4):                                # Restart, Sleep, Cancel, Shut Down
        bx, by = x0 + 24 + i * 94, y0 + 62
        _rect(px, w, bx, by, 80, 20, b"\x00\x00\x00")
        _rect(px, w, bx + 1, by + 1, 78, 18, b"\xee\xee\xee")



# Where Mac OS 9 draws the Finder's menu titles (lab frames at 1024x768): the Apple menu,
# File, Edit, View, Window, Special, Help; the application menu ends near the right edge.
FINDER_MENUS = [(14, 29), (40, 62), (76, 100), (113, 140), (154, 200), (213, 255), (270, 296)]

class Guest:
    def __init__(self, conf: dict, args: dict):
        self.conf = conf
        # Mac OS 7.5.5/8.1 on the q800 (qemu-system-m68k): the power dialog has Restart, Cancel
        # and Shut Down, and R does not restart (it closes the dialog; measured, quadra-hostd).
        self.m68k = ARGV0.name == "qemu-system-m68k" or bool(conf.get("m68k"))
        self.mode = conf["mode"]
        self.args = args
        self.t0 = time.monotonic()
        self.boot_s = float(conf.get("boot_s", 0.5))
        self.finder_s = float(conf.get("finder_s", 1.0))
        self.dialog = False
        self.dialog_pending = False
        self.shutting = False
        self.paused = False
        self.paused_at = 0.0
        self.paused_total = 0.0
        self.clients: list[asyncio.StreamWriter] = []
        self.exit_code: int | None = None
        self.done = asyncio.Event()
        self.keys: list[str] = []
        self.w, self.h = args["width"], args["height"]
        self.mem = None
        self.dsock = None
        self.asleep = False
        self.bridge: asyncio.StreamWriter | None = None
        self.bridge_log: list = []
        self.finder_busy_s = float(conf.get("finder_busy_s") or 0)
        self.busy_until = conf.get("busy_until")
        self.drop_power_s = float(conf.get("drop_power_s") or 0)
        self.icons_s = conf.get("icons_s")
        self.saver_s = conf.get("saver_s")
        self.saver_ended = False
        self.pointer_events = 0
        self.ptr = (0, 0)                # relative pointer offset from where it started
        self.ptr_moved_at = None         # when it left that spot while the saver ran
        self.alert_s = conf.get("alert_s")
        self.switch_s = conf.get("switch_s")
        self.switch_size = tuple(int(v) for v in str(conf.get("switch_size") or "800x600").split("x"))
        threading.Thread(target=self._tcg, name="tcg", daemon=True).start()

    def _tcg(self) -> None:
        """Like QEMU's TCG thread: it runs while Mac OS is busy and naps while it idles."""
        try:
            import ctypes
            ctypes.CDLL(None).prctl(15, b"ALL CPUs/TCG", 0, 0, 0)    # PR_SET_NAME
        except (OSError, AttributeError):
            pass
        while True:
            if self.busy() and not self.paused:
                end = time.monotonic() + 0.02
                while time.monotonic() < end:
                    pass
                time.sleep(0.0005)
            else:
                time.sleep(0.05)

    def since_finder(self) -> float | None:
        if self.phase() != "finder":
            return None
        return self.elapsed() - self.finder_s

    def alert_up(self) -> bool:
        sf = self.since_finder()
        return self.alert_s is not None and sf is not None and sf >= float(self.alert_s)

    def saver(self) -> bool:
        """The screen saver runs (saver_s after the Finder, until the pointer stays moved)."""
        if self.ptr_moved_at is not None and time.monotonic() - self.ptr_moved_at >= 0.2:
            self.saver_ended = True
        sf = self.since_finder()
        return self.saver_s is not None and sf is not None and sf >= float(self.saver_s) and \
            not self.saver_ended and not self.asleep and not self.shutting

    def held(self) -> bool:
        """busy_until: still busy until the test creates that file."""
        return bool(self.busy_until) and not os.path.exists(self.busy_until)

    def busy(self) -> bool:
        ph = self.phase()
        if ph == "gray":
            return True
        if self.asleep:
            return False
        sf = self.since_finder()
        return (sf is not None and (sf < self.finder_busy_s or self.held())) or self.saver() or \
            (self.dialog_pending and not self.alert_up()) or \
            (self.dialog and bool(self.conf.get("dialog_busy"))) or \
            (self.alert_up() and bool(self.conf.get("alert_busy")))

    def elapsed(self) -> float:
        now = time.monotonic()
        paused = self.paused_total + ((now - self.paused_at) if self.paused else 0)
        return now - self.t0 - paused

    def phase(self) -> str:
        if self.mode in ("nocontent", "nocontent-write"):
            return "black"
        e = self.elapsed()
        if e < self.boot_s:
            return "black"
        if e < self.finder_s:
            return "gray"
        return "finder"

    def size(self) -> tuple[int, int]:
        sf = self.since_finder()
        if self.switch_s is not None and sf is not None and sf >= float(self.switch_s):
            return self.switch_size
        return self.w, self.h

    def frame(self) -> bytes:
        w, h = self.size()
        if self.saver():                 # flying toasters over black: a different frame each time
            px = bytearray(w * h * 3)
            x = int(self.elapsed() * 400) % max(1, w - 40)
            _rect(px, w, x, h // 3, 40, 30, b"\xc0\xc0\xc0")
            return bytes(px)
        ph = self.phase()
        if ph == "black":
            px = bytearray(w * h * 3)
        elif ph == "gray":
            px = bytearray(b"\x99\x99\x99" * (w * h))
        else:
            px = bytearray(b"\x66\x66\xcc" * (w * h))  # blue desktop
            bar = self.conf.get("bar")
            dark = bar in ("gamma", "unknown")          # a theme's purple menu bar with light titles
            px[0: w * 20 * 3] = (b"\x60\x20\x90" if dark else b"\xff\xff\xff") * (w * 20)  # menu bar
            dim = self.dialog or (self.alert_up() and bool(self.conf.get("alert_dims")))
            title = (b"\x90\x80\xa0" if dim else b"\xf0\xf0\xf0") if dark else \
                (b"\x99\x99\x99" if dim else b"\x00\x00\x00")   # dimmed under a dialog
            if bar == "unknown":     # titles without the Finder's layout (no Apple or application menu)
                spans, on = [(60, min(w, 400))], (lambda x, x0: x % 9 < 5)
            else:                    # the Apple menu, the Finder's six titles, the application menu
                spans, on = FINDER_MENUS + [(w - 78, w - 18)], (lambda x, x0: (x - x0) % 3 != 2)
            for x0, x1 in spans:
                for x in range(x0, min(w, x1)):
                    if on(x, x0):
                        for y in range(5, 15):
                            o = (y * w + x) * 3
                            px[o:o + 3] = title
            sf = self.since_finder()
            if self.icons_s is not None and sf is not None and sf >= float(self.icons_s):
                for i in range(6):       # a row of desktop icons across the middle
                    _rect(px, w, w // 4 + i * 60, h // 2 - 20, 32, 32, b"\x20\x80\x20")
        if self.alert_up():
            _dialog(px, w, h, buttons=1, y0=h // 3)       # "OK" alert of an application
        if self.dialog:
            _dialog(px, w, h, buttons=3 if self.conf.get("no_sleep_button") or self.m68k else 4,
                    theme=self.conf.get("theme") or "platinum")
        return bytes(px)

    # -- QMP -------------------------------------------------------------------------
    async def qmp_client(self, reader, writer):
        writer.write((json.dumps({"QMP": {"version": {"qemu": {"major": 11, "minor": 0, "micro": 2}},
                                          "capabilities": []}}) + "\n").encode())
        await writer.drain()
        negotiated = False
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                req = json.loads(line)
                cmd = req.get("execute")
                rid = req.get("id")
                args = req.get("arguments") or {}
                ret, err = await self.execute(cmd, args, negotiated)
                if cmd == "qmp_capabilities":
                    negotiated = True
                    self.clients.append(writer)
                resp = {"return": ret} if err is None else {"error": {"class": "GenericError", "desc": err}}
                if rid is not None:
                    resp["id"] = rid
                writer.write((json.dumps(resp) + "\n").encode())
                await writer.drain()
        except (ConnectionError, ValueError):
            pass
        finally:
            if writer in self.clients:
                self.clients.remove(writer)

    def event(self, name: str, data: dict) -> None:
        ts = time.time()
        msg = json.dumps({"event": name, "data": data,
                          "timestamp": {"seconds": int(ts), "microseconds": int((ts % 1) * 1e6)}}) + "\n"
        for w in list(self.clients):
            try:
                w.write(msg.encode())
            except Exception:
                pass

    async def shutdown(self, reason: str, guest: bool) -> None:
        if self.exit_code is not None or getattr(self, "_stopping", False):
            return
        self._stopping = True
        self.event("SHUTDOWN", {"guest": guest, "reason": reason})
        for w in list(self.clients):
            try:
                await w.drain()
            except Exception:
                pass
        await asyncio.sleep(0.05)
        self.exit_code = 0
        self.done.set()

    async def execute(self, cmd, args, negotiated):
        if cmd == "qmp_capabilities":
            return {}, None
        if not negotiated:
            return None, "capabilities negotiation required"
        if cmd == "query-status":
            return {"status": "paused" if self.paused else "running", "running": not self.paused}, None
        if cmd == "stop":
            if not self.paused:
                self.paused = True
                self.paused_at = time.monotonic()
                self.event("STOP", {})
            return {}, None
        if cmd == "cont":
            if self.paused:
                self.paused = False
                self.paused_total += time.monotonic() - self.paused_at
                self.event("RESUME", {})
            return {}, None
        if cmd == "quit":
            asyncio.get_running_loop().create_task(self.shutdown("host-qmp-quit", False))
            return {}, None
        if cmd in ("send-key", "input-send-event"):
            moves = [e["data"] for e in args.get("events", []) if e.get("type") == "rel"]
            if cmd == "input-send-event" and moves:
                self.pointer_events += 1
                x, y = self.ptr
                for m in moves:
                    x, y = (x + m["value"], y) if m["axis"] == "x" else (x, y + m["value"])
                self.ptr = (x, y)
                if self.saver():                     # After Dark sees the pointer when it polls
                    if self.ptr != (0, 0) and self.ptr_moved_at is None:
                        self.ptr_moved_at = time.monotonic()
                    elif self.ptr == (0, 0):
                        self.ptr_moved_at = None
            if cmd == "send-key":
                keys = [k["data"] for k in args.get("keys", [])]
            else:
                keys = [e["data"]["key"]["data"] for e in args.get("events", [])
                        if e.get("type") == "key" and e["data"].get("down")]
            for k in keys:
                self.on_key(k)
            return {}, None
        if cmd == "screendump":
            path = args["filename"]
            fmt = args.get("format", "ppm")
            rgb = self.frame()
            fw, fh = self.size()
            if fmt == "png":
                raw = b"".join(b"\x00" + rgb[y * fw * 3:(y + 1) * fw * 3] for y in range(fh))

                def chunk(tag, payload):
                    return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload))
                data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", fw, fh, 8, 2, 0, 0, 0))
                        + chunk(b"IDAT", zlib.compress(raw, 1)) + chunk(b"IEND", b""))
            else:
                data = f"P6\n{fw} {fh}\n255\n".encode() + rgb
            with open(path, "wb") as f:
                f.write(data)
            return {}, None
        if cmd == "human-monitor-command":
            line = args.get("command-line", "")
            if line.startswith("classicmac-boot-complete"):
                return "classicmac clock: real-time\r\n", None
            return "", None
        return None, f"The command {cmd} has not been found"

    def on_key(self, k: str) -> None:
        if self.asleep:
            self.keys.append(f"wake:{k}")        # wakes Mac OS; it never sees the key
            self.wake("key")
            return
        self.keys.append(k)
        if self.mode == "hang" or self.paused:
            return
        if k == "power" and self.shutting:
            return
        if k == "power" and (self.dialog or self.dialog_pending):
            self.dialog = self.dialog_pending = False    # a second press closes the dialog
        elif k == "power" and self.phase() == "finder":
            sf = self.since_finder() or 0.0
            delay = None
            if self.alert_up():
                self.dialog_pending = True          # waits behind the alert (never shown here)
                return
            if sf < self.drop_power_s:
                self.keys[-1] = "power-dropped"
                return
            if self.mode.startswith("slowdialog:"):
                delay = float(self.mode.split(":")[1])
            elif sf < self.finder_busy_s or self.held():
                delay = max(0.0, self.finder_busy_s - sf) + 0.3    # queued until Mac OS goes idle
            if delay is not None:
                self.dialog_pending = True

                def show():
                    if self.dialog_pending and self.held():
                        asyncio.get_running_loop().call_later(0.3, show)
                    elif self.dialog_pending:
                        self.dialog_pending = False
                        self.dialog = True
                        self.dialog_shown()
                asyncio.get_running_loop().call_later(delay, show)
            else:
                self.dialog = True
                self.dialog_shown()
        elif k in ("ret", "kp_enter") and self.dialog:
            self.shutting = True
            delay = float(self.conf.get("shutdown_delay_s", 0))
            if delay > 0:       # an application asks to save first: Mac OS finishes later
                self.keys.append("shutdown-delayed")
                asyncio.get_running_loop().call_later(
                    delay, lambda: asyncio.get_running_loop().create_task(self.shutdown("guest-shutdown", True)))
            else:
                asyncio.get_running_loop().create_task(self.shutdown("guest-shutdown", True))
        elif k == "r" and self.dialog and self.m68k:
            self.dialog = False                         # 7.5.5/8.1: R closes the dialog
        elif k == "r" and self.dialog:
            self.shutting = True
            asyncio.get_running_loop().create_task(self.shutdown("guest-reset", True))
        elif k == "esc" and self.dialog:
            self.dialog = False
        elif k == "s" and self.dialog and not self.conf.get("no_sleep_button") and not self.m68k:
            self.dialog = False                         # Sleep
            asyncio.get_running_loop().call_later(float(self.conf.get("s_sleep_s", 0.4)), self.sleep, False)

    def dialog_shown(self) -> None:
        """``dialog_user``: the user answers the power dialog hostd opened, ``dialog_user_s``
        after it appears (before hostd's own answer): "restart" or "shutdown"."""
        what = self.conf.get("dialog_user")
        if what not in ("restart", "shutdown"):
            return

        def answer():
            if self.dialog and not self.shutting:
                self.shutting = True
                self.keys.append(f"user-{what}")
                asyncio.get_running_loop().create_task(
                    self.shutdown("guest-reset" if what == "restart" else "guest-shutdown", True))
        asyncio.get_running_loop().call_later(float(self.conf.get("dialog_user_s", 0.1)), answer)

    # -- PMU power bridge ---------------------------------------------------------------
    def bridge_send(self, obj: dict) -> None:
        if self.bridge is not None:
            try:
                self.bridge.write((json.dumps(obj) + "\n").encode())
            except Exception:
                pass

    def sleep(self, low_battery: bool = True) -> None:
        if not self.asleep and not self.shutting:
            self.asleep = True
            self.keys.append("sleep")
            self.bridge_send({"type": "sleep", "low_battery": low_battery})

    def wake(self, reason: str) -> None:
        if self.asleep:
            self.asleep = False
            self.keys.append(f"woke:{reason}")
            self.bridge_send({"type": "wake", "reason": reason})

    async def bridge_loop(self) -> None:
        path = self.args["chardevs"].get(self.args["pmu_chardev"])
        if not path or not self.args["battery"]:
            return
        while not self.done.is_set():
            try:
                r, w = await asyncio.open_unix_connection(path)
            except OSError:
                await asyncio.sleep(0.2)           # reconnect-ms
                continue
            self.bridge = w
            self.bridge_send({"type": "hello", "version": 1, "battery": True, "sleeping": self.asleep,
                              "backlight": False})
            try:
                while True:
                    line = await r.readline()
                    if not line:
                        break
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    self.bridge_log.append(msg)
                    if msg.get("type") == "wake":
                        self.wake("host")
            except ConnectionError:
                pass
            self.bridge = None
            await asyncio.sleep(0.2)

    async def sleep_later(self) -> None:
        secs = self.conf.get("sleep_s")
        if secs is None:
            return
        while self.phase() != "finder":
            await asyncio.sleep(0.05)
        await asyncio.sleep(float(secs))
        self.sleep()

    # -- display ------------------------------------------------------------------------
    async def display_loop(self) -> None:
        path = self.args["display"]
        if not path:
            return
        s = None
        for _ in range(100):
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
                s.connect(path)
                break
            except OSError:
                s.close()
                s = None
                await asyncio.sleep(0.1)
        if s is None:
            return
        self.dsock = s
        session = os.environ.get("RETROBOOT_SESSION", "").encode()[:39]
        s.send(struct.pack("=II", 1, 48) + struct.pack("=II", 1, os.getpid()) + session.ljust(40, b"\0"))
        stride = self.w * 4
        size = stride * self.h
        fd = os.memfd_create("fake-surface")
        os.ftruncate(fd, size)
        import mmap
        self.mem = mmap.mmap(fd, size)
        s.sendmsg([struct.pack("=II", 2, 24) + struct.pack("=IIIIQ", self.w, self.h, stride, 0x34325258, size)],
                  [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("=i", fd))])
        os.close(fd)
        s.setblocking(False)
        seq = 0
        last = None
        cursor_pending = False
        clock_s = float(self.conf.get("clock_s") or 0)
        last_clock = 0.0

        def damage(x, y, w, h) -> bool:
            nonlocal seq
            seq += 1
            try:
                s.send(struct.pack("=II", 3, 24) + struct.pack("=IIiiii", seq, 1, x, y, w, h))
                return True
            except BlockingIOError:
                return True
            except OSError:
                return False

        while not self.done.is_set():
            while True:  # input from the presenter
                try:
                    msg = s.recv(64)
                except BlockingIOError:
                    break
                except OSError:
                    return
                if not msg:
                    return
                if len(msg) >= 8 and struct.unpack("=I", msg[:4])[0] == 17:  # POINTER_ABS
                    cursor_pending = True
            ph = self.phase()
            if ph != last:
                last = ph
                color = {"black": b"\x00\x00\x00\x00", "gray": b"\x99\x99\x99\x00", "finder": b"\xcc\x66\x66\x00"}[ph]
                self.mem[:] = color * (self.w * self.h)
                if not damage(0, 0, self.w, self.h):
                    return
            if not self.paused:
                if cursor_pending and ph != "black":
                    cursor_pending = False
                    if not damage(0, 0, 16, 16):
                        return
                if clock_s and ph == "finder" and self.elapsed() - last_clock >= clock_s:
                    last_clock = self.elapsed()
                    if not damage(self.w - 80, 0, 60, 20):
                        return
            await asyncio.sleep(0.05)


async def main() -> int:
    conf = load_mode()
    args = parse(sys.argv[1:])
    if args["serial"]:
        with open(args["serial"], "a") as f:
            f.write(f"fake qemu launch {conf['launch']} mode {conf['mode']}\n")
    log_path = PKG / "fake-qemu.log"
    with open(log_path, "a") as f:
        f.write(json.dumps({"launch": conf["launch"], "mode": conf["mode"], "argv": sys.argv[1:],
                            "tz": os.environ.get("TZ"), "session": os.environ.get("RETROBOOT_SESSION"),
                            "egl_render_node": os.environ.get("RETROBOOT_EGL_RENDER_NODE"),
                            "uid": os.getuid(), "gid": os.getgid(), "groups": os.getgroups(),
                            "umask": oct(os.umask(0o007))}) + "\n")
    g = Guest(conf, args)
    if conf["mode"] == "nocontent-write" and args["disk"]:
        await asyncio.sleep(0.2)
        with open(args["disk"], "r+b") as f:     # the guest writes its disk, then hangs
            f.seek(1024)
            f.write(b"written by the guest")
    servers = []
    for p in args["qmp"]:
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass
        servers.append(await asyncio.start_unix_server(g.qmp_client, path=p))
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, lambda: loop.create_task(g.shutdown("host-signal", False)))
    loop.add_signal_handler(signal.SIGINT, lambda: loop.create_task(g.shutdown("host-signal", False)))
    disp = loop.create_task(g.display_loop())
    bridge = loop.create_task(g.bridge_loop())
    sleeper = loop.create_task(g.sleep_later())
    mode = g.mode
    if mode == "crash":
        await asyncio.sleep(0.3)
        return 3
    if mode.startswith(("selfreset:", "selfshutdown:")):
        kind, secs = mode.split(":")

        async def later():
            await asyncio.sleep(float(secs))
            await g.shutdown("guest-reset" if kind == "selfreset" else "guest-shutdown", True)
        loop.create_task(later())
    await g.done.wait()
    disp.cancel()
    bridge.cancel()
    sleeper.cancel()
    for s in servers:
        s.close()
    with open(log_path, "a") as f:
        f.write(json.dumps({"launch": conf["launch"], "keys": g.keys, "pointer": g.pointer_events,
                            "bridge": [m for m in g.bridge_log if m.get("type") != "battery"][-20:],
                            "batteries": [m for m in g.bridge_log if m.get("type") == "battery"][-5:]}) + "\n")
    return g.exit_code or 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
