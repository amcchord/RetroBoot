"""Text encodings between today's web (UTF-8) and Mac OS 9 browsers.

Output: pages are sent in Mac OS Roman (``x-mac-roman``, the default) or
ISO Latin-1.  A character the target cannot hold is transliterated (dashes,
quotes, arrows, odd spaces), reduced to its base letter (``ł`` → ``l``),
dropped (emoji, zero-width characters) or, for letters of other scripts,
written as a numeric character reference so a browser with the right fonts
can still show it.

Input: form submissions from those pages arrive in the page encoding.  Query
strings and urlencoded bodies are converted back to UTF-8 unless they are
already valid UTF-8 (links in pages carry UTF-8 percent-escapes).
"""
from __future__ import annotations

import codecs
import re
import unicodedata
from urllib.parse import quote_from_bytes, unquote_to_bytes

# name -> (Python codec, label sent to the browser)
CHARSETS = {
    "macroman": ("mac_roman", "x-mac-roman"),
    "latin1": ("latin-1", "iso-8859-1"),
}

_TRANSLIT = {
    "\u2010": "-", "\u2011": "-", "\u2012": "\u2013", "\u2015": "\u2014", "\u2212": "-",
    "\u201b": "\u2018", "\u201f": "\u201c", "\u2032": "'", "\u2033": "\"", "\u2035": "`",
    "\u2190": "<-", "\u2192": "->", "\u2191": "^", "\u2193": "v", "\u2194": "<->", "\u21d0": "<=",
    "\u21d2": "=>", "\u21d4": "<=>", "\u2794": "->", "\u279c": "->", "\u00d7": "x", "\u2715": "x",
    "\u2716": "x", "\u2713": "v", "\u2714": "v", "\u2026": "...", "\u2022": "*", "\u25cf": "*",
    "\u25aa": "*", "\u25b6": ">", "\u25c0": "<", "\u2605": "*", "\u2606": "*", "\u2039": "<",
    "\u203a": ">", "\u00ab": "<<", "\u00bb": ">>", "\u2018": "'", "\u2019": "'", "\u201a": ",",
    "\u201c": "\"", "\u201d": "\"", "\u201e": "\"", "\u2013": "-", "\u2014": "--", "\u20ac": "EUR",
    "\u2122": "(TM)", "\u00a9": "(C)", "\u00ae": "(R)", "\u2264": "<=", "\u2265": ">=",
    "\u2260": "!=", "\u221e": "oo", "\u00b7": ".", "\u2027": ".", "\u2043": "-", "\u2116": "No.",
    "\u00bd": "1/2", "\u00bc": "1/4", "\u00be": "3/4", "\u2153": "1/3", "\u2154": "2/3",
    "\u00b1": "+/-", "\u2248": "~", "\u02dc": "~", "\u02c6": "^", "\u0192": "f", "\u2030": "o/oo",
    "\u00ad": "", "\ufeff": "", "\u200b": "", "\u200c": "", "\u200d": "", "\u2060": "",
    "\u200e": "", "\u200f": "", "\u202a": "", "\u202b": "", "\u202c": "", "\u202d": "",
    "\u202e": "", "\u2061": "", "\u2062": "", "\u2063": "", "\u2064": "", "\u0142": "l",
    "\u0141": "L", "\u0111": "d", "\u0110": "D", "\u0131": "i", "\u0127": "h", "\u0126": "H",
    "\u014b": "n", "\u014a": "N", "\u00fe": "th", "\u00de": "Th", "\u00f0": "d", "\u00d0": "D",
    "\u0153": "oe", "\u0152": "OE", "\u00e6": "ae", "\u00c6": "AE", "\u00df": "ss", "\u00f8": "o",
    "\u00d8": "O",
}


def _translit(ch: str, codec: str) -> str:
    """Best replacement for one character the target codec cannot encode."""
    rep = _TRANSLIT.get(ch)
    if rep is not None:
        try:
            rep.encode(codec)
            return rep
        except UnicodeEncodeError:
            # e.g. a MacRoman-only replacement while sending Latin-1: retry on its parts
            return "".join(_translit_simple(c, codec) for c in rep)
    return _translit_simple(ch, codec)


