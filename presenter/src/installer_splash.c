/* RetroBoot's installer startup screen (installer_splash.h, ARCHITECTURE.md
 * 4.4.2).
 *
 * The exact art -- Mac OS 9.2's startup box, its Charcoal status line and
 * Appearance progress bar, with "RetroBoot" set in Apple Garamond where Mac
 * OS has its name -- is private (D012): rbplatinum builds it from the user's
 * own disk into installer-startup.rbis and this file only reads it. The
 * built-in art below is RetroBoot's own (October 2026, MIT, like
 * presenter/ui's art): the box's geometry drawn as plain bevels, RetroBoot's
 * compact Mac (startup.c) at 3x with a checkerboard screen, the MIT RetroBoot
 * System 12 font (isplash_font.h) and RetroBoot's bar colours (startup.c).
 * No Apple bitmap was copied or traced.
 */
#include "installer_splash.h"

#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <unistd.h>

#include "isplash_font.h"
#include "log.h"
#include "loop.h"
#include "scale.h"
#include "startup.h"

#define BAR_IN_H (RB_ISPLASH_BAR_H - 4) /* interior rows */
#define RBIS_MAX (4u << 20)

const char *rb_isplash_source_name(int source) { return source == RB_ISPLASH_MACOS ? "macos" : "builtin"; }

/* ---- built-in art ----------------------------------------------------------- */

static const struct rb_isplash_glyph *glyph(uint32_t cp)
{
    for (size_t i = 0; i < sizeof rb_isplash_font / sizeof rb_isplash_font[0]; i++)
        if (rb_isplash_font[i].code == cp)
            return &rb_isplash_font[i];
    return NULL;
}

static uint32_t utf8_next(const char **s)
{
    const unsigned char *p = (const unsigned char *)*s;
    uint32_t cp = p[0];
    int n = 1;
    if (cp >= 0xf0 && p[1] && p[2] && p[3]) {
        cp = (cp & 7u) << 18 | (p[1] & 63u) << 12 | (p[2] & 63u) << 6 | (p[3] & 63u);
        n = 4;
    } else if (cp >= 0xe0 && p[1] && p[2]) {
        cp = (cp & 15u) << 12 | (p[1] & 63u) << 6 | (p[2] & 63u);
        n = 3;
    } else if (cp >= 0xc0 && p[1]) {
        cp = (cp & 31u) << 6 | (p[1] & 63u);
        n = 2;
    }
    *s += n;
    return cp;
}

/* Advance width and ink extent (first and last inked column) of a line. */
static int text_width(const char *s, int *ink0, int *ink1)
{
    int pen = 0, a = 1 << 30, b = -(1 << 30);
    while (*s) {
        const struct rb_isplash_glyph *g = glyph(utf8_next(&s));
        if (!g)
            continue;
        if (g->w) {
            a = pen + g->xoff < a ? pen + g->xoff : a;
            b = pen + g->xoff + g->w - 1 > b ? pen + g->xoff + g->w - 1 : b;
        }
        pen += g->adv;
    }
    if (ink0)
        *ink0 = a;
    if (ink1)
        *ink1 = b;
    return pen;
}

/* Draw text at scale q with its pen at x and the baseline (the first row
 * below standing glyphs) at y, into a w x h buffer. */
static void draw_text(uint32_t *buf, int w, int h, int x, int y, int q, const char *s, uint32_t colour)
{
    int pen = x;
    while (*s) {
        const struct rb_isplash_glyph *g = glyph(utf8_next(&s));
        if (!g)
            continue;
        int top = y - (g->yoff + g->h) * q;
        for (int r = 0; r < g->h; r++)
            for (int c = 0; c < g->w; c++) {
                if (!(g->rows[r] & (0x80u >> c)))
                    continue;
                for (int dy = 0; dy < q; dy++)
                    for (int dx = 0; dx < q; dx++) {
                        int px = pen + (g->xoff + c) * q + dx, py = top + r * q + dy;
                        if (px >= 0 && py >= 0 && px < w && py < h)
                            buf[py * w + px] = colour;
                    }
            }
        pen += g->adv * q;
    }
}

static void fill(uint32_t *buf, int w, int x, int y, int rw, int rh, uint32_t c)
{
    for (int yy = y; yy < y + rh; yy++)
        for (int xx = x; xx < x + rw; xx++)
            buf[yy * w + xx] = c;
}

static uint32_t lerp(uint32_t a, uint32_t b, int t, int n)
{
    uint32_t out = 0;
    for (int sh = 0; sh <= 16; sh += 8) {
        int ca = (int)((a >> sh) & 255u), cb = (int)((b >> sh) & 255u);
        out |= (uint32_t)(ca + (cb - ca) * t / (n > 0 ? n : 1)) << sh;
    }
    return out;
}

