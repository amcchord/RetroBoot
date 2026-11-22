/* Startup Details view (diagview.h): command line, Escape, the document and
 * the picture -- the same pixels at every pixel scale, so the firmware
 * framebuffer, the splash and the presenter draw the same view. */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "diagview.h"
#include "drm.h"
#include "rbtest.h"

static void cfg_tests(void)
{
    struct rb_diag_cfg c;
    rb_diag_cmdline("boot=retroboot-live rb.live=.x fstab=no retroboot.mode=installer quiet", &c);
    CHECK(c.enabled && c.installer && !c.show && c.fb);
    rb_diag_cmdline("retroboot.mode=installer retroboot.diag=0", &c);
    CHECK(!c.enabled && c.installer);
    rb_diag_cmdline("root=PARTLABEL=RBROOT_A quiet", &c);
    CHECK(!c.enabled && !c.installer);
    rb_diag_cmdline("root=PARTLABEL=RBROOT_A retroboot.diag=1", &c);
    CHECK(c.enabled && !c.installer && !c.show);
    rb_diag_cmdline("root=x retroboot.diag=show retroboot.diag_fb=0", &c);
    CHECK(c.enabled && c.show && !c.fb);
    rb_diag_cmdline("retroboot.mode=installerx", &c); /* whole words only */
    CHECK(!c.enabled);
    rb_diag_cmdline("retroboot.diag=1 retroboot.diag=0", &c); /* the last one wins */
    CHECK(!c.enabled);
    rb_diag_cmdline("", &c);
    CHECK(!c.enabled);
}

static void escape_tests(void)
{
    /* installed system without retroboot.diag=1: unchanged (startup options) */
    CHECK_EQ(rb_diag_escape(false, false, true, true), RB_DIAG_ESC_PREBOOT);
    CHECK_EQ(rb_diag_escape(false, false, false, true), RB_DIAG_ESC_NONE);
    CHECK_EQ(rb_diag_escape(false, false, true, false), RB_DIAG_ESC_PREBOOT);
    /* installer: the checkerboard opens the view; the panel keeps its Escape */
    CHECK_EQ(rb_diag_escape(true, false, true, true), RB_DIAG_ESC_SHOW);
    CHECK_EQ(rb_diag_escape(true, false, false, true), RB_DIAG_ESC_NONE);
    /* no display at all (amdgpu failed): Escape is always the way to the details */
    CHECK_EQ(rb_diag_escape(true, false, false, false), RB_DIAG_ESC_SHOW);
    /* visible: Escape hides it, whatever is under it */
    CHECK_EQ(rb_diag_escape(true, true, false, true), RB_DIAG_ESC_HIDE);
    CHECK_EQ(rb_diag_escape(false, true, true, true), RB_DIAG_ESC_HIDE);
    /* Return in the view: the startup options only on an installed system's
     * checkerboard (the installer has none; after the reveal it is swallowed) */
    CHECK(rb_diag_return_opens_options(false, true, true));
    CHECK(!rb_diag_return_opens_options(true, true, true));
    CHECK(!rb_diag_return_opens_options(false, true, false));
    CHECK(!rb_diag_return_opens_options(false, false, true));
}

static struct rb_diag_doc doc, doc2;

static void sample_doc(struct rb_diag_doc *d)
{
    rb_diag_doc_init(d, "Startup Details");
    rb_diag_doc_add(d, 'H', "RetroBoot 1.1.0 product, build 1.1.0-20261016T202525Z-ac38d6d3 \xe2\x80\x94 Installer");
    rb_diag_doc_add(d, 'H', "Computer: LENOVO ThinkPad T14 Gen 5 (21MC0001US) \xc2\xb7 BIOS LENOVO R2EET41W (1.22)");
    rb_diag_doc_add(d, 'H', "Processor: AMD Ryzen 7 PRO 8840HS w/ Radeon 780M Graphics (16 threads)");
    rb_diag_doc_stage(d, RB_DIAG_OK, "0.9 s", "Startup system loaded");
    rb_diag_doc_stage(d, RB_DIAG_OK, "2.4 s", "Installer disk found (/dev/sda)");
    rb_diag_doc_stage(d, RB_DIAG_FAIL, "", "Display driver: amdgpu failed to start (error -2) (AMD 1002:15bf)");
    rb_diag_doc_stage(d, RB_DIAG_WAIT, "", "RetroBoot screen");
    rb_diag_doc_stage(d, RB_DIAG_SKIP, "", "Mac OS shown");
    for (int i = 0; i < 9; i++)
        rb_diag_doc_add(d, 'P', "12.3 s  amdgpu 0000:c4:00.0: Direct firmware load for amdgpu/gc_11_5_0_mes_2.bin failed "
                                "with error -2 and a very long tail that has to wrap onto further lines of the column");
    rb_diag_doc_add(d, 'D', "AMD 1002:15bf at 0000:c4:00.0: no driver (amdgpu failed to start (error -2)) (startup display)");
    rb_diag_doc_add(d, 'D', "Keyboards: AT Translated Set 2 keyboard; Logitech USB Receiver");
    for (int i = 0; i < 60; i++) {
        char t[128];
        snprintf(t, sizeof t, "[%5d.000] kernel message number %d with some text", i, i);
        rb_diag_doc_add(d, 'L', t);
    }
    rb_diag_doc_add(d, 'F', "Saved on the USB stick as retroboot/startup-details/20261016-211403-1a2b3c4d.txt");
}

