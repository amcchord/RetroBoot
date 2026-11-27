"""First-run details from the November 23 2026 out-of-box pass: the Mac a new computer is
offered first, the Time Zone list, word-safe descriptions, stoppable downloads, the space
check before a download and the USB archive wording."""
import asyncio
import json
from pathlib import Path
import unittest
from types import SimpleNamespace

import helpers
from retroboot import catalogs, schema
from retroboot.jobs import FAILED, JobManager
from retroboot.panel import ui
from retroboot.panel.controller import archive_origin

# The public RetroBoot Mac library's families and names (catalog of 2026-11-22), in its order.
LIBRARY = [
    ("mac-os-7.5.5-v1", "Mac OS 7.5.5", "7.5.5", "quadra800"),
    ("mac-os-7-5-5-deluxe-v2", "Mac OS 7.5.5 Deluxe v2", "7.5.5", "quadra800"),
    ("mac-os-8.1-v1", "Mac OS 8.1", "8.1", "quadra800"),
    ("mac-os-8-1-deluxe-v2", "Mac OS 8.1 Deluxe v2", "8.1", "quadra800"),
    ("os921-full-v4", "Mac OS 9.2.1 (v4)", "9.2.1", None),
    ("os922-full-v5", "Mac OS 9.2.2 (v5)", "9.2.2", None),
    ("os922-deluxe-v9", "Mac OS 9.2.2 Deluxe v9", "9.2.2", None),
    ("os922-superdeluxe-v6", "Mac OS 9.2.2 SuperDeluxe v6", "9.2.2", None),
    ("os922-mega-deluxe-v4", "Mac OS 9.2.2 Mega Deluxe v4", "9.2.2", None),
    ("mac-os-x-10-4-11-tiger-period-v1", "Mac OS X 10.4.11 Tiger · Period Edition", "10.4.11", "powerMacOSX"),
    ("mac-os-x-10-4-11-tiger-deluxe-v1", "Mac OS X 10.4.11 Tiger Deluxe", "10.4.11", "powerMacOSX"),
]


def listed(rows=LIBRARY, known=(), families=None):
    entries = []
    for mid, name, version, family in rows:
        e = {"id": mid, "name": name, "osVersion": version}
        if family:
            e["machineFamily"] = family
        entries.append(e)
    results = catalogs.runnable([("internet", entries)], families)
    return catalogs.arrange(results, frozenset(known))


class RecommendedMacTests(unittest.TestCase):
    def test_pc_gets_standard_mac_os_922(self):
        pick = catalogs.recommended(listed())
        self.assertEqual(pick.entry["id"], "os922-full-v5")

    def test_zero_gets_the_newest_standard_68k_system(self):
        pick = catalogs.recommended(listed(families={"quadra800"}))
        self.assertEqual(pick.entry["id"], "mac-os-8.1-v1")

    def test_installed_or_only_editions_fall_back(self):
        # Installing clean Mac OS 9.2.2 never recommends downgrading to 9.2.1;
        # it recommends Mega Deluxe while uninstalled, or nothing once installed.
        pick = catalogs.recommended(listed(known={"os922-full-v5"}))
        self.assertEqual(pick.entry["id"], "os922-mega-deluxe-v4")
        self.assertIsNone(catalogs.recommended(listed(known={"os922-full-v5", "os922-mega-deluxe-v4"})))
        only_editions = [r for r in LIBRARY if "deluxe" in r[0] or "tiger" in r[0]]
        self.assertEqual(catalogs.recommended(listed(only_editions)).entry["id"], "os922-deluxe-v9")
        self.assertIsNone(catalogs.recommended(listed([r for r in LIBRARY if "tiger" in r[0]])))
        self.assertIsNone(catalogs.recommended([]))

    def test_older_versions_are_never_recommended(self):
        rows = [("os922-full-v4", "Mac OS 9.2.2 (v4)", "9.2.2", None), ("os922-full-v5", "Mac OS 9.2.2 (v5)", "9.2.2", None)]
        items = listed(rows)
        self.assertEqual(catalogs.recommended(items).entry["id"], "os922-full-v5")
        self.assertTrue(next(i for i in items if i.entry["id"] == "os922-full-v4").older)


