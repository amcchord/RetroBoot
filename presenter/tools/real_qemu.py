#!/usr/bin/env python3
"""Launch the real Mac OS 9 guest against rb-presenter (lab tool, rb-dev).

Mirrors the qemu-display workstream's reference mac99 argv
(emulator/tools/rbd_lab.py) but points -display retroboot at the running
presenter's socket. Every run uses a disposable sparse copy of the fixture.

  real_qemu.py launch --qemu /opt/rbtest/linux-ppc-r1 --fixture /opt/rbtest/os9-original.img
  real_qemu.py launch --usb-mouse ...  # hostd's USB mouse next to the tablet (disks with USB
                                 # Overdrive: right button and wheel notches, pointer.py)
  real_qemu.py launch --lcd ...  # hostd's laptop machine: PMU, built-in panel (Mac OS draws
                                 # its startup background as a 1-pixel dither), backlight,
                                 # PowerBook buttons (r11 and later)
  real_qemu.py shutdown RUN      # Mac OS Shut Down (ADB power key + Return) via QMP
  real_qemu.py restart RUN       # Mac OS Restart (power key + R)
  real_qemu.py kill RUN          # hard stop (guest disk becomes dirty; discard RUN)
"""
import argparse
import json
import os
import pathlib
import signal
import socket
import subprocess
import sys
import time


def qmp(run, *cmds, timeout=10.0):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(str(run / 'qmp-control.sock'))
    f = s.makefile('rw', buffering=1)
    json.loads(f.readline())
    out = []
    for c in (({'execute': 'qmp_capabilities'},) + cmds):
        f.write(json.dumps(c) + '\n')
        f.flush()
        while True:
            r = json.loads(f.readline())
            if 'event' in r:
                continue
            out.append(r)
            break
    s.close()
    return out


def send_key(run, key, hold_ms=100):
    return qmp(run, {'execute': 'send-key', 'arguments': {'keys': [{'type': 'qcode', 'data': key}],
                                                          'hold-time': hold_ms}})


# hostd's laptop machine (hostd/retroboot/keys.py machine_args, POWERBOOK).
LCD = ['-M', 'via=pmu-adb', '-global', 'via-pmu.backlight=on', '-global', 'VGA.builtin-panel=on',
       '-device', 'adb-buttons,bus=adb.0,id=keys0,handler=0x1f']


