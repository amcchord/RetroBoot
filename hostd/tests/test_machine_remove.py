"""Removing a Mac (DELETE /api/v1/machines/{id}, job machines.remove).

The running or starting Mac, the startup Mac (applied or chosen for the next start)
and the only Mac stay.  Removal is crash-safe: the machine directory and then its
checkpoints are renamed to trash names (the first rename is the commit point), then
deleted; the next start finishes whatever a power cut interrupted, together with the
extraction directories of imports that never finished and unfinished uploads.
"""
import asyncio
import json
import os
import types
import unittest

from aiohttp.test_utils import TestClient, TestServer

import helpers
from retroboot import schema
from retroboot.checkpoints import Checkpoints
from retroboot.config import Paths
from retroboot.hostd import Hostd
from retroboot.machines import MachineRegistry
from retroboot.panel.controller import Checkpoints as CheckpointsWindow, StartupDisk
from retroboot.util import atomic_write_json
from test_lifecycle import Base


def checkpoint(root, mid, cp_id, size=1024 * 1024):
    """A finished checkpoint as Checkpoints.create leaves it."""
    d = root / "var/lib/retroboot/checkpoints" / mid / cp_id
    d.mkdir(parents=True)
    with open(d / "disk.img", "wb") as f:
        f.write(os.urandom(size))
    atomic_write_json(d / "checkpoint.json", {"schema": 1, "id": cp_id, "label": cp_id, "created": "2026-10-13T08:00:00Z",
                                              "machine": mid, "disk_bytes": size, "allocated_bytes": size,
                                              "method": "sparse"})
    return d


class RegistryRemoval(unittest.TestCase):
    def setUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        self.paths = Paths(self.root)
        self.paths.ensure()
        self.reg = MachineRegistry(self.paths)
        helpers.make_machine(self.root, "keep", "Keep")
        helpers.make_machine(self.root, "old-v5", "Old v5")
        checkpoint(self.root, "old-v5", "20261012T080000Z")
        checkpoint(self.root, "keep", "20261012T090000Z")

    def test_trash_then_purge(self):
        m = self.reg.get("old-v5")
        trashed = self.reg.trash(m)
        self.assertEqual(len(trashed), 2)
        self.assertEqual([MachineRegistry.trash_id(p.name) for p in trashed], ["old-v5", "old-v5"])
        # The registry and the checkpoints no longer know it the moment it is renamed.
        self.assertEqual(self.reg.ids(), {"keep"})
        self.assertIsNone(self.reg.get("old-v5"))
        self.assertEqual(Checkpoints(self.paths.checkpoints).list("old-v5"), [])
        self.assertGreaterEqual(self.reg.allocated(trashed), 1024 * 1024)
        self.assertEqual(self.reg.purge(trashed), 2)
        self.assertFalse(any(p.exists() for p in trashed))
        self.assertEqual(sorted(os.listdir(self.paths.machines)), ["keep"])
        self.assertEqual(sorted(os.listdir(self.paths.checkpoints)), ["keep"])
        self.assertEqual(len(Checkpoints(self.paths.checkpoints).list("keep")), 1)

    def test_recover_after_power_cut_between_the_renames(self):
        m = self.reg.get("old-v5")
        # Power cut after the commit point: the machine is in the trash, its checkpoints not yet.
        name = ".trash-old-v5-1760000000000-a1b2c3"
        os.rename(m.dir, self.paths.machines / name)
        staging = self.paths.machines / ".staging-4242-1760000000000"     # an import that never finished
        staging.mkdir()
        (staging / "disk.img").write_bytes(b"\0" * 4096)
        upload = self.paths.downloads / "upload-0123456789ab.tar.gz"       # an upload that never finished
        upload.write_bytes(b"x")
        cached = self.paths.downloads / ("ab" * 32 + ".part")              # a catalog download: resumable, kept
        cached.write_bytes(b"x")
        left = self.reg.recover_removals()
        self.assertEqual(sorted(p.name for p in left),
                         sorted([name, name, staging.name, upload.name]))
        self.assertFalse((self.paths.checkpoints / "old-v5").exists())
        self.assertTrue((self.paths.checkpoints / name / "20261012T080000Z" / "disk.img").is_file())
        self.reg.purge(left)
        self.assertEqual(sorted(os.listdir(self.paths.machines)), ["keep"])
        self.assertEqual(sorted(os.listdir(self.paths.checkpoints)), ["keep"])
        self.assertEqual(sorted(os.listdir(self.paths.downloads)), [cached.name])
        self.assertEqual(self.reg.recover_removals(), [])

    def test_checkpoints_of_a_live_machine_are_never_taken(self):
        # A trash name whose id is a machine that exists again (re-imported) leaves its checkpoints alone.
        os.mkdir(self.paths.machines / ".trash-keep-1760000000000-a1b2c3")
        left = self.reg.recover_removals()
        self.assertEqual([p.name for p in left], [".trash-keep-1760000000000-a1b2c3"])
        self.assertEqual(len(Checkpoints(self.paths.checkpoints).list("keep")), 1)

    def test_names(self):
        self.assertEqual(MachineRegistry.trash_id(".trash-mac-os-9-2-2-v5-1760000000000-0a1b2c"), "mac-os-9-2-2-v5")
        for bad in (".trash-", ".trash-x", ".trash-Bad-1-a1b2c3", ".trash-x-12a-a1b2c3", ".staging-1-2", "keep"):
            self.assertIsNone(MachineRegistry.trash_id(bad), bad)

    def test_new_id_never_inherits_leftover_checkpoints(self):
        (self.paths.checkpoints / "mac-os").mkdir()
        self.assertEqual(self.reg.unique_id("Mac OS"), "mac-os-2")


