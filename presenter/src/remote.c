#include "remote.h"

#include <errno.h>
#include <fcntl.h>
#include <linux/input-event-codes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>
#include <drm_fourcc.h>

#include "checker.h"
#include "log.h"

#ifndef F_SEAL_FUTURE_WRITE
#define F_SEAL_FUTURE_WRITE 0x0010
#endif

/* Changed pixels on nearby rows merge into one rectangle when their column
 * spans are this close (a moving cursor is one rectangle, not sixteen). */
#define BAND_GAP 32
/* Damage is diffed in column tiles this wide (pixels). */
#define DIFF_TILE 128

static uint32_t rd32(const uint8_t *p)
{
    uint32_t v;
    memcpy(&v, p, 4);
    return v;
}

static size_t put32(uint8_t *b, size_t off, uint32_t v)
{
    memcpy(b + off, &v, 4);
    return off + 4;
}

/* ------------------------------------------------------------------ */
/* pure helpers                                                        */

void rb_remote_guest_source(struct rb_remote_src *s, const uint32_t *px, int w, int h, int stride_px,
                            bool holding)
{
    if (holding) {
        s->kind = RB_REMOTE_NONE;
        s->px = NULL;
        s->w = s->h = s->stride_px = 0;
        s->view = "switching";
        return;
    }
    s->kind = RB_REMOTE_GUEST;
    s->px = px;
    s->w = w;
    s->h = h;
    s->stride_px = stride_px;
    s->view = "guest";
}

int rbr_parse(const uint8_t *buf, size_t n, struct rbr_msg *m, char *err, size_t errlen)
{
    memset(m, 0, sizeof *m);
    if (n < 8) {
        snprintf(err, errlen, "short message (%zu bytes)", n);
        return -1;
    }
    m->type = rd32(buf);
    uint32_t len = rd32(buf + 4);
    if ((size_t)len != n - 8) {
        snprintf(err, errlen, "type %u: header length %u != payload %zu", m->type, len, n - 8);
        return -1;
    }
    const uint8_t *p = buf + 8;
    uint32_t want;
    switch (m->type) {
    case RBR_HELLO: case RBR_REQUEST: case RBR_KEY: case RBR_BUTTON: case RBR_VIEWERS: want = 8; break;
    case RBR_SOURCE: want = 4; break;
    case RBR_POINTER: want = 12; break;
    case RBR_RELEASE_ALL: want = 0; break;
    default:
        snprintf(err, errlen, "unknown message type %u", m->type);
        return -1;
    }
    if (len != want) {
        snprintf(err, errlen, "type %u: length %u, expected %u", m->type, len, want);
        return -1;
    }
    if (want >= 4)
        m->a = rd32(p);
    if (want >= 8)
        m->b = rd32(p + 4);
    if (want >= 12)
        m->c = rd32(p + 8);
    switch (m->type) {
    case RBR_HELLO:
    case RBR_SOURCE: {
        uint32_t src = m->type == RBR_HELLO ? m->b : m->a;
        if (src > RB_REMOTE_SRC_GUEST) {
            snprintf(err, errlen, "unknown source %u", src);
            return -1;
        }
        break;
    }
    case RBR_REQUEST:
        if (m->b > RBR_WAIT_MAX_MS)
            m->b = RBR_WAIT_MAX_MS;
        break;
    case RBR_KEY:
        if (m->a == 0 || m->a >= KEY_CNT || m->b > 1) {
            snprintf(err, errlen, "invalid key %u/%u", m->a, m->b);
            return -1;
        }
        break;
    case RBR_BUTTON:
        if (m->a > 4 || m->b > 1) {
            snprintf(err, errlen, "invalid button %u/%u", m->a, m->b);
            return -1;
        }
        break;
    case RBR_POINTER: {
        int32_t x = (int32_t)m->b, y = (int32_t)m->c;
        if (x < 0 || y < 0 || x > 16384 || y > 16384) {
            snprintf(err, errlen, "pointer %d,%d out of range", x, y);
            return -1;
        }
        break;
    }
    case RBR_VIEWERS:
        if (m->a > 1000 || m->b > m->a) {
            snprintf(err, errlen, "invalid viewer counts %u/%u", m->a, m->b);
            return -1;
        }
        break;
    }
    return 0;
}

