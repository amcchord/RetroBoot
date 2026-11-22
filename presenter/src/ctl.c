#include "ctl.h"

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#include "log.h"

bool rb_ctl_connected(const struct rb_ctl *c) { return c->fd >= 0; }

void rb_ctl_disconnect(struct rb_ctl *c)
{
    if (c->fd < 0)
        return;
    rb_watch_remove(c->watch);
    c->watch = NULL;
    close(c->fd);
    c->fd = -1;
    c->inlen = 0;
    c->outlen = c->outoff = 0;
    c->want_out = false;
    rb_log("ctl: hostd disconnected");
    if (c->cb.disconnected)
        c->cb.disconnected(c->cbdata);
}

size_t rb_ctl_split_lines(char *buf, size_t len, void (*fn)(void *ctx, char *line, size_t n), void *ctx)
{
    size_t start = 0;
    for (size_t i = 0; i < len; i++) {
        if (buf[i] != '\n')
            continue;
        size_t n = i - start;
        char *line = buf + start;
        if (n && line[n - 1] == '\r')
            n--;
        line[n] = 0;
        fn(ctx, line, n);
        start = i + 1;
    }
    return start;
}

struct line_ctx {
    struct rb_ctl *c;
    int fd;
};

static void handle_line(void *ctx, char *line, size_t n)
{
    struct line_ctx *lc = ctx;
    struct rb_ctl *c = lc->c;
    if (c->fd != lc->fd)
        return; /* disconnected or replaced during this batch */
    size_t i = 0;
    while (i < n && (line[i] == ' ' || line[i] == '\t'))
        i++;
    if (i == n)
        return;
    cJSON *req = cJSON_ParseWithLength(line, n);
    if (!req || !cJSON_IsObject(req)) {
        rb_ctl_send_line(c, "{\"id\":null,\"ok\":false,\"error\":\"invalid JSON request\"}");
        cJSON_Delete(req);
        return;
    }
    if (c->cb.request)
        c->cb.request(c->cbdata, req);
    cJSON_Delete(req);
}

static void flush_out(struct rb_ctl *c);

static void client_ready(void *data, int fd, uint32_t events)
{
    struct rb_ctl *c = data;
    if (events & EPOLLOUT)
        flush_out(c);
    if (c->fd != fd)
        return;
    if (events & EPOLLIN) {
        for (;;) {
            if (c->incap - c->inlen < 65536) {
                size_t ncap = c->incap ? c->incap * 2 : 131072;
                if (ncap > RB_CTL_MAX_LINE + 65536) {
                    rb_log("ctl: request line too long; dropping client");
                    rb_ctl_disconnect(c);
                    return;
                }
                char *n = realloc(c->in, ncap);
                if (!n) {
                    rb_ctl_disconnect(c);
                    return;
                }
                c->in = n;
                c->incap = ncap;
            }
            ssize_t r = recv(fd, c->in + c->inlen, c->incap - c->inlen - 1, MSG_DONTWAIT);
            if (r < 0) {
                if (errno == EAGAIN || errno == EINTR)
                    break;
                rb_ctl_disconnect(c);
                return;
            }
            if (r == 0) {
                rb_ctl_disconnect(c);
                return;
            }
            c->inlen += (size_t)r;
            struct line_ctx lc = {c, fd};
            size_t used = rb_ctl_split_lines(c->in, c->inlen, handle_line, &lc);
            if (c->fd != fd)
                return;
            memmove(c->in, c->in + used, c->inlen - used);
            c->inlen -= used;
        }
    }
    if (c->fd == fd && (events & (EPOLLHUP | EPOLLERR)) && !(events & EPOLLIN))
        rb_ctl_disconnect(c);
}

static void accept_ready(void *data, int fd, uint32_t events)
{
    (void)events;
    struct rb_ctl *c = data;
    int s = accept4(fd, NULL, NULL, SOCK_NONBLOCK | SOCK_CLOEXEC);
    if (s < 0)
        return;
    if (c->fd >= 0) {
        rb_log("ctl: new hostd connection replaces the old one");
        rb_ctl_disconnect(c);
    }
    struct ucred cred;
    socklen_t cl = sizeof cred;
    if (getsockopt(s, SOL_SOCKET, SO_PEERCRED, &cred, &cl) == 0)
        rb_log("ctl: hostd connected (pid %d uid %d)", cred.pid, cred.uid);
    c->fd = s;
    c->watch = rb_loop_watch(c->loop, s, EPOLLIN, client_ready, c);
    if (!c->watch) {
        close(s);
        c->fd = -1;
        return;
    }
    c->connections++;
    if (c->cb.connected)
        c->cb.connected(c->cbdata);
}

