/* Keyboard tables: evdev (linux/input-event-codes.h) -> QEMU QKeyCode,
 * position based, cross-checked against QEMU 11.0.2 keycodemapdb; recovery
 * chords; and panel key mapping (xkb keysym -> RBUI_KEY_*). */
#pragma once
#include <stdbool.h>
#include <stdint.h>

/* QKeyCode values (qapi/ui.json order, QEMU 11.0.2) used by the presenter. */
enum {
    RB_Q_UNMAPPED = 0,
    RB_Q_SHIFT = 1,
    RB_Q_SHIFT_R = 2,
    RB_Q_ALT = 3,
    RB_Q_ALT_R = 4,
    RB_Q_CTRL = 5,
    RB_Q_CTRL_R = 6,
    RB_Q_ESC = 8,
    RB_Q_RET = 35,
    RB_Q_A = 36,
    RB_Q_SPC = 60,
    RB_Q_DELETE = 104,
    RB_Q_META_L = 117,
    RB_Q_META_R = 118,
    RB_Q_POWER = 129,
    RB_Q_COUNT = 162,
};

int rb_evdev_to_qcode(unsigned code);
/* Applies the Command/Option swap: alt<->meta_l, alt_r<->meta_r. */
int rb_qcode_swap_command_option(int qcode);
const char *rb_qcode_name(int qcode);
bool rb_evdev_is_modifier(unsigned code);

/* Recovery chord: returns the evdev trigger key (KEY_DELETE, KEY_BACKSPACE,
 * KEY_ESC) for "ctrl+alt+delete" etc., or -1. */
int rb_chord_parse(const char *name);
const char *rb_chord_name(int trigger);

/* Panel keys. keysym is an xkb keysym (uint32). Returns RBUI_KEY_* or 0. */
int rb_keysym_to_rbui(uint32_t keysym);
/* Fallback when no xkb keymap is available: US layout from evdev codes.
 * Writes UTF-8 text (or "") and returns RBUI_KEY_* or 0. */
int rb_evdev_to_rbui_fallback(unsigned code, bool shift, char utf8[8]);
