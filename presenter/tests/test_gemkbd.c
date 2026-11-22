/* Gemini PDA input (gemkbd.h, touch transform): the built-in keyboard's keycaps
 * and Fn layer as positional U.S. keys, every key Mac OS needs reachable, every
 * output pressed and released exactly once whatever the release order, and the
 * touchscreen axes fix. Keys are the gemini-nixos DTS's linux,keymap codes. */
#include <linux/input-event-codes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "cJSON.h"
#include "gemkbd.h"
#include "keymap.h"
#include "proto.h"
#include "rbtest.h"
#include "touch.h"

/* The Gemini's matrix (mt6797-gemini-pda.dts linux,keymap): 52 keys. */
static const unsigned MATRIX[] = {
    KEY_1, KEY_2, KEY_3, KEY_4, KEY_5, KEY_6, KEY_7, KEY_8, KEY_9, KEY_0, KEY_Q, KEY_W, KEY_E, KEY_R, KEY_T,
    KEY_Y, KEY_U, KEY_I, KEY_O, KEY_P, KEY_A, KEY_S, KEY_D, KEY_F, KEY_G, KEY_H, KEY_J, KEY_K, KEY_L, KEY_Z,
    KEY_X, KEY_C, KEY_V, KEY_B, KEY_N, KEY_M, KEY_COMMA, KEY_DOT, KEY_APOSTROPHE, KEY_TAB, KEY_BACKSPACE,
    KEY_ENTER, KEY_SPACE, KEY_UP, KEY_DOWN, KEY_LEFT, KEY_RIGHT, KEY_LEFTSHIFT, KEY_RIGHTSHIFT, KEY_LEFTCTRL,
    KEY_LEFTALT, KEY_RIGHTALT,
};
#define NMATRIX (sizeof MATRIX / sizeof MATRIX[0])
#define FN KEY_RIGHTALT
#define TOUCH 1000 /* pseudo key: a touch press (button 0) */

/* Physical events (negative = release) -> what Mac OS gets, as QEMU key codes
 * after the presenter's Command/Option swap: "+shift +grave_accent ...". The
 * names are QEMU's QKeyCodes, which its ADB keyboard sends 1:1 (meta_l is the
 * ADB Command key, alt the Option key). */
static const char *play(struct rb_gemkbd *k, const int *seq, int n)
{
    static char buf[1024];
    buf[0] = 0;
    for (int i = 0; i < n; i++) {
        struct rb_gemkbd_ev ev[RB_GEMKBD_MAX_EV];
        int c = seq[i] < 0 ? -seq[i] : seq[i];
        int m = c == TOUCH ? rb_gemkbd_pointer(k, 0, seq[i] > 0, ev) : rb_gemkbd_key(k, (unsigned)c, seq[i] > 0, ev);
        for (int j = 0; j < m; j++) {
            int q = rb_evdev_to_qcode(ev[j].code);
            if (k->swap)
                q = rb_qcode_swap_command_option(q);
            char t[40];
            if (q)
                snprintf(t, sizeof t, "%s%c%s", buf[0] ? " " : "", ev[j].down ? '+' : '-', rb_qcode_name(q));
            else /* host keys without a QEMU key (brightness) */
                snprintf(t, sizeof t, "%s%chost%d", buf[0] ? " " : "", ev[j].down ? '+' : '-', ev[j].code);
            strncat(buf, t, sizeof buf - strlen(buf) - 1);
        }
    }
    return buf;
}

struct combo {
    int layout;
    bool swap;
    int keys[12]; /* physical events, 0-terminated */
    const char *want;
};

