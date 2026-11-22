/* RetroBoot's startup indicator (startup.h, ARCHITECTURE.md 4.4.1).
 *
 * The indicator is Mac OS's own Happy Mac: a private copy captured from the
 * guest's startup screen (or the Platinum bundle's identical icon), read at
 * run time and never stored in this repository (D012). The built-in art below
 * is only the fallback before any copy exists: original RetroBoot pixel art
 * (October 2026) in the style of presenter/ui's icons, under the same MIT
 * licence (presenter/ui/assets/icons/LICENSE-icons.txt). It shares the
 * compact Mac's geometry (silhouette, screen, light position) so the
 * cut-over lines up; no Apple bitmap was copied or traced.
 */
#include "startup.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <xf86drmMode.h>

#include "scale.h"

#define OPAQUE 0xff000000u

/* ---- art -------------------------------------------------------------------- */

static uint32_t art_colour(char c)
{
    switch (c) {
    case 'K': return 0x000000; /* outline */
    case 'W': return 0xffffff; /* highlight */
    case 'c': return 0xcccccc; /* case */
    case 'a': return 0x999999; /* case shade, foot top */
    case '6': return 0x666666; /* screen bezel */
    case 'm': return 0xaaaadd; /* glass shading */
    case 'l': return 0xccccff; /* screen */
    case '4': return 0x444444; /* floppy slot */
    case '7': return 0x777777; /* foot */
    case '5': return 0x555555; /* foot shadow */
    case 'g': return 0x00aa00; /* power light */
    default: return 0;
    }
}

/* Faceless compact Mac, 32 x 32: case x3..27 / y0..27, foot x4..26 / y28..31,
 * screen bezel x6..24 / y3..17 around the 17 x 13 screen at (7, 4). */
static const char *const builtin_art[RB_ICON_SIZE] = {
    "....KKKKKKKKKKKKKKKKKKKKKKK.....", /*  0 */
    "...KWWWWWWWWWWWWWWWWWWWWWWcK....", /*  1 */
    "...KWcccccccccccccccccccccaK....", /*  2 */
    "...KWc6666666666666666666caK....", /*  3 */
    "...KWc6mmmmmmmmmmmmmmmmmWcaK....", /*  4 */
    "...KWc6mllllllllllllllllWcaK....", /*  5 */
    "...KWc6mllllllllllllllllWcaK....", /*  6 */
    "...KWc6mllllllllllllllllWcaK....", /*  7 */
    "...KWc6mllllllllllllllllWcaK....", /*  8 */
    "...KWc6mllllllllllllllllWcaK....", /*  9 */
    "...KWc6mllllllllllllllllWcaK....", /* 10 */
    "...KWc6mllllllllllllllllWcaK....", /* 11 */
    "...KWc6mllllllllllllllllWcaK....", /* 12 */
    "...KWc6mllllllllllllllllWcaK....", /* 13 */
    "...KWc6mllllllllllllllllWcaK....", /* 14 */
    "...KWc6mllllllllllllllllWcaK....", /* 15 */
    "...KWc6mllllllllllllllllWcaK....", /* 16 */
    "...KWc6WWWWWWWWWWWWWWWWWWcaK....", /* 17 */
    "...KWcccccccccccccccccccccaK....", /* 18 */
    "...KWcccccccccccccccccccccaK....", /* 19 */
    "...KWcccccccccccccccccccccaK....", /* 20 */
    "...KWccccccccc4444444444ccaK....", /* 21 */
    "...KWcggccccccWWWWWWWWWWccaK....", /* 22 */
    "...KWcccccccccccccccccccccaK....", /* 23 */
    "...KWcccccccccccccccccccccaK....", /* 24 */
    "...KWcccccccccccccccccccccaK....", /* 25 */
    "...KWaaaaaaaaaaaaaaaaaaaaaaK....", /* 26 */
    "....KKKKKKKKKKKKKKKKKKKKKKK.....", /* 27 */
    "....KaaaaaaaaaaaaaaaaaaaaaK.....", /* 28 */
    "....K777777777777777777777K.....", /* 29 */
    "....K555555555555555555555K.....", /* 30 */
    "....KKKKKKKKKKKKKKKKKKKKKKK.....", /* 31 */
};

const char *rb_icon_source_name(int source) { return source == RB_ICON_MACOS ? "macos" : "builtin"; }

/* The green light (lower left of the case): saturated green pixels. Off, it
 * is the same green at 40 % (an unlit LED). */
