/* Startup indicator (ARCHITECTURE 4.4.1): layout per guest size and scale,
 * bit-identity of GRUB's background / the splash / the presenter's partial
 * composition, the progress model (monotonic, capped, completes only at the
 * cut-over), the DPI-aware checkerboard rule, the panel quirk, the bundle art
 * derivation and the command line. */
#include <math.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "checker.h"
#include "png.h"
#include "rbtest.h"
#include "scale.h"
#include "startup.h"
#include "xform.h"

/* Where Mac OS 9.2.1 drew its Happy Mac on rb-dev (QMP screendumps of the
 * real guest, presenter/reports/startup-progress-*.md): ((w-32)/2, (h-32)/2). */
static void test_landing(void)
{
    static const int sizes[][2] = {{640, 480}, {800, 600}, {848, 480}, {960, 540}, {1000, 667},
                                   {1280, 800}, {1352, 761}, {1360, 768}, {3840, 2160}};
    static const int measured[][2] = {{304, 224}, {384, 284}, {408, 224}, {464, 254}, {484, 317},
                                      {624, 384}, {660, 364}, {664, 368}, {1904, 1064}};
    for (size_t i = 0; i < sizeof sizes / sizeof sizes[0]; i++) {
        struct rb_startup_geom g;
        /* the guest exactly fills the screen at 1x */
        rb_startup_geometry(&g, sizes[i][0], sizes[i][1], 1, 1, sizes[i][0], sizes[i][1]);
        CHECK(g.valid);
        CHECK_EQ(g.k, 1);
        CHECK_EQ(g.land_x, measured[i][0]);
        CHECK_EQ(g.land_y, measured[i][1]);
        CHECK_EQ(g.x, g.land_x);
        CHECK_EQ(g.q, 1);
    }
    struct rb_startup_geom g;
    /* auto sizes: 1920x1080 at 2x -> 960x540, the icon at (464, 254) x 2 */
    rb_startup_geometry(&g, 1920, 1080, 2, 0, 0, 0);
    CHECK_EQ(g.k, 2);
    CHECK_EQ(g.land_x, 928);
    CHECK_EQ(g.land_y, 508);
    /* 1366x768: guest 1360x768 centred, origin x = 3 */
    rb_startup_geometry(&g, 1366, 768, 1, 0, 0, 0);
    CHECK_EQ(g.land_x, 3 + 664);
    CHECK_EQ(g.land_y, 368);
    /* 4K at 4x: 960x540 x 4 */
    rb_startup_geometry(&g, 3840, 2160, 4, 0, 0, 0);
    CHECK_EQ(g.k, 4);
    CHECK_EQ(g.land_x, 464 * 4);
    CHECK_EQ(g.land_y, 254 * 4);
    /* 4K at 3x: 1280x720 x 3 */
    rb_startup_geometry(&g, 3840, 2160, 3, 3, 0, 0);
    CHECK_EQ(g.k, 3);
    CHECK_EQ(g.land_x, 624 * 3);
    CHECK_EQ(g.land_y, 344 * 3);
    /* 2560x1440 at 3x: 848x480 x 3, origin (painted grid) x = 0, y = 0 */
    rb_startup_geometry(&g, 2560, 1440, 3, 3, 0, 0);
    CHECK_EQ(g.k, 3);
    CHECK_EQ(g.land_x, (2560 - 848 * 3) / 2 / 3 * 3 + 408 * 3);
    CHECK_EQ(g.land_y, 224 * 3);
    /* fixed 640x480 with pixel size 1 on 1920x1080: 1x, centred */
    rb_startup_geometry(&g, 1920, 1080, 1, 1, 640, 480);
    CHECK_EQ(g.k, 1);
    CHECK_EQ(g.land_x, 640 + 304);
    CHECK_EQ(g.land_y, 300 + 224);
    /* fixed 640x480 at auto on 1920x1080 (s = 2): the largest fitting 2x */
    rb_startup_geometry(&g, 1920, 1080, 2, 0, 640, 480);
    CHECK_EQ(g.k, 2);
    CHECK_EQ(g.land_x, 320 + 304 * 2);
    CHECK_EQ(g.land_y, 60 + 224 * 2);
    /* fixed 1024x768 that does not fit at 2x: 1x, origin on the 2-grid */
    rb_startup_geometry(&g, 1920, 1080, 2, 2, 1024, 768);
    CHECK_EQ(g.k, 1);
    CHECK_EQ(g.land_x, 448 + 496);
    CHECK_EQ(g.land_y, 156 + 368);
    /* the same layout the guest gets */
    rb_layout l;
    memset(&l, 0, sizeof l);
    for (int t = 0; t < 400; t++) {
        int W = 640 + (int)(rbt_rand() % 3200), H = 480 + (int)(rbt_rand() % 1700);
        int req = (int)(rbt_rand() % 5), s = rb_effective_scale(W, H, req);
        int gw = 0, gh = 0;
        if (rbt_rand() % 2) {
            gw = 640 + (int)(rbt_rand() % 64) * 8;
            gh = 480 + (int)(rbt_rand() % 300);
        }
        rb_startup_geometry(&g, W, H, s, req, gw, gh);
        int tw, th; /* a classic size turns on its side on a portrait display */
        rb_guest_size_for(W, H, s, gw, gh, &tw, &th);
        CHECK_EQ(rb_layout_compute_ex(&l, W, H, tw, th, RB_SCALING_INTEGER, req ? s : 0, s), 0);
        if (l.mode == RB_SCALING_INTEGER) {
            CHECK_EQ(g.k, l.k);
            CHECK_EQ(g.land_x, l.dst.x + (tw - 32) / 2 * l.k);
            CHECK_EQ(g.land_y, l.dst.y + (th - 32) / 2 * l.k);
        }
        /* the indicator is the Happy Mac at the guest's own scale and place */
        CHECK_EQ(g.q, g.k);
        CHECK_EQ(g.x, g.land_x);
        CHECK_EQ(g.y, g.land_y);
        /* another scale keeps the landing's centre */
        struct rb_startup_geom big = rb_startup_geom_at(&g, 3 * g.k);
        CHECK_EQ(big.x + 16 * big.q, g.land_x + 16 * g.k);
        CHECK_EQ(big.y + 16 * big.q, g.land_y + 16 * g.k);
    }
    rb_layout_free(&l);
}