/* The coordinator's mapping (US keycaps unless noted), with every key released. */
static const struct combo COMBOS[] = {
    /* modifiers: Alt is Command, Ctrl Control, Fn nothing by itself */
    {0, 0, {KEY_LEFTALT, KEY_Q, -KEY_Q, -KEY_LEFTALT}, "+meta_l +q -q -meta_l"},
    {0, 0, {KEY_LEFTCTRL, KEY_A, -KEY_A, -KEY_LEFTCTRL}, "+ctrl +a -a -ctrl"},
    {0, 0, {FN, -FN}, ""},
    /* Fn legends (gemini symbols, variant us) */
    {0, 0, {FN, KEY_K, -KEY_K, -FN}, "+semicolon -semicolon"},
    {0, 0, {FN, KEY_L, -KEY_L, -FN}, "+shift +apostrophe -apostrophe -shift"},
    {0, 0, {FN, KEY_M, -KEY_M, -FN}, "+apostrophe -apostrophe"},
    {0, 0, {FN, KEY_I, -KEY_I, -FN}, "+shift +equal -equal -shift"},
    {0, 0, {FN, KEY_O, -KEY_O, -FN}, "+minus -minus"},
    {0, 0, {FN, KEY_P, -KEY_P, -FN}, "+equal -equal"},
    {0, 0, {FN, KEY_7, -KEY_7, -FN}, "+bracket_left -bracket_left"},
    {0, 0, {FN, KEY_8, -KEY_8, -FN}, "+bracket_right -bracket_right"},
    {0, 0, {FN, KEY_9, -KEY_9, -FN}, "+shift +bracket_left -bracket_left -shift"},
    {0, 0, {FN, KEY_0, -KEY_0, -FN}, "+shift +bracket_right -bracket_right -shift"},
    {0, 0, {FN, KEY_J, -KEY_J, -FN}, "+shift +minus -minus -shift"},
    {0, 0, {FN, KEY_1, -KEY_1, -FN}, "+shift +grave_accent -grave_accent -shift"},
    {0, 0, {FN, KEY_2, -KEY_2, -FN}, "+grave_accent -grave_accent"},
    {0, 0, {FN, KEY_3, -KEY_3, -FN}, "+alt +3 -3 -alt"},                              /* £ */
    {0, 0, {FN, KEY_4, -KEY_4, -FN}, "+shift +alt +2 -2 -alt -shift"},                /* € */
    {0, 0, {FN, KEY_5, -KEY_5, -FN}, "+shift +comma -comma -shift"},
    {0, 0, {FN, KEY_6, -KEY_6, -FN}, "+shift +dot -dot -shift"},
    {0, 0, {FN, KEY_APOSTROPHE, -KEY_APOSTROPHE, -FN}, "+shift +semicolon -semicolon -shift"},
    {0, 0, {FN, KEY_S, -KEY_S, -FN}, "+alt +s -s -alt"},                              /* ß */
    {0, 0, {FN, KEY_H, -KEY_H, -FN}, "+alt +slash -slash -alt"},                      /* ÷ */
    {0, 0, {FN, KEY_Z, -KEY_Z, -FN}, "+alt +backslash -backslash -alt"},              /* « */
    {0, 0, {FN, KEY_X, -KEY_X, -FN}, "+shift +alt +backslash -backslash -alt -shift"}, /* » */
    /* legends Mac OS has no key for: nothing, and no stray Option */
    {0, 0, {FN, KEY_Q, -KEY_Q, KEY_Y, -KEY_Y, KEY_COMMA, -KEY_COMMA, KEY_DOT, -KEY_DOT, -FN}, ""},
    /* Fn+navigation and editing */
    {0, 0, {FN, KEY_UP, -KEY_UP, KEY_LEFT, -KEY_LEFT, KEY_DOWN, -KEY_DOWN, KEY_RIGHT, -KEY_RIGHT, -FN},
     "+pgup -pgup +home -home +pgdn -pgdn +end -end"},
    {0, 0, {FN, KEY_BACKSPACE, -KEY_BACKSPACE, -FN}, "+delete -delete"},
    {0, 0, {FN, KEY_SPACE, -KEY_SPACE, -FN}, "+spc -spc"},
    {0, 0, {FN, KEY_TAB, -KEY_TAB, -FN}, "+caps_lock -caps_lock"},
    {0, 0, {FN, KEY_ENTER, -KEY_ENTER, -FN}, "+ret -ret"},
    /* host keys: volume, mute, brightness */
    {0, 0, {FN, KEY_C, -KEY_C, KEY_V, -KEY_V, KEY_T, -KEY_T, KEY_B, -KEY_B, -FN},
     "+volumedown -volumedown +volumeup -volumeup +audiomute -audiomute +host224 -host224"},
    /* Shift+Fn+digits: F1..F10, Shift released around them */
    {0, 0, {KEY_LEFTSHIFT, FN, KEY_1, -KEY_1, KEY_0, -KEY_0, -FN, -KEY_LEFTSHIFT},
     "+shift -shift +f1 -f1 +shift -shift +f10 -f10 +shift -shift"},
    /* Shift+Fn level 4 with Mac equivalents: ± ° */
    {0, 0, {KEY_LEFTSHIFT, FN, KEY_I, -KEY_I, -FN, -KEY_LEFTSHIFT}, "+shift +alt +equal -equal -alt -shift"},
    /* keycaps without Fn: Shift+, / and Shift+. ? ; the ' key types \ | (US) */
    {0, 0, {KEY_LEFTSHIFT, KEY_COMMA, -KEY_COMMA, -KEY_LEFTSHIFT}, "+shift -shift +slash -slash +shift -shift"},
    {0, 0, {KEY_LEFTSHIFT, KEY_DOT, -KEY_DOT, -KEY_LEFTSHIFT}, "+shift +slash -slash -shift"},
    {0, 0, {KEY_APOSTROPHE, -KEY_APOSTROPHE}, "+backslash -backslash"},
    /* Shift+Backspace stays Shift+Delete (the Mac's delete key) */
    {0, 0, {KEY_LEFTSHIFT, KEY_BACKSPACE, -KEY_BACKSPACE, -KEY_LEFTSHIFT}, "+shift +backspace -backspace -shift"},
    /* Fn with Alt or Ctrl is Option: Command-Option-Escape = Alt+Fn+Esc (Esc from mt6351-keys) */
    {0, 0, {KEY_LEFTALT, FN, -FN, -KEY_LEFTALT}, "+meta_l +alt -alt -meta_l"},
    {0, 0, {FN, KEY_LEFTALT, KEY_D, -KEY_D, -KEY_LEFTALT, -FN}, "+meta_l +alt +d -d -meta_l -alt"},
    {0, 0, {KEY_LEFTCTRL, FN, KEY_K, -KEY_K, -FN, -KEY_LEFTCTRL}, "+ctrl +alt +k -k -alt -ctrl"},
    /* Fn during a touch: Option-click; a symbol never leaves a bare Option */
    {0, 0, {FN, TOUCH, -TOUCH, -FN}, "+alt -alt"},
    {0, 0, {FN, KEY_K, -KEY_K, -FN, TOUCH, -TOUCH}, "+semicolon -semicolon"},
    {0, 0, {TOUCH, FN, -FN, -TOUCH}, ""},
    /* swap on: Alt is Option, Fn+Alt Command; a symbol's Option stays Option */
    {0, 1, {KEY_LEFTALT, KEY_Q, -KEY_Q, -KEY_LEFTALT}, "+alt +q -q -alt"},
    {0, 1, {KEY_LEFTALT, FN, -FN, -KEY_LEFTALT}, "+alt +meta_l -meta_l -alt"},
    {0, 1, {FN, KEY_3, -KEY_3, -FN}, "+alt +3 -3 -alt"},
    /* UK keycaps */
    {1, 0, {FN, KEY_K, -KEY_K, -FN}, "+shift +2 -2 -shift"},                          /* @ */
    {1, 0, {FN, KEY_L, -KEY_L, -FN}, "+semicolon -semicolon"},
    {1, 0, {FN, KEY_1, -KEY_1, -FN}, "+shift +backslash -backslash -shift"},          /* | */
    {1, 0, {FN, KEY_3, -KEY_3, -FN}, "+backslash -backslash"},
    {1, 0, {KEY_LEFTSHIFT, KEY_2, -KEY_2, -KEY_LEFTSHIFT}, "+shift +apostrophe -apostrophe -shift"}, /* " */
    {1, 0, {KEY_LEFTSHIFT, KEY_3, -KEY_3, -KEY_LEFTSHIFT}, "+shift -shift +alt +3 -3 -alt +shift -shift"}, /* £ */
    {1, 0, {KEY_APOSTROPHE, -KEY_APOSTROPHE}, "+apostrophe -apostrophe"},
    {1, 0, {KEY_LEFTSHIFT, KEY_APOSTROPHE, -KEY_APOSTROPHE, -KEY_LEFTSHIFT}, "+shift +grave_accent -grave_accent -shift"},
};

