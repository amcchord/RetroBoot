"""Web Screen page: keymap, paste typing, tile encoding, and the WebSocket
stream end to end against the fake presenter's section 4.6 remote socket
(refusals, frames and flow control, input routing, released keys, view-only
mode, typing, key bar, rate limits, viewers, the pairing-code panel)."""
import asyncio
import json
import os
import re
import socket
import ssl
import struct
import time
import unittest
import zlib
from pathlib import Path

import aiohttp
from aiohttp.test_utils import TestServer

import helpers
from fakes.fake_presenter import FakePresenter
from retroboot.hostd import Hostd
from retroboot.remote import encode, keymap, paste
from retroboot.remote import hub as hub_mod
from retroboot.remote import tap as tap_mod

REPO = Path(__file__).resolve().parents[2]
E = keymap.EVDEV


class KeymapTests(unittest.TestCase):
    def test_evdev_numbers_match_the_kernel_header(self):
        hdr = Path("/usr/include/linux/input-event-codes.h")
        if not hdr.exists():
            self.skipTest("no kernel headers")
        raw = dict(re.findall(r"#define\s+KEY_(\w+)\s+(0x[0-9a-fA-F]+|\d+|KEY_\w+)", hdr.read_text()))

        def resolve(name: str) -> int:
            seen = set()
            val = raw[name]
            while isinstance(val, str) and val.startswith("KEY_") and val not in seen:
                seen.add(val)
                val = raw[val[4:]]
            return int(val, 0)

        for name, value in E.items():
            self.assertEqual(resolve(name), value, name)

    def test_every_browser_key_reaches_the_guest(self):
        """Each evdev code we send has a QKeyCode in the presenter's table (presenter/src/keymap.c)."""
        src = (REPO / "presenter/src/keymap.c").read_text()
        table = src[src.index("evdev_qcode[256]"):src.index("};", src.index("evdev_qcode[256]"))]
        mapped = {m for m in re.findall(r"\[KEY_(\w+)\]\s*=\s*\d+", table)}
        by_value = {v: k for k, v in E.items()}
        for code, ev in keymap.CODE_TO_EVDEV.items():
            self.assertIn(by_value[ev], mapped, f"{code} -> KEY_{by_value[ev]} has no QKeyCode")
        self.assertIn("POWER", mapped)

    def test_page_and_hostd_agree_on_the_keys(self):
        """web/js/kvm/input.js KEY_CODES is exactly the set hostd forwards."""
        js = (REPO / "web/js/kvm/input.js").read_text()
        block = js[js.index("export const KEY_CODES"):js.index("]);", js.index("export const KEY_CODES"))]
        codes = set(re.findall(r"'([A-Za-z0-9]+)'", block))
        for prefix, items in (("Key", "ABCDEFGHIJKLMNOPQRSTUVWXYZ"), ("Digit", "0123456789"), ("Numpad", "0123456789")):
            codes |= {f"{prefix}{c}" for c in items}
        codes |= {f"F{n}" for n in range(1, 25)}
        codes -= {c for c in codes if c in ("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "0123456789")}
        self.assertEqual(codes, set(keymap.CODE_TO_EVDEV))

    def test_table_covers_the_keyboard(self):
        for code in ["KeyA", "KeyZ", "Digit0", "Digit9", "F1", "F12", "F15", "Escape", "Tab", "CapsLock",
                     "ShiftLeft", "ShiftRight", "ControlLeft", "AltLeft", "AltRight", "MetaLeft", "MetaRight",
                     "Space", "Enter", "Backspace", "Delete", "Insert", "Home", "End", "PageUp", "PageDown",
                     "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Backquote", "Minus", "Equal",
                     "BracketLeft", "BracketRight", "Backslash", "Semicolon", "Quote", "Comma", "Period", "Slash",
                     "NumLock", "Numpad0", "NumpadEnter", "NumpadEqual", "NumpadAdd", "IntlBackslash", "Help"]:
            self.assertIsNotNone(keymap.to_evdev(code), code)
        for host_key in ("AudioVolumeUp", "AudioVolumeMute", "BrightnessUp", "MediaPlayPause", "Power", "", None, 5):
            self.assertIsNone(keymap.to_evdev(host_key), host_key)

    def test_command_option(self):
        self.assertEqual(keymap.to_evdev("MetaLeft"), E["LEFTMETA"])        # Cmd is Command
        self.assertEqual(keymap.to_evdev("AltLeft"), E["LEFTALT"])          # Option is Option
        self.assertEqual(keymap.to_evdev("AltLeft", swap=True), E["LEFTMETA"])
        self.assertEqual(keymap.to_evdev("MetaRight", swap=True), E["RIGHTALT"])
        self.assertEqual(keymap.to_evdev("KeyA", swap=True), E["A"])
        self.assertFalse(keymap.swap_for("mac", True))                     # a Mac browser: Cmd stays Command
        self.assertFalse(keymap.swap_for("ios", True))
        self.assertTrue(keymap.swap_for("windows", True))                  # PC keyboards follow the setting
        self.assertFalse(keymap.swap_for("linux", False))
        self.assertTrue(keymap.swap_for("mac", False, override=True))      # the page's own choice wins
        self.assertFalse(keymap.swap_for("windows", True, override=False))

    def test_combos(self):
        self.assertEqual(keymap.combo_keys("cmd-q"), [E["LEFTMETA"], E["Q"]])
        self.assertEqual(keymap.combo_keys("force-quit"), [E["LEFTMETA"], E["LEFTALT"], E["ESC"]])
        self.assertEqual(keymap.combo_keys("power"), [E["POWER"]])
        self.assertEqual(keymap.combo_keys("recovery", "ctrl+alt+backspace"),
                         [E["LEFTCTRL"], E["LEFTALT"], E["BACKSPACE"]])
        self.assertIsNone(keymap.combo_keys("rm -rf"))
        self.assertTrue(all(c in keymap.ALLOWED for name in keymap.COMBOS for c in keymap.combo_keys(name)))


class PasteTests(unittest.TestCase):
    def test_ascii(self):
        strokes, skipped = paste.plan("Hi!\n")
        self.assertEqual(skipped, 0)
        self.assertEqual(strokes, [(E["H"], True, False), (E["I"], False, False), (E["1"], True, False),
                                   (E["ENTER"], False, False)])
        every = "".join(chr(c) for c in range(32, 127)) + "\t\n"
        strokes, skipped = paste.plan(every)
        self.assertEqual((len(strokes), skipped), (len(every), 0))

    def test_mac_roman(self):
        strokes, skipped = paste.plan("é")
        self.assertEqual(strokes, [(E["E"], False, True), (E["E"], False, False)])   # Option-E, e
        strokes, _ = paste.plan("Ü")
        self.assertEqual(strokes, [(E["U"], False, True), (E["U"], True, False)])    # Option-U, Shift-U
        self.assertEqual(paste.plan("“…”")[0], [(E["LEFTBRACE"], False, True), (E["SEMICOLON"], False, True),
                                                (E["LEFTBRACE"], True, True)])
        self.assertEqual(paste.plan("€")[0], [(E["2"], True, True)])
        self.assertEqual(paste.plan("©™")[0], [(E["G"], False, True), (E["2"], False, True)])
        # every character typed must be one Mac OS Roman can hold
        for ch in paste.TABLE:
            if ch not in "\n\t€":
                ch.encode("mac_roman")

    def test_fallbacks(self):
        self.assertEqual(paste.plan("a\r\nb")[0], paste.plan("a\nb")[0])
        self.assertEqual(paste.plan("ā")[0], paste.plan("a")[0])            # accent dropped when needed
        self.assertEqual(paste.plan("x😀y"), (paste.plan("xy")[0], 1))     # not typeable: skipped, counted
        self.assertEqual(paste.plan("−")[0], paste.plan("-")[0])


class EncodeTests(unittest.TestCase):
    def setUp(self):
        self.w, self.h = 40, 30
        self.buf = bytearray(os.urandom(self.w * self.h * 4))
        self.stride = self.w * 4

    def test_zlib_tile_is_the_rows(self):
        r = (3, 4, 10, 5)
        data = zlib.decompress(encode.encode_zlib(self.buf, self.stride, r))
        want = b"".join(self.buf[(4 + i) * self.stride + 12:(4 + i) * self.stride + 52] for i in range(5))
        self.assertEqual(data, want)

    def test_png_tile(self):
        png = encode.encode_png(self.buf, self.stride, (0, 0, 2, 1))
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        idat = png[png.index(b"IDAT") + 4:png.index(b"IEND") - 8]
        raw = zlib.decompress(idat)
        b0, g0, r0 = self.buf[0], self.buf[1], self.buf[2]
        self.assertEqual(raw[:4], bytes([0, r0, g0, b0]))

    def test_frame_round_trip_and_split(self):
        tiles = encode.encode_tiles(self.buf, self.stride, [(0, 0, 40, 30), (5, 5, 1, 1)], encode.ENC_ZLIB)
        seq, gen, flags, back = encode.unpack_frame(encode.pack_frame(7, 3, tiles))
        self.assertEqual((seq, gen, flags), (7, 3, 0))
        self.assertEqual([t[0] for t in back], [(0, 0, 40, 30), (5, 5, 1, 1)])
        self.assertEqual(encode.split((0, 0, 1024, 768)), [(0, y, 1024, 128) for y in range(0, 768, 128)])

    def test_region(self):
        g = encode.Region()
        g.add((0, 0, 10, 10))
        g.add((2, 2, 3, 3))            # covered
        g.add((10, 0, 10, 10))         # adjacent: merges
        self.assertEqual(g.rects, [(0, 0, 20, 10)])
        g.add((500, 500, 5, 5))
        self.assertEqual(len(g.rects), 2)
        for i in range(100):
            g.add((i * 20, 300, 4, 4))
        self.assertLessEqual(len(g.rects), encode.Region.MAX)
        self.assertIsNone(encode.clip((50, 50, 10, 10), 40, 30))
        self.assertEqual(encode.clip((-5, 25, 10, 10), 40, 30), (0, 25, 5, 5))


class TapQueueTests(unittest.IsolatedAsyncioTestCase):
    """A full presenter socket queues messages in order; nothing is lost (a lost key
    release would leave the key held in Mac OS)."""

    async def asyncSetUp(self):
        if not hasattr(socket, "AF_UNIX"):
            self.skipTest("no AF_UNIX")
        try:
            a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        except OSError:
            self.skipTest("no SOCK_SEQPACKET")
        a.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        a.setblocking(False)
        self.addCleanup(b.close)
        self.peer = b
        self.closed = []
        self.tap = tap_mod.TapClient("unused", lambda s: None, lambda: self.closed.append(True))
        self.tap.loop = asyncio.get_running_loop()
        self.tap.sock = a
        self.addCleanup(self.tap.close)

    def fill(self):
        """Send key presses until the socket refuses one (it is queued instead)."""
        n = 0
        while not self.tap._outq:
            self.tap.key(n % 200, True)
            n += 1
            self.assertLess(n, 200000)
        return n

    async def read_all(self, count: int) -> list[tuple]:
        msgs = []
        self.peer.setblocking(False)
        deadline = time.monotonic() + 5
        while len(msgs) < count and time.monotonic() < deadline:
            try:
                data = self.peer.recv(64)
            except BlockingIOError:
                await asyncio.sleep(0.005)
                continue
            mtype, ln = struct.unpack_from("=II", data)
            msgs.append((mtype, *struct.unpack_from(f"={ln // 4}I", data, 8)))
        return msgs

    async def test_full_socket_queues_in_order(self):
        n = self.fill()
        self.tap.pointer(1, 5, 5)
        self.tap.pointer(1, 6, 6)          # replaces the queued move
        self.tap.key(30, False)            # a release after the moves
        self.tap.pointer(1, 7, 7)
        self.tap.button(0, False)
        self.assertGreater(self.tap.stats["queued"], 0)
        msgs = await self.read_all(n + 4)
        keys = [m for m in msgs if m[0] == tap_mod.KEY]
        self.assertEqual([m[1] for m in keys[:n]], [i % 200 for i in range(n)])
        self.assertEqual(msgs[n:], [(tap_mod.POINTER, 1, 6, 6), (tap_mod.KEY, 30, 0),
                                    (tap_mod.POINTER, 1, 7, 7), (tap_mod.BUTTON, 0, 0)])
        await asyncio.sleep(0.05)
        self.assertFalse(self.tap._outq)
        self.assertFalse(self.tap._writing)     # the writer stops once the queue is empty
        self.assertEqual(self.closed, [])

    async def test_a_presenter_that_stops_reading_is_disconnected(self):
        self.fill()
        for i in range(tap_mod.OUTQ_MAX + 1):
            self.tap.key(i % 200, False)
        self.assertFalse(self.tap.connected)     # reconnecting makes the presenter release every key
        self.assertEqual(self.closed, [True])


class DropScoreTests(unittest.TestCase):
    def test_bursts_are_forgiven_and_flooding_is_not(self):
        now = [1000.0]
        orig = hub_mod.mono
        hub_mod.mono = lambda: now[0]
        self.addCleanup(setattr, hub_mod, "mono", orig)
        v = hub_mod.Viewer(None, None, {}, {}, False, "control")
        for _ in range(2000):
            score = v.note_drop()
        self.assertAlmostEqual(score, 2000.0)
        now[0] += 10.0                                   # half-life
        self.assertAlmostEqual(v.note_drop(), 1001.0)
        now[0] += 120.0
        self.assertLess(v.note_drop(), 2.0)              # an old burst is forgotten
        for _ in range(int(hub_mod.DROP_LIMIT) + 1):
            score = v.note_drop()
        self.assertGreater(score, hub_mod.DROP_LIMIT)    # sustained flooding is not
        self.assertEqual(v.dropped, 2000 + 2 + int(hub_mod.DROP_LIMIT) + 1)


W, H = 64, 48


def picture(seed: int = 0) -> bytearray:
    return bytearray(((x * 7 + y * 13 + seed) & 0xFF) for y in range(H) for x in range(W * 4))


class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        helpers.make_package(self.root)
        helpers.make_machine(self.root)
        self.cfg = helpers.make_config(self.root, remote_key_events_per_s=500.0)
        self.app = Hostd(self.cfg)
        self.app.loop = asyncio.get_running_loop()
        self.app.settings.store.load()
        run = self.app.paths.run
        self.fp = FakePresenter(str(run / "presenter.sock"), None, remote_path=str(self.app.paths.remote_sock))
        self.fp.scene = {"scene": "guest"}
        self.fp.remote_picture = (W, H, picture())
        await self.fp.start()
        self.addAsyncCleanup(self.fp.stop)
        self.server = TestServer(self.app.web.web_app, host="127.0.0.1")
        await self.server.start_server(ssl=self.app.web.tls())
        self.addAsyncCleanup(self.server.close)
        self.base = f"127.0.0.1:{self.server.port}"
        self.session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False))
        self.addAsyncCleanup(self.session.close)
        self.addAsyncCleanup(self.app.remote.close)
        code, _ = self.app.pairing.current_code()
        r = await self.session.post(f"https://{self.base}/api/v1/pair", json={"code": code, "device_name": "Test Mac"},
                                    headers={"Origin": f"https://{self.base}"})
        self.assertEqual(r.status, 200, await r.text())
        self.cookie = r.cookies["rb_session"].value
        self.csrf = (await r.json())["csrf"]

    async def open(self, platform="mac", origin=True, csrf=None, scheme="wss", source="composed", **hello):
        hd = {"Cookie": f"rb_session={self.cookie}"}
        if origin:
            hd["Origin"] = origin if isinstance(origin, str) else f"https://{self.base}"
        ws = await self.session.ws_connect(f"{scheme}://{self.base}/api/v1/screen/stream", headers=hd)
        await ws.send_json({"t": "hello", "v": 1, "csrf": csrf or self.csrf, "platform": platform,
                            "enc": ["zlib", "png"], "source": source, **hello})
        self.addAsyncCleanup(ws.close)
        return ws

    async def recv(self, ws, want: str, timeout=5.0):
        """Next message of type ``want`` (JSON 't' or 'frame')."""
        while True:
            m = await asyncio.wait_for(ws.receive(), timeout)
            if m.type == aiohttp.WSMsgType.BINARY:
                if want == "frame":
                    return encode.unpack_frame(m.data)
                continue
            if m.type != aiohttp.WSMsgType.TEXT:
                raise AssertionError(f"socket closed: {m}")
            d = json.loads(m.data)
            if d.get("t") == want:
                return d

    async def wait(self, pred, what):
        return await helpers.wait_for(pred, 5.0, 0.02, what)

    # -- refusals ----------------------------------------------------------------
    async def test_refusals(self):
        url = f"wss://{self.base}/api/v1/screen/stream"
        ok_origin = {"Origin": f"https://{self.base}"}
        for headers, status in (({}, 401), ({**ok_origin, "Cookie": "rb_session=forged"}, 401),
                                ({"Cookie": f"rb_session={self.cookie}"}, 403),
                                ({"Cookie": f"rb_session={self.cookie}", "Origin": "https://evil.example"}, 403)):
            with self.assertRaises(aiohttp.WSServerHandshakeError) as cm:
                await self.session.ws_connect(url, headers=headers)
            self.assertEqual(cm.exception.status, status, headers)
        # never on a plain-HTTP listener, even with a valid session
        plain = TestServer(self.app.web.web_app, host="127.0.0.1")
        await plain.start_server()
        self.addAsyncCleanup(plain.close)
        with self.assertRaises(aiohttp.WSServerHandshakeError) as cm:
            await self.session.ws_connect(f"ws://127.0.0.1:{plain.port}/api/v1/screen/stream",
                                          headers={"Cookie": f"rb_session={self.cookie}",
                                                   "Origin": f"http://127.0.0.1:{plain.port}"})
        self.assertEqual(cm.exception.status, 403)
        # a wrong CSRF token in the hello closes the socket before any frame
        ws = await self.open(csrf="0" * 64)
        d = await self.recv(ws, "error")
        self.assertEqual(d["code"], "csrf-invalid")
        m = await ws.receive()
        self.assertEqual(m.type, aiohttp.WSMsgType.CLOSE)
        self.assertEqual(self.app.remote.viewers, {})
        # turned off
        self.app.settings.set_desired_and_applied({"remote.screen": "off"}, self.app.validation_context())
        with self.assertRaises(aiohttp.WSServerHandshakeError) as cm:
            await self.session.ws_connect(url, headers={"Cookie": f"rb_session={self.cookie}", **ok_origin})
        self.assertEqual(cm.exception.status, 403)

    # -- frames ------------------------------------------------------------------
    async def test_frames_and_flow_control(self):
        ws = await self.open()
        hello = await self.recv(ws, "hello")
        self.assertEqual(hello["mode"], "control")       # the default: paired browsers may control
        self.assertFalse(hello["swap"])
        s = await self.recv(ws, "surface")
        self.assertEqual((s["w"], s["h"], s["view"], s["kind"]), (W, H, "guest", "guest"))
        seq, gen, _, tiles = await self.recv(ws, "frame")
        self.assertEqual(gen, s["gen"])
        self.assertEqual([t[0] for t in tiles], [(0, 0, W, H)])
        self.assertEqual(zlib.decompress(tiles[0][2]), bytes(self.fp.remote_picture[2]))
        await ws.send_json({"t": "ack", "seq": seq})
        # a change: only the changed rows come back
        pic = self.fp.remote_picture[2]
        pic[10 * W * 4: 12 * W * 4] = b"\x01" * (2 * W * 4)
        self.fp.remote.poke()
        seq, gen, _, tiles = await self.recv(ws, "frame")
        self.assertEqual([t[0] for t in tiles], [(0, 10, W, 2)])
        # without acks no more than two frames are in flight; the rest merges
        for i in range(6):
            pic[(20 + i) * W * 4] ^= 0xFF
            self.fp.remote.poke()
            await asyncio.sleep(0.08)
        frames = []
        try:
            while True:
                frames.append(await self.recv(ws, "frame", timeout=0.5))
        except asyncio.TimeoutError:
            pass
        self.assertLessEqual(len(frames), 1)          # one was already in flight
        for f in frames + [(seq, gen, 0, tiles)]:
            await ws.send_json({"t": "ack", "seq": f[0]})
        merged = await self.recv(ws, "frame")
        covered = {y for t in merged[3] for y in range(t[0][1], t[0][1] + t[0][3])}
        self.assertTrue({20, 21, 22, 23, 24, 25} - {y for f in frames for t in f[3]
                                                    for y in range(t[0][1], t[0][1] + t[0][3])} <= covered)
        st = (await (await self.session.get(f"https://{self.base}/api/v1/status",
                                            headers={"Cookie": f"rb_session={self.cookie}"})).json())["remote"]
        self.assertEqual(st["viewers"][0]["name"], "Test Mac")
        self.assertGreaterEqual(st["stats"]["frames"], 3)

    async def test_scene_changes_follow(self):
        ws = await self.open()
        await self.recv(ws, "surface")
        f = await self.recv(ws, "frame")
        await ws.send_json({"t": "ack", "seq": f[0]})
        self.fp.handle({"cmd": "set_scene", "scene": "splash", "id": 1})
        s = await self.recv(ws, "surface")
        self.assertEqual((s["view"], s["kind"]), ("checkerboard", "checkerboard"))
        _, gen, _, tiles = await self.recv(ws, "frame")
        self.assertEqual(gen, s["gen"])
        raw = zlib.decompress(tiles[0][2])
        self.assertNotEqual(raw[0:4], raw[4:8])          # a checkerboard
        # the raw guest instead of the composed picture
        await ws.send_json({"t": "source", "source": "guest"})
        s = await self.recv(ws, "surface")
        self.assertEqual(s["view"], "guest")

    # -- input -------------------------------------------------------------------
    async def test_input_routing(self):
        ws = await self.open(platform="mac")
        s = await self.recv(ws, "surface")
        rem = self.fp.remote
        await ws.send_json({"t": "key", "code": "MetaLeft", "down": True})
        await ws.send_json({"t": "key", "code": "KeyQ", "down": True})
        await ws.send_json({"t": "key", "code": "KeyQ", "down": True})       # browser auto-repeat: one press
        await ws.send_json({"t": "key", "code": "KeyQ", "down": False})
        await ws.send_json({"t": "key", "code": "MetaLeft", "down": False})
        await ws.send_json({"t": "key", "code": "AudioVolumeUp", "down": True})   # host key: never forwarded
        await ws.send_json({"t": "move", "x": 10, "y": 20, "gen": s["gen"]})
        await ws.send_json({"t": "button", "b": 0, "down": True, "x": 11, "y": 21, "gen": s["gen"]})
        await ws.send_json({"t": "button", "b": 0, "down": False})
        # the right button (and a long press or two-finger tap) is Mac button 2, as from a local
        # USB mouse: USB Overdrive's Control-click, or the tablet driver's; never Control + click
        await ws.send_json({"t": "button", "b": 2, "down": True, "x": 12, "y": 22, "gen": s["gen"]})
        await ws.send_json({"t": "button", "b": 2, "down": False})
        await ws.send_json({"t": "button", "b": 1, "down": True})
        await ws.send_json({"t": "button", "b": 1, "down": False})
        await ws.send_json({"t": "button", "b": 3, "down": True})              # wheel only as "wheel"
        await ws.send_json({"t": "wheel", "dy": -2})
        await ws.send_json({"t": "wheel", "dy": 40})                             # at most 10 notches
        await ws.send_json({"t": "move", "x": 99999, "y": 1, "gen": s["gen"]})   # out of range: dropped
        await self.wait(lambda: len(rem.buttons) >= 30, "input")
        self.assertEqual(rem.keys, [(E["LEFTMETA"], True), (E["Q"], True), (E["Q"], False), (E["LEFTMETA"], False)])
        self.assertEqual(rem.pointers, [(s["gen"], 10, 20), (s["gen"], 11, 21), (s["gen"], 12, 22)])
        self.assertEqual(rem.buttons, [(0, True), (0, False), (2, True), (2, False), (1, True), (1, False),
                                       (3, True), (3, False), (3, True), (3, False)] + [(4, True), (4, False)] * 10)

    async def test_caps_lock_follows_the_browser(self):
        """Mac OS's ADB Caps Lock is a locking key: held down while the browser's lock is on."""
        ws = await self.open()
        await self.recv(ws, "surface")
        rem = self.fp.remote
        caps = E["CAPSLOCK"]
        await ws.send_json({"t": "caps", "on": True})
        await ws.send_json({"t": "key", "code": "KeyA", "down": True, "caps": True})
        await ws.send_json({"t": "key", "code": "KeyA", "down": False})
        await ws.send_json({"t": "key", "code": "CapsLock", "down": False, "caps": True})   # never a plain key
        await self.wait(lambda: len(rem.keys) >= 5, "keys")
        self.assertEqual(rem.keys[:4], [(caps, True), (caps, True), (E["A"], True), (E["A"], False)])
        self.assertNotIn((caps, False), rem.keys)
        await ws.send_json({"t": "caps", "on": False})
        await self.wait(lambda: rem.keys[-1] == (caps, False), "unlock")
        await ws.send_json({"t": "caps", "on": False})        # already off: nothing more
        await asyncio.sleep(0.2)
        self.assertEqual(sum(1 for k in rem.keys if k == (caps, False)), 1)

    async def test_pc_browser_follows_the_swap_setting(self):
        self.app.settings.set_desired_and_applied({"input.swap_command_option": True}, self.app.validation_context())
        ws = await self.open(platform="windows")
        hello = await self.recv(ws, "hello")
        self.assertTrue(hello["swap"])
        await ws.send_json({"t": "key", "code": "AltLeft", "down": True})
        await self.wait(lambda: self.fp.remote.keys, "key")
        self.assertEqual(self.fp.remote.keys, [(E["LEFTMETA"], True)])    # Alt next to the space bar = Command
        # the setting changes while the key is down: the release still matches the press
        self.app.settings.set_desired_and_applied({"input.swap_command_option": False}, self.app.validation_context())
        await ws.send_json({"t": "key", "code": "AltLeft", "down": False})
        await self.wait(lambda: len(self.fp.remote.keys) == 2, "release")
        self.assertEqual(self.fp.remote.keys[1], (E["LEFTMETA"], False))

    async def test_keys_released_on_blur_and_disconnect(self):
        ws = await self.open()
        await self.recv(ws, "surface")
        rem = self.fp.remote
        await ws.send_json({"t": "key", "code": "ShiftLeft", "down": True})
        await ws.send_json({"t": "button", "b": 0, "down": True})
        await ws.send_json({"t": "release"})                                 # window blur
        await self.wait(lambda: len(rem.keys) == 2 and len(rem.buttons) == 2, "blur release")
        self.assertEqual(rem.keys, [(E["LEFTSHIFT"], True), (E["LEFTSHIFT"], False)])
        self.assertEqual(rem.buttons, [(0, True), (0, False)])
        await ws.send_json({"t": "key", "code": "KeyX", "down": True})
        await self.wait(lambda: len(rem.keys) == 3, "press")
        await ws.close()
        await self.wait(lambda: len(rem.keys) == 4, "disconnect release")
        self.assertEqual(rem.keys[3], (E["X"], False))
        await self.wait(lambda: not self.app.remote.viewers, "detach")
        ev = helpers.events(self.app, "remote_screen_close")
        self.assertEqual(ev[-1]["device"], self.app.pairing.devices()[0]["id"])

    async def test_two_viewers_share_a_key(self):
        a = await self.open()
        await self.recv(a, "surface")
        b = await self.open()
        viewers = await self.recv(b, "viewers")        # sent to everyone as b joins, before its surface
        self.assertEqual(len(viewers["list"]), 2)
        self.assertEqual([v["you"] for v in viewers["list"]], [False, True])
        await self.recv(b, "surface")
        await self.wait(lambda: self.fp.remote.viewers and self.fp.remote.viewers[-1] == (2, 2), "indicator")
        await a.send_json({"t": "key", "code": "ShiftLeft", "down": True})
        await self.wait(lambda: self.fp.remote.keys, "a's press")
        await b.send_json({"t": "key", "code": "ShiftLeft", "down": True})
        await self.wait(lambda: self.app.remote.key_holders.get(E["LEFTSHIFT"], set()).__len__() == 2, "b holds")
        await a.send_json({"t": "key", "code": "ShiftLeft", "down": False})
        await asyncio.sleep(0.2)
        self.assertEqual(self.fp.remote.keys, [(E["LEFTSHIFT"], True)])       # still held by b
        await b.close()
        await self.wait(lambda: len(self.fp.remote.keys) == 2, "last holder")
        await self.wait(lambda: self.fp.remote.viewers[-1] == (1, 1), "indicator follows")

    async def test_view_only(self):
        self.app.settings.set_desired_and_applied({"remote.screen": "view"}, self.app.validation_context())
        ws = await self.open()
        hello = await self.recv(ws, "hello")
        self.assertEqual(hello["mode"], "view")
        await self.recv(ws, "frame")
        await ws.send_json({"t": "key", "code": "KeyA", "down": True})
        await ws.send_json({"t": "type", "text": "hello"})
        await asyncio.sleep(0.3)
        self.assertEqual(self.fp.remote.keys, [])
        self.assertEqual(self.fp.remote.viewers[-1], (1, 0))
        # control granted live; taking it away releases what was held
        self.app.settings.set_desired_and_applied({"remote.screen": "control"}, self.app.validation_context())
        await self.app.remote.apply_setting()
        self.assertEqual((await self.recv(ws, "mode"))["mode"], "control")
        await ws.send_json({"t": "key", "code": "KeyA", "down": True})
        await self.wait(lambda: self.fp.remote.keys, "key")
        self.app.settings.set_desired_and_applied({"remote.screen": "view"}, self.app.validation_context())
        await self.app.remote.apply_setting()
        await self.wait(lambda: self.fp.remote.keys[-1] == (E["A"], False), "released")
        # off closes the stream
        self.app.settings.set_desired_and_applied({"remote.screen": "off"}, self.app.validation_context())
        await self.app.remote.apply_setting()
        self.assertEqual((await self.recv(ws, "error"))["code"], "remote-screen-off")

    async def test_typing_and_key_bar(self):
        ws = await self.open()
        await self.recv(ws, "surface")
        await ws.send_json({"t": "type", "text": "Aé"})
        done = None
        while not done:
            d = await self.recv(ws, "typing")
            done = d if d["state"] != "running" else None
        self.assertEqual((done["state"], done["done"], done["total"], done["skipped"]), ("done", 3, 3, 0))
        await self.wait(lambda: len(self.fp.remote.keys) >= 10, "typed keys at the presenter")
        self.assertEqual(self.fp.remote.keys, [
            (E["LEFTSHIFT"], True), (E["A"], True), (E["A"], False), (E["LEFTSHIFT"], False),
            (E["LEFTALT"], True), (E["E"], True), (E["E"], False), (E["LEFTALT"], False),
            (E["E"], True), (E["E"], False)])
        self.fp.remote.keys.clear()
        await ws.send_json({"t": "combo", "name": "cmd-q"})
        await self.wait(lambda: len(self.fp.remote.keys) == 4, "combo")
        self.assertEqual(self.fp.remote.keys, [(E["LEFTMETA"], True), (E["Q"], True), (E["Q"], False),
                                               (E["LEFTMETA"], False)])
        self.fp.remote.keys.clear()
        await ws.send_json({"t": "combo", "name": "recovery"})
        await self.wait(lambda: len(self.fp.remote.keys) == 6, "chord")
        self.assertEqual([k for k, d in self.fp.remote.keys if d], [E["LEFTCTRL"], E["LEFTALT"], E["DELETE"]])
        # a long paste can be cancelled, and its modifiers are released
        self.fp.remote.keys.clear()
        await ws.send_json({"t": "type", "text": "ABCDEFGHIJ" * 20})
        await self.recv(ws, "typing")
        await ws.send_json({"t": "cancel"})
        d = await self.recv(ws, "typing")
        while d["state"] == "running":
            d = await self.recv(ws, "typing")
        self.assertEqual(d["state"], "cancelled")

        def released():
            held = {}
            for k, down in self.fp.remote.keys:
                held[k] = down
            return not any(held.values())
        await self.wait(released, "everything released after the cancel")

    async def test_typing_is_paced_for_the_adb_queue(self):
        """A burst fills at most key_burst events, then key_events_per_s (Mac OS drains about 23/s)."""
        hub = self.app.remote
        hub.key_rate, hub.key_burst = 50.0, 10.0
        hub._key_tokens, hub._key_t = 10.0, __import__("time").monotonic()
        t0 = asyncio.get_running_loop().time()
        for _ in range(10):
            await hub._key_slot()
        self.assertLess(asyncio.get_running_loop().time() - t0, 0.05)     # the burst
        for _ in range(10):
            await hub._key_slot()
        self.assertGreater(asyncio.get_running_loop().time() - t0, 0.17)  # then 50 events/s

    async def test_web_management_off_ends_the_stream(self):
        ws = await self.open()
        await self.recv(ws, "frame")
        self.app.settings.set_desired_and_applied({"management.web": "off"}, self.app.validation_context())
        await self.app.apply_engine.apply_live_keys(["management.web"])
        self.assertEqual((await self.recv(ws, "error"))["code"], "web-management-off")
        m = await asyncio.wait_for(ws.receive(), 5)
        self.assertEqual(m.type, aiohttp.WSMsgType.CLOSE)
        self.assertEqual(self.app.remote.viewers, {})

    async def test_unpairing_ends_the_stream(self):
        ws = await self.open()
        await self.recv(ws, "frame")
        dev = self.app.pairing.devices()[0]["id"]
        self.app.pairing.revoke(dev)
        d = await self.recv(ws, "error", timeout=8)
        self.assertEqual(d["code"], "not-paired")
        m = await asyncio.wait_for(ws.receive(), 5)
        self.assertEqual(m.type, aiohttp.WSMsgType.CLOSE)

    async def test_rate_limit(self):
        ws = await self.open()
        s = await self.recv(ws, "surface")
        rem, hub = self.fp.remote, self.app.remote
        # moves beyond the limit are coalesced, never counted as dropped: the newest position arrives
        for i in range(1500):
            await ws.send_json({"t": "move", "x": i % W, "y": 1, "gen": s["gen"]})
        await self.wait(lambda: rem.pointers and rem.pointers[-1] == (s["gen"], 1499 % W, 1), "last move")
        self.assertLess(len(rem.pointers), 1500)
        self.assertEqual(hub.stats["dropped_input"], 0)
        # a flood of presses is cut, but the releases that follow always get through
        await ws.send_json({"t": "key", "code": "ShiftLeft", "down": True})
        await ws.send_json({"t": "button", "b": 0, "down": True})
        for i in range(3000):
            await ws.send_json({"t": "key", "code": "KeyA", "down": i % 2 == 0})
        await ws.send_json({"t": "key", "code": "ShiftLeft", "down": False})
        await ws.send_json({"t": "button", "b": 0, "down": False})
        await ws.send_json({"t": "caps", "on": True})          # Caps Lock state is never dropped
        await self.wait(lambda: rem.buttons[-1:] == [(0, False)], "button release")
        await self.wait(lambda: (E["CAPSLOCK"], True) in rem.keys, "caps")
        self.assertGreater(hub.stats["dropped_input"], 0)
        self.assertIn((E["LEFTSHIFT"], False), rem.keys)
        held = {}
        for k, down in rem.keys:
            held[k] = down
        self.assertFalse(held[E["LEFTSHIFT"]])
        self.assertFalse(held.get(E["A"], False))
        # sustained flooding ends the stream
        hub_mod.DROP_LIMIT, orig = 50.0, hub_mod.DROP_LIMIT
        self.addCleanup(setattr, hub_mod, "DROP_LIMIT", orig)
        for i in range(1500):
            await ws.send_json({"t": "key", "code": "KeyB", "down": True})
        self.assertEqual((await self.recv(ws, "error"))["code"], "too-much-input")
        m = await asyncio.wait_for(ws.receive(), 5)
        self.assertEqual((m.type, m.data), (aiohttp.WSMsgType.CLOSE, 4429))

    async def test_pairing_code_panel_is_private(self):
        await self._pairing_code_panel_is_private("composed")

    async def test_pairing_code_panel_is_private_whatever_the_source(self):
        """Mac OS alone never shows the panel, but its keys would reach the panel: blocked too."""
        await self._pairing_code_panel_is_private("guest")

    async def _pairing_code_panel_is_private(self, source):
        ws = await self.open(source=source)
        await self.recv(ws, "frame")
        self.assertEqual(self.app.remote.source, source)

        class Screen:
            doc_id = "network-web"
            async def action(self, id, values):
                panel.visible = False
                panel.backed = id

        panel = self.app.panel
        panel.screen, panel.visible = Screen(), True
        self.fp.handle({"cmd": "set_scene", "scene": "panel", "id": 1})
        d = await self.recv(ws, "state")
        while d.get("private") is not True:
            d = await self.recv(ws, "state")
        await ws.send_json({"t": "key", "code": "KeyA", "down": True})
        with self.assertRaises(asyncio.TimeoutError):
            await self.recv(ws, "frame", timeout=0.6)
        self.assertEqual(self.fp.remote.keys, [])
        await ws.send_json({"t": "panel-back"})
        await self.wait(lambda: getattr(panel, "backed", None) == "cancel", "panel back")
        d = await self.recv(ws, "state")
        self.assertIs(d["private"], False)
        await self.recv(ws, "frame")

    async def test_only_control_changes_the_picture(self):
        self.app.settings.set_desired_and_applied({"remote.screen": "view"}, self.app.validation_context())
        ws = await self.open(source="guest")                # a view-only browser's choice is ignored
        hello = await self.recv(ws, "hello")
        self.assertEqual((hello["mode"], hello["source"]), ("view", "composed"))
        await ws.send_json({"t": "source", "source": "guest"})
        self.assertEqual((await self.recv(ws, "source"))["source"], "composed")
        self.assertEqual(self.app.remote.source, "composed")
        await self.wait(lambda: self.fp.remote.connections, "tap")
        self.assertEqual(self.fp.remote.source, 0)

    async def test_admission_is_checked_again_after_the_hello(self):
        """The setting or the pairing may change while the socket waits for its hello."""
        hd = {"Cookie": f"rb_session={self.cookie}", "Origin": f"https://{self.base}"}
        ctx = self.app.validation_context()

        def turn_off():
            self.app.settings.set_desired_and_applied({"remote.screen": "off"}, ctx)

        def unpair():
            self.app.pairing.revoke(self.app.pairing.devices()[0]["id"])
        for change, code in ((turn_off, "remote-screen-off"), (unpair, "not-paired")):
            self.app.settings.set_desired_and_applied({"remote.screen": "control"}, ctx)
            ws = await self.session.ws_connect(f"wss://{self.base}/api/v1/screen/stream", headers=hd)
            self.addAsyncCleanup(ws.close)
            change()
            await ws.send_json({"t": "hello", "v": 1, "csrf": self.csrf, "platform": "mac", "enc": ["zlib"]})
            self.assertEqual((await self.recv(ws, "error"))["code"], code)
            m = await asyncio.wait_for(ws.receive(), 5)
            self.assertEqual(m.type, aiohttp.WSMsgType.CLOSE)
            self.assertEqual(self.app.remote.viewers, {})

    async def test_source_follows_while_the_tap_lingers(self):
        self.fp.handle({"cmd": "set_scene", "scene": "splash", "id": 1})
        a = await self.open(source="composed")
        s = await self.recv(a, "surface")
        self.assertEqual(s["view"], "checkerboard")
        await a.close()
        await self.wait(lambda: not self.app.remote.viewers, "detach")
        self.assertTrue(self.app.remote.tap.connected)       # lingering for a page reload
        b = await self.open(source="guest")
        self.assertEqual((await self.recv(b, "hello"))["source"], "guest")
        s = await self.recv(b, "surface")
        while s["view"] != "guest":
            s = await self.recv(b, "surface")
        self.assertEqual(self.fp.remote.connections, 1)      # the same tap followed

    async def test_close_stops_the_loop_for_good(self):
        ws = await self.open()
        await self.recv(ws, "frame")
        hub = self.app.remote
        closing = asyncio.get_running_loop().create_task(hub.close())
        m = await asyncio.wait_for(ws.receive(), 5)
        self.assertEqual(m.type, aiohttp.WSMsgType.CLOSE)
        await closing
        await asyncio.sleep(1.3)                              # past the restart delay
        self.assertTrue(hub._task is None or hub._task.done())
        self.assertFalse(hub.tap.connected)


if __name__ == "__main__":
    unittest.main()
