/* DRM/KMS output: device/connector selection, mode inheritance, dumb buffers,
 * page flips (double buffered) with a single-buffer + DirtyFB fallback. */
#pragma once
#include <stdbool.h>
#include <stdint.h>
#include <sys/types.h>
#include <xf86drmMode.h>

#include "framelog.h"
#include "region.h"
#include "xform.h"

#define RB_MAX_MODES 64
#define RB_MAX_CARDS 8

struct rb_fb {
    uint32_t handle, fb_id, pitch;
    uint64_t size;
    uint8_t *map;
    uint32_t offset; /* byte offset of the scanned-out image in the buffer object */
};

struct rb_output {
    int fd;
    char devnode[64];
    char driver[32];
    dev_t rdev;
    uint32_t conn_id, crtc_id;
    char conn_name[32];
    uint32_t conn_type;
    drmModeModeInfo mode;
    bool mode_inherited;       /* mode was already active on the CRTC */
    bool fw_lit;          /* the firmware left this connector on an active CRTC at open */
    const char *mode_source;   /* "crtc" | "firmware" | "configured" | "preferred" | "set" */
    drmModeModeInfo boot_mode; /* active mode when first opened (for "auto") */
    bool have_boot_mode;
    int w, h, refresh;
    int mm_w, mm_h;
    int nmodes;
    drmModeModeInfo modes[RB_MAX_MODES];
    struct rb_fb fb[2];
    int nfb;
    struct rb_fb retired[2]; /* previous buffers kept until the next modeset completes */
    int nretired;
    int front;   /* buffer on screen, -1 before the first present */
    int pending; /* buffer being flipped to, -1 if none */
    bool flip_ok;
    bool on_screen;
    bool need_modeset;
    rb_region stale[2];        /* in frame (shadow) coordinates */
    bool full[2];              /* buffer needs a complete redraw (borders too) */
    /* Multi-output: the presenter's frame as this output shows it (rotation
     * for the panel orientation; mirrors scaled and centered). */
    struct rb_xform xf;
    int rot;                   /* clockwise content rotation: panel_rot, plus the user rotation on the primary */
    int panel_rot;             /* the panel orientation alone (rotation.h) */
    char orientation[32];      /* "panel orientation" property ("" = none) */
    bool internal;             /* eDP / LVDS / DSI */
    bool shared_fd;            /* second head on another output's DRM file */
    bool off;                  /* DPMS off: nothing is presented */
    uint32_t dpms_prop;
    uint64_t presents;
    uint64_t first_present_mono_ms;
    /* Opt-in observation identity, assigned immediately before a primary
     * submission. With one pending flip these remain its owner until callback.
     * Zero means this output was not bound to a presenter/guest observation. */
    uint64_t trace_presenter_epoch_ms;
    uint32_t trace_guest_pid;
    /* Display updates that stall (Startup Details, ARCHITECTURE 16): a page
     * flip still pending after a limit counts once (rb_output_check_stall);
     * a blocking modeset that took longer than 1 s counts as a slow commit. */
    uint64_t pending_mono_ms;  /* when the pending flip was queued */
    bool pending_stalled;      /* the pending flip was counted */
    uint32_t flip_stalls, slow_commits;
    uint64_t stall_first_mono_ms, stall_last_mono_ms, slow_commit_max_ms;
    int last_errno;
    /* Kernel damage (drivers that copy each committed buffer into fixed scanout
     * memory, geminipda-drm): atomic page flips carry FB_DAMAGE_CLIPS, the region
     * of the new buffer that may differ from what the panel shows. kdamage[i] is
     * what was copied into buffer i (frame coordinates) since buffer i was last
     * committed successfully, kfull[i] that its next commit must copy all of it
     * (rb_output_stage, rb_output_committed; tests/test_damage.c). */
    bool atomic_damage;
    uint32_t plane_id, prop_fb_id, prop_damage;
    rb_region kdamage[2];
    bool kfull[2];
    uint64_t damage_commits, damage_full, damage_px; /* atomic commits, full ones, pixels in their clips */
    /* Lab: a virtual output (rb_output_open with "virtual:WxH[@HZ]") has no
     * DRM device. Its buffers are memory, `fd` is a timerfd that completes
     * each page flip at the next refresh, and with rb_virtual_record set every
     * presented frame goes to a frame log (framelog.h). */
    bool virt;
    uint64_t virt_epoch_us, virt_period_us;
    struct rb_framelog *virt_log;
};

/* Frame log path for virtual outputs (main's --record); NULL: none. */
extern const char *rb_virtual_record;