class TimeZoneListTests(unittest.TestCase):
    def test_real_places_without_aliases(self):
        td, root = helpers.scratch()
        with td:
            tab = root / "zone.tab"
            tab.write_text("# comment\nUS\t+404251-0740023\tAmerica/New_York\tEastern (most areas)\n"
                           "FR\t+4852+00220\tEurope/Paris\n"
                           "ZZ\t+0000+00000\tNowhere/Not_A_Zone\n")
            zones = schema.listed_timezones(root)
        self.assertEqual(zones, {"America/New_York", "Europe/Paris", "UTC"})

    def test_system_tables_leave_out_aliases(self):
        zones = schema.listed_timezones()
        self.assertIn("UTC", zones)
        self.assertIn("America/New_York", zones)
        for alias in ("ROK", "UCT", "US/Eastern", "Etc/GMT+5", "Poland"):
            self.assertNotIn(alias, zones)
        other = schema.timezone_regions(zones).get("Other", [])
        self.assertEqual(other, ["UTC"])


class WordsTests(unittest.TestCase):
    def test_clip_words(self):
        text = "Three original Apple QuickTime sample movies beside QuickTime Player and all earlier guest defaults."
        out = ui.clip_words(text, 60)
        self.assertLessEqual(len(out), 60)
        self.assertTrue(out.endswith("…"))
        self.assertTrue(text.startswith(out[:-1]))
        self.assertEqual(text[len(out) - 1], " ")      # whole words only ("beside…", not "QuickTi…")
        self.assertEqual(ui.clip_words("Short.", 60), "Short.")

    def test_archive_origin(self):
        self.assertEqual(archive_origin("usb:STICK"), "USB disk “STICK”")
        self.assertEqual(archive_origin("embedded"), "the installer")
        self.assertEqual(archive_origin("usb"), "USB disk")


class StoppableJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_a_cancellable_phase_stops(self):
        jobs = JobManager()
        go = asyncio.Event()

        async def body(job):
            job.cancellable = True
            await go.wait()
            return {}

        job = jobs.start("machines.import", body)
        await asyncio.sleep(0.05)
        self.assertTrue(jobs.cancel(job, "The download was stopped."))
        await asyncio.wait_for(job.done.wait(), 2)
        self.assertEqual((job.state, job.error["code"]), (FAILED, "cancelled"))
        self.assertFalse(jobs.cancel(job, "again"))

        async def unpacking(job):
            await go.wait()
            return {}

        other = jobs.start("machines.import", unpacking)
        await asyncio.sleep(0.05)
        self.assertFalse(jobs.cancel(other, "no"))       # unpacking is not stoppable
        go.set()
        await asyncio.wait_for(other.done.wait(), 2)
        self.assertEqual(other.state, "succeeded")


class ImportRoomTests(unittest.TestCase):
    def test_refused_before_downloading_when_the_mac_cannot_fit(self):
        from retroboot.hostd import Hostd
        from retroboot.jobs import JobFailed
        td, root = helpers.scratch()
        with td:
            app = Hostd(helpers.make_config(root))
            app.paths.machines.mkdir(parents=True, exist_ok=True)
            client = SimpleNamespace(downloads=root / "downloads")
            huge = {"id": "big-v1", "name": "Big Mac", "archiveBytes": 1 << 50, "installedBytes": 1 << 50,
                    "sha256": "0" * 64, "archiveURL": "https://example.test/big.tar.gz"}
            with self.assertRaises(JobFailed) as cm:
                app._check_import_room(client, huge)
            self.assertEqual(cm.exception.code, "no_space")
            self.assertIn("Big Mac needs", cm.exception.message)
            small = dict(huge, archiveBytes=1024, installedBytes=4096)
            app._check_import_room(client, small)        # fits: no error


