#include "gamepad.h"
#include "keymap.h"

#include <stdlib.h>
#include <string.h>

#ifdef __linux__
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <libudev.h>
#include <linux/input.h>
#include <stdio.h>
#include <sys/epoll.h>
#include <sys/ioctl.h>
#include <unistd.h>
#include "log.h"
#include "loop.h"
#else
/* Minimal Linux evdev numeric constants so pure gamepad state machine
 * and tests build cleanly off-target (e.g. macOS ASan unit test runner). */
#define EV_SYN 0x00
#define EV_KEY 0x01
#define EV_ABS 0x03
#define SYN_DROPPED 3
#define ABS_X 0x00
#define ABS_Y 0x01
#define ABS_HAT0X 0x10
#define ABS_HAT0Y 0x11
#define BTN_GAMEPAD 0x130
#define BTN_SOUTH 0x130
#define BTN_EAST 0x131
#define BTN_C 0x132
#define BTN_NORTH 0x133
#define BTN_WEST 0x134
#define BTN_Z 0x135
#define BTN_TL 0x136
#define BTN_TR 0x137
#define BTN_TL2 0x138
#define BTN_TR2 0x139
#define BTN_SELECT 0x13a
#define BTN_START 0x13b
#define BTN_MODE 0x13c
#define BTN_THUMBL 0x13d
#define BTN_THUMBR 0x13e
#define BTN_DPAD_UP 0x220
#define BTN_DPAD_DOWN 0x221
#define BTN_DPAD_LEFT 0x222
#define BTN_DPAD_RIGHT 0x223
#endif

#define RB_GP_MAX_DEVS 8

enum {
    SRC_DPAD_U = 0,
    SRC_DPAD_D,
    SRC_DPAD_L,
    SRC_DPAD_R,
    SRC_HAT_U,
    SRC_HAT_D,
    SRC_HAT_L,
    SRC_HAT_R,
    SRC_STICK_U,
    SRC_STICK_D,
    SRC_STICK_L,
    SRC_STICK_R,
    SRC_BTN_SOUTH,
    SRC_BTN_EAST,
    SRC_BTN_WEST,
    SRC_BTN_NORTH,
    SRC_BTN_START,
    SRC_BTN_SELECT,
    SRC_BTN_TL,
    SRC_BTN_TR,
    SRC_COUNT
};

struct gp_slot {
    bool used;
    struct rb_gamepad_abs_info abs_x, abs_y;
    bool src[SRC_COUNT];
#ifdef __linux__
    int fd;
    char devnode[128];
    struct rb_watch *watch;
    struct rb_gamepad *parent;
#endif
};

struct rb_gamepad {
    struct rb_loop *loop;
    rb_gamepad_key_fn on_key;
    void *data;
    enum rb_gamepad_mode mode;
    bool enabled;
    struct gp_slot slots[RB_GP_MAX_DEVS];
    int count;
    uint16_t key_ref[RB_GP_KEY_COUNT];
#ifdef __linux__
    struct udev *udev;
    struct udev_monitor *mon;
    struct rb_watch *mon_watch;
#endif
};

const char *rb_gamepad_mode_name(enum rb_gamepad_mode mode)
{
    switch (mode) {
    case RB_GAMEPAD_WASD: return "wasd";
    case RB_GAMEPAD_OFF: return "off";
    case RB_GAMEPAD_ARROWS:
    default: return "arrows";
    }
}

int rb_gamepad_mode_parse(const char *s)
{
    if (!s)
        return -1;
    if (!strcmp(s, "arrows"))
        return RB_GAMEPAD_ARROWS;
    if (!strcmp(s, "wasd"))
        return RB_GAMEPAD_WASD;
    if (!strcmp(s, "off"))
        return RB_GAMEPAD_OFF;
    return -1;
}

