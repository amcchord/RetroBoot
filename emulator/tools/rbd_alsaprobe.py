#!/usr/bin/env python3
"""Guest sound on real ALSA hardware: stream states, hardware pointer, microphone.

Built for the Gemini PDA's speaker (wave7/gemini-audio), usable on any Linux host
with /proc/asound status files (CONFIG_SND_VERBOSE_PROCFS).  Three recorders run
side by side, off the emulator's CPUs (--probe-cpus):

* the playback substream's status file, read every --poll-ms (state, hw_ptr,
  appl_ptr, avail), one row per change: when the stream ran, how many frames the
  hardware played, every XRUN and re-prepare;
* optionally the microphone (arecord), the objective "was it audible" check;
  aligned through the capture substream's trigger time;
* QEMU's log-backend trace (screamer_*, alsa_*, audio_*) when the run has one.

    rbd_alsaprobe.py mon --out DIR [--seconds S] [--mic] [--card 0 --device 0]
        records until S seconds pass or DIR/stop appears (e.g. around a
        speaker-test or a hostd-launched guest)
    rbd_alsaprobe.py run --out DIR [--mic] [--place gemini] [--a72-max] -- RBD_QTPLAY_ARGS
        runs ``rbd_qtplay.py run --out DIR/qt RBD_QTPLAY_ARGS`` (give it
        --audiodev alsa,...) with the recorders around it; --place gemini puts
        QEMU's TCG thread on cpu8-9 and its other threads on cpu4-7 like hostd
        (cpuplace.py), --a72-max lifts the A72 policy to performance and its top
        clock for the run (restored afterwards)
    rbd_alsaprobe.py loopback --out DIR [--freq 600] [--seconds 3]
        speaker-test tone with the microphone: validates the mic path
    rbd_alsaprobe.py analyze DIR      (writes DIR/probe.json, prints a summary)

Mic route (MT6351 AIN0, Capture_1 = device 3) is set by --mic-route gemini.
Lab tool (Python standard library only); evidence stays in DIR.
"""
from __future__ import annotations

import argparse
import array
import calendar
import json
import os
import re
import signal
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
GEMINI_MIC_ROUTE = [
    ["sset", "PGA L Mux", "AIN0"], ["sset", "PGA R Mux", "AIN0"],
    ["sset", "ADC L Mux", "Left Preamplifier"], ["sset", "ADC R Mux", "Right Preamplifier"],
    ["cset", "name=UL1_CH1 ADDA_UL_CH1", "on"], ["cset", "name=UL1_CH2 ADDA_UL_CH2", "on"],
    ["sset", "PGA", "3,3"],
]


