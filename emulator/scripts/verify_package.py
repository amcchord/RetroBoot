#!/usr/bin/env python3
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import os
import socket
import time
p=Path(sys.argv[1]);q=p/'bin/qemu-system-ppc'
if (p/'bin/qemu-system-m68k').exists() and not q.exists():
    # The Quadra 800 package (linux-m68k-rN) has its own checks (verify_package_m68k.py).
    sys.exit(subprocess.call([sys.executable,str(Path(__file__).with_name('verify_package_m68k.py')),str(p)]))
def output(*args):
    return subprocess.check_output([str(q),*args],text=True,stderr=subprocess.STDOUT)
checks={}
checks['version']=output('--version').splitlines()[0]
assert '11.0.2' in checks['version']
assert 'mac99' in output('-machine','help')
output('-device','screamer,help')
vga=output('-device','VGA,help')
for prop in ['host-resize','packed-lowbpp','hardware-cursor','untracked-vram','classicmac-boot-handoff']:
    assert prop in vga,prop
assert 'gxmetal' not in vga, 'GXMetal must be absent in the baseline build'
for device in ['virtio-9p-pci','virtio-tablet-pci','virtio-blk-pci','loader','sungem']:
    output('-device',f'{device},help')
assert 'dma-completion-delay-ns' in output('-device','macio-ide,help')
assert 'sdl' in output('-display','help')
assert 'retroboot' in output('-display','help')
assert 'pio-status-drain' in output('-device','macio-ide,help')
# OS X uses two 4 KiB flash banks; classic Mac OS keeps its existing layout.
# Exercise both against the actual device, including a backed write/relaunch.
_osx_expected = not (p/'manifest.json').exists() or 'macosx-nvram-1' in json.loads((p/'manifest.json').read_text()).get('features', [])
if _osx_expected:
    assert re.search(r'osx-partitions=<bool>.*default: off', output('-device','macio-nvram,help'))
    import hashlib
    _fwlock = json.loads((Path(__file__).resolve().parents[1]/'manifests/sources.lock.json').read_text())['firmware_build']['openbios']
    assert hashlib.sha256((p/'share/qemu/openbios-ppc-osx').read_bytes()).hexdigest() == _fwlock['osx']['staged_sha256']
    _osx = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1]/'tests/qtest_macosx_nvram.py'),
                           '--qemu', str(q), '--firmware', str(p/'share/qemu'),
                           '--seed-file', str(p/'share/qemu/macosx-nvram-seed.img')],
                          capture_output=True, text=True, timeout=90)
    assert _osx.returncode == 0, _osx.stdout + _osx.stderr
    checks['macosx_nvram'] = json.loads(_osx.stdout)
if 'osx-flash=<bool>' in output('-device', 'macio-nvram,help'):
    _flash = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1]/'tests/qtest_macosx_flash.py'),
                             '--qemu', str(q), '--firmware', str(p/'share/qemu'),
                             '--seed-file', str(p/'share/qemu/macosx-flash-seed.img')],
                            capture_output=True, text=True, timeout=90)
    assert _flash.returncode == 0, _flash.stdout + _flash.stderr
    checks['macosx_native_flash'] = json.loads(_flash.stdout)

# Startup-stall mitigation: empty Cuda interrupt while idle, on by default.
assert re.search(r'idle-irq-ms=<uint32>.*default: 50\b', output('-device','cuda,help'))
# Pre-gray hang fixes (reports/pregray-hang-20261011.md), all on by default.
assert re.search(r'sync-sr-hold=<bool>.*default: on', output('-device','cuda,help'))
assert re.search(r'sr-int-on-access=<bool>.*default: on', output('-device','cuda,help'))
assert re.search(r'coherent-timers=<bool>.*default: on', output('-device','mos6522-cuda,help'))
assert re.search(r'srst-complete-on-access=<bool>.*default: on', output('-device','macio-ide,help'))
# Idle power (reports/idle-power-20261011.md): 32-bit MMU translation contexts, on by default.
assert re.search(r'mmu-contexts=<bool>.*default: on', output('-device','7400_v2.9-powerpc-cpu,help'))
# PowerPC FPU fast paths (reports/ppcfpu-20261012.md): on by default, every part
# (arithmetic helpers, lfs/stfs/fsel inline, AltiVec lanes).
_cpu=output('-device','7400_v2.9-powerpc-cpu,help')
assert re.search(r'fast-fpu=<bool>.*default: on', _cpu)
assert re.search(r'x-fast-fpu-mask=<uint32>.*default: 127\b', _cpu)
assert re.search(r'fast-branch=<bool>.*default: on', _cpu)
assert re.search(r'x-fast-branch-mask=<uint32>.*default: 7\b', _cpu)
# VIA timer 2 holds an interrupt that falls due while the vCPU translates
# (reports/viacal-20261013.md), on by default on both Mac VIAs (Cuda and PMU machines).
for _via in ('mos6522-cuda', 'mos6522-pmu'):
    _h = output('-device', _via + ',help')
    assert re.search(r't2-hold=<bool>.*default: on', _h), _via
    assert re.search(r'x-t2-hold-quiet-us=<uint32>.*default: 100\b', _h), _via
    assert re.search(r'x-t2-hold-max-us=<uint32>.*default: 4000\b', _h), _via
