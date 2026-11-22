/* Display device, connector and mode choice (src/drm.c), on fake device lists.
 *
 * Raspberry Pi 4/5 with dtoverlay=vc4-kms-v3d split the GPU into two DRM
 * devices whose numbering is not stable: v3d (render only, no connectors) and
 * vc4 (KMS: HDMI-A-1, HDMI-A-2, and on a Pi 4 DSI/composite when enabled). A
 * Pi 5 adds RP1's DSI/DPI/composite cards. The split cannot be reproduced in
 * QEMU, so these cases are the evidence for the choice; x86 cases check that
 * nothing changed there. */
#include "drm.h" /* first: it brings libdrm's xf86drmMode.h */

#include <errno.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "rbtest.h"
#include "rotation.h"
#include "startup.h"

#define CARD(n, drv, conn, unk, hdmi, bvga) {(n), (drv), (conn), (unk), (hdmi), (bvga)}

static int rank(const struct rb_card_info *c, int n, char out[][64])
{
    memset(out, 0, RB_MAX_CARDS * 64);
    return rb_drm_rank_cards(c, n, out, RB_MAX_CARDS);
}

static void test_pi_cards(void)
{
    char out[RB_MAX_CARDS][64];
    /* Pi 4, v3d probed first: card0 is render-only, card1 is vc4 with a TV on HDMI-A-1. */
    struct rb_card_info v3d_first[] = {CARD("/dev/dri/card0", "v3d", 0, 0, false, false),
                                       CARD("/dev/dri/card1", "vc4-drm", 1, 0, true, false)};
    CHECK_EQ(rank(v3d_first, 2, out), 1);
    CHECK(!strcmp(out[0], "/dev/dri/card1"));
    /* ... and vc4 probed first. */
    struct rb_card_info vc4_first[] = {CARD("/dev/dri/card0", "vc4-drm", 1, 0, true, false),
                                       CARD("/dev/dri/card1", "v3d", 0, 0, false, false)};
    CHECK_EQ(rank(vc4_first, 2, out), 1);
    CHECK(!strcmp(out[0], "/dev/dri/card0"));
    /* Pi 5 with a DSI display and HDMI: internal panel first by default. */
    struct rb_card_info pi5_dsi[] = {CARD("/dev/dri/card0", "drm-rp1-dsi", 1, 0, false, false),
                                     CARD("/dev/dri/card1", "v3d", 0, 0, false, false),
                                     CARD("/dev/dri/card2", "vc4-drm", 1, 0, true, false)};
    CHECK_EQ(rank(pi5_dsi, 3, out), 2);
    CHECK(!strcmp(out[0], "/dev/dri/card0"));
    CHECK(!strcmp(out[1], "/dev/dri/card2"));
    CHECK_EQ(rb_drm_rank_cards_for_primary(pi5_dsi, 3, out, RB_MAX_CARDS, true), 2);
    CHECK(!strcmp(out[0], "/dev/dri/card2"));
    pi5_dsi[0].driver = "drm-rp1-dpi";
    CHECK_EQ(rank(pi5_dsi, 3, out), 2);
    CHECK(!strcmp(out[0], "/dev/dri/card0"));
    CHECK_EQ(rb_drm_rank_cards_for_primary(pi5_dsi, 3, out, RB_MAX_CARDS, true), 2);
    CHECK(!strcmp(out[0], "/dev/dri/card2"));
    /* An unconnected/unprobed panel never displaces connected HDMI. */
    pi5_dsi[0].connected = 0;
    pi5_dsi[0].unknown = 1;
    CHECK_EQ(rank(pi5_dsi, 3, out), 2);
    CHECK(!strcmp(out[0], "/dev/dri/card2"));
    /* Pi 5 with only the DSI display: the RP1 card. */
    struct rb_card_info pi5_dsi_only[] = {CARD("/dev/dri/card0", "drm-rp1-dsi", 1, 0, false, false),
                                          CARD("/dev/dri/card1", "v3d", 0, 0, false, false),
                                          CARD("/dev/dri/card2", "vc4-drm", 0, 0, false, false)};
    CHECK_EQ(rank(pi5_dsi_only, 3, out), 1);
    CHECK(!strcmp(out[0], "/dev/dri/card0"));
    /* Nothing connected: no candidate (the presenter waits for a hotplug). */
    struct rb_card_info none[] = {CARD("/dev/dri/card0", "v3d", 0, 0, false, false),
                                  CARD("/dev/dri/card1", "vc4-drm", 0, 0, false, false)};
    CHECK_EQ(rank(none, 2, out), 0);
    /* Pi 4 with composite enabled (enable_tvout=1): it has no detection and reads
     * "unknown", so vc4 is tried and rb_output_open finds nothing connected. */
    struct rb_card_info tvout[] = {CARD("/dev/dri/card0", "v3d", 0, 0, false, false),
                                   CARD("/dev/dri/card1", "vc4-drm", 0, 1, false, false)};
    CHECK_EQ(rank(tvout, 2, out), 1);
    CHECK(!strcmp(out[0], "/dev/dri/card1"));
    /* The firmware framebuffer (simpledrm) before vc4 binds, and while both exist. */
    struct rb_card_info fw[] = {CARD("/dev/dri/card0", "simple-framebuffer", 1, 0, false, false)};
    CHECK_EQ(rank(fw, 1, out), 1);
    struct rb_card_info fw_vc4[] = {CARD("/dev/dri/card0", "simple-framebuffer", 1, 0, false, false),
                                    CARD("/dev/dri/card1", "v3d", 0, 0, false, false),
                                    CARD("/dev/dri/card2", "vc4-drm", 1, 0, true, false)};
    CHECK_EQ(rank(fw_vc4, 3, out), 2);
    CHECK(!strcmp(out[0], "/dev/dri/card2"));
}

