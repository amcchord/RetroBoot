/* rb-presenter: RetroBoot display/input owner. See presenter/README.md. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "diagview.h"
#include "drm.h"
#include "log.h"
#include "png.h"
#include "presenter.h"
#include "scale.h"
#include "splash.h"
#include "startup.h"

#ifndef RB_VERSION
#define RB_VERSION "dev"
#endif

static bool parse_size(const char *v, int *w, int *h)
{
    char tail;
    return v && sscanf(v, "%dx%d%c", w, h, &tail) == 2 && *w > 0 && *h > 0;
}

/* --write-checkerboard W H S OUT [options]: GRUB's background, printed as one
 * JSON line for rb-bootdisplay. */
static int write_background(int argc, char **argv)
{
    struct rb_boot_background b;
    memset(&b, 0, sizeof b);
    b.w = atoi(argv[0]);
    b.h = atoi(argv[1]);
    b.s = atoi(argv[2]);
    const char *out = argv[3];
    for (int i = 4; i < argc; i++) {
        const char *o = argv[i], *v = i + 1 < argc ? argv[i + 1] : NULL;
        if (!v) {
            fprintf(stderr, "rb-presenter: %s needs a value\n", o);
            return 2;
        }
        i++;
        if (!strcmp(o, "--pixel-scale")) {
            b.pixel_req = strcmp(v, "auto") ? atoi(v) : 0;
        } else if (!strcmp(o, "--style")) {
            if (strcmp(v, "classic") && strcmp(v, "macosx")) {
                fprintf(stderr, "rb-presenter: bad startup style %s\n", v);
                return 2;
            }
            b.style = !strcmp(v, "macosx") ? RB_STARTUP_OSX : RB_STARTUP_CLASSIC;
        } else if (!strcmp(o, "--checker")) {
            b.checker = strcmp(v, "auto") ? atoi(v) : 0;
        } else if (!strcmp(o, "--mm")) {
            if (!parse_size(v, &b.mm_w, &b.mm_h))
                b.mm_w = b.mm_h = 0;
        } else if (!strcmp(o, "--rotation")) {
            b.rot = atoi(v);
        } else if (!strcmp(o, "--progress")) {
            b.indicator = !strcmp(v, "on");
        } else if (!strcmp(o, "--guest-size")) {
            if (strcmp(v, "auto") && !parse_size(v, &b.guest_w, &b.guest_h)) {
                fprintf(stderr, "rb-presenter: bad --guest-size %s\n", v);
                return 2;
            }
        } else if (!strcmp(o, "--art")) {
            b.art = v;
        } else if (!strcmp(o, "--value")) {
            b.value = atof(v);
        } else if (!strcmp(o, "--light")) {
            b.light_off = !strcmp(v, "off");
        } else {
            fprintf(stderr, "rb-presenter: unknown background option %s\n", o);
            return 2;
        }
    }
    if (b.pixel_req < 0 || b.pixel_req > RB_PIXEL_SCALE_MAX || b.s < 0 || b.checker < 0) {
        fprintf(stderr, "rb-presenter: bad scale\n");
        return 2;
    }
    if (rb_boot_background_write_png(&b, out) < 0) {
        fprintf(stderr, "rb-presenter: cannot write %s\n", out);
        return 1;
    }
    printf("{\"png\":\"%s\",\"width\":%d,\"height\":%d,\"rotation\":%d,\"scale\":%d,\"checker_scale\":%d,"
           "\"checker_multiple\":%d,\"anchor\":[%d,%d],\"progress\":\"%s\",\"art\":\"%s\",\"indicator\":[%d,%d,%d]}\n",
           out, b.w, b.h, b.rot, b.s, b.checker, b.checker_multiple, b.geom.gx, b.geom.gy, b.indicator ? "on" : "off",
           rb_icon_source_name(b.art_source), b.geom.x, b.geom.y, b.geom.q);
    return 0;
}

/* --write-startup-details W H S VIEW OUT.png [--hint]: the Startup Details
 * view (ARCHITECTURE 16) of a view file as every drawer shows it (lab,
 * documentation). --hint draws only the hint, over a white frame. */
static int write_details(int argc, char **argv)
{
    int w = atoi(argv[0]), h = atoi(argv[1]), s = atoi(argv[2]);
    bool hint = argc > 5 && !strcmp(argv[5], "--hint");
    if (w < 64 || h < 64 || w > 8192 || h > 8192 || s < 1 || s > 8) {
        fprintf(stderr, "rb-presenter: bad size or scale\n");
        return 2;
    }
    static char text[64 * 1024];
    static struct rb_diag_doc doc;
    ssize_t n = rb_diag_read_file(argv[3], text, sizeof text);
    if (n < 0) {
        fprintf(stderr, "rb-presenter: cannot read %s\n", argv[3]);
        return 1;
    }
    rb_diag_doc_parse(text, (size_t)n, &doc);
    uint32_t *px = malloc((size_t)w * h * 4);
    if (!px)
        return 1;
    if (hint) {
        for (int i = 0; i < w * h; i++)
            px[i] = 0xffffff;
        rb_diag_render_hint(px, w, h, w, s);
    } else {
        rb_diag_render(px, w, h, w, s, &doc);
    }
    int rc = rb_png_write_xrgb(argv[4], px, w, h, w);
    free(px);
    return rc ? 1 : 0;
}

