/* Display policy, scaling math, partial-update equivalence, checkerboard
 * identity (splash vs. panel) and damage regions. */
#include <stdlib.h>
#include <string.h>

#include "checker.h"
#include "rbtest.h"
#include "rbui.h"
#include "region.h"
#include "scale.h"

static void test_policy(void)
{
    CHECK_EQ(rb_suggested_scale(1024, 768), 1);
    CHECK_EQ(rb_suggested_scale(1280, 800), 1);
    CHECK_EQ(rb_suggested_scale(1366, 768), 1);
    CHECK_EQ(rb_suggested_scale(1920, 1080), 2);
    CHECK_EQ(rb_suggested_scale(1920, 1200), 2);
    CHECK_EQ(rb_suggested_scale(2560, 1600), 2);
    CHECK_EQ(rb_suggested_scale(3840, 2160), 4);
    CHECK_EQ(rb_suggested_scale(640, 480), 1);
    int w, h;
    rb_auto_guest_size(1280, 800, 1, &w, &h);
    CHECK_EQ(w, 1280);
    CHECK_EQ(h, 800);
    rb_auto_guest_size(1366, 768, 1, &w, &h);
    CHECK_EQ(w, 1360);
    CHECK_EQ(h, 768);
    rb_auto_guest_size(1920, 1080, 2, &w, &h);
    CHECK_EQ(w, 960);
    CHECK_EQ(h, 540);
    /* clamped to the emulator's -g range, like hostd's guest_geometry */
    rb_auto_guest_size(5120, 1440, rb_suggested_scale(5120, 1440), &w, &h);
    CHECK_EQ(w, 2560);
    CHECK_EQ(h, 720);
    rb_auto_guest_size(5120, 2880, 1, &w, &h);
    CHECK_EQ(w, 3840);
    CHECK_EQ(h, 2160);
    rb_auto_guest_size(7680, 1080, 1, &w, &h);
    CHECK_EQ(w, 3840);
    CHECK_EQ(h, 1080);
    rb_auto_guest_size(800, 400, 1, &w, &h);
    CHECK_EQ(w, 800);
    CHECK_EQ(h, 480);
    rb_auto_guest_size(600, 480, 1, &w, &h);
    CHECK_EQ(w, 640);
    CHECK_EQ(h, 480);
    rb_auto_guest_size(3840, 2160, 4, &w, &h);
    CHECK_EQ(w, 960);
    CHECK_EQ(h, 540);
}

static void test_layout_geometry(void)
{
    rb_layout l;
    memset(&l, 0, sizeof l);
    CHECK_EQ(rb_layout_compute(&l, 1280, 800, 640, 480, RB_SCALING_INTEGER), 0);
    CHECK_EQ(l.mode, RB_SCALING_INTEGER);
    CHECK_EQ(l.k, 1);
    CHECK_EQ(l.dst.x, 320);
    CHECK_EQ(l.dst.y, 160);
    CHECK_EQ(l.dst.w, 640);
    CHECK_EQ(l.dst.h, 480);
    CHECK_EQ(rb_layout_compute(&l, 1920, 1080, 640, 480, RB_SCALING_INTEGER), 0);
    CHECK_EQ(l.k, 2);
    CHECK_EQ(l.dst.x, 320);
    CHECK_EQ(l.dst.y, 60);
    CHECK_EQ(l.dst.w, 1280);
    CHECK_EQ(l.dst.h, 960);
    CHECK_EQ(rb_layout_compute(&l, 1920, 1080, 960, 540, RB_SCALING_INTEGER), 0);
    CHECK_EQ(l.k, 2);
    CHECK_EQ(l.dst.x, 0);
    CHECK_EQ(l.dst.y, 0);
    /* guest larger than the screen: integer falls back to fit */
    CHECK_EQ(rb_layout_compute(&l, 1280, 800, 1280, 1024, RB_SCALING_INTEGER), 0);
    CHECK_EQ(l.mode, RB_SCALING_FIT);
    CHECK_EQ(l.dst.h, 800);
    CHECK_EQ(l.dst.w, 1000);
    CHECK_EQ(l.dst.x, 140);
    CHECK_EQ(rb_layout_compute(&l, 1280, 800, 640, 480, RB_SCALING_FIT), 0);
    CHECK_EQ(l.mode, RB_SCALING_FIT);
    CHECK_EQ(l.dst.h, 800);
    CHECK_EQ(l.dst.w, 1067);
    CHECK_EQ(l.k, 1);
    CHECK_EQ(rb_layout_compute(&l, 1920, 1080, 640, 480, RB_SCALING_FIT), 0);
    CHECK_EQ(l.dst.h, 1080);
    CHECK_EQ(l.dst.w, 1440);
    CHECK_EQ(l.k, 2);
    /* pointer mapping */
    rb_layout_compute(&l, 1920, 1080, 640, 480, RB_SCALING_INTEGER);
    int gx, gy;
    rb_layout_screen_to_src(&l, 320, 60, &gx, &gy);
    CHECK_EQ(gx, 0);
    CHECK_EQ(gy, 0);
    rb_layout_screen_to_src(&l, 0, 0, &gx, &gy);
    CHECK_EQ(gx, 0);
    CHECK_EQ(gy, 0);
    rb_layout_screen_to_src(&l, 1919, 1079, &gx, &gy);
    CHECK_EQ(gx, 639);
    CHECK_EQ(gy, 479);
    rb_layout_screen_to_src(&l, 320 + 2 * 100 + 1, 60 + 2 * 50, &gx, &gy);
    CHECK_EQ(gx, 100);
    CHECK_EQ(gy, 50);
    rb_layout_free(&l);
}

