#!/usr/bin/env python3
"""Pre-gray hang lab (emulator/reports/pregray-hang-20261011.md).

Cold-boots sparse copies of a Mac OS 9 disk with hostd's argv
(hostd/retroboot/machines.py build_argv: -display retroboot behind a
presenter stub, two QMP sockets, the driver variant ahead of the package,
pio-status-drain, -rtc base=localtime, -action reboot=shutdown, CD drives,
Sungem NAT, ndrvloader, virtio tablet, 9p share) until the Mac OS startup
gray appears, then kills QEMU.  A boot that shows no gray within --hang-after
seconds is a hang: the tool samples the vCPU while it runs, stops the VM and
captures registers, the Cuda VIA (info via), OpenPIC source registers, guest
code around the PC (QEMU's own disassembler), logical memory ranges and
optionally all of RAM, plus the last lines of a trace ring buffer
(--trace-events, QEMU's log backend written into a FIFO that this tool reads
into a deque, so nothing big reaches the disk).  Then it resumes the guest and
watches it for --observe seconds more (does it ever reach the gray?) and
checks whether the guest wrote its disk.

    rbd_pregray.py boot --out DIR --seed IMG [options] [-- QEMU ARGS]
    rbd_pregray.py batch --root ROOT NAME N WORKERS --arm 'A=opts' [--arm 'B=opts'] [-- common opts]
    rbd_pregray.py summarize RUNDIR...

Boot k of a batch uses arm k mod len(arms) (interleaved arms share the same
load).  Evidence per boot: DIR/result.json, argv.json, qemu.out, serial.log,
and for hangs hang/ (registers, via, disassembly, memory, trace tail).
Lab tool (Linux builder, Python standard library only).
"""
from __future__ import annotations

import argparse
import collections
import gzip
import hashlib
import json
import os
import re
import shlex
import shutil
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

PKG = os.environ.get('RB_LAB_PKG', '/srv/retroboot/wave2/artifacts/qemu/linux-ppc-r4')
NDRV = os.environ.get('RB_LAB_NDRV', '/srv/retroboot/wave2/artifacts/ndrv')
OPENPIC = 0x80040000          # MacIO + 0x40000 on mac99 (OpenBIOS assignment)
CUDA_SRC = 25                 # OpenPIC source of the Cuda VIA on mac99


# --------------------------------------------------------------------------
class QMP:
    def __init__(self, path: str, timeout: float = 10):
        deadline = time.monotonic() + timeout
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
        self.lock = threading.Lock()
        self.events: list[dict] = []
        self.cmd('qmp_capabilities')

    def cmd(self, name, **args):
        with self.lock:
            self.f.write(json.dumps({'execute': name, 'arguments': args}) + '\n')
            self.f.flush()
            while True:
                line = self.f.readline()
                if not line:
                    raise EOFError(name)
                msg = json.loads(line)
                if 'event' in msg:
                    msg['mono'] = time.monotonic()
                    self.events.append(msg)
                    continue
                return msg

    def hmp(self, line: str) -> str:
        r = self.cmd('human-monitor-command', **{'command-line': line})
        return r.get('return', json.dumps(r.get('error')))

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class TraceRing:
    """QEMU log-backend output through a FIFO into a bounded deque."""

    def __init__(self, path: Path, keep: int, drop: str | None = None):
        self.path = path
        os.mkfifo(path)
        self.ring: collections.deque[str] = collections.deque(maxlen=keep)
        self.lines = 0
        self.dropped = 0
        self.drop = re.compile(drop) if drop else None
        self.t = threading.Thread(target=self._reader, daemon=True)
        self.t.start()

    def _reader(self):
        with open(self.path, 'r', errors='replace', buffering=1 << 20) as f:
            for line in f:
                self.lines += 1
                if self.drop is not None and self.drop.search(line):
                    self.dropped += 1
                    continue
                self.ring.append(line)

    def dump(self, out: Path):
        out.write_text(''.join(list(self.ring)))


