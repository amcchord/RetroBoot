#!/usr/bin/env python3
"""Qualification runs for ``-display retroboot`` on the RetroBoot builder.

Every run uses a fresh sparse copy of the private OS 9 fixture (never the
fixture itself), the presenter stub on the display socket, and both QMP
sockets.  Evidence (stub log, milestone PNGs, argv, QEMU stderr, result.json)
stays in the run directory; disk copies are deleted unless --keep-disk.

    rbd_qualify.py e2e        --qemu BIN --firmware DIR --root RUNS
    rbd_qualify.py boot       --qemu BIN --firmware DIR --root RUNS --timing accelerated --count 3
    rbd_qualify.py resolution --qemu BIN --firmware DIR --root RUNS
    rbd_qualify.py summarize  --root RUNS      # markdown table of boot runs

Results are JSON on stdout and in RUNS/<name>/result.json.
"""
from __future__ import annotations

import argparse
import array
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
from rbd_lab import (QMP, DEFAULT_FIXTURE, copy_fixture, guest_lowmem,  # noqa: E402
                     reference_argv, start_qemu, start_stub, timebase, wait_path)
from rbd_presenter_stub import StubClient  # noqa: E402


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, file=sys.stderr, flush=True)


class Session:
    """One QEMU + presenter-stub lifetime on a run directory."""

    def __init__(self, args, run: Path, *, timing='realtime', size='1024x768', audio='none',
                 disk: Path | None = None, fresh_dir=True, **argv_options):
        self.run = run
        if fresh_dir:
            run.mkdir(parents=True, exist_ok=False)
        self.disk = disk or run / 'disk.img'
        if disk is None:
            copy_fixture(args.fixture, self.disk)
        for name in ['qmp-control.sock', 'qmp-events.sock', 'hmp.sock', 'display.sock']:
            if (run / name).exists():
                (run / name).unlink()
        self.stub_proc = start_stub(run)
        self.stub = StubClient(run / 'stub.ctl')
        self.argv = reference_argv(args.qemu, args.firmware, run, self.disk, timing=timing,
                                   size=size, audio=audio, name=run.name,
                                   ide_pio_drain=args.ide_pio_drain == 'on',
                                   ram_mb=args.ram, rtc=args.rtc,
                                   ide_dma_delay_ns=args.ide_dma_delay_ns,
                                   share=args.share_dir, **argv_options)
        self.t0 = time.monotonic()
        self.qemu = start_qemu(self.argv, run, {'RETROBOOT_SESSION': run.name})
        wait_path(run / 'qmp-events.sock', 10)
        wait_path(run / 'qmp-control.sock', 10)
        time.sleep(0.05)
        self.qmp = QMP(run / 'qmp-control.sock')
        self.events = QMP(run / 'qmp-events.sock')

    def rel(self, mono: float | None) -> float | None:
        return None if mono is None else round(mono - self.t0, 3)

    def shot(self, name: str) -> dict:
        return self.stub('shot', path=str(self.run / name))

    def restart_stub(self, down_s: float = 1.0) -> dict:
        os.kill(self.stub_proc.pid, signal.SIGTERM)
        self.stub_proc.wait(5)
        self.stub.close()
        time.sleep(down_s)
        listen = time.monotonic()
        self.stub_proc = start_stub(self.run, tag='-restart')
        self.stub = StubClient(self.run / 'stub.ctl')
        hello = self.stub('wait_connected', timeout=10)['hello']
        surface = self.stub('wait_surface', timeout=10)['surface']
        return {'stub_spawn_to_hello_s': round(hello['mono'] - listen, 3),
                'hello_to_surface_ms': round((surface['mono'] - hello['mono']) * 1000, 2),
                'surface': [surface['width'], surface['height']]}

    def clean_shutdown(self, timeout=60) -> dict:
        start = time.monotonic()
        self.stub('tap', keys=['power'], hold=0.1)
        time.sleep(1.0)
        self.shot('power-dialog.png')
        self.stub('tap', keys=['ret'], hold=0.08)
        ev = self.events.wait_event('SHUTDOWN', timeout)
        try:
            self.qemu.wait(15)
        except subprocess.TimeoutExpired:
            pass
        return {'shutdown_event': ev and ev.get('data'), 'seconds': round(time.monotonic() - start, 2),
                'qemu_exit': self.qemu.poll()}

    def close(self, keep_disk=False):
        if self.qemu.poll() is None:
            try:
                self.qmp.execute('quit')
                self.qemu.wait(10)
            except Exception:
                self.qemu.kill()
                self.qemu.wait(5)
        for q in (self.qmp, self.events):
            q.close()
        try:
            self.stub.close()
        except OSError:
            pass
        if self.stub_proc.poll() is None:
            os.kill(self.stub_proc.pid, signal.SIGTERM)
            try:
                self.stub_proc.wait(5)
            except subprocess.TimeoutExpired:
                self.stub_proc.kill()
        if not keep_disk and self.disk.exists() and self.disk.parent == self.run:
            self.disk.unlink()


