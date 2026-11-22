#include "splash.h"

#include <errno.h>
#include <math.h>
#include <libudev.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
#include <xf86drm.h>

#include "checker.h"
#include "diagview.h"
#include "drm.h"
#include "installer_splash.h"
#include "log.h"
#include "loop.h"
#include "rotation.h"
#include "scale.h"
#include "startup.h"
#include "vt.h"
#include "xform.h"

#define MAX_DEV 4
#define MAX_MIRRORS 3

/* What one device shows: its frame and the startup indicator's layout on it. */
struct splash_frame {
    uint32_t *frame;  /* the presenter's logical frame (lw x lh) */
    int lw, lh;
    int s, cs; /* pixel scale and checkerboard pattern scale */
    struct rb_startup_geom geom;
    struct rb_watch *drm_watch;
};

struct splash {
    struct rb_loop *loop;
    struct udev *udev;
    struct udev_monitor *mon;
    struct rb_output dev[MAX_DEV];
    struct splash_frame frame[MAX_DEV];
    bool used[MAX_DEV];
    /* Other connected connectors of each device mirror the checkerboard, as
     * the presenter will (same frame, scale, centering and border). */
    struct rb_output mirror[MAX_DEV][MAX_MIRRORS];
    int nmirror[MAX_DEV];
    int listen_fd;
    int client;
    struct rb_watch *client_watch;
    bool handing_off;
    bool handoff_requested; /* a presenter has asked for the display */
    /* Startup indicator (4.4.1): on when GRUB drew it (retroboot.progress=on);
     * the bar follows the learned kernel time of the cut-over. */
    struct rb_boot_startup boot;
    bool indicator;
    struct rb_icon_art art;
    struct rb_progress prog;
    int fill;          /* bar steps drawn */
    uint64_t blink_t0; /* the green light blinks from here (on first, like GRUB's) */
    bool lit;          /* light drawn on */
    struct rb_timer *timer;
    struct rb_isplash_run isplash; /* installer startup screen (installer_splash.h, 4.4.2) */
    struct rb_diag_watch diag; /* Startup Details (diagview.h): drawn instead of the scene while visible */
    struct rb_timer *diag_timer;  /* 1 s: page flips that do not complete (published for rb-bootdiag) */
    struct rb_diag_flips diag_flips;
};

static socklen_t abstract_addr(struct sockaddr_un *a)
{
    memset(a, 0, sizeof *a);
    a->sun_family = AF_UNIX;
    size_t n = strlen(RB_SPLASH_SOCKET);
    memcpy(a->sun_path + 1, RB_SPLASH_SOCKET, n);
    return (socklen_t)(offsetof(struct sockaddr_un, sun_path) + 1 + n);
}

/* --- splash side --- */

/* The configured pixel scale (GRUB passes retroboot.pixel_scale), so the
 * splash redraws exactly GRUB's checkerboard (ARCHITECTURE 4.4). The scale is
 * the one of the presenter's logical (rotated) frame; the pattern itself is
 * drawn in raw scanout coordinates, exactly like GRUB's background. */
static int splash_scale(const struct rb_output *o)
{
    return rb_effective_scale(rb_output_lw(o), rb_output_lh(o), rb_boot_pixel_scale(NULL));
}

/* Pattern scale: what GRUB drew (retroboot.checker_scale) when it is on this
 * scale's grid, else the DPI rule for this display (4.4). */
static int splash_checker(const struct splash *sp, const struct rb_output *o, int s, int *m)
{
    if (sp->boot.checker_scale > 0 && sp->boot.checker_scale % s == 0) {
        *m = sp->boot.checker_scale / s;
        return sp->boot.checker_scale;
    }
    int mw, mh, lw = rb_output_lw(o), lh = rb_output_lh(o);
    rb_display_mm(o->conn_type, lw, lh, o->mm_w, o->mm_h, &mw, &mh);
    *m = rb_checker_multiple(lw, lh, s, mw, mh);
    return s * *m;
}

static void splash_arm(struct splash *sp);

/* Startup Details over the startup scene (the whole frame), or its hint. */
static void diag_overlay(struct splash *sp, struct splash_frame *f)
{
    if (sp->diag.visible)
        rb_diag_render(f->frame, f->lw, f->lh, f->lw, f->s, sp->diag.doc);
    else if (sp->diag.hint)
        rb_diag_render_hint(f->frame, f->lw, f->lh, f->lw, f->s);
}

/* Present a slot's frame on its primary and every mirror. */
static int flush_slot(struct splash *sp, int slot)
{
    struct splash_frame *f = &sp->frame[slot];
    int rc = rb_output_flush(&sp->dev[slot], f->frame, f->lw);
    for (int i = 0; i < sp->nmirror[slot]; i++)
        rb_output_flush(&sp->mirror[slot][i], f->frame, f->lw);
    return rc;
}

