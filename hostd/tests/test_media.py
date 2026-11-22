"""Media library: identification, the media.cd setting, the CD in QEMU's argv,
the /api/v1/media endpoints and the media.install job (fake rbapps/rbguestprep)."""
import asyncio
import json
import os
import struct
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

import helpers
from retroboot import schema
from retroboot.hostd import Hostd
from retroboot.machines import LaunchOptions, MachineError, build_argv
from retroboot.media import MediaError, MediaLibrary, identify


def write_cd(path: Path, name: bytes = b"Myst", size: int = 5 * 1024 * 1024) -> Path:
    """A partitioned (Apple Partition Map) HFS CD image."""
    with open(path, "wb") as f:
        f.truncate(size)
        f.seek(0)
        f.write(b"ER" + struct.pack(">HI", 512, size // 512))
        for i, (ptype, start, count) in enumerate(((b"Apple_partition_map", 1, 63),
                                                   (b"Apple_HFS", 64, size // 512 - 64)), start=1):
            f.seek(512 * i)
            f.write(b"PM\0\0" + struct.pack(">III", 2, start, count) + b"x".ljust(32, b"\0")
                    + ptype.ljust(32, b"\0"))
        f.seek(64 * 512 + 1024)
        f.write(b"BD" + bytes(0x22) + bytes([len(name)]) + name)
    return path


class FakeApps:
    RETROBOOT_TEST_FAKE = True

    def __init__(self):
        self.calls = []

    def expand(self, source, out, images="extract", work_dir=None):
        self.calls.append(str(source))
        Path(out).mkdir(parents=True)
        if Path(source).read_bytes()[:4] != b"SIT!":
            raise ValueError("not a supported archive")
        return {"source": {"sha256": "ab" * 32}, "top": ["SimCity"]}


class FakeInstall:
    RETROBOOT_TEST_FAKE = True

    class InstallError(Exception):
        def __init__(self, code, message):
            super().__init__(message)
            self.code = code

    def __init__(self, fail=None):
        self.fail = fail
        self.calls = []

    def install_tree(self, image, dest, tree, replace=False):
        self.calls.append((image, dest, tree, replace))
        if self.fail:
            raise self.InstallError(*self.fail)
        with open(image, "r+b") as f:
            f.seek(8192)
            f.write(b"INSTALLED")
        return {"image": image, "top": ["SimCity"], "dest": [dest or "Applications (Mac OS 9)"],
                "files": 41, "bytes": 14203148, "fsck_after": "ok"}


class IdentifyTests(unittest.TestCase):
    def setUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)

    def test_kinds_by_content(self):
        cd = identify(write_cd(self.root / "x.bin"))
        self.assertEqual((cd["kind"], cd["format"], cd["volume_name"]), ("cd-image", "apple-partition-map", "Myst"))
        iso = self.root / "a.iso"
        with open(iso, "wb") as f:
            f.truncate(6 * 1024 * 1024)
            f.seek(0x8001)
            f.write(b"CD001\x01\x00" + b" " * 33 + b"DEMO_DISC".ljust(32))
        self.assertEqual(identify(iso)["kind"], "cd-image")
        self.assertEqual(identify(iso)["volume_name"], "DEMO_DISC")
        sit = self.root / "a.img"     # extension does not matter
        sit.write_bytes(b"SIT!" + bytes(2000))
        self.assertEqual(identify(sit)["kind"], "archive")
        dc42 = self.root / "floppy"
        data = bytes(819200)
        dc42.write_bytes(b"\x05Speed".ljust(64, b"\0") + struct.pack(">IIII", len(data), 0, 0, 0) + b"\x01\x22\x01\x00"
                         + data)
        self.assertEqual((identify(dc42)["kind"], identify(dc42)["format"]), ("disk-image", "dc42"))
        junk = self.root / "junk.iso"
        junk.write_bytes(os.urandom(4096))
        self.assertEqual(identify(junk)["kind"], "unknown")


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        self.paths = helpers.make_config(self.root).paths
        self.paths.ensure()
        self.lib = MediaLibrary(self.paths, apps_module=FakeApps(), install_module=FakeInstall())
        write_cd(self.paths.media_library / "Myst.iso")
        (self.paths.media_library / "Simcity_Classic.sit").write_bytes(b"SIT!" + bytes(4000))

    def test_list_and_cd_choices(self):
        items = {i["id"]: i for i in self.lib.list()}
        self.assertEqual(items["Myst.iso"]["kind"], "cd-image")
        self.assertTrue(items["Myst.iso"]["can_insert"])
        self.assertTrue(items["Simcity_Classic.sit"]["can_install"])
        self.assertEqual(self.lib.cd_choices(), {"Myst.iso": "Myst (Myst.iso)"})

    def test_only_registered_regular_files(self):
        outside = self.root / "secret.iso"
        write_cd(outside)
        os.symlink(outside, self.paths.media_library / "link.iso")
        self.assertIsNone(self.lib.path("link.iso"))
        for bad in ("../secret.iso", "/etc/passwd", ".hidden", "a/b", "", None):
            self.assertIsNone(self.lib.path(bad), bad)
        self.assertNotIn("link.iso", {i["id"] for i in self.lib.list()})

    def test_cd_for_launch(self):
        events = []
        log = lambda kind, **kw: events.append((kind, kw))
        self.assertIsNone(self.lib.cd_for_launch({"media.cd": "none"}, log))
        p = self.lib.cd_for_launch({"media.cd": "Myst.iso"}, log)
        self.assertEqual(p, self.paths.media_library / "Myst.iso")
        self.assertEqual(self.lib.inserted, "Myst.iso")
        # a removed disc (or an archive) leaves the drive empty and never blocks startup
        self.assertIsNone(self.lib.cd_for_launch({"media.cd": "Gone.iso"}, log))
        self.assertIsNone(self.lib.cd_for_launch({"media.cd": "Simcity_Classic.sit"}, log))
        self.assertEqual([e[0] for e in events], ["media_missing", "media_missing"])

    def test_delete_refuses_the_chosen_disc(self):
        with self.assertRaises(MediaError) as cm:
            self.lib.delete("Myst.iso", {"Myst.iso"})
        self.assertEqual(cm.exception.code, "media-in-use")
        self.lib.delete("Simcity_Classic.sit", {"Myst.iso"})
        self.assertFalse((self.paths.media_library / "Simcity_Classic.sit").exists())

    def test_unique_names(self):
        self.assertEqual(self.lib.unique_name("Myst.iso"), "Myst 2.iso")
        self.assertEqual(self.lib.unique_name("../../etc/pass wd"), "pass wd")
        self.assertEqual(self.lib.unique_name("Quake III: Demo?.sit"), "Quake III_ Demo_.sit")


class SettingAndArgvTests(unittest.TestCase):
    def test_media_cd_setting(self):
        ctx = schema.ValidationContext(media_cds={"Myst.iso": "Myst (Myst.iso)"})
        s = schema.BY_KEY["media.cd"]
        self.assertEqual(s.apply, schema.GUEST_RESTART)
        self.assertEqual(s.validate("none", ctx), "none")
        self.assertEqual(s.validate("Myst.iso", ctx), "Myst.iso")
        for bad in ("Other.iso", "/srv/x.iso", "../x", 7, None):
            with self.assertRaises(ValueError):
                s.validate(bad, ctx)
        d = s.describe(ctx)
        self.assertEqual(d["type"], "enum")
        self.assertEqual(d["choices"], [{"value": "none", "label": "No disc"},
                                        {"value": "Myst.iso", "label": "Myst (Myst.iso)"}])
        self.assertEqual(schema.defaults()["media.cd"], "none")

    def test_cd_image_in_argv(self):
        td, root = helpers.scratch()
        self.addCleanup(td.cleanup)
        helpers.make_package(root)
        cfg = helpers.make_config(root)
        paths = cfg.paths
        paths.ensure()
        helpers.make_machine(root)
        from retroboot.machines import MachineRegistry
        m = MachineRegistry(paths).get("mac-os-9")
        cd = write_cd(paths.media_library / "Myst.iso")
        settings = schema.defaults()
        argv = build_argv(m, settings, paths, None, LaunchOptions(cd_image=cd))
        drives = [argv[i + 1] for i, a in enumerate(argv) if a == "-drive"]
        self.assertIn(f"if=ide,index=3,media=cdrom,id=cd0,readonly=on,file={cd},format=raw", drives)
        self.assertIn("-global", argv)
        self.assertIn("macio-ide.pio-status-drain=on", argv)   # still a disk boot
        empty = build_argv(m, settings, paths, None, LaunchOptions())
        self.assertIn("if=ide,index=3,media=cdrom,id=cd0,readonly=on", empty)
        bad = paths.media_library / "a,b.iso"
        write_cd(bad)
        with self.assertRaises(MachineError):
            build_argv(m, settings, paths, None, LaunchOptions(cd_image=bad))


class MediaApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        helpers.make_package(self.root)
        helpers.make_machine(self.root)
        self.cfg = helpers.make_config(self.root)
        self.app = Hostd(self.cfg)
        self.app.loop = asyncio.get_running_loop()
        self.app.settings.store.load()
        helpers.install_fake_prep(self.app)
        self.apps, self.inst = FakeApps(), FakeInstall()
        self.app.media = MediaLibrary(self.app.paths, apps_module=self.apps, install_module=self.inst)
        self.app.settings.set_desired_and_applied({"machine.active": "mac-os-9"},
                                                  schema.ValidationContext(machines={"mac-os-9"},
                                                                           check_capabilities=False))
        self.app.web.tls()
        self.client = TestClient(TestServer(self.app.web.web_app, host="127.0.0.1"))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        code, _ = self.app.pairing.current_code()
        r = await self.client.post("/api/v1/pair", json={"code": code, "device_name": "t"})
        self.cookie = r.headers.getall("Set-Cookie")[0].split("rb_session=", 1)[1].split(";", 1)[0]
        self.csrf = (await r.json())["csrf"]

    def h(self, **extra):
        return {"Cookie": f"rb_session={self.cookie}", "X-RetroBoot-CSRF": self.csrf, **extra}

    async def upload(self, name, data, ctype="application/octet-stream"):
        return await self.client.post(f"/api/v1/media?name={name}", data=data, headers=self.h(**{"Content-Type": ctype}))

    async def wait_job(self, job_id, timeout=20):
        async def done():
            j = await (await self.client.get(f"/api/v1/jobs/{job_id}", headers=self.h())).json()
            return j if j["state"] in ("succeeded", "failed") else None
        return await helpers.wait_for(done, timeout, 0.05, f"job {job_id}")

    async def test_upload_list_select_delete(self):
        cd = write_cd(self.root / "src.iso").read_bytes()
        r = await self.upload("Myst.iso", cd)
        self.assertEqual(r.status, 201, await r.text())
        self.assertEqual((await r.json())["item"]["kind"], "cd-image")
        r = await self.upload("noise.iso", os.urandom(4096))
        self.assertEqual(r.status, 415)
        self.assertEqual((await r.json())["error"]["code"], "unsupported-media")
        self.assertFalse(any(p.name.startswith(".upload") for p in self.app.paths.media_library.iterdir()))
        r = await self.client.post("/api/v1/media?name=x.iso", json={"a": 1}, headers=self.h())
        self.assertEqual(r.status, 415)
        lst = await (await self.client.get("/api/v1/media", headers=self.h())).json()
        self.assertEqual([m["id"] for m in lst["media"]], ["Myst.iso"])
        self.assertEqual(lst["cd"], {"desired": "none", "applied": "none", "inserted": None})
        # the disc is a restart-class setting limited to registered CD images
        st = await (await self.client.get("/api/v1/settings", headers=self.h())).json()
        spec = next(s for s in st["schema"] if s["key"] == "media.cd")
        self.assertEqual([c["value"] for c in spec["choices"]], ["none", "Myst.iso"])
        self.assertEqual(spec["apply"], "guest_restart")
        r = await self.client.patch("/api/v1/settings", json={"revision": st["revision"],
                                                              "changes": {"media.cd": "/etc/passwd"}}, headers=self.h())
        self.assertEqual(r.status, 422)
        r = await self.client.patch("/api/v1/settings", json={"revision": st["revision"],
                                                              "changes": {"media.cd": "Myst.iso"}}, headers=self.h())
        self.assertEqual(r.status, 200, await r.text())
        r = await self.client.delete("/api/v1/media/Myst.iso", headers=self.h())
        self.assertEqual(r.status, 409)
        self.assertEqual((await r.json())["error"]["code"], "media-in-use")
        r = await self.client.delete("/api/v1/media/..%2Fsecret", headers=self.h())
        self.assertEqual(r.status, 404)

    async def test_install_job(self):
        (self.app.paths.media_library / "Simcity_Classic.sit").write_bytes(b"SIT!" + bytes(4000))
        write_cd(self.app.paths.media_library / "Myst.iso")
        disk = self.root / "var/lib/retroboot/machines/mac-os-9/disk.img"
        r = await self.client.post("/api/v1/media/Myst.iso/install", json={}, headers=self.h())
        self.assertEqual(r.status, 422)
        r = await self.client.post("/api/v1/media/Simcity_Classic.sit/install", json={"dest": "a/b"}, headers=self.h())
        self.assertEqual(r.status, 422)
        r = await self.client.post("/api/v1/media/Simcity_Classic.sit/install", json={}, headers=self.h())
        self.assertEqual(r.status, 202, await r.text())
        job = await self.wait_job((await r.json())["id"])
        self.assertEqual(job["state"], "succeeded", job)
        self.assertEqual(job["kind"], "media.install")
        self.assertIn("SimCity is installed in Applications (Mac OS 9).", job["message"])
        with open(disk, "rb") as f:
            f.seek(8192)
            self.assertEqual(f.read(9), b"INSTALLED")   # the verified copy replaced the disk
        meta = json.loads((disk.parent / "machine.json").read_text())
        self.assertEqual(meta["installed"][-1]["media"], "Simcity_Classic.sit")
        self.assertFalse(list(disk.parent.glob(".disk.img.install")))
        self.assertFalse([p for p in self.app.paths.media_library.iterdir() if p.name.startswith(".install-")])
        # a failed install leaves the disk untouched and says why
        self.inst.fail = ("already-installed", "“SimCity” is already in Applications (Mac OS 9).")
        before = disk.read_bytes()
        r = await self.client.post("/api/v1/media/Simcity_Classic.sit/install", json={}, headers=self.h())
        job = await self.wait_job((await r.json())["id"])
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["error"]["code"], "already-installed")
        self.assertEqual(disk.read_bytes(), before)
        self.assertTrue(helpers.events(self.app, "media_install_failed"))

    async def test_install_refused_while_mac_os_runs(self):
        (self.app.paths.media_library / "Simcity_Classic.sit").write_bytes(b"SIT!" + bytes(4000))
        sup = self.app.supervisor
        sup.machine = self.app.machines.get("mac-os-9")
        sup.handle = object()
        sup.state = "running"
        self.addCleanup(lambda: setattr(sup, "handle", None))
        if not sup.active:
            self.skipTest("supervisor.active needs a live guest handle")
        r = await self.client.post("/api/v1/media/Simcity_Classic.sit/install", json={}, headers=self.h())
        self.assertEqual(r.status, 409)
        body = await r.json()
        self.assertEqual(body["error"]["code"], "guest-running")
        self.assertIn("blocked_reason", body["error"])

    async def test_osx_media_install_never_stops_or_writes_the_guest(self):
        from unittest.mock import AsyncMock
        from retroboot.machines import family_fields
        machine = self.app.machines.get("mac-os-9")
        template = {"machineFamily": "powerMacOSX", "guestOS": "macosx-10.4"}
        machine.update_meta(template=template, os_version="10.4.6", os_build="8I128",
                            **family_fields(template))
        before = machine.disk.read_bytes()
        (self.app.paths.media_library / "Simcity_Classic.sit").write_bytes(b"SIT!" + bytes(4000))
        sup = self.app.supervisor
        sup.machine, sup.handle, sup.state = machine, object(), "running"
        sup.ensure_stopped_clean = AsyncMock(side_effect=AssertionError("must not stop OS X"))
        self.addCleanup(lambda: setattr(sup, "handle", None))
        listed = await (await self.client.get("/api/v1/media", headers=self.h())).json()
        self.assertFalse(listed["install"]["available"])
        response = await self.client.post("/api/v1/media/Simcity_Classic.sit/install",
                                          json={"restart": True}, headers=self.h())
        self.assertEqual(response.status, 409, await response.text())
        job = self.app.media_install_job("Simcity_Classic.sit", None, False, True)
        outcome = await self.wait_job(job.id)
        self.assertEqual(outcome["state"], "failed")
        self.assertEqual(outcome["error"]["code"], "capability-missing")
        with self.assertRaises(MediaError):
            await self.app.media.install(machine, "Simcity_Classic.sit", None, False, lambda *a: None)
        sup.ensure_stopped_clean.assert_not_called()
        self.assertEqual(self.apps.calls, [])
        self.assertEqual(self.inst.calls, [])
        self.assertEqual(machine.disk.read_bytes(), before)


from test_lifecycle import Base as LifecycleBase  # noqa: E402


class CdThroughLifecycle(LifecycleBase):
    """media.cd reaches QEMU through the supervisor at start, and changes wait for a restart."""

    def before_start(self):
        write_cd(self.app.paths.media_library / "Myst.iso")
        self.app.settings.set_desired_and_applied(
            {"media.cd": "Myst.iso"}, schema.ValidationContext(media_cds={"Myst.iso": "Myst"},
                                                               check_capabilities=False))

    async def test_disc_in_argv_and_restart_class(self):
        await self.running()
        argv = json.loads(self.app.paths.argv_json.read_text())["argv"]
        cd = str((self.app.paths.media_library / "Myst.iso").resolve())
        self.assertIn(f"if=ide,index=3,media=cdrom,id=cd0,readonly=on,file={cd},format=raw", argv)
        self.assertEqual(self.ev("guest_start")[-1]["cd"], "Myst.iso")
        self.assertEqual(self.app.media.inserted, "Myst.iso")
        s = self.app.settings
        rev = s.patch(s.revision, {"media.cd": "none"}, self.app.validation_context())
        job = await self.job(self.app.apply_job(rev, False, origin="test"))
        self.assertEqual(job.result["pending_restart"], ["media.cd"])
        await self.until_event("finder_detected")
        job = await self.job(self.app.apply_job(s.revision, True, origin="test"), 60)
        self.assertEqual(job.state, "succeeded", job.public())
        argv = json.loads(self.app.paths.argv_json.read_text())["argv"]
        self.assertIn("if=ide,index=3,media=cdrom,id=cd0,readonly=on", argv)
        self.assertFalse(any("Myst.iso" in a for a in argv))
