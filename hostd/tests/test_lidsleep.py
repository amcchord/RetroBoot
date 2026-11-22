"""Closing the lid puts Mac OS to sleep (lidsleep.py, lid_action "sleep").

Real hostd, guest-launcher, QMP and power bridge against fake_qemu on the PMU machine:
Mac OS 9.2.2's power dialog takes S (Sleep: the bridge says ``sleep``) and Escape
(Cancel), as the lab measured (tests/e2e/lidsleep_lab.py).  The lid arrives as the
presenter's ``lid`` event.  Every key the guest received is read from fake QEMU's log
when it exits (the last Shut Down of each test).
"""
import asyncio
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace

import helpers
from retroboot import cpuplace
from retroboot import platform as hostplatform
from retroboot.config import Config
from retroboot.lidpower import LidPower
from retroboot.lidsleep import LidSleep
from test_gemini import KERNEL_GOV, Affinity, fake_cpu_sysfs, fake_proc_threads, gemini_tree
from test_laptop import make_battery
from test_lifecycle import Base

POLICY = "devices/system/cpu/cpufreq/policy{}/{}"
KEYPAD = "module/matrix_keypad/parameters/poll_ms"


def gemini_sysfs(sysfs):
    """The Gemini's three cpufreq policies (the a72dvfs kernel: policy8 booted capped at
    1495 MHz below its 2587 MHz top) and the keyboard matrix polled every 30 ms."""
    fake_cpu_sysfs(sysfs)
    (sysfs / POLICY.format(8, "cpuinfo_max_freq")).write_text("2587000\n")
    (sysfs / KEYPAD).parent.mkdir(parents=True, exist_ok=True)
    (sysfs / KEYPAD).write_text("30\n")


def sysval(sysfs, n, name="scaling_max_freq"):
    return (sysfs / (KEYPAD if n == "keypad" else POLICY.format(n, name))).read_text().strip()


class LidBase(Base):
    cfg_overrides = {"power_controller": "pmu", "battery_poll_s": 0.2, "lid_action": "sleep",
                     "lid_sleep_delay_s": 0.2, "lid_busy_wait_s": 3.0, "lid_sleep_wait_s": 5.0,
                     "power_dialog_wait_s": 6.0}

    def before_start(self):
        man = self.pkg / "manifest.json"
        m = json.loads(man.read_text())
        m["features"] = ["pmu-battery-1"]
        man.write_text(json.dumps(m))
        # current_now 800 mA, voltage_now 7.6 V
        self.bat, _ = make_battery(self.root, pct=60, status="Discharging", ac=False, energy=False)
        self.app.battery.POLL_S = 0.2
        self.app.battery.WAKE_SETTLE_S = 0.2
        self.app.laptop._interval = lambda: 0.2
        self.app.lidsleep.ASLEEP_SAMPLE_S = 0.5

    async def ready(self):
        """Mac OS at the Finder on the PMU machine with its bridge."""
        await self.running()
        await self.until_event("finder_detected")
        await helpers.wait_for(lambda: self.app.battery.writer is not None and self.app.battery.passthrough_active(),
                               10, what="power bridge")

    async def lid(self, open_: bool):
        self.fp.emit({"event": "lid", "open": open_})
        await helpers.wait_for(lambda: self.app.laptop.lid_open is open_, 5, what="lid")

    def displays(self) -> list[tuple]:
        return [(r.get("output"), r.get("on")) for r in self.fp.requests_of("display_power")]

    async def keys_at_shutdown(self) -> list[str]:
        job = await self.job(self.app.lifecycle_job("shutdown", poweroff=False, origin="test"), 60)
        self.assertEqual(job.state, "succeeded", job.public())
        rows = [r for r in helpers.fake_qemu_log(self.pkg) if "keys" in r]
        return rows[-1]["keys"]

    async def quiet(self, secs: float):
        await asyncio.sleep(secs)


