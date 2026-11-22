#!/usr/bin/env python3
"""Boot-time lab: where the emulated boot (QEMU start -> Finder) spends its time.

Boots a sparse copy of a disk with hostd's argv (rbd_restart_lab.build_argv:
-display retroboot behind the presenter stub with REFRESH_RATE 16 and the
connect-time RESIZE, two QMP sockets, the driver variant ahead of the package,
pio-status-drain, -rtc, CD drives, Sungem NAT, ndrvloader, virtio tablet,
optional 9p share) and records one timeline per boot:

  * display milestones from the presenter stub: QEMU's first SURFACE, the
    OpenBIOS mode (first SURFACE of the -g size), first non-black frame,
    the Mac OS gray, the extension parade (every change of the icon row at
    the bottom of the screen, with a PNG crop of the row), the Finder menu
    bar, and every gap >= 1 s without display damage;
  * serial lines with arrival times (OpenBIOS banner, "switching to new
    context" = ndrvloader and Mac OS ROM hand-offs), through a FIFO behind
    QEMU's ``-serial file:``;
  * the vCPU thread's CPU use every --cpu-ms (``/proc/PID/task/TID/schedstat``):
    busy vs idle (Mac OS naps with MSR[POW] when idle);
  * optionally guest register samples every --pcsample-us of host time
    (RB_PCSAMPLE: lab builds only, see emulator/tools/boottime/), and QEMU
    trace events (``--trace-events``) through a FIFO, stamped on arrival.

    rbd_boottime.py boot --out RUN --seed IMG [--pkg DIR] [--ndrv DIR]
        [--pcsample-us 1000] [--trace-events FILE] [--end quit|shutdown|kill]
        [--settle S] [--rtc base=...] [-- QEMU ARGS]
    rbd_boottime.py batch --root DIR NAME N WORKERS --arm 'name=opts' ... -- COMMON
    rbd_boottime.py summarize DIR...              per-arm milestone medians/p90
    rbd_boottime.py timeline RUN [--map MAP]      phase table, serial, parade, I/O
    rbd_boottime.py pcs RUN [--from S --to S]     guest register samples by region
    rbd_boottime.py forth RUN [--from S --to S]   OpenBIOS time by Forth definition
    rbd_boottime.py map IMG MAP                    HFS+ byte range -> file map
    rbd_boottime.py phases DIR... [--idle]        per-arm phase table: wall, vCPU s, idle
    rbd_boottime.py attrib RUN --map MAP          boot chart: vCPU s per file being read
    rbd_boottime.py perfprof RUN [--window ...]   --perf-vcpu boot: host/guest profile per phase

``boot --hostd laptop|desktop`` takes QEMU's argv from hostd's own
machines.build_argv (PMU laptop with the battery bridge, or the Cuda desktop);
``--snapshot`` boots a frozen master (or a .qcow2 lab overlay) read-only.

Times are seconds after QEMU's launch (monotonic clock).  The guest clock is
pinned with ``-rtc base=2026-10-11T09:00:00`` by default (unprepared disks
ignore Shut Down late on Sundays; startup-stall report 7.1).  Lab tool for
the Linux builder, Python standard library only; evidence stays in RUN.
"""
from __future__ import annotations

import argparse
import bisect
import calendar
import collections
import hashlib
import json
import os
import re
import shlex
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

PKG = os.environ.get('RB_LAB_PKG', '/srv/retroboot/wave2/artifacts/qemu/linux-ppc-r6')
NDRV = os.environ.get('RB_LAB_NDRV', '/srv/retroboot/wave2/artifacts/ndrv')
PCS_FMT = '<QQ8I32I4I'
PCS_SIZE = struct.calcsize(PCS_FMT)
ISO = re.compile(r'^(\d+)-(\d+)-(\d+)T(\d+):(\d+):(\d+)\.(\d+)Z (.*)$')


def parse_size(s):
    w, h = s.lower().split('x')[:2]
    return int(w), int(h)


def pct(vals, q):
    v = sorted(vals)
    if not v:
        return None
    k = (len(v) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(v) - 1)
    return round(v[lo] + (v[hi] - v[lo]) * (k - lo), 2)


# --------------------------------------------------------------------------
class FifoReader(threading.Thread):
    """Reads a FIFO line by line and stamps each line with time.monotonic()."""

    def __init__(self, path: Path, out: Path, t0_ref: list):
        super().__init__(daemon=True)
        self.path, self.out, self.t0_ref = path, out, t0_ref
        self.lines = 0

    def run(self):
        try:
            with open(self.path, 'rb', buffering=0) as f, open(self.out, 'w', buffering=1) as o:
                buf = b''
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    now = time.monotonic() - self.t0_ref[0]
                    buf += chunk
                    *full, buf = buf.split(b'\n')
                    for ln in full:
                        o.write('%.6f %s\n' % (now, ln.decode('latin-1').rstrip('\r')))
                        self.lines += 1
                if buf:
                    o.write('%.6f %s\n' % (time.monotonic() - self.t0_ref[0], buf.decode('latin-1')))
        except OSError:
            pass


class CpuSampler(threading.Thread):
    """vCPU and main-loop thread run time every period (schedstat, ns)."""

    def __init__(self, pid: int, vcpu_tid: int, period: float, t0: float):
        super().__init__(daemon=True)
        self.pid, self.tid, self.period, self.t0 = pid, vcpu_tid, period, t0
        self.rows: list[tuple[float, int, int]] = []
        self.stop = threading.Event()

    @staticmethod
    def _run_ns(path):
        with open(path) as f:
            return int(f.read().split()[0])

    def run(self):
        vp = f'/proc/{self.pid}/task/{self.tid}/schedstat'
        mp = f'/proc/{self.pid}/task/{self.pid}/schedstat'
        while not self.stop.is_set():
            try:
                self.rows.append((round(time.monotonic() - self.t0, 4), self._run_ns(vp), self._run_ns(mp)))
            except (OSError, ValueError):
                return
            time.sleep(self.period)


def band_sig(raw: bytes, stride: int, w: int, h: int, rows: int = 44) -> str:
    """Signature of the bottom icon row (the extension parade)."""
    m = hashlib.md5()
    for y in range(max(0, h - rows), h, 2):
        o = y * stride
        m.update(raw[o:o + w * 4:12])
    return m.hexdigest()


def finder_health(w: int, h: int, stride: int, raw: bytes) -> dict:
    """After the settle: menu titles drawn (dark text pixels in the menu bar)
    and the startup disk icon (non-pattern pixels in the top-right corner).
    A Finder that quit or crashed leaves a blank menu bar."""
    dark = 0
    for y in range(3, min(17, h)):
        row = raw[y * stride:y * stride + min(w, 400) * 4]
        dark += sum(1 for i in range(0, len(row), 4) if row[i] < 60 and row[i + 1] < 60 and row[i + 2] < 60)
    an = stub.analyze_frame(w, h, stride, raw)
    return {'menu_dark': dark, 'finder': an['finder'], 'ok': dark > 150 and an['finder']}


def crop_png(raw: bytes, stride: int, w: int, h: int, y0: int, y1: int, x1: int) -> bytes:
    sub = b''.join(raw[y * stride:y * stride + x1 * 4] for y in range(y0, y1))
    rgb = stub.xrgb_to_rgb(sub, x1, y1 - y0, x1 * 4)
    return stub.png_bytes(x1, y1 - y0, rgb)