/* The presenter's logical frame (the primary's rotated size): the startup
 * scene (rb_startup_scene: the checkerboard at the pattern scale anchored at
 * Mac OS's picture, the startup indicator) exactly as GRUB's background has
 * it before its output rotation (rb-bootdisplay) and as the presenter
 * composes it, and the mirrors show exactly what the presenter will show
 * them, so neither handoff changes a pixel. The frame stays: bar steps and
 * blinks redraw only their rectangles. */
static int draw_device(struct splash *sp, int slot)
{
    struct rb_output *o = &sp->dev[slot];
    struct splash_frame *f = &sp->frame[slot];
    int m;
    f->lw = rb_output_lw(o);
    f->lh = rb_output_lh(o);
    f->s = splash_scale(o);
    f->cs = splash_checker(sp, o, f->s, &m);
    free(f->frame);
    f->frame = malloc((size_t)f->lw * f->lh * 4);
    if (!f->frame)
        return -1;
    rb_startup_geometry(&f->geom, f->lw, f->lh, f->s, rb_boot_pixel_scale(NULL), sp->boot.guest_w,
                        sp->boot.guest_h);
    rb_startup_scene_style(f->frame, f->lw, f->lw, f->lh, (rb_rect){0, 0, f->lw, f->lh}, f->cs, &f->geom, &sp->art,
                           sp->indicator, sp->fill, sp->lit, sp->boot.style);
    rb_isplash_run_scene(&sp->isplash, f->frame, f->lw, f->lw, f->lh, (rb_rect){0, 0, f->lw, f->lh}, f->s,
                         rb_boot_pixel_scale(NULL), sp->boot.guest_w, sp->boot.guest_h);
    diag_overlay(sp, f);
    rb_output_set_frame(o, f->lw, f->lh, false);
    o->need_modeset = true;
    int rc = rb_output_flush(o, f->frame, f->lw);
    for (int i = 0; i < sp->nmirror[slot]; i++) {
        struct rb_output *mo = &sp->mirror[slot][i];
        rb_xform_set_src_scale(&mo->xf, f->s); /* a 2x panel's frame: exact 1/2 on a 1080p mirror */
        rb_output_set_frame(mo, f->lw, f->lh, true);
        rb_output_fit_mirror(mo, f->lw, f->lh); /* 1x or more, as the presenter will */
        rb_output_set_border(mo, sp->boot.style == RB_STARTUP_OSX ? RB_BORDER_OSX_GRAY : RB_BORDER_CHECKER,
                             f->cs, rb_startup_anchor(&f->geom));
        mo->need_modeset = true;
        int mrc = rb_output_flush(mo, f->frame, f->lw);
        rb_log("splash: mirror %s %dx%d@%d (%s) %dx scale; present %s", mo->conn_name, mo->w, mo->h, mo->refresh,
               mo->mode_source ? mo->mode_source : "?", mo->xf.k, mrc > 0 ? "ok" : strerror(-mrc));
        /* A DSI panel the driver enabled without it coming up (GPD MicroPC after a
         * cold start with HDMI attached: the VBT needs GPIOs ACPI doesn't describe)
         * never scans out, and every commit on its pipe waits out i915's 10 s
         * flip timeout. Removing our framebuffers from it at exit made three such
         * commits, which held the presenter's DRM calls (and hostd's start of Mac
         * OS) for 30 s. Give the panel up now without another commit: CLOSEFB
         * leaves the scanout as it is, as the presenter does (dsi_stalled). */
        if (mo->conn_type == DRM_MODE_CONNECTOR_DSI && rb_output_scanout_stalled(mo) == 1) {
            rb_log("splash: mirror %s is not scanning out (the driver could not power it on); given up",
                   mo->conn_name);
            rb_output_abandon(mo);
            sp->mirror[slot][i] = sp->mirror[slot][sp->nmirror[slot] - 1];
            sp->nmirror[slot]--;
            i--;
        }
    }
    return rc;
}

/* Bar step or blink: only the bar's and the light's rectangles change. */
static void redraw_part(struct splash *sp, int slot, rb_rect r)
{
    struct splash_frame *f = &sp->frame[slot];
    if (!f->frame || !sp->indicator || !f->geom.valid || !rb_rect_intersect(r, (rb_rect){0, 0, f->lw, f->lh}, &r))
        return;
    if (sp->diag.visible) /* the view covers the indicator; it is redrawn when the view closes */
        return;
    rb_startup_draw(f->frame, f->lw, f->lw, f->lh, r, &f->geom, &sp->art, sp->fill, sp->lit);
    rb_output_damage(&sp->dev[slot], r);
    for (int i = 0; i < sp->nmirror[slot]; i++)
        rb_output_damage(&sp->mirror[slot][i], r);
}

