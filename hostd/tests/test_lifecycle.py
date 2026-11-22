"""Supervisor lifecycle with the fake presenter and fake QEMU (real guest-launcher subprocess).

These run the real hostd, real guest-launcher and real QMP clients against
fakes; only the emulator and the presenter are simulated.
"""
import asyncio
import json
import os
import sys
import unittest
from pathlib import Path

import helpers
from fakes.fake_presenter import FakePresenter
from retroboot import __version__, powerdlg, schema
from retroboot.config import Paths
from retroboot.hostd import Hostd
from test_units import make_archive


def find_item(doc: dict, id: str) -> dict | None:
    """The item with this id anywhere in a panel document (groups, columns, stacks, bands)."""
    for i in doc.get("items") or []:
        if i.get("id") == id:
            return i
        found = find_item(i, id)
        if found:
            return found
    return None


class Base(unittest.IsolatedAsyncioTestCase):
    modes = ["normal"]
    machine = True
    cfg_overrides: dict = {}

    async def asyncSetUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        self.pkg = helpers.make_package(self.root)
        self.prepare_root()
        helpers.fake_qemu_conf(self.pkg, **({"modes": self.modes, "boot_s": 0.4, "finder_s": 0.9} | self.extra_conf()))
        if self.machine:
            helpers.make_machine(self.root)
        self.cfg = helpers.make_config(self.root, **self.cfg_overrides)
        paths = Paths(self.root)
        paths.ensure()
        self.fp = FakePresenter(str(paths.presenter_sock), str(paths.display_sock))
        await self.fp.start()
        self.app = Hostd(self.cfg)
        self.app.settings.store.load()
        if self.machine:
            self.app.settings.set_desired_and_applied(
                {"machine.active": "mac-os-9"},
                schema.ValidationContext(machines={"mac-os-9"}, check_capabilities=False))
        self.prep = helpers.install_fake_prep(self.app)
        self.before_start()
        await self.app.start()

    def extra_conf(self) -> dict:
        return {}

    def prepare_root(self) -> None:
        """Before hostd exists (its platform is detected when it is made): a board's files."""

    def before_start(self) -> None:
        pass

    async def asyncTearDown(self):
        await self.app.shutdown()
        # Nothing of a launch may be left for the test loop's final sweep: a task cancelled
        # there inside create_subprocess_exec can hang the teardown (Hostd._finish).
        self.assertTrue(self.app._boot_task is None or self.app._boot_task.done(), "launch left running")
        await self.fp.stop()

    def ev(self, name):
        return helpers.events(self.app, name)

    async def until_event(self, name, count=1, timeout=15.0):
        return await helpers.wait_for(lambda: len(self.ev(name)) >= count, timeout, what=f"{count}x {name}")

    async def until_doc(self, doc_id, timeout=10.0):
        return await helpers.wait_for(lambda: (self.fp.doc or {}).get("id") == doc_id and
                                      self.fp.scene.get("scene") == "panel", timeout, what=f"doc {doc_id}")

    async def running(self, n=1, timeout=15.0):
        await self.until_event("guest_content", n, timeout)
        await helpers.wait_for(lambda: self.app.supervisor.state == "running", timeout, what="running")

    async def job(self, job, timeout=40.0):
        await asyncio.wait_for(job.done.wait(), timeout)
        return job


class AutostartLifecycle(Base):
    async def test_autostart_finder_clean_restart_and_shutdown(self):
        await self.running()
        sup = self.app.supervisor
        scenes = self.fp.requests_of("set_scene")
        self.assertEqual(scenes[0]["scene"], "splash")
        self.assertIn({"cmd": "set_scene", "scene": "guest", "reveal": "first-content"},
                      [{k: v for k, v in r.items() if k != "id"} for r in scenes])
        content = self.ev("guest_content")[0]
        self.assertEqual(content["via"], "presenter")
        self.assertTrue(self.ev("guest_revealed"))
        spec = json.loads(self.app.paths.argv_json.read_text())
        self.assertIn("-rtc", spec["argv"])
        self.assertEqual(spec["argv"][spec["argv"].index("-rtc") + 1], "base=localtime")
        self.assertEqual(spec["timezone"], "UTC")
        status = json.loads(self.app.paths.status_json.read_text())
        self.assertEqual((status["session"], status["state"]), (sup.session, "running"))
        # Disk prep ran once before the first launch and is recorded in machine.json.
        self.assertEqual([c[0] for c in self.prep.calls], ["apply"])
        self.assertEqual(self.app.machines.get("mac-os-9").meta["prep"]["result"], "applied")
        await self.until_event("finder_detected")
        await self.until_event("known_good_advanced")
        # Host-requested clean restart: ADB power key, dialog verified, R.
        job = await self.job(self.app.lifecycle_job("restart", origin="test"))
        self.assertEqual(job.state, "succeeded", job.public())
        self.assertEqual(job.result["exit"]["reason"], "guest-reset")
        keys = [r for r in helpers.fake_qemu_log(self.pkg) if "keys" in r][0]["keys"]
        self.assertEqual(keys[:2], ["power", "r"])
        self.assertTrue(self.ev("power_dialog_detected"))
        await self.running(2)
        self.assertEqual(len(self.ev("guest_start")), 2)
        self.assertTrue(self.app.machines.get("mac-os-9").state()["clean"] is False)  # running again
        # Host-requested clean shutdown that powers off.
        job = await self.job(self.app.lifecycle_job("shutdown", poweroff=True, origin="test"))
        self.assertEqual(job.state, "succeeded", job.public())
        await self.until_event("poweroff")
        await helpers.wait_for(lambda: self.fp.scene["scene"] == "black" and self.app.powered_off, 5, what="black")
        st = self.app.machines.get("mac-os-9").state()
        self.assertEqual((st["clean"], st["last_exit"]["reason"]), (True, "guest-shutdown"))
        lines = [json.loads(l) for l in self.app.paths.lifecycle_log.read_text().splitlines()]
        exits = [l for l in lines if l["event"] == "guest_exit" and l["source"] == "launcher"]
        self.assertEqual([(e["reason"], e["code"]) for e in exits], [("guest-reset", 10), ("guest-shutdown", 0)])
        for rec in lines:
            self.assertIn("t_mono", rec)
            self.assertIn("t_wall", rec)

    async def test_host_reboot_goes_black_without_the_shut_down_panel(self):
        await self.running()
        await self.until_event("finder_detected")
        job = await self.job(self.app.host_job("reboot", origin="web"))
        self.assertEqual(job.state, "succeeded", job.public())
        self.assertTrue(self.ev("host_reboot"))
        self.assertEqual(self.ev("guest_exited")[-1]["intent"], "shutdown-host")
        shown = [r.get("doc", {}).get("id") for r in self.fp.requests_of("ui_show")]
        self.assertNotIn("stopped", " ".join(str(d) for d in shown))
        self.assertNotIn("panel", self.fp.scenes()[-3:])
        self.assertEqual(self.fp.scene["scene"], "black")

    async def test_duplicate_requests_return_running_job(self):
        await self.running()
        a = self.app.lifecycle_job("restart", origin="web")
        b = self.app.lifecycle_job("restart", origin="panel")
        self.assertIs(a, b)
        await self.job(a)
        self.assertEqual(len([r for r in self.ev("clean_request")]), 1)


