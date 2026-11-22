"""Classic Mac file formats for the shared volume (``share.py``).

classicvirtio (ClassicMac's 9P driver; ``multifork-3.c`` at the pinned
revision fc401b4) keeps every file of the shared volume as up to three host
files::

    NAME          the data fork
    NAME.rdump    the resource fork as Rez source (DeRez output); only when it is not empty
    NAME.idump    Finder info: type and creator (8 bytes, "????" for zero), a newline,
                  then one Finder flag name per line ("kHasBundle", "kColor3", ...)

A folder's Finder flags (kIsInvisible, its label, kNameLocked, kHasCustomIcon...)
live the same way beside it, ``FOLDER.idump`` with "????????" for type and creator,
while one of them is set (RetroBoot's classicvirtio-5 and later; earlier drivers kept no
folder Finder info).  Host names are UTF-8.  Mac OS sees each
name in Mac OS Roman, at most 31 bytes, with a host ':' shown as '/'
(``unicode.c`` mrname/utf8name).  RetroBoot's classicvirtio-6 and later show
every other name too, shortened with '#' and the item's CNID in hex before a
short extension, as Mac OS shows long HFS Plus names: a name longer than 31
characters, one with characters Mac OS Roman lacks, and a precomposed name
whose decomposed spelling is also there (``catalog.c`` CatalogMacName,
``shown_name`` here).  Names that start with '.', the sidecars and names
longer than 93 bytes stay invisible in Mac OS.  The guest writes names
decomposed (NFD) and compares them ignoring ASCII case.

This module converts between those files and what people drop in a browser:
MacBinary I/II/III, BinHex 4.0 and AppleSingle/AppleDouble on the way in,
MacBinary III on the way out, plus Internet Config style type and creator
codes for plain files and optional text conversion.  Standard library only
(the web client's mock API reuses it).
"""
from __future__ import annotations

import base64
import binascii
import codecs
import io
import os
import re
import struct
import time
import unicodedata
from dataclasses import dataclass, field
from typing import BinaryIO, Callable, Iterator

MAX_NAME = 31                       # HFS name limit, Mac OS Roman bytes
MAX_HOST_NAME = 93                  # classicvirtio's MAXNAME (94) less the terminating zero: longer is invisible
MAX_RSRC = 16 * 1024 * 1024         # Resource Manager limit (MacBinary stores 24 bits)
MAX_RESOURCES = 2700                # classicvirtio's Rez() holds 2727 resources...
MAX_NAME_LIST = 0x7000              # ...and a 32 KB name list; more would crash the guest driver
MAC_EPOCH = 2082844800              # seconds from 1904-01-01 to 1970-01-01
SIDECARS = (".rdump", ".idump")


class FormatError(Exception):
    """A dropped file claims a format but its contents don't verify."""


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

def _tables():
    nfd, high = {}, {}
    for c in range(0x80, 0x100):
        u = bytes([c]).decode("mac_roman")
        high[u.encode()] = c
        d = unicodedata.normalize("NFD", u)
        if d != u:
            nfd[d.encode()] = c
    return nfd, high


# classicvirtio's toMacRoman() accepts, at each position, a decomposed letter
# (ASCII + one combining mark), then plain ASCII, then the composed UTF-8 of
# any Mac OS Roman character.  Its utf8char() writes the decomposed (NFD) form,
# which is exactly NFD of Python's mac_roman codec (checked in test_share.py).
_NFD3, _HIGH = _tables()


def _convert(host: str, lossy: bool) -> tuple[bytes | None, bool]:
    """classicvirtio's toMacRoman() over a whole host name: (Mac OS Roman bytes, whether a
    decomposed letter was read).  A character Mac OS Roman lacks (its whole UTF-8 sequence)
    is '_' when ``lossy`` (unicode.c mrlossy), else the result is None."""
    b = host.encode("utf-8", "surrogateescape")
    out = bytearray()
    decomposed = False
    i, n = 0, len(b)
    while i < n:
        if i + 3 <= n and b[i:i + 3] in _NFD3:
            out.append(_NFD3[b[i:i + 3]])
            i += 3
            decomposed = True
        elif b[i] < 0x80:
            out.append(0x2F if b[i] == 0x3A else b[i])     # host ':' is Mac '/'
            i += 1
        elif b[i:i + 2] in _HIGH:
            out.append(_HIGH[b[i:i + 2]])
            i += 2
        elif b[i:i + 3] in _HIGH:
            out.append(_HIGH[b[i:i + 3]])
            i += 3
        elif lossy:
            out.append(0x5F)
            i += 1
            while i < n and b[i] & 0xC0 == 0x80:
                i += 1
        else:
            return None, decomposed
    return bytes(out), decomposed


def mac_bytes(host: str) -> bytes | None:
    """The Mac OS Roman spelling of a host file name, or None when it has none of at most
    31 bytes (unrepresentable, empty or longer).  ``shown_name`` is what Mac OS shows."""
    mac, _ = _convert(host, False)
    return mac if mac and len(mac) <= MAX_NAME else None


def shown_name(host: str, cnid: int, decomposed_exists: Callable[[str], bool] | None = None) -> bytes | None:
    """The name Mac OS shows for a host file or folder through classicvirtio-6
    (catalog.c CatalogMacName), or None when it isn't shown (longer than 93 bytes).

    A name with a Mac OS Roman spelling of at most 31 characters is shown as it is when
    it is written decomposed, as the guest writes names, or precomposed while no
    decomposed sibling of that name exists (``decomposed_exists``).  Any other name is
    shortened, '#' and the CNID's low 30 bits in upper-case hex before an extension of
    1-5 characters: "A rather long report#58EB67.txt"."""
    if len(host.encode("utf-8", "surrogateescape")) > MAX_HOST_NAME or not host:
        return None
    mac, decomposed = _convert(host, False)
    if mac and len(mac) <= MAX_NAME:
        canon = host_name(mac)
        if canon == host:
            return mac
        if not decomposed and not (decomposed_exists and decomposed_exists(canon)):
            return mac
    lossy = _convert(host, True)[0][:255]
    suffix = b"#%X" % (cnid & 0x3FFFFFFF)
    stem, dot, ext = lossy.rpartition(b".")
    if dot and stem and 1 <= len(ext) <= 5:
        ext = b"." + ext
    else:
        stem, ext = lossy, b""
    return stem[:MAX_NAME - len(suffix) - len(ext)] + suffix + ext


