/* First genuine guest content (ARCHITECTURE.md 4.3).
 *
 * A frame is sampled at one pseudo-random point in each cell of an 80x60
 * grid (4800 samples; every pixel for tiny surfaces); a sample is classified
 * by the mean of its 2x2 block, so Mac OS's 1-pixel 50 % dither (its startup
 * background on a built-in LCD) reads as its mean gray, and a dither never
 * aliases into uniform white or black. The caller inspects on
 * every DAMAGE and on a timer. Mac OS paints its startup gray top-down, and
 * a frame caught while it is still being painted changes at the next
 * refresh, so both rules need a frame that has stopped changing:
 *   (gray)     >= 97 % of samples are within +-12 (every channel) of one gray
 *              level G >= RB_CONTENT_MIN_GRAY, and no sample changed for
 *              >= 48 ms (three 16 ms refreshes: the frame held on two or more
 *              consecutive refreshes), or
 *   (nonblack) >= 25 % of samples are non-black, and at most 1 % of the
 *              samples differ from the frame seen 500 ms earlier (small
 *              animations such as a progress bar are allowed).
 * Black samples never count towards either rule.
 */
#pragma once
#include <stdbool.h>
#include <stdint.h>

#define RB_CONTENT_GRID_X 80
#define RB_CONTENT_GRID_Y 60 /* 4800 samples */
#define RB_CONTENT_SAMPLES (RB_CONTENT_GRID_X * RB_CONTENT_GRID_Y)
#define RB_CONTENT_GRAY_TOL 12
#define RB_CONTENT_MIN_GRAY 32     /* a "non-black" gray level */
#define RB_CONTENT_NONBLACK_MAX 24 /* channel max <= this is black */
#define RB_CONTENT_GRAY_PCT 97
#define RB_CONTENT_NONBLACK_PCT 25
#define RB_CONTENT_CHANGED_PCT 1         /* a settled frame differs in at most 1 % of samples */
#define RB_CONTENT_GRAY_SETTLE_MS 48     /* three 16 ms refreshes */
#define RB_CONTENT_NONBLACK_SETTLE_MS 500

enum rb_content_rule { RB_CONTENT_NONE = 0, RB_CONTENT_GRAY = 1, RB_CONTENT_NONBLACK = 2 };

typedef struct {
    bool detected;
    int rule;           /* rule that fired */
    int inspections;    /* inspections since reset */
    uint64_t static_since_ms;  /* last inspection at which any sample changed */
    uint64_t settled_since_ms; /* start of the current settling period (ref taken) */
    int last_gray_pct;  /* last frame statistics (for logs) */
    int last_gray_level;
    int last_nonblack_pct;
    int last_changed_pct; /* samples that differ from the reference frame */
    int last_settled_ms;
    int last_static_ms;
    int samples;
    bool have_ref;
    uint32_t ref[RB_CONTENT_SAMPLES];  /* samples when the settling period began */
    uint32_t prev[RB_CONTENT_SAMPLES]; /* samples at the previous inspection */
} rb_content;

void rb_content_reset(rb_content *c);
/* Inspect the current frame at time now_ms (any monotonic millisecond clock).
 * Returns the rule that fired (once), else NONE. */
int rb_content_inspect(rb_content *c, const uint32_t *px, int w, int h, int stride_px, uint64_t now_ms);
/* The grid samples of a frame (as rb_content_inspect takes them): raw[i] the
 * pixel (RGB), mean[i] the mean of its 2 x 2 block. Returns how many (at most
 * RB_CONTENT_SAMPLES). */
int rb_content_sample(const uint32_t *px, int w, int h, int stride_px, uint32_t *raw, uint32_t *mean);
