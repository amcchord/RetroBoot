#!/usr/bin/env python3
"""Package checks for the Quadra 800 package (linux-m68k-rN), without Apple code.

    verify_package_m68k.py PKG          # also reached through verify_package.py PKG

Device and property checks (-device help), the firmware files against the m68k
lock, and runtime checks on a paused q800 built with the contract's devices: the
nubus slots, the qfb declaration ROM as QEMU patched it (lengths and page count,
read back over QMP), the presenter handshake on -display retroboot (HELLO, a
sealed XRGB8888 memfd, clock and pointer-mode events, RESIZE acknowledged
through ui-info and latched in the card, re-request, reconnection) and both QMP
sockets.  The paused machine runs a RetroBoot-made stand-in ROM (reset vectors
and a branch to itself), never an Apple ROM.  Prints a JSON summary; exit status
0 only when every check passed.
"""
import array
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
LOCK = json.loads((ROOT / 'emulator/manifests/m68k.lock.json').read_text())
FIRMWARE = ['mac_qfb.rom', 'classicvirtio-68k.rom', 'q800-pram-seed.img']
QFB_SLOT = 0xD                      # after the virtio card (slot C)
QFB_SCREEN_BASE = 0x10000


def stand_in_rom(path: Path):
    """1 MiB: SSP 0x2000, PC 0x40800008, then BRA.S * (no Apple code)."""
    rom = bytearray(1 << 20)
    rom[0:8] = struct.pack('>II', 0x2000, 0x40800008)
    rom[8:10] = bytes.fromhex('60fe')
    path.write_bytes(bytes(rom))


class QMP:
    def __init__(self, path: Path):
        deadline = time.monotonic() + 10
        while True:
            try:
                self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.s.settimeout(10)
                self.s.connect(str(path))
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)
        self.f = self.s.makefile('rw')
        json.loads(self.f.readline())
        self('qmp_capabilities')

    def __call__(self, cmd, **args):
        self.f.write(json.dumps({'execute': cmd, 'arguments': args}) + '\n')
        self.f.flush()
        while True:
            r = json.loads(self.f.readline())
            if 'return' in r:
                return r['return']
            if 'error' in r:
                raise RuntimeError(f'{cmd}: {r["error"]}')

    def hmp(self, line):
        return self('human-monitor-command', **{'command-line': line})


def xp_long(q: 'QMP', addr: int) -> int:
    out = q.hmp(f'xp /1wx {addr:#x}')
    return int(out.split(':')[1].split()[0], 16)


def decl_rom_lengths(rom: bytes) -> dict:
    """minorLength (11) and majorLength (13) of the functional sResources."""
    def target(e):
        v = int.from_bytes(rom[e + 1:e + 4], 'big')
        return e + (v - 0x1000000 if v & 0x800000 else v)
    out = {}
    d = target(len(rom) - 20)
    while rom[d] != 0xff:
        if rom[d] >= 128:
            e = target(d)
            while rom[e] != 0xff:
                if rom[e] in (11, 13):
                    r = target(e)
                    out[rom[e]] = int.from_bytes(rom[r:r + 4], 'big')
                e += 4
        d += 4
    return out


