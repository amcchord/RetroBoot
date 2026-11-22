#include "sample.h"
#include <errno.h>
#include <stdlib.h>

static bool row_valid(const struct rb_sample_row *r, int width, int height)
{
    return r && width > 0 && height > 0 && r->x >= 0 && r->y >= 0 &&
        r->step > 0 && r->count > 0 && r->count <= RB_SAMPLE_MAX &&
        r->y < height && (int64_t)r->x + (int64_t)r->step * (r->count - 1) < width;
}

bool rb_sample_row_parse(const char *text, struct rb_sample_row *row)
{
    if (!text || !row)
        return false;
    int fields[4];
    for (int i = 0; i < 4; i++) {
        if (*text < '0' || *text > '9')
            return false;
        char *end;
        errno = 0;
        long value = strtol(text, &end, 10);
        if (errno || value < 0 || value >= 16384 || *end != (i == 3 ? '\0' : ','))
            return false;
        fields[i] = (int)value;
        text = end + (i < 3);
    }
    struct rb_sample_row r = {fields[0], fields[1], fields[2], fields[3]};
    if (!row_valid(&r, 16384, 16384))
        return false;
    *row = r;
    return true;
}

bool rb_sample_row_translate(const struct rb_sample_row *native, rb_rect viewport,
                             int native_w, int native_h, int frame_w, int frame_h,
                             struct rb_sample_row *frame)
{
    if (!frame || !row_valid(native, native_w, native_h) ||
        viewport.w != native_w || viewport.h != native_h ||
        viewport.x < 0 || viewport.y < 0 ||
        (int64_t)viewport.x + viewport.w > frame_w ||
        (int64_t)viewport.y + viewport.h > frame_h)
        return false;
    *frame = *native;
    frame->x += viewport.x;
    frame->y += viewport.y;
    return row_valid(frame, frame_w, frame_h);
}

bool rb_sample_row_read(const uint8_t *map, uint64_t size, uint32_t pitch, uint32_t offset,
                        int width, int height, const struct rb_sample_row *row,
                        uint32_t pixels[RB_SAMPLE_MAX])
{
    if (!map || !pixels || !row_valid(row, width, height) || (uint64_t)width * 4 > pitch)
        return false;
    uint64_t last = (uint64_t)row->x + (uint64_t)row->step * (row->count - 1);
    uint64_t begin = (uint64_t)offset + (uint64_t)row->y * pitch;
    if (begin + last * 4 + 4 > size)
        return false;
    for (int i = 0; i < row->count; i++) {
        const uint8_t *p = map + begin + ((uint64_t)row->x + (uint64_t)row->step * i) * 4;
        pixels[i] = (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16;
    }
    return true;
}
