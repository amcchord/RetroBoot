"""AirPort bridge: name translation, protocol, service policy, backends, hostd hooks.

The card side is a fake: an asyncio client speaking the bridge protocol the
way QEMU's retroboot-airport does (ARCHITECTURE §10).  The NetworkManager
backend runs against tests/fakes/fake_nmcli.py, through the real subprocess
runner.  Nothing here touches real radios or NetworkManager.
"""
import asyncio
import json
import os
import shutil
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path

import helpers  # noqa: F401  (sys.path)
from helpers import FAKES
from retroboot import airport, schema
from retroboot.airport import names
from retroboot.airport.backends import FixtureBackend, HostLink, JoinError, VirtualBackend
from retroboot.airport.bridge import validate
from retroboot.airport.names import (ENTERPRISE, OPEN, PSK, SAE, HostNetwork, NameTable, guest_name,
                                     security_class)
from retroboot.airport.nm import NetworkManagerBackend, parse_wifi_list, signal_dbm
from retroboot.airport.service import AirPortService
from retroboot.network import NetworkAdapter


class NameTests(unittest.TestCase):
    def test_security_classes(self):
        self.assertEqual(security_class("--"), OPEN)
        self.assertEqual(security_class(""), OPEN)
        self.assertEqual(security_class("WPA1 WPA2"), PSK)
        self.assertEqual(security_class("WPA2 WPA3"), PSK)       # transition mode takes a WPA2 passphrase
        self.assertEqual(security_class("WPA3"), SAE)
        self.assertEqual(security_class("WPA2 802.1X"), ENTERPRISE)
        self.assertEqual(security_class("WEP"), names.WEP)
        self.assertEqual(security_class("OWE"), names.OWE)

    def test_guest_names(self):
        self.assertEqual(guest_name(b"RetroBoot"), b"RetroBoot")
        self.assertEqual(guest_name("Café".encode()), b"Caf\x8e")                 # Mac OS Roman é
        self.assertEqual(guest_name("İstanbul".encode()), b"Istanbul")             # accent dropped
        self.assertEqual(guest_name("Tea ☕".encode()), b"Tea ?")                  # not representable
        self.assertEqual(guest_name("📶📶".encode()), b"Wi-Fi Network")            # nothing left
        self.assertEqual(guest_name(b"\xff\xfeLab"), b"??Lab")                      # not UTF-8
        self.assertEqual(guest_name(b"a\x07b"), b"a?b")                             # control character
        self.assertEqual(guest_name(b"x" * 32), b"x" * 32)
        self.assertLessEqual(len(guest_name("é".encode() * 16)), 32)

    def test_duplicates_are_stable_and_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "airport-names.json"
            t = NameTable.load(path)
            wpa = HostNetwork(b"Home", "02:00:00:00:00:01", 6, -50, PSK)
            open_ = HostNetwork(b"Home", "02:00:00:00:00:02", 1, -40, OPEN)
            view = t.guest_view([wpa, open_])
            by_id = {n.id: n.name for n in view}
            self.assertEqual(sorted(by_id.values()), [b"Home", b"Home 2"])
            # the stronger one disappears: the other keeps its name
            again = NameTable.load(path).guest_view([wpa])
            self.assertEqual(again[0].name, by_id[wpa.id])
            self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")

    def test_suffix_fits_32_bytes(self):
        t = NameTable()
        long1 = HostNetwork(b"A" * 32, "02:00:00:00:00:01", 6, -50, PSK)
        long2 = HostNetwork(b"A" * 32, "02:00:00:00:00:02", 6, -50, OPEN)
        view = t.guest_view([long1, long2])
        self.assertEqual(sorted(len(n.name) for n in view), [32, 32])
        self.assertEqual(len({n.name for n in view}), 2)

    def test_grouping_exclusions_and_limit(self):
        t = NameTable()
        aps = [HostNetwork(b"Mesh", f"02:00:00:00:01:{i:02x}", 6, -40 - i, PSK) for i in range(3)]
        aps += [HostNetwork(b"Corp", "02:00:00:00:02:01", 36, -30, ENTERPRISE),
                HostNetwork(b"", "02:00:00:00:02:02", 6, -30, PSK),
                HostNetwork(b"Old", "02:00:00:00:02:03", 6, -30, names.WEP),
                HostNetwork(b"Old Known", "02:00:00:00:02:04", 6, -30, names.WEP, known=True)]
        aps += [HostNetwork(f"N{i}".encode(), f"02:00:00:00:03:{i:02x}", 6, -60, OPEN) for i in range(20)]
        aps.append(HostNetwork(b"Weak but mine", "02:00:00:00:04:01", 6, -95, PSK, known=True, active=True))
        view = t.guest_view(aps)
        names_ = [n.name for n in view]
        self.assertEqual(len(view), 16)
        self.assertEqual(names_[0], b"Weak but mine")                  # connected network always listed first
        mesh = next(n for n in view if n.name == b"Mesh")
        self.assertEqual((mesh.aps, mesh.bssid), (3, "02:00:00:00:01:00"))
        for excluded in (b"Corp", b"", b"Old"):
            self.assertNotIn(excluded, names_)
        self.assertIn(b"Old Known", names_)
        self.assertFalse(any(n.wire()["privacy"] for n in view))         # never a WEP prompt in the guest


class ProtocolTests(unittest.TestCase):
    def test_validate(self):
        self.assertEqual(validate({"type": "join", "seq": 3, "ssid": "486f6d65", "wep": False}),
                         {"type": "join", "seq": 3, "ssid": b"Home", "wep": False})
        self.assertEqual(validate({"type": "scan", "seq": 1}), {"type": "scan", "seq": 1})
        self.assertEqual(validate({"type": "state", "enabled": True, "mode": "ibss", "desired": ""})["mode"], "ibss")
        for bad in ({"type": "join", "seq": -1, "ssid": ""}, {"type": "join", "seq": 1, "ssid": "zz"},
                    {"type": "join", "seq": 1, "ssid": "00" * 33}, {"type": "scan"}, {"type": "evil"},
                    ["not", "a", "dict"], {"type": "scan", "seq": True}):
            self.assertIsNone(validate(bad), bad)