static void test_pc_cards_unchanged(void)
{
    char out[RB_MAX_CARDS][64];
    /* Scores as before the Pi rules: connected 100, unknown 50, boot_vga +20, real driver +10. */
    struct rb_card_info i915 = CARD("/dev/dri/card1", "i915", 1, 0, true, true);
    CHECK_EQ(rb_drm_card_score(&i915), 130);
    struct rb_card_info amd = CARD("/dev/dri/card0", "amdgpu", 2, 0, true, false);
    CHECK_EQ(rb_drm_card_score(&amd), 110);
    struct rb_card_info bochs = CARD("/dev/dri/card0", "bochs-drm", 0, 1, false, true);
    CHECK_EQ(rb_drm_card_score(&bochs), 80);
    struct rb_card_info efi = CARD("/dev/dri/card0", "efidrm", 1, 0, false, false);
    CHECK_EQ(rb_drm_card_score(&efi), 100);
    struct rb_card_info virtio2 = CARD("/dev/dri/card0", "virtio_gpu", 0, 2, false, false); /* not probed yet */
    CHECK_EQ(rb_drm_card_score(&virtio2), 110);
    struct rb_card_info pcs[] = {amd, i915};
    CHECK_EQ(rank(pcs, 2, out), 2);
    CHECK(!strcmp(out[0], "/dev/dri/card1")); /* boot_vga first */
    /* Equal scores: node name order, as before. */
    struct rb_card_info tie[] = {CARD("/dev/dri/card1", "amdgpu", 1, 0, true, false),
                                 CARD("/dev/dri/card0", "amdgpu", 1, 0, false, false)};
    CHECK_EQ(rank(tie, 2, out), 2);
    CHECK(!strcmp(out[0], "/dev/dri/card0"));
}

/* rb_output_open's connector loop over (type, type id, score). */
struct conn {
    uint32_t type, id;
    int score;
};
static int pick(const char *driver, const struct conn *c, int n)
{
    int best = -1;
    for (int i = 0; i < n; i++)
        if (best < 0 || rb_conn_better(driver, c[i].score, c[i].type, c[i].id, c[best].score, c[best].type, c[best].id))
            best = i;
    return best;
}

static void test_connector_choice(void)
{
    const uint32_t H = DRM_MODE_CONNECTOR_HDMIA, D = DRM_MODE_CONNECTOR_DSI;
    /* vc4 lists HDMI-A-2 first (probe order): HDMI-A-1 still wins a tie. */
    struct conn two[] = {{H, 2, 1}, {H, 1, 1}};
    CHECK_EQ(pick("vc4", two, 2), 1);
    struct conn two_in_order[] = {{H, 1, 1}, {H, 2, 1}};
    CHECK_EQ(pick("vc4", two_in_order, 2), 0);
    /* Only HDMI-A-2 connected (the other is skipped before scoring): it is used. */
    struct conn only2[] = {{H, 2, 1}};
    CHECK_EQ(pick("vc4", only2, 1), 0);
    /* The splash already lit HDMI-A-2 alone (active CRTC +100): the presenter keeps it. */
    struct conn lit2[] = {{H, 1, 1}, {H, 2, 101}};
    CHECK_EQ(pick("vc4", lit2, 2), 1);
    /* A DSI panel (+10 internal) is the primary, HDMI mirrors it. */
    struct conn dsi[] = {{H, 1, 1}, {D, 1, 11}};
    CHECK_EQ(pick("vc4", dsi, 2), 1);
    /* PCs: a tie keeps the first connector, as before. */
    CHECK_EQ(pick("i915", two, 2), 0);
    CHECK_EQ(pick("amdgpu", two, 2), 0);
    CHECK_EQ(pick(NULL, two, 2), 0);
}

