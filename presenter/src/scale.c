#include "scale.h"

#if defined(__x86_64__) && (defined(__GNUC__) || defined(__clang__))
#include <immintrin.h>
#define RB_SCALE_AVX2 __attribute__((target("avx2")))
#endif

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int rb_suggested_scale(int w, int h)
{
    int lng = w > h ? w : h, sht = w > h ? h : w;
    int s = 1;
    for (int c = 2; c <= 16; c++)
        if (lng >= 720 * c && sht >= 540 * c)
            s = c;
    return s;
}

void rb_guest_limits(int w, int h, int *min_w, int *min_h, int *max_w, int *max_h)
{
    bool portrait = h > w;
    *min_w = portrait ? RB_GUEST_PMIN_W : RB_GUEST_MIN_W;
    *min_h = portrait ? RB_GUEST_PMIN_H : RB_GUEST_MIN_H;
    *max_w = portrait ? RB_GUEST_PMAX_W : RB_GUEST_MAX_W;
    *max_h = portrait ? RB_GUEST_PMAX_H : RB_GUEST_MAX_H;
}

void rb_auto_guest_size(int w, int h, int s, int *gw, int *gh)
{
    if (s < 1)
        s = 1;
    *gw = (w / s / 8) * 8;
    *gh = h / s;
    /* The emulator's -g range (hostd uses the same rule for -g, so a new
     * guest starts at exactly this size). */
    int mnw, mnh, mxw, mxh;
    rb_guest_limits(w, h, &mnw, &mnh, &mxw, &mxh);
    if (*gw < mnw)
        *gw = mnw;
    if (*gw > mxw)
        *gw = mxw;
    if (*gh < mnh)
        *gh = mnh;
    if (*gh > mxh)
        *gh = mxh;
}

void rb_guest_size_for(int w, int h, int s, int cfg_w, int cfg_h, int *gw, int *gh)
{
    if (cfg_w <= 0 || cfg_h <= 0) {
        rb_auto_guest_size(w, h, s, gw, gh);
        return;
    }
    *gw = cfg_w;
    *gh = cfg_h;
    if (h > w && cfg_w > cfg_h && cfg_h % 8 == 0 && cfg_h >= RB_GUEST_PMIN_W && cfg_w >= RB_GUEST_PMIN_H) {
        *gw = cfg_h;
        *gh = cfg_w;
    }
}

void rb_scaled_guest_size(int w, int h, int s, int *gw, int *gh)
{
    if (s < 1)
        s = 1;
    *gw = (w / s / 8) * 8;
    *gh = h / s;
}

bool rb_pixel_scale_valid(int w, int h, int s)
{
    if (s < 1 || s > RB_PIXEL_SCALE_MAX)
        return false;
    int gw, gh, mnw, mnh, mxw, mxh;
    rb_scaled_guest_size(w, h, s, &gw, &gh);
    rb_guest_limits(w, h, &mnw, &mnh, &mxw, &mxh);
    return gw >= mnw && gh >= mnh && gw <= mxw && gh <= mxh;
}

int rb_effective_scale(int w, int h, int requested)
{
    if (requested <= 0)
        return rb_suggested_scale(w, h);
    if (requested > RB_PIXEL_SCALE_MAX)
        requested = RB_PIXEL_SCALE_MAX;
    /* the nearest valid scale, preferring smaller (larger Mac pixels never
     * push the Mac screen below 640x480) */
    for (int s = requested; s >= 1; s--)
        if (rb_pixel_scale_valid(w, h, s))
            return s;
    for (int s = requested + 1; s <= RB_PIXEL_SCALE_MAX; s++)
        if (rb_pixel_scale_valid(w, h, s))
            return s;
    return rb_suggested_scale(w, h);
}

int rb_cmdline_pixel_scale(const char *cmdline)
{
    static const char key[] = "retroboot.pixel_scale=";
    const char *p = cmdline;
    int found = 0;
    while (p && *p) {
        while (*p == ' ' || *p == '\t' || *p == '\n')
            p++;
        const char *end = p;
        while (*end && *end != ' ' && *end != '\t' && *end != '\n')
            end++;
        size_t n = (size_t)(end - p), kn = sizeof key - 1;
        if (n > kn && !strncmp(p, key, kn)) {
            /* the last occurrence wins, as for other kernel parameters */
            found = 0;
            if (n == kn + 1 && p[kn] >= '1' && p[kn] <= '0' + RB_PIXEL_SCALE_MAX)
                found = p[kn] - '0';
        }
        p = end;
    }
    return found;
}

