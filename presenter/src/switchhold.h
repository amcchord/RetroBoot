/* Guest-initiated mode switches (ARCHITECTURE 4.5): Mac OS changed its own
 * screen size or depth (RetroBoot Display with the ATI Radeon, Monitors, a
 * game). QEMU sends the new surface at once with whatever video memory holds
 * at that moment: the previous picture read at the new stride (ati-vga: one
 * garbled frame), black (a cleared VRAM), Mac OS's clear (its 50 % dither) or
 * a desktop in mid-redraw. The presenter keeps the last good frame on screen
 * and cuts to the new surface once it is complete:
 *
 *   - it has changed since it arrived (what arrived is stale), or nothing has
 *     been drawn for RB_SWITCH_STALE_MS (it arrived complete);
 *   - it is not a uniform fill: black, or one gray level (Mac OS's clear and
 *     its dither), each over >= RB_SWITCH_UNIFORM_PERMILLE of the samples (a
 *     menu bar alone is enough to tell a desktop from a clear even at 3840 x
 *     2160);
 *   - and Mac OS has drawn nothing (no DAMAGE) for RB_SWITCH_SETTLE_MS (the
 *     Finder's redraw pauses for 50 ms between the desktop, the Control Strip
 *     and the icons on an idle builder, 200-350 ms on a loaded one), or the
 *     picture has kept changing for RB_SWITCH_LIVE_MS, drawn at least
 *     RB_SWITCH_LIVE_DRAWS times in the last RB_SWITCH_LIVE_MS (an animation
 *     or a game, shown rather than frozen; the Finder's redraw is a handful of
 *     draws);
 *
 * and at the latest after RB_SWITCH_MAX_MS. Pure: the caller inspects on
 * every DAMAGE (passing its time) and on a timer. */
#pragma once
#include <stdbool.h>
#include <stdint.h>

#include "content.h"

#define RB_SWITCH_SETTLE_MS 400
#define RB_SWITCH_LIVE_MS 400
#define RB_SWITCH_LIVE_DRAWS 8 /* 20 frames a second; the Finder's redraw: 4-6 draws */
#define RB_SWITCH_STALE_MS 600
#define RB_SWITCH_MAX_MS 2000
#define RB_SWITCH_UNIFORM_PERMILLE 995

enum rb_switch_verdict {
    RB_SWITCH_HOLD = 0,  /* keep the last good frame */
    RB_SWITCH_SETTLED,   /* complete: redrawn and still */
    RB_SWITCH_LIVE,      /* complete: changing content (animation) */
    RB_SWITCH_AS_IS,     /* complete: it arrived complete and nothing was drawn since */
    RB_SWITCH_TIMEOUT,   /* the longest hold */
};

struct rb_switch_hold {
    uint64_t start_ms;
    uint64_t live_since_ms; /* non-uniform and changed since (0: not now) */
    bool changed;           /* differs from the frame that arrived */
    bool uniform;           /* the last inspection saw a uniform fill */
    int samples;
    int last_uniform_permille;
    uint64_t draws[RB_SWITCH_LIVE_DRAWS]; /* the latest DAMAGE times (ring) */
    int ndraws, draw_head;
    uint32_t arrival[RB_CONTENT_SAMPLES];
};

const char *rb_switch_verdict_name(int v);
/* The new surface arrived (px w x h); now_ms is any monotonic clock. */
void rb_switch_hold_begin(struct rb_switch_hold *s, const uint32_t *px, int w, int h, int stride_px,
                          uint64_t now_ms);
/* Inspect the surface: RB_SWITCH_HOLD while it is not complete, else why it
 * is. last_damage_ms: when the guest last drew (0: never since begin); each
 * new value counts as one draw. */
int rb_switch_hold_check(struct rb_switch_hold *s, const uint32_t *px, int w, int h, int stride_px,
                         uint64_t now_ms, uint64_t last_damage_ms);