def main():
    pkg = Path(sys.argv[1]).resolve()
    qemu = pkg / 'bin/qemu-system-m68k'
    share = pkg / 'share/qemu'

    def output(*args):
        return subprocess.check_output([str(qemu), *args], text=True, stderr=subprocess.STDOUT)

    checks = {}
    checks['version'] = output('--version').splitlines()[0]
    assert '11.0.2' in checks['version']
    assert re.search(r'^q800\s', output('-machine', 'help'), re.M)
    m = output('-M', 'q800,help')
    for prop in ('audiodev=', 'easc='):
        assert prop in m, prop
    # fb= is an instance property (qfb/integration.patch); the paused machine below uses fb=none
    for dev in ('nubus-qfb', 'nubus-virtio-mmio', 'virtio-tablet-device', 'virtio-9p-device'):
        assert f'name "{dev}"' in output('-device', 'help'), dev
    qfb = output('-device', 'nubus-qfb,help')
    for prop in ('width=', 'height=', 'depth=', 'host-resize-request=', 'mode='):
        assert prop in qfb, prop
    assert re.search(r'vbl-ns=<uint32>.*default: 16625800\b', qfb), 'qfb VBL on the VIA1 tick'
    assert re.search(r'restart-resets=<bool>.*default: off', output('-device', 'm68040-m68k-cpu,help'))
    via1 = output('-device', 'mos6522-q800-via1,help')
    assert re.search(r'adb-idle-poll-ms=<uint32>.*default: 100\b', via1), via1
    assert re.search(r'adb-idle-after-ms=<uint32>.*default: 1000\b', via1), via1
    assert re.search(r'adb-poll-ms=<uint8>.*default: 11\b', via1), via1
    assert re.search(r't2-hold=<bool>.*default: on', via1), via1             # the ROM's calibration
    assert re.search(r'x-t2-hold-quiet-us=<uint32>.*default: 100\b', via1), via1
    assert re.search(r'x-t2-hold-max-us=<uint32>.*default: 20000\b', via1), via1
    output('-device', 'dp8393x,help')           # the Quadra's built-in SONIC (-nic model=dp83932)
    output('-device', 'apple-sound-chip,help')  # the ASC
    assert 'retroboot' in output('-display', 'help')
    audio = output('-audio', 'driver=help')
    for backend in ('alsa', 'none', 'wav'):
        assert backend in audio, backend
    checks['devices'] = 'pass'
    files = {}
    for name in FIRMWARE:
        digest = hashlib.sha256((share / name).read_bytes()).hexdigest()
        assert digest == LOCK['firmware'][name]['sha256'], (name, digest)
        files[name] = digest
    assert (share / 'keymaps').is_dir()
    seed = (share / 'q800-pram-seed.img').read_bytes()
    assert len(seed) in (256, 512) and seed[0x0C:0x10] == b'NuMc', 'PRAM seed without a valid signature'
    checks['firmware'] = files

    with tempfile.TemporaryDirectory(prefix='retroboot-m68k-') as tmp:
        tmp = Path(tmp)
        stand_in_rom(tmp / 'rom.bin')
        (tmp / 'pram.img').write_bytes(seed)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        listener.bind(str(tmp / 'display.sock'))
        listener.listen(1)
        listener.settimeout(10)
        args = [str(qemu), '-M', 'q800,fb=none,audiodev=snd0', '-m', '128', '-bios', str(tmp / 'rom.bin'),
                '-L', str(share), '-drive', f'file={tmp}/pram.img,format=raw,if=mtd',
                '-device', f'nubus-virtio-mmio,romfile={share}/classicvirtio-68k.rom',
                '-device', 'nubus-qfb,width=800,height=600,depth=8,id=qfb0',
                '-device', 'virtio-tablet-device',
                '-device', 'virtio-9p-device,fsdev=share0,mount_tag=RetroBoot Share',
                '-fsdev', f'local,id=share0,security_model=none,path={tmp}',
                '-nic', 'user,model=dp83932', '-audiodev', 'none,id=snd0',
                '-display', f'retroboot,socket={tmp}/display.sock',
                '-qmp', f'unix:{tmp}/qmp-control.sock,server=on,wait=off',
                '-qmp', f'unix:{tmp}/qmp-events.sock,server=on,wait=off',
                '-action', 'reboot=shutdown', '-rtc', 'base=localtime', '-serial', 'null', '-S']
        proc = subprocess.Popen(args, stderr=subprocess.PIPE, text=True,
                                env={**os.environ, 'RETROBOOT_SESSION': 'package-check'})
        try:
            q = QMP(tmp / 'qmp-control.sock')
            QMP(tmp / 'qmp-events.sock')('query-status')
            qtree = q.hmp('info qtree').replace('\r', '')
            slots = dict(re.findall(r'dev: (nubus-[a-z-]+), id "[^"]*"(?:\n[^\n]*?){0,6}?\n\s+slot = \d+ \((0x[0-9a-f]+)\)', qtree))
            assert slots.get('nubus-virtio-mmio') == '0xc' and slots.get('nubus-qfb') == '0xd', slots
            for dev in ('dp8393x', 'apple-sound-chip', 'virtio-tablet-device', 'virtio-9p-device', 'nubus-macfb'):
                assert f'dev: {dev}' in qtree, dev
            checks['slots'] = slots
            # The qfb declaration ROM in slot D, as QEMU patched it at realize.
            top = 0xF0000000 | (QFB_SLOT << 24) | 0xFFFFFF
            size = xp_long(q, top - 15)          # format block: length at size-16
            q('pmemsave', val=top + 1 - size, size=size, filename=str(tmp / 'qfb-rom.bin'))
            lengths = decl_rom_lengths((tmp / 'qfb-rom.bin').read_bytes())
            assert lengths == {11: 0x1000000 - QFB_SCREEN_BASE, 13: 0x2000000 - QFB_SCREEN_BASE}, lengths
            checks['qfb_decl_rom'] = {'bytes': size, 'minor_length': hex(lengths[11]),
                                      'major_length': hex(lengths[13])}
            assert q('qom-get', path='/machine/peripheral/qfb0', property='mode') == '800x600x1'
            # Mac OS's Restart is a system reset on the q800 (compat global)
            assert q('qom-get', path='/machine/cpu', property='restart-resets') is True
            checks['restart_resets'] = True
            # hot data pages (retroboot-tcg-hot-data-pages, shared with the PowerPC series)
            jit = q.hmp('info jit').replace('\r', '')
            if 'hot-data-writes:' in jit:
                assert 'hot-data-writes: 64' in jit, jit
                off = subprocess.run([str(qemu), '-accel', 'tcg,x-hot-data-writes=0', '-M', 'none',
                                      '-display', 'none', '-S', '-qmp', 'stdio'],
                                     input='\n'.join(json.dumps(c) for c in (
                                         {'execute': 'qmp_capabilities'},
                                         {'execute': 'human-monitor-command',
                                          'arguments': {'command-line': 'info jit'}},
                                         {'execute': 'quit'})) + '\n',
                                     capture_output=True, text=True, timeout=60).stdout
                assert 'hot-data-writes: 0' in off, off
                checks['hot_data_pages'] = 'default 64, off switch'

            def recv(conn):
                data, anc, _, _ = conn.recvmsg(65536, socket.CMSG_SPACE(16))
                fds = array.array('i')
                for level, kind, cdata in anc:
                    if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                        fds.frombytes(cdata[:len(cdata) - len(cdata) % 4])
                mtype, length = struct.unpack_from('=II', data)
                assert length == len(data) - 8
                return mtype, data[8:], list(fds)

            def handshake(conn):
                mtype, payload, _ = recv(conn)
                assert mtype == 1 and struct.unpack_from('=II', payload) == (1, proc.pid)
                mtype, payload, fds = recv(conn)
                assert mtype == 2 and len(fds) == 1
                w, h, stride, fmt, size_ = struct.unpack_from('=IIIIQ', payload)
                assert fmt == 0x34325258 and stride >= w * 4 and os.fstat(fds[0]).st_size == size_
                assert fcntl.fcntl(fds[0], 1034) & 0x6 == 0x6
                os.close(fds[0])
                events = []
                for _ in range(2):
                    mtype, payload, _ = recv(conn)
                    assert mtype == 4
                    events.append(json.loads(payload)['event'])
                assert events == ['clock', 'pointer-mode'], events
                return [w, h]

            def resize(conn, w, h):
                conn.send(struct.pack('=IIII', 19, 8, w, h))
                mtype, payload, _ = recv(conn)
                e = json.loads(payload)
                assert mtype == 4 and e['event'] == 'resize-request', e
                return e['method']

            def wait_latched(want):
                deadline = time.monotonic() + 5
                while True:
                    v = q('qom-get', path='/machine/peripheral/qfb0', property='host-resize-request')
                    if v.startswith(want):
                        return v
                    assert time.monotonic() < deadline, f'host request not latched: {want} ({v})'
                    time.sleep(0.05)

            conn, _ = listener.accept()
            conn.settimeout(10)
            checks['surface'] = handshake(conn)
            conn.send(struct.pack('=IIii', 22, 8, 5, -5))          # relative motion (ADB mouse)
            assert resize(conn, 1024, 768) == 'ui-info'
            first = wait_latched('1024x768 serial ')
            assert resize(conn, 1024, 768) == 're-request'          # the card shows 800x600
            again = wait_latched('1024x768 serial ')
            assert int(again.split()[-1]) > int(first.split()[-1]), (first, again)
            checks['resize'] = {'ui_info': first, 're_request': again}
            t = time.monotonic()
            conn.close()
            conn, _ = listener.accept()
            conn.settimeout(10)
            handshake(conn)
            checks['reconnect_s'] = round(time.monotonic() - t, 3)
            conn.close()
            q('quit')
        finally:
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
            listener.close()
    checks['runtime_qualification'] = 'not run (lab: emulator/tools/rbd_quadra_lab.py)'
    print(json.dumps(checks, indent=2))


if __name__ == '__main__':
    main()