static void fill_random(uint32_t *px, int n)
{
    for (int i = 0; i < n; i++)
        px[i] = rbt_rand() & 0xffffff;
}

/* Rendering only the mapped damage must equal a full re-render. */
static void check_partial(int sw, int sh, int src_w, int src_h, enum rb_scaling mode)
{
    rb_layout l;
    memset(&l, 0, sizeof l);
    CHECK_EQ(rb_layout_compute(&l, sw, sh, src_w, src_h, mode), 0);
    uint32_t *src = malloc((size_t)src_w * src_h * 4);
    uint32_t *a = calloc((size_t)sw * sh, 4), *b = calloc((size_t)sw * sh, 4);
    fill_random(src, src_w * src_h);
    rb_layout_render(&l, a, sw, src, src_w, (rb_rect){0, 0, sw, sh});
    for (int iter = 0; iter < 30; iter++) {
        rb_rect r = {(int)(rbt_rand() % src_w), (int)(rbt_rand() % src_h), 1 + (int)(rbt_rand() % 40),
                     1 + (int)(rbt_rand() % 40)};
        r = rb_rect_clip(r, src_w, src_h);
        for (int y = r.y; y < r.y + r.h; y++)
            for (int x = r.x; x < r.x + r.w; x++)
                src[y * src_w + x] = rbt_rand() & 0xffffff;
        rb_rect d = rb_layout_map_damage(&l, r);
        rb_layout_render(&l, a, sw, src, src_w, d);
    }
    rb_layout_render(&l, b, sw, src, src_w, (rb_rect){0, 0, sw, sh});
    CHECK(memcmp(a, b, (size_t)sw * sh * 4) == 0);
    /* Rendering never touches the letterbox. */
    bool outside_clean = true;
    for (int y = 0; y < sh; y++)
        for (int x = 0; x < sw; x++) {
            bool in = x >= l.dst.x && x < l.dst.x + l.dst.w && y >= l.dst.y && y < l.dst.y + l.dst.h;
            if (!in && b[y * sw + x] != 0)
                outside_clean = false;
        }
    CHECK(outside_clean);
    free(src);
    free(a);
    free(b);
    rb_layout_free(&l);
}

/* Stock XRGB blending preserves the fourth byte on a copy and clears it
 * on interpolation. Check it independently, including the clipped edges. */
static uint32_t reference_blend(uint32_t a, uint32_t b, unsigned weight)
{
    if (weight == 0)
        return a;
    uint32_t result = 0;
    for (int shift = 0; shift < 24; shift += 8) {
        unsigned left = (a >> shift) & 255, right = (b >> shift) & 255;
        result |= ((left * (256 - weight) + right * weight) / 256) << shift;
    }
    return result;
}

