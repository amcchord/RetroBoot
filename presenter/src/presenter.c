#include "presenter.h"

#include <errno.h>
#include <fcntl.h>
#include <libudev.h>
#include <linux/input-event-codes.h>
#include <math.h>
#include <signal.h>
#include <spawn.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/pidfd.h>
#include <sys/reboot.h>
#include <sys/signalfd.h>
#include <stddef.h>
#include <sys/socket.h>
#include <poll.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>

#include "cJSON.h"
#include "checker.h"
#include "content.h"
#include "ctl.h"
#include "diagview.h"
#include "drm.h"
#include "framelog.h"
#include "guest.h"
#include "input.h"
#include "installer_splash.h"
#include "keymap.h"
#include "log.h"
#include "loop.h"
#include "png.h"
#include "proto.h"
#include "pointer.h"
#include "sample.h"
#include "rbui.h"
#include "region.h"
#include "remote.h"
#include "rotation.h"
#include "badge.h"
#include "osd.h"
#include "scale.h"
#include "splash.h"
#include "startup.h"
#include "switchhold.h"
#include "touch.h"
#include "vt.h"
#include "xform.h"

#ifndef RB_VERSION
#define RB_VERSION "dev"
#endif

#define FALLBACK_DOC_ID "hostd-lost"
#define HOSTD_GRACE_MS 10000
#define CONTENT_POLL_MS 100
#define TIMELINE_N 64
#define MAX_SHOTS 4
#define KEYBITS (KEY_CNT / 8 + 1)
#define MAX_HEADS 4

extern char **environ;

static const char fallback_doc[] =
    "{\"id\":\"" FALLBACK_DOC_ID "\",\"kind\":\"alert\",\"alert_icon\":\"stop\",\"width\":360,\"reset\":true,"
    "\"items\":[{\"type\":\"text\",\"text\":\"RetroBoot needs to restart.\",\"style\":\"emphasis\"},"
    "{\"type\":\"buttons\",\"items\":[{\"id\":\"shutdown\",\"label\":\"Shut Down\"},"
    "{\"id\":\"restart\",\"label\":\"Restart\",\"default\":true}]}]}";

/* VIEW_HOLD keeps the last composed guest frame on screen after the guest
 * disconnected (Mac OS shut down or restarted) until hostd chooses the next
 * scene, so no checkerboard flashes between Mac OS's last frame and black. */
enum view { VIEW_NONE = 0, VIEW_CHECKER, VIEW_BLACK, VIEW_GUEST, VIEW_PANEL, VIEW_HOLD };

static const char *view_name(enum view v)
{
    switch (v) {
    case VIEW_CHECKER: return "checkerboard";
    case VIEW_BLACK: return "black";
    case VIEW_GUEST: return "guest";
    case VIEW_PANEL: return "panel";
    case VIEW_HOLD: return "hold";
    default: return "none";
    }
}

struct tl_entry {
    uint64_t mono_ms, boot_ms;
    char what[24];
    char detail[112];
};

struct shot {
    bool used;
    pid_t pid;
    int pidfd;
    struct rb_watch *watch;
    cJSON *id;
    char path[512];
    int w, h;
    char what[16];
    struct presenter *p;
};

struct presenter {
    const struct rb_opts *opts;
    struct rb_loop *loop;
    bool quit;
    /* display: `out` is the primary output (its logical size is the frame);
     * heads[] are the other connected connectors of the same card, which
     * mirror the frame (each at its own mode, integer-scaled and centered). */
    struct rb_output out;
    bool have_out;
    struct rb_output heads[MAX_HEADS];
    int nheads;
    bool prefer_external; /* display_config "primary": "external" */
    /* Connectors given up on (a panel that never started scanning out):
     * not driven again until the presenter restarts. */
    uint32_t failed_conn[MAX_HEADS];
    char failed_name[MAX_HEADS][32];
    int nfailed;
    int W, H, s, lw, lh;
    int pixel_req;              /* display.pixel_scale: 0 = auto, else 1..4 (s is the effective scale) */
    /* Rotation (rotation.h, 4.5): user_rot is the user rotation in effect on
     * the primary, rot_req the one hostd asked for. A visible Mac OS keeps
     * the current orientation until its new (rotated) screen size is drawn
     * (rot_deferred, at most RELAYOUT_WAIT_MS), so the turn is one cut. */
    int user_rot, rot_req;
    bool rot_deferred;
    struct rb_timer *rot_timer;
    /* The target changed while Mac OS was starting (its video driver may not
     * have taken the RESIZE yet): re-request it after content, as
     * enforce_size does, until the surface matches. */
    bool retarget_pending;
    bool surface_seen;          /* this connection's first SURFACE arrived */
    uint32_t *shadow;
    rb_region dirty;
    struct udev *udev;
    struct udev_monitor *mon;
    struct rb_watch *drm_watch;
    struct rb_timer *present_timer;
    struct rb_timer *retry_timer;
    bool master_busy;           /* the last open failed: another process (a splash) holds DRM master */
    uint64_t last_retake_ms;    /* last late handoff from the splash (rate limit) */
    uint64_t last_flush_us;
    int host_mode[3]; /* override, 0 = auto */
    /* scene */
    enum rb_scene scene;
    enum rb_reveal reveal;
    bool revealed;
    enum rb_panel_bg panel_bg;
    enum view view;
    /* guest */
    struct rb_guest guest;
    rb_layout layout;
    bool layout_ok;
    /* A pixel-scale change that sent a RESIZE keeps the current surface's
     * layout until Mac OS's new surface arrives (or RELAYOUT_WAIT_MS), so the
     * old picture is never shown at the new scale in between. */
    bool layout_deferred;
    struct rb_timer *relayout_timer;
    /* ... and when it arrives, the previous picture stays until Mac OS has
     * repainted the new surface (QEMU clears VRAM on the mode set), so the
     * switch is one cut instead of a black frame and a visible repaint. */
    bool switch_hold;
    uint64_t switch_hold_mono;
    rb_content switch_content;
    int switch_best_nonblack;   /* paint progress: most non-black samples seen */
    uint64_t switch_best_mono;
    struct rb_timer *switch_timer;
    /* A switch Mac OS made itself (RetroBoot Display, Monitors, a game): the
     * last good frame stays until the new surface is complete (switchhold.h). */
    bool switch_guest;
    struct rb_switch_hold guest_switch;
    int surface_w, surface_h;   /* the connection's previous surface */
    enum rb_scaling scaling;
    int guest_w_cfg, guest_h_cfg; /* 0 = auto */
    rb_content content;
    uint64_t content_mono_ms;
    struct rb_timer *content_timer;
    bool guest_input;
    uint32_t sent_refresh;
    bool hold;                  /* keep the last guest frame (VIEW_HOLD) */
    /* guest size: target_w/h is the size in effect (configured or auto);
     * RESIZE goes out at every guest connection and when the target changes;
     * enforce_size adds the Wave 2 re-requests after content. */
    int target_w, target_h;
    bool enforce_size;
    struct rb_timer *resize_timer;
    int resize_attempts;
    uint64_t damage_seq;        /* DAMAGE messages this connection */
    uint64_t last_damage_mono;
    bool pointer_sent;          /* a POINTER_ABS went to this guest connection */
    struct rb_timer *nudge_timer;
    int nudge_x, nudge_y;
    uint64_t nudge_mono;
    /* panel */
    rbui *ui;
    uint32_t *panel;
    uint32_t *panel_bg_img;
    bool panel_force;
    bool panel_doc; /* hostd has shown a UI document (the installer's startup screen stays until then) */
    bool fallback;
    struct rb_timer *repeat_timer;
    int repeat_rbkey;
    uint32_t repeat_code;
    char repeat_utf8[16];
    unsigned repeat_mods;
    struct rb_timer *tick_timer;
    struct rb_timer *hostkey_timer; /* auto-repeat of a held host key */
    uint32_t hostkey_code;
    /* input */
    struct rb_input *input;
    bool swap;
    int chord_key;
    double speed;
    uint8_t phys[KEYBITS], fwd[KEYBITS], suppress[KEYBITS];
    double gx, gy;
    bool pointer_dirty;
    bool relative_pointer; /* negotiated by QEMU; old backends retain absolute input */
    struct rb_pointer_delta relative_remainder;
    unsigned guest_buttons;
    double px, py;
    unsigned panel_buttons;
    double wheel_acc;
    /* touchscreens and pens (touch.h, 4.8): one recognizer for every
     * touchscreen (seat slots are unique), bound to an output (conn id) when
     * the first finger goes down */
    struct rb_gesture gesture;
    bool touch_gestures;
    int touch_transform;        /* RB_TOUCH_* axes fix (input_config touch_transform, RB_TOUCH_TRANSFORM) */
    uint32_t touch_conn;
    struct rb_timer *touch_timer;
    unsigned pen_buttons;       /* bit b: guest/panel button b held by the pen */
    uint64_t touch_events;
    /* EDID monitor names of the outputs (touch binding), read once per connector
     * and forgotten on every display change */
    struct {
        uint32_t conn;
        char name[24];
    } edid[MAX_HEADS + 1];
    int nedid;
    uint64_t last_input_mono;
    uint64_t input_events;
    /* hostd */
    struct rb_ctl ctl;
    uint64_t hostd_absent_since;
    bool chord_pending;
    bool chord_wait_ack;
    struct rb_timer *chord_timer;
    /* screenshots */
    struct shot shots[MAX_SHOTS];
    /* power fallback */
    struct rb_timer *power_timer;
    int power_cmd; /* RB_AUTOBOOT / RB_POWER_OFF */
    pid_t power_pid;
    /* stats */
    uint64_t start_mono_ms;
    uint64_t present_ring[128]; /* mono us of recent presents (lazy fps) */
    unsigned present_head;
    uint64_t damage_pending_us;  /* first guest damage not yet presented */
    uint64_t trace_last_input;
    double trace_last_gx, trace_last_gy;
    bool trace_pointer_seen;
    bool trace_hash_disabled;
    struct rb_sample_row trace_row;
    bool trace_row_configured, trace_row_enabled, trace_row_disabled, trace_row_waiting;
    bool trace_row_seen;
    uint64_t trace_row_last_input;
    uint32_t trace_row_last_pid;
    uint64_t lat_count, lat_sum_us, lat_max_us;
    struct tl_entry tl[TIMELINE_N];
    int tl_head, tl_count;
    struct rb_splash_handoff handoff;
    bool handoff_used;
    uint64_t handoff_total_ms;
    /* DPI-aware checkerboard (4.4): pattern pixel cs = s * cm. */
    int cs, cm, cm_auto;
    int mm_w, mm_h;             /* physical size used (0 = unknown) */
    const char *mm_source;      /* "drm" | "quirk" | NULL */
    int boot_grid[3];           /* W, H, s the boot checker_scale was drawn for */
    uint32_t *panel_chk;        /* logical checkerboard at cm (the panel's background) */
    bool anchor_set;            /* rb_checker_anchor is this grid's (apply_anchor) */
    struct rb_boot_startup boot; /* GRUB's startup parameters (kernel command line) */
    /* Startup indicator (4.4.1). ind_cfg/art_path are hostd's wishes; they
     * take effect when the checkerboard is next entered (never mid-wait). */
    bool ind_cfg, ind_on;
    int startup_style;  /* selected Mac's preboot; not a global appearance setting */
    char art_path[256];
    bool art_pending;
    struct rb_icon_art art;
    struct rb_startup_geom ind_geom; /* where it stands (from the guest target) */
    struct rb_startup_geom ind_cur;  /* where it is drawn now (landing steps) */
    struct rb_progress prog;         /* CLOCK_BOOTTIME */
    int fill_shown;
    struct rb_timer *ind_timer;
    char ind_phase[24];
    /* Landing: the cut-over after genuine content (the bar completes, the
     * cut once Mac OS's Happy Mac is drawn in the same place, then the fade). */
    bool landing;
    int land_rule;
    uint64_t land_start;
    double land_p0;
    struct rb_startup_geom land; /* Mac OS's icon on screen (q = k) */
    int land_q_from, land_steps, land_step_ms;
    uint32_t land_sum;
    uint64_t land_sum_mono;
    bool land_icon_seen;
    struct rb_timer *land_timer;
    /* The green light blinks (1 Hz) from blink_t0; lit_shown is what is drawn. */
    uint64_t blink_t0;
    bool lit_shown;
    /* Mac OS's own Happy Mac: where hostd wants the exact copy saved (the
     * first startup screen that shows it), once per presenter run. */
    char capture_path[256];
    bool captured;
    struct rb_timer *cap_timer; /* capture without the indicator (it was off) */
    uint64_t cap_start, cap_sum_mono;
    uint32_t cap_sum;
    bool cap_seen;
    /* Mac OS draws its Happy Mac late (startup_config happy_mac "late": the
     * ATI Radeon, about 1.7 s after its startup gray in the lab, 3.4 s on
     * the N5105): the cut-over reveals the gray at once and the completed
     * indicator stays over it, where the Happy Mac will appear, until Mac OS
     * draws there (then it fades into what Mac OS drew) or OVERLAY_MAX_MS
     * passes (then it fades out). */
    bool icon_late;
    bool overlay;
    uint64_t overlay_start;
    uint32_t overlay_bg;        /* Mac OS's startup gray at the icon's corner when the overlay began */
    bool overlay_seen;          /* Mac OS drew there: overlay_sum since overlay_seen_mono */
    uint32_t overlay_sum;
    uint64_t overlay_seen_mono;
    struct rb_timer *overlay_timer;
    /* Cut-over fade: the screen area goes from the completed bar to Mac OS's
     * face over FADE_MS (the rest of the icon is already identical). */
    bool fading;
    uint64_t fade_start;
    rb_rect fade_rect;
    uint32_t *fade_px;
    struct rb_timer *fade_timer;
    /* web Screen page (remote.h): frame tap and remote input. Remote keys and
     * buttons are tracked apart from the local ones; qhold counts the holders
     * of each QKeyCode so the guest sees one press until both let go. */
    struct rb_remote remote;
    uint8_t rkeys[KEYBITS], rfwd[KEYBITS];
    unsigned rbuttons;
    unsigned rpanel_buttons;    /* panel buttons pressed remotely (released with the remote input) */
    uint8_t qhold[RB_Q_COUNT];
    /* remote-viewer indicator (badge.h) */
    bool badge_on, badge_control;
    int badge_where;            /* 0 none, 1 Mac OS menu bar, 2 corner (panel/checkerboard) */
    bool badge_drawn_control;
    int badge_gx;               /* menu bar: glyph x in guest pixels, -1 no room */
    rb_rect badge_fr;           /* drawn here (frame pixels) */
    bool badge_scan;
    enum view badge_view;
    struct rb_timer *badge_timer;
    /* key feedback indicator (osd.h), requested by hostd ("osd") */
    struct rb_osd osd;
    enum view osd_view;         /* the view it was shown over; it goes with it */
    rb_rect osd_fr;             /* where it is drawn (frame pixels) */
    uint32_t *osd_under;        /* the composition under osd_fr (restored for a held frame) */
    int osd_under_w, osd_under_h;
    struct rb_timer *osd_timer;
    /* lab: --record-guest */
    struct rb_framelog guest_log;
    bool guest_log_failed;
    /* installer startup screen (installer_splash.h, 4.4.2) */
    struct rb_isplash_run isplash;
    /* Startup Details (diagview.h, ARCHITECTURE 16): while visible the view
     * is the whole frame (diag_px, rendered for W x H at s) and local input
     * is swallowed except Escape, which hides it. */
    struct rb_diag_watch diag;
    uint32_t *diag_px;
    int diag_w, diag_h, diag_s;
    uint64_t diag_gen;
    bool diag_opts;            /* rendered with "Return: Startup Options" */
    char diag_pub[512];        /* the state last published for rb-bootdiag */
    uint64_t diag_input_ms;    /* first local input since the view last changed (boot ms) */
    uint64_t diag_view_ms;     /* when the view last changed (boot ms) */
    uint64_t diag_start_ms;    /* when the presenter started (boot ms) */
    struct rb_timer *diag_timer; /* 1 s: stalled flips, heartbeat (while rb-bootdiag runs) */
    struct rb_diag_flips diag_flips; /* as published */
};

static struct presenter *g_p;

/* ------------------------------------------------------------------ */
/* helpers                                                             */

static bool bit_get(const uint8_t *b, unsigned i) { return i < KEY_CNT && (b[i / 8] >> (i % 8)) & 1; }
static void bit_set(uint8_t *b, unsigned i, bool v)
{
    if (i >= KEY_CNT)
        return;
    if (v)
        b[i / 8] |= (uint8_t)(1u << (i % 8));
    else
        b[i / 8] &= (uint8_t)~(1u << (i % 8));
}

static void tl_add(struct presenter *p, const char *what, const char *fmt, ...) __attribute__((format(printf, 3, 4)));
static void tl_add(struct presenter *p, const char *what, const char *fmt, ...)
{
    struct tl_entry *e = &p->tl[(p->tl_head + p->tl_count) % TIMELINE_N];
    if (p->tl_count == TIMELINE_N)
        p->tl_head = (p->tl_head + 1) % TIMELINE_N;
    else
        p->tl_count++;
    e->mono_ms = rb_mono_ms();
    e->boot_ms = rb_boot_ms();
    snprintf(e->what, sizeof e->what, "%s", what);
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(e->detail, sizeof e->detail, fmt, ap);
    va_end(ap);
    rb_log("[%s] %s", e->what, e->detail);
}

static void emit(struct presenter *p, cJSON *ev)
{
    if (rb_ctl_connected(&p->ctl))
        rb_ctl_send_json(&p->ctl, ev);
    cJSON_Delete(ev);
}

static cJSON *new_event(const char *name)
{
    cJSON *ev = cJSON_CreateObject();
    cJSON_AddStringToObject(ev, "event", name);
    return ev;
}

static void add_times(cJSON *o)
{
    cJSON_AddNumberToObject(o, "mono_ms", (double)rb_mono_ms());
    cJSON_AddNumberToObject(o, "boot_ms", (double)rb_boot_ms());
}

static cJSON *int_array(int n, const int *v)
{
    cJSON *a = cJSON_CreateArray();
    for (int i = 0; i < n; i++)
        cJSON_AddItemToArray(a, cJSON_CreateNumber(v[i]));
    return a;
}

static bool rotated(const struct rb_output *o) { return o->rot == 90 || o->rot == 270; }

static cJSON *output_json(struct presenter *p, struct rb_output *o, bool primary)
{
    cJSON *j = cJSON_CreateObject();
    cJSON_AddStringToObject(j, "connector", o->conn_name);
    cJSON_AddStringToObject(j, "role", primary ? "primary" : "mirror");
    cJSON_AddBoolToObject(j, "internal", o->internal);
    int mode[3] = {o->w, o->h, o->refresh};
    cJSON_AddItemToObject(j, "mode", int_array(3, mode));
    cJSON_AddStringToObject(j, "mode_source", o->mode_source ? o->mode_source : "unknown");
    cJSON_AddNumberToObject(j, "rotation", o->rot);
    cJSON_AddNumberToObject(j, "panel_rotation", o->panel_rot);
    cJSON_AddStringToObject(j, "orientation", o->orientation[0] ? o->orientation : "none");
    cJSON_AddNumberToObject(j, "scale", o->xf.k);
    cJSON_AddNumberToObject(j, "reduce", o->xf.div); /* k = 0: exact 1/reduce of the frame (0: arbitrary fit) */
    int img[4] = {o->xf.img.x, o->xf.img.y, o->xf.img.w, o->xf.img.h};
    cJSON_AddItemToObject(j, "image", int_array(4, img));
    int mm[2] = {o->mm_w, o->mm_h};
    cJSON_AddItemToObject(j, "physical_mm", int_array(2, mm));
    cJSON_AddBoolToObject(j, "on", !o->off);
    cJSON_AddNumberToObject(j, "presents", (double)o->presents);
    if (o->atomic_damage || o->damage_commits) {
        /* drm.h kernel damage (geminipda-drm): atomic flips with FB_DAMAGE_CLIPS */
        cJSON *kd = cJSON_CreateObject();
        cJSON_AddBoolToObject(kd, "atomic", o->atomic_damage);
        cJSON_AddNumberToObject(kd, "commits", (double)o->damage_commits);
        cJSON_AddNumberToObject(kd, "full", (double)o->damage_full);
        cJSON_AddNumberToObject(kd, "px", (double)o->damage_px);
        cJSON_AddItemToObject(j, "kernel_damage", kd);
    }
    (void)p;
    return j;
}

static void display_fill(struct presenter *p, cJSON *d)
{
    struct rb_output *o = &p->out;
    bool rot = rotated(o);
    /* The connector's modes (and so display.host_mode) are listed the way the
     * panel is mounted, whatever the user rotation, so a turn never changes them. */
    bool prot = rb_rotation_swaps(o->panel_rot);
    cJSON_AddStringToObject(d, "device", o->devnode);
    cJSON_AddStringToObject(d, "driver", o->driver);
    cJSON_AddStringToObject(d, "connector", o->conn_name);
    /* Sizes are logical (after the panel rotation): what the frame, the
     * guest and the host_mode setting use. native_mode is the scanout. */
    int mode[3] = {rb_output_lw(o), rb_output_lh(o), o->refresh};
    cJSON_AddItemToObject(d, "mode", int_array(3, mode));
    int native[3] = {o->w, o->h, o->refresh};
    cJSON_AddItemToObject(d, "native_mode", int_array(3, native));
    cJSON_AddNumberToObject(d, "rotation", o->rot);
    cJSON_AddNumberToObject(d, "panel_rotation", o->panel_rot);
    cJSON_AddNumberToObject(d, "user_rotation", p->user_rot);
    if (p->rot_deferred)
        cJSON_AddNumberToObject(d, "user_rotation_pending", p->rot_req);
    cJSON_AddStringToObject(d, "orientation", p->W >= p->H ? "landscape" : "portrait");
    cJSON *modes = cJSON_CreateArray();
    for (int i = 0; i < o->nmodes; i++) {
        int mw = prot ? o->modes[i].vdisplay : o->modes[i].hdisplay, mh = prot ? o->modes[i].hdisplay : o->modes[i].vdisplay;
        int m[3] = {mw, mh, rb_mode_refresh(&o->modes[i])};
        bool dup = false;
        for (int j = 0; j < i; j++)
            if (o->modes[j].hdisplay == o->modes[i].hdisplay && o->modes[j].vdisplay == o->modes[i].vdisplay &&
                rb_mode_refresh(&o->modes[j]) == m[2])
                dup = true;
        if (!dup)
            cJSON_AddItemToArray(modes, int_array(3, m));
    }
    cJSON_AddItemToObject(d, "modes", modes);
    int mm[2] = {rot ? o->mm_h : o->mm_w, rot ? o->mm_w : o->mm_h};
    cJSON_AddItemToObject(d, "physical_mm", int_array(2, mm));
    cJSON_AddNumberToObject(d, "suggested_scale", rb_suggested_scale(p->W, p->H));
    cJSON_AddNumberToObject(d, "scale", p->s);
    if (p->pixel_req)
        cJSON_AddNumberToObject(d, "pixel_scale", p->pixel_req);
    else
        cJSON_AddStringToObject(d, "pixel_scale", "auto");
    cJSON_AddNumberToObject(d, "checker_scale", p->cs);
    if (p->mm_source) {
        int mm2[2] = {p->mm_w, p->mm_h};
        cJSON_AddItemToObject(d, "physical_mm_used", int_array(2, mm2));
        cJSON_AddStringToObject(d, "physical_mm_source", p->mm_source);
    }
    cJSON_AddBoolToObject(d, "mode_inherited", o->mode_inherited);
    cJSON_AddStringToObject(d, "mode_source", o->mode_source ? o->mode_source : "unknown");
    cJSON_AddBoolToObject(d, "page_flip", o->flip_ok);
    cJSON_AddStringToObject(d, "primary_preference", p->prefer_external ? "external" : "internal");
    cJSON *outs = cJSON_AddArrayToObject(d, "outputs");
    cJSON_AddItemToArray(outs, output_json(p, o, true));
    for (int i = 0; i < p->nheads; i++)
        cJSON_AddItemToArray(outs, output_json(p, &p->heads[i], false));
    if (p->nfailed) {
        cJSON *f = cJSON_AddArrayToObject(d, "failed_outputs");
        for (int i = 0; i < p->nfailed; i++)
            cJSON_AddItemToArray(f, cJSON_CreateString(p->failed_name[i]));
    }
}

static cJSON *display_json(struct presenter *p)
{
    if (!p->have_out)
        return cJSON_CreateNull();
    cJSON *d = cJSON_CreateObject();
    display_fill(p, d);
    return d;
}

static void guest_fill(struct presenter *p, cJSON *g)
{
    bool conn = rb_guest_connected(&p->guest);
    cJSON_AddBoolToObject(g, "connected", conn);
    cJSON_AddNumberToObject(g, "pid", conn ? p->guest.pid : 0);
    cJSON_AddStringToObject(g, "session", conn ? p->guest.session : "");
    if (conn && p->guest.px) {
        int sz[2] = {p->guest.width, p->guest.height};
        cJSON_AddItemToObject(g, "surface", int_array(2, sz));
    } else {
        cJSON_AddNullToObject(g, "surface");
    }
    cJSON_AddBoolToObject(g, "content", p->content.detected);
    cJSON_AddBoolToObject(g, "revealed", p->view == VIEW_GUEST);
    /* Frame activity for hostd's frozen-guest check: DAMAGE (and SURFACE)
     * messages of this connection and the time since the last one. */
    cJSON_AddNumberToObject(g, "damage_seq", conn ? (double)p->damage_seq : 0);
    if (conn && p->last_damage_mono)
        cJSON_AddNumberToObject(g, "last_damage_ms", (double)(rb_mono_ms() - p->last_damage_mono));
    else
        cJSON_AddNullToObject(g, "last_damage_ms");
    if (conn && p->guest.px) {
        int pt[2] = {(int)lround(p->gx), (int)lround(p->gy)};
        cJSON_AddItemToObject(g, "pointer", int_array(2, pt));
    }
}

static cJSON *guest_json(struct presenter *p)
{
    cJSON *g = cJSON_CreateObject();
    guest_fill(p, g);
    return g;
}

/* ------------------------------------------------------------------ */
/* composition                                                         */

static void mark_dirty(struct presenter *p, rb_rect r)
{
    if (!p->shadow)
        return;
    r = rb_rect_clip(r, p->W, p->H);
    if (!rb_rect_empty(r))
        rb_region_add(&p->dirty, r);
}

static void mark_all(struct presenter *p) { mark_dirty(p, (rb_rect){0, 0, p->W, p->H}); }

/* ------------------------------------------------------------------ */
/* startup indicator (4.4.1)                                           */

#define LAND_RAMP_MS 80        /* the bar completes */
#define LAND_FULL_MS 40        /* ... and is seen full (two to three refreshes) */
#define LAND_STEP_MS 60        /* a move, only if the guest's surface is not the target's */
#define LAND_STEPS_MAX_MS 120
#define LAND_ICON_WAIT_MS 1000 /* longest wait for Mac OS's Happy Mac after content */
#define LAND_POLL_MS 16
#define LAND_ICON_MIN_PX 48 /* non-gray pixels of a drawn startup icon (the Happy Mac has ~600) */
#define FADE_MS 250          /* the bar fades into Mac OS's smile */
#define OVERLAY_MAX_MS 8000  /* longest wait for a late Happy Mac (happy_mac "late") */
#define FADE_POLL_MS 16

static bool ind_visible(struct presenter *p)
{
    return p->startup_style != RB_STARTUP_OSX && p->view == VIEW_CHECKER && p->ind_on && p->ind_cur.valid && p->shadow;
}

static double ind_value(struct presenter *p, uint64_t now_boot)
{
    double v = rb_progress_at(&p->prog, now_boot);
    if (p->landing) {
        uint64_t t = rb_mono_ms() - p->land_start;
        double r = t >= LAND_RAMP_MS ? 1.0 : (double)t / LAND_RAMP_MS;
        double lv = p->land_p0 + (1.0 - p->land_p0) * r;
        if (lv > v)
            v = lv;
    }
    return v;
}

/* Bar animation: wake only when the next bar step is due; only the bar's
 * rectangle is redrawn. */
static void ind_tick(struct presenter *p)
{
    if (!ind_visible(p)) {
        rb_timer_arm(p->ind_timer, 0);
        return;
    }
    uint64_t now = rb_boot_ms();
    int fill = rb_startup_fill(ind_value(p, now));
    if (fill > p->fill_shown) {
        p->fill_shown = fill;
        mark_dirty(p, rb_startup_bar_rect(&p->ind_cur));
    }
    /* The light blinks like a drive light; it stays on for the cut-over. */
    bool lit = p->landing || rb_startup_lit(p->blink_t0, now);
    if (lit != p->lit_shown) {
        p->lit_shown = lit;
        mark_dirty(p, rb_startup_light_rect(&p->ind_cur, &p->art));
    }
    if (p->landing) { /* the landing timer drives it */
        rb_timer_arm(p->ind_timer, 0);
        return;
    }
    uint64_t next = now + 5000;
    if (p->prog.timed && p->fill_shown < RB_BAR_STEPS) {
        uint64_t n = rb_progress_next_step(&p->prog, now, RB_BAR_STEPS, 5000);
        next = n < next ? n : next;
    }
    if (p->art.nlight) {
        uint64_t n = p->blink_t0 + ((now - p->blink_t0) / RB_BLINK_MS + 1) * RB_BLINK_MS;
        next = n < next ? n : next;
    }
    rb_timer_arm(p->ind_timer, next > now + 20 ? next - now : 20);
}