static void add_band(rb_region *out, int x0, int y0, int x1, int y1)
{
    if (x1 > x0 && y1 > y0)
        rb_region_add(out, (rb_rect){x0, y0, x1 - x0, y1 - y0});
}

/* One column tile of a damage rect: rows whose pixels differ become bands
 * (consecutive rows whose changed spans overlap or nearly touch). The source
 * is sampled every `step` pixels (1: a plain copy). */
static void diff_tile(uint32_t *dst, int dst_stride, const uint32_t *src, int src_stride, int step, rb_rect r,
                      rb_region *out)
{
    bool open = false;
    int bx0 = 0, bx1 = 0, by0 = 0;
    for (int y = r.y; y < r.y + r.h; y++) {
        uint32_t *d = dst + (size_t)y * dst_stride + r.x;
        const uint32_t *s = src + (size_t)y * step * src_stride + (size_t)r.x * step;
        int x0 = 0, x1 = r.w;
        if (step == 1) {
            if (!memcmp(d, s, (size_t)r.w * 4))
                x0 = r.w;
            else {
                while (x0 < r.w && d[x0] == s[x0])
                    x0++;
                while (x1 > x0 && d[x1 - 1] == s[x1 - 1])
                    x1--;
                if (x1 <= x0) { /* the source changed under us between the compare and the scan */
                    x0 = 0;
                    x1 = r.w;
                }
                memcpy(d + x0, s + x0, (size_t)(x1 - x0) * 4);
            }
        } else {
            while (x0 < r.w && d[x0] == s[(size_t)x0 * step])
                x0++;
            while (x1 > x0 && d[x1 - 1] == s[(size_t)(x1 - 1) * step])
                x1--;
            for (int x = x0; x < x1; x++)
                d[x] = s[(size_t)x * step];
        }
        if (x1 <= x0) {
            if (open) {
                add_band(out, bx0, by0, bx1, y);
                open = false;
            }
            continue;
        }
        int ax0 = r.x + x0, ax1 = r.x + x1;
        if (open && ax0 <= bx1 + BAND_GAP && ax1 >= bx0 - BAND_GAP) {
            if (ax0 < bx0)
                bx0 = ax0;
            if (ax1 > bx1)
                bx1 = ax1;
        } else {
            if (open)
                add_band(out, bx0, by0, bx1, y);
            open = true;
            bx0 = ax0;
            bx1 = ax1;
            by0 = y;
        }
    }
    if (open)
        add_band(out, bx0, by0, bx1, r.y + r.h);
}

int rbr_diff_copy_step(uint32_t *dst, int dst_stride, const uint32_t *src, int src_stride, int step, rb_rect r,
                       rb_region *out)
{
    /* Column tiles keep changes far apart on the same rows (the cursor and
     * the menu-bar clock) in separate rectangles; neighbours merge again in
     * the region when their union is tight. */
    if (step < 1)
        step = 1;
    for (int x = r.x; x < r.x + r.w; x += DIFF_TILE) {
        int w = r.x + r.w - x < DIFF_TILE ? r.x + r.w - x : DIFF_TILE;
        diff_tile(dst, dst_stride, src, src_stride, step, (rb_rect){x, r.y, w, r.h}, out);
    }
    return out->n;
}

int rbr_diff_copy(uint32_t *dst, int dst_stride, const uint32_t *src, int src_stride, rb_rect r, rb_region *out)
{
    return rbr_diff_copy_step(dst, dst_stride, src, src_stride, 1, r, out);
}

