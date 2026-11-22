"""68k Macs: regression tests for the review of codex/quadra-hostd (October 24 2026).

One test (or more) per finding; each failed before its fix.  helpers.TEST_ROM stands in
for the Quadra 800 ROM, as in test_quadra.py."""
import asyncio
import errno
import json
import os
import stat
import unittest
from pathlib import Path
from unittest import mock

from aiohttp import StreamReader
from aiohttp.test_utils import make_mocked_request

import helpers
from retroboot import quadra, schema
from retroboot.catalog import ExtractLimits, ImportError_, install_machine, sanitize_template_config, validate_catalog
from retroboot.machines import MachineUnavailable, family_fields, family_of_template, validate_meta
from test_quadra import QuadraBase, WithRom, archive, entry, settings
from test_startup_disk import write_iso

MiB = 1024 * 1024


# --- 1, 7: a 68k Restart is hostd's Shut Down and a new start -------------------------------
class RestartThatTimesOut(QuadraBase):
    """#1: Mac OS takes longer than clean_stop_timeout (an application asks to save): the job
    fails, then Mac OS finishes the Shut Down hostd started.  That is still a restart: Mac OS
    starts again; the computer neither goes black nor powers off."""
    modes = ["normal", "normal"]
    cfg_overrides = {"clean_stop_timeout": 1.5}

    def extra_conf(self):
        return {"shutdown_delay_s": 4.0}

    async def test_relaunched_not_powered_off(self):
        await self.running()
        await self.until_event("finder_detected")
        job = await self.job(self.app.lifecycle_job("restart", origin="test"))
        self.assertEqual(job.state, "failed", job.public())
        self.assertEqual(job.public()["error"]["code"], "guest-unresponsive")
        await self.running(2, 20)
        self.assertEqual(self.ev("guest_start")[1]["reason"], "guest-reset")
        self.assertFalse(self.ev("poweroff"))
        self.assertFalse(self.app.powered_off)
        self.assertNotIn("black", self.fp.scenes())


class UserRestartsFromTheDialog(QuadraBase):
    """#7: the user clicks Restart in the dialog hostd opened (guest-reset): the request
    succeeded (Mac OS restarts), it did not "stop unexpectedly"."""
    modes = ["normal", "normal"]

    def extra_conf(self):
        return {"dialog_user": "restart", "dialog_user_s": 0.05}

    async def test_restart_job_succeeds(self):
        await self.running()
        await self.until_event("finder_detected")
        job = await self.job(self.app.lifecycle_job("restart", origin="test"))
        self.assertEqual(job.state, "succeeded", job.public())
        self.assertEqual(job.result["exit"]["reason"], "guest-reset")
        await self.running(2, 20)
        self.assertFalse(self.app.powered_off)


class UserShutsDownDuringARestart(QuadraBase):
    """#7: the user chooses Shut Down in the dialog before hostd's own answer: the user's
    Shut Down wins (as on a Power Mac): Mac OS does not start again."""
    modes = ["normal", "normal"]

    def extra_conf(self):
        return {"dialog_user": "shutdown", "dialog_user_s": 0.05}

    async def test_powers_off(self):
        await self.running()
        await self.until_event("finder_detected")
        job = await self.job(self.app.lifecycle_job("restart", origin="test"))
        self.assertEqual(job.state, "failed", job.public())
        self.assertEqual(job.public()["error"]["code"], "unexpected-exit")
        await self.until_event("poweroff")
        self.assertEqual(len(self.ev("guest_start")), 1)
        self.assertNotIn("ret", [k for r in helpers.fake_qemu_log(self.m68k) if "keys" in r for k in r["keys"]])


class LaterShutDownIsAShutDown(QuadraBase):
    """#1: a 68k restart whose Shut Down Mac OS never finished (the user cancelled the save
    dialog) does not turn the user's own later Shut Down into a restart."""
    modes = ["normal", "normal"]

    async def test_cleared_by_the_next_request(self):
        await self.running()
        await self.until_event("finder_detected")
        sup = self.app.supervisor
        # As if hostd's Return had been confirmed long ago for this session.
        from retroboot.util import mono
        sup.restart_by_shutdown = {"session": sup.session, "mono": mono()}
        job = await self.job(self.app.lifecycle_job("shutdown", poweroff=True, origin="test"))
        self.assertEqual(job.state, "succeeded", job.public())
        await self.until_event("poweroff")
        self.assertEqual(len(self.ev("guest_start")), 1)


