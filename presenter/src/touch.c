#include "touch.h"

#include <ctype.h>
#include <math.h>
#include <stdio.h>
#include <string.h>
#include <strings.h>

const char *rb_gesture_state_name(enum rb_gesture_state s)
{
    switch (s) {
    case RB_G_IDLE: return "idle";
    case RB_G_PENDING: return "pending";
    case RB_G_DRAG: return "drag";
    case RB_G_LONG: return "long-press";
    case RB_G_TWO: return "two-finger";
    case RB_G_SCROLL: return "scroll";
    case RB_G_DONE: return "done";
    case RB_G_DIRECT: return "direct";
    }
    return "?";
}

void rb_gesture_init(struct rb_gesture *g, const struct rb_gesture_cb *cb, void *data)
{
    memset(g, 0, sizeof *g);
    g->cb = *cb;
    g->data = data;
    g->enabled = true;
    g->slop = 8;
    g->primary = -1;
}

void rb_gesture_configure(struct rb_gesture *g, bool enabled, double slop)
{
    if (enabled != g->enabled)
        rb_gesture_cancel(g);
    g->enabled = enabled;
    g->slop = slop > 1 ? slop : 1;
}

static void point(struct rb_gesture *g, double x, double y)
{
    if (g->cb.point)
        g->cb.point(g->data, x, y);
}

static void press(struct rb_gesture *g, int b, bool down)
{
    if (b < 0 || b > 2 || g->held[b] == down)
        return;
    g->held[b] = down;
    if (g->cb.button)
        g->cb.button(g->data, b, down);
}

static double dist(double ax, double ay, double bx, double by) { return hypot(ax - bx, ay - by); }

static void centroid(const struct rb_gesture *g, double *x, double *y)
{
    double sx = 0, sy = 0;
    int n = 0;
    for (int i = 0; i < RB_TOUCH_SLOTS; i++)
        if (g->c[i].down) {
            sx += g->c[i].x;
            sy += g->c[i].y;
            n++;
        }
    *x = n ? sx / n : 0;
    *y = n ? sy / n : 0;
}

void rb_gesture_cancel(struct rb_gesture *g)
{
    for (int b = 0; b < 3; b++)
        press(g, b, false);
    for (int i = 0; i < RB_TOUCH_SLOTS; i++)
        g->c[i].down = false;
    g->ndown = 0;
    g->st = RB_G_IDLE;
    g->long_at = 0;
    g->primary = -1;
}

void rb_gesture_down(struct rb_gesture *g, int slot, double x, double y, uint64_t t)
{
    if (slot < 0 || slot >= RB_TOUCH_SLOTS || g->c[slot].down)
        return;
    g->c[slot].down = true;
    g->c[slot].x = g->c[slot].x0 = x;
    g->c[slot].y = g->c[slot].y0 = y;
    g->ndown++;
    if (!g->enabled) {
        if (g->st == RB_G_IDLE) {
            g->primary = slot;
            point(g, x, y);
            press(g, 0, true);
            g->st = RB_G_DIRECT;
        }
        return;
    }
    switch (g->st) {
    case RB_G_IDLE:
        g->primary = slot;
        g->t0 = t;
        g->px = x;
        g->py = y;
        /* a second tap on (nearly) the same spot is a double-click there */
        if (g->have_tap && t - g->tap_t <= RB_TOUCH_DOUBLE_MS && dist(x, y, g->tap_x, g->tap_y) <= 2 * g->slop) {
            g->px = g->tap_x;
            g->py = g->tap_y;
        }
        point(g, g->px, g->py);
        g->st = RB_G_PENDING;
        g->long_at = t + RB_TOUCH_LONG_MS;
        break;
    case RB_G_PENDING:
        g->st = RB_G_TWO;
        g->long_at = 0;
        g->have_tap = false;
        centroid(g, &g->cx, &g->cy);
        break;
    default: /* decided: extra fingers change nothing */
        break;
    }
}