static void find_light(struct rb_icon_art *a)
{
    a->nlight = 0;
    for (int y = 18; y < 27; y++)
        for (int x = 3; x < 13; x++) {
            uint32_t c = a->px[y * RB_ICON_SIZE + x];
            int r = (int)((c >> 16) & 255u), g = (int)((c >> 8) & 255u), b = (int)(c & 255u);
            if (!(c & OPAQUE) || g < 128 || g < r + 64 || g < b + 64 || a->nlight >= RB_ICON_LIGHT_MAX)
                continue;
            a->light[a->nlight] = (uint16_t)(y * RB_ICON_SIZE + x);
            a->light_off[a->nlight] = OPAQUE | (uint32_t)(r * 2 / 5) << 16 | (uint32_t)(g * 2 / 5) << 8 |
                                      (uint32_t)(b * 2 / 5);
            a->nlight++;
        }
}

void rb_icon_art_builtin(struct rb_icon_art *a)
{
    for (int y = 0; y < RB_ICON_SIZE; y++)
        for (int x = 0; x < RB_ICON_SIZE; x++) {
            char c = builtin_art[y][x];
            a->px[y * RB_ICON_SIZE + x] = c == '.' ? 0 : (OPAQUE | art_colour(c));
        }
    a->source = RB_ICON_BUILTIN;
    find_light(a);
}

void rb_icon_art_set(struct rb_icon_art *a, const uint32_t px[RB_ICON_SIZE * RB_ICON_SIZE], int source)
{
    memcpy(a->px, px, sizeof a->px);
    a->source = source;
    find_light(a);
}

bool rb_startup_lit(uint64_t t0, uint64_t t)
{
    return t <= t0 || ((t - t0) / RB_BLINK_MS) % 2 == 0;
}

static bool uniform(const uint32_t *px, int x, int y, int dx, int dy, int n, uint32_t *colour)
{
    uint32_t c = px[y * RB_ICON_SIZE + x];
    for (int i = 1; i < n; i++)
        if (px[(y + i * dy) * RB_ICON_SIZE + x + i * dx] != c)
            return false;
    *colour = c;
    return (c & OPAQUE) != 0;
}

int rb_icon_art_from_rbi(struct rb_icon_art *a, const uint8_t *d, size_t n)
{
    const size_t want = 8 + (size_t)RB_ICON_SIZE * RB_ICON_SIZE * 4;
    if (!d || n != want || memcmp(d, "RBI1", 4))
        return -1;
    if ((d[4] | d[5] << 8) != RB_ICON_SIZE || (d[6] | d[7] << 8) != RB_ICON_SIZE)
        return -1;
    struct rb_icon_art b;
    rb_icon_art_builtin(&b); /* reference silhouette */
    uint32_t px[RB_ICON_SIZE * RB_ICON_SIZE];
    for (int i = 0; i < RB_ICON_SIZE * RB_ICON_SIZE; i++) {
        const uint8_t *p = d + 8 + (size_t)i * 4;
        bool opaque = p[3] >= 128;
        if (p[3] != 0 && p[3] != 255)
            return -1; /* a cicn mask is binary */
        /* The same silhouette as the compact Mac Mac OS draws at startup:
         * anything else is not the icon this indicator stands in for. */
        if (opaque != ((b.px[i] & OPAQUE) != 0))
            return -1;
        px[i] = opaque ? (OPAQUE | (uint32_t)p[0] << 16 | (uint32_t)p[1] << 8 | p[2]) : 0;
    }
    /* The screen bevel around the face: one colour per side. */
    uint32_t top, left, right, bottom;
    if (!uniform(px, RB_ICON_SCREEN_X, RB_ICON_SCREEN_Y - 1, 1, 0, RB_ICON_SCREEN_W, &top) ||
        !uniform(px, RB_ICON_SCREEN_X - 1, RB_ICON_SCREEN_Y, 0, 1, RB_ICON_SCREEN_H, &left) ||
        !uniform(px, RB_ICON_SCREEN_X + RB_ICON_SCREEN_W, RB_ICON_SCREEN_Y, 0, 1, RB_ICON_SCREEN_H, &right) ||
        !uniform(px, RB_ICON_SCREEN_X, RB_ICON_SCREEN_Y + RB_ICON_SCREEN_H, 1, 0, RB_ICON_SCREEN_W, &bottom))
        return -1;
    /* The screen colour: the interior's dominant colour, at least 60 %. */
    uint32_t cand[RB_ICON_SCREEN_W * RB_ICON_SCREEN_H];
    int count[RB_ICON_SCREEN_W * RB_ICON_SCREEN_H], nc = 0, total = 0;
    for (int y = RB_ICON_SCREEN_Y; y < RB_ICON_SCREEN_Y + RB_ICON_SCREEN_H; y++)
        for (int x = RB_ICON_SCREEN_X; x < RB_ICON_SCREEN_X + RB_ICON_SCREEN_W; x++) {
            uint32_t c = px[y * RB_ICON_SIZE + x];
            int j = 0;
            while (j < nc && cand[j] != c)
                j++;
            if (j == nc) {
                cand[nc] = c;
                count[nc++] = 0;
            }
            count[j]++;
            total++;
        }
    int best = 0;
    for (int j = 1; j < nc; j++)
        if (count[j] > count[best])
            best = j;
    if (count[best] * 10 < total * 6 || !(cand[best] & OPAQUE))
        return -1;
    for (int y = RB_ICON_SCREEN_Y; y < RB_ICON_SCREEN_Y + RB_ICON_SCREEN_H; y++)
        for (int x = RB_ICON_SCREEN_X; x < RB_ICON_SCREEN_X + RB_ICON_SCREEN_W; x++)
            px[y * RB_ICON_SIZE + x] = cand[best];
    memcpy(a->px, px, sizeof px);
    a->source = RB_ICON_MACOS;
    find_light(a);
    return 0;
}

