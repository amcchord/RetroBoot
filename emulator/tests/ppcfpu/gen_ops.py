#!/usr/bin/env python3
"""Generate the fpconf op stubs (ops.S) and the op list (ops.json).

Every floating-point instruction the PowerPC 7400 implements (plus fsqrt and
fsqrts, which QEMU's 7400 model also accepts) gets one stub per encoding
variant: record forms, compare fields, FPSCR field masks/bits and load/store
addressing forms.  A stub executes exactly one instruction and returns.

Register convention (see run_op in start.S): f1 = A, f2 = B, f3 = C,
f4 = target (preset to a sentinel), r8 = &in[0], r9 = out + 32, r10 = 0.
"""
import json
import sys
from pathlib import Path

# kinds: input generators in fpconf.c
K = {'ab': 0, 'ac': 1, 'abc': 2, 'b': 3, 'cmp': 4, 'cvt': 5, 'rsp': 6,
     'move': 7, 'fpscr': 8, 'lds': 9, 'ldd': 10, 'sts': 11, 'std': 12,
     'none': 13,
     # AltiVec (run through run_vop: v1 = A, v2 = B, v3 = C, v4 = target)
     'vab': 0x20, 'vabc': 0x21, 'vb': 0x22, 'vcmp': 0x23, 'vcvt': 0x24,
     'vcfx': 0x25}
SINGLE = 0x100
LE = 0x200          # AltiVec op run in little-endian mode (run_vop_le, ops_le)