class FakeCard:
    """The QEMU side of the bridge, enough for policy tests."""

    def __init__(self, path):
        self.path = path
        self.msgs: asyncio.Queue = asyncio.Queue()
        self.seq = 0

    async def connect(self):
        for _ in range(100):
            try:
                self.reader, self.writer = await asyncio.open_unix_connection(str(self.path))
                break
            except OSError:
                await asyncio.sleep(0.02)
        self.task = asyncio.get_running_loop().create_task(self._read())
        self.send({"type": "hello", "version": 1, "mac": "02:52:42:00:00:01", "firmware": "8.40"})

    async def _read(self):
        while True:
            line = await self.reader.readline()
            if not line:
                return
            await self.msgs.put(json.loads(line))

    def send(self, obj):
        self.writer.write(json.dumps(obj).encode() + b"\n")

    async def expect(self, mtype, timeout=5, **match):
        end = asyncio.get_running_loop().time() + timeout
        while True:
            left = end - asyncio.get_running_loop().time()
            if left <= 0:
                raise AssertionError(f"no {mtype} {match}")
            m = await asyncio.wait_for(self.msgs.get(), left)
            if m.get("type") == mtype and all(m.get(k) == v for k, v in match.items()):
                return m

    def enable(self, ssid=b""):
        self.send({"type": "state", "enabled": True, "mode": "bss", "desired": ssid.hex()})
        self.seq += 1
        self.send({"type": "join", "seq": self.seq, "ssid": ssid.hex(), "wep": False})
        return self.seq

    async def close(self):
        self.writer.close()
        self.task.cancel()


class FakePrompter:
    def __init__(self, answers):
        self.answers = list(answers)
        self.asked = []
        self.finished = []
        self.ok = True

    def available(self):
        return self.ok

    async def ask(self, name, security, error, hidden):
        self.asked.append((name, security, error, hidden))
        return self.answers.pop(0) if self.answers else None

    async def working(self, name):
        pass

    async def finish(self, ok, message):
        self.finished.append((ok, message))


