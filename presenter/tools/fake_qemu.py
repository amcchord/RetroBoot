#!/usr/bin/env python3
"""Fake QEMU display client for rb-presenter (ARCHITECTURE.md section 3).

Speaks the display-socket protocol like `-display retroboot`: connects (retry
every 100 ms), sends HELLO and a SURFACE memfd, then animates phases with
DAMAGE messages and logs every input message it receives:

  openbios  black frame with sparse dim "firmware text" (must NOT reveal)
  gray      Mac OS startup gray with a happy-Mac box (reveals: gray rule)
  desktop   platinum desktop, moving window, cursor at the last POINTER_ABS
  modes     optional surface mode switches (--modes 800x600,1024x768)

  fake_qemu.py --openbios 3 --gray 2 --desktop 10
  fake_qemu.py --follow-resize --desktop 60          # obey presenter RESIZE
  fake_qemu.py --cycles 3 --desktop 4                # disconnect/reconnect
  fake_qemu.py --paint-ms 120                        # gray painted top-down (4.3)
  fake_qemu.py --end-black 2                         # Mac OS shutdown screen, then exit
  fake_qemu.py --icon-delay-ms 120                   # plain gray, then the startup icon where Mac OS
                                                     # draws its Happy Mac ((w-32)/2, (h-32)/2)
  fake_qemu.py --dither                              # a built-in LCD's startup background (Mac OS's
                                                     # 1-pixel 50 % dither, black at the origin)
"""
import argparse
import array
import json
import mmap
import os
import socket
import struct
import sys
import time

import numpy as np

QNAMES = ("unmapped shift shift_r alt alt_r ctrl ctrl_r menu esc 1 2 3 4 5 6 7 8 9 0 minus equal backspace tab "
          "q w e r t y u i o p bracket_left bracket_right ret a s d f g h j k l semicolon apostrophe grave_accent "
          "backslash z x c v b n m comma dot slash asterisk spc caps_lock f1 f2 f3 f4 f5 f6 f7 f8 f9 f10 num_lock "
          "scroll_lock kp_divide kp_multiply kp_subtract kp_add kp_enter kp_decimal sysrq kp_0 kp_1 kp_2 kp_3 kp_4 "
          "kp_5 kp_6 kp_7 kp_8 kp_9 less f11 f12 print home pgup pgdn end left up down right insert delete stop "
          "again props undo front copy open paste find cut lf help meta_l meta_r compose pause ro hiragana henkan "
          "yen muhenkan katakanahiragana kp_comma kp_equals power sleep wake audionext audioprev audiostop "
          "audioplay audiomute volumeup volumedown mediaselect mail calculator computer ac_home ac_back "
          "ac_forward ac_refresh ac_bookmarks lang1 lang2 f13 f14 f15 f16 f17 f18 f19 f20 f21 f22 f23 f24").split()

HELLO, SURFACE, DAMAGE, GUEST_EVENT = 1, 2, 3, 4
KEY, POINTER_ABS, BUTTON, RESIZE, RELEASE_ALL, REFRESH_RATE = 16, 17, 18, 19, 20, 21
XRGB8888 = 0x34325258
F_ADD_SEALS, F_SEAL_SHRINK = 1033, 0x0002

MAC_GRAY = 0x00999999
DESK = 0x006666cc


def log(msg, **kw):
    row = {'t': round(time.monotonic(), 4), 'msg': msg}
    row.update(kw)
    print(json.dumps(row), flush=True)


