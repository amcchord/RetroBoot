#!/usr/bin/env python3
"""Scripted stand-in for hostd, for presenter integration tests only.

Connects to the control socket, applies display/input config, shows the guest
with reveal=first-content, and implements the minimal recovery flow:
  chord          -> panel (guest-dimmed) + a recovery document
  ui_action id=continue/cancel -> back to the guest (reveal now)
  ui_action id=splash          -> splash
  preboot_key    -> panel with a "startup options" document
Every message is printed as one JSON line with a local monotonic timestamp.

  mini_hostd.py --scaling integer --fifo /tmp/hostd.fifo
  echo '{"cmd":"screenshot","path":"/tmp/s.png","what":"composed"}' > /tmp/hostd.fifo

With --fifo, JSON request lines written to the FIFO are forwarded (the
presenter accepts one control client, so this is how tests issue extra
requests while the scripted hostd stays connected).
"""
import argparse
import json
import os
import select
import os
import socket
import sys
import time

RECOVERY = {
    "id": "recovery-main", "kind": "window", "title": "RetroBoot Recovery", "width": 420,
    "items": [
        {"type": "text", "text": "Mac OS is paused.", "style": "emphasis"},
        {"type": "text", "text": "Choose Continue to return to Mac OS.", "style": "small"},
        {"type": "buttons", "items": [{"id": "splash", "label": "Splash"},
                                      {"id": "continue", "label": "Continue", "default": True}]},
    ],
}
STARTUP = {
    "id": "startup-options", "kind": "window", "title": "Startup Options", "width": 380,
    "items": [
        {"type": "text", "text": "Escape was pressed during startup."},
        {"type": "buttons", "items": [{"id": "cancel", "label": "Cancel"},
                                      {"id": "continue", "label": "Start Mac OS", "default": True}]},
    ],
}


class Hostd:
    def __init__(self, a):
        self.a = a
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.connect(a.socket)
        self.id = 0
        self.buf = b''

    def send(self, cmd, **kw):
        self.id += 1
        req = {'id': self.id, 'cmd': cmd}
        req.update(kw)
        self.s.sendall((json.dumps(req) + '\n').encode())
        self.out({'sent': req})

    def out(self, obj):
        obj = dict(obj)
        obj['_t'] = round(time.monotonic(), 4)
        print(json.dumps(obj), flush=True)

    def run(self):
        a = self.a
        self.send('hello')
        cfg = {'scaling': a.scaling}
        if a.guest_size:
            w, h = a.guest_size.split('x')
            cfg['guest_size'] = [int(w), int(h)]
        self.send('display_config', **cfg)
        self.send('input_config', recovery_chord=a.chord, swap_command_option=a.swap, pointer_speed=a.speed)
        self.send('set_scene', scene='guest', reveal='first-content')
        deadline = time.monotonic() + a.seconds if a.seconds else None
        fifo = None
        if a.fifo:
            if not os.path.exists(a.fifo):
                os.mkfifo(a.fifo)
            fifo = os.open(a.fifo, os.O_RDWR | os.O_NONBLOCK)
        fbuf = b''
        while True:
            if deadline and time.monotonic() > deadline:
                return
            fds = [self.s] + ([fifo] if fifo is not None else [])
            ready, _, _ = select.select(fds, [], [], 0.5)
            if fifo is not None and fifo in ready:
                fbuf += os.read(fifo, 65536)
                while b'\n' in fbuf:
                    line, fbuf = fbuf.split(b'\n', 1)
                    if line.strip():
                        req = json.loads(line)
                        self.send(req.pop('cmd'), **req)
            if self.s not in ready:
                continue
            chunk = self.s.recv(1 << 20)
            if not chunk:
                self.out({'closed': True})
                return
            self.buf += chunk
            while b'\n' in self.buf:
                line, self.buf = self.buf.split(b'\n', 1)
                msg = json.loads(line)
                self.out(msg)
                self.handle(msg)

    def handle(self, msg):
        ev = msg.get('event')
        if ev == 'chord':
            self.send('set_scene', scene='panel', background='guest-dimmed')
            self.send('ui_show', doc=RECOVERY)
        elif ev == 'preboot_key':
            self.send('set_scene', scene='panel', background='checkerboard')
            self.send('ui_show', doc=STARTUP)
        elif ev == 'ui_action':
            if msg.get('id') in ('continue', 'cancel'):
                # From the startup panel the guest may still be in firmware:
                # keep the checkerboard until genuine content.
                reveal = 'first-content' if msg.get('doc') == 'startup-options' else 'now'
                self.send('set_scene', scene='guest', reveal=reveal)
            elif msg.get('id') == 'splash':
                self.send('set_scene', scene='splash')


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--socket', default='/run/retroboot/presenter.sock')
    ap.add_argument('--scaling', default='integer', choices=['integer', 'fit'])
    ap.add_argument('--guest-size', help='WxH (default: auto)')
    ap.add_argument('--chord', default='ctrl+alt+delete')
    ap.add_argument('--swap', action='store_true')
    ap.add_argument('--speed', type=float, default=1.0)
    ap.add_argument('--seconds', type=float, default=0)
    ap.add_argument('--fifo', help='forward JSON request lines written to this FIFO')
    a = ap.parse_args()
    try:
        Hostd(a).run()
    except KeyboardInterrupt:
        pass
