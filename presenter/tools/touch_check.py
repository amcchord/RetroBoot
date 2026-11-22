#!/usr/bin/env python3
"""Touch and rotation checks against the running presenter and a real Mac OS
guest (lab tool, root, rb-dev; ARCHITECTURE 4.5, 4.8).

Talks to the presenter's control socket as hostd would (run it without
hostd, or it replaces hostd's connection), drives a virtual touchscreen and
pen (uinput_touch.py) and reads Mac OS's own cursor back over QMP (low
memory RawMouse 0x82C / MTemp 0x828, as research/mouse/mouse_lab.py does).

  touch_check.py RUN status
  touch_check.py RUN rotate 90               # display_config rotation=90; waits for Mac OS's new screen
  touch_check.py RUN where                   # Mac OS's cursor
  touch_check.py RUN to-scanout MX MY        # where Mac pixel (MX, MY) is on the panel
  touch_check.py RUN accuracy --count 40 [--touch]   # pen hover (or finger) on random Mac pixels
  touch_check.py RUN gesture tap MX MY       # a gesture at Mac pixel coordinates (uinput_touch.py words)
  touch_check.py RUN shot PATH [composed|guest]
  touch_check.py RUN keys meta_l-w           # QEMU sendkey (HMP) to Mac OS
  touch_check.py RUN press MX MY SECONDS SHOT   # finger down, a guest screenshot while held, up

RUN is a real_qemu.py run directory (for its qmp-control.sock).
"""
from __future__ import annotations

import argparse
import json
import random
import socket
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import uinput_touch  # noqa: E402


class Ctl:
    def __init__(self, path='/run/retroboot/presenter.sock'):
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.connect(path)
        self.f = self.s.makefile('rw', buffering=1)
        self.n = 0

    def __call__(self, cmd, **kw):
        self.n += 1
        self.f.write(json.dumps({'id': self.n, 'cmd': cmd, **kw}) + '\n')
        self.f.flush()
        while True:
            r = json.loads(self.f.readline())
            if r.get('id') == self.n:
                if not r.get('ok'):
                    raise RuntimeError(r.get('error'))
                return r


def qmp_hmp(run: Path, line: str) -> str:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    s.connect(str(run / 'qmp-control.sock'))
    f = s.makefile('rw', buffering=1)
    json.loads(f.readline())
    out = None
    for c in ({'execute': 'qmp_capabilities'},
              {'execute': 'human-monitor-command', 'arguments': {'command-line': line}}):
        f.write(json.dumps(c) + '\n')
        f.flush()
        while True:
            r = json.loads(f.readline())
            if 'event' not in r:
                out = r.get('return')
                break
    s.close()
    return out


def mac_cursor(run: Path, w: int, h: int):
    """(x, y) of RawMouse and MTemp as Mac OS sees them."""
    sv = lambda v: v - 0x10000 if v & 0x8000 else v  # noqa: E731
    r = None
    for _ in range(20):
        out = qmp_hmp(run, 'x /2wx 0x828')
        words = [int(t, 16) for t in out.split(':', 1)[1].split()]
        mt, raw = words[0], words[1]
        r = (sv(raw & 0xFFFF), sv(raw >> 16), sv(mt & 0xFFFF), sv(mt >> 16))
        if 0 <= r[0] < w and 0 <= r[1] < h and 0 <= r[2] < w and 0 <= r[3] < h:
            return r
        time.sleep(0.05)
    return r


def geometry(ctl: Ctl) -> dict:
    st = ctl('status')
    d, lay = st['display'], st.get('layout') or {}
    return {'rot': d['rotation'], 'user_rot': d.get('user_rotation'), 'W': d['mode'][0], 'H': d['mode'][1],
            'fb': d['native_mode'][:2], 'k': lay.get('k'), 'dst': lay.get('dst'), 'src': lay.get('src'),
            'mode': lay.get('mode'), 'surface': (st.get('guest') or {}).get('surface')}