static void ind_timer_fn(void *data) { ind_tick(data); }

/* Installer startup screen: its status line or bar changed. */
static void isplash_redraw(void *data, int what)
{
    struct presenter *p = data;
    if (what & RB_ISPLASH_DIRTY_STATUS)
        tl_add(p, "installer_splash", "%s", p->isplash.phase == RB_ISPLASH_STARTING ? "starting up" : "welcome");
    if (p->view == VIEW_CHECKER && p->shadow)
        mark_dirty(p, rb_isplash_run_dirty(&p->isplash, what, p->W, p->H, p->s, p->pixel_req, p->guest_w_cfg,
                                           p->guest_h_cfg));
}

static void guest_target_size(struct presenter *p, int *w, int *h);
static void publish_display(struct presenter *p);
static void land_end(struct presenter *p, const char *why);
static void compose(struct presenter *p);
static void fade_end(struct presenter *p, const char *why);
static void overlay_end(struct presenter *p, const char *why, bool fade);

static void update_borders(struct presenter *p);

/* The checkerboard's anchor (4.4): the top-left of Mac OS's picture for the
 * guest target, the same as the splash's and GRUB's. A new anchor redraws
 * every place the pattern shows: the frame, the panel's background, the
 * mirrors' borders. */
static void apply_anchor(struct presenter *p)
{
    struct rb_checker_anchor a = rb_startup_anchor(&p->ind_geom);
    bool changed = !p->anchor_set || a.x != rb_checker_anchor.x || a.y != rb_checker_anchor.y;
    rb_checker_anchor = a;
    p->anchor_set = true;
    if (!changed)
        return;
    rbui_set_checker_phase(rb_checker_unit_phase(p->s));
    if (p->panel_chk)
        rb_startup_panel_background(p->panel_chk, p->lw, p->lw, p->lh, p->cm, p->s, p->startup_style);
    update_borders(p);
    if (p->shadow)
        mark_all(p);
    tl_add(p, "checker", "pattern %d px anchored at %d,%d (Mac OS's picture)", p->cs, a.x, a.y);
}

/* Where the indicator stands: Mac OS's icon position for the guest target;
 * the checkerboard is anchored at the same layout's picture. */
static void ind_relayout(struct presenter *p)
{
    overlay_end(p, "layout change", false); /* it stands on the old layout */
    rb_rect old = p->ind_cur.valid ? rb_startup_rect(&p->ind_cur) : (rb_rect){0, 0, 0, 0};
    int gw, gh;
    guest_target_size(p, &gw, &gh);
    rb_startup_geometry(&p->ind_geom, p->W, p->H, p->s, p->pixel_req, gw, gh);
    if (!p->landing)
        p->ind_cur = p->ind_geom;
    if (p->view == VIEW_CHECKER) {
        mark_dirty(p, old);
        mark_dirty(p, rb_startup_rect(&p->ind_cur));
    }
    apply_anchor(p);
}

/* Entering the checkerboard: hostd's latest wishes (on/off, art) apply now,
 * never while the indicator is on screen. After Mac OS was shown, a new
 * startup begins at 0 (hostd retimes it). */
static void ind_latch(struct presenter *p, int old_view)
{
    p->ind_on = p->ind_cfg;
    if (p->art_pending) {
        rb_icon_art_load(&p->art, p->art_path);
        p->art_pending = false;
    }
    if (old_view == VIEW_GUEST || old_view == VIEW_HOLD) {
        rb_progress_reset(&p->prog, rb_boot_ms(), 0);
        p->fill_shown = 0;
        p->blink_t0 = rb_boot_ms();
        p->lit_shown = true;
        snprintf(p->ind_phase, sizeof p->ind_phase, "relaunch");
    }
    p->ind_cur = p->ind_geom;
}

static bool guest_visible_ok(struct presenter *p)
{
    return rb_guest_connected(&p->guest) && p->guest.px && p->layout_ok;
}

static enum view compute_view(struct presenter *p)
{
    switch (p->scene) {
    case RB_SCENE_SPLASH: return VIEW_CHECKER;
    case RB_SCENE_BLACK: return VIEW_BLACK;
    case RB_SCENE_PANEL:
        /* The installer's startup screen (4.4.2) stays until the first panel
         * document can replace it, so the cut goes from the startup box
         * straight to the window, never through an empty checkerboard. */
        return p->isplash.on && !p->panel_doc ? VIEW_CHECKER : VIEW_PANEL;
    case RB_SCENE_GUEST:
        if (p->revealed && guest_visible_ok(p))
            return VIEW_GUEST;
        return p->hold ? VIEW_HOLD : VIEW_CHECKER;
    }
    return VIEW_CHECKER;
}

static void send_refresh_rate(struct presenter *p)
{
    if (!rb_guest_connected(&p->guest))
        return;
    uint32_t want = (p->scene == RB_SCENE_GUEST || p->scene == RB_SCENE_SPLASH) ? 16 : 100;
    if (want != p->sent_refresh) {
        rb_guest_send_refresh(&p->guest, want);
        p->sent_refresh = want;
    }
}

/* Mirrors continue the selected Mac's neutral backdrop. */
static void update_borders(struct presenter *p)
{
    bool checker = p->view == VIEW_CHECKER || p->view == VIEW_NONE ||
                   (p->view == VIEW_PANEL && p->panel_bg == RB_PANEL_BG_CHECKERBOARD);
    for (int i = 0; i < p->nheads; i++)
        rb_output_set_border(&p->heads[i], p->startup_style == RB_STARTUP_OSX &&
            (p->view == VIEW_GUEST || p->view == VIEW_HOLD) ? RB_BORDER_OSX_GRAY : checker ?
            (p->startup_style == RB_STARTUP_OSX ? RB_BORDER_OSX_GRAY : RB_BORDER_CHECKER) :
            RB_BORDER_BLACK, p->cs, rb_checker_anchor);
}

static void osd_hide(struct presenter *p, bool restore);

static void update_view(struct presenter *p)
{
    enum view v = compute_view(p);
    if (v == p->view)
        return;
    enum view old = p->view;
    osd_hide(p, v == VIEW_HOLD);
    p->view = v;
    if (v != VIEW_CHECKER && p->isplash.on) /* the installer's startup screen is over */
        rb_isplash_run_end(&p->isplash);
    p->diag_view_ms = rb_boot_ms();
    p->diag_input_ms = 0;
    if (v == VIEW_CHECKER)
        ind_latch(p, old);
    if (v != VIEW_HOLD) /* the held frame is already in the shadow and on screen */
        mark_all(p);
    if (v == VIEW_PANEL)
        p->panel_force = true;
    if (v == VIEW_GUEST)
        p->hold = false;
    update_borders(p);
    uint64_t since = rb_guest_connected(&p->guest) ? rb_mono_ms() - p->guest.connect_mono_ms : 0;
    tl_add(p, "view", "%s -> %s (scene %s)", view_name(old), view_name(v), rb_scene_name(p->scene));
    if ((old == VIEW_CHECKER || old == VIEW_HOLD) && v == VIEW_GUEST) {
        tl_add(p, "guest_revealed", "t=%llu ms after guest connect", (unsigned long long)since);
        cJSON *ev = new_event("guest_revealed");
        cJSON_AddNumberToObject(ev, "t_ms", (double)since);
        add_times(ev);
        emit(p, ev);
    }
    ind_tick(p);
}

static void fill_black(struct presenter *p, rb_rect r)
{
    for (int y = r.y; y < r.y + r.h; y++)
        memset(p->shadow + (size_t)y * p->W + r.x, 0, (size_t)r.w * 4);
}

static void compose_guest(struct presenter *p, rb_rect r)
{
    rb_rect d = p->layout.dst;
    /* letterbox: up to four bands of r outside dst */
    rb_rect bands[4] = {
        {r.x, r.y, r.w, d.y - r.y},                                 /* above */
        {r.x, d.y + d.h, r.w, r.y + r.h - (d.y + d.h)},             /* below */
        {r.x, d.y, d.x - r.x, d.h},                                 /* left */
        {d.x + d.w, d.y, r.x + r.w - (d.x + d.w), d.h},             /* right */
    };
    for (int i = 0; i < 4; i++) {
        rb_rect b;
        if (rb_rect_intersect(bands[i], r, &b)) {
            if (p->startup_style == RB_STARTUP_OSX)
                rb_startup_scene_style(p->shadow, p->W, p->W, p->H, b, p->cs, &p->ind_cur,
                                       &p->art, false, 0, false, RB_STARTUP_OSX);
            else
                fill_black(p, b);
        }
    }
    rb_layout_render(&p->layout, p->shadow, p->W, p->guest.px, p->guest.stride_px, r);
}

/* The cut-over fade: inside the icon's screen, the completed bar over Mac
 * OS's live frame, more transparent as it goes (smoothstep). */
static void compose_fade(struct presenter *p, rb_rect r)
{
    rb_rect f;
    if (!p->fading || !p->fade_px || !rb_rect_intersect(r, p->fade_rect, &f))
        return;
    uint64_t t = rb_mono_ms() - p->fade_start;
    double x = t >= FADE_MS ? 1.0 : (double)t / FADE_MS;
    uint32_t w = (uint32_t)lround(x * x * (3.0 - 2.0 * x) * 256.0); /* weight of Mac OS's pixels */
    if (w >= 256)
        return;
    uint32_t iw = 256 - w;
    for (int y = f.y; y < f.y + f.h; y++) {
        uint32_t *d = p->shadow + (size_t)y * p->W;
        const uint32_t *b = p->fade_px + (size_t)(y - p->fade_rect.y) * p->fade_rect.w - p->fade_rect.x;
        for (int x0 = f.x; x0 < f.x + f.w; x0++) {
            uint32_t a = b[x0], g = d[x0];
            uint32_t rb = (((a & 0xff00ffu) * iw + (g & 0xff00ffu) * w) >> 8) & 0xff00ffu;
            uint32_t gg = (((a & 0x00ff00u) * iw + (g & 0x00ff00u) * w) >> 8) & 0x00ff00u;
            d[x0] = rb | gg;
        }
    }
}

static void compose_rect(struct presenter *p, rb_rect r)
{
    if (p->diag.visible && p->diag_px) { /* Startup Details covers everything */
        for (int y = r.y; y < r.y + r.h; y++)
            memcpy(p->shadow + (size_t)y * p->W + r.x, p->diag_px + (size_t)y * p->W + r.x, (size_t)r.w * 4);
        return;
    }
    switch (p->view) {
    case VIEW_GUEST:
        if (!p->switch_hold) { /* the previous picture stays in the shadow */
            compose_guest(p, r);
            if (p->overlay) /* the indicator over Mac OS's gray, until its late Happy Mac */
                rb_startup_draw(p->shadow, p->W, p->W, p->H, r, &p->ind_cur, &p->art, p->fill_shown, true);
            compose_fade(p, r);
        }
        break;
    case VIEW_PANEL: {
        int s = p->s;
        rb_rect lr = {r.x / s, r.y / s, (r.x + r.w + s - 1) / s - r.x / s, (r.y + r.h + s - 1) / s - r.y / s};
        rb_upscale_rect(p->shadow, p->W, p->W, p->H, p->panel, p->lw, lr, s);
        break;
    }
    case VIEW_HOLD:
        /* The shadow still holds the last guest frame; leave it (a display
         * change reallocated the shadow, which is black). */
        break;
    case VIEW_BLACK:
    case VIEW_NONE:
        fill_black(p, r);
        break;
    case VIEW_CHECKER:
    default:
        /* The startup scene, as the splash and GRUB's background draw it. */
        rb_startup_scene_style(p->shadow, p->W, p->W, p->H, r, p->cs, &p->ind_cur, &p->art, ind_visible(p), p->fill_shown,
                               p->lit_shown, p->startup_style);
        rb_isplash_run_scene(&p->isplash, p->shadow, p->W, p->W, p->H, r, p->s, p->pixel_req, p->guest_w_cfg,
                             p->guest_h_cfg);
        break;
    }
}

static int rbui_bg(struct presenter *p)
{
    switch (p->panel_bg) {
    case RB_PANEL_BG_BLACK: return RBUI_BG_BLACK;
    case RB_PANEL_BG_GUEST_DIMMED: return RBUI_BG_IMAGE_DIMMED;
    default:
        /* The presenter's own pattern (cm logical pixels, anchored at Mac
         * OS's picture), so the panel's checkerboard is the frame's. */
        return p->panel_chk ? RBUI_BG_IMAGE : RBUI_BG_CHECKERBOARD;
    }
}

static const uint32_t *rbui_bg_img(struct presenter *p)
{
    return rbui_bg(p) == RBUI_BG_IMAGE ? p->panel_chk : p->panel_bg_img;
}

static void panel_update(struct presenter *p)
{
    if (p->view != VIEW_PANEL || !p->ui || !p->panel)
        return;
    int x, y, w, h;
    if (p->panel_force) {
        p->panel_force = false;
        rbui_render(p->ui, p->panel, p->lw, rbui_bg(p), rbui_bg_img(p), p->lw, 1);
        mark_all(p);
        rb_remote_damage(&p->remote, RB_REMOTE_FRAME, &(rb_rect){0, 0, p->lw, p->lh}, 1);
        return;
    }
    if (rbui_damage(p->ui, &x, &y, &w, &h)) {
        rbui_render(p->ui, p->panel, p->lw, rbui_bg(p), rbui_bg_img(p), p->lw, 1);
        mark_dirty(p, (rb_rect){x * p->s, y * p->s, w * p->s, h * p->s});
        rb_remote_damage(&p->remote, RB_REMOTE_FRAME, &(rb_rect){x, y, w, h}, 1);
    }
}

/* ------------------------------------------------------------------ */
/* key feedback indicator (osd.h)                                      */

/* Only over a revealed Mac OS or the panel: never the checkerboard, the
 * startup indicator, a cut-over, a pixel-scale switch or a held frame. */
static bool osd_allowed(const struct presenter *p)
{
    if (p->view == VIEW_PANEL)
        return p->panel != NULL;
    return p->view == VIEW_GUEST && p->revealed && p->layout_ok && !p->switch_hold && !p->fading && !p->landing &&
           !p->overlay;
}

static rb_rect osd_area(const struct presenter *p)
{
    if (p->view == VIEW_GUEST && p->layout_ok)
        return p->layout.dst;
    return (rb_rect){0, 0, p->W, p->H};
}

static void osd_compose(struct presenter *p, rb_rect r)
{
    if (!p->osd.active || p->view != p->osd_view || !osd_allowed(p))
        return;
    rb_rect fr = rb_osd_rect(&p->osd, osd_area(p), p->s, p->W, p->H), oi;
    p->osd_fr = fr;
    if (!rb_rect_intersect(fr, r, &oi))
        return;
    /* keep what is under it, for a guest frame that is held after a disconnect */
    if (!p->osd_under || p->osd_under_w != fr.w || p->osd_under_h != fr.h) {
        free(p->osd_under);
        p->osd_under = calloc((size_t)fr.w * (size_t)fr.h, 4);
        p->osd_under_w = p->osd_under ? fr.w : 0;
        p->osd_under_h = p->osd_under ? fr.h : 0;
    }
    if (p->osd_under)
        for (int y = oi.y; y < oi.y + oi.h; y++)
            memcpy(p->osd_under + (size_t)(y - fr.y) * fr.w + (oi.x - fr.x), p->shadow + (size_t)y * p->W + oi.x,
                   (size_t)oi.w * 4);
    rb_osd_render(&p->osd, p->shadow, p->W, fr, oi, p->s, p->osd.alpha);
}

/* Take the indicator down. restore: put back the picture under it in the
 * shadow (a held frame is never recomposed). */
static void osd_hide(struct presenter *p, bool restore)
{
    if (!p->osd.active)
        return;
    p->osd.active = false;
    rb_timer_arm(p->osd_timer, 0);
    rb_rect fr = rb_rect_clip(p->osd_fr, p->W, p->H);
    if (restore && p->shadow && p->osd_under && !rb_rect_empty(fr) && fr.w == p->osd_under_w &&
        fr.h == p->osd_under_h && fr.x == p->osd_fr.x && fr.y == p->osd_fr.y)
        for (int y = 0; y < fr.h; y++)
            memcpy(p->shadow + (size_t)(fr.y + y) * p->W + fr.x, p->osd_under + (size_t)y * fr.w, (size_t)fr.w * 4);
    if (!rb_rect_empty(fr))
        mark_dirty(p, fr);
    p->osd_fr = (rb_rect){0, 0, 0, 0};
}

static void osd_timer_fn(void *data)
{
    struct presenter *p = data;
    if (!p->osd.active)
        return;
    if (p->view != p->osd_view || !osd_allowed(p)) {
        osd_hide(p, false);
        return;
    }
    uint64_t now = rb_mono_ms();
    int a = rb_osd_alpha(&p->osd, now);
    if (a <= 0) {
        osd_hide(p, false);
        return;
    }
    if (a != p->osd.alpha) {
        p->osd.alpha = a;
        mark_dirty(p, p->osd_fr);
    }
    uint64_t t = now - p->osd.shown_ms;
    rb_timer_arm(p->osd_timer, t < RB_OSD_HOLD_MS ? (int)(RB_OSD_HOLD_MS - t) : RB_OSD_STEP_MS);
}

/* The checkerboard is up and Mac OS not shown yet (Escape's startup options). */
static bool on_checker_preboot(const struct presenter *p)
{
    return p->view == VIEW_CHECKER && (p->scene == RB_SCENE_SPLASH || p->scene == RB_SCENE_GUEST);
}

/* Startup Details at the frame's size and scale (re-rendered when either or
 * the content changed); the hint is drawn over the checkerboard only. */
static void diag_prepare(struct presenter *p)
{
    if (!p->diag.visible) {
        free(p->diag_px);
        p->diag_px = NULL;
        return;
    }
    bool opts = rb_diag_return_opens_options(p->diag.cfg.installer, true, on_checker_preboot(p));
    if (p->diag_px && p->diag_w == p->W && p->diag_h == p->H && p->diag_s == p->s && p->diag_gen == p->diag.doc_gen &&
        p->diag_opts == opts)
        return;
    if (!p->diag_px || p->diag_w != p->W || p->diag_h != p->H) {
        free(p->diag_px);
        p->diag_px = malloc((size_t)p->W * (size_t)p->H * 4);
    }
    if (!p->diag_px)
        return;
    p->diag_w = p->W, p->diag_h = p->H, p->diag_s = p->s, p->diag_gen = p->diag.doc_gen, p->diag_opts = opts;
    rb_diag_render_keys(p->diag_px, p->W, p->H, p->W, p->s > 0 ? p->s : 1, p->diag.doc,
                        opts ? RB_DIAG_KEYS_OPTIONS : NULL);
    mark_all(p);
}

static bool diag_hint_shown(const struct presenter *p)
{
    return p->diag.hint && !p->diag.visible && p->view == VIEW_CHECKER && !p->landing && !p->fading;
}

static void compose(struct presenter *p)
{
    if (!p->shadow)
        return;
    panel_update(p);
    diag_prepare(p);
    rb_rect hint_fr = diag_hint_shown(p) ? rb_diag_hint_rect(p->W, p->H, p->s) : (rb_rect){0, 0, 0, 0};
    for (int i = 0; i < p->dirty.n; i++) {
        rb_rect r = rb_rect_clip(p->dirty.r[i], p->W, p->H);
        if (rb_rect_empty(r))
            continue;
        compose_rect(p, r);
        rb_rect hi;
        if (!rb_rect_empty(hint_fr) && rb_rect_intersect(hint_fr, r, &hi))
            rb_diag_render_hint(p->shadow, p->W, p->H, p->W, p->s);
        if (p->diag.visible) {
            if (p->have_out)
                rb_output_damage(&p->out, r);
            for (int h = 0; h < p->nheads; h++)
                rb_output_damage(&p->heads[h], r);
            continue;
        }
        if (p->view == VIEW_CHECKER && p->s > 0) {
            rb_rect lr = {r.x / p->s, r.y / p->s, (r.x + r.w + p->s - 1) / p->s - r.x / p->s,
                          (r.y + r.h + p->s - 1) / p->s - r.y / p->s};
            rb_remote_damage(&p->remote, RB_REMOTE_CHECKER, &lr, 1);
        }
        rb_rect bi;
        if (p->badge_where && !(p->view == VIEW_GUEST && p->switch_hold) && rb_rect_intersect(p->badge_fr, r, &bi))
            rb_badge_render(p->shadow, p->W, p->badge_fr, bi, p->badge_control ? RB_BADGE_CONTROL : RB_BADGE_VIEW,
                            p->badge_where == 2);
        osd_compose(p, r);
        if (p->have_out)
            rb_output_damage(&p->out, r);
        for (int h = 0; h < p->nheads; h++)
            rb_output_damage(&p->heads[h], r);
    }
    rb_region_clear(&p->dirty);
}

static void close_output(struct presenter *p, const char *why);

static void log_first_present(struct presenter *p, struct rb_output *o, const char *role)
{
    tl_add(p, "modeset", "%s %s %s %dx%d@%d rot %d committed (%s mode from %s%s, %s)", role, o->devnode,
           o->conn_name, o->w, o->h, o->refresh, o->rot, o->mode_inherited ? "inherited" : "new",
           o->mode_source ? o->mode_source : "?", o->fb[0].offset ? ", tall buffer" : "",
           o->flip_ok ? "page-flip" : "single-buffer");
}

static void remove_head(struct presenter *p, int i, const char *why);
static void drain_flips(struct presenter *p);
static void display_changed(struct presenter *p, const char *why);
static void emit_display(struct presenter *p);

static bool conn_failed(const struct presenter *p, uint32_t conn_id)
{
    for (int i = 0; i < p->nfailed; i++)
        if (p->failed_conn[i] == conn_id)
            return true;
    return false;
}

static void mark_failed(struct presenter *p, const struct rb_output *o, const char *why)
{
    if (!conn_failed(p, o->conn_id) && p->nfailed < MAX_HEADS) {
        p->failed_conn[p->nfailed] = o->conn_id;
        size_t n = strnlen(o->conn_name, sizeof p->failed_name[0] - 1);
        memcpy(p->failed_name[p->nfailed], o->conn_name, n); /* o may be p->out */
        p->failed_name[p->nfailed][n] = 0;
        p->nfailed++;
    }
    tl_add(p, "output_failed", "%s: %s; not used until the presenter restarts", o->conn_name, why);
}

/* Only DSI is checked: a stalled DSI pipe is the known failure (see
 * rb_output_scanout_stalled), and an eDP panel in self refresh may hold
 * its frame counter while working normally. */
static bool dsi_stalled(struct rb_output *o, const char *when)
{
    if (o->conn_type != DRM_MODE_CONNECTOR_DSI)
        return false;
    int rc = rb_output_scanout_stalled(o);
    rb_log("display: %s scanout check (%s): %s", o->conn_name, when,
           rc == 1 ? "stalled" : rc == 0 ? "running" : "not active");
    return rc == 1;
}

#define STALLED_WHY "the panel is not scanning out (the driver could not power it on)"

/* A DSI panel the firmware left off while another display is connected (GPD
 * MicroPC after a cold start with HDMI attached): Linux can't power it on
 * either, and committing to it leaves a commit pending that every later one
 * on its pipe waits 10 s for (hostboot2 report). Never committed to. */
static bool dsi_left_off(struct rb_output *o, const char *when)
{
    /* Only a driver that reads back the firmware's state knows the firmware left
     * the panel off (i915); on a Raspberry Pi every CRTC starts off. */
    bool lit = o->fw_lit || rb_driver_no_readout(o->driver);
    if (o->conn_type != DRM_MODE_CONNECTOR_DSI || !rb_output_never_commit(o->conn_type, lit, 0))
        return false;
    rb_log("display: %s left off by the firmware (%s); not used", o->conn_name, when);
    return true;
}

/* An external primary that takes over from a failed panel keeps the mode
 * the firmware gave it (640x480 on the GPD MicroPC after an RTC wake): on
 * Gemini Lake a full modeset on it pulls the stalled pipe into the commit
 * and waits out two flip timeouts (20 s). */

/* The primary stopped working and a mirror can take over. */
static void fail_primary(struct presenter *p, const char *why)
{
    drain_flips(p);
    mark_failed(p, &p->out, why);
    struct rb_output dead = p->out;
    p->out = p->heads[0];
    p->out.shared_fd = false;
    p->heads[0] = p->heads[--p->nheads];
    memset(&p->heads[p->nheads], 0, sizeof p->heads[p->nheads]);
    p->heads[p->nheads].fd = -1;
    dead.shared_fd = true; /* the DRM file stays open for the new primary */
    rb_output_abandon(&dead);
    tl_add(p, "primary", "%s is now primary (%s failed)", p->out.conn_name, dead.conn_name);
    display_changed(p, "output failed");
}

static void fail_head(struct presenter *p, int i, const char *why)
{
    drain_flips(p);
    mark_failed(p, &p->heads[i], why);
    rb_output_abandon(&p->heads[i]);
    p->heads[i] = p->heads[--p->nheads];
    memset(&p->heads[p->nheads], 0, sizeof p->heads[p->nheads]);
    p->heads[p->nheads].fd = -1;
    emit_display(p);
}

/* Mirrors: flipped whenever they have damage and no flip in flight; never
 * paced against the primary (each has its own refresh). */
static void present_heads(struct presenter *p)
{
    for (int i = 0; i < p->nheads; i++) {
        struct rb_output *o = &p->heads[i];
        if (!rb_output_needs_flush(o) || o->pending >= 0)
            continue;
        bool first = !o->on_screen;
        int rc = rb_output_flush(o, p->shadow, p->W);
        if (rc > 0 && first) {
            log_first_present(p, o, "mirror");
            if (!o->mode_inherited && dsi_stalled(o, "first present"))
                fail_head(p, i--, STALLED_WHY);
        } else if (rc == -ENODEV)
            remove_head(p, i--, "device gone");
        else if (rc < 0 && rc != -EBUSY && !rb_timer_armed(p->retry_timer)) {
            rb_log("present failed on %s: %s; retrying", o->conn_name, strerror(-rc));
            rb_timer_arm(p->retry_timer, 100);
        }
    }
}

/* Explicit lab instrumentation: hash the buffer actually submitted to KMS,
 * not the concurrently mutable guest SHM. Hashing cost is part of the record. */
static void trace_submitted_buffer(struct presenter *p)
{
    if (!getenv("RB_PRESENT_TRACE_HASH") || p->trace_hash_disabled)
        return;
    struct rb_output *o = &p->out;
    int index = o->pending >= 0 ? o->pending : o->front;
    if (index < 0 || index >= o->nfb || o->w <= 128 || o->h <= 128)
        return;
    struct rb_fb *fb = &o->fb[index];
    if (!fb->map || fb->pitch < (uint64_t)o->w * 4 ||
        (uint64_t)fb->offset + (uint64_t)(o->h - 1) * fb->pitch + (uint64_t)o->w * 4 > fb->size)
        return;
    uint64_t started = rb_mono_us();
    uint64_t full = 1469598103934665603ULL, content = full;
    rb_rect viewport = p->layout.dst;
    rb_rect roi = {viewport.x + 64, viewport.y + 64, viewport.w - 128, viewport.h - 128};
    int cx = viewport.x + (int)p->gx, cy = viewport.y + (int)p->gy;
    bool qualified = p->view == VIEW_GUEST && p->scene == RB_SCENE_GUEST &&
        o->xf.identity && p->layout_ok && p->layout.mode == RB_SCALING_INTEGER && p->layout.k == 1 &&
        viewport.x >= 0 && viewport.y >= 0 && viewport.x + viewport.w <= o->w &&
        viewport.y + viewport.h <= o->h && roi.w > 0 && roi.h > 0 &&
        viewport.w == p->guest.width && viewport.h == p->guest.height &&
        p->trace_pointer_seen && p->trace_last_input == p->input_events &&
        p->trace_last_gx == p->gx && p->trace_last_gy == p->gy;
    for (int y = 0; y < o->h; y++) {
        const uint8_t *row = fb->map + fb->offset + (uint64_t)y * fb->pitch;
        for (int x = 0; x < o->w; x++) {
            uint32_t pixel;
            memcpy(&pixel, row + x * 4, sizeof pixel);
            pixel &= 0x00ffffffu; /* XRGB unused byte is not a visible change */
            full = (full ^ pixel) * 1099511628211ULL;
            if (x >= roi.x && x < roi.x + roi.w && y >= roi.y && y < roi.y + roi.h) {
                if (x >= cx - 64 && x < cx + 64 && y >= cy - 64 && y < cy + 64)
                    pixel = 0;
                content = (content ^ pixel) * 1099511628211ULL;
            }
        }
    }
    fprintf(stderr, "RBPRESENT {\"stage\":\"submitted-pixels\",\"us\":%llu,\"present\":%llu,\"crtc\":%u,\"guest_pid\":%u,\"seq\":%u,\"fb\":%d,\"width\":%d,\"height\":%d,\"pitch\":%u,\"offset\":%u,\"hash\":\"%016llx\",\"content_hash\":\"%016llx\",\"content_geometry_qualified\":%s,\"viewport\":[%d,%d,%d,%d],\"roi\":[%d,%d,%d,%d],\"roi_inset\":64,\"cursor_half\":64,\"cursor_x\":%d,\"cursor_y\":%d,\"input_events\":%llu,\"hash_us\":%llu}\n",
            (unsigned long long)started, (unsigned long long)o->presents, o->crtc_id,
            p->guest.pid, p->guest.last_seq, index, o->w, o->h, fb->pitch, fb->offset,
            (unsigned long long)full, (unsigned long long)content, qualified ? "true" : "false",
            viewport.x, viewport.y, viewport.w, viewport.h, roi.x, roi.y, roi.w, roi.h,
            cx, cy, (unsigned long long)p->input_events, (unsigned long long)(rb_mono_us() - started));
    p->trace_last_input = p->input_events;
    p->trace_last_gx = p->gx;
    p->trace_last_gy = p->gy;
    p->trace_pointer_seen = true;
    if (rb_mono_us() - started > 1000) {
        p->trace_hash_disabled = true;
        rb_log("full-buffer hash observation disabled after exceeding its 1ms safety budget");
    }
}

