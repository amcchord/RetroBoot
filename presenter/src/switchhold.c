#include "switchhold.h"

#include <string.h>

const char *rb_switch_verdict_name(int v)
{
    switch (v) {
    case RB_SWITCH_SETTLED: return "redrawn";
    case RB_SWITCH_LIVE: return "changing";
    case RB_SWITCH_AS_IS: return "arrived complete";
    case RB_SWITCH_TIMEOUT: return "timeout";
    default: return "hold";
    }
}

/* Uniform: black, or one gray level (within the 4.3 tolerance, by the 2 x 2
 * block means, so Mac OS's 1-pixel dither is its mean gray), over at least
 * RB_SWITCH_UNIFORM_PERMILLE of the samples. Returns the larger share (per
 * mille). */
static int uniform_permille(const uint32_t *mean, int n)
{
    int diff[258];
    memset(diff, 0, sizeof diff);
    int black = 0;
    for (int i = 0; i < n; i++) {
        uint32_t m = mean[i];
        int r = (m >> 16) & 0xff, g = (m >> 8) & 0xff, b = m & 0xff;
        int mx = r > g ? (r > b ? r : b) : (g > b ? g : b);
        int mn = r < g ? (r < b ? r : b) : (g < b ? g : b);
        if (mx <= RB_CONTENT_NONBLACK_MAX) {
            black++;
            continue;
        }
        int lo = mx - RB_CONTENT_GRAY_TOL, hi = mn + RB_CONTENT_GRAY_TOL;
        if (lo < 0)
            lo = 0;
        if (hi > 255)
            hi = 255;
        if (lo <= hi) {
            diff[lo]++;
            diff[hi + 1]--;
        }
    }
    int best = black, run = 0;
    for (int level = 0; level <= 255; level++) {
        run += diff[level];
        if (level >= RB_CONTENT_MIN_GRAY && run > best)
            best = run;
    }
    return n ? best * 1000 / n : 1000;
}

void rb_switch_hold_begin(struct rb_switch_hold *s, const uint32_t *px, int w, int h, int stride_px,
                          uint64_t now_ms)
{
    uint32_t mean[RB_CONTENT_SAMPLES];
    memset(s, 0, sizeof *s);
    s->start_ms = now_ms;
    s->samples = rb_content_sample(px, w, h, stride_px, s->arrival, mean);
    s->last_uniform_permille = uniform_permille(mean, s->samples);
    s->uniform = s->last_uniform_permille >= RB_SWITCH_UNIFORM_PERMILLE;
}

int rb_switch_hold_check(struct rb_switch_hold *s, const uint32_t *px, int w, int h, int stride_px,
                         uint64_t now_ms, uint64_t last_damage_ms)
{
    uint32_t raw[RB_CONTENT_SAMPLES], mean[RB_CONTENT_SAMPLES];
    int n = rb_content_sample(px, w, h, stride_px, raw, mean);
    if (n != s->samples) { /* another surface: start over */
        rb_switch_hold_begin(s, px, w, h, stride_px, s->start_ms);
        return now_ms - s->start_ms >= RB_SWITCH_MAX_MS ? RB_SWITCH_TIMEOUT : RB_SWITCH_HOLD;
    }
    if (!s->changed)
        s->changed = memcmp(raw, s->arrival, (size_t)n * sizeof raw[0]) != 0;
    s->last_uniform_permille = uniform_permille(mean, n);
    s->uniform = s->last_uniform_permille >= RB_SWITCH_UNIFORM_PERMILLE;
    if (s->uniform || !s->changed)
        s->live_since_ms = 0;
    else if (!s->live_since_ms)
        s->live_since_ms = now_ms;
    int newest = (s->draw_head + RB_SWITCH_LIVE_DRAWS - 1) % RB_SWITCH_LIVE_DRAWS;
    if (last_damage_ms > s->start_ms && (!s->ndraws || s->draws[newest] != last_damage_ms)) {
        s->draws[s->draw_head] = last_damage_ms;
        s->draw_head = (s->draw_head + 1) % RB_SWITCH_LIVE_DRAWS;
        if (s->ndraws < RB_SWITCH_LIVE_DRAWS)
            s->ndraws++;
    }
    /* the oldest of the last RB_SWITCH_LIVE_DRAWS draws (the ring is full) */
    bool busy = s->ndraws == RB_SWITCH_LIVE_DRAWS && now_ms - s->draws[s->draw_head] <= RB_SWITCH_LIVE_MS;
    uint64_t drawn = last_damage_ms > s->start_ms ? last_damage_ms : s->start_ms;
    uint64_t quiet = now_ms > drawn ? now_ms - drawn : 0;
    if (now_ms - s->start_ms >= RB_SWITCH_MAX_MS)
        return RB_SWITCH_TIMEOUT;
    if (!s->changed)
        return !s->uniform && quiet >= RB_SWITCH_STALE_MS ? RB_SWITCH_AS_IS : RB_SWITCH_HOLD;
    if (s->uniform)
        return RB_SWITCH_HOLD;
    if (quiet >= RB_SWITCH_SETTLE_MS)
        return RB_SWITCH_SETTLED;
    if (busy && now_ms - s->live_since_ms >= RB_SWITCH_LIVE_MS)
        return RB_SWITCH_LIVE;
    return RB_SWITCH_HOLD;
}
