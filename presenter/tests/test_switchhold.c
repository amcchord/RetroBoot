/* Guest-initiated mode switches (switchhold.h): the last good frame stays
 * through what the lab and the N5105 showed at a switch -- the previous
 * picture at the new stride, Mac OS's dither clear, a desktop in mid-redraw --
 * and the cut comes on the first complete frame; a picture that arrives
 * complete, a game that keeps animating and a guest that draws nothing are
 * shown within bounded times. Also rb_content_sample against the 4.3 sampling. */
#include <stdlib.h>
#include <string.h>

#include "content.h"
#include "rbtest.h"
#include "switchhold.h"

#define W 832
#define H 624

static uint32_t *frame_new(void) { return calloc((size_t)W * H, 4); }

static void fill(uint32_t *f, uint32_t c)
{
    for (int i = 0; i < W * H; i++)
        f[i] = c;
}

/* Mac OS's 50 % dither (black at the origin) with its cursor at (16, 16). */
static void dither(uint32_t *f)
{
    for (int y = 0; y < H; y++)
        for (int x = 0; x < W; x++)
            f[y * W + x] = (x + y) & 1 ? 0xffffffu : 0;
    for (int y = 16; y < 32; y++)
        for (int x = 16; x < 16 + (y - 16); x++)
            f[y * W + x] = 0;
}

/* A desktop: the blue pattern, a menu bar, `icons` icons down the right. */
static void desktop(uint32_t *f, int icons)
{
    for (int y = 0; y < H; y++)
        for (int x = 0; x < W; x++)
            f[y * W + x] = ((x / 7 + y / 5) % 9) ? 0x5c5c99u : 0x6a6aa8u;
    for (int y = 0; y < 20; y++)
        for (int x = 0; x < W; x++)
            f[y * W + x] = y == 19 ? 0 : 0xddddddu;
    for (int i = 0; i < icons; i++)
        for (int y = 40 + i * 60; y < 72 + i * 60; y++)
            for (int x = W - 60; x < W - 28; x++)
                f[y * W + x] = 0x203040u + (uint32_t)(x * 7 + y);
}

/* The 640 x 480 Finder read at the 832-pixel stride (ati-vga's stale frame). */
static void restrided(uint32_t *f)
{
    uint32_t *old = calloc(640 * 480, 4);
    for (int y = 0; y < 480; y++)
        for (int x = 0; x < 640; x++)
            old[y * 640 + x] = y < 20 ? 0xddddddu : (((x / 7 + y / 5) % 9) ? 0x5c5c99u : 0x6a6aa8u);
    memset(f, 0, (size_t)W * H * 4);
    memcpy(f, old, 640 * 480 * 4); /* linear: the same bytes, another stride */
    free(old);
}

/* Drive a sequence: frames[i] shown from t[i] on (DAMAGE at each t[i]);
 * inspections every 16 ms. Returns the time of the verdict (or -1). */
static long run(struct rb_switch_hold *s, uint32_t **frames, const long *t, int n, long until, int *verdict,
                long *never_before)
{
    rb_switch_hold_begin(s, frames[0], W, H, W, 1000);
    uint64_t last_damage = 1000;
    int k = 0;
    for (long now = 0; now <= until; now += 4) {
        while (k + 1 < n && t[k + 1] <= now) {
            k++;
            last_damage = 1000 + (uint64_t)t[k];
        }
        if (now % 16)
            continue;
        int v = rb_switch_hold_check(s, frames[k], W, H, W, 1000 + (uint64_t)now, last_damage);
        if (v != RB_SWITCH_HOLD) {
            *verdict = v;
            if (never_before)
                *never_before = t[n - 1];
            return now;
        }
    }
    *verdict = RB_SWITCH_HOLD;
    return -1;
}

