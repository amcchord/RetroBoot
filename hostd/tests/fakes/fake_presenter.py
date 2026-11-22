#!/usr/bin/env python3
"""Fake rb-presenter for tests and development runs.

Implements the ARCHITECTURE section 4 control socket (newline JSON) and a
minimal section 3 display socket (SOCK_SEQPACKET; HELLO / SURFACE with a memfd /
DAMAGE / GUEST_EVENT).  It records every request, keeps the current scene and
UI document, applies the section 4.3 genuine-content rule to the shared guest
surface and emits ``guest_content``/``guest_revealed`` like the real presenter.

Library use (tests)::

    fp = FakePresenter(control_path, display_path, display=DISPLAY)
    await fp.start(); fp.emit({"event": "chord", "name": "recovery"})

CLI use (development / E2E)::

    python3 fake_presenter.py --socket /run/retroboot/presenter.sock \
        --display-socket /run/retroboot/display.sock --inject /tmp/inject.sock --log /tmp/presenter.jsonl

The inject socket accepts JSON lines: an event object (``{"event": ...}``) is
emitted to hostd; ``{"click": "<button id>", "values": {...}}`` emits a
``ui_action`` for the current document; ``{"query": "state"}`` returns the
current scene/document/request counts; ``guest_keys``, ``guest_click``,
``guest_pointer`` and ``guest_button`` drive the guest over the display socket.

With ``remote_path`` (``--remote-socket``) it also serves the section 4.6
remote screen socket: a memfd tap buffer of the guest surface (scene guest),
of ``remote_picture`` (tests) or of a checkerboard, REQUEST/UPDATE with
row-band diffs, and it records remote input (``remote.keys`` etc.).
"""
from __future__ import annotations

import argparse
import array
import asyncio
import json
import mmap
import os
import socket
import struct
import sys
import time
import zlib

# QAPI QKeyCode values for QEMU 11.0.2 in enum order (same table as the qemu-display
# workstream's emulator/tools/rbd_presenter_stub.py).
QKEYCODES = (
    'unmapped shift shift_r alt alt_r ctrl ctrl_r menu esc 1 2 3 4 5 6 7 8 9 0 '
    'minus equal backspace tab q w e r t y u i o p bracket_left bracket_right '
    'ret a s d f g h j k l semicolon apostrophe grave_accent backslash z x c v '
    'b n m comma dot slash asterisk spc caps_lock f1 f2 f3 f4 f5 f6 f7 f8 f9 '
    'f10 num_lock scroll_lock kp_divide kp_multiply kp_subtract kp_add '
    'kp_enter kp_decimal sysrq kp_0 kp_1 kp_2 kp_3 kp_4 kp_5 kp_6 kp_7 kp_8 '
    'kp_9 less f11 f12 print home pgup pgdn end left up down right insert '
    'delete stop again props undo front copy open paste find cut lf help '
    'meta_l meta_r compose pause ro hiragana henkan yen muhenkan '
    'katakanahiragana kp_comma kp_equals power sleep wake audionext audioprev'
).split()
QCODE = {name: value for value, name in enumerate(QKEYCODES)}
KEY, POINTER_ABS, BUTTON, RESIZE = 16, 17, 18, 19

DEFAULT_DISPLAY = {"device": "/dev/dri/card0", "driver": "fake-drm", "connector": "Virtual-1",
                   "mode": [1024, 768, 60], "modes": [[1024, 768, 60], [1280, 800, 60], [1920, 1080, 60]],
                   "physical_mm": [300, 225], "suggested_scale": 1, "scale": 1, "pixel_scale": "auto"}


def _suggested(w: int, h: int) -> int:
    s = 1
    while w // (s + 1) >= 720 and h // (s + 1) >= 540:
        s += 1
    return s


