#!/usr/bin/env python3
"""Verify dedicated OS X capabilities; native acceptance is recorded separately."""
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import hashlib

root = Path(__file__).resolve().parents[2]
p = Path(sys.argv[1]).resolve()
q = p / 'bin/qemu-system-ppc'
lock = json.loads((root / 'emulator/manifests/osx.lock.json').read_text())
checks = {}
def output(*args):
    return subprocess.check_output([str(q), *args], text=True, stderr=subprocess.STDOUT)
checks['version'] = output('--version').splitlines()[0]
assert 'version 10.0.13' in checks['version']
assert 'retroboot' in output('-display', 'help') and 'sdl' in output('-display', 'help')
assert 'mac99' in output('-machine', 'help')
for forbidden in ('fast-fpu', 'fast-branch', 'mmu-contexts'):
    assert forbidden not in output('-device', '7400_v2.9-powerpc-cpu,help'), forbidden
assert 'dma-completion-delay-ns' not in output('-device', 'macio-ide,help')
assert re.search(r'osx-flash=<bool>.*default: off', output('-device', 'macio-nvram,help'))
assert re.search(r'x-agp=<bool>.*default: off', output('-device', 'ati-vga,help'))
assert re.search(r'x-coherent-vram=<bool>.*default: off', output('-device', 'ati-vga,help'))
assert re.search(r'x-i8-byte-order=<bool>.*default: off', output('-device', 'ati-vga,help'))
assert re.search(r'x-texture-sampling=<bool>.*default: off', output('-device', 'ati-vga,help'))
assert re.search(r'x-yuv16-aperture-view=<bool>.*default: off', output('-device', 'ati-vga,help'))
assert re.search(r'x-yuv-ram-byte-order=<bool>.*default: off', output('-device', 'ati-vga,help'))
assert re.search(r'x-depth-memory=<bool>.*default: off', output('-device', 'ati-vga,help'))
assert re.search(r'x-selective-2d-sync=<bool>.*default: off', output('-device', 'ati-vga,help'))
assert re.search(r'x-selective-2d-ram=<bool>.*default: off', output('-device', 'ati-vga,help'))
assert re.search(r'macosx-compat=<bool>.*default: off', output('-device', 'usb-tablet,help'))
assert re.search(r'osx-sleep=<bool>.*default: off', output('-device', 'via-pmu,help'))
assert re.search(r'osx-codec=<bool>.*default: off', output('-device', 'screamer,help'))
assert hashlib.sha256((p/'share/qemu/openbios-ppc-osx').read_bytes()).hexdigest() == lock['firmware']['sha256']
asset = lock['ndrv_build']
assert hashlib.sha256((p/'share/qemu/qemu_vga.ndrv').read_bytes()).hexdigest() == asset['sha256']
assert (p/'share/qemu/qemu_vga.ndrv').stat().st_size == asset['bytes']
assert 'xres=<uint32>' in output('-device', 'VGA,help') and 'yres=<uint32>' in output('-device', 'VGA,help')
checks['vga_edid'] = 'pinned source-built driver and VGA EDID properties; native QA recorded separately'
if (p/'manifest.json').exists():
    manifest = json.loads((p/'manifest.json').read_text())
    assert manifest['sources']['qemu'] == lock['qemu']
    assert set(lock['features']) <= set(manifest['features'])
    for name, expected in manifest['files'].items():
        assert hashlib.sha256((p/name).read_bytes()).hexdigest() == expected['sha256'], name