static void test_combos(void)
{
    for (size_t i = 0; i < sizeof COMBOS / sizeof COMBOS[0]; i++) {
        const struct combo *c = &COMBOS[i];
        struct rb_gemkbd k;
        rb_gemkbd_init(&k, c->layout, c->swap);
        int n = 0;
        while (n < 12 && c->keys[n])
            n++;
        const char *got = play(&k, c->keys, n);
        rbt_checks++;
        if (strcmp(got, c->want)) {
            rbt_failures++;
            fprintf(stderr, "combo %zu: got \"%s\", want \"%s\"\n", i, got, c->want);
        }
    }
}

/* A key released after Fn keeps what it pressed; swap changed mid-press too. */
static void test_release_order(void)
{
    struct rb_gemkbd k;
    rb_gemkbd_init(&k, RB_GEMKBD_US, false);
    CHECK(!strcmp(play(&k, (const int[]){FN, KEY_L, -FN}, 3), "+shift +apostrophe"));
    CHECK(!strcmp(play(&k, (const int[]){-KEY_L}, 1), "-apostrophe -shift"));
    CHECK(!strcmp(play(&k, (const int[]){FN, KEY_3}, 2), "+alt +3"));
    rb_gemkbd_configure(&k, RB_GEMKBD_US, true);
    CHECK(!strcmp(play(&k, (const int[]){-KEY_3, -FN}, 2), "-3 -meta_l")); /* swapped names, same keys released */
    /* Alt released before Fn: the Option goes with it */
    rb_gemkbd_init(&k, RB_GEMKBD_US, false);
    CHECK(!strcmp(play(&k, (const int[]){KEY_LEFTALT, FN, -KEY_LEFTALT, KEY_K, -KEY_K, -FN}, 6),
                  "+meta_l +alt -meta_l -alt +semicolon -semicolon"));
}

