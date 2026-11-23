#include "proto.h"

#include <math.h>
#include <stdio.h>
#include <string.h>

#include "gemkbd.h"
#include "keymap.h"
#include "touch.h"

const char *rb_scene_name(enum rb_scene s)
{
    switch (s) {
    case RB_SCENE_SPLASH: return "splash";
    case RB_SCENE_GUEST: return "guest";
    case RB_SCENE_PANEL: return "panel";
    case RB_SCENE_BLACK: return "black";
    }
    return "unknown";
}

const char *rb_panel_bg_name(enum rb_panel_bg b)
{
    switch (b) {
    case RB_PANEL_BG_CHECKERBOARD: return "checkerboard";
    case RB_PANEL_BG_GUEST_DIMMED: return "guest-dimmed";
    case RB_PANEL_BG_BLACK: return "black";
    }
    return "unknown";
}

const char *rb_scaling_name(enum rb_scaling s) { return s == RB_SCALING_FIT ? "fit" : "integer"; }

int rb_parse_set_scene(const cJSON *req, struct rb_scene_req *out, char *err, size_t errlen)
{
    memset(out, 0, sizeof *out);
    const cJSON *scene = cJSON_GetObjectItemCaseSensitive(req, "scene");
    if (!cJSON_IsString(scene)) {
        snprintf(err, errlen, "scene must be a string");
        return -1;
    }
    const char *s = scene->valuestring;
    if (!strcmp(s, "splash"))
        out->scene = RB_SCENE_SPLASH;
    else if (!strcmp(s, "guest"))
        out->scene = RB_SCENE_GUEST;
    else if (!strcmp(s, "panel"))
        out->scene = RB_SCENE_PANEL;
    else if (!strcmp(s, "black"))
        out->scene = RB_SCENE_BLACK;
    else {
        snprintf(err, errlen, "unknown scene '%.40s'", s);
        return -1;
    }
    const cJSON *classic = cJSON_GetObjectItemCaseSensitive(req, "classic_guest");
    if (classic && !cJSON_IsBool(classic)) {
        snprintf(err, errlen, "classic_guest must be a boolean");
        return -1;
    }
    out->classic_guest = out->scene == RB_SCENE_GUEST && cJSON_IsTrue(classic);
    const cJSON *reveal = cJSON_GetObjectItemCaseSensitive(req, "reveal");
    out->reveal = RB_REVEAL_FIRST_CONTENT;
    if (reveal && !cJSON_IsNull(reveal)) {
        if (!cJSON_IsString(reveal)) {
            snprintf(err, errlen, "reveal must be a string");
            return -1;
        }
        if (!strcmp(reveal->valuestring, "first-content"))
            out->reveal = RB_REVEAL_FIRST_CONTENT;
        else if (!strcmp(reveal->valuestring, "now"))
            out->reveal = RB_REVEAL_NOW;
        else {
            snprintf(err, errlen, "unknown reveal '%.40s'", reveal->valuestring);
            return -1;
        }
    }
    const cJSON *bg = cJSON_GetObjectItemCaseSensitive(req, "background");
    out->background = RB_PANEL_BG_CHECKERBOARD;
    if (bg && !cJSON_IsNull(bg)) {
        if (!cJSON_IsString(bg)) {
            snprintf(err, errlen, "background must be a string");
            return -1;
        }
        if (!strcmp(bg->valuestring, "checkerboard"))
            out->background = RB_PANEL_BG_CHECKERBOARD;
        else if (!strcmp(bg->valuestring, "guest-dimmed"))
            out->background = RB_PANEL_BG_GUEST_DIMMED;
        else if (!strcmp(bg->valuestring, "black"))
            out->background = RB_PANEL_BG_BLACK;
        else {
            snprintf(err, errlen, "unknown background '%.40s'", bg->valuestring);
            return -1;
        }
    }
    return 0;
}

static int int_pair(const cJSON *a, int n, int *out)
{
    if (!cJSON_IsArray(a) || cJSON_GetArraySize(a) != n)
        return -1;
    for (int i = 0; i < n; i++) {
        const cJSON *v = cJSON_GetArrayItem(a, i);
        if (!cJSON_IsNumber(v) || v->valuedouble != floor(v->valuedouble) || v->valuedouble < 0 ||
            v->valuedouble > 100000)
            return -1;
        out[i] = (int)v->valuedouble;
    }
    return 0;
}

