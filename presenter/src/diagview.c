/* Startup Details view: document, layout and rendering (diagview.h). */
#include "diagview.h"

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/inotify.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include "font.h" /* presenter/ui: the MIT RetroBoot bitmap fonts (fonts_data.c) */

/* ------------------------------------------------------------------------ */
/* command line                                                             */

static bool cmdline_has(const char *cmdline, const char *word, size_t wn, bool prefix, const char **val)
{
    /* the last occurrence wins, as for other kernel parameters */
    bool found = false;
    const char *p = cmdline;
    while (p && *p) {
        while (*p == ' ' || *p == '\t' || *p == '\n')
            p++;
        const char *end = p;
        while (*end && *end != ' ' && *end != '\t' && *end != '\n')
            end++;
        size_t n = (size_t)(end - p);
        if ((prefix ? n >= wn : n == wn) && !strncmp(p, word, wn)) {
            found = true;
            if (val)
                *val = p + wn;
        }
        p = end;
    }
    return found;
}

static bool value_is(const char *v, const char *want)
{
    size_t n = strlen(want);
    return v && !strncmp(v, want, n) && (v[n] == 0 || v[n] == ' ' || v[n] == '\t' || v[n] == '\n');
}

void rb_diag_cmdline(const char *cmdline, struct rb_diag_cfg *c)
{
    memset(c, 0, sizeof *c);
    const char *v = NULL;
    c->installer = cmdline_has(cmdline, "retroboot.mode=", 15, true, &v) && value_is(v, "installer");
    v = NULL;
    bool diag = cmdline_has(cmdline, "retroboot.diag=", 15, true, &v);
    bool off = diag && value_is(v, "0");
    bool on = diag && (value_is(v, "1") || value_is(v, "show"));
    c->show = diag && value_is(v, "show");
    c->enabled = !off && (c->installer || on);
    if (!c->enabled)
        c->show = false;
    v = NULL;
    c->fb = !(cmdline_has(cmdline, "retroboot.diag_fb=", 18, true, &v) && value_is(v, "0"));
}

void rb_diag_boot_cfg(struct rb_diag_cfg *c)
{
    const char *path = getenv("RB_DIAG_CMDLINE");
    char buf[4096];
    ssize_t n = rb_diag_read_file(path && *path ? path : "/proc/cmdline", buf, sizeof buf);
    rb_diag_cmdline(n > 0 ? buf : "", c);
}

enum rb_diag_esc rb_diag_escape(bool diag_enabled, bool visible, bool preboot, bool display)
{
    if (visible)
        return RB_DIAG_ESC_HIDE;
    if (!diag_enabled)
        return preboot ? RB_DIAG_ESC_PREBOOT : RB_DIAG_ESC_NONE;
    /* Without a display nothing the presenter shows can be seen (the panel
     * included): Escape is the way to the details, drawn on the firmware
     * framebuffer by rb-bootdiag. */
    return preboot || !display ? RB_DIAG_ESC_SHOW : RB_DIAG_ESC_NONE;
}

bool rb_diag_return_opens_options(bool installer, bool visible, bool preboot)
{
    return visible && preboot && !installer;
}

/* ------------------------------------------------------------------------ */
/* document                                                                 */

static void copy_field(char *dst, size_t cap, const char *src, size_t n)
{
    if (n >= cap)
        n = cap - 1;
    memcpy(dst, src, n);
    dst[n] = 0;
    /* tabs and control characters never reach the screen */
    for (char *p = dst; *p; p++)
        if ((unsigned char)*p < 0x20)
            *p = ' ';
}

static int state_of(const char *s, size_t n)
{
    if (n == 2 && !strncmp(s, "ok", 2))
        return RB_DIAG_OK;
    if (n == 4 && !strncmp(s, "fail", 4))
        return RB_DIAG_FAIL;
    if (n == 4 && !strncmp(s, "skip", 4))
        return RB_DIAG_SKIP;
    return RB_DIAG_WAIT;
}

static const char *state_name(int s)
{
    switch (s) {
    case RB_DIAG_OK: return "ok";
    case RB_DIAG_FAIL: return "fail";
    case RB_DIAG_SKIP: return "skip";
    default: return "wait";
    }
}

void rb_diag_doc_init(struct rb_diag_doc *d, const char *title)
{
    memset(d, 0, sizeof *d);
    snprintf(d->title, sizeof d->title, "%s", title ? title : "Startup Details");
}

static struct rb_diag_line *doc_new(struct rb_diag_doc *d, char tag)
{
    if (d->n >= RB_DIAG_MAX_LINES)
        return NULL;
    struct rb_diag_line *l = &d->line[d->n++];
    memset(l, 0, sizeof *l);
    l->tag = tag;
    return l;
}

