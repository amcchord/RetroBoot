/* Frame log (lab): every frame a virtual output (drm.h, --device
 * virtual:WxH) presents, so the presenter's own output can be captured frame
 * by frame on a machine without a display (presenter/tools/framelog.py reads
 * and renders it).
 *
 * The same format records the guest's frames as the presenter receives them
 * (--record-guest), whose size changes with Mac OS's mode.
 *
 * File: "RBFL", u32 version (1), then one record per frame:
 *   "FRAM", u32 flags, u64 t_us (CLOCK_MONOTONIC: the refresh at which a
 *   presented frame shows, or when a guest frame arrived), u32 width,
 *   u32 height, u32 bands, then `bands` x { i32 x, y, w, h; w * h XRGB8888
 *   pixels, rows packed }.
 * flags: 1 the first frame of the log, 2 a new size (the canvas starts black
 * at width x height); both are written whole. A band is a run of rows of a
 * changed rectangle that differ from the previous frame, so replaying the
 * bands in order on one canvas gives every frame exactly. All integers are
 * little-endian (the lab hosts' order). */
#pragma once
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>

#include "region.h"

#define RB_FRAMELOG_VERSION 1u
#define RB_FRAMELOG_FIRST 1u
#define RB_FRAMELOG_RESIZE 2u

struct rb_framelog {
    FILE *f;
    int w, h;
    uint32_t *canvas; /* the frame last written */
    uint64_t frames, bytes;
    bool resized;     /* the next frame is written whole with RB_FRAMELOG_RESIZE */
    int error;        /* a write failed: the log stops (lab tool, never the display) */
};

/* Create `path` for frames of w x h. Returns 0 or -1 (errno). */
int rb_framelog_open(struct rb_framelog *l, const char *path, int w, int h);
/* Append the frame px (w x h, stride_px) shown from t_us on; rects (frame
 * pixels, n of them) cover everything that may have changed since the last
 * frame (ignored for the first frame, which is written whole). Returns the
 * number of bands written, or -1. */
int rb_framelog_add(struct rb_framelog *l, uint64_t t_us, const uint32_t *px, int stride_px, const rb_rect *rects,
                    int n);
/* The next frame has another size (a guest mode switch): it is written
 * whole with RB_FRAMELOG_RESIZE. Returns 0 or -1. */
int rb_framelog_resize(struct rb_framelog *l, int w, int h);
void rb_framelog_close(struct rb_framelog *l);