class StopDuringLaunch(Base):
    """hostd stops while it starts Mac OS (test_wifi_detect's EarlyRadio did, by chance, on a
    loaded builder): the launch ends inside shutdown(), not in the loop's final sweep."""

    def before_start(self):
        self.in_launch = asyncio.Event()
        real = self.app.runner.start

        async def start(session):
            self.in_launch.set()
            await asyncio.Event().wait()        # a launch that is still under way
            return await real(session)
        self.app.runner.start = start

    async def test_shutdown_ends_the_launch(self):
        await asyncio.wait_for(self.in_launch.wait(), 15)
        self.assertFalse(self.app._boot_task.done())
        # asyncTearDown: shutdown() must leave no launch behind.


class FinishTasks(unittest.IsolatedAsyncioTestCase):
    async def test_a_subprocess_launch_ends(self):
        """The CPython 3.13 case behind the flaky teardown: a task inside
        create_subprocess_exec, between the fork and its pipe-connection task.  Cancelled
        alone (Hostd._finish) it ends; the final sweep then finds nothing of it."""
        async def launch():
            await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(5)",
                                                 stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                                                 stderr=asyncio.subprocess.DEVNULL)
        t = asyncio.get_running_loop().create_task(launch())
        await asyncio.sleep(0)          # forked; its _connect_pipes task is scheduled
        await asyncio.wait_for(Hostd._finish([t]), 15)
        self.assertTrue(t.cancelled())
        self.assertEqual([x for x in asyncio.all_tasks() if x is not asyncio.current_task()], [])


class StartOrder(Base):
    """hostboot: everything a launch needs is ready before QEMU starts; the web server,
    Modern Web Helper and mDNS start only after the first launch."""

    async def test_launch_waits_for_launch_ready_and_services_follow_the_launch(self):
        await self.running()
        await self.until_event("hostd_services_started")
        order = [r["event"] for r in self.app.lifecycle.recent]
        self.assertLess(order.index("presenter_connected"), order.index("guest_start"))
        self.assertLess(order.index("hostd_launch_ready"), order.index("guest_start"))
        self.assertLess(order.index("airport_bridge_listening") if "airport_bridge_listening" in order
                        else order.index("hostd_launch_ready"), order.index("guest_start"))
        self.assertLess(order.index("guest_start"), order.index("hostd_services_started"))
        self.assertEqual(self.ev("hostd_services_after_wait"), [])
        self.assertIsNotNone(self.app.web.runner)   # the web server did start


class LaunchReadyGate(unittest.IsolatedAsyncioTestCase):
    async def test_boot_waits_for_launch_ready(self):
        # The presenter can report its display before start() has everything a
        # launch needs (AirPort and battery sockets, audio warm-up, guest isolation).
        td, root = helpers.scratch()
        self.addCleanup(td.cleanup)
        app = Hostd(helpers.make_config(root))
        app.settings.store.load()
        app.loop = asyncio.get_running_loop()
        called = []

        async def boot():
            called.append(True)
        app.supervisor.boot = boot
        app._start_boot()
        await asyncio.sleep(0.3)
        self.assertEqual(called, [])
        app._launch_ready.set()
        await asyncio.wait_for(app._boot_task, 5)
        self.assertEqual(called, [True])


class GuestInitiated(Base):
    modes = ["selfreset:1.2", "selfshutdown:1.2"]

    async def test_mac_os_restart_then_shut_down(self):
        await self.until_event("guest_exited", 1)
        self.assertEqual(self.ev("guest_exited")[0]["reason"], "guest-reset")
        await self.running(2)
        await self.until_event("poweroff", 1, 20)
        self.assertEqual(self.ev("guest_exited")[1]["reason"], "guest-shutdown")
        await helpers.wait_for(lambda: self.fp.scene["scene"] == "black", 5, what="black")


class StartupMacChangedWhileRunning(Base):
    """Startup Disk's Use This Mac while Mac OS runs: the change waits for Mac OS to
    restart ("The new Mac starts the next time Mac OS restarts") and Mac OS's own
    Restart starts the new Mac."""
    modes = ["selfreset:2.5", "normal"]

    def before_start(self) -> None:
        helpers.make_machine(self.root, "mac-os-9-b", "Mac OS 9 B")

    async def test_use_this_mac_starts_at_the_next_mac_os_restart(self):
        await self.running()
        s = self.app.settings
        job = await self.job(self.app.select_machine_job("mac-os-9-b", origin="test"))
        self.assertEqual(job.state, "succeeded", job.public())
        self.assertEqual(job.result["pending_restart"], ["machine.active"])
        self.assertEqual(s.applied["machine.active"], "mac-os-9")
        await self.until_event("guest_exited", 1)
        self.assertEqual(self.ev("guest_exited")[0]["reason"], "guest-reset")
        await self.running(2)
        self.assertEqual(s.applied["machine.active"], "mac-os-9-b")
        self.assertEqual(s.pending_keys(), [])
        argv = json.loads(self.app.paths.argv_json.read_text())["argv"]
        self.assertTrue(any("mac-os-9-b" in a for a in argv), argv)


class ForceAndRepair(Base):
    async def test_force_stop_is_labeled_logged_and_repaired_next_launch(self):
        await self.running()
        job = await self.job(self.app.lifecycle_job("force-stop", poweroff=False, origin="web"))
        self.assertEqual(job.state, "succeeded", job.public())
        eid = job.result["evidence"]
        fr = self.ev("force_request")[0]
        self.assertEqual((fr["evidence"], fr["warning"]), (eid, "unsaved changes in Mac OS may be lost"))
        self.assertTrue((self.app.paths.evidence / eid / "summary.json").is_file())
        self.assertTrue((self.app.paths.evidence / eid / "screen-guest.png").is_file())
        await self.until_doc("stopped")
        st = self.app.machines.get("mac-os-9").state()
        self.assertFalse(st["clean"])
        self.assertEqual(st["last_exit"]["reason"], "host-signal")
        # Next launch repairs the dirty disk before QEMU starts.
        self.fp.click("start")
        await self.running(2)
        rep = self.ev("disk_repair")
        self.assertEqual(len(rep), 1)
        self.assertEqual(rep[0]["result"], "ok")
        self.assertIn(("fsck", str(self.app.machines.get("mac-os-9").disk), True), self.prep.calls)
        self.assertLess(rep[0]["t_mono"], self.ev("guest_start")[1]["t_mono"])
        # Force restart relaunches.
        job = await self.job(self.app.lifecycle_job("force-restart", origin="panel"))
        self.assertEqual(job.state, "succeeded", job.public())
        await self.running(3)