def _effective_scale(w: int, h: int, req) -> int:
    """presenter/src/scale.c rb_effective_scale (ARCHITECTURE 4.5), transcribed."""
    if req in (None, "auto", 0):
        return _suggested(w, h)

    def valid(s):
        gw, gh = (w // s // 8) * 8, h // s
        return 640 <= gw <= 3840 and 480 <= gh <= 2160
    for s in list(range(req, 0, -1)) + list(range(req + 1, 5)):
        if valid(s):
            return s
    return _suggested(w, h)


def png_bytes(w: int, h: int, rgb: tuple[int, int, int] = (128, 128, 128)) -> bytes:
    row = b"\x00" + bytes(rgb) * w
    raw = row * h

    def chunk(tag, payload):
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def content_rule(buf, width: int, height: int, stride: int) -> tuple[float, float, bytes]:
    """Section 4.3 statistics on an XRGB8888 buffer: (gray fraction, non-black fraction, sample digest)."""
    n = 64
    hist: dict[int, int] = {}
    nonblack = total = 0
    samples = bytearray()
    for j in range(n + 1):
        y = (j * (height - 1)) // n
        for i in range(n + 1):
            x = (i * (width - 1)) // n
            o = y * stride + x * 4
            b, g, r = buf[o], buf[o + 1], buf[o + 2]
            samples += bytes((r, g, b))
            total += 1
            if max(r, g, b) > 24:
                nonblack += 1
                if max(r, g, b) - min(r, g, b) <= 24 and max(r, g, b) >= 32:
                    lv = (r + g + b) // 3
                    hist[lv] = hist.get(lv, 0) + 1
    best = max((sum(c for l2, c in hist.items() if abs(l2 - l) <= 12) for l in hist), default=0)
    return best / total, nonblack / total, bytes(samples)


GRAY_FRACTION = 0.97      # ARCHITECTURE 4.3: >= 97 % one gray level ...
GRAY_STATIC_S = 0.048     # ... unchanged for three 16 ms refreshes
NONBLACK_FRACTION = 0.25  # or >= 25 % non-black ...
NONBLACK_SETTLE_S = 0.5   # ... settled for 500 ms


class FakePresenter:
    def __init__(self, control_path: str, display_path: str | None = None, display: dict | None = None,
                 log_path: str | None = None, auto_hello_display: bool = True, remote_path: str | None = None):
        self.remote = FakeRemote(self, remote_path) if remote_path else None
        self.remote_picture: tuple[int, int, bytearray] | None = None   # (w, h, BGRX) for tests
        self.control_path = control_path
        self.display_path = display_path
        self.display = dict(display or DEFAULT_DISPLAY)
        self.log_path = log_path
        self.auto_hello_display = auto_hello_display
        self.requests: list[dict] = []
        self.events_sent: list[dict] = []
        self.scene: dict = {"scene": "splash"}
        self.doc: dict | None = None
        self.docs: list[dict] = []
        self.startup: dict = {"enabled": False, "art": None, "value": 0.0}
        self.startup_updates: list[dict] = []
        self.writer: asyncio.StreamWriter | None = None
        self.server = None
        self.client_connected = asyncio.Event()
        self.guest = {"connected": False, "pid": None, "session": "", "surface": None, "content": False}
        self.t0 = time.monotonic()
        self._display_sock: socket.socket | None = None
        self._display_conn: socket.socket | None = None
        self._surface = None
        self._content_ref: bytes | None = None
        self._content_since = 0.0
        self._content_timer: asyncio.TimerHandle | None = None
        self.damage_seq = 0
        self.last_damage = None
        self.last_input_ms: int | None = None   # tests set this to model user input
        self.pointer = [16, 16]
        self.nudges = 0
        self.fail_commands: set[str] = set()
        self.connections = 0

    # -- control socket -----------------------------------------------------------
    async def start(self) -> None:
        for p in (self.control_path, self.display_path):
            if p and os.path.exists(p):
                os.unlink(p)
        self.server = await asyncio.start_unix_server(self._client, path=self.control_path)
        if self.display_path:
            self._start_display()
        if self.remote:
            self.remote.start()

    async def stop(self) -> None:
        loop = asyncio.get_running_loop()
        if self.server:
            self.server.close()
        if self.writer:
            self.writer.close()
            self.writer = None
        for s in (self._display_conn, self._display_sock):
            if s is not None and s.fileno() >= 0:
                try:
                    loop.remove_reader(s.fileno())
                except (ValueError, OSError):
                    pass
                s.close()
        self._display_conn = self._display_sock = None
        if self.remote:
            self.remote.stop()
        if self.server:
            try:
                await asyncio.wait_for(self.server.wait_closed(), 5)
            except asyncio.TimeoutError:
                pass

    def _log(self, rec: dict) -> None:
        if self.log_path:
            with open(self.log_path, "a") as f:
                f.write(json.dumps({"t": round(time.monotonic() - self.t0, 3), **rec}) + "\n")

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.writer:  # a new connection replaces the old one
            self.writer.close()
        self.writer = writer
        self.connections += 1
        self.client_connected.set()
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    req = json.loads(line)
                except ValueError:
                    continue
                self.requests.append(req)
                self._log({"request": req})
                resp = self.handle(req)
                writer.write((json.dumps(resp) + "\n").encode())
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            if self.writer is writer:
                self.writer = None
                self.client_connected.clear()

    def handle(self, req: dict) -> dict:
        cmd = req.get("cmd")
        rid = req.get("id")
        if cmd in self.fail_commands:
            return {"id": rid, "ok": False, "error": "injected failure"}
        if cmd == "hello":
            return {"id": rid, "ok": True, "version": 1, "display": self.display if self.auto_hello_display else None,
                    "scene": self.scene.get("scene"), "guest": self.guest}
        if cmd == "set_scene":
            self.scene = {k: v for k, v in req.items() if k not in ("id", "cmd")}
            if self.remote:
                self.remote.check()
            if self.scene.get("scene") == "guest" and self.guest["content"]:
                self.emit({"event": "guest_revealed", "t_ms": self._ms()})
            return {"id": rid, "ok": True}
        if cmd == "ui_show":
            self.doc = req.get("doc")
            self.docs.append(self.doc)
            return {"id": rid, "ok": True}
        if cmd == "display_config":
            if "pixel_scale" in req:
                ps = req["pixel_scale"]
                if not (ps in ("auto", None) or (type(ps) is int and 0 <= ps <= 4)):
                    return {"id": rid, "ok": False, "error": "pixel_scale must be \"auto\" or 1..4"}
                ps = "auto" if ps in ("auto", None, 0) else ps
                w, h = self.display["mode"][0], self.display["mode"][1]
                before = (self.display.get("scale"), self.display.get("pixel_scale"))
                self.display["pixel_scale"] = ps
                self.display["scale"] = _effective_scale(w, h, ps)
                if (self.display["scale"], ps) != before:
                    self.emit({"event": "display", **self.display})
            return {"id": rid, "ok": True, "display": self.display}
        if cmd in ("input_config", "guest_input", "release_all"):
            return {"id": rid, "ok": True}
        if cmd == "screenshot":
            path = req.get("path")
            w, h = self.display["mode"][0], self.display["mode"][1]
            try:
                data = self.surface_png() if self._surface and self.scene.get("scene") == "guest" else None
                with open(path, "wb") as f:
                    f.write(data or png_bytes(min(w, 320), min(h, 240), (128, 128, 128)))
            except OSError as exc:
                return {"id": rid, "ok": False, "error": str(exc)}
            return {"id": rid, "ok": True, "path": path}
        if cmd == "status":
            guest = dict(self.guest)
            if guest.get("connected"):
                guest["damage_seq"] = self.damage_seq
                guest["last_damage_ms"] = (None if self.last_damage is None else
                                           int((time.monotonic() - self.last_damage) * 1000))
                guest["pointer"] = list(self.pointer)
            return {"id": rid, "ok": True, "scene": self.scene.get("scene"), "display": self.display,
                    "guest": guest, "fps": 60, "frames_presented": 0, "last_input_ms": self.last_input_ms,
                    "uptime_ms": self._ms()}
        if cmd == "startup_config":
            # The startup indicator (4.4.1): recorded, applies from the next startup.
            self.startup.update({k: req[k] for k in ("enabled", "art") if k in req})
            return {"id": rid, "ok": True, "startup": dict(self.startup)}
        if cmd == "startup_progress":
            if req.get("reset"):
                self.startup["value"] = 0.0
            self.startup["remaining_ms"] = req.get("remaining_ms")
            self.startup["phase"] = req.get("phase")
            self.startup_updates.append({k: req.get(k) for k in ("remaining_ms", "phase", "reset")})
            return {"id": rid, "ok": True, "startup": dict(self.startup)}
        if cmd == "pointer_nudge":
            if not self._display_conn or not self._surface:
                return {"id": rid, "ok": False, "error": "no guest surface"}
            x, y = self.pointer
            self.nudges += 1
            self._send_guest(POINTER_ABS, struct.pack("=II", x + 1, y))
            asyncio.get_running_loop().call_later(
                0.12, lambda: self._send_guest(POINTER_ABS, struct.pack("=II", x, y)))
            return {"id": rid, "ok": True, "damage_seq": self.damage_seq, "pointer": [x, y]}
        return {"id": rid, "ok": False, "error": f"unknown command {cmd}"}

    def surface_png(self) -> bytes | None:
        """PNG of the shared guest surface (XRGB8888 -> RGB)."""
        if not self._surface:
            return None
        buf, w, h, stride = self._surface
        rows = []
        for y in range(h):
            row = buf[y * stride: y * stride + w * 4]
            rgb = bytearray(w * 3)
            rgb[0::3] = row[2::4]
            rgb[1::3] = row[1::4]
            rgb[2::3] = row[0::4]
            rows.append(b"\x00" + bytes(rgb))
        raw = b"".join(rows)

        def chunk(tag, payload):
            return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) +
                chunk(b"IDAT", zlib.compress(raw, 3)) + chunk(b"IEND", b""))

    def _send_guest(self, mtype: int, payload: bytes) -> bool:
        if not self._display_conn:
            return False
        self._display_conn.send(struct.pack("=II", mtype, len(payload)) + payload)
        return True

    async def guest_keys(self, names: list[str], hold: float = 0.12) -> bool:
        """Tap keys like a user at the keyboard (forwarded to QEMU over the display socket)."""
        for n in names:
            if not self._send_guest(KEY, struct.pack("=II", QCODE[n], 1)):
                return False
            await asyncio.sleep(hold)
            self._send_guest(KEY, struct.pack("=II", QCODE[n], 0))
            await asyncio.sleep(0.15)
        return True

    async def guest_click(self, x: int, y: int) -> bool:
        if not self._send_guest(POINTER_ABS, struct.pack("=II", x, y)):
            return False
        await asyncio.sleep(0.15)
        self._send_guest(BUTTON, struct.pack("=II", 0, 1))
        await asyncio.sleep(0.1)
        self._send_guest(BUTTON, struct.pack("=II", 0, 0))
        return True

    def _ms(self) -> int:
        return int((time.monotonic() - self.t0) * 1000)

    def emit(self, event: dict) -> None:
        self.events_sent.append(event)
        self._log({"event": event})
        if self.writer:
            self.writer.write((json.dumps(event) + "\n").encode())

    def click(self, button: str, values: dict | None = None) -> None:
        doc_id = (self.doc or {}).get("id")
        self.emit({"event": "ui_action", "doc": doc_id, "id": button, "values": values or {}})

    def change(self, widget: str, value) -> None:
        self.emit({"event": "ui_change", "doc": (self.doc or {}).get("id"), "id": widget, "value": value})

    def requests_of(self, cmd: str) -> list[dict]:
        return [r for r in self.requests if r.get("cmd") == cmd]

    def scenes(self) -> list[str]:
        return [r.get("scene") for r in self.requests_of("set_scene")]

    # -- display socket ------------------------------------------------------------
    def _start_display(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        s.bind(self.display_path)
        os.chmod(self.display_path, 0o666)
        s.listen(2)
        s.setblocking(False)
        self._display_sock = s
        asyncio.get_running_loop().add_reader(s.fileno(), self._accept)

    def _accept(self) -> None:
        try:
            conn, _ = self._display_sock.accept()
        except BlockingIOError:
            return
        loop = asyncio.get_running_loop()
        if self._display_conn:
            loop.remove_reader(self._display_conn.fileno())
            self._display_conn.close()
            self._guest_gone()
        conn.setblocking(False)
        self._display_conn = conn
        loop.add_reader(conn.fileno(), self._readable)

    def _guest_gone(self) -> None:
        self.guest = {"connected": False, "pid": None, "session": "", "surface": None, "content": False}
        self._surface = None
        self._content_ref = None
        if self._content_timer:
            self._content_timer.cancel()
            self._content_timer = None
        self.emit({"event": "guest_disconnected", **self.guest})

    def _readable(self) -> None:
        conn = self._display_conn
        try:
            fds = array.array("i")
            data, anc, flags, _ = conn.recvmsg(65536, socket.CMSG_SPACE(4 * fds.itemsize))
        except BlockingIOError:
            return
        except OSError:
            data = b""
            anc = []
        if not data:
            asyncio.get_running_loop().remove_reader(conn.fileno())
            conn.close()
            self._display_conn = None
            self._guest_gone()
            return
        for level, typ, cdata in anc:
            if level == socket.SOL_SOCKET and typ == socket.SCM_RIGHTS:
                fds.frombytes(cdata[: len(cdata) - (len(cdata) % fds.itemsize)])
        mtype, length = struct.unpack("=II", data[:8])
        payload = data[8:8 + length]
        if mtype == 1:  # HELLO
            version, pid = struct.unpack("=II", payload[:8])
            session = payload[8:48].split(b"\0")[0].decode(errors="replace")
            self.guest.update(connected=True, pid=pid, session=session, content=False)
            self._content_ref = None
            self.damage_seq = 0
            self.last_damage = None
            self.pointer = [16, 16]
            self.emit({"event": "guest_connected", **self.guest})
        elif mtype == 2 and fds:  # SURFACE
            w, h, stride, fmt, size = struct.unpack("=IIIIQ", payload[:24])
            try:
                self._surface = (mmap.mmap(fds[0], size, mmap.MAP_SHARED, mmap.PROT_READ), w, h, stride)
            finally:
                for fd in fds:
                    os.close(fd)
            self.guest["surface"] = [w, h]
            self.damage_seq += 1
            self.last_damage = time.monotonic()
            self.emit({"event": "guest_surface", "size": [w, h]})
            self._check_content()
        elif mtype == 3:  # DAMAGE
            self.damage_seq += 1
            self.last_damage = time.monotonic()
            self._check_content()
            if self.remote:
                self.remote.poke()
        elif mtype == 4:
            self._log({"guest_event": payload.decode(errors="replace")})
        for fd in fds if mtype != 2 else []:
            os.close(fd)

    def _check_content(self) -> None:
        """Section 4.3: a gray frame unchanged for 48 ms, or a non-black one settled for 500 ms
        (inspected on every SURFACE/DAMAGE and every 50 ms while undecided)."""
        if self.guest["content"] or not self._surface:
            return
        buf, w, h, stride = self._surface
        gray, nb, digest = content_rule(buf, w, h, stride)
        now = time.monotonic()
        if digest != self._content_ref:
            self._content_ref = digest
            self._content_since = now
        still = now - self._content_since
        if (gray >= GRAY_FRACTION and still >= GRAY_STATIC_S) or (nb >= NONBLACK_FRACTION and still >= NONBLACK_SETTLE_S):
            self.guest["content"] = True
            self.emit({"event": "guest_content", "t_ms": self._ms(), "rule": "gray" if gray >= GRAY_FRACTION else "nonblack"})
            if self.scene.get("scene") == "guest":
                self.emit({"event": "guest_revealed", "t_ms": self._ms()})
            return
        if self._content_timer is None or self._content_timer.when() <= asyncio.get_running_loop().time():
            self._content_timer = asyncio.get_running_loop().call_later(0.05, self._poll_content)

    def _poll_content(self) -> None:
        self._content_timer = None
        self._check_content()


class FakeRemote:
    """Section 4.6 remote screen socket (SEQPACKET) for tests and development runs."""

    def __init__(self, fp: FakePresenter, path: str):
        self.fp = fp
        self.path = path
        self.sock: socket.socket | None = None
        self.conn: socket.socket | None = None
        self.hello = False
        self.source = 0
        self.gen = 0
        self.kind = 0
        self.view = ""
        self.w = self.h = 0
        self.tap: mmap.mmap | None = None
        self.full = False
        self.held: int | None = None
        self.timer: asyncio.TimerHandle | None = None
        self.keys: list[tuple[int, bool]] = []
        self.pointers: list[tuple[int, int, int]] = []
        self.buttons: list[tuple[int, bool]] = []
        self.viewers: list[tuple[int, int]] = []
        self.releases = 0
        self.connections = 0
        self.updates = 0

    def start(self) -> None:
        if os.path.exists(self.path):
            os.unlink(self.path)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        s.bind(self.path)
        os.chmod(self.path, 0o600)
        s.listen(2)
        s.setblocking(False)
        self.sock = s
        asyncio.get_running_loop().add_reader(s.fileno(), self._accept)

    def stop(self) -> None:
        loop = asyncio.get_running_loop()
        for s in (self.conn, self.sock):
            if s is not None and s.fileno() >= 0:
                try:
                    loop.remove_reader(s.fileno())
                except (ValueError, OSError):
                    pass
                s.close()
        self.conn = self.sock = None

    @property
    def connected(self) -> bool:
        return self.conn is not None and self.hello

    def _accept(self) -> None:
        try:
            conn, _ = self.sock.accept()
        except BlockingIOError:
            return
        if self.conn:
            self._drop()
        conn.setblocking(False)
        self.conn = conn
        self.connections += 1
        asyncio.get_running_loop().add_reader(conn.fileno(), self._readable)

    def _drop(self) -> None:
        if self.conn:
            asyncio.get_running_loop().remove_reader(self.conn.fileno())
            self.conn.close()
        self.conn = None
        self.hello = False
        self.held = None
        self.kind = 0
        self.view = ""
        self.tap = None
        self.fp._log({"remote": "disconnected"})

    def _send(self, data: bytes, fd: int | None = None) -> None:
        if not self.conn:
            return
        anc = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("=i", fd))] if fd is not None else []
        self.conn.sendmsg([data], anc)

    def source_picture(self) -> tuple[int, str, int, int, bytes | None]:
        """(kind, view, w, h, BGRX bytes) the real presenter would stream now."""
        fp = self.fp
        scene = fp.scene.get("scene")
        if fp._surface and (scene == "guest" or self.source == 1):
            buf, w, h, stride = fp._surface
            data = b"".join(bytes(buf[y * stride:y * stride + w * 4]) for y in range(h))
            return 1, "guest", w, h, data
        if fp.remote_picture and (scene == "guest" or self.source == 1):
            w, h, data = fp.remote_picture
            return 1, "guest", w, h, bytes(data)
        if self.source == 1:
            return 0, "stopped", 0, 0, None
        w, h = fp.display["mode"][0], fp.display["mode"][1]
        if scene == "black":
            return 4, "black", w, h, bytes(w * h * 4)
        if scene == "panel":
            return 2, "panel", w, h, b"\xdd\xdd\xdd\x00" * (w * h)
        row0 = b"\xff\xff\xff\x00\x00\x00\x00\x00" * (w // 2)
        row1 = row0[4:] + row0[:4]
        return 3, "checkerboard", w, h, b"".join(row0 if y % 2 == 0 else row1 for y in range(h))

    def check(self) -> None:
        """Start a new surface when the picture's kind, view or size changed."""
        if not self.connected:
            return
        kind, view, w, h, _ = self.source_picture()
        if (kind, view, w, h) != (self.kind, self.view, self.w, self.h):
            self._resurface()
        self.poke()

    def _resurface(self) -> None:
        kind, view, w, h, data = self.source_picture()
        self.gen += 1
        self.kind, self.view, self.w, self.h = kind, view, w, h
        fd = None
        self.tap = None
        size = w * h * 4
        if kind and data is not None:
            fd = os.memfd_create("fake-remote-tap", os.MFD_CLOEXEC)
            os.ftruncate(fd, size)
            self.tap = mmap.mmap(fd, size, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
            self.tap[:] = data
        payload = struct.pack("=6IQ", self.gen, kind, w if fd is not None else 0, h if fd is not None else 0,
                              w * 4, 0x34325258, size if fd is not None else 0) + view.encode().ljust(16, b"\0")[:16]
        self._send(struct.pack("=II", 4, len(payload)) + payload, fd)
        if fd is not None:
            os.close(fd)
        self.full = self.tap is not None

    def _changes(self) -> list[tuple[int, int, int, int]]:
        if self.tap is None:
            return []
        if self.full:
            self.full = False
            return [(0, 0, self.w, self.h)]
        _, _, w, h, data = self.source_picture()
        if data is None or (w, h) != (self.w, self.h):
            return []
        rects, start = [], None
        rw = w * 4
        for y in range(h + 1):
            changed = y < h and self.tap[y * rw:(y + 1) * rw] != data[y * rw:(y + 1) * rw]
            if changed and start is None:
                start = y
            if not changed and start is not None:
                self.tap[start * rw:y * rw] = data[start * rw:y * rw]
                rects.append((0, start, w, y - start))
                start = None
        return rects[:64]

    def answer(self, force: bool) -> None:
        if self.held is None or not self.conn:
            return
        rects = self._changes()
        if not rects and not force:
            return
        payload = struct.pack("=3I", self.held, self.gen, len(rects)) + b"".join(struct.pack("=4i", *r) for r in rects)
        self.held = None
        if self.timer:
            self.timer.cancel()
            self.timer = None
        self.updates += 1
        self._send(struct.pack("=II", 5, len(payload)) + payload)

    def poke(self) -> None:
        """The picture may have changed (tests call this after editing remote_picture)."""
        if self.connected:
            kind, view, w, h, _ = self.source_picture()
            if (kind, view, w, h) != (self.kind, self.view, self.w, self.h):
                self._resurface()
            self.answer(False)

    def _readable(self) -> None:
        conn = self.conn
        try:
            data = conn.recv(256)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            self._drop()
            return
        mtype, length = struct.unpack("=II", data[:8])
        args = struct.unpack(f"={length // 4}I", data[8:8 + length]) if length else ()
        self.fp._log({"remote": mtype, "args": list(args)})
        if mtype == 1:      # HELLO
            self.hello = True
            self.source = args[1]
            self._send(struct.pack("=III", 1, 4, 1))
            self._resurface()
        elif not self.hello:
            return
        elif mtype == 2:    # REQUEST
            seq, wait = args
            if self.held is not None:
                self.answer(True)
            self.held = seq
            self.answer(wait == 0)
            if self.held is not None:
                self.timer = asyncio.get_running_loop().call_later(wait / 1000.0, lambda: self.answer(True))
        elif mtype == 3:    # SOURCE
            self.source = args[0]
            self.check()
        elif mtype == 16:
            self.keys.append((args[0], bool(args[1])))
        elif mtype == 17:
            if args[0] == self.gen:
                self.pointers.append((args[0], args[1], args[2]))
        elif mtype == 18:
            self.buttons.append((args[0], bool(args[1])))
        elif mtype == 20:
            self.releases += 1
        elif mtype == 21:
            self.viewers.append((args[0], args[1]))


async def _inject_server(fp: FakePresenter, path: str) -> None:
    if os.path.exists(path):
        os.unlink(path)

    async def handle(reader, writer):
        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                writer.write(b'{"ok":false}\n')
                continue
            if "event" in msg:
                fp.emit(msg)
                out = {"ok": True}
            elif "click" in msg:
                fp.click(msg["click"], msg.get("values"))
                out = {"ok": True, "doc": (fp.doc or {}).get("id")}
            elif "change" in msg:
                fp.change(msg["change"], msg.get("value"))
                out = {"ok": True}
            elif "guest_keys" in msg:
                out = {"ok": await fp.guest_keys(list(msg["guest_keys"]))}
            elif "guest_pointer" in msg:            # move only (menus need press-move-release)
                x, y = (int(v) for v in msg["guest_pointer"])
                out = {"ok": fp._send_guest(POINTER_ABS, struct.pack("=II", x, y))}
            elif "guest_resize" in msg:             # a live Mac screen size (the presenter's RESIZE)
                w, h = (int(v) for v in msg["guest_resize"])
                out = {"ok": fp._send_guest(RESIZE, struct.pack("=II", w, h))}
            elif "guest_button" in msg:
                out = {"ok": fp._send_guest(BUTTON, struct.pack("=II", 0, 1 if msg["guest_button"] else 0))}
            elif "guest_click" in msg:
                out = {"ok": await fp.guest_click(int(msg["guest_click"][0]), int(msg["guest_click"][1]))}
            elif "guest_png" in msg:
                data = fp.surface_png()
                if data:
                    with open(msg["guest_png"], "wb") as f:
                        f.write(data)
                out = {"ok": bool(data), "surface": fp.guest.get("surface")}
            elif msg.get("query") == "state":
                out = {"ok": True, "scene": fp.scene, "doc": fp.doc, "guest": fp.guest,
                       "requests": len(fp.requests), "hostd_connected": fp.writer is not None}
            else:
                out = {"ok": False}
            writer.write((json.dumps(out) + "\n").encode())
            await writer.drain()
        writer.close()

    await asyncio.start_unix_server(handle, path=path)


async def _main(a) -> None:
    disp = dict(DEFAULT_DISPLAY)
    if a.mode:
        w, h = (int(v) for v in a.mode.split("x"))
        disp["mode"] = [w, h, 60]
        disp["modes"] = [[w, h, 60]] + [m for m in DEFAULT_DISPLAY["modes"] if m[:2] != [w, h]]
        disp["suggested_scale"] = disp["scale"] = _suggested(w, h)
    fp = FakePresenter(a.socket, a.display_socket, disp, a.log, remote_path=a.remote_socket)
    await fp.start()
    if a.inject:
        await _inject_server(fp, a.inject)
    print(f"fake presenter listening on {a.socket}", file=sys.stderr, flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", required=True)
    ap.add_argument("--display-socket")
    ap.add_argument("--inject")
    ap.add_argument("--remote-socket", help="serve the section 4.6 remote screen socket here")
    ap.add_argument("--log")
    ap.add_argument("--mode", help="WxH display mode, default 1024x768")
    asyncio.run(_main(ap.parse_args()))
