#include "input.h"

#include <errno.h>
#include <fcntl.h>
#include <libinput.h>
#include <stdarg.h>
#include <libudev.h>
#include <linux/input.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>
#include <xkbcommon/xkbcommon.h>

#include "gemkbd.h"
#include "log.h"
#include "rbui.h"

struct rb_input {
    struct udev *udev;
    struct libinput *li;
    struct rb_watch *watch;
    struct xkb_context *xkb;
    struct xkb_keymap *keymap;
    struct xkb_state *state;
    struct rb_input_cb cb;
    void *data;
    int screen_w, screen_h;
    int devices;
    bool legacy_mouse_enabled;
    struct libinput_device *motion_device;
    double mouse_dpi; /* zero for other devices, such as a touchpad */
    /* touchscreens and tablets (touch.h): what the presenter binds to outputs */
    struct {
        struct libinput_device *dev;
        struct rb_input_dev info;
    } tdev[RB_INPUT_MAX_TOUCH_DEVS];
    int ntdev;
    double finger_acc;
    bool esc_held; /* Escape was already down when a device was opened */
    /* Gemini PDA built-in keyboard (gemkbd.h): on when the device tree says
     * planet,gemini-pda unless RB_GEMINI_KEYBOARD=off; its devices carry
     * &gemkbd as libinput user data. */
    bool gemini;
    struct rb_gemkbd gemkbd;
    int gemkbd_devices;
};

#define KEY_LONGS ((KEY_MAX + 8 * sizeof(unsigned long)) / (8 * sizeof(unsigned long)))

static int open_restricted(const char *path, int flags, void *user)
{
    struct rb_input *in = user;
    int fd = open(path, flags | O_CLOEXEC);
    if (fd < 0)
        return -errno;
    /* A key held since before the presenter started (Escape held through the
     * checkerboard) produced its press event before this open: libinput never
     * reports it and drops the kernel's autorepeat, so read the key state once. */
    unsigned long keys[KEY_LONGS] = {0};
    if (in && ioctl(fd, EVIOCGKEY(sizeof keys), keys) >= 0 &&
        ((keys[KEY_ESC / (8 * sizeof(unsigned long))] >> (KEY_ESC % (8 * sizeof(unsigned long)))) & 1UL))
        in->esc_held = true;
    return fd;
}

static void close_restricted(int fd, void *user)
{
    (void)user;
    close(fd);
}

static const struct libinput_interface iface = {.open_restricted = open_restricted,
                                                .close_restricted = close_restricted};

static void li_log(struct libinput *li, enum libinput_log_priority p, const char *fmt, va_list args)
{
    (void)li;
    if (p < LIBINPUT_LOG_PRIORITY_ERROR)
        return;
    char buf[512];
    vsnprintf(buf, sizeof buf, fmt, args);
    size_t n = strlen(buf);
    if (n && buf[n - 1] == '\n')
        buf[n - 1] = 0;
    rb_log("libinput: %s", buf);
}

static char *trim_value(char *v)
{
    while (*v == ' ' || *v == '"' || *v == '\'')
        v++;
    size_t n = strlen(v);
    while (n && (v[n - 1] == '\n' || v[n - 1] == '"' || v[n - 1] == '\'' || v[n - 1] == ' '))
        v[--n] = 0;
    return v;
}

static void load_keymap(struct rb_input *in)
{
    in->xkb = xkb_context_new(XKB_CONTEXT_NO_FLAGS);
    if (!in->xkb) {
        rb_log("input: xkb context unavailable; panel uses built-in US mapping");
        return;
    }
    char model[64] = "", layout[64] = "", variant[64] = "", options[128] = "";
    FILE *f = fopen("/etc/default/keyboard", "re");
    if (f) {
        char line[256];
        while (fgets(line, sizeof line, f)) {
            char *eq = strchr(line, '=');
            if (!eq)
                continue;
            *eq = 0;
            char *v = trim_value(eq + 1);
            if (!strcmp(line, "XKBMODEL"))
                snprintf(model, sizeof model, "%s", v);
            else if (!strcmp(line, "XKBLAYOUT"))
                snprintf(layout, sizeof layout, "%s", v);
            else if (!strcmp(line, "XKBVARIANT"))
                snprintf(variant, sizeof variant, "%s", v);
            else if (!strcmp(line, "XKBOPTIONS"))
                snprintf(options, sizeof options, "%s", v);
        }
        fclose(f);
    }
    if (in->gemini) {
        /* The Gemini layer (gemkbd.h) turns its keycaps into U.S. positions, so the
         * panel reads them with the U.S. layout whatever /etc/default/keyboard says. */
        snprintf(layout, sizeof layout, "us");
        variant[0] = options[0] = 0;
    }
    struct xkb_rule_names names = {
        .rules = NULL,
        .model = model[0] ? model : NULL,
        .layout = layout[0] ? layout : "us",
        .variant = variant[0] ? variant : NULL,
        .options = options[0] ? options : NULL,
    };
    in->keymap = xkb_keymap_new_from_names(in->xkb, &names, XKB_KEYMAP_COMPILE_NO_FLAGS);
    if (!in->keymap) {
        rb_log("input: keymap '%s' failed to compile; panel uses built-in US mapping", names.layout);
        return;
    }
    in->state = xkb_state_new(in->keymap);
    rb_log("input: xkb keymap layout '%s'", names.layout);
}

