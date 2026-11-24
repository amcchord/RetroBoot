#include "content.h"

#include <string.h>

void rb_content_reset(rb_content *c) { memset(c, 0, sizeof *c); }

/* The colour a sample is classified by: the mean of the 2 x 2 block it
 * starts (moved inside the frame at its last row and column), so a 1-pixel
 * 50 % dither -- Mac OS's startup background on a built-in LCD -- reads as
 * its mean gray, as the eye sees it, and never as uniform white or black. */
static uint32_t block_mean(const uint32_t *px, int w, int h, int stride_px, int x, int y)
{
    int x0 = x + 1 < w ? x : (x > 0 ? x - 1 : x), y0 = y + 1 < h ? y : (y > 0 ? y - 1 : y);
    int x1 = x0 + 1 < w ? x0 + 1 : x0, y1 = y0 + 1 < h ? y0 + 1 : y0;
    uint32_t q[4] = {px[(size_t)y0 * stride_px + x0], px[(size_t)y0 * stride_px + x1],
                     px[(size_t)y1 * stride_px + x0], px[(size_t)y1 * stride_px + x1]};
    uint32_t out = 0;
    for (int sh = 0; sh <= 16; sh += 8) {
        unsigned sum = 0;
        for (int i = 0; i < 4; i++)
            sum += (q[i] >> sh) & 0xffu;
        out |= ((sum + 2) / 4) << sh;
    }
    return out;
}

/* The sample point of grid cell (i, j) of a gx x gy grid over w x h: one
 * pseudo-random point per cell, so periodic patterns such as 1- or 2-pixel
 * dithers cannot alias into a uniform reading. */
static void sample_point(int i, int j, int w, int h, int gx, int gy, int *x, int *y)
{
    int y0 = (int)(((long)j * h) / gy), y1 = (int)(((long)(j + 1) * h) / gy);
    int x0 = (int)(((long)i * w) / gx), x1 = (int)(((long)(i + 1) * w) / gx);
    uint32_t hsh = (uint32_t)i * 0x9e3779b1u ^ (uint32_t)j * 0x85ebca77u;
    hsh ^= hsh >> 15;
    hsh *= 0x2c1b3c6du;
    hsh ^= hsh >> 12;
    *x = x0 + (int)(hsh % (uint32_t)(x1 > x0 ? x1 - x0 : 1));
    *y = y0 + (int)((hsh >> 16) % (uint32_t)(y1 > y0 ? y1 - y0 : 1));
}

int rb_content_sample(const uint32_t *px, int w, int h, int stride_px, uint32_t *raw, uint32_t *mean)
{
    if (!px || w <= 0 || h <= 0)
        return 0;
    int gx = w < RB_CONTENT_GRID_X ? w : RB_CONTENT_GRID_X;
    int gy = h < RB_CONTENT_GRID_Y ? h : RB_CONTENT_GRID_Y;
    int n = 0;
    for (int j = 0; j < gy; j++)
        for (int i = 0; i < gx; i++) {
            int x, y;
            sample_point(i, j, w, h, gx, gy, &x, &y);
            raw[n] = px[(size_t)y * stride_px + x] & 0x00ffffffu;
            mean[n] = block_mean(px, w, h, stride_px, x, y);
            n++;
        }
    return n;
}

