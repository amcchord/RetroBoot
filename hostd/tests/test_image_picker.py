"""The installer's "Choose Macs to Install": several Macs from the disc, RetroBoot downloads
(lab catalog over HTTPS with Basic auth, a local test server), the Internet catalog and USB;
downloads straight onto the new disk, Try Again / Skip, Sign In, the space check, and the
installed system's seed.  install-to-disk and list-disks are fakes; the new disk's RBDATA
is a directory."""
import asyncio
import base64
import hashlib
import io
import json
import ssl
import sys
import tarfile
import unittest
from pathlib import Path

import helpers
from fakes.fake_presenter import FakePresenter
from retroboot import labauth
from retroboot.config import Paths
from retroboot.hostd import Hostd
from retroboot.panel import picker
from test_units import lab_ca_and_server_cert

GiB = 1024 ** 3
MiB = 1024 ** 2
USER, PASSWORD = "labtester", "Pw-0123456789-not-real"

LIST_DISKS = """#!{py}
import json
print(json.dumps([
  {{"path": "/dev/sr0", "model": "QEMU DVD-ROM", "size_bytes": 900000000, "removable": True, "transport": "sata", "is_boot_medium": True}},
  {{"path": "/dev/vda", "model": "Virtual Disk", "size_bytes": {size}, "removable": False, "transport": "virtio", "is_boot_medium": False}}
]))
"""

# Stands in for install-to-disk: the new disk's RBDATA is DATA (a directory); --mount-data
# links the mount point to it.  Every call is appended to REC.
INSTALL = """#!{py}
import json, os, sys, pathlib
argv = sys.argv[1:]
with open({rec!r}, "a") as f:
    f.write(json.dumps(argv) + "\\n")
data = pathlib.Path({data!r})
def out(**kw):
    print(json.dumps(kw), flush=True)
if "--mount-data" in argv:
    mnt = pathlib.Path(argv[argv.index("--mount-data") + 1])
    if mnt.is_symlink() or mnt.exists():
        sys.exit(5)                       # already mounted: the installer must have closed it
    mnt.symlink_to(data)
    out(phase="mount", progress=1, message="Data volume ready", path=str(mnt))
    sys.exit(0)
if "--unmount-data" in argv:
    mnt = pathlib.Path(argv[argv.index("--unmount-data") + 1])
    if mnt.is_symlink():
        mnt.unlink()
    out(phase="mount", progress=1, message="Data volume closed", path=str(mnt))
    sys.exit(0)
for d in ("machines", "config", "downloads"):
    (data / d).mkdir(parents=True, exist_ok=True)
dirs = [argv[i + 1] for i, a in enumerate(argv) if a == "--machine-dir"]
for n, mdir in enumerate(dirs, 1):
    mid = json.loads(pathlib.Path(mdir, "machine.json").read_text())["id"]
    out(phase="machine", progress=n / (len(dirs) + 1), message="Copying", machine_dir=mdir)
    dest = data / "machines" / mid
    dest.mkdir()
    (dest / "machine.json").write_text(pathlib.Path(mdir, "machine.json").read_text())
    out(phase="machine", progress=n / (len(dirs) + 1), message="copied", machine_dir=mdir, machine_id=mid)
seed = argv[argv.index("--seed") + 1]
(data / "config" / "install-seed.json").write_text(pathlib.Path(seed).read_text())
out(phase="finish", progress=1, message="Installation complete")
"""


def embedded(inst: Path, mid: str, name: str, os_version: str = "9.2.2") -> Path:
    d = inst / "embedded/machines" / mid
    d.mkdir(parents=True)
    with open(d / "disk.img", "wb") as f:
        f.truncate(64 * MiB)
        f.seek(MiB)
        f.write(b"\x01" * 4096)
    st = (d / "disk.img").stat()
    (d / "disk.img.extents").write_text(json.dumps({"format": "retroboot-extents-1", "size": st.st_size,
                                                    "mtime": int(st.st_mtime), "extents": [[MiB, 4096]]}))
    (d / "machine.json").write_text(json.dumps({"schema_version": 1, "id": mid, "name": name, "disk": "disk.img",
                                                "os_version": os_version, "source": {"fixture": mid}}))
    return d