static unsigned current_mods(struct rb_input *in)
{
    if (!in->state)
        return 0;
    unsigned m = 0;
    if (xkb_state_mod_name_is_active(in->state, XKB_MOD_NAME_SHIFT, XKB_STATE_MODS_EFFECTIVE) > 0)
        m |= RBUI_MOD_SHIFT;
    if (xkb_state_mod_name_is_active(in->state, XKB_MOD_NAME_CTRL, XKB_STATE_MODS_EFFECTIVE) > 0)
        m |= RBUI_MOD_CTRL;
    if (xkb_state_mod_name_is_active(in->state, XKB_MOD_NAME_ALT, XKB_STATE_MODS_EFFECTIVE) > 0)
        m |= RBUI_MOD_OPTION;
    if (xkb_state_mod_name_is_active(in->state, XKB_MOD_NAME_LOGO, XKB_STATE_MODS_EFFECTIVE) > 0)
        m |= RBUI_MOD_COMMAND;
    return m;
}

static void copy_prop(char *out, size_t n, struct udev_device *d, const char *name)
{
    const char *v = d ? udev_device_get_property_value(d, name) : NULL;
    if (v && *v && !out[0])
        snprintf(out, n, "%s", v);
}

/* A touchscreen or tablet: its name, output binding hints (4.8). */
static void add_touch_device(struct rb_input *in, struct libinput_device *dev)
{
    bool touch = libinput_device_has_capability(dev, LIBINPUT_DEVICE_CAP_TOUCH);
    bool tablet = libinput_device_has_capability(dev, LIBINPUT_DEVICE_CAP_TABLET_TOOL);
    if ((!touch && !tablet) || in->ntdev >= RB_INPUT_MAX_TOUCH_DEVS)
        return;
    struct rb_input_dev *i = &in->tdev[in->ntdev].info;
    memset(i, 0, sizeof *i);
    in->tdev[in->ntdev].dev = dev;
    snprintf(i->name, sizeof i->name, "%s", libinput_device_get_name(dev));
    i->touch = touch;
    i->tablet = tablet;
    /* touchscreens are direct; tablets are when they map onto a display
     * (libinput offers a calibration matrix only for direct devices) */
    i->direct = touch || libinput_device_config_calibration_has_matrix(dev);
    struct udev_device *ud = libinput_device_get_udev_device(dev);
    copy_prop(i->output, sizeof i->output, ud, "RETROBOOT_OUTPUT");
    copy_prop(i->output, sizeof i->output, ud, "WL_OUTPUT");
    struct udev_device *usb = ud ? udev_device_get_parent_with_subsystem_devtype(ud, "usb", "usb_device") : NULL;
    if (usb) {
        const char *rm = udev_device_get_sysattr_value(usb, "removable");
        i->usb_external = !rm || strcmp(rm, "fixed");
    }
    if (ud)
        udev_device_unref(ud);
    in->ntdev++;
    rb_log("input: %s '%s' (%s%s%s)", touch ? "touchscreen" : "tablet", i->name, i->direct ? "direct" : "indirect",
           i->usb_external ? ", USB" : "", i->output[0] ? ", output from udev" : "");
    if (in->cb.devices_changed)
        in->cb.devices_changed(in->data);
}

static void remove_touch_device(struct rb_input *in, struct libinput_device *dev)
{
    for (int k = 0; k < in->ntdev; k++)
        if (in->tdev[k].dev == dev) {
            in->tdev[k] = in->tdev[--in->ntdev];
            if (in->cb.devices_changed)
                in->cb.devices_changed(in->data);
            return;
        }
}