audio_tmp = tempfile.TemporaryDirectory(prefix='retroboot-osx-audio-check-')
for name, script, extra in [
    ('pmu_sleep',root/'emulator/tests/qtest_osx_sleep.py',['--support',str(p/'share/qemu')]),
    ('pmu_migration',root/'emulator/tests/qtest_osx_sleep_migration.py',['--support',str(p/'share/qemu')]),
    ('pmu_hid_wake',root/'emulator/tests/qtest_osx_wake_input.py',['--support',str(p/'share/qemu')]),
    ('msscr0_completion',root/'emulator/tests/qtest_msscr0_7400.py',['--support',str(p/'share/qemu')]),
    ('ppc_fp_pending',root/'emulator/tests/qtest_ppc_fp_pending.py',['--support',str(p/'share/qemu')]),
    ('egl_selection', root/'emulator/tests/qtest_egl_selection.py', ['--support',str(p/'share/qemu')]),
    ('native_flash', root/'emulator/tests/qtest_macosx_flash.py', ['--firmware',str(p/'share/qemu'),'--seed-file',str(p/'share/qemu/macosx-flash-seed.img')]),
    ('agp_dma',root/'research/gfx/tests/qtest_agp.py',['--support',str(p/'share/qemu')]),
    ('aic_pci_dma',root/'research/gfx/tests/qtest_aic.py',['--support',str(p/'share/qemu')]),
    ('mc_2d_upload',root/'research/gfx/tests/qtest_mc2d.py',['--support',str(p/'share/qemu')]),
    ('coherent_vram',root/'research/gfx/tests/qtest_coherent_vram.py',['--support',str(p/'share/qemu'),'--coherent']),
    ('clipping_default',root/'research/gfx/tests/qtest_coherent_vram.py',['--support',str(p/'share/qemu')]),
    ('r100_pixels',root/'research/gfx/tests/qtest_r100.py',['--extra=-L','--extra='+str(p/'share/qemu')]),
    ('r100_pixels_coherent',root/'research/gfx/tests/qtest_r100.py',['--extra=-L','--extra='+str(p/'share/qemu'),'--extra=-global','--extra=ati-vga.x-coherent-vram=on']),
    ('r100_pixels_sampling',root/'research/gfx/tests/qtest_r100.py',['--extra=-L','--extra='+str(p/'share/qemu'),'--device-extra=,x-coherent-vram=on,x-i8-byte-order=on,x-texture-sampling=on']),
    ('i8_byte_order',root/'research/gfx/tests/qtest_i8_byte_order.py',['--support',str(p/'share/qemu'),'--device-extra=,x-coherent-vram=on,x-i8-byte-order=on']),
    ('texture_sampling',root/'research/gfx/tests/qtest_texture_edges.py',['--support',str(p/'share/qemu'),'--device-extra=,x-coherent-vram=on,x-texture-sampling=on']),
    ('texture_requirements',root/'research/gfx/tests/qtest_texture_requirements.py',['--support',str(p/'share/qemu')]),
    ('texture_cache_pages',root/'research/gfx/tests/qtest_texture_cache_pages.py',['--support',str(p/'share/qemu')]),
    ('texture_cache_pages_coherent',root/'research/gfx/tests/qtest_texture_cache_pages.py',['--support',str(p/'share/qemu'),'--coherent']),
    ('target_lifetime_owned',root/'research/gfx/tests/qtest_target_lifetime.py',['--support',str(p/'share/qemu'),'--case','owned']),
    ('target_lifetime_clean',root/'research/gfx/tests/qtest_target_lifetime.py',['--support',str(p/'share/qemu'),'--case','clean']),
    ('target_lifetime_replacement',root/'research/gfx/tests/qtest_target_lifetime.py',['--support',str(p/'share/qemu'),'--case','replacement']),
    ('target_lifetime_growth',root/'research/gfx/tests/qtest_target_lifetime.py',['--support',str(p/'share/qemu'),'--case','growth']),
    ('target_lifetime_reset',root/'research/gfx/tests/qtest_target_lifetime.py',['--support',str(p/'share/qemu'),'--case','reset']),
    ('target_lifetime_pending',root/'research/gfx/tests/qtest_target_lifetime.py',['--support',str(p/'share/qemu'),'--case','pending']),
    ('selective_2d_requirements',root/'research/gfx/tests/qtest_selective2d_requirements.py',['--support',str(p/'share/qemu')]),
    ('ram_source_requirements',root/'research/gfx/tests/qtest_ram_source_requirements.py',['--support',str(p/'share/qemu')]),
    ('ram_source_off',root/'research/gfx/tests/qtest_mc_ram_sync.py',['--support',str(p/'share/qemu'),'--agp']),
    ('ram_source_on',root/'research/gfx/tests/qtest_mc_ram_sync.py',['--support',str(p/'share/qemu'),'--agp','--enabled']),
    ('selective_2d_off',root/'research/gfx/tests/qtest_2d_sync_boundaries.py',['--support',str(p/'share/qemu')]),
    ('selective_2d_on',root/'research/gfx/tests/qtest_2d_sync_boundaries.py',['--support',str(p/'share/qemu'),'--enabled']),
    ('r100_pixels_selective',root/'research/gfx/tests/qtest_r100.py',['--extra=-L','--extra='+str(p/'share/qemu'),'--device-extra=,x-coherent-vram=on,x-selective-2d-sync=on']),
    ('microtile_mip_rows',root/'research/gfx/tests/qtest_texture_mip_pitch.py',['--support',str(p/'share/qemu')]),
    ('microtile_mip_rows_noncoherent',root/'research/gfx/tests/qtest_texture_mip_pitch.py',['--support',str(p/'share/qemu'),'--non-coherent']),
    ('yuv_cpu_view',root/'research/gfx/tests/qtest_yuv_cpu_view.py',['--support',str(p/'share/qemu')]),
    ('yuv_ram_properties',root/'research/gfx/yuv-ram-domain/check_properties.py',['--support',str(p/'share/qemu'),'--out',str(Path(audio_tmp.name)/'yuv_ram_properties')]),
    ('yuv_ram_byte_order',root/'research/gfx/yuv-ram-domain/qtest_domain.py',['--support',str(p/'share/qemu'),'--tests',str(root/'research/gfx/tests'),'--out',str(Path(audio_tmp.name)/'yuv_ram_byte_order')]),
    ('stencil_default',root/'research/gfx/tests/qtest_stencil.py',['--support',str(p/'share/qemu')]),
    ('stencil_coherent',root/'research/gfx/tests/qtest_stencil.py',['--support',str(p/'share/qemu'),'--coherent']),
    ('stencil_depth_memory',root/'research/gfx/tests/qtest_stencil.py',['--support',str(p/'share/qemu'),'--coherent','--depth-memory']),
    ('vertex_normals',root/'research/gfx/tests/qtest_vertex_normals.py',['--support',str(p/'share/qemu'),'--coherent']),
    ('directional_tcl',root/'research/gfx/tests/qtest_tcl_lighting.py',['--support',str(p/'share/qemu'),'--coherent']),
    ('zmask_origin_extent',root/'research/gfx/tests/qtest_zmask_allocation.py',['--support',str(p/'share/qemu')]),
    ('depth_memory',root/'research/gfx/tests/qtest_depth_memory.py',['--support',str(p/'share/qemu'),'--coherent','--depth-memory']),
    ('depth_memory_off_control',root/'research/gfx/tests/qtest_depth_memory.py',['--support',str(p/'share/qemu'),'--coherent','--expect-legacy']),
    ('depth_lifecycle',root/'research/gfx/tests/qtest_depth_lifecycle.py',['--support',str(p/'share/qemu')]),
    ('depth_fail_closed',root/'research/gfx/tests/qtest_depth_errors.py',['--support',str(p/'share/qemu')]),
    ('depth_scanout',root/'research/gfx/tests/qtest_depth_scanout.py',['--support',str(p/'share/qemu')]),
    ('screamer_codec',root/'research/osx-audio/qtest_codec.py',['--share',str(p/'share/qemu'),'--run',str(Path(audio_tmp.name)/'screamer_codec')]),
    ('screamer_fallback',root/'research/osx-audio/qtest_codec.py',['--share',str(p/'share/qemu'),'--run',str(Path(audio_tmp.name)/'screamer_fallback'),'--legacy']),
    ('screamer_s0',root/'research/osx-audio/qtest_s0.py',['--share',str(p/'share/qemu'),'--run',str(Path(audio_tmp.name)/'screamer_s0')]),
    ('screamer_s0_default',root/'research/osx-audio/qtest_s0.py',['--share',str(p/'share/qemu'),'--run',str(Path(audio_tmp.name)/'screamer_s0_default'),'--legacy']),
    ('argb_cursor',root/'research/gfx/tests/qtest_radeon_cursor.py',['--support',str(p/'share/qemu')])]:
    result = subprocess.run([sys.executable,str(script),'--qemu',str(q),*extra],capture_output=True,text=True,timeout=90)
    assert result.returncode == 0, result.stdout+result.stderr
    checks[name] = json.loads(result.stdout) if name == 'native_flash' else 'pass; synthetic device test'
audio_tmp.cleanup()
result = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s',
                         str(root/'research/osx-audio'), '-p', 'test_analyze_pcm.py'],
                        capture_output=True, text=True, timeout=30)
assert result.returncode == 0, result.stdout+result.stderr
checks['screamer_pcm_analysis'] = 'pass; original PCM analysis controls'


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
        args=[str(q),'-M','mac99,via=pmu,usb=off,audiodev=snd0','-audiodev','none,id=snd0','-L',str(p/'share/qemu'),'-vga','std','-global','VGA.romfile=','-display',f'retroboot,socket={path}','-S']
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
            assert mtype==4 and event['event']=='resize-request' and event['method'] in ('ui-info', 'unsupported'),event
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

checks['display_socket'] = display_socket_contract()
checks['native_runtime_qualification'] = 'not run by build'
checks['graphics_acceleration_qualification'] = 'not run by build'
print(json.dumps(checks, indent=2))
