"""Bluetooth input devices (retroboot/bluetooth.py, panel/bluetooth.py, web/bluetooth_api.py).

The real hostd runs against the fake presenter with a scripted BlueZ
(``FakeBackend``): the radio follows ``bluetooth.enabled`` (with rfkill),
the Recovery/Setup windows list and pair devices, a keyboard's passkey and a
legacy keyboard's PIN appear large on the panel and are typed by the "device",
numeric comparison, failures with their reason, cancel, forget, battery,
reconnects without a restart (and after bluetoothd restarts), the setup
assistant with no keyboard attached (never over the Mac OS startup: an offer
after the Finder, a search only with no keyboard and no pointer, Not Now
until a keyboard change, the setting that turns it off), and the web API (the
code only for the browser that started the pairing). Codes never reach a log
record or a file.
"""
import asyncio
import json
import logging
import unittest

from aiohttp.test_utils import TestClient, TestServer

import helpers
from retroboot import schema
from retroboot.bluetooth import (AgentRejected, AgentRequest, Bluetooth, BluetoothError, FakeBackend, FakeDevice,
                                 InputDevices, Rfkill, classify, message_for, signal_bars)
from retroboot.hostd import Hostd
from test_lifecycle import Base

KB = "C8:2B:96:1A:4E:01"      # keyboard: passkey (Secure Simple Pairing)
MOUSE = "C8:2B:96:1A:4E:02"   # LE mouse: Just Works
OLDKB = "C8:2B:96:1A:4E:03"   # legacy keyboard: PIN shown, typed on it
PAD = "C8:2B:96:1A:4E:05"     # paired trackpad
OLDMOUSE = "C8:2B:96:1A:4E:06"  # legacy mouse with the fixed PIN 0000
PHONE = "C8:2B:96:1A:4E:07"   # numeric comparison
GONE = "C8:2B:96:1A:4E:08"    # switched off after the scan


def devices():
    return [
        FakeDevice(KB, "Bluetooth Keyboard", cls=0x002540, method="passkey", battery=90, rssi=-50),
        FakeDevice(MOUSE, "Wireless Mouse", appearance=0x03C2, method="justworks", le=True, battery=64, rssi=-66),
        FakeDevice(OLDKB, "Old Keyboard", cls=0x000540, method="pin", rssi=-75),
        FakeDevice(PAD, "Magic Trackpad", cls=0x002580, method="justworks", paired=True, trusted=True,
                   connected=True, discoverable=False, battery=41),
        FakeDevice(OLDMOUSE, "Old Mouse", cls=0x000580, method="pin", pin="0000", rssi=-81),
        FakeDevice(PHONE, "Keyboard Cover", cls=0x0025C0, method="confirm", rssi=-62),
        FakeDevice(GONE, "Travel Keyboard", cls=0x002540, method="unreachable", rssi=-85),
        FakeDevice("C8:2B:96:1A:4E:09", "Someone's Phone", cls=0x5A020C, rssi=-40, hid=False),   # not input
    ]


ASSISTANT_DOCS = ("bluetooth-offer", "bluetooth-assistant", "bluetooth-pair")


def bitmap(*bits):
    v = 0
    for b in bits:
        v |= 1 << b
    words = []
    while True:
        words.append(v & (2 ** 64 - 1))
        v >>= 64
        if not v:
            break
    return " ".join(f"{w:x}" for w in reversed(words))


def add_input(root, n, name, bus, kind, uniq=""):
    d = root / "sys/class/input" / f"input{n}"
    (d / "capabilities").mkdir(parents=True)
    (d / "id").mkdir()
    (d / "name").write_text(name + "\n")
    (d / "uniq").write_text(uniq + "\n")
    (d / "id" / "bustype").write_text(f"{bus:04x}\n")
    caps = {"ev": bitmap(0), "key": "0", "rel": "0", "abs": "0"}
    if kind == "keyboard":
        caps.update(ev=bitmap(0, 1, 4, 17, 20), key=bitmap(1, 28, 30, 44, 57, 100))
    elif kind == "mouse":
        caps.update(ev=bitmap(0, 1, 2), key=bitmap(0x110, 0x111), rel=bitmap(0, 1, 8))
    elif kind == "touchpad":
        caps.update(ev=bitmap(0, 1, 3), key=bitmap(0x110, 0x145, 0x14A), abs=bitmap(0, 1, 0x35, 0x36))
    elif kind == "power":
        caps.update(ev=bitmap(0, 1), key=bitmap(116))
    for k, v in caps.items():
        (d / "capabilities" / k).write_text(v + "\n")
    return d


def add_rfkill(root, soft="0", hard="0"):
    d = root / "sys/class/rfkill/rfkill0"
    d.mkdir(parents=True)
    for k, v in (("type", "bluetooth"), ("name", "hci0"), ("soft", soft), ("hard", hard)):
        (d / k).write_text(v + "\n")
    w = root / "sys/class/rfkill/rfkill1"
    w.mkdir(parents=True)
    for k, v in (("type", "wlan"), ("name", "phy0"), ("soft", "0"), ("hard", "0")):
        (w / k).write_text(v + "\n")
    return d


