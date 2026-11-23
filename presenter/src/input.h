/* libinput (udev seat0) + xkbcommon. Translates devices into a small set of
 * callbacks; all routing policy lives in the presenter. */
#pragma once
#include <stdbool.h>
#include <stdint.h>

#include "loop.h"
#include "pointer.h"

/* A touchscreen or tablet (pen) device (touch.h, 4.8). */
struct rb_input_dev {
    char name[64];
    char output[32];   /* udev RETROBOOT_OUTPUT, else WL_OUTPUT: the connector it belongs to ("" = automatic) */
    bool touch;        /* a touchscreen (multitouch) */
    bool tablet;       /* pens / tablet tools */
    bool direct;       /* maps 1:1 onto a display (touchscreens, display tablets) */
    bool usb_external; /* on a USB port not marked fixed: a touch monitor rather than a built-in panel */
};

enum rb_touch_kind { RB_TOUCH_DOWN = 0, RB_TOUCH_MOTION, RB_TOUCH_UP, RB_TOUCH_CANCEL };

#define RB_INPUT_MAX_TOUCH_DEVS 8

struct rb_input_cb {
    /* code: evdev key code. keysym/utf8 from xkb (0/"" when unavailable).
     * mods: RBUI_MOD_* derived from the xkb state after the event. */
    void (*key)(void *data, uint32_t code, bool down, uint32_t keysym, const char *utf8, unsigned mods);
    /* Raw sensor motion is present only for a physical relative mouse. */
    void (*motion)(void *data, double dx, double dy, const struct rb_mouse_motion *mouse);
    void (*motion_abs)(void *data, double x, double y); /* screen pixels */
    void (*button)(void *data, uint32_t code, bool down); /* BTN_* */
    void (*scroll)(void *data, double v120);            /* +down / -up, 120 per notch */
    void (*lid)(void *data, bool open);
    /* Optional: absolute position as a fraction (0..1) of the device's range;
     * direct = a touchscreen (maps 1:1 to its panel's scanout). When set it
     * replaces motion_abs, so the presenter can map through an output. */
    void (*abs_norm)(void *data, double nx, double ny, bool direct);
    /* Touchscreens: every contact change; slot is libinput's seat slot,
     * nx, ny 0..1 of the device's range with its calibration applied
     * (unused for UP and CANCEL). */
    void (*touch)(void *data, const struct rb_input_dev *dev, enum rb_touch_kind kind, int slot, double nx,
                  double ny);
    /* Pens: in proximity at nx, ny (prox false: the pen left); tip 1 down, 0
     * up, -1 unchanged; button (BTN_STYLUS, BTN_STYLUS2, ...) with
     * button_down 1/0, or button 0. */
    void (*tablet)(void *data, const struct rb_input_dev *dev, bool prox, double nx, double ny, int tip,
                   uint32_t button, int button_down);
    /* A touchscreen or tablet was added or removed (rb_input_touch_devices). */
    void (*devices_changed)(void *data);
};

struct rb_input;

struct rb_input *rb_input_new(struct rb_loop *loop, const struct rb_input_cb *cb, void *data);
void rb_input_free(struct rb_input *in);
void rb_input_set_screen(struct rb_input *in, int w, int h);
void rb_input_set_legacy_mouse(struct rb_input *in, bool enabled);
bool rb_input_has_xkb(const struct rb_input *in);
/* Gemini PDA keyboard layer (gemkbd.h): keycap layout (enum rb_gemkbd_layout,
 * -1 keeps it) and input.swap_command_option (an injected Option is the key
 * that reaches Mac OS as Option). */
void rb_input_set_gemini(struct rb_input *in, int layout, bool swap);
/* The Gemini PDA's keyboard layer is active (board and keyboard found). */
bool rb_input_is_gemini(const struct rb_input *in);
/* A touch button (0 primary, 2 secondary) goes down/up: Fn held makes it an
 * Option-click/drag (gemkbd.h); delivers the Option key through the key callback. */
void rb_input_gemini_pointer(struct rb_input *in, int button, bool down);
/* "gemini-us" / "gemini-uk" / "gemini (not found)" on a Gemini PDA, NULL elsewhere. */
const char *rb_input_gemini_keyboard(const struct rb_input *in);
int rb_input_device_count(const struct rb_input *in);
/* The touchscreens and tablets present (at most max). */
int rb_input_touch_devices(const struct rb_input *in, const struct rb_input_dev **out, int max);
/* True once if Escape was already held down when an input device was opened
 * (its press happened before the presenter could see it); clears the flag. */
bool rb_input_take_escape_held(struct rb_input *in);
/* Release libinput's device fds (debug exit); not used in production. */
void rb_input_suspend(struct rb_input *in);
