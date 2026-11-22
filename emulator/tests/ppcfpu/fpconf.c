/*
 * fpconf: bare-metal PowerPC FP conformance program (see fpconf.h, start.S).
 *
 * For every enabled FPSCR/MSR configuration and every op stub (ops.S), it
 * generates COUNT deterministic operand vectors (random and edge-case
 * values, cancellations, single-precision midpoints, conversion edges),
 * executes the single instruction through run_op and folds the complete
 * outcome (target FPR, FPSCR, CR, exception count/SRR0/SRR1, stored memory,
 * updated base registers) into a 64-bit hash per (configuration, op).  Log
 * mode stores every record instead.  The same binary is run with
 * -cpu 7400,fast-fpu=off (QEMU's softfloat path, the reference) and with the
 * fast path; the driver compares hashes and records bit for bit.
 *
 * No floating point is used in C: values are built with integer arithmetic,
 * so input generation never depends on the FPU under test.
 */
#include <stdint.h>
#include "fpconf.h"

typedef uint64_t u64;
typedef uint32_t u32;

extern void run_op(void (*stub)(void), const u64 *in, u32 *out, const u32 *cfg);
extern void run_vop(void (*stub)(void), const u32 *in, u32 *out, const u32 *cfg);
extern void smc_main(void);
extern void run_vop_le(void (*stub)(void), const u32 *in, u32 *out, const u32 *cfg);
extern void set_msr(u32 msr);
extern u32 get_msr(void);
extern u64 read_tb(void);
extern const u32 op_table[];
extern const u32 op_count;
extern void bench_main(void);

#define REG32(a) (*(volatile u32 *)(a))
#define MSR_BASE 0x02003040u
#define MSR_FE0  0x00000800u
#define MSR_FE1  0x00000100u
#define MSR_VEC  0x02000000u

/* ---- PRNG (xorshift64*) ---- */
static u64 rng_state;

static u64 rnd(void)
{
    u64 x = rng_state;
    x ^= x >> 12;
    x ^= x << 25;
    x ^= x >> 27;
    rng_state = x;
    return x * 0x2545F4914F6CDD1DULL;
}

static u32 rnd32(void)
{
    return (u32)(rnd() >> 32);
}

static u32 below(u32 n)
{
    return (u32)(((u64)rnd32() * n) >> 32);
}

/* ---- value construction (integer only) ---- */
#define D_SIGN 0x8000000000000000ULL
#define D_FRAC 0x000fffffffffffffULL

static u64 mkd(u32 sign, int exp_biased, u64 frac)
{
    return ((u64)sign << 63) | ((u64)(exp_biased & 0x7ff) << 52) | (frac & D_FRAC);
}

/* single bits -> double bits (exact, like lfs) */
static u64 s2d(u32 f)
{
    u32 sign = f >> 31, e = (f >> 23) & 0xff, m = f & 0x7fffff;
    if (e == 0xff) {
        return mkd(sign, 0x7ff, (u64)m << 29);
    }
    if (e == 0) {
        int sh;
        if (m == 0) {
            return (u64)sign << 63;
        }
        sh = 0;
        while (!(m & 0x800000)) {
            m <<= 1;
            sh++;
        }
        return mkd(sign, 1023 - 126 - sh, (u64)(m & 0x7fffff) << 29);
    }
    return mkd(sign, (int)e - 127 + 1023, (u64)m << 29);
}

