#!/usr/bin/env python3
"""Quadra 800 (68k) lab scenarios with an in-process presenter (D044, LEGACY-01/02).

Each scenario launches the contract argv (rbd_quadra.contract_argv) with the
presenter stub's Presenter running in this process (frames are read straight
from the shared memfd), records milestones against the QEMU launch time, drives
Mac OS through the display socket and writes RUN/result.json.  Lab tooling only.

    rbd_quadra_lab.py boot    --pkg PKG --rom ROM --pram-seed SEED --disk DISK --run RUN [--idle 30] [--end shutdown|restart|kill]
    rbd_quadra_lab.py latency ...same...      # key-to-screen through the display socket
    rbd_quadra_lab.py pointer ...same...      # Mac OS cursor frames per second in qfb VRAM
    rbd_quadra_lab.py seed    ...  --out SEED # make the PRAM seed (no virtio card, then with it)

Milestones (seconds after QEMU's launch): ``first_frame`` (first DAMAGE), ``desktop``
(the menu-bar band, any depth), ``finder`` (the band plus the Apple logo's red, 8-bit
and deeper), ``quiet`` (desktop, then no drawing for --quiet seconds) and
``responsive`` (the Apple menu opened and closed on input).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import rbd_quadra as rq  # noqa: E402
from rbd_lab import QMP  # noqa: E402
from rbd_presenter_stub import DAMAGE, Presenter, analyze_frame, control_server, xrgb_to_rgb, _save_frame  # noqa: E402
import struct  # noqa: E402

MENU_BAR_ROWS = 48   # drawing above this is the menu bar (Mac OS 8's clock flashes its colon)


class LabPresenter(Presenter):
    """The stub's Presenter, also timing drawing below the menu bar."""

    def __init__(self, *args, **kw):
        self.last_body_damage = None
        self.body_damages = 0
        super().__init__(*args, **kw)

    def handle(self, cid, data, fds, flags):
        if len(data) >= 16 and struct.unpack_from('=I', data)[0] == DAMAGE:
            count = struct.unpack_from('=I', data, 12)[0]
            for i in range(min(count, 64)):
                x, y, w, h = struct.unpack_from('=iiii', data, 16 + 16 * i)
                if y + h > MENU_BAR_ROWS:
                    self.last_body_damage = time.monotonic()
                    self.body_damages += 1
                    break
        super().handle(cid, data, fds, flags)


def menubar(width: int, height: int, stride: int, raw: bytes) -> bool:
    """Mac OS's menu bar at any depth: a white band (rows 2-17 at least 70 %
    bright, some dark text) above a black line (row 19 or 20 at least 90 % dark).
    The ROM's 50 % gray dither and the Happy Mac screen fail it."""
    def px(y):
        row = raw[y * stride:y * stride + width * 4]
        return list(zip(row[2::4], row[1::4], row[0::4]))
    if height < 24 or width < 64:
        return False
    bright = dark = n = 0
    for y in range(2, 18):
        for r, g, b in px(y):
            n += 1
            if r > 170 and g > 170 and b > 170:
                bright += 1
            elif r < 60 and g < 60 and b < 60:
                dark += 1
    if bright < 0.7 * n or dark < 0.005 * n:
        return False
    for y in (19, 20):
        line = px(y)
        if sum(1 for r, g, b in line if r < 60 and g < 60 and b < 60) >= 0.9 * len(line):
            return True
    return False