class CloseSleepsOpenWakes(LidBase):
    async def test_close_sleep_open_wake_twice(self):
        await self.ready()
        await self.lid(False)
        await self.until_event("lid_sleep_done", timeout=20)
        req = self.ev("lid_sleep_request")[0]
        self.assertEqual((req["dialog"], req["busy"], req["battery_ma"], req["battery_mv"]),
                         ("power key", False, 800, 7600))
        conf = self.ev("power_dialog_confirmed")[0]
        self.assertEqual((conf["origin"], conf["key"], conf["dialog_from"], conf["buttons"]), ("lid", "s", "lid", 4))
        done = self.ev("lid_sleep_done")[0]
        self.assertEqual((done["battery_ma"], done["dialog"]), (800, "power key"))
        self.assertTrue(self.app.battery.asleep)
        self.assertIsNone(self.app.supervisor.power_key)        # nothing left pending
        self.assertEqual(self.displays()[:2], [("internal", False), ("all", False)])
        st = (await self.app.status())["power"]["lid_sleep"]
        self.assertEqual((st["action"], st["closed"], st["last"]["outcome"]), ("sleep", True, "lid_sleep_done"))
        await self.until_event("lid_asleep_power")
        self.assertEqual(self.ev("lid_asleep_power")[0]["battery_ma"], 800)
        # open: Mac OS wakes through the bridge, and its wake turns the panel on
        n = len(self.displays())
        await self.lid(True)
        await self.until_event("lid_wake")
        wake = self.ev("lid_wake")[0]
        self.assertEqual((wake["woke"], wake["battery_ma"]), (True, 800))
        await helpers.wait_for(lambda: ("internal", True) in self.displays()[n:], 5, what="panel on")
        self.assertEqual(self.ev("guest_wake")[0]["reason"], "host")
        # a second close is a second request
        await self.lid(False)
        await self.until_event("lid_sleep_done", 2, timeout=20)
        await self.lid(True)
        await self.until_event("lid_wake", 2)
        self.assertEqual(await self.keys_at_shutdown(),
                         ["power", "s", "sleep", "woke:host", "power", "s", "sleep", "woke:host", "power", "ret"])
        self.assertFalse(self.ev("lid_sleep_skipped"))


class BounceIsNotAClose(LidBase):
    async def test_quick_open_sends_nothing(self):
        await self.ready()
        await self.lid(False)
        await self.lid(True)
        await self.until_event("lid_sleep_skipped")
        skipped = self.ev("lid_sleep_skipped")[0]
        self.assertEqual((skipped["reason"], skipped["phase"]), ("lid opened", "waiting"))
        await self.quiet(2.5)
        self.assertFalse(self.ev("power_key_sent"))
        self.assertFalse(self.ev("lid_sleep_request"))
        self.assertEqual(self.displays(), [("internal", False), ("internal", True)])
        self.assertEqual(await self.keys_at_shutdown(), ["power", "ret"])


class OpenWhileTheDialogComes(LidBase):
    """A busy Mac OS draws the dialog 2 s after the key; the lid opens meanwhile: no S,
    and the dialog, once it is there, gets Escape (no second power key)."""
    modes = ["slowdialog:2"]

    async def test_escape_once_the_dialog_is_there(self):
        await self.ready()
        await self.lid(False)
        await self.until_event("power_key_sent", timeout=20)
        await self.lid(True)
        await self.until_event("lid_sleep_skipped", timeout=20)
        skipped = self.ev("lid_sleep_skipped")[0]
        self.assertEqual((skipped["reason"], skipped["dialog_cancelled"]), ("lid opened", True))
        self.assertIn(skipped["phase"], ("key", "dialog"))
        self.assertEqual(self.ev("power_dialog_cancelled")[0]["origin"], "lid")
        self.assertFalse(self.ev("power_dialog_confirmed"))
        self.assertFalse(self.app.battery.asleep)
        self.assertEqual(await self.keys_at_shutdown(), ["power", "esc", "power", "ret"])


