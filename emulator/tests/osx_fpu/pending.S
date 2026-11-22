        .section .vectors,"ax"
        .global handler
handler:
        lwz 20,0(31)
        addi 20,20,1
        stw 20,0(31)
        mfsrr0 20
        stw 20,40(31)
        addi 20,20,4
        mtsrr0 20
        mfmsr 21
        ori 21,21,0x2000
        mtmsr 21
        isync
        mffs 30
        stfd 30,32(31)
        mtfsf 255,31
        rfi

        .section .text,"ax"
        .global _start
_start:
        lis 31,0x20
        li 3,0
        stw 3,0(31)
        stw 3,4(31)
        li 3,0x2000
        mtmsr 3
        isync
        lis 4,constants@ha
        addi 4,4,constants@l
        lfd 1,0(4)
        lfd 2,8(4)
        lfd 3,16(4)
        lfd 4,24(4)
        lfd 5,32(4)
        lfd 6,40(4)
        fmr 31,4

        /* A: disabled global delivery, XE enabled, then clear FPSCR. */
        mtfsf 255,3
        fdiv 0,1,2
        mtfsf 255,4
        li 3,0x2900
        mtmsr 3
        isync
        fcmpu 0,1,1
        lwz 3,0(31)
        stw 3,8(31)

        /* B: genuinely enabled inexact still traps after the FPR update. */
        mtfsf 255,3
        fdiv 0,1,2
        stfd 0,24(31)
        lwz 3,0(31)
        stw 3,12(31)

        /* C: retain XX while clearing XE/FEX before enabling delivery. */
        mtfsf 255,4
        li 3,0x2000
        mtmsr 3
        isync
        mtfsf 255,3
        fdiv 0,1,2
        mtfsf 255,5
        li 3,0x2900
        mtmsr 3
        isync
        fcmpu 0,1,1
        lwz 3,0(31)
        stw 3,16(31)

        /* D: enabling XE with XX already set is a legitimate deferred trap. */
        mtfsf 255,6
        lwz 3,0(31)
        stw 3,20(31)

        /* E: an FPSCR update with global FE off must not leave a trap. */
        mtfsf 255,4
        li 3,0x2000
        mtmsr 3
        isync
        mtfsf 255,6
        mtfsf 255,4
        li 3,0x2900
        mtmsr 3
        isync
        fcmpu 0,1,1
        lwz 3,0(31)
        stw 3,44(31)
        lis 3,0x600d
        ori 3,3,0xcafe
        stw 3,4(31)
done:   b done
        .align 3
constants:
        .quad 0x3ff0000000000000
        .quad 0x4008000000000000
        .quad 0x0000000000000008
        .quad 0x0000000000000000
        .quad 0x0000000002000000
        .quad 0x0000000002000008
