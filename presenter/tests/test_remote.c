/* Remote screen socket (remote.c) and the viewer indicator (badge.c):
 * message parsing, tight diffing, the REQUEST/UPDATE cycle over a real
 * SEQPACKET socket with a sealed memfd, input routing and generation checks,
 * and the remote picture through a Mac OS mode switch (the presenter's
 * switch-hold decision, switchhold.h). */
#include <errno.h>
#include <fcntl.h>
#include <linux/input-event-codes.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include "badge.h"
#include "loop.h"
#include "rbtest.h"
#include "remote.h"
#include "switchhold.h"

static size_t msg(uint8_t *b, uint32_t type, int nargs, uint32_t a, uint32_t bb, uint32_t c)
{
    uint32_t v[5] = {type, (uint32_t)nargs * 4, a, bb, c};
    memcpy(b, v, 8 + (size_t)nargs * 4);
    return 8 + (size_t)nargs * 4;
}

static void test_parse(void)
{
    uint8_t b[64];
    struct rbr_msg m;
    char err[120];
    CHECK_EQ(rbr_parse(b, msg(b, RBR_HELLO, 2, 1, 0, 0), &m, err, sizeof err), 0);
    CHECK_EQ(m.a, 1);
    CHECK(rbr_parse(b, msg(b, RBR_HELLO, 2, 1, 7, 0), &m, err, sizeof err) < 0); /* unknown source */
    CHECK_EQ(rbr_parse(b, msg(b, RBR_REQUEST, 2, 9, 999999, 0), &m, err, sizeof err), 0);
    CHECK_EQ(m.b, RBR_WAIT_MAX_MS); /* clamped */
    CHECK_EQ(rbr_parse(b, msg(b, RBR_KEY, 2, KEY_A, 1, 0), &m, err, sizeof err), 0);
    CHECK(rbr_parse(b, msg(b, RBR_KEY, 2, 0, 1, 0), &m, err, sizeof err) < 0);
    CHECK(rbr_parse(b, msg(b, RBR_KEY, 2, KEY_CNT, 1, 0), &m, err, sizeof err) < 0);
    CHECK(rbr_parse(b, msg(b, RBR_KEY, 2, KEY_A, 2, 0), &m, err, sizeof err) < 0);
    CHECK_EQ(rbr_parse(b, msg(b, RBR_POINTER, 3, 4, 100, 200), &m, err, sizeof err), 0);
    CHECK(rbr_parse(b, msg(b, RBR_POINTER, 3, 4, (uint32_t)-5, 200), &m, err, sizeof err) < 0);
    CHECK(rbr_parse(b, msg(b, RBR_POINTER, 2, 4, 100, 0), &m, err, sizeof err) < 0); /* short */
    CHECK_EQ(rbr_parse(b, msg(b, RBR_BUTTON, 2, 4, 1, 0), &m, err, sizeof err), 0);
    CHECK(rbr_parse(b, msg(b, RBR_BUTTON, 2, 5, 1, 0), &m, err, sizeof err) < 0);
    CHECK_EQ(rbr_parse(b, msg(b, RBR_RELEASE_ALL, 0, 0, 0, 0), &m, err, sizeof err), 0);
    CHECK_EQ(rbr_parse(b, msg(b, RBR_VIEWERS, 2, 2, 1, 0), &m, err, sizeof err), 0);
    CHECK(rbr_parse(b, msg(b, RBR_VIEWERS, 2, 1, 2, 0), &m, err, sizeof err) < 0);
    CHECK(rbr_parse(b, msg(b, 99, 0, 0, 0, 0), &m, err, sizeof err) < 0);
    size_t n = msg(b, RBR_KEY, 2, KEY_A, 1, 0);
    CHECK(rbr_parse(b, n - 1, &m, err, sizeof err) < 0);
    CHECK(rbr_parse(b, 4, &m, err, sizeof err) < 0);
}