class Records(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


# --- pure functions ---------------------------------------------------------------------

class Classification(unittest.TestCase):
    def test_kinds_from_class_appearance_and_names(self):
        self.assertEqual(classify({"Class": 0x002540}), "keyboard")
        self.assertEqual(classify({"Class": 0x002580}), "mouse")
        self.assertEqual(classify({"Class": 0x002580, "Name": "Magic Trackpad 2"}), "trackpad")
        self.assertEqual(classify({"Class": 0x0025C0}), "combo")
        self.assertEqual(classify({"Class": 0x000508}), "gamepad")
        self.assertEqual(classify({"Class": 0x000504}), "gamepad")
        self.assertEqual(classify({"Class": 0x000594}), "tablet")
        self.assertEqual(classify({"Appearance": 0x03C1}), "keyboard")
        self.assertEqual(classify({"Appearance": 0x03C2}), "mouse")
        self.assertEqual(classify({"Appearance": 0x03C4}), "gamepad")
        self.assertEqual(classify({"Appearance": 0x03C9}), "trackpad")
        self.assertEqual(classify({"Appearance": 0x03C0, "Name": "MX Keys Keyboard"}), "keyboard")
        self.assertEqual(classify({"Icon": "input-gaming"}), "gamepad")
        self.assertEqual(classify({"UUIDs": ["00001812-0000-1000-8000-00805f9b34fb"]}), "other")
        self.assertIsNone(classify({"Class": 0x5A020C, "Name": "Phone"}))          # smartphone
        self.assertIsNone(classify({"Class": 0x240404, "Name": "Headphones"}))     # audio
        self.assertIsNone(classify({"Appearance": 0x0941, "Name": "Watch"}))

    def test_signal_and_messages(self):
        self.assertEqual([signal_bars(r) for r in (-45, -65, -75, -90, None)], [4, 3, 2, 1, None])
        self.assertIn("“K380”", message_for("unreachable", "K380"))
        self.assertEqual(message_for("canceled"), "Pairing was canceled.")
        self.assertTrue(message_for("timeout", "K380").startswith("“K380” didn't answer in time."))


class HostFacts(unittest.TestCase):
    def setUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)

    def test_input_presence_and_bluetooth_inputs(self):
        inputs = InputDevices(self.root / "sys")
        self.assertEqual(inputs.presence(), (False, False))
        add_input(self.root, 0, "Power Button", 0x19, "power")
        add_input(self.root, 1, "AT Translated Set 2 keyboard", 0x11, "keyboard")
        self.assertEqual(inputs.presence(count_builtin=True), (True, False))
        self.assertEqual(inputs.presence(count_builtin=False), (False, False))   # a desktop's phantom i8042
        add_input(self.root, 2, "SYNA3602:00 Touchpad", 0x18, "touchpad")
        self.assertEqual(inputs.presence(count_builtin=False), (False, True))
        add_input(self.root, 3, "Bluetooth Keyboard", 0x05, "keyboard", uniq=KB.lower())
        self.assertEqual(inputs.presence(count_builtin=False), (True, True))
        self.assertEqual(inputs.bluetooth_inputs(), {KB: ["Bluetooth Keyboard"]})

    def test_rfkill_soft_block_touches_only_bluetooth(self):
        add_rfkill(self.root)
        rf = Rfkill(self.root / "sys")
        self.assertEqual(rf.state(), {"present": True, "soft": False, "hard": False})
        rf.set_soft(True)
        self.assertEqual((self.root / "sys/class/rfkill/rfkill0/soft").read_text().strip(), "1")
        self.assertEqual((self.root / "sys/class/rfkill/rfkill1/soft").read_text().strip(), "0")
        self.assertTrue(rf.state()["soft"])


# --- the service and panel through a running hostd ---------------------------------------

class BTBase(Base):
    machine = False
    keyboard_attached = True
    pointer_attached = True
    assistant = "auto"
    rfkill = True

    def before_start(self):
        if self.keyboard_attached:
            add_input(self.root, 1, "USB Keyboard", 0x03, "keyboard")
        if self.pointer_attached:
            add_input(self.root, 2, "USB Mouse", 0x03, "mouse")
        if self.rfkill:
            add_rfkill(self.root)
        self.fake = FakeBackend(devices(), step_s=0.02)
        self.app.cfg.bluetooth_assistant = self.assistant
        self.app.bluetooth = Bluetooth(self.app, self.fake)
        self.app.bluetooth.TICK_S = 0.2
        # The assistant's pauses, shortened (25 s after the Finder, 10 s in setup).
        self.app.bluetooth.ASSISTANT_STEP_S = 0.2
        self.app.bluetooth.OFFER_DELAY_S = 1.5
        self.app.bluetooth.SETUP_DELAY_S = 0.8
        self.app.bluetooth.REPOWER_MIN_S = 0.4
        self.records = Records()
        root = logging.getLogger()
        self.addCleanup(root.removeHandler, self.records)
        self.addCleanup(root.setLevel, root.level)
        root.addHandler(self.records)
        root.setLevel(logging.DEBUG)

    @property
    def bt(self):
        return self.app.bluetooth

    def items(self, doc=None, kind=None):
        out = []

        def walk(items):
            for it in items or []:
                out.append(it)
                walk(it.get("items") if it.get("type") != "list" else None)
        walk((doc or self.fp.doc or {}).get("items"))
        return [i for i in out if kind is None or i.get("type") == kind]

    def device_list(self):
        lists = self.items(kind="list")
        return lists[0] if lists else None

    def rows(self):
        lst = self.device_list()
        return {r["id"]: r for r in (lst or {}).get("items", [])}

    def buttons(self):
        rows = self.items(kind="buttons")
        return {b["id"]: b for b in rows[-1]["items"]} if rows else {}

    def texts(self):
        return [i["text"] for i in self.items(kind="text")]

    def large(self):
        return [i["text"] for i in self.items(kind="text") if i.get("style") == "large"]

    async def until_text(self, text, timeout=10.0):
        await helpers.wait_for(lambda: any(text in t for t in self.texts()), timeout, what=text)

    async def open_bt(self):
        await self.until_doc("setup-welcome")
        btn = [b for r in self.items(kind="buttons") for b in r["items"] if b["id"] == "bluetooth"]
        self.assertEqual(btn[0]["label"], "Bluetooth…")
        self.fp.click("bluetooth")
        await self.until_doc("bluetooth")
        await helpers.wait_for(lambda: KB in self.rows(), 5, what="keyboard found")

    def assert_code_kept(self, *codes):
        logs = "\n".join(self.records.lines)
        files = b"".join(p.read_bytes() for p in self.root.rglob("*") if p.is_file() and not p.is_symlink()
                         and p.stat().st_size < 50_000_000)
        events = json.dumps(list(self.app.lifecycle.recent))
        status = json.dumps(self.bt.public(None))
        for c in codes:
            self.assertNotIn(c, logs, "a pairing code was logged")
            self.assertNotIn(c.encode(), files, "a pairing code was written to a file")
            self.assertNotIn(c, events)
            self.assertNotIn(c, status)


class Power(BTBase):
    async def test_radio_follows_the_setting_with_rfkill(self):
        await helpers.wait_for(lambda: self.fake.powered, 5, what="powered at start")
        self.assertIn(("power", True), self.fake.calls)
        self.assertTrue(self.app.capabilities()["bluetooth"])
        self.assertEqual(self.bt.public()["state"], "on")
        rf = self.root / "sys/class/rfkill/rfkill0/soft"
        await self.app.apply_engine.change_and_apply({"bluetooth.enabled": False}, origin="test")
        self.assertFalse(self.fake.powered)
        self.assertEqual(rf.read_text().strip(), "1")
        self.assertEqual((self.root / "sys/class/rfkill/rfkill1/soft").read_text().strip(), "0", "Wi-Fi untouched")
        self.assertEqual(self.bt.public()["state"], "off")
        with self.assertRaises(BluetoothError) as cm:
            await self.bt.pair(KB)
        self.assertEqual(cm.exception.code, "off")
        await self.app.apply_engine.change_and_apply({"bluetooth.enabled": True}, origin="test")
        self.assertEqual(rf.read_text().strip(), "0")
        self.assertTrue(self.fake.powered)
        ev = [e for e in self.ev("bluetooth_power")]
        self.assertEqual([e["why"] for e in ev], ["start", "setting", "setting"])

    async def test_hard_switch_is_reported(self):
        await helpers.wait_for(lambda: self.fake.powered, 5, what="powered")
        (self.root / "sys/class/rfkill/rfkill0/hard").write_text("1\n")
        self.assertEqual(self.bt.public()["state"], "blocked")
        with self.assertRaises(BluetoothError) as cm:
            await self.bt.pair(KB)
        self.assertEqual(cm.exception.code, "blocked")