static const u64 specials[] = {
    0x0000000000000000ULL, 0x8000000000000000ULL,       /* +-0 */
    0x0000000000000001ULL, 0x000fffffffffffffULL,       /* denormal min/max */
    0x0008000000000000ULL, 0x0000000000000400ULL,
    0x0010000000000000ULL, 0x0010000000000001ULL,       /* min normal */
    0x001fffffffffffffULL, 0x0020000000000000ULL,
    0x7fefffffffffffffULL, 0x7feffffffffffffeULL,       /* max */
    0x7fe0000000000000ULL, 0x7fdfffffffffffffULL,
    0x7ff0000000000000ULL,                              /* inf */
    0x7ff8000000000000ULL, 0x7ff8000000000001ULL,       /* qNaN */
    0x7fffffffffffffffULL, 0x7ff0000000000001ULL,       /* qNaN, sNaN */
    0x7ff4000000000000ULL, 0x7ff7ffffffffffffULL,       /* sNaN */
    0x3ff0000000000000ULL, 0x3ff0000000000001ULL,       /* 1, 1+ulp */
    0x3fefffffffffffffULL, 0x4000000000000000ULL,       /* 1-ulp, 2 */
    0x3fe0000000000000ULL, 0x4008000000000000ULL,       /* 0.5, 3 */
    0x47efffffe0000000ULL, 0x47effffff0000000ULL,       /* FLT_MAX, FLT_MAX+half ulp */
    0x47efffffefffffffULL, 0x47f0000000000000ULL,       /* below it, 2^128 */
    0x3810000000000000ULL, 0x380fffffffffffffULL,       /* FLT_MIN, just below */
    0x380ffffff0000000ULL, 0x3810000010000000ULL,       /* FLT_MIN-half ulp, +half ulp */
    0x36a0000000000000ULL, 0x3690000000000000ULL,       /* single denorm min, half of it */
    0x36a8000000000000ULL, 0x380fffffe0000000ULL,       /* 1.5 denorm min, max single denorm */
    0x41dfffffffc00000ULL, 0x41e0000000000000ULL,       /* 2^31-1, 2^31 */
    0xc1e0000000000000ULL, 0xc1e0000000100000ULL,       /* -2^31, -2^31-0.5 */
    0x41dfffffffe00000ULL, 0x43300000000000ffULL,       /* 2^31-0.5, 2^52+255 */
    0x3ff8000000000000ULL, 0x4004000000000000ULL,       /* 1.5, 2.5 */
    0x3ff0000010000000ULL, 0x3ff0000030000000ULL,       /* single midpoints (even/odd) */
    0x3ff0000008000000ULL, 0x3ff0000018000000ULL,
    0x3ff0000010000001ULL, 0x3ff000000fffffffULL,       /* around a midpoint */
    0x4330000000000000ULL, 0x4340000000000000ULL,       /* 2^52, 2^53 */
};
#define NSPECIALS (sizeof(specials) / sizeof(specials[0]))

static u64 gen_special(void)
{
    u64 v = specials[below(NSPECIALS)];
    if (rnd32() & 1) {
        v ^= D_SIGN;
    }
    return v;
}

static u64 gen_moderate(int span)
{
    return mkd(rnd32() & 1, 1023 - span + (int)below(2 * span + 1), rnd());
}

static u32 gen_single_bits(void)
{
    u32 r = below(100), sign = rnd32() & 0x80000000u;
    if (r < 60) {
        return sign | ((127 - 30 + below(61)) << 23) | (rnd32() & 0x7fffff);
    } else if (r < 70) {
        return rnd32();
    } else if (r < 78) {
        return sign | (rnd32() & 0x7fffff);                 /* denormal/zero */
    } else if (r < 83) {
        return sign | 0x7f800000u | ((rnd32() & 1) ? (rnd32() & 0x7fffff) : 0);
    } else if (r < 90) {
        return sign | ((1 + below(6)) << 23) | (rnd32() & 0x7fffff);
    } else if (r < 95) {
        return sign | ((254 - below(6)) << 23) | (rnd32() & 0x7fffff);
    }
    return sign | ((127 + below(24)) << 23) | ((rnd32() & 0x7fffff) & ~((1u << below(23)) - 1));
}

