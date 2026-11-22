/* Kernel damage (drm.h): the FB_DAMAGE_CLIPS each atomic flip carries must keep
 * what the panel shows equal to the presenter's frame.
 *
 * Two kernel models are driven by the presenter's own bookkeeping
 * (rb_output_damage, rb_output_damage_all, rb_output_stage, rb_damage_clips,
 * rb_output_committed, rb_output_kernel_damage_all) with random damage, refused
 * commits, full redraws and another DRM master drawing in between:
 *
 *   A. geminipda-drm: one fixed scanout; every commit copies the clips of the new
 *      buffer into it (Linux 6.6 shadow-plane helper);
 *   B. a per-buffer upload (virtio-gpu style, or a kernel that double-buffers
 *      inside its own memory): each buffer has its own copy and a commit uploads
 *      the clips of that buffer into it before it is shown.
 *
 * With the clips the presenter sends (what was copied into the buffer since it
 * was last committed: the last two frames' damage) both models always show the
 * frame. A negative control sends only the damage since the previous commit
 * (frame damage): model A still shows the frame -- the reason it would be
 * enough for geminipda -- but model B shows stale pixels, which is why the
 * presenter sends buffer damage. Every rotation, 1x1 frames inside a larger
 * output are covered by the transform tests (test_xform). */
#include <stdlib.h>
#include <string.h>

#include "drm.h"
#include "rbtest.h"

#define FBW 48
#define FBH 96

static uint32_t rand_px(void) { return 0xff000000u | (rbt_rand() & 0xffffffu); }

static void fill_rect(uint32_t *buf, int stride, rb_rect r, uint32_t v)
{
    for (int y = r.y; y < r.y + r.h; y++)
        for (int x = r.x; x < r.x + r.w; x++)
            buf[(size_t)y * stride + x] = v;
}

static void copy_clips(uint32_t *dst, const uint32_t *src, const struct drm_mode_rect *c, int n, bool full)
{
    if (full) {
        memcpy(dst, src, (size_t)FBW * FBH * 4);
        return;
    }
    for (int i = 0; i < n; i++)
        for (int y = c[i].y1; y < c[i].y2; y++)
            memcpy(dst + (size_t)y * FBW + c[i].x1, src + (size_t)y * FBW + c[i].x1,
                   (size_t)(c[i].x2 - c[i].x1) * 4);
}

/* The frame as the output would show it: the full transform of the shadow. */
static void reference(const struct rb_output *o, const uint32_t *shadow, int sw, uint32_t *out)
{
    memset(out, 0, (size_t)FBW * FBH * 4);
    rb_xform_fill_border(&o->xf, out, FBW);
    rb_xform_blit(&o->xf, out, FBW, shadow, sw, (rb_rect){0, 0, o->xf.src_w, o->xf.src_h});
}