class Lab:
    """One QEMU run with an in-process presenter."""

    def __init__(self, a, run: Path):
        self.a = a
        self.run = run.resolve()
        self.run.mkdir(parents=True, exist_ok=True)
        if not a.disk_in_place and not (self.run / 'disk.img').exists():
            subprocess.run(['cp', '--sparse=always', str(a.disk), str(self.run / 'disk.img')], check=True)
        if not (self.run / 'pram.img').exists():
            shutil.copyfile(a.pram_seed, self.run / 'pram.img')
        self.argv = rq.build_argv(a, self.run)
        (self.run / 'argv.json').write_text(json.dumps(self.argv, indent=2) + '\n')
        self.p = LabPresenter(self.run / 'display.sock', self.run / 'stub.jsonl', None)
        threading.Thread(target=self.p.accept_loop, daemon=True).start()
        threading.Thread(target=control_server, args=(self.p, self.run / 'stub.ctl'), daemon=True).start()
        self.events: list[dict] = []
        self.marks: dict[str, float] = {}
        self.cpu_at: dict[str, float] = {}
        self.proc = None
        self.t0 = None

    # -- process ---------------------------------------------------------
    def start(self):
        out = (self.run / 'qemu.out').open('ab')
        self.t0 = time.monotonic()
        self.proc = subprocess.Popen(self.argv, stdout=out, stderr=subprocess.STDOUT,
                                     env={**os.environ, 'RETROBOOT_SESSION': self.run.name})
        (self.run / 'qemu.pid').write_text(f'{self.proc.pid}\n')
        threading.Thread(target=self._events, daemon=True).start()
        threading.Thread(target=self._watch, daemon=True).start()

    def _events(self):
        sock = self.run / 'qmp-events.sock'
        for _ in range(300):
            if sock.exists():
                try:
                    q = QMP(sock, timeout=None)
                    break
                except OSError:
                    pass
            time.sleep(0.05)
        else:
            return
        with (self.run / 'qmp-events.jsonl').open('a', buffering=1) as log:
            while True:
                try:
                    e = q._read()
                except (ConnectionError, OSError, ValueError):
                    return
                if 'event' in e:
                    e['host_mono'] = time.monotonic()
                    self.events.append(e)
                    log.write(json.dumps(e) + '\n')

    def since(self, mono: float | None = None) -> float:
        return round((mono if mono is not None else time.monotonic()) - self.t0, 3)

    def mark(self, name: str, mono: float | None = None):
        if name not in self.marks:
            self.marks[name] = self.since(mono)
            try:
                self.cpu_at[name] = round(rq.proc_total(self.proc.pid), 2)
            except (OSError, AttributeError):
                pass
            self.p.record('mark', name=name, t=self.marks[name])

    def _watch(self, interval: float = 0.05):
        """Milestones from the shared frame, against the launch time."""
        seen = -1
        while self.proc.poll() is None:
            time.sleep(interval)
            with self.p.lock:
                n = self.p.damage_msgs + self.p.surfaces
                first = self.p.first_damage_mono
            if first and 'first_frame' not in self.marks:
                self.mark('first_frame', first)
            if n == seen:
                continue
            seen = n
            try:
                a = self.analyze()
            except RuntimeError:
                continue
            now = time.monotonic()
            if menubar(*self.frame()):
                if 'desktop' not in self.marks:
                    self.shot('milestone-desktop.png')
                self.mark('desktop', now)
            if a['finder']:
                self.mark('finder', now)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self, timeout: float = 10):
        if self.alive():
            self.proc.terminate()
            try:
                self.proc.wait(timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.p.stopping = True

    # -- frames ----------------------------------------------------------
    def frame(self):
        return self.p.frame_raw()

    def analyze(self) -> dict:
        return analyze_frame(*self.frame())

    def region(self, x0: int, y0: int, x1: int, y1: int) -> bytes:
        w, h, stride, raw = self.frame()
        x1, y1 = min(x1, w), min(y1, h)
        return b''.join(raw[y * stride + x0 * 4:y * stride + x1 * 4] for y in range(y0, y1))

    def shot(self, name: str):
        w, h, stride, raw = self.frame()
        _save_frame(w, h, xrgb_to_rgb(raw, w, h, stride), self.run / name)

    def last_damage(self) -> float | None:
        """The last drawing below the menu bar (the clock's flashing colon excluded)."""
        with self.p.lock:
            return self.p.last_body_damage or self.p.first_damage_mono

    def wait(self, predicate, timeout: float, poll: float = 0.05, while_alive: bool = True):
        deadline = time.monotonic() + timeout
        while True:
            v = predicate()
            if v:
                return v
            if time.monotonic() >= deadline or (while_alive and not self.alive()):
                return predicate() or None
            time.sleep(poll)

    def wait_quiet(self, quiet: float, timeout: float) -> float | None:
        """Wait until nothing was drawn for `quiet` seconds; returns the last draw."""
        def ok():
            last = self.last_damage()
            return last if last is not None and time.monotonic() - last >= quiet else None
        return self.wait(ok, timeout, 0.1)

    # -- input -----------------------------------------------------------
    def tap(self, *keys, hold: float = 0.08):
        self.p.tap(list(keys), hold, 0.06)

    def click(self, x: int, y: int):
        self.p.click(x, y)

    def apple_menu_probe(self, timeout: float = 5) -> dict | None:
        """Open the Apple menu and close it again; the guest drew both.

        Press on the Apple menu's title, hold, drag off the menu and release
        there (no item chosen; Mac OS 8 keeps a menu open only after a quick
        click), then click the desktop if a menu is still showing."""
        box = (4, 40, 240, 200)   # below the cursor at the Apple menu's title
        w, h, _, _ = self.frame()
        away = (w * 3 // 4, h * 3 // 4)
        self.p.pointer(20, 10)
        time.sleep(0.3)
        before = self.region(*box)
        t = time.monotonic()
        self.p.button('left', True)
        opened = self.wait(lambda: self.region(*box) != before, timeout, 0.005)
        t_open = time.monotonic()
        time.sleep(0.3)
        self.p.pointer(*away)
        time.sleep(0.2)
        self.p.button('left', False)
        closed = self.wait(lambda: self.region(*box) == before, 2, 0.02)
        if not closed:
            self.click(*away)
            closed = self.wait(lambda: self.region(*box) == before, timeout, 0.02)
        if not opened or not closed:
            return None
        return {'open_ms': round((t_open - t) * 1000, 1)}

    def wait_responsive(self, timeout: float, probe_timeout: float = 2.0) -> float | None:
        """Probe the Apple menu until it opens and closes; records each attempt."""
        self.attempts = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self.alive():
            t = self.since()
            r = self.apple_menu_probe(probe_timeout)
            self.attempts.append({'t': t, 'ok': bool(r), **(r or {})})
            if r:
                self.mark('responsive', time.monotonic())
                return self.marks['responsive']
            time.sleep(0.5)
        return None

    def open_path(self, names, wait: float = 3.0):
        """Open an item through the Finder by type-select: click the desktop,
        then for each name type its first characters and Command-O."""
        w, h, _, _ = self.frame()
        self.click(w // 2, h - 12)
        time.sleep(0.8)
        for name in names:
            self.p.type_text(name[:8])
            time.sleep(0.8)
            self.tap('meta_l', 'o')
            time.sleep(wait)

    def dismiss_dialogs(self, tries: int = 3):
        """Return on a startup alert (improper Shut Down, extension conflict)."""
        for _ in range(tries):
            self.tap('ret')
            time.sleep(0.5)

    def power(self, answer: str, timeout: float = 90) -> dict:
        """Mac OS's own Shut Down or Restart: the ADB power key, then Return or R."""
        t = time.monotonic()
        self.tap('power', hold=0.12)
        time.sleep(1.5)
        self.shot(f'power-dialog-{answer}.png')
        self.tap('ret' if answer == 'shutdown' else 'r')
        t1 = time.monotonic()
        e = self.wait(lambda: next((e for e in self.events if e['event'] == 'SHUTDOWN' and e['host_mono'] >= t), None),
                      timeout, 0.05, while_alive=False)
        exit_code = None
        if e:
            try:
                exit_code = self.proc.wait(15)
            except subprocess.TimeoutExpired:
                pass
        return {'event': e and {'reason': e['data'].get('reason'), 'guest': e['data'].get('guest')},
                'after_answer_s': round(e['host_mono'] - t1, 3) if e else None, 'exit_code': exit_code}

    def cpu(self, seconds: float) -> dict:
        return rq.cpu_window(self.proc.pid, seconds)


def boot_and_settle(lab: Lab, a) -> dict:
    """Launch; desktop; probe the Apple menu from then on (responsive); quiet."""
    lab.start()
    desktop = lab.wait(lambda: 'desktop' in lab.marks, a.timeout)
    out = {'desktop_s': lab.marks.get('desktop')}
    if not desktop:
        lab.shot('timeout.png')
        return out
    if a.dismiss:
        lab.dismiss_dialogs()
    out['responsive_s'] = lab.wait_responsive(a.timeout)
    out['probe_attempts'] = getattr(lab, 'attempts', [])
    quiet_last = lab.wait_quiet(a.quiet, a.timeout)
    if quiet_last:
        lab.mark('quiet')
        out['quiet_last_draw_s'] = lab.since(quiet_last)
    lab.shot('desktop.png')
    return out


def scenario_boot(a) -> dict:
    lab = Lab(a, a.run)
    res = {'scenario': 'boot', 'argv': lab.argv, 'loadavg_start': Path('/proc/loadavg').read_text().split()[:3]}
    try:
        res.update(boot_and_settle(lab, a))
        if a.idle and lab.alive():
            time.sleep(a.idle_settle)
            res['idle'] = lab.cpu(a.idle)
        if a.end in ('shutdown', 'restart') and lab.alive():
            res['power'] = lab.power(a.end)
    finally:
        res['marks'] = lab.marks
        res['cpu_s_at'] = lab.cpu_at
        res['loadavg_end'] = Path('/proc/loadavg').read_text().split()[:3]
        res['qemu_exit'] = lab.proc.poll() if lab.proc else None
        lab.stop()
    return res


def scenario_latency(a) -> dict:
    """Key-to-screen: a key that changes the Finder's selection, KEY sent -> first DAMAGE."""
    lab = Lab(a, a.run)
    res = {'scenario': 'latency', 'argv': lab.argv}
    try:
        res.update(boot_and_settle(lab, a))
        lab.click(a.desktop_x, a.desktop_y)       # empty desktop: the Finder's desktop is active
        time.sleep(1.5)
        samples = []
        for i in range(a.count):
            key = a.keys[i % len(a.keys)]
            lab.wait_quiet(0.6, 5)
            with lab.p.lock:
                before = lab.p.body_damages
            t = time.monotonic()
            lab.p.key(key, True)
            d = lab.wait(lambda: lab.p.body_damages > before and lab.p.last_body_damage, 2, 0.001)
            lab.p.key(key, False)
            samples.append(round((d - t) * 1000, 1) if d else None)
            time.sleep(0.3)
        ok = sorted(s for s in samples if s is not None)
        res['latency_ms'] = samples
        if ok:
            res['median_ms'] = ok[len(ok) // 2]
            res['p90_ms'] = ok[int(len(ok) * 0.9) - 1 if len(ok) > 1 else 0]
        if a.end == 'shutdown' and lab.alive():
            res['power'] = lab.power('shutdown')
    finally:
        res['marks'] = lab.marks
        lab.stop()
    return res


def qfb_vram(pid: int) -> int:
    """Address of the 32 MiB qfb-vram mapping in QEMU's address space."""
    for line in open(f'/proc/{pid}/maps'):
        f = line.split()
        lo, hi = (int(v, 16) for v in f[0].split('-'))
        if hi - lo == 32 << 20 and f[1].startswith('rw'):
            return lo
    raise RuntimeError('no 32 MiB VRAM mapping')


def scenario_pointer(a) -> dict:
    """Mac OS cursor redraws per second: move the pointer in a circle at 125 Hz
    (absolute through the virtio tablet, or relative through the ADB mouse) and
    sample the screen in qfb VRAM about every millisecond (lab/hw_pointer_cadence.py)."""
    import math
    lab = Lab(a, a.run)
    res = {'scenario': 'pointer', 'argv': lab.argv, 'mode': a.mode}
    try:
        res.update(boot_and_settle(lab, a))
        w, h, stride, _ = lab.frame()
        bpp = {1: 1, 2: 2, 4: 4, 8: 8, 16: 16, 24: 32, 32: 32}[int(a.size.split('x')[2])]
        row = ((w * bpp + 31) // 8) & ~3
        base = qfb_vram(lab.proc.pid) + 0x10000
        nbytes = row * h
        mem = os.open(f'/proc/{lab.proc.pid}/mem', os.O_RDONLY)
        cx, cy, r = w // 2, h // 2, 60
        changes = []
        done = threading.Event()

        def sample():
            prev = os.pread(mem, nbytes, base)
            while not done.is_set():
                cur = os.pread(mem, nbytes, base)
                if cur != prev:
                    changes.append(time.monotonic())
                    prev = cur
                time.sleep(0.0005)

        def move(n, step0):
            t0 = time.monotonic()
            px, py = cx + r, cy
            for i in range(n):
                ang = 2 * math.pi * (step0 + i) * a.turns / 125.0
                nx, ny = round(cx + r * math.cos(ang)), round(cy + r * math.sin(ang))
                if a.mode == 'abs':
                    lab.p.pointer(nx, ny)
                else:
                    lab.p.relative(nx - px, ny - py)
                px, py = nx, ny
                d = t0 + (i + 1) / 125 - time.monotonic()
                if d > 0:
                    time.sleep(d)
        if a.mode == 'abs':
            lab.p.pointer(cx + r, cy)
        move(125, 0)
        th = threading.Thread(target=sample)
        th.start()
        time.sleep(0.3)
        t_start = time.monotonic()
        cpu0 = rq.proc_total(lab.proc.pid)
        move(int(a.seconds * 125), 125)
        t_end = time.monotonic()
        cpu1 = rq.proc_total(lab.proc.pid)
        done.set()
        th.join()
        os.close(mem)
        frames = []
        for t in changes:
            if t_start <= t <= t_end and (not frames or t - frames[-1] > 0.004):
                frames.append(t)
        dur = t_end - t_start
        gaps = sorted(round((y - x) * 1000, 1) for x, y in zip(frames, frames[1:]))
        res.update({'window_s': round(dur, 2), 'pointer_frames': len(frames),
                    'pointer_fps': round(len(frames) / dur, 1),
                    'gap_ms': {'min': gaps[0], 'median': gaps[len(gaps) // 2], 'max': gaps[-1]} if gaps else None,
                    'cpu_pct_during': round((cpu1 - cpu0) / dur * 100, 1)})
        if a.end == 'shutdown' and lab.alive():
            res['power'] = lab.power('shutdown')
    finally:
        res['marks'] = lab.marks
        lab.stop()
    return res


def wav_profile(path: Path, window: float = 0.25) -> dict:
    """Peak deviation from silence per window of a QEMU wav capture.

    Reads the format from the header and the samples to the end of the file
    (QEMU writes the RIFF and data sizes only when its audio is torn down,
    which a guest Shut Down skips)."""
    data = path.read_bytes()
    ch, rate = struct.unpack_from('<HI', data, 22)
    bits = struct.unpack_from('<H', data, 34)[0]
    width = bits // 8
    frames = data[44:]
    step = int(rate * window) * width * ch
    peaks = []
    for i in range(0, len(frames) - step + 1, step):
        chunk = frames[i:i + step]
        if width == 1:
            peak = max((abs(b - 128) for b in chunk), default=0)
        else:
            vals = struct.unpack(f'<{len(chunk) // 2}h', chunk[:len(chunk) // 2 * 2])
            peak = max((abs(v) >> 8 for v in vals), default=0)
        peaks.append(peak)
    return {'rate': rate, 'width': width, 'channels': ch, 'window_s': window, 'peaks': peaks}


def pcap_dhcp(path: Path) -> list[dict]:
    """DHCP messages (op, type, yiaddr) in a pcap (Ethernet/IPv4/UDP 67-68)."""
    out = []
    data = path.read_bytes()
    pos = 24
    while pos + 16 <= len(data):
        ts, tus, incl, _ = struct.unpack_from('<IIII', data, pos)
        pkt = data[pos + 16:pos + 16 + incl]
        pos += 16 + incl
        if len(pkt) < 42 or pkt[12:14] != b'\x08\x00' or pkt[23] != 17:
            continue
        ihl = (pkt[14] & 0xf) * 4
        sport, dport = struct.unpack_from('>HH', pkt, 14 + ihl)
        if {sport, dport} != {67, 68}:
            continue
        bootp = pkt[14 + ihl + 8:]
        if len(bootp) < 240 or bootp[236:240] != b'\x63\x82\x53\x63':
            continue
        mtype, i = None, 240
        while i < len(bootp) and bootp[i] != 255:
            if bootp[i] == 0:
                i += 1
                continue
            if bootp[i] == 53:
                mtype = bootp[i + 2]
            i += 2 + bootp[i + 1]
        out.append({'t': ts + tus / 1e6, 'op': bootp[0], 'type': {1: 'DISCOVER', 2: 'OFFER', 3: 'REQUEST', 5: 'ACK', 6: 'NAK'}.get(mtype, mtype),
                    'yiaddr': '.'.join(str(b) for b in bootp[16:20]), 'chaddr': bootp[28:34].hex(':')})
    return out


def scenario_proof(a) -> dict:
    """The functional checks of one Mac OS start (quadra contract, LEGACY-01):
    Finder, keyboard (type-to-select), alert sound and silence, the shared
    volume (a host file duplicated by the Finder), DHCP on the SONIC, a
    resize request followed by the guest, then Shut Down or Restart."""
    share = a.run / 'share'
    share.mkdir(parents=True, exist_ok=True)
    (share / 'rbhello.txt').write_text('RetroBoot Quadra share check\n')
    a.share = share
    a.audio = 'wav'
    a.pcap = True
    www = a.run / 'www'
    www.mkdir(exist_ok=True)
    (www / 'rb.html').write_text('<html><body><h1>RetroBoot Quadra network check</h1></body></html>\n')
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    http_port = sock.getsockname()[1]
    sock.close()
    http = subprocess.Popen([sys.executable, '-m', 'http.server', str(http_port), '--bind', '127.0.0.1',
                             '--directory', str(www)], stdout=subprocess.DEVNULL,
                            stderr=(a.run / 'http.log').open('w'))
    lab = Lab(a, a.run)
    res = {'scenario': 'proof', 'argv': lab.argv, 'loadavg_start': Path('/proc/loadavg').read_text().split()[:3]}
    steps = res['steps'] = {}
    try:
        res.update(boot_and_settle(lab, a))
        w, h, _, _ = lab.frame()
        # Alert sounds: open the sound list (7.5.5 Sound control panel, 8.1
        # SimpleSound); each arrow key selects the next alert sound and plays it.
        lab.open_path([a.boot_volume] + a.sound_path.split(':'))
        lab.wait_quiet(1.0, 10)
        steps['beep_t'] = lab.since()
        for xy in a.sound_items.split(';'):      # click a sound in the list: Mac OS plays it
            lab.click(*(int(v) for v in xy.split(',')))
            time.sleep(1.5)
        lab.shot('proof-sound.png')
        lab.tap('meta_l', 'w')
        time.sleep(1)
        lab.tap('meta_l', 'q') if a.sound_path.endswith('SimpleSound') else None
        time.sleep(1)
        lab.tap('meta_l', 'alt', 'w')
        time.sleep(1)
        # Shared volume: type-to-select on the desktop, open, select the host file, duplicate.
        lab.click(w // 2, h - 12)
        time.sleep(0.8)
        lab.p.type_text('RetroBoot')
        time.sleep(0.8)
        lab.tap('meta_l', 'o')
        time.sleep(3)
        lab.p.type_text('rbhello')
        time.sleep(0.8)
        lab.tap('meta_l', 'd')
        copy = lab.wait(lambda: [f.name for f in share.iterdir() if f.name.startswith('rbhello') and f.name != 'rbhello.txt'
                                 and not f.name.endswith(('.idump', '.rdump'))], 20, 0.2)
        lab.wait_quiet(1.0, 10)                  # the Finder finishes the copy
        lab.shot('proof-share.png')
        steps['share_duplicate'] = copy
        if copy:
            steps['share_duplicate_content_ok'] = (share / copy[0]).read_bytes() == (share / 'rbhello.txt').read_bytes()
        lab.tap('meta_l', 'w')
        time.sleep(1)
        if a.net_app != '-':
            # Network: open the TCP/IP control panel (Open Transport loads TCP/IP and,
            # set to DHCP, asks QEMU's slirp for an address on the dp83932).
            lab.click(w // 2, h - 12)
            time.sleep(0.8)
            lab.open_path([a.boot_volume, 'System Folder', 'Control Panels', 'TCP/IP'])
            lab.wait_quiet(1.0, 10)
            lab.shot('proof-tcpip.png')
            lab.tap('meta_l', 'w')                 # TCP/IP
            time.sleep(1)
            lab.tap('meta_l', 'alt', 'w')          # every Finder window
            time.sleep(1.5)
            # TCP/IP loads when a program opens it: start Netscape Navigator 3.
            lab.open_path([a.boot_volume] + a.net_app.split(':'))
            # Open Location (QEMU's host, slirp 10.0.2.2); on a busy host Navigator may
            # still be starting (or resolving its home page), so try up to three times.
            got = None
            for attempt in range(3):
                time.sleep(6 if attempt == 0 else 1)
                lab.tap('meta_l', 'l')
                time.sleep(1.5)
                lab.p.type_text(f'http://10.0.2.2:{http_port}/rb.html\n')
                got = lab.wait(lambda: 'GET /rb.html' in (a.run / 'http.log').read_text(), 25, 0.5)
                if got:
                    break
                lab.shot(f'proof-netscape-try{attempt + 1}.png')
                lab.tap('esc')                     # an Open Location dialog left open
            steps['http_attempts'] = attempt + 1
            steps['http_get'] = bool(got)
            pcap = a.run / 'net.pcap'
            acked = lab.wait(lambda: [m for m in pcap_dhcp(pcap) if m['type'] == 'ACK'] if pcap.exists() else None, 60, 0.5)
            steps['dhcp_ack'] = bool(acked)
            time.sleep(5)
            lab.shot('proof-netscape.png')
            lab.tap('meta_l', 'q')
            time.sleep(4)
            lab.tap('ret')                         # a "quit?" or error alert, if any
            time.sleep(2)
            lab.tap('meta_l', 'alt', 'w')
            time.sleep(1)
        # Resize: ask for 1024x768, the guest's driver switches through the Display Manager.
        t = time.monotonic()
        lab.p.send(19, struct.pack('=II', 1024, 768))
        got = lab.wait(lambda: lab.p.surface and (lab.p.surface['width'], lab.p.surface['height']) == (1024, 768), 30, 0.1)
        steps['resize_1024x768_s'] = round(time.monotonic() - t, 2) if got else None
        lab.wait_quiet(1.0, 15)
        lab.shot('proof-resized.png')
        t = time.monotonic()
        lab.p.send(19, struct.pack('=II', 800, 600))
        got = lab.wait(lambda: lab.p.surface and (lab.p.surface['width'], lab.p.surface['height']) == (800, 600), 30, 0.1)
        steps['resize_back_800x600_s'] = round(time.monotonic() - t, 2) if got else None
        steps['resize_events'] = [e for e in lab.p.events if e.get('event') == 'resize-request']
        lab.wait_quiet(1.0, 15)
        steps['idle_5s'] = lab.cpu(5)
        res['power'] = lab.power(a.end if a.end != 'kill' else 'shutdown')
    finally:
        res['marks'] = lab.marks
        lab.stop()
        http.terminate()
    wav = a.run / 'guest-audio.wav'
    if wav.exists():
        prof = wav_profile(wav)
        pk = prof['peaks']
        win = prof['window_s']
        res['audio'] = {'rate': prof['rate'], 'width': prof['width'], 'channels': prof['channels'],
                        'seconds': round(len(pk) * win, 1),
                        'chime_peak_first_3s': max(pk[:int(3 / win)], default=0),
                        'beep_peak': max(pk[int((steps.get('beep_t', 0)) / win):int((steps.get('beep_t', 0) + 4) / win)], default=0)
                        if 'beep_t' in steps else None,
                        'loud_windows': [round(i * win, 2) for i, v in enumerate(pk) if v > 8]}
        tail_start = int((res['marks'].get('quiet', 0)) / win)
    pcap = a.run / 'net.pcap'
    if pcap.exists():
        res['dhcp'] = pcap_dhcp(pcap)
    return res


QFB_SLOT_PRAM = 0x46 + (0xD - 0x9) * 8
DEPTH_MODES = {1: 0x80, 2: 0x81, 4: 0x82, 8: 0x83, 16: 0x84, 32: 0x85}


def scenario_seed(a) -> dict:
    """Make the PRAM seed: boot an all-zero PRAM without the virtio card (the
    card's declaration ROM hangs a cold PRAM), Shut Down, then boot that PRAM
    with the full contract argv and Shut Down again; the PRAM after the second
    boot is the seed.  Only Mac OS and the Apple ROM write it, as on a real Mac's
    first two starts; it holds settings, no code."""
    res = {'scenario': 'seed', 'stages': []}
    pram = a.run / 'pram-stage.img'
    a.run.mkdir(parents=True, exist_ok=True)
    pram.write_bytes(bytes(256))
    for stage, virtio in (('cold', False), ('virtio', True)):
        a.no_virtio = not virtio
        run = a.run / stage
        run.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(pram, run / 'pram.img')
        lab = Lab(a, run)
        try:
            out = boot_and_settle(lab, a)
            out['power'] = lab.power('shutdown') if lab.alive() else None
        finally:
            out['marks'] = lab.marks
            lab.stop()
        out['stage'] = stage
        out['pram_sha256'] = hashlib.sha256((run / 'pram.img').read_bytes()).hexdigest()
        res['stages'].append(out)
        ok = out['power'] and out['power']['event'] and out['power']['event']['reason'] == 'guest-shutdown'
        if not ok:
            raise SystemExit(f'seed stage {stage} did not end with a guest Shut Down: {out}')
        shutil.copyfile(run / 'pram.img', pram)
    data = bytearray(pram.read_bytes())
    if data[0x0C:0x10] != b'NuMc':
        raise SystemExit('PRAM has no valid signature')
    # The card's slot parameter RAM: 8 bytes per NuBus slot from 0x46 (slot 9),
    # board ID then the video mode the ROM opens the card in (0x80 1 bit ...
    # 0x85 millions).  nubus-qfb follows the virtio card, so it is in slot D.
    rec = QFB_SLOT_PRAM
    if data[rec:rec + 2] != b'\x95\x45':
        raise SystemExit(f'slot D parameter RAM is not the qfb card: {data[rec:rec + 8].hex()}')
    data[rec + 2] = DEPTH_MODES[a.seed_depth]
    data = bytes(data)
    a.out.write_bytes(data)
    res['seed'] = {'path': str(a.out), 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
                   'nonzero_offsets': [i for i, b in enumerate(data) if b]}
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='scenario', required=True)
    for name in ('boot', 'latency', 'pointer', 'seed', 'proof'):
        p = sub.add_parser(name)
        rq.add_launch_options(p)
        p.add_argument('--timeout', type=float, default=180)
        p.add_argument('--quiet', type=float, default=2.0)
        p.add_argument('--dismiss', action='store_true', help='press Return a few times at the desktop')
        p.add_argument('--end', choices=['shutdown', 'restart', 'kill'], default='shutdown')
        if name == 'boot':
            p.add_argument('--idle', type=float, default=0)
            p.add_argument('--idle-settle', type=float, default=5)
        if name == 'latency':
            p.add_argument('--count', type=int, default=20)
            p.add_argument('--keys', nargs='+', default=['m', 't'])
            p.add_argument('--desktop-x', type=int, default=400)
            p.add_argument('--desktop-y', type=int, default=580)
        if name == 'proof':
            p.add_argument('--boot-volume', default='Mac OS 8.1', help='the startup disk name (type-select)')
            p.add_argument('--sound-path', default='System Folder:Apple Menu Items:SimpleSound',
                           help="7.5.5: 'System Folder:Control Panels:Sound'")
            p.add_argument('--net-app', default='Applications:Netscape:Netscape',
                           help="a TCP/IP program to open by type-select ('-': no network step)")
            p.add_argument('--sound-items', default='52,107;56,139',
                           help="list positions to click (8.1 SimpleSound: Quack, Sosumi; 7.5.5 Sound: '258,147;269,179')")
        if name == 'pointer':
            p.add_argument('--mode', choices=['abs', 'rel'], default='abs')
            p.add_argument('--seconds', type=float, default=4)
            p.add_argument('--turns', type=float, default=2)
        if name == 'seed':
            p.add_argument('--out', type=Path, required=True)
            p.add_argument('--seed-depth', type=int, choices=sorted(DEPTH_MODES), default=8,
                           help="Mac OS's first-start depth, stored in the card's slot PRAM")
    a = ap.parse_args()
    a.run = a.run.resolve()
    if a.scenario == 'seed':
        a.disk_in_place = False
        res = scenario_seed(a)
    else:
        res = {'boot': scenario_boot, 'latency': scenario_latency, 'pointer': scenario_pointer,
               'proof': scenario_proof}[a.scenario](a)
    (a.run / 'result.json').write_text(json.dumps(res, indent=2) + '\n')
    print(json.dumps({k: v for k, v in res.items() if k != 'argv'}, indent=2))


if __name__ == '__main__':
    main()
