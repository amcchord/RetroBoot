"""The installer on a Raspberry Pi (ARCHITECTURE 8.2): the installer's own card as a
destination (only the free space after the installer), other disks erased with the Pi
layout, the medium after installation, and Recovery's Start Installer… on a card that
keeps the installer (tryboot)."""
import asyncio
import importlib.machinery
import importlib.util
import json
import sys
import unittest
from pathlib import Path

import helpers
from fakes.fake_presenter import FakePresenter
from retroboot import platform as hostplatform
from retroboot import rpiboot
from retroboot.config import Paths
from retroboot.hostd import Hostd
from retroboot.panel import installer as inst
from retroboot.panel.picker import FIXED_BYTES, MARGIN, room_for_macs

GiB = 1024 ** 3
PI4 = ("Raspberry Pi 4 Model B Rev 1.5", ["raspberrypi,4-model-b", "brcm,bcm2711"], 0xc03115)
REPO = Path(__file__).resolve().parents[2]

FREE = {"eligible": True, "reason": None, "message": None, "bytes": 25 * GiB, "start": 4016 * 1024 ** 2,
        "end": 32 * GiB, "table": "dos", "existing": None, "remove": [], "numbers": [3, 4],
        "boot_partition": "mmcblk0p1"}

LIST_DISKS = """#!{py}
import json
print(json.dumps([
  {{"path": "/dev/mmcblk0", "model": "SD SN32G", "size_bytes": 31914983424, "removable": True,
    "transport": None, "is_boot_medium": True, "free_space": {free}}},
  {{"path": "/dev/sda", "model": "Samsung Portable SSD", "size_bytes": 250059350016, "removable": True,
    "transport": "usb", "is_boot_medium": False,
    "contents": {{"partitions": 5, "os": "RetroBoot", "labels": [], "summary": "RetroBoot"}}}}
]))
"""

INSTALL = """#!{py}
import json, sys, time, pathlib
pathlib.Path({rec!r}).write_text(json.dumps(sys.argv[1:]))
for i, phase in enumerate(["partition", "system", "bootloader", "machine"]):
    print(json.dumps({{"phase": phase, "progress": (i + 1) / 5, "message": "Installing " + phase}}), flush=True)
    time.sleep(0.03)
print(json.dumps({{"phase": "finish", "progress": 1.0, "message": "Installation complete", "medium": {medium!r}}}),
      flush=True)
"""


class PiInstallerBase(unittest.IsolatedAsyncioTestCase):
    free = FREE
    medium = "kept"

    async def asyncSetUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        helpers.make_package(self.root)
        helpers.make_device_tree(self.root, *PI4)
        self.cfg = helpers.make_config(self.root, mode="installer", installer_board="auto")
        paths = Paths(self.root)
        paths.ensure()
        idir = paths.installer_dir
        mdir = idir / "embedded/machines/os922"
        mdir.mkdir(parents=True)
        with open(mdir / "disk.img", "wb") as f:
            f.truncate(1024 * 1024)
        (mdir / "machine.json").write_text(json.dumps({"schema_version": 1, "id": "os922", "name": "Mac OS 9.2.2",
                                                       "disk": "disk.img"}))
        (idir / "embedded/machines/order").write_text("os922\n")
        self.record = str(self.root / "install-args.json")
        (idir / "list-disks").write_text(LIST_DISKS.format(py=sys.executable, free=repr(self.free)))
        (idir / "install-to-disk").write_text(INSTALL.format(py=sys.executable, rec=self.record, medium=self.medium))
        for n in ("list-disks", "install-to-disk"):
            (idir / n).chmod(0o755)
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

    def text(self):
        return json.dumps(self.fp.doc, ensure_ascii=False)

    def rows(self):
        lst = next(i for i in self.fp.doc["items"] if i.get("type") == "list")
        return {r["id"]: r for r in lst["items"]}

    async def to_destination(self):
        await self.doc("installer-welcome", "free space on the installer’s own card")
        self.fp.click("continue")
        await self.doc("installer-hardware", "2 disks available")
        self.assertIn("Computer: Raspberry Pi 4 Model B Rev 1.5, 4 GB", self.text())
        self.fp.click("continue")
        await self.doc("installer-destination")

    async def through_macs(self):
        await self.doc("installer-macs", "On this disc")
        self.fp.click("continue")
        await self.doc("installer-display")
        self.fp.click("continue")
        await self.doc("timezone")
        self.fp.click("set", {"zone": "Europe/Paris"})
        await self.doc("installer-network")
        self.fp.click("continue")
        await self.doc("installer-ready")