int main(void)
{
    struct rb_switch_hold s;
    int v;

    /* rb_content_sample matches the 4.3 grid: 80 x 60 samples, the block mean
     * of a dither is its mean gray */
    {
        uint32_t *f = frame_new(), raw[RB_CONTENT_SAMPLES], mean[RB_CONTENT_SAMPLES];
        dither(f);
        CHECK_EQ(rb_content_sample(f, W, H, W, raw, mean), RB_CONTENT_SAMPLES);
        int gray = 0;
        for (int i = 0; i < RB_CONTENT_SAMPLES; i++)
            gray += ((mean[i] >> 8) & 0xff) >= 120 && ((mean[i] >> 8) & 0xff) <= 135;
        CHECK(gray > RB_CONTENT_SAMPLES * 99 / 100);
        CHECK_EQ(rb_content_sample(f, 10, 8, W, raw, mean), 80); /* tiny: every pixel */
        CHECK_EQ(rb_content_sample(NULL, W, H, W, raw, mean), 0);
        free(f);
    }

    /* N5105 (hardware): the old picture at the new stride (25 ms), Mac OS's
     * dither clear (80 ms), then the desktop redrawn over ~200 ms */
    {
        uint32_t *f[6];
        for (int i = 0; i < 6; i++)
            f[i] = frame_new();
        restrided(f[0]);
        dither(f[1]);
        desktop(f[2], 0);
        desktop(f[3], 1);
        desktop(f[4], 3);
        desktop(f[5], 5);
        long t[6] = {0, 25, 105, 170, 240, 305};
        long end = run(&s, f, t, 6, 3000, &v, NULL);
        CHECK_EQ(v, RB_SWITCH_SETTLED);
        CHECK(end >= 305 + RB_SWITCH_SETTLE_MS);   /* never on a partial desktop */
        CHECK(end <= 305 + RB_SWITCH_SETTLE_MS + 16);
        CHECK(s.changed);
        CHECK(!s.uniform);
        for (int i = 0; i < 6; i++)
            free(f[i]);
    }

    /* the lab (builder): the first frame is already Mac OS's dither clear, the
     * redraw follows within 15 ms and goes on in 16 ms steps for 120 ms */
    {
        uint32_t *f[5];
        for (int i = 0; i < 5; i++)
            f[i] = frame_new();
        dither(f[0]);
        desktop(f[1], 0);
        desktop(f[2], 2);
        desktop(f[3], 4);
        desktop(f[4], 5);
        long t[5] = {0, 15, 98, 115, 132};
        long end = run(&s, f, t, 5, 3000, &v, NULL);
        CHECK_EQ(v, RB_SWITCH_SETTLED);
        CHECK(end >= 132 + RB_SWITCH_SETTLE_MS && end <= 132 + RB_SWITCH_SETTLE_MS + 16);
        for (int i = 0; i < 5; i++)
            free(f[i]);
    }

    /* a loaded host (the builder at load 6): the desktop and its pattern, a
     * 354 ms pause, the Control Strip, 218 ms, the icons -- the cut waits for
     * the icons and is not taken for an animation */
    {
        uint32_t *f[4];
        for (int i = 0; i < 4; i++)
            f[i] = frame_new();
        dither(f[0]);
        desktop(f[1], 0);
        desktop(f[2], 0);
        f[2][(H - 10) * W + 5] = 0xffffffu; /* the Control Strip */
        desktop(f[3], 5);
        long t[4] = {0, 70, 424, 642};
        long end = run(&s, f, t, 4, 3000, &v, NULL);
        CHECK_EQ(v, RB_SWITCH_SETTLED);
        CHECK(end >= 642 + RB_SWITCH_SETTLE_MS && end <= 642 + RB_SWITCH_SETTLE_MS + 16);
        for (int i = 0; i < 4; i++)
            free(f[i]);
    }

    /* a stale frame that stays still is never shown before RB_SWITCH_STALE_MS:
     * here the clear comes after 300 ms, then the desktop */
    {
        uint32_t *f[3];
        for (int i = 0; i < 3; i++)
            f[i] = frame_new();
        restrided(f[0]);
        dither(f[1]);
        desktop(f[2], 5);
        long t[3] = {0, 300, 420};
        long end = run(&s, f, t, 3, 3000, &v, NULL);
        CHECK_EQ(v, RB_SWITCH_SETTLED);
        CHECK(end >= 420 + RB_SWITCH_SETTLE_MS);
        for (int i = 0; i < 3; i++)
            free(f[i]);
    }

    /* it arrived complete and nothing is drawn after: shown as it is */
    {
        uint32_t *f[1] = {frame_new()};
        desktop(f[0], 5);
        long t[1] = {0};
        long end = run(&s, f, t, 1, 3000, &v, NULL);
        CHECK_EQ(v, RB_SWITCH_AS_IS);
        CHECK(end >= RB_SWITCH_STALE_MS && end <= RB_SWITCH_STALE_MS + 16);
        free(f[0]);
    }

    /* a guest that clears to black and draws nothing: the longest hold */
    {
        uint32_t *f[2] = {frame_new(), frame_new()};
        fill(f[0], 0x404040u);
        fill(f[1], 0);
        long t[2] = {0, 30};
        long end = run(&s, f, t, 2, 3000, &v, NULL);
        CHECK_EQ(v, RB_SWITCH_TIMEOUT);
        CHECK(end >= RB_SWITCH_MAX_MS && end <= RB_SWITCH_MAX_MS + 16);
        free(f[0]);
        free(f[1]);
    }

    /* a game: black, then frames that keep changing every 16 ms */
    {
        enum { N = 200 };
        uint32_t **f = calloc(N, sizeof *f);
        long t[N];
        f[0] = frame_new();
        t[0] = 0;
        for (int i = 1; i < N; i++) {
            f[i] = frame_new();
            for (int y = 0; y < H; y++)
                for (int x = 0; x < W; x++)
                    f[i][y * W + x] = (uint32_t)((x + i * 3) * 2654435761u ^ (y * 40503u)) & 0xffffffu;
            t[i] = 40 + (i - 1) * 16;
        }
        long end = run(&s, f, t, N, 3000, &v, NULL);
        CHECK_EQ(v, RB_SWITCH_LIVE);
        CHECK(end >= 40 + RB_SWITCH_LIVE_MS && end <= 40 + RB_SWITCH_LIVE_MS + 32);
        CHECK(end < 40 + RB_SWITCH_SETTLE_MS + RB_SWITCH_MAX_MS); /* not held to the end */
        for (int i = 0; i < N; i++)
            free(f[i]);
        free(f);
    }

    /* a large desktop with nothing but a menu bar is not a "clear" */
    {
        enum { BW = 1920, BH = 1440 };
        uint32_t *a = calloc((size_t)BW * BH, 4), *b = calloc((size_t)BW * BH, 4);
        for (int i = 0; i < BW * BH; i++)
            a[i] = b[i] = 0x888888u;
        for (int y = 0; y < 20; y++)
            for (int x = 0; x < BW; x++)
                b[y * BW + x] = y == 19 ? 0 : 0xeeeeeeu;
        rb_switch_hold_begin(&s, a, BW, BH, BW, 0);
        CHECK(s.uniform); /* a plain gray is the clear */
        CHECK_EQ(rb_switch_hold_check(&s, b, BW, BH, BW, 100, 10), RB_SWITCH_HOLD);
        CHECK(!s.uniform);
        CHECK(s.last_uniform_permille < RB_SWITCH_UNIFORM_PERMILLE);
        CHECK_EQ(rb_switch_hold_check(&s, b, BW, BH, BW, 10 + RB_SWITCH_SETTLE_MS, 10), RB_SWITCH_SETTLED);
        free(a);
        free(b);
    }

    CHECK(!strcmp(rb_switch_verdict_name(RB_SWITCH_SETTLED), "redrawn"));
    CHECK(!strcmp(rb_switch_verdict_name(RB_SWITCH_HOLD), "hold"));
    TEST_DONE();
}
