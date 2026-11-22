#!/usr/bin/env python3
"""Virtual touchscreen and pen for rb-presenter (lab tool, root, /dev/uinput).

Creates a multitouch touchscreen (type B slots, INPUT_PROP_DIRECT) and/or a
pen tablet (BTN_TOOL_PEN, tip, barrel buttons, INPUT_PROP_DIRECT) whose axis
ranges are the panel's scanout size, so a coordinate is a scanout pixel
(libinput maps 0..W-1 onto the scanout; ARCHITECTURE 4.8). Then plays a
script of gestures, one per argument group separated by ',':

  uinput_touch.py --size 1280x800 tap 100 200 , hold 300 300 1000 , two-tap 400 400 \\
                  , drag 10 10 300 300 600 , two-drag 640 400 0 -240 800 , wait 2
  uinput_touch.py --size 1280x800 --pen pen-tap 100 200 , pen-hover 300 300 , pen-barrel 300 300

Gestures (times in ms):
  tap X Y                     one finger down and up (80 ms)
  double-tap X Y              two taps 150 ms apart
  hold X Y MS                 one finger held still
  drag X0 Y0 X1 Y1 MS         one finger moved in 20 steps
  two-tap X Y                 two fingers 40 px apart, down and up (120 ms)
  two-drag X Y DX DY MS       two fingers 40 px apart moved together
  down SLOT X Y / move SLOT X Y / up SLOT    raw contacts (then 'sync')
  pen-tap X Y / pen-hover X Y / pen-barrel X Y / pen-drag X0 Y0 X1 Y1 MS
  wait S                      sleep
  --keep S                    keep the devices S seconds after the script
                              (libinput sees them before the first gesture)
"""
from __future__ import annotations

import argparse
import fcntl
import os
import struct
import sys
import time

UI_SET_EVBIT, UI_SET_KEYBIT, UI_SET_ABSBIT, UI_SET_PROPBIT = 0x40045564, 0x40045565, 0x40045567, 0x4004556e
UI_SET_MSCBIT = 0x40045568
EV_MSC, MSC_TIMESTAMP = 4, 5
UI_DEV_SETUP, UI_DEV_CREATE, UI_DEV_DESTROY, UI_ABS_SETUP = 0x405c5503, 0x5501, 0x5502, 0x401c5504
EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
SYN_REPORT = 0
BTN_TOUCH, BTN_TOOL_FINGER, BTN_TOOL_DOUBLETAP, BTN_TOOL_PEN = 0x14a, 0x145, 0x14d, 0x140
BTN_STYLUS, BTN_STYLUS2 = 0x14b, 0x14c
ABS_X, ABS_Y, ABS_PRESSURE = 0x00, 0x01, 0x18
ABS_MT_SLOT, ABS_MT_POSITION_X, ABS_MT_POSITION_Y, ABS_MT_TRACKING_ID = 0x2f, 0x35, 0x36, 0x39
INPUT_PROP_DIRECT = 0x01
BUS_I2C, BUS_USB = 0x18, 0x03
SLOTS = 10


def abs_setup(fd, code, lo, hi, res=0):
    fcntl.ioctl(fd, UI_SET_ABSBIT, code)
    fcntl.ioctl(fd, UI_ABS_SETUP, struct.pack('HHiiiiii', code, 0, 0, lo, hi, 0, 0, res))


