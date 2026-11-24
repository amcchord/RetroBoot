/* RetroBoot's startup indicator (ARCHITECTURE.md 4.4.1).
 *
 * While the checkerboard waits for Mac OS, the Happy Mac stands exactly where
 * Mac OS will draw it: a 32 x 32 icon at guest pixel ((w - 32) / 2,
 * (h - 32) / 2) (measured on the real guest at nine sizes, odd ones
 * included), mapped through the guest's integer layout, at the guest's own
 * scale. It is Mac OS's own icon, pixel for pixel (captured from the guest's
 * first startup screen, or the Platinum bundle's identical copy), except its
 * screen: there a small Platinum progress bar of RetroBoot's own startup
 * replaces the face, and the green light blinks like a drive light. The bar
 * completes only at the cut-over to genuine Mac OS pixels, where it fades
 * into Mac OS's smile. RetroBoot's original art (same geometry) is only the
 * fallback before any copy exists.
 *
 * Everything here is pure (no DRM, no I/O except the explicit loaders) and is
 * shared by GRUB's background (--write-checkerboard), the initramfs splash and
 * the presenter, so all three draw the same pixels.
 */
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "checker.h"
#include "region.h"

enum rb_startup_style { RB_STARTUP_CLASSIC = 0, RB_STARTUP_OSX = 1 };
#define RB_OSX_BOOT_GRAY 0x00c0c0c0u /* measured on original Jaguar and Panther BootX */

#define RB_ICON_SIZE 32
/* The compact Mac's screen interior (the Happy Mac's face area) and the bar,
 * in icon pixels. The bar is 15 x 5 with a black 1-px frame, centred in the
 * 17 x 13 screen with one pixel of screen around it horizontally. */
#define RB_ICON_SCREEN_X 7
#define RB_ICON_SCREEN_Y 4
#define RB_ICON_SCREEN_W 17
#define RB_ICON_SCREEN_H 13
#define RB_BAR_X 8
#define RB_BAR_Y 8
#define RB_BAR_W 15
#define RB_BAR_H 5
#define RB_BAR_STEPS (RB_BAR_W - 2) /* fill granularity: whole icon pixels */

enum rb_icon_source { RB_ICON_BUILTIN = 0, RB_ICON_MACOS = 1 }; /* "builtin", "macos" (the exact icon) */

/* 32 x 32 sprite, 0xffRRGGBB opaque or 0 transparent; the green light's
 * pixels and their colour while it is off (blink). */
#define RB_ICON_LIGHT_MAX 16
struct rb_icon_art {
    uint32_t px[RB_ICON_SIZE * RB_ICON_SIZE];
    int source;
    int nlight;
    uint16_t light[RB_ICON_LIGHT_MAX];
    uint32_t light_off[RB_ICON_LIGHT_MAX];
};
#define RB_BLINK_MS 500 /* the light: 500 ms on, 500 ms off (1 Hz) */

const char *rb_icon_source_name(int source);
/* RetroBoot's own faceless compact Mac (original MIT pixel art). */
void rb_icon_art_builtin(struct rb_icon_art *a);
/* Mac OS's Happy Mac as an RBI file ("RBI1", u16 width, u16 height
 * little-endian, then RGBA rows): the copy captured from the guest's own
 * startup screen (rb_icon_capture_rbi) or the Platinum bundle's
 * icons/overview-32.rbi (System cicn -20024, identical apart from the face),
 * both private (D012). Checks the compact-Mac structure (silhouette, the
 * screen bevel around the face) and paints the screen interior with its own
 * colour, which removes the face. Returns 0, or -1 leaving `a` untouched. */
int rb_icon_art_from_rbi(struct rb_icon_art *a, const uint8_t *data, size_t n);
/* Mac OS's startup background around its icon rectangle at (ix, iy) of a
 * guest frame: bg[0] is the rectangle's corner pixel (outside the icon's
 * silhouette); bg[1] is the other colour of a 1-pixel 50 % dither -- Mac OS's
 * startup background on a built-in LCD (VGA builtin-panel) -- when the
 * pixels left of, above and diagonally before the corner continue one, else
 * the corner colour again (the plain startup gray). The background at
 * (ix + dx, iy + dy) is rb_startup_bg_at(bg, dx, dy). */
void rb_startup_bg(const uint32_t *px, int stride_px, int ix, int iy, uint32_t bg[2]);
static inline uint32_t rb_startup_bg_at(const uint32_t bg[2], int dx, int dy) { return bg[(dx + dy) & 1]; }
/* Capture Mac OS's startup icon from a guest frame (w x h, XRGB): the
 * 32 x 32 rectangle at ((w-32)/2, (h-32)/2) must be the compact-Mac
 * silhouette on Mac OS's startup background (rb_startup_bg: the startup
 * gray, or the built-in LCD's dither), with the structure
 * rb_icon_art_from_rbi accepts. Writes the RBI (face included, alpha = the
 * silhouette) into out (RB_ICON_RBI_BYTES). Returns 0 or -1. */
