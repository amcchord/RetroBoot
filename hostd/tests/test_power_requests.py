"""Shut Down / Restart at any moment (hostd/reports/powerdlg-2026-10-12.md).

Real hostd, guest-launcher and QMP against fake_qemu, which models what the lab measured
on Mac OS 9.2.2: for a few seconds after its Finder appears Mac OS is busy (its TCG thread
spins) and a power key waits in the event queue, its dialog appearing only when Mac OS
goes idle; a Return/R sent before the dialog is drawn goes to the Finder; the Finder may
draw desktop icons or change the screen size meanwhile; some keys are dropped.
"""
import asyncio
import json

import helpers
from test_lifecycle import Base


def keys_of(pkg, launch=1):
    rows = [r for r in helpers.fake_qemu_log(pkg) if "keys" in r]
    return rows[launch - 1]["keys"] if len(rows) >= launch else None


class EarlyRestartWhileFinderBusy(Base):
    """A Restart right as the Finder appears, while the Finder draws its desktop icons
    (2 % of the middle of the screen: before, taken for the dialog, so R went to the
    Finder and the request failed after the dialog came up).  The key goes at once and
    waits in Mac OS; R only once the dialog is really there."""

    def extra_conf(self):
        # hostd's Finder probe runs every 2 s: the icons come after its key, the dialog later
        return {"finder_busy_s": 6.0, "icons_s": 3.5}

    async def test_one_key_at_once_and_r_only_on_the_dialog(self):
        await self.until_event("finder_detected")
        job = await self.job(self.app.lifecycle_job("restart", origin="test"), 60)
        self.assertEqual(job.state, "succeeded", job.public())
        self.assertEqual(keys_of(self.pkg)[:2], ["power", "r"])
        ready = self.ev("power_ready")[0]
        self.assertEqual(ready["waited_s"], 0.0)
        self.assertIs(ready["idle"], False)                 # Mac OS was busy: the key waited in it
        detected = self.ev("power_dialog_detected")
        self.assertEqual(len(detected), 1)
        self.assertGreater(detected[0]["after_s"], 1.0)
        self.assertFalse(self.ev("power_key_ignored"))
        await self.running(2)


class IdleFirstGate(Base):
    """power_idle_first: the first key only once Mac OS is idle (the slower variant).

    Mac OS stays busy after the Finder until the test lets it go idle (fake_qemu's
    busy_until), so the request always finds it busy.  With a fixed busy time, a request
    made one of hostd's 2 s Finder probes later (a loaded host) found it already idle and
    waited 0 s."""
    cfg_overrides = {"power_idle_first": True}

    def extra_conf(self):
        return {"finder_busy_s": 3.0, "icons_s": 0.8, "busy_until": str(self.pkg / "mac-os-idle")}

    async def test_waits_for_idle_then_one_key(self):
        await self.until_event("finder_detected")
        job = self.app.lifecycle_job("restart", origin="test")
        # Busy: hostd waits (and says so after a second), and no key goes meanwhile.
        await helpers.wait_for(lambda: job.message == "Waiting for Mac OS to be ready…", 30, what="waiting")
        self.assertFalse(self.ev("power_ready"))
        self.assertFalse(self.ev("power_key_sent"))
        (self.pkg / "mac-os-idle").touch()                  # Mac OS goes idle
        await self.job(job, 60)
        self.assertEqual(job.state, "succeeded", job.public())
        self.assertEqual(keys_of(self.pkg)[:2], ["power", "r"])
        ready = self.ev("power_ready")[0]
        self.assertTrue(ready["idle"])
        self.assertGreaterEqual(ready["waited_s"], 1.0)


