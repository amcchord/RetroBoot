/* Frame log (framelog.h) and the virtual output (drm.h "virtual:WxH"): the
 * log replays to exactly the frames presented, overlapping and unchanged
 * rectangles included, and the virtual output completes its flips at the
 * refresh and records every presented frame. */
#include <poll.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/timerfd.h>
#include <time.h>
#include <unistd.h>

#include "drm.h"
#include "framelog.h"
#include "log.h"
#include "rbtest.h"

struct replay {
    FILE *f;
    int w, h;
    uint32_t *canvas;
    uint64_t t_us;
    uint32_t flags, bands;
};

static int replay_open(struct replay *r, const char *path)
{
    memset(r, 0, sizeof *r);
    r->f = fopen(path, "rb");
    if (!r->f)
        return -1;
    char magic[4];
    uint32_t version;
    if (fread(magic, 1, 4, r->f) != 4 || memcmp(magic, "RBFL", 4) || fread(&version, 4, 1, r->f) != 1 ||
        version != RB_FRAMELOG_VERSION)
        return -1;
    return 0;
}

/* Next frame into r->canvas: 1, 0 at the end, -1 on a format error. */
static int replay_next(struct replay *r)
{
    char tag[4];
    if (fread(tag, 1, 4, r->f) != 4)
        return 0;
    uint32_t dims[3];
    if (memcmp(tag, "FRAM", 4) || fread(&r->flags, 4, 1, r->f) != 1 || fread(&r->t_us, 8, 1, r->f) != 1 ||
        fread(dims, 4, 3, r->f) != 3)
        return -1;
    if (!r->canvas || (r->flags & RB_FRAMELOG_RESIZE)) {
        free(r->canvas);
        r->w = (int)dims[0];
        r->h = (int)dims[1];
        r->canvas = calloc((size_t)r->w * r->h, 4);
    }
    if ((int)dims[0] != r->w || (int)dims[1] != r->h)
        return -1;
    r->bands = dims[2];
    for (uint32_t b = 0; b < dims[2]; b++) {
        int32_t hdr[4];
        if (fread(hdr, 4, 4, r->f) != 4 || hdr[0] < 0 || hdr[1] < 0 || hdr[2] <= 0 || hdr[3] <= 0 ||
            hdr[0] + hdr[2] > r->w || hdr[1] + hdr[3] > r->h)
            return -1;
        for (int y = hdr[1]; y < hdr[1] + hdr[3]; y++)
            if (fread(r->canvas + (size_t)y * r->w + hdr[0], 4, (size_t)hdr[2], r->f) != (size_t)hdr[2])
                return -1;
    }
    return 1;
}

static void replay_close(struct replay *r)
{
    if (r->f)
        fclose(r->f);
    free(r->canvas);
}

static void test_log(const char *path)
{
    enum { W = 97, H = 61, N = 40 };
    uint32_t *frames = calloc((size_t)N * W * H, 4);
    struct rb_framelog l;
    CHECK_EQ(rb_framelog_open(&l, path, W, H), 0);
    uint32_t *cur = calloc((size_t)W * H, 4);
    for (int i = 0; i < W * H; i++)
        cur[i] = rbt_rand() | 0xff000000u;
    for (int f = 0; f < N; f++) {
        rb_rect rects[6];
        int n = 0;
        if (f > 0) {
            n = (int)(rbt_rand() % 4) + 1;
            for (int k = 0; k < n; k++) {
                rb_rect r = {(int)(rbt_rand() % W), (int)(rbt_rand() % H), (int)(rbt_rand() % 40) + 1,
                             (int)(rbt_rand() % 30) + 1};
                rects[k] = r;
                /* change only part of the rectangle (unchanged rows are not written) */
                rb_rect c = rb_rect_clip(r, W, H);
                for (int y = c.y; y < c.y + c.h; y += 2)
                    for (int x = c.x; x < c.x + c.w; x++)
                        cur[y * W + x] = rbt_rand();
            }
            if (f % 7 == 0) {
                rects[n] = rects[0]; /* overlaps the first one completely */
                rects[n++].w += 5;
            }
            if (f % 11 == 0)
                rects[n++] = (rb_rect){-5, -5, 3, 3}; /* off the frame */
        }
        memcpy(frames + (size_t)f * W * H, cur, (size_t)W * H * 4);
        /* a stride wider than the frame */
        uint32_t *strided = calloc((size_t)(W + 3) * H, 4);
        for (int y = 0; y < H; y++)
            memcpy(strided + (size_t)y * (W + 3), cur + (size_t)y * W, W * 4);
        CHECK(rb_framelog_add(&l, 1000u + (uint64_t)f * 16667u, strided, W + 3, rects, n) >= 0);
        free(strided);
    }
    /* a frame that changed nothing still records a refresh (no bands) */
    CHECK_EQ(rb_framelog_add(&l, 99999999u, cur, W, &(rb_rect){0, 0, W, H}, 1), 0);
    CHECK_EQ(l.frames, N + 1);
    CHECK_EQ(l.error, 0);
    rb_framelog_close(&l);

    struct replay r;
    CHECK_EQ(replay_open(&r, path), 0);
    for (int f = 0; f < N; f++) {
        CHECK_EQ(replay_next(&r), 1);
        CHECK_EQ(r.t_us, 1000u + (uint64_t)f * 16667u);
        CHECK_EQ(r.flags, f == 0 ? RB_FRAMELOG_FIRST : 0);
        CHECK(!memcmp(r.canvas, frames + (size_t)f * W * H, (size_t)W * H * 4));
    }
    CHECK_EQ(replay_next(&r), 1);
    CHECK_EQ(r.bands, 0);
    CHECK_EQ(replay_next(&r), 0);
    replay_close(&r);
    free(frames);
    free(cur);
}

