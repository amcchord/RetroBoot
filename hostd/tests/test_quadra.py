"""68k Macs on an emulated Quadra 800 (quadra.py, D044, LEGACY-01/02).

Every test uses helpers.TEST_ROM, RetroBoot's own bytes standing in for the Quadra 800 ROM
(quadra.py accepts it only inside ``helpers.test_rom()``): no Apple ROM is in this tree.
Unit tests of the family model, the catalog, the ROM store, PRAM and the q800 arguments;
then the lifecycle through the real supervisor, guest-launcher and the fake QEMU as
qemu-system-m68k (test_lifecycle.Base); then the panel and the web API."""
import asyncio
import gzip
import io
import json
import os
import tarfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

import helpers
from retroboot import catalogs, guestprep, quadra, schema
from retroboot.catalog import ExtractLimits, ImportError_, install_machine, sanitize_template_config, validate_catalog
from retroboot.config import Paths
from retroboot.machines import (BLANK_SIZES_GB, PRAM_FILE, QUADRA800, ROM_FILE, LaunchOptions, MachineError,
                                MachineRegistry, MachineUnavailable, build_argv, family_fields, family_of_template,
                                launch_spec, validate_meta)
from test_lifecycle import Base

MiB = 1024 * 1024


def settings(**over) -> dict:
    s = schema.defaults()
    s.update(over)
    return s


class WithRom(unittest.TestCase):
    def setUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        self.rom_patch = p = helpers.test_rom()
        p.start()
        self.addCleanup(p.stop)
        self.paths = Paths(self.root)
        self.paths.ensure()
        self.reg = MachineRegistry(self.paths)


# --- the family model -------------------------------------------------------------------
class Family(WithRom):
    def test_machine_json_family(self):
        base = {"schema_version": 1, "id": "m", "disk": "disk.img"}
        self.assertEqual(validate_meta(dict(base), "m"), base)                         # a Power Mac, as before
        validate_meta(dict(base, family="powermac"), "m")
        validate_meta(dict(base, family="quadra800", rom="Quadra800.rom", pram="pram.img"), "m")
        for bad in ({"family": "powerMac7500"}, {"family": 3}, {"rom": "Quadra800.rom"},          # ROM on a Power Mac
                    {"family": "quadra800", "rom": "../rom"}, {"family": "quadra800", "pram": "disk.img"},
                    {"family": "quadra800", "rom": ".hidden"}):
            with self.assertRaises(MachineError, msg=bad):
                validate_meta(dict(base, **bad), "m")

    def test_registry(self):
        helpers.make_machine(self.root)
        helpers.make_quadra_machine(self.root)
        m9, m8 = self.reg.get("mac-os-9"), self.reg.get("mac-os-8-1")
        self.assertEqual((m9.family, m9.quadra, m8.family, m8.quadra), ("powermac", False, "quadra800", True))
        self.assertEqual((m8.rom.name, m8.pram.name), ("Quadra800.rom", "pram.img"))
        self.assertEqual((m9.public()["family"], m8.public()["family_label"]), ("powermac", "68k"))
        bad = self.root / "var/lib/retroboot/machines/copland"
        helpers.make_machine(self.root, "copland", "Copland")
        meta = json.loads((bad / "machine.json").read_text())
        (bad / "machine.json").write_text(json.dumps(dict(meta, family="powerMac7500")))
        self.assertIsNone(self.reg.get("copland"))          # a kind of Mac this RetroBoot can't run

    def test_templates(self):
        self.assertEqual(family_of_template({}), "powermac")
        self.assertEqual(family_of_template({"machineFamily": "powerMacG4"}), "powermac")
        self.assertEqual(family_of_template({"machineFamily": "quadra800"}), "quadra800")
        self.assertIsNone(family_of_template({"machineFamily": "powerMac7500"}))
        self.assertEqual(family_fields({"machineFamily": "powerMacG4"}), {})
        self.assertEqual(family_fields({"machineFamily": "quadra800"}),
                         {"schema_version": 2, "family": "quadra800", "rom": "Quadra800.rom", "pram": "pram.img"})


# --- the catalog and archives ---------------------------------------------------------------
def entry(eid: str, family: str | None) -> dict:
    e = {"id": eid, "name": eid, "summary": "A Mac.", "osVersion": "8.1", "archiveURL": f"https://x.test/{eid}.tar.gz",
         "archiveBytes": 100, "installedBytes": 1024, "sha256": "ab" * 32}
    if family:
        e["machineFamily"] = family
    return e


def archive(path: Path, members: dict) -> Path:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as tf:
        for name, data in members.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    path.write_bytes(gzip.compress(buf.getvalue()))
    return path