void rb_diag_doc_add(struct rb_diag_doc *d, char tag, const char *text)
{
    if (tag == 'T') {
        copy_field(d->title, sizeof d->title, text, strlen(text));
        return;
    }
    struct rb_diag_line *l = doc_new(d, tag);
    if (l)
        copy_field(l->text, sizeof l->text, text, strlen(text));
}

void rb_diag_doc_stage(struct rb_diag_doc *d, int state, const char *time, const char *label)
{
    struct rb_diag_line *l = doc_new(d, 'S');
    if (!l)
        return;
    l->state = state;
    copy_field(l->time, sizeof l->time, time ? time : "", strlen(time ? time : ""));
    copy_field(l->text, sizeof l->text, label, strlen(label));
}

void rb_diag_doc_parse(const char *text, size_t len, struct rb_diag_doc *d)
{
    rb_diag_doc_init(d, NULL);
    size_t i = 0;
    while (i < len) {
        size_t e = i;
        while (e < len && text[e] != '\n')
            e++;
        const char *ln = text + i;
        size_t n = e - i;
        i = e + 1;
        if (n < 2 || ln[1] != '\t')
            continue;
        char tag = ln[0];
        const char *f = ln + 2;
        size_t fn = n - 2;
        if (tag == 'T') {
            copy_field(d->title, sizeof d->title, f, fn);
        } else if (tag == 'S') {
            const char *t1 = memchr(f, '\t', fn);
            if (!t1)
                continue;
            const char *t2 = memchr(t1 + 1, '\t', fn - (size_t)(t1 + 1 - f));
            if (!t2)
                continue;
            struct rb_diag_line *l = doc_new(d, 'S');
            if (!l)
                break;
            l->state = state_of(f, (size_t)(t1 - f));
            copy_field(l->time, sizeof l->time, t1 + 1, (size_t)(t2 - t1 - 1));
            copy_field(l->text, sizeof l->text, t2 + 1, fn - (size_t)(t2 + 1 - f));
        } else if (strchr("HPDLF", tag)) {
            struct rb_diag_line *l = doc_new(d, tag);
            if (!l)
                break;
            copy_field(l->text, sizeof l->text, f, fn);
        }
    }
}

size_t rb_diag_doc_format(const struct rb_diag_doc *d, char *out, size_t cap)
{
    size_t n = 0;
    if (!cap)
        return 0;
    out[0] = 0;
#define PUT(...)                                                                                                       \
    do {                                                                                                               \
        if (n < cap) {                                                                                                 \
            int k = snprintf(out + n, cap - n, __VA_ARGS__);                                                           \
            n = k < 0 ? n : (n + (size_t)k < cap ? n + (size_t)k : cap - 1);                                          \
        }                                                                                                              \
    } while (0)
    PUT("T\t%s\n", d->title);
    for (int i = 0; i < d->n; i++) {
        const struct rb_diag_line *l = &d->line[i];
        if (l->tag == 'S')
            PUT("S\t%s\t%s\t%s\n", state_name(l->state), l->time, l->text);
        else
            PUT("%c\t%s\n", l->tag, l->text);
    }
#undef PUT
    return n;
}

/* ------------------------------------------------------------------------ */
/* fonts and canvas                                                         */

static const rbf_font *font_named(const char *name)
{
    for (int i = 0; rbui_font_table[i]; i++)
        if (!strcmp(rbui_font_table[i]->name, name))
            return rbui_font_table[i];
    return rbui_font_table[0];
}

static const rbf_font *font_of(enum rb_diag_font f)
{
    switch (f) {
    case RB_DIAG_FONT_SMALL: return font_named("small10");
    case RB_DIAG_FONT_SYSTEM: return font_named("system12");
    default: return font_named("regular12");
    }
}

static const rbf_glyph *glyph(const rbf_font *f, uint32_t cp)
{
    int lo = 0, hi = f->count - 1;
    while (lo <= hi) {
        int mid = (lo + hi) / 2;
        uint32_t c = f->glyphs[mid].cp;
        if (c == cp)
            return &f->glyphs[mid];
        if (c < cp)
            lo = mid + 1;
        else
            hi = mid - 1;
    }
    if (cp != 0xFFFD)
        return glyph(f, 0xFFFD);
    return NULL;
}

/* One UTF-8 code point; invalid bytes read as U+FFFD (Latin-1 never reaches here
 * mangled: rb-bootdiag writes UTF-8). */