/* Installer startup screen: its status line or bar changed (installer_splash.h). */
static void isplash_redraw(void *data, int what)
{
    struct splash *sp = data;
    for (int i = 0; i < MAX_DEV && !sp->handing_off; i++) {
        struct splash_frame *f = &sp->frame[i];
        if (!sp->used[i] || !f->frame)
            continue;
        rb_rect r = rb_isplash_run_dirty(&sp->isplash, what, f->lw, f->lh, f->s, rb_boot_pixel_scale(NULL),
                                         sp->boot.guest_w, sp->boot.guest_h);
        if (!rb_rect_intersect(r, (rb_rect){0, 0, f->lw, f->lh}, &r))
            continue;
        rb_isplash_run_scene(&sp->isplash, f->frame, f->lw, f->lw, f->lh, r, f->s, rb_boot_pixel_scale(NULL),
                             sp->boot.guest_w, sp->boot.guest_h);
        rb_output_damage(&sp->dev[i], r);
        for (int m = 0; m < sp->nmirror[i]; m++)
            rb_output_damage(&sp->mirror[i][m], r);
        flush_slot(sp, i);
    }
}

static void splash_tick(void *data)
{
    struct splash *sp = data;
    if (sp->handing_off || !sp->indicator)
        return;
    uint64_t now = rb_boot_ms();
    int fill = rb_startup_fill(rb_progress_at(&sp->prog, now));
    bool lit = rb_startup_lit(sp->blink_t0, now);
    bool bar = fill > sp->fill, light = lit != sp->lit;
    if (bar)
        sp->fill = fill;
    if (light)
        sp->lit = lit;
    for (int i = 0; i < MAX_DEV && (bar || light); i++) {
        if (!sp->used[i])
            continue;
        if (bar)
            redraw_part(sp, i, rb_startup_bar_rect(&sp->frame[i].geom));
        if (light)
            redraw_part(sp, i, rb_startup_light_rect(&sp->frame[i].geom, &sp->art));
        if (sp->frame[i].frame)
            flush_slot(sp, i);
    }
    splash_arm(sp);
}

static void splash_arm(struct splash *sp)
{
    if (!sp->indicator || sp->handing_off) {
        rb_timer_arm(sp->timer, 0);
        return;
    }
    uint64_t now = rb_boot_ms(), next = now + 5000;
    if (sp->prog.timed && sp->fill < RB_BAR_STEPS) {
        uint64_t n = rb_progress_next_step(&sp->prog, now, RB_BAR_STEPS, 5000);
        next = n < next ? n : next;
    }
    if (sp->art.nlight) {
        uint64_t n = sp->blink_t0 + ((now - sp->blink_t0) / RB_BLINK_MS + 1) * RB_BLINK_MS;
        next = n < next ? n : next;
    }
    rb_timer_arm(sp->timer, next > now + 20 ? next - now : 20);
}

/* Page-flip completion (primary and mirrors share the card's fd); an update
 * that found a flip pending goes out now. */
static void drm_event(void *data, int fd, uint32_t events)
{
    (void)events;
    struct splash *sp = data;
    for (int i = 0; i < MAX_DEV; i++) {
        if (!sp->used[i] || sp->dev[i].fd != fd)
            continue;
        rb_output_dispatch(&sp->dev[i]);
        if (sp->handing_off || !sp->frame[i].frame)
            continue;
        bool due = rb_output_needs_flush(&sp->dev[i]);
        for (int j = 0; j < sp->nmirror[i]; j++)
            due = due || rb_output_needs_flush(&sp->mirror[i][j]);
        if (due)
            flush_slot(sp, i);
    }
}

static void open_mirrors(struct splash *sp, int slot)
{
    struct rb_output *o = &sp->dev[slot];
    uint32_t ids[16];
    int n = rb_output_connected(o, ids, 16);
    for (int j = 0; j < n && sp->nmirror[slot] < MAX_MIRRORS; j++) {
        if (ids[j] == o->conn_id)
            continue;
        uint32_t busy[MAX_MIRRORS + 1];
        int nb = 0;
        busy[nb++] = o->crtc_id;
        for (int i = 0; i < sp->nmirror[slot]; i++)
            busy[nb++] = sp->mirror[slot][i].crtc_id;
        char err[200];
        struct rb_output *mo = &sp->mirror[slot][sp->nmirror[slot]];
        if (rb_output_open_head(mo, o, ids[j], busy, nb, err, sizeof err) < 0) {
            rb_log("splash: mirror: %s", err);
            continue;
        }
        /* Checked before the first commit, as the presenter does (dsi_stalled):
         * the check after the commit (draw_device) came after i915's 10 s wait,
         * and the commit stayed pending for systemd-backlight, the presenter
         * and the shutdown (image/reports/hostboot2-2026-10-12.md). */
        if (mo->conn_type == DRM_MODE_CONNECTOR_DSI &&
            rb_output_never_commit(mo->conn_type, mo->fw_lit || rb_driver_no_readout(mo->driver),
                                   mo->fw_lit ? rb_output_scanout_stalled(mo) : 0)) {
            rb_log("splash: mirror %s %s; not used", mo->conn_name,
                   mo->fw_lit ? "is not scanning out (the driver could not power it on)"
                              : "was left off by the firmware (the driver can't power it on)");
            rb_output_abandon(mo);
            continue;
        }
        sp->nmirror[slot]++;
    }
}

