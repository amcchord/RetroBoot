/* Rotation and touch (ARCHITECTURE 4.5, 4.8): a touch lands exactly on the
 * Mac pixel shown under it at every rotation, pixel scale, mirror factor
 * and letterbox; the orientation-aware guest size rules; the gesture
 * recognizer; the output binding helpers. */
#include <math.h>
#include <stdint.h>
#include <string.h>

#include "rbtest.h"
#include "rotation.h"
#include "scale.h"
#include "touch.h"
#include "xform.h"

/* ---- mapping: what the scanout shows == where a touch lands ---------------- */

/* Every scanout pixel of an output showing a frame of unique pixel values
 * (index + 1): the touch mapping of a point inside that scanout pixel must
 * give the frame pixel whose value is there. Border pixels map to the
 * nearest image edge. */
static int check_xform_mapping(int rot, int fb_w, int fb_h, int src_w, int src_h, bool mirror, int src_scale)
{
    struct rb_xform x;
    memset(&x, 0, sizeof x);
    rb_xform_set_src_scale(&x, src_scale);
    if (rb_xform_setup(&x, rot, fb_w, fb_h, src_w, src_h, mirror) < 0)
        return 1;
    uint32_t *src = malloc((size_t)src_w * src_h * 4), *fb = calloc((size_t)fb_w * fb_h, 4);
    for (int i = 0; i < src_w * src_h; i++)
        src[i] = (uint32_t)i + 1;
    rb_xform_blit(&x, fb, fb_w, src, src_w, (rb_rect){0, 0, src_w, src_h});
    int bad = 0, inside = 0;
    for (int fy = 0; fy < fb_h; fy++)
        for (int fx = 0; fx < fb_w; fx++) {
            uint32_t want = fb[(size_t)fy * fb_w + fx];
            if (!want)
                continue; /* border */
            inside++;
            /* anywhere inside the scanout pixel, including its edges */
            static const double off[][2] = {{0, 0}, {0.5, 0.5}, {0.999, 0.001}, {0.001, 0.999}};
            for (size_t k = 0; k < sizeof off / sizeof off[0]; k++) {
                int sx, sy;
                rb_xform_fb_point_to_frame(&x, fx + off[k][0], fy + off[k][1], &sx, &sy);
                if ((uint32_t)(sy * src_w + sx) + 1 != want) {
                    if (bad < 3)
                        fprintf(stderr, "  rot %d fb %dx%d src %dx%d k %d: fb (%d,%d)+(%.3f,%.3f) -> (%d,%d), shows %u\n",
                                rot, fb_w, fb_h, src_w, src_h, x.k, fx, fy, off[k][0], off[k][1], sx, sy, want - 1);
                    bad++;
                }
            }
        }
    CHECK(inside > 0);
    free(src);
    free(fb);
    rb_xform_free(&x);
    return bad;
}