def data_extents_digest(path: Path) -> dict:
    """SHA-256 over (offset, bytes) of every data extent of a sparse file."""
    h = hashlib.sha256()
    total = 0
    with open(path, 'rb') as f:
        fd = f.fileno()
        size = os.fstat(fd).st_size
        off = 0
        while off < size:
            try:
                d = os.lseek(fd, off, os.SEEK_DATA)
            except OSError:
                break
            e = os.lseek(fd, d, os.SEEK_HOLE)
            f.seek(d)
            left = e - d
            h.update(struct.pack('>QQ', d, left))
            while left:
                chunk = f.read(min(left, 1 << 22))
                if not chunk:
                    break
                h.update(chunk)
                left -= len(chunk)
            total += e - d
            off = e
    return {'sha256': h.hexdigest(), 'data_bytes': total}


# --------------------------------------------------------------------------
def build_argv(a, out: Path, disk: Path) -> list[str]:
    """hostd/retroboot/machines.py build_argv for a disk boot, with toggles."""
    bios = f'{a.pkg}/share/qemu'
    data_dirs = ['-L', a.ndrv] if a.ndrv else []
    data_dirs += ['-L', bios]
    w, h = a.size
    args = [f'{a.pkg}/bin/qemu-system-ppc', '-accel', 'tcg,tb-size=512', '-M', 'mac99,via=cuda,audiodev=snd0',
            '-cpu', a.cpu, '-m', str(a.mem), *data_dirs,
            '-vga', 'std', '-global', 'VGA.vgamem_mb=64', '-global', 'VGA.host-resize=on',
            '-global', 'VGA.packed-lowbpp=on', '-global', 'VGA.hardware-cursor=off',
            '-global', 'VGA.untracked-vram=off',
            '-global', 'macio-ide.dma-completion-delay-ns=0']
    if a.drain:
        args += ['-global', 'macio-ide.pio-status-drain=on']
    args += ['-prom-env', 'output-device=ttya', '-g', f'{w}x{h}x32',
             '-name', 'Mac OS 9', '-audiodev', 'none,id=snd0', '-action', 'reboot=shutdown',
             '-rtc', a.rtc,
             '-qmp', f'unix:{out}/qmp-control.sock,server=on,wait=off',
             '-qmp', f'unix:{out}/qmp-events.sock,server=on,wait=off',
             '-serial', f'file:{out}/serial.log',
             '-drive', f'file={disk},format=raw,media=disk,index=0',
             '-display', f'retroboot,socket={out}/display.sock']
    if a.speed == 'accelerated':
        args += ['-icount', 'shift=4,sleep=off', '-global', 'VGA.classicmac-boot-handoff=on']
    if a.cds:
        args += ['-drive', 'if=ide,index=3,media=cdrom,id=cd0,readonly=on',
                 '-drive', 'if=ide,index=2,media=cdrom,id=tools0,readonly=on']
    args += ['-nic', 'user,model=sungem' if a.net else 'none']
    if a.ndrvloader:
        args += ['-device', f'loader,addr=0x4000000,file={bios}/ndrvloader']
    args += ['-prom-env', 'boot-command=init-program go']
    if a.tablet:
        args += ['-device', 'virtio-tablet-pci']
    if a.share:
        args += ['-device', 'virtio-9p-pci,fsdev=share0,mount_tag=RetroBoot Share',
                 '-fsdev', f'local,id=share0,security_model=none,path={a.share}']
    if a.trace_events:
        args += ['-trace', f'events={a.trace_events}', '-D', f'{out}/trace.fifo', '-msg', 'timestamp=on']
    if a.d:
        args += ['-d', a.d]
        if not a.trace_events:
            args += ['-D', f'{out}/trace.fifo', '-msg', 'timestamp=on']
    args += a.extra
    return args


