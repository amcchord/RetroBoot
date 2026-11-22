/* The installer startup screen (installer_splash.h, ARCHITECTURE.md 4.4.2).
 *
 *  - geometry: Mac OS's own placement of its startup box, measured on the
 *    real guest at six sizes, and its layout on frames at every scale,
 *    rotation and guest size the startup indicator is tested with;
 *  - the private art file (synthetic, no Apple data): parse, reject, and
 *    the composition of status lines and the bar exactly as Mac OS composes
 *    its bar (the full bar left of the fill's end, the end, the empty bar);
 *  - the built-in MIT art: complete, opaque, the box's geometry;
 *  - progress: the boot-diag line format, goal and easing (never backwards,
 *    the welcome line first, the time estimate without a file);
 *  - the splash's frame == the presenter's composition by dirty rectangles,
 *    and the handoff (state line) continues the same pixels;
 *  - off outside installer mode and after "ready": the startup scene of
 *    4.4.1 is untouched.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "installer_splash.h"
#include "log.h"
#include "rbtest.h"
#include "scale.h"
#include "startup.h"

/* ---- geometry ---------------------------------------------------------------- */

static void test_box_origin(void)
{
    /* the real Mac OS 9.2.2 guest (presenter/reports/installer-splash-2026-10-16.md) */
    static const int m[][4] = {
        {640, 480, 109, 47},   {800, 600, 189, 83},    {1024, 768, 301, 134},
        {1360, 768, 469, 134}, {1920, 1080, 749, 227}, {1024, 778, 301, 137},
    };
    for (size_t i = 0; i < sizeof m / sizeof m[0]; i++) {
        int bx, by;
        rb_isplash_box_origin(m[i][0], m[i][1], &bx, &by);
        CHECK_EQ(bx, m[i][2]);
        CHECK_EQ(by, m[i][3]);
    }
    int bx, by;
    rb_isplash_box_origin(400, 300, &bx, &by); /* smaller than the box: at the corner */
    CHECK_EQ(bx, 0);
    CHECK_EQ(by, 0);
}

struct layout {
    int W, H, pixel_req, guest_w, guest_h;
};
static const struct layout layouts[] = {
    {1024, 768, 0, 0, 0},   {1366, 768, 0, 0, 0},  {1920, 1080, 0, 0, 0}, {1920, 1080, 1, 0, 0},
    {1920, 1080, 2, 640, 480}, {2560, 1600, 0, 0, 0}, {2160, 1440, 0, 0, 0}, {3840, 2160, 0, 0, 0},
    {1280, 800, 0, 0, 0},   {720, 1280, 0, 0, 0},  {1080, 1920, 0, 0, 0}, {1920, 1080, 1, 800, 600},
    {1920, 1080, 1, 1920, 1080}, {1024, 768, 0, 1280, 1024}, /* larger than the frame: fit */
};

static void test_geometry(void)
{
    for (size_t i = 0; i < sizeof layouts / sizeof layouts[0]; i++) {
        const struct layout *l = &layouts[i];
        int s = rb_effective_scale(l->W, l->H, l->pixel_req);
        struct rb_isplash_geom g;
        rb_isplash_geometry(&g, l->W, l->H, s, l->pixel_req, l->guest_w, l->guest_h);
        CHECK(g.valid);
        struct rb_startup_geom sg;
        rb_startup_geometry(&sg, l->W, l->H, s, l->pixel_req, l->guest_w, l->guest_h);
        int gw, gh;
        rb_guest_size_for(l->W, l->H, s, l->guest_w, l->guest_h, &gw, &gh);
        rb_rect r = rb_isplash_rect(&g);
        CHECK(r.x >= 0 && r.y >= 0 && r.x + r.w <= l->W && r.y + r.h <= l->H);
        if (gw * sg.k <= l->W && gh * sg.k <= l->H) {
            /* where Mac OS draws it on the guest's picture, at the guest's scale */
            int bx, by;
            rb_isplash_box_origin(gw, gh, &bx, &by);
            CHECK_EQ(g.k, sg.k);
            CHECK_EQ(g.x, sg.gx + bx * sg.k);
            CHECK_EQ(g.y, sg.gy + by * sg.k);
            CHECK_EQ(g.x % s, 0); /* on the pixel grid, like the picture */
            CHECK_EQ(g.y % s, 0);
        } else {
            CHECK_EQ(g.k, 1);
        }
        rb_rect b = rb_isplash_bar_rect(&g), st = rb_isplash_status_rect(&g);
        CHECK_EQ(b.x, g.x + RB_ISPLASH_BAR_X * g.k);
        CHECK_EQ(b.w, RB_ISPLASH_BAR_W * g.k);
        CHECK(st.y + st.h <= b.y); /* the status line is above the bar */
    }
    /* the 1920 x 1080 auto layout: 960 x 540 at 2x */
    struct rb_isplash_geom g;
    rb_isplash_geometry(&g, 1920, 1080, 2, 0, 0, 0);
    CHECK_EQ(g.k, 2);
    CHECK_EQ(g.bx, 269);
    CHECK_EQ(g.by, 65);
    CHECK_EQ(g.x, 538);
    CHECK_EQ(g.y, 130);
}