# ---------------------------------------------------------------- recorders
def parse_status(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        k, _, v = line.partition(':')
        k, v = k.strip(), v.strip()
        if k in ('state', 'trigger_time', 'hw_ptr', 'appl_ptr', 'avail', 'delay', 'owner_pid'):
            out[k] = v
    if not out and text.strip() == 'closed':
        out['state'] = 'CLOSED'
    return out


class StatusPoller(threading.Thread):
    """Rows (mono, wall, state, hw_ptr, appl_ptr, avail, delay, trigger) on change."""

    def __init__(self, path: Path, out: Path, poll_ms: float, extra: list[Path] = ()):
        super().__init__(daemon=True)
        self.path, self.out, self.poll = path, out, poll_ms / 1000.0
        self.extra = list(extra)            # other substreams: state changes only
        self.stop = threading.Event()
        self.rows = 0

    def run(self):
        last = None
        last_extra = {}
        with open(self.out, 'w') as f:
            f.write('mono,wall,state,hw_ptr,appl_ptr,avail,delay,trigger,file\n')
            while not self.stop.is_set():
                mono, wall = time.monotonic(), time.time()
                for i, p in enumerate([self.path] + self.extra):
                    try:
                        st = parse_status(p.read_text())
                    except OSError:
                        st = {'state': 'GONE'}
                    key = (st.get('state'), st.get('hw_ptr'), st.get('appl_ptr'), st.get('trigger_time'))
                    if i == 0:
                        if key == last:
                            continue
                        last = key
                    else:
                        if last_extra.get(p) == key[0::3]:
                            continue
                        last_extra[p] = key[0::3]
                    f.write(f"{mono:.6f},{wall:.6f},{st.get('state', '')},{st.get('hw_ptr', '')},"
                            f"{st.get('appl_ptr', '')},{st.get('avail', '')},{st.get('delay', '')},"
                            f"{st.get('trigger_time', '')},{i}\n")
                    self.rows += 1
                time.sleep(self.poll)


PINCTRL_PINS = Path('/sys/kernel/debug/pinctrl/10005000.pinctrl-pinctrl_paris/pins')


class AmpPoller(threading.Thread):
    """Speaker amplifier GPIOs (Gemini: 243, 244) from MediaTek's pinctrl debug file
    ("243: MDIO..." = mode, direction, input, output), every poll_ms, one row per
    change: when rb-gemini-audio actually switched the amplifiers."""

    def __init__(self, pins: list[int], out: Path, poll_ms: float = 25.0, path: Path = PINCTRL_PINS):
        super().__init__(daemon=True)
        self.pins, self.out, self.poll, self.path = pins, out, poll_ms / 1000.0, path
        self.stop = threading.Event()

    def run(self):
        rx = re.compile(r'^pin (\d+) .*?\d+: (\d)(\d)(\d)(\d)')
        hp = next(iter(Path('/sys/kernel/debug/asoc').glob('*/*/dapm/HPL Power')), None)
        last = None
        with open(self.out, 'w') as f:
            f.write('mono,' + ','.join(f'amp{p}' for p in self.pins) + ',hp\n')
            while not self.stop.is_set():
                vals = {}
                try:
                    for line in self.path.read_text().splitlines():
                        m = rx.match(line)
                        if m and int(m.group(1)) in self.pins:
                            vals[int(m.group(1))] = m.group(5) if m.group(3) == '1' else 'i'
                except OSError:
                    return
                try:
                    hp_on = 'H' if hp and ': On' in hp.read_text().splitlines()[0] else 'h'
                except (OSError, IndexError):
                    hp_on = '?'
                key = tuple(vals.get(p, '?') for p in self.pins) + (hp_on,)
                if key != last:
                    f.write(f'{time.monotonic():.4f},' + ','.join(key) + '\n')
                    f.flush()
                    last = key
                time.sleep(self.poll)


def set_mic_route(card: str):
    for cmd in GEMINI_MIC_ROUTE:
        subprocess.run(['amixer', '-q', '-c', card, *cmd], check=False)


def start_mic(out: Path, card_id: str, device: int, rate: int, cpus: str | None):
    argv = ['arecord', '-q', '-D', f'plughw:CARD={card_id},DEV={device}', '-f', 'S16_LE', '-r', str(rate),
            '-c', '2', '-t', 'wav', str(out)]
    if cpus:
        argv = ['taskset', '-c', cpus] + argv
    return subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=open(str(out) + '.err', 'w'))


def stop_proc(p, sig=signal.SIGINT, wait=5):
    if p and p.poll() is None:
        p.send_signal(sig)
        try:
            p.wait(wait)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()


class Recorders:
    def __init__(self, a, out: Path):
        self.a, self.out = a, out
        self.card = Path(f'/proc/asound/card{a.card}')
        try:
            self.card_id = (self.card / 'id').read_text().strip()
        except OSError:
            self.card_id = str(a.card)
        self.status = self.card / f'pcm{a.device}p' / 'sub0' / 'status'
        self.cap_status = self.card / f'pcm{a.mic_device}c' / 'sub0' / 'status'
        self.poller = None
        self.mic = None
        self.amps = None

    def start(self):
        if self.a.probe_cpus:
            os.sched_setaffinity(0, {int(c) for c in parse_cpulist(self.a.probe_cpus)})
        meta = {'status': str(self.status), 'card_id': self.card_id, 'poll_ms': self.a.poll_ms,
                'start_wall': time.time(), 'start_mono': time.monotonic()}
        extra = [self.cap_status] if self.a.mic else []
        self.poller = StatusPoller(self.status, self.out / 'status.csv', self.a.poll_ms, extra)
        self.poller.start()
        if self.a.amp_pins and PINCTRL_PINS.exists():
            self.amps = AmpPoller([int(x) for x in self.a.amp_pins.split(',')], self.out / 'amps.csv')
            self.amps.start()
        if self.a.mic:
            if self.a.mic_route == 'gemini':
                set_mic_route(str(self.a.card))
            meta['mic'] = {'device': self.a.mic_device, 'rate': self.a.mic_rate, 'spawn_mono': time.monotonic()}
            self.mic = start_mic(self.out / 'mic.wav', self.card_id, self.a.mic_device, self.a.mic_rate,
                                 self.a.probe_cpus)
        (self.out / 'probe-meta.json').write_text(json.dumps(meta, indent=1))

    def stop(self):
        stop_proc(self.mic)
        if self.amps:
            self.amps.stop.set()
            self.amps.join(5)
        if self.poller:
            self.poller.stop.set()
            self.poller.join(5)