# Hot data pages (reports/hot-data-pages-2026-10-24.md): the TCG accelerator drops a code
# page's TBs after 64 stores that miss them by default; x-hot-data-writes=0 is QEMU's behaviour.
def _jit(accel):
    _in = '\n'.join(json.dumps(c) for c in ({'execute': 'qmp_capabilities'},
          {'execute': 'human-monitor-command', 'arguments': {'command-line': 'info jit'}}, {'execute': 'quit'}))
    return subprocess.run([str(q), '-accel', accel, '-M', 'none', '-display', 'none', '-S', '-qmp', 'stdio'],
                          input=_in + '\n', capture_output=True, text=True, timeout=60).stdout
_hot = _jit('tcg,tb-size=512')
if 'hot-data-writes:' in _hot:      # retroboot-tcg-hot-data-pages.patch is in this build
    assert 'hot-data-writes: 64' in _hot
    assert 'hot-data-writes: 0' in _jit('tcg,tb-size=512,x-hot-data-writes=0')
    checks['hot_data_pages'] = 'default 64, off switch'
# Split 16-byte accesses (image/reports/perf-n5105-2026-10-24.md): without host 16-byte
# loads/stores, lvx/stvx become two inline 8-byte accesses; x-split-i128=off is QEMU's helper call.
if 'split-i128:' in _hot:           # retroboot-tcg-split-i128.patch is in this build
    assert 'split-i128: on' in _hot
    assert 'split-i128: off' in _jit('tcg,tb-size=512,x-split-i128=off')
    checks['split_i128'] = 'default on, off switch'
# Optional: the bare-metal conformance harness (tests/ppcfpu) bit for bit, fast-fpu
# off against on, when a built fpconf.elf is given (powerpc-linux-gnu-gcc needed to build it).
if os.environ.get('FPCONF_ELF'):
    subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1]/'tests/ppcfpu/fpconf.py'), 'conf',
                    '--qemu', str(q), '--elf', os.environ['FPCONF_ELF'], '--count', '300',
                    '--arm', 'off=fast-fpu=off', '--arm', 'on=fast-fpu=on', '--arm', 'nobr=fast-branch=off', '--root', tempfile.mkdtemp(prefix='fpconf-')],
                   check=True)
    checks['ppcfpu_conformance']='equal'
assert re.search(r'idle-frames=<uint32>.*default: 8\b', output('-device','pci-ohci,help'))
assert re.search(r'idle-stop-ms=<uint32>.*default: 1000\b', output('-device','screamer,help'))
# Sound (reports/qtaudio-20261012.md): output DMA at the codec's pace and the frame counter, on by default.
assert re.search(r'paced-dma=<bool>.*default: on', output('-device','screamer,help'))
assert re.search(r'frame-count=<bool>.*default: on', output('-device','screamer,help'))
# the codec clock that does not slip, the adaptive host lead (retroboot-screamer-realtime.patch)
# and the wait for an unserviced sound interrupt (retroboot-screamer-irq-hold.patch)
for prop, default in (('dma-lead-us', 5000), ('resync-ms', 200), ('host-lead-ms', 40), ('host-lead-max-ms', 120),
                      ('irq-hold-ms', 100)):
    assert re.search(prop + r'=<uint32>.*default: %d\)' % default, output('-device','screamer,help')), prop