class NoAdapter(BTBase):
    def before_start(self):
        super().before_start()
        self.fake.has_adapter = False

    async def test_no_hardware_hides_bluetooth(self):
        await self.until_doc("setup-welcome")
        ids = [b["id"] for r in self.items(kind="buttons") for b in r["items"]]
        self.assertNotIn("bluetooth", ids)
        self.assertFalse(self.app.capabilities()["bluetooth"])
        await self.app.panel.show_recovery()
        await self.until_doc("recovery-main")
        ids = [b["id"] for r in self.items(kind="buttons") for b in r["items"]]
        ids += [row["id"] for lst in self.items(kind="list") for row in lst["items"]]
        self.assertIn("display", ids)
        self.assertNotIn("bluetooth", ids)
        with self.assertRaises(schema.ValidationError):
            self.app.settings.patch(self.app.settings.revision, {"bluetooth.enabled": False},
                                    self.app.validation_context())
        self.assertEqual(self.bt.public()["reason"], "This computer has no Bluetooth hardware.")


class PanelPairing(BTBase):
    async def test_recovery_has_bluetooth_beside_network(self):
        await self.until_doc("setup-welcome")
        await self.app.panel.show_recovery()
        await self.until_doc("recovery-main")
        panes = [row["id"] for row in self.items(kind="list")[0]["items"]]
        self.assertEqual(panes[3:8], ["network", "printers", "bluetooth", "download", "update"])
        self.fp.click("pane", {"pane": "bluetooth"})            # one click on its row
        await self.until_doc("bluetooth")
        self.fp.click("cancel")
        await self.until_doc("recovery-main")
        self.fp.click("bluetooth")                              # the older button id still opens it
        await self.until_doc("bluetooth")
        self.fp.click("cancel")
        await self.until_doc("recovery-main")

    async def test_list_shows_paired_first_then_input_devices_nearby(self):
        await self.open_bt()
        rows = list(self.rows().values())
        self.assertEqual(rows[0]["id"], PAD)
        self.assertEqual((rows[0]["checked"], rows[0]["detail"]), (True, "Trackpad · Connected · Battery 41%"))
        self.assertNotIn("signal", rows[0])
        found = [r["id"] for r in rows[1:]]
        self.assertEqual(found[:3], [KB, PHONE, MOUSE])                     # strongest signal first
        self.assertNotIn("C8:2B:96:1A:4E:09", self.rows(), "a phone is not an input device")
        self.assertEqual(self.rows()[KB]["detail"], "Keyboard · Not paired")
        self.assertEqual(self.rows()[KB]["signal"], 4)
        self.assertFalse(self.rows()[KB]["checked"])
        self.assertEqual(self.device_list()["activate"], "click")
        self.assertIn("“Magic Trackpad” is connected.", self.texts())
        self.assertIn("Click a device to pair it.", self.texts())
        self.assertEqual(list(self.buttons()), ["scan", "cancel"])
        self.assertTrue(self.bt.discovering())
        # Keyboard selection: Return pairs (Pair is the default button).
        self.fp.change("device", KB)
        await helpers.wait_for(lambda: "pair" in self.buttons(), 5, what="Pair button")
        self.assertTrue(self.buttons()["pair"]["default"])
        # A paired device selected: Forget…; Done stays the default while it is connected.
        self.fp.change("device", PAD)
        await helpers.wait_for(lambda: "forget" in self.buttons(), 5, what="Forget button")
        self.assertTrue(self.buttons()["cancel"]["default"])
        # Done stops looking.
        self.fp.click("cancel")
        await self.until_doc("setup-welcome")
        await helpers.wait_for(lambda: not self.bt.discovering(), 5, what="discovery stopped")

    async def test_keyboard_passkey_is_shown_large_and_typed_on_the_keyboard(self):
        await self.open_bt()
        self.fp.click("device", {"device": KB})
        await self.until_doc("bluetooth-pair")
        await helpers.wait_for(lambda: self.large(), 5, what="code shown")
        code = self.large()[0].replace(" ", "")
        self.assertRegex(code, r"^\d{6}$")
        self.assertEqual(self.large()[0], " ".join(code))
        self.assertIn("Type this code on “Bluetooth Keyboard”, then press Return:", self.texts())
        self.assertEqual(list(self.buttons()), ["cancel"])
        self.assertFalse(self.bt.discovering(), "discovery pauses while pairing")
        # Classic keyboards report each digit typed.
        await self.fake._ask(AgentRequest("display_passkey", KB, passkey=int(code), entered=3))
        await self.until_text("3 of 6 digits typed")
        self.fake.type_code(KB, code)
        await self.until_doc("message", 10)
        self.assertEqual(self.fp.doc["alert_icon"], "note")
        self.assertEqual(self.texts()[:2], ["“Bluetooth Keyboard” is connected.",
                                            "It connects by itself whenever it is on and near this computer."])
        self.assertIn(("trust", KB, True), self.fake.calls)
        self.assertIn(("connect", KB), self.fake.calls)
        self.fp.click("ok")
        await self.until_doc("bluetooth")
        await helpers.wait_for(lambda: self.rows().get(KB, {}).get("checked"), 5, what="keyboard checked")
        rows = list(self.rows())
        self.assertEqual(rows[:2], [KB, PAD])                                # connected, by name
        self.assertEqual(self.rows()[KB]["detail"], "Keyboard · Connected · Battery 90%")
        ev = self.ev("bluetooth_paired")[-1]
        self.assertEqual((ev["address"], ev["kind"], ev["origin"], ev["connected"]), (KB, "keyboard", "panel", True))
        self.assert_code_kept(code)

    async def test_wrong_code_says_why_and_returns_to_the_list(self):
        await self.open_bt()
        self.fp.click("device", {"device": KB})
        await self.until_doc("bluetooth-pair")
        await helpers.wait_for(lambda: self.large(), 5, what="code shown")
        code = self.large()[0].replace(" ", "")
        self.fake.type_code(KB, "000000" if code != "000000" else "111111")
        await self.until_doc("message", 10)
        self.assertEqual(self.fp.doc["alert_icon"], "caution")
        self.assertEqual(self.texts()[:2], ["Could not pair with “Bluetooth Keyboard”.", message_for(
            "auth_failed", "Bluetooth Keyboard")])
        self.fp.click("ok")
        await self.until_doc("bluetooth")
        self.assertFalse(self.rows()[KB].get("checked"))
        self.assertEqual(self.ev("bluetooth_pair_failed")[-1]["code"], "auth_failed")

    async def test_legacy_keyboard_pin_and_legacy_mouse_fixed_pin(self):
        await self.open_bt()
        await helpers.wait_for(lambda: OLDKB in self.rows(), 5, what="old keyboard")
        self.fp.click("device", {"device": OLDKB})
        await self.until_doc("bluetooth-pair")
        await helpers.wait_for(lambda: self.large(), 5, what="PIN shown")
        pin = self.large()[0].replace(" ", "")
        self.assertRegex(pin, r"^\d{6}$")
        self.assertIn("Type this code on “Old Keyboard”, then press Return:", self.texts())
        self.fake.type_code(OLDKB, pin)
        await self.until_doc("message", 10)
        self.assertEqual(self.texts()[0], "“Old Keyboard” is connected.")
        self.fp.click("ok")
        await self.until_doc("bluetooth")
        await helpers.wait_for(lambda: OLDMOUSE in self.rows(), 5, what="old mouse")
        shown = len([d for d in self.fp.docs if d["id"] == "bluetooth-pair"])
        self.fp.click("device", {"device": OLDMOUSE})
        await self.until_doc("message", 10)
        self.assertEqual(self.texts()[0], "“Old Mouse” is connected.")
        pair_docs = [d for d in self.fp.docs if d["id"] == "bluetooth-pair"][shown:]
        self.assertFalse(any(i.get("style") == "large" for d in pair_docs for i in self.items(d, "text")),
                         "a mouse's fixed PIN is not shown")
        self.assert_code_kept(pin)

    async def test_numeric_comparison_and_cancel(self):
        await self.open_bt()
        self.fp.click("device", {"device": PHONE})
        await self.until_doc("bluetooth-pair")
        await helpers.wait_for(lambda: self.large(), 5, what="number shown")
        self.assertIn("Does “Keyboard Cover” show this code?", self.texts())
        self.assertEqual(self.buttons()["confirm"], {"id": "confirm", "label": "Pair", "default": True})
        self.fp.click("confirm")
        await self.until_doc("message", 10)
        self.assertEqual(self.texts()[0], "“Keyboard Cover” is connected.")
        # A second comparison refused with Cancel (Escape): back to the list, no failure alert.
        self.fake.devs[PHONE].paired = False
        self.fake.devs[PHONE].connected = False
        self.fake.devs[PHONE].discoverable = True
        self.fp.click("ok")
        await self.until_doc("bluetooth")
        await helpers.wait_for(lambda: PHONE in self.rows() and not self.rows()[PHONE].get("checked"), 5,
                               what="listed again")
        self.fp.click("device", {"device": PHONE})
        await self.until_doc("bluetooth-pair")
        await helpers.wait_for(lambda: self.large(), 5, what="number shown")
        self.fp.click("cancel")
        await self.until_doc("bluetooth", 10)
        self.assertNotIn("message", [d["id"] for d in self.fp.docs[-3:]])
        self.assertEqual(self.ev("bluetooth_pair_failed")[-1]["code"], "canceled")

    async def test_unreachable_device_reason(self):
        await self.open_bt()
        await helpers.wait_for(lambda: GONE in self.rows(), 5, what="travel keyboard")
        self.fp.click("device", {"device": GONE})
        await self.until_doc("message", 10)
        self.assertEqual(self.texts()[:2], ["Could not pair with “Travel Keyboard”.",
                                            message_for("unreachable", "Travel Keyboard")])

    async def test_forget_has_cancel_as_default(self):
        await self.open_bt()
        self.fp.click("device", {"device": PAD})
        await helpers.wait_for(lambda: "forget" in self.buttons(), 5, what="Forget…")
        self.fp.click("forget")
        await self.until_doc("bluetooth-forget")
        self.assertEqual(self.texts()[0], "Forget “Magic Trackpad”?")
        self.assertEqual(self.buttons()["cancel"].get("default"), True)
        self.fp.click("cancel")
        await self.until_doc("bluetooth")
        self.assertIn(PAD, self.rows())
        self.fp.click("device", {"device": PAD})
        await helpers.wait_for(lambda: "forget" in self.buttons(), 5, what="Forget…")
        self.fp.click("forget")
        await self.until_doc("bluetooth-forget")
        self.fp.click("forget")
        await self.until_doc("bluetooth")
        await self.until_text("“Magic Trackpad” was forgotten.")
        self.assertNotIn(PAD, self.rows())
        self.assertIn(("remove", PAD), self.fake.calls)
        self.assertTrue(self.ev("bluetooth_forgotten"))

    async def test_reconnects_without_a_restart_and_connect_button(self):
        await self.open_bt()
        self.fake.device_off(PAD)
        await helpers.wait_for(lambda: not self.rows()[PAD].get("checked"), 5, what="disconnected")
        self.assertEqual(self.rows()[PAD]["detail"], "Trackpad · Not connected")
        self.fake.device_reconnects(PAD)
        await helpers.wait_for(lambda: self.rows()[PAD].get("checked"), 5, what="reconnected")
        names = [e["event"] for e in self.app.lifecycle.recent if e.get("address") == PAD]
        self.assertIn("bluetooth_disconnected", names)
        self.assertEqual(names[-1], "bluetooth_connected")
        # Connect: a paired device that is not connected.
        self.fake.devs[PAD].connected = False
        self.fake.changed()
        self.fp.click("device", {"device": PAD})
        await helpers.wait_for(lambda: "connect" in self.buttons(), 5, what="Connect")
        self.assertTrue(self.buttons()["connect"]["default"])
        self.fp.click("connect")
        await self.until_doc("bluetooth", 10)
        await self.until_text("“Magic Trackpad” is connected.")

    async def test_use_bluetooth_checkbox_turns_the_radio_off(self):
        await self.open_bt()
        self.fp.change("enabled", False)
        await helpers.wait_for(lambda: not self.fake.powered, 5, what="radio off")
        await self.until_text("Bluetooth is off.")
        self.assertTrue(self.device_list().get("disabled"))
        self.assertTrue(self.buttons()["scan"].get("disabled"))
        self.assertFalse(self.app.settings.applied["bluetooth.enabled"])
        self.fp.change("enabled", True)
        await helpers.wait_for(lambda: self.fake.powered, 5, what="radio on")


