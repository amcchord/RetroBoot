/*
 * fpconf benchmark mode (P_MODE = 3): times each kernel in bench.S with the
 * timebase (25 MHz on mac99) and stores { iters, ops per iteration,
 * ticks hi, ticks lo } per kernel at BENCH_BASE.  Kernel names are in
 * bench_names.txt, in the same order.
 */
#include <stdint.h>
#include "fpconf.h"

typedef uint32_t u32;
typedef uint64_t u64;

extern u64 read_tb(void);
typedef void kernel_fn(u32 iters, void *data);
extern kernel_fn bench_fadd, bench_fmul, bench_fmadd, bench_fmadds, bench_fdiv,
    bench_cvt, bench_ldst, bench_cmp, bench_xform, bench_norm, bench_math,
    bench_vxform, bench_vlight, bench_int, bench_mixer, bench_intlfs;

#define REG32(a) (*(volatile u32 *)(a))
#define DATA 0x02000000u

struct kernel {
    kernel_fn *fn;
    u32 ops;        /* FP instructions per iteration */
    u32 scale;      /* iteration divisor (inner loops) */
};

static const struct kernel kernels[] = {
    { bench_fadd, 4, 1 },
    { bench_fmul, 4, 1 },
    { bench_fmadd, 8, 1 },
    { bench_fmadds, 8, 1 },
    { bench_fdiv, 4, 1 },
    { bench_cvt, 4, 1 },
    { bench_ldst, 8, 1 },
    { bench_cmp, 4, 1 },
    { bench_xform, 256 * 24, 256 },
    { bench_norm, 256 * 21, 256 },
    { bench_math, 14, 1 },
    { bench_vxform, 256 * 4, 256 },
    { bench_vlight, 256 * 7, 256 },
    { bench_int, 8, 1 },
    { bench_mixer, 256 * 3, 256 },
    { bench_intlfs, 10, 1 },
};
#define NKERNELS (sizeof(kernels) / sizeof(kernels[0]))

static void put64(u32 addr, u64 v)
{
    REG32(addr) = (u32)(v >> 32);
    REG32(addr + 4) = (u32)v;
}

static u32 fbits(int sign, int exp, u32 frac)
{
    return ((u32)sign << 31) | ((u32)(exp + 127) << 23) | (frac & 0x7fffff);
}

void bench_main(void)
{
    u32 iters = REG32(PARAM_BASE + P_BENCH_ITERS);
    u32 mask = REG32(PARAM_BASE + P_BENCH_MASK);
    u32 k, i;
    /* doubles: 1, 0.5, x, c0..c7 (alternating Taylor-like), 1000.25, 2/pi, 2^52+2^31 */
    static const u64 dconst[] = {
        0x3ff0000000000000ULL, 0x3fe0000000000000ULL, 0x3fe921fb54442d18ULL,
        0x3ff0000000000000ULL, 0xbfc5555555555555ULL, 0x3f81111111111111ULL,
        0xbf2a01a01a01a01aULL, 0x3ec71de3a556c734ULL, 0xbe5ae64567f544e4ULL,
        0x3de6124613a86d09ULL, 0x3ff0000000000000ULL, 0x408f420000000000ULL,
        0x3fe45f306dc9c883ULL, 0x4330000080000000ULL,
    };

    if (!iters) {
        iters = 1000000;
    }
    for (i = 0; i < sizeof(dconst) / sizeof(dconst[0]); i++) {
        put64(DATA + i * 8, dconst[i]);
    }
    /* matrix: rotation-like values near 1, 0.5, 0.25 */
    for (i = 0; i < 16; i++) {
        REG32(DATA + 128 + i * 4) = fbits(i & 1, -(int)(i & 3), 0x123456 * (i + 1));
    }
    REG32(DATA + 256) = fbits(0, -1, 0x3504f3);   /* light / x */
    REG32(DATA + 260) = fbits(0, 0, 0x200000);
    REG32(DATA + 264) = fbits(0, -1, 0);          /* 0.5 */
    REG32(DATA + 268) = fbits(0, 0, 0x400000);    /* 1.5 */
    for (i = 0; i < 11; i++) {
        REG32(DATA + 272 + i * 4) = fbits(0, -1, 0x2aaaaa * i);
    }
    REG32(DATA + 316) = fbits(0, 14, 0x7ffe00);     /* 32767.0 */
    REG32(DATA + 320) = fbits(0, 23, 0x400000);     /* 1.5 * 2^23 */
    for (i = 0; i < 256 * 4; i++) {
        u32 r = i * 2654435761u;
        REG32(DATA + 512 + i * 4) = fbits((r >> 31) & ((i & 3) != 3), (int)(r % 7) - 3, r);
    }
    asm volatile("mtfsf 0xff,%0" : : "f"(0.0));
    for (k = 0; k < NKERNELS; k++) {
        u32 n = iters / kernels[k].scale;
        u64 t0, t1;
        if (!(mask & (1u << k))) {
            continue;
        }
        if (kernels[k].fn == bench_fdiv || kernels[k].fn == bench_math) {
            n /= 2;
        }
        if (!n) {
            n = 1;
        }
        kernels[k].fn(n / 8 ? n / 8 : 1, (void *)DATA);    /* warm up: translate */
        t0 = read_tb();
        kernels[k].fn(n, (void *)DATA);
        t1 = read_tb();
        REG32(BENCH_BASE + k * 16) = n;
        REG32(BENCH_BASE + k * 16 + 4) = kernels[k].ops;
        put64(BENCH_BASE + k * 16 + 8, t1 - t0);
    }
}