static void check_fit_pixels(int sw, int sh, int gw, int gh)
{
    rb_layout l = {0};
    CHECK_EQ(rb_layout_compute(&l, sw, sh, gw, gh, RB_SCALING_FIT), 0);
    int ss = gw + 3, ds = sw + 5;
    uint32_t *src = malloc((size_t)ss * gh * sizeof(*src));
    uint32_t *dst = malloc((size_t)ds * sh * sizeof(*dst));
    for (int i = 0; i < ss * gh; i++)
        src[i] = rbt_rand();
    for (int k = 0; k < 9; k++) {
        rb_rect area = k == 0 ? (rb_rect){0, 0, sw, sh} :
                       (rb_rect){l.dst.x + k - 1, l.dst.y + k % 3, l.dst.w - k, l.dst.h - k % 3};
        for (int i = 0; i < ds * sh; i++)
            dst[i] = 0xcafef00d;
        rb_layout_render(&l, dst, ds, src, ss, area);
        bool same = true;
        for (int y = 0; y < sh && same; y++)
            for (int x = 0; x < ds; x++) {
                uint32_t expected = 0xcafef00d;
                int rx = x - l.dst.x, ry = y - l.dst.y;
                if (rx >= 0 && ry >= 0 && rx < l.dst.w && ry < l.dst.h &&
                    x >= area.x && x < area.x + area.w && y >= area.y && y < area.y + area.h) {
                    const uint32_t *a = src + ss * l.y0[ry], *b = src + ss * l.y1[ry];
                    uint32_t top = reference_blend(a[l.x0[rx]], a[l.x1[rx]], l.xw[rx]);
                    uint32_t bottom = reference_blend(b[l.x0[rx]], b[l.x1[rx]], l.xw[rx]);
                    expected = reference_blend(top, bottom, l.yw[ry]);
                }
                if (dst[y * ds + x] != expected) {
                    same = false;
                    break;
                }
            }
        CHECK(same);
    }
    free(src);
    free(dst);
    rb_layout_free(&l);
}

static void test_render(void)
{
    check_partial(320, 200, 160, 100, RB_SCALING_INTEGER);
    check_partial(333, 211, 100, 70, RB_SCALING_INTEGER);
    check_partial(320, 200, 160, 100, RB_SCALING_FIT);
    check_partial(333, 211, 100, 70, RB_SCALING_FIT);
    check_partial(300, 200, 250, 190, RB_SCALING_FIT); /* mild upscale */
    check_partial(200, 150, 320, 240, RB_SCALING_FIT); /* downscale */
    check_partial(200, 150, 320, 240, RB_SCALING_INTEGER);

    /* integer: exact nearest neighbour */
    rb_layout l;
    memset(&l, 0, sizeof l);
    int sw = 100, sh = 80, gw = 30, gh = 20;
    rb_layout_compute(&l, sw, sh, gw, gh, RB_SCALING_INTEGER);
    CHECK_EQ(l.k, 3);
    uint32_t src[30 * 20], dst[100 * 80];
    fill_random(src, gw * gh);
    memset(dst, 0, sizeof dst);
    rb_layout_render(&l, dst, sw, src, gw, (rb_rect){0, 0, sw, sh});
    bool ok = true;
    for (int y = l.dst.y; y < l.dst.y + l.dst.h; y++)
        for (int x = l.dst.x; x < l.dst.x + l.dst.w; x++)
            if (dst[y * sw + x] != src[((y - l.dst.y) / 3) * gw + (x - l.dst.x) / 3])
                ok = false;
    CHECK(ok);
    /* fit at an exact integer ratio is pixel-sharp (no blending) */
    rb_layout_compute(&l, 60, 40, gw, gh, RB_SCALING_FIT);
    CHECK_EQ(l.k, 2);
    CHECK_EQ(l.dst.w, 60);
    uint32_t d2[60 * 40];
    rb_layout_render(&l, d2, 60, src, gw, (rb_rect){0, 0, 60, 40});
    ok = true;
    for (int y = 0; y < 40; y++)
        for (int x = 0; x < 60; x++)
            if (d2[y * 60 + x] != src[(y / 2) * gw + x / 2])
                ok = false;
    CHECK(ok);
    /* fit blends at non-integer ratios but keeps flat areas exact */
    uint32_t flat[30 * 20];
    for (int i = 0; i < 30 * 20; i++)
        flat[i] = 0x00336699;
    rb_layout_compute(&l, 97, 71, gw, gh, RB_SCALING_FIT);
    uint32_t d3[97 * 71];
    memset(d3, 0, sizeof d3);
    rb_layout_render(&l, d3, 97, flat, gw, (rb_rect){0, 0, 97, 71});
    ok = true;
    for (int y = l.dst.y; y < l.dst.y + l.dst.h; y++)
        for (int x = l.dst.x; x < l.dst.x + l.dst.w; x++)
            if (d3[y * 97 + x] != 0x00336699)
                ok = false;
    CHECK(ok);
    rb_layout_free(&l);
}

/* Integer scaling of random rectangles (odd offsets start on the right half of a
 * Mac pixel at 2x) equals the per-pixel definition: k = 1..4, the 2x row path
 * (NEON on arm64) included, with the Gemini PDA's 1080x540 Mac screen on its
 * 2160x1080 frame. */