class Fake:
    def __init__(self, a):
        self.a = a
        self.sock = None
        self.w, self.h = a.size
        self.ptr = (self.w // 2, self.h // 2)
        self.buttons = set()
        self.refresh_ms = 16
        self.seq = 0
        self.resize_req = None
        self.keys_down = set()
        self.inputs = 0

    # --- transport ---
    def connect(self):
        while True:
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
                s.connect(self.a.socket)
                s.setblocking(False)
                self.sock = s
                return
            except OSError:
                s.close()
                time.sleep(0.1)

    def send(self, typ, payload=b'', fd=None):
        data = struct.pack('=II', typ, len(payload)) + payload
        if fd is None:
            self.sock.send(data)
        else:
            self.sock.sendmsg([data], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [fd]))])

    def hello(self):
        session = os.environ.get('RETROBOOT_SESSION', 'fake-qemu').encode()[:40].ljust(40, b'\0')
        self.send(HELLO, struct.pack('=II', 1, os.getpid()) + session)
        log('HELLO sent', pid=os.getpid())

    def surface(self, w, h):
        self.w, self.h = w, h
        stride = w * 4
        size = stride * h
        fd = os.memfd_create('rb-fake-surface', os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
        os.ftruncate(fd, size)
        try:
            import fcntl
            fcntl.fcntl(fd, F_ADD_SEALS, F_SEAL_SHRINK)
        except OSError:
            pass
        self.mm = mmap.mmap(fd, size)
        self.px = np.frombuffer(self.mm, dtype=np.uint32).reshape(h, w)
        self.px[:] = 0
        self.send(SURFACE, struct.pack('=IIIIQ', w, h, stride, XRGB8888, size), fd=fd)
        os.close(fd)
        self.ptr = (min(self.ptr[0], w - 1), min(self.ptr[1], h - 1))
        log('SURFACE sent', w=w, h=h)

    def damage(self, rects):
        self.seq += 1
        payload = struct.pack('=II', self.seq, len(rects))
        for x, y, w, h in rects:
            payload += struct.pack('=iiii', x, y, w, h)
        try:
            self.send(DAMAGE, payload)
        except BlockingIOError:
            pass  # like QEMU: drop damage when the socket is full

    def poll_input(self):
        while True:
            try:
                data = self.sock.recv(64)
            except BlockingIOError:
                return True
            except OSError as e:
                log('socket error', error=str(e))
                return False
            if not data:
                log('presenter closed the connection')
                return False
            typ, ln = struct.unpack_from('=II', data)
            v = struct.unpack_from('=' + 'I' * (ln // 4), data, 8) if ln else ()
            self.inputs += 1
            if typ == KEY:
                name = QNAMES[v[0]] if v[0] < len(QNAMES) else str(v[0])
                (self.keys_down.add if v[1] else self.keys_down.discard)(name)
                log('KEY', qcode=v[0], name=name, down=v[1])
            elif typ == POINTER_ABS:
                self.ptr = (v[0], v[1])
                self.pointer_msgs = getattr(self, 'pointer_msgs', 0) + 1
                if self.a.log_motion:
                    log('POINTER_ABS', x=v[0], y=v[1])
            elif typ == BUTTON:
                (self.buttons.add if v[1] else self.buttons.discard)(v[0])
                log('BUTTON', button=v[0], down=v[1], x=self.ptr[0], y=self.ptr[1])
            elif typ == RESIZE:
                log('RESIZE', w=v[0], h=v[1])
                self.resize_req = (v[0], v[1])
            elif typ == RELEASE_ALL:
                log('RELEASE_ALL', held_keys=sorted(self.keys_down), held_buttons=sorted(self.buttons))
                self.keys_down.clear()
                self.buttons.clear()
            elif typ == REFRESH_RATE:
                self.refresh_ms = max(1, v[0])
                log('REFRESH_RATE', interval_ms=v[0])
            else:
                log('unknown message', type=typ)

    # --- drawing ---
    def fill(self, x, y, w, h, c):
        self.px[max(0, y):max(0, y + h), max(0, x):max(0, x + w)] = c

    def draw_openbios(self, frame):
        self.px[:] = 0
        for i in range(min(8, frame // 10 + 1)):
            self.fill(16, 16 + i * 14, 8 * (10 + (i * 7) % 30), 8, 0x00808080)
        return [(0, 0, self.w, self.h)]

    def startup_bg(self, y0=0, y1=None):
        """Mac OS's startup background: the gray, or with --dither the 50 %
        dither it draws on a built-in LCD (QuickDraw's gray pattern, black at
        the origin)."""
        y1 = self.h if y1 is None else y1
        if not self.a.dither:
            self.px[y0:y1, :] = MAC_GRAY
            return
        y, x = np.mgrid[y0:y1, 0:self.w]
        self.px[y0:y1, :] = np.where(((x + y) & 1) == 1, 0x00ffffff, 0).astype(np.uint32)

    def draw_gray(self):
        self.startup_bg()
        if self.a.icon_delay_ms > 0:
            return [(0, 0, self.w, self.h)]
        cx, cy = self.w // 2, self.h // 2
        self.fill(cx - 16, cy - 20, 32, 40, 0)
        self.fill(cx - 13, cy - 17, 26, 20, 0x00ffffff)
        return [(0, 0, self.w, self.h)]

    def draw_startup_icon(self):
        """A stand-in for Mac OS's startup icon, in its 32 x 32 rectangle."""
        x, y = (self.w - 32) // 2, (self.h - 32) // 2
        self.fill(x + 3, y, 25, 32, 0)
        self.fill(x + 4, y + 1, 23, 26, 0x00cccccc)
        self.fill(x + 7, y + 4, 17, 13, 0x00ccccff)
        self.fill(x + 11, y + 7, 1, 2, 0)
        self.fill(x + 19, y + 7, 1, 2, 0)
        self.fill(x + 12, y + 13, 7, 1, 0)
        return [(x, y, 32, 32)]

    def draw_desktop_base(self):
        y, x = np.mgrid[0:self.h, 0:self.w]
        self.px[:] = np.where(((x + y) % 4) == 0, 0x00333399, DESK).astype(np.uint32)
        self.fill(0, 0, self.w, 20, 0x00eeeeee)
        self.fill(0, 20, self.w, 1, 0)
        for i, wdt in enumerate((36, 40, 36, 44, 56)):
            self.fill(20 + i * 70, 6, wdt, 8, 0)
        return [(0, 0, self.w, self.h)]

    def draw_window(self, pos, erase=None):
        rects = []
        ww, wh = min(240, self.w // 3), min(160, self.h // 3)
        if erase is not None:
            ex, ey = erase
            y, x = np.mgrid[ey:ey + wh, ex:ex + ww]
            self.px[ey:ey + wh, ex:ex + ww] = np.where(((x + y) % 4) == 0, 0x00333399, DESK).astype(np.uint32)
            rects.append((ex, ey, ww, wh))
        x0, y0 = pos
        self.fill(x0, y0, ww, wh, 0)
        self.fill(x0 + 1, y0 + 1, ww - 2, wh - 2, 0x00dddddd)
        self.fill(x0 + 1, y0 + 1, ww - 2, 18, 0x00bbbbbb)
        rects.append((x0, y0, ww, wh))
        return rects

    def draw_cursor(self, pos, erase_under=None):
        x, y = pos
        x, y = min(max(0, x), self.w - 1), min(max(0, y), self.h - 1)
        saved = self.px[y:y + 12, x:x + 8].copy()
        for i in range(12):
            self.fill(x, y + i, min(i + 1, 8), 1, 0x00000000 if i in (0, 11) else 0x00ffffff)
            self.fill(x, y + i, 1, 1, 0)
        return (x, y, saved)

    # --- phases ---
    def run_once(self):
        a = self.a
        self.connect()
        log('connected', socket=a.socket)
        self.hello()
        self.surface(*a.size)
        period = lambda: self.refresh_ms / 1000.0
        frame = 0
        t_end = time.monotonic() + a.openbios
        log('phase openbios')
        while time.monotonic() < t_end:
            if not self.poll_input():
                return False
            self.damage(self.draw_openbios(frame))
            frame += 1
            time.sleep(period())
        log('phase gray')
        self.send(GUEST_EVENT, json.dumps({'event': 'fake-startup-gray'}).encode())
        if a.paint_ms > 0:
            # Mac OS paints its startup gray top-down: one band per refresh.
            bands = max(1, int(a.paint_ms / max(1, self.refresh_ms)))
            for b in range(bands):
                y0, y1 = self.h * b // bands, self.h * (b + 1) // bands
                self.startup_bg(y0, y1)
                self.damage([(0, y0, self.w, y1 - y0)])
                if not self.poll_input():
                    return False
                time.sleep(period())
            log('gray painted', bands=bands)
        self.damage(self.draw_gray())
        if a.icon_delay_ms > 0:
            t_icon = time.monotonic() + a.icon_delay_ms / 1000.0
            while time.monotonic() < t_icon:
                if not self.poll_input():
                    return False
                time.sleep(period())
            self.damage(self.draw_startup_icon())
            log('startup icon drawn')
        t_end = time.monotonic() + a.gray
        while time.monotonic() < t_end:
            if not self.poll_input():
                return False
            time.sleep(period())
        log('phase desktop')
        modes = list(a.modes)
        mode_at = time.monotonic() + (a.desktop / (len(modes) + 1) if modes else 1e9)
        self.damage(self.draw_desktop_base())
        pos, vel = [40, 40], [3, 2]
        self.draw_window(tuple(pos))
        cur = None
        t_end = time.monotonic() + a.desktop
        while time.monotonic() < t_end:
            if not self.poll_input():
                return False
            if self.resize_req and a.follow_resize and self.resize_req != (self.w, self.h):
                w, h = self.resize_req
                self.surface(w, h)
                self.damage(self.draw_desktop_base())
                pos = [40, 40]
                cur = None
            if time.monotonic() >= mode_at and modes:
                w, h = modes.pop(0)
                self.surface(w, h)
                self.damage(self.draw_desktop_base())
                pos, cur = [40, 40], None
                mode_at = time.monotonic() + a.desktop / (len(a.modes) + 1)
            rects = []
            if a.fullscreen:
                # worst case: every pixel changes every refresh
                self.px[:] = np.roll(self.px, 3, axis=1)
                self.damage([(0, 0, self.w, self.h)])
                time.sleep(period())
                continue
            if cur is not None:
                cx, cy, saved = cur
                self.px[cy:cy + saved.shape[0], cx:cx + saved.shape[1]] = saved
                rects.append((cx, cy, saved.shape[1], saved.shape[0]))
            if not a.static:
                old = tuple(pos)
                ww, wh = min(240, self.w // 3), min(160, self.h // 3)
                for i in (0, 1):
                    pos[i] += vel[i]
                    lim = (self.w - ww, self.h - wh)[i]
                    if pos[i] < 22 or pos[i] > lim:
                        vel[i] = -vel[i]
                        pos[i] = max(22, min(lim, pos[i]))
                rects += self.draw_window(tuple(pos), erase=old)
            cur = self.draw_cursor(self.ptr)
            rects.append((cur[0], cur[1], 8, 12))
            self.damage(rects)
            time.sleep(period())
        if a.end_black > 0:
            log('phase shutdown (black)')
            self.px[:] = 0
            self.damage([(0, 0, self.w, self.h)])
            t_end = time.monotonic() + a.end_black
            while time.monotonic() < t_end:
                if not self.poll_input():
                    return False
                time.sleep(period())
        log('phase end', inputs=self.inputs)
        return True

    def run(self):
        for cycle in range(self.a.cycles):
            log('cycle', n=cycle + 1)
            self.run_once()
            if self.sock:
                self.sock.close()
                self.sock = None
            log('disconnected')
            if cycle + 1 < self.a.cycles:
                time.sleep(self.a.gap)


def size(s):
    w, h = s.lower().split('x')
    return int(w), int(h)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--socket', default='/run/retroboot/display.sock')
    ap.add_argument('--size', type=size, default=(640, 480))
    ap.add_argument('--openbios', type=float, default=3.0)
    ap.add_argument('--gray', type=float, default=2.0)
    ap.add_argument('--desktop', type=float, default=10.0)
    ap.add_argument('--modes', type=lambda s: [size(x) for x in s.split(',') if x], default=[])
    ap.add_argument('--follow-resize', action='store_true')
    ap.add_argument('--static', action='store_true', help='no moving window (idle desktop)')
    ap.add_argument('--fullscreen', action='store_true', help='full-surface change every refresh (stress)')
    ap.add_argument('--cycles', type=int, default=1)
    ap.add_argument('--gap', type=float, default=1.0, help='seconds between cycles')
    ap.add_argument('--log-motion', action='store_true')
    ap.add_argument('--paint-ms', type=float, default=0, help='paint the startup gray top-down over this time')
    ap.add_argument('--end-black', type=float, default=0, help='show black for this long before disconnecting')
    ap.add_argument('--dither', action='store_true',
                    help="a built-in LCD's startup background: Mac OS's 1-pixel 50 %% dither instead of the gray")
    ap.add_argument('--icon-delay-ms', type=float, default=0,
                    help='plain gray first, then the startup icon this much later (Mac OS: ~120-800 ms)')
    a = ap.parse_args()
    try:
        Fake(a).run()
    except KeyboardInterrupt:
        pass
