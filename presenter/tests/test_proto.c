/* Display-socket message parsing and control-socket JSON request parsing. */
#include <drm_fourcc.h>
#include <linux/input-event-codes.h>
#include <string.h>

#include "ctl.h"
#include "guest.h"
#include "proto.h"
#include "rbtest.h"

static size_t put32(uint8_t *b, size_t off, uint32_t v)
{
    memcpy(b + off, &v, 4);
    return off + 4;
}

static size_t put64(uint8_t *b, size_t off, uint64_t v)
{
    memcpy(b + off, &v, 8);
    return off + 8;
}

static void test_display_messages(void)
{
    uint8_t b[2048];
    struct rbd_msg m;
    char err[160];

    size_t n = put32(b, 0, RBD_HELLO);
    n = put32(b, n, RBD_HELLO_LEN);
    n = put32(b, n, 1);
    n = put32(b, n, 4242);
    memset(b + n, 0, 40);
    memcpy(b + n, "session-abc", 11);
    n += 40;
    CHECK_EQ(rbd_parse(b, n, &m, err, sizeof err), 0);
    CHECK_EQ(m.type, RBD_HELLO);
    CHECK_EQ(m.u.hello.version, 1);
    CHECK_EQ(m.u.hello.pid, 4242);
    CHECK(!strcmp(m.u.hello.session, "session-abc"));
    /* session without NUL padding is still terminated */
    memset(b + 16, 'x', 40);
    CHECK_EQ(rbd_parse(b, n, &m, err, sizeof err), 0);
    CHECK_EQ(strlen(m.u.hello.session), 40);
    CHECK(rbd_parse(b, n - 1, &m, err, sizeof err) < 0);

    n = put32(b, 0, RBD_SURFACE);
    n = put32(b, n, RBD_SURFACE_LEN);
    n = put32(b, n, 640);
    n = put32(b, n, 480);
    n = put32(b, n, 2560);
    n = put32(b, n, DRM_FORMAT_XRGB8888);
    n = put64(b, n, 2560ull * 480);
    CHECK_EQ(rbd_parse(b, n, &m, err, sizeof err), 0);
    CHECK_EQ(m.u.surface.width, 640);
    CHECK_EQ(m.u.surface.size, 2560ull * 480);
    CHECK_EQ(rbd_check_surface(&m, err, sizeof err), 0);
    m.u.surface.stride = 2000;
    CHECK(rbd_check_surface(&m, err, sizeof err) < 0);
    m.u.surface.stride = 2560;
    m.u.surface.size = 100;
    CHECK(rbd_check_surface(&m, err, sizeof err) < 0);
    m.u.surface.size = 2560ull * 480;
    m.u.surface.format = DRM_FORMAT_RGB565;
    CHECK(rbd_check_surface(&m, err, sizeof err) < 0);
    m.u.surface.format = DRM_FORMAT_XRGB8888;
    m.u.surface.width = 0;
    CHECK(rbd_check_surface(&m, err, sizeof err) < 0);
    m.u.surface.width = 100000;
    CHECK(rbd_check_surface(&m, err, sizeof err) < 0);

    n = put32(b, 0, RBD_DAMAGE);
    n = put32(b, n, 8 + 2 * 16);
    n = put32(b, n, 77);
    n = put32(b, n, 2);
    int32_t r1[4] = {10, 20, 30, 40}, r2[4] = {600, 470, 100, 100};
    memcpy(b + n, r1, 16);
    memcpy(b + n + 16, r2, 16);
    n += 32;
    CHECK_EQ(rbd_parse(b, n, &m, err, sizeof err), 0);
    CHECK_EQ(m.u.damage.seq, 77);
    CHECK_EQ(m.u.damage.count, 2);
    rb_rect r = rbd_damage_rect(&m, 0, 640, 480);
    CHECK_EQ(r.x, 10);
    CHECK_EQ(r.h, 40);
    r = rbd_damage_rect(&m, 1, 640, 480);
    CHECK_EQ(r.w, 40);
    CHECK_EQ(r.h, 10);
    /* count/length mismatch */
    put32(b, 12, 3);
    CHECK(rbd_parse(b, n, &m, err, sizeof err) < 0);
    /* hostile rect values are clipped/rejected without overflow */
    put32(b, 12, 1);
    put32(b, 4, 8 + 16);
    int32_t evil[4] = {2147483600, 5, 2147483600, 5};
    memcpy(b + 16, evil, 16);
    CHECK_EQ(rbd_parse(b, 8 + 8 + 16, &m, err, sizeof err), 0);
    CHECK(rb_rect_empty(rbd_damage_rect(&m, 0, 640, 480)));
    int32_t neg[4] = {-10, -10, 20, 20};
    memcpy(b + 16, neg, 16);
    r = rbd_damage_rect(&m, 0, 640, 480);
    CHECK_EQ(r.x, 0);
    CHECK_EQ(r.w, 10);
    int32_t negw[4] = {5, 5, -3, 4};
    memcpy(b + 16, negw, 16);
    CHECK(rb_rect_empty(rbd_damage_rect(&m, 0, 640, 480)));

    const char *ev = "{\"event\":\"boot-handoff\",\"clock\":\"real-time\"}";
    n = put32(b, 0, RBD_GUEST_EVENT);
    n = put32(b, n, (uint32_t)strlen(ev));
    memcpy(b + n, ev, strlen(ev));
    n += strlen(ev);
    CHECK_EQ(rbd_parse(b, n, &m, err, sizeof err), 0);
    CHECK_EQ(m.u.event.len, strlen(ev));

    n = put32(b, 0, 99);
    n = put32(b, n, 0);
    CHECK(rbd_parse(b, n, &m, err, sizeof err) < 0);
    CHECK(rbd_parse(b, 4, &m, err, sizeof err) < 0);

    uint8_t out[16];
    CHECK_EQ(rbd_encode(out, RBD_KEY, 36, 1, 2), 16);
    uint32_t v[4];
    memcpy(v, out, 16);
    CHECK_EQ(v[0], RBD_KEY);
    CHECK_EQ(v[1], 8);
    CHECK_EQ(v[2], 36);
    CHECK_EQ(v[3], 1);
    CHECK_EQ(rbd_encode(out, RBD_POINTER_REL, (uint32_t)-1200, 900, 2), 16);
    int32_t motion[2];
    memcpy(motion, out + 8, sizeof motion);
    CHECK_EQ(motion[0], -1200);
    CHECK_EQ(motion[1], 900);
    CHECK_EQ(rbd_encode(out, RBD_RELEASE_ALL, 0, 0, 0), 8);
    CHECK_EQ(rbd_encode(out, RBD_REFRESH_RATE, 16, 0, 1), 12);
}