def host_name(mac: bytes) -> str:
    """Host file name for a Mac OS Roman name, written the way the guest writes it."""
    s = unicodedata.normalize("NFD", bytes(mac).decode("mac_roman"))
    return s.translate({0x2F: 0x3A, 0x3A: 0x2F})


def display(mac: bytes) -> str:
    return bytes(mac).decode("mac_roman")


def fold(mac: bytes) -> str:
    """Comparison key: Mac OS treats names that differ only in case as the same file."""
    return unicodedata.normalize("NFC", display(mac)).casefold()


def is_sidecar(host: str) -> bool:
    return host.endswith(SIDECARS) or host.endswith(".rdump.tmp")


def visible(host: str) -> bool:
    """Would Mac OS list this host name at all (classicvirtio-6: under ``shown_name``)?"""
    return (not host.startswith(".") and not is_sidecar(host)
            and 0 < len(host.encode("utf-8", "surrogateescape")) <= MAX_HOST_NAME)


_PUNCT = str.maketrans({"\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2015": "-", "\u2032": "'", "\u2033": '"',
                        "\u00a0": " ", "\u2009": " ", "\u202f": " ", "\u2007": " ", "\u200b": None, "\ufeff": None})


def to_mac(name: str) -> bytes:
    """Mac OS Roman bytes for a name from a browser or an archive (no length limit yet).

    Characters Mac OS Roman lacks lose their accents where that helps (ł stays
    unknown, ā becomes a) and otherwise become '_'.  A ':' becomes '/', which is
    how Mac OS shows the same host name.  Control characters become '_'."""
    s = unicodedata.normalize("NFC", name).translate(_PUNCT)
    out = bytearray()
    for ch in s:
        if ch == ":":
            out.append(0x2F)
            continue
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(0x5F)
            continue
        try:
            out += ch.encode("mac_roman")
            continue
        except UnicodeEncodeError:
            pass
        base = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
        try:
            out += base.encode("mac_roman") if base else b"_"
        except UnicodeEncodeError:
            out.append(0x5F)
    return bytes(out)


def fit(mac: bytes, limit: int = MAX_NAME, suffix: bytes = b"") -> bytes:
    """Shorten a name to ``limit`` bytes including ``suffix``, keeping a short extension:
    "A very long holiday photograph.jpg" -> "A very long holiday photo….jpg"."""
    room = limit - len(suffix)
    if len(mac) <= room:
        return mac + suffix
    stem, dot, ext = mac.rpartition(b".")
    if dot and stem and 1 <= len(ext) <= 5 and room > len(ext) + 2:
        return stem[:room - len(ext) - 2] + b"\xc9." + ext + suffix     # 0xC9 is the ellipsis
    return mac[:room - 1] + b"\xc9" + suffix


def safe_mac(name: str, fallback: str = "untitled") -> bytes:
    """A name from outside (browser, archive header) made into a usable Mac name:
    Mac OS Roman, visible on the share (no leading '.'), not a sidecar, at most 31 bytes."""
    mac = to_mac(name).strip(b" ") or to_mac(fallback)
    if mac.startswith(b"."):
        mac = b"_" + mac[1:]
    for sc in (b".rdump.tmp", b".rdump", b".idump"):
        if mac.endswith(sc):
            mac = mac[:-len(sc)] + b"_" + sc[1:]
    return fit(mac)


def safe_mac_bytes(mac: bytes) -> bytes:
    """A Mac name from inside an encoding (MacBinary, BinHex, AppleSingle) kept byte for
    byte where the share allows it: no ':' or leading '.', no sidecar suffix, 31 bytes."""
    mac = bytes(mac).replace(b":", b"-") or b"untitled"
    if mac.startswith(b"."):
        mac = b"_" + mac[1:]
    for sc in (b".rdump.tmp", b".rdump", b".idump"):
        if mac.endswith(sc):
            mac = mac[:-len(sc)] + b"_" + sc[1:]
    return fit(mac)


def copy_name(mac: bytes, n: int) -> bytes:
    """The Finder's names for a second item: "Name copy", "Name copy 2", ..."""
    return fit(mac, suffix=b" copy" if n == 1 else b" copy %d" % n)


# ---------------------------------------------------------------------------
# Type and creator for plain files (Internet Config style)
# ---------------------------------------------------------------------------

# extension -> (type, creator, kind, icon).  Creators name the classic application
# Internet Config 2.0 sends each kind of file to; Mac OS asks File Exchange for a
# substitute (SimpleText for text and PICT) when that application isn't installed.
EXTENSIONS: dict[str, tuple[bytes, bytes, str, str]] = {}


def _ext(names: str, type_: bytes, creator: bytes, kind: str, icon: str) -> None:
    for n in names.split():
        EXTENSIONS[n] = (type_, creator, kind, icon)


_ext("txt text log nfo diz 1st me ini cfg conf csv tsv md markdown json yaml yml js c h cc cpp hpp m r p pas "
     "py pl rb sh bas asm s tex bib srt sub", b"TEXT", b"ttxt", "SimpleText text document", "text")