def wait_finder(s: Session, timeout: float, fallback_deadline: float | None = None,
                frames_every: float = 0) -> dict:
    """Wait for the Finder milestone; optionally fire the HMP handoff fallback.

    frames_every > 0 saves a PNG of the shared buffer at that interval into
    <run>/frames/ (boot-stall diagnosis).
    """
    out = {'fallback_sent_s': None, 'fallback_reply': None}
    deadline = s.t0 + timeout
    next_frame = s.t0
    if frames_every:
        (s.run / 'frames').mkdir(exist_ok=True)
    while time.monotonic() < deadline:
        if frames_every and time.monotonic() >= next_frame:
            next_frame += frames_every
            try:
                s.stub('shot', path=str(s.run / 'frames' / f't{time.monotonic() - s.t0:05.1f}.png'))
            except RuntimeError:
                pass
        m = s.stub('milestones')['milestones']
        if 'finder' in m:
            break
        handed = any(e.get('event') == 'boot-handoff' for e in s.stub('events')['events'])
        if (fallback_deadline is not None and not handed and out['fallback_sent_s'] is None
                and time.monotonic() - s.t0 >= fallback_deadline):
            out['fallback_sent_s'] = round(time.monotonic() - s.t0, 3)
            out['fallback_reply'] = s.qmp.hmp('classicmac-boot-complete').strip()
            log('fallback handoff requested:', out['fallback_reply'])
        if s.qemu.poll() is not None:
            raise RuntimeError(f'QEMU exited with {s.qemu.returncode}')
        time.sleep(0.2)
    m = s.stub('milestones')['milestones']
    out['milestones'] = {k: {'t': s.rel(v['mono']), 'size': [v['width'], v['height']]} for k, v in m.items()}
    if 'finder' not in m:
        raise TimeoutError('Finder not reached')
    return out


def clock_sample(s: Session) -> dict:
    out = guest_lowmem(s.qmp)
    out['tb'] = timebase(s.qmp)
    out['host_mono_after'] = time.monotonic()
    out['offset_s'] = round(out['guest_unix'] - out['host_unix'], 2)
    return out


def clock_compare(a: dict, b: dict) -> dict:
    dt = b['host_mono'] - a['host_mono']
    return {'interval_s': round(dt, 2),
            'guest_time_rate': round((b['time'] - a['time']) / dt, 4),
            'ticks_per_s': round((b['ticks'] - a['ticks']) / dt, 3),
            'timebase_mhz': round((b['tb'] - a['tb']) / dt / 1e6, 4),
            'offset_start_s': a['offset_s'], 'offset_end_s': b['offset_s'],
            'drift_s': round(b['offset_s'] - a['offset_s'], 2)}


def pidstat(pid: int, seconds: int) -> dict:
    """Average %usr/%system/%CPU of one process over `seconds` 1 s samples."""
    out = subprocess.run(['pidstat', '-h', '-u', '-p', str(pid), '1', str(seconds)],
                         capture_output=True, text=True, timeout=seconds + 10).stdout
    return parse_pidstat(out)


def pidstat_threads(pid: int, seconds: int, proc: subprocess.Popen | None = None) -> dict:
    """Per-thread average %CPU (QEMU runs with -name ...,debug-threads=on)."""
    if proc is None:
        proc = subprocess.Popen(['pidstat', '-h', '-t', '-u', '-p', str(pid), '1', str(seconds)],
                                stdout=subprocess.PIPE, text=True)
    out, _ = proc.communicate(timeout=seconds + 15)
    header, acc = None, {}
    for line in out.splitlines():
        if line.startswith('#'):
            header = line[1:].split()
            continue
        if not header or not line.strip() or not line.strip()[0].isdigit():
            continue
        row = dict(zip(header, line.split(None, len(header) - 1)))
        if row.get('TGID') != '-':
            key = 'process'
        else:
            key = row['Command'].replace('|__', '').strip()
            if row['TID'] == str(pid):
                key = 'main-loop'
        acc.setdefault(key, []).append(float(row['%CPU']))
    return {k: round(sum(v) / len(v), 1) for k, v in sorted(acc.items()) if sum(v) > 0 or k == 'process'}


def pidstat_timeline(out: str, pid: int, t0_unix: float) -> list:
    """[t, vcpu%, main%] per 1 s sample from `pidstat -h -t` output.

    pidstat prints wall-clock HH:MM:SS (or epoch seconds, depending on
    S_TIME_FORMAT); t is seconds since QEMU was launched either way.
    """
    import datetime
    header, rows = None, {}
    for line in out.splitlines():
        if line.startswith('#'):
            header = line[1:].split()
            continue
        if not header or not line.strip() or not line.strip()[0].isdigit():
            continue
        row = dict(zip(header, line.split(None, len(header) - 1)))
        if row.get('TGID') != '-':
            continue
        stamp = row['Time']
        try:
            if ':' in stamp:
                day = datetime.datetime.fromtimestamp(t0_unix)
                hh, mm, ss = map(int, stamp.split(':'))
                when = day.replace(hour=hh, minute=mm, second=ss, microsecond=0).timestamp()
                if when < t0_unix - 43200:
                    when += 86400
            else:
                when = float(stamp)
        except ValueError:
            continue
        t = round(when - t0_unix)
        slot = rows.setdefault(t, [t, 0.0, 0.0])
        if row['TID'] == str(pid):
            slot[2] = float(row['%CPU'])
        elif 'TCG' in row['Command']:
            slot[1] = float(row['%CPU'])
    return [rows[k] for k in sorted(rows)]