int rb_boot_pixel_scale(const char *path)
{
    char buf[4096];
    FILE *f = fopen(path ? path : "/proc/cmdline", "re");
    if (!f)
        return 0;
    size_t n = fread(buf, 1, sizeof buf - 1, f);
    fclose(f);
    buf[n] = 0;
    return rb_cmdline_pixel_scale(buf);
}

void rb_layout_free(rb_layout *l)
{
    free(l->x0);
    free(l->x1);
    free(l->y0);
    free(l->y1);
    free(l->xw);
    free(l->yw);
    l->x0 = l->x1 = l->y0 = l->y1 = NULL;
    l->xw = l->yw = NULL;
}

static void build_axis(int n_src, int k, int n_dst, int *i0, int *i1, uint8_t *wt)
{
    long n_mid = (long)n_src * k;
    for (int d = 0; d < n_dst; d++) {
        double p = ((d + 0.5) * (double)n_mid) / n_dst - 0.5;
        if (p < 0)
            p = 0;
        long a = (long)p;
        if (a > n_mid - 1)
            a = n_mid - 1;
        double frac = p - (double)a;
        long b = a + 1 < n_mid ? a + 1 : a;
        int sa = (int)(a / k), sb = (int)(b / k);
        int w = (int)(frac * 256.0 + 0.5);
        if (w >= 256) {
            sa = sb;
            w = 0;
        }
        if (sa == sb)
            w = 0;
        i0[d] = sa;
        i1[d] = sb;
        wt[d] = (uint8_t)w;
    }
}

int rb_layout_compute(rb_layout *l, int screen_w, int screen_h, int src_w, int src_h, enum rb_scaling mode)
{
    return rb_layout_compute_ex(l, screen_w, screen_h, src_w, src_h, mode, 0, 1);
}

int rb_layout_logical(const rb_layout *phys, int s, rb_layout *out)
{
    memset(out, 0, sizeof *out);
    if (s < 1 || phys->mode != RB_SCALING_INTEGER || phys->k % s || phys->dst.x % s || phys->dst.y % s)
        return -1;
    out->screen_w = (phys->screen_w + s - 1) / s;
    out->screen_h = (phys->screen_h + s - 1) / s;
    out->src_w = phys->src_w;
    out->src_h = phys->src_h;
    out->requested = out->mode = RB_SCALING_INTEGER;
    out->k = phys->k / s;
    out->dst = (rb_rect){phys->dst.x / s, phys->dst.y / s, phys->src_w * out->k, phys->src_h * out->k};
    return 0;
}

int rb_layout_compute_ex(rb_layout *l, int screen_w, int screen_h, int src_w, int src_h, enum rb_scaling mode,
                         int max_k, int grid)
{
    rb_layout_free(l);
    memset(l, 0, sizeof *l);
    l->screen_w = screen_w;
    l->screen_h = screen_h;
    l->src_w = src_w;
    l->src_h = src_h;
    l->requested = mode;
    if (screen_w <= 0 || screen_h <= 0 || src_w <= 0 || src_h <= 0)
        return -1;
    if (mode == RB_SCALING_INTEGER) {
        int kx = screen_w / src_w, ky = screen_h / src_h;
        int k = kx < ky ? kx : ky;
        if (max_k > 0 && k > max_k)
            k = max_k;
        if (grid < 1)
            grid = 1;
        if (k >= 1) {
            l->mode = RB_SCALING_INTEGER;
            l->k = k;
            int x = (screen_w - src_w * k) / 2, y = (screen_h - src_h * k) / 2;
            l->dst = (rb_rect){x - x % grid, y - y % grid, src_w * k, src_h * k};
            return 0;
        }
    }
    l->mode = RB_SCALING_FIT;
    int dw, dh;
    if ((long)screen_w * src_h <= (long)screen_h * src_w) {
        dw = screen_w;
        dh = (int)(((long)src_h * screen_w * 2 + src_w) / (2L * src_w));
    } else {
        dh = screen_h;
        dw = (int)(((long)src_w * screen_h * 2 + src_h) / (2L * src_h));
    }
    if (dw < 1)
        dw = 1;
    if (dh < 1)
        dh = 1;
    if (dw > screen_w)
        dw = screen_w;
    if (dh > screen_h)
        dh = screen_h;
    int k = dw / src_w < dh / src_h ? dw / src_w : dh / src_h;
    if (k < 1)
        k = 1;
    l->k = k;
    l->dst = (rb_rect){(screen_w - dw) / 2, (screen_h - dh) / 2, dw, dh};
    l->x0 = malloc(sizeof(int) * dw);
    l->x1 = malloc(sizeof(int) * dw);
    l->xw = malloc(dw);
    l->y0 = malloc(sizeof(int) * dh);
    l->y1 = malloc(sizeof(int) * dh);
    l->yw = malloc(dh);
    if (!l->x0 || !l->x1 || !l->xw || !l->y0 || !l->y1 || !l->yw) {
        rb_layout_free(l);
        return -1;
    }
    build_axis(src_w, k, dw, l->x0, l->x1, l->xw);
    build_axis(src_h, k, dh, l->y0, l->y1, l->yw);
    return 0;
}