static u64 gen_double(int single)
{
    u32 r = below(100);
    if (single && r < 45) {
        return s2d(gen_single_bits());
    }
    if (r < 18) {
        return gen_special();
    } else if (r < 45) {
        return gen_moderate(40);
    } else if (r < 58) {
        return s2d(gen_single_bits());
    } else if (r < 66) {
        return rnd();
    } else if (r < 74) {
        switch (below(4)) {
        case 0:
            return mkd(rnd32() & 1, 1 + below(80), rnd());
        case 1:
            return mkd(rnd32() & 1, 0x7fe - below(80), rnd());
        case 2:
            return mkd(rnd32() & 1, 1023 - 126 - 4 + below(9), rnd() & (below(2) ? ~0ULL : ~0x1fffffffULL));
        default:
            return mkd(rnd32() & 1, 1023 + 127 - 4 + below(9), rnd() & (below(2) ? ~0ULL : ~0x1fffffffULL));
        }
    } else if (r < 86) {
        /* small integers and simple fractions: exact arithmetic */
        u32 k = 1 + below(1 << (1 + below(20)));
        int sh = 0;
        while (!(k & 0x80000000u)) {
            k <<= 1;
            sh++;
        }
        return mkd(rnd32() & 1, 1023 + 31 - sh - 12 + (int)below(25),
                   ((u64)(k & 0x7fffffff)) << 21);
    } else if (r < 93) {
        return mkd(rnd32() & 1, 0, rnd() >> below(52));     /* denormal */
    }
    /* single-precision denormal range / midpoints */
    if (rnd32() & 1) {
        return mkd(rnd32() & 1, 1023 - 150 + below(25), rnd() & ~0xfffffffULL);
    }
    return mkd(rnd32() & 1, 1023 - 20 + below(41),
               (rnd() & ~0x1fffffffULL) | 0x10000000ULL);
}

/* top 64 bits of the 106-bit product of two 53-bit significands */
static u64 mulhi(u64 a, u64 b)
{
    u64 al = (u32)a, ah = a >> 32, bl = (u32)b, bh = b >> 32;
    u64 ll = al * bl, lh = al * bh, hl = ah * bl, hh = ah * bh;
    u64 mid = (ll >> 32) + (u32)lh + (u32)hl;
    return hh + (lh >> 32) + (hl >> 32) + (mid >> 32);
}

/* approximately -(a * c) for normal a, c (fma cancellation) */
static u64 neg_product(u64 a, u64 c)
{
    int ea = (int)((a >> 52) & 0x7ff), ec = (int)((c >> 52) & 0x7ff);
    u64 ma = (a & D_FRAC) | (1ULL << 52), mc = (c & D_FRAC) | (1ULL << 52);
    u64 p = mulhi(ma << 11, mc << 11);  /* product scaled by 2^-128+22 */
    int e = ea + ec - 1023;
    u32 sign = !((a ^ c) >> 63);
    if (ea == 0 || ec == 0 || ea == 0x7ff || ec == 0x7ff) {
        return gen_moderate(10);
    }
    if (p & 0x8000000000000000ULL) {
        e += 1;
        p >>= 11;
    } else {
        p >>= 10;
    }
    if (e <= 0 || e >= 0x7ff) {
        return gen_moderate(10);
    }
    switch (below(4)) {
    case 0:
        break;
    case 1:
        p ^= below(8);
        break;
    case 2:
        p += (u64)below(1 << 20) - (1 << 19);
        break;
    default:
        p &= ~((1ULL << below(40)) - 1);
        break;
    }
    return mkd(sign, e, p);
}

static u64 perturb(u64 v)
{
    switch (below(5)) {
    case 0:
        return v ^ D_SIGN;
    case 1:
        return (v ^ D_SIGN) ^ below(16);
    case 2:
        return (v ^ D_SIGN) + (below(2) ? 1 : -1);
    case 3:
        return v;
    default:
        return ((v ^ D_SIGN) & ~((1ULL << below(52)) - 1));
    }
}

static u64 gen_cvt(void)
{
    u32 r = below(10);
    if (r < 3) {
        return gen_double(0);
    } else if (r < 6) {
        /* n + {0, .25, .5, .75} near small and boundary integers */
        u32 n = below(4) == 0 ? 0x7fffffffu - below(4) : below(1u << below(31));
        u64 v = mkd(0, 1023 + 31, 0);
        int sh = 0;
        u64 m;
        if (n == 0) {
            return mkd(rnd32() & 1, 1023 - 1 - below(3), rnd() & ~((1ULL << 50) - 1));
        }
        m = (u64)n << 2 | below(4);
        while (!(m & (1ULL << 52))) {
            m <<= 1;
            sh++;
        }
        v = mkd(rnd32() & 1, 1023 + 52 - sh - 2, m);
        return v;
    } else if (r < 8) {
        return mkd(rnd32() & 1, 1023 + 28 + below(8), rnd());
    }
    return gen_special();
}