class CrashRetry(Base):
    modes = ["crash", "crash", "normal"]

    async def test_crash_retries_once_then_classic_alert(self):
        await self.until_doc("failure", 20)
        self.assertEqual(len(self.ev("guest_retry")), 1)
        alert = self.fp.doc
        self.assertEqual(alert["kind"], "alert")
        texts = json.dumps(alert)
        eid = self.ev("failure_alert")[0]["evidence"]
        self.assertIn(eid, texts)
        self.assertNotIn("Traceback", texts)
        self.fp.click("restart")
        await self.running(1, 20)


class NoContent(Base):
    modes = ["nocontent", "normal"]
    cfg_overrides = {"guest_content_timeout": 2.5}

    async def test_missing_content_retries(self):
        await self.until_event("guest_content_timeout", 1, 10)
        await self.running(1, 20)
        self.assertEqual(len(self.ev("guest_retry")), 1)
        self.assertEqual(len(self.ev("guest_start")), 2)
        # A hang before the gray that never wrote the disk keeps the machine clean:
        # no repair before the retry (pre-gray hang, pregray-hang-20261011.md).
        self.assertEqual(len(self.ev("disk_untouched")), 1)
        self.assertEqual(self.ev("disk_untouched")[0]["clean"], True)
        self.assertEqual(self.ev("disk_repair"), [])
        self.assertEqual(self.ev("guest_content_timeout")[0]["deadline_s"], 2.5)


class NoContentAfterWrite(Base):
    modes = ["nocontent-write", "normal"]
    cfg_overrides = {"guest_content_timeout": 2.5}

    async def test_written_disk_is_repaired_before_retry(self):
        await self.until_event("guest_content_timeout", 1, 10)
        await self.running(1, 20)
        self.assertEqual(self.ev("disk_untouched"), [])
        rep = self.ev("disk_repair")
        self.assertEqual(len(rep), 1)
        self.assertLess(rep[0]["t_mono"], self.ev("guest_start")[1]["t_mono"])


class LearnedContentDeadline(Base):
    # cap 60 s, floor 0.5 s: the deadline follows this machine's own time to the gray
    cfg_overrides = {"guest_content_timeout": 60.0, "guest_content_floor": 0.5}

    async def test_deadline_learns_from_first_content(self):
        await self.running()
        self.assertEqual(self.ev("guest_start")[0]["content_deadline_s"], 60.0)   # no history: the cap
        st = self.app.machines.get("mac-os-9").state()
        self.assertEqual(len(st["content_s"]), 1)
        t = st["content_s"][0]
        job = await self.job(self.app.lifecycle_job("restart", origin="web"))
        self.assertEqual(job.state, "succeeded", job.public())
        await self.running(2)
        self.assertAlmostEqual(self.ev("guest_start")[1]["content_deadline_s"], max(0.5, round(3 * t, 1)), places=1)
        self.assertEqual(len(self.app.machines.get("mac-os-9").state()["content_s"]), 2)


class BootOptions(Base):
    def extra_conf(self):
        return {"boot_s": 3.0, "finder_s": 3.5}

    async def test_escape_pauses_guest_and_resumes(self):
        await self.until_event("qmp_connected")
        self.fp.emit({"event": "preboot_key", "key": "escape"})
        await self.until_doc("boot-options")
        self.assertTrue(self.app.supervisor.paused)
        status = await self.app.supervisor.qmp.query_status()
        self.assertEqual(status["status"], "paused")
        self.fp.click("recovery")
        await self.until_doc("recovery-main")
        self.assertEqual(self.fp.scene["background"], "checkerboard")
        self.fp.click("cancel")  # Return to Mac OS resumes
        await self.running(1, 15)
        self.assertEqual(len(self.ev("guest_start")), 1)  # never killed
        self.assertTrue(self.ev("guest_resumed"))