/* A raised box with a recessed white panel (the startup box's geometry). */
static void builtin_box(uint32_t *b)
{
    const int W = RB_ISPLASH_BOX_W, H = RB_ISPLASH_BOX_H;
    fill(b, W, 0, 0, W, H, 0x000000);
    fill(b, W, 1, 1, W - 2, H - 2, 0xdddddd);
    fill(b, W, 1, 1, W - 2, 1, 0xffffff);
    fill(b, W, 1, 1, 1, H - 2, 0xffffff);
    fill(b, W, 2, H - 2, W - 3, 1, 0xaaaaaa);
    fill(b, W, W - 2, 2, 1, H - 3, 0xaaaaaa);
    const int px = RB_ISPLASH_PANEL_X, py = RB_ISPLASH_PANEL_Y, pw = RB_ISPLASH_PANEL_W, ph = RB_ISPLASH_PANEL_H;
    fill(b, W, px - 1, py - 1, pw + 1, 1, 0xaaaaaa); /* the well: shadow above and left */
    fill(b, W, px - 1, py - 1, 1, ph + 1, 0xaaaaaa);
    fill(b, W, px, py + ph, pw + 1, 1, 0xffffff); /* ... light below and right */
    fill(b, W, px + pw, py, 1, ph + 1, 0xffffff);
    fill(b, W, px, py, pw, ph, 0x000000);
    fill(b, W, px + 1, py + 1, pw - 2, ph - 2, 0xffffff);
    /* RetroBoot's compact Mac at 3x where Mac OS has its face, with the
     * checkerboard on its screen. */
    struct rb_icon_art icon;
    rb_icon_art_builtin(&icon);
    const int q = 3, ix = RB_ISPLASH_NAME_CX - RB_ICON_SIZE * q / 2, iy = 54;
    for (int y = 0; y < RB_ICON_SIZE; y++)
        for (int x = 0; x < RB_ICON_SIZE; x++) {
            uint32_t c = icon.px[y * RB_ICON_SIZE + x];
            if (!(c & 0xff000000u))
                continue;
            if (x >= RB_ICON_SCREEN_X && x < RB_ICON_SCREEN_X + RB_ICON_SCREEN_W && y >= RB_ICON_SCREEN_Y &&
                y < RB_ICON_SCREEN_Y + RB_ICON_SCREEN_H)
                c = ((x + y) & 1) ? 0xffffff : 0x000000;
            fill(b, W, ix + x * q, iy + y * q, q, q, c & 0xffffffu);
        }
}

/* RetroBoot's name at 4x in RetroBoot System 12, centred where Mac OS's own
 * name stands. */
static void builtin_name(uint32_t *b)
{
    const int q = 4;
    int ink0, ink1;
    text_width("RetroBoot", &ink0, &ink1);
    int x = RB_ISPLASH_NAME_CX - ((ink0 + ink1 + 1) * q) / 2;
    draw_text(b, RB_ISPLASH_BOX_W, RB_ISPLASH_BOX_H, x, RB_ISPLASH_NAME_BASELINE, q, "RetroBoot", 0x000000);
}

/* Status lines are centred as Mac OS centres its own: "Welcome to Mac OS"
 * one pixel left of the box's centre, "Starting Up..." on it. */
static int status_pen(int phase, int adv)
{
    return phase == RB_ISPLASH_WELCOME ? (RB_ISPLASH_BOX_W - 2 - adv) / 2 : (RB_ISPLASH_BOX_W - adv) / 2;
}

static int builtin_status(struct rb_isplash_strip *st, int phase, const uint32_t *box)
{
    st->x = RB_ISPLASH_STATUS_X;
    st->y = RB_ISPLASH_STATUS_Y;
    st->w = RB_ISPLASH_STATUS_W;
    st->h = RB_ISPLASH_STATUS_H;
    st->px = malloc((size_t)st->w * st->h * 4);
    if (!st->px)
        return -1;
    for (int y = 0; y < st->h; y++)
        memcpy(st->px + y * st->w, box + (st->y + y) * RB_ISPLASH_BOX_W + st->x, (size_t)st->w * 4);
    const char *text = phase == RB_ISPLASH_WELCOME ? RB_ISPLASH_WELCOME_TEXT : RB_ISPLASH_STARTING_TEXT;
    int adv = text_width(text, NULL, NULL);
    draw_text(st->px, st->w, st->h, status_pen(phase, adv) - st->x, RB_ISPLASH_STATUS_BASELINE - st->y, 1, text,
              0x000000);
    return 0;
}

/* RetroBoot's own Platinum bar (startup.c's colours): black frame in an
 * etch, recessed light track, lavender fill closed by a dark line. */
static void builtin_bar(uint32_t *e, uint32_t *f, uint32_t *cap)
{
    const int W = RB_ISPLASH_BAR_W, H = RB_ISPLASH_BAR_H;
    fill(e, W, 0, 0, W, H, 0x888888);
    fill(e, W, 1, H - 1, W - 1, 1, 0xffffff);
    fill(e, W, W - 1, 1, 1, H - 1, 0xffffff);
    fill(e, W, 1, 1, W - 2, H - 2, 0x000000);
    memcpy(f, e, (size_t)W * H * 4);
    for (int j = 0; j < BAR_IN_H; j++) {
        uint32_t track = j == 0 ? 0xa5a5a5u : lerp(0xcececeu, 0xf7f7f7u, j - 1, BAR_IN_H - 2);
        uint32_t full = j == 0 ? 0xc6c4ffu : lerp(0xa5a3f7u, 0x4a4da5u, j - 1, BAR_IN_H - 2);
        fill(e, W, 2, 2 + j, W - 4, 1, track);
        fill(f, W, 2, 2 + j, W - 4, 1, full);
        /* the end of a fill: two fill columns, the dark line, then track */
        uint32_t row[RB_ISPLASH_CAP_W] = {full, full, 0x3a3d8cu, track, track, track};
        memcpy(cap + j * RB_ISPLASH_CAP_W, row, sizeof row);
    }
}

