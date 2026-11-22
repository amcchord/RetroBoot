#!/usr/bin/env python3
"""Finder smoke test for a package on a slow or emulated host (wave6/arm-emu).

Boots a sparse copy of a disk with rbd_boottime.py (hostd's argv, presenter stub,
control socket) and, once the Finder menu bar is seen, checks that the guest
answers input and shuts down cleanly:

  1. waits until the display has been quiet for --quiet-s (startup finished);
  2. pointer: clicks the Apple menu (x 20, y 10) and waits until the menu is
     drawn (>= 2 % of the region under it changed), then Escape and waits until
     the region is back (menu closed);
  3. keyboard and shutdown: rbd_boottime's power key -> Shut Down dialog ->
     Return, and QEMU's SHUTDOWN event (guest-shutdown).

Each wait is open-ended up to --step-timeout, so the times are the guest's own
response times on this host.  An aarch64 package runs on the x86 builder under
qemu-user (binfmt) with --env QEMU_LD_PREFIX=<arm64 sysroot>: those times are an
emulated host's, never a Raspberry Pi's.

    rbd_arm64_smoke.py --out RUN --seed DISK --pkg PKG --ndrv NDRV [--env K=V]...
        [--timeout S] [-- extra rbd_boottime boot options]

Writes RUN/smoke.json (and rbd_boottime's RUN/result.json, frames as PNG).
Lab tool, Python standard library only; evidence stays in RUN.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from rbd_presenter_stub import StubClient  # noqa: E402


def load_ppm(path: Path):
    head, dims, _mx, px = path.read_bytes().split(b'\n', 3)
    w, h = map(int, dims.split())
    return w, h, px


def changed(a: Path, b: Path, region) -> float:
    w, h, pa = load_ppm(a)
    w2, h2, pb = load_ppm(b)
    if (w, h) != (w2, h2):
        return 1.0
    x0, y0, x1, y1 = region
    diff = total = 0
    for y in range(y0, min(y1, h)):
        ra = pa[(y * w + x0) * 3:(y * w + min(x1, w)) * 3]
        rb = pb[(y * w + x0) * 3:(y * w + min(x1, w)) * 3]
        for i in range(0, len(ra), 3):
            total += 1
            diff += ra[i:i + 3] != rb[i:i + 3]
    return diff / total if total else 0.0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--seed', required=True)
    ap.add_argument('--pkg', required=True)
    ap.add_argument('--ndrv', required=True)
    ap.add_argument('--env', action='append', default=[])
    ap.add_argument('--timeout', type=float, default=5400, help='QEMU start -> Finder, s')
    ap.add_argument('--step-timeout', type=float, default=1200)
    ap.add_argument('--quiet-s', type=float, default=5)
    ap.add_argument('rest', nargs=argparse.REMAINDER)
    a = ap.parse_args()
    rest = a.rest[1:] if a.rest[:1] == ['--'] else a.rest
    out = a.out
    out.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(HERE / 'rbd_boottime.py'), 'boot', '--out', str(out), '--seed', a.seed,
           '--pkg', a.pkg, '--ndrv', a.ndrv, '--share', 'auto', '--ctl', '--timeout', str(a.timeout),
           '--settle', str(4 * a.step_timeout), '--end', 'shutdown', '--exit-timeout', str(a.step_timeout),
           '--quiet-s', str(a.quiet_s), '--quiet-max', '300', '--settle-until', str(out / 'settle.end'),
           '--dialog-wait', str(a.step_timeout / 4)]
    for e in a.env:
        cmd += ['--env', e]
    cmd += rest
    res = {'cmd': cmd, 'started': time.strftime('%FT%T%z')}
    t0 = time.monotonic()
    boot = subprocess.Popen(cmd, stdout=open(out / 'boottime.log', 'w'), stderr=subprocess.STDOUT)
    el = lambda: round(time.monotonic() - t0, 2)  # noqa: E731

    def save():
        (out / 'smoke.json').write_text(json.dumps(res, indent=1) + '\n')

    # Finder (rbd_boottime writes finder.png when it sees the menu bar)
    while not (out / 'finder.png').exists() and boot.poll() is None:
        time.sleep(1)
    if boot.poll() is not None and not (out / 'finder.png').exists():
        res['outcome'] = 'no-finder'
        save()
        return 1
    res['finder_seen_s'] = el()
    ctl = StubClient(out / 'stub.ctl', timeout=a.step_timeout)
    try:
        # 1. quiet display
        ctl('stats', reset=True)
        t_q = time.monotonic() + a.step_timeout
        last = time.monotonic()
        while time.monotonic() < t_q:
            time.sleep(1)
            st = ctl('stats', reset=True)['stats']
            if st.get('messages', 0):
                last = time.monotonic()
            elif time.monotonic() - last >= a.quiet_s:
                break
        res['quiet_s'] = el()
        w = ctl('shot', path=str(out / 'before-menu.ppm'))['width']
        ctl('shot', path=str(out / 'before-menu.png'))
        region = (0, 22, min(w, 320), 420)
        # 2. Apple menu: a quick click leaves it open (Mac OS 8+ sticky menus; a long
        #    press followed by a release over the title closes it again)
        ctl('click', x=20, y=10)
        t_c = time.monotonic()
        opened = None
        while time.monotonic() - t_c < a.step_timeout:
            time.sleep(0.5)
            ctl('shot', path=str(out / 'menu.ppm'))
            f = changed(out / 'before-menu.ppm', out / 'menu.ppm', region)
            if f >= 0.02:
                time.sleep(2)
                ctl('shot', path=str(out / 'apple-menu.png'))
                opened = {'after_s': round(time.monotonic() - t_c - 2, 2), 'region_changed': round(f, 4)}
                break
        res['apple_menu'] = opened
        if opened:
            ctl('tap', keys=['esc'], hold=0.3)
            t_e = time.monotonic()
            closed = None
            while time.monotonic() - t_e < a.step_timeout:
                time.sleep(0.5)
                ctl('shot', path=str(out / 'menu-closed.ppm'))
                f = changed(out / 'before-menu.ppm', out / 'menu-closed.ppm', region)
                if f < 0.005:
                    ctl('shot', path=str(out / 'menu-closed.png'))
                    closed = {'after_s': round(time.monotonic() - t_e, 2), 'region_changed': round(f, 4)}
                    break
            res['apple_menu_closed'] = closed
        res['inputs_done_s'] = el()
        save()
    finally:
        # end the settle period: rbd_boottime then runs its power key -> Shut Down -> Return
        try:
            ctl.close()
        except OSError:
            pass
    (out / 'settle.end').write_text('')      # rbd_boottime --settle-until: go on to Shut Down
    rc = None
    try:
        rc = boot.wait(timeout=8 * a.step_timeout + 600)
    except subprocess.TimeoutExpired:
        boot.kill()
    res['boottime_rc'] = rc
    try:
        r = json.loads((out / 'result.json').read_text())
        res['milestones'] = r.get('milestones')
        res['shutdown'] = r.get('shutdown')
        res['shutdown_s'] = r.get('shutdown_s')
        res['power_attempts'] = r.get('power_attempts')
        res['settled'] = r.get('settled')
    except (OSError, ValueError):
        pass
    res['outcome'] = ('pass' if res.get('apple_menu') and res.get('apple_menu_closed')
                      and (res.get('shutdown') or {}).get('reason') == 'guest-shutdown' else 'fail')
    res['total_s'] = el()
    save()
    print(json.dumps({k: res.get(k) for k in ('outcome', 'finder_seen_s', 'apple_menu', 'apple_menu_closed',
                                               'milestones', 'shutdown', 'shutdown_s', 'total_s')}))
    return 0 if res['outcome'] == 'pass' else 1


if __name__ == '__main__':
    sys.exit(main())
