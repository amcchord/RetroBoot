"""What kind of response this is, and the type Mac OS should see for downloads.

Classic Mac archives often arrive as ``application/octet-stream`` or even
``text/plain`` (BinHex).  For known extensions the helper sends the type the
browser and Internet Config map to StuffIt Expander or Disk Copy, so the file
is saved and expanded instead of shown as text.
"""
from __future__ import annotations

import posixpath
import re
from urllib.parse import unquote, urlsplit

MAC_TYPES = {
    ".sit": "application/x-stuffit",
    ".sitx": "application/x-stuffitx",
    ".sea": "application/x-sea",
    ".hqx": "application/mac-binhex40",
    ".bin": "application/macbinary",
    ".macbin": "application/macbinary",
    ".cpt": "application/mac-compactpro",
    ".dmg": "application/x-apple-diskimage",
    ".img": "application/x-apple-diskimage",
    ".smi": "application/x-apple-diskimage",
    ".dsk": "application/x-apple-diskimage",
    ".image": "application/x-apple-diskimage",
    ".toast": "application/octet-stream",
    ".iso": "application/octet-stream",
    ".cdr": "application/octet-stream",
    ".zip": "application/zip",
    ".gz": "application/x-gzip",
    ".tgz": "application/x-gzip",
    ".tar": "application/x-tar",
    ".z": "application/x-compress",
    ".mov": "video/quicktime",
    ".qt": "video/quicktime",
    ".mp3": "audio/mpeg",
    ".aif": "audio/x-aiff",
    ".aiff": "audio/x-aiff",
    ".wav": "audio/x-wav",
    ".mid": "audio/midi",
    ".pdf": "application/pdf",
    ".rtf": "text/rtf",
}
DOWNLOAD_EXT = set(MAC_TYPES) - {".pdf", ".rtf", ".mov", ".qt", ".mp3", ".aif", ".aiff", ".wav", ".mid"}
HTML_TYPES = ("text/html", "application/xhtml+xml")
TEXT_TYPES = ("text/plain",)


def extension(url: str, disposition: str | None = None) -> str:
    name = filename(url, disposition)
    low = name.lower()
    for double in (".sit.hqx", ".sea.hqx", ".sea.bin", ".dmg.hqx", ".img.hqx", ".tar.gz"):
        if low.endswith(double):
            return "." + double.rsplit(".", 1)[1]
    return posixpath.splitext(low)[1]


def filename(url: str, disposition: str | None = None) -> str:
    if disposition:
        m = re.search(r"filename\*\s*=\s*[^']*'[^']*'([^;]+)", disposition, re.I)
        if m:
            return unquote(m.group(1).strip().strip('"'))
        m = re.search(r'filename\s*=\s*"?([^";]+)"?', disposition, re.I)
        if m:
            return m.group(1).strip()
    return posixpath.basename(unquote(urlsplit(url).path)) or ""


def classify(content_type: str, url: str, disposition: str | None, method: str = "GET") -> str:
    """html | image | text | download"""
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    attachment = bool(disposition and disposition.strip().lower().startswith("attachment"))
    ext = extension(url, disposition)
    if ext in DOWNLOAD_EXT:
        return "download"
    if attachment:
        return "download"
    if mime in HTML_TYPES:
        return "html"
    if mime.startswith("image/"):
        return "image"
    if mime in TEXT_TYPES:
        return "text"
    return "download"


def download_type(content_type: str, url: str, disposition: str | None) -> str:
    ext = extension(url, disposition)
    if ext in MAC_TYPES:
        return MAC_TYPES[ext]
    mime = (content_type or "").split(";", 1)[0].strip()
    return mime or "application/octet-stream"


def old_disposition(disposition: str | None, url: str) -> str | None:
    """A Content-Disposition old browsers read: a plain filename, at most 31 characters."""
    if not disposition:
        return None
    name = filename(url, disposition)
    kind = disposition.split(";", 1)[0].strip() or "attachment"
    if not name:
        return kind
    safe = "".join(c if 32 <= ord(c) < 127 and c not in '"\\/:' else "_" for c in name)
    if len(safe) > 31:
        stem, dot, ext = safe.rpartition(".")
        if dot and len(ext) <= 8:
            safe = stem[:31 - len(ext) - 1] + "." + ext
        else:
            safe = safe[:31]
    return f'{kind}; filename="{safe}"'