static int count_diff(const uint32_t *a, const uint32_t *b, size_t n)
{
    int d = 0;
    for (size_t i = 0; i < n; i++)
        d += a[i] != b[i];
    return d;
}

/* GRUB's background (rb-presenter --write-checkerboard), the splash's whole
 * frame and the presenter's composition by arbitrary dirty rectangles are the
 * same pixels; a bar update redraws only the bar. */
static void test_identity(void)
{
    static const int cases[][6] = {
        /* W, H, s, pixel_req, mm_w, mm_h */
        {1280, 800, 1, 0, 0, 0},     {1920, 1080, 2, 0, 531, 299}, {1280, 720, 1, 0, 133, 75},
        {1366, 768, 1, 1, 0, 0},     {2560, 1440, 3, 3, 0, 0},     {1352, 761, 1, 0, 0, 0},
        {3840, 2160, 2, 2, 597, 336},
    };
    for (size_t c = 0; c < sizeof cases / sizeof cases[0]; c++) {
        int W = cases[c][0], H = cases[c][1], s = cases[c][2], req = cases[c][3];
        int m = rb_checker_multiple(W, H, s, cases[c][4], cases[c][5]);
        size_t n = (size_t)W * H;
        uint32_t *grub = malloc(n * 4), *splash = malloc(n * 4), *pres = malloc(n * 4);
        struct rb_boot_background b;
        memset(&b, 0, sizeof b);
        b.w = W;
        b.h = H;
        b.s = s;
        b.pixel_req = req;
        b.mm_w = cases[c][4];
        b.mm_h = cases[c][5];
        b.indicator = true;
        CHECK_EQ(rb_boot_background_render(&b, grub), 0);
        CHECK_EQ(b.checker, s * m);
        /* splash: the startup scene, the indicator at 0 */
        struct rb_icon_art art;
        rb_icon_art_builtin(&art);
        struct rb_startup_geom g;
        rb_startup_geometry(&g, W, H, s, req, 0, 0);
        rb_startup_scene(splash, W, W, H, (rb_rect){0, 0, W, H}, s * m, &g, &art, true, 0, true);
        CHECK_EQ(count_diff(grub, splash, n), 0);
        CHECK_EQ(g.x, b.geom.x);
        CHECK_EQ(g.q, b.geom.q);
        CHECK_EQ(g.q, s); /* the Happy Mac's size: not the DPI-scaled pattern's */
        /* presenter: random dirty rectangles, each composed as compose_rect does */
        memset(pres, 0x55, n * 4);
        for (int i = 0; i < 300; i++) {
            int x = (int)(rbt_rand() % W), y = (int)(rbt_rand() % H);
            int w = 1 + (int)(rbt_rand() % 200), h = 1 + (int)(rbt_rand() % 200);
            if (x + w > W)
                w = W - x;
            if (y + h > H)
                h = H - y;
            rb_startup_scene(pres, W, W, H, (rb_rect){x, y, w, h}, s * m, &g, &art, true, 0, true);
        }
        /* plus the rows/columns the random rects missed */
        for (int y = 0; y < H; y += 64) {
            int h = y + 64 > H ? H - y : 64;
            rb_startup_scene(pres, W, W, H, (rb_rect){0, y, W, h}, s * m, &g, &art, true, 0, true);
        }
        CHECK_EQ(count_diff(grub, pres, n), 0);
        /* a bar step redraws only the bar's rectangle, a blink only the light's */
        for (int f = 1; f <= RB_BAR_STEPS; f++) {
            bool lit = f % 2;
            rb_startup_draw(pres, W, W, H, rb_startup_bar_rect(&g), &g, &art, f, !lit);
            rb_startup_draw(pres, W, W, H, rb_startup_light_rect(&g, &art), &g, &art, f, lit);
            rb_startup_draw(splash, W, W, H, (rb_rect){0, 0, W, H}, &g, &art, f, lit);
            CHECK_EQ(count_diff(splash, pres, n), 0);
        }
        /* the indicator is opaque only inside its silhouette: the rest of its
         * rectangle is the checkerboard */
        rb_rect r = rb_startup_rect(&g);
        CHECK(r.x >= 0 && r.y >= 0 && r.x + r.w <= W && r.y + r.h <= H);
        CHECK_EQ(splash[(size_t)r.y * W + r.x], rb_checker_pixel_at(r.x, r.y, s * m, rb_startup_anchor(&g)));
        free(grub);
        free(splash);
        free(pres);
    }
}

