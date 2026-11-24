/* Display policy (ARCHITECTURE.md 4.5) and guest scaling.
 *
 * integer: largest integer factor k with src*k fitting the screen, centered,
 *          nearest neighbour (sharp). Falls back to fit when k would be 0.
 * fit:     aspect-preserving; "sharp bilinear": nearest-neighbour prescale by
 *          k = floor(f) followed by bilinear filtering to the final size,
 *          computed in one pass from per-column/per-row tables.
 */
#pragma once
#include <stdbool.h>
#include <stdint.h>

#include "region.h"

enum rb_scaling { RB_SCALING_INTEGER = 0, RB_SCALING_FIT = 1 };

/* Guest size "auto" (4.5): (floor(w/s/8)*8, floor(h/s)) clamped to the
 * emulator's -g range. hostd's machines.guest_geometry is the same rule.
 * The maximum is the largest Mac OS screen verified with the r3 package and
 * the RetroBoot video driver (3840x2160 at 32 bits fits the 64 MB VRAM and
 * QuickDraw's 14-bit rowBytes). */
#define RB_GUEST_MIN_W 640
#define RB_GUEST_MIN_H 480
#define RB_GUEST_MAX_W 3840
#define RB_GUEST_MAX_H 2160
/* A portrait display (h > w, e.g. a rotated panel, 4.5): the same range on
 * its side, except that the narrowest Mac screen is 512 pixels wide (the
 * video driver's smallest live resize). */
#define RB_GUEST_PMIN_W 512
#define RB_GUEST_PMIN_H 640
#define RB_GUEST_PMAX_W 2160
#define RB_GUEST_PMAX_H 3840
/* Automatic scale: the largest s with long/s >= 640 and short/s >= 480
 * (orientation-independent, so a rotation keeps the scale). */
int rb_suggested_scale(int w, int h);
void rb_auto_guest_size(int w, int h, int s, int *gw, int *gh);
/* The -g range for a w x h display (its orientation picks the range). */
void rb_guest_limits(int w, int h, int *min_w, int *min_h, int *max_w, int *max_h);
/* The guest target for a w x h display at scale s: the auto size when cfg_w
 * or cfg_h is 0, else the configured classic size, turned on its side on a
 * portrait display when that is a valid portrait size (800x600 -> 600x800,
 * 1024x768 -> 768x1024, 1280x1024 -> 1024x1280; 640x480 and 1152x870 stay). */
void rb_guest_size_for(int w, int h, int s, int cfg_w, int cfg_h, int *gw, int *gh);

/* Pixel scale (display.pixel_scale, 4.5): every Mac pixel is an s x s block.
 * 0 = auto (rb_suggested_scale). A numeric scale s is valid for a w x h
 * display when the unclamped Mac screen at s, (floor(w/s/8)*8, floor(h/s)),
 * lies within the -g range. The effective scale for a request is the request
 * when valid, else the largest valid smaller scale, else the smallest valid
 * larger one, else the suggested scale (displays below 640x480). hostd
 * (machines.pixel_scale_choices) and image rb-bootdisplay use the same rule. */
#define RB_PIXEL_SCALE_MAX 4
void rb_scaled_guest_size(int w, int h, int s, int *gw, int *gh);
bool rb_pixel_scale_valid(int w, int h, int s);
int rb_effective_scale(int w, int h, int requested);
/* "retroboot.pixel_scale=auto|1..4" on a kernel command line (GRUB passes the
 * configured scale so the splash and the presenter draw GRUB's checkerboard);
 * returns 0 for auto, absent or invalid. */
int rb_cmdline_pixel_scale(const char *cmdline);
/* rb_cmdline_pixel_scale of a file (NULL = /proc/cmdline); 0 when unreadable. */
int rb_boot_pixel_scale(const char *path);

typedef struct {
    int screen_w, screen_h;
    int src_w, src_h;
    enum rb_scaling requested;
    enum rb_scaling mode; /* effective */
    int k;                /* integer factor (integer) or prescale (fit) */
    rb_rect dst;          /* destination on screen */
    /* fit tables, indexed by destination column/row relative to dst */
    int *x0, *x1, *y0, *y1;
    uint8_t *xw, *yw; /* weight of x1/y1 in 1/256 */
} rb_layout;

int rb_layout_compute(rb_layout *l, int screen_w, int screen_h, int src_w, int src_h, enum rb_scaling mode);
/* integer: k is at most max_k (0 = no limit; the chosen pixel scale when it
 * fits, else the largest fitting factor) and the centered origin is rounded
 * down to a multiple of grid, so the guest's blocks line up with the
 * checkerboard/panel grid of scale `grid`. fit ignores both. */
int rb_layout_compute_ex(rb_layout *l, int screen_w, int screen_h, int src_w, int src_h, enum rb_scaling mode,
                         int max_k, int grid);
/* The same integer layout on the logical (1/s) grid, for drawing the guest
 * into the panel's logical buffer: returns 0 and fills `out` (no tables) when
 * the physical layout is integer with k and its origin multiples of s, else -1. */
int rb_layout_logical(const rb_layout *phys, int s, rb_layout *out);
/* Panel dimmed-background layout matching `phys->dst` on the 1/s logical grid
 * even when k is not divisible by s (e.g. 800x600 at 1x on a 2x screen) or
 * `phys` uses fit scaling. Caller frees `out` with rb_layout_free(). */
int rb_layout_panel(const rb_layout *phys, int s, rb_layout *out);
void rb_layout_free(rb_layout *l);

/* Screen rect that must be redrawn when source rect r changes (clipped to dst). */
rb_rect rb_layout_map_damage(const rb_layout *l, rb_rect r);

/* Render screen rect `area` (intersected with dst) from the source buffer. */
void rb_layout_render(const rb_layout *l, uint32_t *dst, int dst_stride, const uint32_t *src, int src_stride,
                      rb_rect area);

/* Map a screen coordinate to a source pixel (clamped). */
void rb_layout_screen_to_src(const rb_layout *l, int sx, int sy, int *gx, int *gy);

/* Screen pixels per source pixel (horizontal), for relative pointer motion. */
double rb_layout_ratio(const rb_layout *l);

/* Nearest-neighbour upscale by integer s of logical rect `lr` from a logical
 * buffer into a physical buffer (used for the classic panel). */
void rb_upscale_rect(uint32_t *dst, int dst_stride, int dst_w, int dst_h, const uint32_t *src, int src_stride,
                     rb_rect lr, int s);