/* Opt-in sparse reads from the actual submitted buffer. Unlike full-frame
 * hashing this has at most 64 pixel locations. Raw colors let an independent
 * decoder check the original movie's frame marker after YUV rounding.
 * Coordinates are native guest pixels; only exact, unrotated 1x is accepted.
 * A sent pointer position is not independent native cursor interrogation. The
 * workload must also verify its cursor is parked away from the sampled row. */
static void trace_submitted_row(struct presenter *p)
{
    const char *config = getenv("RB_PRESENT_TRACE_ROW");
    if (!config || p->trace_row_disabled)
        return;
    uint64_t started = rb_mono_us();
    if (!p->trace_row_configured) {
        p->trace_row_configured = true;
        p->trace_row_enabled = rb_sample_row_parse(config, &p->trace_row);
        if (!p->trace_row_enabled)
            rb_log("submitted-row observation disabled: use x,y,step,count, at most 64 pixels");
    }
    if (!p->trace_row_enabled)
        return;
    struct rb_output *o = &p->out;
    struct rb_sample_row row;
    bool geometry = p->view == VIEW_GUEST && p->scene == RB_SCENE_GUEST &&
        o->xf.identity && p->layout_ok && p->layout.mode == RB_SCALING_INTEGER && p->layout.k == 1 &&
        rb_sample_row_translate(&p->trace_row, p->layout.dst, p->guest.width, p->guest.height,
                                o->w, o->h, &row);
    if (!geometry) {
        if (!p->trace_row_waiting)
            rb_log("submitted-row observation waits for an unclipped, unrotated 1x guest");
        p->trace_row_waiting = true;
        p->trace_row_seen = false;
        return;
    }
    p->trace_row_waiting = false;
    int index = o->pending >= 0 ? o->pending : o->front;
    if (index < 0 || index >= o->nfb)
        return;
    struct rb_fb *fb = &o->fb[index];
    uint32_t pixels[RB_SAMPLE_MAX];
    uint64_t read_started = rb_mono_us();
    if (!rb_sample_row_read(fb->map, fb->size, fb->pitch, fb->offset, o->w, o->h, &row, pixels)) {
        p->trace_row_disabled = true;
        rb_log("submitted-row observation disabled: framebuffer bounds differ");
        return;
    }
    uint64_t read_us = rb_mono_us() - read_started;
    bool overlap = false;
    uint64_t hash = 1469598103934665603ULL;
    for (int i = 0; i < row.count; i++) {
        int x = p->trace_row.x + p->trace_row.step * i;
        if (x >= p->gx - 64 && x < p->gx + 64 &&
            p->trace_row.y >= p->gy - 64 && p->trace_row.y < p->gy + 64)
            overlap = true;
        hash = (hash ^ pixels[i]) * 1099511628211ULL;
    }
    bool stable = p->trace_row_seen && p->trace_row_last_pid == p->guest.pid &&
        p->trace_row_last_input == p->input_events;
    bool qualified = p->pointer_sent && !overlap && stable;
    char line[4096];
    int used = snprintf(line, sizeof line,
        "{\"stage\":\"submitted-samples\",\"us\":%llu,\"presenter_epoch_ms\":%llu,\"output_epoch_ms\":%llu,\"present\":%llu,\"crtc\":%u,\"guest_pid\":%u,\"seq\":%u,\"fb\":%d,\"flip_expected\":%s,\"format\":\"XRGB8888\",\"geometry_qualified\":true,\"qualified\":%s,\"viewport\":[%d,%d,%d,%d],\"native_row\":[%d,%d,%d,%d],\"frame_row\":[%d,%d,%d,%d],\"pointer_sent\":%s,\"cursor_overlap\":%s,\"input_stable\":%s,\"cursor\":[%d,%d],\"input_events\":%llu,\"read_us\":%llu,\"hash\":\"%016llx\",\"rgb\":[",
        (unsigned long long)started, (unsigned long long)p->start_mono_ms,
        (unsigned long long)o->first_present_mono_ms, (unsigned long long)o->presents,
        o->crtc_id, p->guest.pid, p->guest.last_seq, index, o->pending >= 0 ? "true" : "false",
        qualified ? "true" : "false", p->layout.dst.x, p->layout.dst.y, p->layout.dst.w, p->layout.dst.h,
        p->trace_row.x, p->trace_row.y, p->trace_row.step, p->trace_row.count,
        row.x, row.y, row.step, row.count, p->pointer_sent ? "true" : "false",
        overlap ? "true" : "false", stable ? "true" : "false", (int)p->gx, (int)p->gy,
        (unsigned long long)p->input_events, (unsigned long long)read_us, (unsigned long long)hash);
    for (int i = 0; i < row.count && used >= 0 && (size_t)used < sizeof line; i++) {
        int n = snprintf(line + used, sizeof line - (size_t)used, "%s\"%06x\"", i ? "," : "", pixels[i]);
        used = n < 0 ? -1 : used + n;
    }
    if (used < 0 || (size_t)used + 4 >= sizeof line) {
        p->trace_row_disabled = true;
        rb_log("submitted-row observation disabled: record exceeded its bound");
        return;
    }
    memcpy(line + used, "]}\n", 4);
    fprintf(stderr, "RBPRESENT %s", line);
    /* Includes sampling, formatting and the raw-record write. The following
     * cost-record write is additional: off/on workload controls remain needed. */
    uint64_t whole_us = rb_mono_us() - started;
    bool over = whole_us > 1000;
    fprintf(stderr, "RBPRESENT {\"stage\":\"sample-observer-cost\",\"presenter_epoch_ms\":%llu,\"output_epoch_ms\":%llu,\"present\":%llu,\"crtc\":%u,\"guest_pid\":%u,\"whole_us\":%llu,\"over_budget\":%s}\n",
            (unsigned long long)p->start_mono_ms, (unsigned long long)o->first_present_mono_ms,
            (unsigned long long)o->presents, o->crtc_id, p->guest.pid,
            (unsigned long long)whole_us, over ? "true" : "false");
    if (over) {
        p->trace_row_disabled = true;
        rb_log("submitted-row observation disabled after exceeding its 1ms safety budget");
    }
    p->trace_row_seen = true;
    p->trace_row_last_input = p->input_events;
    p->trace_row_last_pid = p->guest.pid;
}

static void present(struct presenter *p)
{
    if (!p->have_out)
        return;
    present_heads(p);
    if (!rb_output_needs_flush(&p->out) || p->out.pending >= 0)
        return;
    uint64_t now = rb_mono_us();
    uint64_t min_us = p->out.refresh > 0 ? 500000u / (uint64_t)p->out.refresh : 8000u;
    if (p->out.on_screen && now - p->last_flush_us < min_us) {
        if (!rb_timer_armed(p->present_timer))
            rb_timer_arm(p->present_timer, (min_us - (now - p->last_flush_us)) / 1000u + 1);
        return;
    }
    bool first = !p->out.on_screen;
    if (getenv("RB_PRESENT_TRACE") || getenv("RB_PRESENT_TRACE_ROW")) {
        p->out.trace_presenter_epoch_ms = p->start_mono_ms;
        p->out.trace_guest_pid = p->guest.pid;
    }
    int rc = rb_output_flush(&p->out, p->shadow, p->W);
    if (rc > 0) {
        trace_submitted_row(p);
        trace_submitted_buffer(p);
        if (getenv("RB_PRESENT_TRACE") || getenv("RB_PRESENT_TRACE_ROW"))
            fprintf(stderr, "RBPRESENT {\"stage\":\"submit\",\"us\":%llu,\"guest_pid\":%u,\"seq\":%u,\"present\":%llu,\"scene\":%d,\"pending\":%d}\n",
                    (unsigned long long)rb_mono_us(), p->guest.pid, p->guest.last_seq,
                    (unsigned long long)p->out.presents, p->scene, p->out.pending);
        p->last_flush_us = now;
        p->present_ring[p->present_head++ % 128] = now;
        if (p->damage_pending_us) {
            uint64_t lat = rb_mono_us() - p->damage_pending_us;
            p->lat_count++;
            p->lat_sum_us += lat;
            if (lat > p->lat_max_us)
                p->lat_max_us = lat;
            p->damage_pending_us = 0;
        }
        if (first) {
            log_first_present(p, &p->out, "primary");
            if (!p->out.mode_inherited && p->nheads > 0 && dsi_stalled(&p->out, "first present"))
                fail_primary(p, STALLED_WHY);
        }
    } else if (rc == -ENODEV) {
        close_output(p, "device gone");
    } else if (rc < 0) {
        if (!rb_timer_armed(p->retry_timer)) {
            rb_log("present failed on %s: %s; retrying", p->out.devnode, strerror(-rc));
            rb_timer_arm(p->retry_timer, 100);
        }
    }
}

static void send_pointer(struct presenter *p)
{
    if (!p->pointer_dirty)
        return;
    p->pointer_dirty = false;
    p->relative_remainder = (struct rb_pointer_delta){0};
    if (p->scene == RB_SCENE_GUEST && rb_guest_connected(&p->guest) && p->guest.px && p->guest_input) {
        rb_guest_send_pointer(&p->guest, (uint32_t)p->gx, (uint32_t)p->gy);
        p->pointer_sent = true;
    }
}

static void badge_update(struct presenter *p);

static void diag_publish(struct presenter *p);

static void idle(void *data)
{
    struct presenter *p = data;
    send_pointer(p);
    badge_update(p);
    compose(p);
    present(p);
    rb_remote_flush(&p->remote);
    diag_publish(p);
}

static void present_timer_fn(void *data) { (void)data; }

/* ------------------------------------------------------------------ */
/* display lifecycle                                                   */

#define RESIZE_SETTLE_MS 2000
#define RESIZE_MAX_ATTEMPTS 4

static void guest_target_size(struct presenter *p, int *w, int *h)
{
    /* configured classic sizes turn on their side on a portrait frame */
    rb_guest_size_for(p->W, p->H, p->s, p->guest_w_cfg, p->guest_h_cfg, w, h);
}

static void send_resize(struct presenter *p, const char *why)
{
    if (!rb_guest_connected(&p->guest) || !p->have_out || !p->target_w)
        return;
    rb_guest_send_resize(&p->guest, (uint32_t)p->target_w, (uint32_t)p->target_h);
    tl_add(p, "resize", "RESIZE %dx%d (%s; %s)", p->target_w, p->target_h, p->guest_w_cfg ? "configured" : "auto",
           why);
}

/* enforce_size only (display_config "enforce_guest_size", the Wave 2
 * behaviour): Mac OS may restore its own saved resolution after the
 * connect-time RESIZE; once genuine content is showing and the surface has
 * been stable for a moment, re-request the target size (QEMU re-nudges the
 * driver when the request repeats), a bounded number of times per
 * connection. By default the presenter never re-requests: a resolution
 * chosen inside Mac OS (Monitors, a game's own mode switch) is scaled. */
static void schedule_resize_check(struct presenter *p)
{
    if (p->retarget_pending && rb_guest_connected(&p->guest) && p->guest.width == p->target_w &&
        p->guest.height == p->target_h)
        p->retarget_pending = false;
    if (!(p->enforce_size || p->retarget_pending) || !rb_guest_connected(&p->guest) || !p->guest.px ||
        !p->content.detected || !p->have_out || !p->target_w)
        return;
    if (p->guest.width == p->target_w && p->guest.height == p->target_h) {
        rb_timer_arm(p->resize_timer, 0);
        return;
    }
    if (p->resize_attempts < RESIZE_MAX_ATTEMPTS)
        rb_timer_arm(p->resize_timer, RESIZE_SETTLE_MS);
}

static void resize_timer_fn(void *data)
{
    struct presenter *p = data;
    if (!(p->enforce_size || p->retarget_pending) || !rb_guest_connected(&p->guest) || !p->guest.px)
        return;
    if (p->guest.width == p->target_w && p->guest.height == p->target_h)
        return;
    p->resize_attempts++;
    tl_add(p, "resize_retry", "guest at %dx%d, re-requesting %dx%d (attempt %d)", p->guest.width,
           p->guest.height, p->target_w, p->target_h, p->resize_attempts);
    send_resize(p, p->enforce_size ? "enforce" : "retarget");
    if (p->resize_attempts < RESIZE_MAX_ATTEMPTS)
        rb_timer_arm(p->resize_timer, RESIZE_SETTLE_MS * 3);
}

/* Recompute the guest size target (configured guest_size, else the 4.5 auto
 * size of the current host mode). A connected guest gets a RESIZE only when
 * the target changed: a deliberate host-side change (display mode or the
 * guest_size setting). hostd launches QEMU with -g equal to the target, so a
 * new guest starts at the right size without one. */
static bool update_target(struct presenter *p, const char *why)
{
    if (!p->have_out)
        return false;
    int w, h;
    guest_target_size(p, &w, &h);
    if (w == p->target_w && h == p->target_h)
        return false;
    p->target_w = w;
    p->target_h = h;
    p->resize_attempts = 0;
    send_resize(p, why);
    schedule_resize_check(p);
    return true;
}

/* Guest layout on the shared grid: with a numeric pixel scale every Mac
 * pixel is an s x s block when that fits, else the largest fitting factor;
 * auto keeps the largest fitting factor (4.5). */
static void relayout(struct presenter *p)
{
    p->layout_ok = false;
    if (p->layout_deferred) {
        p->layout_deferred = false;
        rb_timer_arm(p->relayout_timer, 0);
    }
    if (p->guest.px && p->W > 0)
        p->layout_ok = rb_layout_compute_ex(&p->layout, p->W, p->H, p->guest.width, p->guest.height, p->scaling,
                                            p->pixel_req ? p->s : 0, p->s) == 0;
    else
        rb_layout_free(&p->layout);
}

static void render_dimmed_bg(struct presenter *p)
{
    if (!p->panel_bg_img)
        return;
    if (!guest_visible_ok(p)) {
        rb_startup_panel_background(p->panel_bg_img, p->lw, p->lw, p->lh, p->cm, p->s, p->startup_style);
        return;
    }
    rb_layout l;
    memset(&l, 0, sizeof l);
    /* Exactly where the guest view shows it when its blocks are whole logical
     * pixels (always for a numeric pixel scale); else fitted to the panel. */
    if (rb_layout_logical(&p->layout, p->s, &l) < 0 &&
        rb_layout_compute(&l, p->lw, p->lh, p->guest.width, p->guest.height, p->scaling) < 0)
        return;
    memset(p->panel_bg_img, 0, (size_t)p->lw * p->lh * 4);
    rb_layout_render(&l, p->panel_bg_img, p->lw, p->guest.px, p->guest.stride_px, (rb_rect){0, 0, p->lw, p->lh});
    rb_layout_free(&l);
}

/* The shared pixel grid (4.4/4.5): scale s for the checkerboard, the guest
 * and the panel, whose logical buffers are ceil(W/s) x ceil(H/s). */
static void fit_mirror_mode(struct presenter *p, struct rb_output *h);

static void setup_grid(struct presenter *p)
{
    p->s = rb_effective_scale(p->W, p->H, p->pixel_req);
    p->lw = (p->W + p->s - 1) / p->s;
    p->lh = (p->H + p->s - 1) / p->s;
    /* The checkerboard's pattern pixel: GRUB's (retroboot.checker_scale) as
     * long as the grid is the one it was drawn for, else the DPI rule. */
    p->mm_source = p->have_out ? rb_display_mm(p->out.conn_type, p->W, p->H, p->out.mm_w, p->out.mm_h, &p->mm_w,
                                               &p->mm_h)
                               : NULL;
    p->cm_auto = rb_checker_multiple(p->W, p->H, p->s, p->mm_w, p->mm_h);
    p->cm = p->cm_auto;
    if (p->boot.checker_scale > 0 && p->boot_grid[0] == 0) {
        p->boot_grid[0] = p->W;
        p->boot_grid[1] = p->H;
        p->boot_grid[2] = p->s;
    }
    if (p->boot.checker_scale > 0 && p->boot_grid[0] == p->W && p->boot_grid[1] == p->H && p->boot_grid[2] == p->s &&
        p->boot.checker_scale % p->s == 0)
        p->cm = p->boot.checker_scale / p->s;
    p->cs = p->s * p->cm;
    /* Every output shows the frame (primary 1:1, mirrors scaled); a re-grid
     * redraws them all. The pattern's anchor follows the startup geometry
     * (ind_relayout below). */
    rb_output_set_frame(&p->out, p->W, p->H, false);
    for (int i = 0; i < p->nheads; i++) {
        rb_xform_set_src_scale(&p->heads[i].xf, p->s);
        rb_output_set_frame(&p->heads[i], p->W, p->H, true);
        fit_mirror_mode(p, &p->heads[i]);
    }
    update_borders(p);
    free(p->panel);
    free(p->panel_bg_img);
    free(p->panel_chk);
    p->panel = calloc((size_t)p->lw * p->lh, 4);
    p->panel_bg_img = calloc((size_t)p->lw * p->lh, 4);
    p->panel_chk = malloc((size_t)p->lw * p->lh * 4);
    if (!p->panel || !p->panel_bg_img || !p->panel_chk) {
        rb_log("out of memory for %dx%d panel buffers", p->lw, p->lh);
        abort();
    }
    if (p->ui)
        rbui_resize(p->ui, p->lw, p->lh);
    p->anchor_set = false; /* new buffers: the pattern is drawn whatever the anchor */
    ind_relayout(p);
}

/* Rotation (4.5): the primary turns by its panel orientation plus the user
 * rotation; mirrors keep their own panel orientation, so an external display
 * shows the picture upright (pillarboxed when it is portrait). */
static void assign_rotations(struct presenter *p)
{
    rb_user_rotation = p->user_rot;
    p->out.rot = rb_rotation_add(p->out.panel_rot, p->user_rot);
    for (int i = 0; i < p->nheads; i++)
        p->heads[i].rot = p->heads[i].panel_rot;
}

static void display_changed(struct presenter *p, const char *why)
{
    assign_rotations(p);
    p->nedid = 0; /* outputs may have changed: read their EDID names again */
    /* The frame is the primary output's logical (rotated) size. */
    p->W = rb_output_lw(&p->out);
    p->H = rb_output_lh(&p->out);
    free(p->shadow);
    p->shadow = calloc((size_t)p->W * p->H, 4);
    if (!p->shadow) {
        rb_log("out of memory for %dx%d buffers", p->W, p->H);
        abort();
    }
    if (p->landing) {
        land_end(p, "display change");
        p->revealed = true;
    }
    overlay_end(p, "display change", false);
    fade_end(p, "display change");
    setup_grid(p);
    p->switch_hold = p->switch_guest = false; /* the new shadow is black: draw the guest at once */
    rb_input_set_screen(p->input, p->W, p->H);
    if (p->px >= p->W || p->py >= p->H || (p->px == 0 && p->py == 0)) {
        p->px = p->W / 2.0;
        p->py = p->H / 2.0;
    }
    relayout(p);
    rb_region_clear(&p->dirty);
    p->view = VIEW_NONE;
    update_view(p);
    mark_all(p);
    p->panel_force = true;
    if (p->scene == RB_SCENE_PANEL && p->panel_bg == RB_PANEL_BG_GUEST_DIMMED)
        render_dimmed_bg(p);
    update_borders(p);
    tl_add(p, "display", "%s: %s (%s) %s %dx%d@%d rot %d (panel %d user %d) scale %d logical %dx%d checker %d (%s mm %dx%d); "
           "%d mirror(s)",
           why, p->out.devnode, p->out.driver, p->out.conn_name, p->W, p->H, p->out.refresh, p->out.rot,
           p->out.panel_rot, p->user_rot, p->s, p->lw, p->lh, p->cs, p->mm_source ? p->mm_source : "no", p->mm_w,
           p->mm_h, p->nheads);
    publish_display(p);
    cJSON *ev = new_event("display");
    display_fill(p, ev);
    emit(p, ev);
    update_target(p, why);
}

#define RELAYOUT_WAIT_MS 5000

static void relayout_timer_fn(void *data)
{
    struct presenter *p = data;
    if (!p->layout_deferred)
        return;
    tl_add(p, "relayout", "no new guest surface within %d ms; relaying out %dx%d at scale %d", RELAYOUT_WAIT_MS,
           p->guest.width, p->guest.height, p->s);
    relayout(p);
    if (p->view == VIEW_GUEST)
        mark_all(p);
}

#define SWITCH_HOLD_MAX_MS 1000
#define SWITCH_SETTLE_MS 150
#define SWITCH_GROWTH_MS 100
#define SWITCH_POLL_MS 16

static void rotation_now(struct presenter *p, const char *why);

static void end_switch_hold(struct presenter *p, const char *why)
{
    if (!p->switch_hold)
        return;
    p->switch_hold = false;
    rb_timer_arm(p->switch_timer, 0);
    if (p->switch_guest)
        tl_add(p, "switch_shown", "%dx%d k=%d after %llu ms (Mac OS's switch, %s; uniform %d/1000, changed %s)",
               p->guest.width, p->guest.height, p->layout.k,
               (unsigned long long)(rb_mono_ms() - p->switch_hold_mono), why,
               p->guest_switch.last_uniform_permille, p->guest_switch.changed ? "yes" : "no");
    else
        tl_add(p, "switch_shown", "%dx%d k=%d after %llu ms (%s; nonblack %d%% settled %d ms)", p->guest.width,
               p->guest.height, p->layout.k, (unsigned long long)(rb_mono_ms() - p->switch_hold_mono), why,
               p->switch_content.last_nonblack_pct, p->switch_content.last_settled_ms);
    p->switch_guest = false;
    if (p->rot_deferred) {
        /* Mac OS has drawn its rotated screen: turn the frame with it, one cut */
        rotation_now(p, why);
        return;
    }
    if (p->view == VIEW_GUEST)
        mark_all(p);
}

/* The new surface counts as repainted when a quarter of it is non-black and
 * either the paint has stopped spreading (the non-black share has not grown
 * for 100 ms; an animation keeps changing pixels but does not grow it) or it
 * has been settled (<= 1 % of the samples changed) for 150 ms, or by the 4.3
 * rules; at most 1 s (e.g. a mostly black desktop). */
static void check_switch_hold(struct presenter *p)
{
    if (!p->switch_hold)
        return;
    if (!p->guest.px || p->view != VIEW_GUEST) {
        end_switch_hold(p, "not shown");
        return;
    }
    uint64_t now = rb_mono_ms();
    if (p->switch_guest) {
        int v = rb_switch_hold_check(&p->guest_switch, p->guest.px, p->guest.width, p->guest.height,
                                     p->guest.stride_px, now, p->last_damage_mono);
        if (v != RB_SWITCH_HOLD)
            end_switch_hold(p, rb_switch_verdict_name(v));
        return;
    }
    int rule = rb_content_inspect(&p->switch_content, p->guest.px, p->guest.width, p->guest.height,
                                  p->guest.stride_px, now);
    int nonblack = p->switch_content.last_nonblack_pct;
    if (nonblack > p->switch_best_nonblack) {
        p->switch_best_nonblack = nonblack;
        p->switch_best_mono = now;
    }
    bool painted = nonblack >= RB_CONTENT_NONBLACK_PCT;
    if (rule || (painted && p->switch_content.last_settled_ms >= SWITCH_SETTLE_MS))
        end_switch_hold(p, "repainted");
    else if (painted && now - p->switch_best_mono >= SWITCH_GROWTH_MS)
        end_switch_hold(p, "painted");
    else if (now - p->switch_hold_mono >= SWITCH_HOLD_MAX_MS)
        end_switch_hold(p, "timeout");
}

static void switch_timer_fn(void *data)
{
    struct presenter *p = data;
    check_switch_hold(p);
    if (p->switch_hold)
        rb_timer_arm(p->switch_timer, SWITCH_POLL_MS);
}

/* display_config pixel_scale (live): re-grid the checkerboard and panel at
 * the new scale and retarget the guest. When that sends Mac OS a RESIZE the
 * visible guest keeps its current layout until the new surface arrives, so
 * the only transition the user sees is Mac OS's own mode switch. */
static void set_pixel_scale(struct presenter *p, int req)
{
    int old_req = p->pixel_req, old_s = p->s;
    p->pixel_req = req;
    if (!p->have_out) {
        tl_add(p, "display_config", "pixel_scale %d (no display yet)", req);
        return;
    }
    if (rb_effective_scale(p->W, p->H, req) != old_s) {
        setup_grid(p);
        p->panel_force = true;
        if (p->view == VIEW_CHECKER || p->view == VIEW_PANEL)
            mark_all(p);
    }
    bool resized = update_target(p, "pixel_scale");
    int max_k_old = old_req ? old_s : 0, max_k_new = req ? p->s : 0;
    bool layout_changes = p->s != old_s || max_k_old != max_k_new;
    if (layout_changes && resized && guest_visible_ok(p)) {
        p->layout_deferred = true;
        rb_timer_arm(p->relayout_timer, RELAYOUT_WAIT_MS);
    } else if (layout_changes) {
        relayout(p);
        if (p->view == VIEW_GUEST)
            mark_all(p);
    }
    if (p->scene == RB_SCENE_PANEL && p->panel_bg == RB_PANEL_BG_GUEST_DIMMED && p->s != old_s)
        render_dimmed_bg(p);
    update_view(p);
    tl_add(p, "display_config", "pixel_scale %s%d: scale %d -> %d logical %dx%d target %dx%d%s",
           req ? "" : "auto=", req ? req : rb_suggested_scale(p->W, p->H), old_s, p->s, p->lw, p->lh, p->target_w,
           p->target_h, p->layout_deferred ? " (guest layout waits for the new surface)" : "");
    if (p->s != old_s || old_req != req) {
        cJSON *ev = new_event("display");
        display_fill(p, ev);
        emit(p, ev);
    }
    publish_display(p);
}

/* display_config "rotation" (4.5): the user rotation, applied at once over
 * the checkerboard and the panel. Over a visible Mac OS the frame keeps its
 * orientation while Mac OS switches to the rotated screen size (RESIZE now),
 * and turns when that screen has been drawn (guest_surface, then the repaint
 * hold), or after RELAYOUT_WAIT_MS when Mac OS does not switch. */
static void rotation_now(struct presenter *p, const char *why)
{
    p->rot_deferred = false;
    rb_timer_arm(p->rot_timer, 0);
    int old = p->user_rot;
    p->user_rot = p->rot_req;
    if (!p->have_out) {
        rb_user_rotation = p->user_rot;
        return;
    }
    bool starting = rb_guest_connected(&p->guest) && !p->content.detected;
    display_changed(p, why);
    tl_add(p, "rotation", "%d -> %d: frame %dx%d, guest target %dx%d (%s)", old, p->user_rot, p->W, p->H, p->target_w,
           p->target_h, why);
    if (starting) {
        /* the video driver may not take a RESIZE before it is up */
        p->retarget_pending = true;
        p->resize_attempts = 0;
    }
}

static void rot_timer_fn(void *data)
{
    struct presenter *p = data;
    if (p->rot_deferred)
        rotation_now(p, "no rotated guest surface in time");
}

static void set_rotation(struct presenter *p, int rot)
{
    p->rot_req = rot;
    if (!p->have_out) {
        p->user_rot = rot;
        rb_user_rotation = rot;
        tl_add(p, "display_config", "rotation %d (no display yet)", rot);
        return;
    }
    if (rot == p->user_rot) {
        if (p->rot_deferred) { /* turned back before Mac OS switched */
            p->rot_deferred = false;
            rb_timer_arm(p->rot_timer, 0);
            update_target(p, "rotation cancelled");
            tl_add(p, "rotation", "back to %d before Mac OS switched", rot);
        }
        return;
    }
    /* The frame and guest target under the new rotation. */
    int nw, nh, tw, th;
    rb_rotated_size(rb_rotation_add(p->out.panel_rot, rot), p->out.w, p->out.h, &nw, &nh);
    rb_guest_size_for(nw, nh, rb_effective_scale(nw, nh, p->pixel_req), p->guest_w_cfg, p->guest_h_cfg, &tw, &th);
    if (p->view == VIEW_GUEST && guest_visible_ok(p) && p->content.detected &&
        (tw != p->guest.width || th != p->guest.height)) {
        p->rot_deferred = true;
        p->target_w = tw;
        p->target_h = th;
        p->resize_attempts = 0;
        send_resize(p, "rotation");
        rb_timer_arm(p->rot_timer, RELAYOUT_WAIT_MS);
        tl_add(p, "rotation", "%d -> %d: waiting for Mac OS at %dx%d", p->user_rot, rot, tw, th);
        emit_display(p);
        return;
    }
    rotation_now(p, "rotation");
}