# --- 2, 3, 4: ROMs, PRAM, the store --------------------------------------------------------
class RomAndPram(WithRom):
    def setUp(self):
        super().setUp()
        self.pkg = helpers.make_m68k_package(self.root)
        helpers.make_quadra_machine(self.root)
        self.m = self.reg.get("mac-os-8-1")
        self.store = quadra.RomStore(self.paths.rom_store)
        self.victim = self.root / "victim"
        self.victim.write_bytes(b"precious")
        os.chmod(self.victim, 0o600)

    def test_wrong_rom_replaced_from_the_store(self):
        """#2: a Mac with the wrong ROM gets the store's verified one."""
        self.m.rom.write_bytes(b"not the ROM" * 100)
        with self.assertRaises(MachineUnavailable):
            quadra.prepare(self.m, self.pkg, self.store)           # nothing in the store yet
        self.store.add_bytes(helpers.TEST_ROM)
        out = quadra.prepare(self.m, self.pkg, self.store)
        self.assertEqual(out["rom"], "replaced")
        self.assertEqual(self.m.rom.read_bytes(), helpers.TEST_ROM)

    def test_adopt_rom_replaces_a_wrong_one(self):
        """#2: an installed 68k Mac with a wrong ROM (install-to-disk copies it as it is)."""
        from retroboot.hostd import Hostd
        app = Hostd.__new__(Hostd)
        app.roms = self.store
        self.store.add_bytes(helpers.TEST_ROM)
        self.m.rom.write_bytes(b"wrong")
        self.assertEqual(Hostd.adopt_rom(app, self.m), "replaced")
        self.assertEqual(self.m.rom.read_bytes(), helpers.TEST_ROM)

    def test_planted_symlinks_are_never_followed(self):
        """#3: links the guest user could plant in its machine directory."""
        self.store.add_bytes(helpers.TEST_ROM)
        self.m.rom.unlink()
        for tmp in (".pram.img.tmp", ".Quadra800.rom.tmp"):
            (self.m.dir / tmp).symlink_to(self.victim)
        quadra.prepare(self.m, self.pkg, self.store)
        self.assertEqual(self.victim.read_bytes(), b"precious")
        self.assertEqual(stat.S_IMODE(self.victim.stat().st_mode), 0o600)
        self.assertTrue(stat.S_ISREG(os.lstat(self.m.pram).st_mode))
        self.assertTrue(stat.S_ISREG(os.lstat(self.m.rom).st_mode))
        # pram.img itself a link: never written through (it is made again from the seed)
        self.m.pram.unlink()
        self.m.pram.symlink_to(self.victim)
        self.assertIsNone(quadra.point_startup(self.m, cd=True))
        self.assertEqual(self.victim.read_bytes(), b"precious")
        quadra.prepare(self.m, self.pkg, self.store)
        self.assertTrue(stat.S_ISREG(os.lstat(self.m.pram).st_mode))
        self.assertEqual(self.victim.read_bytes(), b"precious")
        # the ROM a link to a good ROM elsewhere: not used, replaced by a regular copy
        elsewhere = self.root / "elsewhere.rom"
        elsewhere.write_bytes(helpers.TEST_ROM)
        self.m.rom.unlink()
        self.m.rom.symlink_to(elsewhere)
        self.assertEqual(quadra.prepare(self.m, self.pkg, self.store)["rom"], "replaced")
        self.assertTrue(stat.S_ISREG(os.lstat(self.m.rom).st_mode))
        # a FIFO where the ROM should be never blocks the check
        self.m.rom.unlink()
        os.mkfifo(self.m.rom)
        with self.assertRaises(MachineUnavailable):
            quadra.check_rom(self.m.rom)

    @unittest.skipUnless(os.geteuid() == 0, "needs root (chown)")
    def test_launcher_does_not_chown_through_links(self):
        """#3: guest-launcher gives the guest user its writable files, never a link's target."""
        import pwd
        from retroboot.launcher import Launcher
        self.m.pram.symlink_to(self.victim)
        spec = {"session": "s", "status": str(self.root / "status.json"), "sockets": [],
                "serial_log": str(self.root / "serial.log"), "qmp_events": str(self.root / "q.sock"),
                "writable": [str(self.m.pram)]}
        Launcher(spec, None, verify=False)._prepare_paths((pwd.getpwnam("nobody"), []))
        self.assertEqual(self.victim.stat().st_uid, 0)
        self.assertEqual(stat.S_IMODE(self.victim.stat().st_mode), 0o600)

    def test_damaged_store_copy_repaired_by_adding_again(self):
        """#4: a stored copy of the right size but wrong bytes is neither offered nor kept."""
        self.store.add_bytes(helpers.TEST_ROM)
        p = self.store.path(helpers.TEST_ROM_SHA256)
        os.chmod(p, 0o600)
        p.write_bytes(b"x" * len(helpers.TEST_ROM))
        self.assertFalse(self.store.available())
        self.assertEqual(self.store.list(), [])
        info = self.store.add_bytes(helpers.TEST_ROM)
        self.assertTrue(info["added"])
        self.assertTrue(self.store.available())
        self.assertEqual(self.store.find().read_bytes(), helpers.TEST_ROM)

    def test_pram_depth_from_the_template(self):
        """The emulator workstream: Mac OS takes the qfb's depth from PRAM 0x68 (0x80 1 bit ..
        0x85 millions); a new PRAM gets the template's depth, the argv follows PRAM."""
        from retroboot.machines import LaunchOptions, build_argv
        self.m.update_meta(template=dict(self.m.meta["template"], depth=16))
        quadra.prepare(self.m, self.pkg, None)
        self.assertEqual(self.m.pram.read_bytes()[0x68], 0x84)
        a = build_argv(self.m, settings(), self.paths, {"mode": [1600, 1200, 60]})
        self.assertIn("nubus-qfb,width=800,height=600,depth=16", a)
        with open(self.m.pram, "r+b") as f:          # the user chose millions in Monitors
            f.seek(0x68)
            f.write(b"\x85")
        quadra.prepare(self.m, self.pkg, None)
        self.assertEqual(self.m.pram.read_bytes()[0x68], 0x85)       # never overridden
        a = build_argv(self.m, settings(), self.paths, {"mode": [1600, 1200, 60]}, LaunchOptions())
        self.assertIn("nubus-qfb,width=800,height=600,depth=24", a)
        # without a template depth the seed's (8 bits) stays
        helpers.make_quadra_machine(self.root, "mac-os-7-5-5", "Mac OS 7.5.5", "7.5.5", template={"machineFamily": "quadra800"})
        m7 = self.reg.get("mac-os-7-5-5")
        quadra.prepare(m7, self.pkg, None)
        self.assertEqual(m7.pram.read_bytes(), helpers.PRAM_SEED)
        self.assertIn("nubus-qfb,width=800,height=600,depth=8", build_argv(m7, settings(), self.paths,
                                                                           {"mode": [1600, 1200, 60]}))


    def test_pram_without_the_mark_is_made_again(self):
        """Integration: a PRAM the ROM never initialized (no NuMc) would be reset to the ROM's
        defaults, which leave 32-bit addressing off, and the Quadra would stay gray: made again
        from the seed."""
        self.m.pram.write_bytes(bytes(256))
        out = quadra.prepare(self.m, self.pkg, None)
        self.assertEqual(out["pram"], "reseeded")
        self.assertNotIn("pram_32bit", out)
        self.assertEqual(self.m.pram.read_bytes()[0x0C:0x10], b"NuMc")
        self.assertEqual(self.m.pram.read_bytes()[0x8A] & 0x05, 0x05)
        self.assertNotIn("pram", quadra.prepare(self.m, self.pkg, None))

    def test_32bit_addressing_turned_back_on(self):
        """Integration: 7.5.5's Memory control panel can turn 32-bit addressing off; hostd turns
        it on again before the next start, keeps the other bits, and only once."""
        quadra.prepare(self.m, self.pkg, None)
        p = bytearray(self.m.pram.read_bytes())
        p[0x8A] = 0x40
        self.m.pram.write_bytes(bytes(p))
        out = quadra.prepare(self.m, self.pkg, None)
        self.assertEqual(out["pram_32bit"], "turned-on (0x40 -> 0x45)")
        after = self.m.pram.read_bytes()
        self.assertEqual(after[0x8A], 0x45)
        self.assertEqual(after[:0x8A] + after[0x8B:], bytes(p[:0x8A] + p[0x8B:]))
        self.assertNotIn("pram_32bit", quadra.prepare(self.m, self.pkg, None))


    def test_qfb_slot_record_from_the_seed(self):
        """Integration (VM165): a 68k Mac whose PRAM was made on the built-in framebuffer has no
        slot D record for the qfb card, and Mac OS started in black and white.  hostd puts the
        seed's record in, with the template's depth; the Mac's own record (Monitors) is kept."""
        quadra.prepare(self.m, self.pkg, None)
        p = bytearray(self.m.pram.read_bytes())
        p[0x66:0x6E] = bytes(8)                               # never had the card
        self.m.pram.write_bytes(bytes(p))
        self.m.update_meta(template=dict(self.m.meta["template"], depth=16))
        out = quadra.prepare(self.m, self.pkg, None)
        self.assertEqual(out["pram_qfb"], "set (16-bit)")
        after = self.m.pram.read_bytes()
        want = bytearray(helpers.PRAM_SEED[0x66:0x6E])
        want[2] = 0x84
        self.assertEqual(after[0x66:0x6E], bytes(want))
        self.assertEqual(after[:0x66] + after[0x6E:], bytes(p[:0x66] + p[0x6E:]))
        with open(self.m.pram, "r+b") as f:                   # the user picks millions in Monitors
            f.seek(0x68)
            f.write(b"\x85")
        self.assertNotIn("pram_qfb", quadra.prepare(self.m, self.pkg, None))
        self.assertEqual(self.m.pram.read_bytes()[0x68], 0x85)


    def test_os_version_learned_from_preparation(self):
        """Integration (VM169): a blank 68k Mac that Mac OS 8.1 was installed on from its CD had no
        OS version after its first preparation, which read 8.1 from the System file.  It is taken
        when the Mac has none; a known one and anything that isn't a version are left alone."""
        from retroboot.supervisor import Supervisor
        self.m.update_meta(os_version=None)
        Supervisor._learn_os_version(self.m, {"result": "applied", "report": {"os_version": "8.1"}})
        self.assertEqual(self.m.meta["os_version"], "8.1")
        Supervisor._learn_os_version(self.m, {"result": "applied", "report": {"os_version": "7.5.5"}})
        self.assertEqual(self.m.meta["os_version"], "8.1")
        self.m.update_meta(os_version=None)
        for bad in ("8.1; rm", "", None, 81, "System 7"):
            Supervisor._learn_os_version(self.m, {"result": "applied", "report": {"os_version": bad}})
            self.assertIsNone(self.m.meta["os_version"])


