#!/usr/bin/env python3
"""Run original bare-PPC FP exception controls without guest disks or ROM inputs."""
import argparse
import json
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import time


# Assembled from our osx_fpu/pending.S and pending.ld, not guest code.
# Keeping the tiny program here makes package checks independent of a cross compiler.
VECTORS = bytes.fromhex(
    "829f00003a940001929f00007e9a02a6929f00283a9400047e9a03a67ea000a6"
    "62b520007ea001244c00012cffc0048edbdf0020fdfefd8e4c000064")
PROGRAM = bytes.fromhex(
    "3fe0002038600000907f0000907f0004386020007c6001244c00012c3c800010"
    "388400f8c8240000c8440008c8640010c8840018c8a40020c8c40028ffe02090"
    "fdfe1d8efc011024fdfe258e386029007c6001244c00012cfc010800807f0000"
    "907f0008fdfe1d8efc011024d81f0018807f0000907f000cfdfe258e38602000"
    "7c6001244c00012cfdfe1d8efc011024fdfe2d8e386029007c6001244c00012c"
    "fc010800807f0000907f0010fdfe358e807f0000907f0014fdfe258e38602000"
    "7c6001244c00012cfdfe358efdfe258e386029007c6001244c00012cfc010800"
    "807f0000907f002c3c60600d6063cafe907f000448000000")


class RSP:
    def __init__(self, path):
        self.s = socket.socket(socket.AF_UNIX)
        self.s.settimeout(5)
        self.s.connect(str(path))

    def receive(self):
        while True:
            value = self.s.recv(1)
            if not value:
                raise EOFError('QEMU GDB connection closed')
            if value == b'$':
                break
        data = b''
        while True:
            c = self.s.recv(1)
            if not c:
                raise EOFError('QEMU GDB packet truncated')
            if c == b'#':
                break
            data += c
        check = self.s.recv(2)
        assert int(check, 16) == sum(data) % 256
        self.s.sendall(b'+')
        return data.decode()

    def send(self, data, response=True):
        b = data.encode()
        self.s.sendall(b'$' + b + b'#' + ('%02x' % (sum(b) % 256)).encode())
        assert self.s.recv(1) == b'+'
        return self.receive() if response else None


def run(binary, support, program, cpu="7400"):
    with tempfile.TemporaryDirectory(prefix='rb-fpreg-') as tmp:
        path = Path(tmp)
        sock = path/'gdb.sock'
        a = [binary, '-M', 'mac99,via=pmu,usb=off,audiodev=snd0',
             '-audiodev', 'none,id=snd0', '-cpu', cpu, '-m', '64',
             '-L', support, '-nodefaults', '-vga', 'none', '-display', 'none',
             '-serial', 'none', '-monitor', 'none', '-S',
             '-gdb', 'unix:'+str(sock)+',server=on,wait=off']
        with open(path/'stderr.log', 'wb') as out:
            p = subprocess.Popen(a, stdout=out, stderr=out)
            try:
                for _ in range(100):
                    if sock.exists():
                        break
                    time.sleep(.02)
                r = RSP(sock)
                r.send('qSupported:multiprocess+;xmlRegisters=ppc')
                for address, data in ((0x700, VECTORS), (0x100000, program)):
                    assert r.send('M%x,%x:%s' % (address,len(data),data.hex())) == 'OK'
                assert r.send('M200000,30:'+bytes(48).hex()) == 'OK'
                entry = 0x100000
                reply=r.send('P40=%08x' % entry)
                assert reply == 'OK', reply
                done = entry + len(PROGRAM) - 4
                assert r.send('Z0,%x,4' % done) == 'OK'
                stop=r.send('c')
                assert stop.startswith('T05'), stop
                data=bytes.fromhex(r.send('m200000,30'))
                words=struct.unpack('>12I',data)
                result=dict(binary=binary,stop=stop,words=['%08x'%v for v in words],
                            stages=list(words[2:6]),done=words[1]==0x600dcafe,
                            fpr_result=data[24:32].hex(),handler_fpscr=data[32:40].hex())
                r.s.close()
                return result
            finally:
                if p.poll() is None:
                    p.terminate()
                p.wait(timeout=10)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--qemu', required=True)
    ap.add_argument('--support', required=True)
    ap.add_argument('--cpu', choices=('7400', '750'), default='7400')
    a = ap.parse_args()
    results = []
    for kind, left, right, mask, sticky, expected_result in (
        ('inexact', 0x3ff0000000000000, 0x4008000000000000, 8,
         0x02000000, '3fd5555555555555'),
        ('underflow', 0x0010000000000000, 0x4008000000000000, 0x20,
         0x08000000, '5ff5555555555555'),
        ('overflow', 0x7fefffffffffffff, 0x0010000000000000, 0x40,
         0x10000000, '5fcfffffffffffff')):
        values = struct.pack('>6Q', left, right, mask, 0, sticky, sticky | mask)
        for delayed in (False, True):
            program = PROGRAM
            if delayed:
                # Retain the enabled condition instead of clearing FPSCR in E.
                old = bytes.fromhex('fdfe358efdfe258e38602900')
                assert program.count(old) == 1
                program = program.replace(old, bytes.fromhex('fdfe358e6000000038602900'))
            result = run(a.qemu, a.support, program + values, a.cpu)
            result.update(kind=kind, delayed_fex=delayed, cpu=a.cpu)
            results.append(result)
            expected = [0, 1, 1, 2, 3 if delayed else 2]
            actual = result['stages'] + [int(result['words'][11], 16)]
            assert result['done'] and actual == expected, json.dumps(result)
            assert result['fpr_result'] == expected_result, json.dumps(result)
            assert int(result['handler_fpscr'], 16) & 0x40000000, json.dumps(result)
    print(json.dumps({'result': 'pass', 'controls': results}, indent=2))


if __name__ == '__main__':
    main()
