#!/usr/bin/env python3
"""Lab helper for arm64_virt_check.sh (runs on rb-lab; needs python3-pil for `analyze`).

  arm64_virt_check.py run DIR [--interval 5] [--min-seconds 180] [--max-seconds 900]
                          [--end-marker RB-PROBE-END] -- qemu-system-aarch64 ARGS...
      Starts QEMU in DIR (its argv must use -qmp unix:DIR/qmp.sock and
      -serial file:DIR/serial.log), takes a QMP screendump every INTERVAL s
      (shot-<ms>.png, PPM if PNG is unsupported) until the probe's END marker
      is in serial.log and MIN seconds have passed (or MAX), then `quit`.
      Writes DIR/shots.json.
  arm64_virt_check.py analyze DIR
      Classifies every screendump against RetroBoot's checkerboard
      (presenter/src/checker.h: white 0xffffff / black 0x000000, square cells
      anchored at Mac OS's picture): black, dark (black with a little text, e.g.
      QEMU's "Display output is not active."), checker (whole frame),
      checker+overlay (startup indicator, panel or alert over it, with the
      overlay's box), other.
      Writes DIR/analysis.json and prints a table.
"""
import argparse
import collections
import json
import os
import signal
import socket
import subprocess
import sys
import time


class QMP:
    def __init__(self, path, timeout=30.0):
        deadline = time.monotonic() + timeout
        while True:
            try:
                self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.s.connect(path)
                break
            except OSError:
                self.s.close()
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)
        self.f = self.s.makefile('rb')
        json.loads(self.f.readline())               # greeting
        self.cmd('qmp_capabilities')

    def cmd(self, name, **args):
        msg = {'execute': name}
        if args:
            msg['arguments'] = args
        self.s.sendall(json.dumps(msg).encode() + b'\n')
        while True:
            line = self.f.readline()
            if not line:
                raise EOFError('QMP closed')
            r = json.loads(line)
            if 'return' in r or 'error' in r:
                return r