static uint32_t utf8_next(const char *s, size_t *i)
{
    const unsigned char *u = (const unsigned char *)s + *i;
    uint32_t c = u[0];
    int n = 0;
    if (c < 0x80) {
        *i += 1;
        return c;
    } else if ((c & 0xe0) == 0xc0) {
        c &= 0x1f;
        n = 1;
    } else if ((c & 0xf0) == 0xe0) {
        c &= 0x0f;
        n = 2;
    } else if ((c & 0xf8) == 0xf0) {
        c &= 0x07;
        n = 3;
    } else {
        *i += 1;
        return 0xFFFD;
    }
    for (int k = 1; k <= n; k++) {
        if ((u[k] & 0xc0) != 0x80) {
            *i += 1;
            return 0xFFFD;
        }
        c = c << 6 | (u[k] & 0x3f);
    }
    *i += (size_t)n + 1;
    return c;
}

static int adv_of(const rbf_font *f, uint32_t cp)
{
    const rbf_glyph *g = glyph(f, cp);
    return g ? g->adv : 0;
}

static int width_n(const rbf_font *f, const char *s, size_t len)
{
    int w = 0;
    size_t i = 0;
    while (i < len && s[i])
        w += adv_of(f, utf8_next(s, &i));
    return w;
}

int rb_diag_text_width(enum rb_diag_font f, const char *s) { return width_n(font_of(f), s, strlen(s)); }

struct canvas {
    uint32_t *px;
    int w, h; /* allocated logical size */
};

static void fill(struct canvas *c, int x, int y, int w, int h, uint32_t col)
{
    int x0 = x < 0 ? 0 : x, y0 = y < 0 ? 0 : y;
    int x1 = x + w > c->w ? c->w : x + w, y1 = y + h > c->h ? c->h : y + h;
    for (int yy = y0; yy < y1; yy++)
        for (int xx = x0; xx < x1; xx++)
            c->px[(size_t)yy * c->w + xx] = col;
}

static void hline(struct canvas *c, int x, int y, int w, uint32_t col) { fill(c, x, y, w, 1, col); }
static void vline(struct canvas *c, int x, int y, int h, uint32_t col) { fill(c, x, y, 1, h, col); }

/* Draw text with its baseline at y, clipped to [x, x + maxw); returns the advance. */
static int text_n(struct canvas *c, const rbf_font *f, int x, int y, const char *s, size_t len, uint32_t col, int maxw)
{
    int pen = 0;
    size_t i = 0;
    while (i < len && s[i]) {
        uint32_t cp = utf8_next(s, &i);
        const rbf_glyph *g = glyph(f, cp);
        if (!g)
            continue;
        if (maxw >= 0 && pen + g->adv > maxw)
            break;
        int rb = (g->w + 7) / 8;
        for (int r = 0; r < g->h; r++) {
            const uint8_t *row = f->bits + g->off + (size_t)r * rb;
            for (int b = 0; b < g->w; b++)
                if (row[b / 8] & (0x80 >> (b % 8))) {
                    int px = x + pen + g->xoff + b, py = y + g->top + r;
                    if (px >= 0 && py >= 0 && px < c->w && py < c->h)
                        c->px[(size_t)py * c->w + px] = col;
                }
        }
        pen += g->adv;
    }
    return pen;
}

/* Text cut to maxw with a trailing ellipsis when it does not fit. */
static void text_fit(struct canvas *c, const rbf_font *f, int x, int y, const char *s, uint32_t col, int maxw)
{
    size_t len = strlen(s);
    if (width_n(f, s, len) <= maxw) {
        text_n(c, f, x, y, s, len, col, -1);
        return;
    }
    int ell = adv_of(f, 0x2026);
    size_t i = 0, cut = 0;
    int w = 0;
    while (i < len) {
        size_t j = i;
        int a = adv_of(f, utf8_next(s, &j));
        if (w + a + ell > maxw)
            break;
        w += a;
        i = j;
        cut = i;
    }
    text_n(c, f, x, y, s, cut, col, -1);
    text_n(c, f, x + w, y, "\xe2\x80\xa6", 3, col, -1);
}

/* Word-wrap s into lines of at most maxw; calls back each line (start, len).
 * A word wider than a line is broken between characters. Returns the line count. */
typedef void (*wrap_fn)(void *ctx, const char *s, size_t n, int line);
static int wrap(const rbf_font *f, const char *s, int maxw, int max_lines, wrap_fn fn, void *ctx)
{
    size_t len = strlen(s), i = 0;
    int lines = 0;
    if (maxw < 8)
        maxw = 8;
    while (i < len && lines < max_lines) {
        while (i < len && s[i] == ' ')
            i++;
        if (i >= len)
            break;
        size_t start = i, last_break = 0, j = i;
        int w = 0;
        bool broke = false;
        while (j < len) {
            size_t k = j;
            uint32_t cp = utf8_next(s, &k);
            int a = adv_of(f, cp);
            if (w + a > maxw) {
                broke = true;
                break;
            }
            w += a;
            j = k;
            if (cp == ' ')
                last_break = j;
        }
        size_t end = j;
        if (broke && last_break > start)
            end = last_break;
        if (end == start) { /* not even one character fits */
            size_t k = start;
            utf8_next(s, &k);
            end = k;
        }
        size_t n = end - start;
        while (n > 0 && s[start + n - 1] == ' ')
            n--;
        if (lines == max_lines - 1 && end < len && broke) {
            /* last allowed line: the rest goes in with an ellipsis */
            if (fn)
                fn(ctx, s + start, len - start, -1 - lines);
        } else if (fn) {
            fn(ctx, s + start, n, lines);
        }
        lines++;
        i = end;
    }
    return lines;
}

