"""Battery passthrough end to end (real hostd, guest-launcher, panel and power
bridge; fake QEMU and presenter): the N5105's critical-battery path with Mac OS
asleep.  hostd's notice closes itself (the real panel's auto-close, shortened),
the clean shutdown starts, hostd wakes Mac OS over the power bridge before the
power key, and Mac OS shuts down; also the bridge after a hostd restart."""
import asyncio
import json
import unittest
from unittest import mock

import helpers
from retroboot.panel import controller
from test_laptop import _w, make_battery
from test_lifecycle import Base


class PMUBase(Base):
    cfg_overrides = {"power_controller": "pmu", "battery_poll_s": 0.2}

    def extra_conf(self):
        return {"sleep_s": 0.3}

    def before_start(self):
        man = self.pkg / "manifest.json"
        m = json.loads(man.read_text())
        m["features"] = ["pmu-battery-1"]
        man.write_text(json.dumps(m))
        self.bat, _ = make_battery(self.root, pct=30, status="Discharging", ac=False)
        self.app.battery.POLL_S = 0.2
        self.app.battery.WAKE_SETTLE_S = 0.2
        self.app.laptop._interval = lambda: 0.2

    async def asyncSetUp(self):
        orig = controller.MessageAlert.__init__

        def quick(alert, *a, **kw):
            orig(alert, *a, **kw)
            if alert.auto:
                alert.auto = alert.tick = 0.3          # the 8 s auto-close, shortened
        patcher = mock.patch.object(controller.MessageAlert, "__init__", quick)
        patcher.start()
        self.addCleanup(patcher.stop)
        await super().asyncSetUp()


class CriticalWhileAsleep(PMUBase):
    async def test_wakes_mac_os_then_shuts_it_down(self):
        await self.running()
        await self.until_event("finder_detected")
        await self.until_event("guest_sleep")
        self.assertTrue(self.app.battery.passthrough_active())
        self.assertTrue(self.app.battery.asleep)
        _w(self.bat / "capacity", 8)                   # hostd's passthrough critical level
        await self.until_event("battery_critical", timeout=10)
        # nobody clicks: the notice closes itself and the clean shutdown goes on
        await self.until_event("host_power_request", timeout=30)
        await helpers.wait_for(lambda: any("keys" in r for r in helpers.fake_qemu_log(self.pkg)), 10,
                               what="fake QEMU exit")
        last = [r for r in helpers.fake_qemu_log(self.pkg) if "keys" in r][-1]
        self.assertEqual(last["keys"], ["sleep", "woke:host", "power", "ret"])
        self.assertEqual([m["type"] for m in last["bridge"]][:2], ["hello", "wake"])
        self.assertEqual(self.ev("guest_wake_request")[0]["why"], "power-dialog")
        self.assertEqual(self.ev("guest_wake")[0]["reason"], "host")
        self.assertEqual(self.ev("host_power_request")[0]["origin"], "battery")
        self.assertEqual(self.ev("guest_exited")[-1]["reason"], "guest-shutdown")


class BridgeAfterRestart(PMUBase):
    def extra_conf(self):
        return {}

    async def test_passthrough_is_active_again_after_a_hostd_restart(self):
        await self.running()
        await self.until_event("finder_detected")
        await helpers.wait_for(lambda: self.app.battery.writer is not None, 5, what="bridge")
        sup = self.app.supervisor
        session = sup.session
        # a new hostd: the bridge state starts empty, adoption restores it from argv.json
        self.app.battery.session_pmu = None
        self.app.keys.buttons = "stale"
        sup._adopt_hardware(session)
        self.assertTrue(self.app.battery.passthrough_active())
        self.assertIsNone(self.app.keys.buttons)       # this launch had no buttons (no adb-buttons-1)
        # without a readable argv.json the bridge's hello still re-attaches it
        self.app.battery.session_pmu = None
        await self.app.battery._message({"type": "hello", "version": 1, "battery": True, "sleeping": False})
        self.assertTrue(self.app.battery.passthrough_active())
        self.assertTrue(self.app.battery.public()["active"])
        # the bridge carries what the laptop believes about the battery
        await helpers.wait_for(lambda: self.app.battery.last_sent and self.app.battery.last_sent.get("percent") == 30,
                               5, what="battery sent")


if __name__ == "__main__":
    unittest.main()