static void test_dsi_on_a_pi_is_used(void)
{
    CHECK(rb_connector_internal(DRM_MODE_CONNECTOR_DPI));
    CHECK(rb_connector_internal(DRM_MODE_CONNECTOR_DSI));
    CHECK(!rb_connector_internal(DRM_MODE_CONNECTOR_HDMIA));
    CHECK(rb_driver_rpi_panel("drm-rp1-dpi"));
    CHECK(rb_driver_rpi_panel("drm-rp1-dsi"));
    CHECK(!rb_driver_rpi_panel("drm-rp1-vec"));
    /* i915 reads back the firmware's state: a DSI panel it left off is never committed. */
    CHECK(!rb_driver_no_readout("i915"));
    CHECK(rb_output_never_commit(DRM_MODE_CONNECTOR_DSI, false || rb_driver_no_readout("i915"), 0));
    /* vc4 and RP1 start with every CRTC off: "not lit" says nothing there. */
    CHECK(rb_driver_no_readout("vc4"));
    CHECK(rb_driver_no_readout("drm-rp1-dsi"));
    CHECK(!rb_driver_no_readout(NULL));
    CHECK(!rb_driver_no_readout("amdgpu"));
    CHECK(!rb_output_never_commit(DRM_MODE_CONNECTOR_DSI, false || rb_driver_no_readout("vc4"), 0));
}

static drmModeModeInfo mode(int w, int h, int hz, bool preferred, bool interlace)
{
    drmModeModeInfo m;
    memset(&m, 0, sizeof m);
    m.hdisplay = (uint16_t)w;
    m.vdisplay = (uint16_t)h;
    m.vrefresh = (uint32_t)hz;
    m.type = preferred ? DRM_MODE_TYPE_PREFERRED : 0;
    m.flags = interlace ? DRM_MODE_FLAG_INTERLACE : 0;
    snprintf(m.name, sizeof m.name, "%dx%d", w, h);
    return m;
}

static void test_mode_cap(void)
{
    int cw = 0, ch = 0;
    unsetenv("RB_AUTO_MODE_MAX");
    CHECK(rb_auto_mode_cap("vc4", &cw, &ch));
    CHECK_EQ(cw, 1920);
    CHECK_EQ(ch, 1080);
    CHECK(!rb_auto_mode_cap("i915", &cw, &ch));
    CHECK(!rb_auto_mode_cap("bochs-drm", &cw, &ch));
    CHECK(!rb_auto_mode_cap("virtio_gpu", &cw, &ch));
    CHECK(!rb_auto_mode_cap(NULL, &cw, &ch));
    setenv("RB_AUTO_MODE_MAX", "1280x720", 1);
    CHECK(rb_auto_mode_cap("virtio_gpu", &cw, &ch));
    CHECK_EQ(cw, 1280);
    setenv("RB_AUTO_MODE_MAX", "0", 1);
    CHECK(!rb_auto_mode_cap("vc4", &cw, &ch));
    unsetenv("RB_AUTO_MODE_MAX");

    CHECK(rb_mode_within_cap(1920, 1080, 1920, 1080));
    CHECK(rb_mode_within_cap(1080, 1920, 1920, 1080)); /* portrait */
    CHECK(rb_mode_within_cap(1280, 1024, 1920, 1080));
    CHECK(!rb_mode_within_cap(2560, 1080, 1920, 1080));
    CHECK(!rb_mode_within_cap(3840, 2160, 1920, 1080));

    /* A 4K TV: its preferred 3840x2160 -> 1920x1080 at 60 Hz. */
    drmModeModeInfo tv[] = {mode(3840, 2160, 60, true, false), mode(3840, 2160, 30, false, false),
                            mode(2560, 1440, 60, false, false), mode(1920, 1080, 50, false, false),
                            mode(1920, 1080, 60, false, false), mode(1920, 1080, 60, false, true),
                            mode(1920, 1080, 30, false, false), mode(1280, 720, 60, false, false)};
    const drmModeModeInfo *m = rb_mode_pick_capped(tv, 8, &tv[0], 1920, 1080);
    CHECK(m == &tv[4]);
    /* The same TV listing only 1080p50 and 1080p30. */
    drmModeModeInfo tv50[] = {mode(3840, 2160, 30, true, false), mode(1920, 1080, 30, false, false),
                              mode(1920, 1080, 50, false, false), mode(1280, 720, 60, false, false)};
    CHECK(rb_mode_pick_capped(tv50, 4, &tv50[0], 1920, 1080) == &tv50[2]);
    /* A 1080p or smaller monitor: its preferred mode, untouched. */
    drmModeModeInfo fhd[] = {mode(1280, 720, 60, false, false), mode(1920, 1080, 60, true, false)};
    CHECK(rb_mode_pick_capped(fhd, 2, &fhd[1], 1920, 1080) == &fhd[1]);
    drmModeModeInfo sxga[] = {mode(1280, 1024, 60, true, false), mode(1024, 768, 60, false, false)};
    CHECK(rb_mode_pick_capped(sxga, 2, &sxga[0], 1920, 1080) == &sxga[0]);
    /* An ultrawide: 2560x1080 is wider than the cap, 1920x1080 is offered. */
    drmModeModeInfo uw[] = {mode(3440, 1440, 60, true, false), mode(2560, 1080, 60, false, false),
                            mode(1920, 1080, 60, false, false), mode(1680, 1050, 60, false, false)};
    CHECK(rb_mode_pick_capped(uw, 4, &uw[0], 1920, 1080) == &uw[2]);
    /* No 1080p mode at all: the largest within the cap. */
    drmModeModeInfo odd[] = {mode(2560, 1600, 60, true, false), mode(1600, 900, 60, false, false),
                             mode(1680, 1050, 60, false, false), mode(1280, 720, 60, false, false)};
    CHECK(rb_mode_pick_capped(odd, 4, &odd[0], 1920, 1080) == &odd[2]);
    /* Nothing within the cap: none (the caller keeps its mode). */
    drmModeModeInfo big[] = {mode(3840, 2160, 60, true, false), mode(2560, 1440, 60, false, false)};
    CHECK(rb_mode_pick_capped(big, 2, &big[0], 1920, 1080) == NULL);
}