static void drm_ready(void *data, int fd, uint32_t events)
{
    (void)fd;
    (void)events;
    struct presenter *p = data;
    if (p->have_out)
        rb_output_dispatch(&p->out);
}

static void close_output(struct presenter *p, const char *why)
{
    if (!p->have_out)
        return;
    tl_add(p, "display_lost", "%s: %s", p->out.devnode, why);
    if (p->drm_watch)
        rb_watch_remove(p->drm_watch);
    p->drm_watch = NULL;
    for (int i = 0; i < p->nheads; i++)
        rb_output_close(&p->heads[i]); /* shared file: closed with the primary */
    p->nheads = 0;
    rb_output_close(&p->out);
    p->have_out = false;
    cJSON *ev = new_event("display");
    cJSON_AddNullToObject(ev, "device");
    emit(p, ev);
}

/* --- mirrors (other connectors of the primary's card) --- */

static void emit_display(struct presenter *p)
{
    cJSON *ev = new_event("display");
    display_fill(p, ev);
    emit(p, ev);
}

/* Wait (bounded) for page flips in flight: their completion events carry
 * the output's address, so outputs must not move while one is pending. */
static void drain_flips(struct presenter *p)
{
    for (int tries = 0; tries < 10; tries++) {
        bool pending = p->out.pending >= 0;
        for (int i = 0; i < p->nheads; i++)
            pending |= p->heads[i].pending >= 0;
        if (!pending)
            return;
        struct pollfd pfd = {.fd = p->out.fd, .events = POLLIN};
        if (poll(&pfd, 1, 20) > 0)
            rb_output_dispatch(&p->out);
    }
    p->out.pending = -1;
    for (int i = 0; i < p->nheads; i++)
        p->heads[i].pending = -1;
}

static void remove_head(struct presenter *p, int i, const char *why)
{
    drain_flips(p);
    tl_add(p, "mirror_removed", "%s (%s)", p->heads[i].conn_name, why);
    rb_output_close_head(&p->heads[i]);
    p->heads[i] = p->heads[--p->nheads];
    memset(&p->heads[p->nheads], 0, sizeof p->heads[p->nheads]);
}

static bool wants_primary(struct presenter *p, const struct rb_output *o)
{
    return p->prefer_external ? !o->internal : o->internal;
}

/* Make the preferred connector the primary (the frame follows its size). */
static bool elect_primary(struct presenter *p, const char *why)
{
    if (!p->have_out || wants_primary(p, &p->out))
        return false;
    for (int i = 0; i < p->nheads; i++) {
        if (!wants_primary(p, &p->heads[i]))
            continue;
        drain_flips(p);
        struct rb_output t = p->out;
        p->out = p->heads[i];
        p->heads[i] = t;
        p->out.shared_fd = false; /* the primary owns the DRM file */
        p->heads[i].shared_fd = true;
        tl_add(p, "primary", "%s is now primary, %s mirrors (%s)", p->out.conn_name, p->heads[i].conn_name, why);
        /* A panel the firmware ran as a scaled clone of an external display
         * (N5105 laptop, RTC cold boot with HDMI: eDP 2160x1440 at 1920x1080)
         * gets its native mode, as a panel opened as primary does. */
        if (rb_output_use_native(&p->out) > 0)
            tl_add(p, "mode", "%s native %dx%d", p->out.conn_name, p->out.w, p->out.h);
        display_changed(p, why);
        return true;
    }
    return false;
}

/* A mirror whose inherited mode cannot show the frame at 1x (fbcon's clone
 * mode, a firmware mode) switches to a mode that can: its preferred mode
 * when that fits, else the largest that does. */
static void fit_mirror_mode(struct presenter *p, struct rb_output *h)
{
    int ow = h->w, oh = h->h;
    /* A portrait frame (a turned laptop) never changes a landscape mirror's
     * mode: it is shown upright and pillarboxed at the mode it has. */
    if (p->H > p->W && rb_output_lw(h) > rb_output_lh(h))
        return;
    int rc = rb_output_fit_mirror(h, p->W, p->H);
    if (rc > 0)
        tl_add(p, "mirror_mode", "%s %dx%d -> %dx%d so the %dx%d frame shows at %dx%s", h->conn_name, ow, oh, h->w,
               h->h, p->W, p->H, h->xf.k, h->xf.div == 2 ? " (exact 1/2)" : h->xf.div ? " (exact 1/d)" : "");
    else if (rc < 0)
        rb_log("display: %s cannot show the %dx%d frame at 1x (%s); scaled to fit", h->conn_name, p->W, p->H,
               strerror(-rc));
}

/* Bring heads[] in line with the card's connected connectors. Emits the
 * display event when something changed (emit_always: in any case). */
static void sync_heads(struct presenter *p, const char *why, bool emit_always)
{
    if (!p->have_out)
        return;
    uint32_t ids[16];
    int n = rb_output_connected(&p->out, ids, 16);
    bool changed = false;
    for (int i = 0; i < p->nheads; i++) {
        bool still = false;
        for (int j = 0; j < n; j++)
            still |= ids[j] == p->heads[i].conn_id;
        if (!still || rb_output_reprobe(&p->heads[i]) <= 0) {
            remove_head(p, i--, "disconnected");
            changed = true;
        } else if (p->heads[i].xf.k == 0) {
            int w = p->heads[i].w;
            fit_mirror_mode(p, &p->heads[i]); /* the mode list may have grown */
            changed |= p->heads[i].w != w;
        }
    }
    for (int j = 0; j < n && p->nheads < MAX_HEADS; j++) {
        bool known = ids[j] == p->out.conn_id || conn_failed(p, ids[j]);
        for (int i = 0; i < p->nheads; i++)
            known |= ids[j] == p->heads[i].conn_id;
        if (known)
            continue;
        uint32_t busy[MAX_HEADS + 1];
        int nb = 0;
        busy[nb++] = p->out.crtc_id;
        for (int i = 0; i < p->nheads; i++)
            busy[nb++] = p->heads[i].crtc_id;
        struct rb_output *h = &p->heads[p->nheads];
        char err[256];
        if (rb_output_open_head(h, &p->out, ids[j], busy, nb, err, sizeof err) < 0) {
            rb_log("display: mirror: %s", err);
            continue;
        }
        if (dsi_left_off(h, why) || dsi_stalled(h, why)) {
            mark_failed(p, h, STALLED_WHY);
            rb_output_abandon(h);
            memset(h, 0, sizeof *h);
            h->fd = -1;
            changed = true;
            continue;
        }
        p->nheads++;
        rb_xform_set_src_scale(&h->xf, p->s);
        rb_output_set_frame(h, p->W, p->H, true);
        fit_mirror_mode(p, h);
        rb_output_set_border(h, RB_BORDER_BLACK, p->cs, rb_checker_anchor);
        tl_add(p, "mirror_added", "%s %dx%d@%d (%s) frame %dx%d at %dx scale, image %d,%d %dx%d (%s)", h->conn_name,
               h->w, h->h, h->refresh, h->mode_source, p->W, p->H, h->xf.k, h->xf.img.x, h->xf.img.y, h->xf.img.w,
               h->xf.img.h, why);
        changed = true;
    }
    update_borders(p);
    if (changed)
        p->nedid = 0;
    if (elect_primary(p, why))
        return; /* display_changed emitted the display event */
    if (changed || emit_always)
        emit_display(p);
}

static int open_output(struct presenter *p, const char *node, const char *conn)
{
    char err[256];
    const int *want = p->host_mode[0] ? p->host_mode : NULL;
    if (!conn)
        conn = p->prefer_external ? "external" : "internal";
    if (rb_output_open(&p->out, node, conn, want, err, sizeof err) < 0) {
        rb_log("display: %s", err);
        if (strstr(err, "cannot become DRM master"))
            p->master_busy = true;
        return -1;
    }
    uint32_t ids[16];
    if (rb_output_connected(&p->out, ids, 16) > 1 && (dsi_left_off(&p->out, "acquired") || dsi_stalled(&p->out, "acquired"))) {
        /* Another connector can show the frame: never commit on this one. */
        mark_failed(p, &p->out, STALLED_WHY);
        rb_output_abandon(&p->out);
        if (rb_output_open(&p->out, node, "external", want, err, sizeof err) < 0) {
            rb_log("display: %s", err);
            return -1;
        }
    }
    p->have_out = true;
    p->drm_watch = rb_loop_watch(p->loop, p->out.fd, EPOLLIN, drm_ready, p);
    display_changed(p, "acquired");
    sync_heads(p, "acquired", false);
    return 0;
}

static void scan_outputs(struct presenter *p)
{
    if (p->have_out)
        return;
    if (p->opts->device) {
        open_output(p, p->opts->device, p->opts->connector);
        return;
    }
    char cards[RB_MAX_CARDS][64];
    int n = rb_drm_list_cards_for_primary(cards, RB_MAX_CARDS, p->prefer_external);
    for (int i = 0; i < n && !p->have_out; i++)
        open_output(p, cards[i], p->opts->connector);
}

/* Pi 5 panels and HDMI occupy different cards, so same-card mirror election
 * cannot change between them. Acquire the preferred card before releasing the
 * current one; a failed acquisition keeps the existing usable display. */
static bool elect_pi_card(struct presenter *p, const char *why)
{
    if (!p->have_out || p->opts->device || p->opts->connector || wants_primary(p, &p->out))
        return false;
    if (!rb_driver_rpi_panel(p->out.driver) && strncmp(p->out.driver, "vc4", 3))
        return false;
    char cards[RB_MAX_CARDS][64];
    int n = rb_drm_list_cards_for_primary(cards, RB_MAX_CARDS, p->prefer_external);
    if (n < 1 || !strcmp(cards[0], p->out.devnode))
        return false;
    struct rb_output next;
    char err[256];
    const int *mode = p->host_mode[0] ? p->host_mode : NULL;
    if (rb_output_open(&next, cards[0], p->prefer_external ? "external" : "internal", mode, err, sizeof err) < 0)
        return false;
    if (!wants_primary(p, &next)) {
        rb_output_close(&next);
        return false;
    }
    drain_flips(p);
    close_output(p, why);
    p->out = next;
    p->have_out = true;
    p->drm_watch = rb_loop_watch(p->loop, p->out.fd, EPOLLIN, drm_ready, p);
    tl_add(p, "primary", "%s on %s is now primary (%s); separate DRM cards do not mirror",
           p->out.conn_name, p->out.devnode, why);
    display_changed(p, why);
    sync_heads(p, why, false);
    return true;
}

/* A splash still holds DRM master on a device (it kept it after an aborted
 * handoff, or took it before this fix): ask it for the display again, as at
 * start. "done" when this presenter now shows the frame (or the splash had no
 * device); otherwise the splash keeps showing its checkerboard. At most every
 * 5 s (hostboot report). */
static void retake_from_splash(struct presenter *p)
{
    uint64_t now = rb_mono_ms();
    if (p->last_retake_ms && now - p->last_retake_ms < 5000)
        return;
    p->last_retake_ms = now;
    struct rb_splash_handoff h;
    if (rb_splash_handoff_begin(&h, 300) != 0)
        return;
    if (h.devnode[0] && strcmp(h.devnode, "none"))
        open_output(p, h.devnode, h.connector[0] ? h.connector : p->opts->connector);
    if (!p->have_out)
        scan_outputs(p);
    if (p->have_out) {
        compose(p);
        present(p);
    }
    bool shown = p->have_out && p->out.on_screen;
    bool ok = rb_splash_handoff_complete(&h, shown);
    rb_splash_handoff_finish(&h, ok);
    tl_add(p, "handoff_late", "%s", shown ? "presenter took the display from the splash"
                                         : ok ? "splash had no display" : "splash keeps the display");
}

static void retry_timer_fn(void *data)
{
    struct presenter *p = data;
    if (!p->have_out) {
        p->master_busy = false;
        scan_outputs(p);
        if (!p->have_out && p->master_busy)
            retake_from_splash(p);
        if (!p->have_out)
            rb_timer_arm(p->retry_timer, 1000);
        return;
    }
    if (!rb_output_is_master(&p->out) && rb_output_set_master(&p->out) < 0) {
        rb_timer_arm(p->retry_timer, 250);
        return;
    }
    p->out.need_modeset = true;
    rb_output_damage_all(&p->out);
    for (int i = 0; i < p->nheads; i++) {
        p->heads[i].need_modeset = true;
        rb_output_damage_all(&p->heads[i]);
    }
}

static void udev_ready(void *data, int fd, uint32_t events)
{
    (void)fd;
    (void)events;
    struct presenter *p = data;
    struct udev_device *d;
    while ((d = udev_monitor_receive_device(p->mon))) {
        const char *action = udev_device_get_action(d);
        const char *sysname = udev_device_get_sysname(d);
        const char *node = udev_device_get_devnode(d);
        if (!action || !sysname || strncmp(sysname, "card", 4) || strchr(sysname, '-')) {
            udev_device_unref(d);
            continue;
        }
        rb_log("udev: %s %s", action, sysname);
        if (!strcmp(action, "remove")) {
            if (p->have_out && node && !strcmp(node, p->out.devnode))
                close_output(p, "removed");
            scan_outputs(p);
        } else if (!strcmp(action, "add")) {
            scan_outputs(p);
        } else if (!strcmp(action, "change")) {
            if (p->have_out && node && !strcmp(node, p->out.devnode)) {
                int rc = rb_output_reprobe(&p->out);
                if (rc == 0 && p->nheads > 0) {
                    /* The primary (an external display) went away: a mirror
                     * becomes primary, the old primary is released. */
                    drain_flips(p);
                    struct rb_output gone = p->out;
                    p->out = p->heads[0];
                    p->out.shared_fd = false;
                    p->heads[0] = gone;
                    p->heads[0].shared_fd = true;
                    remove_head(p, 0, "primary disconnected");
                    tl_add(p, "primary", "%s is now primary (previous primary disconnected)", p->out.conn_name);
                    display_changed(p, "hotplug");
                    sync_heads(p, "hotplug", false);
                } else if (rc <= 0) {
                    close_output(p, rc < 0 ? "device error" : "connector disconnected");
                    scan_outputs(p);
                } else {
                    sync_heads(p, "hotplug", true);
                }
            } else {
                scan_outputs(p);
            }
        }
        udev_device_unref(d);
    }
    elect_pi_card(p, "hotplug");
    if (!p->have_out && !rb_timer_armed(p->retry_timer))
        rb_timer_arm(p->retry_timer, 1000);
}

/* ------------------------------------------------------------------ */
/* guest                                                               */

static void clear_forwarded(struct presenter *p)
{
    memset(p->fwd, 0, sizeof p->fwd);
    p->guest_buttons = 0;
    memset(p->rkeys, 0, sizeof p->rkeys);
    memset(p->rfwd, 0, sizeof p->rfwd);
    p->rbuttons = 0;
    memset(p->qhold, 0, sizeof p->qhold);
}

/* One press for the guest while any source (local keyboard, remote viewer)
 * holds the key; the release goes out when the last holder lets go. */
static void guest_key(struct presenter *p, int q, bool down)
{
    if (q <= 0 || q >= RB_Q_COUNT)
        return;
    if (getenv("RB_INPUT_TRACE"))
        rb_log("RBINPUT guest-key us=%llu q=%d down=%d prior_holders=%u",
               (unsigned long long)rb_mono_us(), q, down, p->qhold[q]);
    if (down) {
        if (p->qhold[q]++ == 0)
            rb_guest_send_key(&p->guest, (uint32_t)q, true);
    } else if (p->qhold[q] > 0 && --p->qhold[q] == 0) {
        rb_guest_send_key(&p->guest, (uint32_t)q, false);
    }
}

static void release_guest_input(struct presenter *p, const char *why)
{
    bool any = p->guest_buttons != 0 || p->rbuttons != 0;
    for (size_t i = 0; i < sizeof p->fwd && !any; i++)
        any = p->fwd[i] != 0 || p->rfwd[i] != 0;
    if (rb_guest_connected(&p->guest))
        rb_guest_send_release_all(&p->guest);
    clear_forwarded(p);
    if (any)
        rb_log("input: released held guest keys/buttons (%s)", why);
}

/* Mac OS's own startup icon in the guest frame: enough pixels off the
 * startup background (the gray, or a built-in LCD's dither) in its 32 x 32
 * rectangle; *sum identifies the picture (stable = drawn). */
static bool guest_icon_drawn(struct presenter *p, uint32_t *sum)
{
    int gw = p->guest.width, gh = p->guest.height;
    int ix = (gw - RB_ICON_SIZE) / 2, iy = (gh - RB_ICON_SIZE) / 2;
    if (!p->guest.px || ix < 0 || iy < 0)
        return false;
    /* The startup background: the rectangle's corner (outside the icon's
     * silhouette), and the dither's other colour where there is one. */
    uint32_t bg[2];
    rb_startup_bg(p->guest.px, p->guest.stride_px, ix, iy, bg);
    int n = 0;
    uint32_t h = 2166136261u;
    for (int y = iy; y < iy + RB_ICON_SIZE; y++) {
        const uint32_t *row = p->guest.px + (size_t)y * p->guest.stride_px;
        for (int x = ix; x < ix + RB_ICON_SIZE; x++) {
            uint32_t c = row[x] & 0xffffffu;
            n += c != rb_startup_bg_at(bg, x - ix, y - iy);
            h = (h ^ c) * 16777619u;
        }
    }
    *sum = h;
    return n >= LAND_ICON_MIN_PX;
}

/* Mac OS's own Happy Mac, the first time a startup screen shows it: saved
 * (1x guest pixels, RBI) where hostd asked, so later startups and GRUB's
 * background draw the exact icon. Written only when it differs. */
static void capture_icon(struct presenter *p)
{
    static uint8_t rbi[RB_ICON_RBI_BYTES], old[RB_ICON_RBI_BYTES + 1];
    p->captured = true;
    cJSON *ev = new_event("startup_icon");
    cJSON_AddStringToObject(ev, "path", p->capture_path);
    if (rb_icon_capture_rbi(p->guest.px, p->guest.stride_px, p->guest.width, p->guest.height, rbi) < 0) {
        tl_add(p, "startup_icon", "Mac OS's startup icon is not the expected Happy Mac; not saved");
        cJSON_AddBoolToObject(ev, "valid", false); /* not "ok": hostd reads that as a reply */
        emit(p, ev);
        return;
    }
    size_t n = 0;
    FILE *f = fopen(p->capture_path, "rbe");
    if (f) {
        n = fread(old, 1, sizeof old, f);
        fclose(f);
    }
    bool same = n == sizeof rbi && !memcmp(old, rbi, sizeof rbi), written = false;
    if (!same) {
        char tmp[272];
        snprintf(tmp, sizeof tmp, "%s.tmp", p->capture_path);
        int fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644);
        if (fd >= 0) {
            written = write(fd, rbi, sizeof rbi) == (ssize_t)sizeof rbi && fsync(fd) == 0;
            close(fd);
            written = written && rename(tmp, p->capture_path) == 0;
            if (!written)
                unlink(tmp);
        }
    }
    tl_add(p, "startup_icon", "Mac OS's Happy Mac %s %s", same ? "unchanged at" : written ? "saved to" : "NOT saved to",
           p->capture_path);
    cJSON_AddBoolToObject(ev, "valid", true);
    cJSON_AddBoolToObject(ev, "saved", same || written);
    cJSON_AddBoolToObject(ev, "written", written);
    emit(p, ev);
}

static void fade_end(struct presenter *p, const char *why)
{
    if (!p->fading)
        return;
    p->fading = false;
    rb_timer_arm(p->fade_timer, 0);
    mark_dirty(p, p->fade_rect);
    free(p->fade_px);
    p->fade_px = NULL;
    tl_add(p, "fade", "%s after %llu ms", why, (unsigned long long)(rb_mono_ms() - p->fade_start));
}

static void fade_tick(struct presenter *p)
{
    if (!p->fading)
        return;
    if (p->view != VIEW_GUEST || !guest_visible_ok(p)) {
        fade_end(p, "interrupted");
        return;
    }
    if (rb_mono_ms() - p->fade_start >= FADE_MS) {
        fade_end(p, "done");
        return;
    }
    mark_dirty(p, p->fade_rect);
    rb_timer_arm(p->fade_timer, FADE_POLL_MS);
}

static void fade_timer_fn(void *data) { fade_tick(data); }

/* From what the shadow shows in `area` now to Mac OS's live frame. */
static void fade_begin_rect(struct presenter *p, rb_rect area)
{
    compose(p); /* the shadow holds the last indicator frame */
    rb_rect r;
    if (!p->shadow || !rb_rect_intersect(area, (rb_rect){0, 0, p->W, p->H}, &r))
        return;
    free(p->fade_px);
    p->fade_px = malloc((size_t)r.w * r.h * 4);
    if (!p->fade_px)
        return;
    for (int y = 0; y < r.h; y++)
        memcpy(p->fade_px + (size_t)y * r.w, p->shadow + (size_t)(r.y + y) * p->W + r.x, (size_t)r.w * 4);
    p->fade_rect = r;
    p->fading = true;
    p->fade_start = rb_mono_ms();
    rb_timer_arm(p->fade_timer, FADE_POLL_MS);
}

/* At the cut the icon's screen still shows the completed bar; it fades into
 * Mac OS's face, which is all that differs. */
static void fade_begin(struct presenter *p) { fade_begin_rect(p, rb_startup_screen_rect(&p->ind_cur)); }

static bool guest_icon_drawn(struct presenter *p, uint32_t *sum);

/* Mac OS's background at the corner of its icon's rectangle. */
static uint32_t guest_icon_corner(struct presenter *p)
{
    int ix = (p->guest.width - RB_ICON_SIZE) / 2, iy = (p->guest.height - RB_ICON_SIZE) / 2;
    return p->guest.px && ix >= 0 && iy >= 0 ? p->guest.px[(size_t)iy * p->guest.stride_px + ix] & 0xffffffu : 0;
}

static bool colour_near(uint32_t a, uint32_t b, int tol)
{
    for (int sh = 0; sh <= 16; sh += 8) {
        int d = (int)((a >> sh) & 0xffu) - (int)((b >> sh) & 0xffu);
        if (d > tol || d < -tol)
            return false;
    }
    return true;
}

/* happy_mac "late": Mac OS's gray is revealed; the completed indicator stays
 * where its Happy Mac will appear. */
static void overlay_begin(struct presenter *p)
{
    p->overlay = true;
    p->overlay_start = rb_mono_ms();
    p->overlay_bg = guest_icon_corner(p);
    p->overlay_seen = false;
    rb_timer_arm(p->overlay_timer, LAND_POLL_MS);
    mark_dirty(p, rb_startup_rect(&p->ind_cur));
}

/* The indicator leaves the revealed Mac OS: with `fade`, over FADE_MS from
 * what is on screen (the whole icon: into Mac OS's Happy Mac only the face
 * differs, into anything else it is a cross-fade); else at once. */
static void overlay_end(struct presenter *p, const char *why, bool fade)
{
    if (!p->overlay)
        return;
    rb_rect r = rb_startup_rect(&p->ind_cur);
    if (fade && p->view == VIEW_GUEST && !p->switch_hold)
        fade_begin_rect(p, r); /* composes first: the snapshot has the indicator */
    p->overlay = false;
    rb_timer_arm(p->overlay_timer, 0);
    mark_dirty(p, r);
    tl_add(p, "overlay", "%s after %llu ms%s", why, (unsigned long long)(rb_mono_ms() - p->overlay_start),
           p->fading ? "; the indicator fades" : "");
}

/* Ends at the first thing Mac OS draws in the icon's rectangle, once it is
 * unchanged over two refreshes: its Happy Mac (only the face differs, so the
 * bar fades into the smile as with a prompt one), or anything else on another
 * background (the indicator fades into it). Mac OS repainting its gray a few
 * levels lighter (the lab: just before the Happy Mac) does not count. At most
 * OVERLAY_MAX_MS. */
static void overlay_tick(struct presenter *p)
{
    if (!p->overlay)
        return;
    if (p->view != VIEW_GUEST || !guest_visible_ok(p) || p->switch_hold) {
        overlay_end(p, "interrupted", false);
        return;
    }
    uint64_t now = rb_mono_ms();
    uint32_t sum = 0;
    bool icon = guest_icon_drawn(p, &sum);
    if (icon || !colour_near(guest_icon_corner(p), p->overlay_bg, 24)) {
        if (p->overlay_seen && sum == p->overlay_sum && now - p->overlay_seen_mono >= 30) {
            overlay_end(p, icon ? "Mac OS drew its Happy Mac" : "Mac OS drew over the indicator", true);
            return;
        }
        if (!p->overlay_seen || sum != p->overlay_sum) {
            p->overlay_sum = sum;
            p->overlay_seen_mono = now;
        }
        p->overlay_seen = true;
    } else {
        p->overlay_seen = false;
    }
    if (now - p->overlay_start >= OVERLAY_MAX_MS) {
        overlay_end(p, "no Happy Mac in time", true);
        return;
    }
    rb_timer_arm(p->overlay_timer, LAND_POLL_MS);
}

static void overlay_timer_fn(void *data) { overlay_tick(data); }

static void land_end(struct presenter *p, const char *why)
{
    if (!p->landing)
        return;
    p->landing = false;
    rb_timer_arm(p->land_timer, 0);
    tl_add(p, "landing", "%s after %llu ms (steps %d, bar %d/%d, icon %s)", why,
           (unsigned long long)(rb_mono_ms() - p->land_start), p->land_steps, p->fill_shown, RB_BAR_STEPS,
           p->land_icon_seen ? "drawn" : "not seen");
}

static void land_tick(struct presenter *p)
{
    if (!p->landing)
        return;
    if (p->scene != RB_SCENE_GUEST || !guest_visible_ok(p) || p->view != VIEW_CHECKER) {
        land_end(p, "interrupted");
        p->revealed = p->scene == RB_SCENE_GUEST;
        update_view(p);
        return;
    }
    uint64_t now = rb_mono_ms(), t = now - p->land_start;
    ind_tick(p); /* the bar completes */
    if (p->land_steps > 0 && t >= LAND_RAMP_MS + LAND_FULL_MS) {
        int idx = (int)((t - LAND_RAMP_MS - LAND_FULL_MS) / (uint64_t)p->land_step_ms) + 1;
        int q = idx >= p->land_steps ? p->land.k : p->land_q_from - idx * p->land.k;
        if (q != p->ind_cur.q || p->ind_cur.x != p->land.x + (RB_ICON_SIZE / 2) * (p->land.k - q)) {
            mark_dirty(p, rb_startup_rect(&p->ind_cur));
            p->ind_cur = rb_startup_geom_at(&p->land, q);
            mark_dirty(p, rb_startup_rect(&p->ind_cur));
        }
    }
    bool shrunk = t >= LAND_RAMP_MS + LAND_FULL_MS + (uint64_t)p->land_steps * (uint64_t)p->land_step_ms;
    bool icon = p->land_rule != RB_CONTENT_GRAY;
    if (!icon) {
        uint32_t sum = 0;
        if (guest_icon_drawn(p, &sum)) {
            /* Drawn and unchanged over two refreshes: not caught mid-draw. */
            if (p->land_icon_seen && sum == p->land_sum && now - p->land_sum_mono >= 30)
                icon = true;
            else if (!p->land_icon_seen || sum != p->land_sum) {
                p->land_sum = sum;
                p->land_sum_mono = now;
            }
            p->land_icon_seen = true;
        }
        if (icon && !p->captured && p->capture_path[0])
            capture_icon(p);
    }
    if (p->icon_late && !icon && !p->land_icon_seen && shrunk && p->fill_shown >= RB_BAR_STEPS &&
        p->ind_cur.q == p->land.k && p->ind_cur.x == p->land.x && p->ind_cur.y == p->land.y) {
        /* Mac OS draws its Happy Mac later (happy_mac "late"): reveal its gray
         * now, the indicator stays where the Happy Mac will appear. */
        land_end(p, "revealed; the indicator waits for Mac OS's late Happy Mac");
        p->revealed = true;
        update_view(p);
        if (p->view == VIEW_GUEST)
            overlay_begin(p);
        return;
    }
    if ((shrunk && icon && p->fill_shown >= RB_BAR_STEPS) || t >= LAND_ICON_WAIT_MS) {
        land_end(p, icon ? "revealed" : "revealed without the startup icon");
        /* Mac OS's Happy Mac is where the indicator stands: only the screen
         * differs, and it fades from the bar to the face. */
        if (p->land_rule == RB_CONTENT_GRAY && icon && p->ind_cur.q == p->land.k && p->ind_cur.x == p->land.x &&
            p->ind_cur.y == p->land.y)
            fade_begin(p);
        p->revealed = true;
        update_view(p);
        return;
    }
    rb_timer_arm(p->land_timer, LAND_POLL_MS);
}

static void land_timer_fn(void *data) { land_tick(data); }

