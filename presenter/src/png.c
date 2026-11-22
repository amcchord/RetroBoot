#include "png.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "checker.h"
#include "scale.h"
#include "xform.h"
#include "stb_image_write.h"

int rb_png_write_xrgb(const char *path, const uint32_t *px, int w, int h, int stride_px)
{
    if (!path || w <= 0 || h <= 0)
        return -1;
    unsigned char *rgb = malloc((size_t)w * h * 3);
    if (!rgb)
        return -1;
    for (int y = 0; y < h; y++) {
        const uint32_t *src = px + (size_t)y * stride_px;
        unsigned char *dst = rgb + (size_t)y * w * 3;
        for (int x = 0; x < w; x++) {
            uint32_t p = src[x];
            dst[x * 3 + 0] = (unsigned char)(p >> 16);
            dst[x * 3 + 1] = (unsigned char)(p >> 8);
            dst[x * 3 + 2] = (unsigned char)p;
        }
    }
    char tmp[4096];
    if (snprintf(tmp, sizeof tmp, "%s.tmp", path) >= (int)sizeof tmp) {
        free(rgb);
        return -1;
    }
    int ok = stbi_write_png(tmp, w, h, 3, rgb, w * 3);
    free(rgb);
    if (!ok) {
        unlink(tmp);
        return -1;
    }
    if (rename(tmp, path) < 0) {
        unlink(tmp);
        return -1;
    }
    return 0;
}

int rb_checker_write_png(int w, int h, int s, const char *path)
{
    if (w <= 0 || h <= 0 || w > 16384 || h > 16384 || s < 1)
        return -1;
    uint32_t *buf = malloc((size_t)w * h * 4);
    if (!buf)
        return -1;
    rb_checker_fill_at(buf, w, 0, 0, w, h, s, (struct rb_checker_anchor){0, 0});
    int rc = rb_png_write_xrgb(path, buf, w, h, w);
    free(buf);
    return rc;
}

int rb_boot_background_render(struct rb_boot_background *b, uint32_t *buf)
{
    if (b->w <= 0 || b->h <= 0 || b->w > 16384 || b->h > 16384 || !buf)
        return -1;
    int rot = (b->rot == 90 || b->rot == 180 || b->rot == 270) ? b->rot : 0;
    bool swap = rot == 90 || rot == 270;
    int lw = swap ? b->h : b->w, lh = swap ? b->w : b->h; /* logical frame */
    if (b->s <= 0)
        b->s = rb_effective_scale(lw, lh, b->pixel_req);
    b->checker_multiple = b->checker > 0 ? 0 : rb_checker_multiple(lw, lh, b->s, b->mm_w, b->mm_h);
    if (b->checker <= 0)
        b->checker = b->s * b->checker_multiple;
    else if (b->checker % b->s == 0)
        b->checker_multiple = b->checker / b->s;
    /* The splash and the presenter compose the logical (rotated) frame -- the
     * pattern anchored at Mac OS's picture, the indicator (rb_startup_scene)
     * -- and their output transform rotates it into the scanout; GRUB's
     * background is that same frame through the same transform, so all three
     * are the same pixels for every layout and rotation. */
    uint32_t *logical = rot ? malloc((size_t)lw * lh * 4) : buf;
    if (!logical)
        return -1;
    rb_startup_geometry(&b->geom, lw, lh, b->s, b->pixel_req, b->guest_w, b->guest_h);
    struct rb_icon_art art;
    rb_icon_art_builtin(&art);
    b->art_source = b->indicator ? rb_icon_art_load(&art, b->art) : RB_ICON_BUILTIN;
    if (b->style == RB_STARTUP_OSX)
        b->indicator = false;
    rb_startup_scene_style(logical, lw, lw, lh, (rb_rect){0, 0, lw, lh}, b->checker, &b->geom, &art, b->indicator,
                           rb_startup_fill(b->value), !b->light_off, b->style);
    if (rot) {
        struct rb_xform x;
        memset(&x, 0, sizeof x);
        int rc = rb_xform_setup(&x, rot, b->w, b->h, lw, lh, false);
        if (rc == 0)
            rb_xform_blit(&x, buf, b->w, logical, lw, (rb_rect){0, 0, lw, lh});
        rb_xform_free(&x);
        free(logical);
        if (rc < 0)
            return -1;
    }
    return 0;
}

int rb_boot_background_write_png(struct rb_boot_background *b, const char *path)
{
    if (b->w <= 0 || b->h <= 0 || b->w > 16384 || b->h > 16384)
        return -1;
    uint32_t *buf = malloc((size_t)b->w * b->h * 4);
    if (!buf)
        return -1;
    int rc = rb_boot_background_render(b, buf);
    if (rc == 0)
        rc = rb_png_write_xrgb(path, buf, b->w, b->h, b->w);
    free(buf);
    return rc;
}
