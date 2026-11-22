#include "guest.h"

#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>
#include <drm_fourcc.h>

#include "log.h"

static uint32_t rd32(const uint8_t *p)
{
    uint32_t v;
    memcpy(&v, p, 4);
    return v;
}

static uint64_t rd64(const uint8_t *p)
{
    uint64_t v;
    memcpy(&v, p, 8);
    return v;
}

int rbd_parse(const uint8_t *buf, size_t n, struct rbd_msg *m, char *err, size_t errlen)
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
    switch (m->type) {
    case RBD_HELLO:
        if (len != RBD_HELLO_LEN) {
            snprintf(err, errlen, "HELLO length %u", len);
            return -1;
        }
        m->u.hello.version = rd32(p);
        m->u.hello.pid = rd32(p + 4);
        memcpy(m->u.hello.session, p + 8, 40);
        m->u.hello.session[40] = 0;
        return 0;
    case RBD_SURFACE:
        if (len != RBD_SURFACE_LEN) {
            snprintf(err, errlen, "SURFACE length %u", len);
            return -1;
        }
        m->u.surface.width = rd32(p);
        m->u.surface.height = rd32(p + 4);
        m->u.surface.stride = rd32(p + 8);
        m->u.surface.format = rd32(p + 12);
        m->u.surface.size = rd64(p + 16);
        return 0;
    case RBD_DAMAGE:
        if (len < 8) {
            snprintf(err, errlen, "DAMAGE length %u", len);
            return -1;
        }
        m->u.damage.seq = rd32(p);
        m->u.damage.count = rd32(p + 4);
        if (m->u.damage.count > RBD_MAX_RECTS || (size_t)len != 8 + (size_t)m->u.damage.count * 16) {
            snprintf(err, errlen, "DAMAGE count %u does not match length %u", m->u.damage.count, len);
            return -1;
        }
        m->u.damage.rects = p + 8;
        return 0;
    case RBD_GUEST_EVENT:
        m->u.event.json = (const char *)p;
        m->u.event.len = len;
        return 0;
    default:
        snprintf(err, errlen, "unknown message type %u", m->type);
        return -1;
    }
}

int rbd_check_surface(const struct rbd_msg *m, char *err, size_t errlen)
{
    uint32_t w = m->u.surface.width, h = m->u.surface.height, stride = m->u.surface.stride;
    if (w == 0 || h == 0 || w > RBD_MAX_DIM || h > RBD_MAX_DIM) {
        snprintf(err, errlen, "surface size %ux%u out of range", w, h);
        return -1;
    }
    if (m->u.surface.format != DRM_FORMAT_XRGB8888) {
        snprintf(err, errlen, "surface format 0x%08x is not XRGB8888", m->u.surface.format);
        return -1;
    }
    if (stride < w * 4 || stride % 4) {
        snprintf(err, errlen, "surface stride %u invalid for width %u", stride, w);
        return -1;
    }
    if (m->u.surface.size < (uint64_t)stride * h) {
        snprintf(err, errlen, "surface size %llu < stride*height", (unsigned long long)m->u.surface.size);
        return -1;
    }
    return 0;
}

rb_rect rbd_damage_rect(const struct rbd_msg *m, uint32_t i, int w, int h)
{
    const uint8_t *p = m->u.damage.rects + (size_t)i * 16;
    int32_t v[4];
    memcpy(v, p, 16);
    rb_rect r = {v[0], v[1], v[2], v[3]};
    /* Guard against overflow before clipping. */
    if (r.w < 0 || r.h < 0 || r.x > RBD_MAX_DIM || r.y > RBD_MAX_DIM || r.w > 2 * RBD_MAX_DIM ||
        r.h > 2 * RBD_MAX_DIM || r.x < -RBD_MAX_DIM || r.y < -RBD_MAX_DIM)
        return (rb_rect){0, 0, 0, 0};
    return rb_rect_clip(r, w, h);
}

