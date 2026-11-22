"""mDNS name: show what avahi publishes; give a defaulted computer a stable unique name on a conflict."""
import asyncio
import json
import unittest
from pathlib import Path

import helpers
from retroboot import mdns


class FakeRun:
    def __init__(self, published="retroboot.local"):
        self.published = published
        self.calls = []

    async def __call__(self, argv, timeout=None):
        self.calls.append(argv)
        if "GetHostNameFqdn" in argv:
            if self.published is None:
                return 1, "", "Failed to connect to bus"
            return 0, json.dumps({"type": "s", "data": [self.published]}), ""
        if "SetHostName" in argv:
            self.published = argv[-1] + ".local"
            return 0, "", ""
        return 1, "", "unexpected"


class FakeSettings:
    def __init__(self, hostname):
        self.applied = {"management.hostname": hostname}
        self.desired = dict(self.applied)

    def set_desired_and_applied(self, changes, ctx):
        self.applied.update(changes)
        self.desired.update(changes)


class FakeNetwork:
    def __init__(self):
        self.names = []

    async def set_hostname(self, name):
        self.names.append(name)
        return True


class FakeLifecycle:
    def __init__(self):
        self.events = []

    def event(self, name, **kw):
        self.events.append((name, kw))


class FakeApp:
    def __init__(self, hostname):
        self.settings = FakeSettings(hostname)
        self.network = FakeNetwork()
        self.lifecycle = FakeLifecycle()

    def validation_context(self):
        return None


def make_sysfs(root: Path, ifaces: dict):
    for name, (address, kind) in ifaces.items():
        d = root / "class/net" / name
        d.mkdir(parents=True)
        (d / "address").write_text(address + "\n")
        (d / "type").write_text("772\n" if kind == "loopback" else "1\n")
        if kind != "virtual" and kind != "loopback":
            (d / "device").mkdir()
        if kind == "wireless":
            (d / "wireless").mkdir()
    return root


class MdnsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)

    def test_hardware_suffix_prefers_wired_physical(self):
        sysfs = make_sysfs(self.root / "a", {"lo": ("00:00:00:00:00:00", "loopback"),
                                             "docker0": ("02:42:ac:11:00:02", "virtual"),
                                             "wlp1s0": ("8c:12:ac:98:25:e7", "wireless"),
                                             "eno1": ("00:9e:9f:03:00:cb", "wired")})
        self.assertEqual(mdns.hardware_suffix(sysfs), "00cb")
        sysfs = make_sysfs(self.root / "b", {"wlp1s0": ("8c:12:ac:98:25:e7", "wireless")})
        self.assertEqual(mdns.hardware_suffix(sysfs), "25e7")
        self.assertIsNone(mdns.hardware_suffix(self.root / "none"))
        self.assertEqual(mdns.unique_default("25e7"), "retroboot-25e7")

    async def test_published_name_and_fallback(self):
        w = mdns.MdnsWatch(FakeApp("retroboot"), FakeRun(None))
        self.assertIsNone(await w.check())
        self.assertEqual(w.display_name("retroboot"), "retroboot.local")      # no avahi: the configured name

    async def test_default_name_free_is_kept(self):
        app, run = FakeApp("retroboot"), FakeRun("retroboot.local")
        w = mdns.MdnsWatch(app, run, sysfs=make_sysfs(self.root / "s", {"eno1": ("00:9e:9f:03:00:cb", "wired")}))
        self.assertEqual(await w.check(), "retroboot.local")
        self.assertEqual(app.settings.applied["management.hostname"], "retroboot")
        self.assertEqual(app.network.names, [])

    async def test_conflict_gives_a_stable_unique_name(self):
        app, run = FakeApp("retroboot"), FakeRun("retroboot-2.local")
        w = mdns.MdnsWatch(app, run, sysfs=make_sysfs(self.root / "s", {"eno1": ("00:9e:9f:03:00:cb", "wired")}))
        self.assertEqual(await w.check(), "retroboot-00cb.local")
        self.assertEqual(app.settings.applied["management.hostname"], "retroboot-00cb")
        self.assertEqual(app.network.names, ["retroboot-00cb"])
        self.assertTrue(any("SetHostName" in c for c in run.calls))
        self.assertEqual(app.lifecycle.events[0][0], "hostname_unique")
        self.assertEqual(w.display_name("retroboot-00cb"), "retroboot-00cb.local")
        # Settled: later checks change nothing.
        await w.check()
        self.assertEqual(app.network.names, ["retroboot-00cb"])

    async def test_users_own_name_is_never_changed(self):
        app, run = FakeApp("studio-mac"), FakeRun("studio-mac-2.local")
        w = mdns.MdnsWatch(app, run, sysfs=make_sysfs(self.root / "s", {"eno1": ("00:9e:9f:03:00:cb", "wired")}))
        self.assertEqual(await w.check(), "studio-mac-2.local")
        self.assertEqual(app.settings.applied["management.hostname"], "studio-mac")
        self.assertEqual(w.display_name("studio-mac"), "studio-mac-2.local")   # shows the real published name
        self.assertEqual(app.network.names, [])


if __name__ == "__main__":
    unittest.main()
