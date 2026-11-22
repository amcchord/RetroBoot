#!/usr/bin/env python3
"""Quadra 800 (68k) lab harness for the linux-m68k package (D044, LEGACY-01/02).

Launches qemu-system-m68k with the shared contract's argv (hostd builds the same
one), a presenter stub on -display retroboot and a QMP event logger, and measures
what the lab needs: boot milestones, per-thread host CPU, guest events.  Lab and
qualification tooling only; Python standard library.

    rbd_quadra.py launch --pkg PKG --rom ROM --pram-seed SEED --disk DISK --run RUN \\
        [--ram 128] [--size 800x600x8] [--share DIR] [--net user|none] [--audio none|wav]
    rbd_quadra.py argv ...same options...         # print the argv only
    rbd_quadra.py cpu --run RUN [--seconds 30]      # host CPU per QEMU thread
    rbd_quadra.py stop --run RUN [--answer shutdown|restart]   # power key + dialog
    rbd_quadra.py kill --run RUN
    rbd_presenter_stub.py ctl RUN/stub.ctl '{"cmd":"shot","path":"RUN/x.png"}'

The disk is copied into RUN (cp --sparse=always) unless --disk-in-place; the PRAM
seed is copied to RUN/pram.img unless RUN/pram.img exists.  Every run directory
keeps argv.json, qemu.out, serial.log, stub.jsonl (presenter log, milestones with
monotonic times), qmp-events.jsonl and launch.json (the monotonic launch time).
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
from rbd_lab import QMP, start_stub, wait_path  # noqa: E402
from rbd_presenter_stub import StubClient  # noqa: E402

CLK_TCK = os.sysconf('SC_CLK_TCK') if hasattr(os, 'sysconf') else 100


def contract_argv(qemu: Path, pkg_share: Path, rom: Path, pram: Path, disk: Path, run: Path, *,
                  ram_mb: int = 128, width: int = 800, height: int = 600, depth: int = 8,
                  share: Path | None = None, share_tag: str = 'RetroBoot Share',
                  iso: Path | None = None, net: str = 'user', audio: str = 'none',
                  virtio: bool = True, tablet: bool = True, thread_names: bool = True,
                  pcap: Path | None = None, extra: list[str] | None = None,
                  machine: str = 'q800,fb=none,audiodev=snd0', qfb: bool = True) -> list[str]:
    """The shared contract's launch argv (quadra-contract item 2), in its order.

    virtio=False drops the virtio card (and with it the tablet and the share):
    only for making the PRAM seed.  thread_names adds -name ...,debug-threads=on
    (thread names for per-thread CPU accounting; no other effect).  machine and
    qfb=False are lab diagnostics only (the contract is q800,fb=none + nubus-qfb).
    """
    args = [str(qemu), '-M', machine, '-m', str(ram_mb),
            '-bios', str(rom), '-L', str(pkg_share),
            '-drive', f'file={pram},format=raw,if=mtd']
    if virtio:
        args += ['-device', f'nubus-virtio-mmio,romfile={pkg_share}/classicvirtio-68k.rom']
    if qfb:
        args += ['-device', f'nubus-qfb,width={width},height={height},depth={depth}']
    if virtio and tablet:
        args += ['-device', 'virtio-tablet-device']
    if virtio and share is not None:
        args += ['-device', f'virtio-9p-device,fsdev=share0,mount_tag={share_tag}',
                 '-fsdev', f'local,id=share0,security_model=none,path={share}']
    args += ['-device', 'scsi-hd,scsi-id=0,drive=hd0',
             '-drive', f'file={disk},format=raw,if=none,id=hd0',
             '-device', 'scsi-cd,scsi-id=3,drive=cd0',
             '-drive', 'if=none,id=cd0,media=cdrom,readonly=on' + (f',file={iso}' if iso else '')]
    if net == 'user':
        args += ['-nic', 'user,model=dp83932' + (',id=net0' if pcap else '')]
    else:
        args += ['-nic', 'none']
    if audio == 'wav':
        args += ['-audiodev', f'wav,id=snd0,path={run}/guest-audio.wav']
    elif audio == 'alsa':
        args += ['-audiodev', 'alsa,id=snd0']
    else:
        args += ['-audiodev', 'none,id=snd0']
    args += ['-display', f'retroboot,socket={run}/display.sock',
             '-qmp', f'unix:{run}/qmp-control.sock,server=on,wait=off',
             '-qmp', f'unix:{run}/qmp-events.sock,server=on,wait=off',
             '-action', 'reboot=shutdown', '-rtc', 'base=localtime',
             '-serial', f'file:{run}/serial.log']
    if thread_names:
        args += ['-name', 'quadra,debug-threads=on']
    if pcap:
        args += ['-object', f'filter-dump,id=dump0,netdev=net0,file={pcap}']
    return args + list(extra or [])


def package_paths(a) -> tuple[Path, Path]:
    if a.pkg:
        return a.pkg / 'bin/qemu-system-m68k', a.pkg / 'share/qemu'
    return a.qemu, a.share_qemu


def build_argv(a, run: Path) -> list[str]:
    qemu, share_qemu = package_paths(a)
    w, h, d = (int(v) for v in a.size.split('x'))
    disk = a.disk if a.disk_in_place else run / 'disk.img'
    return contract_argv(qemu.resolve(), share_qemu.resolve(), a.rom.resolve(), run / 'pram.img',
                         disk.resolve(), run, ram_mb=a.ram, width=w, height=h, depth=d,
                         share=a.share.resolve() if a.share else None, share_tag=a.share_tag,
                         iso=a.iso.resolve() if a.iso else None, net=a.net, audio=a.audio,
                         virtio=not a.no_virtio, tablet=not a.no_tablet,
                         pcap=run / 'net.pcap' if a.pcap else None, extra=a.extra,
                         machine=a.machine, qfb=not a.no_qfb)


def event_logger(run: Path):
    """Append every QMP event (with host monotonic time) to qmp-events.jsonl."""
    sock = run / 'qmp-events.sock'
    wait_path(sock, 30)
    for _ in range(100):
        try:
            q = QMP(sock, timeout=None)
            break
        except OSError:
            time.sleep(0.1)
    else:
        return
    with (run / 'qmp-events.jsonl').open('a', buffering=1) as log:
        log.write(json.dumps({'event': 'LOGGER_CONNECTED', 'host_mono': time.monotonic()}) + '\n')
        while True:
            try:
                e = q._read()
            except (ConnectionError, OSError, ValueError):
                log.write(json.dumps({'event': 'LOGGER_CLOSED', 'host_mono': time.monotonic()}) + '\n')
                return
            if 'event' in e:
                e['host_mono'] = time.monotonic()
                log.write(json.dumps(e) + '\n')


def launch(a) -> dict:
    run = a.run.resolve()
    run.mkdir(parents=True, exist_ok=True)
    for name in ('display.sock', 'qmp-control.sock', 'qmp-events.sock'):
        if (run / name).exists():
            raise SystemExit(f'{run / name} exists: use a fresh run directory')
    if not a.disk_in_place:
        if (run / 'disk.img').exists():
            raise SystemExit(f'{run}/disk.img exists')
        subprocess.run(['cp', '--sparse=always', str(a.disk), str(run / 'disk.img')], check=True)
    if not (run / 'pram.img').exists():
        if not a.pram_seed:
            raise SystemExit('no RUN/pram.img and no --pram-seed')
        shutil.copyfile(a.pram_seed, run / 'pram.img')
    args = build_argv(a, run)
    stub = start_stub(run)
    (run / 'stub.pid').write_text(f'{stub.pid}\n')
    logger = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'events', '--run', str(run)],
                              stdout=(run / 'events.out').open('ab'), stderr=subprocess.STDOUT,
                              start_new_session=True)
    (run / 'logger.pid').write_text(f'{logger.pid}\n')
    (run / 'argv.json').write_text(json.dumps(args, indent=2) + '\n')
    env = {**os.environ, 'RETROBOOT_SESSION': run.name}
    out = (run / 'qemu.out').open('ab')
    t0 = time.monotonic()
    proc = subprocess.Popen(args, stdout=out, stderr=subprocess.STDOUT, start_new_session=True, env=env)
    info = {'run': str(run), 'qemu_pid': proc.pid, 'stub_pid': stub.pid, 'launch_mono': t0,
            'launch_unix': time.time()}
    (run / 'qemu.pid').write_text(f'{proc.pid}\n')
    (run / 'launch.json').write_text(json.dumps(info, indent=2) + '\n')
    return info


def proc_threads(pid: int) -> dict[str, float]:
    """CPU seconds (user+system) per thread name of a process."""
    out: dict[str, float] = {}
    for tid in os.listdir(f'/proc/{pid}/task'):
        try:
            stat = Path(f'/proc/{pid}/task/{tid}/stat').read_text()
            name = Path(f'/proc/{pid}/task/{tid}/comm').read_text().strip()
        except OSError:
            continue
        fields = stat[stat.rindex(')') + 2:].split()
        secs = (int(fields[11]) + int(fields[12])) / CLK_TCK
        out[name] = out.get(name, 0.0) + secs
    return out


def proc_total(pid: int) -> float:
    stat = Path(f'/proc/{pid}/stat').read_text()
    fields = stat[stat.rindex(')') + 2:].split()
    return (int(fields[11]) + int(fields[12])) / CLK_TCK


def ctx_switches(pid: int) -> int:
    total = 0
    for tid in os.listdir(f'/proc/{pid}/task'):
        try:
            for line in Path(f'/proc/{pid}/task/{tid}/status').read_text().splitlines():
                if line.startswith(('voluntary_ctxt_switches', 'nonvoluntary_ctxt_switches')):
                    total += int(line.split()[1])
        except OSError:
            pass
    return total


def cpu_window(pid: int, seconds: float) -> dict:
    t0, c0, th0, cs0 = time.monotonic(), proc_total(pid), proc_threads(pid), ctx_switches(pid)
    time.sleep(seconds)
    t1, c1, th1, cs1 = time.monotonic(), proc_total(pid), proc_threads(pid), ctx_switches(pid)
    dt = t1 - t0
    load = Path('/proc/loadavg').read_text().split()[:3]
    return {'seconds': round(dt, 2), 'cpu_pct': round((c1 - c0) / dt * 100, 2),
            'threads_pct': {k: round((th1.get(k, 0) - th0.get(k, 0)) / dt * 100, 2)
                            for k in sorted(th1) if th1.get(k, 0) - th0.get(k, 0) > 0},
            'ctx_switches_per_s': round((cs1 - cs0) / dt, 1), 'loadavg': load}


def run_pid(run: Path, name: str = 'qemu.pid') -> int:
    return int((run / name).read_text())


def stop(run: Path, answer: str = 'shutdown', timeout: float = 60) -> dict:
    """Mac OS's own Shut Down/Restart: ADB power key, then Return (Shut Down) or R."""
    stub = StubClient(run / 'stub.ctl')
    t0 = time.monotonic()
    stub('tap', keys=['power'], hold=0.1)
    time.sleep(1.5)
    stub('tap', keys=['ret' if answer == 'shutdown' else 'r'], hold=0.08)
    t1 = time.monotonic()
    deadline = t1 + timeout
    while time.monotonic() < deadline:
        for e in read_events(run):
            if e.get('event') == 'SHUTDOWN' and e['host_mono'] >= t0:
                return {'event': e, 'after_answer_s': round(e['host_mono'] - t1, 3)}
        time.sleep(0.1)
    return {'event': None}


