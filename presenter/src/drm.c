#include "drm.h"

#include <errno.h>
#include <fcntl.h>
#include <libudev.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/timerfd.h>
#include <time.h>
#include <unistd.h>
#include <drm_fourcc.h>
#include <xf86drm.h>

#include "log.h"
#include "rotation.h"
#include "startup.h"

int rb_mode_refresh(const drmModeModeInfo *m)
{
    if (m->vrefresh)
        return (int)m->vrefresh;
    if (m->htotal && m->vtotal)
        return (int)((m->clock * 1000L + (long)m->htotal * m->vtotal / 2) / ((long)m->htotal * m->vtotal));
    return 60;
}

int rb_screen_info_size(const uint8_t *buf, size_t n, int *w, int *h)
{
    if (!buf || n < 0x16)
        return -1;
    uint8_t type = buf[0x0f];
    if (type != 0x23 && type != 0x70) /* VIDEO_TYPE_VLFB, VIDEO_TYPE_EFI */
        return -1;
    int lw = buf[0x12] | buf[0x13] << 8;
    int lh = buf[0x14] | buf[0x15] << 8;
    if (lw < 320 || lh < 200)
        return -1;
    *w = lw;
    *h = lh;
    return 0;
}

int rb_boot_fb_size(const char *path, int *w, int *h)
{
    const char *env = getenv("RB_BOOT_PARAMS"); /* tests */
    int fd = open(env && *env ? env : (path ? path : RB_BOOT_PARAMS), O_RDONLY | O_CLOEXEC);
    if (fd < 0)
        return -1;
    uint8_t buf[64];
    ssize_t n = read(fd, buf, sizeof buf);
    close(fd);
    return n > 0 ? rb_screen_info_size(buf, (size_t)n, w, h) : -1;
}

static bool is_firmware_driver(const char *drv)
{
    return drv && (!strcmp(drv, "simpledrm") || !strcmp(drv, "simple-framebuffer") || !strcmp(drv, "efidrm") ||
                   !strcmp(drv, "vesadrm") || !strcmp(drv, "ofdrm"));
}

/* --- card enumeration through sysfs (never opens device nodes: opening and
 * closing a primary node can trigger an fbdev restore on last close) --- */

static void copy_node(char *dst, const char *src)
{
    size_t n = strlen(src);
    if (n > 63)
        n = 63;
    memcpy(dst, src, n);
    dst[n] = 0;
}

static bool is_vc4(const char *drv) { return drv && !strncmp(drv, "vc4", 3); }

bool rb_driver_damage_atomic(const char *driver)
{
    const char *e = getenv("RB_DRM_DAMAGE");
    if (e && !strcmp(e, "atomic"))
        return true;
    if (e && !strcmp(e, "legacy"))
        return false;
    /* geminipda-drm: the shadow-plane copy into LK's scanout (drm.h). */
    return driver && !strcmp(driver, "geminipda-drm");
}

int rb_damage_clips(const struct rb_xform *xf, const rb_region *kd, bool kfull, struct drm_mode_rect *out, int max,
                    bool *full)
{
    *full = kfull;
    if (kfull || max <= 0)
        return 0;
    int n = 0;
    for (int i = 0; i < kd->n; i++) {
        rb_rect r = rb_xform_src_to_fb(xf, kd->r[i]);
        if (rb_rect_empty(r))
            continue;
        if (n >= max) {
            *full = true;
            return 0;
        }
        out[n++] = (struct drm_mode_rect){r.x, r.y, r.x + r.w, r.y + r.h};
    }
    if (!n) /* nothing visible changed: the smallest commit the helper accepts */
        out[n++] = (struct drm_mode_rect){0, 0, 1, 1};
    return n;
}

void rb_output_committed(struct rb_output *o, int idx, bool ok)
{
    if (!ok || idx < 0 || idx > 1)
        return;
    rb_region_clear(&o->kdamage[idx]);
    o->kfull[idx] = false;
}

void rb_output_kernel_damage_all(struct rb_output *o)
{
    for (int i = 0; i < 2; i++) {
        rb_region_clear(&o->kdamage[i]);
        o->kfull[i] = true;
    }
}

/* The primary plane of o's CRTC and its FB_ID / FB_DAMAGE_CLIPS properties,
 * with the atomic client capability on; on any failure the capability goes
 * back off and the output keeps legacy flips. */
static void setup_atomic_damage(struct rb_output *o)
{
    o->atomic_damage = false;
    if (drmSetClientCap(o->fd, DRM_CLIENT_CAP_ATOMIC, 1) < 0) {
        rb_log("display: %s: no atomic modesetting (%s); legacy page flips", o->devnode, strerror(errno));
        return;
    }
    int crtc_index = -1;
    drmModeRes *res = drmModeGetResources(o->fd);
    for (int i = 0; res && i < res->count_crtcs; i++)
        if (res->crtcs[i] == o->crtc_id)
            crtc_index = i;
    if (res)
        drmModeFreeResources(res);
    drmModePlaneRes *pr = crtc_index >= 0 ? drmModeGetPlaneResources(o->fd) : NULL;
    for (uint32_t i = 0; pr && i < pr->count_planes && !o->atomic_damage; i++) {
        drmModePlane *pl = drmModeGetPlane(o->fd, pr->planes[i]);
        if (!pl)
            continue;
        bool ours = pl->possible_crtcs & (1u << crtc_index);
        uint32_t id = pl->plane_id;
        drmModeFreePlane(pl);
        if (!ours)
            continue;
        drmModeObjectProperties *props = drmModeObjectGetProperties(o->fd, id, DRM_MODE_OBJECT_PLANE);
        bool primary = false;
        uint32_t fb_prop = 0, dmg_prop = 0;
        for (uint32_t k = 0; props && k < props->count_props; k++) {
            drmModePropertyRes *prop = drmModeGetProperty(o->fd, props->props[k]);
            if (!prop)
                continue;
            if (!strcmp(prop->name, "type"))
                primary = props->prop_values[k] == DRM_PLANE_TYPE_PRIMARY;
            else if (!strcmp(prop->name, "FB_ID"))
                fb_prop = prop->prop_id;
            else if (!strcmp(prop->name, "FB_DAMAGE_CLIPS"))
                dmg_prop = prop->prop_id;
            drmModeFreeProperty(prop);
        }
        if (props)
            drmModeFreeObjectProperties(props);
        if (primary && fb_prop && dmg_prop) {
            o->plane_id = id;
            o->prop_fb_id = fb_prop;
            o->prop_damage = dmg_prop;
            o->atomic_damage = true;
        }
    }
    if (pr)
        drmModeFreePlaneResources(pr);
    if (o->atomic_damage) {
        rb_log("display: %s (%s): atomic page flips with FB_DAMAGE_CLIPS on plane %u", o->devnode, o->driver,
               o->plane_id);
    } else {
        drmSetClientCap(o->fd, DRM_CLIENT_CAP_ATOMIC, 0);
        rb_log("display: %s (%s): no primary plane with FB_DAMAGE_CLIPS; legacy page flips", o->devnode, o->driver);
    }
}

bool rb_driver_rpi_panel(const char *driver)
{
    return driver && (!strncmp(driver, "drm-rp1-dsi", 11) || !strncmp(driver, "drm-rp1-dpi", 11) ||
                      !strncmp(driver, "rp1-dsi", 7) || !strncmp(driver, "rp1-dpi", 7));
}

int rb_drm_card_score_for_primary(const struct rb_card_info *ci, bool external)
{
    int sum = 2 * ci->connected + ci->unknown; /* as before: two "unknown" connectors count like one connected */
    int score = sum >= 2 ? 100 : (sum ? 50 : 0);
    if (ci->boot_vga)
        score += 20;
    if (!is_firmware_driver(ci->driver))
        score += 10;
    /* Pi 5's connected DSI/DPI panel is a separate DRM card. Prefer it as
     * the internal primary; preserve HDMI preference for an external primary. */
    if (!external && ci->connected && rb_driver_rpi_panel(ci->driver))
        score += 20;
    if (is_vc4(ci->driver) && ci->hdmi_connected)
        score += 5;
    return score;
}

int rb_drm_card_score(const struct rb_card_info *ci)
{
    return rb_drm_card_score_for_primary(ci, false);
}

int rb_drm_rank_cards_for_primary(const struct rb_card_info *cards, int n, char out[][64], int max, bool external)
{
    int order[RB_MAX_CARDS], score[RB_MAX_CARDS];
    if (n > RB_MAX_CARDS)
        n = RB_MAX_CARDS;
    for (int i = 0; i < n; i++) {
        order[i] = i;
        score[i] = rb_drm_card_score_for_primary(&cards[i], external);
    }
    /* Stable sort by score, then node name. */
    for (int i = 1; i < n; i++) {
        int t = order[i];
        int j = i - 1;
        while (j >= 0 && (score[order[j]] < score[t] ||
                          (score[order[j]] == score[t] && strcmp(cards[order[j]].node, cards[t].node) > 0))) {
            order[j + 1] = order[j];
            j--;
        }
        order[j + 1] = t;
    }
    int count = 0;
    for (int i = 0; i < n && count < max; i++)
        if (score[order[i]] >= 50)
            copy_node(out[count++], cards[order[i]].node);
    return count;
}