class OpenRightAfterS(LidBase):
    """S went out; Mac OS takes 2 s to sleep; the lid opens meanwhile: once Mac OS
    sleeps it is woken again, and the panel ends up on."""

    def extra_conf(self):
        return {"s_sleep_s": 2.0}

    async def test_wakes_it_again(self):
        await self.ready()
        await self.lid(False)
        await helpers.wait_for(lambda: [e for e in self.ev("power_dialog_confirmed") if e.get("origin") == "lid"],
                               20, what="S")
        await self.lid(True)
        await self.until_event("lid_sleep_skipped", timeout=20)
        skipped = self.ev("lid_sleep_skipped")[0]
        self.assertEqual((skipped["reason"], skipped["phase"], skipped["woke"]), ("lid opened", "sleeping", True))
        self.assertEqual(self.ev("lid_wake")[0]["after"], "sleep request")
        self.assertFalse(self.app.battery.asleep)
        self.assertEqual([d for d in self.displays() if d[0] == "internal"][-1], ("internal", True))
        self.assertEqual(await self.keys_at_shutdown(), ["power", "s", "sleep", "woke:host", "power", "ret"])


class PowerButtonDialogLeftOpen(LidBase):
    """The power button's dialog is up (Mac OS spins in it) when the lid closes: S
    straight away, never a second power key (it would close the dialog).  Shut Down
    with the lid still closed wakes Mac OS first and leaves the panel off."""

    def extra_conf(self):
        return {"dialog_busy": True}

    async def test_answers_the_open_dialog(self):
        await self.ready()
        self.fp.emit({"event": "power_button"})
        await self.until_event("power_dialog_detected", timeout=20)
        await helpers.wait_for(lambda: not self.app.supervisor.power_pending, 10, what="button request over")
        await self.lid(False)
        await self.until_event("lid_sleep_done", timeout=20)
        self.assertEqual(self.ev("lid_sleep_request")[0]["dialog"], "already open")
        self.assertEqual(self.ev("lid_sleep_done")[0]["dialog"], "already open")
        self.assertEqual(len(self.ev("power_key_sent")), 1)               # the button's only
        self.assertEqual(self.ev("power_dialog_confirmed")[0]["dialog_from"], "button")
        n = len(self.displays())
        keys = await self.keys_at_shutdown()
        self.assertEqual(keys, ["power", "s", "sleep", "woke:host", "power", "ret"])
        self.assertNotIn(("internal", True), self.displays()[n:])           # the lid is still closed


class BusyMacOSGetsTheKeyAfterTheWait(LidBase):
    """Mac OS stays busy with nothing on screen: after lid_busy_wait_s the key goes
    anyway (as the power button's would), and the dialog is watched for until it comes."""

    def extra_conf(self):
        return {"busy_until": str(self.pkg / "mac-os-idle")}

    async def test_key_after_the_busy_wait(self):
        await self.ready()
        await self.lid(False)
        await self.until_event("lid_sleep_request", timeout=20)
        req = self.ev("lid_sleep_request")[0]
        self.assertTrue(req["busy"])
        self.assertGreaterEqual(req["after_close_s"], 3.0)
        await self.until_event("power_key_sent")
        await asyncio.sleep(1.0)
        (self.pkg / "mac-os-idle").touch()                  # Mac OS gets to the key: the dialog
        await self.until_event("lid_sleep_done", timeout=20)
        self.assertEqual(len(self.ev("power_key_sent")), 1)
        self.assertEqual(await self.keys_at_shutdown(), ["power", "s", "sleep", "woke:host", "power", "ret"])


class ModalAlertIsLeftAlone(LidBase):
    """An application's modal alert (menus dimmed, Mac OS spinning in it): no key, no S."""

    def extra_conf(self):
        return {"alert_s": 0.2, "alert_busy": True, "alert_dims": True}

    async def test_skipped(self):
        await self.ready()
        await self.lid(False)
        await self.until_event("lid_sleep_skipped", timeout=20)
        skipped = self.ev("lid_sleep_skipped")[0]
        self.assertEqual((skipped["reason"], skipped["busy"]), ("alert on screen", True))
        self.assertFalse(self.ev("power_key_sent"))
        self.assertFalse(self.app.battery.asleep)


class IdleModalAlertIsLeftAlone(LidBase):
    """The alert comes after the Finder was seen with its menus enabled, and Mac OS idles
    in it: the dimmed menus are enough, no key."""

    def extra_conf(self):
        return {"alert_s": 2.5, "alert_dims": True}

    async def test_skipped(self):
        await self.ready()
        await asyncio.sleep(3.0)
        await self.lid(False)
        await self.until_event("lid_sleep_skipped", timeout=20)
        skipped = self.ev("lid_sleep_skipped")[0]
        self.assertEqual((skipped.get("reason"), skipped.get("busy")), ("alert on screen", False), skipped)
        self.assertFalse(self.ev("power_key_sent"))