/* A sideways panel: GRUB's PNG is the scanout; the indicator laid out on
 * the logical frame lands, rotated, where the presenter's output transform
 * puts it (xform l2f: 90 -> fx = oh - 1 - ly, fy = lx). */
static void test_rotated(void)
{
    const int fb_w = 720, fb_h = 1280; /* GPD MicroPC scanout */
    for (int rot = 90; rot <= 270; rot += 90) {
        int ow = rot == 180 ? fb_w : fb_h, oh = rot == 180 ? fb_h : fb_w;
        uint32_t *logical = calloc((size_t)ow * oh, 4), *fb = calloc((size_t)fb_w * fb_h, 4);
        struct rb_icon_art art;
        rb_icon_art_builtin(&art);
        struct rb_startup_geom g;
        rb_startup_geometry(&g, ow, oh, 1, 0, 0, 0);
        CHECK_EQ(g.q, 1);
        rb_startup_draw(logical, ow, ow, oh, (rb_rect){0, 0, ow, oh}, &g, &art, 5, false);
        rb_startup_draw_rotated(fb, fb_w, fb_w, fb_h, rot, &g, &art, 5, false);
        int bad = 0;
        for (int ly = 0; ly < oh; ly++)
            for (int lx = 0; lx < ow; lx++) {
                int fx, fy;
                switch (rot) {
                case 90: fx = oh - 1 - ly; fy = lx; break;
                case 180: fx = ow - 1 - lx; fy = oh - 1 - ly; break;
                default: fx = ly; fy = ow - 1 - lx; break;
                }
                bad += logical[(size_t)ly * ow + lx] != fb[(size_t)fy * fb_w + fx];
            }
        CHECK_EQ(bad, 0);
        free(logical);
        free(fb);
    }
    /* rb_boot_background_render with a rotation: outside the indicator, the
     * logical pattern anchored at Mac OS's picture, rotated */
    struct rb_boot_background b;
    memset(&b, 0, sizeof b);
    b.w = fb_w;
    b.h = fb_h;
    b.rot = 90;
    b.mm_w = 75;
    b.mm_h = 133;
    b.indicator = true;
    uint32_t *buf = malloc((size_t)fb_w * fb_h * 4);
    CHECK_EQ(rb_boot_background_render(&b, buf), 0);
    CHECK_EQ(b.s, 1);
    CHECK_EQ(b.checker, 3);
    int bad = 0;
    for (int y = 0; y < fb_h; y += 7)
        for (int x = 0; x < fb_w; x += 5) {
            int lx = y, ly = fb_w - 1 - x; /* inverse of l2f for 90 */
            bool icon = lx >= b.geom.x && lx < b.geom.x + 32 && ly >= b.geom.y && ly < b.geom.y + 32;
            if (!icon)
                bad += buf[(size_t)y * fb_w + x] != rb_checker_pixel_at(lx, ly, 3, rb_startup_anchor(&b.geom));
        }
    CHECK_EQ(bad, 0);
    free(buf);
}

/* The GPD MicroPC (720 x 1280 scanout, Right Side Up, 245 DPI): the splash's
 * and presenter's logical frame -- the startup scene, the checkerboard at the
 * pattern scale anchored at Mac OS's picture plus the indicator -- rotated
 * into the scanout by the output transform is exactly GRUB's background from
 * rb-bootdisplay (more layouts: test_anchor). */
