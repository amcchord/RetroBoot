"""RetroBoot downloads on an installed system: with the sign-in the installer stored
(lab_http_auth_file, RBDATA/config/lab-auth.env), the panel's Install a Mac window, the web
API and the web page offer the lab catalog beside the Internet catalog (catalogs.py), and
download from it through the resumable, SHA-256-checked import.  The password appears in no
API response, log, lifecycle record, panel document or diagnostics bundle.  A local HTTPS
server with a test CA stands in for both catalogs."""
import asyncio
import base64
import io
import json
import ssl
import tarfile
import unittest

from aiohttp.test_utils import TestClient, TestServer

import helpers
from retroboot import catalogs, labauth
from retroboot.hostd import Hostd
from retroboot.panel import controller as c
from test_image_picker import archive_bytes, entry
from test_units import lab_ca_and_server_cert

USER, PASSWORD = "device-user", "Device-Pw-55-not-real"


class SystemLabelTest(unittest.TestCase):
    def test_period_osx_and_classic_names(self):
        for version in ("10.2", "10.2.8", "10.3.9", "10.4.11"):
            self.assertEqual(catalogs.os_label(version), f"Mac OS X {version}")
        self.assertEqual(catalogs.os_label("9.2.2"), "Mac OS 9.2.2")
        self.assertEqual(catalogs.os_label("Mac OS X 10.3.9"), "Mac OS X 10.3.9")
        self.assertEqual(catalogs.os_label("Copland D11E4"), "Copland D11E4")


class DeviceCatalogBase(unittest.IsolatedAsyncioTestCase):
    signed_in = True

    async def asyncSetUp(self):
        from aiohttp import web
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        helpers.make_package(self.root)
        ca_pem, cert_pem, key_pem = lab_ca_and_server_cert("localhost")
        for name, data in (("ca.pem", ca_pem), ("srv.pem", cert_pem), ("srv.key", key_pem)):
            (self.root / name).write_bytes(data)
        self.token = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
        self.seen: list[tuple[str, str | None]] = []
        self.files: dict[str, bytes] = {}
        self.catalogs: dict[str, str] = {}

        async def handler(request):
            self.seen.append((request.path, request.headers.get("Authorization")))
            if request.path.startswith("/lab/") and request.headers.get("Authorization") != self.token:
                return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="lab"'})
            if request.path in self.catalogs:
                return web.Response(text=self.catalogs[request.path])
            if request.path in self.files:
                return web.Response(body=self.files[request.path])
            return web.Response(status=404)
        app = web.Application()
        app.router.add_get("/{tail:.*}", handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.root / "srv.pem", self.root / "srv.key")
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0, ssl_context=ctx)
        await site.start()
        self.addAsyncCleanup(self.runner.cleanup)
        self.base = base = f"https://localhost:{site._server.sockets[0].getsockname()[1]}"
        lab_arc, lab_size = archive_bytes("Mac OS 9.2.1")
        pub_arc, pub_size = archive_bytes("Mac OS 9")
        self.files.update({"/lab/os921-full-v4.tar.gz": lab_arc, "/pub/mac-os-9.tar.gz": pub_arc,
                           "/pub/os921-full-v4.tar.gz": pub_arc})
        self.catalogs["/lab/catalog.json"] = json.dumps({"schemaVersion": 1, "machines": [
            entry("os922-full-v5", "Mac OS 9.2.2 (v5)", f"{base}/lab/x.tar.gz", lab_arc, lab_size),
            entry("os922-deluxe-v1", "Mac OS 9.2.2 Deluxe", f"{base}/lab/d1.tar.gz", lab_arc, lab_size),
            entry("os922-deluxe-v4", "Mac OS 9.2.2 Deluxe v4", f"{base}/lab/d4.tar.gz", lab_arc, lab_size),
            entry("os921-full-v4", "Mac OS 9.2.1 (v4)", f"{base}/lab/os921-full-v4.tar.gz", lab_arc, lab_size,
                  "9.2.1")]})
        self.catalogs["/pub/catalog.json"] = json.dumps({"schemaVersion": 1, "machines": [
            entry("mac-os-9", "Mac OS 9", f"{base}/pub/mac-os-9.tar.gz", pub_arc, pub_size, "Mac OS 9.2.1"),
            # the same id as a RetroBoot download: listed once, from RetroBoot downloads
            entry("os921-full-v4", "Mac OS 9.2.1 (public copy)", f"{base}/pub/os921-full-v4.tar.gz", pub_arc,
                  pub_size, "9.2.1")]})
        # This computer already has Deluxe v4 (installed from the installer's disc).
        helpers.make_machine(self.root, "os922-deluxe-v4", "Mac OS 9.2.2 Deluxe v4")
        over = {"catalog_url": f"{base}/pub/catalog.json", "catalog_ca_file": str(self.root / "ca.pem")}
        if self.signed_in:
            env = self.root / "var/lib/retroboot/config/lab-auth.env"   # what the installer writes
            labauth.write_env(env, labauth.LabAuth(f"{base}/lab/", USER, PASSWORD))
            over["lab_http_auth_file"] = str(env)
        self.cfg = helpers.make_config(self.root, **over)
        self.app = Hostd(self.cfg)
        self.app.loop = asyncio.get_running_loop()
        self.app.settings.store.load()
        helpers.install_fake_prep(self.app)
        self.app.web.tls()
        self.client = TestClient(TestServer(self.app.web.web_app, host="127.0.0.1"))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        code, _ = self.app.pairing.current_code()
        r = await self.client.post("/api/v1/pair", json={"code": code, "device_name": "t"})
        self.cookie = r.headers.getall("Set-Cookie")[0].split("rb_session=", 1)[1].split(";", 1)[0]
        self.csrf = (await r.json())["csrf"]
        self.texts: list[str] = []       # every response body, checked for the password

    def h(self):
        return {"Cookie": f"rb_session={self.cookie}", "X-RetroBoot-CSRF": self.csrf}

    async def get(self, path):
        r = await self.client.get(path, headers=self.h())
        text = await r.text()
        self.texts.append(text)
        return r.status, (json.loads(text) if r.content_type == "application/json" else text)

    async def post(self, path, body):
        r = await self.client.post(path, json=body, headers=self.h())
        text = await r.text()
        self.texts.append(text)
        return r.status, json.loads(text)

    async def machines_ready(self):
        async def ready():
            _, body = await self.get("/api/v1/machines")
            return body if body["catalog_state"] == "ready" else None
        return await helpers.wait_for(ready, 20, 0.1, "catalogs ready")

    async def wait_job(self, job_id):
        async def done():
            _, j = await self.get(f"/api/v1/jobs/{job_id}")
            return j if j["state"] in ("succeeded", "failed") else None
        return await helpers.wait_for(done, 20, 0.1, f"job {job_id}")