size_t rbd_encode(uint8_t out[16], uint32_t type, uint32_t a, uint32_t b, int nargs)
{
    uint32_t len = (uint32_t)nargs * 4;
    memcpy(out, &type, 4);
    memcpy(out + 4, &len, 4);
    if (nargs > 0)
        memcpy(out + 8, &a, 4);
    if (nargs > 1)
        memcpy(out + 12, &b, 4);
    return 8 + len;
}

/* --- connection handling --- */

static void unmap_surface(struct rb_guest *g)
{
    if (g->map)
        munmap(g->map, g->map_len);
    g->map = NULL;
    g->px = NULL;
    g->map_len = 0;
    g->width = g->height = g->stride_px = 0;
    g->size = 0;
}

bool rb_guest_connected(const struct rb_guest *g) { return g->fd >= 0; }

void rb_guest_disconnect(struct rb_guest *g, const char *reason)
{
    if (g->fd < 0)
        return;
    rb_watch_remove(g->watch);
    g->watch = NULL;
    close(g->fd);
    g->fd = -1;
    unmap_surface(g);
    g->hello = false;
    g->qcount = g->qhead = 0;
    g->want_out = false;
    rb_log("guest: disconnected (%s) after %llu damage messages", reason,
           (unsigned long long)g->damage_msgs);
    if (g->cb.disconnected)
        g->cb.disconnected(g->cbdata, reason);
}

static void close_fds(struct msghdr *mh)
{
    for (struct cmsghdr *c = CMSG_FIRSTHDR(mh); c; c = CMSG_NXTHDR(mh, c)) {
        if (c->cmsg_level != SOL_SOCKET || c->cmsg_type != SCM_RIGHTS)
            continue;
        int nfd = (int)((c->cmsg_len - CMSG_LEN(0)) / sizeof(int));
        int fds[8];
        if (nfd > 8)
            nfd = 8;
        memcpy(fds, CMSG_DATA(c), (size_t)nfd * sizeof(int));
        for (int i = 0; i < nfd; i++)
            if (fds[i] >= 0)
                close(fds[i]);
    }
}

static int take_fd(struct msghdr *mh)
{
    int found = -1;
    for (struct cmsghdr *c = CMSG_FIRSTHDR(mh); c; c = CMSG_NXTHDR(mh, c)) {
        if (c->cmsg_level != SOL_SOCKET || c->cmsg_type != SCM_RIGHTS)
            continue;
        int nfd = (int)((c->cmsg_len - CMSG_LEN(0)) / sizeof(int));
        int fds[8];
        if (nfd > 8)
            nfd = 8;
        memcpy(fds, CMSG_DATA(c), (size_t)nfd * sizeof(int));
        for (int i = 0; i < nfd; i++) {
            if (found < 0)
                found = fds[i];
            else
                close(fds[i]);
        }
    }
    return found;
}

static void handle_surface(struct rb_guest *g, const struct rbd_msg *m, int memfd)
{
    char err[160];
    if (memfd < 0) {
        rb_log("guest: SURFACE without memfd; ignored");
        return;
    }
    if (rbd_check_surface(m, err, sizeof err) < 0) {
        rb_log("guest: rejected SURFACE: %s", err);
        close(memfd);
        return;
    }
    struct stat st;
    if (fstat(memfd, &st) < 0 || (uint64_t)st.st_size < m->u.surface.size) {
        rb_log("guest: SURFACE memfd smaller than declared size");
        close(memfd);
        return;
    }
    /* Prevent the producer from shrinking the buffer under our mapping
     * (SIGBUS). Works when the memfd allows sealing. */
    bool sealed = false;
    int seals = fcntl(memfd, F_GET_SEALS);
    if (seals >= 0 && (seals & F_SEAL_SHRINK))
        sealed = true;
    else if (seals >= 0 && !(seals & F_SEAL_SEAL) && fcntl(memfd, F_ADD_SEALS, F_SEAL_SHRINK) == 0)
        sealed = true;
    size_t len = (size_t)m->u.surface.size;
    void *map = mmap(NULL, len, PROT_READ, MAP_SHARED, memfd, 0);
    close(memfd);
    if (map == MAP_FAILED) {
        rb_log("guest: SURFACE mmap failed: %s", strerror(errno));
        return;
    }
    unmap_surface(g);
    g->map = map;
    g->map_len = len;
    g->px = map;
    g->width = (int)m->u.surface.width;
    g->height = (int)m->u.surface.height;
    g->stride_px = (int)(m->u.surface.stride / 4);
    g->size = m->u.surface.size;
    g->sealed = sealed;
    g->surfaces++;
    rb_log("guest: surface %dx%d stride %d size %llu%s", g->width, g->height, g->stride_px * 4,
           (unsigned long long)g->size, sealed ? " (shrink-sealed)" : " (UNSEALED)");
    if (g->cb.surface)
        g->cb.surface(g->cbdata);
}