def archive_bytes(name: str) -> tuple[bytes, int]:
    cfg = json.dumps({"name": name, "machineFamily": "powerMacG4"}).encode()
    disk = bytearray(2 * MiB)
    disk[4096:4100] = b"MacD"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.GNU_FORMAT) as tf:
        for member, data in (("config.json", cfg), ("disk.img", bytes(disk))):
            ti = tarfile.TarInfo(member)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue(), len(cfg) + len(disk)


def entry(mid: str, name: str, url: str, payload: bytes, installed: int, os_version: str = "9.2.2") -> dict:
    return {"id": mid, "name": name, "summary": f"{name} for tests.", "osVersion": os_version,
            "archiveURL": url, "archiveBytes": len(payload), "installedBytes": installed,
            "sha256": hashlib.sha256(payload).hexdigest()}


class PickerBase(unittest.IsolatedAsyncioTestCase):
    disk_size = 42949672960
    media_auth = True          # retroboot/lab-auth.env with the sign-in (else only the base URL)
    remember = False

    async def asyncSetUp(self):
        from aiohttp import web
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        helpers.make_package(self.root)
        ca_pem, cert_pem, key_pem = lab_ca_and_server_cert("localhost")
        for name, data in (("ca.pem", ca_pem), ("srv.pem", cert_pem), ("srv.key", key_pem)):
            (self.root / name).write_bytes(data)
        # The lab server: /lab/ needs Basic auth, /pub/ doesn't.
        self.token = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
        self.seen: list[tuple[str, str | None]] = []
        self.fail_archive = 0          # answer HTTP 500 for this many archive requests
        self.files: dict[str, bytes] = {}
        self.catalogs: dict[str, str] = {}

        async def handler(request):
            self.seen.append((request.path, request.headers.get("Authorization")))
            if request.path.startswith("/lab/") and request.headers.get("Authorization") != self.token:
                return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="lab"'})
            if request.path in self.catalogs:
                return web.Response(text=self.catalogs[request.path])
            if request.path in self.files:
                if self.fail_archive:
                    self.fail_archive -= 1
                    return web.Response(status=500)
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
        self.base = f"https://localhost:{site._server.sockets[0].getsockname()[1]}"
        payload, installed = archive_bytes("Mac OS 9.2.1")
        self.files["/lab/os921-full-v4.tar.gz"] = payload
        pub, pub_installed = archive_bytes("Mac OS 9")
        self.files["/pub/mac-os-9.tar.gz"] = pub
        lab = [entry("os922-full-v5", "Mac OS 9.2.2 (v5)", f"{self.base}/lab/x5.tar.gz", payload, installed),
               entry("os921-full-v4", "Mac OS 9.2.1 (v4)", f"{self.base}/lab/os921-full-v4.tar.gz", payload,
                     installed, "9.2.1"),
               entry("os922-deluxe-v1", "Mac OS 9.2.2 Deluxe", f"{self.base}/lab/d1.tar.gz", payload, installed),
               entry("os922-deluxe-v2", "Mac OS 9.2.2 Deluxe v2", f"{self.base}/lab/d2.tar.gz", payload, installed),
               entry("os922-deluxe-v4", "Mac OS 9.2.2 Deluxe v4", f"{self.base}/lab/d4.tar.gz", payload, installed),
               dict(entry("big-v1", "Big Mac", f"{self.base}/lab/big.tar.gz", payload, 6 * GiB + 4096),
                    diskCapacityBytes=6 * GiB, requiredStorageBytes=5 * GiB)]
        self.catalogs["/lab/catalog.json"] = json.dumps({"schemaVersion": 1, "machines": lab})
        self.catalogs["/pub/catalog.json"] = json.dumps({"schemaVersion": 1, "machines": [
            entry("mac-os-9", "Mac OS 9", f"{self.base}/pub/mac-os-9.tar.gz", pub, pub_installed, "Mac OS 9.2.1")]})

        self.cfg = helpers.make_config(self.root, mode="installer", catalog_ca_file=str(self.root / "ca.pem"),
                                       catalog_url=f"{self.base}/pub/catalog.json")
        paths = Paths(self.root)
        paths.ensure()
        inst = paths.installer_dir
        embedded(inst, "os922-deluxe-v4", "Mac OS 9.2.2 Deluxe v4")
        embedded(inst, "os922-full-v5", "Mac OS 9.2.2 (v5)")
        (inst / "embedded/machines/order").write_text("os922-deluxe-v4\nos922-full-v5\n")
        medium = paths.run / "live/medium/retroboot"
        medium.mkdir(parents=True)
        text = f"RB_LAB_BASE_URL={self.base}/lab/\n"
        if self.media_auth:
            text += f"RB_LAB_HTTP_USER={USER}\nRB_LAB_HTTP_PASS='{PASSWORD}'\n"
        (medium / "lab-auth.env").write_text(text + "GEMINI_PASS=ignored-other-key\n")
        self.data = self.root / "target-data"
        self.record = self.root / "install-calls.jsonl"
        (inst / "list-disks").write_text(LIST_DISKS.format(py=sys.executable, size=self.disk_size))
        (inst / "install-to-disk").write_text(INSTALL.format(py=sys.executable, rec=str(self.record),
                                                             data=str(self.data)))
        for n in ("list-disks", "install-to-disk"):
            (inst / n).chmod(0o755)
        self.fp = FakePresenter(str(paths.presenter_sock), None)
        await self.fp.start()
        self.app = Hostd(self.cfg)
        self.online = ["192.168.1.50"]

        async def addresses():
            return list(self.online)
        self.app.network.all_addresses = addresses
        self.app.settings.store.load()
        helpers.install_fake_prep(self.app)
        await self.app.start()

    async def asyncTearDown(self):
        await self.app.shutdown()
        await self.fp.stop()
        await self.runner.cleanup()

    # -- helpers ---------------------------------------------------------------------
    async def doc(self, doc_id, contains=None, timeout=10):
        await helpers.wait_for(lambda: (self.fp.doc or {}).get("id") == doc_id and
                               (contains is None or contains in json.dumps(self.fp.doc, ensure_ascii=False)), timeout,
                               what=f"{doc_id} {contains or ''}")

    def items(self, type_):
        def walk(items):
            for i in items:
                yield i
                yield from walk(i.get("items") or []) if i.get("type") in ("columns", "stack", "group") else ()
        return [i for i in walk(self.fp.doc["items"]) if i.get("type") == type_]

    def rows(self):
        return {r["id"]: r for r in self.items("list")[0]["items"]}

    def button(self, bid):
        return next((b for i in self.items("buttons") for b in i["items"] if b["id"] == bid), None)

    async def to_macs(self):
        await self.doc("installer-welcome")
        self.fp.click("continue")
        await self.doc("installer-hardware", "1 disk available")
        self.fp.click("continue")
        await self.doc("installer-destination")
        self.fp.click("continue", {"disk": "/dev/vda"})
        await self.doc("installer-erase")
        self.fp.click("erase")
        await self.doc("installer-macs", "On this disc")

    async def to_install(self, values=None):
        self.fp.click("continue", values or {})
        await self.doc("installer-display")
        self.fp.click("continue")
        await self.doc("timezone")
        self.fp.click("set", {"zone": "Europe/Paris"})
        await self.doc("installer-network")
        self.fp.click("continue")
        await self.doc("installer-ready")
        ready = json.dumps(self.fp.doc, ensure_ascii=False)
        self.fp.click("install")
        return ready

    def calls(self):
        return [json.loads(l) for l in self.record.read_text().splitlines()]

    def seed(self):
        return json.loads((self.data / "config/install-seed.json").read_text())["settings"]

    def assert_sign_in_kept(self):
        env = self.data / "config/lab-auth.env"
        self.assertEqual(oct(env.stat().st_mode & 0o777), "0o600")
        self.assertEqual(labauth.load(env).headers_for(f"{self.base}/lab/catalog.json"), {"Authorization": self.token})
        self.assertEqual(json.loads((self.data / "config/hostd.local.json").read_text()),
                         {"lab_http_auth_file": "/var/lib/retroboot/config/lab-auth.env"})

    def assert_no_secret(self):
        texts = [json.dumps(list(self.app.lifecycle.recent))]
        texts += [f.read_text(errors="replace") for f in self.app.paths.logs.rglob("*") if f.is_file()]
        texts += [json.dumps(d, ensure_ascii=False) for d in self.fp.docs]
        for t in texts:
            self.assertNotIn(PASSWORD, t)
            self.assertNotIn(USER, t)



