#pragma once
#include <stdbool.h>
#include <stdint.h>

#include "startup.h"

/* Write an XRGB8888 buffer as an 8-bit RGB PNG (atomically: path.tmp then
 * rename). Returns 0 on success, -1 on failure. */
int rb_png_write_xrgb(const char *path, const uint32_t *px, int w, int h, int stride_px);

/* Write a W x H RGB PNG of the plain checkerboard at scale s, anchored at the
 * screen origin. Returns 0 on success. */
int rb_checker_write_png(int w, int h, int s, const char *path);

/* GRUB's background (rb-bootdisplay): the startup scene (rb_startup_scene:
 * the checkerboard at pattern scale c anchored at Mac OS's picture, plus the
 * startup indicator, 4.4.1) on the logical frame (the scanout rotated by rot),
 * through the same output transform as the splash and the presenter. */
struct rb_boot_background {
    int style;            /* enum rb_startup_style */
    int w, h;             /* scanout */
    int rot;              /* 0/90/180/270, the panel's content rotation */
    int s;                /* effective pixel scale (0: the 4.5 scale of the logical frame) */
    int pixel_req;        /* configured pixel scale, 0 = auto */
    int checker;          /* pattern scale, 0 = s * rb_checker_multiple(mm) */
    int mm_w, mm_h;       /* physical size, 0 = unknown */
    bool indicator;
    int guest_w, guest_h; /* 0 = auto */
    const char *art;      /* Platinum bundle icon (.rbi), NULL = built-in art */
    double value;         /* bar progress drawn (0 for GRUB) */
    bool light_off;       /* the green light off (renders of the blink; GRUB: on) */
    /* results */
    int checker_multiple;
    int art_source;
    struct rb_startup_geom geom;
};
int rb_boot_background_write_png(struct rb_boot_background *b, const char *path);
/* The same into a w x h buffer (stride w). */
int rb_boot_background_render(struct rb_boot_background *b, uint32_t *buf);