class WaitsForTheFinder(LidBase):
    def extra_conf(self):
        return {"finder_s": 3.0}

    async def test_lid_closed_during_startup(self):
        await self.until_event("guest_content")
        await helpers.wait_for(lambda: self.app.battery.writer is not None, 10, what="power bridge")
        await self.lid(False)
        await self.until_event("lid_sleep_done", timeout=30)
        self.assertGreaterEqual(self.ev("power_key_sent")[0]["since_finder_s"], 0.0)
        self.assertEqual(self.ev("lid_sleep_done")[0]["episode"], 1)


class WaitsForAJob(LidBase):
    async def test_job_holding_the_guest(self):
        await self.ready()
        release = asyncio.Event()

        async def body(job):
            await release.wait()
            return {}
        job = self.app.jobs.start("test.hold", body, resources=("guest",))
        await self.lid(False)
        await asyncio.sleep(2.5)
        self.assertFalse(self.ev("power_key_sent"))
        self.assertEqual((await self.app.status())["power"]["lid_sleep"]["phase"], "waiting")
        release.set()
        await self.job(job)
        await self.until_event("lid_sleep_done", timeout=20)


class UserWakesMacOSWithTheLidClosed(LidBase):
    """A key wakes Mac OS while the lid stays closed: the panel stays off and hostd does
    not put it back to sleep; the next close does."""

    async def test_no_second_request_until_the_next_close(self):
        await self.ready()
        await self.lid(False)
        await self.until_event("lid_sleep_done", timeout=20)
        n = len(self.displays())
        await self.app.supervisor.qmp.send_key("a")
        await self.until_event("guest_wake")
        self.assertEqual(self.ev("guest_wake")[0]["reason"], "key")
        await asyncio.sleep(3.0)
        self.assertEqual(len(self.ev("power_key_sent")), 1)
        self.assertEqual(len(self.ev("lid_sleep_request")), 1)
        self.assertNotIn(("internal", True), self.displays()[n:])
        await self.lid(True)
        await helpers.wait_for(lambda: ("internal", True) in self.displays()[n:], 5, what="panel on")
        self.assertFalse(self.ev("lid_wake"))                       # it was awake already
        await self.lid(False)
        await self.until_event("lid_sleep_done", 2, timeout=20)
        self.assertEqual(await self.keys_at_shutdown(),
                         ["power", "s", "sleep", "wake:a", "woke:key", "power", "s", "sleep", "woke:host",
                          "power", "ret"])


class NoSleepButton(LidBase):
    """S does nothing (a dialog without Sleep): the dialog is cancelled, Mac OS stays up."""

    def extra_conf(self):
        return {"no_sleep_button": True}

    async def test_cancelled(self):
        await self.ready()
        await self.lid(False)
        await self.until_event("lid_sleep_skipped", timeout=30)
        skipped = self.ev("lid_sleep_skipped")[0]
        self.assertEqual((skipped["reason"], skipped["dialog_cancelled"]), ("no sleep after S", True))
        self.assertIsNone(self.app.supervisor.power_key)
        self.assertEqual(await self.keys_at_shutdown(), ["power", "s", "esc", "power", "ret"])


class ExternalDisplayKeepsMacOSAwake(LidBase):
    async def test_skipped(self):
        await self.ready()
        outs = [{"connector": "DSI-1", "internal": True}, {"connector": "HDMI-A-1", "internal": False}]
        self.fp.display["outputs"] = outs
        self.app.presenter.display = dict(self.app.presenter.display or {}, outputs=outs)
        await self.lid(False)
        await self.until_event("lid_sleep_skipped", timeout=20)
        self.assertEqual(self.ev("lid_sleep_skipped")[0]["reason"], "external display")
        self.assertFalse(self.ev("power_key_sent"))
        self.assertEqual(self.displays(), [("internal", False)])


