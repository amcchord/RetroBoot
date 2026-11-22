#!/usr/bin/env python3
"""Build a project-authored color launcher beside an unchanged classic game."""
import argparse
import re
import subprocess
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
root = here.parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--toolchain', required=True, type=Path)
parser.add_argument('--out', required=True, type=Path)
parser.add_argument('--name', required=True)
parser.add_argument('--original', required=True)
parser.add_argument('--creator', required=True)
parser.add_argument('--arch', default='ppc', choices=('ppc', '68k'))
parser.add_argument('--depth', required=True, type=int, choices=(1, 4, 8, 16, 32))
args = parser.parse_args()
for name in (args.name, args.original):
    if not name or ':' in name or len(name.encode('mac_roman')) > 31:
        parser.error('Names must be valid 31-byte classic Mac names')
if not re.fullmatch('[A-Za-z][A-Za-z0-9]{3}', args.creator):
    parser.error('Creator must be four ASCII letters/digits, starting with a letter')
args.out.mkdir(parents=True, exist_ok=True)
data = args.original.encode('mac_roman')
config = args.out.resolve() / 'config.r'
config.write_text('data \'RBcf\' (128) { $"' +
                  (bytes([1, args.depth, len(data)]) + data).hex() + '" };\n')
subprocess.run(['cmake', '-S', str(here), '-B', str(args.out / 'cmake'),
                '-DCMAKE_TOOLCHAIN_FILE=' + str(args.toolchain.resolve() /
                    ('m68k-apple-macos/cmake/retro68.toolchain.cmake' if args.arch == '68k'
                     else 'powerpc-apple-macos/cmake/retroppc.toolchain.cmake')),
                '-DCMAKE_BUILD_TYPE=Release', '-DRB_CREATOR=' + args.creator,
                '-DRB_CONFIG_RESOURCE=' + str(config)], check=True)
subprocess.run(['cmake', '--build', str(args.out / 'cmake')], check=True)
subprocess.run([sys.executable, str(root / 'research/gfx/guestapps/make_tree.py'),
                '--out', str(args.out / 'tree'),
                '--arch', args.arch,
                str(args.out / 'cmake/RetroBootColor.bin') + '=' + args.name], check=True)
