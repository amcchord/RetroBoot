#!/usr/bin/env python3
"""Presenter-side test stub for QEMU's ``-display retroboot`` backend.

Implements the presenter end of the display socket (docs/ARCHITECTURE.md
section 3): it listens on an AF_UNIX SOCK_SEQPACKET socket, accepts one QEMU
connection at a time (a new connection replaces the old one), maps the shared
XRGB8888 memfd passed with SURFACE, records DAMAGE statistics and GUEST_EVENT
messages, saves frames, and sends KEY / POINTER_ABS / BUTTON / RESIZE /
RELEASE_ALL / REFRESH_RATE.

It is a developer and qualification tool, not the product presenter.  Python
standard library only.

    rbd_presenter_stub.py serve --socket S --control C [--log events.jsonl]
    rbd_presenter_stub.py ctl C '{"cmd":"status"}'
    rbd_presenter_stub.py ctl C '{"cmd":"shot","path":"/abs/frame.png"}'
    rbd_presenter_stub.py ctl C '{"cmd":"click","x":590,"y":60,"double":true}'
    rbd_presenter_stub.py ctl C '{"cmd":"type","text":"Hello"}'
    rbd_presenter_stub.py ctl C '{"cmd":"resize","width":800,"height":600}'
    rbd_presenter_stub.py script C steps.json     # list of control requests

Control requests are JSON objects with ``cmd``; replies are one JSON line,
``{"ok":true,...}`` or ``{"ok":false,"error":"..."}``.  See ``COMMANDS``.
"""
from __future__ import annotations

import argparse
import array
import json
import mmap
import os
from pathlib import Path
import signal
import socket
import struct
import sys
import threading
import time
import zlib

# Message types (docs/ARCHITECTURE.md section 3).
HELLO, SURFACE, DAMAGE, GUEST_EVENT = 1, 2, 3, 4
KEY, POINTER_ABS, BUTTON, RESIZE, RELEASE_ALL, REFRESH_RATE = 16, 17, 18, 19, 20, 21
POINTER_REL = 22
DRM_FORMAT_XRGB8888 = 0x34325258
BUTTONS = {'left': 0, 'middle': 1, 'right': 2, 'wheel-up': 3, 'wheel-down': 4}

# QAPI QKeyCode values for QEMU 11.0.2, in enum order (value == index).
# tests/test_rbd_protocol.py checks this list against a QEMU build's
# generated qapi-types-ui.c when one is available.
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
    'katakanahiragana kp_comma kp_equals power sleep wake audionext audioprev '
    'audiostop audioplay audiomute volumeup volumedown mediaselect mail '
    'calculator computer ac_home ac_back ac_forward ac_refresh ac_bookmarks '
    'lang1 lang2 f13 f14 f15 f16 f17 f18 f19 f20 f21 f22 f23 f24'
).split()
QCODE = {name: value for value, name in enumerate(QKEYCODES)}

# US layout text entry: character -> (qcode name, shifted)
_SHIFTED = {'!': '1', '@': '2', '#': '3', '$': '4', '%': '5', '^': '6', '&': '7',
            '*': '8', '(': '9', ')': '0', '_': 'minus', '+': 'equal',
            '{': 'bracket_left', '}': 'bracket_right', '|': 'backslash',
            ':': 'semicolon', '"': 'apostrophe', '~': 'grave_accent',
            '<': 'comma', '>': 'dot', '?': 'slash'}
_PLAIN = {' ': 'spc', '\n': 'ret', '\t': 'tab', '-': 'minus', '=': 'equal',
          '[': 'bracket_left', ']': 'bracket_right', '\\': 'backslash',
          ';': 'semicolon', "'": 'apostrophe', '`': 'grave_accent',
          ',': 'comma', '.': 'dot', '/': 'slash'}


def char_to_key(ch: str) -> tuple[str, bool]:
    if ch.isascii() and ch.isalpha():
        return ch.lower(), ch.isupper()
    if ch.isascii() and ch.isdigit():
        return ch, False
    if ch in _PLAIN:
        return _PLAIN[ch], False
    if ch in _SHIFTED:
        return _SHIFTED[ch], True
    raise ValueError(f'no US-layout key for {ch!r}')