def mac_to_scanout(g: dict, mx: int, my: int) -> tuple[int, int]:
    """The scanout pixel at the middle of Mac pixel (mx, my): layout, then the
    output's rotation (xform.c rb_xform_l2f)."""
    k, (dx, dy, _, _) = g['k'], g['dst']
    lx, ly = dx + mx * k + k // 2, dy + my * k + k // 2
    ow, oh, r = g['W'], g['H'], g['rot']
    if r == 90:
        return oh - 1 - ly, lx
    if r == 180:
        return ow - 1 - lx, oh - 1 - ly
    if r == 270:
        return ly, ow - 1 - lx
    return lx, ly


def cmd_rotate(a, ctl):
    want = int(a.rotation)
    ctl('display_config', rotation=want)
    t0 = time.monotonic()
    while time.monotonic() - t0 < 15:
        g = geometry(ctl)
        if g['user_rot'] == want and g['surface'] and g['src'] == g['surface'] and \
                (g['surface'][0] > g['surface'][1]) == (g['W'] > g['H']):
            print(json.dumps({'rotation': want, 'after_s': round(time.monotonic() - t0, 2), **g}))
            return 0
        time.sleep(0.1)
    print(json.dumps({'rotation': want, 'timeout': True, **geometry(ctl)}))
    return 1