static void flush_queue(struct rb_guest *g);

static void guest_ready(void *data, int fd, uint32_t events)
{
    struct rb_guest *g = data;
    if (events & EPOLLOUT)
        flush_queue(g);
    if (g->fd != fd)
        return;
    if (events & EPOLLIN) {
        static uint8_t buf[65536 + 16];
        for (int iter = 0; iter < 256 && g->fd == fd; iter++) {
            union {
                char b[CMSG_SPACE(sizeof(int) * 8)];
                struct cmsghdr align;
            } cbuf;
            struct iovec iov = {.iov_base = buf, .iov_len = sizeof buf};
            struct msghdr mh = {.msg_iov = &iov, .msg_iovlen = 1, .msg_control = cbuf.b,
                                .msg_controllen = sizeof cbuf.b};
            ssize_t n = recvmsg(fd, &mh, MSG_DONTWAIT | MSG_CMSG_CLOEXEC);
            if (n < 0) {
                if (errno == EAGAIN || errno == EINTR)
                    break;
                rb_guest_disconnect(g, strerror(errno));
                return;
            }
            if (n == 0) {
                close_fds(&mh);
                rb_guest_disconnect(g, "closed by peer");
                return;
            }
            if (mh.msg_flags & (MSG_TRUNC | MSG_CTRUNC)) {
                close_fds(&mh);
                rb_log("guest: truncated message dropped");
                continue;
            }
            struct rbd_msg m;
            char err[160];
            if (rbd_parse(buf, (size_t)n, &m, err, sizeof err) < 0) {
                close_fds(&mh);
                rb_log("guest: protocol error: %s", err);
                continue;
            }
            switch (m.type) {
            case RBD_HELLO:
                close_fds(&mh);
                g->hello = true;
                g->version = m.u.hello.version;
                g->pid = m.u.hello.pid;
                memcpy(g->session, m.u.hello.session, sizeof g->session);
                rb_log("guest: HELLO version %u pid %u session '%s'", g->version, g->pid, g->session);
                if (g->version != RBD_VERSION)
                    rb_log("guest: protocol version %u differs from %u; continuing", g->version, RBD_VERSION);
                if (g->cb.connected)
                    g->cb.connected(g->cbdata);
                break;
            case RBD_SURFACE:
                handle_surface(g, &m, take_fd(&mh));
                break;
            case RBD_DAMAGE: {
                close_fds(&mh);
                g->damage_msgs++;
                g->last_seq = m.u.damage.seq;
                if (!g->px)
                    break;
                rb_rect rects[64];
                int nr = 0;
                rb_rect bound = {0, 0, 0, 0};
                for (uint32_t i = 0; i < m.u.damage.count; i++) {
                    rb_rect r = rbd_damage_rect(&m, i, g->width, g->height);
                    if (rb_rect_empty(r))
                        continue;
                    if (nr < 64)
                        rects[nr++] = r;
                    bound = rb_rect_union(bound, r);
                }
                if (m.u.damage.count > 64) {
                    rects[0] = bound;
                    nr = rb_rect_empty(bound) ? 0 : 1;
                }
                if (g->cb.damage)
                    g->cb.damage(g->cbdata, rects, nr);
                break;
            }
            case RBD_GUEST_EVENT:
                close_fds(&mh);
                if (g->cb.event)
                    g->cb.event(g->cbdata, m.u.event.json, m.u.event.len);
                break;
            }
        }
    }
    if (g->fd == fd && (events & (EPOLLHUP | EPOLLERR)) && !(events & EPOLLIN))
        rb_guest_disconnect(g, "hangup");
}