def parse_cpulist(text: str) -> list[int]:
    out = []
    for part in text.split(','):
        if '-' in part:
            a, b = part.split('-')
            out += range(int(a), int(b) + 1)
        elif part:
            out.append(int(part))
    return out


# ---------------------------------------------------------------- placement
class Placement(threading.Thread):
    """hostd's Gemini placement (cpuplace.py) for a lab QEMU: TCG thread(s) on
    ``vcpu``, every other thread on ``other``; re-applied every 2 s."""

    def __init__(self, parent_pid: int, vcpu: str, other: str):
        super().__init__(daemon=True)
        self.parent, self.vcpu, self.other = parent_pid, set(parse_cpulist(vcpu)), set(parse_cpulist(other))
        self.stop = threading.Event()
        self.placed = {}

    def qemu_pids(self):
        out = []
        for d in Path('/proc').iterdir():
            if not d.name.isdigit():
                continue
            try:
                if (d / 'comm').read_text().strip() == 'qemu-system-ppc':
                    out.append(int(d.name))
            except OSError:
                continue
        return out

    def run(self):
        while not self.stop.is_set():
            for pid in self.qemu_pids():
                try:
                    tids = os.listdir(f'/proc/{pid}/task')
                except OSError:
                    continue
                for tid in tids:
                    try:
                        name = Path(f'/proc/{pid}/task/{tid}/comm').read_text().strip()
                        want = self.vcpu if ('TCG' in name or name.startswith('CPU')) else self.other
                        if os.sched_getaffinity(int(tid)) != want:
                            os.sched_setaffinity(int(tid), want)
                            self.placed[tid] = [name, sorted(want)]
                    except OSError:
                        continue
            self.stop.wait(2.0)


class A72Policy:
    """The vCPU cluster's cpufreq policy as hostd sets it while Mac OS runs."""

    def __init__(self, policy: str = '/sys/devices/system/cpu/cpufreq/policy8'):
        self.d = Path(policy)
        self.saved = {}

    def apply(self):
        if not self.d.is_dir():
            return None
        for name in ('scaling_governor', 'scaling_max_freq'):
            self.saved[name] = (self.d / name).read_text().strip()
        try:
            (self.d / 'scaling_governor').write_text('performance')
            (self.d / 'scaling_max_freq').write_text((self.d / 'cpuinfo_max_freq').read_text().strip())
        except OSError as e:
            return {'error': str(e)}
        return {k: (self.d / k).read_text().strip() for k in ('scaling_governor', 'scaling_max_freq')}

    def restore(self):
        for name in ('scaling_max_freq', 'scaling_governor'):
            if name in self.saved:
                try:
                    (self.d / name).write_text(self.saved[name])
                except OSError:
                    pass