_ext("html htm shtml xhtml xml css", b"TEXT", b"MSIE", "Web page", "text")
_ext("rtf", b"TEXT", b"MSWD", "RTF document", "text")
_ext("jpg jpeg jpe jfif", b"JPEG", b"ogle", "JPEG picture", "picture")
_ext("gif", b"GIFf", b"ogle", "GIF picture", "picture")
_ext("png", b"PNGf", b"ogle", "PNG picture", "picture")
_ext("bmp", b"BMPf", b"ogle", "BMP picture", "picture")
_ext("tif tiff", b"TIFF", b"ogle", "TIFF picture", "picture")
_ext("pict pct pic", b"PICT", b"ttxt", "PICT picture", "picture")
_ext("psd", b"8BPS", b"8BIM", "Photoshop document", "picture")
_ext("pdf", b"PDF ", b"CARO", "PDF document", "document")
_ext("ps eps", b"TEXT", b"ttxt", "PostScript file", "text")
_ext("mp3", b"MPG3", b"TVOD", "MP3 audio", "sound")
_ext("aif aiff", b"AIFF", b"TVOD", "AIFF sound", "sound")
_ext("aifc", b"AIFC", b"TVOD", "AIFF-C sound", "sound")
_ext("wav", b"WAVE", b"TVOD", "WAVE sound", "sound")
_ext("au snd", b"ULAW", b"TVOD", "μ-law sound", "sound")
_ext("mid midi", b"Midi", b"TVOD", "MIDI file", "sound")
_ext("mov qt", b"MooV", b"TVOD", "QuickTime movie", "movie")
_ext("mp4 m4v", b"mpg4", b"TVOD", "MPEG-4 movie", "movie")
_ext("mpg mpeg", b"MPEG", b"TVOD", "MPEG movie", "movie")
_ext("avi", b"VfW ", b"TVOD", "AVI movie", "movie")
_ext("sit", b"SIT!", b"SITx", "StuffIt archive", "archive")
_ext("sea", b"SIT!", b"SITx", "StuffIt self-extracting archive", "archive")
_ext("sitx", b"SITX", b"SITx", "StuffIt X archive", "archive")
_ext("cpt", b"PACT", b"CPCT", "Compact Pro archive", "archive")
_ext("hqx", b"TEXT", b"SITx", "BinHex file", "archive")
_ext("bin", b"BINA", b"SITx", "MacBinary file", "archive")
_ext("zip", b"ZIP ", b"SITx", "Zip archive", "archive")
_ext("gz tgz", b"Gzip", b"SITx", "gzip archive", "archive")
_ext("tar", b"TARF", b"SITx", "tar archive", "archive")
_ext("img dsk image", b"rohd", b"ddsk", "disk image", "disk")
_ext("dmg smi", b"dimg", b"ddsk", "disk image", "disk")
# Virtual CD/DVD-ROM Utility's original FREF advertises GImg (creator VCDM).
# Plain disc uploads therefore open that utility on double-click; copy them
# onto the Mac disk before mounting. Explicit wrapped metadata still wins.
_ext("iso cdr toast", b"GImg", b"VCDM", "CD image", "cd")
_ext("doc", b"W8BN", b"MSWD", "Word document", "document")
_ext("xls", b"XLS8", b"XCEL", "Excel document", "document")
_ext("ppt", b"SLD8", b"PPT3", "PowerPoint document", "document")

# Finder kinds by type code (when the extension doesn't decide; e.g. files from Mac OS).
TYPE_KINDS = {b"APPL": ("application program", "application"), b"APPC": ("control panel", "application"),
              b"APPD": ("desk accessory", "application"), b"cdev": ("control panel", "application"),
              b"INIT": ("system extension", "document"), b"TEXT": ("text document", "text"),
              b"ttro": ("SimpleText read-only document", "text"), b"PICT": ("PICT picture", "picture"),
              b"JPEG": ("JPEG picture", "picture"), b"GIFf": ("GIF picture", "picture"),
              b"SIT!": ("StuffIt archive", "archive"), b"SITD": ("StuffIt archive", "archive"),
              b"SIT5": ("StuffIt archive", "archive"), b"dImg": ("disk image", "disk"),
              b"rohd": ("disk image", "disk"), b"dimg": ("disk image", "disk"), b"MooV": ("QuickTime movie", "movie"),
              b"AIFF": ("AIFF sound", "sound"), b"sfil": ("sound", "sound"), b"PDF ": ("PDF document", "document"),
              b"FFIL": ("font suitcase", "document"), b"pref": ("preferences file", "document")}


def extension(name: str) -> str:
    stem, dot, ext = name.rpartition(".")
    return ext.lower() if dot and stem and 1 <= len(ext) <= 8 else ""


def codes_for(name: str) -> tuple[bytes, bytes] | None:
    """Type and creator for a plain file from its name, or None (no Finder info)."""
    e = EXTENSIONS.get(extension(name))
    return (e[0], e[1]) if e else None


def kind_for(name: str, type_: bytes, creator: bytes) -> tuple[str, str]:
    """(Finder kind, icon name) for a listing row."""
    if type_ == b"TEXT" and creator == b"ttxt":
        return "SimpleText text document", "text"
    e = EXTENSIONS.get(extension(name))
    if e and (e[0] == type_ or not any(type_)):
        return e[2], e[3]                      # the extension is more specific (Web page, MP3 audio)
    if type_ in TYPE_KINDS:
        return TYPE_KINDS[type_]
    return "document", "document"


def is_text_name(name: str) -> bool:
    e = EXTENSIONS.get(extension(name))
    return bool(e) and e[0] == b"TEXT" and extension(name) not in ("hqx", "ps", "eps")


# ---------------------------------------------------------------------------
# Finder info sidecar (.idump)
# ---------------------------------------------------------------------------

FLAG_NAMES = [(0x000E, "kColor7"), (0x0006, "kColor3"), (0x000A, "kColor5"), (0x000C, "kColor6"),
              (0x0002, "kColor1"), (0x0004, "kColor2"), (0x0008, "kColor4"), (0x0040, "kIsShared"),
              (0x0080, "kHasNoINITs"), (0x0100, "kHasBeenInited"), (0x0400, "kHasCustomIcon"),
              (0x0800, "kIsStationery"), (0x1000, "kNameLocked"), (0x2000, "kHasBundle"),
              (0x4000, "kIsInvisible"), (0x8000, "kIsAlias")]
FLAG_HAS_BEEN_INITED = 0x0100
FLAG_IS_ON_DESK = 0x0001


def idump(type_: bytes, creator: bytes, flags: int) -> bytes:
    """classicvirtio flagsToText(): what the guest itself writes."""
    t = (type_ if any(type_) else b"????") + (creator if any(creator) else b"????")
    lo, hi = flags & 0xFF, (flags >> 8) & 0xFF
    lines = [t]
    if lo & 0x0E:
        lines.append(b"kColor%d" % ((lo >> 1) & 7))
    for bit, name in ((0x40, b"kIsShared"), (0x80, b"kHasNoINITs")):
        if lo & bit:
            lines.append(name)
    for bit, name in ((0x01, b"kHasBeenInited"), (0x02, b"aoce-letter"), (0x04, b"kHasCustomIcon"),
                      (0x08, b"kIsStationery"), (0x10, b"kNameLocked"), (0x20, b"kHasBundle"),
                      (0x40, b"kIsInvisible"), (0x80, b"kIsAlias")):
        if hi & bit:
            lines.append(name)
    return b"\n".join(lines) + b"\n"


