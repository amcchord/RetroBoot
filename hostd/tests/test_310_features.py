"""Comprehensive test suite for RetroBoot 3.1.0 hostd features and fixes."""
from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import logging
import os
import struct
import sys
import tarfile
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer

import helpers
from retroboot import apply as apply_mod, catalog, catalogs, checkpoints, labauth, machines, printer, schema, usbshare
from retroboot.hostd import Hostd
from retroboot.remote import encode, hub as hub_mod, keymap
from retroboot.web import pict


REPO = Path(__file__).resolve().parents[2]
GUESTIO_PATH = REPO / "apps/rbapps/guestio.py"


def _load_guestio():
    spec = importlib.util.spec_from_file_location("rb_guestio_mod", GUESTIO_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _make_archive(dest: Path, files: list[tuple[str, bytes]]) -> None:
    with tarfile.open(dest, "w:gz") as tf:
        for name, data in files:
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))


def _make_pict_v2_8bit() -> bytes:
    """Construct a valid synthetic 4x2 8-bit PackBits PICT v2 image."""
    hdr = bytearray(512)
    # PICT header: size(2)=0, frame rect (top=0, left=0, bottom=2, right=4)
    pict_hdr = struct.pack(">Hhhhh", 0, 0, 0, 2, 4) + b"\x00\x11\x02\xff"
    # PackBitsRect opcode 0x0098
    op = struct.pack(">H", 0x0098)
    # PixMap record without baseAddr (starts at rowBytes | 0x8000):
    # rowBytes(H)=0x8004, bounds(4h)=(0,0,2,4), pmVersion(H)=0, packType(H)=0, packSize(I)=0,
    # hRes(I)=72<<16, vRes(I)=72<<16, pixelType(H)=0, pixelSize(H)=8, cmpCount(H)=1, cmpSize(H)=8,
    # planeBytes(I)=0, pmTable(I)=0, pmReserved(I)=0
    pixmap = struct.pack(">HhhhhHHIIIHHHHIII",
                         0x8004, 0, 0, 2, 4, 0, 0, 0,
                         72 << 16, 72 << 16, 0, 8, 1, 8, 0, 0, 0)
    # ColorTable: ctSeed(I)=0, ctFlags(H)=0, ctSize(H)=1 (2 entries: idx 0=red, idx 1=blue)
    ctable = struct.pack(">IHH", 0, 0, 1)
    ctable += struct.pack(">HHHH", 0, 65535, 0, 0)
    ctable += struct.pack(">HHHH", 1, 0, 0, 65535)
    src_dst_mode = struct.pack(">hhhhhhhhH", 0, 0, 2, 4, 0, 0, 2, 4, 0)
    # 2 rows of 4 unpacked bytes (since rowBytes < 8 in PICT spec, rows are stored raw uncompressed!)
    row0 = bytes([0, 0, 1, 1])
    row1 = bytes([1, 1, 0, 0])
    end_op = struct.pack(">H", 0x00FF)
    return bytes(hdr) + pict_hdr + op + pixmap + ctable + src_dst_mode + row0 + row1 + end_op


def _make_pict_v2_16bit_direct() -> bytes:
    """Construct a valid synthetic 2x1 16-bit DirectBitsRect PICT v2 image."""
    pict_hdr = struct.pack(">Hhhhh", 0, 0, 0, 1, 2) + b"\x00\x11\x02\xff"
    op = struct.pack(">H", 0x009A)
    # baseAddr(I)=0xFF + PixMap(rowBytes=0x8004, bounds=(0,0,1,2), packType=3 uncompressed)
    pixmap = struct.pack(">IHhhhhHHIIIHHHHIII",
                         0x000000FF,
                         0x8004, 0, 0, 1, 2, 0, 3, 0,
                         72 << 16, 72 << 16, 16, 16, 3, 5, 0, 0, 0)
    src_dst_mode = struct.pack(">hhhhhhhhH", 0, 0, 1, 2, 0, 0, 1, 2, 0)
    # 2 pixels RGB555: pure green (0x03E0), pure white (0x7FFF)
    row = struct.pack(">HH", 0x03E0, 0x7FFF)
    return pict_hdr + op + pixmap + src_dst_mode + row + struct.pack(">H", 0x00FF)