static void test_integer_rects(void)
{
    static const int cases[][4] = {{2160, 1080, 1080, 540}, {100, 80, 50, 40}, {101, 81, 50, 40}, {91, 61, 30, 20},
                                   {120, 90, 30, 22}, {7, 5, 3, 2}};
    int bad = 0;
    for (size_t c = 0; c < sizeof cases / sizeof cases[0]; c++) {
        int sw = cases[c][0], sh = cases[c][1], gw = cases[c][2], gh = cases[c][3];
        rb_layout l;
        memset(&l, 0, sizeof l);
        rb_layout_compute(&l, sw, sh, gw, gh, RB_SCALING_INTEGER);
        uint32_t *src = malloc((size_t)gw * gh * 4), *dst = calloc((size_t)sw * sh, 4);
        fill_random(src, gw * gh);
        int iters = sw * sh > 100000 ? 4 : 60;
        for (int it = 0; it < iters; it++) {
            rb_rect r = it == 0 ? (rb_rect){0, 0, sw, sh}
                                : (rb_rect){(int)(rbt_rand() % (unsigned)sw), (int)(rbt_rand() % (unsigned)sh),
                                            1 + (int)(rbt_rand() % 37), 1 + (int)(rbt_rand() % 9)};
            memset(dst, 0, (size_t)sw * sh * 4);
            rb_layout_render(&l, dst, sw, src, gw, r);
            rb_rect in;
            if (!rb_rect_intersect(r, l.dst, &in))
                continue;
            for (int y = 0; y < sh; y++)
                for (int x = 0; x < sw; x++) {
                    bool inside = x >= in.x && x < in.x + in.w && y >= in.y && y < in.y + in.h;
                    uint32_t want = inside ? src[((y - l.dst.y) / l.k) * gw + (x - l.dst.x) / l.k] : 0;
                    if (dst[(size_t)y * sw + x] != want)
                        bad++;
                }
        }
        free(src);
        free(dst);
        rb_layout_free(&l);
    }
    CHECK_EQ(bad, 0);
}