def parse_idump(data: bytes) -> tuple[bytes, bytes, int]:
    """classicvirtio textToFlags(): (type, creator, flags)."""
    if len(data) < 8:
        return bytes(4), bytes(4), 0
    t, c = data[:4], data[4:8]
    t = bytes(4) if t == b"????" else t
    c = bytes(4) if c == b"????" else c
    names = {n.encode(): v for v, n in FLAG_NAMES}
    flags = 0
    for line in data[9:].split(b"\n"):
        flags |= names.get(line, 0)
    return t, c, flags & 0xFFFF


# ---------------------------------------------------------------------------
# Resource forks, Rez text (.rdump)
# ---------------------------------------------------------------------------

@dataclass
class Resource:
    type: bytes
    id: int
    name: bytes | None
    attrs: int
    data: bytes


def parse_fork(fork: bytes) -> list[Resource]:
    """Resources of a binary resource fork, in map order."""
    if not fork:
        return []
    if len(fork) < 16:
        raise FormatError("resource fork header is too short")
    doff, moff, dlen, mlen = struct.unpack(">IIII", fork[:16])
    if moff + mlen > len(fork) or doff + dlen > len(fork) or mlen < 30:
        raise FormatError("resource fork header points outside the fork")
    rmap = fork[moff:moff + mlen]
    tl, nl = struct.unpack(">HH", rmap[24:28])
    if tl + 2 > len(rmap) or nl > len(rmap):
        raise FormatError("resource map offsets are out of range")
    ntypes = (struct.unpack(">H", rmap[tl:tl + 2])[0] + 1) & 0xFFFF
    out = []
    for i in range(ntypes):
        e = rmap[tl + 2 + 8 * i:tl + 10 + 8 * i]
        if len(e) < 8:
            raise FormatError("resource type list is truncated")
        rtype, count, ref = struct.unpack(">4sHH", e)
        for j in range(count + 1):
            r = rmap[tl + ref + 12 * j:tl + ref + 12 * j + 12]
            if len(r) < 12:
                raise FormatError("resource reference list is truncated")
            rid, noff, attr_off = struct.unpack(">hHI", r[:8])
            at = doff + (attr_off & 0xFFFFFF)
            if at + 4 > len(fork):
                raise FormatError("resource data is out of range")
            length = struct.unpack(">I", fork[at:at + 4])[0]
            if at + 4 + length > len(fork):
                raise FormatError("resource data is out of range")
            name = None
            if noff != 0xFFFF:
                p = nl + noff
                if p >= len(rmap) or p + 1 + rmap[p] > len(rmap):
                    raise FormatError("resource name is out of range")
                name = rmap[p + 1:p + 1 + rmap[p]]
            out.append(Resource(rtype, rid, name, attr_off >> 24, fork[at + 4:at + 4 + length]))
    return out


def build_fork(resources: list[Resource]) -> bytes:
    """A resource fork laid out exactly as classicvirtio's Rez() builds it."""
    if not resources:
        return b""
    data = bytearray(256)
    refs = []
    names = bytearray()
    for r in resources:
        off = len(data) - 256
        data += struct.pack(">I", len(r.data)) + r.data
        data += bytes(-len(data) % 4)
        noff = 0xFFFF
        if r.name is not None:
            noff = len(names)
            names += bytes([len(r.name)]) + r.name
        refs.append((r.type, r.id, noff, ((r.attrs & 0xFF) << 24) | off))
    content = len(data) - 256
    refs.sort(key=lambda x: (x[0], x[1]))
    types: list[tuple[bytes, int]] = []
    for t, *_ in refs:
        if types and types[-1][0] == t:
            types[-1] = (t, types[-1][1] + 1)
        else:
            types.append((t, 1))
    ntype, nres = len(types), len(refs)
    m = bytearray(25) + bytes([28]) + struct.pack(">HH", 28 + 2 + 8 * ntype + 12 * nres, (ntype - 1) & 0xFFFF)
    base = 2 + 8 * ntype
    for t, n in types:
        m += t + struct.pack(">HH", n - 1, base)
        base += 12 * n
    for t, rid, noff, ao in refs:
        m += struct.pack(">hHI4x", rid, noff, ao)
    m += names
    head = struct.pack(">IIII", 256, 256 + content, content, len(m))
    return head + bytes(data[16:]) + bytes(m)


_CMT = bytearray(b"." * 32 + bytes(range(32, 127)) + b"." + b"." * 128)
_HEX = b"0123456789ABCDEF"


def _quoted(raw: bytes, mark: int) -> bytes:
    # classicvirtio's Rez reads "\0xHH" escapes wrongly (both digits through its
    # high-nibble table), so bytes go in raw; only the quote mark and '\' are escaped.
    out = bytearray()
    for c in raw:
        if c == mark or c == 0x5C:
            out.append(0x5C)
        out.append(c)
    return bytes(out)


def derez(resources: list[Resource], out: BinaryIO) -> int:
    """Write Rez source like classicvirtio's DeRez() (``derez.c``).  Returns bytes written."""
    if len(resources) > MAX_RESOURCES:
        raise FormatError(f"the resource fork has {len(resources)} resources; Mac OS's shared volume can hold "
                          f"up to {MAX_RESOURCES}")
    if sum(1 + len(r.name) for r in resources if r.name is not None) > MAX_NAME_LIST:
        raise FormatError("the resource fork's names are too long for Mac OS's shared volume")
    total = 0
    for r in resources:
        head = bytearray(b"data '") + _quoted(r.type, 0x27) + b"' (%d" % r.id
        if r.name is not None:
            head += b', "' + _quoted(r.name, 0x22) + b'"'
        a = r.attrs & 0xFF
        if a & 0x83:
            head += b", $%02X" % a
        else:
            for bit, word in ((0x40, b"sysheap"), (0x20, b"purgeable"), (0x10, b"locked"), (0x08, b"protected"),
                              (0x04, b"preload")):
                if a & bit:
                    head += b", " + word
        head += b") {\n"
        buf = bytearray(head)
        d = r.data
        for i in range(0, len(d), 16):
            chunk = d[i:i + 16]
            hexs = bytearray()
            for k in range(0, len(chunk), 2):
                if k:
                    hexs.append(0x20)
                hexs += _HEX_PAIR[chunk[k]]
                if k + 1 < len(chunk):
                    hexs += _HEX_PAIR[chunk[k + 1]]
            cmt = bytearray()
            slash = 0x2F
            for c in chunk:
                ch = _CMT[c]
                cmt.append(slash if c == 0x2F else ch)
                if c == 0x2A:
                    slash = 0x2E
                elif c >= 32:
                    slash = 0x2F
            missing = 16 - len(chunk)
            pad = 39 - len(hexs)
            buf += b'\t$"' + hexs + b'"' + b" " * (pad + 12) + b"/* " + cmt + b" */\n"
        buf += b"};\n\n"
        out.write(buf)
        total += len(buf)
    return total


