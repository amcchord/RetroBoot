/* Output transforms: panel rotation, mirror scaling/centering, borders,
 * damage mapping and the anchored checkerboard through rotation. */
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "checker.h"
#include "rbtest.h"
#include "xform.h"

static uint32_t *frame_ramp(int w, int h)
{
    uint32_t *f = malloc((size_t)w * h * 4);
    for (int y = 0; y < h; y++)
        for (int x = 0; x < w; x++)
            f[(size_t)y * w + x] = (uint32_t)(y * w + x + 1);
    return f;
}

static void test_orientation_names(void)
{
    CHECK_EQ(rb_rotation_from_orientation("Normal"), 0);
    CHECK_EQ(rb_rotation_from_orientation("Right Side Up"), 90);
    CHECK_EQ(rb_rotation_from_orientation("Left Side Up"), 270);
    CHECK_EQ(rb_rotation_from_orientation("Upside Down"), 180);
    CHECK_EQ(rb_rotation_from_orientation("bogus"), -1);
}

/* Every logical pixel lands where l2f says, for each rotation (GPD MicroPC:
 * 720x1280 scanout, 1280x720 logical, Right Side Up = 90). */
static void test_rotation_mapping(void)
{
    int rots[] = {0, 90, 180, 270};
    for (int ri = 0; ri < 4; ri++) {
        int rot = rots[ri];
        int fw = (rot == 90 || rot == 270) ? 72 : 128, fh = (rot == 90 || rot == 270) ? 128 : 72;
        struct rb_xform x;
        memset(&x, 0, sizeof x);
        CHECK_EQ(rb_xform_setup(&x, rot, fw, fh, 128, 72, false), 0);
        CHECK_EQ(x.ow, 128);
        CHECK_EQ(x.oh, 72);
        CHECK_EQ(x.identity, rot == 0);
        uint32_t *src = frame_ramp(128, 72);
        uint32_t *fb = calloc((size_t)fw * fh, 4);
        rb_xform_blit(&x, fb, fw, src, 128, (rb_rect){0, 0, 128, 72});
        int bad = 0;
        for (int ly = 0; ly < 72; ly++)
            for (int lx = 0; lx < 128; lx++) {
                int fx, fy, bx, by;
                rb_xform_l2f(&x, lx, ly, &fx, &fy);
                rb_xform_f2l(&x, fx, fy, &bx, &by);
                if (fx < 0 || fy < 0 || fx >= fw || fy >= fh || fb[(size_t)fy * fw + fx] != src[ly * 128 + lx] ||
                    bx != lx || by != ly)
                    bad++;
            }
        CHECK_EQ(bad, 0);
        /* Right Side Up: the logical top-left pixel is at the scanout's top-right. */
        if (rot == 90)
            CHECK_EQ(fb[fw - 1], src[0]);
        free(src);
        free(fb);
        rb_xform_free(&x);
    }
}

/* Partial updates equal a full update, and stay inside the mapped rect. */
static void test_partial_equivalence(void)
{
    int rots[] = {0, 90, 180, 270};
    for (int ri = 0; ri < 4; ri++) {
        int rot = rots[ri];
        bool swap = rot == 90 || rot == 270;
        for (int mirror = 0; mirror < 2; mirror++) {
            int fw = mirror ? (swap ? 300 : 500) : (swap ? 90 : 160), fh = mirror ? (swap ? 500 : 300) : (swap ? 160 : 90);
            struct rb_xform x;
            memset(&x, 0, sizeof x);
            CHECK_EQ(rb_xform_setup(&x, rot, fw, fh, 160, 90, mirror), 0);
            uint32_t *src = frame_ramp(160, 90);
            uint32_t *full = calloc((size_t)fw * fh, 4), *part = calloc((size_t)fw * fh, 4);
            rb_xform_blit(&x, full, fw, src, 160, (rb_rect){0, 0, 160, 90});
            int outside = 0;
            for (int i = 0; i < 40; i++) {
                rb_rect r = {(int)(rbt_rand() % 160), (int)(rbt_rand() % 90), 1 + (int)(rbt_rand() % 40),
                             1 + (int)(rbt_rand() % 30)};
                uint32_t *probe = calloc((size_t)fw * fh, 4);
                rb_xform_blit(&x, probe, fw, src, 160, r);
                rb_rect m = rb_xform_src_to_fb(&x, r);
                for (int y = 0; y < fh; y++)
                    for (int xx = 0; xx < fw; xx++)
                        if (probe[(size_t)y * fw + xx] &&
                            !(xx >= m.x && xx < m.x + m.w && y >= m.y && y < m.y + m.h))
                            outside++;
                free(probe);
            }
            CHECK_EQ(outside, 0);
            for (int y = 0; y < 90; y += 10)
                for (int xx = 0; xx < 160; xx += 16)
                    rb_xform_blit(&x, part, fw, src, 160, (rb_rect){xx, y, 16, 10});
            CHECK(memcmp(full, part, (size_t)fw * fh * 4) == 0);
            free(src);
            free(full);
            free(part);
            rb_xform_free(&x);
        }
    }
}