/* 30 000 random press/release orders over every key, Fn and touches: every output
 * key pressed and released exactly once, nothing held at the end, both layouts
 * and swaps. */
static void test_balanced(void)
{
    for (int variant = 0; variant < 4; variant++) {
        struct rb_gemkbd k;
        rb_gemkbd_init(&k, variant & 1, variant >> 1);
        int depth[RB_GEMKBD_CODES] = {0}, bad = 0;
        bool phys[RB_GEMKBD_CODES] = {false}, touch = false;
        rbt_seed = 777u + (unsigned)variant;
        for (int it = 0; it < 30000; it++) {
            struct rb_gemkbd_ev ev[RB_GEMKBD_MAX_EV];
            int n;
            if (rbt_rand() % 10 == 0) {
                touch = !touch;
                n = rb_gemkbd_pointer(&k, 0, touch, ev);
            } else {
                unsigned c = MATRIX[rbt_rand() % NMATRIX];
                bool down = !phys[c];
                if (rbt_rand() % 3 == 0 && !down)
                    continue;
                phys[c] = down;
                n = rb_gemkbd_key(&k, c, down, ev);
            }
            for (int j = 0; j < n; j++) {
                depth[ev[j].code] += ev[j].down ? 1 : -1;
                if (depth[ev[j].code] < 0 || depth[ev[j].code] > 1)
                    bad++;
            }
        }
        struct rb_gemkbd_ev ev[RB_GEMKBD_MAX_EV];
        int n = touch ? rb_gemkbd_pointer(&k, 0, false, ev) : 0;
        for (int j = 0; j < n; j++)
            depth[ev[j].code] += ev[j].down ? 1 : -1;
        for (unsigned c = 0; c < RB_GEMKBD_CODES; c++) /* release everything */
            if (phys[c]) {
                n = rb_gemkbd_key(&k, c, false, ev);
                for (int j = 0; j < n; j++)
                    depth[ev[j].code] += ev[j].down ? 1 : -1;
            }
        int left = 0;
        for (int c = 0; c < RB_GEMKBD_CODES; c++)
            left += depth[c] != 0;
        CHECK_EQ(bad, 0);
        CHECK_EQ(left, 0);
        CHECK_EQ(k.lift, 0);
        CHECK(!k.fnopt);
    }
}

/* Every key a Mac keyboard needs is reachable (Escape: the Esc/On key; Option:
 * Fn with Alt or Ctrl), and everything produced has a QEMU key code or is a
 * host key. */