static void accept_ready(void *data, int fd, uint32_t events)
{
    (void)events;
    struct rb_guest *g = data;
    int c = accept4(fd, NULL, NULL, SOCK_NONBLOCK | SOCK_CLOEXEC);
    if (c < 0)
        return;
    if (g->fd >= 0)
        rb_guest_disconnect(g, "replaced by new connection");
    struct ucred cred;
    socklen_t cl = sizeof cred;
    if (getsockopt(c, SOL_SOCKET, SO_PEERCRED, &cred, &cl) == 0)
        rb_log("guest: connection from pid %d uid %d", cred.pid, cred.uid);
    g->fd = c;
    g->watch = rb_loop_watch(g->loop, c, EPOLLIN, guest_ready, g);
    if (!g->watch) {
        close(c);
        g->fd = -1;
        return;
    }
    g->connect_mono_ms = rb_mono_ms();
    g->hello = false;
    g->damage_msgs = 0;
    g->connections++;
    g->qcount = g->qhead = 0;
    g->want_out = false;
}

int rb_guest_listen(struct rb_guest *g, struct rb_loop *loop, const char *path, const char *group,
                    const struct rb_guest_cb *cb, void *cbdata)
{
    memset(g, 0, sizeof *g);
    g->fd = -1;
    g->listen_fd = -1;
    g->loop = loop;
    g->cb = *cb;
    g->cbdata = cbdata;
    snprintf(g->path, sizeof g->path, "%s", path);
    int s = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (s < 0)
        return -1;
    struct sockaddr_un addr = {.sun_family = AF_UNIX};
    snprintf(addr.sun_path, sizeof addr.sun_path, "%s", path);
    unlink(path);
    mode_t old = umask(0177);
    int rc = bind(s, (struct sockaddr *)&addr, sizeof addr);
    umask(old);
    if (rc < 0 || listen(s, 4) < 0) {
        rb_log("guest: cannot listen on %s: %s", path, strerror(errno));
        close(s);
        return -1;
    }
    struct group *gr = group && *group ? getgrnam(group) : NULL;
    if (gr) {
        if (chown(path, 0, gr->gr_gid) == 0)
            chmod(path, 0660);
    }
    g->listen_fd = s;
    g->listen_watch = rb_loop_watch(loop, s, EPOLLIN, accept_ready, g);
    rb_log("guest: listening on %s%s%s", path, gr ? " group " : "", gr ? group : "");
    return 0;
}

void rb_guest_shutdown(struct rb_guest *g)
{
    rb_guest_disconnect(g, "presenter exit");
    if (g->listen_fd >= 0) {
        rb_watch_remove(g->listen_watch);
        close(g->listen_fd);
        unlink(g->path);
        g->listen_fd = -1;
    }
}

/* --- outgoing --- */

static void set_want_out(struct rb_guest *g, bool on)
{
    if (g->want_out == on || !g->watch)
        return;
    g->want_out = on;
    rb_watch_set_events(g->watch, EPOLLIN | (on ? EPOLLOUT : 0));
}

static int try_send(struct rb_guest *g, const uint8_t *msg, size_t len)
{
    ssize_t n = send(g->fd, msg, len, MSG_DONTWAIT | MSG_NOSIGNAL);
    if (n == (ssize_t)len)
        return 1;
    if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == ENOBUFS))
        return 0;
    return -1;
}