/* Returns the number of checks where the panel differed from the frame. */
static int run(int rot, bool per_buffer, bool frame_damage_only, int iters, unsigned seed)
{
    rbt_seed = seed;
    struct rb_output o;
    memset(&o, 0, sizeof o);
    o.fd = -1;
    o.w = FBW;
    o.h = FBH;
    o.rot = rot;
    o.nfb = 2;
    o.front = o.pending = -1;
    uint32_t *fbs[2], *host[2], *scan = calloc((size_t)FBW * FBH, 4), *ref = calloc((size_t)FBW * FBH, 4);
    for (int i = 0; i < 2; i++) {
        fbs[i] = calloc((size_t)FBW * FBH, 4);
        host[i] = calloc((size_t)FBW * FBH, 4);
        o.fb[i].map = (uint8_t *)fbs[i];
        o.fb[i].pitch = FBW * 4;
    }
    int lw = rot == 90 || rot == 270 ? FBH : FBW, lh = rot == 90 || rot == 270 ? FBW : FBH;
    rb_output_set_frame(&o, lw, lh, false);
    rb_output_kernel_damage_all(&o);
    uint32_t *shadow = calloc((size_t)lw * lh, 4);
    for (int i = 0; i < lw * lh; i++)
        shadow[i] = rand_px();
    rb_region frame_acc; /* negative control: damage since the last successful commit */
    rb_region_clear(&frame_acc);
    bool frame_acc_full = true;
    int mismatches = 0;
    for (int it = 0; it < iters; it++) {
        int nd = (int)(rbt_rand() % 4);
        for (int d = 0; d < nd; d++) {
            int x = (int)(rbt_rand() % (unsigned)lw), y = (int)(rbt_rand() % (unsigned)lh);
            rb_rect r = {x, y, 1 + (int)(rbt_rand() % 9), 1 + (int)(rbt_rand() % 9)};
            r = rb_rect_clip(r, lw, lh);
            fill_rect(shadow, lw, r, rand_px());
            rb_output_damage(&o, r);
            rb_region_add(&frame_acc, r);
        }
        if (rbt_rand() % 40 == 0) { /* a full redraw (frame change, DPMS on) */
            rb_output_damage_all(&o);
            frame_acc_full = true;
        }
        if (rbt_rand() % 50 == 0) { /* another master drew, then the presenter got the device back */
            for (int i = 0; i < FBW * FBH; i++)
                scan[i] = host[0][i] = host[1][i] = rand_px();
            rb_output_kernel_damage_all(&o);
            frame_acc_full = true;
            if (o.front >= 0)
                o.need_modeset = true; /* rb_output_flush's modeset path */
        }
        if (o.front < 0 || o.need_modeset) { /* the first commit: SetCrtc, no clips */
            int idx = o.front < 0 ? 0 : o.front;
            o.full[idx] = true;
            rb_output_stage(&o, idx, shadow, lw);
            memcpy(scan, fbs[idx], (size_t)FBW * FBH * 4);
            memcpy(host[idx], fbs[idx], (size_t)FBW * FBH * 4);
            rb_output_committed(&o, idx, true);
            o.front = idx;
            o.need_modeset = false;
            rb_region_clear(&frame_acc);
            frame_acc_full = false;
        } else {
            if (rb_region_empty(&o.stale[o.front]) && !o.full[o.front])
                continue; /* nothing to present */
            int back = 1 - o.front;
            rb_output_stage(&o, back, shadow, lw);
            struct drm_mode_rect clips[RB_REGION_MAX];
            bool full;
            int n;
            if (frame_damage_only)
                n = rb_damage_clips(&o.xf, &frame_acc, frame_acc_full || o.kfull[back], clips, RB_REGION_MAX, &full);
            else
                n = rb_damage_clips(&o.xf, &o.kdamage[back], o.kfull[back], clips, RB_REGION_MAX, &full);
            bool ok = rbt_rand() % 10 != 0; /* EBUSY now and then */
            if (ok) {
                if (per_buffer)
                    copy_clips(host[back], fbs[back], clips, n, full);
                else
                    copy_clips(scan, fbs[back], clips, n, full);
                o.front = back;
                rb_region_clear(&frame_acc);
                frame_acc_full = false;
            }
            rb_output_committed(&o, back, ok);
            if (!ok)
                continue;
        }
        reference(&o, shadow, lw, ref);
        const uint32_t *shown = per_buffer ? host[o.front] : scan;
        if (memcmp(shown, ref, (size_t)FBW * FBH * 4))
            mismatches++;
    }
    for (int i = 0; i < 2; i++) {
        free(fbs[i]);
        free(host[i]);
    }
    free(scan);
    free(ref);
    free(shadow);
    rb_xform_free(&o.xf);
    return mismatches;
}

static void test_clips(void)
{
    struct rb_xform x = {0};
    /* The Gemini PDA: a 1080x2160 portrait scanout, Left Side Up (270), frame 2160x1080 */
    rb_xform_setup(&x, 270, 1080, 2160, 2160, 1080, false);
    rb_region g;
    rb_region_clear(&g);
    rb_region_add(&g, (rb_rect){0, 0, 10, 20});
    rb_region_add(&g, (rb_rect){2150, 1070, 10, 10}); /* bottom-right of the landscape frame */
    struct drm_mode_rect c[RB_REGION_MAX];
    bool full;
    int n = rb_damage_clips(&x, &g, false, c, RB_REGION_MAX, &full);
    CHECK(!full);
    CHECK_EQ(n, 2);
    /* frame (x, y, w, h) -> scanout (y, ow - x - w, h, w) */
    CHECK_EQ(c[0].x1, 0);
    CHECK_EQ(c[0].y1, 2150);
    CHECK_EQ(c[0].x2, 20);
    CHECK_EQ(c[0].y2, 2160);
    CHECK_EQ(c[1].x1, 1070);
    CHECK_EQ(c[1].y1, 0);
    CHECK_EQ(c[1].x2, 1080);
    CHECK_EQ(c[1].y2, 10);
    /* a blinking cursor: 2x16 Mac pixels at 2x is 4x32 frame pixels, 128 scanout pixels */
    rb_region_clear(&g);
    rb_region_add(&g, (rb_rect){600, 300, 4, 32});
    n = rb_damage_clips(&x, &g, false, c, RB_REGION_MAX, &full);
    CHECK_EQ(n, 1);
    CHECK_EQ((long)(c[0].x2 - c[0].x1) * (c[0].y2 - c[0].y1), 128);
    /* full, nothing visible, too many */
    n = rb_damage_clips(&x, &g, true, c, RB_REGION_MAX, &full);
    CHECK(full);
    CHECK_EQ(n, 0);
    rb_region_clear(&g);
    n = rb_damage_clips(&x, &g, false, c, RB_REGION_MAX, &full);
    CHECK(!full);
    CHECK_EQ(n, 1);
    CHECK_EQ(c[0].x2 - c[0].x1, 1);
    rb_region_add(&g, (rb_rect){0, 0, 4, 4});
    rb_region_add(&g, (rb_rect){100, 100, 4, 4});
    n = rb_damage_clips(&x, &g, false, c, 1, &full);
    CHECK(full);
    rb_xform_free(&x);
}

