/* Remote screen socket (ARCHITECTURE.md section 4.6): the frame tap and the
 * remote input path behind the web client's Screen page.
 *
 * hostd connects to /run/retroboot/remote.sock (AF_UNIX SOCK_SEQPACKET, root
 * 0600, one client; a new connection replaces the old) only while a paired
 * browser watches. The presenter keeps a private copy of the picture being
 * streamed (the "tap buffer", a sealed memfd shared read-only with hostd) and
 * changes it only immediately before answering hostd's REQUEST, so hostd reads
 * a stable frame between an UPDATE and its next REQUEST. Copies are diffed:
 * UPDATE carries the tight rectangles of pixels that really changed, not the
 * guest's full-width damage strips. Nothing is copied while nobody watches. */
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "loop.h"
#include "region.h"

#define RBR_VERSION 1

enum {
    RBR_HELLO = 1,       /* both ways. hostd: u32 version, u32 source. presenter: u32 version */
    RBR_REQUEST = 2,     /* hostd: u32 seq, u32 wait_ms */
    RBR_SOURCE = 3,      /* hostd: u32 source */
    RBR_SURFACE = 4,     /* presenter: see struct below (+ memfd unless kind none) */
    RBR_UPDATE = 5,      /* presenter: u32 seq, u32 gen, u32 count, {i32 x, y, w, h}[count] */
    RBR_KEY = 16,        /* hostd: u32 evdev code, u32 down */
    RBR_POINTER = 17,    /* hostd: u32 gen, i32 x, i32 y (tap surface pixels) */
    RBR_BUTTON = 18,     /* hostd: u32 button (0 left, 1 middle, 2 right, 3 wheel up, 4 wheel down), u32 down */
    RBR_RELEASE_ALL = 20, /* hostd: release every key/button held remotely */
    RBR_VIEWERS = 21,    /* hostd: u32 viewers, u32 controllers (local indicator) */
};

enum rb_remote_source { RB_REMOTE_SRC_COMPOSED = 0, RB_REMOTE_SRC_GUEST = 1 };

/* What the tap buffer holds. */
enum rb_remote_kind {
    RB_REMOTE_NONE = 0,    /* no picture (guest stopped, held last frame): hostd keeps the last one */
    RB_REMOTE_GUEST = 1,   /* the Mac OS framebuffer at its own size */
    RB_REMOTE_FRAME = 2,   /* the presenter's logical frame (panel), W/s x H/s */
    RB_REMOTE_CHECKER = 3, /* the checkerboard, logical size (generated) */
    RB_REMOTE_BLACK = 4,   /* black, logical size (generated) */
};

#define RBR_SURFACE_LEN 48 /* u32 gen, kind, width, height, stride (bytes), format; u64 size; char view[16] */
#define RBR_MAX_RECTS 64
#define RBR_WAIT_MAX_MS 5000

/* Description of the picture the presenter would stream now. */
struct rb_remote_src {
    enum rb_remote_kind kind;
    const uint32_t *px; /* GUEST/FRAME: source pixels; CHECKER: the presenter's frame (NULL: generated) */
    int w, h, stride_px;
    int step;           /* source pixels per tap pixel (the frame at pixel scale s); 0 or 1: 1 */
    const char *view;   /* "guest", "switching", "hold", "panel", "checkerboard", "black", "none", "stopped" */
    int scale;          /* FRAME/CHECKER/BLACK: logical -> frame pixels (pointer mapping) */
    rb_rect hide;       /* tap pixels refilled with the checkerboard: the local indicator's box */
    int hide_cm;        /* checkerboard pattern pixel for the refill, in tap pixels */
};

struct rb_remote_cb {
    /* Fill *s with the current picture for `source`. */
    void (*source)(void *data, int source, struct rb_remote_src *s);
    void (*connected)(void *data);
    void (*disconnected)(void *data);
    void (*key)(void *data, uint32_t evdev, bool down);
    /* x, y in the given kind's pixels (already checked against the tap generation). */
    void (*pointer)(void *data, enum rb_remote_kind kind, int x, int y);
    void (*button)(void *data, enum rb_remote_kind kind, uint32_t button, bool down);
    void (*release_all)(void *data);
    void (*viewers)(void *data, uint32_t viewers, uint32_t controllers);
};

