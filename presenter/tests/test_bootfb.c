/* Boot framebuffer size from boot_params screen_info (firmware/GRUB mode). */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "drm.h"
#include "rbtest.h"

static void put16(uint8_t *b, int off, int v)
{
    b[off] = (uint8_t)(v & 0xff);
    b[off + 1] = (uint8_t)(v >> 8);
}

int main(void)
{
    uint8_t b[64];
    int w = 0, h = 0;

    /* rb-dev / VM 164 under OVMF: VIDEO_TYPE_EFI 1280x800x32 (captured bytes) */
    static const uint8_t ovmf[0x28] = {0x00, 0x00, 0x00, 0x80, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0x70,
                                       0x10, 0x00, 0x00, 0x05, 0x20, 0x03, 0x20, 0x00, 0, 0, 0, 0xfd, 0, 0,
                                       0x3f, 0x00, 0x3f, 0xa3, 0x00, 0x10, 0x00, 0x14, 0x08, 0x10};
    CHECK_EQ(rb_screen_info_size(ovmf, sizeof ovmf, &w, &h), 0);
    CHECK_EQ(w, 1280);
    CHECK_EQ(h, 800);

    /* GRUB VBE (VIDEO_TYPE_VLFB) 1920x1080 */
    memset(b, 0, sizeof b);
    b[0x0f] = 0x23;
    put16(b, 0x12, 1920);
    put16(b, 0x14, 1080);
    CHECK_EQ(rb_screen_info_size(b, sizeof b, &w, &h), 0);
    CHECK_EQ(w, 1920);
    CHECK_EQ(h, 1080);

    /* text mode / no framebuffer / nonsense sizes / short buffers */
    b[0x0f] = 0x01; /* VIDEO_TYPE_MDA */
    CHECK(rb_screen_info_size(b, sizeof b, &w, &h) < 0);
    b[0x0f] = 0x70;
    put16(b, 0x12, 0);
    CHECK(rb_screen_info_size(b, sizeof b, &w, &h) < 0);
    put16(b, 0x12, 1280);
    CHECK(rb_screen_info_size(b, 0x10, &w, &h) < 0);
    CHECK(rb_screen_info_size(NULL, 64, &w, &h) < 0);

    /* file path (RB_BOOT_PARAMS overrides the sysfs path for tests) */
    char path[] = "/tmp/rb-bootparams-XXXXXX";
    int fd = mkstemp(path);
    CHECK(fd >= 0);
    put16(b, 0x12, 1366);
    put16(b, 0x14, 768);
    CHECK_EQ(write(fd, b, sizeof b), (long)sizeof b);
    close(fd);
    setenv("RB_BOOT_PARAMS", path, 1);
    CHECK_EQ(rb_boot_fb_size(NULL, &w, &h), 0);
    CHECK_EQ(w, 1366);
    CHECK_EQ(h, 768);
    unlink(path);
    CHECK(rb_boot_fb_size(NULL, &w, &h) < 0);
    unsetenv("RB_BOOT_PARAMS");
    TEST_DONE();
}