class PickerTests(PickerBase):
    async def test_two_embedded_and_one_download(self):
        await self.to_macs()
        await self.doc("installer-macs", "Mac OS 9.2.1 (v4)")
        await self.doc("installer-macs", "Internet")
        rows = self.rows()
        # Disc first in build order, checked; the disc's v5 and Deluxe v4 are not offered twice,
        # older Deluxe versions wait behind the checkbox.
        self.assertEqual(list(rows)[:2], ["disc:os922-deluxe-v4", "disc:os922-full-v5"])
        self.assertTrue(rows["disc:os922-deluxe-v4"]["checked"] and rows["disc:os922-full-v5"]["checked"])
        self.assertIn("On this disc", rows["disc:os922-deluxe-v4"]["detail"])
        self.assertEqual(rows["lab:os921-full-v4"]["detail"], "RetroBoot downloads · 2.0 MB")   # name has the OS
        self.assertEqual(rows["catalog:mac-os-9"]["detail"], "Internet · Mac OS 9.2.1 · 2.0 MB")
        self.assertFalse(rows["lab:os921-full-v4"]["checked"])
        self.assertNotIn("lab:os922-full-v5", rows)
        self.assertNotIn("lab:os922-deluxe-v4", rows)
        self.assertNotIn("lab:os922-deluxe-v1", rows)
        self.assertIn("catalog:mac-os-9", rows)
        self.assertEqual(rows["catalog:mac-os-9"]["icon"], "web")
        self.assertEqual(self.items("checkbox")[0]["id"], "older")
        startup = self.items("popup")[0]
        self.assertEqual(startup["value"], "disc:os922-deluxe-v4")
        self.fp.change("older", True)
        await helpers.wait_for(lambda: "lab:os922-deluxe-v1" in self.rows(), 5, what="older versions")
        self.fp.change("older", False)
        await helpers.wait_for(lambda: "lab:os922-deluxe-v1" not in self.rows(), 5, what="older hidden")
        # A click checks a download; Starts first: that download.
        self.fp.click("macs", {"macs": "lab:os921-full-v4"})
        await helpers.wait_for(lambda: self.rows()["lab:os921-full-v4"]["checked"], 5, what="checked")
        self.assertIn("3 Macs", json.dumps(self.fp.doc, ensure_ascii=False))
        self.assertIn("to download during installation", json.dumps(self.fp.doc, ensure_ascii=False))
        ready = await self.to_install({"startup": "lab:os921-full-v4"})
        self.assertIn("downloaded onto the disk", ready)
        await self.doc("installer-done", "Remove the installation disc", 30)
        calls = self.calls()
        main = calls[0]
        inst = str(self.app.paths.installer_dir / "embedded/machines")
        self.assertEqual([main[i + 1] for i, a in enumerate(main) if a == "--machine-dir"],
                         [f"{inst}/os922-deluxe-v4", f"{inst}/os922-full-v5"])
        self.assertIn("--mount-data", calls[1])
        self.assertIn("--unmount-data", calls[2])
        mid = "mac-os-9-2-1-v4"
        meta = json.loads((self.data / "machines" / mid / "machine.json").read_text())
        self.assertEqual((meta["name"], meta["os_version"], meta["source"]["type"], meta["source"]["template_id"]),
                         ("Mac OS 9.2.1 (v4)", "9.2.1", "catalog", "os921-full-v4"))
        self.assertTrue((self.data / "machines" / mid / "disk.img").is_file())
        self.assertEqual(sorted(p.name for p in (self.data / "machines").iterdir()),
                         [mid, "os922-deluxe-v4", "os922-full-v5"])
        self.assertEqual(list((self.data / "downloads").iterdir()), [])      # archive removed after unpacking
        self.assertEqual(self.seed()["machine.active"], mid)
        self.assertEqual(self.seed()["system.timezone"], "Europe/Paris")
        self.assert_sign_in_kept()                     # the medium's sign-in, without asking
        archive = [h for p, h in self.seen if p == "/lab/os921-full-v4.tar.gz"]
        self.assertEqual(archive, [self.token])
        self.assertTrue(all(h is None for p, h in self.seen if p.startswith("/pub/")))
        ev = helpers.events(self.app, "installer_macs_chosen")[0]
        self.assertEqual((ev["count"], ev["sources"], ev["startup"]), (3, ["disc", "lab"], mid))
        self.assertTrue(helpers.events(self.app, "installer_media_lab")[0]["signed_in"])
        self.assert_no_secret()

    async def test_medium_sign_in_is_kept_without_downloads(self):
        """The ISO's sign-in goes onto the new system even when only Macs from the disc are installed."""
        await self.to_macs()
        await self.doc("installer-macs", "Mac OS 9.2.1 (v4)")
        await self.to_install()
        await self.doc("installer-done", "Remove the installation disc", 30)
        calls = self.calls()
        self.assertEqual([("--mount-data" in a, "--unmount-data" in a) for a in calls[1:]],
                         [(True, False), (False, True)])
        self.assert_sign_in_kept()
        self.assertEqual(sorted(p.name for p in (self.data / "machines").iterdir()),
                         ["os922-deluxe-v4", "os922-full-v5"])
        self.assertTrue(helpers.events(self.app, "installer_lab_auth_kept"))
        self.assert_no_secret()

    async def test_download_fails_then_skip(self):
        self.fail_archive = 100
        await self.to_macs()
        await self.doc("installer-macs", "Mac OS 9.2.1 (v4)")
        self.fp.click("macs", {"macs": "lab:os921-full-v4"})
        await helpers.wait_for(lambda: self.rows()["lab:os921-full-v4"]["checked"], 5, what="checked")
        self.fp.click("macs", {"macs": "disc:os922-full-v5"})          # one Mac from the disc only
        await helpers.wait_for(lambda: not self.rows()["disc:os922-full-v5"]["checked"], 5, what="unchecked")
        await self.to_install({"startup": "lab:os921-full-v4"})
        await self.doc("installer-download-problem", "could not be installed", 30)
        text = json.dumps(self.fp.doc, ensure_ascii=False)
        self.assertIn("HTTP 500", text)
        self.assertIn("“Mac OS 9.2.2 Deluxe v4”", text)                 # the other Mac is installed
        self.assertEqual([b["id"] for b in self.items("buttons")[0]["items"]], ["wifi", "skip", "retry"])
        self.assertEqual(self.seed()["machine.active"], "os922-deluxe-v4")   # startup falls back meanwhile
        self.fp.click("retry")
        await helpers.wait_for(lambda: len(self.calls()) >= 5, 20, what="second attempt")
        await self.doc("installer-download-problem", "could not be installed", 30)
        self.fp.click("skip")
        await self.doc("installer-done", "was not installed", 30)
        self.assertEqual(sorted(p.name for p in (self.data / "machines").iterdir()), ["os922-deluxe-v4"])
        self.assertEqual(self.seed()["machine.active"], "os922-deluxe-v4")
        self.assertEqual(list((self.data / "downloads").iterdir()), [])
        self.assertFalse((self.app.paths.run / "install-target").exists())   # closed again
        states = [e["state"] for e in helpers.events(self.app, "installer_mac")]
        self.assertEqual(states, ["failed", "failed"])
        self.assertTrue(helpers.events(self.app, "installer_downloads_skipped"))

    async def test_download_fails_then_try_again_succeeds(self):
        self.fail_archive = 1
        await self.to_macs()
        await self.doc("installer-macs", "Mac OS 9.2.1 (v4)")
        self.fp.click("macs", {"macs": "lab:os921-full-v4"})
        await helpers.wait_for(lambda: self.rows()["lab:os921-full-v4"]["checked"], 5, what="checked")
        await self.to_install({"startup": "lab:os921-full-v4"})
        await self.doc("installer-download-problem", None, 30)
        self.fp.click("retry")
        await self.doc("installer-done", "Remove the installation disc", 30)
        self.assertNotIn("not installed", json.dumps(self.fp.doc, ensure_ascii=False))
        self.assertEqual(self.seed()["machine.active"], "mac-os-9-2-1-v4")
        self.assertEqual(len(list((self.data / "machines").iterdir())), 3)


