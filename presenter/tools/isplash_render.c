/* isplash-render: draw the installer startup screen (ARCHITECTURE 4.4.2) to a
 * PNG with the presenter's own code -- the frame the splash and the presenter
 * show, or only the box -- for documentation renders and the lab's pixel
 * comparison with Mac OS (tools/isplash_compare.py). Lab tool, not installed.
 *
 *   isplash-render W H OUT.png [--art FILE.rbis] [--phase welcome|starting]
 *       [--fill N | --value P] [--pixel-scale auto|N] [--guest-size WxH] [--box-only]
 *
 * Prints one JSON line: the box's place and scale, the art used.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "installer_splash.h"
#include "png.h"
#include "scale.h"
#include "startup.h"

int main(int argc, char **argv)
{
    if (argc < 4) {
        fprintf(stderr, "usage: isplash-render W H OUT.png [--art FILE.rbis] [--phase welcome|starting] "
                        "[--fill N | --value P] [--pixel-scale auto|N] [--guest-size WxH] [--box-only]\n");
        return 2;
    }
    int W = atoi(argv[1]), H = atoi(argv[2]);
    const char *out = argv[3], *art_path = NULL;
    int phase = RB_ISPLASH_STARTING, fill = -1, pixel_req = 0, gw = 0, gh = 0;
    double value = 0.4;
    int box_only = 0;
    for (int i = 4; i < argc; i++) {
        const char *o = argv[i], *v = i + 1 < argc ? argv[i + 1] : "";
        if (!strcmp(o, "--box-only")) {
            box_only = 1;
            continue;
        }
        i++;
        if (!strcmp(o, "--art"))
            art_path = v;
        else if (!strcmp(o, "--phase"))
            phase = strcmp(v, "welcome") ? RB_ISPLASH_STARTING : RB_ISPLASH_WELCOME;
        else if (!strcmp(o, "--fill"))
            fill = atoi(v);
        else if (!strcmp(o, "--value"))
            value = atof(v);
        else if (!strcmp(o, "--pixel-scale"))
            pixel_req = strcmp(v, "auto") ? atoi(v) : 0;
        else if (!strcmp(o, "--guest-size")) {
            if (sscanf(v, "%dx%d", &gw, &gh) != 2)
                gw = gh = 0;
        } else {
            fprintf(stderr, "isplash-render: unknown option %s\n", o);
            return 2;
        }
    }
    if (fill < 0)
        fill = rb_isplash_fill(value);
    struct rb_isplash_art art;
    if (rb_isplash_art_load(&art, art_path) < 0)
        return 1;
    uint32_t *box = malloc((size_t)RB_ISPLASH_BOX_W * RB_ISPLASH_BOX_H * 4);
    if (!box)
        return 1;
    rb_isplash_compose(box, &art, phase, fill);
    if (box_only) {
        int rc = rb_png_write_xrgb(out, box, RB_ISPLASH_BOX_W, RB_ISPLASH_BOX_H, RB_ISPLASH_BOX_W);
        printf("{\"png\":\"%s\",\"art\":\"%s\",\"phase\":\"%s\",\"fill\":%d}\n", out,
               rb_isplash_source_name(art.source), phase ? "starting" : "welcome", fill);
        return rc ? 1 : 0;
    }
    if (W <= 0 || H <= 0 || W > 16384 || H > 16384)
        return 2;
    int s = rb_effective_scale(W, H, pixel_req);
    uint32_t *fb = malloc((size_t)W * H * 4);
    if (!fb)
        return 1;
    /* the startup scene as the splash draws it in installer mode: the
     * checkerboard anchored at Mac OS's picture (no Happy Mac), then the box */
    struct rb_startup_geom sg;
    rb_startup_geometry(&sg, W, H, s, pixel_req, gw, gh);
    struct rb_icon_art icon;
    rb_icon_art_builtin(&icon);
    rb_startup_scene(fb, W, W, H, (rb_rect){0, 0, W, H}, s, &sg, &icon, false, 0, true);
    struct rb_isplash_geom g;
    rb_isplash_geometry(&g, W, H, s, pixel_req, gw, gh);
    rb_isplash_blit(fb, W, W, H, (rb_rect){0, 0, W, H}, &g, box);
    int rc = rb_png_write_xrgb(out, fb, W, H, W);
    printf("{\"png\":\"%s\",\"width\":%d,\"height\":%d,\"scale\":%d,\"box\":[%d,%d,%d],\"guest_box\":[%d,%d],"
           "\"art\":\"%s\",\"phase\":\"%s\",\"fill\":%d}\n",
           out, W, H, s, g.x, g.y, g.k, g.bx, g.by, rb_isplash_source_name(art.source), phase ? "starting" : "welcome",
           fill);
    free(fb);
    free(box);
    rb_isplash_art_free(&art);
    return rc ? 1 : 0;
}