class EarlyShutDownAcrossAScreenSizeChange(Base):
    """RetroBoot Display (ATI card) changes Mac OS's screen size around the Finder: a size
    change is not the dialog."""

    def extra_conf(self):
        return {"finder_busy_s": 5.0, "switch_s": 3.5, "switch_size": "800x600"}

    async def test_shut_down_with_one_key(self):
        await self.until_event("finder_detected")
        job = await self.job(self.app.lifecycle_job("shutdown", poweroff=True, origin="test"), 60)
        self.assertEqual(job.state, "succeeded", job.public())
        self.assertEqual(keys_of(self.pkg), ["power", "ret"])
        await self.until_event("poweroff")


class DroppedKeyIsPressedAgain(Base):
    """Mac OS dropped the key (idle, nothing on screen): one more key, then the dialog."""

    def extra_conf(self):
        return {"drop_power_s": 2.5}

    async def test_second_key_after_an_idle_mac_os_ignored_the_first(self):
        await self.until_event("finder_detected")
        job = await self.job(self.app.lifecycle_job("shutdown", poweroff=False, origin="test"), 60)
        self.assertEqual(job.state, "succeeded", job.public())
        self.assertEqual(keys_of(self.pkg), ["power-dropped", "power", "ret"])
        self.assertEqual(len(self.ev("power_key_ignored")), 1)
        self.assertEqual([e["attempt"] for e in self.ev("power_key_sent")], [1, 2])


class ShutDownWhileTheScreenSaverRuns(Base):
    """After Dark's Flying Toasters (Deluxe, SuperDeluxe, after 30 idle minutes) keep Mac OS
    busy, cover the Shut Down dialog and are not ended by the power key: Restart to Update
    failed after a minute on the X1 Carbon and the Z13.  A one-pixel pointer movement ends
    the saver first, then one key opens the dialog on the desktop."""

    def extra_conf(self):
        return {"saver_s": 4.0}      # after hostd's Finder probe (every 2 s) has seen the Finder

    async def test_pointer_ends_the_saver_then_one_key(self):
        await self.until_event("finder_detected")
        await asyncio.sleep(4.5)                            # the toasters are flying
        job = await self.job(self.app.lifecycle_job("shutdown", poweroff=False, origin="test"), 60)
        self.assertEqual(job.state, "succeeded", job.public())
        self.assertEqual(keys_of(self.pkg), ["power", "ret"])
        woken = self.ev("screen_woken")
        self.assertEqual(len(woken), 1)
        self.assertTrue(woken[0]["changed"])                # the saver gave way to the desktop
        self.assertTrue(woken[0]["menu_bar"])
        launch = [r for r in helpers.fake_qemu_log(self.pkg) if "keys" in r][0]
        self.assertGreaterEqual(launch["pointer"], 1)          # QMP relative events reached the guest
        self.assertEqual(len(self.ev("power_key_sent")), 1)


class ScreenSaverWithoutThePointerNudge(Base):
    """power_wake_nudge off is the old behaviour: the key's dialog stays under the saver and
    the request fails as "busy"."""
    cfg_overrides = {"power_wake_nudge": False, "power_request_budget_s": 6.0}

    def extra_conf(self):
        return {"saver_s": 4.0}

    async def test_the_request_fails(self):
        await self.until_event("finder_detected")
        await asyncio.sleep(4.5)
        job = await self.job(self.app.lifecycle_job("shutdown", poweroff=False, origin="test"), 60)
        self.assertEqual(job.state, "failed", job.public())
        self.assertIn("stayed busy", json.dumps(job.public()))
        self.assertFalse(self.ev("screen_woken"))
        self.assertFalse(self.ev("power_dialog_detected"))


class NoScreenSaverNoNudge(Base):
    """With the menu bar on screen nothing changes: no pointer movement, the key at once."""

    async def test_no_nudge(self):
        await self.until_event("finder_detected")
        job = await self.job(self.app.lifecycle_job("shutdown", poweroff=False, origin="test"), 60)
        self.assertEqual(job.state, "succeeded", job.public())
        self.assertFalse(self.ev("screen_woken"))
        launch = [r for r in helpers.fake_qemu_log(self.pkg) if "keys" in r][0]
        self.assertEqual(launch["pointer"], 0)
        self.assertEqual(keys_of(self.pkg), ["power", "ret"])