/* Gemini PDA: geminipda-drm (LK's framebuffer, one DSI connector) beside
 * panfrost's render-only Mali card, in either probe order. */
static void test_gemini_cards(void)
{
    char out[RB_MAX_CARDS][64];
    struct rb_card_info a[] = {CARD("/dev/dri/card0", "geminipda-drm", 1, 0, false, false),
                               CARD("/dev/dri/card1", "panfrost", 0, 0, false, false)};
    CHECK_EQ(rank(a, 2, out), 1);
    CHECK(!strcmp(out[0], "/dev/dri/card0"));
    struct rb_card_info b[] = {CARD("/dev/dri/card0", "panfrost", 0, 0, false, false),
                               CARD("/dev/dri/card1", "geminipda-drm", 1, 0, false, false)};
    CHECK_EQ(rank(b, 2, out), 1);
    CHECK(!strcmp(out[0], "/dev/dri/card1"));
    CHECK(rb_driver_no_readout("geminipda-drm"));
    CHECK(!rb_output_never_commit(DRM_MODE_CONNECTOR_DSI, false || rb_driver_no_readout("geminipda-drm"), 0));
    int w, h;
    unsetenv("RB_AUTO_MODE_MAX");
    CHECK(!rb_auto_mode_cap("geminipda-drm", &w, &h)); /* a fixed panel mode: no cap */
}

static void test_hyperpixel_panel_rotation(void)
{
    char root[] = "/tmp/rb-hyperpixel-XXXXXX";
    CHECK(mkdtemp(root) != NULL);
    char spi[128], panel[128], compatible[160], rotation[160];
    snprintf(spi, sizeof spi, "%s/spi", root);
    snprintf(panel, sizeof panel, "%s/spi/display@0", root);
    snprintf(compatible, sizeof compatible, "%s/compatible", panel);
    snprintf(rotation, sizeof rotation, "%s/rotation", panel);
    CHECK_EQ(mkdir(spi, 0700), 0);
    CHECK_EQ(mkdir(panel, 0700), 0);
    CHECK_EQ(rb_hyperpixel_panel_rotation(root), -1);
    FILE *f = fopen(compatible, "wb");
    const char known[] = "pimoroni,hyperpixel4";
    CHECK(f != NULL);
    if (f) {
        fwrite(known, 1, sizeof known, f);
        fclose(f);
    }
    CHECK_EQ(rb_hyperpixel_panel_rotation(root), -1);
    f = fopen(rotation, "wb");
    const unsigned char ninety[] = {0, 0, 0, 90};
    CHECK(f != NULL);
    if (f) {
        fwrite(ninety, 1, sizeof ninety, f);
        fclose(f);
    }
    CHECK_EQ(rb_hyperpixel_panel_rotation(root), 90);
    /* Square is a different panel: a partial compatible match is unsafe. */
    f = fopen(compatible, "wb");
    const char square[] = "pimoroni,hyperpixel4square";
    if (f) {
        fwrite(square, 1, sizeof square, f);
        fclose(f);
    }
    CHECK_EQ(rb_hyperpixel_panel_rotation(root), -1);
    unlink(compatible);
    unlink(rotation);
    rmdir(panel);
    rmdir(spi);
    rmdir(root);
}

int main(void)
{
    test_hyperpixel_panel_rotation();
    test_gemini_cards();
    test_pi_cards();
    test_pc_cards_unchanged();
    test_connector_choice();
    test_dsi_on_a_pi_is_used();
    test_mode_cap();
    TEST_DONE();
}