int rb_drm_rank_cards(const struct rb_card_info *cards, int n, char out[][64], int max)
{
    return rb_drm_rank_cards_for_primary(cards, n, out, max, false);
}

bool rb_conn_better(const char *driver, int score_a, uint32_t type_a, uint32_t id_a, int score_b, uint32_t type_b,
                    uint32_t id_b)
{
    if (score_a != score_b)
        return score_a > score_b;
    return is_vc4(driver) && type_a == DRM_MODE_CONNECTOR_HDMIA && type_b == DRM_MODE_CONNECTOR_HDMIA &&
           id_a < id_b;
}

bool rb_driver_no_readout(const char *driver)
{
    /* vc4 and RP1 (Raspberry Pi); geminipda-drm (Gemini PDA: LK lit the panel, the
     * driver's CRTC still starts off) */
    return driver && (is_vc4(driver) || !strncmp(driver, "drm-rp1", 7) || !strncmp(driver, "rp1", 3) ||
                      !strcmp(driver, "geminipda-drm"));
}

bool rb_auto_mode_cap(const char *driver, int *cap_w, int *cap_h)
{
    const char *env = getenv("RB_AUTO_MODE_MAX");
    if (env && *env) {
        int w = 0, h = 0;
        if (sscanf(env, "%dx%d", &w, &h) == 2 && w > 0 && h > 0) {
            *cap_w = w;
            *cap_h = h;
            return true;
        }
        return false; /* "0": no cap */
    }
    if (!is_vc4(driver))
        return false;
    *cap_w = 1920;
    *cap_h = 1080;
    return true;
}

bool rb_mode_within_cap(int w, int h, int cap_w, int cap_h)
{
    return (w <= cap_w && h <= cap_h) || (w <= cap_h && h <= cap_w);
}

static int refresh_distance(const drmModeModeInfo *m)
{
    int r = rb_mode_refresh(m);
    return r == 60 ? -1 : abs(r - 60);
}

const drmModeModeInfo *rb_mode_pick_capped(const drmModeModeInfo *modes, int n, const drmModeModeInfo *preferred,
                                           int cap_w, int cap_h)
{
    if (preferred && rb_mode_within_cap(preferred->hdisplay, preferred->vdisplay, cap_w, cap_h))
        return preferred;
    const drmModeModeInfo *exact = NULL, *best = NULL;
    for (int i = 0; i < n; i++) {
        const drmModeModeInfo *m = &modes[i];
        if (m->flags & DRM_MODE_FLAG_INTERLACE)
            continue;
        if (!rb_mode_within_cap(m->hdisplay, m->vdisplay, cap_w, cap_h))
            continue;
        if (m->hdisplay == cap_w && m->vdisplay == cap_h &&
            (!exact || refresh_distance(m) < refresh_distance(exact)))
            exact = m;
        long area = (long)m->hdisplay * m->vdisplay;
        long barea = best ? (long)best->hdisplay * best->vdisplay : -1;
        if (!best || area > barea || (area == barea && refresh_distance(m) < refresh_distance(best)))
            best = m;
    }
    return exact ? exact : best;
}

int rb_drm_list_cards_for_primary(char out[][64], int max, bool external)
{
    struct udev *udev = udev_new();
    if (!udev)
        return 0;
    struct udev_enumerate *en = udev_enumerate_new(udev);
    udev_enumerate_add_match_subsystem(en, "drm");
    udev_enumerate_add_match_sysname(en, "card[0-9]*");
    udev_enumerate_scan_devices(en);
    struct rb_card_info cand[RB_MAX_CARDS];
    char nodes[RB_MAX_CARDS][64], drivers[RB_MAX_CARDS][64];
    int n = 0;
    struct udev_list_entry *e;
    udev_list_entry_foreach(e, udev_enumerate_get_list_entry(en))
    {
        const char *syspath = udev_list_entry_get_name(e);
        struct udev_device *dev = udev_device_new_from_syspath(udev, syspath);
        if (!dev)
            continue;
        const char *sysname = udev_device_get_sysname(dev);
        const char *node = udev_device_get_devnode(dev);
        /* Only card nodes, not connectors (cardN-NAME). */
        if (!node || strchr(sysname, '-') || n >= RB_MAX_CARDS) {
            udev_device_unref(dev);
            continue;
        }
        struct rb_card_info *ci = &cand[n];
        memset(ci, 0, sizeof *ci);
        struct udev_enumerate *ce = udev_enumerate_new(udev);
        char pattern[64];
        snprintf(pattern, sizeof pattern, "%s-*", sysname);
        udev_enumerate_add_match_subsystem(ce, "drm");
        udev_enumerate_add_match_sysname(ce, pattern);
        udev_enumerate_scan_devices(ce);
        struct udev_list_entry *ci_e;
        udev_list_entry_foreach(ci_e, udev_enumerate_get_list_entry(ce))
        {
            struct udev_device *cd = udev_device_new_from_syspath(udev, udev_list_entry_get_name(ci_e));
            if (!cd)
                continue;
            const char *status = udev_device_get_sysattr_value(cd, "status");
            if (status && !strncmp(status, "connected", 9)) {
                ci->connected++;
                const char *cname = udev_device_get_sysname(cd) + strlen(sysname) + 1;
                if (!strncmp(cname, "HDMI-A-", 7))
                    ci->hdmi_connected = true;
            } else if (status && !strncmp(status, "unknown", 7)) {
                ci->unknown++; /* not probed yet (driver just bound) */
            }
            udev_device_unref(cd);
        }
        udev_enumerate_unref(ce);
        struct udev_device *pci = udev_device_get_parent_with_subsystem_devtype(dev, "pci", NULL);
        if (pci) {
            const char *boot_vga = udev_device_get_sysattr_value(pci, "boot_vga");
            ci->boot_vga = boot_vga && boot_vga[0] == '1';
        }
        struct udev_device *parent = udev_device_get_parent(dev);
        const char *driver = parent ? udev_device_get_driver(parent) : NULL;
        copy_node(nodes[n], node);
        ci->node = nodes[n];
        if (driver) {
            copy_node(drivers[n], driver);
            ci->driver = drivers[n];
        }
        n++;
        udev_device_unref(dev);
    }
    udev_enumerate_unref(en);
    udev_unref(udev);
    return rb_drm_rank_cards_for_primary(cand, n, out, max, external);
}

int rb_drm_list_cards(char out[][64], int max)
{
    return rb_drm_list_cards_for_primary(out, max, false);
}

/* --- buffers --- */

static void fb_destroy(int fd, struct rb_fb *fb)
{
    if (fb->map && fb->map != MAP_FAILED)
        munmap(fb->map, fb->size);
    if (fb->fb_id)
        drmModeRmFB(fd, fb->fb_id);
    if (fb->handle) {
        struct drm_mode_destroy_dumb d = {.handle = fb->handle};
        drmIoctl(fd, DRM_IOCTL_MODE_DESTROY_DUMB, &d);
    }
    memset(fb, 0, sizeof *fb);
}

/* tall: allocate the buffer object twice as high and scan out its lower half
 * (AddFB2 offsets[0] = pitch * h). On QEMU std-VGA/bochs the first VBE
 * enable after a modeset zeroes VRAM [0, pitch * h) -- the start of the first
 * buffer object -- after we drew into it; the lower half is outside that
 * range, so the frame drawn before the commit survives it (no black
 * interval). */
static int fb_create(int fd, int w, int h, bool tall, struct rb_fb *fb)
{
    memset(fb, 0, sizeof *fb);
    struct drm_mode_create_dumb c = {.width = (uint32_t)w, .height = (uint32_t)(tall ? 2 * h : h), .bpp = 32};
    if (drmIoctl(fd, DRM_IOCTL_MODE_CREATE_DUMB, &c) < 0)
        return -errno;
    fb->handle = c.handle;
    fb->pitch = c.pitch;
    fb->size = c.size;
    fb->offset = tall ? c.pitch * (uint32_t)h : 0;
    uint32_t handles[4] = {c.handle}, pitches[4] = {c.pitch}, offsets[4] = {fb->offset};
    if (drmModeAddFB2(fd, (uint32_t)w, (uint32_t)h, DRM_FORMAT_XRGB8888, handles, pitches, offsets, &fb->fb_id, 0) <
        0) {
        if (tall) {
            /* legacy AddFB has no offsets */
            int err = -errno;
            fb_destroy(fd, fb);
            return err;
        }
        if (drmModeAddFB(fd, (uint32_t)w, (uint32_t)h, 24, 32, c.pitch, c.handle, &fb->fb_id) < 0) {
            int err = -errno;
            fb_destroy(fd, fb);
            return err;
        }
    }
    struct drm_mode_map_dumb m = {.handle = c.handle};
    if (drmIoctl(fd, DRM_IOCTL_MODE_MAP_DUMB, &m) < 0) {
        int err = -errno;
        fb_destroy(fd, fb);
        return err;
    }
    fb->map = mmap(NULL, fb->size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, (off_t)m.offset);
    if (fb->map == MAP_FAILED) {
        int err = -errno;
        fb->map = NULL;
        fb_destroy(fd, fb);
        return err;
    }
    return 0;
}