static const struct rb_input_dev *touch_info(struct rb_input *in, struct libinput_device *dev)
{
    for (int k = 0; k < in->ntdev; k++)
        if (in->tdev[k].dev == dev)
            return &in->tdev[k].info;
    static const struct rb_input_dev none = {.direct = true};
    return &none;
}

static void configure_device(struct rb_input *in, struct libinput_device *dev)
{
    if (libinput_device_config_tap_get_finger_count(dev) > 0) {
        libinput_device_config_tap_set_enabled(dev, LIBINPUT_CONFIG_TAP_ENABLED);
        libinput_device_config_tap_set_drag_enabled(dev, LIBINPUT_CONFIG_DRAG_ENABLED);
    }
    char caps[64] = "";
    static const struct {
        enum libinput_device_capability c;
        const char *n;
    } names[] = {{LIBINPUT_DEVICE_CAP_KEYBOARD, "keyboard"}, {LIBINPUT_DEVICE_CAP_POINTER, "pointer"},
                 {LIBINPUT_DEVICE_CAP_TOUCH, "touch"},       {LIBINPUT_DEVICE_CAP_TABLET_TOOL, "tablet"},
                 {LIBINPUT_DEVICE_CAP_SWITCH, "switch"}};
    for (size_t i = 0; i < sizeof names / sizeof names[0]; i++)
        if (libinput_device_has_capability(dev, names[i].c)) {
            if (caps[0])
                strncat(caps, ",", sizeof caps - strlen(caps) - 1);
            strncat(caps, names[i].n, sizeof caps - strlen(caps) - 1);
        }
    in->devices++;
    rb_log("input: device added '%s' [%s]", libinput_device_get_name(dev), caps);
    if (in->gemini && libinput_device_has_capability(dev, LIBINPUT_DEVICE_CAP_KEYBOARD) &&
        rb_gemini_keyboard_device(libinput_device_get_name(dev), libinput_device_get_id_bustype(dev))) {
        libinput_device_set_user_data(dev, &in->gemkbd);
        in->gemkbd_devices++;
        rb_log("input: '%s' is the Gemini PDA keyboard (%s keycaps, Fn layer)", libinput_device_get_name(dev),
               rb_gemkbd_layout_name(in->gemkbd.layout));
    }
    add_touch_device(in, dev);
}

static void deliver_key(struct rb_input *in, uint32_t code, bool down)
{
    uint32_t sym = 0;
    char utf8[16] = "";
    if (in->state) {
        xkb_keycode_t kc = code + 8;
        if (down) {
            sym = xkb_state_key_get_one_sym(in->state, kc);
            xkb_state_key_get_utf8(in->state, kc, utf8, sizeof utf8);
        }
        xkb_state_update_key(in->state, kc, down ? XKB_KEY_DOWN : XKB_KEY_UP);
    }
    /* Control characters are not text. */
    if ((unsigned char)utf8[0] < 0x20 || utf8[0] == 0x7f)
        utf8[0] = 0;
    if (in->cb.key)
        in->cb.key(in->data, code, down, sym, utf8, current_mods(in));
}

static void handle_key(struct rb_input *in, struct libinput_event_keyboard *k, struct libinput_device *dev)
{
    uint32_t code = libinput_event_keyboard_get_key(k);
    bool down = libinput_event_keyboard_get_key_state(k) == LIBINPUT_KEY_STATE_PRESSED;
    if (dev && libinput_device_get_user_data(dev) == &in->gemkbd) {
        /* The Gemini's keycaps and Fn layer (gemkbd.h), before anything else. */
        struct rb_gemkbd_ev ev[RB_GEMKBD_MAX_EV];
        int n = rb_gemkbd_key(&in->gemkbd, code, down, ev);
        for (int i = 0; i < n; i++)
            deliver_key(in, ev[i].code, ev[i].down);
        return;
    }
    uint32_t seat_count = libinput_event_keyboard_get_seat_key_count(k);
    /* Same key on two keyboards: only the first press / last release. */
    if ((down && seat_count != 1) || (!down && seat_count != 0))
        return;
    deliver_key(in, code, down);
}