class RaspberryPiOobeTests(unittest.IsolatedAsyncioTestCase):
    async def test_zero2w_oobe_flow_then_next_boot_autostarts(self):
        from unittest import mock
        from fakes.fake_presenter import FakePresenter
        from retroboot.config import Paths
        from retroboot.hostd import Hostd
        from retroboot.panel.controller import RpiOobeStartupMac, RpiOobeWelcome, SetupWelcome

        td, root = helpers.scratch()
        self.addCleanup(td.cleanup)
        helpers.make_device_tree(root, "Raspberry Pi Zero 2 W Rev 1.0",
                                 ["raspberrypi,model-zero-2-w", "brcm,bcm2837"], 0x902120)
        prof = root / "usr/lib/retroboot/rpi-profile.json"
        prof.parent.mkdir(parents=True, exist_ok=True)
        prof.write_text(json.dumps({"schema_version": 1, "model": "zero2w"}))
        m68k_pkg = helpers.make_m68k_package(root)
        helpers.fake_qemu_conf(m68k_pkg, modes=["normal"], boot_s=0.1, finder_s=0.2)
        helpers.make_quadra_machine(root, "mac-os-7-5-5", "Mac OS 7.5.5", "7.5.5")
        helpers.make_quadra_machine(root, "mac-os-8-1", "Mac OS 8.1", "8.1")
        (root / "var/lib/retroboot/config").mkdir(parents=True, exist_ok=True)
        (root / "var/lib/retroboot/config/install-seed.json").write_text(
            json.dumps({"settings": {"machine.active": "mac-os-7-5-5"}, "oobe": True})
        )

        with helpers.test_rom():
            paths = Paths(root)
            paths.ensure()
            fp = FakePresenter(str(paths.presenter_sock), str(paths.display_sock))
            await fp.start()
            app = Hostd(helpers.make_config(root, platinum_build=False))
            helpers.install_fake_prep(app, module_68k=helpers.FakePrep68kModule())
            platinum_calls = []

            async def fake_ensure(disk):
                platinum_calls.append(Path(disk))
                return True

            try:
                with mock.patch.object(app.platinum, "ensure", side_effect=fake_ensure):
                    await app.start()
                    await helpers.wait_for(lambda: app.panel.visible and isinstance(app.panel.screen, RpiOobeWelcome),
                                           timeout=10, what="oobe-welcome")
                    self.assertTrue(app.oobe_pending())
                    self.assertEqual(app.supervisor.state, "stopped")
                    await helpers.wait_for(lambda: len(platinum_calls) >= 1, timeout=5, what="oobe platinum warm")
                    self.assertEqual(platinum_calls[-1].parent.name, "mac-os-8-1")

                    # Continue -> Time Zone
                    await app.panel.on_event({"event": "ui_action", "doc": "oobe-welcome", "id": "continue", "values": {}})
                    self.assertEqual(app.panel.screen.doc_id, "timezone")
                    # Set Time Zone -> Startup Mac picker
                    await app.panel.on_event({"event": "ui_action", "doc": "timezone", "id": "set",
                                              "values": {"zone": "America/New_York"}})
                    self.assertIsInstance(app.panel.screen, RpiOobeStartupMac)
                    self.assertEqual(app.settings.applied["system.timezone"], "America/New_York")
                    # Pick 8.1 and Start Mac OS -> completes OOBE and starts chosen Mac
                    await app.panel.on_event({"event": "ui_action", "doc": "oobe-startup-mac", "id": "start",
                                              "values": {"machine": "mac-os-8-1"}})
                    await helpers.wait_for(lambda: (not app.oobe_pending()
                                                    and app.settings.applied.get("machine.active") == "mac-os-8-1"
                                                    and app.supervisor.state == "running"),
                                           timeout=15, what="oobe start mac")
            finally:
                await app.shutdown()
                await fp.stop()

            # Second boot with existing settings.json skips OOBE and autostarts.
            fp2 = FakePresenter(str(paths.presenter_sock), str(paths.display_sock))
            await fp2.start()
            app2 = Hostd(helpers.make_config(root, platinum_build=False))
            helpers.install_fake_prep(app2, module_68k=helpers.FakePrep68kModule())
            try:
                await app2.start()
                self.assertFalse(app2.oobe_pending())
                await helpers.wait_for(lambda: app2.supervisor.state == "running", timeout=15, what="second boot autostart")
                self.assertFalse(isinstance(app2.panel.screen, RpiOobeWelcome))
            finally:
                await app2.shutdown()
                await fp2.stop()

            # Empty Pi 4/5 fresh SD card goes oobe-welcome -> timezone -> setup-welcome (skip_timezone=True) -> catalog
            td2, root2 = helpers.scratch()
            self.addCleanup(td2.cleanup)
            helpers.make_device_tree(root2, "Raspberry Pi 5 Model B Rev 1.0", ["raspberrypi,5-model-b", "brcm,bcm2712"])
            helpers.make_package(root2)
            paths2 = Paths(root2)
            paths2.ensure()
            fp3 = FakePresenter(str(paths2.presenter_sock), str(paths2.display_sock))
            await fp3.start()
            app3 = Hostd(helpers.make_config(root2, platinum_build=False))
            try:
                await app3.start()
                self.assertTrue(app3.oobe_pending())
                await helpers.wait_for(lambda: isinstance(app3.panel.screen, RpiOobeWelcome),
                                       timeout=10, what="pi5 empty oobe")
                await app3.panel.on_event({"event": "ui_action", "doc": "oobe-welcome", "id": "continue", "values": {}})
                self.assertEqual(app3.panel.screen.doc_id, "timezone")
                await app3.panel.on_event({"event": "ui_action", "doc": "timezone", "id": "set", "values": {"zone": "UTC"}})
                self.assertIsInstance(app3.panel.screen, SetupWelcome)
                await app3.panel.on_event({"event": "ui_action", "doc": "setup-welcome", "id": "continue",
                                           "values": {"source": "catalog"}})
                self.assertEqual(app3.panel.screen.doc_id, "setup-catalog")
            finally:
                await app3.shutdown()
                await fp3.stop()

            # Opt-out ("oobe": false) skips OOBE even on fresh Pi
            td3, root3 = helpers.scratch()
            self.addCleanup(td3.cleanup)
            helpers.make_device_tree(root3, "Raspberry Pi 5 Model B Rev 1.0", ["raspberrypi,5-model-b", "brcm,bcm2712"])
            helpers.make_package(root3)
            (root3 / "var/lib/retroboot/config").mkdir(parents=True, exist_ok=True)
            (root3 / "var/lib/retroboot/config/install-seed.json").write_text(json.dumps({"oobe": False}))
            app4 = Hostd(helpers.make_config(root3, platinum_build=False))
            await app4.start()
            try:
                self.assertFalse(app4.oobe_pending())
            finally:
                await app4.shutdown()

            # Emulated ARM / profile-backed RPi image (image_model without Broadcom DT) schedules OOBE
            # and every RPi OOBE screen emits only valid built-in rbui icon names.
            import re as _re
            gfx_src = (Path(__file__).resolve().parents[2] / "presenter/ui/src/gfx.c").read_text()
            m_icons = _re.search(r"kIconNames\[\]\s*=\s*\{(.*?)NULL\s*\};", gfx_src, _re.DOTALL)
            self.assertIsNotNone(m_icons)
            valid_icons = set(_re.findall(r'"([a-z0-9_]+)"', m_icons.group(1)))

            td4, root4 = helpers.scratch()
            self.addCleanup(td4.cleanup)
            helpers.make_package(root4)
            (root4 / "usr/lib/retroboot").mkdir(parents=True, exist_ok=True)
            (root4 / "usr/lib/retroboot/rpi-profile.json").write_text(json.dumps({"schema_version": 1, "model": "pi4-5"}))
            app5 = Hostd(helpers.make_config(root4, platinum_build=False))
            await app5.start()
            try:
                self.assertTrue(app5.oobe_pending())
                for scr in (RpiOobeWelcome(app5.panel), RpiOobeStartupMac(app5.panel, back=lambda: None)):
                    doc = scr.doc()
                    icons = []
                    def walk(node):
                        if isinstance(node, dict):
                            if "icon" in node and isinstance(node["icon"], str):
                                icons.append(node["icon"])
                            for v in node.values():
                                walk(v)
                        elif isinstance(node, list):
                            for v in node:
                                walk(v)
                    walk(doc)
                    self.assertTrue(all(ic in valid_icons for ic in icons), f"{scr.doc_id}: invalid icons {icons}")
            finally:
                await app5.shutdown()


if __name__ == "__main__":
    unittest.main()
