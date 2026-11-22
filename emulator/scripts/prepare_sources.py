#!/usr/bin/env python3
"""Prepare a pinned disposable QEMU tree, without resetting any existing checkout."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / 'emulator/manifests/sources.lock.json'

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def run(*args):
    subprocess.run(args, check=True)

def clone(spec, dest):
    if not dest.exists():
        run('git', 'clone', '--depth', '1', '--branch', spec.get('tag', 'main'), spec['url'], str(dest))
    if subprocess.check_output(['git', '-C', str(dest), 'rev-parse', 'HEAD'], text=True).strip() != spec['commit']:
        if subprocess.check_output(['git', '-C', str(dest), 'status', '--porcelain'], text=True):
            raise SystemExit(f'Refusing to change a dirty dependency: {dest}')
        run('git', '-C', str(dest), 'fetch', '--depth', '1', 'origin', spec['commit'])
        run('git', '-C', str(dest), 'checkout', '--detach', spec['commit'])

def prepare_m68k(work, lock_path):
    """Quadra 800 tree (emulator/manifests/m68k.lock.json): ClassicMac's qfb
    files and patches, then the lock's RetroBoot patches, in order.  The
    PowerPC lock and its preparation are not involved."""
    lock=json.loads(lock_path.read_text())
    ppc=json.loads(LOCK.read_text())
    for key in ('classicmac','qemu'):
        if lock[key]!=ppc[key]:
            raise SystemExit(f'm68k lock {key} pin differs from the PowerPC lock')
    q=work/'qemu'; c=work/'classicmac'; stamp=work/'prepared.json'
    retroboot=[(ROOT/e['path'],e['sha256']) for e in lock['retroboot_patches']]
    identity=':'.join([sha(lock_path)]+[sha(path) for path,_ in retroboot])
    if stamp.exists():
        state=json.loads(stamp.read_text())
        if state['identity']!=identity or any(not (q/p).is_file() or sha(q/p)!=v for p,v in state['files'].items()):
            raise SystemExit('Prepared sources changed. Choose a fresh ignored build directory; no reset is performed.')
        print(q)
        return
    clone(lock['classicmac'],c);clone(lock['qemu'],q)
    if subprocess.check_output(['git','-C',str(q),'status','--porcelain'],text=True):
        raise SystemExit('Refusing to patch existing dirty QEMU without matching preparation stamp')
    for asset in lock['assets']:
        src=c/asset['path']
        if sha(src)!=asset['sha256']:
            raise SystemExit(f'Input hash mismatch: {src}')
        dest=q/asset['destination'];dest.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(src,dest)
    for entry in lock['classicmac_patches']:
        patch=c/entry['path']
        if sha(patch)!=entry['sha256']:
            raise SystemExit(f'Patch hash mismatch: {patch}')
        run('git','-C',str(q),'apply',str(patch))
    for path,digest in retroboot:
        if sha(path)!=digest:
            raise SystemExit(f'RetroBoot patch hash mismatch: {path}')
        run('git','-C',str(q),'apply',str(path))
    changed=subprocess.check_output(['git','-C',str(q),'diff','--name-only'],text=True).splitlines()
    changed+=subprocess.check_output(['git','-C',str(q),'ls-files','--others','--exclude-standard'],text=True).splitlines()
    changed+=[a['destination'] for a in lock['assets']]
    state={'identity':identity,'flavor':'m68k','lock':str(lock_path.relative_to(ROOT)),'classicmac_commit':lock['classicmac']['commit'],
           'qemu_commit':lock['qemu']['commit'],'files':{p:sha(q/p) for p in sorted(set(changed))}}
    stamp.write_text(json.dumps(state,indent=2)+'\n')
    print(q)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--work-dir', type=Path, default=ROOT/'build/linux')
    # --lock emulator/manifests/m68k.lock.json prepares the Quadra 800 tree;
    # without it, the PowerPC tree exactly as before.
    ap.add_argument('--lock', type=Path, default=None)
    args=ap.parse_args()
    work=args.work_dir.resolve()
    if not work.is_relative_to(ROOT/'build'):
        raise SystemExit('Source/build work must be under this RetroBoot checkout build/')
    work.mkdir(parents=True, exist_ok=True)
    if args.lock is not None and args.lock.resolve()!=LOCK:
        lock_path=args.lock.resolve()
        if json.loads(lock_path.read_text()).get('flavor')!='m68k':
            raise SystemExit('Unknown lock flavor')
        prepare_m68k(work, lock_path)
        return
    lock=json.loads(LOCK.read_text())
    q=work/'qemu'; c=work/'classicmac'; stamp=work/'prepared.json'
    local_patch=ROOT/lock['patch_application'][lock['patch_order'][-1]]['replacement']
    repair=ROOT/'emulator/patches/classic-nvram-unused-osx-helper.patch'
    # RetroBoot-owned QEMU patches (display backend, ...), applied last, in order.
    retroboot=[(ROOT/e['path'],e['sha256']) for e in lock.get('retroboot_patches',[])]
    identity=':'.join([sha(LOCK),sha(local_patch),sha(repair)]+[sha(path) for path,_ in retroboot])
    if stamp.exists():
        state=json.loads(stamp.read_text())
        if state['identity']!=identity or any(not (q/p).is_file() or sha(q/p)!=v for p,v in state['files'].items()):
            raise SystemExit('Prepared sources changed. Choose a fresh ignored build directory; no reset is performed.')
        print(q)
        return
    clone(lock['classicmac'],c);clone(lock['qemu'],q)
    if subprocess.check_output(['git','-C',str(q),'status','--porcelain'],text=True):
        raise SystemExit('Refusing to patch existing dirty QEMU without matching preparation stamp')
    inventory={p['path']:p for p in lock['inventory']}
    for asset in lock['assets']:
        src=c/asset['path']
        if sha(src)!=asset['sha256']:
            raise SystemExit(f'Input hash mismatch: {src}')
        dest=q/asset['destination'];dest.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(src,dest)
    for name in lock['patch_order']:
        patch=c/name
        if sha(patch)!=inventory[name]['sha256']:
            raise SystemExit(f'Patch hash mismatch: {patch}')
        excludes=lock['patch_application'].get(name,{}).get('exclude',[])
        run('git','-C',str(q),'apply',*(f'--exclude={p}' for p in excludes),str(patch))
    run('git','-C',str(q),'apply',str(local_patch))
    run('git','-C',str(q),'apply',str(repair))
    for path,digest in retroboot:
        if sha(path)!=digest:
            raise SystemExit(f'RetroBoot patch hash mismatch: {path}')
        run('git','-C',str(q),'apply',str(path))
    firmware=q/'pc-bios/openbios-ppc'; data=firmware.read_bytes()
    if data.count(bytes.fromhex('00ffffcc'))!=1:
        raise SystemExit('OpenBIOS palette literal must occur exactly once')
    data=data.replace(bytes.fromhex('00ffffcc'),bytes(4))
    # Same fix as emulator/patches/openbios-sound-objects-terminator.patch for the
    # prebuilt firmware: "sound-objects" is published with 0x3e6 bytes so the list
    # keeps its terminators (li r6,0x3e4 -> 0x3e6 next to the property name load).
    seq=bytes.fromhex('809e80b838c003e438bd0010')
    literal=data.find(b'init operation 2 param 00000001 param-size 4\0')
    if data.count(seq)!=1 or literal<0 or data[literal+990:literal+998]!=b'utPort\0\0':
        raise SystemExit('OpenBIOS sound-objects length site not found exactly once')
    data=data.replace(seq,bytes.fromhex('809e80b838c003e638bd0010'))
    expected=lock.get('firmware_build',{}).get('openbios',{}).get('pinned_staging',{}).get('staged_sha256')
    if expected and hashlib.sha256(data).hexdigest()!=expected:
        raise SystemExit('Staged pinned OpenBIOS does not match the lock')
    firmware.write_bytes(data)
    # Keep the architectural clock and size checks intact on the supported 64-bit host.
    for name in ['hw/ppc/ppc.c','include/hw/ppc/ppc.h']:
        if any(x in (q/name).read_text() for x in ['cntvct_el0','cpu_ppc_fast_tb','fast_tb_']):
            raise SystemExit(f'PowerPC virtual-clock bypass in {name}')
    changed=subprocess.check_output(['git','-C',str(q),'diff','--name-only'],text=True).splitlines()
    changed+=subprocess.check_output(['git','-C',str(q),'ls-files','--others','--exclude-standard'],text=True).splitlines()
    changed+= [a['destination'] for a in lock['assets']]
    state={'identity':identity,'classicmac_commit':lock['classicmac']['commit'],'qemu_commit':lock['qemu']['commit'],'files':{p:sha(q/p) for p in sorted(set(changed))}}
    stamp.write_text(json.dumps(state,indent=2)+'\n')
    print(q)

if __name__=='__main__':
    main()
