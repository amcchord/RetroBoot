"""Joining Wi-Fi from the local panel (controller.WiFi / WiFiJoin) against the fake presenter.

The real hostd runs with a scripted Wi-Fi adapter.  Besides the flow (one click on a
network joins it, the joined network is checked and listed first, an explicit result
before returning), these tests prove the password never appears in a panel document,
a log record or any file hostd writes: "Show password" is the presenter's own business.
"""
import asyncio
import json
import logging

import helpers
from retroboot.network import NetworkError
from retroboot.panel.controller import WiFi
from test_lifecycle import Base

SECRET = "correct horse 7Q!x"
WRONG = "not-the-password-9"


class FakeWifi:
    def __init__(self):
        self.current = "HomeNet"
        self.joins: list[tuple[str, str | None]] = []
        self.nets = [("HomeNet", 82, "WPA2"), ("Coffee Shop", 64, ""), ("NETGEAR-5G", 48, "WPA2 WPA3"),
                     ("Neighbor’s Network", 30, "WPA2")]

    def install(self, net) -> None:
        net.wifi_device = self.wifi_device
        net.wifi_state = self.wifi_state
        net.scan = self.scan
        net.connect = self.connect

    async def wifi_device(self):
        return "wlan0"

    def entries(self):
        return [{"id": s, "ssid": s, "signal": sig, "security": sec or "open", "in_use": s == self.current,
                 "active": s == self.current, "channel": "6"} for s, sig, sec in self.nets]

    async def wifi_state(self):
        name = f"RetroBoot {self.current}" if self.current else None
        return {"available": True, "device": "wlan0", "connected": name, "ssid": self.current, "profiles": [],
                "networks": self.entries(), "radio": True, "state": "connected" if name else "disconnected",
                "scanned_at": None}

    async def scan(self):
        await asyncio.sleep(0.05)
        return sorted(self.entries(), key=lambda n: (-n["in_use"], -n["signal"], n["ssid"]))

    async def connect(self, ssid, psk, hidden=False, key_mgmt="wpa-psk"):
        self.joins.append((ssid, psk))
        await asyncio.sleep(0.3)
        if psk == WRONG:
            raise NetworkError("The password was not accepted. Check it and try again.", "join_failed")
        self.current = ssid
        return {"ssid": ssid, "profile": "00000000-0000-0000-0000-000000000000"}