_HEX_PAIR = [bytes([_HEX[c >> 4], _HEX[c & 15]]) for c in range(256)]


def derez_bytes(resources: list[Resource]) -> bytes:
    b = io.BytesIO()
    derez(resources, b)
    return b.getvalue()


_ESC = {ord("b"): 8, ord("t"): 9, ord("r"): 10, ord("v"): 11, ord("f"): 12, ord("n"): 13, ord("?"): 127}
_WS = b" \t\r\n"


class _Rez:
    def __init__(self, text: bytes):
        self.t = text
        self.i = 0

    def ws(self):
        t, i = self.t, self.i
        while i < len(t) and t[i] in _WS:
            i += 1
        self.i = i

    def fail(self, what):
        line = self.t.count(b"\n", 0, self.i) + 1
        raise FormatError(f"resource fork text: {what} (line {line})")

    def quote(self, mark: int, lo: int, hi: int) -> bytes:
        t = self.t
        if self.i >= len(t) or t[self.i] != mark:
            self.fail("expected a quoted string")
        i = self.i + 1
        out = bytearray()
        while True:
            if i >= len(t):
                self.fail("unterminated string")
            c = t[i]
            i += 1
            if c == mark:
                break
            if c == 0x5C:
                if i >= len(t):
                    self.fail("unterminated escape")
                c = t[i]
                i += 1
                if c in _ESC:
                    c = _ESC[c]
                elif c == 0x30 and t[i:i + 1] == b"x":
                    try:
                        c = int(t[i + 1:i + 3], 16)
                    except ValueError:
                        self.fail("bad \\0x escape")
                    i += 3
            out.append(c)
            if len(out) > hi:
                self.fail("string too long")
        if len(out) < lo:
            self.fail("string too short")
        self.i = i
        return bytes(out)

    def parse(self) -> list[Resource]:
        t = self.t
        out = []
        while True:
            self.ws()
            if self.i >= len(t) or t[self.i] == 0:
                return out
            if t[self.i:self.i + 4] != b"data":
                self.fail("expected 'data'")
            self.i += 4
            self.ws()
            rtype = self.quote(0x27, 4, 4)
            self.ws()
            if t[self.i:self.i + 1] != b"(":
                self.fail("expected '('")
            self.i += 1
            self.ws()
            m = re.compile(rb"-?\d+").match(t, self.i)
            if not m:
                self.fail("expected a resource ID")
            rid = int(m.group())
            if not -0x8000 <= rid <= 0x7FFF:
                self.fail("resource ID out of range")
            self.i = m.end()
            name, attrs = None, 0
            self.ws()
            while t[self.i:self.i + 1] == b",":
                self.i += 1
                self.ws()
                c = t[self.i:self.i + 1]
                if c == b'"' and name is None:
                    name = self.quote(0x22, 0, 255)
                elif c == b"$":
                    try:
                        attrs = int(t[self.i + 1:self.i + 3], 16)
                    except ValueError:
                        self.fail("bad attribute")
                    self.i += 3
                else:
                    for word, bit in ((b"sysheap", 0x40), (b"purgeable", 0x20), (b"locked", 0x10),
                                      (b"protected", 0x08), (b"preload", 0x04)):
                        if t.startswith(word, self.i):
                            attrs |= bit
                            self.i += len(word)
                            break
                    else:
                        self.fail("unknown resource attribute")
                self.ws()
            if t[self.i:self.i + 1] != b")":
                self.fail("expected ')'")
            self.i += 1
            self.ws()
            if t[self.i:self.i + 1] != b"{":
                self.fail("expected '{'")
            self.i += 1
            data = bytearray()
            while True:
                self.ws()
                c = t[self.i:self.i + 1]
                if c == b"/":
                    if t[self.i + 1:self.i + 2] != b"*":
                        self.fail("bad comment")
                    end = t.find(b"*/", self.i + 2)
                    if end < 0:
                        self.fail("unterminated comment")
                    self.i = end + 2
                elif c == b"$":
                    if t[self.i + 1:self.i + 2] != b'"':
                        self.fail("expected $\"")
                    end = t.find(b'"', self.i + 2)
                    if end < 0:
                        self.fail("unterminated hex string")
                    hexs = t[self.i + 2:end].replace(b" ", b"").replace(b"\t", b"")
                    try:
                        data += bytes.fromhex(hexs.decode("ascii"))
                    except ValueError:
                        self.fail("bad hex data")
                    self.i = end + 1
                elif c == b"}":
                    self.i += 1
                    self.ws()
                    if t[self.i:self.i + 1] != b";":
                        self.fail("expected ';'")
                    self.i += 1
                    break
                else:
                    self.fail("unexpected text in resource data")
            out.append(Resource(rtype, rid, name, attrs, bytes(data)))
            if len(out) > 32767:
                self.fail("too many resources")


def rez(text: bytes) -> list[Resource]:
    """Parse an .rdump written by the guest (DeRez) or by derez()."""
    return _Rez(text).parse()


def rez_fork_size(text: bytes) -> int:
    """Size of the binary fork an .rdump stands for, without building it."""
    res = rez(text)
    return len(build_fork(res)) if res else 0


# ---------------------------------------------------------------------------
# Decoded items
# ---------------------------------------------------------------------------

@dataclass
class MacFile:
    """A classic Mac file ready to be written into the share."""
    name: bytes                       # Mac OS Roman, from the encoding (may be empty: use the upload's)
    type: bytes = bytes(4)
    creator: bytes = bytes(4)
    flags: int = 0
    data_path: str | None = None      # a file holding the data fork (or data_src range)
    data_offset: int = 0
    data_length: int = 0
    rsrc: bytes = b""
    modified: float | None = None     # Unix time
    source: str = "plain"             # plain | macbinary | binhex | applesingle | appledouble
    notes: list[str] = field(default_factory=list)


