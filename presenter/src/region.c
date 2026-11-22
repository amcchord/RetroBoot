#include "region.h"

#include <stddef.h>

static int imin(int a, int b) { return a < b ? a : b; }
static int imax(int a, int b) { return a > b ? a : b; }

bool rb_rect_intersect(rb_rect a, rb_rect b, rb_rect *out)
{
    int x0 = imax(a.x, b.x), y0 = imax(a.y, b.y);
    int x1 = imin(a.x + a.w, b.x + b.w), y1 = imin(a.y + a.h, b.y + b.h);
    if (x1 <= x0 || y1 <= y0) {
        if (out)
            *out = (rb_rect){0, 0, 0, 0};
        return false;
    }
    if (out)
        *out = (rb_rect){x0, y0, x1 - x0, y1 - y0};
    return true;
}

rb_rect rb_rect_union(rb_rect a, rb_rect b)
{
    if (rb_rect_empty(a))
        return b;
    if (rb_rect_empty(b))
        return a;
    int x0 = imin(a.x, b.x), y0 = imin(a.y, b.y);
    int x1 = imax(a.x + a.w, b.x + b.w), y1 = imax(a.y + a.h, b.y + b.h);
    return (rb_rect){x0, y0, x1 - x0, y1 - y0};
}

rb_rect rb_rect_clip(rb_rect r, int w, int h)
{
    rb_rect out;
    rb_rect_intersect(r, (rb_rect){0, 0, w, h}, &out);
    return out;
}

void rb_region_clear(rb_region *g) { g->n = 0; }

static long area(rb_rect r) { return rb_rect_empty(r) ? 0 : (long)r.w * r.h; }

static bool touches(rb_rect a, rb_rect b)
{
    /* Overlapping or edge-adjacent rectangles merge when the union is tight. */
    rb_rect grown = {a.x - 1, a.y - 1, a.w + 2, a.h + 2};
    if (!rb_rect_intersect(grown, b, NULL))
        return false;
    rb_rect u = rb_rect_union(a, b);
    return area(u) <= area(a) + area(b) + (area(u) >> 3);
}

void rb_region_add(rb_region *g, rb_rect r)
{
    if (rb_rect_empty(r))
        return;
    for (;;) {
        bool merged = false;
        for (int i = 0; i < g->n; i++) {
            rb_rect inter;
            if (rb_rect_intersect(g->r[i], r, &inter) && inter.w == r.w && inter.h == r.h)
                return; /* already covered */
            if (touches(g->r[i], r)) {
                r = rb_rect_union(g->r[i], r);
                g->r[i] = g->r[--g->n];
                merged = true;
                break;
            }
        }
        if (!merged)
            break;
    }
    if (g->n < RB_REGION_MAX) {
        g->r[g->n++] = r;
        return;
    }
    /* Full: merge the new rect with the existing one that wastes least. */
    int best = 0;
    long best_cost = -1;
    for (int i = 0; i < g->n; i++) {
        long cost = area(rb_rect_union(g->r[i], r)) - area(g->r[i]) - area(r);
        if (best_cost < 0 || cost < best_cost) {
            best_cost = cost;
            best = i;
        }
    }
    rb_rect u = rb_rect_union(g->r[best], r);
    g->r[best] = g->r[--g->n];
    rb_region_add(g, u);
}

void rb_region_add_region(rb_region *g, const rb_region *src)
{
    for (int i = 0; i < src->n; i++)
        rb_region_add(g, src->r[i]);
}

rb_rect rb_region_bounds(const rb_region *g)
{
    rb_rect b = {0, 0, 0, 0};
    for (int i = 0; i < g->n; i++)
        b = rb_rect_union(b, g->r[i]);
    return b;
}

long rb_region_area(const rb_region *g)
{
    long a = 0;
    for (int i = 0; i < g->n; i++)
        a += area(g->r[i]);
    return a;
}