class SlowDialogOnABusyMacGetsOneKey(Base):
    """A busy Mac OS draws the dialog late: never a second key meanwhile (it would close it)."""
    modes = ["slowdialog:7"]

    async def test_one_key(self):
        await self.until_event("finder_detected")
        job = await self.job(self.app.lifecycle_job("restart", origin="test"), 60)
        self.assertEqual(job.state, "succeeded", job.public())
        self.assertEqual(keys_of(self.pkg)[:2], ["power", "r"])
        self.assertFalse(self.ev("power_key_ignored"))


class RequestDuringTheExtensionParade(Base):
    """Asked while Mac OS is still starting: queued, carried out once it can take it."""

    def extra_conf(self):
        return {"boot_s": 0.4, "finder_s": 4.0, "finder_busy_s": 2.0}

    async def test_restart_requested_during_startup(self):
        await self.until_event("guest_content")
        job = self.app.lifecycle_job("restart", origin="test")
        await helpers.wait_for(lambda: job.message == "Waiting for Mac OS to finish starting…", 5, what="message")
        await self.job(job, 60)
        self.assertEqual(job.state, "succeeded", job.public())
        self.assertEqual(keys_of(self.pkg)[:2], ["power", "r"])
        sent = self.ev("power_key_sent")[0]
        self.assertIsNotNone(sent["since_finder_s"])       # not before the Finder
        self.assertLess(sent["since_finder_s"], 1.0)

    async def test_host_power_off_requested_during_startup(self):
        await self.until_event("guest_content")
        job = await self.job(self.app.host_job("poweroff", origin="test"), 60)
        self.assertEqual(job.state, "succeeded", job.public())
        self.assertEqual(keys_of(self.pkg), ["power", "ret"])
        await self.until_event("poweroff")


class PowerButtonWhileStarting(Base):
    """The host power button during startup: Mac OS's own dialog once it can show it."""

    def extra_conf(self):
        return {"boot_s": 0.4, "finder_s": 3.0, "finder_busy_s": 1.5}

    async def test_button_is_queued_until_mac_os_takes_the_key(self):
        await self.until_event("guest_content")
        self.fp.emit({"event": "power_button"})
        await self.until_event("power_button_queued")
        await self.until_event("power_dialog_detected", timeout=30)
        self.assertFalse(self.ev("power_dialog_confirmed"))      # the user answers it
        self.assertEqual(self.app.supervisor.state, "running")
        self.fp.emit({"event": "power_button"})                   # a second press leaves it up
        await self.until_event("power_button_ignored")
        job = await self.job(self.app.lifecycle_job("shutdown", poweroff=False, origin="test"), 60)
        self.assertEqual(job.state, "succeeded", job.public())
        self.assertTrue(self.ev("power_key_reused"))
        self.assertEqual(len(self.ev("power_key_sent")), 1)


class ShutDownBeforeMacOSStarts(Base):
    """Shut Down while the launch hooks run (the first launch's disk preparation): they
    finish, and Mac OS is not started at all."""

    def before_start(self):
        self.prep = helpers.install_fake_prep(self.app, helpers.FakePrepModule(delay=2.5))

    async def test_shut_down_during_preparation(self):
        await helpers.wait_for(lambda: self.app.supervisor.launch_pending and self.prep.calls, 10, what="prep")
        job = await self.job(self.app.lifecycle_job("shutdown", poweroff=False, origin="test"), 30)
        self.assertEqual(job.state, "succeeded", job.public())
        self.assertTrue(job.result["start_cancelled"])
        self.assertTrue(self.ev("guest_start_cancelled"))
        self.assertFalse(self.ev("guest_start"))
        self.assertEqual(helpers.fake_qemu_log(self.pkg), [])
        await self.until_doc("stopped")
        self.assertEqual(self.app.machines.get("mac-os-9").meta["prep"]["result"], "applied")
        # Start Mac OS afterwards: it starts normally.
        job = await self.job(self.app.lifecycle_job("start", origin="test"), 30)
        self.assertEqual(job.state, "succeeded", job.public())

    async def test_power_off_during_preparation(self):
        await helpers.wait_for(lambda: self.app.supervisor.launch_pending and self.prep.calls, 10, what="prep")
        job = await self.job(self.app.host_job("poweroff", origin="test"), 30)
        self.assertEqual(job.state, "succeeded", job.public())
        await self.until_event("poweroff")
        self.assertFalse(self.ev("guest_start"))
        lines = [json.loads(l) for l in self.app.paths.lifecycle_log.read_text().splitlines()]
        order = [l["event"] for l in lines if l["event"] in ("guest_prep", "guest_start_cancelled", "poweroff")]
        self.assertEqual(order, ["guest_prep", "guest_start_cancelled", "poweroff"])


