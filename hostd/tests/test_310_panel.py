"""Tests for RetroBoot 3.1.0 Platinum Control Panel & Installer OOBE improvements."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from retroboot import osx as _osx, schema
from retroboot.catalog import download_of, validate_catalog
from retroboot.catalogs import Catalogs
from retroboot.jobs import Job, SUCCEEDED
from retroboot.panel import ui
from retroboot.panel.controller import (
    CatalogList,
    CorruptDiskRecovery,
    DisplaySound,
    KeepSetting,
    MacOptions,
    NetworkWeb,
    Panel,
    SetupWelcome,
    StartupDisk,
)
from retroboot.panel.installer import DisplayCheck, HardwareCheck, InstallProgress, InstallerPanel, NetworkStep, _dedupe_model
from retroboot.panel.picker import ChooseMacs
from retroboot.panel.update import SoftwareUpdate
from retroboot.settings import NothingToConfirm


PUBLIC_CATALOG = Path(__file__).resolve().parents[2] / "lab" / "catalogs" / "retroboot-public.json"


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for x in node:
            yield from _walk(x)


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


def _all_text(doc: dict) -> str:
    parts = []
    for n in _walk(doc):
        for key in ("text", "label", "title", "message", "detail"):
            v = n.get(key)
            if isinstance(v, str):
                parts.append(v)
    return "\n".join(parts)


class _MemSettings:
    def __init__(self):
        self.desired = schema.defaults()
        self.applied = dict(self.desired)
        self.revision = 1
        self.pending_confirm = None

    def patch(self, rev: int, changes: dict, ctx=None):
        validated = schema.validate_changes(self.desired, changes, ctx)
        self.desired.update(validated)
        self.revision += 1
        return validated

    def set_desired_and_applied(self, changes: dict, ctx=None):
        self.desired.update(changes)
        self.applied.update(changes)
        self.revision += 1

    def pending_by_policy(self):
        return {schema.LIVE: [], schema.GUEST_RESTART: [], schema.HOST_REBOOT: []}

    def pending_confirm_public(self):
        return self.pending_confirm


class _FakeApplyEngine:
    def __init__(self, settings: _MemSettings):
        self.settings = settings
        self.confirm_exc: Exception | None = None
        self.reverted = 0
        self.next_result: dict = {}

    async def change_and_apply(self, changes: dict, origin: str = "panel"):
        self.settings.patch(self.settings.revision, changes, None)
        if not self.next_result.get("confirm"):
            self.settings.applied.update(changes)
        return dict(self.next_result)

    async def confirm(self, origin: str = "panel"):
        if self.confirm_exc is not None:
            raise self.confirm_exc
        self.settings.pending_confirm = None

    async def revert_now(self, origin: str = "panel"):
        self.reverted += 1
        self.settings.desired["display.host_mode"] = "auto"
        self.settings.applied["display.host_mode"] = "auto"
        self.settings.pending_confirm = None


class _FakePresenter:
    def __init__(self):
        self.display = {"mode": [1024, 768, 60], "scale": 1, "suggested_scale": 1, "outputs": []}
        self.docs: list[dict] = []

    async def try_request(self, method: str, **kwargs):
        if method == "ui_show" and "doc" in kwargs:
            self.docs.append(kwargs["doc"])
        return {"ok": True}


class _FakeMachines:
    def __init__(self, items: list | None = None):
        self._items = list(items or [])

    def list(self):
        return list(self._items)

    def get(self, mid: str | None):
        return next((m for m in self._items if m.id == mid), None)


class _FakeMachine:
    def __init__(self, mid: str, name: str, *, os_version: str = "9.2.2", quadra: bool = False,
                 osx: bool = False, guest_os: str = "classic", settings: dict | None = None):
        self.id = mid
        self.name = name
        self.quadra = quadra
        self.osx = osx
        self.meta = {"id": mid, "name": name, "os_version": os_version, "guest_os": guest_os,
                     "settings": dict(settings or {})}

    def needs_cd(self) -> bool:
        return False


class _FakeApp:
    def __init__(self, root: Path, machines: list | None = None):
        self.loop = asyncio.get_running_loop()
        self.mode = "normal"
        self.settings = _MemSettings()
        self.apply_engine = _FakeApplyEngine(self.settings)
        self.presenter = _FakePresenter()
        self.machines = _FakeMachines(machines)
        downloads = root / "downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        self.paths = SimpleNamespace(
            installer_dir=root / "installer",
            run=root / "run",
            downloads=downloads,
            machines=root / "machines",
            bluetooth_storage=root / "bt",
            qemu_m68k_package=root / "m68k",
            qemu_osx_package=root / "osxpkg",
        )
        self.paths.run.mkdir(parents=True, exist_ok=True)
        self.paths.machines.mkdir(parents=True, exist_ok=True)
        self.cfg = SimpleNamespace(catalog_ca_file=None, pi_boot_dir=root / "piboot", installer_board="x86")
        self.platform = SimpleNamespace(
            is_raspberry_pi=False, model="Test PC", ram_mb=8192, machine="x86_64",
            machine_families=("powermac", "quadra800"),
        )
        self.health = None
        self.lab_auth = None
        self.supervisor = SimpleNamespace(
            state="stopped", active=False, paused=False, content_seen=False, launch_pending=False,
            machine=None, unresponsive_reason=None, responsive=True, unavailable=None,
            timeline=lambda: [],
            active_machine=lambda: self.machines.get(self.settings.desired.get("machine.active")),
        )
        self.rpi_display = SimpleNamespace(enabled=False, public=lambda: {"available": False})
        self.speakers = None
        self.laptop = None
        self.bluetooth = SimpleNamespace(capable=lambda: False)
        self.optical = SimpleNamespace(present=lambda: False, list=lambda: [])
        self.usbshare = None
        self.updates = SimpleNamespace(
            public=lambda: {"available": True, "url": None, "current": {"version": "3.1.0", "label": "3.1.0"}},
            scan_usb=self._empty_async,
        )
        self.webproxy = SimpleNamespace(
            available=lambda: True,
            public=lambda: {"available": True, "enabled": True, "state": "running", "stats": {"counts": {}}},
            refresh=self._empty_async,
        )
        self.jobs = SimpleNamespace(holder=lambda *a: None)
        self.graphics = SimpleNamespace(ui_context=lambda: {"available": True, "message": None}, exact=lambda: False)
        self.network = SimpleNamespace(
            enabled=True,
            nm_dir=root / "nm",
            all_addresses=self._all_addrs,
            primary_address=self._primary_addr,
            wifi_hardware=self._wifi_hw,
        )
        self.addrs = ["192.168.1.40"]
        self.wifi_hw: dict | None = {"iface": "wlan0", "label": "Realtek Wi-Fi"}
        self.web = SimpleNamespace(
            url_for=lambda host: f"https://{host}/" if host else None,
            fingerprint=":".join(f"{i:02X}" for i in range(32)),
        )
        self.mdns = SimpleNamespace(display_name=lambda h: f"{h}.local")
        self._code_calls = 0
        self.pairing = SimpleNamespace(current_code=self._current_code, devices=lambda: [])
        self.lifecycle = SimpleNamespace(event=lambda *a, **k: None)
        self.archives = SimpleNamespace(known={}, list=self._empty_async, release=self._empty_async)
        self.catalog = SimpleNamespace(url="https://example.com/catalog.json", fetch=self._empty_async)
        self.catalogs = _FakeCatalogs([], self)
        self.usb = SimpleNamespace(scan=self._empty_async, find=lambda *a: None)
        self.scenes: list[tuple[str, str | None]] = []
        self._caps = {"audio": True, "display_modes": True, "wifi": True, "touch": False, "internal_display": False, "accelerometer": False}
        self._host_modes = ["1920x1080@60", "1280x800@60", "1024x768@60", "800x600@60", "640x480@60"]
        self.audio_outputs = [
            {"id": "auto", "label": "Automatic", "available": True},
            {"id": "speakers", "label": "Built-in Speakers", "available": True},
            {"id": "hdmi", "label": "HDMI Audio", "available": True},
        ]
        self.test_sound_calls: list[dict] = []
        self.test_sound_result = {"ok": True, "output": "auto"}
        self.updated_machine_settings: list[tuple[str, dict]] = []
        self.started_jobs: list[tuple[str, object]] = []
        self.power_calls: list[tuple] = []

    def lifecycle_job(self, kind: str, poweroff: bool | None = None, origin: str = "web"):
        self.power_calls.append(("lifecycle", kind, poweroff, origin))
        j = Job(id=f"j-{kind}", kind=f"guest.{kind}", resources=(), state=SUCCEEDED, result={})
        j.done.set()
        return j

    def host_job(self, action: str, force: bool = False, origin: str = "web"):
        self.power_calls.append(("host", action, force, origin))
        j = Job(id=f"j-host-{action}", kind=f"host.{action}", resources=(), state=SUCCEEDED, result={})
        j.done.set()
        return j

    async def _all_addrs(self):
        return list(self.addrs)

    async def _primary_addr(self):
        return self.addrs[0] if self.addrs else None

    async def _wifi_hw(self):
        return self.wifi_hw

    async def _empty_async(self):
        return []

    def _current_code(self, regenerate: bool = False):
        self._code_calls += 1
        return ("145554", 300.0)

    def capabilities(self):
        return dict(self._caps)

    def host_modes(self):
        return list(self._host_modes)

    def display_mode(self):
        m = self.presenter.display.get("mode")
        return tuple(m[:2]) if m else None

    def validation_context(self):
        return schema.ValidationContext(display_mode=self.display_mode(), host_modes=self.host_modes())

    def remove_blocked(self, m):
        return None

    def list_audio_outputs(self):
        return list(self.audio_outputs)

    def test_sound(self, output: str = "auto"):
        self.test_sound_calls.append({"output": output})
        return dict(self.test_sound_result)

    def machine_settings(self, mid: str) -> dict:
        m = self.machines.get(mid)
        return dict(m.meta.get("settings") or {}) if m else {}

    def update_machine_settings(self, mid: str, changes: dict) -> None:
        m = self.machines.get(mid)
        self.updated_machine_settings.append((mid, dict(changes)))
        if m is not None:
            s = dict(m.meta.get("settings") or {})
            s.update(changes)
            m.meta["settings"] = s

    async def show_scene(self, name: str, background: str | None = None, **kwargs):
        self.scenes.append((name, background))

    def apply_job(self, rev: int, restart: bool, origin: str = "panel"):
        j = Job(id="apply", kind="settings.apply", resources=(), state=SUCCEEDED, result={"restarting": restart})
        j.done.set()
        self.settings.applied.update(self.settings.desired)
        self.started_jobs.append(("apply", j.result))
        return j


class _FakeCatalogs:
    def __init__(self, entries: list[dict], app):
        self.entries = entries
        self.app = app

    async def fetch_all(self):
        return [("internet", self.entries)], {}

    def clients(self):
        return [("internet", None)]

    def custom_clients(self, *args, **kwargs):
        return []

    def label(self, source: str) -> str:
        return "Internet"

    def listing(self, results):
        return Catalogs(self.app).listing(results)


class TestPanel310(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    async def test_pairing_visibility_contract(self):
        app = _FakeApp(self.root)
        panel = Panel(app)
        self.assertFalse(panel.is_pairing_visible())

        nw = NetworkWeb(panel)
        await panel.show(nw)
        self.assertTrue(panel.is_pairing_visible())

        await panel.show(SetupWelcome(panel))
        self.assertFalse(panel.is_pairing_visible())

        ip = InstallerPanel(app)
        self.assertFalse(ip.is_pairing_visible())
        await ip.show(NetworkStep(ip))
        self.assertTrue(ip.is_pairing_visible())

        await ip.close(to_guest=False)
        self.assertFalse(ip.is_pairing_visible())

    async def test_network_screens_qr_and_4x8_fingerprint_and_airport_hint(self):
        app = _FakeApp(self.root)
        panel = Panel(app)
        nw = NetworkWeb(panel)
        await nw._load()
        doc = nw.doc()
        qr_nodes = [n for n in _walk(doc) if n.get("type") == "qr"]
        self.assertEqual(len(qr_nodes), 1)
        expected_lines = [
            "00:01:02:03:04:05:06:07",
            "08:09:0A:0B:0C:0D:0E:0F",
            "10:11:12:13:14:15:16:17",
            "18:19:1A:1B:1C:1D:1E:1F",
        ]
        self.assertEqual(ui.format_fingerprint(app.web.fingerprint), expected_lines)
        txt = _all_text(doc)
        for line in expected_lines:
            self.assertIn(line, txt)
        self.assertIn("AirPort:", txt)

        ip = InstallerPanel(app)
        ns = NetworkStep(ip)
        await ns._load()
        ndoc = ns.doc()
        self.assertEqual(len([n for n in _walk(ndoc) if n.get("type") == "qr"]), 1)
        ntxt = _all_text(ndoc)
        for line in expected_lines:
            self.assertIn(line, ntxt)

    def test_disk_dedup_and_stable_order(self):
        self.assertEqual(_dedupe_model("QEMU QEMU HARDDISK"), "QEMU HARDDISK")
        self.assertEqual(_dedupe_model("SanDisk SanDisk Ultra"), "SanDisk Ultra")
        self.assertEqual(_dedupe_model("QEMU CORP QEMU CORP NVMe"), "QEMU CORP NVMe")

    async def test_list_disks_stable_order_across_refreshes(self):
        app = _FakeApp(self.root)
        app.paths.installer_dir.mkdir(parents=True, exist_ok=True)
        script = app.paths.installer_dir / "list-disks"
        calls = [0]

        async def _fake_list():
            a = {"path": "/dev/sda", "model": "QEMU QEMU HARDDISK", "size_bytes": 64 * 1024**3,
                 "removable": False, "transport": "sata", "is_boot_medium": False}
            b = {"path": "/dev/sdb", "model": "Kingston Kingston USB", "size_bytes": 32 * 1024**3,
                 "removable": True, "transport": "usb", "is_boot_medium": False}
            c = {"path": "/dev/sr0", "model": "QEMU DVD-ROM", "size_bytes": 900 * 1024**2,
                 "removable": True, "transport": "sata", "is_boot_medium": True}
            raw = [b, a, c] if calls[0] % 2 == 0 else [c, a, b]
            calls[0] += 1
            script.write_text(f"#!/usr/bin/env python3\nimport json\nprint(json.dumps({raw!r}))\n")
            script.chmod(0o755)
            return await ip.list_disks()

        ip = InstallerPanel(app)
        first = await _fake_list()
        second = await _fake_list()
        self.assertEqual([d["path"] for d in first], [d["path"] for d in second])
        self.assertEqual(first[0]["model"], "QEMU HARDDISK")
        self.assertEqual(first[1]["model"], "Kingston USB")

    async def test_display_check_filter_and_revert_sync(self):
        app = _FakeApp(self.root)
        app.presenter.display = {"mode": [640, 480, 60], "scale": 1}
        ip = InstallerPanel(app)
        dc = DisplayCheck(ip)
        doc = dc.doc()
        guest_popup = _find_id(doc, "display.guest_size")
        self.assertEqual([o["id"] for o in guest_popup["options"]], ["auto", "640x480"])

        # Reverted display mode resets popup selection back to "auto" (#7).
        app.apply_engine.next_result = {"confirm": True}
        await dc.change("display.host_mode", "1920x1080@60")
        await dc.action("apply", {"display.host_mode": "1920x1080@60"})
        self.assertIsInstance(ip.screen, KeepSetting)
        app.settings.desired["display.host_mode"] = "auto"
        await ip.on_setting_reverted(["display.host_mode"])
        self.assertEqual(dc.mode, "auto")
        self.assertEqual(_find_id(dc.doc(), "display.host_mode")["value"], "auto")

    async def test_keep_setting_handles_nothing_to_confirm(self):
        app = _FakeApp(self.root)
        panel = Panel(app)
        ds = DisplaySound(panel, page="monitor")
        ds.values["display.host_mode"] = "1920x1080@60"
        app.settings.desired["display.host_mode"] = "auto"
        app.apply_engine.confirm_exc = NothingToConfirm("expired")
        ks = KeepSetting(panel, ds)
        await panel.show(ks)
        await ks.action("keep", {})
        self.assertEqual(panel.screen.doc_id, "message")
        self.assertEqual(ds.values["display.host_mode"], "auto")
        await panel.screen.action("ok", {})
        self.assertIs(panel.screen, ds)

    def test_install_progress_combines_duplicate_download_lines(self):
        self.assertEqual(ui.format_download_status(0.44, "Downloading… 87 of 197 MB"), "44% · 87 of 197 MB")
        async def _check():
            app = _FakeApp(self.root)
            ip = InstallerPanel(app)
            ip.st.plan = [{"key": "cat:mega", "choice": SimpleNamespace(remote=True), "id": "mega", "name": "Mega Deluxe"}]
            ip.st.results = {"cat:mega": {"state": "downloading", "fraction": 0.44, "detail": "44% · 87 of 197 MB"}}
            job = Job(id="inst", kind="installer.install", resources=())
            job.update(0.31, "Mega Deluxe: 44% · 87 of 197 MB")
            scr = InstallProgress(ip, job, "Installing Mac OS", lambda j: None)
            txt = _all_text(scr.doc())
            self.assertIn("Mega Deluxe: 44% · 87 of 197 MB", txt)
            self.assertNotIn("downloading… 44 %", txt)
        asyncio.run(_check())

    async def test_hardware_check_and_setup_welcome_keyboard_toggle(self):
        app = _FakeApp(self.root)
        ip = InstallerPanel(app)
        hw = HardwareCheck(ip)
        self.assertEqual(_find_id(hw.doc(), "input.swap_command_option")["value"], "mac")
        await hw.change("input.swap_command_option", "pc")
        self.assertTrue(app.settings.desired["input.swap_command_option"])
        self.assertIn("Alt+Q", _all_text(hw.doc()))

        panel = Panel(app)
        sw = SetupWelcome(panel)
        await sw.change("input.swap_command_option", "mac")
        self.assertFalse(app.settings.desired["input.swap_command_option"])
        self.assertIn("Windows+Q", _all_text(sw.doc()))

    async def test_display_sound_keyboard_and_sound_controls(self):
        app = _FakeApp(self.root)
        panel = Panel(app)
        ds = DisplaySound(panel, page="keyboard")
        await panel.show(ds)
        doc = ds.doc()
        dpi_field = _find_id(doc, "input.mouse_dpi")
        self.assertEqual(dpi_field["width"], 80)
        self.assertIn("0 = automatic", _all_text(doc))
        self.assertIsNotNone(_find_id(doc, "input.gamepad_mode"))
        self.assertIsNotNone(_find_id(doc, "input.natural_scroll"))

        # Strip whitespace & treat blank as 0 (#27).
        await ds.change("input.mouse_dpi", " 1600 ")
        self.assertEqual(ds.values["input.mouse_dpi"], 1600)
        await ds.change("input.mouse_dpi", "   ")
        self.assertEqual(ds.values["input.mouse_dpi"], 0)

        # Invalid DPI keeps Display & Sound open with inline validation (#27).
        await ds.action("apply", {"input.mouse_dpi": "bad"})
        self.assertIs(panel.screen, ds)
        self.assertTrue(ds.error)
        self.assertIn(ds.error, _all_text(ds.doc()))

        # Sound tab: Startup chime, Sound output, Test Sound (#12, #41).
        ds.error = None
        await ds.change("page", "sound")
        sdoc = ds.doc()
        chime_popup = _find_id(sdoc, "audio.startup_chime")
        self.assertEqual([o["id"] for o in chime_popup["options"]],
                         ["auto", "os9", "quadra", "powermac", "macii", "se", "off"])
        out_popup = _find_id(sdoc, "audio.output")
        self.assertEqual([o["id"] for o in out_popup["options"]], ["auto", "speakers", "hdmi"])
        self.assertIn("test-sound", _all_buttons(sdoc))
        await ds.action("test-sound", {"audio.output": "hdmi", "audio.startup_chime": "quadra"})
        self.assertEqual(app.test_sound_calls[-1], {"output": "hdmi"})
        self.assertIs(panel.screen, ds)
        app.test_sound_result = {"ok": False, "error": "No playback device"}
        await ds.action("test-sound", {"audio.output": "hdmi"})
        self.assertEqual(ds.error, "No playback device")

    async def test_first_boot_hero_13_mac_library_and_paused_resume(self):
        entries = validate_catalog(json.loads(PUBLIC_CATALOG.read_text()))
        self.assertEqual(len(entries), 14)

        # Installer ChooseMacs hero + full 13-Mac library + active Add/Remove (#9, #47).
        app = _FakeApp(self.root)
        (app.paths.qemu_m68k_package / "bin").mkdir(parents=True, exist_ok=True)
        (app.paths.qemu_m68k_package / "bin" / "qemu-system-m68k").write_text("")
        app.paths.qemu_osx_package.mkdir(parents=True, exist_ok=True)
        (app.paths.qemu_osx_package / "manifest.json").write_text(json.dumps({
            "features": [_osx.FEATURE, _osx.QEMU_FEATURE, _osx.NVRAM_FEATURE, _osx.FIRMWARE_FEATURE, "display-retroboot"],
            "sources": {"qemu": _osx.QEMU_SOURCE},
        }))
        ip = InstallerPanel(app)
        ip.st.target = {"path": "/dev/vda", "model": "Virtual Disk", "size_bytes": 64 * 1024**3}
        cm = ChooseMacs(ip)
        await cm.ps.loading
        ps = cm.ps
        ps.raw["catalog"] = (entries, "https://example.com/catalog.json", None)
        ps.status["catalog"] = {"state": "ready"}
        ps._rebuild_hosted()
        ps._recommend()
        doc = cm.doc()
        hero = _find_id(doc, "hero_pick")
        self.assertIsNotNone(hero)
        self.assertIn("Mega Deluxe", _all_text(doc))
        self.assertIn("Kid Pix Studio Deluxe", _all_text(doc))
        self.assertIn("Classic Clean", _all_text(doc))
        self.assertIn("Browse all 14 Library Macs", _all_text(doc))
        self.assertIn("host-only", _all_buttons(doc))
        # No mysteriously greyed-out Add button on hero screen.
        self.assertNotIn("toggle", _all_buttons(doc))
        # Expand 14-Mac library -> focused row has enabled Remove/Add button.
        await cm.change("library", True)
        ldoc = cm.doc()
        self.assertEqual(len(_find_id(ldoc, "macs")["items"]), 14)
        self.assertFalse(_all_buttons(ldoc)["toggle"].get("disabled", False))

        # Control Panel Download a Mac: hero + Paused / Resume + no 9.2.1 downgrade (#32, #47).
        installed_922 = _FakeMachine("m-922", "Mac OS 9.2.2", os_version="9.2.2")
        app2 = _FakeApp(self.root, machines=[installed_922])
        app2.paths.qemu_m68k_package = app.paths.qemu_m68k_package
        app2.paths.qemu_osx_package = app.paths.qemu_osx_package
        app2.catalogs = _FakeCatalogs(entries, app2)
        mega = next(e for e in entries if e["id"].startswith("os922-mega-deluxe"))
        sha = download_of(mega)["sha256"]
        part = app2.paths.downloads / f"{sha}.part"
        part.write_bytes(b"x" * (87 * 1024 * 1024))  # 87 MB partial file
        panel = Panel(app2)
        cl = CatalogList(panel, back=panel.show_recovery)
        await cl._load()
        cdoc = cl.doc()
        self.assertIsNotNone(_find_id(cdoc, "hero_pick"))
        self.assertEqual(_all_buttons(cdoc)["install"]["label"], "Resume")
        await cl.change("library", True)
        mega_row = next(r for r in _find_id(cl.doc(), "entry")["items"] if r["id"].endswith(":" + mega["id"]))
        self.assertIn("Paused · 87 of ", mega_row["detail"])
        # Never recommend 9.2.1 downgrade when 9.2.2 is installed.
        self.assertFalse(cl.recommended and str(cl.recommended.entry.get("osVersion", "")).startswith("9.2.1"))

    async def test_68k_mega_deluxe_hero_pick_labels(self):
        entries = [
            e for e in validate_catalog(json.loads(PUBLIC_CATALOG.read_text()))
            if e.get("machineFamily") == "quadra800"
        ]
        app = _FakeApp(self.root)
        (app.paths.qemu_m68k_package / "bin").mkdir(parents=True, exist_ok=True)
        (app.paths.qemu_m68k_package / "bin" / "qemu-system-m68k").write_text("")
        app.catalogs = _FakeCatalogs(entries, app)
        panel = Panel(app)
        cl = CatalogList(panel, back=panel.show_recovery)
        await cl._load()
        txt = _all_text(cl.doc())
        self.assertIn("Mac OS 8.1 Mega Deluxe — Recommended", txt)
        self.assertIn("Mac OS 8.1 (v2) — Classic Clean desktop", txt)
        self.assertNotIn("Mac OS 9.2.2", txt)

    async def test_update_copy_no_stale_installer_signin(self):
        app = _FakeApp(self.root)
        panel = Panel(app)
        su = SoftwareUpdate(panel)
        su.scanning = False
        txt = _all_text(su.doc())
        self.assertNotIn("Sign in to RetroBoot downloads in the installer", txt)
        self.assertIn("mcchord.net", txt)
        self.assertIn(".rbu", txt)

    async def test_mac_options_and_usb_eject_and_corrupt_disk_recovery(self):
        m_ppc = _FakeMachine("mac-922", "Mac OS 9.2.2", settings={"ram_mb": 512, "graphics": "standard"})
        m_tiger = _FakeMachine("mac-tiger", "Mac OS X Tiger", os_version="10.4.11", osx=True, guest_os="macosx-10.4",
                               settings={"osx_ram_mb": 768, "graphics": "ati-radeon"})
        app = _FakeApp(self.root, machines=[m_ppc, m_tiger])
        drives = [{"device": "/dev/sdb1", "name": "USB Stick", "label": "DOCS", "bytes": 16 * 1024**3}]
        ejected = []
        app.usbshare = SimpleNamespace(list_drives=lambda: list(drives), eject=lambda dev: ejected.append(dev) or drives.clear())
        panel = Panel(app)
        sd = StartupDisk(panel, back=panel.show_recovery, selected="mac-922")
        await sd._load_usb()
        await panel.show(sd)
        sdoc = sd.doc()
        self.assertIn("Eject “DOCS”", _all_buttons(sdoc)["eject-usb"]["label"])
        await sd.action("eject-usb", {})
        self.assertEqual(ejected, ["/dev/sdb1"])
        self.assertIn("Safe to unplug “DOCS”.", _all_text(sd.doc()))

        # Per-Mac Options (#44)
        await sd.action("options", {})
        self.assertIsInstance(panel.screen, MacOptions)
        await panel.screen.action("save", {"ram": "768", "graphics": "ati-radeon"})
        self.assertEqual(app.updated_machine_settings[-1], ("mac-922", {"ram_mb": 768, "graphics": "ati-radeon"}))

        mo_tiger = MacOptions(panel, "mac-tiger", sd)
        t_ram_ids = [o["id"] for o in _find_id(mo_tiger.doc(), "ram")["options"]]
        self.assertNotIn("128", t_ram_ids)

        # Corrupt disk recovery (#48)
        restored = []
        reset = []
        def _clean_job(mid, origin="panel"):
            restored.append(mid)
            j = Job(id="r1", kind="checkpoint.restore", resources=(), state=SUCCEEDED, result={})
            j.done.set()
            return j
        def _reset_job(mid, origin="panel"):
            reset.append(mid)
            j = Job(id="r2", kind="checkpoint.reset", resources=(), state=SUCCEEDED, result={})
            j.done.set()
            return j
        app.settings.desired["startup.autostart"] = False
        app.restore_last_clean_checkpoint_job = _clean_job
        app.reset_to_fresh_install_job = _reset_job
        await panel.show_corrupt_disk_recovery({
            "machine": "mac-922",
            "last_clean_checkpoint": "cp-clean",
            "fresh_install_checkpoint": "cp-fresh",
        })
        self.assertIsInstance(panel.screen, CorruptDiskRecovery)
        btns = _all_buttons(panel.screen.doc())
        self.assertIn("restore-clean", btns)
        self.assertIn("reset-fresh", btns)
        await panel.screen.action("restore-clean", {})
        await asyncio.sleep(0)
        self.assertEqual(restored, ["mac-922"])

        await panel.show_corrupt_disk_recovery({"machine": "mac-922", "fresh_install_checkpoint": "cp-fresh"})
        await panel.screen.action("reset-fresh", {})
        await asyncio.sleep(0)
        self.assertEqual(reset, ["mac-922"])

    def test_no_kid_lock_or_parent_pin_in_panel(self):
        panel_dir = Path(__file__).resolve().parents[1] / "retroboot" / "panel"
        combined = "\n".join(p.read_text() for p in sorted(panel_dir.glob("*.py")))
        for banned in ("Parent PIN", "Kid Lock", "kid_lock", "parent_pin", "SafeSearch"):
            self.assertNotIn(banned, combined)

    async def test_recovery_panes_keyboard_webhelper_and_force_reboot(self):
        from retroboot.panel.controller import DisplaySound, ForceRebootAlert, RetroBootWindow, WebHelper

        app = _FakeApp(self.root, machines=[_FakeMachine("m1", "Mac OS 9")])
        app.supervisor.active = True
        panel = Panel(app)
        win = RetroBootWindow(panel)
        doc = win.doc()
        pane_ids = [r["id"] for r in _find_id(doc, "pane")["items"]]
        self.assertIn("keyboard", pane_ids)
        self.assertIn("webhelper", pane_ids)
        self.assertIn("force-reboot", _all_buttons(doc))

        await win.open("keyboard")
        self.assertIsInstance(panel.screen, DisplaySound)
        self.assertEqual(panel.screen.page, "keyboard")
        self.assertEqual(panel.screen.doc()["title"], "Keyboard & Mouse")

        await win.open("webhelper")
        self.assertIsInstance(panel.screen, WebHelper)
        self.assertIn("bypass", _all_buttons(panel.screen.doc()))

        await win.action("force-reboot", {})
        self.assertIsInstance(panel.screen, ForceRebootAlert)
        btns = _all_buttons(panel.screen.doc())
        self.assertIn("force-restart-mac", btns)
        self.assertIn("force-reboot-host", btns)
        self.assertFalse(btns["force-restart-mac"].get("disabled", False))

        await panel.screen.action("force-restart-mac", {})
        await asyncio.sleep(0)
        self.assertIn(("lifecycle", "force-restart", None, "panel"), app.power_calls)

        await panel.show(ForceRebootAlert(panel, back=win))
        await panel.screen.action("force-reboot-host", {})
        await asyncio.sleep(0)
        self.assertIn(("host", "reboot", True, "panel"), app.power_calls)

    async def test_catalog_list_blank_machine_show_scene_race_and_error(self):
        entries = validate_catalog(json.loads(PUBLIC_CATALOG.read_text()))

        # 1. Blank disk (os_version=None) must not crash _load() or stick on striped progress bar.
        blank = _FakeMachine("blank-1", "Untitled Mac", os_version=None)
        app = _FakeApp(self.root, machines=[blank])
        app.catalogs = _FakeCatalogs(entries, app)
        panel = Panel(app)
        cl = CatalogList(panel, back=panel.show_recovery)
        await panel.show(cl)
        await asyncio.sleep(0.02)
        self.assertIsNotNone(cl.items)
        self.assertGreater(len(cl.items), 0)
        self.assertIsNone(cl.error)
        last_doc = app.presenter.docs[-1]
        self.assertFalse(any(n.get("type") == "progress" for n in _walk(last_doc)))
        self.assertIsNotNone(_find_id(last_doc, "hero_pick"))

        # 2. Fast/cached fetch finishing while show_scene awaits must still render loaded doc, not stale loading.
        app2 = _FakeApp(self.root, machines=[_FakeMachine("m1", "Mac OS 9.2.2", os_version="9.2.2")])
        orig_show_scene = app2.show_scene

        async def yielding_show_scene(*a, **k):
            await asyncio.sleep(0.01)
            return await orig_show_scene(*a, **k)

        app2.show_scene = yielding_show_scene
        app2.catalogs = _FakeCatalogs(entries, app2)
        panel2 = Panel(app2)
        cl2 = CatalogList(panel2, back=panel2.show_recovery)
        await panel2.show(cl2)
        await asyncio.sleep(0.02)
        final_doc = app2.presenter.docs[-1]
        self.assertFalse(any(n.get("type") == "progress" for n in _walk(final_doc)))
        self.assertIsNotNone(_find_id(final_doc, "hero_pick"))

        # 3. Unexpected exception during listing transitions to error notice instead of hanging on progress.
        app3 = _FakeApp(self.root)
        bad_cats = _FakeCatalogs(entries, app3)

        def boom_listing(_results):
            raise RuntimeError("boom")

        bad_cats.listing = boom_listing
        app3.catalogs = bad_cats
        panel3 = Panel(app3)
        cl3 = CatalogList(panel3, back=panel3.show_recovery)
        await panel3.show(cl3)
        await asyncio.sleep(0.02)
        err_doc = app3.presenter.docs[-1]
        self.assertFalse(any(n.get("type") == "progress" for n in _walk(err_doc)))
        self.assertIn("The catalog could not be loaded.", _all_text(err_doc))
        self.assertIn("retry", _all_buttons(err_doc))

    async def test_display_sound_volume_slider_clears_mute_and_refreshes(self):
        app = _FakeApp(self.root)
        app.capabilities = lambda: {"audio": True, "audio_output": True, "display_modes": False}
        app.laptop = SimpleNamespace(muted=True, can_mute=True)
        pushed = []

        async def push_volume():
            pushed.append(app.settings.applied["audio.volume"])
            app.laptop.muted = False
            return True

        app.apply_engine = SimpleNamespace(push_volume=push_volume)
        panel = Panel(app)
        scr = DisplaySound(panel, page="sound")
        await panel.show(scr)
        before_docs = len(app.presenter.docs)
        await scr.change("audio.volume", 55)
        self.assertEqual(pushed, [55])
        self.assertFalse(app.laptop.muted)
        self.assertGreater(len(app.presenter.docs), before_docs)
