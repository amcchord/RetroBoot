/* Gamepad mode parsing, evdev bitmask filtering, and button/stick -> Mac key
 * refcounting & hysteresis (#54). */
#include <string.h>

#ifdef __linux__
#include <linux/input-event-codes.h>
#else
#define EV_SYN 0x00
#define EV_KEY 0x01
#define EV_ABS 0x03
#define SYN_DROPPED 3
#define ABS_X 0x00
#define ABS_Y 0x01
#define ABS_HAT0X 0x10
#define ABS_HAT0Y 0x11
#define BTN_LEFT 0x110
#define BTN_SOUTH 0x130
#define BTN_EAST 0x131
#define BTN_NORTH 0x133
#define BTN_WEST 0x134
#define BTN_TL 0x136
#define BTN_TR 0x137
#define BTN_SELECT 0x13a
#define BTN_START 0x13b
#define BTN_DPAD_UP 0x220
#define BTN_DPAD_DOWN 0x221
#define BTN_DPAD_LEFT 0x222
#define BTN_DPAD_RIGHT 0x223
#define KEY_A 30
#endif

#include "gamepad.h"
#include "keymap.h"
#include "rbtest.h"

struct key_ev {
    int qcode;
    bool down;
};

struct log_state {
    struct key_ev ev[64];
    int n;
    int held[RB_Q_COUNT];
};

static void record_key(void *data, int qcode, bool down)
{
    struct log_state *st = data;
    if (st->n < 64)
        st->ev[st->n++] = (struct key_ev){qcode, down};
    if (qcode >= 0 && qcode < RB_Q_COUNT)
        st->held[qcode] += down ? 1 : -1;
}

static void set_bit(unsigned long *bits, unsigned bit)
{
    bits[bit / (8 * sizeof(unsigned long))] |= 1UL << (bit % (8 * sizeof(unsigned long)));
}

static void test_modes_and_qcodes(void)
{
    CHECK(!strcmp(rb_gamepad_mode_name(RB_GAMEPAD_ARROWS), "arrows"));
    CHECK(!strcmp(rb_gamepad_mode_name(RB_GAMEPAD_WASD), "wasd"));
    CHECK(!strcmp(rb_gamepad_mode_name(RB_GAMEPAD_OFF), "off"));

    CHECK_EQ(rb_gamepad_mode_parse("arrows"), RB_GAMEPAD_ARROWS);
    CHECK_EQ(rb_gamepad_mode_parse("wasd"), RB_GAMEPAD_WASD);
    CHECK_EQ(rb_gamepad_mode_parse("off"), RB_GAMEPAD_OFF);
    CHECK_EQ(rb_gamepad_mode_parse("invalid"), -1);
    CHECK_EQ(rb_gamepad_mode_parse(NULL), -1);

    CHECK_EQ(rb_gamepad_key_to_qcode(RB_GP_KEY_UP), RB_Q_UP);
    CHECK(!strcmp(rb_qcode_name(rb_gamepad_key_to_qcode(RB_GP_KEY_UP)), "up"));
    CHECK(!strcmp(rb_qcode_name(rb_gamepad_key_to_qcode(RB_GP_KEY_DOWN)), "down"));
    CHECK(!strcmp(rb_qcode_name(rb_gamepad_key_to_qcode(RB_GP_KEY_LEFT)), "left"));
    CHECK(!strcmp(rb_qcode_name(rb_gamepad_key_to_qcode(RB_GP_KEY_RIGHT)), "right"));
    CHECK(!strcmp(rb_qcode_name(rb_gamepad_key_to_qcode(RB_GP_KEY_W)), "w"));
    CHECK(!strcmp(rb_qcode_name(rb_gamepad_key_to_qcode(RB_GP_KEY_A)), "a"));
    CHECK(!strcmp(rb_qcode_name(rb_gamepad_key_to_qcode(RB_GP_KEY_S)), "s"));
    CHECK(!strcmp(rb_qcode_name(rb_gamepad_key_to_qcode(RB_GP_KEY_D)), "d"));
    CHECK(!strcmp(rb_qcode_name(rb_gamepad_key_to_qcode(RB_GP_KEY_SPACE)), "spc"));
    CHECK(!strcmp(rb_qcode_name(rb_gamepad_key_to_qcode(RB_GP_KEY_RETURN)), "ret"));
    CHECK(!strcmp(rb_qcode_name(rb_gamepad_key_to_qcode(RB_GP_KEY_OPTION)), "alt"));
    CHECK(!strcmp(rb_qcode_name(rb_gamepad_key_to_qcode(RB_GP_KEY_CTRL)), "ctrl"));
    CHECK(!strcmp(rb_qcode_name(rb_gamepad_key_to_qcode(RB_GP_KEY_ESC)), "esc"));
    CHECK_EQ(rb_gamepad_key_to_qcode(RB_GP_KEY_COUNT), 0);
}

