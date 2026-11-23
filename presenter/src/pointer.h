#pragma once
#include <stdbool.h>
#include <stdint.h>

/* Fractional pixels survive slow trackpad motion and integer screen scaling. */
struct rb_pointer_delta { double x, y; };
bool rb_pointer_relative(struct rb_pointer_delta *remainder, double dx, double dy,
                         double gain, double ratio, int32_t *x, int32_t *y);

struct rb_mouse_motion {
    double dx, dy; /* raw sensor counts */
    double dpi;
};

#define RB_MOUSE_DPI_MIN 100
#define RB_MOUSE_DPI_MAX 100000

/* Default resolution from udev's MOUSE_DPI property, or 1000 if unknown. */
double rb_mouse_dpi(const char *property);
/* Convert physical travel to a 200-DPI virtual mouse report. */
bool rb_pointer_native(struct rb_pointer_delta *remainder, const struct rb_mouse_motion *mouse,
                       int override_dpi, double gain, int32_t *x, int32_t *y);
