#include "xform.h"

#include <math.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>

#include "checker.h"

int rb_rotation_from_orientation(const char *name)
{
    if (!name)
        return -1;
    if (!strcmp(name, "Normal"))
        return 0;
    if (!strcmp(name, "Upside Down"))
        return 180;
    if (!strcmp(name, "Left Side Up"))
        return 270; /* DRM_MODE_ROTATE_90 / FB_ROTATE_CCW */
    if (!strcmp(name, "Right Side Up"))
        return 90; /* DRM_MODE_ROTATE_270 / FB_ROTATE_CW */
    return -1;
}

const char *rb_rotation_name(int rot)
{
    switch (rot) {
    case 90: return "right-side-up";
    case 180: return "upside-down";
    case 270: return "left-side-up";
    default: return "normal";
    }
}

void rb_rotated_size(int rot, int fb_w, int fb_h, int *ow, int *oh)
{
    bool swap = rot == 90 || rot == 270;
    *ow = swap ? fb_h : fb_w;
    *oh = swap ? fb_w : fb_h;
}

void rb_xform_free(struct rb_xform *x)
{
    free(x->xmap);
    free(x->ymap);
    x->xmap = x->ymap = NULL;
}

int rb_xform_setup(struct rb_xform *x, int rot, int fb_w, int fb_h, int src_w, int src_h, bool mirror)
{
    rb_xform_free(x);
    int border = x->border, cs = x->checker_s, ss = x->src_scale;
    struct rb_checker_anchor ca = x->checker_anchor;
    memset(x, 0, sizeof *x);
    x->src_scale = ss;
    x->border = border;
    x->checker_s = cs > 0 ? cs : 1;
    x->checker_anchor = ca;
    if (rot != 90 && rot != 180 && rot != 270)
        rot = 0;
    x->rot = rot;
    x->fb_w = fb_w;
    x->fb_h = fb_h;
    rb_rotated_size(rot, fb_w, fb_h, &x->ow, &x->oh);
    x->src_w = src_w;
    x->src_h = src_h;
    if (src_w <= 0 || src_h <= 0 || fb_w <= 0 || fb_h <= 0)
        return -1;
    if (!mirror || (src_w == x->ow && src_h == x->oh)) {
        x->k = 1;
        x->img = (rb_rect){0, 0, src_w < x->ow ? src_w : x->ow, src_h < x->oh ? src_h : x->oh};
    } else {
        int k = 1;
        while ((long)src_w * (k + 1) <= x->ow && (long)src_h * (k + 1) <= x->oh)
            k++;
        if (src_w > x->ow || src_h > x->oh) {
            /* Even 1x does not fit: an exact 1/d of a frame drawn at scale
             * src_scale when one fits, else a nearest-neighbour fit, aspect kept. */
            int d = 0;
            for (int c = 2; c <= ss && !d; c++)
                if (ss % c == 0 && src_w % c == 0 && src_h % c == 0 && src_w / c <= x->ow && src_h / c <= x->oh)
                    d = c;
            double f = (double)x->ow / src_w;
            if ((double)x->oh / src_h < f)
                f = (double)x->oh / src_h;
            int dw = (int)(src_w * f), dh = (int)(src_h * f);
            if (d) {
                dw = src_w / d;
                dh = src_h / d;
                x->div = d;
            }
            if (dw < 1)
                dw = 1;
            if (dh < 1)
                dh = 1;
            x->k = 0;
            x->img = (rb_rect){(x->ow - dw) / 2, (x->oh - dh) / 2, dw, dh};
            x->xmap = malloc(sizeof(int) * (size_t)dw);
            x->ymap = malloc(sizeof(int) * (size_t)dh);
            if (!x->xmap || !x->ymap) {
                rb_xform_free(x);
                return -1;
            }
            /* exact reduction: i*d + d/2, inside the i-th d x d block */
            for (int i = 0; i < dw; i++)
                x->xmap[i] = (int)(((long)i * src_w + src_w / 2) / dw);
            for (int i = 0; i < dh; i++)
                x->ymap[i] = (int)(((long)i * src_h + src_h / 2) / dh);
        } else {
            x->k = k;
            x->img = (rb_rect){(x->ow - src_w * k) / 2, (x->oh - src_h * k) / 2, src_w * k, src_h * k};
        }
    }
    x->identity = x->rot == 0 && x->k == 1 && x->img.x == 0 && x->img.y == 0 && x->img.w == x->ow &&
                  x->img.h == x->oh;
    return 0;
}