# the pre-roll that holds through DBDMA program gaps (retroboot-screamer-lead-strict.patch): off unless hostd asks
assert re.search(r'host-lead-strict=<bool>.*default: off', output('-device','screamer,help'))
# USB Overdrive wheel (apps/reports/mouse-2026-10-11.md): QEMU's default stays 127 notches per report.
assert re.search(r'wheel-per-report=<uint32>.*default: 127\b', output('-device','usb-mouse,help'))
# Boot-time fixes (reports/boottime-20261011.md): the ATAPI abort interrupt is on by
# default, and the firmware looks up Forth words natively (OpenBIOS patch).
assert re.search(r'atapi-abort-irq=<bool>.*default: on', output('-device','macio-ide,help'))
_fw=os.environ.get('PORT_FIRMWARE') or (json.loads((p/'manifest.json').read_text()).get('build',{}).get('openbios') if (p/'manifest.json').exists() else 'source')
# PORT_FIRMWARE=copy (arm64) ships an x86 package's source-built firmware, checked against its manifest.
if _fw=='copy':_fw='source'
if _fw=='source':
    assert b'(find-wordlist)' in (p/'share/qemu/openbios-ppc').read_bytes(), 'OpenBIOS without the native find-wordlist'
    checks['openbios_native_find']='present'
# Graphics (research/gfx, experimental): the R100 Radeon model of ati-vga, off unless
# the argv adds -vga none -device ati-vga,model=r100; the firmware names it ATY,Rage6p.
_ati=output('-device','ati-vga,help')
for prop in ['model','guest_hwcursor','x-accel-stats']:
    assert prop in _ati,prop
if 'x-agp=<bool>' in _ati:
    assert re.search(r'x-agp=<bool>.*default: off', _ati)
    assert re.search(r'x-agp-compat=<bool>.*default: off', output('-device','uni-north-pci-pcihost,help'))
    _agp = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[2]/'research/gfx/tests/qtest_agp.py'),
                           '--qemu', str(q), '--support', str(p/'share/qemu')],
                          capture_output=True, text=True, timeout=90)
    assert _agp.returncode == 0 and 'RESULT ok' in _agp.stdout, _agp.stdout + _agp.stderr
    checks['macosx_agp_dma'] = 'pass (no guest driver; acceleration remains unqualified)'
    _cursor = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[2]/'research/gfx/tests/qtest_radeon_cursor.py'),
                              '--qemu', str(q), '--support', str(p/'share/qemu')],
                             capture_output=True, text=True, timeout=90)
    assert _cursor.returncode == 0 and 'RESULT ok' in _cursor.stdout, _cursor.stdout + _cursor.stderr
    checks['radeon_argb_cursor'] = 'pass (synthetic pixels and clipping)'
    checks['radeon_monochrome_cursor'] = 'pass (144 synthetic edge/plane/depth cases and cursor erasure)'
if _fw=='source':
    assert b'ATY,Rage6p' in (p/'share/qemu/openbios-ppc').read_bytes(), 'OpenBIOS without the ATI Radeon node'
    checks['ati_r100']='present'
# Exact Mac screens with the card (research/gfx/reports/gfxcard-2026-10-15.md): the
# emulated monitor's EDID, read over DDC as ATI's driver does, has ati-vga's xres x yres
# as its detailed timing (QEMU 11 built it before the properties were set).
for prop in ['xres','yres']:
    assert prop in _ati,prop
# build-linux.sh verifies before it writes the manifest: a package without one is this lock's
if not (p/'manifest.json').exists() or 'ati-edid-modes' in (json.loads((p/'manifest.json').read_text()).get('features') or []):
    _edid=subprocess.run([sys.executable,str(Path(__file__).resolve().parents[2]/'research/gfx/tests/qtest_r100.py'),
                          '--qemu',str(q),'--extra=-L',f'--extra={p/"share/qemu"}','--edid-only'],
                         capture_output=True,text=True,timeout=300)
    assert _edid.returncode==0 and 'RESULT ok' in _edid.stdout,_edid.stdout[-1500:]
    checks['ati_edid_modes']='pass'
