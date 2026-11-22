#!/usr/bin/env python3
"""Build the OS X VGA driver in an isolated tree using an existing Retro68 SDK."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[2]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--classicmac-source', required=True, type=Path)
    parser.add_argument('--toolchain-bin', required=True, type=Path)
    parser.add_argument('--work-dir', required=True, type=Path)
    args = parser.parse_args()
    lock = json.loads((ROOT / 'emulator/manifests/osx.lock.json').read_text())
    entry = lock['ndrv_build']
    source, work = args.classicmac_source.resolve(), args.work_dir.resolve()
    tools = args.toolchain_bin.resolve()
    if not work.is_relative_to(ROOT / 'build') or work.exists():
        raise SystemExit('Use a new task-owned build directory; existing trees are never reset')
    revision = lock['classicmac']['commit']
    found = subprocess.check_output(['git', '-C', str(source), 'rev-parse', revision], text=True).strip()
    if found != revision:
        raise SystemExit('ClassicMac source revision differs from its pin')
    patch = ROOT / entry['patch']['path']
    if sha(patch) != entry['patch']['sha256']:
        raise SystemExit('OS X NDRV patch differs from its source pin')
    for name in ('powerpc-apple-macos-gcc', 'MakePEF'):
        if not (tools / name).is_file():
            raise SystemExit(f'Missing existing Retro68 tool: {name}')
    work.mkdir(parents=True)
    archive = work / 'source.tar'
    with archive.open('wb') as output:
        subprocess.run(['git', '-C', str(source), 'archive', revision,
                        'ppcvid/driver', 'gxmetal/guest/include', 'gxmetal/protocol'],
                       stdout=output, check=True)
    tree = work / 'source'
    tree.mkdir()
    with tarfile.open(archive) as package:
        package.extractall(tree, filter='data')
    subprocess.run(['git', '-C', str(tree), 'init', '-q'], check=True)
    subprocess.run(['git', '-C', str(tree), 'apply', '--check', str(patch)], check=True)
    subprocess.run(['git', '-C', str(tree), 'apply', str(patch)], check=True)
    env = os.environ.copy()
    env['PATH'] = str(tools) + os.pathsep + env.get('PATH', '')
    subprocess.run(['make', '-C', str(tree / 'ppcvid/driver'), '-j4'], env=env, check=True)
    binary = tree / 'ppcvid/driver/bin/qemu_vga.ndrv'
    if sha(binary) != entry['sha256'] or binary.stat().st_size != entry['bytes']:
        raise SystemExit('Built OS X NDRV differs from the reviewed output; retain this tree for diagnosis')
    record = {'classicmac_commit': revision, 'patch': entry['patch'],
              'binary_sha256': sha(binary),
              'tool_sha256': {name: sha((tools / name).resolve())
                             for name in ('powerpc-apple-macos-gcc', 'MakePEF')},
              'compiler': subprocess.check_output([str(tools / 'powerpc-apple-macos-gcc'), '--version'], text=True).splitlines()[0]}
    (work / 'build-record.json').write_text(json.dumps(record, indent=2) + '\n')
    print(binary)


if __name__ == '__main__':
    main()
