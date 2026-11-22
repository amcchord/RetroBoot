/* evdev -> QKeyCode table, chords and panel key mapping. */
#include <linux/input-event-codes.h>
#include <string.h>
#include <xkbcommon/xkbcommon-keysyms.h>

#include "keymap.h"
#include "rbtest.h"
#include "rbui.h"

/* Linux keycode -> QKeyCode name, from QEMU 11.0.2 keycodemapdb/keymaps.csv. */
static const struct {
    unsigned code;
    const char *q;
} expected[] = {
    {1, "esc"}, {2, "1"}, {11, "0"}, {12, "minus"}, {13, "equal"}, {14, "backspace"}, {15, "tab"}, {16, "q"},
    {25, "p"}, {26, "bracket_left"}, {27, "bracket_right"}, {28, "ret"}, {29, "ctrl"}, {30, "a"}, {38, "l"},
    {39, "semicolon"}, {40, "apostrophe"}, {41, "grave_accent"}, {42, "shift"}, {43, "backslash"}, {44, "z"},
    {50, "m"}, {51, "comma"}, {52, "dot"}, {53, "slash"}, {54, "shift_r"}, {55, "kp_multiply"}, {56, "alt"},
    {57, "spc"}, {58, "caps_lock"}, {59, "f1"}, {68, "f10"}, {69, "num_lock"}, {70, "scroll_lock"}, {71, "kp_7"},
    {72, "kp_8"}, {73, "kp_9"}, {74, "kp_subtract"}, {75, "kp_4"}, {76, "kp_5"}, {77, "kp_6"}, {78, "kp_add"},
    {79, "kp_1"}, {80, "kp_2"}, {81, "kp_3"}, {82, "kp_0"}, {83, "kp_decimal"}, {86, "less"}, {87, "f11"},
    {88, "f12"}, {89, "ro"}, {91, "hiragana"}, {92, "henkan"}, {93, "katakanahiragana"}, {94, "muhenkan"},
    {96, "kp_enter"}, {97, "ctrl_r"}, {98, "kp_divide"}, {99, "print"}, {100, "alt_r"}, {101, "lf"},
    {102, "home"}, {103, "up"}, {104, "pgup"}, {105, "left"}, {106, "right"}, {107, "end"}, {108, "down"},
    {109, "pgdn"}, {110, "insert"}, {111, "delete"}, {113, "audiomute"}, {114, "volumedown"}, {115, "volumeup"},
    {116, "power"}, {117, "kp_equals"}, {119, "pause"}, {121, "kp_comma"}, {122, "lang1"}, {123, "lang2"},
    {124, "yen"}, {125, "meta_l"}, {126, "meta_r"}, {127, "compose"}, {128, "stop"}, {129, "again"},
    {130, "props"}, {131, "undo"}, {132, "front"}, {133, "copy"}, {134, "open"}, {135, "paste"}, {136, "find"},
    {137, "cut"}, {138, "help"}, {139, "menu"}, {140, "calculator"}, {142, "sleep"}, {143, "wake"},
    {155, "mail"}, {156, "ac_bookmarks"}, {157, "computer"}, {158, "ac_back"}, {159, "ac_forward"},
    {163, "audionext"}, {164, "audioplay"}, {165, "audioprev"}, {166, "audiostop"}, {172, "ac_home"},
    {173, "ac_refresh"}, {183, "f13"}, {194, "f24"}, {226, "mediaselect"},
};