void rb_startup_bg(const uint32_t *px, int stride_px, int ix, int iy, uint32_t bg[2])
{
    bg[0] = bg[1] = px[(size_t)iy * stride_px + ix] & 0xffffffu; /* outside the silhouette */
    if (ix < 1 || iy < 1)
        return;
    uint32_t left = px[(size_t)iy * stride_px + ix - 1] & 0xffffffu;
    uint32_t above = px[(size_t)(iy - 1) * stride_px + ix] & 0xffffffu;
    uint32_t diag = px[(size_t)(iy - 1) * stride_px + ix - 1] & 0xffffffu;
    if (left != bg[0] && left == above && diag == bg[0])
        bg[1] = left;
}

int rb_icon_capture_rbi(const uint32_t *px, int stride_px, int w, int h, uint8_t *out)
{
    int ix = (w - RB_ICON_SIZE) / 2, iy = (h - RB_ICON_SIZE) / 2;
    if (!px || ix < 0 || iy < 0)
        return -1;
    uint32_t bgc[2];
    rb_startup_bg(px, stride_px, ix, iy, bgc);
    struct rb_icon_art b;
    rb_icon_art_builtin(&b); /* the silhouette */
    memcpy(out, "RBI1", 4);
    out[4] = RB_ICON_SIZE;
    out[5] = 0;
    out[6] = RB_ICON_SIZE;
    out[7] = 0;
    int drawn = 0;
    for (int y = 0; y < RB_ICON_SIZE; y++)
        for (int x = 0; x < RB_ICON_SIZE; x++) {
            uint32_t c = px[(size_t)(iy + y) * stride_px + ix + x] & 0xffffffu;
            int r = (int)(c >> 16), g = (int)((c >> 8) & 255u), bl = (int)(c & 255u);
            bool bg = c == rb_startup_bg_at(bgc, x, y);
            bool inside = (b.px[y * RB_ICON_SIZE + x] & OPAQUE) != 0;
            if (!inside && !bg)
                return -1; /* something else around it: not the compact Mac on its background */
            drawn += inside && !bg;
            uint8_t *o = out + 8 + (size_t)(y * RB_ICON_SIZE + x) * 4;
            o[0] = (uint8_t)r;
            o[1] = (uint8_t)g;
            o[2] = (uint8_t)bl;
            o[3] = inside ? 255 : 0;
        }
    /* The Happy Mac has ~780 pixels off the gray (on the dither, black and
     * white icon pixels that fall on the same colour do not count); a partly
     * drawn one fails here or in the structure check. */
    struct rb_icon_art check;
    if (drawn < 600 || rb_icon_art_from_rbi(&check, out, RB_ICON_RBI_BYTES) < 0)
        return -1;
    return 0;
}