/* ------------------------------------------------------------------------ */
/* look                                                                     */

#define C_BLACK 0x000000u
#define C_WHITE 0xffffffu
#define C_TITLE 0xddddddu
#define C_STRIPE_D 0x999999u
#define C_STRIPE_L 0xffffffu
#define C_RULE 0xbbbbbbu
#define C_GRAY 0x666666u
#define C_RED 0xcc0000u
#define C_GREEN 0x007700u

static void desktop(struct canvas *c)
{
    /* the classic 50 % pattern, one logical pixel per cell, black at (0, 0) */
    for (int y = 0; y < c->h; y++)
        for (int x = 0; x < c->w; x++)
            c->px[(size_t)y * c->w + x] = ((x + y) & 1) ? C_WHITE : C_BLACK;
}

/* 9 x 9 state marks (rows MSB first, 9 bits) */
static const uint16_t mark_ok[9] = {0x001, 0x003, 0x006, 0x00c, 0x118, 0x1b0, 0x0e0, 0x040, 0x000};
static const uint16_t mark_fail[9] = {0x000, 0x101, 0x082, 0x044, 0x028, 0x044, 0x082, 0x101, 0x000};
static const uint16_t mark_wait[9] = {0x000, 0x000, 0x000, 0x000, 0x000, 0x000, 0x000, 0x124, 0x124};
static const uint16_t mark_skip[9] = {0x000, 0x000, 0x000, 0x000, 0x0fe, 0x000, 0x000, 0x000, 0x000};

static void mark(struct canvas *c, int x, int y, int state)
{
    const uint16_t *m = state == RB_DIAG_OK ? mark_ok : state == RB_DIAG_FAIL ? mark_fail
                      : state == RB_DIAG_SKIP ? mark_skip : mark_wait;
    uint32_t col = state == RB_DIAG_OK ? C_GREEN : state == RB_DIAG_FAIL ? C_RED : C_GRAY;
    for (int r = 0; r < 9; r++)
        for (int b = 0; b < 9; b++)
            if (m[r] & (0x100 >> b)) {
                fill(c, x + b, y + r, 1, 1, col);
                if (state == RB_DIAG_OK || state == RB_DIAG_FAIL)
                    fill(c, x + b, y + r + 1, 1, 1, col); /* bold */
            }
}

static void window(struct canvas *c, int x, int y, int w, int h, const char *title)
{
    /* drop shadow, frame, title bar with Platinum stripes, close box */
    fill(c, x + 2, y + 2, w, h, C_BLACK);
    fill(c, x, y, w, h, C_BLACK);
    fill(c, x + 1, y + 1, w - 2, h - 2, C_WHITE);
    int tb = 19;
    fill(c, x + 1, y + 1, w - 2, tb - 1, C_TITLE);
    hline(c, x + 1, y + 1, w - 2, C_WHITE);
    vline(c, x + 1, y + 1, tb - 1, C_WHITE);
    hline(c, x + 1, y + tb - 1, w - 2, C_STRIPE_D);
    hline(c, x, y + tb, w, C_BLACK);
    const rbf_font *f = font_of(RB_DIAG_FONT_SYSTEM);
    int tw = width_n(f, title, strlen(title));
    if (tw > w - 60)
        tw = w - 60;
    int tx = x + (w - tw) / 2;
    for (int r = 4; r <= 14; r += 2) {
        int sx0 = x + 6, sx1 = x + w - 6;
        int gap0 = tx - 8, gap1 = tx + tw + 8;
        for (int sx = sx0; sx < sx1; sx++) {
            if (sx >= gap0 && sx < gap1)
                continue;
            if (sx >= x + 8 && sx < x + 21) /* the close box */
                continue;
            fill(c, sx, y + r, 1, 1, C_STRIPE_D);
            fill(c, sx + 1, y + r + 1, 1, 1, C_STRIPE_L);
        }
    }
    /* close box: 11 x 11 bevelled square */
    int bx = x + 9, by = y + 4;
    fill(c, bx, by, 11, 11, C_BLACK);
    fill(c, bx + 1, by + 1, 9, 9, C_TITLE);
    hline(c, bx + 1, by + 1, 8, C_WHITE);
    vline(c, bx + 1, by + 1, 8, C_WHITE);
    hline(c, bx + 2, by + 9, 8, C_STRIPE_D);
    vline(c, bx + 9, by + 2, 8, C_STRIPE_D);
    text_n(c, f, tx, y + 14, title, strlen(title), C_BLACK, tw);
}