static void close_slot(struct splash *sp, int slot)
{
    if (sp->frame[slot].drm_watch)
        rb_watch_remove(sp->frame[slot].drm_watch);
    sp->frame[slot].drm_watch = NULL;
    for (int i = 0; i < sp->nmirror[slot]; i++)
        rb_output_close(&sp->mirror[slot][i]); /* shared file: closed with the primary */
    sp->nmirror[slot] = 0;
    rb_output_close(&sp->dev[slot]);
    free(sp->frame[slot].frame);
    sp->frame[slot].frame = NULL;
    sp->used[slot] = false;
}

static bool have_node(struct splash *sp, const char *node)
{
    for (int i = 0; i < MAX_DEV; i++)
        if (sp->used[i] && !strcmp(sp->dev[i].devnode, node))
            return true;
    return false;
}

bool rb_splash_may_take_device(bool handing_off, bool handoff_requested)
{
    return !handing_off && !handoff_requested;
}

bool rb_splash_keep_after_abort(int devices_held)
{
    return devices_held > 0;
}

static void add_device(struct splash *sp, const char *node)
{
    if (!rb_splash_may_take_device(sp->handing_off, sp->handoff_requested) || have_node(sp, node)) {
        if (sp->handoff_requested && !sp->handing_off && !have_node(sp, node))
            rb_log("splash: %s appeared after the presenter asked for the display; leaving it to the presenter", node);
        return;
    }
    int slot = -1;
    for (int i = 0; i < MAX_DEV; i++)
        if (!sp->used[i]) {
            slot = i;
            break;
        }
    if (slot < 0)
        return;
    char err[200];
    struct rb_output *o = &sp->dev[slot];
    if (rb_output_open(o, node, NULL, NULL, err, sizeof err) < 0) {
        rb_log("splash: %s", err);
        return;
    }
    uint32_t ids[16];
    bool lit = o->fw_lit || rb_driver_no_readout(o->driver); /* vc4: every CRTC starts off */
    if (o->conn_type == DRM_MODE_CONNECTOR_DSI && rb_output_connected(o, ids, 16) > 1 &&
        rb_output_never_commit(o->conn_type, lit, o->fw_lit ? rb_output_scanout_stalled(o) : 0)) {
        /* Another connector can show the frame (the presenter does the same). */
        rb_log("splash: %s is dark (left off by the firmware or not scanning out); using another display",
               o->conn_name);
        rb_output_abandon(o);
        if (rb_output_open(o, node, "external", NULL, err, sizeof err) < 0) {
            rb_log("splash: %s", err);
            return;
        }
    }
    sp->used[slot] = true;
    sp->nmirror[slot] = 0;
    open_mirrors(sp, slot);
    int rc = draw_device(sp, slot);
    sp->frame[slot].drm_watch = rb_loop_watch(sp->loop, o->fd, EPOLLIN, drm_event, sp);
    struct splash_frame *f = &sp->frame[slot];
    rb_log("splash: %s (%s) %s %dx%d@%d rot %d scale %d checker %d at %d,%d mode from %s%s; indicator %s %s at "
           "%d,%d x%d fill %d; present %s",
           node, o->driver, o->conn_name, o->w, o->h, o->refresh, o->rot, f->s, f->cs, f->geom.gx, f->geom.gy,
           o->mode_source ? o->mode_source : "?", o->fb[0].offset ? " (tall first buffer)" : "",
           sp->indicator && f->geom.valid ? "on" : "off", rb_icon_source_name(sp->art.source), f->geom.x, f->geom.y, f->geom.q,
           sp->fill, rc > 0 ? "ok" : strerror(-rc));
}

static void remove_device(struct splash *sp, const char *node)
{
    for (int i = 0; i < MAX_DEV; i++)
        if (sp->used[i] && !strcmp(sp->dev[i].devnode, node)) {
            rb_log("splash: %s removed", node);
            close_slot(sp, i);
        }
}

static void scan(struct splash *sp)
{
    char cards[RB_MAX_CARDS][64];
    int n = rb_drm_list_cards(cards, RB_MAX_CARDS);
    for (int i = 0; i < n; i++)
        add_device(sp, cards[i]);
}

