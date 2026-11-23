#!/usr/bin/env python3
"""Write the RetroBoot image manifest (package versions, components, hashes)."""
import argparse
import datetime
import hashlib
import json
import os
import pathlib
import platform
import subprocess

_FW = ('Debian non-free-firmware (src:firmware-nonfree, linux-firmware): binary firmware under the '
       'vendor licenses listed in /usr/share/doc/{pkg}/copyright; redistribution of unmodified files '
       'permitted, no source, no modification.')
LICENSE_NOTES = {
    'hfsprogs': 'APSL-2.0; Debian non-free (APSL is not DFSG-free). Redistributable under APSL terms.',
    'intel-microcode': 'Debian non-free-firmware: Intel microcode license (redistribution of unmodified binaries permitted).',
    'amd64-microcode': 'Debian non-free-firmware: AMD microcode license (LICENSE.amd-ucode; redistribution of unmodified binaries permitted).',
    'firmware-sof-signed': 'Debian non-free-firmware: Intel SOF signed firmware (BSD-3-Clause source, signed binaries redistributable unmodified).',
}
for _pkg, _what in {
    'firmware-intel-graphics': 'Intel i915/xe GuC/HuC/DMC (LICENSE.i915)',
    'firmware-iwlwifi': 'Intel Wi-Fi/Bluetooth (LICENCE.iwlwifi_firmware)',
    'firmware-amd-graphics': 'AMD amdgpu/radeon GPU, display, video and PSP firmware (LICENSE.amdgpu, LICENSE.radeon)',
    'firmware-mediatek': 'MediaTek mt76/mt79xx Wi-Fi and Bluetooth (LICENCE.mediatek, LICENCE.ralink_a_mediatek_company_firmware)',
    'firmware-realtek': 'Realtek rtw88/rtw89/rtl8xxxu Wi-Fi, Bluetooth and r8169 NIC (LICENCE.rtlwifi_firmware.txt and others)',
    'firmware-atheros': 'Qualcomm Atheros ath9k_htc/ath10k/ath11k/ath12k Wi-Fi and Bluetooth (LICENCE.atheros_firmware, LICENSE.QualcommAtheros_ath10k)',
    'firmware-misc-nonfree': 'miscellaneous devices: NVIDIA, Intel ISH/VPU, TI audio amplifiers, Marvell, others (per-file licenses in WHENCE)',
    'firmware-cirrus': 'Cirrus Logic CS35L41/CS35L54/CS35L56 laptop speaker amplifiers: DSP firmware and per-model tuning (LICENSE.cirrus, Debian binary-redist-Cirrus)',
    'firmware-brcm80211': 'Broadcom/Cypress brcmfmac Wi-Fi (LICENCE.broadcom_bcm43xx, LICENCE.cypress)',
}.items():
    LICENSE_NOTES[_pkg] = f'{_what}. ' + _FW.format(pkg=_pkg)


def sha256(path, chunk=8 << 20):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def cached_sha256(path, cache_dir):
    st = os.stat(path)
    key = hashlib.sha256(f'{os.path.realpath(path)}:{st.st_size}:{st.st_mtime_ns}'.encode()).hexdigest()[:24]
    cache = pathlib.Path(cache_dir, f'sha256-{key}')
    if cache.exists():
        return cache.read_text().strip()
    digest = sha256(path)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(digest)
    return digest


def gpt_layout(img):
    out = subprocess.run(['sgdisk', '-p', img], capture_output=True, text=True).stdout
    parts = []
    for line in out.splitlines():
        cols = line.split()
        if cols and cols[0].isdigit() and len(cols) >= 7:
            num = int(cols[0])
            info = subprocess.run(['sgdisk', '-i', str(num), img], capture_output=True, text=True).stdout
            guid = next((l.split(':', 1)[1].strip().split()[0] for l in info.splitlines()
                         if l.startswith('Partition unique GUID')), None)
            parts.append({'number': num, 'start_sector': int(cols[1]), 'end_sector': int(cols[2]),
                          'type_code': cols[5], 'name': ' '.join(cols[6:]), 'partuuid': guid})
    return parts


def mbr_layout(img):
    """Partition entries of an MBR image (the universal medium; scripts/universal.py)."""
    with open(img, 'rb') as f:
        s0 = f.read(512)
    parts = []
    for i in range(4):
        e = s0[446 + 16 * i:462 + 16 * i]
        start, count = int.from_bytes(e[8:12], 'little'), int.from_bytes(e[12:16], 'little')
        if e[4]:
            parts.append({'number': i + 1, 'type': f'{e[4]:#04x}', 'bootable': e[0] == 0x80,
                          'start_sector': start, 'sectors': count})
    return {'disk_id': s0[440:444][::-1].hex(), 'partitions': parts}