int rb_parse_display_config(const cJSON *req, struct rb_display_req *out, char *err, size_t errlen)
{
    memset(out, 0, sizeof *out);
    const cJSON *scaling = cJSON_GetObjectItemCaseSensitive(req, "scaling");
    if (scaling) {
        if (!cJSON_IsString(scaling)) {
            snprintf(err, errlen, "scaling must be a string");
            return -1;
        }
        if (!strcmp(scaling->valuestring, "integer"))
            out->scaling = RB_SCALING_INTEGER;
        else if (!strcmp(scaling->valuestring, "fit"))
            out->scaling = RB_SCALING_FIT;
        else {
            snprintf(err, errlen, "unknown scaling '%.40s'", scaling->valuestring);
            return -1;
        }
        out->has_scaling = true;
    }
    const cJSON *gs = cJSON_GetObjectItemCaseSensitive(req, "guest_size");
    if (gs) {
        out->has_guest_size = true;
        if (!cJSON_IsNull(gs)) {
            int v[2];
            if (int_pair(gs, 2, v) < 0 || v[0] < 320 || v[1] < 200 || v[0] > 4096 || v[1] > 4096) {
                snprintf(err, errlen, "guest_size must be [w,h] within 320x200..4096x4096 or null");
                return -1;
            }
            out->guest_w = v[0];
            out->guest_h = v[1];
        }
    }
    const cJSON *enf = cJSON_GetObjectItemCaseSensitive(req, "enforce_guest_size");
    if (enf) {
        if (!cJSON_IsBool(enf)) {
            snprintf(err, errlen, "enforce_guest_size must be a boolean");
            return -1;
        }
        out->has_enforce = true;
        out->enforce = cJSON_IsTrue(enf);
    }
    const cJSON *ps = cJSON_GetObjectItemCaseSensitive(req, "pixel_scale");
    if (ps) {
        if (cJSON_IsNull(ps) || (cJSON_IsString(ps) && !strcmp(ps->valuestring, "auto"))) {
            out->pixel_scale = 0;
        } else if (cJSON_IsNumber(ps) && ps->valuedouble == floor(ps->valuedouble) && ps->valuedouble >= 0 &&
                   ps->valuedouble <= RB_PIXEL_SCALE_MAX) {
            out->pixel_scale = (int)ps->valuedouble;
        } else {
            snprintf(err, errlen, "pixel_scale must be \"auto\" or 1..%d", RB_PIXEL_SCALE_MAX);
            return -1;
        }
        out->has_pixel_scale = true;
    }
    const cJSON *hm = cJSON_GetObjectItemCaseSensitive(req, "host_mode");
    if (hm) {
        out->has_host_mode = true;
        if (!cJSON_IsNull(hm)) {
            int v[3];
            if (int_pair(hm, 3, v) < 0 || v[0] < 320 || v[1] < 200) {
                snprintf(err, errlen, "host_mode must be [w,h,refresh] or null");
                return -1;
            }
            out->host_w = v[0];
            out->host_h = v[1];
            out->host_refresh = v[2];
        }
    }
    const cJSON *pr = cJSON_GetObjectItemCaseSensitive(req, "primary");
    if (pr) {
        if (!cJSON_IsString(pr) || (strcmp(pr->valuestring, "internal") && strcmp(pr->valuestring, "external"))) {
            snprintf(err, errlen, "primary must be \"internal\" or \"external\"");
            return -1;
        }
        out->has_primary = true;
        out->primary_external = !strcmp(pr->valuestring, "external");
    }
    const cJSON *rot = cJSON_GetObjectItemCaseSensitive(req, "rotation");
    if (rot) {
        int r = cJSON_IsNumber(rot) ? (int)rot->valuedouble : -1;
        if (!cJSON_IsNumber(rot) || rot->valuedouble != r || (r != 0 && r != 90 && r != 180 && r != 270)) {
            snprintf(err, errlen, "rotation must be 0, 90, 180 or 270");
            return -1;
        }
        out->has_rotation = true;
        out->rotation = r;
    }
    return 0;
}

int rb_parse_display_power(const cJSON *req, struct rb_power_req *out, char *err, size_t errlen)
{
    memset(out, 0, sizeof *out);
    const cJSON *o = cJSON_GetObjectItemCaseSensitive(req, "output");
    const cJSON *on = cJSON_GetObjectItemCaseSensitive(req, "on");
    if (!cJSON_IsBool(on)) {
        snprintf(err, errlen, "on must be a boolean");
        return -1;
    }
    out->on = cJSON_IsTrue(on);
    const char *name = "all";
    if (o) {
        if (!cJSON_IsString(o) || !o->valuestring[0] || strlen(o->valuestring) >= sizeof out->output) {
            snprintf(err, errlen, "output must be internal, external, all or a connector name");
            return -1;
        }
        name = o->valuestring;
    }
    snprintf(out->output, sizeof out->output, "%s", name);
    return 0;
}