static int lines_seen;
static char last_line[256];
static void on_line(void *ctx, char *line, size_t n)
{
    (void)ctx;
    lines_seen++;
    snprintf(last_line, sizeof last_line, "%.*s", (int)n, line);
}

static void test_lines(void)
{
    char buf[128];
    strcpy(buf, "{\"a\":1}\r\n{\"b\":2}\n{\"c\"");
    size_t used = rb_ctl_split_lines(buf, strlen(buf), on_line, NULL);
    CHECK_EQ(lines_seen, 2);
    CHECK(!strcmp(last_line, "{\"b\":2}"));
    CHECK_EQ(used, strlen("{\"a\":1}\r\n{\"b\":2}\n"));
    strcpy(buf, "no newline");
    CHECK_EQ(rb_ctl_split_lines(buf, strlen(buf), on_line, NULL), 0);
}

static cJSON *J(const char *s) { return cJSON_Parse(s); }

static void test_requests(void)
{
    char err[200];
    struct rb_scene_req s;
    cJSON *j = J("{\"cmd\":\"set_scene\",\"scene\":\"guest\",\"reveal\":\"first-content\"}");
    CHECK_EQ(rb_parse_set_scene(j, &s, err, sizeof err), 0);
    CHECK_EQ(s.scene, RB_SCENE_GUEST);
    CHECK_EQ(s.reveal, RB_REVEAL_FIRST_CONTENT);
    CHECK(!s.classic_guest);
    cJSON_Delete(j);
    j = J("{\"scene\":\"guest\",\"reveal\":\"now\",\"classic_guest\":true}");
    CHECK_EQ(rb_parse_set_scene(j, &s, err, sizeof err), 0);
    CHECK_EQ(s.reveal, RB_REVEAL_NOW);
    CHECK(s.classic_guest);
    cJSON_Delete(j);
    j = J("{\"scene\":\"guest\"}");
    CHECK_EQ(rb_parse_set_scene(j, &s, err, sizeof err), 0);
    CHECK_EQ(s.reveal, RB_REVEAL_FIRST_CONTENT); /* default keeps the checkerboard */
    cJSON_Delete(j);
    j = J("{\"scene\":\"panel\",\"background\":\"guest-dimmed\"}");
    CHECK_EQ(rb_parse_set_scene(j, &s, err, sizeof err), 0);
    CHECK_EQ(s.scene, RB_SCENE_PANEL);
    CHECK_EQ(s.background, RB_PANEL_BG_GUEST_DIMMED);
    cJSON_Delete(j);
    const char *bad_scenes[] = {"{\"scene\":\"desktop\"}", "{\"scene\":5}", "{}",
                                "{\"scene\":\"guest\",\"reveal\":\"later\"}",
                                "{\"scene\":\"panel\",\"background\":\"blue\"}"};
    for (size_t i = 0; i < sizeof bad_scenes / sizeof bad_scenes[0]; i++) {
        j = J(bad_scenes[i]);
        CHECK(rb_parse_set_scene(j, &s, err, sizeof err) < 0);
        cJSON_Delete(j);
    }

    struct rb_display_req d;
    j = J("{\"scaling\":\"fit\",\"guest_size\":[1024,768],\"host_mode\":[1920,1080,60]}");
    CHECK_EQ(rb_parse_display_config(j, &d, err, sizeof err), 0);
    CHECK(d.has_scaling && d.scaling == RB_SCALING_FIT);
    CHECK(d.has_guest_size && d.guest_w == 1024 && d.guest_h == 768);
    CHECK(d.has_host_mode && d.host_w == 1920 && d.host_refresh == 60);
    cJSON_Delete(j);
    j = J("{\"guest_size\":null,\"host_mode\":null}");
    CHECK_EQ(rb_parse_display_config(j, &d, err, sizeof err), 0);
    CHECK(d.has_guest_size && d.guest_w == 0);
    CHECK(d.has_host_mode && d.host_w == 0);
    CHECK(!d.has_scaling);
    cJSON_Delete(j);
    j = J("{\"enforce_guest_size\":true}");
    CHECK_EQ(rb_parse_display_config(j, &d, err, sizeof err), 0);
    CHECK(d.has_enforce && d.enforce && !d.has_guest_size && !d.has_scaling);
    cJSON_Delete(j);
    j = J("{\"scaling\":\"integer\"}");
    CHECK_EQ(rb_parse_display_config(j, &d, err, sizeof err), 0);
    CHECK(!d.has_enforce);
    cJSON_Delete(j);
    j = J("{\"enforce_guest_size\":1}");
    CHECK(rb_parse_display_config(j, &d, err, sizeof err) < 0);
    cJSON_Delete(j);
    /* pixel_scale: "auto" (or null) or 1..4 */
    j = J("{\"pixel_scale\":3,\"scaling\":\"integer\"}");
    CHECK_EQ(rb_parse_display_config(j, &d, err, sizeof err), 0);
    CHECK(d.has_pixel_scale && d.pixel_scale == 3 && d.has_scaling);
    cJSON_Delete(j);
    j = J("{\"pixel_scale\":\"auto\"}");
    CHECK_EQ(rb_parse_display_config(j, &d, err, sizeof err), 0);
    CHECK(d.has_pixel_scale && d.pixel_scale == 0);
    cJSON_Delete(j);
    j = J("{\"pixel_scale\":null}");
    CHECK_EQ(rb_parse_display_config(j, &d, err, sizeof err), 0);
    CHECK(d.has_pixel_scale && d.pixel_scale == 0);
    cJSON_Delete(j);
    j = J("{\"scaling\":\"fit\"}");
    CHECK_EQ(rb_parse_display_config(j, &d, err, sizeof err), 0);
    CHECK(!d.has_pixel_scale);
    cJSON_Delete(j);
    /* rotation: the user rotation of the primary (rotation.h) */
    j = J("{\"rotation\":270}");
    CHECK_EQ(rb_parse_display_config(j, &d, err, sizeof err), 0);
    CHECK(d.has_rotation && d.rotation == 270 && !d.has_scaling);
    cJSON_Delete(j);
    j = J("{\"scaling\":\"fit\"}");
    CHECK_EQ(rb_parse_display_config(j, &d, err, sizeof err), 0);
    CHECK(!d.has_rotation);
    cJSON_Delete(j);
    const char *bad_rotation[] = {"{\"rotation\":45}", "{\"rotation\":360}", "{\"rotation\":\"90\"}",
                                  "{\"rotation\":90.5}", "{\"rotation\":-90}", "{\"rotation\":null}"};
    for (size_t i = 0; i < sizeof bad_rotation / sizeof bad_rotation[0]; i++) {
        j = J(bad_rotation[i]);
        CHECK(rb_parse_display_config(j, &d, err, sizeof err) < 0);
        cJSON_Delete(j);
    }
    const char *bad_display[] = {"{\"scaling\":\"stretch\"}", "{\"guest_size\":[1024]}",
                                 "{\"guest_size\":[10,10]}", "{\"guest_size\":[1024.5,768]}",
                                 "{\"host_mode\":[1920,1080]}", "{\"guest_size\":\"auto\"}",
                                 "{\"pixel_scale\":5}", "{\"pixel_scale\":-1}", "{\"pixel_scale\":2.5}",
                                 "{\"pixel_scale\":\"2\"}", "{\"pixel_scale\":true}", "{\"crt_bezel\":1}"};
    for (size_t i = 0; i < sizeof bad_display / sizeof bad_display[0]; i++) {
        j = J(bad_display[i]);
        CHECK(rb_parse_display_config(j, &d, err, sizeof err) < 0);
        cJSON_Delete(j);
    }
    j = J("{\"crt_bezel\":true}");
    CHECK_EQ(rb_parse_display_config(j, &d, err, sizeof err), 0);
    CHECK(d.has_crt_bezel && d.crt_bezel);
    cJSON_Delete(j);

    struct rb_input_req in;
    j = J("{\"swap_command_option\":true,\"recovery_chord\":\"ctrl+alt+backspace\",\"pointer_speed\":1.5}");
    CHECK_EQ(rb_parse_input_config(j, &in, err, sizeof err), 0);
    CHECK(in.has_swap && in.swap);
    CHECK(in.has_chord && in.chord == KEY_BACKSPACE);
    CHECK(in.has_speed && in.speed == 1.5);
    CHECK(!in.has_touch_gestures);
    cJSON_Delete(j);
    CHECK(!in.has_legacy_mouse);
    CHECK(!in.has_mouse_dpi);
    j = J("{\"touch_gestures\":false,\"legacy_mouse\":true,\"mouse_dpi\":64000,\"gamepad_mode\":\"wasd\",\"natural_scroll\":true}");
    CHECK_EQ(rb_parse_input_config(j, &in, err, sizeof err), 0);
    CHECK(in.has_touch_gestures && !in.touch_gestures);
    CHECK(in.has_legacy_mouse && in.legacy_mouse);
    CHECK(in.has_mouse_dpi && in.mouse_dpi == 64000);
    CHECK(in.has_gamepad_mode && in.gamepad_mode == 1);
    CHECK(in.has_natural_scroll && in.natural_scroll);
    cJSON_Delete(j);
    const char *bad_input[] = {"{\"swap_command_option\":1}", "{\"recovery_chord\":\"ctrl+c\"}",
                               "{\"pointer_speed\":0.1}", "{\"pointer_speed\":\"fast\"}",
                               "{\"pointer_speed\":5}", "{\"touch_gestures\":1}",
                               "{\"legacy_mouse\":\"true\"}", "{\"mouse_dpi\":99}", "{\"mouse_dpi\":100001}",
                               "{\"mouse_dpi\":1200.5}", "{\"mouse_dpi\":\"64000\"}",
                               "{\"gamepad_mode\":\"analog\"}", "{\"natural_scroll\":1}"};
    for (size_t i = 0; i < sizeof bad_input / sizeof bad_input[0]; i++) {
        j = J(bad_input[i]);
        CHECK(rb_parse_input_config(j, &in, err, sizeof err) < 0);
        cJSON_Delete(j);
    }

    struct rb_set_config_req cfg;
    j = J("{\"cmd\":\"set_config\",\"gamepad_mode\":\"off\",\"natural_scroll\":true,\"crt_bezel\":true}");
    CHECK_EQ(rb_parse_set_config(j, &cfg, err, sizeof err), 0);
    CHECK(cfg.has_gamepad_mode && cfg.gamepad_mode == 2);
    CHECK(cfg.has_natural_scroll && cfg.natural_scroll);
    CHECK(cfg.has_crt_bezel && cfg.crt_bezel);
    cJSON_Delete(j);
    j = J("{\"cmd\":\"set_config\",\"gamepad_mode\":\"arrows\"}");
    CHECK_EQ(rb_parse_set_config(j, &cfg, err, sizeof err), 0);
    CHECK(cfg.has_gamepad_mode && cfg.gamepad_mode == 0 && !cfg.has_natural_scroll && !cfg.has_crt_bezel);
    cJSON_Delete(j);
    j = J("{\"gamepad_mode\":\"bad\"}");
    CHECK(rb_parse_set_config(j, &cfg, err, sizeof err) < 0);
    cJSON_Delete(j);
    j = J("{\"natural_scroll\":\"yes\"}");
    CHECK(rb_parse_set_config(j, &cfg, err, sizeof err) < 0);
    cJSON_Delete(j);
    j = J("{\"crt_bezel\":0}");
    CHECK(rb_parse_set_config(j, &cfg, err, sizeof err) < 0);
    cJSON_Delete(j);
    CHECK(!strcmp(rb_scene_name(RB_SCENE_BLACK), "black"));
    CHECK(!strcmp(rb_panel_bg_name(RB_PANEL_BG_GUEST_DIMMED), "guest-dimmed"));
}