def read_packages(path):
    packages = []
    for line in pathlib.Path(path).read_text().splitlines():
        name, version, arch, section, source = (line.split('\t') + [''] * 5)[:5]
        component = section.split('/', 1)[0] if '/' in section else 'main'
        entry = {'name': name, 'version': version, 'arch': arch, 'section': section,
                 'component': component, 'source': source or name}
        if name in LICENSE_NOTES:
            entry['license_note'] = LICENSE_NOTES[name]
        packages.append(entry)
    return packages


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True)
    ap.add_argument('--build-id', required=True)
    ap.add_argument('--flavor', required=True)
    ap.add_argument('--private', default='0')
    ap.add_argument('--rootfs', required=True)
    ap.add_argument('--packages', required=True)
    ap.add_argument('--boot', required=True)
    ap.add_argument('--source-revision', default='unknown')
    ap.add_argument('--machine-dir', action='append', default=[],
                    help='embedded machine directory (repeatable; empty values are ignored)')
    ap.add_argument('--guest-id', action='append', default=[],
                    help='registered guest fixture id of the --machine-dir at the same position, or empty')
    ap.add_argument('--factory-catalog', type=pathlib.Path, help='pinned public factory Mac archives in startup order')
    ap.add_argument('--artifact', action='append', default=[])
    ap.add_argument('--arm64-rootfs', help='the universal medium\'s arm64 live root (build.sh --universal)')
    ap.add_argument('--arm64-packages')
    ap.add_argument('--arm64-boot')
    a = ap.parse_args()
    root = pathlib.Path(a.rootfs)
    cache_dir = os.path.join(os.environ.get('RB_WORK_ROOT', '/srv/retroboot/wave2/image'), 'cache', 'hashes')

    packages = read_packages(a.packages)
    kernels = sorted(p.name for p in (root / 'lib/modules').iterdir())
    manifest = {
        'schema': 1,
        'product': 'RetroBoot',
        'release': json.loads((root / 'usr/lib/retroboot/release.json').read_text()),
        'build_id': a.build_id,
        'flavor': a.flavor,
        'private': a.private == '1',
        'built_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'image_source_revision': a.source_revision,
        'builder': {'host': platform.node(), 'debian_version': pathlib.Path('/etc/debian_version').read_text().strip(),
                    'mmdebstrap': subprocess.run(['mmdebstrap', '--version'], capture_output=True, text=True).stdout.strip()},
        'kernel': kernels,
        'boot': json.loads(pathlib.Path(a.boot).read_text()),
        'components': json.loads((root / 'usr/lib/retroboot/components.json').read_text()),
        'packages': packages,
        'non_free_packages': [p for p in packages if p['component'] != 'main'],
        'artifacts': [],
    }
    embedded = []
    for i, mdir in enumerate(a.machine_dir):
        if not mdir:
            continue
        files = {}
        for f in sorted(pathlib.Path(mdir).iterdir()):
            st = f.stat()
            files[f.name] = {'size_bytes': st.st_size, 'allocated_bytes': st.st_blocks * 512,
                             'sha256': cached_sha256(f, cache_dir)}
        guest = a.guest_id[i] if i < len(a.guest_id) else ''
        embedded.append({'id': pathlib.Path(mdir).name, 'source': mdir, 'files': files, 'guest_fixture': guest or None})
    if embedded:
        manifest['embedded_machine'] = embedded[0]        # the startup Mac (older readers)
        manifest['embedded_machines'] = embedded          # every embedded Mac, in install order
    if a.factory_catalog:
        manifest['factory_machines'] = json.loads(a.factory_catalog.read_text())['machines']
    for art in a.artifact:
        st = os.stat(art)
        entry = {'file': os.path.basename(art), 'size_bytes': st.st_size, 'allocated_bytes': st.st_blocks * 512,
                 'sha256': cached_sha256(art, cache_dir)}
        if art.endswith('.img') and ('-universal-' in os.path.basename(art) or
                                     manifest['boot'].get('partition_table') == 'dos'):
            entry['mbr'] = mbr_layout(art)
        elif art.endswith('.img'):
            entry['gpt'] = gpt_layout(art)
        manifest['artifacts'].append(entry)
    if a.arm64_rootfs:
        arm = pathlib.Path(a.arm64_rootfs)
        manifest['arm64'] = {
            'release': json.loads((arm / 'usr/lib/retroboot/release.json').read_text()),
            'kernel': sorted(p.name for p in (arm / 'lib/modules').iterdir()),
            'boot': json.loads(pathlib.Path(a.arm64_boot).read_text()) if a.arm64_boot else None,
            'components': json.loads((arm / 'usr/lib/retroboot/components.json').read_text()),
            'packages': read_packages(a.arm64_packages) if a.arm64_packages else [],
        }
    pathlib.Path(a.out).write_text(json.dumps(manifest, indent=2) + '\n')
    print(f'manifest: {a.out}')


if __name__ == '__main__':
    main()