def png_bytes(width: int, height: int, rgb: bytes) -> bytes:
    """Encode 8-bit RGB as PNG (filter type 0 on every row)."""
    row = width * 3
    raw = b''.join(b'\x00' + rgb[y * row:(y + 1) * row] for y in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack('>I', len(data)) + kind + data +
                struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff))

    return (b'\x89PNG\r\n\x1a\n' +
            chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)) +
            chunk(b'IDAT', zlib.compress(raw, 6)) + chunk(b'IEND', b''))


def xrgb_to_rgb(buf: bytes, width: int, height: int, stride: int) -> bytes:
    """Little-endian XRGB8888 rows -> packed RGB."""
    if stride != width * 4:
        buf = b''.join(buf[y * stride:y * stride + width * 4] for y in range(height))
    else:
        buf = buf[:width * height * 4]
    rgb = bytearray(width * height * 3)
    rgb[0::3] = buf[2::4]
    rgb[1::3] = buf[1::4]
    rgb[2::3] = buf[0::4]
    return bytes(rgb)


def _pixel(raw: bytes, stride: int, x: int, y: int) -> tuple[int, int, int]:
    o = y * stride + x * 4
    return raw[o + 2], raw[o + 1], raw[o]


def _block_mean(raw: bytes, stride: int, width: int, height: int, x: int, y: int) -> tuple[int, int, int]:
    x0 = x if x + 1 < width else max(0, x - 1)
    y0 = y if y + 1 < height else max(0, y - 1)
    q = [_pixel(raw, stride, xx, yy) for yy in (y0, min(height - 1, y0 + 1))
         for xx in (x0, min(width - 1, x0 + 1))]
    return tuple((sum(p[c] for p in q) + 2) // 4 for c in range(3))


def analyze_frame(width: int, height: int, stride: int, raw: bytes) -> dict:
    """Detectors on a raw XRGB8888 frame.

    nonblack/gray follow docs/ARCHITECTURE.md section 4.3 (>= 4096 samples,
    each the mean of its 2x2 block, so Mac OS's 1-pixel dither -- its startup
    background on a built-in LCD -- counts as its mean gray);
    finder is ClassicMac's menu-bar band rule (scripts/benchmark-os9-boot.py)
    plus the red stripe of the Apple-menu logo, which the band rule alone
    also accepts on the "Starting Up" desktop before Finder draws its menus.
    """
    xs = [int((i + 0.5) * width / 64) for i in range(64)]
    ys = [int((i + 0.5) * height / 64) for i in range(64)]
    samples = [_block_mean(raw, stride, width, height, x, y) for y in ys for x in xs]
    n = len(samples)
    nonblack = sum(1 for p in samples if max(p) > 16) / n
    best_gray, best_count = None, 0
    for level in range(28, 256, 4):
        count = sum(1 for r, g, b in samples
                    if abs(r - level) <= 12 and abs(g - level) <= 12 and abs(b - level) <= 12)
        if count > best_count:
            best_gray, best_count = level, count
    rows = min(20, height)
    dark = bright = 0
    for y in range(rows):
        row = raw[y * stride:y * stride + width * 4]
        for b, g, r in zip(row[0::4], row[1::4], row[2::4]):
            if r < 50 and g < 50 and b < 50:
                dark += 1
            elif r > 170 and g > 170 and b > 170:
                bright += 1
    total = width * rows
    band = dark >= min(800, max(200, total // 25)) and bright >= max(2000, total // 2)
    apple_red = 0
    if width >= 32 and height >= 20:
        for y in range(2, 19):
            for x in range(12, 32):
                r, g, b = _pixel(raw, stride, x, y)
                if r > 180 and g < 70 and b < 70:
                    apple_red += 1
    return {'width': width, 'height': height, 'nonblack': round(nonblack, 4),
            'gray_level': best_gray, 'gray_fraction': round(best_count / n, 4),
            'gray': best_count >= 0.6 * n and best_gray is not None,
            'menu_band': band, 'apple_red': apple_red, 'finder': band and apple_red >= 3}


class Presenter:
    def __init__(self, socket_path: Path, log_path: Path | None,
                 milestones_dir: Path | None = None):
        self.socket_path = socket_path
        self.milestones_dir = milestones_dir
        self.milestones: dict[str, dict] = {}
        self.log = log_path.open('a', buffering=1) if log_path else None
        self.lock = threading.Condition()
        self.conn: socket.socket | None = None
        self.conn_id = 0
        self.hello: dict | None = None
        self.surface: dict | None = None
        self.mm: mmap.mmap | None = None
        self.surfaces = 0
        self.surface_history: list[dict] = []
        self.events: list[dict] = []
        self.damage_msgs = 0
        self.last_seq: int | None = None
        self.started = time.monotonic()
        self.first_damage_mono: float | None = None
        # Display-damage gaps (guest froze or drew nothing) for stall timing.
        self.last_damage_mono: float | None = None
        self.damage_gaps: list[tuple[float, float]] = []
        self.gap_threshold = 1.0
        # RBD_FRAME_LOG=PATH: one JSON line per DAMAGE with a 32 x 24 sample of the whole
        # surface (share of near-black samples, mean level), to find frames that flash
        # black (lab: ATI page flips and readbacks).
        frame_log = os.environ.get('RBD_FRAME_LOG')
        self.frame_log = open(frame_log, 'a', buffering=1) if frame_log else None
        self.reset_stats()
        self.stopping = False
        self.read_gate = threading.Event()  # cleared to simulate a stalled presenter
        self.read_gate.set()
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        if socket_path.exists():
            socket_path.unlink()
        self.listener.bind(str(socket_path))
        self.listener.listen(2)
        self.record('listening', socket=str(socket_path), pid=os.getpid())

    # -- bookkeeping -----------------------------------------------------
    def record(self, kind: str, **fields):
        entry = {'t': round(time.monotonic() - self.started, 4), 'mono': time.monotonic(),
                 'kind': kind, **fields}
        if self.log:
            self.log.write(json.dumps(entry) + '\n')
        return entry

    def reset_stats(self):
        self.stats = {'since_mono': time.monotonic(), 'messages': 0, 'rects': 0, 'pixels': 0,
                      'max_rects': 0, 'seq_gaps': 0, 'full_frame_damages': 0,
                      'bounding_fallbacks': 0}

    def stats_snapshot(self) -> dict:
        s = dict(self.stats)
        elapsed = time.monotonic() - s.pop('since_mono')
        s['elapsed_s'] = round(elapsed, 3)
        s['messages_per_s'] = round(s['messages'] / elapsed, 2) if elapsed else 0
        s['mpixels_per_s'] = round(s['pixels'] / elapsed / 1e6, 3) if elapsed else 0
        s['copy_mbytes_per_s'] = round(s['pixels'] * 4 / elapsed / 1e6, 3) if elapsed else 0
        return s

    # -- QEMU connection -------------------------------------------------
    def accept_loop(self):
        while not self.stopping:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                if self.stopping:
                    return
                raise
            with self.lock:
                old = self.conn
                self.conn = conn
                self.conn_id += 1
                cid = self.conn_id
                self.hello = None
                self.lock.notify_all()
            if old is not None:
                try:
                    old.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            self.record('accepted', connection=cid)
            threading.Thread(target=self.reader, args=(conn, cid), daemon=True).start()

    def reader(self, conn: socket.socket, cid: int):
        fd_size = struct.calcsize('i')
        while True:
            self.read_gate.wait()
            try:
                data, ancdata, flags, _ = conn.recvmsg(65536, socket.CMSG_SPACE(4 * fd_size))
            except OSError as exc:
                self.record('read_error', connection=cid, error=str(exc))
                data = b''
                ancdata = []
            fds = []
            for level, kind, cdata in ancdata:
                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                    fds_arr = array.array('i')
                    fds_arr.frombytes(cdata[:len(cdata) - (len(cdata) % fd_size)])
                    fds.extend(fds_arr)
            if not data:
                for fd in fds:
                    os.close(fd)
                with self.lock:
                    if self.conn is conn:
                        self.conn = None
                    self.lock.notify_all()
                self.record('disconnected', connection=cid)
                conn.close()
                return
            try:
                self.handle(cid, data, fds, flags)
            finally:
                pass

    def handle(self, cid: int, data: bytes, fds: list[int], flags: int):
        now = time.monotonic()
        if len(data) < 8:
            self.record('protocol_error', connection=cid, error='short message')
            return
        mtype, length = struct.unpack_from('=II', data)
        payload = data[8:]
        if length != len(payload) or flags & socket.MSG_TRUNC:
            self.record('protocol_error', connection=cid, error='length mismatch', type=mtype)
            return
        if mtype == HELLO:
            version, pid = struct.unpack_from('=II', payload)
            session = payload[8:48].split(b'\0', 1)[0].decode('utf-8', 'replace')
            with self.lock:
                self.hello = {'version': version, 'qemu_pid': pid, 'session': session,
                              'connection': cid, 'mono': now}
                self.lock.notify_all()
            self.record('hello', **self.hello)
        elif mtype == SURFACE:
            width, height, stride, fmt, size = struct.unpack_from('=IIIIQ', payload)
            if len(fds) != 1:
                self.record('protocol_error', connection=cid, error=f'SURFACE with {len(fds)} fds')
                for fd in fds:
                    os.close(fd)
                return
            fd = fds[0]
            st = os.fstat(fd)
            try:
                import fcntl
                seals = fcntl.fcntl(fd, getattr(fcntl, 'F_GET_SEALS', 1034))
            except OSError:
                seals = None
            mm = mmap.mmap(fd, size, mmap.MAP_SHARED, mmap.PROT_READ)
            os.close(fd)
            with self.lock:
                old = self.mm
                self.mm = mm
                self.surface = {'width': width, 'height': height, 'stride': stride,
                                'drm_format': fmt, 'size': size, 'file_size': st.st_size,
                                'seals': seals, 'connection': cid, 'mono': now,
                                'index': self.surfaces + 1}
                self.surfaces += 1
                self.surface_history.append(dict(self.surface))
                self.lock.notify_all()
            if old is not None:
                old.close()
            ok = fmt == DRM_FORMAT_XRGB8888 and stride >= width * 4 and size >= stride * height
            self.record('surface', format_ok=ok, **self.surface)
        elif mtype == DAMAGE:
            seq, count = struct.unpack_from('=II', payload)
            rects = [struct.unpack_from('=iiii', payload, 8 + 16 * i) for i in range(count)]
            pixels = sum(w * h for _, _, w, h in rects)
            with self.lock:
                s = self.stats
                s['messages'] += 1
                s['rects'] += count
                s['pixels'] += pixels
                s['max_rects'] = max(s['max_rects'], count)
                if self.last_seq is not None and seq != self.last_seq + 1:
                    s['seq_gaps'] += 1
                self.last_seq = seq
                if self.surface and count == 1 and rects[0] == (0, 0, self.surface['width'], self.surface['height']):
                    s['full_frame_damages'] += 1
                self.damage_msgs += 1
                if self.first_damage_mono is None:
                    self.first_damage_mono = now
                if self.last_damage_mono is not None and now - self.last_damage_mono >= self.gap_threshold:
                    self.damage_gaps.append((self.last_damage_mono, now))
                self.last_damage_mono = now
                self.lock.notify_all()
            if self.frame_log is not None:
                self.log_frame(seq, rects, now)
            if count > 64:
                self.record('protocol_error', connection=cid, error=f'{count} rects > 64')
        elif mtype == GUEST_EVENT:
            try:
                event = json.loads(payload.decode('utf-8'))
            except ValueError:
                event = {'invalid': payload.decode('utf-8', 'replace')}
            with self.lock:
                self.events.append({'mono': now, 'connection': cid, **event})
                self.lock.notify_all()
            self.record('guest_event', connection=cid, **event)
        else:
            self.record('protocol_error', connection=cid, error=f'unknown type {mtype}')
        for fd in fds if mtype != SURFACE else []:
            os.close(fd)

    # -- presenter -> QEMU ----------------------------------------------
    def log_frame(self, seq: int, rects: list, now: float):
        with self.lock:
            mm, surf = self.mm, self.surface
            if mm is None or surf is None:
                return
            w, h, stride = surf['width'], surf['height'], surf['stride']
            black = total = 0
            for j in range(24):
                y = int((j + 0.5) * h / 24)
                for i in range(32):
                    x = int((i + 0.5) * w / 32)
                    o = y * stride + x * 4
                    b, g, r = mm[o], mm[o + 1], mm[o + 2]
                    black += max(r, g, b) <= 16
                    total += r + g + b
        x0 = min((r[0] for r in rects), default=0)
        y0 = min((r[1] for r in rects), default=0)
        x1 = max((r[0] + r[2] for r in rects), default=0)
        y1 = max((r[1] + r[3] for r in rects), default=0)
        self.frame_log.write(json.dumps({'mono': round(now, 4), 'seq': seq, 'size': [w, h],
                                         'box': [x0, y0, x1 - x0, y1 - y0], 'rects': len(rects),
                                         'black': round(black / 768, 3), 'mean': round(total / (768 * 3), 1)}) + '\n')

    def send(self, mtype: int, payload: bytes = b''):
        with self.lock:
            conn = self.conn
        if conn is None:
            raise RuntimeError('QEMU is not connected')
        conn.send(struct.pack('=II', mtype, len(payload)) + payload)

    def key(self, name: str, down: bool):
        self.send(KEY, struct.pack('=II', QCODE[name], int(down)))

    def pointer(self, x: int, y: int):
        self.send(POINTER_ABS, struct.pack('=II', x, y))

    def relative(self, dx: int, dy: int):
        self.send(POINTER_REL, struct.pack('=ii', dx, dy))

    def button(self, name: str, down: bool):
        self.send(BUTTON, struct.pack('=II', BUTTONS[name], int(down)))

    def tap(self, names: list[str], hold: float = 0.06, gap: float = 0.06):
        try:
            for name in names:
                self.key(name, True)
                time.sleep(0.02)
            time.sleep(hold)
        finally:
            for name in reversed(names):
                self.key(name, False)
        time.sleep(gap)

    def type_text(self, text: str, hold: float = 0.05, gap: float = 0.08):
        for ch in text:
            name, shifted = char_to_key(ch)
            self.tap((['shift'] if shifted else []) + [name], hold, gap)

    def click(self, x: int, y: int, button: str = 'left', double: bool = False):
        self.pointer(x, y)
        time.sleep(0.12)
        for _ in range(2 if double else 1):
            self.button(button, True)
            time.sleep(0.08)
            self.button(button, False)
            time.sleep(0.10)

    def drag(self, x0: int, y0: int, x1: int, y1: int, steps: int, step_delay: float,
             hold_after: float = 0.1):
        self.pointer(x0, y0)
        time.sleep(0.12)
        self.button('left', True)
        time.sleep(0.15)
        try:
            for i in range(1, steps + 1):
                self.pointer(round(x0 + (x1 - x0) * i / steps), round(y0 + (y1 - y0) * i / steps))
                time.sleep(step_delay)
            time.sleep(hold_after)
        finally:
            self.button('left', False)

    # -- frames ----------------------------------------------------------
    def frame_raw(self) -> tuple[int, int, int, bytes]:
        with self.lock:
            if self.mm is None or self.surface is None:
                raise RuntimeError('no surface')
            s = self.surface
            return s['width'], s['height'], s['stride'], self.mm[:s['stride'] * s['height']]

    def track_milestones(self, interval: float = 0.05):
        """Record when each boot milestone first appears in the shared buffer."""
        seen_msgs = -1
        while not self.stopping:
            time.sleep(interval)
            with self.lock:
                msgs = self.damage_msgs + self.surfaces
            if msgs == seen_msgs:
                continue
            seen_msgs = msgs
            try:
                w, h, stride, raw = self.frame_raw()
            except RuntimeError:
                continue
            now = time.monotonic()
            a = analyze_frame(w, h, stride, raw)
            for name in ('nonblack', 'gray', 'finder'):
                hit = a[name] >= 0.25 if name == 'nonblack' else a[name]
                if hit and name not in self.milestones:
                    entry = {'mono': now, 'width': w, 'height': h, 'connection': self.conn_id,
                             'analysis': a}
                    with self.lock:
                        self.milestones[name] = entry
                        self.lock.notify_all()
                    self.record('milestone', name=name, **entry)
                    if self.milestones_dir:
                        rgb = xrgb_to_rgb(raw, w, h, stride)
                        _save_frame(w, h, rgb, self.milestones_dir / f'milestone-{name}.png')

    def frame(self) -> tuple[int, int, bytes]:
        with self.lock:
            if self.mm is None or self.surface is None:
                raise RuntimeError('no surface')
            s = self.surface
            raw = self.mm[:s['stride'] * s['height']]
            return s['width'], s['height'], xrgb_to_rgb(raw, s['width'], s['height'], s['stride'])

    def wait_for(self, predicate, timeout: float):
        deadline = time.monotonic() + timeout
        with self.lock:
            while True:
                value = predicate()
                if value:
                    return value
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.lock.wait(min(remaining, 0.25))


def _save_frame(width: int, height: int, rgb: bytes, path: Path):
    if path.suffix == '.ppm':
        path.write_bytes(b'P6\n%d %d\n255\n' % (width, height) + rgb)
    else:
        path.write_bytes(png_bytes(width, height, rgb))


COMMANDS = """status | stats [reset] | events [since] | shot path(.png|.ppm) |
probe | milestones [wait name timeout] | surfaces |
wait_connected timeout | wait_surface [width height] [after_index] timeout |
wait_event event timeout [after_mono] | wait_finder timeout [poll] |
key name down | tap keys[] | type text | move x y | button name down |
click x y [button] [double] | drag x0 y0 x1 y1 steps step_delay |
resize width height | release_all | refresh_rate ms | pause_reading seconds | quit"""


def control_server(p: Presenter, control_path: Path):
    if control_path.exists():
        control_path.unlink()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(control_path))
    srv.listen(4)

    def serve_client(c: socket.socket):
        f = c.makefile('rw', buffering=1)
        for line in f:
            if not line.strip():
                continue
            try:
                req = json.loads(line)
                reply = {'ok': True, **(dispatch(p, req) or {})}
            except Exception as exc:  # report every failure to the client
                reply = {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}
            f.write(json.dumps(reply) + '\n')
            f.flush()
            if reply.get('quit'):
                os.kill(os.getpid(), signal.SIGTERM)
        c.close()

    while True:
        c, _ = srv.accept()
        threading.Thread(target=serve_client, args=(c,), daemon=True).start()


def dispatch(p: Presenter, req: dict) -> dict | None:
    cmd = req['cmd']
    if cmd == 'status':
        with p.lock:
            return {'connected': p.conn is not None, 'connection': p.conn_id, 'hello': p.hello,
                    'surface': p.surface, 'surfaces': p.surfaces, 'damage_messages': p.damage_msgs,
                    'events': len(p.events), 'pid': os.getpid(),
                    'first_damage_mono': p.first_damage_mono}
    if cmd == 'stats':
        with p.lock:
            snap = p.stats_snapshot()
            if req.get('reset'):
                p.reset_stats()
        return {'stats': snap}
    if cmd == 'surfaces':
        with p.lock:
            return {'surfaces': list(p.surface_history)}
    if cmd == 'events':
        since = req.get('since', 0)
        with p.lock:
            return {'events': [e for e in p.events if e['mono'] >= since]}
    if cmd == 'probe':
        w, h, stride, raw = p.frame_raw()
        return {'analysis': analyze_frame(w, h, stride, raw), 'mono': time.monotonic()}
    if cmd == 'milestones':
        if req.get('wait'):
            name = req['wait']
            if not p.wait_for(lambda: name in p.milestones, req.get('timeout', 120)):
                raise TimeoutError(f'milestone {name} not reached')
        with p.lock:
            return {'milestones': dict(p.milestones)}
    if cmd == 'shot':
        path = Path(req['path'])
        if not path.is_absolute():
            raise ValueError('shot path must be absolute')
        w, h, rgb = p.frame()
        _save_frame(w, h, rgb, path)
        return {'width': w, 'height': h, 'path': str(path), 'mono': time.monotonic()}
    if cmd == 'wait_connected':
        ok = p.wait_for(lambda: p.hello is not None and p.conn is not None and p.hello, req.get('timeout', 10))
        if not ok:
            raise TimeoutError('QEMU did not connect')
        return {'hello': ok}
    if cmd == 'wait_surface':
        after = req.get('after_index', 0)
        want = (req.get('width'), req.get('height'))

        def ready():
            s = p.surface
            if s and s['index'] > after and (want[0] is None or (s['width'], s['height']) == want):
                return dict(s)
            return None
        s = p.wait_for(ready, req.get('timeout', 10))
        if not s:
            raise TimeoutError(f'no matching SURFACE (current {p.surface})')
        return {'surface': s}
    if cmd == 'wait_event':
        after = req.get('after_mono', 0)
        name = req['event']
        e = p.wait_for(lambda: next((e for e in p.events if e.get('event') == name and e['mono'] >= after), None),
                       req.get('timeout', 10))
        if not e:
            raise TimeoutError(f'no {name} event')
        return {'event': e}
    if cmd == 'wait_finder':
        deadline = time.monotonic() + req.get('timeout', 120)
        while time.monotonic() < deadline:
            try:
                a = analyze_frame(*p.frame_raw())
                if a['finder']:
                    return {'mono': time.monotonic(), 'width': a['width'], 'height': a['height']}
            except RuntimeError:
                pass
            time.sleep(req.get('poll', 0.1))
        raise TimeoutError('Finder menu bar not seen')
    if cmd == 'key':
        p.key(req['name'], bool(req['down']))
        return None
    if cmd == 'tap':
        p.tap(req['keys'], req.get('hold', 0.06), req.get('gap', 0.06))
        return None
    if cmd == 'type':
        p.type_text(req['text'], req.get('hold', 0.05), req.get('gap', 0.08))
        return None
    if cmd == 'move':
        p.pointer(req['x'], req['y'])
        return None
    if cmd == 'button':
        p.button(req['name'], bool(req['down']))
        return None
    if cmd == 'click':
        p.click(req['x'], req['y'], req.get('button', 'left'), req.get('double', False))
        return None
    if cmd == 'drag':
        p.drag(req['x0'], req['y0'], req['x1'], req['y1'], req.get('steps', 20),
               req.get('step_delay', 0.05), req.get('hold_after', 0.1))
        return None
    if cmd == 'resize':
        p.send(RESIZE, struct.pack('=II', req['width'], req['height']))
        return {'mono': time.monotonic()}
    if cmd == 'release_all':
        p.send(RELEASE_ALL)
        return None
    if cmd == 'refresh_rate':
        p.send(REFRESH_RATE, struct.pack('=I', req['ms']))
        return None
    if cmd == 'pause_reading':
        # Stop draining QEMU's messages for `seconds` (fills the socket so
        # QEMU must drop DAMAGE and later repaint the whole surface).
        p.read_gate.clear()
        p.record('pause_reading', seconds=req['seconds'])
        threading.Timer(req['seconds'], p.read_gate.set).start()
        return None
    if cmd == 'quit':
        return {'quit': True}
    raise ValueError(f'unknown cmd {cmd!r}; commands: {COMMANDS}')


class StubClient:
    """Synchronous client for the control socket (used by test drivers)."""

    def __init__(self, control_path: Path, timeout: float = 300):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(str(control_path))
        self.f = self.sock.makefile('rw', buffering=1)

    def __call__(self, cmd: str, **kw) -> dict:
        self.f.write(json.dumps({'cmd': cmd, **kw}) + '\n')
        self.f.flush()
        line = self.f.readline()
        if not line:
            raise ConnectionError('stub closed the control socket')
        reply = json.loads(line)
        if not reply.get('ok'):
            raise RuntimeError(f'{cmd}: {reply.get("error")}')
        return reply

    def close(self):
        self.f.close()
        self.sock.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='mode', required=True)
    s = sub.add_parser('serve')
    s.add_argument('--socket', type=Path, required=True)
    s.add_argument('--control', type=Path, required=True)
    s.add_argument('--log', type=Path)
    s.add_argument('--milestones-dir', type=Path,
                   help='track boot milestones (nonblack, gray, finder) and save a PNG of each')
    c = sub.add_parser('ctl')
    c.add_argument('control', type=Path)
    c.add_argument('request')
    sc = sub.add_parser('script')
    sc.add_argument('control', type=Path)
    sc.add_argument('steps', type=Path)
    args = ap.parse_args()

    if args.mode == 'serve':
        p = Presenter(args.socket, args.log, args.milestones_dir)

        def stop(*_):
            p.stopping = True
            p.record('stopping')
            try:
                p.listener.close()
            finally:
                os._exit(0)
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        threading.Thread(target=p.accept_loop, daemon=True).start()
        if args.milestones_dir:
            threading.Thread(target=p.track_milestones, daemon=True).start()
        control_server(p, args.control)
    elif args.mode == 'ctl':
        client = StubClient(args.control)
        req = json.loads(args.request)
        print(json.dumps(client(req.pop('cmd'), **req), indent=2))
    else:
        client = StubClient(args.control)
        for step in json.loads(args.steps.read_text()):
            step = dict(step)
            print(json.dumps(client(step.pop('cmd'), **step)))


if __name__ == '__main__':
    sys.exit(main())