FIXTURE = {"radio": True, "networks": [
    {"ssid": "Open Lab", "bssid": "02:52:42:aa:00:01", "channel": 6, "signal": -48, "security": "--"},
    {"ssid": "Home", "bssid": "02:52:42:aa:00:02", "channel": 11, "signal": -55, "security": "WPA2",
     "password": "correct horse"},
    {"ssid": "Corp", "bssid": "02:52:42:aa:00:03", "channel": 36, "signal": -40, "security": "802.1X"}]}


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fixture = self.root / "nets.json"
        self.write_fixture(FIXTURE)
        self.events = []
        self.backend = FixtureBackend(self.fixture)
        self.backend.poll_interval = 0.05
        self.prompter = FakePrompter([])
        self.svc = AirPortService(self.backend, self.root / "airport.sock", NameTable(), self.prompter,
                                  event=lambda name, **kw: self.events.append((name, kw)))
        await self.svc.start()
        self.card = FakeCard(self.root / "airport.sock")
        await self.card.connect()
        await self.card.expect("hello", backend="fixture")
        await self.card.expect("networks")

    async def asyncTearDown(self):
        await self.card.close()
        await self.svc.stop()
        self.tmp.cleanup()

    def write_fixture(self, data):
        # Atomic replacement (a new inode), which the fixture backend detects
        # however close together two writes are; no clock games.
        tmp = self.fixture.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, self.fixture)

    async def test_scan_lists_joinable_networks_without_privacy(self):
        self.card.send({"type": "scan", "seq": 7})
        m = await self.card.expect("networks", seq=7)
        got = {bytes.fromhex(n["ssid"]): n for n in m["networks"]}
        self.assertEqual(set(got), {b"Open Lab", b"Home"})
        self.assertFalse(any(n["privacy"] for n in m["networks"]))
        self.assertEqual(got[b"Home"]["bssid"], "02:52:42:aa:00:02")

    async def test_join_any_reports_host_link(self):
        seq = self.card.enable(b"")
        m = await self.card.expect("link", seq=seq)
        self.assertEqual(m["state"], "searching")      # fixture host is not connected yet

    async def test_open_network_joins_without_prompt(self):
        seq = self.card.enable(b"Open Lab")
        prov = await self.card.expect("link", seq=seq)
        self.assertEqual((prov["state"], prov.get("provisional")), ("associated", True))
        final = await self.card.expect("link", seq=seq, state="associated")
        self.assertEqual(bytes.fromhex(final["ssid"]), b"Open Lab")
        self.assertEqual(self.prompter.asked, [])

    async def test_wpa_network_prompts_retries_and_joins(self):
        self.prompter.answers = ["wrong password", "correct horse"]
        seq = self.card.enable(b"Home")
        await self.card.expect("link", seq=seq, provisional=True)
        final = await self.card.expect("link", seq=seq, state="associated", timeout=10)
        self.assertNotIn("provisional", final)
        self.assertEqual([a[2] for a in self.prompter.asked], [None, "The password was not accepted."])
        self.assertEqual(self.prompter.finished, [(True, None)])
        # the password reached the host backend, never the card
        self.assertFalse(any("correct horse" in json.dumps(e) for e in self.events))
        self.assertEqual([j["with_secret"] for j in self.backend.joins], [False, True, True])

    async def test_cancel_fails_and_suppresses_reprompt(self):
        self.prompter.answers = [None]
        seq = self.card.enable(b"Home")
        await self.card.expect("link", seq=seq, provisional=True)
        m = await self.card.expect("link", seq=seq, state="failed")
        self.assertEqual(m["reason"], "cancelled")
        seq = self.card.enable(b"Home")                # the driver retries on its own
        m = await self.card.expect("link", seq=seq, state="failed")
        self.assertEqual(m["reason"], "suppressed")
        self.assertEqual(len(self.prompter.asked), 1)

    async def test_cancel_keeps_desired_on_the_associated_network(self):
        """Cancelling the password window for another network leaves Mac OS on the host's
        network; status must not keep reporting the declined one as desired (hardware: the
        Ryzen showed desired = a neighbour's network while Mac OS marked the right one)."""
        seq = self.card.enable(b"Open Lab")
        await self.card.expect("link", seq=seq, state="associated", provisional=None)
        self.prompter.answers = [None]
        self.card.seq += 1
        self.card.send({"type": "join", "seq": self.card.seq, "ssid": b"Home".hex(), "wep": False})
        m = await self.card.expect("link", seq=self.card.seq, state="failed")
        self.assertEqual(m["reason"], "cancelled")
        self.assertEqual(self.svc.public()["guest"]["desired"], "Open Lab")
        # The driver keeps "Home" in its desired-SSID register and reports it.
        self.card.send({"type": "state", "enabled": True, "mode": "bss", "desired": b"Home".hex()})
        await asyncio.sleep(0.2)
        self.assertEqual(self.svc.public()["guest"]["desired"], "Open Lab")
        # Choosing a network again is followed as before.
        seq = self.card.enable(b"Open Lab")
        await self.card.expect("link", seq=seq, state="associated")
        self.assertEqual(self.svc.public()["guest"]["desired"], "Open Lab")
        self.assertIsNone(self.svc.guest.declined)

    async def test_no_prompt_available(self):
        self.prompter.ok = False
        seq = self.card.enable(b"Home")
        m = await self.card.expect("link", seq=seq, state="failed")
        self.assertEqual(m["reason"], "no-prompt")

    async def test_host_link_changes_reach_the_card(self):
        seq = self.card.enable(b"Open Lab")
        await self.card.expect("link", seq=seq, state="associated", provisional=None)
        weak = json.loads(json.dumps(FIXTURE))
        weak["networks"][0]["signal"] = -86
        self.write_fixture(weak)
        m = await self.card.expect("link", state="associated", signal=-86)
        gone = {"radio": True, "networks": FIXTURE["networks"][1:]}
        self.write_fixture(gone)
        await self.card.expect("link", state="lost")
        self.write_fixture(FIXTURE)
        back = await self.card.expect("link", state="associated")
        self.assertEqual(back["bssid"], m["bssid"])
        self.write_fixture(dict(FIXTURE, radio=False))
        m = await self.card.expect("link", state="lost")
        self.assertEqual(m["radio"], "off")

    async def test_guest_radio_off_does_not_touch_host(self):
        seq = self.card.enable(b"Open Lab")
        await self.card.expect("link", seq=seq, state="associated", provisional=None)
        self.card.send({"type": "leave"})
        self.card.send({"type": "state", "enabled": False, "mode": "bss", "desired": b"Open Lab".hex()})
        await asyncio.sleep(0.3)
        link = await self.backend.link()
        self.assertIsNotNone(link.connected)
        self.assertIn(("airport_guest_radio", {"radio": "off", "mode": "bss"}), self.events)

    async def test_reconnect_gets_fresh_hello(self):
        await self.card.close()
        self.card = FakeCard(self.root / "airport.sock")
        await self.card.connect()
        await self.card.expect("hello")
        self.assertEqual(self.svc.bridge.connections, 2)

    async def test_hostd_restart_card_reconnects_and_rejoins(self):
        # hostd stops (socket removed) and starts again while the guest is
        # associated; the card reconnects and re-sends hello/state/join like
        # QEMU's retroboot-airport, and gets the host's association back.
        seq = self.card.enable(b"Open Lab")
        await self.card.expect("link", seq=seq, state="associated", provisional=None)
        await self.svc.stop()
        self.assertFalse((self.root / "airport.sock").exists())
        await asyncio.wait_for(self.card.task, 5)                       # the card sees the host go away (EOF)
        await self.card.close()
        self.svc = AirPortService(self.backend, self.root / "airport.sock", NameTable(), self.prompter,
                                  event=lambda name, **kw: self.events.append((name, kw)))
        await self.svc.start()
        self.card = FakeCard(self.root / "airport.sock")
        await self.card.connect()
        await self.card.expect("hello", backend="fixture")
        nets = await self.card.expect("networks")
        self.assertIn(b"Open Lab", {bytes.fromhex(n["ssid"]) for n in nets["networks"]})
        seq = self.card.enable(b"Open Lab")
        m = await self.card.expect("link", seq=seq)
        self.assertEqual((m["state"], m.get("provisional")), ("associated", None))
        self.assertEqual(bytes.fromhex(m["ssid"]), b"Open Lab")
        self.assertEqual(self.svc.public()["guest"]["connected"], True)

    async def test_stale_socket_file_is_replaced(self):
        # hostd killed without cleanup leaves the socket file behind (QEMU then
        # gets "connection refused" until hostd listens again)
        await self.card.close()
        await self.svc.stop()
        import socket
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(self.root / "airport.sock"))
        stale.close()
        with self.assertRaises(ConnectionRefusedError):
            await asyncio.open_unix_connection(str(self.root / "airport.sock"))
        self.svc = AirPortService(self.backend, self.root / "airport.sock", NameTable(), self.prompter)
        await self.svc.start()
        self.card = FakeCard(self.root / "airport.sock")
        await self.card.connect()
        await self.card.expect("hello")

    async def test_hidden_network_by_name(self):
        hidden = json.loads(json.dumps(FIXTURE))
        hidden["networks"].append({"ssid": "Secret", "bssid": "02:52:42:aa:00:09", "channel": 3, "signal": -60,
                                   "security": "WPA2", "password": "hidden pass", "hidden": True})
        self.write_fixture(hidden)
        self.card.send({"type": "scan", "seq": 9})
        m = await self.card.expect("networks", seq=9)
        self.assertNotIn(b"Secret", {bytes.fromhex(n["ssid"]) for n in m["networks"]})
        self.prompter.answers = ["hidden pass"]
        # typed with "Other…": not in the list, joined as a hidden network
        seq = self.card.enable(b"Secret")
        final = await self.card.expect("link", seq=seq, state="associated", provisional=None, timeout=10)
        self.assertEqual(bytes.fromhex(final["ssid"]), b"Secret")
        self.assertEqual(self.prompter.asked[0][3], True)                 # hidden prompt
        self.assertTrue(self.backend.joins[-1]["hidden"])


CONNECTED = {"radio": True, "networks": [dict(FIXTURE["networks"][0], connected=True)] + FIXTURE["networks"][1:]}


