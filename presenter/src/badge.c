#include "badge.h"

#include <stdlib.h>
#include <string.h>

/* '#' black, 'w' white, '.' transparent (the box face in box mode). */
static const char *const glyph_control[RB_BADGE_H] = {
    "................",
    ".##############.",
    ".#wwwwwwwwwwww#.",
    ".#w#wwwwwwwwww#.",
    ".#w##wwwwwwwww#.",
    ".#w###wwwwwwww#.",
    ".#w####wwwwwww#.",
    ".#w#####wwwwww#.",
    ".#w######wwwww#.",
    ".#w###wwwwwwww#.",
    ".#w#ww#wwwwwww#.",
    ".##############.",
    "......####......",
    "....########....",
    "................",
    "................",
};

static const char *const glyph_view[RB_BADGE_H] = {
    "................",
    ".##############.",
    ".#wwwwwwwwwwww#.",
    ".#wwwwwwwwwwww#.",
    ".#wwww####wwww#.",
    ".#ww##w##w##ww#.",
    ".#w#ww####ww#w#.",
    ".#ww##w##w##ww#.",
    ".#wwww####wwww#.",
    ".#wwwwwwwwwwww#.",
    ".#wwwwwwwwwwww#.",
    ".##############.",
    "......####......",
    "....########....",
    "................",
    "................",
};

#define BLACK 0x00000000u
#define WHITE 0x00ffffffu
#define FACE 0x00ddddddu

static int cmp_u64(const void *a, const void *b)
{
    uint64_t x = *(const uint64_t *)a, y = *(const uint64_t *)b;
    return x < y ? -1 : x > y;
}

static int lum(uint32_t c) { return (int)(((c >> 16) & 0xff) * 3 + ((c >> 8) & 0xff) * 6 + (c & 0xff)) / 10; }

int rb_badge_menubar_gap(const uint32_t *px, int w, int h, int stride_px, int margin)
{
    if (!px || w < 200 || h < 100 || w > 8192)
        return -1;
    uint64_t *col = malloc((size_t)w * sizeof *col);
    uint64_t *sorted = malloc((size_t)w * sizeof *sorted);
    if (!col || !sorted) {
        free(col);
        free(sorted);
        return -1;
    }
    for (int x = 0; x < w; x++) {
        uint64_t hsh = 1469598103934665603ull;
        for (int y = 0; y < RB_BADGE_MENU_H; y++) {
            hsh ^= px[(size_t)y * stride_px + x] & 0xffffffu;
            hsh *= 1099511628211ull;
        }
        col[x] = sorted[x] = hsh;
    }
    qsort(sorted, (size_t)w, sizeof *sorted, cmp_u64);
    uint64_t mode = sorted[0];
    int best = 0;
    for (int i = 0; i < w;) {
        int j = i;
        while (j < w && sorted[j] == sorted[i])
            j++;
        if (j - i > best) {
            best = j - i;
            mode = sorted[i];
        }
        i = j;
    }
    free(sorted);
    int result = -1;
    int ref = -1;
    for (int x = 0; x < w && ref < 0; x++)
        if (col[x] == mode)
            ref = x;
    /* The background must be most of the bar and look like a menu bar: a
     * flat light band (rows 1..17) over a dark bottom edge (row 19). */
    bool bar = best * 4 >= w && ref >= 0;
    if (bar) {
        uint32_t face = px[(size_t)1 * stride_px + ref] & 0xffffffu;
        for (int y = 2; y < RB_BADGE_MENU_H - 2 && bar; y++)
            bar = (px[(size_t)y * stride_px + ref] & 0xffffffu) == face;
        bar = bar && lum(face) >= 128 && lum(px[(size_t)(RB_BADGE_MENU_H - 1) * stride_px + ref]) < 64;
    }
    if (bar) {
        int run_start = -1, best_len = 0, best_end = -1;
        for (int x = 0; x <= w; x++) {
            bool empty = x < w && col[x] == mode;
            if (empty && run_start < 0)
                run_start = x;
            if (!empty && run_start >= 0) {
                if (x - run_start > best_len) {
                    best_len = x - run_start;
                    best_end = x;
                }
                run_start = -1;
            }
        }
        if (best_len >= RB_BADGE_W + 2 * margin)
            result = best_end - margin - RB_BADGE_W;
    }
    free(col);
    return result;
}

void rb_badge_render(uint32_t *dst, int stride_px, rb_rect target, rb_rect clip, enum rb_badge_style style, bool box)
{
    rb_rect r;
    if (rb_rect_empty(target) || !rb_rect_intersect(target, clip, &r))
        return;
    const char *const *g = style == RB_BADGE_CONTROL ? glyph_control : glyph_view;
    int gw = box ? RB_BADGE_BOX : RB_BADGE_W, gh = box ? RB_BADGE_BOX : RB_BADGE_H;
    int off = box ? (RB_BADGE_BOX - RB_BADGE_W) / 2 : 0;
    for (int y = r.y; y < r.y + r.h; y++) {
        int by = (int)((long)(y - target.y) * gh / target.h);
        uint32_t *row = dst + (size_t)y * stride_px;
        for (int x = r.x; x < r.x + r.w; x++) {
            int bx = (int)((long)(x - target.x) * gw / target.w);
            char c = '.';
            if (box && (bx == 0 || by == 0 || bx == gw - 1 || by == gh - 1))
                c = '#';
            else if (bx - off >= 0 && bx - off < RB_BADGE_W && by - off >= 0 && by - off < RB_BADGE_H)
                c = g[by - off][bx - off];
            if (c == '#')
                row[x] = BLACK;
            else if (c == 'w')
                row[x] = WHITE;
            else if (box)
                row[x] = FACE;
        }
    }
}
