/* RetroBoot's installer startup screen (ARCHITECTURE.md 4.4.2).
 *
 * While the installer loads, the checkerboard carries a replica of Mac OS
 * 9.2's startup box: the box stands where Mac OS draws its own (measured on
 * the real guest at six sizes: guest pixel ((w - 422) / 2,
 * round((h - 323) * 3 / 10))), at the guest layout's integer scale, with
 * "RetroBoot" where Mac OS has its name, "Welcome to RetroBoot" under it
 * first, then "Starting Up..." with a Platinum progress bar showing
 * RetroBoot's own loading progress (/run/retroboot/bootdiag/progress).
 *
 * Art: Mac OS's own box, status font and bar from the private Platinum
 * bundle's installer-startup.rbis (platinum/rbplatinum/startup.py, D012), or
 * RetroBoot's original MIT art with the same geometry. Everything drawing is
 * pure and shared by the initramfs splash and the presenter, so both draw
 * the same pixels and the handoff changes none. Installer mode only
 * (retroboot.mode=installer); installed systems keep the startup indicator
 * of 4.4.1 unchanged.
 */
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "region.h"

struct rb_loop;
struct rb_timer;

/* Mac OS 9.2's startup box (System 'PICT' -16506) in guest pixels. */
#define RB_ISPLASH_BOX_W 422
#define RB_ISPLASH_BOX_H 323
/* Its progress bar (an Appearance Manager bar, 14 px tall with the etch)
 * and the status line, box-relative. */
#define RB_ISPLASH_BAR_X 130
#define RB_ISPLASH_BAR_Y 290
#define RB_ISPLASH_BAR_W 162
#define RB_ISPLASH_BAR_H 14
#define RB_ISPLASH_BAR_STEPS (RB_ISPLASH_BAR_W - 4) /* fill columns */
#define RB_ISPLASH_BAR_MIN 5                         /* the smallest fill drawn */
#define RB_ISPLASH_CAP_W 6                           /* the fill's end: 3 fill columns, the separator, 2 shade */
#define RB_ISPLASH_STATUS_X 4
#define RB_ISPLASH_STATUS_Y 262
#define RB_ISPLASH_STATUS_W 414
#define RB_ISPLASH_STATUS_H 26
#define RB_ISPLASH_STATUS_BASELINE 279
/* The inner panel and the name's place in it (box-relative). */
#define RB_ISPLASH_PANEL_X 36
#define RB_ISPLASH_PANEL_Y 27
#define RB_ISPLASH_PANEL_W 349
#define RB_ISPLASH_PANEL_H 222
#define RB_ISPLASH_NAME_CX 210      /* ink centre of Mac OS's own name */
#define RB_ISPLASH_NAME_BASELINE 225

enum rb_isplash_phase { RB_ISPLASH_WELCOME = 0, RB_ISPLASH_STARTING = 1 };
enum rb_isplash_source { RB_ISPLASH_BUILTIN = 0, RB_ISPLASH_MACOS = 1 };

#define RB_ISPLASH_WELCOME_TEXT "Welcome to RetroBoot"
#define RB_ISPLASH_STARTING_TEXT "Starting Up\xe2\x80\xa6"

/* ---- art ---------------------------------------------------------------- */

struct rb_isplash_strip {
    int x, y, w, h;   /* box-relative */
    uint32_t *px;     /* opaque XRGB, w x h */
};
struct rb_isplash_art {
    int source;                          /* enum rb_isplash_source */
    bool named;                          /* the box carries a pre-rendered name ("WORD") */
    uint32_t *box;                       /* BOX_W x BOX_H: the box with its name */
    struct rb_isplash_strip status[2];   /* indexed by phase */
    uint32_t *bar_empty, *bar_full;      /* BAR_W x BAR_H */
    uint32_t *cap;                       /* CAP_W x (BAR_H - 4): interior rows of the fill's end */
};
/* RetroBoot's original art (MIT): the box's geometry, RetroBoot's compact
 * Mac, "RetroBoot" in RetroBoot System 12, RetroBoot's bar colours. */