/* Mirrors: largest integer factor, centered; fit when 1x does not fit. */
static void test_mirror_geometry(void)
{
    struct rb_xform x;
    memset(&x, 0, sizeof x);
    CHECK_EQ(rb_xform_setup(&x, 0, 1920, 1080, 1280, 720, true), 0);
    CHECK_EQ(x.k, 1);
    CHECK_EQ(x.img.x, 320);
    CHECK_EQ(x.img.y, 180);
    CHECK_EQ(rb_xform_setup(&x, 0, 1920, 1080, 960, 540, true), 0);
    CHECK_EQ(x.k, 2);
    CHECK_EQ(x.img.x, 0);
    CHECK_EQ(x.img.w, 1920);
    CHECK(x.identity == false);
    CHECK_EQ(rb_xform_setup(&x, 0, 3840, 2160, 1280, 720, true), 0);
    CHECK_EQ(x.k, 3);
    CHECK_EQ(x.img.x, 0);
    CHECK_EQ(x.img.y, 0);
    /* external primary 1920x1080 frame shown on the rotated 720x1280 panel */
    CHECK_EQ(rb_xform_setup(&x, 90, 720, 1280, 1920, 1080, true), 0);
    CHECK_EQ(x.k, 0);
    CHECK_EQ(x.img.w, 1280);
    CHECK_EQ(x.img.h, 720);
    uint32_t *src = frame_ramp(1920, 1080);
    uint32_t *fb = calloc((size_t)720 * 1280, 4);
    rb_xform_blit(&x, fb, 720, src, 1920, (rb_rect){0, 0, 1920, 1080});
    int fx, fy;
    rb_xform_l2f(&x, 0, 0, &fx, &fy);
    CHECK_EQ(fb[(size_t)fy * 720 + fx], src[0]);
    rb_xform_l2f(&x, 1279, 719, &fx, &fy);
    CHECK_EQ(fb[(size_t)fy * 720 + fx], src[(size_t)1079 * 1920 + 1919]);
    free(src);
    free(fb);
    /* 960x540 frame (external primary at 2x) on the panel: 1x, centered */
    CHECK_EQ(rb_xform_setup(&x, 90, 720, 1280, 960, 540, true), 0);
    CHECK_EQ(x.k, 1);
    CHECK_EQ(x.img.x, 160);
    CHECK_EQ(x.img.y, 90);
    rb_xform_free(&x);
}

/* The pattern is anchored in the logical frame (at Mac OS's picture, 4.4):
 * rotated into the scanout, the anchor's cell lands where the transform puts
 * that logical pixel, black, for every rotation and anchor. */
static void test_checker_anchor_rotated(void)
{
    int rots[] = {0, 90, 180, 270};
    int anchors[][2] = {{0, 0}, {3, 0}, {6, 4}};
    for (int ri = 0; ri < 4; ri++)
        for (int ai = 0; ai < 3; ai++)
            for (int s = 1; s <= 3; s++) {
                int rot = rots[ri], fw = 720, fh = 1280;
                struct rb_xform x;
                memset(&x, 0, sizeof x);
                CHECK_EQ(rb_xform_setup(&x, rot, fw, fh, rot % 180 ? fh : fw, rot % 180 ? fw : fh, false), 0);
                struct rb_checker_anchor a = {anchors[ai][0] * s, anchors[ai][1] * s};
                uint32_t *frame = malloc((size_t)x.ow * x.oh * 4), *fb = malloc((size_t)fw * fh * 4);
                rb_checker_fill_at(frame, x.ow, 0, 0, x.ow, x.oh, s, a);
                rb_xform_blit(&x, fb, fw, frame, x.ow, (rb_rect){0, 0, x.ow, x.oh});
                int bad = 0;
                for (int ly = 0; ly < x.oh; ly += 7)
                    for (int lx = 0; lx < x.ow; lx += 5) {
                        int fx, fy;
                        rb_xform_l2f(&x, lx, ly, &fx, &fy);
                        bad += fb[(size_t)fy * fw + fx] != rb_checker_pixel_at(lx, ly, s, a);
                    }
                CHECK_EQ(bad, 0);
                int fx, fy;
                rb_xform_l2f(&x, a.x, a.y, &fx, &fy);
                CHECK_EQ(fb[(size_t)fy * fw + fx], RB_CHECKER_BLACK);
                free(frame);
                free(fb);
                rb_xform_free(&x);
            }
}

