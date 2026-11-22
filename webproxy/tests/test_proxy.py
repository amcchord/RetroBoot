"""The helper end to end, as IE 5 / Netscape 4 would use it, against local sites."""
import asyncio
import io
import logging
import os
import unittest

from multidict import CIMultiDict
from PIL import Image

from support import HelperCase, parse_response
from rbwebproxy.proxy import App
from rbwebproxy.config import Config
from rbwebproxy.urls import TLSMemory


def page(body: str, **kw):
    from aiohttp import web
    return web.Response(text=f"<html><head><title>T</title></head><body>{body}</body></html>",
                        content_type="text/html", charset="utf-8", **kw)


class TransparentTests(HelperCase):
    helper_config = {"redirects": [{"host": "livepage.apple.com", "path": "", "to": "http://system7today.com/"},
                                   {"host": "www.excite.com", "path": "/apple/", "to": "search"}]}

    async def test_page_is_translated(self):
        async def h(req):
            r = page('<script>x()</script><p>Café</p><a href="https://secure.test/a">s</a>')
            r.set_cookie("sid", "abc123", secure=True, samesite="None", httponly=True)
            return r
        self.up.routes["/"] = h
        status, hdrs, body = await self.h.get("/", host=self.site(), headers={"Cookie": "old=1"})
        self.assertEqual(status, 200)
        self.assertEqual(hdrs["content-type"], "text/html; charset=x-mac-roman")
        self.assertIn(b"Caf\x8e", body)
        self.assertNotIn(b"<script", body)
        self.assertIn(b'href="http://secure.test/a"', body)
        cookie = hdrs["set-cookie"]
        self.assertIn("sid=abc123", cookie)
        self.assertNotIn("Secure", cookie)
        self.assertNotIn("SameSite", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertEqual(self.up.seen[-1].headers.get("Cookie"), "old=1")
        self.assertEqual(self.up.seen[-1].headers.get("Accept-Encoding"), "gzip, deflate")
        self.assertTrue(self.h.app.tls.needs("secure.test"))

    async def test_explicit_proxy_request_and_keep_alive(self):
        self.up.routes["/"] = lambda req: asyncio.sleep(0, page("<p>one</p>"))
        url = f"http://{self.site()}/"
        req = (f"GET {url} HTTP/1.0\r\nProxy-Connection: Keep-Alive\r\n\r\n"
               f"GET {url} HTTP/1.0\r\nConnection: close\r\n\r\n").encode()
        raw = await self.h.raw(req)
        self.assertEqual(raw.count(b"HTTP/1.0 200 OK"), 2)
        self.assertIn(b"Proxy-Connection: keep-alive", raw)

    async def test_redirect_to_https_is_rewritten_and_remembered(self):
        tls = self.up.tls_port
        from aiohttp import web

        async def r(req):
            if not req.secure:
                raise web.HTTPMovedPermanently(f"https://secure.test:{tls}/story?id=1")
            return page("<p>over TLS</p>")
        self.up.routes["/story"] = r
        status, hdrs, _ = await self.h.get("/story", host=self.site())
        self.assertEqual(status, 301)
        self.assertEqual(hdrs["location"], f"http://secure.test:{tls}/story?id=1")
        status, hdrs, body = await self.h.get("/story?id=1", host=f"secure.test:{tls}")
        self.assertEqual(status, 200)
        self.assertIn(b"over TLS", body)
        self.assertTrue(self.up.seen[-1].secure)

    async def test_certificate_errors(self):
        self.up.routes["*"] = lambda req: asyncio.sleep(0, page("<p>should not be shown</p>"))
        for port, why in ((self.up.bad_tls_port, b"unable to get local issuer certificate"),
                          (self.up.expired_tls_port, b"certificate has expired")):
            self.h.app.tls.mark(f"secure.test:{port}")
            status, _, body = await self.h.get("/", host=f"secure.test:{port}")
            self.assertEqual(status, 502)
            self.assertIn(b"could not be verified", body)
            self.assertIn(why, body)
            self.assertNotIn(b"should not be shown", body)

    async def test_homepage_and_search_redirects(self):
        status, hdrs, body = await self.h.get("/", host="livepage.apple.com")
        self.assertEqual((status, hdrs["location"]), (302, "http://system7today.com/"))
        self.assertIn(b"This page has moved", body)
        status, hdrs, _ = await self.h.get("/apple/?search=classic%20mac", host="www.excite.com")
        self.assertEqual(hdrs["location"], "http://frogfind.com/?q=classic+mac")

    async def test_refusals(self):
        for host in ("127.0.0.1:443", "10.0.2.2", "localhost:8080", "[::1]", "169.254.169.254", "2130706433",
                     f"site.test:{self.up.http_port + 1 if self.up.http_port < 65535 else 1}"):
            status, _, body = await self.h.get("/api/v1/status", host=host)
            self.assertEqual(status, 403, host)
            self.assertIn(b"does not open this address", body)
        status, _, body = await self.h.get("/", host="mail.example:25")
        self.assertEqual(status, 403)
        self.assertIn(b"port 25", body)
        self.assertEqual(len(self.up.seen), 0)

    async def test_downloads_pass_unchanged(self):
        payload = os.urandom(300_000)
        from aiohttp import web
        self.up.routes["/files/Tool.sit"] = lambda req: asyncio.sleep(0, web.Response(
            body=payload, content_type="application/octet-stream"))
        self.up.routes["/files/Tool.hqx"] = lambda req: asyncio.sleep(0, web.Response(
            text="(This file must be converted with BinHex 4.0)\r\n:abc", content_type="text/plain"))
        self.up.routes["/dl"] = lambda req: asyncio.sleep(0, web.Response(
            body=payload[:1000], content_type="application/x-bin",
            headers={"Content-Disposition": "attachment; filename*=UTF-8''Caf%C3%A9%20Tool%20with%20a%20long%20name.sit"}))
        status, hdrs, body = await self.h.get("/files/Tool.sit", host=self.site())
        self.assertEqual((status, hdrs["content-type"], body), (200, "application/x-stuffit", payload))
        status, hdrs, body = await self.h.get("/files/Tool.hqx", host=self.site())
        self.assertEqual(hdrs["content-type"], "application/mac-binhex40")
        self.assertTrue(body.startswith(b"(This file must be converted"))
        status, hdrs, body = await self.h.get("/dl", host=self.site())
        self.assertEqual(hdrs["content-type"], "application/x-stuffit")
        self.assertRegex(hdrs["content-disposition"], r'^attachment; filename="[ -~]{1,31}\.sit"$')

    async def test_images_converted(self):
        img = Image.new("RGB", (1000, 500), (10, 120, 200))
        for x in range(0, 1000, 2):
            img.putpixel((x, 3), (x % 256, 0, 0))
        buf = io.BytesIO()
        img.save(buf, "WEBP", quality=80)
        from aiohttp import web
        self.up.routes["/pic"] = lambda req: asyncio.sleep(0, web.Response(body=buf.getvalue(),
                                                                          content_type="image/webp"))
        status, hdrs, body = await self.h.get("/pic", host=self.site(), headers={"Accept": "image/gif, */*"})
        self.assertEqual(status, 200)
        self.assertIn(hdrs["content-type"], ("image/jpeg", "image/gif"))
        self.assertEqual(Image.open(io.BytesIO(body)).width, 640)
        self.assertEqual(self.up.seen[-1].headers["Accept"].split(",")[0], "image/jpeg")

    async def test_form_post_from_macroman_page(self):
        from aiohttp import web
        got = {}

        async def r(req):
            got.update(await req.post())
            return page("<p>thanks</p>")
        self.up.routes["/login"] = r
        status, _, _ = await self.h.get("/login", host=self.site(), method="POST", body=b"user=Ren%8E&pw=s%A7cret",
                                        headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 200)
        self.assertEqual(got, {"user": "René", "pw": "sßcret"})

    async def test_connect_gets_friendly_page(self):
        raw = await self.h.raw(b"CONNECT www.example.com:443 HTTP/1.0\r\n\r\n")
        status, _, body = parse_response(raw)
        self.assertEqual(status, 502)
        self.assertIn(b"http://www.example.com/", body)

    async def test_view_switch_and_icons(self):
        status, hdrs, _ = await self.h.get("/__rbwh__/view?mode=full&to=%2Fnews%3Fa%3D1", host="paper.example")
        self.assertEqual((status, hdrs["location"]), (302, "http://paper.example/news?a=1"))
        self.assertEqual(self.h.app.view_for("paper.example"), "full")
        status, hdrs, _ = await self.h.get("/__rbwh__/view?mode=full&to=http://evil.example/", host="paper.example")
        self.assertEqual(hdrs["location"], "http://paper.example/")
        status, hdrs, body = await self.h.get("/__rbwh__/caution.gif", host="paper.example")
        self.assertEqual((status, hdrs["content-type"], body[:6]), (200, "image/gif", b"GIF89a"))

    async def test_no_cookies_or_queries_in_logs(self):
        self.up.routes["/acct"] = lambda req: asyncio.sleep(0, page("<p>x</p>"))
        with self.assertLogs("rbwebproxy", level="INFO") as logs:
            await self.h.get("/acct?token=SECRET123&pw=hunter2", host=self.site(),
                             headers={"Cookie": "session=COOKIEVALUE", "Authorization": "Basic QUxBRERJTg=="})
        text = "\n".join(logs.output)
        self.assertIn("/acct", text)
        for secret in ("SECRET123", "hunter2", "COOKIEVALUE", "QUxBRERJTg"):
            self.assertNotIn(secret, text)


class LimitTests(HelperCase):
    helper_config = {"limits": {"page_bytes": 50_000, "page_s": 1.0, "idle_s": 1.0, "image_bytes": 20_000}}

    async def test_page_too_large(self):
        from aiohttp import web
        self.up.routes["/big"] = lambda req: asyncio.sleep(0, page("x" * 80_000))

        async def stream(req):
            resp = web.StreamResponse(headers={"Content-Type": "text/html"})
            await resp.prepare(req)
            for _ in range(10):
                await resp.write(b"<p>" + b"y" * 10_000 + b"</p>")
            await resp.write_eof()
            return resp
        self.up.routes["/stream"] = stream
        for path in ("/big", "/stream"):
            status, _, body = await self.h.get(path, host=self.site())
            self.assertEqual(status, 502, path)
            self.assertIn(b"too large", body)

    async def test_slow_upstream_times_out(self):
        async def slow(req):
            await asyncio.sleep(5)
            return page("late")
        self.up.routes["/slow"] = slow
        status, _, body = await self.h.get("/slow", host=self.site())
        self.assertEqual(status, 504)
        self.assertIn(b"not responding", body)


class PeerTests(HelperCase):
    helper_config = {"guest_uids": [os.getuid() + 4242]}

    async def test_other_local_programs_refused(self):
        status, _, body = await self.h.get("/", host="example.com")
        self.assertEqual(status, 403)
        self.assertIn(b"serves Mac OS only", body)


class FakeResp:
    def __init__(self, status, location=None):
        self.status = status
        self.headers = CIMultiDict({"Location": location} if location else {})
        self.released = False

    def release(self):
        self.released = True


class UpgradeTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_address_upgrade_is_followed(self):
        app = App(Config.from_dict({}))
        calls = []

        async def fake_open(method, url, headers, body):
            calls.append(url)
            if url.startswith("http://"):
                return FakeResp(301, url.replace("http://", "https://"))
            return FakeResp(200)
        app._open = fake_open
        from rbwebproxy.server import Request
        req = Request("GET", "/a?b=1", "HTTP/1.0", [("Host", "up.example")])
        resp, real = await app.fetch(req, "http://up.example/a?b=1", "page")
        self.assertEqual(resp.status, 200)
        self.assertEqual(calls, ["http://up.example/a?b=1", "https://up.example/a?b=1"])
        self.assertEqual(real, "https://up.example/a?b=1")
        self.assertTrue(app.tls.needs("up.example"))
        calls.clear()
        await app.fetch(req, "http://up.example/c", "page")
        self.assertEqual(calls, ["https://up.example/c"])


class SearchFallbackTests(unittest.TestCase):
    def test_unreachable_engine_falls_back(self):
        app = App(Config.from_dict({"search": "frogfind"}))
        self.assertEqual(app.search_engine(), "frogfind")
        extra = app.search_failed("frogfind.com", "http://frogfind.com/?q=power+mac")
        self.assertEqual(extra, [("Search with Bing", "http://www.bing.com/search?q=power%20mac")])
        self.assertTrue(app.tls.needs("www.bing.com"))
        self.assertEqual(app.search_engine(), "bing")
        self.assertEqual(app.search_failed("example.com", "http://example.com/"), [])

    def test_modern_user_agent_upstream(self):
        from rbwebproxy.server import Request
        app = App(Config.from_dict({}))
        req = Request("GET", "/", "HTTP/1.0", [("Host", "a.example"), ("User-Agent", "Mozilla/4.0 (compatible; MSIE 5.0; Mac_PowerPC)")])
        self.assertIn("Firefox", app.upstream_headers(req, "page", "http://a.example/")["User-Agent"])
        app = App(Config.from_dict({"user_agent": "guest"}))
        self.assertIn("MSIE 5.0", app.upstream_headers(req, "page", "http://a.example/")["User-Agent"])


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    unittest.main()
