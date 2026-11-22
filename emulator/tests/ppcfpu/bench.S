/*
 * fpconf microbenchmarks.  Each kernel: void bench_N(uint32_t iters,
 * void *data), iterates with CTR, keeps its values in the normal range and
 * reports nothing (bench.c times it with the timebase).  data points to a
 * RAM block prepared by bench.c (constants at +0, arrays further on).
 *
 * Data block layout (bench.c):
 *   +0   doubles: 1.0, 0.5, x=0.7853981633974483, c0..c7 polynomial coeffs
 *   +128 singles: 4x4 matrix (16 floats)
 *   +256 singles: light vector (4), scale (4)
 *   +512 vertices in: NV x 4 floats; out: NV x 4 floats at +0x10000
 */

        .text

/* dependent chain: x = x + c; x = x - c */
        .globl  bench_fadd
bench_fadd:
        mtctr   %r3
        lfd     %f1,0(%r4)
        lfd     %f2,24(%r4)
1:      fadd    %f1,%f1,%f2
        fsub    %f1,%f1,%f2
        fadd    %f1,%f1,%f2
        fsub    %f1,%f1,%f2
        bdnz    1b
        blr

/* 4 independent multiplies by ~1 and back */
        .globl  bench_fmul
bench_fmul:
        mtctr   %r3
        lfd     %f1,0(%r4)
        lfd     %f2,8(%r4)
        lfd     %f3,16(%r4)
        lfd     %f4,24(%r4)
        lfd     %f5,32(%r4)
        lfd     %f6,40(%r4)
1:      fmul    %f7,%f1,%f5
        fmul    %f8,%f2,%f5
        fmul    %f9,%f3,%f6
        fmul    %f10,%f4,%f6
        bdnz    1b
        blr

/* Horner polynomial, double fmadd chain (8 per iteration) */
        .globl  bench_fmadd
bench_fmadd:
        mtctr   %r3
        lfd     %f0,16(%r4)             /* x */
        lfd     %f2,24(%r4)
        lfd     %f3,32(%r4)
        lfd     %f4,40(%r4)
        lfd     %f5,48(%r4)
        lfd     %f6,56(%r4)
        lfd     %f7,64(%r4)
        lfd     %f8,72(%r4)
        lfd     %f9,80(%r4)
1:      fmadd   %f1,%f9,%f0,%f8
        fmadd   %f1,%f1,%f0,%f7
        fmadd   %f1,%f1,%f0,%f6
        fmadd   %f1,%f1,%f0,%f5
        fmadd   %f1,%f1,%f0,%f4
        fmadd   %f1,%f1,%f0,%f3
        fmadd   %f1,%f1,%f0,%f2
        fmadd   %f1,%f1,%f0,%f9
        bdnz    1b
        blr

/* same in single precision (fmadds) */
        .globl  bench_fmadds
bench_fmadds:
        mtctr   %r3
        lfs     %f0,256(%r4)
        lfs     %f2,128(%r4)
        lfs     %f3,132(%r4)
        lfs     %f4,136(%r4)
        lfs     %f5,140(%r4)
        lfs     %f6,144(%r4)
        lfs     %f7,148(%r4)
        lfs     %f8,152(%r4)
        lfs     %f9,156(%r4)
1:      fmadds  %f1,%f9,%f0,%f8
        fmadds  %f1,%f1,%f0,%f7
        fmadds  %f1,%f1,%f0,%f6
        fmadds  %f1,%f1,%f0,%f5
        fmadds  %f1,%f1,%f0,%f4
        fmadds  %f1,%f1,%f0,%f3
        fmadds  %f1,%f1,%f0,%f2
        fmadds  %f1,%f1,%f0,%f9
        bdnz    1b
        blr

/* fdiv, fdivs: 4 independent quotients */
        .globl  bench_fdiv
bench_fdiv:
        mtctr   %r3
        lfd     %f1,16(%r4)
        lfd     %f2,24(%r4)
        lfd     %f3,32(%r4)
1:      fdiv    %f4,%f1,%f2
        fdiv    %f5,%f2,%f3
        fdivs   %f6,%f1,%f3
        fdivs   %f7,%f3,%f1
        bdnz    1b
        blr

/* frsp of products, fctiwz + stfd + lwz (C float to int) */
        .globl  bench_cvt
bench_cvt:
        mtctr   %r3
        stwu    %r1,-32(%r1)
        lfd     %f1,16(%r4)
        lfd     %f2,88(%r4)             /* 1000.25 */
1:      fmul    %f3,%f1,%f2
        frsp    %f4,%f3
        fctiwz  %f5,%f3
        stfd    %f5,8(%r1)
        lwz     %r5,12(%r1)
        fctiw   %f6,%f4
        stfd    %f6,16(%r1)
        lwz     %r6,20(%r1)
        bdnz    1b
        addi    %r1,%r1,32
        blr