/* User rotation (rotation.h) that rb_output_open gives the output it opens
 * (the primary): 0, 90, 180 or 270. Heads get their panel orientation only. */
extern int rb_user_rotation;

/* Card device nodes with KMS and a connected connector, best first. */
int rb_drm_list_cards(char out[][64], int max);
int rb_drm_list_cards_for_primary(char out[][64], int max, bool external);
/* Open + become master + choose connector/CRTC/mode + allocate buffers.
 * want_conn (may be NULL) prefers a connector by name; want_mode (may be
 * NULL) is {w, h, refresh} overriding mode inheritance. Nothing is shown
 * until rb_output_flush(). devnode "virtual:WxH" or "virtual:WxH@HZ" opens a
 * virtual output instead (lab: no display needed; see `virt`). */
int rb_output_open(struct rb_output *o, const char *devnode, const char *want_conn, const int *want_mode, char *err,
                   size_t errlen);
/* want_conn may also be "internal" or "external": prefer that kind of
 * connector (the primary display). */

/* Logical (rotated) size of an output. */
static inline int rb_output_lw(const struct rb_output *o) { return o->rot == 90 || o->rot == 270 ? o->h : o->w; }
static inline int rb_output_lh(const struct rb_output *o) { return o->rot == 90 || o->rot == 270 ? o->w : o->h; }

/* Connected connectors of `card`'s device (ids), at most max. */
int rb_output_connected(const struct rb_output *card, uint32_t *ids, int max);
/* Open another connector of card's device as a second head on the same DRM
 * file. busy_crtcs: CRTC ids already used by other heads (n of them). */
int rb_output_open_head(struct rb_output *o, const struct rb_output *card, uint32_t conn_id,
                        const uint32_t *busy_crtcs, int n_busy, char *err, size_t errlen);
/* Release a head: switch its CRTC off (frees it) and its buffers. */
void rb_output_close_head(struct rb_output *o);
/* Release an output without another commit on its CRTC: buffers that may be
 * on screen are closed (CLOSEFB), never removed (a removal disables the
 * CRTC, which on a stalled pipe waits out the driver's flip timeout). */
void rb_output_abandon(struct rb_output *o);
/* Is the CRTC scanning out? 1: active but its vblank counter stood still
 * for ~120 ms (stalled); 0: running; negative errno: unknown (CRTC off,
 * DPMS off, no vblank support). */
int rb_output_scanout_stalled(const struct rb_output *o);
/* Whether an output must never be committed to: a DSI panel the firmware did
 * not light (GPD MicroPC after a cold start with HDMI attached: the VBT needs
 * GPIOs ACPI doesn't describe, so Linux can't power the panel on either), or
 * whose pipe runs without the panel scanning out. A commit on that pipe waits
 * out i915's 10 s flip timeout and is never completed, so every later commit
 * touching the pipe, its plane or the connector waits 10 s for it
 * (systemd-backlight, the presenter, and i915's own shutdown: three more 10 s
 * waits before power-off). Only asked with another output to show the frame.
 * fw_lit: rb_output.fw_lit; scanout: rb_output_scanout_stalled() (1 stalled). */
bool rb_output_never_commit(uint32_t conn_type, bool fw_lit, int scanout);
/* Frame the output shows (the presenter's shadow): 1:1 for the primary,
 * scaled and centered for mirrors. Marks the output for a full redraw. */
int rb_output_set_frame(struct rb_output *o, int frame_w, int frame_h, bool mirror);
/* A mode of o's connector that shows a frame_w x frame_h frame at 1x or more
 * (logical, after rotation): the preferred mode when it does, else the
 * largest mode that does; NULL when none does. */
const drmModeModeInfo *rb_output_mode_for_frame(const struct rb_output *o, int frame_w, int frame_h);
/* Mirror whose current mode cannot show the frame at 1x: switch to
 * rb_output_mode_for_frame (reprobing the connector once when its cached list
 * has none, e.g. an EDID not yet read) and re-frame. Returns 1 when the mode
 * changed, 0 when nothing to do, -errno on failure. */
int rb_output_fit_mirror(struct rb_output *o, int frame_w, int frame_h);
/* Border around a mirrored frame (black or the continued checkerboard). */
void rb_output_set_border(struct rb_output *o, int border, int checker_s, struct rb_checker_anchor anchor);
/* DPMS on/off (lid). Off outputs are not presented; on redraws everything. */
int rb_output_set_power(struct rb_output *o, bool on);
void rb_output_close(struct rb_output *o);
/* Switch to another mode from the connector list (real modeset). */
int rb_output_set_mode(struct rb_output *o, const drmModeModeInfo *mode);
/* An internal panel left at a scaled (non-native) mode switches to its native
 * one, as rb_output_open does for a primary. 1: changed, 0: nothing, -errno. */