class Catalog(WithRom):
    def test_templates_and_entries(self):
        self.assertEqual(sanitize_template_config(b'{"machineFamily": "quadra800", "ramMB": 128}')["machineFamily"],
                         "quadra800")
        with self.assertRaises(ImportError_) as cm:
            sanitize_template_config(b'{"machineFamily": "powerMac7500"}')
        self.assertEqual(cm.exception.code, "unsupported")
        entries = validate_catalog({"schemaVersion": 1, "machines": [
            entry("os81", "quadra800"), entry("os922", "powerMacG4"), entry("old", None), entry("copland", "powerMac7500")]})
        self.assertEqual([(e["id"], e.get("machineFamily")) for e in entries],
                         [("os81", "quadra800"), ("os922", "powerMacG4"), ("old", None)])   # Copland left out
        self.assertEqual([catalogs.entry_family(e) for e in entries], ["quadra800", "powermac", "powermac"])
        res = [("internet", entries)]
        self.assertEqual([e["id"] for e in catalogs.runnable(res, {"powermac"})[0][1]], ["os922", "old"])
        self.assertEqual(len(catalogs.runnable(res, {"powermac", "quadra800"})[0][1]), 3)
        self.assertEqual(catalogs.runnable_families(self.paths), {"powermac"})       # no 68k emulator here
        helpers.make_m68k_package(self.root)
        self.assertEqual(catalogs.runnable_families(self.paths), {"powermac", "quadra800"})

    async def _install(self, members: dict, name="Mac OS 8.1") -> str:
        a = archive(self.root / "a.tar.gz", members)
        return await install_machine(a, self.reg, name, "8.1", {"type": "upload"}, ExtractLimits(),
                                     lambda f, m: None)

    def install(self, members: dict, name="Mac OS 8.1") -> str:
        return asyncio.run(self._install(members, name))

    def test_quadra_archive_with_rom_and_pram(self):
        disk = bytes(MiB)
        cfg = json.dumps({"name": "Mac OS 8.1", "machineFamily": "quadra800", "ramMB": 128, "width": 800,
                          "height": 600, "depth": 8}).encode()
        mid = self.install({"config.json": cfg, "disk.img": disk, ROM_FILE: helpers.TEST_ROM, PRAM_FILE: bytes(256)})
        m = self.reg.get(mid)
        self.assertEqual((m.family, m.meta["rom"], m.meta["pram"]), ("quadra800", "Quadra800.rom", "pram.img"))
        self.assertEqual(m.rom.read_bytes(), helpers.TEST_ROM)
        self.assertEqual(m.meta["template"]["machineFamily"], "quadra800")

    def test_quadra_archive_without_rom(self):
        cfg = b'{"name": "Mac OS 7.5.5", "machineFamily": "quadra800"}'
        m = self.reg.get(self.install({"config.json": cfg, "disk.img": bytes(MiB)}, "Mac OS 7.5.5"))
        self.assertTrue(m.quadra)
        self.assertFalse(m.rom.exists())            # supplied from the ROM store at its first start

    def test_refusals(self):
        quadra_cfg = b'{"machineFamily": "quadra800"}'
        cases = [({"config.json": quadra_cfg, "disk.img": bytes(MiB), ROM_FILE: b"not a ROM" * 100}, "unsupported_rom"),
                 ({"config.json": b'{"machineFamily": "powerMacG4"}', "disk.img": bytes(MiB),
                   ROM_FILE: helpers.TEST_ROM}, "unsafe_archive"),          # a ROM in a Power Mac's archive
                 ({"config.json": quadra_cfg, "disk.img": bytes(MiB), PRAM_FILE: bytes(100)}, "unsafe_archive"),
                 ({"config.json": b'{"machineFamily": "powerMac7500"}', "disk.img": bytes(MiB)}, "unsupported")]
        for members, code in cases:
            with self.assertRaises(ImportError_, msg=code) as cm:
                self.install(members)
            self.assertEqual(cm.exception.code, code)
        self.assertEqual(self.reg.list(), [])


# --- the ROM store, PRAM ---------------------------------------------------------------------
class Roms(WithRom):
    def test_store(self):
        store = quadra.RomStore(self.paths.rom_store)
        self.assertFalse(store.available())
        self.assertIsNone(store.find())
        src = self.root / "q650.rom"
        src.write_bytes(helpers.TEST_ROM)
        info = store.add_file(src)
        self.assertEqual((info["sha256"], info["added"], info["label"]), (helpers.TEST_ROM_SHA256, True, "Quadra 800 ROM"))
        self.assertFalse(store.add_file(src)["added"])
        self.assertTrue(store.available())
        self.assertEqual(store.find().read_bytes(), helpers.TEST_ROM)
        self.assertEqual(store.path(helpers.TEST_ROM_SHA256).stat().st_mode & 0o777, 0o600)
        self.assertEqual([r["sha256"] for r in store.list()], [helpers.TEST_ROM_SHA256])
        self.assertNotIn("data", json.dumps(store.list()))
        other = self.root / "other.rom"
        other.write_bytes(helpers.TEST_ROM[:-1] + b"!")
        with self.assertRaises(quadra.RomError):
            store.add_file(other)
        with self.assertRaises(quadra.RomError):
            store.add_bytes(bytes(5 * MiB))
        self.assertTrue(store.add_bytes(helpers.TEST_ROM)["sha256"])
        # A stored file that no longer matches its name is never used.
        store.path(helpers.TEST_ROM_SHA256).write_bytes(b"x" * len(helpers.TEST_ROM))
        self.assertIsNone(store.find())

    def test_store_rechecks_bytes_when_file_stat_is_unchanged(self):
        store = quadra.RomStore(self.paths.rom_store)
        store.add_bytes(helpers.TEST_ROM)
        path = store.find()
        before = path.stat()
        path.write_bytes(b"x" * before.st_size)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = path.stat()
        self.assertEqual((after.st_size, after.st_mtime_ns, after.st_ino),
                         (before.st_size, before.st_mtime_ns, before.st_ino))
        self.assertIsNone(store.find())
        self.assertFalse(store.available())
        self.assertEqual(store.list(), [])

    def test_the_real_rom_is_the_only_one_accepted(self):
        """Outside the tests' patch only the Quadra 800 ROM's hash is accepted."""
        p = self.rom_patch
        p.stop()
        try:
            self.assertEqual(list(quadra.ROMS), ["05ad753fb594e656cf078023ec189e09e2a7655a780de993b75b8c51ed6b09ca"])
            rom = self.root / "x.rom"
            rom.write_bytes(helpers.TEST_ROM)
            with self.assertRaises(MachineUnavailable):
                quadra.check_rom(rom)
        finally:
            p.start()

    def test_prepare_pram_and_rom(self):
        pkg = helpers.make_m68k_package(self.root)
        helpers.make_quadra_machine(self.root, rom=None)
        m = self.reg.get("mac-os-8-1")
        store = quadra.RomStore(self.paths.rom_store)
        with self.assertRaises(MachineUnavailable) as cm:
            quadra.prepare(m, pkg, store)
        self.assertIn("no Quadra 800 ROM", str(cm.exception))
        store.add_bytes(helpers.TEST_ROM)
        out = quadra.prepare(m, pkg, store)
        self.assertEqual((out["rom"], out["pram"], out["rom_sha256"]), ("supplied", "created", helpers.TEST_ROM_SHA256))
        self.assertEqual(m.pram.read_bytes(), helpers.PRAM_SEED)
        self.assertEqual(m.rom.stat().st_mode & 0o777, 0o644)
        own = bytearray(b"\x01" * 256)                    # Mac OS's own PRAM is kept
        own[0x0C:0x10] = b"NuMc"                          # (written by the ROM: initialized,
        own[0x8A] = 0x45                                  # 32-bit addressing on, the qfb card's
        own[0x66:0x68] = helpers.PRAM_SEED[0x66:0x68]     # slot record in millions)
        own[0x68] = 0x85
        m.pram.write_bytes(bytes(own))
        self.assertEqual(quadra.prepare(m, pkg, store), {"rom_sha256": helpers.TEST_ROM_SHA256})
        self.assertEqual(m.pram.read_bytes(), bytes(own))
        m.rom.write_bytes(b"wrong")                      # a wrong ROM gets the store's (review #2)
        self.assertEqual(quadra.prepare(m, pkg, store)["rom"], "replaced")
        self.assertEqual(m.rom.read_bytes(), helpers.TEST_ROM)
        m.rom.write_bytes(b"wrong")
        store.remove(helpers.TEST_ROM_SHA256)
        with self.assertRaises(MachineUnavailable) as cm:
            quadra.prepare(m, pkg, store)
        self.assertIn("isn't a Quadra 800 ROM", str(cm.exception))

    def test_startup_device_for_a_cd_start(self):
        helpers.make_quadra_machine(self.root)
        m = self.reg.get("mac-os-8-1")
        m.pram.write_bytes(bytes(256))
        with open(m.pram, "r+b") as f:
            f.seek(0x78)
            f.write(bytes.fromhex("0000ffdf"))           # SCSI ID 0, the hard disk
        self.assertIsNone(quadra.point_startup(m, cd=False))
        self.assertEqual(quadra.point_startup(m, cd=True), "cd")
        self.assertEqual(m.pram.read_bytes()[0x78:0x7C], bytes.fromhex("0000ffdc"))    # SCSI ID 3: -(33 + 3)
        self.assertEqual(self.reg.get(m.id).meta["pram_startup"], "0000ffdf")
        self.assertIsNone(quadra.point_startup(m, cd=True))     # already the CD; what was there is kept
        self.assertEqual(self.reg.get(m.id).meta["pram_startup"], "0000ffdf")
        self.assertEqual(quadra.point_startup(m, cd=False), "restored")
        self.assertEqual(m.pram.read_bytes()[0x78:0x7C], bytes.fromhex("0000ffdf"))
        self.assertNotIn("pram_startup", self.reg.get(m.id).meta)
        # The package's seed (and ClassicMac's) has ffffffdf; a record that names no SCSI device
        # gets ffff before the CD's reference number.
        self.assertEqual(quadra.startup_record(3, bytes.fromhex("ffffffdf")), bytes.fromhex("ffffffdc"))
        self.assertEqual(quadra.startup_record(3, bytes.fromhex("01020304")), bytes.fromhex("ffffffdc"))


