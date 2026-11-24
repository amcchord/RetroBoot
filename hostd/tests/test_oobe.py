"""First-run details from the November 23 2026 out-of-box pass: the Mac a new computer is
offered first, the Time Zone list, word-safe descriptions, stoppable downloads, the space
check before a download and the USB archive wording."""
import asyncio
import json
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


if __name__ == "__main__":
    unittest.main()