static int alloc_buffers(struct rb_output *o)
{
    o->nfb = 0;
    for (int i = 0; i < 2; i++) {
        /* The buffer committed by a real modeset is "tall" (see fb_create)
         * on bochs-drm only: that is the device whose enable clears VRAM,
         * and other drivers may ignore AddFB2 offsets at scanout (virtio-gpu
         * sets the scanout to the resource origin, which would show the
         * undrawn upper half). RB_DRM_NO_TALL=1 disables it (lab control
         * experiment). */
        const char *nt = getenv("RB_DRM_NO_TALL");
        bool tall = i == 0 && !o->mode_inherited && !strcmp(o->driver, "bochs-drm") && !(nt && nt[0] == '1');
        int rc = fb_create(o->fd, o->w, o->h, tall, &o->fb[i]);
        if (rc < 0 && tall)
            rc = fb_create(o->fd, o->w, o->h, false, &o->fb[i]);
        if (rc < 0) {
            if (i == 0)
                return rc;
            break; /* single buffer is acceptable */
        }
        o->nfb++;
    }
    o->front = -1;
    o->pending = -1;
    o->on_screen = false;
    o->need_modeset = true;
    o->flip_ok = o->nfb == 2;
    rb_output_damage_all(o);
    rb_output_kernel_damage_all(o);
    return 0;
}

/* --- connector / CRTC selection --- */

static void conn_name(int fd, drmModeConnector *c, char *out, size_t n)
{
    (void)fd;
    const char *type = drmModeGetConnectorTypeName(c->connector_type);
    snprintf(out, n, "%s-%u", type ? type : "Unknown", c->connector_type_id);
}

static uint32_t active_crtc_for(int fd, drmModeConnector *c, drmModeModeInfo *mode)
{
    if (!c->encoder_id)
        return 0;
    drmModeEncoder *enc = drmModeGetEncoder(fd, c->encoder_id);
    if (!enc)
        return 0;
    uint32_t crtc_id = enc->crtc_id;
    drmModeFreeEncoder(enc);
    if (!crtc_id)
        return 0;
    drmModeCrtc *crtc = drmModeGetCrtc(fd, crtc_id);
    if (!crtc)
        return 0;
    uint32_t result = 0;
    if (crtc->mode_valid && crtc->mode.hdisplay && crtc->mode.vdisplay) {
        if (mode)
            *mode = crtc->mode;
        result = crtc_id;
    }
    drmModeFreeCrtc(crtc);
    return result;
}

static uint32_t pick_crtc(int fd, drmModeRes *res, drmModeConnector *c)
{
    for (int i = 0; i < c->count_encoders; i++) {
        drmModeEncoder *enc = drmModeGetEncoder(fd, c->encoders[i]);
        if (!enc)
            continue;
        for (int j = 0; j < res->count_crtcs; j++) {
            if (!(enc->possible_crtcs & (1u << j)))
                continue;
            drmModeFreeEncoder(enc);
            return res->crtcs[j];
        }
        drmModeFreeEncoder(enc);
    }
    return 0;
}

/* "panel orientation" (enum name) and the DPMS property id of a connector. */
static void read_conn_props(int fd, uint32_t conn_id, char *orient, size_t olen, uint32_t *dpms)
{
    orient[0] = 0;
    *dpms = 0;
    drmModeObjectProperties *props = drmModeObjectGetProperties(fd, conn_id, DRM_MODE_OBJECT_CONNECTOR);
    if (!props)
        return;
    for (uint32_t i = 0; i < props->count_props; i++) {
        drmModePropertyRes *pr = drmModeGetProperty(fd, props->props[i]);
        if (!pr)
            continue;
        if (!strcmp(pr->name, "DPMS")) {
            *dpms = pr->prop_id;
        } else if (!strcmp(pr->name, "panel orientation") && (pr->flags & DRM_MODE_PROP_ENUM)) {
            for (int e = 0; e < pr->count_enums; e++)
                if (pr->enums[e].value == props->prop_values[i])
                    snprintf(orient, olen, "%s", pr->enums[e].name);
        }
        drmModeFreeProperty(pr);
    }
    drmModeFreeObjectProperties(props);
}

/* Clockwise content rotation for a connector: RB_PANEL_ROTATION (lab
 * override: 0/90/180/270) else the kernel's panel orientation. */
static int content_rotation(const struct rb_output *o)
{
    const char *env = getenv("RB_PANEL_ROTATION");
    if (env && *env) {
        int r = atoi(env);
        if (r == 0 || r == 90 || r == 180 || r == 270)
            return r;
    }
    int r = rb_rotation_from_orientation(o->orientation);
    if (r < 0 && o->conn_type == DRM_MODE_CONNECTOR_DPI && o->w == 480 && o->h == 800)
        r = rb_hyperpixel_panel_rotation(NULL);
    return r < 0 ? 0 : r;
}

int rb_user_rotation;

static void setup_connector_info(struct rb_output *o)
{
    read_conn_props(o->fd, o->conn_id, o->orientation, sizeof o->orientation, &o->dpms_prop);
    o->internal = rb_connector_internal(o->conn_type);
    o->panel_rot = o->internal ? content_rotation(o) : 0;
    o->rot = o->panel_rot;
}

static drmModeConnector *get_connector(int fd, uint32_t id)
{
    drmModeConnector *c = drmModeGetConnectorCurrent(fd, id);
    if (c && c->connection == DRM_MODE_CONNECTED && c->count_modes > 0)
        return c;
    if (c)
        drmModeFreeConnector(c);
    return drmModeGetConnector(fd, id);
}

static void store_modes(struct rb_output *o, drmModeConnector *c)
{
    o->nmodes = 0;
    for (int i = 0; i < c->count_modes && o->nmodes < RB_MAX_MODES; i++) {
        drmModeModeInfo *m = &c->modes[i];
        if (m->flags & DRM_MODE_FLAG_INTERLACE)
            continue;
        o->modes[o->nmodes++] = *m;
    }
}

const drmModeModeInfo *rb_output_find_mode(const struct rb_output *o, int w, int h, int refresh)
{
    const drmModeModeInfo *best = NULL;
    for (int i = 0; i < o->nmodes; i++) {
        const drmModeModeInfo *m = &o->modes[i];
        if (m->hdisplay != w || m->vdisplay != h)
            continue;
        if (refresh > 0 && rb_mode_refresh(m) != refresh)
            continue;
        if (!best || (m->type & DRM_MODE_TYPE_PREFERRED))
            best = m;
    }
    return best;
}

/* --- virtual output (lab) --- */

const char *rb_virtual_record;

static void virt_free(struct rb_output *o)
{
    for (int i = 0; i < 2; i++) {
        free(o->fb[i].map);
        memset(&o->fb[i], 0, sizeof o->fb[i]);
    }
    o->nfb = 0;
    if (o->virt_log) {
        rb_log("display: virtual: frame log closed (%llu frames, %llu bytes%s)",
               (unsigned long long)o->virt_log->frames, (unsigned long long)o->virt_log->bytes,
               o->virt_log->error ? ", write error" : "");
        rb_framelog_close(o->virt_log);
        free(o->virt_log);
        o->virt_log = NULL;
    }
}