class DeviceCatalogTests(DeviceCatalogBase):
    async def test_web_lists_both_catalogs_and_imports_a_download(self):
        self.assertEqual([sid for sid, _ in self.app.catalogs.clients()], ["lab", "internet"])
        body = await self.machines_ready()
        self.assertEqual([(k["id"], k["label"], k["state"]) for k in body["catalogs"]],
                         [("lab", "RetroBoot downloads", "ready"), ("internet", "Internet", "ready")])
        rows = [(e["catalog"], e["id"], e["older"], e["imported"]) for e in body["catalog"]]
        self.assertEqual(rows, [("lab", "os922-full-v5", False, False), ("lab", "os922-deluxe-v4", False, True),
                                ("lab", "os922-deluxe-v1", True, False), ("lab", "os921-full-v4", False, False),
                                ("internet", "mac-os-9", False, False)])
        lab = body["catalog"][3]
        self.assertEqual((lab["catalog_label"], lab["family"], lab["version"], lab["download_bytes"]),
                         ("RetroBoot downloads", "os921-full", 4, lab["archiveBytes"]))
        status, cat = await self.get("/api/v1/machines/catalog")
        self.assertEqual(status, 200)
        self.assertEqual([(m["catalog"], m["id"]) for m in cat["machines"]], [(r[0], r[1]) for r in rows])
        self.assertEqual([k["state"] for k in cat["catalogs"]], ["ready", "ready"])
        # Import from RetroBoot downloads: the sign-in goes to the lab archive only.
        self.seen.clear()
        status, res = await self.post("/api/v1/machines/import", {"catalog_id": "os921-full-v4", "catalog": "lab"})
        self.assertEqual(status, 202, res)
        job = await self.wait_job(res["job"]["id"])
        self.assertEqual(job["state"], "succeeded", job)
        m = self.app.machines.get(job["result"]["machine"])
        self.assertEqual((m.meta["source"]["catalog"], m.meta["source"]["template_id"], m.name),
                         ("lab", "os921-full-v4", "Mac OS 9.2.1 (v4)"))
        self.assertEqual(self.seen, [("/lab/os921-full-v4.tar.gz", self.token)])
        body = await self.machines_ready()
        self.assertTrue(next(e for e in body["catalog"] if e["id"] == "os921-full-v4")["imported"])
        # An old client without "catalog" still imports from the Internet catalog.
        status, res = await self.post("/api/v1/machines/import", {"catalog_id": "mac-os-9"})
        job = await self.wait_job(res["job"]["id"])
        self.assertEqual(self.app.machines.get(job["result"]["machine"]).meta["source"]["catalog"], "internet")
        self.assertTrue(all(h is None for p, h in self.seen if p.startswith("/pub/")))
        status, res = await self.post("/api/v1/machines/import", {"catalog_id": "mac-os-9", "catalog": "elsewhere"})
        self.assertEqual(status, 422)
        await self.assert_no_password()

    async def test_custom_catalog_is_independent_and_never_gets_lab_credentials(self):
        from retroboot.schema import validate_catalog_urls
        for url in ("http://example.test/catalog.json", "https://user:secret@example.test/a",
                    "https://example.test:bad/a", "https://example.test/a?token=foo",
                    "https://example.test/", "https://[broken"):
            with self.assertRaises(ValueError):
                validate_catalog_urls([url])
        archive, expanded = archive_bytes("Custom Mac")
        url = f"{self.base}/lab/custom.json"
        # A URL within the protected base must still never get the built-in sign-in.
        self.catalogs["/lab/custom.json"] = json.dumps({"schemaVersion": 1, "machines": []})
        cfg = self.app.settings
        cfg.set_desired_and_applied({"catalog.urls": [url]}, self.app.validation_context())
        results, errors = await self.app.catalogs.fetch_all()
        source = catalogs.custom_id(url)
        self.assertIn(source, errors)
        self.assertIn(("/lab/custom.json", None), self.seen)
        self.assertIsNone(self.app.catalogs.client(source).auth)

        custom_url = f"{self.base}/custom/catalog.json"
        self.files["/custom/mac.tar.gz"] = archive
        self.catalogs["/custom/catalog.json"] = json.dumps({"schemaVersion": 1, "machines": [
            entry("os922-full-v5", "Custom Mac", f"{self.base}/custom/mac.tar.gz", archive, expanded)
        ]})
        cfg.set_desired_and_applied({"catalog.urls": [custom_url]}, self.app.validation_context())
        source = catalogs.custom_id(custom_url)
        status, body = await self.get("/api/v1/machines/catalog")
        self.assertEqual(status, 200)
        matching = [(e["catalog"], e["name"]) for e in body["machines"] if e["id"] == "os922-full-v5"]
        self.assertEqual(matching, [("lab", "Mac OS 9.2.2 (v5)"), (source, "Custom Mac")])
        status, res = await self.post("/api/v1/machines/import", {"catalog_id": "os922-full-v5", "catalog": source})
        self.assertEqual(status, 202)
        job = await self.wait_job(res["job"]["id"])
        self.assertEqual(job["state"], "succeeded", job)
        m = self.app.machines.get(job["result"]["machine"])
        self.assertEqual(m.meta["source"]["catalog_url"], custom_url)
        self.assertIn(("/custom/mac.tar.gz", None), self.seen)
        cfg.set_desired_and_applied({"catalog.urls": []}, self.app.validation_context())
        self.assertIsNone(self.app.catalogs.client(source))
        self.assertIsNotNone(self.app.machines.get(m.id))

    async def test_panel_downloads_a_mac_from_retroboot_downloads(self):
        p = self.app.panel
        scr = c.CatalogList(p, back=lambda: None)
        await p.show(scr)
        await helpers.wait_for(lambda: scr.items is not None, 20, what="catalog list")
        doc = scr.doc()
        self.assertEqual(doc["title"], "Download a Mac")
        lst = next(i for i in doc["items"] if i.get("type") == "list")
        self.assertEqual([r["id"] for r in lst["items"]],
                         ["lab:os922-full-v5", "lab:os922-deluxe-v4", "lab:os921-full-v4", "internet:mac-os-9"])
        self.assertEqual(lst["value"], "lab:os922-full-v5")            # the first Mac not installed yet
        self.assertIn("Installed", lst["items"][1]["detail"])
        # RetroBoot downloads' Macs carry no source label; the Internet catalog's say so.
        self.assertNotIn("RetroBoot downloads", lst["items"][0]["detail"])
        self.assertTrue(lst["items"][3]["detail"].startswith("Internet · "), lst["items"][3]["detail"])
        buttons = [b["id"] for r in doc["items"] if r.get("type") == "buttons" for b in r["items"]]
        self.assertEqual(buttons, ["servers", "usb", "cancel", "install"])
        self.assertEqual(next(i for i in doc["items"] if i.get("type") == "checkbox")["id"], "older")
        await scr.change("older", True)
        self.assertIn("lab:os922-deluxe-v1", json.dumps(scr.doc()))
        await scr.action("install", {"entry": "lab:os921-full-v4"})
        await helpers.wait_for(lambda: (p.last_doc or {}).get("id") == "setup-done", 20, what="installed")
        imported = [e for e in helpers.events(self.app, "machine_imported")]
        self.assertEqual(len(imported), 1)
        mid = imported[0]["machine"]
        self.assertEqual(self.app.machines.get(mid).meta["source"]["catalog"], "lab")
        self.texts.append(json.dumps(doc))
        await self.assert_no_password()

    async def test_a_second_copy_gets_its_own_name(self):
        """Downloading a Mac this computer already has gives the new one its own name, so Startup
        Disk never lists two Macs called the same."""
        names = []
        for _ in range(2):
            status, res = await self.post("/api/v1/machines/import", {"catalog_id": "os921-full-v4", "catalog": "lab"})
            self.assertEqual(status, 202, res)
            job = await self.wait_job(res["job"]["id"])
            self.assertEqual(job["state"], "succeeded", job)
            names.append(self.app.machines.get(job["result"]["machine"]).name)
        self.assertEqual(names, ["Mac OS 9.2.1 (v4)", "Mac OS 9.2.1 (v4) 2"])

    async def test_after_a_download_offers_to_start_up_with_it(self):
        """From the RetroBoot window, a download ends in “… is on this computer. Start up with it
        now?”; Not Now goes back."""
        p = self.app.panel
        back = []

        async def went_back():
            back.append(True)
        scr = c.CatalogList(p, back=went_back, then=c.after_download)
        await p.show(scr)
        await helpers.wait_for(lambda: scr.items is not None, 20, what="catalog list")
        self.assertEqual([b["label"] for r in scr.doc()["items"] if r.get("type") == "buttons" for b in r["items"]],
                         ["Image Servers…", "From a USB Disk…", "Cancel", "Download"])
        await scr.action("install", {"entry": "lab:os921-full-v4"})
        await helpers.wait_for(lambda: (p.last_doc or {}).get("id") == "downloaded", 20, what="downloaded")
        self.assertIn("is on this computer", json.dumps(p.last_doc, ensure_ascii=False))
        self.assertIn("Start up with it now?", json.dumps(p.last_doc, ensure_ascii=False))
        await p.screen.action("cancel", {})
        self.assertEqual(back, [True])

    async def assert_no_password(self):
        """Not in any response, log, lifecycle record, settings export or diagnostics bundle."""
        for path in ("/api/v1/status", "/api/v1/settings", "/api/v1/jobs", "/api/v1/logs?source=hostd"):
            await self.get(path)
        status, res = await self.post("/api/v1/diagnostics/bundle", {})
        job = await self.wait_job(res["job"]["id"])
        self.assertEqual(job["state"], "succeeded", job)
        r = await self.client.get(f"/api/v1/diagnostics/bundle/{job['result']['id']}", headers=self.h())
        with tarfile.open(fileobj=io.BytesIO(await r.read()), mode="r:gz") as tf:
            blob = b"".join(tf.extractfile(m).read() for m in tf.getmembers() if m.isfile())
        self.assertNotIn(PASSWORD.encode(), blob)
        self.assertNotIn(self.token.split()[1].encode(), blob)
        texts = self.texts + [json.dumps(list(self.app.lifecycle.recent))]
        texts += [f.read_text(errors="replace") for f in self.app.paths.logs.rglob("*") if f.is_file()]
        for t in texts:
            self.assertNotIn(PASSWORD, t)
            self.assertNotIn(self.token.split()[1], t)
        self.assertEqual(self.app.redactor(f"x {PASSWORD} y"), "x [REDACTED] y")
        self.assertNotIn(PASSWORD, repr(self.app.lab_auth))