class RecoveryAndPower(Base):
    async def test_chord_and_power_button(self):
        await self.running()
        await self.until_event("finder_detected")
        self.fp.emit({"event": "chord", "name": "recovery"})
        await self.until_doc("recovery-main")
        self.assertEqual(self.fp.scene["background"], "guest-dimmed")
        self.fp.click("diagnostics")
        await self.until_doc("diagnostics")
        doc = json.dumps(self.fp.doc, ensure_ascii=False)
        self.assertIn(f"RetroBoot {__version__}", doc)
        self.assertIn("Display: 1024 × 768 at 60 Hz, scale 1×", doc)
        self.fp.click("cancel")
        await self.until_doc("recovery-main")
        self.fp.click("network")
        await self.until_doc("network-web")
        self.fp.click("wifi")
        await self.until_doc("wifi")
        await helpers.wait_for(lambda: "No Wi-Fi hardware" in json.dumps(self.fp.doc), 5, what="no wifi")
        self.fp.click("cancel")
        await self.until_doc("network-web")
        self.fp.click("cancel")
        await self.until_doc("recovery-main")
        self.fp.click("cancel")
        await helpers.wait_for(lambda: self.fp.scene == {"scene": "guest", "reveal": "now"}, 5, what="guest scene")
        self.fp.emit({"event": "power_button"})
        await self.until_event("power_button")
        await self.until_event("power_dialog_detected")
        frame = await self.app.supervisor.dump_frame()
        self.assertTrue(powerdlg.find_power_dialog(None, frame)["found"])  # Mac OS's dialog is up; the user decides
        self.assertEqual(self.app.supervisor.state, "running")
        self.assertFalse(self.ev("power_dialog_confirmed"))

    async def test_main_display_choice_on_a_laptop(self):
        """Display & Sound offers "Main display" on a laptop (internal panel), enabled
        only with an external display connected; choosing it reaches the presenter live."""
        laptop = dict(self.fp.display, connector="eDP-1", outputs=[
            {"connector": "eDP-1", "role": "primary", "internal": True, "mode": [2160, 1440, 60]}])
        self.fp.display = laptop
        self.fp.emit({"event": "display", **laptop})
        await self.running()
        await helpers.wait_for(lambda: self.app.capabilities()["internal_display"], 5, what="internal display")

        def popup():
            return find_item(self.fp.doc, "display.primary")
        self.fp.emit({"event": "chord", "name": "recovery"})
        await self.until_doc("recovery-main")
        self.fp.click("display")
        await self.until_doc("display-sound")
        self.assertTrue(popup()["disabled"])
        self.assertIn("Connect an external display", json.dumps(self.fp.doc))
        self.fp.click("cancel")
        await self.until_doc("recovery-main")
        with_hdmi = dict(laptop, outputs=laptop["outputs"] + [
            {"connector": "HDMI-A-2", "role": "mirror", "internal": False, "mode": [1920, 1080, 60]}])
        self.fp.display = with_hdmi
        self.fp.emit({"event": "display", **with_hdmi})
        await helpers.wait_for(lambda: len((self.app.presenter.display or {}).get("outputs") or []) == 2, 5,
                               what="external output")
        self.fp.click("display")
        await self.until_doc("display-sound")
        self.assertFalse(popup().get("disabled"))
        self.assertEqual([o["label"] for o in popup()["options"]],
                         ["Built-in display", "External display when connected"])
        self.fp.click("apply", {"display.primary": "external"})
        await helpers.wait_for(lambda: self.app.settings.applied["display.primary"] == "external", 10,
                               what="applied")
        await helpers.wait_for(lambda: (self.fp.requests_of("display_config") or [{}])[-1].get("primary") ==
                               "external", 5, what="presenter told")

    async def test_3d_acceleration_checkbox(self):
        """Display & Sound shows "3D acceleration (ATI Radeon)" disabled with the reason
        until this computer, the emulator and the Mac's disk can use it; then ticking it
        is a restart-class change and the panel names the Mac screen it gives."""
        from retroboot import graphics

        def box():
            return find_item(self.fp.doc, "display.graphics")
        await self.running()
        self.fp.emit({"event": "chord", "name": "recovery"})
        await self.until_doc("recovery-main")
        self.fp.click("display")
        await self.until_doc("display-sound")
        self.assertEqual(box()["label"], "3D acceleration (ATI Radeon)")
        self.assertTrue(box()["disabled"] and not box()["value"])
        self.assertIn("emulator does not include the ATI Radeon", json.dumps(self.fp.doc))
        self.fp.click("cancel")
        await self.until_doc("recovery-main")
        # a capable package, a hardware host GL and ATI's software on the disk
        pkg = self.app.paths.qemu_package
        m = json.loads((pkg / "manifest.json").read_text())
        (pkg / "manifest.json").write_text(json.dumps(dict(m, features=[graphics.FEATURE])))
        graphics.set_host_gl({"ok": True, "renderer": "AMD Radeon 890M", "software": False,
                              "render_nodes": ["/dev/dri/renderD128"]})
        try:
            mach = self.app.supervisor.active_machine()
            mach.update_meta(graphics={"ati_software": "present", "helper": True})
            self.fp.click("display")
            await self.until_doc("display-sound")
            self.assertFalse(box().get("disabled"))
            self.assertIn("Takes effect when Mac OS restarts", json.dumps(self.fp.doc))
            self.fp.click("apply", {"display.graphics": True})
            await helpers.wait_for(lambda: self.app.settings.desired["display.graphics"] == "ati-radeon", 10,
                                   what="desired")
            self.assertEqual(self.app.settings.applied["display.graphics"], "standard")   # Mac OS is running
            self.assertIn("display.graphics", self.app.settings.pending_by_policy()[schema.GUEST_RESTART])
        finally:
            graphics.set_host_gl(None)

    async def test_retroboot_window_panes_open_and_shut_down(self):
        """Control-Alt-Delete: the panes as a Control Panels icon view under a Finder header,
        About This Computer's facts and processor meters below; a selection makes Open the
        default; Shut Down… asks Mac OS's power question and Cancel comes back here."""
        await self.running()
        await self.until_event("finder_detected")
        self.fp.emit({"event": "chord", "name": "recovery"})
        await self.until_doc("recovery-main")
        doc = self.fp.doc
        self.assertEqual(doc["title"], "RetroBoot")
        lst = next(i for i in doc["items"] if i.get("type") == "list")
        self.assertEqual([r["id"] for r in lst["items"]],
                         ["disk", "download", "update", "network", "display", "timezone", "diagnostics"])
        self.assertEqual((lst["view"], lst["columns"]), ("icons", 4))
        self.assertTrue(lst["header"].startswith("7 items, Mac OS "), lst["header"])
        meters = next(i for i in doc["items"] if i.get("type") == "meters")["items"]
        self.assertEqual([(m["icon"], m["label"]) for m in meters][0], ("mac", self.app.supervisor.machine.name))
        self.assertEqual(meters[1]["icon"], "chip")
        self.assertEqual(next(r for r in lst["items"] if r["id"] == "display")["detail"], "1024 × 768")
        self.assertEqual(next(r for r in lst["items"] if r["id"] == "disk")["detail"], "Mac OS 9")
        buttons = [(b["id"], bool(b.get("default"))) for b in doc["items"][-1]["items"]]
        self.assertEqual(buttons, [("power", False), ("cancel", True)])
        self.assertNotIn("force-restart", json.dumps(doc))         # only when Mac OS is frozen
        self.fp.change("pane", "display")
        await helpers.wait_for(lambda: [b["id"] for b in self.fp.doc["items"][-1]["items"]] ==
                               ["power", "open", "cancel"], 5, what="Open button")
        self.assertTrue(self.fp.doc["items"][-1]["items"][1]["default"])
        self.fp.click("open")
        await self.until_doc("display-sound")
        self.fp.click("cancel")
        await self.until_doc("recovery-main")
        self.fp.click("pane", {"pane": "disk"})                     # a double-click on its icon
        await self.until_doc("startup-disk")
        self.fp.click("cancel")
        await self.until_doc("recovery-main")
        self.fp.click("power")
        await self.until_doc("power")
        self.assertIn("Mac OS shuts down first", json.dumps(self.fp.doc))
        self.fp.click("cancel")
        await self.until_doc("recovery-main")
        self.assertEqual(self.app.supervisor.state, "running")

    async def test_startup_disk_keeps_the_startup_options(self):
        """Startup Disk holds the old Setup window's two checkboxes."""
        await self.running()
        self.fp.emit({"event": "chord", "name": "recovery"})
        await self.until_doc("recovery-main")
        self.fp.click("disk")
        await self.until_doc("startup-disk")
        self.assertIn("When This Computer Starts", json.dumps(self.fp.doc))
        self.fp.change("autostart", False)
        await helpers.wait_for(lambda: self.app.settings.applied["startup.autostart"] is False, 5, what="applied")
        self.fp.click("checkpoints")
        await self.until_doc("checkpoints")
        self.fp.click("cancel")
        await self.until_doc("startup-disk")

    async def test_force_from_panel_requires_confirmation(self):
        await self.running()
        self.fp.emit({"event": "chord", "name": "recovery"})
        await self.until_doc("recovery-main")
        self.fp.click("force-shutdown")
        await self.until_doc("confirm-force")
        self.assertIn("Unsaved changes may be lost", json.dumps(self.fp.doc))
        self.fp.click("cancel")
        await self.until_doc("recovery-main")
        self.assertEqual(self.app.supervisor.state, "running")
        self.fp.click("force-shutdown")
        await self.until_doc("confirm-force")
        self.fp.click("force")
        await self.until_event("poweroff", 1, 15)
        self.assertEqual(self.ev("force_request")[0]["origin"], "panel")