static int open_virtual(struct rb_output *o, const char *spec, char *err, size_t errlen)
{
    int w = 0, h = 0, r = 60;
    char tail;
    int n = sscanf(spec, "%dx%d@%d%c", &w, &h, &r, &tail);
    if (n != 2 && n != 3) {
        snprintf(err, errlen, "virtual:%s: expected virtual:WxH or virtual:WxH@HZ", spec);
        return -1;
    }
    if (w < 64 || h < 64 || w > 8192 || h > 8192 || r < 1 || r > 240) {
        snprintf(err, errlen, "virtual:%s: size or refresh out of range", spec);
        return -1;
    }
    o->virt = true;
    o->fd = timerfd_create(CLOCK_MONOTONIC, TFD_NONBLOCK | TFD_CLOEXEC);
    if (o->fd < 0) {
        snprintf(err, errlen, "virtual: timerfd: %s", strerror(errno));
        return -1;
    }
    snprintf(o->driver, sizeof o->driver, "virtual");
    snprintf(o->conn_name, sizeof o->conn_name, "Virtual-1");
    o->conn_type = DRM_MODE_CONNECTOR_VIRTUAL;
    o->conn_id = 1;
    o->crtc_id = 1;
    drmModeModeInfo m;
    memset(&m, 0, sizeof m);
    m.hdisplay = m.hsync_start = m.hsync_end = m.htotal = (uint16_t)w;
    m.vdisplay = m.vsync_start = m.vsync_end = m.vtotal = (uint16_t)h;
    m.vrefresh = (uint32_t)r;
    m.clock = (uint32_t)((uint64_t)w * h * r / 1000);
    m.type = DRM_MODE_TYPE_PREFERRED | DRM_MODE_TYPE_DRIVER;
    snprintf(m.name, sizeof m.name, "%dx%d", w, h);
    o->modes[0] = m;
    o->nmodes = 1;
    o->mode = m;
    o->boot_mode = m;
    o->have_boot_mode = true;
    o->mode_inherited = true;
    o->mode_source = "configured";
    o->w = w;
    o->h = h;
    o->refresh = r;
    o->panel_rot = 0;
    o->rot = rb_rotation_add(0, rb_user_rotation);
    rb_output_set_frame(o, rb_output_lw(o), rb_output_lh(o), false);
    for (int i = 0; i < 2; i++) {
        o->fb[i].map = calloc((size_t)w * h, 4);
        if (!o->fb[i].map) {
            virt_free(o);
            close(o->fd);
            o->fd = -1;
            snprintf(err, errlen, "virtual: out of memory");
            return -1;
        }
        o->fb[i].pitch = (uint32_t)w * 4;
        o->fb[i].size = (uint64_t)w * h * 4;
    }
    o->nfb = 2;
    o->front = o->pending = -1;
    o->on_screen = false;
    o->need_modeset = true;
    o->flip_ok = true;
    o->virt_period_us = 1000000u / (uint64_t)r;
    o->virt_epoch_us = rb_mono_us();
    rb_output_damage_all(o);
    if (rb_virtual_record && *rb_virtual_record) {
        o->virt_log = calloc(1, sizeof *o->virt_log);
        if (!o->virt_log || rb_framelog_open(o->virt_log, rb_virtual_record, w, h) < 0) {
            rb_log("display: virtual: cannot record to %s: %s", rb_virtual_record, strerror(errno));
            free(o->virt_log);
            o->virt_log = NULL;
        }
    }
    rb_log("display: virtual output %dx%d@%d%s%s", w, h, r, o->virt_log ? ", frame log " : "",
           o->virt_log ? rb_virtual_record : "");
    return 0;
}

/* The frame in buffer idx shows from t_us on: append what changed. */
static void virt_record(struct rb_output *o, int idx, uint64_t t_us, const rb_region *g, bool full)
{
    if (!o->virt_log)
        return;
    rb_rect rects[RB_REGION_MAX];
    int n = 0;
    if (full) {
        rects[n++] = (rb_rect){0, 0, o->w, o->h};
    } else {
        for (int i = 0; i < g->n; i++) {
            rb_rect r = rb_xform_src_to_fb(&o->xf, g->r[i]);
            if (!rb_rect_empty(r))
                rects[n++] = r;
        }
    }
    rb_framelog_add(o->virt_log, t_us, (const uint32_t *)o->fb[idx].map, o->w, rects, n);
}

int rb_output_open(struct rb_output *o, const char *devnode, const char *want_conn, const int *want_mode, char *err,
                   size_t errlen)
{
    memset(o, 0, sizeof *o);
    o->fd = -1;
    o->front = o->pending = -1;
    snprintf(o->devnode, sizeof o->devnode, "%s", devnode);
    if (!strncmp(devnode, "virtual:", 8)) {
        if (open_virtual(o, devnode + 8, err, errlen) < 0) {
            if (o->fd >= 0)
                close(o->fd);
            o->fd = -1;
            return -1;
        }
        return 0;
    }
    o->fd = open(devnode, O_RDWR | O_CLOEXEC | O_NONBLOCK);
    if (o->fd < 0) {
        snprintf(err, errlen, "open %s: %s", devnode, strerror(errno));
        return -1;
    }
    struct stat st;
    if (fstat(o->fd, &st) == 0)
        o->rdev = st.st_rdev;
    drmVersion *ver = drmGetVersion(o->fd);
    if (ver) {
        snprintf(o->driver, sizeof o->driver, "%s", ver->name);
        drmFreeVersion(ver);
    }
    uint64_t cap = 0;
    if (drmGetCap(o->fd, DRM_CAP_DUMB_BUFFER, &cap) < 0 || !cap) {
        snprintf(err, errlen, "%s: no dumb buffer support", devnode);
        goto fail;
    }
    if (!drmIsMaster(o->fd) && drmSetMaster(o->fd) < 0) {
        snprintf(err, errlen, "%s: cannot become DRM master: %s", devnode, strerror(errno));
        goto fail;
    }
    drmModeRes *res = drmModeGetResources(o->fd);
    if (!res) {
        snprintf(err, errlen, "%s: not a KMS device", devnode);
        goto fail;
    }
    drmModeConnector *best = NULL;
    int best_score = -1;
    drmModeModeInfo best_active;
    uint32_t best_crtc = 0;
    for (int i = 0; i < res->count_connectors; i++) {
        drmModeConnector *c = get_connector(o->fd, res->connectors[i]);
        if (!c)
            continue;
        if (c->connection != DRM_MODE_CONNECTED || c->count_modes == 0) {
            drmModeFreeConnector(c);
            continue;
        }
        char name[32];
        conn_name(o->fd, c, name, sizeof name);
        drmModeModeInfo active;
        uint32_t crtc = active_crtc_for(o->fd, c, &active);
        int score = 1;
        if (crtc)
            score += 100;
        if (rb_connector_internal(c->connector_type))
            score += 10;
        if (want_conn && (!strcmp(want_conn, name) ||
                          (!strcmp(want_conn, "internal") && rb_connector_internal(c->connector_type)) ||
                          (!strcmp(want_conn, "external") && !rb_connector_internal(c->connector_type))))
            score += 1000;
        if (!best || rb_conn_better(o->driver, score, c->connector_type, c->connector_type_id, best_score,
                                    best->connector_type, best->connector_type_id)) {
            if (best)
                drmModeFreeConnector(best);
            best = c;
            best_score = score;
            best_crtc = crtc;
            if (crtc)
                best_active = active;
        } else {
            drmModeFreeConnector(c);
        }
    }
    if (!best) {
        drmModeFreeResources(res);
        snprintf(err, errlen, "%s: no connected connector", devnode);
        goto fail;
    }
    o->conn_id = best->connector_id;
    o->conn_type = best->connector_type;
    conn_name(o->fd, best, o->conn_name, sizeof o->conn_name);
    o->mm_w = (int)best->mmWidth;
    o->mm_h = (int)best->mmHeight;
    store_modes(o, best);
    const drmModeModeInfo *preferred = NULL;
    for (int i = 0; i < o->nmodes; i++)
        if (o->modes[i].type & DRM_MODE_TYPE_PREFERRED) {
            preferred = &o->modes[i];
            break;
        }
    if (!preferred && o->nmodes)
        preferred = &o->modes[0];
    o->fw_lit = best_crtc != 0;
    o->crtc_id = best_crtc ? best_crtc : pick_crtc(o->fd, res, best);
    drmModeFreeConnector(best);
    drmModeFreeResources(res);
    if (!o->crtc_id) {
        snprintf(err, errlen, "%s: no usable CRTC for %s", devnode, o->conn_name);
        goto fail;
    }
    const drmModeModeInfo *chosen = NULL;
    if (want_mode && want_mode[0] > 0)
        chosen = rb_output_find_mode(o, want_mode[0], want_mode[1], want_mode[2]);
    if (best_crtc) {
        o->boot_mode = best_active;
        o->have_boot_mode = true;
    } else {
        /* No CRTC state to inherit (bochs-drm and virtio-gpu do no hardware
         * readout): the firmware/GRUB mode from boot_params is what is on
         * screen, and keeping it means no visible mode change. */
        int fw, fh;
        const drmModeModeInfo *fm = NULL;
        if (rb_boot_fb_size(NULL, &fw, &fh) == 0) {
            fm = rb_output_find_mode(o, fw, fh, 0);
            if (preferred && fm && preferred->hdisplay == fw && preferred->vdisplay == fh)
                fm = preferred;
        }
        if (fm) {
            o->boot_mode = *fm;
            o->have_boot_mode = true;
        }
    }
    o->mode_source = "preferred";
    bool native_override = false;
    /* An internal panel only looks right at its native (preferred) mode; an
     * active smaller mode is a panel-fitter scale-up (fbcon's clone mode on
     * the GPD MicroPC set its 720x1280 DSI panel to 720x1080), so it is not
     * inherited. Firmware/GRUB run panels natively, so a normal boot still
     * inherits without a modeset. */
    if (!chosen && best_crtc && preferred && rb_connector_internal(o->conn_type) &&
        (best_active.hdisplay != preferred->hdisplay || best_active.vdisplay != preferred->vdisplay)) {
        rb_log("display: %s active mode %dx%d is not the panel's native %dx%d; using the native mode", o->conn_name,
               best_active.hdisplay, best_active.vdisplay, preferred->hdisplay, preferred->vdisplay);
        chosen = preferred;
        /* "auto" (host_mode null) means this mode from now on, not the
         * scaled firmware mode. */
        o->boot_mode = *preferred;
        native_override = true;
    }
    /* Raspberry Pi (vc4): a 4K display would be driven at 4K and every
     * frame scaled to it in software, competing with the emulator for the
     * CPU. Unless a host_mode is configured, an external display gets its
     * preferred mode only up to 1920x1080, else its 1080p mode. The splash
     * applies the same rule, so the presenter inherits its mode. */
    int cap_w = 0, cap_h = 0;
    bool capped = false;
    if (!(want_mode && want_mode[0] > 0) && !rb_connector_internal(o->conn_type) && rb_auto_mode_cap(o->driver, &cap_w, &cap_h)) {
        const drmModeModeInfo *cur = chosen         ? chosen
                                     : best_crtc    ? &best_active
                                     : o->have_boot_mode ? &o->boot_mode
                                                    : preferred;
        if (cur && !rb_mode_within_cap(cur->hdisplay, cur->vdisplay, cap_w, cap_h)) {
            const drmModeModeInfo *cm = rb_mode_pick_capped(o->modes, o->nmodes, preferred, cap_w, cap_h);
            if (cm) {
                rb_log("display: %s %dx%d is above the %dx%d this computer draws comfortably; using %dx%d@%d",
                       o->conn_name, cur->hdisplay, cur->vdisplay, cap_w, cap_h, cm->hdisplay, cm->vdisplay,
                       rb_mode_refresh(cm));
                chosen = cm;
                o->boot_mode = *cm; /* "auto" (host_mode null) means this mode */
                o->have_boot_mode = true;
                capped = true;
            }
        }
    }
    if (!chosen && best_crtc) {
        o->mode = best_active;
        o->mode_inherited = true;
        o->mode_source = "crtc";
    } else if (chosen) {
        o->mode = *chosen;
        o->mode_inherited = best_crtc && chosen->hdisplay == best_active.hdisplay &&
                            chosen->vdisplay == best_active.vdisplay && chosen->clock == best_active.clock;
        o->mode_source = native_override ? "native" : o->mode_inherited ? "crtc" : capped ? "capped" : "configured";
    } else if (o->have_boot_mode) {
        o->mode = o->boot_mode;
        o->mode_source = "firmware";
    } else if (preferred) {
        o->mode = *preferred;
    } else {
        snprintf(err, errlen, "%s: no mode", devnode);
        goto fail;
    }
    o->w = o->mode.hdisplay;
    o->h = o->mode.vdisplay;
    o->refresh = rb_mode_refresh(&o->mode);
    setup_connector_info(o);
    o->rot = rb_rotation_add(o->panel_rot, rb_user_rotation); /* the primary turns with the user rotation */
    rb_output_set_frame(o, rb_output_lw(o), rb_output_lh(o), false);
    if (rb_driver_damage_atomic(o->driver))
        setup_atomic_damage(o);
    int rc = alloc_buffers(o);
    if (rc < 0) {
        snprintf(err, errlen, "%s: buffer allocation failed: %s", devnode, strerror(-rc));
        goto fail;
    }
    return 0;
fail:
    if (o->fd >= 0)
        close(o->fd);
    o->fd = -1;
    return -1;
}