int rb_output_use_native(struct rb_output *o);
const drmModeModeInfo *rb_output_find_mode(const struct rb_output *o, int w, int h, int refresh);
/* Re-read connector state after a hotplug uevent. Returns 1 if our connector
 * is still connected, 0 if it went away, <0 on device errors. */
int rb_output_reprobe(struct rb_output *o);

void rb_output_damage(struct rb_output *o, rb_rect r);
void rb_output_damage_all(struct rb_output *o);
/* Copy stale regions from the shadow and present. Returns 1 if a frame was
 * presented, 0 if nothing to do or a flip is pending, -errno on failure. */
int rb_output_flush(struct rb_output *o, const uint32_t *shadow, int stride_px);
bool rb_output_needs_flush(const struct rb_output *o);
/* A page flip pending for longer than limit_ms (the driver did not complete
 * it: amdgpu's "flip_done timed out" on a panel stuck in self refresh) is
 * counted once in flip_stalls. Returns true when a new stall was counted. */
bool rb_output_check_stall(struct rb_output *o, uint64_t now_mono_ms, uint64_t limit_ms);
/* Read DRM events (page flip completion). */
void rb_output_dispatch(struct rb_output *o);
/* Close the output but leave the scanned-out framebuffer on screen
 * (DRM_IOCTL_MODE_CLOSEFB, Linux 6.8+), so the next owner inherits the
 * picture instead of a disabled CRTC. Falls back to a normal close. */
void rb_output_leave(struct rb_output *o);
/* Async-signal-safe variant for crash handlers: detaches the front buffer
 * from the file only. */
void rb_output_leave_signal_safe(const struct rb_output *o);
int rb_output_drop_master(struct rb_output *o);
int rb_output_set_master(struct rb_output *o);
bool rb_output_is_master(const struct rb_output *o);

int rb_mode_refresh(const drmModeModeInfo *m);

/* Boot framebuffer size from the firmware/GRUB screen_info in
 * /sys/kernel/boot_params/data (x86): orig_video_isVGA @0x0f must be
 * VIDEO_TYPE_VLFB (0x23) or VIDEO_TYPE_EFI (0x70); lfb_width @0x12 and
 * lfb_height @0x14 are u16 little-endian. Used as the inherited mode when the
 * KMS driver cannot read back the CRTC state (bochs-drm, virtio-gpu). */
int rb_screen_info_size(const uint8_t *buf, size_t n, int *w, int *h);
int rb_boot_fb_size(const char *path, int *w, int *h);
#define RB_BOOT_PARAMS "/sys/kernel/boot_params/data"
/* (Not on arm64: /sys/kernel/boot_params is x86 only, so rb_boot_fb_size fails
 * and the connector's preferred mode, capped below on a Raspberry Pi, is used.) */

/* --- Kernel damage (FB_DAMAGE_CLIPS; tests/test_damage.c) ---
 *
 * geminipda-drm (Gemini PDA) is a shadow-plane driver: every atomic commit copies
 * the new framebuffer into the LK bootloader's fixed scanout memory, scoped by
 * the plane's FB_DAMAGE_CLIPS (Linux 6.6 drm_atomic_helper_damage_iter_init:
 * clips are honoured when the plane's source rectangle is unchanged, even when
 * the framebuffer changes) and otherwise the full 1080x2160 frame, 9.3 MB. A
 * legacy page flip (drmModePageFlip) never carries clips, so it copies the whole
 * frame every time. On such a driver the presenter flips with atomic commits
 * carrying clips. Which clips: a double-buffered presenter flips to buffer B
 * whose content was last shown two commits ago; the regions copied into B since
 * then (the last two frames' damage) are what may differ between B and what the
 * panel shows -- the kernel copies from B only, so the frame damage alone is
 * enough for this driver's single scanout, but not for a driver that uploads per
 * buffer (virtio-gpu) or one that double-buffers inside the kernel (the planned
 * geminipda "flip1": "copy = this commit's damage + the previous commit's").
 * kdamage is kept until a commit succeeds, so a refused flip loses nothing, and
 * any event that leaves the scanout unknown (a modeset, a new buffer, DPMS, a
 * master change) makes the next commit full. */

/* Should this driver's flips carry FB_DAMAGE_CLIPS through atomic commits?
 * geminipda-drm yes; others no (their legacy flips stay as they are).
 * RB_DRM_DAMAGE=atomic forces it on any driver, =legacy off (lab). */
