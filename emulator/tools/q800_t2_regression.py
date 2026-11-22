#!/usr/bin/env python3
"""Exercise actual prepared VIA T2 hold/delivery with a slow translation window.

Compiles the real hold, timer callback and timer-load functions with API stubs.
The 14-block, 300 us/block timeline is the documented 2026-10-24 calibration
regression. No guest ROM or OS is used. --old requires the earlier cap to
deliver an interrupt inside that window; all other cases must pass both ways.
"""
import argparse
from pathlib import Path
import re
import subprocess
import tempfile


def function(source, name):
    match = re.search(r'^static [^\n]*\b' + re.escape(name) + r'\([^;]*?\)\n\{', source, re.M)
    if match is None:
        raise ValueError('Cannot find function ' + name)
    start = match.start()
    depth = 0
    for i in range(source.index('{', start), len(source)):
        if source[i] == '{':
            depth += 1
        elif source[i] == '}':
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
    raise ValueError('Unterminated function ' + name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path, help='prepared QEMU hw/misc directory')
    parser.add_argument('--old', action='store_true')
    parser.add_argument('--cc', default='cc')
    parser.add_argument('--keep', type=Path)
    args = parser.parse_args()
    via = (args.source / 'mac_via.c').read_text()
    maximum = int(re.search(r'parent_obj.t2_hold_max_us,\s*(\d+)\)', via).group(1))
    quiet = int(re.search(r'parent_obj.t2_hold_quiet_us,\s*(\d+)\)', via).group(1))
    mos = (args.source / 'mos6522.c').read_text()
    actual = '\n\n'.join(function(mos, f) for f in ('mos6522_t2_hold', 'mos6522_timer2', 'set_counter'))
    template = Path(__file__).with_suffix('.c.in').read_text()
    program = template.replace('/* @ACTUAL_VIA@ */', actual).replace('@MAXIMUM@', str(maximum)).replace('@QUIET@', str(quiet))
    temp = tempfile.TemporaryDirectory(prefix='q800-t2-') if args.keep is None else None
    output = args.keep or Path(temp.name)
    output.mkdir(parents=True, exist_ok=True)
    c, exe = output / 'q800-t2.c', output / 'q800-t2'
    c.write_text(program)
    subprocess.run([args.cc, '-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                    '-fsanitize=address,undefined', '-fno-sanitize-recover=all',
                    '-fno-omit-frame-pointer', str(c), '-o', str(exe)], check=True)
    result = subprocess.run([str(exe), 'old' if args.old else 'fixed'], capture_output=True, text=True)
    (output / 'result.log').write_text(result.stdout + result.stderr)
    print(f'Q800 maximum={maximum} us, quiet={quiet} us')
    print(result.stdout + result.stderr, end='')
    if temp is not None:
        temp.cleanup()
    return 0 if result.returncode == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
