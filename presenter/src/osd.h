/* Key feedback indicator (ARCHITECTURE.md section 4.7): the on-screen
 * indicator for the brightness, volume and mute keys when Mac OS shows none of
 * its own (brightness, every key while Mac OS is not running). hostd asks for
 * it with the "osd" request; the look is rbui's Mac OS 9 style feedback window
 * (rbui_draw_feedback), integer-scaled by the pixel scale, centred near the
 * bottom of the Mac OS screen (or of the panel), held for RB_OSD_HOLD_MS and
 * faded out over RB_OSD_FADE_MS. It is drawn into the presenter's composition
 * only (never the Mac OS framebuffer, the remote tap or a guest screenshot)
 * and only over a revealed Mac OS or the panel: never over the checkerboard,
 * the startup indicator, a hand-over or a held last frame. */
#pragma once
#include <stdbool.h>
#include <stdint.h>

#include "region.h"

#define RB_OSD_HOLD_MS 1500
#define RB_OSD_FADE_MS 250
#define RB_OSD_STEP_MS 25        /* fade frames */
#define RB_OSD_MARGIN 2          /* logical pixels above the bottom edge (as Mac OS 9's own) */

enum rb_osd_kind { RB_OSD_VOLUME = 0, RB_OSD_MUTE = 1, RB_OSD_BRIGHTNESS = 2 };

struct rb_osd {
    bool active;                 /* shown or fading */
    enum rb_osd_kind kind;
    double level;                /* 0..1 */
    bool muted;
    uint32_t *img;               /* logical render, w x h */
    int w, h;
    uint64_t shown_ms;           /* CLOCK_MONOTONIC of the last update */
    int alpha;                   /* 0..255 as last composed */
};

/* Frees the render; the indicator is inactive. */
void rb_osd_free(struct rb_osd *o);
/* Render kind/level/muted (rbui) and make the indicator active from now_ms.
 * Returns -1 on allocation failure. */
int rb_osd_show(struct rb_osd *o, enum rb_osd_kind kind, double level, bool muted, uint64_t now_ms);
/* Opacity at now_ms: 255 during the hold, then linear to 0 over the fade. */
int rb_osd_alpha(const struct rb_osd *o, uint64_t now_ms);
/* Where it is drawn (frame pixels): centred on `area` (the Mac OS picture or
 * the whole frame), RB_OSD_MARGIN logical pixels above its bottom, scaled by
 * s >= 1, kept inside the frame (W x H). Empty when inactive. */
rb_rect rb_osd_rect(const struct rb_osd *o, rb_rect area, int s, int W, int H);
/* Blend the indicator at `alpha` into dst over the part of `target` (from
 * rb_osd_rect) inside `clip`, nearest-neighbour scaled by s. */
void rb_osd_render(const struct rb_osd *o, uint32_t *dst, int stride_px, rb_rect target, rb_rect clip, int s,
                   int alpha);