struct wrap_ctx {
    struct canvas *c;
    const rbf_font *f;
    int x, y, lh, maxw;
    uint32_t col;
};

static void wrap_draw(void *ctx, const char *s, size_t n, int line)
{
    struct wrap_ctx *w = ctx;
    int l = line < 0 ? -1 - line : line;
    int y = w->y + l * w->lh;
    if (line < 0) {
        char tmp[RB_DIAG_LINE];
        size_t k = n < sizeof tmp - 1 ? n : sizeof tmp - 1;
        memcpy(tmp, s, k);
        tmp[k] = 0;
        text_fit(w->c, w->f, w->x, y, tmp, w->col, w->maxw);
    } else {
        text_n(w->c, w->f, w->x, y, s, n, w->col, w->maxw);
    }
}

static int count_tag(const struct rb_diag_doc *d, char tag)
{
    int n = 0;
    for (int i = 0; i < d->n; i++)
        n += d->line[i].tag == tag;
    return n;
}

static void heading(struct canvas *c, int x, int y, const char *s)
{
    text_n(c, font_of(RB_DIAG_FONT_SYSTEM), x, y, s, strlen(s), C_BLACK, -1);
}

/* A column of wrapped items (P or D lines) from y to at most ybot; returns
 * the y below the last line drawn. When they do not all fit, the last line
 * says how many more the saved report has. */
static int wrapped_items(struct canvas *c, const struct rb_diag_doc *d, char tag, int x, int y, int maxw, int ybot,
                         int per_item_lines, uint32_t bullet)
{
    const rbf_font *f = font_of(RB_DIAG_FONT_REGULAR);
    int lh = f->ascent + f->descent - 2;
    int total = 0, need_all = 0;
    for (int i = 0; i < d->n; i++)
        if (d->line[i].tag == tag) {
            total++;
            need_all += wrap(f, d->line[i].text, maxw - 10, per_item_lines, NULL, NULL);
        }
    bool all = y + need_all * lh <= ybot;
    int limit = all ? ybot : ybot - lh; /* keep a line for "and N more" */
    int shown = 0;
    for (int i = 0; i < d->n; i++) {
        const struct rb_diag_line *l = &d->line[i];
        if (l->tag != tag)
            continue;
        int need = wrap(f, l->text, maxw - 10, per_item_lines, NULL, NULL);
        if (y + need * lh > limit) {
            int room = (limit - y) / lh;
            if (room < 1 || shown + 1 < total)
                break;
            need = room; /* the last item: as much of it as fits */
        }
        fill(c, x + 2, y + f->ascent - 7, 3, 3, bullet);
        struct wrap_ctx w = {c, f, x + 10, y + f->ascent - 2, lh, maxw - 10, C_BLACK};
        wrap(f, l->text, maxw - 10, need, wrap_draw, &w);
        y += need * lh;
        shown++;
    }
    if (shown < total && y + lh <= ybot) {
        char more[80];
        snprintf(more, sizeof more, "\xe2\x80\xa6 and %d more in the saved report", total - shown);
        text_fit(c, f, x + 10, y + f->ascent - 2, more, C_GRAY, maxw - 10);
        y += lh;
    }
    return y;
}