static void test_mapping_outputs(void)
{
    static const int rots[] = {0, 90, 180, 270};
    for (int r = 0; r < 4; r++) {
        int rot = rots[r];
        /* primary: 1:1, frame = logical size */
        int ow, oh;
        rb_rotated_size(rot, 72, 40, &ow, &oh);
        CHECK_EQ(check_xform_mapping(rot, 72, 40, ow, oh, false, 1), 0);
        /* mirrors: integer factors 1..3 with letterbox, a 1/2 reduction, a nearest fit */
        CHECK_EQ(check_xform_mapping(rot, 96, 60, 30, 20, true, 1), 0);   /* k = 3 (rot 0) */
        CHECK_EQ(check_xform_mapping(rot, 64, 50, 30, 20, true, 1), 0);   /* k = 2 */
        CHECK_EQ(check_xform_mapping(rot, 40, 35, 30, 20, true, 1), 0);   /* k = 1, letterbox */
        CHECK_EQ(check_xform_mapping(rot, 40, 30, 60, 40, true, 2), 0);   /* exact 1/2 */
        CHECK_EQ(check_xform_mapping(rot, 50, 31, 60, 40, true, 1), 0);   /* nearest fit */
        /* a portrait frame pillarboxed on a landscape mirror */
        CHECK_EQ(check_xform_mapping(rot, 80, 48, 18, 32, true, 1), 0);
    }
    /* a device that reports scanout pixels: i / n * n is a hair below i for
     * some i (libinput's normalisation), and must still be pixel i */
    {
        struct rb_xform id;
        memset(&id, 0, sizeof id);
        rb_xform_setup(&id, 0, 1280, 800, 1280, 800, false);
        int off = 0;
        for (int i = 0; i < 1280; i++) {
            int sx, sy;
            double n = (double)i / 1280.0, m = (double)(i % 800) / 800.0;
            rb_xform_fb_point_to_frame(&id, n * 1280, m * 800, &sx, &sy);
            off += sx != i || sy != i % 800;
        }
        CHECK_EQ(off, 0);
        rb_xform_free(&id);
    }
    /* outside the image: the nearest edge pixel */
    struct rb_xform x;
    memset(&x, 0, sizeof x);
    rb_xform_setup(&x, 0, 100, 50, 30, 20, true); /* k = 2, image at 20,5 */
    int sx, sy;
    rb_xform_fb_point_to_frame(&x, 0.5, 0.5, &sx, &sy);
    CHECK_EQ(sx, 0);
    CHECK_EQ(sy, 0);
    rb_xform_fb_point_to_frame(&x, 99.5, 49.5, &sx, &sy);
    CHECK_EQ(sx, 29);
    CHECK_EQ(sy, 19);
    rb_xform_free(&x);
}

/* The whole chain on a primary output: scanout pixel -> frame pixel (rotation)
 * -> Mac pixel (the guest's integer layout at the pixel scale, letterboxed),
 * against the picture actually composed there. */
static void test_mapping_guest(void)
{
    static const int rots[] = {0, 90, 180, 270};
    struct {
        int fb_w, fb_h; /* panel scanout */
        int pixel_scale;
        int gw, gh;     /* 0 = auto */
    } cases[] = {
        {120, 64, 1, 0, 0}, {120, 64, 2, 0, 0}, {120, 64, 3, 0, 0}, {120, 64, 2, 40, 24}, {64, 120, 1, 0, 0},
        {63, 121, 2, 0, 0},
    };
    for (size_t c = 0; c < sizeof cases / sizeof cases[0]; c++)
        for (int r = 0; r < 4; r++) {
            int rot = rots[r], W, H;
            rb_rotated_size(rot, cases[c].fb_w, cases[c].fb_h, &W, &H);
            int s = cases[c].pixel_scale;
            int gw = cases[c].gw ? cases[c].gw : (W / s / 8) * 8, gh = cases[c].gh ? cases[c].gh : H / s;
            if (gw < 8)
                gw = 8;
            rb_layout l;
            memset(&l, 0, sizeof l);
            CHECK_EQ(rb_layout_compute_ex(&l, W, H, gw, gh, RB_SCALING_INTEGER, s, s), 0);
            CHECK(l.mode == RB_SCALING_INTEGER);
            uint32_t *guest = malloc((size_t)gw * gh * 4), *frame = calloc((size_t)W * H, 4),
                     *fb = calloc((size_t)cases[c].fb_w * cases[c].fb_h, 4);
            for (int i = 0; i < gw * gh; i++)
                guest[i] = (uint32_t)i + 1;
            rb_layout_render(&l, frame, W, guest, gw, (rb_rect){0, 0, W, H});
            struct rb_xform x;
            memset(&x, 0, sizeof x);
            rb_xform_setup(&x, rot, cases[c].fb_w, cases[c].fb_h, W, H, false);
            rb_xform_blit(&x, fb, cases[c].fb_w, frame, W, (rb_rect){0, 0, W, H});
            int bad = 0;
            for (int fy = 0; fy < cases[c].fb_h; fy++)
                for (int fx = 0; fx < cases[c].fb_w; fx++) {
                    uint32_t want = fb[(size_t)fy * cases[c].fb_w + fx];
                    if (!want)
                        continue; /* letterbox */
                    /* libinput: a touch at fraction n of the range, n in [0, 1) */
                    double nx = (fx + 0.25) / cases[c].fb_w, ny = (fy + 0.75) / cases[c].fb_h;
                    int sx, sy, mx, my;
                    rb_xform_fb_point_to_frame(&x, nx * cases[c].fb_w, ny * cases[c].fb_h, &sx, &sy);
                    rb_layout_screen_to_src(&l, sx, sy, &mx, &my);
                    if ((uint32_t)(my * gw + mx) + 1 != want) {
                        if (bad < 3)
                            fprintf(stderr, "  rot %d s %d: fb (%d,%d) -> Mac (%d,%d), shows %u\n", rot, s, fx, fy, mx,
                                    my, want - 1);
                        bad++;
                    }
                }
            CHECK_EQ(bad, 0);
            free(guest);
            free(frame);
            free(fb);
            rb_xform_free(&x);
            rb_layout_free(&l);
        }
}

