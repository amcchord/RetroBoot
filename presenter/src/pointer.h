#pragma once
#include <stdbool.h>
#include <stdint.h>

/* Fractional pixels survive slow trackpad motion and integer screen scaling. */
struct rb_pointer_delta { double x, y; };
bool rb_pointer_relative(struct rb_pointer_delta *remainder, double dx, double dy,
                         double gain, double ratio, int32_t *x, int32_t *y);

struct rb_legacy_mouse {
    uint64_t last_us;
    double speed_ips;
};

/* Default resolution from udev's MOUSE_DPI property, or 1000 if unknown. */
double rb_mouse_dpi(const char *property);
/* Unaccelerated device units in, fractional classic guest pixels out. */
struct rb_pointer_delta rb_legacy_mouse_motion(struct rb_legacy_mouse *mouse, uint64_t time_us,
                                               double dx, double dy, double dpi);