void rb_output_close(struct rb_output *o)
{
    if (o->fd < 0)
        return;
    if (o->virt) {
        virt_free(o);
        rb_xform_free(&o->xf);
        close(o->fd);
        o->fd = -1;
        return;
    }
    for (int i = 0; i < o->nfb; i++)
        fb_destroy(o->fd, &o->fb[i]);
    for (int i = 0; i < o->nretired; i++)
        fb_destroy(o->fd, &o->retired[i]);
    o->nfb = o->nretired = 0;
    rb_xform_free(&o->xf);
    if (!o->shared_fd)
        close(o->fd);
    o->fd = -1;
}

/* --- multiple heads on one card --- */

int rb_output_connected(const struct rb_output *card, uint32_t *ids, int max)
{
    if (card->fd < 0)
        return 0;
    if (card->virt) {
        if (max > 0)
            ids[0] = card->conn_id;
        return max > 0 ? 1 : 0;
    }
    drmModeRes *res = drmModeGetResources(card->fd);
    if (!res)
        return 0;
    int n = 0;
    for (int i = 0; i < res->count_connectors && n < max; i++) {
        drmModeConnector *c = get_connector(card->fd, res->connectors[i]);
        if (!c)
            continue;
        if (c->connection == DRM_MODE_CONNECTED && c->count_modes > 0)
            ids[n++] = c->connector_id;
        drmModeFreeConnector(c);
    }
    drmModeFreeResources(res);
    return n;
}

int rb_output_open_head(struct rb_output *o, const struct rb_output *card, uint32_t conn_id,
                        const uint32_t *busy_crtcs, int n_busy, char *err, size_t errlen)
{
    memset(o, 0, sizeof *o);
    o->fd = -1;
    o->front = o->pending = -1;
    drmModeRes *res = drmModeGetResources(card->fd);
    if (!res) {
        snprintf(err, errlen, "%s: no resources", card->devnode);
        return -1;
    }
    drmModeConnector *c = get_connector(card->fd, conn_id);
    if (!c || c->connection != DRM_MODE_CONNECTED || c->count_modes == 0) {
        if (c)
            drmModeFreeConnector(c);
        drmModeFreeResources(res);
        snprintf(err, errlen, "connector %u is not connected", conn_id);
        return -1;
    }
    o->fd = card->fd;
    o->shared_fd = true;
    o->rdev = card->rdev;
    snprintf(o->devnode, sizeof o->devnode, "%s", card->devnode);
    snprintf(o->driver, sizeof o->driver, "%s", card->driver);
    o->conn_id = c->connector_id;
    o->conn_type = c->connector_type;
    conn_name(o->fd, c, o->conn_name, sizeof o->conn_name);
    o->mm_w = (int)c->mmWidth;
    o->mm_h = (int)c->mmHeight;
    store_modes(o, c);
    drmModeModeInfo active;
    uint32_t crtc = active_crtc_for(o->fd, c, &active);
    o->fw_lit = crtc != 0;   /* before the clone check: a shared firmware CRTC still lit it */
    for (int i = 0; i < n_busy && crtc; i++)
        if (busy_crtcs[i] == crtc)
            crtc = 0; /* shared with another head (firmware clone): take our own */
    if (crtc) {
        o->crtc_id = crtc;
        o->mode = active;
        o->boot_mode = active;
        o->have_boot_mode = true;
        o->mode_inherited = true;
        o->mode_source = "crtc";
    } else {
        /* A CRTC that this encoder can drive and no other head uses. */
        for (int e = 0; e < c->count_encoders && !o->crtc_id; e++) {
            drmModeEncoder *enc = drmModeGetEncoder(o->fd, c->encoders[e]);
            if (!enc)
                continue;
            for (int j = 0; j < res->count_crtcs && !o->crtc_id; j++) {
                if (!(enc->possible_crtcs & (1u << j)))
                    continue;
                bool busy = false;
                for (int b = 0; b < n_busy; b++)
                    busy |= busy_crtcs[b] == res->crtcs[j];
                if (!busy)
                    o->crtc_id = res->crtcs[j];
            }
            drmModeFreeEncoder(enc);
        }
        const drmModeModeInfo *pref = NULL;
        for (int i = 0; i < o->nmodes; i++)
            if (o->modes[i].type & DRM_MODE_TYPE_PREFERRED) {
                pref = &o->modes[i];
                break;
            }
        if (!pref && o->nmodes)
            pref = &o->modes[0];
        o->mode_source = "preferred";
        int cap_w, cap_h;
        if (pref && !rb_connector_internal(o->conn_type) && rb_auto_mode_cap(o->driver, &cap_w, &cap_h) &&
            !rb_mode_within_cap(pref->hdisplay, pref->vdisplay, cap_w, cap_h)) {
            const drmModeModeInfo *cm = rb_mode_pick_capped(o->modes, o->nmodes, pref, cap_w, cap_h);
            if (cm) {
                pref = cm;
                o->mode_source = "capped";
            }
        }
        if (pref)
            o->mode = *pref;
    }
    drmModeFreeConnector(c);
    drmModeFreeResources(res);
    if (!o->crtc_id || !o->mode.hdisplay) {
        snprintf(err, errlen, "%s: no free CRTC or mode for %s", o->devnode, o->conn_name);
        o->fd = -1;
        return -1;
    }
    o->w = o->mode.hdisplay;
    o->h = o->mode.vdisplay;
    o->refresh = rb_mode_refresh(&o->mode);
    setup_connector_info(o);
    int rc = alloc_buffers(o);
    if (rc < 0) {
        snprintf(err, errlen, "%s: buffer allocation failed for %s: %s", o->devnode, o->conn_name, strerror(-rc));
        o->fd = -1;
        return -1;
    }
    return 0;
}