static void test_fill(void)
{
    CHECK_EQ(rb_isplash_fill(0), RB_ISPLASH_BAR_MIN);
    CHECK_EQ(rb_isplash_fill(-1), RB_ISPLASH_BAR_MIN);
    CHECK_EQ(rb_isplash_fill(1.0), RB_ISPLASH_BAR_STEPS);
    CHECK_EQ(rb_isplash_fill(2.0), RB_ISPLASH_BAR_STEPS);
    CHECK_EQ(rb_isplash_fill(0.5), 79);
    int prev = 0;
    for (int i = 0; i <= 1000; i++) {
        int f = rb_isplash_fill(i / 1000.0);
        CHECK(f >= prev);
        prev = f;
    }
}

/* ---- the art file ---------------------------------------------------------------- */

struct piece {
    const char *tag;
    int x, y, w, h;
    uint32_t colour;
};

static void put16(uint8_t *p, unsigned v) { p[0] = (uint8_t)v, p[1] = (uint8_t)(v >> 8); }
static void put32(uint8_t *p, uint32_t v) { put16(p, v & 0xffffu), put16(p + 2, v >> 16); }

/* A synthetic RBIS: every piece one colour, except the cap (column index in
 * the colour's low byte) so the composition can be traced. */
static uint8_t *make_rbis(const struct piece *pc, int n, size_t *len)
{
    size_t size = 8 + 16 * (size_t)n;
    for (int i = 0; i < n; i++)
        size += 8 + (size_t)pc[i].w * pc[i].h * 4;
    uint8_t *d = calloc(1, size);
    memcpy(d, "RBIS", 4);
    put16(d + 4, 1);
    put16(d + 6, (unsigned)n);
    size_t off = 8 + 16 * (size_t)n;
    for (int i = 0; i < n; i++) {
        uint8_t *e = d + 8 + 16 * (size_t)i;
        memcpy(e, pc[i].tag, 4);
        put16(e + 4, (unsigned)(uint16_t)pc[i].x);
        put16(e + 6, (unsigned)(uint16_t)pc[i].y);
        size_t blob = 8 + (size_t)pc[i].w * pc[i].h * 4;
        put32(e + 8, (uint32_t)off);
        put32(e + 12, (uint32_t)blob);
        uint8_t *b = d + off;
        memcpy(b, "RBI1", 4);
        put16(b + 4, (unsigned)pc[i].w);
        put16(b + 6, (unsigned)pc[i].h);
        for (int y = 0; y < pc[i].h; y++)
            for (int x = 0; x < pc[i].w; x++) {
                uint32_t c = pc[i].colour;
                if (!strcmp(pc[i].tag, "BCAP"))
                    c = (c & 0xffff00u) | (uint32_t)x;
                uint8_t *q = b + 8 + ((size_t)y * pc[i].w + x) * 4;
                q[0] = (uint8_t)(c >> 16), q[1] = (uint8_t)(c >> 8), q[2] = (uint8_t)c, q[3] = 255;
            }
        off += blob;
    }
    *len = size;
    return d;
}

#define C_BOX 0x101010u
#define C_WORD 0x202020u
#define C_WELC 0x303030u
#define C_STRT 0x404040u
#define C_BARE 0x505050u
#define C_BARF 0x606060u
#define C_CAP 0x707000u

