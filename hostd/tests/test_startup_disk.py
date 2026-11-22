"""Startup Disk (1.6.0): the chosen Mac shown while it waits for Mac OS's restart, New Blank
Disk, Delete, this computer's CD-ROM drive (a USB CD-ROM drive) and Start Up from CD.

Unit tests for what a disk holds (startup_volume), blank disks, drive discovery and the
QEMU arguments; then the panel and the CD start through the real supervisor, guest-launcher
and fake QEMU (test_lifecycle.Base); then the web API."""
import json
import os
import struct
import types
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

import helpers
from retroboot import schema
from retroboot.config import Paths
from retroboot.machines import (LaunchOptions, MachineError, MachineRegistry, build_argv, startup_volume)
from retroboot.optical import OpticalDrives
from retroboot.panel.controller import (NeedsCD, NewBlankDisk, RestartForMac, StartFromCD, StartupDisk)
from test_lifecycle import Base
from test_media import write_cd

MiB = 1024 * 1024


def write_iso(path: Path, name: bytes = b"MACOS_921", size: int = 6 * MiB) -> Path:
    """An ISO 9660 CD image (volume identifier ``name``)."""
    with open(path, "wb") as f:
        f.truncate(size)
        f.seek(0x8000)
        f.write(b"\x01CD001\x01\x00" + b"LINUX".ljust(32) + name.ljust(32))
    return path