void rb_xform_set_src_scale(struct rb_xform *x, int s)
{
    x->src_scale = s > 1 ? s : 0;
}

void rb_xform_set_border(struct rb_xform *x, int border, int checker_s, struct rb_checker_anchor anchor)
{
    x->border = border;
    x->checker_s = checker_s > 0 ? checker_s : 1;
    x->checker_anchor = anchor;
}

void rb_xform_l2f(const struct rb_xform *x, int lx, int ly, int *fx, int *fy)
{
    switch (x->rot) {
    case 90: *fx = x->oh - 1 - ly; *fy = lx; break;
    case 180: *fx = x->ow - 1 - lx; *fy = x->oh - 1 - ly; break;
    case 270: *fx = ly; *fy = x->ow - 1 - lx; break;
    default: *fx = lx; *fy = ly; break;
    }
}

void rb_xform_f2l(const struct rb_xform *x, int fx, int fy, int *lx, int *ly)
{
    switch (x->rot) {
    case 90: *lx = fy; *ly = x->oh - 1 - fx; break;
    case 180: *lx = x->ow - 1 - fx; *ly = x->oh - 1 - fy; break;
    case 270: *lx = x->ow - 1 - fy; *ly = fx; break;
    default: *lx = fx; *ly = fy; break;
    }
}

rb_rect rb_xform_lrect_to_fb(const struct rb_xform *x, rb_rect r)
{
    switch (x->rot) {
    case 90: return (rb_rect){x->oh - r.y - r.h, r.x, r.h, r.w};
    case 180: return (rb_rect){x->ow - r.x - r.w, x->oh - r.y - r.h, r.w, r.h};
    case 270: return (rb_rect){r.y, x->ow - r.x - r.w, r.h, r.w};
    default: return r;
    }
}

/* Frame rect -> logical rect (image area), clipped to the frame. */
static rb_rect src_to_logical(const struct rb_xform *x, rb_rect r)
{
    r = rb_rect_clip(r, x->src_w, x->src_h);
    if (rb_rect_empty(r))
        return (rb_rect){0, 0, 0, 0};
    if (x->k > 0)
        return (rb_rect){x->img.x + r.x * x->k, x->img.y + r.y * x->k, r.w * x->k, r.h * x->k};
    /* fit: conservative destination span */
    long dw = x->img.w, dh = x->img.h;
    int x0 = (int)((long)r.x * dw / x->src_w) - 1, x1 = (int)(((long)(r.x + r.w) * dw + x->src_w - 1) / x->src_w) + 1;
    int y0 = (int)((long)r.y * dh / x->src_h) - 1, y1 = (int)(((long)(r.y + r.h) * dh + x->src_h - 1) / x->src_h) + 1;
    if (x0 < 0) x0 = 0;
    if (y0 < 0) y0 = 0;
    if (x1 > x->img.w) x1 = x->img.w;
    if (y1 > x->img.h) y1 = x->img.h;
    return (rb_rect){x->img.x + x0, x->img.y + y0, x1 - x0, y1 - y0};
}

rb_rect rb_xform_src_to_fb(const struct rb_xform *x, rb_rect r)
{
    rb_rect l = src_to_logical(x, r);
    if (rb_rect_empty(l))
        return l;
    return rb_rect_clip(rb_xform_lrect_to_fb(x, l), x->fb_w, x->fb_h);
}

void rb_xform_fb_to_frame(const struct rb_xform *x, double fx, double fy, double *sx, double *sy)
{
    double lx, ly;
    switch (x->rot) {
    case 90: lx = fy; ly = x->oh - 1 - fx; break;
    case 180: lx = x->ow - 1 - fx; ly = x->oh - 1 - fy; break;
    case 270: lx = x->ow - 1 - fy; ly = fx; break;
    default: lx = fx; ly = fy; break;
    }
    double u = lx - x->img.x, v = ly - x->img.y;
    if (x->img.w > 0 && x->img.h > 0) {
        u = u * x->src_w / x->img.w;
        v = v * x->src_h / x->img.h;
    }
    if (u < 0) u = 0;
    if (v < 0) v = 0;
    if (u > x->src_w - 1) u = x->src_w - 1;
    if (v > x->src_h - 1) v = x->src_h - 1;
    *sx = u;
    *sy = v;
}