def start_pidstat_threads(pid: int, seconds: int) -> subprocess.Popen:
    return subprocess.Popen(['stdbuf', '-oL', 'pidstat', '-h', '-t', '-u', '-p', str(pid), '1', str(seconds)],
                            stdout=subprocess.PIPE, text=True)


def loadavg() -> str:
    return Path('/proc/loadavg').read_text().split(' ', 3)[:3].__str__()


def changed_fraction(a: Path, b: Path, region: tuple[int, int, int, int] | None = None) -> float:
    """Fraction of pixels that differ between two PPM frames of equal size."""
    def load(p):
        head, dims, mx, px = p.read_bytes().split(b'\n', 3)
        w, h = map(int, dims.split())
        return w, h, px
    w, h, pa = load(a)
    w2, h2, pb = load(b)
    if (w, h) != (w2, h2):
        return 1.0
    x0, y0, x1, y1 = region or (0, 0, w, h)
    diff = total = 0
    for y in range(y0, min(y1, h)):
        ra = pa[(y * w + x0) * 3:(y * w + min(x1, w)) * 3]
        rb = pb[(y * w + x0) * 3:(y * w + min(x1, w)) * 3]
        for i in range(0, len(ra), 3):
            total += 1
            if ra[i:i + 3] != rb[i:i + 3]:
                diff += 1
    return round(diff / total, 4) if total else 0.0


def screendump_match(s: Session, label: str, pause: bool) -> dict:
    if pause:
        s.qmp.execute('stop')
        time.sleep(0.1)
    try:
        qpath = s.run / f'screendump-{label}.ppm'
        spath = s.run / f'stub-{label}.ppm'
        s.qmp.execute('screendump', filename=str(qpath), format='ppm')
        time.sleep(0.05)
        s.stub('shot', path=str(spath))
    finally:
        if pause:
            s.qmp.execute('cont')
    a, b = qpath.read_bytes(), spath.read_bytes()
    return {'identical': a == b, 'bytes': len(a),
            'differing_fraction': 0.0 if a == b else changed_fraction(qpath, spath)}


def wav_summary(path: Path, start_byte: int = 44) -> dict:
    if not path.exists():
        return {'present': False}
    data = path.read_bytes()[start_byte:]
    samples = array.array('h')
    samples.frombytes(data[:len(data) - len(data) % 2])
    nz = [i for i, v in enumerate(samples) if abs(v) > 64]
    frames = len(samples) // 2
    return {'present': True, 'frames': frames, 'seconds_at_44k1': round(frames / 44100, 3),
            'nonzero_span_frames': (nz[-1] - nz[0]) // 2 + 1 if nz else 0,
            'peak': max((abs(v) for v in samples), default=0)}


# -- scenarios -------------------------------------------------------------