static void test_rotated_frame_is_grubs(void)
{
    const int fb_w = 720, fb_h = 1280;
    for (int rot = 0; rot <= 270; rot += 90) {
        bool swap = rot == 90 || rot == 270;
        int lw = swap ? fb_h : fb_w, lh = swap ? fb_w : fb_h;
        struct rb_boot_background b;
        memset(&b, 0, sizeof b);
        b.w = fb_w;
        b.h = fb_h;
        b.rot = rot;
        b.mm_w = 75;
        b.mm_h = 133;
        b.indicator = true;
        b.value = 5.0 / 13;
        uint32_t *grub = malloc((size_t)fb_w * fb_h * 4), *fb = calloc((size_t)fb_w * fb_h, 4);
        uint32_t *logical = malloc((size_t)lw * lh * 4);
        CHECK_EQ(rb_boot_background_render(&b, grub), 0);
        int s = rb_effective_scale(lw, lh, 0), m = rb_checker_multiple(lw, lh, s, 75, 133), cs = s * m;
        CHECK_EQ(cs, b.checker);
        struct rb_icon_art art;
        rb_icon_art_builtin(&art);
        struct rb_startup_geom g;
        rb_startup_geometry(&g, lw, lh, s, 0, 0, 0);
        rb_startup_scene(logical, lw, lw, lh, (rb_rect){0, 0, lw, lh}, cs, &g, &art, true, rb_startup_fill(b.value),
                         true);
        struct rb_xform x;
        memset(&x, 0, sizeof x);
        CHECK_EQ(rb_xform_setup(&x, rot, fb_w, fb_h, lw, lh, false), 0);
        rb_xform_blit(&x, fb, fb_w, logical, lw, (rb_rect){0, 0, lw, lh});
        rb_xform_free(&x);
        CHECK_EQ(count_diff(grub, fb, (size_t)fb_w * fb_h), 0);
        free(grub);
        free(fb);
        free(logical);
    }
}

static void test_progress(void)
{
    struct rb_progress pr;
    rb_progress_reset(&pr, 1000, 0);
    CHECK(!pr.timed);
    CHECK(rb_progress_at(&pr, 50000) == 0.0);
    CHECK_EQ(rb_progress_next_step(&pr, 1000, RB_BAR_STEPS, 5000), 6000);
    rb_progress_reset(&pr, 1000, 11000);
    CHECK(fabs(rb_progress_at(&pr, 1000)) < 1e-12);
    CHECK(fabs(rb_progress_at(&pr, 6000) - RB_PROGRESS_KNEE / 2) < 1e-9);
    CHECK(fabs(rb_progress_at(&pr, 11000) - RB_PROGRESS_KNEE) < 1e-9);
    CHECK(rb_progress_at(&pr, 20000) < RB_PROGRESS_CAP);
    CHECK(rb_progress_at(&pr, 3600000) < RB_PROGRESS_CAP + 1e-12);
    CHECK(rb_progress_at(&pr, 3600000) > RB_PROGRESS_CAP - 1e-6);
    /* continuous slope at the knee */
    double d1 = rb_progress_at(&pr, 11000) - rb_progress_at(&pr, 10990);
    double d2 = rb_progress_at(&pr, 11010) - rb_progress_at(&pr, 11000);
    CHECK(fabs(d1 - d2) < d1 * 0.01);
    /* the bar never fills on its own */
    CHECK(rb_startup_fill(rb_progress_at(&pr, 3600000)) < RB_BAR_STEPS);
    CHECK_EQ(rb_startup_fill(0), 0);
    CHECK_EQ(rb_startup_fill(1.0), RB_BAR_STEPS);
    CHECK_EQ(rb_startup_fill(12.0 / 13.0), 12);
    CHECK_EQ(rb_startup_fill(0.999), 12);
    /* random retargets (hostd milestones, early or late): never backwards */
    for (int run = 0; run < 200; run++) {
        uint64_t t = 1000 + rbt_rand() % 5000;
        rb_progress_reset(&pr, t, rbt_rand() % 3 ? t + 1000 + rbt_rand() % 40000 : 0);
        double last = rb_progress_at(&pr, t);
        for (int i = 0; i < 300; i++) {
            t += rbt_rand() % 700;
            if (rbt_rand() % 10 == 0) {
                uint64_t eta = rbt_rand() % 4 ? t + rbt_rand() % 30000 : t - (t > 500 ? rbt_rand() % 500 : 0);
                rb_progress_retarget(&pr, t, eta);
            }
            double v = rb_progress_at(&pr, t);
            CHECK(v + 1e-12 >= last);
            CHECK(v < RB_PROGRESS_CAP + 1e-12);
            last = v;
        }
    }
    /* next_step: exactly when the shown steps increase */
    for (int run = 0; run < 100; run++) {
        rb_progress_reset(&pr, 0, 2000 + rbt_rand() % 30000);
        uint64_t now = rbt_rand() % 20000;
        uint64_t nx = rb_progress_next_step(&pr, now, RB_BAR_STEPS, 5000);
        int cur = rb_startup_fill(rb_progress_at(&pr, now));
        if (nx < now + 5000) {
            CHECK(rb_startup_fill(rb_progress_at(&pr, nx)) > cur);
            CHECK_EQ(rb_startup_fill(rb_progress_at(&pr, nx - 1)), cur);
        } else {
            CHECK_EQ(rb_startup_fill(rb_progress_at(&pr, now + 5000)), cur);
        }
    }
}