class DeviceWithoutSignInTests(DeviceCatalogBase):
    signed_in = False

    async def test_only_the_internet_catalog(self):
        self.assertIsNone(self.app.lab_auth)
        self.assertEqual([sid for sid, _ in self.app.catalogs.clients()], ["internet"])
        body = await self.machines_ready()
        self.assertEqual([k["id"] for k in body["catalogs"]], ["internet"])
        self.assertEqual([(e["catalog"], e["id"]) for e in body["catalog"]],
                         [("internet", "mac-os-9"), ("internet", "os921-full-v4")])
        scr = c.CatalogList(self.app.panel, back=lambda: None)
        await helpers.wait_for(lambda: scr.items is not None, 20, what="catalog list")
        doc = scr.doc()
        self.assertEqual(doc["title"], "Download a Mac")
        lst = next(i for i in doc["items"] if i.get("type") == "list")
        detail = lst["items"][0]["detail"]
        self.assertRegex(detail, r"^Mac OS 9\.2\.1 · Recommended · [0-9.]+ [KMG]B download$")
        self.assertFalse(any(p.startswith("/lab/") for p, _ in self.seen))
        status, res = await self.post("/api/v1/machines/import", {"catalog_id": "mac-os-9", "catalog": "lab"})
        self.assertEqual(status, 422)


