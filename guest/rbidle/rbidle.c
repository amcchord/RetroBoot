/*
 * RetroBoot Idle - a 68k startup extension that lets an emulated 68040 Mac
 * stop its processor while Mac OS has nothing to do (D044, LEGACY-02).
 *
 * The problem.  68k Mac OS never halts the CPU: with no events it keeps
 * running its idle loop, so the emulator (QEMU's q800) burns a host core at
 * 100 % at an idle Finder.  Basilisk II solves this inside its emulator
 * ("idlewait"): it patches Mac OS's idle-time routine, SynchIdleTime, and
 * sleeps there until the next interrupt when the event queue is empty
 * (macemu rsrc_patches.cpp patch_idle_time, emul_op.cpp
 * M68K_EMUL_OP_IDLE_TIME).  RetroBoot Idle does the same from inside Mac OS
 * with the 68040's own STOP instruction, so it needs nothing from the
 * emulator: STOP #$2000 waits for the next interrupt and QEMU idles the host
 * thread meanwhile.
 *
 * What it patches.  The idle routine Mac OS 7.5.x and 8.x install in the
 * system heap contains, within 0x80 bytes before the Power Manager call
 * "moveq #3,d0 ; _PowerDispatch" (7003 A09F), the instruction pair
 *
 *     movea.l ExpandMem($2B6).w,a0 ; lea $80(a0),a0      2078 02B6 41E8 0080
 *
 * (Basilisk II's signature).  At startup this extension scans the system
 * heap for that exact signature and replaces the 8 bytes with
 *
 *     jsr handler(abs.l) ; nop                           4EB9 hhhh hhhh 4E71
 *
 * The handler (rbidle_block below, copied into a non-relocatable block in the
 * system heap) ends with the work of the two replaced instructions (as
 * movea.l + adda.w, which leave the condition codes alone like lea, so the
 * handler never contains the signature itself): the patched routine sees the
 * same A0 and condition codes as before.  A0 is free to use
 * inside the handler because the replaced code loads it.
 *
 * When it stops.  With interrupts masked (so nothing can post an event
 * between the test and the STOP), the handler checks that
 *   - the OS event queue is empty (EventQueue.qHead, $14C, is nil), and
 *   - the caller ran in supervisor mode on the interrupt stack with no trace
 *     and interrupt mask 0 (saved SR high byte exactly $20),
 * counts consecutive idle calls within the current tick (Ticks, $16A), and
 * from the min_run-th such call on executes STOP #$2000.  STOP loads SR =
 * $2000 (mask 0) and waits for any interrupt; an interrupt that arrived while
 * the mask was raised is still pending and ends the STOP at once, so no event
 * is ever waited on for longer than it takes the next interrupt to come
 * (the Quadra's 60.15 Hz VIA1 tick at the latest).  Every event source of a
 * Mac (keyboard and mouse via ADB, timers, sound, SCSI, Ethernet, serial,
 * VBL) is an interrupt, and a task-level PostEvent cannot run while this
 * task sits in the idle routine.  After the interrupt the original SR is
 * restored.
 *
 * min_run (default RBIDLE_MIN_RUN) keeps programs that poll for events once
 * per frame from losing time: the first idle call(s) of each tick never stop,
 * only a caller that keeps finding nothing to do within the same tick does
 * (the Process Manager's idle loop, many thousand calls a second).  See
 * guest/reports/quadra-guest-2026-10-24.md for the measurements.
 *
 * When it does nothing.  On anything but a 68040 (Gestalt 'proc'), on a
 * PowerPC Mac ('cput'), before System 7.5, with virtual memory on (Mac OS
 * may then run tasks in user mode, where STOP and moving from SR trap), when
 * it is already active (Gestalt 'RBid' exists: a second copy installs
 * nothing), when no signature is found, or when more than RBIDLE_MAX_SITES
 * are found.  It never patches a site twice: a patched site no longer
 * matches the signature.
 *
 * Checking it from outside.  Gestalt('RBid') returns the address of the info
 * block (layout below: magic "RBidIdle", layout version, patched sites,
 * idle-call and STOP counters, the patched addresses).  A lab tool can find
 * the block in a RAM dump by its magic and verify each recorded site holds
 * "jsr handler ; nop" (guest/rbidle/tools/rbidle_check.py).
 *
 * Original RetroBoot code (MIT).  Built with Retro68 as a flat 68k 'INIT'
 * code resource (build.sh); the handler is position-independent.
 */