int rb_ctl_listen(struct rb_ctl *c, struct rb_loop *loop, const char *path, const struct rb_ctl_cb *cb, void *data)
{
    memset(c, 0, sizeof *c);
    c->fd = -1;
    c->listen_fd = -1;
    c->loop = loop;
    c->cb = *cb;
    c->cbdata = data;
    snprintf(c->path, sizeof c->path, "%s", path);
    int s = socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (s < 0)
        return -1;
    struct sockaddr_un addr = {.sun_family = AF_UNIX};
    snprintf(addr.sun_path, sizeof addr.sun_path, "%s", path);
    unlink(path);
    mode_t old = umask(0177);
    int rc = bind(s, (struct sockaddr *)&addr, sizeof addr);
    umask(old);
    if (rc < 0 || listen(s, 4) < 0) {
        rb_log("ctl: cannot listen on %s: %s", path, strerror(errno));
        close(s);
        return -1;
    }
    c->listen_fd = s;
    c->listen_watch = rb_loop_watch(loop, s, EPOLLIN, accept_ready, c);
    rb_log("ctl: listening on %s", path);
    return 0;
}

void rb_ctl_shutdown(struct rb_ctl *c)
{
    rb_ctl_disconnect(c);
    if (c->listen_fd >= 0) {
        rb_watch_remove(c->listen_watch);
        close(c->listen_fd);
        unlink(c->path);
        c->listen_fd = -1;
    }
    free(c->in);
    free(c->out);
    c->in = c->out = NULL;
    c->incap = c->outcap = 0;
}

static void set_want_out(struct rb_ctl *c, bool on)
{
    if (c->want_out == on || !c->watch)
        return;
    c->want_out = on;
    rb_watch_set_events(c->watch, EPOLLIN | (on ? EPOLLOUT : 0));
}

static void flush_out(struct rb_ctl *c)
{
    while (c->fd >= 0 && c->outoff < c->outlen) {
        ssize_t n = send(c->fd, c->out + c->outoff, c->outlen - c->outoff, MSG_DONTWAIT | MSG_NOSIGNAL);
        if (n < 0) {
            if (errno == EAGAIN || errno == EINTR) {
                set_want_out(c, true);
                return;
            }
            rb_ctl_disconnect(c);
            return;
        }
        c->outoff += (size_t)n;
    }
    c->outoff = c->outlen = 0;
    set_want_out(c, false);
}

void rb_ctl_send_line(struct rb_ctl *c, const char *line)
{
    if (c->fd < 0 || !line)
        return;
    size_t n = strlen(line);
    if (c->outoff && c->outoff == c->outlen)
        c->outoff = c->outlen = 0;
    if (c->outlen + n + 1 > c->outcap) {
        if (c->outoff) {
            memmove(c->out, c->out + c->outoff, c->outlen - c->outoff);
            c->outlen -= c->outoff;
            c->outoff = 0;
        }
        size_t need = c->outlen + n + 1;
        if (need > RB_CTL_MAX_OUT) {
            rb_log("ctl: hostd is not reading (output backlog); dropping client");
            rb_ctl_disconnect(c);
            return;
        }
        size_t ncap = c->outcap ? c->outcap : 65536;
        while (ncap < need)
            ncap *= 2;
        char *nb = realloc(c->out, ncap);
        if (!nb) {
            rb_ctl_disconnect(c);
            return;
        }
        c->out = nb;
        c->outcap = ncap;
    }
    memcpy(c->out + c->outlen, line, n);
    c->out[c->outlen + n] = '\n';
    c->outlen += n + 1;
    flush_out(c);
}

void rb_ctl_send_json(struct rb_ctl *c, const cJSON *obj)
{
    if (c->fd < 0)
        return;
    char *s = cJSON_PrintUnformatted(obj);
    if (s) {
        rb_ctl_send_line(c, s);
        cJSON_free(s);
    }
}
