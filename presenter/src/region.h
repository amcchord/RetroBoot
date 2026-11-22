/* Rectangles and small damage regions. A region is a short list of
 * non-overlapping-ish rectangles; when it fills up, the pair whose union
 * wastes the least area is merged. Coverage is always conservative. */
#pragma once
#include <stdbool.h>

typedef struct {
    int x, y, w, h;
} rb_rect;

#define RB_REGION_MAX 24

typedef struct {
    int n;
    rb_rect r[RB_REGION_MAX];
} rb_region;

static inline bool rb_rect_empty(rb_rect r) { return r.w <= 0 || r.h <= 0; }
bool rb_rect_intersect(rb_rect a, rb_rect b, rb_rect *out);
rb_rect rb_rect_union(rb_rect a, rb_rect b);
rb_rect rb_rect_clip(rb_rect r, int w, int h);

void rb_region_clear(rb_region *g);
static inline bool rb_region_empty(const rb_region *g) { return g->n == 0; }
void rb_region_add(rb_region *g, rb_rect r);
void rb_region_add_region(rb_region *g, const rb_region *src);
rb_rect rb_region_bounds(const rb_region *g);
long rb_region_area(const rb_region *g);
