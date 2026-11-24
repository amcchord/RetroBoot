"""Modern Web Helper in hostd: settings, the on/off switch, config, systemd control, API."""
import asyncio
import importlib.util
import json
import socket
import sys
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

import helpers
from retroboot import schema, webproxy
from retroboot.hostd import Hostd
from test_lifecycle import Base

REPO_WEBPROXY = Path(__file__).resolve().parents[2] / "webproxy"
HAVE_WEBPROXY_DEPS = importlib.util.find_spec("lxml") is not None


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class SchemaTests(unittest.TestCase):
    def test_defaults(self):
        d = schema.defaults()
        self.assertEqual(d["network.web_proxy"], "on")
        self.assertEqual(d["network.web_proxy_view"], "auto")
        self.assertTrue(d["network.web_proxy_homepages"])
        self.assertTrue(d["network.web_proxy_lan"])
        self.assertEqual(d["network.web_proxy_search"], "frogfind")
        hosts = {e["host"] for e in d["network.web_proxy_redirects"]}
        self.assertTrue({"livepage.apple.com", "www.excite.com", "home.netscape.com", "www.msn.com",
                         "home.microsoft.com", "ie.search.msn.com", "keyword.netscape.com"} <= hosts)
        self.assertNotIn("www.apple.com", hosts)               # real sites are translated, not redirected
        self.assertEqual(schema.validate_redirects(d["network.web_proxy_redirects"]), d["network.web_proxy_redirects"])

    def test_redirect_validation(self):
        ok = schema.validate_redirects([{"host": "Home.Netscape.COM.", "path": "", "to": "http://system7today.com/"},
                                        {"host": "www.excite.com", "path": "/apple", "to": "search"}])
        self.assertEqual(ok[0]["host"], "home.netscape.com")
        bad = [
            ("not a list", "list"),
            ([{"host": "bad host", "to": "search"}], "host name"),
            ([{"host": "a.example", "path": "nope", "to": "search"}], "starts with /"),
            ([{"host": "a.example", "to": "javascript:alert(1)"}], "http://"),
            ([{"host": "a.example", "to": "file:///etc/passwd"}], "http://"),
            ([{"host": "a.example", "to": "search", "extra": 1}], "not valid"),
            ([{"host": "a.example", "to": "search"}, {"host": "a.example", "to": "http://x.example/"}], "already"),
            ([{"host": f"h{i}.example", "to": "search"} for i in range(65)], "Up to 64"),
        ]
        for value, msg in bad:
            with self.assertRaises(ValueError) as cm:
                schema.validate_redirects(value)
            self.assertIn(msg, str(cm.exception))

    def test_described_as_list(self):
        d = {x["key"]: x for x in schema.describe()}
        self.assertEqual(d["network.web_proxy_redirects"]["type"], "list")
        self.assertEqual(d["network.web_proxy"]["apply"], "live")
        self.assertEqual(d["network.web_proxy"]["label"], "Modern Web Helper")


class FakeSystemctl:
    def __init__(self, active="inactive", nft_ok=True):
        self.active = active
        self.nft_ok = nft_ok
        self.calls = []

    async def __call__(self, cmd, timeout=None, **kw):
        self.calls.append(list(cmd))
        if cmd[:2] == ["systemctl", "is-active"]:
            return (0 if self.active == "active" else 3), self.active + "\n", ""
        if cmd[:2] == ["systemctl", "start"]:
            self.active = "active"
        if cmd[:2] == ["systemctl", "stop"]:
            self.active = "inactive"
        if cmd[0] == "nft":
            return (0 if self.nft_ok else 1), "", ""
        return 0, "", ""


class SystemdControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        helpers.make_package(self.root)
        self.cfg = helpers.make_config(self.root, webproxy_runner="systemd", webproxy_path=str(REPO_WEBPROXY),
                                       run_as_user="nobody")
        self.app = Hostd(self.cfg)
        self.app.loop = asyncio.get_running_loop()
        self.app.settings.store.load()
        self.fake = FakeSystemctl()
        self._orig = webproxy.run_cmd
        webproxy.run_cmd = self.fake

    async def asyncTearDown(self):
        webproxy.run_cmd = self._orig

    async def test_on_starts_change_reloads_off_stops(self):
        wp = self.app.webproxy
        self.assertTrue(wp.available())
        await wp.sync("test")
        self.assertIn(["systemctl", "start", webproxy.UNIT], self.fake.calls)
        self.assertEqual((wp.state, wp.intercepting), ("running", True))
        conf = json.loads(wp.config_path.read_text())
        self.assertEqual(conf["view"], "auto")
        self.assertTrue(conf["intercept"])
        self.assertEqual(conf["intercept_user"], "nobody")
        self.assertEqual(conf["guest_uids"], [conf["intercept_uid"]])
        self.assertIn({"host": "livepage.apple.com", "path": "", "to": "http://system7today.com/"}, conf["redirects"])
        self.fake.calls.clear()
        await self.app.apply_engine.change_and_apply({"network.web_proxy_view": "simplified"}, origin="web")
        self.assertIn(["systemctl", "reload", webproxy.UNIT], self.fake.calls)
        self.assertEqual(json.loads(wp.config_path.read_text())["view"], "simplified")
        self.fake.calls.clear()
        await self.app.apply_engine.change_and_apply({"network.web_proxy": "off"}, origin="panel")
        self.assertIn(["systemctl", "stop", webproxy.UNIT], self.fake.calls)
        self.assertIn(["nft", "delete", "table", "inet", webproxy.TABLE], self.fake.calls)
        self.assertEqual(wp.state, "off")
        self.assertFalse(self.app.supervisor.active)      # nothing about Mac OS changed

    async def test_failed_redirect_is_reported(self):
        self.fake.nft_ok = False
        await self.app.webproxy.sync("test")
        self.assertEqual(self.app.webproxy.public()["intercepting"], False)


@unittest.skipUnless(HAVE_WEBPROXY_DEPS, "rbwebproxy dependencies (lxml) not installed")
class ChildProcessTests(unittest.IsolatedAsyncioTestCase):
    """The real helper as a child (development runner), driven by settings."""

    async def asyncSetUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        helpers.make_package(self.root)
        self.port = free_port()
        self.cfg = helpers.make_config(self.root, webproxy_runner="subprocess", webproxy_path=str(REPO_WEBPROXY),
                                       webproxy_port=self.port, webproxy_dns_port=free_port())
        self.app = Hostd(self.cfg)
        self.app.loop = asyncio.get_running_loop()
        self.app.settings.store.load()

    async def asyncTearDown(self):
        await self.app.webproxy.stop()

    async def ask(self, host: str) -> bytes:
        r, w = await asyncio.open_connection("127.0.0.1", self.port)
        w.write(f"GET / HTTP/1.0\r\nHost: {host}\r\n\r\n".encode())
        data = await asyncio.wait_for(r.read(), 10)
        w.close()
        return data

    async def test_switch_takes_effect_live(self):
        wp = self.app.webproxy
        await wp.sync("test")
        await helpers.wait_for(lambda: self._listening(), 10, 0.1, "helper listening")
        reply = await self.ask("livepage.apple.com")
        self.assertIn(b"302", reply.split(b"\r\n", 1)[0])
        self.assertIn(b"Location: http://system7today.com/", reply)
        reply = await self.ask("127.0.0.1")
        self.assertIn(b" 403 ", reply.split(b"\r\n", 1)[0])
        # Redirect rule changes reload the helper via SIGHUP without hitting the external internet.
        await self.app.apply_engine.change_and_apply(
            {"network.web_proxy_redirects": [{"host": "offline-test.invalid", "path": "", "to": "search"}]},
            origin="web")
        reply = await self.ask("offline-test.invalid")
        self.assertIn(b"302", reply.split(b"\r\n", 1)[0])
        self.assertIn(b"frogfind", reply)
        # Turning home pages off reloads via SIGHUP and disables the redirect.
        await self.app.apply_engine.change_and_apply({"network.web_proxy_homepages": False}, origin="web")
        reply = await self.ask("offline-test.invalid")
        self.assertNotIn(b"frogfind", reply)
        await self.app.apply_engine.change_and_apply({"network.web_proxy": "off"}, origin="web")
        await helpers.wait_for(lambda: not self._listening(), 10, 0.1, "helper stopped")
        self.assertEqual(wp.state, "off")

    def _listening(self) -> bool:
        s = socket.socket()
        try:
            s.connect(("127.0.0.1", self.port))
            return True
        except OSError:
            return False
        finally:
            s.close()


