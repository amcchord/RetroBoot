/*
 * fpconf startup: exception vectors (MSR[IP] = 1, so at 0xfff00000 in ROM),
 * real-mode C environment, and the run_op trampoline used by the
 * conformance loop.  Every exception records SRR0/SRR1 and the vector and
 * resumes at the address stored in TRAP_RESUME, so an enabled FP exception
 * (or an illegal instruction) is part of the recorded outcome rather than a
 * crash.
 */
#include "fpconf.h"

#define MSR_BASE 0x02003040     /* VEC | FP | ME | IP */

        .section .vectors, "ax"
        .globl  _vectors
_vectors:
        .macro  VEC num
        .org    \num
        mtsprg  2,%r3
        li      %r3,\num
        b       trap_common
        .endm

        VEC     0x000
        .org    0x100
        b       _start
        VEC     0x200
        VEC     0x300
        VEC     0x400
        VEC     0x500
        VEC     0x600
        VEC     0x700
        VEC     0x800
        VEC     0x900
        VEC     0xa00
        VEC     0xb00
        VEC     0xc00
        VEC     0xd00
        VEC     0xe00
        VEC     0xf00
        .org    0xf20
        mtsprg  2,%r3
        li      %r3,0xf20
        b       trap_common
        VEC     0x1000
        VEC     0x1100
        VEC     0x1200
        VEC     0x1300
        VEC     0x1400
        VEC     0x1600
        VEC     0x1700

        .org    0x2000
trap_common:
        mtsprg  3,%r4
        stw     %r3,TRAP_VECTOR(0)
        mfsrr0  %r3
        stw     %r3,TRAP_SRR0(0)
        mfsrr1  %r3
        stw     %r3,TRAP_SRR1(0)
        lwz     %r3,TRAP_COUNT(0)
        addi    %r3,%r3,1
        stw     %r3,TRAP_COUNT(0)
        lwz     %r3,TRAP_TOTAL(0)
        addi    %r3,%r3,1
        stw     %r3,TRAP_TOTAL(0)
        lwz     %r3,TRAP_RESUME(0)
        mtsrr0  %r3
        mfsrr1  %r3
        rlwinm  %r3,%r3,0,0,30          /* resume big-endian: MSR[LE] = 0 */
        mtsrr1  %r3
        mfsprg  %r3,2
        mfsprg  %r4,3
        rfi

        .text
        .globl  _start
_start:
        lis     %r3,MSR_BASE@h
        ori     %r3,%r3,MSR_BASE@l
        mtmsr   %r3
        isync
        /* stack */
        lis     %r1,0x00f0
        li      %r0,0
        stwu    %r0,-16(%r1)
        /* zero .bss */
        lis     %r3,__bss_start@ha
        addi    %r3,%r3,__bss_start@l
        lis     %r4,__bss_end@ha
        addi    %r4,%r4,__bss_end@l
1:      cmplw   %r3,%r4
        bge     2f
        stw     %r0,0(%r3)
        addi    %r3,%r3,4
        b       1b
2:      li      %r3,0
        stw     %r3,TRAP_TOTAL(0)
        lis     %r3,fatal_resume@ha
        addi    %r3,%r3,fatal_resume@l
        stw     %r3,TRAP_RESUME(0)
        bl      main
fatal_resume:
3:      b       3b

/* void set_msr(uint32_t msr) */
        .globl  set_msr
set_msr:
        mtmsr   %r3
        isync
        blr

/* uint32_t get_msr(void) */
        .globl  get_msr
get_msr:
        mfmsr   %r3
        blr

/* uint64_t read_tb(void) */
        .globl  read_tb
read_tb:
1:      mftbu   %r3
        mftb    %r4
        mftbu   %r5
        cmpw    %r3,%r5
        bne     1b
        blr

/*
 * void run_op(void (*stub)(void), const uint64_t in[4], uint8_t out[48],
 *             const uint32_t cfg[4])
 *   in:  f1 = in[0] (A), f2 = in[1] (B), f3 = in[2] (C), f4 = in[3] (sentinel)
 *   cfg: [0..1] FPSCR image (mtfsf 0xff), [2] CR image
 *   out: +0 f4, +8 mffs, +16 CR, +20 traps, +24 SRR0, +28 SRR1,
 *        +32 store area (8), +40 r8, +44 r9
 *   stubs: r8 = &in[0] (loads), r9 = out + 32 (stores), r10 = 0
 */
        .globl  run_op
run_op:
        mflr    %r0
        stwu    %r1,-48(%r1)
        stw     %r0,52(%r1)
        mfcr    %r12
        stw     %r12,8(%r1)
        li      %r10,0
        stw     %r10,TRAP_COUNT(0)
        stw     %r10,TRAP_SRR0(0)
        stw     %r10,TRAP_SRR1(0)
        lis     %r11,1f@ha
        addi    %r11,%r11,1f@l
        stw     %r11,TRAP_RESUME(0)
        lfd     %f0,0(%r6)
        mtfsf   0xff,%f0
        lfd     %f1,0(%r4)
        lfd     %f2,8(%r4)
        lfd     %f3,16(%r4)
        lfd     %f4,24(%r4)
        lwz     %r11,8(%r6)
        mr      %r8,%r4
        addi    %r9,%r5,32
        mtcrf   0xff,%r11
        mtctr   %r3
        bctrl