# --- the q800 arguments -----------------------------------------------------------------------
class Arguments(WithRom):
    def setUp(self):
        super().setUp()
        self.pkg = helpers.make_m68k_package(self.root)
        helpers.make_package(self.root)
        helpers.make_quadra_machine(self.root)
        self.m = self.reg.get("mac-os-8-1")
        quadra.prepare(self.m, self.pkg, None)
        self.s = settings()
        self.display = {"mode": [1600, 1200, 60]}

    def test_golden(self):
        """The argv a sample 68k Mac gets (the contract's item 2)."""
        a = build_argv(self.m, self.s, self.paths, self.display, LaunchOptions(audio_device="default:CARD=PCH"))
        pkg, d, run = self.pkg.resolve(), self.m.dir.resolve(), self.paths.run
        share = self.paths.share.resolve()
        self.assertEqual(a, [
            str(pkg / "bin/qemu-system-m68k"), "-accel", "tcg,tb-size=512",
            "-M", "q800,fb=none,audiodev=snd0", "-m", "128",
            "-bios", str(d / "Quadra800.rom"), "-L", str(pkg / "share/qemu"),
            "-drive", f"file={d / 'pram.img'},format=raw,if=mtd",
            "-device", f"nubus-virtio-mmio,romfile={pkg / 'share/qemu/classicvirtio-68k.rom'}",
            "-device", "nubus-qfb,width=800,height=600,depth=8",
            "-device", "virtio-tablet-device",
            "-device", "virtio-9p-device,fsdev=share0,mount_tag=RetroBoot Share",
            "-fsdev", f"local,id=share0,security_model=none,path={share}",
            "-device", "scsi-hd,scsi-id=0,drive=hd0", "-drive", f"file={d / 'disk.img'},format=raw,if=none,id=hd0",
            "-device", "scsi-cd,scsi-id=3,drive=cd0", "-drive", "if=none,id=cd0,media=cdrom,readonly=on",
            "-nic", "user,model=dp83932", "-name", "Mac OS 8.1",
            "-audiodev", "alsa,id=snd0,out.dev=default:CARD=PCH",
            "-display", f"retroboot,socket={run / 'display.sock'}",
            "-qmp", f"unix:{run / 'qmp-control.sock'},server=on,wait=off",
            "-qmp", f"unix:{run / 'qmp-events.sock'},server=on,wait=off",
            "-action", "reboot=shutdown", "-rtc", "base=localtime",
            "-serial", f"file:{self.paths.serial_log}"])
        for ppc in ("-prom-env", "-cpu", "-g", "-icount", "-boot", "-vga"):
            self.assertNotIn(ppc, a)
        self.assertFalse(any(x for x in a if "via" in x or "pmu" in x or "usb" in x or "macio" in x or "ati" in x
                             or "openbios" in x or "ndrv" in x))

    def test_settings_and_options(self):
        cd = self.root / "install.iso"
        cd.write_bytes(bytes(MiB))
        drive = self.root / "dev/sr0"
        drive.parent.mkdir()
        drive.write_bytes(b"")
        s = settings(**{"machine.quadra_ram_mb": 256, "audio.enabled": False, "network.guest": "none",
                        "share.enabled": False, "display.guest_size": "1024x768",
                        "machine.ram_mb": 896, "machine.cpu": "g3"})           # Power Mac values change nothing
        a = build_argv(self.m, s, self.paths, self.display,
                       LaunchOptions(cd_image=cd, cd_drive=drive, display_backend="vnc", tb_size_mb=256,
                                     lab_qmp=self.paths.lab_qmp))
        j = " ".join(a)
        self.assertEqual(a[a.index("-m") + 1], "256")
        self.assertIn("tcg,tb-size=256", a)
        self.assertIn("nubus-qfb,width=1024,height=768,depth=8", a)
        self.assertEqual(a[a.index("-nic") + 1], "none")
        self.assertEqual(a[a.index("-audiodev") + 1], "none,id=snd0")
        self.assertNotIn("virtio-9p-device", j)
        self.assertIn(f"if=none,id=cd0,media=cdrom,readonly=on,file={cd.resolve()},format=raw", a)
        self.assertIn("scsi-cd,scsi-id=4,drive=cd1", a)
        self.assertIn(f"if=none,id=cd1,media=cdrom,readonly=on,file.driver=host_cdrom,file.filename={drive},format=raw", a)
        self.assertEqual(a[a.index("-display") + 1:a.index("-display") + 3], ["none", "-vnc"])
        self.assertIn(f"unix:{self.paths.lab_qmp},server=on,wait=off", a)
        # Start Up from CD: the CD in the first CD-ROM drive, no shared folder; the depth PRAM
        # says Mac OS starts with (16 bits: 0x84 at 0x68).
        with open(self.m.pram, "r+b") as f:
            f.seek(0x68)
            f.write(b"\x84")
        a = build_argv(self.m, settings(), self.paths, self.display, LaunchOptions(boot_cd=cd))
        self.assertIn(f"if=none,id=cd0,media=cdrom,readonly=on,file={cd.resolve()},format=raw", a)
        self.assertNotIn("virtio-9p-device,fsdev=share0,mount_tag=RetroBoot Share", a)
        self.assertIn("nubus-qfb,width=800,height=600,depth=16", a)

    def test_refusals(self):
        with self.assertRaises(MachineError):
            build_argv(self.m, settings(**{"machine.quadra_ram_mb": 512}), self.paths, self.display)
        with self.assertRaises(MachineError):              # wider than nubus-qfb's 3840 x 2160
            build_argv(self.m, settings(**{"display.pixel_scale": "1"}), self.paths, {"mode": [2160, 3840, 60]})
        self.m.rom.write_bytes(b"x" * 100)
        with self.assertRaises(MachineUnavailable) as cm:
            build_argv(self.m, settings(), self.paths, self.display)
        self.assertEqual(str(cm.exception), quadra.WRONG_ROM)
        self.m.rom.unlink()
        with self.assertRaises(MachineUnavailable):
            build_argv(self.m, settings(), self.paths, self.display)
        self.m.rom.write_bytes(helpers.TEST_ROM)
        (self.pkg / "bin/qemu-system-m68k").unlink()
        with self.assertRaises(MachineUnavailable) as cm:
            build_argv(self.m, settings(), self.paths, self.display)
        self.assertEqual(str(cm.exception), quadra.NO_PACKAGE)

    def test_scsi_identity_is_adjustable(self):
        from unittest import mock
        with mock.patch.object(quadra, "SCSI_HD_IDENTITY", {"vendor": "SEAGATE", "product": "ST3600N"}):
            a = build_argv(self.m, settings(), self.paths, self.display)
        self.assertIn("scsi-hd,scsi-id=0,drive=hd0,vendor=SEAGATE,product=ST3600N", a)
        for bad in ({"vendor": "TOO LONG VENDOR"}, {"product": "a,b"}, {"serial": "1"}):
            with self.assertRaises(MachineError):
                quadra.scsi_props(bad)

    def test_launch_spec(self):
        a = build_argv(self.m, settings(), self.paths, self.display)
        spec = launch_spec(self.m, a, self.paths, "s1", settings(), "rbguest", "retroboot")
        self.assertEqual(spec["package"], {"root": str(self.paths.qemu_m68k_package), "verify": list(quadra.PACKAGE_FILES),
                                           "rom": {"dir": str(self.m.dir), "files": {"Quadra800.rom": helpers.TEST_ROM_SHA256}}})
        self.assertIn(str(self.m.pram), spec["writable"])
        self.assertEqual((spec["family"], spec["boot_speed"]), ("quadra800", "realtime"))

    def test_power_mac_unchanged(self):
        """A Power Mac's argv and spec are as before, whether its machine.json names the family or not."""
        helpers.make_machine(self.root)
        m9 = self.reg.get("mac-os-9")
        before = build_argv(m9, settings(), self.paths, self.display)
        spec = launch_spec(m9, before, self.paths, "s1", settings(), "rbguest", "retroboot")
        self.assertNotIn("family", spec)
        self.assertEqual(spec["package"]["root"], str(self.paths.qemu_package))
        self.assertEqual(spec["writable"], [str(m9.dir), str(m9.disk), str(self.paths.share)])
        m9.update_meta(family="powermac")
        self.assertEqual(build_argv(self.reg.get("mac-os-9"), settings(), self.paths, self.display), before)
        self.assertIn("mac99,via=cuda,audiodev=snd0", before)

    def test_launcher_checks_the_rom_again(self):
        from retroboot.launcher import verify_overlay
        overlay = {"dir": str(self.m.dir), "files": {"Quadra800.rom": helpers.TEST_ROM_SHA256}}
        verify_overlay(overlay)
        self.m.rom.write_bytes(b"swapped")
        with self.assertRaises(MachineError):
            verify_overlay(overlay)


