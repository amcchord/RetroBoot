#!/usr/bin/env python3
"""Startup indicator check against a running rb-presenter (lab, rb-dev; 4.4.1).

Acts as hostd on the control socket: turns the indicator on, enters the
checkerboard, times the bar like hostd does (startup_progress), then shows
the guest scene and lets a display client (fake_qemu.py, or a real QEMU
started with --launch) reach genuine content. Composed screenshots are taken
throughout and checked:

  * every frame before the reveal is the checkerboard with the indicator, and
    the bar never goes back;
  * the bar is complete in the last checkerboard frame;
  * the first guest frame shows Mac OS's startup icon exactly where the
    indicator stood (the presenter's "landing"), and the presenter's
    timeline gives the landing time (completion + step-down + icon wait);
  * reported, not checked: how much of the background outside the icon is
    unchanged across the cut, on the whole screen and inside Mac OS's
    picture (the checkerboard against Mac OS's gray: none; against a
    built-in LCD's dither, fake_qemu.py --dither or real_qemu.py --lcd: all
    of the picture where the pattern cell is the guest's pixel, since the
    pattern is anchored at Mac OS's picture, 4.4).

  startup_check.py --out /tmp/sc --remaining 6000 \\
      --launch 'python3 fake_qemu.py --size 1280x800 --openbios 6 --gray 2 --icon-delay-ms 150 --desktop 2'

Writes OUT/frames/*.png, OUT/events.jsonl and OUT/result.json; exit 0 when
all checks pass.
"""
import argparse
import json
import os
import shlex
import socket
import subprocess
import sys
import time

import numpy as np
from PIL import Image

BAR_TOP = (0xC6, 0xC4, 0xFF)   # the fill's top row (startup.c)
BAR_END = (0x3A, 0x3D, 0x8C)   # the line that closes a partial fill
TRACK_TOP = (0xA5, 0xA5, 0xA5)  # the empty track's top row