class FreeSpaceTests(PiInstallerBase):
    async def test_this_sd_card_keeps_the_installer(self):
        self.assertEqual(self.app.panel.board, "rpi")
        await self.to_destination()
        rows = self.rows()
        self.assertEqual(list(rows), ["/dev/mmcblk0#free-space", "/dev/sda"])     # the card first
        free = rows["/dev/mmcblk0#free-space"]
        self.assertEqual(free["label"], "This SD card")
        self.assertEqual(free["detail"], "25 GB free after the installer")
        self.assertIn("keeps the installer and uses only the free space after it; any other disk", self.text())
        self.fp.click("continue", {"disk": "/dev/mmcblk0#free-space"})
        await self.doc("installer-erase", "Install in the free space on this SD card?")
        self.assertIn("The installer stays on the SD card", self.text())
        self.assertIn("Use Free Space", self.text())
        self.fp.click("cancel")
        await self.doc("installer-destination")
        self.assertFalse(Path(self.record).exists())
        self.fp.click("continue", {"disk": "/dev/mmcblk0#free-space"})
        await self.doc("installer-erase")
        self.fp.click("erase")
        await self.doc("installer-macs", "On this disc")
        # room: the free space less the root, its alignment and install-to-disk's margin
        self.assertIn(f"of {ui_bytes(25 * GiB - inst.PI_FREE_FIXED_BYTES - MARGIN)} available", self.text())
        await self.through_macs()
        self.assertIn("in the free space after the installer on this SD card", self.text())
        self.assertIn("The installer is kept.", self.text())
        self.fp.click("install")
        await self.doc("installer-done", "start RetroBoot from this SD card", 20)
        self.assertIn("Start Installer", self.text())
        self.assertNotIn("Remove the", self.text())
        args = json.loads(Path(self.record).read_text())
        self.assertEqual(args[:5], ["--target", "/dev/mmcblk0", "--board", "rpi", "--mode"])
        self.assertEqual(args[5], "free-space")
        self.assertIn("--machine-dir", args)
        ev = helpers.events(self.app, "install_started")[0]
        self.assertEqual((ev["board"], ev["mode"]), ("rpi", "free-space"))
        self.assertEqual(helpers.events(self.app, "installer_erase_confirmed")[0]["mode"], "free-space")
        self.assertEqual(helpers.events(self.app, "install_finished")[0]["medium"], "kept")
        self.fp.click("restart")
        await helpers.wait_for(lambda: helpers.events(self.app, "host_reboot"), 5, what="reboot")
        self.assertNotIn("arg", helpers.events(self.app, "host_reboot")[0])


class ReplaceTests(PiInstallerBase):
    free = dict(FREE, existing="RetroBoot", remove=[3, 4])

    async def test_replacing_an_earlier_installation_says_so(self):
        await self.to_destination()
        self.assertEqual(self.rows()["/dev/mmcblk0#free-space"]["detail"], "25 GB with RetroBoot, after the installer")
        self.fp.click("continue", {"disk": "/dev/mmcblk0#free-space"})
        await self.doc("installer-erase", "Replace RetroBoot on this SD card?")
        self.assertIn("permanently erased, with its Macs, documents and settings", self.text())
        self.assertIn("Erase and Continue", self.text())


class NotOfferedTests(PiInstallerBase):
    free = dict(FREE, eligible=False, reason="other-partitions", message="other partitions follow the installer")

    async def test_reason_is_shown_and_other_disks_still_offered(self):
        await self.doc("installer-welcome")
        self.fp.click("continue")
        await self.doc("installer-hardware", "1 disk available")
        self.fp.click("continue")
        await self.doc("installer-destination")
        self.assertEqual(list(self.rows()), ["/dev/sda"])
        self.assertIn("This SD card isn’t offered: other partitions follow the installer.", self.text())


class UsbDiskTests(PiInstallerBase):
    medium = "in-use"

    async def test_usb_disk_is_erased_and_the_card_comes_out_after_shut_down(self):
        await self.to_destination()
        self.fp.click("continue", {"disk": "/dev/sda"})
        # a disk with RetroBoot is not updated in place on a Pi: straight to the erase alert
        await self.doc("installer-erase", "Samsung Portable SSD")
        self.assertIn("It contains: RetroBoot.", self.text())
        self.fp.click("erase")
        await self.through_macs()
        self.assertIn("The disk will be erased now.", self.text())
        self.fp.click("install")
        await self.doc("installer-done", "Click Shut Down, remove the installation SD card", 20)
        self.assertIn("boot order", self.text())
        self.assertIn("rpi-eeprom-config", self.text())
        args = json.loads(Path(self.record).read_text())
        self.assertEqual(args[:4], ["--target", "/dev/sda", "--board", "rpi"])
        self.assertNotIn("--mode", args)
        self.fp.click("shutdown")
        await helpers.wait_for(lambda: helpers.events(self.app, "host_poweroff") or
                               helpers.events(self.app, "poweroff"), 5, what="poweroff")