int rb_icon_art_load(struct rb_icon_art *a, const char *path)
{
    rb_icon_art_builtin(a);
    if (!path || !path[0])
        return a->source;
    FILE *f = fopen(path, "rbe");
    if (!f)
        return a->source;
    uint8_t buf[8 + RB_ICON_SIZE * RB_ICON_SIZE * 4 + 1];
    size_t n = fread(buf, 1, sizeof buf, f);
    fclose(f);
    rb_icon_art_from_rbi(a, buf, n);
    return a->source;
}

/* RetroBoot's own Platinum progress bar (presenter/ui pt_progress colours):
 * black frame; lavender bar shaded top to bottom over a recessed light track;
 * a dark line closes a partial fill. */
static uint32_t lerp_rgb(uint32_t a, uint32_t b, int t, int n)
{
    if (n <= 0)
        return a;
    uint32_t out = 0;
    for (int sh = 0; sh <= 16; sh += 8) {
        int ca = (int)((a >> sh) & 255u), cb = (int)((b >> sh) & 255u);
        out |= (uint32_t)(ca + (cb - ca) * t / n) << sh;
    }
    return out;
}

void rb_icon_compose(uint32_t out[RB_ICON_SIZE * RB_ICON_SIZE], const struct rb_icon_art *a, int fill, bool lit)
{
    memcpy(out, a->px, sizeof a->px);
    if (!lit)
        for (int i = 0; i < a->nlight; i++)
            out[a->light[i]] = a->light_off[i];
    if (fill < 0)
        fill = 0;
    if (fill > RB_BAR_STEPS)
        fill = RB_BAR_STEPS;
    for (int y = 0; y < RB_BAR_H; y++)
        for (int x = 0; x < RB_BAR_W; x++) {
            uint32_t c;
            if (x == 0 || y == 0 || x == RB_BAR_W - 1 || y == RB_BAR_H - 1) {
                c = 0x000000;
            } else {
                int ix = x - 1, iy = y - 1, n = RB_BAR_H - 3; /* inner rows 0..n */
                if (ix < fill) {
                    c = iy == 0 ? 0xc6c4ffu : lerp_rgb(0xa5a3f7u, 0x4a4da5u, iy - 1, n - 1);
                    if (fill < RB_BAR_STEPS && ix == fill - 1)
                        c = 0x3a3d8cu;
                } else {
                    c = iy == 0 ? 0xa5a5a5u : lerp_rgb(0xcececeu, 0xf7f7f7u, iy - 1, n - 1);
                }
            }
            out[(RB_BAR_Y + y) * RB_ICON_SIZE + RB_BAR_X + x] = OPAQUE | c;
        }
}

/* ---- geometry ------------------------------------------------------------------ */

void rb_startup_geometry(struct rb_startup_geom *g, int W, int H, int s, int pixel_req, int guest_w, int guest_h)
{
    memset(g, 0, sizeof *g);
    if (W <= 0 || H <= 0)
        return;
    if (s < 1)
        s = 1;
    int gw, gh; /* a classic size turns on its side on a portrait display, as the guest target does */
    rb_guest_size_for(W, H, s, guest_w, guest_h, &gw, &gh);
    /* Exactly the guest's layout (presenter relayout(): integer, the pixel
     * scale as the largest factor when one is configured, origin on the s
     * grid). */
    rb_layout l;
    memset(&l, 0, sizeof l);
    int ix = (gw - RB_ICON_SIZE) / 2, iy = (gh - RB_ICON_SIZE) / 2;
    if (rb_layout_compute_ex(&l, W, H, gw, gh, RB_SCALING_INTEGER, pixel_req ? s : 0, s) < 0) {
        rb_layout_free(&l);
        return;
    }
    g->gx = l.dst.x;
    g->gy = l.dst.y;
    if (l.mode == RB_SCALING_INTEGER) {
        g->k = l.k;
        g->land_x = l.dst.x + ix * l.k;
        g->land_y = l.dst.y + iy * l.k;
    } else {
        /* The guest is larger than the screen: Mac OS's icon is scaled
         * down; stand at its centre at 1x. */
        g->k = 1;
        g->land_x = l.dst.x + (int)(((long)(ix + RB_ICON_SIZE / 2) * l.dst.w) / gw) - RB_ICON_SIZE / 2;
        g->land_y = l.dst.y + (int)(((long)(iy + RB_ICON_SIZE / 2) * l.dst.h) / gh) - RB_ICON_SIZE / 2;
    }
    rb_layout_free(&l);
    *g = rb_startup_geom_at(g, g->k);
}

