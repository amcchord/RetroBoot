#include <math.h>
#include <string.h>
#include "guest.h"
#include "pointer.h"
#include "rbtest.h"

static void test_legacy_mouse(void)
{
    CHECK_EQ(rb_mouse_dpi(NULL), 1000);
    CHECK_EQ(rb_mouse_dpi("800@125"), 800);
    CHECK_EQ(rb_mouse_dpi("400@125 *1600@500 3200@1000"), 1600);
    CHECK_EQ(rb_mouse_dpi("bogus"), 1000);
    CHECK_EQ(rb_mouse_dpi("0"), 1000);

    struct rb_legacy_mouse mouse = {0};
    struct rb_pointer_delta slow = rb_legacy_mouse_motion(&mouse, 8000, 8, 0, 1000);
    CHECK(fabs(slow.x - 0.576) < 1e-9);
    CHECK_EQ(slow.y, 0);
    struct rb_pointer_delta swing = rb_legacy_mouse_motion(&mouse, 80000, 96, -48, 1000);
    CHECK(fabs(swing.x - 27.648) < 1e-9);
    CHECK(fabs(swing.y + 13.824) < 1e-9);
    struct rb_pointer_delta after_pause = rb_legacy_mouse_motion(&mouse, 160000, -8, 0, 1000);
    CHECK(fabs(after_pause.x + slow.x) < 1e-9);
    struct rb_pointer_delta image = rb_legacy_mouse_motion(&mouse, 240000, 16, 0, 2000);
    CHECK(fabs(image.x - slow.x) < 1e-9);

    struct rb_legacy_mouse slow_reports = {0}, fast_reports = {0};
    struct rb_pointer_delta a = {0}, b = {0};
    for (uint64_t t = 2000; t <= 240000; t += 2000) {
        b = rb_legacy_mouse_motion(&fast_reports, t, 8, 0, 1000);
        if (t % 8000 == 0)
            a = rb_legacy_mouse_motion(&slow_reports, t, 32, 0, 1000);
    }
    CHECK(fabs(a.x - 4 * b.x) < 1e-5);
    CHECK(fabs(a.x - 4.032) < 1e-9);
}

int main(void)
{
    test_legacy_mouse();
    struct rb_pointer_delta r = {0};
    int32_t x, y;
    /* Four half-pixel reports at 2x scaling still move one guest pixel. */
    for (int i = 0; i < 3; i++)
        CHECK(!rb_pointer_relative(&r, 0.5, -0.5, 1, 2, &x, &y));
    CHECK(rb_pointer_relative(&r, 0.5, -0.5, 1, 2, &x, &y));
    CHECK_EQ(x, 1); CHECK_EQ(y, -1);
    CHECK(rb_pointer_relative(&r, -100, 70, 2, 1, &x, &y));
    CHECK_EQ(x, -200); CHECK_EQ(y, 140);
    /* Large reports cannot overflow guest HID accumulators or retain a tail. */
    CHECK(rb_pointer_relative(&r, 1e100, -1e100, 1, 1, &x, &y));
    CHECK_EQ(x, 32767); CHECK_EQ(y, -32767);
    CHECK(!rb_pointer_relative(&r, 0, 0, 1, 1, &x, &y));
    CHECK(!rb_pointer_relative(&r, NAN, 0, 1, 1, &x, &y));
    CHECK(!rb_pointer_relative(&r, 1, 1, 1, 0, &x, &y));
    CHECK_EQ(x, 0); CHECK_EQ(y, 0);
    /* A blocked display connection must sum deltas rather than replace them. */
    struct rb_guest g = {0};
    g.fd = 42; /* No writes: the existing queue item takes the coalescing path. */
    g.qcount = 1;
    g.qlen_of[0] = (uint8_t)rbd_encode(g.q[0], RBD_POINTER_REL, 50, (uint32_t)-100, 2);
    rb_guest_send_relative(&g, -70, 80);
    int32_t accumulated[2];
    memcpy(accumulated, g.q[0] + 8, sizeof accumulated);
    CHECK_EQ(g.qcount, 1);
    CHECK_EQ(accumulated[0], -20); CHECK_EQ(accumulated[1], -20);
    rb_guest_send_relative(&g, INT32_MAX, INT32_MIN);
    memcpy(accumulated, g.q[0] + 8, sizeof accumulated);
    CHECK_EQ(accumulated[0], 32767); CHECK_EQ(accumulated[1], -32767);
    TEST_DONE();
}