static void test_reachable(void)
{
    static const unsigned need[] = {
        KEY_GRAVE, KEY_MINUS, KEY_EQUAL, KEY_LEFTBRACE, KEY_RIGHTBRACE, KEY_BACKSLASH, KEY_SEMICOLON,
        KEY_APOSTROPHE, KEY_COMMA, KEY_DOT, KEY_SLASH, KEY_SPACE, KEY_ENTER, KEY_BACKSPACE, KEY_DELETE,
        KEY_TAB, KEY_CAPSLOCK, KEY_UP, KEY_DOWN, KEY_LEFT, KEY_RIGHT, KEY_HOME, KEY_END, KEY_PAGEUP, KEY_PAGEDOWN,
        KEY_F1, KEY_F2, KEY_F3, KEY_F4, KEY_F5, KEY_F6, KEY_F7, KEY_F8, KEY_F9, KEY_F10, KEY_LEFTSHIFT,
        KEY_LEFTCTRL, KEY_LEFTALT, KEY_LEFTMETA, KEY_A, KEY_Z, KEY_0, KEY_9,
    };
    for (int layout = 0; layout < 2; layout++) {
        bool got[RB_GEMKBD_CODES] = {false};
        for (int level = 0; level < 8; level++) /* bit 0 Shift, 1 Fn, 2 Alt held */
            for (size_t i = 0; i < NMATRIX; i++) {
                struct rb_gemkbd k;
                rb_gemkbd_init(&k, layout, false);
                struct rb_gemkbd_ev ev[RB_GEMKBD_MAX_EV];
                int n;
                if (level & 4) {
                    n = rb_gemkbd_key(&k, KEY_LEFTALT, true, ev);
                    for (int j = 0; j < n; j++)
                        got[ev[j].code] = true;
                }
                if (level & 1)
                    rb_gemkbd_key(&k, KEY_LEFTSHIFT, true, ev);
                if (level & 2) {
                    n = rb_gemkbd_key(&k, KEY_RIGHTALT, true, ev);
                    for (int j = 0; j < n; j++)
                        got[ev[j].code] = true;
                }
                n = rb_gemkbd_key(&k, MATRIX[i], true, ev);
                for (int j = 0; j < n; j++)
                    if (ev[j].down)
                        got[ev[j].code] = true;
            }
        int missing = 0;
        for (size_t i = 0; i < sizeof need / sizeof need[0]; i++)
            if (!got[need[i]]) {
                fprintf(stderr, "layout %d: key %u unreachable\n", layout, need[i]);
                missing++;
            }
        CHECK_EQ(missing, 0);
        int unmapped = 0;
        for (int c = 1; c < RB_GEMKBD_CODES; c++)
            if (got[c] && !rb_evdev_to_qcode((unsigned)c) && c != KEY_BRIGHTNESSUP && c != KEY_BRIGHTNESSDOWN)
                unmapped++;
        CHECK_EQ(unmapped, 0);
    }
    /* the table, as data */
    struct rb_gemkbd_map m = rb_gemkbd_lookup(RB_GEMKBD_US, KEY_K, 2);
    CHECK(m.code == KEY_SEMICOLON && m.shift == 0 && !m.option);
    m = rb_gemkbd_lookup(RB_GEMKBD_US, KEY_4, 2);
    CHECK(m.code == KEY_2 && m.shift == 1 && m.option);
    m = rb_gemkbd_lookup(RB_GEMKBD_US, KEY_Q, 2);
    CHECK(m.code == 0);
    m = rb_gemkbd_lookup(RB_GEMKBD_UK, KEY_K, 2);
    CHECK(m.code == KEY_2 && m.shift == 1);
    m = rb_gemkbd_lookup(RB_GEMKBD_US, KEY_ENTER, 2);
    CHECK(m.code == KEY_ENTER && m.shift == -1);
}