static void test_policy(void)
{
    unsetenv("RB_DRM_DAMAGE");
    CHECK(rb_driver_damage_atomic("geminipda-drm"));
    CHECK(!rb_driver_damage_atomic("i915"));
    CHECK(!rb_driver_damage_atomic("amdgpu"));
    CHECK(!rb_driver_damage_atomic("vc4"));
    CHECK(!rb_driver_damage_atomic("bochs-drm"));
    CHECK(!rb_driver_damage_atomic("virtio_gpu"));
    CHECK(!rb_driver_damage_atomic(NULL));
    setenv("RB_DRM_DAMAGE", "legacy", 1);
    CHECK(!rb_driver_damage_atomic("geminipda-drm"));
    setenv("RB_DRM_DAMAGE", "atomic", 1);
    CHECK(rb_driver_damage_atomic("i915"));
    unsetenv("RB_DRM_DAMAGE");
}

/* A refused commit keeps the buffer's kernel damage; a successful one clears it. */
static void test_bookkeeping(void)
{
    struct rb_output o;
    memset(&o, 0, sizeof o);
    o.fd = -1;
    o.w = FBW;
    o.h = FBH;
    o.nfb = 2;
    uint32_t *fbs[2];
    for (int i = 0; i < 2; i++) {
        fbs[i] = calloc((size_t)FBW * FBH, 4);
        o.fb[i].map = (uint8_t *)fbs[i];
        o.fb[i].pitch = FBW * 4;
    }
    rb_output_set_frame(&o, FBW, FBH, false);
    uint32_t *shadow = calloc((size_t)FBW * FBH, 4);
    rb_output_stage(&o, 0, shadow, FBW);
    CHECK(o.kfull[0]);
    rb_output_committed(&o, 0, true);
    CHECK(!o.kfull[0]);
    CHECK(o.kfull[1] == false); /* buffer 1 never staged: its full redraw is still pending */
    CHECK(o.full[1]);
    rb_output_stage(&o, 1, shadow, FBW);
    rb_output_committed(&o, 1, true);
    rb_output_damage(&o, (rb_rect){1, 2, 3, 4});
    rb_output_stage(&o, 0, shadow, FBW);
    CHECK_EQ(o.kdamage[0].n, 1);
    rb_output_committed(&o, 0, false); /* EBUSY */
    rb_output_damage(&o, (rb_rect){20, 30, 2, 2});
    rb_output_stage(&o, 0, shadow, FBW);
    CHECK_EQ(rb_region_area(&o.kdamage[0]) >= 3 * 4 + 2 * 2, 1); /* both, until a commit succeeds */
    rb_output_committed(&o, 0, true);
    CHECK_EQ(o.kdamage[0].n, 0);
    CHECK(rb_region_area(&o.stale[1]) >= 3 * 4 + 2 * 2); /* buffer 1 still owes both */
    rb_output_kernel_damage_all(&o);
    CHECK(o.kfull[0] && o.kfull[1]);
    free(fbs[0]);
    free(fbs[1]);
    free(shadow);
    rb_xform_free(&o.xf);
}

int main(void)
{
    test_clips();
    test_policy();
    test_bookkeeping();
    static const int rots[] = {0, 90, 180, 270};
    int bad_frame_b = 0;
    for (int r = 0; r < 4; r++) {
        for (unsigned seed = 1; seed <= 6; seed++) {
            /* what the presenter sends: right for both kinds of kernel */
            CHECK_EQ(run(rots[r], false, false, 400, seed * 7919u), 0);
            CHECK_EQ(run(rots[r], true, false, 400, seed * 7919u), 0);
            /* frame damage alone: right for the single scanout (A) ... */
            CHECK_EQ(run(rots[r], false, true, 400, seed * 7919u), 0);
            /* ... wrong for a per-buffer copy (B) */
            bad_frame_b += run(rots[r], true, true, 400, seed * 7919u);
        }
    }
    CHECK(bad_frame_b > 0);
    fprintf(stderr, "negative control: frame damage alone left a per-buffer kernel stale %d times\n", bad_frame_b);
    TEST_DONE();
}
