/* Planet Computers Gemini PDA built-in keyboard (pure state machine; tests/test_gemkbd.c).
 *
 * The Gemini's keyboard is an AW9523B-scanned matrix (gpio-matrix-keypad, input
 * device "keyboard", BUS_HOST): KEY_1..0, the letters, COMMA, DOT, APOSTROPHE,
 * TAB, BACKSPACE, ENTER, SPACE, the arrows, both Shifts, LEFTCTRL, LEFTALT, and
 * Fn reported as KEY_RIGHTALT (matrix (4,3)). There is no Windows/Super key. The
 * Fn layer exists only in the gemini-nixos "gemini" XKB symbols (levels: base,
 * Shift, Fn, Fn+Shift; layout "gemini" = UK keycaps, variant "us" = US keycaps).
 * Escape is the separate Esc/On key (mt6351-keys: a tap sends KEY_ESC on release,
 * a 0.8 s hold KEY_POWER), the silver side button KEY_SLEEP.
 *
 * QEMU's keyboard is positional (a Mac with Mac OS's U.S. keyboard layout), so the
 * layer turns each key into the U.S. keystroke (with Shift and Option as needed)
 * that types its keycap's symbol, before host keys, the recovery chord, the
 * panel's xkb and the guest see it. The table (gemkbd.c, GK_US / GK_UK) is the
 * symbols file's four levels translated to Mac OS's U.S. KCHR:
 *
 *   Alt                   Command (KEY_LEFTMETA, the Windows key's code)
 *   Ctrl                  Control
 *   Fn + key              the Fn legend: ; " ' + - = _ [ ] { } ~ ` £ € and so on
 *   Shift+Fn + 1..0       F1..F10
 *   Fn + arrows           Page Up / Home / Page Down / End
 *   Fn + Backspace        forward Delete;  Fn+Tab Caps Lock;  Fn+Space space
 *   Fn + C V B N T        volume down/up, brightness down/up, mute (host keys)
 *   Fn with Alt or Ctrl   Option (KEY_LEFTALT): Command-Option-Esc = Alt+Fn+Esc
 *   Fn during a touch     Option for that tap or drag (rb_gemkbd_pointer)
 *   Shift+, Shift+.       / ?  (the keycaps); Shift+Backspace stays Shift+Delete
 *   legends Mac OS has no key for (media, ☺, arrows as symbols): nothing
 *
 * input.swap_command_option keeps working: the presenter swaps KEY_LEFTMETA and
 * KEY_LEFTALT, so with it on Alt is Option and Fn+Alt/Fn+Ctrl Command; an
 * Option the table adds to type a symbol (£ is Option+3) is the swap-aware code,
 * so it stays Option. Fn never reaches Mac OS by itself. Injected and physical
 * modifiers are reference counted, so every output key gets exactly one press
 * and one release whatever order the keys are released in. */
#pragma once
#include <stdbool.h>
#include <stdint.h>

enum rb_gemkbd_layout { RB_GEMKBD_US = 0, RB_GEMKBD_UK = 1 };

struct rb_gemkbd_ev {
    uint16_t code; /* evdev */
    bool down;
};

#define RB_GEMKBD_CODES 256
#define RB_GEMKBD_MAX_EV 8

struct rb_gemkbd {
    int layout; /* enum rb_gemkbd_layout */
    bool swap;  /* input.swap_command_option */
    bool fn, lshift, rshift, alt, ctrl;
    bool fnopt;      /* Fn acting as Option (with Alt/Ctrl, or for a touch) */
    int touch_opt;   /* bit b: pointer button b went down with Fn held */
    struct {
        uint16_t out;  /* the key this physical key pressed (0: none) */
        uint16_t optc; /* the Option key it injected (0: none) */
        uint8_t mods;  /* shift handling it applied (gemkbd.c GK_*) */
    } held[RB_GEMKBD_CODES];
    uint8_t refs[RB_GEMKBD_CODES]; /* presses of each output key */
    int lift; /* keys holding Shift off */
};

void rb_gemkbd_init(struct rb_gemkbd *k, int layout, bool swap);
void rb_gemkbd_configure(struct rb_gemkbd *k, int layout, bool swap);
/* One physical event from the Gemini's keyboard; writes the events to deliver
 * (at most RB_GEMKBD_MAX_EV) and returns how many. */
int rb_gemkbd_key(struct rb_gemkbd *k, unsigned code, bool down, struct rb_gemkbd_ev *out);
/* Pointer button `button` (0..7; a touch tap or drag) goes down or up: with Fn
 * held at the press, Option is held until it goes up (Option-click, Option-drag). */
int rb_gemkbd_pointer(struct rb_gemkbd *k, int button, bool down, struct rb_gemkbd_ev *out);
/* What a key gives at a level (0 base, 1 Shift, 2 Fn, 3 Fn+Shift): the evdev key
 * (0: nothing) and whether Shift/Option are pressed with it (for tests and docs):
 * shift 1 pressed, 0 released, -1 as held; option true/false. */
struct rb_gemkbd_map {
    uint16_t code;
    int shift;
    bool option;
};
struct rb_gemkbd_map rb_gemkbd_lookup(int layout, unsigned key, int level);
/* "us" / "uk" (also "gb"); -1 otherwise. */
int rb_gemkbd_layout_parse(const char *name);
const char *rb_gemkbd_layout_name(int layout);
/* Is this the Gemini PDA (a device tree compatible list containing
 * "planet,gemini-pda")? path NULL: /proc/device-tree/compatible. */
bool rb_gemini_board(const char *path);
/* Is an input device the Gemini's built-in keyboard? name: libinput's;
 * bus: its bustype (BUS_HOST for the matrix); RB_GEMINI_KEYBOARD_NAME
 * overrides the name. */
bool rb_gemini_keyboard_device(const char *name, unsigned bus);