static void render_logical(struct canvas *c, int lw, int lh, const struct rb_diag_doc *d, const char *keys)
{
    desktop(c);
    int m = lw >= 800 && lh >= 560 ? 16 : 6;
    int wx = m, wy = m, ww = lw - 2 * m - 2, wh = lh - 2 * m - 2;
    if (ww < 200 || wh < 120) { /* tiny frame: a plain page */
        wx = wy = 0;
        ww = lw;
        wh = lh;
    }
    window(c, wx, wy, ww, wh, d && d->title[0] ? d->title : "Startup Details");
    const rbf_font *reg = font_of(RB_DIAG_FONT_REGULAR), *sys = font_of(RB_DIAG_FONT_SYSTEM),
                   *sml = font_of(RB_DIAG_FONT_SMALL);
    int rlh = reg->ascent + reg->descent - 2; /* 14 */
    int slh = sml->ascent + sml->descent - 1; /* 11 */
    int x0 = wx + 12, x1 = wx + ww - 12, cw = x1 - x0;
    int y = wy + 20 + 6;
    int ybot = wy + wh - 6;
    if (!d || d->n == 0) {
        const char *wait = "Collecting startup details\xe2\x80\xa6";
        text_n(c, reg, x0, y + reg->ascent, wait, strlen(wait), C_BLACK, cw);
        return;
    }
    /* footer: where the report went, and the way back */
    int fy = ybot - 4;
    const char *back = keys && *keys ? keys : RB_DIAG_KEYS_BACK;
    int bw = width_n(sys, back, strlen(back));
    text_n(c, sys, x1 - bw, fy, back, strlen(back), C_BLACK, -1);
    for (int i = 0; i < d->n; i++)
        if (d->line[i].tag == 'F') {
            text_fit(c, reg, x0, fy, d->line[i].text, C_BLACK, cw - bw - 16);
            break;
        }
    ybot = fy - reg->ascent - 4;
    hline(c, x0, ybot + 1, cw, C_RULE);
    ybot -= 3;

    /* header */
    bool first = true;
    for (int i = 0; i < d->n; i++) {
        const struct rb_diag_line *l = &d->line[i];
        if (l->tag != 'H')
            continue;
        if (y + rlh > ybot)
            break;
        text_fit(c, first ? sys : reg, x0, y + reg->ascent - 2, l->text, C_BLACK, cw);
        y += rlh;
        first = false;
    }
    y += 4;
    hline(c, x0, y, cw, C_RULE);
    y += 6;

    /* columns: the checklist on the left; problems and display on the right */
    bool two = cw >= 560;
    int lcw = cw * 2 / 5;
    lcw = !two ? cw : lcw < 260 ? 260 : lcw > 400 ? 400 : lcw;
    int rx = two ? x0 + lcw + 16 : x0, rcw = two ? x1 - rx : cw;
    int nlog = count_tag(d, 'L');
    int log_min = nlog ? 16 + slh * (nlog < 6 ? nlog : 6) : 0; /* keep room for some messages */
    int col_bot = ybot - log_min;
    int ly = y;
    heading(c, x0, ly + sys->ascent - 2, "Startup");
    ly += rlh + 2;
    for (int i = 0; i < d->n; i++) {
        const struct rb_diag_line *l = &d->line[i];
        if (l->tag != 'S')
            continue;
        if (ly + rlh > col_bot)
            break;
        mark(c, x0, ly + 2, l->state);
        int tw = width_n(reg, l->time, strlen(l->time));
        if (l->time[0])
            text_n(c, reg, x0 + lcw - tw, ly + reg->ascent - 2, l->time, strlen(l->time), C_GRAY, -1);
        uint32_t col = l->state == RB_DIAG_FAIL ? C_RED : l->state == RB_DIAG_SKIP ? C_GRAY : C_BLACK;
        text_fit(c, reg, x0 + 14, ly + reg->ascent - 2, l->text, col, lcw - 14 - (tw ? tw + 8 : 0));
        ly += rlh;
    }
    int ry = two ? y : ly + 6;
    heading(c, rx, ry + sys->ascent - 2, "Problems");
    ry += rlh + 2;
    if (!count_tag(d, 'P')) {
        if (ry + rlh <= col_bot)
            text_n(c, reg, rx + 10, ry + reg->ascent - 2, "None found so far.", 18, C_GRAY, rcw);
        ry += rlh;
    } else {
        int disp = count_tag(d, 'D');
        int pbot = col_bot - (disp ? rlh + 6 + rlh * (disp < 3 ? disp : 3) : 0);
        ry = wrapped_items(c, d, 'P', rx, ry, rcw, pbot, 3, C_RED);
    }
    if (count_tag(d, 'D') && ry + 2 * rlh <= col_bot) {
        ry += 6;
        heading(c, rx, ry + sys->ascent - 2, "Display and keyboard");
        ry += rlh + 2;
        ry = wrapped_items(c, d, 'D', rx, ry, rcw, col_bot, 2, C_GRAY);
    }
    y = (two ? (ly > ry ? ly : ry) : ry) + 4;

    /* the latest log lines, newest at the bottom */
    if (nlog && y + 16 + slh <= ybot) {
        hline(c, x0, y, cw, C_RULE);
        y += 4;
        heading(c, x0, y + sys->ascent - 2, "Latest messages");
        y += rlh + 2;
        int room = (ybot - y) / slh;
        int skip = nlog > room ? nlog - room : 0, k = 0;
        for (int i = 0; i < d->n; i++) {
            const struct rb_diag_line *l = &d->line[i];
            if (l->tag != 'L')
                continue;
            if (k++ < skip)
                continue;
            text_fit(c, sml, x0, y + sml->ascent - 1, l->text, C_BLACK, cw);
            y += slh;
        }
    }
}

/* Upscale the logical canvas into the frame (every logical pixel s x s). */
static void blit_scaled(const struct canvas *c, uint32_t *px, int w, int h, int stride, int s, int x0, int y0)
{
    for (int y = 0; y < h; y++) {
        int ly = (y0 + y) / s;
        if (ly >= c->h)
            ly = c->h - 1;
        const uint32_t *src = c->px + (size_t)ly * c->w;
        uint32_t *dst = px + (size_t)y * stride;
        if (s == 1 && x0 == 0) {
            memcpy(dst, src, (size_t)(w < c->w ? w : c->w) * 4);
            continue;
        }
        for (int x = 0; x < w; x++) {
            int lx = (x0 + x) / s;
            dst[x] = src[lx < c->w ? lx : c->w - 1];
        }
    }
}