def scenario_boot(args) -> dict:
    results = []
    for i in range(args.count):
        run = args.root / f'boot-{args.timing}-{args.tag}{i + 1}'
        log('boot run', run.name, 'load', loadavg())
        s = Session(args, run, timing=args.timing, size=args.size, audio=args.audio)
        r = {'run': run.name, 'timing': args.timing, 'ide_pio_drain': args.ide_pio_drain,
             'firmware': str(args.firmware), 'qemu': str(args.qemu), 'load_before': loadavg()}
        t0_unix = time.time() - (time.monotonic() - s.t0)
        cpu = start_pidstat_threads(s.qemu.pid, int(args.timeout))
        try:
            fb = args.fallback_deadline if args.timing == 'accelerated' else None
            try:
                r.update(wait_finder(s, args.timeout, fb, args.frames_every))
            finally:
                cpu.send_signal(signal.SIGINT)
                try:
                    r['boot_cpu_timeline'] = pidstat_timeline(cpu.communicate(timeout=10)[0],
                                                              s.qemu.pid, t0_unix)
                except Exception as exc:  # diagnostics only; never fail a boot run
                    r['boot_cpu_timeline_error'] = f'{type(exc).__name__}: {exc}'
            status = s.stub('status')
            r['hello_s'] = s.rel(status['hello']['mono'])
            events = s.stub('events')['events']
            r['guest_events'] = [{**{k: v for k, v in e.items() if k not in ('mono', 'connection')},
                                  't': s.rel(e['mono'])} for e in events]
            surfaces = s.stub('surfaces')['surfaces']
            r['surfaces'] = [{'t': s.rel(x['mono']), 'size': [x['width'], x['height']]} for x in surfaces]
            time.sleep(2)
            if args.timing == 'accelerated':
                handed = s.stub('wait_event', event='boot-handoff', timeout=30)['event']
                r['handoff_s'] = s.rel(handed['mono'])
                r['handoff_uptime_ms'] = handed.get('uptime_ms')
            r['hmp_clock'] = s.qmp.hmp('classicmac-boot-complete').strip().splitlines()
            a = clock_sample(s)
            r['clock_at_finder'] = {k: a[k] for k in ('offset_s', 'time', 'ticks', 'tb')}
            log(f"finder {r['milestones']['finder']['t']} s; offset {a['offset_s']} s; waiting {args.clock_wait} s")
            if args.audio == 'wav':
                r['audio'] = audio_probe(s)
            time.sleep(max(0, args.clock_wait - (time.monotonic() - a['host_mono'])))
            b = clock_sample(s)
            r['clock'] = clock_compare(a, b)
            s.shot('after-clock-wait.png')
            r['shutdown'] = s.clean_shutdown()
        except Exception as exc:
            r['error'] = f'{type(exc).__name__}: {exc}'
            log('run failed:', r['error'])
        finally:
            s.close(keep_disk=args.keep_disk)
            r['qemu_stderr_warnings'] = [l for l in (run / 'qemu.out').read_text().splitlines()
                                         if 'ALSA' not in l and 'info:' not in l][:20]
        (run / 'result.json').write_text(json.dumps(r, indent=2) + '\n')
        results.append(r)
        log(json.dumps({k: r.get(k) for k in ('run', 'milestones', 'handoff_s', 'clock', 'error')}))
    summary = summarize_boot(results)
    (args.root / f'boot-{args.timing}-{args.tag}summary.json').write_text(
        json.dumps({'summary': summary, 'runs': results}, indent=2) + '\n')
    return {'summary': summary, 'runs': results}


