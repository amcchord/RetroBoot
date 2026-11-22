"""Request handling: from Mac OS's request to the translated response.

1. Work out the address: absolute-form (browser configured with a proxy),
   else ``Host`` (transparent), else the original destination.
2. The helper's own paths (``/__rbwh__/``): icons and the view switch.
3. Home-page and search redirects.
4. Fetch over http or https (hosts seen as https in pages or redirects are
   fetched with TLS), through vetted addresses only, with size and time caps.
   A redirect from http to the same address over https is followed here;
   other redirects go to the browser with an http address.
5. Translate: HTML (full or simplified view), images, plain text; downloads
   stream through unchanged with Mac-friendly types.

Logs carry method, host, path (never the query), status, kind, size and
time; never cookies, credentials or other headers.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
import time
from typing import AsyncIterator
from urllib.parse import parse_qs, quote, urljoin, urlsplit, urlunsplit

import aiohttp
from aiohttp.abc import AbstractResolver

from . import __version__, charset, html, images, mime, pages, redirects
from .config import MODERN_UA, SEARCH_ENGINES, SEARCH_FALLBACK, Config
from .security import Policy, Refused, peer_uid
from .server import HTTPError, Request, Response
from .urls import CONTROL_PREFIX, TLSMemory, for_client, idna_host, netloc_of, upstream

log = logging.getLogger("rbwebproxy")

HOP = {"connection", "proxy-connection", "keep-alive", "te", "trailer", "trailers", "upgrade",
       "transfer-encoding", "proxy-authorization", "proxy-authenticate", "host", "content-length",
       "accept-encoding", "expect"}
DROP_RESPONSE = {"connection", "keep-alive", "transfer-encoding", "content-encoding", "content-length",
                 "strict-transport-security", "content-security-policy",
                 "content-security-policy-report-only", "alt-svc", "public-key-pins", "expect-ct", "nel",
                 "report-to", "reporting-endpoints", "permissions-policy", "feature-policy",
                 "cross-origin-opener-policy", "cross-origin-embedder-policy", "cross-origin-resource-policy",
                 "origin-agent-cluster", "link", "server-timing", "accept-ch", "critical-ch",
                 "x-xss-protection", "upgrade", "proxy-connection", "trailer", "set-cookie", "location",
                 "refresh", "content-type", "content-disposition", "x-frame-options",
                 "x-content-type-options", "document-policy", "speculation-rules", "timing-allow-origin",
                 "access-control-allow-origin", "access-control-allow-credentials", "vary", "etag", "age",
                 "via", "x-cache", "cf-ray", "cf-cache-status", "server"}
KEEP_FOR_PASSTHROUGH = {"etag", "vary"}
REDIRECTS = (301, 302, 303, 307, 308)
HTML_ACCEPT = "text/html,application/xhtml+xml;q=0.9,text/plain;q=0.8,image/jpeg,image/png,image/gif,*/*;q=0.5"
IMAGE_ACCEPT = "image/jpeg,image/png,image/gif;q=0.9,image/*;q=0.5,*/*;q=0.3"
VIEW_OVERRIDE_S = 3600.0


class SafeResolver(AbstractResolver):
    """aiohttp resolver that returns only addresses the policy allows."""

    def __init__(self, policy: Policy):
        self.policy = policy

    async def resolve(self, host: str, port: int = 0, family: int = 0):
        return await self.policy.resolve(asyncio.get_running_loop(), host, port)

    async def close(self) -> None:
        return None


class Stats:
    def __init__(self):
        self.started = time.time()
        self.counts = {"requests": 0, "pages": 0, "simplified": 0, "images": 0, "images_converted": 0,
                       "downloads": 0, "redirects": 0, "errors": 0, "refused": 0, "certificate_errors": 0}
        self.bytes_in = 0
        self.bytes_out = 0
        self.last_error: dict | None = None
        self.transform_ms = 0.0
        self.changed = True

    def add(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n
        self.changed = True

    def error(self, kind: str, host: str) -> None:
        self.add("errors")
        self.last_error = {"kind": kind, "host": host, "at": int(time.time())}

    def public(self, extra: dict | None = None) -> dict:
        d = {"version": __version__, "pid": os.getpid(), "started": int(self.started),
             "updated": int(time.time()), "counts": dict(self.counts), "bytes_in": self.bytes_in,
             "bytes_out": self.bytes_out, "transform_ms": round(self.transform_ms),
             "last_error": self.last_error}
        d.update(extra or {})
        return d


class Upstream(Exception):
    def __init__(self, kind: str, detail: str = ""):
        super().__init__(detail or kind)
        self.kind = kind
        self.detail = detail


def _strip_cookie_attrs(sc: str) -> str:
    """Set-Cookie for an http-only browser: drop Secure, SameSite, Partitioned."""
    parts = [p for p in sc.split(";")]
    keep = [parts[0]]
    for p in parts[1:]:
        name = p.strip().split("=", 1)[0].strip().lower()
        if name in ("secure", "samesite", "partitioned", "priority"):
            continue
        keep.append(p)
    return ";".join(keep)


class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.policy = Policy(cfg.allow_lan, cfg.guest_network, cfg.extra_blocked)
        self.tls = TLSMemory()
        self.views: dict[str, tuple[str, float]] = {}
        self.engines_down: dict[str, float] = {}
        self.stats = Stats()
        self.session: aiohttp.ClientSession | None = None
        self.sem = asyncio.Semaphore(max(1, cfg.limits.concurrent_transforms))
        self._status_task: asyncio.Task | None = None

    # -- life cycle ---------------------------------------------------------------------
    async def start(self) -> None:
        ctx = ssl.create_default_context(cafile=self.cfg.ca_file) if self.cfg.ca_file else \
            ssl.create_default_context()
        conn = aiohttp.TCPConnector(resolver=SafeResolver(self.policy), ssl=ctx, limit=48,
                                    limit_per_host=8, ttl_dns_cache=30, enable_cleanup_closed=True)
        self.session = aiohttp.ClientSession(
            connector=conn, cookie_jar=aiohttp.DummyCookieJar(), trust_env=False, auto_decompress=True,
            timeout=aiohttp.ClientTimeout(total=None, connect=self.cfg.limits.connect_s,
                                          sock_connect=self.cfg.limits.connect_s,
                                          sock_read=self.cfg.limits.idle_s),
            max_line_size=65536, max_field_size=65536, read_bufsize=65536)
        if self.cfg.status_path:
            self._status_task = asyncio.create_task(self._status_loop())

    async def close(self) -> None:
        if self._status_task:
            self._status_task.cancel()
        if self.session:
            await self.session.close()

    def reconfigure(self, cfg: Config) -> None:
        self.cfg = cfg
        self.policy.allow_lan = cfg.allow_lan
        self.sem = asyncio.Semaphore(max(1, cfg.limits.concurrent_transforms))
        self.stats.changed = True

    async def _status_loop(self) -> None:
        while True:
            if self.stats.changed:
                self.stats.changed = False
                self.write_status()
            await asyncio.sleep(5.0)

    def write_status(self) -> None:
        if not self.cfg.status_path:
            return
        data = self.stats.public({"view": self.cfg.view, "tls_hosts": len(self.tls)})
        tmp = self.cfg.status_path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.chmod(tmp, 0o644)
            os.replace(tmp, self.cfg.status_path)
        except OSError:
            pass

    # -- who may connect -------------------------------------------------------------------
    def allow_peer(self, peer) -> bool:
        if not self.cfg.guest_uids:
            return True
        uid = peer_uid(peer) if peer else None
        ok = uid is not None and uid in self.cfg.guest_uids
        if not ok:
            log.warning("refused a connection from a local program that is not Mac OS (uid %s)", uid)
        return ok

    def refused_response(self) -> Response:
        return self.page(403, pages.error_page("not-guest", detail="Other programs on this computer can't use "
                                               "it.", back=False, cs=self.cfg.charset))

    def page(self, status: int, body: bytes, extra: list[tuple[str, str]] | None = None) -> Response:
        _, label = charset.codec_for(self.cfg.charset)
        headers = [("Content-Type", f"text/html; charset={label}"), ("Cache-Control", "no-cache"),
                   ("Pragma", "no-cache"), ("Expires", "Thu, 01 Jan 1970 00:00:00 GMT")]
        return Response(status, headers + (extra or []), body)

    def error_response(self, status: int, message: str) -> Response:
        return self.page(status, pages.error_page("bad-request", detail=message, back=True, cs=self.cfg.charset))

    # -- request entry ------------------------------------------------------------------------
    def client_url(self, req: Request) -> str:
        if req.absolute:
            return req.target
        host = req.header("Host")
        if not host and req.original_dst:
            ip, port = req.original_dst
            host = ip if port == 80 else f"{ip}:{port}"
        if not host:
            raise HTTPError(400, "The request has no Host header.")
        target = req.target if req.target.startswith("/") else "/" + req.target
        return f"http://{host.strip()}{target}"

    async def handle(self, req: Request) -> Response:
        t0 = time.monotonic()
        self.stats.add("requests")
        if req.method == "CONNECT":
            host = req.target.rsplit(":", 1)[0].strip("[]")
            self.stats.add("refused")
            return self.page(502, pages.secure_page(host, f"http://{host}/", self.cfg.charset))
        try:
            url = self.client_url(req)
            s = urlsplit(url)
        except (HTTPError, ValueError) as e:
            return self.error_response(400, getattr(e, "message", "The address could not be read."))
        if s.scheme != "http":
            what = "Secure (https) addresses" if s.scheme == "https" else f"{s.scheme.upper()} addresses"
            return self.page(501, pages.error_page("unsupported", host=s.hostname or "",
                                                   detail=f"{what} open directly in the browser, not through "
                                                          f"the helper.", cs=self.cfg.charset))
        host = (s.hostname or "").lower()
        if s.path.startswith(CONTROL_PREFIX):
            return self.control(s)
        resp = await self.dispatch(req, url, host)
        ms = (time.monotonic() - t0) * 1000
        log.info("%s %s%s -> %s %s %s %.0fms", req.method, host, s.path[:120], resp.status,
                 resp.get("X-RB-Kind") or "-", resp.length if resp.length is not None else len(resp.body or b""),
                 ms)
        resp.headers = [(k, v) for k, v in resp.headers if k != "X-RB-Kind"]
        return resp

    def control(self, s) -> Response:
        name = s.path[len(CONTROL_PREFIX):]
        if name in ("caution.gif", "stop.gif", "note.gif"):
            return Response(200, [("Content-Type", "image/gif"), ("Cache-Control", "max-age=86400")],
                            pages.icon_gif(name[:-4]))
        if name == "view":
            q = parse_qs(s.query)
            mode = (q.get("mode") or ["auto"])[0]
            to = (q.get("to") or ["/"])[0]
            if not to.startswith("/") or to.startswith("//"):
                to = "/"
            host = (s.hostname or "").lower()
            if mode in ("full", "simplified", "auto"):
                self.views[host] = (mode, time.monotonic() + VIEW_OVERRIDE_S)
            loc = urlunsplit(("http", s.netloc, to.split("?", 1)[0], to.split("?", 1)[1] if "?" in to else "", ""))
            return Response(302, [("Location", loc), ("Cache-Control", "no-cache")], b"")
        return self.page(404, pages.error_page("bad-request", detail="Unknown helper page.", cs=self.cfg.charset))

    def view_for(self, host: str) -> str:
        v = self.views.get(host)
        if v and v[1] > time.monotonic():
            return v[0]
        if v:
            self.views.pop(host, None)
        return self.cfg.view

    # -- redirects ------------------------------------------------------------------------------
    def homepage_redirect(self, req: Request, url: str, host: str) -> Response | None:
        if not self.cfg.homepages or req.method not in ("GET", "HEAD"):
            return None
        s = urlsplit(url)
        entry = redirects.match(self.cfg.redirects, host, s.path or "/")
        if entry is None:
            return None
        query = charset.transcode_form(s.query, self.cfg.charset) if s.query else ""
        to = redirects.target(entry, query, self.search_engine(), s.path)
        if not to or redirects.same_place(to, url):
            return None
        loc = for_client(to, self.tls)
        self.stats.add("redirects")
        body = pages.alert_html("This page has moved.", f"Opening {loc}", [("Continue", loc)], "note",
                                self.cfg.charset)
        r = self.page(302, body, [("Location", loc)])
        r.set("X-RB-Kind", "redirect")
        return r

    # -- search engine health --------------------------------------------------------------------
    @staticmethod
    def engine_host(name: str) -> str:
        home = (SEARCH_ENGINES.get(name) or {}).get("home") or ""
        return (urlsplit(home).hostname or "").lower()

    def search_engine(self) -> str:
        """The chosen engine, or the fallback while the chosen one is not answering."""
        name = self.cfg.search
        until = self.engines_down.get(self.engine_host(name))
        if until and until > time.monotonic() and name != SEARCH_FALLBACK and name != "none":
            return SEARCH_FALLBACK
        return name

    def search_failed(self, host: str, url: str) -> list[tuple[str, str]]:
        """Remember an unreachable engine; offer the fallback engine for this search."""
        if host != self.engine_host(self.cfg.search) or self.cfg.search == SEARCH_FALLBACK:
            return []
        self.engines_down[host] = time.monotonic() + 600
        terms = redirects.search_terms(urlsplit(url).query)
        fb = SEARCH_ENGINES[SEARCH_FALLBACK]
        target = fb["query"].format(q=quote(terms or "", safe="")) if terms else fb["home"]
        return [(f"Search with {fb['label']}", for_client(target, self.tls))]

    # -- fetching --------------------------------------------------------------------------------
    def upstream_headers(self, req: Request, kind_hint: str, target: str) -> dict:
        out: dict[str, str] = {}
        multi: list[tuple[str, str]] = []
        for k, v in req.headers:
            kl = k.lower()
            if kl in HOP or kl.startswith("proxy-"):
                continue
            if kl == "referer":
                try:
                    v = upstream(v, self.tls) if v.startswith("http://") else v
                except ValueError:
                    continue
            if kl == "cookie":
                multi.append(("Cookie", v))
                continue
            if kl in ("if-none-match", "if-modified-since") and kind_hint == "page":
                continue                    # translated pages have no validators of their own
            out[k] = v
        has_ua = any(k.lower() == "user-agent" for k in out)
        if self.cfg.user_agent != "guest" or not has_ua:
            ua = MODERN_UA if self.cfg.user_agent in ("modern", "guest") else self.cfg.user_agent
            out = {k: v for k, v in out.items() if k.lower() != "user-agent"}
            out["User-Agent"] = ua
        out["Accept-Encoding"] = "gzip, deflate"
        out = {k: v for k, v in out.items() if k.lower() != "accept"}
        out["Accept"] = IMAGE_ACCEPT if kind_hint == "image" else HTML_ACCEPT
        if multi:
            out["Cookie"] = "; ".join(v for _, v in multi)
        return out

    async def _open(self, method: str, url: str, headers: dict, body: bytes | None) -> aiohttp.ClientResponse:
        s = urlsplit(url)
        host = s.hostname or ""
        port = s.port or (443 if s.scheme == "https" else 80)
        lit = self.policy.literal(host)
        self.policy.check_port(port)
        if lit is not None:
            self.policy.check_ip(lit)
        else:
            self.policy.check_name(host)
        try:
            return await self.session.request(method, url, headers=headers, data=body if body else None,
                                              allow_redirects=False, ssl=None)
        except Refused:
            raise
        except aiohttp.ClientConnectorCertificateError as e:
            err = getattr(e, "certificate_error", None)
            detail = getattr(err, "verify_message", None) or str(err or e)
            raise Upstream("certificate", detail)
        except aiohttp.ClientSSLError as e:
            raise Upstream("refused-connection", f"The secure connection failed ({type(e).__name__}).")
        except aiohttp.ClientConnectorDNSError:
            raise Upstream("unreachable", "")
        except aiohttp.ClientConnectorError as e:
            raise Upstream("refused-connection", "") from e
        except (asyncio.TimeoutError, aiohttp.ServerTimeoutError):
            raise Upstream("timeout")
        except aiohttp.ClientError as e:
            raise Upstream("upstream", f"{type(e).__name__}")

    async def fetch(self, req: Request, client_url: str, kind_hint: str) -> tuple[aiohttp.ClientResponse, str]:
        """Open the upstream response, following same-address https upgrades."""
        method = req.method
        body = req.body or None
        if body and (req.header("Content-Type") or "").lower().startswith("application/x-www-form-urlencoded"):
            body = charset.transcode_form(body, self.cfg.charset).encode("latin-1")
        s = urlsplit(client_url)
        if s.query:
            client_url = urlunsplit((s.scheme, s.netloc, s.path, charset.transcode_form(s.query, self.cfg.charset), ""))
        headers = self.upstream_headers(req, kind_hint, client_url)
        url = upstream(client_url, self.tls)
        fell_back = False
        for _hop in range(self.cfg.limits.max_redirects + 1):
            try:
                resp = await self._open(method, url, headers, body)
            except Upstream as e:
                # http refused or reset: the site may only speak https (and the reverse for a
                # host remembered as https that no longer does).
                if e.kind in ("refused-connection",) and not fell_back:
                    fell_back = True
                    sp = urlsplit(url)
                    if sp.scheme == "http":
                        url = urlunsplit(("https", sp.netloc, sp.path, sp.query, ""))
                    else:
                        self.tls.forget(netloc_of(client_url))
                        url = urlunsplit(("http", sp.netloc.replace(":443", ""), sp.path, sp.query, ""))
                    continue
                raise
            if fell_back and urlsplit(url).scheme == "https" and resp.status < 500:
                self.tls.mark(netloc_of(client_url))
            if resp.status in REDIRECTS and resp.headers.get("Location"):
                loc = urljoin(url, resp.headers["Location"])
                if not loc.lower().startswith(("http://", "https://")):
                    return resp, url
                new_client = for_client(loc, self.tls)
                if redirects.same_place(new_client, client_url) and \
                        urlsplit(new_client).query == urlsplit(client_url).query:
                    resp.release()
                    if resp.status == 303 or (resp.status in (301, 302) and method == "POST"):
                        method, body = "GET", None
                    url = loc
                    continue
            return resp, url
        raise Upstream("upstream", "Too many redirects.")

    # -- dispatch ----------------------------------------------------------------------------------
    async def dispatch(self, req: Request, url: str, host: str) -> Response:
        r = self.homepage_redirect(req, url, host)
        if r is not None:
            return r
        kind_hint = "image" if (req.header("Accept") or "").startswith("image/") or \
            os.path.splitext(urlsplit(url).path)[1].lower() in (".jpg", ".jpeg", ".png", ".gif", ".webp",
                                                                 ".avif", ".svg") else "page"
        try:
            resp, real_url = await self.fetch(req, url, kind_hint)
        except Refused as e:
            self.stats.add("refused")
            kind = {"lan": "refused-lan", "port": "refused-port"}.get(e.kind, "refused-host")
            log.info("refused %s (%s)", host, e.kind)
            r = self.page(403, pages.error_page(kind, host=host, detail=e.message, cs=self.cfg.charset))
            r.set("X-RB-Kind", "refused")
            return r
        except Upstream as e:
            if e.kind == "certificate":
                self.stats.add("certificate_errors")
            self.stats.error(e.kind, host)
            status = {"timeout": 504, "certificate": 502}.get(e.kind, 502)
            extra = self.search_failed(host, url) if e.kind in ("timeout", "refused-connection", "unreachable") else []
            r = self.page(status, pages.error_page(e.kind, host=host, detail=e.detail, retry=url,
                                                   cs=self.cfg.charset, extra=extra))
            r.set("X-RB-Kind", "error")
            return r
        try:
            return await self.respond(req, url, real_url, resp)
        except Upstream as e:
            self.stats.error(e.kind, host)
            resp.release()
            r = self.page(502 if e.kind != "timeout" else 504,
                          pages.error_page(e.kind, host=host, detail=e.detail, retry=url, cs=self.cfg.charset))
            r.set("X-RB-Kind", "error")
            return r

    def response_headers(self, resp: aiohttp.ClientResponse, real_url: str, passthrough: bool) -> list:
        out = []
        for k, v in resp.headers.items():
            kl = k.lower()
            if kl in DROP_RESPONSE and not (passthrough and kl in KEEP_FOR_PASSTHROUGH):
                continue
            if kl.startswith(("x-amz-", "x-goog-", "cf-", "x-served", "x-timer", "x-request")):
                continue
            out.append((k, v))
        for sc in resp.headers.getall("Set-Cookie", []):
            out.append(("Set-Cookie", _strip_cookie_attrs(sc)))
        loc = resp.headers.get("Location")
        if loc:
            absu = urljoin(real_url, loc)
            out.append(("Location", for_client(absu, self.tls) if absu.lower().startswith(("http://", "https://"))
                        else loc))
        ref = resp.headers.get("Refresh")
        if ref:
            out.append(("Refresh", html.refresh_target(ref, html.Cleaner(self._ctx(real_url, real_url),
                                                                           real_url)) or "0"))
        return out

    def _ctx(self, real_url: str, client_url: str) -> html.Context:
        host = (urlsplit(client_url).hostname or "").lower()
        return html.Context(url=real_url, client_url=client_url, tls=self.tls, charset=self.cfg.charset,
                            max_width=self.cfg.image_max_width, view=self.view_for(host),
                            vintage_hosts=tuple(self.cfg.passthrough_hosts))

    async def _read_capped(self, resp: aiohttp.ClientResponse, cap: int, deadline: float) -> bytes:
        cl = resp.content_length
        if cl is not None and cl > cap and "content-encoding" not in {k.lower() for k in resp.headers}:
            raise Upstream("too-large", _size(cl))
        buf = bytearray()
        try:
            async with asyncio.timeout(deadline):
                async for chunk in resp.content.iter_chunked(65536):
                    buf += chunk
                    if len(buf) > cap:
                        raise Upstream("too-large", f"more than {_size(cap)}")
        except TimeoutError:
            raise Upstream("timeout")
        except aiohttp.ClientError as e:
            raise Upstream("upstream", type(e).__name__)
        finally:
            resp.release()
        self.stats.bytes_in += len(buf)
        return bytes(buf)

    async def respond(self, req: Request, client_url: str, real_url: str, resp: aiohttp.ClientResponse) -> Response:
        lim = self.cfg.limits
        ctype = resp.headers.get("Content-Type", "")
        disp = resp.headers.get("Content-Disposition")
        kind = mime.classify(ctype, real_url, disp, req.method)
        if resp.status in REDIRECTS:
            headers = self.response_headers(resp, real_url, passthrough=False)
            resp.release()
            self.stats.add("redirects")
            loc = next((v for k, v in headers if k == "Location"), "")
            if req.method == "HEAD" or not loc:
                r = Response(resp.status, headers, b"")
            else:
                r = self.page(resp.status, pages.alert_html("This page has moved.", f"Opening {loc}",
                                                            [("Continue", loc)], "note", self.cfg.charset), headers)
            r.set("X-RB-Kind", "redirect")
            return r
        if req.method == "HEAD" or resp.status in (204, 304):
            headers = self.response_headers(resp, real_url, passthrough=True)
            if ctype:
                headers.append(("Content-Type", ctype if kind != "html" else
                                f"text/html; charset={charset.codec_for(self.cfg.charset)[1]}"))
            resp.release()
            r = Response(resp.status, headers, b"")
            r.set("X-RB-Kind", "head" if req.method == "HEAD" else "status")
            return r
        if kind == "html":
            raw = await self._read_capped(resp, lim.page_bytes, lim.page_s)
            ct_charset = resp.charset
            ctx = self._ctx(real_url, client_url)
            t0 = time.monotonic()
            async with self.sem:
                result = await asyncio.to_thread(html.translate_bytes, raw, ct_charset, ctx)
            self.stats.transform_ms += (time.monotonic() - t0) * 1000
            self.stats.add("pages")
            if result.view == "simplified":
                self.stats.add("simplified")
            headers = self.response_headers(resp, real_url, passthrough=False)
            r = self.page(resp.status, result.body, headers)
            r.set("X-RB-Kind", f"html:{result.view}")
            self.stats.bytes_out += len(result.body)
            return r
        if kind == "image":
            raw = await self._read_capped(resp, lim.image_bytes, lim.page_s)
            t0 = time.monotonic()
            async with self.sem:
                if images.sniff(raw) == "svg":
                    conv = await images.rasterize(raw, self.cfg.image_max_width, self.cfg.jpeg_quality)
                else:
                    conv = await asyncio.to_thread(images.convert, raw, self.cfg.image_max_width,
                                                   self.cfg.jpeg_quality)
            self.stats.transform_ms += (time.monotonic() - t0) * 1000
            self.stats.add("images")
            passed = conv.note.startswith("passed")
            if not passed:
                self.stats.add("images_converted")
            headers = self.response_headers(resp, real_url, passthrough=passed)
            headers.append(("Content-Type", conv.content_type or ctype or "application/octet-stream"))
            self.stats.bytes_out += len(conv.body)
            r = Response(resp.status, headers, conv.body)
            r.set("X-RB-Kind", "image:" + conv.note)
            return r
        if kind == "text":
            raw = await self._read_capped(resp, lim.page_bytes, lim.page_s)
            text = charset.decode(raw, resp.charset)
            body = charset.encode_plain(text, self.cfg.charset)
            headers = self.response_headers(resp, real_url, passthrough=False)
            headers.append(("Content-Type", f"text/plain; charset={charset.codec_for(self.cfg.charset)[1]}"))
            r = Response(resp.status, headers, body)
            r.set("X-RB-Kind", "text")
            return r
        return self.download(req, real_url, resp)

    def download(self, req: Request, real_url: str, resp: aiohttp.ClientResponse) -> Response:
        lim = self.cfg.limits
        headers = self.response_headers(resp, real_url, passthrough=True)
        disp = resp.headers.get("Content-Disposition")
        headers.append(("Content-Type", mime.download_type(resp.headers.get("Content-Type", ""), real_url, disp)))
        od = mime.old_disposition(disp, real_url)
        if od:
            headers.append(("Content-Disposition", od))
        encoded = "content-encoding" in {k.lower() for k in resp.headers}
        length = None if encoded else resp.content_length
        if length is not None and length > lim.download_bytes:
            resp.release()
            raise Upstream("too-large", _size(length))
        self.stats.add("downloads")
        stats = self.stats

        async def body() -> AsyncIterator[bytes]:
            sent = 0
            t_end = time.monotonic() + lim.download_s
            try:
                while True:
                    try:
                        chunk = await asyncio.wait_for(resp.content.readany(), lim.idle_s)
                    except (asyncio.TimeoutError, aiohttp.ClientError):
                        log.info("download from %s stopped after %s", urlsplit(real_url).hostname, _size(sent))
                        return
                    if not chunk:
                        return
                    sent += len(chunk)
                    stats.bytes_in += len(chunk)
                    stats.bytes_out += len(chunk)
                    if sent > lim.download_bytes or time.monotonic() > t_end:
                        return
                    yield chunk
            finally:
                resp.release()

        r = Response(resp.status, headers, None, stream=body(), length=length)
        r.set("X-RB-Kind", "download")
        return r


def _size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    if n >= 1024:
        return f"{n // 1024} KB"
    return f"{n} bytes"


__all__ = ["App", "SafeResolver", "Stats", "idna_host", "quote"]