void rb_isplash_art_free(struct rb_isplash_art *a)
{
    free(a->box);
    free(a->status[0].px);
    free(a->status[1].px);
    free(a->bar_empty);
    free(a->bar_full);
    free(a->cap);
    memset(a, 0, sizeof *a);
}

static int alloc_art(struct rb_isplash_art *a)
{
    memset(a, 0, sizeof *a);
    a->box = malloc((size_t)RB_ISPLASH_BOX_W * RB_ISPLASH_BOX_H * 4);
    a->bar_empty = malloc((size_t)RB_ISPLASH_BAR_W * RB_ISPLASH_BAR_H * 4);
    a->bar_full = malloc((size_t)RB_ISPLASH_BAR_W * RB_ISPLASH_BAR_H * 4);
    a->cap = malloc((size_t)RB_ISPLASH_CAP_W * BAR_IN_H * 4);
    if (!a->box || !a->bar_empty || !a->bar_full || !a->cap) {
        rb_isplash_art_free(a);
        return -1;
    }
    return 0;
}

int rb_isplash_art_builtin(struct rb_isplash_art *a)
{
    if (alloc_art(a) < 0)
        return -1;
    builtin_box(a->box);
    builtin_name(a->box);
    builtin_bar(a->bar_empty, a->bar_full, a->cap);
    if (builtin_status(&a->status[0], RB_ISPLASH_WELCOME, a->box) < 0 ||
        builtin_status(&a->status[1], RB_ISPLASH_STARTING, a->box) < 0) {
        rb_isplash_art_free(a);
        return -1;
    }
    a->source = RB_ISPLASH_BUILTIN;
    return 0;
}

/* ---- the private art file --------------------------------------------------------- */

static uint16_t le16(const uint8_t *p) { return (uint16_t)(p[0] | p[1] << 8); }
static uint32_t le32(const uint8_t *p) { return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24; }

struct rbis_entry {
    bool found;
    int x, y, w, h;
    const uint8_t *rgba;
};

static int rbis_find(const uint8_t *d, size_t n, const char tag[4], struct rbis_entry *e)
{
    memset(e, 0, sizeof *e);
    unsigned count = le16(d + 6);
    for (unsigned i = 0; i < count; i++) {
        const uint8_t *p = d + 8 + (size_t)i * 16;
        if (memcmp(p, tag, 4))
            continue;
        uint32_t off = le32(p + 8), len = le32(p + 12);
        if (off > n || len > n - off || len < 8 || memcmp(d + off, "RBI1", 4))
            return -1;
        int w = le16(d + off + 4), h = le16(d + off + 6);
        if (w <= 0 || h <= 0 || (size_t)len != 8 + (size_t)w * h * 4)
            return -1;
        e->found = true;
        e->x = (int16_t)le16(p + 4);
        e->y = (int16_t)le16(p + 6);
        e->w = w;
        e->h = h;
        e->rgba = d + off + 8;
        return 0;
    }
    return 0;
}

static void rgba_to_xrgb(uint32_t *dst, int dst_stride, const struct rbis_entry *e)
{
    for (int y = 0; y < e->h; y++)
        for (int x = 0; x < e->w; x++) {
            const uint8_t *p = e->rgba + ((size_t)y * e->w + x) * 4;
            dst[y * dst_stride + x] = (uint32_t)p[0] << 16 | (uint32_t)p[1] << 8 | p[2];
        }
}

static bool inside_box(const struct rbis_entry *e)
{
    return e->x >= 0 && e->y >= 0 && e->x + e->w <= RB_ISPLASH_BOX_W && e->y + e->h <= RB_ISPLASH_BOX_H;
}