/* ---- rotation helpers and the orientation-aware guest rules -------------- */

static void test_rotation(void)
{
    CHECK_EQ(rb_rotation_norm(0), 0);
    CHECK_EQ(rb_rotation_norm(450), 90);
    CHECK_EQ(rb_rotation_norm(-90), 270);
    CHECK_EQ(rb_rotation_norm(45), -1);
    CHECK_EQ(rb_rotation_add(90, 270), 0);
    CHECK_EQ(rb_rotation_add(90, 90), 180);
    CHECK_EQ(rb_rotation_add(270, 180), 90);
    CHECK_EQ(rb_cmdline_rotation("quiet retroboot.rotation=90 x"), 90);
    CHECK_EQ(rb_cmdline_rotation("retroboot.rotation=270"), 270);
    CHECK_EQ(rb_cmdline_rotation("retroboot.rotation=90 retroboot.rotation=180"), 180);
    CHECK_EQ(rb_cmdline_rotation("retroboot.rotation=45"), 0);
    CHECK_EQ(rb_cmdline_rotation("retroboot.rotation=900"), 0);
    CHECK_EQ(rb_cmdline_rotation("retroboot.rotation="), 0);
    CHECK_EQ(rb_cmdline_rotation(""), 0);
    CHECK(rb_rotation_swaps(90) && rb_rotation_swaps(270) && !rb_rotation_swaps(180) && !rb_rotation_swaps(0));
}

static void test_portrait_rules(void)
{
    /* the automatic scale does not change with the orientation */
    CHECK_EQ(rb_suggested_scale(1080, 1920), rb_suggested_scale(1920, 1080));
    CHECK_EQ(rb_suggested_scale(720, 1280), 1);
    CHECK_EQ(rb_suggested_scale(1440, 2560), 3);
    CHECK_EQ(rb_suggested_scale(2160, 3840), 4);
    int w, h;
    rb_auto_guest_size(720, 1280, 1, &w, &h); /* GPD MicroPC turned to portrait */
    CHECK_EQ(w, 720);
    CHECK_EQ(h, 1280);
    rb_auto_guest_size(1080, 1920, 2, &w, &h);
    CHECK_EQ(w, 536);
    CHECK_EQ(h, 960);
    rb_auto_guest_size(768, 1024, 1, &w, &h);
    CHECK_EQ(w, 768);
    CHECK_EQ(h, 1024);
    rb_auto_guest_size(2160, 3840, 1, &w, &h);
    CHECK_EQ(w, 2160);
    CHECK_EQ(h, 3840);
    rb_auto_guest_size(480, 800, 1, &w, &h); /* clamped to the portrait minimum */
    CHECK_EQ(w, RB_GUEST_PMIN_W);
    CHECK_EQ(h, 800);
    /* pixel scales offered on a portrait display */
    CHECK(rb_pixel_scale_valid(1080, 1920, 1));
    CHECK(rb_pixel_scale_valid(1080, 1920, 2));  /* 536 x 960 */
    CHECK(!rb_pixel_scale_valid(1080, 1920, 3)); /* 360 wide */
    CHECK(rb_pixel_scale_valid(2160, 3840, 4));  /* 536 x 960 */
    CHECK(!rb_pixel_scale_valid(960, 1280, 2));  /* 480 wide: below the driver's 512 */
    CHECK(rb_pixel_scale_valid(1920, 1080, 2) == rb_pixel_scale_valid(1080, 1920, 2));
    CHECK_EQ(rb_effective_scale(1080, 1920, 0), 2);
    /* classic sizes turn on their side on a portrait display */
    rb_guest_size_for(768, 1366, 1, 1024, 768, &w, &h);
    CHECK_EQ(w, 768);
    CHECK_EQ(h, 1024);
    rb_guest_size_for(1080, 1920, 1, 800, 600, &w, &h);
    CHECK_EQ(w, 600);
    CHECK_EQ(h, 800);
    rb_guest_size_for(1080, 1920, 1, 1280, 1024, &w, &h);
    CHECK_EQ(w, 1024);
    CHECK_EQ(h, 1280);
    rb_guest_size_for(1080, 1920, 1, 640, 480, &w, &h); /* 480 wide is not a portrait size */
    CHECK_EQ(w, 640);
    CHECK_EQ(h, 480);
    rb_guest_size_for(1080, 1920, 1, 1152, 870, &w, &h); /* 870 is not a multiple of 8 */
    CHECK_EQ(w, 1152);
    CHECK_EQ(h, 870);
    rb_guest_size_for(1920, 1080, 1, 1024, 768, &w, &h); /* landscape: as configured */
    CHECK_EQ(w, 1024);
    CHECK_EQ(h, 768);
    rb_guest_size_for(1080, 1920, 2, 0, 0, &w, &h); /* auto */
    CHECK_EQ(w, 536);
    CHECK_EQ(h, 960);
}