def summarize_boot(results: list[dict]) -> dict:
    def col(path):
        vals = []
        for r in results:
            v = r
            for key in path:
                v = v.get(key) if isinstance(v, dict) else None
            if isinstance(v, (int, float)):
                vals.append(v)
        return {'min': min(vals), 'median': sorted(vals)[len(vals) // 2], 'max': max(vals),
                'n': len(vals)} if vals else None
    return {'gray_s': col(['milestones', 'gray', 't']), 'finder_s': col(['milestones', 'finder', 't']),
            'nonblack_s': col(['milestones', 'nonblack', 't']), 'handoff_s': col(['handoff_s']),
            'offset_at_finder_s': col(['clock_at_finder', 'offset_s']),
            'drift_s': col(['clock', 'drift_s']), 'ticks_per_s': col(['clock', 'ticks_per_s']),
            'timebase_mhz': col(['clock', 'timebase_mhz']),
            'errors': [r['error'] for r in results if 'error' in r],
            'fallbacks': sum(1 for r in results if r.get('fallback_sent_s') is not None),
            'shutdown_reasons': [((r.get('shutdown') or {}).get('shutdown_event') or {}).get('reason')
                                 for r in results]}


def audio_probe(s: Session) -> dict:
    """Trigger Mac OS's alert sound (click outside the modal Shut Down dialog)."""
    wav = s.run / 'guest-audio.wav'
    before = wav.stat().st_size if wav.exists() else 0
    s.stub('tap', keys=['power'], hold=0.1)
    time.sleep(1.2)
    w, h = s.stub('status')['surface']['width'], s.stub('status')['surface']['height']
    t_click = time.monotonic()
    s.stub('click', x=20, y=h - 60)
    time.sleep(2.5)
    s.stub('tap', keys=['esc'])
    time.sleep(1.0)
    after = wav.stat().st_size if wav.exists() else 0
    out = wav_summary(wav, start_byte=max(44, before))
    out.update({'bytes_before': before, 'bytes_after': after,
                'growth_s_at_44k1': round((after - before) / 4 / 44100, 3),
                'wall_s': round(time.monotonic() - t_click, 3)})
    return out


def scenario_e2e(args) -> dict:
    run = args.root / f'e2e-{args.timing}-{args.tag}'.rstrip('-')
    s = Session(args, run, timing=args.timing, size=args.size)
    r: dict = {'run': run.name, 'timing': args.timing, 'load_before': loadavg()}
    try:
        r['boot'] = wait_finder(s, args.timeout, args.fallback_deadline if args.timing == 'accelerated' else None)
        st = s.stub('status')
        r['hello'] = {k: st['hello'][k] for k in ('version', 'session')} | {'pid_matches': st['hello']['qemu_pid'] == s.qemu.pid}
        r['surface_at_finder'] = st['surface'] and {k: st['surface'][k] for k in ('width', 'height', 'stride', 'drm_format', 'size', 'file_size', 'seals')}
        time.sleep(4)
        w, h = st['surface']['width'], st['surface']['height']
        st = s.stub('status')
        w, h = st['surface']['width'], st['surface']['height']
        r['finder_size'] = [w, h]

        # 1. frames byte-match QMP screendump
        r['screendump_running'] = screendump_match(s, 'running', pause=False)
        r['screendump_paused'] = screendump_match(s, 'paused', pause=True)

        # 2. idle desktop: damage throughput and CPU, at the default 16 ms
        #    refresh and at 1000 ms (attributes the display refresh cost)
        s.stub('stats', reset=True)
        r['idle_cpu_threads_16ms'] = pidstat_threads(s.qemu.pid, 10)
        r['idle_damage'] = s.stub('stats', reset=True)['stats']
        s.stub('refresh_rate', ms=1000)
        time.sleep(1)
        r['idle_cpu_threads_1000ms'] = pidstat_threads(s.qemu.pid, 10)
        s.stub('refresh_rate', ms=16)
        time.sleep(1)

        # 3. pointer: double-click Macintosh HD (top-right desktop icon)
        s.stub('shot', path=str(run / 'before-hd.ppm'))
        hd = (w - 50, 52)
        s.stub('click', x=hd[0], y=hd[1], double=True)
        time.sleep(2.5)
        s.stub('shot', path=str(run / 'after-hd.ppm'))
        s.shot('hd-window.png')
        r['hd_double_click'] = {'at': hd, 'window_region_changed': changed_fraction(
            run / 'before-hd.ppm', run / 'after-hd.ppm', (0, 22, w - 120, min(h, 300)))}

        # 4. keyboard: Finder Cmd-N, then name the folder
        s.stub('tap', keys=['meta_l', 'n'])
        time.sleep(1.5)
        s.stub('type', text='RetroBoot Display Test')
        s.stub('tap', keys=['ret'])
        time.sleep(1.5)
        s.stub('shot', path=str(run / 'after-folder.ppm'))
        s.shot('new-folder.png')
        r['new_folder'] = {'window_region_changed': changed_fraction(
            run / 'after-hd.ppm', run / 'after-folder.ppm', (0, 22, w - 120, min(h, 300)))}

        # 5. window drag: damage throughput and CPU while dragging the HD window
        s.stub('stats', reset=True)
        cpu = start_pidstat_threads(s.qemu.pid, 10)
        t_end = time.monotonic() + 10
        drags = 0
        while time.monotonic() < t_end - 1.5:
            x0, y0, x1, y1 = (200, 32, 320, 160) if drags % 2 == 0 else (320, 160, 200, 32)
            s.stub('drag', x0=x0, y0=y0, x1=x1, y1=y1, steps=30, step_delay=0.02, hold_after=0.1)
            drags += 1
            time.sleep(0.2)
        r['drag_cpu_threads'] = pidstat_threads(s.qemu.pid, 10, cpu)
        r['drag_damage'] = s.stub('stats', reset=True)['stats'] | {'drags': drags}
        s.shot('after-drag.png')

        # 5b. backpressure: stop reading for 6 s while the guest draws at a 4 ms
        #     refresh; QEMU must drop DAMAGE (seq gaps), stay connected, then
        #     repaint with one full-surface DAMAGE; frames must match again.
        s.stub('refresh_rate', ms=4)
        s.stub('stats', reset=True)
        s.stub('pause_reading', seconds=6)
        t_end = time.monotonic() + 6
        drags = 0
        while time.monotonic() < t_end:
            x0, y0, x1, y1 = (200, 32, 320, 160) if drags % 2 == 0 else (320, 160, 200, 32)
            s.stub('drag', x0=x0, y0=y0, x1=x1, y1=y1, steps=20, step_delay=0.01, hold_after=0.05)
            drags += 1
        time.sleep(1.5)
        s.stub('refresh_rate', ms=16)
        st = s.stub('status')
        r['backpressure'] = {'stats': s.stub('stats', reset=True)['stats'], 'drags': drags,
                             'still_connected': st['connected'] and st['connection'] == 1,
                             'screendump': screendump_match(s, 'after-backpressure', pause=True)}

        # 6. RESIZE 800x600 -> Mac OS switches mode, new SURFACE
        before_index = s.stub('status')['surface']['index']
        t_req = s.stub('resize', width=800, height=600)['mono']
        surf = s.stub('wait_surface', width=800, height=600, after_index=before_index, timeout=20)['surface']
        time.sleep(2)
        s.shot('resized-800x600.png')
        ev = [e for e in s.stub('events')['events'] if e.get('event') == 'resize-request']
        r['resize_800x600'] = {'surface_after_ms': round((surf['mono'] - t_req) * 1000, 1),
                               'event': ev[-1] if ev else None,
                               'screendump': screendump_match(s, 'resized', pause=True)}

        # 7. presenter restart with a key held: QEMU must release it
        s.stub('key', name='shift', down=True)
        time.sleep(0.2)
        r['presenter_restart'] = s.restart_stub(1.0)
        time.sleep(1.0)
        r['presenter_restart']['screendump'] = screendump_match(s, 'reconnected', pause=True)
        s.stub('tap', keys=['meta_l', 'n'])
        time.sleep(1.5)
        s.stub('type', text='lower case after restart')
        s.stub('tap', keys=['ret'])
        time.sleep(1.5)
        s.shot('after-restart-folder.png')

        # 8. RELEASE_ALL: hold Command, release via RELEASE_ALL, type plain text
        s.stub('key', name='meta_l', down=True)
        time.sleep(0.2)
        s.stub('release_all')
        time.sleep(0.2)
        s.stub('tap', keys=['meta_l', 'n'])
        time.sleep(1.5)
        s.stub('type', text='after release all')
        s.stub('tap', keys=['ret'])
        time.sleep(1.5)
        s.shot('after-release-all.png')

        # 9. clean Shut Down through the display socket's power key
        r['shutdown'] = s.clean_shutdown()
        r['qemu_log'] = [l.split('qemu-system-ppc: ', 1)[-1] for l in (run / 'qemu.out').read_text().splitlines()
                         if 'ALSA' not in l]
    except Exception as exc:
        r['error'] = f'{type(exc).__name__}: {exc}'
        log('e2e failed:', r['error'])
    finally:
        s.close(keep_disk=args.keep_disk)
    (run / 'result.json').write_text(json.dumps(r, indent=2) + '\n')
    return r


def parse_pidstat(out: str) -> dict:
    header = next((l.lstrip('#').split() for l in out.splitlines() if l.startswith('#')), [])
    rows = [l.split() for l in out.splitlines() if l.strip() and not l.startswith('#')
            and re.match(r'^\d', l.strip())]
    if not rows or '%CPU' not in header:
        return {'raw': out}
    idx = {name: header.index(name) for name in ['%usr', '%system', '%CPU']}
    return {k: round(sum(float(r[i]) for r in rows) / len(rows), 1) for k, i in idx.items()} | {'samples': len(rows)}


def scenario_resolution(args) -> dict:
    """Late mode switch, re-request after Finder, and persistence across boots.

    One disk copy per size; each boot gets its own evidence directory.
    Boot 1 sends an early RESIZE (before the guest's video driver loads) and,
    if the guest is not at the size 3 s after Finder, a re-request.  Later
    boots use the same disk and -g size with no RESIZE at all.
    """
    results = {}
    for size in args.sizes:
        w, h = map(int, size.split('x'))
        root = args.root / f'res-{size}-{args.tag}'.rstrip('-')
        root.mkdir(parents=True, exist_ok=False)
        disk = root / 'disk.img'
        copy_fixture(args.fixture, disk)
        r: dict = {'size': size}
        for n in range(1, 2 + args.persist_boots):
            boot = f'boot{n}'
            s = Session(args, root / boot, timing=args.timing, size=size, disk=disk)
            b: dict = {}
            try:
                s.stub('wait_connected', timeout=10)
                if n == 1:
                    b['early_resize'] = s.stub('resize', width=w, height=h)['mono'] - s.t0
                b.update(wait_finder(s, args.timeout,
                                     args.fallback_deadline if args.timing == 'accelerated' else None))
                time.sleep(3)
                cur = s.stub('status')['surface']
                b['size_at_finder_plus_3s'] = [cur['width'], cur['height']]
                if n == 1 and (cur['width'], cur['height']) != (w, h):
                    t = s.stub('resize', width=w, height=h)['mono']
                    surf = s.stub('wait_surface', width=w, height=h, after_index=cur['index'],
                                  timeout=20)['surface']
                    b['re_request_ms'] = round((surf['mono'] - t) * 1000, 1)
                b['resize_events'] = [{k: e[k] for k in ('width', 'height', 'method')}
                                      for e in s.stub('events')['events'] if e.get('event') == 'resize-request']
                time.sleep(3)
                s.shot('final.png')
                b['surfaces'] = [{'t': s.rel(x['mono']), 'size': [x['width'], x['height']]}
                                 for x in s.stub('surfaces')['surfaces']]
                cur = s.stub('status')['surface']
                b['final_size'] = [cur['width'], cur['height']]
                b['shutdown'] = s.clean_shutdown()
            except Exception as exc:
                b['error'] = f'{type(exc).__name__}: {exc}'
                log(size, boot, 'failed:', b['error'])
            finally:
                s.close(keep_disk=True)
            r[boot] = b
            log(size, boot, json.dumps({k: b.get(k) for k in ('surfaces', 'final_size', 'error')}))
        if not args.keep_disk and disk.exists():
            disk.unlink()
        (root / 'result.json').write_text(json.dumps(r, indent=2) + '\n')
        results[size] = r
    return results


def start_resize_schedule(s: Session, schedule: str | None) -> dict | None:
    """Send RESIZE at fixed times after launch: "800x600@15,1024x768@18".

    Uses its own control connection so it runs alongside the boot watcher.
    """
    if not schedule:
        return None
    import threading
    steps = []
    for item in schedule.split(','):
        size, at = item.split('@')
        w, h = map(int, size.split('x'))
        steps.append((float(at), w, h))
    state = {'stop': False, 'sent': []}

    def worker():
        client = StubClient(s.run / 'stub.ctl')
        try:
            for at, w, h in sorted(steps):
                while time.monotonic() - s.t0 < at:
                    if state['stop']:
                        return
                    time.sleep(0.05)
                try:
                    client('resize', width=w, height=h)
                    state['sent'].append([round(time.monotonic() - s.t0, 2), w, h])
                except Exception as exc:
                    state['sent'].append([round(time.monotonic() - s.t0, 2), w, h, str(exc)])
        finally:
            client.close()
    threading.Thread(target=worker, daemon=True).start()
    return state


def scenario_reliability(args) -> dict:
    """Repeated boots to Finder with frame capture, for startup crash/stall rates.

    Configurations rotate every round (timing x RAM) so builder load affects
    them alike.  A boot passes when the Finder milestone appears before
    --timeout; otherwise the frames, serial log and QEMU log are kept and the
    VM is stopped.  Passing boots end with Mac OS's own Shut Down.
    """
    configs = [(t, ram) for t in args.timings for ram in args.rams]
    results = []
    for rnd in range(1, args.count + 1):
        for timing, ram in configs:
            args.ram = ram
            name = f'rel-{timing}-{ram}-{args.tag}r{rnd:02d}'
            run = args.root / name
            s = Session(args, run, timing=timing, size=args.size, audio=args.audio)
            r = {'run': name, 'timing': timing, 'ram': ram, 'drain': args.ide_pio_drain,
                 'audio': args.audio, 'rtc': args.rtc, 'ide_dma_delay_ns': args.ide_dma_delay_ns,
                 'share': bool(args.share_dir), 'load_before': loadavg()}
            t0_unix = time.time() - (time.monotonic() - s.t0)
            cpu = start_pidstat_threads(s.qemu.pid, int(args.timeout))
            resizer = start_resize_schedule(s, args.resize_schedule)
            r['resize_schedule'] = args.resize_schedule
            try:
                fb = args.fallback_deadline if timing == 'accelerated' else None
                try:
                    r.update(wait_finder(s, args.timeout, fb, args.frames_every))
                    r['outcome'] = 'finder'
                finally:
                    cpu.send_signal(signal.SIGINT)
                    try:
                        r['boot_cpu_timeline'] = pidstat_timeline(cpu.communicate(timeout=10)[0],
                                                                  s.qemu.pid, t0_unix)
                    except Exception as exc:
                        r['boot_cpu_timeline_error'] = str(exc)
                    r['guest_events'] = [{'event': e.get('event'), 't': s.rel(e['mono'])}
                                         for e in s.stub('events')['events']]
                    r['surfaces'] = [{'t': s.rel(x['mono']), 'size': [x['width'], x['height']]}
                                     for x in s.stub('surfaces')['surfaces']]
                time.sleep(2)
                r['shutdown'] = s.clean_shutdown()
            except TimeoutError as exc:
                r['outcome'] = 'no-finder'
                r['error'] = str(exc)
                m = s.stub('milestones')['milestones']
                r['milestones'] = {k: {'t': s.rel(v['mono'])} for k, v in m.items()}
                s.shot('final.png')
                r['qemu_running'] = s.qemu.poll() is None
            except Exception as exc:
                r['outcome'] = 'error'
                r['error'] = f'{type(exc).__name__}: {exc}'
            finally:
                if resizer:
                    resizer['stop'] = True
                    r['resizes_sent'] = resizer['sent']
                s.close(keep_disk=args.keep_disk)
            if r.get('outcome') == 'finder' and not args.keep_frames:
                # keep the extension-parade frames only for failures and slow boots
                fin = r['milestones']['finder']['t']
                if fin < args.slow_s and (run / 'frames').exists():
                    shutil.rmtree(run / 'frames')
            (run / 'result.json').write_text(json.dumps(r, indent=2) + '\n')
            results.append(r)
            log(name, r.get('outcome'), 'finder', (r.get('milestones') or {}).get('finder', {}).get('t'),
                'handoff', next((e['t'] for e in r.get('guest_events', []) if e['event'] == 'boot-handoff'), None),
                r.get('error', ''))
    summary = {}
    for r in results:
        key = f"{r['timing']}-{r['ram']}"
        g = summary.setdefault(key, {'boots': 0, 'finder': 0, 'failed': [], 'finder_s': []})
        g['boots'] += 1
        if r.get('outcome') == 'finder':
            g['finder'] += 1
            g['finder_s'].append(r['milestones']['finder']['t'])
        else:
            g['failed'].append(r['run'])
    (args.root / f'reliability-{args.tag}summary.json').write_text(
        json.dumps({'summary': summary, 'runs': results}, indent=2) + '\n')
    return summary


def scenario_summarize(args) -> dict:
    """Group boot results by configuration (run name minus round/index suffix)."""
    groups: dict[str, list[dict]] = {}
    for f in sorted(args.root.glob('boot-*/result.json')):
        r = json.loads(f.read_text())
        key = re.sub(r'-r\d+-\d+$', '', r['run'])
        groups.setdefault(key, []).append(r)
    rows = {}
    for key, runs in groups.items():
        summary = summarize_boot(runs)
        summary['audio_nonzero_s'] = [round(r['audio']['nonzero_span_frames'] / 44100, 3)
                                      for r in runs if r.get('audio', {}).get('present')]
        summary['load_before'] = [r.get('load_before') for r in runs]
        rows[key] = summary
    lines = ['| configuration | n | gray s (min/med/max) | Finder s (min/med/max) | handoff s | '
             'clock offset s | drift s | Ticks/s | TB MHz | shutdown |', '|' + ' --- |' * 10]

    def fmt(c):
        return '-' if not c else f"{c['min']:.1f} / {c['median']:.1f} / {c['max']:.1f}"
    for key, sm in rows.items():
        reasons = sorted(set(str(x) for x in sm['shutdown_reasons']))
        lines.append(f"| {key} | {sm['finder_s']['n'] if sm['finder_s'] else 0} | {fmt(sm['gray_s'])} | "
                     f"{fmt(sm['finder_s'])} | {fmt(sm['handoff_s'])} | {fmt(sm['offset_at_finder_s'])} | "
                     f"{fmt(sm['drift_s'])} | {fmt(sm['ticks_per_s'])} | {fmt(sm['timebase_mhz'])} | "
                     f"{', '.join(reasons)} |")
    table = '\n'.join(lines)
    (args.root / 'summary.md').write_text(table + '\n')
    (args.root / 'summary.json').write_text(json.dumps(rows, indent=2) + '\n')
    print(table, file=sys.stderr)
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('scenario', choices=['e2e', 'boot', 'resolution', 'reliability', 'summarize'])
    ap.add_argument('--qemu', type=Path)
    ap.add_argument('--firmware', type=Path)
    ap.add_argument('--root', type=Path, required=True)
    ap.add_argument('--fixture', type=Path, default=DEFAULT_FIXTURE)
    ap.add_argument('--timing', choices=['realtime', 'accelerated'], default='realtime')
    ap.add_argument('--size', default='1024x768')
    ap.add_argument('--sizes', nargs='+', default=['1024x768', '800x600'])
    ap.add_argument('--persist-boots', type=int, default=1)
    ap.add_argument('--audio', choices=['none', 'wav'], default='none')
    ap.add_argument('--ide-pio-drain', choices=['on', 'off'], default='on',
                    help='macio-ide.pio-status-drain (RetroBoot firmware PIO speedup)')
    ap.add_argument('--count', type=int, default=3)
    ap.add_argument('--timeout', type=float, default=150)
    ap.add_argument('--fallback-deadline', type=float, default=60.0,
                    help='seconds after launch to send HMP classicmac-boot-complete (accelerated)')
    ap.add_argument('--clock-wait', type=float, default=180)
    ap.add_argument('--tag', default='')
    ap.add_argument('--ram', type=int, default=512)
    ap.add_argument('--rtc', help='QEMU -rtc value, e.g. base=localtime (hostd uses it)')
    ap.add_argument('--timings', nargs='+', default=['accelerated', 'realtime'])
    ap.add_argument('--rams', nargs='+', type=int, default=[512, 768])
    ap.add_argument('--slow-s', type=float, default=45, help='reliability: keep frames of boots slower than this')
    ap.add_argument('--keep-frames', action='store_true')
    ap.add_argument('--ide-dma-delay-ns', type=int, default=0,
                    help='macio-ide.dma-completion-delay-ns (0 = Wave 1 disk-boot value)')
    ap.add_argument('--share-dir', type=Path, help='attach a virtio-9p share (hostd default)')
    ap.add_argument('--resize-schedule', help='reliability: RESIZE schedule, e.g. 800x600@15,1024x768@18')
    ap.add_argument('--frames-every', type=float, default=0,
                    help='boot: save a PNG every N seconds until Finder (stall diagnosis)')
    ap.add_argument('--keep-disk', action='store_true')
    args = ap.parse_args()
    if args.scenario != 'summarize' and not (args.qemu and args.firmware):
        ap.error('--qemu and --firmware are required')
    args.root = args.root.resolve()
    if args.qemu:
        args.qemu, args.firmware = args.qemu.resolve(), args.firmware.resolve()
    args.root.mkdir(parents=True, exist_ok=True)
    if args.tag and not args.tag.endswith('-'):
        args.tag += '-'
    result = {'e2e': scenario_e2e, 'boot': scenario_boot, 'resolution': scenario_resolution,
              'reliability': scenario_reliability, 'summarize': scenario_summarize}[args.scenario](args)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