int rb_gamepad_key_to_qcode(enum rb_gamepad_key key)
{
    switch (key) {
    case RB_GP_KEY_UP: return RB_Q_UP;
    case RB_GP_KEY_DOWN: return RB_Q_DOWN;
    case RB_GP_KEY_LEFT: return RB_Q_LEFT;
    case RB_GP_KEY_RIGHT: return RB_Q_RIGHT;
    case RB_GP_KEY_W: return RB_Q_W;
    case RB_GP_KEY_A: return RB_Q_A;
    case RB_GP_KEY_S: return RB_Q_S;
    case RB_GP_KEY_D: return RB_Q_D;
    case RB_GP_KEY_SPACE: return RB_Q_SPC;
    case RB_GP_KEY_RETURN: return RB_Q_RET;
    case RB_GP_KEY_OPTION: return RB_Q_ALT;
    case RB_GP_KEY_CTRL: return RB_Q_CTRL;
    case RB_GP_KEY_ESC: return RB_Q_ESC;
    default: return 0;
    }
}

static bool bit_in(const unsigned long *bits, size_t nlongs, unsigned bit)
{
    size_t idx = bit / (8 * sizeof(unsigned long));
    if (idx >= nlongs)
        return false;
    return (bits[idx] >> (bit % (8 * sizeof(unsigned long)))) & 1UL;
}

bool rb_gamepad_is_gamepad_bits(const unsigned long *evbits, size_t ev_longs, const unsigned long *keybits,
                                size_t key_longs)
{
    if (!evbits || !keybits || !bit_in(evbits, ev_longs, EV_KEY))
        return false;
    for (unsigned b = BTN_GAMEPAD; b <= BTN_THUMBR; b++)
        if (bit_in(keybits, key_longs, b))
            return true;
    for (unsigned b = BTN_DPAD_UP; b <= BTN_DPAD_RIGHT; b++)
        if (bit_in(keybits, key_longs, b))
            return true;
    return false;
}

static int map_source_key(enum rb_gamepad_mode mode, int src)
{
    if (mode == RB_GAMEPAD_OFF)
        return -1;
    bool wasd = mode == RB_GAMEPAD_WASD;
    switch (src) {
    case SRC_DPAD_U:
    case SRC_HAT_U:
    case SRC_STICK_U: return wasd ? RB_GP_KEY_W : RB_GP_KEY_UP;
    case SRC_DPAD_D:
    case SRC_HAT_D:
    case SRC_STICK_D: return wasd ? RB_GP_KEY_S : RB_GP_KEY_DOWN;
    case SRC_DPAD_L:
    case SRC_HAT_L:
    case SRC_STICK_L: return wasd ? RB_GP_KEY_A : RB_GP_KEY_LEFT;
    case SRC_DPAD_R:
    case SRC_HAT_R:
    case SRC_STICK_R: return wasd ? RB_GP_KEY_D : RB_GP_KEY_RIGHT;
    case SRC_BTN_SOUTH: return RB_GP_KEY_SPACE;
    case SRC_BTN_EAST:
    case SRC_BTN_TR: return RB_GP_KEY_OPTION;
    case SRC_BTN_WEST:
    case SRC_BTN_TL: return RB_GP_KEY_CTRL;
    case SRC_BTN_NORTH:
    case SRC_BTN_START: return RB_GP_KEY_RETURN;
    case SRC_BTN_SELECT: return RB_GP_KEY_ESC;
    default: return -1;
    }
}

static void emit_key_ref(struct rb_gamepad *gp, int k, bool down)
{
    if (k < 0 || k >= RB_GP_KEY_COUNT)
        return;
    if (down) {
        if (gp->key_ref[k]++ == 0 && gp->on_key) {
            int q = rb_gamepad_key_to_qcode((enum rb_gamepad_key)k);
            if (q)
                gp->on_key(gp->data, q, true);
        }
    } else if (gp->key_ref[k] > 0 && --gp->key_ref[k] == 0 && gp->on_key) {
        int q = rb_gamepad_key_to_qcode((enum rb_gamepad_key)k);
        if (q)
            gp->on_key(gp->data, q, false);
    }
}

