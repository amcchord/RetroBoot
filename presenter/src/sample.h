#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "region.h"

#define RB_SAMPLE_MAX 64

/* Explicit lab observation: native coordinates of sparse pixels on one row. */
struct rb_sample_row { int x, y, step, count; };
bool rb_sample_row_parse(const char *text, struct rb_sample_row *row);
bool rb_sample_row_translate(const struct rb_sample_row *native, rb_rect viewport,
                             int native_w, int native_h, int frame_w, int frame_h,
                             struct rb_sample_row *frame);
/* The presenter's submitted buffers use DRM_FORMAT_XRGB8888, little-endian
 * B,G,R,X bytes. Ignore X; return visible colors as 0xRRGGBB. Never write map. */
bool rb_sample_row_read(const uint8_t *map, uint64_t size, uint32_t pitch, uint32_t offset,
                        int width, int height, const struct rb_sample_row *row,
                        uint32_t pixels[RB_SAMPLE_MAX]);