rb_rect rb_layout_map_damage(const rb_layout *l, rb_rect r)
{
    rb_rect c;
    if (!rb_rect_intersect(r, (rb_rect){0, 0, l->src_w, l->src_h}, &c))
        return (rb_rect){0, 0, 0, 0};
    if (l->mode == RB_SCALING_INTEGER)
        return (rb_rect){l->dst.x + c.x * l->k, l->dst.y + c.y * l->k, c.w * l->k, c.h * l->k};
    long dw = l->dst.w, dh = l->dst.h;
    long x0 = (long)c.x * dw / l->src_w - 3;
    long x1 = ((long)(c.x + c.w) * dw + l->src_w - 1) / l->src_w + 3;
    long y0 = (long)c.y * dh / l->src_h - 3;
    long y1 = ((long)(c.y + c.h) * dh + l->src_h - 1) / l->src_h + 3;
    if (x0 < 0)
        x0 = 0;
    if (y0 < 0)
        y0 = 0;
    if (x1 > dw)
        x1 = dw;
    if (y1 > dh)
        y1 = dh;
    if (x1 <= x0 || y1 <= y0)
        return (rb_rect){0, 0, 0, 0};
    return (rb_rect){l->dst.x + (int)x0, l->dst.y + (int)y0, (int)(x1 - x0), (int)(y1 - y0)};
}

static inline uint32_t lerp_px(uint32_t a, uint32_t b, uint32_t w)
{
    if (!w)
        return a;
    uint32_t iw = 256 - w;
    uint32_t rb = (((a & 0xff00ffu) * iw + (b & 0xff00ffu) * w) >> 8) & 0xff00ffu;
    uint32_t g = (((a & 0x00ff00u) * iw + (b & 0x00ff00u) * w) >> 8) & 0x00ff00u;
    return rb | g;
}

#if defined(__aarch64__) && defined(__ARM_NEON)
#include <arm_neon.h>
#endif

/* d[2j] = d[2j + 1] = s[j] for j < n: the 2x pixel scale's rows (a 1080x540 Mac
 * screen on the Gemini PDA's 2160x1080 frame, 1920x1080 at 2x on PCs). NEON on
 * arm64: one 16-byte load, one interleaving 32-byte store per four pixels. */
static void double_row(uint32_t *d, const uint32_t *s, int n)
{
    int j = 0;
#if defined(__aarch64__) && defined(__ARM_NEON)
    for (; j + 4 <= n; j += 4) {
        uint32x4_t v = vld1q_u32(s + j);
        uint32x4x2_t z = {{v, v}};
        vst2q_u32(d + 2 * j, z);
    }
#endif
    for (; j < n; j++)
        d[2 * j] = d[2 * j + 1] = s[j];
}

static void render_integer(const rb_layout *l, uint32_t *dst, int dst_stride, const uint32_t *src, int src_stride,
                           rb_rect r)
{
    int k = l->k;
    int prev_sy = -1;
    for (int y = r.y; y < r.y + r.h; y++) {
        int sy = (y - l->dst.y) / k;
        uint32_t *d = dst + (size_t)y * dst_stride + r.x;
        if (sy == prev_sy) {
            memcpy(d, d - dst_stride, (size_t)r.w * 4);
            continue;
        }
        prev_sy = sy;
        const uint32_t *s = src + (size_t)sy * src_stride;
        int rel = r.x - l->dst.x;
        if (k == 1) {
            memcpy(d, s + rel, (size_t)r.w * 4);
            continue;
        }
        int sx = rel / k, phase = rel % k, i = 0;
        if (k == 2) {
            if (phase) /* the rect starts on the right half of a Mac pixel */
                d[i++] = s[sx++];
            int n = (r.w - i) / 2;
            double_row(d + i, s + sx, n);
            i += 2 * n;
            sx += n;
            if (i < r.w)
                d[i] = s[sx];
            continue;
        }
        while (i < r.w) {
            uint32_t p = s[sx++];
            int run = k - phase;
            phase = 0;
            if (run > r.w - i)
                run = r.w - i;
            for (int j = 0; j < run; j++)
                d[i + j] = p;
            i += run;
        }
    }
}

