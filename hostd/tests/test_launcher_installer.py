"""guest-launcher exit codes/status.json and the installer panel flow."""
import asyncio
import json
import os
import signal
import subprocess
import sys
import unittest
from pathlib import Path

import helpers
from fakes.fake_presenter import FakePresenter
from retroboot import schema
from retroboot.config import Paths
from retroboot.hostd import Hostd
from retroboot.machines import MachineRegistry, build_argv, launch_spec


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        self.pkg = helpers.make_package(self.root)
        helpers.make_machine(self.root)
        self.paths = Paths(self.root)
        self.paths.ensure()
        m = MachineRegistry(self.paths).get("mac-os-9")
        settings = dict(schema.defaults(), **{"system.timezone": "Europe/Berlin"})
        argv = build_argv(m, settings, self.paths, None)
        spec = launch_spec(m, argv, self.paths, "sess-1", settings, None, "retroboot")
        self.paths.argv_json.write_text(json.dumps(spec))

    def launch(self, mode, signal_after=None):
        helpers.fake_qemu_conf(self.pkg, modes=[mode], boot_s=0.1, finder_s=0.2)
        env = dict(os.environ, PYTHONPATH=str(helpers.SRC))
        p = subprocess.Popen([sys.executable, "-m", "retroboot.launcher", "--root", str(self.root)], env=env)
        if signal_after is not None:
            import time
            deadline = time.time() + 10
            while time.time() < deadline:
                st = self.status()
                if st and st.get("state") == "running":
                    break
                time.sleep(0.05)
            time.sleep(signal_after)
            p.send_signal(signal.SIGTERM)
        return p.wait(timeout=30)

    def status(self):
        try:
            return json.loads(self.paths.status_json.read_text())
        except (OSError, ValueError):
            return None

    def test_exit_codes(self):
        for mode, code, reason in (("selfshutdown:0.5", 0, "guest-shutdown"), ("selfreset:0.5", 10, "guest-reset"),
                                   ("crash", 1, "crash")):
            with self.subTest(mode=mode):
                self.assertEqual(self.launch(mode), code)
                st = self.status()
                self.assertEqual((st["session"], st["state"], st["exit"]["reason"], st["exit"]["code"]),
                                 ("sess-1", "exited", reason, code))
        self.assertEqual(self.launch("normal", signal_after=0.3), 20)
        self.assertEqual(self.status()["exit"]["reason"], "host-signal")
        launches = [r for r in helpers.fake_qemu_log(self.pkg) if "argv" in r]
        self.assertTrue(all(r["session"] == "sess-1" for r in launches))
        self.assertEqual(launches[0]["tz"], ":Europe/Berlin")
        self.assertTrue(self.paths.serial_log.exists())

    @unittest.skipUnless(os.geteuid() == 0, "needs root")
    def test_runs_qemu_as_rbguest(self):
        import grp
        import pwd
        try:
            pw = pwd.getpwnam("rbguest")
        except KeyError:
            self.skipTest("no rbguest user (packaging/retroboot.sysusers)")
        os.chmod(self.root, 0o755)  # mkdtemp is 0700; the image's paths are world-traversable
        helpers.fake_qemu_conf(self.pkg, modes=["selfshutdown:0.5"], boot_s=0.1, finder_s=0.2)
        for n in ("fake-qemu.state", "fake-qemu.log"):   # the fake's own bookkeeping, not product files
            (self.pkg / n).touch()
            os.chmod(self.pkg / n, 0o666)
        spec = json.loads(self.paths.argv_json.read_text())
        spec["run_as"] = "rbguest"
        self.paths.argv_json.write_text(json.dumps(spec))
        env = dict(os.environ, PYTHONPATH=str(helpers.SRC))
        rc = subprocess.run([sys.executable, "-m", "retroboot.launcher", "--root", str(self.root)], env=env,
                            timeout=30).returncode
        self.assertEqual(rc, 0, self.status())
        rec = [r for r in helpers.fake_qemu_log(self.pkg) if "uid" in r][-1]
        self.assertEqual((rec["uid"], rec["gid"]), (pw.pw_uid, pw.pw_gid))
        want = {grp.getgrnam(g).gr_gid for g in ("audio", "rbdata")}
        for g in ("render", "video"):   # the GPU for the ATI card (graphics.py), where the group exists
            try:
                want.add(grp.getgrnam(g).gr_gid)
            except KeyError:
                pass
        self.assertTrue(want <= set(rec["groups"]))
        self.assertEqual(rec["umask"], "0o7")
        self.assertEqual(self.paths.serial_log.stat().st_uid, pw.pw_uid)
        m = MachineRegistry(self.paths).get("mac-os-9")
        self.assertEqual(m.disk.stat().st_uid, pw.pw_uid)
        self.assertEqual(oct(self.paths.run.stat().st_mode & 0o7777), "0o1775")

    def test_package_verification(self):
        (self.pkg / "share/qemu/ndrvloader").write_bytes(b"tampered")
        self.assertEqual(self.launch("normal"), 1)
        st = self.status()
        self.assertIn("hash mismatch", st["exit"]["detail"])
        self.assertFalse(helpers.fake_qemu_log(self.pkg))  # QEMU never ran

    def test_only_qualified_native_ati_receives_the_selected_gpu(self):
        from retroboot import osx, osx_graphics
        spec = json.loads(self.paths.argv_json.read_text())
        manifest_path = self.pkg / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["features"] = sorted({osx.FEATURE, osx.QEMU_FEATURE, osx.NVRAM_FEATURE,
                                       osx.FIRMWARE_FEATURE, "display-retroboot",
                                       osx_graphics.FEATURE, osx_graphics.EGL_FEATURE,
                                       osx_graphics.COHERENT_FEATURE, osx_graphics.I8_FEATURE} |
                                      osx.SLEEP_FEATURES)
        manifest["sources"]["qemu"] = osx.QEMU_SOURCE
        manifest_path.write_text(json.dumps(manifest))
        spec.update(guest_os="macosx-10.4", os_build="8S165", egl_render_node="/dev/dri/renderD129")
        spec["argv"] += ["-device", "ati-vga,model=r100,x-agp=on,x-coherent-vram=on,x-i8-byte-order=on",
                         "-global", "via-pmu.osx-sleep=on", "-global",
                         "uni-north-pci-pcihost.x-agp-compat=on", "-boot", "c"]
        self.paths.argv_json.write_text(json.dumps(spec))
        self.assertEqual(self.launch("selfshutdown:0.5"), 0)
        record = [r for r in helpers.fake_qemu_log(self.pkg) if "argv" in r][-1]
        self.assertEqual(record["egl_render_node"], "/dev/dri/renderD129")
        before = len(helpers.fake_qemu_log(self.pkg))
        spec["guest_os"] = "classic"
        self.paths.argv_json.write_text(json.dumps(spec))
        self.assertEqual(self.launch("normal"), 1)
        self.assertEqual(len(helpers.fake_qemu_log(self.pkg)), before)