int rb_isplash_art_from_rbis(struct rb_isplash_art *a, const uint8_t *d, size_t n)
{
    memset(a, 0, sizeof *a);
    if (!d || n < 8 || n > RBIS_MAX || memcmp(d, "RBIS", 4) || le16(d + 4) != 1 || 8 + (size_t)le16(d + 6) * 16 > n)
        return -1;
    struct rbis_entry box, word, st[2], be, bf, bc;
    if (rbis_find(d, n, "BOX ", &box) < 0 || rbis_find(d, n, "WORD", &word) < 0 ||
        rbis_find(d, n, "WELC", &st[0]) < 0 || rbis_find(d, n, "STRT", &st[1]) < 0 ||
        rbis_find(d, n, "BARE", &be) < 0 || rbis_find(d, n, "BARF", &bf) < 0 || rbis_find(d, n, "BCAP", &bc) < 0)
        return -1;
    if (!box.found || box.w != RB_ISPLASH_BOX_W || box.h != RB_ISPLASH_BOX_H || !st[0].found || !st[1].found ||
        !inside_box(&st[0]) || !inside_box(&st[1]) || !be.found || !bf.found || !bc.found ||
        be.w != RB_ISPLASH_BAR_W || be.h != RB_ISPLASH_BAR_H || bf.w != RB_ISPLASH_BAR_W ||
        bf.h != RB_ISPLASH_BAR_H || bc.w != RB_ISPLASH_CAP_W || bc.h != BAR_IN_H || (word.found && !inside_box(&word)))
        return -1;
    if (alloc_art(a) < 0)
        return -1;
    rgba_to_xrgb(a->box, RB_ISPLASH_BOX_W, &box);
    if (word.found)
        rgba_to_xrgb(a->box + word.y * RB_ISPLASH_BOX_W + word.x, RB_ISPLASH_BOX_W, &word);
    else
        builtin_name(a->box);
    a->named = word.found;
    for (int i = 0; i < 2; i++) {
        struct rb_isplash_strip *s = &a->status[i];
        s->x = st[i].x;
        s->y = st[i].y;
        s->w = st[i].w;
        s->h = st[i].h;
        s->px = malloc((size_t)s->w * s->h * 4);
        if (!s->px) {
            rb_isplash_art_free(a);
            return -1;
        }
        rgba_to_xrgb(s->px, s->w, &st[i]);
    }
    rgba_to_xrgb(a->bar_empty, RB_ISPLASH_BAR_W, &be);
    rgba_to_xrgb(a->bar_full, RB_ISPLASH_BAR_W, &bf);
    rgba_to_xrgb(a->cap, RB_ISPLASH_CAP_W, &bc);
    a->source = RB_ISPLASH_MACOS;
    return 0;
}

static uint8_t *read_file(const char *path, size_t max, size_t *n)
{
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0)
        return NULL;
    struct stat st;
    uint8_t *buf = NULL;
    if (fstat(fd, &st) == 0 && S_ISREG(st.st_mode) && st.st_size > 0 && (size_t)st.st_size <= max &&
        (buf = malloc((size_t)st.st_size))) {
        size_t got = 0;
        while (got < (size_t)st.st_size) {
            ssize_t r = read(fd, buf + got, (size_t)st.st_size - got);
            if (r <= 0)
                break;
            got += (size_t)r;
        }
        if (got != (size_t)st.st_size) {
            free(buf);
            buf = NULL;
        }
        *n = got;
    }
    close(fd);
    return buf;
}

int rb_isplash_art_load(struct rb_isplash_art *a, const char *path)
{
    size_t n = 0;
    uint8_t *d = path && path[0] ? read_file(path, RBIS_MAX, &n) : NULL;
    int rc = d ? rb_isplash_art_from_rbis(a, d, n) : -1;
    free(d);
    if (rc == 0)
        return a->source;
    return rb_isplash_art_builtin(a) == 0 ? a->source : -1;
}

/* ---- geometry and drawing ---------------------------------------------------------- */

void rb_isplash_box_origin(int w, int h, int *bx, int *by)
{
    int dx = w - RB_ISPLASH_BOX_W, dy = h - RB_ISPLASH_BOX_H;
    *bx = dx > 0 ? dx / 2 : 0;
    *by = dy > 0 ? (dy * 3 + 5) / 10 : 0; /* 3/10 down, halves up: 768 -> 134, 480 -> 47 */
}

void rb_isplash_geometry(struct rb_isplash_geom *g, int W, int H, int s, int pixel_req, int guest_w, int guest_h)
{
    memset(g, 0, sizeof *g);
    if (W <= 0 || H <= 0)
        return;
    if (s < 1)
        s = 1;
    struct rb_startup_geom sg;
    rb_startup_geometry(&sg, W, H, s, pixel_req, guest_w, guest_h);
    int gw, gh;
    rb_guest_size_for(W, H, s, guest_w, guest_h, &gw, &gh);
    if (!sg.valid || sg.k < 1)
        return;
    int k = sg.k;
    if (gw * k > W || gh * k > H) {
        /* The guest is larger than the frame (aspect fit): Mac OS's rule on
         * the frame itself, at 1x. */
        k = 1;
        rb_isplash_box_origin(W, H, &g->bx, &g->by);
        g->x = g->bx;
        g->y = g->by;
    } else {
        rb_isplash_box_origin(gw, gh, &g->bx, &g->by);
        g->x = sg.gx + g->bx * k;
        g->y = sg.gy + g->by * k;
    }
    g->k = k;
    g->valid = true;
}

rb_rect rb_isplash_rect(const struct rb_isplash_geom *g)
{
    return (rb_rect){g->x, g->y, RB_ISPLASH_BOX_W * g->k, RB_ISPLASH_BOX_H * g->k};
}

rb_rect rb_isplash_status_rect(const struct rb_isplash_geom *g)
{
    return (rb_rect){g->x + RB_ISPLASH_STATUS_X * g->k, g->y + RB_ISPLASH_STATUS_Y * g->k,
                     RB_ISPLASH_STATUS_W * g->k, RB_ISPLASH_STATUS_H * g->k};
}

