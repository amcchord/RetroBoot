"""Display-socket protocol checks for the presenter stub (no QEMU or guest needed).

The SEQPACKET/memfd handshake test runs on Linux only.  Set RBD_QEMU_BUILD to a
configured QEMU build directory to check the QKeyCode table against QEMU's
generated qapi-types-ui.c.
"""
import array
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest

TOOLS = Path(__file__).resolve().parents[1] / 'tools'
spec = importlib.util.spec_from_file_location('rbd_presenter_stub', TOOLS / 'rbd_presenter_stub.py')
stub = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stub)


def xrgb_frame(width, height, color, band=None):
    """Raw XRGB8888 (little-endian B,G,R,X) frame, optional top-band painter."""
    b, g, r = color[2], color[1], color[0]
    row = bytearray(bytes((b, g, r, 0)) * width)
    rows = [bytearray(row) for _ in range(height)]
    if band:
        band(rows, width)
    return b''.join(bytes(x) for x in rows)


class KeyAndFrameHelpers(unittest.TestCase):
    def test_relative_motion_keeps_sign_and_does_not_clip_at_screen_edges(self):
        sent = []
        class Wire:
            def send(self, kind, payload):
                sent.append((kind, payload))
        stub.Presenter.relative(Wire(), -1200, 900)
        self.assertEqual(sent, [(22, struct.pack('=ii', -1200, 900))])

    def test_qkeycode_table_matches_qemu_11(self):
        self.assertEqual(len(stub.QKEYCODES), 162)
        for name, value in {'unmapped': 0, 'shift': 1, 'esc': 8, 'ret': 35, 'a': 36,
                            'spc': 60, 'meta_l': 117, 'power': 129, 'f24': 161}.items():
            self.assertEqual(stub.QCODE[name], value, name)
        build = os.environ.get('RBD_QEMU_BUILD')
        if build:
            text = (Path(build) / 'qapi/qapi-types-ui.c').read_text()
            body = text[text.index('const QEnumLookup QKeyCode_lookup'):]
            names = re.findall(r'\[Q_KEY_CODE_[A-Z0-9_]+\] = "([^"]+)"', body[:body.index('};')])
            self.assertEqual(names, stub.QKEYCODES)

    def test_us_layout_text_entry(self):
        self.assertEqual(stub.char_to_key('R'), ('r', True))
        self.assertEqual(stub.char_to_key(' '), ('spc', False))
        self.assertEqual(stub.char_to_key(':'), ('semicolon', True))
        with self.assertRaises(ValueError):
            stub.char_to_key('é')

    def test_detectors_on_synthetic_frames(self):
        w, h = 640, 480
        black = stub.analyze_frame(w, h, w * 4, xrgb_frame(w, h, (0, 0, 0)))
        self.assertFalse(black['gray'])
        self.assertEqual(black['nonblack'], 0)
        gray = stub.analyze_frame(w, h, w * 4, xrgb_frame(w, h, (136, 136, 136)))
        self.assertTrue(gray['gray'])
        self.assertFalse(gray['finder'])

        def dither(rows, width):        # Mac OS's startup background on a built-in LCD
            for y, row in enumerate(rows):
                for x in range(width):
                    v = 255 if (x + y) & 1 else 0
                    row[x * 4:x * 4 + 3] = bytes((v, v, v))
        lcd = stub.analyze_frame(w, h, w * 4, xrgb_frame(w, h, (0, 0, 0), dither))
        self.assertTrue(lcd['gray'])
        self.assertLessEqual(abs(lcd['gray_level'] - 128), 12)
        self.assertFalse(lcd['finder'])

        def finder_bar(rows, width):
            for y in range(20):
                for x in range(width):
                    o = x * 4
                    dark = y in (6, 7, 8, 9, 10, 11, 12) and (x // 3) % 4 == 0
                    v = 0 if dark else 221
                    rows[y][o:o + 3] = bytes((v, v, v))
            for y in range(6, 14):
                for x in range(18, 26):
                    rows[y][x * 4:x * 4 + 3] = bytes((0, 0, 221))  # B,G,R: red
        finder = stub.analyze_frame(w, h, w * 4, xrgb_frame(w, h, (96, 96, 160), finder_bar))
        self.assertTrue(finder['menu_band'])
        self.assertTrue(finder['finder'])

    def test_png_encoder_round_trip_header(self):
        png = stub.png_bytes(2, 1, bytes([255, 0, 0, 0, 255, 0]))
        self.assertEqual(png[:8], b'\x89PNG\r\n\x1a\n')
        self.assertEqual(struct.unpack('>II', png[16:24]), (2, 1))


@unittest.skipUnless(sys.platform.startswith('linux') and hasattr(os, 'memfd_create'),
                     'SOCK_SEQPACKET + memfd handshake needs Linux')
class FakeQemuHandshake(unittest.TestCase):
    """Play QEMU's side of section 3 against the stub's Presenter."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'display.sock'
        self.p = stub.Presenter(self.path, Path(self.tmp.name) / 'log.jsonl')
        threading.Thread(target=self.p.accept_loop, daemon=True).start()
        self.qemu = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.qemu.connect(str(self.path))
        self.addCleanup(self.qemu.close)

    def send(self, mtype, payload=b'', fd=None):
        msg = struct.pack('=II', mtype, len(payload)) + payload
        anc = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [fd]))] if fd is not None else []
        self.qemu.sendmsg([msg], anc)

    def test_hello_surface_damage_events_and_input(self):
        w, h = 64, 48
        fd = os.memfd_create('rbd-test', os.MFD_ALLOW_SEALING)
        os.ftruncate(fd, w * h * 4)
        os.pwrite(fd, xrgb_frame(w, h, (136, 136, 136)), 0)
        self.send(stub.HELLO, struct.pack('=II40s', 1, 4242, b'unit-session'))
        self.send(stub.SURFACE, struct.pack('=IIIIQ', w, h, w * 4, stub.DRM_FORMAT_XRGB8888, w * h * 4), fd)
        os.close(fd)
        self.send(stub.DAMAGE, struct.pack('=II', 1, 2) + struct.pack('=iiii', 0, 0, w, 1) + struct.pack('=iiii', 0, 5, 8, 8))
        self.send(stub.DAMAGE, struct.pack('=II', 3, 1) + struct.pack('=iiii', 0, 0, w, h))
        self.send(stub.GUEST_EVENT, json.dumps({'event': 'boot-handoff', 'clock': 'real-time'}).encode())
        self.assertTrue(self.p.wait_for(lambda: len(self.p.events) == 1, 5))
        self.assertEqual(self.p.hello['session'], 'unit-session')
        self.assertEqual((self.p.surface['width'], self.p.surface['height']), (w, h))
        stats = self.p.stats_snapshot()
        self.assertEqual((stats['messages'], stats['rects'], stats['seq_gaps'], stats['full_frame_damages']), (2, 3, 1, 1))
        width, height, rgb = self.p.frame()
        self.assertEqual(rgb[:3], bytes((136, 136, 136)))

        self.p.key('meta_l', True)
        self.p.pointer(10, 20)
        self.p.button('wheel-down', True)
        self.p.send(stub.RESIZE, struct.pack('=II', 800, 600))
        self.p.send(stub.RELEASE_ALL)
        self.p.send(stub.REFRESH_RATE, struct.pack('=I', 33))
        got = []
        self.qemu.settimeout(2)
        for _ in range(6):
            got.append(self.qemu.recv(64))
        self.assertEqual(got[0], struct.pack('=IIII', stub.KEY, 8, 117, 1))
        self.assertEqual(got[1], struct.pack('=IIII', stub.POINTER_ABS, 8, 10, 20))
        self.assertEqual(got[2], struct.pack('=IIII', stub.BUTTON, 8, 4, 1))
        self.assertEqual(got[3], struct.pack('=IIII', stub.RESIZE, 8, 800, 600))
        self.assertEqual(got[4], struct.pack('=II', stub.RELEASE_ALL, 0))
        self.assertEqual(got[5], struct.pack('=III', stub.REFRESH_RATE, 4, 33))

    def test_new_connection_replaces_old(self):
        self.send(stub.HELLO, struct.pack('=II40s', 1, 1, b''))
        self.assertTrue(self.p.wait_for(lambda: self.p.hello, 5))
        second = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(second.close)
        second.connect(str(self.path))
        second.send(struct.pack('=II', stub.HELLO, 48) + struct.pack('=II40s', 1, 2, b''))
        self.assertTrue(self.p.wait_for(lambda: self.p.hello and self.p.hello['qemu_pid'] == 2, 5))
        self.qemu.settimeout(2)
        self.assertEqual(self.qemu.recv(16), b'')  # old connection shut down


if __name__ == '__main__':
    unittest.main()
