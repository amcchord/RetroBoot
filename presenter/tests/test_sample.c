#include "sample.h"
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

int main(void)
{
    struct rb_sample_row r, f;
    assert(rb_sample_row_parse("27,63,8,28", &r));
    assert(r.x == 27 && r.y == 63 && r.step == 8 && r.count == 28);
    for (const char **p = (const char *[]){"", "1,2,3", "1,2,3,4,5", "-1,2,3,4",
             "0,0,0,1", "0,0,1,65", "16383,0,1,2", "0,16384,1,1", "0,0,1,0",
             "999999999999999999999,0,1,1", "1,2,3,4 ", NULL}; *p; p++)
        assert(!rb_sample_row_parse(*p, &r));

    /* A centered 1x native viewport, a padded buffer and a nonzero BO offset.
     * These colors are an external golden pattern, not another sampler. */
    r = (struct rb_sample_row){0, 1, 1, 4};
    assert(rb_sample_row_translate(&r, (rb_rect){2, 3, 4, 2}, 4, 2, 8, 6, &f));
    assert(f.x == 2 && f.y == 4 && f.count == 4);
    uint8_t memory[256], original[256];
    memset(memory, 0xee, sizeof memory);
    const uint8_t colors[] = {0x56,0x34,0x12,0x55, 0xef,0xcd,0xab,0xaa,
                              0x00,0x00,0x00,0xff, 0xff,0xff,0xff,0x00};
    memcpy(memory + 16 + 4 * 40 + 2 * 4, colors, sizeof colors);
    memcpy(original, memory, sizeof memory);
    uint32_t pixels[RB_SAMPLE_MAX];
    assert(rb_sample_row_read(memory, sizeof memory, 40, 16, 8, 6, &f, pixels));
    assert(pixels[0] == 0x123456 && pixels[1] == 0xabcdef && pixels[2] == 0 && pixels[3] == 0xffffff);
    assert(!memcmp(memory, original, sizeof memory));
    f.step = 2; f.count = 2;
    assert(rb_sample_row_read(memory, sizeof memory, 40, 16, 8, 6, &f, pixels));
    assert(pixels[0] == 0x123456 && pixels[1] == 0);
    assert(!rb_sample_row_read(memory, 16 + 4 * 40 + 4 * 4 + 3, 40, 16, 8, 6, &f, pixels));
    assert(!rb_sample_row_read(memory, sizeof memory, 28, 16, 8, 6, &f, pixels));
    assert(!rb_sample_row_read(NULL, sizeof memory, 40, 16, 8, 6, &f, pixels));
    assert(!rb_sample_row_translate(&r, (rb_rect){2,3,8,4}, 4, 2, 8, 6, &f)); /* scaled */
    assert(!rb_sample_row_translate(&r, (rb_rect){6,3,4,2}, 4, 2, 8, 6, &f)); /* clipped */
    assert(!rb_sample_row_translate(&r, (rb_rect){-1,3,4,2}, 4, 2, 8, 6, &f));
    puts("bounded submitted-row RGB samples: OK");
    return 0;
}
