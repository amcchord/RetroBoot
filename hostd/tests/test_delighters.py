"""Tests for RetroBoot 3.4.0 usability delighters:
1. Network time zone auto-guess (tzguess, Hostd.guess_timezone, TimeZone screen, HardwareCheck).
2. Live transfer speed and ETA reporting (TransferMeter, CatalogClient.download, UpdateManager._download).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from aiohttp import web

import helpers
from retroboot import schema, tzguess
from retroboot.catalog import CatalogClient
from retroboot.hostd import Hostd
from retroboot.panel import ui
from retroboot.panel.controller import Panel, TimeZone
from retroboot.panel.installer import HardwareCheck, InstallerPanel
from retroboot.update.manager import UpdateManager
from retroboot.util import TransferMeter, format_eta, format_speed, format_transfer_progress


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for x in node:
            yield from _walk(x)


def _all_text(doc: dict) -> str:
    parts = []
    for n in _walk(doc):
        for key in ("text", "label", "title", "message", "detail"):
            v = n.get(key)
            if isinstance(v, str):
                parts.append(v)
    return "\n".join(parts)


def _find_id(doc: dict, control_id: str) -> dict | None:
    for n in _walk(doc):
        if n.get("id") == control_id:
            return n
    return None


def _all_buttons(doc: dict) -> dict[str, dict]:
    out = {}
    for n in _walk(doc):
        if n.get("type") == "buttons":
            for b in n.get("items", []):
                if "id" in b:
                    out[b["id"]] = b
    return out


class TransferSpeedTests(unittest.TestCase):
    def test_format_speed_and_eta(self):
        self.assertEqual(format_speed(None), "")
        self.assertEqual(format_speed(0), "")
        self.assertEqual(format_speed(512), "512 B/s")
        self.assertEqual(format_speed(2560), "2.5 KB/s")
        self.assertEqual(format_speed(42 * 1024), "42 KB/s")
        self.assertEqual(format_speed(5.5 * 1024 * 1024), "5.5 MB/s")
        self.assertEqual(format_speed(142 * 1024 * 1024), "142 MB/s")

        self.assertEqual(format_eta(None), "")
        self.assertEqual(format_eta(0), "")
        self.assertEqual(format_eta(14.2), "14 s left")
        self.assertEqual(format_eta(125), "2m 05s left")
        self.assertEqual(format_eta(1200), "20 min left")
        self.assertEqual(format_eta(3720), "1h 02m left")

    def test_transfer_meter_resumed_and_reset(self):
        now = [100.0]
        # Resumed transfer starting at 200 MB of 300 MB: initial 200 MB must NOT inflate speed.
        meter = TransferMeter(total=300 * 1024 * 1024, initial=200 * 1024 * 1024, clock=lambda: now[0])
        self.assertEqual(meter.update(200 * 1024 * 1024), (None, None))
        now[0] = 102.0
        bps, eta = meter.update(220 * 1024 * 1024)
        self.assertIsNotNone(bps)
        self.assertAlmostEqual(bps / (1024 * 1024), 10.0, places=2)
        self.assertAlmostEqual(eta, 8.0, places=2)

        # Reset when server ignores Range (done < last_done).
        now[0] = 103.0
        bps, eta = meter.update(1024 * 1024)
        self.assertEqual((bps, eta), (None, None))
        now[0] = 105.0
        bps, eta = meter.update(21 * 1024 * 1024)
        self.assertAlmostEqual(bps / (1024 * 1024), 10.0, places=2)
        self.assertIsNotNone(eta)

    def test_format_transfer_progress_and_ui_dedupe(self):
        line = format_transfer_progress(
            "Downloading…",
            87 * 1024 * 1024,
            197 * 1024 * 1024,
            14.2 * 1024 * 1024,
            8.0,
        )
        self.assertEqual(line, "Downloading… 44% · 87 of 197 MB · 14.2 MB/s · 8 s left")
        # ui.format_download_status must not double-prefix percentage when hostd scales progress to 0.5
        self.assertEqual(ui.format_download_status(0.22, line), "44% · 87 of 197 MB · 14.2 MB/s · 8 s left")
        # Legacy strings without leading % keep working identically
        self.assertEqual(ui.format_download_status(0.44, "Downloading… 87 of 197 MB"), "44% · 87 of 197 MB")


class _LocalHttpsMixin:
    async def _start_server(self, app):
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        return runner, f"http://127.0.0.1:{port}"


class TzGuessTests(unittest.IsolatedAsyncioTestCase, _LocalHttpsMixin):
    async def test_guess_timezone_fallback_and_validation(self):
        async def bad_fail(request):
            return web.json_response({"success": False, "timezone": {"id": "Europe/Paris"}})

        async def invalid_tz(request):
            return web.json_response({"timezone": "Mars/Olympus_Mons"})

        async def good_ipwho(request):
            return web.json_response({"success": True, "timezone": {"id": "America/New_York"}})

        async def good_text(request):
            return web.Response(text="Europe/Berlin\n", content_type="text/plain")

        app = web.Application()
        app.router.add_get("/fail", bad_fail)
        app.router.add_get("/bad", invalid_tz)
        app.router.add_get("/ipwho", good_ipwho)
        app.router.add_get("/text", good_text)
        runner, base = await self._start_server(app)
        try:
            # Non-HTTPS rejected unless allow_http=True
            z, src = await tzguess.guess_timezone([f"{base}/ipwho"], allow_http=False)
            self.assertIsNone(z)
            self.assertIsNone(src)

            # Empty list disables cleanly
            z, src = await tzguess.guess_timezone([])
            self.assertIsNone(z)

            # Falls through fail + invalid to valid ipwho.is format
            z, src = await tzguess.guess_timezone(
                [f"{base}/fail", f"{base}/bad", f"{base}/ipwho"],
                allow_http=True,
            )
            self.assertEqual(z, "America/New_York")
            self.assertEqual(src, "127.0.0.1")

            # Plain text endpoint (ipapi.co style)
            z, _ = await tzguess.guess_timezone([f"{base}/text"], allow_http=True)
            self.assertEqual(z, "Europe/Berlin")
        finally:
            await runner.cleanup()


class PanelTimeZoneAndHardwareTests(unittest.IsolatedAsyncioTestCase, _LocalHttpsMixin):
    async def test_hostd_guess_timezone_and_screen_behavior(self):
        async def tz_handler(request):
            return web.json_response({"timezone": "America/Chicago"})

        web_app = web.Application()
        web_app.router.add_get("/tz", tz_handler)
        runner, base = await self._start_server(web_app)
        td, root = helpers.scratch()
        try:
            with td:
                cfg = helpers.make_config(
                    root,
                    timezone_guess_urls=[f"{base}/tz"],
                    timezone_guess_allow_http=True,
                )
                app = Hostd(cfg)
                app.settings.store.load()

                async def fake_addrs():
                    return ["192.168.1.50"]

                async def fake_hw():
                    return {"label": "Intel Wi-Fi 6 AX200"}

                async def fake_ws():
                    return {"available": True, "connected": "LabNet", "ssid": "LabNet"}

                async def noop(*args, **kwargs):
                    return None

                app.network.all_addresses = fake_addrs
                app.network.wifi_hardware = fake_hw
                app.network.wifi_state = fake_ws
                app.show_scene = noop
                app.presenter.try_request = noop
                app.loop = asyncio.get_running_loop()

                class _FakePanel(Panel):
                    def __init__(self, a):
                        super().__init__(a)
                        self.docs = []

                    async def show(self, screen, background=None, reset=True):
                        self.screen = screen
                        self.visible = True
                        self.docs.append(screen.doc())

                    async def refresh(self):
                        if self.screen and self.visible:
                            self.docs.append(self.screen.doc())

                # 1. HardwareCheck shows IP/SSID while keeping exact Wi-Fi line and triggers prefetch
                ip = InstallerPanel(app)
                ip.list_disks = lambda: asyncio.sleep(0, result=[
                    {"path": "/dev/vda", "model": "QEMU HARDDISK", "size_bytes": 40 * 1024**3,
                     "removable": False, "transport": "virtio", "is_boot_medium": False, "key": "/dev/vda"}
                ])
                hw = HardwareCheck(ip)
                await ip.show(hw)
                await helpers.wait_for(lambda: hw.addresses is not None, timeout=3.0)
                doc = hw.doc()
                txt = _all_text(doc)
                self.assertIn("Connected to “LabNet” (192.168.1.50)", txt)
                self.assertIn("Wi-Fi: Intel Wi-Fi 6 AX200", txt)
                self.assertIn("wifi", _all_buttons(doc))

                # Prefetch populates guessed_timezone
                await helpers.wait_for(lambda: app.guessed_timezone == "America/Chicago", timeout=3.0)
                self.assertEqual(app.guessed_timezone, "America/Chicago")

                # 2. TimeZone pre-selects cached guessed timezone and includes Detect + local clock
                panel = _FakePanel(app)
                tz_screen = TimeZone(panel, back=lambda: asyncio.sleep(0))
                await panel.show(tz_screen)
                tz_doc = tz_screen.doc()
                tz_txt = _all_text(tz_doc)
                self.assertEqual(tz_screen.selected, "America/Chicago")
                self.assertEqual(tz_screen.region, "America")
                self.assertIn("Suggested from your network: Chicago", tz_txt)
                self.assertIn("Selected: America/Chicago", tz_txt)
                self.assertIn("detect", _all_buttons(tz_doc))

                # 3. User touch prevents async auto-guess from overriding user selection
                app._guessed_timezone = None
                tz2 = TimeZone(panel, back=lambda: asyncio.sleep(0))
                await panel.show(tz2)
                await tz2.change("zone", "Europe/London")
                await helpers.wait_for(lambda: not tz2.guessing, timeout=3.0)
                self.assertEqual(tz2.selected, "Europe/London")
        finally:
            await runner.cleanup()


class DownloadAndUpdateProgressTests(unittest.IsolatedAsyncioTestCase, _LocalHttpsMixin):
    async def test_catalog_and_update_download_report_speed(self):
        payload = b"R" * (2 * 1024 * 1024)
        sha = hashlib.sha256(payload).hexdigest()

        async def slow_file(request):
            resp = web.StreamResponse(status=200, headers={"Content-Length": str(len(payload))})
            await resp.prepare(request)
            half = len(payload) // 2
            await resp.write(payload[:half])
            await asyncio.sleep(0.3)
            await resp.write(payload[half:])
            await resp.write_eof()
            return resp

        web_app = web.Application()
        web_app.router.add_get("/mac.tar.gz", slow_file)
        web_app.router.add_get("/update.rbu", slow_file)
        runner, base = await self._start_server(web_app)
        td, root = helpers.scratch()
        try:
            with td:
                # 1. CatalogClient.download (test HTTPS check bypassed only via verify_tls=False + https rewrite isn't needed
                # because secure_url enforces https; test _download in UpdateManager with update_allow_http=True, and
                # test CatalogClient download progress callback via monkeypatched _get).
                client = CatalogClient("https://example.test/catalog.json", root / "dl")
                seen_msgs = []
                entry = {
                    "id": "test-mac",
                    "name": "Test Mac",
                    "archiveURL": f"{base}/mac.tar.gz",
                    "archiveBytes": len(payload),
                    "installedBytes": 4096,
                    "sha256": sha,
                }
                import aiohttp
                orig_get = client._get

                async def local_get(session, url, headers=None):
                    return await session.get(url, headers=headers or {})

                client._get = local_get
                client._session = lambda **kw: aiohttp.ClientSession(auto_decompress=False)
                out_path = await client.download(entry, lambda f, m: seen_msgs.append((f, m)))
                self.assertTrue(out_path.is_file())
                self.assertTrue(any("/s" in m and "2 MB" in m for _, m in seen_msgs), seen_msgs)

                # 2. UpdateManager._download emits determinate progress 0..0.5 and speed
                cfg = helpers.make_config(root, update_allow_http=True)
                app = Hostd(cfg)
                app.loop = asyncio.get_running_loop()
                um = UpdateManager(app)
                um.unavailable = None
                um.committed = True
                um.state = {
                    "check": {
                        "at": "2026-11-25T10:00:00Z",
                        "url": f"{base}/feed.json",
                        "found": {
                            "version": "3.4.0",
                            "build_id": "3.4.0-20261125T100000Z-test",
                            "url": f"{base}/update.rbu",
                            "sha256": sha,
                            "bytes": len(payload),
                            "notes": "",
                            "label": "3.4.0",
                        },
                    }
                }
                updates = []

                class _FakeJob:
                    def update(self, p, m):
                        updates.append((p, m))

                dl = await um._download(_FakeJob(), progress_span=0.5)
                self.assertTrue(dl.is_file())
                self.assertTrue(any(p is not None and 0.0 < p <= 0.5 for p, _ in updates), updates)
                self.assertTrue(any("/s" in m and "Downloading RetroBoot 3.4.0…" in m for _, m in updates), updates)
        finally:
            await runner.cleanup()


if __name__ == "__main__":
    unittest.main()