class StartupJoinTests(unittest.IsolatedAsyncioTestCase):
    """AirPort's own rejoin while Mac OS starts follows the host: it never changes the
    host's Wi-Fi and never asks for a password (research/airport/reports/airstart-2026-10-12.md)."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fixture = self.root / "nets.json"
        self.fixture.write_text(json.dumps(CONNECTED))
        self.events = []
        self.starting = True
        self.backend = FixtureBackend(self.fixture)
        self.backend.poll_interval = 0.05
        self.prompter = FakePrompter([])
        self.names = NameTable.load(self.root / "airport-names.json")
        self.svc = AirPortService(self.backend, self.root / "airport.sock", self.names, self.prompter,
                                  event=lambda name, **kw: self.events.append((name, kw)),
                                  startup=lambda: self.starting)
        await self.svc.start()
        self.card = FakeCard(self.root / "airport.sock")
        await self.card.connect()
        await self.card.expect("hello")
        await self.card.expect("networks")
        self.open_lab = names.network_id(b"Open Lab", OPEN)

    async def asyncTearDown(self):
        await self.card.close()
        await self.svc.stop()
        self.tmp.cleanup()

    def joins(self):
        return [kw for n, kw in self.events if n == "airport_join"]

    async def test_remembered_network_the_host_is_on(self):
        seq = self.card.enable(b"Open Lab")
        m = await self.card.expect("link", seq=seq)
        self.assertEqual((m["state"], bytes.fromhex(m["ssid"]), m.get("provisional")),
                         ("associated", b"Open Lab", None))
        self.assertEqual(self.joins()[-1], {"network": self.open_lab, "startup": True,
                                            "result": "already-connected"})
        self.assertEqual(self.backend.joins, [])

    async def test_another_remembered_network_follows_the_host(self):
        # e.g. a WPA network without a host profile, or a neighbour's open network chosen
        # once by mistake: no password window, no host join, Mac OS shows the host's network
        seq = self.card.enable(b"Home")
        m = await self.card.expect("link", seq=seq)
        self.assertEqual((m["state"], bytes.fromhex(m["ssid"]), m.get("provisional")),
                         ("associated", b"Open Lab", None))
        self.assertEqual(self.joins()[-1]["result"], "follows-host")
        self.assertEqual((self.prompter.asked, self.backend.joins), ([], []))
        self.assertTrue(self.svc.public()["guest"]["associated"])

    async def test_host_not_connected_yet(self):
        # a cold boot racing NetworkManager: AirPort gets an answer within its two
        # seconds, then out of range, then the host's network once it is connected
        self.backend.connected_id = None
        await asyncio.sleep(0.15)
        seq = self.card.enable(b"Open Lab")
        m = await self.card.expect("link", seq=seq)
        self.assertEqual((m["state"], m.get("provisional")), ("associated", True))
        self.assertEqual(self.joins()[-1]["result"], "waiting-for-host")
        await self.card.expect("link", state="lost")
        self.backend.connected_id = self.open_lab
        m = await self.card.expect("link", state="associated")
        self.assertEqual(m["bssid"], "02:52:42:aa:00:01")
        self.assertEqual(self.backend.joins, [])

    async def test_after_startup_the_guest_choice_drives_the_host(self):
        self.starting = False
        self.prompter.answers = ["correct horse"]
        seq = self.card.enable(b"Home")
        await self.card.expect("link", seq=seq, provisional=True)
        m = await self.card.expect("link", seq=seq, state="associated", provisional=None, timeout=10)
        self.assertEqual(bytes.fromhex(m["ssid"]), b"Home")
        self.assertEqual(len(self.prompter.asked), 1)

    async def test_probe_answer_after_disable_is_not_an_association(self):
        # AirPort probes the card with ENABLE/DISABLE at every start: hostd's answer arrives
        # after the port is off, and the card ignores it.  Status said "associated" with the
        # guest radio off (fleet4, Ryzen).
        for _ in range(2):
            self.card.enable(b"")
            self.card.send({"type": "leave"})
            self.card.send({"type": "state", "enabled": False, "mode": "bss", "desired": ""})
        await asyncio.sleep(0.3)
        g = self.svc.public()["guest"]
        self.assertEqual((g["radio"], g["associated"]), ("off", False))
        seq = self.card.enable(b"Open Lab")
        await self.card.expect("link", seq=seq, state="associated")
        self.assertTrue(self.svc.public()["guest"]["associated"])

    async def test_startup_network_sources(self):
        await asyncio.sleep(0.15)                                  # a poll saw the host
        want = await self.svc.startup_network()
        self.assertEqual(want, {"name": b"Open Lab", "network": self.open_lab, "source": "host"})
        self.assertEqual(json.loads((self.root / "airport-names.json").read_text())["last_host"],
                         {"fixture": self.open_lab})
        self.backend.connected_id = None
        await asyncio.sleep(0.15)
        want = await self.svc.startup_network()
        self.assertEqual(want, {"name": b"Open Lab", "network": self.open_lab, "source": "last-host"})
        again = NameTable.load(self.root / "airport-names.json")
        self.assertEqual(again.last_host_name("fixture"), (self.open_lab, b"Open Lab"))
        self.assertIsNone(again.last_host_name("networkmanager"))

    async def test_startup_network_before_the_first_poll_and_unknown(self):
        svc = AirPortService(FixtureBackend(self.fixture), self.root / "other.sock", NameTable())
        self.assertEqual((await svc.startup_network())["source"], "host")    # asks the backend
        empty = self.root / "empty.json"
        empty.write_text(json.dumps(FIXTURE))
        svc = AirPortService(FixtureBackend(empty), self.root / "other.sock", NameTable())
        self.assertIsNone(await svc.startup_network())


class StartupNetworkVirtualTests(unittest.IsolatedAsyncioTestCase):
    async def test_virtual_network_even_with_the_uplink_down(self):
        with tempfile.TemporaryDirectory() as tmp:
            sysfs = Path(tmp)
            (sysfs / "eth9").mkdir()
            (sysfs / "eth9" / "operstate").write_text("down\n")
            (sysfs / "eth9" / "carrier").write_text("0\n")
            be = VirtualBackend(uplink="eth9", sysfs=sysfs)
            svc = AirPortService(be, Path(tmp) / "a.sock", NameTable())
            want = await svc.startup_network()
            self.assertEqual((want["name"], want["source"]), (b"RetroBoot", "virtual"))
            (sysfs / "eth9" / "operstate").write_text("up\n")
            (sysfs / "eth9" / "carrier").write_text("1\n")
            want = await svc.startup_network()
            self.assertEqual((want["name"], want["source"]), (b"RetroBoot", "host"))
            self.assertEqual(svc.names.last_host, {})              # nothing to remember


class FixtureBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_replacement_within_one_clock_tick_is_seen(self):
        # The kernel's file clock is coarse (4 ms at HZ=250): two replacements
        # can carry the same mtime and size.  The backend must still reload
        # (this made ServiceTests.test_hidden_network_by_name flaky).
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nets.json"

            def write(ssid):
                data = {"radio": True, "networks": [{"ssid": ssid, "bssid": "02:52:42:aa:00:01",
                                                     "channel": 6, "signal": -50, "security": "--"}]}
                t = path.with_suffix(".tmp")
                t.write_text(json.dumps(data))
                os.utime(t, ns=(1_700_000_000_000_000_000,) * 2)
                os.replace(t, path)

            write("AAAA")
            b = FixtureBackend(path)
            self.assertEqual([n.ssid for n in await b.scan()], [b"AAAA"])
            write("BBBB")                                   # same size, same mtime, new inode
            self.assertEqual([n.ssid for n in await b.scan()], [b"BBBB"])


class VirtualBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_follows_the_uplink(self):
        with tempfile.TemporaryDirectory() as tmp:
            sysfs = Path(tmp) / "net"
            (sysfs / "rb0").mkdir(parents=True)
            (sysfs / "rb0" / "operstate").write_text("up\n")
            (sysfs / "rb0" / "carrier").write_text("1\n")
            b = VirtualBackend(uplink="rb0", sysfs=sysfs)
            nets = await b.scan()
            self.assertEqual([(n.ssid, n.security, n.known) for n in nets], [(b"RetroBoot", OPEN, True)])
            self.assertIsNotNone((await b.link()).connected)
            await b.join(nets[0])
            (sysfs / "rb0" / "carrier").write_text("0\n")
            self.assertEqual(await b.scan(), [])
            self.assertIsNone((await b.link()).connected)
            with self.assertRaises(JoinError):
                await b.join(nets[0])
            with self.assertRaises(JoinError):
                await b.join(HostNetwork(b"Elsewhere", "", 0, -60, OPEN))

    def test_default_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            route = Path(tmp) / "route"
            route.write_text("Iface\tDestination\tGateway\tFlags\n"
                             "ens18\t0064A8C0\t00000000\t0001\nens18\t00000000\t0164A8C0\t0003\n")
            from retroboot.airport.backends import default_route_iface
            self.assertEqual(default_route_iface(route), "ens18")


class NetworkManagerTests(unittest.IsolatedAsyncioTestCase):
    """Real subprocess runner against tests/fakes/fake_nmcli.py on PATH."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        bindir = root / "bin"
        bindir.mkdir()
        shim = bindir / "nmcli"
        shim.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKES / 'fake_nmcli.py'} \"$@\"\n")
        shim.chmod(0o755)
        self.state = root / "nm.json"
        self.write({"device": "wlan0", "radio": True, "active": None, "profiles": [], "aps": [
            {"ssid": "Home", "bssid": "aa:bb:cc:00:00:01", "chan": 6, "signal": 80, "security": "WPA2",
             "password": "secret123"},
            {"ssid": "Home", "bssid": "aa:bb:cc:00:00:02", "chan": 1, "signal": 40, "security": "WPA2",
             "password": "secret123"},
            {"ssid": "Cafe", "bssid": "aa:bb:cc:00:00:03", "chan": 11, "signal": 60, "security": ""},
            {"ssid": "Modern", "bssid": "aa:bb:cc:00:00:04", "chan": 36, "signal": 70, "security": "WPA3",
             "password": "sae-pass-1"},
            {"ssid": "Office", "bssid": "aa:bb:cc:00:00:05", "chan": 44, "signal": 90, "security": "WPA2 802.1X"}]})
        self.env = {"PATH": os.environ.get("PATH", ""), "FAKE_NMCLI_STATE": os.environ.get("FAKE_NMCLI_STATE")}
        os.environ["PATH"] = f"{bindir}:{os.environ.get('PATH', '')}"
        os.environ["FAKE_NMCLI_STATE"] = str(self.state)
        self.nm_dir = root / "system-connections"
        self.adapter = NetworkAdapter(nm_connections_dir=self.nm_dir)
        self.b = NetworkManagerBackend(self.adapter)

    async def asyncTearDown(self):
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def write(self, st):
        self.state.write_text(json.dumps(st))

    def read(self):
        return json.loads(self.state.read_text())

    async def test_recent_profile_for_a_first_boot(self):
        self.assertIsNone(await self.b.recent_profile())
        st = self.read()
        st["profiles"] = [
            {"name": "Cafe", "uuid": "11111111-0000-0000-0000-000000000001", "ssid": "Cafe", "key_mgmt": "",
             "timestamp": 1791000100},
            {"name": "Home", "uuid": "11111111-0000-0000-0000-000000000002", "ssid": "Home", "key_mgmt": "wpa-psk",
             "timestamp": 1791000200},
            {"name": "Never", "uuid": "11111111-0000-0000-0000-000000000003", "ssid": "Never", "key_mgmt": "sae"}]
        self.write(st)
        net = await self.b.recent_profile()
        self.assertEqual((net.ssid, net.security), (b"Home", PSK))
        self.assertEqual(net.id, names.network_id(b"Home", PSK))       # the id its scan has
        # what AirPort gets at launch when the host is not connected and nothing is remembered
        svc = AirPortService(self.b, Path(self.tmp.name) / "a.sock", NameTable())
        want = await svc.startup_network()
        self.assertEqual((want["name"], want["source"]), (b"Home", "profile"))

    def test_parse_list_and_signal(self):
        rows = parse_wifi_list("*:486F6D65:AA\\:BB\\:CC\\:00\\:00\\:01:6:80:WPA2\n :--:AA\\:BB:1:10:--\n")
        self.assertEqual(rows[0], {"in_use": True, "ssid": b"Home", "bssid": "aa:bb:cc:00:00:01", "channel": 6,
                                   "signal": 80, "security": PSK})
        self.assertEqual(rows[1]["ssid"], b"")
        self.assertEqual((signal_dbm(100), signal_dbm(0), signal_dbm(60)), (-50, -100, -70))

    async def test_scan_and_link(self):
        nets = await self.b.scan()
        self.assertEqual(len(nets), 5)
        home = [n for n in nets if n.ssid == b"Home"]
        self.assertEqual({n.signal for n in home}, {-60, -80})
        self.assertFalse(any(n.known for n in home))
        self.assertTrue(next(n for n in nets if n.ssid == b"Cafe").known)        # open: no secret needed
        self.assertEqual(await self.b.link(), HostLink(True, None))

    async def test_new_wpa2_profile_wrong_then_right(self):
        net = next(n for n in await self.b.scan() if n.ssid == b"Home")
        with self.assertRaises(JoinError) as cm:
            await self.b.join(net)
        self.assertEqual(cm.exception.code, "needs-secret")
        with self.assertRaises(JoinError) as cm:
            await self.b.join(net, "not-the-password")
        self.assertEqual(cm.exception.code, "wrong-password")
        st = self.read()
        self.assertEqual(st["profiles"], [])                               # broken profile removed
        self.assertEqual(list(self.nm_dir.iterdir()), [])
        await self.b.join(net, "secret123")
        st = self.read()
        self.assertEqual(len(st["profiles"]), 1)
        self.assertEqual((st["profiles"][0]["key_mgmt"], st["profiles"][0]["file_mode"]), ("wpa-psk", 0o600))
        self.assertFalse(any("secret123" in " ".join(c) for c in st["calls"]))   # never in argv
        link = await self.b.link()
        self.assertEqual((link.connected.ssid, link.connected.bssid), (b"Home", "aa:bb:cc:00:00:01"))
        # a later join of the same network uses the saved profile, no secret
        self.write(dict(self.read(), active=None))
        await self.b.join(net)
        self.assertEqual(self.read()["active"]["uuid"], st["profiles"][0]["uuid"])

    async def test_transition_network_wrong_password_reads_as_wrong_password(self):
        # WPA2/WPA3 transition AP (GPD MicroPC lab, Intel AC 7265): SAE rejects
        # the password and NetworkManager reports ssid-not-found.
        st = self.read()
        st["aps"][0]["sae_reject"] = True
        st["aps"][1]["sae_reject"] = True
        self.write(st)
        net = next(n for n in await self.b.scan() if n.ssid == b"Home")
        with self.assertRaises(JoinError) as cm:
            await self.b.join(net, "dummywrong1")
        self.assertEqual(cm.exception.code, "wrong-password")
        self.assertEqual(self.read()["profiles"], [])
        await self.b.join(net, "secret123")
        self.assertEqual(len(self.read()["profiles"]), 1)

    async def test_wpa3_only_uses_sae(self):
        net = next(n for n in await self.b.scan() if n.ssid == b"Modern")
        self.assertEqual(net.security, SAE)
        await self.b.join(net, "sae-pass-1")
        self.assertEqual(self.read()["profiles"][0]["key_mgmt"], "sae")

    async def test_enterprise_and_radio(self):
        office = next(n for n in await self.b.scan() if n.ssid == b"Office")
        with self.assertRaises(JoinError):
            await self.b.join(office, "x" * 10)
        self.write(dict(self.read(), radio=False))
        self.assertEqual(await self.b.link(), HostLink(False, None))
        self.assertEqual(await self.b.scan(), [])
        self.assertTrue(await self.b.radio_on())
        self.assertTrue(self.read()["radio"])

    async def test_open_network_and_hidden(self):
        cafe = next(n for n in await self.b.scan() if n.ssid == b"Cafe")
        await self.b.join(cafe)
        prof = self.read()["profiles"][0]
        self.assertEqual((prof["ssid"], prof["psk"]), ("Cafe", None))
        with self.assertRaises(JoinError) as cm:
            await self.b.join(HostNetwork(b"Nowhere", "", 0, -60, PSK), "password1", hidden=True)
        self.assertEqual(cm.exception.code, "not-found")


class HookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pkg = Path(self.tmp.name) / "pkg"
        self.pkg.mkdir()
        self.paths = types.SimpleNamespace(qemu_package=self.pkg, run=Path(self.tmp.name) / "run")

    def tearDown(self):
        self.tmp.cleanup()

    def capable(self, yes=True):
        (self.pkg / "manifest.json").write_text(json.dumps({"features": ["airport-bridge-1"] if yes else []}))

    def machine(self, report=None):
        return types.SimpleNamespace(meta={"prep": {"result": "applied", "report": report or {}}})

    def test_setting_is_declared_with_capability(self):
        s = schema.BY_KEY["network.guest_adapter"]
        self.assertEqual((s.default, s.choices, s.apply, s.capability), ("ethernet", ("ethernet", "airport"),
                                                                         schema.GUEST_RESTART, "airport"))
        ctx = schema.ValidationContext(capabilities={"airport": False})
        with self.assertRaises(schema.ValidationError):
            schema.validate_changes(schema.defaults(), {"network.guest_adapter": "airport"}, ctx)
        ctx.capabilities["airport"] = True
        schema.validate_changes(schema.defaults(), {"network.guest_adapter": "airport"}, ctx)

    def test_nic_args(self):
        base = {"network.guest": "nat", "network.guest_adapter": "airport"}
        self.capable(False)
        self.assertEqual(airport.guest_nic_args(base, self.paths, self.machine()), ["-nic", "user,model=sungem"])
        self.capable(True)
        args = airport.guest_nic_args(base, self.paths, self.machine())
        self.assertEqual(args[:2], ["-nic", "none"])
        self.assertIn("macio-newworld.airport=true", args)
        self.assertIn(f"socket,id=airport0,path={self.paths.run / 'airport.sock'},reconnect-ms=1000", args)
        absent = self.machine({"airport_software": "absent"})
        self.assertEqual(airport.guest_nic_args(base, self.paths, absent), ["-nic", "user,model=sungem"])
        self.assertEqual(airport.guest_nic_args(dict(base, **{"network.guest": "none"}), self.paths, None),
                         ["-nic", "none"])
        self.assertEqual(airport.guest_nic_args({"network.guest": "nat"}, self.paths, None),
                         ["-nic", "user,model=sungem"])
        self.assertEqual(airport.prep_options(base, self.pkg), {"network_port": "airport"})
        self.capable(False)
        self.assertEqual(airport.prep_options(base, self.pkg), {"network_port": "ethernet"})

    def test_guestprep_passes_options_only_when_supported(self):
        from retroboot.guestprep import GuestPrep

        class New:
            RETROBOOT_TEST_FAKE = True
            calls = []

            def apply(self, image, *, network_port="ethernet"):
                self.calls.append(network_port)
                return {"network_port": network_port}

        class Old(helpers.FakePrepModule):
            pass

        for mod, expect in ((New(), ["airport"]), (Old(), None)):
            img = Path(self.tmp.name) / "disk.img"
            img.write_bytes(b"\0" * 1024)
            gp = GuestPrep(Path(self.tmp.name), module=mod)
            gp.options_provider = lambda: {"network_port": "airport"}
            res = asyncio.run(gp.apply(img))
            self.assertEqual(res["result"], "applied")
            if expect is not None:
                self.assertEqual(mod.calls, expect)