class Dev:
    def __init__(self, name, w, h, pen=False, bus=BUS_I2C):
        self.fd = os.open('/dev/uinput', os.O_WRONLY | os.O_NONBLOCK)
        fd = self.fd
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_ABS)
        fcntl.ioctl(fd, UI_SET_PROPBIT, INPUT_PROP_DIRECT)
        keys = (BTN_TOOL_PEN, BTN_TOUCH, BTN_STYLUS, BTN_STYLUS2) if pen else (BTN_TOUCH, BTN_TOOL_FINGER,
                                                                               BTN_TOOL_DOUBLETAP)
        for k in keys:
            fcntl.ioctl(fd, UI_SET_KEYBIT, k)
        res = 10 if pen else 4    # units per mm (libinput needs a resolution for tablets)
        abs_setup(fd, ABS_X, 0, w - 1, res)
        abs_setup(fd, ABS_Y, 0, h - 1, res)
        if pen:
            abs_setup(fd, ABS_PRESSURE, 0, 1023)
            # real pens stamp every report (the kernel drops reports that change nothing)
            fcntl.ioctl(fd, UI_SET_EVBIT, EV_MSC)
            fcntl.ioctl(fd, UI_SET_MSCBIT, MSC_TIMESTAMP)
        else:
            abs_setup(fd, ABS_MT_SLOT, 0, SLOTS - 1)
            abs_setup(fd, ABS_MT_TRACKING_ID, 0, 65535)
            abs_setup(fd, ABS_MT_POSITION_X, 0, w - 1, res)
            abs_setup(fd, ABS_MT_POSITION_Y, 0, h - 1, res)
        fcntl.ioctl(fd, UI_DEV_SETUP, struct.pack('HHHH80sI', bus, 0x1209, 0x7a10 if pen else 0x7a11, 1,
                                                  name.encode(), 0))
        fcntl.ioctl(fd, UI_DEV_CREATE)
        self.tid = 100
        self.down = {}

    def ev(self, typ, code, val):
        os.write(self.fd, struct.pack('llHHi', 0, 0, typ, code, val))

    def sync(self):
        self.ev(EV_SYN, SYN_REPORT, 0)

    # -- touch (type B) --
    def contacts(self):
        n = len(self.down)
        self.ev(EV_KEY, BTN_TOUCH, 1 if n else 0)
        self.ev(EV_KEY, BTN_TOOL_FINGER, 1 if n == 1 else 0)
        self.ev(EV_KEY, BTN_TOOL_DOUBLETAP, 1 if n == 2 else 0)

    def t_down(self, slot, x, y):
        self.ev(EV_ABS, ABS_MT_SLOT, slot)
        self.tid += 1
        self.ev(EV_ABS, ABS_MT_TRACKING_ID, self.tid)
        self.ev(EV_ABS, ABS_MT_POSITION_X, x)
        self.ev(EV_ABS, ABS_MT_POSITION_Y, y)
        self.down[slot] = (x, y)
        self.contacts()
        first = min(self.down)
        self.ev(EV_ABS, ABS_X, self.down[first][0])
        self.ev(EV_ABS, ABS_Y, self.down[first][1])

    def t_move(self, slot, x, y):
        self.ev(EV_ABS, ABS_MT_SLOT, slot)
        self.ev(EV_ABS, ABS_MT_POSITION_X, x)
        self.ev(EV_ABS, ABS_MT_POSITION_Y, y)
        self.down[slot] = (x, y)
        if slot == min(self.down):
            self.ev(EV_ABS, ABS_X, x)
            self.ev(EV_ABS, ABS_Y, y)

    def t_up(self, slot):
        self.ev(EV_ABS, ABS_MT_SLOT, slot)
        self.ev(EV_ABS, ABS_MT_TRACKING_ID, -1)
        self.down.pop(slot, None)
        self.contacts()

    # -- pen --
    def p_pos(self, x, y, prox=True, tip=False, pressure=0):
        self.ev(EV_MSC, MSC_TIMESTAMP, int(time.monotonic() * 1e6) & 0x7fffffff)
        self.ev(EV_KEY, BTN_TOOL_PEN, 1 if prox else 0)
        self.ev(EV_ABS, ABS_X, x)
        self.ev(EV_ABS, ABS_Y, y)
        self.ev(EV_ABS, ABS_PRESSURE, pressure)
        self.ev(EV_KEY, BTN_TOUCH, 1 if tip else 0)

    def p_stream(self, x, y, ms_, **kw):
        """Hold the pen for ms_ milliseconds, reporting every 8 ms as real pens
        do (libinput forces a proximity-out after 50 ms without events on
        non-Wacom tablets)."""
        end = time.monotonic() + ms_ / 1000.0
        while True:
            self.p_pos(x, y, **kw)
            self.sync()
            if time.monotonic() >= end:
                break
            time.sleep(0.008)

    def close(self):
        fcntl.ioctl(self.fd, UI_DEV_DESTROY)
        os.close(self.fd)


def ms(v):
    time.sleep(float(v) / 1000.0)