/* display.pixel_scale (4.5): offered scales and their Mac screens. */
static void test_pixel_scale(void)
{
    struct {
        int w, h;
        int valid[RB_PIXEL_SCALE_MAX + 1]; /* index s: expected Mac width, 0 = not offered */
        int gh[RB_PIXEL_SCALE_MAX + 1];
    } cases[] = {
        {1920, 1080, {0, 1920, 960, 0, 0}, {0, 1080, 540, 0, 0}},
        {2560, 1440, {0, 2560, 1280, 848, 0}, {0, 1440, 720, 480, 0}},
        {3840, 2160, {0, 3840, 1920, 1280, 960}, {0, 2160, 1080, 720, 540}},
        {1280, 800, {0, 1280, 0, 0, 0}, {0, 800, 0, 0, 0}},
        {1366, 768, {0, 1360, 0, 0, 0}, {0, 768, 0, 0, 0}},
        {1024, 768, {0, 1024, 0, 0, 0}, {0, 768, 0, 0, 0}},
        {2560, 1600, {0, 2560, 1280, 848, 0}, {0, 1600, 800, 533, 0}},
        {2880, 1800, {0, 2880, 1440, 960, 0}, {0, 1800, 900, 600, 0}}, /* 4x: 720x450 */
        {3440, 1440, {0, 3440, 1720, 1144, 0}, {0, 1440, 720, 480, 0}},
        {5120, 2880, {0, 0, 2560, 1704, 1280}, {0, 0, 1440, 960, 720}},
        {640, 480, {0, 640, 0, 0, 0}, {0, 480, 0, 0, 0}},
        {800, 400, {0, 0, 0, 0, 0}, {0, 0, 0, 0, 0}},
    };
    for (size_t i = 0; i < sizeof cases / sizeof cases[0]; i++) {
        for (int s = 1; s <= RB_PIXEL_SCALE_MAX; s++) {
            int gw, gh;
            rb_scaled_guest_size(cases[i].w, cases[i].h, s, &gw, &gh);
            bool want = cases[i].valid[s] != 0;
            if (rb_pixel_scale_valid(cases[i].w, cases[i].h, s) != want)
                fprintf(stderr, "  %dx%d at %d: valid=%d\n", cases[i].w, cases[i].h, s, !want);
            CHECK(rb_pixel_scale_valid(cases[i].w, cases[i].h, s) == want);
            if (want) {
                CHECK_EQ(gw, cases[i].valid[s]);
                CHECK_EQ(gh, cases[i].gh[s]);
            }
        }
        CHECK(!rb_pixel_scale_valid(cases[i].w, cases[i].h, 0));
        CHECK(!rb_pixel_scale_valid(cases[i].w, cases[i].h, RB_PIXEL_SCALE_MAX + 1));
    }
    /* effective scale: auto = suggested; invalid requests fall back to the
     * nearest valid scale, preferring smaller */
    CHECK_EQ(rb_effective_scale(1920, 1080, 0), 2);
    CHECK_EQ(rb_effective_scale(1920, 1080, 1), 1);
    CHECK_EQ(rb_effective_scale(1920, 1080, 4), 2);
    CHECK_EQ(rb_effective_scale(1920, 1080, 3), 2);
    CHECK_EQ(rb_effective_scale(2560, 1440, 4), 3);
    CHECK_EQ(rb_effective_scale(1280, 800, 4), 1);
    CHECK_EQ(rb_effective_scale(3840, 2160, 0), 4);
    CHECK_EQ(rb_effective_scale(3840, 2160, 1), 1);
    CHECK_EQ(rb_effective_scale(5120, 2880, 1), 2); /* 1x would exceed the -g range */
    CHECK_EQ(rb_effective_scale(800, 400, 2), 1);   /* nothing valid: suggested */
    CHECK_EQ(rb_effective_scale(3840, 2160, 9), 4);
    /* kernel command line */
    CHECK_EQ(rb_cmdline_pixel_scale("quiet retroboot.pixel_scale=2 loglevel=0"), 2);
    CHECK_EQ(rb_cmdline_pixel_scale("retroboot.pixel_scale=auto"), 0);
    CHECK_EQ(rb_cmdline_pixel_scale("retroboot.pixel_scale=4\n"), 4);
    CHECK_EQ(rb_cmdline_pixel_scale("retroboot.pixel_scale=5"), 0);
    CHECK_EQ(rb_cmdline_pixel_scale("retroboot.pixel_scale=22"), 0);
    CHECK_EQ(rb_cmdline_pixel_scale("retroboot.pixel_scale="), 0);
    CHECK_EQ(rb_cmdline_pixel_scale("xretroboot.pixel_scale=2"), 0);
    CHECK_EQ(rb_cmdline_pixel_scale("retroboot.pixel_scale=3 retroboot.pixel_scale=1"), 1);
    CHECK_EQ(rb_cmdline_pixel_scale("retroboot.pixel_scale=3 retroboot.pixel_scale=x"), 0);
    CHECK_EQ(rb_cmdline_pixel_scale(""), 0);
    CHECK_EQ(rb_cmdline_pixel_scale(NULL), 0);
    CHECK_EQ(rb_boot_pixel_scale("/nonexistent/cmdline"), 0);
}

/* Render a random guest through the layout and check that every guest pixel
 * is exactly one k x k block at dst, the margins are black, and nothing
 * outside dst is touched: no seams, no off-by-one rows or columns. */
static bool blocks_exact(const rb_layout *l, int sw, int sh, const uint32_t *src, int gw)
{
    uint32_t *dst = malloc((size_t)sw * sh * 4);
    memset(dst, 0, (size_t)sw * sh * 4);
    rb_layout_render(l, dst, sw, src, gw, (rb_rect){0, 0, sw, sh});
    bool ok = true;
    for (int y = 0; y < sh && ok; y++)
        for (int x = 0; x < sw; x++) {
            bool in = x >= l->dst.x && x < l->dst.x + l->dst.w && y >= l->dst.y && y < l->dst.y + l->dst.h;
            uint32_t want = in ? src[((y - l->dst.y) / l->k) * gw + (x - l->dst.x) / l->k] : 0;
            if (dst[(size_t)y * sw + x] != want) {
                fprintf(stderr, "  %dx%d k=%d: pixel %d,%d is %06x, want %06x\n", sw, sh, l->k, x, y,
                        dst[(size_t)y * sw + x], want);
                ok = false;
                break;
            }
        }
    free(dst);
    return ok;
}