# --- 6, 9 and the import gap ----------------------------------------------------------------
class FamiliesAndImports(WithRom):
    def test_non_string_machine_family(self):
        """#6: no TypeError for a list or dict machineFamily: dropped or refused as unsupported."""
        for bad in ([], {}, 7, ["quadra800"]):
            self.assertIsNone(family_of_template({"machineFamily": bad}))
            with self.assertRaises(ImportError_) as cm:
                sanitize_template_config(json.dumps({"machineFamily": bad}).encode())
            self.assertEqual(cm.exception.code, "unsupported")
            e = entry("x", None)
            e["machineFamily"] = bad
            self.assertEqual(validate_catalog({"schemaVersion": 1, "machines": [e, entry("ok", None)]})[0]["id"], "ok")

    def test_68k_machine_json_is_schema_2(self):
        """#9: an older hostd (schema_version 1 only) skips 68k Macs instead of starting them on
        mac99; this hostd reads 1 and 2."""
        self.assertEqual(family_fields({"machineFamily": "quadra800"})["schema_version"], 2)
        self.assertNotIn("schema_version", family_fields({"machineFamily": "powerMacG4"}))
        self.store = quadra.RomStore(self.paths.rom_store)
        m = self.reg.create_blank("Q", 2 * 1024 ** 3, family="quadra800", rom=self._rom())
        self.assertEqual(json.loads((m.dir / "machine.json").read_text())["schema_version"], 2)
        p = self.reg.create_blank("P", 2 * 1024 ** 3)
        self.assertEqual(json.loads((p.dir / "machine.json").read_text())["schema_version"], 1)
        mid = asyncio.run(install_machine(archive(self.root / "a.tar.gz", {
            "config.json": b'{"machineFamily": "quadra800"}', "disk.img": bytes(MiB)}), self.reg, "Mac OS 8.1", "8.1",
            {"type": "upload"}, ExtractLimits(), lambda f, m: None))
        self.assertEqual(json.loads((self.reg.get(mid).dir / "machine.json").read_text())["schema_version"], 2)
        base = {"id": "m", "disk": "disk.img"}
        for v in (1, 2):
            validate_meta(dict(base, schema_version=v), "m")
            validate_meta(dict(base, schema_version=v, family="quadra800"), "m")
        with self.assertRaises(Exception):
            validate_meta(dict(base, schema_version=3), "m")
        # a 68k Mac written as schema 1 (an earlier build) is brought to 2 when hostd writes it
        helpers.make_quadra_machine(self.root, "old")
        old = self.reg.get("old")
        old.update_meta(prep=None)
        self.assertEqual(json.loads((old.dir / "machine.json").read_text())["schema_version"], 2)

    def _rom(self):
        p = self.root / "rom"
        p.write_bytes(helpers.TEST_ROM)
        return p

    def test_import_refuses_what_this_computer_cant_start(self):
        """A 68k Mac's archive on a build without the 68k emulator, or a catalog entry whose
        archive is another kind of Mac: refused with a plain message."""
        a = archive(self.root / "q.tar.gz", {"config.json": b'{"machineFamily": "quadra800"}', "disk.img": bytes(MiB)})
        with self.assertRaises(ImportError_) as cm:
            asyncio.run(install_machine(a, self.reg, "Q", None, {"type": "upload"}, ExtractLimits(), lambda f, m: None,
                                        allowed_families={"powermac"}))
        self.assertEqual(cm.exception.code, "unsupported")
        self.assertIn("68k emulator", cm.exception.message)
        with self.assertRaises(ImportError_) as cm:
            asyncio.run(install_machine(a, self.reg, "Q", None, {"type": "catalog"}, ExtractLimits(),
                                        lambda f, m: None, expected_family="powermac"))
        self.assertEqual(cm.exception.code, "unsafe_archive")
        self.assertEqual(self.reg.list(), [])