static const struct piece good[] = {
    {"BOX ", 0, 0, RB_ISPLASH_BOX_W, RB_ISPLASH_BOX_H, C_BOX},
    {"WORD", 37, 171, 347, 77, C_WORD},
    {"WELC", RB_ISPLASH_STATUS_X, RB_ISPLASH_STATUS_Y, RB_ISPLASH_STATUS_W, RB_ISPLASH_STATUS_H, C_WELC},
    {"STRT", RB_ISPLASH_STATUS_X, RB_ISPLASH_STATUS_Y, RB_ISPLASH_STATUS_W, RB_ISPLASH_STATUS_H, C_STRT},
    {"BARE", RB_ISPLASH_BAR_X, RB_ISPLASH_BAR_Y, RB_ISPLASH_BAR_W, RB_ISPLASH_BAR_H, C_BARE},
    {"BARF", RB_ISPLASH_BAR_X, RB_ISPLASH_BAR_Y, RB_ISPLASH_BAR_W, RB_ISPLASH_BAR_H, C_BARF},
    {"BCAP", 0, 0, RB_ISPLASH_CAP_W, RB_ISPLASH_BAR_H - 4, C_CAP},
};

static uint32_t at(const uint32_t *box, int x, int y) { return box[y * RB_ISPLASH_BOX_W + x]; }

static void test_rbis(void)
{
    size_t n;
    uint8_t *d = make_rbis(good, 7, &n);
    struct rb_isplash_art a;
    CHECK_EQ(rb_isplash_art_from_rbis(&a, d, n), 0);
    CHECK_EQ(a.source, RB_ISPLASH_MACOS);
    CHECK(a.named);
    uint32_t *out = malloc((size_t)RB_ISPLASH_BOX_W * RB_ISPLASH_BOX_H * 4);
    rb_isplash_compose(out, &a, RB_ISPLASH_WELCOME, 0);
    CHECK_EQ(at(out, 0, 0), C_BOX);
    CHECK_EQ(at(out, 40, 200), C_WORD);
    CHECK_EQ(at(out, 100, 270), C_WELC);
    CHECK_EQ(at(out, RB_ISPLASH_BAR_X + 50, RB_ISPLASH_BAR_Y + 5), C_BOX); /* no bar while welcoming */
    for (int fill = 0; fill <= RB_ISPLASH_BAR_STEPS + 3; fill++) {
        rb_isplash_compose(out, &a, RB_ISPLASH_STARTING, fill);
        CHECK_EQ(at(out, 100, 270), C_STRT);
        int nf = fill < RB_ISPLASH_BAR_MIN ? RB_ISPLASH_BAR_MIN : fill > RB_ISPLASH_BAR_STEPS ? RB_ISPLASH_BAR_STEPS : fill;
        int bad = 0;
        for (int y = 0; y < RB_ISPLASH_BAR_H; y++)
            for (int x = 0; x < RB_ISPLASH_BAR_W; x++) {
                uint32_t c = at(out, RB_ISPLASH_BAR_X + x, RB_ISPLASH_BAR_Y + y), want = C_BARE;
                bool interior = y >= 2 && y < RB_ISPLASH_BAR_H - 2 && x >= 2 && x < RB_ISPLASH_BAR_W - 2;
                int cap0 = 2 + nf - 3;
                if (interior && x < cap0)
                    want = C_BARF;
                else if (interior && x < cap0 + RB_ISPLASH_CAP_W)
                    want = C_CAP | (uint32_t)(x - cap0);
                bad += c != want;
            }
        CHECK_EQ(bad, 0);
    }
    /* the full bar: the end's separator lands on the frame (Mac OS's full bar) */
    rb_isplash_compose(out, &a, RB_ISPLASH_STARTING, RB_ISPLASH_BAR_STEPS);
    CHECK_EQ(at(out, RB_ISPLASH_BAR_X + RB_ISPLASH_BAR_W - 3, RB_ISPLASH_BAR_Y + 5), C_CAP | 2u);
    CHECK_EQ(at(out, RB_ISPLASH_BAR_X + RB_ISPLASH_BAR_W - 2, RB_ISPLASH_BAR_Y + 5), C_BARE);
    rb_isplash_art_free(&a);

    /* without a name the built-in one is drawn into the box's panel */
    size_t n2;
    uint8_t *d2 = make_rbis((const struct piece[]){good[0], good[2], good[3], good[4], good[5], good[6]}, 6, &n2);
    CHECK_EQ(rb_isplash_art_from_rbis(&a, d2, n2), 0);
    CHECK(!a.named);
    int black = 0;
    for (int i = 0; i < RB_ISPLASH_BOX_W * RB_ISPLASH_BOX_H; i++)
        black += a.box[i] == 0;
    CHECK(black > 200);
    rb_isplash_art_free(&a);
    free(d2);

    /* rejected: truncation, a wrong magic or version, sizes, pieces outside the box */
    for (size_t cut = 0; cut < n; cut += n / 13 + 1)
        CHECK_EQ(rb_isplash_art_from_rbis(&a, d, cut), -1);
    uint8_t *bad = malloc(n);
    memcpy(bad, d, n);
    bad[0] = 'X';
    CHECK_EQ(rb_isplash_art_from_rbis(&a, bad, n), -1);
    memcpy(bad, d, n);
    bad[4] = 2;
    CHECK_EQ(rb_isplash_art_from_rbis(&a, bad, n), -1);
    memcpy(bad, d, n);
    bad[8 + 12] ^= 0x40; /* the BOX's length */
    CHECK_EQ(rb_isplash_art_from_rbis(&a, bad, n), -1);
    free(bad);
    struct piece moved[7];
    memcpy(moved, good, sizeof moved);
    moved[2].x = 300; /* the welcome strip would leave the box */
    size_t n3;
    uint8_t *d3 = make_rbis(moved, 7, &n3);
    CHECK_EQ(rb_isplash_art_from_rbis(&a, d3, n3), -1);
    free(d3);
    memcpy(moved, good, sizeof moved);
    moved[0].w = 421;
    d3 = make_rbis(moved, 7, &n3);
    CHECK_EQ(rb_isplash_art_from_rbis(&a, d3, n3), -1);
    free(d3);
    memcpy(moved, good, sizeof moved);
    d3 = make_rbis(moved, 6, &n3); /* no cap */
    CHECK_EQ(rb_isplash_art_from_rbis(&a, d3, n3), -1);
    free(d3);
    /* a missing or bad file: the built-in art */
    CHECK_EQ(rb_isplash_art_load(&a, "/nonexistent/installer-startup.rbis"), RB_ISPLASH_BUILTIN);
    rb_isplash_art_free(&a);
    free(out);
    free(d);
}