int main(void)
{
    for (size_t i = 0; i < sizeof expected / sizeof expected[0]; i++) {
        const char *got = rb_qcode_name(rb_evdev_to_qcode(expected[i].code));
        if (strcmp(got, expected[i].q))
            fprintf(stderr, "evdev %u: got %s want %s\n", expected[i].code, got, expected[i].q);
        CHECK(!strcmp(got, expected[i].q));
    }
    /* Letters/digits are complete. */
    const unsigned letters[] = {KEY_A, KEY_B, KEY_C, KEY_D, KEY_E, KEY_F, KEY_G, KEY_H, KEY_I, KEY_J, KEY_K,
                                KEY_L, KEY_M, KEY_N, KEY_O, KEY_P, KEY_Q, KEY_R, KEY_S, KEY_T, KEY_U, KEY_V,
                                KEY_W, KEY_X, KEY_Y, KEY_Z};
    for (int i = 0; i < 26; i++) {
        char want[2] = {(char)('a' + i), 0};
        CHECK(!strcmp(rb_qcode_name(rb_evdev_to_qcode(letters[i])), want));
    }
    for (unsigned k = KEY_F1; k <= KEY_F10; k++)
        CHECK(rb_evdev_to_qcode(k) != 0);
    CHECK_EQ(rb_evdev_to_qcode(0), 0);
    CHECK_EQ(rb_evdev_to_qcode(5000), 0);
    CHECK_EQ(rb_evdev_to_qcode(KEY_LEFTMETA), RB_Q_META_L);
    CHECK_EQ(rb_evdev_to_qcode(KEY_LEFTALT), RB_Q_ALT);
    CHECK_EQ(rb_evdev_to_qcode(KEY_ESC), RB_Q_ESC);
    CHECK_EQ(rb_evdev_to_qcode(KEY_ENTER), RB_Q_RET);
    CHECK_EQ(rb_evdev_to_qcode(KEY_POWER), RB_Q_POWER);
    /* No two evdev codes collide except documented aliases. */
    int seen[RB_Q_COUNT];
    memset(seen, 0, sizeof seen);
    for (unsigned k = 1; k < 256; k++) {
        int q = rb_evdev_to_qcode(k);
        if (q)
            seen[q]++;
    }
    for (int q = 1; q < RB_Q_COUNT; q++)
        CHECK(seen[q] <= 1);

    CHECK_EQ(rb_qcode_swap_command_option(RB_Q_ALT), RB_Q_META_L);
    CHECK_EQ(rb_qcode_swap_command_option(RB_Q_META_R), RB_Q_ALT_R);
    CHECK_EQ(rb_qcode_swap_command_option(RB_Q_A), RB_Q_A);

    CHECK_EQ(rb_chord_parse("ctrl+alt+delete"), KEY_DELETE);
    CHECK_EQ(rb_chord_parse("ctrl+alt+backspace"), KEY_BACKSPACE);
    CHECK_EQ(rb_chord_parse("ctrl+alt+escape"), KEY_ESC);
    CHECK_EQ(rb_chord_parse("ctrl+alt+f1"), -1);
    CHECK_EQ(rb_chord_parse(NULL), -1);
    CHECK(!strcmp(rb_chord_name(KEY_BACKSPACE), "ctrl+alt+backspace"));

    CHECK(rb_evdev_is_modifier(KEY_LEFTCTRL));
    CHECK(rb_evdev_is_modifier(KEY_RIGHTMETA));
    CHECK(!rb_evdev_is_modifier(KEY_A));

    CHECK_EQ(rb_keysym_to_rbui(XKB_KEY_Return), RBUI_KEY_RETURN);
    CHECK_EQ(rb_keysym_to_rbui(XKB_KEY_ISO_Left_Tab), RBUI_KEY_TAB);
    CHECK_EQ(rb_keysym_to_rbui(XKB_KEY_KP_Enter), RBUI_KEY_KP_ENTER);
    CHECK_EQ(rb_keysym_to_rbui(XKB_KEY_Escape), RBUI_KEY_ESCAPE);
    CHECK_EQ(rb_keysym_to_rbui(XKB_KEY_Next), RBUI_KEY_PAGEDOWN);
    CHECK_EQ(rb_keysym_to_rbui(XKB_KEY_space), RBUI_KEY_SPACE);
    CHECK_EQ(rb_keysym_to_rbui(XKB_KEY_a), RBUI_KEY_NONE);

    char u[8];
    CHECK_EQ(rb_evdev_to_rbui_fallback(KEY_A, false, u), RBUI_KEY_NONE);
    CHECK(!strcmp(u, "a"));
    CHECK_EQ(rb_evdev_to_rbui_fallback(KEY_2, true, u), RBUI_KEY_NONE);
    CHECK(!strcmp(u, "@"));
    CHECK_EQ(rb_evdev_to_rbui_fallback(KEY_ENTER, false, u), RBUI_KEY_RETURN);
    CHECK(!strcmp(u, ""));
    CHECK_EQ(rb_evdev_to_rbui_fallback(KEY_SPACE, false, u), RBUI_KEY_SPACE);
    CHECK(!strcmp(u, " "));
    TEST_DONE();
}
