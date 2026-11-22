/* Startup Details (ARCHITECTURE 16): the diagnostics view shared by the three
 * programs that can draw it -- rb-bootdiag on the firmware framebuffer, the
 * initramfs splash and the presenter on a KMS device -- and the files they
 * exchange in /run/retroboot/bootdiag/ (/run survives switch_root).
 *
 *   progress  "<permille> <stage-id> <text>\n", written by rb-bootdiag only
 *             (the installer splash's progress bar reads it; never decreases)
 *   visible   exists while the view covers the screen; created/removed by
 *             whoever owns Escape (rb-bootdiag until the presenter has input)
 *   view      the view's content (rb_diag_doc_parse), written by rb-bootdiag
 *   hint      exists while "Press Esc for startup details" should be shown
 *             over the startup scene
 *   presenter the presenter's state for rb-bootdiag (key=value lines)
 *   state     rb-bootdiag's own state across switch_root (bootdiag.c)
 *
 * The picture depends only on the frame size, the pixel scale and the view
 * document, so all three drawers produce the same pixels: the view is laid
 * out on the logical frame (W/s x H/s) and every logical pixel is an s x s
 * block (tests/test_diagview.c). Fonts: RetroBoot's MIT bitmap fonts
 * (presenter/ui/src/fonts_data.c). Libc only. */
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <sys/types.h>

#include "region.h"

#define RB_DIAG_DIR "/run/retroboot/bootdiag"
#define RB_DIAG_PROGRESS RB_DIAG_DIR "/progress"
#define RB_DIAG_VISIBLE RB_DIAG_DIR "/visible"
#define RB_DIAG_VIEW RB_DIAG_DIR "/view"
#define RB_DIAG_HINT RB_DIAG_DIR "/hint"
#define RB_DIAG_PRESENTER RB_DIAG_DIR "/presenter"
#define RB_DIAG_STATE RB_DIAG_DIR "/state"

/* Kernel command line: on by default in installer mode (retroboot.mode=
 * installer), opt-in elsewhere (retroboot.diag=1); retroboot.diag=0 turns it
 * off everywhere. retroboot.diag=show opens the view at once (GRUB's startup
 * option); retroboot.diag_fb=0 never draws on the firmware framebuffer. */
struct rb_diag_cfg {
    bool enabled;
    bool installer;
    bool show;     /* open the view as soon as something can draw it */
    bool fb;       /* rb-bootdiag may draw on the firmware framebuffer */
};
void rb_diag_cmdline(const char *cmdline, struct rb_diag_cfg *c);
/* rb_diag_cmdline of /proc/cmdline (RB_DIAG_CMDLINE overrides, for tests). */
void rb_diag_boot_cfg(struct rb_diag_cfg *c);

/* What Escape does in the presenter. visible: the view is up; preboot: the
 * checkerboard scene (splash, or guest before the reveal); display: the
 * presenter has a display it shows something on. */
enum rb_diag_esc {
    RB_DIAG_ESC_NONE = 0, /* not ours: route as before (panel, guest) */
    RB_DIAG_ESC_PREBOOT,  /* the installed system's startup options (preboot_key) */
    RB_DIAG_ESC_SHOW,
    RB_DIAG_ESC_HIDE,
};
enum rb_diag_esc rb_diag_escape(bool diag_enabled, bool visible, bool preboot, bool display);
/* Return while the view is up: on an installed system still on the
 * checkerboard it closes the view and opens the startup options (what Escape
 * opens there without Startup Details); otherwise it is swallowed like any key. */
bool rb_diag_return_opens_options(bool installer, bool visible, bool preboot);

/* ---- the view document -------------------------------------------------
 * Text, one item per line, "<tag>\t<fields>" (tabs separate fields):
 *   T <title>
 *   H <header line>
 *   S <ok|fail|wait|skip> <time text> <label>      startup checklist
 *   P <problem>                                    wrapped
 *   D <display / input line>
 *   L <log line>                                   newest last
 *   F <footer: where the report was saved>
 * Unknown tags are ignored; lines are cut at RB_DIAG_LINE bytes. */
