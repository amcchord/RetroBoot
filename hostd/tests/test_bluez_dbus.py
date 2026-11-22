"""The real BlueZ backend (retroboot/bluez.py) over a private D-Bus against fakes/fake_bluez.py.

Covers what the in-process fake cannot: D-Bus messages and signatures,
ObjectManager/PropertiesChanged handling, the exported Agent1 object (BlueZ
calling DisplayPasskey/RequestPinCode/RequestConfirmation, unsolicited requests
refused with org.bluez.Error.Rejected), error-name mapping, Trusted/Powered
through Properties.Set, RemoveDevice, and bluetoothd restarting.

Needs python3-dbus-fast and dbus-daemon (skipped otherwise).
"""
import asyncio
import shutil
import unittest

import helpers
from test_bluetooth import add_input

try:
    from fakes.fake_bluez import HID, FakeBluez, dev_path, private_bus
    from retroboot.bluez import AGENT_PATH, BluezBackend, map_error
    HAVE = True
except ImportError:     # no python3-dbus-fast
    HAVE = False

from retroboot.bluetooth import Bluetooth, BluetoothError
from retroboot.hostd import Hostd

KB = "C8:2B:96:1A:4E:01"
OLDKB = "C8:2B:96:1A:4E:03"
PAD = "C8:2B:96:1A:4E:05"
OLDMOUSE = "C8:2B:96:1A:4E:06"
COVER = "C8:2B:96:1A:4E:07"
GONE = "C8:2B:96:1A:4E:08"