/* Genuine content behind the indicator: complete the bar (the light stays
 * on), and cut over once Mac OS has drawn its Happy Mac in the same place;
 * then the bar fades into the face. Should the guest's surface differ from
 * the target the indicator was laid out for, it moves there first. */
static void land_start(struct presenter *p, int rule)
{
    p->landing = true;
    p->land_rule = rule;
    p->land_start = rb_mono_ms();
    p->land_p0 = rb_progress_at(&p->prog, rb_boot_ms());
    if ((double)p->fill_shown / RB_BAR_STEPS > p->land_p0)
        p->land_p0 = (double)p->fill_shown / RB_BAR_STEPS;
    p->land_icon_seen = false;
    p->land_sum = 0;
    /* Where Mac OS's icon is for the surface it actually has. */
    p->land = p->ind_geom;
    if (p->layout.mode == RB_SCALING_INTEGER && p->layout.k >= 1) {
        struct rb_startup_geom g = p->ind_geom;
        g.k = p->layout.k;
        g.land_x = p->layout.dst.x + ((p->guest.width - RB_ICON_SIZE) / 2) * p->layout.k;
        g.land_y = p->layout.dst.y + ((p->guest.height - RB_ICON_SIZE) / 2) * p->layout.k;
        p->land = g;
    }
    p->land = rb_startup_geom_at(&p->land, p->land.k);
    p->land_q_from = p->ind_cur.q;
    int k = p->land.k;
    if (p->land_q_from > k && p->land_q_from % k == 0)
        p->land_steps = p->land_q_from / k - 1;
    else
        p->land_steps = p->land_q_from != k || p->ind_cur.x != p->land.x || p->ind_cur.y != p->land.y ? 1 : 0;
    p->land_step_ms = p->land_steps ? (LAND_STEPS_MAX_MS / p->land_steps < LAND_STEP_MS
                                           ? LAND_STEPS_MAX_MS / p->land_steps
                                           : LAND_STEP_MS)
                                    : LAND_STEP_MS;
    tl_add(p, "landing", "start: bar %d/%d (%.3f), indicator %dx at %d,%d -> Mac OS icon %dx at %d,%d (%d steps)",
           p->fill_shown, RB_BAR_STEPS, p->land_p0, p->ind_cur.q, p->ind_cur.x, p->ind_cur.y, k, p->land.x,
           p->land.y, p->land_steps);
    land_tick(p);
}

/* The indicator was not shown (off, or GRUB had none yet): still learn Mac
 * OS's Happy Mac from this startup screen, so the next startup has it. */
static void cap_tick(struct presenter *p)
{
    if (p->captured || !p->capture_path[0] || !p->guest.px || rb_mono_ms() - p->cap_start > LAND_ICON_WAIT_MS)
        return;
    uint32_t sum = 0;
    uint64_t now = rb_mono_ms();
    if (guest_icon_drawn(p, &sum)) {
        if (p->cap_seen && sum == p->cap_sum && now - p->cap_sum_mono >= 30) {
            capture_icon(p);
            return;
        }
        if (!p->cap_seen || sum != p->cap_sum) {
            p->cap_sum = sum;
            p->cap_sum_mono = now;
        }
        p->cap_seen = true;
    }
    rb_timer_arm(p->cap_timer, LAND_POLL_MS);
}

static void cap_timer_fn(void *data) { cap_tick(data); }

static void content_found(struct presenter *p, int rule)
{
    p->content_mono_ms = rb_mono_ms();
    uint64_t t = p->content_mono_ms - p->guest.connect_mono_ms;
    rb_timer_arm(p->content_timer, 0);
    tl_add(p, "guest_content", "rule %s gray %d%% (level %d) nonblack %d%% static %d ms settled %d ms t=%llu ms",
           rule == RB_CONTENT_GRAY ? "gray" : "nonblack", p->content.last_gray_pct, p->content.last_gray_level,
           p->content.last_nonblack_pct, p->content.last_static_ms, p->content.last_settled_ms,
           (unsigned long long)t);
    cJSON *ev = new_event("guest_content");
    cJSON_AddNumberToObject(ev, "t_ms", (double)t);
    cJSON_AddStringToObject(ev, "rule", rule == RB_CONTENT_GRAY ? "gray" : "nonblack");
    add_times(ev);
    emit(p, ev);
    if (p->scene == RB_SCENE_GUEST && p->reveal == RB_REVEAL_FIRST_CONTENT) {
        if (ind_visible(p) && guest_visible_ok(p) && !p->landing) {
            land_start(p, rule);
            schedule_resize_check(p);
            return;
        }
        p->revealed = true;
    }
    if (rule == RB_CONTENT_GRAY && !p->captured && p->capture_path[0]) {
        p->cap_start = rb_mono_ms();
        p->cap_seen = false;
        rb_timer_arm(p->cap_timer, LAND_POLL_MS);
    }
    update_view(p);
    schedule_resize_check(p);
}

static void inspect_content(struct presenter *p)
{
    if (p->content.detected || !p->guest.px)
        return;
    int rule = rb_content_inspect(&p->content, p->guest.px, p->guest.width, p->guest.height, p->guest.stride_px,
                                  rb_mono_ms());
    if (rule)
        content_found(p, rule);
}

static void content_timer_fn(void *data)
{
    struct presenter *p = data;
    inspect_content(p);
    if (!p->content.detected && rb_guest_connected(&p->guest))
        rb_timer_arm(p->content_timer, CONTENT_POLL_MS);
}

static void guest_connected(void *data)
{
    struct presenter *p = data;
    rb_content_reset(&p->content);
    p->revealed = false;
    p->reveal = RB_REVEAL_FIRST_CONTENT;
    p->sent_refresh = 0;
    p->resize_attempts = 0;
    p->damage_seq = 0;
    p->last_damage_mono = 0;
    p->pointer_sent = false;
    p->relative_pointer = false;
    p->relative_remainder = (struct rb_pointer_delta){0};
    p->surface_seen = false;
    p->retarget_pending = false;
    /* Mac OS starts with its cursor at (16,16) (observed with the virtio
     * tablet before any input); relative motion and pointer nudges start
     * from there instead of wherever the previous session left it. */
    p->gx = 16;
    p->gy = 16;
    clear_forwarded(p);
    update_view(p);
    send_refresh_rate(p);
    /* The connect-time RESIZE is a no-op for the size (hostd starts QEMU at
     * the target with -g); it confirms the target to the video driver. It
     * does not affect the "Apple Audio Extension - illegal instruction"
     * startup error, which comes from OpenBIOS's truncated sound-objects
     * property (emulator/reports/restart-crash-20261011.md). */
    send_resize(p, "connect");
    rb_timer_arm(p->content_timer, CONTENT_POLL_MS);
    tl_add(p, "guest_connected", "pid %u session '%s'", p->guest.pid, p->guest.session);
    cJSON *ev = new_event("guest_connected");
    guest_fill(p, ev);
    add_times(ev);
    emit(p, ev);
}

static void guest_disconnected(void *data, const char *reason)
{
    struct presenter *p = data;
    p->relative_pointer = false;
    p->relative_remainder = (struct rb_pointer_delta){0};
    rb_timer_arm(p->content_timer, 0);
    rb_timer_arm(p->resize_timer, 0);
    rb_timer_arm(p->nudge_timer, 0);
    land_end(p, "guest disconnected");
    overlay_end(p, "guest disconnected", false);
    fade_end(p, "guest disconnected");
    rb_timer_arm(p->cap_timer, 0);
    clear_forwarded(p);
    /* Keep Mac OS's last frame (its shutdown/restart screen) until hostd
     * picks the next scene: black before poweroff, splash for a relaunch. */
    if (p->view == VIEW_GUEST || p->view == VIEW_HOLD)
        p->hold = true;
    p->revealed = false;
    p->reveal = RB_REVEAL_FIRST_CONTENT;
    p->layout_ok = false;
    p->layout_deferred = false;
    rb_timer_arm(p->relayout_timer, 0);
    bool turn = p->rot_deferred; /* Mac OS went before it switched: turn now */
    p->rot_deferred = false;
    p->retarget_pending = false;
    end_switch_hold(p, "guest disconnected");
    rb_layout_free(&p->layout);
    rb_content_reset(&p->content);
    update_view(p);
    if (turn)
        rotation_now(p, "guest disconnected");
    tl_add(p, "guest_disconnected", "%s%s", reason, p->view == VIEW_HOLD ? "; holding the last frame" : "");
    cJSON *ev = new_event("guest_disconnected");
    guest_fill(p, ev);
    cJSON_AddStringToObject(ev, "reason", reason);
    add_times(ev);
    emit(p, ev);
}

/* Lab (--record-guest): the guest's frames as they arrive, on the clock of
 * the presenter's own frame log. */
static void guest_log_frame(struct presenter *p, const rb_rect *rects, int n, bool surface)
{
    if (!p->opts->record_guest || !p->opts->record_guest[0] || p->guest_log_failed || !p->guest.px)
        return;
    if (!p->guest_log.f) {
        if (rb_framelog_open(&p->guest_log, p->opts->record_guest, p->guest.width, p->guest.height) < 0) {
            rb_log("cannot record guest frames to %s: %s", p->opts->record_guest, strerror(errno));
            p->guest_log_failed = true;
            return;
        }
    } else if (surface) {
        rb_framelog_resize(&p->guest_log, p->guest.width, p->guest.height);
    }
    rb_rect whole = {0, 0, p->guest.width, p->guest.height};
    rb_framelog_add(&p->guest_log, rb_mono_us(), p->guest.px, p->guest.stride_px, surface ? &whole : rects,
                    surface ? 1 : n);
}

static void guest_surface(void *data)
{
    struct presenter *p = data;
    p->damage_seq++;
    p->badge_scan = true;
    p->last_damage_mono = rb_mono_ms();
    guest_log_frame(p, NULL, 0, true);
    bool switching = (p->layout_deferred || p->rot_deferred) && p->view == VIEW_GUEST && p->content.detected;
    /* Mac OS switched by itself while it is shown: the new surface holds
     * whatever video memory had (the old picture at the new stride, black,
     * Mac OS's clear), so the last good frame stays until it is complete. */
    bool guest_switch = !switching && p->surface_seen && p->view == VIEW_GUEST && p->revealed &&
                        p->content.detected && !p->landing;
    int from_w = p->surface_w, from_h = p->surface_h;
    p->surface_w = p->guest.width;
    p->surface_h = p->guest.height;
    if (!p->surface_seen) {
        /* QEMU's -g came from hostd before a turn (or a display change) that
         * the guest has not caught up with: re-request once Mac OS is up */
        p->surface_seen = true;
        if (p->target_w && (p->guest.width != p->target_w || p->guest.height != p->target_h))
            p->retarget_pending = true;
    }
    relayout(p);
    if (switching) {
        p->switch_hold = true;
        p->switch_guest = false;
        p->switch_hold_mono = p->switch_best_mono = rb_mono_ms();
        p->switch_best_nonblack = 0;
        rb_content_reset(&p->switch_content);
        rb_timer_arm(p->switch_timer, SWITCH_POLL_MS);
    } else if (guest_switch) {
        uint64_t now = rb_mono_ms();
        bool again = p->switch_hold && p->switch_guest; /* another switch while holding: hold on */
        fade_end(p, "guest mode switch");
        overlay_end(p, "guest mode switch", false);
        if (!p->switch_hold || !p->switch_guest)
            p->switch_hold_mono = now;
        p->switch_hold = p->switch_guest = true;
        rb_switch_hold_begin(&p->guest_switch, p->guest.px, p->guest.width, p->guest.height, p->guest.stride_px,
                             p->switch_hold_mono);
        rb_timer_arm(p->switch_timer, SWITCH_POLL_MS);
        tl_add(p, "switch_hold", "Mac OS %dx%d -> %dx%d: the last frame stays until the new one is drawn%s", from_w,
               from_h, p->guest.width, p->guest.height, again ? " (again)" : "");
    } else {
        end_switch_hold(p, "new surface");
    }
    if (p->gx >= p->guest.width)
        p->gx = p->guest.width - 1;
    if (p->gy >= p->guest.height)
        p->gy = p->guest.height - 1;
    tl_add(p, "guest_surface", "%dx%d scaling %s k=%d dst %d,%d %dx%d", p->guest.width, p->guest.height,
           rb_scaling_name(p->layout.mode), p->layout.k, p->layout.dst.x, p->layout.dst.y, p->layout.dst.w,
           p->layout.dst.h);
    cJSON *ev = new_event("guest_surface");
    int sz[2] = {p->guest.width, p->guest.height};
    cJSON_AddItemToObject(ev, "size", int_array(2, sz));
    emit(p, ev);
    inspect_content(p);
    if (p->rot_deferred && !p->switch_hold) {
        rotation_now(p, "rotated guest surface");
        return;
    }
    if (p->view == VIEW_GUEST)
        mark_all(p);
    update_view(p);
    schedule_resize_check(p);
}

static void guest_damage(void *data, const rb_rect *rects, int n)
{
    struct presenter *p = data;
    p->damage_seq++;
    if (getenv("RB_PRESENT_TRACE") || getenv("RB_PRESENT_TRACE_ROW"))
        fprintf(stderr, "RBPRESENT {\"stage\":\"damage\",\"us\":%llu,\"guest_pid\":%u,\"seq\":%u,\"rects\":%d}\n",
                (unsigned long long)rb_mono_us(), p->guest.pid, p->guest.last_seq, n);
    p->last_damage_mono = rb_mono_ms();
    guest_log_frame(p, rects, n, false);
    rb_remote_damage(&p->remote, RB_REMOTE_GUEST, rects, n);
    if (p->badge_on && !rb_timer_armed(p->badge_timer))
        for (int i = 0; i < n; i++)
            if (rects[i].y < RB_BADGE_MENU_H) {
                rb_timer_arm(p->badge_timer, 300);
                break;
            }
    if (!p->content.detected)
        inspect_content(p);
    if (p->view != VIEW_GUEST)
        return;
    if (p->switch_hold) {
        check_switch_hold(p);
        return; /* still holding: nothing to draw; the release redraws everything */
    }
    if (n && !p->damage_pending_us)
        p->damage_pending_us = rb_mono_us();
    for (int i = 0; i < n; i++)
        mark_dirty(p, rb_layout_map_damage(&p->layout, rects[i]));
}

static void guest_event(void *data, const char *json, size_t len)
{
    struct presenter *p = data;
    cJSON *obj = cJSON_ParseWithLength(json, len);
    if (!obj) {
        rb_log("guest: GUEST_EVENT with invalid JSON ignored");
        return;
    }
    const cJSON *event = cJSON_GetObjectItemCaseSensitive(obj, "event");
    const cJSON *relative = cJSON_GetObjectItemCaseSensitive(obj, "relative");
    if (cJSON_IsString(event) && !strcmp(event->valuestring, "pointer-mode") && cJSON_IsBool(relative)) {
        p->relative_pointer = cJSON_IsTrue(relative);
        p->relative_remainder = (struct rb_pointer_delta){0};
    }
    char *s = cJSON_PrintUnformatted(obj);
    tl_add(p, "guest_event", "%.100s", s ? s : "?");
    cJSON_free(s);
    cJSON *ev = new_event("guest_event");
    cJSON_AddItemToObject(ev, "data", obj);
    add_times(ev);
    emit(p, ev);
}

static void panel_tick(struct presenter *p);

/* ------------------------------------------------------------------ */
/* scenes                                                              */

static void set_scene(struct presenter *p, enum rb_scene scene, enum rb_reveal reveal, enum rb_panel_bg bg)
{
    enum rb_scene old = p->scene;
    if (old == RB_SCENE_GUEST && scene != RB_SCENE_GUEST)
        release_guest_input(p, "scene change");
    /* Any scene choice ends a held last frame (VIEW_HOLD) and a landing. */
    p->hold = false;
    end_switch_hold(p, "scene");
    if (p->landing) {
        land_end(p, "scene change");
        if (scene == RB_SCENE_GUEST)
            p->revealed = true;
    }
    overlay_end(p, "scene change", false);
    fade_end(p, "scene change");
    p->scene = scene;
    if (scene == RB_SCENE_GUEST) {
        p->reveal = reveal;
        p->revealed = reveal == RB_REVEAL_NOW || p->content.detected;
    }
    if (scene == RB_SCENE_PANEL) {
        bool bg_changed = p->panel_bg != bg || old != RB_SCENE_PANEL;
        p->panel_bg = bg;
        if (bg == RB_PANEL_BG_GUEST_DIMMED && bg_changed)
            render_dimmed_bg(p);
        p->panel_force = true;
        if (p->ui)
            rbui_pointer(p->ui, (int)(p->px / p->s), (int)(p->py / p->s), p->panel_buttons);
    }
    if (scene != RB_SCENE_PANEL)
        rb_timer_arm(p->repeat_timer, 0);
    panel_tick(p);
    send_refresh_rate(p);
    if (old != scene)
        tl_add(p, "scene", "%s -> %s%s", rb_scene_name(old), rb_scene_name(scene),
               scene == RB_SCENE_GUEST ? (reveal == RB_REVEAL_NOW ? " (reveal now)" : " (reveal first-content)")
                                       : "");
    update_view(p);
    if (p->view == VIEW_PANEL)
        mark_all(p);
}

/* ------------------------------------------------------------------ */
/* power fallback                                                      */

static void power_timer_fn(void *data)
{
    struct presenter *p = data;
    rb_log("power: systemctl did not finish; forcing %s", p->power_cmd == RB_POWER_OFF ? "poweroff" : "reboot");
    sync();
    reboot(p->power_cmd);
}

static void power_action(struct presenter *p, bool poweroff)
{
    tl_add(p, "power", "fallback alert: %s", poweroff ? "Shut Down" : "Restart");
    p->fallback = false;
    set_scene(p, RB_SCENE_BLACK, RB_REVEAL_NOW, RB_PANEL_BG_BLACK);
    compose(p);
    present(p);
    char *argv[] = {"systemctl", poweroff ? "poweroff" : "reboot", NULL};
    pid_t pid;
    p->power_cmd = poweroff ? RB_POWER_OFF : RB_AUTOBOOT;
    posix_spawnattr_t attr;
    posix_spawnattr_init(&attr);
    sigset_t none;
    sigemptyset(&none);
    posix_spawnattr_setsigmask(&attr, &none);
    posix_spawnattr_setflags(&attr, POSIX_SPAWN_SETSIGMASK);
    int rc = posix_spawnp(&pid, "systemctl", NULL, &attr, argv, environ);
    posix_spawnattr_destroy(&attr);
    if (rc != 0) {
        rb_log("power: cannot spawn systemctl: %s", strerror(rc));
        rb_timer_arm(p->power_timer, 2000);
        return;
    }
    p->power_pid = pid;
    rb_timer_arm(p->power_timer, 30000);
}

static void show_fallback(struct presenter *p)
{
    if (!p->ui)
        return;
    const char *doc = rbui_builtin_doc("hostd-lost");
    if (!doc)
        doc = fallback_doc;
    char err[200] = "";
    if (rbui_set_doc(p->ui, doc, strlen(doc), err, sizeof err) < 0 && doc != fallback_doc)
        rbui_set_doc(p->ui, fallback_doc, strlen(fallback_doc), err, sizeof err);
    p->fallback = true;
    p->panel_doc = true;
    tl_add(p, "fallback", "hostd unreachable; showing built-in restart alert");
    set_scene(p, RB_SCENE_PANEL, RB_REVEAL_NOW, RB_PANEL_BG_CHECKERBOARD);
}

static void ui_emit(void *ctx, const char *json)
{
    struct presenter *p = ctx;
    cJSON *ev = cJSON_Parse(json);
    if (!ev)
        return;
    const cJSON *doc = cJSON_GetObjectItemCaseSensitive(ev, "doc");
    const cJSON *id = cJSON_GetObjectItemCaseSensitive(ev, "id");
    const cJSON *kind = cJSON_GetObjectItemCaseSensitive(ev, "event");
    if (p->fallback && cJSON_IsString(doc) && !strcmp(doc->valuestring, FALLBACK_DOC_ID)) {
        if (cJSON_IsString(kind) && !strcmp(kind->valuestring, "ui_action") && cJSON_IsString(id)) {
            if (!strcmp(id->valuestring, "restart"))
                power_action(p, false);
            else if (!strcmp(id->valuestring, "shutdown"))
                power_action(p, true);
        }
        cJSON_Delete(ev);
        return;
    }
    if (!cJSON_GetObjectItemCaseSensitive(ev, "event"))
        cJSON_AddStringToObject(ev, "event", "ui_action");
    emit(p, ev);
}

/* ------------------------------------------------------------------ */
/* hostd / chord                                                       */

static void deliver_chord(struct presenter *p)
{
    p->chord_pending = false;
    cJSON *ev = new_event("chord");
    cJSON_AddStringToObject(ev, "name", "recovery");
    add_times(ev);
    emit(p, ev);
    p->chord_wait_ack = true;
    rb_timer_arm(p->chord_timer, HOSTD_GRACE_MS);
}

static void chord_timer_fn(void *data)
{
    struct presenter *p = data;
    if (p->chord_pending && !rb_ctl_connected(&p->ctl)) {
        p->chord_pending = false;
        show_fallback(p);
    } else if (p->chord_wait_ack) {
        p->chord_wait_ack = false;
        rb_log("chord: hostd did not respond within %d ms", HOSTD_GRACE_MS);
        show_fallback(p);
    }
}

static void on_chord(struct presenter *p)
{
    tl_add(p, "chord", "recovery chord (%s)", rb_chord_name(p->chord_key));
    if (rb_ctl_connected(&p->ctl)) {
        deliver_chord(p);
        return;
    }
    uint64_t absent = rb_mono_ms() - p->hostd_absent_since;
    if (absent >= HOSTD_GRACE_MS) {
        show_fallback(p);
        return;
    }
    p->chord_pending = true;
    rb_timer_arm(p->chord_timer, HOSTD_GRACE_MS - absent);
}

static bool diag_active(const struct presenter *p);

/* Escape held since before the keyboard was opened (held through the
 * checkerboard, or from power-on on keyboards with typematic repeat) counts as
 * one press in the splash scene. Reported when hostd connects, so it reaches
 * hostd before it decides to start Mac OS; its release is swallowed. */
static void held_escape_check(struct presenter *p)
{
    if (!rb_input_take_escape_held(p->input))
        return;
    bool preboot = p->view == VIEW_CHECKER && (p->scene == RB_SCENE_SPLASH || p->scene == RB_SCENE_GUEST);
    if (!preboot)
        return;
    bit_set(p->suppress, KEY_ESC, true);
    if (diag_active(p)) /* Escape is Startup Details' (rb-bootdiag saw the press) */
        return;
    tl_add(p, "preboot_key", "escape (held before input opened)");
    cJSON *ev = new_event("preboot_key");
    cJSON_AddStringToObject(ev, "key", "escape");
    cJSON_AddBoolToObject(ev, "held", true);
    add_times(ev);
    emit(p, ev);
}

static void ctl_connected(void *data)
{
    struct presenter *p = data;
    tl_add(p, "hostd", "connected");
    if (p->chord_pending)
        deliver_chord(p);
    held_escape_check(p);
}

static void ctl_disconnected(void *data)
{
    struct presenter *p = data;
    p->hostd_absent_since = rb_mono_ms();
    p->chord_wait_ack = false;
    tl_add(p, "hostd_lost", "hostd disconnected; keeping scene %s", rb_scene_name(p->scene));
}

/* ------------------------------------------------------------------ */
/* input routing                                                       */

static void touch_input(struct presenter *p)
{
    p->last_input_mono = rb_mono_ms();
    p->input_events++;
    if (p->diag.cfg.enabled && !p->diag_input_ms)
        p->diag_input_ms = rb_boot_ms(); /* published for rb-bootdiag (diag_publish) */
}

/* ------------------------------------------------------------------ */
/* Startup Details (diagview.h, ARCHITECTURE 16)                       */

/* rb-bootdiag is running (it collects the details; the view is only useful then). */
static bool diag_active(const struct presenter *p)
{
    if (!p->diag.cfg.enabled)
        return false;
    char buf[32];
    if (rb_diag_read_file(RB_DIAG_DIR "/pid", buf, sizeof buf) <= 0)
        return false;
    int pid = atoi(buf);
    return pid > 1 && (kill(pid, 0) == 0 || errno == EPERM);
}

static void diag_show(struct presenter *p, bool on, const char *how)
{
    if (on == p->diag.visible)
        return;
    rb_diag_set_visible(on);
    p->diag.visible = on;
    if (on) {
        release_guest_input(p, "startup details");
        rb_timer_arm(p->repeat_timer, 0);
        osd_hide(p, false);
    }
    tl_add(p, "startup_details", "%s (%s)", on ? "shown" : "hidden", how);
    if (!on && p->view == VIEW_PANEL)
        p->panel_force = true;
    mark_all(p);
}

/* While the view is up: Escape hides it; on an installed system still on the
 * checkerboard Return hides it and asks for the startup options (preboot_key,
 * as Escape does there without Startup Details); everything else is
 * swallowed. Returns true when the key was taken. */
static bool diag_key(struct presenter *p, uint32_t code, bool down, bool preboot)
{
    if (p->diag.visible) {
        if (code == KEY_ESC && down) {
            bit_set(p->suppress, code, true);
            diag_show(p, false, "escape");
        } else if ((code == KEY_ENTER || code == KEY_KPENTER) && down &&
                   rb_diag_return_opens_options(p->diag.cfg.installer, true, on_checker_preboot(p))) {
            bit_set(p->suppress, code, true);
            diag_show(p, false, "return");
            tl_add(p, "preboot_key", "return (startup details)");
            cJSON *ev = new_event("preboot_key");
            cJSON_AddStringToObject(ev, "key", "return");
            cJSON_AddStringToObject(ev, "source", "startup details");
            add_times(ev);
            emit(p, ev);
        } else if (!down) {
            bit_set(p->suppress, code, false);
        }
        return true;
    }
    if (code != KEY_ESC || !diag_active(p))
        return false;
    if (rb_diag_escape(true, false, preboot, p->have_out && p->out.on_screen) != RB_DIAG_ESC_SHOW)
        return false;
    if (down) {
        bit_set(p->suppress, code, true);
        diag_show(p, true, "escape");
    }
    return true;
}

static void diag_ready(void *data, int fd, uint32_t events)
{
    (void)fd;
    (void)events;
    struct presenter *p = data;
    bool was = p->diag.visible;
    int chg = rb_diag_watch_update(&p->diag);
    if ((chg & RB_DIAG_CHG_VISIBLE) && p->diag.visible != was) {
        if (p->diag.visible)
            release_guest_input(p, "startup details");
        tl_add(p, "startup_details", "%s (rb-bootdiag)", p->diag.visible ? "shown" : "hidden");
        if (!p->diag.visible && p->view == VIEW_PANEL)
            p->panel_force = true;
        mark_all(p);
    }
    if ((chg & RB_DIAG_CHG_HINT) && p->view == VIEW_CHECKER)
        mark_dirty(p, rb_diag_hint_rect(p->W, p->H, p->s));
}

/* Every second while rb-bootdiag runs: page flips that have not completed
 * after 1 s (a panel stuck in self refresh: the screen shows the frame before
 * it, the checkerboard), published for its report, and the heartbeat that
 * tells it this process still runs its loop. */