static void hline(const rb_layout *l, uint32_t *out, const uint32_t *row, rb_rect r)
{
    for (int i = 0; i < r.w; i++) {
        int rx = r.x - l->dst.x + i;
        out[i] = lerp_px(row[l->x0[rx]], row[l->x1[rx]], l->xw[rx]);
    }
}

static void vline(uint32_t *out, const uint32_t *top, const uint32_t *bottom, int n, uint32_t weight)
{
    for (int i = 0; i < n; i++)
        out[i] = lerp_px(top[i], bottom[i], weight);
}

#ifdef RB_SCALE_AVX2
/* Each 16-bit lane computes (a * 256 + (b-a) * w) >> 8. The result
 * before shifting is in [0, 65280], including when the difference wraps. */
RB_SCALE_AVX2 static inline __m256i lerp_bytes(__m256i a, __m256i b, __m256i w)
{
    return _mm256_srli_epi16(_mm256_add_epi16(_mm256_slli_epi16(a, 8),
                              _mm256_mullo_epi16(_mm256_sub_epi16(b, a), w)), 8);
}

RB_SCALE_AVX2 static inline __m256i lerp8(__m256i a, __m256i b, __m256i weights, __m256i mask)
{
    __m256i low_bytes = _mm256_set1_epi16(255);
    __m256i rb = lerp_bytes(_mm256_and_si256(a, low_bytes), _mm256_and_si256(b, low_bytes), weights);
    __m256i gx = lerp_bytes(_mm256_srli_epi16(a, 8), _mm256_srli_epi16(b, 8), weights);
    return _mm256_and_si256(_mm256_or_si256(rb, _mm256_slli_epi16(gx, 8)), mask);
}

RB_SCALE_AVX2 static void hline8(const rb_layout *l, uint32_t *out, const uint32_t *row, rb_rect r)
{
    int i = 0, x = r.x - l->dst.x;
    for (; i + 8 <= r.w; i += 8) {
        __m256i ai = _mm256_loadu_si256((const __m256i *)(l->x0 + x + i));
        __m256i bi = _mm256_loadu_si256((const __m256i *)(l->x1 + x + i));
        /* Upscaling usually puts all sixteen taps in one source window.
         * Clamp the load at the right edge, even for an unpadded row. */
        int base = l->x0[x + i];
        if (base > l->src_w - 8)
            base = l->src_w - 8;
        __m256i a, b;
        if (base >= 0 && l->x1[x + i + 7] - base < 8) {
            __m256i pixels = _mm256_loadu_si256((const __m256i *)(row + base));
            __m256i origin = _mm256_set1_epi32(base);
            a = _mm256_permutevar8x32_epi32(pixels, _mm256_sub_epi32(ai, origin));
            b = _mm256_permutevar8x32_epi32(pixels, _mm256_sub_epi32(bi, origin));
        } else {
            a = _mm256_i32gather_epi32((const int *)row, ai, 4);
            b = _mm256_i32gather_epi32((const int *)row, bi, 4);
        }
        __m256i w = _mm256_cvtepu8_epi32(_mm_loadl_epi64((const __m128i *)(l->xw + x + i)));
        /* lerp_px preserves the X byte only when no blending takes place. */
        __m256i mask = _mm256_or_si256(_mm256_cmpeq_epi32(w, _mm256_setzero_si256()),
                                       _mm256_set1_epi32(0x00ffffff));
        w = _mm256_or_si256(w, _mm256_slli_epi32(w, 16));
        _mm256_storeu_si256((__m256i *)(out + i), lerp8(a, b, w, mask));
    }
    for (; i < r.w; i++)
        out[i] = lerp_px(row[l->x0[x+i]], row[l->x1[x+i]], l->xw[x+i]);
}

RB_SCALE_AVX2 static void vline8(uint32_t *out, const uint32_t *top, const uint32_t *bottom, int n, uint32_t weight)
{
    int i = 0;
    __m256i w = _mm256_set1_epi16(weight);
    for (; i + 8 <= n; i += 8) {
        __m256i a = _mm256_loadu_si256((const __m256i *)(top + i));
        __m256i b = _mm256_loadu_si256((const __m256i *)(bottom + i));
        _mm256_storeu_si256((__m256i *)(out + i), lerp8(a, b, w, _mm256_set1_epi32(0x00ffffff)));
    }
    vline(out + i, top + i, bottom + i, n-i, weight);
}
#endif