class Reconnect(BTBase):
    def before_start(self):
        super().before_start()
        self.fake.devs[KB].paired = self.fake.devs[KB].trusted = True
        self.fake.devs[KB].discoverable = False

    async def test_paired_classic_devices_are_asked_to_connect_at_start(self):
        await helpers.wait_for(lambda: ("connect", KB) in self.fake.calls, 5, what="reconnect attempt")
        await helpers.wait_for(lambda: self.fake.devs[KB].connected, 5, what="connected")
        self.assertNotIn(("connect", PAD), self.fake.calls, "already connected")
        self.assertEqual(self.ev("bluetooth_reconnect_sweep")[0]["why"], "start")


class Agent(BTBase):
    async def test_requests_outside_a_pairing_are_refused(self):
        await helpers.wait_for(lambda: self.fake.powered, 5, what="powered")
        for m in ("request_authorization", "request_confirmation", "request_pin", "display_passkey"):
            with self.assertRaises(AgentRejected):
                await self.bt.agent_request(AgentRequest(m, KB, passkey=123456))
        with self.assertRaises(AgentRejected):
            await self.bt.agent_request(AgentRequest("authorize_service", KB,
                                                     uuid="00001124-0000-1000-8000-00805f9b34fb"))
        # A paired HID device's input service is allowed; anything else is not.
        await self.bt.agent_request(AgentRequest("authorize_service", PAD,
                                                 uuid="00001124-0000-1000-8000-00805f9b34fb"))
        with self.assertRaises(AgentRejected):
            await self.bt.agent_request(AgentRequest("authorize_service", PAD,
                                                     uuid="0000110b-0000-1000-8000-00805f9b34fb"))
        self.assertGreaterEqual(len(self.ev("bluetooth_refused")), 6)


