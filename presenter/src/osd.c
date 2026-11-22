/* osd.c - key feedback indicator (osd.h) */
#include "osd.h"

#include <stdlib.h>
#include <string.h>

#include "rbui.h"

void rb_osd_free(struct rb_osd *o)
{
    free(o->img);
    memset(o, 0, sizeof *o);
}

int rb_osd_show(struct rb_osd *o, enum rb_osd_kind kind, double level, bool muted, uint64_t now_ms)
{
    int w, h;
    rbui_feedback_size(&w, &h);
    if (!o->img || o->w != w || o->h != h) {
        uint32_t *img = malloc((size_t)w * (size_t)h * sizeof *img);
        if (!img)
            return -1;
        free(o->img);
        o->img = img;
        o->w = w;
        o->h = h;
    }
    if (!(level >= 0.0))
        level = 0.0;
    if (level > 1.0)
        level = 1.0;
    int rk = kind == RB_OSD_BRIGHTNESS ? RBUI_FEEDBACK_BRIGHTNESS
           : kind == RB_OSD_MUTE ? RBUI_FEEDBACK_MUTE : RBUI_FEEDBACK_VOLUME;
    rbui_draw_feedback(o->img, w, rk, level, muted);
    o->kind = kind;
    o->level = level;
    o->muted = muted;
    o->shown_ms = now_ms;
    o->active = true;
    o->alpha = 255;
    return 0;
}

int rb_osd_alpha(const struct rb_osd *o, uint64_t now_ms)
{
    if (!o->active)
        return 0;
    uint64_t t = now_ms > o->shown_ms ? now_ms - o->shown_ms : 0;
    if (t <= RB_OSD_HOLD_MS)
        return 255;
    t -= RB_OSD_HOLD_MS;
    if (t >= RB_OSD_FADE_MS)
        return 0;
    return (int)(255 - (255 * t) / RB_OSD_FADE_MS);
}

rb_rect rb_osd_rect(const struct rb_osd *o, rb_rect area, int s, int W, int H)
{
    if (!o->active || !o->img || s < 1)
        return (rb_rect){0, 0, 0, 0};
    int w = o->w * s, h = o->h * s;
    int x = area.x + (area.w - w) / 2;
    int y = area.y + area.h - h - RB_OSD_MARGIN * s;
    /* inside the frame even when the area is smaller than the indicator */
    if (x + w > W)
        x = W - w;
    if (y + h > H)
        y = H - h;
    if (x < 0)
        x = 0;
    if (y < 0)
        y = 0;
    return (rb_rect){x, y, w, h};
}

void rb_osd_render(const struct rb_osd *o, uint32_t *dst, int stride_px, rb_rect target, rb_rect clip, int s,
                   int alpha)
{
    rb_rect r;
    if (!o->img || alpha <= 0 || s < 1 || !rb_rect_intersect(target, clip, &r))
        return;
    unsigned a = (unsigned)(alpha > 255 ? 255 : alpha);
    for (int y = r.y; y < r.y + r.h; y++) {
        int sy = (y - target.y) / s;
        if (sy < 0 || sy >= o->h)
            continue;
        const uint32_t *src = o->img + (size_t)sy * (size_t)o->w;
        uint32_t *d = dst + (size_t)y * (size_t)stride_px;
        for (int x = r.x; x < r.x + r.w; x++) {
            int sx = (x - target.x) / s;
            if (sx < 0 || sx >= o->w)
                continue;
            uint32_t c = src[sx] & 0xFFFFFFu;
            if (a == 255) {
                d[x] = c;
                continue;
            }
            uint32_t b = d[x], out = 0;
            for (int sh = 0; sh <= 16; sh += 8) {
                unsigned cs = (c >> sh) & 255u, bs = (b >> sh) & 255u;
                out |= ((cs * a + bs * (255u - a) + 127u) / 255u) << sh;
            }
            d[x] = out;
        }
    }
}