void rb_output_close_head(struct rb_output *o)
{
    if (o->fd < 0)
        return;
    if (o->virt) {
        rb_output_close(o);
        return;
    }
    /* Switch the CRTC off before its framebuffers go away (frees the CRTC
     * for another head; the connector is usually gone already). */
    if (o->crtc_id)
        drmModeSetCrtc(o->fd, o->crtc_id, 0, 0, 0, NULL, 0, NULL);
    rb_output_close(o);
}

void rb_output_abandon(struct rb_output *o)
{
    if (o->fd < 0)
        return;
    if (o->virt) {
        rb_output_close(o);
        return;
    }
    if (o->on_screen) {
        struct rb_fb *sets[2] = {o->fb, o->retired};
        int counts[2] = {o->nfb, o->nretired};
        for (int s = 0; s < 2; s++)
            for (int i = 0; i < counts[s]; i++) {
                if (!sets[s][i].fb_id)
                    continue;
                struct drm_mode_closefb arg = {.fb_id = sets[s][i].fb_id};
                ioctl(o->fd, DRM_IOCTL_MODE_CLOSEFB, &arg); /* unsupported: the id leaks until close */
                sets[s][i].fb_id = 0;
            }
    }
    rb_output_close(o);
}

/* A DSI pipe the driver enabled without the panel coming up does not scan
 * out, and every later commit on it waits out the driver's flip_done
 * timeout (10 s on i915). Seen on Gemini Lake (GPD MicroPC) when the
 * firmware left the panel off because an external display was attached:
 * the VBT power-on sequence needs GPIOs that the ACPI tables do not
 * describe. The vblank counter of such a pipe stands still. */
int rb_output_scanout_stalled(const struct rb_output *o)
{
    if (o->fd < 0 || !o->crtc_id || o->virt)
        return -ENODEV;
    uint64_t first = 0;
    for (int i = 0; i < 4; i++) {
        struct drm_crtc_get_sequence q = {.crtc_id = o->crtc_id};
        if (drmIoctl(o->fd, DRM_IOCTL_CRTC_GET_SEQUENCE, &q) < 0)
            return -errno; /* EINVAL: CRTC off or DPMS off */
        if (!q.active)
            return -ENOTCONN;
        if (i == 0)
            first = q.sequence;
        else if (q.sequence != first)
            return 0;
        if (i < 3)
            usleep(40000);
    }
    return 1;
}

bool rb_output_never_commit(uint32_t conn_type, bool fw_lit, int scanout)
{
    return conn_type == DRM_MODE_CONNECTOR_DSI && (!fw_lit || scanout == 1);
}

int rb_output_set_frame(struct rb_output *o, int frame_w, int frame_h, bool mirror)
{
    int border = o->xf.border, cs = o->xf.checker_s;
    struct rb_checker_anchor ca = o->xf.checker_anchor;
    int rc = rb_xform_setup(&o->xf, o->rot, o->w, o->h, frame_w, frame_h, mirror);
    rb_xform_set_border(&o->xf, border, cs, ca);
    rb_output_damage_all(o);
    return rc;
}

static const drmModeModeInfo *mode_for_frame_within(const struct rb_output *o, int frame_w, int frame_h, int cap_w,
                                                    int cap_h)
{
    const drmModeModeInfo *best = NULL;
    for (int i = 0; i < o->nmodes; i++) {
        const drmModeModeInfo *m = &o->modes[i];
        int lw, lh;
        rb_rotated_size(o->rot, m->hdisplay, m->vdisplay, &lw, &lh);
        if (lw < frame_w || lh < frame_h)
            continue;
        if (cap_w && !rb_mode_within_cap(m->hdisplay, m->vdisplay, cap_w, cap_h))
            continue;
        if (m->type & DRM_MODE_TYPE_PREFERRED)
            return m;
        if (!best || (long)m->hdisplay * m->vdisplay > (long)best->hdisplay * best->vdisplay)
            best = m;
    }
    return best;
}

const drmModeModeInfo *rb_output_mode_for_frame(const struct rb_output *o, int frame_w, int frame_h)
{
    int cap_w, cap_h;
    if (!rb_connector_internal(o->conn_type) && rb_auto_mode_cap(o->driver, &cap_w, &cap_h)) {
        /* Raspberry Pi: a sufficient mode within the cap first. */
        const drmModeModeInfo *m = mode_for_frame_within(o, frame_w, frame_h, cap_w, cap_h);
        if (m)
            return m;
    }
    return mode_for_frame_within(o, frame_w, frame_h, 0, 0);
}

/* A mode for the frame at 1x, else (frame drawn at pixel scale ss) for an exact
 * 1/d of it, smallest divisor d of ss first. */
static const drmModeModeInfo *mode_for_scaled_frame(const struct rb_output *o, int frame_w, int frame_h)
{
    const drmModeModeInfo *m = rb_output_mode_for_frame(o, frame_w, frame_h);
    int ss = o->xf.src_scale;
    for (int d = 2; !m && d <= ss; d++)
        if (ss % d == 0 && frame_w % d == 0 && frame_h % d == 0)
            m = rb_output_mode_for_frame(o, frame_w / d, frame_h / d);
    return m;
}

int rb_output_fit_mirror(struct rb_output *o, int frame_w, int frame_h)
{
    if (o->xf.k != 0)
        return 0;
    const drmModeModeInfo *m = mode_for_scaled_frame(o, frame_w, frame_h);
    if (!m && rb_output_reprobe(o) > 0)
        m = mode_for_scaled_frame(o, frame_w, frame_h);
    if (!m)
        return -ENOENT;
    if (m->hdisplay == o->w && m->vdisplay == o->h && m->clock == o->mode.clock)
        return 0;
    drmModeModeInfo mode = *m;
    int rc = rb_output_set_mode(o, &mode);
    if (rc < 0)
        return rc;
    rb_output_set_frame(o, frame_w, frame_h, true);
    return 1;
}

void rb_output_set_border(struct rb_output *o, int border, int checker_s, struct rb_checker_anchor anchor)
{
    if (o->xf.border == border && o->xf.checker_s == checker_s && o->xf.checker_anchor.x == anchor.x &&
        o->xf.checker_anchor.y == anchor.y)
        return;
    rb_xform_set_border(&o->xf, border, checker_s, anchor);
    if (!o->xf.identity)
        o->full[0] = o->full[1] = true;
}

int rb_output_set_power(struct rb_output *o, bool on)
{
    if (o->fd < 0)
        return -ENODEV;
    if (o->off == !on)
        return 0;
    int rc = 0;
    if (o->virt)
        rc = 0;
    else if (o->dpms_prop)
        rc = drmModeConnectorSetProperty(o->fd, o->conn_id, o->dpms_prop, on ? DRM_MODE_DPMS_ON : DRM_MODE_DPMS_OFF);
    else
        rc = -1;
    if (rc < 0)
        return -errno;
    o->off = !on;
    if (on) {
        /* Redraw everything on the next present (content changed while off). */
        o->pending = -1;
        rb_output_damage_all(o);
    }
    return 0;
}

int rb_output_use_native(struct rb_output *o)
{
    if (!rb_connector_internal(o->conn_type))
        return 0;
    const drmModeModeInfo *preferred = NULL;
    for (int i = 0; i < o->nmodes && !preferred; i++)
        if (o->modes[i].type & DRM_MODE_TYPE_PREFERRED)
            preferred = &o->modes[i];
    if (!preferred || (preferred->hdisplay == o->w && preferred->vdisplay == o->h))
        return 0;
    rb_log("display: %s mode %dx%d is not the panel's native %dx%d; using the native mode", o->conn_name, o->w, o->h,
           preferred->hdisplay, preferred->vdisplay);
    drmModeModeInfo m = *preferred;
    int rc = rb_output_set_mode(o, &m);
    if (rc < 0)
        return rc;
    o->boot_mode = m;     /* "auto" means the native mode from now on */
    o->have_boot_mode = true;
    o->mode_source = "native";
    return 1;
}