static void test_pixel_scale_layouts(void)
{
    /* Real modes and odd remainders (W, H not multiples of s, or of 8s). */
    const int modes[][2] = {{1920, 1080}, {2560, 1440}, {3840, 2160}, {1280, 800}, {1366, 768}, {2560, 1600},
                            {1921, 1081}, {2563, 1443}, {3843, 2162}, {2575, 1447}, {3000, 2000}, {1600, 1200}};
    for (size_t i = 0; i < sizeof modes / sizeof modes[0]; i++) {
        int W = modes[i][0], H = modes[i][1];
        for (int s = 1; s <= RB_PIXEL_SCALE_MAX; s++) {
            if (!rb_pixel_scale_valid(W, H, s))
                continue;
            int gw, gh;
            rb_auto_guest_size(W, H, s, &gw, &gh);
            int sw, sh;
            rb_scaled_guest_size(W, H, s, &sw, &sh);
            CHECK_EQ(gw, sw); /* valid scales are never clamped */
            CHECK_EQ(gh, sh);
            CHECK_EQ(gw % 8, 0);
            rb_layout l;
            memset(&l, 0, sizeof l);
            CHECK_EQ(rb_layout_compute_ex(&l, W, H, gw, gh, RB_SCALING_INTEGER, s, s), 0);
            CHECK_EQ(l.mode, RB_SCALING_INTEGER);
            CHECK_EQ(l.k, s);
            /* on the checkerboard/panel grid and centered within s-1 px */
            CHECK_EQ(l.dst.x % s, 0);
            CHECK_EQ(l.dst.y % s, 0);
            int mx = W - gw * s, my = H - gh * s;
            CHECK(my >= 0 && my < s);
            CHECK(mx >= 0 && mx < 8 * s);
            CHECK(l.dst.x <= mx / 2 && mx / 2 - l.dst.x < s);
            CHECK(l.dst.y <= my / 2 && my / 2 - l.dst.y < s);
            CHECK(l.dst.x + l.dst.w <= W && l.dst.y + l.dst.h <= H);
            /* auto pixel scale (max_k 0) gives the same picture for the auto size */
            rb_layout a;
            memset(&a, 0, sizeof a);
            rb_layout_compute_ex(&a, W, H, gw, gh, RB_SCALING_INTEGER, 0, s);
            CHECK_EQ(a.k, s);
            rb_layout_free(&a);
            /* pixel-exact blocks: full render on a scaled-down copy of the
             * geometry for the big modes keeps the test fast */
            if ((long)W * H <= 2560L * 1600) {
                uint32_t *src = malloc((size_t)gw * gh * 4);
                fill_random(src, gw * gh);
                CHECK(blocks_exact(&l, W, H, src, gw));
                free(src);
            }
            /* screen -> guest pointer mapping: block (gx, gy) covers exactly
             * [dst + g*s, dst + g*s + s) */
            int gx, gy;
            rb_layout_screen_to_src(&l, l.dst.x + 5 * s, l.dst.y + 7 * s + s - 1, &gx, &gy);
            CHECK_EQ(gx, 5);
            CHECK_EQ(gy, 7);
            rb_layout_screen_to_src(&l, l.dst.x + 5 * s - 1, l.dst.y + 7 * s, &gx, &gy);
            CHECK_EQ(gx, 4);
            CHECK_EQ(gy, 7);
            rb_layout_screen_to_src(&l, W - 1, H - 1, &gx, &gy);
            CHECK_EQ(gx, gw - 1);
            CHECK_EQ(gy, gh - 1);
            /* damage of one guest pixel is exactly its block */
            rb_rect d = rb_layout_map_damage(&l, (rb_rect){gw - 1, 3, 1, 1});
            CHECK_EQ(d.x, l.dst.x + (gw - 1) * s);
            CHECK_EQ(d.y, l.dst.y + 3 * s);
            CHECK_EQ(d.w, s);
            CHECK_EQ(d.h, s);
            /* the panel's logical copy of the guest lands on the same blocks */
            rb_layout lg;
            CHECK_EQ(rb_layout_logical(&l, s, &lg), 0);
            CHECK_EQ(lg.k, 1);
            CHECK_EQ(lg.dst.x * s, l.dst.x);
            CHECK_EQ(lg.dst.y * s, l.dst.y);
            rb_layout_free(&l);
        }
    }
    /* Exact blocks for the large modes at reduced size: same remainders. */
    {
        int W = 3843, H = 2163, s = 4, gw, gh;
        rb_auto_guest_size(W, H, s, &gw, &gh);
        rb_layout l;
        memset(&l, 0, sizeof l);
        rb_layout_compute_ex(&l, W, H, gw, gh, RB_SCALING_INTEGER, s, s);
        uint32_t *src = malloc((size_t)gw * gh * 4);
        fill_random(src, gw * gh);
        CHECK(blocks_exact(&l, W, H, src, gw));
        free(src);
        rb_layout_free(&l);
    }
    /* Fixed classic sizes: the pixel scale when it fits, else the largest
     * integer that fits. */
    rb_layout l;
    memset(&l, 0, sizeof l);
    rb_layout_compute_ex(&l, 3840, 2160, 640, 480, RB_SCALING_INTEGER, 2, 2);
    CHECK_EQ(l.k, 2);
    CHECK_EQ(l.dst.x, 1280);
    CHECK_EQ(l.dst.y, 600);
    rb_layout_compute_ex(&l, 3840, 2160, 640, 480, RB_SCALING_INTEGER, 0, 4);
    CHECK_EQ(l.k, 4);
    rb_layout_compute_ex(&l, 1920, 1080, 1280, 1024, RB_SCALING_INTEGER, 2, 2);
    CHECK_EQ(l.k, 1);
    CHECK_EQ(l.dst.x, 320);
    CHECK_EQ(l.dst.y, 28);
    rb_layout_compute_ex(&l, 2560, 1440, 800, 600, RB_SCALING_INTEGER, 3, 3);
    CHECK_EQ(l.k, 2);
    CHECK_EQ(l.dst.x % 3, 0);
    CHECK_EQ(l.dst.y % 3, 0);
    CHECK_EQ(l.dst.x, 480);
    CHECK_EQ(l.dst.y, 120);
    uint32_t *src = malloc(800 * 600 * 4);
    fill_random(src, 800 * 600);
    CHECK(blocks_exact(&l, 2560, 1440, src, 800));
    free(src);
    /* k not a multiple of s: no logical copy */
    rb_layout lg;
    CHECK_EQ(rb_layout_logical(&l, 3, &lg), -1);
    /* fit ignores max_k and the grid */
    rb_layout_compute_ex(&l, 1920, 1080, 640, 480, RB_SCALING_FIT, 1, 2);
    CHECK_EQ(l.mode, RB_SCALING_FIT);
    CHECK_EQ(l.dst.w, 1440);
    rb_layout_free(&l);
}

