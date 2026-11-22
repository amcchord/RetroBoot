/* The classic 50% checkerboard (ARCHITECTURE.md section 4.4).
 *
 * Pattern cells are s x s frame pixels counted from the anchor, the top-left
 * of Mac OS's picture (the guest target's integer layout: rb_startup_geometry
 * gx, gy), and cell (i, j) is black when i + j is even: QuickDraw's gray
 * pattern, black at Mac OS's pixel (0, 0). It is the dither Mac OS draws its
 * own startup screen with on a built-in LCD, so wherever the pattern cell is
 * the guest's pixel (s == k) the checkerboard continues as Mac OS's picture
 * at the cut-over. This is the single implementation used by the initramfs
 * splash, the presenter and --write-checkerboard (GRUB background). */
#pragma once
#include <stdint.h>

#define RB_CHECKER_WHITE 0x00ffffffu
#define RB_CHECKER_BLACK 0x00000000u

/* A frame pixel where a black cell begins. */
struct rb_checker_anchor {
    int x, y;
};

/* The anchor of rb_checker_pixel / rb_checker_fill / rb_checker_fill_units:
 * the presenter's (set from its startup geometry); the splash and GRUB's
 * background writer pass theirs explicitly. */
extern struct rb_checker_anchor rb_checker_anchor;

static inline int rb_floordiv(int a, int b) { return a >= 0 ? a / b : -((-a + b - 1) / b); }

static inline uint32_t rb_checker_pixel_at(int x, int y, int s, struct rb_checker_anchor a)
{
    if (s < 1)
        s = 1;
    return ((rb_floordiv(x - a.x, s) + rb_floordiv(y - a.y, s)) & 1) ? RB_CHECKER_WHITE : RB_CHECKER_BLACK;
}

static inline uint32_t rb_checker_pixel(int x, int y, int s) { return rb_checker_pixel_at(x, y, s, rb_checker_anchor); }

/* Fill frame rect (x, y, w, h) of an XRGB8888 buffer (cells of s pixels). */
void rb_checker_fill_at(uint32_t *px, int stride_px, int x, int y, int w, int h, int s, struct rb_checker_anchor a);
void rb_checker_fill(uint32_t *px, int stride_px, int x, int y, int w, int h, int s);

/* The same pattern in a buffer at 1/unit of the frame (the panel's and the
 * remote screen's logical pixels, unit = the pixel scale s): cells of `cells`
 * buffer pixels (the pattern multiple m), anchored at rb_checker_anchor / unit
 * (the anchor is on the s grid). */
void rb_checker_fill_units(uint32_t *px, int stride_px, int x, int y, int w, int h, int cells, int unit);

/* The pattern's phase in a buffer at 1/unit whose cells are one buffer pixel
 * (presenter/ui's rbui_set_checker_phase: black where (x + y + phase) is odd). */
int rb_checker_unit_phase(int unit);
