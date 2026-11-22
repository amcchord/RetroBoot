/*
 * RBFrames - lab test for RetroBoot Idle (not shipped; MIT, RetroBoot).
 *
 * A game-like loop: per "frame" it does a fixed amount of CPU work, inverts a
 * small rectangle in its window (drawing) and polls for events once, the way
 * many 68k games do.  RetroBoot Idle must not throttle such a program in a way
 * a user would notice.  For each frame cost (0, 1, 4 and 12 ms of work,
 * calibrated at startup) and each event call (WaitNextEvent with sleep 0;
 * SystemTask + GetNextEvent; EventAvail; no event call at all) it counts
 * frames for RBF_TICKS ticks and reads RetroBoot Idle's counters (Gestalt
 * 'RBid') before and after.  Results go to "RBFrames Results" (text) in the
 * application's folder; then it shuts the Mac down (ShutDwnPower), so a lab
 * run can put it in Startup Items and read the file from the disk afterwards.
 */
#include <Quickdraw.h>
#include <Fonts.h>
#include <Windows.h>
#include <Menus.h>
#include <TextEdit.h>
#include <Dialogs.h>
#include <Events.h>
#include <OSUtils.h>
#include <Files.h>
#include <Gestalt.h>
#include <ShutDown.h>
#include <ToolUtils.h>
#include <stdio.h>
#include <string.h>

#define RBF_TICKS 240             /* 4 s per measurement */

static volatile unsigned long sink;
static unsigned long units_per_ms;
static char out[8192];
static long outlen;
static WindowPtr win;
static Rect spot = {10, 10, 30, 30};

static void work(unsigned long units)
{
    unsigned long i, s = sink;
    for (i = 0; i < units; i++)
        s = (s << 1) ^ (s >> 3) ^ i;
    sink = s;
}

static void say(const char *s)
{
    long n = strlen(s);
    if (outlen + n < (long)sizeof out) {
        memcpy(out + outlen, s, n);
        outlen += n;
    }
}

static void counters(unsigned long *calls, unsigned long *stops)
{
    long blk;
    *calls = *stops = 0;
    if (Gestalt('RBid', &blk) == noErr && blk) {
        *calls = *(unsigned long *)(blk + 12);
        *stops = *(unsigned long *)(blk + 16);
    }
}

static void calibrate(void)
{
    unsigned long t0, ticks, n = 100000;

    for (;;) {
        t0 = TickCount();
        while (TickCount() == t0)
            ;
        t0 = TickCount();
        work(n);
        ticks = TickCount() - t0;
        if (ticks >= 60 || n > 0x10000000UL)
            break;
        n *= 2;
    }
    /* n units took ticks/60.15 s */
    units_per_ms = (unsigned long)((double)n * 60.15 / (double)ticks / 1000.0);
}

enum { EV_WNE0, EV_GNE, EV_AVAIL, EV_NONE };
static const char *ev_name[] = {"WaitNextEvent(0)", "SystemTask+GetNextEvent", "EventAvail", "none"};

static void measure(int ev, int ms)
{
    EventRecord e;
    unsigned long frames = 0, t0, t1, c0, s0, c1, s1;
    unsigned long units = units_per_ms * (unsigned long)ms;
    char line[160];
    long fps10;

    SetPort(win);
    counters(&c0, &s0);
    t0 = TickCount();
    while ((t1 = TickCount()) - t0 < RBF_TICKS) {
        work(units);
        InvertRect(&spot);
        switch (ev) {
        case EV_WNE0: WaitNextEvent(everyEvent, &e, 0, NULL); break;
        case EV_GNE: SystemTask(); GetNextEvent(everyEvent, &e); break;
        case EV_AVAIL: EventAvail(everyEvent, &e); break;
        default: break;
        }
        frames++;
    }
    counters(&c1, &s1);
    fps10 = (long)((double)frames * 601.5 / (double)(t1 - t0));   /* 60.15 Hz ticks; x10 */
    sprintf(line, "%-24s work_ms=%2d frames=%6lu ticks=%lu fps=%ld.%ld idle_calls=%lu stops=%lu\r",
            ev_name[ev], ms, frames, t1 - t0, fps10 / 10, fps10 % 10, c1 - c0, s1 - s0);
    say(line);
}

static void save(void)
{
    FSSpec spec;
    short ref;
    long n = outlen;
    OSErr err;

    err = FSMakeFSSpec(0, 0, "\pRBFrames Results", &spec);
    if (err == noErr)
        FSpDelete(&spec);
    if (FSpCreate(&spec, 'ttxt', 'TEXT', smSystemScript) != noErr)
        return;
    if (FSpOpenDF(&spec, fsRdWrPerm, &ref) != noErr)
        return;
    FSWrite(ref, &n, out);
    FSClose(ref);
    FlushVol(NULL, spec.vRefNum);
}

int main(void)
{
    static const int costs[] = {0, 2, 5, 11, 25};
    Rect r = {60, 60, 160, 260};
    EventRecord e;
    unsigned long t0, calls, stops;
    long blk = 0, sysv = 0;
    char line[160];
    int ev, k;

    InitGraf(&qd.thePort);
    InitFonts();
    InitWindows();
    InitMenus();
    TEInit();
    InitDialogs(NULL);
    InitCursor();
    win = NewWindow(NULL, &r, "\pRBFrames", true, documentProc, (WindowPtr)-1, false, 0);
    /* let the Finder and the other startup items settle */
    t0 = TickCount();
    while (TickCount() - t0 < 600)
        WaitNextEvent(everyEvent, &e, 30, NULL);
    Gestalt('sysv', &sysv);
    Gestalt('RBid', &blk);
    calibrate();
    counters(&calls, &stops);
    sprintf(line, "RBFrames sysv=%04lx rbidle=%s min_run=%d units_per_ms=%lu\r", sysv & 0xFFFF,
            blk ? "active" : "absent", blk ? *(unsigned short *)(blk + 26) : 0, units_per_ms);
    say(line);
    for (k = 0; k < 5; k++)
        for (ev = 0; ev < 4; ev++)
            measure(ev, costs[k]);
    /* the idle Finder-like case: an application waiting with a long sleep */
    counters(&calls, &stops);
    t0 = TickCount();
    while (TickCount() - t0 < RBF_TICKS)
        WaitNextEvent(everyEvent, &e, 60, NULL);
    {
        unsigned long c1, s1;
        counters(&c1, &s1);
        sprintf(line, "%-24s idle_calls=%lu stops=%lu over %d ticks\r", "WaitNextEvent(60) idle", c1 - calls,
                s1 - stops, RBF_TICKS);
        say(line);
    }
    save();
    ShutDwnPower();
    return 0;
}