/* The panel's dimmed guest background (logical) upscaled by s equals the
 * guest view's pixels. */
static void test_logical_layout(void)
{
    int W = 2560, H = 1440, s = 3, gw, gh;
    rb_auto_guest_size(W, H, s, &gw, &gh);
    rb_layout l, lg;
    memset(&l, 0, sizeof l);
    rb_layout_compute_ex(&l, W, H, gw, gh, RB_SCALING_INTEGER, s, s);
    CHECK_EQ(rb_layout_logical(&l, s, &lg), 0);
    int lw = (W + s - 1) / s, lh = (H + s - 1) / s;
    CHECK_EQ(lg.screen_w, lw);
    uint32_t *src = malloc((size_t)gw * gh * 4);
    fill_random(src, gw * gh);
    uint32_t *phys = calloc((size_t)W * H, 4), *logical = calloc((size_t)lw * lh, 4),
             *up = calloc((size_t)W * H, 4);
    rb_layout_render(&l, phys, W, src, gw, (rb_rect){0, 0, W, H});
    rb_layout_render(&lg, logical, lw, src, gw, (rb_rect){0, 0, lw, lh});
    rb_upscale_rect(up, W, W, H, logical, lw, (rb_rect){0, 0, lw, lh}, s);
    CHECK(memcmp(phys, up, (size_t)W * H * 4) == 0);
    free(src);
    free(phys);
    free(logical);
    free(up);
    rb_layout_free(&l);
}

