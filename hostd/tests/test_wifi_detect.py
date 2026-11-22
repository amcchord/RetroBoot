"""Wi-Fi detection at hostd start (fleet2 hardware finding, October 12 2026).

Since the hostboot change hostd starts beside NetworkManager. On the Ryzen and the
N5105, NetworkManager listed the Wi-Fi device 0.3-1 s after hostd asked, so the wifi
capability read False for the whole boot and the AirPort bridge fell back to the
RetroBoot network although the computer has Wi-Fi. hostd now also counts a radio the
kernel registered (/sys/class/ieee80211), and keeps looking for a driver that binds
later; a bridge that fell back then moves to NetworkManager.
"""
import asyncio
import tempfile
import types
import unittest
from pathlib import Path

import helpers
from retroboot.airport import AirPort
from retroboot.network import NetworkAdapter
from test_lifecycle import Base


class WifiHardwareTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.dir = Path(self.td.name) / "ieee80211"

    def tearDown(self):
        self.td.cleanup()

    def test_registered_radio_counts(self):
        net = NetworkAdapter(enabled=True, ieee80211_dir=self.dir)
        self.assertFalse(net.wifi_radio_registered())          # no directory: no cfg80211
        self.dir.mkdir()
        self.assertFalse(net.wifi_radio_registered())          # cfg80211 loaded, no radio
        (self.dir / "phy0").mkdir()
        self.assertTrue(net.wifi_radio_registered())

    def test_disabled_adapter_reports_none(self):
        (self.dir / "phy0").mkdir(parents=True)
        self.assertFalse(NetworkAdapter(enabled=False, ieee80211_dir=self.dir).wifi_radio_registered())


class EarlyRadio(Base):
    """NetworkManager doesn't list the device yet; the kernel has the radio."""

    def before_start(self):
        async def no_device():
            return None
        self.app.network.wifi_device = no_device
        self.app.network.wifi_radio_registered = lambda: True

    async def test_capability_is_on_from_the_start(self):
        self.assertTrue(self.app.capabilities()["wifi"])
        self.assertIsNone(self.app._wifi_task)
        self.assertEqual(self.ev("wifi_late"), [])


class LateRadio(Base):
    """The driver binds after hostd started: the capability follows within the watch."""

    def before_start(self):
        self.present = False

        async def device():
            return "wlan0" if self.present else None
        self.app.network.wifi_device = device
        self.app.network.wifi_radio_registered = lambda: False
        self.app.wifi_watch_s = (0.05, 5.0)

    async def test_capability_follows_a_late_driver(self):
        self.assertFalse(self.app.capabilities()["wifi"])
        self.present = True
        await helpers.wait_for(lambda: self.ev("wifi_late"), timeout=5, what="wifi_late")
        self.assertTrue(self.app.capabilities()["wifi"])


class NoRadio(Base):
    def before_start(self):
        async def no_device():
            return None
        self.app.network.wifi_device = no_device
        self.app.network.wifi_radio_registered = lambda: False
        self.app.wifi_watch_s = (0.05, 0.3)

    async def test_watch_gives_up_quietly(self):
        await asyncio.sleep(0.6)
        self.assertTrue(self.app._wifi_task.done())
        self.assertFalse(self.app.capabilities()["wifi"])
        self.assertEqual(self.ev("wifi_late"), [])


class BridgeMoveTests(unittest.IsolatedAsyncioTestCase):
    def hook(self, backend="auto", tools=True, current="virtual"):
        events = []
        app = types.SimpleNamespace(cfg=types.SimpleNamespace(airport_backend=backend, allow_network_tools=tools),
                                    lifecycle=types.SimpleNamespace(event=lambda name, **kw: events.append((name, kw))))
        ap = AirPort(app)
        stopped = []

        async def stop():
            stopped.append(current)
        ap.service = types.SimpleNamespace(backend=types.SimpleNamespace(name=current), stop=stop)
        started = []

        async def start():
            started.append(True)
            ap.service = types.SimpleNamespace(backend=types.SimpleNamespace(name="networkmanager"))
        ap.start = start
        return ap, stopped, started, events

    async def test_virtual_fallback_moves_to_networkmanager(self):
        ap, stopped, started, events = self.hook()
        await ap.wifi_appeared()
        self.assertEqual((stopped, started), (["virtual"], [True]))
        self.assertEqual(events, [("airport_backend_changed", {"backend": "networkmanager"})])

    async def test_left_alone_when_chosen_or_already_real(self):
        for kw in ({"backend": "virtual"}, {"tools": False}, {"current": "networkmanager"}):
            ap, stopped, started, events = self.hook(**kw)
            await ap.wifi_appeared()
            self.assertEqual((stopped, started, events), ([], [], []), kw)


if __name__ == "__main__":
    unittest.main()