@unittest.skipUnless(HAVE and shutil.which("dbus-daemon"), "needs python3-dbus-fast and dbus-daemon")
class BluezOverDbus(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.proc, self.addr, self.bus_dir = private_bus()
        self.addCleanup(self.bus_dir.cleanup)
        self.addCleanup(self.proc.wait)
        self.addCleanup(self.proc.terminate)
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        add_input(self.root, 1, "USB Keyboard", 0x03, "keyboard")
        self.fb = self.new_bluez()
        await self.fb.start()
        async def stop_bluez():
            await self.fb.stop()
        self.addAsyncCleanup(stop_bluez)
        cfg = helpers.make_config(self.root)
        self.app = Hostd(cfg)
        self.app.loop = asyncio.get_running_loop()
        self.app.settings.store.load()
        self.backend = BluezBackend(bus_address=self.addr)
        self.app.bluetooth = Bluetooth(self.app, self.backend)
        self.app.bluetooth.TICK_S = 0.2
        await self.app.bluetooth.start()
        self.addAsyncCleanup(self.app.bluetooth.stop)
        await helpers.wait_for(lambda: self.bt.powered(), 5, what="adapter powered")

    def new_bluez(self):
        fb = FakeBluez(self.addr)
        fb.add(KB, "Bluetooth Keyboard", cls=0x002540, method_="passkey", battery=77, rssi=-50)
        fb.add(OLDKB, "Old Keyboard", cls=0x000540, method_="pin", rssi=-70)
        fb.add(OLDMOUSE, "Old Mouse", cls=0x000580, method_="pin", rssi=-72)
        fb.add(COVER, "Keyboard Cover", cls=0x0025C0, method_="confirm", rssi=-60)
        fb.add(PAD, "Magic Trackpad", cls=0x002580, paired=True, connected=True, battery=41)
        fb.add(GONE, "Travel Keyboard", cls=0x002540, method_="unreachable", rssi=-85)
        fb.add("C8:2B:96:1A:4E:09", "Someone's Phone", cls=0x5A020C, rssi=-40, uuids=[])
        return fb

    @property
    def bt(self):
        return self.app.bluetooth

    def dev(self, address):
        return self.bt.device(address)

    async def scan(self):
        self.bt.scan("test", 60)
        await helpers.wait_for(lambda: self.dev(KB) and self.dev(COVER) and self.dev(GONE), 5, what="devices found")

    async def pair(self, address):
        job = self.app.bluetooth_job("pair", origin="panel", address=address)
        return job

    async def stage(self, stage):
        await helpers.wait_for(lambda: self.bt.session and self.bt.session.stage == stage, 5, what=stage)
        return self.bt.session

    async def done(self, job):
        await asyncio.wait_for(job.done.wait(), 10)
        return job

    async def test_agent_power_and_paired_devices(self):
        self.assertIn(("RegisterAgent", AGENT_PATH, "KeyboardDisplay"), self.fb.calls)
        self.assertIn(("RequestDefaultAgent", AGENT_PATH), self.fb.calls)
        self.assertIn(("Powered", True), self.fb.calls)
        self.assertEqual(self.bt.public()["adapter"], {"address": "00:1A:7D:DA:71:13", "name": "retroboot"})
        pad = self.dev(PAD)
        self.assertEqual((pad["kind"], pad["paired"], pad["connected"], pad["battery"]), ("trackpad", True, True, 41))
        self.assertIsNone(self.dev(KB), "not listed before a scan")
        self.assertEqual(self.backend.describe()["agent"], True)

    async def test_scan_filter_and_found_devices(self):
        await self.scan()
        flt = [c for c in self.fb.calls if c[0] == "SetDiscoveryFilter"][0][1]
        self.assertEqual(flt, {"Transport": "auto", "DuplicateData": False, "Discoverable": True})
        kb = self.dev(KB)
        self.assertEqual((kb["kind"], kb["paired"], kb["rssi"], kb["signal"]), ("keyboard", False, -50, 4))
        self.assertIsNone(self.dev("C8:2B:96:1A:4E:09"), "phones are not listed")
        self.bt.stop_scan("test")
        await helpers.wait_for(lambda: ("StopDiscovery",) in self.fb.calls, 5, what="stop")
        await helpers.wait_for(lambda: not self.bt.discovering(), 5, what="not discovering")
        self.assertEqual(self.dev(KB)["rssi"], -50, "a device seen moments ago stays listed")

    async def test_passkey_pairing_trusts_and_connects(self):
        await self.scan()
        job = await self.pair(KB)
        s = await self.stage("passkey")
        self.assertEqual(s.passkey, "314159")
        self.assertIn(("StopDiscovery",), self.fb.calls, "discovery pauses while pairing")
        self.fb.type_code(KB, "314159")
        await self.done(job)
        self.assertEqual(job.state, "succeeded", job.public())
        self.assertEqual(job.message, "“Bluetooth Keyboard” is connected.")
        self.assertIn(("Set", KB, "Trusted", True), self.fb.calls)
        self.assertIn(("Connect", KB), self.fb.calls)
        await helpers.wait_for(lambda: self.dev(KB)["battery"] == 77, 5, what="battery")
        d = self.dev(KB)
        self.assertEqual((d["paired"], d["trusted"], d["connected"]), (True, True, True))
        self.assertEqual(("agent", "DisplayPasskey", None), [c for c in self.fb.calls if c[0] == "agent"][0])

    async def test_wrong_passkey_and_cancel(self):
        await self.scan()
        job = await self.pair(KB)
        await self.stage("passkey")
        self.fb.type_code(KB, "000000")
        await self.done(job)
        self.assertEqual((job.state, job.error["code"]), ("failed", "auth_failed"))
        job = await self.pair(KB)
        await self.stage("passkey")
        self.assertTrue(await self.bt.cancel_pairing())
        await self.done(job)
        self.assertEqual(job.error["code"], "canceled")
        self.assertIn(("CancelPairing", KB), self.fb.calls)

    async def test_legacy_pin_keyboard_and_mouse(self):
        await self.scan()
        job = await self.pair(OLDKB)
        s = await self.stage("pin")
        pin = s.passkey
        self.assertRegex(pin, r"^\d{6}$")
        self.assertEqual(self.fb.pins[-1], pin)
        self.fb.type_code(OLDKB, pin)
        await self.done(job)
        self.assertEqual(job.state, "succeeded", job.public())
        job = await self.pair(OLDMOUSE)
        await self.done(job)
        self.assertEqual(job.state, "succeeded", job.public())
        self.assertEqual(self.fb.pins[-1], "0000")

    async def test_numeric_comparison(self):
        await self.scan()
        job = await self.pair(COVER)
        s = await self.stage("confirm")
        self.assertEqual(s.passkey, "271828")
        self.assertTrue(self.bt.confirm(False))
        await self.done(job)
        self.assertEqual(job.error["code"], "rejected")
        job = await self.pair(COVER)
        await self.stage("confirm")
        self.bt.confirm(True)
        await self.done(job)
        self.assertEqual(job.state, "succeeded", job.public())

    async def test_unreachable_maps_to_its_reason(self):
        await self.scan()
        job = await self.pair(GONE)
        await self.done(job)
        self.assertEqual(job.error["code"], "unreachable")
        self.assertIn("Make sure it is turned on", job.message)
        self.assertEqual(map_error("org.bluez.Error.Failed", "br-connection-page-timeout").code, "unreachable")
        self.assertEqual(map_error("org.bluez.Error.AuthenticationTimeout", "").code, "timeout")

    async def test_unsolicited_agent_requests_are_rejected(self):
        reply = await self.fb.ask("RequestAuthorization", "o", [dev_path(KB)])
        self.assertEqual(reply.error_name, "org.bluez.Error.Rejected")
        reply = await self.fb.ask("RequestPinCode", "o", [dev_path(KB)])
        self.assertEqual(reply.error_name, "org.bluez.Error.Rejected")
        reply = await self.fb.ask("AuthorizeService", "os", [dev_path(PAD), HID])
        self.assertIsNone(reply.error_name)
        reply = await self.fb.ask("AuthorizeService", "os", [dev_path(PAD), "0000110b-0000-1000-8000-00805f9b34fb"])
        self.assertEqual(reply.error_name, "org.bluez.Error.Rejected")

    async def test_forget_and_power(self):
        await self.bt.forget(PAD)
        self.assertIn(("RemoveDevice", dev_path(PAD)), self.fb.calls)
        await helpers.wait_for(lambda: self.dev(PAD) is None, 5, what="forgotten")
        with self.assertRaises(BluetoothError):
            await self.bt.forget(PAD)
        self.app.settings.patch(self.app.settings.revision, {"bluetooth.enabled": False},
                                self.app.validation_context())
        await self.app.apply_engine.apply(self.app.settings.revision, False, None, "test")
        await helpers.wait_for(lambda: not self.bt.powered(), 5, what="powered off")
        self.assertIn(("Powered", False), self.fb.calls)

    async def test_bluetoothd_restart_is_followed(self):
        await self.fb.stop()
        await helpers.wait_for(lambda: not self.bt.capable(), 5, what="BlueZ gone")
        self.fb = self.new_bluez()
        await self.fb.start()
        await helpers.wait_for(lambda: self.bt.capable() and self.backend.agent_registered, 5, what="back")
        self.assertIn(("RegisterAgent", AGENT_PATH, "KeyboardDisplay"), self.fb.calls)
        await helpers.wait_for(lambda: self.bt.powered(), 5, what="powered again")
        self.assertIn(("Powered", True), self.fb.calls)
        self.assertEqual(self.backend.generation, 2)
        await self.scan()

    async def test_quick_restart_leaves_no_unpowered_adapter(self):
        """``systemctl restart bluetooth`` on the GPD: gone and back between two ticks,
        unpowered. hostd powers it again (it used to stay off until a reboot)."""
        self.bt.TICK_S = 1.0
        await asyncio.sleep(1.1)
        await self.fb.stop()
        self.fb = self.new_bluez()
        await self.fb.start()
        await helpers.wait_for(lambda: self.backend.generation == 2, 5, what="back")
        await helpers.wait_for(lambda: self.bt.powered(), 5, what="powered again")
        self.assertIn(("Powered", True), self.fb.calls)


if __name__ == "__main__":
    unittest.main()