void rb_diag_render(uint32_t *px, int w, int h, int stride, int s, const struct rb_diag_doc *d)
{
    rb_diag_render_keys(px, w, h, stride, s, d, NULL);
}

void rb_diag_render_keys(uint32_t *px, int w, int h, int stride, int s, const struct rb_diag_doc *d,
                         const char *keys)
{
    if (!px || w <= 0 || h <= 0)
        return;
    if (s < 1)
        s = 1;
    if (s > 8)
        s = 8;
    struct canvas c = {NULL, (w + s - 1) / s, (h + s - 1) / s};
    c.px = malloc((size_t)c.w * c.h * 4);
    if (!c.px) { /* out of memory: a white page at least */
        for (int y = 0; y < h; y++)
            for (int x = 0; x < w; x++)
                px[(size_t)y * stride + x] = C_WHITE;
        return;
    }
    render_logical(&c, w / s, h / s, d, keys);
    blit_scaled(&c, px, w, h, stride, s, 0, 0);
    free(c.px);
}

#define HINT_H 22
rb_rect rb_diag_hint_rect(int w, int h, int s)
{
    if (s < 1)
        s = 1;
    int lw = w / s, lh = h / s;
    int bw = rb_diag_text_width(RB_DIAG_FONT_SYSTEM, RB_DIAG_HINT_TEXT) + 24 + 2;
    int bh = HINT_H + 2;
    if (bw > lw)
        bw = lw;
    int bx = (lw - bw) / 2, by = lh - bh - (lh >= 400 ? 24 : 4);
    if (by < 0)
        by = 0;
    return (rb_rect){bx * s, by * s, bw * s, bh * s};
}

void rb_diag_render_hint(uint32_t *px, int w, int h, int stride, int s)
{
    if (s < 1)
        s = 1;
    rb_rect r = rb_diag_hint_rect(w, h, s);
    struct canvas c = {NULL, r.w / s, r.h / s};
    if (c.w <= 0 || c.h <= 0 || r.x + r.w > w || r.y + r.h > h)
        return;
    c.px = malloc((size_t)c.w * c.h * 4);
    if (!c.px)
        return;
    /* a help tag: white box, black frame, one-pixel shadow (the box is drawn
     * over the checkerboard, so the shadow's corner pixels keep it) */
    for (int i = 0; i < c.w * c.h; i++)
        c.px[i] = C_WHITE;
    int bw = c.w - 2, bh = c.h - 2;
    fill(&c, 0, 0, bw, bh, C_BLACK);
    fill(&c, 1, 1, bw - 2, bh - 2, C_WHITE);
    fill(&c, 2, bh, bw, 2, C_BLACK);
    fill(&c, bw, 2, 2, bh, C_BLACK);
    const rbf_font *f = font_of(RB_DIAG_FONT_SYSTEM);
    int tw = width_n(f, RB_DIAG_HINT_TEXT, strlen(RB_DIAG_HINT_TEXT));
    text_n(&c, f, (bw - tw) / 2, 15, RB_DIAG_HINT_TEXT, strlen(RB_DIAG_HINT_TEXT), C_BLACK, bw - 4);
    /* the two shadow corners show what was there (no shadow beyond the box) */
    for (int y = 0; y < r.h; y++) {
        int ly = y / s;
        uint32_t *dst = px + (size_t)(r.y + y) * stride + r.x;
        for (int x = 0; x < r.w; x++) {
            int lx = x / s;
            bool corner = (lx >= bw && ly < 2) || (ly >= bh && lx < 2);
            if (!corner)
                dst[x] = c.px[(size_t)ly * c.w + lx];
        }
    }
    free(c.px);
}

/* ------------------------------------------------------------------------ */
/* the drawers' side                                                         */

static void mkdirs(void)
{
    mkdir("/run/retroboot", 0755);
    mkdir(RB_DIAG_DIR, 0755);
}

int rb_diag_watch_init(struct rb_diag_watch *w)
{
    memset(w, 0, sizeof *w);
    w->fd = -1;
    rb_diag_boot_cfg(&w->cfg);
    if (!w->cfg.enabled)
        return -1;
    w->doc = calloc(1, sizeof *w->doc);
    if (!w->doc)
        return -1;
    rb_diag_doc_init(w->doc, NULL);
    mkdirs();
    w->fd = inotify_init1(IN_NONBLOCK | IN_CLOEXEC);
    if (w->fd >= 0 &&
        inotify_add_watch(w->fd, RB_DIAG_DIR, IN_CREATE | IN_DELETE | IN_MOVED_TO | IN_MOVED_FROM | IN_CLOSE_WRITE) < 0) {
        close(w->fd);
        w->fd = -1;
    }
    return w->fd;
}