static void test_detection_and_config(void)
{
    char path[] = "/tmp/rbgemkbdXXXXXX";
    int fd = mkstemp(path);
    CHECK(fd >= 0);
    static const char gem[] = "planet,gemini-pda\0mediatek,mt6797\0";
    CHECK_EQ(write(fd, gem, sizeof gem - 1), (long)(sizeof gem - 1));
    close(fd);
    CHECK(rb_gemini_board(path));
    FILE *f = fopen(path, "w");
    fputs("raspberrypi,5-model-b", f);
    fputc(0, f);
    fclose(f);
    CHECK(!rb_gemini_board(path));
    unlink(path);
    CHECK(!rb_gemini_board("/nonexistent/compatible"));
    unsetenv("RB_GEMINI_KEYBOARD_NAME");
    CHECK(rb_gemini_keyboard_device("keyboard", 0x19));
    CHECK(!rb_gemini_keyboard_device("keyboard", 0x03)); /* a USB keyboard named that */
    CHECK(!rb_gemini_keyboard_device("mt6351-keys", 0x19));
    CHECK(!rb_gemini_keyboard_device("lid-switch", 0x19));
    setenv("RB_GEMINI_KEYBOARD_NAME", "Gemini Test KB", 1);
    CHECK(rb_gemini_keyboard_device("Gemini Test KB", 0x03));
    unsetenv("RB_GEMINI_KEYBOARD_NAME");
    CHECK_EQ(rb_gemkbd_layout_parse("UK"), RB_GEMKBD_UK);
    CHECK_EQ(rb_gemkbd_layout_parse("gb"), RB_GEMKBD_UK);
    CHECK_EQ(rb_gemkbd_layout_parse("us"), RB_GEMKBD_US);
    CHECK_EQ(rb_gemkbd_layout_parse("de"), -1);
    /* input_config fields */
    char err[128];
    struct rb_input_req in;
    cJSON *req = cJSON_Parse("{\"keyboard_layout\":\"uk\",\"touch_transform\":\"swap-xy,invert-y\"}");
    CHECK_EQ(rb_parse_input_config(req, &in, err, sizeof err), 0);
    CHECK(in.has_keyboard_layout && in.keyboard_layout == RB_GEMKBD_UK);
    CHECK(in.has_touch_transform && in.touch_transform == (RB_TOUCH_SWAP_XY | RB_TOUCH_INVERT_Y));
    cJSON_Delete(req);
    req = cJSON_Parse("{\"keyboard_layout\":\"fr\"}");
    CHECK_EQ(rb_parse_input_config(req, &in, err, sizeof err), -1);
    cJSON_Delete(req);
    req = cJSON_Parse("{\"touch_transform\":\"rotate\"}");
    CHECK_EQ(rb_parse_input_config(req, &in, err, sizeof err), -1);
    cJSON_Delete(req);
    req = cJSON_Parse("{\"swap_command_option\":true}"); /* a PC's request: nothing new */
    CHECK_EQ(rb_parse_input_config(req, &in, err, sizeof err), 0);
    CHECK(!in.has_keyboard_layout && !in.has_touch_transform);
    cJSON_Delete(req);
}

static void test_touch_transform(void)
{
    CHECK_EQ(rb_touch_transform_parse(NULL), 0);
    CHECK_EQ(rb_touch_transform_parse(""), 0);
    CHECK_EQ(rb_touch_transform_parse("none"), 0);
    CHECK_EQ(rb_touch_transform_parse("invert-x,invert-y"), RB_TOUCH_INVERT_X | RB_TOUCH_INVERT_Y);
    CHECK_EQ(rb_touch_transform_parse("swap-xy"), RB_TOUCH_SWAP_XY);
    CHECK_EQ(rb_touch_transform_parse("flip"), -1);
    double x = 0.25, y = 0.75;
    rb_touch_transform_apply(0, &x, &y);
    CHECK(x == 0.25 && y == 0.75);
    rb_touch_transform_apply(RB_TOUCH_SWAP_XY, &x, &y);
    CHECK(x == 0.75 && y == 0.25);
    rb_touch_transform_apply(RB_TOUCH_INVERT_X | RB_TOUCH_INVERT_Y, &x, &y); /* a 180 degree turn */
    CHECK(x == 0.25 && y == 0.75);
    x = 0.0;
    y = 1.0;
    rb_touch_transform_apply(RB_TOUCH_INVERT_X | RB_TOUCH_INVERT_Y, &x, &y);
    CHECK(x < 1.0 && x > 0.999 && y == 0.0);
    char buf[48];
    CHECK(!strcmp(rb_touch_transform_name(RB_TOUCH_SWAP_XY | RB_TOUCH_INVERT_X, buf, sizeof buf), "swap-xy,invert-x"));
    CHECK(!strcmp(rb_touch_transform_name(0, buf, sizeof buf), "none"));
}

int main(void)
{
    test_combos();
    test_release_order();
    test_balanced();
    test_reachable();
    test_detection_and_config();
    test_touch_transform();
    TEST_DONE();
}