/* ---- gestures ------------------------------------------------------------- */

struct rec {
    char log[64][32];
    int n;
    double x, y;
};

static void r_point(void *d, double x, double y)
{
    struct rec *r = d;
    r->x = x;
    r->y = y;
}
static void r_button(void *d, int b, bool down)
{
    struct rec *r = d;
    if (r->n < 64)
        snprintf(r->log[r->n++], 32, "%c%s@%.0f,%.0f", down ? '+' : '-', b == 0 ? "L" : b == 2 ? "R" : "M", r->x, r->y);
}
static void r_pan(void *d, double dx, double dy)
{
    struct rec *r = d;
    if (r->n < 64)
        snprintf(r->log[r->n++], 32, "pan%+.0f,%+.0f", dx, dy);
}

static bool logged(const struct rec *r, const char *const *want, int n)
{
    if (r->n != n) {
        fprintf(stderr, "  got %d actions:", r->n);
        for (int i = 0; i < r->n; i++)
            fprintf(stderr, " %s", r->log[i]);
        fprintf(stderr, "\n");
        return false;
    }
    for (int i = 0; i < n; i++)
        if (strcmp(r->log[i], want[i])) {
            fprintf(stderr, "  action %d: got %s, want %s\n", i, r->log[i], want[i]);
            return false;
        }
    return true;
}

static void gesture_new(struct rb_gesture *g, struct rec *r, bool enabled)
{
    memset(r, 0, sizeof *r);
    struct rb_gesture_cb cb = {r_point, r_button, r_pan};
    rb_gesture_init(g, &cb, r);
    rb_gesture_configure(g, enabled, 10);
}

