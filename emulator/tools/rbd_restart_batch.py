#!/usr/bin/env python3
"""Run rbd_restart_lab.py chains in parallel and summarize outcomes.

    rbd_restart_batch.py run --root DIR NAME N WORKERS -- LAB ARGS...
    rbd_restart_batch.py summarize RUNDIR...

Chain k writes ROOT/runs/NAME/c<k>/ and ROOT/disks/NAME-c<k>.img (deleted
when every boot of the chain reached Finder).  '{chain}' in LAB ARGS becomes
c<k> (``--share DIR/{chain}`` gives every chain its own shared folder).  summarize counts cold boots
(boot0) and relaunches (boot>0) per outcome and lists the failures.
"""
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

LAB = Path(__file__).resolve().parent / 'rbd_restart_lab.py'


def run(argv):
    import argparse
    split = argv.index('--') if '--' in argv else len(argv)
    ap = argparse.ArgumentParser(prog='rbd_restart_batch.py run')
    ap.add_argument('--root', required=True, type=Path)
    ap.add_argument('name')
    ap.add_argument('chains', type=int)
    ap.add_argument('workers', type=int)
    a = ap.parse_args(argv[:split])
    root, name, n, w = a.root, a.name, a.chains, a.workers
    rest = argv[split + 1:]
    (root / 'runs' / name).mkdir(parents=True, exist_ok=True)
    (root / 'disks').mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    todo = list(range(n))

    def worker():
        while True:
            with lock:
                if not todo:
                    return
                k = todo.pop(0)
            out = root / 'runs' / name / f'c{k:02d}'
            disk = root / 'disks' / f'{name}-c{k:02d}.img'
            # '{chain}' in the lab arguments becomes c<k> (e.g. a shared folder per chain)
            cmd = [sys.executable, str(LAB), '--out', str(out), '--disk', str(disk), '--quiet'] + \
                [v.replace('{chain}', f'c{k:02d}') for v in rest]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=open(str(out) + '.err', 'w'))
            try:
                res = json.loads((out / 'result.json').read_text())
                summ = [(b['action'], b['outcome']) for b in res['boots']]
            except (OSError, ValueError, KeyError) as exc:
                summ = str(exc)
            print(time.strftime('%H:%M:%S'), name, k, summ, flush=True)

    ts = [threading.Thread(target=worker) for _ in range(w)]
    for t in ts:
        t.start()
        time.sleep(3)
    for t in ts:
        t.join()


def summarize(dirs):
    for rd in dirs:
        cold, rel, fails, stalls, finders, loads = {}, {}, [], [], [], []
        shut = {}
        for rj in sorted(Path(rd).glob('c*/result.json')):
            for b in json.loads(rj.read_text())['boots']:
                d = cold if b['boot'] == 0 else rel
                d[b['outcome']] = d.get(b['outcome'], 0) + 1
                loads.append(b.get('load1', 0))
                if b['outcome'] not in ('finder', 'gray'):
                    fails.append((rj.parent.name, b['boot'], b['outcome'], b.get('crash_s'), b.get('gray_s')))
                if b.get('finder_s'):
                    finders.append(b['finder_s'])
                if b.get('stall'):
                    big = max(b['gaps'], key=lambda g: g[1])
                    stalls.append((rj.parent.name, b['boot'], 'gap@%.1fs' % big[0], '%.1fs' % big[1],
                                   b.get('finder_s')))
                if b['action'] in ('restart', 'shutdown') and b['outcome'] == 'finder':
                    r = (b.get('shutdown') or {}).get('reason', 'none')
                    shut[(b['action'], r)] = shut.get((b['action'], r), 0) + 1
        n = sum(cold.values()) + sum(rel.values())
        finders.sort()
        med = finders[len(finders) // 2] if finders else None
        print(rd, 'cold', cold, 'relaunch', rel)
        print('    boots %d, stalls (gap >= stall-gap s) %d, Finder median %s s, max %s s, load1 %.1f-%.1f'
              % (n, len(stalls), med, finders[-1] if finders else None, min(loads or [0]), max(loads or [0])))
        if shut:
            print('    actions', {f'{k[0]}:{k[1]}': v for k, v in sorted(shut.items())})
        for f in fails:
            print('   ', f)
        for st in stalls:
            print('    stall', st)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'run':
        run(sys.argv[2:])
    elif len(sys.argv) > 1 and sys.argv[1] == 'summarize':
        summarize(sys.argv[2:])
    else:
        sys.exit(__doc__)