static void udev_ready(void *data, int fd, uint32_t events)
{
    (void)fd;
    (void)events;
    struct splash *sp = data;
    struct udev_device *d;
    while ((d = udev_monitor_receive_device(sp->mon))) {
        const char *action = udev_device_get_action(d);
        const char *sysname = udev_device_get_sysname(d);
        const char *node = udev_device_get_devnode(d);
        if (action && sysname && !strncmp(sysname, "card", 4) && !strchr(sysname, '-')) {
            if (!strcmp(action, "remove") && node)
                remove_device(sp, node);
            else if (!strcmp(action, "add") || !strcmp(action, "change"))
                scan(sp);
        }
        udev_device_unref(d);
    }
}

static void reacquire(struct splash *sp)
{
    sp->handing_off = false;
    rb_isplash_run_pause(&sp->isplash, false);
    int held = 0;
    for (int i = 0; i < MAX_DEV; i++)
        held += sp->used[i];
    if (!rb_splash_keep_after_abort(held)) {
        /* Nothing on screen to keep: the presenter takes the display when a
         * driver binds; staying would race it for DRM master. */
        rb_log("splash: no display to take back; exiting");
        rb_loop_quit(sp->loop, 0);
        return;
    }
    for (int i = 0; i < MAX_DEV; i++) {
        if (!sp->used[i])
            continue;
        if (rb_output_set_master(&sp->dev[i]) < 0) {
            rb_log("splash: cannot reacquire master on %s: %s", sp->dev[i].devnode, strerror(errno));
            continue;
        }
        draw_device(sp, i);
        rb_log("splash: reacquired %s", sp->dev[i].devnode);
    }
    if (!sp->handoff_requested)
        scan(sp);
    splash_arm(sp);
}

static void reply(struct splash *sp, const char *msg) { send(sp->client, msg, strlen(msg), MSG_NOSIGNAL); }

static void drop_client(struct splash *sp)
{
    if (sp->client < 0)
        return;
    rb_watch_remove(sp->client_watch);
    close(sp->client);
    sp->client = -1;
    if (sp->handing_off) {
        rb_log("splash: presenter went away during handoff; taking the display back");
        reacquire(sp);
    }
}

static void client_ready(void *data, int fd, uint32_t events)
{
    struct splash *sp = data;
    char buf[512];
    ssize_t n = recv(fd, buf, sizeof buf - 1, MSG_DONTWAIT);
    if (n <= 0) {
        if (n < 0 && (errno == EAGAIN || errno == EINTR) && !(events & (EPOLLHUP | EPOLLERR)))
            return;
        drop_client(sp);
        return;
    }
    buf[n] = 0;
    if (!strcmp(buf, "handoff")) {
        int primary = -1;
        for (int i = 0; i < MAX_DEV; i++) {
            if (!sp->used[i])
                continue;
            if (primary < 0)
                primary = i;
            if (rb_output_drop_master(&sp->dev[i]) < 0)
                rb_log("splash: drop master on %s: %s", sp->dev[i].devnode, strerror(errno));
        }
        sp->handing_off = true;
        sp->handoff_requested = true;
        rb_isplash_run_pause(&sp->isplash, true); /* the presenter continues it */
        rb_timer_arm(sp->timer, 0); /* the bar stays as drawn: the presenter continues it */
        char msg[200];
        if (primary < 0) {
            snprintf(msg, sizeof msg, "ready none");
        } else {
            struct rb_output *o = &sp->dev[primary];
            snprintf(msg, sizeof msg, "ready %s %s %d %d %d", o->devnode, o->conn_name, o->w, o->h, o->refresh);
        }
        rb_log("splash: handoff requested; %s", msg);
        reply(sp, msg);
    } else if (!strcmp(buf, "done")) {
        rb_log("splash: handoff done; exiting");
        sp->handing_off = false;
        reply(sp, "bye");
        rb_loop_quit(sp->loop, 0);
    } else if (!strcmp(buf, "abort")) {
        reply(sp, "ok");
        reacquire(sp);
    } else if (!strncmp(buf, "chroot ", 7)) {
        const char *dir = buf + 7;
        if (chroot(dir) == 0 && chdir("/") == 0) {
            rb_log("splash: now rooted at %s", dir);
            reply(sp, "ok");
        } else {
            rb_log("splash: chroot %s: %s", dir, strerror(errno));
            reply(sp, "error");
        }
    } else if (!strcmp(buf, "startup")) {
        /* The indicator as drawn, so the presenter's first frame is this one
         * and its bar continues from here. */
        char msg[200];
        snprintf(msg, sizeof msg, "startup %s %d %llu %ld %llu %d %s %llu %d %d", sp->indicator ? "on" : "off",
                 sp->prog.timed ? 1 : 0, (unsigned long long)sp->prog.t0, (long)lround(sp->prog.p0 * 1e6),
                 (unsigned long long)sp->prog.eta, sp->fill, rb_icon_source_name(sp->art.source),
                 (unsigned long long)sp->blink_t0, sp->lit ? 1 : 0, sp->boot.style);
        reply(sp, msg);
    } else if (!strcmp(buf, "isplash")) {
        char msg[256];
        if (rb_isplash_state_format(&sp->isplash, msg, sizeof msg) < 0)
            snprintf(msg, sizeof msg, "error");
        reply(sp, msg);
    } else if (!strcmp(buf, "icon")) {
        static uint8_t msg[5 + sizeof sp->art.px];
        memcpy(msg, "icon ", 5);
        memcpy(msg + 5, sp->art.px, sizeof sp->art.px);
        send(sp->client, msg, sizeof msg, MSG_NOSIGNAL);
    } else if (!strcmp(buf, "status")) {
        char msg[256];
        int n_dev = 0;
        for (int i = 0; i < MAX_DEV; i++)
            n_dev += sp->used[i];
        snprintf(msg, sizeof msg, "splash devices=%d handing_off=%d", n_dev, sp->handing_off);
        reply(sp, msg);
    } else {
        reply(sp, "error unknown command");
    }
}