static void test_diff(void)
{
    enum { W = 320, H = 200 };
    uint32_t *src = calloc(W * H, 4), *dst = calloc(W * H, 4);
    rb_region out;
    rb_region_clear(&out);
    /* identical: nothing */
    CHECK_EQ(rbr_diff_copy(dst, W, src, W, (rb_rect){0, 0, W, H}, &out), 0);
    /* a 16x16 "cursor" inside a full-width damage strip -> one tight rect */
    for (int y = 50; y < 66; y++)
        for (int x = 100; x < 116; x++)
            src[y * W + x] = 0xffffff;
    rb_region_clear(&out);
    CHECK_EQ(rbr_diff_copy(dst, W, src, W, (rb_rect){0, 48, W, 20}, &out), 1);
    CHECK_EQ(out.r[0].x, 100);
    CHECK_EQ(out.r[0].y, 50);
    CHECK_EQ(out.r[0].w, 16);
    CHECK_EQ(out.r[0].h, 16);
    CHECK(!memcmp(dst, src, W * H * 4));
    /* two far-apart changes on the same rows -> two rects */
    src[10 * W + 5] = 1;
    src[10 * W + 300] = 2;
    rb_region_clear(&out);
    CHECK_EQ(rbr_diff_copy(dst, W, src, W, (rb_rect){0, 0, W, 20}, &out), 2);
    CHECK(!memcmp(dst, src, W * H * 4));
    /* a change spanning tiles is still one rect (neighbours merge) */
    for (int x = 120; x < 140; x++)
        src[30 * W + x] = 9;
    rb_region_clear(&out);
    CHECK_EQ(rbr_diff_copy(dst, W, src, W, (rb_rect){0, 30, W, 1}, &out), 1);
    CHECK_EQ(out.r[0].x, 120);
    CHECK_EQ(out.r[0].w, 20);
    /* only the damaged rect is examined and copied */
    src[150 * W + 10] = 3;
    rb_region_clear(&out);
    CHECK_EQ(rbr_diff_copy(dst, W, src, W, (rb_rect){0, 0, W, 100}, &out), 0);
    CHECK(dst[150 * W + 10] == 0);
    /* different strides (tight buffer from a padded source) */
    memset(dst, 0, W * H * 4);
    uint32_t *wide = calloc(400 * H, 4);
    wide[20 * 400 + 30] = 7;
    rb_region_clear(&out);
    CHECK_EQ(rbr_diff_copy(dst, W, wide, 400, (rb_rect){0, 0, 64, 64}, &out), 1);
    CHECK_EQ(dst[20 * W + 30], 7);
    CHECK_EQ(out.r[0].x, 30);
    free(wide);
    /* sampled every second pixel */
    uint32_t *big = calloc(2 * W * 2 * H, 4);
    memset(dst, 0, W * H * 4);
    big[(2 * 7) * (2 * W) + 2 * 9] = 5;
    big[(2 * 7) * (2 * W) + 2 * 9 + 1] = 6;   /* between samples: never seen */
    rb_region_clear(&out);
    CHECK_EQ(rbr_diff_copy_step(dst, W, big, 2 * W, 2, (rb_rect){0, 0, W, H}, &out), 1);
    CHECK_EQ(out.r[0].x, 9);
    CHECK_EQ(out.r[0].y, 7);
    CHECK_EQ(dst[7 * W + 9], 5);
    CHECK_EQ(dst[7 * W + 10], 0);
    free(big);
    free(src);
    free(dst);
}

static void test_encode(void)
{
    uint8_t b[8 + RBR_SURFACE_LEN];
    size_t n = rbr_encode_surface(b, 3, RB_REMOTE_GUEST, 640, 480, 2560, 640 * 480 * 4, "guest");
    CHECK_EQ(n, 8 + RBR_SURFACE_LEN);
    uint32_t v[6];
    memcpy(v, b, 24);
    CHECK_EQ(v[0], RBR_SURFACE);
    CHECK_EQ(v[1], RBR_SURFACE_LEN);
    CHECK_EQ(v[2], 3);
    CHECK_EQ(v[4], 640);
    CHECK(!strcmp((const char *)b + 8 + 32, "guest"));
    uint8_t u[8 + 12 + RBR_MAX_RECTS * 16];
    rb_rect rects[2] = {{1, 2, 3, 4}, {5, 6, 7, 8}};
    n = rbr_encode_update(u, sizeof u, 11, 3, rects, 2);
    CHECK_EQ(n, 8 + 12 + 32);
    int32_t r1[4];
    memcpy(r1, u + 8 + 12 + 16, 16);
    CHECK_EQ(r1[0], 5);
    CHECK_EQ(r1[3], 8);
    CHECK_EQ(rbr_encode_update(u, 20, 1, 1, rects, 2), 0); /* too small */
}

/* --- socket round trip -------------------------------------------------- */