# VNC is a server option, not a -display frontend. Realize a paused mac99
# and query the server through QMP rather than infer it from display help.
with tempfile.TemporaryDirectory(prefix='retroboot-cap-') as tmp:
    sock=Path(tmp)/'qmp.sock'
    proc=subprocess.Popen([str(q),'-M','mac99,via=cuda,audiodev=snd0','-audiodev','none,id=snd0','-L',str(p/'share/qemu'),'-display','none','-vnc',f'unix:{tmp}/vnc.sock','-qmp',f'unix:{sock},server=on,wait=off','-S'],stderr=subprocess.PIPE,text=True)
    try:
        deadline=time.monotonic()+10
        while not sock.exists() and proc.poll() is None and time.monotonic()<deadline:time.sleep(0.1)
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
            client.settimeout(10);client.connect(str(sock));f=client.makefile('rw');json.loads(f.readline())
            for command in ['qmp_capabilities','query-vnc']:
                f.write(json.dumps({'execute':command})+'\n');f.flush()
                reply=json.loads(f.readline());assert 'return' in reply,reply
            assert reply['return']['enabled'],reply
        checks['paused_mac99_vnc_qmp']='pass'
    finally:
        proc.terminate();proc.wait(timeout=10)

def display_socket_contract():
    """-display retroboot against a minimal presenter, with two QMP sockets.

    Checks HELLO, a sealed XRGB8888 memfd SURFACE, the clock GUEST_EVENT,
    RESIZE acknowledgement, both QMP monitors, and reconnection after the
    presenter drops the connection (docs/ARCHITECTURE.md section 3).
    """
    import array,fcntl,os,struct
    result={}
    relative_expected = not (p/'manifest.json').exists() or 'display-relative-pointer' in json.loads((p/'manifest.json').read_text()).get('features', [])
    with tempfile.TemporaryDirectory(prefix='retroboot-rbd-') as tmp:
        path=Path(tmp)/'display.sock';qmps=[Path(tmp)/'qmp-control.sock',Path(tmp)/'qmp-events.sock']
        listener=socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET);listener.bind(str(path));listener.listen(1);listener.settimeout(10)
        args=[str(q),'-M','mac99,via=cuda,audiodev=snd0','-audiodev','none,id=snd0','-L',str(p/'share/qemu'),'-vga','std','-global','VGA.host-resize=on','-display',f'retroboot,socket={path}','-S']
        for s_ in qmps:args+=['-qmp',f'unix:{s_},server=on,wait=off']
        proc=subprocess.Popen(args,stderr=subprocess.PIPE,text=True,env={**os.environ,'RETROBOOT_SESSION':'package-check'})
        def recv(conn):
            data,anc,flags,_=conn.recvmsg(65536,socket.CMSG_SPACE(16))
            fds=array.array('i')
            for level,kind,cdata in anc:
                if level==socket.SOL_SOCKET and kind==socket.SCM_RIGHTS:fds.frombytes(cdata[:len(cdata)-len(cdata)%4])
            mtype,length=struct.unpack_from('=II',data);assert length==len(data)-8,(mtype,length,len(data))
            return mtype,data[8:],list(fds)
        def handshake(conn):
            mtype,payload,_=recv(conn);assert mtype==1,mtype
            version,pid=struct.unpack_from('=II',payload);assert version==1 and pid==proc.pid,(version,pid)
            assert payload[8:48].split(b'\0')[0]==b'package-check'
            mtype,payload,fds=recv(conn);assert mtype==2 and len(fds)==1,(mtype,fds)
            w,h,stride,fmt,size=struct.unpack_from('=IIIIQ',payload)
            assert fmt==0x34325258 and stride>=w*4 and size>=stride*h,(w,h,stride,fmt,size)
            assert os.fstat(fds[0]).st_size==size
            seals=fcntl.fcntl(fds[0],1034)  # F_GET_SEALS
            assert seals&0x6==0x6,seals      # F_SEAL_SHRINK|F_SEAL_GROW
            os.close(fds[0])
            mtype,payload,_=recv(conn);assert mtype==4 and json.loads(payload)['event']=='clock',payload
            if relative_expected:
                mtype,payload,_=recv(conn);event=json.loads(payload)
                assert mtype==4 and event['event']=='pointer-mode' and isinstance(event['relative'],bool),event
                result['relative_pointer']=True
            return [w,h]
        try:
            conn,_=listener.accept();conn.settimeout(10)
            result['surface']=handshake(conn)
            if relative_expected:
                conn.send(struct.pack('=IIii',22,8,-1200,900))
                conn.send(struct.pack('=IIii',22,8,2147483647,-2147483648))
                # A truncated relative payload must not poison the next command.
                conn.send(struct.pack('=III',22,4,1))
            conn.send(struct.pack('=IIII',19,8,800,600))
            mtype,payload,_=recv(conn);event=json.loads(payload)
            assert mtype==4 and event['event']=='resize-request' and event['method']=='ui-info',event
            for s_ in qmps:
                with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as c:
                    c.settimeout(10);c.connect(str(s_));f=c.makefile('rw');json.loads(f.readline())
                    for command in ['qmp_capabilities','query-status']:
                        f.write(json.dumps({'execute':command})+'\n');f.flush();reply=json.loads(f.readline());assert 'return' in reply,reply
            result['qmp_sockets']=len(qmps)
            started=time.monotonic();conn.close()
            conn,_=listener.accept();conn.settimeout(10)
            handshake(conn);result['reconnect_s']=round(time.monotonic()-started,3)
            conn.close()
        finally:
            proc.terminate();proc.wait(timeout=10);listener.close()
    return result