struct rb_remote {
    struct rb_loop *loop;
    struct rb_remote_cb cb;
    void *cbdata;
    char path[108];
    int listen_fd;
    struct rb_watch *listen_watch;
    int fd;
    struct rb_watch *watch;
    bool hello;
    int source;
    /* tap buffer */
    int memfd;
    uint32_t *buf;
    size_t buf_len;
    int w, h;
    enum rb_remote_kind kind;
    char view[16];
    const uint32_t *src_px; /* identity of the source the buffer was filled from */
    int src_stride, src_step;
    rb_rect hide;           /* never streamed: refilled with the checkerboard */
    int hide_cm;
    uint32_t gen;
    rb_region pending;      /* source damage not yet diffed into the buffer */
    bool full;              /* resend everything (new surface) */
    /* hostd's REQUEST, held until there is a change or wait_ms passed */
    bool held;
    uint32_t held_seq;
    struct rb_timer *wait_timer;
    /* stats */
    uint64_t updates, rects, pixels, connections, input_events;
    uint32_t viewers, controllers;
};

int rb_remote_listen(struct rb_remote *r, struct rb_loop *loop, const char *path, const struct rb_remote_cb *cb,
                     void *data);
void rb_remote_shutdown(struct rb_remote *r);
bool rb_remote_connected(const struct rb_remote *r);
/* Source damage: GUEST rects in guest pixels, FRAME rects in logical pixels.
 * Cheap no-op while hostd is not connected. */
void rb_remote_damage(struct rb_remote *r, enum rb_remote_kind kind, const rb_rect *rects, int n);
/* Called once per loop iteration after composition: picks up source changes
 * and answers a held REQUEST when something changed. */
void rb_remote_flush(struct rb_remote *r);

/* --- pure helpers (unit tested) --- */

/* The guest's picture for the tap: Mac OS's framebuffer, or -- while the
 * presenter holds its last good frame through a mode switch (switchhold.h: the
 * new surface still holds the old picture at the new stride, a clear or a
 * desktop in mid-redraw) -- no picture, view "switching": hostd keeps the last
 * picture it streamed, as the local display keeps its last frame, and the new
 * surface goes out whole once the presenter shows it. */
void rb_remote_guest_source(struct rb_remote_src *s, const uint32_t *px, int w, int h, int stride_px,
                            bool holding);

struct rbr_msg {
    uint32_t type;
    uint32_t a, b, c;
};
/* Parse one hostd -> presenter message. Returns 0 or -1 with err set. */
int rbr_parse(const uint8_t *buf, size_t n, struct rbr_msg *m, char *err, size_t errlen);
/* Copy rect r of src into dst (both w x h at their strides, in pixels) and
 * store the tight rectangles of pixels that changed (at most max; extra
 * changes merge). Returns the number of rectangles. */
int rbr_diff_copy(uint32_t *dst, int dst_stride, const uint32_t *src, int src_stride, rb_rect r, rb_region *out);
/* The same from a source sampled every `step` pixels (dst pixel (x, y) = src (x*step, y*step)). */
int rbr_diff_copy_step(uint32_t *dst, int dst_stride, const uint32_t *src, int src_stride, int step, rb_rect r,
                       rb_region *out);
/* Encode presenter -> hostd messages; return length. */
size_t rbr_encode_surface(uint8_t out[8 + RBR_SURFACE_LEN], uint32_t gen, uint32_t kind, uint32_t w, uint32_t h,
                          uint32_t stride, uint64_t size, const char *view);
size_t rbr_encode_update(uint8_t *out, size_t cap, uint32_t seq, uint32_t gen, const rb_rect *rects, int n);