# --- 5, Start Up from CD through start_guest ---------------------------------------------------
class StaleUnavailable(QuadraBase):
    """#5: the 68k Mac's "can't start" sentence does not stay for a later start of another Mac."""
    package = False

    async def test_reset_by_the_next_start(self):
        await self.until_event("guest_start_failed")
        sup = self.app.supervisor
        self.assertTrue(sup.unavailable)
        blank = self.app.machines.create_blank("Blank", 2 * 1024 ** 3)        # a Power Mac without Mac OS
        self.app.settings.set_desired_and_applied({"machine.active": blank.id}, schema.ValidationContext(
            machines=self.app.machines.ids(), check_capabilities=False))
        self.assertFalse(await sup.start_guest("user"))                       # no CD: refused early
        self.assertIsNone(sup.unavailable)


class StartUpFromCd68k(QuadraBase):
    """Start Up from CD through start_guest: the CD in the first CD-ROM drive, PRAM pointed at
    it for that start; a start that fails, then a start without the CD, gets PRAM back."""
    modes = ["normal", "crash", "normal", "normal"]

    def before_start(self):
        super().before_start()
        write_iso(self.app.paths.media_library / "MacOS81.iso")

    async def test_cd_start_and_back(self):
        await self.running()
        await self.until_event("finder_detected")
        m = self.app.machines.get("mac-os-8-1")
        self.assertEqual(m.pram.read_bytes()[0x78:0x7C].hex(), "ffffffdf")
        job = await self.job(self.app.cd_start_job("mac-os-8-1", "MacOS81.iso", origin="test"), 60)
        # Mac OS restarts (Shut Down, then the start from the CD), whose first QEMU crashes
        # (mode "crash"): the retry starts without the CD.
        await self.until_event("guest_start", 3, 40)
        await self.running(2, 40)
        starts = self.ev("guest_start")
        self.assertEqual((starts[1].get("start_cd"), starts[2].get("start_cd")), ("image", None))
        self.assertEqual([e.get("startup_device") for e in self.ev("quadra_prepared")], [None, "cd", "restored"])
        argv = json.loads(self.app.paths.argv_json.read_text())["argv"]
        self.assertIn("if=none,id=cd0,media=cdrom,readonly=on", argv)              # no CD this time
        self.assertEqual(self.app.machines.get("mac-os-8-1").pram.read_bytes()[0x78:0x7C].hex(), "ffffffdf")
        self.assertNotIn("pram_startup", self.app.machines.get("mac-os-8-1").meta)
        cd_argv = [r["argv"] for r in helpers.fake_qemu_log(self.m68k) if "argv" in r][1]
        iso = (self.app.paths.media_library / "MacOS81.iso").resolve()
        self.assertIn(f"if=none,id=cd0,media=cdrom,readonly=on,file={iso},format=raw", cd_argv)
        self.assertIsNotNone(job)