class PowerBridge(threading.Thread):
    """hostd's side of the PMU battery bridge, reduced to what a boot needs: hello and
    the battery state to every client (battery.py's wire format; labelled simulated)."""

    def __init__(self, path: Path, state: dict):
        super().__init__(daemon=True)
        import socket
        self.path, self.state = path, state
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(str(path))
        self.srv.listen(4)
        self.clients = []

    def run(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            try:
                c.sendall(b'{"type":"hello","version":1,"backend":"simulated"}\n' +
                          json.dumps({'type': 'battery', **self.state}).encode() + b'\n')
            except OSError:
                pass
            self.clients.append(c)   # kept open; whatever QEMU sends is ignored


def hostd_argv(a, out: Path, disk: Path):
    """QEMU's argv exactly as hostd builds it (machines.build_argv, as apps/lab/rbsession.py
    does), with the lab's sockets, serial FIFO, pinned RTC and a silent audio backend."""
    repo = Path(__file__).resolve().parents[2]
    for sub in ('hostd', 'guestprep'):
        if str(repo / sub) not in sys.path:
            sys.path.insert(0, str(repo / sub))
    from retroboot import schema, pointer  # noqa: E402
    from retroboot.config import Paths  # noqa: E402
    from retroboot.machines import LaunchOptions, Machine, build_argv as hostd_build  # noqa: E402
    from rbguestprep import macloc, usboverdrive  # noqa: E402
    paths = Paths(root=out / 'hostd', qemu_package_override=Path(a.pkg),
                  ndrv_override=Path(a.ndrv) if a.ndrv else None)
    paths.ensure()
    machine = Machine('lab', disk.parent, {'schema_version': 1, 'id': 'lab', 'disk': disk.name, 'name': 'Mac OS 9'})
    if a.template_config:
        from retroboot.catalog import sanitize_template_config
        template = sanitize_template_config(Path(a.template_config).read_bytes())
        machine.meta.update(template=template, name=template.get('name') or 'Mac OS 9')
    settings = schema.defaults()
    settings.update({'share.enabled': bool(a.share), 'system.timezone': a.tz, 'display.pixel_scale': '1'})
    if not a.net:
        settings['network.guest'] = 'off'
    probe = a.probe_disk or str(disk)     # hostd's read-only checks need the raw HFS+ image
    overdrive = usboverdrive.present(probe) if a.usb_mouse == 'auto' else None
    usb_mouse = pointer.wanted(a.usb_mouse, overdrive)
    wheel = pointer.WHEEL_PER_REPORT if usb_mouse and pointer.package_wheel_per_report(Path(a.pkg)) else None
    cities = list(macloc.read_cities(probe)) or None
    bridge = None
    power = None
    if a.hostd == 'laptop':
        power = out / 'power.sock'
        bridge = PowerBridge(power, json.loads(a.battery))
        bridge.start()
    display = {'mode': [a.size[0], a.size[1], 60], 'suggested_scale': 1}
    argv = hostd_build(machine, settings, paths, display,
                       LaunchOptions(display_backend='retroboot', audio_capable=True,
                                     ndrv_dir=Path(a.ndrv) if a.ndrv else None,
                                     loader_dir=Path(a.loader) if a.loader else None,
                                     power_socket=power, buttons='powerbook' if power else None,
                                     usb_mouse=usb_mouse, usb_wheel_per_report=wheel, mac_cities=cities))
    if a.share:   # the lab's share directory instead of hostd's
        i = next(k for k, v in enumerate(argv) if v.startswith('local,id=share0,'))
        argv[i] = f'local,id=share0,security_model=none,path={a.share}'
    qmps = [k for k, v in enumerate(argv) if v == '-qmp']
    argv[qmps[0] + 1] = f'unix:{out}/qmp-control.sock,server=on,wait=off'
    argv[qmps[1] + 1] = f'unix:{out}/qmp-events.sock,server=on,wait=off'
    argv[argv.index('-display') + 1] = f'retroboot,socket={out}/display.sock'
    argv[argv.index('-audiodev') + 1] = (f'wav,id=snd0,path={out}/audio.wav' if a.audio == 'wav'
                                         else 'none,id=snd0')
    argv[argv.index('-rtc') + 1] = a.rtc
    argv[argv.index('-cpu') + 1] = a.cpu
    if disk.suffix == '.qcow2':   # a lab overlay over a released master (guest-change prototypes)
        i = argv.index(f'file={disk},format=raw,media=disk,index=0')
        argv[i] = f'file={disk},format=qcow2,media=disk,index=0'
    (out / 'hostd-launch.json').write_text(json.dumps({'profile': a.hostd, 'usb_mouse': usb_mouse,
                                                       'usb_overdrive': overdrive, 'wheel': wheel,
                                                       'cities': bool(cities), 'battery': a.battery if power else None}))
    return argv, bridge


# --------------------------------------------------------------------------
def cmd_boot(argv):
    split = argv.index('--') if '--' in argv else len(argv)
    ap = argparse.ArgumentParser(prog='rbd_boottime.py boot')
    ap.add_argument('--out', required=True)
    ap.add_argument('--seed', help='disk copied sparsely into RUN/disk.img (never modified)')
    ap.add_argument('--disk', help='boot this disk in place instead of a copy of --seed')
    ap.add_argument('--arm', default='')
    ap.add_argument('--pkg', default=PKG)
    ap.add_argument('--ndrv', default=NDRV, help="driver variant dir ('' = the package's driver)")
    ap.add_argument('--cpu', default='7400')
    ap.add_argument('--size', default='1280x800', type=parse_size)
    ap.add_argument('--mem', type=int, default=512)
    ap.add_argument('--speed', choices=['realtime', 'accelerated'], default='realtime')
    ap.add_argument('--audio', choices=['none', 'wav'], default='none')
    ap.add_argument('--rtc', default='base=2026-10-11T09:00:00')
    ap.add_argument('--tz', default='UTC')
    ap.add_argument('--env', action='append', default=[], help='K=V for the QEMU environment')
    ap.add_argument('--no-drain', dest='drain', action='store_false')
    ap.add_argument('--no-net', dest='net', action='store_false')
    ap.add_argument('--share', default=None, help="DIR, or 'auto' for an empty RUN/share")
    ap.add_argument('--resize-connect', type=int, default=1)
    ap.add_argument('--pcsample-us', type=int, default=0, help='guest register samples (lab build only)')
    ap.add_argument('--trace-events', default=None)
    ap.add_argument('--trace-file', action='store_true', help='QEMU writes the trace to RUN/trace.txt (no FIFO)')
    ap.add_argument('--trace-count', action='store_true',
                    help='only count trace lines per second (RUN/trace-count.txt; high-rate events)')
    ap.add_argument('-d', default=None, help='QEMU -d items (into the trace FIFO)')
    ap.add_argument('--cpu-ms', type=float, default=10.0)
    ap.add_argument('--poll', type=float, default=0.05, help='display poll period, s')
    ap.add_argument('--frames-every', type=float, default=0.0)
    ap.add_argument('--parade-crops', type=int, default=1)
    ap.add_argument('--timeout', type=float, default=150.0)
    ap.add_argument('--settle', type=float, default=0.0, help='keep running this long after Finder')
    ap.add_argument('--settle-until', default=None, help='end --settle early once this file exists (driven runs)')
    ap.add_argument('--end', choices=['quit', 'shutdown', 'kill'], default='quit')
    ap.add_argument('--exit-timeout', type=float, default=60.0)
    ap.add_argument('--power-attempts', type=int, default=3, help='power-key presses until the Shut Down dialog shows')
    ap.add_argument('--dialog-wait', type=float, default=15.0, help='wait this long for the Shut Down dialog per power-key press')
    ap.add_argument('--quiet-s', type=float, default=3.0, help='wait for this long without display damage before Shut Down')
    ap.add_argument('--quiet-max', type=float, default=90.0)
    ap.add_argument('--keep-disk', action='store_true')
    ap.add_argument('--gap', type=float, default=1.0, help='report damage gaps at least this long')
    ap.add_argument('--cd', default=None, help='CD image in the user drive (cd0), as hostd media.cd')
    ap.add_argument('--boot-cd', default=None, help="installer route: boot this CD (hostd's boot_cd argv)")
    ap.add_argument('--airport', default=None, help='AirPort card on, bridge socket PATH (hostd guest_nic_args)')
    ap.add_argument('--ctl', action='store_true', help='presenter-stub control socket RUN/stub.ctl (click/type/shot)')
    ap.add_argument('--pcap', action='store_true', help='dump the guest NIC traffic to RUN/net.pcap')
    ap.add_argument('--perf', action='store_true', help='perf record the QEMU process from launch to Finder (adds -perfmap)')
    ap.add_argument('--perf-vcpu', action='store_true',
                    help='perf record the vCPU thread (DWARF chains, CLOCK_MONOTONIC) to the end of --settle (adds -perfmap)')
    ap.add_argument('--perf-hz', type=int, default=499)
    ap.add_argument('--hmp-at', action='append', default=[],
                    help="T:CMD;CMD...  at T s stop the VM, run HMP commands into RUN/hmp-T.txt, continue")
    ap.add_argument('--hostd', choices=['laptop', 'desktop'], default=None,
                    help="argv from hostd's own machines.build_argv: laptop = PMU with the battery bridge "
                         "(answered in-process), PowerBook buttons, backlight, built-in panel; desktop = Cuda")
    ap.add_argument('--template-config', default=None, help="the Mac's config.json (hostd's per-machine template)")
    ap.add_argument('--loader', default=None, help="classicvirtio loader variant dir (hostd's loader_dir)")
    ap.add_argument('--usb-mouse', choices=['auto', 'on', 'off'], default='auto')
    ap.add_argument('--battery', default='{"present": true, "ac": true, "charging": false, "percent": 100}',
                    help='battery state the in-process power bridge reports (laptop profile)')
    ap.add_argument('--box-white', type=float, default=0.2,
                    help="centre share of white that marks Mac OS's startup box after the gray")
    ap.add_argument('--probe-disk', default=None, help='raw image for hostd\'s read-only checks (a .qcow2 --disk)')
    ap.add_argument('--snapshot', action='store_true',
                    help='boot --disk read-only with -snapshot (overlay in RUN, TMPDIR); no copy')
    a = ap.parse_args(argv[:split])
    a.extra = argv[split + 1:]
    if a.trace_events:
        a.trace_events = str(Path(a.trace_events).resolve())
    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if a.share == 'auto':
        a.share = str(out / 'share')
        Path(a.share).mkdir(exist_ok=True)
    if a.disk:
        disk = Path(a.disk).resolve()
    else:
        disk = out / 'disk.img'
        subprocess.run(['cp', '--sparse=always', a.seed, str(disk)], check=True)
        os.chmod(disk, 0o644)

    serial_fifo = out / 'serial.fifo'
    bridge = None
    if a.hostd:
        argv_q, bridge = hostd_argv(a, out, disk)
        argv_q[argv_q.index('-serial') + 1] = f'file:{serial_fifo}'
    else:
        # rbd_restart_lab.build_argv expects these attributes
        a.ndrv_dir = a.ndrv or None
        a.host_resize = 'on'
        a.trace_events_lab = a.trace_events
        ns = argparse.Namespace(**vars(a))
        ns.trace_events = None
        ns.extra = []
        argv_q = lab.build_argv(ns, out, disk, 0)
        argv_q[argv_q.index('-cpu') + 1] = a.cpu
        # serial -> FIFO (still QEMU's file backend)
        si = argv_q.index(f'file:{out}/serial-0.log')
        argv_q[si] = f'file:{serial_fifo}'
    if a.snapshot:
        argv_q += ['-snapshot']
    # rtc
    argv_q[argv_q.index('-rtc') + 1] = a.rtc
    trace_fifo = out / 'trace.fifo'
    if a.trace_events or a.d:
        if a.trace_events:
            argv_q += ['-trace', f'events={a.trace_events}']
        if a.d:
            argv_q += ['-d', a.d]
        # --trace-file: QEMU writes RUN/trace.txt itself (wall-clock stamps, -msg timestamp=on)
        # so a busy harness can never hold QEMU up on a full FIFO
        argv_q += ['-D', str(out / 'trace.txt') if a.trace_file else str(trace_fifo), '-msg', 'timestamp=on']
    counter = None
    if a.cd:
        i = argv_q.index('if=ide,index=3,media=cdrom,id=cd0,readonly=on')
        argv_q[i] += f',file={Path(a.cd).resolve()},format=raw'
    if a.boot_cd:
        # hostd build_argv(boot_cd=...): 1 ms DMA completion, no PIO drain, no share,
        # the CD on index 2 and the tools drive on 3, -boot d
        argv_q[argv_q.index('macio-ide.dma-completion-delay-ns=0')] = 'macio-ide.dma-completion-delay-ns=1000000'
        if 'macio-ide.pio-status-drain=on' in argv_q:
            k = argv_q.index('macio-ide.pio-status-drain=on')
            del argv_q[k - 1:k + 1]
        argv_q[argv_q.index('if=ide,index=3,media=cdrom,id=cd0,readonly=on')] = \
            f'if=ide,index=2,media=cdrom,id=cd0,readonly=on,file={Path(a.boot_cd).resolve()},format=raw'
        argv_q[argv_q.index('if=ide,index=2,media=cdrom,id=tools0,readonly=on')] = 'if=ide,index=3,media=cdrom,id=tools0,readonly=on'
        argv_q += ['-boot', 'd']
    if a.airport:
        k = argv_q.index('user,model=sungem')
        argv_q[k] = 'none'
        argv_q += ['-netdev', 'user,id=apnet', '-global', 'macio-newworld.airport=true',
                   '-global', 'retroboot-airport.netdev=apnet',
                   '-chardev', f'socket,id=airport0,path={a.airport},reconnect-ms=1000',
                   '-global', 'retroboot-airport.chardev=airport0']
    if a.pcap and 'user,model=sungem' in argv_q:
        argv_q[argv_q.index('user,model=sungem')] = 'user,model=sungem,id=nic0'
        argv_q += ['-object', f'filter-dump,id=dump0,netdev=nic0,file={out}/net.pcap']
    if a.perf or a.perf_vcpu:
        argv_q += ['-perfmap']
    argv_q += a.extra
    (out / 'argv.json').write_text(json.dumps(argv_q, indent=1))
    for fifo in [serial_fifo] + ([trace_fifo] if (a.trace_events or a.d) and not a.trace_file else []):
        try:
            fifo.unlink()
        except FileNotFoundError:
            pass
        os.mkfifo(fifo)

    env = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'RETROBOOT_SESSION': 'boottime',
           'HOME': '/root', 'LANG': 'C.UTF-8', 'TZ': ':' + a.tz}
    if a.snapshot:
        env['TMPDIR'] = str(out)        # -snapshot's temporary overlay stays in RUN
    if a.pcsample_us:
        env['RB_PCSAMPLE'] = str(out / 'pcs.bin')
        env['RB_PCSAMPLE_US'] = str(a.pcsample_us)
    for kv in a.env:
        k, v = kv.split('=', 1)
        env[k] = v

    p = stub.Presenter(out / 'display.sock', out / 'display.jsonl')
    threading.Thread(target=p.accept_loop, daemon=True).start()
    if a.ctl:
        threading.Thread(target=stub.control_server, args=(p, out / 'stub.ctl'), daemon=True).start()
    t0_ref = [0.0]
    readers = [FifoReader(serial_fifo, out / 'serial.txt', t0_ref)]
    if (a.trace_events or a.d) and a.trace_count:
        # high-rate events (e.g. memory_notdirty_write_access): awk counts them per wall
        # second from QEMU's own stamps, at C speed, so QEMU never waits on the FIFO
        counter = subprocess.Popen(['awk', '{c[substr($1,1,19)]++; n++} END {for (k in c) print k, c[k]; '
                                    'print "total", n}', str(trace_fifo)],
                                   stdout=open(out / 'trace-count.txt', 'w'))
    elif (a.trace_events or a.d) and not a.trace_file:
        readers.append(FifoReader(trace_fifo, out / 'trace.txt', t0_ref))
    load = os.getloadavg()[0]
    t0_wall = time.time()
    t0 = time.monotonic()
    t0_ref[0] = t0
    for r in readers:
        r.start()
    proc = subprocess.Popen(argv_q, stdout=open(out / 'qemu.out', 'w'), stderr=subprocess.STDOUT,
                            env=env, cwd=str(out), start_new_session=True)
    res = {'arm': a.arm, 'load1': round(load, 2), 'ncpu_busy_qemu': None, 't0_wall': round(t0_wall, 3),
           't0_mono': t0, 'pkg': a.pkg, 'ndrv': a.ndrv, 'pid': proc.pid}
    try:
        res['ncpu_busy_qemu'] = int(subprocess.run(['pgrep', '-c', 'qemu-system-ppc'], capture_output=True,
                                                   text=True).stdout.strip() or 0)
    except OSError:
        pass

    def bye(*_):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        sys.exit(2)
    signal.signal(signal.SIGTERM, bye)

    try:
        ctl = lab.QMP(str(out / 'qmp-control.sock'))
        evq = lab.QMP(str(out / 'qmp-events.sock'))
    except OSError as exc:
        res['outcome'] = 'qemu-failed'
        res['error'] = str(exc)
        bye_soft = proc.poll()
        res['exit_code'] = bye_soft
        (out / 'result.json').write_text(json.dumps(res, indent=1))
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
        os._exit(1)
    vcpu_tid = None
    try:
        cpus = ctl.cmd('query-cpus-fast').get('return', [])
        vcpu_tid = cpus[0]['thread-id'] if cpus else None
    except Exception:  # noqa: BLE001
        pass
    perf = None
    if a.perf_vcpu and vcpu_tid:
        # the vCPU thread only, DWARF call chains (guest PC of helper samples), monotonic
        # timestamps (aligned with t0_mono); runs until the settle ends
        perf = subprocess.Popen(['perf', 'record', '-F', str(a.perf_hz), '-k', 'CLOCK_MONOTONIC',
                                 '--call-graph', 'dwarf,2048', '-t', str(vcpu_tid), '-o', str(out / 'perf.data')],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif a.perf:
        perf = subprocess.Popen(['perf', 'record', '-F', '997', '-g', '-p', str(proc.pid), '-o', str(out / 'perf.data')],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sampler = None
    if vcpu_tid:
        sampler = CpuSampler(proc.pid, vcpu_tid, a.cpu_ms / 1000.0, t0)
        sampler.start()
    hello = p.wait_for(lambda: p.hello, 20)
    if hello:
        res['hello_s'] = round(hello['mono'] - t0, 3)
        try:
            p.send(stub.REFRESH_RATE, struct.pack('=I', 16))
            if a.resize_connect:
                p.send(stub.RESIZE, struct.pack('=II', *a.size))
        except (RuntimeError, OSError) as exc:
            res['send_error'] = str(exc)

    hmp_at = sorted((float(x.split(':', 1)[0]), x.split(':', 1)[1]) for x in a.hmp_at)

    def run_hmp(t, cmds):
        outp = []
        try:
            for _attempt in range(200):     # stop where the MMU is on (virtual reads work)
                ctl.cmd('stop')
                regs = str(ctl.hmp('info registers').get('return', ''))
                mm = re.search(r'MSR ([0-9a-f]+)', regs)
                if mm and int(mm.group(1), 16) & 0x30 == 0x30:
                    break
                ctl.cmd('cont')
                time.sleep(0.002)
            for c in cmds.split(';'):
                if c.strip():
                    outp.append('## ' + c.strip() + '\n' + str(ctl.hmp(c.strip()).get('return', '')))
            ctl.cmd('cont')
        except Exception as exc:  # noqa: BLE001
            outp.append(f'error {exc}')
        (out / f'hmp-{t:07.3f}.txt').write_text('\n'.join(outp))

    ms: dict[str, float] = {}
    scenes = []
    parade = []
    surfaces = []
    last_band = None
    gray_mono = finder_mono = None
    next_png = 0.0
    outcome = None
    crops = out / 'parade'
    while True:
        now = time.monotonic()
        el = now - t0
        if proc.poll() is not None:
            outcome = 'exited'
            break
        if el > a.timeout:
            outcome = 'timeout'
            break
        while hmp_at and el >= hmp_at[0][0]:
            run_hmp(*hmp_at.pop(0))
        try:
            w, h, stride, raw = p.frame_raw()
        except RuntimeError:
            time.sleep(a.poll)
            continue
        if not surfaces or surfaces[-1][1:] != [w, h]:
            surfaces.append([round(el, 3), w, h])
            if (w, h) == tuple(a.size) and 'mode_set' not in ms:
                ms['mode_set'] = round(el, 3)
        an = stub.analyze_frame(w, h, stride, raw)
        if gray_mono is not None or an['gray']:
            cw = lab.dialog_score(raw, stride, w, h)['center_white']
            scene = 'box' if cw >= a.box_white else ('gray' if an['gray'] else 'desktop')
            if not scenes or scenes[-1][1] != scene:
                scenes.append([round(el, 3), scene, cw])
                (out / 'scenes').mkdir(exist_ok=True)
                lab.save_png(p, out / 'scenes' / f'{len(scenes):02d}-{el:07.3f}-{scene}.png')
                if scene == 'box' and 'box' not in ms:
                    ms['box'] = round(el, 3)
                if scene == 'desktop' and 'desktop' not in ms:
                    ms['desktop'] = round(el, 3)
        if 'nonblack' not in ms and an['nonblack'] >= 0.02:
            ms['nonblack'] = round(el, 3)
        if gray_mono is None and an['gray']:
            gray_mono = now
            ms['gray'] = round(el, 3)
            last_band = band_sig(raw, stride, w, h)
        elif gray_mono is not None:
            sig = band_sig(raw, stride, w, h)
            if sig != last_band:
                last_band = sig
                parade.append(round(el, 3))
                if a.parade_crops:
                    crops.mkdir(exist_ok=True)
                    (crops / f'{len(parade):03d}-{el:07.3f}.png').write_bytes(
                        crop_png(raw, stride, w, h, h - 44, h, min(w, 1280)))
        if a.frames_every and el >= next_png:
            (out / 'frames').mkdir(exist_ok=True)
            lab.save_png(p, out / 'frames' / f'f{el:07.2f}.png')
            next_png = el + a.frames_every
        if an['finder']:
            finder_mono = now
            ms['finder'] = round(el, 3)
            outcome = 'finder'
            lab.save_png(p, out / 'finder.png')
            break
        time.sleep(a.poll)
    def stop_perf():
        perf.send_signal(signal.SIGINT)
        try:
            perf.wait(60)
        except subprocess.TimeoutExpired:
            perf.kill()
        res['perf_pid'] = proc.pid
    if perf and not a.perf_vcpu:
        stop_perf()
    res['outcome'] = outcome
    res['milestones'] = ms
    res['parade'] = parade
    res['scenes'] = scenes
    res['surfaces'] = surfaces
    with p.lock:
        gaps = list(p.damage_gaps)
    lo = gray_mono if gray_mono is not None else t0
    hi = finder_mono if finder_mono is not None else time.monotonic()
    res['gaps'] = [(round(g0 - t0, 3), round(g1 - g0, 3)) for g0, g1 in gaps
                   if g1 > lo and g0 < hi and g1 - g0 >= a.gap]
    if outcome == 'finder' and a.settle:
        t_end = time.monotonic() + a.settle
        while time.monotonic() < t_end and proc.poll() is None and \
                not (a.settle_until and os.path.exists(a.settle_until)):
            time.sleep(0.2)
        lab.save_png(p, out / 'settled.png')
        try:
            w, h, stride, raw = p.frame_raw()
            res['settled'] = finder_health(w, h, stride, raw)
        except RuntimeError:
            pass
    for t_h, cmds in hmp_at:          # the rest after the Finder (e.g. 999:info jit)
        run_hmp(t_h, cmds)
    if perf and a.perf_vcpu:
        stop_perf()
    # end
    if outcome == 'finder' and a.end == 'shutdown':
        ta = time.monotonic()
        try:
            # Like a user: wait until Mac OS has finished its startup (no display
            # damage for --quiet-s; with the hardware cursor off even the busy
            # watch cursor draws), press the power key, wait until the Shut Down
            # dialog is on screen (>= 10 % of the centre changed), then Return for
            # its default button.  No dialog within 15 s: press the power key again.
            def centre():
                w, h, stride, raw = p.frame_raw()
                return [raw[y * stride + x * 4:y * stride + x * 4 + 3]
                        for y in range(h // 4, 3 * h // 4, 8) for x in range(w // 4, 3 * w // 4, 8)]
            attempts = []
            for attempt in range(a.power_attempts):
                t_q = time.monotonic() + a.quiet_max
                while time.monotonic() < t_q:
                    ld = p.last_damage_mono
                    if ld is None or time.monotonic() - ld >= a.quiet_s:
                        break
                    time.sleep(0.2)
                before = centre()
                p.tap(['power'], hold=0.15)
                shown = None
                t_d = time.monotonic() + a.dialog_wait
                while time.monotonic() < t_d:
                    time.sleep(0.5)
                    now_c = centre()
                    changed = sum(1 for x, y in zip(before, now_c) if x != y) / max(1, len(before))
                    if changed >= 0.10:
                        time.sleep(1.0)          # let it finish drawing
                        shown = round(time.monotonic() - t0, 2)
                        break
                attempts.append({'t': round(time.monotonic() - t0, 2), 'dialog_s': shown})
                if shown is None:
                    continue
                lab.save_png(p, out / 'power-dialog.png')
                p.tap(['ret'], hold=0.1)
                break
            res['power_attempts'] = attempts
            proc.wait(a.exit_timeout)
        except (RuntimeError, OSError) as exc:
            res['action_error'] = str(exc)
        except subprocess.TimeoutExpired:
            res['action_timeout'] = True
            try:                     # what Mac OS shows instead of shutting down
                lab.save_png(p, out / 'action-timeout.png')
            except (RuntimeError, OSError):
                pass
        with evq.cond:
            sd = [e for e in evq.events if e.get('event') == 'SHUTDOWN']
        res['shutdown'] = sd[-1]['data'] if sd else None
        res['shutdown_s'] = round(time.monotonic() - ta, 2)
    if proc.poll() is None and a.end != 'kill':
        try:
            ctl.cmd('quit', timeout=5)
            proc.wait(10)
        except Exception:  # noqa: BLE001
            pass
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(10)
    res['exit_code'] = proc.returncode
    if sampler:
        sampler.stop.set()
        sampler.join(2)
        (out / 'cpu.json').write_text(json.dumps(sampler.rows))
        # vCPU thread CPU time up to each milestone: load-robust cost of CPU-bound phases
        for k, t in ms.items():
            prior = [r for r in sampler.rows if r[0] <= t]
            if prior:
                res.setdefault('vcpu_s', {})[k] = round(prior[-1][1] / 1e9, 3)
    for r in readers:
        r.join(3)
    if counter is not None:
        try:
            counter.wait(30)
        except subprocess.TimeoutExpired:
            counter.kill()
    ctl.close()
    evq.close()
    res['elapsed_s'] = round(time.monotonic() - t0, 2)
    for f in (serial_fifo, trace_fifo):
        try:
            f.unlink()
        except FileNotFoundError:
            pass
    if not a.disk and not a.keep_disk:
        disk.unlink()
    (out / 'result.json').write_text(json.dumps(res, indent=1))
    print(json.dumps({k: res[k] for k in ('arm', 'outcome', 'milestones', 'load1')}), flush=True)


# --------------------------------------------------------------------------
def cmd_batch(argv):
    split = argv.index('--') if '--' in argv else len(argv)
    ap = argparse.ArgumentParser(prog='rbd_boottime.py batch')
    ap.add_argument('--root', required=True, type=Path)
    ap.add_argument('--arm', action='append', default=[], help="NAME=boot options (shlex)")
    ap.add_argument('--stagger', type=float, default=2.0)
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
    rd = a.root / a.name
    rd.mkdir(parents=True, exist_ok=True)
    (rd / 'batch.json').write_text(json.dumps({'arms': arms, 'common': [common, common_extra], 'n': a.n,
                                               'workers': a.workers, 'started': time.time()}, indent=1))
    lock = threading.Lock()
    todo = list(range(a.start, a.start + a.n))
    me = Path(__file__).resolve()

    def worker():
        while True:
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
                line = f"{r.get('arm')} {r.get('outcome')} {r.get('milestones')}"
            except (OSError, ValueError) as exc:
                line = f'error {exc}'
            print(time.strftime('%H:%M:%S'), a.name, k, line, flush=True)

    ts = [threading.Thread(target=worker) for _ in range(a.workers)]
    for t in ts:
        t.start()
        time.sleep(a.stagger)
    for t in ts:
        t.join()


def load_results(dirs):
    for d in dirs:
        d = Path(d)
        files = [d / 'result.json'] if (d / 'result.json').exists() else sorted(d.glob('*/result.json'))
        for rj in files:
            try:
                r = json.loads(rj.read_text())
            except ValueError:
                continue
            r['_dir'] = str(rj.parent)
            yield r


def cmd_summarize(argv):
    ap = argparse.ArgumentParser(prog='rbd_boottime.py summarize')
    ap.add_argument('--keys', default='mode_set,nonblack,gray,finder')
    ap.add_argument('--list', action='store_true')
    ap.add_argument('dirs', nargs='+')
    a = ap.parse_args(argv)
    keys = a.keys.split(',')
    per = collections.defaultdict(list)
    for r in load_results(a.dirs):
        per[r.get('arm')].append(r)
    for arm in sorted(per, key=str):
        rs = per[arm]
        oc = collections.Counter(r.get('outcome') for r in rs)
        loads = [r.get('load1', 0) for r in rs]
        print(f'arm {arm!r}: {len(rs)} boots {dict(oc)} load {min(loads):.1f}-{max(loads):.1f}')
        for k in keys:
            v = [r['milestones'][k] for r in rs if k in r.get('milestones', {})]
            if v:
                print(f'   {k:10s} n={len(v):3d} min {min(v):6.2f} median {pct(v, .5):6.2f} '
                      f'p90 {pct(v, .9):6.2f} max {max(v):6.2f}')
        for k in ('gray', 'finder'):
            v = [r['vcpu_s'][k] for r in rs if k in r.get('vcpu_s', {})]
            if v:
                print(f'   vcpu@{k:6s} n={len(v):3d} min {min(v):6.2f} median {pct(v, .5):6.2f} '
                      f'p90 {pct(v, .9):6.2f} max {max(v):6.2f}')
        gf = [r['milestones']['finder'] - r['milestones']['gray'] for r in rs
              if 'finder' in r.get('milestones', {}) and 'gray' in r['milestones']]
        if gf:
            print(f'   gray->Finder n={len(gf):3d} min {min(gf):6.2f} median {pct(gf, .5):6.2f} '
                  f'p90 {pct(gf, .9):6.2f} max {max(gf):6.2f}')
        mg = [max([g[1] for g in r.get('gaps', [])], default=0) for r in rs if r.get('outcome') == 'finder']
        if mg:
            print(f'   max damage gap median {pct(mg, .5)} max {max(mg)}; >=5 s: {sum(1 for x in mg if x >= 5)}')
        st = [r for r in rs if 'settled' in r]
        if st:
            print(f"   settled Finder healthy {sum(1 for r in st if r['settled'].get('ok'))}/{len(st)}")
        sd = [r for r in rs if 'shutdown' in r]
        if sd:
            ok = sum(1 for r in sd if (r.get('shutdown') or {}).get('reason') == 'guest-shutdown')
            retry = sum(1 for r in sd if len(r.get('power_attempts', [])) > 1)
            nodlg = sum(1 for r in sd if not any(x.get('dialog_s') for x in r.get('power_attempts', [])))
            print(f'   shutdown {ok}/{len(sd)} (power key pressed more than once: {retry}; dialog never seen: {nodlg})')
        if a.list:
            for r in rs:
                print('     ', Path(r['_dir']).name, r.get('outcome'), r.get('milestones'), 'load', r.get('load1'))


# --------------------------------------------------------------------------
def cmd_map(argv):
    """HFS+ map: byte ranges on the disk image -> file path and fork."""
    ap = argparse.ArgumentParser(prog='rbd_boottime.py map')
    ap.add_argument('image')
    ap.add_argument('out')
    a = ap.parse_args(argv)
    root = Path(__file__).resolve().parents[2] / 'guestprep'
    sys.path.insert(0, str(root))
    from rbguestprep import hfsplus  # noqa: E402
    vol = hfsplus.Volume(a.image)
    recs = list(vol.records())
    names = {r.cnid: (r.parent_id, r.name) for r in recs if r.is_folder}

    def path_of(parent, name):
        parts = [name]
        seen = 0
        while parent in names and parent not in (1, 2) and seen < 64:
            parent, n = names[parent]
            parts.append(n)
            seen += 1
        return ':'.join(reversed(parts))
    ranges = []
    bs = vol.block_size
    for r in recs:
        if not r.is_file:
            continue
        p = path_of(r.parent_id, r.name)
        for which in ('data', 'rsrc'):
            fork = r.fork(which)
            off = 0
            for e in fork.extents:
                if e.count == 0:
                    continue
                start = vol.offset + e.start * bs
                ranges.append([start, start + e.count * bs, p, which, off])
                off += e.count * bs
    ranges.sort()
    Path(a.out).write_text(json.dumps({'image': a.image, 'volume_offset': vol.offset, 'block_size': bs,
                                       'ranges': ranges}))
    print(f'{len(ranges)} extents from {len(recs)} catalog records')


class DiskMap:
    def __init__(self, path):
        d = json.loads(Path(path).read_text())
        self.r = d['ranges']
        self.starts = [x[0] for x in self.r]

    def lookup(self, off):
        i = bisect.bisect_right(self.starts, off) - 1
        if i >= 0 and self.r[i][0] <= off < self.r[i][1]:
            s, _e, p, which, foff = self.r[i]
            return p, which, foff + off - s
        return None, None, None


# --------------------------------------------------------------------------
def read_pcs(run: Path, t0: float):
    data = (run / 'pcs.bin').read_bytes()
    out = []
    for i in range(0, len(data) - PCS_SIZE + 1, PCS_SIZE):
        v = struct.unpack_from(PCS_FMT, data, i)
        out.append({'t': v[0] / 1e9 - t0, 'vm': v[1] / 1e9, 'nip': v[2], 'lr': v[3], 'ctr': v[4], 'msr': v[5],
                    'srr0': v[6], 'srr1': v[7], 'dec': v[8], 'halted': v[9], 'gpr': v[10:42],
                    'peek': v[42:46]})
    return out


def region(s):
    nip = s['nip']
    if s['halted']:
        return 'halted'
    if nip >= 0xFFF00000 and not (s['msr'] & 0x30):
        return 'openbios-realmode'
    if 0xFFF00000 <= nip:
        return 'hi-fff'
    if 0xFFC00000 <= nip < 0xFFF00000:
        return 'rom-ffc'
    if 0x04000000 <= nip < 0x04100000:
        return 'ndrvloader'
    if 0x68000000 <= nip < 0x68200000:
        return 'emu68k'
    return 'other-%x' % (nip >> 20)


def cmd_pcs(argv):
    ap = argparse.ArgumentParser(prog='rbd_boottime.py pcs')
    ap.add_argument('run')
    ap.add_argument('--from', dest='t0', type=float, default=0.0)
    ap.add_argument('--to', dest='t1', type=float, default=1e9)
    ap.add_argument('--bucket', type=float, default=0.5)
    ap.add_argument('--top', type=int, default=25)
    ap.add_argument('--by', default='nip', help='nip | lr | r24 | page (nip >> 12) | fn (nip & ~0xff)')
    a = ap.parse_args(argv)
    run = Path(a.run)
    res = json.loads((run / 'result.json').read_text())
    ss = [s for s in read_pcs(run, res['t0_mono']) if a.t0 <= s['t'] < a.t1]
    if not ss:
        print('no samples')
        return
    print(f'{len(ss)} samples {ss[0]["t"]:.3f}..{ss[-1]["t"]:.3f} s; vm-rt offset {ss[-1]["vm"] - ss[-1]["t"]:.3f}')
    # buckets
    b = collections.defaultdict(collections.Counter)
    for s in ss:
        b[int(s['t'] / a.bucket)][region(s)] += 1
    for k in sorted(b):
        c = b[k]
        n = sum(c.values())
        print(f'{k * a.bucket:7.2f} n={n:4d} ' + ' '.join(f'{r}:{100 * v // n}%' for r, v in c.most_common(4)))
    key = {'nip': lambda s: s['nip'], 'lr': lambda s: s['lr'], 'r24': lambda s: s['gpr'][24],
           'page': lambda s: s['nip'] >> 12 << 12, 'fn': lambda s: s['nip'] & ~0xff}[a.by]
    c = collections.Counter(key(s) for s in ss if not s['halted'])
    nh = sum(1 for s in ss if s['halted'])
    print(f'halted {nh} ({100 * nh / len(ss):.1f}%); top {a.by} of the rest:')
    for k, v in c.most_common(a.top):
        ex = next(s for s in ss if not s['halted'] and key(s) == k)
        print(f'  {k:08x} {v:6d} {100 * v / len(ss):5.1f}%  e.g. nip {ex["nip"]:08x} lr {ex["lr"]:08x} '
              f'msr {ex["msr"]:x} r24 {ex["gpr"][24]:08x} r27 {ex["gpr"][27]:08x}')


def forth_words(dump: Path):
    """(cfa, name) for every OpenBIOS dictionary header in a dump written by
    RB_PCSAMPLE_DUMP (4-byte little-endian base address, then the bytes).
    Header layout (kernel/dict.c): name, length|0x80, zero pad, status byte,
    link cell (previous LFA or 0), code field."""
    raw = dump.read_bytes()
    base = struct.unpack('<I', raw[:4])[0]
    d = raw[4:]
    words = []
    for lfa in range(8, len(d) - 8, 4):
        link = struct.unpack('>I', d[lfa:lfa + 4])[0]
        if link and not (base <= link < base + lfa and link % 4 == 0):
            continue
        i = lfa - 1                      # status byte
        j = i - 1
        while j > 0 and d[j] == 0 and i - j < 8:
            j -= 1
        b = d[j]
        if b < 0x81 or b > 0x80 + 40:
            continue
        n = b - 0x80
        name = d[j - n:j]
        if len(name) != n or not all(0x21 <= c < 0x7f for c in name):
            continue
        words.append((base + lfa + 4, name.decode('ascii')))
    words.sort()
    return words


def cmd_forth(argv):
    """Attribute OpenBIOS time to Forth definitions (RB_PCSAMPLE_PEEK=fffad020)."""
    ap = argparse.ArgumentParser(prog='rbd_boottime.py forth')
    ap.add_argument('run')
    ap.add_argument('--dump', default=None, help='dictionary dump (default RUN/dict.bin)')
    ap.add_argument('--from', dest='t0', type=float, default=0.0)
    ap.add_argument('--to', dest='t1', type=float, default=1e9)
    ap.add_argument('--top', type=int, default=30)
    ap.add_argument('--bucket', type=float, default=0.0, help='also print the top word per bucket')
    a = ap.parse_args(argv)
    run = Path(a.run)
    res = json.loads((run / 'result.json').read_text())
    words = forth_words(Path(a.dump or run / 'dict.bin'))
    cfas = [w[0] for w in words]

    def word_of(pc):
        i = bisect.bisect_right(cfas, pc) - 1
        return words[i][1] if i >= 0 and pc - cfas[i] < 0x4000 else '?%08x' % pc
    ss = [s for s in read_pcs(run, res['t0_mono']) if a.t0 <= s['t'] < a.t1 and s['nip'] >= 0xFFF00000]
    print(f'{len(words)} dictionary words; {len(ss)} OpenBIOS samples')
    c = collections.Counter(word_of(s['peek'][0]) for s in ss)
    for k, v in c.most_common(a.top):
        print(f'  {v:6d} {100 * v / max(1, len(ss)):5.1f}%  {k}')
    if a.bucket:
        b = collections.defaultdict(collections.Counter)
        for s in ss:
            b[int(s['t'] / a.bucket)][word_of(s['peek'][0])] += 1
        for k in sorted(b):
            n = sum(b[k].values())
            print(f'{k * a.bucket:7.2f} n={n:4d} ' + ' '.join(f'{w}:{100 * v // n}%' for w, v in b[k].most_common(4)))


# --------------------------------------------------------------------------
def cmd_timeline(argv):
    ap = argparse.ArgumentParser(prog='rbd_boottime.py timeline')
    ap.add_argument('run')
    ap.add_argument('--map', default=None)
    ap.add_argument('--files', type=int, default=200, help='file-read groups to print')
    ap.add_argument('--merge', type=float, default=0.25, help='merge reads of one file closer than this, s')
    a = ap.parse_args(argv)
    run = Path(a.run)
    res = json.loads((run / 'result.json').read_text())
    print('milestones', res.get('milestones'), 'outcome', res.get('outcome'), 'load', res.get('load1'))
    print('gaps', res.get('gaps'))
    print('parade', res.get('parade'))
    st = run / 'serial.txt'
    if st.exists():
        print('-- serial')
        for ln in st.read_text(errors='replace').splitlines():
            t, _, text = ln.partition(' ')
            if text.strip():
                print(f'  {float(t):8.3f} {text[:110]}')
    cj = run / 'cpu.json'
    if cj.exists():
        rows = json.loads(cj.read_text())
        print('-- vCPU busy fraction per 0.5 s (main loop in brackets)')
        buckets = collections.OrderedDict()
        for (t_a, v_a, m_a), (t_b, v_b, m_b) in zip(rows, rows[1:]):
            k = int(t_a / 0.5)
            bb = buckets.setdefault(k, [0.0, 0, 0])
            bb[0] += t_b - t_a
            bb[1] += v_b - v_a
            bb[2] += m_b - m_a
        line = []
        for k, (dt, dv, dm) in buckets.items():
            line.append(f'{k * 0.5:5.1f}:{100 * dv / 1e9 / dt:3.0f}[{100 * dm / 1e9 / dt:2.0f}]')
            if len(line) == 8:
                print('  ' + '  '.join(line))
                line = []
        if line:
            print('  ' + '  '.join(line))
    tt = run / 'trace.txt'
    if tt.exists():
        dm = DiskMap(a.map) if a.map else None
        groups = []
        cmds = collections.Counter()
        # '\n' only: 9p trace lines carry Mac file names, and "Icon\r" holds a CR
        for ln in tt.read_text(errors='replace').split('\n'):
            t, _, rest = ln.partition(' ')
            if ISO.match(ln):
                t, rest = 0.0, ln
            else:
                try:
                    t = float(t)
                except ValueError:
                    continue
            m = ISO.match(rest)
            if m:
                g = [int(x) for x in m.groups()[:6]]
                t = calendar.timegm((g[0], g[1], g[2], g[3], g[4], g[5], 0, 0, 0)) + \
                    int(m.group(7)) / 10 ** len(m.group(7)) - res['t0_wall']
                rest = m.group(8)
            ev, _, args = rest.partition(' ')
            if ':' in ev and ev.split(':')[0].isdigit():   # "pid@sec.usec:event" style prefix
                ev = ev.split(':', 1)[1]
            cmds[ev] += 1
            off = ln_ = None
            if ev.startswith('pmac_ide_transfer_submit'):
                kv = dict(x.split('=', 1) for x in args.split() if '=' in x)
                off, ln_ = int(kv.get('offset', -1)), int(kv.get('len', 0))
            elif ev.startswith('ide_sector_read'):
                kv = dict(x.split('=', 1) for x in args.split() if '=' in x)
                off, ln_ = int(kv['sector']) * 512, int(kv['nsectors']) * 512
            if off is None or off < 0:
                continue
            name = '?'
            if dm:
                p, which, _fo = dm.lookup(off)
                name = f'{p} [{which}]' if p else f'(metadata @{off})'
            if groups and groups[-1][2] == name and t - groups[-1][1] <= a.merge:
                groups[-1][1] = t
                groups[-1][3] += ln_
                groups[-1][4] += 1
            else:
                groups.append([t, t, name, ln_, 1])
        print('-- trace events', dict(cmds.most_common(30)))
        if groups:
            print('-- disk reads (first, last, bytes, requests, file)')
            for g in groups[:a.files]:
                print(f'  {g[0]:8.3f} {g[1]:8.3f} {g[3]:9d} {g[4]:5d}  {g[2]}')


# --------------------------------------------------------------------------
class CpuRows:
    """The vCPU thread's run time (cpu.json) as a function of time after launch."""

    def __init__(self, run: Path):
        self.rows = json.loads((run / 'cpu.json').read_text())
        self.ts = [r[0] for r in self.rows]

    def at(self, t):
        """vCPU seconds used by time t (linear between samples)."""
        i = bisect.bisect_right(self.ts, t) - 1
        if i < 0:
            return 0.0
        if i >= len(self.rows) - 1:
            return self.rows[-1][1] / 1e9
        (t0, v0, _), (t1, v1, _) = self.rows[i], self.rows[i + 1]
        return (v0 + (v1 - v0) * (t - t0) / max(1e-9, t1 - t0)) / 1e9

    def busy(self, t0, t1):
        return (self.at(t1) - self.at(t0)) / max(1e-9, t1 - t0)


def busy_end(cpu: CpuRows, start: float, window: float = 2.0, level: float = 0.3, step: float = 0.1):
    """First time from START at which the vCPU stays below LEVEL busy for WINDOW s:
    Mac OS has finished its startup work (startup items, desktop) and naps."""
    t = start
    end = cpu.ts[-1] - window
    while t <= end:
        if cpu.busy(t, t + window) < level:
            return round(t, 2)
        t += step
    return None


def idle_windows(cpu: CpuRows, t0: float, t1: float, step: float = 0.1, level: float = 0.5, min_s: float = 0.3):
    """Stretches of at least MIN_S where the vCPU was busy less than LEVEL of the time."""
    out = []
    cur = None
    t = t0
    while t < t1:
        b = cpu.busy(t, t + step)
        if b < level:
            if cur is None:
                cur = [t, t + step, 0.0]
            cur[1] = t + step
            cur[2] += b * step
        elif cur is not None:
            if cur[1] - cur[0] >= min_s:
                out.append(cur)
            cur = None
        t += step
    if cur is not None and cur[1] - cur[0] >= min_s:
        out.append(cur)
    return [[round(a, 2), round(b - a, 2), round(v / max(1e-9, b - a), 2)] for a, b, v in out]


PHASES = [('launch', 'gray', 'QEMU start -> Mac OS gray (firmware, ROM)'),
          ('gray', 'box', 'gray -> "Welcome to Mac OS" box'),
          ('box', 'parade', 'box -> first extension icon'),
          ('parade', 'desktop', 'extension parade -> desktop drawn'),
          ('desktop', 'finder', 'desktop -> Finder menus'),
          ('finder', 'busy_end', 'Finder menus -> vCPU naps (startup items, desktop icons)')]


def run_phases(run: Path) -> dict | None:
    res = json.loads((run / 'result.json').read_text())
    if res.get('outcome') != 'finder' or not (run / 'cpu.json').exists():
        return None
    cpu = CpuRows(run)
    ms = dict(res['milestones'])
    ms['launch'] = 0.0
    # the desktop: the last change to a frame that is neither the gray nor the box before
    # the Finder (single redraw frames of the box read as desktop and are skipped)
    sc = [x for x in res.get('scenes', []) if x[0] < ms['finder']]
    if sc and sc[-1][1] == 'desktop':
        ms['desktop'] = sc[-1][0]
    if res.get('parade'):
        firsts = [t for t in res['parade'] if t > ms.get('box', ms.get('gray', 0))]
        if firsts:
            ms['parade'] = firsts[0]
    be = busy_end(cpu, ms['finder'])
    if be is not None:
        ms['busy_end'] = be
    out = {'arm': res.get('arm'), 'load1': res.get('load1'), 'ms': ms,
           'vcpu': {k: round(cpu.at(v), 3) for k, v in ms.items()}, 'phases': {}}
    for a_, b_, _label in PHASES:
        if a_ in ms and b_ in ms:
            wall = ms[b_] - ms[a_]
            v = cpu.at(ms[b_]) - cpu.at(ms[a_])
            out['phases'][f'{a_}-{b_}'] = {'wall': round(wall, 3), 'vcpu': round(v, 3), 'idle': round(wall - v, 3)}
    out['idle_windows'] = idle_windows(cpu, 0.0, ms.get('busy_end', ms['finder']))
    return out


def cmd_phases(argv):
    """Per-arm medians of the phase table (wall, vCPU, wall - vCPU) and idle stretches."""
    ap = argparse.ArgumentParser(prog='rbd_boottime.py phases')
    ap.add_argument('--json', default=None, help='write every run\'s phases here')
    ap.add_argument('--idle', action='store_true', help='print every run\'s idle stretches')
    ap.add_argument('dirs', nargs='+')
    a = ap.parse_args(argv)
    per = collections.defaultdict(list)
    allr = []
    for r in load_results(a.dirs):
        ph = run_phases(Path(r['_dir']))
        if ph:
            ph['dir'] = r['_dir']
            per[ph['arm']].append(ph)
            allr.append(ph)
    if a.json:
        Path(a.json).write_text(json.dumps(allr, indent=1))
    for arm in sorted(per, key=str):
        rs = per[arm]
        print(f"arm {arm!r}: {len(rs)} boots, load {min(r['load1'] for r in rs):.1f}-{max(r['load1'] for r in rs):.1f}")
        print('   milestone   wall median (min-max)    vCPU s median')
        for k in ('gray', 'box', 'parade', 'desktop', 'finder', 'busy_end'):
            v = [r['ms'][k] for r in rs if k in r['ms']]
            c = [r['vcpu'][k] for r in rs if k in r['vcpu']]
            if v:
                print(f'   {k:10s} {pct(v, .5):7.2f} ({min(v):6.2f}-{max(v):6.2f})   {pct(c, .5):7.2f}')
        print('   phase                       wall    vCPU    idle (median)')
        for a_, b_, label in PHASES:
            key = f'{a_}-{b_}'
            xs = [r['phases'][key] for r in rs if key in r['phases']]
            if xs:
                print(f"   {key:24s} {pct([x['wall'] for x in xs], .5):6.2f} {pct([x['vcpu'] for x in xs], .5):7.2f} "
                      f"{pct([x['idle'] for x in xs], .5):7.2f}   {label}")
        tot = [sum(w[1] * (1 - w[2]) for w in r['idle_windows']) for r in rs]
        print(f'   idle stretches (>=0.3 s, <50 % busy) to busy_end: median {pct(tot, .5)} s idle')
        if a.idle:
            for r in rs:
                print('     ', Path(r['dir']).name, r['idle_windows'])


def trace_events(run: Path, t0_wall: float):
    """(t, event, args) from RUN/trace.txt (FIFO arrival time, or QEMU's own stamp)."""
    tt = run / 'trace.txt'
    if not tt.exists():
        return
    for ln in tt.read_text(errors='replace').split('\n'):
        t, _, rest = ln.partition(' ')
        if ISO.match(ln):
            t, rest = 0.0, ln
        else:
            try:
                t = float(t)
            except ValueError:
                continue
        m = ISO.match(rest)
        if m:
            g = [int(x) for x in m.groups()[:6]]
            t = calendar.timegm((g[0], g[1], g[2], g[3], g[4], g[5], 0, 0, 0)) + \
                int(m.group(7)) / 10 ** len(m.group(7)) - t0_wall
            rest = m.group(8)
        ev, _, args = rest.partition(' ')
        if ':' in ev and ev.split(':')[0].isdigit():
            ev = ev.split(':', 1)[1]
        yield t, ev, args


def item_of(path: str, depth: int) -> str:
    parts = path.split(':')
    if parts and parts[0] == 'System Folder':
        if len(parts) >= 2 and parts[1] == 'Fonts' or (parts[1:2] == ['Preferences'] and depth < 3):
            return ':'.join(parts[:2])
        return ':'.join(parts[:3])
    return ':'.join(parts[:depth])


def cmd_attrib(argv):
    """Boot chart: charge each stretch of the boot to the file Mac OS was reading when it
    began (disk trace mapped to files), as vCPU and wall seconds; 9p bursts separately.
    Approximate by construction: a file's code may run later than its read."""
    ap = argparse.ArgumentParser(prog='rbd_boottime.py attrib')
    ap.add_argument('run')
    ap.add_argument('--map', required=True)
    ap.add_argument('--from', dest='t_from', default='gray')
    ap.add_argument('--depth', type=int, default=2)
    ap.add_argument('--top', type=int, default=40)
    ap.add_argument('--seq', action='store_true', help='print the sequence, not only the totals')
    a = ap.parse_args(argv)
    run = Path(a.run)
    res = json.loads((run / 'result.json').read_text())
    ph = run_phases(run)
    cpu = CpuRows(run)
    ms = ph['ms']
    t_start = ms.get(a.t_from, 0.0)
    t_end = ms.get('busy_end', ms['finder'])
    dm = DiskMap(a.map)
    reads = []
    p9 = []
    for t, ev, args in trace_events(run, res['t0_wall']):
        if ev.startswith('v9fs_'):
            p9.append(t)
            continue
        off = None
        if ev.startswith('pmac_ide_transfer_submit'):
            kv = dict(x.split('=', 1) for x in args.split() if '=' in x)
            off = int(kv.get('offset', -1))
        elif ev.startswith('ide_sector_read'):
            kv = dict(x.split('=', 1) for x in args.split() if '=' in x)
            off = int(kv['sector']) * 512
        if off is None or off < 0 or not (t_start <= t <= t_end):
            continue
        p, which, _fo = dm.lookup(off)
        reads.append((t, item_of(p, a.depth) if p else '(volume metadata)'))
    # sequence of items: a change of item starts a new stretch (metadata reads do not)
    seq = []
    for t, it in reads:
        if it == '(volume metadata)' and seq:
            continue
        if not seq or seq[-1][1] != it:
            seq.append([t, it])
    tot = collections.OrderedDict()
    print(f"{run.name}: arm {res.get('arm')}  {a.t_from} {t_start:.2f} -> busy_end {t_end:.2f}; "
          f"{len(reads)} disk requests, {len(p9)} 9p requests")
    bounds = [s[0] for s in seq] + [t_end]
    if seq and seq[0][0] > t_start:
        seq.insert(0, [t_start, '(before the first read)'])
        bounds.insert(0, t_start)
    for i, (t, it) in enumerate(seq):
        t1 = bounds[i + 1]
        v = cpu.at(t1) - cpu.at(t)
        n9 = sum(1 for x in p9 if t <= x < t1)
        e = tot.setdefault(it, [0.0, 0.0, 0, t])
        e[0] += t1 - t
        e[1] += v
        e[2] += n9
        if a.seq:
            print(f'  {t:7.2f} {t1 - t:6.2f} s  vCPU {v:5.2f}  9p {n9:5d}  {it}')
    print('  total wall  vCPU   idle   9p   first  item')
    for it, (w, v, n9, first) in sorted(tot.items(), key=lambda kv: -kv[1][1])[:a.top]:
        print(f'  {w:9.2f} {v:6.2f} {w - v:6.2f} {n9:5d} {first:7.2f}  {it}')


GUEST_PC = re.compile(r'guest-0x([0-9a-f]+)')


def guest_region(pc: int) -> str:
    """Coarse guest address regions of a Mac OS 9 boot (logical addresses, TB pc)."""
    if pc >= 0xFFF00000:
        return 'fff00000+ (OpenBIOS before the hand-off; ROM image top)'
    if pc >= 0xFFC00000:
        return 'ffc00000-ffefffff (Mac OS ROM image: NanoKernel, ROM natives)'
    if 0x68000000 <= pc < 0x68200000:
        return '68000000-681fffff (68k emulator)'
    if 0x04000000 <= pc < 0x04100000:
        return '04000000-040fffff (ndrvloader)'
    return '%08x-%08x' % (pc >> 24 << 24, (pc >> 24 << 24) + 0xffffff)


def cmd_perfprof(argv):
    """Guest-PC and host-category profile of a --perf-vcpu boot, per phase."""
    ap = argparse.ArgumentParser(prog='rbd_boottime.py perfprof')
    ap.add_argument('run')
    ap.add_argument('--top', type=int, default=8)
    ap.add_argument('--pages', type=int, default=0, help='also print the hottest 4 KB guest pages per phase')
    ap.add_argument('--window', action='append', default=[],
                    help='T0,T1[,LABEL]: also report this stretch (seconds after launch) on its own, with its '
                         'hottest guest instructions (a handful of PCs = a polling loop)')
    a = ap.parse_args(argv)
    run = Path(a.run)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests/ppcfpu/lab'))
    from guest_profile import host_category  # noqa: E402
    res = json.loads((run / 'result.json').read_text())
    ph = run_phases(run)
    ms = ph['ms']
    t0 = res['t0_mono']
    pid = res.get('perf_pid') or res['pid']
    pm = Path(f'/tmp/perf-{pid}.map')
    if pm.exists() and not (run / pm.name).exists():
        (run / pm.name).write_bytes(pm.read_bytes())
    script = run / 'perf.script'
    if not script.exists():
        with open(script, 'w') as f:
            subprocess.run(['perf', 'script', '-i', str(run / 'perf.data'), '-F', 'time,ip,sym,dso'],
                           stdout=f, stderr=subprocess.DEVNULL, check=False)
    bounds = [(n, ms[n]) for n in ('gray', 'box', 'parade', 'desktop', 'finder', 'busy_end') if n in ms]
    names = ['launch-' + bounds[0][0]] + [f'{bounds[i][0]}-{bounds[i + 1][0]}' for i in range(len(bounds) - 1)] + ['after']
    edges = [b[1] for b in bounds]

    wins = []
    for w in a.window:
        f = w.split(',')
        wins.append((float(f[0]), float(f[1]), f[2] if len(f) > 2 else f'{f[0]}-{f[1]} s'))
    pcs = collections.defaultdict(collections.Counter)

    def phase_of(t):
        return names[bisect.bisect_right(edges, t)]
    hostc = collections.defaultdict(collections.Counter)
    guestc = collections.defaultdict(collections.Counter)
    pages = collections.defaultdict(collections.Counter)
    n_ph = collections.Counter()
    cur = None

    def flush(c):
        if not c or c[1] is None:
            return
        t, frames = c[0], c[1]
        p = phase_of(t)
        n_ph[p] += 1
        leaf_sym, leaf_dso = frames[0] if frames else ('?', '?')
        g = next((int(m.group(1), 16) for s_, _d in frames for m in [GUEST_PC.search(s_)] if m), None)
        cat = 'translated guest code' if GUEST_PC.search(leaf_sym) else host_category(leaf_sym, leaf_dso)
        hostc[p][cat] += 1
        guestc[p][guest_region(g) if g is not None else '(no guest frame)'] += 1
        if g is not None:
            pages[p][g >> 12 << 12] += 1
        for w0, w1, lab in wins:
            if w0 <= t < w1:
                n_ph['window ' + lab] += 1
                hostc['window ' + lab][cat] += 1
                guestc['window ' + lab][guest_region(g) if g is not None else '(no guest frame)'] += 1
                if g is not None:
                    pcs['window ' + lab][g] += 1
    with open(script, errors='replace') as f:
        for ln in f:
            if not ln.strip():
                continue
            if not ln.startswith((' ', '\t')) or re.match(r'^\s*\d+\.\d+:', ln):
                m = re.search(r'(\d+\.\d+):', ln)
                if m:
                    flush(cur)
                    cur = [float(m.group(1)) - t0, []]
                    continue
            parts = ln.strip().split(None, 1)
            if cur is not None and len(parts) == 2:
                rest = parts[1]
                dm_ = re.search(r'\(([^)]*)\)\s*$', rest)
                cur[1].append((rest[:dm_.start()].strip() if dm_ else rest, dm_.group(1) if dm_ else ''))
    flush(cur)
    tot = sum(v for k, v in n_ph.items() if not k.startswith('window '))
    print(f'{run.name}: {tot} vCPU samples; milestones ' + ', '.join(f'{n} {t:.2f}' for n, t in bounds))
    for p in names + ['window ' + w[2] for w in wins]:
        n = n_ph[p]
        if not n:
            continue
        print(f'== {p}: {n} samples ({100 * n / tot:.1f} %)')
        print('   host: ' + ', '.join(f'{k} {100 * v / n:.0f}%' for k, v in hostc[p].most_common(a.top)))
        print('   guest: ' + ', '.join(f'{k} {100 * v / n:.0f}%' for k, v in guestc[p].most_common(a.top)))
        if a.pages and p in pages:
            print('   pages: ' + ', '.join(f'{k:08x} {100 * v / n:.1f}%' for k, v in pages[p].most_common(a.pages)))
        if p in pcs:
            top = pcs[p].most_common(12)
            print(f'   {len(pcs[p])} distinct guest PCs; top 12 hold '
                  f'{100 * sum(v for _k, v in top) / n:.0f}%: ' + ', '.join(f'{k:08x} {v}' for k, v in top))


if __name__ == '__main__':
    cmds = {'perfprof': cmd_perfprof, 'boot': cmd_boot, 'batch': cmd_batch, 'summarize': cmd_summarize, 'map': cmd_map,
            'timeline': cmd_timeline, 'pcs': cmd_pcs, 'forth': cmd_forth, 'phases': cmd_phases,
            'attrib': cmd_attrib}
    if len(sys.argv) > 1 and sys.argv[1] in cmds:
        cmds[sys.argv[1]](sys.argv[2:])
    else:
        sys.exit(__doc__)