static bool active_routing(const struct rb_gamepad *gp)
{
    return gp->enabled && gp->mode != RB_GAMEPAD_OFF;
}

static void sync_all_keys(struct rb_gamepad *gp, enum rb_gamepad_mode prev_mode, bool prev_active)
{
    bool next_active = active_routing(gp);
    if (prev_active && (!next_active || prev_mode != gp->mode)) {
        for (int k = 0; k < RB_GP_KEY_COUNT; k++) {
            if (gp->key_ref[k] > 0) {
                gp->key_ref[k] = 0;
                if (gp->on_key) {
                    int q = rb_gamepad_key_to_qcode((enum rb_gamepad_key)k);
                    if (q)
                        gp->on_key(gp->data, q, false);
                }
            }
        }
    }
    if (next_active && (!prev_active || prev_mode != gp->mode)) {
        for (int i = 0; i < RB_GP_MAX_DEVS; i++) {
            if (!gp->slots[i].used)
                continue;
            for (int s = 0; s < SRC_COUNT; s++) {
                if (gp->slots[i].src[s])
                    emit_key_ref(gp, map_source_key(gp->mode, s), true);
            }
        }
    }
}

static void set_src(struct rb_gamepad *gp, struct gp_slot *sl, int src, bool down)
{
    if (src < 0 || src >= SRC_COUNT || sl->src[src] == down)
        return;
    sl->src[src] = down;
    if (active_routing(gp))
        emit_key_ref(gp, map_source_key(gp->mode, src), down);
}

static void clear_slot_sources(struct rb_gamepad *gp, struct gp_slot *sl)
{
    for (int s = 0; s < SRC_COUNT; s++)
        set_src(gp, sl, s, false);
}

static void stick_dir(const struct rb_gamepad_abs_info *info, int32_t value, bool prev_neg, bool prev_pos,
                      bool *neg_out, bool *pos_out)
{
    int32_t mn = info->minimum, mx = info->maximum;
    if (mx <= mn) {
        mn = -32768;
        mx = 32767;
    }
    int64_t mid = ((int64_t)mn + (int64_t)mx) / 2;
    int64_t half = ((int64_t)mx - (int64_t)mn) / 2;
    if (half <= 0)
        half = 1;
    int64_t hi = half / 2;       /* 50% threshold to press */
    int64_t lo = (half * 7) / 20; /* 35% threshold to release (hysteresis) */
    if (info->flat > lo)
        lo = info->flat;
    if (lo > hi)
        hi = lo;
    int64_t d = (int64_t)value - mid;
    *neg_out = d <= -(prev_neg ? lo : hi);
    *pos_out = d >= (prev_pos ? lo : hi);
}

int rb_gamepad_add_slot(struct rb_gamepad *gp, const struct rb_gamepad_abs_info *abs_x,
                        const struct rb_gamepad_abs_info *abs_y)
{
    if (!gp)
        return -1;
    for (int i = 0; i < RB_GP_MAX_DEVS; i++) {
        if (!gp->slots[i].used) {
            memset(&gp->slots[i], 0, sizeof gp->slots[i]);
            gp->slots[i].used = true;
            if (abs_x && abs_x->maximum > abs_x->minimum)
                gp->slots[i].abs_x = *abs_x;
            else
                gp->slots[i].abs_x = (struct rb_gamepad_abs_info){-32768, 32767, 4096};
            if (abs_y && abs_y->maximum > abs_y->minimum)
                gp->slots[i].abs_y = *abs_y;
            else
                gp->slots[i].abs_y = (struct rb_gamepad_abs_info){-32768, 32767, 4096};
#ifdef __linux__
            gp->slots[i].fd = -1;
            gp->slots[i].parent = gp;
#endif
            gp->count++;
            return i;
        }
    }
    return -1;
}

