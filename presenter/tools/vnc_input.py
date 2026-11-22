#!/usr/bin/env python3
"""Inject keyboard/pointer input through the Proxmox VNC console of VM 163,
i.e. the same USB tablet + PS/2 keyboard path a user at the console uses.

  vnc_input.py 'move:640,400' 'click:700,420' 'chord:ctrl+alt+Delete' \
               'type:hello' 'key:Return' 'wait:500' 'drag:100,100:300,300'

Coordinates are outer-display pixels. Keys use X11 keysym names below or a
single character; chords hold every key then release in reverse order.
RB_VNC_VMID=164 targets the appliance VM instead of VM 163.
"""
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from harness.rfb import RFB  # noqa: E402

KEYSYMS = {
    'ctrl': 0xffe3, 'ctrl_r': 0xffe4, 'alt': 0xffe9, 'alt_r': 0xffea, 'shift': 0xffe1, 'shift_r': 0xffe2,
    'super': 0xffeb, 'super_r': 0xffec, 'Delete': 0xffff, 'BackSpace': 0xff08, 'Escape': 0xff1b,
    'Return': 0xff0d, 'Tab': 0xff09, 'Left': 0xff51, 'Up': 0xff52, 'Right': 0xff53, 'Down': 0xff54,
    'Home': 0xff50, 'End': 0xff57, 'Page_Up': 0xff55, 'Page_Down': 0xff56, 'space': 0x20, 'F1': 0xffbe,
}


def keysym(name):
    if name in KEYSYMS:
        return KEYSYMS[name]
    if len(name) == 1:
        return ord(name)
    raise ValueError(f'unknown key {name}')


def main(argv):
    r = RFB(vmid=int(os.environ.get('RB_VNC_VMID', '163')))
    x = y = 0
    mask = 0
    try:
        for step in argv:
            op, _, arg = step.partition(':')
            if op == 'move':
                x, y = (int(v) for v in arg.split(','))
                r.pointer(x, y, mask)
            elif op == 'click':
                if arg:
                    x, y = (int(v) for v in arg.split(','))
                    r.pointer(x, y, mask)
                    time.sleep(0.05)
                r.pointer(x, y, mask | 1)
                time.sleep(0.08)
                r.pointer(x, y, mask)
            elif op == 'rclick':
                r.pointer(x, y, mask | 4)
                time.sleep(0.08)
                r.pointer(x, y, mask)
            elif op == 'down':
                mask |= 1
                r.pointer(x, y, mask)
            elif op == 'up':
                mask &= ~1
                r.pointer(x, y, mask)
            elif op == 'drag':
                a, b = arg.split(':')
                x0, y0 = (int(v) for v in a.split(','))
                x1, y1 = (int(v) for v in b.split(','))
                r.pointer(x0, y0, 0)
                time.sleep(0.05)
                r.pointer(x0, y0, 1)
                for i in range(1, 21):
                    r.pointer(x0 + (x1 - x0) * i // 20, y0 + (y1 - y0) * i // 20, 1)
                    time.sleep(0.02)
                r.pointer(x1, y1, 0)
                x, y = x1, y1
            elif op == 'key':
                k = keysym(arg)
                r.key(k, True)
                time.sleep(0.05)
                r.key(k, False)
            elif op == 'hold':
                r.key(keysym(arg), True)
            elif op == 'release':
                r.key(keysym(arg), False)
            elif op == 'chord':
                keys = [keysym(k) for k in arg.split('+')]
                for k in keys:
                    r.key(k, True)
                    time.sleep(0.04)
                time.sleep(0.1)
                for k in reversed(keys):
                    r.key(k, False)
                    time.sleep(0.04)
            elif op == 'type':
                for ch in arg:
                    shifted = ch.isupper() or ch in '!@#$%^&*()_+{}|:"<>?~'
                    if shifted:
                        r.key(KEYSYMS['shift'], True)
                    r.key(ord(ch), True)
                    time.sleep(0.03)
                    r.key(ord(ch), False)
                    if shifted:
                        r.key(KEYSYMS['shift'], False)
                    time.sleep(0.03)
            elif op == 'wait':
                time.sleep(int(arg) / 1000.0)
            else:
                raise ValueError(f'unknown step {step}')
            print(f'{time.monotonic():.3f} {step}', flush=True)
            time.sleep(0.05)
    finally:
        r.close()


if __name__ == '__main__':
    main(sys.argv[1:])