def crc16(data: bytes, crc: int = 0) -> int:
    """CRC-16/XMODEM (CCITT, initial 0) as used by MacBinary II and BinHex 4.0."""
    return binascii.crc_hqx(data, crc)


def mac_to_unix(t: int) -> float | None:
    """Mac dates are local time since 1904."""
    if not t:
        return None
    naive = t - MAC_EPOCH
    try:
        return naive - time.localtime(naive).tm_gmtoff
    except (OverflowError, OSError, ValueError):
        return None


def unix_to_mac(t: float) -> int:
    try:
        v = int(t) + time.localtime(t).tm_gmtoff + MAC_EPOCH
    except (OverflowError, OSError, ValueError):
        return 0
    return v if 0 < v <= 0xFFFFFFFF else 0


def _pad128(n: int) -> int:
    return (n + 127) & ~127


def macbinary_header(head: bytes, size: int) -> dict | None:
    """Parse a MacBinary I/II/III header if ``head`` (at least 128 bytes of a ``size``-byte
    file) is one.  Checked strictly: version byte, name, zero fillers, CRC (II/III) or
    zeroed tail (I), and fork lengths that match the file size."""
    if len(head) < 128 or head[0] != 0 or head[74] != 0 or head[82] != 0:
        return None
    n = head[1]
    if not 1 <= n <= 63 or b":" in head[2:2 + n]:
        return None
    dlen, rlen = struct.unpack(">II", head[83:91])
    if dlen > 0x7FFFFFFF or rlen > 0xFFFFFF:
        return None
    crc_ok = crc16(head[:124]) == struct.unpack(">H", head[124:126])[0]
    version = head[122]
    if crc_ok and version >= 129:
        level = 3 if head[102:106] == b"mBIN" else 2
        sec = struct.unpack(">H", head[120:122])[0]
        flags = head[73] << 8 | head[101]
        comment = struct.unpack(">H", head[99:101])[0]
    elif not any(head[99:126]):
        level, sec, flags, comment = 1, 0, head[73] << 8, 0
    else:
        return None
    data_off = 128 + _pad128(sec)
    least = data_off + (_pad128(dlen) + rlen if rlen else dlen)
    most = data_off + _pad128(dlen) + _pad128(rlen) + _pad128(comment) + 128
    if not least <= size <= most:
        return None
    return {"level": level, "name": head[2:2 + n], "type": head[65:69], "creator": head[69:73],
            "flags": flags & ~FLAG_IS_ON_DESK, "data_offset": data_off, "data_length": dlen,
            "rsrc_offset": data_off + _pad128(dlen), "rsrc_length": rlen,
            "created": struct.unpack(">I", head[91:95])[0], "modified": struct.unpack(">I", head[95:99])[0]}


def macbinary_decode(path: str) -> MacFile:
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        h = macbinary_header(f.read(128), size)
        if h is None:
            raise FormatError("not a MacBinary file")
        f.seek(h["rsrc_offset"])
        rsrc = f.read(h["rsrc_length"])
    if len(rsrc) != h["rsrc_length"]:
        raise FormatError("the MacBinary file is shorter than its header says")
    return MacFile(name=h["name"], type=h["type"], creator=h["creator"], flags=h["flags"], data_path=path,
                   data_offset=h["data_offset"], data_length=h["data_length"], rsrc=rsrc,
                   modified=mac_to_unix(h["modified"]), source="macbinary")


def macbinary_header_bytes(mac_name: bytes, type_: bytes, creator: bytes, flags: int, dlen: int, rlen: int,
                           created: int = 0, modified: int = 0) -> bytes:
    """A MacBinary III header (readable by MacBinary II tools)."""
    h = bytearray(128)
    h[1] = len(mac_name)
    h[2:2 + len(mac_name)] = mac_name
    h[65:69] = type_.ljust(4, b"\0")[:4]
    h[69:73] = creator.ljust(4, b"\0")[:4]
    h[73] = (flags >> 8) & 0xFF
    h[83:91] = struct.pack(">II", dlen, rlen)
    h[91:99] = struct.pack(">II", created, modified)
    h[101] = flags & 0xFE                  # low byte, without kIsOnDesk
    h[102:106] = b"mBIN"
    h[122], h[123] = 130, 129
    h[124:126] = struct.pack(">H", crc16(bytes(h[:124])))
    return bytes(h)


def macbinary_stream(mac_name: bytes, type_: bytes, creator: bytes, flags: int, data: BinaryIO, dlen: int,
                     rsrc: bytes, modified: float | None = None, chunk: int = 256 * 1024) -> Iterator[bytes]:
    """MacBinary III, streamed: header, data fork, resource fork (each padded to 128)."""
    m = unix_to_mac(modified) if modified else 0
    yield macbinary_header_bytes(mac_name, type_, creator, flags, dlen, len(rsrc), m, m)
    left = dlen
    while left:
        b = data.read(min(chunk, left))
        if not b:
            raise FormatError("the data fork changed while it was being read")
        left -= len(b)
        yield b
    yield bytes(_pad128(dlen) - dlen)
    if rsrc:
        yield rsrc + bytes(_pad128(len(rsrc)) - len(rsrc))


def macbinary_length(dlen: int, rlen: int) -> int:
    return 128 + _pad128(dlen) + _pad128(rlen)


# -- BinHex 4.0 -------------------------------------------------------------------

BINHEX_MARK = b"(This file must be converted with BinHex"
HQX_BLOCK = 256 * 1024              # read size; tests shrink it to cross chunk boundaries
_HQX = b"!\"#$%&'()*+,-012345689@ABCDEFGHIJKLMNPQRSTUVXYZ[`abcdefhijklmpqr"
_B64 = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_HQX_TO_B64 = bytes.maketrans(_HQX, _B64)
_HQX_VALID = set(_HQX)


