#!/usr/bin/env python3
"""Mac OS 9 restart-crash lab (emulator/reports/restart-crash-20261011.md).

Boots a disk with hostd's argv (hostd/retroboot/machines.py build_argv:
-display retroboot, two QMP sockets, -L variant before the package,
pio-status-drain, -rtc base=localtime, -action reboot=shutdown, ...) behind
a presenter-like display listener (rbd_presenter_stub.Presenter, kept across
relaunches like the real presenter; REFRESH_RATE 16 and optionally the
connect-time RESIZE on every connection).  After Finder it drives Mac OS
Restart (power key, then R) or Shut Down (power key, Return), relaunches QEMU
on the same disk exactly as hostd does after a guest reset, and classifies
every boot as finder / crash (Mac OS system-error alert) / stuck / timeout.

    rbd_restart_lab.py --out RUN --disk DISK [--seed SEED]
        --actions restart,restart,none [--pkg DIR] [--ndrv v2|stock|DIR]
        [--size WxH] [--mem MB] [--speed realtime|accelerated] [--audio none|wav]
        [--resize-connect 0|1] [--share DIR] [--ndrvloader FILE] [--env K=V] [-- QEMU ARGS]

Frames, argv, QEMU output and result.json per boot go to RUN/bootN/; the
disk is copied (sparse) from --seed first, and its state before each relaunch
is kept as bootN/pre.img when that boot does not reach Finder.
rbd_restart_batch.py runs many chains in parallel and summarizes them.
Lab tool (Linux builder, Python standard library only).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rbd_presenter_stub as stub  # noqa: E402

PKG = os.environ.get('RB_LAB_PKG', '/srv/retroboot/wave2/artifacts/qemu/linux-ppc')
NDRV = {'v2': os.environ.get('RB_LAB_NDRV', '/srv/retroboot/wave2/artifacts/ndrv'), 'stock': None}


# --------------------------------------------------------------------------
class QMP:
    def __init__(self, path: str, log=None):
        self.path = path
        self.events: list[dict] = []
        self.cond = threading.Condition()
        self.log = log
        deadline = time.monotonic() + 10
        while True:
            try:
                self.sock = socket.socket(socket.AF_UNIX)
                self.sock.connect(path)
                break
            except OSError:
                self.sock.close()
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.02)
        self.f = self.sock.makefile('rw')
        self.f.readline()
        self.replies: list[dict] = []
        threading.Thread(target=self._reader, daemon=True).start()
        self.cmd('qmp_capabilities')

    def _reader(self):
        while True:
            try:
                line = self.f.readline()
            except (OSError, ValueError):
                line = ''
            if not line:
                with self.cond:
                    self.events.append({'event': '_EOF', 'mono': time.monotonic()})
                    self.cond.notify_all()
                return
            msg = json.loads(line)
            with self.cond:
                if 'event' in msg:
                    msg['mono'] = time.monotonic()
                    self.events.append(msg)
                    if self.log:
                        self.log(msg)
                else:
                    self.replies.append(msg)
                self.cond.notify_all()

    def cmd(self, name, timeout=10, **args):
        with self.cond:
            n = len(self.replies)
        self.f.write(json.dumps({'execute': name, 'arguments': args}) + '\n')
        self.f.flush()
        deadline = time.monotonic() + timeout
        with self.cond:
            while len(self.replies) <= n:
                rem = deadline - time.monotonic()
                if rem <= 0:
                    raise TimeoutError(name)
                self.cond.wait(rem)
            return self.replies[n]

    def hmp(self, line):
        return self.cmd('human-monitor-command', **{'command-line': line})

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------
def build_argv(a, run: Path, disk: Path, boot_index: int) -> list[str]:
    bios = f'{a.pkg}/share/qemu'
    data_dirs = []
    if a.ndrv_dir:
        data_dirs += ['-L', a.ndrv_dir]
    data_dirs += ['-L', bios]
    w, h = a.size
    audio = {'none': 'none,id=snd0', 'wav': f'wav,id=snd0,path={run}/audio-{boot_index}.wav'}[a.audio]
    args = [f'{a.pkg}/bin/qemu-system-ppc', '-accel', 'tcg,tb-size=512', '-M', 'mac99,via=cuda,audiodev=snd0',
            '-cpu', '7400', '-m', str(a.mem), *data_dirs,
            '-vga', 'std', '-global', 'VGA.vgamem_mb=64', '-global', f'VGA.host-resize={a.host_resize}',
            '-global', 'VGA.packed-lowbpp=on', '-global', 'VGA.hardware-cursor=off',
            '-global', 'VGA.untracked-vram=off',
            '-global', 'macio-ide.dma-completion-delay-ns=0']
    if a.drain:
        args += ['-global', 'macio-ide.pio-status-drain=on']
    args += ['-prom-env', 'output-device=ttya', '-g', f'{w}x{h}x32',
             '-name', 'Mac OS 9', '-audiodev', audio, '-action', 'reboot=shutdown', '-rtc', 'base=localtime',
             '-qmp', f'unix:{run}/qmp-control.sock,server=on,wait=off',
             '-qmp', f'unix:{run}/qmp-events.sock,server=on,wait=off',
             '-serial', f'file:{run}/serial-{boot_index}.log',
             '-drive', f'file={disk},format=raw,media=disk,index=0',
             '-display', f'retroboot,socket={run}/display.sock']
    if a.speed == 'accelerated':
        args += ['-icount', 'shift=4,sleep=off', '-global', 'VGA.classicmac-boot-handoff=on']
    args += ['-drive', 'if=ide,index=3,media=cdrom,id=cd0,readonly=on',
             '-drive', 'if=ide,index=2,media=cdrom,id=tools0,readonly=on']
    args += ['-nic', 'user,model=sungem' if a.net else 'none']
    loader = getattr(a, 'ndrvloader', None) or f'{bios}/ndrvloader'   # classicvirtio loader variant
    args += ['-device', f'loader,addr=0x4000000,file={loader}',
             '-prom-env', 'boot-command=init-program go', '-device', 'virtio-tablet-pci']
    if a.share:
        args += ['-device', 'virtio-9p-pci,fsdev=share0,mount_tag=RetroBoot Share',
                 '-fsdev', f'local,id=share0,security_model=none,path={a.share}']
    if a.trace_events:
        # log backend, wall-clock stamped lines (aligned with result.json t0_wall)
        args += ['-trace', f'events={a.trace_events}', '-D', f'{run}/boot{boot_index}/trace.log',
                 '-msg', 'timestamp=on']
    args += a.extra
    return args


def frame_sig(raw: bytes, stride: int, w: int, h: int) -> str:
    # cheap signature over a sparse grid
    m = hashlib.md5()
    for y in range(0, h, 7):
        o = y * stride
        m.update(raw[o:o + w * 4:52])
    return m.hexdigest()


def save_png(p: stub.Presenter, path: Path):
    try:
        w, h, rgb = p.frame()
    except RuntimeError:
        return False
    path.write_bytes(stub.png_bytes(w, h, rgb))
    return True


def dialog_score(raw: bytes, stride: int, w: int, h: int) -> dict:
    """Features of Mac OS's system-error alert: a white box in the centre
    third with black text, on the startup screen.  Returns raw features; the
    classifier thresholds live in classify()."""
    x0, x1, y0, y1 = w // 4, 3 * w // 4, h // 4, 3 * h // 4
    white = black = total = 0
    for y in range(y0, y1, 4):
        o = y * stride
        row = raw[o + x0 * 4:o + x1 * 4]
        for i in range(0, len(row), 16):
            b, g, r = row[i], row[i + 1], row[i + 2]
            total += 1
            if r > 235 and g > 235 and b > 235:
                white += 1
            elif r < 40 and g < 40 and b < 40:
                black += 1
    return {'center_white': round(white / total, 4), 'center_black': round(black / total, 4)}


def alert_rows(raw: bytes, stride: int, w: int, h: int) -> int:
    """Rows in the upper 60 % whose centre 400 px are (almost) all black: the
    system-error alert's double frame gives >= 5 such rows; the startup
    screen's Welcome box gives at most 2.  Every pixel is looked at: Mac OS's
    1-pixel startup dither on a built-in LCD (keys report) made every other
    row look black when only every 4th pixel was sampled."""
    n = 0
    x0 = max(0, w // 2 - 200)
    x1 = min(w, w // 2 + 200)
    for y in range(int(h * 0.6)):
        o = y * stride
        row = raw[o + x0 * 4:o + x1 * 4]
        dark = 0
        cnt = 0
        for i in range(0, len(row), 4):
            cnt += 1
            if row[i] < 60 and row[i + 1] < 60 and row[i + 2] < 60:
                dark += 1
        if cnt and dark >= 0.98 * cnt:
            n += 1
    return n


# --------------------------------------------------------------------------
class Lab:
    def __init__(self, a):
        self.a = a
        self.run = Path(a.out).resolve()
        self.run.mkdir(parents=True, exist_ok=True)
        self.logf = (self.run / 'lab.jsonl').open('a', buffering=1)
        self.p = stub.Presenter(self.run / 'display.sock', self.run / 'display.jsonl')
        threading.Thread(target=self.p.accept_loop, daemon=True).start()
        self.proc: subprocess.Popen | None = None

    def log(self, kind, **kw):
        e = {'t': round(time.time(), 3), 'kind': kind, **kw}
        self.logf.write(json.dumps(e) + '\n')
        if not self.a.quiet:
            print(json.dumps(e), flush=True)

    def kill(self):
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.proc.wait(10)

    def boot(self, idx: int, disk: Path, action: str) -> dict:
        a = self.a
        bdir = self.run / f'boot{idx}'
        bdir.mkdir(exist_ok=True)
        for s in ('qmp-control.sock', 'qmp-events.sock'):
            try:
                (self.run / s).unlink()
            except FileNotFoundError:
                pass
        if a.snapshot and idx > 0:
            subprocess.run(['cp', '--sparse=always', str(disk), str(bdir / 'pre.img')], check=True)
        argv = build_argv(a, self.run, disk, idx)
        (bdir / 'argv.json').write_text(json.dumps(argv, indent=1))
        env = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'RETROBOOT_SESSION': f'lab-{idx}',
               'HOME': '/root', 'LANG': 'C.UTF-8', 'TZ': ':' + a.tz}
        for kv in a.env:
            k, v = kv.split('=', 1)
            env[k] = v
        with self.p.lock:
            conn_before = self.p.conn_id
        load = os.getloadavg()[0]
        with self.p.lock:
            gaps_before = len(self.p.damage_gaps)
        t0_wall = time.time()
        t0 = time.monotonic()
        self.proc = subprocess.Popen(argv, stdout=open(bdir / 'qemu.out', 'w'), stderr=subprocess.STDOUT,
                                     env=env, cwd=str(self.run), start_new_session=True)
        res = {'boot': idx, 'action': action, 'load1': round(load, 2), 'pid': self.proc.pid,
               't0_wall': round(t0_wall, 3)}
        freeze_samples = []
        next_sample = 0.0
        events_q = QMP(str(self.run / 'qmp-events.sock'))
        ctl_q = QMP(str(self.run / 'qmp-control.sock'))
        # presenter-side: on the new connection's HELLO send REFRESH_RATE and (optionally) RESIZE
        hello = self.p.wait_for(lambda: self.p.hello if (self.p.hello and self.p.hello['connection'] > conn_before) else None, 20)
        if hello:
            res['hello_s'] = round(hello['mono'] - t0, 3)
            try:
                self.p.send(stub.REFRESH_RATE, struct.pack('=I', 16))
                if a.resize_connect:
                    self.p.send(stub.RESIZE, struct.pack('=II', *a.resize_size))
                    res['resize_connect_s'] = round(time.monotonic() - t0, 3)
            except (RuntimeError, OSError) as exc:
                res['send_error'] = str(exc)
        # optional delayed resize (variable b)
        resize_at = a.resize_at
        # watch frames
        timeout = a.timeout
        last_sig, static_since = None, time.monotonic()
        gray = finder = None
        next_png = 0.0
        outcome = None
        surfaces = []
        resized_late = False
        while True:
            now = time.monotonic()
            el = now - t0
            if self.proc.poll() is not None:
                outcome = outcome or 'exited'
                break
            if resize_at is not None and not resized_late and gray is not None and now - gray >= resize_at:
                try:
                    self.p.send(stub.RESIZE, struct.pack('=II', *a.resize_size2))
                    res['resize_late_s'] = round(el, 3)
                except (RuntimeError, OSError):
                    pass
                resized_late = True
            try:
                with self.p.lock:
                    cur = self.p.surface
                if not cur or cur['connection'] <= conn_before:
                    raise RuntimeError('no surface for this boot yet')
                w, h, stride, raw = self.p.frame_raw()
            except RuntimeError:
                time.sleep(0.25)
                if el > timeout:
                    outcome = 'other'
                    break
                continue
            if not surfaces or surfaces[-1][1:] != (w, h):
                surfaces.append((round(el, 2), w, h))
            sig = frame_sig(raw, stride, w, h)
            if sig != last_sig:
                last_sig, static_since = sig, now
            an = stub.analyze_frame(w, h, stride, raw)
            if gray is None and an['gray']:
                gray = now
                res['gray_s'] = round(el, 2)
                if a.until == 'gray':
                    outcome = 'gray'
                    break
            if an['finder']:
                finder = now
                res['finder_s'] = round(el, 2)
                outcome = 'finder'
                save_png(self.p, bdir / 'finder.png')
                break
            # Startup stall probe: no display damage at all (even the animated
            # watch cursor draws into the framebuffer) for freeze_probe seconds
            # between the Mac OS gray and Finder.
            ld = self.p.last_damage_mono
            if (gray is not None and ld is not None and now - ld >= a.freeze_probe and
                    now >= next_sample and len(freeze_samples) < a.freeze_samples):
                try:
                    smp = {'t': round(el, 2), 'wall': round(time.time(), 3), 'since_damage': round(now - ld, 2)}
                    for cmd in a.freeze_hmp.split(';'):
                        if cmd.strip():
                            smp[cmd.strip()] = ctl_q.hmp(cmd.strip()).get('return', '')
                    freeze_samples.append(smp)
                    if len(freeze_samples) == 1:
                        save_png(self.p, bdir / 'freeze.png')
                    if a.freeze_kick and len(freeze_samples) == a.freeze_kick_at:
                        # Diagnostic: raise one external interrupt (a virtio-tablet
                        # pointer event) and see whether the stall ends at once.
                        ev = [{'type': 'abs', 'data': {'axis': 'x', 'value': a.kick_xy[0]}},
                              {'type': 'abs', 'data': {'axis': 'y', 'value': a.kick_xy[1]}}]
                        kr = ctl_q.cmd('input-send-event', events=ev)
                        res['kick'] = {'t': round(time.monotonic() - t0, 3), 'wall': round(time.time(), 3),
                                       'reply': 'return' in kr}
                except Exception as exc:  # noqa: BLE001
                    res['freeze_error'] = str(exc)
                next_sample = now + a.freeze_interval
            if el >= next_png and a.frames_every > 0:
                save_png(self.p, bdir / f'f{int(el):04d}.png')
                next_png = el + a.frames_every
            static = now - static_since
            if gray is not None and static >= 2.0 and (now - t0) > 5:
                ar = alert_rows(raw, stride, w, h)
                if ar >= 6:
                    res['alert_rows'] = ar
                    res['crash_s'] = round(static_since - t0, 2)
                    outcome = 'crash'
                    save_png(self.p, bdir / 'crash.png')
                    break
            if gray is not None and static >= a.static_crash:
                ds = dialog_score(raw, stride, w, h)
                res['static_features'] = ds
                res['static_s'] = round(static, 1)
                outcome = 'stuck'
                save_png(self.p, bdir / 'stuck.png')
                break
            if el > timeout:
                outcome = 'timeout'
                save_png(self.p, bdir / 'timeout.png')
                break
            time.sleep(0.25)
        res['outcome'] = outcome
        res['surfaces'] = surfaces
        with self.p.lock:
            gaps = list(self.p.damage_gaps[gaps_before:])
        lo = gray if gray is not None else t0
        hi = finder if finder is not None else time.monotonic()
        bg = [(round(g0 - t0, 2), round(g1 - g0, 2)) for g0, g1 in gaps if g1 > lo and g0 < hi]
        ld = self.p.last_damage_mono
        if outcome != 'finder' and ld is not None and ld > lo:
            bg.append((round(ld - t0, 2), round(hi - ld, 2)))   # still frozen at the end
        res['gaps'] = bg
        res['max_gap_s'] = max([d for _, d in bg], default=0.0)
        res['stall'] = res['max_gap_s'] >= a.stall_gap
        if freeze_samples:
            (bdir / 'freeze-samples.json').write_text(json.dumps(freeze_samples, indent=1))
            res['freeze_samples'] = len(freeze_samples)
        res['elapsed_s'] = round(time.monotonic() - t0, 2)
        if outcome == 'timeout':
            # pre-gray / firmware-phase hang: sample the vCPU a few times
            try:
                samples = []
                for _ in range(5):
                    samples.append(ctl_q.hmp('info registers').get('return', ''))
                    time.sleep(0.3)
                (bdir / 'registers-timeout.txt').write_text('\n=====\n'.join(samples))
            except Exception as exc:  # noqa: BLE001
                res['reg_error'] = str(exc)
        if outcome in ('stuck', 'crash'):
            # dump registers for the mechanism hunt
            try:
                r = ctl_q.hmp('info registers')
                (bdir / 'registers.txt').write_text(r.get('return', ''))
                dump = []
                for line in a.crash_dump.split(';'):
                    if line.strip():
                        dump.append('## ' + line + '\n' + ctl_q.hmp(line.strip()).get('return', ''))
                (bdir / 'crash-dump.txt').write_text('\n'.join(dump))
            except Exception as exc:  # noqa: BLE001
                res['reg_error'] = str(exc)
        shut = None
        if outcome == 'finder' and action == 'beep-shutdown':
            # Alert sound: click outside Mac OS's modal Shut Down dialog, then cancel it.
            time.sleep(a.settle)
            try:
                self.p.tap(['power'], hold=0.15)
                time.sleep(2.0)
                w, h, _ = self.p.frame()
                self.p.click(w // 2, h - 30)
                time.sleep(2.5)
                save_png(self.p, bdir / 'beep-dialog.png')
                self.p.tap(['esc'], hold=0.1)
                time.sleep(1.5)
            except (RuntimeError, OSError) as exc:
                res['beep_error'] = str(exc)
            action = 'shutdown'
        if outcome == 'finder' and action in ('restart', 'shutdown'):
            time.sleep(a.settle)
            save_png(self.p, bdir / 'before-action.png')
            ta = time.monotonic()
            try:
                self.p.tap(['power'], hold=0.15)
                time.sleep(2.0)
                save_png(self.p, bdir / 'power-dialog.png')
                self.p.tap(['r'] if action == 'restart' else ['ret'], hold=0.1)
            except (RuntimeError, OSError) as exc:
                res['action_error'] = str(exc)
            try:
                self.proc.wait(a.exit_timeout)
            except subprocess.TimeoutExpired:
                save_png(self.p, bdir / 'action-timeout.png')
                res['action_timeout'] = True
            with events_q.cond:
                sd = [e for e in events_q.events if e.get('event') == 'SHUTDOWN']
            shut = sd[-1]['data'] if sd else None
            res['shutdown'] = shut
            res['action_s'] = round(time.monotonic() - ta, 2)
        if outcome == 'exited':
            with events_q.cond:
                sd = [e for e in events_q.events if e.get('event') == 'SHUTDOWN']
            res['shutdown'] = sd[-1]['data'] if sd else None
        events_q.close()
        ctl_q.close()
        self.kill()
        res['exit_code'] = self.proc.returncode
        if outcome == 'finder' and a.snapshot and not a.keep_snapshots:
            try:
                (bdir / 'pre.img').unlink()
            except FileNotFoundError:
                pass
        if outcome == 'finder' and not a.keep_frames:
            for f in bdir.glob('f0*.png'):
                f.unlink()
        if a.trace_events and a.trace_keep == 'stall' and outcome == 'finder' and not res['stall']:
            try:
                (bdir / 'trace.log').unlink()
            except FileNotFoundError:
                pass
        (bdir / 'result.json').write_text(json.dumps(res, indent=1))
        self.log('boot', **res)
        return res


def parse_size(s):
    w, h = s.lower().split('x')[:2]
    return int(w), int(h)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True)
    ap.add_argument('--disk', required=True, help='disk image to boot (created from --seed if given)')
    ap.add_argument('--seed', help='copy this image sparsely to --disk first')
    ap.add_argument('--actions', default='restart,none',
                    help='comma list per boot: restart | shutdown | none (classify only)')
    ap.add_argument('--pkg', default=PKG)
    ap.add_argument('--ndrv', default='v2', help='v2 | stock | DIR')
    ap.add_argument('--ndrvloader', default=None, help="classicvirtio loader instead of the package's")
    ap.add_argument('--size', default='1280x800', type=parse_size)
    ap.add_argument('--mem', type=int, default=512)
    ap.add_argument('--speed', choices=['realtime', 'accelerated'], default='realtime')
    ap.add_argument('--audio', choices=['none', 'wav'], default='none')
    ap.add_argument('--host-resize', default='on', choices=['on', 'off'])
    ap.add_argument('--no-drain', dest='drain', action='store_false')
    ap.add_argument('--no-net', dest='net', action='store_false')
    ap.add_argument('--resize-connect', type=int, default=1)
    ap.add_argument('--resize-size', type=parse_size, default=None)
    ap.add_argument('--resize-at', type=float, default=None, help='send a RESIZE this many s after gray')
    ap.add_argument('--resize-size2', type=parse_size, default=None)
    ap.add_argument('--tz', default='UTC')
    ap.add_argument('--share', default=None)
    ap.add_argument('--until', default='finder', choices=['finder', 'gray'], help='gray: stop at the Mac OS gray (pre-gray hang survey)')
    ap.add_argument('--crash-dump', default='xp /64wx 0x0;xp /64wx 0x1000;xp /16wx 0x70c000;xp /16wx 0x727000')
    ap.add_argument('--env', action='append', default=[], help='K=V for the QEMU environment')
    ap.add_argument('--timeout', type=float, default=150)
    ap.add_argument('--static-crash', type=float, default=40.0)
    ap.add_argument('--settle', type=float, default=6.0)
    ap.add_argument('--exit-timeout', type=float, default=60.0)
    ap.add_argument('--frames-every', type=float, default=2.0)
    ap.add_argument('--keep-frames', action='store_true')
    ap.add_argument('--snapshot', type=int, default=1, help='save the disk before each relaunch (kept when it fails)')
    ap.add_argument('--keep-snapshots', action='store_true')
    ap.add_argument('--delete-disk', type=int, default=1, help='delete the chain disk at the end when every boot passed')
    ap.add_argument('--stop-on-fail', type=int, default=1)
    ap.add_argument('--quiet', action='store_true')
    ap.add_argument('--trace-events', default=None, help='QEMU trace event list file (log backend)')
    ap.add_argument('--trace-keep', default='all', choices=['all', 'stall'])
    ap.add_argument('--freeze-probe', type=float, default=3.0,
                    help='sample the guest after this many s without display damage (gray..Finder)')
    ap.add_argument('--freeze-interval', type=float, default=1.0)
    ap.add_argument('--freeze-samples', type=int, default=30)
    ap.add_argument('--freeze-hmp', default='info registers')
    ap.add_argument('--stall-gap', type=float, default=5.0, help='a damage gap this long is a stall')
    ap.add_argument('--freeze-kick', action='store_true',
                    help='diagnostic: send one tablet pointer event during a stall (after --freeze-kick-at samples)')
    ap.add_argument('--freeze-kick-at', type=int, default=2)
    ap.add_argument('--kick-xy', type=lambda v: tuple(int(x) for x in v.split(',')), default=(64, 64))
    ap.add_argument('extra', nargs='*')
    a = ap.parse_args()
    a.ndrv_dir = NDRV.get(a.ndrv, a.ndrv)
    a.resize_size = a.resize_size or a.size
    a.resize_size2 = a.resize_size2 or a.size
    disk = Path(a.disk).resolve()
    if a.share:
        Path(a.share).mkdir(parents=True, exist_ok=True)
    if a.seed:
        subprocess.run(['cp', '--sparse=always', a.seed, str(disk)], check=True)
        os.chmod(disk, 0o644)
    lab = Lab(a)
    lab.log('config', **{k: (v if not isinstance(v, Path) else str(v)) for k, v in vars(a).items()})
    results = []

    def bye(*_):
        lab.kill()
        sys.exit(2)
    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)
    try:
        for i, action in enumerate(a.actions.split(',')):
            r = lab.boot(i, disk, action)
            results.append(r)
            if r['outcome'] != 'finder' and a.stop_on_fail:
                break
            if action in ('restart', 'shutdown') and r.get('shutdown') is None:
                break
    finally:
        lab.kill()
        if a.delete_disk and results and all(r['outcome'] in ('finder', 'gray') for r in results):
            try:
                disk.unlink()
            except FileNotFoundError:
                pass
        (lab.run / 'result.json').write_text(json.dumps({'config': {k: str(v) for k, v in vars(a).items()},
                                                          'boots': results}, indent=1))


if __name__ == '__main__':
    main()