class PickerSignInTests(PickerBase):
    media_auth = False

    async def test_sign_in_and_remember(self):
        await self.to_macs()
        await self.doc("installer-macs", "Sign in to see these Macs")
        rows = self.rows()
        self.assertTrue(rows["status:lab"]["disabled"])
        self.assertIsNotNone(self.button("signin"))
        self.fp.click("signin")
        await self.doc("installer-signin", "localhost")
        self.assertEqual([f["value"] for f in self.items("field")], ["", ""])
        self.assertTrue(self.items("field")[1]["secure"])
        self.assertTrue(next(i for i in self.items("checkbox") if i["id"] == "remember")["value"])   # on by default
        self.fp.click("signin", {"user": USER, "password": "wrong-password-123", "remember": True})
        await self.doc("installer-signin", "not correct", 20)
        self.fp.click("signin", {"user": USER, "password": PASSWORD, "remember": True})
        await self.doc("installer-macs", "Mac OS 9.2.1 (v4)", 20)
        self.assertIsNone(self.button("signin"))
        self.fp.click("macs", {"macs": "lab:os921-full-v4"})
        await helpers.wait_for(lambda: self.rows()["lab:os921-full-v4"]["checked"], 5, what="checked")
        await self.to_install()
        await self.doc("installer-done", "Remove the installation disc", 30)
        self.assert_sign_in_kept()
        self.assertEqual(self.seed()["machine.active"], "os922-deluxe-v4")   # the default startup Mac
        signins = helpers.events(self.app, "installer_lab_signin")
        self.assertEqual([e["accepted"] for e in signins], [False, True])
        self.assert_no_secret()

    async def test_offline(self):
        self.online = []
        await self.to_macs()
        await self.doc("installer-macs", "Not connected to a network")
        self.assertTrue(self.rows()["status:lab"]["disabled"])
        self.assertIsNotNone(self.button("wifi"))
        self.assertIn("appear when this computer is connected", json.dumps(self.fp.doc, ensure_ascii=False))
        self.online = ["192.168.1.50"]               # Wi-Fi joins: the window notices by itself
        await self.doc("installer-macs", "Sign in to see these Macs", 15)