static void diag_timer_fn(void *data)
{
    struct presenter *p = data;
    if (!diag_active(p))
        return; /* nobody collects it any more */
    uint64_t now = rb_mono_ms();
    struct rb_diag_flips f;
    memset(&f, 0, sizeof f);
    for (int i = -1; i < p->nheads; i++) {
        struct rb_output *o = i < 0 ? (p->have_out ? &p->out : NULL) : &p->heads[i];
        if (!o)
            continue;
        if (rb_output_check_stall(o, now, 1000))
            tl_add(p, "flip_stall", "%s: page flip pending for %llu ms", o->conn_name,
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
    if ((f.stalls || f.slow_commits) && memcmp(&f, &p->diag_flips, sizeof f)) {
        rb_diag_publish_flips("presenter", &f);
        p->diag_flips = f;
    }
    rb_diag_heartbeat();
    rb_timer_arm(p->diag_timer, RB_DIAG_HEARTBEAT_MS);
}

/* The presenter's state for rb-bootdiag (written only when it changed). */
static void diag_publish(struct presenter *p)
{
    if (!p->diag.cfg.enabled)
        return;
    char buf[512];
    snprintf(buf, sizeof buf,
             "pid=%d\ninput=%d\ndisplay=%d\ndriver=%s\ndevice=%s\nmode=%dx%d@%d\nscene=%s\nview=%s\nhostd=%d\n"
             "visible=%d\nstart_ms=%llu\nview_ms=%llu\ninput_ms=%llu\n",
             (int)getpid(), p->input != NULL, p->have_out && p->out.on_screen, p->have_out ? p->out.driver : "",
             p->have_out ? p->out.devnode : "", p->have_out ? p->out.w : 0, p->have_out ? p->out.h : 0,
             p->have_out ? p->out.refresh : 0, rb_scene_name(p->scene), view_name(p->view), rb_ctl_connected(&p->ctl),
             p->diag.visible, (unsigned long long)p->diag_start_ms, (unsigned long long)p->diag_view_ms,
             (unsigned long long)p->diag_input_ms);
    if (strcmp(buf, p->diag_pub) && rb_diag_write_atomic(RB_DIAG_PRESENTER, buf, strlen(buf)) == 0)
        snprintf(p->diag_pub, sizeof p->diag_pub, "%s", buf);
}

static bool guest_routing(struct presenter *p)
{
    return p->scene == RB_SCENE_GUEST && p->guest_input && rb_guest_connected(&p->guest);
}

static void panel_key(struct presenter *p, int rbkey, const char *utf8, bool down, unsigned mods)
{
    if (!p->ui)
        return;
    panel_tick(p);
    rbui_key(p->ui, rbkey, utf8, down, mods);
}

static void repeat_timer_fn(void *data)
{
    struct presenter *p = data;
    if (p->scene != RB_SCENE_PANEL || !bit_get(p->phys, p->repeat_code))
        return;
    panel_key(p, p->repeat_rbkey, p->repeat_utf8, true, p->repeat_mods);
    rb_timer_arm(p->repeat_timer, 40);
}

/* Keys the host handles (hostd: backlight, host volume); never sent to
 * Mac OS, in any scene. */
static const char *host_key_name(uint32_t code)
{
    switch (code) {
    case KEY_BRIGHTNESSUP: return "brightness_up";
    case KEY_BRIGHTNESSDOWN: return "brightness_down";
    case KEY_VOLUMEUP: return "volume_up";
    case KEY_VOLUMEDOWN: return "volume_down";
    case KEY_MUTE: return "mute";
    default: return NULL;
    }
}

static void emit_host_key(struct presenter *p, uint32_t code, bool repeat)
{
    cJSON *ev = new_event("host_key");
    cJSON_AddStringToObject(ev, "key", host_key_name(code));
    cJSON_AddBoolToObject(ev, "repeat", repeat);
    add_times(ev);
    emit(p, ev);
}

static void hostkey_timer_fn(void *data)
{
    struct presenter *p = data;
    if (!p->hostkey_code || !bit_get(p->phys, p->hostkey_code))
        return;
    emit_host_key(p, p->hostkey_code, true);
    rb_timer_arm(p->hostkey_timer, 120);
}

static void on_key(void *data, uint32_t code, bool down, uint32_t keysym, const char *utf8, unsigned mods)
{
    struct presenter *p = data;
    if (getenv("RB_INPUT_TRACE"))
        rb_log("RBINPUT local-key us=%llu evdev=%u down=%d prior_phys=%d prior_fwd=%d scene=%d route=%d",
               (unsigned long long)rb_mono_us(), code, down, bit_get(p->phys, code),
               bit_get(p->fwd, code), p->scene, guest_routing(p));
    touch_input(p);
    bit_set(p->phys, code, down);
    if (host_key_name(code)) {
        if (down) {
            tl_add(p, "host_key", "%s", host_key_name(code));
            emit_host_key(p, code, false);
            p->hostkey_code = code;
            if (strcmp(host_key_name(code), "mute"))
                rb_timer_arm(p->hostkey_timer, 400);
        } else if (code == p->hostkey_code) {
            rb_timer_arm(p->hostkey_timer, 0);
            p->hostkey_code = 0;
        }
        return;
    }
    if (code == KEY_POWER) {
        if (down) {
            tl_add(p, "power_button", "pressed");
            cJSON *ev = new_event("power_button");
            add_times(ev);
            emit(p, ev);
        }
        return;
    }
    bool ctrl = bit_get(p->phys, KEY_LEFTCTRL) || bit_get(p->phys, KEY_RIGHTCTRL);
    bool alt = bit_get(p->phys, KEY_LEFTALT) || bit_get(p->phys, KEY_RIGHTALT);
    bool chord_key = (int)code == p->chord_key;
    if (rb_input_is_gemini(p->input)) {
        /* Gemini PDA (gemkbd.h): its Alt arrives as KEY_LEFTMETA (Command), and its one
         * Backspace key is also Delete (with Fn): either one completes a Backspace or
         * Delete chord, so Ctrl+Alt+Backspace and Ctrl+Alt+Fn+Backspace both work. */
        alt = alt || bit_get(p->phys, KEY_LEFTMETA);
        if ((p->chord_key == KEY_BACKSPACE || p->chord_key == KEY_DELETE) &&
            (code == KEY_BACKSPACE || code == KEY_DELETE))
            chord_key = true;
    }
    if (p->diag.visible) { /* Startup Details: only Escape (it hides the view) */
        diag_key(p, code, down, false);
        return;
    }
    if (down && chord_key && ctrl && alt) {
        bit_set(p->suppress, code, true);
        release_guest_input(p, "recovery chord");
        on_chord(p);
        return;
    }
    if (!down && bit_get(p->suppress, code)) {
        bit_set(p->suppress, code, false);
        return;
    }
    bool preboot = on_checker_preboot(p);
    if (code == KEY_ESC && diag_key(p, code, down, preboot)) /* Startup Details' Escape */
        return;
    if (code == KEY_ESC && preboot) {
        if (down) {
            bit_set(p->suppress, code, true);
            tl_add(p, "preboot_key", "escape");
            cJSON *ev = new_event("preboot_key");
            cJSON_AddStringToObject(ev, "key", "escape");
            add_times(ev);
            emit(p, ev);
        }
        return;
    }
    if (p->scene == RB_SCENE_PANEL) {
        if (rb_evdev_is_modifier(code))
            return;
        char fb_utf8[8];
        int rbkey;
        const char *text = utf8;
        if (rb_input_has_xkb(p->input)) {
            rbkey = rb_keysym_to_rbui(keysym);
        } else {
            bool shift = bit_get(p->phys, KEY_LEFTSHIFT) || bit_get(p->phys, KEY_RIGHTSHIFT);
            rbkey = rb_evdev_to_rbui_fallback(code, shift, fb_utf8);
            text = fb_utf8;
            mods = (shift ? RBUI_MOD_SHIFT : 0) | (ctrl ? RBUI_MOD_CTRL : 0) | (alt ? RBUI_MOD_OPTION : 0);
        }
        if (!down) {
            if (code == p->repeat_code)
                rb_timer_arm(p->repeat_timer, 0);
            panel_key(p, rbkey, "", false, mods);
            return;
        }
        if (!rbkey && !(text && text[0]))
            return;
        panel_key(p, rbkey, text ? text : "", true, mods);
        p->repeat_code = code;
        p->repeat_rbkey = rbkey;
        snprintf(p->repeat_utf8, sizeof p->repeat_utf8, "%s", text ? text : "");
        p->repeat_mods = mods;
        rb_timer_arm(p->repeat_timer, 400);
        return;
    }
    if (!guest_routing(p))
        return;
    int q = rb_evdev_to_qcode(code);
    if (!q)
        return;
    if (p->swap)
        q = rb_qcode_swap_command_option(q);
    if (down) {
        if (!bit_get(p->fwd, code)) {
            bit_set(p->fwd, code, true);
            guest_key(p, q, true);
        }
    } else if (bit_get(p->fwd, code)) {
        bit_set(p->fwd, code, false);
        guest_key(p, q, false);
    }
}

static void clamp_guest_pointer(struct presenter *p)
{
    double maxx = p->guest.px ? p->guest.width - 1 : 0, maxy = p->guest.px ? p->guest.height - 1 : 0;
    if (p->gx < 0) p->gx = 0;
    if (p->gy < 0) p->gy = 0;
    if (p->gx > maxx) p->gx = maxx;
    if (p->gy > maxy) p->gy = maxy;
}

static void panel_pointer(struct presenter *p)
{
    if (p->scene == RB_SCENE_PANEL && p->ui) {
        panel_tick(p);
        rbui_pointer(p->ui, (int)(p->px / p->s), (int)(p->py / p->s), p->panel_buttons);
    }
}

static void on_motion(void *data, double dx, double dy)
{
    struct presenter *p = data;
    touch_input(p);
    if (p->diag.visible)
        return;
    p->px += dx;
    p->py += dy;
    if (p->px < 0) p->px = 0;
    if (p->py < 0) p->py = 0;
    if (p->px > p->W - 1) p->px = p->W - 1;
    if (p->py > p->H - 1) p->py = p->H - 1;
    if (p->layout_ok) {
        double ratio = rb_layout_ratio(&p->layout);
        p->gx += dx * p->speed / ratio;
        p->gy += dy * p->speed / ratio;
        clamp_guest_pointer(p);
        if (p->relative_pointer && guest_routing(p) && p->guest.px) {
            /* Physical mice/trackpads must reach InputSprocket as deltas.
             * Touch, web positioning and the local panel retain their own
             * absolute path. Send an earlier absolute event first. */
            send_pointer(p);
            int32_t x, y;
            if (rb_pointer_relative(&p->relative_remainder, dx, dy, p->speed, ratio, &x, &y))
                rb_guest_send_relative(&p->guest, x, y);
        } else {
            p->relative_remainder = (struct rb_pointer_delta){0};
            p->pointer_dirty = true;
        }
    }
    panel_pointer(p);
}

static void on_motion_abs(void *data, double x, double y)
{
    struct presenter *p = data;
    touch_input(p);
    if (p->diag.visible)
        return;
    /* Absolute devices (VNC/USB tablets) quantise to 0..32767; round so a
     * pointer at screen pixel N maps to N, not N-1. */
    x = round(x);
    y = round(y);
    p->px = x < 0 ? 0 : (x > p->W - 1 ? p->W - 1 : x);
    p->py = y < 0 ? 0 : (y > p->H - 1 ? p->H - 1 : y);
    if (p->layout_ok) {
        int gx, gy;
        rb_layout_screen_to_src(&p->layout, (int)lround(p->px), (int)lround(p->py), &gx, &gy);
        p->gx = gx;
        p->gy = gy;
        p->pointer_dirty = true;
    }
    panel_pointer(p);
}

/* Absolute pointers (tablets, KVM consoles) and touchscreens: a touchscreen
 * maps 1:1 to the internal panel's scanout; any other absolute device to the
 * external display when one is mirrored (that is where a KVM console looks),
 * else to the primary. The position is mapped through that output's
 * rotation, scale and letterbox to frame pixels. */
static void on_abs_norm(void *data, double nx, double ny, bool direct)
{
    struct presenter *p = data;
    if (!p->have_out)
        return;
    struct rb_output *o = &p->out;
    for (int i = 0; i < p->nheads; i++)
        if (direct ? p->heads[i].internal && !o->internal : !p->heads[i].internal) {
            o = &p->heads[i];
            break;
        }
    double fx = nx * o->w, fy = ny * o->h, x, y;
    rb_xform_fb_to_frame(&o->xf, fx, fy, &x, &y);
    on_motion_abs(p, x, y);
}

static void on_button(void *data, uint32_t code, bool down)
{
    struct presenter *p = data;
    touch_input(p);
    if (p->diag.visible)
        return;
    int b;
    switch (code) {
    case BTN_LEFT: b = 0; break;
    case BTN_MIDDLE: b = 1; break;
    case BTN_RIGHT: b = 2; break;
    default: return;
    }
    if (p->scene == RB_SCENE_PANEL) {
        unsigned bit = b == 0 ? 1u : (b == 2 ? 2u : 4u);
        if (down)
            p->panel_buttons |= bit;
        else
            p->panel_buttons &= ~bit;
        panel_pointer(p);
        return;
    }
    if (!guest_routing(p) || !p->guest.px)
        return;
    send_pointer(p);
    unsigned bit = 1u << b;
    if (down) {
        if (!(p->guest_buttons & bit)) {
            p->guest_buttons |= bit;
            if (!(p->rbuttons & bit))
                rb_guest_send_button(&p->guest, (uint32_t)b, true);
        }
    } else if (p->guest_buttons & bit) {
        p->guest_buttons &= ~bit;
        if (!(p->rbuttons & bit))
            rb_guest_send_button(&p->guest, (uint32_t)b, false);
    }
}

static void on_scroll(void *data, double v120)
{
    struct presenter *p = data;
    touch_input(p);
    if (p->diag.visible)
        return;
    if (p->scene == RB_SCENE_PANEL && p->ui) {
        p->wheel_acc += v120;
        int steps = (int)(p->wheel_acc / 120.0);
        if (steps) {
            p->wheel_acc -= steps * 120.0;
            panel_tick(p);
            rbui_wheel(p->ui, steps);
        }
        return;
    }
    if (!guest_routing(p) || !p->guest.px)
        return;
    p->wheel_acc += v120;
    send_pointer(p);
    while (p->wheel_acc >= 120.0 || p->wheel_acc <= -120.0) {
        uint32_t b = p->wheel_acc > 0 ? 4 : 3; /* positive = down */
        rb_guest_send_button(&p->guest, b, true);
        rb_guest_send_button(&p->guest, b, false);
        p->wheel_acc += p->wheel_acc > 0 ? -120.0 : 120.0;
    }
}

static void on_lid(void *data, bool open)
{
    struct presenter *p = data;
    tl_add(p, "lid", "%s", open ? "open" : "closed");
    cJSON *ev = new_event("lid");
    cJSON_AddBoolToObject(ev, "open", open);
    add_times(ev);
    emit(p, ev);
}

/* ------------------------------------------------------------------ */
/* touchscreens and pens (touch.h, 4.8)                                */

static struct rb_output *output_by_conn(struct presenter *p, uint32_t conn)
{
    if (!p->have_out)
        return NULL;
    if (p->out.conn_id == conn)
        return &p->out;
    for (int i = 0; i < p->nheads; i++)
        if (p->heads[i].conn_id == conn)
            return &p->heads[i];
    return NULL;
}

static void output_edid_name(struct presenter *p, const struct rb_output *o, char *out, size_t n)
{
    for (int i = 0; i < p->nedid; i++)
        if (p->edid[i].conn == o->conn_id) {
            snprintf(out, n, "%s", p->edid[i].name);
            return;
        }
    out[0] = 0;
    drmModeObjectProperties *props = drmModeObjectGetProperties(o->fd, o->conn_id, DRM_MODE_OBJECT_CONNECTOR);
    if (!props)
        return;
    for (uint32_t i = 0; i < props->count_props && !out[0]; i++) {
        drmModePropertyRes *pr = drmModeGetProperty(o->fd, props->props[i]);
        if (!pr)
            continue;
        if (!strcmp(pr->name, "EDID") && (pr->flags & DRM_MODE_PROP_BLOB) && props->prop_values[i]) {
            drmModePropertyBlobRes *b = drmModeGetPropertyBlob(o->fd, (uint32_t)props->prop_values[i]);
            if (b) {
                rb_edid_monitor_name(b->data, b->length, out, n);
                drmModeFreePropertyBlob(b);
            }
        }
        drmModeFreeProperty(pr);
    }
    drmModeFreeObjectProperties(props);
    if (p->nedid < MAX_HEADS + 1) {
        p->edid[p->nedid].conn = o->conn_id;
        size_t len = strnlen(out, sizeof p->edid[0].name - 1);
        memcpy(p->edid[p->nedid].name, out, len);
        p->edid[p->nedid].name[len] = 0;
        p->nedid++;
    }
}

/* The output a touchscreen or display tablet belongs to (rb_touch_pick_output):
 * udev's RETROBOOT_OUTPUT/WL_OUTPUT, an EDID name match, a USB touch monitor
 * with an external display, else the built-in panel or the primary. */
static struct rb_output *touch_output_for(struct presenter *p, const struct rb_input_dev *d)
{
    struct rb_output *outs[MAX_HEADS + 1];
    struct rb_touch_output to[MAX_HEADS + 1];
    char edid[MAX_HEADS + 1][24];
    int n = 0;
    outs[n++] = &p->out;
    for (int i = 0; i < p->nheads; i++)
        outs[n++] = &p->heads[i];
    for (int i = 0; i < n; i++) {
        output_edid_name(p, outs[i], edid[i], sizeof edid[i]);
        to[i] = (struct rb_touch_output){outs[i]->conn_name, edid[i], outs[i]->internal};
    }
    int i = rb_touch_pick_output(to, n, d->output, d->name, d->usb_external);
    return i >= 0 ? outs[i] : &p->out;
}

/* Frame pixel under a position on output o (0..1 of its scanout). */
static void touch_frame_point(struct rb_output *o, double nx, double ny, double *x, double *y)
{
    int sx, sy;
    rb_xform_fb_point_to_frame(&o->xf, nx * o->w, ny * o->h, &sx, &sy);
    *x = sx + 0.5;
    *y = sy + 0.5;
}

/* Gesture slop: 3 mm on the output (its physical size, else the DPI rule's
 * size for the primary), in frame pixels; 1 % of the frame when unknown. */
static double touch_slop(struct presenter *p, const struct rb_output *o)
{
    double px_per_mm = 0;
    if (o->mm_w > 0 && o->mm_h > 0)
        px_per_mm = (double)o->w / o->mm_w;
    else if (o == &p->out && p->mm_w > 0)
        px_per_mm = (double)p->W / p->mm_w;
    double k = o->xf.k > 0 ? o->xf.k : 1;
    double slop = px_per_mm > 0 ? 3.0 * px_per_mm / k : (p->W > p->H ? p->W : p->H) / 100.0;
    return slop < 4 ? 4 : (slop > 64 ? 64 : slop);
}

static void touch_arm(struct presenter *p)
{
    int ms = rb_gesture_tick(&p->gesture, rb_mono_ms());
    rb_timer_arm(p->touch_timer, ms > 0 ? ms : 0);
}

static void touch_timer_fn(void *data)
{
    touch_arm(data);
}

/* The checkerboard (Mac OS not shown yet): the touch way to Escape, the
 * startup options. */
static void touch_preboot(struct presenter *p, const char *how)
{
    tl_add(p, "preboot_key", "escape (%s)", how);
    cJSON *ev = new_event("preboot_key");
    cJSON_AddStringToObject(ev, "key", "escape");
    cJSON_AddStringToObject(ev, "source", how);
    add_times(ev);
    emit(p, ev);
}

static bool preboot_view(struct presenter *p)
{
    return on_checker_preboot(p);
}

static void g_point(void *data, double x, double y)
{
    on_motion_abs(data, floor(x), floor(y));
}

static void g_button(void *data, int b, bool down)
{
    struct presenter *p = data;
    if (preboot_view(p)) {
        /* nothing to click on the checkerboard: a long press (or two-finger
         * tap) opens the startup options, as Escape does */
        if (b == 2 && down)
            touch_preboot(p, "touch");
        return;
    }
    if (down) /* Gemini PDA: Fn held makes it an Option-click or Option-drag */
        rb_input_gemini_pointer(p->input, b, true);
    on_button(p, b == 0 ? BTN_LEFT : (b == 1 ? BTN_MIDDLE : BTN_RIGHT), down);
    if (!down)
        rb_input_gemini_pointer(p->input, b, false);
}

/* Two-finger scrolling: content follows the fingers, one wheel notch per
 * RB_TOUCH_NOTCH_PX Mac pixels (panel: logical pixels). */
static void g_pan(void *data, double dx, double dy)
{
    (void)dx;
    struct presenter *p = data;
    double k = 1;
    if (p->scene == RB_SCENE_PANEL)
        k = p->s;
    else if (p->layout_ok)
        k = rb_layout_ratio(&p->layout);
    on_scroll(p, rb_touch_pan_v120(dy, k));
}

static void on_touch(void *data, const struct rb_input_dev *d, enum rb_touch_kind kind, int slot, double nx,
                     double ny)
{
    struct presenter *p = data;
    if (!p->have_out || p->diag.visible)
        return;
    p->touch_events++;
    if (kind == RB_TOUCH_CANCEL) {
        rb_gesture_cancel(&p->gesture);
        touch_arm(p);
        return;
    }
    struct rb_output *o = NULL;
    if (kind == RB_TOUCH_DOWN && p->gesture.ndown == 0) {
        o = touch_output_for(p, d);
        p->touch_conn = o->conn_id;
        rb_gesture_configure(&p->gesture, p->touch_gestures, touch_slop(p, o));
    } else {
        o = output_by_conn(p, p->touch_conn);
    }
    if (!o) { /* the output went away mid-gesture */
        rb_gesture_cancel(&p->gesture);
        touch_arm(p);
        return;
    }
    uint64_t now = rb_mono_ms();
    double x = 0, y = 0;
    if (kind != RB_TOUCH_UP) {
        rb_touch_transform_apply(p->touch_transform, &nx, &ny);
        touch_frame_point(o, nx, ny, &x, &y);
    }
    if (kind == RB_TOUCH_DOWN)
        rb_gesture_down(&p->gesture, slot, x, y, now);
    else if (kind == RB_TOUCH_MOTION)
        rb_gesture_motion(&p->gesture, slot, x, y, now);
    else
        rb_gesture_up(&p->gesture, slot, now);
    touch_arm(p);
}

/* Pens: the pointer follows the pen while it is in proximity; the tip is
 * the primary button, the barrel button (BTN_STYLUS) the secondary one
 * (right-click), BTN_STYLUS2 the middle one. A display tablet maps onto its
 * display like a touchscreen; any other tablet onto the frame. */
static void pen_button(struct presenter *p, int b, bool down)
{
    unsigned bit = 1u << b;
    if (down == !!(p->pen_buttons & bit))
        return;
    p->pen_buttons = down ? p->pen_buttons | bit : p->pen_buttons & ~bit;
    g_button(p, b, down);
}

static void on_tablet(void *data, const struct rb_input_dev *d, bool prox, double nx, double ny, int tip,
                      uint32_t button, int button_down)
{
    struct presenter *p = data;
    if (!p->have_out || p->diag.visible)
        return;
    p->touch_events++;
    if (!prox) {
        for (int b = 0; b < 3; b++)
            pen_button(p, b, false);
        return;
    }
    if (d->direct) {
        double x, y;
        touch_frame_point(touch_output_for(p, d), nx, ny, &x, &y);
        g_point(p, x, y);
    } else {
        on_abs_norm(p, nx, ny, false);
    }
    if (tip >= 0)
        pen_button(p, 0, tip == 1);
    if (button && button_down >= 0) {
        if (button == BTN_STYLUS)
            pen_button(p, 2, button_down == 1);
        else if (button == BTN_STYLUS2)
            pen_button(p, 1, button_down == 1);
    }
}

static cJSON *input_json(struct presenter *p)
{
    cJSON *in = cJSON_CreateObject();
    cJSON_AddNumberToObject(in, "devices", rb_input_device_count(p->input));
    cJSON_AddBoolToObject(in, "xkb", rb_input_has_xkb(p->input));
    cJSON_AddNumberToObject(in, "events", (double)p->input_events);
    const struct rb_input_dev *devs[RB_INPUT_MAX_TOUCH_DEVS];
    int n = rb_input_touch_devices(p->input, devs, RB_INPUT_MAX_TOUCH_DEVS), nt = 0, np = 0;
    cJSON *a = cJSON_AddArrayToObject(in, "touch_devices");
    for (int i = 0; i < n; i++) {
        cJSON *o = cJSON_CreateObject();
        cJSON_AddStringToObject(o, "name", devs[i]->name);
        cJSON_AddStringToObject(o, "kind", devs[i]->touch ? "touchscreen" : "pen");
        cJSON_AddBoolToObject(o, "direct", devs[i]->direct);
        if (p->have_out && devs[i]->direct)
            cJSON_AddStringToObject(o, "output", touch_output_for(p, devs[i])->conn_name);
        if (devs[i]->output[0])
            cJSON_AddStringToObject(o, "output_hint", devs[i]->output);
        cJSON_AddItemToArray(a, o);
        nt += devs[i]->touch;
        np += devs[i]->tablet;
    }
    cJSON_AddNumberToObject(in, "touchscreens", nt);
    cJSON_AddNumberToObject(in, "pens", np);
    cJSON_AddBoolToObject(in, "touch_gestures", p->touch_gestures);
    if (p->touch_transform) {
        char tt[48];
        cJSON_AddStringToObject(in, "touch_transform", rb_touch_transform_name(p->touch_transform, tt, sizeof tt));
    }
    const char *gk = rb_input_gemini_keyboard(p->input);
    if (gk)
        cJSON_AddStringToObject(in, "keyboard", gk);
    cJSON_AddStringToObject(in, "gesture", rb_gesture_state_name(p->gesture.st));
    cJSON_AddNumberToObject(in, "touch_events", (double)p->touch_events);
    return in;
}

static void on_devices_changed(void *data)
{
    struct presenter *p = data;
    if (p->gesture.ndown)
        rb_gesture_cancel(&p->gesture);
    cJSON *ev = new_event("input");
    cJSON *in = input_json(p);
    cJSON *c;
    while ((c = in->child)) {
        cJSON_DetachItemViaPointer(in, c);
        cJSON_AddItemToObject(ev, c->string, c);
    }
    cJSON_Delete(in);
    emit(p, ev);
}

/* ------------------------------------------------------------------ */
/* web Screen page: tap source, remote input, viewer indicator         */

/* What the remote screen shows: Mac OS whenever the guest view is up (or the
 * raw guest was asked for), else the presenter's own logical frame. Through a
 * mode switch the remote holds its last picture exactly while the display
 * holds its last good frame (switch_hold, switchhold.h). */
static void remote_source(void *data, int source, struct rb_remote_src *s)
{
    struct presenter *p = data;
    bool guest = rb_guest_connected(&p->guest) && p->guest.px;
    s->scale = p->s > 0 ? p->s : 1;
    if (source == RB_REMOTE_SRC_GUEST || p->view == VIEW_GUEST) {
        if (guest) {
            rb_remote_guest_source(s, p->guest.px, p->guest.width, p->guest.height, p->guest.stride_px,
                                   p->switch_hold && p->view == VIEW_GUEST);
        } else {
            s->kind = RB_REMOTE_NONE;
            s->view = source == RB_REMOTE_SRC_GUEST ? "stopped" : view_name(p->view);
        }
        return;
    }
    s->w = p->lw;
    s->h = p->lh;
    s->kind = RB_REMOTE_NONE;
    s->view = view_name(p->view);
    if (!p->shadow || p->lw <= 0)
        return;
    switch (p->view) {
    case VIEW_PANEL:
        if (p->panel) {
            s->kind = RB_REMOTE_FRAME;
            s->px = p->panel;
            s->stride_px = p->lw;
        }
        break;
    case VIEW_CHECKER:
        /* The frame itself, one sample per logical pixel: the DPI-aware pattern
         * and the startup indicator exactly as shown; the local indicator's
         * corner box goes back to the pattern. */
        s->kind = RB_REMOTE_CHECKER;
        s->px = p->shadow;
        s->stride_px = p->W;
        s->step = p->s > 0 ? p->s : 1;
        s->hide_cm = p->cm > 0 ? p->cm : 1;
        if (p->badge_where == 2)
            s->hide = (rb_rect){p->badge_fr.x / s->step, p->badge_fr.y / s->step,
                                (p->badge_fr.w + s->step - 1) / s->step, (p->badge_fr.h + s->step - 1) / s->step};
        break;
    case VIEW_BLACK: s->kind = RB_REMOTE_BLACK; break;
    default: break; /* hold: hostd keeps the last picture */
    }
}

static void remote_release(struct presenter *p, const char *why)
{
    bool any = p->rbuttons != 0;
    for (unsigned code = 0; code < KEY_CNT; code++) {
        if (!bit_get(p->rfwd, code))
            continue;
        any = true;
        guest_key(p, code == KEY_POWER ? RB_Q_POWER : rb_evdev_to_qcode(code), false);
    }
    for (uint32_t b = 0; b < 3; b++)
        if ((p->rbuttons & (1u << b)) && !(p->guest_buttons & (1u << b)))
            rb_guest_send_button(&p->guest, b, false);
    memset(p->rkeys, 0, sizeof p->rkeys);
    memset(p->rfwd, 0, sizeof p->rfwd);
    p->rbuttons = 0;
    if (p->rpanel_buttons) {
        any = true;
        p->panel_buttons &= ~p->rpanel_buttons;
        p->rpanel_buttons = 0;
        panel_pointer(p);
    }
    if (any)
        rb_log("input: released remotely held keys/buttons (%s)", why);
}

static void remote_connected(void *data)
{
    struct presenter *p = data;
    tl_add(p, "remote", "web screen connected");
}

static void remote_disconnected(void *data)
{
    struct presenter *p = data;
    remote_release(p, "web screen disconnected");
    p->badge_on = false;
    tl_add(p, "remote", "web screen disconnected");
}

/* Keys from a browser (evdev codes; hostd already applied its Command/Option
 * choice). The recovery chord and Escape on the checkerboard behave as on the
 * local keyboard; the Power key is Mac OS's (ADB power: its Shut Down dialog),
 * never the host's power button. */
static void remote_key(void *data, uint32_t code, bool down)
{
    struct presenter *p = data;
    if (bit_get(p->rkeys, code) == down)
        return;
    touch_input(p);
    bit_set(p->rkeys, code, down);
    bool ctrl = bit_get(p->rkeys, KEY_LEFTCTRL) || bit_get(p->rkeys, KEY_RIGHTCTRL);
    bool alt = bit_get(p->rkeys, KEY_LEFTALT) || bit_get(p->rkeys, KEY_RIGHTALT);
    if (down && (int)code == p->chord_key && ctrl && alt) {
        release_guest_input(p, "recovery chord (web)");
        tl_add(p, "chord", "recovery chord from the web screen");
        on_chord(p);
        return;
    }
    bool preboot = p->view == VIEW_CHECKER && (p->scene == RB_SCENE_SPLASH || p->scene == RB_SCENE_GUEST);
    if (code == KEY_ESC && preboot && !bit_get(p->rfwd, code)) {
        if (down) {
            tl_add(p, "preboot_key", "escape (web)");
            cJSON *ev = new_event("preboot_key");
            cJSON_AddStringToObject(ev, "key", "escape");
            cJSON_AddBoolToObject(ev, "remote", true);
            add_times(ev);
            emit(p, ev);
        }
        return;
    }
    if (p->scene == RB_SCENE_PANEL) {
        if (rb_evdev_is_modifier(code))
            return;
        bool shift = bit_get(p->rkeys, KEY_LEFTSHIFT) || bit_get(p->rkeys, KEY_RIGHTSHIFT);
        bool cmd = bit_get(p->rkeys, KEY_LEFTMETA) || bit_get(p->rkeys, KEY_RIGHTMETA);
        char utf8[8];
        int rbkey = rb_evdev_to_rbui_fallback(code, shift, utf8);
        unsigned mods = (shift ? RBUI_MOD_SHIFT : 0) | (ctrl ? RBUI_MOD_CTRL : 0) | (alt ? RBUI_MOD_OPTION : 0) |
                        (cmd ? RBUI_MOD_COMMAND : 0);
        if (!down)
            panel_key(p, rbkey, "", false, mods);
        else if (rbkey || utf8[0])
            panel_key(p, rbkey, utf8, true, mods);
        return;
    }
    int q = code == KEY_POWER ? RB_Q_POWER : rb_evdev_to_qcode(code);
    if (!down) {
        if (bit_get(p->rfwd, code)) {
            bit_set(p->rfwd, code, false);
            guest_key(p, q, false);
        }
        return;
    }
    if (!guest_routing(p) || !q)
        return;
    bit_set(p->rfwd, code, true);
    guest_key(p, q, true);
}

static void remote_pointer(void *data, enum rb_remote_kind kind, int x, int y)
{
    struct presenter *p = data;
    touch_input(p);
    if (kind == RB_REMOTE_GUEST) {
        if (!guest_routing(p) || !p->guest.px)
            return;
        p->gx = x;
        p->gy = y;
        clamp_guest_pointer(p);
        p->pointer_dirty = true;
    } else if (kind == RB_REMOTE_FRAME && p->scene == RB_SCENE_PANEL) {
        p->px = (double)x * p->s + p->s / 2;
        p->py = (double)y * p->s + p->s / 2;
        if (p->px > p->W - 1)
            p->px = p->W - 1;
        if (p->py > p->H - 1)
            p->py = p->H - 1;
        panel_pointer(p);
    }
}

static void remote_button(void *data, enum rb_remote_kind kind, uint32_t b, bool down)
{
    struct presenter *p = data;
    touch_input(p);
    if (kind == RB_REMOTE_FRAME && p->scene == RB_SCENE_PANEL) {
        if (b >= 3) {
            if (down && p->ui) {
                panel_tick(p);
                rbui_wheel(p->ui, b == 4 ? 1 : -1);
            }
            return;
        }
        unsigned bit = b == 0 ? 1u : (b == 2 ? 2u : 4u);
        if (down) {
            p->panel_buttons |= bit;
            p->rpanel_buttons |= bit;
        } else {
            p->panel_buttons &= ~bit;
            p->rpanel_buttons &= ~bit;
        }
        panel_pointer(p);
        return;
    }
    if (kind != RB_REMOTE_GUEST || !guest_routing(p) || !p->guest.px)
        return;
    send_pointer(p);
    if (b >= 3) { /* wheel notches: press and release as sent */
        rb_guest_send_button(&p->guest, b, down);
        return;
    }
    unsigned bit = 1u << b;
    if (down) {
        if (!(p->rbuttons & bit)) {
            p->rbuttons |= bit;
            if (!(p->guest_buttons & bit))
                rb_guest_send_button(&p->guest, b, true);
        }
    } else if (p->rbuttons & bit) {
        p->rbuttons &= ~bit;
        if (!(p->guest_buttons & bit))
            rb_guest_send_button(&p->guest, b, false);
    }
}

static void remote_release_all(void *data) { remote_release(data, "web request"); }

static void remote_viewers(void *data, uint32_t viewers, uint32_t controllers)
{
    struct presenter *p = data;
    bool on = viewers > 0, control = controllers > 0;
    if (on != p->badge_on || control != p->badge_control)
        tl_add(p, "remote", "%u web viewer(s), %u with control", viewers, controllers);
    if (on && !p->badge_on)
        p->badge_scan = true;
    p->badge_on = on;
    p->badge_control = control;
}

/* Where the indicator goes now; marks the old and new places dirty when that
 * changes. Runs before every composition (cheap unless a rescan is due). */
static void badge_update(struct presenter *p)
{
    int where = 0;
    rb_rect fr = {0, 0, 0, 0};
    if (p->view != p->badge_view) {
        p->badge_view = p->view;
        p->badge_scan = true;
    }
    if (p->badge_on && p->have_out && p->shadow) {
        if (p->view == VIEW_GUEST && p->layout_ok && p->guest.px) {
            if (p->badge_scan) {
                p->badge_scan = false;
                p->badge_gx = rb_badge_menubar_gap(p->guest.px, p->guest.width, p->guest.height, p->guest.stride_px,
                                                   12);
            }
            if (p->badge_gx >= 0) {
                where = 1;
                fr = rb_layout_map_damage(&p->layout,
                                          (rb_rect){p->badge_gx, RB_BADGE_MENU_Y, RB_BADGE_W, RB_BADGE_H});
            }
        } else if ((p->view == VIEW_PANEL || p->view == VIEW_CHECKER) && p->lw > 2 * RB_BADGE_BOX) {
            where = 2;
            fr = (rb_rect){(p->lw - RB_BADGE_BOX - 8) * p->s, 8 * p->s, RB_BADGE_BOX * p->s, RB_BADGE_BOX * p->s};
        }
    }
    if (where == p->badge_where && p->badge_control == p->badge_drawn_control &&
        (!where || !memcmp(&fr, &p->badge_fr, sizeof fr)))
        return;
    if (p->badge_where)
        mark_dirty(p, p->badge_fr);
    p->badge_where = where;
    p->badge_fr = fr;
    p->badge_drawn_control = p->badge_control;
    if (where)
        mark_dirty(p, fr);
}

static void badge_timer_fn(void *data)
{
    struct presenter *p = data;
    p->badge_scan = true;
}

static cJSON *remote_json(struct presenter *p)
{
    struct rb_remote *r = &p->remote;
    cJSON *j = cJSON_CreateObject();
    cJSON_AddBoolToObject(j, "connected", rb_remote_connected(r));
    cJSON_AddStringToObject(j, "source", r->source == RB_REMOTE_SRC_GUEST ? "guest" : "composed");
    cJSON_AddStringToObject(j, "view", r->view);
    int sz[2] = {r->w, r->h};
    cJSON_AddItemToObject(j, "size", int_array(2, sz));
    cJSON_AddNumberToObject(j, "viewers", r->viewers);
    cJSON_AddNumberToObject(j, "controllers", r->controllers);
    cJSON_AddNumberToObject(j, "updates", (double)r->updates);
    cJSON_AddNumberToObject(j, "rects", (double)r->rects);
    cJSON_AddNumberToObject(j, "pixels", (double)r->pixels);
    cJSON_AddNumberToObject(j, "input_events", (double)r->input_events);
    cJSON_AddStringToObject(j, "indicator", p->badge_where == 1 ? "menu-bar" : p->badge_where == 2 ? "corner" : "none");
    return j;
}

/* ------------------------------------------------------------------ */
/* screenshots                                                         */

static void shot_done(void *data, int fd, uint32_t events)
{
    (void)fd;
    (void)events;
    struct shot *s = data;
    struct presenter *p = s->p;
    int status = 0;
    pid_t r = waitpid(s->pid, &status, WNOHANG);
    if (r == 0)
        return;
    bool ok = r == s->pid && WIFEXITED(status) && WEXITSTATUS(status) == 0;
    cJSON *resp = cJSON_CreateObject();
    cJSON_AddItemToObject(resp, "id", s->id);
    cJSON_AddBoolToObject(resp, "ok", ok);
    if (ok) {
        cJSON_AddStringToObject(resp, "path", s->path);
        cJSON_AddStringToObject(resp, "what", s->what);
        int sz[2] = {s->w, s->h};
        cJSON_AddItemToObject(resp, "size", int_array(2, sz));
    } else {
        cJSON_AddStringToObject(resp, "error", "screenshot write failed");
    }
    rb_ctl_send_json(&p->ctl, resp);
    cJSON_Delete(resp);
    rb_watch_remove(s->watch);
    close(s->pidfd);
    s->used = false;
}

static const char *start_screenshot(struct presenter *p, const cJSON *id, const char *path, const char *what)
{
    bool guest = !strcmp(what, "guest");
    if (!guest && strcmp(what, "composed"))
        return "what must be composed or guest";
    if (!path || path[0] != '/')
        return "path must be absolute";
    const uint32_t *px;
    int w, h, stride;
    if (guest) {
        if (!rb_guest_connected(&p->guest) || !p->guest.px)
            return "no guest surface";
        px = p->guest.px;
        w = p->guest.width;
        h = p->guest.height;
        stride = p->guest.stride_px;
    } else {
        if (!p->shadow)
            return "no display";
        compose(p);
        px = p->shadow;
        w = p->W;
        h = p->H;
        stride = p->W;
    }
    struct shot *s = NULL;
    for (int i = 0; i < MAX_SHOTS; i++)
        if (!p->shots[i].used) {
            s = &p->shots[i];
            break;
        }
    if (!s)
        return "too many screenshots in progress";
    pid_t pid = fork();
    if (pid < 0)
        return "fork failed";
    if (pid == 0) {
        /* Child: copy-on-write snapshot of the composed frame. */
        _exit(rb_png_write_xrgb(path, px, w, h, stride) == 0 ? 0 : 1);
    }
    int pidfd = pidfd_open(pid, 0);
    memset(s, 0, sizeof *s);
    s->used = true;
    s->pid = pid;
    s->p = p;
    s->w = w;
    s->h = h;
    snprintf(s->path, sizeof s->path, "%s", path);
    snprintf(s->what, sizeof s->what, "%s", what);
    s->id = id ? cJSON_Duplicate(id, 1) : cJSON_CreateNull();
    if (pidfd < 0) {
        /* No pidfd: wait synchronously (short). */
        s->pidfd = -1;
        int status = 0;
        waitpid(pid, &status, 0);
        bool ok = WIFEXITED(status) && WEXITSTATUS(status) == 0;
        cJSON *resp = cJSON_CreateObject();
        cJSON_AddItemToObject(resp, "id", s->id);
        cJSON_AddBoolToObject(resp, "ok", ok);
        if (ok)
            cJSON_AddStringToObject(resp, "path", path);
        else
            cJSON_AddStringToObject(resp, "error", "screenshot write failed");
        rb_ctl_send_json(&p->ctl, resp);
        cJSON_Delete(resp);
        s->used = false;
        return NULL;
    }
    s->pidfd = pidfd;
    s->watch = rb_loop_watch(p->loop, pidfd, EPOLLIN, shot_done, s);
    return NULL;
}

/* ------------------------------------------------------------------ */
/* pointer nudge (hostd's frozen-guest check)                          */

#define NUDGE_RESTORE_MS 120

/* Move the guest pointer by one pixel and put it back NUDGE_RESTORE_MS later.
 * Mac OS draws its cursor in software, so a responsive guest repaints it
 * (DAMAGE) while a frozen one does not. Sent regardless of the scene; the
 * restore is skipped if the user moved the pointer meanwhile. */
static const char *pointer_nudge(struct presenter *p)
{
    if (!rb_guest_connected(&p->guest) || !p->guest.px)
        return "no guest surface";
    if (rb_timer_armed(p->nudge_timer))
        return "nudge in progress";
    int x = (int)lround(p->gx), y = (int)lround(p->gy);
    if (x < 0) x = 0;
    if (y < 0) y = 0;
    if (x >= p->guest.width) x = p->guest.width - 1;
    if (y >= p->guest.height) y = p->guest.height - 1;
    int nx = x + 1 < p->guest.width ? x + 1 : (x > 0 ? x - 1 : x);
    p->nudge_x = x;
    p->nudge_y = y;
    p->nudge_mono = rb_mono_ms();
    rb_guest_send_pointer(&p->guest, (uint32_t)nx, (uint32_t)y);
    rb_timer_arm(p->nudge_timer, NUDGE_RESTORE_MS);
    tl_add(p, "pointer_nudge", "%d,%d -> %d,%d and back (damage_seq %llu)", x, y, nx, y,
           (unsigned long long)p->damage_seq);
    return NULL;
}

static void nudge_timer_fn(void *data)
{
    struct presenter *p = data;
    if (!rb_guest_connected(&p->guest) || !p->guest.px)
        return;
    if (p->last_input_mono > p->nudge_mono)
        return; /* the user moved since; their position wins */
    rb_guest_send_pointer(&p->guest, (uint32_t)p->nudge_x, (uint32_t)p->nudge_y);
    p->pointer_sent = true;
}

/* ------------------------------------------------------------------ */
/* control requests                                                    */

static cJSON *reply_new(const cJSON *id, bool ok)
{
    cJSON *r = cJSON_CreateObject();
    cJSON_AddItemToObject(r, "id", id ? cJSON_Duplicate(id, 1) : cJSON_CreateNull());
    cJSON_AddBoolToObject(r, "ok", ok);
    return r;
}

static void reply_error(struct presenter *p, const cJSON *id, const char *msg)
{
    cJSON *r = reply_new(id, false);
    cJSON_AddStringToObject(r, "error", msg);
    rb_ctl_send_json(&p->ctl, r);
    cJSON_Delete(r);
}

static void reply_ok(struct presenter *p, cJSON *r)
{
    rb_ctl_send_json(&p->ctl, r);
    cJSON_Delete(r);
}

static cJSON *startup_json(struct presenter *p)
{
    cJSON *o = cJSON_CreateObject();
    uint64_t now = rb_boot_ms();
    cJSON_AddBoolToObject(o, "enabled", p->ind_cfg);
    cJSON_AddStringToObject(o, "style", p->startup_style == RB_STARTUP_OSX ? "macosx" : "classic");
    cJSON_AddBoolToObject(o, "on", p->ind_on);
    cJSON_AddBoolToObject(o, "visible", ind_visible(p));
    cJSON_AddStringToObject(o, "art", rb_icon_source_name(p->art.source));
    cJSON_AddNumberToObject(o, "value", round(ind_value(p, now) * 1000.0) / 1000.0);
    cJSON_AddNumberToObject(o, "fill", p->fill_shown);
    cJSON_AddNumberToObject(o, "steps", RB_BAR_STEPS);
    if (p->prog.timed && p->prog.eta > now)
        cJSON_AddNumberToObject(o, "eta_ms", (double)(p->prog.eta - now));
    else
        cJSON_AddNullToObject(o, "eta_ms");
    cJSON_AddStringToObject(o, "phase", p->ind_phase);
    cJSON_AddBoolToObject(o, "landing", p->landing);
    cJSON_AddStringToObject(o, "happy_mac", p->icon_late ? "late" : "prompt");
    cJSON_AddBoolToObject(o, "overlay", p->overlay);
    cJSON_AddBoolToObject(o, "fading", p->fading);
    cJSON_AddBoolToObject(o, "lit", p->lit_shown);
    if (p->capture_path[0])
        cJSON_AddStringToObject(o, "capture", p->capture_path);
    int g[5] = {p->ind_cur.x, p->ind_cur.y, p->ind_cur.q, p->ind_geom.land_x, p->ind_geom.land_y};
    cJSON_AddItemToObject(o, "geometry", int_array(5, g)); /* x, y, scale, Mac OS icon x, y */
    cJSON_AddNumberToObject(o, "guest_scale", p->ind_geom.k);
    cJSON_AddNumberToObject(o, "checker_scale", p->cs);
    int an[2] = {rb_checker_anchor.x, rb_checker_anchor.y};
    cJSON_AddItemToObject(o, "checker_anchor", int_array(2, an)); /* Mac OS's picture's top-left (4.4) */
    cJSON_AddNumberToObject(o, "checker_multiple", p->cm);
    cJSON_AddNumberToObject(o, "checker_multiple_auto", p->cm_auto);
    return o;
}

/* /run/retroboot/display.json: what rb-bootdisplay needs for GRUB's next
 * background (4.4): the primary's scanout and rotation, the physical size the
 * DPI rule used (EDID or a panel quirk; GRUB cannot know it) and the grid in
 * effect. */
static void publish_display(struct presenter *p)
{
    if (!p->have_out || !p->opts->control_socket)
        return;
    char path[256], tmp[272];
    snprintf(path, sizeof path, "%s", p->opts->control_socket);
    char *slash = strrchr(path, '/');
    if (!slash)
        return;
    *slash = 0;
    if (mkdir(path, 0755) < 0 && errno != EEXIST) /* early: before the sockets exist */
        return;
    *slash = '/';
    snprintf(slash + 1, sizeof path - (size_t)(slash + 1 - path), "display.json");
    snprintf(tmp, sizeof tmp, "%s.tmp", path);
    cJSON *o = cJSON_CreateObject();
    int scan[2] = {p->out.w, p->out.h}, mode[2] = {p->W, p->H}, mm[2] = {p->mm_w, p->mm_h};
    cJSON_AddItemToObject(o, "scanout", int_array(2, scan));
    cJSON_AddItemToObject(o, "mode", int_array(2, mode));
    cJSON_AddNumberToObject(o, "rotation", p->out.rot); /* the scanout is the frame rotated by this */
    /* ... of which the user rotation (GRUB passes it as retroboot.rotation) */
    cJSON_AddNumberToObject(o, "user_rotation", p->user_rot);
    cJSON_AddNumberToObject(o, "panel_rotation", p->out.panel_rot);
    cJSON_AddStringToObject(o, "connector", p->out.conn_name);
    cJSON_AddBoolToObject(o, "internal", rb_connector_internal(p->out.conn_type));
    cJSON_AddItemToObject(o, "physical_mm", int_array(2, mm));
    if (p->mm_source)
        cJSON_AddStringToObject(o, "physical_mm_source", p->mm_source);
    else
        cJSON_AddNullToObject(o, "physical_mm_source");
    cJSON_AddNumberToObject(o, "scale", p->s);
    cJSON_AddNumberToObject(o, "checker_scale", p->cs);
    cJSON_AddNumberToObject(o, "checker_multiple_auto", p->cm_auto);
    char *txt = cJSON_PrintUnformatted(o);
    cJSON_Delete(o);
    if (!txt)
        return;
    FILE *f = fopen(tmp, "we");
    if (f) {
        bool ok = fputs(txt, f) >= 0 && fputc('\n', f) != EOF;
        ok = fclose(f) == 0 && ok;
        if (!ok || rename(tmp, path) < 0)
            unlink(tmp);
    }
    cJSON_free(txt);
}

static cJSON *status_json(struct presenter *p, const cJSON *id)
{
    cJSON *r = reply_new(id, true);
    uint64_t now = rb_mono_ms();
    cJSON_AddStringToObject(r, "scene", rb_scene_name(p->scene));
    cJSON_AddStringToObject(r, "view", view_name(p->view));
    cJSON_AddItemToObject(r, "display", display_json(p));
    cJSON_AddItemToObject(r, "guest", guest_json(p));
    uint64_t now_us = rb_mono_us();
    int recent = 0;
    for (int i = 0; i < 128; i++)
        if (p->present_ring[i] && now_us - p->present_ring[i] <= 1000000u)
            recent++;
    cJSON_AddNumberToObject(r, "fps", recent);
    cJSON *lat = cJSON_AddObjectToObject(r, "guest_latency_ms");
    cJSON_AddNumberToObject(lat, "frames", (double)p->lat_count);
    cJSON_AddNumberToObject(lat, "avg", p->lat_count ? round(p->lat_sum_us / (double)p->lat_count / 10.0) / 100.0 : 0);
    cJSON_AddNumberToObject(lat, "max", round(p->lat_max_us / 10.0) / 100.0);
    cJSON_AddNumberToObject(r, "frames_presented", p->have_out ? (double)p->out.presents : 0);
    if (p->last_input_mono)
        cJSON_AddNumberToObject(r, "last_input_ms", (double)(now - p->last_input_mono));
    else
        cJSON_AddNullToObject(r, "last_input_ms");
    cJSON_AddNumberToObject(r, "uptime_ms", (double)(now - p->start_mono_ms));
    cJSON_AddStringToObject(r, "version", RB_VERSION);
    cJSON *osd = cJSON_AddObjectToObject(r, "osd");
    cJSON_AddBoolToObject(osd, "active", p->osd.active);
    if (p->osd.active) {
        static const char *const kinds[] = {"volume", "mute", "brightness"};
        cJSON_AddStringToObject(osd, "kind", kinds[p->osd.kind]);
        cJSON_AddNumberToObject(osd, "level", p->osd.level);
        cJSON_AddBoolToObject(osd, "muted", p->osd.muted);
        cJSON_AddNumberToObject(osd, "alpha", p->osd.alpha);
        int fr[4] = {p->osd_fr.x, p->osd_fr.y, p->osd_fr.w, p->osd_fr.h};
        cJSON_AddItemToObject(osd, "rect", int_array(4, fr));
    }
    cJSON *cfg = cJSON_AddObjectToObject(r, "config");
    cJSON_AddStringToObject(cfg, "scaling", rb_scaling_name(p->scaling));
    if (p->guest_w_cfg) {
        int gs[2] = {p->guest_w_cfg, p->guest_h_cfg};
        cJSON_AddItemToObject(cfg, "guest_size", int_array(2, gs));
    } else {
        cJSON_AddNullToObject(cfg, "guest_size");
    }
    cJSON_AddBoolToObject(cfg, "enforce_guest_size", p->enforce_size);
    if (p->pixel_req)
        cJSON_AddNumberToObject(cfg, "pixel_scale", p->pixel_req);
    else
        cJSON_AddStringToObject(cfg, "pixel_scale", "auto");
    if (p->target_w) {
        int ts[2] = {p->target_w, p->target_h};
        cJSON_AddItemToObject(cfg, "guest_target", int_array(2, ts));
    }
    cJSON_AddBoolToObject(cfg, "swap_command_option", p->swap);
    cJSON_AddStringToObject(cfg, "recovery_chord", rb_chord_name(p->chord_key));
    cJSON_AddNumberToObject(cfg, "pointer_speed", p->speed);
    cJSON_AddBoolToObject(cfg, "guest_input", p->guest_input);
    cJSON_AddItemToObject(r, "remote", remote_json(p));
    cJSON_AddItemToObject(r, "input", input_json(p));
    if (p->handoff_used) {
        cJSON *h = cJSON_AddObjectToObject(r, "handoff");
        cJSON_AddStringToObject(h, "device", p->handoff.devnode);
        cJSON_AddNumberToObject(h, "total_ms", (double)p->handoff_total_ms);
    }
    if (p->guest.px) {
        cJSON *lay = cJSON_AddObjectToObject(r, "layout");
        cJSON_AddStringToObject(lay, "mode", rb_scaling_name(p->layout.mode));
        cJSON_AddNumberToObject(lay, "k", p->layout.k);
        int d[4] = {p->layout.dst.x, p->layout.dst.y, p->layout.dst.w, p->layout.dst.h};
        cJSON_AddItemToObject(lay, "dst", int_array(4, d));
        int src[2] = {p->layout.src_w, p->layout.src_h};
        cJSON_AddItemToObject(lay, "src", int_array(2, src));
        cJSON_AddBoolToObject(lay, "deferred", p->layout_deferred);
    }
    cJSON_AddItemToObject(r, "startup", startup_json(p));
    cJSON *tl = cJSON_AddArrayToObject(r, "timeline");
    for (int i = 0; i < p->tl_count; i++) {
        struct tl_entry *e = &p->tl[(p->tl_head + i) % TIMELINE_N];
        cJSON *o = cJSON_CreateObject();
        cJSON_AddStringToObject(o, "what", e->what);
        cJSON_AddStringToObject(o, "detail", e->detail);
        cJSON_AddNumberToObject(o, "mono_ms", (double)e->mono_ms);
        cJSON_AddNumberToObject(o, "boot_ms", (double)e->boot_ms);
        cJSON_AddItemToArray(tl, o);
    }
    return r;
}

static void apply_display_config(struct presenter *p, const cJSON *id, const struct rb_display_req *d)
{
    const drmModeModeInfo *target = NULL;
    if (d->has_host_mode && p->have_out) {
        if (d->host_w) {
            /* host_mode is as reported in "modes" (the panel's mounting): unrotate it. */
            bool rot = rb_rotation_swaps(p->out.panel_rot);
            target = rb_output_find_mode(&p->out, rot ? d->host_h : d->host_w, rot ? d->host_w : d->host_h,
                                         d->host_refresh);
            if (!target) {
                reply_error(p, id, "host_mode is not offered by the connector");
                return;
            }
        } else if (p->out.have_boot_mode) {
            target = &p->out.boot_mode;
        } else {
            for (int i = 0; i < p->out.nmodes; i++)
                if (p->out.modes[i].type & DRM_MODE_TYPE_PREFERRED)
                    target = &p->out.modes[i];
            if (!target && p->out.nmodes)
                target = &p->out.modes[0];
        }
    }
    if (d->has_scaling && d->scaling != p->scaling) {
        p->scaling = d->scaling;
        relayout(p);
        if (p->view == VIEW_GUEST)
            mark_all(p);
        tl_add(p, "display_config", "scaling %s", rb_scaling_name(p->scaling));
    }
    if (d->has_enforce && d->enforce != p->enforce_size) {
        p->enforce_size = d->enforce;
        tl_add(p, "display_config", "enforce_guest_size %s", p->enforce_size ? "on" : "off");
        if (!p->enforce_size)
            rb_timer_arm(p->resize_timer, 0);
        p->resize_attempts = 0;
        schedule_resize_check(p);
    }
    if (d->has_primary && d->primary_external != p->prefer_external) {
        p->prefer_external = d->primary_external;
        tl_add(p, "display_config", "primary %s", p->prefer_external ? "external" : "internal");
        if (p->have_out && !elect_pi_card(p, "primary setting") && !elect_primary(p, "primary setting"))
            emit_display(p);
    }
    if (d->has_guest_size && (d->guest_w != p->guest_w_cfg || d->guest_h != p->guest_h_cfg)) {
        p->guest_w_cfg = d->guest_w;
        p->guest_h_cfg = d->guest_h;
        ind_relayout(p);
    }
    /* One retarget for both: the pixel scale change includes the guest_size one. */
    if (d->has_pixel_scale && d->pixel_scale != p->pixel_req)
        set_pixel_scale(p, d->pixel_scale);
    else if (d->has_guest_size)
        update_target(p, "guest_size");
    if (d->has_rotation && (d->rotation != p->rot_req || (d->rotation != p->user_rot && !p->rot_deferred))) {
        tl_add(p, "display_config", "rotation %d", d->rotation);
        set_rotation(p, d->rotation);
    }
    if (d->has_host_mode) {
        if (d->host_w) {
            p->host_mode[0] = d->host_w;
            p->host_mode[1] = d->host_h;
            p->host_mode[2] = d->host_refresh;
        } else {
            memset(p->host_mode, 0, sizeof p->host_mode);
        }
        if (target && (target->hdisplay != p->out.mode.hdisplay || target->vdisplay != p->out.mode.vdisplay ||
                       target->clock != p->out.mode.clock)) {
            drmModeModeInfo m = *target;
            int rc = rb_output_set_mode(&p->out, &m);
            if (rc < 0) {
                reply_error(p, id, "modeset failed");
                return;
            }
            display_changed(p, "modeset");
        }
    }
    cJSON *r = reply_new(id, true);
    cJSON_AddItemToObject(r, "display", display_json(p));
    reply_ok(p, r);
}

static void on_request(void *data, const cJSON *req)
{
    struct presenter *p = data;
    const cJSON *id = cJSON_GetObjectItemCaseSensitive(req, "id");
    const cJSON *cmdj = cJSON_GetObjectItemCaseSensitive(req, "cmd");
    if (p->chord_wait_ack) {
        p->chord_wait_ack = false;
        rb_timer_arm(p->chord_timer, 0);
    }
    if (!cJSON_IsString(cmdj)) {
        reply_error(p, id, "cmd must be a string");
        return;
    }
    const char *cmd = cmdj->valuestring;
    char err[200];
    if (!strcmp(cmd, "hello")) {
        cJSON *r = reply_new(id, true);
        cJSON_AddNumberToObject(r, "version", 1);
        cJSON_AddItemToObject(r, "display", display_json(p));
        cJSON_AddStringToObject(r, "scene", rb_scene_name(p->scene));
        cJSON_AddItemToObject(r, "guest", guest_json(p));
        cJSON_AddItemToObject(r, "input", input_json(p));
        reply_ok(p, r);
    } else if (!strcmp(cmd, "set_scene")) {
        struct rb_scene_req s;
        if (rb_parse_set_scene(req, &s, err, sizeof err) < 0) {
            reply_error(p, id, err);
            return;
        }
        p->fallback = false;
        set_scene(p, s.scene, s.reveal, s.background);
        cJSON *r = reply_new(id, true);
        cJSON_AddStringToObject(r, "scene", rb_scene_name(p->scene));
        cJSON_AddStringToObject(r, "view", view_name(p->view));
        reply_ok(p, r);
    } else if (!strcmp(cmd, "ui_show")) {
        const cJSON *doc = cJSON_GetObjectItemCaseSensitive(req, "doc");
        if (!cJSON_IsObject(doc)) {
            reply_error(p, id, "doc must be an object");
            return;
        }
        char *json = cJSON_PrintUnformatted(doc);
        int rc = json && p->ui ? rbui_set_doc(p->ui, json, strlen(json), err, sizeof err) : -1;
        if (!json || !p->ui)
            snprintf(err, sizeof err, "panel unavailable");
        cJSON_free(json);
        if (rc < 0) {
            reply_error(p, id, err);
            return;
        }
        p->fallback = false;
        const cJSON *did = cJSON_GetObjectItemCaseSensitive(doc, "id");
        tl_add(p, "ui_show", "%s", cJSON_IsString(did) ? did->valuestring : "?");
        if (!p->panel_doc) {
            p->panel_doc = true;
            update_view(p);
        }
        reply_ok(p, reply_new(id, true));
    } else if (!strcmp(cmd, "display_config")) {
        struct rb_display_req d;
        if (rb_parse_display_config(req, &d, err, sizeof err) < 0) {
            reply_error(p, id, err);
            return;
        }
        apply_display_config(p, id, &d);
    } else if (!strcmp(cmd, "display_power")) {
        struct rb_power_req pw;
        if (rb_parse_display_power(req, &pw, err, sizeof err) < 0) {
            reply_error(p, id, err);
            return;
        }
        if (!p->have_out) {
            reply_error(p, id, "no display");
            return;
        }
        int matched = 0, failed = 0;
        for (int i = -1; i < p->nheads; i++) {
            struct rb_output *o = i < 0 ? &p->out : &p->heads[i];
            bool m = !strcmp(pw.output, "all") || !strcmp(pw.output, o->conn_name) ||
                     (!strcmp(pw.output, "internal") && o->internal) ||
                     (!strcmp(pw.output, "external") && !o->internal);
            if (!m)
                continue;
            matched++;
            bool was_off = o->off;
            if (rb_output_set_power(o, pw.on) < 0)
                failed++;
            else if (was_off != o->off)
                tl_add(p, "display_power", "%s %s", o->conn_name, pw.on ? "on" : "off");
        }
        if (failed) {
            reply_error(p, id, "DPMS change failed");
            return;
        }
        cJSON *r = reply_new(id, true);
        cJSON_AddNumberToObject(r, "outputs", matched);
        cJSON_AddItemToObject(r, "display", display_json(p));
        reply_ok(p, r);
    } else if (!strcmp(cmd, "input_config")) {
        struct rb_input_req in;
        if (rb_parse_input_config(req, &in, err, sizeof err) < 0) {
            reply_error(p, id, err);
            return;
        }
        if (in.has_swap && in.swap != p->swap) {
            release_guest_input(p, "swap change");
            p->swap = in.swap;
        }
        /* Gemini PDA keyboard layer (gemkbd.h): its keycaps, and Option's key under the swap */
        rb_input_set_gemini(p->input, in.has_keyboard_layout ? in.keyboard_layout : -1, p->swap);
        if (in.has_touch_transform)
            p->touch_transform = in.touch_transform;
        if (in.has_chord)
            p->chord_key = in.chord;
        if (in.has_speed)
            p->speed = in.speed;
        if (in.has_touch_gestures && in.touch_gestures != p->touch_gestures) {
            p->touch_gestures = in.touch_gestures;
            rb_gesture_cancel(&p->gesture);
        }
        tl_add(p, "input_config", "swap %d chord %s speed %.2f touch gestures %s", p->swap, rb_chord_name(p->chord_key),
               p->speed, p->touch_gestures ? "on" : "off");
        reply_ok(p, reply_new(id, true));
    } else if (!strcmp(cmd, "guest_input")) {
        const cJSON *en = cJSON_GetObjectItemCaseSensitive(req, "enabled");
        if (!cJSON_IsBool(en)) {
            reply_error(p, id, "enabled must be a boolean");
            return;
        }
        bool on = cJSON_IsTrue(en);
        if (!on && p->guest_input)
            release_guest_input(p, "guest input disabled");
        p->guest_input = on;
        reply_ok(p, reply_new(id, true));
    } else if (!strcmp(cmd, "screenshot")) {
        const cJSON *path = cJSON_GetObjectItemCaseSensitive(req, "path");
        const cJSON *what = cJSON_GetObjectItemCaseSensitive(req, "what");
        const char *e = start_screenshot(p, id, cJSON_IsString(path) ? path->valuestring : NULL,
                                         cJSON_IsString(what) ? what->valuestring : "composed");
        if (e)
            reply_error(p, id, e);
    } else if (!strcmp(cmd, "osd")) {
        struct rb_osd_req o;
        if (rb_parse_osd(req, &o, err, sizeof err) < 0) {
            reply_error(p, id, err);
            return;
        }
        bool shown = osd_allowed(p);
        if (shown) {
            if (p->osd.active)
                mark_dirty(p, p->osd_fr);
            if (rb_osd_show(&p->osd, (enum rb_osd_kind)o.kind, o.level, o.muted, rb_mono_ms()) < 0) {
                reply_error(p, id, "out of memory");
                return;
            }
            p->osd_view = p->view;
            p->osd_fr = rb_osd_rect(&p->osd, osd_area(p), p->s, p->W, p->H);
            mark_dirty(p, p->osd_fr);
            rb_timer_arm(p->osd_timer, RB_OSD_HOLD_MS);
        }
        cJSON *r = reply_new(id, true);
        cJSON_AddBoolToObject(r, "shown", shown);
        cJSON_AddStringToObject(r, "view", view_name(p->view));
        reply_ok(p, r);
    } else if (!strcmp(cmd, "status")) {
        reply_ok(p, status_json(p, id));
    } else if (!strcmp(cmd, "startup_config")) {
        /* hostd's startup.progress_indicator and the Platinum art: they apply
         * the next time the checkerboard is entered. */
        const cJSON *en = cJSON_GetObjectItemCaseSensitive(req, "enabled");
        const cJSON *art = cJSON_GetObjectItemCaseSensitive(req, "art");
        const cJSON *cap = cJSON_GetObjectItemCaseSensitive(req, "capture");
        const cJSON *hm = cJSON_GetObjectItemCaseSensitive(req, "happy_mac");
        const cJSON *style = cJSON_GetObjectItemCaseSensitive(req, "style");
        if (style && !(cJSON_IsString(style) &&
                      (!strcmp(style->valuestring, "classic") || !strcmp(style->valuestring, "macosx")))) {
            reply_error(p, id, "style must be classic or macosx");
            return;
        }
        if ((en && !cJSON_IsBool(en)) || (art && !cJSON_IsString(art) && !cJSON_IsNull(art)) ||
            (cap && !cJSON_IsString(cap) && !cJSON_IsNull(cap))) {
            reply_error(p, id, "enabled must be a boolean, art and capture a path or null");
            return;
        }
        if (hm &&
            !(cJSON_IsString(hm) && (!strcmp(hm->valuestring, "prompt") || !strcmp(hm->valuestring, "late")))) {
            reply_error(p, id, "happy_mac must be \"prompt\" or \"late\"");
            return;
        }
        /* When Mac OS draws its Happy Mac (the card of this startup): applies
         * at once, to the next cut-over; it changes nothing on screen. */
        if (hm && p->icon_late != !strcmp(hm->valuestring, "late")) {
            p->icon_late = !strcmp(hm->valuestring, "late");
            tl_add(p, "startup_config", "Mac OS's Happy Mac comes %s", p->icon_late ? "late" : "with its gray");
        }
        const cJSON *paths[2] = {art, cap};
        for (int i = 0; i < 2; i++)
            if (cJSON_IsString(paths[i]) &&
                (paths[i]->valuestring[0] != '/' || strlen(paths[i]->valuestring) >= sizeof p->art_path ||
                 strstr(paths[i]->valuestring, ".."))) {
                reply_error(p, id, "art and capture must be absolute paths");
                return;
            }
        if (cap)
            snprintf(p->capture_path, sizeof p->capture_path, "%s", cJSON_IsString(cap) ? cap->valuestring : "");
        bool changed = false;
        if (style) {
            int next = !strcmp(style->valuestring, "macosx") ? RB_STARTUP_OSX : RB_STARTUP_CLASSIC;
            if (next != p->startup_style) {
                p->startup_style = next;
                rb_startup_panel_background(p->panel_chk, p->lw, p->lw, p->lh, p->cm, p->s, next);
                if (p->panel_bg == RB_PANEL_BG_GUEST_DIMMED)
                    render_dimmed_bg(p);
                p->panel_force = true;
                changed = true;
                mark_all(p);
                update_borders(p);
                tl_add(p, "startup_style", "%s", style->valuestring);
            }
        }
        if (en && p->ind_cfg != cJSON_IsTrue(en)) {
            p->ind_cfg = cJSON_IsTrue(en);
            changed = true;
        }
        if (art) {
            const char *v = cJSON_IsString(art) ? art->valuestring : "";
            if (strcmp(v, p->art_path) ||
                (!p->art_pending && p->art.source != (v[0] ? RB_ICON_MACOS : RB_ICON_BUILTIN))) {
                snprintf(p->art_path, sizeof p->art_path, "%s", v);
                p->art_pending = changed = true;
            }
        }
        if (p->view != VIEW_CHECKER) /* not on screen: nothing to keep */
            ind_latch(p, VIEW_NONE);
        if (changed)
            tl_add(p, "startup_config", "indicator %s, art %s%s", p->ind_cfg ? "on" : "off",
                   p->art_path[0] ? p->art_path : "built-in",
                   p->view == VIEW_CHECKER ? " (from the next startup)" : "");
        cJSON *r = reply_new(id, true);
        cJSON_AddItemToObject(r, "startup", startup_json(p));
        reply_ok(p, r);
    } else if (!strcmp(cmd, "startup_progress")) {
        /* hostd's milestones: the expected time left until the cut-over
         * (the bar is re-anchored where it is, so it never goes back), or a
         * new attempt (reset: the bar starts again at 0). */
        const cJSON *reset = cJSON_GetObjectItemCaseSensitive(req, "reset");
        const cJSON *rem = cJSON_GetObjectItemCaseSensitive(req, "remaining_ms");
        const cJSON *phase = cJSON_GetObjectItemCaseSensitive(req, "phase");
        if ((reset && !cJSON_IsBool(reset)) || (rem && !cJSON_IsNumber(rem) && !cJSON_IsNull(rem)) ||
            (phase && !cJSON_IsString(phase))) {
            reply_error(p, id, "reset must be a boolean, remaining_ms a number and phase a string");
            return;
        }
        uint64_t now = rb_boot_ms();
        if (cJSON_IsTrue(reset) && !p->landing) {
            rb_progress_reset(&p->prog, now, 0);
            if (p->fill_shown) {
                p->fill_shown = 0;
                if (ind_visible(p))
                    mark_dirty(p, rb_startup_bar_rect(&p->ind_cur));
            }
        }
        if (cJSON_IsNumber(rem) && !p->landing) {
            double ms = rem->valuedouble;
            if (!(ms >= 0))
                ms = 0;
            if (ms > 3600000)
                ms = 3600000;
            rb_progress_retarget(&p->prog, now, now + (uint64_t)ms);
        }
        if (cJSON_IsString(phase))
            snprintf(p->ind_phase, sizeof p->ind_phase, "%s", phase->valuestring);
        tl_add(p, "startup", "%s%s remaining %ld ms: bar %.3f (%d/%d)", p->ind_phase,
               cJSON_IsTrue(reset) ? " (reset)" : "", cJSON_IsNumber(rem) ? (long)rem->valuedouble : -1L,
               ind_value(p, now), p->fill_shown, RB_BAR_STEPS);
        ind_tick(p);
        cJSON *r = reply_new(id, true);
        cJSON_AddItemToObject(r, "startup", startup_json(p));
        reply_ok(p, r);
    } else if (!strcmp(cmd, "release_all")) {
        release_guest_input(p, "hostd request");
        reply_ok(p, reply_new(id, true));
    } else if (!strcmp(cmd, "pointer_nudge")) {
        const char *e = pointer_nudge(p);
        if (e) {
            reply_error(p, id, e);
            return;
        }
        cJSON *r = reply_new(id, true);
        cJSON_AddNumberToObject(r, "damage_seq", (double)p->damage_seq);
        int pt[2] = {p->nudge_x, p->nudge_y};
        cJSON_AddItemToObject(r, "pointer", int_array(2, pt));
        reply_ok(p, r);
    } else {
        snprintf(err, sizeof err, "unknown cmd '%.60s'", cmd);
        reply_error(p, id, err);
    }
}

/* ------------------------------------------------------------------ */
/* main                                                                */

static void panel_tick(struct presenter *p)
{
    if (p->scene != RB_SCENE_PANEL || !p->ui) {
        rb_timer_arm(p->tick_timer, 0);
        return;
    }
    int again = rbui_tick(p->ui, rb_mono_ms());
    rb_timer_arm(p->tick_timer, again > 0 ? (uint64_t)again : 100);
}

static void tick_timer_fn(void *data) { panel_tick(data); }

static void signal_ready(void *data, int fd, uint32_t events)
{
    (void)events;
    struct presenter *p = data;
    struct signalfd_siginfo si;
    while (read(fd, &si, sizeof si) == sizeof si) {
        if (si.ssi_signo == SIGTERM || si.ssi_signo == SIGINT) {
            tl_add(p, "exit", "signal %u", si.ssi_signo);
            rb_loop_quit(p->loop, 0);
        } else if (si.ssi_signo == SIGCHLD) {
            /* Only the systemctl child is reaped here; screenshot children
             * are reaped through their pidfds. */
            int status;
            if (p->power_pid > 0 && waitpid(p->power_pid, &status, WNOHANG) == p->power_pid) {
                p->power_pid = 0;
                if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
                    rb_log("power: systemctl failed; forcing");
                    rb_timer_arm(p->power_timer, 1000);
                }
            }
        }
    }
}

/* Fatal signals: leave the current frame on screen (no black flash while
 * systemd restarts us), then die with the original signal. */
static void crash_handler(int sig)
{
    if (g_p && g_p->have_out) {
        rb_output_leave_signal_safe(&g_p->out);
        for (int i = 0; i < g_p->nheads; i++)
            rb_output_leave_signal_safe(&g_p->heads[i]);
    }
    signal(sig, SIG_DFL);
    raise(sig);
}

static void install_crash_handlers(void)
{
#ifdef __SANITIZE_ADDRESS__
    return; /* keep the sanitizer's own reports in ASan builds */
#endif
    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = crash_handler;
    sa.sa_flags = SA_RESETHAND;
    sigemptyset(&sa.sa_mask);
    int sigs[] = {SIGSEGV, SIGBUS, SIGILL, SIGFPE, SIGABRT};
    for (size_t i = 0; i < sizeof sigs / sizeof sigs[0]; i++)
        sigaction(sigs[i], &sa, NULL);
}

static void sd_notify_ready(void)
{
    const char *path = getenv("NOTIFY_SOCKET");
    if (!path || !path[0])
        return;
    struct sockaddr_un a = {.sun_family = AF_UNIX};
    size_t n = strlen(path);
    if (n >= sizeof a.sun_path)
        return;
    memcpy(a.sun_path, path, n);
    if (a.sun_path[0] == '@')
        a.sun_path[0] = 0;
    int fd = socket(AF_UNIX, SOCK_DGRAM | SOCK_CLOEXEC, 0);
    if (fd < 0)
        return;
    sendto(fd, "READY=1", 7, MSG_NOSIGNAL, (struct sockaddr *)&a,
           (socklen_t)(offsetof(struct sockaddr_un, sun_path) + n));
    close(fd);
}

static void mkdir_parent(const char *path)
{
    char dir[256];
    snprintf(dir, sizeof dir, "%s", path);
    char *slash = strrchr(dir, '/');
    if (!slash || slash == dir)
        return;
    *slash = 0;
    if (mkdir(dir, 0755) < 0 && errno != EEXIST)
        rb_log("mkdir %s: %s", dir, strerror(errno));
}

int rb_presenter_main(const struct rb_opts *opts)
{
    static struct presenter P;
    struct presenter *p = &P;
    g_p = p;
    memset(p, 0, sizeof *p);
    p->opts = opts;
    p->start_mono_ms = rb_mono_ms();
    p->diag_start_ms = rb_boot_ms();
    p->hostd_absent_since = p->start_mono_ms;
    p->speed = 1.0;
    p->chord_key = KEY_DELETE;
    p->scaling = RB_SCALING_INTEGER;
    /* GRUB's checkerboard was drawn at the configured pixel scale; start with
     * it so the handoff from the splash is identical (hostd's display_config
     * then confirms or changes it). */
    p->pixel_req = rb_boot_pixel_scale(NULL);
    /* ... and the user rotation it was drawn for (rotation.h) */
    p->user_rot = p->rot_req = rb_user_rotation = rb_boot_rotation(NULL);
    /* ... and GRUB's startup indicator (4.4.1): same guest target, pattern
     * scale and expected cut-over. */
    rb_boot_startup(NULL, &p->boot);
    p->guest_w_cfg = p->boot.guest_w;
    p->guest_h_cfg = p->boot.guest_h;
    p->ind_cfg = p->boot.progress == 1;
    p->startup_style = p->boot.style;
    rb_icon_art_builtin(&p->art);
    snprintf(p->ind_phase, sizeof p->ind_phase, "boot");
    p->guest_input = true;
    p->scene = RB_SCENE_SPLASH;
    if (opts->initial_scene && !strcmp(opts->initial_scene, "black"))
        p->scene = RB_SCENE_BLACK;
    p->reveal = RB_REVEAL_FIRST_CONTENT;
    p->guest.fd = -1;
    p->ctl.fd = -1;
    rb_log_init("rb-presenter", 0);
    tl_add(p, "start", "rb-presenter %s pid %d", RB_VERSION, getpid());

    sigset_t mask;
    sigemptyset(&mask);
    sigaddset(&mask, SIGTERM);
    sigaddset(&mask, SIGINT);
    sigaddset(&mask, SIGCHLD);
    sigprocmask(SIG_BLOCK, &mask, NULL);
    signal(SIGPIPE, SIG_IGN);
    int sfd = signalfd(-1, &mask, SFD_NONBLOCK | SFD_CLOEXEC);

    p->loop = rb_loop_new();
    if (!p->loop)
        return 1;
    install_crash_handlers();
    rb_loop_set_idle(p->loop, idle, p);
    if (sfd >= 0)
        rb_loop_watch(p->loop, sfd, EPOLLIN, signal_ready, p);
    p->present_timer = rb_timer_new(p->loop, present_timer_fn, p);
    p->retry_timer = rb_timer_new(p->loop, retry_timer_fn, p);
    p->content_timer = rb_timer_new(p->loop, content_timer_fn, p);
    p->resize_timer = rb_timer_new(p->loop, resize_timer_fn, p);
    p->relayout_timer = rb_timer_new(p->loop, relayout_timer_fn, p);
    p->switch_timer = rb_timer_new(p->loop, switch_timer_fn, p);
    p->repeat_timer = rb_timer_new(p->loop, repeat_timer_fn, p);
    p->chord_timer = rb_timer_new(p->loop, chord_timer_fn, p);
    p->power_timer = rb_timer_new(p->loop, power_timer_fn, p);
    p->tick_timer = rb_timer_new(p->loop, tick_timer_fn, p);
    p->nudge_timer = rb_timer_new(p->loop, nudge_timer_fn, p);
    p->hostkey_timer = rb_timer_new(p->loop, hostkey_timer_fn, p);
    p->ind_timer = rb_timer_new(p->loop, ind_timer_fn, p);
    p->land_timer = rb_timer_new(p->loop, land_timer_fn, p);
    p->fade_timer = rb_timer_new(p->loop, fade_timer_fn, p);
    p->overlay_timer = rb_timer_new(p->loop, overlay_timer_fn, p);
    p->cap_timer = rb_timer_new(p->loop, cap_timer_fn, p);
    p->badge_timer = rb_timer_new(p->loop, badge_timer_fn, p);
    p->osd_timer = rb_timer_new(p->loop, osd_timer_fn, p);
    p->rot_timer = rb_timer_new(p->loop, rot_timer_fn, p);
    p->touch_timer = rb_timer_new(p->loop, touch_timer_fn, p);
    p->touch_gestures = true;
    {
        int tt = rb_touch_transform_parse(getenv("RB_TOUCH_TRANSFORM"));
        p->touch_transform = tt > 0 ? tt : 0;
    }
    {
        struct rb_gesture_cb gcb = {g_point, g_button, g_pan};
        rb_gesture_init(&p->gesture, &gcb, p);
    }
    p->badge_gx = -1;
    p->remote.fd = p->remote.listen_fd = p->remote.memfd = -1;

    p->ui = rbui_new();
    if (p->ui)
        rbui_set_emitter(p->ui, ui_emit, p);
    /* Startup Details: the view may already be up (the splash or the
     * firmware framebuffer showed it); the first frame shows it too. */
    if (rb_diag_watch_init(&p->diag) >= 0) {
        rb_loop_watch(p->loop, p->diag.fd, EPOLLIN, diag_ready, p);
        rb_diag_watch_update(&p->diag);
        if (p->diag.visible)
            tl_add(p, "startup_details", "shown at start");
        p->diag_timer = rb_timer_new(p->loop, diag_timer_fn, p);
        rb_timer_arm(p->diag_timer, RB_DIAG_HEARTBEAT_MS);
    }

    if (!opts->no_vt)
        rb_vt_take();

    /* 1. Display: take it over from the initramfs splash when present. */
    /* The bar's time base: from here unless the splash already started it. */
    {
        uint64_t now = rb_boot_ms(), eta = p->boot.startup_ms;
        rb_progress_reset(&p->prog, now, eta ? (eta > now ? eta : now) : 0);
        p->blink_t0 = now;
        p->lit_shown = true;
    }
    /* Installer: Mac OS's startup box with RetroBoot's progress (4.4.2). */
    if (rb_isplash_run_start(&p->isplash, p->loop, NULL, rb_boot_ms(), isplash_redraw, p))
        tl_add(p, "installer_splash", "on (%s art)", rb_isplash_source_name(p->isplash.art.source));
    if (!opts->no_handoff && rb_splash_handoff_begin(&p->handoff, 2000) == 0) {
        p->handoff_used = true;
        tl_add(p, "handoff", "splash released %s %s %dx%d@%d", p->handoff.devnode, p->handoff.connector,
               p->handoff.w, p->handoff.h, p->handoff.refresh);
        /* Continue the splash's indicator exactly as it is on screen. */
        struct rb_splash_startup st;
        if (rb_splash_handoff_startup(&p->handoff, &st) == 0) {
            p->ind_cfg = st.indicator;
            p->startup_style = st.style;
            p->prog.timed = st.timed;
            p->prog.t0 = st.t0;
            p->prog.p0 = st.p0;
            p->prog.eta = st.eta;
            p->fill_shown = st.fill;
            p->blink_t0 = st.blink_t0;
            p->lit_shown = st.lit;
            if (st.have_art)
                rb_icon_art_set(&p->art, st.art, st.source);
            tl_add(p, "handoff_startup", "indicator %s (%s art) bar %d/%d, cut-over expected at %llu ms",
                   st.indicator ? "on" : "off", rb_icon_source_name(p->art.source), st.fill, RB_BAR_STEPS,
                   (unsigned long long)st.eta);
        }
        if (p->isplash.on && rb_isplash_handoff_take(&p->isplash, p->handoff.fd, rb_boot_ms()) == 0)
            tl_add(p, "installer_splash", "continued from the splash: %s, bar %d/%d (%s art)",
                   p->isplash.on ? (p->isplash.phase ? "starting up" : "welcome") : "off", p->isplash.fill,
                   RB_ISPLASH_BAR_STEPS, rb_isplash_source_name(p->isplash.art.source));
        if (p->handoff.devnode[0] && strcmp(p->handoff.devnode, "none"))
            open_output(p, p->handoff.devnode, p->handoff.connector[0] ? p->handoff.connector : opts->connector);
    }
    /* No splash (a presenter restart, or rb-splash off): the art GRUB used,
     * Mac OS's captured Happy Mac, else the Platinum bundle's (hostd's
     * startup_config confirms it). */
    if (!p->handoff_used && p->ind_cfg &&
        rb_icon_art_load(&p->art, "/var/lib/retroboot/state/startup-icon.rbi") != RB_ICON_MACOS)
        rb_icon_art_load(&p->art, "/var/lib/retroboot/assets/platinum/icons/overview-32.rbi");
    scan_outputs(p);
    if (p->have_out) {
        compose(p);
        present(p);
    }
    if (p->handoff_used) {
        bool shown = p->have_out && p->out.on_screen;
        bool ok = rb_splash_handoff_complete(&p->handoff, shown);
        rb_splash_handoff_finish(&p->handoff, ok);
        p->handoff_total_ms = rb_mono_ms() - p->handoff.start_mono_ms;
        tl_add(p, "handoff_done", "%s in %llu ms",
               shown ? "presenter owns the display" : ok ? "splash had no display; presenter waits for one" : "aborted",
               (unsigned long long)p->handoff_total_ms);
    }

    /* 2. Hotplug monitoring for DRM devices (driver bind/unbind, connectors). */
    p->udev = udev_new();
    if (p->udev) {
        p->mon = udev_monitor_new_from_netlink(p->udev, "udev");
        if (p->mon) {
            udev_monitor_filter_add_match_subsystem_devtype(p->mon, "drm", NULL);
            udev_monitor_enable_receiving(p->mon);
            rb_loop_watch(p->loop, udev_monitor_get_fd(p->mon), EPOLLIN, udev_ready, p);
        }
    }
    if (!p->have_out) {
        rb_log("display: no usable DRM device yet; waiting for hotplug");
        rb_timer_arm(p->retry_timer, 1000);
    }

    /* 3. Sockets. */
    mkdir_parent(opts->display_socket);
    mkdir_parent(opts->control_socket);
    struct rb_guest_cb gcb = {guest_connected, guest_disconnected, guest_surface, guest_damage, guest_event};
    if (rb_guest_listen(&p->guest, p->loop, opts->display_socket, opts->socket_group, &gcb, p) < 0)
        rb_log("display socket unavailable");
    struct rb_ctl_cb ccb = {ctl_connected, ctl_disconnected, on_request};
    if (rb_ctl_listen(&p->ctl, p->loop, opts->control_socket, &ccb, p) < 0)
        rb_log("control socket unavailable");
    if (opts->remote_socket && opts->remote_socket[0]) {
        mkdir_parent(opts->remote_socket);
        struct rb_remote_cb rcb = {remote_source,  remote_connected,  remote_disconnected, remote_key,
                                   remote_pointer, remote_button,     remote_release_all,  remote_viewers};
        if (rb_remote_listen(&p->remote, p->loop, opts->remote_socket, &rcb, p) < 0)
            rb_log("remote screen socket unavailable");
    }

    /* 4. Input. */
    if (!opts->no_input) {
        struct rb_input_cb icb = {.key = on_key, .motion = on_motion, .motion_abs = on_motion_abs,
                                  .button = on_button, .scroll = on_scroll, .lid = on_lid, .abs_norm = on_abs_norm,
                                  .touch = on_touch, .tablet = on_tablet, .devices_changed = on_devices_changed};
        p->input = rb_input_new(p->loop, &icb, p);
        rb_input_set_screen(p->input, p->W, p->H);
        on_devices_changed(p); /* the touchscreens and pens found at start */
    }
    sd_notify_ready();
    tl_add(p, "ready", "presenter running (display %s)", p->have_out ? p->out.devnode : "none");

    while (rb_loop_running(p->loop))
        rb_loop_dispatch(p->loop, -1);

    /* Debug exit path; production never gets here except on SIGTERM. */
    release_guest_input(p, "exit");
    if (p->remote.loop)
        rb_remote_shutdown(&p->remote);
    rb_guest_shutdown(&p->guest);
    rb_ctl_shutdown(&p->ctl);
    rb_input_free(p->input);
    if (p->have_out) {
        if (opts->restore_vt)
            rb_output_close(&p->out);
        else
        {
            /* keep the picture for the next presenter (mirrors first: they
             * share the primary's DRM file) */
            for (int i = 0; i < p->nheads; i++)
                rb_output_leave(&p->heads[i]);
            rb_output_leave(&p->out);
        }
    }
    if (opts->restore_vt)
        rb_vt_restore();
    rbui_free(p->ui);
    if (p->mon)
        udev_monitor_unref(p->mon);
    if (p->udev)
        udev_unref(p->udev);
    if (sfd >= 0)
        close(sfd);
    rb_layout_free(&p->layout);
    free(p->shadow);
    free(p->panel);
    free(p->panel_bg_img);
    free(p->panel_chk);
    free(p->fade_px);
    rb_osd_free(&p->osd);
    free(p->osd_under);
    if (p->guest_log.f)
        rb_framelog_close(&p->guest_log);
    rb_loop_free(p->loop);
    return 0;
}
