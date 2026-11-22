/* Output transform: how one output shows the presenter's composed frame.
 *
 * The presenter composes a single logical frame (the shadow, src_w x src_h).
 * Every DRM output shows that frame:
 *   - rotated for its panel orientation: `rot` is the clockwise rotation (0,
 *     90, 180, 270) applied to the content on its way into the scanout
 *     buffer (the connector's "panel orientation" property: Right Side Up ->
 *     90, Left Side Up -> 270, Upside Down -> 180; the same convention as the
 *     kernel's fbcon rotation hint);
 *   - for mirrors, scaled into the output's logical (rotated) size by the
 *     largest integer factor k that fits, or a nearest-neighbour fit when even
 *     1x does not fit (k = 0), and centered; the remaining border is black or
 *     continues the checkerboard pattern of the frame. When the frame is drawn
 *     at an integer pixel scale (src_scale, the presenter's s) and 1x does not
 *     fit, the fit takes an exact 1/d of it for the smallest divisor d of
 *     src_scale that fits (div = d): one frame pixel per d x d block, so a 2x
 *     high-DPI panel's frame shows on a 1080p mirror with every Mac pixel exact.
 * The primary output shows the frame 1:1 (its logical size is the frame).
 *
 * Coordinates: "frame" = shadow pixels; "logical" = the output after
 * rotation (ow x oh); "fb" = scanout buffer pixels (fb_w x fb_h). */
#pragma once
#include <stdbool.h>
#include <stdint.h>

#include "checker.h"
#include "region.h"

enum rb_border { RB_BORDER_BLACK = 0, RB_BORDER_CHECKER = 1, RB_BORDER_OSX_GRAY = 2 };

struct rb_xform {
    int rot;          /* 0, 90, 180, 270 (clockwise, content -> scanout) */
    int fb_w, fb_h;   /* scanout size */
    int ow, oh;       /* logical size (fb size with 90/270 swapped) */
    int src_w, src_h; /* frame size */
    rb_rect img;      /* frame image in logical coordinates */
    int k;            /* integer scale (>= 1), 0 = nearest fit */
    int src_scale;    /* frame pixel scale (kept across setup); 0/1 = none */
    int div;          /* k = 0 only: exact 1/div reduction, 0 = arbitrary fit */
    int *xmap, *ymap; /* fit only: image-relative logical -> frame pixel */
    bool identity;    /* rot 0, 1:1, image covers the output */
    int border;       /* enum rb_border */
    int checker_s;    /* pattern cell (frame pixels) */
    struct rb_checker_anchor checker_anchor; /* frame pixel where a black cell begins */
};

int rb_rotation_from_orientation(const char *name); /* DRM enum name -> clockwise degrees, -1 unknown */
const char *rb_rotation_name(int rot);
/* Logical size of a scanout of fb_w x fb_h under rotation rot. */
void rb_rotated_size(int rot, int fb_w, int fb_h, int *ow, int *oh);

/* (Re)configure. mirror=false: frame must equal the logical size (1:1). */
int rb_xform_setup(struct rb_xform *x, int rot, int fb_w, int fb_h, int src_w, int src_h, bool mirror);
void rb_xform_free(struct rb_xform *x);
void rb_xform_set_border(struct rb_xform *x, int border, int checker_s, struct rb_checker_anchor anchor);
/* Frame pixel scale for exact reductions (takes effect at the next setup). */
void rb_xform_set_src_scale(struct rb_xform *x, int s);

/* Map a logical point to the scanout and back. */
void rb_xform_l2f(const struct rb_xform *x, int lx, int ly, int *fx, int *fy);
void rb_xform_f2l(const struct rb_xform *x, int fx, int fy, int *lx, int *ly);
/* Scanout rect covered by logical rect r. */
rb_rect rb_xform_lrect_to_fb(const struct rb_xform *x, rb_rect r);
/* Scanout rect that shows frame rect r (conservative for fit). */
rb_rect rb_xform_src_to_fb(const struct rb_xform *x, rb_rect r);

/* Scanout point -> frame point (clamped to the frame; the border maps to the
 * nearest image edge). For absolute pointers and touchscreens. */
void rb_xform_fb_to_frame(const struct rb_xform *x, double fx, double fy, double *sx, double *sy);
/* Scanout position (continuous: pixel i covers [i, i+1)) -> the frame pixel
 * shown there, exactly: the scanout pixel under it, through the rotation, then
 * the mirror's integer block (or the fit's map). Clamped to the frame; the
 * border gives the nearest image edge. Touchscreens and pens (touch.h). */
void rb_xform_fb_point_to_frame(const struct rb_xform *x, double fx, double fy, int *sx, int *sy);

/* Draw frame rect r (clipped to the frame) into the scanout buffer. */
void rb_xform_blit(const struct rb_xform *x, uint32_t *fb, int fb_stride_px, const uint32_t *src, int src_stride_px,
                   rb_rect r);
/* Fill everything outside the image with the border. */
void rb_xform_fill_border(const struct rb_xform *x, uint32_t *fb, int fb_stride_px);
/* The 1:1 fast path of rb_xform_blit (tiled, NEON on arm64) is on unless
 * RB_XFORM_GENERIC=1; tests switch it to compare both. */
bool rb_xform_fast_enabled(void);
void rb_xform_force_generic(int on);

