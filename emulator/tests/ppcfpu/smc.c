/*
 * fpconf mode 5: self-modifying code against the branch caches (fast-branch,
 * emulator/reports/ppcfpu2-20261012.md).  Code is written into RAM pages at
 * run time, called until every cache that could hold its destination is
 * filled (cross-page b, the return-address cache, the per-site slot of a
 * bctr, a TB spanning two pages), then changed, with and without
 * dcbst/sync/icbi/isync, and called again.  Each call's r3 is recorded with
 * the value the new code returns.  The architecture requires the new code
 * after icbi + isync and allows either without them; QEMU (r16) always runs
 * the new code, and r17 must do exactly what r16 does.
 *
 * Results at RESULT_BASE + 0x100: n records of { u32 id, observed, expected }.
 */
#include "fpconf.h"

typedef unsigned int u32;
typedef u32 (*fn_t)(u32 r3, u32 r4, u32 r5);

#define REG32(a) (*(volatile u32 *)(a))

#define PAGE_A  0x00400000u
#define PAGE_B  0x00401000u
#define PAGE_C  0x00402000u
#define SPAN    0x00403ff8u     /* a function across 0x00404000 */
#define SCRATCH 0x00405000u

#define LI(rd, v)       (0x38000000u | ((rd) << 21) | ((v) & 0xffff))
#define ADDI(rd, ra, v) (0x38000000u | ((rd) << 21) | ((ra) << 16) | ((v) & 0xffff))
#define BLR             0x4e800020u
#define BCTR            0x4e800420u
#define NOP             0x60000000u
#define MFLR0           0x7c0802a6u
#define MTLR0           0x7c0803a6u
#define MTCTR4          0x7c8903a6u
#define STWU_R1         0x9421fff0u     /* stwu r1,-16(r1) */
#define STW_R0          0x90010014u     /* stw r0,20(r1) */
#define LWZ_R0          0x80010014u     /* lwz r0,20(r1) */
#define ADDI_R1         0x38210010u     /* addi r1,r1,16 */
#define STW_R5_R4       0x90a40000u     /* stw r5,0(r4) */
#define DCBST_R4        0x7c00206cu     /* dcbst 0,r4 */
#define SYNC            0x7c0004acu
#define ICBI_R4         0x7c0027acu     /* icbi 0,r4 */
#define ISYNC           0x4c00012cu

static u32 B_TO(u32 from, u32 to, int lk)
{
    return 0x48000000u | ((to - from) & 0x03fffffcu) | (lk ? 1 : 0);
}

static void sync_code(u32 addr)
{
    __asm__ volatile("dcbst 0,%0; sync; icbi 0,%0; isync" : : "r"(addr) : "memory");
}

static void put(u32 addr, u32 insn)
{
    REG32(addr) = insn;
    sync_code(addr);
}

/* change one instruction: plainly, or with the architected sequence */
static void patch(u32 addr, u32 insn, int coherent)
{
    REG32(addr) = insn;
    if (coherent) {
        sync_code(addr);
    }
}

static volatile u32 *rec;
static u32 nrec;

static void record(u32 id, u32 got, u32 want)
{
    rec[nrec * 3] = id;
    rec[nrec * 3 + 1] = got;
    rec[nrec * 3 + 2] = want;
    nrec++;
}

static u32 call(u32 addr, u32 r4, u32 r5)
{
    return ((fn_t)addr)(0, r4, r5);
}

/* id = scenario << 8 | variant << 4 | step */
#define ID(s, v, k) (((s) << 8) | ((v) << 4) | (k))

static void calls(u32 s, u32 v, u32 *k, u32 addr, u32 r4, u32 r5, u32 want, int n)
{
    for (int i = 0; i < n; i++) {
        record(ID(s, v, *k), call(addr, r4, r5), want);
        (*k)++;
    }
}

static void prologue_call(u32 at, u32 callee, u32 ret_insn)
{
    /* at: mflr; stwu; stw r0; bl callee; RET; lwz r0; mtlr; addi r1; blr */
    put(at, MFLR0);
    put(at + 4, STWU_R1);
    put(at + 8, STW_R0);
    put(at + 12, B_TO(at + 12, callee, 1));
    put(at + 16, ret_insn);
    put(at + 20, LWZ_R0);
    put(at + 24, MTLR0);
    put(at + 28, ADDI_R1);
    put(at + 32, BLR);
}