def binhex_start(head: bytes) -> int | None:
    """Offset just after the ':' that starts BinHex 4.0 data, if ``head`` has one."""
    at = head.find(BINHEX_MARK)
    if at < 0 or at > 4096:
        return None
    colon = head.find(b":", at + len(BINHEX_MARK))
    while colon >= 0 and colon > 0 and head[colon - 1] not in b"\r\n":
        colon = head.find(b":", colon + 1)
    return colon + 1 if colon >= 0 else None


def binhex_decode(path: str, work: str, max_bytes: int) -> MacFile:
    """Decode BinHex 4.0 into ``work`` (the data fork); the resource fork stays in memory."""
    with open(path, "rb") as f:
        start = binhex_start(f.read(8192))
        if start is None:
            raise FormatError("not a BinHex 4.0 file")
        f.seek(start)
        raw = work + ".raw"
        _hqx_to_raw(f, raw, max_bytes)
    try:
        return _binhex_parse(raw, work)
    finally:
        try:
            os.unlink(raw)
        except FileNotFoundError:
            pass


def _hqx_to_raw(f: BinaryIO, raw_path: str, max_bytes: int) -> None:
    """6-bit decoding (through base64) and run-length expansion, streamed."""
    pending = b""            # 6-bit characters not yet a multiple of 4
    last = None              # previous output byte, for runs
    marker = False           # a 0x90 ended the previous chunk
    total = 0
    with open(raw_path, "wb") as out:
        done = False
        while not done:
            block = f.read(HQX_BLOCK)
            if not block:
                raise FormatError("the BinHex data has no closing ':'")
            end = block.find(b":")
            if end >= 0:
                block, done = block[:end], True
            chars = block.translate(None, b" \t\r\n")
            if not set(chars) <= _HQX_VALID:
                raise FormatError("the BinHex data contains characters BinHex doesn't use")
            chars = pending + chars
            usable = len(chars) - len(chars) % 4
            if done:
                usable = len(chars)
            body, pending = chars[:usable], chars[usable:]
            tail = len(body) % 4
            decoded = base64.b64decode(body[:len(body) - tail].translate(_HQX_TO_B64)) if len(body) >= 4 else b""
            if tail:
                group = body[len(body) - tail:].translate(_HQX_TO_B64) + b"A" * (4 - tail)
                decoded += base64.b64decode(group)[:tail - 1]
            # run-length: 0x90 n repeats the previous byte n-1 more times; 0x90 0x00 is a literal 0x90
            outbuf = bytearray()
            i = 0
            if marker and decoded:
                n = decoded[0]
                i = 1
                marker = False
                if n == 0:
                    outbuf.append(0x90)
                    last = 0x90
                elif last is None:
                    raise FormatError("the BinHex data starts with a run")
                else:
                    outbuf += bytes([last]) * (n - 1)
            while i < len(decoded):
                j = decoded.find(b"\x90", i)
                if j < 0:
                    outbuf += decoded[i:]
                    last = decoded[-1]
                    break
                outbuf += decoded[i:j]
                if j > i:
                    last = decoded[j - 1]
                if j + 1 >= len(decoded):
                    marker = True
                    break
                n = decoded[j + 1]
                if n == 0:
                    outbuf.append(0x90)
                    last = 0x90
                elif last is None:
                    raise FormatError("the BinHex data starts with a run")
                else:
                    outbuf += bytes([last]) * (n - 1)
                i = j + 2
            total += len(outbuf)
            if total > max_bytes:
                raise FormatError("the BinHex file expands beyond the size limit")
            out.write(outbuf)


def _binhex_parse(raw: str, data_out: str) -> MacFile:
    size = os.path.getsize(raw)
    with open(raw, "rb") as f:
        head = f.read(22 + 63)
        if not head:
            raise FormatError("the BinHex file is empty")
        n = head[0]
        hlen = 1 + n + 1 + 4 + 4 + 2 + 4 + 4
        if not 1 <= n <= 63 or len(head) < hlen + 2:
            raise FormatError("the BinHex header is damaged")
        hdr = head[:hlen]
        name = hdr[1:1 + n]
        type_, creator = hdr[2 + n:6 + n], hdr[6 + n:10 + n]
        flags, dlen, rlen = struct.unpack(">HII", hdr[10 + n:20 + n])
        if crc16(hdr) != struct.unpack(">H", head[hlen:hlen + 2])[0]:
            raise FormatError("the BinHex header checksum doesn't match")
        if hlen + 2 + dlen + 2 + rlen + 2 > size or rlen > MAX_RSRC:
            raise FormatError("the BinHex file is shorter than its header says")
        f.seek(hlen + 2)
        crc = 0
        with open(data_out, "wb") as out:
            left = dlen
            while left:
                b = f.read(min(1 << 20, left))
                crc = crc16(b, crc)
                out.write(b)
                left -= len(b)
        if crc != struct.unpack(">H", f.read(2))[0]:
            raise FormatError("the BinHex data fork checksum doesn't match")
        rsrc = f.read(rlen)
        if crc16(rsrc) != struct.unpack(">H", f.read(2) or b"\0\0")[0]:
            raise FormatError("the BinHex resource fork checksum doesn't match")
    return MacFile(name=name, type=type_, creator=creator, flags=flags & ~FLAG_IS_ON_DESK, data_path=data_out,
                   data_offset=0, data_length=dlen, rsrc=rsrc, source="binhex")


def binhex_encode(mac_name: bytes, type_: bytes, creator: bytes, flags: int, data: bytes, rsrc: bytes) -> bytes:
    """BinHex 4.0 (used by tests and the lab tools)."""
    body = bytearray()

    def part(b: bytes):
        body.extend(b)
        body.extend(struct.pack(">H", crc16(b)))
    part(bytes([len(mac_name)]) + mac_name + b"\0" + type_ + creator + struct.pack(">HII", flags, len(data), len(rsrc)))
    part(data)
    part(rsrc)
    rle = bytearray()
    i = 0
    while i < len(body):
        c = body[i]
        j = i + 1
        while j < len(body) and body[j] == c and j - i < 255:
            j += 1
        run = j - i
        if c == 0x90:
            rle += b"\x90\x00" * run
        elif run >= 3:
            rle += bytes([c, 0x90, run])
        else:
            rle += bytes([c]) * run
        i = j
    enc = base64.b64encode(bytes(rle)).rstrip(b"=").translate(bytes.maketrans(_B64, _HQX))
    lines = [b":" + enc[:63]] + [enc[i:i + 64] for i in range(63, len(enc), 64)]
    return BINHEX_MARK + b" 4.0)\r\n" + b"\r\n".join(lines) + b":\r\n"


