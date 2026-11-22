#!/usr/bin/env python3
"""Exercise actual mac_qfb.c drawing with MMIO injected at the snapshot boundary.

Use a prepared, locked QEMU source file. No Apple ROM, guest OS or disk is used.
The companion C fixture replaces only QEMU APIs and injects the interleaving;
qfb's real pixel readers, row writers, dirty checks and refresh function compile
unchanged from the supplied source. Range assertions and sanitizers stay on.
"""
import argparse
import hashlib
from pathlib import Path
import resource
import signal
import subprocess
import tempfile

CASES = ('unchanged', 'idle', 'wrap', 'base', 'width', 'height', 'depth', 'stride', 'surface')


def draw_path(source):
    start = source.index('static inline uint8_t qfb_read_byte(')
    end = source.index('static uint32_t qfb_calculate_stride(', start)
    return source[start:end]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('source', type=Path, help='prepared QEMU hw/display/mac_qfb.c')
    p.add_argument('--cc', default='cc')
    p.add_argument('--old', action='store_true', help='require base-change failure from the original source')
    p.add_argument('--keep', type=Path, help='retain generated C, binary and logs here')
    args = p.parse_args()
    data = args.source.read_bytes()
    temp = tempfile.TemporaryDirectory(prefix='qfb-snapshot-') if args.keep is None else None
    out = args.keep or Path(temp.name)
    out.mkdir(parents=True, exist_ok=True)
    template = Path(__file__).with_suffix('.c.in').read_text()
    actual = template.replace('/* @ACTUAL_QFB_DRAW_PATH@ */', draw_path(data.decode()))
    c, exe = out / 'qfb-snapshot.c', out / 'qfb-snapshot'
    c.write_text(actual)
    subprocess.run([args.cc, '-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                    '-Wno-unused-function', '-Wno-sign-compare', '-fsanitize=address,undefined', '-fno-sanitize-recover=all',
                    '-fno-omit-frame-pointer', str(c), '-o', str(exe)], check=True)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    results = []
    for case in ('unchanged', 'idle', 'wrap', 'base') if args.old else CASES:
        r = subprocess.run([str(exe), case], capture_output=True, text=True)
        (out / (case + '.log')).write_text(r.stdout + r.stderr)
        if args.old and case == 'base':
            expected = (r.returncode == -signal.SIGABRT and
                        'addr + len <= s->end' in r.stderr)
        else:
            expected = r.returncode == 0
        results.append({'case': case, 'exit': r.returncode, 'expected': expected})
        print(f'{case}: exit={r.returncode}, expected={expected}')
    print('source_sha256=' + hashlib.sha256(data).hexdigest())
    if temp is not None:
        temp.cleanup()
    return 0 if all(r['expected'] for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