#include <Gestalt.h>
#include <MacMemory.h>
#include <OSUtils.h>
#include <LowMem.h>
#include "Retro68Runtime.h"

#ifndef RBIDLE_MIN_RUN
#define RBIDLE_MIN_RUN 4
#endif
#define RBIDLE_MAX_SITES 4
#define RBIDLE_SEARCH_BACK 0x80          /* bytes before 7003 A09F */
#define RBIDLE_VERSION 0x0100            /* 1.0, as in 'vers' */
#define kRBidSelector 'RBid'

/* Info block offsets (keep in sync with the assembly and rbidle_check.py) */
#define OFF_LAYOUT   8
#define OFF_SITES    10
#define OFF_MINRUN   26
#define OFF_SITE     28
#define OFF_VERSION  44
#define OFF_HANDLER  48

#define STR(x) #x
#define XSTR(x) STR(x)

/*
 * The block copied into the system heap.  Only PC-relative references, so it
 * runs wherever it is copied.  Offsets: 0 magic "RBidIdle", 8 u16 layout
 * version (1), 10 u16 patched sites, 12 u32 idle calls, 16 u32 STOPs, 20 u32
 * tick of the current run, 24 u16 idle calls in that tick, 26 u16 min_run,
 * 28 u32[4] patched addresses, 44 u32 RetroBoot Idle version, 48 handler.
 */
__asm__(
"    .section .text.rbidle_block,\"ax\",@progbits\n"
"    .globl rbidle_block\n"
"    .globl rbidle_block_end\n"
"    .even\n"
"rbidle_block:\n"
"    .ascii \"RBidIdle\"\n"
"    .short 1\n"                       /*  8 layout version */
"    .short 0\n"                       /* 10 patched sites */
"    .long 0\n"                        /* 12 idle calls */
"    .long 0\n"                        /* 16 STOPs */
"    .long 0\n"                        /* 20 tick of the current run */
"    .short 0\n"                       /* 24 idle calls in that tick */
"    .short " XSTR(RBIDLE_MIN_RUN) "\n" /* 26 min_run */
"    .long 0, 0, 0, 0\n"               /* 28 patched addresses */
"    .long 0\n"                        /* 44 version */
"rbidle_handler:\n"                    /* 48: jsr'd from a patched site */
"    move.w  %sr,-(%sp)\n"
"    ori.w   #0x0700,%sr\n"             /* no interrupt between test and STOP */
"    move.l  %d0,-(%sp)\n"
"    lea     rbidle_block(%pc),%a0\n"
"    addq.l  #1,12(%a0)\n"
"    tst.l   (0x014C).w\n"              /* EventQueue.qHead: an event waits? */
"    bne.s   1f\n"
"    cmpi.b  #0x20,4(%sp)\n"            /* caller: S, no T/M, IPL 0 */
"    bne.s   2f\n"
"    move.l  (0x016A).w,%d0\n"          /* Ticks */
"    cmp.l   20(%a0),%d0\n"
"    beq.s   3f\n"
"    move.l  %d0,20(%a0)\n"             /* first idle call in this tick */
"    clr.w   24(%a0)\n"
"3:  addq.w  #1,24(%a0)\n"
"    move.w  24(%a0),%d0\n"
"    cmp.w   26(%a0),%d0\n"
"    bcs.s   2f\n"                      /* not idle long enough yet */
"    addq.l  #1,16(%a0)\n"
"    stop    #0x2000\n"                 /* sleep until the next interrupt */
"    bra.s   2f\n"
"1:  clr.w   24(%a0)\n"                 /* busy: start a new run */
"2:  move.l  (%sp)+,%d0\n"
"    move.w  (%sp)+,%sr\n"
"    movea.l (0x02B6).w,%a0\n"          /* what the replaced instructions did; */
"    adda.w  #0x80,%a0\n"               /* adda, not lea: no copy of the signature */
"    rts\n"
"rbidle_block_end:\n"
"    .text\n");