def read_events(run: Path) -> list[dict]:
    p = run / 'qmp-events.jsonl'
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def kill(run: Path):
    for name in ['qemu.pid', 'stub.pid', 'logger.pid']:
        f = run / name
        if f.exists():
            try:
                os.kill(int(f.read_text()), signal.SIGTERM)
            except (ProcessLookupError, ValueError):
                pass


def add_launch_options(p):
    p.add_argument('--pkg', type=Path, help='package root (bin/qemu-system-m68k, share/qemu)')
    p.add_argument('--qemu', type=Path)
    p.add_argument('--share-qemu', type=Path)
    p.add_argument('--rom', type=Path, required=True)
    p.add_argument('--pram-seed', type=Path)
    p.add_argument('--disk', type=Path, required=True)
    p.add_argument('--disk-in-place', action='store_true')
    p.add_argument('--run', type=Path, required=True)
    p.add_argument('--ram', type=int, default=128)
    p.add_argument('--size', default='800x600x8')
    p.add_argument('--share', type=Path)
    p.add_argument('--share-tag', default='RetroBoot Share')
    p.add_argument('--iso', type=Path)
    p.add_argument('--net', choices=['user', 'none'], default='user')
    p.add_argument('--audio', choices=['none', 'wav', 'alsa'], default='none')
    p.add_argument('--no-virtio', action='store_true')
    p.add_argument('--no-tablet', action='store_true')
    p.add_argument('--pcap', action='store_true')
    p.add_argument('--machine', default='q800,fb=none,audiodev=snd0', help='diagnostics only')
    p.add_argument('--no-qfb', action='store_true', help='diagnostics only (use with --machine q800,fb=mac,...)')
    p.add_argument('--extra', action='append', default=[], help='one extra QEMU argument (repeat)')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='mode', required=True)
    add_launch_options(sub.add_parser('launch'))
    add_launch_options(sub.add_parser('argv'))
    e = sub.add_parser('events')
    e.add_argument('--run', type=Path, required=True)
    c = sub.add_parser('cpu')
    c.add_argument('--run', type=Path, required=True)
    c.add_argument('--seconds', type=float, default=30)
    s = sub.add_parser('stop')
    s.add_argument('--run', type=Path, required=True)
    s.add_argument('--answer', choices=['shutdown', 'restart'], default='shutdown')
    k = sub.add_parser('kill')
    k.add_argument('--run', type=Path, required=True)
    a = ap.parse_args()
    if a.mode == 'launch':
        print(json.dumps(launch(a)))
    elif a.mode == 'argv':
        print(json.dumps(build_argv(a, a.run.resolve()), indent=2))
    elif a.mode == 'events':
        event_logger(a.run.resolve())
    elif a.mode == 'cpu':
        print(json.dumps(cpu_window(run_pid(a.run.resolve()), a.seconds), indent=2))
    elif a.mode == 'stop':
        print(json.dumps(stop(a.run.resolve(), a.answer)))
    else:
        kill(a.run.resolve())


if __name__ == '__main__':
    main()