static void test_builtin(void)
{
    struct rb_isplash_art a;
    CHECK_EQ(rb_isplash_art_builtin(&a), 0);
    CHECK_EQ(a.source, RB_ISPLASH_BUILTIN);
    /* the box's geometry: black outline, the panel's outline, white panel */
    CHECK_EQ(at(a.box, 0, 0), 0x000000);
    CHECK_EQ(at(a.box, RB_ISPLASH_BOX_W - 1, RB_ISPLASH_BOX_H - 1), 0x000000);
    CHECK_EQ(at(a.box, 10, 10), 0xdddddd);
    CHECK_EQ(at(a.box, RB_ISPLASH_PANEL_X, 100), 0x000000);
    CHECK_EQ(at(a.box, RB_ISPLASH_PANEL_X + 3, RB_ISPLASH_PANEL_Y + 3), 0xffffff);
    /* the name stands in the panel, centred where Mac OS's stands */
    int x0 = RB_ISPLASH_BOX_W, x1 = -1, y1 = -1;
    for (int y = 160; y < RB_ISPLASH_PANEL_Y + RB_ISPLASH_PANEL_H - 1; y++)
        for (int x = RB_ISPLASH_PANEL_X + 1; x < RB_ISPLASH_PANEL_X + RB_ISPLASH_PANEL_W - 1; x++)
            if (at(a.box, x, y) == 0) {
                x0 = x < x0 ? x : x0;
                x1 = x > x1 ? x : x1;
                y1 = y > y1 ? y : y1;
            }
    CHECK(x1 > x0);
    CHECK(abs((x0 + x1 + 1) / 2 - RB_ISPLASH_NAME_CX) <= 2);
    CHECK_EQ(y1, RB_ISPLASH_NAME_BASELINE - 1);
    /* status lines differ, inside their strip; bar pieces as composed */
    CHECK(memcmp(a.status[0].px, a.status[1].px, (size_t)a.status[0].w * a.status[0].h * 4) != 0);
    uint32_t *out = malloc((size_t)RB_ISPLASH_BOX_W * RB_ISPLASH_BOX_H * 4);
    rb_isplash_compose(out, &a, RB_ISPLASH_STARTING, 40);
    uint32_t dark = at(out, RB_ISPLASH_BAR_X + 2 + 40 - 1, RB_ISPLASH_BAR_Y + 5);
    CHECK_EQ(dark, 0x3a3d8c); /* RetroBoot's bar closes the fill with a dark line */
    CHECK(at(out, RB_ISPLASH_BAR_X + 10, RB_ISPLASH_BAR_Y + 5) != at(out, RB_ISPLASH_BAR_X + 100, RB_ISPLASH_BAR_Y + 5));
    free(out);
    rb_isplash_art_free(&a);
}