/* lfs/stfs streaming copy with scaling (lfs, fmuls, stfs) */
        .globl  bench_ldst
bench_ldst:
        mtctr   %r3
        lfs     %f0,260(%r4)
        addi    %r5,%r4,512
        addis   %r6,%r4,1
1:      lfs     %f1,0(%r5)
        lfs     %f2,4(%r5)
        lfs     %f3,8(%r5)
        lfs     %f4,12(%r5)
        stfs    %f1,0(%r6)
        stfs    %f2,4(%r6)
        stfs    %f3,8(%r6)
        stfs    %f4,12(%r6)
        bdnz    1b
        blr

/* fcmpu + branches, fsel */
        .globl  bench_cmp
bench_cmp:
        mtctr   %r3
        lfd     %f1,16(%r4)
        lfd     %f2,24(%r4)
        lfd     %f3,32(%r4)
1:      fcmpu   %cr0,%f1,%f2
        blt     2f
        fmr     %f4,%f1
2:      fcmpu   %cr1,%f2,%f3
        bgt     %cr1,3f
        fmr     %f5,%f2
3:      fsel    %f6,%f1,%f2,%f3
        fsel    %f7,%f2,%f3,%f1
        bdnz    1b
        blr

/*
 * Software transform: 4x4 matrix (singles) times NV=256 vertices per
 * iteration; 4 lfs + 16 fmuls/fmadds + 4 stfs per vertex.
 */
        .globl  bench_xform
bench_xform:
        mtctr   %r3
        stwu    %r1,-160(%r1)
        stfd    %f14,8(%r1)
        stfd    %f15,16(%r1)
        stfd    %f16,24(%r1)
        stfd    %f17,32(%r1)
        stfd    %f18,40(%r1)
        stfd    %f19,48(%r1)
        stfd    %f20,56(%r1)
        stfd    %f21,64(%r1)
        stfd    %f22,72(%r1)
        stfd    %f23,80(%r1)
        stfd    %f24,88(%r1)
        stfd    %f25,96(%r1)
        stfd    %f26,104(%r1)
        stfd    %f27,112(%r1)
        stfd    %f28,120(%r1)
        stfd    %f29,128(%r1)
        lfs     %f14,128(%r4)
        lfs     %f15,132(%r4)
        lfs     %f16,136(%r4)
        lfs     %f17,140(%r4)
        lfs     %f18,144(%r4)
        lfs     %f19,148(%r4)
        lfs     %f20,152(%r4)
        lfs     %f21,156(%r4)
        lfs     %f22,160(%r4)
        lfs     %f23,164(%r4)
        lfs     %f24,168(%r4)
        lfs     %f25,172(%r4)
        lfs     %f26,176(%r4)
        lfs     %f27,180(%r4)
        lfs     %f28,184(%r4)
        lfs     %f29,188(%r4)
1:      addi    %r5,%r4,512
        addis   %r6,%r4,1
        li      %r7,256
2:      lfs     %f1,0(%r5)
        lfs     %f2,4(%r5)
        lfs     %f3,8(%r5)
        lfs     %f4,12(%r5)
        fmuls   %f5,%f1,%f14
        fmuls   %f6,%f1,%f15
        fmuls   %f7,%f1,%f16
        fmuls   %f8,%f1,%f17
        fmadds  %f5,%f2,%f18,%f5
        fmadds  %f6,%f2,%f19,%f6
        fmadds  %f7,%f2,%f20,%f7
        fmadds  %f8,%f2,%f21,%f8
        fmadds  %f5,%f3,%f22,%f5
        fmadds  %f6,%f3,%f23,%f6
        fmadds  %f7,%f3,%f24,%f7
        fmadds  %f8,%f3,%f25,%f8
        fmadds  %f5,%f4,%f26,%f5
        fmadds  %f6,%f4,%f27,%f6
        fmadds  %f7,%f4,%f28,%f7
        fmadds  %f8,%f4,%f29,%f8
        stfs    %f5,0(%r6)
        stfs    %f6,4(%r6)
        stfs    %f7,8(%r6)
        stfs    %f8,12(%r6)
        addi    %r5,%r5,16
        addi    %r6,%r6,16
        addic.  %r7,%r7,-1
        bne     2b
        bdnz    1b
        lfd     %f14,8(%r1)
        lfd     %f15,16(%r1)
        lfd     %f16,24(%r1)
        lfd     %f17,32(%r1)
        lfd     %f18,40(%r1)
        lfd     %f19,48(%r1)
        lfd     %f20,56(%r1)
        lfd     %f21,64(%r1)
        lfd     %f22,72(%r1)
        lfd     %f23,80(%r1)
        lfd     %f24,88(%r1)
        lfd     %f25,96(%r1)
        lfd     %f26,104(%r1)
        lfd     %f27,112(%r1)
        lfd     %f28,120(%r1)
        lfd     %f29,128(%r1)
        addi    %r1,%r1,160
        blr