class Assistant(BTBase):
    """First-time setup with neither a keyboard nor a pointer: the search window,
    then the strongest keyboard's code."""
    keyboard_attached = False
    pointer_attached = False

    async def test_no_input_searches_and_shows_a_keyboard_code(self):
        await self.until_doc("setup-welcome")
        await helpers.wait_for(lambda: self.bt.assistant["looking_for"] == "keyboard-or-mouse", 5, what="assistant")
        await self.until_doc("bluetooth-assistant", 10)
        self.assertEqual(self.texts()[0], "No keyboard or mouse is connected.")
        self.assertEqual(self.buttons()["cancel"]["label"], "Not Now")
        # The strongest keyboard in pairing mode gets the code window by itself.
        await self.until_doc("bluetooth-pair", 10)
        await helpers.wait_for(lambda: self.large(), 5, what="code shown")
        code = self.large()[0].replace(" ", "")
        self.assertIn("Type this code on “Bluetooth Keyboard”, then press Return:", self.texts())
        self.assertTrue(any("found this Bluetooth keyboard" in t for t in self.texts()))
        self.fake.type_code(KB, code)
        await self.until_doc("message", 10)
        self.assertEqual(self.texts()[0], "“Bluetooth Keyboard” is connected.")
        # The keyboard now exists as an input device: the assistant stops.
        add_input(self.root, 5, "Bluetooth Keyboard", 0x05, "keyboard", uniq=KB.lower())
        await self.until_doc("setup-welcome", 15)       # the note closes by itself
        await helpers.wait_for(lambda: self.bt.assistant["looking_for"] is None, 5, what="assistant done")
        self.assertEqual(self.ev("bluetooth_paired")[0]["origin"], "assistant")
        self.assertEqual(self.ev("bluetooth_assistant_shown")[0]["context"], "setup")
        self.assert_code_kept(code)


class AssistantPointer(BTBase):
    """The search pairs a mouse (no code), then only offers the keyboard."""
    keyboard_attached = False
    pointer_attached = False

    def before_start(self):
        super().before_start()
        for a in (KB, OLDKB, PHONE, GONE):
            self.fake.devs.pop(a)

    async def test_a_mouse_is_paired_then_the_keyboard_is_only_offered(self):
        await self.until_doc("setup-welcome")
        await helpers.wait_for(lambda: self.fake.devs[MOUSE].paired, 15, what="mouse paired")
        await self.until_doc("message", 10)
        self.assertEqual(self.texts()[0], "“Wireless Mouse” is connected.")
        self.assertNotIn("bluetooth-pair", [d["id"] for d in self.fp.docs if any(
            i.get("style") == "large" for i in self.items(d, "text"))])
        add_input(self.root, 6, "Wireless Mouse", 0x05, "mouse", uniq=MOUSE.lower())
        await helpers.wait_for(lambda: self.bt.assistant["looking_for"] == "keyboard", 5, what="keyboard only now")
        await self.until_doc("bluetooth-offer", 15)
        self.assertEqual(self.texts()[0], "No keyboard is connected. Pair a Bluetooth keyboard?")
        self.fp.click("cancel")
        await self.until_doc("setup-welcome", 5)
        self.assertEqual(self.ev("bluetooth_assistant_dismissed")[-1]["why"], "not-now")


class AssistantRightAfterBoot(BTBase):
    """First-time setup: hostd starts seconds after power-on (CLOCK_MONOTONIC is small)."""
    keyboard_attached = False
    pointer_attached = False

    def before_start(self):
        super().before_start()
        import time as _time
        from unittest import mock
        import retroboot.bluetooth as btmod
        t0 = _time.monotonic()
        patcher = mock.patch.object(btmod, "mono", lambda: 8.0 + (_time.monotonic() - t0))
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_assistant_does_not_wait_for_uptime(self):
        await self.until_doc("setup-welcome")
        await self.until_doc("bluetooth-pair", 8)      # not two minutes after boot
        self.assertEqual(self.bt.session.origin, "assistant")


class AssistantWithKeyboard(BTBase):
    async def test_attached_keyboard_means_no_assistant(self):
        await self.until_doc("setup-welcome")
        await asyncio.sleep(3.2)
        self.assertIsNone(self.bt.assistant["looking_for"])
        self.assertFalse(any(c[0] == "pair" for c in self.fake.calls))
        self.assertFalse([d for d in self.fp.docs if d.get("id") in ASSISTANT_DOCS])