static uint64_t mono_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000u + (uint64_t)ts.tv_nsec;
}

/* Completes the pending flip and returns the time read just after the
 * dispatch that completed it, however long the scheduler keeps the test off
 * the CPU (0: not within 10 s). The first dispatch comes before any poll, so
 * a flip that completed before its refresh would show. */
static uint64_t wait_flip(struct rb_output *o)
{
    uint64_t give_up = rb_mono_us() + 10000000u;
    for (;;) {
        rb_output_dispatch(o);
        if (o->pending < 0)
            return rb_mono_us();
        if (rb_mono_us() > give_up)
            return 0;
        struct pollfd pfd = {.fd = o->fd, .events = POLLIN};
        poll(&pfd, 1, 100);
    }
}

/* The absolute CLOCK_MONOTONIC time (ns) the virtual output's flip timer is
 * armed for, bracketed by clock reads around timerfd_gettime: *lo <= armed <=
 * *hi. A timer that already expired (it_value 0) only bounds it from above. */
static void armed_at(const struct rb_output *o, uint64_t *lo, uint64_t *hi)
{
    struct itimerspec its;
    uint64_t a = mono_ns();
    CHECK_EQ(timerfd_gettime(o->fd, &its), 0);
    uint64_t b = mono_ns();
    uint64_t rem = (uint64_t)its.it_value.tv_sec * 1000000000u + (uint64_t)its.it_value.tv_nsec;
    *lo = rem ? a + rem : 0;
    *hi = b + rem;
}