enum { GW = 64, GH = 48 };
static uint32_t guest_px[GW * GH];
static uint32_t guest_px2[GW * GH];
static uint32_t frame_px[GW * 2 * GH * 2]; /* the presenter's frame at scale 2 */
static const uint32_t *src_px = guest_px;
static int src_kind = RB_REMOTE_GUEST;
static int keys_down, keys_up, pointers, buttons, releases, viewers_seen, disconnects;
static int last_px = -1, last_py = -1;

static void cb_source(void *d, int source, struct rb_remote_src *s)
{
    (void)d;
    (void)source;
    s->kind = src_kind;
    s->px = src_px;
    s->w = GW;
    s->h = GH;
    s->stride_px = GW;
    s->view = src_kind == RB_REMOTE_GUEST ? "guest" : src_kind == RB_REMOTE_CHECKER ? "checkerboard" : "hold";
    s->scale = 1;
    if (src_kind == RB_REMOTE_CHECKER) { /* sampled from a 2x frame, top-left 4x4 hidden */
        s->px = frame_px;
        s->stride_px = GW * 2;
        s->step = 2;
        s->hide = (rb_rect){0, 0, 4, 4};
        s->hide_cm = 1;
    }
}
static void cb_key(void *d, uint32_t code, bool down)
{
    (void)d;
    (void)code;
    if (down)
        keys_down++;
    else
        keys_up++;
}
static void cb_pointer(void *d, enum rb_remote_kind k, int x, int y)
{
    (void)d;
    (void)k;
    pointers++;
    last_px = x;
    last_py = y;
}
static void cb_button(void *d, enum rb_remote_kind k, uint32_t b, bool down)
{
    (void)d;
    (void)k;
    (void)b;
    (void)down;
    buttons++;
}
static void cb_release(void *d) { (void)d; releases++; }
static void cb_viewers(void *d, uint32_t v, uint32_t c)
{
    (void)d;
    viewers_seen = (int)(v * 10 + c);
}
static void cb_disconnected(void *d) { (void)d; disconnects++; }

static void pump(struct rb_loop *loop, int rounds)
{
    for (int i = 0; i < rounds; i++)
        rb_loop_dispatch(loop, 5);
}

/* Receive one message (and fd) from the presenter, pumping its loop. */
static ssize_t recv_one(struct rb_loop *loop, int fd, uint8_t *buf, size_t cap, int *memfd)
{
    for (int i = 0; i < 100; i++) {
        pump(loop, 1);
        union {
            char b[CMSG_SPACE(sizeof(int))];
            struct cmsghdr align;
        } cbuf;
        struct iovec iov = {.iov_base = buf, .iov_len = cap};
        struct msghdr mh = {.msg_iov = &iov, .msg_iovlen = 1, .msg_control = cbuf.b, .msg_controllen = sizeof cbuf.b};
        ssize_t n = recvmsg(fd, &mh, MSG_DONTWAIT);
        if (n < 0 && errno == EAGAIN)
            continue;
        if (memfd) {
            *memfd = -1;
            struct cmsghdr *c = CMSG_FIRSTHDR(&mh);
            if (c && c->cmsg_type == SCM_RIGHTS)
                memcpy(memfd, CMSG_DATA(c), sizeof(int));
        }
        return n;
    }
    return -1;
}

static void send_msg(int fd, uint32_t type, int nargs, uint32_t a, uint32_t b, uint32_t c)
{
    uint8_t m[32];
    size_t n = msg(m, type, nargs, a, b, c);
    CHECK_EQ(send(fd, m, n, 0), (ssize_t)n);
}