#define RB_DIAG_LINE 240
#define RB_DIAG_MAX_LINES 160
enum rb_diag_state { RB_DIAG_OK, RB_DIAG_FAIL, RB_DIAG_WAIT, RB_DIAG_SKIP };
struct rb_diag_line {
    char tag;
    int state;           /* S lines */
    char time[16];       /* S lines */
    char text[RB_DIAG_LINE];
};
struct rb_diag_doc {
    char title[64];
    int n;
    struct rb_diag_line line[RB_DIAG_MAX_LINES];
};
void rb_diag_doc_parse(const char *text, size_t len, struct rb_diag_doc *d);
/* Serialize (the inverse of parse); returns the length written (truncated to cap). */
size_t rb_diag_doc_format(const struct rb_diag_doc *d, char *out, size_t cap);
/* Builder helpers (rb-bootdiag). */
void rb_diag_doc_init(struct rb_diag_doc *d, const char *title);
void rb_diag_doc_add(struct rb_diag_doc *d, char tag, const char *text);
void rb_diag_doc_stage(struct rb_diag_doc *d, int state, const char *time, const char *label);

/* ---- rendering ---------------------------------------------------------- */

/* The whole view: an XRGB8888 frame of w x h (stride in pixels) at pixel
 * scale s (1..8; the frame is drawn as (w/s) x (h/s) logical pixels, each
 * s x s, and the right/bottom remainder continues the background). */
void rb_diag_render(uint32_t *px, int w, int h, int stride, int s, const struct rb_diag_doc *d);
/* The same with the footer's keys line (NULL: RB_DIAG_KEYS_BACK). Only the
 * presenter, which handles Return, offers RB_DIAG_KEYS_OPTIONS. */
void rb_diag_render_keys(uint32_t *px, int w, int h, int stride, int s, const struct rb_diag_doc *d,
                         const char *keys);
#define RB_DIAG_KEYS_BACK "Esc: back"
#define RB_DIAG_KEYS_OPTIONS "Return: Startup Options   Esc: back"
/* Where the one-line hint ("Press Esc for startup details") goes on a w x h
 * frame at scale s (frame pixels), and its pixels drawn into that rectangle
 * of px (anything around it is left alone). */
rb_rect rb_diag_hint_rect(int w, int h, int s);
void rb_diag_render_hint(uint32_t *px, int w, int h, int stride, int s);
#define RB_DIAG_HINT_TEXT "Press Esc for startup details"

/* Text width in pixels of the view's fonts (layout tests). */
enum rb_diag_font { RB_DIAG_FONT_REGULAR, RB_DIAG_FONT_SMALL, RB_DIAG_FONT_SYSTEM };
int rb_diag_text_width(enum rb_diag_font f, const char *s);

/* ---- the drawers' side (splash, presenter) ------------------------------ */

struct rb_diag_watch {
    struct rb_diag_cfg cfg;
    int fd;          /* inotify on RB_DIAG_DIR (-1: not enabled) */
    bool visible, hint;
    struct rb_diag_doc *doc;
    uint64_t doc_gen; /* bumped whenever the document changed */
};
enum { RB_DIAG_CHG_VISIBLE = 1, RB_DIAG_CHG_DOC = 2, RB_DIAG_CHG_HINT = 4 };
/* Reads the command line; when enabled creates RB_DIAG_DIR (0755) and
 * watches it. Returns the fd to poll, or -1 (diag off: nothing changes). */
int rb_diag_watch_init(struct rb_diag_watch *w);
/* After the fd was readable (and once after init): re-reads what changed;
 * returns RB_DIAG_CHG_* bits. */
int rb_diag_watch_update(struct rb_diag_watch *w);
void rb_diag_watch_free(struct rb_diag_watch *w);
/* The presenter shows/hides the view (creates/removes `visible`). */
int rb_diag_set_visible(bool on);

/* Display updates that stall, as a drawer saw them (the presenter, the
 * splash): RB_DIAG_DIR/flips-<who>, key=value lines with times in boot ms.
 * stalls: page flips still pending after 1 s; slow_commits: blocking
 * modesets that took longer than 1 s (slow_max_ms the longest). Times are
 * given in CLOCK_MONOTONIC ms and written as CLOCK_BOOTTIME ms. */
struct rb_diag_flips {
    unsigned stalls, slow_commits;
    uint64_t first_mono_ms, last_mono_ms, slow_max_ms;
    char connector[32], driver[32];
};
void rb_diag_publish_flips(const char *who, const struct rb_diag_flips *f);
/* The presenter's heartbeat for rb-bootdiag: touches RB_DIAG_PRESENTER, so
 * a presenter stuck in the kernel (a commit waiting for a stalled flip) is
 * told apart from an idle one. */
void rb_diag_heartbeat(void);
#define RB_DIAG_HEARTBEAT_MS 1000

/* ---- small helpers ------------------------------------------------------ */

/* Write a file atomically (temp + rename in the same directory). */
int rb_diag_write_atomic(const char *path, const char *data, size_t len);
/* Read up to cap-1 bytes and NUL-terminate; returns the length or -1. */
ssize_t rb_diag_read_file(const char *path, char *buf, size_t cap);