class SubprocessSyncTests(unittest.IsolatedAsyncioTestCase):
    """Stdlib-only subprocess lifecycle test for WebHelper start, SIGHUP reload, and stop."""

    async def test_subprocess_start_sighup_and_stop(self):
        td, root = helpers.scratch()
        self.addCleanup(td.cleanup)
        helpers.make_package(root)
        fake_lib = root / "fake_webproxy"
        pkg = fake_lib / "rbwebproxy"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "__main__.py").write_text(
            "import argparse, json, pathlib, signal, time\n"
            "ap = argparse.ArgumentParser(); ap.add_argument('--config')\n"
            "a = ap.parse_args(); cfg = json.loads(pathlib.Path(a.config).read_text())\n"
            "st = pathlib.Path(cfg['status_path']); st.parent.mkdir(parents=True, exist_ok=True)\n"
            "events = st.parent / 'sighup.log'; running = True\n"
            "def on_hup(*_): events.write_text((events.read_text() if events.exists() else '') + 'hup\\n')\n"
            "def on_term(*_): global running; running = False\n"
            "signal.signal(signal.SIGHUP, on_hup); signal.signal(signal.SIGTERM, on_term)\n"
            "st.write_text(json.dumps({'counts': {}, 'started': 1, 'version': 'test'}))\n"
            "while running: time.sleep(0.05)\n"
        )
        cfg = helpers.make_config(root, webproxy_runner="subprocess", webproxy_path=str(fake_lib),
                                  webproxy_port=free_port(), webproxy_dns_port=free_port())
        app = Hostd(cfg)
        app.loop = asyncio.get_running_loop()
        app.settings.store.load()
        self.addAsyncCleanup(app.webproxy.stop)
        await app.webproxy.sync("start")
        self.assertEqual(app.webproxy.state, "running")
        self.assertEqual(app.webproxy.stats.get("version"), "test")
        await app.apply_engine.change_and_apply({"network.web_proxy_view": "simplified"}, origin="web")
        hup_log = app.paths.run / "sighup.log"
        await helpers.wait_for(lambda: hup_log.exists() and "hup" in hup_log.read_text(), 5, 0.05, "SIGHUP delivered")
        await app.apply_engine.change_and_apply({"network.web_proxy": "off"}, origin="web")
        self.assertEqual(app.webproxy.state, "off")


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        helpers.make_package(self.root)
        self.cfg = helpers.make_config(self.root, webproxy_runner="systemd", webproxy_path=str(REPO_WEBPROXY))
        self.app = Hostd(self.cfg)
        self.app.loop = asyncio.get_running_loop()
        self.app.settings.store.load()
        self.fake = FakeSystemctl()
        self._orig = webproxy.run_cmd
        webproxy.run_cmd = self.fake
        self.app.web.tls()
        self.client = TestClient(TestServer(self.app.web.web_app, host="127.0.0.1"))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        code, _ = self.app.pairing.current_code()
        r = await self.client.post("/api/v1/pair", json={"code": code, "device_name": "t"})
        self.cookie = r.headers.getall("Set-Cookie")[0].split("rb_session=", 1)[1].split(";", 1)[0]
        self.csrf = (await r.json())["csrf"]

    async def asyncTearDown(self):
        webproxy.run_cmd = self._orig

    def h(self):
        return {"Cookie": f"rb_session={self.cookie}", "X-RetroBoot-CSRF": self.csrf}

    async def test_patch_validation_and_status(self):
        r = await self.client.get("/api/v1/settings", headers=self.h())
        body = await r.json()
        rev = body["revision"]
        r = await self.client.patch("/api/v1/settings", headers=self.h(), json={
            "revision": rev, "changes": {"network.web_proxy_redirects": [{"host": "bad host", "to": "search"}]}})
        self.assertEqual(r.status, 422)
        self.assertIn("host name", (await r.json())["error"]["fields"]["network.web_proxy_redirects"])
        table = [{"host": "home.netscape.com", "path": "", "to": "http://68k.news/"}]
        r = await self.client.patch("/api/v1/settings", headers=self.h(), json={
            "revision": rev, "changes": {"network.web_proxy_redirects": table, "network.web_proxy_search": "wiby"}})
        self.assertEqual(r.status, 200, await r.text())
        r = await self.client.post("/api/v1/settings/apply", headers=self.h(),
                                   json={"revision": (await r.json())["revision"], "restart": False})
        self.assertIn(r.status, (200, 202))
        await helpers.wait_for(lambda: self.app.webproxy.config_path.exists() and
                               json.loads(self.app.webproxy.config_path.read_text())["search"] == "wiby",
                               10, 0.05, "config written")
        conf = json.loads(self.app.webproxy.config_path.read_text())
        self.assertEqual(conf["redirects"], table)
        r = await self.client.get("/api/v1/status", headers=self.h())
        st = (await r.json())["web_proxy"]
        self.assertEqual((st["available"], st["enabled"], st["search"]), (True, True, "wiby"))
        r = await self.client.get("/api/v1/network", headers=self.h())
        self.assertIn("web_proxy", await r.json())


