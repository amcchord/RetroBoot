/* Linux evdev gamepad reader & classic Mac key injector (#54).
 *
 * Reads /dev/input/event* devices matching EV_ABS / BTN_GAMEPAD / BTN_DPAD_*
 * and translates directional controls (D-pad, HAT0, analog stick) and face
 * buttons (South=Space, East=Option, West=Ctrl, North=Return, Start=Return,
 * Select=Esc, L1=Ctrl, R1=Option) into refcounted Mac key events while active
 * (VIEW_GUEST focused and mode != off). */
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

struct rb_loop;

enum rb_gamepad_mode {
    RB_GAMEPAD_ARROWS = 0,
    RB_GAMEPAD_WASD = 1,
    RB_GAMEPAD_OFF = 2,
};

const char *rb_gamepad_mode_name(enum rb_gamepad_mode mode);
int rb_gamepad_mode_parse(const char *s); /* returns enum rb_gamepad_mode or -1 */

/* Logical Mac key slots injected by the gamepad (mapped to QKeyCode by keymap). */
enum rb_gamepad_key {
    RB_GP_KEY_UP = 0,
    RB_GP_KEY_DOWN,
    RB_GP_KEY_LEFT,
    RB_GP_KEY_RIGHT,
    RB_GP_KEY_W,
    RB_GP_KEY_A,
    RB_GP_KEY_S,
    RB_GP_KEY_D,
    RB_GP_KEY_SPACE,
    RB_GP_KEY_RETURN,
    RB_GP_KEY_OPTION,
    RB_GP_KEY_CTRL,
    RB_GP_KEY_ESC,
    RB_GP_KEY_COUNT
};

/* Return QEMU QKeyCode (RB_Q_*) or 0 if invalid. Pure mapping helper. */
int rb_gamepad_key_to_qcode(enum rb_gamepad_key key);

struct rb_gamepad_abs_info {
    int32_t minimum;
    int32_t maximum;
    int32_t flat;
};

struct rb_gamepad;

typedef void (*rb_gamepad_key_fn)(void *data, int qcode, bool down);

struct rb_gamepad *rb_gamepad_new(struct rb_loop *loop, rb_gamepad_key_fn on_key, void *data);
void rb_gamepad_free(struct rb_gamepad *gp);

void rb_gamepad_set_mode(struct rb_gamepad *gp, enum rb_gamepad_mode mode);
enum rb_gamepad_mode rb_gamepad_get_mode(const struct rb_gamepad *gp);
void rb_gamepad_set_enabled(struct rb_gamepad *gp, bool enabled);
bool rb_gamepad_get_enabled(const struct rb_gamepad *gp);
int rb_gamepad_count(const struct rb_gamepad *gp);

/* Pure classification & event feeding (used both by Linux evdev reader and unit tests). */
bool rb_gamepad_is_gamepad_bits(const unsigned long *evbits, size_t ev_longs, const unsigned long *keybits,
                                size_t key_longs);
int rb_gamepad_add_slot(struct rb_gamepad *gp, const struct rb_gamepad_abs_info *abs_x,
                        const struct rb_gamepad_abs_info *abs_y);
void rb_gamepad_remove_slot(struct rb_gamepad *gp, int slot);
void rb_gamepad_feed_ev(struct rb_gamepad *gp, int slot, uint16_t type, uint16_t code, int32_t value);
