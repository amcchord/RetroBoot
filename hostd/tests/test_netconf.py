"""Host Wi-Fi profiles on the data partition, the installation-media network file
(retroboot/network.conf) and the installer carrying Wi-Fi to the installed system.

Every password here is a dummy value; the tests also check that none of them
reaches a log, a lifecycle event, an error message or a repr.
"""
import asyncio
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

import helpers
from fakes.fake_presenter import FakePresenter
from retroboot import netconf
from retroboot.config import Paths
from retroboot.hostd import Hostd
from retroboot.network import NetworkAdapter, NetworkError

FAKE_NMCLI = helpers.FAKES / "fake_nmcli.py"
DUMMY = "dummy-pass-7Q"            # never a real secret
APS = [{"ssid": "LabNet", "bssid": "aa:bb:cc:00:00:01", "chan": 36, "signal": 80, "security": "WPA2",
        "password": DUMMY},
       {"ssid": "Cafe", "bssid": "aa:bb:cc:00:00:02", "chan": 6, "signal": 40, "security": ""}]
HARDWARE = {"vendor": "Realtek Semiconductor Co., Ltd.",
            "product": "RTL8821CE 802.11ac PCIe Wireless Network Adapter", "driver": "rtw_8821ce"}


def fake_runner(state: Path):
    """nmcli -> tests/fakes/fake_nmcli.py on STATE; ip -> no addresses; the rest succeed."""
    async def run(argv, timeout=None, **kw):
        if argv[0] == "nmcli":
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(FAKE_NMCLI), *argv[1:], stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=dict(os.environ, FAKE_NMCLI_STATE=str(state)))
            out, err = await proc.communicate()
            return proc.returncode, out.decode(), err.decode()
        if argv[0] == "ip":
            return 0, "[]", ""
        return 0, "", ""
    return run


def write_state(path: Path, **extra) -> None:
    st = {"device": "wlp1s0", "radio": True, "aps": APS, "profiles": [], "active": None, "hardware": HARDWARE}
    st.update(extra)
    path.write_text(json.dumps(st))