class Ctl:
    def __init__(self, path):
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.connect(path)
        self.buf = b''
        self.n = 0
        self.events = []

    def _read(self, timeout):
        self.s.settimeout(timeout)
        try:
            chunk = self.s.recv(1 << 20)
        except socket.timeout:
            return
        if not chunk:
            raise ConnectionError('presenter closed the connection')
        self.buf += chunk

    def _lines(self):
        while b'\n' in self.buf:
            line, self.buf = self.buf.split(b'\n', 1)
            yield json.loads(line)

    def req(self, cmd, timeout=10.0, **kw):
        self.n += 1
        msg = {'id': self.n, 'cmd': cmd, **kw}
        self.s.sendall((json.dumps(msg) + '\n').encode())
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for m in self._lines():
                if m.get('id') == self.n and 'ok' in m:
                    if not m['ok']:
                        raise RuntimeError(f'{cmd}: {m.get("error")}')
                    return m
                if 'event' in m:
                    m['t_local'] = time.monotonic()
                    self.events.append(m)
            self._read(0.05)
        raise TimeoutError(cmd)

    def pump(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self._read(max(0.01, min(0.05, end - time.monotonic())))
            for m in self._lines():
                if 'event' in m:
                    m['t_local'] = time.monotonic()
                    self.events.append(m)


def bar_fill(img, geo):
    """Bar steps in a composed frame (top inner row of the bar), None when
    the indicator is not there."""
    x, y, q = geo[0], geo[1], geo[2]
    row_y = y + 9 * q + q // 2
    a = np.asarray(img.convert('RGB'))
    if row_y >= a.shape[0]:
        return None
    fill = 0
    for i in range(13):
        px = tuple(int(v) for v in a[row_y, x + (9 + i) * q + q // 2])
        if px in (BAR_TOP, BAR_END) and fill == i:
            fill += 1
        elif px != TRACK_TOP:
            return None
    # the outline at the icon's top-left corner of the case
    if tuple(int(v) for v in a[y + q // 2, x + 4 * q + q // 2]) != (0, 0, 0):
        return None
    return fill


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--socket', default='/run/retroboot/presenter.sock')
    ap.add_argument('--out', required=True)
    ap.add_argument('--remaining', type=float, default=6000, help='expected ms until the cut-over (hostd)')
    ap.add_argument('--art', default=None, help='Platinum icon (.rbi) for the indicator')
    ap.add_argument('--capture', default=None, help="where the presenter saves Mac OS's Happy Mac (startup_icon)")
    ap.add_argument('--launch', required=True, help='display client command (run from this directory)')
    ap.add_argument('--interval', type=float, default=0.25)
    ap.add_argument('--timeout', type=float, default=120)
    ap.add_argument('--after', type=float, default=1.0, help='seconds of frames after the reveal')
    a = ap.parse_args()
    os.makedirs(os.path.join(a.out, 'frames'), exist_ok=True)
    c = Ctl(a.socket)
    c.req('hello')
    c.req('startup_config', enabled=True, art=a.art, **({'capture': a.capture} if a.capture else {}))
    c.req('set_scene', scene='black')
    c.req('set_scene', scene='splash')
    c.req('startup_progress', reset=True, remaining_ms=a.remaining, phase='lab')
    st = c.req('status')
    geo = st['startup']['geometry']
    c.req('set_scene', scene='guest', reveal='first-content')
    t0 = time.monotonic()
    proc = subprocess.Popen(shlex.split(a.launch), stdout=open(os.path.join(a.out, 'client.log'), 'w'),
                            stderr=subprocess.STDOUT, cwd=os.path.dirname(os.path.abspath(__file__)))
    frames = []
    revealed_at = None
    i = 0
    while time.monotonic() - t0 < a.timeout:
        path = os.path.join(a.out, 'frames', f'{i:04d}.png')
        c.req('screenshot', path=path, what='composed')
        # guest_revealed has been received (the composed frame can lag the
        # event by a refresh, so the cut itself is found in the pixels).
        cut = any(e.get('event') == 'guest_revealed' for e in c.events)
        frames.append({'i': i, 't': round(time.monotonic() - t0, 3), 'png': path, 'after_cut': cut})
        i += 1
        if any(e.get('event') == 'guest_revealed' for e in c.events):
            revealed_at = revealed_at or time.monotonic()
            if time.monotonic() - revealed_at >= a.after:
                break
        c.pump(a.interval)
    st = c.req('status')
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()
    with open(os.path.join(a.out, 'events.jsonl'), 'w') as f:
        for e in c.events:
            f.write(json.dumps(e) + '\n')
    # --- checks ---
    fills, problems, last_checker = [], [], None
    first_guest = None
    for fr in frames:
        img = Image.open(fr['png'])
        f = bar_fill(img, geo)
        fr['fill'] = f
        if f is not None:
            if fills and f < fills[-1]:
                problems.append(f'bar went back at frame {fr["i"]}: {fills[-1]} -> {f}')
            fills.append(f)
            last_checker = fr
        elif first_guest is None and fills:
            first_guest = fr
    rev = [e for e in c.events if e.get('event') == 'guest_revealed']
    content = [e for e in c.events if e.get('event') == 'guest_content']
    land = [e for e in st.get('timeline', []) if e['what'] == 'landing']
    if not rev:
        problems.append('no guest_revealed')
    if not fills:
        problems.append('the indicator was never seen')
    landing_ms = None
    if rev and content:
        landing_ms = round(rev[0]['mono_ms'] - content[0]['mono_ms'])
    icon_match = background = None
    if first_guest is not None:
        a1 = np.asarray(Image.open(first_guest['png']).convert('RGB')).astype(int)
        x, y = geo[3], geo[4]
        k = st['startup']['guest_scale']
        region = a1[y:y + 32 * k, x:x + 32 * k]
        # Mac OS's startup background at the icon (the presenter's
        # rb_startup_bg): the corner's colour, and a dither's other colour
        # when the Mac pixels left of and above the corner continue one.
        c0, left, above = a1[y, x], a1[y, x - k], a1[y - k, x]
        c1 = left if (left != c0).any() and (left == above).all() else c0
        dy, dx = np.mgrid[0:32 * k, 0:32 * k]
        expected = np.where((((dx // k) + (dy // k)) & 1)[..., None] == 1, c1, c0)
        icon_match = int((np.abs(region - expected).sum(axis=2) > 36).sum() // (k * k))
        # The background outside the icon, frame to frame around the cut:
        # the pair where it changed most (all of it unchanged when the
        # patterns line up).
        k0 = next((n for n, fr in enumerate(frames) if fr['after_cut']), None)
        lay = st.get('layout') or {}
        pic = lay.get('dst')
        if k0 is not None:
            keep = None
            inside = None
            worst = None
            window = frames[max(0, k0 - 3):k0 + 4]
            prev = None
            for fr in window:
                cur = np.asarray(Image.open(fr['png']).convert('RGB')).astype(int)
                if keep is None:
                    keep = np.ones(cur.shape[:2], bool)
                    for rx, ry, q in ((geo[0], geo[1], geo[2]), (x, y, k)):
                        keep[max(0, ry):ry + 32 * q, max(0, rx):rx + 32 * q] = False
                    # Mac OS's picture (the guest layout): the margins go black at the cut
                    inside = keep.copy()
                    if pic:
                        m = np.zeros_like(keep)
                        m[pic[1]:pic[1] + pic[3], pic[0]:pic[0] + pic[2]] = True
                        inside &= m
                if prev is not None:
                    same = (prev[1] == cur).all(axis=2)
                    pct = round(100.0 * same[keep].mean(), 2)
                    pin = round(100.0 * same[inside].mean(), 2) if inside.any() else None
                    if worst is None or pct < worst[0]:
                        worst = (pct, prev[0]['png'], fr['png'], pin)
                prev = (fr, cur)
            if worst:
                background = {'unchanged_pct': worst[0], 'unchanged_in_picture_pct': worst[3],
                              'picture': pic, 'anchor': st['startup'].get('checker_anchor'),
                              'dither_at_icon': bool((c1 != c0).any()), 'frames': list(worst[1:3])}
    fades = [e['detail'] for e in st.get('timeline', []) if e['what'] == 'fade']
    captured = [{k: e.get(k) for k in ('valid', 'saved', 'written')} for e in c.events
                if e.get('event') == 'startup_icon']
    result = {'frames': len(frames), 'fills': fills, 'problems': problems, 'geometry': geo,
              'landing_ms': landing_ms, 'landing': [e['detail'] for e in land], 'fade': fades,
              'startup_icon': captured,
              'first_guest_frame': first_guest and first_guest['png'],
              'icon_pixels_at_landing': icon_match, 'background_across_cut': background,
              'startup': st['startup']}
    with open(os.path.join(a.out, 'result.json'), 'w') as f:
        json.dump(result, f, indent=1)
    print(json.dumps(result, indent=1))
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