static void doc_tests(void)
{
    sample_doc(&doc);
    static char text[64 * 1024];
    size_t n = rb_diag_doc_format(&doc, text, sizeof text);
    CHECK(n > 100);
    rb_diag_doc_parse(text, n, &doc2);
    CHECK(!memcmp(&doc, &doc2, sizeof doc));
    /* tabs/control characters in fields never survive; junk lines are ignored */
    const char junk[] = "T\tTitle\nX\tunknown\nS\tok\nS\tbogus\t1 s\tA stage\nP\tline\twith tab\n\n";
    rb_diag_doc_parse(junk, sizeof junk - 1, &doc2);
    CHECK(!strcmp(doc2.title, "Title"));
    CHECK_EQ(doc2.n, 2);
    CHECK_EQ(doc2.line[0].state, RB_DIAG_WAIT);
    CHECK(!strcmp(doc2.line[0].time, "1 s"));
    CHECK(!strcmp(doc2.line[1].text, "line with tab"));
    /* a full document stops at its line limit */
    rb_diag_doc_init(&doc2, NULL);
    for (int i = 0; i < RB_DIAG_MAX_LINES + 20; i++)
        rb_diag_doc_add(&doc2, 'L', "x");
    CHECK_EQ(doc2.n, RB_DIAG_MAX_LINES);
}

/* the frame at scale s == the frame at 1x of the logical size, each pixel s x s */
static int scale_identity(int w, int h, int s, const struct rb_diag_doc *d)
{
    uint32_t *big = calloc((size_t)w * h, 4), *small = calloc((size_t)(w / s) * (h / s), 4);
    rb_diag_render(big, w, h, w, s, d);
    rb_diag_render(small, w / s, h / s, w / s, 1, d);
    int bad = 0;
    for (int y = 0; y < h && !bad; y++)
        for (int x = 0; x < w; x++)
            if (big[(size_t)y * w + x] != small[(size_t)(y / s) * (w / s) + x / s]) {
                bad = 1;
                fprintf(stderr, "scale %d mismatch at %d,%d\n", s, x, y);
                break;
            }
    free(big);
    free(small);
    return bad;
}

static void render_tests(void)
{
    sample_doc(&doc);
    static const int sizes[][3] = {{1280, 800, 1}, {1920, 1080, 2}, {2880, 1800, 3}, {3840, 2160, 4},
                                   {1366, 768, 1}, {2560, 1600, 2}, {1024, 768, 1}, {800, 600, 1}};
    for (size_t i = 0; i < sizeof sizes / sizeof sizes[0]; i++)
        CHECK_EQ(scale_identity(sizes[i][0], sizes[i][1], sizes[i][2], &doc), 0);

    /* readable content: the window, text in black, a red failure mark */
    int w = 960, h = 540;
    uint32_t *px = calloc((size_t)w * h, 4), *px2 = calloc((size_t)w * h, 4);
    rb_diag_render(px, w, h, w, 1, &doc);
    long white = 0, black = 0, red = 0;
    for (int i = 0; i < w * h; i++) {
        white += px[i] == 0xffffff;
        black += px[i] == 0;
        red += px[i] == 0xcc0000;
    }
    CHECK(white > w * h / 2); /* the window's page */
    CHECK(black > 2000);      /* text and frame */
    CHECK(red > 20);          /* the failed stage and the problem bullets */
    /* deterministic */
    rb_diag_render(px2, w, h, w, 1, &doc);
    CHECK(!memcmp(px, px2, (size_t)w * h * 4));
    /* the margins outside the window show the classic 50 % pattern */
    CHECK_EQ(px[0], 0x000000u);
    CHECK_EQ(px[1], 0xffffffu);
    /* stride wider than the frame: the extra pixels are left alone */
    int stride = w + 7;
    uint32_t *wide = malloc((size_t)stride * h * 4);
    for (int i = 0; i < stride * h; i++)
        wide[i] = 0x123456;
    rb_diag_render(wide, w, h, stride, 1, &doc);
    int kept = 1;
    for (int y = 0; y < h; y++)
        for (int x = w; x < stride; x++)
            kept &= wide[(size_t)y * stride + x] == 0x123456;
    CHECK(kept);
    int same = 1;
    for (int y = 0; y < h; y++)
        same &= !memcmp(wide + (size_t)y * stride, px + (size_t)y * w, (size_t)w * 4);
    CHECK(same);
    free(wide);
    /* the keys line: NULL is "Esc: back"; the startup options line changes only
     * the footer row and still fits the narrowest frame's window */
    rb_diag_render_keys(px2, w, h, w, 1, &doc, NULL);
    CHECK(!memcmp(px, px2, (size_t)w * h * 4));
    rb_diag_render_keys(px2, w, h, w, 1, &doc, RB_DIAG_KEYS_OPTIONS);
    int first = -1, last = -1;
    for (int y = 0; y < h; y++)
        if (memcmp(px + (size_t)y * w, px2 + (size_t)y * w, (size_t)w * 4)) {
            if (first < 0)
                first = y;
            last = y;
        }
    CHECK(first > h - 40 && last < h - 6);
    CHECK(rb_diag_text_width(RB_DIAG_FONT_SYSTEM, RB_DIAG_KEYS_OPTIONS) < (800 - 12 - 24) / 2);
    /* empty document, tiny frames and odd sizes do not crash (ASan) */
    struct rb_diag_doc empty;
    rb_diag_doc_init(&empty, NULL);
    rb_diag_render(px, w, h, w, 1, &empty);
    rb_diag_render(px, 100, 80, 100, 1, &doc);
    rb_diag_render(px, 321, 203, 321, 2, &doc);
    rb_diag_render(px, 7, 5, 7, 4, &doc);
    rb_diag_render(px, 640, 480, 640, 1, NULL);
    free(px);
    free(px2);
}