/* A mirror's checker border continues the frame's pattern seamlessly. */
static void test_checker_border(void)
{
    for (int k = 1; k <= 2; k++)
        for (int ai = 0; ai < 2; ai++) {
            struct rb_xform x;
            memset(&x, 0, sizeof x);
            int sw = k == 1 ? 1280 : 960, sh = k == 1 ? 720 : 540;
            struct rb_checker_anchor a = {ai * 3, ai * 5};
            CHECK_EQ(rb_xform_setup(&x, 0, 1920, 1200, sw, sh, true), 0);
            CHECK_EQ(x.k, k);
            rb_xform_set_border(&x, RB_BORDER_CHECKER, 1, a);
            uint32_t *frame = malloc((size_t)sw * sh * 4);
            rb_checker_fill_at(frame, sw, 0, 0, sw, sh, 1, a);
            uint32_t *fb = calloc((size_t)1920 * 1200, 4);
            rb_xform_fill_border(&x, fb, 1920);
            rb_xform_blit(&x, fb, 1920, frame, sw, (rb_rect){0, 0, sw, sh});
            int bad = 0;
            for (int y = 0; y < 1200; y++)
                for (int xx = 0; xx < 1920; xx++) {
                    int u = (xx - x.img.x) >= 0 ? (xx - x.img.x) / k : -((x.img.x - xx + k - 1) / k);
                    int v = (y - x.img.y) >= 0 ? (y - x.img.y) / k : -((x.img.y - y + k - 1) / k);
                    uint32_t want = rb_checker_pixel_at(u, v, 1, a);
                    bad += fb[(size_t)y * 1920 + xx] != want;
                }
            CHECK_EQ(bad, 0);
            rb_xform_set_border(&x, RB_BORDER_BLACK, 1, a);
            rb_xform_fill_border(&x, fb, 1920);
            CHECK_EQ(fb[0], 0);
            uint32_t inside = fb[(size_t)x.img.y * 1920 + x.img.x];
            rb_xform_set_border(&x, RB_BORDER_OSX_GRAY, 1, a);
            rb_xform_fill_border(&x, fb, 1920);
            CHECK_EQ(fb[0], 0x00c0c0c0u);
            CHECK_EQ(fb[(size_t)1199 * 1920 + 1919], 0x00c0c0c0u);
            CHECK_EQ(fb[(size_t)x.img.y * 1920 + x.img.x], inside);
            free(frame);
            free(fb);
            rb_xform_free(&x);
        }
    /* #15: 1/d and fit downscaled mirrors continue with checkerboard borders instead of black. */
    struct rb_xform x;
    memset(&x, 0, sizeof x);
    rb_xform_set_src_scale(&x, 2);
    CHECK_EQ(rb_xform_setup(&x, 0, 1920, 1200, 3840, 2160, true), 0);
    CHECK_EQ(x.k, 0);
    CHECK_EQ(x.div, 2);
    rb_xform_set_border(&x, RB_BORDER_CHECKER, 1, (struct rb_checker_anchor){0, 0});
    uint32_t *fb2 = calloc((size_t)1920 * 1200, 4);
    rb_xform_fill_border(&x, fb2, 1920);
    CHECK(fb2[0] != fb2[1]); /* 1x checkerboard top border row instead of solid black */
    CHECK(fb2[0] != fb2[1920]);
    free(fb2);
    rb_xform_free(&x);
}

