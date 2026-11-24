#include "keymap.h"

#include <linux/input-event-codes.h>
#include <string.h>
#include <xkbcommon/xkbcommon-keysyms.h>

#include "rbui.h"

static const char *const qnames[RB_Q_COUNT] = {
    "unmapped", "shift", "shift_r", "alt", "alt_r", "ctrl", "ctrl_r", "menu", "esc", "1", "2", "3", "4", "5", "6",
    "7", "8", "9", "0", "minus", "equal", "backspace", "tab", "q", "w", "e", "r", "t", "y", "u", "i", "o", "p",
    "bracket_left", "bracket_right", "ret", "a", "s", "d", "f", "g", "h", "j", "k", "l", "semicolon", "apostrophe",
    "grave_accent", "backslash", "z", "x", "c", "v", "b", "n", "m", "comma", "dot", "slash", "asterisk", "spc",
    "caps_lock", "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10", "num_lock", "scroll_lock",
    "kp_divide", "kp_multiply", "kp_subtract", "kp_add", "kp_enter", "kp_decimal", "sysrq", "kp_0", "kp_1", "kp_2",
    "kp_3", "kp_4", "kp_5", "kp_6", "kp_7", "kp_8", "kp_9", "less", "f11", "f12", "print", "home", "pgup", "pgdn",
    "end", "left", "up", "down", "right", "insert", "delete", "stop", "again", "props", "undo", "front", "copy",
    "open", "paste", "find", "cut", "lf", "help", "meta_l", "meta_r", "compose", "pause", "ro", "hiragana",
    "henkan", "yen", "muhenkan", "katakanahiragana", "kp_comma", "kp_equals", "power", "sleep", "wake",
    "audionext", "audioprev", "audiostop", "audioplay", "audiomute", "volumeup", "volumedown", "mediaselect",
    "mail", "calculator", "computer", "ac_home", "ac_back", "ac_forward", "ac_refresh", "ac_bookmarks", "lang1",
    "lang2", "f13", "f14", "f15", "f16", "f17", "f18", "f19", "f20", "f21", "f22", "f23", "f24",
};