static int clampi(int v, int lo, int hi) { return v < lo ? lo : (v > hi ? hi : v); }

void rb_xform_fb_point_to_frame(const struct rb_xform *x, double fx, double fy, int *sx, int *sy)
{
    *sx = *sy = 0;
    if (x->fb_w <= 0 || x->fb_h <= 0 || x->src_w <= 0 || x->src_h <= 0)
        return;
    /* libinput hands over i / range, which times the size can come out a hair
     * below i (439 / 800 * 800 = 438.99999999999994): a position within 1e-6
     * of a pixel's start is that pixel. */
    int px = clampi((int)floor(fx + 1e-6), 0, x->fb_w - 1), py = clampi((int)floor(fy + 1e-6), 0, x->fb_h - 1), lx, ly;
    rb_xform_f2l(x, px, py, &lx, &ly);
    int u = lx - x->img.x, v = ly - x->img.y;
    if (x->k > 0) {
        u = u >= 0 ? u / x->k : -1;
        v = v >= 0 ? v / x->k : -1;
    } else if (x->xmap && x->ymap && x->img.w > 0 && x->img.h > 0) {
        u = x->xmap[clampi(u, 0, x->img.w - 1)];
        v = x->ymap[clampi(v, 0, x->img.h - 1)];
    }
    *sx = clampi(u, 0, x->src_w - 1);
    *sy = clampi(v, 0, x->src_h - 1);
}

static inline int src_x(const struct rb_xform *x, int dx) { return x->k > 0 ? dx / x->k : x->xmap[dx]; }
static inline int src_y(const struct rb_xform *x, int dy) { return x->k > 0 ? dy / x->k : x->ymap[dy]; }

/* --- 1:1 blits (k = 1: every primary output, rotated or not) ------------------
 *
 * Scanout pixel (fx, fy) shows frame pixel src[o0 + fx * sx + fy * sy] with
 * (sx, sy) one of (1, S), (-S, 1), (-1, -S), (S, -1) for 0/90/180/270 (S = the
 * frame's stride). With |sx| = 1 a scanout row is a frame row, forwards or
 * backwards. With |sx| = S (90, 270: a panel mounted on its side, the Gemini
 * PDA's 1080x2160 Left Side Up) a scanout row is a frame column: the generic
 * loop read one frame row per pixel (a cache line each, 8640 bytes apart on a
 * 2160-pixel frame) with a division by k per pixel. Here the rectangle is cut
 * into 16x16 tiles whose 16 frame rows stay in L1, and on arm64 each tile is
 * moved as 4x4 blocks: four 16-byte loads, a NEON transpose, four 16-byte
 * stores (tests/test_xform.c: identical to the generic loop for every rotation,
 * offset and size; RB_XFORM_GENERIC=1 uses the generic loop, lab A/B). */
#if defined(__aarch64__) && defined(__ARM_NEON)
#include <arm_neon.h>
#define RB_XFORM_NEON 1
#endif

static int xform_generic = -1;

bool rb_xform_fast_enabled(void)
{
    if (xform_generic < 0) {
        const char *e = getenv("RB_XFORM_GENERIC");
        xform_generic = e && e[0] == '1';
    }
    return !xform_generic;
}

void rb_xform_force_generic(int on) { xform_generic = on ? 1 : 0; }

#define TILE 16

