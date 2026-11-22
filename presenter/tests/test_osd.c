/* Key feedback indicator (osd.c): the hold and fade, placement (centred near
 * the bottom of the Mac OS picture or the frame, integer-scaled, kept inside
 * the frame) and pixel-exact, clipped, blended drawing. */
#include <stdlib.h>
#include <string.h>

#include "osd.h"
#include "rbtest.h"
#include "rbui.h"

static void test_timing(void)
{
    struct rb_osd o = {0};
    CHECK_EQ(rb_osd_alpha(&o, 0), 0);
    CHECK_EQ(rb_osd_show(&o, RB_OSD_VOLUME, 0.5, false, 1000), 0);
    CHECK(o.active && o.img && o.w == 212 && o.h == 58);
    CHECK_EQ(rb_osd_alpha(&o, 1000), 255);
    CHECK_EQ(rb_osd_alpha(&o, 1000 + RB_OSD_HOLD_MS), 255);
    int mid = rb_osd_alpha(&o, 1000 + RB_OSD_HOLD_MS + RB_OSD_FADE_MS / 2);
    CHECK(mid > 100 && mid < 155);
    CHECK_EQ(rb_osd_alpha(&o, 1000 + RB_OSD_HOLD_MS + RB_OSD_FADE_MS), 0);
    /* a new key press restarts the hold */
    CHECK_EQ(rb_osd_show(&o, RB_OSD_VOLUME, 0.6, false, 2600), 0);
    CHECK_EQ(rb_osd_alpha(&o, 2700), 255);
    /* levels outside 0..1 are clamped */
    CHECK_EQ(rb_osd_show(&o, RB_OSD_BRIGHTNESS, 7.0, false, 0), 0);
    CHECK(o.level == 1.0);
    rb_osd_free(&o);
    CHECK(!o.active && !o.img);
}

static void test_placement(void)
{
    struct rb_osd o = {0};
    rb_rect r = rb_osd_rect(&o, (rb_rect){0, 0, 1024, 768}, 1, 1024, 768);
    CHECK(r.w == 0 && r.h == 0); /* inactive */
    rb_osd_show(&o, RB_OSD_VOLUME, 0.5, false, 0);
    /* 1024 x 768 Mac OS at 1x: where Mac OS 9 puts its own Volume window */
    r = rb_osd_rect(&o, (rb_rect){0, 0, 1024, 768}, 1, 1024, 768);
    CHECK_EQ(r.x, (1024 - 212) / 2);
    CHECK_EQ(r.y, 768 - 58 - RB_OSD_MARGIN);
    CHECK(r.w == 212 && r.h == 58);
    /* letterboxed Mac OS at 2x on a 2560 x 1600 frame */
    r = rb_osd_rect(&o, (rb_rect){256, 32, 2048, 1536}, 2, 2560, 1600);
    CHECK_EQ(r.x, 256 + (2048 - 424) / 2);
    CHECK_EQ(r.y, 32 + 1536 - 116 - 2 * RB_OSD_MARGIN);
    CHECK(r.w == 424 && r.h == 116);
    /* a picture smaller than the indicator still keeps it on screen */
    r = rb_osd_rect(&o, (rb_rect){0, 0, 100, 40}, 3, 800, 300);
    CHECK(r.x >= 0 && r.y >= 0 && r.x + r.w <= 800 && r.y + r.h <= 300);
    rb_osd_free(&o);
}

static void test_render(void)
{
    struct rb_osd o = {0};
    rb_osd_show(&o, RB_OSD_BRIGHTNESS, 0.75, false, 0);
    const int W = 600, H = 200, s = 2;
    uint32_t *f = malloc((size_t)W * H * 4);
    for (int i = 0; i < W * H; i++)
        f[i] = 0x336699;
    rb_rect t = rb_osd_rect(&o, (rb_rect){0, 0, W, H}, s, W, H);
    /* clipped: only the left half */
    rb_rect half = {t.x, t.y, t.w / 2, t.h};
    rb_osd_render(&o, f, W, t, half, s, 255);
    int exact = 1, outside_ok = 1;
    for (int y = t.y; y < t.y + t.h; y++)
        for (int x = t.x; x < t.x + t.w; x++) {
            uint32_t want = o.img[(size_t)((y - t.y) / s) * o.w + (x - t.x) / s] & 0xFFFFFFu;
            if (x < t.x + t.w / 2 && f[(size_t)y * W + x] != want)
                exact = 0;
            if (x >= t.x + t.w / 2 && f[(size_t)y * W + x] != 0x336699)
                outside_ok = 0;
        }
    CHECK(exact);      /* every logical pixel is an s x s block of its own colour */
    CHECK(outside_ok); /* nothing outside the clip */
    CHECK(f[0] == 0x336699 && f[(size_t)W * H - 1] == 0x336699);
    /* half transparent: between the indicator and the picture */
    for (int i = 0; i < W * H; i++)
        f[i] = 0x000000;
    rb_osd_render(&o, f, W, t, t, s, 128);
    uint32_t c = f[(size_t)(t.y + 10 * s) * W + t.x + 4 * s]; /* the face (0xDEDEDE) */
    CHECK((c & 0xFF) > 0x60 && (c & 0xFF) < 0x80);
    /* alpha 0 draws nothing */
    for (int i = 0; i < W * H; i++)
        f[i] = 0x123456;
    rb_osd_render(&o, f, W, t, t, s, 0);
    CHECK(f[(size_t)(t.y + 3) * W + t.x + 3] == 0x123456);
    free(f);
    rb_osd_free(&o);
}

int main(void)
{
    test_timing();
    test_placement();
    test_render();
    TEST_DONE();
}