static bool legacy_mouse_sample(struct rb_input *in, struct libinput_event *ev,
                                struct libinput_event_pointer *p, struct rb_mouse_motion *motion)
{
    if (!in->legacy_mouse_enabled)
        return false;
    struct libinput_device *device = libinput_event_get_device(ev);
    if (device != in->motion_device) {
        in->motion_device = device;
        in->mouse_dpi = 0;
        struct udev_device *ud = libinput_device_get_udev_device(device);
        if (ud) {
            if (udev_device_get_property_value(ud, "ID_INPUT_MOUSE") &&
                !udev_device_get_property_value(ud, "ID_INPUT_TOUCHPAD") &&
                !udev_device_get_property_value(ud, "ID_INPUT_POINTINGSTICK"))
                in->mouse_dpi = rb_mouse_dpi(udev_device_get_property_value(ud, "MOUSE_DPI"));
            udev_device_unref(ud);
        }
    }
    if (!in->mouse_dpi)
        return false;
    *motion = (struct rb_mouse_motion){libinput_event_pointer_get_dx_unaccelerated(p),
                                      libinput_event_pointer_get_dy_unaccelerated(p), in->mouse_dpi};
    return true;
}

void rb_input_set_legacy_mouse(struct rb_input *in, bool enabled)
{
    if (in && enabled != in->legacy_mouse_enabled) {
        in->legacy_mouse_enabled = enabled;
        in->motion_device = NULL;
    }
}

