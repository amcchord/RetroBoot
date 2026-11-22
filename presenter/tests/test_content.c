/* First genuine content detection (ARCHITECTURE.md 4.3). */
#include <string.h>

#include "content.h"
#include "rbtest.h"

#define W 640
#define H 480
static uint32_t frame[W * H];
static uint64_t now;

static void solid(uint32_t c)
{
    for (int i = 0; i < W * H; i++)
        frame[i] = c;
}

static void rect(int x, int y, int w, int h, uint32_t c)
{
    for (int j = y; j < y + h; j++)
        for (int i = x; i < x + w; i++)
            frame[j * W + i] = c;
}

/* One inspection, `dt` ms after the previous one. */
static int inspect_at(rb_content *c, int dt)
{
    now += (uint64_t)dt;
    return rb_content_inspect(c, frame, W, H, W, now);
}

/* Inspect a static frame on the 100 ms poll until something fires (or not). */
static int settle(rb_content *c, int max_ms)
{
    for (int t = 0; t < max_ms; t += 16) {
        int r = inspect_at(c, 16);
        if (r)
            return r;
    }
    return RB_CONTENT_NONE;
}

/* Mac OS startup gray painted top-down: never detected on a partly painted
 * frame, detected once the full gray has been stable for three refreshes.
 * Each band stays on screen for `hold` ms with an inspection every 16 ms (a
 * stalled paint); the gray rule never fires on a partial frame, and the
 * non-black rule needs a frame unchanged for 500 ms. */
static void top_down_paint(int step_rows, int hold)
{
    rb_content c;
    rb_content_reset(&c);
    solid(0);
    CHECK_EQ(inspect_at(&c, 100), RB_CONTENT_NONE);
    for (int y = 0; y < H; y += step_rows) {
        int rows = y + step_rows > H ? H - y : step_rows;
        rect(0, y, W, rows, 0x00999999);
        if (y + rows < H) {
            for (int t = 0; t < hold; t += 16) {
                CHECK_EQ(inspect_at(&c, 16), RB_CONTENT_NONE);
                CHECK(!c.detected);
            }
        }
    }
    rect(296, 210, 48, 60, 0x00000000); /* happy Mac */
    rect(300, 214, 40, 30, 0x00ffffff);
    CHECK_EQ(inspect_at(&c, 16), RB_CONTENT_NONE); /* complete, but not yet settled */
    CHECK_EQ(inspect_at(&c, 16), RB_CONTENT_NONE);
    CHECK_EQ(inspect_at(&c, 16), RB_CONTENT_NONE);
    CHECK_EQ(inspect_at(&c, 16), RB_CONTENT_GRAY); /* 48 ms unchanged */
    CHECK(c.detected);
    CHECK(c.last_gray_pct >= RB_CONTENT_GRAY_PCT);
    CHECK_EQ(c.last_changed_pct, 0);
    CHECK_EQ(inspect_at(&c, 16), RB_CONTENT_NONE); /* fires once */
}

