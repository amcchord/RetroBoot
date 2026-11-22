/* Early-boot splash (initramfs) and its display handoff to the presenter.
 *
 * The splash draws the checkerboard on every DRM device that appears and
 * listens on the abstract SOCK_SEQPACKET socket @retroboot-splash:
 *   presenter -> "handoff"   splash drops DRM master, keeps its framebuffer
 *   splash    -> "ready <devnode> <connector> <w> <h> <refresh>"
 *   presenter    becomes master, commits an identical checkerboard
 *   presenter -> "done"      splash closes its devices and exits
 * If the presenter disconnects (or sends "abort") before "done", the splash
 * takes master back and recommits its framebuffer -- only on the devices it
 * held: once a presenter has asked for the display, the splash never takes a
 * device that appears later (the presenter does), and a splash that held
 * none exits. Otherwise a KMS driver that binds after the presenter started
 * (amdgpu from the root file system) went to the splash, and the presenter
 * could never become DRM master (image/reports/hostboot-2026-10-12.md).
 *   helper    -> "chroot <dir>"  (initramfs init-bottom) splash chroots into
 *                the real root so it keeps working after switch_root.
 * During a handoff the presenter also asks for the startup indicator as drawn
 * (ARCHITECTURE 4.4.1), so its first frame is the splash's last one:
 *   presenter -> "startup"   splash -> "startup on|off <timed> <t0_ms> <p0*1e6>
 *                            <eta_ms> <fill> builtin|bundle <blink_t0_ms> <lit>"
 *                            (CLOCK_BOOTTIME)
 *   presenter -> "icon"      splash -> "icon " + the 32x32 art (u32 0xffRRGGBB)
 * and, in installer mode, the installer startup screen's state (4.4.2):
 *   presenter -> "isplash"   splash -> rb_isplash_state_format's line
 */
#pragma once
#include <stdbool.h>
#include <stdint.h>

#define RB_SPLASH_SOCKET "retroboot-splash" /* abstract namespace */

int rb_splash_main(int argc, char **argv);

/* Whether the splash may take a DRM device that appeared: never during a
 * handoff and never after a presenter has asked for the display. */
bool rb_splash_may_take_device(bool handing_off, bool handoff_requested);
/* After an aborted handoff (or the presenter going away): keep running and
 * take back the devices held, or exit when there were none. */
bool rb_splash_keep_after_abort(int devices_held);
/* Client helper for the initramfs init-bottom script. */
int rb_splash_send_chroot(const char *dir);

struct rb_splash_handoff {
    int fd;
    bool active;
    char devnode[64];
    char connector[32];
    int w, h, refresh;
    uint64_t start_mono_ms, ready_mono_ms;
};

/* Returns 0 when a splash is running and has released its device, -1 when
 * there is no splash (normal start). */
int rb_splash_handoff_begin(struct rb_splash_handoff *h, int timeout_ms);
void rb_splash_handoff_finish(struct rb_splash_handoff *h, bool ok);
/* The presenter's answer after a handoff: "done" (true) when it shows the
 * frame, or when the splash released no device (there is nothing for it to
 * keep; the presenter takes the device when its driver binds); "abort" only
 * when the splash should keep showing a device it held. */
bool rb_splash_handoff_complete(const struct rb_splash_handoff *h, bool presenter_on_screen);

struct rb_splash_startup {
    int style; /* enum rb_startup_style */
    bool known; /* the splash answered (an older splash does not) */
    bool indicator;
    bool timed;
    uint64_t t0, eta; /* CLOCK_BOOTTIME ms */
    double p0;
    int fill;
    int source; /* enum rb_icon_source */
    uint64_t blink_t0; /* the light's blink origin (CLOCK_BOOTTIME) */
    bool lit;          /* light drawn on */
    bool have_art;
    uint32_t art[32 * 32];
};
/* After rb_splash_handoff_begin: the splash's startup indicator. */
int rb_splash_handoff_startup(struct rb_splash_handoff *h, struct rb_splash_startup *out);
