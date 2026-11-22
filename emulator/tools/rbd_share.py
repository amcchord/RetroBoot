#!/usr/bin/env python3
"""Shared-folder lab (emulator/reports/sharefix-20261012.md): one Mac OS guest
with hostd's argv (rbd_restart_lab.build_argv) behind the presenter stub's
control socket, kept running between commands so a share driver can be
watched, driven and measured.

    rbd_share.py up      --run RUN --seed IMG [--pkg DIR] [--ndrvloader FILE]
                         [--share DIR | --no-share] [--size 1280x720]
                         [--clock 2026-10-11T09:00:00 | localtime] [--tz ZONE] [--driver-log] [-- QEMU ARGS]
    rbd_share.py ctl     --run RUN '{"cmd":"click","x":10,"y":10}' ...   stub requests
    rbd_share.py shot    --run RUN NAME                RUN/NAME.png
    rbd_share.py measure --run RUN --seconds 120 [--label L]   QEMU CPU/wakeups by thread role
    rbd_share.py hmp     --run RUN 'info registers'
    rbd_share.py refresh --run RUN --region X0,Y0,X1,Y1 [--create PATH] [--timeout 30]
                                                       time until the region (an open Finder
                                                       window) changes after PATH is created
    rbd_share.py down    --run RUN [--kill]            Mac OS Shut Down (power, Return)

``--driver-log`` adds a virtio console whose emergency-write register
classicvirtio's drivers find and print every File Manager call to
(RUN/driver.log, with the current application's name); it slows the driver
a lot, so never measure with it.  ``--ndrvloader`` replaces the package's
loader (the package is never modified).  The guest clock is pinned (Software
Update's Sunday check, startup-stall report 7.1) unless ``--clock localtime``.
Lab tool for the Linux builder, Python standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import rbd_idle as idle  # noqa: E402
import rbd_presenter_stub as stub  # noqa: E402
import rbd_restart_lab as lab  # noqa: E402


def client(run: Path, timeout: float = 300) -> stub.StubClient:
    return stub.StubClient(run / 'ctl.sock', timeout)


def build(a, run: Path) -> list[str]:
    extra = list(a.extra)
    if a.clock != 'localtime':
        extra += ['-rtc', f'base={a.clock}']
    if a.driver_log:
        extra += ['-device', 'virtio-serial-pci', '-device', 'virtconsole,chardev=drvlog',
                  '-chardev', f'file,id=drvlog,path={run}/driver.log']
    ns = argparse.Namespace(pkg=a.pkg, ndrv_dir=lab.NDRV.get(a.ndrv, a.ndrv), size=a.size, mem=a.mem,
                            audio='none', host_resize='on', drain=True, speed='realtime', net=a.net,
                            share=None if a.no_share else a.share, trace_events=None, extra=extra)
    argv = lab.build_argv(ns, run, run / 'disk.img', 0)
    if a.ndrvloader:
        i = next(i for i, v in enumerate(argv) if v.startswith('loader,addr=0x4000000,file='))
        argv[i] = f'loader,addr=0x4000000,file={Path(a.ndrvloader).resolve()}'
    return argv


def up(a):
    run = Path(a.run).resolve()
    run.mkdir(parents=True, exist_ok=True)
    disk = run / 'disk.img'
    if not (a.reuse and disk.exists()):
        subprocess.run(['cp', '--sparse=always', a.seed, str(disk)], check=True)
        os.chmod(disk, 0o644)
    for s in ('qmp-control.sock', 'qmp-events.sock', 'ctl.sock'):
        (run / s).unlink(missing_ok=True)
    stub_pid = subprocess.Popen([sys.executable, str(HERE / 'rbd_presenter_stub.py'), 'serve',
                                 '--socket', str(run / 'display.sock'), '--control', str(run / 'ctl.sock'),
                                 '--log', str(run / 'presenter.jsonl')],
                                stdout=open(run / 'stub.out', 'a'), stderr=subprocess.STDOUT,
                                start_new_session=True).pid
    for _ in range(100):
        if (run / 'ctl.sock').exists():
            break
        time.sleep(0.05)
    argv = build(a, run)
    (run / 'argv.json').write_text(json.dumps(argv, indent=1))
    env = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'RETROBOOT_SESSION': 'share', 'HOME': '/root',
           'LANG': 'C.UTF-8', 'TZ': ':' + a.tz}
    t0 = time.monotonic()
    q = subprocess.Popen(argv, stdout=open(run / 'qemu.out', 'w'), stderr=subprocess.STDOUT, env=env,
                         cwd=str(run), start_new_session=True)
    (run / 'pids.json').write_text(json.dumps({'stub': stub_pid, 'qemu': q.pid}))
    c = client(run)
    c('wait_connected', timeout=30)
    c('refresh_rate', ms=16)
    c('resize', width=a.size[0], height=a.size[1])
    r = c('wait_finder', timeout=a.timeout, poll=0.25)
    out = {'finder_s': round(r['mono'] - t0, 2), 'qemu_pid': q.pid}
    print(json.dumps(out))
    (run / 'up.json').write_text(json.dumps(out))


def pids(run: Path) -> dict:
    return json.loads((run / 'pids.json').read_text())


def ctl(a):
    c = client(Path(a.run).resolve())
    for req in a.requests:
        req = json.loads(req)
        print(json.dumps(c(req.pop('cmd'), **req)))


def shot(a):
    run = Path(a.run).resolve()
    print(json.dumps(client(run)('shot', path=str(run / f'{a.name}.png'))))


def qmp(run: Path) -> lab.QMP:
    return lab.QMP(str(run / 'qmp-control.sock'))


def hmp(a):
    q = qmp(Path(a.run).resolve())
    try:
        for line in a.lines:
            print(q.hmp(line).get('return', ''))
    finally:
        q.close()


def measure(a):
    run = Path(a.run).resolve()
    pid = pids(run)['qemu']
    q = qmp(run)
    try:
        vcpu = {c['thread-id'] for c in q.cmd('query-cpus-fast').get('return', [])}
    finally:
        q.close()
    s = idle.Sampler(pid, vcpu)
    rows = []
    start = prev = s.snap()
    while time.monotonic() - start['mono'] < a.seconds - 0.01:
        time.sleep(min(a.interval, a.seconds - (time.monotonic() - start['mono'])))
        cur = s.snap()
        rows.append(s.delta(prev, cur)['cpu_pct'])
        prev = cur
    res = s.delta(start, prev)
    res['interval_cpu_pct'] = rows
    res['label'] = a.label
    res['wall'] = time.time()
    if a.label:
        (run / f'measure-{a.label}.json').write_text(json.dumps(res, indent=1))
    print(json.dumps({k: res[k] for k in ('label', 'seconds', 'cpu_pct', 'wakeups_s', 'roles', 'host_busy_pct')}))


def region_sig(c, run: Path, region) -> str:
    import hashlib
    p = run / '.probe.ppm'
    c('shot', path=str(p))
    data = p.read_bytes()
    hdr = data.split(b'\n', 3)
    w = int(hdr[1].split()[0])
    px = hdr[3]
    x0, y0, x1, y1 = region
    m = hashlib.md5()
    for y in range(y0, y1):
        m.update(px[(y * w + x0) * 3:(y * w + x1) * 3])
    return m.hexdigest()


def refresh(a):
    """Latency from creating a host file to the first change inside the
    region (the open Finder window), sampled every --poll s."""
    run = Path(a.run).resolve()
    c = client(run)
    region = tuple(int(v) for v in a.region.split(','))
    before = region_sig(c, run, region)
    t0 = time.monotonic()
    if a.create:
        Path(a.create).write_text('RetroBoot refresh probe\n')
    if a.command:
        subprocess.run(a.command, shell=True, check=True)
    t_created = time.monotonic()
    res = {'created_s': round(t_created - t0, 3), 'changed_s': None}
    while time.monotonic() - t0 < a.timeout:
        if region_sig(c, run, region) != before:
            res['changed_s'] = round(time.monotonic() - t_created, 2)
            break
        time.sleep(a.poll)
    if a.shot:
        c('shot', path=str(run / f'{a.shot}.png'))
    print(json.dumps(res))


def down(a):
    run = Path(a.run).resolve()
    p = pids(run)
    res = {}
    if not a.kill:
        try:
            c = client(run, 30)
            c('tap', keys=['power'], hold=0.15)
            time.sleep(2.0)
            c('shot', path=str(run / 'power-dialog.png'))
            c('tap', keys=['ret'], hold=0.1)
            t0 = time.monotonic()
            while time.monotonic() - t0 < a.timeout:
                try:
                    os.kill(p['qemu'], 0)
                except ProcessLookupError:
                    break
                time.sleep(0.25)
            res['shutdown_s'] = round(time.monotonic() - t0, 2)
        except Exception as exc:  # noqa: BLE001
            res['error'] = str(exc)
    for k in ('qemu', 'stub'):
        try:
            os.killpg(p[k], signal.SIGKILL)
            res.setdefault('killed', []).append(k)
        except ProcessLookupError:
            pass
    print(json.dumps(res))


def main():
    argv = sys.argv[1:]
    split = argv.index('--') if '--' in argv else len(argv)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='mode', required=True)
    u = sub.add_parser('up')
    u.add_argument('--run', required=True)
    u.add_argument('--seed', required=True)
    u.add_argument('--reuse', action='store_true', help='boot RUN/disk.img if it exists')
    u.add_argument('--pkg', default=lab.PKG)
    u.add_argument('--ndrv', default='v2')
    u.add_argument('--ndrvloader', default=None)
    u.add_argument('--share', default=None)
    u.add_argument('--no-share', action='store_true')
    u.add_argument('--no-net', dest='net', action='store_false')
    u.add_argument('--size', default='1280x720', type=lab.parse_size)
    u.add_argument('--mem', type=int, default=512)
    u.add_argument('--clock', default='2026-10-11T09:00:00')
    u.add_argument('--tz', default='UTC', help='TZ for QEMU (with --clock localtime the Mac clock follows it, as hostd does)')
    u.add_argument('--driver-log', action='store_true')
    u.add_argument('--timeout', type=float, default=240)
    for name, fn in (('ctl', ctl), ('shot', shot), ('hmp', hmp), ('measure', measure), ('down', down),
                     ('refresh', refresh)):
        s = sub.add_parser(name)
        s.add_argument('--run', required=True)
        s.set_defaults(fn=fn)
        if name == 'ctl':
            s.add_argument('requests', nargs='+')
        elif name == 'shot':
            s.add_argument('name')
        elif name == 'hmp':
            s.add_argument('lines', nargs='+')
        elif name == 'measure':
            s.add_argument('--seconds', type=float, default=120)
            s.add_argument('--interval', type=float, default=10)
            s.add_argument('--label', default=None)
        elif name == 'refresh':
            s.add_argument('--region', required=True)
            s.add_argument('--create', default=None)
            s.add_argument('--command', default=None, help='shell command that changes the share')
            s.add_argument('--timeout', type=float, default=30)
            s.add_argument('--poll', type=float, default=0.25)
            s.add_argument('--shot', default=None)
        elif name == 'down':
            s.add_argument('--kill', action='store_true')
            s.add_argument('--timeout', type=float, default=60)
    a = ap.parse_args(argv[:split])
    a.extra = argv[split + 1:]
    if a.mode == 'up':
        if not a.share and not a.no_share:
            ap.error('--share DIR or --no-share')
        up(a)
    else:
        a.fn(a)


if __name__ == '__main__':
    main()