/* exponent field of a double, and v with its exponent field replaced */
static int dexp(u64 v)
{
    return (int)((v >> 52) & 0x7ff);
}

static u64 with_exp(u64 v, int e)
{
    if (e < 1) {
        e = 1;
    } else if (e > 0x7fe) {
        e = 0x7fe;
    }
    return (v & ~0x7ff0000000000000ULL) | ((u64)e << 52);
}

/*
 * Range edges: make the result of op(A, B) (or A*C + B) land within a few
 * binades of the smallest normal, of FLT_MIN/FLT_MAX for single ops, or of
 * overflow, where exactness tests must also respect the exponent range.
 */
/* structured significands: powers of two, all ones, sparse, short */
static u64 struct_frac(int single)
{
    u64 f;
    switch (below(6)) {
    case 0:
        f = 0;
        break;
    case 1:
        f = D_FRAC;                                   /* 1.111...1 */
        break;
    case 2:
        f = D_FRAC - below(8);
        break;
    case 3:
        f = (1ULL << below(52)) | below(2);           /* sparse */
        break;
    case 4:
        f = rnd() & ~((1ULL << (26 + below(27))) - 1); /* short */
        break;
    default:
        f = rnd();
        break;
    }
    if (single) {
        f &= ~0x1fffffffULL;
        if (below(4) == 0) {
            f |= 0x000fffffe0000000ULL;               /* single all ones */
        }
    }
    return f & D_FRAC;
}

static void gen_edges(u32 k, int single, u64 *in)
{
    static const int dtarget[] = { 1, 2, 3, 0x7fe, 1023 - 126, 1023 - 125, 1023 + 127, 1023 - 149 };
    int t = dtarget[single ? 4 + below(4) : below(5)] + (int)below(5) - 2;
    u64 a = gen_moderate(30), b = gen_moderate(30), c = gen_moderate(30);

    if (single && below(2)) {
        a &= ~0x1fffffffULL;
        b &= ~0x1fffffffULL;
        c &= ~0x1fffffffULL;
    } else if (below(2)) {
        /* short significands: exact products and quotients */
        a &= ~((1ULL << (26 + below(27))) - 1);
        c &= ~((1ULL << (26 + below(27))) - 1);
        b &= ~((1ULL << below(53)) - 1);
    }
    if (below(2)) {
        a = (a & ~D_FRAC) | struct_frac(single);
        b = (b & ~D_FRAC) | struct_frac(single);
        c = (c & ~D_FRAC) | struct_frac(single);
    }
    switch (k) {
    case 0: /* ab: A/B (div) near t, or A + B cancelling near t */
        if (below(2)) {
            a = with_exp(a, t + dexp(b) - 1023);
        } else {
            a = with_exp(a, t + (int)below(3));
            b = (a ^ D_SIGN) + (below(2) ? 1 : -1) * (u64)below(1 << below(30));
        }
        break;
    case 1: /* ac: A*C near t */
        c = with_exp(c, t - dexp(a) + 1023 - (int)below(2));
        break;
    case 2: /* abc: A*C near t, B near the product or tiny */
        c = with_exp(c, t - dexp(a) + 1023 - (int)below(2));
        if (below(2)) {
            b = neg_product(a, c);
        } else {
            b = with_exp(b, t - (int)below(60));
        }
        break;
    default:
        break;
    }
    in[0] = a;
    in[1] = b;
    in[2] = c;
}

