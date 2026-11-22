"""Browser keyboard -> Linux evdev key codes for the web Screen page.

Browsers report physical keys as ``KeyboardEvent.code`` (W3C UI Events
"code" values, position based like evdev).  hostd maps them to evdev codes;
the presenter maps those to QEMU QKeyCodes with the same table it uses for the
local keyboard (presenter/src/keymap.c), and QEMU's ADB keyboard gives Mac OS
its Mac key codes (Meta = Command, Alt = Option, Insert = Help, NumLock =
Clear).  Only keys in this table ever reach the guest.

Command and Option.  A browser on a Mac (or an iPad keyboard) reports the
Command key as Meta and Option as Alt, so Command stays Command there.  On
other systems the ``input.swap_command_option`` setting applies, as on the
computer's own PC keyboard (Alt next to the space bar becomes Command); the
page may override that per browser.
"""
from __future__ import annotations

# linux/input-event-codes.h (stable ABI)
EVDEV = {
    "ESC": 1, "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9, "9": 10, "0": 11,
    "MINUS": 12, "EQUAL": 13, "BACKSPACE": 14, "TAB": 15,
    "Q": 16, "W": 17, "E": 18, "R": 19, "T": 20, "Y": 21, "U": 22, "I": 23, "O": 24, "P": 25,
    "LEFTBRACE": 26, "RIGHTBRACE": 27, "ENTER": 28, "LEFTCTRL": 29,
    "A": 30, "S": 31, "D": 32, "F": 33, "G": 34, "H": 35, "J": 36, "K": 37, "L": 38,
    "SEMICOLON": 39, "APOSTROPHE": 40, "GRAVE": 41, "LEFTSHIFT": 42, "BACKSLASH": 43,
    "Z": 44, "X": 45, "C": 46, "V": 47, "B": 48, "N": 49, "M": 50,
    "COMMA": 51, "DOT": 52, "SLASH": 53, "RIGHTSHIFT": 54, "KPASTERISK": 55, "LEFTALT": 56, "SPACE": 57,
    "CAPSLOCK": 58, "F1": 59, "F2": 60, "F3": 61, "F4": 62, "F5": 63, "F6": 64, "F7": 65, "F8": 66,
    "F9": 67, "F10": 68, "NUMLOCK": 69, "SCROLLLOCK": 70,
    "KP7": 71, "KP8": 72, "KP9": 73, "KPMINUS": 74, "KP4": 75, "KP5": 76, "KP6": 77, "KPPLUS": 78,
    "KP1": 79, "KP2": 80, "KP3": 81, "KP0": 82, "KPDOT": 83,
    "102ND": 86, "F11": 87, "F12": 88, "RO": 89,
    "KPENTER": 96, "RIGHTCTRL": 97, "KPSLASH": 98, "SYSRQ": 99, "RIGHTALT": 100,
    "HOME": 102, "UP": 103, "PAGEUP": 104, "LEFT": 105, "RIGHT": 106, "END": 107, "DOWN": 108,
    "PAGEDOWN": 109, "INSERT": 110, "DELETE": 111, "POWER": 116, "KPEQUAL": 117, "PAUSE": 119,
    "KPCOMMA": 121, "YEN": 124, "LEFTMETA": 125, "RIGHTMETA": 126, "COMPOSE": 127, "HELP": 138,
    "F13": 183, "F14": 184, "F15": 185, "F16": 186, "F17": 187, "F18": 188, "F19": 189, "F20": 190,
    "F21": 191, "F22": 192, "F23": 193, "F24": 194,
}

