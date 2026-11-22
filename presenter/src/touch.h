/* Touchscreens and pens (ARCHITECTURE 4.8): gestures, and the pure helpers
 * that bind a touch device to an output.
 *
 * rb_gesture turns the contacts of a touchscreen into pointer actions. The
 * presenter maps every contact through the output the device belongs to
 * first (rb_xform_fb_point_to_frame: rotation, mirror scale, letterbox), so
 * positions here are frame pixels and the pointer lands on the pixel under
 * the finger. Mac-like and simple:
 *
 *   - one finger: the pointer goes under it. A tap clicks there; a drag
 *     presses there and drags. The press waits until the finger has moved
 *     beyond `slop` or lifted, so the touch can still become a long press or
 *     a two-finger gesture;
 *   - a long press (still for RB_TOUCH_LONG_MS): the secondary button goes
 *     down (right-click: Control-click in Mac OS, a contextual menu) and is
 *     released when the finger lifts, so sliding to a menu item and lifting
 *     chooses it;
 *   - two fingers that lift within RB_TOUCH_TAP_MS without moving: a
 *     secondary click where the first finger is; two fingers moving
 *     together: scrolling (pan, which the presenter turns into wheel notches,
 *     content following the fingers);
 *   - a tap within RB_TOUCH_DOUBLE_MS and 2 x slop of the previous one lands
 *     on exactly the same pixel, so a double-tap is a double-click.
 *
 * With gestures off, a touch is a plain primary button: down where it lands,
 * drag, up. Extra fingers are ignored once a gesture has been decided. */
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define RB_TOUCH_SLOTS 16
#define RB_TOUCH_LONG_MS 650
#define RB_TOUCH_TAP_MS 450
#define RB_TOUCH_DOUBLE_MS 500
/* Two-finger scrolling: one wheel notch (USB Overdrive: 3 lines) per this
 * many Mac pixels of finger travel. */
#define RB_TOUCH_NOTCH_PX 48

enum rb_gesture_state {
    RB_G_IDLE = 0,
    RB_G_PENDING, /* one finger, undecided */
    RB_G_DRAG,    /* primary button held, following the finger */
    RB_G_LONG,    /* secondary button held (long press), following the finger */
    RB_G_TWO,     /* two fingers, undecided (tap or scroll) */
    RB_G_SCROLL,  /* two fingers scrolling */
    RB_G_DONE,    /* decided and finished: wait until every finger is up */
    RB_G_DIRECT,  /* gestures off: primary button held */
};

struct rb_gesture_cb {
    void (*point)(void *data, double x, double y);      /* move the pointer (frame pixels) */
    void (*button)(void *data, int button, bool down);   /* 0 primary, 2 secondary */
    void (*pan)(void *data, double dx, double dy);       /* two-finger scroll (frame pixels) */
};

struct rb_gesture {
    bool enabled;
    double slop; /* frame pixels */
    struct rb_gesture_cb cb;
    void *data;
    enum rb_gesture_state st;
    struct {
        bool down;
        double x, y, x0, y0;
    } c[RB_TOUCH_SLOTS];
    int ndown;
    int primary;         /* slot of the first finger */
    uint64_t t0;         /* first finger down */
    uint64_t long_at;    /* long-press deadline (PENDING), 0 = none */
    double px, py;       /* where the first finger went down (after double-tap snapping) */
    double cx, cy;       /* two-finger centroid at the last pan */
    bool have_tap;
    double tap_x, tap_y; /* last tap */
    uint64_t tap_t;
    bool held[3];        /* buttons this recognizer holds down */
};

void rb_gesture_init(struct rb_gesture *g, const struct rb_gesture_cb *cb, void *data);
void rb_gesture_configure(struct rb_gesture *g, bool enabled, double slop);
void rb_gesture_down(struct rb_gesture *g, int slot, double x, double y, uint64_t t_ms);
void rb_gesture_motion(struct rb_gesture *g, int slot, double x, double y, uint64_t t_ms);
void rb_gesture_up(struct rb_gesture *g, int slot, uint64_t t_ms);
/* The device was lost or the touch cancelled: release what is held, forget
 * every contact. */
void rb_gesture_cancel(struct rb_gesture *g);
/* Work due at t (the long press). Returns the ms until the next deadline, or
 * -1 when none is pending. */
int rb_gesture_tick(struct rb_gesture *g, uint64_t t_ms);
const char *rb_gesture_state_name(enum rb_gesture_state s);

/* Touchscreen axes before the output mapping (input_config "touch_transform",
 * RB_TOUCH_TRANSFORM): "swap-xy", "invert-x", "invert-y", comma-separated, applied
 * in that order like the device tree's touchscreen-swapped-x-y and -inverted-x/-y,
 * for a unit whose controller is mounted differently from what its kernel says.
 * parse: flags (0 = as reported), -1 for an unknown word. */
enum { RB_TOUCH_SWAP_XY = 1, RB_TOUCH_INVERT_X = 2, RB_TOUCH_INVERT_Y = 4 };
int rb_touch_transform_parse(const char *s);
void rb_touch_transform_apply(int flags, double *nx, double *ny);
const char *rb_touch_transform_name(int flags, char *buf, size_t n);

/* Wheel notches for a two-finger pan of dy frame pixels at k frame pixels per
 * Mac pixel: content follows the fingers (fingers up = scroll down = positive
 * v120, the presenter's wheel convention). acc carries the remainder. */
double rb_touch_pan_v120(double dy, double k);

/* EDID monitor name (descriptor 0xFC), trimmed; "" when absent or the block
 * is not an EDID. Returns the length. */
size_t rb_edid_monitor_name(const uint8_t *edid, size_t n, char *out, size_t outlen);

/* Which output a touch device belongs to (4.8). Candidates are the
 * connected outputs; returns the index of the chosen one:
 *   1. the device's udev RETROBOOT_OUTPUT or WL_OUTPUT names a connector
 *      ("internal" / "external" also work);
 *   2. the device name contains a monitor's EDID name (4+ characters,
 *      case-insensitive): a touch monitor and its display;
 *   3. a USB touch device with an external display connected: that display
 *      (touch monitors are USB; built-in touchscreens are I2C/platform);
 *   4. the built-in panel, else the primary (index 0). */
struct rb_touch_output {
    const char *connector;
    const char *edid_name;
    bool internal;
};
int rb_touch_pick_output(const struct rb_touch_output *outs, int n, const char *want, const char *dev_name,
                         bool usb);
