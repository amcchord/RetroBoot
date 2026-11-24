/* The checkerboard anchored at Mac OS's picture (ARCHITECTURE.md 4.4).
 *
 * For laptop and desktop layouts (unrotated and the GPD MicroPC's sideways
 * panel; pattern scale c and guest factor k equal or not):
 *
 *  (a) GRUB's background (rb-presenter --write-checkerboard), the splash's
 *      frame and the presenter's composition by arbitrary dirty rectangles,
 *      each through its output transform into the scanout, are the same
 *      pixels;
 *  (b) at the cut-over, the checkerboard inside Mac OS's picture is exactly
 *      Mac OS's own startup background on a built-in LCD (QuickDraw's gray
 *      pattern, black at its pixel (0, 0), drawn through the presenter's
 *      guest layout) wherever the pattern cell is the guest's pixel (c == k);
 *      where it is not, the pattern is coarser by design (c = s * m).
 *
 * The splash and the presenter are exercised through the code they run:
 * rb_startup_geometry with their inputs (the splash passes the auto guest
 * size as 0x0, the presenter its computed target), rb_startup_scene and the
 * output transform's blit (rb_output_flush); the presenter's anchor is set as
 * apply_anchor sets it. */
#include <stdlib.h>
#include <string.h>

#include "checker.h"
#include "png.h"
#include "rbtest.h"
#include "scale.h"
#include "startup.h"
#include "xform.h"

struct layout {
    const char *name;
    int fb_w, fb_h, rot; /* scanout and panel rotation */
    int pixel_req;       /* display.pixel_scale (0 = auto) */
    int mm_w, mm_h;      /* physical size (0 = unknown) */
    int guest_w, guest_h; /* display.guest_size (0 = auto) */
    int want_s, want_cs, want_k;
};

static const struct layout layouts[] = {
    {"1920x1080 at 1x (24-inch)", 1920, 1080, 0, 1, 531, 299, 0, 0, 1, 1, 1},
    {"1920x1080 at 2x (24-inch)", 1920, 1080, 0, 0, 531, 299, 0, 0, 2, 2, 2},
    {"1920x1080 at 2x (15.6-inch laptop)", 1920, 1080, 0, 2, 344, 194, 0, 0, 2, 2, 2},
    {"1920x1080 at 2x, 640x480 guest", 1920, 1080, 0, 2, 531, 299, 640, 480, 2, 2, 2},
    {"1920x1080 at 1x, 800x600 guest", 1920, 1080, 0, 1, 531, 299, 800, 600, 1, 1, 1},
    {"2160x1440 at 2x (N5105)", 2160, 1440, 0, 2, 296, 197, 0, 0, 2, 2, 2},
    {"2160x1440 at auto (3x)", 2160, 1440, 0, 0, 296, 197, 0, 0, 3, 3, 3},
    {"1366x768 (size unknown)", 1366, 768, 0, 0, 0, 0, 0, 0, 1, 1, 1},
    {"1366x768 (11.6-inch, 2-pixel pattern)", 1366, 768, 0, 0, 256, 144, 0, 0, 1, 2, 1},
    {"GPD MicroPC 720x1280 Right Side Up (3-pixel pattern)", 720, 1280, 90, 0, 75, 133, 0, 0, 1, 3, 1},
    {"720x1280 Right Side Up (size unknown)", 720, 1280, 90, 0, 0, 0, 0, 0, 1, 1, 1},
    {"720x1280 Left Side Up (size unknown)", 720, 1280, 270, 0, 0, 0, 0, 0, 1, 1, 1},
    {"1280x800 upside down", 1280, 800, 180, 0, 0, 0, 0, 0, 1, 1, 1},
};

static int count_diff(const uint32_t *a, const uint32_t *b, size_t n)
{
    int d = 0;
    for (size_t i = 0; i < n; i++)
        d += a[i] != b[i];
    return d;
}

/* Mac OS's startup background on a built-in LCD: a 1-pixel 50 % dither,
 * black at the guest's (0, 0) (measured on the lab guest, keys report 5). */
static void macos_dither(uint32_t *px, int gw, int gh)
{
    for (int y = 0; y < gh; y++)
        for (int x = 0; x < gw; x++)
            px[(size_t)y * gw + x] = ((x + y) & 1) ? 0x00ffffffu : 0;
}

static int matched_layouts;

