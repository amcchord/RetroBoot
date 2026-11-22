#!/usr/bin/env python3
"""Interleaved A/B runs of rbd_quadra_lab.py scenarios on a shared lab host.

    rbd_quadra_batch.py run ROOT COUNT --arm 'NAME=boot --pkg P --disk D ...' --arm ... -- COMMON...
    rbd_quadra_batch.py summarize ROOT

Each round runs every arm once, in rotating order, so host load falls on all
arms alike; every run keeps its own directory ROOT/NAME-I with result.json
(its disk copy is removed afterwards unless --keep-disks).
summarize prints medians (and the spread) of the milestones, the QEMU CPU
seconds at them, idle CPU, latency and pointer rates, with the host load seen.
"""
import argparse
import json
from pathlib import Path
import shlex
import statistics
import subprocess
import sys

LAB = Path(__file__).resolve().with_name('rbd_quadra_lab.py')


def run(a):
    root = Path(a.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    arms = [(spec.split('=', 1)[0], shlex.split(spec.split('=', 1)[1])) for spec in a.arm]
    for i in range(a.count):
        order = arms[i % len(arms):] + arms[:i % len(arms)]
        for name, args in order:
            d = root / f'{name}-{i}'
            if (d / 'result.json').exists():
                continue
            cmd = [sys.executable, str(LAB), args[0], *args[1:], *a.common, '--run', str(d)]
            r = subprocess.run(cmd, capture_output=True, text=True)
            (root / f'{name}-{i}.log').write_text(r.stdout + r.stderr)
            if not a.keep_disks:        # each run's disk copy (a full lab disk slows and stops QEMU)
                (d / 'disk.img').unlink(missing_ok=True)
            print(name, i, 'rc', r.returncode, flush=True)


def med(values):
    v = [x for x in values if x is not None]
    if not v:
        return None
    return {'median': round(statistics.median(v), 3), 'min': min(v), 'max': max(v), 'n': len(v)}


def summarize(a):
    root = Path(a.root)
    arms = {}
    for f in sorted(root.glob('*/result.json')):
        name = f.parent.name.rsplit('-', 1)[0]
        arms.setdefault(name, []).append(json.loads(f.read_text()))
    out = {}
    for name, rs in arms.items():
        s = {}
        for k in ('first_frame', 'desktop', 'finder', 'responsive', 'quiet'):
            s[k + '_s'] = med([r.get('marks', {}).get(k) for r in rs])
            s[k + '_cpu_s'] = med([r.get('cpu_s_at', {}).get(k) for r in rs])
        s['idle_cpu_pct'] = med([r.get('idle', {}).get('cpu_pct') for r in rs])
        s['idle_vcpu_pct'] = med([r.get('idle', {}).get('threads_pct', {}).get('ALL CPUs/TCG') for r in rs])
        s['idle_main_pct'] = med([r.get('idle', {}).get('threads_pct', {}).get('qemu-system-m68') for r in rs])
        s['idle_ctxsw_per_s'] = med([r.get('idle', {}).get('ctx_switches_per_s') for r in rs])
        s['latency_median_ms'] = med([r.get('median_ms') for r in rs])
        s['pointer_fps'] = med([r.get('pointer_fps') for r in rs])
        s['shutdown_s'] = med([(r.get('power') or {}).get('after_answer_s') for r in rs])
        s['reasons'] = sorted({str(((r.get('power') or {}).get('event') or {}).get('reason')) for r in rs})
        s['load_start'] = med([float(r['loadavg_start'][0]) for r in rs if r.get('loadavg_start')])
        out[name] = {k: v for k, v in s.items() if v is not None and v != ['None']}
    if getattr(a, 'table', False):
        for name, s in out.items():
            print(name.ljust(14), ' '.join(f"{k}={v['median'] if isinstance(v, dict) else v}" + (f"(n{v['n']})" if isinstance(v, dict) and k == 'finder_s' else '')
                                         for k, v in s.items() if not k.endswith('_cpu_s') or k in ('finder_cpu_s',)))
        return
    print(json.dumps(out, indent=2))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='mode', required=True)
    r = sub.add_parser('run')
    r.add_argument('root')
    r.add_argument('count', type=int)
    r.add_argument('--arm', action='append', required=True)
    r.add_argument('--keep-disks', action='store_true', help="keep each run's disk.img copy")
    s = sub.add_parser('summarize')
    s.add_argument('root')
    s.add_argument('--table', action='store_true', help='one line per arm: medians only')
    argv = sys.argv[1:]
    common = []
    if '--' in argv:
        i = argv.index('--')
        argv, common = argv[:i], argv[i + 1:]
    a = ap.parse_args(argv)
    if a.mode == 'run':
        a.common = common
        run(a)
    else:
        summarize(a)


if __name__ == '__main__':
    main()