void rb_startup_scene(uint32_t *px, int stride_px, int W, int H, rb_rect clip, int cs, const struct rb_startup_geom *g,
                      const struct rb_icon_art *a, bool indicator, int fill, bool lit)
{
    rb_startup_scene_style(px, stride_px, W, H, clip, cs, g, a, indicator, fill, lit, RB_STARTUP_CLASSIC);
}

void rb_startup_scene_style(uint32_t *px, int stride_px, int W, int H, rb_rect clip, int cs,
                            const struct rb_startup_geom *g, const struct rb_icon_art *a,
                            bool indicator, int fill, bool lit, int style)
{
    rb_rect r;
    if (!px || !rb_rect_intersect(clip, (rb_rect){0, 0, W, H}, &r))
        return;
    if (style == RB_STARTUP_OSX) {
        for (int y = r.y; y < r.y + r.h; y++)
            for (int x = r.x; x < r.x + r.w; x++)
                px[(size_t)y * stride_px + x] = RB_OSX_BOOT_GRAY;
        return;
    }
    rb_checker_fill_at(px, stride_px, r.x, r.y, r.w, r.h, cs, rb_startup_anchor(g));
    if (indicator && g->valid)
        rb_startup_draw(px, stride_px, W, H, r, g, a, fill, lit);
}

void rb_startup_panel_background(uint32_t *px, int stride_px, int W, int H, int cm, int scale, int style)
{
    if (!px)
        return;
    if (style == RB_STARTUP_OSX) {
        for (int y = 0; y < H; y++)
            for (int x = 0; x < W; x++)
                px[(size_t)y * stride_px + x] = RB_OSX_BOOT_GRAY;
    } else {
        rb_checker_fill_units(px, stride_px, 0, 0, W, H, cm, scale);
    }
}

struct rb_startup_geom rb_startup_geom_at(const struct rb_startup_geom *g, int q)
{
    struct rb_startup_geom r = *g;
    if (q < 1)
        q = 1;
    r.q = q;
    r.x = g->land_x + (RB_ICON_SIZE / 2) * g->k - (RB_ICON_SIZE / 2) * q;
    r.y = g->land_y + (RB_ICON_SIZE / 2) * g->k - (RB_ICON_SIZE / 2) * q;
    r.valid = g->k >= 1;
    return r;
}

rb_rect rb_startup_rect(const struct rb_startup_geom *g)
{
    return (rb_rect){g->x, g->y, RB_ICON_SIZE * g->q, RB_ICON_SIZE * g->q};
}

rb_rect rb_startup_bar_rect(const struct rb_startup_geom *g)
{
    return (rb_rect){g->x + (RB_BAR_X + 1) * g->q, g->y + (RB_BAR_Y + 1) * g->q, (RB_BAR_W - 2) * g->q,
                     (RB_BAR_H - 2) * g->q};
}

rb_rect rb_startup_screen_rect(const struct rb_startup_geom *g)
{
    return (rb_rect){g->x + RB_ICON_SCREEN_X * g->q, g->y + RB_ICON_SCREEN_Y * g->q, RB_ICON_SCREEN_W * g->q,
                     RB_ICON_SCREEN_H * g->q};
}

rb_rect rb_startup_light_rect(const struct rb_startup_geom *g, const struct rb_icon_art *a)
{
    if (!a->nlight)
        return (rb_rect){0, 0, 0, 0};
    int x0 = RB_ICON_SIZE, y0 = RB_ICON_SIZE, x1 = 0, y1 = 0;
    for (int i = 0; i < a->nlight; i++) {
        int x = a->light[i] % RB_ICON_SIZE, y = a->light[i] / RB_ICON_SIZE;
        x0 = x < x0 ? x : x0;
        y0 = y < y0 ? y : y0;
        x1 = x + 1 > x1 ? x + 1 : x1;
        y1 = y + 1 > y1 ? y + 1 : y1;
    }
    return (rb_rect){g->x + x0 * g->q, g->y + y0 * g->q, (x1 - x0) * g->q, (y1 - y0) * g->q};
}

int rb_startup_fill(double p)
{
    if (!(p > 0))
        return 0;
    int f = (int)floor(p * RB_BAR_STEPS + 1e-9);
    return f > RB_BAR_STEPS ? RB_BAR_STEPS : f;
}