static void test_dpi(void)
{
    /* GPD MicroPC: 6" 1280x720 (245 DPI) at 1x -> 3 (82 DPI) */
    CHECK_EQ(rb_checker_multiple(1280, 720, 1, 133, 75), 3);
    CHECK_EQ(rb_checker_multiple(1280, 720, 1, 75, 133), 3); /* portrait-native size */
    CHECK_EQ(rb_checker_multiple(720, 1280, 1, 75, 133), 3);
    /* QEMU's EDID (100 DPI) and an ordinary 24" monitor: unchanged */
    CHECK_EQ(rb_checker_multiple(1280, 800, 1, 325, 203), 1);
    CHECK_EQ(rb_checker_multiple(1920, 1080, 2, 531, 299), 1);
    /* 15.6" 1920x1080 laptop (141 DPI) at 2x: 70 DPI is the nearest */
    CHECK_EQ(rb_checker_multiple(1920, 1080, 2, 344, 194), 1);
    /* 13.3" 2560x1600 (227 DPI) with pixel size 1: 3 (76 DPI) */
    CHECK_EQ(rb_checker_multiple(2560, 1600, 1, 286, 179), 3);
    /* ... and at 2x: 113 DPI beats 57 */
    CHECK_EQ(rb_checker_multiple(2560, 1600, 2, 286, 179), 1);
    /* unknown, aspect-ratio-only and nonsense sizes: the guest grid */
    CHECK_EQ(rb_checker_multiple(1920, 1080, 2, 0, 0), 1);
    CHECK_EQ(rb_checker_multiple(1920, 1080, 2, 16, 9), 1);
    CHECK_EQ(rb_checker_multiple(1920, 1080, 1, 400, 100), 1);
}

static void write_file(const char *path, const char *text)
{
    FILE *f = fopen(path, "w");
    if (f) {
        fputs(text, f);
        fclose(f);
    }
}

static void test_quirk(void)
{
    char dir[] = "/tmp/rbtest-dmi-XXXXXX";
    CHECK(mkdtemp(dir) != NULL);
    char p1[64], p2[64];
    snprintf(p1, sizeof p1, "%s/sys_vendor", dir);
    snprintf(p2, sizeof p2, "%s/product_name", dir);
    write_file(p1, "GPD\n");
    write_file(p2, "MicroPC\n");
    unsetenv("RB_PANEL_MM");
    int w = 0, h = 0;
    CHECK(rb_panel_mm_quirk(dir, 1280, 720, &w, &h));
    CHECK_EQ(w, 133);
    CHECK_EQ(h, 75);
    CHECK(rb_panel_mm_quirk(dir, 720, 1280, &w, &h));
    CHECK_EQ(w, 75);
    CHECK_EQ(h, 133);
    CHECK(!rb_panel_mm_quirk(dir, 1920, 1080, &w, &h)); /* another panel in the same model */
    write_file(p2, "MicroPC 2\n");
    CHECK(!rb_panel_mm_quirk(dir, 1280, 720, &w, &h));
    write_file(p1, "GPD Technology\n");
    write_file(p2, "MicroPC\n");
    CHECK(!rb_panel_mm_quirk(dir, 1280, 720, &w, &h)); /* exact match only */
    setenv("RB_PANEL_MM", "300x200", 1);
    CHECK(rb_panel_mm_quirk(dir, 1, 1, &w, &h));
    CHECK_EQ(w, 300);
    CHECK_EQ(h, 200);
    unsetenv("RB_PANEL_MM");
    unlink(p1);
    unlink(p2);
    rmdir(dir);
    /* the connector's own size comes first; a quirk only for built-in panels */
    int mw, mh;
    CHECK(rb_display_mm(11 /* HDMI-A */, 1920, 1080, 531, 299, &mw, &mh) != NULL);
    CHECK_EQ(mw, 531);
    CHECK(rb_display_mm(11, 1920, 1080, 0, 0, &mw, &mh) == NULL);
    CHECK_EQ(mw, 0);
    /* an external display claiming 16 x 9 cm (TVs that store the aspect
     * ratio): no external display that small exists */
    CHECK(rb_display_mm(11, 1920, 1080, 160, 90, &mw, &mh) == NULL);
    CHECK(rb_display_mm(14 /* eDP */, 1280, 720, 133, 75, &mw, &mh) != NULL);
    CHECK_EQ(mw, 133);
    CHECK(rb_connector_internal(16 /* DSI */));
    CHECK(rb_connector_internal(14 /* eDP */));
    CHECK(!rb_connector_internal(11));
}

