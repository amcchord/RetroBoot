/* Splash/presenter display handoff policy (hostboot report): a KMS driver that
 * binds after the presenter started (amdgpu loaded from the root file system,
 * virtio-gpu in the lab's late-driver test) must reach the presenter, never the
 * splash, or the presenter can't become DRM master and Mac OS never shows. */
#include "drm.h" /* first: it brings libdrm's xf86drmMode.h */

#include <errno.h>
#include <string.h>

#include "rbtest.h"
#include "splash.h"

static struct rb_splash_handoff released(const char *devnode)
{
    struct rb_splash_handoff h;
    memset(&h, 0, sizeof h);
    snprintf(h.devnode, sizeof h.devnode, "%s", devnode);
    return h;
}

static void test_splash_takes_devices_only_before_a_handoff(void)
{
    CHECK(rb_splash_may_take_device(false, false));   /* initramfs: draw on whatever binds */
    CHECK(!rb_splash_may_take_device(true, true));    /* during the handoff */
    CHECK(!rb_splash_may_take_device(false, true));   /* after it, even if aborted */
    CHECK(!rb_splash_may_take_device(true, false));
}

static void test_abort_keeps_only_a_display_the_splash_held(void)
{
    CHECK(!rb_splash_keep_after_abort(0)); /* nothing to show: exit, the presenter waits for the driver */
    CHECK(rb_splash_keep_after_abort(1));
    CHECK(rb_splash_keep_after_abort(2));
}

static void test_presenter_answer(void)
{
    struct rb_splash_handoff none = released("none"), empty = released(""), card = released("/dev/dri/card0");
    /* "ready none" (the splash had no device): done, so the splash exits */
    CHECK(rb_splash_handoff_complete(&none, false));
    CHECK(rb_splash_handoff_complete(&empty, false));
    /* the splash showed a device the presenter couldn't: abort, the splash keeps it */
    CHECK(!rb_splash_handoff_complete(&card, false));
    /* the presenter shows the frame: done */
    CHECK(rb_splash_handoff_complete(&card, true));
    CHECK(rb_splash_handoff_complete(&none, true));
}

/* hostboot2: a DSI pipe the firmware left running without the panel (GPD
 * MicroPC, cold start with HDMI attached) is never committed to. */
static void test_dark_dsi_is_never_committed(void)
{
    CHECK(rb_output_never_commit(DRM_MODE_CONNECTOR_DSI, false, 0));          /* left off by the firmware */
    CHECK(rb_output_never_commit(DRM_MODE_CONNECTOR_DSI, true, 1));           /* lit, not scanning out */
    CHECK(!rb_output_never_commit(DRM_MODE_CONNECTOR_DSI, true, 0));          /* lit and running */
    CHECK(!rb_output_never_commit(DRM_MODE_CONNECTOR_DSI, true, -ENOTCONN));
    CHECK(!rb_output_never_commit(DRM_MODE_CONNECTOR_eDP, false, 1));         /* eDP: Linux powers it on */
    CHECK(!rb_output_never_commit(DRM_MODE_CONNECTOR_HDMIA, false, 1));
}

int main(void)
{
    test_dark_dsi_is_never_committed();
    test_splash_takes_devices_only_before_a_handoff();
    test_abort_keeps_only_a_display_the_splash_held();
    test_presenter_answer();
    TEST_DONE();
}
