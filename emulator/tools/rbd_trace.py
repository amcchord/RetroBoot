#!/usr/bin/env python3
"""Read QEMU log-backend traces written by rbd_restart_lab.py --trace-events.

Lines look like ``2026-10-11T12:43:41.822850Z event args`` (QEMU ``-msg
timestamp=on``); times are printed relative to the boot's ``t0_wall`` from
result.json, so they line up with the lab's frame gaps.  ``-d int`` lines
(``Raise exception at ...``, ``syscall r0=...``) carry the same stamps.

    rbd_trace.py hist BOOTDIR                      events per second by prefix
    rbd_trace.py grep BOOTDIR T0 T1 REGEX          lines in [T0, T1] matching REGEX
    rbd_trace.py window BOOTDIR T0 T1 [EXCLUDE]    every line in [T0, T1]
    rbd_trace.py cuda BOOTDIR...                   classify Cuda transactions (see below)
    rbd_trace.py decneg BOOTDIR...                 DEC stores of a negative value while DEC < 0

``cuda`` needs openpic_iack/openpic_eoi (RetroBoot debug trace points),
ppc_decr_store and cuda_packet_receive.  For every transaction it reports
whether the decrementer expired inside the Cuda interrupt handler (DEC set
to 0x64bd before the last EOI) and whether the nanokernel forced an
immediate decrementer exception (DEC <- 0xffffffff) right after that EOI,
against the stalls (frame gaps >= 10 s) recorded in result.json.
Lab tool (Python standard library only).
"""
import calendar
import json
import re
import sys
from collections import Counter, defaultdict

PAT = re.compile(r'^(\d+)-(\d+)-(\d+)T(\d+):(\d+):(\d+)\.(\d+)Z (\S+)\s?(.*)$')


def lines(bd):
    t0 = json.load(open(bd + '/result.json'))['t0_wall']
    with open(bd + '/trace.log', errors='replace') as f:
        for ln in f:
            m = PAT.match(ln)
            if not m:
                continue
            g = [int(x) for x in m.groups()[:6]]
            t = calendar.timegm((g[0], g[1], g[2], g[3], g[4], g[5], 0, 0, 0)) + int(m.group(7)) / 1e6 - t0
            yield t, m.group(8), m.group(9)


def hist(bd):
    cats = defaultdict(Counter)
    for t, ev, _ in lines(bd):
        cats[int(t)]['_'.join(ev.split('_')[:2])] += 1
    for s in sorted(cats):
        print(s, dict(cats[s]))


def grep(bd, a0, a1, rx, invert=False):
    rx = re.compile(rx) if rx else None
    for t, ev, a in lines(bd):
        if t < a0:
            continue
        if t > a1:
            break
        hit = bool(rx and rx.search(ev + ' ' + a))
        if (hit and not invert) or (invert and not hit) or rx is None:
            print('%.6f %s %s' % (t, ev, a[:200]))


def cuda(bds):
    tab = Counter()
    for bd in bds:
        r = json.load(open(bd + '/result.json'))
        stalls = [g[0] for g in r.get('gaps', []) if g[1] >= 10]
        ev = list(lines(bd))
        i, n = 0, len(ev)
        while i < n:
            t, e, a = ev[i]
            if not (e == 'openpic_iack' and a.startswith('irq 25 ')):
                i += 1
                continue
            start, j, last_eoi, dec_inside, pkt = t, i, None, False, False
            while j < n and ev[j][0] - start < 0.006:
                tj, ej, aj = ev[j]
                if ej == 'openpic_eoi' and aj.startswith('irq 25'):
                    last_eoi = (j, tj)
                if ej == 'ppc_decr_store' and aj.endswith('0x00000000000064bd') and last_eoi is None:
                    dec_inside = True
                if ej == 'cuda_packet_receive':
                    pkt = True
                j += 1
            if not pkt or last_eoi is None:
                i += 1
                continue
            k, te = last_eoi
            forced = any(x[1] == 'ppc_decr_store' and x[2].endswith('0x00000000ffffffff')
                         for x in ev[k:k + 20] if x[0] - te < 0.0002)
            stall = any(abs(s - start) < 1.0 for s in stalls)
            tab[(dec_inside, forced, stall)] += 1
            if forced or stall:
                print(bd, round(start, 3), 'dec-in-isr' if dec_inside else '-',
                      'forced-dec' if forced else '-', 'STALL' if stall else '')
            i = j
    print('(dec expired in Cuda ISR, forced DEC after EOI, stall starts): transactions')
    for k, v in sorted(tab.items()):
        print(' ', k, v)


def decneg(bds):
    for bd in bds:
        out = []
        for t, ev, a in lines(bd):
            if ev != 'ppc_decr_store':
                continue
            m = re.search(r'0x([0-9a-f]+) => 0x([0-9a-f]+)', a)
            d, v = int(m.group(1), 16) & 0xffffffff, int(m.group(2), 16) & 0xffffffff
            if d & 0x80000000 and v & 0x80000000:
                out.append((round(t, 4), hex(d), hex(v)))
        print(bd, len(out), out[:8])


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == 'hist':
        hist(args[0])
    elif cmd == 'grep':
        grep(args[0], float(args[1]), float(args[2]), args[3])
    elif cmd == 'window':
        grep(args[0], float(args[1]), float(args[2]), args[3] if len(args) > 3 else None, invert=len(args) > 3)
    elif cmd == 'cuda':
        cuda(args)
    elif cmd == 'decneg':
        decneg(args)
    else:
        sys.exit(__doc__)


if __name__ == '__main__':
    try:
        main()
    except BrokenPipeError:
        pass