class PickerSpaceTests(PickerBase):
    disk_size = 9 * GiB

    async def test_space_check(self):
        await self.to_macs()
        await self.doc("installer-macs", "Big Mac")
        self.assertFalse(self.button("continue").get("disabled"))
        self.fp.click("macs", {"macs": "lab:big-v1"})
        await self.doc("installer-macs", "Uncheck a Mac or choose a larger disk")
        self.assertTrue(self.button("continue")["disabled"])
        self.fp.click("continue")                         # refused
        await asyncio.sleep(0.3)
        self.assertEqual(self.fp.doc["id"], "installer-macs")
        # With the keyboard: select the row, then Remove.
        self.fp.change("macs", "lab:big-v1")
        await helpers.wait_for(lambda: self.button("toggle")["label"] == "Remove", 5, what="Remove")
        self.fp.click("toggle")
        await helpers.wait_for(lambda: not self.button("continue").get("disabled"), 5, what="fits again")
        # Unchecking every Mac is refused too.
        for key in ("disc:os922-deluxe-v4", "disc:os922-full-v5"):
            self.fp.click("macs", {"macs": key})
            await helpers.wait_for(lambda k=key: not self.rows()[k]["checked"], 5, what="unchecked")
        await self.doc("installer-macs", "Check at least one Mac")
        self.assertTrue(self.button("continue")["disabled"])