#define RB_ICON_RBI_BYTES (8 + RB_ICON_SIZE * RB_ICON_SIZE * 4)
int rb_icon_capture_rbi(const uint32_t *px, int stride_px, int w, int h, uint8_t *out);
/* Art already checked elsewhere (the splash's, at the handoff): the pixels
 * and source as given, the light found in them. */
void rb_icon_art_set(struct rb_icon_art *a, const uint32_t px[RB_ICON_SIZE * RB_ICON_SIZE], int source);
/* Load a file for rb_icon_art_from_rbi; on any failure `a` is the built-in
 * art. Returns the resulting source. */
int rb_icon_art_load(struct rb_icon_art *a, const char *path);
/* The sprite with the bar drawn into its screen (fill 0..RB_BAR_STEPS) and the
 * green light on or off. */
void rb_icon_compose(uint32_t out[RB_ICON_SIZE * RB_ICON_SIZE], const struct rb_icon_art *a, int fill, bool lit);
/* Light on at time t of a blink that started (on) at t0. */
bool rb_startup_lit(uint64_t t0, uint64_t t);

/* Geometry on the presenter's frame. */
struct rb_startup_geom {
    bool valid;
    int k;              /* guest layout factor: Mac OS draws its icon at k x k per pixel */
    int land_x, land_y; /* where Mac OS's own 32 x 32 icon lands on screen (scale k) */
    int q;              /* indicator scale: k (the Happy Mac's own size) */
    int x, y;           /* indicator top-left */
    int gx, gy;         /* Mac OS's picture's top-left (the guest target's layout): the checkerboard's anchor */
};
/* W x H frame, effective pixel scale s, the configured pixel scale request
 * (0 = auto; it limits the guest factor exactly as the guest layout does) and
 * the guest target (0,0 = the 4.5 auto size). The indicator is the Happy Mac
 * at the guest's scale: a DPI-scaled checkerboard does not change it. */
/* Indicator scale for the Happy Mac during startup: doubles crisp 1x icons on
 * large >= 1280 displays (and shrinks cleanly at landing) so it stays visible. */
static inline int rb_startup_boot_q(int W, int H, int k)
{
    return (k == 1 && (W >= 1280 || H >= 1280) && W >= 720 && H >= 720) ? 2 : k;
}
/* Compute g->k, landing (where Mac OS draws) and the indicator's own rect. */
void rb_startup_geometry(struct rb_startup_geom *g, int W, int H, int s, int pixel_req, int guest_w, int guest_h);
/* The checkerboard's anchor for this layout (checker.h): black at Mac OS's
 * pixel (0, 0), so on a built-in LCD the pattern is Mac OS's own startup
 * dither wherever its cell is the guest's pixel. */
static inline struct rb_checker_anchor rb_startup_anchor(const struct rb_startup_geom *g)
{
    return (struct rb_checker_anchor){g->gx, g->gy};
}
/* The startup scene (what GRUB's background, the splash and the presenter's
 * checkerboard view draw), clipped to `clip` of a W x H frame: the
 * checkerboard at pattern scale cs anchored at g's Mac OS picture, then, with
 * `indicator`, the startup indicator. */
void rb_startup_scene(uint32_t *px, int stride_px, int W, int H, rb_rect clip, int cs, const struct rb_startup_geom *g,
                      const struct rb_icon_art *a, bool indicator, int fill, bool lit);
/* OS X preboot is its plain 75% gray. Apple logo/spinner/progress come only from
 * the running guest; classic Happy Mac art is never overlaid on an OS X boot. */
void rb_startup_scene_style(uint32_t *px, int stride_px, int W, int H, rb_rect clip, int cs,
                            const struct rb_startup_geom *g, const struct rb_icon_art *a,
                            bool indicator, int fill, bool lit, int style);
/* The local panel uses the same neutral background in logical pixels. */
void rb_startup_panel_background(uint32_t *px, int stride_px, int W, int H, int cm, int scale, int style);
/* The same landing at another indicator scale q (>= 1), same centre. */
struct rb_startup_geom rb_startup_geom_at(const struct rb_startup_geom *g, int q);
rb_rect rb_startup_rect(const struct rb_startup_geom *g);
rb_rect rb_startup_bar_rect(const struct rb_startup_geom *g);
/* The screen interior (where the face is; the cut-over fades it). */
rb_rect rb_startup_screen_rect(const struct rb_startup_geom *g);
/* Bounding rectangle of the green light (empty without one). */
rb_rect rb_startup_light_rect(const struct rb_startup_geom *g, const struct rb_icon_art *a);
/* Bar steps shown for progress p (0..1). */
int rb_startup_fill(double p);
/* Draw the indicator over px (W x H, the background already drawn), clipped
 * to `clip`. */
