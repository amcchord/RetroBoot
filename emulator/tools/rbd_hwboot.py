#!/usr/bin/env python3
"""Hardware pre-gray boots (emulator/reports/viacal-20261013.md; ON the device as root, debug
flavor, hostd's guest stopped through the API first and started again afterwards): a second QEMU with
hostd's own argv (ARGV, from /proc/<qemu>/cmdline), changed only in package, VIA machine,
disk snapshot=on, no display/audio/network/CD/battery socket/AirPort card, own QMP.  Each boot runs until
Mac OS's first content (QMP screendump > 5 % non-black and not one flat colour) or TIMEOUT s; a boot without content
is a hang and its registers are kept.  Arms are interleaved.

    rbd_hwboot.py OUT ARGV N TIMEOUT ARM=PKG:VIA [ARM=PKG:VIA ...]     (VIA: cuda | pmu-adb)

ARGV is hostd's argv, one argument per line (tr '\\0' '\\n' < /proc/<qemu>/cmdline).  The Mac
disk is opened snapshot=on and never written.  OUT/results.jsonl has one line per boot.
"""
import json
import os
import signal
import socket
import subprocess
import sys
import time


def build(src, pkg, via, run):
    out, i = [], 0
    while i < len(src):
        a = src[i]
        v = src[i + 1] if i + 1 < len(src) else None
        if i == 0:
            out.append(f'{pkg}/bin/qemu-system-ppc'); i += 1; continue
        if a in ('-qmp', '-serial', '-chardev', '-display', '-nic', '-audiodev'):
            i += 2; continue
        if a == '-M' and v and v.startswith('mac99'):
            parts = [p for p in v.split(',') if not p.startswith('via=')]
            out += [a, ','.join(parts[:1] + [f'via={via}'] + parts[1:])]; i += 2; continue
        if a == '-global' and v and v.startswith('via-pmu.') and (via != 'pmu-adb' or v.startswith(('via-pmu.chardev', 'via-pmu.battery'))):
            i += 2; continue
        if a == '-global' and v and v.startswith(('retroboot-airport.', 'macio-newworld.airport')):
            i += 2; continue        # the AirPort card needs hostd's bridge socket
        if a == '-drive' and v and 'media=cdrom' in v:
            i += 2; continue
        if a == '-drive' and v and 'media=disk' in v:
            out += [a, v + ',snapshot=on']; i += 2; continue
        if a == '-L' and v and v.endswith('/qemu/share/qemu'):
            out += [a, f'{pkg}/share/qemu']; i += 2; continue
        if a == '-device' and v and v.startswith('loader,') and 'ndrvloader' in v:
            out += [a, f'loader,addr=0x4000000,file={pkg}/share/qemu/ndrvloader']; i += 2; continue
        if a == '-fsdev':
            out += [a, f'local,id=share0,security_model=none,path={run}/share']; i += 2; continue
        out.append(a); i += 1
    return out + ['-audiodev', 'none,id=snd0', '-display', 'none', '-nic', 'none',
                  '-qmp', f'unix:{run}/qmp.sock,server=on,wait=off', '-serial', f'file:{run}/serial.log']


def qmp(path, t0):
    while True:
        try:
            s = socket.socket(socket.AF_UNIX); s.connect(path); break
        except OSError:
            if time.time() - t0 > 30:
                raise
            time.sleep(0.05)
    f = s.makefile('rw'); f.readline()

    def cmd(name, **args):
        f.write(json.dumps({'execute': name, 'arguments': args}) + '\n'); f.flush()
        while True:
            r = json.loads(f.readline())
            if 'return' in r or 'error' in r:
                return r
    cmd('qmp_capabilities')
    return cmd


def mac_content(p):
    """Mac OS has drawn: more than 5 % non-black and not a single flat colour (the ATI card
    shows a flat gray before Mac OS; Mac OS's first screen has the Happy Mac on its gray)."""
    data = open(p, 'rb').read()
    px = data.split(b'\n', 3)[3]
    samples = [px[k:k + 3] for k in range(0, len(px) - 2, 3 * 7)]
    nonblack = sum(1 for s in samples if s != b'\0\0\0') / max(len(samples), 1)
    common = max(set(samples), key=samples.count) if samples else b''
    other = sum(1 for s in samples if s != common) / max(len(samples), 1)
    return nonblack > 0.05 and other > 0.001


def main():
    out, argvf, n, timeout = sys.argv[1], sys.argv[2], int(sys.argv[3]), float(sys.argv[4])
    arms = [(s.split('=', 1)[0], *s.split('=', 1)[1].rsplit(':', 1)) for s in sys.argv[5:]]
    src = [l for l in open(argvf).read().split('\n') if l]
    os.makedirs(out, exist_ok=True)
    for k in range(n * len(arms)):
        arm, pkg, via = arms[k % len(arms)]
        run = f'{out}/{arm}-{k:03d}'
        os.makedirs(f'{run}/share', exist_ok=True)
        argv = build(src, pkg, via, run)
        open(f'{run}/argv.json', 'w').write(json.dumps(argv, indent=1))
        t0 = time.time()
        proc = subprocess.Popen(argv, stdout=open(f'{run}/qemu.log', 'w'), stderr=subprocess.STDOUT,
                                start_new_session=True, env=dict(os.environ, EGL_PLATFORM='surfaceless'))
        res = {'arm': arm, 'k': k, 'pkg': pkg, 'via': via, 'first_content_s': None}
        try:
            cmd = qmp(f'{run}/qmp.sock', t0)
            while time.time() - t0 < timeout:
                time.sleep(0.5)
                if proc.poll() is not None:
                    res['exited'] = proc.returncode; break
                p = f'{run}/shot.ppm'
                if 'error' in cmd('screendump', filename=p):
                    continue
                if mac_content(p):
                    res['first_content_s'] = round(time.time() - t0, 2)
                    os.replace(p, f'{run}/first-content.ppm')
                    break
            if res['first_content_s'] is None and proc.poll() is None:
                regs = cmd('human-monitor-command', **{'command-line': 'info registers'}).get('return', '')
                open(f'{run}/registers.txt', 'w').write(regs)
                for line in regs.splitlines():
                    for key in ('NIP', 'SRR0', 'DAR'):
                        if key in line:
                            res.setdefault('regs', []).append(' '.join(line.split()[:8]))
                res['type_b'] = '6806d444' in regs.lower()
        except Exception as e:  # noqa: BLE001
            res['error'] = repr(e)
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
        res['outcome'] = 'content' if res['first_content_s'] is not None else 'hang'
        line = json.dumps(res)
        open(f'{out}/results.jsonl', 'a').write(line + '\n')
        print(line, flush=True)


if __name__ == '__main__':
    main()
