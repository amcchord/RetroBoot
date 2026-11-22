"""Plain HTTP: loopback tools get the app; network browsers are sent to HTTPS."""
import asyncio
import unittest
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import make_mocked_request

import helpers
from retroboot.hostd import Hostd


class PlainHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        helpers.make_package(self.root)
        self.cfg = helpers.make_config(self.root)
        self.cfg.https_port = 443  # the product port (tests elsewhere use an ephemeral one)
        self.app = Hostd(self.cfg)
        self.app.loop = asyncio.get_running_loop()
        self.app.settings.store.load()
        self.web = self.app.web
        self.web._hosts = {"192.168.1.72", "retroboot.local", "localhost", "127.0.0.1", "::1", "fe80::1"}
        self.web._hosts_time = 1e18  # never refresh from the (fake) network during the test

    def request(self, method, path, host, remote):
        transport = mock.Mock()
        transport.get_extra_info.side_effect = lambda name, default=None: {
            "peername": (remote, 50000), "sslcontext": None}.get(name, default)
        return make_mocked_request(method, path, headers={"Host": host}, transport=transport)

    async def call(self, req):
        async def handler(_request):
            return web.Response(text="app")
        try:
            return await self.web.mw_plain_http(req, handler)
        except web.HTTPException as e:
            return e

    async def test_network_browser_is_redirected_to_https(self):
        r = await self.call(self.request("GET", "/#/overview?x=1", "192.168.1.72", "192.168.1.158"))
        self.assertEqual(r.status, 301)
        self.assertEqual(r.headers["Location"], "https://192.168.1.72/")
        r = await self.call(self.request("GET", "/api/v1/status?a=b", "retroboot.local:80", "192.168.1.158"))
        self.assertEqual(r.headers["Location"], "https://retroboot.local/api/v1/status?a=b")

    async def test_non_get_keeps_its_method_and_never_reaches_the_app(self):
        r = await self.call(self.request("POST", "/api/v1/pair", "192.168.1.72", "192.168.1.158"))
        self.assertEqual(r.status, 308)

    async def test_https_port_and_ipv6_host(self):
        self.cfg.https_port = 18443
        r = await self.call(self.request("GET", "/", "[fe80::1]", "fe80::2"))
        self.assertEqual(r.headers["Location"], "https://[fe80::1]:18443/")

    async def test_unknown_host_is_refused_not_redirected(self):
        with mock.patch.object(self.web.hostd.network, "all_addresses", mock.AsyncMock(return_value=[])):
            r = await self.call(self.request("GET", "/", "evil.example", "192.168.1.158"))
        self.assertEqual(r.status, 421)
        self.assertNotIn("Location", r.headers)

    async def test_loopback_tools_still_reach_the_app(self):
        for remote in ("127.0.0.1", "::1"):
            r = await self.call(self.request("GET", "/api/v1/status", "localhost", remote))
            self.assertEqual(r.status, 200)
            self.assertEqual(r.text, "app")


if __name__ == "__main__":
    unittest.main()