class SchemaAndApplyTests(unittest.TestCase):
    def test_new_310_settings_and_no_kidproofing(self):
        by_key = {e["key"]: e for e in schema.describe()}
        self.assertEqual(schema.BY_KEY["audio.startup_chime"].group, "audio")
        self.assertEqual(by_key["audio.startup_chime"]["group"], "Sound")
        self.assertEqual(by_key["audio.startup_chime"]["apply"], "live")
        self.assertEqual(by_key["audio.startup_chime"]["default"], "auto")
        self.assertEqual([c["value"] for c in by_key["audio.startup_chime"]["choices"]],
                         ["auto", "os9", "quadra", "powermac", "macii", "se", "off"])
        self.assertEqual(schema.BY_KEY["audio.output"].group, "audio")
        self.assertEqual(by_key["audio.output"]["default"], "auto")
        self.assertEqual(by_key["audio.output"]["apply"], "guest_restart")
        self.assertEqual(schema.BY_KEY["input.gamepad_mode"].group, "input")
        self.assertEqual([c["value"] for c in by_key["input.gamepad_mode"]["choices"]], ["arrows", "wasd", "off"])
        self.assertEqual(by_key["input.gamepad_mode"]["apply"], "live")
        self.assertEqual(by_key["input.natural_scroll"]["default"], False)
        self.assertEqual(by_key["input.natural_scroll"]["apply"], "live")
        self.assertEqual(schema.BY_KEY["printer.enabled"].group, "storage")
        self.assertEqual(by_key["printer.enabled"]["group"], "Storage")
        self.assertEqual(by_key["printer.enabled"]["default"], True)
        self.assertEqual(by_key["printer.enabled"]["apply"], "live")
        self.assertEqual(schema.BY_KEY["usb_share.enabled"].group, "storage")
        self.assertEqual(by_key["usb_share.enabled"]["group"], "Storage")
        self.assertEqual(by_key["usb_share.enabled"]["default"], True)
        self.assertEqual(by_key["usb_share.enabled"]["apply"], "live")
        # User explicitly opted out of Kid Lock / PIN / SafeSearch / Allowlist.
        for k in by_key:
            low = k.lower()
            self.assertNotIn("kid", low)
            self.assertNotIn("pin", low)
            self.assertNotIn("safesearch", low)
            self.assertNotIn("allowlist", low)

    def test_validate_new_settings_with_outputs_context(self):
        ctx = schema.ValidationContext(audio_outputs=["auto", "hdmi:0", "analog:1"])
        base = schema.defaults()
        ok = schema.validate_changes(base, {"audio.output": "hdmi:0", "audio.startup_chime": "quadra",
                                            "input.gamepad_mode": "wasd", "input.natural_scroll": True,
                                            "printer.enabled": False, "usb_share.enabled": False}, ctx=ctx)
        self.assertEqual(ok["input.gamepad_mode"], "wasd")
        with self.assertRaises(schema.ValidationError):
            schema.validate_changes(base, {"audio.output": "bad;cmd"}, ctx=ctx)
        with self.assertRaises(schema.ValidationError):
            schema.validate_changes(base, {"audio.startup_chime": "bogus"}, ctx=ctx)
        with self.assertRaises(schema.ValidationError):
            schema.validate_changes(base, {"input.gamepad_mode": "dpad"}, ctx=ctx)


class ApplyAndApiFeatureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        helpers.make_package(self.root)
        self.cfg = helpers.make_config(self.root, allow_network_tools=False)
        self.app = Hostd(self.cfg)
        self.app.loop = asyncio.get_running_loop()
        self.app.settings.store.load()
        self.app.web.tls()
        self.client = TestClient(TestServer(self.app.web.web_app, host="127.0.0.1"))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        code, _ = self.app.pairing.current_code()
        r = await self.client.post("/api/v1/pair", json={"code": code, "device_name": "test"})
        self.cookie = r.headers.getall("Set-Cookie")[0].split("rb_session=", 1)[1].split(";", 1)[0]
        self.csrf = (await r.json())["csrf"]

    def h(self, extra=None):
        hd = {"Cookie": f"rb_session={self.cookie}", "X-RetroBoot-CSRF": self.csrf}
        if extra:
            hd.update(extra)
        return hd

    async def test_live_set_config_and_audio_output_sync(self):
        sent = []
        self.app.presenter.connected.set()

        async def fake_try_request(cmd, **kw):
            sent.append({"t": cmd, **kw})
            return {"ok": True}

        self.app.presenter.try_request = fake_try_request
        called = {"audio": 0, "printer": 0, "usb": 0}
        async def fake_sync(_out=None):
            called["audio"] += 1
            return True

        self.app.sync_audio_output = fake_sync
        self.app.printer.sync = AsyncMock(side_effect=lambda: called.__setitem__("printer", called["printer"] + 1) or {})
        self.app.usbshare.sync = AsyncMock(side_effect=lambda: called.__setitem__("usb", called["usb"] + 1) or [])
        await self.app.apply_engine.change_and_apply({
            "input.gamepad_mode": "wasd",
            "input.natural_scroll": True,
            "audio.output": "hdmi-test",
            "printer.enabled": False,
            "usb_share.enabled": False,
        }, origin="web")
        cfg_msgs = [m for m in sent if m.get("t") == "set_config"]
        self.assertTrue(cfg_msgs)
        self.assertEqual(cfg_msgs[-1]["gamepad_mode"], "wasd")
        self.assertEqual(cfg_msgs[-1]["natural_scroll"], True)
        self.assertGreaterEqual(called["audio"], 1)
        self.assertGreaterEqual(called["printer"], 1)
        self.assertGreaterEqual(called["usb"], 1)

    async def test_privacy_mask_screen_endpoints_and_hub_swap(self):
        raw_outer = encode.encode_png(bytes([10, 20, 30, 255] * (64 * 48)), 64 * 4, (0, 0, 64, 48))

        async def fake_try_req(cmd, **kw):
            if cmd == "screenshot" and "path" in kw:
                Path(kw["path"]).write_bytes(raw_outer)
                return {"ok": True}
            return {"ok": True}

        self.app.presenter.connected.set()
        self.app.presenter.try_request = fake_try_req

        # Unmasked normally.
        self.app.panel = SimpleNamespace(is_pairing_visible=lambda: False)
        self.assertFalse(self.app.is_pairing_visible())
        r = await self.client.get("/api/v1/screen", headers=self.h())
        self.assertEqual(r.status, 200)
        self.assertNotIn("X-RetroBoot-Masked", r.headers)
        self.assertEqual(await r.read(), raw_outer)

        # Masked when NetworkWeb / NetworkStep pairing is visible!
        self.app.panel = SimpleNamespace(is_pairing_visible=lambda: True)
        self.assertTrue(self.app.is_pairing_visible())
        for ep in ("/api/v1/screen", "/api/v1/screen.png", "/api/v1/diagnostics/screen"):
            r = await self.client.get(ep, headers=self.h())
            self.assertEqual(r.status, 200)
            self.assertEqual(r.headers.get("X-RetroBoot-Masked"), "1", ep)
            body = await r.read()
            self.assertTrue(body.startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertNotEqual(body, raw_outer)

        # Installer mode also masks cleanly when pairing panel reports visible.
        self.app.mode = "install"
        self.assertEqual((await self.client.get("/api/v1/screen", headers=self.h())).headers.get("X-RetroBoot-Masked"), "1")
        self.app.mode = "normal"

        # remote/hub.py:664 swap applies consistently in PANEL and GUEST modes, while keeping recovery chord intact.
        hub = self.app.remote
        sent_keys = []
        hub.tap.key = lambda code, down: sent_keys.append((code, down))
        hub.tap.sock = object()
        self.app.panel = SimpleNamespace(is_pairing_visible=lambda: False)
        self.app.settings.set_desired_and_applied({"input.swap_command_option": True},
                                                self.app.validation_context())
        v = hub_mod.Viewer(SimpleNamespace(closed=False), None, {"id": "d1"},
                           {"platform": "windows"}, True, "control")
        hub.viewers[v.id] = v
        hub.source = "panel"
        await hub.on_message(v, {"t": "key", "code": "AltLeft", "down": True})
        await hub.on_message(v, {"t": "key", "code": "ControlLeft", "down": True})
        await hub.on_message(v, {"t": "key", "code": "Delete", "down": True})
        await asyncio.sleep(0.25)
        codes = [c for c, down in sent_keys if down]
        # AltLeft swapped -> LEFTMETA (125); Ctrl+Alt+Delete recovery preserves LEFTALT (56)!
        self.assertIn(keymap.EVDEV["LEFTMETA"], codes)
        self.assertIn(keymap.EVDEV["LEFTALT"], codes)

        hub._release_viewer(v)
        hub.source = "guest"
        sent_keys.clear()
        await hub.on_message(v, {"t": "key", "code": "MetaLeft", "down": True})
        self.assertEqual(sent_keys[-1], (keymap.EVDEV["LEFTALT"], True))

    async def test_fresh_install_checkpoint_machine_settings_recovery_and_chime(self):
        # Seed Tiger Deluxe catalog entry with recommendedSettings and import cleanly using fake prep.
        async def fake_apply(disk):
            return {"result": "applied", "report": {"os_version": "10.4.11", "os_build": "8S165"}, "error": None}
        self.app.supervisor._prep = lambda m: {"version": lambda: 1, "apply": fake_apply}

        tiger_tar = self.root / "tiger-deluxe.tar.gz"
        cfg_json = json.dumps({
            "name": "Mac OS X 10.4 Deluxe",
            "machineFamily": "powerMacOSX",
            "guestOS": "macosx-10.4",
            "memoryMB": 512,
            "recommendedSettings": {"ram_mb": 2048, "graphics": "ati-radeon"},
        }).encode()
        disk_buf = bytearray(1024 * 1024)
        disk_buf[0:2] = b"ER"
        disk_buf[4096:6144] = os.urandom(2048)
        _make_archive(tiger_tar, [("config.json", cfg_json), ("disk.img", bytes(disk_buf))])

        import hashlib
        import zlib
        from retroboot import osx
        pkg_dir = self.app.paths.qemu_osx_package
        (pkg_dir / "share/qemu").mkdir(parents=True, exist_ok=True)
        seed_bytes = bytearray(osx.NVRAM_BYTES)
        for off in (0, 8192):
            seed_bytes[off] = 0x5A
            struct.pack_into(">H", seed_bytes, off + 2, 2)
            seed_bytes[off + 1] = osx._header_sum(bytes(seed_bytes[off:off + 16]))
            struct.pack_into(">I", seed_bytes, off + 16, zlib.adler32(bytes(seed_bytes[off + 20:off + 8192]), 1))
        nv_seed = pkg_dir / osx.NVRAM_SEED
        nv_seed.write_bytes(bytes(seed_bytes))
        seed_hash = hashlib.sha256(seed_bytes).hexdigest()
        (pkg_dir / "manifest.json").write_text(json.dumps({
            "features": [osx.FEATURE, osx.QEMU_FEATURE, osx.NVRAM_FEATURE, osx.FIRMWARE_FEATURE, "display-retroboot"],
            "sources": {"qemu": osx.QEMU_SOURCE},
            "files": {osx.NVRAM_SEED: {"sha256": seed_hash}},
        }))

        with patch.object(self.app, "platform", SimpleNamespace(
            ram_mb=8192, has_gl=True, features={"gl": True}, family="pc", board="pc",
            machine="x86_64", is_zero2w=False, is_raspberry_pi=False, is_gemini=False,
            has_grub=True, machine_families=frozenset(("powermac", "quadra800")),
            machine_unavailable=lambda f: None, tcg_tb_size_mb=512,
        )), patch.object(self.app.graphics, "capable", return_value=True), \
                patch.object(self.app.graphics, "ui_context", return_value={"available": True, "capable": True}):
            r = await self.client.post("/api/v1/machines/import?name=Tiger", data=tiger_tar.read_bytes(),
                                       headers=self.h({"Content-Type": "application/gzip"}))
            self.assertEqual(r.status, 202, await r.text())
            job = await (await self.client.get(f"/api/v1/jobs/{(await r.json())['job']['id']}",
                                               headers=self.h())).json()
            while job["state"] in ("queued", "running"):
                await asyncio.sleep(0.05)
                job = await (await self.client.get(f"/api/v1/jobs/{job['id']}", headers=self.h())).json()
            self.assertEqual(job["state"], "succeeded", job)

        mid = job["result"]["machine"]
        # Automatic Fresh Install checkpoint created!
        cps = await (await self.client.get(f"/api/v1/machines/{mid}/checkpoints", headers=self.h())).json()
        self.assertTrue(any(c.get("kind") == "fresh-install" or c.get("label") == "Fresh Install"
                            for c in cps["checkpoints"]))

        # Per-machine settings seeded (Tiger Deluxe 2048 + ati-radeon) and updatable via GET/PATCH.
        ms_resp = await (await self.client.get(f"/api/v1/machines/{mid}/settings", headers=self.h())).json()
        self.assertEqual(ms_resp["settings"]["osx_ram_mb"], 2048)
        self.assertEqual(ms_resp["settings"]["graphics"], "ati-radeon")
        p_resp = await self.client.patch(f"/api/v1/machines/{mid}/settings",
                                         json={"ram_mb": 1024, "graphics": "standard"}, headers=self.h())
        self.assertEqual(p_resp.status, 200, await p_resp.text())
        self.assertEqual((await p_resp.json())["settings"]["osx_ram_mb"], 1024)
        # Also supports wrapped {"settings": ...} payload (web UI compatibility).
        p_resp2 = await self.client.patch(f"/api/v1/machines/{mid}/settings",
                                          json={"settings": {"osx_ram_mb": 1536}}, headers=self.h())
        self.assertEqual(p_resp2.status, 200, await p_resp2.text())
        self.assertEqual((await p_resp2.json())["settings"]["osx_ram_mb"], 1536)

        # Import a Classic Mac OS 9.2.2 archive -> auto Fresh Install checkpoint + reset-fresh + corrupt recovery.
        os9_tar = self.root / "os9-rec.tar.gz"
        os9_cfg = json.dumps({
            "name": "Mac OS 9.2.2",
            "machineFamily": "powerMacG4",
            "guestOS": "classic",
            "memoryMB": 512,
        }).encode()
        _make_archive(os9_tar, [("config.json", os9_cfg), ("disk.img", bytes(disk_buf))])
        r9 = await self.client.post("/api/v1/machines/import?name=OS9Test", data=os9_tar.read_bytes(),
                                    headers=self.h({"Content-Type": "application/gzip"}))
        self.assertEqual(r9.status, 202, await r9.text())
        j9 = await (await self.client.get(f"/api/v1/jobs/{(await r9.json())['job']['id']}",
                                          headers=self.h())).json()
        while j9["state"] in ("queued", "running"):
            await asyncio.sleep(0.05)
            j9 = await (await self.client.get(f"/api/v1/jobs/{j9['id']}", headers=self.h())).json()
        self.assertEqual(j9["state"], "succeeded", j9)
        m9_id = j9["result"]["machine"]
        cps9 = await (await self.client.get(f"/api/v1/machines/{m9_id}/checkpoints", headers=self.h())).json()
        self.assertTrue(any(c.get("kind") == "fresh-install" or c.get("label") == "Fresh Install"
                            for c in cps9["checkpoints"]))

        # Corrupt disk recovery notice + restore clean checkpoint / reset to Fresh Install.
        shown = []
        self.app.panel = SimpleNamespace(
            is_pairing_visible=lambda: False,
            show_corrupt_disk_recovery=lambda info: shown.append(info),
        )
        m9 = self.app.machines.get(m9_id)
        m9.disk.write_bytes(b"CORRUPTED")
        self.app.on_corrupt_disk_recovery({"machine_id": m9_id, "machine_name": m9.name, "error": "fsck failed"})
        await asyncio.sleep(0)
        self.assertTrue(shown)
        st = await (await self.client.get("/api/v1/status", headers=self.h())).json()
        self.assertEqual(st["corrupt_disk_recovery"]["machine_id"], m9_id)
        rj = await self.client.post(f"/api/v1/machines/{m9_id}/reset-fresh", headers=self.h())
        self.assertEqual(rj.status, 202, await rj.text())
        rjob = await (await self.client.get(f"/api/v1/jobs/{(await rj.json())['job']['id']}", headers=self.h())).json()
        while rjob["state"] in ("queued", "running"):
            await asyncio.sleep(0.05)
            rjob = await (await self.client.get(f"/api/v1/jobs/{rjob['id']}", headers=self.h())).json()
        self.assertEqual(rjob["state"], "succeeded", rjob)
        self.assertEqual(m9.disk.read_bytes()[:2], b"ER")
        st2 = await (await self.client.get("/api/v1/status", headers=self.h())).json()
        self.assertIsNone(st2["corrupt_disk_recovery"])
        m = self.app.machines.get(mid)

        # Audio outputs & Test Sound API (works with fallback and with @hostd-audio-chimes audio.list_outputs).
        outs_fallback = await (await self.client.get("/api/v1/audio/outputs", headers=self.h())).json()
        self.assertTrue(any(o["id"] == "auto" for o in outs_fallback["outputs"]))
        self.app.supervisor.play_startup_chime = AsyncMock()
        self.app.supervisor.test_sound = AsyncMock(return_value={"ok": True, "output": "auto"})
        with patch("retroboot.audio.list_outputs", create=True,
                   return_value=[{"id": "auto", "label": "Automatic"}, {"id": "hdmi", "label": "HDMI"}]):
            outs = await (await self.client.get("/api/v1/audio/outputs", headers=self.h())).json()
            self.assertEqual([o["id"] for o in outs["outputs"]], ["auto", "hdmi"])
        tr = await self.client.post("/api/v1/audio/test", json={"output": "auto"}, headers=self.h())
        self.assertEqual(tr.status, 200)
        self.app.supervisor.test_sound.assert_awaited()

        # Startup chime lifecycle hook plays when enabled, skips when off.
        await self.app.play_startup_chime(m)
        self.app.supervisor.play_startup_chime.assert_awaited()
        self.app.supervisor.play_startup_chime.reset_mock()
        self.app.settings.applied["audio.startup_chime"] = "off"
        await self.app.play_startup_chime(m)
        self.app.supervisor.play_startup_chime.assert_not_awaited()

    async def test_files_preview_pict_zip_move_and_usb_routes(self):
        # PICT preview -> PNG
        p_bytes = _make_pict_v2_8bit()
        mk = await self.client.post("/api/v1/share/mkdir", json={"path": "", "name": "Art"}, headers=self.h())
        self.assertEqual(mk.status, 201, await mk.text())
        r = await self.client.post("/api/v1/share/upload?path=Art&name=Sample.pict", data=p_bytes,
                                   headers=self.h({"Content-Type": "application/octet-stream"}))
        self.assertEqual(r.status, 201, await r.text())
        pr = await self.client.get("/api/v1/files/preview?path=Art/Sample.pict", headers=self.h())
        self.assertEqual(pr.status, 200)
        self.assertEqual(pr.headers["Content-Type"], "image/png")
        self.assertTrue((await pr.read()).startswith(b"\x89PNG\r\n\x1a\n"))

        # PDF inline preview headers (SAMEORIGIN for iframe!).
        r = await self.client.post("/api/v1/share/upload?path=Art&name=Doc.pdf",
                                   data=b"%PDF-1.4\n%%EOF\n",
                                   headers=self.h({"Content-Type": "application/octet-stream"}))
        self.assertEqual(r.status, 201, await r.text())
        pdf_r = await self.client.get("/api/v1/files/preview?path=Art/Doc.pdf", headers=self.h())
        self.assertEqual(pdf_r.status, 200)
        self.assertEqual(pdf_r.headers.get("X-Frame-Options"), "SAMEORIGIN")
        self.assertIn("frame-ancestors 'self'", pdf_r.headers.get("Content-Security-Policy", ""))

        # MacRoman text preview decoded cleanly -> UTF-8
        up_txt = await self.client.post("/api/v1/share/upload?path=Art&name=Readme.txt",
                                        data=b"Caf\x8e\rLine 2",
                                        headers=self.h({"Content-Type": "application/octet-stream"}))
        self.assertEqual(up_txt.status, 201, await up_txt.text())
        tr = await self.client.get("/api/v1/files/preview?path=Art/Readme.txt", headers=self.h())
        self.assertEqual(tr.status, 200)
        self.assertIn("Café", await tr.text())

        # Path traversal rejected!
        bad = await self.client.get("/api/v1/files/preview?path=../../etc/passwd", headers=self.h())
        self.assertIn(bad.status, (400, 404, 422))

        # Folder ZIP download
        zr = await self.client.get("/api/v1/files/zip?path=Art", headers=self.h())
        self.assertEqual(zr.status, 200)
        zf = zipfile.ZipFile(io.BytesIO(await zr.read()))
        self.assertTrue(any("Sample.pict" in n for n in zf.namelist()))

        # Move into folder (and verify folder stays intact).
        mk2 = await self.client.post("/api/v1/share/mkdir", json={"path": "", "name": "Dest"}, headers=self.h())
        self.assertEqual(mk2.status, 201)
        mv = await self.client.post("/api/v1/files/move",
                                    json={"path": "Art/Doc.pdf", "to_dir": "Dest"}, headers=self.h())
        self.assertEqual(mv.status, 200, await mv.text())
        self.assertTrue((self.app.paths.share / "Dest" / "Doc.pdf").is_file())
        self.assertFalse((self.app.paths.share / "Art" / "Doc.pdf").exists())

        # USB drives API routes.
        with patch.object(self.app.cfg, "allow_network_tools", True), \
                patch.object(self.app.usbshare, "refresh", AsyncMock(return_value=[
            {"device": "/dev/sdb1", "label": "Thumb", "share_path": "USB Drives/Thumb",
             "fstype": "vfat", "bytes": 1024, "free_bytes": 512, "read_only": False}
        ])), patch.object(self.app.usbshare, "eject", AsyncMock(return_value={"ok": True, "ejected": "/dev/sdb1"})):
            dr = await self.client.get("/api/v1/usb/drives", headers=self.h())
            self.assertEqual(dr.status, 200)
            self.assertEqual((await dr.json())["drives"][0]["label"], "Thumb")
            ej = await self.client.post("/api/v1/usb/eject", json={"device": "/dev/sdb1"}, headers=self.h())
            self.assertEqual(ej.status, 200)
            self.assertTrue((await ej.json())["ok"])

    async def test_kvm_ws_audio_streaming(self):
        import aiohttp

        async def fake_audio_src():
            yield 48000, 2, struct.pack("<hhhh", 100, -100, 200, -200)

        self.app.remote.audio_provider = fake_audio_src
        self.app.presenter.connected.set()

        async def fake_tap_connect(src):
            self.app.remote.tap.sock = object()

        async def fake_tap_update(wait):
            await asyncio.sleep(0.05)
            return []

        self.app.remote.tap.connect = fake_tap_connect
        self.app.remote.tap.update = fake_tap_update
        self.app.remote.tap.close = lambda *a: setattr(self.app.remote.tap, "sock", None)
        self.app.remote.tap.viewers = lambda *a: None
        tls_srv = TestServer(self.app.web.web_app, host="127.0.0.1")
        await tls_srv.start_server(ssl=self.app.web.tls())
        self.addAsyncCleanup(tls_srv.close)
        base = f"127.0.0.1:{tls_srv.port}"
        sess = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False))
        self.addAsyncCleanup(sess.close)
        self.addAsyncCleanup(self.app.remote.close)
        code, _ = self.app.pairing.current_code()
        pr = await sess.post(f"https://{base}/api/v1/pair", json={"code": code, "device_name": "ws-audio"},
                             headers={"Origin": f"https://{base}"})
        self.assertEqual(pr.status, 200)
        cookie = pr.cookies["rb_session"].value
        csrf = (await pr.json())["csrf"]
        ws = await sess.ws_connect(f"wss://{base}/api/v1/screen/ws",
                                   headers={"Cookie": f"rb_session={cookie}", "Origin": f"https://{base}"})
        await ws.send_json({"t": "hello", "v": 1, "csrf": csrf, "platform": "mac", "enc": ["png"]})
        hello = json.loads(await ws.receive_str())
        self.assertEqual(hello["t"], "hello")
        await ws.send_json({"t": "audio", "on": True})
        got_audio = None
        for _ in range(20):
            msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
            if isinstance(msg.data, (bytes, bytearray)) and msg.data[:1] == b"A":
                got_audio = msg.data
                break
        await ws.send_json({"t": "audio", "on": False})
        await ws.close()
        self.assertIsNotNone(got_audio)
        kind, rate, ch = struct.unpack_from(">BIB", got_audio, 0)
        self.assertEqual((kind, rate, ch), (0x41, 48000, 2))