static void test_socket(void)
{
    char path[] = "/tmp/rb-remote-test-XXXXXX";
    int tfd = mkstemp(path);
    close(tfd);
    unlink(path);
    struct rb_loop *loop = rb_loop_new();
    struct rb_remote r;
    struct rb_remote_cb cb = {cb_source, NULL, cb_disconnected, cb_key, cb_pointer, cb_button, cb_release, cb_viewers};
    CHECK_EQ(rb_remote_listen(&r, loop, path, &cb, NULL), 0);
    for (int i = 0; i < GW * GH; i++)
        guest_px[i] = (uint32_t)i;

    int c = socket(AF_UNIX, SOCK_SEQPACKET, 0);
    struct sockaddr_un addr = {.sun_family = AF_UNIX};
    snprintf(addr.sun_path, sizeof addr.sun_path, "%s", path);
    CHECK_EQ(connect(c, (struct sockaddr *)&addr, sizeof addr), 0);
    pump(loop, 2);
    /* input before HELLO is ignored */
    send_msg(c, RBR_KEY, 2, KEY_A, 1, 0);
    pump(loop, 2);
    CHECK_EQ(keys_down, 0);

    send_msg(c, RBR_HELLO, 2, 1, RB_REMOTE_SRC_COMPOSED, 0);
    uint8_t buf[2048];
    int memfd = -1;
    ssize_t n = recv_one(loop, c, buf, sizeof buf, NULL);
    CHECK_EQ(n, 12);
    n = recv_one(loop, c, buf, sizeof buf, &memfd);
    CHECK_EQ(n, 8 + RBR_SURFACE_LEN);
    uint32_t h[8];
    memcpy(h, buf, 32);
    CHECK_EQ(h[0], RBR_SURFACE);
    uint32_t gen = h[2];
    CHECK_EQ(h[3], RB_REMOTE_GUEST);
    CHECK_EQ(h[4], GW);
    CHECK_EQ(h[5], GH);
    CHECK(memfd >= 0);
    /* hostd's mapping is read-only by seal: a writable mapping must fail */
    void *wmap = mmap(NULL, GW * GH * 4, PROT_READ | PROT_WRITE, MAP_SHARED, memfd, 0);
    CHECK(wmap == MAP_FAILED);
    if (wmap != MAP_FAILED)
        munmap(wmap, GW * GH * 4);
    const uint32_t *tap = mmap(NULL, GW * GH * 4, PROT_READ, MAP_SHARED, memfd, 0);
    CHECK(tap != MAP_FAILED);
    CHECK(!memcmp(tap, guest_px, sizeof guest_px));

    /* first REQUEST: the whole surface */
    send_msg(c, RBR_REQUEST, 2, 1, 1000, 0);
    n = recv_one(loop, c, buf, sizeof buf, NULL);
    CHECK_EQ(n, 8 + 12 + 16);
    uint32_t u[5];
    memcpy(u, buf, 20);
    CHECK_EQ(u[0], RBR_UPDATE);
    CHECK_EQ(u[2], 1);
    CHECK_EQ(u[3], gen);
    CHECK_EQ(u[4], 1);

    /* nothing changed: the request is held, the source changes while it is
     * held, damage arrives: one tight rect; the buffer only changes then */
    send_msg(c, RBR_REQUEST, 2, 2, 2000, 0);
    pump(loop, 3);
    CHECK_EQ(recv(c, buf, sizeof buf, MSG_DONTWAIT), -1);
    guest_px[10 * GW + 20] = 0xdead;
    guest_px[11 * GW + 21] = 0xbeef;
    CHECK(tap[10 * GW + 20] != 0xdead);
    rb_rect strip = {0, 8, GW, 6};
    rb_remote_damage(&r, RB_REMOTE_GUEST, &strip, 1);
    rb_remote_flush(&r);
    n = recv_one(loop, c, buf, sizeof buf, NULL);
    CHECK_EQ(n, 8 + 12 + 16);
    int32_t rr[4];
    memcpy(u, buf, 20);
    memcpy(rr, buf + 20, 16);
    CHECK_EQ(u[2], 2);
    CHECK_EQ(rr[0], 20);
    CHECK_EQ(rr[1], 10);
    CHECK_EQ(rr[2], 2);
    CHECK_EQ(rr[3], 2);
    CHECK_EQ(tap[10 * GW + 20], 0xdead);

    /* wait 0 answers at once, even with no change */
    send_msg(c, RBR_REQUEST, 2, 3, 0, 0);
    n = recv_one(loop, c, buf, sizeof buf, NULL);
    memcpy(u, buf, 20);
    CHECK_EQ(u[2], 3);
    CHECK_EQ(u[4], 0);

    /* input */
    send_msg(c, RBR_KEY, 2, KEY_A, 1, 0);
    send_msg(c, RBR_KEY, 2, KEY_A, 0, 0);
    send_msg(c, RBR_POINTER, 3, gen, 5, 6);
    send_msg(c, RBR_POINTER, 3, gen + 1, 7, 8); /* stale generation */
    send_msg(c, RBR_POINTER, 3, gen, GW, 0);    /* outside the surface */
    send_msg(c, RBR_BUTTON, 2, 0, 1, 0);
    send_msg(c, RBR_VIEWERS, 2, 2, 1, 0);
    send_msg(c, RBR_RELEASE_ALL, 0, 0, 0, 0);
    pump(loop, 4);
    CHECK_EQ(keys_down, 1);
    CHECK_EQ(keys_up, 1);
    CHECK_EQ(pointers, 1);
    CHECK_EQ(last_px, 5);
    CHECK_EQ(last_py, 6);
    CHECK_EQ(buttons, 1);
    CHECK_EQ(viewers_seen, 21);
    CHECK_EQ(releases, 1);

    /* a new guest surface whose pixels change before hostd asks: the full
     * update carries the pixels of the moment it is sent, not of creation */
    for (int i = 0; i < GW * GH; i++)
        guest_px2[i] = 0x111111;
    src_px = guest_px2;
    rb_remote_flush(&r);
    int memfdg = -1;
    n = recv_one(loop, c, buf, sizeof buf, &memfdg);
    memcpy(h, buf, 32);
    CHECK_EQ(h[0], RBR_SURFACE);
    CHECK_EQ(h[2], gen + 1);
    guest_px2[5 * GW + 5] = 0x222222;   /* no damage message: the surface is pending a full update */
    rb_rect any = {5, 5, 1, 1};
    rb_remote_damage(&r, RB_REMOTE_GUEST, &any, 1);
    send_msg(c, RBR_REQUEST, 2, 4, 0, 0);
    n = recv_one(loop, c, buf, sizeof buf, NULL);
    memcpy(u, buf, 20);
    CHECK_EQ(u[2], 4);
    CHECK_EQ(u[4], 1);
    const uint32_t *tg = mmap(NULL, GW * GH * 4, PROT_READ, MAP_SHARED, memfdg, 0);
    CHECK(tg != MAP_FAILED && tg[5 * GW + 5] == 0x222222);
    munmap((void *)tg, GW * GH * 4);
    close(memfdg);

    /* the source changes (checkerboard view: the frame sampled every second
     * pixel, the indicator's box refilled with the pattern) */
    for (int y = 0; y < GH * 2; y++)
        for (int x = 0; x < GW * 2; x++)
            frame_px[y * GW * 2 + x] = (uint32_t)(0x10000 + y * 1000 + x);
    src_kind = RB_REMOTE_CHECKER;
    rb_remote_flush(&r);
    int memfd2 = -1;
    n = recv_one(loop, c, buf, sizeof buf, &memfd2);
    memcpy(h, buf, 32);
    CHECK_EQ(h[0], RBR_SURFACE);
    CHECK_EQ(h[2], gen + 2);
    CHECK_EQ(h[3], RB_REMOTE_CHECKER);
    CHECK(memfd2 >= 0);
    const uint32_t *chk = mmap(NULL, GW * GH * 4, PROT_READ, MAP_SHARED, memfd2, 0);
    CHECK(chk != MAP_FAILED);
    CHECK_EQ(chk[10 * GW + 7], frame_px[20 * GW * 2 + 14]);  /* sampled */
    CHECK(chk[0] != chk[1] && (chk[0] == 0xffffff || chk[0] == 0));   /* hidden box: the pattern */
    CHECK(chk[3 * GW + 3] != frame_px[6 * GW * 2 + 6]);
    /* damage in the frame updates the sampled copy, the box stays hidden */
    send_msg(c, RBR_REQUEST, 2, 5, 0, 0);
    n = recv_one(loop, c, buf, sizeof buf, NULL);   /* the full update of the new surface */
    frame_px[20 * GW * 2 + 14] = 0xabcdef;
    frame_px[2 * GW * 2 + 2] = 0x123456;
    rb_rect box = {0, 0, 4, 4};                     /* damage inside the hidden box only */
    rb_remote_damage(&r, RB_REMOTE_CHECKER, &box, 1);
    send_msg(c, RBR_REQUEST, 2, 6, 0, 0);
    n = recv_one(loop, c, buf, sizeof buf, NULL);
    memcpy(u, buf, 20);
    CHECK_EQ(u[2], 6);
    CHECK(chk[1 * GW + 1] != 0x123456);                       /* refilled with the pattern */
    CHECK_EQ(chk[10 * GW + 7], 0x10000 + 20 * 1000 + 14);      /* not damaged: unchanged */
    rb_rect wide_damage = {0, 0, 16, 16};
    rb_remote_damage(&r, RB_REMOTE_CHECKER, &wide_damage, 1);
    send_msg(c, RBR_REQUEST, 2, 7, 0, 0);
    n = recv_one(loop, c, buf, sizeof buf, NULL);
    CHECK_EQ(chk[10 * GW + 7], 0xabcdef);
    /* a picture-less source (held frame) sends no descriptor */
    src_kind = RB_REMOTE_NONE;
    rb_remote_flush(&r);
    int memfd3 = -2;
    n = recv_one(loop, c, buf, sizeof buf, &memfd3);
    memcpy(h, buf, 32);
    CHECK_EQ(h[3], RB_REMOTE_NONE);
    CHECK_EQ(memfd3, -1);

    /* disconnect releases the remote input */
    close(c);
    pump(loop, 3);
    CHECK_EQ(disconnects, 1);
    CHECK(!rb_remote_connected(&r));
    munmap((void *)tap, GW * GH * 4);
    munmap((void *)chk, GW * GH * 4);
    close(memfd);
    close(memfd2);
    rb_remote_shutdown(&r);
    rb_loop_free(loop);
}