class SlowStart(BTBase):
    machine = True
    keyboard_attached = False

    def extra_conf(self):
        return {"boot_s": 1.0, "finder_s": 3.0}

    def assert_quiet_until_finder(self):
        """Nothing on screen, no scan and no pairing before the Finder and the pause after it."""
        finder = self.ev("finder_detected")[0]["t_mono"]
        shown = self.ev("bluetooth_assistant_shown")
        self.assertGreaterEqual(shown[0]["t_mono"] - finder, self.bt.OFFER_DELAY_S - 0.3)
        first = next(i for i, d in enumerate(self.fp.docs) if d.get("id") in ASSISTANT_DOCS)
        self.assertEqual(self.fp.docs[first]["id"], shown[0]["stage"] == "offer" and "bluetooth-offer" or
                         "bluetooth-assistant")
        self.assertEqual(shown[0]["context"], "finder")
        self.assertEqual(shown[0]["over"], "mac-os")


class OfferAfterTheFinder(SlowStart):
    """A pointer but no keyboard, Mac OS starting: the offer comes only after the
    Finder, scans nothing until accepted, and leads to the Bluetooth window."""

    async def test_offer_waits_for_the_finder_and_pairs_only_when_accepted(self):
        await self.until_event("guest_content")
        await asyncio.sleep(1.0)
        self.assertFalse(self.ev("finder_detected"))
        self.assertFalse([d for d in self.fp.docs if d.get("id") in ASSISTANT_DOCS], "nothing over the startup")
        await self.until_event("finder_detected")
        await self.until_doc("bluetooth-offer", 10)
        self.assert_quiet_until_finder()
        self.assertEqual(self.fp.scene.get("background"), "guest-dimmed")
        self.assertEqual(self.texts()[0], "No keyboard is connected. Pair a Bluetooth keyboard?")
        self.assertEqual(self.buttons()["pair"]["label"], "Pair Keyboard…")
        self.assertEqual(self.buttons()["cancel"]["label"], "Not Now")
        await asyncio.sleep(1.0)
        self.assertNotIn(("discovery", True), self.fake.calls, "no scan before the person accepts")
        self.assertFalse(any(c[0] == "pair" for c in self.fake.calls))
        self.fp.click("pair")
        await self.until_doc("bluetooth")
        await helpers.wait_for(lambda: KB in self.rows(), 5, what="keyboard listed")
        self.fp.click("device", {"device": KB})
        await self.until_doc("bluetooth-pair")
        await helpers.wait_for(lambda: self.large(), 5, what="code")
        self.fake.type_code(KB, self.large()[0].replace(" ", ""))
        await self.until_doc("message", 10)
        add_input(self.root, 5, "Bluetooth Keyboard", 0x05, "keyboard", uniq=KB.lower())
        self.fp.click("ok")
        await self.until_doc("bluetooth")
        self.fp.click("cancel")             # Done
        await helpers.wait_for(lambda: self.fp.scene.get("scene") == "guest", 10, what="back to Mac OS")
        self.assertEqual(self.ev("bluetooth_paired")[0]["origin"], "panel")
        self.assertEqual(self.ev("bluetooth_assistant_dismissed")[0]["why"], "accepted")


class OfferNotNow(SlowStart):
    """Not Now: not again until a keyboard comes or goes (or the next boot)."""

    async def test_not_now_lasts_until_a_keyboard_change(self):
        await self.until_doc("bluetooth-offer", 20)
        self.fp.click("cancel")
        await helpers.wait_for(lambda: self.fp.scene.get("scene") == "guest", 10, what="back to Mac OS")
        note = self.app.paths.run / "bluetooth-assistant.json"
        self.assertEqual(json.loads(note.read_text())["dismissed"], "not-now")
        n = len(self.ev("bluetooth_assistant_shown"))
        await asyncio.sleep(self.bt.OFFER_DELAY_S * 2 + 1)
        self.assertEqual(len(self.ev("bluetooth_assistant_shown")), n, "the offer stays away")
        self.assertEqual(self.fp.scene.get("scene"), "guest")
        # A hostd restart in the same boot keeps the note.
        again = Bluetooth(self.app, FakeBackend([], step_s=0.02))
        again._load_cooldown()
        self.assertEqual(again._dismissed, "not-now")
        # A keyboard comes and goes: the offer may come back.
        kb = add_input(self.root, 7, "USB Keyboard", 0x03, "keyboard")
        await self.until_event("bluetooth_assistant_rearmed")
        self.assertFalse(note.exists())
        import shutil
        shutil.rmtree(kb)
        await self.until_doc("bluetooth-offer", 15)
        self.assertEqual(len(self.ev("bluetooth_assistant_shown")), n + 1)


class OfferTurnedOff(SlowStart):
    def before_start(self):
        super().before_start()
        self.app.settings.set_desired_and_applied(
            {"bluetooth.offer_keyboard_setup": False}, schema.ValidationContext(check_capabilities=False))

    async def test_setting_turns_the_offer_off(self):
        s = schema.BY_KEY["bluetooth.offer_keyboard_setup"]
        self.assertEqual((s.default, s.capability, s.group), (True, "bluetooth", "bluetooth"))
        await self.until_event("finder_detected")
        await asyncio.sleep(self.bt.OFFER_DELAY_S + 2)
        self.assertFalse([d for d in self.fp.docs if d.get("id") in ASSISTANT_DOCS])
        self.assertIsNone(self.bt.assistant["looking_for"])
        self.assertFalse(self.bt.public()["assistant"]["offer"])
        # On again (the Bluetooth window's checkbox does the same): the offer follows.
        await self.app.apply_engine.change_and_apply({"bluetooth.offer_keyboard_setup": True}, origin="test")
        await self.until_doc("bluetooth-offer", 10)


class OfferCheckbox(BTBase):
    async def test_bluetooth_window_has_the_offer_checkbox(self):
        await self.open_bt()
        boxes = {i["id"]: i for i in self.items(kind="checkbox")}
        self.assertTrue(boxes["offer"]["value"])
        self.assertEqual(boxes["offer"]["label"], "Offer to pair a keyboard when none is connected")
        self.fp.change("offer", False)
        await helpers.wait_for(lambda: not self.app.settings.applied["bluetooth.offer_keyboard_setup"], 5,
                               what="setting off")


class SearchOverTheFinder(SlowStart):
    """No keyboard and no pointer: the search window after the Finder, which gives
    Mac OS its screen back when nothing turns up."""
    pointer_attached = False

    def before_start(self):
        super().before_start()
        for d in self.fake.devs.values():
            d.discoverable = False      # nothing in pairing mode
        self.bt.SEARCH_LIMIT_S = 2.5

    async def test_search_after_the_finder_then_gives_up(self):
        await self.until_doc("bluetooth-assistant", 20)
        self.assert_quiet_until_finder()
        await helpers.wait_for(lambda: self.bt.discovering(), 5, what="scanning while the window shows")
        await helpers.wait_for(lambda: self.fp.scene.get("scene") == "guest", 10, what="back to Mac OS")
        self.assertEqual(self.ev("bluetooth_assistant_dismissed")[0]["why"], "timeout")
        await helpers.wait_for(lambda: not self.bt.discovering(), 5, what="scan stopped")


