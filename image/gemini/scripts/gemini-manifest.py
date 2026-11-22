#!/usr/bin/env python3
"""Add the Gemini PDA's own section to a build manifest (after image/scripts/make-manifest.py).

    gemini-manifest.py MANIFEST.json WORKDIR OUTDIR

manifest["gemini"]: the kernel handoff as staged (build id, boot.img and modules hashes,
boot-attempt counter contract), the pinned upstream files with their hashes, the
installer's rb-unzstd provenance, the Wi-Fi profiles seeded into the root (file names and
checks only, never contents), the payload files, and for the debug flavor the SSH host key
(public key and SHA256 fingerprint, to pin before the first boot).
"""
import json
import os
import subprocess
import sys


def load(path, default=None):
    try:
        return json.load(open(path))
    except FileNotFoundError:
        return default


def main():
    manifest_path, work, out = sys.argv[1:4]
    m = json.load(open(manifest_path))
    for a in m.get('artifacts', []):
        if a.get('file') == 'boot.img':
            a.pop('gpt', None)            # an Android boot image, not a GPT disk
    g = {
        'kernel': load(os.path.join(work, 'kernel', 'kernel.json')),
        'upstream': load(os.path.join(work, 'upstream', 'upstream.json')),
        'rb_unzstd': load(os.path.join(work, 'unzstd', 'rb-unzstd.json')),
        'wifi_profiles': load(os.path.join(work, 'boot', 'wifi-profiles.json'), []),
        'download_sign_in': load(os.path.join(work, 'boot', 'download-sign-in.json'), {'present': False}),
        'payload': sorted(f for f in os.listdir(out) if f.endswith(('.tar.zst', '.img', '.sh', 'SHA256SUMS', 'rb-unzstd'))),
        'sha256sums': open(os.path.join(out, 'SHA256SUMS')).read().splitlines(),
        'ssh_host_key': None,
    }
    pub = os.path.join(work, 'boot', 'ssh_host_ed25519_key.pub')
    if m.get('flavor') == 'debug' and os.path.exists(pub):
        fp = subprocess.run(['ssh-keygen', '-lf', pub], capture_output=True, text=True, check=True).stdout.split()
        g['ssh_host_key'] = {'type': 'ssh-ed25519', 'public_key': ' '.join(open(pub).read().split()[:2]),
                             'fingerprint_sha256': fp[1], 'file': 'ssh_host_ed25519_key.pub'}
    m['gemini'] = g
    json.dump(m, open(manifest_path, 'w'), indent=2)
    open(manifest_path, 'a').write('\n')
    print(f'manifest: gemini section added ({len(g["wifi_profiles"])} Wi-Fi profile(s), '
          f'ssh host key {"yes" if g["ssh_host_key"] else "no"})', file=sys.stderr)


if __name__ == '__main__':
    main()