int rb_content_inspect(rb_content *c, const uint32_t *px, int w, int h, int stride_px, uint64_t now_ms)
{
    if (c->detected || !px || w <= 0 || h <= 0)
        return RB_CONTENT_NONE;
    int gx = w < RB_CONTENT_GRID_X ? w : RB_CONTENT_GRID_X;
    int gy = h < RB_CONTENT_GRID_Y ? h : RB_CONTENT_GRID_Y;
    /* coverage[G] = number of samples within +-tol of gray level G
     * (difference array over intervals [max-tol, min+tol]). */
    int diff[258];
    memset(diff, 0, sizeof diff);
    int nonblack = 0, total = 0, changed = 0, moved = 0;
    bool same_grid = c->have_ref && c->samples == gx * gy;
    uint32_t cur[RB_CONTENT_SAMPLES];
    for (int j = 0; j < gy; j++) {
        for (int i = 0; i < gx; i++) {
            int x, y;
            sample_point(i, j, w, h, gx, gy, &x, &y);
            uint32_t p = px[(size_t)y * stride_px + x] & 0x00ffffffu;
            if (same_grid && c->ref[total] != p)
                changed++;
            if (same_grid && c->prev[total] != p)
                moved++;
            cur[total++] = p;
            uint32_t m = block_mean(px, w, h, stride_px, x, y);
            int r = (m >> 16) & 0xff, g = (m >> 8) & 0xff, b = m & 0xff;
            int mx = r > g ? (r > b ? r : b) : (g > b ? g : b);
            int mn = r < g ? (r < b ? r : b) : (g < b ? g : b);
            if (mx <= RB_CONTENT_NONBLACK_MAX)
                continue; /* black samples never count towards either rule */
            nonblack++;
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
    }
    int best = 0, best_level = 0, run = 0;
    for (int level = 0; level <= 255; level++) {
        run += diff[level];
        if (level >= RB_CONTENT_MIN_GRAY && run > best) {
            best = run;
            best_level = level;
        }
    }
    /* Static: no sample changed since the previous inspection (the startup
     * gray is a still picture; any paint progress resets it).
     * Settled: the samples are compared with the reference frame taken when
     * the current settling period began (not with the previous inspection,
     * so a slow paint accumulates); more than 1 % changed (a paint in
     * progress, a mode switch) starts a new period, a small animation such as
     * a progress bar does not. */
    if (!same_grid || moved)
        c->static_since_ms = now_ms;
    if (!same_grid || changed * 100 > total * RB_CONTENT_CHANGED_PCT) {
        memcpy(c->ref, cur, (size_t)total * sizeof cur[0]);
        c->settled_since_ms = now_ms;
    }
    memcpy(c->prev, cur, (size_t)total * sizeof cur[0]);
    c->have_ref = true;
    c->inspections++;
    c->samples = total;
    c->last_gray_pct = total ? best * 100 / total : 0;
    c->last_gray_level = best_level;
    c->last_nonblack_pct = total ? nonblack * 100 / total : 0;
    c->last_changed_pct = same_grid && total ? changed * 100 / total : 100;
    uint64_t settled = now_ms - c->settled_since_ms, still = now_ms - c->static_since_ms;
    c->last_settled_ms = settled > 1000000 ? 1000000 : (int)settled;
    c->last_static_ms = still > 1000000 ? 1000000 : (int)still;
    if (!total)
        return RB_CONTENT_NONE;
    if (best * 100 >= total * RB_CONTENT_GRAY_PCT && still >= RB_CONTENT_GRAY_SETTLE_MS) {
        c->detected = true;
        c->rule = RB_CONTENT_GRAY;
        return RB_CONTENT_GRAY;
    }
    if (nonblack * 100 >= total * RB_CONTENT_NONBLACK_PCT && settled >= RB_CONTENT_NONBLACK_SETTLE_MS) {
        c->detected = true;
        c->rule = RB_CONTENT_NONBLACK;
        return RB_CONTENT_NONBLACK;
    }
    return RB_CONTENT_NONE;
}

bool rb_content_is_osx_desktop(const uint32_t *px, int w, int h, int stride_px)
{
    if (!px || w <= 0 || h <= 0)
        return false;
    int gx = w < RB_CONTENT_GRID_X ? w : RB_CONTENT_GRID_X;
    int gy = h < RB_CONTENT_GRID_Y ? h : RB_CONTENT_GRID_Y;
    int total = 0, nonblack = 0, boot_gray = 0;
    for (int j = 0; j < gy; j++)
        for (int i = 0; i < gx; i++) {
            int x, y;
            sample_point(i, j, w, h, gx, gy, &x, &y);
            uint32_t m = block_mean(px, w, h, stride_px, x, y);
            int r = (m >> 16) & 0xff, g = (m >> 8) & 0xff, b = m & 0xff;
            int mx = r > g ? (r > b ? r : b) : (g > b ? g : b);
            int mn = r < g ? (r < b ? r : b) : (g < b ? g : b);
            total++;
            if (mx <= RB_CONTENT_NONBLACK_MAX)
                continue;
            nonblack++;
            if (mn >= 172 && mx <= 212 && mx - mn <= 16)
                boot_gray++;
        }
    if (!total || nonblack * 100 < total * RB_CONTENT_NONBLACK_PCT)
        return false;
    if (boot_gray * 100 < total * 85)
        return true;
    /* Even with a neutral gray wallpaper, OS X Finder/LoginWindow draws the bright
     * 22-pixel Aqua menu bar at the top instead of BootX's uniform #bfbfbf top rows. */
    int menubar_rows = h < 20 ? h : 20, step = w > 160 ? w / 80 : 1;
    int bar_total = 0, bar_bright = 0, bar_boot_gray = 0;
    for (int y = 4; y < menubar_rows; y += 4)
        for (int x = 0; x < w; x += step) {
            uint32_t p = px[(size_t)y * stride_px + x] & 0x00ffffffu;
            int r = (p >> 16) & 0xff, g = (p >> 8) & 0xff, b = p & 0xff;
            int mx = r > g ? (r > b ? r : b) : (g > b ? g : b);
            int mn = r < g ? (r < b ? r : b) : (g < b ? g : b);
            bar_total++;
            if (mn >= 216)
                bar_bright++;
            if (mn >= 172 && mx <= 212 && mx - mn <= 16)
                bar_boot_gray++;
        }
    return bar_total > 0 && bar_bright * 100 >= bar_total * 40 && bar_boot_gray * 100 < bar_total * 60;
}