def cmd_accuracy(a, ctl):
    g = geometry(ctl)
    sw, sh = g['surface']
    fbw, fbh = g['fb']
    rnd = random.Random(a.seed)
    # empty desktop (no icons on the right, no menu bar, no control strip)
    targets = [(rnd.randrange(16, sw * 2 // 3), rnd.randrange(40, sh - 60)) for _ in range(a.count)]
    targets += [(16, 40), (sw * 2 // 3, sh - 61)]
    dev = uinput_touch.Dev('RetroBoot Lab Touchscreen' if a.touch else 'RetroBoot Lab Pen', fbw, fbh,
                           pen=not a.touch)
    time.sleep(1.5)
    bad, rows = 0, []
    try:
        for mx, my in targets:
            fx, fy = mac_to_scanout(g, mx, my)
            if a.touch:
                dev.t_down(0, fx, fy)
                dev.sync()
                time.sleep(a.settle)
                got = mac_cursor(Path(a.run), sw, sh)
                dev.t_up(0)       # a tap on empty desktop: harmless
                dev.sync()
                time.sleep(0.6)   # past the double-tap window
            else:
                dev.p_stream(fx, fy, a.settle * 1000)   # hovering, as real pens report
                got = mac_cursor(Path(a.run), sw, sh)
            ok = got[:2] == (mx, my) and got[2:] == (mx, my)
            bad += not ok
            rows.append({'mac': [mx, my], 'scanout': [fx, fy], 'raw': list(got[:2]), 'mtemp': list(got[2:]),
                         'ok': ok})
        if not a.touch:
            dev.p_pos(0, 0, prox=False)
            dev.sync()
    finally:
        dev.close()
    print(json.dumps({'rotation': g['rot'], 'user_rotation': g['user_rot'], 'k': g['k'], 'surface': g['surface'],
                      'device': 'touch' if a.touch else 'pen', 'samples': len(rows), 'exact': len(rows) - bad,
                      'bad': [r for r in rows if not r['ok']][:5]}))
    return 0 if not bad else 1


def cmd_gesture(a, ctl):
    g = geometry(ctl)
    words = a.words
    op, nums = words[0], [int(w) for w in words[1:]]
    # Mac coordinates -> scanout for the positional arguments of each gesture
    pos = {'tap': 1, 'double-tap': 1, 'hold': 1, 'two-tap': 1, 'pen-tap': 1, 'pen-hover': 1, 'pen-barrel': 1,
           'drag': 2, 'pen-drag': 2, 'hold-drag': 2, 'two-drag': 1}[op]
    out = []
    for i in range(pos):
        out += list(mac_to_scanout(g, nums[2 * i], nums[2 * i + 1]))
    rest = nums[2 * pos:]
    if op == 'two-drag':   # DX DY in Mac pixels -> a scanout vector
        x0, y0 = mac_to_scanout(g, nums[0], nums[1])
        x1, y1 = mac_to_scanout(g, nums[0] + rest[0], nums[1] + rest[1])
        rest = [x1 - x0, y1 - y0] + rest[2:]
    fbw, fbh = g['fb']
    pen = op.startswith('pen')
    dev = uinput_touch.Dev('RetroBoot Lab Pen' if pen else 'RetroBoot Lab Touchscreen', fbw, fbh, pen=pen)
    time.sleep(1.5)
    try:
        uinput_touch.play(None if pen else dev, dev if pen else None, [op] + [str(v) for v in out + rest])
        time.sleep(0.5)
    finally:
        dev.close()
    print(json.dumps({'gesture': op, 'mac': nums, 'scanout': out + rest, 'rotation': g['rot']}))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('run')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('status')
    r = sub.add_parser('rotate')
    r.add_argument('rotation')
    sub.add_parser('where')
    m = sub.add_parser('to-scanout')
    m.add_argument('mx', type=int)
    m.add_argument('my', type=int)
    ac = sub.add_parser('accuracy')
    ac.add_argument('--count', type=int, default=30)
    ac.add_argument('--seed', type=int, default=1)
    ac.add_argument('--settle', type=float, default=0.35)
    ac.add_argument('--touch', action='store_true')
    ge = sub.add_parser('gesture')
    ge.add_argument('words', nargs='+')
    k = sub.add_parser('keys')
    k.add_argument('keys')
    pr = sub.add_parser('press')
    pr.add_argument('mx', type=int)
    pr.add_argument('my', type=int)
    pr.add_argument('seconds', type=float)
    pr.add_argument('shot')
    pr.add_argument('--to', type=int, nargs=2, help='slide here (Mac pixels) before lifting')
    sh = sub.add_parser('shot')
    sh.add_argument('path')
    sh.add_argument('what', nargs='?', default='composed')
    a = ap.parse_args()
    ctl = Ctl()
    if a.cmd == 'status':
        print(json.dumps(geometry(ctl)))
    elif a.cmd == 'rotate':
        return cmd_rotate(a, ctl)
    elif a.cmd == 'where':
        g = geometry(ctl)
        print(json.dumps(mac_cursor(Path(a.run), *g['surface'])))
    elif a.cmd == 'to-scanout':
        print(json.dumps(mac_to_scanout(geometry(ctl), a.mx, a.my)))
    elif a.cmd == 'accuracy':
        return cmd_accuracy(a, ctl)
    elif a.cmd == 'gesture':
        return cmd_gesture(a, ctl)
    elif a.cmd == 'keys':
        print(qmp_hmp(Path(a.run), f'sendkey {a.keys}'))
    elif a.cmd == 'press':
        g = geometry(ctl)
        dev = uinput_touch.Dev('RetroBoot Lab Touchscreen', *g['fb'])
        time.sleep(1.5)
        try:
            dev.t_down(0, *mac_to_scanout(g, a.mx, a.my))
            dev.sync()
            time.sleep(a.seconds)
            ctl('screenshot', path=a.shot, what='guest')
            time.sleep(0.8)
            if a.to:
                x0, y0 = mac_to_scanout(g, a.mx, a.my)
                x1, y1 = mac_to_scanout(g, *a.to)
                for i in range(1, 21):
                    dev.t_move(0, x0 + (x1 - x0) * i // 20, y0 + (y1 - y0) * i // 20)
                    dev.sync()
                    time.sleep(0.03)
                time.sleep(0.4)
            dev.t_up(0)
            dev.sync()
            time.sleep(0.3)
        finally:
            dev.close()
        print(a.shot)
    elif a.cmd == 'shot':
        ctl('screenshot', path=a.path, what=a.what)
        time.sleep(1.0)
        print(a.path)
    return 0


if __name__ == '__main__':
    sys.exit(main())
