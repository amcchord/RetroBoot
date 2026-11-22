#!/usr/bin/env python3
"""Validate a private machine profile and emit argv; never evaluates a shell."""
import argparse
import json
from pathlib import Path
import re
import subprocess

PROFILE_KEYS={'schema_version','id','package_root','run_dir','cpu','ram_mb','display','audio','media','network','input','share','timing','nvram','airport'}

def keys(value, allowed, label):
    if not isinstance(value,dict) or set(value)-allowed:
        raise ValueError(f'Unknown fields or wrong type in {label}')

def path(value, label, kind='file'):
    p=Path(value)
    if not p.is_absolute() or ',' in value or '\n' in value or '\r' in value:
        raise ValueError(f'{label} requires an absolute path without commas/newlines')
    if kind=='file' and not p.is_file():raise ValueError(f'Missing file: {label}')
    if kind=='dir' and not p.is_dir():raise ValueError(f'Missing directory: {label}')
    return str(p.resolve())

def argv(p):
    keys(p,PROFILE_KEYS,'profile')
    if p['schema_version']!=1 or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,47}',p['id']):raise ValueError('Invalid schema version or machine ID')
    if p['cpu'] not in ['g3','7400']:raise ValueError('Unsupported CPU')
    if type(p['ram_mb']) is not int or not 128<=p['ram_mb']<=1024:raise ValueError('RAM outside prototype bounds')
    package=path(p['package_root'],'package_root','dir');run=path(p['run_dir'],'run_dir','dir')
    bios=Path(package)/'share/qemu';q=path(str(Path(package)/'bin/qemu-system-ppc'),'qemu')
    for name in ['openbios-ppc','qemu_vga.ndrv','ndrvloader']:path(str(bios/name),name)
    # Reject package mixing: each critical binary/firmware must match its build manifest.
    import hashlib
    manifest=json.loads((Path(package)/'manifest.json').read_text())
    for name in ['bin/qemu-system-ppc','share/qemu/openbios-ppc','share/qemu/qemu_vga.ndrv','share/qemu/ndrvloader']:
        actual=hashlib.sha256((Path(package)/name).read_bytes()).hexdigest()
        if actual!=manifest['files'][name]['sha256']:raise ValueError(f'Package hash mismatch: {name}')
    keys(p['display'],{'backend','width','height','depth','socket'},'display');d=p['display']
    if d['backend'] not in ['vnc','sdl','retroboot'] or d['depth'] not in [15,32]:raise ValueError('Unsupported display')
    if 'socket' in d and d['backend']!='retroboot':raise ValueError('display.socket applies to the retroboot backend only')
    display_socket=path(d.get('socket',str(Path(run)/'display.sock')),'display.socket','output') if d['backend']=='retroboot' else None
    for name in ['width','height']:
        if type(d[name]) is not int or not (640 if name=='width' else 480)<=d[name]<=2048:raise ValueError('Display dimensions outside prototype bounds')
    keys(p['audio'],{'backend','wav_path'},'audio');a=p['audio']
    if a['backend'] not in ['none','wav','alsa']:raise ValueError('Unsupported audio backend')
    audio=f"{a['backend']},id=snd0"
    if a['backend']=='wav':audio+=',path='+path(a['wav_path'],'wav_path','output')
    keys(p['media'],{'disk','disk_fixture_id','cd','cd_fixture_id','tools','tools_fixture_id','boot'},'media');m=p['media']
    disk=path(m['disk'],'disk')
    if m['boot'] not in ['disk','cd']:raise ValueError('Invalid boot route')
    boot_cd=m['boot']=='cd'
    if boot_cd and not m.get('cd'):raise ValueError('CD boot requires registered optical media')
    if boot_cd and p['cpu']!='7400':raise ValueError('G3 optical compatibility route is not qualified')
    for name in ['disk_fixture_id']+(['cd_fixture_id'] if m.get('cd') else [])+(['tools_fixture_id'] if m.get('tools') else []):
        if not re.fullmatch(r'[A-Za-z0-9._-]{1,96}',m.get(name,'')):raise ValueError(f'Register a fixture ID for {name}')
    if p['network'] not in ['none','user-sungem','user-airport']:raise ValueError('Unsupported network route')
    if 'airport' in p and p['network']!='user-airport':raise ValueError('airport applies to the user-airport network only')
    if p['network']=='user-airport':
        keys(p.get('airport',{}),{'socket','trace'},'airport')
        if not any(e['path'].endswith('airport-hermes-card.patch') for e in manifest.get('sources',{}).get('retroboot_patches',[])):raise ValueError('This package has no AirPort card')
        airport_socket=path(p.get('airport',{}).get('socket',str(Path(run)/'airport.sock')),'airport.socket','output')
        if type(p.get('airport',{}).get('trace',False)) is not bool:raise ValueError('airport.trace must be boolean')
    if p['input'] not in ['adb','virtio-tablet']:raise ValueError('Unsupported input')
    if p['timing'] not in ['realtime','icount-finder-experimental']:raise ValueError('Unsupported timing')
    if p['nvram']!='volatile':raise ValueError('mac99 persistent NVRAM not implemented')
    keys(p['share'],{'enabled','path','tag'},'share');s=p['share']
    if type(s['enabled']) is not bool:raise ValueError('Share enabled must be boolean')
    if not re.fullmatch(r'[A-Za-z0-9 _-]{1,31}',s.get('tag','')):raise ValueError('Invalid classic volume name')
    sharing=s['enabled'] and not boot_cd
    tools=bool(m.get('tools')) and not boot_cd
    tablet=p['input']=='virtio-tablet'
    args=[q,'-accel','tcg,tb-size=512','-M','mac99,via=cuda,audiodev=snd0','-cpu',p['cpu'],'-m',str(p['ram_mb']),'-L',str(bios),'-vga','std','-global','VGA.vgamem_mb=64','-global','VGA.host-resize=on','-global','VGA.packed-lowbpp=on',
          '-global','VGA.hardware-cursor=off','-global','VGA.untracked-vram=off','-global',f'macio-ide.dma-completion-delay-ns={1000000 if boot_cd else 0}',
          '-prom-env','output-device=ttya','-g',f"{d['width']}x{d['height']}x{d['depth']}",'-name',p['id'],'-audiodev',audio,'-action','reboot=shutdown']
    # The appliance runs two QMP clients (hostd control, guest-launcher events).
    qmp_sockets=['qmp-control.sock','qmp-events.sock'] if d['backend']=='retroboot' else ['qmp.sock']
    for sock in qmp_sockets:args+=['-qmp',f'unix:{run}/{sock},server=on,wait=off']
    args+=['-monitor',f'unix:{run}/hmp.sock,server=on,wait=off','-serial',f'file:{run}/serial.log',
          '-drive',f'file={disk},format=raw,media=disk,index=0']
    # Disk boots: complete OpenBIOS's PIO reads before its 1 ms BSY polls
    # (firmware phase only; DMA/ATAPI and the installer route are unchanged).
    if not boot_cd:args+=['-global','macio-ide.pio-status-drain=on']
    if d['backend']=='vnc':args+=['-display','none','-vnc',f'unix:{run}/vnc.sock']
    elif d['backend']=='retroboot':args+=['-display',f'retroboot,socket={display_socket}']
    else:args+=['-display','sdl,window-close=off']
    if p['timing']=='icount-finder-experimental' and not boot_cd:args+=['-icount','shift=4,sleep=off','-global','VGA.classicmac-boot-handoff=on']
    user_cd=f"if=ide,index={2 if boot_cd else 3},media=cdrom,id=cd0,readonly=on"
    if m.get('cd'):user_cd+=',file='+path(m['cd'],'cd')+',format=raw'
    args+=['-drive',user_cd,'-drive',f"if=ide,index={3 if boot_cd else 2},media=cdrom,id=tools0,readonly=on"]
    if boot_cd:args+=['-boot','d']
    if tools:
        t=path(m['tools'],'tools');args+=['-blockdev',json.dumps({'driver':'file','node-name':'tools-file','filename':t,'read-only':True}),'-blockdev',json.dumps({'driver':'raw','node-name':'tools-disk','file':'tools-file','read-only':True}),'-device','virtio-blk-pci,drive=tools-disk']
    args+=['-nic','user,model=sungem' if p['network']=='user-sungem' else 'none']
    if p['network']=='user-airport':
        # AirPort card (docs/ARCHITECTURE.md section 10): NAT data path plus the
        # host bridge, a reconnecting client of the hostd-owned socket.
        args+=['-netdev','user,id=apnet','-global','macio-newworld.airport=true','-global','retroboot-airport.netdev=apnet',
               '-chardev',f'socket,id=airport0,path={airport_socket},reconnect-ms=1000','-global','retroboot-airport.chardev=airport0']
        if p.get('airport',{}).get('trace'):args+=['-global','retroboot-airport.trace=on']
    if sharing or tablet or tools:
        args+=['-device',f'loader,addr=0x4000000,file={bios}/ndrvloader','-prom-env','boot-command=init-program go']
        if tablet:args+=['-device','virtio-tablet-pci']
        if sharing:
            args+=['-device',f"virtio-9p-pci,fsdev=share0,mount_tag={s['tag']}",'-fsdev',f"local,id=share0,security_model=none,path={path(s['path'],'share path','dir')}"]
    return args

def main():
    ap=argparse.ArgumentParser();ap.add_argument('profile',type=Path);ap.add_argument('--launch',action='store_true');a=ap.parse_args()
    p=json.loads(a.profile.read_text());args=argv(p)
    if a.launch:
        run=Path(p['run_dir'])
        for sock in ['qmp.sock','qmp-control.sock','qmp-events.sock','hmp.sock','vnc.sock','display.sock']:
            if (run/sock).exists():raise SystemExit('Existing run socket: acquire a fresh exclusive run directory')
        (run/'argv.json').write_text(json.dumps(args,indent=2)+'\n')
        with (run/'stderr.log').open('w') as err:
            raise SystemExit(subprocess.call(args,stderr=err))
    print(json.dumps(args,indent=2))

if __name__=='__main__':main()