rb_rect rb_isplash_bar_rect(const struct rb_isplash_geom *g)
{
    return (rb_rect){g->x + RB_ISPLASH_BAR_X * g->k, g->y + RB_ISPLASH_BAR_Y * g->k, RB_ISPLASH_BAR_W * g->k,
                     RB_ISPLASH_BAR_H * g->k};
}

int rb_isplash_fill(double v)
{
    if (!(v > 0))
        v = 0;
    int f = (int)lround(v * RB_ISPLASH_BAR_STEPS);
    if (f < RB_ISPLASH_BAR_MIN)
        f = RB_ISPLASH_BAR_MIN;
    return f > RB_ISPLASH_BAR_STEPS ? RB_ISPLASH_BAR_STEPS : f;
}

void rb_isplash_compose(uint32_t *out, const struct rb_isplash_art *a, int phase, int fill_cols)
{
    memcpy(out, a->box, (size_t)RB_ISPLASH_BOX_W * RB_ISPLASH_BOX_H * 4);
    const struct rb_isplash_strip *st = &a->status[phase == RB_ISPLASH_STARTING];
    if (st->px)
        for (int y = 0; y < st->h; y++)
            memcpy(out + (st->y + y) * RB_ISPLASH_BOX_W + st->x, st->px + y * st->w, (size_t)st->w * 4);
    if (phase != RB_ISPLASH_STARTING)
        return;
    int n = fill_cols < RB_ISPLASH_BAR_MIN ? RB_ISPLASH_BAR_MIN : fill_cols;
    n = n > RB_ISPLASH_BAR_STEPS ? RB_ISPLASH_BAR_STEPS : n;
    const int cap0 = 2 + n - 3; /* the fill's end: its separator lands at column 2 + n */
    for (int y = 0; y < RB_ISPLASH_BAR_H; y++) {
        uint32_t *row = out + (RB_ISPLASH_BAR_Y + y) * RB_ISPLASH_BOX_W + RB_ISPLASH_BAR_X;
        for (int x = 0; x < RB_ISPLASH_BAR_W; x++) {
            const uint32_t *src = a->bar_empty + y * RB_ISPLASH_BAR_W + x;
            if (y >= 2 && y < RB_ISPLASH_BAR_H - 2 && x >= 2 && x < RB_ISPLASH_BAR_W - 2) {
                if (x < cap0)
                    src = a->bar_full + y * RB_ISPLASH_BAR_W + x;
                else if (x < cap0 + RB_ISPLASH_CAP_W)
                    src = a->cap + (y - 2) * RB_ISPLASH_CAP_W + (x - cap0);
            }
            row[x] = *src;
        }
    }
}

void rb_isplash_blit(uint32_t *px, int stride_px, int W, int H, rb_rect clip, const struct rb_isplash_geom *g,
                     const uint32_t *composed)
{
    rb_rect r;
    if (!px || !composed || !g->valid || !rb_rect_intersect(rb_isplash_rect(g), clip, &r) ||
        !rb_rect_intersect(r, (rb_rect){0, 0, W, H}, &r))
        return;
    const int k = g->k;
    for (int y = r.y; y < r.y + r.h; y++) {
        const uint32_t *srow = composed + ((y - g->y) / k) * RB_ISPLASH_BOX_W;
        uint32_t *drow = px + (size_t)y * stride_px;
        if (k == 1) {
            memcpy(drow + r.x, srow + (r.x - g->x), (size_t)r.w * 4);
            continue;
        }
        for (int x = r.x; x < r.x + r.w; x++)
            drow[x] = srow[(x - g->x) / k];
    }
}

/* ---- progress ------------------------------------------------------------------------- */

/* boot-diag's stages (contract owned by codex/boot-diag). */
static const int stage_permille[] = {50, 100, 250, 350, 450, 550, 650, 800, 1000};

int rb_isplash_next_stage(int permille)
{
    for (size_t i = 0; i < sizeof stage_permille / sizeof stage_permille[0]; i++)
        if (stage_permille[i] > permille)
            return stage_permille[i];
    return 1000;
}

int rb_isplash_parse_progress(const char *line, size_t n, int *permille, char *stage, size_t stage_n)
{
    size_t i = 0;
    int v = 0, digits = 0;
    while (i < n && line[i] >= '0' && line[i] <= '9' && digits < 5) {
        v = v * 10 + (line[i++] - '0');
        digits++;
    }
    if (!digits || v > 1000 || i >= n || line[i] != ' ')
        return -1;
    i++;
    size_t s0 = i;
    while (i < n && line[i] != ' ' && line[i] != '\n' && line[i] != '\0')
        i++;
    if (i == s0)
        return -1;
    if (stage && stage_n) {
        size_t len = i - s0 < stage_n - 1 ? i - s0 : stage_n - 1;
        memcpy(stage, line + s0, len);
        stage[len] = 0;
    }
    *permille = v;
    return 0;
}

void rb_isplash_progress_update(struct rb_isplash_progress *p, int permille, uint64_t now)
{
    if (permille < 0 || permille > 1000 || (p->have && permille <= p->permille))
        return;
    p->have = true;
    p->permille = permille;
    p->t_stage = now;
}