static void handle_event(struct rb_input *in, struct libinput_event *ev)
{
    enum libinput_event_type type = libinput_event_get_type(ev);
    switch (type) {
    case LIBINPUT_EVENT_DEVICE_ADDED:
        configure_device(in, libinput_event_get_device(ev));
        break;
    case LIBINPUT_EVENT_DEVICE_REMOVED:
        in->devices--;
        if (in->motion_device == libinput_event_get_device(ev))
            in->motion_device = NULL;
        rb_log("input: device removed '%s'", libinput_device_get_name(libinput_event_get_device(ev)));
        remove_touch_device(in, libinput_event_get_device(ev));
        break;
    case LIBINPUT_EVENT_KEYBOARD_KEY:
        handle_key(in, libinput_event_get_keyboard_event(ev), libinput_event_get_device(ev));
        break;
    case LIBINPUT_EVENT_POINTER_MOTION: {
        struct libinput_event_pointer *p = libinput_event_get_pointer_event(ev);
        if (in->cb.motion) {
            struct rb_mouse_motion legacy;
            bool have_legacy = legacy_mouse_sample(in, ev, p, &legacy);
            in->cb.motion(in->data, libinput_event_pointer_get_dx(p), libinput_event_pointer_get_dy(p),
                          have_legacy ? &legacy : NULL);
        }
        break;
    }
    case LIBINPUT_EVENT_POINTER_MOTION_ABSOLUTE: {
        struct libinput_event_pointer *p = libinput_event_get_pointer_event(ev);
        if (in->cb.abs_norm)
            in->cb.abs_norm(in->data, libinput_event_pointer_get_absolute_x_transformed(p, 1),
                            libinput_event_pointer_get_absolute_y_transformed(p, 1), false);
        else if (in->cb.motion_abs && in->screen_w > 0)
            in->cb.motion_abs(in->data, libinput_event_pointer_get_absolute_x_transformed(p, (uint32_t)in->screen_w),
                              libinput_event_pointer_get_absolute_y_transformed(p, (uint32_t)in->screen_h));
        break;
    }
    case LIBINPUT_EVENT_POINTER_BUTTON: {
        struct libinput_event_pointer *p = libinput_event_get_pointer_event(ev);
        bool down = libinput_event_pointer_get_button_state(p) == LIBINPUT_BUTTON_STATE_PRESSED;
        uint32_t count = libinput_event_pointer_get_seat_button_count(p);
        if ((down && count != 1) || (!down && count != 0))
            break;
        if (in->cb.button)
            in->cb.button(in->data, libinput_event_pointer_get_button(p), down);
        break;
    }
    case LIBINPUT_EVENT_POINTER_SCROLL_WHEEL: {
        struct libinput_event_pointer *p = libinput_event_get_pointer_event(ev);
        if (libinput_event_pointer_has_axis(p, LIBINPUT_POINTER_AXIS_SCROLL_VERTICAL) && in->cb.scroll)
            in->cb.scroll(in->data,
                          libinput_event_pointer_get_scroll_value_v120(p, LIBINPUT_POINTER_AXIS_SCROLL_VERTICAL));
        break;
    }
    case LIBINPUT_EVENT_POINTER_SCROLL_FINGER:
    case LIBINPUT_EVENT_POINTER_SCROLL_CONTINUOUS: {
        struct libinput_event_pointer *p = libinput_event_get_pointer_event(ev);
        if (libinput_event_pointer_has_axis(p, LIBINPUT_POINTER_AXIS_SCROLL_VERTICAL) && in->cb.scroll) {
            /* ~15 units of finger travel per wheel notch */
            double v = libinput_event_pointer_get_scroll_value(p, LIBINPUT_POINTER_AXIS_SCROLL_VERTICAL);
            in->cb.scroll(in->data, v * 8.0);
        }
        break;
    }
    case LIBINPUT_EVENT_TOUCH_DOWN:
    case LIBINPUT_EVENT_TOUCH_MOTION:
    case LIBINPUT_EVENT_TOUCH_UP:
    case LIBINPUT_EVENT_TOUCH_CANCEL: {
        struct libinput_event_touch *t = libinput_event_get_touch_event(ev);
        const struct rb_input_dev *d = touch_info(in, libinput_event_get_device(ev));
        int slot = type == LIBINPUT_EVENT_TOUCH_CANCEL ? -1 : libinput_event_touch_get_seat_slot(t);
        double nx = 0, ny = 0;
        if (type == LIBINPUT_EVENT_TOUCH_DOWN || type == LIBINPUT_EVENT_TOUCH_MOTION) {
            nx = libinput_event_touch_get_x_transformed(t, 1);
            ny = libinput_event_touch_get_y_transformed(t, 1);
        }
        enum rb_touch_kind k = type == LIBINPUT_EVENT_TOUCH_DOWN     ? RB_TOUCH_DOWN
                               : type == LIBINPUT_EVENT_TOUCH_MOTION ? RB_TOUCH_MOTION
                               : type == LIBINPUT_EVENT_TOUCH_UP     ? RB_TOUCH_UP
                                                                     : RB_TOUCH_CANCEL;
        if (in->cb.touch)
            in->cb.touch(in->data, d, k, slot, nx, ny);
        break;
    }
    case LIBINPUT_EVENT_TABLET_TOOL_AXIS:
    case LIBINPUT_EVENT_TABLET_TOOL_PROXIMITY:
    case LIBINPUT_EVENT_TABLET_TOOL_TIP:
    case LIBINPUT_EVENT_TABLET_TOOL_BUTTON: {
        struct libinput_event_tablet_tool *t = libinput_event_get_tablet_tool_event(ev);
        const struct rb_input_dev *d = touch_info(in, libinput_event_get_device(ev));
        bool prox = type != LIBINPUT_EVENT_TABLET_TOOL_PROXIMITY ||
                    libinput_event_tablet_tool_get_proximity_state(t) == LIBINPUT_TABLET_TOOL_PROXIMITY_STATE_IN;
        int tip = type == LIBINPUT_EVENT_TABLET_TOOL_TIP
                      ? libinput_event_tablet_tool_get_tip_state(t) == LIBINPUT_TABLET_TOOL_TIP_DOWN
                      : -1;
        uint32_t button = 0;
        int bdown = -1;
        if (type == LIBINPUT_EVENT_TABLET_TOOL_BUTTON) {
            button = libinput_event_tablet_tool_get_button(t);
            bdown = libinput_event_tablet_tool_get_button_state(t) == LIBINPUT_BUTTON_STATE_PRESSED;
        }
        double nx = libinput_event_tablet_tool_get_x_transformed(t, 1);
        double ny = libinput_event_tablet_tool_get_y_transformed(t, 1);
        if (in->cb.tablet)
            in->cb.tablet(in->data, d, prox, nx, ny, tip, button, bdown);
        else if (in->cb.abs_norm && prox)
            in->cb.abs_norm(in->data, nx, ny, d->direct);
        break;
    }
    case LIBINPUT_EVENT_SWITCH_TOGGLE: {
        struct libinput_event_switch *s = libinput_event_get_switch_event(ev);
        if (libinput_event_switch_get_switch(s) == LIBINPUT_SWITCH_LID && in->cb.lid)
            in->cb.lid(in->data, libinput_event_switch_get_switch_state(s) == LIBINPUT_SWITCH_STATE_OFF);
        break;
    }
    default:
        break;
    }
}

static void input_ready(void *data, int fd, uint32_t events)
{
    (void)fd;
    (void)events;
    struct rb_input *in = data;
    if (libinput_dispatch(in->li) < 0)
        rb_log("input: libinput_dispatch failed");
    struct libinput_event *ev;
    while ((ev = libinput_get_event(in->li))) {
        handle_event(in, ev);
        libinput_event_destroy(ev);
    }
}