# KeyboardEvent.code -> evdev name.  Host keys (volume, brightness, media)
# are deliberately absent: they belong to the browser's own computer.
CODE_TO_NAME = {
    **{f"Key{c}": c for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"},
    **{f"Digit{d}": d for d in "0123456789"},
    **{f"F{n}": f"F{n}" for n in range(1, 25)},
    **{f"Numpad{d}": f"KP{d}" for d in "0123456789"},
    "Escape": "ESC", "Minus": "MINUS", "Equal": "EQUAL", "Backspace": "BACKSPACE", "Tab": "TAB",
    "BracketLeft": "LEFTBRACE", "BracketRight": "RIGHTBRACE", "Enter": "ENTER",
    "ControlLeft": "LEFTCTRL", "ControlRight": "RIGHTCTRL",
    "Semicolon": "SEMICOLON", "Quote": "APOSTROPHE", "Backquote": "GRAVE", "Backslash": "BACKSLASH",
    "ShiftLeft": "LEFTSHIFT", "ShiftRight": "RIGHTSHIFT",
    "Comma": "COMMA", "Period": "DOT", "Slash": "SLASH",
    "AltLeft": "LEFTALT", "AltRight": "RIGHTALT", "MetaLeft": "LEFTMETA", "MetaRight": "RIGHTMETA",
    "OSLeft": "LEFTMETA", "OSRight": "RIGHTMETA",           # older Firefox
    "Space": "SPACE", "CapsLock": "CAPSLOCK", "NumLock": "NUMLOCK", "ScrollLock": "SCROLLLOCK",
    "NumpadMultiply": "KPASTERISK", "NumpadSubtract": "KPMINUS", "NumpadAdd": "KPPLUS",
    "NumpadDecimal": "KPDOT", "NumpadEnter": "KPENTER", "NumpadDivide": "KPSLASH", "NumpadEqual": "KPEQUAL",
    "NumpadComma": "KPCOMMA", "IntlBackslash": "102ND", "IntlRo": "RO", "IntlYen": "YEN",
    "PrintScreen": "SYSRQ", "Pause": "PAUSE", "ContextMenu": "COMPOSE", "Help": "HELP",
    "Home": "HOME", "End": "END", "PageUp": "PAGEUP", "PageDown": "PAGEDOWN",
    "ArrowUp": "UP", "ArrowDown": "DOWN", "ArrowLeft": "LEFT", "ArrowRight": "RIGHT",
    "Insert": "INSERT", "Delete": "DELETE",
}
CODE_TO_EVDEV = {code: EVDEV[name] for code, name in CODE_TO_NAME.items()}

MODIFIERS = {EVDEV[n] for n in ("LEFTSHIFT", "RIGHTSHIFT", "LEFTCTRL", "RIGHTCTRL", "LEFTALT", "RIGHTALT",
                                "LEFTMETA", "RIGHTMETA")}
SWAP = {EVDEV["LEFTALT"]: EVDEV["LEFTMETA"], EVDEV["LEFTMETA"]: EVDEV["LEFTALT"],
        EVDEV["RIGHTALT"]: EVDEV["RIGHTMETA"], EVDEV["RIGHTMETA"]: EVDEV["RIGHTALT"]}
# Every evdev code a viewer may hold (the table plus the Power key, which only
# the key bar sends).
ALLOWED = frozenset(CODE_TO_EVDEV.values()) | {EVDEV["POWER"]}

MAC_PLATFORMS = ("mac", "ios")


def swap_for(platform: str | None, setting: bool, override: bool | None = None) -> bool:
    """Whether Alt and Meta trade places for a browser on ``platform``."""
    if isinstance(override, bool):
        return override
    if (platform or "").lower() in MAC_PLATFORMS:
        return False
    return bool(setting)


def to_evdev(code: str, swap: bool = False) -> int | None:
    """The evdev code for a KeyboardEvent.code, or None for keys that never reach Mac OS."""
    ev = CODE_TO_EVDEV.get(code) if isinstance(code, str) else None
    if ev is None:
        return None
    return SWAP.get(ev, ev) if swap else ev


# Key bar combinations: pressed in order, released in reverse.  Command is
# always Meta here (the swap is a keyboard-position convenience; the bar names
# Mac OS keys).  "recovery" uses the configured recovery chord.
COMBOS = {
    "cmd-q": ("Command-Q", ("LEFTMETA", "Q")),
    "cmd-w": ("Command-W", ("LEFTMETA", "W")),
    "cmd-tab": ("Command-Tab", ("LEFTMETA", "TAB")),
    "cmd-period": ("Command-Period", ("LEFTMETA", "DOT")),
    "force-quit": ("Command-Option-Escape", ("LEFTMETA", "LEFTALT", "ESC")),
    "esc": ("Escape", ("ESC",)),
    "power": ("Power key (Shut Down…)", ("POWER",)),
    "recovery": ("RetroBoot Recovery", None),
}
CHORD_KEYS = {"ctrl+alt+delete": "DELETE", "ctrl+alt+backspace": "BACKSPACE", "ctrl+alt+escape": "ESC"}


def combo_keys(name: str, chord: str = "ctrl+alt+delete") -> list[int] | None:
    """Evdev codes for a key bar combination (None if unknown)."""
    entry = COMBOS.get(name)
    if entry is None:
        return None
    names = entry[1]
    if names is None:   # the recovery chord, never forwarded to Mac OS by the presenter
        names = ("LEFTCTRL", "LEFTALT", CHORD_KEYS.get(chord, "DELETE"))
    return [EVDEV[n] for n in names]


def combo_list() -> list[dict]:
    return [{"name": k, "label": v[0]} for k, v in COMBOS.items()]