static void accept_ready(void *data, int fd, uint32_t events)
{
    (void)events;
    struct splash *sp = data;
    int c = accept4(fd, NULL, NULL, SOCK_NONBLOCK | SOCK_CLOEXEC);
    if (c < 0)
        return;
    if (sp->client >= 0) {
        if (sp->handing_off) {
            close(c); /* never disturb an in-progress handoff */
            return;
        }
        drop_client(sp);
    }
    sp->client = c;
    sp->client_watch = rb_loop_watch(sp->loop, c, EPOLLIN, client_ready, sp);
}

/* Startup Details changed (shown, hidden, new content, hint): redraw every
 * frame the splash shows, unless it is handing the display over. */
static void diag_ready(void *data, int fd, uint32_t events)
{
    (void)fd;
    (void)events;
    struct splash *sp = data;
    int chg = rb_diag_watch_update(&sp->diag);
    bool redraw = (chg & (RB_DIAG_CHG_VISIBLE | RB_DIAG_CHG_HINT)) || ((chg & RB_DIAG_CHG_DOC) && sp->diag.visible);
    if (!redraw || sp->handing_off)
        return;
    if (chg & RB_DIAG_CHG_VISIBLE)
        rb_log("splash: startup details %s", sp->diag.visible ? "shown" : "hidden");
    for (int i = 0; i < MAX_DEV; i++) {
        struct splash_frame *f = &sp->frame[i];
        if (!sp->used[i] || !f->frame)
            continue;
        rb_startup_scene_style(f->frame, f->lw, f->lw, f->lh, (rb_rect){0, 0, f->lw, f->lh}, f->cs, &f->geom, &sp->art,
                               sp->indicator, sp->fill, sp->lit, sp->boot.style);
        diag_overlay(sp, f);
        rb_output_damage(&sp->dev[i], (rb_rect){0, 0, f->lw, f->lh});
        for (int j = 0; j < sp->nmirror[i]; j++)
            rb_output_damage(&sp->mirror[i][j], (rb_rect){0, 0, f->lw, f->lh});
        flush_slot(sp, i);
    }
}

/* Page flips still pending after 1 s (rb_output_check_stall), as the presenter counts them. */
static void diag_flip_check(void *data)
{
    struct splash *sp = data;
    uint64_t now = rb_mono_ms();
    struct rb_diag_flips f;
    memset(&f, 0, sizeof f);
    for (int i = 0; i < MAX_DEV; i++) {
        if (!sp->used[i])
            continue;
        for (int j = -1; j < sp->nmirror[i]; j++) {
            struct rb_output *o = j < 0 ? &sp->dev[i] : &sp->mirror[i][j];
            if (rb_output_check_stall(o, now, 1000))
                rb_log("splash: %s: page flip pending for %llu ms", o->conn_name,
                       (unsigned long long)(now - o->pending_mono_ms));
            f.stalls += o->flip_stalls;
            f.slow_commits += o->slow_commits;
            if (o->slow_commit_max_ms > f.slow_max_ms)
                f.slow_max_ms = o->slow_commit_max_ms;
            if (o->stall_first_mono_ms && (!f.first_mono_ms || o->stall_first_mono_ms < f.first_mono_ms))
                f.first_mono_ms = o->stall_first_mono_ms;
            if (o->stall_last_mono_ms > f.last_mono_ms)
                f.last_mono_ms = o->stall_last_mono_ms;
            if ((o->flip_stalls || o->slow_commits) && !f.connector[0]) {
                snprintf(f.connector, sizeof f.connector, "%s", o->conn_name);
                snprintf(f.driver, sizeof f.driver, "%s", o->driver);
            }
        }
    }
    if ((f.stalls || f.slow_commits) && memcmp(&f, &sp->diag_flips, sizeof f)) {
        rb_diag_publish_flips("splash", &f);
        sp->diag_flips = f;
    }
    rb_timer_arm(sp->diag_timer, 1000);
}