void smc_main(void)
{
    rec = (volatile u32 *)(RESULT_BASE + 0x100);
    nrec = 0;
    for (u32 v = 0; v < 2; v++) {           /* 0: plain store, 1: icbi + isync */
        u32 k, off = v * 0x800;
        u32 fa = PAGE_A + off, fb = PAGE_B + off, fc = PAGE_C + off;

        /* 1: a direct branch to another page (XPAGE slot), target changed */
        k = 0;
        put(fa, LI(3, 1));
        put(fa + 4, BLR);
        put(fb, B_TO(fb, fa, 0));
        calls(1, v, &k, fb, 0, 0, 1, 3);
        patch(fa, LI(3, 2), v);
        calls(1, v, &k, fb, 0, 0, 2, 3);
        /* ... and the branch itself redirected to a third page */
        put(fc, LI(3, 3));
        put(fc + 4, BLR);
        patch(fb, B_TO(fb, fc, 0), v);
        calls(1, v, &k, fb, 0, 0, 3, 3);

        /* 2: the return site of a bl (RAS slot) changed between calls */
        k = 0;
        put(fa + 0x100, BLR);
        prologue_call(fb + 0x100, fa + 0x100, LI(3, 10));
        calls(2, v, &k, fb + 0x100, 0, 0, 10, 3);
        patch(fb + 0x100 + 16, LI(3, 11), v);
        calls(2, v, &k, fb + 0x100, 0, 0, 11, 3);

        /* 3: the callee rewrites its caller's return site, then returns */
        k = 0;
        put(fa + 0x200, STW_R5_R4);
        if (v) {
            put(fa + 0x204, DCBST_R4);
            put(fa + 0x208, SYNC);
            put(fa + 0x20c, ICBI_R4);
            put(fa + 0x210, ISYNC);
            put(fa + 0x214, BLR);
        } else {
            put(fa + 0x204, BLR);
        }
        prologue_call(fb + 0x200, fa + 0x200, LI(3, 20));
        calls(3, v, &k, fb + 0x200, SCRATCH, 0, 20, 3);
        calls(3, v, &k, fb + 0x200, fb + 0x200 + 16, LI(3, 21), 21, 1);
        calls(3, v, &k, fb + 0x200, SCRATCH, 0, 21, 3);

        /* 4: a bctr alternating between targets (SITE slot), one changed */
        k = 0;
        put(fa + 0x300, LI(3, 30));
        put(fa + 0x304, BLR);
        put(fc + 0x300, LI(3, 31));
        put(fc + 0x304, BLR);
        put(fb + 0x300, MTCTR4);
        put(fb + 0x304, BCTR);
        calls(4, v, &k, fb + 0x300, fa + 0x300, 0, 30, 3);
        calls(4, v, &k, fb + 0x300, fc + 0x300, 0, 31, 3);
        calls(4, v, &k, fb + 0x300, fa + 0x300, 0, 30, 1);
        patch(fc + 0x300, LI(3, 32), v);
        calls(4, v, &k, fb + 0x300, fc + 0x300, 0, 32, 3);
        calls(4, v, &k, fb + 0x300, fa + 0x300, 0, 30, 1);

        /* 5: a TB across two pages, its second page changed */
        k = 0;
        {
            u32 sp = SPAN - v * 0x2000;     /* 0x403ff8 and 0x401ff8 */
            put(sp, LI(3, 40));
            put(sp + 4, NOP);
            put(sp + 8, ADDI(3, 3, 1));
            put(sp + 12, BLR);
            put(fc + 0x400, B_TO(fc + 0x400, sp, 0));
            calls(5, v, &k, fc + 0x400, 0, 0, 41, 3);
            patch(sp + 8, ADDI(3, 3, 5), v);
            calls(5, v, &k, fc + 0x400, 0, 0, 45, 3);
        }

        /* 6: an instruction stores over the next one of its own TB */
        k = 0;
        put(fa + 0x500, STW_R5_R4);
        put(fa + 0x504, LI(3, 50));
        put(fa + 0x508, BLR);
        calls(6, v, &k, fa + 0x500, SCRATCH, 0, 50, 3);
        calls(6, v, &k, fa + 0x500, fa + 0x504, LI(3, 51), 51, 1);
        calls(6, v, &k, fa + 0x500, SCRATCH, 0, 51, 3);
    }
    REG32(RESULT_BASE + 0x08) = nrec;       /* R_NOPS: the record count */
}
