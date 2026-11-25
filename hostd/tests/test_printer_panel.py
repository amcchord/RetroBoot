"""Tests for the RetroBoot Printers control panel screens."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest

from retroboot import schema
from retroboot.panel.controller import (
    Panel,
    PrinterAddManual,
    PrinterConfirmRemove,
    PrinterDiscover,
    Printers,
    RetroBootWindow,
)


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
        self.desired.setdefault("printer.target", "pdf")
        self.desired.setdefault("printer.keep_pdf", False)
        self.applied = dict(self.desired)
        self.revision = 1
        self.pending_confirm = None

    def set_desired_and_applied(self, changes: dict, ctx=None):
        self.desired.update(changes)
        self.applied.update(changes)
        self.revision += 1
        return self.revision

    def pending_by_policy(self):
        return {schema.LIVE: [], schema.GUEST_RESTART: [], schema.HOST_REBOOT: []}


class _FakeApplyEngine:
    def __init__(self, settings: _MemSettings):
        self.settings = settings
        self.applied_live: list[list[str]] = []

    async def apply_live_keys(self, keys: list[str]) -> dict:
        self.applied_live.append(list(keys))
        return {k: self.settings.applied.get(k) for k in keys}


class _FakeCups:
    def __init__(self, avail: bool = True):
        self._available = avail
        self.printers = [
            {
                "name": "HP_LaserJet_4000",
                "description": "HP LaserJet 4000",
                "uri": "socket://192.168.1.50:9100",
                "state": "idle",
                "accepting": True,
            }
        ]
        self.discovered = [
            {
                "id": "disc-brother",
                "name": "Brother HL-L2350DW",
                "model": "Brother HL-L2350DW series",
                "uri": "ipp://192.168.1.72:631/ipp/print",
                "transport": "network",
                "driver_hint": "auto",
                "address": "192.168.1.72",
                "already_added": False,
            }
        ]
        self.added_calls: list[dict] = []
        self.removed_calls: list[str] = []
        self.print_pdf_calls: list[tuple] = []

    def available(self) -> bool:
        return self._available

    def list_printers(self) -> list[dict]:
        return list(self.printers)

    async def discover(self, timeout: float = 5.0) -> list[dict]:
        return list(self.discovered)

    def add_printer(self, *, name: str, uri: str, driver: str = "auto", description: str = "") -> dict:
        self.added_calls.append({"name": name, "uri": uri, "driver": driver, "description": description})
        entry = {
            "name": name,
            "description": description or name,
            "uri": uri,
            "state": "idle",
            "accepting": True,
        }
        self.printers.append(entry)
        return entry

    def remove_printer(self, name: str) -> bool:
        self.removed_calls.append(name)
        self.printers = [p for p in self.printers if p["name"] != name]
        return True

    def print_pdf(self, queue: str, pdf_bytes: bytes, title: str = "RetroBoot Print") -> dict:
        self.print_pdf_calls.append((queue, len(pdf_bytes), title))
        return {"job_id": 42}


class _FakePrinterServer:
    def __init__(self):
        self.test_calls: list[tuple[str, str]] = []

    def public(self) -> dict:
        return {"enabled": True, "running": True, "lpd_port": 515, "ipp_port": 631, "jobs": len(self.test_calls)}

    def list_jobs(self) -> list[dict]:
        return []

    async def print_test_page(self, name: str, version: str) -> dict:
        self.test_calls.append((name, version))
        return {"ok": True, "printer": name}


class _FakePresenter:
    def __init__(self):
        self.display = {"mode": [1280, 800, 60]}
        self.docs: list[dict] = []

    async def try_request(self, method: str, **kwargs):
        if method == "ui_show" and "doc" in kwargs:
            self.docs.append(kwargs["doc"])
        return {"ok": True}


class _FakeApp:
    def __init__(self, root: Path, *, with_cups: bool = True, cups_available: bool = True):
        self.loop = asyncio.get_running_loop()
        self.mode = "normal"
        self.settings = _MemSettings()
        self.apply_engine = _FakeApplyEngine(self.settings)
        self.presenter = _FakePresenter()
        self.machines = SimpleNamespace(list=lambda: [], get=lambda _id: None)
        self.cfg = SimpleNamespace(pi_boot_dir=root / "piboot")
        self.platform = SimpleNamespace(model="Test PC")
        self.supervisor = SimpleNamespace(
            state="stopped",
            active=False,
            paused=False,
            content_seen=False,
            machine=None,
            responsive=True,
            unresponsive_reason=None,
            failure=None,
            timeline=lambda: [],
            active_machine=lambda: None,
        )
        self.bluetooth = SimpleNamespace(capable=lambda: False)
        self.webproxy = SimpleNamespace(available=lambda: False)
        self.catalogs = SimpleNamespace(clients=lambda: [])
        self.updates = SimpleNamespace(public=lambda: {"available": False})
        self.jobs = SimpleNamespace(holder=lambda *a: None)
        self.network = SimpleNamespace(primary_address=self._primary)
        self.printer = _FakePrinterServer()
        if with_cups:
            self.cups = _FakeCups(avail=cups_available)

    async def _primary(self):
        return "192.168.1.40"

    def validation_context(self):
        return None

    async def show_scene(self, *a, **kw):
        pass


class TestPrinterPanel(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    async def test_retroboot_window_printer_summary_and_missing_cups_fallback(self):
        app_no_cups = _FakeApp(self.root, with_cups=False)
        panel = Panel(app_no_cups)
        win = RetroBootWindow(panel)
        # Printers are off by default until turned on via the checkmark.
        self.assertFalse(app_no_cups.settings.applied["printer.enabled"])
        self.assertEqual(win._printer_summary(), "Off")
        doc = win.doc()
        pane_rows = {r["id"]: r for r in _find_id(doc, "pane")["items"]}
        self.assertIn("printers", pane_rows)
        self.assertEqual(pane_rows["printers"]["label"], "Printers…")
        self.assertEqual(pane_rows["printers"]["detail"], "Off")

        # Open Printers while off -> checkbox unchecked, Test Page disabled, CUPS buttons disabled
        await win.open("printers")
        self.assertIsInstance(panel.screen, Printers)
        off_doc = panel.screen.doc()
        self.assertFalse(_find_id(off_doc, "enabled")["value"])
        self.assertTrue(_all_buttons(off_doc)["test"].get("disabled"))
        self.assertIn("Enable Mac printing", _all_text(off_doc))

        # Turn printing on via the checkmark -> summary says Save as PDF, Test Page enabled
        await panel.screen.change("enabled", True)
        self.assertTrue(app_no_cups.settings.applied["printer.enabled"])
        self.assertEqual(win._printer_summary(), "Save as PDF")
        pdoc = panel.screen.doc()
        btns = _all_buttons(pdoc)
        self.assertTrue(_find_id(pdoc, "enabled")["value"])
        self.assertTrue(btns["discover"].get("disabled"))
        self.assertTrue(btns["add"].get("disabled"))
        self.assertFalse(btns["test"].get("disabled"))
        self.assertIn("CUPS is not installed", _all_text(pdoc))

    async def test_printers_switch_target_keep_pdf_and_test_page(self):
        app = _FakeApp(self.root)
        panel = Panel(app)
        win = RetroBootWindow(panel)
        await win.open("printers")
        scr = panel.screen
        self.assertIsInstance(scr, Printers)
        await scr.change("enabled", True)
        self.assertIn(["printer.enabled"], app.apply_engine.applied_live)

        # Select CUPS printer and click Use for Mac
        await scr.change("target", "cups:HP_LaserJet_4000")
        await scr.action("use", {})
        self.assertEqual(app.settings.applied["printer.target"], "cups:HP_LaserJet_4000")
        self.assertIn(["printer.target"], app.apply_engine.applied_live)
        self.assertEqual(win._printer_summary(), "HP LaserJet 4000")

        # Toggle Also save PDF copy
        await scr.change("keep_pdf", True)
        self.assertTrue(app.settings.applied["printer.keep_pdf"])
        self.assertIn(["printer.keep_pdf"], app.apply_engine.applied_live)

        # Print test page for active CUPS printer and for PDF
        await scr.action("test", {"target": "cups:HP_LaserJet_4000"})
        self.assertEqual(app.printer.test_calls[-1][0], "HP_LaserJet_4000")

        await scr.change("target", "pdf")
        await scr.action("test", {})
        self.assertEqual(app.printer.test_calls[-1][0], "pdf")

    async def test_manual_add_discover_add_and_remove_printer(self):
        app = _FakeApp(self.root)
        panel = Panel(app)
        scr = Printers(panel, back=panel.show_recovery)
        await panel.show(scr)

        # 1. Manual Add
        await scr.action("add", {})
        self.assertIsInstance(panel.screen, PrinterAddManual)
        await panel.screen.action("add", {
            "name": "Office Laser",
            "host": "192.168.1.99",
            "protocol": "socket",
            "driver": "generic_ps",
            "set_target": True,
        })
        self.assertIsInstance(panel.screen, Printers)
        self.assertEqual(app.cups.added_calls[-1]["name"], "Office_Laser")
        self.assertEqual(app.cups.added_calls[-1]["uri"], "socket://192.168.1.99:9100")
        self.assertEqual(app.cups.added_calls[-1]["driver"], "generic_ps")
        self.assertEqual(app.settings.applied["printer.target"], "cups:Office_Laser")

        # 2. Discover Add
        await panel.screen.action("discover", {})
        self.assertIsInstance(panel.screen, PrinterDiscover)
        await asyncio.sleep(0)
        ddoc = panel.screen.doc()
        self.assertIn("Brother HL-L2350DW", _all_text(ddoc))
        await panel.screen.action("add", {"set_target": True})
        self.assertIsInstance(panel.screen, Printers)
        self.assertEqual(app.cups.added_calls[-1]["uri"], "ipp://192.168.1.72:631/ipp/print")
        self.assertEqual(app.settings.applied["printer.target"], "cups:Brother_HL-L2350DW")

        # 3. Remove active CUPS printer -> resets target to pdf
        await panel.screen.change("target", "cups:Brother_HL-L2350DW")
        await panel.screen.action("remove", {})
        self.assertIsInstance(panel.screen, PrinterConfirmRemove)
        self.assertIn("switch back to Save as PDF", _all_text(panel.screen.doc()))
        await panel.screen.action("remove", {})
        self.assertIsInstance(panel.screen, Printers)
        self.assertEqual(app.cups.removed_calls[-1], "Brother_HL-L2350DW")
        self.assertEqual(app.settings.applied["printer.target"], "pdf")

    async def test_tuple_cups_available_and_async_list_printers(self):
        class _AsyncTupleCups(_FakeCups):
            def available(self):
                return (self._available, None if self._available else "CUPS daemon unavailable.")

            async def list_printers(self):
                return list(self.printers)

        app = _FakeApp(self.root, with_cups=False)
        app.cups = _AsyncTupleCups(avail=False)
        panel = Panel(app)
        scr = Printers(panel, back=panel.show_recovery)
        self.assertFalse(scr.cups_available)
        doc = scr.doc()
        self.assertIn("CUPS daemon unavailable.", _all_text(doc))
        self.assertTrue(_all_buttons(doc)["discover"].get("disabled"))

        # Now available=(True, None) with async list_printers()
        app.cups._available = True
        app.settings.applied["printer.enabled"] = True
        app.settings.applied["printer.target"] = "cups:HP_LaserJet_4000"
        win = RetroBootWindow(panel)
        await asyncio.sleep(0)
        self.assertEqual(win._printer_summary(), "HP LaserJet 4000")

        scr2 = Printers(panel, back=panel.show_recovery)
        await asyncio.sleep(0)
        self.assertTrue(scr2.cups_available)
        self.assertEqual(len(scr2.printers), 1)
        self.assertFalse(_all_buttons(scr2.doc())["discover"].get("disabled"))


if __name__ == "__main__":
    unittest.main()