def write_mac_disk(path: Path, blessed: bool, wrapper: bool = True, size: int = 64 * MiB) -> Path:
    """A disk as Drive Setup leaves it: an Apple partition map and an Apple_HFS partition
    holding an HFS+ volume (inside an HFS wrapper, as Mac OS 9 makes them), with or
    without a blessed System Folder (finderInfo[0])."""
    start = 96                      # partition start, in 512-byte blocks
    with open(path, "r+b" if path.exists() else "wb") as f:
        f.truncate(size)
        f.seek(0)
        f.write(b"ER" + struct.pack(">HI", 512, size // 512))
        for i, (ptype, s, n) in enumerate(((b"Apple_partition_map", 1, 63), (b"Apple_Driver_ATA", 64, 32),
                                           (b"Apple_HFS", start, size // 512 - start)), start=1):
            f.seek(512 * i)
            f.write(b"PM\0\0" + struct.pack(">III", 3, s, n) + b"x".ljust(32, b"\0") + ptype.ljust(32, b"\0"))
        vh = bytearray(512)
        vh[0:2] = b"H+"
        vh[0x28:0x2C] = struct.pack(">I", 4096)
        if blessed:
            vh[0x50:0x54] = struct.pack(">I", 18)        # the System Folder's catalog node id
        part = start * 512
        if wrapper:
            mdb = bytearray(512)
            mdb[0:2] = b"BD"
            mdb[0x14:0x18] = struct.pack(">I", 4096)     # drAlBlkSiz
            mdb[0x1C:0x1E] = struct.pack(">H", 8)        # drAlBlSt (512-byte blocks)
            mdb[0x7C:0x7E] = b"H+"                        # drEmbedSigWord
            mdb[0x7E:0x82] = struct.pack(">HH", 2, 100)  # drEmbedExtent: start 2, count 100 blocks
            f.seek(part + 1024)
            f.write(bytes(mdb))
            f.seek(part + 8 * 512 + 2 * 4096 + 1024)
        else:
            f.seek(part + 1024)
        f.write(bytes(vh))
    return path


class WhatADiskHolds(unittest.TestCase):
    def setUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)

    def test_empty_volume_and_system(self):
        d = self.root / "disk.img"
        with open(d, "wb") as f:
            f.truncate(32 * MiB)
        self.assertEqual(startup_volume(d), "empty")
        write_mac_disk(d, blessed=False)
        self.assertEqual(startup_volume(d), "volume")
        write_mac_disk(d, blessed=True)
        self.assertEqual(startup_volume(d), "system")
        plain = write_mac_disk(self.root / "plain.img", blessed=True, wrapper=False)
        self.assertEqual(startup_volume(plain), "system")
        self.assertEqual(startup_volume(self.root / "missing.img"), "empty")

    def test_bare_hfs_volume(self):
        d = self.root / "hfs.img"
        with open(d, "wb") as f:
            f.truncate(8 * MiB)
            f.seek(1024)
            mdb = bytearray(512)
            mdb[0:2] = b"BD"
            f.write(bytes(mdb))
        self.assertEqual(startup_volume(d), "volume")
        with open(d, "r+b") as f:
            f.seek(1024 + 0x5C)
            f.write(struct.pack(">I", 99))
        self.assertEqual(startup_volume(d), "system")


class BlankDisks(unittest.TestCase):
    def setUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        self.paths = Paths(self.root)
        self.paths.ensure()
        self.reg = MachineRegistry(self.paths)

    def test_create_blank(self):
        m = self.reg.create_blank("  My\tNew   Mac\x07 ", 2 * 1024 ** 3)
        self.assertEqual((m.id, m.name), ("my-new-mac", "My New Mac"))
        st = m.disk.stat()
        self.assertEqual(st.st_size, 2 * 1024 ** 3)
        self.assertLess(st.st_blocks * 512, MiB)          # sparse
        self.assertTrue(m.blank)
        self.assertTrue(m.needs_cd())
        self.assertEqual(m.meta["source"], {"type": "blank", "size_bytes": 2 * 1024 ** 3})
        self.assertIsNone(m.meta["os_version"])
        self.assertEqual(m.state(), {"clean": True, "last_exit": None})
        pub = m.public()
        self.assertEqual((pub["blank"], pub["startup_volume"]), (True, "empty"))
        again = self.reg.create_blank("My New Mac", 1024 ** 3)
        self.assertEqual(again.id, "my-new-mac-2")
        self.assertFalse([p for p in os.listdir(self.paths.machines) if p.startswith(".")])
        # Mac OS installed from the CD: the disk now has a blessed System Folder.
        write_mac_disk(m.disk, blessed=True, size=2 * 1024 ** 3)
        self.assertFalse(m.needs_cd())
        self.assertEqual(m.public()["startup_volume"], "system")

    def test_refusals(self):
        for name, size in (("", 2 * 1024 ** 3), ("  ", 2 * 1024 ** 3), (None, 2 * 1024 ** 3),
                           ("Mac", 512 * MiB), ("Mac", 129 * 1024 ** 3), ("Mac", 2 * 1024 ** 3 + 7), ("Mac", 2.5)):
            with self.subTest(name=name, size=size), self.assertRaises(MachineError):
                self.reg.create_blank(name, size)
        self.assertEqual(self.reg.list(), [])

    def test_imported_mac_is_not_blank(self):
        helpers.make_machine(self.root)
        m = self.reg.get("mac-os-9")
        self.assertFalse(m.blank)
        self.assertFalse(m.needs_cd())          # never auto-starts from a CD, whatever its disk


class Drives(unittest.TestCase):
    """optical.OpticalDrives on a fake /sys/block and /dev (regular files stand in for discs)."""

    def setUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        sysb = self.root / "sys/block"
        usbdev = self.root / "sys/devices/pci0000:00/0000:00:14.0/usb2/2-1/2-1:1.0/host6/target6:0:0/6:0:0:0"
        (usbdev / "block/sr0").mkdir(parents=True)
        (usbdev / "vendor").write_text("HL-DT-ST\n")
        (usbdev / "model").write_text("DVDRAM GP57EB40 \n")
        (usbdev / "block/sr0/device").symlink_to(usbdev)
        sata = self.root / "sys/devices/pci0000:00/0000:00:17.0/ata1/host0/target0:0:0/0:0:0:0/block/sr1"
        sata.mkdir(parents=True)
        sysb.mkdir(parents=True)
        (sysb / "sr0").symlink_to(usbdev / "block/sr0")
        (sysb / "sr1").symlink_to(sata)
        (sysb / "sda").mkdir()
        self.dev = self.root / "dev"
        self.dev.mkdir()
        self.drives = OpticalDrives(self.root / "sys", self.dev, ttl=0)

    def test_discs_and_labels(self):
        write_iso(self.dev / "sr0", b"MACOS_921")
        (self.dev / "sr1").write_bytes(b"")              # an empty drive
        got = self.drives.list()
        self.assertEqual([d["id"] for d in got], ["sr0", "sr1"])
        usb, sata = got
        self.assertEqual((usb["usb"], usb["kind"], usb["disc"], usb["volume_name"]),
                         (True, "USB CD-ROM drive", True, "MACOS_921"))
        self.assertEqual(usb["product"], "HL-DT-ST DVDRAM GP57EB40")
        self.assertEqual(usb["label"], "MACOS_921 (USB CD-ROM drive)")
        self.assertEqual((sata["usb"], sata["kind"], sata["disc"], sata["status"]),
                         (False, "CD-ROM drive", False, "empty"))
        self.assertEqual(self.drives.first_disc()["id"], "sr0")
        write_cd(self.dev / "sr1", b"Mac OS 9.2.1")      # a Mac OS CD: its HFS volume's name
        self.assertEqual(self.drives.list()[1]["volume_name"], "Mac OS 9.2.1")
        (self.dev / "sr0").unlink()
        self.assertEqual(self.drives.list()[0]["status"], "unavailable")
        self.assertEqual(self.drives.first_disc()["id"], "sr1")

    def test_no_drives(self):
        d = OpticalDrives(self.root / "nowhere", self.dev)
        self.assertEqual((d.present(), d.list(), d.first_disc()), (False, [], None))


class Arguments(unittest.TestCase):
    def setUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        self.pkg = helpers.make_package(self.root)
        helpers.make_machine(self.root)
        self.paths = Paths(self.root)
        self.paths.ensure()
        self.m = MachineRegistry(self.paths).get("mac-os-9")
        self.settings = schema.defaults()
        self.drive = self.root / "dev/sr0"
        self.drive.parent.mkdir()
        write_iso(self.drive)
        self.image = write_iso(self.root / "install.iso")

    def drives(self, opts):
        a = build_argv(self.m, self.settings, self.paths, None, opts)
        return [a[i + 1] for i, v in enumerate(a) if v == "-drive"], a

    def test_disc_in_the_drive_beside_mac_os(self):
        d, a = self.drives(LaunchOptions(cd_drive=self.drive))
        self.assertIn(f"if=ide,index=2,media=cdrom,id=cd1,readonly=on,file.driver=host_cdrom,"
                      f"file.filename={self.drive},format=raw", d)
        self.assertIn("if=ide,index=3,media=cdrom,id=cd0,readonly=on", d)
        self.assertNotIn("-boot", a)
        self.assertIn("macio-ide.pio-status-drain=on", a)

    def test_start_up_from_the_drive(self):
        d, a = self.drives(LaunchOptions(boot_cd=self.drive, boot_cd_drive=True, cd_drive=self.drive))
        self.assertIn(f"if=ide,index=2,media=cdrom,id=cd0,readonly=on,file.driver=host_cdrom,"
                      f"file.filename={self.drive},format=raw", d)
        self.assertIn("if=ide,index=3,media=cdrom,id=cd1,readonly=on", d)    # not twice
        self.assertEqual(a[a.index("-boot") + 1], "d")
        self.assertIn("macio-ide.dma-completion-delay-ns=1000000", a)
        self.assertIn(f"file={self.m.disk.resolve()},format=raw,media=disk,index=0", " ".join(a))

    def test_start_up_from_an_image_with_a_disc_in_the_drive(self):
        d, a = self.drives(LaunchOptions(boot_cd=self.image, cd_drive=self.drive))
        self.assertIn(f"if=ide,index=2,media=cdrom,id=cd0,readonly=on,file={self.image.resolve()},format=raw", d)
        self.assertIn(f"if=ide,index=3,media=cdrom,id=cd1,readonly=on,file.driver=host_cdrom,"
                      f"file.filename={self.drive},format=raw", d)

    def test_missing_drive_refused(self):
        with self.assertRaises(MachineError):
            build_argv(self.m, self.settings, self.paths, None, LaunchOptions(cd_drive=self.root / "dev/sr9"))


def fake_drive(test, iso_name: bytes | None = b"MACOS_921") -> Path:
    """A USB CD-ROM drive under the test root's sysfs/dev (cfg.sysfs_path, cfg.dev_path)."""
    usbdev = test.root / "sys/devices/pci0000:00/usb1/1-1/1-1:1.0/host2/target2:0:0/2:0:0:0"
    (usbdev / "block/sr0").mkdir(parents=True, exist_ok=True)
    (test.root / "sys/block").mkdir(parents=True, exist_ok=True)
    link = test.root / "sys/block/sr0"
    if not link.is_symlink():
        link.symlink_to(usbdev / "block/sr0")
    dev = test.root / "dev"
    dev.mkdir(exist_ok=True)
    if iso_name is None:
        (dev / "sr0").write_bytes(b"")
    else:
        write_iso(dev / "sr0", iso_name)
    test.app.optical.dev = dev
    return dev / "sr0"


def argv_of(app) -> list[str]:
    return json.loads(app.paths.argv_json.read_text())["argv"]


class StartupDiskPanel(Base):
    """The page no longer forgets a choice made while Mac OS runs (the Gemini report)."""
    modes = ["normal", "normal"]

    def before_start(self):
        helpers.make_machine(self.root, "superdeluxe", "SuperDeluxe v2")

    async def test_choice_waits_for_restart_and_is_shown(self):
        await self.running()
        await self.until_event("finder_detected")
        p = self.app.panel
        await p.show(StartupDisk(p, back=p.show_recovery))
        await self.until_doc("startup-disk")
        self.assertEqual(self.fp.doc["width"], 540)
        self.fp.change("machine", "superdeluxe")
        await helpers.wait_for(lambda: not self.find(self.fp.doc, "use").get("disabled"), what="Use enabled")
        self.fp.click("use")
        await self.until_doc("restart-for-mac")
        self.assertIn("“SuperDeluxe v2” starts the next time Mac OS restarts.", json.dumps(self.fp.doc,
                                                                                           ensure_ascii=False))
        self.fp.click("later")
        await self.until_doc("startup-disk")
        doc = self.fp.doc
        lst = self.find(doc, "machine")
        # The selection is the Mac chosen to start next, not the running one.
        self.assertEqual(lst["value"], "superdeluxe")
        rows = {r["id"]: r.get("detail") for r in lst["items"]}
        self.assertIn("Running", rows["mac-os-9"])
        self.assertIn("Starts next", rows["superdeluxe"])
        # The startup Mac carries the check mark; every row keeps the check-mark column.
        self.assertEqual({r["id"]: r.get("checked") for r in lst["items"]}["superdeluxe"], True)
        self.assertTrue(all("checked" in r for r in lst["items"]))
        self.assertTrue(self.find(doc, "use").get("disabled"))
        self.assertIsNotNone(self.find(doc, "restart"))
        self.assertTrue(self.find(doc, "delete").get("disabled"))       # chosen to start next
        s = self.app.settings
        self.assertEqual((s.desired["machine.active"], s.applied["machine.active"]), ("superdeluxe", "mac-os-9"))
        # Reopening the page (Done, then Startup Disk again) still shows the choice.
        await p.show(StartupDisk(p, back=p.show_recovery))
        await self.until_doc("startup-disk")
        self.assertEqual(self.find(self.fp.doc, "machine")["value"], "superdeluxe")
        self.fp.click("restart")
        await self.running(2, 30)
        self.assertEqual(s.applied["machine.active"], "superdeluxe")
        self.assertTrue(any("superdeluxe" in a for a in argv_of(self.app)))

    @staticmethod
    def find(doc, wid):
        found = {}

        def walk(o):
            if isinstance(o, dict):
                if o.get("id") == wid and ("type" in o or "label" in o):
                    found.setdefault("w", o)
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        walk(doc.get("items", []))
        return found.get("w")


class NewBlankAndDelete(Base):
    async def test_new_blank_disk_then_delete_it(self):
        await self.running()
        p = self.app.panel
        disk = StartupDisk(p, back=p.show_recovery)
        await p.show(NewBlankDisk(p, disk))
        await self.until_doc("new-blank-disk")
        self.fp.change("size", "4")
        self.fp.click("create", {"name": "Install Test"})
        await self.until_doc("message")
        self.assertIn("“Install Test” is ready.", json.dumps(self.fp.doc, ensure_ascii=False))
        m = self.app.machines.get("install-test")
        self.assertEqual((m.disk.stat().st_size, m.blank), (4 * 1024 ** 3, True))
        self.assertEqual(self.ev("machine_created")[0]["size_gb"], 4)
        self.fp.click("ok")
        await self.until_doc("startup-disk")
        doc = json.dumps(self.fp.doc, ensure_ascii=False)
        self.assertIn("No Mac OS yet · 4.0 GB disk", doc)
        self.assertEqual(StartupDiskPanel.find(self.fp.doc, "machine")["value"], "install-test")
        self.fp.click("delete")
        await self.until_doc("confirm-delete")
        self.fp.click("delete")
        await self.until_doc("message")
        self.assertIn("was deleted", json.dumps(self.fp.doc))
        self.assertIsNone(self.app.machines.get("install-test"))
        self.assertEqual(self.ev("machine_removed")[0]["origin"], "panel")

    async def test_name_required(self):
        await self.running()
        p = self.app.panel
        await p.show(NewBlankDisk(p, StartupDisk(p, back=p.show_recovery)))
        await self.until_doc("new-blank-disk")
        self.fp.click("create", {"name": "   "})
        await helpers.wait_for(lambda: "Type a name" in json.dumps(self.fp.doc), what="error")
        self.assertEqual([m.id for m in self.app.machines.list()], ["mac-os-9"])


class BlankDiskWithoutCd(Base):
    """A blank disk without Mac OS and no CD: no firmware prompt, a panel that says what to do."""
    machine = False

    def before_start(self):
        m = self.app.machines.create_blank("Empty", 2 * 1024 ** 3)
        self.app.settings.set_desired_and_applied(
            {"machine.active": m.id}, schema.ValidationContext(machines={m.id}, check_capabilities=False))

    async def test_refused_with_a_way_forward(self):
        await self.until_doc("needs-cd")
        self.assertIn("There's no Mac OS on “Empty” yet.", json.dumps(self.fp.doc, ensure_ascii=False))
        self.assertEqual(self.ev("guest_start"), [])
        self.assertEqual(self.prep.calls, [])                     # nothing prepared on an empty disk
        self.fp.click("disk")
        await self.until_doc("startup-disk")
        job = await self.job(self.app.lifecycle_job("start", origin="test"))
        self.assertEqual(job.state, "failed")
        self.assertEqual(job.error["code"], "needs_cd")


class InstallFromTheUsbDrive(Base):
    """New Blank Disk → a Mac OS CD in the USB CD-ROM drive → the disk starts up from it
    (-boot d, host_cdrom) until Mac OS is on it, then from its own disk, prepared once."""
    machine = False
    modes = ["normal", "normal", "normal"]

    def before_start(self):
        self.drive = fake_drive(self)
        m = self.app.machines.create_blank("New Mac", 8 * 1024 ** 3)
        self.mid = m.id
        self.app.settings.set_desired_and_applied(
            {"machine.active": m.id}, schema.ValidationContext(machines={m.id}, check_capabilities=False))

    async def test_install_then_start_from_disk(self):
        await self.running()
        a = argv_of(self.app)
        self.assertEqual(a[a.index("-boot") + 1], "d")
        self.assertIn(f"if=ide,index=2,media=cdrom,id=cd0,readonly=on,file.driver=host_cdrom,"
                      f"file.filename={self.drive},format=raw", a)
        start = self.ev("guest_cd_start")[0]
        self.assertEqual((start["why"], start["cd"], start["drive"]), ("no-system", "drive:sr0", True))
        self.assertEqual(self.prep.calls, [])
        self.assertEqual(self.ev("guest_start")[0]["start_cd"], "drive")
        # Mac OS's installer initialized the disk and installed Mac OS; Mac OS restarts.
        m = self.app.machines.get(self.mid)
        write_mac_disk(m.disk, blessed=True, size=8 * 1024 ** 3)
        job = await self.job(self.app.lifecycle_job("restart", origin="test"), 60)
        self.assertEqual(job.state, "succeeded", job.public())
        await self.running(2, 30)
        a = argv_of(self.app)
        self.assertNotIn("-boot", a)
        # The disc stays available to Mac OS in the second drive.
        self.assertIn(f"if=ide,index=2,media=cdrom,id=cd1,readonly=on,file.driver=host_cdrom,"
                      f"file.filename={self.drive},format=raw", a)
        self.assertEqual([c[0] for c in self.prep.calls], ["apply"])
        self.assertEqual(len(self.ev("guest_cd_start")), 1)


class StartUpFromCdImage(Base):
    """Start Up from CD on an installed Mac: once, from a library CD image, Mac OS restarting."""
    modes = ["normal", "normal", "normal"]

    def before_start(self):
        write_iso(self.app.paths.media_library / "MacOS921.iso")

    async def test_once_then_disk_again(self):
        await self.running()
        await self.until_event("finder_detected")
        p = self.app.panel
        disk = StartupDisk(p, back=p.show_recovery)
        screen = StartFromCD(p, "mac-os-9", disk)
        await screen._load()
        await p.show(screen)
        await self.until_doc("start-from-cd")
        doc = json.dumps(self.fp.doc)
        self.assertIn("MacOS921.iso", doc)
        self.assertIn('"Restart"', doc)
        self.fp.click("start")
        await self.running(2, 40)
        a = argv_of(self.app)
        iso = (self.app.paths.media_library / "MacOS921.iso").resolve()
        self.assertIn(f"if=ide,index=2,media=cdrom,id=cd0,readonly=on,file={iso},format=raw", a)
        self.assertEqual(a[a.index("-boot") + 1], "d")
        self.assertEqual(self.ev("cd_start_request")[0]["cd"], "MacOS921.iso")
        self.assertNotIn("cd_start", self.app.machines.get("mac-os-9").meta)    # not a blank disk
        await self.until_event("finder_detected", 2)
        job = await self.job(self.app.lifecycle_job("restart", origin="test"), 60)
        self.assertEqual(job.state, "succeeded", job.public())
        await self.running(3, 30)
        self.assertNotIn("-boot", argv_of(self.app))                 # once only


class WebApi(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import asyncio
        from retroboot.hostd import Hostd
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        helpers.make_package(self.root)
        helpers.make_machine(self.root)
        self.app = Hostd(helpers.make_config(self.root))
        self.app.loop = asyncio.get_running_loop()
        self.app.settings.store.load()
        self.app.settings.set_desired_and_applied(
            {"machine.active": "mac-os-9"},
            schema.ValidationContext(machines=self.app.machines.ids(), check_capabilities=False))
        fake_drive(self, b"MAC OS 9")
        self.app.web.tls()
        self.client = TestClient(TestServer(self.app.web.web_app, host="127.0.0.1"))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        code, _ = self.app.pairing.current_code()
        r = await self.client.post("/api/v1/pair", json={"code": code, "device_name": "Test"})
        self.cookie = r.headers.getall("Set-Cookie")[0].split("rb_session=", 1)[1].split(";", 1)[0]
        self.csrf = (await r.json())["csrf"]

    def h(self):
        return {"Cookie": f"rb_session={self.cookie}", "X-RetroBoot-CSRF": self.csrf}

    async def test_blank_and_drives(self):
        r = await self.client.post("/api/v1/machines/blank", json={"name": "Web Mac", "size_gb": 16},
                                   headers=self.h())
        body = await r.json()
        self.assertEqual(r.status, 201, body)
        self.assertEqual((body["machine"]["id"], body["machine"]["blank"]), ("web-mac", True))
        r = await self.client.post("/api/v1/machines/blank", json={"name": "Web Mac", "size_gb": 3},
                                   headers=self.h())
        self.assertEqual(r.status, 422)
        r = await self.client.post("/api/v1/machines/blank", json={"name": "X", "size_gb": 2, "path": "/"},
                                   headers=self.h())
        self.assertEqual(r.status, 422)
        r = await self.client.post("/api/v1/machines/blank", json={"name": "No CSRF", "size_gb": 2},
                                   headers={"Cookie": f"rb_session={self.cookie}"})
        self.assertEqual(r.status, 403)
        r = await self.client.get("/api/v1/machines", headers=self.h())
        body = await r.json()
        web_mac = next(m for m in body["machines"] if m["id"] == "web-mac")
        self.assertEqual(web_mac["startup_volume"], "empty")
        self.assertEqual(body["blank_sizes_gb"], [2, 4, 8, 16, 32, 64])
        self.assertEqual(body["cd_drives"][0]["volume_name"], "MAC OS 9")
        self.assertEqual(body["cds"][0]["id"], "drive:sr0")
        self.assertEqual([m.id for m in self.app.machines.list()], ["mac-os-9", "web-mac"])

    async def test_start_cd_validation(self):
        r = await self.client.post("/api/v1/machines/nope/start-cd", json={}, headers=self.h())
        self.assertEqual(r.status, 404)
        r = await self.client.post("/api/v1/machines/mac-os-9/start-cd", json={"source": 3}, headers=self.h())
        self.assertEqual(r.status, 422)