/* --- a Mac OS mode switch on the remote screen ---------------------------- */

/* As the presenter's remote_source: Mac OS's framebuffer, held (no picture)
 * while the switch-hold decision says the new surface is not complete. */
enum { SW = 832, SH = 624, OW = 640, OH = 480 };
static uint32_t *sw_px;
static int sw_w, sw_h;
static bool sw_holding;

static void cb_switch_source(void *d, int source, struct rb_remote_src *s)
{
    (void)d;
    (void)source;
    s->scale = 1;
    rb_remote_guest_source(s, sw_px, sw_w, sw_h, sw_w, sw_holding);
}

/* A desktop (pattern, menu bar, `icons` icons) at w x h; the dither clear. */
static void sw_desktop(uint32_t *f, int w, int h, int icons)
{
    for (int y = 0; y < h; y++)
        for (int x = 0; x < w; x++)
            f[y * w + x] = y < 19 ? 0xddddddu : y == 19 ? 0 : (((x / 7 + y / 5) % 9) ? 0x5c5c99u : 0x6a6aa8u);
    for (int i = 0; i < icons; i++)
        for (int y = 40 + i * 60; y < 72 + i * 60; y++)
            for (int x = w - 60; x < w - 28; x++)
                f[y * w + x] = 0x203040u + (uint32_t)(x * 7 + y);
}