int rb_diag_watch_update(struct rb_diag_watch *w)
{
    if (!w->cfg.enabled || !w->doc)
        return 0;
    char ev[4096];
    if (w->fd >= 0)
        while (read(w->fd, ev, sizeof ev) > 0) {
        }
    int chg = 0;
    bool vis = access(RB_DIAG_VISIBLE, F_OK) == 0, hint = access(RB_DIAG_HINT, F_OK) == 0;
    if (vis != w->visible)
        chg |= RB_DIAG_CHG_VISIBLE;
    if (hint != w->hint)
        chg |= RB_DIAG_CHG_HINT;
    w->visible = vis;
    w->hint = hint;
    static char buf[64 * 1024];
    ssize_t n = rb_diag_read_file(RB_DIAG_VIEW, buf, sizeof buf);
    if (n > 0) {
        static struct rb_diag_doc nd;
        rb_diag_doc_parse(buf, (size_t)n, &nd);
        if (memcmp(&nd, w->doc, sizeof nd)) {
            *w->doc = nd;
            w->doc_gen++;
            chg |= RB_DIAG_CHG_DOC;
        }
    }
    return chg;
}

void rb_diag_watch_free(struct rb_diag_watch *w)
{
    if (w->fd >= 0)
        close(w->fd);
    w->fd = -1;
    free(w->doc);
    w->doc = NULL;
}

int rb_diag_set_visible(bool on)
{
    if (!on)
        return unlink(RB_DIAG_VISIBLE) == 0 || errno == ENOENT ? 0 : -1;
    mkdirs();
    int fd = open(RB_DIAG_VISIBLE, O_WRONLY | O_CREAT | O_CLOEXEC, 0644);
    if (fd < 0)
        return -1;
    close(fd);
    return 0;
}

static uint64_t clock_ms(clockid_t id)
{
    struct timespec ts;
    clock_gettime(id, &ts);
    return (uint64_t)ts.tv_sec * 1000u + (uint64_t)ts.tv_nsec / 1000000u;
}

void rb_diag_publish_flips(const char *who, const struct rb_diag_flips *f)
{
    uint64_t boot = clock_ms(CLOCK_BOOTTIME), mono = clock_ms(CLOCK_MONOTONIC);
    uint64_t off = boot > mono ? boot - mono : 0; /* time spent suspended */
    char path[128], buf[512];
    snprintf(path, sizeof path, "%s/flips-%s", RB_DIAG_DIR, who);
    int n = snprintf(buf, sizeof buf,
                     "stalls=%u\nslow_commits=%u\nfirst_ms=%llu\nlast_ms=%llu\nslow_max_ms=%llu\nconnector=%s\n"
                     "driver=%s\n",
                     f->stalls, f->slow_commits,
                     (unsigned long long)(f->first_mono_ms ? f->first_mono_ms + off : 0),
                     (unsigned long long)(f->last_mono_ms ? f->last_mono_ms + off : 0),
                     (unsigned long long)f->slow_max_ms, f->connector, f->driver);
    if (n > 0)
        rb_diag_write_atomic(path, buf, (size_t)n < sizeof buf ? (size_t)n : sizeof buf - 1);
}

void rb_diag_heartbeat(void) { utimensat(AT_FDCWD, RB_DIAG_PRESENTER, NULL, 0); }

int rb_diag_write_atomic(const char *path, const char *data, size_t len)
{
    char tmp[PATH_MAX];
    snprintf(tmp, sizeof tmp, "%s.tmp%d", path, (int)getpid());
    int fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644);
    if (fd < 0)
        return -1;
    size_t off = 0;
    while (off < len) {
        ssize_t k = write(fd, data + off, len - off);
        if (k < 0 && errno == EINTR)
            continue;
        if (k <= 0) {
            close(fd);
            unlink(tmp);
            return -1;
        }
        off += (size_t)k;
    }
    if (close(fd) < 0 || rename(tmp, path) < 0) {
        unlink(tmp);
        return -1;
    }
    return 0;
}

ssize_t rb_diag_read_file(const char *path, char *buf, size_t cap)
{
    if (!cap)
        return -1;
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) {
        buf[0] = 0;
        return -1;
    }
    size_t n = 0;
    while (n < cap - 1) {
        ssize_t k = read(fd, buf + n, cap - 1 - n);
        if (k < 0 && errno == EINTR)
            continue;
        if (k <= 0)
            break;
        n += (size_t)k;
    }
    close(fd);
    buf[n] = 0;
    return (ssize_t)n;
}