class CatalogAndMaintenanceTests(unittest.TestCase):
    def test_first_boot_picks_and_recommendations(self):
        # Mock choices mimicking RetroBoot library.
        def item(iid, name, guest_os, family, installed=False, size=500 * 1024 ** 2):
            return {
                "id": iid, "name": name, "guest_os": guest_os, "family": family,
                "installed": installed, "compatible": True, "size": size, "install_bytes": size * 2,
                "tags": ["recommended"] if "mega" in iid else [], "highlights": ["Kid Pix"],
                "recommendedSettings": {"ram_mb": 512},
            }

        items = [
            item("os921-full-v4", "Mac OS 9.2.1", "macos-9.2.1", "powermac"),
            item("os922-full-v5", "Mac OS 9.2.2", "macos-9.2.2", "powermac", size=400 * 1024 ** 2),
            item("os922-mega-deluxe-v4", "Mac OS 9.2.2 Mega Deluxe", "macos-9.2.2", "powermac",
                 size=4 * 1024 ** 3),
            item("sys755-deluxe", "System 7.5.5 Deluxe", "macos-7.5.5", "quadra800", size=300 * 1024 ** 2),
            item("sys755-full", "System 7.5.5", "macos-7.5.5", "quadra800", size=200 * 1024 ** 2),
        ]
        deluxe, base = catalogs.first_boot_picks(items)
        self.assertEqual(deluxe["id"], "os922-mega-deluxe-v4")
        self.assertEqual(base["id"], "os922-full-v5")
        # Zero 2 W constrained disk fits only Quadra / smaller picks -> no huge deluxe crash.
        deluxe2, base2 = catalogs.first_boot_picks(items[3:], max_bytes=1024 ** 3)
        self.assertEqual((deluxe2["id"], base2["id"]), ("sys755-deluxe", "sys755-full"))
        deluxe3, base3 = catalogs.first_boot_picks([items[4]], max_bytes=1024 ** 3)
        self.assertEqual((deluxe3, base3["id"]), (None, "sys755-full"))

        # Smart recommendation: when 9.2.2 standard is installed, recommend mega-deluxe (NEVER 9.2.1!).
        installed_922 = [dict(i, installed=(i["id"] == "os922-full-v5")) for i in items]
        self.assertEqual(catalogs.recommended(installed_922)["id"], "os922-mega-deluxe-v4")
        # Once 9.2.2 Mega Deluxe is installed, recommend nothing (no 9.2.1 downgrade!).
        installed_mega = [dict(i, installed=(i["id"] == "os922-mega-deluxe-v4")) for i in items]
        self.assertIsNone(catalogs.recommended(installed_mega))

    def test_pict_decoder_variants(self):
        png1, mime1 = pict.decode_pict_to_image(_make_pict_v2_8bit())
        self.assertEqual(mime1, "image/png")
        self.assertTrue(png1.startswith(b"\x89PNG\r\n\x1a\n"))

        png2, mime2 = pict.decode_pict_to_image(_make_pict_v2_16bit_direct())
        self.assertEqual(mime2, "image/png")
        self.assertTrue(png2.startswith(b"\x89PNG\r\n\x1a\n"))

        # Embedded QuickTime JPEG opcode 0x8200.
        jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\xff\xd9"
        qt_pict = (struct.pack(">Hhhhh", 0, 0, 0, 10, 10) + b"\x00\x11\x02\xff"
                   + struct.pack(">HI", 0x8200, len(jpeg)) + jpeg + struct.pack(">H", 0x00FF))
        out, mime3 = pict.decode_pict_to_image(qt_pict)
        self.assertEqual((mime3, out), ("image/jpeg", jpeg))

    def test_labauth_enoent_is_debug_and_guestio_ppm_padding(self):
        # Missing /etc/retroboot/lab-auth.env logs at DEBUG, not WARNING (#38).
        td, root = helpers.scratch()
        self.addCleanup(td.cleanup)
        missing = root / "no-such-lab-auth.env"
        with self.assertLogs("retroboot.labauth", level=logging.DEBUG) as cm:
            self.assertIsNone(labauth.load(missing))
        self.assertTrue(any(r.levelno == logging.DEBUG for r in cm.records))
        self.assertFalse(any(r.levelno >= logging.WARNING for r in cm.records))

        # 4-byte padded scanline stride for 1366x768 Proxmox PPM (#24).
        guestio = _load_guestio()
        w, h = 1366, 768
        stride = (w * 3 + 3) & ~3
        self.assertEqual(stride, 4100)
        hdr = f"P6\n{w} {h}\n255\n".encode()
        row = bytes([1, 2, 3]) * w + b"\x99\x99"  # 2 padding bytes to 4100
        ppm_path = root / "1366.ppm"
        ppm_path.write_bytes(hdr + row * h)
        img = guestio.read_ppm(ppm_path.read_bytes())
        self.assertEqual((img.width, img.height, len(img.rgb)), (w, h, w * h * 3))
        self.assertEqual(img.rgb[:6], bytes([1, 2, 3, 1, 2, 3]))
        self.assertEqual(img.rgb[-3:], bytes([1, 2, 3]))