class ParseTests(unittest.TestCase):
    def test_minimal_and_quoted(self):
        net = netconf.parse('# lab\nWIFI_SSID="My Net"\nexport WIFI_PASSWORD=\'pa ss=word\'\n')
        self.assertEqual((net.ssid, net.password, net.key_mgmt, net.hidden), ("My Net", "pa ss=word", "wpa-psk", False))

    def test_security_variants(self):
        self.assertEqual(netconf.parse("WIFI_SSID=x\nWIFI_SECURITY=WPA3\nWIFI_PASSWORD=12345678").key_mgmt, "sae")
        self.assertEqual(netconf.parse("WIFI_SSID=x\nWIFI_SECURITY=open").key_mgmt, "open")
        self.assertEqual(netconf.parse("WIFI_SSID=x").key_mgmt, "open")
        self.assertTrue(netconf.parse("wifi_ssid=x\nWIFI_HIDDEN=yes").hidden)

    def test_errors_never_contain_values(self):
        cases = ["WIFI_PASSWORD=" + DUMMY,                               # no SSID
                 "WIFI_SSID=x\nWIFI_PASSWORD=short",                     # too short
                 "WIFI_SSID=x\nWIFI_SECURITY=open\nWIFI_PASSWORD=" + DUMMY,
                 "WIFI_SSID=x\nWIFI_SECURITY=wep\nWIFI_PASSWORD=" + DUMMY,
                 "WIFI_SSID=x\nPSK=" + DUMMY,                            # unknown key
                 "WIFI_SSID=x\n" + DUMMY]                                # not KEY=VALUE
        for text in cases:
            with self.assertRaises(netconf.NetConfError) as cm:
                netconf.parse(text)
            self.assertNotIn(DUMMY, str(cm.exception))

    def test_repr_hides_password(self):
        net = netconf.parse(f"WIFI_SSID=LabNet\nWIFI_PASSWORD={DUMMY}")
        self.assertNotIn(DUMMY, repr(net))
        self.assertNotIn(DUMMY, str(net))

    def test_read_file_limits(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "network.conf"
            self.assertIsNone(netconf.read_file(f, "iso"))
            f.write_text("WIFI_SSID=x\n" + "#" * 5000)
            with self.assertRaises(netconf.NetConfError):
                netconf.read_file(f, "iso")
            f.write_bytes(b"WIFI_SSID=\xff\xfe")
            with self.assertRaises(netconf.NetConfError):
                netconf.read_file(f, "iso")


class MediaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.root = Path(self.td.name)
        self.medium = self.root / "medium"
        self.medium.mkdir()
        self.state = self.root / "nm.json"
        write_state(self.state)
        self.nm_dir = self.root / "data/network/connections"
        self.adapter = NetworkAdapter(runner=fake_runner(self.state), nm_connections_dir=self.nm_dir)

    async def test_iso_file_imported_and_joined(self):
        (self.medium / "retroboot").mkdir()
        (self.medium / "retroboot/network.conf").write_text(f"WIFI_SSID=LabNet\nWIFI_PASSWORD={DUMMY}\n")
        net = await netconf.find_on_media(self.medium, runner=self.adapter.run, mount_root=self.root)
        self.assertEqual((net.ssid, net.source), ("LabNet", "iso"))
        result = await netconf.import_network(self.adapter, net, connect_timeout=45)
        self.assertTrue(result["connected"])
        files = list(self.nm_dir.iterdir())
        self.assertEqual(len(files), 1)
        self.assertEqual(stat.S_IMODE(files[0].stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.nm_dir.stat().st_mode), 0o700)
        st = json.loads(self.state.read_text())
        self.assertEqual(st["profiles"][0]["psk"], DUMMY)
        self.assertNotIn(DUMMY, json.dumps(st["calls"]))                 # never in argv
        self.assertIn("Joined “LabNet”", netconf.describe(result, None))

    async def test_second_import_replaces_the_profile(self):
        net = netconf.parse(f"WIFI_SSID=LabNet\nWIFI_PASSWORD={DUMMY}")
        first = await netconf.import_network(self.adapter, net, connect_timeout=45)
        second = await netconf.import_network(self.adapter, net, connect_timeout=45)
        self.assertNotEqual(first["profile"], second["profile"])
        self.assertEqual(len(list(self.nm_dir.iterdir())), 1)               # hostd restarted: no duplicate
        st = json.loads(self.state.read_text())
        self.assertEqual([p["uuid"] for p in st["profiles"]], [second["profile"]])

    async def test_out_of_range_profile_is_kept(self):
        write_state(self.state, aps=[])
        net = netconf.parse(f"WIFI_SSID=LabNet\nWIFI_PASSWORD={DUMMY}")
        result = await netconf.import_network(self.adapter, net, connect_timeout=45)
        self.assertFalse(result["connected"])
        self.assertEqual(len(list(self.nm_dir.iterdir())), 1)             # joins later, where it is used
        self.assertIn("joins it when it is in range", netconf.describe(result, None))

    async def test_fat_partition_of_a_usb_stick(self):
        mounted = {}

        async def runner(argv, timeout=None, **kw):
            if argv[0] == "findmnt":
                return 0, "/dev/sdb\n", ""
            if argv[:5] == ["lsblk", "-n", "-r", "-o", "PKNAME"]:
                return 0, "\n", ""
            if argv[:5] == ["lsblk", "-n", "-r", "-o", "PATH,LABEL,FSTYPE"]:
                return 0, "/dev/sdb  \n/dev/sdb1 RETROBOOT iso9660\n/dev/sdb2 RBINSTEFI vfat\n", ""
            if argv[0] == "mount":
                self.assertIn("ro,nosuid,nodev,noexec", argv)
                mnt = Path(argv[-1])
                (mnt / "retroboot").mkdir()
                (mnt / "retroboot/network.conf").write_text("WIFI_SSID=Cafe\n")
                mounted["dev"] = argv[-2]
                return 0, "", ""
            if argv[0] == "umount":
                mnt = Path(argv[-1])
                (mnt / "retroboot/network.conf").unlink()
                (mnt / "retroboot").rmdir()
                return 0, "", ""
            return 1, "", ""
        net = await netconf.find_on_media(self.medium, runner=runner, mount_root=self.root)
        self.assertEqual((net.ssid, net.key_mgmt, net.source), ("Cafe", "open", "fat"))
        self.assertEqual(mounted["dev"], "/dev/sdb2")
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith("netconf-")], [])

    async def test_no_wifi_hardware(self):
        write_state(self.state, device=None)
        with self.assertRaises(NetworkError):
            await netconf.import_network(self.adapter, netconf.parse("WIFI_SSID=Cafe"))

    async def test_wifi_hardware_label(self):
        hw = await self.adapter.wifi_hardware()
        self.assertEqual(hw["label"], "Realtek RTL8821CE 802.11ac")
        self.assertEqual(hw["driver"], "rtw_8821ce")