/* ---- progress ----------------------------------------------------------------------- */

static void test_parse(void)
{
    int p;
    char st[32];
    const char *ok = "650 presenter Starting the display\n";
    CHECK_EQ(rb_isplash_parse_progress(ok, strlen(ok), &p, st, sizeof st), 0);
    CHECK_EQ(p, 650);
    CHECK(!strcmp(st, "presenter"));
    CHECK_EQ(rb_isplash_parse_progress("1000 ready", 10, &p, NULL, 0), 0);
    CHECK_EQ(p, 1000);
    CHECK_EQ(rb_isplash_parse_progress("0 kernel x", 10, &p, st, sizeof st), 0);
    CHECK_EQ(p, 0);
    const char *bad[] = {"", "\n", "1001 x y", "-5 kernel", "abc kernel", "650", "650 ", "650\n", " 650 presenter",
                         "99999 x"};
    for (size_t i = 0; i < sizeof bad / sizeof bad[0]; i++)
        CHECK_EQ(rb_isplash_parse_progress(bad[i], strlen(bad[i]), &p, st, sizeof st), -1);
    CHECK_EQ(rb_isplash_parse_progress("550 display-driver-with-a-very-long-name z", 42, &p, st, 8), 0);
    CHECK(strlen(st) == 7);
    CHECK_EQ(rb_isplash_next_stage(0), 50);
    CHECK_EQ(rb_isplash_next_stage(450), 550);
    CHECK_EQ(rb_isplash_next_stage(651), 800);
    CHECK_EQ(rb_isplash_next_stage(1000), 1000);
}

static void test_progress_model(void)
{
    struct rb_isplash_progress p;
    memset(&p, 0, sizeof p);
    /* no file: the kernel-time estimate, monotonic, 85 % at the expected time, below 95 % */
    double prev = -1;
    for (uint64_t t = 0; t < 120000; t += 250) {
        double g = rb_isplash_goal(&p, t);
        CHECK(g >= prev - 1e-12);
        CHECK(g < 0.95 + 1e-9);
        prev = g;
    }
    CHECK(rb_isplash_goal(&p, (uint64_t)RB_ISPLASH_EXPECT_MS) > 0.849);
    /* stages: at the stage, creeping at most half way to the next one */
    rb_isplash_progress_update(&p, 250, 1000);
    CHECK(rb_isplash_goal(&p, 1000) > 0.249 && rb_isplash_goal(&p, 1000) < 0.251);
    CHECK(rb_isplash_goal(&p, 60000) <= 0.300 + 1e-9);
    CHECK(rb_isplash_goal(&p, 5000) > 0.26);
    rb_isplash_progress_update(&p, 100, 2000); /* lower values are ignored */
    CHECK_EQ(p.permille, 250);
    /* easing: never backwards, reaches the goal, never above 1 */
    memset(&p, 0, sizeof p);
    p.t_shown = 1000;
    rb_isplash_progress_update(&p, 550, 1000);
    double last = 0;
    for (uint64_t t = 1000; t < 4000; t += 16) {
        double v = rb_isplash_advance(&p, t);
        CHECK(v >= last);
        last = v;
    }
    CHECK(last > 0.55 && last < 0.65);
    rb_isplash_progress_update(&p, 450, 4100); /* a lower stage later: nothing moves back */
    CHECK(rb_isplash_advance(&p, 4200) >= last);
    rb_isplash_progress_update(&p, 1000, 5000);
    for (uint64_t t = 5000; t < 6000; t += 16)
        rb_isplash_advance(&p, t);
    CHECK(p.shown == 1.0);
    CHECK_EQ(rb_isplash_fill(p.shown), RB_ISPLASH_BAR_STEPS);
}

/* ---- one process's screen ---------------------------------------------------------------- */

static char tmpdir[64];
static char progress_path[128], visible_path[128], cmdline_path[128];

static void write_file(const char *path, const char *text)
{
    FILE *f = fopen(path, "w");
    if (f) {
        fputs(text, f);
        fclose(f);
    }
}

static int redraws, redraw_what;
static void count_redraw(void *ctx, int what)
{
    (void)ctx;
    redraws++;
    redraw_what |= what;
}