def parse_regs(text: str) -> dict:
    r = {}
    m = re.search(r'NIP ([0-9a-f]+)\s+LR ([0-9a-f]+) CTR ([0-9a-f]+)', text)
    if m:
        r['nip'], r['lr'], r['ctr'] = m.groups()
    m = re.search(r'MSR ([0-9a-f]+)', text)
    if m:
        r['msr'] = m.group(1)
    m = re.search(r'TB ([0-9a-f]+) (\d+) DECR (\d+)', text)
    if m:
        r['tb'] = int(m.group(2))
        r['decr'] = int(m.group(3))
    m = re.search(r'SRR0 ([0-9a-f]+)\s+SRR1 ([0-9a-f]+)', text)
    if m:
        r['srr0'], r['srr1'] = m.groups()
    m = re.search(r'DAR ([0-9a-f]+)\s+DSISR ([0-9a-f]+)', text)
    if m:
        r['dar'], r['dsisr'] = m.groups()
    gprs = re.findall(r'GPR(\d\d) ((?:[0-9a-f]{16} ?){4})', text)
    g = {}
    for base, vals in gprs:
        for i, v in enumerate(vals.split()):
            g[int(base) + i] = v[8:]
    if g:
        r['gpr'] = [g.get(i) for i in range(32)]
    return r


def classify(regs: dict, running: dict | None = None) -> str:
    """Where the vCPU loops: by the most frequent running NIP (the stopped NIP
    can be an exception vector), else by the stopped registers."""
    nip = None
    if running:
        top = max(running.items(), key=lambda kv: kv[1])[0]
        if top:
            nip = int(top, 16)
    if nip is None:
        nip = int(regs.get('nip', '0'), 16)
    if nip >= 0xfff00000:
        return 'openbios'
    if 0x68000000 <= nip < 0x68200000:
        return 'emulator-68k'
    if 0x00f00000 <= nip < 0x01000000:
        return 'nanokernel'
    return 'other'