# --- settings, graphics, guest preparation ---------------------------------------------------
class SettingsFor68k(WithRom):
    def test_schema(self):
        d = {x["key"]: x for x in schema.describe(schema.ValidationContext(
            machines={"a", "b"}, machine_families={"a": "powermac", "b": "quadra800"}))}
        q = d["machine.quadra_ram_mb"]
        self.assertEqual((q["default"], [c["value"] for c in q["choices"]], q["families"]),
                         (128, [64, 128, 256], ["quadra800"]))
        self.assertEqual(q["choices"][1]["label"], "128 MB")
        for key in ("machine.ram_mb", "machine.cpu", "machine.boot_speed", "display.graphics",
                    "system.battery_passthrough", "input.mac_os_keys", "network.guest_adapter"):
            self.assertEqual(d[key]["families"], ["powermac"], key)
        self.assertNotIn("families", d["display.guest_size"])
        self.assertEqual({c["value"]: c["family"] for c in d["machine.active"]["choices"]},
                         {"a": "powermac", "b": "quadra800"})

    def test_existing_settings_files_load_with_the_new_key(self):
        from retroboot.settings import SettingsStore
        path = self.paths.settings
        values = schema.defaults()
        del values["machine.quadra_ram_mb"]                 # written by a build before 68k Macs
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema": 1, "revision": 3, "desired": values, "applied": values,
                                    "applied_revision": 3, "known_good": None, "pending_confirm": None}))
        st = SettingsStore(path, self.paths.state / "known-good.json").load()
        self.assertEqual(st["applied"]["machine.quadra_ram_mb"], 128)
        self.assertEqual(st["applied"]["machine.ram_mb"], values["machine.ram_mb"])
        with self.assertRaises(schema.ValidationError):
            schema.validate_changes(st["desired"], {"machine.quadra_ram_mb": 512}, None)

    def test_ati_is_never_offered_for_a_68k_mac(self):
        from retroboot import graphics
        helpers.make_quadra_machine(self.root)
        m = self.reg.get("mac-os-8-1")
        self.assertEqual(graphics.why_not({"display.graphics": "ati-radeon"}, m, self.paths.qemu_package), "family")
        self.assertEqual(graphics.effective({"display.graphics": "ati-radeon"}, m, self.paths.qemu_package),
                         "standard")
        ctx = schema.ValidationContext(graphics={"available": True, "by_machine": {"mac-os-8-1": {
            "available": False, "message": graphics.AVAILABILITY["family"]}}})
        with self.assertRaises(schema.ValidationError) as cm:
            schema.validate_changes(schema.defaults(), {"display.graphics": "ati-radeon",
                                                        "machine.active": "mac-os-8-1"}, ctx)
        self.assertIn("68k Mac", str(cm.exception))