class RestartBeforeMacOSStarts(Base):
    """Restart while the launch hooks run: Mac OS simply starts."""

    def before_start(self):
        self.prep = helpers.install_fake_prep(self.app, helpers.FakePrepModule(delay=1.5))

    async def test_restart_during_preparation_starts_mac_os(self):
        await helpers.wait_for(lambda: self.app.supervisor.launch_pending and self.prep.calls, 10, what="prep")
        job = await self.job(self.app.lifecycle_job("restart", origin="test"), 30)
        self.assertEqual(job.state, "succeeded", job.public())
        self.assertEqual(len(self.ev("guest_start")), 1)
        await asyncio.sleep(0)


class AnotherAlertIsUp(Base):
    """An application's alert is on screen: the key waits behind it, so it is pressed only
    once and the request says what is in the way."""

    def extra_conf(self):
        return {"alert_s": 0.0}

    async def test_one_key_and_an_honest_failure(self):
        await self.until_event("finder_detected")
        job = await self.job(self.app.lifecycle_job("shutdown", poweroff=False, origin="test"), 60)
        self.assertEqual(job.state, "failed")
        self.assertEqual(job.error["code"], "guest_alert", job.public())
        self.assertTrue(job.result["force_available"])
        self.assertEqual(len(self.ev("power_key_sent")), 1)
        self.assertTrue(self.ev("power_dialog_blocked"))
        self.assertEqual(self.app.supervisor.state, "running")


class MacOSShutsDownByItselfMeanwhile(Base):
    """Mac OS shuts down on its own (the user chose Special > Shut Down) while hostd waits for
    it to be idle (power_idle_first): the request is fulfilled, without a power key."""
    modes = ["selfshutdown:3.5"]
    cfg_overrides = {"power_idle_first": True}

    def extra_conf(self):
        return {"finder_busy_s": 10.0}

    async def test_request_ends_with_the_shutdown(self):
        await self.until_event("finder_detected")
        job = await self.job(self.app.lifecycle_job("shutdown", poweroff=False, origin="test"), 40)
        self.assertEqual(job.state, "succeeded", job.public())
        self.assertEqual(job.result["exit"]["reason"], "guest-shutdown")
        self.assertFalse(self.ev("power_key_sent"))


class MacOSShutsDownByItselfWhileTheKeyWaits(Base):
    """The same while the first key waits in a busy Mac OS: the request ends with that shutdown."""
    modes = ["selfshutdown:3.5"]

    def extra_conf(self):
        return {"finder_busy_s": 10.0}

    async def test_request_ends_with_the_shutdown(self):
        await self.until_event("finder_detected")
        job = await self.job(self.app.lifecycle_job("shutdown", poweroff=False, origin="test"), 40)
        self.assertEqual(job.state, "succeeded", job.public())
        self.assertEqual(job.result["exit"]["reason"], "guest-shutdown")
        self.assertEqual(len(self.ev("power_key_sent")), 1)
        self.assertFalse(self.ev("power_dialog_confirmed"))