void rb_startup_draw(uint32_t *px, int stride_px, int W, int H, rb_rect clip, const struct rb_startup_geom *g,
                     const struct rb_icon_art *a, int fill, bool lit);
/* For a rotated scanout (GRUB's background on a panel mounted sideways):
 * draw the indicator laid out on the logical (rotated) frame into the scanout
 * buffer fb_w x fb_h. rot is the clockwise content rotation (0/90/180/270,
 * presenter xform convention: 90 -> fx = oh - 1 - ly, fy = lx). */
void rb_startup_draw_rotated(uint32_t *fb, int stride_px, int fb_w, int fb_h, int rot,
                             const struct rb_startup_geom *g, const struct rb_icon_art *a, int fill, bool lit);

/* DPI-aware checkerboard (4.4). The pattern pixel is c = s * m physical
 * pixels: m >= 1 makes the pattern's effective DPI (display DPI / c) closest
 * to 72-110 (ties: smaller m), so a dense panel still shows a checkerboard
 * instead of a flat gray, and every pattern cell stays whole Mac pixels.
 * Unknown or implausible physical sizes give m = 1. W x H and mm_w x mm_h
 * may be in either orientation (the diagonal is used). */
int rb_checker_multiple(int W, int H, int s, int mm_w, int mm_h);
/* Physical size of a built-in panel whose connector reports none (DSI panels
 * without EDID): the smallest possible DMI quirk table, matched exactly on
 * sys_vendor and product_name in dmi_dir (NULL = /sys/class/dmi/id) and on
 * the panel's resolution (either orientation). RB_PANEL_MM=WxH overrides
 * (lab). Returns true and the size when known. */
bool rb_panel_mm_quirk(const char *dmi_dir, int W, int H, int *mm_w, int *mm_h);

/* The physical size the DPI rule uses for a connector: the connector's own
 * (EDID/firmware), else rb_panel_mm_quirk for a built-in panel (eDP, LVDS,
 * DSI, DPI); RB_PANEL_MM=WxH overrides both (lab). Returns "drm", "quirk",
 * "override" or NULL (unknown; *out = 0). */
bool rb_connector_internal(uint32_t conn_type);
const char *rb_display_mm(uint32_t conn_type, int w, int h, int mm_w, int mm_h, int *out_w, int *out_h);

/* Progress model. Time is any millisecond clock (the splash and presenter
 * use CLOCK_BOOTTIME). From (t0, p0) the value moves linearly to
 * RB_PROGRESS_KNEE at the expected cut-over `eta`, then approaches
 * RB_PROGRESS_CAP asymptotically (same slope at the knee) when the startup
 * runs long; it never reaches 1 on its own: only the cut-over completes it.
 * Untimed models stay at p0. */
#define RB_PROGRESS_KNEE 0.85
#define RB_PROGRESS_CAP 0.95
#define RB_PROGRESS_TAU_MS 15000.0 /* overdue: time constant of the approach to the cap */
struct rb_progress {
    bool timed;
    uint64_t t0;
    double p0;
    uint64_t eta;
};
double rb_progress_at(const struct rb_progress *pr, uint64_t t);
/* Start over at 0 (a new attempt); eta 0 = untimed. */
void rb_progress_reset(struct rb_progress *pr, uint64_t now, uint64_t eta);
/* New expected cut-over: re-anchored at the current value (never backwards). */
void rb_progress_retarget(struct rb_progress *pr, uint64_t now, uint64_t eta);
/* Earliest time after `now` at which the shown bar steps (floor(p * steps))
 * increase; now + horizon when that does not happen within horizon ms. */
uint64_t rb_progress_next_step(const struct rb_progress *pr, uint64_t now, int steps, uint64_t horizon);

/* Startup parameters GRUB passes on the kernel command line (from
 * rb-bootdisplay's display.cfg): retroboot.progress=on|off,
 * retroboot.guest_size=WxH|auto, retroboot.checker_scale=N,
 * retroboot.startup_ms=N (learned kernel time of the cut-over). */
struct rb_boot_startup {
    int style;            /* enum rb_startup_style; absent = classic */
    int progress;         /* -1 absent, 0 off, 1 on */
    int guest_w, guest_h; /* 0 = auto */
    int checker_scale;    /* 0 = absent */
    uint64_t startup_ms;  /* 0 = absent */
};
void rb_cmdline_startup(const char *cmdline, struct rb_boot_startup *o);
/* rb_cmdline_startup of a file (NULL = /proc/cmdline). */
void rb_boot_startup(const char *path, struct rb_boot_startup *o);
/* Where the initramfs finds the Platinum-derived art GRUB loaded as an extra
 * initrd (rb-bootdisplay's startup.cpio). */
#define RB_STARTUP_ICON_INITRD "/retroboot/startup-icon.rbi"
