/* Control socket server (ARCHITECTURE.md section 4): SOCK_STREAM,
 * newline-delimited JSON, one hostd client (a new connection replaces the
 * old one). */
#pragma once
#include <stdbool.h>
#include <stddef.h>

#include "cJSON.h"
#include "loop.h"

#define RB_CTL_MAX_LINE (4u << 20)
#define RB_CTL_MAX_OUT (16u << 20)

struct rb_ctl_cb {
    void (*connected)(void *data);
    void (*disconnected)(void *data);
    /* Called for each parsed request object; the callee owns nothing. */
    void (*request)(void *data, const cJSON *req);
};

struct rb_ctl {
    struct rb_loop *loop;
    struct rb_ctl_cb cb;
    void *cbdata;
    char path[108];
    int listen_fd;
    struct rb_watch *listen_watch;
    int fd;
    struct rb_watch *watch;
    char *in;
    size_t inlen, incap;
    char *out;
    size_t outlen, outoff, outcap;
    bool want_out;
    unsigned long connections;
};

int rb_ctl_listen(struct rb_ctl *c, struct rb_loop *loop, const char *path, const struct rb_ctl_cb *cb, void *data);
void rb_ctl_shutdown(struct rb_ctl *c);
bool rb_ctl_connected(const struct rb_ctl *c);
/* Queue one JSON line (a newline is appended). */
void rb_ctl_send_line(struct rb_ctl *c, const char *line);
void rb_ctl_send_json(struct rb_ctl *c, const cJSON *obj);
void rb_ctl_disconnect(struct rb_ctl *c);

/* Split complete lines out of a buffer: calls fn for each line (without the
 * newline; NUL-terminated in place) and returns bytes consumed. */
size_t rb_ctl_split_lines(char *buf, size_t len, void (*fn)(void *ctx, char *line, size_t n), void *ctx);