static void on_signal(int sig) { (void)sig; }

int rb_splash_main(int argc, char **argv)
{
    (void)argc;
    /* argv[0][0] == '@': systemd's switch-root/shutdown killing spree skips
     * this process (see systemd ROOT_STORAGE_DAEMONS). */
    if (argv && argv[0] && argv[0][0])
        argv[0][0] = '@';
    signal(SIGPIPE, SIG_IGN);
    signal(SIGHUP, on_signal);
    rb_log_init("rb-splash", 1);
    rb_log("splash: start");
    rb_vt_take();
    struct splash sp;
    memset(&sp, 0, sizeof sp);
    sp.client = -1;
    sp.loop = rb_loop_new();
    sp.udev = udev_new();
    if (!sp.loop || !sp.udev)
        return 1;
    /* Startup indicator: only when GRUB drew it, with the art GRUB used (the
     * Platinum-derived icon arrives as an extra initrd next to GRUB's PNG). */
    rb_boot_startup(NULL, &sp.boot);
    sp.indicator = sp.boot.progress == 1 && sp.boot.style != RB_STARTUP_OSX;
    rb_user_rotation = rb_boot_rotation(NULL); /* the user rotation GRUB's background was drawn for */
    rb_icon_art_load(&sp.art, RB_STARTUP_ICON_INITRD);
    if (rb_diag_watch_init(&sp.diag) >= 0) {
        rb_loop_watch(sp.loop, sp.diag.fd, EPOLLIN, diag_ready, &sp);
        rb_diag_watch_update(&sp.diag);
        sp.diag_timer = rb_timer_new(sp.loop, diag_flip_check, &sp);
        rb_timer_arm(sp.diag_timer, 1000);
    }
    uint64_t now = rb_boot_ms();
    /* Learned cut-over time; already past it: approach the cap slowly. */
    rb_progress_reset(&sp.prog, now, sp.boot.startup_ms ? (sp.boot.startup_ms > now ? sp.boot.startup_ms : now) : 0);
    sp.blink_t0 = now;
    sp.lit = true;
    sp.timer = rb_timer_new(sp.loop, splash_tick, &sp);
    /* Installer: Mac OS's startup box (private art from GRUB's extra initrd) */
    rb_isplash_run_start(&sp.isplash, sp.loop, RB_ISPLASH_INITRD_ART, now, isplash_redraw, &sp);
    rb_log("splash: indicator %s (%s art), cut-over expected at %llu ms (now %llu), rotation %d",
           sp.indicator ? "on" : "off", rb_icon_source_name(sp.art.source), (unsigned long long)sp.boot.startup_ms,
           (unsigned long long)now, rb_user_rotation);
    /* Kernel uevents: available before udevd runs (the splash starts ahead
     * of udev in the initramfs); devtmpfs provides the device node. */
    sp.mon = udev_monitor_new_from_netlink(sp.udev, "kernel");
    if (sp.mon) {
        udev_monitor_filter_add_match_subsystem_devtype(sp.mon, "drm", NULL);
        udev_monitor_enable_receiving(sp.mon);
        rb_loop_watch(sp.loop, udev_monitor_get_fd(sp.mon), EPOLLIN, udev_ready, &sp);
    }
    scan(&sp);
    sp.listen_fd = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    struct sockaddr_un addr;
    socklen_t alen = abstract_addr(&addr);
    if (sp.listen_fd < 0 || bind(sp.listen_fd, (struct sockaddr *)&addr, alen) < 0 || listen(sp.listen_fd, 4) < 0) {
        rb_log("splash: cannot listen on @%s: %s (another splash running?)", RB_SPLASH_SOCKET, strerror(errno));
        return 1;
    }
    rb_loop_watch(sp.loop, sp.listen_fd, EPOLLIN, accept_ready, &sp);
    splash_arm(&sp);
    while (rb_loop_running(sp.loop))
        rb_loop_dispatch(sp.loop, -1);
    for (int i = 0; i < MAX_DEV; i++)
        if (sp.used[i])
            close_slot(&sp, i);
    return rb_loop_exit_code(sp.loop);
}

/* --- client side --- */

static int connect_splash(int timeout_ms)
{
    int fd = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0);
    if (fd < 0)
        return -1;
    struct sockaddr_un addr;
    socklen_t alen = abstract_addr(&addr);
    if (connect(fd, (struct sockaddr *)&addr, alen) < 0) {
        close(fd);
        return -1;
    }
    struct timeval tv = {.tv_sec = timeout_ms / 1000, .tv_usec = (timeout_ms % 1000) * 1000};
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    return fd;
}