class SettingsApply(Base):
    cfg_overrides = {"confirm_seconds": 1.5}

    async def test_restart_class_waits_for_apply_and_restart(self):
        await self.running()
        await self.until_event("finder_detected")
        s = self.app.settings
        rev = s.patch(s.revision, {"machine.ram_mb": 768, "input.swap_command_option": True},
                      self.app.validation_context())
        job = await self.job(self.app.apply_job(rev, False, origin="test"))
        self.assertEqual(job.result["pending_restart"], ["machine.ram_mb"])
        self.assertEqual(job.result["applied"], ["input.swap_command_option"])
        self.assertEqual(self.fp.requests_of("input_config")[-1]["swap_command_option"], True)
        self.assertEqual(s.applied["machine.ram_mb"], 512)
        argv = json.loads(self.app.paths.argv_json.read_text())["argv"]
        self.assertEqual(argv[argv.index("-m") + 1], "512")
        job = await self.job(self.app.apply_job(s.revision, True, origin="test"), 60)
        self.assertEqual(job.state, "succeeded", job.public())
        self.assertTrue(job.result["restarting"])
        argv = json.loads(self.app.paths.argv_json.read_text())["argv"]
        self.assertEqual(argv[argv.index("-m") + 1], "768")
        self.assertEqual(s.pending_keys(), [])

    async def test_display_sound_pages_and_live_volume(self):
        """Display & Sound as Monitors: the display picture and Monitor/Sound/Keyboard bevel
        buttons across the top, one window height for every page, and Sound's Main Volume
        along the bottom, applied as it moves (no Mute without a mixer switch)."""
        self.app.mixer.available = True        # sound, but no mixer control to mute
        await self.running()
        self.fp.emit({"event": "chord", "name": "recovery"})
        await self.until_doc("recovery-main")
        self.fp.click("pane", {"pane": "display"})
        await self.until_doc("display-sound")
        doc = self.fp.doc
        top = doc["items"][0]
        self.assertEqual((top["type"], top["rule"], top["height"]), ("band", "bottom", 76))
        bevels = find_item(doc, "page")
        self.assertEqual(([o["id"] for o in bevels["items"]], bevels["value"]), (["monitor", "sound", "keyboard"], "monitor"))
        self.assertEqual((doc["height"], doc["width"]), (409, 507))
        self.assertIsNotNone(find_item(doc, "display.pixel_scale"))
        self.assertIsNotNone(find_item(doc, "display.guest_size"))
        self.assertIsNone(find_item(doc, "audio.enabled"))
        volume = find_item(doc, "audio.volume")
        self.assertEqual((volume["type"], volume["icons"]), ("slider", ["speaker_low", "speaker_high"]))
        self.assertTrue(next(i for i in doc["items"] if i.get("type") == "band" and i.get("pin"))["pin"])
        self.assertIsNone(find_item(doc, "audio.mute"))          # the test mixer has no switch
        self.fp.change("page", "sound")
        await helpers.wait_for(lambda: find_item(self.fp.doc, "audio.enabled") is not None, 5, what="Sound page")
        self.assertEqual((find_item(self.fp.doc, "page")["value"], self.fp.doc["height"]), ("sound", 409))
        self.assertIsNone(find_item(self.fp.doc, "display.pixel_scale"))
        self.fp.change("page", "keyboard")
        await helpers.wait_for(lambda: find_item(self.fp.doc, "input.pointer_speed") is not None, 5,
                               what="Keyboard page")
        self.fp.change("audio.volume", 35)
        await helpers.wait_for(lambda: self.app.settings.applied["audio.volume"] == 35, 5, what="volume live")
        self.assertEqual(self.app.settings.desired["audio.volume"], 35)
        self.fp.click("cancel")                                 # the volume stays, as in Mac OS
        await self.until_doc("recovery-main")
        self.assertEqual(self.app.settings.applied["audio.volume"], 35)

    async def test_display_mode_reverts_unless_kept(self):
        await self.running()
        s = self.app.settings
        rev = s.patch(s.revision, {"display.host_mode": "1280x800@60"}, self.app.validation_context())
        job = await self.job(self.app.apply_job(rev, False, origin="web"))
        self.assertEqual(job.result["confirm"]["keys"], ["display.host_mode"])
        self.assertEqual(self.fp.requests_of("display_config")[-1]["host_mode"], [1280, 800, 60])
        await self.until_event("settings_reverted", 1, 5)
        self.assertEqual(s.applied["display.host_mode"], "auto")
        self.assertIsNone(self.fp.requests_of("display_config")[-1]["host_mode"])
        # A change from the web never puts a Keep/Revert or "restored" alert on this
        # computer's screen (it would block Mac OS); the web shows the outcome.
        self.assertNotIn("keep-setting", [d.get("id") for d in self.fp.docs])
        self.assertNotIn("message", [d.get("id") for d in self.fp.docs])
        self.assertEqual(self.fp.scene.get("scene"), "guest")
        self.assertEqual(s.last_confirm["outcome"], "reverted")
        rev = s.patch(s.revision, {"display.host_mode": "1280x800@60"}, self.app.validation_context())
        await self.job(self.app.apply_job(rev, False, origin="panel"))
        await self.until_doc("keep-setting")
        self.fp.click("keep")
        await self.until_event("settings_confirmed")
        await asyncio.sleep(2.0)
        self.assertEqual(s.applied["display.host_mode"], "1280x800@60")
        self.assertEqual(s.state["known_good"]["values"]["display.host_mode"], "1280x800@60")

    async def test_presenter_restart_gets_state_again(self):
        await self.running()
        n = len(self.fp.requests)
        paths = self.app.paths
        await self.fp.stop()
        fp2 = FakePresenter(str(paths.presenter_sock), None)
        await fp2.start()
        self.fp = fp2
        await helpers.wait_for(lambda: fp2.requests_of("hello") and fp2.requests_of("set_scene"), 10, what="repush")
        cmds = [r["cmd"] for r in fp2.requests]
        self.assertIn("display_config", cmds)
        self.assertIn("input_config", cmds)
        self.assertEqual(fp2.requests_of("set_scene")[-1]["scene"], "guest")