static void check_layout(const struct layout *L)
{
    const int fb_w = L->fb_w, fb_h = L->fb_h;
    const size_t n = (size_t)fb_w * fb_h;
    bool swap = L->rot == 90 || L->rot == 270;
    int lw = swap ? fb_h : fb_w, lh = swap ? fb_w : fb_h;
    struct rb_icon_art art;
    rb_icon_art_builtin(&art);

    /* GRUB's background, as rb-bootdisplay asks for it. */
    struct rb_boot_background b;
    memset(&b, 0, sizeof b);
    b.w = fb_w;
    b.h = fb_h;
    b.rot = L->rot;
    b.pixel_req = L->pixel_req;
    b.mm_w = L->mm_w;
    b.mm_h = L->mm_h;
    b.guest_w = L->guest_w;
    b.guest_h = L->guest_h;
    b.indicator = true;
    uint32_t *grub = malloc(n * 4);
    CHECK_EQ(rb_boot_background_render(&b, grub), 0);
    const int s = b.s, cs = b.checker;
    CHECK_EQ(s, L->want_s);
    CHECK_EQ(cs, L->want_cs);
    if (s != L->want_s || cs != L->want_cs)
        fprintf(stderr, "%s: s %d cs %d\n", L->name, s, cs);

    struct rb_xform x;
    memset(&x, 0, sizeof x);
    CHECK_EQ(rb_xform_setup(&x, L->rot, fb_w, fb_h, lw, lh, false), 0);

    /* The splash: its geometry from the kernel command line (auto = 0x0),
     * the whole scene, one flush. */
    struct rb_startup_geom gs;
    rb_startup_geometry(&gs, lw, lh, s, L->pixel_req, L->guest_w, L->guest_h);
    uint32_t *sframe = malloc((size_t)lw * lh * 4), *splash = calloc(n, 4);
    rb_startup_scene(sframe, lw, lw, lh, (rb_rect){0, 0, lw, lh}, cs, &gs, &art, true, 0, true);
    rb_xform_blit(&x, splash, fb_w, sframe, lw, (rb_rect){0, 0, lw, lh});

    /* The presenter: its guest target (guest_target_size: the configured
     * size, else the auto size computed), the anchor as apply_anchor sets it,
     * dirty rectangles composed and flushed one by one. */
    int gw = L->guest_w, gh = L->guest_h;
    if (!gw || !gh)
        rb_auto_guest_size(lw, lh, s, &gw, &gh);
    struct rb_startup_geom gp;
    rb_startup_geometry(&gp, lw, lh, s, L->pixel_req, gw, gh);
    rb_checker_anchor = rb_startup_anchor(&gp);
    uint32_t *shadow = malloc((size_t)lw * lh * 4), *pres = malloc(n * 4);
    memset(shadow, 0x55, (size_t)lw * lh * 4);
    memset(pres, 0x33, n * 4);
    for (int i = 0; i < 200; i++) {
        int rx = (int)(rbt_rand() % (unsigned)lw), ry = (int)(rbt_rand() % (unsigned)lh);
        int rw = 1 + (int)(rbt_rand() % 300), rh = 1 + (int)(rbt_rand() % 300);
        rb_rect r = rb_rect_clip((rb_rect){rx, ry, rw, rh}, lw, lh);
        rb_startup_scene(shadow, lw, lw, lh, r, cs, &gp, &art, true, 0, true);
        rb_xform_blit(&x, pres, fb_w, shadow, lw, r);
    }
    for (int y = 0; y < lh; y += 97) {
        rb_rect r = rb_rect_clip((rb_rect){0, y, lw, 97}, lw, lh);
        rb_startup_scene(shadow, lw, lw, lh, r, cs, &gp, &art, true, 0, true);
        rb_xform_blit(&x, pres, fb_w, shadow, lw, r);
    }

    /* (a) the same pixels, and the same anchor and indicator. */
    CHECK_EQ(count_diff(grub, splash, n), 0);
    CHECK_EQ(count_diff(grub, pres, n), 0);
    CHECK_EQ(gs.gx, gp.gx);
    CHECK_EQ(gs.gy, gp.gy);
    CHECK_EQ(b.geom.gx, gp.gx);
    CHECK_EQ(b.geom.gy, gp.gy);
    CHECK_EQ(gs.x, gp.x);
    CHECK_EQ(gs.y, gp.y);
    /* the presenter's own fills (its global anchor) agree with the scene's */
    uint32_t *plain = malloc((size_t)lw * lh * 4), *plain2 = malloc((size_t)lw * lh * 4);
    rb_checker_fill(plain, lw, 0, 0, lw, lh, cs);
    rb_startup_scene(plain2, lw, lw, lh, (rb_rect){0, 0, lw, lh}, cs, &gp, &art, false, 0, true);
    CHECK_EQ(count_diff(plain, plain2, (size_t)lw * lh), 0);

    /* (b) the cut-over: Mac OS's picture through the presenter's layout. */
    rb_layout l;
    memset(&l, 0, sizeof l);
    CHECK_EQ(rb_layout_compute_ex(&l, lw, lh, gw, gh, RB_SCALING_INTEGER, L->pixel_req ? s : 0, s), 0);
    CHECK_EQ(l.mode, RB_SCALING_INTEGER);
    CHECK_EQ(l.k, L->want_k);
    CHECK_EQ(l.dst.x, gp.gx);
    CHECK_EQ(l.dst.y, gp.gy);
    uint32_t *guest = malloc((size_t)gw * gh * 4), *mframe = calloc((size_t)lw * lh, 4);
    macos_dither(guest, gw, gh);
    rb_layout_render(&l, mframe, lw, guest, gw, l.dst);
    uint32_t *mfb = calloc(n, 4), *cfb = calloc(n, 4);
    rb_xform_blit(&x, mfb, fb_w, mframe, lw, (rb_rect){0, 0, lw, lh});
    rb_xform_blit(&x, cfb, fb_w, plain2, lw, (rb_rect){0, 0, lw, lh});
    rb_rect area = rb_xform_lrect_to_fb(&x, l.dst);
    int diff = 0, total = 0;
    for (int y = area.y; y < area.y + area.h; y++)
        for (int xx = area.x; xx < area.x + area.w; xx++) {
            total++;
            diff += mfb[(size_t)y * fb_w + xx] != cfb[(size_t)y * fb_w + xx];
        }
    CHECK(total > 0);
    if (cs == l.k) {
        CHECK_EQ(diff, 0); /* the pattern is Mac OS's dither: nothing changes but the Happy Mac */
        matched_layouts++;
    } else {
        CHECK(diff > 0); /* a coarser pattern (c = s * m): the texture changes, as to the gray */
    }
    fprintf(stderr, "  %-52s s %d c %d k %d anchor %d,%d: %s\n", L->name, s, cs, l.k, gp.gx, gp.gy,
            cs == l.k ? (diff ? "MISMATCH" : "pattern = Mac OS's dither") : "pattern coarser than the dither");

    rb_layout_free(&l);
    rb_xform_free(&x);
    free(grub);
    free(sframe);
    free(splash);
    free(shadow);
    free(pres);
    free(plain);
    free(plain2);
    free(guest);
    free(mframe);
    free(mfb);
    free(cfb);
}