def play(touch: Dev | None, pen: Dev | None, words: list[str]) -> None:
    op, a = words[0], [int(float(w)) if w.lstrip('-').replace('.', '', 1).isdigit() else w for w in words[1:]]
    t = touch
    if op == 'tap':
        t.t_down(0, a[0], a[1]); t.sync(); ms(80); t.t_up(0); t.sync()
    elif op == 'double-tap':
        for _ in range(2):
            t.t_down(0, a[0], a[1]); t.sync(); ms(70); t.t_up(0); t.sync(); ms(150)
    elif op == 'hold':
        t.t_down(0, a[0], a[1]); t.sync(); ms(a[2]); t.t_up(0); t.sync()
    elif op == 'hold-drag':   # long press, then slide to X1 Y1 and lift (contextual menu item)
        x0, y0, x1, y1, hold, dur = a[:6]
        t.t_down(0, x0, y0); t.sync(); ms(hold)
        for i in range(1, 21):
            t.t_move(0, x0 + (x1 - x0) * i // 20, y0 + (y1 - y0) * i // 20); t.sync(); ms(dur / 20)
        ms(150); t.t_up(0); t.sync()
    elif op == 'drag':
        x0, y0, x1, y1, dur = a[:5]
        t.t_down(0, x0, y0); t.sync(); ms(40)
        for i in range(1, 21):
            t.t_move(0, x0 + (x1 - x0) * i // 20, y0 + (y1 - y0) * i // 20); t.sync(); ms(dur / 20)
        ms(60); t.t_up(0); t.sync()
    elif op == 'two-tap':
        t.t_down(0, a[0], a[1]); t.sync(); ms(20); t.t_down(1, a[0] + 40, a[1]); t.sync(); ms(100)
        t.t_up(0); t.sync(); ms(10); t.t_up(1); t.sync()
    elif op == 'two-drag':
        x, y, dx, dy, dur = a[:5]
        t.t_down(0, x, y); t.sync(); ms(15); t.t_down(1, x + 40, y); t.sync(); ms(30)
        for i in range(1, 31):
            t.t_move(0, x + dx * i // 30, y + dy * i // 30)
            t.t_move(1, x + 40 + dx * i // 30, y + dy * i // 30); t.sync(); ms(dur / 30)
        ms(40); t.t_up(0); t.t_up(1); t.sync()
    elif op == 'down':
        t.t_down(a[0], a[1], a[2])
    elif op == 'move':
        t.t_move(a[0], a[1], a[2])
    elif op == 'up':
        t.t_up(a[0])
    elif op == 'sync':
        t.sync()
    elif op == 'pen-hover':
        pen.p_stream(a[0], a[1], 300)
    elif op == 'pen-tap':
        pen.p_stream(a[0], a[1], 60)
        pen.p_stream(a[0], a[1], 80, tip=True, pressure=600)
        pen.p_stream(a[0], a[1], 60)
        pen.p_pos(a[0], a[1], prox=False); pen.sync()
    elif op == 'pen-drag':
        x0, y0, x1, y1, dur = a[:5]
        pen.p_stream(x0, y0, 60)
        pen.p_stream(x0, y0, 40, tip=True, pressure=600)
        for i in range(1, 21):
            pen.p_stream(x0 + (x1 - x0) * i // 20, y0 + (y1 - y0) * i // 20, dur / 20, tip=True, pressure=600)
        pen.p_stream(x1, y1, 40)
        pen.p_pos(x1, y1, prox=False); pen.sync()
    elif op == 'pen-barrel':
        pen.p_stream(a[0], a[1], 80)
        pen.ev(EV_KEY, BTN_STYLUS, 1)
        pen.p_stream(a[0], a[1], 120)
        pen.ev(EV_KEY, BTN_STYLUS, 0)
        pen.p_stream(a[0], a[1], 60)
        pen.p_pos(a[0], a[1], prox=False); pen.sync()
    elif op == 'wait':
        time.sleep(float(words[1]))
    else:
        raise SystemExit(f'unknown gesture {op!r}')


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--size', default='1280x800', help='scanout WxH (the axis ranges)')
    ap.add_argument('--pen', action='store_true', help='also create the pen tablet')
    ap.add_argument('--no-touch', action='store_true')
    ap.add_argument('--usb', action='store_true', help='USB bus (a touch monitor) instead of I2C')
    ap.add_argument('--name', default='RetroBoot Lab Touchscreen')
    ap.add_argument('--settle', type=float, default=1.5, help='seconds for libinput to add the devices')
    ap.add_argument('--keep', type=float, default=0.5)
    ap.add_argument('script', nargs='*')
    a = ap.parse_args()
    w, h = (int(v) for v in a.size.split('x'))
    touch = None if a.no_touch else Dev(a.name, w, h, bus=BUS_USB if a.usb else BUS_I2C)
    pen = Dev('RetroBoot Lab Pen', w, h, pen=True) if a.pen else None
    time.sleep(a.settle)
    groups, cur = [], []
    for word in a.script:
        if word == ',':
            groups.append(cur)
            cur = []
        else:
            cur.append(word)
    if cur:
        groups.append(cur)
    try:
        for g in groups:
            print(time.strftime('%H:%M:%S'), ' '.join(g), flush=True)
            play(touch, pen, g)
            time.sleep(0.15)
        time.sleep(a.keep)
    finally:
        for d in (touch, pen):
            if d:
                d.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