LIST_DISKS = """#!{py}
import json
print(json.dumps([
  {{"path": "/dev/sr0", "model": "QEMU DVD-ROM", "size_bytes": 900000000, "removable": True, "transport": "sata", "is_boot_medium": True}},
  {{"path": "/dev/vda", "model": "Virtual Disk", "size_bytes": 42949672960, "removable": False, "transport": "virtio", "is_boot_medium": False}}
]))
"""

# Two disks of the same model and size (as on the first hardware tested): one
# holds Windows, the other only data volumes.
LIST_DISKS_TWINS = """#!{py}
import json
twin = {{"model": "Samsung SSD 980 PRO 2TB", "size_bytes": 2000398934016, "removable": False, "transport": "nvme",
        "is_boot_medium": False}}
print(json.dumps([
  {{"path": "/dev/sda", "model": "JetKVM Virtual Media", "size_bytes": 754511872, "removable": True, "transport": "usb", "is_boot_medium": True}},
  dict(twin, path="/dev/nvme0n1", serial="S6B0NU0W928620H",
       contents={{"partitions": 4, "os": None, "labels": ["VMStorage", "LittleFat"], "summary": "VMStorage, LittleFat"}}),
  dict(twin, path="/dev/nvme1n1", serial="S6B0NU0W929018R",
       contents={{"partitions": 4, "os": "Windows", "labels": [], "summary": "Windows"}}),
]))
"""

