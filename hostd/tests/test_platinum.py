"""Platinum asset bundle: served from the data partition with long caching; built once when missing."""
import asyncio
import json
import sys
import types
import unittest

from aiohttp.test_utils import TestClient, TestServer

import helpers
from retroboot.hostd import Hostd
from retroboot.platinum import PlatinumAssets


class PlatinumServeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        helpers.make_package(self.root)
        self.app = Hostd(helpers.make_config(self.root))
        self.app.loop = asyncio.get_running_loop()
        self.app.settings.store.load()
        self.client = TestClient(TestServer(self.app.web.web_app, host="127.0.0.1"))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def test_absent_bundle_is_404(self):
        r = await self.client.get("/assets/platinum/manifest.json")
        self.assertEqual(r.status, 404)

    async def test_served_with_cache_headers_and_no_escape(self):
        out = self.app.paths.platinum_assets
        (out / "fonts").mkdir(parents=True)
        (out / "manifest.json").write_text(json.dumps({"format": 1}))
        (out / "fonts" / "geneva-10.ttf").write_bytes(b"\0\1\0\0font")
        (out.parent / "secret.txt").write_text("no")
        r = await self.client.get("/assets/platinum/manifest.json")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.headers["Cache-Control"], "no-cache")
        self.assertEqual(await r.json(), {"format": 1})
        r = await self.client.get("/assets/platinum/fonts/geneva-10.ttf")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.headers["Content-Type"], "font/ttf")
        self.assertIn("immutable", r.headers["Cache-Control"])
        self.assertIn("default-src 'self'", r.headers["Content-Security-Policy"])
        for bad in ("/assets/platinum/../secret.txt", "/assets/platinum/%2e%2e/secret.txt",
                    "/assets/platinum/.hidden", "/assets/platinum/fonts/"):
            r = await self.client.get(bad)
            self.assertEqual(r.status, 404, bad)


class PlatinumBuildTests(unittest.IsolatedAsyncioTestCase):
    async def test_builds_once_and_never_raises(self):
        td, root = helpers.scratch()
        self.addCleanup(td.cleanup)
        calls = []
        fake = types.ModuleType("rbplatinum.bundle")

        def build(src, out, force=False):
            calls.append(src)
            if len(calls) == 1:
                raise ValueError("no HFS+ volume found in image")
            return {"missing": []}
        fake.build = build
        pkg = types.ModuleType("rbplatinum")
        sys.modules["rbplatinum"] = pkg
        sys.modules["rbplatinum.bundle"] = fake
        self.addCleanup(sys.modules.pop, "rbplatinum")
        self.addCleanup(sys.modules.pop, "rbplatinum.bundle")
        events = []
        log = types.SimpleNamespace(event=lambda kind, **kw: events.append((kind, kw)))
        assets = PlatinumAssets(root / "assets", root / "lib", lifecycle=log)
        res = await assets.ensure(root / "disk.img")
        self.assertEqual(res["result"], "failed")
        self.assertEqual((await assets.ensure(root / "disk.img"))["result"], "skipped")   # once per run
        self.assertEqual(len(calls), 1)
        self.assertEqual(events[0][0], "platinum_assets")
        disabled = PlatinumAssets(root / "assets", root / "lib", enabled=False)
        self.assertEqual((await disabled.ensure(root / "disk.img"))["result"], "skipped")

    async def test_rebuilds_a_bundle_from_an_older_builder(self):
        td, root = helpers.scratch()
        self.addCleanup(td.cleanup)
        out = root / "assets"
        out.mkdir()
        (out / "manifest.json").write_text('{"generator": "rbplatinum 1"}')
        calls = []
        fake = types.ModuleType("rbplatinum.bundle")
        fake.__version__ = "2"
        fake.build = lambda src, dest, force=False: calls.append(force) or {"missing": []}
        sys.modules["rbplatinum"] = types.ModuleType("rbplatinum")
        sys.modules["rbplatinum.bundle"] = fake
        self.addCleanup(sys.modules.pop, "rbplatinum")
        self.addCleanup(sys.modules.pop, "rbplatinum.bundle")
        self.assertEqual((await PlatinumAssets(out, root / "lib").ensure(root / "disk.img"))["result"], "built")
        self.assertEqual(calls, [True])
        (out / "manifest.json").write_text('{"generator": "rbplatinum 2"}')
        self.assertEqual((await PlatinumAssets(out, root / "lib").ensure(root / "disk.img"))["result"], "skipped")


if __name__ == "__main__":
    unittest.main()
