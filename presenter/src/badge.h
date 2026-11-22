/* Remote-viewer indicator (ARCHITECTURE.md section 4.6): a small menu-bar
 * style glyph the presenter draws over its own composition while a paired
 * browser watches (eye) or may control (monitor and pointer) the screen.
 * Never written into the Mac OS framebuffer, the tap buffer or screenshots of
 * the guest. Over Mac OS it sits only in an empty stretch of the Mac OS menu
 * bar (right end of the widest gap, next to the clock), so it covers nothing
 * of the Mac's; with no menu bar (a full-screen game) it is not drawn. Over
 * the classic panel and the checkerboard it sits in the top-right corner. */
#pragma once
#include <stdbool.h>
#include <stdint.h>

#include "region.h"

#define RB_BADGE_W 16
#define RB_BADGE_H 16
#define RB_BADGE_MENU_Y 2      /* glyph row inside the 20-pixel Mac OS menu bar */
#define RB_BADGE_MENU_H 20
#define RB_BADGE_BOX 20        /* panel/checkerboard: glyph on a 20 x 20 box */

enum rb_badge_style { RB_BADGE_VIEW = 0, RB_BADGE_CONTROL = 1 };

/* Find room for the glyph in a Mac OS menu bar (rows 0..19 of the guest
 * framebuffer). The menu bar background is the column profile that occurs
 * most often across the bar; it must look like one (a flat band over a dark
 * bottom row). Returns the glyph's left x (right end of the widest empty
 * stretch, `margin` pixels from its end) or -1. */
int rb_badge_menubar_gap(const uint32_t *px, int w, int h, int stride_px, int margin);

/* Draw the glyph scaled into `target` (frame pixels; nearest neighbour, any
 * size), only inside `clip`. box: draw the 20 x 20 Platinum box behind it
 * (target is then the box). */
void rb_badge_render(uint32_t *dst, int stride_px, rb_rect target, rb_rect clip, enum rb_badge_style style, bool box);