# -- AppleSingle / AppleDouble ----------------------------------------------------

APPLESINGLE = 0x00051600
APPLEDOUBLE = 0x00051607


def apple_header(head: bytes) -> tuple[int, list[tuple[int, int, int]]] | None:
    if len(head) < 26:
        return None
    magic, version = struct.unpack(">II", head[:8])
    if magic not in (APPLESINGLE, APPLEDOUBLE) or version not in (0x00010000, 0x00020000):
        return None
    n = struct.unpack(">H", head[24:26])[0]
    if n > 64 or len(head) < 26 + 12 * n:
        return None
    return magic, [struct.unpack(">III", head[26 + 12 * i:38 + 12 * i]) for i in range(n)]


def apple_decode(path: str) -> MacFile:
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(26 + 12 * 64)
        parsed = apple_header(head)
        if parsed is None:
            raise FormatError("not an AppleSingle or AppleDouble file")
        magic, entries = parsed
        mf = MacFile(name=b"", source="applesingle" if magic == APPLESINGLE else "appledouble",
                     data_path=path)
        for eid, off, ln in entries:
            if off + ln > size:
                raise FormatError(f"AppleSingle entry {eid} lies outside the file")
            if eid == 1:
                mf.data_offset, mf.data_length = off, ln
            elif eid == 2:
                if ln > MAX_RSRC:
                    raise FormatError("the resource fork is larger than 16 MB")
                f.seek(off)
                mf.rsrc = f.read(ln)
            elif eid == 3 and ln:
                f.seek(off)
                raw = f.read(min(ln, 255))
                try:
                    u = raw.decode("utf-8")
                    mf.name = to_mac(u) if any(c > 0x7F for c in raw) else raw
                except UnicodeDecodeError:
                    mf.name = raw
            elif eid == 9 and ln >= 10:
                f.seek(off)
                fi = f.read(32)
                mf.type, mf.creator = fi[0:4], fi[4:8]
                mf.flags = struct.unpack(">H", fi[8:10])[0] & ~FLAG_IS_ON_DESK
            elif eid == 8 and ln >= 16:
                f.seek(off)
                _c, m = struct.unpack(">ii", f.read(8))     # seconds since 2000-01-01 GMT
                mf.modified = 946684800 + m
    return mf


def applesingle_encode(mac_name: bytes, type_: bytes, creator: bytes, flags: int, data: bytes, rsrc: bytes,
                       double: bool = False) -> bytes:
    """AppleSingle (or the AppleDouble header file) version 2, for tests."""
    entries = [(3, mac_name), (9, type_ + creator + struct.pack(">H", flags) + bytes(22))]
    if rsrc:
        entries.append((2, rsrc))
    if not double:
        entries.append((1, data))
    off = 26 + 12 * len(entries)
    head = struct.pack(">II16sH", APPLEDOUBLE if double else APPLESINGLE, 0x00020000, bytes(16), len(entries))
    body = b""
    for eid, blob in entries:
        head += struct.pack(">III", eid, off + len(body), len(blob))
        body += blob
    return head + body


# -- Sniffing ---------------------------------------------------------------------

STUFFIT_MAGIC = (b"SIT!", b"ST46", b"ST50", b"ST60", b"ST65", b"STin", b"STi2", b"STi3", b"STi4")


def sniff(path: str) -> str:
    """macbinary | binhex | applesingle | appledouble | stuffit | plain."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(8192)
    if macbinary_header(head, size):
        return "macbinary"
    a = apple_header(head)
    if a:
        return "applesingle" if a[0] == APPLESINGLE else "appledouble"
    if binhex_start(head) is not None:
        return "binhex"
    if head[:4] in STUFFIT_MAGIC or head.startswith(b"StuffIt (c)1997-"):
        return "stuffit"
    return "plain"


def is_stuffit(mf: MacFile, name: str) -> bool:
    return mf.type in (b"SIT!", b"SITD", b"SIT5", b"SITX") or extension(name) in ("sit", "sea", "sitx")


# ---------------------------------------------------------------------------
# Text conversion (on request only)
# ---------------------------------------------------------------------------

def looks_like_text(head: bytes) -> bool:
    return b"\0" not in head[:65536]


def convert_text(src: BinaryIO, dst: BinaryIO, length: int, chunk: int = 1 << 20) -> dict:
    """Mac line endings (CR) and, for valid UTF-8, Mac OS Roman characters.

    Reads ``src`` twice (validation, then conversion); memory stays bounded."""
    start = src.tell()
    dec = codecs.getincrementaldecoder("utf-8")("strict")
    utf8, non_ascii = True, False
    left = length
    try:
        while left:
            b = src.read(min(chunk, left))
            if not b:
                break
            left -= len(b)
            non_ascii = non_ascii or any(c > 0x7F for c in b)
            dec.decode(b)
        dec.decode(b"", final=True)
    except UnicodeDecodeError:
        utf8 = False
    src.seek(start)
    recode = utf8 and non_ascii
    dec = codecs.getincrementaldecoder("utf-8-sig")("strict") if recode else None
    left = length
    cr_pending = False
    unmapped = 0
    lines = 0
    first = True
    while left:
        b = src.read(min(chunk, left))
        if not b:
            break
        left -= len(b)
        if first and not recode and b.startswith(codecs.BOM_UTF8):
            b = b[3:]
        first = False
        if cr_pending and b.startswith(b"\n"):
            b = b[1:]
        cr_pending = b.endswith(b"\r")
        lines += b.count(b"\n")
        b = b.replace(b"\r\n", b"\r").replace(b"\n", b"\r")
        if recode:
            s = dec.decode(b)
            outb = bytearray()
            for ch in s:
                try:
                    outb += ch.encode("mac_roman")
                except UnicodeEncodeError:
                    rep = to_mac(ch)
                    unmapped += 1
                    outb += rep if rep != b"_" else b"?"
            b = bytes(outb)
        dst.write(b)
    if recode:
        dst.write(dec.decode(b"", final=True).encode("mac_roman", "replace"))
    return {"line_endings": lines, "charset": "utf-8 -> Mac OS Roman" if recode else None, "unmapped": unmapped}
