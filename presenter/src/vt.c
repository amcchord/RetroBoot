#include "vt.h"

#include <errno.h>
#include <fcntl.h>
#include <linux/kd.h>
#include <linux/vt.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

#include "log.h"

static int vt_fd = -1;
static int vt_num = 0;
static int saved_mode = KD_TEXT;
static int saved_kbmode = K_UNICODE;

int rb_vt_take(void)
{
    if (vt_fd >= 0)
        return 0;
    int t0 = open("/dev/tty0", O_RDWR | O_CLOEXEC | O_NOCTTY);
    if (t0 < 0) {
        rb_log("vt: /dev/tty0 unavailable (%s); no VT handling", strerror(errno));
        return 0;
    }
    struct vt_stat st;
    if (ioctl(t0, VT_GETSTATE, &st) < 0) {
        rb_log("vt: VT_GETSTATE failed: %s", strerror(errno));
        close(t0);
        return -1;
    }
    close(t0);
    char path[32];
    snprintf(path, sizeof path, "/dev/tty%d", st.v_active);
    vt_fd = open(path, O_RDWR | O_CLOEXEC | O_NOCTTY);
    if (vt_fd < 0) {
        rb_log("vt: open %s: %s", path, strerror(errno));
        return -1;
    }
    vt_num = st.v_active;
    int mode = KD_TEXT;
    long kb = K_UNICODE;
    if (ioctl(vt_fd, KDGETMODE, &mode) == 0)
        saved_mode = mode;
    if (ioctl(vt_fd, KDGKBMODE, &kb) == 0)
        saved_kbmode = (int)kb;
    if (saved_kbmode == K_OFF)
        saved_kbmode = K_UNICODE;
    if (ioctl(vt_fd, KDSETMODE, KD_GRAPHICS) < 0)
        rb_log("vt: KDSETMODE KD_GRAPHICS on tty%d: %s", vt_num, strerror(errno));
    if (ioctl(vt_fd, KDSKBMODE, K_OFF) < 0)
        rb_log("vt: KDSKBMODE K_OFF on tty%d: %s", vt_num, strerror(errno));
    rb_log("vt: tty%d graphics mode, keyboard off (was mode %d kb %d)", vt_num, mode, (int)kb);
    return 0;
}

void rb_vt_restore(void)
{
    if (vt_fd < 0)
        return;
    ioctl(vt_fd, KDSKBMODE, saved_kbmode);
    ioctl(vt_fd, KDSETMODE, saved_mode == KD_GRAPHICS ? KD_TEXT : saved_mode);
    close(vt_fd);
    vt_fd = -1;
    rb_log("vt: tty%d restored", vt_num);
}

int rb_vt_number(void) { return vt_num; }