/*
 * Normalise 256 vectors per iteration: dot product (fmuls/fmadds),
 * frsqrte estimate, one Newton step (fmuls, fnmsubs), scale, plus a
 * reciprocal (fres) for a perspective divide.
 */
        .globl  bench_norm
bench_norm:
        mtctr   %r3
        lfs     %f10,264(%r4)           /* 0.5 */
        lfs     %f11,268(%r4)           /* 1.5 */
1:      addi    %r5,%r4,512
        addis   %r6,%r4,1
        li      %r7,256
2:      lfs     %f1,0(%r5)
        lfs     %f2,4(%r5)
        lfs     %f3,8(%r5)
        lfs     %f4,12(%r5)
        fmuls   %f5,%f1,%f1
        fmadds  %f5,%f2,%f2,%f5
        fmadds  %f5,%f3,%f3,%f5
        frsqrte %f6,%f5
        fmuls   %f7,%f6,%f6
        fmuls   %f8,%f5,%f10
        fnmsubs %f7,%f7,%f8,%f11
        fmuls   %f6,%f6,%f7
        fmuls   %f1,%f1,%f6
        fmuls   %f2,%f2,%f6
        fmuls   %f3,%f3,%f6
        fres    %f9,%f4
        fmuls   %f1,%f1,%f9
        stfs    %f1,0(%r6)
        stfs    %f2,4(%r6)
        stfs    %f3,8(%r6)
        stfs    %f9,12(%r6)
        addi    %r5,%r5,16
        addi    %r6,%r6,16
        addic.  %r7,%r7,-1
        bne     2b
        bdnz    1b
        blr

/*
 * Double-precision "math library" kernel: range reduction (fmul, fctiwz,
 * stfd/lwz, integer to float via the 2^52 trick with fsub), a degree-7
 * polynomial (fmadd), and a division.
 */
        .globl  bench_math
bench_math:
        mtctr   %r3
        stwu    %r1,-32(%r1)
        lfd     %f0,16(%r4)             /* x */
        lfd     %f10,96(%r4)            /* 2/pi */
        lfd     %f11,104(%r4)           /* 2^52 + 2^31 */
        lfd     %f12,0(%r4)             /* 1.0 */
        lfd     %f2,24(%r4)
        lfd     %f3,32(%r4)
        lfd     %f4,40(%r4)
        lfd     %f5,48(%r4)
        lfd     %f6,56(%r4)
        lfd     %f7,64(%r4)
        lfd     %f8,72(%r4)
        lis     %r8,0x4330
        stw     %r8,16(%r1)
1:      fmul    %f1,%f0,%f10
        fctiwz  %f13,%f1
        stfd    %f13,8(%r1)
        lwz     %r5,12(%r1)
        xoris   %r5,%r5,0x8000
        stw     %r5,20(%r1)
        lfd     %f13,16(%r1)
        fsub    %f13,%f13,%f11          /* (double)k */
        fnmsub  %f1,%f13,%f10,%f0       /* crude reduction */
        fmul    %f9,%f1,%f1
        fmadd   %f13,%f8,%f9,%f7
        fmadd   %f13,%f13,%f9,%f6
        fmadd   %f13,%f13,%f9,%f5
        fmadd   %f13,%f13,%f9,%f4
        fmadd   %f13,%f13,%f9,%f3
        fmadd   %f13,%f13,%f9,%f2
        fmul    %f13,%f13,%f1
        fadd    %f13,%f13,%f12
        fdiv    %f13,%f12,%f13
        bdnz    1b
        addi    %r1,%r1,32
        blr

/* AltiVec transform: 4x4 matrix (vsplat columns) times 256 vertices */
        .globl  bench_vxform
bench_vxform:
        mtctr   %r3
        li      %r9,128
        lvx     %v10,%r4,%r9
        li      %r9,144
        lvx     %v11,%r4,%r9
        li      %r9,160
        lvx     %v12,%r4,%r9
        li      %r9,176
        lvx     %v13,%r4,%r9
        vxor    %v14,%v14,%v14
1:      addi    %r5,%r4,512
        addis   %r6,%r4,1
        li      %r7,256