void rb_gamepad_remove_slot(struct rb_gamepad *gp, int slot)
{
    if (!gp || slot < 0 || slot >= RB_GP_MAX_DEVS || !gp->slots[slot].used)
        return;
    clear_slot_sources(gp, &gp->slots[slot]);
#ifdef __linux__
    if (gp->slots[slot].watch)
        rb_watch_remove(gp->slots[slot].watch);
    if (gp->slots[slot].fd >= 0)
        close(gp->slots[slot].fd);
#endif
    memset(&gp->slots[slot], 0, sizeof gp->slots[slot]);
#ifdef __linux__
    gp->slots[slot].fd = -1;
#endif
    gp->count--;
}

void rb_gamepad_feed_ev(struct rb_gamepad *gp, int slot, uint16_t type, uint16_t code, int32_t value)
{
    if (!gp || slot < 0 || slot >= RB_GP_MAX_DEVS || !gp->slots[slot].used)
        return;
    struct gp_slot *sl = &gp->slots[slot];
    if (type == EV_SYN && code == SYN_DROPPED) {
        clear_slot_sources(gp, sl);
        return;
    }
    if (type == EV_KEY) {
        bool down = value != 0;
        switch (code) {
        case BTN_DPAD_UP: set_src(gp, sl, SRC_DPAD_U, down); break;
        case BTN_DPAD_DOWN: set_src(gp, sl, SRC_DPAD_D, down); break;
        case BTN_DPAD_LEFT: set_src(gp, sl, SRC_DPAD_L, down); break;
        case BTN_DPAD_RIGHT: set_src(gp, sl, SRC_DPAD_R, down); break;
        case BTN_SOUTH: set_src(gp, sl, SRC_BTN_SOUTH, down); break;
        case BTN_EAST: set_src(gp, sl, SRC_BTN_EAST, down); break;
        case BTN_WEST: set_src(gp, sl, SRC_BTN_WEST, down); break;
        case BTN_NORTH: set_src(gp, sl, SRC_BTN_NORTH, down); break;
        case BTN_START: set_src(gp, sl, SRC_BTN_START, down); break;
        case BTN_SELECT: set_src(gp, sl, SRC_BTN_SELECT, down); break;
        case BTN_TL: set_src(gp, sl, SRC_BTN_TL, down); break;
        case BTN_TR: set_src(gp, sl, SRC_BTN_TR, down); break;
        default: break;
        }
        return;
    }
    if (type == EV_ABS) {
        if (code == ABS_HAT0X) {
            set_src(gp, sl, SRC_HAT_L, value < 0);
            set_src(gp, sl, SRC_HAT_R, value > 0);
        } else if (code == ABS_HAT0Y) {
            set_src(gp, sl, SRC_HAT_U, value < 0);
            set_src(gp, sl, SRC_HAT_D, value > 0);
        } else if (code == ABS_X) {
            bool neg = false, pos = false;
            stick_dir(&sl->abs_x, value, sl->src[SRC_STICK_L], sl->src[SRC_STICK_R], &neg, &pos);
            set_src(gp, sl, SRC_STICK_L, neg);
            set_src(gp, sl, SRC_STICK_R, pos);
        } else if (code == ABS_Y) {
            bool neg = false, pos = false;
            stick_dir(&sl->abs_y, value, sl->src[SRC_STICK_U], sl->src[SRC_STICK_D], &neg, &pos);
            set_src(gp, sl, SRC_STICK_U, neg);
            set_src(gp, sl, SRC_STICK_D, pos);
        }
    }
}

void rb_gamepad_set_mode(struct rb_gamepad *gp, enum rb_gamepad_mode mode)
{
    if (!gp)
        return;
    if (mode != RB_GAMEPAD_ARROWS && mode != RB_GAMEPAD_WASD && mode != RB_GAMEPAD_OFF)
        mode = RB_GAMEPAD_ARROWS;
    if (mode == gp->mode)
        return;
    enum rb_gamepad_mode prev_mode = gp->mode;
    bool prev_active = active_routing(gp);
    gp->mode = mode;
    sync_all_keys(gp, prev_mode, prev_active);
}