/* dst rows y0..y0+3, cols x0..x0+3 where dst(x, y) = src[o0 + x * sx + y * sy], |sx| = S, sy = +-1 */
static inline void block4(uint32_t *fb, int fbs, const uint32_t *src, ptrdiff_t o0, ptrdiff_t sx, ptrdiff_t sy, int x0,
                          int y0)
{
#ifdef RB_XFORM_NEON
    uint32x4_t q[4];
    for (int i = 0; i < 4; i++) {
        /* source column for dst column x0+i, lanes = dst rows y0..y0+3 */
        const uint32_t *row = src + o0 + (ptrdiff_t)(x0 + i) * sx + (ptrdiff_t)y0 * sy;
        if (sy > 0) {
            q[i] = vld1q_u32(row);
        } else {
            uint32x4_t v = vld1q_u32(row - 3); /* lanes y0+3 .. y0 */
            v = vrev64q_u32(v);
            q[i] = vextq_u32(v, v, 2);
        }
    }
    uint32x4x2_t t01 = vtrnq_u32(q[0], q[1]), t23 = vtrnq_u32(q[2], q[3]);
    uint32x4_t r0 = vcombine_u32(vget_low_u32(t01.val[0]), vget_low_u32(t23.val[0]));
    uint32x4_t r1 = vcombine_u32(vget_low_u32(t01.val[1]), vget_low_u32(t23.val[1]));
    uint32x4_t r2 = vcombine_u32(vget_high_u32(t01.val[0]), vget_high_u32(t23.val[0]));
    uint32x4_t r3 = vcombine_u32(vget_high_u32(t01.val[1]), vget_high_u32(t23.val[1]));
    vst1q_u32(fb + (size_t)y0 * fbs + x0, r0);
    vst1q_u32(fb + (size_t)(y0 + 1) * fbs + x0, r1);
    vst1q_u32(fb + (size_t)(y0 + 2) * fbs + x0, r2);
    vst1q_u32(fb + (size_t)(y0 + 3) * fbs + x0, r3);
#else
    for (int y = y0; y < y0 + 4; y++) {
        uint32_t *d = fb + (size_t)y * fbs;
        const uint32_t *s = src + o0 + (ptrdiff_t)y * sy;
        for (int x = x0; x < x0 + 4; x++)
            d[x] = s[(ptrdiff_t)x * sx];
    }
#endif
}

static void transpose_rect(uint32_t *fb, int fbs, const uint32_t *src, ptrdiff_t o0, ptrdiff_t sx, ptrdiff_t sy,
                           rb_rect f)
{
    int x4 = f.x + (f.w & ~3), y4 = f.y + (f.h & ~3);
    for (int ty = f.y; ty < y4; ty += TILE) {
        int ty1 = ty + TILE < y4 ? ty + TILE : y4;
        for (int tx = f.x; tx < x4; tx += TILE) {
            int tx1 = tx + TILE < x4 ? tx + TILE : x4;
            for (int y = ty; y < ty1; y += 4)
                for (int x = tx; x < tx1; x += 4)
                    block4(fb, fbs, src, o0, sx, sy, x, y);
        }
    }
    /* the ragged right and bottom edges */
    for (int y = f.y; y < f.y + f.h; y++) {
        uint32_t *d = fb + (size_t)y * fbs;
        const uint32_t *s = src + o0 + (ptrdiff_t)y * sy;
        for (int x = y < y4 ? x4 : f.x; x < f.x + f.w; x++)
            d[x] = s[(ptrdiff_t)x * sx];
    }
}

/* k = 1: returns false when not applicable (the generic loop runs). */
static bool blit_1to1(const struct rb_xform *x, uint32_t *fb, int fbs, const uint32_t *src, int S, rb_rect l)
{
    if (x->k != 1 || !rb_xform_fast_enabled())
        return false;
    rb_rect f = rb_xform_lrect_to_fb(x, l);
    if (rb_rect_empty(f))
        return true;
    ptrdiff_t ix = x->img.x, iy = x->img.y, ow = x->ow, oh = x->oh, St = S;
    ptrdiff_t o0, sx, sy;
    switch (x->rot) {
    case 90: o0 = (oh - 1 - iy) * St - ix; sx = -St; sy = 1; break;
    case 180: o0 = (oh - 1 - iy) * St + (ow - 1 - ix); sx = -1; sy = -St; break;
    case 270: o0 = -iy * St + (ow - 1 - ix); sx = St; sy = -1; break;
    default: o0 = -iy * St - ix; sx = 1; sy = St; break;
    }
    if (sx == 1) {
        for (int y = f.y; y < f.y + f.h; y++)
            memcpy(fb + (size_t)y * fbs + f.x, src + o0 + f.x + (ptrdiff_t)y * sy, (size_t)f.w * 4);
    } else if (sx == -1) {
        for (int y = f.y; y < f.y + f.h; y++) {
            uint32_t *d = fb + (size_t)y * fbs + f.x;
            const uint32_t *s = src + o0 - f.x + (ptrdiff_t)y * sy; /* d[0]'s source; then backwards */
            for (int i = 0; i < f.w; i++)
                d[i] = s[-i];
        }
    } else {
        transpose_rect(fb, fbs, src, o0, sx, sy, f);
    }
    return true;
}