static void sw_dither(uint32_t *f, int w, int h)
{
    for (int y = 0; y < h; y++)
        for (int x = 0; x < w; x++)
            f[y * w + x] = (x + y) & 1 ? 0xffffffu : 0;
}

static uint32_t recv_surface(struct rb_loop *loop, int c, uint32_t *kind, uint32_t *w, uint32_t *h, char *view,
                             int *memfd)
{
    uint8_t buf[256];
    uint32_t hd[8];
    ssize_t n = recv_one(loop, c, buf, sizeof buf, memfd);
    CHECK_EQ(n, 8 + RBR_SURFACE_LEN);
    memcpy(hd, buf, 32);
    CHECK_EQ(hd[0], RBR_SURFACE);
    *kind = hd[3];
    *w = hd[4];
    *h = hd[5];
    memcpy(view, buf + 8 + 32, 16);
    view[15] = 0;
    return hd[2];
}

/* REQUEST with wait 0: the UPDATE's rectangle count (-1: no UPDATE) and total area. */
static int request_now(struct rb_loop *loop, int c, uint32_t seq, long *area)
{
    uint8_t buf[2048];
    send_msg(c, RBR_REQUEST, 2, seq, 0, 0);
    ssize_t n = recv_one(loop, c, buf, sizeof buf, NULL);
    uint32_t u[5];
    if (n < 20)
        return -1;
    memcpy(u, buf, 20);
    if (u[0] != RBR_UPDATE || u[1] + 8 != (uint32_t)n)
        return -1;
    *area = 0;
    for (uint32_t i = 0; i < u[4]; i++) {
        int32_t r[4];
        memcpy(r, buf + 20 + i * 16, 16);
        *area += (long)r[2] * r[3];
    }
    return (int)u[4];
}

