"""Helper configuration: written by hostd as JSON, re-read on SIGHUP.

hostd owns the user's choices (settings ``network.web_proxy*``) and writes
``/run/retroboot/webproxy.json``; nothing here is secret.  Unknown keys are
ignored and bad values fall back to defaults, so a newer hostd never stops an
older helper from starting.
"""
from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

VIEWS = ("auto", "simplified", "full")

# Search engines offered for old search pages (redirect target "search").
SEARCH_ENGINES = {
    "frogfind": {"label": "FrogFind", "home": "http://frogfind.com/", "query": "http://frogfind.com/?q={q}"},
    "bing": {"label": "Bing", "home": "https://www.bing.com/", "query": "https://www.bing.com/search?q={q}"},
    "duckduckgo": {"label": "DuckDuckGo (HTML)", "home": "https://html.duckduckgo.com/html/",
                   "query": "https://html.duckduckgo.com/html/?q={q}"},
    "wiby": {"label": "Wiby", "home": "http://wiby.me/", "query": "http://wiby.me/?q={q}"},
    "none": {"label": "Keep the original search page", "home": None, "query": None},
}
# When the chosen engine does not answer, searches go here instead for a while.
SEARCH_FALLBACK = "bing"

# Sent upstream unless user_agent is "guest": many sites refuse or challenge a 1999
# browser's User-Agent (the helper does the modern part of the work for it).
MODERN_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"

HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")


@dataclass
class Limits:
    page_bytes: int = 8 * 1024 * 1024        # HTML/text to translate
    image_bytes: int = 16 * 1024 * 1024      # images to convert
    download_bytes: int = 2 * 1024 * 1024 * 1024
    upload_bytes: int = 32 * 1024 * 1024     # request bodies from Mac OS
    connect_s: float = 10.0
    page_s: float = 30.0                     # whole fetch of a page or image
    idle_s: float = 30.0                     # downloads: no data for this long
    download_s: float = 4 * 3600.0
    max_redirects: int = 8
    concurrent_transforms: int = 2


@dataclass
class Config:
    listen: list = field(default_factory=lambda: ["127.0.0.1"])
    port: int = 8080
    dns: bool = True
    dns_listen: list = field(default_factory=lambda: ["127.0.0.1", "::1"])
    dns_port: int = 8053
    guest_uids: list = field(default_factory=list)    # [] = any local client (development)
    view: str = "auto"
    homepages: bool = True
    redirects: list = field(default_factory=list)      # [{"host", "path", "to"}]
    search: str = "frogfind"
    allow_lan: bool = True
    guest_network: str = "10.0.2.0/24"
    redirect_address: str = "10.0.2.2"                # DNS answer for dead redirected names
    charset: str = "macroman"
    image_max_width: int = 640
    jpeg_quality: int = 72
    user_agent: str = "modern"                        # "modern", "guest" (the browser's own) or a string
    passthrough_hosts: list = field(default_factory=lambda: [
        "system7today.com", "frogfind.com", "68k.news", "wiby.me", "theoldnet.com", "oldweb.today"])
    bypass_hosts: list = field(default_factory=list)
    status_path: str | None = None
    ca_file: str | None = None                        # tests: extra trust anchor
    extra_blocked: list = field(default_factory=list)  # more networks to refuse
    log_level: str = "INFO"
    limits: Limits = field(default_factory=Limits)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        cfg = cls()
        known = {f.name for f in fields(cls)}
        for k, v in (data or {}).items():
            if k == "limits" and isinstance(v, dict):
                lim = Limits()
                for lk, lv in v.items():
                    if hasattr(lim, lk) and isinstance(lv, (int, float)) and not isinstance(lv, bool):
                        setattr(lim, lk, type(getattr(lim, lk))(lv))
                cfg.limits = lim
            elif k in known and k != "limits":
                default = getattr(cfg, k)
                if default is None or isinstance(v, type(default)) or (isinstance(default, float) and isinstance(v, int)):
                    setattr(cfg, k, v)
        if cfg.view not in VIEWS:
            cfg.view = "auto"
        if cfg.search not in SEARCH_ENGINES:
            cfg.search = "frogfind"
        if isinstance(cfg.listen, str):
            cfg.listen = [cfg.listen]
        cfg.redirects = normalize_redirects(cfg.redirects)
        cfg.bypass_hosts = normalize_bypass_hosts(cfg.bypass_hosts)
        cfg.image_max_width = max(160, min(2048, int(cfg.image_max_width)))
        return cfg

    @classmethod
    def load(cls, path: str | Path | None) -> "Config":
        if not path:
            return cls()
        data = json.loads(Path(path).read_text())
        if not isinstance(data, dict):
            raise ValueError("configuration must be a JSON object")
        return cls.from_dict(data)


def normalize_redirects(entries: Any) -> list[dict]:
    """Valid entries only: host (lowercase name), path ("" = any), to (URL or "search")."""
    out = []
    if not isinstance(entries, list):
        return out
    for e in entries[:200]:
        if not isinstance(e, dict):
            continue
        host = str(e.get("host") or "").strip().lower().rstrip(".")
        path = str(e.get("path") or "").strip()
        to = str(e.get("to") or "").strip()
        if not HOST_RE.match(host) or len(path) > 512 or (path and not path.startswith("/")):
            continue
        if to != "search" and not re.match(r"^https?://[^\s/?#]+[^\s]*$", to):
            continue
        if e.get("enabled") is False:
            continue
        out.append({"host": host, "path": path, "to": to})
    return out


def normalize_bypass_hosts(entries: Any) -> list[str]:
    """Normalized hostnames or IPv4 addresses that bypass helper rewriting."""
    if not isinstance(entries, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in entries[:128]:
        if not isinstance(raw, str):
            continue
        host = raw.strip().lower()
        host = re.sub(r"^https?://", "", host)
        host = host.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
        if ":" in host and not host.startswith("["):
            host = host.rsplit(":", 1)[0]
        while host.startswith("*."):
            host = host[2:]
        host = host.strip(".")
        if not host:
            continue
        ok = bool(HOST_RE.match(host))
        if not ok:
            try:
                ok = isinstance(ipaddress.ip_address(host), ipaddress.IPv4Address)
            except ValueError:
                ok = False
        if ok and host not in seen:
            seen.add(host)
            out.append(host)
    return out


def is_bypassed(host: str, bypass_hosts: list[str] | tuple[str, ...] | set[str]) -> bool:
    """True if host (exact or subdomain) is in the bypass whitelist."""
    h = (host or "").strip().lower().rstrip(".")
    if not h or not bypass_hosts:
        return False
    is_ip = False
    try:
        ipaddress.ip_address(h)
        is_ip = True
    except ValueError:
        pass
    for b in bypass_hosts:
        if h == b:
            return True
        if not is_ip and h.endswith("." + b):
            return True
    return False