class PowerKeyOnce(Base):
    """One ADB power key per shut-down request. A second press while Mac OS's dialog is
    still drawing or open closes it again, so the following Return/R reaches the Finder and
    the request times out (measured on Mac OS 9.2.1; fake_qemu models it)."""
    modes = ["slowdialog:9"]

    async def test_slow_dialog_gets_one_power_key(self):
        await self.running()
        await self.until_event("finder_detected")
        job = await self.job(self.app.lifecycle_job("shutdown", poweroff=False, origin="test"))
        self.assertEqual(job.state, "succeeded", job.public())
        self.assertEqual(job.result["exit"]["reason"], "guest-shutdown")
        keys = [r for r in helpers.fake_qemu_log(self.pkg) if "keys" in r][0]["keys"]
        self.assertEqual(keys, ["power", "ret"])
        self.assertEqual(len(self.ev("power_key_sent")), 1)


class PowerButtonThenRequest(Base):
    async def test_button_repeat_and_request_reuse_one_key(self):
        await self.running()
        await self.until_event("finder_detected")
        self.fp.emit({"event": "power_button"})
        await self.until_event("power_key_sent")
        self.fp.emit({"event": "power_button"})          # impatient second press
        await self.until_event("power_button", 2)
        await self.until_event("power_button_ignored")
        # A shut-down request while the dialog the button opened is up confirms it.
        job = await self.job(self.app.lifecycle_job("shutdown", poweroff=False, origin="test"))
        self.assertEqual(job.state, "succeeded", job.public())
        self.assertTrue(self.ev("power_key_reused"))
        self.assertEqual(len(self.ev("power_key_sent")), 1)
        keys = [r for r in helpers.fake_qemu_log(self.pkg) if "keys" in r][0]["keys"]
        self.assertEqual(keys, ["power", "ret"])


class PrepUpgrade(Base):
    """A machine prepared by an older rbguestprep (no or a lower PREP_VERSION in
    machine.json) is prepared again once before its next launch; apply is idempotent."""

    def before_start(self) -> None:
        self.app.machines.get("mac-os-9").update_meta(
            prep={"result": "applied", "at": "2026-10-11T09:00:00Z", "attempts": 1, "report": {}})
        self.prep.PREP_VERSION = 2

    async def test_older_prep_is_upgraded_once(self):
        await self.running()
        self.assertEqual([c[0] for c in self.prep.calls], ["apply"])
        up = [e for e in self.ev("guest_prep") if e.get("at") == "upgrade"]
        self.assertEqual((len(up), up[0]["from_version"], up[0]["version"], up[0]["result"]), (1, 1, 2, "applied"))
        prep = self.app.machines.get("mac-os-9").meta["prep"]
        self.assertEqual((prep["result"], prep["version"], prep["upgraded_from"]), ("applied", 2, 1))
        # The next launch finds the disk current.
        job = await self.job(self.app.lifecycle_job("restart", origin="test"))
        self.assertEqual(job.state, "succeeded", job.public())
        await self.running(2)
        self.assertEqual([c[0] for c in self.prep.calls], ["apply"])


class FirstLaunchHooks(Base):
    """A disk prepared during this launch still gets the launch hooks (AirPort
    TCP/IP matching, guest isolation, the Platinum bundle); an early return after
    a fresh preparation used to skip them until the next launch."""

    def before_start(self) -> None:
        self.calls = []
        for obj, name in ((self.app.airport, "prepare_launch"), (self.app, "ensure_guest_isolation"),
                          (self.app.platinum, "ensure")):
            orig = getattr(obj, name)

            async def wrapped(*a, _orig=orig, _name=name, **kw):
                self.calls.append(_name)
                return await _orig(*a, **kw)
            setattr(obj, name, wrapped)

    async def test_hooks_run_on_the_launch_that_prepares_the_disk(self):
        await self.running()
        self.assertEqual([c[0] for c in self.prep.calls], ["apply"])
        for name in ("prepare_launch", "ensure_guest_isolation", "ensure"):
            self.assertIn(name, self.calls)


class PixelScaleLive(Base):
    """display.pixel_scale: offered for the presenter's display, applied live
    (display_config, no Mac OS restart), persisted for the next cold boot
    (rb-bootdisplay) and used for the next launch's -g."""

    def before_start(self) -> None:
        self.fp.display = dict(self.fp.display, mode=[1920, 1080, 60], suggested_scale=2, scale=2,
                               modes=[[1920, 1080, 60], [1280, 800, 60]])
        self.marker = Path(self.root) / "bootdisplay.log"
        self.app.cfg.bootdisplay_cmd = [sys.executable, "-c",
                                        "import sys; open(sys.argv[1], 'a').write('ran\\n')", str(self.marker)]

    def argv_g(self):
        argv = json.loads(self.app.paths.argv_json.read_text())["argv"]
        return argv[argv.index("-g") + 1]

    async def test_pixel_scale_live_and_next_launch(self):
        await self.running()
        self.assertEqual(self.argv_g(), "960x540x32")
        self.assertEqual(self.fp.requests_of("display_config")[-1]["pixel_scale"], "auto")
        st = await self.app.status()
        self.assertEqual(st["display"]["scale"], 2)
        self.assertEqual([(c["scale"], c["size"], c["available"]) for c in st["display"]["pixel_scales"]],
                         [(1, [1920, 1080], True), (2, [960, 540], True), (3, [640, 360], False),
                          (4, [480, 270], False)])
        snap = self.app.settings.snapshot(self.app.validation_context())
        ps = next(d for d in snap["schema"] if d["key"] == "display.pixel_scale")
        self.assertEqual([(c["value"], c.get("disabled", False)) for c in ps["choices"]],
                         [("auto", False), ("1", False), ("2", False), ("3", True), ("4", True)])
        s = self.app.settings
        with self.assertRaises(schema.ValidationError):
            s.patch(s.revision, {"display.pixel_scale": "3"}, self.app.validation_context())
        rev = s.patch(s.revision, {"display.pixel_scale": "1"}, self.app.validation_context())
        job = await self.job(self.app.apply_job(rev, False, origin="web"))
        self.assertEqual(job.state, "succeeded", job.public())
        self.assertIn("display.pixel_scale", job.result["applied"])
        self.assertIsNone(job.result["confirm"])
        self.assertEqual(self.fp.requests_of("display_config")[-1]["pixel_scale"], 1)
        await helpers.wait_for(lambda: (self.app.presenter.display or {}).get("scale") == 1, 5, what="display event")
        self.assertEqual(self.marker.read_text(), "ran\n")
        self.assertTrue(self.ev("boot_display_update")[-1]["ok"])
        st = await self.app.status()
        self.assertEqual((st["display"]["scale"], st["display"]["guest_size"]), (1, [1920, 1080]))
        self.assertEqual(len(self.ev("guest_start")), 1)   # live: Mac OS was not restarted
        # The next launch starts Mac OS at the new size.
        job = await self.job(self.app.lifecycle_job("restart", origin="test"))
        self.assertEqual(job.state, "succeeded", job.public())
        await self.running(2)
        self.assertEqual(self.argv_g(), "1920x1080x32")
        # The presenter restarting gets the pixel size again.
        self.assertEqual(self.app.apply_engine.display_config()["pixel_scale"], 1)