size_t rbr_encode_surface(uint8_t out[8 + RBR_SURFACE_LEN], uint32_t gen, uint32_t kind, uint32_t w, uint32_t h,
                          uint32_t stride, uint64_t size, const char *view)
{
    size_t n = put32(out, 0, RBR_SURFACE);
    n = put32(out, n, RBR_SURFACE_LEN);
    n = put32(out, n, gen);
    n = put32(out, n, kind);
    n = put32(out, n, w);
    n = put32(out, n, h);
    n = put32(out, n, stride);
    n = put32(out, n, DRM_FORMAT_XRGB8888);
    memcpy(out + n, &size, 8);
    n += 8;
    memset(out + n, 0, 16);
    if (view)
        memcpy(out + n, view, strnlen(view, 15));
    return n + 16;
}

size_t rbr_encode_update(uint8_t *out, size_t cap, uint32_t seq, uint32_t gen, const rb_rect *rects, int n)
{
    if (n < 0)
        n = 0;
    if (n > RBR_MAX_RECTS)
        n = RBR_MAX_RECTS;
    size_t len = 12 + (size_t)n * 16;
    if (cap < 8 + len)
        return 0;
    size_t o = put32(out, 0, RBR_UPDATE);
    o = put32(out, o, (uint32_t)len);
    o = put32(out, o, seq);
    o = put32(out, o, gen);
    o = put32(out, o, (uint32_t)n);
    for (int i = 0; i < n; i++) {
        int32_t v[4] = {rects[i].x, rects[i].y, rects[i].w, rects[i].h};
        memcpy(out + o, v, 16);
        o += 16;
    }
    return o;
}

/* ------------------------------------------------------------------ */
/* connection                                                          */

bool rb_remote_connected(const struct rb_remote *r) { return r->fd >= 0 && r->hello; }

static void drop_buffer(struct rb_remote *r)
{
    if (r->buf)
        munmap(r->buf, r->buf_len);
    if (r->memfd >= 0)
        close(r->memfd);
    r->buf = NULL;
    r->buf_len = 0;
    r->memfd = -1;
    r->w = r->h = 0;
    r->kind = RB_REMOTE_NONE;
    r->view[0] = 0;
    r->src_px = NULL;
    r->src_stride = 0;
    r->src_step = 1;
    r->hide = (rb_rect){0, 0, 0, 0};
    r->hide_cm = 1;
    rb_region_clear(&r->pending);
    r->full = false;
}

static void disconnect(struct rb_remote *r, const char *why)
{
    if (r->fd < 0)
        return;
    rb_watch_remove(r->watch);
    r->watch = NULL;
    close(r->fd);
    r->fd = -1;
    bool was = r->hello;
    r->hello = false;
    r->held = false;
    rb_timer_arm(r->wait_timer, 0);
    drop_buffer(r);
    r->viewers = r->controllers = 0;
    rb_log("remote: hostd disconnected (%s) after %llu updates, %llu rects, %llu pixels", why,
           (unsigned long long)r->updates, (unsigned long long)r->rects, (unsigned long long)r->pixels);
    if (was && r->cb.disconnected)
        r->cb.disconnected(r->cbdata);
}

static int send_msg(struct rb_remote *r, const void *buf, size_t len, int fd)
{
    if (r->fd < 0)
        return -1;
    struct iovec iov = {.iov_base = (void *)buf, .iov_len = len};
    union {
        char b[CMSG_SPACE(sizeof(int))];
        struct cmsghdr align;
    } cbuf;
    struct msghdr mh = {.msg_iov = &iov, .msg_iovlen = 1};
    if (fd >= 0) {
        memset(&cbuf, 0, sizeof cbuf);
        mh.msg_control = cbuf.b;
        mh.msg_controllen = sizeof cbuf.b;
        struct cmsghdr *c = CMSG_FIRSTHDR(&mh);
        c->cmsg_level = SOL_SOCKET;
        c->cmsg_type = SCM_RIGHTS;
        c->cmsg_len = CMSG_LEN(sizeof(int));
        memcpy(CMSG_DATA(c), &fd, sizeof(int));
    }
    ssize_t n = sendmsg(r->fd, &mh, MSG_DONTWAIT | MSG_NOSIGNAL);
    if (n == (ssize_t)len)
        return 0;
    /* hostd answers every message promptly (UPDATE is a reply to its own
     * REQUEST); a full socket means it is stuck. It reconnects. */
    disconnect(r, n < 0 ? strerror(errno) : "short send");
    return -1;
}