/* evdev -> QKeyCode. Values are indices into qnames. */
static const uint8_t evdev_qcode[256] = {
    [KEY_ESC] = 8,
    [KEY_1] = 9, [KEY_2] = 10, [KEY_3] = 11, [KEY_4] = 12, [KEY_5] = 13,
    [KEY_6] = 14, [KEY_7] = 15, [KEY_8] = 16, [KEY_9] = 17, [KEY_0] = 18,
    [KEY_MINUS] = 19, [KEY_EQUAL] = 20, [KEY_BACKSPACE] = 21, [KEY_TAB] = 22,
    [KEY_Q] = 23, [KEY_W] = 24, [KEY_E] = 25, [KEY_R] = 26, [KEY_T] = 27,
    [KEY_Y] = 28, [KEY_U] = 29, [KEY_I] = 30, [KEY_O] = 31, [KEY_P] = 32,
    [KEY_LEFTBRACE] = 33, [KEY_RIGHTBRACE] = 34, [KEY_ENTER] = 35, [KEY_LEFTCTRL] = 5,
    [KEY_A] = 36, [KEY_S] = 37, [KEY_D] = 38, [KEY_F] = 39, [KEY_G] = 40,
    [KEY_H] = 41, [KEY_J] = 42, [KEY_K] = 43, [KEY_L] = 44,
    [KEY_SEMICOLON] = 45, [KEY_APOSTROPHE] = 46, [KEY_GRAVE] = 47, [KEY_LEFTSHIFT] = 1,
    [KEY_BACKSLASH] = 48,
    [KEY_Z] = 49, [KEY_X] = 50, [KEY_C] = 51, [KEY_V] = 52, [KEY_B] = 53,
    [KEY_N] = 54, [KEY_M] = 55, [KEY_COMMA] = 56, [KEY_DOT] = 57, [KEY_SLASH] = 58,
    [KEY_RIGHTSHIFT] = 2, [KEY_KPASTERISK] = 75, [KEY_LEFTALT] = 3, [KEY_SPACE] = 60,
    [KEY_CAPSLOCK] = 61,
    [KEY_F1] = 62, [KEY_F2] = 63, [KEY_F3] = 64, [KEY_F4] = 65, [KEY_F5] = 66,
    [KEY_F6] = 67, [KEY_F7] = 68, [KEY_F8] = 69, [KEY_F9] = 70, [KEY_F10] = 71,
    [KEY_NUMLOCK] = 72, [KEY_SCROLLLOCK] = 73,
    [KEY_KP7] = 88, [KEY_KP8] = 89, [KEY_KP9] = 90, [KEY_KPMINUS] = 76,
    [KEY_KP4] = 85, [KEY_KP5] = 86, [KEY_KP6] = 87, [KEY_KPPLUS] = 77,
    [KEY_KP1] = 82, [KEY_KP2] = 83, [KEY_KP3] = 84, [KEY_KP0] = 81, [KEY_KPDOT] = 79,
    [KEY_102ND] = 91, [KEY_F11] = 92, [KEY_F12] = 93, [KEY_RO] = 121,
    [KEY_HIRAGANA] = 122, [KEY_HENKAN] = 123, [KEY_KATAKANAHIRAGANA] = 126, [KEY_MUHENKAN] = 125,
    [KEY_KPENTER] = 78, [KEY_RIGHTCTRL] = 6, [KEY_KPSLASH] = 74, [KEY_SYSRQ] = 94,
    [KEY_RIGHTALT] = 4, [KEY_LINEFEED] = 115,
    [KEY_HOME] = 95, [KEY_UP] = 100, [KEY_PAGEUP] = 96, [KEY_LEFT] = 99, [KEY_RIGHT] = 102,
    [KEY_END] = 98, [KEY_DOWN] = 101, [KEY_PAGEDOWN] = 97, [KEY_INSERT] = 103, [KEY_DELETE] = 104,
    [KEY_MUTE] = 136, [KEY_VOLUMEDOWN] = 138, [KEY_VOLUMEUP] = 137, [KEY_POWER] = 129,
    [KEY_KPEQUAL] = 128, [KEY_PAUSE] = 120, [KEY_KPCOMMA] = 127,
    [KEY_HANGEUL] = 148, [KEY_HANJA] = 149, [KEY_YEN] = 124,
    [KEY_LEFTMETA] = 117, [KEY_RIGHTMETA] = 118, [KEY_COMPOSE] = 119,
    [KEY_STOP] = 105, [KEY_AGAIN] = 106, [KEY_PROPS] = 107, [KEY_UNDO] = 108, [KEY_FRONT] = 109,
    [KEY_COPY] = 110, [KEY_OPEN] = 111, [KEY_PASTE] = 112, [KEY_FIND] = 113, [KEY_CUT] = 114,
    [KEY_HELP] = 116, [KEY_MENU] = 7, [KEY_CALC] = 141, [KEY_SLEEP] = 130, [KEY_WAKEUP] = 131,
    [KEY_MAIL] = 140, [KEY_BOOKMARKS] = 147, [KEY_COMPUTER] = 142, [KEY_BACK] = 144, [KEY_FORWARD] = 145,
    [KEY_NEXTSONG] = 132, [KEY_PLAYPAUSE] = 135, [KEY_PREVIOUSSONG] = 133, [KEY_STOPCD] = 134,
    [KEY_HOMEPAGE] = 143, [KEY_REFRESH] = 146,
    [KEY_F13] = 150, [KEY_F14] = 151, [KEY_F15] = 152, [KEY_F16] = 153, [KEY_F17] = 154, [KEY_F18] = 155,
    [KEY_F19] = 156, [KEY_F20] = 157, [KEY_F21] = 158, [KEY_F22] = 159, [KEY_F23] = 160, [KEY_F24] = 161,
    [KEY_MEDIA] = 139,
};

int rb_evdev_to_qcode(unsigned code)
{
    if (code >= sizeof evdev_qcode)
        return RB_Q_UNMAPPED;
    return evdev_qcode[code];
}

int rb_qcode_swap_command_option(int q)
{
    switch (q) {
    case RB_Q_ALT: return RB_Q_META_L;
    case RB_Q_ALT_R: return RB_Q_META_R;
    case RB_Q_META_L: return RB_Q_ALT;
    case RB_Q_META_R: return RB_Q_ALT_R;
    default: return q;
    }
}

const char *rb_qcode_name(int q)
{
    if (q < 0 || q >= RB_Q_COUNT)
        return "invalid";
    return qnames[q];
}

bool rb_evdev_is_modifier(unsigned code)
{
    switch (code) {
    case KEY_LEFTCTRL: case KEY_RIGHTCTRL: case KEY_LEFTALT: case KEY_RIGHTALT:
    case KEY_LEFTSHIFT: case KEY_RIGHTSHIFT: case KEY_LEFTMETA: case KEY_RIGHTMETA:
        return true;
    default:
        return false;
    }
}

