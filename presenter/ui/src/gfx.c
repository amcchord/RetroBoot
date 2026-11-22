/* gfx.c - canvas primitives, UTF-8 text and icon drawing */
#include <string.h>

#include "rbui_internal.h"

rb_rect r_and(rb_rect a, rb_rect b)
{
    int x0 = a.x > b.x ? a.x : b.x, y0 = a.y > b.y ? a.y : b.y;
    int x1 = a.x + a.w < b.x + b.w ? a.x + a.w : b.x + b.w;
    int y1 = a.y + a.h < b.y + b.h ? a.y + a.h : b.y + b.h;
    if (x1 <= x0 || y1 <= y0) return rr(0, 0, 0, 0);
    return rr(x0, y0, x1 - x0, y1 - y0);
}

rb_rect r_or(rb_rect a, rb_rect b)
{
    if (r_empty(a)) return b;
    if (r_empty(b)) return a;
    int x0 = a.x < b.x ? a.x : b.x, y0 = a.y < b.y ? a.y : b.y;
    int x1 = a.x + a.w > b.x + b.w ? a.x + a.w : b.x + b.w;
    int y1 = a.y + a.h > b.y + b.h ? a.y + a.h : b.y + b.h;
    return rr(x0, y0, x1 - x0, y1 - y0);
}

/* ---- Primitives ---------------------------------------------------------- */

void g_fill(rb_gfx *g, int x, int y, int w, int h, uint32_t c)
{
    if (w <= 0 || h <= 0) return;
    rb_rect r = r_and(rr(x + g->dx, y + g->dy, w, h), g->clip);
    if (r_empty(r)) return;
    for (int yy = r.y; yy < r.y + r.h; ++yy) {
        uint32_t *p = g->px + (size_t)yy * (size_t)g->stride + (size_t)r.x;
        for (int i = 0; i < r.w; ++i) p[i] = c;
    }
}

void g_pixel(rb_gfx *g, int x, int y, uint32_t c)
{
    x += g->dx;
    y += g->dy;
    if (r_has(g->clip, x, y)) g->px[(size_t)y * (size_t)g->stride + (size_t)x] = c;
}

void g_blend(rb_gfx *g, int x, int y, uint32_t argb)
{
    unsigned a = argb >> 24;
    if (a == 0) return;
    if (a == 255) {
        g_pixel(g, x, y, argb & 0xFFFFFFu);
        return;
    }
    x += g->dx;
    y += g->dy;
    if (!r_has(g->clip, x, y)) return;
    uint32_t *p = g->px + (size_t)y * (size_t)g->stride + (size_t)x;
    uint32_t d = *p, out = 0;
    for (int sh = 0; sh <= 16; sh += 8) {
        unsigned s = (argb >> sh) & 255u, t = (d >> sh) & 255u;
        unsigned v = (s * a + t * (255u - a) + 127u) / 255u;
        out |= v << sh;
    }
    *p = out;
}

void g_level(rb_gfx *g, int x, int y, uint32_t fg, int level)
{
    if (level <= 0) return;
    if (level >= 16) {
        g_pixel(g, x, y, fg);
        return;
    }
    x += g->dx;
    y += g->dy;
    if (!r_has(g->clip, x, y)) return;
    uint32_t *p = g->px + (size_t)y * (size_t)g->stride + (size_t)x;
    uint32_t d = *p, out = 0;
    for (int sh = 0; sh <= 16; sh += 8) {
        int b = (int)((d >> sh) & 255u), f = (int)((fg >> sh) & 255u);
        int t = (f - b) * 17 * level;
        int v = b + (t >= 0 ? t >> 8 : -((-t + 255) >> 8));   /* floor division */
        out |= (uint32_t)v << sh;
    }
    *p = out;
}

void g_frame(rb_gfx *g, rb_rect r, uint32_t c)
{
    if (r.w <= 0 || r.h <= 0) return;
    g_hline(g, r.x, r.y, r.w, c);
    g_hline(g, r.x, r.y + r.h - 1, r.w, c);
    g_vline(g, r.x, r.y, r.h, c);
    g_vline(g, r.x + r.w - 1, r.y, r.h, c);
}

rb_rect g_push_clip(rb_gfx *g, rb_rect r)
{
    rb_rect old = g->clip;
    g->clip = r_and(g->clip, rr(r.x + g->dx, r.y + g->dy, r.w, r.h));
    return old;
}

/* Character codes shared by the measured Platinum sprites (M5Tab's
 * preboot_ui.cpp conventions). */
