#include "pointer.h"
#include <math.h>
#include <stdlib.h>
#include <string.h>

static int32_t axis(double *remainder, double motion)
{
    double total = *remainder + motion;
    if (total >= 32767 || total <= -32767) {
        *remainder = 0;
        return total > 0 ? 32767 : -32767;
    }
    int32_t pixels = (int32_t)total;
    *remainder = total - pixels;
    return pixels;
}

bool rb_pointer_relative(struct rb_pointer_delta *remainder, double dx, double dy,
                         double gain, double ratio, int32_t *x, int32_t *y)
{
    *x = *y = 0;
    if (!isfinite(dx) || !isfinite(dy) || !isfinite(gain) || !isfinite(ratio) ||
        gain <= 0 || ratio <= 0) {
        *remainder = (struct rb_pointer_delta){0};
        return false;
    }
    *x = axis(&remainder->x, dx * gain / ratio);
    *y = axis(&remainder->y, dy * gain / ratio);
    return *x || *y;
}

double rb_mouse_dpi(const char *property)
{
    if (!property)
        return 1000;
    const char *default_entry = strchr(property, '*');
    const char *entry = default_entry ? default_entry + 1 : property;
    char *end;
    long dpi = strtol(entry, &end, 10);
    if (end == entry || (*end && *end != '@' && *end != ' ') || dpi <= 0 || dpi > 100000)
        return 1000;
    return dpi;
}

/* As in Apple's Cursor Device Manager, each point pairs a device speed
 * with a cursor speed in inches/s (Technical Note HW01). These points are
 * tuned for RetroBoot. The last point limits gain for very fast motion. */
static double legacy_gain(double speed)
{
    static const struct {
        double device, cursor;
    } curve[] = {{0, 0}, {2, 2}, {4, 7}, {8, 24}, {12, 48}};

    for (size_t i = 1; i < sizeof curve / sizeof *curve; i++) {
        if (speed <= curve[i].device) {
            double t = (speed - curve[i - 1].device) / (curve[i].device - curve[i - 1].device);
            double cursor = curve[i - 1].cursor + t * (curve[i].cursor - curve[i - 1].cursor);
            return speed > 0 ? cursor / speed : 1;
        }
    }
    return 4;
}

struct rb_pointer_delta rb_legacy_mouse_motion(struct rb_legacy_mouse *mouse, uint64_t time_us,
                                               double dx, double dy, double dpi)
{
    /* Smooth estimates over roughly a classic display tick. Estimate from
     * a nominal 125 Hz report when a movement starts after a pause. */
    uint64_t elapsed = time_us > mouse->last_us ? time_us - mouse->last_us : 0;
    bool fresh = !mouse->last_us || !elapsed || elapsed > 50000;
    double interval = fresh ? 8000 : fmax(1000, elapsed);
    double speed = hypot(dx, dy) * 1e6 / (dpi * interval);
    if (fresh)
        mouse->speed_ips = speed;
    else
        mouse->speed_ips += (speed - mouse->speed_ips) * -expm1(-interval / 16000);
    mouse->last_us = time_us;

    double scale = 72 * legacy_gain(mouse->speed_ips) / dpi;
    return (struct rb_pointer_delta){dx * scale, dy * scale};
}
