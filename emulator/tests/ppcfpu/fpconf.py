#!/usr/bin/env python3
"""Driver for the bare-metal PowerPC FP conformance program (fpconf.elf).

Runs fpconf.elf under qemu-system-ppc -M mac99 -cpu 7400 (no OS) once per
"arm" (CPU option set), compares the per-(configuration, op) outcome hashes
against the reference arm (QEMU's softfloat path, fast-fpu=off) and, for any
mismatch, reruns that pair in log mode and prints the first differing records
decoded.  Every QEMU runs in its own process group and is killed on every exit
path (normal return, exception, SIGINT/SIGTERM/SIGHUP).

    fpconf.py conf --qemu BIN --elf fpconf.elf --count 20000 \
        --arm ref=fast-fpu=off --arm fast=fast-fpu=on [--jobs 4] [--seed N]
    fpconf.py bench --qemu BIN --elf fpconf.elf --arm off=fast-fpu=off \
        --arm on=fast-fpu=on [--iters N] [--repeat 3]
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

HERE = Path(__file__).resolve().parent
PARAM_BASE = 0x4000
RESULT_BASE = 0x6000
BENCH_BASE = 0x20000
LOG_BASE = 0x01000000
LOG_REC = 128
MAX_OPS = 512
NCFGS = 16
DONE, FAIL = 0x444f4e45, 0x4641494c

_children: set[subprocess.Popen] = set()
_lock = threading.Lock()


def _kill_all(*_):
    with _lock:
        procs = list(_children)
    for p in procs:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            p.wait(timeout=5)
        except Exception:
            pass


def _on_signal(signum, frame):
    _kill_all()
    sys.exit(128 + signum)


atexit.register(_kill_all)
for _s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
    signal.signal(_s, _on_signal)


class QMP:
    def __init__(self, path: str, timeout: float = 30.0):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(path)
        self.f = self.sock.makefile('rw', buffering=1)
        json.loads(self.f.readline())
        self.cmd('qmp_capabilities')

    def cmd(self, name, **args):
        self.f.write(json.dumps({'execute': name, 'arguments': args}) + '\n')
        self.f.flush()
        while True:
            line = self.f.readline()
            if not line:
                raise ConnectionError('QMP closed')
            r = json.loads(line)
            if 'event' in r:
                continue
            if 'error' in r:
                raise RuntimeError(f'{name}: {r["error"]}')
            return r.get('return')

    def hmp(self, text):
        return self.cmd('human-monitor-command', **{'command-line': text})

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def read_word(q: QMP, addr: int) -> int:
    out = q.hmp(f'xp /1wx 0x{addr:x}')
    return int(out.split(':')[1].split()[0], 16)


def split_arm(qemu: str, opts: str):
    """'OPTS[@QEMU]' -> (qemu binary, -cpu value)"""
    if '@' in opts:
        opts, qemu = opts.split('@', 1)
    return qemu, (f'7400,{opts}' if opts else '7400')


def run_guest(qemu: str, elf: str, params: dict, cpu: str, workdir: Path,
              ram: int = 512, timeout: float = 3600, dumps=(), log=None,
              extra=()) -> dict:
    """Boot fpconf.elf with the given parameter words; return dumps."""
    workdir.mkdir(parents=True, exist_ok=True)
    sock = str(workdir / 'qmp.sock')
    if os.path.exists(sock):
        os.unlink(sock)
    words = [
        (0x00, 0x46504350), (0x04, params['mode']),
        (0x08, (params.get('seed', 1) >> 32) & 0xffffffff),
        (0x0c, params.get('seed', 1) & 0xffffffff),
        (0x10, params.get('count', 1000)), (0x14, params.get('cfgmask', 0xffff)),
        (0x18, params.get('first', 0)), (0x1c, params.get('last', 0xffff)),
        (0x20, params.get('iters', 0)), (0x24, params.get('bmask', 0xffffffff)),
    ]
    argv = [qemu, '-M', 'mac99,audiodev=snd0', '-audiodev', 'none,id=snd0', '-cpu', cpu, '-m', str(ram), '-bios', elf,
            '-nodefaults', '-display', 'none', '-serial', 'none',
            '-qmp', f'unix:{sock},server=on,wait=off',
            '-name', 'ppcfpu-fpconf', *extra]
    if os.environ.get('FPCONF_MONITOR'):
        argv += ['-monitor', f'unix:{workdir}/mon.sock,server=on,wait=off']
    # generic-loader converts its 64-bit value to big endian and stores the
    # first data-len bytes, so write the parameter words in 8-byte pairs.
    for (off, hi), (_, lo) in zip(words[0::2], words[1::2]):
        argv += ['-device', f'loader,addr=0x{PARAM_BASE + off:x},'
                 f'data=0x{hi:08x}{lo:08x},data-len=8,data-be=on']
    errf = open(workdir / 'qemu.err', 'w')
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=errf,
                         stderr=subprocess.STDOUT, start_new_session=True)
    with _lock:
        _children.add(p)
    t0 = time.monotonic()
    q = None
    try:
        while q is None:
            if p.poll() is not None:
                raise RuntimeError(f'QEMU exited {p.returncode}: '
                                   + (workdir / 'qemu.err').read_text()[-2000:])
            try:
                q = QMP(sock)
            except (FileNotFoundError, ConnectionRefusedError):
                time.sleep(0.05)
        while True:
            done = read_word(q, RESULT_BASE)
            if done in (DONE, FAIL):
                break
            if time.monotonic() - t0 > timeout:
                raise TimeoutError('guest did not finish')
            time.sleep(0.2)
        elapsed = time.monotonic() - t0
        result = {'done': done, 'elapsed': elapsed}
        for name, addr, size in dumps:
            path = workdir / f'{name}.bin'
            q.cmd('pmemsave', val=addr, size=size, filename=str(path))
            result[name] = path.read_bytes()
        if log is not None:
            n = read_word(q, RESULT_BASE + 0x10)
            path = workdir / 'log.bin'
            if n:
                q.cmd('pmemsave', val=LOG_BASE, size=n * LOG_REC, filename=str(path))
                result['log'] = path.read_bytes()
            else:
                result['log'] = b''
        try:
            q.cmd('quit')
        except Exception:
            pass
        return result
    finally:
        if q:
            q.close()
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.wait()
        with _lock:
            _children.discard(p)
        errf.close()


def load_ops(elf: str):
    path = Path(elf).with_name('ops.json')
    return json.loads(path.read_text())


FPSCR_BITS = ['FX', 'FEX', 'VX', 'OX', 'UX', 'ZX', 'XX', 'VXSNAN', 'VXISI',
              'VXIDI', 'VXZDZ', 'VXIMZ', 'VXVC', 'FR', 'FI', 'C', 'FL', 'FG',
              'FE', 'FU', '-', 'VXSOFT', 'VXSQRT', 'VXCVI', 'VE', 'OE', 'UE',
              'ZE', 'XE', 'NI', 'RN0', 'RN1']


def fpscr_str(v):
    return '|'.join(n for i, n in enumerate(FPSCR_BITS) if v & (1 << (31 - i))) or '0'


def decode(rec: bytes):
    c, op, i, kind = struct.unpack('>4I', rec[:16])
    if (kind & 0xff) >= 0x20:
        v = rec[16:80].hex()
        out = rec[80:128]
        cr, traps, srr0, srr1 = struct.unpack('>4I', out[16:32])
        return dict(vector=True, cfg=c, op=op, i=i, a=v[0:32], b=v[32:64], c=v[64:96],
                    v4=out[:16].hex(), vscr=struct.unpack('>I', out[44:48])[0], cr=cr,
                    traps=traps, srr0=srr0, srr1=srr1)
    ins = struct.unpack('>4Q', rec[16:48])
    out = rec[80:128]
    f4, fpscr = struct.unpack('>2Q', out[0:16])
    cr, traps, srr0, srr1 = struct.unpack('>4I', out[16:32])
    store = out[32:40]
    r8, r9 = struct.unpack('>2I', out[40:48])
    return dict(vector=False, cfg=c, op=op, i=i, a=ins[0], b=ins[1], c=ins[2], f4=f4,
                fpscr=fpscr & 0xffffffff, fpscr_hi=fpscr >> 32, cr=cr,
                traps=traps, srr0=srr0, srr1=srr1, store=store.hex(), r8=r8, r9=r9)


def fmt(d):
    if d['vector']:
        return (f"a={d['a']} b={d['b']} c={d['c']} -> v4={d['v4']} vscr={d['vscr']:08x} "
                f"cr={d['cr']:08x} traps={d['traps']} srr0={d['srr0']:08x} srr1={d['srr1']:08x}")
    return (f"a={d['a']:016x} b={d['b']:016x} c={d['c']:016x} -> f4={d['f4']:016x} "
            f"fpscr={d['fpscr']:08x}[{fpscr_str(d['fpscr'])}] cr={d['cr']:08x} "
            f"traps={d['traps']} srr0={d['srr0']:08x} srr1={d['srr1']:08x} "
            f"store={d['store']} r8={d['r8']:08x} r9={d['r9']:08x}")


def hashes(blob: bytes, nops: int):
    out = {}
    for c in range(NCFGS):
        for op in range(nops):
            off = 0x100 + (c * MAX_OPS + op) * 8
            out[(c, op)] = blob[off:off + 8]
    return out


def cmd_conf(a):
    ops = load_ops(a.elf)
    nops = len(ops)
    first, last = 0, nops - 1
    if a.ops:
        names = [o['name'] for o in ops]
        sel = [i for i, n in enumerate(names) if any(n == s or n.startswith(s + '_') or
                                                     n == s + '_rc' for s in a.ops.split(','))]
        if not sel:
            raise SystemExit('no op matches')
        first, last = min(sel), max(sel)
    arms = [s.split('=', 1) for s in a.arm]
    root = Path(a.root)
    params = dict(mode=1, seed=a.seed, count=a.count, cfgmask=a.cfgmask,
                  first=first, last=last)
    results = {}
    errors = []

    def worker(name, opts):
        try:
            q, cpu = split_arm(a.qemu, opts)
            r = run_guest(q, a.elf, params, cpu,
                          root / f'{name}', dumps=[('res', RESULT_BASE, 0x100 + NCFGS * MAX_OPS * 8)],
                          timeout=a.timeout,
                          extra=[x.replace('%n', name) for x in a.qemu_extra.split()])
            results[name] = r
        except Exception as e:  # noqa: BLE001
            errors.append((name, repr(e)))

    pending = list(arms)
    threads = []
    while pending or threads:
        while pending and len(threads) < a.jobs:
            name, opts = pending.pop(0)
            t = threading.Thread(target=worker, args=(name, opts))
            t.start()
            threads.append(t)
        for t in list(threads):
            t.join(timeout=0.1)
            if not t.is_alive():
                threads.remove(t)
    if errors:
        for e in errors:
            print('ERROR', *e)
        return 2
    ref_name = arms[0][0]
    ref = results[ref_name]
    summary = {'seed': a.seed, 'count': a.count, 'cfgmask': a.cfgmask,
               'ops': [ops[i]['name'] for i in range(first, last + 1)], 'arms': {}}
    status = 0
    for name, _ in arms:
        r = results[name]
        vec = struct.unpack('>I', r['res'][0x18:0x1c])[0]
        traps = struct.unpack('>I', r['res'][0x1c:0x20])[0]
        print(f'{name}: done={r["done"]:08x} {r["elapsed"]:.1f}s vectors={vec} traps={traps}')
        summary['arms'][name] = {'elapsed': round(r['elapsed'], 2), 'vectors': vec, 'traps': traps}
    href = hashes(ref['res'], nops)
    for name, opts in arms[1:]:
        h = hashes(results[name]['res'], nops)
        bad = [(c, op) for c in range(NCFGS) for op in range(first, last + 1)
               if (a.cfgmask >> c) & 1 and h[(c, op)] != href[(c, op)]]
        summary['arms'][name]['mismatches'] = len(bad)
        compared = sum(1 for c in range(NCFGS) if (a.cfgmask >> c) & 1) * (last - first + 1)
        print(f'{name} vs {ref_name}: {compared - len(bad)}/{compared} (cfg, op) hashes equal')
        if bad:
            status = 1
            bad_ops = {}
            for c, op in bad:
                bad_ops.setdefault(ops[op]['name'], []).append(c)
            for n, cs in sorted(bad_ops.items()):
                print(f'  MISMATCH {n}: cfgs {cs}')
            for c, op in bad[:a.explain]:
                explain(a, c, op, ops, (ref_name, arms[0][1]), (name, opts), root)
    (root / 'summary.json').write_text(json.dumps(summary, indent=1))
    return status


def explain(a, c, op, ops, ref, arm, root):
    params = dict(mode=2, seed=a.seed, count=min(a.count, a.log_count), cfgmask=1 << c,
                  first=op, last=op)
    logs = {}
    for name, opts in (ref, arm):
        q, cpu = split_arm(a.qemu, opts)
        r = run_guest(q, a.elf, params, cpu,
                      root / f'log-{name}-{c}-{op}', log=True, timeout=a.timeout)
        logs[name] = r['log']
    la, lb = logs[ref[0]], logs[arm[0]]
    n = min(len(la), len(lb)) // LOG_REC
    shown = 0
    for i in range(n):
        ra, rb = la[i * LOG_REC:(i + 1) * LOG_REC], lb[i * LOG_REC:(i + 1) * LOG_REC]
        if ra != rb:
            print(f'  [{ops[op]["name"]} cfg {c} vector {i}]')
            print(f'    {ref[0]:>5}: {fmt(decode(ra))}')
            print(f'    {arm[0]:>5}: {fmt(decode(rb))}')
            shown += 1
            if shown >= a.show:
                break
    if not shown:
        print(f'  [{ops[op]["name"]} cfg {c}] no difference in the first {n} records')


def cmd_bench(a):
    arms = [s.split('=', 1) for s in a.arm]
    root = Path(a.root)
    bn = Path(a.elf).with_name('bench_names.txt')
    names = bn.read_text().split() if bn.exists() else []
    table = {}
    for rep in range(a.repeat):
        for name, opts in arms:
            params = dict(mode=3, iters=a.iters, bmask=a.bmask)
            q, cpu = split_arm(a.qemu, opts)
            r = run_guest(q, a.elf, params, cpu,
                          root / f'{name}-{rep}', dumps=[('bench', BENCH_BASE, 64 * 16)],
                          timeout=a.timeout, extra=a.qemu_extra.split() if a.qemu_extra else ())
            blob = r['bench']
            for k in range(64):
                iters, per, hi, lo = struct.unpack('>4I', blob[k * 16:(k + 1) * 16])
                if not iters:
                    continue
                ticks = (hi << 32) | lo
                ns = ticks * 40.0 / (iters * per)   # 25 MHz timebase
                table.setdefault(k, {}).setdefault(name, []).append(ns)
    print(f'{"kernel":<22}' + ''.join(f'{n:>12}' for n, _ in arms) + '   ns/op (best of %d)' % a.repeat)
    out = {}
    for k in sorted(table):
        label = names[k] if k < len(names) else f'k{k}'
        row = {n: min(v) for n, v in table[k].items()}
        out[label] = row
        base = row.get(arms[0][0])
        cells = ''
        for n, _ in arms:
            v = row.get(n)
            cells += f'{v:12.2f}' if v is not None else ' ' * 12
        if base and len(arms) > 1:
            cells += '   ' + ' '.join(f'x{base / row[n]:.2f}' for n, _ in arms[1:] if row.get(n))
        print(f'{label:<22}{cells}')
    (root / 'bench.json').write_text(json.dumps(out, indent=1))
    return 0


def cmd_smc(a):
    """Self-modifying code (mode 5, smc.c): every arm's record against the
    first arm's and against the value the new code returns."""
    arms = [s.split('=', 1) for s in a.arm]
    root = Path(a.root)
    got = {}
    for name, opts in arms:
        q, cpu = split_arm(a.qemu, opts)
        r = run_guest(q, a.elf, dict(mode=5), cpu, root / f'smc-{name}',
                      dumps=[('res', RESULT_BASE, 0x100 + 3 * 4 * 512)], timeout=a.timeout)
        n = struct.unpack('>I', r['res'][8:12])[0]
        recs = [struct.unpack('>3I', r['res'][0x100 + 12 * i:0x100 + 12 * (i + 1)]) for i in range(n)]
        got[name] = recs
    ref = arms[0][0]
    status = 0
    names = {1: 'cross-page b, target changed', 2: 'bl return site changed',
             3: 'callee rewrites the return site', 4: 'bctr site, target changed',
             5: 'TB across pages, 2nd page changed', 6: 'store into the own TB'}
    print(f'{"scenario":<36}{"var":>4}{"step":>5}{"new":>6}' + ''.join(f'{n:>8}' for n, _ in arms))
    for i, (ident, _, want) in enumerate(got[ref]):
        s, v, k = ident >> 8, (ident >> 4) & 0xf, ident & 0xf
        vals = [got[n][i][1] if i < len(got[n]) else None for n, _ in arms]
        mark = '' if all(x == vals[0] for x in vals) else '  DIFFERENT'
        print(f'{names.get(s, s):<36}{"icbi" if v else "-":>4}{k:>5}{want:>6}'
              + ''.join(f'{x:>8}' for x in vals) + mark)
    for name, _ in arms:
        same = got[name] == got[ref]
        stale = sum(1 for _, o, w in got[name] if o != w)
        print(f'{name}: {len(got[name])} calls, {"equal to " + ref if same else "DIFFERENT from " + ref}, '
              f'{stale} returned the old code')
        if not same:
            status = 1
    return status


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    for name in ('conf', 'bench', 'smc'):
        p = sub.add_parser(name)
        p.add_argument('--qemu', required=True)
        p.add_argument('--elf', default=str(HERE / 'out/fpconf.elf'))
        p.add_argument('--arm', action='append', required=True,
                       help='NAME=cpu-options[@qemu-binary] (first arm is the reference)')
        p.add_argument('--root', default=tempfile.mkdtemp(prefix='fpconf-'))
        p.add_argument('--timeout', type=float, default=7200)
        p.add_argument('--jobs', type=int, default=2)
        p.add_argument('--qemu-extra', default='')
    p = sub.choices['conf']
    p.add_argument('--count', type=int, default=2000)
    p.add_argument('--seed', type=lambda s: int(s, 0), default=0x5eed)
    p.add_argument('--cfgmask', type=lambda s: int(s, 0), default=0xffff)
    p.add_argument('--ops', default='')
    p.add_argument('--explain', type=int, default=6)
    p.add_argument('--show', type=int, default=4)
    p.add_argument('--log-count', type=int, default=20000)
    p = sub.choices['bench']
    p.add_argument('--iters', type=int, default=2000000)
    p.add_argument('--bmask', type=lambda s: int(s, 0), default=0xffffffff)
    p.add_argument('--repeat', type=int, default=3)
    a = ap.parse_args()
    if a.jobs > 4:
        raise SystemExit('cap: at most 4 QEMUs')
    return {'conf': cmd_conf, 'bench': cmd_bench, 'smc': cmd_smc}[a.cmd](a)


if __name__ == '__main__':
    sys.exit(main())