checks['display_socket']=display_socket_contract()

def airport_contract():
    """AirPort card (docs/ARCHITECTURE.md section 10): off by default, its
    properties exist, and a paused mac99 with the card connects to a minimal
    bridge listener, says hello/state, accepts host messages and reconnects."""
    manifest=json.loads((p/'manifest.json').read_text()) if (p/'manifest.json').exists() else {}
    if 'airport-hermes-card.patch' not in json.dumps(manifest.get('sources',{}).get('retroboot_patches',[])) and \
       'airport=' not in output('-device','macio-newworld,help'):
        return 'absent'
    assert 'airport=<bool>' in output('-device','macio-newworld,help')
    props=output('-device','retroboot-airport,help')
    for prop in ['chardev=','netdev=','trace=<bool>','scan-timeout-ms=']:
        assert prop in props,prop
    assert re.search(r'trace=<bool>\s+- on/off \(default: off\)',props), 'card trace must default to off'
    result={}
    with tempfile.TemporaryDirectory(prefix='retroboot-ap-') as tmp:
        path=f'{tmp}/airport.sock'
        listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);listener.bind(path);listener.listen(1);listener.settimeout(10)
        args=[str(q),'-M','mac99,via=cuda,audiodev=snd0','-audiodev','none,id=snd0','-L',str(p/'share/qemu'),'-display','none',
              '-netdev','user,id=apnet','-nic','none','-global','macio-newworld.airport=true','-global','retroboot-airport.netdev=apnet',
              '-chardev',f'socket,id=airport0,path={path},reconnect-ms=100','-global','retroboot-airport.chardev=airport0',
              '-qmp',f'unix:{tmp}/qmp.sock,server=on,wait=off','-S']
        proc=subprocess.Popen(args,stderr=subprocess.PIPE,text=True)
        def lines(conn,n):
            buf=b'';out=[]
            while len(out)<n:
                chunk=conn.recv(4096)
                assert chunk,'bridge closed'
                buf+=chunk
                while b'\n' in buf:
                    line,buf=buf.split(b'\n',1);out.append(json.loads(line))
            return out
        try:
            conn,_=listener.accept();conn.settimeout(10)
            hello,state=lines(conn,2)
            assert hello['type']=='hello' and hello['version']==1 and len(hello['mac'])==17,hello
            assert state['type']=='state' and state['enabled'] is False,state
            for msg in ({'type':'hello','version':1,'backend':'verify'},
                        {'type':'networks','networks':[{'ssid':'5265','bssid':'02:00:00:00:00:01','channel':6,'signal':-40,'noise':-95}]},
                        {'type':'link','state':'associated','ssid':'5265','bssid':'02:00:00:00:00:01'},'not json',{'type':'unknown'}):
                conn.sendall((msg if isinstance(msg,str) else json.dumps(msg)).encode()+b'\n')
            conn.close()
            # The bridge disappears for a while (hostd restart): QEMU 11.0.2 without
            # qemu-char-socket-reconnect-yank.patch aborts on the first failed retry.
            listener.close();os.unlink(path);time.sleep(0.5)
            assert proc.poll() is None,f'QEMU exited while the bridge socket was missing (status {proc.returncode})'
            result['bridge_absent']='survived'
            listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);listener.bind(path);listener.listen(1);listener.settimeout(10)
            started=time.monotonic()
            conn,_=listener.accept();conn.settimeout(10)
            assert lines(conn,1)[0]['type']=='hello'
            result['reconnect_s']=round(time.monotonic()-started,3)
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
                client.settimeout(10);client.connect(f'{tmp}/qmp.sock');f=client.makefile('rw');json.loads(f.readline())
                for command in ['qmp_capabilities','query-status']:
                    f.write(json.dumps({'execute':command})+'\n');f.flush();reply=json.loads(f.readline());assert 'return' in reply,reply
            result['hello']='pass';result['host_messages']='accepted'
            conn.close()
        finally:
            proc.terminate();proc.wait(timeout=10);listener.close()
    return result