static void test_mode(void)
{
    unsetenv("RB_INSTALLER_SPLASH");
    write_file(cmdline_path, "BOOT_IMAGE=/live/vmlinuz boot=retroboot-live retroboot.mode=installer quiet\n");
    CHECK(rb_isplash_installer_mode(cmdline_path));
    CHECK(rb_isplash_classic_startup(cmdline_path));
    write_file(cmdline_path, "retroboot.mode=installer retroboot.startup_style=macosx\n");
    CHECK(rb_isplash_installer_mode(cmdline_path));
    CHECK(!rb_isplash_classic_startup(cmdline_path));
    write_file(cmdline_path, "retroboot.mode=installer retroboot.startup_style=macosx retroboot.startup_style=classic\n");
    CHECK(rb_isplash_classic_startup(cmdline_path));
    write_file(cmdline_path, "root=PARTLABEL=RBROOT_A quiet retroboot.progress=on\n");
    CHECK(!rb_isplash_installer_mode(cmdline_path));
    CHECK(!rb_isplash_classic_startup(cmdline_path));
    write_file(cmdline_path, "retroboot.mode=installer retroboot.mode=normal\n");
    CHECK(!rb_isplash_installer_mode(cmdline_path));
    write_file(cmdline_path, "retroboot.mode=installerx\n");
    CHECK(!rb_isplash_installer_mode(cmdline_path));
    setenv("RB_INSTALLER_SPLASH", "1", 1);
    CHECK(rb_isplash_installer_mode(cmdline_path));
    setenv("RB_INSTALLER_SPLASH", "0", 1);
    CHECK(!rb_isplash_installer_mode(cmdline_path));
}

/* The splash draws whole frames; the presenter composes dirty rectangles
 * over the same startup scene: the same pixels. */
static int frames_differ(struct rb_isplash_run *r, int W, int H, int s, int pixel_req, int gw, int gh)
{
    uint32_t *a = malloc((size_t)W * H * 4), *b = malloc((size_t)W * H * 4);
    struct rb_startup_geom sg;
    rb_startup_geometry(&sg, W, H, s, pixel_req, gw, gh);
    struct rb_icon_art icon;
    rb_icon_art_builtin(&icon);
    rb_startup_scene(a, W, W, H, (rb_rect){0, 0, W, H}, s, &sg, &icon, false, 0, true);
    rb_isplash_run_scene(r, a, W, W, H, (rb_rect){0, 0, W, H}, s, pixel_req, gw, gh);
    memset(b, 0x5a, (size_t)W * H * 4);
    for (int i = 0; i < 400; i++) {
        rb_rect c = {(int)(rbt_rand() % (unsigned)W), (int)(rbt_rand() % (unsigned)H), 1 + (int)(rbt_rand() % 300),
                     1 + (int)(rbt_rand() % 300)};
        rb_rect cl;
        if (!rb_rect_intersect(c, (rb_rect){0, 0, W, H}, &cl))
            continue;
        rb_startup_scene(b, W, W, H, cl, s, &sg, &icon, false, 0, true);
        rb_isplash_run_scene(r, b, W, W, H, cl, s, pixel_req, gw, gh);
    }
    /* then everything once more in tiles, so no pixel is left from the fill */
    for (int y = 0; y < H; y += 97)
        for (int x = 0; x < W; x += 131) {
            rb_rect cl;
            rb_rect_intersect((rb_rect){x, y, 131, 97}, (rb_rect){0, 0, W, H}, &cl);
            rb_startup_scene(b, W, W, H, cl, s, &sg, &icon, false, 0, true);
            rb_isplash_run_scene(r, b, W, W, H, cl, s, pixel_req, gw, gh);
        }
    int d = 0;
    for (size_t i = 0; i < (size_t)W * H; i++)
        d += a[i] != b[i];
    free(a);
    free(b);
    return d;
}