double rb_isplash_goal(const struct rb_isplash_progress *p, uint64_t now)
{
    if (p->have) {
        if (p->permille >= 1000)
            return 1.0;
        double at = p->permille / 1000.0, next = rb_isplash_next_stage(p->permille) / 1000.0;
        double dt = now > p->t_stage ? (double)(now - p->t_stage) : 0.0;
        return at + (next - at) * 0.5 * (1.0 - exp(-dt / RB_ISPLASH_CREEP_MS));
    }
    /* No progress file: an estimate on the kernel's clock. */
    double t = (double)now, knee = 0.85, cap = 0.95;
    if (t <= RB_ISPLASH_EXPECT_MS)
        return knee * t / RB_ISPLASH_EXPECT_MS;
    double slope = knee / RB_ISPLASH_EXPECT_MS;
    return cap - (cap - knee) * exp(-(t - RB_ISPLASH_EXPECT_MS) * slope / (cap - knee));
}

double rb_isplash_advance(struct rb_isplash_progress *p, uint64_t now)
{
    if (now <= p->t_shown) /* clock not advanced (or older): nothing moves */
        return p->shown;
    double g = rb_isplash_goal(p, now);
    double tau = p->have && p->permille >= 1000 ? RB_ISPLASH_TAU_READY_MS : RB_ISPLASH_TAU_MS;
    double dt = (double)(now - p->t_shown);
    if (g > p->shown) {
        double v = p->shown + (g - p->shown) * (1.0 - exp(-dt / tau));
        if (g - v < 0.5 / RB_ISPLASH_BAR_STEPS / 4) /* close enough: settle (no endless tail) */
            v = g;
        p->shown = v > 1.0 ? 1.0 : v;
    }
    p->t_shown = now;
    return p->shown;
}

/* ---- one process's screen ----------------------------------------------------------------- */

bool rb_isplash_installer_mode(const char *cmdline_path)
{
    const char *ov = getenv("RB_INSTALLER_SPLASH");
    if (ov && (ov[0] == '0' || ov[0] == '1'))
        return ov[0] == '1';
    size_t n = 0;
    uint8_t *d = read_file(cmdline_path ? cmdline_path : "/proc/cmdline", 65536, &n);
    if (!d) {
        /* /proc/cmdline reports size 0 (not a regular file size): read it plainly */
        FILE *f = fopen(cmdline_path ? cmdline_path : "/proc/cmdline", "re");
        if (!f)
            return false;
        d = malloc(4097);
        n = d ? fread(d, 1, 4096, f) : 0;
        fclose(f);
        if (!d)
            return false;
    }
    bool on = false;
    const char *key = "retroboot.mode=";
    size_t kn = strlen(key);
    for (size_t i = 0; i < n;) {
        while (i < n && (d[i] == ' ' || d[i] == '\n' || d[i] == '\t'))
            i++;
        size_t j = i;
        while (j < n && d[j] != ' ' && d[j] != '\n' && d[j] != '\t')
            j++;
        if (j - i >= kn && !memcmp(d + i, key, kn))
            on = j - i - kn == 9 && !memcmp(d + i + kn, "installer", 9); /* the last one counts */
        i = j;
    }
    free(d);
    return on;
}

bool rb_isplash_classic_startup(const char *cmdline_path)
{
    struct rb_boot_startup boot;
    rb_boot_startup(cmdline_path, &boot);
    return boot.style != RB_STARTUP_OSX && rb_isplash_installer_mode(cmdline_path);
}

static bool read_progress(const char *path, int *permille)
{
    char buf[256];
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0)
        return false;
    ssize_t n = read(fd, buf, sizeof buf - 1);
    close(fd);
    return n > 0 && rb_isplash_parse_progress(buf, (size_t)n, permille, NULL, 0) == 0;
}

static void run_timer_fn(void *data)
{
    struct rb_isplash_run *r = data;
    rb_isplash_run_tick(r, rb_boot_ms());
}

static void arm(struct rb_isplash_run *r, uint64_t now)
{
    if (!r->timer)
        return;
    if (!r->on || r->paused) {
        rb_timer_arm(r->timer, 0);
        return;
    }
    /* Animate at ~30 Hz while the bar moves; otherwise look at the progress
     * file ten times a second. */
    double g = rb_isplash_goal(&r->prog, now);
    bool moving = (g - r->prog.shown) * RB_ISPLASH_BAR_STEPS >= 0.25;
    uint64_t ms = moving ? 33 : 100;
    if (r->phase == RB_ISPLASH_WELCOME && r->t_welcome + RB_ISPLASH_WELCOME_MS > now &&
        r->t_welcome + RB_ISPLASH_WELCOME_MS - now < ms)
        ms = r->t_welcome + RB_ISPLASH_WELCOME_MS - now;
    rb_timer_arm(r->timer, ms ? ms : 1);
}

static void art_publish(const char *src, const char *dst)
{
    size_t n = 0;
    uint8_t *d = read_file(src, RBIS_MAX, &n);
    if (!d)
        return;
    char dir[256], tmp[300];
    snprintf(dir, sizeof dir, "%s", dst);
    char *slash = strrchr(dir, '/');
    if (slash) {
        *slash = 0;
        mkdir(dir, 0755); /* /run/retroboot (already there after the first boot stage) */
    }
    snprintf(tmp, sizeof tmp, "%s.tmp", dst);
    int fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644);
    if (fd >= 0) {
        bool ok = write(fd, d, n) == (ssize_t)n;
        ok = close(fd) == 0 && ok;
        if (!ok || rename(tmp, dst) < 0)
            unlink(tmp);
    }
    free(d);
}