static void test_device_classification(void)
{
    unsigned long ev[2] = {0}, key[16] = {0};
    CHECK(!rb_gamepad_is_gamepad_bits(NULL, 0, NULL, 0));
    CHECK(!rb_gamepad_is_gamepad_bits(ev, 2, key, 16));

    set_bit(ev, EV_KEY);
    set_bit(key, KEY_A);
    CHECK(!rb_gamepad_is_gamepad_bits(ev, 2, key, 16));

    memset(key, 0, sizeof key);
    set_bit(key, BTN_LEFT);
    CHECK(!rb_gamepad_is_gamepad_bits(ev, 2, key, 16));

    set_bit(key, BTN_SOUTH);
    CHECK(rb_gamepad_is_gamepad_bits(ev, 2, key, 16));

    memset(key, 0, sizeof key);
    set_bit(key, BTN_DPAD_UP);
    CHECK(rb_gamepad_is_gamepad_bits(ev, 2, key, 16));
}

static void test_arrows_wasd_refcount_and_gating(void)
{
    struct log_state st = {0};
    struct rb_gamepad *gp = rb_gamepad_new(NULL, record_key, &st);
    CHECK(gp != NULL);
    CHECK_EQ(rb_gamepad_get_mode(gp), RB_GAMEPAD_ARROWS);
    CHECK(!rb_gamepad_get_enabled(gp));

    struct rb_gamepad_abs_info byte_axis = {0, 255, 16};
    int s0 = rb_gamepad_add_slot(gp, &byte_axis, &byte_axis);
    int s1 = rb_gamepad_add_slot(gp, NULL, NULL);
    CHECK(s0 >= 0 && s1 >= 0 && s0 != s1);
    CHECK_EQ(rb_gamepad_count(gp), 2);

    /* Disabled until VIEW_GUEST is active: pressing buttons emits nothing yet. */
    rb_gamepad_feed_ev(gp, s0, EV_KEY, BTN_SOUTH, 1);
    CHECK_EQ(st.n, 0);
    rb_gamepad_feed_ev(gp, s0, EV_KEY, BTN_SOUTH, 0);
    CHECK_EQ(st.n, 0);

    /* Enable guest injection. */
    rb_gamepad_set_enabled(gp, true);
    CHECK(rb_gamepad_get_enabled(gp));

    /* Face & shoulder buttons -> Space / Option / Ctrl / Return / Esc. */
    rb_gamepad_feed_ev(gp, s0, EV_KEY, BTN_SOUTH, 1);
    rb_gamepad_feed_ev(gp, s0, EV_KEY, BTN_SOUTH, 0);
    rb_gamepad_feed_ev(gp, s0, EV_KEY, BTN_EAST, 1);
    rb_gamepad_feed_ev(gp, s0, EV_KEY, BTN_TR, 1); /* second Option source: no duplicate down */
    rb_gamepad_feed_ev(gp, s0, EV_KEY, BTN_EAST, 0);
    CHECK_EQ(st.held[RB_Q_ALT], 1);
    rb_gamepad_feed_ev(gp, s0, EV_KEY, BTN_TR, 0);
    CHECK_EQ(st.held[RB_Q_ALT], 0);
    rb_gamepad_feed_ev(gp, s0, EV_KEY, BTN_WEST, 1);
    rb_gamepad_feed_ev(gp, s0, EV_KEY, BTN_WEST, 0);
    rb_gamepad_feed_ev(gp, s0, EV_KEY, BTN_START, 1);
    rb_gamepad_feed_ev(gp, s0, EV_KEY, BTN_START, 0);
    rb_gamepad_feed_ev(gp, s0, EV_KEY, BTN_SELECT, 1);
    rb_gamepad_feed_ev(gp, s0, EV_KEY, BTN_SELECT, 0);

    CHECK_EQ(st.n, 10);
    CHECK(st.ev[0].qcode == RB_Q_SPC && st.ev[0].down);
    CHECK(st.ev[1].qcode == RB_Q_SPC && !st.ev[1].down);
    CHECK(st.ev[2].qcode == RB_Q_ALT && st.ev[2].down);
    CHECK(st.ev[3].qcode == RB_Q_ALT && !st.ev[3].down);
    CHECK(st.ev[4].qcode == RB_Q_CTRL && st.ev[4].down);
    CHECK(st.ev[5].qcode == RB_Q_CTRL && !st.ev[5].down);
    CHECK(st.ev[6].qcode == RB_Q_RET && st.ev[6].down);
    CHECK(st.ev[7].qcode == RB_Q_RET && !st.ev[7].down);
    CHECK(st.ev[8].qcode == RB_Q_ESC && st.ev[8].down);
    CHECK(st.ev[9].qcode == RB_Q_ESC && !st.ev[9].down);

    /* Analog stick hysteresis [0..255]: mid=127, hi=63 (190+), lo=44 (171+). */
    st.n = 0;
    rb_gamepad_feed_ev(gp, s0, EV_ABS, ABS_X, 175); /* below hi=190 -> stays unpressed */
    CHECK_EQ(st.n, 0);
    rb_gamepad_feed_ev(gp, s0, EV_ABS, ABS_X, 200); /* >=190 -> RIGHT down */
    CHECK_EQ(st.n, 1);
    CHECK(st.ev[0].qcode == RB_Q_RIGHT && st.ev[0].down);
    rb_gamepad_feed_ev(gp, s0, EV_ABS, ABS_X, 175); /* >=171 -> stays pressed */
    CHECK_EQ(st.n, 1);
    rb_gamepad_feed_ev(gp, s0, EV_ABS, ABS_X, 160); /* <171 -> RIGHT up */
    CHECK_EQ(st.n, 2);
    CHECK(st.ev[1].qcode == RB_Q_RIGHT && !st.ev[1].down);

    /* Multi-source & cross-device refcounting + mode switch ARROWS -> WASD. */
    st.n = 0;
    rb_gamepad_feed_ev(gp, s0, EV_KEY, BTN_DPAD_UP, 1);
    rb_gamepad_feed_ev(gp, s0, EV_ABS, ABS_HAT0Y, -1);
    rb_gamepad_feed_ev(gp, s1, EV_KEY, BTN_DPAD_UP, 1);
    CHECK_EQ(st.n, 1);
    CHECK(st.ev[0].qcode == RB_Q_UP && st.ev[0].down);
    CHECK_EQ(st.held[RB_Q_UP], 1);

    rb_gamepad_set_mode(gp, RB_GAMEPAD_WASD);
    CHECK_EQ(st.held[RB_Q_UP], 0);
    CHECK_EQ(st.held[RB_Q_W], 1);

    /* Releasing 2 of 3 UP/W sources keeps W down; releasing 3rd drops W. */
    rb_gamepad_feed_ev(gp, s0, EV_KEY, BTN_DPAD_UP, 0);
    rb_gamepad_feed_ev(gp, s0, EV_ABS, ABS_HAT0Y, 0);
    CHECK_EQ(st.held[RB_Q_W], 1);
    rb_gamepad_feed_ev(gp, s1, EV_KEY, BTN_DPAD_UP, 0);
    CHECK_EQ(st.held[RB_Q_W], 0);

    /* Mode OFF releases held keys and blocks input; restoring WASD syncs cleanly. */
    rb_gamepad_feed_ev(gp, s0, EV_ABS, ABS_HAT0X, -1);
    CHECK_EQ(st.held[RB_Q_A], 1);
    rb_gamepad_set_mode(gp, RB_GAMEPAD_OFF);
    CHECK_EQ(st.held[RB_Q_A], 0);
    rb_gamepad_feed_ev(gp, s0, EV_KEY, BTN_NORTH, 1);
    CHECK_EQ(st.held[RB_Q_RET], 0);
    rb_gamepad_set_mode(gp, RB_GAMEPAD_WASD);
    CHECK_EQ(st.held[RB_Q_A], 1);
    CHECK_EQ(st.held[RB_Q_RET], 1);

    /* SYN_DROPPED clears slot 0 sources cleanly, leaving nothing stuck. */
    rb_gamepad_feed_ev(gp, s0, EV_SYN, SYN_DROPPED, 0);
    CHECK_EQ(st.held[RB_Q_A], 0);
    CHECK_EQ(st.held[RB_Q_RET], 0);

    /* Losing focus releases active keys on free / disable. */
    rb_gamepad_feed_ev(gp, s1, EV_ABS, ABS_Y, -32768);
    CHECK_EQ(st.held[RB_Q_W], 1);
    rb_gamepad_set_enabled(gp, false);
    CHECK_EQ(st.held[RB_Q_W], 0);

    rb_gamepad_remove_slot(gp, s0);
    CHECK_EQ(rb_gamepad_count(gp), 1);
    rb_gamepad_free(gp);
    CHECK_EQ(st.held[RB_Q_W], 0);
}

int main(void)
{
    test_modes_and_qcodes();
    test_device_classification();
    test_arrows_wasd_refcount_and_gating();
    TEST_DONE();
}