def _translit_simple(ch: str, codec: str) -> str:
    try:
        ch.encode(codec)
        return ch
    except UnicodeEncodeError:
        pass
    rep = _TRANSLIT.get(ch)
    if rep is not None and rep != ch:
        try:
            rep.encode(codec)
            return rep
        except UnicodeEncodeError:
            pass
    cp = ord(ch)
    cat = unicodedata.category(ch)
    if 0xFE00 <= cp <= 0xFE0F or 0xE0100 <= cp <= 0xE01EF or cp in (0x20E3,):
        return ""                                  # variation selectors, keycap
    if cat.startswith("Z"):
        return " "
    if cat in ("Cf", "Cc", "Co", "Cs", "Cn", "Mn", "Me", "Mc"):
        return ""
    base = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
    if base and base != ch:
        try:
            base.encode(codec)
            return base
        except UnicodeEncodeError:
            pass
    if cat.startswith("S"):
        return ""                                  # emoji, pictographs, dingbats
    if cat.startswith("P"):
        return "?"
    return f"&#{cp};"


def _make_handler(codec: str):
    # Charmap codecs report their encoding as "charmap", so each target codec gets its
    # own registered handler.
    def handler(exc: UnicodeError):
        if not isinstance(exc, UnicodeEncodeError):
            raise exc
        return "".join(_translit(c, codec) for c in exc.object[exc.start:exc.end]), exc.end
    return handler


for _codec, _ in CHARSETS.values():
    codecs.register_error(f"rbwebproxy-{_codec}", _make_handler(_codec))


def codec_for(name: str) -> tuple[str, str]:
    return CHARSETS.get(name, CHARSETS["macroman"])


def encode_text(text: str, charset: str = "macroman") -> bytes:
    """Encode for the old browser (references for what the charset cannot hold)."""
    codec, _ = codec_for(charset)
    return text.encode(codec, errors=f"rbwebproxy-{codec}")


def encode_plain(text: str, charset: str = "macroman") -> bytes:
    """Like encode_text for plain text (no markup: references would show literally)."""
    out = encode_text(text, charset)
    return re.sub(rb"&#(\d+);", b"?", out)


# -- input decoding -----------------------------------------------------------------

_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_:.\-]+)""", re.I)
_XML_DECL = re.compile(rb"""^\s*<\?xml[^>]*encoding\s*=\s*["']([A-Za-z0-9_.\-]+)""", re.I)


def _known(name: str | None) -> str | None:
    if not name:
        return None
    name = name.strip().strip("\"'").lower()
    aliases = {"x-mac-roman": "mac_roman", "macintosh": "mac_roman", "mac": "mac_roman",
               "iso-8859-1": "cp1252", "latin1": "cp1252", "us-ascii": "cp1252", "ascii": "cp1252",
               "unicode": "utf-8", "utf8": "utf-8", "x-sjis": "shift_jis"}
    name = aliases.get(name, name)
    try:
        return codecs.lookup(name).name
    except LookupError:
        return None


def detect(body: bytes, content_type_charset: str | None) -> str:
    """The encoding of an HTML or text document (WHATWG order, simplified)."""
    if body.startswith(codecs.BOM_UTF8):
        return "utf-8"
    if body.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    cs = _known(content_type_charset)
    if cs:
        return cs
    head = body[:4096]
    m = _XML_DECL.search(head) or _META_CHARSET.search(head)
    if m:
        cs = _known(m.group(1).decode("ascii", "replace"))
        if cs and not cs.startswith("utf-16"):
            return cs
    try:
        body.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "cp1252"


def decode(body: bytes, content_type_charset: str | None) -> str:
    enc = detect(body, content_type_charset)
    if enc == "utf-8" and body.startswith(codecs.BOM_UTF8):
        body = body[3:]
    return body.decode(enc, errors="replace")


# -- form submissions ----------------------------------------------------------------

def _transcode_component(raw: bytes, codec: str) -> bytes:
    """One percent-decoded value: legacy bytes -> UTF-8 (UTF-8 input is kept)."""
    if raw.isascii():
        return raw
    try:
        raw.decode("utf-8")
        return raw
    except UnicodeDecodeError:
        return raw.decode(codec, errors="replace").encode("utf-8")


def transcode_form(data: str | bytes, charset: str = "macroman") -> str:
    """An application/x-www-form-urlencoded string (query or body) to UTF-8."""
    codec, _ = codec_for(charset)
    if isinstance(data, bytes):
        data = data.decode("latin-1")          # byte-transparent
    if "%" not in data and data.isascii():
        return data
    out = []
    for pair in data.split("&"):
        conv = []
        for p in pair.split("=", 1):
            if "%" not in p and p.isascii():
                conv.append(p)
                continue
            raw = unquote_to_bytes(p.encode("latin-1").replace(b"+", b" "))
            new = _transcode_component(raw, codec)
            if new == raw and p.isascii():
                conv.append(p)                 # ASCII or already UTF-8: unchanged
                continue
            conv.append(quote_from_bytes(new, safe="").replace("%20", "+"))
        out.append("=".join(conv))
    return "&".join(out)