# --- 8: the ROM upload --------------------------------------------------------------------------
class RomUpload(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from retroboot.hostd import Hostd
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        p = helpers.test_rom()
        p.start()
        self.addCleanup(p.stop)
        helpers.make_package(self.root)
        helpers.make_m68k_package(self.root)
        self.app = Hostd(helpers.make_config(self.root))
        self.app.loop = asyncio.get_running_loop()
        self.app.settings.store.load()

    async def post(self, data: bytes, length: int | None, mode: str = "normal"):
        from retroboot.web.server import ApiError
        self.app.mode = mode
        headers = {"Content-Type": "application/octet-stream"}
        if length is not None:
            headers["Content-Length"] = str(length)
        proto = mock.Mock()
        reader = StreamReader(proto, 2 ** 16, loop=asyncio.get_running_loop())
        reader.feed_data(data)
        reader.feed_eof()
        req = make_mocked_request("POST", "/api/v1/roms", headers=headers, payload=reader)
        try:
            r = await self.app.web.roms_add(req)
            return r.status, json.loads(r.body)
        except ApiError as e:
            return e.status, {"error": {"code": e.code, "message": e.message}}

    async def test_upload_stays_private_and_edge_cases(self):
        seen = []
        real = quadra.RomStore.add_file

        def spy(store, src):
            seen.append((Path(src).parent == store.root, stat.S_IMODE(os.stat(src).st_mode)))
            return real(store, src)
        with mock.patch.object(quadra.RomStore, "add_file", spy):
            status, body = await self.post(helpers.TEST_ROM, len(helpers.TEST_ROM))
        self.assertEqual(status, 201, body)
        self.assertEqual(seen, [(True, 0o600)])                 # written only inside the 0700 store
        self.assertEqual([p.name for p in self.app.paths.run.iterdir() if "rom" in p.name], [])
        status, _ = await self.post(helpers.TEST_ROM[:1000], len(helpers.TEST_ROM))      # cut short
        self.assertEqual(status, 400)
        status, _ = await self.post(helpers.TEST_ROM, None)
        self.assertEqual(status, 411)
        status, body = await self.post(helpers.TEST_ROM, len(helpers.TEST_ROM), mode="installer")
        self.assertEqual(status, 409)

        def full(*a, **k):
            raise OSError(errno.ENOSPC, "No space left on device")
        with mock.patch.object(quadra.RomStore, "add_file", full):
            status, body = await self.post(helpers.TEST_ROM, len(helpers.TEST_ROM))
        self.assertEqual(status, 507)
        self.assertIn("space", body["error"]["message"])


if __name__ == "__main__":
    unittest.main()