checks['airport']=airport_contract()

def pmu_battery_contract():
    """PMU battery (docs/ARCHITECTURE.md section 12): via-pmu properties and
    defaults, and a paused mac99,via=pmu-adb with battery=on connects to a
    minimal power bridge, says hello, accepts host messages, survives the
    bridge's absence and reconnects."""
    manifest=json.loads((p/'manifest.json').read_text()) if (p/'manifest.json').exists() else {}
    props=output('-device','via-pmu,help')
    if 'battery=' not in props and 'retroboot-pmu-battery.patch' not in json.dumps(manifest.get('sources',{})):
        return 'absent'
    for prop,default in (('battery','off'),('battery-trace','off'),('sleep','on'),('adb-reply-cmd','on')):
        assert re.search(prop+r'=<bool>\s+- on/off \(default: '+default+r'\)',props),(prop,default)
    assert 'chardev=' in props
    result={}
    with tempfile.TemporaryDirectory(prefix='retroboot-pw-') as tmp:
        path=f'{tmp}/power.sock'
        listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);listener.bind(path);listener.listen(1);listener.settimeout(10)
        args=[str(q),'-M','mac99,via=pmu-adb,audiodev=snd0','-audiodev','none,id=snd0','-L',str(p/'share/qemu'),'-display','none',
              '-global','via-pmu.battery=on','-chardev',f'socket,id=power0,path={path},reconnect-ms=100',
              '-global','via-pmu.chardev=power0','-qmp',f'unix:{tmp}/qmp.sock,server=on,wait=off','-S']
        proc=subprocess.Popen(args,stderr=subprocess.PIPE,text=True)
        def hello(conn):
            buf=b''
            while b'\n' not in buf:
                chunk=conn.recv(4096);assert chunk,'bridge closed';buf+=chunk
            return json.loads(buf.split(b'\n',1)[0])
        try:
            conn,_=listener.accept();conn.settimeout(10)
            h=hello(conn)
            assert h=={'type':'hello','version':1,'battery':True,'sleeping':False,'backlight':False} or \
                (h=={'type':'hello','version':1,'battery':True,'sleeping':False} and
                 'retroboot-pmu-backlight.patch' not in json.dumps(manifest.get('sources',{}))),h
            for msg in ({'type':'hello','version':1},
                        {'type':'battery','present':True,'ac':False,'charging':False,'percent':57,'time_to_empty_min':143},
                        {'type':'battery','present':True,'ac':True,'charging':True,'percent':58,'time_to_full_min':90,'voltage_mv':12300},
                        {'type':'battery','present':False,'ac':True},{'type':'wake'},'not json',{'type':'unknown'}):
                conn.sendall((msg if isinstance(msg,str) else json.dumps(msg)).encode()+b'\n')
            conn.close()
            listener.close();os.unlink(path);time.sleep(0.5)
            assert proc.poll() is None,f'QEMU exited while the power bridge was missing (status {proc.returncode})'
            result['bridge_absent']='survived'
            listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);listener.bind(path);listener.listen(1);listener.settimeout(10)
            started=time.monotonic()
            conn,_=listener.accept();conn.settimeout(10)
            assert hello(conn)['type']=='hello'
            result['reconnect_s']=round(time.monotonic()-started,3)
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
                client.settimeout(10);client.connect(f'{tmp}/qmp.sock');f=client.makefile('rw');json.loads(f.readline())
                for command in ['qmp_capabilities','query-status']:
                    f.write(json.dumps({'execute':command})+'\n');f.flush();reply=json.loads(f.readline());assert 'return' in reply,reply
            result['hello']='pass';result['host_messages']='accepted'
            conn.close()
        finally:
            proc.terminate();proc.wait(timeout=10);listener.close()
    return result
