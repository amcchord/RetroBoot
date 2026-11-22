#!/usr/bin/env python3
"""Idle-cost harness: what an idle Mac OS 9 Finder costs the host.

Boots a sparse copy of a disk with hostd's argv (rbd_restart_lab.build_argv:
-display retroboot behind the presenter stub with REFRESH_RATE 16 and the
connect-time RESIZE, two QMP sockets, the driver variant ahead of the
package, pio-status-drain, Sungem NAT, virtio tablet, optional 9p share),
waits for the Finder, lets the guest settle, then samples the QEMU process
for a window:

  * CPU: process utime+stime (/proc/PID/stat, includes exited threads) and
    per-thread run time (/proc/PID/task/TID/schedstat, ns) split into the
    vCPU thread (QMP query-cpus-fast), the main loop (TID == PID) and the
    rest (RCU, thread pool, audio);
  * wakeups: voluntary context switches per thread per second;
  * display: DAMAGE messages, rects and pixels the stub received;
  * host: whole-machine busy fraction and load average (other tenants).

Optional, after the window (so they never disturb the CPU numbers):
``--perf S`` records ``perf record -g`` for S seconds (``-perfmap`` is added
so TCG code is attributed to guest PCs), ``--pc-samples N`` samples the
guest's NIP/MSR/HID0 N times through HMP ``info registers``.  ``--pointer``
moves the pointer continuously during the window (60 Hz circle).  The run
ends with Mac OS Shut Down (power key, Return; default) or a kill.

    rbd_idle.py run --out RUN --seed IMG [--pkg DIR] [--size 1280x720]
        [--settle 30] [--window 120] [--interval 10] [--perf 20]
        [--pc-samples 0] [--pointer] [--end shutdown|kill] [--ndrvloader FILE]
        [--steps STEPS.json] [-- QEMU ARGS]
    rbd_idle.py summarize RUN...      # one line per run, grouped by label

Extra QEMU arguments after ``--`` are appended (QEMU takes the last value of
a repeated -global/-rtc).  The guest clock is fixed with
``-rtc base=2026-10-11T09:00:00`` unless ``--clock localtime`` (see
emulator/reports/startup-stall-20261011.md 7.1).  Lab tool for the Linux
builder, Python standard library only; evidence stays in RUN.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import signal
import statistics
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rbd_presenter_stub as stub  # noqa: E402
import rbd_restart_lab as lab  # noqa: E402

CLK_TCK = os.sysconf('SC_CLK_TCK')


# -- /proc sampling ------------------------------------------------------
def proc_cpu_ticks(pid: int) -> int:
    with open(f'/proc/{pid}/stat') as f:
        fields = f.read().rsplit(')', 1)[1].split()
    return int(fields[11]) + int(fields[12])     # utime + stime (fields 14, 15)


def threads(pid: int) -> dict[int, dict]:
    out = {}
    for tid in os.listdir(f'/proc/{pid}/task'):
        base = f'/proc/{pid}/task/{tid}'
        try:
            with open(base + '/schedstat') as f:
                run_ns, _wait_ns, slices = (int(x) for x in f.read().split())
            vcs = nvcs = 0
            with open(base + '/status') as f:
                for line in f:
                    if line.startswith('voluntary_ctxt_switches'):
                        vcs = int(line.split()[1])
                    elif line.startswith('nonvoluntary_ctxt_switches'):
                        nvcs = int(line.split()[1])
            with open(base + '/comm') as f:
                comm = f.read().strip()
        except (OSError, ValueError):
            continue
        out[int(tid)] = {'run_ns': run_ns, 'slices': slices, 'vcs': vcs, 'nvcs': nvcs, 'comm': comm}
    return out


def host_busy() -> tuple[int, int]:
    with open('/proc/stat') as f:
        v = [int(x) for x in f.readline().split()[1:]]
    idle = v[3] + v[4]
    return sum(v) - idle, sum(v)


class Sampler:
    """Cumulative counters at the window start and end (plus interval rows)."""

    def __init__(self, pid: int, vcpu_tids: set[int]):
        self.pid = pid
        self.vcpu = vcpu_tids

    def snap(self) -> dict:
        busy, total = host_busy()
        return {'mono': time.monotonic(), 'ticks': proc_cpu_ticks(self.pid), 'threads': threads(self.pid),
                'host_busy': busy, 'host_total': total, 'load1': os.getloadavg()[0]}

    def role(self, tid: int) -> str:
        if tid in self.vcpu:
            return 'vcpu'
        if tid == self.pid:
            return 'main'
        return 'other'

    def delta(self, a: dict, b: dict) -> dict:
        dt = b['mono'] - a['mono']
        roles = {r: {'cpu_pct': 0.0, 'wakeups_s': 0.0, 'preempt_s': 0.0} for r in ('vcpu', 'main', 'other')}
        per_thread = []
        for tid, tb in b['threads'].items():
            ta = a['threads'].get(tid, {'run_ns': 0, 'vcs': 0, 'nvcs': 0, 'slices': 0})
            cpu = (tb['run_ns'] - ta['run_ns']) / 1e9 / dt * 100
            wk = (tb['vcs'] - ta['vcs']) / dt
            pre = (tb['nvcs'] - ta['nvcs']) / dt
            r = roles[self.role(tid)]
            r['cpu_pct'] += cpu
            r['wakeups_s'] += wk
            r['preempt_s'] += pre
            per_thread.append({'tid': tid, 'role': self.role(tid), 'comm': tb['comm'], 'cpu_pct': round(cpu, 2),
                               'wakeups_s': round(wk, 1)})
        for r in roles.values():
            for k in r:
                r[k] = round(r[k], 2)
        hb = (b['host_busy'] - a['host_busy']) / max(1, b['host_total'] - a['host_total'])
        return {
            'seconds': round(dt, 2),
            'cpu_pct': round((b['ticks'] - a['ticks']) / CLK_TCK / dt * 100, 2),
            'roles': roles,
            'wakeups_s': round(sum(r['wakeups_s'] for r in roles.values()), 1),
            'threads': sorted(per_thread, key=lambda t: -t['cpu_pct'])[:8],
            'host_busy_pct': round(hb * 100, 1),
            'load1': round(b['load1'], 2),
        }


class LatencyPresenter(stub.Presenter):
    """The stub, plus a timestamped log of DAMAGE rects while damage_log is a list."""

    damage_log = None

    def handle(self, cid, data, fds, flags):
        log = self.damage_log
        if log is not None and len(data) >= 16 and struct.unpack_from('=I', data)[0] == stub.DAMAGE:
            count = struct.unpack_from('=I', data, 12)[0]
            log.append((time.monotonic(), [struct.unpack_from('=iiii', data, 16 + 16 * i) for i in range(count)]))
        super().handle(cid, data, fds, flags)


def pointer_latency(p: LatencyPresenter, n: int, gap: float) -> dict:
    """Move the pointer n times between distant points; for each move, the time
    until a DAMAGE rect covers the new pointer position (software cursor)."""
    w, h, _, _ = p.frame_raw()
    pts = [(w // 3, h // 3), (2 * w // 3, 2 * h // 3)]
    lat, missed = [], 0
    for i in range(n):
        x, y = pts[i % 2]
        x += (i * 7) % 23
        y += (i * 5) % 17
        p.damage_log = []
        t0 = time.monotonic()
        p.pointer(x, y)
        hit = None
        while time.monotonic() - t0 < 1.0 and hit is None:
            time.sleep(0.002)
            for t, rects in list(p.damage_log):
                if any(rx <= x + 2 < rx + rw and ry <= y + 2 < ry + rh for rx, ry, rw, rh in rects):
                    hit = t
                    break
        p.damage_log = None
        if hit is None:
            missed += 1
        else:
            lat.append((hit - t0) * 1000)
        time.sleep(gap)
    lat.sort()
    q = lambda f: round(lat[min(len(lat) - 1, int(f * len(lat)))], 1) if lat else None  # noqa: E731
    return {'moves': n, 'missed': missed, 'median_ms': q(0.5), 'p90_ms': q(0.9), 'max_ms': round(max(lat), 1) if lat else None,
            'mean_ms': round(statistics.mean(lat), 1) if lat else None}


# -- the run ---------------------------------------------------------------
def lab_args(a, run: Path) -> argparse.Namespace:
    """The namespace rbd_restart_lab.build_argv expects."""
    extra = list(a.extra)
    if a.clock != 'localtime':
        extra += ['-rtc', f'base={a.clock}']
    if a.perf:
        extra += ['-perfmap']
    audio = a.audio
    ns = argparse.Namespace(pkg=a.pkg, ndrv_dir=lab.NDRV.get(a.ndrv, a.ndrv), size=a.size, mem=a.mem,
                            audio='none', host_resize='on', drain=True, speed='realtime', net=a.net,
                            share=a.share, trace_events=None, extra=extra, ndrvloader=a.ndrvloader)
    argv = lab.build_argv(ns, run, Path(a.disk), 0)
    if audio != 'none':
        i = argv.index('-audiodev')
        argv[i + 1] = {'wav': f'wav,id=snd0,path={run}/audio.wav'}.get(audio, audio)
    return argv


def pointer_loop(p: stub.Presenter, stop: threading.Event, w: int, h: int, hz: float):
    t0 = time.monotonic()
    n = 0
    while not stop.is_set():
        ang = (time.monotonic() - t0) * 2 * math.pi / 4.0      # one circle per 4 s
        x = int(w / 2 + w / 4 * math.cos(ang))
        y = int(h / 2 + h / 4 * math.sin(ang))
        try:
            p.pointer(x, y)
            n += 1
        except (RuntimeError, OSError):
            pass
        stop.wait(1 / hz)
    return n


def run(a):
    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    disk = Path(a.disk or out / 'disk.img').resolve()
    a.disk = str(disk)
    subprocess.run(['cp', '--sparse=always', a.seed, str(disk)], check=True)
    os.chmod(disk, 0o644)
    p = LatencyPresenter(out / 'display.sock', None)
    threading.Thread(target=p.accept_loop, daemon=True).start()
    for s in ('qmp-control.sock', 'qmp-events.sock'):
        try:
            (out / s).unlink()
        except FileNotFoundError:
            pass
    (out / 'boot0').mkdir(exist_ok=True)
    argv = lab_args(a, out)
    if a.display_extra:
        i = argv.index('-display')
        argv[i + 1] += ',' + a.display_extra
    if a.taskset:
        argv = ['taskset', '-c', a.taskset] + argv
    (out / 'argv.json').write_text(json.dumps(argv, indent=1))
    env = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'RETROBOOT_SESSION': 'idle', 'HOME': '/root',
           'LANG': 'C.UTF-8', 'TZ': ':UTC'}
    for kv in a.env:
        k, v = kv.split('=', 1)
        env[k] = v
    t0 = time.monotonic()
    proc = subprocess.Popen(argv, stdout=open(out / 'qemu.out', 'w'), stderr=subprocess.STDOUT,
                            env=env, cwd=str(out), start_new_session=True)
    res = {'label': a.label, 'pkg': a.pkg, 'env': a.env, 'refresh_ms': a.refresh_ms, 'size': '%dx%d' % a.size, 'audio': a.audio, 'extra': a.extra,
           'pointer': a.pointer, 'load1_start': round(os.getloadavg()[0], 2)}

    def kill():
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(10)

    def bye(*_):
        kill()
        sys.exit(2)
    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)
    ev = ctl = None
    try:
        ev = lab.QMP(str(out / 'qmp-events.sock'))
        ctl = lab.QMP(str(out / 'qmp-control.sock'))
        cpus = ctl.cmd('query-cpus-fast').get('return', [])
        qpid = proc.pid
        if a.taskset:   # taskset execs QEMU in place: same pid
            pass
        vcpu = {c['thread-id'] for c in cpus}
        res['qemu_pid'] = qpid
        res['vcpu_tids'] = sorted(vcpu)
        hello = p.wait_for(lambda: p.hello, 20)
        if hello:
            p.send(stub.REFRESH_RATE, struct.pack('=I', 16))
            p.send(stub.RESIZE, struct.pack('=II', *a.size))
        # Finder
        finder = None
        while time.monotonic() - t0 < a.timeout and proc.poll() is None:
            try:
                w, h, stride, raw = p.frame_raw()
                if stub.analyze_frame(w, h, stride, raw)['finder']:
                    finder = time.monotonic()
                    break
            except RuntimeError:
                pass
            time.sleep(0.25)
        if finder is None:
            res['outcome'] = 'no-finder'
            lab.save_png(p, out / 'timeout.png')
            return res
        res['finder_s'] = round(finder - t0, 2)
        if a.steps:
            # e.g. open the shared volume's window before settling (stub control requests)
            for step in json.loads(Path(a.steps).read_text()):
                step = dict(step)
                if step.get('cmd') == 'sleep':
                    time.sleep(step['seconds'])
                else:
                    stub.dispatch(p, step)
            lab.save_png(p, out / 'after-steps.png')
        sampler = Sampler(qpid, vcpu)
        # settle: keep interval rows from Finder on, so settling is visible
        rows = []
        s_prev = sampler.snap()
        while time.monotonic() - finder < a.settle:
            time.sleep(min(a.interval, a.settle - (time.monotonic() - finder) + 0.01))
            s = sampler.snap()
            d = sampler.delta(s_prev, s)
            d['phase'] = 'settle'
            d['t'] = round(s['mono'] - finder, 1)
            rows.append(d)
            s_prev = s
        lab.save_png(p, out / 'window-start.png')
        stop = threading.Event()
        pthread = None
        if a.pointer:
            w, h, _, _ = p.frame_raw()
            pthread = threading.Thread(target=pointer_loop, args=(p, stop, w, h, a.pointer_hz), daemon=True)
        if a.refresh_ms != 16:
            p.send(stub.REFRESH_RATE, struct.pack('=I', a.refresh_ms))
        with p.lock:
            p.reset_stats()
        start = sampler.snap()
        if pthread:
            pthread.start()
        s_prev = start
        while time.monotonic() - start['mono'] < a.window - 0.01:
            time.sleep(min(a.interval, a.window - (time.monotonic() - start['mono'])))
            s = sampler.snap()
            d = sampler.delta(s_prev, s)
            d['phase'] = 'window'
            d['t'] = round(s['mono'] - finder, 1)
            rows.append(d)
            s_prev = s
        end = sampler.snap()
        stop.set()
        if pthread:
            pthread.join()
        with p.lock:
            dmg = p.stats_snapshot()
        res['window'] = sampler.delta(start, end)
        res['window']['interval_cpu_pct'] = [r['cpu_pct'] for r in rows if r['phase'] == 'window']
        res['damage'] = dmg
        res['rows'] = rows
        lab.save_png(p, out / 'window-end.png')
        # diagnostics after the window
        if a.latency:
            res['pointer_latency'] = pointer_latency(p, a.latency, a.latency_gap)
        if a.pc_samples:
            samples = []
            for _ in range(a.pc_samples):
                txt = ctl.hmp('info registers').get('return', '')
                samples.append(parse_regs(txt))
                time.sleep(a.pc_interval)
            (out / 'pc-samples.json').write_text(json.dumps(samples))
            hist = {}
            for smp in samples:
                k = f"{smp.get('NIP')} MSR={smp.get('MSR')} HID0={smp.get('HID0')} halted={smp.get('HALTED')}"
                hist[k] = hist.get(k, 0) + 1
            res['pc_hist'] = sorted(hist.items(), key=lambda kv: -kv[1])[:20]
        if a.perf:
            data = out / 'perf.data'
            subprocess.run(['perf', 'record', '-q', '-F', str(a.perf_freq), '-g', '-p', str(qpid), '-o', str(data),
                            '--', 'sleep', str(a.perf)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            mp = Path(f'/tmp/perf-{qpid}.map')
            if mp.exists():
                shutil.copy(mp, out / 'perf.map')
            for name, sort in (('perf-sym.txt', 'sym'), ('perf-dso.txt', 'dso'), ('perf-comm.txt', 'comm,dso')):
                with open(out / name, 'w') as f:
                    subprocess.run(['perf', 'report', '-i', str(data), '--stdio', '--no-children', '--sort', sort,
                                    '-g', 'none', '--percent-limit', '0.3'], stdout=f, stderr=subprocess.DEVNULL)
            with open(out / 'perf-callers.txt', 'w') as f:
                subprocess.run(['perf', 'report', '-i', str(data), '--stdio', '--no-children', '--sort', 'sym',
                                '-G', '--percent-limit', '2'], stdout=f, stderr=subprocess.DEVNULL)
        if a.hold:
            # keep the idle guest for out-of-band probes (perf probe, gdb);
            # RUN/hold holds the QEMU pid; deleting it ends the hold early
            hold = out / 'hold'
            hold.write_text(str(qpid))
            t_end = time.monotonic() + a.hold
            while time.monotonic() < t_end and hold.exists() and proc.poll() is None:
                cmds = out / 'hmp.in'
                if cmds.exists():
                    # one HMP command per line; "sleep S" pauses
                    lines = cmds.read_text().splitlines()
                    cmds.unlink()
                    with open(out / 'hmp.out', 'a') as f:
                        for line in lines:
                            if line.startswith('sleep '):
                                time.sleep(float(line.split()[1]))
                            elif line.strip():
                                f.write(f'## {time.time():.3f} {line}\n' + ctl.hmp(line).get('return', '') + '\n')
                time.sleep(0.2)
            hold.unlink(missing_ok=True)
        # end
        if a.end == 'shutdown':
            ta = time.monotonic()
            p.tap(['power'], hold=0.15)
            time.sleep(2.0)
            lab.save_png(p, out / 'power-dialog.png')
            p.tap(['ret'], hold=0.1)
            try:
                proc.wait(a.exit_timeout)
            except subprocess.TimeoutExpired:
                lab.save_png(p, out / 'shutdown-timeout.png')
                res['shutdown_timeout'] = True
            with ev.cond:
                sd = [e for e in ev.events if e.get('event') == 'SHUTDOWN']
            res['shutdown'] = sd[-1]['data'] if sd else None
            res['shutdown_s'] = round(time.monotonic() - ta, 2)
        res['outcome'] = 'ok'
        return res
    finally:
        for q in (ev, ctl):
            if q:
                q.close()
        kill()
        res['exit_code'] = proc.returncode
        try:
            Path(f'/tmp/perf-{proc.pid}.map').unlink()
        except FileNotFoundError:
            pass
        if not a.keep_disk:
            disk.unlink(missing_ok=True)
        (out / 'result.json').write_text(json.dumps(res, indent=1))
        p.stopping = True
        try:
            p.listener.close()
        except OSError:
            pass


def parse_regs(txt: str) -> dict:
    out = {}
    toks = txt.replace('\n', ' ').split()
    for i, t in enumerate(toks):
        if t in ('NIP', 'LR', 'MSR', 'HID0', 'HF', 'DECR') and i + 1 < len(toks):
            out[t] = toks[i + 1]
        if t.startswith('HALTED') or t == 'halted':
            out['HALTED'] = toks[i + 1] if i + 1 < len(toks) else '?'
    for t in toks:
        if t.startswith('idx'):
            break
    return out


# -- summary -------------------------------------------------------------
def summarize(dirs):
    rows = {}
    for d in dirs:
        rj = Path(d) / 'result.json'
        try:
            r = json.loads(rj.read_text())
        except (OSError, ValueError):
            continue
        if r.get('outcome') != 'ok':
            print(f'{d}: {r.get("outcome")}')
            continue
        rows.setdefault(r.get('label') or Path(d).name, []).append(r)
    hdr = ('label', 'n', 'qemu%', 'sd', 'vcpu%', 'main%', 'other%', 'wake/s', 'vcpu wk/s', 'main wk/s',
           'dmg/s', 'Mpx/s', 'host%', 'finder s', 'shutdown', 'lat med', 'lat p90')
    print('  '.join(f'{h:>9}' for h in hdr))
    for label, rs in rows.items():
        def m(f):
            v = [f(r) for r in rs]
            return statistics.mean(v)
        cpu = [r['window']['cpu_pct'] for r in rs]
        sd = statistics.stdev(cpu) if len(cpu) > 1 else 0.0
        sh = sum(1 for r in rs if (r.get('shutdown') or {}).get('reason') == 'guest-shutdown')
        vals = (label, len(rs), m(lambda r: r['window']['cpu_pct']), sd,
                m(lambda r: r['window']['roles']['vcpu']['cpu_pct']),
                m(lambda r: r['window']['roles']['main']['cpu_pct']),
                m(lambda r: r['window']['roles']['other']['cpu_pct']),
                m(lambda r: r['window']['wakeups_s']),
                m(lambda r: r['window']['roles']['vcpu']['wakeups_s']),
                m(lambda r: r['window']['roles']['main']['wakeups_s']),
                m(lambda r: r['damage']['messages_per_s']), m(lambda r: r['damage']['mpixels_per_s']),
                m(lambda r: r['window']['host_busy_pct']), m(lambda r: r['finder_s']), f'{sh}/{len(rs)}',
                m(lambda r: (r.get('pointer_latency') or {}).get('median_ms') or 0.0),
                m(lambda r: (r.get('pointer_latency') or {}).get('p90_ms') or 0.0))
        print('  '.join(f'{v:>9.2f}' if isinstance(v, float) else f'{str(v):>9}' for v in vals))


def main():
    if len(sys.argv) > 1 and sys.argv[1] == 'summarize':
        summarize(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == 'run':
        argv = sys.argv[2:]
    else:
        sys.exit(__doc__)
    split = argv.index('--') if '--' in argv else len(argv)
    ap = argparse.ArgumentParser(prog='rbd_idle.py run')
    ap.add_argument('--out', required=True)
    ap.add_argument('--seed', required=True)
    ap.add_argument('--disk', default=None)
    ap.add_argument('--keep-disk', action='store_true')
    ap.add_argument('--label', default=None)
    ap.add_argument('--pkg', default=lab.PKG)
    ap.add_argument('--ndrv', default='v2')
    ap.add_argument('--size', default='1280x720', type=lab.parse_size)
    ap.add_argument('--mem', type=int, default=512)
    ap.add_argument('--audio', default='none', help='none | wav | an -audiodev value (id=snd0)')
    ap.add_argument('--share', default=None)
    ap.add_argument('--ndrvloader', default=None, help="classicvirtio loader instead of the package's")
    ap.add_argument('--steps', default=None, help='JSON list of stub control requests run after Finder ({"cmd":"sleep","seconds":S} waits)')
    ap.add_argument('--no-net', dest='net', action='store_false')
    ap.add_argument('--clock', default='2026-10-11T09:00:00', help='-rtc base value, or localtime')
    ap.add_argument('--env', action='append', default=[], help='K=V for the QEMU environment')
    ap.add_argument('--display-extra', default=None, help='appended to the -display retroboot options')
    ap.add_argument('--taskset', default=None, help='CPU list for taskset')
    ap.add_argument('--timeout', type=float, default=150)
    ap.add_argument('--settle', type=float, default=30)
    ap.add_argument('--window', type=float, default=120)
    ap.add_argument('--interval', type=float, default=10)
    ap.add_argument('--refresh-ms', type=int, default=16, help='REFRESH_RATE sent for the window (16 = presenter)')
    ap.add_argument('--pointer', action='store_true')
    ap.add_argument('--pointer-hz', type=float, default=60)
    ap.add_argument('--latency', type=int, default=0, help='pointer moves for the frame-latency probe (after the window)')
    ap.add_argument('--latency-gap', type=float, default=0.3)
    ap.add_argument('--perf', type=float, default=0)
    ap.add_argument('--perf-freq', type=int, default=997)
    ap.add_argument('--pc-samples', type=int, default=0)
    ap.add_argument('--pc-interval', type=float, default=0.1)
    ap.add_argument('--hold', type=float, default=0, help='keep the idle guest running this long after the window')
    ap.add_argument('--end', choices=['shutdown', 'kill'], default='shutdown')
    ap.add_argument('--exit-timeout', type=float, default=60)
    ap.add_argument('extra', nargs='*')
    a = ap.parse_args(argv[:split])
    a.extra = argv[split + 1:]
    a.label = a.label or Path(a.out).name
    res = run(a)
    w = res.get('window', {})
    print(json.dumps({'label': a.label, 'outcome': res.get('outcome'), 'finder_s': res.get('finder_s'),
                      'cpu_pct': w.get('cpu_pct'), 'roles': w.get('roles'), 'wakeups_s': w.get('wakeups_s'),
                      'damage_per_s': res.get('damage', {}).get('messages_per_s'),
                      'shutdown': (res.get('shutdown') or {}).get('reason')}), flush=True)


if __name__ == '__main__':
    main()