def argv(qemu, run, disk, size, ram, socket_path, ndrv=None, lcd=False, usb_mouse=False):
    fw = qemu / 'share' / 'qemu'
    # The RetroBoot video driver variant (emulator/ndrv) is searched first, as hostd does.
    data = (['-L', str(ndrv)] if ndrv else []) + ['-L', str(fw)]
    return [str(qemu / 'bin' / 'qemu-system-ppc'), '-accel', 'tcg,tb-size=512',
            '-M', 'mac99,via=cuda,audiodev=snd0', '-cpu', '7400', '-m', str(ram), *data,
            '-vga', 'std', '-global', 'VGA.vgamem_mb=64', '-global', 'VGA.host-resize=on',
            '-global', 'VGA.packed-lowbpp=on', '-global', 'VGA.hardware-cursor=off',
            '-global', 'VGA.untracked-vram=off', '-global', 'macio-ide.dma-completion-delay-ns=0',
            '-global', 'macio-ide.pio-status-drain=on',
            '-prom-env', 'output-device=ttya', '-g', f'{size}x32', '-name', 'retroboot-guest',
            '-audiodev', 'none,id=snd0', '-action', 'reboot=shutdown',
            '-qmp', f'unix:{run}/qmp-control.sock,server=on,wait=off',
            '-qmp', f'unix:{run}/qmp-events.sock,server=on,wait=off',
            '-serial', f'file:{run}/serial.log', '-msg', 'timestamp=on',
            '-drive', f'file={disk},format=raw,media=disk,index=0',
            '-display', f'retroboot,socket={socket_path}',
            '-drive', 'if=ide,index=3,media=cdrom,id=cd0,readonly=on',
            '-drive', 'if=ide,index=2,media=cdrom,id=tools0,readonly=on',
            '-nic', 'user,model=sungem',
            '-device', f'loader,addr=0x4000000,file={fw}/ndrvloader',
            '-prom-env', 'boot-command=init-program go', '-device', 'virtio-tablet-pci'] + (LCD if lcd else []) + \
        (['-device', 'usb-mouse,wheel-per-report=1'] if usb_mouse else [])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='mode', required=True)
    la = sub.add_parser('launch')
    la.add_argument('--qemu', type=pathlib.Path, default=pathlib.Path('/opt/rbtest/linux-ppc-r1'))
    la.add_argument('--fixture', type=pathlib.Path, default=pathlib.Path('/opt/rbtest/os9-original.img'))
    la.add_argument('--runs', type=pathlib.Path, default=pathlib.Path('/opt/rbtest/runs'))
    la.add_argument('--size', default='1024x768', help='initial guest mode (-g)')
    la.add_argument('--ram', type=int, default=512)
    la.add_argument('--socket', default='/run/retroboot/display.sock')
    la.add_argument('--session', default=None)
    la.add_argument('--cold', action='store_true', help='drop host page caches before launching')
    la.add_argument('--disk', type=pathlib.Path, help='reuse this disk instead of a fresh fixture copy')
    la.add_argument('--ndrv', type=pathlib.Path, help='directory with the qemu_vga.ndrv variant (-L first)')
    la.add_argument('--lcd', action='store_true', help="hostd's laptop machine (built-in panel; r11 and later)")
    la.add_argument('--usb-mouse', action='store_true', help="hostd's USB mouse (wheel-per-report=1) for USB Overdrive")
    for name in ('shutdown', 'restart', 'kill', 'status'):
        sp = sub.add_parser(name)
        sp.add_argument('run', type=pathlib.Path)
    a = ap.parse_args()
    if a.mode == 'launch':
        stamp = time.strftime('%Y%m%dT%H%M%S')
        run = a.runs / stamp
        run.mkdir(parents=True)
        disk = run / 'disk.img'
        if a.disk:
            disk = a.disk  # reuse a previous run's (cleanly shut down) disk
        else:
            subprocess.run(['cp', '--sparse=always', str(a.fixture), str(disk)], check=True)
        if a.cold:
            # emulate a cold boot: guest disk pages not in the host page cache
            subprocess.run(['sync'], check=True)
            pathlib.Path('/proc/sys/vm/drop_caches').write_text('3\n')
        args = argv(a.qemu, run, disk, a.size, a.ram, a.socket, a.ndrv, a.lcd, a.usb_mouse)
        (run / 'argv.json').write_text(json.dumps(args, indent=1))
        env = dict(os.environ, RETROBOOT_SESSION=a.session or stamp)
        out = open(run / 'qemu.out', 'ab')
        p = subprocess.Popen(args, stdout=out, stderr=subprocess.STDOUT, start_new_session=True, env=env)
        (run / 'qemu.pid').write_text(f'{p.pid}\n')
        print(json.dumps({'run': str(run), 'pid': p.pid, 'mono': time.monotonic()}))
        return 0
    run = a.run
    if a.mode == 'status':
        print(json.dumps(qmp(run, {'execute': 'query-status'})))
    elif a.mode == 'shutdown':
        send_key(run, 'power')
        time.sleep(1.5)
        send_key(run, 'ret')
    elif a.mode == 'restart':
        send_key(run, 'power')
        time.sleep(1.5)
        send_key(run, 'r')
    elif a.mode == 'kill':
        pid = int((run / 'qemu.pid').read_text())
        os.kill(pid, signal.SIGTERM)
    return 0


if __name__ == '__main__':
    sys.exit(main())