checks['pmu_battery']=pmu_battery_contract()


def keys_contract():
    """Keys (research/keys): the adb-buttons device and via-pmu.backlight
    (default off); a paused PMU machine with both takes button presses over
    QMP qom-set and refuses unknown buttons."""
    manifest=json.loads((p/'manifest.json').read_text()) if (p/'manifest.json').exists() else {}
    props=output('-device','adb-buttons,help') if 'adb-buttons' in output('-device','help') else ''
    if not props and 'retroboot-adb-buttons.patch' not in json.dumps(manifest.get('sources',{})):
        return 'absent'
    assert re.search(r'handler=<uint8>\s+-\s+\(default: 31\)',props),props
    assert re.search(r'backlight=<bool>\s+- on/off \(default: off\)',output('-device','via-pmu,help'))
    assert re.search(r'builtin-panel=<bool>\s+- on/off \(default: off\)',output('-device','VGA,help'))
    result={}
    with tempfile.TemporaryDirectory(prefix='retroboot-keys-') as tmp:
        args=[str(q),'-M','mac99,via=pmu-adb,audiodev=snd0','-audiodev','none,id=snd0','-L',str(p/'share/qemu'),'-display','none',
              '-global','via-pmu.backlight=on','-device','adb-buttons,bus=adb.0,id=keys0,handler=0x1f',
              '-qmp',f'unix:{tmp}/qmp.sock,server=on,wait=off','-S']
        proc=subprocess.Popen(args,stderr=subprocess.PIPE,text=True)
        try:
            for _ in range(100):
                if os.path.exists(f'{tmp}/qmp.sock'):
                    break
                time.sleep(0.05)
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
                client.settimeout(10);client.connect(f'{tmp}/qmp.sock');f=client.makefile('rw');json.loads(f.readline())
                def qmp(command,arguments=None):
                    f.write(json.dumps({'execute':command,**({'arguments':arguments} if arguments else {})})+'\n');f.flush()
                    return json.loads(f.readline())
                assert 'return' in qmp('qmp_capabilities')
                for value in ('volume-up','mute:down','mute:up','brightness-down'):
                    reply=qmp('qom-set',{'path':'/machine/peripheral/keys0','property':'press','value':value})
                    assert 'return' in reply,(value,reply)
                for value in ('contrast','volume-up:sideways'):
                    reply=qmp('qom-set',{'path':'/machine/peripheral/keys0','property':'press','value':value})
                    assert 'error' in reply,(value,reply)
            result['press']='pass'
        finally:
            proc.terminate();proc.wait(timeout=10)
    return result
checks['keys']=keys_contract()
def screamer_dma_pacing():
    """Mac OS's double-buffered sound DMA must complete one half at a time at
    the codec's pace (QuickTime movies, alerts, Sound control panel), and a
    looping program must take 44,100 frames per second without slipping
    (r12/r13 ran slow, so ALSA ran dry): synthetic DBDMA programs through
    tools/qtest_screamer.py, OpenBIOS only."""
    tool=Path(__file__).resolve().parents[1]/'tools/qtest_screamer.py'
    with tempfile.TemporaryDirectory(prefix='retroboot-dma-') as tmp:
        run=subprocess.run([sys.executable,str(tool),'--qemu',str(q),'--share',str(p/'share/qemu'),
                            '--run',f'{tmp}/run'],capture_output=True,text=True,timeout=180)
        result=json.loads(run.stdout)
        assert run.returncode==0 and result['pass'],result
        return {'half_done_ms':result['half_done_ms'],'frame_stamps':result['frame_stamps'],
                'pace_frames_per_s':result.get('pace_frames_per_s'),
                'hold_frames_per_s':result.get('hold_frames_per_s'),'pass':True}
checks['screamer_dma_pacing']=screamer_dma_pacing()
audio=output('-audio','driver=help')
for backend in ['alsa','none','wav']:
    assert backend in audio, backend
for f in ['openbios-ppc','qemu_vga.ndrv','ndrvloader']:
    assert (p/'share/qemu'/f).is_file(), f
checks.update({'device_properties':'pass','audio_backends':['alsa','none','wav'],'gxmetal':'absent','display_backends':['retroboot','sdl'],'runtime_qualification':'not run'})
print(json.dumps(checks,indent=2))
