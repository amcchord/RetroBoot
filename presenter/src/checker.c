#include "checker.h"

#include <string.h>

struct rb_checker_anchor rb_checker_anchor;

void rb_checker_fill_at(uint32_t *px, int stride_px, int x, int y, int w, int h, int s, struct rb_checker_anchor a)
{
    if (w <= 0 || h <= 0)
        return;
    if (s < 1)
        s = 1;
    /* Each row is one of two phases; build each phase once and copy. */
    const uint32_t *phase_row[2] = {NULL, NULL};
    for (int row = y; row < y + h; row++) {
        uint32_t *dst = px + (size_t)row * stride_px + x;
        int phase = rb_floordiv(row - a.y, s) & 1;
        if (phase_row[phase]) {
            memcpy(dst, phase_row[phase], (size_t)w * 4);
            continue;
        }
        for (int col = 0; col < w; col++)
            dst[col] = rb_checker_pixel_at(x + col, row, s, a);
        phase_row[phase] = dst;
    }
}

void rb_checker_fill(uint32_t *px, int stride_px, int x, int y, int w, int h, int s)
{
    rb_checker_fill_at(px, stride_px, x, y, w, h, s, rb_checker_anchor);
}

static struct rb_checker_anchor unit_anchor(int unit)
{
    if (unit < 1)
        unit = 1;
    return (struct rb_checker_anchor){rb_floordiv(rb_checker_anchor.x, unit), rb_floordiv(rb_checker_anchor.y, unit)};
}

void rb_checker_fill_units(uint32_t *px, int stride_px, int x, int y, int w, int h, int cells, int unit)
{
    rb_checker_fill_at(px, stride_px, x, y, w, h, cells, unit_anchor(unit));
}

int rb_checker_unit_phase(int unit)
{
    struct rb_checker_anchor a = unit_anchor(unit);
    /* black at (a.x, a.y): (a.x + a.y + phase) odd */
    return (a.x + a.y + 1) & 1;
}