/* The panel's and the remote screen's logical buffers (1/s of the frame,
 * cells of m) and presenter/ui's 1-pixel pattern are the frame's pattern. */
static void test_units(void)
{
    static const int anchors[][2] = {{0, 0}, {3, 0}, {2, 6}, {320, 60}, {7, 5}};
    for (size_t a = 0; a < sizeof anchors / sizeof anchors[0]; a++)
        for (int s = 1; s <= 3; s++)
            for (int m = 1; m <= 3; m++) {
                rb_checker_anchor = (struct rb_checker_anchor){anchors[a][0] * s, anchors[a][1] * s};
                int W = 120 * s, H = 90 * s, lw = 120, lh = 90;
                uint32_t *frame = malloc((size_t)W * H * 4), *logical = malloc((size_t)lw * lh * 4),
                         *up = malloc((size_t)W * H * 4);
                rb_checker_fill(frame, W, 0, 0, W, H, s * m);
                rb_checker_fill_units(logical, lw, 0, 0, lw, lh, m, s);
                rb_upscale_rect(up, W, W, H, logical, lw, (rb_rect){0, 0, lw, lh}, s);
                CHECK_EQ(count_diff(frame, up, (size_t)W * H), 0);
                if (m == 1) {
                    /* presenter/ui's own pattern: black where (x + y + phase) is odd */
                    int ph = rb_checker_unit_phase(s);
                    int bad = 0;
                    for (int y = 0; y < lh; y++)
                        for (int xx = 0; xx < lw; xx++)
                            bad += logical[(size_t)y * lw + xx] != (((xx + y + ph) & 1) ? 0u : 0x00ffffffu);
                    CHECK_EQ(bad, 0);
                }
                /* black at the anchor */
                CHECK_EQ(rb_checker_pixel(rb_checker_anchor.x, rb_checker_anchor.y, s * m), RB_CHECKER_BLACK);
                free(frame);
                free(logical);
                free(up);
            }
    rb_checker_anchor = (struct rb_checker_anchor){0, 0};
}

int main(void)
{
    for (size_t i = 0; i < sizeof layouts / sizeof layouts[0]; i++)
        check_layout(&layouts[i]);
    CHECK_EQ(matched_layouts, 11);
    test_units();
    TEST_DONE();
}