INSTALL = """#!{py}
import json, sys, time, pathlib
pathlib.Path({rec!r}).write_text(json.dumps(sys.argv[1:]))
seed = sys.argv[sys.argv.index("--seed") + 1]
pathlib.Path({rec!r} + ".seed").write_text(pathlib.Path(seed).read_text())
for i, phase in enumerate(["partition", "root", "data", "machine", "bootloader"]):
    print(json.dumps({{"phase": phase, "progress": (i + 1) / 5, "message": "Installing " + phase}}), flush=True)
    time.sleep(0.05)
sys.exit({rc})
"""


class InstallerTests(unittest.IsolatedAsyncioTestCase):
    rc = 0

    async def asyncSetUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        helpers.make_package(self.root)
        self.cfg = helpers.make_config(self.root, mode="installer")
        paths = Paths(self.root)
        paths.ensure()
        inst = paths.installer_dir
        (inst / "embedded/machine").mkdir(parents=True)
        mdir = inst / "embedded/machine"
        with open(mdir / "disk.img", "wb") as f:
            f.truncate(1024 * 1024)
        (mdir / "machine.json").write_text(json.dumps({"schema_version": 1, "id": "mac-os-9", "name": "Mac OS 9",
                                                       "disk": "disk.img"}))
        self.record = str(self.root / "install-args.json")
        (inst / "list-disks").write_text(LIST_DISKS.format(py=sys.executable))
        (inst / "install-to-disk").write_text(INSTALL.format(py=sys.executable, rec=self.record, rc=self.rc))
        for n in ("list-disks", "install-to-disk"):
            (inst / n).chmod(0o755)
        self.fp = FakePresenter(str(paths.presenter_sock), None)
        await self.fp.start()
        self.app = Hostd(self.cfg)
        self.app.settings.store.load()
        helpers.install_fake_prep(self.app)
        await self.app.start()

    async def asyncTearDown(self):
        await self.app.shutdown()
        await self.fp.stop()

    async def doc(self, doc_id, contains=None, timeout=10):
        await helpers.wait_for(lambda: (self.fp.doc or {}).get("id") == doc_id and
                               (contains is None or contains in json.dumps(self.fp.doc, ensure_ascii=False)), timeout,
                               what=f"{doc_id} {contains or ''}")

    async def walk_to_install(self):
        await self.doc("installer-welcome")
        self.assertEqual(self.fp.scene["scene"], "panel")
        self.fp.click("continue")
        await self.doc("installer-hardware", "1 disk available")
        self.fp.click("continue")
        await self.doc("installer-destination")
        self.assertNotIn("/dev/sr0", json.dumps(self.fp.doc))  # boot medium never offered
        self.fp.click("continue", {"disk": "/dev/vda"})
        await self.doc("installer-erase", "Virtual Disk")
        self.assertIn("40 GB", json.dumps(self.fp.doc))
        self.fp.click("cancel")  # default button is Cancel
        await self.doc("installer-destination")
        self.assertFalse(Path(self.record).exists())
        self.fp.click("continue", {"disk": "/dev/vda"})
        await self.doc("installer-erase")
        self.fp.click("erase")
        await self.doc("installer-macs", "On this disc")
        self.fp.click("continue")
        await self.doc("installer-display", "1024 × 768")
        self.fp.click("continue")
        await self.doc("timezone")
        self.fp.click("set", {"zone": "Europe/Paris"})
        await self.doc("installer-network")
        self.fp.click("continue")
        await self.doc("installer-ready")
        self.fp.click("install")

    async def test_full_install(self):
        await self.walk_to_install()
        await self.doc("installer-done", "Remove the installation", 20)
        args = json.loads(Path(self.record).read_text())
        mdir = str(self.app.paths.installer_dir / "embedded/machine")
        self.assertEqual(args, ["--target", "/dev/vda", "--machine-dir", mdir,
                                "--seed", str(self.app.paths.run / "install-seed.json"), "--hostname", "retroboot"])
        seed = json.loads(Path(self.record + ".seed").read_text())
        self.assertEqual(seed["settings"]["system.timezone"], "Europe/Paris")
        self.assertEqual(seed["settings"]["machine.active"], "mac-os-9")
        phases = [e["phase"] for e in helpers.events(self.app, "install_phase")]
        self.assertEqual(phases, ["partition", "root", "data", "machine", "bootloader"])
        self.assertTrue(helpers.events(self.app, "installer_erase_confirmed"))
        self.fp.click("restart")
        await helpers.wait_for(lambda: helpers.events(self.app, "host_reboot"), 5, what="reboot")
        # Recovery chord and Escape are inert in the installer.
        self.fp.emit({"event": "chord", "name": "recovery"})
        await asyncio.sleep(0.3)
        self.assertNotEqual((self.fp.doc or {}).get("id"), "recovery-main")


