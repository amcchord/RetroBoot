/*
 * rb-standin-presenter — TEMPORARY image-workstream stand-in, NOT the product
 * presenter. It exists only so boot-silence tests can run before the real
 * rb-presenter (presenter/, workstream presenter-core) is available. The image
 * build installs it only when no presenter artifact is supplied, and records
 * "presenter": "standin" in the image manifest.
 *
 * Behaviour: wait for a DRM device, become DRM master, keep the mode GRUB or the
 * firmware left behind (from /sys/kernel/boot_params screen_info; else the
 * connector's preferred mode), draw the ARCHITECTURE.md section 4.4 checkerboard
 * at the section 4.5 scale, and hold it until SIGTERM. Then it blanks to black
 * (the "black" scene) so poweroff ends on a black screen.
 *
 * Build: cc -O2 -Wall -o rb-standin-presenter rb-standin-presenter.c $(pkg-config --cflags --libs libdrm)
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <glob.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include <xf86drm.h>
#include <xf86drmMode.h>
#include <drm_fourcc.h>

static volatile sig_atomic_t stop;
static void on_signal(int sig) { (void)sig; stop = 1; }

static double now_s(void) {
    struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

/* struct screen_info lives at offset 0 of struct boot_params (x86). */
static int boot_fb_size(unsigned *w, unsigned *h, unsigned *type) {
    unsigned char b[0x26];
    int fd = open("/sys/kernel/boot_params/data", O_RDONLY | O_CLOEXEC);
    if (fd < 0) return -1;
    ssize_t n = read(fd, b, sizeof b);
    close(fd);
    if (n != (ssize_t)sizeof b) return -1;
    *type = b[0x0f];
    *w = b[0x12] | b[0x13] << 8;
    *h = b[0x14] | b[0x15] << 8;
    return (*type == 0x23 || *type == 0x70) && *w && *h ? 0 : -1;
}

static unsigned suggested_scale(unsigned w, unsigned h) {
    unsigned s = 1;
    while (w / (s + 1) >= 720 && h / (s + 1) >= 540) s++;
    return s;
}

static int open_card(void) {
    for (int tries = 0; !stop; tries++) {
        glob_t g;
        if (glob("/dev/dri/card*", 0, NULL, &g) == 0) {
            for (size_t i = 0; i < g.gl_pathc; i++) {
                int fd = open(g.gl_pathv[i], O_RDWR | O_CLOEXEC);
                if (fd < 0) continue;
                drmModeRes *res = drmModeGetResources(fd);
                if (res && res->count_connectors > 0) {
                    fprintf(stderr, "standin: using %s after %d polls\n", g.gl_pathv[i], tries);
                    drmModeFreeResources(res); globfree(&g); return fd;
                }
                if (res) drmModeFreeResources(res);
                close(fd);
            }
            globfree(&g);
        }
        usleep(20000);
    }
    return -1;
}