class PCKeepsBlanking(LidBase):
    """lid_action "auto" on a PC: the panel goes off and on, Mac OS is left alone."""
    cfg_overrides = {k: v for k, v in LidBase.cfg_overrides.items() if k != "lid_action"}

    async def test_blank_only(self):
        await self.ready()
        self.assertEqual(self.app.lidsleep.action, "blank")
        await self.lid(False)
        await asyncio.sleep(2.5)
        await self.lid(True)
        self.assertFalse([e for e in self.app.lifecycle.recent if e["event"].startswith("lid_")])
        self.assertFalse(self.ev("power_key_sent"))
        self.assertEqual(self.displays(), [("internal", False), ("internal", True)])
        self.assertNotIn("lid_sleep", (await self.app.status())["power"])


class LowPowerUnit(unittest.TestCase):
    """lidpower.py against a fake Gemini sysfs, with cpuplace.py placing a fake QEMU."""
    QEMU = 4242

    def setUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        self.sysfs = self.root / "sys"
        gemini_sysfs(self.sysfs)
        fake_proc_threads(self.root / "proc", self.QEMU, [(4242, "qemu-system-ppc", 300), (4244, "CPU 0/TCG", 9000)])
        self.events = []
        self.gem = hostplatform.Platform(family="gemini", features=hostplatform.gemini_features())

    def make(self, platform=None, **cfg):
        c = helpers.make_config(self.root, **cfg)
        plat = platform or self.gem
        lifecycle = SimpleNamespace(event=lambda name, **kw: self.events.append((name, kw)))
        app = SimpleNamespace(cfg=c, platform=plat, paths=SimpleNamespace(run=self.root / "run"), lifecycle=lifecycle,
                              laptop=SimpleNamespace(battery_reading=lambda: {"battery_ma": 900}))
        self.aff = Affinity(self.sysfs)
        app.cpuplace = cpuplace.CpuPlacement(cpuplace.resolve(c, plat), sysfs=self.sysfs, proc=self.root / "proc",
                                             run_file=self.root / "run/cpuplace.json", lifecycle=lifecycle,
                                             setaffinity=self.aff.set, getaffinity=self.aff.get, self_pid=200)
        lp = LidPower(app)
        app.cpuplace.lid_hold = lp.hold
        return app, lp

    def v(self, n, name="scaling_max_freq"):
        return sysval(self.sysfs, n, name)

    def saved_file(self):
        f = self.root / "run/lidpower.json"
        return json.loads(f.read_text()) if f.exists() else None

    def test_enter_leave_with_cpuplace(self):
        app, lp = self.make()
        cp = app.cpuplace

        async def go():
            cp.guest_started("s1", lambda: self.QEMU)
            await helpers.wait_for(lambda: self.v(8) == "2587000", 5, what="max_mhz")
            self.assertTrue(await lp.enter("lid closed"))
            self.assertEqual([self.v(n) for n in (0, 4, 8, "keypad")], ["624000", "650000", "845000", "1000"])
            self.assertEqual(self.saved_file()["cpufreq"][str(self.sysfs / POLICY.format(8, "scaling_max_freq"))],
                             "2587000")
            on = [kw for n, kw in self.events if n == "lid_low_power"][-1]
            self.assertEqual((on["on"], on["policies_mhz"], on["keypad_poll_ms"], on["battery_ma"]),
                             (True, {"policy0": 624, "policy4": 650, "policy8": 845}, 1000, 900))
            self.assertFalse(await lp.enter("again"))
            # cpuplace's polls leave the capped policy alone
            self.assertTrue(cp.apply_once(self.QEMU)["lid_closed"])
            await asyncio.sleep(0.3)
            self.assertEqual((self.v(8), self.v(8, "scaling_governor")), ("845000", "schedutil"))
            # Mac OS sleeps: cpuplace gives its originals back; the boot cap waits for the lid
            await cp.guest_sleep(True)
            self.assertEqual((self.v(8), self.v(8, "scaling_governor")), ("845000", KERNEL_GOV))
            self.assertEqual(self.saved_file()["cpufreq"][str(self.sysfs / POLICY.format(8, "scaling_max_freq"))],
                             "1495000")
            # the lid opens: the exact values first (Mac OS still asleep: the boot cap), then the wake
            self.assertTrue(await lp.leave("lid opened"))
            self.assertEqual([self.v(n) for n in (0, 4, 8, "keypad")], ["1118000", "1352000", "1495000", "30"])
            self.assertIsNone(self.saved_file())
            self.assertFalse(cp.lid_capped)
            await cp.guest_sleep(False)
            self.assertEqual((self.v(8), self.v(8, "scaling_governor")), ("2587000", "schedutil"))
            # awake all along: the leave puts cpuplace's plan straight back
            await lp.enter("lid closed")
            self.assertEqual(self.v(8), "845000")
            await lp.leave("lid opened")
            self.assertEqual((self.v(8), self.v("keypad")), ("2587000", "30"))
            # Mac OS stops while the lid is closed: cpuplace's restore goes to the lid's values
            await lp.enter("lid closed")
            cp.guest_stopped("s1")
            self.assertEqual((self.v(8), self.v(8, "scaling_governor")), ("845000", KERNEL_GOV))
            await lp.leave("lid opened")
            self.assertEqual([self.v(n) for n in (0, 4, 8)], ["1118000", "1352000", "1495000"])
        asyncio.run(go())
        self.assertFalse(lp.public()["active"])

    def test_hostd_restart_with_the_lid_closed(self):
        app, lp = self.make()
        asyncio.run(lp.enter("lid closed"))
        app2, lp2 = self.make()                                    # the next hostd
        lp2.start(lid_open=False)
        self.assertTrue(lp2.active and app2.cpuplace.lid_capped)
        self.assertEqual([self.v(n) for n in (0, 8, "keypad")], ["624000", "845000", "1000"])
        self.assertEqual(self.saved_file()["keypad"], {str(self.sysfs / KEYPAD): "30"})   # not the lowered 1000
        asyncio.run(lp2.leave("lid opened"))
        self.assertEqual([self.v(n) for n in (0, 4, 8, "keypad")], ["1118000", "1352000", "1495000", "30"])
        # the lid opened (or nobody knows) while hostd was down: restored at its start
        for lid in (True, None):
            app, lp = self.make()
            asyncio.run(lp.enter("lid closed"))
            app3, lp3 = self.make()
            lp3.start(lid_open=lid)
            self.assertFalse(lp3.active)
            self.assertEqual([self.v(n) for n in (0, 8, "keypad")], ["1118000", "1495000", "30"])
            self.assertIsNone(self.saved_file())
        _, lp4 = self.make()
        lp4.start(lid_open=False)                                  # nothing saved: nothing to do
        self.assertFalse(lp4.active)

    def test_policy_and_still(self):
        _, lp = self.make()
        self.assertFalse(asyncio.run(lp.enter("lid closed", still=lambda: False)))   # the lid opened meanwhile
        self.assertEqual(self.v(8), "1495000")
        pc = hostplatform.Platform()
        _, lp = self.make(platform=pc)
        self.assertFalse(lp.enabled)
        self.assertFalse(asyncio.run(lp.enter("lid closed")))
        self.assertEqual(self.v(0), "1118000")
        _, lp = self.make(platform=pc, lid_low_power="on")         # any board on request: no keypad there
        self.assertTrue(asyncio.run(lp.enter("lid closed")))
        self.assertEqual((self.v(0), self.v("keypad")), ("624000", "30"))
        asyncio.run(lp.leave("lid opened"))
        _, lp = self.make(lid_low_power="off")
        self.assertFalse(lp.enabled)
        _, lp = self.make(lid_keypad_poll_ms=0)                    # the keypad left alone
        asyncio.run(lp.enter("lid closed"))
        self.assertEqual((self.v(0), self.v("keypad")), ("624000", "30"))
        asyncio.run(lp.leave("lid opened"))