int rb_parse_osd(const cJSON *req, struct rb_osd_req *out, char *err, size_t errlen)
{
    static const char *const kinds[] = {"volume", "mute", "brightness"};
    memset(out, 0, sizeof *out);
    const cJSON *k = cJSON_GetObjectItemCaseSensitive(req, "kind");
    const cJSON *lv = cJSON_GetObjectItemCaseSensitive(req, "level");
    const cJSON *m = cJSON_GetObjectItemCaseSensitive(req, "muted");
    out->kind = -1;
    for (int i = 0; cJSON_IsString(k) && i < 3; i++)
        if (!strcmp(k->valuestring, kinds[i]))
            out->kind = i;
    if (out->kind < 0) {
        snprintf(err, errlen, "kind must be volume, mute or brightness");
        return -1;
    }
    if (!cJSON_IsNumber(lv) || !(lv->valuedouble >= 0.0 && lv->valuedouble <= 1.0)) {
        snprintf(err, errlen, "level must be a number from 0 to 1");
        return -1;
    }
    out->level = lv->valuedouble;
    if (m && !cJSON_IsBool(m)) {
        snprintf(err, errlen, "muted must be a boolean");
        return -1;
    }
    out->muted = cJSON_IsTrue(m);
    return 0;
}

int rb_parse_input_config(const cJSON *req, struct rb_input_req *out, char *err, size_t errlen)
{
    memset(out, 0, sizeof *out);
    const cJSON *swap = cJSON_GetObjectItemCaseSensitive(req, "swap_command_option");
    if (swap) {
        if (!cJSON_IsBool(swap)) {
            snprintf(err, errlen, "swap_command_option must be a boolean");
            return -1;
        }
        out->has_swap = true;
        out->swap = cJSON_IsTrue(swap);
    }
    const cJSON *chord = cJSON_GetObjectItemCaseSensitive(req, "recovery_chord");
    if (chord) {
        int c = cJSON_IsString(chord) ? rb_chord_parse(chord->valuestring) : -1;
        if (c < 0) {
            snprintf(err, errlen, "recovery_chord must be ctrl+alt+delete, ctrl+alt+backspace or ctrl+alt+escape");
            return -1;
        }
        out->has_chord = true;
        out->chord = c;
    }
    const cJSON *speed = cJSON_GetObjectItemCaseSensitive(req, "pointer_speed");
    if (speed) {
        if (!cJSON_IsNumber(speed) || speed->valuedouble < 0.25 || speed->valuedouble > 4.0) {
            snprintf(err, errlen, "pointer_speed must be a number in 0.25..4.0");
            return -1;
        }
        out->has_speed = true;
        out->speed = speed->valuedouble;
    }
    const cJSON *legacy = cJSON_GetObjectItemCaseSensitive(req, "legacy_mouse");
    if (legacy) {
        if (!cJSON_IsBool(legacy)) {
            snprintf(err, errlen, "legacy_mouse must be a boolean");
            return -1;
        }
        out->has_legacy_mouse = true;
        out->legacy_mouse = cJSON_IsTrue(legacy);
    }
    const cJSON *tg = cJSON_GetObjectItemCaseSensitive(req, "touch_gestures");
    if (tg) {
        if (!cJSON_IsBool(tg)) {
            snprintf(err, errlen, "touch_gestures must be a boolean");
            return -1;
        }
        out->has_touch_gestures = true;
        out->touch_gestures = cJSON_IsTrue(tg);
    }
    const cJSON *kl = cJSON_GetObjectItemCaseSensitive(req, "keyboard_layout");
    if (kl) {
        int l = cJSON_IsString(kl) ? rb_gemkbd_layout_parse(kl->valuestring) : -1;
        if (l < 0) {
            snprintf(err, errlen, "keyboard_layout must be us or uk");
            return -1;
        }
        out->has_keyboard_layout = true;
        out->keyboard_layout = l;
    }
    const cJSON *tt = cJSON_GetObjectItemCaseSensitive(req, "touch_transform");
    if (tt) {
        int f = cJSON_IsString(tt) ? rb_touch_transform_parse(tt->valuestring) : -1;
        if (f < 0) {
            snprintf(err, errlen, "touch_transform must list swap-xy, invert-x, invert-y or none");
            return -1;
        }
        out->has_touch_transform = true;
        out->touch_transform = f;
    }
    return 0;
}