extern const char rbidle_block[], rbidle_handler[], rbidle_block_end[];

static int is_site(const volatile UInt16 *q)
{
    return q[0] == 0x2078 && q[1] == 0x02B6 && q[2] == 0x41E8 && q[3] == 0x0080;
}

/* Collect the signature sites in [start, end); returns the count, or -1 when
 * there are more than RBIDLE_MAX_SITES (unexpected: patch nothing). */
static short find_sites(const volatile UInt16 *start, const volatile UInt16 *end,
                        volatile UInt16 **sites)
{
    const volatile UInt16 *p, *q;
    short n = 0, k;

    for (p = start + RBIDLE_SEARCH_BACK / 2; p + 2 <= end; p++) {
        if (p[0] != 0x7003 || p[1] != 0xA09F)
            continue;
        for (q = p - RBIDLE_SEARCH_BACK / 2; q + 4 <= p; q++) {
            if (!is_site(q))
                continue;
            for (k = 0; k < n && sites[k] != q; k++)
                ;
            if (k < n)
                continue;                   /* already found */
            if (n == RBIDLE_MAX_SITES)
                return -1;
            sites[n++] = (volatile UInt16 *)q;
        }
    }
    return n;
}

static int machine_ok(void)
{
    long r;

    if (Gestalt(kRBidSelector, &r) == noErr)
        return 0;                           /* already active */
    if (Gestalt(gestaltSystemVersion, &r) != noErr || (r & 0xFFFF) < 0x0750)
        return 0;
    if (Gestalt(gestaltProcessorType, &r) != noErr || r != gestalt68040)
        return 0;
    if (Gestalt(gestaltNativeCPUtype, &r) == noErr && r >= 0x0100)
        return 0;                           /* PowerPC */
    if (Gestalt(gestaltVMAttr, &r) == noErr && (r & (1L << gestaltVMPresent)))
        return 0;
    return 1;
}

static void run(void)
{
    volatile UInt16 *sites[RBIDLE_MAX_SITES];
    THz sys = LMGetSysZone();
    unsigned long size = (unsigned long)(rbidle_block_end - rbidle_block);
    unsigned long handler;
    unsigned short sr;
    char *block;
    short n, i;

    if (!machine_ok())
        return;
    n = find_sites((const volatile UInt16 *)sys, (const volatile UInt16 *)sys->bkLim, sites);
    if (n <= 0)
        return;
    block = NewPtrSys(size);
    if (block == NULL)
        return;
    BlockMoveData(rbidle_block, block, size);
    handler = (unsigned long)block + OFF_HANDLER;
    *(UInt16 *)(block + OFF_SITES) = n;
    *(UInt32 *)(block + OFF_VERSION) = RBIDLE_VERSION;
    for (i = 0; i < n; i++)
        *(UInt32 *)(block + OFF_SITE + 4 * i) = (UInt32)sites[i];

    __asm__ volatile("move.w %%sr,%0\n\tori.w #0x0700,%%sr" : "=d"(sr) : : "memory");
    for (i = 0; i < n; i++) {
        volatile UInt16 *q = sites[i];
        q[3] = 0x4E71;                                  /* nop */
        q[2] = (UInt16)handler;
        q[1] = (UInt16)(handler >> 16);
        q[0] = 0x4EB9;                                  /* jsr (abs).l */
    }
    __asm__ volatile("move.w %0,%%sr" : : "d"(sr) : "memory");
    FlushDataCache();
    FlushInstructionCache();
    NewGestaltValue(kRBidSelector, (long)block);
}

void _start(void)
{
    RETRO68_RELOCATE();
    run();
}
