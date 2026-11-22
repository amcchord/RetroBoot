#include "pointer.h"
#include <math.h>

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