class InstallerBluetoothTests(InstallerTests):
    """A Bluetooth keyboard paired during installation goes to the installed system."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        dev = self.app.paths.bluetooth_storage / "00:1A:7D:DA:71:13" / "C8:2B:96:1A:4E:01"
        dev.mkdir(parents=True)
        (dev / "info").write_text("[General]\nName=Bluetooth Keyboard\n")

    async def test_full_install(self):
        await self.walk_to_install()
        await self.doc("installer-done", "Remove the installation", 20)
        args = json.loads(Path(self.record).read_text())
        self.assertEqual(args[-2:], ["--bluetooth", str(self.app.paths.bluetooth_storage)])
        self.assertEqual(helpers.events(self.app, "install_started")[0]["bluetooth_pairings"], 1)


class InstallerFailureTests(InstallerTests):
    rc = 3

    async def test_full_install(self):
        await self.walk_to_install()
        await self.doc("message", "did not finish", 20)
        self.assertTrue(helpers.events(self.app, "install_failed")[0]["evidence"].startswith("RB-"))


class InstallerTwinDiskTests(InstallerTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        (self.app.paths.installer_dir / "list-disks").write_text(LIST_DISKS_TWINS.format(py=sys.executable))

    async def test_full_install(self):
        await self.doc("installer-welcome")
        self.fp.click("continue")
        await self.doc("installer-hardware", "2 disks available")
        self.fp.click("continue")
        await self.doc("installer-destination")
        text = json.dumps(self.fp.doc, ensure_ascii=False)
        # look-alike disks are told apart by contents and serial suffix; none is preselected
        self.assertIn("…018R · Windows", text)
        self.assertIn("…620H · VMStorage, LittleFat", text)
        self.assertNotIn("JetKVM", text)

        def state():
            lst = next(i for i in self.fp.doc["items"] if i.get("type") == "list")
            cont = [b for i in self.fp.doc["items"] if i.get("type") == "buttons" for b in i["items"]
                    if b["id"] == "continue"]
            return lst.get("value"), bool(cont[0].get("disabled"))
        self.assertEqual(state(), (None, True))
        # choosing a row enables Continue (the document is re-sent)
        self.fp.change("disk", "/dev/nvme1n1")
        await helpers.wait_for(lambda: state() == ("/dev/nvme1n1", False), 5, what="Continue enabled")
        self.fp.click("continue", {"disk": "/dev/nvme1n1"})
        await self.doc("installer-erase", "It contains: Windows.")
        self.assertIn("serial number ending 018R", json.dumps(self.fp.doc))   # look-alike disks, no device path
        self.assertNotIn("/dev/", json.dumps(self.fp.doc))


class InstalledSeedTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_start_adopts_seed(self):
        td, root = helpers.scratch()
        self.addCleanup(td.cleanup)
        mdir = helpers.make_machine(root, "mac-os-9")
        (mdir / "install-seed.json").write_text(json.dumps({"settings": {
            "system.timezone": "Asia/Tokyo", "machine.active": "mac-os-9", "management.hostname": "macbox",
            "bogus": 1}}))
        app = Hostd(helpers.make_config(root))
        app.settings.store.load()
        app.loop = asyncio.get_running_loop()
        app._seed_from_install()
        self.assertEqual(app.settings.applied["system.timezone"], "Asia/Tokyo")
        self.assertEqual(app.settings.applied["machine.active"], "mac-os-9")
        self.assertEqual(app.settings.applied["management.hostname"], "macbox")
        self.assertTrue((mdir / "install-seed.applied.json").exists())


if __name__ == "__main__":
    unittest.main()