static void gen_inputs(u32 kind, u64 *in)
{
    int single = (kind & 0x100) != 0;
    u32 k = kind & 0xff;
    u32 r = below(100);

    in[0] = gen_double(single);
    in[1] = gen_double(single);
    in[2] = gen_double(single);
    if (k <= 2 && r >= 88) {
        gen_edges(k, single, in);
        return;
    }
    switch (k) {
    case 0: /* ab */
    case 4: /* cmp */
        if (single && r >= 80 && r < 88) {
            /* float -> int rounding with a magic constant (Sound Manager) */
            static const u64 magic[] = { 0x4160000000000000ULL, 0x4168000000000000ULL,
                                         0x4330000000000000ULL, 0x4338000000000000ULL };
            in[1] = magic[below(4)] ^ (below(4) == 0 ? D_SIGN : 0);
            in[0] = mkd(rnd32() & 1, 1023 - 10 + below(32), rnd() & ~0x1fffffffULL);
            if (below(2)) {
                in[0] |= 0x10000000ULL & -(u64)below(2);      /* halves */
            }
            break;
        }
        if (r < 15) {
            in[1] = perturb(in[0]);
        } else if (r < 22) {
            u64 e = (in[0] >> 52) & 0x7ff;
            int d = 20 + (int)below(40);
            if (e > (u64)d + 1 && e < 0x7fe) {
                in[1] = mkd(rnd32() & 1, (int)e - d, rnd() & (single ? ~0x1fffffffULL : ~0ULL));
            }
        } else if (k == 4 && r < 30) {
            in[1] = in[0];
        }
        break;
    case 1: /* ac */
        if (r < 8) {
            in[2] = in[0];
        }
        break;
    case 2: /* abc: A*C + B */
        if (r < 20) {
            in[1] = neg_product(in[0], in[2]);
            if (single && below(2)) {
                in[1] &= ~0x1fffffffULL;
            }
        } else if (r < 25) {
            in[0] = (in[0] & D_SIGN) | (below(2) ? 0 : 0);  /* zero product */
        }
        break;
    case 3: /* b */
        break;
    case 5: /* cvt */
        in[1] = gen_cvt();
        break;
    case 6: /* rsp: double -> single rounding edges */
        if (r < 30) {
            in[1] = mkd(rnd32() & 1, 1023 - 30 + below(61),
                        (rnd() & ~0x1fffffffULL) | (below(2) ? 0x10000000ULL : 0x0fffffffULL + below(3)));
        } else if (r < 45) {
            in[1] = mkd(rnd32() & 1, 1023 - 160 + below(40), rnd());
        } else if (r < 55) {
            in[1] = mkd(rnd32() & 1, 1023 + 120 + below(12), rnd());
        }
        break;
    case 7: /* move */
        break;
    case 8: /* fpscr: B carries random FPSCR bits */
        in[1] = rnd();
        if (r < 50) {
            in[1] &= ~0x000000f8ULL;   /* enables off half the time */
        }
        break;
    case 9: /* lds: in[0] high word = single bits */
        in[0] = ((u64)gen_single_bits() << 32) | rnd32();
        break;
    case 10: /* ldd */
        in[0] = gen_double(0);
        break;
    case 11: /* sts */
    case 12: /* std */
        if (r < 40) {
            in[0] = mkd(rnd32() & 1, 1023 - 160 + below(40), rnd());
        }
        break;
    default:
        break;
    }
}

/* single-precision value for AltiVec lanes */
static u32 gen_lane(void)
{
    u32 r = below(100);
    if (r < 12) {
        static const u32 sp[] = {
            0x00000000, 0x80000000, 0x00800000, 0x007fffff, 0x00000001,
            0x7f7fffff, 0x7f800000, 0x7fc00000, 0x7fa00000, 0x3f800000,
            0x3f800001, 0x3effffff, 0x4b000000, 0x4f000000, 0x4effffff,
            0x5effffff, 0x5f000000, 0x00800001, 0x3fc00000, 0x40490fdb,
        };
        return sp[below(sizeof(sp) / sizeof(sp[0]))] ^ (rnd32() & 0x80000000u);
    }
    return gen_single_bits();
}