class Boot:
    def __init__(self, a):
        self.a = a
        self.out = Path(a.out).resolve()
        self.out.mkdir(parents=True, exist_ok=True)
        self.res: dict = {'arm': a.arm, 'out': str(self.out)}
        self.proc = None

    def kill(self):
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                pass

    def run(self) -> dict:
        a, out, res = self.a, self.out, self.res
        disk = out / 'disk.img'
        subprocess.run(['cp', '--sparse=always', a.seed, str(disk)], check=True)
        os.chmod(disk, 0o644)
        if a.disk_check:
            res['disk_before'] = data_extents_digest(disk)
        for s in ('qmp-control.sock', 'qmp-events.sock', 'display.sock', 'trace.fifo'):
            try:
                (out / s).unlink()
            except FileNotFoundError:
                pass
        p = stub.Presenter(out / 'display.sock', None)
        threading.Thread(target=p.accept_loop, daemon=True).start()
        ring = TraceRing(out / 'trace.fifo', a.trace_keep, a.trace_drop) if (a.trace_events or a.d) else None
        argv = build_argv(a, out, disk)
        (out / 'argv.json').write_text(json.dumps(argv, indent=1))
        env = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'RETROBOOT_SESSION': 'pregray', 'HOME': '/root',
               'LANG': 'C.UTF-8', 'TZ': ':' + a.tz}
        for kv in a.env:
            k, _, v = kv.partition('=')
            env[k] = v
        res['load1_start'] = round(os.getloadavg()[0], 2)
        res['qemu_procs_start'] = sum(1 for c in Path('/proc').glob('[0-9]*/comm')
                                      if _read(c) == 'qemu-system-ppc')
        t0 = time.monotonic()
        res['t0_wall'] = round(time.time(), 3)
        self.proc = subprocess.Popen(argv, stdout=open(out / 'qemu.out', 'w'), stderr=subprocess.STDOUT,
                                     env=env, cwd=str(out), start_new_session=True)
        try:
            ctl = QMP(str(out / 'qmp-control.sock'))
            evq = QMP(str(out / 'qmp-events.sock'))
        except Exception as exc:  # noqa: BLE001
            res['outcome'] = 'qmp-error'
            res['error'] = str(exc)
            self.kill()
            return self.finish(disk, ring)
        hello = p.wait_for(lambda: p.hello, 20)
        if hello:
            res['hello_s'] = round(hello['mono'] - t0, 3)
            try:
                p.send(stub.REFRESH_RATE, struct.pack('=I', 16))
            except (RuntimeError, OSError):
                pass
        nonblack = gray = None
        seen = -1
        outcome = None
        while True:
            now = time.monotonic()
            el = now - t0
            if self.proc.poll() is not None:
                outcome = 'exited'
                break
            with p.lock:
                msgs = p.damage_msgs + p.surfaces
            if msgs != seen:
                seen = msgs
                try:
                    w, h, stride, raw = p.frame_raw()
                    an = stub.analyze_frame(w, h, stride, raw)
                    if nonblack is None and an['nonblack'] >= 0.25:
                        nonblack = el
                        res['nonblack_s'] = round(el, 3)
                    if an['gray']:
                        gray = el
                        res['gray_s'] = round(el, 3)
                        res['gray_size'] = [w, h]
                        outcome = 'gray'
                        break
                except RuntimeError:
                    pass
            if el >= a.hang_after:
                outcome = 'hang'
                break
            time.sleep(0.05)
        res['outcome'] = outcome
        res['surfaces'] = p.surfaces
        if outcome == 'hang':
            try:
                self.capture(ctl, p, t0, ring)
            except Exception as exc:  # noqa: BLE001
                res['capture_error'] = repr(exc)
            # resume and watch: does it ever reach the gray on its own?
            if a.observe > 0 and self.proc.poll() is None:
                try:
                    ctl.cmd('cont')
                except Exception:  # noqa: BLE001
                    pass
                end = time.monotonic() + a.observe
                while time.monotonic() < end and self.proc.poll() is None:
                    try:
                        w, h, stride, raw = p.frame_raw()
                        if stub.analyze_frame(w, h, stride, raw)['gray']:
                            res['late_gray_s'] = round(time.monotonic() - t0, 2)
                            break
                    except RuntimeError:
                        pass
                    time.sleep(0.5)
                try:
                    res['regs_after_observe'] = parse_regs(ctl.hmp('info registers'))
                except Exception:  # noqa: BLE001
                    pass
        try:
            res['shutdown_events'] = [e.get('data') for e in evq.events if e.get('event') == 'SHUTDOWN']
        except Exception:  # noqa: BLE001
            pass
        ctl.close()
        evq.close()
        self.kill()
        p.stopping = True
        try:
            p.listener.close()
        except OSError:
            pass
        return self.finish(disk, ring)

    def capture(self, ctl: QMP, p, t0: float, ring):
        a, res = self.a, self.res
        hd = self.out / 'hang'
        hd.mkdir(exist_ok=True)
        # running samples (where does the loop live?)
        samples = []
        for _ in range(a.samples):
            samples.append(ctl.hmp('info registers'))
            time.sleep(a.sample_gap)
        (hd / 'registers-running.txt').write_text('\n=====\n'.join(samples))
        pcs = collections.Counter(parse_regs(s).get('nip') for s in samples)
        lrs = collections.Counter(parse_regs(s).get('lr') for s in samples)
        res['running_nips'] = dict(pcs.most_common(8))
        res['running_lrs'] = dict(lrs.most_common(4))
        ctl.cmd('stop')
        res['stopped_s'] = round(time.monotonic() - t0, 2)
        try:
            w, h, rgb = p.frame()
            (hd / 'screen.png').write_bytes(stub.png_bytes(w, h, rgb))
        except (RuntimeError, OSError):
            pass
        regs_txt = ctl.hmp('info registers')
        (hd / 'registers.txt').write_text(regs_txt)
        regs = parse_regs(regs_txt)
        res['regs'] = regs
        res['kind'] = classify(regs, res.get('running_nips'))
        dump = {}
        for cmd in ('info via', 'info cpus', 'info tlb', 'info irq', 'info mtree -f', 'info qtree'):
            try:
                dump[cmd] = ctl.hmp(cmd)
            except Exception as exc:  # noqa: BLE001
                dump[cmd] = repr(exc)
        # OpenPIC (little-endian registers): every source's IVPR (mask bit 31,
        # activity bit 30, priority, vector) and IDR, plus CPU0's task priority;
        # all side-effect-free reads (never IACK).
        def le(word):
            v = int(word, 16)
            return int.from_bytes(v.to_bytes(4, 'big'), 'little')
        pic = []
        ivpr = {}
        for src in range(0, 64):
            base = OPENPIC + 0x10000 + src * 0x20
            words = re.findall(r'0x([0-9a-f]{8})', ctl.hmp(f'xp /5wx {base:#x}').replace('\n', ' '))
            words = [w for w in words if not w.startswith('8005')]
            if words:
                ivpr[src] = le(words[0])
                pic.append(f'src {src:2d} ivpr {le(words[0]):#010x} idr {le(words[4]) if len(words) > 4 else 0:#010x}')
        words = re.findall(r'0x([0-9a-f]{8})', ctl.hmp(f'xp /1wx {OPENPIC + 0x20080:#x}').split(':', 1)[-1])
        pic.append(f'cpu0 ctpr {le(words[0]) if words else -1:#x}')
        dump['openpic'] = '\n'.join(pic)
        (hd / 'devices.txt').write_text('\n'.join(f'## {k}\n{v}' for k, v in dump.items()))
        via = dump.get('info via', '')
        res['via'] = {k: v for k, v in re.findall(r'^\s+(IFR|IER|ORB|ACR|PCR|SR|DDRB)\s*:\s+(0x[0-9a-f]+)', via, re.M)}
        m = re.search(r'T1 .*counter=(0x[0-9a-f]+) latch=(0x[0-9a-f]+)\s+load_time\(ns\)=(\d+) next_irq_time\(ns\)=(\d+)', via)
        if m:
            res['via_t1'] = m.groups()
        m = re.search(r'T2 .*counter=(0x[0-9a-f]+) latch=(0x[0-9a-f]+)\s+load_time\(ns\)=(\d+) next_irq_time\(ns\)=(\d+)', via)
        if m:
            res['via_t2'] = m.groups()
        if CUDA_SRC in ivpr:
            res['openpic_cuda_ivpr'] = f'{ivpr[CUDA_SRC]:#010x}'
        res['openpic_unmasked'] = [s for s, v in ivpr.items() if not v & 0x80000000]
        # guest code around PC/LR/SRR0 (current translation; disassemble offline)
        for name in ('nip', 'lr', 'srr0'):
            v = regs.get(name)
            if v:
                addr = max(0, (int(v, 16) & ~0xff) - 0x400)
                ctl.cmd('memsave', val=addr, size=0x900, filename=str(hd / f'code-{name}-{addr:08x}.bin'))
        # logical memory ranges (current translation) and RAM
        for spec in a.memsave:
            addr, size = (int(x, 0) for x in spec.split(':'))
            path = hd / f'mem-{addr:08x}-{size:x}.bin'
            ctl.cmd('memsave', val=addr, size=size, filename=str(path))
        gp = regs.get('gpr') or []
        if len(gp) > 1 and gp[1]:
            sp = int(gp[1], 16)
            ctl.cmd('memsave', val=max(0, sp - 0x400), size=0x1000, filename=str(hd / f'stack-{sp:08x}.bin'))
        if a.vram:
            # raw VRAM (OpenBIOS's text may be drawn in colours the patched
            # palette maps to black): first 8 MB of the VGA BAR, gzip
            m = re.search(r'([0-9a-f]{16})-[0-9a-f]{16} \(prio \d+, ram\): vga.vram', dump.get('info mtree -f', ''))
            if m:
                raw = hd / 'vram.bin'
                ctl.cmd('pmemsave', val=int(m.group(1), 16), size=8 << 20, filename=str(raw))
                with open(raw, 'rb') as fi, gzip.open(str(raw) + '.gz', 'wb', compresslevel=3) as fo:
                    shutil.copyfileobj(fi, fo, 1 << 22)
                raw.unlink()
        if a.pmem:
            raw = hd / 'phys.bin'
            ctl.cmd('pmemsave', val=0, size=a.mem << 20, filename=str(raw))
            with open(raw, 'rb') as fi, gzip.open(str(raw) + '.gz', 'wb', compresslevel=3) as fo:
                shutil.copyfileobj(fi, fo, 1 << 22)
            raw.unlink()
        if ring is not None:
            # the log backend buffers its FILE: switching the log file closes
            # (and flushes) the FIFO, so the ring holds everything up to the stop
            try:
                ctl.hmp('logfile /dev/null')
            except Exception:  # noqa: BLE001
                pass
            ring.t.join(2.0)
            ring.dump(hd / 'trace-tail.log')
            res['trace_lines'] = ring.lines

    def finish(self, disk: Path, ring) -> dict:
        a, res = self.a, self.res
        if a.disk_check:
            res['disk_after'] = data_extents_digest(disk)
            res['disk_written'] = res['disk_after'] != res['disk_before']
        if ring is not None:
            ring.t.join(3.0)            # QEMU is gone: let the reader drain the FIFO
            lines = list(ring.ring)
            if a.trace_count:
                rx = re.compile(a.trace_count)
                res['trace_count'] = sum(1 for line in lines if rx.search(line))
            if a.trace_max:
                rx = re.compile(a.trace_max)
                vals = [int(m.group(1)) for m in map(rx.search, lines) if m]
                res['trace_max'] = max(vals) if vals else None
                res['trace_max_n'] = len(vals)
            if res.get('outcome') != 'hang' and a.trace_dump_all:
                ring.dump(self.out / 'trace-tail.log')
        res['exit_code'] = self.proc.returncode if self.proc else None
        keep = a.keep_disk == 'always' or (a.keep_disk == 'hang' and res.get('outcome') != 'gray')
        if not keep:
            try:
                disk.unlink()
            except FileNotFoundError:
                pass
        for s in ('qmp-control.sock', 'qmp-events.sock', 'display.sock', 'trace.fifo'):
            try:
                (self.out / s).unlink()
            except FileNotFoundError:
                pass
        (self.out / 'result.json').write_text(json.dumps(res, indent=1))
        return res