class PickerUnitTests(unittest.TestCase):
    def test_families_and_disc_duplicates(self):
        self.assertEqual(picker.family_of("os922-deluxe-v4"), ("os922-deluxe", 4))
        self.assertEqual(picker.family_of("mac-os-9.2.1-gxmetal-2.3.0-32gb-v2"), ("mac-os-9.2.1-gxmetal-2.3.0-32gb", 2))
        self.assertEqual(picker.family_of("copland"), (None, None))
        e = lambda i, n: {"id": i, "name": n, "osVersion": "9.2.2", "archiveURL": "https://x/a", "archiveBytes": 9,
                          "installedBytes": 4096, "sha256": "0" * 64}
        disc = [picker.Choice("disc:os922-deluxe-v3", "disc", "dir", "Deluxe v3", machine_id="os922-deluxe-v3",
                              family="os922-deluxe", version=3)]
        out = picker.hosted_choices(
            [("lab", [e("os922-full-v4", "v4"), e("os922-deluxe-v2", "D2"), e("os921-full-v4", "921"),
                      e("os922-full-v5", "v5"), e("os922-deluxe-v3", "D3"), e("os922-deluxe-v4", "D4")],
              "https://x/catalog.json", None),
             ("catalog", [e("os921-full-v4", "public copy"), e("mac-os-9", "Mac OS 9")], "https://p/catalog.json", None)],
            disc)
        self.assertEqual([(c.machine_id, c.older) for c in out["lab"]],
                         [("os922-full-v5", False), ("os922-full-v4", True), ("os922-deluxe-v4", False),
                          ("os922-deluxe-v2", True), ("os921-full-v4", False)])
        self.assertEqual([(c.key, c.catalog_url) for c in out["catalog"]], [("catalog:mac-os-9", "https://p/catalog.json")])

    def test_which_sign_in_is_kept(self):
        from retroboot.panel.installer import InstallerState
        st = InstallerState()
        self.assertIsNone(st.remember_auth)
        media = labauth.parse_media("RB_LAB_BASE_URL=https://f.example/lab/\nRB_LAB_HTTP_USER=u\nRB_LAB_HTTP_PASS=pw\n")
        st.media_lab = media
        self.assertIs(st.remember_auth, media.auth)               # always, without asking
        typed = labauth.LabAuth("https://f.example/lab/", "v", "pw2")
        st.lab_auth = typed
        self.assertIs(st.remember_auth, typed)                    # Remember is on by default
        st.lab_remember = False
        self.assertIsNone(st.remember_auth)
        st.media_lab = labauth.parse_media("RB_LAB_BASE_URL=https://f.example/lab/\n")
        st.lab_auth = None
        self.assertIsNone(st.remember_auth)                       # a location only: nothing to keep

    def test_room_for_macs(self):
        self.assertEqual(picker.room_for_macs({"size_bytes": 8 * GiB}), 8 * GiB - picker.FIXED_BYTES - GiB)
        self.assertEqual(picker.room_for_macs({"size_bytes": GiB}), 0)

    def test_old_single_layout_and_archives(self):
        td, root = helpers.scratch()
        self.addCleanup(td.cleanup)
        emb = root / "embedded"
        (emb / "machine").mkdir(parents=True)
        with open(emb / "machine/disk.img", "wb") as f:
            f.truncate(MiB)
        (emb / "machine/config.json").write_text(json.dumps({"name": "Mac OS 9"}))
        out = picker.embedded_choices(emb)
        self.assertEqual([(c.key, c.kind, c.machine_id) for c in out], [("disc:mac-os-9", "dir", "mac-os-9")])