static void test_gestures(void)
{
    struct rb_gesture g;
    struct rec r;
    uint64_t t = 1000;

    /* tap = click where it landed (small jitter does not move it) */
    gesture_new(&g, &r, true);
    rb_gesture_down(&g, 0, 100, 50, t);
    CHECK_EQ(r.x, 100);
    rb_gesture_motion(&g, 0, 104, 53, t + 40);
    rb_gesture_up(&g, 0, t + 90);
    CHECK(logged(&r, (const char *const[]){"+L@100,50", "-L@100,50"}, 2));
    CHECK_EQ(g.st, RB_G_IDLE);

    /* double-tap: the second tap lands on the first one's pixel */
    rb_gesture_down(&g, 1, 106, 46, t + 300);
    rb_gesture_up(&g, 1, t + 360);
    CHECK(logged(&r, (const char *const[]){"+L@100,50", "-L@100,50", "+L@100,50", "-L@100,50"}, 4));
    /* ... but not a later or a distant one */
    rb_gesture_down(&g, 0, 106, 46, t + 2000);
    rb_gesture_up(&g, 0, t + 2050);
    CHECK_EQ(r.x, 106);

    /* drag: pressed where the finger came down, then follows it */
    gesture_new(&g, &r, true);
    rb_gesture_down(&g, 0, 10, 10, t);
    rb_gesture_motion(&g, 0, 15, 10, t + 30); /* within slop: nothing */
    CHECK_EQ(r.n, 0);
    rb_gesture_motion(&g, 0, 40, 10, t + 60);
    CHECK_EQ(g.st, RB_G_DRAG);
    CHECK_EQ(r.x, 40);
    rb_gesture_motion(&g, 0, 80, 30, t + 90);
    rb_gesture_up(&g, 0, t + 120);
    CHECK(logged(&r, (const char *const[]){"+L@10,10", "-L@80,30"}, 2));
    /* no long press once dragging */
    CHECK_EQ(rb_gesture_tick(&g, t + 5000), -1);

    /* long press = secondary button down; sliding to a menu item and lifting chooses it */
    gesture_new(&g, &r, true);
    rb_gesture_down(&g, 0, 200, 100, t);
    int ms = rb_gesture_tick(&g, t + 100);
    CHECK_EQ(ms, RB_TOUCH_LONG_MS - 100);
    rb_gesture_motion(&g, 0, 203, 102, t + 300); /* within slop */
    CHECK_EQ(rb_gesture_tick(&g, t + RB_TOUCH_LONG_MS), -1);
    CHECK_EQ(g.st, RB_G_LONG);
    rb_gesture_motion(&g, 0, 230, 160, t + 900);
    rb_gesture_up(&g, 0, t + 1000);
    CHECK(logged(&r, (const char *const[]){"+R@200,100", "-R@230,160"}, 2));

    /* two-finger tap = secondary click where the first finger was */
    gesture_new(&g, &r, true);
    rb_gesture_down(&g, 0, 300, 300, t);
    rb_gesture_down(&g, 1, 340, 305, t + 40);
    rb_gesture_up(&g, 0, t + 150);
    rb_gesture_up(&g, 1, t + 170);
    CHECK(logged(&r, (const char *const[]){"+R@300,300", "-R@300,300"}, 2));
    /* ... not when held too long */
    gesture_new(&g, &r, true);
    rb_gesture_down(&g, 0, 300, 300, t);
    rb_gesture_down(&g, 1, 340, 305, t + 40);
    rb_gesture_up(&g, 0, t + 900);
    rb_gesture_up(&g, 1, t + 950);
    CHECK_EQ(r.n, 0);
    CHECK_EQ(rb_gesture_tick(&g, t + 2000), -1); /* and never a long press */

    /* two-finger drag = scrolling by the centroid's movement */
    gesture_new(&g, &r, true);
    rb_gesture_down(&g, 0, 100, 400, t);
    rb_gesture_down(&g, 1, 140, 400, t + 20);
    rb_gesture_motion(&g, 0, 100, 390, t + 40); /* centroid -5: within slop */
    CHECK_EQ(r.n, 0);
    rb_gesture_motion(&g, 1, 140, 380, t + 60); /* centroid -15 */
    CHECK_EQ(g.st, RB_G_SCROLL);
    rb_gesture_motion(&g, 0, 100, 350, t + 80); /* centroid -35 */
    rb_gesture_up(&g, 1, t + 100);
    rb_gesture_motion(&g, 0, 100, 300, t + 120); /* one finger left: ignored */
    rb_gesture_up(&g, 0, t + 140);
    CHECK(logged(&r, (const char *const[]){"pan+0,-15", "pan+0,-20"}, 2));
    CHECK_EQ(g.st, RB_G_IDLE);

    /* gestures off: a plain primary button */
    gesture_new(&g, &r, false);
    rb_gesture_down(&g, 0, 5, 6, t);
    rb_gesture_motion(&g, 0, 7, 6, t + 10);
    rb_gesture_down(&g, 1, 50, 50, t + 20); /* ignored */
    rb_gesture_up(&g, 1, t + 30);
    rb_gesture_up(&g, 0, t + 900);
    CHECK_EQ(rb_gesture_tick(&g, t + 2000), -1);
    CHECK(logged(&r, (const char *const[]){"+L@5,6", "-L@7,6"}, 2));

    /* cancel releases what is held */
    gesture_new(&g, &r, true);
    rb_gesture_down(&g, 0, 1, 1, t);
    rb_gesture_motion(&g, 0, 60, 1, t + 10);
    rb_gesture_cancel(&g);
    CHECK(logged(&r, (const char *const[]){"+L@1,1", "-L@60,1"}, 2));
    CHECK_EQ(g.ndown, 0);
    /* hostile slots are ignored */
    rb_gesture_down(&g, -1, 0, 0, t);
    rb_gesture_down(&g, RB_TOUCH_SLOTS, 0, 0, t);
    rb_gesture_up(&g, 3, t);
    CHECK_EQ(g.ndown, 0);

    /* wheel notches: content follows the fingers */
    CHECK(fabs(rb_touch_pan_v120(-RB_TOUCH_NOTCH_PX, 1) - 120) < 1e-9);    /* fingers up: scroll down */
    CHECK(fabs(rb_touch_pan_v120(2 * RB_TOUCH_NOTCH_PX, 2) + 120) < 1e-9); /* at 2x */
}

