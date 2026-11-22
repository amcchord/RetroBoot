#!/usr/bin/env python3
"""Mac OS sound qualification: QuickTime movies, game video, alerts, volume keys.

Boots a sparse copy of a disk with hostd's argv (rbd_restart_lab.build_argv:
-display retroboot behind the presenter stub, two QMP sockets, the driver
variant, pio-status-drain, Sungem NAT, virtio tablet, 9p share) and
``-audiodev wav``, waits for the Finder, then runs one scenario and reads the
WAV capture after Mac OS has shut down (emulator/reports/qtaudio-20261012.md).

movie (default): opens a movie through the Finder (type-select each name of
    --path and Command-O; dbl:X:Y double-clicks a point; --prepare steps run
    first, e.g. to copy a game from --cd to the desktop) and lets it play.
    Pass: the picture below the menu bar changes in more than half of the
    play seconds, or by >= --min-changed-px sampled pixels from 3 s into
    playback to the end, and the capture holds >= 4 half-second blocks with
    a peak >= 1000.  The Sample Movie runs to 00:00:05 with ~4.7 s of
    sound; a stalled one keeps its first frame, a 00:00:00 clock and a
    silent channel.
sounds: --alerts alert sounds (click outside the modal Shut Down dialog),
    each followed by --alert-gap s of silence (the host stream stops: the
    next alert restarts it), then --volume-keys PowerBook volume-button
    presses through QOM like hostd (needs -device adb-buttons,id=keys0).
    Pass: no sound shorter than --min-segment (r11 kept 12-23 ms of every
    alert) and at least 0.2 s of sound per alert and 0.1 s per volume beep.

    rbd_qtplay.py run --out RUN --seed DISK [--pkg PKG] [--ndrv DIR]
        [--size 1024x768] [--scenario movie|sounds]
        [--path 'Mac,Applications,QuickTime,Sample Movie'] [--prepare STEPS]
        [--cd ISO] [--after-open 'sleep:12,ret,click:X:Y,shot:NAME'] [--play 20] [--settle 20]
        [--alerts 2] [--alert-gap 12] [--volume-keys 0]
        [--trace-events FILE] [--hold S] [--audiodev SPEC] [-- QEMU ARGS]
    rbd_qtplay.py summarize RUN...

Lab tool (Linux builder, Python standard library only); evidence stays in RUN.
"""
from __future__ import annotations

import argparse
import array
import json
import os
import signal
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rbd_presenter_stub as stub  # noqa: E402
import rbd_restart_lab as lab  # noqa: E402