static void test_run(void)
{
    setenv("RB_INSTALLER_SPLASH", "1", 1);
    setenv("RB_INSTALLER_PROGRESS", progress_path, 1);
    setenv("RB_INSTALLER_VISIBLE", visible_path, 1);
    unlink(progress_path);
    unlink(visible_path);
    struct rb_isplash_run r;
    CHECK(rb_isplash_run_start(&r, NULL, "/nonexistent.rbis", 2000, count_redraw, NULL));
    CHECK_EQ(r.art.source, RB_ISPLASH_BUILTIN);
    CHECK_EQ(r.phase, RB_ISPLASH_WELCOME);
    /* only the kernel stage: welcome, however long */
    write_file(progress_path, "50 kernel Starting\n");
    redraws = 0;
    rb_isplash_run_tick(&r, 2500);
    rb_isplash_run_tick(&r, 4000);
    CHECK_EQ(r.phase, RB_ISPLASH_WELCOME);
    CHECK_EQ(redraws, 0);
    /* the next stage: "Starting Up..." with the bar */
    write_file(progress_path, "100 medium-search Looking for the installer\n");
    rb_isplash_run_tick(&r, 4100);
    CHECK_EQ(r.phase, RB_ISPLASH_STARTING);
    CHECK(redraw_what & RB_ISPLASH_DIRTY_STATUS);
    int last = r.fill;
    CHECK(last >= RB_ISPLASH_BAR_MIN);
    const char *stages[] = {"250 medium-found x\n", "350 root-mounted x\n", "450 switch-root x\n",
                            "550 display-driver x\n", "650 presenter x\n"};
    uint64_t t = 4100;
    for (size_t i = 0; i < sizeof stages / sizeof stages[0]; i++) {
        write_file(progress_path, stages[i]);
        for (int k = 0; k < 60; k++) {
            t += 33;
            rb_isplash_run_tick(&r, t);
            CHECK(r.fill >= last);
            last = r.fill;
        }
    }
    CHECK(r.fill > 650 * RB_ISPLASH_BAR_STEPS / 1000 - 2);
    /* boot-diag's Startup Details: no drawing while they cover the screen, the
     * box again after */
    write_file(visible_path, "1\n");
    write_file(progress_path, "800 hostd x\n");
    redraws = 0;
    for (int k = 0; k < 30; k++)
        rb_isplash_run_tick(&r, t += 33);
    CHECK_EQ(redraws, 0);
    CHECK(r.covered);
    uint32_t px = 0x123456;
    rb_isplash_run_scene(&r, &px, 1, 1, 1, (rb_rect){0, 0, 1, 1}, 1, 0, 0, 0);
    CHECK_EQ(px, 0x123456); /* nothing drawn over them */
    unlink(visible_path);
    redraw_what = 0;
    rb_isplash_run_tick(&r, t += 33);
    CHECK(redraw_what & RB_ISPLASH_DIRTY_ALL);
    CHECK(!r.covered);

    /* identity, then the handoff: the presenter continues with the same pixels */
    for (size_t i = 0; i < sizeof layouts / sizeof layouts[0]; i++) {
        const struct layout *l = &layouts[i];
        int s = rb_effective_scale(l->W, l->H, l->pixel_req);
        CHECK_EQ(frames_differ(&r, l->W, l->H, s, l->pixel_req, l->guest_w, l->guest_h), 0);
    }
    char line[256];
    CHECK_EQ(rb_isplash_state_format(&r, line, sizeof line), 0);
    struct rb_isplash_run q;
    CHECK(rb_isplash_run_start(&q, NULL, NULL, t + 5, NULL, NULL));
    CHECK_EQ(rb_isplash_state_apply(&q, line, t + 5), 0);
    CHECK_EQ(q.phase, r.phase);
    CHECK_EQ(q.fill, r.fill);
    CHECK_EQ(q.art.source, r.art.source);
    CHECK(!memcmp(q.composed, r.composed, (size_t)RB_ISPLASH_BOX_W * RB_ISPLASH_BOX_H * 4));
    CHECK(q.prog.shown == r.prog.shown || (q.prog.shown - r.prog.shown) < 1e-9);
    CHECK_EQ(rb_isplash_state_apply(&q, "isplash garbage", t), -1);
    CHECK_EQ(rb_isplash_state_apply(&q, "isplash 1 7 3 0 0 0 0 0 0 macos 0", t), -1);
    /* ready: the bar completes */
    write_file(progress_path, "1000 ready x\n");
    for (int k = 0; k < 40; k++)
        rb_isplash_run_tick(&q, t += 33);
    CHECK_EQ(q.fill, RB_ISPLASH_BAR_STEPS);
    /* the end: nothing drawn any more */
    rb_isplash_run_end(&q);
    px = 0x654321;
    rb_isplash_run_scene(&q, &px, 1, 1, 1, (rb_rect){0, 0, 1, 1}, 1, 0, 0, 0);
    CHECK_EQ(px, 0x654321);
    /* a splash that showed none: neither does the presenter */
    struct rb_isplash_run z;
    unlink(progress_path);
    CHECK(rb_isplash_run_start(&z, NULL, NULL, 10, NULL, NULL));
    CHECK_EQ(rb_isplash_state_apply(&z, "isplash 0 0 0 0 0 0 0 0 0 builtin 0", 20), 0);
    CHECK(!z.on);
    rb_isplash_run_free(&z);
    rb_isplash_run_free(&q);
    rb_isplash_run_free(&r);

    /* off after "ready" (a presenter restart) and outside installer mode */
    write_file(progress_path, "1000 ready Installer\n");
    CHECK(!rb_isplash_run_start(&r, NULL, NULL, 1000, NULL, NULL));
    rb_isplash_run_free(&r);
    unlink(progress_path);
    setenv("RB_INSTALLER_SPLASH", "0", 1);
    CHECK(!rb_isplash_run_start(&r, NULL, NULL, 1000, NULL, NULL));
    uint32_t fb[64 * 48], ref[64 * 48];
    for (int i = 0; i < 64 * 48; i++)
        fb[i] = ref[i] = 0xabcdef;
    rb_isplash_run_scene(&r, fb, 64, 64, 48, (rb_rect){0, 0, 64, 48}, 1, 0, 0, 0);
    CHECK(!memcmp(fb, ref, sizeof fb));
    rb_isplash_run_free(&r);
    unsetenv("RB_INSTALLER_SPLASH");
}