class AnotherAppearanceTheme(Base):
    """Another Appearance theme (Aqua Theme Beta's dialog: light frame, white body): not
    recognized by its Platinum look, but a box that appeared with the menus changing state,
    unchanged for power_fallback_s while Mac OS idles, is the dialog; one key."""

    def extra_conf(self):
        return {"theme": "aqua", "finder_busy_s": 3.0}

    async def test_restart_and_shut_down_with_one_key_each(self):
        await self.until_event("finder_detected")
        job = await self.job(self.app.lifecycle_job("restart", origin="test"), 60)
        self.assertEqual(job.state, "succeeded", job.public())
        self.assertEqual(keys_of(self.pkg)[:2], ["power", "r"])
        self.assertEqual(self.ev("power_dialog_detected")[0]["via"], "modal")
        self.assertFalse(self.ev("power_key_ignored"))
        await self.running(2)
        await self.until_event("finder_detected", 2)
        job = await self.job(self.app.lifecycle_job("shutdown", poweroff=False, origin="test"), 60)
        self.assertEqual(job.state, "succeeded", job.public())
        self.assertEqual(keys_of(self.pkg, 2), ["power", "ret"])


class FinderInAnotherTheme(Base):
    """A theme whose menu bar is not Platinum's (Gamma: purple, light titles): the Finder is
    recognized by its menu-bar layout (finder.py), so a request goes through at the Finder
    without the started-without-Finder rule (before finderdet: 20 s + 5 s idle)."""
    cfg_overrides = {"power_started_after_s": 30.0, "power_started_idle_s": 5.0}

    def extra_conf(self):
        return {"bar": "gamma", "theme": "aqua"}

    async def test_finder_by_layout_and_shut_down(self):
        await self.until_event("finder_detected")
        self.assertEqual(self.ev("finder_detected")[0]["via"], "layout")
        self.assertTrue(self.app.supervisor.mac_os_up)
        job = await self.job(self.app.lifecycle_job("shutdown", poweroff=False, origin="test"), 60)
        self.assertEqual(job.state, "succeeded", job.public())
        self.assertFalse(self.ev("mac_os_started"))
        self.assertEqual(keys_of(self.pkg), ["power", "ret"])


class FinderMenuBarNotRecognized(Base):
    """A menu bar finder.py does not recognize (no Finder layout): the last resort, Mac OS
    counts as started once it idled after its content (mac_os_started, logged at startup
    whether or not a request waits), and the request goes through (before powerdlg, it
    waited for the Finder until finder_wait_timeout and failed)."""
    cfg_overrides = {"power_started_after_s": 2.0, "power_started_idle_s": 1.0}

    def extra_conf(self):
        return {"bar": "unknown", "theme": "aqua"}

    async def test_started_without_a_recognized_finder(self):
        await self.until_event("mac_os_started")
        sup = self.app.supervisor
        self.assertTrue(sup.mac_os_up)
        self.assertFalse(sup.finder_seen)
        self.assertIn("mac_os_started", [e["event"] for e in sup.timeline()])
        job = await self.job(self.app.lifecycle_job("shutdown", poweroff=False, origin="test"), 60)
        self.assertEqual(job.state, "succeeded", job.public())
        self.assertFalse(self.ev("finder_detected"))
        self.assertEqual(len(self.ev("mac_os_started")), 1)
        self.assertEqual(keys_of(self.pkg), ["power", "ret"])

    async def test_a_request_during_startup_waits_for_the_started_rule(self):
        await self.until_event("guest_content")
        job = await self.job(self.app.lifecycle_job("shutdown", poweroff=False, origin="test"), 60)
        self.assertEqual(job.state, "succeeded", job.public())
        self.assertFalse(self.ev("finder_detected"))
        self.assertTrue(self.ev("mac_os_started"))
        self.assertEqual(keys_of(self.pkg), ["power", "ret"])


