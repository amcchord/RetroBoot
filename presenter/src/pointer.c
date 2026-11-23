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
    if (end == entry || (*end && *end != '@' && *end != ' ') || dpi < RB_MOUSE_DPI_MIN || dpi > RB_MOUSE_DPI_MAX)
        return 1000;
    return dpi;
}

bool rb_pointer_native(struct rb_pointer_delta *remainder, const struct rb_mouse_motion *mouse,
                       int override_dpi, double gain, int32_t *x, int32_t *y)
{
    /* 200 counts/inch is QEMU's default ADB mouse handler resolution.
     * Use the same nominal resolution for its generic USB mouse. The guest
     * owns acceleration, so this conversion uses neither velocity nor screen fit. */
    double dpi = override_dpi ? override_dpi : mouse->dpi;
    return rb_pointer_relative(remainder, mouse->dx, mouse->dy, gain, dpi / 200, x, y);
}