void rb_xform_blit(const struct rb_xform *x, uint32_t *fb, int fb_stride_px, const uint32_t *src, int src_stride_px,
                   rb_rect r)
{
    rb_rect l = src_to_logical(x, r);
    if (rb_rect_empty(l))
        return;
    if (x->identity) {
        for (int y = l.y; y < l.y + l.h; y++)
            memcpy(fb + (size_t)y * fb_stride_px + l.x, src + (size_t)y * src_stride_px + l.x, (size_t)l.w * 4);
        return;
    }
    if (blit_1to1(x, fb, fb_stride_px, src, src_stride_px, l))
        return;
    rb_rect f = rb_xform_lrect_to_fb(x, l);
    for (int fy = f.y; fy < f.y + f.h; fy++) {
        uint32_t *row = fb + (size_t)fy * fb_stride_px;
        int lx, ly;
        rb_xform_f2l(x, f.x, fy, &lx, &ly);
        switch (x->rot) {
        case 0: {
            /* logical row ly, columns lx.. */
            const uint32_t *s = src + (size_t)src_y(x, ly - x->img.y) * src_stride_px;
            for (int fx = f.x; fx < f.x + f.w; fx++)
                row[fx] = s[src_x(x, fx - x->img.x)];
            break;
        }
        case 180: {
            const uint32_t *s = src + (size_t)src_y(x, ly - x->img.y) * src_stride_px;
            for (int fx = f.x; fx < f.x + f.w; fx++)
                row[fx] = s[src_x(x, x->ow - 1 - fx - x->img.x)];
            break;
        }
        case 90: {
            /* scanout row fy is logical column lx = fy; logical y = oh-1-fx */
            const uint32_t *s = src + src_x(x, lx - x->img.x);
            for (int fx = f.x; fx < f.x + f.w; fx++)
                row[fx] = s[(size_t)src_y(x, x->oh - 1 - fx - x->img.y) * src_stride_px];
            break;
        }
        case 270: {
            /* logical column lx = ow-1-fy; logical y = fx */
            const uint32_t *s = src + src_x(x, lx - x->img.x);
            for (int fx = f.x; fx < f.x + f.w; fx++)
                row[fx] = s[(size_t)src_y(x, fx - x->img.y) * src_stride_px];
            break;
        }
        }
    }
}

static uint32_t border_pixel(const struct rb_xform *x, int lx, int ly)
{
    if (x->border == RB_BORDER_OSX_GRAY)
        return 0x00c0c0c0u;
    if (x->border != RB_BORDER_CHECKER || x->k <= 0)
        return 0;
    /* Continue the frame's checkerboard (frame pixel u, v; cell s). */
    int u = rb_floordiv(lx - x->img.x, x->k), v = rb_floordiv(ly - x->img.y, x->k);
    return rb_checker_pixel_at(u, v, x->checker_s, x->checker_anchor);
}

void rb_xform_fill_border(const struct rb_xform *x, uint32_t *fb, int fb_stride_px)
{
    if (x->identity)
        return;
    rb_rect in = rb_xform_lrect_to_fb(x, x->img);
    for (int fy = 0; fy < x->fb_h; fy++) {
        uint32_t *row = fb + (size_t)fy * fb_stride_px;
        bool inside_rows = in.w > 0 && fy >= in.y && fy < in.y + in.h;
        for (int fx = 0; fx < x->fb_w; fx++) {
            if (inside_rows && fx == in.x) {
                fx = in.x + in.w - 1;
                continue;
            }
            int lx, ly;
            rb_xform_f2l(x, fx, fy, &lx, &ly);
            row[fx] = border_pixel(x, lx, ly);
        }
    }
}