def _read(p: Path) -> str:
    try:
        return p.read_text().strip()
    except OSError:
        return ''


def boot_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog='rbd_pregray.py boot')
    ap.add_argument('--out', required=True)
    ap.add_argument('--seed', required=True, help='disk image copied sparsely for this boot (never modified)')
    ap.add_argument('--arm', default='')
    ap.add_argument('--pkg', default=PKG)
    ap.add_argument('--ndrv', default=NDRV, help="driver variant dir ('' for the package's own driver)")
    ap.add_argument('--cpu', default='7400')
    ap.add_argument('--size', default='1280x800', type=lambda s: tuple(int(x) for x in s.lower().split('x')[:2]))
    ap.add_argument('--mem', type=int, default=512)
    ap.add_argument('--speed', choices=['realtime', 'accelerated'], default='realtime')
    ap.add_argument('--rtc', default='base=localtime')
    ap.add_argument('--tz', default='UTC')
    ap.add_argument('--env', action='append', default=[], help='K=V for the QEMU environment (lab knobs)')
    ap.add_argument('--no-drain', dest='drain', action='store_false')
    ap.add_argument('--no-net', dest='net', action='store_false')
    ap.add_argument('--no-cds', dest='cds', action='store_false')
    ap.add_argument('--no-tablet', dest='tablet', action='store_false')
    ap.add_argument('--no-ndrvloader', dest='ndrvloader', action='store_false')
    ap.add_argument('--share', default=None)
    ap.add_argument('--hang-after', type=float, default=30.0)
    ap.add_argument('--observe', type=float, default=0.0, help='after a hang capture, resume and watch this long')
    ap.add_argument('--samples', type=int, default=12)
    ap.add_argument('--sample-gap', type=float, default=0.05)
    ap.add_argument('--memsave', action='append', default=None, help='ADDR:SIZE logical range (repeatable)')
    ap.add_argument('--pmem', action='store_true', help='save all RAM (gzip) on a hang')
    ap.add_argument('--vram', action='store_true', help='save the first 8 MB of VGA VRAM (gzip) on a hang')
    ap.add_argument('--trace-events', default=None)
    ap.add_argument('-d', default=None, help='QEMU -d items (into the ring)')
    ap.add_argument('--trace-keep', type=int, default=200000)
    ap.add_argument('--trace-drop', default=None, help='do not keep trace lines matching this regex (still counted)')
    ap.add_argument('--trace-dump-all', action='store_true')
    ap.add_argument('--trace-count', default=None, help='count ring lines matching this regex (result.json trace_count)')
    ap.add_argument('--trace-max', default=None, help='max of the integer group(1) of this regex over the ring (trace_max)')
    ap.add_argument('--disk-check', action='store_true')
    ap.add_argument('--keep-disk', choices=['never', 'hang', 'always'], default='never')
    ap.add_argument('extra', nargs='*')
    return ap