static void render_fit(const rb_layout *l, uint32_t *dst, int dst_stride, const uint32_t *src, int src_stride,
                       rb_rect r)
{
    void (*horizontal)(const rb_layout *, uint32_t *, const uint32_t *, rb_rect) = hline;
    void (*vertical)(uint32_t *, const uint32_t *, const uint32_t *, int, uint32_t) = vline;
#ifdef RB_SCALE_AVX2
    if (__builtin_cpu_supports("avx2")) {
        horizontal = hline8;
        vertical = vline8;
    }
#endif
    uint32_t *lines = malloc((size_t)r.w * 4 * 2);
    if (!lines)
        return;
    uint32_t *line[2] = {lines, lines + r.w};
    int tag[2] = {-1, -1};
    for (int y = r.y; y < r.y + r.h; y++) {
        int ry = y - l->dst.y;
        int a = l->y0[ry], b = l->y1[ry];
        uint32_t wy = l->yw[ry];
        /* Keep line[0] = row a, line[1] = row b, reusing cached rows. */
        if (tag[0] != a) {
            if (tag[1] == a) {
                uint32_t *t = line[0];
                line[0] = line[1];
                line[1] = t;
                tag[1] = tag[0];
                tag[0] = a;
            } else {
                horizontal(l, line[0], src + (size_t)a * src_stride, r);
                tag[0] = a;
            }
        }
        uint32_t *d = dst + (size_t)y * dst_stride + r.x;
        if (!wy || a == b) {
            memcpy(d, line[0], (size_t)r.w * 4);
            continue;
        }
        if (tag[1] != b) {
            horizontal(l, line[1], src + (size_t)b * src_stride, r);
            tag[1] = b;
        }
        vertical(d, line[0], line[1], r.w, wy);
    }
    free(lines);
}

void rb_layout_render(const rb_layout *l, uint32_t *dst, int dst_stride, const uint32_t *src, int src_stride,
                      rb_rect area)
{
    rb_rect r;
    if (!src || !rb_rect_intersect(area, l->dst, &r))
        return;
    if (l->mode == RB_SCALING_INTEGER ||
        (l->dst.w == (long)l->src_w * l->k && l->dst.h == (long)l->src_h * l->k))
        render_integer(l, dst, dst_stride, src, src_stride, r);
    else
        render_fit(l, dst, dst_stride, src, src_stride, r);
}

void rb_layout_screen_to_src(const rb_layout *l, int sx, int sy, int *gx, int *gy)
{
    long x = l->dst.w > 0 ? ((long)(sx - l->dst.x) * l->src_w) / l->dst.w : 0;
    long y = l->dst.h > 0 ? ((long)(sy - l->dst.y) * l->src_h) / l->dst.h : 0;
    if (sx < l->dst.x)
        x = 0;
    if (sy < l->dst.y)
        y = 0;
    if (x >= l->src_w)
        x = l->src_w - 1;
    if (y >= l->src_h)
        y = l->src_h - 1;
    if (x < 0)
        x = 0;
    if (y < 0)
        y = 0;
    *gx = (int)x;
    *gy = (int)y;
}

double rb_layout_ratio(const rb_layout *l)
{
    if (l->src_w <= 0)
        return 1.0;
    return (double)l->dst.w / l->src_w;
}

void rb_upscale_rect(uint32_t *dst, int dst_stride, int dst_w, int dst_h, const uint32_t *src, int src_stride,
                     rb_rect lr, int s)
{
    if (s < 1)
        s = 1;
    rb_rect pr = {lr.x * s, lr.y * s, lr.w * s, lr.h * s};
    if (!rb_rect_intersect(pr, (rb_rect){0, 0, dst_w, dst_h}, &pr))
        return;
    int prev_ly = -1;
    for (int y = pr.y; y < pr.y + pr.h; y++) {
        int ly = y / s;
        uint32_t *d = dst + (size_t)y * dst_stride + pr.x;
        if (ly == prev_ly) {
            memcpy(d, d - dst_stride, (size_t)pr.w * 4);
            continue;
        }
        prev_ly = ly;
        const uint32_t *row = src + (size_t)ly * src_stride;
        if (s == 1) {
            memcpy(d, row + pr.x, (size_t)pr.w * 4);
            continue;
        }
        for (int i = 0; i < pr.w; i++)
            d[i] = row[(pr.x + i) / s];
    }
}