void rb_startup_draw(uint32_t *px, int stride_px, int W, int H, rb_rect clip, const struct rb_startup_geom *g,
                     const struct rb_icon_art *a, int fill, bool lit)
{
    if (!g->valid || !px)
        return;
    rb_rect r;
    if (!rb_rect_intersect(rb_startup_rect(g), clip, &r) ||
        !rb_rect_intersect(r, (rb_rect){0, 0, W, H}, &r))
        return;
    uint32_t sprite[RB_ICON_SIZE * RB_ICON_SIZE];
    rb_icon_compose(sprite, a, fill, lit);
    int q = g->q;
    for (int y = r.y; y < r.y + r.h; y++) {
        const uint32_t *srow = sprite + ((y - g->y) / q) * RB_ICON_SIZE;
        uint32_t *drow = px + (size_t)y * stride_px;
        for (int x = r.x; x < r.x + r.w; x++) {
            uint32_t c = srow[(x - g->x) / q];
            if (c & OPAQUE)
                drow[x] = c & 0xffffffu;
        }
    }
}

void rb_startup_draw_rotated(uint32_t *fb, int stride_px, int fb_w, int fb_h, int rot,
                             const struct rb_startup_geom *g, const struct rb_icon_art *a, int fill, bool lit)
{
    if (rot != 90 && rot != 180 && rot != 270) {
        rb_startup_draw(fb, stride_px, fb_w, fb_h, (rb_rect){0, 0, fb_w, fb_h}, g, a, fill, lit);
        return;
    }
    if (!g->valid || !fb)
        return;
    int ow = rot == 180 ? fb_w : fb_h, oh = rot == 180 ? fb_h : fb_w;
    uint32_t sprite[RB_ICON_SIZE * RB_ICON_SIZE];
    rb_icon_compose(sprite, a, fill, lit);
    rb_rect r;
    if (!rb_rect_intersect(rb_startup_rect(g), (rb_rect){0, 0, ow, oh}, &r))
        return;
    for (int ly = r.y; ly < r.y + r.h; ly++)
        for (int lx = r.x; lx < r.x + r.w; lx++) {
            uint32_t c = sprite[((ly - g->y) / g->q) * RB_ICON_SIZE + (lx - g->x) / g->q];
            if (!(c & OPAQUE))
                continue;
            int fx, fy;
            switch (rot) {
            case 90: fx = oh - 1 - ly; fy = lx; break;
            case 180: fx = ow - 1 - lx; fy = oh - 1 - ly; break;
            default: fx = ly; fy = ow - 1 - lx; break; /* 270 */
            }
            if (fx >= 0 && fy >= 0 && fx < fb_w && fy < fb_h)
                fb[(size_t)fy * stride_px + fx] = c & 0xffffffu;
        }
}

/* ---- DPI-aware checkerboard ------------------------------------------------------- */

int rb_checker_multiple(int W, int H, int s, int mm_w, int mm_h)
{
    if (s < 1)
        s = 1;
    if (W <= 0 || H <= 0 || mm_w <= 0 || mm_h <= 0)
        return 1;
    /* EDIDs sometimes carry an aspect ratio (16 x 9 "cm") or nonsense instead
     * of a size: the physical aspect must match the mode's (either way round)
     * and the density must be one a real display can have. */
    double ap = (double)W / H, am = (double)mm_w / mm_h;
    double r1 = ap / am, r2 = ap * am;
    if (!((r1 > 0.85 && r1 < 1.18) || (r2 > 0.85 && r2 < 1.18)))
        return 1;
    double dpi = sqrt((double)W * W + (double)H * H) * 25.4 / sqrt((double)mm_w * mm_w + (double)mm_h * mm_h);
    if (dpi < 40.0 || dpi > 800.0)
        return 1;
    int best = 1;
    double best_d = 1e9;
    for (int m = 1; m <= 8; m++) {
        double e = dpi / (s * m);
        double d = e < 72.0 ? 72.0 - e : e > 110.0 ? e - 110.0 : 0.0;
        if (d < best_d - 1e-9) {
            best_d = d;
            best = m;
        }
    }
    return best;
}

static bool read_line(const char *dir, const char *name, char *out, size_t n)
{
    char path[512];
    snprintf(path, sizeof path, "%s/%s", dir, name);
    FILE *f = fopen(path, "re");
    if (!f)
        return false;
    bool ok = fgets(out, (int)n, f) != NULL;
    fclose(f);
    if (ok)
        out[strcspn(out, "\r\n")] = 0;
    return ok;
}

/* Built-in panels that report no physical size. Keep this tiny: DRM, EDID
 * and the firmware tables come first; an entry is only for a machine
 * RetroBoot has qualified (vendor and product exactly as in SMBIOS). */
