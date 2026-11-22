#pragma once
#include <stdbool.h>
#include <stdint.h>

/* Fractional pixels survive slow trackpad motion and integer screen scaling. */
struct rb_pointer_delta { double x, y; };
bool rb_pointer_relative(struct rb_pointer_delta *remainder, double dx, double dy,
                         double gain, double ratio, int32_t *x, int32_t *y);