class SeedTests(unittest.IsolatedAsyncioTestCase):
    """AirPort.prepare_launch names the host's network as AirPort's startup network."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        pkg = Path(self.tmp.name) / "pkg"
        pkg.mkdir()
        (pkg / "manifest.json").write_text(json.dumps({"features": ["airport-bridge-1"]}))
        self.events, self.calls = [], []
        test = self

        class GP:
            async def airport_network(self, image, name):
                test.calls.append((image, name))
                return {"result": "created", "seconds": 0.5}

        class Svc:
            want = {"name": b"Open Lab", "network": "n0123456789abcdef", "source": "host"}

            async def startup_network(self):
                return self.want

        self.app = types.SimpleNamespace(
            settings=types.SimpleNamespace(applied={"network.guest": "nat", "network.guest_adapter": "airport"}),
            paths=types.SimpleNamespace(qemu_package=pkg), guestprep=GP(),
            lifecycle=types.SimpleNamespace(event=lambda n, **kw: self.events.append((n, kw))))
        self.ap = airport.AirPort(self.app)
        self.ap.service = Svc()
        self.machine = types.SimpleNamespace(id="m1", disk=Path("/m1/disk.img"), meta={"prep": {
            "result": "applied", "report": {"airport_software": "present", "network_port": "airport"}}})

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def test_seeds_with_the_card(self):
        await self.ap.prepare_launch(self.machine)
        self.assertEqual(self.calls, [(Path("/m1/disk.img"), b"Open Lab")])
        self.assertEqual(self.events[-1], ("airport_startup_network", {
            "machine": "m1", "network": "n0123456789abcdef", "basis": "host", "result": "created",
            "seconds": 0.5}))

    async def test_not_with_ethernet_or_without_airport_software(self):
        self.app.settings.applied["network.guest_adapter"] = "ethernet"
        self.machine.meta["prep"]["report"]["network_port"] = "ethernet"
        await self.ap.prepare_launch(self.machine)
        self.app.settings.applied["network.guest_adapter"] = "airport"
        self.machine.meta["prep"]["report"] = {"airport_software": "absent", "network_port": "ethernet"}
        await self.ap.seed_startup_network(self.machine)
        self.ap.service = None
        self.machine.meta["prep"]["report"] = {"airport_software": "present", "network_port": "airport"}
        await self.ap.seed_startup_network(self.machine)
        self.assertEqual(self.calls, [])

    async def test_no_host_network_known(self):
        self.ap.service.want = None
        await self.ap.seed_startup_network(self.machine)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.events[-1], ("airport_startup_network", {"machine": "m1",
                                                                       "result": "no-host-network"}))


class GuestPrepAirPortTests(unittest.IsolatedAsyncioTestCase):
    """GuestPrep.airport_network: in place when the setting exists, else on a copy."""

    class Mod:
        def __init__(self, first):
            self.first, self.calls = first, []

        def set_network(self, image, name, create=False):
            self.calls.append((Path(image).name, name, create))
            if isinstance(self.first, Exception):
                raise self.first
            if not create:
                return self.first
            with open(image, "r+b") as f:
                f.write(b"AIRPORT!")
            return "created"

    async def run_with(self, first):
        from retroboot.guestprep import GuestPrep
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        img = Path(tmp.name) / "disk.img"
        img.write_bytes(b"\0" * 4096)
        gp = GuestPrep(Path(tmp.name), module=helpers.FakePrepModule())
        gp.airport_module = self.Mod(first)
        res = await gp.airport_network(img, b"Open Lab")
        return res, gp.airport_module.calls, img.read_bytes()[:8], sorted(p.name for p in Path(tmp.name).iterdir())

    async def test_in_place(self):
        res, calls, head, files = await self.run_with("set")
        self.assertEqual((res["result"], calls, head), ("set", [("disk.img", b"Open Lab", False)], bytes(8)))
        self.assertEqual(files, ["disk.img"])

    async def test_created_on_a_copy(self):
        res, calls, head, files = await self.run_with("missing")
        self.assertEqual(res["result"], "created")
        self.assertEqual(calls, [("disk.img", b"Open Lab", False), (".disk.img.prep", b"Open Lab", True)])
        self.assertEqual((head, files), (b"AIRPORT!", ["disk.img"]))

    async def test_failure_never_raises(self):
        res, _calls, head, _files = await self.run_with(OSError("boom"))
        self.assertEqual((res["result"], head), ("failed", bytes(8)))
        self.assertIn("boom", res["error"])

    async def test_unavailable_without_the_module(self):
        from retroboot.guestprep import GuestPrep
        gp = GuestPrep(Path("/nonexistent"))
        self.assertEqual((await gp.airport_network(Path("/x.img"), b"N"))["result"], "unavailable")


if __name__ == "__main__":
    unittest.main()


class FakeHostApp:
    """Just enough of Hostd for the real Panel and PanelPrompter."""

    def __init__(self):
        self.mode = "normal"
        self.loop = asyncio.get_running_loop()
        self.docs, self.scenes = [], []
        app = self

        class Presenter:
            connected = asyncio.Event()

            async def try_request(self, cmd, **kw):
                if cmd == "ui_show":
                    app.docs.append(kw["doc"])
                return {}
        self.presenter = Presenter()
        self.presenter.connected.set()
        self.supervisor = types.SimpleNamespace(state="running", content_seen=True, paused=False, active=True)

    async def show_scene(self, scene, **kw):
        self.scenes.append((scene, kw))


class PanelPrompterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from retroboot.airport.panel import PanelPrompter
        from retroboot.panel.controller import Panel
        self.app = FakeHostApp()
        self.app.panel = Panel(self.app)
        self.p = PanelPrompter(self.app)

    async def action(self, doc, id, **values):
        for _ in range(100):
            if self.app.docs and self.app.docs[-1]["id"] == doc:
                break
            await asyncio.sleep(0.01)
        await self.app.panel.on_event({"event": "ui_action", "doc": doc, "id": id, "values": values})

    async def test_password_join_and_return_to_mac_os(self):
        self.assertTrue(self.p.available())
        task = asyncio.create_task(self.p.ask("Home", "WPA2 Personal", None, False))
        await self.action("airport-password", "join", psk="short")      # rejected locally
        await asyncio.sleep(0.05)
        self.assertIn("8 to 63", json.dumps(self.app.docs[-1]))
        await self.action("airport-password", "join", psk="secret123")
        self.assertEqual(await task, "secret123")
        self.assertEqual(self.app.scenes[0], ("panel", {"background": "guest-dimmed"}))
        self.assertNotIn("secret123", json.dumps(self.app.docs))         # never echoed into a document
        await self.p.working("Home")
        self.assertEqual(self.app.docs[-1]["id"], "airport-joining")
        await self.p.finish(True, None)
        self.assertEqual(self.app.scenes[-1], ("guest", {"reveal": "now"}))
        self.assertFalse(self.app.panel.visible)

    async def test_cancel_and_failure_alert(self):
        task = asyncio.create_task(self.p.ask("Home", "WPA2 Personal", "The password was not accepted.", False))
        await asyncio.sleep(0.05)
        self.assertIn("The password was not accepted.", json.dumps(self.app.docs[-1]))
        await self.action("airport-password", "cancel")
        self.assertIsNone(await task)
        self.assertFalse(self.app.panel.visible)                         # Cancel returns to Mac OS
        self.assertEqual(self.app.scenes[-1], ("guest", {"reveal": "now"}))

    async def test_replaced_by_recovery_counts_as_cancel(self):
        task = asyncio.create_task(self.p.ask("Hidden", "", None, True))
        await asyncio.sleep(0.05)
        self.assertIn("leave the password empty", json.dumps(self.app.docs[-1]))
        await self.app.panel.message("note", "Recovery", None)
        self.assertIsNone(await asyncio.wait_for(task, 3))

    async def test_not_available_while_another_panel_or_paused(self):
        await self.app.panel.message("note", "Something else", None)
        self.assertFalse(self.p.available())
        self.app.panel.forget()
        self.app.supervisor.paused = True
        self.assertFalse(self.p.available())
        self.app.supervisor.paused = False
        self.app.supervisor.content_seen = False
        self.assertFalse(self.p.available())

    async def test_no_window_during_startup(self):
        # AirPort's boot-time rejoin of a remembered network without a host
        # profile must not put a password window over the startup screen.
        from retroboot.util import mono
        sup = self.app.supervisor
        sup.finder_seen, sup.t_start = False, mono()
        self.assertFalse(self.p.available())
        sup.t_start = mono() - 600           # Finder probe missed it: long after the launch
        self.assertTrue(self.p.available())
        sup.finder_seen, sup.t_start = True, mono()
        self.assertTrue(self.p.available())
        # the supervisor's mac_os_up decides when it has one (the Finder in any theme, or
        # Mac OS started without it)
        sup.finder_seen, sup.mac_os_up = False, True
        self.assertTrue(self.p.available())
        sup.mac_os_up = False
        self.assertFalse(self.p.available())


class PollPaceTests(unittest.IsolatedAsyncioTestCase):
    """The host link is polled at the backend's pace only while a card is connected
    (NetworkManager: three nmcli runs every 2 s); otherwise every IDLE_POLL_S, and a card
    that connects brings the fast pace back at once."""

    async def asyncSetUp(self):
        from retroboot.airport import service as service_mod
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "nets.json").write_text(json.dumps(FIXTURE))
        self.backend = FixtureBackend(self.root / "nets.json")
        self.backend.poll_interval = 0.05
        self.calls = 0
        real = self.backend.link

        async def counted():
            self.calls += 1
            return await real()
        self.backend.link = counted
        self.old_idle, service_mod.IDLE_POLL_S = service_mod.IDLE_POLL_S, 0.8
        self.mod = service_mod
        self.svc = AirPortService(self.backend, self.root / "airport.sock", NameTable(), FakePrompter([]),
                                  event=lambda name, **kw: None)
        await self.svc.start()

    async def asyncTearDown(self):
        self.mod.IDLE_POLL_S = self.old_idle
        await self.svc.stop()
        self.tmp.cleanup()

    async def test_slow_without_a_card_fast_with_one(self):
        await asyncio.sleep(0.4)
        self.assertEqual(self.calls, 1)                    # the first poll, then the idle pace
        self.assertEqual(self.svc.poll_delay(), 0.8)
        card = FakeCard(self.root / "airport.sock")
        await card.connect()
        await card.expect("hello", backend="fixture")
        before = self.calls
        await asyncio.sleep(0.4)
        self.assertGreaterEqual(self.calls - before, 4)    # woken at once, then every 0.05 s
        self.assertEqual(self.svc.poll_delay(), 0.05)
        await card.close()
        for _ in range(50):
            if not self.svc.guest.connected:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)
        settled = self.calls
        await asyncio.sleep(0.4)
        self.assertLessEqual(self.calls - settled, 1)