2:      lvx     %v1,0,%r5
        vspltw  %v2,%v1,0
        vspltw  %v3,%v1,1
        vspltw  %v4,%v1,2
        vspltw  %v5,%v1,3
        vmaddfp %v6,%v2,%v10,%v14
        vmaddfp %v6,%v3,%v11,%v6
        vmaddfp %v6,%v4,%v12,%v6
        vmaddfp %v6,%v5,%v13,%v6
        stvx    %v6,0,%r6
        addi    %r5,%r5,16
        addi    %r6,%r6,16
        addic.  %r7,%r7,-1
        bne     2b
        bdnz    1b
        blr

/* AltiVec lighting-style kernel: vaddfp, vmaddfp, vrsqrtefp, vrefp, vmaxfp */
        .globl  bench_vlight
bench_vlight:
        mtctr   %r3
        li      %r9,256
        lvx     %v10,%r4,%r9
        vxor    %v14,%v14,%v14
1:      addi    %r5,%r4,512
        addis   %r6,%r4,1
        li      %r7,256
2:      lvx     %v1,0,%r5
        vmaddfp %v2,%v1,%v1,%v14
        vrsqrtefp %v3,%v2
        vmaddfp %v4,%v1,%v10,%v14
        vmaxfp  %v4,%v4,%v14
        vrefp   %v5,%v3
        vaddfp  %v6,%v4,%v5
        vnmsubfp %v6,%v6,%v3,%v1
        stvx    %v6,0,%r6
        addi    %r5,%r5,16
        addi    %r6,%r6,16
        addic.  %r7,%r7,-1
        bne     2b
        bdnz    1b
        blr

/*
 * Sound Manager style float -> 16-bit mixer: scale, add the 1.5 * 2^23 magic
 * constant (fadds rounds to an integer), stfs, read the low half, flip the
 * sign bit; 256 samples per iteration, f14 saved/restored around it.
 */
        .globl  bench_mixer
bench_mixer:
        mtctr   %r3
        stwu    %r1,-32(%r1)
        stfd    %f14,8(%r1)
        lfs     %f10,316(%r4)           /* volume 32767.0 */
        lfs     %f11,320(%r4)           /* magic 1.5 * 2^23 */
1:      addi    %r5,%r4,512
        addis   %r6,%r4,1
        li      %r7,64
2:      lfs     %f1,0(%r5)
        lfs     %f2,4(%r5)
        lfs     %f3,8(%r5)
        lfs     %f4,12(%r5)
        fmuls   %f1,%f1,%f10
        fmuls   %f2,%f2,%f10
        fmuls   %f3,%f3,%f10
        fmuls   %f4,%f4,%f10
        fadds   %f1,%f1,%f11
        fadds   %f2,%f2,%f11
        fadds   %f3,%f3,%f11
        fadds   %f14,%f4,%f11
        stfs    %f1,0(%r6)
        stfs    %f2,4(%r6)
        stfs    %f3,8(%r6)
        stfs    %f14,12(%r6)
        lhz     %r8,2(%r6)
        lhz     %r9,6(%r6)
        lhz     %r10,10(%r6)
        lhz     %r11,14(%r6)
        xori    %r8,%r8,0x8000
        xori    %r9,%r9,0x8000
        xori    %r10,%r10,0x8000
        xori    %r11,%r11,0x8000
        sth     %r8,0(%r6)
        sth     %r9,2(%r6)
        sth     %r10,4(%r6)
        sth     %r11,6(%r6)
        addi    %r5,%r5,16
        addi    %r6,%r6,16
        addic.  %r7,%r7,-1
        bne     2b
        bdnz    1b
        lfd     %f14,8(%r1)
        addi    %r1,%r1,32
        blr

/*
 * Integer code with one lfs and one stfs per iteration and eight live
 * accumulators: shows the cost of anything that splits the TB's basic
 * blocks around a load/store conversion (globals spilled and reloaded).
 */
        .globl  bench_intlfs
bench_intlfs:
        mtctr   %r3
        addi    %r5,%r4,512
        addis   %r6,%r4,1
1:      add     %r7,%r7,%r3
        lfs     %f1,0(%r5)
        add     %r8,%r8,%r7
        add     %r9,%r9,%r8
        add     %r10,%r10,%r9
        stfs    %f1,0(%r6)
        add     %r11,%r11,%r10
        add     %r12,%r12,%r11
        add     %r0,%r0,%r12
        add     %r7,%r7,%r0
        bdnz    1b
        blr

/* integer baseline: empty counted loop with 8 adds */
        .globl  bench_int
bench_int:
        mtctr   %r3
1:      addi    %r5,%r5,1
        addi    %r6,%r6,1
        addi    %r7,%r7,1
        addi    %r8,%r8,1
        addi    %r5,%r5,1
        addi    %r6,%r6,1
        addi    %r7,%r7,1
        addi    %r8,%r8,1
        bdnz    1b
        blr
