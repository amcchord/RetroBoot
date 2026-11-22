#include "framelog.h"

#include <errno.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>

int rb_framelog_open(struct rb_framelog *l, const char *path, int w, int h)
{
    memset(l, 0, sizeof *l);
    if (w <= 0 || h <= 0) {
        errno = EINVAL;
        return -1;
    }
    l->canvas = calloc((size_t)w * (size_t)h, 4);
    if (!l->canvas)
        return -1;
    l->f = fopen(path, "wbe");
    if (!l->f) {
        int e = errno;
        free(l->canvas);
        l->canvas = NULL;
        errno = e;
        return -1;
    }
    l->w = w;
    l->h = h;
    uint32_t version = RB_FRAMELOG_VERSION;
    if (fwrite("RBFL", 1, 4, l->f) != 4 || fwrite(&version, 4, 1, l->f) != 1)
        l->error = 1;
    l->bytes = 8;
    return 0;
}

struct buf {
    uint8_t *p;
    size_t n, cap;
    bool oom;
};

static void buf_put(struct buf *b, const void *p, size_t n)
{
    if (b->oom)
        return;
    if (b->n + n > b->cap) {
        size_t cap = b->cap ? b->cap : 65536;
        while (cap < b->n + n)
            cap *= 2;
        uint8_t *q = realloc(b->p, cap);
        if (!q) {
            b->oom = true;
            return;
        }
        b->p = q;
        b->cap = cap;
    }
    memcpy(b->p + b->n, p, n);
    b->n += n;
}

/* The bands of rect r whose rows differ from the canvas (every row with
 * `all`): appended to b and copied into the canvas. Rows a previous rect of
 * the same frame already updated compare equal, so overlaps are written once. */
static int bands_of(struct rb_framelog *l, struct buf *b, rb_rect r, const uint32_t *px, int stride_px, bool all)
{
    int count = 0;
    size_t rowb = (size_t)r.w * 4;
    int y = r.y;
    while (y < r.y + r.h) {
        if (!all && !memcmp(px + (size_t)y * stride_px + r.x, l->canvas + (size_t)y * l->w + r.x, rowb)) {
            y++;
            continue;
        }
        int y0 = y;
        while (y < r.y + r.h &&
               (all || memcmp(px + (size_t)y * stride_px + r.x, l->canvas + (size_t)y * l->w + r.x, rowb)))
            y++;
        int32_t hdr[4] = {r.x, y0, r.w, y - y0};
        buf_put(b, hdr, sizeof hdr);
        for (int yy = y0; yy < y; yy++) {
            const uint32_t *s = px + (size_t)yy * stride_px + r.x;
            buf_put(b, s, rowb);
            memcpy(l->canvas + (size_t)yy * l->w + r.x, s, rowb);
        }
        count++;
    }
    return count;
}

int rb_framelog_add(struct rb_framelog *l, uint64_t t_us, const uint32_t *px, int stride_px, const rb_rect *rects,
                    int n)
{
    if (!l->f || l->error)
        return -1;
    bool first = l->frames == 0, whole_frame = first || l->resized;
    rb_rect whole = {0, 0, l->w, l->h};
    if (whole_frame) {
        rects = &whole;
        n = 1;
    }
    struct buf b = {0};
    int bands = 0;
    for (int i = 0; i < n; i++) {
        rb_rect r = rb_rect_clip(rects[i], l->w, l->h);
        if (!rb_rect_empty(r))
            bands += bands_of(l, &b, r, px, stride_px, whole_frame);
    }
    if (b.oom) {
        free(b.p);
        l->error = 1;
        return -1;
    }
    uint32_t flags = (first ? RB_FRAMELOG_FIRST : 0) | (l->resized ? RB_FRAMELOG_RESIZE : 0);
    uint32_t dims[3] = {(uint32_t)l->w, (uint32_t)l->h, (uint32_t)bands};
    if (fwrite("FRAM", 1, 4, l->f) != 4 || fwrite(&flags, 4, 1, l->f) != 1 || fwrite(&t_us, 8, 1, l->f) != 1 ||
        fwrite(dims, 4, 3, l->f) != 3 || (b.n && fwrite(b.p, 1, b.n, l->f) != b.n))
        l->error = 1;
    l->bytes += 28 + b.n;
    free(b.p);
    l->frames++;
    l->resized = false;
    return l->error ? -1 : bands;
}

int rb_framelog_resize(struct rb_framelog *l, int w, int h)
{
    if (!l->f || l->error || w <= 0 || h <= 0)
        return -1;
    if (w == l->w && h == l->h)
        return 0;
    uint32_t *c = calloc((size_t)w * (size_t)h, 4);
    if (!c) {
        l->error = 1;
        return -1;
    }
    free(l->canvas);
    l->canvas = c;
    l->w = w;
    l->h = h;
    l->resized = l->frames > 0;
    return 0;
}

void rb_framelog_close(struct rb_framelog *l)
{
    if (l->f)
        fclose(l->f);
    free(l->canvas);
    memset(l, 0, sizeof *l);
}