class ApiRemoval(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        helpers.make_package(self.root)
        self.cfg = helpers.make_config(self.root)
        for mid, name in (("current", "Mac OS 9.2.2 v5"), ("old-v5", "Mac OS 9.2.2 v5 (first copy)"),
                          ("spare", "Spare")):
            helpers.make_machine(self.root, mid, name)
        checkpoint(self.root, "old-v5", "20261012T080000Z")
        checkpoint(self.root, "old-v5", "20261012T090000Z", size=2 * 1024 * 1024)
        self.app = Hostd(self.cfg)
        self.app.loop = asyncio.get_running_loop()
        self.app.settings.store.load()
        self.app.settings.set_desired_and_applied(
            {"machine.active": "current"},
            schema.ValidationContext(machines=self.app.machines.ids(), check_capabilities=False))
        self.app.web.tls()
        self.client = TestClient(TestServer(self.app.web.web_app, host="127.0.0.1"))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        code, _ = self.app.pairing.current_code()
        r = await self.client.post("/api/v1/pair", json={"code": code, "device_name": "Test"})
        self.cookie = r.headers.getall("Set-Cookie")[0].split("rb_session=", 1)[1].split(";", 1)[0]
        self.csrf = (await r.json())["csrf"]

    def h(self, csrf=True):
        hd = {"Cookie": f"rb_session={self.cookie}"}
        if csrf:
            hd["X-RetroBoot-CSRF"] = self.csrf
        return hd

    async def delete(self, mid, **kw):
        r = await self.client.delete(f"/api/v1/machines/{mid}", headers=self.h(**kw))
        return r.status, await r.json()

    async def wait_job(self, job_id):
        async def done():
            j = await (await self.client.get(f"/api/v1/jobs/{job_id}", headers=self.h())).json()
            return j if j["state"] in ("succeeded", "failed") else None
        return await helpers.wait_for(done, 20, 0.05, f"job {job_id}")

    def ev(self, name):
        return helpers.events(self.app, name)

    async def listing(self):
        return {m["id"]: m for m in (await (await self.client.get("/api/v1/machines", headers=self.h())).json())["machines"]}

    async def test_remove_an_inactive_mac(self):
        ms = await self.listing()
        self.assertEqual((ms["old-v5"]["can_remove"], ms["old-v5"]["remove_blocked_reason"]), (True, None))
        self.assertEqual((ms["old-v5"]["checkpoints"], ms["old-v5"]["checkpoints_bytes"]), (2, 3 * 1024 * 1024))
        self.assertFalse(ms["current"]["can_remove"])
        self.assertIn("startup Mac", ms["current"]["remove_blocked_reason"])
        # The session and CSRF rules of every other change apply.
        r = await self.client.delete("/api/v1/machines/old-v5")
        self.assertEqual(r.status, 401)
        status, body = await self.delete("old-v5", csrf=False)
        self.assertEqual((status, body["error"]["code"]), (403, "csrf-invalid"))
        r = await self.client.delete("/api/v1/machines/old-v5",
                                     headers=self.h() | {"Origin": "https://evil.example"})
        self.assertEqual(r.status, 403)
        self.assertTrue(self.app.machines.get("old-v5"))
        status, body = await self.delete("old-v5")
        self.assertEqual(status, 202, body)
        self.assertEqual(body["kind"], "machines.remove")
        job = await self.wait_job(body["id"])
        self.assertEqual(job["state"], "succeeded", job)
        self.assertEqual(job["message"], "Mac OS 9.2.2 v5 (first copy) was removed.")
        self.assertEqual((job["result"]["machine"], job["result"]["checkpoints"]), ("old-v5", 2))
        self.assertGreaterEqual(job["result"]["allocated_bytes"], 3 * 1024 * 1024)
        p = self.app.paths
        self.assertEqual(sorted(os.listdir(p.machines)), ["current", "spare"])
        self.assertFalse((p.checkpoints / "old-v5").exists())
        self.assertEqual([n for n in os.listdir(p.checkpoints) if n.startswith(".")], [])
        ev = self.ev("machine_removed")[0]
        self.assertEqual((ev["machine"], ev["name"], ev["checkpoints"], ev["origin"]),
                         ("old-v5", "Mac OS 9.2.2 v5 (first copy)", 2, "web"))
        self.assertEqual(sorted(await self.listing()), ["current", "spare"])
        status, body = await self.delete("old-v5")
        self.assertEqual((status, body["error"]["code"]), (404, "not-found"))
        # Settings never pointed at it; validation still accepts the remaining Macs.
        r = await self.client.get("/api/v1/settings", headers=self.h())
        self.assertEqual(r.status, 200)

    async def test_refusals(self):
        sup = self.app.supervisor
        # The startup Mac.
        status, body = await self.delete("current")
        self.assertEqual((status, body["error"]["code"]), (409, "machine-active"))
        self.assertTrue(body["error"]["blocked_reason"].endswith("."))
        # Running or starting (a Mac still running after another was made the startup Mac).
        for state in ("running", "starting"):
            sup.state, sup.machine = state, self.app.machines.get("spare")
            status, body = await self.delete("spare")
            self.assertEqual((status, body["error"]["code"]), (409, "guest-running"), state)
        sup.state, sup.machine = "stopped", None
        # Chosen for the next start but not applied yet.
        r = await self.client.patch("/api/v1/settings", json={"revision": self.app.settings.revision,
                                                              "changes": {"machine.active": "spare"}},
                                    headers=self.h())
        self.assertEqual(r.status, 200, await r.text())
        status, body = await self.delete("spare")
        self.assertEqual((status, body["error"]["code"]), (409, "machine-active"))
        self.assertIn("start next", body["error"]["message"])
        # Something else holds the machines: busy, nothing removed.
        gate = asyncio.Event()

        async def slow(job):
            await gate.wait()
        held = self.app.jobs.start("machines.import", slow, resources=("machines",))
        status, body = await self.delete("old-v5")
        self.assertEqual((status, body["error"]["code"]), (409, "busy"))
        gate.set()
        await held.done.wait()
        self.assertTrue(self.app.machines.get("old-v5"))
        self.assertEqual(self.ev("machine_removed"), [])

    async def test_the_only_mac_stays(self):
        for mid in ("old-v5", "spare"):
            status, body = await self.delete(mid)
            self.assertEqual(status, 202, body)
            self.assertEqual((await self.wait_job(body["id"]))["state"], "succeeded")
        # Make "current" removable but for being the only one.
        self.app.settings.set_desired_and_applied(
            {"machine.active": None}, schema.ValidationContext(machines=self.app.machines.ids(), check_capabilities=False))
        status, body = await self.delete("current")
        self.assertEqual((status, body["error"]["code"]), (409, "last-machine"))
        self.assertEqual(self.app.machines.ids(), {"current"})

    async def test_refused_again_inside_the_job(self):
        """The check repeats in the job, right before the rename (a Mac made the startup Mac
        in between stays)."""
        job = self.app.remove_machine_job("spare", origin="test")
        self.app.settings.set_desired_and_applied(
            {"machine.active": "spare"}, schema.ValidationContext(machines=self.app.machines.ids(), check_capabilities=False))
        await job.done.wait()
        self.assertEqual((job.state, job.error["code"]), ("failed", "machine-active"))
        self.assertTrue(self.app.machines.get("spare"))

    async def test_downloaded_archive_goes_with_the_last_mac_from_it(self):
        sha = "cd" * 32
        for mid in ("old-v5", "spare"):
            m = self.app.machines.get(mid)
            m.update_meta(source={"type": "catalog", "template_id": "v5", "archive_sha256": sha})
        archive = self.app.paths.downloads / f"{sha}.tar.gz"
        archive.write_bytes(b"\0" * 8192)
        other = self.app.paths.downloads / ("ef" * 32 + ".tar.gz")
        other.write_bytes(b"\0")
        for mid, kept in (("old-v5", True), ("spare", False)):
            status, body = await self.delete(mid)
            self.assertEqual((await self.wait_job(body["id"]))["state"], "succeeded")
            self.assertEqual(archive.exists(), kept, mid)
        self.assertTrue(other.exists())
        self.assertEqual(self.ev("machine_removed")[-1].get("archive_removed"), True)

    async def test_the_classic_panel_follows(self):
        panel = types.SimpleNamespace(app=self.app)
        screen = StartupDisk(panel, back=None)
        screen.selected = "old-v5"
        cps = CheckpointsWindow(panel, "old-v5", screen)
        cps.checkpoint = "20261012T080000Z"
        self.assertIn("20261012T080000Z", json.dumps(cps.doc()))
        status, body = await self.delete("old-v5")
        self.assertEqual((await self.wait_job(body["id"]))["state"], "succeeded")
        doc = json.dumps(screen.doc())
        self.assertNotIn("old-v5", doc)
        self.assertEqual(screen.selected, "current")
        doc = cps.doc()                       # the Mac and its checkpoints are gone
        self.assertIsNone(cps.checkpoint)
        self.assertTrue(next(b for r in doc["items"] if r.get("type") == "buttons" for b in r["items"]
                             if b["id"] == "save").get("disabled"))


class LeftoversAtStart(Base):
    """A removal or an import a power cut interrupted is finished when hostd starts,
    beside Mac OS's start."""

    def before_start(self):
        p = self.app.paths
        helpers.make_machine(self.root, "old-v5", "Old v5")
        checkpoint(self.root, "old-v5", "20261012T080000Z")
        self.trash = ".trash-old-v5-1760000000000-a1b2c3"
        os.rename(p.machines / "old-v5", p.machines / self.trash)     # the commit point, then the cut
        (p.machines / ".staging-99-1760000000000").mkdir()

    async def test_leftovers_are_deleted(self):
        await self.until_event("machine_leftovers_removed")
        ev = self.ev("machine_leftovers_removed")[0]
        self.assertEqual(ev["count"], 3)
        p = self.app.paths
        self.assertEqual(sorted(os.listdir(p.machines)), ["mac-os-9"])
        self.assertEqual(os.listdir(p.checkpoints), [])
        await self.running()