/* The welcome line stays at least RB_ISPLASH_WELCOME_MS, also when the
 * stages are already past (a display driver that binds late). */
static void test_welcome_first(void)
{
    setenv("RB_INSTALLER_SPLASH", "1", 1);
    write_file(progress_path, "550 display-driver x\n");
    struct rb_isplash_run r;
    CHECK(rb_isplash_run_start(&r, NULL, NULL, 9000, NULL, NULL));
    rb_isplash_run_tick(&r, 9500);
    CHECK_EQ(r.phase, RB_ISPLASH_WELCOME);
    rb_isplash_run_tick(&r, 9000 + RB_ISPLASH_WELCOME_MS);
    CHECK_EQ(r.phase, RB_ISPLASH_STARTING);
    rb_isplash_run_free(&r);
    /* the welcome line's second begins when the box is first drawn (a
     * display that comes up after the splash started) */
    uint64_t t0 = rb_boot_ms();
    CHECK(rb_isplash_run_start(&r, NULL, NULL, t0 > 5000 ? t0 - 5000 : 0, NULL, NULL));
    uint32_t px1 = 0;
    rb_isplash_run_scene(&r, &px1, 1, 1, 1, (rb_rect){0, 0, 1, 1}, 1, 0, 0, 0);
    CHECK(r.drawn);
    CHECK(r.t_welcome >= t0);
    rb_isplash_run_tick(&r, r.t_welcome + RB_ISPLASH_WELCOME_MS - 1);
    CHECK_EQ(r.phase, RB_ISPLASH_WELCOME);
    rb_isplash_run_tick(&r, r.t_welcome + RB_ISPLASH_WELCOME_MS);
    CHECK_EQ(r.phase, RB_ISPLASH_STARTING);
    rb_isplash_run_free(&r);
    /* no progress file: the estimate after the welcome */
    unlink(progress_path);
    CHECK(rb_isplash_run_start(&r, NULL, NULL, 3000, NULL, NULL));
    rb_isplash_run_tick(&r, 3000 + RB_ISPLASH_WELCOME_MS);
    CHECK_EQ(r.phase, RB_ISPLASH_STARTING);
    CHECK(!r.prog.have);
    rb_isplash_run_free(&r);
    unsetenv("RB_INSTALLER_SPLASH");
}

int main(void)
{
    snprintf(tmpdir, sizeof tmpdir, "/tmp/test_isplash.XXXXXX");
    if (!mkdtemp(tmpdir))
        return 1;
    snprintf(progress_path, sizeof progress_path, "%s/progress", tmpdir);
    snprintf(visible_path, sizeof visible_path, "%s/visible", tmpdir);
    snprintf(cmdline_path, sizeof cmdline_path, "%s/cmdline", tmpdir);
    test_box_origin();
    test_geometry();
    test_fill();
    test_rbis();
    test_builtin();
    test_parse();
    test_progress_model();
    test_mode();
    test_run();
    test_welcome_first();
    unlink(progress_path);
    unlink(visible_path);
    unlink(cmdline_path);
    rmdir(tmpdir);
    TEST_DONE();
}