static void hint_tests(void)
{
    static const int sizes[][3] = {{1280, 800, 1}, {1920, 1080, 2}, {3840, 2160, 4}, {2880, 1800, 3}};
    for (size_t i = 0; i < sizeof sizes / sizeof sizes[0]; i++) {
        int w = sizes[i][0], h = sizes[i][1], s = sizes[i][2];
        rb_rect r = rb_diag_hint_rect(w, h, s), r1 = rb_diag_hint_rect(w / s, h / s, 1);
        CHECK(r.x >= 0 && r.y >= 0 && r.x + r.w <= w && r.y + r.h <= h);
        CHECK(r.x == r1.x * s && r.y == r1.y * s && r.w == r1.w * s && r.h == r1.h * s);
        CHECK(r.y > h / 2); /* at the bottom, away from the startup indicator */
        uint32_t *px = malloc((size_t)w * h * 4);
        for (int k = 0; k < w * h; k++)
            px[k] = 0xabcdef;
        rb_diag_render_hint(px, w, h, w, s);
        int outside = 0, changed = 0;
        for (int y = 0; y < h; y++)
            for (int x = 0; x < w; x++) {
                bool in = x >= r.x && x < r.x + r.w && y >= r.y && y < r.y + r.h;
                if (px[(size_t)y * w + x] != 0xabcdef) {
                    if (in)
                        changed++;
                    else
                        outside++;
                }
            }
        CHECK_EQ(outside, 0);
        CHECK(changed > r.w * r.h / 2);
        free(px);
    }
    CHECK(rb_diag_text_width(RB_DIAG_FONT_SYSTEM, RB_DIAG_HINT_TEXT) > 100);
    CHECK(rb_diag_text_width(RB_DIAG_FONT_SMALL, "abc") < rb_diag_text_width(RB_DIAG_FONT_SYSTEM, "abc"));
}

/* A page flip the driver does not complete counts once after the limit
 * (rb_output_check_stall, published by the presenter and the splash). */
static void stall_tests(void)
{
    static struct rb_output o;
    memset(&o, 0, sizeof o);
    o.pending = -1;
    CHECK(!rb_output_check_stall(&o, 10000, 1000)); /* nothing pending */
    o.pending = 1;
    o.pending_mono_ms = 5000;
    CHECK(!rb_output_check_stall(&o, 5900, 1000));
    CHECK(rb_output_check_stall(&o, 6000, 1000));
    CHECK(!rb_output_check_stall(&o, 9000, 1000)); /* counted once per flip */
    CHECK_EQ(o.flip_stalls, 1);
    CHECK_EQ(o.stall_first_mono_ms, 6000);
    o.pending = -1; /* completed (late) ... */
    o.pending_stalled = false;
    o.pending = 0; /* ... and the next one stalls too */
    o.pending_mono_ms = 12000;
    CHECK(rb_output_check_stall(&o, 13500, 1000));
    CHECK_EQ(o.flip_stalls, 2);
    CHECK_EQ(o.stall_first_mono_ms, 6000);
    CHECK_EQ(o.stall_last_mono_ms, 13500);
    o.virt = true; /* the lab's virtual display completes flips by a timer */
    o.pending_stalled = false;
    CHECK(!rb_output_check_stall(&o, 99999, 1000));
}

int main(void)
{
    stall_tests();
    cfg_tests();
    escape_tests();
    doc_tests();
    render_tests();
    hint_tests();
    TEST_DONE();
}