int rb_output_set_mode(struct rb_output *o, const drmModeModeInfo *mode)
{
    if (o->virt) /* one mode: the size it was opened with */
        return mode->hdisplay == o->w && mode->vdisplay == o->h ? 0 : -ENOTSUP;
    for (int i = 0; i < o->nretired; i++)
        fb_destroy(o->fd, &o->retired[i]);
    /* Keep the scanned-out buffers alive until the new mode is committed so
     * the CRTC is never disabled by an fb removal. */
    o->nretired = 0;
    for (int i = 0; i < o->nfb; i++)
        o->retired[o->nretired++] = o->fb[i];
    memset(o->fb, 0, sizeof o->fb);
    drmModeModeInfo saved = o->mode;
    int sw = o->w, sh = o->h, sr = o->refresh;
    o->mode = *mode;
    o->w = mode->hdisplay;
    o->h = mode->vdisplay;
    o->refresh = rb_mode_refresh(mode);
    o->mode_inherited = false;
    o->mode_source = "set";
    int rc = alloc_buffers(o);
    if (rc < 0) {
        o->mode = saved;
        o->w = sw;
        o->h = sh;
        o->refresh = sr;
        for (int i = 0; i < o->nretired; i++)
            o->fb[i] = o->retired[i];
        o->nfb = o->nretired;
        o->nretired = 0;
        o->front = 0;
        o->on_screen = true;
        o->need_modeset = false;
        return rc;
    }
    return 0;
}

int rb_output_reprobe(struct rb_output *o)
{
    if (o->virt)
        return 1;
    drmModeConnector *c = drmModeGetConnector(o->fd, o->conn_id);
    if (!c)
        return errno == ENODEV ? -ENODEV : 0;
    int connected = c->connection == DRM_MODE_CONNECTED && c->count_modes > 0;
    if (connected) {
        store_modes(o, c);
        o->mm_w = (int)c->mmWidth;
        o->mm_h = (int)c->mmHeight;
    }
    drmModeFreeConnector(c);
    return connected;
}

/* Damage is tracked in frame (shadow) coordinates; the transform maps it to
 * the scanout when the buffer is updated. */
void rb_output_damage(struct rb_output *o, rb_rect r)
{
    r = rb_rect_clip(r, o->xf.src_w, o->xf.src_h);
    if (rb_rect_empty(r))
        return;
    rb_region_add(&o->stale[0], r);
    rb_region_add(&o->stale[1], r);
}

void rb_output_damage_all(struct rb_output *o)
{
    rb_region_clear(&o->stale[0]);
    rb_region_clear(&o->stale[1]);
    o->full[0] = o->full[1] = true;
    rb_output_damage(o, (rb_rect){0, 0, o->xf.src_w, o->xf.src_h});
}

bool rb_output_needs_flush(const struct rb_output *o)
{
    if (o->fd < 0 || o->nfb == 0 || o->off)
        return false;
    if (!o->on_screen || o->need_modeset)
        return true;
    return o->full[o->front] || !rb_region_empty(&o->stale[o->front]);
}

static void copy_region(struct rb_output *o, int idx, const uint32_t *shadow, int stride_px)
{
    struct rb_fb *fb = &o->fb[idx];
    uint32_t *base = (uint32_t *)(fb->map + fb->offset);
    int fb_stride = (int)(fb->pitch / 4);
    if (o->full[idx]) {
        rb_xform_fill_border(&o->xf, base, fb_stride);
        rb_xform_blit(&o->xf, base, fb_stride, shadow, stride_px, (rb_rect){0, 0, o->xf.src_w, o->xf.src_h});
        return;
    }
    rb_region *g = &o->stale[idx];
    for (int i = 0; i < g->n; i++)
        rb_xform_blit(&o->xf, base, fb_stride, shadow, stride_px, g->r[i]);
}

static void dirty_fb(struct rb_output *o, int idx, const rb_region *g, bool full)
{
    drmModeClip clips[RB_REGION_MAX];
    int n = 0;
    if (full) {
        clips[n++] = (drmModeClip){0, 0, (uint16_t)o->w, (uint16_t)o->h};
    } else {
        for (int i = 0; i < g->n && n < RB_REGION_MAX; i++) {
            rb_rect r = rb_xform_src_to_fb(&o->xf, g->r[i]);
            if (rb_rect_empty(r))
                continue;
            clips[n++] = (drmModeClip){(uint16_t)r.x, (uint16_t)r.y, (uint16_t)(r.x + r.w), (uint16_t)(r.y + r.h)};
        }
    }
    if (n)
        drmModeDirtyFB(o->fd, o->fb[idx].fb_id, clips, (uint32_t)n);
}

static void clear_stale(struct rb_output *o, int idx)
{
    rb_region_clear(&o->stale[idx]);
    o->full[idx] = false;
}

void rb_output_stage(struct rb_output *o, int idx, const uint32_t *shadow, int stride_px)
{
    if (o->full[idx])
        o->kfull[idx] = true;
    else
        rb_region_add_region(&o->kdamage[idx], &o->stale[idx]);
    copy_region(o, idx, shadow, stride_px);
    clear_stale(o, idx);
}

/* Flip to buffer idx with an atomic commit carrying its kernel damage (drm.h).
 * 0 on success; -errno otherwise (-ENOTSUP-like errors: the caller falls back). */
static int atomic_flip(struct rb_output *o, int idx)
{
    struct drm_mode_rect clips[RB_REGION_MAX];
    bool full;
    int n = rb_damage_clips(&o->xf, &o->kdamage[idx], o->kfull[idx], clips, RB_REGION_MAX, &full);
    uint32_t blob = 0;
    if (!full && n > 0 && drmModeCreatePropertyBlob(o->fd, clips, sizeof clips[0] * (size_t)n, &blob) < 0) {
        blob = 0; /* no blob: the kernel copies everything */
        full = true;
    }
    drmModeAtomicReq *req = drmModeAtomicAlloc();
    if (!req) {
        if (blob)
            drmModeDestroyPropertyBlob(o->fd, blob);
        return -ENOMEM;
    }
    int rc = 0;
    if (drmModeAtomicAddProperty(req, o->plane_id, o->prop_fb_id, o->fb[idx].fb_id) < 0 ||
        drmModeAtomicAddProperty(req, o->plane_id, o->prop_damage, blob) < 0)
        rc = -ENOMEM;
    else if (drmModeAtomicCommit(o->fd, req, DRM_MODE_PAGE_FLIP_EVENT | DRM_MODE_ATOMIC_NONBLOCK, o) < 0)
        rc = -errno;
    drmModeAtomicFree(req);
    if (blob) /* the committed state holds its own reference */
        drmModeDestroyPropertyBlob(o->fd, blob);
    if (rc == 0) {
        o->damage_commits++;
        if (full) {
            o->damage_full++;
            o->damage_px += (uint64_t)o->w * (uint64_t)o->h;
        } else {
            for (int i = 0; i < n; i++)
                o->damage_px += (uint64_t)(clips[i].x2 - clips[i].x1) * (uint64_t)(clips[i].y2 - clips[i].y1);
        }
    }
    return rc;
}

static void page_flip_done(int fd, unsigned int seq, unsigned int sec, unsigned int usec, void *data)
{
    (void)fd;
    (void)seq;
    (void)sec;
    (void)usec;
    struct rb_output *o = data;
    if (getenv("RB_PRESENT_TRACE") || getenv("RB_PRESENT_TRACE_ROW"))
        fprintf(stderr, "RBPRESENT {\"stage\":\"flip\",\"us\":%llu,\"presenter_epoch_ms\":%llu,\"output_epoch_ms\":%llu,\"present\":%llu,\"crtc\":%u,\"guest_pid\":%u,\"kernel_seq\":%u,\"kernel_sec\":%u,\"kernel_usec\":%u}\n",
                (unsigned long long)rb_mono_us(), (unsigned long long)o->trace_presenter_epoch_ms,
                (unsigned long long)o->first_present_mono_ms, (unsigned long long)o->presents,
                o->crtc_id, o->trace_guest_pid, seq, sec, usec);
    if (o->pending >= 0) {
        o->front = o->pending;
        o->pending = -1;
    }
    o->pending_stalled = false;
}

bool rb_output_check_stall(struct rb_output *o, uint64_t now_mono_ms, uint64_t limit_ms)
{
    if (o->pending < 0 || o->virt || o->pending_stalled || now_mono_ms < o->pending_mono_ms + limit_ms)
        return false;
    o->pending_stalled = true;
    o->flip_stalls++;
    if (!o->stall_first_mono_ms)
        o->stall_first_mono_ms = now_mono_ms;
    o->stall_last_mono_ms = now_mono_ms;
    return true;
}

void rb_output_dispatch(struct rb_output *o)
{
    if (o->virt) { /* the refresh at which the pending frame shows */
        uint64_t expirations;
        if (read(o->fd, &expirations, sizeof expirations) == (ssize_t)sizeof expirations && o->pending >= 0) {
            o->front = o->pending;
            o->pending = -1;
        }
        return;
    }
    drmEventContext ev = {.version = 2, .page_flip_handler = page_flip_done};
    drmHandleEvent(o->fd, &ev);
}