/* "osd" (osd.h): kind, level 0..1, optional muted. */
static void test_osd_request(void)
{
    char err[200];
    struct rb_osd_req o;
    cJSON *j = J("{\"cmd\":\"osd\",\"kind\":\"brightness\",\"level\":0.5}");
    CHECK_EQ(rb_parse_osd(j, &o, err, sizeof err), 0);
    CHECK_EQ(o.kind, 2);
    CHECK(o.level == 0.5 && !o.muted);
    cJSON_Delete(j);
    j = J("{\"kind\":\"mute\",\"level\":1,\"muted\":true}");
    CHECK_EQ(rb_parse_osd(j, &o, err, sizeof err), 0);
    CHECK(o.kind == 1 && o.muted && o.level == 1.0);
    cJSON_Delete(j);
    const char *bad[] = {"{\"kind\":\"contrast\",\"level\":0.5}", "{\"level\":0.5}",
                         "{\"kind\":\"volume\"}", "{\"kind\":\"volume\",\"level\":1.5}",
                         "{\"kind\":\"volume\",\"level\":-0.1}", "{\"kind\":\"volume\",\"level\":\"half\"}",
                         "{\"kind\":\"volume\",\"level\":0.2,\"muted\":1}", "{\"kind\":3,\"level\":0.2}"};
    for (size_t i = 0; i < sizeof bad / sizeof bad[0]; i++) {
        j = J(bad[i]);
        CHECK(rb_parse_osd(j, &o, err, sizeof err) < 0);
        cJSON_Delete(j);
    }
}

int main(void)
{
    test_display_messages();
    test_lines();
    test_requests();
    test_osd_request();
    TEST_DONE();
}