class LegacyProfileTests(unittest.TestCase):
    def test_moved_once_never_replaced(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            legacy = root / "etc/NetworkManager/system-connections"
            legacy.mkdir(parents=True)
            (legacy / "retroboot-1.nmconnection").write_text("[connection]\nid=a\n")
            (legacy / "Lab.nmconnection").write_text("[connection]\nid=b\n")
            (legacy / "link.nmconnection").symlink_to(legacy / "Lab.nmconnection")
            new = root / "var/lib/retroboot/network/connections"
            new.mkdir(parents=True)
            (new / "Lab.nmconnection").write_text("[connection]\nid=newer\n")
            adapter = NetworkAdapter(enabled=False, nm_connections_dir=new)
            self.assertEqual(adapter.migrate_legacy_profiles(legacy), 1)
            self.assertEqual(stat.S_IMODE((new / "retroboot-1.nmconnection").stat().st_mode), 0o600)
            self.assertFalse((legacy / "retroboot-1.nmconnection").exists())
            self.assertEqual((new / "Lab.nmconnection").read_text(), "[connection]\nid=newer\n")
            self.assertTrue((legacy / "Lab.nmconnection").exists())
            self.assertFalse((new / "link.nmconnection").exists())
            self.assertEqual(adapter.migrate_legacy_profiles(legacy), 0)


LIST_DISKS = """#!{py}
import json
print(json.dumps([{{"path": "/dev/vda", "model": "Virtual Disk", "size_bytes": 42949672960, "removable": False,
                    "transport": "virtio", "is_boot_medium": False}}]))
"""
INSTALL = """#!{py}
import json, os, sys
argv = sys.argv[1:]
json.dump(argv, open({rec!r}, "w"))
if "--network-profiles" in argv:
    d = argv[argv.index("--network-profiles") + 1]
    json.dump({{n: oct(os.stat(os.path.join(d, n)).st_mode & 0o777) for n in os.listdir(d)}}, open({rec!r} + ".nm", "w"))
for phase in ("partition", "machine"):
    print(json.dumps({{"phase": phase, "progress": 0.5, "message": phase}}), flush=True)
"""


class InstallerWifiTests(unittest.IsolatedAsyncioTestCase):
    """The installer's Wi-Fi: hardware check entry point, on-screen join, media
    file, and the profiles handed to install-to-disk (fake nmcli, dummy values)."""

    async def asyncSetUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        helpers.make_package(self.root)
        self.cfg = helpers.make_config(self.root, mode="installer")
        paths = Paths(self.root)
        paths.ensure()
        inst = paths.installer_dir
        mdir = inst / "embedded/machine"
        mdir.mkdir(parents=True)
        with open(mdir / "disk.img", "wb") as f:
            f.truncate(1024 * 1024)
        (mdir / "machine.json").write_text(json.dumps({"schema_version": 1, "id": "mac-os-9", "name": "Mac OS 9",
                                                       "disk": "disk.img"}))
        self.record = str(self.root / "install-args.json")
        (inst / "list-disks").write_text(LIST_DISKS.format(py=sys.executable))
        (inst / "install-to-disk").write_text(INSTALL.format(py=sys.executable, rec=self.record))
        for n in ("list-disks", "install-to-disk"):
            (inst / n).chmod(0o755)
        self.state = self.root / "nm.json"
        write_state(self.state)
        self.medium = self.root / "medium"
        self.medium.mkdir()
        self.fp = FakePresenter(str(paths.presenter_sock), None)
        await self.fp.start()
        self.app = Hostd(self.cfg)
        self.app.network.enabled = True
        self.app.network.run = fake_runner(self.state)
        self.app.settings.store.load()
        helpers.install_fake_prep(self.app)
        await self.app.start()

    async def asyncTearDown(self):
        await self.app.shutdown()
        await self.fp.stop()

    async def doc(self, doc_id, contains=None, timeout=10):
        await helpers.wait_for(lambda: (self.fp.doc or {}).get("id") == doc_id and
                               (contains is None or contains in json.dumps(self.fp.doc, ensure_ascii=False)), timeout,
                               what=f"{doc_id} {contains or ''}")

    def buttons(self):
        return [b["id"] for i in self.fp.doc["items"] if i.get("type") == "buttons" for b in i["items"]]

    async def finish_install(self):
        await self.doc("installer-hardware", "1 disk available")
        self.fp.click("continue")
        await self.doc("installer-destination")
        self.fp.click("continue", {"disk": "/dev/vda"})
        await self.doc("installer-erase")
        self.fp.click("erase")
        await self.doc("installer-macs", "On this disc")
        self.fp.click("continue")
        await self.doc("installer-display")
        self.fp.click("continue")
        await self.doc("timezone")
        self.fp.click("set", {"zone": "UTC"})
        await self.doc("installer-network")
        self.fp.click("continue")
        await self.doc("installer-ready")
        self.fp.click("install")
        await self.doc("installer-done", None, 20)
        return json.loads(Path(self.record).read_text())

    def assert_no_secret_leak(self):
        for f in self.app.paths.logs.rglob("*"):
            if f.is_file():
                self.assertNotIn(DUMMY, f.read_text(errors="replace"), str(f))
        self.assertNotIn(DUMMY, json.dumps(json.loads(self.state.read_text())["calls"]))

    async def test_join_on_screen_is_carried_to_the_installed_system(self):
        await self.doc("installer-welcome")
        self.fp.click("continue")
        await self.doc("installer-hardware", "Wi-Fi: Realtek RTL8821CE 802.11ac")
        self.assertIn("Not connected (optional)", json.dumps(self.fp.doc))
        self.assertIn("wifi", self.buttons())
        self.fp.click("wifi")
        await self.doc("wifi", "LabNet")
        self.fp.change("network", "LabNet")
        await helpers.wait_for(lambda: "join" in self.buttons() and not next(
            b for i in self.fp.doc["items"] if i.get("type") == "buttons" for b in i["items"]
            if b["id"] == "join").get("disabled"), 5, what="Join enabled")
        self.fp.click("join", {"network": "LabNet"})
        await self.doc("wifi-join", "LabNet")
        self.fp.click("join", {"psk": DUMMY})
        await self.doc("message", "Connected to “LabNet”", 20)
        self.fp.click(self.buttons()[-1])
        await self.doc("wifi")
        self.fp.click("cancel")
        args = await self.finish_install()
        nm_dir = str(self.app.paths.nm_connections)
        self.assertEqual(args[-2:], ["--network-profiles", nm_dir])
        carried = json.loads(Path(self.record + ".nm").read_text())
        self.assertEqual(len(carried), 1)
        self.assertEqual(list(carried.values()), ["0o600"])
        self.assertEqual(helpers.events(self.app, "install_started")[0]["network_profiles"], 1)
        self.assert_no_secret_leak()

    async def test_media_network_file(self):
        (self.medium / "retroboot").mkdir()
        (self.medium / "retroboot/network.conf").write_text(f"WIFI_SSID=LabNet\nWIFI_PASSWORD={DUMMY}\n")
        await self.doc("installer-welcome")
        await self.app.panel.media_task                          # the start-up import (no medium there)
        await self.app.panel.import_media_network(self.medium)
        self.assertTrue(self.app.panel.st.media_network["connected"])
        self.fp.click("continue")
        await self.doc("installer-hardware", "using the network settings on the installation media")
        args = await self.finish_install()
        self.assertIn("--network-profiles", args)
        self.assertEqual(helpers.events(self.app, "installer_media_network")[0]["found_on"], "iso")
        self.assert_no_secret_leak()

    async def test_invalid_media_file_is_reported_without_values(self):
        (self.medium / "retroboot").mkdir()
        (self.medium / "retroboot/network.conf").write_text("WIFI_SSID=LabNet\nWIFI_PASSWORD=short\n")
        await self.doc("installer-welcome")
        await self.app.panel.media_task
        await self.app.panel.import_media_network(self.medium)
        self.fp.click("continue")
        await self.doc("installer-hardware", "were not used: WIFI_PASSWORD must be 8 to 63 characters")
        args = await self.finish_install()
        self.assertNotIn("--network-profiles", args)

    async def test_no_profiles_no_flag(self):
        await self.doc("installer-welcome")
        self.fp.click("continue")
        args = await self.finish_install()
        self.assertEqual(args, ["--target", "/dev/vda", "--machine-dir",
                                str(self.app.paths.installer_dir / "embedded/machine"),
                                "--seed", str(self.app.paths.run / "install-seed.json"), "--hostname", "retroboot"])


if __name__ == "__main__":
    unittest.main()