1:      mfcr    %r11
        stfd    %f4,0(%r5)
        mffs    %f0
        stfd    %f0,8(%r5)
        stw     %r11,16(%r5)
        lwz     %r11,TRAP_COUNT(0)
        stw     %r11,20(%r5)
        lwz     %r11,TRAP_SRR0(0)
        stw     %r11,24(%r5)
        lwz     %r11,TRAP_SRR1(0)
        stw     %r11,28(%r5)
        stw     %r8,40(%r5)
        stw     %r9,44(%r5)
        lis     %r11,fatal_resume@ha
        addi    %r11,%r11,fatal_resume@l
        stw     %r11,TRAP_RESUME(0)
        lwz     %r12,8(%r1)
        mtcrf   0xff,%r12
        lwz     %r0,52(%r1)
        mtlr    %r0
        addi    %r1,%r1,48
        blr

/*
 * void run_vop(void (*stub)(void), const uint32_t in[16], uint32_t out[12],
 *              const uint32_t cfg[4])          (in, out 16-byte aligned)
 *   v1 = in[0..3] (A), v2 = in[4..7] (B), v3 = in[8..11] (C),
 *   v4 = in[12..15] (sentinel); VSCR = cfg[3], CR = cfg[2]
 *   out: +0 v4, +16 CR, +20 traps, +24 SRR0, +28 SRR1, +32 VSCR (mfvscr)
 */
        .globl  run_vop
run_vop:
        mflr    %r0
        stwu    %r1,-48(%r1)
        stw     %r0,52(%r1)
        mfcr    %r12
        stw     %r12,8(%r1)
        li      %r10,0
        stw     %r10,TRAP_COUNT(0)
        stw     %r10,TRAP_SRR0(0)
        stw     %r10,TRAP_SRR1(0)
        lis     %r11,1f@ha
        addi    %r11,%r11,1f@l
        stw     %r11,TRAP_RESUME(0)
        addi    %r9,%r5,32
        lwz     %r11,12(%r6)
        stw     %r11,12(%r9)
        lvx     %v0,0,%r9
        mtvscr  %v0
        lvx     %v1,0,%r4
        addi    %r9,%r4,16
        lvx     %v2,0,%r9
        addi    %r9,%r4,32
        lvx     %v3,0,%r9
        addi    %r9,%r4,48
        lvx     %v4,0,%r9
        lwz     %r11,8(%r6)
        mtcrf   0xff,%r11
        mtctr   %r3
        bctrl
1:      mfcr    %r11
        stvx    %v4,0,%r5
        stw     %r11,16(%r5)
        lwz     %r11,TRAP_COUNT(0)
        stw     %r11,20(%r5)
        lwz     %r11,TRAP_SRR0(0)
        stw     %r11,24(%r5)
        lwz     %r11,TRAP_SRR1(0)
        stw     %r11,28(%r5)
        mfvscr  %v0
        addi    %r9,%r5,32
        stvx    %v0,0,%r9
        lis     %r11,fatal_resume@ha
        addi    %r11,%r11,fatal_resume@l
        stw     %r11,TRAP_RESUME(0)
        lwz     %r12,8(%r1)
        mtcrf   0xff,%r12
        lwz     %r0,52(%r1)
        mtlr    %r0
        addi    %r1,%r1,48
        blr

/*
 * void run_vop_le(...): run_vop with the stub in little-endian mode.  The
 * stub (byte-reversed words, ops_le.S) ends with sc; trap_common resumes
 * at 1: big-endian.  The sc counts as a trap in the outcome.
 */
        .globl  run_vop_le
run_vop_le:
        mflr    %r0
        stwu    %r1,-48(%r1)
        stw     %r0,52(%r1)
        mfcr    %r12
        stw     %r12,8(%r1)
        li      %r10,0
        stw     %r10,TRAP_COUNT(0)
        stw     %r10,TRAP_SRR0(0)
        stw     %r10,TRAP_SRR1(0)
        lis     %r11,1f@ha
        addi    %r11,%r11,1f@l
        stw     %r11,TRAP_RESUME(0)
        addi    %r9,%r5,32
        lwz     %r11,12(%r6)
        stw     %r11,12(%r9)
        lvx     %v0,0,%r9
        mtvscr  %v0
        lvx     %v1,0,%r4
        addi    %r9,%r4,16
        lvx     %v2,0,%r9
        addi    %r9,%r4,32
        lvx     %v3,0,%r9
        addi    %r9,%r4,48
        lvx     %v4,0,%r9
        lwz     %r11,8(%r6)
        mtcrf   0xff,%r11
        mtsrr0  %r3                     /* the stub runs little-endian */
        mfmsr   %r11
        ori     %r11,%r11,1             /* MSR[LE] */
        mtsrr1  %r11
        rfi                             /* the stub ends with sc */
1:      mfcr    %r11
        stvx    %v4,0,%r5
        stw     %r11,16(%r5)
        lwz     %r11,TRAP_COUNT(0)
        stw     %r11,20(%r5)
        lwz     %r11,TRAP_SRR0(0)
        stw     %r11,24(%r5)
        lwz     %r11,TRAP_SRR1(0)
        stw     %r11,28(%r5)
        mfvscr  %v0
        addi    %r9,%r5,32
        stvx    %v0,0,%r9
        lis     %r11,fatal_resume@ha
        addi    %r11,%r11,fatal_resume@l
        stw     %r11,TRAP_RESUME(0)
        lwz     %r12,8(%r1)
        mtcrf   0xff,%r12
        lwz     %r0,52(%r1)
        mtlr    %r0
        addi    %r1,%r1,48
        blr
