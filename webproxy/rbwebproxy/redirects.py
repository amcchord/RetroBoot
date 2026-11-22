"""Home-page and search-page redirects.

Mac OS 9.2.1 and its browsers start on pages that no longer exist
(``livepage.apple.com``, ``home.netscape.com``, Excite's Apple search page).
An entry maps a host, optionally limited to a path prefix, to a new URL or to
the chosen search engine.  When a search page carries a query (``?q=``,
``?MT=`` ...), the query goes to the engine's results page.
"""
from __future__ import annotations

from urllib.parse import parse_qsl, quote_plus, unquote_plus, urlsplit

from .config import SEARCH_ENGINES

QUERY_KEYS = ("q", "query", "qt", "mt", "search", "searchfor", "p", "s", "text", "keywords", "kw")


def path_matches(p: str, path: str) -> bool:
    """An empty path matches every page of the host, "/" only its front page, "/x" the folder /x."""
    if not p:
        return True
    if p == "/":
        return path in ("", "/")
    return path == p or path.startswith(p if p.endswith("/") else p + "/") or (p.endswith("/") and path == p[:-1])


def match(redirects: list[dict], host: str, path: str) -> dict | None:
    """The most specific entry for host+path (longest path prefix wins)."""
    host = host.lower().rstrip(".")
    best = None
    for e in redirects:
        if e["host"] != host:
            continue
        p = e["path"]
        if not path_matches(p, path):
            continue
        if best is None or len(p) > len(best["path"]):
            best = e
    return best


def search_terms(query: str, path: str = "") -> str | None:
    for k, v in parse_qsl(query or "", keep_blank_values=False):
        if k.lower() in QUERY_KEYS and v.strip():
            return v.strip()
    # Netscape Internet Keywords put the words in the path: /keyword/<words>
    if path.lower().startswith("/keyword/"):
        words = unquote_plus(path[len("/keyword/"):]).strip()
        return words or None
    return None


def target(entry: dict, query: str, engine: str, path: str = "") -> str | None:
    """Where an entry sends this request (None: leave the request alone)."""
    to = entry["to"]
    if to != "search":
        return to
    eng = SEARCH_ENGINES.get(engine) or SEARCH_ENGINES["frogfind"]
    if not eng["home"]:
        return None
    terms = search_terms(query, path)
    if terms and eng["query"]:
        return eng["query"].format(q=quote_plus(terms))
    return eng["home"]


def hosts(redirects: list[dict]) -> set[str]:
    return {e["host"] for e in redirects}


def same_place(a: str, b: str) -> bool:
    """True when following a redirect would come back to the request itself."""
    x, y = urlsplit(a), urlsplit(b)
    return (x.hostname or "").lower() == (y.hostname or "").lower() and (x.path or "/") == (y.path or "/")