class CodeOverMacOS(SlowStart):
    """No keyboard and no pointer over the Finder: the keyboard's code, then Mac OS."""
    pointer_attached = False

    async def test_code_window_over_mac_os_then_back(self):
        await self.until_doc("bluetooth-pair", 25)
        self.assert_quiet_until_finder()
        self.assertEqual(self.fp.scene.get("background"), "guest-dimmed")
        await helpers.wait_for(lambda: self.large(), 5, what="code")
        self.fake.type_code(KB, self.large()[0].replace(" ", ""))
        await self.until_doc("message", 10)
        add_input(self.root, 5, "Bluetooth Keyboard", 0x05, "keyboard", uniq=KB.lower())
        await helpers.wait_for(lambda: self.fp.scene.get("scene") == "guest", 15, what="back to Mac OS")


class Context(unittest.TestCase):
    """Where the assistant may appear (bluetooth.Bluetooth.assistant_context)."""

    def make(self, mode="normal", state="stopped", visible=False, machines=("m",), **sup):
        from types import SimpleNamespace as NS
        sup = NS(**({"state": state, "active": state in ("starting", "running", "paused", "stopping"),
                     "paused": False, "responsive": True, "finder_seen": False, "finder_timeout_logged": False,
                     "content_seen": state == "running", "t_start": 0.0, "boot_pending": False} | sup))
        app = NS(mode=mode, supervisor=sup, panel=NS(visible=visible, screen=None),
                 cfg=NS(finder_probe=True, finder_wait_timeout=60.0, sysfs_path="/nonexistent"),
                 machines=NS(list=lambda: list(machines)))
        return Bluetooth.assistant_context(NS(app=app))

    def test_contexts(self):
        self.assertIsNone(self.make(state="starting"), "never over the startup")
        self.assertIsNone(self.make(state="running"), "Mac OS running, no Finder yet")
        self.assertEqual(self.make(state="running", finder_seen=True), "finder")
        self.assertEqual(self.make(state="running", finder_timeout_logged=True), "finder")
        # Mac OS started without its Finder recognized (an unknown theme): the last resort
        self.assertEqual(self.make(state="running", mac_os_up=True), "finder")
        self.assertIsNone(self.make(state="running", mac_os_up=False))
        self.assertIsNone(self.make(state="running", finder_seen=True, paused=True))
        self.assertIsNone(self.make(state="running", finder_seen=True, responsive=False))
        self.assertIsNone(self.make(state="stopping", finder_seen=True))
        self.assertEqual(self.make(visible=True), "stopped")
        self.assertIsNone(self.make(visible=False), "stopped, but nothing on screen yet")
        self.assertIsNone(self.make(visible=True, boot_pending=True))
        self.assertEqual(self.make(visible=True, machines=()), "setup")
        self.assertEqual(self.make(mode="installer", visible=True), "setup")
        self.assertIsNone(self.make(mode="installer", visible=False))


class Repower(BTBase):
    def before_start(self):
        super().before_start()
        self.fake.devs[KB].paired = self.fake.devs[KB].trusted = True
        self.fake.devs[KB].discoverable = False

    async def test_adapter_is_powered_again_after_bluetoothd_restarts(self):
        await helpers.wait_for(lambda: self.fake.devs[KB].connected, 5, what="connected at start")
        self.fake.daemon_restarted()            # back at once, unpowered, nothing connected
        await helpers.wait_for(lambda: self.fake.powered, 5, what="powered again")
        await helpers.wait_for(lambda: self.fake.devs[KB].connected, 5, what="reconnected")
        self.assertEqual(self.ev("bluetooth_daemon_restarted")[0]["generation"], 1)
        self.assertIn("bluetoothd", [e["why"] for e in self.ev("bluetooth_power")])
        # Powered off behind hostd's back (no restart seen): on again after a pause.
        self.fake.powered = False
        self.fake.changed()
        await helpers.wait_for(lambda: self.fake.powered, 5, what="repowered")
        self.assertIn("repower", [e["why"] for e in self.ev("bluetooth_power")])
        # Off by the setting: it stays off.
        await self.app.apply_engine.change_and_apply({"bluetooth.enabled": False}, origin="test")
        await asyncio.sleep(1.5)
        self.assertFalse(self.fake.powered)


class Failures(BTBase):
    def before_start(self):
        super().before_start()
        self.fake.devs[GONE].method = "lecancel"

    async def test_le_connection_failure_is_not_called_canceled(self):
        await helpers.wait_for(lambda: self.fake.powered, 5, what="powered")
        self.bt.scan("test")
        await helpers.wait_for(lambda: self.bt.device(GONE), 5, what="found")
        with self.assertRaises(BluetoothError) as cm:
            await self.bt.pair(GONE)
        self.assertEqual(cm.exception.code, "unreachable")
        self.assertEqual(str(cm.exception), message_for("unreachable", "Travel Keyboard"))

    async def test_connect_failure_has_a_sentence_not_bluez_text(self):
        await helpers.wait_for(lambda: self.fake.powered, 5, what="powered")
        self.fake.device_off(PAD)
        job = self.app.bluetooth_job("connect", origin="test", address=PAD)
        await self.job(job)
        self.assertEqual(job.error["code"], "connect_failed")
        self.assertEqual(job.message, message_for("connect_failed", "Magic Trackpad"))
        self.assertNotIn("Host is down", job.message)
        self.assertEqual(self.ev("bluetooth_connect_failed")[0]["detail"], "Host is down")


# --- web API ----------------------------------------------------------------------------