static const struct {
    const char *vendor, *product;
    int w, h;       /* panel resolution (landscape) */
    int mm_w, mm_h; /* landscape */
} panel_quirks[] = {
    /* GPD MicroPC (2019): 6.0" 1280 x 720, scanned out as 720 x 1280. */
    {"GPD", "MicroPC", 1280, 720, 133, 75},
};

static bool mm_override(int *mm_w, int *mm_h)
{
    const char *ov = getenv("RB_PANEL_MM");
    int a = 0, b = 0;
    if (!ov || sscanf(ov, "%dx%d", &a, &b) != 2 || a <= 0 || b <= 0)
        return false;
    *mm_w = a;
    *mm_h = b;
    return true;
}

bool rb_panel_mm_quirk(const char *dmi_dir, int W, int H, int *mm_w, int *mm_h)
{
    if (mm_override(mm_w, mm_h))
        return true;
    char vendor[128], product[128];
    const char *dir = dmi_dir ? dmi_dir : "/sys/class/dmi/id";
    if (!read_line(dir, "sys_vendor", vendor, sizeof vendor) ||
        !read_line(dir, "product_name", product, sizeof product))
        return false;
    for (size_t i = 0; i < sizeof panel_quirks / sizeof panel_quirks[0]; i++) {
        if (strcmp(vendor, panel_quirks[i].vendor) || strcmp(product, panel_quirks[i].product))
            continue;
        bool landscape = W == panel_quirks[i].w && H == panel_quirks[i].h;
        bool portrait = W == panel_quirks[i].h && H == panel_quirks[i].w;
        if (!landscape && !portrait)
            continue;
        *mm_w = landscape ? panel_quirks[i].mm_w : panel_quirks[i].mm_h;
        *mm_h = landscape ? panel_quirks[i].mm_h : panel_quirks[i].mm_w;
        return true;
    }
    return false;
}

bool rb_connector_internal(uint32_t conn_type)
{
    return conn_type == DRM_MODE_CONNECTOR_LVDS || conn_type == DRM_MODE_CONNECTOR_eDP ||
           conn_type == DRM_MODE_CONNECTOR_DSI || conn_type == DRM_MODE_CONNECTOR_DPI;
}

const char *rb_display_mm(uint32_t conn_type, int w, int h, int mm_w, int mm_h, int *out_w, int *out_h)
{
    *out_w = *out_h = 0;
    if (mm_override(out_w, out_h)) /* lab: RB_PANEL_MM=WxH for any connector */
        return "override";
    bool internal = rb_connector_internal(conn_type);
    /* External displays smaller than 10" are EDIDs that store an aspect
     * ratio or nonsense (some TVs and capture devices): unknown. */
    if (mm_w > 0 && mm_h > 0 && !internal && (long)mm_w * mm_w + (long)mm_h * mm_h < 254L * 254L)
        return NULL;
    if (mm_w > 0 && mm_h > 0) {
        *out_w = mm_w;
        *out_h = mm_h;
        return "drm";
    }
    if (internal && rb_panel_mm_quirk(NULL, w, h, out_w, out_h))
        return "quirk";
    return NULL;
}

/* ---- progress model ------------------------------------------------------------------ */

double rb_progress_at(const struct rb_progress *pr, uint64_t t)
{
    double p0 = pr->p0;
    if (!pr->timed || t <= pr->t0 || p0 >= RB_PROGRESS_CAP)
        return p0;
    double dt = (double)(t - pr->t0);
    if (p0 < RB_PROGRESS_KNEE && pr->eta > pr->t0) {
        double x = dt / (double)(pr->eta - pr->t0);
        if (x <= 1.0)
            return p0 + (RB_PROGRESS_KNEE - p0) * x;
        double a = (RB_PROGRESS_KNEE - p0) / (RB_PROGRESS_CAP - RB_PROGRESS_KNEE);
        return RB_PROGRESS_CAP - (RB_PROGRESS_CAP - RB_PROGRESS_KNEE) * exp(-(x - 1.0) * a);
    }
    /* Overdue (or already past the knee): approach the cap slowly. */
    return RB_PROGRESS_CAP - (RB_PROGRESS_CAP - p0) * exp(-dt / RB_PROGRESS_TAU_MS);
}

void rb_progress_reset(struct rb_progress *pr, uint64_t now, uint64_t eta)
{
    pr->timed = eta != 0;
    pr->t0 = now;
    pr->p0 = 0.0;
    pr->eta = eta;
}