static void usage(FILE *f)
{
    fprintf(f,
            "usage: rb-presenter [options]\n"
            "       rb-presenter --splash\n"
            "       rb-presenter --splash-chroot DIR\n"
            "       rb-presenter --write-checkerboard W H S OUT.png [background options]\n"
            "           (GRUB's background; S=0: the 4.5 scale) background options:\n"
            "           --pixel-scale auto|N  configured pixel scale (guest layout)\n"
            "           --checker N|auto      pattern scale (auto: DPI-aware from --mm)\n"
            "           --style classic|macosx  preboot presentation for the selected Mac\n"
            "           --mm WxH              physical size of the display\n"
            "           --rotation 0|90|180|270  panel content rotation (W H are the scanout)\n"
            "           --progress on|off     draw the startup indicator (default off)\n"
            "           --guest-size WxH|auto --art FILE.rbi  --value P (bar, 0..1)\n"
            "           --light on|off        the green light (renders of the blink)\n"
            "       rb-presenter --write-startup-details W H S VIEW-FILE OUT.png [--hint]\n"
            "           (Startup Details as drawn at W x H, pixel scale S, from a view file)\n"
            "options:\n"
            "  --display-socket PATH   default /run/retroboot/display.sock\n"
            "  --control-socket PATH   default /run/retroboot/presenter.sock\n"
            "  --remote-socket PATH    default /run/retroboot/remote.sock (web Screen page; empty: off)\n"
            "  --socket-group NAME     group allowed to connect to the display socket (default rbguest)\n"
            "  --device /dev/dri/cardN force a DRM device\n"
            "  --device virtual:WxH[@HZ]  lab: a virtual display (no DRM device; flips at HZ, default 60)\n"
            "  --record FILE           lab: write every frame the virtual display presents (framelog.h)\n"
            "  --record-guest FILE     lab: write every guest frame as it arrives (framelog.h)\n"
            "  --connector NAME        prefer a connector (e.g. eDP-1)\n"
            "  --scene splash|black    initial scene (default splash)\n"
            "  --no-vt                 leave the VT alone\n"
            "  --no-input              do not open input devices\n"
            "  --no-handoff            do not contact an initramfs splash\n"
            "  --restore-vt            restore VT text mode on exit (development)\n"
            "  --survive-shutdown      mark argv[0] with '@' so systemd's final kill skips us\n"
            "  --version\n");
}

int main(int argc, char **argv)
{
    struct rb_opts o = {
        .display_socket = "/run/retroboot/display.sock",
        .control_socket = "/run/retroboot/presenter.sock",
        .remote_socket = "/run/retroboot/remote.sock",
        .socket_group = "rbguest",
    };
    bool survive = false;
    for (int i = 1; i < argc; i++) {
        const char *a = argv[i];
        const char *next = i + 1 < argc ? argv[i + 1] : NULL;
        if (!strcmp(a, "--splash")) {
            return rb_splash_main(argc, argv);
        } else if (!strcmp(a, "--splash-chroot") && next) {
            return rb_splash_send_chroot(next);
        } else if (!strcmp(a, "--write-checkerboard")) {
            if (i + 4 >= argc) {
                usage(stderr);
                return 2;
            }
            return write_background(argc - i - 1, argv + i + 1);
        } else if (!strcmp(a, "--write-startup-details")) {
            if (i + 5 >= argc) {
                usage(stderr);
                return 2;
            }
            return write_details(argc - i - 1, argv + i + 1);
        } else if (!strcmp(a, "--display-socket") && next) {
            o.display_socket = argv[++i];
        } else if (!strcmp(a, "--control-socket") && next) {
            o.control_socket = argv[++i];
        } else if (!strcmp(a, "--remote-socket") && next) {
            o.remote_socket = argv[++i];
        } else if (!strcmp(a, "--socket-group") && next) {
            o.socket_group = argv[++i];
        } else if (!strcmp(a, "--device") && next) {
            o.device = argv[++i];
        } else if (!strcmp(a, "--record") && next) {
            rb_virtual_record = argv[++i];
        } else if (!strcmp(a, "--record-guest") && next) {
            o.record_guest = argv[++i];
        } else if (!strcmp(a, "--connector") && next) {
            o.connector = argv[++i];
        } else if (!strcmp(a, "--scene") && next) {
            o.initial_scene = argv[++i];
        } else if (!strcmp(a, "--no-vt")) {
            o.no_vt = true;
        } else if (!strcmp(a, "--no-input")) {
            o.no_input = true;
        } else if (!strcmp(a, "--no-handoff")) {
            o.no_handoff = true;
        } else if (!strcmp(a, "--restore-vt")) {
            o.restore_vt = true;
        } else if (!strcmp(a, "--survive-shutdown")) {
            survive = true;
        } else if (!strcmp(a, "--version")) {
            printf("rb-presenter %s\n", RB_VERSION);
            return 0;
        } else if (!strcmp(a, "--help") || !strcmp(a, "-h")) {
            usage(stdout);
            return 0;
        } else {
            fprintf(stderr, "rb-presenter: unknown option %s\n", a);
            usage(stderr);
            return 2;
        }
    }
    if (survive && argv[0][0])
        argv[0][0] = '@';
    return rb_presenter_main(&o);
}