class WebApi(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        helpers.make_package(self.root)
        add_input(self.root, 1, "USB Keyboard", 0x03, "keyboard")
        self.cfg = helpers.make_config(self.root)
        self.app = Hostd(self.cfg)
        self.app.loop = asyncio.get_running_loop()
        self.app.settings.store.load()
        self.fake = FakeBackend(devices(), step_s=0.02)
        self.app.bluetooth = Bluetooth(self.app, self.fake)
        await self.app.bluetooth.start()
        self.addAsyncCleanup(self.app.bluetooth.stop)
        self.app.web.tls()
        self.client = TestClient(TestServer(self.app.web.web_app, host="127.0.0.1"))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.browsers = {}

    async def pair_browser(self, name):
        code, _ = self.app.pairing.current_code(regenerate=True)
        r = await self.client.post("/api/v1/pair", json={"code": code, "device_name": name})
        self.assertEqual(r.status, 200, await r.text())
        cookie = r.headers.getall("Set-Cookie")[0].split("rb_session=", 1)[1].split(";", 1)[0]
        self.browsers[name] = {"Cookie": f"rb_session={cookie}", "X-RetroBoot-CSRF": (await r.json())["csrf"]}

    async def get(self, who):
        r = await self.client.get("/api/v1/bluetooth", headers=self.browsers[who])
        self.assertEqual(r.status, 200)
        return await r.json()

    async def post(self, who, op, body=None):
        r = await self.client.post(f"/api/v1/bluetooth/{op}", json=body or {}, headers=self.browsers[who])
        return r.status, await r.json()

    async def test_unpaired_browsers_get_nothing(self):
        r = await self.client.get("/api/v1/bluetooth")
        self.assertEqual(r.status, 401)
        r = await self.client.post("/api/v1/bluetooth/pair", json={"address": KB})
        self.assertEqual(r.status, 401)

    async def test_web_pairing_shows_the_code_to_that_browser_only(self):
        await self.pair_browser("Phone")
        await self.pair_browser("Laptop")
        st, body = await self.post("Phone", "scan", {"seconds": 30})
        self.assertEqual((st, body["scanning"]), (200, True))
        async def listed():
            st = await self.get("Phone")
            return st if [d["address"] for d in st["devices"]][:2] == [PAD, KB] else None
        state = await helpers.wait_for(listed, 5, what="devices listed")
        self.assertEqual(state["state"], "on")
        self.assertNotIn("C8:2B:96:1A:4E:09", [d["address"] for d in state["devices"]])
        st, job = await self.post("Phone", "pair", {"address": KB})
        self.assertEqual(st, 202, job)
        self.assertEqual(job["kind"], "bluetooth.pair")

        async def code():
            p = (await self.get("Phone"))["pairing"]
            return p.get("passkey") if p and p["stage"] == "passkey" else None
        passkey = await helpers.wait_for(code, 5, what="passkey for the starting browser")
        mine = (await self.get("Phone"))["pairing"]
        self.assertEqual((mine["code_shown_on"], mine["name"]), ("here", "Bluetooth Keyboard"))
        other = (await self.get("Laptop"))["pairing"]
        self.assertNotIn("passkey", other)
        self.assertEqual((other["stage"], other["code_shown_on"]), ("passkey", "web"))
        self.assertNotIn(passkey, json.dumps(await self.app.status()))
        self.assertNotIn(passkey, json.dumps(self.app.bluetooth.public("panel")), "the panel shows nothing")
        # Busy while one pairing runs.
        st, err = await self.post("Laptop", "pair", {"address": OLDKB})
        self.assertEqual((st, err["error"]["code"]), (409, "busy"))
        self.fake.type_code(KB, passkey)
        done = await helpers.wait_for(lambda: self.app.jobs.get(job["id"]).done.is_set(), 10, what="job")
        self.assertTrue(done)
        j = self.app.jobs.get(job["id"]).public()
        self.assertEqual((j["state"], j["message"]), ("succeeded", "“Bluetooth Keyboard” is connected."))
        dev = [d for d in (await self.get("Phone"))["devices"] if d["address"] == KB][0]
        self.assertEqual((dev["paired"], dev["connected"], dev["battery"]), (True, True, 90))
        st, _ = await self.post("Phone", "dismiss")
        self.assertIsNone((await self.get("Phone"))["pairing"])

    async def test_confirm_cancel_forget_and_errors(self):
        await self.pair_browser("Phone")
        await self.post("Phone", "scan")
        st, job = await self.post("Phone", "pair", {"address": PHONE})
        await helpers.wait_for(lambda: (self.app.bluetooth.session or None) and
                               self.app.bluetooth.session.stage == "confirm", 5, what="comparison")
        st, body = await self.post("Phone", "confirm", {"accept": True})
        self.assertEqual((st, body), (200, {"accepted": True}))
        await helpers.wait_for(lambda: self.app.jobs.get(job["id"]).done.is_set(), 10, what="job")
        st, job = await self.post("Phone", "pair", {"address": KB})
        await helpers.wait_for(lambda: self.app.bluetooth.session.stage == "passkey", 5, what="passkey")
        st, body = await self.post("Phone", "cancel")
        self.assertTrue(body["canceled"])
        await helpers.wait_for(lambda: self.app.jobs.get(job["id"]).done.is_set(), 10, what="job")
        self.assertEqual(self.app.jobs.get(job["id"]).error["code"], "canceled")
        st, err = await self.post("Phone", "pair", {"address": "nonsense"})
        self.assertEqual(st, 422)
        st, err = await self.post("Phone", "pair", {"address": "00:11:22:33:44:55"})
        self.assertEqual((st, err["error"]["code"]), (404, "not-found"))
        st, err = await self.post("Phone", "connect", {"address": KB})
        self.assertEqual((st, err["error"]["code"]), (409, "not-paired"))
        st, body = await self.post("Phone", "forget", {"address": PAD})
        self.assertEqual((st, body), (200, {"forgotten": PAD}))
        st, err = await self.post("Phone", "confirm", {"accept": True})
        self.assertEqual(st, 409)
        # Off: actions are refused with a reason.
        self.app.settings.patch(self.app.settings.revision, {"bluetooth.enabled": False},
                                self.app.validation_context())
        await self.app.apply_engine.apply(self.app.settings.revision, False, None, "test")
        st, err = await self.post("Phone", "pair", {"address": KB})
        self.assertEqual((st, err["error"]["code"]), (409, "bluetooth-off"))
        self.assertEqual(err["error"]["message"], "Bluetooth is off. Turn it on, then try again.")

    async def test_settings_and_status_carry_bluetooth(self):
        await self.pair_browser("Phone")
        r = await self.client.get("/api/v1/settings", headers=self.browsers["Phone"])
        snap = await r.json()
        self.assertEqual(snap["capabilities"]["bluetooth"], {"present": True})
        offer = next(d for d in snap["schema"] if d["key"] == "bluetooth.offer_keyboard_setup")
        self.assertEqual((offer["group_id"], offer["group"], offer["default"]), ("bluetooth", "Bluetooth", True))
        self.assertTrue(snap["applied"]["bluetooth.offer_keyboard_setup"])
        st = await self.app.status()
        self.assertTrue(st["capabilities"]["bluetooth"])
        self.assertEqual(st["bluetooth"]["state"], "on")


if __name__ == "__main__":
    unittest.main()