static void test_virtual(const char *path)
{
    enum { W = 160, H = 120 };
    struct rb_output o;
    char err[256];
    rb_virtual_record = path;
    CHECK_EQ(rb_output_open(&o, "virtual:bogus", NULL, NULL, err, sizeof err), -1);
    CHECK_EQ(rb_output_open(&o, "virtual:160x120@50", NULL, NULL, err, sizeof err), 0);
    CHECK(o.virt);
    CHECK_EQ(o.w, W);
    CHECK_EQ(o.h, H);
    CHECK_EQ(o.refresh, 50);
    CHECK(rb_output_is_master(&o));
    uint32_t ids[4];
    CHECK_EQ(rb_output_connected(&o, ids, 4), 1);
    uint32_t *shadow = calloc((size_t)W * H, 4), *expect = calloc((size_t)3 * W * H, 4);
    for (int i = 0; i < W * H; i++)
        shadow[i] = 0x00808080u;
    memcpy(expect, shadow, (size_t)W * H * 4);
    CHECK(rb_output_needs_flush(&o));
    CHECK_EQ(rb_output_flush(&o, shadow, W), 1); /* first frame: at once */
    CHECK_EQ(o.front, 0);
    CHECK_EQ(o.pending, -1);
    /* second frame: pending until the refresh */
    for (int y = 10; y < 30; y++)
        for (int x = 20; x < 60; x++)
            shadow[y * W + x] = 0x00ff0000u;
    rb_output_damage(&o, (rb_rect){20, 10, 40, 20});
    memcpy(expect + W * H, shadow, (size_t)W * H * 4);
    /* The flip's timing is checked against the refresh it was armed for (the
     * time the frame log records), not against how soon this process runs
     * again: under load the scheduler can keep it off the CPU for longer than
     * a refresh. */
    uint64_t t0 = rb_mono_us();
    CHECK_EQ(rb_output_flush(&o, shadow, W), 1);
    uint64_t t1 = rb_mono_us();
    uint64_t lo1, hi1;
    armed_at(&o, &lo1, &hi1);
    CHECK_EQ(o.pending, 1);
    CHECK_EQ(rb_output_flush(&o, shadow, W), 0); /* a flip is pending */
    uint64_t flipped1 = wait_flip(&o);
    CHECK(flipped1);
    CHECK_EQ(o.pending, -1);
    CHECK_EQ(o.front, 1);
    /* third frame */
    for (int x = 0; x < W; x++)
        shadow[100 * W + x] = 0x000000ffu;
    rb_output_damage(&o, (rb_rect){0, 100, W, 1});
    memcpy(expect + 2 * W * H, shadow, (size_t)W * H * 4);
    uint64_t t2 = rb_mono_us();
    CHECK_EQ(rb_output_flush(&o, shadow, W), 1);
    uint64_t t3 = rb_mono_us();
    uint64_t lo2, hi2;
    armed_at(&o, &lo2, &hi2);
    uint64_t flipped2 = wait_flip(&o);
    CHECK(flipped2);
    CHECK_EQ(o.front, 0);
    CHECK_EQ(o.presents, 3);
    uint64_t epoch = o.virt_epoch_us;
    rb_output_close(&o);
    rb_virtual_record = NULL;

    struct replay r;
    CHECK_EQ(replay_open(&r, path), 0);
    uint64_t last = 0;
    const uint64_t flush_start[3] = {0, t0, t2}, flush_end[3] = {0, t1, t3}, flipped[3] = {0, flipped1, flipped2};
    const uint64_t lo[3] = {0, lo1, lo2}, hi[3] = {0, hi1, hi2};
    for (int f = 0; f < 3; f++) {
        CHECK_EQ(replay_next(&r), 1);
        CHECK(!memcmp(r.canvas, expect + (size_t)f * W * H, (size_t)W * H * 4));
        CHECK(r.t_us >= last);
        if (f > 0) {
            uint64_t due = r.t_us;
            /* shown at the next 50 Hz refresh after the flush: within one
             * refresh of it, and on the output's refresh grid */
            CHECK(due > flush_start[f]);
            CHECK(due <= flush_end[f] + 20000u);
            CHECK_EQ((due - epoch) % 20000u, 0);
            /* the flip timer is armed for exactly that refresh (1 us for the
             * clock reads' rounding) ... */
            CHECK(lo[f] <= due * 1000u + 1000u);
            CHECK(due * 1000u <= hi[f] + 1000u);
            /* ... and the flip completed on it, not before */
            CHECK(flipped[f] >= due);
        }
        last = r.t_us;
    }
    CHECK_EQ(replay_next(&r), 0);
    replay_close(&r);
    free(shadow);
    free(expect);
}

/* A guest log: the size changes with Mac OS's mode. */
static void test_resize(const char *path)
{
    struct rb_framelog l;
    CHECK_EQ(rb_framelog_open(&l, path, 64, 48), 0);
    uint32_t a[64 * 48], b[80 * 60];
    for (int i = 0; i < 64 * 48; i++)
        a[i] = 0x00102030u + (uint32_t)i;
    for (int i = 0; i < 80 * 60; i++)
        b[i] = 0x00405060u + (uint32_t)i;
    CHECK(rb_framelog_add(&l, 10, a, 64, NULL, 0) > 0);
    CHECK_EQ(rb_framelog_resize(&l, 64, 48), 0); /* same size: nothing */
    CHECK(!l.resized);
    CHECK_EQ(rb_framelog_resize(&l, 80, 60), 0);
    CHECK_EQ(rb_framelog_add(&l, 20, b, 80, &(rb_rect){0, 0, 1, 1}, 1), 1); /* written whole: one band */
    b[5 * 80 + 7] = 0;
    CHECK_EQ(rb_framelog_add(&l, 30, b, 80, &(rb_rect){0, 0, 80, 60}, 1), 1);
    rb_framelog_close(&l);
    struct replay r;
    CHECK_EQ(replay_open(&r, path), 0);
    CHECK_EQ(replay_next(&r), 1);
    CHECK(!memcmp(r.canvas, a, sizeof a));
    CHECK_EQ(replay_next(&r), 1);
    CHECK_EQ(r.flags, RB_FRAMELOG_RESIZE);
    CHECK_EQ(r.w, 80);
    CHECK_EQ(replay_next(&r), 1);
    CHECK_EQ(r.flags, 0);
    CHECK(!memcmp(r.canvas, b, sizeof b));
    CHECK_EQ(replay_next(&r), 0);
    replay_close(&r);
}

int main(void)
{
    char path[] = "/tmp/rb-framelog-XXXXXX";
    int fd = mkstemp(path);
    CHECK(fd >= 0);
    close(fd);
    test_log(path);
    test_resize(path);
    test_virtual(path);
    unlink(path);
    TEST_DONE();
}