int rb_isplash_art_builtin(struct rb_isplash_art *a);
/* The private art file (rbplatinum's installer-startup.rbis, little-endian):
 *   "RBIS" u16 version (1) u16 count, then count entries of
 *   char tag[4], i16 x, i16 y, u32 offset, u32 length -> an RBI1 image
 *   (u16 w, u16 h, RGBA rows; alpha ignored: every piece is opaque).
 * Tags: "BOX " (BOX_W x BOX_H at 0,0), "WORD" (optional: the name, drawn
 * over the box at x,y; without it the built-in name is drawn), "WELC" and
 * "STRT" (status lines, inside the box), "BARE"/"BARF" (the empty and full
 * bar, BAR_W x BAR_H) and "BCAP" (CAP_W x BAR_H - 4). Returns 0, or -1 with
 * `a` zeroed. */
int rb_isplash_art_from_rbis(struct rb_isplash_art *a, const uint8_t *data, size_t n);
/* Load a file for rb_isplash_art_from_rbis; on any failure the built-in art.
 * Returns the source, or -1 when not even the built-in art could be made. */
int rb_isplash_art_load(struct rb_isplash_art *a, const char *path);
void rb_isplash_art_free(struct rb_isplash_art *a);
const char *rb_isplash_source_name(int source);

/* ---- geometry and drawing -------------------------------------------------- */

struct rb_isplash_geom {
    bool valid;
    int k;      /* scale: the guest layout's factor */
    int x, y;   /* the box's top-left on the frame */
    int bx, by; /* the same in guest pixels */
};
/* Mac OS's own placement of its startup box on a w x h screen. */
void rb_isplash_box_origin(int w, int h, int *bx, int *by);
/* On a W x H (logical) frame at pixel scale s, with the configured pixel
 * scale request (0 = auto) and guest target (0, 0 = the 4.5 auto size):
 * the box where Mac OS would draw it on the guest's layout. */
void rb_isplash_geometry(struct rb_isplash_geom *g, int W, int H, int s, int pixel_req, int guest_w, int guest_h);
rb_rect rb_isplash_rect(const struct rb_isplash_geom *g);
rb_rect rb_isplash_status_rect(const struct rb_isplash_geom *g);
rb_rect rb_isplash_bar_rect(const struct rb_isplash_geom *g);
/* Bar fill columns for a progress value (0..1): RB_ISPLASH_BAR_MIN..STEPS. */
int rb_isplash_fill(double v);
/* The box as shown (BOX_W x BOX_H into out): the box, the phase's status
 * line and, while starting, the bar filled `fill` columns (Mac OS's
 * composition: the full bar left of the fill's end, its end, the empty bar). */
void rb_isplash_compose(uint32_t *out, const struct rb_isplash_art *a, int phase, int fill);
/* Draw a composed box onto px (W x H, stride) at g, clipped to clip. */
void rb_isplash_blit(uint32_t *px, int stride_px, int W, int H, rb_rect clip, const struct rb_isplash_geom *g,
                     const uint32_t *composed);

/* ---- progress ---------------------------------------------------------------- */

#define RB_ISPLASH_PROGRESS_FILE "/run/retroboot/bootdiag/progress"
#define RB_ISPLASH_VISIBLE_FILE "/run/retroboot/bootdiag/visible"
#define RB_ISPLASH_WELCOME_MS 1000   /* "Welcome to RetroBoot" stays at least this long */
#define RB_ISPLASH_EXPECT_MS 25000.0 /* no progress file: 85 % at this kernel time */
#define RB_ISPLASH_TAU_MS 300.0      /* the bar eases toward its goal */
#define RB_ISPLASH_TAU_READY_MS 100.0
#define RB_ISPLASH_CREEP_MS 4000.0   /* between stages: up to half way to the next */

/* One line "<permille> <stage-id> <text>\n" (permille 0..1000). Returns 0 and
 * the permille (and stage id, NUL-terminated, when stage is not NULL). */
int rb_isplash_parse_progress(const char *line, size_t n, int *permille, char *stage, size_t stage_n);
/* The next stage's permille after p (boot-diag's contract), 1000 at the end. */
int rb_isplash_next_stage(int permille);