class PrinterAndUsbShareTests(unittest.IsolatedAsyncioTestCase):
    async def test_lpd_and_ipp_print_to_pdf(self):
        td, root = helpers.scratch()
        self.addCleanup(td.cleanup)
        printed = []

        async def mock_conv(ps: bytes, out_pdf: Path):
            out_pdf.write_bytes(b"%PDF-1.4\n%converted:" + ps[:16] + b"\n%%EOF\n")

        srv = printer.PrintServer(root, enabled=True, on_printed=printed.append,
                                  lpd_port=0, ipp_port=0, converter=mock_conv)
        await srv.start()
        self.addAsyncCleanup(srv.stop)
        self.assertTrue(srv.running)

        # LPD RFC 1179 job.
        r, w = await asyncio.open_connection("127.0.0.1", srv.bound_lpd_port)
        w.write(b"\x02lp\n")
        self.assertEqual(await r.readexactly(1), b"\x00")
        ctrl = b"JClassic Letter.ps\n"
        w.write(f"\x02{len(ctrl)} cfA001mac\n".encode() + ctrl + b"\x00")
        self.assertEqual(await r.readexactly(1), b"\x00")
        self.assertEqual(await r.readexactly(1), b"\x00")
        ps = b"%!PS-Adobe-3.0\nshowpage\n%%EOF\n"
        w.write(f"\x03{len(ps)} dfA001mac\n".encode() + ps + b"\x00")
        self.assertEqual(await r.readexactly(1), b"\x00")
        self.assertEqual(await r.readexactly(1), b"\x00")
        w.close()
        await helpers.wait_for(lambda: len(printed) >= 1, 5, 0.05, "LPD PDF written")
        self.assertIn("Classic Letter", printed[0]["name"])
        self.assertTrue((root / printed[0]["path"]).is_file())

        # IPP RFC 8010/8011 Print-Job with Expect: 100-continue + chunked body.
        ipp_attr = struct.pack(">BBHI", 1, 1, 0x0002, 42) + b"\x01" + \
            b"\x42\x00\x08job-name\x00\x09MacPaint1\x03" + b"%PDF-1.4\n%%EOF\n"
        ir, iw = await asyncio.open_connection("127.0.0.1", srv.bound_ipp_port)
        iw.write(b"POST /printers/RetroBoot HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n"
                 b"Expect: 100-continue\r\nTransfer-Encoding: chunked\r\n\r\n")
        resp1 = await ir.readuntil(b"\r\n\r\n")
        self.assertIn(b"100 Continue", resp1)
        iw.write(f"{len(ipp_attr):x}\r\n".encode() + ipp_attr + b"\r\n0\r\n\r\n")
        await iw.drain()
        http_resp = await asyncio.wait_for(ir.read(), 5)
        self.assertIn(b"200 OK", http_resp)
        iw.close()
        await helpers.wait_for(lambda: len(printed) >= 2, 5, 0.05, "IPP PDF written")
        self.assertIn("MacPaint1", printed[1]["name"])

    async def test_usb_share_filtering_mount_and_eject(self):
        td, root = helpers.scratch()
        self.addCleanup(td.cleanup)
        lsblk_payload = json.dumps({"blockdevices": [
            {"name": "sda", "path": "/dev/sda", "rm": False, "hotplug": False, "tran": "sata",
             "type": "disk", "children": [
                 {"name": "sda1", "path": "/dev/sda1", "fstype": "vfat", "label": "RBEFI", "mountpoint": "/boot/efi"},
                 {"name": "sda3", "path": "/dev/sda3", "fstype": "ext4", "label": "RBROOT_A", "mountpoint": "/"},
             ]},
            {"name": "sdb", "path": "/dev/sdb", "rm": True, "hotplug": True, "tran": "usb",
             "type": "disk", "children": [
                 {"name": "sdb1", "path": "/dev/sdb1", "fstype": "vfat", "label": "My Files",
                  "mountpoint": "", "size": 128 * 1024 ** 2, "pkname": "sdb"},
             ]},
        ]})
        mounted_calls, umount_calls = [], []

        async def fake_run(cmd, timeout=10):
            if cmd[0] == "lsblk":
                return 0, lsblk_payload, ""
            return 0, "", ""

        async def fake_mount(dev, target, fstype):
            mounted_calls.append((dev, target, fstype))
            target.mkdir(parents=True, exist_ok=True)
            return True, False

        async def fake_umount(target, disk_dev=None):
            umount_calls.append((target, disk_dev))
            return True

        us = usbshare.UsbShare(root, enabled=True, runner=fake_run, poll_s=0.0,
                               mount_fn=fake_mount, umount_fn=fake_umount)
        drives = await us.refresh()
        self.assertEqual(len(drives), 1)
        self.assertEqual(drives[0]["device"], "/dev/sdb1")
        self.assertEqual(drives[0]["share_path"], "USB Drives/My Files")
        self.assertEqual(mounted_calls[0][0], "/dev/sdb1")

        # Eject unmounts and stays ignored while still attached in lsblk.
        res = await us.eject("/dev/sdb1")
        self.assertTrue(res["ok"])
        self.assertEqual(await us.refresh(), [])
        self.assertEqual(umount_calls[0][1], "/dev/sdb")


if __name__ == "__main__":
    unittest.main()