static void test_switch_hold(void)
{
    char path[] = "/tmp/rb-remote-switch-XXXXXX";
    int tfd = mkstemp(path);
    close(tfd);
    unlink(path);
    struct rb_loop *loop = rb_loop_new();
    struct rb_remote r;
    struct rb_remote_cb cb = {cb_switch_source, NULL, NULL, NULL, NULL, NULL, NULL, NULL};
    uint32_t *old = calloc((size_t)OW * OH, 4), *cur = calloc((size_t)SW * SH, 4), *done = calloc((size_t)SW * SH, 4);
    sw_desktop(old, OW, OH, 5);
    sw_desktop(done, SW, SH, 5);
    sw_px = old, sw_w = OW, sw_h = OH, sw_holding = false;
    CHECK_EQ(rb_remote_listen(&r, loop, path, &cb, NULL), 0);
    int c = socket(AF_UNIX, SOCK_SEQPACKET, 0);
    struct sockaddr_un addr = {.sun_family = AF_UNIX};
    snprintf(addr.sun_path, sizeof addr.sun_path, "%s", path);
    CHECK_EQ(connect(c, (struct sockaddr *)&addr, sizeof addr), 0);
    pump(loop, 2);
    send_msg(c, RBR_HELLO, 2, 1, RB_REMOTE_SRC_COMPOSED, 0);
    uint8_t buf[64];
    CHECK_EQ(recv_one(loop, c, buf, sizeof buf, NULL), 12);
    uint32_t kind, w, h;
    char view[16];
    int memfd = -1;
    uint32_t gen = recv_surface(loop, c, &kind, &w, &h, view, &memfd);
    CHECK_EQ(kind, RB_REMOTE_GUEST);
    CHECK_EQ(w, OW);
    long area = 0;
    CHECK_EQ(request_now(loop, c, 1, &area), 1);
    CHECK_EQ(area, (long)OW * OH);
    close(memfd);

    /* Mac OS switches 640 x 480 -> 832 x 624 (the N5105 with the ATI Radeon):
     * the new surface holds the old picture at the new stride, then Mac OS's
     * clear, then the desktop drawn in steps. The presenter decides with
     * rb_switch_hold_check, as check_switch_hold does on every DAMAGE. */
    memset(cur, 0, (size_t)SW * SH * 4);
    memcpy(cur, old, (size_t)OW * OH * 4);
    sw_px = cur, sw_w = SW, sw_h = SH;
    struct rb_switch_hold sh;
    uint64_t t0 = 1000;
    rb_switch_hold_begin(&sh, cur, SW, SH, SW, t0);
    sw_holding = true;
    rb_remote_flush(&r);
    memfd = -2;
    uint32_t g = recv_surface(loop, c, &kind, &w, &h, view, &memfd);
    CHECK_EQ(g, gen + 1);
    CHECK_EQ(kind, RB_REMOTE_NONE);       /* hostd keeps its last picture */
    CHECK(!strcmp(view, "switching"));
    CHECK_EQ(memfd, -1);
    uint32_t seq = 2;
    const long steps[] = {25, 105, 170, 240, 305};
    uint64_t last_damage = t0;
    for (int i = 0; i < 5; i++) {
        if (i == 0)
            sw_dither(cur, SW, SH);
        else
            sw_desktop(cur, SW, SH, i == 1 ? 0 : i == 2 ? 1 : i == 3 ? 3 : 5);
        last_damage = t0 + (uint64_t)steps[i];
        rb_rect all = {0, 0, SW, SH};
        rb_remote_damage(&r, RB_REMOTE_GUEST, &all, 1);
        int v = rb_switch_hold_check(&sh, cur, SW, SH, SW, last_damage, last_damage);
        CHECK_EQ(v, RB_SWITCH_HOLD);       /* the dither and every partial desktop: hold */
        sw_holding = v == RB_SWITCH_HOLD;
        rb_remote_flush(&r);
        CHECK_EQ(request_now(loop, c, seq++, &area), 0);   /* nothing streamed, no new surface */
    }
    /* drawn and still for RB_SWITCH_SETTLE_MS: the presenter cuts, the remote with it */
    int v = rb_switch_hold_check(&sh, cur, SW, SH, SW, last_damage + RB_SWITCH_SETTLE_MS, last_damage);
    CHECK_EQ(v, RB_SWITCH_SETTLED);
    sw_holding = v == RB_SWITCH_HOLD;
    rb_remote_flush(&r);
    memfd = -1;
    g = recv_surface(loop, c, &kind, &w, &h, view, &memfd);
    CHECK_EQ(g, gen + 2);
    CHECK_EQ(kind, RB_REMOTE_GUEST);
    CHECK_EQ(w, SW);
    CHECK_EQ(h, SH);
    CHECK(memfd >= 0);
    const uint32_t *tap = mmap(NULL, (size_t)SW * SH * 4, PROT_READ, MAP_SHARED, memfd, 0);
    CHECK(tap != MAP_FAILED);
    CHECK_EQ(request_now(loop, c, seq++, &area), 1);
    CHECK_EQ(area, (long)SW * SH);
    if (tap != MAP_FAILED) {
        CHECK(!memcmp(tap, done, (size_t)SW * SH * 4));   /* the complete desktop, first and whole */
        munmap((void *)tap, (size_t)SW * SH * 4);
    }
    close(memfd);

    /* the helper itself */
    struct rb_remote_src s;
    memset(&s, 0, sizeof s);
    rb_remote_guest_source(&s, done, SW, SH, SW, false);
    CHECK_EQ(s.kind, RB_REMOTE_GUEST);
    CHECK(s.px == done && s.w == SW && s.h == SH && s.stride_px == SW && !strcmp(s.view, "guest"));
    rb_remote_guest_source(&s, done, SW, SH, SW, true);
    CHECK_EQ(s.kind, RB_REMOTE_NONE);
    CHECK(s.px == NULL && s.w == 0 && !strcmp(s.view, "switching"));

    close(c);
    pump(loop, 3);
    rb_remote_shutdown(&r);
    rb_loop_free(loop);
    free(old);
    free(cur);
    free(done);
}