/* A synthetic stand-in for the bundle's Happy Mac (no Apple pixels here):
 * the compact-Mac silhouette, a uniform bevel and a "face" in the screen. */
static size_t make_rbi(uint8_t *out, int variant)
{
    struct rb_icon_art b;
    rb_icon_art_builtin(&b);
    memcpy(out, "RBI1", 4);
    out[4] = 32;
    out[5] = 0;
    out[6] = 32;
    out[7] = 0;
    for (int i = 0; i < 32 * 32; i++) {
        uint32_t c = b.px[i];
        int x = i % 32, y = i / 32;
        if (x >= RB_ICON_SCREEN_X && x < RB_ICON_SCREEN_X + RB_ICON_SCREEN_W && y >= RB_ICON_SCREEN_Y &&
            y < RB_ICON_SCREEN_Y + RB_ICON_SCREEN_H)
            c = 0xff2040f0u; /* screen */
        if ((x == 11 || x == 19) && (y == 7 || y == 8))
            c = 0xff000000u; /* eyes */
        if (y == 13 && x >= 12 && x <= 18)
            c = 0xff000000u; /* smile */
        uint8_t *p = out + 8 + (size_t)i * 4;
        p[0] = (uint8_t)(c >> 16);
        p[1] = (uint8_t)(c >> 8);
        p[2] = (uint8_t)c;
        p[3] = (c >> 24) ? 255 : 0;
        if (variant == 1 && i == 0)
            p[3] = 255; /* outside the silhouette */
        if (variant == 2 && (c >> 24))
            p[3] = 200; /* soft mask */
    }
    if (variant == 3) /* a broken bevel */
        out[8 + (size_t)(3 * 32 + 10) * 4] ^= 0x40;
    return 8 + 32 * 32 * 4;
}

static void test_art(void)
{
    static uint8_t buf[8 + 32 * 32 * 4];
    struct rb_icon_art a;
    size_t n = make_rbi(buf, 0);
    rb_icon_art_builtin(&a);
    CHECK_EQ(rb_icon_art_from_rbi(&a, buf, n), 0);
    CHECK_EQ(a.source, RB_ICON_MACOS);
    for (int y = RB_ICON_SCREEN_Y; y < RB_ICON_SCREEN_Y + RB_ICON_SCREEN_H; y++)
        for (int x = RB_ICON_SCREEN_X; x < RB_ICON_SCREEN_X + RB_ICON_SCREEN_W; x++)
            CHECK_EQ(a.px[y * 32 + x], 0xff2040f0u); /* the face is gone */
    struct rb_icon_art b;
    rb_icon_art_builtin(&b);
    for (int i = 0; i < 32 * 32; i++)
        CHECK_EQ((a.px[i] >> 24) != 0, (b.px[i] >> 24) != 0); /* the same silhouette */
    for (int v = 1; v <= 3; v++) {
        make_rbi(buf, v);
        rb_icon_art_builtin(&a);
        CHECK_EQ(rb_icon_art_from_rbi(&a, buf, n), -1);
        CHECK_EQ(a.source, RB_ICON_BUILTIN);
    }
    make_rbi(buf, 0);
    CHECK_EQ(rb_icon_art_from_rbi(&a, buf, n - 1), -1);
    buf[0] = 'X';
    CHECK_EQ(rb_icon_art_from_rbi(&a, buf, n), -1);
    CHECK_EQ(rb_icon_art_load(&a, "/nonexistent/overview-32.rbi"), RB_ICON_BUILTIN);
    /* the bar sits inside the screen with screen around it */
    uint32_t sprite[32 * 32];
    rb_icon_compose(sprite, &b, 0, true);
    CHECK_EQ(sprite[RB_BAR_Y * 32 + RB_BAR_X], 0xff000000u);
    CHECK(RB_BAR_X > RB_ICON_SCREEN_X && RB_BAR_X + RB_BAR_W < RB_ICON_SCREEN_X + RB_ICON_SCREEN_W);
    CHECK(RB_BAR_Y > RB_ICON_SCREEN_Y && RB_BAR_Y + RB_BAR_H < RB_ICON_SCREEN_Y + RB_ICON_SCREEN_H);
    uint32_t full[32 * 32];
    rb_icon_compose(full, &b, RB_BAR_STEPS, true);
    int changed = 0;
    for (int i = 0; i < 32 * 32; i++)
        changed += sprite[i] != full[i];
    CHECK_EQ(changed, (RB_BAR_W - 2) * (RB_BAR_H - 2));
}

/* The green light blinks (only its pixels), and the exact icon is captured
 * from a guest frame only when it is the compact Mac on the startup gray. */