enum rb_gamepad_mode rb_gamepad_get_mode(const struct rb_gamepad *gp)
{
    return gp ? gp->mode : RB_GAMEPAD_OFF;
}

void rb_gamepad_set_enabled(struct rb_gamepad *gp, bool enabled)
{
    if (!gp || enabled == gp->enabled)
        return;
    bool prev_active = active_routing(gp);
    gp->enabled = enabled;
    sync_all_keys(gp, gp->mode, prev_active);
}

bool rb_gamepad_get_enabled(const struct rb_gamepad *gp) { return gp && gp->enabled; }
int rb_gamepad_count(const struct rb_gamepad *gp) { return gp ? gp->count : 0; }

#ifdef __linux__
#define NLONGS(n) (((n) + 8 * sizeof(unsigned long) - 1) / (8 * sizeof(unsigned long)))

static int slot_by_devnode(const struct rb_gamepad *gp, const char *devnode)
{
    for (int i = 0; i < RB_GP_MAX_DEVS; i++)
        if (gp->slots[i].used && !strcmp(gp->slots[i].devnode, devnode))
            return i;
    return -1;
}

static void gp_fd_ready(void *data, int fd, uint32_t events)
{
    struct gp_slot *sl = data;
    struct rb_gamepad *gp = sl->parent;
    int slot = (int)(sl - gp->slots);
    if (events & (EPOLLHUP | EPOLLERR)) {
        rb_log("gamepad: removed %s", sl->devnode);
        rb_gamepad_remove_slot(gp, slot);
        return;
    }
    struct input_event evs[64];
    for (;;) {
        ssize_t n = read(fd, evs, sizeof evs);
        if (n < 0) {
            if (errno == EINTR)
                continue;
            if (errno == EAGAIN || errno == EWOULDBLOCK)
                break;
            rb_log("gamepad: %s read error: %s", sl->devnode, strerror(errno));
            rb_gamepad_remove_slot(gp, slot);
            return;
        }
        if (n == 0) {
            rb_gamepad_remove_slot(gp, slot);
            return;
        }
        size_t count = (size_t)n / sizeof evs[0];
        for (size_t i = 0; i < count; i++)
            rb_gamepad_feed_ev(gp, slot, evs[i].type, evs[i].code, evs[i].value);
    }
}

static void try_open_devnode(struct rb_gamepad *gp, const char *devnode)
{
    if (!gp->loop || !devnode || strncmp(devnode, "/dev/input/event", 16) || strlen(devnode) >= 128)
        return;
    if (slot_by_devnode(gp, devnode) >= 0)
        return;
    int fd = open(devnode, O_RDONLY | O_NONBLOCK | O_CLOEXEC);
    if (fd < 0)
        return;
    unsigned long evbits[NLONGS(EV_MAX + 1)] = {0};
    unsigned long keybits[NLONGS(KEY_MAX + 1)] = {0};
    if (ioctl(fd, EVIOCGBIT(0, sizeof evbits), evbits) < 0 ||
        ioctl(fd, EVIOCGBIT(EV_KEY, sizeof keybits), keybits) < 0 ||
        !rb_gamepad_is_gamepad_bits(evbits, NLONGS(EV_MAX + 1), keybits, NLONGS(KEY_MAX + 1))) {
        close(fd);
        return;
    }
    struct input_absinfo aix = {0}, aiy = {0};
    struct rb_gamepad_abs_info ax = {-32768, 32767, 4096}, ay = {-32768, 32767, 4096};
    if (ioctl(fd, EVIOCGABS(ABS_X), &aix) >= 0 && aix.maximum > aix.minimum)
        ax = (struct rb_gamepad_abs_info){aix.minimum, aix.maximum, aix.flat};
    if (ioctl(fd, EVIOCGABS(ABS_Y), &aiy) >= 0 && aiy.maximum > aiy.minimum)
        ay = (struct rb_gamepad_abs_info){aiy.minimum, aiy.maximum, aiy.flat};
    int slot = rb_gamepad_add_slot(gp, &ax, &ay);
    if (slot < 0) {
        close(fd);
        return;
    }
    struct gp_slot *sl = &gp->slots[slot];
    sl->fd = fd;
    snprintf(sl->devnode, sizeof sl->devnode, "%s", devnode);
    sl->watch = rb_loop_watch(gp->loop, fd, EPOLLIN, gp_fd_ready, sl);
    if (!sl->watch) {
        rb_gamepad_remove_slot(gp, slot);
        return;
    }
    char name[128] = "gamepad";
    if (ioctl(fd, EVIOCGNAME(sizeof name), name) < 0)
        snprintf(name, sizeof name, "%s", devnode);
    rb_log("gamepad: connected '%s' (%s)", name, devnode);
}

