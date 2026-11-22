"""Type text into Mac OS as keystrokes (the web Screen page's Paste).

Mac OS 9 has no shared clipboard with the browser, so pasted text is typed on
the U.S. keyboard layout: ASCII with Shift, Mac OS Roman symbols with Option
and Shift-Option, and accented letters with the Option dead keys (Option-E
then a vowel for an acute accent, and so on).  Line breaks become Return and
tabs Tab.  A character Mac OS Roman cannot hold is typed without its accent
when that makes it typeable (as the AirPort network names do), otherwise
skipped and counted.
"""
from __future__ import annotations

import unicodedata

from .keymap import EVDEV

# Unshifted and shifted characters of the U.S. layout, by evdev key name.
_ROWS = [
    ("GRAVE", "`", "~"), ("1", "1", "!"), ("2", "2", "@"), ("3", "3", "#"), ("4", "4", "$"), ("5", "5", "%"),
    ("6", "6", "^"), ("7", "7", "&"), ("8", "8", "*"), ("9", "9", "("), ("0", "0", ")"), ("MINUS", "-", "_"),
    ("EQUAL", "=", "+"), ("LEFTBRACE", "[", "{"), ("RIGHTBRACE", "]", "}"), ("BACKSLASH", "\\", "|"),
    ("SEMICOLON", ";", ":"), ("APOSTROPHE", "'", '"'), ("COMMA", ",", "<"), ("DOT", ".", ">"),
    ("SLASH", "/", "?"), ("SPACE", " ", None),
] + [(c, c.lower(), c) for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"]

# Option and Shift-Option characters of the Mac OS 8/9 U.S. layout.  None:
# a dead key (handled below) or nothing.
_OPTION = {
    "A": ("å", "Å"), "B": ("∫", "ı"), "C": ("ç", "Ç"), "D": ("∂", "Î"), "E": (None, "´"), "F": ("ƒ", "Ï"),
    "G": ("©", "˝"), "H": ("˙", "Ó"), "I": (None, "ˆ"), "J": ("∆", "Ô"), "K": ("˚", ""), "L": ("¬", "Ò"),
    "M": ("µ", "Â"), "N": (None, "˜"), "O": ("ø", "Ø"), "P": ("π", "∏"), "Q": ("œ", "Œ"), "R": ("®", "‰"),
    "S": ("ß", "Í"), "T": ("†", "ˇ"), "U": (None, "¨"), "V": ("√", "◊"), "W": ("∑", "„"), "X": ("≈", "˛"),
    "Y": ("¥", "Á"), "Z": ("Ω", "¸"),
    "1": ("¡", "⁄"), "2": ("™", "€"), "3": ("£", "‹"), "4": ("¢", "›"), "5": ("∞", "ﬁ"), "6": ("§", "ﬂ"),
    "7": ("¶", "‡"), "8": ("•", "°"), "9": ("ª", "·"), "0": ("º", "‚"), "MINUS": ("–", "—"), "EQUAL": ("≠", "±"),
    "LEFTBRACE": ("“", "”"), "RIGHTBRACE": ("‘", "’"), "BACKSLASH": ("«", "»"), "SEMICOLON": ("…", "Ú"),
    "APOSTROPHE": ("æ", "Æ"), "COMMA": ("≤", "¯"), "DOT": ("≥", "˘"), "SLASH": ("÷", "¿"), "GRAVE": (None, "`"),
    "SPACE": (" ", None),
}

# Dead keys: Option + key, then the base letter.
_DEAD = {
    "E": {"a": "á", "e": "é", "i": "í", "o": "ó", "u": "ú", "A": "Á", "E": "É", "I": "Í", "O": "Ó", "U": "Ú"},
    "GRAVE": {"a": "à", "e": "è", "i": "ì", "o": "ò", "u": "ù", "A": "À", "E": "È", "I": "Ì", "O": "Ò", "U": "Ù"},
    "I": {"a": "â", "e": "ê", "i": "î", "o": "ô", "u": "û", "A": "Â", "E": "Ê", "I": "Î", "O": "Ô", "U": "Û"},
    "U": {"a": "ä", "e": "ë", "i": "ï", "o": "ö", "u": "ü", "y": "ÿ",
          "A": "Ä", "E": "Ë", "I": "Ï", "O": "Ö", "U": "Ü", "Y": "Ÿ"},
    "N": {"a": "ã", "n": "ñ", "o": "õ", "A": "Ã", "N": "Ñ", "O": "Õ"},
}

# A stroke: (evdev code, shift, option).
Stroke = tuple[int, bool, bool]


def _build() -> dict[str, list[Stroke]]:
    table: dict[str, list[Stroke]] = {}
    for name, plain, shifted in _ROWS:
        code = EVDEV[name]
        table.setdefault(plain, [(code, False, False)])
        if shifted:
            table.setdefault(shifted, [(code, True, False)])
    for name, (opt, shift_opt) in _OPTION.items():
        code = EVDEV[name]
        if opt:
            table.setdefault(opt, [(code, False, True)])
        if shift_opt:
            table.setdefault(shift_opt, [(code, True, True)])
    for dead, letters in _DEAD.items():
        for base, out in letters.items():
            table.setdefault(out, [(EVDEV[dead], False, True)] + table[base])
    table["\n"] = [(EVDEV["ENTER"], False, False)]
    table["\t"] = [(EVDEV["TAB"], False, False)]
    return table


TABLE = _build()
# Common typographic characters outside Mac OS Roman, typed as their nearest neighbour.
_SUBST = {"−": "-", "‐": "-", "‑": "-", "′": "'", "″": '"', "­": "",
          "​": "", "﻿": "", " ": "\n", " ": "\n", " ": " ", " ": " "}

MAX_TEXT = 8000


def plan(text: str) -> tuple[list[Stroke], int]:
    """Strokes typing ``text`` and the number of characters that cannot be typed."""
    text = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    strokes: list[Stroke] = []
    skipped = 0
    for ch in text:
        ch = _SUBST.get(ch, ch)
        if not ch:
            continue
        seq = TABLE.get(ch)
        if seq is None:
            base = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
            if base and all(c in TABLE for c in base):
                seq = [s for c in base for s in TABLE[c]]
        if seq is None:
            skipped += 1
            continue
        strokes.extend(seq)
    return strokes, skipped