uint32_t code_color(char c, int *ok)
{
    *ok = 1;
    switch (c) {
    case 'K': return C_BLACK;
    case 'W': return C_WHITE;
    case '3': return C_3A;
    case '6': return C_63;
    case '7': return C_73;
    case '9': return C_94;
    case 'A': return C_A5;
    case 'B': return C_B5;
    case 'C': return C_C5;
    case 'c': return C_CE;
    case 'D': return C_DE;
    case 'E': return C_E6;
    case 'F': return C_F7;
    case 'l': return C_ACC_L;
    case 'a': return C_ACC;
    case 's': return C_ACC_S;
    case 'd': return C_ACC_D;
    default: *ok = 0; return 0;
    }
}

void g_coded(rb_gfx *g, int x, int y, const char *const *rows, int nrows, int mode)
{
    (void)mode;
    for (int r = 0; r < nrows; ++r)
        for (int c = 0; rows[r][c]; ++c) {
            int ok;
            uint32_t col = code_color(rows[r][c], &ok);
            if (ok) g_pixel(g, x + c, y + r, col);
        }
}

/* ---- Fonts ---------------------------------------------------------------- */

static const rbf_font *font_named(const char *name)
{
    const rbf_font *a = assets_font(name);
    if (a) return a;
    for (int i = 0; rbui_font_table[i]; ++i)
        if (strcmp(rbui_font_table[i]->name, name) == 0) return rbui_font_table[i];
    return NULL;
}

const rbf_font *rbf_get(const char *name)
{
    const rbf_font *f = font_named(name);
    if (!f) f = font_named("system12");
    return f ? f : rbui_font_table[0];
}

uint32_t rbf_utf8_next(const char *s, size_t len, size_t *i)
{
    const unsigned char *p = (const unsigned char *)s + *i;
    size_t left = len - *i;
    unsigned c = p[0];
    if (c < 0x80) {
        *i += 1;
        return c;
    }
    int n;
    uint32_t cp, min;
    if ((c & 0xE0) == 0xC0) { n = 1; cp = c & 0x1F; min = 0x80; }
    else if ((c & 0xF0) == 0xE0) { n = 2; cp = c & 0x0F; min = 0x800; }
    else if ((c & 0xF8) == 0xF0) { n = 3; cp = c & 0x07; min = 0x10000; }
    else { *i += 1; return 0xFFFD; }
    if ((size_t)n >= left) { *i += 1; return 0xFFFD; }
    for (int k = 1; k <= n; ++k) {
        if ((p[k] & 0xC0) != 0x80) { *i += 1; return 0xFFFD; }
        cp = (cp << 6) | (p[k] & 0x3F);
    }
    if (cp < min || cp > 0x10FFFF || (cp >= 0xD800 && cp <= 0xDFFF)) { *i += 1; return 0xFFFD; }
    *i += (size_t)n + 1;
    return cp;
}

size_t rbf_utf8_prev(const char *s, size_t i)
{
    if (i == 0) return 0;
    /* Step back over continuation bytes, at most 3, then verify by decoding. */
    size_t j = i - 1;
    int k = 0;
    while (j > 0 && k < 3 && (((unsigned char)s[j]) & 0xC0) == 0x80) { --j; ++k; }
    size_t t = j;
    rbf_utf8_next(s, i, &t);
    return t == i ? j : i - 1;
}

int rbf_utf8_put(uint32_t cp, char *o)
{
    if (cp < 0x80) { o[0] = (char)cp; return 1; }
    if (cp < 0x800) { o[0] = (char)(0xC0 | (cp >> 6)); o[1] = (char)(0x80 | (cp & 0x3F)); return 2; }
    if (cp < 0x10000) {
        o[0] = (char)(0xE0 | (cp >> 12)); o[1] = (char)(0x80 | ((cp >> 6) & 0x3F));
        o[2] = (char)(0x80 | (cp & 0x3F));
        return 3;
    }
    o[0] = (char)(0xF0 | (cp >> 18)); o[1] = (char)(0x80 | ((cp >> 12) & 0x3F));
    o[2] = (char)(0x80 | ((cp >> 6) & 0x3F)); o[3] = (char)(0x80 | (cp & 0x3F));
    return 4;
}

const rbf_glyph *rbf_find(const rbf_font *f, uint32_t cp)
{
    int lo = 0, hi = f->count - 1;
    while (lo <= hi) {
        int mid = (lo + hi) / 2;
        uint32_t v = f->glyphs[mid].cp;
        if (v == cp) return &f->glyphs[mid];
        if (v < cp) lo = mid + 1;
        else hi = mid - 1;
    }
    return NULL;
}

static const rbf_glyph *glyph_or_replacement(const rbf_font *f, uint32_t cp)
{
    if (cp == 0x00A0) cp = 0x20;
    const rbf_glyph *gl = rbf_find(f, cp);
    if (!gl) gl = rbf_find(f, 0xFFFD);
    return gl;
}

int rbf_advance(const rbf_font *f, uint32_t cp)
{
    const rbf_glyph *gl = glyph_or_replacement(f, cp);
    return gl ? gl->adv : f->cap * 2 / 3 + 1;
}