static const char *const art_search[] = {
    RB_ISPLASH_RUN_ART,                                                 /* the splash's copy */
    "/run/retroboot/live/medium/live/installer-startup.rbis",           /* the installer medium */
    "/var/lib/retroboot/assets/platinum/startup/installer-startup.rbis", /* the Platinum bundle */
};

bool rb_isplash_run_start(struct rb_isplash_run *r, struct rb_loop *loop, const char *art_path, uint64_t now,
                          rb_isplash_redraw_fn redraw, void *ctx)
{
    memset(r, 0, sizeof *r);
    r->progress_path = getenv("RB_INSTALLER_PROGRESS") ? getenv("RB_INSTALLER_PROGRESS") : RB_ISPLASH_PROGRESS_FILE;
    r->visible_path = getenv("RB_INSTALLER_VISIBLE") ? getenv("RB_INSTALLER_VISIBLE") : RB_ISPLASH_VISIBLE_FILE;
    r->redraw = redraw;
    r->ctx = ctx;
    if (!rb_isplash_classic_startup(NULL))
        return false;
    int permille;
    if (read_progress(r->progress_path, &permille)) {
        if (permille >= 1000)
            return false; /* the installer is up already (a presenter restart) */
        rb_isplash_progress_update(&r->prog, permille, now);
    }
    int src = -1;
    if (art_path) {
        src = rb_isplash_art_load(&r->art, art_path);
        if (src == RB_ISPLASH_MACOS && !strcmp(art_path, RB_ISPLASH_INITRD_ART))
            art_publish(art_path, RB_ISPLASH_RUN_ART); /* for the presenter (the initramfs goes away) */
    } else {
        for (size_t i = 0; i < sizeof art_search / sizeof art_search[0] && src != RB_ISPLASH_MACOS; i++) {
            if (src >= 0)
                rb_isplash_art_free(&r->art);
            src = rb_isplash_art_load(&r->art, art_search[i]);
        }
    }
    r->composed = malloc((size_t)RB_ISPLASH_BOX_W * RB_ISPLASH_BOX_H * 4);
    if (src < 0 || !r->composed) {
        rb_isplash_run_free(r);
        return false;
    }
    r->on = true;
    r->phase = RB_ISPLASH_WELCOME;
    r->t_welcome = now;
    r->prog.t_shown = now;
    r->covered = access(r->visible_path, F_OK) == 0;
    rb_isplash_compose(r->composed, &r->art, r->phase, r->fill);
    if (loop)
        r->timer = rb_timer_new(loop, run_timer_fn, r);
    arm(r, now);
    rb_log("installer startup screen: on (%s art), progress %s", rb_isplash_source_name(r->art.source),
           r->prog.have ? "from boot-diag" : "estimated");
    return true;
}

void rb_isplash_run_tick(struct rb_isplash_run *r, uint64_t now)
{
    if (!r->on || r->paused)
        return;
    int permille, what = 0;
    if (read_progress(r->progress_path, &permille))
        rb_isplash_progress_update(&r->prog, permille, now);
    bool covered = access(r->visible_path, F_OK) == 0;
    if (covered != r->covered) {
        r->covered = covered;
        if (!covered)
            what |= RB_ISPLASH_DIRTY_ALL; /* the Startup Details went away: the box again */
    }
    double v = rb_isplash_advance(&r->prog, now);
    int phase = r->phase;
    if (phase == RB_ISPLASH_WELCOME && now >= r->t_welcome + RB_ISPLASH_WELCOME_MS &&
        (!r->prog.have || r->prog.permille > stage_permille[0]))
        phase = RB_ISPLASH_STARTING;
    int f = phase == RB_ISPLASH_STARTING ? rb_isplash_fill(v) : 0;
    if (phase != r->phase)
        what |= RB_ISPLASH_DIRTY_STATUS | RB_ISPLASH_DIRTY_BAR;
    if (f != r->fill)
        what |= RB_ISPLASH_DIRTY_BAR;
    if (phase != r->phase || f != r->fill) {
        r->phase = phase;
        r->fill = f;
        rb_isplash_compose(r->composed, &r->art, r->phase, r->fill);
    }
    if (what && !r->covered && r->redraw)
        r->redraw(r->ctx, what);
    arm(r, now);
}

void rb_isplash_run_pause(struct rb_isplash_run *r, bool paused)
{
    r->paused = paused;
    arm(r, rb_boot_ms());
}

void rb_isplash_run_end(struct rb_isplash_run *r)
{
    if (!r->on)
        return;
    r->on = false;
    if (r->timer)
        rb_timer_arm(r->timer, 0);
    rb_log("installer startup screen: over (bar %d/%d)", r->fill, RB_ISPLASH_BAR_STEPS);
}