/* Absolute input: scanout point -> frame point through rotation/letterbox. */
static void test_fb_to_frame(void)
{
    struct rb_xform x;
    memset(&x, 0, sizeof x);
    double u, v;
    /* KVM on a 1920x1080 mirror of a 1280x720 frame (1x at 320,180) */
    CHECK_EQ(rb_xform_setup(&x, 0, 1920, 1080, 1280, 720, true), 0);
    rb_xform_fb_to_frame(&x, 320 + 100, 180 + 50, &u, &v);
    CHECK_EQ((int)u, 100);
    CHECK_EQ((int)v, 50);
    rb_xform_fb_to_frame(&x, 5, 5, &u, &v); /* border -> clamped to the edge */
    CHECK_EQ((int)u, 0);
    CHECK_EQ((int)v, 0);
    /* 2x mirror */
    CHECK_EQ(rb_xform_setup(&x, 0, 1920, 1080, 960, 540, true), 0);
    rb_xform_fb_to_frame(&x, 1000, 600, &u, &v);
    CHECK_EQ((int)u, 500);
    CHECK_EQ((int)v, 300);
    /* touchscreen on the rotated panel: inverse of l2f */
    CHECK_EQ(rb_xform_setup(&x, 90, 720, 1280, 1280, 720, false), 0);
    int fx, fy;
    rb_xform_l2f(&x, 1000, 200, &fx, &fy);
    rb_xform_fb_to_frame(&x, fx, fy, &u, &v);
    CHECK_EQ((int)u, 1000);
    CHECK_EQ((int)v, 200);
    rb_xform_free(&x);
}

/* A 2x high-DPI panel's frame (N5105 laptop: 2160x1440 at scale 2) on a 1080p
 * mirror: an exact 1/2, every Mac pixel one mirror pixel, centered. Without the
 * frame scale (or when 1/2 does not fit) the arbitrary nearest fit stays. */
static void test_mirror_exact_reduction(void)
{
    struct rb_xform x;
    memset(&x, 0, sizeof x);
    rb_xform_set_src_scale(&x, 2);
    CHECK_EQ(rb_xform_setup(&x, 0, 1920, 1080, 2160, 1440, true), 0);
    CHECK_EQ(x.k, 0);
    CHECK_EQ(x.div, 2);
    CHECK_EQ(x.img.x, 420);
    CHECK_EQ(x.img.y, 180);
    CHECK_EQ(x.img.w, 1080);
    CHECK_EQ(x.img.h, 720);
    /* frame of 2x2 blocks, each block one value: the mirror shows each block once */
    uint32_t *src = malloc((size_t)2160 * 1440 * 4);
    for (int y = 0; y < 1440; y++)
        for (int xx = 0; xx < 2160; xx++)
            src[(size_t)y * 2160 + xx] = (uint32_t)((y / 2) * 1080 + xx / 2 + 1);
    uint32_t *fb = calloc((size_t)1920 * 1080, 4);
    rb_xform_blit(&x, fb, 1920, src, 2160, (rb_rect){0, 0, 2160, 1440});
    int bad = 0;
    for (int v = 0; v < 720; v++)
        for (int u = 0; u < 1080; u++)
            bad += fb[(size_t)(180 + v) * 1920 + 420 + u] != (uint32_t)(v * 1080 + u + 1);
    CHECK_EQ(bad, 0);
    /* partial updates still equal the full blit */
    uint32_t *part = calloc((size_t)1920 * 1080, 4);
    for (int y = 0; y < 1440; y += 90)
        for (int xx = 0; xx < 2160; xx += 135)
            rb_xform_blit(&x, part, 1920, src, 2160, (rb_rect){xx, y, 135, 90});
    CHECK(memcmp(fb, part, (size_t)1920 * 1080 * 4) == 0);
    /* the scale survives re-setup; a 1024x768 mirror cannot take 1/2 of it */
    CHECK_EQ(rb_xform_setup(&x, 0, 1024, 768, 2160, 1440, true), 0);
    CHECK_EQ(x.div, 0);
    CHECK_EQ(x.img.w, 1024);
    rb_xform_set_src_scale(&x, 1);
    CHECK_EQ(rb_xform_setup(&x, 0, 1920, 1080, 2160, 1440, true), 0);
    CHECK_EQ(x.div, 0);
    CHECK_EQ(x.img.w, 1620);
    /* scale 4 (4320x2880 frame): smallest divisor that fits is 4 on 1080p */
    rb_xform_set_src_scale(&x, 4);
    CHECK_EQ(rb_xform_setup(&x, 0, 1920, 1080, 4320, 2880, true), 0);
    CHECK_EQ(x.div, 4);
    CHECK_EQ(x.img.w, 1080);
    free(src);
    free(fb);
    free(part);
    rb_xform_free(&x);
}

/* The 1:1 fast path (tiled, NEON 4x4 transposes on arm64) draws exactly what the
 * generic per-pixel loop draws: every rotation, odd and tiny sizes, letterboxed
 * images (a mirror at 1x), random rectangles and stride padding. */