def ops():
    out = []

    def add(name, insn, kind, single=False, le=False):
        out.append((name, insn, K[kind] | (SINGLE if single else 0) |
                    (LE if le else 0)))

    for rc in ('', '.'):
        sfx = '_rc' if rc else ''
        for base, kind in (('fadd', 'ab'), ('fsub', 'ab'), ('fdiv', 'ab')):
            add(base + sfx, f'{base}{rc} %f4,%f1,%f2', kind)
            add(base + 's' + sfx, f'{base}s{rc} %f4,%f1,%f2', kind, True)
        add('fmul' + sfx, f'fmul{rc} %f4,%f1,%f3', 'ac')
        add('fmuls' + sfx, f'fmuls{rc} %f4,%f1,%f3', 'ac', True)
        for base in ('fmadd', 'fmsub', 'fnmadd', 'fnmsub'):
            add(base + sfx, f'{base}{rc} %f4,%f1,%f3,%f2', 'abc')
            add(base + 's' + sfx, f'{base}s{rc} %f4,%f1,%f3,%f2', 'abc', True)
        add('fsel' + sfx, f'fsel{rc} %f4,%f1,%f3,%f2', 'abc')
        add('fres' + sfx, f'fres{rc} %f4,%f2', 'b', True)
        add('frsqrte' + sfx, f'frsqrte{rc} %f4,%f2', 'b')
        add('fsqrt' + sfx, f'fsqrt{rc} %f4,%f2', 'b')
        add('fsqrts' + sfx, f'fsqrts{rc} %f4,%f2', 'b', True)
        add('frsp' + sfx, f'frsp{rc} %f4,%f2', 'rsp')
        add('fctiw' + sfx, f'fctiw{rc} %f4,%f2', 'cvt')
        add('fctiwz' + sfx, f'fctiwz{rc} %f4,%f2', 'cvt')
        for base in ('fmr', 'fneg', 'fabs', 'fnabs'):
            add(base + sfx, f'{base}{rc} %f4,%f2', 'move')
        add('mffs' + sfx, f'mffs{rc} %f4', 'none')
        for flm in (0xff, 0x01, 0x02, 0x80, 0x7f, 0xfe, 0x0f, 0xf0, 0x5a):
            add(f'mtfsf_{flm:02x}{sfx}', f'mtfsf{rc} {flm},%f2', 'fpscr')
        for crf, imm in ((7, 0), (7, 1), (7, 3), (6, 15), (6, 8), (0, 9),
                         (0, 6), (1, 1), (2, 15), (3, 4), (4, 0xa)):
            add(f'mtfsfi_{crf}_{imm:x}{sfx}', f'mtfsfi{rc} {crf},{imm}', 'fpscr')
        for bit in range(32):
            add(f'mtfsb0_{bit}{sfx}', f'mtfsb0{rc} {bit}', 'fpscr')
            add(f'mtfsb1_{bit}{sfx}', f'mtfsb1{rc} {bit}', 'fpscr')
    for crf in (0, 1, 3, 7):
        add(f'fcmpu_cr{crf}', f'fcmpu {crf},%f1,%f2', 'cmp')
        add(f'fcmpo_cr{crf}', f'fcmpo {crf},%f1,%f2', 'cmp')
    for d in range(8):
        for s in range(8):
            if (d, s) in ((0, 0), (1, 1), (3, 5), (7, 3), (2, 7), (5, 0),
                          (6, 1), (4, 2), (0, 4), (1, 6)):
                add(f'mcrfs_{d}_{s}', f'mcrfs {d},{s}', 'fpscr')
    add('lfs', 'lfs %f4,0(%r8)', 'lds')
    add('lfsx', 'lfsx %f4,%r8,%r10', 'lds')
    add('lfsu', 'lfsu %f4,0(%r8)', 'lds')
    add('lfsux', 'lfsux %f4,%r8,%r10', 'lds')
    add('lfd', 'lfd %f4,0(%r8)', 'ldd')
    add('lfdx', 'lfdx %f4,%r8,%r10', 'ldd')
    add('lfdu', 'lfdu %f4,0(%r8)', 'ldd')
    add('lfdux', 'lfdux %f4,%r8,%r10', 'ldd')
    add('stfs', 'stfs %f1,0(%r9)', 'sts')
    add('stfsx', 'stfsx %f1,%r9,%r10', 'sts')
    add('stfsu', 'stfsu %f1,0(%r9)', 'sts')
    add('stfsux', 'stfsux %f1,%r9,%r10', 'sts')
    add('stfd', 'stfd %f1,0(%r9)', 'std')
    add('stfdx', 'stfdx %f1,%r9,%r10', 'std')
    add('stfdu', 'stfdu %f1,0(%r9)', 'std')
    add('stfdux', 'stfdux %f1,%r9,%r10', 'std')
    add('stfiwx', 'stfiwx %f1,%r9,%r10', 'sts')
    # AltiVec floating point (VSCR[NJ] = configuration number & 1)
    for name in ('vaddfp', 'vsubfp', 'vmaxfp', 'vminfp'):
        add(name, f'{name} %v4,%v1,%v2', 'vab')
    add('vmaddfp', 'vmaddfp %v4,%v1,%v3,%v2', 'vabc')
    add('vnmsubfp', 'vnmsubfp %v4,%v1,%v3,%v2', 'vabc')
    for name in ('vrefp', 'vrsqrtefp', 'vexptefp', 'vlogefp', 'vrfin', 'vrfiz',
                 'vrfip', 'vrfim'):
        add(name, f'{name} %v4,%v2', 'vb')
    for name in ('vcmpeqfp', 'vcmpgefp', 'vcmpgtfp', 'vcmpbfp'):
        add(name, f'{name} %v4,%v1,%v2', 'vcmp')
        add(name + '_rc', f'{name}. %v4,%v1,%v2', 'vcmp')
    for uim in (0, 1, 16, 31):
        add(f'vctsxs_{uim}', f'vctsxs %v4,%v2,{uim}', 'vcvt')
        add(f'vctuxs_{uim}', f'vctuxs %v4,%v2,{uim}', 'vcvt')
        add(f'vcfsx_{uim}', f'vcfsx %v4,%v2,{uim}', 'vcfx')
        add(f'vcfux_{uim}', f'vcfux %v4,%v2,{uim}', 'vcfx')
    # AltiVec element loads and stores at a data-dependent offset 0..15
    # (r4 = A's data, r9 = the 16 bytes v4 was loaded from), and vperm
    for suf in ('b', 'h', 'w'):
        add(f'lve{suf}x', 'lbz %r11,3(%r4)\n\tclrlwi %r11,%r11,28\n\t'
            f'lve{suf}x %v4,%r4,%r11', 'vab')
        add(f'stve{suf}x', 'lbz %r11,3(%r4)\n\tclrlwi %r11,%r11,28\n\t'
            f'stve{suf}x %v1,%r9,%r11\n\tlvx %v4,0,%r9', 'vab')
    add('vperm', 'vperm %v4,%v1,%v2,%v3', 'vabc')
    # the same element loads and stores in little-endian mode
    for suf in ('b', 'h', 'w'):
        add(f'lve{suf}x_le', 'lbz %r11,3(%r4)\n\tclrlwi %r11,%r11,28\n\t'
            f'lve{suf}x %v4,%r4,%r11', 'vab', le=True)
        add(f'stve{suf}x_le', 'lbz %r11,3(%r4)\n\tclrlwi %r11,%r11,28\n\t'
            f'stve{suf}x %v1,%r9,%r11\n\tlvx %v4,0,%r9', 'vab', le=True)
    add('seq_vmaddfp_chain', 'vmaddfp %v4,%v1,%v3,%v2\n\tvmaddfp %v4,%v4,%v3,%v2', 'vabc')
    # Two-instruction sequences that compilers emit and the fast path chains:
    add('seq_fmadds_frsp', 'fmadds %f4,%f1,%f3,%f2\n\tfrsp %f4,%f4', 'abc', True)
    add('seq_fmul_fadd', 'fmul %f4,%f1,%f3\n\tfadd %f4,%f4,%f2', 'abc')
    add('seq_fctiwz_stfd', 'fctiwz %f4,%f2\n\tstfd %f4,0(%r9)', 'cvt')
    add('seq_lfs_fmuls', 'lfs %f4,0(%r8)\n\tfmuls %f4,%f4,%f3', 'lds', True)
    return out


def main():
    dest = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('.')
    table = ops()
    lines = ['/* generated by gen_ops.py */', '\t.text']
    le_src = ['/* little-endian stubs before byte reversal (build.sh) */',
              '\t.text']
    for i, (name, insn, kind) in enumerate(table):
        if kind & LE:
            le_src += ['\t.balign 32', f'/* {i}: {name} */', f'\t{insn}', '\tsc']
        else:
            lines += [f'\t.globl op_{i}', f'op_{i}:  /* {name} */', f'\t{insn}',
                      '\tblr']
    lines += ['', '\t.section .rodata', '\t.align 2', '\t.globl op_table',
              'op_table:']
    n_le = 0
    for i, (name, insn, kind) in enumerate(table):
        if kind & LE:
            lines.append(f'\t.long op_le_base + {32 * n_le}, {kind}')
            n_le += 1
        else:
            lines.append(f'\t.long op_{i}, {kind}')
    lines += ['\t.globl op_count', 'op_count:', f'\t.long {len(table)}', '']
    (dest / 'ops.S').write_text('\n'.join(lines))
    (dest / 'ops_le_src.S').write_text('\n'.join(le_src) + '\n')
    (dest / 'ops.json').write_text(json.dumps(
        [{'index': i, 'name': n, 'insn': s, 'kind': k}
         for i, (n, s, k) in enumerate(table)], indent=1))
    print(len(table), 'ops')


if __name__ == '__main__':
    main()