struct rb_input *rb_input_new(struct rb_loop *loop, const struct rb_input_cb *cb, void *data)
{
    struct rb_input *in = calloc(1, sizeof *in);
    if (!in)
        return NULL;
    in->cb = *cb;
    in->data = data;
    const char *gk = getenv("RB_GEMINI_KEYBOARD"); /* us | uk | off (lab) */
    in->gemini = rb_gemini_board(getenv("RB_DEVICE_TREE_COMPATIBLE")) && !(gk && !strcmp(gk, "off"));
    rb_gemkbd_init(&in->gemkbd, rb_gemkbd_layout_parse(gk) == RB_GEMKBD_UK ? RB_GEMKBD_UK : RB_GEMKBD_US, false);
    if (in->gemini)
        rb_log("input: Gemini PDA: built-in keyboard layer on (%s keycaps)", rb_gemkbd_layout_name(in->gemkbd.layout));
    load_keymap(in);
    in->udev = udev_new();
    if (!in->udev)
        goto fail;
    in->li = libinput_udev_create_context(&iface, in, in->udev);
    if (!in->li)
        goto fail;
    libinput_log_set_handler(in->li, li_log);
    libinput_log_set_priority(in->li, LIBINPUT_LOG_PRIORITY_ERROR);
    if (libinput_udev_assign_seat(in->li, "seat0") < 0) {
        rb_log("input: cannot assign seat0");
        goto fail;
    }
    in->watch = rb_loop_watch(loop, libinput_get_fd(in->li), EPOLLIN, input_ready, in);
    if (!in->watch)
        goto fail;
    input_ready(in, -1, 0);
    rb_log("input: libinput seat0 with %d devices", in->devices);
    return in;
fail:
    rb_log("input: initialisation failed");
    rb_input_free(in);
    return NULL;
}

void rb_input_free(struct rb_input *in)
{
    if (!in)
        return;
    if (in->watch)
        rb_watch_remove(in->watch);
    if (in->li)
        libinput_unref(in->li);
    if (in->udev)
        udev_unref(in->udev);
    if (in->state)
        xkb_state_unref(in->state);
    if (in->keymap)
        xkb_keymap_unref(in->keymap);
    if (in->xkb)
        xkb_context_unref(in->xkb);
    free(in);
}

void rb_input_set_screen(struct rb_input *in, int w, int h)
{
    if (!in)
        return;
    in->screen_w = w;
    in->screen_h = h;
}

bool rb_input_has_xkb(const struct rb_input *in) { return in && in->state; }

void rb_input_set_gemini(struct rb_input *in, int layout, bool swap)
{
    if (!in)
        return;
    const char *gk = getenv("RB_GEMINI_KEYBOARD"); /* the lab override wins */
    if (gk && rb_gemkbd_layout_parse(gk) >= 0)
        layout = rb_gemkbd_layout_parse(gk);
    rb_gemkbd_configure(&in->gemkbd, layout < 0 ? in->gemkbd.layout : layout, swap);
}

bool rb_input_is_gemini(const struct rb_input *in) { return in && in->gemini && in->gemkbd_devices > 0; }

void rb_input_gemini_pointer(struct rb_input *in, int button, bool down)
{
    if (!rb_input_is_gemini(in))
        return;
    struct rb_gemkbd_ev ev[RB_GEMKBD_MAX_EV];
    int n = rb_gemkbd_pointer(&in->gemkbd, button, down, ev);
    for (int i = 0; i < n; i++)
        deliver_key(in, ev[i].code, ev[i].down);
}

const char *rb_input_gemini_keyboard(const struct rb_input *in)
{
    if (!in || !in->gemini)
        return NULL;
    if (!in->gemkbd_devices)
        return "gemini (not found)";
    return in->gemkbd.layout == RB_GEMKBD_UK ? "gemini-uk" : "gemini-us";
}

bool rb_input_take_escape_held(struct rb_input *in)
{
    if (!in || !in->esc_held)
        return false;
    in->esc_held = false;
    return true;
}
int rb_input_device_count(const struct rb_input *in) { return in ? in->devices : 0; }

int rb_input_touch_devices(const struct rb_input *in, const struct rb_input_dev **out, int max)
{
    int n = 0;
    for (int k = 0; in && k < in->ntdev && n < max; k++)
        out[n++] = &in->tdev[k].info;
    return n;
}

void rb_input_suspend(struct rb_input *in)
{
    if (in && in->li)
        libinput_suspend(in->li);
}