/* --- badge -------------------------------------------------------------- */

static void menubar(uint32_t *px, int w, int stride)
{
    for (int x = 0; x < w; x++) {
        px[x] = 0xffffff;
        for (int y = 1; y < 18; y++)
            px[y * stride + x] = 0xdddddd;
        px[18 * stride + x] = 0x999999;
        px[19 * stride + x] = 0;
    }
}

static void title(uint32_t *px, int stride, int x0, int x1)
{
    for (int x = x0; x < x1; x++)
        px[(6 + x % 7) * stride + x] = 0;
}

static void test_badge(void)
{
    enum { W = 1024, H = 768 };
    uint32_t *px = calloc(W * H, 4);
    for (int i = 0; i < W * H; i++)
        px[i] = 0x63639c;
    menubar(px, W, W);
    title(px, W, 10, 310);   /* Apple, File ... Help */
    title(px, W, 880, 930);  /* clock */
    title(px, W, 940, 1010); /* application menu */
    int x = rb_badge_menubar_gap(px, W, H, W, 12);
    CHECK_EQ(x, 880 - 12 - RB_BADGE_W);
    /* the widest gap wins (a short one next to the clock is skipped) */
    title(px, W, 600, 860);
    x = rb_badge_menubar_gap(px, W, H, W, 12);
    CHECK_EQ(x, 600 - 12 - RB_BADGE_W);
    /* a full bar of menus: no room */
    title(px, W, 310, 600);
    CHECK_EQ(rb_badge_menubar_gap(px, W, H, W, 12), -1);
    /* no menu bar (a full-screen game) */
    for (int i = 0; i < W * H; i++)
        px[i] = (uint32_t)rbt_rand() & 0xffffff;
    CHECK_EQ(rb_badge_menubar_gap(px, W, H, W, 12), -1);
    /* a uniform screen is not a menu bar either (no dark bottom row) */
    for (int i = 0; i < W * H; i++)
        px[i] = 0xdddddd;
    CHECK_EQ(rb_badge_menubar_gap(px, W, H, W, 12), -1);
    CHECK_EQ(rb_badge_menubar_gap(NULL, W, H, W, 12), -1);

    /* rendering: 2x scale, clipped, transparent outside the glyph */
    memset(px, 0x11, (size_t)W * H * 4);
    rb_rect t = {100, 4, 32, 32};
    rb_badge_render(px, W, t, (rb_rect){0, 0, 116, H}, RB_BADGE_CONTROL, false);
    CHECK_EQ(px[4 * W + 100], 0x11111111u);         /* glyph corner is transparent */
    CHECK_EQ(px[(4 + 2) * W + 102], 0u);            /* outline */
    CHECK_EQ(px[(4 + 4) * W + 104], 0x00ffffffu);   /* screen */
    CHECK_EQ(px[(4 + 2) * W + 120], 0x11111111u);   /* clipped */
    rb_badge_render(px, W, (rb_rect){200, 4, 20, 20}, (rb_rect){0, 0, W, H}, RB_BADGE_VIEW, true);
    CHECK_EQ(px[4 * W + 200], 0u);                  /* box frame */
    CHECK_EQ(px[5 * W + 201], 0x00ddddddu);         /* box face */
    free(px);
}

int main(void)
{
    test_parse();
    test_diff();
    test_encode();
    test_socket();
    test_switch_hold();
    test_badge();
    TEST_DONE();
}