static void test_light_and_capture(void)
{
    struct rb_icon_art a;
    rb_icon_art_builtin(&a);
    CHECK_EQ(a.nlight, 2);
    uint32_t on[32 * 32], off[32 * 32];
    rb_icon_compose(on, &a, 7, true);
    rb_icon_compose(off, &a, 7, false);
    int diff = 0;
    for (int i = 0; i < 32 * 32; i++)
        diff += on[i] != off[i];
    CHECK_EQ(diff, 2);
    CHECK(rb_startup_lit(1000, 1000));
    CHECK(rb_startup_lit(1000, 1499));
    CHECK(!rb_startup_lit(1000, 1500));
    CHECK(!rb_startup_lit(1000, 1999));
    CHECK(rb_startup_lit(1000, 2000));
    CHECK(rb_startup_lit(1000, 10)); /* before the start: on (GRUB's) */
    struct rb_startup_geom g;
    rb_startup_geometry(&g, 1280, 800, 1, 0, 0, 0);
    rb_rect lr = rb_startup_light_rect(&g, &a), sr = rb_startup_screen_rect(&g);
    CHECK_EQ(lr.x, 624 + 6);
    CHECK_EQ(lr.y, 384 + 22);
    CHECK_EQ(lr.w, 2);
    CHECK_EQ(lr.h, 1);
    CHECK_EQ(sr.x, 624 + 7);
    CHECK_EQ(sr.w, 17);
    CHECK_EQ(sr.h, 13);
    /* a guest frame: the stand-in icon on Mac OS's gray, at Mac OS's place */
    const int w = 640, h = 480;
    uint32_t *fr = malloc((size_t)w * h * 4);
    for (int i = 0; i < w * h; i++)
        fr[i] = 0x888888u;
    for (int y = 0; y < 32; y++)
        for (int x = 0; x < 32; x++)
            if (a.px[y * 32 + x] >> 24)
                fr[(224 + y) * w + 304 + x] = a.px[y * 32 + x] & 0xffffffu;
    static uint8_t rbi[RB_ICON_RBI_BYTES];
    CHECK_EQ(rb_icon_capture_rbi(fr, w, w, h, rbi), 0);
    struct rb_icon_art c;
    CHECK_EQ(rb_icon_art_from_rbi(&c, rbi, sizeof rbi), 0);
    CHECK_EQ(c.source, RB_ICON_MACOS);
    CHECK_EQ(c.nlight, 2);
    /* the handoff copy finds the same light (and its off colour) */
    struct rb_icon_art ho;
    rb_icon_art_builtin(&ho);
    rb_icon_art_set(&ho, c.px, c.source);
    CHECK_EQ(ho.nlight, c.nlight);
    CHECK_EQ(ho.light_off[0], c.light_off[0]);
    CHECK(!memcmp(ho.px, c.px, sizeof ho.px));
    for (int i = 0; i < 32 * 32; i++) {
        int x = i % 32, y = i / 32;
        bool screen = x >= 7 && x < 24 && y >= 4 && y < 17;
        if (!screen)
            CHECK_EQ(c.px[i], a.px[i]); /* everything but the face, exactly */
    }
    /* the cursor (or anything) next to it, a half-drawn icon, another place: refused */
    fr[(224 + 2) * w + 304 + 1] = 0;
    CHECK_EQ(rb_icon_capture_rbi(fr, w, w, h, rbi), -1);
    fr[(224 + 2) * w + 304 + 1] = 0x888888u;
    CHECK_EQ(rb_icon_capture_rbi(fr, w, w, h, rbi), 0);
    for (int y = 16; y < 32; y++)
        for (int x = 0; x < 32; x++)
            fr[(224 + y) * w + 304 + x] = 0x888888u;
    CHECK_EQ(rb_icon_capture_rbi(fr, w, w, h, rbi), -1);
    CHECK_EQ(rb_icon_capture_rbi(fr, w, w - 2, h, rbi), -1);
    /* a built-in LCD: the same icon on Mac OS's 1-pixel dither (black at the
     * origin), captured exactly; the plain gray's background is one colour */
    uint32_t bg[2];
    for (int i = 0; i < w * h; i++)
        fr[i] = 0x888888u;
    rb_startup_bg(fr, w, 304, 224, bg);
    CHECK_EQ(bg[0], 0x888888u);
    CHECK_EQ(bg[1], 0x888888u);
    for (int y = 0; y < h; y++)
        for (int x = 0; x < w; x++)
            fr[y * w + x] = ((x + y) & 1) ? 0xffffffu : 0;
    rb_startup_bg(fr, w, 304, 224, bg);
    CHECK_EQ(bg[0], 0u);
    CHECK_EQ(bg[1], 0xffffffu);
    CHECK_EQ(rb_startup_bg_at(bg, 1, 0), 0xffffffu);
    CHECK_EQ(rb_startup_bg_at(bg, 1, 1), 0u);
    CHECK_EQ(rb_icon_capture_rbi(fr, w, w, h, rbi), -1); /* the dither alone */
    for (int y = 0; y < 32; y++)
        for (int x = 0; x < 32; x++)
            if (a.px[y * 32 + x] >> 24)
                fr[(224 + y) * w + 304 + x] = a.px[y * 32 + x] & 0xffffffu;
    CHECK_EQ(rb_icon_capture_rbi(fr, w, w, h, rbi), 0);
    struct rb_icon_art d;
    CHECK_EQ(rb_icon_art_from_rbi(&d, rbi, sizeof rbi), 0);
    CHECK(!memcmp(d.px, c.px, sizeof d.px));
    fr[(224 + 2) * w + 304 + 1] = 0x00aa00u; /* something else beside it */
    CHECK_EQ(rb_icon_capture_rbi(fr, w, w, h, rbi), -1);
    free(fr);
}