/* ---- output binding ------------------------------------------------------- */

static void test_binding(void)
{
    uint8_t e[128];
    memset(e, 0, sizeof e);
    static const uint8_t magic[8] = {0, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0};
    memcpy(e, magic, 8);
    uint8_t *d = e + 72; /* second descriptor */
    d[3] = 0xFC;
    memcpy(d + 5, "ELO Touch  \n ", 13);
    char name[24];
    CHECK_EQ(rb_edid_monitor_name(e, sizeof e, name, sizeof name), 9);
    CHECK(!strcmp(name, "ELO Touch"));
    e[1] = 0;
    CHECK_EQ(rb_edid_monitor_name(e, sizeof e, name, sizeof name), 0);
    CHECK_EQ(rb_edid_monitor_name(e, 64, name, sizeof name), 0);

    struct rb_touch_output o[3] = {{"eDP-1", "", true}, {"HDMI-A-1", "ELO Touch", false}, {"DP-1", "DELL U2415", false}};
    CHECK_EQ(rb_touch_pick_output(o, 3, NULL, "Goodix Capacitive TouchScreen", false), 0); /* built-in */
    CHECK_EQ(rb_touch_pick_output(o, 3, "DP-1", "Goodix", false), 2);                     /* udev */
    CHECK_EQ(rb_touch_pick_output(o, 3, "external", "x", false), 1);
    CHECK_EQ(rb_touch_pick_output(o, 3, "internal", "x", true), 0);
    CHECK_EQ(rb_touch_pick_output(o, 3, NULL, "ELO Touch Solutions ELO Touch USB", true), 1); /* EDID name */
    CHECK_EQ(rb_touch_pick_output(o, 3, NULL, "eGalax Inc. USB TouchController", true), 1);   /* USB: an external */
    CHECK_EQ(rb_touch_pick_output(o, 1, NULL, "eGalax", true), 0);                             /* only the panel */
    struct rb_touch_output desk[1] = {{"Virtual-1", "", false}};
    CHECK_EQ(rb_touch_pick_output(desk, 1, NULL, "x", false), 0); /* the primary */
    CHECK_EQ(rb_touch_pick_output(desk, 0, NULL, "x", false), -1);
}

/* Gemini PDA: the touch controller reports in the panel's native portrait frame
 * (1080x2160, inverted x/y applied by its driver, as the device tree says), the
 * same frame as the scanout, and the panel is mounted Left Side Up: content
 * rotation 270. Synthetic contacts (0..1 of the device range, as libinput gives
 * them) land on the landscape frame's pixel shown under them and on the Mac
 * pixel at 2x; tap, press-and-drag and long press at the Gemini's slop (3 mm,
 * 15.9 px/mm); the lab's touch_transform turns a mounted-upside-down controller. */
