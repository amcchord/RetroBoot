#!/usr/bin/env python3
"""A display client (ARCHITECTURE 3) that replays a guest-initiated mode switch
as the N5105 showed it with the ATI Radeon (lab tool; standard library only).

    switch_scenario.py --socket PATH [--from 640x480] [--to 832x624]
        [--stale-ms 25] [--clear-ms 80] [--steps 60,60] [--hold 2]

Startup gray, then a desktop (pattern, menu bar, icons); then the switch: a
new SURFACE whose memory still holds the previous picture at the new stride
(ati-vga's stale frame; JetKVM: about 25 ms), Mac OS's 50 % dither clear
(about 80 ms), and the desktop redrawn in steps separated by --steps ms
(pattern and menu bar, then the Control Strip, then the icons; the loaded
builder showed 354,218). ati_lab.py --client runs it against the real
presenter instead of QEMU.
"""
import argparse
import mmap
import os
import socket
import struct
import sys
import time

HELLO, SURFACE, DAMAGE = 1, 2, 3
XRGB8888 = 0x34325258


def size(s):
    w, h = s.lower().split("x")
    return int(w), int(h)


class Client:
    def __init__(self, path):
        deadline = time.monotonic() + 10
        while True:
            try:
                self.s = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
                self.s.connect(path)
                break
            except OSError:
                self.s.close()
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)
        self.s.setblocking(False)
        self.seq = 0
        self.mm = None
        payload = struct.pack("=II", 1, os.getpid()) + b"switch-scenario".ljust(40, b"\0")
        self.send(HELLO, payload)

    def send(self, typ, payload=b"", fd=None):
        msg = struct.pack("=II", typ, len(payload)) + payload
        anc = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("i", fd))] if fd is not None else []
        try:
            self.s.sendmsg([msg], anc)
        except BlockingIOError:
            pass

    def drain(self):
        try:
            while self.s.recv(64):
                pass
        except (BlockingIOError, OSError):
            pass

    def surface(self, w, h, content=None):
        """A new surface; `content` (bytes) is what its memory holds at first."""
        fd = os.memfd_create("rb-switch-scenario", os.MFD_CLOEXEC)
        os.ftruncate(fd, w * h * 4)
        mm = mmap.mmap(fd, w * h * 4)
        if content:
            mm[:min(len(content), w * h * 4)] = content[:w * h * 4]
        self.send(SURFACE, struct.pack("=IIIIQ", w, h, w * 4, XRGB8888, w * h * 4), fd=fd)
        os.close(fd)
        self.mm, self.w, self.h = mm, w, h

    def damage(self, rects=None):
        rects = rects or [(0, 0, self.w, self.h)]
        self.seq += 1
        payload = struct.pack("=II", self.seq, len(rects)) + b"".join(struct.pack("=iiii", *r) for r in rects)
        self.send(DAMAGE, payload)

    def rows(self, y0, y1, row):
        for y in range(y0, y1):
            self.mm[y * self.w * 4:(y + 1) * self.w * 4] = row(y)


def px(c):
    return struct.pack("<I", c)


def gray(cl):
    line = px(0x888888) * cl.w
    cl.rows(0, cl.h, lambda y: line)


def dither(cl):
    a = (px(0) + px(0xffffff)) * (cl.w // 2 + 1)
    b = (px(0xffffff) + px(0)) * (cl.w // 2 + 1)
    cl.rows(0, cl.h, lambda y: (a if y % 2 == 0 else b)[:cl.w * 4])


def desktop(cl, strip=False, icons=False):
    pat = [b"".join(px(0x6a6aa8 if (x // 7 + y // 5) % 9 == 0 else 0x5c5c99) for x in range(cl.w)) for y in range(45)]
    menu = px(0xdddddd) * cl.w
    cl.rows(0, cl.h, lambda y: menu if y < 19 else px(0) * cl.w if y == 19 else pat[y % 45])
    if strip:
        bar = px(0xcccccc) * (cl.w // 2)
        for y in range(cl.h - 22, cl.h - 4):
            o = (y * cl.w + 4) * 4
            cl.mm[o:o + len(bar)] = bar
    if icons:
        for i in range(5):
            for y in range(40 + i * 60, 72 + i * 60):
                o = (y * cl.w + cl.w - 60) * 4
                cl.mm[o:o + 32 * 4] = px(0x203040 + i * 0x101010) * 32


def wait(cl, s):
    t = time.monotonic() + s
    while time.monotonic() < t:
        cl.drain()
        time.sleep(0.005)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--socket", default=os.environ.get("RB_DISPLAY_SOCKET", "/run/retroboot/display.sock"))
    ap.add_argument("--from", dest="src", type=size, default=(640, 480))
    ap.add_argument("--to", dest="dst", type=size, default=(832, 624))
    ap.add_argument("--stale-ms", type=float, default=25)
    ap.add_argument("--clear-ms", type=float, default=80)
    ap.add_argument("--steps", default="60,60")
    ap.add_argument("--hold", type=float, default=2.0)
    a = ap.parse_args()
    cl = Client(a.socket)
    cl.surface(*a.src)
    cl.damage()
    wait(cl, 0.5)
    gray(cl)
    cl.damage()
    wait(cl, 1.5)                       # startup gray: content, reveal
    desktop(cl, strip=True, icons=True)
    cl.damage()
    wait(cl, a.hold)
    print(f"{time.monotonic():.6f} switch {a.src} -> {a.dst}", flush=True)
    old = bytes(cl.mm)
    cl.surface(*a.dst, content=old)     # the old picture at the new stride
    cl.damage()
    wait(cl, a.stale_ms / 1000.0)
    dither(cl)                          # Mac OS's clear
    cl.damage()
    wait(cl, a.clear_ms / 1000.0)
    steps = [float(x) / 1000.0 for x in a.steps.split(",") if x]
    desktop(cl)                         # pattern and menu bar
    cl.damage()
    for i, gap in enumerate(steps):
        wait(cl, gap)
        desktop(cl, strip=True, icons=i >= 1)
        cl.damage()
    wait(cl, a.hold)
    print(f"{time.monotonic():.6f} end", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