int rb_chord_parse(const char *name)
{
    if (!name)
        return -1;
    if (!strcmp(name, "ctrl+alt+delete"))
        return KEY_DELETE;
    if (!strcmp(name, "ctrl+alt+backspace"))
        return KEY_BACKSPACE;
    if (!strcmp(name, "ctrl+alt+escape"))
        return KEY_ESC;
    return -1;
}

const char *rb_chord_name(int trigger)
{
    switch (trigger) {
    case KEY_DELETE: return "ctrl+alt+delete";
    case KEY_BACKSPACE: return "ctrl+alt+backspace";
    case KEY_ESC: return "ctrl+alt+escape";
    default: return "none";
    }
}

int rb_keysym_to_rbui(uint32_t sym)
{
    switch (sym) {
    case XKB_KEY_Tab: case XKB_KEY_ISO_Left_Tab: case XKB_KEY_KP_Tab: return RBUI_KEY_TAB;
    case XKB_KEY_Return: return RBUI_KEY_RETURN;
    case XKB_KEY_KP_Enter: return RBUI_KEY_KP_ENTER;
    case XKB_KEY_Escape: return RBUI_KEY_ESCAPE;
    case XKB_KEY_BackSpace: return RBUI_KEY_BACKSPACE;
    case XKB_KEY_Delete: case XKB_KEY_KP_Delete: return RBUI_KEY_DELETE;
    case XKB_KEY_Left: case XKB_KEY_KP_Left: return RBUI_KEY_LEFT;
    case XKB_KEY_Right: case XKB_KEY_KP_Right: return RBUI_KEY_RIGHT;
    case XKB_KEY_Up: case XKB_KEY_KP_Up: return RBUI_KEY_UP;
    case XKB_KEY_Down: case XKB_KEY_KP_Down: return RBUI_KEY_DOWN;
    case XKB_KEY_Home: case XKB_KEY_KP_Home: return RBUI_KEY_HOME;
    case XKB_KEY_End: case XKB_KEY_KP_End: return RBUI_KEY_END;
    case XKB_KEY_Prior: case XKB_KEY_KP_Prior: return RBUI_KEY_PAGEUP;
    case XKB_KEY_Next: case XKB_KEY_KP_Next: return RBUI_KEY_PAGEDOWN;
    case XKB_KEY_space: case XKB_KEY_KP_Space: return RBUI_KEY_SPACE;
    default: return RBUI_KEY_NONE;
    }
}

/* US layout fallback (no xkb data): unshifted and shifted characters. */
static const char *const us_plain[KEY_SLASH + 1] = {
    [KEY_1] = "1", [KEY_2] = "2", [KEY_3] = "3", [KEY_4] = "4", [KEY_5] = "5", [KEY_6] = "6", [KEY_7] = "7",
    [KEY_8] = "8", [KEY_9] = "9", [KEY_0] = "0", [KEY_MINUS] = "-", [KEY_EQUAL] = "=", [KEY_Q] = "q",
    [KEY_W] = "w", [KEY_E] = "e", [KEY_R] = "r", [KEY_T] = "t", [KEY_Y] = "y", [KEY_U] = "u", [KEY_I] = "i",
    [KEY_O] = "o", [KEY_P] = "p", [KEY_LEFTBRACE] = "[", [KEY_RIGHTBRACE] = "]", [KEY_A] = "a", [KEY_S] = "s",
    [KEY_D] = "d", [KEY_F] = "f", [KEY_G] = "g", [KEY_H] = "h", [KEY_J] = "j", [KEY_K] = "k", [KEY_L] = "l",
    [KEY_SEMICOLON] = ";", [KEY_APOSTROPHE] = "'", [KEY_GRAVE] = "`", [KEY_BACKSLASH] = "\\", [KEY_Z] = "z",
    [KEY_X] = "x", [KEY_C] = "c", [KEY_V] = "v", [KEY_B] = "b", [KEY_N] = "n", [KEY_M] = "m", [KEY_COMMA] = ",",
    [KEY_DOT] = ".", [KEY_SLASH] = "/",
};
static const char *const us_shift[KEY_SLASH + 1] = {
    [KEY_1] = "!", [KEY_2] = "@", [KEY_3] = "#", [KEY_4] = "$", [KEY_5] = "%", [KEY_6] = "^", [KEY_7] = "&",
    [KEY_8] = "*", [KEY_9] = "(", [KEY_0] = ")", [KEY_MINUS] = "_", [KEY_EQUAL] = "+", [KEY_Q] = "Q",
    [KEY_W] = "W", [KEY_E] = "E", [KEY_R] = "R", [KEY_T] = "T", [KEY_Y] = "Y", [KEY_U] = "U", [KEY_I] = "I",
    [KEY_O] = "O", [KEY_P] = "P", [KEY_LEFTBRACE] = "{", [KEY_RIGHTBRACE] = "}", [KEY_A] = "A", [KEY_S] = "S",
    [KEY_D] = "D", [KEY_F] = "F", [KEY_G] = "G", [KEY_H] = "H", [KEY_J] = "J", [KEY_K] = "K", [KEY_L] = "L",
    [KEY_SEMICOLON] = ":", [KEY_APOSTROPHE] = "\"", [KEY_GRAVE] = "~", [KEY_BACKSLASH] = "|", [KEY_Z] = "Z",
    [KEY_X] = "X", [KEY_C] = "C", [KEY_V] = "V", [KEY_B] = "B", [KEY_N] = "N", [KEY_M] = "M", [KEY_COMMA] = "<",
    [KEY_DOT] = ">", [KEY_SLASH] = "?",
};