int rb_splash_send_chroot(const char *dir)
{
    int fd = connect_splash(2000);
    if (fd < 0)
        return 1; /* no splash: nothing to do */
    char msg[300];
    snprintf(msg, sizeof msg, "chroot %s", dir);
    send(fd, msg, strlen(msg), MSG_NOSIGNAL);
    char buf[64] = "";
    ssize_t n = recv(fd, buf, sizeof buf - 1, 0);
    close(fd);
    return n > 0 && !strncmp(buf, "ok", 2) ? 0 : 2;
}

int rb_splash_handoff_begin(struct rb_splash_handoff *h, int timeout_ms)
{
    memset(h, 0, sizeof *h);
    h->fd = -1;
    h->start_mono_ms = rb_mono_ms();
    int fd = connect_splash(timeout_ms);
    if (fd < 0)
        return -1;
    if (send(fd, "handoff", 7, MSG_NOSIGNAL) != 7) {
        close(fd);
        return -1;
    }
    char buf[256];
    ssize_t n = recv(fd, buf, sizeof buf - 1, 0);
    if (n <= 0) {
        rb_log("handoff: splash did not answer (%s)", n < 0 ? strerror(errno) : "closed");
        close(fd);
        return -1;
    }
    buf[n] = 0;
    h->ready_mono_ms = rb_mono_ms();
    h->fd = fd;
    h->active = true;
    if (sscanf(buf, "ready %63s %31s %d %d %d", h->devnode, h->connector, &h->w, &h->h, &h->refresh) != 5) {
        h->devnode[0] = 0;
        h->connector[0] = 0;
    }
    rb_log("handoff: splash released the display (%s) after %llu ms", buf,
           (unsigned long long)(h->ready_mono_ms - h->start_mono_ms));
    return 0;
}

bool rb_splash_handoff_complete(const struct rb_splash_handoff *h, bool presenter_on_screen)
{
    return presenter_on_screen || !h->devnode[0] || !strcmp(h->devnode, "none");
}

void rb_splash_handoff_finish(struct rb_splash_handoff *h, bool ok)
{
    if (!h->active)
        return;
    const char *msg = ok ? "done" : "abort";
    send(h->fd, msg, strlen(msg), MSG_NOSIGNAL);
    char buf[32];
    struct timeval tv = {.tv_sec = 1, .tv_usec = 0};
    setsockopt(h->fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    if (recv(h->fd, buf, sizeof buf, 0) < 0)
        rb_log("handoff: no acknowledgement from splash");
    close(h->fd);
    h->fd = -1;
    h->active = false;
    rb_log("handoff: %s", ok ? "complete; splash exiting" : "aborted; splash keeps the display");
}

int rb_splash_handoff_startup(struct rb_splash_handoff *h, struct rb_splash_startup *out)
{
    memset(out, 0, sizeof *out);
    if (!h->active || h->fd < 0)
        return -1;
    if (send(h->fd, "startup", 7, MSG_NOSIGNAL) != 7)
        return -1;
    char buf[256];
    ssize_t n = recv(h->fd, buf, sizeof buf - 1, 0);
    if (n <= 0)
        return -1;
    buf[n] = 0;
    char on[8] = "", src[16] = "";
    int timed = 0, fill = 0, lit = 1, style = RB_STARTUP_CLASSIC;
    unsigned long long t0 = 0, eta = 0, blink_t0 = 0;
    long p0 = 0;
    if (sscanf(buf, "startup %7s %d %llu %ld %llu %d %15s %llu %d %d", on, &timed, &t0, &p0, &eta, &fill, src, &blink_t0,
               &lit, &style) < 9) {
        rb_log("handoff: splash has no startup indicator state (%.40s)", buf);
        return -1;
    }
    out->known = true;
    out->indicator = !strcmp(on, "on");
    out->style = style == RB_STARTUP_OSX ? RB_STARTUP_OSX : RB_STARTUP_CLASSIC;
    out->timed = timed != 0;
    out->t0 = t0;
    out->eta = eta;
    out->p0 = p0 / 1e6;
    out->fill = fill;
    out->source = !strcmp(src, "macos") ? 1 : 0;
    out->blink_t0 = blink_t0;
    out->lit = lit != 0;
    if (out->indicator && send(h->fd, "icon", 4, MSG_NOSIGNAL) == 4) {
        static uint8_t ibuf[5 + sizeof out->art];
        ssize_t m = recv(h->fd, ibuf, sizeof ibuf, 0);
        if (m == (ssize_t)sizeof ibuf && !memcmp(ibuf, "icon ", 5)) {
            memcpy(out->art, ibuf + 5, sizeof out->art);
            out->have_art = true;
        }
    }
    return 0;
}