static void flush_queue(struct rb_guest *g)
{
    while (g->fd >= 0 && g->qcount > 0) {
        int rc = try_send(g, g->q[g->qhead], g->qlen_of[g->qhead]);
        if (rc == 0) {
            set_want_out(g, true);
            return;
        }
        if (rc < 0) {
            rb_guest_disconnect(g, "send failed");
            return;
        }
        g->qhead = (g->qhead + 1) % RB_GUEST_QUEUE;
        g->qcount--;
    }
    set_want_out(g, false);
}

static void enqueue(struct rb_guest *g, uint32_t type, uint32_t a, uint32_t b, int nargs)
{
    if (getenv("RB_INPUT_TRACE") && (type == RBD_KEY || type == RBD_RELEASE_ALL))
        rb_log("RBINPUT enqueue us=%llu type=%u q=%u down=%u fd=%d queued=%d",
               (unsigned long long)rb_mono_us(), type, a, b, g->fd, g->qcount);
    if (g->fd < 0)
        return;
    uint8_t msg[16];
    size_t len = rbd_encode(msg, type, a, b, nargs);
    if (g->qcount == 0) {
        int rc = try_send(g, msg, len);
        if (rc > 0)
            return;
        if (rc < 0) {
            rb_guest_disconnect(g, "send failed");
            return;
        }
    }
    /* Coalesce consecutive pointer positions while blocked. */
    if ((type == RBD_POINTER_ABS || type == RBD_POINTER_REL) && g->qcount > 0) {
        int last = (g->qhead + g->qcount - 1) % RB_GUEST_QUEUE;
        uint32_t lt;
        memcpy(&lt, g->q[last], 4);
        if (lt == type && type == RBD_POINTER_ABS) {
            memcpy(g->q[last], msg, len);
            return;
        }
        if (lt == type && type == RBD_POINTER_REL) {
            /* Motion adds; keeping only the newest delta loses turns. */
            int32_t previous[2], next[2];
            memcpy(previous, g->q[last] + 8, sizeof previous);
            memcpy(next, msg + 8, sizeof next);
            for (int i = 0; i < 2; i++) {
                int64_t sum = (int64_t)previous[i] + next[i];
                next[i] = sum > 32767 ? 32767 : (sum < -32767 ? -32767 : (int32_t)sum);
            }
            memcpy(g->q[last] + 8, next, sizeof next);
            return;
        }
    }
    if (g->qcount >= RB_GUEST_QUEUE) {
        g->dropped_out++;
        if (type == RBD_POINTER_ABS)
            return;
        rb_guest_disconnect(g, "input queue overflow");
        return;
    }
    int slot = (g->qhead + g->qcount) % RB_GUEST_QUEUE;
    memcpy(g->q[slot], msg, len);
    g->qlen_of[slot] = (uint8_t)len;
    g->qcount++;
    set_want_out(g, true);
}

void rb_guest_send_key(struct rb_guest *g, uint32_t qcode, bool down) { enqueue(g, RBD_KEY, qcode, down, 2); }
void rb_guest_send_pointer(struct rb_guest *g, uint32_t x, uint32_t y) { enqueue(g, RBD_POINTER_ABS, x, y, 2); }
void rb_guest_send_relative(struct rb_guest *g, int32_t dx, int32_t dy) { enqueue(g, RBD_POINTER_REL, (uint32_t)dx, (uint32_t)dy, 2); }
void rb_guest_send_button(struct rb_guest *g, uint32_t b, bool down) { enqueue(g, RBD_BUTTON, b, down, 2); }
void rb_guest_send_resize(struct rb_guest *g, uint32_t w, uint32_t h) { enqueue(g, RBD_RESIZE, w, h, 2); }
void rb_guest_send_release_all(struct rb_guest *g) { enqueue(g, RBD_RELEASE_ALL, 0, 0, 0); }
void rb_guest_send_refresh(struct rb_guest *g, uint32_t ms) { enqueue(g, RBD_REFRESH_RATE, ms, 0, 1); }
