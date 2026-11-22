#!/usr/bin/env python3
"""Session transition checks on an image/lab/boot_capture.py run (lab tool).

    transition_check.py RUN_DIR [RUN_DIR ...] [--json]

Reads timeline.jsonl, events.jsonl and the distinct frames of a boot capture
and reports, from the outer display only:

* reveal: the first guest frame after the checkerboard -- its black fraction
  (a partly painted Mac OS gray shows as a black band) and its kind;
* surfaces: the guest picture size of every guest frame from the reveal to
  the Finder, measured as the bounding box of non-black pixels with black
  letterbox bars (one size = no guest mode switch; Mac OS falling back to
  640x480 shows as a centered 640x480 box);
* black frames between the reveal and the Finder (Mac OS blanks the screen
  on a mode switch);
* after the guest: checkerboard frames before the VM stops or the next
  guest reveal (a shutdown flash), and the sequence of kinds from the last
  guest frame to the end;
* timing: checkerboard, reveal, Finder, VM stopped (seconds from power-on).

Runs with the lab venv (numpy, Pillow).
"""
import argparse
import json
import pathlib
import sys

import numpy as np
from PIL import Image

GUEST = ('flat-gray', 'desktop-startup', 'finder', 'other', 'flat')


def load(run):
    rows = [json.loads(l) for l in open(run / 'timeline.jsonl')]
    events = [json.loads(l) for l in open(run / 'events.jsonl')] if (run / 'events.jsonl').exists() else []
    return rows, events


_box_cache = {}


def content_box(run, png):
    """Bounding box (x, y, w, h) of non-black pixels and whether everything outside it is black."""
    key = (str(run), png)
    if key not in _box_cache:
        a = np.asarray(Image.open(run / png).convert('RGB'))
        nonblack = a.max(axis=2) > 24
        ys, xs = np.nonzero(nonblack)
        if not len(xs):
            _box_cache[key] = None
        else:
            x0, x1, y0, y1 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
            _box_cache[key] = (x0, y0, x1 - x0 + 1, y1 - y0 + 1)
    return _box_cache[key]


def check(run):
    run = pathlib.Path(run)
    rows, events = load(run)
    out = {'run': run.name}
    checker = next((r for r in rows if r['kind'] == 'checkerboard'), None)
    out['checkerboard_s'] = round(checker['t'], 3) if checker else None
    if not checker:
        return out
    after = [r for r in rows if r['t'] >= checker['t']]
    reveal = next((r for r in after if r['kind'] in GUEST), None)
    finder = next((r for r in after if r['kind'] == 'finder'), None)
    t0_mono = next((e['mono'] for e in events if e['kind'] == 'resumed-power-on'), None)
    stopped = next((e['t'] for e in events if e['kind'] == 'vm-stopped' and e.get('t') is not None
                    and (t0_mono is None or e['mono'] > t0_mono)), None)
    out.update(reveal_s=round(reveal['t'], 3) if reveal else None, finder_s=round(finder['t'], 3) if finder else None,
               vm_stopped_s=round(stopped, 3) if stopped else None)
    if reveal:
        out['reveal_frame'] = {'png': reveal['png'], 'kind': reveal['kind'], 'black': reveal['black'],
                               'box': content_box(run, reveal['png'])}
        out['partial_reveal'] = reveal['black'] > 0.02
    if reveal and finder:
        sizes, blacks = {}, []
        W, H = reveal['w'], reveal['h']
        for r in after:
            if r['t'] < reveal['t'] or r['t'] > finder['t']:
                continue
            if r['kind'] == 'black':
                blacks.append(round(r['t'], 3))
                continue
            box = content_box(run, r['png'])
            if box is None:
                continue
            x, y, w, h = box
            # Letterboxed: black bars on both sides (or top and bottom) around a centered picture.
            letterboxed = (x > 8 and W - (x + w) > 8) or (y > 8 and H - (y + h) > 8)
            size = f'{w}x{h}' if letterboxed else 'full'
            sizes.setdefault(size, []).append(round(r['t'], 3))
        out['guest_picture_sizes'] = {k: {'frames': len(v), 'first_s': v[0], 'last_s': v[-1]} for k, v in sizes.items()}
        out['black_frames_reveal_to_finder'] = blacks
        out['one_surface_size'] = list(sizes) == ['full'] and not blacks
    # After the last guest frame: what follows?
    last_guest = max((i for i, r in enumerate(rows) if r['kind'] in GUEST), default=None)
    if last_guest is not None:
        tail = []
        for r in rows[last_guest:]:
            if not tail or tail[-1]['kind'] != r['kind']:
                tail.append({'kind': r['kind'], 't': round(r['t'], 3)})
        out['after_last_guest'] = tail[:12]
    # Checkerboard between guest sessions (restart) vs. a checkerboard flash before stop (shutdown).
    flashes = []
    seen_guest = False
    for i, r in enumerate(rows):
        if r['kind'] in GUEST:
            seen_guest = True
            continue
        if seen_guest and r['kind'] == 'checkerboard':
            nxt = next((x for x in rows[i + 1:] if x['kind'] != 'checkerboard'), None)
            end = nxt['t'] if nxt else (stopped or r['t'])
            flashes.append({'t': round(r['t'], 3), 'until': round(end, 3), 'next': nxt['kind'] if nxt else 'end',
                            'between_sessions': bool(nxt and nxt['kind'] in GUEST)})
            seen_guest = False
    out['checkerboard_after_guest'] = flashes
    out['shutdown_flash'] = any(not f['between_sessions'] for f in flashes)
    s = json.loads((run / 'summary.json').read_text()) if (run / 'summary.json').exists() else {}
    out['flags_after_firmware_boundary'] = len(s.get('flags_after_firmware_boundary', []))
    out['transition_glitches'] = s.get('transition_glitches', [])
    out['probe_latency_median_s'] = (s.get('cadence') or {}).get('probe_latency_median_s')
    out['probe_latency_max_s'] = (s.get('cadence') or {}).get('probe_latency_max_s')
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('runs', nargs='+')
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args()
    results = [check(r) for r in a.runs]
    if a.json:
        print(json.dumps(results, indent=1))
        return
    for r in results:
        print(json.dumps(r))


if __name__ == '__main__':
    sys.exit(main())
