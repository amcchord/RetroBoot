#include <math.h>
#include <string.h>
#include "guest.h"
#include "pointer.h"
#include "rbtest.h"

int main(void)
{
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
