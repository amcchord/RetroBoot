#!/usr/bin/env python3
"""manifest.json for the Quadra 800 package (linux-m68k-rN).

    package_manifest_m68k.py PKG PORT_WORK    # also reached through package_manifest.py

Hashes of the shipped files, the m68k lock and the prepared tree's stamp, the
compiler, configure options, the dynamic libraries the binary needs and the
package's features (hostd's capability gates)."""
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
LOCK_PATH = ROOT / 'emulator/manifests/m68k.lock.json'
FILES = ['bin/qemu-system-m68k', 'share/qemu/mac_qfb.rom', 'share/qemu/classicvirtio-68k.rom',
         'share/qemu/q800-pram-seed.img']
# feature -> patch or firmware that provides it (hostd gates on these names)
PATCH_FEATURES = {
    'display-retroboot': 'retroboot-display.patch',
    'display-idle-refresh': 'retroboot-display-idle-refresh.patch',
    'display-relative-pointer': 'retroboot-relative-pointer.patch',
    'char-socket-reconnect': 'qemu-char-socket-reconnect-yank.patch',
    'nubus-qfb-resize': 'retroboot-qfb-resize.patch',
    'nubus-qfb-fit': 'retroboot-qfb-lengths.patch',
}
CLASSICMAC_FEATURES = {
    'q800': 'qfb/integration.patch',
    'nubus-qfb': 'qfb/integration.patch',
    'asc-idle-silence': 'qfb/asc-silence.patch',
    'tcg-code-coverage': 'powermac/tcg-graphics-fast-path.patch',
}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    pkg, work = Path(sys.argv[1]), Path(sys.argv[2])
    # PORT_ARCH=arm64: a cross build, described by the cross compiler, readelf and pkg-config
    arm64 = os.environ.get('PORT_ARCH', 'x86_64') == 'arm64'
    cross = 'aarch64-linux-gnu-' if arm64 else ''
    lock = json.loads(LOCK_PATH.read_text())
    patches = [Path(e['path']).name for e in lock['retroboot_patches']]
    cm = [e['path'] for e in lock['classicmac_patches']]
    features = [f for f, p in CLASSICMAC_FEATURES.items() if p in cm]
    features += [f for f, p in PATCH_FEATURES.items() if p in patches]
    features += [f for f, p in lock.get('feature_patches', {}).items() if p in patches]
    if 'classicvirtio-68k.rom' in lock['firmware']:
        features.append('classicvirtio-68k')
    if 'q800-pram-seed.img' in lock['firmware']:
        features.append('q800-pram-seed')
    needed = [line.split('[')[1].rstrip(']') for line in
              subprocess.check_output([cross + 'readelf', '-d', str(pkg / 'bin/qemu-system-m68k')],
                                      text=True).splitlines()
              if '(NEEDED)' in line]
    configure = (work / 'logs/configure.log')
    manifest = {
        'schema_version': 1,
        'package': lock['package'],
        'flavor': 'm68k',
        'host': {'system': platform.system(), 'machine': platform.machine(), 'platform': platform.platform()},
        'sources': lock,
        'prepared': json.loads((work / 'prepared.json').read_text()),
        'files': {f: {'sha256': sha(pkg / f), 'bytes': (pkg / f).stat().st_size} for f in FILES},
        'compiler': subprocess.check_output([cross + 'gcc' if cross else 'cc', '--version'], text=True).splitlines()[0],
        'build': {'extra_cflags': os.environ.get('PORT_OPT_CFLAGS', '-O2'), 'target_list': lock['target_list'],
                  'display_backend': 'retroboot', 'configure_log_sha256': sha(configure) if configure.exists() else None},
        'features': features,
        'dependencies': subprocess.check_output([cross + 'pkg-config', '--modversion', 'glib-2.0', 'pixman-1', 'slirp',
                                                 'alsa'], text=True).splitlines(),
        'runtime_needed': needed,
        'port_source_files': {str(f.relative_to(ROOT)): sha(f) for f in sorted(
            [LOCK_PATH, *(ROOT / e['path'] for e in lock['retroboot_patches']),
             *(ROOT / 'emulator/scripts').glob('*m68k*'), ROOT / 'emulator/scripts/prepare_sources.py',
             ROOT / 'emulator/qfb/retroboot-gray-rows.patch', ROOT / 'emulator/qfb/build-qfb-rom.sh',
             ROOT / 'emulator/classicvirtio/build-declrom-68k.sh'])},
        'runtime_qualification': 'emulator/reports/quadra-m68k-2026-10-24.md',
    }
    if arm64:
        manifest['arch'] = 'aarch64'
        manifest['build']['cross_prefix'] = cross
        manifest['build']['fp_contract'] = 'off'
    (pkg / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')


if __name__ == '__main__':
    main()