int rbf_width(const rbf_font *f, const char *s, size_t len)
{
    int w = 0;
    size_t i = 0;
    while (i < len) w += rbf_advance(f, rbf_utf8_next(s, len, &i));
    return w;
}

/* ---- Text ------------------------------------------------------------------- */

static int draw_glyph(rb_gfx *g, const rbf_font *f, int scale, int x, int baseline, uint32_t cp, uint32_t c)
{
    const rbf_glyph *gl = glyph_or_replacement(f, cp);
    if (!gl) {
        /* No replacement glyph in the font: a hollow box, cap height tall. */
        int w = f->cap * 2 / 3;
        g_frame(g, rr(x, baseline - f->cap * scale, w * scale, f->cap * scale), c);
        return (w + 1) * scale;
    }
    if (f->levels) {
        const uint8_t *cov = f->bits + gl->off;
        for (int yy = 0; yy < gl->h; ++yy)
            for (int xx = 0; xx < gl->w; ++xx) {
                int v = cov[yy * gl->w + xx];
                if (!v) continue;
                int px = x + (gl->xoff + xx) * scale, py = baseline + (gl->top + yy) * scale;
                for (int sy = 0; sy < scale; ++sy)
                    for (int sx = 0; sx < scale; ++sx) g_level(g, px + sx, py + sy, c, v);
            }
        return gl->adv * scale;
    }
    int rowb = (gl->w + 7) / 8;
    const uint8_t *bits = f->bits + gl->off;
    for (int yy = 0; yy < gl->h; ++yy)
        for (int xx = 0; xx < gl->w; ++xx)
            if (bits[yy * rowb + (xx >> 3)] & (0x80 >> (xx & 7))) {
                int px = x + (gl->xoff + xx) * scale, py = baseline + (gl->top + yy) * scale;
                if (scale == 1) g_pixel(g, px, py, c);
                else g_fill(g, px, py, scale, scale, c);
            }
    return gl->adv * scale;
}

int tx_draw(rb_gfx *g, const rbf_font *f, int scale, int x, int baseline, const char *s, size_t len, uint32_t c)
{
    size_t i = 0;
    if (!s) return x;
    while (i < len) {
        uint32_t cp = rbf_utf8_next(s, len, &i);
        x += draw_glyph(g, f, scale, x, baseline, cp, c);
    }
    return x;
}

int tx_width(const rbf_font *f, int scale, const char *s, size_t len)
{
    return s ? rbf_width(f, s, len) * scale : 0;
}

static const char kEllipsis[] = "\xE2\x80\xA6";

int tx_fit_len(const rbf_font *f, const char *s, size_t len, int maxw, int *ellipsis)
{
    *ellipsis = 0;
    if (rbf_width(f, s, len) <= maxw) return (int)len;
    *ellipsis = 1;
    int ew = rbf_width(f, kEllipsis, 3), w = 0;
    size_t i = 0, keep = 0;
    while (i < len) {
        size_t j = i;
        int a = rbf_advance(f, rbf_utf8_next(s, len, &j));
        if (w + a + ew > maxw) break;
        w += a;
        i = j;
        keep = i;
    }
    while (keep > 0 && s[keep - 1] == ' ') --keep;
    return (int)keep;
}

int tx_draw_fit(rb_gfx *g, const rbf_font *f, int x, int baseline, int maxw, const char *s, uint32_t c)
{
    if (!s) return 0;
    size_t len = strlen(s);
    int ell;
    int n = tx_fit_len(f, s, len, maxw, &ell);
    int end = tx_draw(g, f, 1, x, baseline, s, (size_t)n, c);
    if (ell) end = tx_draw(g, f, 1, end, baseline, kEllipsis, 3, c);
    return end - x;
}

/* ---- Icons and cursor ------------------------------------------------------ */

static const char *const kIconNames[] = {
    "mac", "disk", "cd", "folder", "network", "wifi", "web", "wrench", "stop", "caution", "note",
    "display", "sound", "power", "usb", "lock", "bluetooth",
    "startup", "update", "clock", "profiler",          /* the RetroBoot window's panes */
    "chip",                                            /* its processor meter */
    "monitor_page", "sound_page", "keyboard",          /* Display & Sound's bevel buttons */
    "speaker_low", "speaker_high",                     /* its volume slider (feedback art) */
    NULL
};

int icon_index(const char *name)
{
    if (!name) return -1;
    for (int i = 0; kIconNames[i]; ++i)
        if (strcmp(kIconNames[i], name) == 0) return i;
    return -1;
}

const char *icon_name(int index)
{
    if (index < 0 || index >= (int)(sizeof(kIconNames) / sizeof(kIconNames[0])) - 1) return NULL;
    return kIconNames[index];
}