void rb_progress_retarget(struct rb_progress *pr, uint64_t now, uint64_t eta)
{
    double p = rb_progress_at(pr, now);
    if (pr->timed && now < pr->t0)
        now = pr->t0;
    pr->timed = true;
    pr->t0 = now;
    pr->p0 = p;
    pr->eta = eta;
}

static int steps_at(const struct rb_progress *pr, uint64_t t, int steps)
{
    double p = rb_progress_at(pr, t);
    int f = p > 0 ? (int)floor(p * steps + 1e-9) : 0;
    return f > steps ? steps : f;
}

uint64_t rb_progress_next_step(const struct rb_progress *pr, uint64_t now, int steps, uint64_t horizon)
{
    uint64_t hi = now + horizon;
    if (!pr->timed)
        return hi;
    int cur = steps_at(pr, now, steps);
    if (steps_at(pr, hi, steps) <= cur)
        return hi;
    uint64_t lo = now;
    while (hi - lo > 1) {
        uint64_t mid = lo + (hi - lo) / 2;
        if (steps_at(pr, mid, steps) > cur)
            hi = mid;
        else
            lo = mid;
    }
    return hi;
}

/* ---- kernel command line ------------------------------------------------------------- */

/* Value of the last `key=` token (key includes the '='), or NULL. */
static const char *cmdline_value(const char *cmdline, const char *key, size_t *len)
{
    const char *found = NULL;
    size_t kn = strlen(key);
    const char *p = cmdline;
    while (p && *p) {
        while (*p == ' ' || *p == '\t' || *p == '\n')
            p++;
        const char *end = p;
        while (*end && *end != ' ' && *end != '\t' && *end != '\n')
            end++;
        if ((size_t)(end - p) >= kn && !strncmp(p, key, kn)) {
            found = p + kn;
            *len = (size_t)(end - found);
        }
        p = end;
    }
    return found;
}

static bool parse_uint(const char *v, size_t n, unsigned long long max, unsigned long long *out)
{
    if (!n || n > 20)
        return false;
    unsigned long long x = 0;
    for (size_t i = 0; i < n; i++) {
        if (v[i] < '0' || v[i] > '9')
            return false;
        x = x * 10 + (unsigned)(v[i] - '0');
        if (x > max)
            return false;
    }
    *out = x;
    return true;
}

void rb_cmdline_startup(const char *cmdline, struct rb_boot_startup *o)
{
    memset(o, 0, sizeof *o);
    o->progress = -1;
    size_t n;
    const char *v;
    if ((v = cmdline_value(cmdline, "retroboot.startup_style=", &n)) && n == 6 && !strncmp(v, "macosx", 6))
        o->style = RB_STARTUP_OSX;
    if ((v = cmdline_value(cmdline, "retroboot.progress=", &n))) {
        if ((n == 2 && !strncmp(v, "on", 2)) || (n == 1 && v[0] == '1'))
            o->progress = 1;
        else if ((n == 3 && !strncmp(v, "off", 3)) || (n == 1 && v[0] == '0'))
            o->progress = 0;
    }
    if ((v = cmdline_value(cmdline, "retroboot.guest_size=", &n))) {
        char buf[32];
        int w = 0, h = 0;
        if (n < sizeof buf) {
            memcpy(buf, v, n);
            buf[n] = 0;
            char tail;
            if (sscanf(buf, "%dx%d%c", &w, &h, &tail) == 2 && w >= RB_GUEST_MIN_W && h >= RB_GUEST_MIN_H &&
                w <= RB_GUEST_MAX_W && h <= RB_GUEST_MAX_H) {
                o->guest_w = w;
                o->guest_h = h;
            }
        }
    }
    unsigned long long x;
    if ((v = cmdline_value(cmdline, "retroboot.checker_scale=", &n)) && parse_uint(v, n, 64, &x) && x >= 1)
        o->checker_scale = (int)x;
    if ((v = cmdline_value(cmdline, "retroboot.startup_ms=", &n)) && parse_uint(v, n, 3600000ull, &x) && x >= 1)
        o->startup_ms = x;
}

void rb_boot_startup(const char *path, struct rb_boot_startup *o)
{
    char buf[4096];
    FILE *f = fopen(path ? path : "/proc/cmdline", "re");
    size_t n = 0;
    if (f) {
        n = fread(buf, 1, sizeof buf - 1, f);
        fclose(f);
    }
    buf[n] = 0;
    rb_cmdline_startup(buf, o);
}