# ---------------------------------------------------------------- analysis
def read_wav(path: Path):
    raw = path.read_bytes()
    if len(raw) < 44 or raw[:4] != b'RIFF':
        return None
    # find the data chunk (arecord and QEMU both write a plain 44-byte header,
    # but arecord's size fields stay 0x7fffffff when it is interrupted)
    pos, ch, rate, data = 12, 2, 16000, None
    while pos + 8 <= len(raw):
        cid, size = raw[pos:pos + 4], struct.unpack_from('<I', raw, pos + 4)[0]
        if cid == b'fmt ':
            ch, rate = struct.unpack_from('<HI', raw, pos + 10)
        if cid == b'data':
            data = raw[pos + 8:]
            break
        pos += 8 + size
    if data is None:
        return None
    a = array.array('h', data[:len(data) // 2 * 2])
    return rate, ch, a


def envelope(rate, ch, a, win_ms=10):
    """RMS per window and channel: [(rms_l, rms_r)]."""
    n = rate * win_ms // 1000
    out = []
    frames = len(a) // ch
    for i in range(0, frames - n + 1, n):
        seg = a[i * ch:(i + n) * ch]
        rms = []
        for c in range(ch):
            s = seg[c::ch]
            rms.append((sum(x * x for x in s) / len(s)) ** 0.5)
        out.append(rms)
    return out


def segments(flags, win_s, min_gap=5):
    """Runs of True (windows), merging gaps shorter than min_gap windows."""
    segs, start, gap = [], None, 0
    for i, f in enumerate(flags):
        if f:
            if start is None:
                start = i
            gap = 0
            end = i
        elif start is not None:
            gap += 1
            if gap >= min_gap:
                segs.append((start * win_s, (end + 1) * win_s))
                start = None
    if start is not None:
        segs.append((start * win_s, (end + 1) * win_s))
    return segs


def mic_analysis(path: Path, t0_mono: float | None, noise=None):
    w = read_wav(path)
    if not w:
        return {'error': 'no mic wav'}
    rate, ch, a = w
    env = envelope(rate, ch, a, 10)
    if not env:
        return {'error': 'empty'}
    # noise floor per channel: the 20th percentile window
    floor = []
    for c in range(ch):
        v = sorted(e[c] for e in env)
        floor.append(v[len(v) // 5])
    if noise:
        floor = [max(f, n) for f, n in zip(floor, noise)]
    loud = [any(e[c] > max(3 * floor[c], floor[c] + 150) for c in range(ch)) for e in env]
    segs = segments(loud, 0.01)
    res = {'seconds': round(len(a) / ch / rate, 2), 'floor_rms': [round(f) for f in floor],
           'peak_rms': [round(max(e[c] for e in env)) for c in range(ch)],
           'segments': [[round(s, 2), round(e, 2), round(e - s, 2)] for s, e in segs],
           'audible_s': round(sum(e - s for s, e in segs), 2)}
    if t0_mono is not None:
        res['t0_mono'] = round(t0_mono, 4)
        res['segments_mono'] = [[round(t0_mono + s, 3), round(t0_mono + e, 3)] for s, e in segs]
    return res


def mic_match(d: Path, res: dict, win_ms=20, loud=800, max_lag_ms=80):
    """Was each guest sound heard, all of it?  The guest's output (wavcapture: exactly
    what the screamer handed to the host, in order) is cut per sound, and its 20 ms
    envelope is laid over the microphone's from the moment the host stream ran (best
    lag 0-80 ms: buffer and acoustic delay).  A window where the guest's output is loud
    (peak RMS >= ``loud``) counts as heard when the microphone is above three times
    its floor (and 60).  heard_share 1.0: the whole sound came out of the speaker;
    first_heard_ms: where it started to."""
    tr, mic = res.get('trace'), res.get('mic') or {}
    cap = d / 'qt' / 'audio-0.wav'
    if not tr or mic.get('t0_mono') is None or not cap.exists() or not (d / 'mic.wav').exists():
        return None
    w, m = read_wav(cap), read_wav(d / 'mic.wav')
    if not w or not m:
        return None
    rate, ch, a = w
    mrate, mch, ma = m
    menv = [e[0] for e in envelope(mrate, mch, ma, win_ms)]          # left mic channel (the quieter floor)
    thr = max(60, 3 * mic['floor_rms'][0])
    runs = sorted(t for e in res.get('episodes', []) for t in e.get('running_at', []))
    out = []
    for snd in tr['sounds']:
        if not snd.get('host_t0') or snd['host_bytes'] < 4 * 441 * 5:
            continue
        f0, n = snd['capture_offset'] // 4, snd['host_bytes'] // 4
        genv = [max(e) for e in envelope(rate, ch, a[f0 * ch:(f0 + n) * ch], win_ms)]
        t_run = next((t for t in runs if t >= snd['host_t0'] - 0.05), None)
        if t_run is None or not genv:
            continue
        i0 = int(round((t_run - mic['t0_mono']) * 1000 / win_ms))
        best = None
        for lag in range(0, max_lag_ms // win_ms + 1):
            seg = menv[i0 + lag:i0 + lag + len(genv)]
            if len(seg) < len(genv):
                break
            score = sum(x * y for x, y in zip(genv, seg))
            if best is None or score > best[0]:
                best = (score, lag)
        if best is None:
            continue
        seg = menv[i0 + best[1]:i0 + best[1] + len(genv)]
        loud_w = [i for i, g in enumerate(genv) if g >= loud]
        heard = [i for i in loud_w if seg[i] > thr]
        out.append({'t0': snd['t0'], 'sound_s': round(n / 44100, 3), 'lag_ms': best[1] * win_ms,
                    'loud_windows': len(loud_w), 'heard_windows': len(heard),
                    'heard_share': round(len(heard) / len(loud_w), 2) if loud_w else None,
                    'first_loud_ms': loud_w[0] * win_ms if loud_w else None,
                    'first_heard_ms': heard[0] * win_ms if heard else None})
    return out


def load_status(path: Path):
    rows = []
    if not path.exists():
        return rows
    with open(path) as f:
        next(f, None)
        for line in f:
            p = line.rstrip('\n').split(',')
            if len(p) < 9:
                continue
            rows.append({'mono': float(p[0]), 'wall': float(p[1]), 'state': p[2],
                         'hw': int(p[3]) if p[3].isdigit() else None,
                         'appl': int(p[4]) if p[4].isdigit() else None,
                         'avail': int(p[5]) if p[5].lstrip('-').isdigit() else None,
                         'trigger': float(p[7]) if p[7] else None, 'file': int(p[8])})
    return rows


def stream_episodes(rows):
    """Playback episodes: from leaving SETUP/CLOSED to returning there.  For each: the
    RUNNING spans, the frames the hardware played in them (hw_ptr advance while
    RUNNING; QEMU's appl_ptr restarts at every prepare), XRUNs and re-prepares
    (RUNNING -> XRUN/PREPARED without SETUP)."""
    eps, cur, prev = [], None, None
    for r in (r for r in rows if r['file'] == 0):
        st = r['state']
        idle = st in ('SETUP', 'CLOSED', 'OPEN', 'GONE', '')
        if cur is None and not idle:
            cur = {'start': r['mono'], 'runs': [], 'xruns': 0, 'reprepares': 0, 'played': 0, 'states': []}
        if cur is not None:
            if st == 'RUNNING' and r['hw'] is not None:
                if prev != 'RUNNING':
                    cur['runs'].append([r['mono'], None])
                    cur['hw_start'] = r['hw']
                cur['hw_last'] = r['hw']
            if prev == 'RUNNING' and st != 'RUNNING' and cur['runs']:
                cur['runs'][-1][1] = r['mono']
                cur['played'] += cur.get('hw_last', 0) - cur.get('hw_start', 0)
            if not cur['states'] or cur['states'][-1][1] != st:
                cur['states'].append((round(r['mono'], 4), st))
                if st == 'XRUN':
                    cur['xruns'] += 1
                if st == 'PREPARED' and prev in ('RUNNING', 'XRUN'):
                    cur['reprepares'] += 1
            if idle:
                cur['end'] = r['mono']
                eps.append(cur)
                cur = None
        prev = st
    if cur is not None:
        cur['end'] = None
        eps.append(cur)
    out = []
    for e in eps:
        spans = [round((t1 - t0) * 1000) if t1 else None for t0, t1 in e['runs']]
        out.append({'start': round(e['start'], 3), 'end': round(e['end'], 3) if e['end'] else None,
                    'running_spans_ms': spans, 'running_at': [round(t0, 3) for t0, _ in e['runs']],
                    'running_ms': sum(x or 0 for x in spans), 'played_frames': e['played'],
                    'xruns': e['xruns'], 'reprepares': e['reprepares'], 'states': e['states'][:60]})
    return out


TRACE_ISO = re.compile(r'^(\d+)-(\d+)-(\d+)T(\d+):(\d+):(\d+)\.(\d+)Z (?:\d+@\d+\.\d+:)?(\S+)\s?(.*)$')
TRACE_PID = re.compile(r'^(\d+)@(\d+)\.(\d+):(\S+)\s?(.*)$')


def trace_events(path: Path):
    """(wall, event, args) from a log-backend trace."""
    if not path.exists():
        return
    with open(path, errors='replace') as f:
        for ln in f:
            m = TRACE_ISO.match(ln)
            if m:
                g = [int(x) for x in m.groups()[:6]]
                t = calendar.timegm((g[0], g[1], g[2], g[3], g[4], g[5], 0, 0, 0)) + int(m.group(7)) / 10 ** len(m.group(7))
                yield t, m.group(8), m.group(9)
                continue
            m = TRACE_PID.match(ln)
            if m:
                yield int(m.group(2)) + int(m.group(3)) / 1e6, m.group(4), m.group(5)


def kv(args: str) -> dict:
    out = {}
    toks = args.split()
    for i in range(len(toks) - 1):
        if re.fullmatch(r'[a-z_]+', toks[i]) and re.fullmatch(r'-?\d+', toks[i + 1]):
            out[toks[i]] = int(toks[i + 1])
    return out


def trace_analysis(path: Path, wall_to_mono: float | None):
    """Per sound (runs of screamer_out separated by > 0.3 s): bytes the guest DMA'd,
    DMA wall span against its play time, host writes, underruns, drops, holds,
    xruns, late timers."""
    sounds, cur = [], None
    counts = {}
    timer_late = []
    for t, ev, args in trace_events(path) or []:
        counts[ev] = counts.get(ev, 0) + 1
        mono = t + wall_to_mono if wall_to_mono is not None else t
        if ev == 'audio_timer_delayed':
            m = re.search(r'(\d+)', args)
            timer_late.append((round(mono, 3), int(m.group(1)) if m else None))
            continue
        if ev == 'screamer_out':
            k = kv(args)
            if cur is None or mono - cur['last_out'] > 0.3:
                cur = {'t0': mono, 'last_out': mono, 'bytes': 0, 'cmds': 0, 'host_bytes': 0, 'host_calls': 0,
                       'underruns': 0, 'drops': 0, 'holds': 0, 'xruns': 0, 'max_late_us': 0, 'first_host': None,
                       'enable': [], 'max_gap_ms': 0.0, 'leads': []}
                sounds.append(cur)
            cur['max_gap_ms'] = max(cur['max_gap_ms'], (mono - cur['last_out']) * 1000)
            cur['last_out'] = mono
            cur['bytes'] += k.get('len', 0)
            cur['cmds'] += 1
            cur['max_late_us'] = max(cur['max_late_us'], k.get('late_us', 0))
        elif cur is not None:
            if ev == 'screamer_host':
                k = kv(args)
                cur['host_bytes'] += k.get('written', 0)
                cur['host_calls'] += 1
                if cur['first_host'] is None and k.get('written'):
                    cur['first_host'] = mono
                cur['last_host'] = mono
            elif ev == 'screamer_underrun':
                cur['underruns'] += 1
                m = re.search(r'lead now (\d+)', args)
                if m:
                    cur['leads'].append(int(m.group(1)))
                cur.setdefault('underrun_at', []).append(round(mono - cur['t0'], 3))
            elif ev == 'screamer_drop':
                cur['drops'] += 1
            elif ev == 'screamer_hold':
                cur['holds'] += 1
            elif ev == 'alsa_xrun_out':
                cur['xruns'] += 1
            elif ev == 'alsa_enable_out':
                cur['enable'].append((round(mono - cur['t0'], 3), args))
    out = []
    offset = 0
    for s in sounds:
        play = s['bytes'] / 4 / 44100
        span = s['last_out'] - s['t0']
        out.append({'t0': round(s['t0'], 3), 'host_t0': round(s['first_host'], 4) if s['first_host'] else None,
                    'capture_offset': offset,
                    'dma_bytes': s['bytes'], 'dma_play_s': round(play, 3),
                    'dma_wall_s': round(span, 3), 'dma_max_gap_ms': round(s['max_gap_ms'], 1),
                    'host_bytes': s['host_bytes'], 'host_calls': s['host_calls'],
                    'host_first_after_ms': round((s['first_host'] - s['t0']) * 1000, 1) if s['first_host'] else None,
                    'underruns': s['underruns'], 'underrun_at_s': s.get('underrun_at', [])[:20],
                    'leads': s['leads'][:20], 'drops': s['drops'], 'holds': s['holds'], 'alsa_xruns': s['xruns'],
                    'max_late_us': s['max_late_us'], 'alsa_enable': s['enable'][:10]})
        offset += s['host_bytes']
    return {'counts': counts, 'sounds': out, 'timer_delayed': timer_late[:200],
            'timer_delayed_n': len(timer_late)}


def analyze(d: Path) -> dict:
    meta = json.loads((d / 'probe-meta.json').read_text()) if (d / 'probe-meta.json').exists() else {}
    rows = load_status(d / 'status.csv')
    res = {'dir': str(d), 'meta': meta}
    res['episodes'] = stream_episodes(rows)
    amps = d / 'amps.csv'
    if amps.exists():
        changes = []
        with open(amps) as f:
            next(f, None)
            for line in f:
                p = line.strip().split(',')
                changes.append((float(p[0]), ''.join(p[1:3]) + ('' if len(p) < 4 else ' ' + p[3])))
        res['amp_changes'] = changes
        for e in res['episodes']:
            # amplifier state when the stream first ran, and when they came on
            t = e['running_at'][0] if e['running_at'] else e['start']
            state = next((v for tt, v in reversed(changes) if tt <= t), None)
            on_at = next((tt for tt, v in changes if tt >= e['start'] - 5 and '1' in v and tt >= t), None)
            e['amps_at_run'] = state
            e['amps_on_after_ms'] = round((on_at - t) * 1000) if on_at and state is not None and '1' not in state else None
    wall_to_mono = (meta['start_mono'] - meta['start_wall']) if meta.get('start_wall') else None
    for trace in (d / 'qt' / 'boot0' / 'trace.log', d / 'boot0' / 'trace.log', d / 'trace.log'):
        if trace.exists():
            res['trace'] = trace_analysis(trace, wall_to_mono)
            break
    mic = d / 'mic.wav'
    if mic.exists():
        # the capture stream's trigger time (CLOCK_MONOTONIC) is the mic's t0
        t0 = None
        for r in rows:
            if r['file'] == 1 and r['state'] == 'RUNNING' and r['trigger']:
                t0 = r['trigger']
                break
        res['mic'] = mic_analysis(mic, t0)
    for wav in (d / 'qt' / 'audio-0.wav', d / 'audio-0.wav', d / 'capture.wav'):
        if wav.exists():
            sys.path.insert(0, str(HERE))
            import rbd_qtplay
            res['guest_wav'] = rbd_qtplay.wav_stats(wav)
            res['guest_wav'].pop('dropout_runs_ms', None)
            break
    if os.environ.get('RB_MIC_MATCH', '1') != '0':
        res['mic_match'] = mic_match(d, res)
    qt = d / 'qt' / 'result.json'
    if not qt.exists() and (d / 'argv.json').exists():
        qt = d / 'result.json'
    if qt.exists():
        res['qtplay'] = json.loads(qt.read_text())
        res['qtplay'].pop('audio', None)
    (d / 'probe.json').write_text(json.dumps(res, indent=1))
    return res


def summary(res: dict):
    print(f"== {res['dir']}")
    for e in res.get('episodes', []):
        print(f"  stream {e['start']:.3f}-{e['end']}: running {e['running_ms']} ms in spans {e['running_spans_ms'][:12]}, "
              f"{e['played_frames']} frames played, xruns {e['xruns']}, re-prepares {e['reprepares']}, amps {e.get('amps_at_run')} at the first run"
              + (f", on {e['amps_on_after_ms']} ms later" if e.get('amps_on_after_ms') is not None else ''))
    tr = res.get('trace')
    if tr:
        for s in tr['sounds']:
            print(f"  guest sound @{s['t0']:.3f}: DMA {s['dma_play_s']} s of audio in {s['dma_wall_s']} s "
                  f"(max gap {s['dma_max_gap_ms']} ms), host got {s['host_bytes'] / 4 / 44100:.3f} s in "
                  f"{s['host_calls']} calls (first after {s['host_first_after_ms']} ms), underruns {s['underruns']} "
                  f"{s['underrun_at_s'][:6]}, drops {s['drops']}, holds {s['holds']}, xruns {s['alsa_xruns']}")
        print(f"  audio timer delayed {tr['timer_delayed_n']} times")
    m = res.get('mic')
    if m and 'segments' in m:
        print(f"  mic: floor {m['floor_rms']} peak {m['peak_rms']}, audible {m['audible_s']} s in "
              f"{len(m['segments'])} segments {m['segments'][:12]}")
    for mm in res.get('mic_match') or []:
        print(f"  mic vs guest sound @{mm['t0']:.3f} ({mm['sound_s']} s): heard {mm['heard_windows']}/"
              f"{mm['loud_windows']} loud 20 ms windows, first loud at {mm['first_loud_ms']} ms, first heard at "
              f"{mm['first_heard_ms']} ms (lag {mm['lag_ms']} ms)")
    g = res.get('guest_wav')
    if g:
        print(f"  guest output (capture): {g.get('segments_s')} peak {g.get('peak')}")


# ---------------------------------------------------------------- commands
def cmd_mon(a):
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rec = Recorders(a, out)
    rec.start()
    t_end = time.monotonic() + a.seconds if a.seconds else None
    try:
        while not (out / 'stop').exists() and (t_end is None or time.monotonic() < t_end):
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    rec.stop()
    summary(analyze(out))


def cmd_loopback(a):
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    a.mic = True
    rec = Recorders(a, out)
    rec.start()
    time.sleep(1.5)
    t = time.monotonic()
    p = subprocess.run(['timeout', str(a.seconds + 1), 'speaker-test', '-D',
                        f'plughw:CARD={rec.card_id},DEV={a.device}', '-c', '2', '-t', 'sine', '-f', str(a.freq),
                        '-l', '1', '-p', '100000'], capture_output=True, text=True)
    time.sleep(1.5)
    rec.stop()
    res = analyze(out)
    res['tone_started_mono'] = round(t, 3)
    (out / 'probe.json').write_text(json.dumps(res, indent=1))
    summary(res)


def cmd_run(a, rest):
    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    policy = A72Policy() if a.a72_max else None
    pol = policy.apply() if policy else None
    rec = Recorders(a, out)
    rec.start()
    qt = [sys.executable, str(HERE / 'rbd_qtplay.py'), 'run', '--out', str(out / 'qt')] + rest
    if a.nice is not None:
        qt = ['nice', '-n', str(a.nice)] + qt
    if a.probe_cpus:
        qt = ['taskset', '-c', a.probe_cpus] + qt     # the presenter stub and driver, off QEMU's CPUs
    (out / 'qtplay-argv.json').write_text(json.dumps(qt))
    place = None
    if a.place == 'gemini':
        place = Placement(os.getpid(), '8-9', '4-7')
        place.start()
    rc = None
    try:
        rc = subprocess.run(qt).returncode
    finally:
        if place:
            place.stop.set()
        rec.stop()
        if policy:
            policy.restore()
    res = analyze(out)
    res['returncode'] = rc
    res['a72_policy'] = pol
    res['placed'] = place.placed if place else None
    (out / 'probe.json').write_text(json.dumps(res, indent=1))
    summary(res)


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ('mon', 'run', 'loopback', 'analyze'):
        sys.exit(__doc__)
    cmd = sys.argv[1]
    argv = sys.argv[2:]
    if cmd == 'analyze':
        for d in argv:
            summary(analyze(Path(d)))
        return
    split = argv.index('--') if '--' in argv else len(argv)
    ap = argparse.ArgumentParser(prog=f'rbd_alsaprobe.py {cmd}')
    ap.add_argument('--out', required=True)
    ap.add_argument('--card', type=int, default=0)
    ap.add_argument('--device', type=int, default=0)
    ap.add_argument('--poll-ms', type=float, default=1.0)
    ap.add_argument('--probe-cpus', default='0-3', help='CPUs for the recorders (and the qtplay driver)')
    ap.add_argument('--mic', action='store_true')
    ap.add_argument('--mic-device', type=int, default=3)
    ap.add_argument('--mic-rate', type=int, default=16000)
    ap.add_argument('--mic-route', default='gemini', choices=['gemini', 'none'])
    ap.add_argument('--seconds', type=float, default=0)
    ap.add_argument('--amp-pins', default='243,244', help='speaker amplifier GPIOs to log (Gemini); empty: none')
    ap.add_argument('--freq', type=int, default=600)
    ap.add_argument('--place', default=None, choices=[None, 'gemini'])
    ap.add_argument('--a72-max', action='store_true')
    ap.add_argument('--nice', type=int, default=-5, help='QEMU niceness (retroboot-guest.service: -5)')
    a = ap.parse_args(argv[:split])
    rest = argv[split + 1:]
    if cmd == 'mon':
        cmd_mon(a)
    elif cmd == 'loopback':
        cmd_loopback(a)
    else:
        cmd_run(a, rest)


if __name__ == '__main__':
    main()