void rb_isplash_run_free(struct rb_isplash_run *r)
{
    if (r->timer)
        rb_timer_free(r->timer);
    r->timer = NULL;
    free(r->composed);
    r->composed = NULL;
    rb_isplash_art_free(&r->art);
    r->on = false;
}

void rb_isplash_run_scene(struct rb_isplash_run *r, uint32_t *px, int stride_px, int W, int H, rb_rect clip,
                          int s, int pixel_req, int guest_w, int guest_h)
{
    if (!r->on || r->covered || !r->composed)
        return;
    if (!r->drawn) { /* "Welcome" is seen from here (the display may come up late) */
        r->drawn = true;
        uint64_t now = rb_boot_ms();
        if (r->phase == RB_ISPLASH_WELCOME && now > r->t_welcome)
            r->t_welcome = now;
    }
    struct rb_isplash_geom g;
    rb_isplash_geometry(&g, W, H, s, pixel_req, guest_w, guest_h);
    rb_isplash_blit(px, stride_px, W, H, clip, &g, r->composed);
}

rb_rect rb_isplash_run_dirty(const struct rb_isplash_run *r, int what, int W, int H, int s, int pixel_req,
                             int guest_w, int guest_h)
{
    (void)r;
    struct rb_isplash_geom g;
    rb_isplash_geometry(&g, W, H, s, pixel_req, guest_w, guest_h);
    if (!g.valid)
        return (rb_rect){0, 0, 0, 0};
    if (what & RB_ISPLASH_DIRTY_ALL)
        return rb_isplash_rect(&g);
    rb_rect out = {0, 0, 0, 0};
    if (what & RB_ISPLASH_DIRTY_STATUS)
        out = rb_isplash_status_rect(&g);
    if (what & RB_ISPLASH_DIRTY_BAR)
        out = rb_rect_empty(out) ? rb_isplash_bar_rect(&g) : rb_rect_union(out, rb_isplash_bar_rect(&g));
    return out;
}

/* ---- handoff ------------------------------------------------------------------------------ */

int rb_isplash_state_format(const struct rb_isplash_run *r, char *out, size_t n)
{
    int len = snprintf(out, n, "isplash %d %d %d %ld %llu %llu %d %d %llu %s %d", r->on ? 1 : 0, r->phase, r->fill,
                       lround(r->prog.shown * 1e9), (unsigned long long)r->prog.t_shown,
                       (unsigned long long)r->t_welcome, r->prog.have ? 1 : 0, r->prog.permille,
                       (unsigned long long)r->prog.t_stage, rb_isplash_source_name(r->art.source), r->covered ? 1 : 0);
    return len > 0 && (size_t)len < n ? 0 : -1;
}

int rb_isplash_state_apply(struct rb_isplash_run *r, const char *line, uint64_t now)
{
    int on, phase, f, have, permille, covered;
    long shown;
    unsigned long long t_shown, t_welcome, t_stage;
    char src[16];
    if (sscanf(line, "isplash %d %d %d %ld %llu %llu %d %d %llu %15s %d", &on, &phase, &f, &shown, &t_shown,
               &t_welcome, &have, &permille, &t_stage, src, &covered) != 11 ||
        (phase != RB_ISPLASH_WELCOME && phase != RB_ISPLASH_STARTING) || f < 0 || f > RB_ISPLASH_BAR_STEPS ||
        shown < 0 || shown > 1000000000L || permille < 0 || permille > 1000)
        return -1;
    if (!on) {
        rb_isplash_run_end(r); /* the splash showed none: neither do we */
        return 0;
    }
    if (!r->on)
        return -1;
    /* The splash's art: the same file (its copy in /run) or the built-in art. */
    int want = !strcmp(src, "macos") ? RB_ISPLASH_MACOS : RB_ISPLASH_BUILTIN;
    if (r->art.source != want) {
        struct rb_isplash_art a;
        int got = want == RB_ISPLASH_MACOS ? rb_isplash_art_load(&a, RB_ISPLASH_RUN_ART) : rb_isplash_art_builtin(&a);
        if (got >= 0) {
            rb_isplash_art_free(&r->art);
            r->art = a;
        }
    }
    r->phase = phase;
    r->fill = f;
    r->prog.shown = shown / 1e9;
    r->prog.t_shown = t_shown < now ? t_shown : now;
    r->t_welcome = t_welcome;
    r->drawn = true; /* the splash drew it */
    r->prog.have = have != 0;
    r->prog.permille = permille;
    r->prog.t_stage = t_stage;
    r->covered = covered != 0;
    rb_isplash_compose(r->composed, &r->art, r->phase, r->fill);
    arm(r, now);
    return 0;
}

int rb_isplash_handoff_take(struct rb_isplash_run *r, int fd, uint64_t now)
{
    if (!r->on || fd < 0 || send(fd, "isplash", 7, MSG_NOSIGNAL) != 7)
        return -1;
    char buf[256];
    ssize_t n = recv(fd, buf, sizeof buf - 1, 0);
    if (n <= 0)
        return -1;
    buf[n] = 0;
    if (strncmp(buf, "isplash ", 8)) /* an older splash: "error unknown command" */
        return -1;
    return rb_isplash_state_apply(r, buf, now);
}