class CheckpointRunning(Base):
    async def test_checkpoint_and_restore_with_running_guest(self):
        await self.running()
        await self.until_event("finder_detected")
        job = await self.job(self.app.checkpoint_job("mac-os-9", "Clean", origin="web"), 60)
        self.assertEqual(job.state, "succeeded", job.public())
        self.assertIn(job.result["method"], ("reflink", "sparse"))
        exits = self.ev("guest_exited")
        self.assertEqual(exits[0]["reason"], "guest-shutdown")
        self.assertEqual(exits[0]["intent"], "shutdown-hold")
        self.assertFalse(self.ev("poweroff"))
        await self.running(2)
        await self.until_event("finder_detected", 2)
        cp = job.result["id"]
        job = await self.job(self.app.restore_job("mac-os-9", cp, origin="web"), 60)
        self.assertEqual(job.state, "succeeded", job.public())
        await self.running(3)
        self.assertTrue(self.ev("checkpoint_restored"))


class FirstTimeSetup(Base):
    machine = False

    def before_start(self):
        emb = self.app.paths.installer_dir / "embedded"
        emb.mkdir(parents=True)
        cfg = json.dumps({"name": "Mac OS 9", "machineFamily": "powerMacG4"}).encode()
        make_archive(emb / "macos9.tar.gz", [("config.json", cfg, "file"), ("disk.img", 1024 * 1024, "file")])

    async def test_local_setup_from_archive_with_time_zone(self):
        await self.until_doc("setup-welcome")
        self.assertEqual(self.fp.scene["background"], "checkerboard")
        self.fp.change("source", "usb")
        self.fp.click("continue", {"source": "usb"})
        await self.until_doc("timezone")
        self.fp.change("region", "Europe")
        await helpers.wait_for(lambda: "Europe/Berlin" in json.dumps(self.fp.doc), 5, what="europe list")
        self.fp.change("search", "new york")
        await helpers.wait_for(lambda: "America/New_York" in json.dumps(self.fp.doc), 5, what="search")
        self.fp.click("set", {"zone": "America/New_York"})
        await self.until_doc("setup-usb")
        await helpers.wait_for(lambda: "macos9.tar.gz" in json.dumps(self.fp.doc), 5, what="archive list")
        self.fp.click("install")
        await self.until_doc("setup-done", 20)
        self.assertEqual(self.app.settings.applied["system.timezone"], "America/New_York")
        mid = self.app.settings.applied["machine.active"]
        self.assertTrue(mid)
        self.fp.click("start")
        await self.running(1, 20)
        launch = [r for r in helpers.fake_qemu_log(self.pkg) if "argv" in r][0]
        self.assertEqual(launch["tz"], ":America/New_York")


if __name__ == "__main__":
    unittest.main()


class ShutdownScenes(Base):
    """Mac OS Restart -> splash (checkerboard) relaunch; Mac OS Shut Down -> black as soon as
    QMP reports SHUTDOWN, before QEMU exits (the presenter holds Mac OS's last frame meanwhile)."""
    modes = ["selfreset:1.2", "selfshutdown:1.2"]

    async def test_reset_relaunches_behind_splash_and_shutdown_goes_black_before_exit(self):
        await self.until_event("poweroff", 1, 25)
        seen = self.ev("shutdown_seen")
        self.assertEqual([s["reason"] for s in seen], ["guest-reset", "guest-shutdown"])
        self.assertNotIn("scene", seen[0])
        self.assertEqual(seen[1]["scene"], "black")
        exits = self.ev("guest_exited")
        black = [e for e in self.ev("scene") if e["scene"] == "black"]
        self.assertLess(black[0]["t_mono"], exits[1]["t_mono"])
        # Between the two sessions the relaunch showed the splash; after the second
        # session's guest scene nothing but black was requested.
        scenes = self.fp.scenes()
        last_guest = len(scenes) - 1 - scenes[::-1].index("guest")
        self.assertEqual(set(scenes[last_guest + 1:]), {"black"})
        first_guest = scenes.index("guest")
        self.assertIn("splash", scenes[first_guest + 1:last_guest])


class FrozenGuest(Base):
    cfg_overrides = {"frozen_stale_s": 1.5, "frozen_quiet_s": 0.0, "frozen_nudge_wait_s": 0.6,
                     "frozen_poll_s": 0.25}

    async def test_frozen_guest_flagged_without_restart_and_cleared_by_cont(self):
        await self.running()
        await self.until_event("finder_detected")
        sup = self.app.supervisor
        # A static but live guest redraws its cursor after the nudge: nothing is flagged.
        await helpers.wait_for(lambda: any(e.get("result") == "redrawn" for e in self.ev("guest_nudge")), 10,
                               what="nudge redrawn")
        self.assertIsNot(sup.responsive, False)
        self.assertGreaterEqual(self.fp.nudges, 1)
        # Freeze the guest with QMP stop; QMP itself keeps answering.
        await sup.qmp.stop()
        await self.until_event("guest_unresponsive", 1, 15)
        self.assertEqual(self.ev("guest_unresponsive")[0]["check"], "frozen display")
        self.assertTrue(any(e.get("result") == "no-redraw" for e in self.ev("guest_nudge")))
        st = await self.app.status()
        self.assertIs(st["guest"]["responsive"], False)
        self.assertEqual(st["guest"]["unresponsive_reason"], "display")
        self.assertIn("guest-unresponsive", [n["id"] for n in st["notices"]])
        self.assertEqual(sup.state, "running")
        await self.until_event("guest_unresponsive_evidence", 1, 10)
        # The recovery panel says so and offers Force Restart.
        self.fp.emit({"event": "chord", "name": "recovery"})
        await self.until_doc("recovery-main")
        doc = json.dumps(self.fp.doc)
        self.assertIn("Mac OS is not responding.", doc)
        self.assertIn("force-restart", doc)
        self.fp.click("cancel")      # back to Mac OS: the guest scene is shown again
        await helpers.wait_for(lambda: self.fp.scene.get("scene") == "guest", 5, what="guest scene")
        await helpers.wait_for(lambda: len(self.ev("guest_revealed")) >= 2, 5, what="second reveal event")
        ids = [e["id"] for e in (await self.app.status())["boot"]["events"]]
        self.assertEqual(len(ids), len(set(ids)), ids)       # startup timeline: first occurrence only
        # cont: the guest catches up (cursor redraw) and the flag clears; never restarted.
        await sup.qmp.cont()
        await self.until_event("guest_responsive_again", 1, 15)
        self.assertIsNot(sup.responsive, False)
        self.assertEqual(len(self.ev("guest_start")), 1)
        self.assertFalse(self.ev("guest_exited"))