static void test_fast_path_identical(void)
{
    static const int rots[] = {0, 90, 180, 270};
    static const int sizes[][4] = {
        /* scanout w, h; frame w, h (frame <= logical: letterboxed at 1x when smaller) */
        {1080, 2160, 2160, 1080}, /* Gemini PDA: portrait panel, landscape frame */
        {37, 53, 53, 37}, {53, 37, 53, 37}, {5, 3, 3, 5}, {1, 1, 1, 1}, {4, 4, 4, 4},
        {64, 48, 40, 30}, {48, 64, 30, 40}, {66, 70, 61, 59},
    };
    int bad = 0, cases = 0;
    for (size_t si = 0; si < sizeof sizes / sizeof sizes[0]; si++) {
        for (int ri = 0; ri < 4; ri++) {
            int rot = rots[ri], fw = sizes[si][0], fh = sizes[si][1], sw = sizes[si][2], sh = sizes[si][3];
            bool swap = rot == 90 || rot == 270;
            int lw = swap ? fh : fw, lh = swap ? fw : fh;
            if (sw > lw || sh > lh)
                continue;
            struct rb_xform x;
            memset(&x, 0, sizeof x);
            bool mirror = sw != lw || sh != lh;
            CHECK_EQ(rb_xform_setup(&x, rot, fw, fh, sw, sh, mirror), 0);
            if (x.k != 1) {
                rb_xform_free(&x);
                continue;
            }
            int sstride = sw + 3, fstride = fw + 5; /* padded rows */
            uint32_t *src = malloc((size_t)sstride * sh * 4);
            for (int i = 0; i < sstride * sh; i++)
                src[i] = rbt_rand();
            uint32_t *a = calloc((size_t)fstride * fh, 4), *b = calloc((size_t)fstride * fh, 4);
            int nrect = fw > 200 ? 30 : 60;
            for (int k = 0; k <= nrect; k++) {
                rb_rect r = k == 0 ? (rb_rect){0, 0, sw, sh}
                                   : (rb_rect){(int)(rbt_rand() % (unsigned)sw), (int)(rbt_rand() % (unsigned)sh),
                                               1 + (int)(rbt_rand() % (unsigned)(sw < 40 ? sw : 40)),
                                               1 + (int)(rbt_rand() % (unsigned)(sh < 40 ? sh : 40))};
                rb_xform_force_generic(1);
                rb_xform_blit(&x, a, fstride, src, sstride, r);
                rb_xform_force_generic(0);
                rb_xform_blit(&x, b, fstride, src, sstride, r);
                cases++;
                if (memcmp(a, b, (size_t)fstride * fh * 4))
                    bad++;
            }
            free(src);
            free(a);
            free(b);
            rb_xform_free(&x);
        }
    }
    CHECK_EQ(bad, 0);
    CHECK(cases > 1000);
    rb_xform_force_generic(0);
}

static double now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1e3 + ts.tv_nsec / 1e6;
}

/* Not a check: the full-frame cost on this machine, generic loop against the fast
 * path, for the Gemini PDA's 2160x1080 frame into its 1080x2160 scanout. */
static void bench_gemini_full_frame(void)
{
    struct rb_xform x;
    memset(&x, 0, sizeof x);
    rb_xform_setup(&x, 270, 1080, 2160, 2160, 1080, false);
    uint32_t *src = malloc((size_t)2160 * 1080 * 4), *fb = malloc((size_t)1088 * 2160 * 4);
    for (int i = 0; i < 2160 * 1080; i++)
        src[i] = (uint32_t)i;
    double t[2];
    for (int g = 0; g < 2; g++) {
        rb_xform_force_generic(g == 0);
        rb_xform_blit(&x, fb, 1088, src, 2160, (rb_rect){0, 0, 2160, 1080}); /* warm */
        double t0 = now_ms();
        for (int i = 0; i < 5; i++)
            rb_xform_blit(&x, fb, 1088, src, 2160, (rb_rect){0, 0, 2160, 1080});
        t[g] = (now_ms() - t0) / 5;
    }
    rb_xform_force_generic(0);
    fprintf(stderr, "bench: 2160x1080 frame -> 1080x2160 scanout (270): generic %.2f ms, fast %.2f ms (%s)\n", t[0],
            t[1],
#if defined(__aarch64__) && defined(__ARM_NEON)
            "NEON"
#else
            "scalar tiles"
#endif
    );
    free(src);
    free(fb);
    rb_xform_free(&x);
}

int main(void)
{
    test_fast_path_identical();
    bench_gemini_full_frame();
    test_mirror_exact_reduction();
    test_fb_to_frame();
    test_orientation_names();
    test_rotation_mapping();
    test_partial_equivalence();
    test_mirror_geometry();
    test_checker_anchor_rotated();
    test_checker_border();
    TEST_DONE();
}