void rb_gesture_motion(struct rb_gesture *g, int slot, double x, double y, uint64_t t)
{
    (void)t;
    if (slot < 0 || slot >= RB_TOUCH_SLOTS || !g->c[slot].down)
        return;
    g->c[slot].x = x;
    g->c[slot].y = y;
    switch (g->st) {
    case RB_G_DIRECT:
    case RB_G_DRAG:
    case RB_G_LONG:
        if (slot == g->primary)
            point(g, x, y);
        break;
    case RB_G_PENDING:
        if (slot == g->primary && dist(x, y, g->c[slot].x0, g->c[slot].y0) > g->slop) {
            /* a drag: press where the finger came down, then follow it */
            g->long_at = 0;
            g->have_tap = false;
            point(g, g->px, g->py);
            press(g, 0, true);
            point(g, x, y);
            g->st = RB_G_DRAG;
        }
        break;
    case RB_G_TWO: {
        if (g->ndown < 2) {
            /* one finger already lifted and the other moves: no tap */
            if (dist(x, y, g->c[slot].x0, g->c[slot].y0) > g->slop)
                g->st = RB_G_DONE;
            break;
        }
        double nx, ny;
        centroid(g, &nx, &ny);
        if (dist(nx, ny, g->cx, g->cy) > g->slop) {
            g->st = RB_G_SCROLL;
            if (g->cb.pan)
                g->cb.pan(g->data, nx - g->cx, ny - g->cy);
            g->cx = nx;
            g->cy = ny;
        }
        break;
    }
    case RB_G_SCROLL: {
        double nx, ny;
        centroid(g, &nx, &ny);
        if (g->cb.pan && (nx != g->cx || ny != g->cy))
            g->cb.pan(g->data, nx - g->cx, ny - g->cy);
        g->cx = nx;
        g->cy = ny;
        break;
    }
    default:
        break;
    }
}

void rb_gesture_up(struct rb_gesture *g, int slot, uint64_t t)
{
    if (slot < 0 || slot >= RB_TOUCH_SLOTS || !g->c[slot].down)
        return;
    g->c[slot].down = false;
    g->ndown--;
    bool last = g->ndown == 0;
    switch (g->st) {
    case RB_G_DIRECT:
        if (slot == g->primary) {
            press(g, 0, false);
            g->st = last ? RB_G_IDLE : RB_G_DONE;
        }
        break;
    case RB_G_PENDING:
        /* a tap: click where it landed */
        g->long_at = 0;
        point(g, g->px, g->py);
        press(g, 0, true);
        press(g, 0, false);
        g->have_tap = true;
        g->tap_x = g->px;
        g->tap_y = g->py;
        g->tap_t = t;
        g->st = last ? RB_G_IDLE : RB_G_DONE;
        break;
    case RB_G_DRAG:
        if (slot == g->primary) {
            press(g, 0, false);
            g->st = last ? RB_G_IDLE : RB_G_DONE;
        }
        break;
    case RB_G_LONG:
        if (slot == g->primary) {
            press(g, 2, false);
            g->st = last ? RB_G_IDLE : RB_G_DONE;
        }
        break;
    case RB_G_TWO:
        if (last) {
            if (t - g->t0 <= RB_TOUCH_TAP_MS) {
                /* a two-finger tap: secondary click where the first finger was */
                point(g, g->px, g->py);
                press(g, 2, true);
                press(g, 2, false);
            }
            g->st = RB_G_IDLE;
        }
        break;
    case RB_G_SCROLL:
    case RB_G_DONE:
        g->st = last ? RB_G_IDLE : RB_G_DONE;
        break;
    default:
        break;
    }
    if (last)
        g->primary = -1;
}

int rb_gesture_tick(struct rb_gesture *g, uint64_t t)
{
    if (g->st != RB_G_PENDING || !g->long_at)
        return -1;
    if (t < g->long_at)
        return (int)(g->long_at - t);
    /* a long press: secondary button down where the finger is */
    g->long_at = 0;
    g->have_tap = false;
    point(g, g->px, g->py);
    press(g, 2, true);
    g->st = RB_G_LONG;
    return -1;
}