class GuestPrep68k(WithRom):
    def test_adapter(self):
        helpers.make_quadra_machine(self.root)
        m = self.reg.get("mac-os-8-1")
        fake = helpers.FakePrep68kModule()
        gp = guestprep.GuestPrep(Path("/nonexistent"), module=helpers.FakePrepModule(), module_68k=fake)
        self.assertEqual(gp.version_68k(), 1)
        res = asyncio.run(gp.apply_68k(m.disk))
        self.assertEqual((res["result"], res["report"]["retroboot_idle"]), ("applied", "installed"))
        self.assertNotIn("image", res["report"])
        self.assertEqual(asyncio.run(gp.repair_68k(m.disk))["result"], "ok")
        self.assertEqual([c[0] for c in fake.calls], ["apply", "fsck"])
        calls = gp.for_family("quadra800")
        self.assertEqual((calls["apply"], calls["version"]), (gp.apply_68k, gp.version_68k))
        # A library without the 68k module (or the tests' Mac OS 9 fake alone): 68k Macs start unprepared.
        gp = guestprep.GuestPrep(Path("/nonexistent"), module=helpers.FakePrepModule())
        self.assertEqual(asyncio.run(gp.apply_68k(m.disk))["result"], guestprep.UNAVAILABLE)
        self.assertIsNone(gp.version_68k())


# --- what hostd sees of Mac OS 7.5.5 and 8.1 -------------------------------------------------
# Synthetic 640 x 480 frames drawn after the measurements of the q800 smoke runs (ClassicMac's
# 7.5.5 and 8.1 base disks, hostd/reports/quadra-hostd-2026-10-24.md); no Apple pixels.
W68, H68 = 640, 480
BAR_755 = [(14, 29), (42, 64), (78, 102), (116, 147), (161, 195), (209, 254), (519, 570), (579, 595), (610, 623)]
BAR_81 = [(14, 29), (42, 63), (77, 101), (113, 145), (158, 203), (217, 244), (547, 594), (608, 625)]


def frame68(bar, titles=b"\x00\x00\x00", desktop=b"\xa8\xa8\xa8"):
    px = bytearray(desktop * (W68 * H68))

    def rect(x, y, w, h, c):
        for yy in range(y, y + h):
            o = (yy * W68 + x) * 3
            px[o:o + w * 3] = c * w
    rect(0, 0, W68, 1, b"\xff\xff\xff")
    rect(0, 1, W68, 18, b"\xe7\xe7\xe7")                 # the bar (231), its bottom line in row 19
    rect(0, 19, W68, 1, b"\x00\x00\x00")
    for i, (a, b) in enumerate(bar or []):
        colour = titles if 0 < i < len(bar) - 3 else b"\x00\x00\x00"   # the titles dim, not the clock
        for x in range(a, b + 1):
            if (x - a) % 3 != 2:
                rect(x, 5, 1, 10, colour)
    return px, rect


def dialog68(px_rect):
    """The power-key alert as 7.5.5 and 8.1 draw it on the q800: a 1-pixel black frame with a
    2-pixel black right and bottom edge, a white bevel, light gray (231) inside, a caution
    icon, two lines of text and Restart, Cancel and Shut Down (a ring around the default)."""
    px, rect = px_rect
    x0, y0 = 118, 91
    rect(x0, y0, 403, 103, b"\x00\x00\x00")             # the frame: columns 118-520, rows 91-193
    rect(x0 + 2, y0 + 103, 402, 1, b"\x00\x00\x00")      # its 1-pixel shadow, 2 pixels in
    rect(x0 + 403, y0 + 2, 1, 102, b"\x00\x00\x00")
    rect(x0 + 1, y0 + 1, 401, 101, b"\xff\xff\xff")       # the white bevel
    rect(x0 + 3, y0 + 3, 397, 97, b"\xe7\xe7\xe7")
    rect(x0 + 24, y0 + 18, 32, 28, b"\xff\xee\x00")
    for line in range(2):
        for i in range(24):
            rect(x0 + 85 + i * 11, y0 + 21 + line * 16, 7, 9, b"\x00\x00\x00")
    for bx0, bx1 in ((145, 221), (331, 407), (429, 499)):
        rect(bx0, 155, bx1 - bx0, 20, b"\x00\x00\x00")
        rect(bx0 + 1, 156, bx1 - bx0 - 2, 18, b"\xe7\xe7\xe7")
        rect(bx0 + 20, 162, 30, 7, b"\x00\x00\x00")
    rect(424, 152, 80, 1, b"\x00\x00\x00")                  # the default button's ring
    rect(424, 177, 80, 1, b"\x00\x00\x00")
    rect(424, 152, 1, 26, b"\x00\x00\x00")
    rect(503, 152, 1, 26, b"\x00\x00\x00")
    return px


class SeenOn68kMacOS(unittest.TestCase):
    def test_finder(self):
        from retroboot import finder
        for bar in (BAR_755, BAR_81):
            px, _ = frame68(bar)
            self.assertEqual(finder.finder_on_screen(W68, H68, bytes(px)), "platinum")
        # "This computer may not have been shut down properly...": an empty bar until OK.
        px, _ = frame68([(14, 17)])
        self.assertIsNone(finder.finder_on_screen(W68, H68, bytes(px)))
        px, _ = frame68(BAR_755[:1] + BAR_755[-3:])                       # the clock, no menus yet
        self.assertIsNone(finder.finder_on_screen(W68, H68, bytes(px)))

    def test_power_dialog(self):
        from retroboot import powerdlg
        for bar in (BAR_755, BAR_81):
            before, _ = frame68(bar)
            after = dialog68(frame68(bar, titles=b"\x87\x87\x87"))
            res = powerdlg.find_power_dialog((W68, H68, bytes(before)), (W68, H68, bytes(after)))
            self.assertTrue(res["found"], res)
            self.assertEqual((res["box"], res["buttons"], res["menus_dimmed"]), ((118, 91, 521, 194), 3, True))
            self.assertTrue(powerdlg.alert_on_screen((W68, H68, bytes(after))))
            buttons = powerdlg.button_boxes((W68, H68, bytes(after)), res["box"])
            self.assertEqual(buttons[0], (145, 155, 221, 175))                # Restart, the leftmost
            self.assertEqual(len(buttons), 3)
            self.assertFalse(powerdlg.find_power_dialog((W68, H68, bytes(before)), (W68, H68, bytes(before)))["found"])


