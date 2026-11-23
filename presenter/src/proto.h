/* Control-protocol request parsing (pure functions, unit tested). */
#pragma once
#include <stdbool.h>
#include <stddef.h>

#include "cJSON.h"
#include "scale.h"

enum rb_scene { RB_SCENE_SPLASH = 0, RB_SCENE_GUEST, RB_SCENE_PANEL, RB_SCENE_BLACK };
enum rb_reveal { RB_REVEAL_FIRST_CONTENT = 0, RB_REVEAL_NOW };
enum rb_panel_bg { RB_PANEL_BG_CHECKERBOARD = 0, RB_PANEL_BG_GUEST_DIMMED, RB_PANEL_BG_BLACK };

const char *rb_scene_name(enum rb_scene s);
const char *rb_panel_bg_name(enum rb_panel_bg b);
const char *rb_scaling_name(enum rb_scaling s);

struct rb_scene_req {
    enum rb_scene scene;
    enum rb_reveal reveal;
    enum rb_panel_bg background;
    bool classic_guest;
};

struct rb_display_req {
    bool has_scaling;
    enum rb_scaling scaling;
    bool has_guest_size;
    int guest_w, guest_h; /* 0,0 = auto */
    bool has_host_mode;
    int host_w, host_h, host_refresh; /* 0 = auto */
    bool has_enforce;
    bool enforce; /* enforce_guest_size */
    bool has_pixel_scale;
    int pixel_scale; /* 0 = auto, 1..RB_PIXEL_SCALE_MAX */
    bool has_primary;
    bool primary_external; /* "primary": "internal" (default) | "external" */
    bool has_rotation;
    int rotation; /* user rotation of the primary: 0, 90, 180, 270 (rotation.h) */
};

/* display_power: {"output": "internal"|"external"|"all"|<connector>, "on": bool} */
/* "osd" (osd.h): kind 0 volume, 1 mute, 2 brightness (enum rb_osd_kind). */
struct rb_osd_req {
    int kind;
    double level;   /* 0..1 */
    bool muted;
};

struct rb_power_req {
    char output[32];
    bool on;
};

struct rb_input_req {
    bool has_swap;
    bool swap;
    bool has_chord;
    int chord; /* evdev trigger */
    bool has_speed;
    double speed;
    bool has_legacy_mouse;
    bool legacy_mouse;
    bool has_mouse_dpi;
    int mouse_dpi;
    bool has_touch_gestures;
    bool touch_gestures; /* touch.h: tap, drag, two-finger tap / long press, two-finger scroll */
    bool has_keyboard_layout;
    int keyboard_layout; /* gemkbd.h: the Gemini PDA's keycaps, "us" | "uk" */
    bool has_touch_transform;
    int touch_transform; /* touch.h RB_TOUCH_* flags */
};

int rb_parse_set_scene(const cJSON *req, struct rb_scene_req *out, char *err, size_t errlen);
int rb_parse_display_config(const cJSON *req, struct rb_display_req *out, char *err, size_t errlen);
int rb_parse_input_config(const cJSON *req, struct rb_input_req *out, char *err, size_t errlen);
int rb_parse_display_power(const cJSON *req, struct rb_power_req *out, char *err, size_t errlen);
int rb_parse_osd(const cJSON *req, struct rb_osd_req *out, char *err, size_t errlen);