class SeedTests(unittest.IsolatedAsyncioTestCase):
    async def test_global_seed_and_missing_startup_mac(self):
        td, root = helpers.scratch()
        self.addCleanup(td.cleanup)
        helpers.make_machine(root, "os922-deluxe-v4", "Deluxe")
        helpers.make_machine(root, "os922-full-v5", "Standard")
        seed = root / "var/lib/retroboot/config/install-seed.json"
        seed.parent.mkdir(parents=True, exist_ok=True)
        seed.write_text(json.dumps({"settings": {"system.timezone": "Asia/Tokyo", "machine.active": "os922-full-v5"}}))
        app = Hostd(helpers.make_config(root))
        app.settings.store.load()
        app.loop = asyncio.get_running_loop()
        app._seed_from_install()
        self.assertEqual(app.settings.applied["machine.active"], "os922-full-v5")
        self.assertEqual(app.settings.applied["system.timezone"], "Asia/Tokyo")
        self.assertTrue(seed.with_name("install-seed.applied.json").exists())
        # A seed naming a Mac that was never installed (a skipped download): the first Mac starts.
        td2, root2 = helpers.scratch()
        self.addCleanup(td2.cleanup)
        helpers.make_machine(root2, "os922-deluxe-v4", "Deluxe")
        seed2 = root2 / "var/lib/retroboot/config/install-seed.json"
        seed2.parent.mkdir(parents=True, exist_ok=True)
        seed2.write_text(json.dumps({"settings": {"machine.active": "mac-os-9-2-1-v4"}}))
        app2 = Hostd(helpers.make_config(root2))
        app2.settings.store.load()
        app2.loop = asyncio.get_running_loop()
        app2._seed_from_install()
        self.assertEqual(app2.settings.applied["machine.active"], "os922-deluxe-v4")