# --- the lifecycle (real supervisor and guest-launcher, fake qemu-system-m68k) ----------------
class QuadraBase(Base):
    machine = False
    rom = True
    package = True

    async def asyncSetUp(self):
        p = helpers.test_rom()
        p.start()
        self.addCleanup(p.stop)
        await super().asyncSetUp()

    def prepare_root(self):
        if self.package:
            self.m68k = helpers.make_m68k_package(self.root)
            helpers.fake_qemu_conf(self.m68k, **({"modes": self.modes, "boot_s": 0.4, "finder_s": 0.9} |
                                                 self.extra_conf()))
        helpers.make_quadra_machine(self.root, rom=helpers.TEST_ROM if self.rom else None)

    def before_start(self):
        self.app.settings.set_desired_and_applied(
            {"machine.active": "mac-os-8-1"},
            schema.ValidationContext(machines=self.app.machines.ids(), check_capabilities=False))
        self.prep68k = helpers.FakePrep68kModule()
        self.prep = helpers.install_fake_prep(self.app, module_68k=self.prep68k)

    def keys(self) -> list[str]:
        return [k for r in helpers.fake_qemu_log(self.m68k) if "keys" in r for k in r["keys"]]


class QuadraLifecycle(QuadraBase):
    modes = ["normal", "normal"]

    async def test_start_restart_and_shut_down(self):
        await self.running()
        sup = self.app.supervisor
        argv = json.loads(self.app.paths.argv_json.read_text())["argv"]
        self.assertTrue(argv[0].endswith("qemu-system-m68k"))
        self.assertIn("q800,fb=none,audiodev=snd0", argv)
        self.assertNotIn("-prom-env", argv)
        self.assertEqual(sup.family, QUADRA800)
        self.assertEqual(self.ev("guest_start")[0]["family"], "quadra800")
        m = self.app.machines.get("mac-os-8-1")
        self.assertEqual(m.pram.read_bytes(), helpers.PRAM_SEED)              # made from the package's seed
        # The 68k preparation ran, none of the Mac OS 9 ones.
        self.assertEqual([c[0] for c in self.prep68k.calls], ["apply"])
        self.assertEqual(self.prep.calls, [])
        self.assertEqual(m.meta["prep"]["report"]["retroboot_idle"], "installed")
        self.assertEqual(self.ev("power_machine")[-1]["machine"], "cuda")         # no PMU, no battery
        self.assertEqual(self.ev("keys_machine")[-1]["buttons"], "none")
        await self.until_event("finder_detected")
        # Restart: Mac OS 7.5.5/8.1 take no R, so hostd shuts Mac OS down (Return) and starts it again.
        job = await self.job(self.app.lifecycle_job("restart", origin="test"))
        self.assertEqual(job.state, "succeeded", job.public())
        self.assertEqual(job.result["exit"]["reason"], "guest-shutdown")
        self.assertEqual(self.keys()[:2], ["power", "ret"])
        self.assertEqual(self.ev("clean_request")[-1]["how"], "shut down, then start")
        self.assertTrue(self.ev("shutdown_seen")[-1].get("restart"))
        await self.running(2)
        self.assertNotIn("black", self.fp.scenes())                    # no black frame for a restart
        self.assertFalse(self.app.powered_off)
        self.assertEqual(len(self.ev("guest_start")), 2)
        self.assertEqual(self.ev("guest_start")[1]["reason"], "guest-reset")
        st = self.app.machines.get("mac-os-8-1").state()
        self.assertEqual(st["last_exit"]["reason"], "guest-shutdown")
        # Shut Down that powers off.
        job = await self.job(self.app.lifecycle_job("shutdown", poweroff=True, origin="test"))
        self.assertEqual(job.state, "succeeded", job.public())
        await self.until_event("poweroff")
        await helpers.wait_for(lambda: self.fp.scene["scene"] == "black" and self.app.powered_off, 5, what="black")
        st = self.app.machines.get("mac-os-8-1").state()
        self.assertEqual((st["clean"], st["last_exit"]["reason"]), (True, "guest-shutdown"))


class QuadraForceStop(QuadraBase):
    extra_conf = lambda self: {"modes": ["hang"]}                         # noqa: E731

    async def test_force_stop(self):
        await self.running()
        job = await self.job(self.app.lifecycle_job("force-stop", origin="test"))
        self.assertEqual(job.state, "succeeded", job.public())
        self.assertEqual(self.app.supervisor.state, "stopped")
        self.assertFalse(self.app.machines.get("mac-os-8-1").state()["clean"])


class QuadraWithoutPackage(QuadraBase):
    package = False

    async def test_refused_with_a_plain_message(self):
        await self.until_event("guest_start_failed")
        rec = self.ev("guest_start_failed")[-1]
        self.assertTrue(rec["unavailable"])
        self.assertEqual(rec["error"], quadra.NO_PACKAGE)
        await self.until_doc("failure")
        self.assertIn("68k emulator", json.dumps(self.fp.doc))
        self.assertNotIn("emulator files or machine settings", json.dumps(self.fp.doc))
        self.assertIn("Startup Disk…", json.dumps(self.fp.doc, ensure_ascii=False))       # another Mac
        self.fp.click("disk")
        await self.until_doc("startup-disk")
        job = await self.job(self.app.lifecycle_job("start", origin="test"))
        self.assertEqual((job.state, job.public()["error"]["code"]), ("failed", "machine-unavailable"))
        self.assertEqual(job.public()["error"]["message"], quadra.NO_PACKAGE)
        self.assertTrue(self.app.capabilities()["emulator"])
        self.assertFalse(self.app.capabilities()["emulator_68k"])