double rb_touch_pan_v120(double dy, double k)
{
    if (k <= 0)
        k = 1;
    return -dy / (k * RB_TOUCH_NOTCH_PX) * 120.0;
}

size_t rb_edid_monitor_name(const uint8_t *e, size_t n, char *out, size_t outlen)
{
    static const uint8_t magic[8] = {0, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0};
    if (!outlen)
        return 0;
    out[0] = 0;
    if (!e || n < 128 || memcmp(e, magic, sizeof magic))
        return 0;
    for (int d = 54; d + 18 <= 126; d += 18) {
        const uint8_t *b = e + d;
        if (b[0] || b[1] || b[2] || b[3] != 0xFC)
            continue;
        size_t len = 0;
        for (int i = 5; i < 18 && b[i] != 0x0a && b[i] && len + 1 < outlen; i++)
            out[len++] = isprint(b[i]) ? (char)b[i] : '?';
        while (len && out[len - 1] == ' ')
            len--;
        out[len] = 0;
        return len;
    }
    return 0;
}

static bool name_contains(const char *hay, const char *needle)
{
    size_t n = strlen(needle);
    if (n < 4 || !hay)
        return false;
    for (const char *p = hay; *p; p++)
        if (!strncasecmp(p, needle, n))
            return true;
    return false;
}

int rb_touch_pick_output(const struct rb_touch_output *outs, int n, const char *want, const char *dev_name,
                         bool usb)
{
    if (n <= 0)
        return -1;
    if (want && *want) {
        for (int i = 0; i < n; i++)
            if (outs[i].connector && !strcasecmp(outs[i].connector, want))
                return i;
        bool in = !strcasecmp(want, "internal"), ex = !strcasecmp(want, "external");
        for (int i = 0; (in || ex) && i < n; i++)
            if (outs[i].internal == in)
                return i;
    }
    for (int i = 0; i < n; i++)
        if (outs[i].edid_name && name_contains(dev_name, outs[i].edid_name))
            return i;
    for (int i = 0; usb && i < n; i++)
        if (!outs[i].internal)
            return i;
    for (int i = 0; i < n; i++)
        if (outs[i].internal)
            return i;
    return 0;
}

int rb_touch_transform_parse(const char *s)
{
    if (!s)
        return 0;
    int flags = 0;
    const char *p = s;
    while (*p) {
        while (*p == ',' || *p == ' ')
            p++;
        if (!*p)
            break;
        const char *e = p;
        while (*e && *e != ',' && *e != ' ')
            e++;
        size_t n = (size_t)(e - p);
        if (n == 7 && !strncmp(p, "swap-xy", n))
            flags |= RB_TOUCH_SWAP_XY;
        else if (n == 8 && !strncmp(p, "invert-x", n))
            flags |= RB_TOUCH_INVERT_X;
        else if (n == 8 && !strncmp(p, "invert-y", n))
            flags |= RB_TOUCH_INVERT_Y;
        else if (!(n == 4 && !strncmp(p, "none", n)))
            return -1;
        p = e;
    }
    return flags;
}

static double flip01(double v)
{
    v = 1.0 - v;
    return v < 0 ? 0 : (v >= 1.0 ? 1.0 - 1e-9 : v);
}

void rb_touch_transform_apply(int flags, double *nx, double *ny)
{
    if (flags & RB_TOUCH_SWAP_XY) {
        double t = *nx;
        *nx = *ny;
        *ny = t;
    }
    if (flags & RB_TOUCH_INVERT_X)
        *nx = flip01(*nx);
    if (flags & RB_TOUCH_INVERT_Y)
        *ny = flip01(*ny);
}

const char *rb_touch_transform_name(int flags, char *buf, size_t n)
{
    snprintf(buf, n, "%s%s%s%s", flags & RB_TOUCH_SWAP_XY ? "swap-xy," : "", flags & RB_TOUCH_INVERT_X ? "invert-x," : "",
             flags & RB_TOUCH_INVERT_Y ? "invert-y," : "", flags ? "" : "none");
    size_t l = strlen(buf);
    if (l && buf[l - 1] == ',')
        buf[l - 1] = 0;
    return buf;
}