class LabAuthMediaTests(unittest.TestCase):
    def test_media_file(self):
        base = "https://files.example/lab/"
        m = labauth.parse_media(f"RB_LAB_BASE_URL={base}\nRB_LAB_HTTP_USER=u\nRB_LAB_HTTP_PASS='p w:x'\n"
                                "RB_LAB_REMEMBER=yes\nOTHER_SECRET=zzz\n")
        self.assertEqual((m.base_url, m.remember), (base, True))
        self.assertEqual(m.auth.headers_for(base + "catalog.json"),
                         {"Authorization": "Basic " + base64.b64encode(b"u:p w:x").decode()})
        self.assertNotIn("p w:x", repr(m))
        only = labauth.parse_media(f"RB_LAB_BASE_URL={base}\n")
        self.assertIsNone(only.auth)
        self.assertFalse(only.remember)
        for bad, why in (("RB_LAB_HTTP_USER=zq9user\n", "missing"),
                         (f"RB_LAB_BASE_URL={base}\nRB_LAB_HTTP_USER=zq9user\n", "together"),
                         ("RB_LAB_BASE_URL=http://zq9host.example/lab/\n", "https")):
            with self.assertRaises(labauth.LabAuthError) as cm:
                labauth.parse_media(bad)
            self.assertIn(why, str(cm.exception))
            self.assertNotIn("zq9", str(cm.exception))      # never a value
        self.assertEqual(labauth.catalog_url(base), base + "catalog.json")
        self.assertEqual(labauth.catalog_url("https://f.example/lab"), "https://f.example/lab/catalog.json")

    def test_env_round_trip(self):
        td, root = helpers.scratch()
        self.addCleanup(td.cleanup)
        for pw in ("plain", "with 'quote' inside", "'leading", "ends'", 'dq"x"'):
            auth = labauth.LabAuth("https://files.example/lab/", "user name", pw)
            path = root / "cfg" / "lab-auth.env"
            labauth.write_env(path, auth)
            self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
            back = labauth.load(path)
            self.assertEqual(back.headers_for("https://files.example/lab/x"),
                             auth.headers_for("https://files.example/lab/x"), pw)


if __name__ == "__main__":
    unittest.main()