DEFAULT_MEMSAVE = ['0x0:0x4000', '0xffc00000:0x400000', '0x68000000:0x200000']


def cmd_boot(argv):
    a = boot_parser().parse_args(argv)
    if a.memsave is None:
        a.memsave = DEFAULT_MEMSAVE
    b = Boot(a)

    def bye(*_):
        b.kill()
        sys.exit(2)
    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)
    try:
        res = b.run()
    finally:
        b.kill()
    print(json.dumps({k: res.get(k) for k in ('arm', 'outcome', 'gray_s', 'kind', 'running_nips')}))


def cmd_batch(argv):
    split = argv.index('--') if '--' in argv else len(argv)
    ap = argparse.ArgumentParser(prog='rbd_pregray.py batch')
    ap.add_argument('--root', required=True, type=Path)
    ap.add_argument('--arm', action='append', default=[], help="NAME=boot options (shlex)")
    ap.add_argument('--stagger', type=float, default=1.0)
    ap.add_argument('--start', type=int, default=0)
    ap.add_argument('name')
    ap.add_argument('n', type=int)
    ap.add_argument('workers', type=int)
    a = ap.parse_args(argv[:split])
    def opts_extra(lst):
        if '--' in lst:
            i = lst.index('--')
            return lst[:i], lst[i + 1:]
        return lst, []
    common, common_extra = opts_extra(argv[split + 1:])
    arms = []
    for spec in a.arm or ['A=']:
        name, _, opts = spec.partition('=')
        arms.append((name,) + opts_extra(shlex.split(opts)))
    rd = a.root / 'runs' / a.name
    rd.mkdir(parents=True, exist_ok=True)
    (rd / 'batch.json').write_text(json.dumps({'arms': arms, 'common': [common, common_extra], 'n': a.n,
                                               'workers': a.workers, 'started': time.time()}, indent=1))
    lock = threading.Lock()
    todo = list(range(a.start, a.start + a.n))
    me = Path(__file__).resolve()
    stop = threading.Event()

    def worker():
        while not stop.is_set():
            with lock:
                if not todo:
                    return
                k = todo.pop(0)
            name, opts, extra = arms[k % len(arms)]
            out = rd / f'b{k:04d}'
            cmd = ([sys.executable, str(me), 'boot', '--out', str(out), '--arm', name] + common + opts +
                   ['--'] + common_extra + extra)
            with open(str(out) + '.log', 'w') as lf:
                subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT)
            try:
                r = json.loads((out / 'result.json').read_text())
                line = f"{r.get('arm')} {r.get('outcome')} gray={r.get('gray_s')} {r.get('kind', '')}"
            except (OSError, ValueError) as exc:
                line = f'error {exc}'
            print(time.strftime('%H:%M:%S'), a.name, k, line, flush=True)

    ts = [threading.Thread(target=worker) for _ in range(a.workers)]
    for t in ts:
        t.start()
        time.sleep(a.stagger)
    try:
        for t in ts:
            t.join()
    except KeyboardInterrupt:
        stop.set()