class FrozenEmulator(Base):
    """QEMU stops answering QMP (SIGSTOP, like a deadlocked emulator): clean requests fail
    with force available instead of an internal error, the state is not left at stopping,
    and Force Restart acts within seconds."""
    cfg_overrides = {"qmp_watchdog_s": 0.5, "qmp_watchdog_timeout_s": 0.5, "frozen_check": False}

    async def test_clean_requests_offer_force_and_force_is_prompt(self):
        import signal
        import time
        await self.running()
        await self.until_event("finder_detected")
        sup = self.app.supervisor
        pid = json.loads(self.app.paths.status_json.read_text())["pid"]
        os.kill(pid, signal.SIGSTOP)
        self.addCleanup(lambda: os.kill(pid, signal.SIGCONT) if os.path.exists(f"/proc/{pid}") else None)
        await helpers.wait_for(lambda: sup.qmp_ok is False, 10, what="emulator flagged")
        self.assertEqual(self.ev("guest_unresponsive")[-1]["check"], "qmp query-status")
        for kind, power_off, word in (("restart", None, "restart"), ("shutdown", False, "shut down")):
            job = await self.job(self.app.lifecycle_job(kind, poweroff=power_off, origin="web"), 20)
            pub = job.public()
            self.assertEqual(pub["state"], "failed")
            self.assertEqual(pub["error"]["code"], "guest-unresponsive", pub)
            self.assertTrue(pub["result"]["force_available"])
            self.assertIn(f"request to {word}", pub["error"]["message"])
            self.assertEqual(sup.state, "running")
            self.assertIsNone(sup.intent)
        job = await self.job(self.app.host_job("poweroff", origin="web"), 20)
        self.assertEqual(job.public()["error"]["code"], "guest-unresponsive")
        self.assertIn("RetroBoot is still on", job.public()["error"]["message"])
        self.assertFalse(self.app.powered_off)
        st = await self.app.status()
        self.assertEqual(st["guest"]["state"], "running")
        self.assertIs(st["guest"]["responsive"], False)
        self.assertEqual(st["guest"]["unresponsive_reason"], "emulator")
        # Force Restart: bounded evidence, SIGKILL after the SIGTERM grace, new session.
        t0 = time.monotonic()
        job = await self.job(self.app.lifecycle_job("force-restart", origin="web"), 40)
        self.assertEqual(job.state, "succeeded", job.public())
        stop_s = self.ev("guest_exited")[-1]["t_mono"] - self.ev("force_request")[-1]["t_mono"]
        self.assertLess(self.ev("force_request")[-1]["t_mono"] - t0, 5.0)   # evidence within its budget
        self.assertLess(stop_s, 6.0)                                        # SIGKILL after 3 s
        self.assertTrue(self.ev("force_kill")[-1]["killed"])
        summary = json.loads((self.app.paths.evidence / job.result["evidence"] / "summary.json").read_text())
        self.assertIn("not answering QMP", summary["screenshots"]["guest_note"])
        self.assertTrue(job.message.startswith("Mac OS was restarted."), job.message)
        self.assertEqual(len(self.ev("guest_start")), 2)


class EscapeBeforeLaunch(Base):
    """Escape pressed while hostd is still getting ready (disk prep, before QEMU starts)
    holds the automatic start and shows the startup options (the panel used to flash and
    Mac OS started anyway)."""

    def before_start(self):
        self.prep.delay = 2.0

    async def test_escape_while_preparing_shows_startup_options(self):
        await helpers.wait_for(lambda: self.prep.calls, 10, what="disk prep running")
        self.fp.emit({"event": "preboot_key", "key": "escape"})
        await self.until_doc("startup-options")
        await asyncio.sleep(3.0)
        self.assertFalse(self.ev("guest_start"))
        self.assertTrue(self.ev("guest_start_held"))
        self.assertEqual((self.fp.doc or {}).get("id"), "startup-options")
        self.assertEqual(self.fp.scene["scene"], "panel")
        self.fp.click("start")
        await self.running(1, 20)


class EscapeDuringLaunch(Base):
    """Escape that lands after the disk-prep check but before QEMU runs (package
    verification) pauses the new guest at once and shows the boot options."""

    def before_start(self):
        import time as _t
        real = self.app.package.verify

        def slow_verify():
            _t.sleep(2.0)
            return real()
        self.app.package.verify = slow_verify

    async def test_escape_during_package_verify_pauses_new_guest(self):
        await helpers.wait_for(lambda: self.prep.calls, 10, what="disk prep done")
        await asyncio.sleep(0.5)                 # now inside package verification
        self.fp.emit({"event": "preboot_key", "key": "escape"})
        await self.until_doc("boot-options", 15)
        await helpers.wait_for(lambda: self.app.supervisor.paused, 10, what="guest paused")
        self.assertTrue(self.ev("guest_start"))
        self.assertTrue([e for e in self.ev("guest_start_held") if e.get("why") == "escape during launch"])
        # Escape opens this window; a second Escape must not dismiss it (no "cancel" button).
        self.assertNotIn('"id": "cancel"', json.dumps(self.fp.doc))
        self.fp.click("start")                   # Start Mac OS
        await self.running(1, 20)


class AdoptionTests(unittest.IsolatedAsyncioTestCase):
    """A guest adopted after a hostd restart counts as at the Finder only when its
    session is old enough; a young one is still starting (N5105: a restart request
    1 min into a boot confirmed a dialog that had not appeared)."""

    async def adopt(self, age_s):
        from retroboot.util import mono
        td, root = helpers.scratch()
        self.addCleanup(td.cleanup)
        app = Hostd(helpers.make_config(root))
        app.settings.store.load()
        app.loop = asyncio.get_running_loop()

        class Handle:
            session, pid = "s-1", 4242

            def status(self):
                return {"machine": "mac-os-9", "session": "s-1", "pid": 4242, "started_mono": mono() - age_s}

            async def wait(self):
                await asyncio.Event().wait()

        async def adopt():
            return Handle()
        app.runner.adopt = adopt

        async def show_scene(*a, **k):
            return None
        app.show_scene = show_scene
        sup = app.supervisor
        self.assertTrue(await sup.try_adopt())
        for t in list(sup._tasks):
            t.cancel()
        return sup

    async def test_young_session_is_probed(self):
        sup = await self.adopt(50)
        self.assertFalse(sup.finder_seen)
        self.assertFalse(sup.finder_event.is_set())
        self.assertFalse(sup.mac_os_up)
        self.assertFalse(sup.up_event.is_set())

    async def test_settled_session_is_at_the_finder(self):
        sup = await self.adopt(600)
        self.assertTrue(sup.finder_seen)
        self.assertTrue(sup.finder_event.is_set())
        self.assertTrue(sup.mac_os_up)
        self.assertTrue(sup.up_event.is_set())
