"""Management API: pairing, sessions, CSRF/Origin/Host checks, validation, conflicts, jobs, data paths."""
import asyncio
import io
import json
import tarfile
import unittest

from aiohttp.test_utils import TestClient, TestServer

import helpers
from retroboot.hostd import Hostd
from test_units import make_archive


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        helpers.make_package(self.root)
        self.cfg = helpers.make_config(self.root)
        self.app = Hostd(self.cfg)
        self.app.loop = asyncio.get_running_loop()
        self.app.settings.store.load()
        self.prep = helpers.install_fake_prep(self.app)
        self.app.web.tls()
        self.client = TestClient(TestServer(self.app.web.web_app, host="127.0.0.1"))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.cookie = None
        self.csrf = None

    def h(self, csrf=True, extra=None):
        hd = {}
        if self.cookie:
            hd["Cookie"] = f"rb_session={self.cookie}"
        if csrf and self.csrf:
            hd["X-RetroBoot-CSRF"] = self.csrf
        hd.update(extra or {})
        return hd

    async def pair(self, name="Test Browser"):
        code, _ = self.app.pairing.current_code()
        r = await self.client.post("/api/v1/pair", json={"code": code, "device_name": name})
        self.assertEqual(r.status, 200, await r.text())
        sc = r.headers.getall("Set-Cookie")[0]
        for flag in ("HttpOnly", "Secure", "SameSite=Strict", "Path=/"):
            self.assertIn(flag, sc)
        self.cookie = sc.split("rb_session=", 1)[1].split(";", 1)[0]
        body = await r.json()
        self.csrf = body["csrf"]
        return body

    async def wait_job(self, job_id, timeout=20):
        async def done():
            r = await self.client.get(f"/api/v1/jobs/{job_id}", headers=self.h())
            j = await r.json()
            return j if j["state"] in ("succeeded", "failed") else None
        return await helpers.wait_for(done, timeout, 0.1, f"job {job_id}")

    async def test_unpaired_access_is_limited(self):
        r = await self.client.get("/api/v1/session")
        self.assertEqual(await r.json() | {"version": None, "hostname": None},
                         {"paired": False, "device_name": None, "device_id": None, "csrf": None, "version": None,
                          "mode": "normal", "hostname": None, "form_factor": "desktop"})
        for method, path in (("GET", "/api/v1/status"), ("GET", "/api/v1/settings"), ("POST", "/api/v1/guest/start"),
                             ("GET", "/api/v1/logs"), ("GET", "/api/v1/screen.png"), ("POST", "/api/v1/diagnostics/bundle"),
                             ("GET", "/api/v1/devices")):
            r = await self.client.request(method, path)
            self.assertEqual(r.status, 401, path)
            self.assertEqual((await r.json())["error"]["code"], "not-paired")
        r = await self.client.get("/api/v1/status", headers={"Cookie": "rb_session=forged"})
        self.assertEqual(r.status, 401)

    async def test_pairing_rules(self):
        code, _ = self.app.pairing.current_code()
        wrong = "000000" if code != "000000" else "999999"
        r = await self.client.post("/api/v1/pair", json={"code": wrong, "device_name": "x"})
        self.assertEqual(r.status, 403)
        r = await self.client.post("/api/v1/pair", data="code=" + code, headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(r.status, 415)
        r = await self.client.post("/api/v1/pair", json={"code": code, "device_name": "x"},
                                   headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status, 403)
        self.assertEqual((await r.json())["error"]["code"], "origin-not-allowed")
        for _ in range(5):
            await self.client.post("/api/v1/pair", json={"code": wrong, "device_name": "x"})
        r = await self.client.post("/api/v1/pair", json={"code": code, "device_name": "x"})
        self.assertEqual(r.status, 429)
        self.assertTrue(any(e["event"] == "pairing_failed" for e in self.app.lifecycle.recent))

    async def test_session_csrf_origin_host(self):
        await self.pair()
        r = await self.client.get("/api/v1/session", headers=self.h())
        s = await r.json()
        self.assertTrue(s["paired"])
        self.assertEqual(s["csrf"], self.csrf)
        rev = (await (await self.client.get("/api/v1/settings", headers=self.h())).json())["revision"]
        body = {"revision": rev, "changes": {"input.pointer_speed": 1.5}}
        r = await self.client.patch("/api/v1/settings", json=body, headers=self.h(csrf=False))
        self.assertEqual((r.status, (await r.json())["error"]["code"]), (403, "csrf-invalid"))
        r = await self.client.patch("/api/v1/settings", json=body, headers=self.h(extra={"Origin": "http://evil.example"}))
        self.assertEqual((r.status, (await r.json())["error"]["code"]), (403, "origin-not-allowed"))
        r = await self.client.patch("/api/v1/settings", json=body, headers=self.h(extra={"Host": "attacker.example"}))
        self.assertEqual(r.status, 421)
        # The name avahi published after a conflict (retroboot-2.local) is this computer too.
        r = await self.client.get("/api/v1/session", headers=self.h(extra={"Host": "retroboot-2.local"}))
        self.assertEqual(r.status, 421)
        self.app.mdns.name = "retroboot-2.local"
        self.app.web._hosts_time = 0
        r = await self.client.get("/api/v1/session", headers=self.h(extra={"Host": "retroboot-2.local"}))
        self.assertEqual(r.status, 200, await r.text())
        r = await self.client.patch("/api/v1/settings", data=json.dumps(body), headers=self.h(extra={"Content-Type": "text/plain"}))
        self.assertEqual(r.status, 415)
        host = f"127.0.0.1:{self.client.port}"
        r = await self.client.patch("/api/v1/settings", json=body, headers=self.h(extra={"Origin": f"http://{host}"}))
        self.assertEqual(r.status, 200, await r.text())

    async def test_pixel_scale_api(self):
        """display.pixel_scale over the API: per-display choices with the Mac
        screen, disabled sizes rejected with a reason, status capability."""
        self.app.presenter.display = {"mode": [2560, 1440, 60], "modes": [[2560, 1440, 60]], "suggested_scale": 2}
        await self.pair()
        snap = await (await self.client.get("/api/v1/settings", headers=self.h())).json()
        ps = next(d for d in snap["schema"] if d["key"] == "display.pixel_scale")
        self.assertEqual((ps["type"], ps["label"], ps["group"]), ("enum", "Pixel size", "Display"))
        self.assertEqual([(c["value"], c["label"], c.get("disabled", False)) for c in ps["choices"]],
                         [("auto", "Automatic (2× — 1280 × 720)", False), ("1", "1× — 2560 × 1440", False),
                          ("2", "2× — 1280 × 720", False), ("3", "3× — 848 × 480", False),
                          ("4", "4× — needs a larger display", True)])
        rev = snap["revision"]
        r = await self.client.patch("/api/v1/settings", json={"revision": rev, "changes": {"display.pixel_scale": "4"}},
                                    headers=self.h())
        self.assertEqual(r.status, 422)
        self.assertIn("isn't available on this display", (await r.json())["error"]["fields"]["display.pixel_scale"])
        r = await self.client.patch("/api/v1/settings", json={"revision": rev, "changes": {"display.pixel_scale": "3"}},
                                    headers=self.h())
        self.assertEqual(r.status, 200, await r.text())
        r = await self.client.post("/api/v1/settings/apply", json={"revision": (await r.json())["revision"],
                                                                    "restart": False}, headers=self.h())
        job = await self.wait_job((await r.json())["job"]["id"])
        self.assertEqual(job["state"], "succeeded", job)
        self.assertEqual(job["result"]["applied"], ["display.pixel_scale"])
        st = await (await self.client.get("/api/v1/status", headers=self.h())).json()
        self.assertEqual((st["display"]["pixel_scale"], st["display"]["guest_size"]), ("3", [848, 480]))
        self.assertEqual([c["scale"] for c in st["display"]["pixel_scales"] if c["available"]], [1, 2, 3])

    async def test_settings_flow(self):
        await self.pair()
        snap = await (await self.client.get("/api/v1/settings", headers=self.h())).json()
        self.assertEqual({d["key"] for d in snap["schema"]} >= {"system.timezone", "display.host_mode"}, True)
        rev = snap["revision"]
        r = await self.client.patch("/api/v1/settings", json={"revision": rev + 5, "changes": {"audio.enabled": False}},
                                    headers=self.h())
        self.assertEqual(r.status, 409)
        self.assertEqual((await r.json())["error"]["revision"], rev)
        r = await self.client.patch("/api/v1/settings", json={"revision": rev, "changes": {"machine.ram_mb": 7,
                                                                                           "bogus": 1}},
                                    headers=self.h())
        self.assertEqual(r.status, 422)
        err = (await r.json())["error"]
        self.assertEqual(err["code"], "invalid-settings")
        self.assertEqual(set(err["fields"]), {"machine.ram_mb", "bogus"})
        r = await self.client.patch("/api/v1/settings", json={"revision": rev, "changes": {
            "audio.enabled": False, "input.pointer_speed": 2.0, "system.timezone": "America/New_York"}}, headers=self.h())
        self.assertEqual(r.status, 200)
        new = await r.json()
        self.assertEqual(new["pending"], ["audio.enabled", "input.pointer_speed", "system.timezone"])
        r = await self.client.post("/api/v1/settings/apply", json={"revision": rev, "restart": False}, headers=self.h())
        self.assertEqual(r.status, 409)
        r = await self.client.post("/api/v1/settings/apply", json={"revision": new["revision"], "restart": "yes"},
                                   headers=self.h())
        self.assertEqual(r.status, 422)
        r = await self.client.post("/api/v1/settings/apply", json={"revision": new["revision"], "restart": False},
                                   headers=self.h())
        self.assertEqual(r.status, 202)
        job = await self.wait_job((await r.json())["job"]["id"])
        self.assertEqual(job["state"], "succeeded", job)
        # No guest is running, so restart-class keys apply immediately.
        self.assertEqual(sorted(job["result"]["applied"]), ["audio.enabled", "input.pointer_speed", "system.timezone"])
        snap = await (await self.client.get("/api/v1/settings", headers=self.h())).json()
        self.assertEqual(snap["applied"]["system.timezone"], "America/New_York")
        self.assertEqual(snap["pending"], [])
        r = await self.client.post("/api/v1/settings/confirm", json={"keep": True}, headers=self.h())
        self.assertEqual((r.status, (await r.json())["error"]["code"]), (409, "no-pending-confirmation"))
        r = await self.client.post("/api/v1/settings/confirm", json={"keep": False}, headers=self.h())
        self.assertEqual(r.status, 409)
        # Descriptor shape used by the web client.
        d = {x["key"]: x for x in snap["schema"]}
        self.assertEqual(d["machine.cpu"]["choices"][0], {"value": "7400", "label": "PowerPC G4 (7400)"})
        self.assertEqual(d["machine.ram_mb"]["choices"][0], {"value": 256, "label": "256 MB"})
        self.assertEqual(d["input.pointer_speed"]["type"], "number")
        self.assertEqual(d["input.pointer_speed"]["range"], {"min": 0.25, "max": 4.0, "step": 0.25})
        self.assertEqual(d["display.host_mode"]["choices"][0]["value"], "auto")
        self.assertEqual(d["system.timezone"]["type"], "enum")
        self.assertIn("off", d["management.web"]["warning_values"])
        self.assertEqual(snap["capabilities"]["audio"]["present"], False)
        self.assertTrue(snap["capabilities"]["audio"]["reason"].endswith("."))

    async def test_guest_actions_and_jobs(self):
        await self.pair()
        r = await self.client.post("/api/v1/guest/explode", headers=self.h())
        self.assertEqual(r.status, 404)
        r = await self.client.post("/api/v1/guest/shutdown", json={"power_off": "yes"}, headers=self.h())
        self.assertEqual(r.status, 422)
        r = await self.client.post("/api/v1/guest/shutdown", json={"command": "rm -rf /"}, headers=self.h())
        self.assertEqual(r.status, 422)
        r = await self.client.post("/api/v1/guest/start", headers=self.h())
        self.assertEqual(r.status, 202)
        job = await self.wait_job((await r.json())["job"]["id"])
        self.assertEqual(job["state"], "failed")
        r = await self.client.get("/api/v1/jobs/doesnotexist", headers=self.h())
        self.assertEqual(r.status, 404)
        r = await self.client.get("/api/v1/screen.png?what=guest", headers=self.h())
        self.assertEqual((r.status, (await r.json())["error"]["code"]), (409, "guest-not-running"))
        r = await self.client.get("/api/v1/screen.png?what=/etc/passwd", headers=self.h())
        self.assertEqual(r.status, 422)
        r = await self.client.get("/api/v1/nonexistent", headers=self.h())
        self.assertEqual((r.status, (await r.json())["error"]["code"]), (404, "not-found"))

    async def test_logs_redaction_and_bundle(self):
        await self.pair()
        code, _ = self.app.pairing.current_code()
        self.app.paths.hostd_log.write_text(f"wifi psk=supersecret ok\nissued {code}\nCookie: rb_session={self.cookie}\n")
        reports = self.app.paths.logs / "startup-details"
        reports.mkdir(parents=True)
        for i in range(4):
            (reports / f"20261017-21000{i}-abcd123{i}.txt").write_text(f"Startup Details {i}\npsk=supersecret\n")
        r = await self.client.get("/api/v1/logs?source=hostd&lines=10", headers=self.h())
        text = "\n".join((await r.json())["lines"])
        for secret in ("supersecret", code, self.cookie):
            self.assertNotIn(secret, text)
        r = await self.client.get("/api/v1/logs?source=/etc/shadow", headers=self.h())
        self.assertEqual(r.status, 422)
        r = await self.client.post("/api/v1/diagnostics/bundle", headers=self.h())
        job = await self.wait_job((await r.json())["job"]["id"])
        self.assertEqual(job["state"], "succeeded", job)
        r = await self.client.get(f"/api/v1/diagnostics/bundle/{job['result']['id']}", headers=self.h())
        self.assertEqual(r.status, 200)
        data = await r.read()
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            names = tf.getnames()
            blob = b"".join(tf.extractfile(m).read() for m in tf.getmembers() if m.isfile())
        self.assertTrue(any(n.endswith("manifest.json") for n in names))
        self.assertTrue(any(n.endswith("lifecycle.jsonl") for n in names))
        details = sorted(n.split("/", 1)[1] for n in names if "/startup-details/" in n)
        self.assertEqual(details, [f"startup-details/20261017-21000{i}-abcd123{i}.txt" for i in (1, 2, 3)])
        for secret in (b"supersecret", code.encode(), self.cookie.encode(), self.csrf.encode()):
            self.assertNotIn(secret, blob)
        r = await self.client.get("/api/v1/diagnostics/bundle/../../etc", headers=self.h())
        self.assertIn(r.status, (404,))

    async def test_upload_import_checkpoints(self):
        await self.pair()
        disk = bytearray(1024 * 1024)
        disk[0:2] = b"ER"
        cfg = json.dumps({"name": "Uploaded OS 9", "machineFamily": "powerMacG4"}).encode()
        arc = self.root / "up.tar.gz"
        make_archive(arc, [("config.json", cfg, "file"), ("disk.img", bytes(disk), "file")])
        r = await self.client.post("/api/v1/machines/import?name=Lab%20Mac", data=arc.read_bytes(),
                                   headers=self.h(extra={"Content-Type": "application/gzip"}))
        self.assertEqual(r.status, 202, await r.text())
        job = await self.wait_job((await r.json())["job"]["id"])
        self.assertEqual(job["state"], "succeeded", job)
        mid = job["result"]["machine"]
        self.assertEqual(job["result"]["prep"], "applied")
        ms = await (await self.client.get("/api/v1/machines", headers=self.h())).json()
        self.assertEqual(ms["active"], mid)
        self.assertEqual(ms["machines"][0]["prep"]["result"], "applied")
        self.assertFalse(list(self.app.paths.downloads.glob("upload-*")))
        # Checkpoint a second (inactive) machine, restore it, delete it.
        helpers.make_machine(self.root, "spare", "Spare")
        r = await self.client.post("/api/v1/machines/spare/checkpoints", json={"label": "Before"}, headers=self.h())
        job = await self.wait_job((await r.json())["job"]["id"])
        self.assertEqual(job["state"], "succeeded", job)
        cp = job["result"]["id"]
        lst = await (await self.client.get("/api/v1/machines/spare/checkpoints", headers=self.h())).json()
        self.assertEqual(lst["checkpoints"][0]["id"], cp)
        disk_path = self.app.machines.get("spare").disk
        disk_path.write_bytes(b"CHANGED")
        r = await self.client.post("/api/v1/machines/spare/restore", json={"checkpoint": cp}, headers=self.h())
        job = await self.wait_job((await r.json())["job"]["id"])
        self.assertEqual(job["state"], "succeeded", job)
        self.assertEqual(disk_path.stat().st_size, 8 * 1024 * 1024)
        r = await self.client.post("/api/v1/machines/spare/restore", json={"checkpoint": "../../x"}, headers=self.h())
        self.assertEqual(r.status, 404)
        r = await self.client.delete(f"/api/v1/machines/spare/checkpoints/{cp}", headers=self.h())
        self.assertEqual(r.status, 200)
        # A dirty machine refuses checkpoints until it shuts down cleanly.
        self.app.machines.get("spare").set_state(clean=False)
        r = await self.client.post("/api/v1/machines/spare/checkpoints", json={}, headers=self.h())
        body = await r.json()
        self.assertEqual((r.status, body["error"]["code"]), (409, "not-clean"))
        self.assertTrue(body["error"]["blocked_reason"].endswith("."))
        lst = await (await self.client.get("/api/v1/machines/spare/checkpoints", headers=self.h())).json()
        self.assertEqual((lst["can_create"], bool(lst["blocked_reason"])), (False, True))

    async def test_host_power_and_duplicate_jobs(self):
        await self.pair()
        r = await self.client.post("/api/v1/host/explode", headers=self.h())
        self.assertEqual(r.status, 404)
        r = await self.client.post("/api/v1/host/poweroff", json={"force": "yes"}, headers=self.h())
        self.assertEqual(r.status, 422)
        gate = asyncio.Event()

        async def slow(job):
            await gate.wait()
        first = self.app.jobs.start("diagnostics.bundle", slow)
        r = await self.client.post("/api/v1/diagnostics/bundle", headers=self.h())
        self.assertEqual(r.status, 200)  # duplicate returns the running job
        self.assertEqual((await r.json())["id"], first.id)
        gate.set()
        r = await self.client.post("/api/v1/host/reboot", headers=self.h())
        self.assertEqual(r.status, 202)
        job = await self.wait_job((await r.json())["job"]["id"])
        self.assertEqual((job["state"], job["kind"]), ("succeeded", "host.reboot"))
        self.assertTrue(job["result"]["dev_hook"])
        self.assertTrue(helpers.events(self.app, "host_reboot"))
        st = await (await self.client.get("/api/v1/status", headers=self.h())).json()
        for k in ("guest", "boot", "host", "display", "capabilities", "versions", "jobs", "notices"):
            self.assertIn(k, st)
        self.assertEqual(st["guest"]["state"], "stopped")
        self.assertIn("urls", st["host"])

    async def test_devices_logout_network_static(self):
        await self.pair("Phone")
        first = self.cookie
        await self.pair("Laptop")
        devs = (await (await self.client.get("/api/v1/devices", headers=self.h())).json())["devices"]
        self.assertEqual(sorted(d["name"] for d in devs), ["Laptop", "Phone"])
        self.assertNotIn("token_hash", json.dumps(devs))
        phone = next(d for d in devs if d["name"] == "Phone")
        r = await self.client.delete(f"/api/v1/devices/{phone['id']}", headers=self.h())
        self.assertEqual(r.status, 204)
        r = await self.client.get("/api/v1/status", headers={"Cookie": f"rb_session={first}"})
        self.assertEqual(r.status, 401)
        st = await (await self.client.get("/api/v1/status", headers=self.h())).json()
        self.assertEqual(st["guest"]["state"], "stopped")
        self.assertIn("capabilities", st)
        net = await (await self.client.get("/api/v1/network", headers=self.h())).json()
        self.assertEqual(net["wifi"]["available"], False)
        self.assertRegex(net["web"]["fingerprint_sha256"], r"^([0-9A-F]{2}:){31}[0-9A-F]{2}$")
        r = await self.client.post("/api/v1/network/wifi/connect", json={"ssid": "x", "password": "y"}, headers=self.h())
        self.assertEqual((r.status, (await r.json())["error"]["code"]), (409, "capability-missing"))
        self.assertEqual(net["host"]["hostname"], "retroboot")
        r = await self.client.post("/api/v1/logout", headers=self.h())
        self.assertEqual(r.status, 204)
        r = await self.client.get("/api/v1/status", headers=self.h())
        self.assertEqual(r.status, 401)
        r = await self.client.get("/")
        self.assertEqual(r.status, 200)
        self.assertIn("Content-Security-Policy", r.headers)
        r = await self.client.get("/..%2f..%2fetc/passwd")
        self.assertEqual(r.status, 404)


if __name__ == "__main__":
    unittest.main()