static void gen_vinputs(u32 kind, u32 *v)
{
    u32 k = kind & 0xff, r, i;

    for (i = 0; i < 12; i++) {
        v[i] = gen_lane();
    }
    for (i = 12; i < 16; i++) {
        v[i] = 0x7fa5a5a5u;                       /* sentinel */
    }
    for (i = 0; i < 4; i++) {
        r = below(100);
        if (k == 0x20 || k == 0x23) {             /* vab, vcmp: A vs B */
            if (r < 15) {
                v[4 + i] = v[i] ^ 0x80000000u ^ below(4);
            } else if (r < 25) {
                v[4 + i] = v[i];
            } else if (r < 30) {
                v[4 + i] = v[i] ^ 0x80000000u;
            }
        } else if (k == 0x21 && r < 20) {         /* vabc: B ~ -(A*C) */
            u64 p = ((u64)((v[i] & 0x7fffff) | 0x800000)) *
                    ((v[8 + i] & 0x7fffff) | 0x800000);
            int e = (int)((v[i] >> 23) & 0xff) + (int)((v[8 + i] >> 23) & 0xff) - 127;
            if (p & (1ULL << 47)) {
                p >>= 24;
                e++;
            } else {
                p >>= 23;
            }
            if (e > 0 && e < 255 && ((v[i] >> 23) & 0xff) && ((v[8 + i] >> 23) & 0xff)) {
                v[4 + i] = (((v[i] ^ v[8 + i] ^ 0x80000000u)) & 0x80000000u) |
                           ((u32)e << 23) | ((u32)(p & 0x7fffff) ^ below(3));
            }
        } else if (k == 0x25) {                   /* vcfx: integers */
            v[4 + i] = r < 50 ? rnd32() : (r < 75 ? below(1u << below(32)) : -below(1u << below(31)));
        } else if (k == 0x24 && r < 40) {         /* vcvt: near integer limits */
            v[4 + i] = ((rnd32() & 0x80000000u) | ((127 + 14 + below(20)) << 23)) | (rnd32() & 0x7fffff);
        }
    }
}

/* ---- configurations ---- */
struct cfg {
    u32 fpscr;
    u32 msr_fe;
};

static const struct cfg cfgs[] = {
    { 0x00000000, 0 },                        /* 0: RN, no enables (Mac OS default) */
    { 0x0001f000, 0 },                        /* 1: FPRF preset */
    { 0x9ffe0700, 0 },                        /* 2: every sticky bit preset */
    { 0x00000001, 0 },                        /* 3: RZ */
    { 0x00000002, 0 },                        /* 4: RP */
    { 0x00000003, 0 },                        /* 5: RM */
    { 0x000000f8, MSR_FE0 | MSR_FE1 },        /* 6: all enables, precise */
    { 0x000000f0, MSR_FE0 | MSR_FE1 },        /* 7: VE OE UE ZE, precise */
    { 0x00000008, MSR_FE0 | MSR_FE1 },        /* 8: XE, precise */
    { 0x000000f8, 0 },                        /* 9: all enables, ignored */
    { 0x00000004, 0 },                        /* 10: NI */
    { 0x00000060, MSR_FE0 | MSR_FE1 },        /* 11: OE UE, precise */
    { 0x000000f9, MSR_FE0 | MSR_FE1 },        /* 12: RZ + all enables */
    { 0x9ffe07f8, 0 },                        /* 13: sticky + enables, ignored */
    { 0x00000080, MSR_FE1 },                  /* 14: VE, imprecise recoverable */
    { 0x00000010, MSR_FE0 },                  /* 15: ZE, imprecise nonrecoverable */
};
#define NCFGS (sizeof(cfgs) / sizeof(cfgs[0]))

/* ---- hashing ---- */
struct hash {
    u32 a, b;
};

static void hash_words(struct hash *h, const u32 *w, int n)
{
    u32 a = h->a, b = h->b;
    int i;
    for (i = 0; i < n; i++) {
        a = (a ^ w[i]) * 0x01000193u;
        b = (b ^ w[i] ^ (a >> 15)) * 0x85ebca6bu;
        b ^= b >> 13;
    }
    h->a = a;
    h->b = b;
}

static void copy_words(u32 *dst, const u32 *src, int n)
{
    int i;
    for (i = 0; i < n; i++) {
        dst[i] = src[i];
    }
}