struct rb_isplash_progress {
    bool have;        /* a progress line has been read */
    int permille;     /* the highest value read */
    uint64_t t_stage; /* when it was first read (CLOCK_BOOTTIME ms) */
    double shown;     /* the value drawn (0..1), never decreasing */
    uint64_t t_shown;
};
/* A value read from the file at now (lower values are ignored). */
void rb_isplash_progress_update(struct rb_isplash_progress *p, int permille, uint64_t now);
/* Where the bar is heading at now: the stage, creeping toward the next one;
 * without a file, a kernel-time estimate (linear to 85 % at EXPECT_MS, then
 * toward 95 %). */
double rb_isplash_goal(const struct rb_isplash_progress *p, uint64_t now);
/* Ease `shown` toward the goal up to now (never backwards); returns it. */
double rb_isplash_advance(struct rb_isplash_progress *p, uint64_t now);

/* ---- one process's screen (the splash's or the presenter's) --------------------- */

enum { RB_ISPLASH_DIRTY_STATUS = 1, RB_ISPLASH_DIRTY_BAR = 2, RB_ISPLASH_DIRTY_ALL = 4 };
typedef void (*rb_isplash_redraw_fn)(void *ctx, int what);

struct rb_isplash_run {
    bool on;            /* shown (installer mode, startup not over) */
    bool paused;        /* the display is handed off */
    bool covered;       /* boot-diag's Startup Details are on screen */
    struct rb_isplash_art art;
    struct rb_isplash_progress prog;
    int phase, fill;    /* drawn */
    uint64_t t_welcome; /* "Welcome" was first drawn (CLOCK_BOOTTIME ms) */
    bool drawn;         /* drawn on a frame at least once */
    uint32_t *composed; /* the box as drawn */
    struct rb_timer *timer;
    rb_isplash_redraw_fn redraw;
    void *ctx;
    const char *progress_path, *visible_path;
};
/* retroboot.mode=installer on the kernel command line (NULL = /proc/cmdline);
 * RB_INSTALLER_SPLASH=1|0 overrides (lab). */
bool rb_isplash_installer_mode(const char *cmdline_path);
/* The classic startup box belongs only to classic/mixed installer media.
 * Explicit OS X media keeps its neutral gray preparation scene. */
bool rb_isplash_classic_startup(const char *cmdline_path);
/* Start (on only in installer mode and before boot-diag's "ready"): load the
 * art, read the progress, arm the timer on `loop` (NULL: no timer, tests).
 * Returns r->on. */
bool rb_isplash_run_start(struct rb_isplash_run *r, struct rb_loop *loop, const char *art_path, uint64_t now,
                          rb_isplash_redraw_fn redraw, void *ctx);
/* Advance to now: read the progress and visible files, ease the bar, switch
 * the phase; calls redraw for what changed. */
void rb_isplash_run_tick(struct rb_isplash_run *r, uint64_t now);
void rb_isplash_run_pause(struct rb_isplash_run *r, bool paused);
/* The startup screen is over (the presenter left the checkerboard). */
void rb_isplash_run_end(struct rb_isplash_run *r);
void rb_isplash_run_free(struct rb_isplash_run *r);
/* Draw the box (when on and not covered) onto a frame, clipped. The first
 * draw starts the welcome line's time. */
void rb_isplash_run_scene(struct rb_isplash_run *r, uint32_t *px, int stride_px, int W, int H, rb_rect clip,
                          int s, int pixel_req, int guest_w, int guest_h);
/* The frame rectangle `what` (RB_ISPLASH_DIRTY_*) covers on that frame. */
rb_rect rb_isplash_run_dirty(const struct rb_isplash_run *r, int what, int W, int H, int s, int pixel_req,
                             int guest_w, int guest_h);

/* Handoff (splash -> presenter, splash.h): the state as one line, and its
 * parse. The presenter continues exactly where the splash is. */
#define RB_ISPLASH_RUN_ART "/run/retroboot/installer-startup.rbis" /* the splash's art, for the presenter */
#define RB_ISPLASH_INITRD_ART "/retroboot/installer-startup.rbis"  /* GRUB's extra initrd (private ISO) */
int rb_isplash_state_format(const struct rb_isplash_run *r, char *out, size_t n);
int rb_isplash_state_apply(struct rb_isplash_run *r, const char *line, uint64_t now);
/* Presenter: ask the splash on the handoff socket (splash.h's fd, after
 * "handoff") and continue its state. -1: no answer (an older splash). */
int rb_isplash_handoff_take(struct rb_isplash_run *r, int fd, uint64_t now);