def fisher_one_sided(a_hit: int, a_n: int, b_hit: int, b_n: int) -> float:
    """P(arm A has <= a_hit hits) under the hypergeometric null (A better than B)."""
    from math import comb
    k = a_hit + b_hit
    n = a_n + b_n
    den = comb(n, k)
    return sum(comb(a_n, i) * comb(b_n, k - i) for i in range(0, a_hit + 1) if k - i <= b_n) / den


def cmd_summarize(argv):
    ap = argparse.ArgumentParser(prog='rbd_pregray.py summarize')
    ap.add_argument('--list', action='store_true', help='list every hang')
    ap.add_argument('dirs', nargs='+')
    a = ap.parse_args(argv)
    for rd in a.dirs:
        per = collections.defaultdict(collections.Counter)
        kinds = collections.defaultdict(collections.Counter)
        grays = collections.defaultdict(list)
        hangs = []
        loads = []
        for rj in sorted(Path(rd).glob('b*/result.json')):
            r = json.loads(rj.read_text())
            arm = r.get('arm')
            per[arm][r.get('outcome')] += 1
            loads.append(r.get('load1_start', 0))
            if r.get('gray_s'):
                grays[arm].append(r['gray_s'])
            if r.get('outcome') == 'hang':
                kind = classify(r.get('regs') or {}, r.get('running_nips'))
                kinds[arm][kind] += 1
                hangs.append((rj.parent.name, arm, kind, (r.get('regs') or {}).get('nip'),
                              r.get('via', {}).get('IFR'), r.get('via', {}).get('IER'),
                              r.get('openpic_cuda_ivpr'), r.get('late_gray_s'), r.get('disk_written')))
        print(rd, 'load1 at start %.1f-%.1f' % (min(loads or [0]), max(loads or [0])))
        for arm in sorted(per):
            g = sorted(grays[arm])
            n = sum(per[arm].values())
            q = (lambda f: g[min(len(g) - 1, int(f * len(g)))] if g else None)
            print(f'  arm {arm!r}: {n} boots, gray {per[arm]["gray"]}, hang {per[arm]["hang"]} {dict(kinds[arm])}, '
                  f'other {n - per[arm]["gray"] - per[arm]["hang"]}; gray s median {q(0.5)} p95 {q(0.95)} max {g[-1] if g else None}')
        arms = sorted(per)
        if len(arms) == 2:
            (x, y) = arms
            hx, nx, hy, ny = per[x]['hang'], sum(per[x].values()), per[y]['hang'], sum(per[y].values())
            lo, hi = (x, y) if hx / max(nx, 1) <= hy / max(ny, 1) else (y, x)
            p = fisher_one_sided(per[lo]['hang'], sum(per[lo].values()), per[hi]['hang'], sum(per[hi].values()))
            print(f'  one-sided Fisher ({lo!r} fewer hangs than {hi!r}): p = {p:.2g}')
        if a.list:
            for h in hangs:
                print('   hang', h)


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'boot':
        cmd_boot(sys.argv[2:])
    elif len(sys.argv) > 1 and sys.argv[1] == 'batch':
        cmd_batch(sys.argv[2:])
    elif len(sys.argv) > 1 and sys.argv[1] == 'summarize':
        cmd_summarize(sys.argv[2:])
    else:
        sys.exit(__doc__)
