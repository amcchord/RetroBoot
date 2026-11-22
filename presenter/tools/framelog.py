#!/usr/bin/env python3
"""Frame logs (presenter/src/framelog.h): read, render and summarise (lab tool).

rb-presenter writes them with ``--device virtual:WxH --record FILE`` (every
frame its virtual display presents, stamped with the refresh at which it
shows) and ``--record-guest FILE`` (every guest frame as it arrives), both on
CLOCK_MONOTONIC, so a lab run without a display still has a frame-accurate
capture of what the presenter showed and of what Mac OS drew.

    framelog.py info LOG                         frames, time span, sizes
    framelog.py png LOG OUT [--from S] [--to S] [--t0 MONO_S] [--every N]
                                                 one PNG per frame (OUT/index.jsonl lists them)
    framelog.py at LOG MONO_S OUT.png            the frame on screen at that time

Times on the command line are seconds of CLOCK_MONOTONIC (``--t0`` makes the
PNG names relative to it, e.g. the QEMU launch).  Python standard library only.
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
import zlib
from pathlib import Path

FIRST, RESIZE = 1, 2
_HDR = struct.Struct('<4sIQIII')
_BAND = struct.Struct('<iiii')


class Frame:
    __slots__ = ('index', 't_us', 'flags', 'w', 'h', 'bands', 'changed_px', 'canvas', 'boxes')

    def __init__(self, index, t_us, flags, w, h, bands, changed_px, canvas, boxes):
        self.index, self.t_us, self.flags, self.w, self.h = index, t_us, flags, w, h
        self.bands, self.changed_px, self.canvas, self.boxes = bands, changed_px, canvas, boxes

    @property
    def t(self) -> float:
        return self.t_us / 1e6


def read(path):
    """Yield every frame; ``frame.canvas`` is the live canvas (copy it to keep it)."""
    with open(path, 'rb') as f:
        head = f.read(8)
        if len(head) != 8 or head[:4] != b'RBFL' or struct.unpack('<I', head[4:])[0] != 1:
            raise ValueError(f'{path}: not a version 1 frame log')
        canvas = None
        w = h = 0
        index = 0
        while True:
            hdr = f.read(_HDR.size)
            if len(hdr) < _HDR.size:
                return
            tag, flags, t_us, fw, fh, nb = _HDR.unpack(hdr)
            if tag != b'FRAM':
                raise ValueError(f'{path}: bad record at frame {index}')
            if canvas is None or flags & RESIZE or (fw, fh) != (w, h):
                w, h = fw, fh
                canvas = bytearray(w * h * 4)
            changed = 0
            boxes = []
            for _ in range(nb):
                x, y, bw, bh = _BAND.unpack(f.read(_BAND.size))
                if x < 0 or y < 0 or bw <= 0 or bh <= 0 or x + bw > w or y + bh > h:
                    raise ValueError(f'{path}: band outside the frame at frame {index}')
                row = bw * 4
                data = f.read(row * bh)
                if len(data) != row * bh:
                    return  # truncated (the writer was killed)
                for i in range(bh):
                    o = ((y + i) * w + x) * 4
                    canvas[o:o + row] = data[i * row:(i + 1) * row]
                changed += bw * bh
                boxes.append((x, y, bw, bh))
            yield Frame(index, t_us, flags, w, h, nb, changed, canvas, boxes)
            index += 1


def to_rgb(canvas, w, h) -> bytes:
    rgb = bytearray(w * h * 3)
    rgb[0::3] = canvas[2::4]
    rgb[1::3] = canvas[1::4]
    rgb[2::3] = canvas[0::4]
    return bytes(rgb)


def png_bytes(w: int, h: int, rgb: bytes, level: int = 3) -> bytes:
    row = w * 3
    raw = b''.join(b'\x00' + rgb[y * row:(y + 1) * row] for y in range(h))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)

    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0)) +
            chunk(b'IDAT', zlib.compress(raw, level)) + chunk(b'IEND', b''))


def write_png(path, frame: Frame):
    Path(path).write_bytes(png_bytes(frame.w, frame.h, to_rgb(frame.canvas, frame.w, frame.h)))


def info(path) -> dict:
    n = 0
    first = last = None
    sizes = []
    for fr in read(path):
        if first is None:
            first = fr.t
        last = fr.t
        if not sizes or sizes[-1]['size'] != [fr.w, fr.h]:
            sizes.append({'size': [fr.w, fr.h], 'from_s': round(fr.t, 4), 'frame': fr.index})
        n += 1
    return {'frames': n, 'first_s': first, 'last_s': last, 'sizes': sizes}


def cmd_png(a):
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    t0 = a.t0 if a.t0 is not None else None
    idx = (out / 'index.jsonl').open('w')
    k = 0
    for fr in read(a.log):
        if t0 is None:
            t0 = fr.t
        if a.from_s is not None and fr.t < a.from_s:
            continue
        if a.to_s is not None and fr.t > a.to_s:
            break
        k += 1
        if a.every > 1 and (k - 1) % a.every:
            continue
        rel = (fr.t - t0) * 1000.0
        name = f'{fr.index:06d}_{rel:+010.1f}ms.png'
        write_png(out / name, fr)
        idx.write(json.dumps({'png': name, 'frame': fr.index, 't': round(fr.t, 6), 'rel_ms': round(rel, 1),
                              'size': [fr.w, fr.h], 'bands': fr.bands, 'changed_px': fr.changed_px,
                              'boxes': fr.boxes[:16], 'flags': fr.flags}) + '\n')
    idx.close()
    print(json.dumps({'out': str(out), 'frames': k}))


def cmd_at(a):
    shown = None
    for fr in read(a.log):
        if fr.t > a.t:
            break
        shown = Frame(fr.index, fr.t_us, fr.flags, fr.w, fr.h, fr.bands, fr.changed_px, bytes(fr.canvas), fr.boxes)
    if shown is None:
        raise SystemExit('no frame at that time')
    write_png(a.out, shown)
    print(json.dumps({'frame': shown.index, 't': shown.t, 'size': [shown.w, shown.h]}))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('info')
    s.add_argument('log')
    s = sub.add_parser('png')
    s.add_argument('log')
    s.add_argument('out')
    s.add_argument('--from', dest='from_s', type=float)
    s.add_argument('--to', dest='to_s', type=float)
    s.add_argument('--t0', type=float)
    s.add_argument('--every', type=int, default=1)
    s = sub.add_parser('at')
    s.add_argument('log')
    s.add_argument('t', type=float)
    s.add_argument('out')
    a = ap.parse_args()
    if a.cmd == 'info':
        print(json.dumps(info(a.log), indent=1))
    elif a.cmd == 'png':
        cmd_png(a)
    else:
        cmd_at(a)
    return 0


if __name__ == '__main__':
    sys.exit(main())