class DamageLog(stub.Presenter):
    """The stub plus a timestamped log of DAMAGE rects."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.dlog: list[tuple[float, list]] = []

    def handle(self, cid, data, fds, flags):
        if len(data) >= 16 and struct.unpack_from('=I', data)[0] == stub.DAMAGE:
            count = struct.unpack_from('=I', data, 12)[0]
            self.dlog.append((time.monotonic(),
                              [struct.unpack_from('=iiii', data, 16 + 16 * i) for i in range(count)]))
        super().handle(cid, data, fds, flags)


def run_step(p, step: str):
    """One --prepare step: type:TEXT, key:CHORD (meta_l+o), click:X:Y,
    shiftclick:X:Y, dbl:X:Y, drag:X0:Y0:X1:Y1 or sleep:S."""
    kind, _, arg = step.partition(':')
    if kind == 'type':
        p.type_text(arg, hold=0.04, gap=0.05)
    elif kind == 'key':
        p.tap(arg.split('+'), hold=0.1)
    elif kind in ('click', 'dbl', 'shiftclick'):
        x, y = (int(v) for v in arg.split(':'))
        if kind == 'shiftclick':
            p.key('shift', True)
            time.sleep(0.05)
        p.click(x, y, double=kind == 'dbl')
        if kind == 'shiftclick':
            p.key('shift', False)
    elif kind == 'drag':
        x0, y0, x1, y1 = (int(v) for v in arg.split(':'))
        p.drag(x0, y0, x1, y1, steps=30, step_delay=0.03, hold_after=0.3)
    elif kind == 'sleep':
        time.sleep(float(arg))
    else:
        raise ValueError(f'unknown step {step!r}')
    time.sleep(0.3)


def frame_diff(a, b) -> int:
    """Pixels (sampled every 2nd column and row, below the menu bar) that differ."""
    if not a or not b or a[:3] != b[:3]:
        return -1
    w, h, stride, ra = a
    rb = b[3]
    n = 0
    for y in range(24, h, 2):
        o = y * stride
        for x in range(0, w * 4, 8):
            if ra[o + x:o + x + 3] != rb[o + x:o + x + 3]:
                n += 1
    return n


def wav_stats(path: Path) -> dict:
    raw = path.read_bytes()
    if len(raw) < 44 or raw[:4] != b'RIFF':
        return {'error': 'no wav'}
    ch, rate = struct.unpack_from('<HI', raw, 22)
    data = array.array('h', raw[44:44 + (len(raw) - 44) // 2 * 2])
    n = len(data) // ch
    # per 0.5 s block peaks, to tell short alert sounds from a playing movie
    blk = rate // 2 * ch
    peaks = [max(map(abs, data[i:i + blk]), default=0) for i in range(0, len(data), blk)]
    # sound segments: non-silent runs separated by >= 50 ms of silence
    amp = [max(abs(data[i * ch + c]) for c in range(ch)) for i in range(n)]
    segs, start, quiet, last = [], None, 0, 0
    for i, v in enumerate(amp):
        if v > 64:
            if start is None:
                start = i
            quiet, last = 0, i
        elif start is not None:
            quiet += 1
            if quiet >= rate // 20:
                segs.append(round((last - start + 1) / rate, 3))
                start = None
    if start is not None:
        segs.append(round((last - start + 1) / rate, 3))
    return {'rate': rate, 'channels': ch, 'seconds': round(n / rate, 2), 'segments_s': segs,
            'peak': max(map(abs, data), default=0), 'nonzero': sum(1 for x in data if x),
            'loud_blocks': sum(1 for p in peaks if p >= 1000), 'blocks': len(peaks),
            **dropouts(amp, rate)}


def dropouts(amp, rate) -> dict:
    """Digital silence inside the longest sound (the movie's soundtrack).

    The soundtrack is the longest stretch of samples above 64 whose quiet
    parts are all shorter than 0.5 s; inside it, runs of exact zeros of
    >= 5 ms are dropouts: the Sound Manager mixed silence because its
    channel had no data (fleet2's measure on the N5105 and GPD: 99 runs,
    3.4 s; Ryzen: none).
    """
    segs, s0, last = [], None, None
    for i, v in enumerate(amp):
        if v > 64:
            if s0 is None or i - last > rate // 2:
                if s0 is not None:
                    segs.append((s0, last))
                s0 = i
            last = i
    if s0 is not None:
        segs.append((s0, last))
    if not segs:
        return {'span_s': 0, 'dropouts': 0, 'dropout_s': 0, 'dropout_max_ms': 0}
    s0, s1 = max(segs, key=lambda x: x[1] - x[0])
    runs, z = [], None
    for i in range(s0, s1):
        if amp[i] == 0:
            if z is None:
                z = i
        elif z is not None:
            if i - z >= rate // 200:
                runs.append(i - z)
            z = None
    return {'span_s': round((s1 - s0) / rate, 2), 'dropouts': len(runs),
            'dropout_s': round(sum(runs) / rate, 2),
            'dropout_max_ms': round(max(runs, default=0) / rate * 1000),
            'dropout_runs_ms': [round(r / rate * 1000, 1) for r in runs[:400]]}


def cpu_max_line(pct) -> str:
    """cpu.max for pct % of one CPU with the shortest period the kernel allows
    for it (quota >= 1 ms): a slower core, not 100 ms on / off stretches."""
    period = max(1000, int(1000 * 100 / pct))
    return f'{max(1000, int(period * pct / 100))} {period}\n'


def cpu_limit(a, pid):
    """A slow host for the play window, from the last open step on (the boot
    and the Finder navigation run unlimited: type-select loses keys on a
    starved guest).  --cpu-pin: QEMU's threads on these host CPUs.
    --cpu-max: QEMU in a cgroup with cpu.max = that % of one CPU.
    --vcpu-max: only the vCPU thread limited (threaded cgroup), like a slow
    core with the main loop on another core, as on a 4-core N5105.
    Root only.  Returns the cgroup to remove (None without a limit)."""
    cg = None
    if a.cpu_max or a.vcpu_max:
        cg = Path(f'/sys/fs/cgroup/rbqtplay-{os.getpid()}')
        cg.mkdir(exist_ok=True)
        (cg / 'cpu.max').write_text(cpu_max_line(a.cpu_max) if a.cpu_max else 'max 100000\n')
        if a.vcpu_max:
            # a threaded child first: the parent becomes "domain threaded" and
            # may then hold the process and enable the cpu controller below
            v = cg / 'vcpu'
            v.mkdir(exist_ok=True)
            (v / 'cgroup.type').write_text('threaded')
            (cg / 'cgroup.subtree_control').write_text('+cpu')
            (v / 'cpu.max').write_text(cpu_max_line(a.vcpu_max))
        (cg / 'cgroup.procs').write_text(str(pid))
    if a.vcpu_max:
        for tid in os.listdir(f'/proc/{pid}/task'):
            name = Path(f'/proc/{pid}/task/{tid}/comm').read_text()
            if 'TCG' in name or name.startswith('CPU'):
                (v / 'cgroup.threads').write_text(tid)
    if a.cpu_pin:
        cpus = {int(c) for c in a.cpu_pin.split(',')}
        for tid in os.listdir(f'/proc/{pid}/task'):
            os.sched_setaffinity(int(tid), cpus)
    return cg


def proc_cpu_s(pid) -> float:
    try:
        f = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return (int(f[11]) + int(f[12])) / os.sysconf('SC_CLK_TCK')
    except (OSError, IndexError):
        return 0.0


def thread_cpu_s(pid) -> dict:
    """CPU seconds per QEMU thread kind (vCPU, main loop, workers, other)."""
    out = {}
    for tid in os.listdir(f'/proc/{pid}/task'):
        try:
            name = Path(f'/proc/{pid}/task/{tid}/comm').read_text().strip()
            f = Path(f'/proc/{pid}/task/{tid}/stat').read_text().rsplit(')', 1)[1].split()
        except OSError:
            continue
        kind = ('vcpu' if 'TCG' in name or name.startswith('CPU') else
                'main' if tid == str(pid) else 'worker' if name.startswith('worker') else name)
        out[kind] = out.get(kind, 0.0) + (int(f[11]) + int(f[12])) / os.sysconf('SC_CLK_TCK')
    return out


def run(a):
    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    disk = out / 'disk.img'
    subprocess.run(['cp', '--sparse=always', a.seed, str(disk)], check=True)
    os.chmod(disk, 0o644)
    p = DamageLog(out / 'display.sock', None)
    threading.Thread(target=p.accept_loop, daemon=True).start()
    (out / 'boot0').mkdir(exist_ok=True)
    ns = argparse.Namespace(pkg=a.pkg, ndrv_dir=lab.NDRV.get(a.ndrv, a.ndrv), size=a.size, mem=a.mem,
                            audio='wav', host_resize='on', drain=True, speed='realtime', net=True,
                            share=a.share, trace_events=a.trace_events,
                            extra=['-rtc', f'base={a.clock}'] + a.extra)
    argv = lab.build_argv(ns, out, disk, 0)
    if a.audiodev:
        # a real host backend (e.g. alsa,id=snd0,out.dev=...) instead of the WAV file;
        # QMP wavcapture still records what the guest played into audio-0.wav
        argv[argv.index('-audiodev') + 1] = a.audiodev
    if a.display_extra:
        i = argv.index('-display')
        argv[i + 1] += ',' + a.display_extra
    if a.cd:
        i = next(i for i, v in enumerate(argv) if 'id=cd0' in v)
        argv[i] += f',file={a.cd},format=raw'
    (out / 'argv.json').write_text(json.dumps(argv, indent=1))
    env = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'RETROBOOT_SESSION': 'qtplay', 'HOME': '/root',
           'LANG': 'C.UTF-8', 'TZ': ':UTC'}
    cg = None
    t0 = time.monotonic()
    proc = subprocess.Popen(argv, stdout=open(out / 'qemu.out', 'w'), stderr=subprocess.STDOUT,
                            env=env, cwd=str(out), start_new_session=True)
    res = {'label': a.label, 'pkg': a.pkg, 'extra': a.extra, 'seed': a.seed,
           'cpu_pin': a.cpu_pin, 'cpu_max_pct': a.cpu_max}

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
    ev = None
    try:
        t_sock = time.monotonic() + 60     # slow (emulated) hosts create the socket late
        while not (out / 'qmp-events.sock').exists() and proc.poll() is None and time.monotonic() < t_sock:
            time.sleep(0.1)
        ev = lab.QMP(str(out / 'qmp-events.sock'))
        if a.audiodev:
            cap = lab.QMP(str(out / 'qmp-control.sock'))
            res['wavcapture'] = cap.hmp(f"wavcapture {out / 'audio-0.wav'} snd0").get('return')
            # shut the connection down: close() alone leaves it open while the reader
            # thread's file object lives, and QEMU then never greets the next client
            cap.sock.shutdown(2)
            cap.close()
        if p.wait_for(lambda: p.hello, 30):
            p.send(stub.REFRESH_RATE, struct.pack('=I', 16))
            p.send(stub.RESIZE, struct.pack('=II', *a.size))
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
        time.sleep(a.settle)
        w, h, _, _ = p.frame_raw()
        # Finder frontmost, nothing selected: click an empty desktop spot
        p.click(w // 2, h - 40)
        time.sleep(1.0)
        if a.scenario == 'sounds':
            # alert sounds (click outside the modal Shut Down dialog), each after
            # --alert-gap s of silence (the host stream has stopped by then:
            # idle-stop), then optional volume-key presses (PMU feedback beeps)
            for i in range(a.alerts):
                p.tap(['power'], hold=0.15)
                time.sleep(2.0)
                p.click(w // 2, h - 30)
                time.sleep(1.5)
                lab.save_png(p, out / f'alert-{i}.png')
                p.tap(['esc'], hold=0.1)
                time.sleep(a.alert_gap)
            if a.volume_keys:
                # the PowerBook volume buttons, as hostd presses them (keys.py):
                # QOM "press" on the adb-buttons device (-device adb-buttons,id=keys0)
                ctl = lab.QMP(str(out / 'qmp-control.sock'))
                for i in range(a.volume_keys):
                    name = 'volume-down' if i % 2 else 'volume-up'
                    for state in ('down', 'up'):
                        ctl.cmd('qom-set', path='/machine/peripheral/keys0', property='press',
                                value=f'{name}:{state}')
                        time.sleep(0.12)
                    time.sleep(2.0)
                    lab.save_png(p, out / f'volume-{i}.png')
                    time.sleep(a.alert_gap)
                ctl.close()
            time.sleep(2.0)
            res['scenario'] = 'sounds'
            res['events'] = a.alerts + a.volume_keys
            p.tap(['power'], hold=0.15)
            time.sleep(2.5)
            p.tap(['ret'], hold=0.1)
            try:
                proc.wait(a.exit_timeout)
            except subprocess.TimeoutExpired:
                res['shutdown_timeout'] = True
            with ev.cond:
                sd = [e for e in ev.events if e.get('event') == 'SHUTDOWN']
            res['shutdown'] = sd[-1]['data'] if sd else None
            res['outcome'] = 'ok'
            return res
        for step in [x for x in a.prepare.split(';') if x]:
            run_step(p, step)
        if a.prepare:
            lab.save_png(p, out / 'prepared.png')
        names = [n for n in a.path.split(',') if n]
        for i, name in enumerate(names):
            if name.startswith('dbl:'):       # dbl:X:Y double-clicks a fixed spot
                _, x, y = name.split(':')
                p.click(int(x), int(y), double=True)
            else:
                p.type_text(name, hold=0.04, gap=0.05)
                time.sleep(0.8)
                p.tap(['meta_l', 'o'], hold=0.1)
            if i == len(names) - 1 and (a.cpu_pin or a.cpu_max or a.vcpu_max):
                cg = cpu_limit(a, proc.pid)     # the movie opens and plays on the slow host
            time.sleep(a.step_wait if i < len(names) - 1 else 0.5)
            lab.save_png(p, out / f'open-{i}.png')
        for step in [x for x in a.after_open.split(',') if x]:
            if step.startswith('sleep:'):
                time.sleep(float(step[6:]))
            elif step.startswith('shot:'):
                lab.save_png(p, out / f'{step[5:]}.png')
            elif step.split(':')[0] in ('click', 'dbl', 'shiftclick', 'drag', 'type'):
                run_step(p, step)       # e.g. clicks through a game's opening
            else:
                p.tap(step.split('+'), hold=0.1)
        if a.after_open:
            lab.save_png(p, out / 'after-open.png')
        t_open = time.monotonic()
        cpu0 = proc_cpu_s(proc.pid)
        thr0 = thread_cpu_s(proc.pid)
        res['open_s'] = round(t_open - t0, 2)
        p.dlog.clear()
        shots = sorted({3, a.play // 2, a.play - 1})
        sigs = []           # frame signature (below the menu bar) once a second
        next_sig = 1.0
        first = last = None
        while time.monotonic() - t_open < a.play:
            el = time.monotonic() - t_open
            if shots and el >= shots[0]:
                lab.save_png(p, out / f'play-{shots.pop(0):02d}s.png')
            if el >= next_sig:
                fw, fh, fstride, fraw = p.frame_raw()
                sigs.append(lab.frame_sig(fraw[24 * fstride:], fstride, fw, fh - 24))
                last = (fw, fh, fstride, fraw)
                if first is None and el >= 3.0:
                    first = last            # the player window is up by now
                next_sig += 1.0
            time.sleep(0.1)
        # movie damage: rects away from the pointer and the menu bar clock, per second
        per_s = [0] * int(a.play)
        for t, rects in list(p.dlog):
            s = int(t - t_open)
            if 0 <= s < len(per_s):
                per_s[s] += sum(rw * rh for rx, ry, rw, rh in rects if ry > 30 and rw * rh > 400)
        res['damage_px_per_s'] = per_s
        el = time.monotonic() - t_open
        res['qemu_cpu_pct'] = round((proc_cpu_s(proc.pid) - cpu0) / el * 100, 1)
        res['thread_cpu_pct'] = {k: round((v - thr0.get(k, 0.0)) / el * 100, 1)
                                 for k, v in thread_cpu_s(proc.pid).items()}
        if cg is not None:
            res['cpu_max'] = (cg / 'cpu.max').read_text().strip()
            res['cpu_stat'] = dict(line.split() for line in (cg / 'cpu.stat').read_text().splitlines())
            if a.vcpu_max:
                res['vcpu_max'] = (cg / 'vcpu' / 'cpu.max').read_text().strip()
                res['vcpu_cpu_stat'] = dict(line.split() for line in
                                            (cg / 'vcpu' / 'cpu.stat').read_text().splitlines())
        # a playing movie changes the picture (movie frame, time display) every second;
        # a stalled one keeps one frozen frame
        changes = sum(1 for x, y in zip(sigs[1:], sigs[2:]) if x != y)
        res['changing_seconds'] = changes
        res['play_seconds'] = max(0, len(sigs) - 2)
        # how much of the picture changed from 3 s into playback to the end: a
        # movie that runs replaces its picture (the sample movie ends on the
        # QuickTime logo); a stalled one moves at most a few pixels
        res['changed_px'] = frame_diff(first, last)
        lab.save_png(p, out / 'play-end.png')
        if a.hold:
            # keep the session (movie playing) for out-of-band probes; HMP commands
            # in RUN/hmp.in (one per line, "sleep S" pauses) go to RUN/hmp.out;
            # deleting RUN/hold ends the hold early
            ctl = lab.QMP(str(out / 'qmp-control.sock'))
            hold = out / 'hold'
            hold.write_text(str(proc.pid))
            t_end = time.monotonic() + a.hold
            while time.monotonic() < t_end and hold.exists() and proc.poll() is None:
                cmds = out / 'hmp.in'
                if cmds.exists():
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
            ctl.close()
        # quit QuickTime Player and shut down so the WAV header is final
        p.tap(['meta_l', 'q'], hold=0.1)
        time.sleep(3)
        p.tap(['power'], hold=0.15)
        time.sleep(2.5)
        p.tap(['ret'], hold=0.1)
        try:
            proc.wait(a.exit_timeout)
        except subprocess.TimeoutExpired:
            lab.save_png(p, out / 'shutdown-timeout.png')
            res['shutdown_timeout'] = True
        with ev.cond:
            sd = [e for e in ev.events if e.get('event') == 'SHUTDOWN']
        res['shutdown'] = sd[-1]['data'] if sd else None
        res['outcome'] = 'ok'
        return res
    finally:
        if ev:
            ev.close()
        kill()
        res['exit_code'] = proc.returncode
        wav = out / 'audio-0.wav'
        if wav.exists():
            res['audio'] = wav_stats(wav)
        if res.get('outcome') == 'ok' and res.get('scenario') == 'sounds':
            segs = (res.get('audio') or {}).get('segments_s', [])
            # every event sounded and nothing was cut short (r11 kept 12-23 ms of
            # each alert; a complete alert lasts >= 0.2 s)
            # every sound complete: none cut short (r11 kept 12-23 ms of each
            # alert) and at least 0.2 s per alert, 0.1 s per volume beep.  The
            # count of segments is not checked: the capture holds only what
            # the guest played (no silence while the stream is stopped), so
            # consecutive sounds can join into one segment
            res['pass'] = (bool(segs) and min(segs) >= a.min_segment
                           and sum(segs) >= a.alerts * 0.2 + a.volume_keys * 0.1)
        elif res.get('outcome') == 'ok':
            au = res.get('audio') or {}
            moving = (res['changed_px'] >= a.min_changed_px or
                      res['changing_seconds'] * 2 > res['play_seconds'])
            res['pass'] = moving and au.get('loud_blocks', 0) >= 4
        if not a.keep_disk:
            disk.unlink(missing_ok=True)
        (out / 'result.json').write_text(json.dumps(res, indent=1))
        p.stopping = True
        if cg is not None:
            for _ in range(20):
                try:
                    if (cg / 'vcpu').exists():
                        (cg / 'vcpu').rmdir()
                    cg.rmdir()
                    break
                except OSError:
                    time.sleep(0.5)


def summarize(dirs):
    for d in dirs:
        try:
            r = json.loads((Path(d) / 'result.json').read_text())
        except (OSError, ValueError):
            continue
        au = r.get('audio') or {}
        if r.get('scenario') == 'sounds':
            print(f"{r.get('label') or d}: {r.get('outcome')} pass={r.get('pass')} sound segments "
                  f"{au.get('segments_s')} for {r.get('events')} events, shutdown "
                  f"{(r.get('shutdown') or {}).get('reason')}")
            continue
        print(f"{r.get('label') or d}: {r.get('outcome')} pass={r.get('pass')} picture changed {r.get('changed_px')} px "
              f"({r.get('changing_seconds')}/{r.get('play_seconds')} s), audio peak {au.get('peak')} loud blocks {au.get('loud_blocks')}/"
              f"{au.get('blocks')}, finder {r.get('finder_s')} s, shutdown "
              f"{(r.get('shutdown') or {}).get('reason')}")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == 'summarize':
        summarize(sys.argv[2:])
        return
    if len(sys.argv) < 2 or sys.argv[1] != 'run':
        sys.exit(__doc__)
    argv = sys.argv[2:]
    split = argv.index('--') if '--' in argv else len(argv)
    ap = argparse.ArgumentParser(prog='rbd_qtplay.py run')
    ap.add_argument('--out', required=True)
    ap.add_argument('--seed', required=True)
    ap.add_argument('--label', default=None)
    ap.add_argument('--pkg', default=lab.PKG)
    ap.add_argument('--ndrv', default='v2')
    ap.add_argument('--size', default='1024x768', type=lab.parse_size)
    ap.add_argument('--mem', type=int, default=512)
    ap.add_argument('--share', default=None)
    ap.add_argument('--clock', default='2026-10-12T09:00:00')
    ap.add_argument('--path', default='Mac,Applications,QuickTime,Sample Movie',
                    help='comma-separated type-select prefixes from the desktop to the movie '
                         '(dbl:X:Y double-clicks that point instead)')
    ap.add_argument('--settle', type=float, default=20)
    ap.add_argument('--step-wait', type=float, default=4)
    ap.add_argument('--play', type=int, default=20)
    ap.add_argument('--timeout', type=float, default=240)
    ap.add_argument('--exit-timeout', type=float, default=90)
    ap.add_argument('--trace-events', default=None)
    ap.add_argument('--audiodev', default=None,
                    help='-audiodev value instead of the WAV backend (id must be snd0), e.g. '
                         'alsa,id=snd0,out.dev=plughw:CARD=X,,DEV=0; audio-0.wav then comes from QMP wavcapture')
    ap.add_argument('--keep-disk', action='store_true')
    ap.add_argument('--min-changed-px', type=int, default=3000,
                    help='picture change (sampled pixels) that counts as a running movie')
    ap.add_argument('--scenario', choices=['movie', 'sounds'], default='movie')
    ap.add_argument('--alerts', type=int, default=2, help='sounds: alert sounds to trigger')
    ap.add_argument('--alert-gap', type=float, default=12, help='sounds: silence after each alert (s)')
    ap.add_argument('--volume-keys', type=int, default=0,
                    help='sounds: PowerBook volume-button presses (needs -device adb-buttons,id=keys0,handler=0x1f)')
    ap.add_argument('--min-segment', type=float, default=0.08, help='sounds: shortest acceptable sound (s)')
    ap.add_argument('--prepare', default='',
                    help='; separated steps before --path: type:T key:CHORD click:X:Y '
                         'shiftclick:X:Y dbl:X:Y drag:X0:Y0:X1:Y1 sleep:S (e.g. install a game)')
    ap.add_argument('--cd', default=None, help='CD image for the user CD drive (e.g. a game disc)')
    ap.add_argument('--after-open', default='',
                    help='comma list after the last Command-O: key chords (meta_l+o) or sleep:S')
    ap.add_argument('--hold', type=float, default=0, help='keep the session this long after the play window')
    ap.add_argument('--display-extra', default=None, help='appended to the -display retroboot options')
    ap.add_argument('--cpu-pin', default=None, help='QEMU on these host CPUs only (e.g. 7), like a slow host')
    ap.add_argument('--cpu-max', type=float, default=None,
                    help='QEMU in a cgroup limited to this %% of one CPU (cpu.max; needs root)')
    ap.add_argument('--vcpu-max', type=float, default=None,
                    help="only QEMU's vCPU thread limited to this %% of one CPU (threaded cgroup)")
    ap.add_argument('extra', nargs='*')
    a = ap.parse_args(argv[:split])
    a.extra = argv[split + 1:]
    a.label = a.label or Path(a.out).name
    r = run(a)
    summarize([a.out])


if __name__ == '__main__':
    main()
