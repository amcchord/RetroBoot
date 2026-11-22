"""Form factor: "the laptop" only on a portable (system battery or portable SMBIOS chassis)."""
import asyncio
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

import helpers
from retroboot import formfactor
from retroboot.hostd import Hostd


def sysfs(chassis=None, supplies=()):
    td = tempfile.TemporaryDirectory()
    root = Path(td.name)
    if chassis is not None:
        (root / "class/dmi/id").mkdir(parents=True)
        (root / "class/dmi/id/chassis_type").write_text(f"{chassis}\n")
    for name, kind, scope, present in supplies:
        d = root / "class/power_supply" / name
        d.mkdir(parents=True)
        (d / "type").write_text(kind + "\n")
        if scope:
            (d / "scope").write_text(scope + "\n")
        if present is not None:
            (d / "present").write_text(present + "\n")
    return td, root


class DetectTests(unittest.TestCase):
    def check(self, expect, **kw):
        td, root = sysfs(**kw)
        self.addCleanup(td.cleanup)
        return self.assertEqual(formfactor.detect(root)["form_factor"], expect)

    def test_desktop_and_unknown(self):
        self.check("desktop")                                   # no DMI, no supplies (VMs, containers)
        self.check("desktop", chassis=3)                        # Desktop
        self.check("desktop", chassis=35)                       # Mini PC
        self.check("desktop", chassis=1, supplies=[("AC", "Mains", None, None)])

    def test_portable(self):
        self.check("portable", chassis=10)                      # Notebook
        self.check("portable", chassis=31)                      # Convertible
        self.check("portable", chassis=3, supplies=[("BAT0", "Battery", None, "1")])   # a battery wins

    def test_peripheral_and_absent_batteries_dont_count(self):
        self.check("desktop", chassis=3, supplies=[("hidpp_battery_0", "Battery", "Device", None),
                                                   ("BAT1", "Battery", None, "0")])

    def test_override(self):
        td, root = sysfs(chassis=10)
        self.addCleanup(td.cleanup)
        self.assertEqual(formfactor.detect(root, "desktop")["form_factor"], "desktop")
        self.assertEqual(formfactor.detect(Path("/nonexistent"), "portable")["form_factor"], "portable")
        self.assertEqual(formfactor.detect(root)["chassis_type"], 10)


class StatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_and_session_report_it(self):
        td, root = helpers.scratch()
        self.addCleanup(td.cleanup)
        helpers.make_package(root)
        fake, sys_root = sysfs(chassis=9)
        self.addCleanup(fake.cleanup)
        app = Hostd(helpers.make_config(root, sysfs_path=str(sys_root)))
        app.loop = asyncio.get_running_loop()
        app.settings.store.load()
        app.web.tls()
        client = TestClient(TestServer(app.web.web_app, host="127.0.0.1"))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        self.assertEqual((await (await client.get("/api/v1/session")).json())["form_factor"], "portable")
        self.assertEqual((await app.status())["host"]["form_factor"], "portable")


if __name__ == "__main__":
    unittest.main()