int rb_evdev_to_rbui_fallback(unsigned code, bool shift, char utf8[8])
{
    utf8[0] = 0;
    switch (code) {
    case KEY_TAB: return RBUI_KEY_TAB;
    case KEY_ENTER: return RBUI_KEY_RETURN;
    case KEY_KPENTER: return RBUI_KEY_KP_ENTER;
    case KEY_ESC: return RBUI_KEY_ESCAPE;
    case KEY_BACKSPACE: return RBUI_KEY_BACKSPACE;
    case KEY_DELETE: return RBUI_KEY_DELETE;
    case KEY_LEFT: return RBUI_KEY_LEFT;
    case KEY_RIGHT: return RBUI_KEY_RIGHT;
    case KEY_UP: return RBUI_KEY_UP;
    case KEY_DOWN: return RBUI_KEY_DOWN;
    case KEY_HOME: return RBUI_KEY_HOME;
    case KEY_END: return RBUI_KEY_END;
    case KEY_PAGEUP: return RBUI_KEY_PAGEUP;
    case KEY_PAGEDOWN: return RBUI_KEY_PAGEDOWN;
    case KEY_SPACE:
        strcpy(utf8, " ");
        return RBUI_KEY_SPACE;
    case KEY_KP0: strcpy(utf8, "0"); return RBUI_KEY_NONE;
    case KEY_KP1: strcpy(utf8, "1"); return RBUI_KEY_NONE;
    case KEY_KP2: strcpy(utf8, "2"); return RBUI_KEY_NONE;
    case KEY_KP3: strcpy(utf8, "3"); return RBUI_KEY_NONE;
    case KEY_KP4: strcpy(utf8, "4"); return RBUI_KEY_NONE;
    case KEY_KP5: strcpy(utf8, "5"); return RBUI_KEY_NONE;
    case KEY_KP6: strcpy(utf8, "6"); return RBUI_KEY_NONE;
    case KEY_KP7: strcpy(utf8, "7"); return RBUI_KEY_NONE;
    case KEY_KP8: strcpy(utf8, "8"); return RBUI_KEY_NONE;
    case KEY_KP9: strcpy(utf8, "9"); return RBUI_KEY_NONE;
    case KEY_KPDOT: strcpy(utf8, "."); return RBUI_KEY_NONE;
    case KEY_KPPLUS: strcpy(utf8, "+"); return RBUI_KEY_NONE;
    case KEY_KPMINUS: strcpy(utf8, "-"); return RBUI_KEY_NONE;
    case KEY_KPASTERISK: strcpy(utf8, "*"); return RBUI_KEY_NONE;
    case KEY_KPSLASH: strcpy(utf8, "/"); return RBUI_KEY_NONE;
    case KEY_KPEQUAL: strcpy(utf8, "="); return RBUI_KEY_NONE;
    case KEY_KPCOMMA: strcpy(utf8, ","); return RBUI_KEY_NONE;
    default: break;
    }
    if (code <= KEY_SLASH) {
        const char *s = shift ? us_shift[code] : us_plain[code];
        if (s) {
            strncpy(utf8, s, 7);
            utf8[7] = 0;
        }
    }
    return RBUI_KEY_NONE;
}