static void test_gemini_touch(void)
{
    struct rb_xform x;
    memset(&x, 0, sizeof x);
    CHECK_EQ(rb_xform_setup(&x, rb_rotation_from_orientation("Left Side Up"), 1080, 2160, 2160, 1080, false), 0);
    int fx, fy;
    /* native corners -> landscape frame */
    rb_xform_fb_point_to_frame(&x, 0.0 / 1080 * 1080, 2159.0 / 2160 * 2160, &fx, &fy);
    CHECK(fx == 0 && fy == 0); /* the portrait panel's bottom-left is the top-left */
    rb_xform_fb_point_to_frame(&x, 1079.0, 0.0, &fx, &fy);
    CHECK(fx == 2159 && fy == 1079);
    rb_xform_fb_point_to_frame(&x, 1079.0, 2159.0, &fx, &fy);
    CHECK(fx == 0 && fy == 1079);
    rb_xform_fb_point_to_frame(&x, 0.0, 0.0, &fx, &fy);
    CHECK(fx == 2159 && fy == 0);
    /* the pixel drawn there is the pixel touched, everywhere */
    int bad = 0;
    for (int py = 0; py < 2160; py += 37)
        for (int px = 0; px < 1080; px += 29) {
            int lx, ly, bx, by;
            rb_xform_fb_point_to_frame(&x, px + 0.5, py + 0.5, &fx, &fy);
            rb_xform_l2f(&x, fx, fy, &bx, &by);
            rb_xform_f2l(&x, px, py, &lx, &ly);
            if (bx != px || by != py || lx != fx || ly != fy)
                bad++;
        }
    CHECK_EQ(bad, 0);
    /* a finger moving toward the panel's native top moves right on the landscape screen */
    int last = -1, mono = 1;
    for (double ny = 0.9; ny >= 0.1; ny -= 0.05) {
        rb_xform_fb_point_to_frame(&x, 0.5 * 1080, ny * 2160, &fx, &fy);
        if (fx <= last || fy != 540)
            mono = 0;
        last = fx;
    }
    CHECK(mono);
    /* Mac pixel at 2x (1080x540 Mac screen fills the frame): native row 1079 is frame column 1080 */
    rb_xform_fb_point_to_frame(&x, 540.0, 1079.0, &fx, &fy);
    CHECK_EQ(fx / 2, 540);
    CHECK_EQ(fy / 2, 270);
    /* a controller mounted the other way round: invert-x,invert-y is a half turn */
    double nx = 1.0 / 1080, ny = 2158.0 / 2160;
    rb_touch_transform_apply(rb_touch_transform_parse("invert-x,invert-y"), &nx, &ny);
    rb_xform_fb_point_to_frame(&x, nx * 1080, ny * 2160, &fx, &fy);
    CHECK(fx >= 2157 && fy >= 1077);
    rb_xform_free(&x);

    /* gestures at the Gemini's 3 mm slop (47.6 frame pixels) */
    struct rb_gesture g;
    struct rec r;
    uint64_t t = 5000;
    double slop = 3.0 * 1080.0 / 68.0;
    gesture_new(&g, &r, true);
    rb_gesture_configure(&g, true, slop);
    rb_gesture_down(&g, 0, 600, 300, t);
    rb_gesture_motion(&g, 0, 630, 320, t + 50); /* a finger's wobble: still a tap */
    rb_gesture_up(&g, 0, t + 120);
    CHECK(logged(&r, (const char *const[]){"+L@600,300", "-L@600,300"}, 2));
    gesture_new(&g, &r, true);
    rb_gesture_configure(&g, true, slop);
    rb_gesture_down(&g, 0, 100, 100, t);
    rb_gesture_motion(&g, 0, 300, 120, t + 80); /* press and drag a window */
    rb_gesture_motion(&g, 0, 500, 140, t + 160);
    rb_gesture_up(&g, 0, t + 200);
    CHECK(logged(&r, (const char *const[]){"+L@100,100", "-L@500,140"}, 2));
    gesture_new(&g, &r, true);
    rb_gesture_configure(&g, true, slop);
    rb_gesture_down(&g, 0, 800, 400, t);
    rb_gesture_tick(&g, t + RB_TOUCH_LONG_MS + 1); /* touch and hold: secondary button */
    rb_gesture_up(&g, 0, t + RB_TOUCH_LONG_MS + 300);
    CHECK(logged(&r, (const char *const[]){"+R@800,400", "-R@800,400"}, 2));
}

int main(void)
{
    test_gemini_touch();
    test_mapping_outputs();
    test_mapping_guest();
    test_rotation();
    test_portrait_rules();
    test_gestures();
    test_binding();
    TEST_DONE();
}