static void test_cmdline(void)
{
    struct rb_boot_startup o;
    rb_cmdline_startup("quiet retroboot.pixel_scale=2 retroboot.progress=on retroboot.guest_size=800x600 "
                       "retroboot.checker_scale=3 retroboot.startup_ms=41000",
                       &o);
    CHECK_EQ(o.progress, 1);
    CHECK_EQ(o.guest_w, 800);
    CHECK_EQ(o.guest_h, 600);
    CHECK_EQ(o.checker_scale, 3);
    CHECK_EQ(o.startup_ms, 41000);
    rb_cmdline_startup("retroboot.progress=on retroboot.progress=off retroboot.guest_size=auto", &o);
    CHECK_EQ(o.progress, 0); /* the last one wins */
    CHECK_EQ(o.guest_w, 0);
    rb_cmdline_startup("retroboot.progressive=on retroboot.guest_size=10x10 retroboot.checker_scale=0 "
                       "retroboot.startup_ms=12x",
                       &o);
    CHECK_EQ(o.progress, -1);
    CHECK_EQ(o.guest_w, 0);
    CHECK_EQ(o.checker_scale, 0);
    CHECK_EQ(o.startup_ms, 0);
    rb_cmdline_startup("", &o);
    CHECK_EQ(o.progress, -1);
    CHECK_EQ(o.style, RB_STARTUP_CLASSIC);
    rb_cmdline_startup("retroboot.startup_style=macosx retroboot.progress=on", &o);
    CHECK_EQ(o.style, RB_STARTUP_OSX);
    rb_cmdline_startup("retroboot.startup_style=unknown", &o);
    CHECK_EQ(o.style, RB_STARTUP_CLASSIC);
}

static void test_osx_scene(void)
{
    uint32_t px[96 * 64];
    uint32_t classic[96 * 64];
    for (int scale = 1; scale <= 4; scale++) {
        rb_startup_panel_background(px, 96, 96, 64, 2, scale, RB_STARTUP_OSX);
        for (int i = 0; i < 96 * 64; i++)
            CHECK_EQ(px[i], RB_OSX_BOOT_GRAY);
        rb_startup_panel_background(px, 96, 96, 64, 2, scale, RB_STARTUP_CLASSIC);
        rb_checker_fill_units(classic, 96, 0, 0, 96, 64, 2, scale);
        CHECK_EQ(memcmp(px, classic, sizeof px), 0);
    }
    for (int rotation = 0; rotation < 360; rotation += 90) {
        struct rb_boot_background b = {.w = 96, .h = 64, .s = 1, .rot = rotation,
                                       .style = RB_STARTUP_OSX, .indicator = true, .value = 1.0};
        CHECK_EQ(rb_boot_background_render(&b, px), 0);
        CHECK(!b.indicator); /* no classic Mac/progress over an OS X boot */
        for (int i = 0; i < 96 * 64; i++)
            CHECK_EQ(px[i], RB_OSX_BOOT_GRAY);
    }
    for (int i = 0; i < 96 * 64; i++)
        px[i] = 0x123456;
    struct rb_startup_geom g = {0};
    rb_startup_scene_style(px, 96, 96, 64, (rb_rect){10, 12, 20, 23}, 1, &g, NULL,
                           true, RB_BAR_STEPS, true, RB_STARTUP_OSX);
    for (int y = 0; y < 64; y++)
        for (int x = 0; x < 96; x++)
            CHECK_EQ(px[y * 96 + x], x >= 10 && x < 30 && y >= 12 && y < 35 ? RB_OSX_BOOT_GRAY : 0x123456);
}

int main(void)
{
    test_landing();
    test_identity();
    test_rotated();
    test_rotated_frame_is_grubs();
    test_progress();
    test_dpi();
    test_quirk();
    test_art();
    test_light_and_capture();
    test_cmdline();
    test_osx_scene();
    TEST_DONE();
}