static void retire_old(struct rb_output *o)
{
    for (int i = 0; i < o->nretired; i++)
        fb_destroy(o->fd, &o->retired[i]);
    o->nretired = 0;
}

/* Virtual output: the frame shows at the next refresh after the copy (the
 * timerfd completes the flip then); the first frame shows at once. */
static int virt_flush(struct rb_output *o, const uint32_t *shadow, int stride_px)
{
    uint64_t now = rb_mono_us();
    if (!o->on_screen || o->need_modeset) {
        if (o->pending >= 0)
            return 0;
        o->full[0] = true;
        copy_region(o, 0, shadow, stride_px);
        clear_stale(o, 0);
        o->front = 0;
        o->on_screen = true;
        o->need_modeset = false;
        virt_record(o, 0, now, NULL, true);
        o->presents++;
        if (!o->first_present_mono_ms)
            o->first_present_mono_ms = rb_mono_ms();
        return 1;
    }
    if (o->pending >= 0)
        return 0;
    if (rb_region_empty(&o->stale[o->front]) && !o->full[o->front])
        return 0;
    int back = 1 - o->front;
    rb_region done = o->stale[back];
    bool full = o->full[back];
    copy_region(o, back, shadow, stride_px);
    clear_stale(o, back);
    uint64_t due = o->virt_epoch_us + ((now - o->virt_epoch_us) / o->virt_period_us + 1) * o->virt_period_us;
    struct itimerspec its = {.it_value = {.tv_sec = (time_t)(due / 1000000u),
                                          .tv_nsec = (long)(due % 1000000u) * 1000}};
    if (timerfd_settime(o->fd, TFD_TIMER_ABSTIME, &its, NULL) < 0) {
        o->front = back; /* cannot wait for the refresh: show it now */
        due = now;
    } else {
        o->pending = back;
    }
    virt_record(o, back, due, &done, full);
    o->presents++;
    return 1;
}

int rb_output_flush(struct rb_output *o, const uint32_t *shadow, int stride_px)
{
    if (o->fd < 0 || o->nfb == 0 || o->off)
        return 0;
    if (o->virt)
        return virt_flush(o, shadow, stride_px);
    if (!o->on_screen || o->need_modeset) {
        if (o->pending >= 0)
            return 0;
        int idx = 0;
        o->full[idx] = true; /* the first commit of a buffer shows all of it */
        rb_output_stage(o, idx, shadow, stride_px);
        uint64_t t0 = rb_mono_ms();
        int setcrtc = drmModeSetCrtc(o->fd, o->crtc_id, o->fb[idx].fb_id, 0, 0, &o->conn_id, 1, &o->mode);
        uint64_t took = rb_mono_ms() - t0;
        if (took > 1000) { /* the driver waited for the display (a stalled flip before it) */
            o->slow_commits++;
            if (took > o->slow_commit_max_ms)
                o->slow_commit_max_ms = took;
            rb_log("display: %s: modeset took %llu ms", o->devnode, (unsigned long long)took);
        }
        if (setcrtc < 0) {
            int e = errno;
            o->last_errno = e;
            rb_output_damage_all(o);
            if (o->fb[idx].offset && e != EACCES && e != EPERM) {
                /* The tall buffer may not fit the device's video memory:
                 * retry once with an ordinary one. */
                rb_log("display: %s: modeset with a tall buffer failed (%s); retrying with a normal one",
                       o->devnode, strerror(e));
                fb_destroy(o->fd, &o->fb[idx]);
                if (fb_create(o->fd, o->w, o->h, false, &o->fb[idx]) < 0) {
                    o->nfb = 0;
                    return -e;
                }
                return rb_output_flush(o, shadow, stride_px);
            }
            return -e;
        }
        o->front = idx;
        o->on_screen = true;
        o->need_modeset = false;
        rb_output_committed(o, idx, true); /* a legacy commit has no clips: the whole buffer */
        static int no_repaint = -1; /* RB_DRM_NO_REPAINT=1: lab check of the tall buffer alone */
        if (no_repaint < 0) {
            const char *e = getenv("RB_DRM_NO_REPAINT");
            no_repaint = e && e[0] == '1';
        }
        if (!o->mode_inherited && !no_repaint) {
            /* A real modeset may clear video memory after our buffer was
             * written (QEMU std-VGA/bochs: the VBE enable write without
             * NOCLEARMEM memsets VRAM). The tall first buffer keeps the
             * scanned-out image outside that range; repaint anyway for
             * buffers that could not be allocated tall. */
            o->full[idx] = true;
            copy_region(o, idx, shadow, stride_px);
            clear_stale(o, idx);
            dirty_fb(o, idx, NULL, true);
        }
        o->presents++;
        if (!o->first_present_mono_ms)
            o->first_present_mono_ms = rb_mono_ms();
        retire_old(o);
        return 1;
    }
    if (o->pending >= 0)
        return 0;
    if (rb_region_empty(&o->stale[o->front]) && !o->full[o->front])
        return 0;
    if (o->flip_ok) {
        int back = 1 - o->front;
        rb_output_stage(o, back, shadow, stride_px);
        if (o->atomic_damage) {
            int rc = atomic_flip(o, back);
            if (rc == 0) {
                rb_output_committed(o, back, true);
                o->pending = back;
                o->pending_mono_ms = rb_mono_ms();
                o->pending_stalled = false;
                o->presents++;
                return 1;
            }
            o->last_errno = -rc;
            if (rc == -EBUSY || rc == -EACCES || rc == -EPERM || rc == -EINTR || rc == -ENOMEM)
                return rc; /* kernel damage kept: the retry carries it */
            rb_log("display: %s: atomic flip refused (%s); legacy page flips from now on", o->devnode,
                   strerror(-rc));
            o->atomic_damage = false;
            drmSetClientCap(o->fd, DRM_CLIENT_CAP_ATOMIC, 0);
        }
        if (drmModePageFlip(o->fd, o->crtc_id, o->fb[back].fb_id, DRM_MODE_PAGE_FLIP_EVENT, o) == 0) {
            rb_output_committed(o, back, true);
            o->pending = back;
            o->pending_mono_ms = rb_mono_ms();
            o->pending_stalled = false;
            o->presents++;
            return 1;
        }
        int e = errno;
        o->last_errno = e;
        if (e == EBUSY || e == EACCES || e == EPERM)
            return -e; /* fb[back] is current; stale[front] keeps the frame pending */
        rb_log("page flip unsupported on %s (%s); using single buffer with dirty-fb", o->devnode, strerror(e));
        o->flip_ok = false;
        /* fb[back] has fresh content but is not shown; fall through to
         * update the scanned-out buffer instead. */
    }
    int idx = o->front;
    rb_region done = o->stale[idx];
    bool full = o->full[idx];
    rb_output_stage(o, idx, shadow, stride_px);
    clear_stale(o, 1 - idx);
    dirty_fb(o, idx, &done, full);
    rb_output_committed(o, idx, true);
    o->presents++;
    return 1;
}

void rb_output_leave(struct rb_output *o)
{
    if (o->fd < 0)
        return;
    if (o->virt) {
        rb_output_close(o);
        return;
    }
    int front = o->pending >= 0 ? o->pending : o->front;
    if (o->on_screen && front >= 0 && o->fb[front].fb_id) {
        if (drmModeCloseFB(o->fd, o->fb[front].fb_id) == 0) {
            /* The framebuffer now belongs to the scanout, not to us; it keeps
             * the GEM object alive until someone replaces it. */
            o->fb[front].fb_id = 0;
            rb_log("display: left %s on screen for the next owner", o->devnode);
        } else {
            rb_log("display: CLOSEFB unsupported (%s); the CRTC will blank", strerror(errno));
        }
    }
    rb_output_close(o);
}

void rb_output_leave_signal_safe(const struct rb_output *o)
{
    if (o->fd < 0 || !o->on_screen || o->virt)
        return;
    int front = o->pending >= 0 ? o->pending : o->front;
    if (front < 0 || !o->fb[front].fb_id)
        return;
    struct drm_mode_closefb arg = {.fb_id = o->fb[front].fb_id};
    ioctl(o->fd, DRM_IOCTL_MODE_CLOSEFB, &arg);
}

int rb_output_drop_master(struct rb_output *o) { return o->virt ? 0 : drmDropMaster(o->fd); }

int rb_output_set_master(struct rb_output *o)
{
    if (o->virt || drmIsMaster(o->fd))
        return 0;
    int rc = drmSetMaster(o->fd);
    if (rc == 0) /* another master may have drawn meanwhile */
        rb_output_kernel_damage_all(o);
    return rc;
}

bool rb_output_is_master(const struct rb_output *o) { return o->fd >= 0 && (o->virt || drmIsMaster(o->fd)); }