int main(void) {
    signal(SIGTERM, on_signal); signal(SIGINT, on_signal); signal(SIGHUP, SIG_IGN);
    double t0 = now_s();
    int fd = open_card();
    if (fd < 0) return 1;
    for (int i = 0; drmSetMaster(fd) != 0 && i < 250; i++) usleep(20000);

    drmModeRes *res = drmModeGetResources(fd);
    drmModeConnector *conn = NULL;
    for (int i = 0; res && i < res->count_connectors; i++) {
        drmModeConnector *c = drmModeGetConnector(fd, res->connectors[i]);
        if (c && c->connection == DRM_MODE_CONNECTED && c->count_modes > 0) { conn = c; break; }
        drmModeFreeConnector(c);
    }
    if (!conn) { fprintf(stderr, "standin: no connected connector\n"); pause(); return 1; }

    unsigned bw = 0, bh = 0, btype = 0;
    int have_boot = boot_fb_size(&bw, &bh, &btype) == 0;
    drmModeModeInfo *mode = NULL;
    const char *source = "preferred";
    if (have_boot)
        for (int i = 0; i < conn->count_modes && !mode; i++)
            if (conn->modes[i].hdisplay == bw && conn->modes[i].vdisplay == bh) { mode = &conn->modes[i]; source = "boot-framebuffer"; }
    for (int i = 0; i < conn->count_modes && !mode; i++)
        if (conn->modes[i].type & DRM_MODE_TYPE_PREFERRED) mode = &conn->modes[i];
    if (!mode) mode = &conn->modes[0];

    drmModeEncoder *enc = conn->encoder_id ? drmModeGetEncoder(fd, conn->encoder_id) : NULL;
    uint32_t crtc_id = enc && enc->crtc_id ? enc->crtc_id : 0;
    if (!crtc_id) {
        for (int e = 0; e < conn->count_encoders && !crtc_id; e++) {
            drmModeEncoder *en = drmModeGetEncoder(fd, conn->encoders[e]);
            for (int c = 0; en && c < res->count_crtcs; c++)
                if (en->possible_crtcs & (1u << c)) { crtc_id = res->crtcs[c]; break; }
            drmModeFreeEncoder(en);
        }
    }

    /*
     * bochs-drm (QEMU std VGA) enables the pipe with VBE_DISPI_ENABLED but without
     * VBE_DISPI_NOCLEARMEM, so QEMU zeroes VRAM [0, yres * pitch) on the first
     * modeset. The first scanout BO lands at VRAM offset 0, which would make the
     * screen go black. Allocate a double-height BO and scan out its second half,
     * which lies outside the cleared range.
     */
    drmVersionPtr ver = drmGetVersion(fd);
    int bochs = ver && ver->name && strcmp(ver->name, "bochs-drm") == 0;
    drmFreeVersion(ver);
    struct drm_mode_create_dumb cd = { .width = mode->hdisplay, .height = mode->vdisplay * (bochs ? 2 : 1), .bpp = 32 };
    if (drmIoctl(fd, DRM_IOCTL_MODE_CREATE_DUMB, &cd)) { perror("create_dumb"); return 1; }
    uint32_t visible_offset = bochs ? cd.pitch * mode->vdisplay : 0;
    uint32_t handles[4] = { cd.handle }, pitches[4] = { cd.pitch }, offsets[4] = { visible_offset }, fb_id = 0;
    if (drmModeAddFB2(fd, mode->hdisplay, mode->vdisplay, DRM_FORMAT_XRGB8888, handles, pitches, offsets, &fb_id, 0)) { perror("addfb2"); return 1; }
    struct drm_mode_map_dumb md = { .handle = cd.handle };
    if (drmIoctl(fd, DRM_IOCTL_MODE_MAP_DUMB, &md)) { perror("map_dumb"); return 1; }
    uint8_t *px = mmap(NULL, cd.size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, md.offset);
    if (px == MAP_FAILED) { perror("mmap"); return 1; }

    unsigned s = suggested_scale(mode->hdisplay, mode->vdisplay);
    for (unsigned y = 0; y < mode->vdisplay; y++) {
        uint32_t *row = (uint32_t *)(px + visible_offset + (size_t)y * cd.pitch);
        for (unsigned x = 0; x < mode->hdisplay; x++)
            row[x] = ((x / s + y / s) & 1) ? 0x00000000u : 0x00ffffffu;
    }
    if (drmModeSetCrtc(fd, crtc_id, fb_id, 0, 0, &conn->connector_id, 1, mode)) { perror("setcrtc"); return 1; }
    fprintf(stderr, "standin: mode %ux%u@%u source=%s boot_fb=%ux%u type=0x%x scale=%u bochs_offset=%u t=%.3fs\n",
            mode->hdisplay, mode->vdisplay, mode->vrefresh, source, bw, bh, btype, s, visible_offset, now_s() - t0);

    mkdir("/run/retroboot", 0755);
    FILE *f = fopen("/run/retroboot/display.json", "w");
    if (f) {
        fprintf(f, "{\"presenter\":\"standin\",\"mode\":[%u,%u,%u],\"suggested_scale\":%u,\"mode_source\":\"%s\",\"boot_framebuffer\":[%u,%u],\"boot_framebuffer_type\":%u}\n",
                mode->hdisplay, mode->vdisplay, mode->vrefresh, s, source, bw, bh, btype);
        fclose(f);
    }
    FILE *tty = fopen("/dev/ttyS0", "w");
    if (tty) { fprintf(tty, "\r\nRBMARK presenter-standin-shown\r\n"); fclose(tty); }

    while (!stop) pause();
    memset(px, 0, cd.size);   /* "black" scene before poweroff */
    usleep(100000);
    return 0;
}
