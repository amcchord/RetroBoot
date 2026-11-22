/* Display socket server (ARCHITECTURE.md section 3): SOCK_SEQPACKET, one QEMU
 * client, shared-memory XRGB8888 surface via memfd, damage rectangles in,
 * input messages out. */
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "loop.h"
#include "region.h"

enum {
    RBD_HELLO = 1,
    RBD_SURFACE = 2,
    RBD_DAMAGE = 3,
    RBD_GUEST_EVENT = 4,
    RBD_KEY = 16,
    RBD_POINTER_ABS = 17,
    RBD_BUTTON = 18,
    RBD_RESIZE = 19,
    RBD_RELEASE_ALL = 20,
    RBD_REFRESH_RATE = 21,
    RBD_POINTER_REL = 22,
};

#define RBD_VERSION 1
#define RBD_MAX_RECTS 1024
#define RBD_MAX_DIM 8192
#define RBD_HELLO_LEN 48
#define RBD_SURFACE_LEN 24

struct rbd_msg {
    uint32_t type;
    union {
        struct {
            uint32_t version, pid;
            char session[41];
        } hello;
        struct {
            uint32_t width, height, stride, format;
            uint64_t size;
        } surface;
        struct {
            uint32_t seq, count;
            const uint8_t *rects; /* count * 4 x int32, unaligned */
        } damage;
        struct {
            const char *json;
            size_t len;
        } event;
    } u;
};

/* Parse one SEQPACKET message. Returns 0 on success, -1 with err set. */
int rbd_parse(const uint8_t *buf, size_t n, struct rbd_msg *m, char *err, size_t errlen);
/* Validate a SURFACE description. */
int rbd_check_surface(const struct rbd_msg *m, char *err, size_t errlen);
/* Damage rect i, clipped to (w, h). */
rb_rect rbd_damage_rect(const struct rbd_msg *m, uint32_t i, int w, int h);
/* Encode an outgoing message; returns its length (<= 16). */
size_t rbd_encode(uint8_t out[16], uint32_t type, uint32_t a, uint32_t b, int nargs);

struct rb_guest_cb {
    void (*connected)(void *data);
    void (*disconnected)(void *data, const char *reason);
    void (*surface)(void *data);
    void (*damage)(void *data, const rb_rect *rects, int n);
    void (*event)(void *data, const char *json, size_t len);
};

#define RB_GUEST_QUEUE 512

struct rb_guest {
    struct rb_loop *loop;
    struct rb_guest_cb cb;
    void *cbdata;
    char path[108];
    int listen_fd;
    struct rb_watch *listen_watch;
    int fd;
    struct rb_watch *watch;
    bool hello;
    uint32_t version, pid;
    char session[41];
    uint64_t connect_mono_ms;
    /* current surface */
    int width, height, stride_px;
    uint64_t size;
    const uint32_t *px;
    void *map;
    size_t map_len;
    bool sealed;
    /* outgoing queue of fixed 16-byte slots */
    uint8_t q[RB_GUEST_QUEUE][16];
    uint8_t qlen_of[RB_GUEST_QUEUE];
    int qhead, qcount;
    bool want_out;
    /* stats */
    uint64_t damage_msgs, surfaces, dropped_out, connections;
    uint32_t last_seq;
};

int rb_guest_listen(struct rb_guest *g, struct rb_loop *loop, const char *path, const char *group,
                    const struct rb_guest_cb *cb, void *cbdata);
void rb_guest_shutdown(struct rb_guest *g);
bool rb_guest_connected(const struct rb_guest *g);
void rb_guest_disconnect(struct rb_guest *g, const char *reason);

void rb_guest_send_key(struct rb_guest *g, uint32_t qcode, bool down);
void rb_guest_send_pointer(struct rb_guest *g, uint32_t x, uint32_t y);
void rb_guest_send_relative(struct rb_guest *g, int32_t dx, int32_t dy);
void rb_guest_send_button(struct rb_guest *g, uint32_t button, bool down);
void rb_guest_send_resize(struct rb_guest *g, uint32_t w, uint32_t h);
void rb_guest_send_release_all(struct rb_guest *g);
void rb_guest_send_refresh(struct rb_guest *g, uint32_t interval_ms);
