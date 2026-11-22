#!/usr/bin/env python3
"""Stage wave7/gemini-kernel's handoff for one image build.

    stage-kernel.py HANDOFF.json OUTDIR KERNEL_VERSION

Reads the kernel workstream's handoff (default /srv/retroboot/wave7/handoff/gemini-kernel.json),
copies the boot.img, the modules tarball and System.map into OUTDIR after checking every
SHA-256 the handoff states, and writes OUTDIR/kernel.json in the shape the image scripts use:

    {"build_id", "kernel_release", "handoff": {path, sha256},
     "boot_img": {"file": "boot.img", "sha256", "size_bytes", "source"},
     "modules": {"file", "sha256", "source"}, "system_map": {...} | null,
     "counter": {"file", "reset", "linux_mount", ...}, "cmdline", "source_revision", "notes"}

The handoff's own field names are looked up in a few spellings, so a renamed key does not
silently drop a hash: a stated hash that doesn't match fails the build, and a boot.img or
modules tarball without any stated hash fails it too.
"""
import hashlib
import json
import os
import shutil
import sys


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def first(d, *keys, default=None):
    for k in keys:
        cur = d
        for part in k.split('.'):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur not in (None, ''):
            return cur
    return default


def artifact(h, kind):
    """(path, stated sha256 or None) of the boot.img / modules tarball / System.map."""
    keys = {'boot_img': ('boot_img', 'bootimg', 'boot_image', 'artifacts.boot_img', 'artifacts.boot.img'),
            'modules': ('modules_tarball', 'modules', 'modules_tar', 'artifacts.modules', 'artifacts.modules_tarball'),
            'system_map': ('system_map', 'System.map', 'artifacts.system_map', 'artifacts.System.map')}[kind]
    v = first(h, *keys)
    if v is None and kind == 'system_map':      # {"kernel": {"files": {"System.map": {...}}}}
        v = ((h.get('kernel') or {}).get('files') or {}).get('System.map')
    path = want = None
    if isinstance(v, dict):
        path = first(v, 'path', 'file', 'tarball', 'location')
        want = first(v, 'sha256', 'hash')
    elif isinstance(v, str):
        path = v
    if path and not os.path.isabs(path):
        base = first(h, 'out_dir', 'output_dir', 'artifacts_dir', 'dir') or ''
        path = os.path.join(base, path)
    if path and not want:
        sums = first(h, 'sha256') or {}
        if isinstance(sums, dict):
            want = sums.get(os.path.basename(path)) or sums.get(path) or sums.get(kind)
    return path, want


def main():
    handoff, out, kver = sys.argv[1:4]
    h = json.load(open(handoff))
    os.makedirs(out, exist_ok=True)
    k = {'handoff': {'path': handoff, 'sha256': sha256(handoff)},
         'build_id': first(h, 'build_id', 'kernel_build_id', 'kernel.build_id'),
         'initrd_build_id': first(h, 'initrd.build_id'),
         'kernel_release': first(h, 'kernel_release', 'kernel_version', 'version', 'release', 'kernel.release'),
         'cmdline': first(h, 'cmdline', 'kernel_cmdline', 'kernel.cmdline_compiled_in', 'boot_img.cmdline'),
         'boot_img_header_cmdline': first(h, 'boot_img.header_cmdline'),
         'source_revision': first(h, 'source_revision', 'commit', 'revision', 'retroboot_commit'),
         'branch': first(h, 'branch'), 'status': first(h, 'status'), 'notes': first(h, 'notes')}
    if k['kernel_release'] and str(k['kernel_release']) != kver:
        sys.exit(f'kernel handoff is for {k["kernel_release"]}, the image expects {kver}')
    for kind, name in (('boot_img', 'boot.img'), ('modules', None), ('system_map', 'System.map')):
        path, want = artifact(h, kind)
        if not path:
            if kind == 'system_map':
                k[kind] = None
                continue
            sys.exit(f'kernel handoff names no {kind}')
        if not os.path.isfile(path):
            sys.exit(f'kernel handoff {kind} {path} does not exist')
        got = sha256(path)
        if want and want != got:
            sys.exit(f'kernel handoff {kind} {path}: sha256 {got}, handoff says {want}')
        if not want and kind != 'system_map':
            sys.exit(f'kernel handoff states no sha256 for {kind} ({path})')
        dest = name or os.path.basename(path)
        shutil.copyfile(path, os.path.join(out, dest))
        k[kind] = {'file': dest, 'sha256': got, 'size_bytes': os.path.getsize(path), 'source': path}
    c = first(h, 'counter', 'boot_counter', 'boot_attempts', 'contract.counter') or {}
    if isinstance(c, str):
        c = {'file': c}
    contract = h.get('contract') or {}
    if not c and contract.get('counter_file'):
        # {"contract": {"counter_file": "/.retroboot-boot-attempts at the root of ...", ...}}
        c = {'file': next(w for w in contract['counter_file'].split() if w.startswith('/')), 'reset': '0'}
    k['contract'] = contract or None
    k['counter'] = {'file': first(c, 'file', 'path', 'name') or '/.retroboot-boot-attempts',
                    'reset': str(first(c, 'reset', 'reset_value', 'healthy_value', default='0')),
                    'linux_mount': first(c, 'linux_mount', 'mount', 'outer_root', 'linux_root') or
                                   first(h, 'linux_mount', 'outer_root') or '',
                    'max_attempts': first(c, 'max_attempts', 'limit', 'threshold'),
                    'stated_by_handoff': bool(c)}
    if not k['counter']['file'].startswith('/'):
        k['counter']['file'] = '/' + k['counter']['file']
    if (k['counter']['file'] + '/').startswith('/retroboot/'):
        sys.exit('the boot-attempt counter must live outside /retroboot (it survives a reinstall)')
    json.dump(k, open(os.path.join(out, 'kernel.json'), 'w'), indent=2)
    print(f"kernel {k['kernel_release'] or kver} build {k['build_id']}: boot.img {k['boot_img']['sha256'][:16]}..., "
          f"modules {k['modules']['file']}, counter {k['counter']['file']}", file=sys.stderr)


if __name__ == '__main__':
    main()