const rbi_icon *icon_get(const char *name, int size)
{
    const rbi_icon *own = assets_icon(name, size);
    if (!own && size > 32) own = assets_icon(name, 32);   /* the bundle's 32 px before our art */
    if (own) return own;
    const rbi_icon *best = NULL;
    for (int i = 0; i < rbui_icon_count; ++i) {
        const rbi_icon *ic = &rbui_icons[i];
        if (strcmp(ic->name, name) != 0) continue;
        if (ic->size == size) return ic;
        if (!best || ic->size > best->size) best = ic;
    }
    return best;
}

static void blit_icon(rb_gfx *g, const rbi_icon *ic, int x, int y, int size, int dim)
{
    /* Draw at `size`, scaling a different-size icon by nearest neighbour. */
    for (int yy = 0; yy < size; ++yy)
        for (int xx = 0; xx < size; ++xx) {
            int sx = xx * ic->size / size, sy = yy * ic->size / size;
            uint32_t c;
            if (ic->argb) {
                c = ic->argb[sy * ic->size + sx];
                if (!(c >> 24)) continue;
            } else {
                uint8_t idx = ic->px[sy * ic->size + sx];
                if (!idx) continue;
                c = ic->pal[idx];
            }
            if (dim == ICON_SELECTED) {
                /* Selected (Finder), measured on Mac OS 9.2.2: the largest channel (or channels)
                 * halved, the others quartered, rounding down: (102,153,255) -> (25,38,127). */
                unsigned r = (c >> 16) & 255u, gg = (c >> 8) & 255u, bb = c & 255u;
                unsigned m = r > gg ? (r > bb ? r : bb) : (gg > bb ? gg : bb);
                r = r == m ? r >> 1 : r >> 2;
                gg = gg == m ? gg >> 1 : gg >> 2;
                bb = bb == m ? bb >> 1 : bb >> 2;
                c = (c & 0xFF000000u) | (r << 16) | (gg << 8) | bb;
            } else if (dim) {
                /* Disabled: blend 50% toward the dialog gray. */
                uint32_t a = c >> 24, out = 0;
                for (int sh = 0; sh <= 16; sh += 8) {
                    unsigned v = (((c >> sh) & 255u) + ((g_pal.face >> sh) & 255u)) / 2u;
                    out |= v << sh;
                }
                c = (a << 24) | out;
            }
            g_blend(g, x + xx, y + yy, c);
        }
}

void icon_draw(rb_gfx *g, int index, int size, int x, int y, int dim)
{
    const char *name = icon_name(index);
    if (!name) return;
    const rbi_icon *ic = icon_get(name, size);
    if (ic && size > 32 && ic->size < size) {
        /* No art at 40 or 64 px (bevel buttons, Monitors' display): the 32 px icon centred. */
        int d = (size - ic->size) / 2;
        blit_icon(g, ic, x + d, y + d, ic->size, dim);
        return;
    }
    if (ic) blit_icon(g, ic, x, y, size, dim);
}

void icon_draw_named(rb_gfx *g, const char *name, int size, int x, int y, int dim)
{
    const rbi_icon *ic = name ? icon_get(name, size) : NULL;
    if (ic) blit_icon(g, ic, x, y, size, dim);
}

/* Fallback arrow if the icon tables lack a "cursor" entry (original art). */
static const char *const kArrow[16] = {
    "K...........",
    "KK..........",
    "KWK.........",
    "KWWK........",
    "KWWWK.......",
    "KWWWWK......",
    "KWWWWWK.....",
    "KWWWWWWK....",
    "KWWWWWWWK...",
    "KWWWWWKKKK..",
    "KWWKWWK.....",
    "KWK.KWWK....",
    "KK..KWWK....",
    "K....KWWK...",
    ".....KWWK...",
    "......KK....",
};

rb_rect cursor_bounds(int x, int y)
{
    const rbi_icon *ic = icon_get("cursor", 16);
    if (ic) return rr(x - ic->hot_x, y - ic->hot_y, ic->size, ic->size);
    return rr(x, y, 12, 16);
}

void cursor_draw(rb_gfx *g, int x, int y, rb_rect *bounds)
{
    const rbi_icon *ic = icon_get("cursor", 16);
    if (ic) {
        blit_icon(g, ic, x - ic->hot_x, y - ic->hot_y, ic->size, 0);
    } else {
        /* The fallback art uses K for the outline and W for the fill; invert
         * it so the arrow is black with a white outline. */
        for (int r = 0; r < 16; ++r)
            for (int c = 0; kArrow[r][c]; ++c) {
                char ch = kArrow[r][c];
                if (ch == 'K') g_pixel(g, x + c, y + r, C_WHITE);
                else if (ch == 'W') g_pixel(g, x + c, y + r, C_BLACK);
            }
    }
    if (bounds) *bounds = cursor_bounds(x, y);
}