class Records(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(self.format(record))


class WifiPanel(Base):
    machine = False

    def before_start(self):
        self.wifi = FakeWifi()
        self.wifi.install(self.app.network)
        self.records = Records()
        root = logging.getLogger()
        self.addCleanup(root.removeHandler, self.records)
        self.addCleanup(root.setLevel, root.level)
        root.addHandler(self.records)
        root.setLevel(logging.DEBUG)

    # -- helpers ------------------------------------------------------------------------
    def items(self, doc=None, kind=None):
        out = []

        def walk(items):
            for it in items or []:
                out.append(it)
                walk(it.get("items") if it.get("type") != "list" else None)
        walk((doc or self.fp.doc or {}).get("items"))
        return [i for i in out if kind is None or i.get("type") == kind]

    def network_list(self):
        lists = self.items(kind="list")
        return lists[0] if lists else None

    def buttons(self):
        rows = self.items(kind="buttons")
        return {b["id"]: b for b in rows[-1]["items"]} if rows else {}

    def texts(self):
        return [i["text"] for i in self.items(kind="text")]

    async def open_wifi(self):
        await self.until_doc("setup-welcome")
        await self.app.panel.show(WiFi(self.app.panel, back=self.app.panel.show_setup))
        await self.until_doc("wifi")
        await helpers.wait_for(lambda: self.network_list() and len(self.network_list()["items"]) == 4, 5,
                               what="networks listed")

    async def until_text(self, text, timeout=10.0):
        await helpers.wait_for(lambda: any(text in t for t in self.texts()), timeout, what=text)

    def assert_secret_kept(self, *secrets):
        shown = json.dumps(self.fp.docs, ensure_ascii=False) + json.dumps(self.fp.requests, ensure_ascii=False)
        logs = "\n".join(self.records.lines)
        files = b"".join(p.read_bytes() for p in self.root.rglob("*") if p.is_file() and not p.is_symlink()
                         and p.stat().st_size < 50_000_000)
        for s in secrets:
            self.assertNotIn(s, shown, "a password was put into a panel document")
            self.assertNotIn(s, logs, "a password was logged")
            self.assertNotIn(s.encode(), files, "a password was written to a file")

    # -- tests ------------------------------------------------------------------------------
    async def test_joined_network_is_checked_first_and_one_click_opens_the_password_dialog(self):
        await self.open_wifi()
        lst = self.network_list()
        self.assertEqual(lst["activate"], "click")
        self.assertIsNone(lst["value"])
        rows = lst["items"]
        self.assertEqual([r["id"] for r in rows], ["HomeNet", "Coffee Shop", "NETGEAR-5G", "Neighbor’s Network"])
        self.assertEqual([bool(r.get("checked")) for r in rows], [True, False, False, False])
        self.assertEqual([bool(r.get("lock")) for r in rows], [True, False, True, True])
        self.assertEqual([r["signal"] for r in rows], [4, 3, 2, 2])
        self.assertIn("Connected to “HomeNet”.", self.texts())
        self.assertIn("Click a network to join it.", self.texts())
        self.assertEqual(list(self.buttons()), ["scan", "cancel"])
        self.assertTrue(self.buttons()["cancel"].get("default"))

        # A network chosen with the keyboard: the default button joins it, Done is no longer default.
        self.fp.change("network", "NETGEAR-5G")
        await helpers.wait_for(lambda: "join" in self.buttons(), 5, what="Join… button")
        self.assertEqual(self.buttons()["join"], {"id": "join", "label": "Join…", "default": True})
        self.assertNotIn("default", self.buttons()["cancel"])

        # One click on a secured network: the password dialog, empty, with "Show password" off.
        self.fp.click("network", {"network": "NETGEAR-5G"})
        await self.until_doc("wifi-join")
        doc = self.fp.doc
        self.assertEqual(doc["focus"], "psk")
        field = self.items(doc, "field")[0]
        self.assertEqual((field["id"], field["secure"], field["value"]), ("psk", True, ""))
        show = self.items(doc, "checkbox")[0]
        self.assertEqual((show["id"], show["label"], show["value"], show["reveals"]),
                         ("show", "Show password", False, "psk"))
        self.assertIn("The network “NETGEAR-5G” requires a password.", self.texts())

        # Typing and showing the password are the presenter's; hostd sends nothing back.
        sent = len(self.fp.requests_of("ui_show"))
        for i in range(1, len(SECRET) + 1):
            self.fp.emit({"event": "ui_change", "doc": "wifi-join", "id": "psk", "value": SECRET[:i], "secure": True})
        self.fp.change("show", True)
        await asyncio.sleep(0.3)
        self.assertEqual(len(self.fp.requests_of("ui_show")), sent, "no document while typing or showing")

        # Too short: said in the dialog, which keeps what was typed (same document, no reset).
        self.fp.click("join", {"psk": "short", "show": True})
        await self.until_text("8 to 63 characters")
        self.assertNotIn("reset", self.fp.doc)
        self.assertEqual(self.wifi.joins, [])

        self.fp.click("join", {"psk": SECRET, "show": True})
        await self.until_doc("message", 10)
        self.assertEqual(self.fp.doc["items"][0]["text"], "Connected to “NETGEAR-5G”.")
        self.assertEqual(self.wifi.joins, [("NETGEAR-5G", SECRET)])
        self.assertIn("job-progress", [d["id"] for d in self.fp.docs])

        # Back to the list: the joined network is checked and first.
        self.fp.click("ok")
        await self.until_doc("wifi")
        await helpers.wait_for(lambda: self.network_list() and self.network_list()["items"][0]["id"] == "NETGEAR-5G",
                               5, what="joined network first")
        rows = self.network_list()["items"]
        self.assertEqual([r["id"] for r in rows if r.get("checked")], ["NETGEAR-5G"])
        self.assertIn("Connected to “NETGEAR-5G”.", self.texts())
        self.assert_secret_kept(SECRET)

    async def test_open_network_joins_at_once_with_progress_and_a_result(self):
        await self.open_wifi()
        self.fp.change("network", "Coffee Shop")
        self.fp.click("network", {"network": "Coffee Shop"})
        await self.until_doc("job-progress")
        self.assertIn("Joining “Coffee Shop”…", self.texts())
        await self.until_doc("message", 10)
        self.assertEqual(self.fp.doc["alert_icon"], "note")
        self.assertEqual(self.fp.doc["items"][0]["text"], "Connected to “Coffee Shop”.")
        self.assertEqual(self.wifi.joins, [("Coffee Shop", None)])
        self.assertNotIn("wifi-join", [d["id"] for d in self.fp.docs])
        self.fp.click("ok")
        await self.until_doc("wifi")
        await helpers.wait_for(lambda: self.network_list()["items"][0].get("checked")
                               and self.network_list()["items"][0]["id"] == "Coffee Shop", 5, what="checked")

    async def test_wrong_password_says_so_and_asks_again(self):
        await self.open_wifi()
        self.fp.click("network", {"network": "Neighbor’s Network"})
        await self.until_doc("wifi-join")
        self.fp.click("join", {"psk": WRONG, "show": False})
        await self.until_doc("message", 10)
        self.assertEqual(self.fp.doc["alert_icon"], "caution")
        self.assertEqual(self.texts()[:2], ["Could not join “Neighbor’s Network”.",
                                            "The password was not accepted. Check it and try again."])
        self.fp.click("ok")
        await self.until_doc("wifi-join")
        self.assertEqual(self.items(kind="field")[0]["value"], "")
        # Cancel: back to the list with that network still selected, so Return tries it again.
        self.fp.click("cancel")
        await self.until_doc("wifi")
        self.assertEqual(self.network_list()["value"], "Neighbor’s Network")
        self.assertTrue(self.buttons()["join"]["default"])
        self.assertIn("Connected to “HomeNet”.", self.texts())
        self.fp.click("cancel", {"network": "Neighbor’s Network"})
        await self.until_doc("setup-welcome")
        self.assert_secret_kept(WRONG)

    async def test_clicking_the_joined_network_changes_nothing(self):
        await self.open_wifi()
        self.fp.change("network", "HomeNet")
        self.fp.click("network", {"network": "HomeNet"})
        await asyncio.sleep(0.4)
        self.assertEqual(self.fp.doc["id"], "wifi")
        self.assertEqual(self.wifi.joins, [])
        self.assertTrue(self.buttons()["cancel"].get("default"))
        self.assertNotIn("join", self.buttons())
