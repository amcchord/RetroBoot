#!/usr/bin/env python3
"""Lab helpers for qualifying ``-display retroboot`` on the RetroBoot builder.

Developer/qualification tooling only (not the appliance guest-launcher):
QMP client, Mac OS low-memory clock probes, disposable fixture copies and a
reference QEMU argv that mirrors ``emulator/scripts/machine.py`` with the
RetroBoot display backend and two QMP sockets.

    rbd_lab.py launch --qemu BIN --firmware DIR --run RUN [--timing accelerated]
    rbd_lab.py stop --run RUN            # clean Shut Down via ADB power key
    rbd_lab.py kill --run RUN            # stop stub + QEMU without a clean guest stop
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

TOOLS = Path(__file__).resolve().parent
STUB = TOOLS / 'rbd_presenter_stub.py'
DEFAULT_FIXTURE = Path('/srv/retroboot/fixtures/os9-original/disk.img')
MAC_EPOCH_OFFSET = 2082844800  # seconds from 1904-01-01 to 1970-01-01

sys.path.insert(0, str(TOOLS))
from rbd_presenter_stub import StubClient  # noqa: E402


class QMP:
    def __init__(self, path: Path, timeout: float = 30):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(str(path))
        self.f = self.sock.makefile('rw', buffering=1)
        self.events: list[dict] = []
        self.greeting = self._read()
        self.execute('qmp_capabilities')

    def _read(self) -> dict:
        line = self.f.readline()
        if not line:
            raise ConnectionError('QMP closed')
        return json.loads(line)

    def execute(self, command: str, **arguments):
        self.f.write(json.dumps({'execute': command, 'arguments': arguments}) + '\n')
        self.f.flush()
        while True:
            reply = self._read()
            if 'event' in reply:
                reply['host_mono'] = time.monotonic()
                self.events.append(reply)
                continue
            if 'error' in reply:
                raise RuntimeError(f'{command}: {reply["error"]}')
            return reply.get('return')

    def hmp(self, line: str) -> str:
        return self.execute('human-monitor-command', **{'command-line': line})

    def wait_event(self, name: str, timeout: float) -> dict | None:
        for e in self.events:
            if e.get('event') == name:
                return e
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.sock.settimeout(max(0.05, deadline - time.monotonic()))
            try:
                e = self._read()
            except (socket.timeout, TimeoutError):
                break
            except ConnectionError:
                break
            if 'event' in e:
                e['host_mono'] = time.monotonic()
                self.events.append(e)
                if e['event'] == name:
                    return e
        return None

    def close(self):
        try:
            self.f.close()
            self.sock.close()
        except OSError:
            pass


def guest_lowmem(qmp: QMP, attempts: int = 40) -> dict:
    """Mac OS low-memory globals: Ticks (0x16A, 60 Hz) and Time (0x20C, s since 1904).

    Read through the CPU's logical mapping (HMP ``x``) in one command so both
    words come from the same translation state; Mac OS 9 does not keep low
    memory at physical address 0.  When the vCPU happens to be in real mode
    the read returns physical memory instead, so retry until Time is within a
    year of the host clock.
    """
    for _ in range(attempts):
        text = qmp.hmp('x /42wx 0x168')
        words = {}
        for line in text.splitlines():
            addr, _, vals = line.partition(':')
            for i, v in enumerate(vals.split()):
                words[int(addr, 16) + 4 * i] = int(v, 16)
        host = time.time()
        mac_time = words[0x20C]
        ticks = ((words[0x168] & 0xFFFF) << 16) | (words[0x16C] >> 16)
        if abs(mac_time - MAC_EPOCH_OFFSET - host) < 366 * 86400:
            return {'time': mac_time, 'ticks': ticks, 'host_unix': host,
                    'host_mono': time.monotonic(), 'guest_unix': mac_time - MAC_EPOCH_OFFSET}
        time.sleep(0.05)
    raise RuntimeError('Mac OS low memory not readable through the logical mapping')


def nvram_partitions(path: Path) -> dict:
    """CHRP partitions of an 8 KiB Power Mac NVRAM image: name -> (offset, bytes)."""
    data = path.read_bytes()
    out, off = {}, 0
    while off + 16 <= len(data):
        sig, _csum, blocks = data[off], data[off + 1], int.from_bytes(data[off + 2:off + 4], 'big')
        size = blocks * 16
        if size < 16 or off + size > len(data):
            break
        name = data[off + 4:off + 16].split(b'\0', 1)[0].decode('latin-1')
        out[name] = {'signature': sig, 'offset': off, 'size': size, 'body': data[off + 16:off + size]}
        off += size
    return out


def timebase(qmp: QMP) -> int:
    import re
    m = re.search(r'\bTB\s+\d+\s+(\d+)\s+DECR\b', qmp.hmp('info registers'))
    if not m:
        raise RuntimeError('TB missing from info registers')
    return int(m.group(1))


def reference_argv(qemu: Path, firmware: Path, run: Path, disk: Path, *, timing: str = 'realtime',
                   size: str = '1024x768', depth: int = 32, ram_mb: int = 512, cpu: str = '7400',
                   audio: str = 'none', name: str = 'retroboot-guest', nic: bool = True,
                   tablet: bool = True, ide_pio_drain: bool = True,
                   nvram: Path | None = None, rtc: str | None = None,
                   ide_dma_delay_ns: int = 0, share: Path | None = None,
                   share_tag: str = 'RetroBoot Share') -> list[str]:
    """machine.py's mac99 profile, with -display retroboot and two QMP sockets."""
    audiodev = 'none,id=snd0' if audio == 'none' else f'wav,id=snd0,path={run}/guest-audio.wav'
    args = [str(qemu), '-accel', 'tcg,tb-size=512', '-M', 'mac99,via=cuda,audiodev=snd0',
            '-cpu', cpu, '-m', str(ram_mb), '-L', str(firmware), '-vga', 'std',
            '-global', 'VGA.vgamem_mb=64', '-global', 'VGA.host-resize=on',
            '-global', 'VGA.packed-lowbpp=on', '-global', 'VGA.hardware-cursor=off',
            '-global', 'VGA.untracked-vram=off',
            '-global', f'macio-ide.dma-completion-delay-ns={ide_dma_delay_ns}',
            '-global', f"macio-ide.pio-status-drain={'on' if ide_pio_drain else 'off'}",
            '-prom-env', 'output-device=ttya', '-g', f'{size}x{depth}',
            '-name', f'{name},debug-threads=on',
            '-audiodev', audiodev, '-action', 'reboot=shutdown',
            '-qmp', f'unix:{run}/qmp-control.sock,server=on,wait=off',
            '-qmp', f'unix:{run}/qmp-events.sock,server=on,wait=off',
            '-monitor', f'unix:{run}/hmp.sock,server=on,wait=off',
            '-serial', f'file:{run}/serial.log', '-msg', 'timestamp=on',
            '-drive', f'file={disk},format=raw,media=disk,index=0',
            '-display', f'retroboot,socket={run}/display.sock',
            '-drive', 'if=ide,index=3,media=cdrom,id=cd0,readonly=on',
            '-drive', 'if=ide,index=2,media=cdrom,id=tools0,readonly=on',
            '-nic', 'user,model=sungem' if nic else 'none']
    if timing == 'accelerated':
        args += ['-icount', 'shift=4,sleep=off', '-global', 'VGA.classicmac-boot-handoff=on']
    if rtc:
        args += ['-rtc', rtc]
    if share is not None:
        args += ['-device', f'virtio-9p-pci,fsdev=share0,mount_tag={share_tag}',
                 '-fsdev', f'local,id=share0,security_model=none,path={share}']
    if nvram is not None:
        # Persistent Power Mac NVRAM: an 8 KiB raw image (needs the
        # retroboot macio-nvram patch to survive mac99's NVRAM formatting).
        args += ['-drive', f'if=none,id=pram0,file={nvram},format=raw',
                 '-global', 'macio-nvram.drive=pram0']
    if tablet:
        args += ['-device', f'loader,addr=0x4000000,file={firmware}/ndrvloader',
                 '-prom-env', 'boot-command=init-program go', '-device', 'virtio-tablet-pci']
    return args