class GeminiLowPower(LidBase):
    """The Gemini PDA end to end: the lid closes, Mac OS sleeps, the clocks and the keyboard
    poll go down and stay down through cpuplace's polls; the lid opens: the values come
    back first, then the A72 plan, then the wake."""
    def prepare_root(self):
        gemini_tree(self.root)
        gemini_sysfs(self.root / "sys")

    def before_start(self):
        super().before_start()
        self.aff = Affinity(self.root / "sys")
        cp = self.app.cpuplace
        cp.setaffinity, cp.getaffinity = self.aff.set, self.aff.get
        cp.proc = Path("/proc")                            # the fake QEMU's real threads
        cp.self_pid = -1

    cfg_overrides = dict(LidBase.cfg_overrides, cpu_poll_s=0.5)

    def v(self, n, name="scaling_max_freq"):
        return sysval(self.root / "sys", n, name)

    async def test_lid_low_power(self):
        self.assertTrue(self.app.platform.is_gemini and self.app.lidpower.enabled)
        await self.ready()
        await helpers.wait_for(lambda: self.v(8) == "2587000", 15, what="A72 cap lifted")
        await self.lid(False)
        await self.until_event("lid_sleep_done", timeout=20)
        await self.until_event("lid_low_power")
        self.assertEqual([self.v(n) for n in (0, 4, 8, "keypad")], ["624000", "650000", "845000", "1000"])
        st = (await self.app.status())["power"]["lid_sleep"]
        self.assertTrue(st["low_power"]["active"])
        await asyncio.sleep(1.5)                                   # cpuplace polls (0.5 s)
        self.assertEqual(self.v(8), "845000")
        await self.lid(True)
        await self.until_event("lid_wake")
        names = [r["event"] for r in self.app.lifecycle.recent]
        off = max(i for i, r in enumerate(self.app.lifecycle.recent)
                  if r["event"] == "lid_low_power" and r.get("on") is False)
        self.assertLess(off, names.index("cpu_placement_wake"))
        self.assertLess(names.index("cpu_placement_wake"), names.index("guest_wake_request"))
        await helpers.wait_for(lambda: ("internal", True) in self.displays(), 5, what="panel on")
        self.assertEqual([self.v(n) for n in (0, 4, 8, "keypad")], ["1118000", "1352000", "2587000", "30"])
        self.assertEqual(self.v(8, "scaling_governor"), "schedutil")
        self.assertFalse((self.app.paths.run / "lidpower.json").exists())