class QuadraRomFromTheStore(QuadraBase):
    rom = False

    def before_start(self):
        super().before_start()
        self.app.roms.add_bytes(helpers.TEST_ROM)

    async def test_supplied(self):
        await self.running()
        self.assertEqual(self.ev("quadra_prepared")[0]["rom"], "supplied")
        self.assertEqual(self.app.machines.get("mac-os-8-1").rom.read_bytes(), helpers.TEST_ROM)


class QuadraWithoutRom(QuadraBase):
    rom = False

    async def test_refused(self):
        await self.until_event("guest_start_failed")
        self.assertEqual(self.ev("guest_start_failed")[-1]["error"], quadra.NO_ROM)


class IdleAndAdoption(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from retroboot.hostd import Hostd
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        helpers.make_package(self.root)
        self.app = Hostd(helpers.make_config(self.root))
        self.app.settings.store.load()

    def test_a_busy_68k_mac_os_is_judged_by_its_screen(self):
        sup = self.app.supervisor

        class Load:
            peak_value = 0.95

            def peak(self, now, span):
                return self.peak_value
        load = Load()
        sup._guest_load = lambda: load
        self.assertIs(sup._idle_now(), False)                  # a Power Mac: busy is busy
        sup.family = QUADRA800
        self.assertIsNone(sup._idle_now())                     # 68k Mac OS without RetroBoot Idle never halts
        load.peak_value = 0.1
        self.assertIs(sup._idle_now(), True)                   # with it, idle counts

    def test_adopted_q800(self):
        sup = self.app.supervisor
        argv = ["/usr/lib/retroboot/qemu-m68k/bin/qemu-system-m68k", "-M", "q800,fb=none,audiodev=snd0"]
        self.app.paths.argv_json.write_text(json.dumps({"session": "s1", "argv": argv}))
        sup._adopt_hardware("s1")
        self.assertEqual(sup.family, QUADRA800)
        self.assertIsNone(self.app.battery.session_pmu)
        self.assertIsNone(self.app.keys.buttons)
        self.app.paths.argv_json.write_text(json.dumps({"session": "s2", "argv": ["/x/qemu-system-ppc", "-M", "mac99"]}))
        sup._adopt_hardware("s2")
        self.assertEqual(sup.family, "powermac")


# --- the panel ------------------------------------------------------------------------------------
def find(doc, wid):
    found = []

    def walk(o):
        if isinstance(o, dict):
            if o.get("id") == wid and ("type" in o or "label" in o):
                found.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(doc.get("items", []))
    return found[0] if found else None


class Panel(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from retroboot.hostd import Hostd
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        p = helpers.test_rom()
        p.start()
        self.addCleanup(p.stop)
        helpers.make_package(self.root)
        helpers.make_machine(self.root)
        helpers.make_quadra_machine(self.root)
        self.app = Hostd(helpers.make_config(self.root))
        self.app.loop = asyncio.get_running_loop()
        self.app.settings.store.load()
        self.app.settings.set_desired_and_applied(
            {"machine.active": "mac-os-9"}, schema.ValidationContext(machines=self.app.machines.ids(),
                                                                     check_capabilities=False))

    def test_startup_disk_rows(self):
        from retroboot.panel.controller import StartupDisk
        doc = StartupDisk(self.app.panel, back=None).doc()
        items = find(doc, "machine")["items"]
        rows = {r["id"]: r["detail"] for r in items}
        self.assertEqual(rows["mac-os-8-1"], "Mac OS 8.1 · 68k")
        self.assertEqual(rows["mac-os-9"], "Mac OS 9.2.1")                           # a Power Mac reads as before
        self.assertEqual({r["id"]: r["checked"] for r in items}, {"mac-os-8-1": False, "mac-os-9": True})

    def test_new_blank_disk_kinds(self):
        from retroboot.panel.controller import NewBlankDisk, StartupDisk
        nb = NewBlankDisk(self.app.panel, StartupDisk(self.app.panel, back=None))
        doc = nb.doc()
        kind = find(doc, "family")
        self.assertEqual(kind["type"], "radio")
        self.assertEqual([(o["id"], o["label"], o.get("disabled", False)) for o in kind["options"]],
                         [("powermac", "PowerPC: Mac OS 8.5–9.2.2", False),
                          ("quadra800", "68k Quadra: System 7.1–Mac OS 8.1", True)])
        self.assertIn("68k emulator", json.dumps(doc, ensure_ascii=False))
        self.assertEqual([o["id"] for o in find(doc, "size")["options"]], [str(g) for g in BLANK_SIZES_GB])
        helpers.make_m68k_package(self.root)
        doc = nb.doc()
        self.assertIn("needs a Quadra 800 ROM", json.dumps(doc, ensure_ascii=False))
        self.app.roms.add_bytes(helpers.TEST_ROM)
        doc = nb.doc()
        self.assertFalse(any(o.get("disabled") for o in find(doc, "family")["options"]))

    async def test_new_blank_68k_mac(self):
        from retroboot.panel.controller import NewBlankDisk, StartupDisk
        helpers.make_m68k_package(self.root)
        self.app.roms.add_bytes(helpers.TEST_ROM)
        nb = NewBlankDisk(self.app.panel, StartupDisk(self.app.panel, back=None))
        shown = []

        async def show(screen, *a, **k):
            shown.append(screen)
        self.app.panel.refresh = lambda: asyncio.sleep(0)
        await nb.change("family", "quadra800")
        doc = nb.doc()
        self.assertEqual(find(doc, "family")["value"], "quadra800")
        self.assertEqual([o["id"] for o in find(doc, "size")["options"]], ["1", "2", "4"])
        self.assertEqual(find(doc, "size")["value"], "2")
        self.assertIn("System 7.5 can use up to 2 GB", json.dumps(doc, ensure_ascii=False))
        messages = []

        async def message(*a, **k):
            messages.append(a)
        self.app.panel.message = message
        await nb.action("create", {"name": "Quadra"})
        m = self.app.machines.get("quadra")
        self.assertEqual((m.family, m.blank, m.disk.stat().st_size), ("quadra800", True, 2 * 1024 ** 3))
        self.assertEqual(m.rom.read_bytes(), helpers.TEST_ROM)
        self.assertEqual(m.meta["template"], {"machineFamily": "quadra800"})
        self.assertTrue(messages)

    def test_display_sound_has_no_ati_for_a_68k_mac(self):
        from retroboot.panel.controller import DisplaySound
        self.assertIsNotNone(find(DisplaySound(self.app.panel).doc(), "display.graphics"))
        self.app.settings.set_desired_and_applied({"machine.active": "mac-os-8-1"},
                                                  schema.ValidationContext(machines=self.app.machines.ids(),
                                                                           check_capabilities=False))
        doc = DisplaySound(self.app.panel).doc()
        self.assertIsNone(find(doc, "display.graphics"))
        self.assertNotIn("ATI", json.dumps(doc))


# --- the web API -------------------------------------------------------------------------------------
class WebApi(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from retroboot.hostd import Hostd
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        p = helpers.test_rom()
        p.start()
        self.addCleanup(p.stop)
        helpers.make_package(self.root)
        helpers.make_machine(self.root)
        self.app = Hostd(helpers.make_config(self.root))
        self.app.loop = asyncio.get_running_loop()
        self.app.settings.store.load()
        self.app.settings.set_desired_and_applied(
            {"machine.active": "mac-os-9"},
            schema.ValidationContext(machines=self.app.machines.ids(), check_capabilities=False))
        self.app.web.tls()
        self.client = TestClient(TestServer(self.app.web.web_app, host="127.0.0.1"))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        code, _ = self.app.pairing.current_code()
        r = await self.client.post("/api/v1/pair", json={"code": code, "device_name": "Test"})
        self.cookie = r.headers.getall("Set-Cookie")[0].split("rb_session=", 1)[1].split(";", 1)[0]
        self.csrf = (await r.json())["csrf"]

    def h(self, **extra):
        return {"Cookie": f"rb_session={self.cookie}", "X-RetroBoot-CSRF": self.csrf, **extra}

    async def kinds(self):
        r = await self.client.get("/api/v1/machines", headers=self.h())
        return {k["family"]: k for k in (await r.json())["blank_kinds"]}

    async def test_roms_and_blank_68k_mac(self):
        k = await self.kinds()
        self.assertEqual((k["powermac"]["available"], k["quadra800"]["available"]), (True, False))
        self.assertIn("68k emulator", k["quadra800"]["reason"])
        self.assertEqual(k["quadra800"]["sizes_gb"], [1, 2, 4])
        r = await self.client.post("/api/v1/machines/blank", json={"name": "Q", "size_gb": 2, "family": "quadra800"},
                                   headers=self.h())
        self.assertEqual(r.status, 422)
        self.assertIn("68k emulator", (await r.json())["error"]["message"])
        helpers.make_m68k_package(self.root)
        r = await self.client.get("/api/v1/roms", headers=self.h())
        body = await r.json()
        self.assertEqual((body["roms"], body["emulator_68k"]), ([], True))
        # A file that isn't the ROM is refused, without keeping anything.
        r = await self.client.post("/api/v1/roms", data=b"not a ROM", headers=self.h(**{
            "Content-Type": "application/octet-stream"}))
        self.assertEqual(r.status, 422)
        self.assertEqual((await r.json())["error"]["code"], "unsupported-rom")
        self.assertEqual(self.app.roms.list(), [])
        r = await self.client.post("/api/v1/roms", data=bytes(5 * MiB), headers=self.h(**{
            "Content-Type": "application/octet-stream"}))
        self.assertEqual(r.status, 413)
        r = await self.client.post("/api/v1/roms", data=helpers.TEST_ROM, headers={
            "Cookie": f"rb_session={self.cookie}", "Content-Type": "application/octet-stream"})
        self.assertEqual(r.status, 403)                              # CSRF
        r = await self.client.post("/api/v1/roms", data=helpers.TEST_ROM, headers=self.h(**{
            "Content-Type": "application/octet-stream"}))
        body = await r.json()
        self.assertEqual(r.status, 201, body)
        self.assertEqual((body["rom"]["sha256"], body["rom"]["label"]), (helpers.TEST_ROM_SHA256, "Quadra 800 ROM"))
        r = await self.client.post("/api/v1/roms", data=helpers.TEST_ROM, headers=self.h(**{
            "Content-Type": "application/octet-stream"}))
        self.assertEqual(r.status, 200)
        r = await self.client.get("/api/v1/roms", headers=self.h())
        listed = (await r.json())["roms"]
        self.assertEqual([x["sha256"] for x in listed], [helpers.TEST_ROM_SHA256])
        self.assertNotIn(helpers.TEST_ROM[:32].decode(), json.dumps(listed))
        self.assertTrue((await self.kinds())["quadra800"]["available"])
        for bad in ({"name": "Q", "size_gb": 8, "family": "quadra800"}, {"name": "Q", "size_gb": 2, "family": "copland"},
                    {"name": "Q", "size_gb": 2, "family": 7}):
            r = await self.client.post("/api/v1/machines/blank", json=bad, headers=self.h())
            self.assertEqual(r.status, 422, bad)
        r = await self.client.post("/api/v1/machines/blank", json={"name": "Quadra", "size_gb": 4,
                                                                  "family": "quadra800"}, headers=self.h())
        body = await r.json()
        self.assertEqual(r.status, 201, body)
        self.assertEqual((body["machine"]["family"], body["machine"]["family_label"]), ("quadra800", "68k"))
        self.assertEqual(self.app.machines.get("quadra").rom.read_bytes(), helpers.TEST_ROM)
        rec = [r for r in self.app.lifecycle.recent if r["event"] == "rom_added"]
        self.assertEqual(rec[0]["sha256"], helpers.TEST_ROM_SHA256[:16])
        log = self.app.paths.lifecycle_log.read_bytes()
        self.assertNotIn(helpers.TEST_ROM[:40], log)
        # Settings: the startup Mac's family on machine.active's choices; status names it.
        r = await self.client.get("/api/v1/settings", headers=self.h())
        sch = {d["key"]: d for d in (await r.json())["schema"]}
        self.assertEqual({c["value"]: c["family"] for c in sch["machine.active"]["choices"]},
                         {"mac-os-9": "powermac", "quadra": "quadra800"})
        r = await self.client.get("/api/v1/status", headers=self.h())
        st = await r.json()
        self.assertEqual(st["guest"]["family"], "powermac")
        self.assertTrue(st["capabilities"]["emulator_68k"])
        self.assertEqual(st["versions"]["68k emulator"], "QEMU v11.0.2-fake-m68k")
        r = await self.client.get("/api/v1/machines", headers=self.h())
        self.assertEqual({m["id"]: m["family"] for m in (await r.json())["machines"]},
                         {"mac-os-9": "powermac", "quadra": "quadra800"})


if __name__ == "__main__":
    unittest.main()