static int make_buffer(struct rb_remote *r, int w, int h)
{
    size_t len = (size_t)w * (size_t)h * 4;
    int fd = memfd_create("rb-remote-tap", MFD_CLOEXEC | MFD_ALLOW_SEALING);
    if (fd < 0)
        return -1;
    if (ftruncate(fd, (off_t)len) < 0) {
        close(fd);
        return -1;
    }
    void *map = mmap(NULL, len, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (map == MAP_FAILED) {
        close(fd);
        return -1;
    }
    /* hostd maps it read-only and can neither resize nor write it. */
    if (fcntl(fd, F_ADD_SEALS, F_SEAL_SHRINK | F_SEAL_GROW | F_SEAL_FUTURE_WRITE | F_SEAL_SEAL) < 0)
        fcntl(fd, F_ADD_SEALS, F_SEAL_SHRINK | F_SEAL_GROW);
    r->memfd = fd;
    r->buf = map;
    r->buf_len = len;
    r->w = w;
    r->h = h;
    return 0;
}

static bool has_picture(enum rb_remote_kind k) { return k != RB_REMOTE_NONE; }

static bool copies(const struct rb_remote *r) { return r->buf && r->src_px; }

/* The local indicator's box is never streamed: put the checkerboard back. */
static void refill_hidden(struct rb_remote *r)
{
    rb_rect h;
    if (r->buf && rb_rect_intersect(r->hide, (rb_rect){0, 0, r->w, r->h}, &h))
        rb_checker_fill_units(r->buf, r->w, h.x, h.y, h.w, h.h, r->hide_cm > 0 ? r->hide_cm : 1, r->src_step);
}

/* Copy the whole source into the tap buffer (a new surface, or a full update). */
static void copy_full(struct rb_remote *r)
{
    if (!copies(r))
        return;
    for (int y = 0; y < r->h; y++) {
        uint32_t *d = r->buf + (size_t)y * r->w;
        const uint32_t *s = r->src_px + (size_t)y * r->src_step * r->src_stride;
        if (r->src_step == 1) {
            memcpy(d, s, (size_t)r->w * 4);
        } else {
            for (int x = 0; x < r->w; x++)
                d[x] = s[(size_t)x * r->src_step];
        }
    }
    refill_hidden(r);
}

/* Re-evaluate the source; on any change start a new tap surface. */
static void check_surface(struct rb_remote *r)
{
    struct rb_remote_src s;
    memset(&s, 0, sizeof s);
    r->cb.source(r->cbdata, r->source, &s);
    const char *view = s.view ? s.view : "none";
    int step = s.step > 1 ? s.step : 1;
    if (has_picture(s.kind) && (s.w <= 0 || s.h <= 0 || s.w > 8192 || s.h > 8192 ||
                                ((s.kind == RB_REMOTE_GUEST || s.kind == RB_REMOTE_FRAME) && !s.px)))
        s.kind = RB_REMOTE_NONE;
    bool same = s.kind == r->kind && !strncmp(view, r->view, sizeof r->view - 1);
    if (same && has_picture(s.kind))
        same = s.w == r->w && s.h == r->h && s.px == r->src_px && s.stride_px == r->src_stride &&
               step == r->src_step;
    if (same) {
        if (memcmp(&s.hide, &r->hide, sizeof s.hide) || s.hide_cm != r->hide_cm) {
            /* The indicator moved, came or went: re-copy both places. */
            rb_region_add(&r->pending, rb_rect_clip(r->hide, r->w, r->h));
            r->hide = s.hide;
            r->hide_cm = s.hide_cm;
            rb_region_add(&r->pending, rb_rect_clip(r->hide, r->w, r->h));
        }
        return;
    }
    drop_buffer(r);
    r->gen++;
    r->kind = s.kind;
    snprintf(r->view, sizeof r->view, "%s", view);
    if (has_picture(s.kind) && make_buffer(r, s.w, s.h) < 0) {
        rb_log("remote: cannot allocate a %dx%d tap buffer: %s", s.w, s.h, strerror(errno));
        r->kind = RB_REMOTE_NONE;
    }
    if (has_picture(r->kind)) {
        r->src_px = s.px;
        r->src_stride = s.stride_px;
        r->src_step = step;
        r->hide = s.hide;
        r->hide_cm = s.hide_cm;
    }
    if (copies(r))
        copy_full(r);
    else if (r->kind == RB_REMOTE_CHECKER)
        rb_checker_fill_units(r->buf, r->w, 0, 0, r->w, r->h, s.hide_cm > 0 ? s.hide_cm : 1, step);
    /* black: a new memfd is zero-filled; none: no buffer */
    r->full = has_picture(r->kind);
    uint8_t msg[8 + RBR_SURFACE_LEN];
    size_t n = rbr_encode_surface(msg, r->gen, r->kind, (uint32_t)r->w, (uint32_t)r->h, (uint32_t)r->w * 4,
                                  r->buf_len, r->view);
    rb_log("remote: surface %u %s %dx%d (%s)", r->gen, r->view, r->w, r->h,
           r->kind == RB_REMOTE_GUEST ? "guest" : r->kind == RB_REMOTE_FRAME ? "frame"
           : r->kind == RB_REMOTE_CHECKER ? "checkerboard" : r->kind == RB_REMOTE_BLACK ? "black" : "none");
    send_msg(r, msg, n, has_picture(r->kind) ? r->memfd : -1);
}

/* Answer the held REQUEST with what changed since the last UPDATE (force:
 * even when nothing did). The tap buffer changes only here, while hostd waits
 * for this answer. */
static void answer(struct rb_remote *r, bool force)
{
    if (!r->held || r->fd < 0)
        return;
    rb_region out;
    rb_region_clear(&out);
    if (r->full) {
        /* The source may have changed since the surface was created (damage
         * is not collected meanwhile): send it as it is now. */
        copy_full(r);
        rb_region_add(&out, (rb_rect){0, 0, r->w, r->h});
        r->full = false;
        rb_region_clear(&r->pending);
    } else if (!rb_region_empty(&r->pending) && copies(r)) {
        for (int i = 0; i < r->pending.n; i++)
            rbr_diff_copy_step(r->buf, r->w, r->src_px, r->src_stride, r->src_step,
                               rb_rect_clip(r->pending.r[i], r->w, r->h), &out);
        rb_region_clear(&r->pending);
        if (!rb_region_empty(&out))
            refill_hidden(r);
    }
    if (rb_region_empty(&out) && !force)
        return;
    uint8_t msg[8 + 12 + RBR_MAX_RECTS * 16];
    size_t n = rbr_encode_update(msg, sizeof msg, r->held_seq, r->gen, out.r, out.n);
    r->held = false;
    rb_timer_arm(r->wait_timer, 0);
    r->updates++;
    r->rects += (uint64_t)out.n;
    r->pixels += (uint64_t)rb_region_area(&out);
    send_msg(r, msg, n, -1);
}

void rb_remote_flush(struct rb_remote *r)
{
    if (!rb_remote_connected(r))
        return;
    check_surface(r);
    if (r->fd >= 0)
        answer(r, false);
}

void rb_remote_damage(struct rb_remote *r, enum rb_remote_kind kind, const rb_rect *rects, int n)
{
    if (!rb_remote_connected(r) || kind != r->kind || r->full)
        return;
    for (int i = 0; i < n; i++)
        rb_region_add(&r->pending, rb_rect_clip(rects[i], r->w, r->h));
}

static void wait_timer_fn(void *data)
{
    struct rb_remote *r = data;
    if (!r->held)
        return;
    check_surface(r);
    answer(r, true);
}

static void handle(struct rb_remote *r, const struct rbr_msg *m)
{
    switch (m->type) {
    case RBR_HELLO: {
        r->hello = true;
        r->source = (int)m->b;
        uint8_t msg[12];
        size_t n = put32(msg, 0, RBR_HELLO);
        n = put32(msg, n, 4);
        n = put32(msg, n, RBR_VERSION);
        rb_log("remote: hostd HELLO version %u source %s", m->a, m->b ? "guest" : "composed");
        if (send_msg(r, msg, n, -1) < 0)
            return;
        if (r->cb.connected)
            r->cb.connected(r->cbdata);
        check_surface(r);
        break;
    }
    case RBR_SOURCE:
        r->source = (int)m->a;
        check_surface(r);
        break;
    case RBR_REQUEST:
        if (r->held)
            answer(r, true); /* one reply per REQUEST, in order */
        if (r->fd < 0)
            return;
        r->held = true;
        r->held_seq = m->a;
        check_surface(r);
        answer(r, m->b == 0);
        if (r->held)
            rb_timer_arm(r->wait_timer, m->b);
        break;
    case RBR_KEY:
        r->input_events++;
        if (r->cb.key)
            r->cb.key(r->cbdata, m->a, m->b != 0);
        break;
    case RBR_POINTER: {
        int x = (int)(int32_t)m->b, y = (int)(int32_t)m->c;
        r->input_events++;
        /* A position meant for an earlier picture (the scene changed in
         * between) is dropped rather than misplaced. */
        if (m->a != r->gen || !has_picture(r->kind) || x >= r->w || y >= r->h)
            break;
        if (r->cb.pointer)
            r->cb.pointer(r->cbdata, r->kind, x, y);
        break;
    }
    case RBR_BUTTON:
        r->input_events++;
        if (r->cb.button)
            r->cb.button(r->cbdata, r->kind, m->a, m->b != 0);
        break;
    case RBR_RELEASE_ALL:
        if (r->cb.release_all)
            r->cb.release_all(r->cbdata);
        break;
    case RBR_VIEWERS:
        r->viewers = m->a;
        r->controllers = m->b;
        if (r->cb.viewers)
            r->cb.viewers(r->cbdata, m->a, m->b);
        break;
    }
}

static void client_ready(void *data, int fd, uint32_t events)
{
    struct rb_remote *r = data;
    if (r->fd != fd)
        return;
    if (events & EPOLLIN) {
        for (int iter = 0; iter < 256 && r->fd == fd; iter++) {
            uint8_t buf[64];
            union {
                char b[CMSG_SPACE(sizeof(int) * 4)];
                struct cmsghdr align;
            } cbuf;
            struct iovec iov = {.iov_base = buf, .iov_len = sizeof buf};
            struct msghdr mh = {.msg_iov = &iov, .msg_iovlen = 1, .msg_control = cbuf.b,
                                .msg_controllen = sizeof cbuf.b};
            ssize_t n = recvmsg(fd, &mh, MSG_DONTWAIT | MSG_CMSG_CLOEXEC);
            if (n < 0) {
                if (errno == EAGAIN || errno == EINTR)
                    break;
                disconnect(r, strerror(errno));
                return;
            }
            if (n == 0) {
                disconnect(r, "closed by hostd");
                return;
            }
            /* hostd never sends descriptors: close any that arrive. */
            for (struct cmsghdr *c = CMSG_FIRSTHDR(&mh); c; c = CMSG_NXTHDR(&mh, c))
                if (c->cmsg_level == SOL_SOCKET && c->cmsg_type == SCM_RIGHTS) {
                    int nfd = (int)((c->cmsg_len - CMSG_LEN(0)) / sizeof(int));
                    for (int i = 0; i < nfd && i < 4; i++) {
                        int x;
                        memcpy(&x, CMSG_DATA(c) + (size_t)i * sizeof(int), sizeof x);
                        close(x);
                    }
                }
            if (mh.msg_flags & (MSG_TRUNC | MSG_CTRUNC)) {
                rb_log("remote: oversized message dropped");
                continue;
            }
            struct rbr_msg m;
            char err[120];
            if (rbr_parse(buf, (size_t)n, &m, err, sizeof err) < 0) {
                rb_log("remote: protocol error: %s", err);
                continue;
            }
            if (!r->hello && m.type != RBR_HELLO) {
                rb_log("remote: message %u before HELLO ignored", m.type);
                continue;
            }
            handle(r, &m);
        }
    }
    if (r->fd == fd && (events & (EPOLLHUP | EPOLLERR)) && !(events & EPOLLIN))
        disconnect(r, "hangup");
}

static void accept_ready(void *data, int fd, uint32_t events)
{
    (void)events;
    struct rb_remote *r = data;
    int c = accept4(fd, NULL, NULL, SOCK_NONBLOCK | SOCK_CLOEXEC);
    if (c < 0)
        return;
    struct ucred cred = {0, 0, 0};
    socklen_t cl = sizeof cred;
    if (getsockopt(c, SOL_SOCKET, SO_PEERCRED, &cred, &cl) == 0 && cred.uid != 0 && cred.uid != geteuid()) {
        rb_log("remote: refused connection from uid %d", cred.uid);
        close(c);
        return;
    }
    if (r->fd >= 0)
        disconnect(r, "replaced by a new connection");
    r->fd = c;
    r->watch = rb_loop_watch(r->loop, c, EPOLLIN, client_ready, r);
    if (!r->watch) {
        close(c);
        r->fd = -1;
        return;
    }
    r->connections++;
    r->updates = r->rects = r->pixels = r->input_events = 0;
    rb_log("remote: hostd connected (pid %d)", cred.pid);
}

int rb_remote_listen(struct rb_remote *r, struct rb_loop *loop, const char *path, const struct rb_remote_cb *cb,
                     void *data)
{
    memset(r, 0, sizeof *r);
    r->fd = -1;
    r->listen_fd = -1;
    r->memfd = -1;
    r->loop = loop;
    r->cb = *cb;
    r->cbdata = data;
    r->wait_timer = rb_timer_new(loop, wait_timer_fn, r);
    snprintf(r->path, sizeof r->path, "%s", path);
    int s = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (s < 0)
        return -1;
    struct sockaddr_un addr = {.sun_family = AF_UNIX};
    snprintf(addr.sun_path, sizeof addr.sun_path, "%s", path);
    unlink(path);
    mode_t old = umask(0177);
    int rc = bind(s, (struct sockaddr *)&addr, sizeof addr);
    umask(old);
    if (rc < 0 || listen(s, 2) < 0) {
        rb_log("remote: cannot listen on %s: %s", path, strerror(errno));
        close(s);
        return -1;
    }
    r->listen_fd = s;
    r->listen_watch = rb_loop_watch(loop, s, EPOLLIN, accept_ready, r);
    rb_log("remote: listening on %s", path);
    return 0;
}

void rb_remote_shutdown(struct rb_remote *r)
{
    disconnect(r, "presenter exit");
    if (r->listen_fd >= 0) {
        rb_watch_remove(r->listen_watch);
        close(r->listen_fd);
        unlink(r->path);
        r->listen_fd = -1;
    }
    if (r->wait_timer) {
        rb_timer_free(r->wait_timer);
        r->wait_timer = NULL;
    }
}