int main(void)
{
    u32 mode, count, cfgmask, first, last, nops, c, op, i;
    u64 seed;
    u64 vectors = 0;
    u32 nrecs = 0;
    volatile u32 *res = (volatile u32 *)RESULT_BASE;

    if (REG32(PARAM_BASE + P_MAGIC) != PARAM_MAGIC) {
        res[R_STATUS / 4] = 1;
        res[R_DONE / 4] = FAIL_MAGIC;
        return 0;
    }
    mode = REG32(PARAM_BASE + P_MODE);
    seed = ((u64)REG32(PARAM_BASE + P_SEED_HI) << 32) | REG32(PARAM_BASE + P_SEED_LO);
    count = REG32(PARAM_BASE + P_COUNT);
    cfgmask = REG32(PARAM_BASE + P_CFGMASK);
    first = REG32(PARAM_BASE + P_OPFIRST);
    last = REG32(PARAM_BASE + P_OPLAST);
    nops = op_count;
    res[R_NOPS / 4] = nops;
    res[R_NCFGS / 4] = NCFGS;
    res[R_TBFREQ / 4] = 25000000;

    if (mode == 5) {                    /* self-modifying code (smc.c) */
        smc_main();
        res[R_DONE / 4] = DONE_MAGIC;
        return 0;
    }
    if (mode == 3) {
        bench_main();
        res[R_DONE / 4] = DONE_MAGIC;
        return 0;
    }
    if (last >= nops) {
        last = nops - 1;
    }
    for (c = 0; c < NCFGS; c++) {
        u32 cfgimg[4];
        if (!(cfgmask & (1u << c))) {
            continue;
        }
        cfgimg[0] = 0;
        cfgimg[1] = cfgs[c].fpscr;
        cfgimg[2] = 0x5a3c96e1u;
        cfgimg[3] = 0;
        set_msr(MSR_BASE | cfgs[c].msr_fe);
        for (op = first; op <= last; op++) {
            void (*stub)(void) = (void (*)(void))op_table[op * 2];
            u32 kind = op_table[op * 2 + 1];
            struct hash h = { 0x811c9dc5u, 0x12345678u };
            res[R_STATUS / 4] = 0x80000000u | (c << 16) | op;
            rng_state = seed ^ ((u64)(c + 1) << 48) ^ ((u64)(op + 1) * 0x9E3779B97F4A7C15ULL);
            if (!rng_state) {
                rng_state = 1;
            }
            for (i = 0; i < 16; i++) {
                rnd();
            }
            for (i = 0; i < count; i++) {
                u64 in[8] __attribute__((aligned(16)));
                u32 out[12] __attribute__((aligned(16)));
                int j;
                for (j = 0; j < 12; j++) {
                    out[j] = 0xa5a5a5a5u;
                }
                if ((kind & 0xff) >= 0x20) {
                    gen_vinputs(kind, (u32 *)in);
                    cfgimg[3] = (c & 1) << 16;            /* VSCR[NJ] */
                    if (kind & 0x200) {                     /* MSR[LE] */
                        run_vop_le(stub, (const u32 *)in, out, cfgimg);
                    } else {
                        run_vop(stub, (const u32 *)in, out, cfgimg);
                    }
                } else {
                    gen_inputs(kind, in);
                    in[3] = 0x7ff4deadbeefcafeULL;
                    in[4] = in[5] = in[6] = in[7] = 0;
                    run_op(stub, in, out, cfgimg);
                }
                hash_words(&h, out, 12);
                if (mode == 2) {
                    u32 *rec = (u32 *)(LOG_BASE + nrecs * LOG_REC);
                    rec[0] = c;
                    rec[1] = op;
                    rec[2] = i;
                    rec[3] = kind;
                    copy_words(rec + 4, (const u32 *)in, 16);
                    copy_words(rec + 20, out, 12);
                    nrecs++;
                    res[R_NRECS / 4] = nrecs;
                }
                vectors++;
            }
            res[(R_HASHES / 4) + (c * MAX_OPS + op) * 2] = h.a;
            res[(R_HASHES / 4) + (c * MAX_OPS + op) * 2 + 1] = h.b;
        }
    }
    set_msr(MSR_BASE);
    res[R_VECTORS_LO / 4] = (u32)vectors;
    res[R_TRAPS / 4] = REG32(TRAP_TOTAL);
    res[R_DONE / 4] = DONE_MAGIC;
    return 0;
}