def copy_fixture(fixture: Path, dest: Path):
    if dest.exists():
        raise SystemExit(f'{dest} exists; use a fresh run directory')
    subprocess.run(['cp', '--sparse=always', str(fixture), str(dest)], check=True)


def wait_path(path: Path, timeout: float):
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > deadline:
            raise TimeoutError(f'{path} did not appear')
        time.sleep(0.02)


def start_stub(run: Path, tag: str = '') -> subprocess.Popen:
    ctl = run / 'stub.ctl'
    if ctl.exists():
        ctl.unlink()
    out = (run / f'stub{tag}.out').open('ab')
    proc = subprocess.Popen([sys.executable, str(STUB), 'serve', '--socket', str(run / 'display.sock'),
                             '--control', str(ctl), '--log', str(run / 'stub.jsonl'),
                             '--milestones-dir', str(run)],
                            stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
    wait_path(ctl, 10)
    return proc


def start_qemu(args: list[str], run: Path, env: dict | None = None) -> subprocess.Popen:
    (run / 'argv.json').write_text(json.dumps(args, indent=2) + '\n')
    out = (run / 'qemu.out').open('ab')
    proc = subprocess.Popen(args, stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
                            env={**os.environ, **(env or {})})
    (run / 'qemu.pid').write_text(f'{proc.pid}\n')
    return proc


def clean_shutdown(stub: StubClient, qmp: QMP, timeout: float = 60) -> dict | None:
    """Mac OS's own Shut Down: ADB power key, then Return on the dialog."""
    stub('tap', keys=['power'], hold=0.1)
    time.sleep(1.0)
    stub('tap', keys=['ret'], hold=0.08)
    return qmp.wait_event('SHUTDOWN', timeout)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='mode', required=True)
    la = sub.add_parser('launch')
    la.add_argument('--qemu', type=Path, required=True)
    la.add_argument('--firmware', type=Path, required=True)
    la.add_argument('--run', type=Path, required=True)
    la.add_argument('--fixture', type=Path, default=DEFAULT_FIXTURE)
    la.add_argument('--timing', choices=['realtime', 'accelerated'], default='realtime')
    la.add_argument('--size', default='1024x768')
    la.add_argument('--audio', choices=['none', 'wav'], default='none')
    st = sub.add_parser('stop')
    st.add_argument('--run', type=Path, required=True)
    ki = sub.add_parser('kill')
    ki.add_argument('--run', type=Path, required=True)
    a = ap.parse_args()
    run = a.run.resolve()
    if a.mode == 'launch':
        run.mkdir(parents=True, exist_ok=False)
        copy_fixture(a.fixture, run / 'disk.img')
        stub = start_stub(run)
        (run / 'stub.pid').write_text(f'{stub.pid}\n')
        args = reference_argv(a.qemu.resolve(), a.firmware.resolve(), run, run / 'disk.img',
                              timing=a.timing, size=a.size, audio=a.audio)
        q = start_qemu(args, run, {'RETROBOOT_SESSION': run.name})
        print(json.dumps({'run': str(run), 'qemu_pid': q.pid, 'stub_pid': stub.pid}))
    elif a.mode == 'stop':
        qmp = QMP(run / 'qmp-events.sock')
        stub = StubClient(run / 'stub.ctl')
        print(json.dumps(clean_shutdown(stub, qmp)))
    else:
        for name in ['qemu.pid', 'stub.pid']:
            f = run / name
            if f.exists():
                try:
                    os.kill(int(f.read_text()), signal.SIGTERM)
                except ProcessLookupError:
                    pass


if __name__ == '__main__':
    main()