class ArrangeTests(unittest.TestCase):
    def test_merge_families_and_known(self):
        e = lambda i: {"id": i, "name": i}
        out = catalogs.arrange([("lab", [e("os922-full-v4"), e("os922-deluxe-v2"), e("x"), e("os922-full-v5")]),
                                ("internet", [e("x"), e("os922-deluxe-v3"), e("y")])],
                               *catalogs.known_from(["os922-deluxe-v3", None, "mac"]))
        self.assertEqual([(i.source, i.entry["id"], i.older, i.known) for i in out],
                         [("lab", "os922-full-v5", False, False), ("lab", "os922-full-v4", True, False),
                          ("internet", "os922-deluxe-v3", False, True), ("lab", "os922-deluxe-v2", True, False),
                          ("lab", "x", False, False), ("internet", "y", False, False)])
        self.assertEqual(catalogs.overall([{"state": "ready"}, {"state": "error", "error": "e"}]), ("ready", None))
        self.assertEqual(catalogs.overall([{"state": "error", "error": "e"}]), ("error", "e"))
        self.assertEqual(catalogs.overall([{"state": "loading"}, {"state": "ready"}]), ("loading", None))

    def test_snapshot_respects_retry_window_after_failure(self):
        async def run():
            class FailClient:
                cache = None
                url = "https://example.invalid/catalog.json"

                async def fetch(self, max_age=60.0):
                    raise catalogs.ImportError_("Network down", "network")

            class DummyApp:
                def __init__(self):
                    self.loop = asyncio.get_running_loop()
                    self.lab_auth = None
                    self.catalog = FailClient()
                    self.settings = type("S", (), {"applied": {"catalog.urls": []}})()

            cats = catalogs.Catalogs(DummyApp())
            _res, st1 = cats.snapshot()
            self.assertEqual(st1[0]["state"], "loading")
            await asyncio.sleep(0.02)
            _res, st2 = cats.snapshot()
            self.assertEqual(st2[0]["state"], "error")
            self.assertEqual(st2[0]["error"], "Network down")

            # Failure from fetch_all() also stays in error state until RETRY_S elapses.
            cats2 = catalogs.Catalogs(DummyApp())
            await cats2.fetch_all()
            _res, st3 = cats2.snapshot()
            self.assertEqual(st3[0]["state"], "error")
            self.assertEqual(st3[0]["error"], "Network down")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