class ReleasedTests(PiInstallerBase):
    medium = "released"

    async def test_released_medium_restart(self):
        await self.to_destination()
        self.fp.click("continue", {"disk": "/dev/sda"})
        await self.doc("installer-erase")
        self.fp.click("erase")
        await self.through_macs()
        self.fp.click("install")
        await self.doc("installer-done", "Remove the installation SD card, then click Restart.", 20)


class BootDisplayTests(PiInstallerBase):
    async def test_installer_never_edits_a_boot_partition(self):
        """The live installer's /boot/firmware could only be another disk's: the seed reaches
        the installed cmdline.txt through install-to-disk instead."""
        self.app.cfg.bootdisplay_cmd = ["false"]
        self.assertFalse(await self.app.apply_engine.update_boot_display())
        self.assertEqual(helpers.events(self.app, "boot_display_update")[-1]["skipped"], "installer")


def ui_bytes(n):
    from retroboot.panel import ui
    return ui.human_bytes(n)


class BoardTests(unittest.TestCase):
    def board(self, plat, setting="auto"):
        class App:
            pass
        app = App()
        app.cfg = type("C", (), {"installer_board": setting})()
        app.platform = plat
        return inst.InstallerPanel.board.fget(type("P", (), {"app": app})())

    def test_auto(self):
        pi = hostplatform.Platform(family="raspberrypi", board="pi4", machine="aarch64")
        self.assertEqual(self.board(pi), "rpi")
        self.assertEqual(self.board(hostplatform.Platform(family="other", machine="aarch64")), "rpi")   # QEMU virt
        self.assertEqual(self.board(hostplatform.Platform(family="pc", machine="x86_64")), "x86")
        self.assertEqual(self.board(hostplatform.Platform(family="pc", machine="x86_64"), "rpi"), "rpi")
        self.assertEqual(self.board(pi, "x86"), "x86")

    def test_room_for_macs(self):
        self.assertEqual(room_for_macs({"size_bytes": 40 * GiB}), 40 * GiB - FIXED_BYTES - MARGIN)
        self.assertEqual(room_for_macs({"size_bytes": 10 * GiB, "fixed_bytes": inst.PI_FREE_FIXED_BYTES}),
                         10 * GiB - inst.PI_FREE_FIXED_BYTES - MARGIN)
        self.assertEqual(room_for_macs({"size_bytes": 1 * GiB, "fixed_bytes": inst.PI_FREE_FIXED_BYTES}), 0)

    def test_constants_match_the_image(self):
        """hostd's copies of rpi_layout's numbers and marker (the installer image)."""
        path = REPO / "image/rootfs/usr/lib/retroboot/installer/rpi_layout.py"
        loader = importlib.machinery.SourceFileLoader("rpi_layout_hostd", str(path))
        spec = importlib.util.spec_from_loader("rpi_layout_hostd", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        self.assertEqual(mod.FREE_FIXED_BYTES, inst.PI_FREE_FIXED_BYTES)
        self.assertEqual(mod.CONFIG_MARK, rpiboot.CONFIG_MARK)


def card(root: Path, installed=True, tryboot=True, live=True) -> Path:
    boot = root / "boot-firmware"
    (boot / "retroboot/os").mkdir(parents=True)
    base = (REPO / "image/rpi/boot/config.txt").read_text()
    installer_cfg = base                                          # the universal image's own config.txt
    (boot / "config.txt").write_text((rpiboot.CONFIG_MARK + "\n[all]\nos_prefix=retroboot/os/\n" + base)
                                     if installed else installer_cfg)
    if tryboot:
        (boot / "tryboot.txt").write_text(installer_cfg)
    (boot / "cmdline.txt").write_text("console=serial0,115200 quiet " +
                                      ("boot=retroboot-live rb.live=.retroboot-installer-1.1.0-B1 " if live else
                                       "root=PARTUUID=1-02 ") + "retroboot.mode=installer\n")
    (boot / "retroboot/os/cmdline.txt").write_text("root=PARTUUID=aa-03 quiet\n")
    return boot


class RpiBootTests(unittest.TestCase):
    def test_installer_beside(self):
        td, root = helpers.scratch()
        self.addCleanup(td.cleanup)
        info = rpiboot.installer_beside(card(root / "a"))
        self.assertEqual(info["build"], "1.1.0-B1")
        self.assertTrue(info["cmdline"].endswith("boot-firmware/cmdline.txt"))
        self.assertIsNone(rpiboot.installer_beside(card(root / "b", installed=False)))   # the installer itself
        self.assertIsNone(rpiboot.installer_beside(card(root / "c", tryboot=False)))
        self.assertIsNone(rpiboot.installer_beside(card(root / "d", live=False)))       # tryboot.txt of something else
        self.assertIsNone(rpiboot.installer_beside(root / "missing"))
        boot = card(root / "e")
        (boot / "tryboot.txt").write_text("[all]\nos_prefix=inst/\ncmdline=c.txt\n")
        (boot / "inst").mkdir()
        (boot / "inst/c.txt").write_text("boot=retroboot-live rb.live=.retroboot-installer-X\n")
        self.assertEqual(rpiboot.installer_beside(boot)["build"], "X")


class StartInstallerTests(unittest.IsolatedAsyncioTestCase):
    installed = True

    async def asyncSetUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        helpers.make_package(self.root)
        helpers.make_device_tree(self.root, *PI4)
        boot = card(self.root, installed=self.installed)
        self.cfg = helpers.make_config(self.root, pi_boot_dir=str(boot))
        paths = Paths(self.root)
        paths.ensure()
        self.fp = FakePresenter(str(paths.presenter_sock), None)
        await self.fp.start()
        self.app = Hostd(self.cfg)
        self.app.settings.store.load()
        helpers.install_fake_prep(self.app)
        await self.app.start()

    async def asyncTearDown(self):
        await self.app.shutdown()
        await self.fp.stop()

    def button_ids(self):
        """Buttons and the RetroBoot window's panes (Start Installer is a row of its list)."""
        out = []

        def walk(items):
            for i in items:
                if i.get("type") in ("buttons", "list"):
                    out.extend(b["id"] for b in i["items"])
                if i.get("items") and i.get("type") not in ("buttons", "list"):
                    walk(i["items"])
        walk(self.fp.doc.get("items") or [])
        return out

    async def test_start_installer_restarts_with_tryboot(self):
        await helpers.wait_for(lambda: (self.fp.doc or {}).get("id"), 10, what="first window")
        await self.app.panel.show_recovery()
        await helpers.wait_for(lambda: (self.fp.doc or {}).get("id") == "recovery-main", 5, what="recovery")
        self.assertIn("installer", self.button_ids())
        self.fp.click("installer")
        await helpers.wait_for(lambda: (self.fp.doc or {}).get("id") == "start-installer", 5, what="alert")
        self.assertIn("restarts once into the installer on this card", json.dumps(self.fp.doc, ensure_ascii=False))
        self.fp.click("cancel")
        await helpers.wait_for(lambda: (self.fp.doc or {}).get("id") == "recovery-main", 5, what="back")
        self.fp.click("installer")
        await helpers.wait_for(lambda: (self.fp.doc or {}).get("id") == "start-installer", 5, what="alert")
        self.fp.click("restart")
        await helpers.wait_for(lambda: helpers.events(self.app, "host_reboot"), 10, what="reboot")
        self.assertEqual(helpers.events(self.app, "host_reboot")[0]["arg"], "0 tryboot")
        self.assertEqual(helpers.events(self.app, "installer_tryboot")[0]["build"], "1.1.0-B1")


class NoInstallerOnCardTests(StartInstallerTests):
    installed = False

    async def test_start_installer_restarts_with_tryboot(self):
        await helpers.wait_for(lambda: (self.fp.doc or {}).get("id"), 10, what="first window")
        await self.app.panel.show_recovery()
        await helpers.wait_for(lambda: (self.fp.doc or {}).get("id") == "recovery-main", 5, what="recovery")
        self.assertNotIn("installer", self.button_ids())


class RebootArgTests(unittest.IsolatedAsyncioTestCase):
    async def test_reboot_command_gets_the_argument(self):
        td, root = helpers.scratch()
        self.addCleanup(td.cleanup)
        helpers.make_package(root)
        rec = root / "reboot.txt"
        script = root / "fake-reboot"
        script.write_text(f"#!/bin/sh\nprintf '%s|' \"$@\" > {rec}\n")
        script.chmod(0o755)
        cfg = helpers.make_config(root, reboot_cmd=[str(script)])
        app = Hostd(cfg)
        app.loop = asyncio.get_running_loop()

        async def scene(*a, **k):
            return None
        app.show_scene = scene
        self.assertTrue(await app.supervisor.reboot_host("test", arg="0 tryboot"))
        # systemd 257 refuses a positional reboot argument ("Too many arguments")
        self.assertEqual(rec.read_text(), "--reboot-argument=0 tryboot|")
        await app.supervisor.reboot_host("test")
        self.assertEqual(rec.read_text(), "|")
        script.write_text("#!/bin/sh\necho 'Too many arguments.' >&2\nexit 1\n")
        self.assertFalse(await app.supervisor.reboot_host("test", arg="0 tryboot"))
        self.assertEqual(helpers.events(app, "host_reboot_failed")[-1]["rc"], 1)


if __name__ == "__main__":
    unittest.main()
