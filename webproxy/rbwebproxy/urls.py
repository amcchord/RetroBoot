"""URLs as Mac OS sees them (always http) and as the web serves them.

Every https URL handed to the old browser becomes http, and its host is
remembered so the next request for it is fetched over TLS again.  Hostnames
are IDNA-encoded and non-ASCII path or query characters percent-encoded,
because 1999-era browsers know neither.
"""
from __future__ import annotations

import re
from collections import OrderedDict
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

_SAFE = ":/?#[]@!$&'()*+,;=%~"
_WS = re.compile(r"[\t\n\r]")
CONTROL_PREFIX = "/__rbwh__/"


class TLSMemory:
    """Hosts (netloc in http form) that must be fetched with https."""

    def __init__(self, size: int = 8192):
        self.size = size
        self._hosts: OrderedDict[str, None] = OrderedDict()

    def mark(self, netloc: str) -> None:
        netloc = netloc.lower()
        if not netloc:
            return
        self._hosts[netloc] = None
        self._hosts.move_to_end(netloc)
        while len(self._hosts) > self.size:
            self._hosts.popitem(last=False)

    def needs(self, netloc: str) -> bool:
        netloc = netloc.lower()
        if netloc in self._hosts:
            self._hosts.move_to_end(netloc)
            return True
        return False

    def forget(self, netloc: str) -> None:
        self._hosts.pop(netloc.lower(), None)

    def __len__(self) -> int:
        return len(self._hosts)


def idna_host(host: str) -> str:
    if host.isascii():
        return host.lower()
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return quote(host, safe="")


def _netloc(scheme: str, host: str, port: int | None, userinfo: str = "") -> str:
    h = idna_host(host)
    if ":" in h and not h.startswith("["):
        h = f"[{h}]"
    default = 443 if scheme == "https" else 80
    nl = h if port in (None, default) else f"{h}:{port}"
    return f"{userinfo}@{nl}" if userinfo else nl


def for_client(url: str, tls: TLSMemory | None) -> str:
    """An absolute http(s) URL as the old browser should request it."""
    s = urlsplit(url)
    if s.scheme not in ("http", "https") or not s.hostname:
        return url
    try:
        port = s.port
    except ValueError:
        return url
    userinfo = s.netloc.rsplit("@", 1)[0] if "@" in s.netloc else ""
    if s.scheme == "https":
        nl = _netloc("http", s.hostname, port if port not in (None, 443) else None, userinfo)
        if tls is not None:
            tls.mark(nl.rsplit("@", 1)[-1])
    else:
        nl = _netloc("http", s.hostname, port, userinfo)
    return urlunsplit(("http", nl, quote(s.path, safe=_SAFE) or "/", quote(s.query, safe=_SAFE),
                       quote(s.fragment, safe=_SAFE)))


def rewrite(raw: str | None, base: str, tls: TLSMemory | None) -> str | None:
    """A link, image or form address from a page, for the old browser.

    None means "drop it" (javascript:, data:, blob: and empty addresses)."""
    if raw is None:
        return None
    raw = _WS.sub("", raw).strip()
    if not raw:
        return None
    low = raw[:12].lower()
    if low.startswith(("javascript:", "data:", "blob:", "about:", "vbscript:", "intent:", "chrome:")):
        return None
    if low.startswith(("mailto:", "news:", "nntp:", "ftp:", "gopher:", "telnet:")):
        return raw
    if raw.startswith("#"):
        return quote(raw, safe=_SAFE)
    try:
        absu = urljoin(base, raw)
    except ValueError:
        return None
    if not absu.lower().startswith(("http://", "https://")):
        return None if ":" in absu.split("/", 1)[0] else absu
    try:
        return for_client(absu, tls)
    except ValueError:
        return None


def upstream(client_url: str, tls: TLSMemory) -> str:
    """The URL to fetch for a request from the old browser."""
    s = urlsplit(client_url)
    host = (s.netloc.rsplit("@", 1)[-1]).lower()
    if s.scheme == "http" and tls.needs(host):
        port = s.port
        nl = _netloc("https", s.hostname or "", port if port not in (None, 80) else None)
        return urlunsplit(("https", nl, s.path or "/", s.query, ""))
    return urlunsplit((s.scheme, s.netloc, s.path or "/", s.query, ""))


def netloc_of(url: str) -> str:
    return urlsplit(url).netloc.rsplit("@", 1)[-1].lower()


def strip_default_port(netloc: str, scheme: str = "http") -> str:
    if scheme == "http" and netloc.endswith(":80"):
        return netloc[:-3]
    if scheme == "https" and netloc.endswith(":443"):
        return netloc[:-4]
    return netloc