bool rb_driver_damage_atomic(const char *driver);
/* FB_DAMAGE_CLIPS (struct drm_mode_rect: x1, y1, x2, y2) for a commit: the
 * region `kd` (frame coordinates) through the output transform, clipped to the
 * scanout. Returns the number of clips; *full is set when the whole frame must be
 * copied (kfull, or more clips than max), and an empty region gives one 1x1 clip
 * (the buffer is current everywhere, so any pixel of it is right). */
struct drm_mode_rect;
int rb_damage_clips(const struct rb_xform *xf, const rb_region *kd, bool kfull, struct drm_mode_rect *out, int max,
                    bool *full);
/* Copy buffer idx's stale regions from the shadow, add them to its kernel damage
 * and clear its stale regions: the buffer then shows the current frame. */
void rb_output_stage(struct rb_output *o, int idx, const uint32_t *shadow, int stride_px);
/* Buffer idx's commit succeeded (ok) or was refused: its kernel damage is cleared
 * only when the kernel took it. */
void rb_output_committed(struct rb_output *o, int idx, bool ok);
/* The scanout no longer shows a known buffer: both buffers' next commits copy everything. */
void rb_output_kernel_damage_all(struct rb_output *o);

/* --- Device and mode choice, as pure functions (tests/test_drm_select.c) --- */

/* What rb_drm_list_cards learns about a card from sysfs. */
struct rb_card_info {
    const char *node;      /* /dev/dri/cardN */
    const char *driver;    /* kernel driver of the card's parent device ("vc4-drm", "v3d", "i915", NULL) */
    int connected;         /* connectors whose status is "connected" */
    int unknown;           /* ... "unknown" (not probed yet, or no detection: composite) */
    bool hdmi_connected;   /* one of the connected ones is HDMI-A */
    bool boot_vga;         /* PCI boot_vga = 1 */
};
/* Ranking of a card: >= 50 means it may show something. A render-only device
 * (Raspberry Pi v3d: no connectors) scores below 50 and is never opened. A
 * firmware framebuffer driver (simpledrm, simple-framebuffer, efidrm...) ranks
 * below a real KMS driver. A connected Pi 5 RP1 DSI/DPI panel ranks first for
 * an internal primary; an external primary keeps vc4 HDMI first. */
int rb_drm_card_score(const struct rb_card_info *ci);
int rb_drm_card_score_for_primary(const struct rb_card_info *ci, bool external);
/* RP1 panel cards are separate from Pi 5 HDMI; composite is not an internal panel. */
bool rb_driver_rpi_panel(const char *driver);
/* Sort cards by score (best first; ties by node name) and copy the nodes of
 * those scoring >= 50 to out. Returns how many. */
int rb_drm_rank_cards(const struct rb_card_info *cards, int n, char out[][64], int max);
int rb_drm_rank_cards_for_primary(const struct rb_card_info *cards, int n, char out[][64], int max, bool external);

/* Connector choice within a card: should a connector (type_a, type id_a) with
 * score_a replace the best so far (type_b, id_b, score_b)? A higher score wins;
 * on a Raspberry Pi (driver vc4) a tie goes to the lower-numbered HDMI port
 * (HDMI-A-1 before HDMI-A-2) whatever the order the kernel lists them in.
 * Elsewhere a tie keeps the first, as before. */
bool rb_conn_better(const char *driver, int score_a, uint32_t type_a, uint32_t id_a, int score_b, uint32_t type_b,
                    uint32_t id_b);

/* Drivers that cannot read back the firmware's display state (every CRTC is
 * off when Linux opens the card), so "the firmware did not light it" says
 * nothing about a DSI panel there: Raspberry Pi vc4 and RP1, the Gemini PDA's
 * geminipda-drm. */
bool rb_driver_no_readout(const char *driver);

/* Largest mode the presenter chooses by itself on this driver ("auto", no
 * host_mode): 1920x1080 (or 1080x1920) on a Raspberry Pi (vc4), where every
 * frame is scaled in software and competes with the emulator for the CPU;
 * none elsewhere. RB_AUTO_MODE_MAX=WxH overrides (0 disables; lab). Returns
 * true and the cap when there is one. */
bool rb_auto_mode_cap(const char *driver, int *cap_w, int *cap_h);
bool rb_mode_within_cap(int w, int h, int cap_w, int cap_h);
/* The mode to use under a cap: the preferred mode when it is within the cap;
 * else exactly cap_w x cap_h (60 Hz first, then the refresh nearest 60,
 * progressive only), else the largest mode within the cap. NULL if none. */
const drmModeModeInfo *rb_mode_pick_capped(const drmModeModeInfo *modes, int n, const drmModeModeInfo *preferred,
                                           int cap_w, int cap_h);