class PanelTests(Base):
    """The local classic panel: Recovery > Network & Web Access > Web Helper…"""

    cfg_overrides = {"webproxy_runner": "systemd", "webproxy_path": str(REPO_WEBPROXY)}

    def before_start(self) -> None:
        self.fake = FakeSystemctl()
        self._orig = webproxy.run_cmd
        webproxy.run_cmd = self.fake
        self.addCleanup(lambda: setattr(webproxy, "run_cmd", self._orig))

    async def test_switch_and_view_from_the_panel(self):
        await self.running()
        self.assertIn(["systemctl", "start", webproxy.UNIT], self.fake.calls)     # on by default
        self.fp.emit({"event": "chord", "name": "recovery"})
        await self.until_doc("recovery-main")
        self.fp.click("network")
        await self.until_doc("network-web")
        self.fp.click("webhelper")
        await self.until_doc("web-helper")
        doc = json.dumps(self.fp.doc, ensure_ascii=False)
        self.assertIn("Modern Web Helper is on.", doc)
        self.assertIn("Use the Modern Web Helper", doc)
        self.fp.click("ok", {"network.web_proxy_view": "simplified"})
        await self.until_doc("network-web")
        await helpers.wait_for(lambda: self.app.settings.applied["network.web_proxy_view"] == "simplified", 5,
                               what="view applied")
        self.assertIn(["systemctl", "reload", webproxy.UNIT], self.fake.calls)
        self.fp.click("webhelper")
        await self.until_doc("web-helper")
        self.fp.change("network.web_proxy", False)
        self.fp.click("ok")
        await self.until_doc("network-web")
        await helpers.wait_for(lambda: self.app.settings.applied["network.web_proxy"] == "off", 5, what="off")
        self.assertIn(["systemctl", "stop", webproxy.UNIT], self.fake.calls)
        self.assertEqual(self.app.supervisor.state, "running")                  # Mac OS kept running
        self.fp.click("webhelper")
        await self.until_doc("web-helper")
        self.assertIn("Modern Web Helper is off.", json.dumps(self.fp.doc, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