int main(void)
{
    rb_content c;

    /* Black OpenBIOS phase, with sparse white "text": never content. */
    rb_content_reset(&c);
    solid(0);
    for (int i = 0; i < 20; i++)
        CHECK_EQ(inspect_at(&c, 100), RB_CONTENT_NONE);
    for (int y = 10; y < 200; y += 16)
        rect(20, y, 300, 8, 0xffffff); /* ~10 % of the frame */
    for (int i = 0; i < 20; i++)
        CHECK_EQ(inspect_at(&c, 100), RB_CONTENT_NONE);
    CHECK(!c.detected);
    CHECK(c.samples >= 4096);

    /* Startup gray painted top-down in 12 % bands at the 16 ms refresh, one
     * row per refresh, and a paint that stalls for 480 ms per band (just
     * under the non-black settle time). */
    top_down_paint(H * 12 / 100, 16);
    top_down_paint(1, 16);
    top_down_paint(H * 12 / 100, 480);

    /* The Wave 2 failure: a frame 61-77 % painted (23-39 % black) caught on a
     * DAMAGE, followed immediately by the 100 ms poll of the same frame. */
    rb_content_reset(&c);
    solid(0);
    CHECK_EQ(inspect_at(&c, 100), RB_CONTENT_NONE);
    rect(0, 0, W, H * 61 / 100, 0x00999999);
    CHECK_EQ(inspect_at(&c, 16), RB_CONTENT_NONE);
    CHECK_EQ(inspect_at(&c, 100), RB_CONTENT_NONE);
    CHECK(c.last_gray_pct >= 59 && c.last_gray_pct <= 62);
    rect(0, H * 61 / 100, W, H - H * 61 / 100, 0x00999999);
    CHECK_EQ(inspect_at(&c, 16), RB_CONTENT_NONE);
    CHECK_EQ(inspect_at(&c, 100), RB_CONTENT_GRAY);

    /* Full gray delivered in one refresh: detected on the next poll. */
    rb_content_reset(&c);
    solid(0);
    CHECK_EQ(inspect_at(&c, 100), RB_CONTENT_NONE);
    solid(0x00999999);
    CHECK_EQ(inspect_at(&c, 16), RB_CONTENT_NONE);
    CHECK_EQ(inspect_at(&c, 100), RB_CONTENT_GRAY);

    /* Slightly noisy gray (+-10 per channel) still counts. */
    rb_content_reset(&c);
    for (int i = 0; i < W * H; i++) {
        int v = 0x80 + (int)(rbt_rand() % 21) - 10;
        frame[i] = (uint32_t)(v << 16 | (v + 1) << 8 | (v - 1));
    }
    CHECK_EQ(settle(&c, 200), RB_CONTENT_GRAY);

    /* Gray with a "Welcome to Mac OS" box (~6 % of the frame) is below the
     * 97 % gray rule; it is detected by the non-black rule once settled. */
    rb_content_reset(&c);
    solid(0x00999999);
    rect(220, 160, 200, 92, 0x00ffffff);
    CHECK_EQ(inspect_at(&c, 16), RB_CONTENT_NONE);
    CHECK_EQ(inspect_at(&c, 100), RB_CONTENT_NONE);
    CHECK(c.last_gray_pct < RB_CONTENT_GRAY_PCT);
    CHECK_EQ(settle(&c, 600), RB_CONTENT_NONBLACK);
    CHECK(now - c.settled_since_ms >= RB_CONTENT_NONBLACK_SETTLE_MS);

    /* A small animation (progress bar, < 1 % of samples) does not unsettle. */
    rb_content_reset(&c);
    solid(0x00999999);
    rect(220, 160, 200, 92, 0x00ffffff);
    CHECK_EQ(inspect_at(&c, 16), RB_CONTENT_NONE);
    int fired = 0;
    for (int t = 0; t < 40 && !fired; t++) {
        rect(240, 230, 4 * t, 4, 0x00303080);
        fired = inspect_at(&c, 16);
    }
    CHECK_EQ(fired, RB_CONTENT_NONBLACK);

    /* Near-black gray (below the non-black threshold) is not content. */
    rb_content_reset(&c);
    solid(0x00141414);
    CHECK_EQ(settle(&c, 1000), RB_CONTENT_NONE);
    CHECK(!c.detected);

    /* Colourful non-gray picture: non-black rule once settled for 500 ms. */
    rb_content_reset(&c);
    for (int i = 0; i < W * H; i++)
        frame[i] = (rbt_rand() & 0xff) << 16 | 0x40 << 8 | 0xc0;
    CHECK_EQ(inspect_at(&c, 16), RB_CONTENT_NONE);
    CHECK_EQ(inspect_at(&c, 400), RB_CONTENT_NONE);
    CHECK_EQ(inspect_at(&c, 100), RB_CONTENT_NONBLACK);

    /* A changing non-black picture (every refresh repaints a new image) never
     * settles. */
    rb_content_reset(&c);
    for (int t = 0; t < 60; t++) {
        for (int i = 0; i < W * H; i++)
            frame[i] = (rbt_rand() & 0xff) << 16 | (rbt_rand() & 0xff);
        CHECK_EQ(inspect_at(&c, 16), RB_CONTENT_NONE);
    }

    /* 30 % coloured area on black: non-black rule (>= 25 %). */
    rb_content_reset(&c);
    solid(0);
    rect(0, 0, W, H * 3 / 10 + 2, 0x00c04000);
    CHECK_EQ(settle(&c, 600), RB_CONTENT_NONBLACK);

    /* Mac OS's startup background on a built-in LCD: a 1-pixel 50 % dither,
     * black at the origin (QuickDraw's gray pattern), painted top-down. It
     * reads as its mean gray (never as uniform white), so the gray rule
     * holds the cut until the dither is complete, then fires as it does on
     * the startup gray. */
    rb_content_reset(&c);
    solid(0);
    CHECK_EQ(inspect_at(&c, 100), RB_CONTENT_NONE);
    for (int y = 0; y < H; y++) {
        for (int x = 0; x < W; x++)
            frame[y * W + x] = ((x + y) & 1) ? 0xffffff : 0;
        if (y % 48 == 47 && y + 1 < H)
            CHECK_EQ(inspect_at(&c, 16), RB_CONTENT_NONE);
    }
    CHECK(c.last_gray_pct < RB_CONTENT_GRAY_PCT);
    rect(296, 210, 32, 32, 0x00cccccc); /* the Happy Mac, drawn over it */
    CHECK_EQ(settle(&c, 100), RB_CONTENT_GRAY);
    CHECK(c.last_gray_pct >= RB_CONTENT_GRAY_PCT);
    CHECK(c.last_gray_level >= 127 - RB_CONTENT_GRAY_TOL && c.last_gray_level <= 128 + RB_CONTENT_GRAY_TOL);

    /* A 2-pixel dither does not alias into a uniform reading either: not
     * the gray rule (its 2x2 blocks are partly white), the non-black one. */
    rb_content_reset(&c);
    for (int y = 0; y < H; y++)
        for (int x = 0; x < W; x++)
            frame[y * W + x] = ((x / 2 + y / 2) & 1) ? 0 : 0xffffff;
    CHECK_EQ(inspect_at(&c, 16), RB_CONTENT_NONE);
    CHECK(c.last_gray_pct < RB_CONTENT_GRAY_PCT);
    CHECK_EQ(settle(&c, 600), RB_CONTENT_NONBLACK);

    /* Tiny surfaces sample every pixel. */
    rb_content_reset(&c);
    uint32_t tiny[16 * 8];
    for (int i = 0; i < 16 * 8; i++)
        tiny[i] = 0x00a0a0a0;
    CHECK_EQ(rb_content_inspect(&c, tiny, 16, 8, 16, 1000), RB_CONTENT_NONE);
    CHECK_EQ(rb_content_inspect(&c, tiny, 16, 8, 16, 1060), RB_CONTENT_GRAY);
    TEST_DONE();
}