static void gp_udev_ready(void *data, int fd, uint32_t events)
{
    (void)fd;
    (void)events;
    struct rb_gamepad *gp = data;
    struct udev_device *ud;
    while ((ud = udev_monitor_receive_device(gp->mon))) {
        const char *act = udev_device_get_action(ud);
        const char *devnode = udev_device_get_devnode(ud);
        if (devnode) {
            if (act && !strcmp(act, "remove"))
                rb_gamepad_remove_slot(gp, slot_by_devnode(gp, devnode));
            else if (!act || !strcmp(act, "add") || !strcmp(act, "change"))
                try_open_devnode(gp, devnode);
        }
        udev_device_unref(ud);
    }
}

static void scan_initial_gamepads(struct rb_gamepad *gp)
{
    DIR *d = opendir("/dev/input");
    if (!d)
        return;
    struct dirent *de;
    while ((de = readdir(d))) {
        if (strncmp(de->d_name, "event", 5))
            continue;
        char path[128];
        if (snprintf(path, sizeof path, "/dev/input/%s", de->d_name) < (int)sizeof path)
            try_open_devnode(gp, path);
    }
    closedir(d);
}
#endif

struct rb_gamepad *rb_gamepad_new(struct rb_loop *loop, rb_gamepad_key_fn on_key, void *data)
{
    struct rb_gamepad *gp = calloc(1, sizeof *gp);
    if (!gp)
        return NULL;
    gp->loop = loop;
    gp->on_key = on_key;
    gp->data = data;
    gp->mode = RB_GAMEPAD_ARROWS;
#ifdef __linux__
    for (int i = 0; i < RB_GP_MAX_DEVS; i++)
        gp->slots[i].fd = -1;
    if (loop) {
        scan_initial_gamepads(gp);
        gp->udev = udev_new();
        if (gp->udev) {
            gp->mon = udev_monitor_new_from_netlink(gp->udev, "udev");
            if (gp->mon) {
                udev_monitor_filter_add_match_subsystem_devtype(gp->mon, "input", NULL);
                udev_monitor_enable_receiving(gp->mon);
                gp->mon_watch = rb_loop_watch(loop, udev_monitor_get_fd(gp->mon), EPOLLIN, gp_udev_ready, gp);
            }
        }
    }
#endif
    return gp;
}

void rb_gamepad_free(struct rb_gamepad *gp)
{
    if (!gp)
        return;
    rb_gamepad_set_enabled(gp, false);
    for (int i = 0; i < RB_GP_MAX_DEVS; i++)
        if (gp->slots[i].used)
            rb_gamepad_remove_slot(gp, i);
#ifdef __linux__
    if (gp->mon_watch)
        rb_watch_remove(gp->mon_watch);
    if (gp->mon)
        udev_monitor_unref(gp->mon);
    if (gp->udev)
        udev_unref(gp->udev);
#endif
    free(gp);
}