class Policy(unittest.TestCase):
    def test_lid_action(self):
        gem = hostplatform.Platform(family="gemini", features=hostplatform.gemini_features())
        pc = hostplatform.Platform()

        def action(platform, **cfg):
            c = Config()
            c.update(cfg)
            return LidSleep(SimpleNamespace(cfg=c, platform=platform)).action
        self.assertEqual(action(gem), "sleep")
        self.assertEqual(action(pc), "blank")
        self.assertEqual(action(gem, lid_action="blank"), "blank")
        self.assertEqual(action(pc, lid_action="sleep"), "sleep")
        self.assertEqual(action(gem, lid_action="hibernate"), "blank")

    def test_lid_state_from_the_input_device(self):
        """The Gemini has no ACPI lid: its Hall sensor's SW_LID through EVIOCGSW at start."""
        import fcntl
        from unittest import mock
        from retroboot import laptop
        td, root = helpers.scratch()
        self.addCleanup(td.cleanup)
        self.assertIsNone(laptop._lid_evdev(root))
        for n, sw in ((2, "0"), (5, "1")):                         # event5: the lid switch
            cap = root / f"sys/class/input/event{n}/device/capabilities/sw"
            cap.parent.mkdir(parents=True)
            cap.write_text(sw + "\n")
            (root / "dev/input").mkdir(parents=True, exist_ok=True)
            (root / f"dev/input/event{n}").write_text("")
        seen = []

        def ioctl(fd, req, buf, state):
            seen.append((os.readlink(f"/proc/self/fd/{fd}"), req))
            buf[0] = state
            return 0
        for state, want in ((1, False), (0, True)):
            with mock.patch.object(fcntl, "ioctl", lambda fd, req, buf, s=state: ioctl(fd, req, buf, s)):
                self.assertIs(laptop._lid_evdev(root), want)
        self.assertTrue(all(path.endswith("dev/input/event5") and req == 0x8008451B for path, req in seen))


if __name__ == "__main__":
    unittest.main()