class ShutDownFinishedAfterTheRequestGaveUp(Base):
    """An application asks to save during a Shut Down that keeps RetroBoot on: the request gives
    up after clean_stop_timeout, then Mac OS finishes the Shut Down (the user answered the
    application).  That is still the Shut Down asked for: the panel says so and the computer
    stays on.  It used to power off, as if the user had chosen Shut Down in Mac OS."""
    cfg_overrides = {"clean_stop_timeout": 1.5}

    def extra_conf(self):
        return {"shutdown_delay_s": 4.0}

    async def test_stays_on(self):
        await self.until_event("finder_detected")
        job = await self.job(self.app.lifecycle_job("shutdown", poweroff=False, origin="test"), 60)
        self.assertEqual(job.state, "failed", job.public())
        self.assertIn("When Mac OS finishes, RetroBoot stays on.", json.dumps(job.public()))
        self.assertEqual(self.ev("late_stop_armed")[0]["then"], "hold")
        await self.until_event("late_stop_finished")
        await self.until_doc("stopped")
        self.assertEqual(self.ev("guest_exited")[0]["reason"], "guest-shutdown")
        self.assertFalse(self.ev("poweroff"))
        self.assertFalse(self.app.powered_off)
        self.assertEqual(len(self.ev("guest_start")), 1)


class RestartRetroBootFinishedLate(Base):
    """VM169, October 24 2026: Mac OS Setup Assistant answered the Shut Down with "Are you sure
    you want to quit?", the restart gave up, and the later Quit powered the computer off.  Now
    Restart RetroBoot goes on when Mac OS finishes."""
    cfg_overrides = {"clean_stop_timeout": 1.5}

    def extra_conf(self):
        return {"shutdown_delay_s": 4.0}

    async def test_restarts_not_powers_off(self):
        await self.until_event("finder_detected")
        job = await self.job(self.app.host_job("reboot", origin="test"), 60)
        self.assertEqual(job.state, "failed", job.public())
        self.assertIn("When Mac OS finishes, RetroBoot restarts.", json.dumps(job.public()))
        self.assertFalse(self.ev("host_reboot"))
        await self.until_event("host_reboot")
        self.assertEqual(self.ev("late_stop_finished")[0]["then"], "resume")
        again = [j for j in self.app.jobs.jobs.values() if j.kind == "host.reboot" and j is not job]
        self.assertEqual(len(again), 1)
        await self.job(again[0])
        self.assertEqual(again[0].state, "succeeded", again[0].public())
        self.assertFalse(self.ev("poweroff"))
        self.assertFalse(self.app.powered_off)


class TurnOffFinishedLate(Base):
    """Turn Off (Shut Down with power off) that gave up: the late Shut Down still turns off."""
    cfg_overrides = {"clean_stop_timeout": 1.5}

    def extra_conf(self):
        return {"shutdown_delay_s": 4.0}

    async def test_turns_off(self):
        await self.until_event("finder_detected")
        job = await self.job(self.app.lifecycle_job("shutdown", poweroff=True, origin="test"), 60)
        self.assertEqual(job.state, "failed", job.public())
        self.assertIn("When Mac OS finishes, RetroBoot turns off.", json.dumps(job.public()))
        await self.until_event("poweroff")
        self.assertEqual(self.ev("late_stop_finished")[0]["then"], "poweroff")


class LateStopClearedByTheNextRequest(Base):
    """A request that gave up long ago (the user cancelled the application's question, so Mac
    OS never shut down) does not decide what the next request's Shut Down means."""

    async def test_next_request_wins(self):
        await self.until_event("finder_detected")
        sup = self.app.supervisor
        from retroboot.util import mono
        sup.late_stop = {"session": sup.session, "mono": mono(), "purpose": "shutdown", "then": "hold",
                         "resume": None}
        job = await self.job(self.app.lifecycle_job("shutdown", poweroff=True, origin="test"))
        self.assertEqual(job.state, "succeeded", job.public())
        await self.until_event("poweroff")
        self.assertFalse(self.ev("late_stop_finished"))