def run(a):
    d = os.path.abspath(a.dir)
    os.makedirs(d, exist_ok=True)
    serial = os.path.join(d, 'serial.log')
    log = open(os.path.join(d, 'qemu.log'), 'wb')
    t0 = time.monotonic()
    p = subprocess.Popen(a.qemu, cwd=d, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    shots, fmt, seen_end = [], 'png', None
    try:
        q = QMP(os.path.join(d, 'qmp.sock'))
        while p.poll() is None:
            t = time.monotonic() - t0
            name = 'shot-%06d.%s' % (int(t * 1000), fmt)
            r = q.cmd('screendump', filename=os.path.join(d, name), format=fmt)
            if 'error' in r and fmt == 'png':
                fmt = 'ppm'
                continue
            shots.append({'file': name, 't': round(t, 2), 'error': r.get('error')})
            try:
                with open(serial, 'rb') as f:
                    end = a.end_marker.encode() in f.read()
            except OSError:
                end = False
            if end and seen_end is None:
                seen_end = t
            if (seen_end is not None and t >= a.min_seconds and t >= seen_end + a.interval) or t >= a.max_seconds:
                break
            time.sleep(max(0.0, a.interval - (time.monotonic() - t0 - t)))
        if p.poll() is None:
            q.cmd('quit')
    finally:
        try:
            p.wait(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)       # only the QEMU this helper started
            p.wait()
        with open(os.path.join(d, 'shots.json'), 'w') as f:
            json.dump({'qemu': a.qemu, 'shots': shots, 'end_marker_s': seen_end,
                       'qemu_exit': p.returncode}, f, indent=1)
    print('captured %d screendumps; end marker at %s s; qemu exit %s' % (len(shots), seen_end, p.returncode))
    return 0 if seen_end is not None else 2


def runs(row):
    """Lengths of runs of equal values in a sequence of grey levels."""
    out, start = [], 0
    for i in range(1, len(row) + 1):
        if i == len(row) or row[i] != row[start]:
            out.append((start, i - start, row[start]))
            start = i
    return out


def classify(path):
    from PIL import Image, ImageChops
    im = Image.open(path).convert('RGB')
    w, h = im.size
    g = im.convert('L')
    px = g.tobytes()
    n = w * h
    black = px.count(0) / n
    white = px.count(255) / n
    res = {'size': [w, h], 'black': round(black, 4), 'white': round(white, 4)}
    if black > 0.995:
        # 'dark': black with a little text, e.g. QEMU's "Display output is not active."
        res['class'] = 'black' if black > 0.99999 else 'dark'
        return res
    if black + white < 0.3:
        res['class'] = 'other'
        res['colors'] = collections.Counter(im.getdata()).most_common(4)
        return res
    # Cell size: the most common run length of pure black/white along sample rows/cols.
    lengths = collections.Counter()
    edges_x, edges_y = collections.Counter(), collections.Counter()
    for y in range(h // 8, h, max(1, h // 8)):
        row = px[y * w:(y + 1) * w]
        for x, ln, v in runs(row):
            if v in (0, 255) and 0 < x and x + ln < w:
                lengths[ln] += 1
                edges_x[x] += 1
    for x in range(w // 8, w, max(1, w // 8)):
        col = px[x::w]
        for y, ln, v in runs(col):
            if v in (0, 255) and 0 < y and y + ln < h:
                lengths[ln] += 1
                edges_y[y] += 1
    if not lengths:
        res['class'] = 'other'
        return res
    s = lengths.most_common(1)[0][0]
    ax = collections.Counter({k % s: c for k, c in edges_x.items()}).most_common(1)[0][0] if edges_x else 0
    ay = collections.Counter({k % s: c for k, c in edges_y.items()}).most_common(1)[0][0] if edges_y else 0
    best = None
    for phase in (0, 1):
        tile = Image.new('L', (2 * s, 2 * s), 0)
        for (i, j) in ((0, 0), (1, 1)) if phase else ((1, 0), (0, 1)):
            tile.paste(255, (i * s, j * s, (i + 1) * s, (j + 1) * s))
        strip = Image.new('L', (w + 2 * s, 2 * s))
        for xx in range(0, w + 2 * s, 2 * s):
            strip.paste(tile, (xx, 0))
        ref = Image.new('L', (w + 2 * s, h + 2 * s))
        for yy in range(0, h + 2 * s, 2 * s):
            ref.paste(strip, (0, yy))
        ref = ref.crop((2 * s - ax, 2 * s - ay, 2 * s - ax + w, 2 * s - ay + h))
        diff = ImageChops.difference(g, ref)
        hist = diff.histogram()
        mism = (n - hist[0]) / n
        if best is None or mism < best[0]:
            best = (mism, phase, diff.getbbox())
    mism, phase, bbox = best
    # Anchor: a pixel where a black cell begins (ax/ay mod s, black/white phase).
    anchor_black = (ax, ay) if phase == 0 else ((ax + s) % (2 * s), ay)
    res.update({'cell': s, 'anchor_black_mod_2cell': anchor_black, 'match': round(1 - mism, 5)})
    if mism < 0.0005:
        res['class'] = 'checker'
    elif mism < 0.5:
        res['class'] = 'checker+overlay'
        res['overlay_bbox'] = bbox
        bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
        res['overlay_size'] = [bw, bh]
        res['overlay_center_offset'] = [bbox[0] + bw // 2 - w // 2, bbox[1] + bh // 2 - h // 2]
    else:
        res['class'] = 'other'
    return res


def analyze(a):
    d = os.path.abspath(a.dir)
    meta = json.load(open(os.path.join(d, 'shots.json')))
    rows, prev_key = [], None
    first = {}
    for s in meta['shots']:
        path = os.path.join(d, s['file'])
        if not os.path.exists(path):
            continue
        r = classify(path)
        r['t'], r['file'] = s['t'], s['file']
        key = (r['class'], tuple(r.get('overlay_bbox') or ()), r.get('cell'))
        r['changed'] = key != prev_key
        prev_key = key
        first.setdefault(r['class'], r['t'])
        rows.append(r)
        extra = ''
        if 'cell' in r:
            extra = 'cell=%d match=%.4f' % (r['cell'], r['match'])
        if 'overlay_bbox' in r:
            extra += ' overlay=%s' % (r['overlay_bbox'],)
        print('%7.1fs %-16s %s %s' % (r['t'], r['class'], '%dx%d' % tuple(r['size']), extra))
    summary = {'first_seen_s': first, 'frames': len(rows), 'end_marker_s': meta.get('end_marker_s')}
    json.dump({'summary': summary, 'frames': rows}, open(os.path.join(d, 'analysis.json'), 'w'), indent=1)
    print(json.dumps(summary))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='op', required=True)
    r = sub.add_parser('run')
    r.add_argument('dir')
    r.add_argument('--interval', type=float, default=5.0)
    r.add_argument('--min-seconds', type=float, default=180.0)
    r.add_argument('--max-seconds', type=float, default=900.0)
    r.add_argument('--end-marker', default='RB-PROBE-END')
    n = sub.add_parser('analyze')
    n.add_argument('dir')
    argv = sys.argv[1:]
    qemu = argv[argv.index('--') + 1:] if '--' in argv else []
    a = ap.parse_args(argv[:len(argv) - len(qemu) - (1 if qemu else 0)])
    if a.op == 'run':
        if not qemu:
            ap.error('run needs -- QEMU ARGS')
        a.qemu = qemu
        return run(a)
    return analyze(a)


if __name__ == '__main__':
    sys.exit(main())