static void test_checker(void)
{
    /* Splash/presenter physical pattern == panel pattern (logical +
     * nearest-neighbour upscale), for every scale, odd sizes and anchors on
     * the s grid (Mac OS's picture, 4.4). */
    for (int s = 1; s <= 4; s++)
        for (int a = 0; a < 3; a++) {
            int W = 97 + s, H = 61 + 2 * s;
            int lw = (W + s - 1) / s, lh = (H + s - 1) / s;
            rb_checker_anchor = (struct rb_checker_anchor){a * 3 * s, a * s};
            rbui_set_checker_phase(rb_checker_unit_phase(s));
            uint32_t *phys = malloc((size_t)W * H * 4), *viap = malloc((size_t)W * H * 4);
            uint32_t *logical = malloc((size_t)lw * lh * 4);
            rb_checker_fill(phys, W, 0, 0, W, H, s);
            rbui_draw_checkerboard(logical, lw, 0, 0, lw, lh);
            rb_upscale_rect(viap, W, W, H, logical, lw, (rb_rect){0, 0, lw, lh}, s);
            CHECK(memcmp(phys, viap, (size_t)W * H * 4) == 0);
            bool formula = true;
            for (int y = 0; y < H; y++)
                for (int x = 0; x < W; x++) {
                    int i = rb_floordiv(x - rb_checker_anchor.x, s), j = rb_floordiv(y - rb_checker_anchor.y, s);
                    uint32_t want = ((i + j) % 2 == 0) ? 0u : 0xffffffu; /* black at the anchor */
                    if (phys[y * W + x] != want)
                        formula = false;
                }
            CHECK(formula);
            /* partial fills agree with the full fill */
            uint32_t *part = calloc((size_t)W * H, 4);
            rb_checker_fill(part, W, 3, 5, 40, 20, s);
            rb_checker_fill(part, W, 0, 0, W, H, s);
            CHECK(memcmp(phys, part, (size_t)W * H * 4) == 0);
            free(part);
            free(phys);
            free(viap);
            free(logical);
        }
    rb_checker_anchor = (struct rb_checker_anchor){0, 0};
    rbui_set_checker_phase(rb_checker_unit_phase(1));
    CHECK_EQ(rb_checker_pixel(0, 0, 1), RB_CHECKER_BLACK); /* QuickDraw's gray: black at the anchor */
    CHECK_EQ(rb_checker_pixel(1, 0, 1), RB_CHECKER_WHITE);
    CHECK_EQ(rb_checker_pixel(1, 1, 1), RB_CHECKER_BLACK);
    CHECK_EQ(rb_checker_pixel(1, 0, 2), RB_CHECKER_BLACK);
    CHECK_EQ(rb_checker_pixel(2, 0, 2), RB_CHECKER_WHITE);
    rb_checker_anchor = (struct rb_checker_anchor){3, 1};
    CHECK_EQ(rb_checker_pixel(3, 1, 2), RB_CHECKER_BLACK);
    CHECK_EQ(rb_checker_pixel(2, 1, 2), RB_CHECKER_WHITE); /* the cell left of the anchor */
    CHECK_EQ(rb_checker_pixel(0, 0, 2), RB_CHECKER_WHITE); /* cell (-2, -1) */
    rb_checker_anchor = (struct rb_checker_anchor){0, 0};
}

static void test_region(void)
{
    rb_region g;
    rb_region_clear(&g);
    static uint8_t want[200][200], have[200][200];
    memset(want, 0, sizeof want);
    for (int i = 0; i < 300; i++) {
        rb_rect r = {(int)(rbt_rand() % 190), (int)(rbt_rand() % 190), 1 + (int)(rbt_rand() % 10),
                     1 + (int)(rbt_rand() % 10)};
        rb_region_add(&g, r);
        for (int y = r.y; y < r.y + r.h && y < 200; y++)
            for (int x = r.x; x < r.x + r.w && x < 200; x++)
                want[y][x] = 1;
    }
    CHECK(g.n <= RB_REGION_MAX);
    memset(have, 0, sizeof have);
    for (int i = 0; i < g.n; i++)
        for (int y = g.r[i].y; y < g.r[i].y + g.r[i].h && y < 200; y++)
            for (int x = g.r[i].x; x < g.r[i].x + g.r[i].w && x < 200; x++)
                have[y][x] = 1;
    bool covered = true;
    for (int y = 0; y < 200; y++)
        for (int x = 0; x < 200; x++)
            if (want[y][x] && !have[y][x])
                covered = false;
    CHECK(covered);
    rb_region_clear(&g);
    rb_region_add(&g, (rb_rect){0, 0, 10, 10});
    rb_region_add(&g, (rb_rect){2, 2, 3, 3});
    CHECK_EQ(g.n, 1);
    rb_region_add(&g, (rb_rect){10, 0, 10, 10});
    CHECK_EQ(g.n, 1);
    CHECK_EQ(g.r[0].w, 20);
    rb_rect clip = rb_rect_clip((rb_rect){-5, -5, 20, 20}, 10, 10);
    CHECK_EQ(clip.x, 0);
    CHECK_EQ(clip.w, 10);
    CHECK(rb_rect_empty(rb_rect_clip((rb_rect){20, 20, 5, 5}, 10, 10)));
}

int main(void)
{
    test_policy();
    test_layout_geometry();
    test_render();
    check_fit_pixels(29, 19, 1, 2);
    check_fit_pixels(20, 13, 7, 5);
    check_fit_pixels(31, 23, 29, 19);
    check_fit_pixels(32, 20, 32, 20);
    check_fit_pixels(72, 44, 32, 20);
    check_fit_pixels(51, 39, 17, 13);
    check_fit_pixels(47, 29, 121, 81);
    check_fit_pixels(192, 108, 128, 80);
    test_integer_rects();
    test_pixel_scale();
    test_pixel_scale_layouts();
    test_logical_layout();
    test_checker();
    test_region();
    TEST_DONE();
}
