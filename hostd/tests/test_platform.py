"""The board hostd runs on (retroboot/platform.py) and what branches on it.

Fake /proc/device-tree trees for a Raspberry Pi 4 Model B, Pi 400, CM4, Pi 5
and Pi 500 (model, compatible and the firmware's revision code as the real
boards publish them), and DMI trees for PCs.  On a Pi: Diagnostics and the
status name the board, Software Update says it isn't available on this test
build (and writes nothing), startup/shutdown deadlines still at their defaults
are stretched, and Wi-Fi settings on the FAT boot partition are imported once.
On a PC nothing changes.
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import helpers
from fakes.fake_presenter import FakePresenter
from retroboot import platform as hostplatform
from retroboot.config import Config, Paths
from retroboot.hostd import Hostd
from retroboot.update import manager as update_manager

PI5 = ("Raspberry Pi 5 Model B Rev 1.0", ["raspberrypi,5-model-b", "brcm,bcm2712"], 0xd04170)
PI4 = ("Raspberry Pi 4 Model B Rev 1.5", ["raspberrypi,4-model-b", "brcm,bcm2711"], 0xc03115)
PI400 = ("Raspberry Pi 400 Rev 1.1", ["raspberrypi,400", "brcm,bcm2711"], 0xc03131)
CM4 = ("Raspberry Pi Compute Module 4 Rev 1.1", ["raspberrypi,4-compute-module", "brcm,bcm2711"], 0xd03141)
PI500 = ("Raspberry Pi 500 Rev 1.0", ["raspberrypi,500", "brcm,bcm2712"], 0xd04190)
DUMMY = "dummy-pass-7Q"            # never a real secret


def tree(model, compatible, revision, meminfo_kb=None):
    td = tempfile.TemporaryDirectory()
    root = Path(td.name)
    helpers.make_device_tree(root, model, compatible, revision)
    (root / "proc").mkdir(exist_ok=True)
    if meminfo_kb:
        (root / "proc/meminfo").write_text(f"MemTotal:       {meminfo_kb} kB\nMemFree:  1 kB\n")
    return td, root


def dmi(**values):
    td = tempfile.TemporaryDirectory()
    root = Path(td.name)
    d = root / "sys/class/dmi/id"
    d.mkdir(parents=True)
    for k, v in values.items():
        (d / k).write_text(v + "\n")
    return td, root


class DetectTests(unittest.TestCase):
    def detect(self, td_root):
        td, root = td_root
        self.addCleanup(td.cleanup)
        return hostplatform.detect(root / "proc/device-tree", root / "sys/class/dmi/id", root / "proc",
                                   machine="aarch64")

    def test_pi5(self):
        p = self.detect(tree(*PI5))
        self.assertTrue(p.is_raspberry_pi)
        self.assertEqual((p.board, p.soc, p.ram_mb, p.model, p.pi_family), ("pi5", "BCM2712", 8192,
                                                                           "Raspberry Pi 5 Model B Rev 1.0", "pi5"))
        self.assertEqual(p.revision, "d04170")
        self.assertEqual(p.features, {"battery": False, "lid": False, "backlight": False, "power_button": True,
                                      "rtc": True, "analog_audio": False, "grub": False, "dmi": False})
        self.assertFalse(p.has_grub)
        self.assertEqual(p.timeout_scale, 2.0)

    def test_pi4_family(self):
        p = self.detect(tree(*PI4))
        self.assertEqual((p.board, p.soc, p.ram_mb, p.pi_family), ("pi4", "BCM2711", 4096, "pi4"))
        self.assertEqual((p.features["power_button"], p.features["analog_audio"], p.features["rtc"]),
                         (False, True, False))
        self.assertEqual(p.timeout_scale, 3.0)
        p400 = self.detect(tree(*PI400))
        self.assertEqual((p400.board, p400.pi_family, p400.features["analog_audio"]), ("pi400", "pi4", False))
        cm4 = self.detect(tree(*CM4))
        self.assertEqual((cm4.board, cm4.ram_mb, cm4.pi_family), ("cm4", 8192, "pi4"))
        p500 = self.detect(tree(*PI500))
        self.assertEqual((p500.board, p500.pi_family, p500.features["power_button"]), ("pi500", "pi5", True))

    def test_without_revision_code(self):
        # Older firmware or another loader: compatible and the model text are enough,
        # RAM from /proc/meminfo.
        p = self.detect(tree(PI4[0], PI4[1], None, meminfo_kb=3884000))
        self.assertEqual((p.board, p.soc, p.ram_mb, p.revision), ("pi4", "BCM2711", 3792, None))
        p = self.detect(tree("Raspberry Pi 5 Model B Rev 1.1", ["brcm,bcm2712"], None))
        self.assertEqual((p.board, p.soc, p.pi_family), ("pi5", "BCM2712", "pi5"))

    def test_pcs(self):
        p = self.detect(dmi(sys_vendor="GPD", product_name="MicroPC"))
        self.assertEqual((p.family, p.model, p.source, p.timeout_scale, p.has_grub), ("pc", "GPD MicroPC", "dmi",
                                                                                      1.0, True))
        self.assertFalse(p.is_raspberry_pi)
        p = self.detect(dmi(sys_vendor="To Be Filled By O.E.M.", product_name="Default string", board_name="N5105"))
        self.assertEqual(p.model, "N5105")
        p = self.detect(dmi(sys_vendor="LENOVO", product_name="LENOVO 82XX"))
        self.assertEqual(p.model, "LENOVO 82XX")
        p = self.detect(dmi(sys_vendor="QEMU", product_name="Standard PC (Q35 + ICH9, 2009)"))
        self.assertEqual(p.model, "QEMU Standard PC (Q35 + ICH9, 2009)")

    def test_nothing_readable(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        p = hostplatform.detect(Path(td.name) / "none", Path(td.name) / "none", Path(td.name) / "none", machine="x86_64")
        self.assertEqual((p.family, p.model, p.source, p.timeout_scale), ("pc", None, "none", 1.0))
        # Another device-tree board (not a Pi) is not mistaken for one.
        td2, root = tree("Pine64 RockPro64 v2.1", ["pine64,rockpro64-v2.1", "rockchip,rk3399"], None)
        self.addCleanup(td2.cleanup)
        p = hostplatform.detect(root / "proc/device-tree", root / "nodmi", root / "proc", machine="aarch64")
        self.assertEqual((p.family, p.model, p.timeout_scale, p.has_grub), ("other", "Pine64 RockPro64 v2.1", 1.0,
                                                                            True))
        td3, root3 = tree("linux,dummy-virt", ["linux,dummy-virt"], None)      # QEMU's arm64 virt machine
        self.addCleanup(td3.cleanup)
        p = hostplatform.detect(root3 / "proc/device-tree", root3 / "nodmi", root3 / "proc", machine="aarch64")
        self.assertEqual((p.family, p.is_raspberry_pi, p.timeout_scale), ("other", False, 1.0))
        self.assertEqual(p.model, "Virtual Arm computer")

    def test_revision_codes(self):
        self.assertEqual(hostplatform.decode_revision(0xd04170), {"board": "pi5", "soc": "BCM2712", "ram_mb": 8192,
                                                                   "rev": 0})
        self.assertEqual(hostplatform.decode_revision(0xa03111)["ram_mb"], 1024)
        self.assertEqual(hostplatform.decode_revision(0xb03115)["ram_mb"], 2048)
        self.assertEqual(hostplatform.decode_revision(0x0010), {})      # old-style code
        self.assertEqual(hostplatform.decode_revision(None), {})


class TimeoutTests(unittest.TestCase):
    def test_only_defaults_are_stretched(self):
        td, root = tree(*PI4)
        self.addCleanup(td.cleanup)
        p = hostplatform.detect(root / "proc/device-tree", root / "x", root / "proc")
        cfg = Config()
        cfg.clean_stop_timeout = 45.0              # set in hostd.json: kept
        changed = hostplatform.scale_timeouts(cfg, p)
        self.assertEqual(cfg.guest_content_timeout, 180.0)     # 60 s x 3: the first-boot pre-gray deadline
        self.assertEqual(cfg.finder_wait_timeout, 450.0)
        self.assertEqual(cfg.clean_stop_timeout, 45.0)
        self.assertNotIn("clean_stop_timeout", changed)
        self.assertEqual(cfg.frozen_stale_s, Config().frozen_stale_s)   # not a startup deadline
        self.assertEqual(changed["power_started_after_s"], (20.0, 60.0))

    def test_pc_unchanged(self):
        cfg = Config()
        self.assertEqual(hostplatform.scale_timeouts(cfg, hostplatform.Platform()), {})
        self.assertEqual(cfg, Config())


class MemoryTests(unittest.TestCase):
    """QEMU's TCG translation buffer on small Pis (its pages fill up beside the 512 MB guest)."""

    def plat(self, revision=None, meminfo_kb=None, board=PI4):
        td, root = tree(board[0], board[1], revision, meminfo_kb=meminfo_kb)
        self.addCleanup(td.cleanup)
        return hostplatform.detect(root / "proc/device-tree", root / "x", root / "proc")

    def test_tb_size_by_ram(self):
        self.assertEqual(self.plat(0xa03111).tcg_tb_size_mb, 128)        # Pi 4 1 GB
        self.assertTrue(self.plat(0xa03111).small_memory)
        self.assertEqual(self.plat(0xb03115).tcg_tb_size_mb, 256)        # Pi 4 2 GB
        self.assertFalse(self.plat(0xb03115).small_memory)
        self.assertEqual(self.plat(0xc03115).tcg_tb_size_mb, 512)        # Pi 4 4 GB
        self.assertEqual(self.plat(0xd03114).tcg_tb_size_mb, 512)        # Pi 4 8 GB
        self.assertEqual(self.plat(0xb04170, board=PI5).tcg_tb_size_mb, 256)   # Pi 5 2 GB
        # Without a revision code, from /proc/meminfo (a little under the board's size).
        self.assertEqual(self.plat(None, 1893000).tcg_tb_size_mb, 256)   # 2 GB board
        self.assertEqual(self.plat(None, 3884000).tcg_tb_size_mb, 512)   # 4 GB board
        self.assertEqual(self.plat(None, 944000).tcg_tb_size_mb, 128)    # 1 GB board
        self.assertEqual(self.plat(None, None).tcg_tb_size_mb, 512)      # unknown: as before
        self.assertEqual(hostplatform.Platform().tcg_tb_size_mb, 512)    # PCs: unchanged

    def test_argv(self):
        from retroboot.machines import LaunchOptions, MachineError, build_argv
        td, root = helpers.scratch()
        self.addCleanup(td.cleanup)
        helpers.make_package(root)
        helpers.make_machine(root)
        paths = Paths(root)
        paths.ensure()
        from retroboot import schema
        from retroboot.machines import MachineRegistry
        machine = MachineRegistry(paths).get("mac-os-9")
        settings = schema.defaults()
        display = {"mode": [1920, 1080, 60]}
        pc = build_argv(machine, settings, paths, display)
        self.assertEqual(pc[pc.index("-accel") + 1], "tcg,tb-size=512")              # PCs byte-identical
        self.assertEqual(pc, build_argv(machine, settings, paths, display, LaunchOptions(tb_size_mb=512)))
        small = build_argv(machine, settings, paths, display, LaunchOptions(tb_size_mb=256))
        self.assertEqual(small[small.index("-accel") + 1], "tcg,tb-size=256")
        self.assertEqual([a for a in small if a != "tcg,tb-size=256"], [a for a in pc if a != "tcg,tb-size=512"])
        with self.assertRaises(MachineError):
            build_argv(machine, settings, paths, display, LaunchOptions(tb_size_mb=1000))


def fake_pi_sysfs(sysfs: Path, temp_milli=62000, cur_khz=1800000, max_khz=1800000, limit_khz=None, alarm="0"):
    """The sysfs a Raspberry Pi kernel offers for Health: raspberrypi-hwmon (rpi_volt), the
    SoC thermal zone and cpu0's cpufreq. None leaves that file out."""
    hw = sysfs / "class/hwmon/hwmon0"
    hw.mkdir(parents=True, exist_ok=True)
    (sysfs / "class/hwmon/hwmon1").mkdir(exist_ok=True)
    (sysfs / "class/hwmon/hwmon1/name").write_text("cpu_thermal\n")          # another hwmon device first
    (hw / "name").write_text("rpi_volt\n")
    tz = sysfs / "class/thermal/thermal_zone0"
    tz.mkdir(parents=True, exist_ok=True)
    cpu = sysfs / "devices/system/cpu/cpu0/cpufreq"
    cpu.mkdir(parents=True, exist_ok=True)
    for path, value in ((hw / "in0_lcrit_alarm", alarm), (tz / "temp", temp_milli), (cpu / "scaling_cur_freq", cur_khz),
                        (cpu / "cpuinfo_max_freq", max_khz), (cpu / "scaling_max_freq", limit_khz or max_khz)):
        if value is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(f"{value}\n")


class HealthTests(unittest.TestCase):
    """Diagnostics' "Power and temperature" from sysfs (raspberrypi-hwmon, thermal, cpufreq):
    the firmware's get_throttled file is gone from Raspberry Pi kernels 6.18."""

    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.sysfs = Path(td.name)
        self.h = hostplatform.Health(self.sysfs)

    def test_normal(self):
        fake_pi_sysfs(self.sysfs)
        d = self.h.sample()
        self.assertEqual(d["text"], "62 °C, 1.8 GHz of 1.8 GHz, power OK")
        self.assertEqual((d["under_voltage"], d["under_voltage_seen"], d["temp_c"], d["cpu_mhz"], d["max_mhz"],
                          d["capped"]), (False, False, 62.0, 1800, 1800, False))
        fake_pi_sysfs(self.sysfs, temp_milli=48312, cur_khz=600000, max_khz=2400000)   # Pi 5 idling: not slowed
        self.assertEqual(self.h.sample()["text"], "48 °C, 0.6 GHz of 2.4 GHz, power OK")

    def test_under_voltage_now_then_since_start(self):
        fake_pi_sysfs(self.sysfs, alarm="1", cur_khz=1500000)
        d = self.h.sample()
        self.assertTrue(d["under_voltage"] and d["under_voltage_seen"] and d["capped"])
        self.assertEqual(d["text"], "Under-voltage detected (now), CPU slowed to 1.5 GHz (temperature 62 °C)")
        fake_pi_sysfs(self.sysfs, alarm="0")           # the driver cleared the alarm 2 s later
        d = self.h.sample()
        self.assertEqual((d["under_voltage"], d["under_voltage_seen"]), (False, True))
        self.assertEqual(d["text"], "Under-voltage detected (since start), 62 °C, 1.8 GHz of 1.8 GHz")

    def test_hot_and_capped(self):
        fake_pi_sysfs(self.sysfs, temp_milli=82450, cur_khz=1000000)
        d = self.h.sample()
        self.assertTrue(d["capped"])
        self.assertEqual(d["text"], "CPU slowed to 1.0 GHz (temperature 82 °C), power OK")
        fake_pi_sysfs(self.sysfs, temp_milli=70000, cur_khz=1200000, limit_khz=1200000)   # a lowered limit
        self.assertEqual(self.h.sample()["text"], "CPU slowed to 1.2 GHz (temperature 70 °C), power OK")
        fake_pi_sysfs(self.sysfs, temp_milli=82450, cur_khz=1800000)                       # hot, full speed
        self.assertEqual(self.h.sample()["text"], "82 °C, 1.8 GHz of 1.8 GHz, power OK")

    def test_files_missing(self):
        d = self.h.sample()                            # nothing at all (or a PC's sysfs)
        self.assertIsNone(d["text"])
        self.assertEqual((d["under_voltage"], d["temp_c"], d["cpu_mhz"], d["capped"]), (None, None, None, False))
        fake_pi_sysfs(self.sysfs, alarm=None)          # no raspberrypi-hwmon: no power verdict
        self.assertEqual(self.h.sample()["text"], "62 °C, 1.8 GHz of 1.8 GHz")
        fake_pi_sysfs(self.sysfs, temp_milli=None, cur_khz=None)
        self.assertEqual(self.h.sample()["text"], "power OK")
        (self.sysfs / "class/hwmon/hwmon0/in0_lcrit_alarm").write_text("junk\n")
        self.assertIsNone(self.h.sample()["text"])


def fake_runner(state: Path):
    async def run(argv, timeout=None, **kw):
        if argv[0] == "nmcli":
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(helpers.FAKES / "fake_nmcli.py"), *argv[1:], stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=dict(os.environ, FAKE_NMCLI_STATE=str(state)))
            out, err = await proc.communicate()
            return proc.returncode, out.decode(), err.decode()
        if argv[0] == "ip":
            return 0, "[]", ""
        return 0, "", ""
    return run


class PiHostdTests(unittest.IsolatedAsyncioTestCase):
    """hostd on a fake Raspberry Pi 5 (fake presenter, no emulator started)."""

    board = PI5

    async def asyncSetUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        helpers.make_package(self.root)
        helpers.make_device_tree(self.root, *self.board)
        self.boot = self.root / "boot/firmware/retroboot"
        self.boot.mkdir(parents=True)
        self.cfg = helpers.make_config(self.root, boot_network_conf=str(self.boot / "network.conf"))
        paths = Paths(self.root)
        paths.ensure()
        self.fp = FakePresenter(str(paths.presenter_sock), None)
        await self.fp.start()
        self.app = Hostd(self.cfg)
        self.app.settings.store.load()
        helpers.install_fake_prep(self.app)
        await self.app.start()

    async def asyncTearDown(self):
        await self.app.shutdown()
        await self.fp.stop()

    async def test_status_diagnostics_and_update(self):
        self.assertTrue(self.app.platform.is_raspberry_pi)
        st = await self.app.status()
        self.assertEqual(st["host"]["model"], "Raspberry Pi 5 Model B Rev 1.0")
        self.assertEqual(st["host"]["platform"]["board"], "pi5")
        self.assertEqual(st["versions"]["Computer"], "Raspberry Pi 5 Model B Rev 1.0, 8 GB")
        self.assertIsNone(st["host"]["throttled"]["text"])     # no Pi sysfs in this tree: no row
        self.assertNotIn("Power and temperature", st["versions"])
        fake_pi_sysfs(self.root / "sys", temp_milli=82000, cur_khz=1000000, alarm="1")
        st = await self.app.status()
        self.assertEqual(st["versions"]["Power and temperature"],
                         "Under-voltage detected (now), CPU slowed to 1.0 GHz (temperature 82 °C)")
        self.assertTrue(st["host"]["throttled"]["under_voltage_seen"])
        fake_pi_sysfs(self.root / "sys")
        from retroboot.panel.controller import Diagnostics
        self.assertIn("Power and temperature: Under-voltage detected (since start), 62 °C",
                      json.dumps(Diagnostics(self.app.panel).doc(), ensure_ascii=False))
        upd = self.app.updates.public()
        self.assertFalse(upd["available"])
        expected_reason = "This RetroBoot wasn't installed with room for updates."
        self.assertEqual(upd["reason"], expected_reason)
        self.assertFalse(upd["can_install"])
        self.assertEqual(helpers.events(self.app, "update_unavailable")[0]["reason"], expected_reason)
        self.assertTrue(helpers.events(self.app, "platform"))
        # The classic panel's Diagnostics names the board.
        from retroboot.panel.controller import Diagnostics
        doc = Diagnostics(self.app.panel).doc()
        self.assertIn("Computer: Raspberry Pi 5 Model B Rev 1.0, 8 GB", json.dumps(doc))
        # Deadlines make_config set stay; the ones still at their defaults are stretched.
        self.assertEqual(self.app.cfg.guest_content_timeout, 6.0)
        self.assertEqual(self.app.cfg.power_started_after_s, 40.0)
        self.assertEqual(self.app.startup.launch_estimate(None), hostplatform.LAUNCH_MS["pi5"])

    async def test_boot_partition_wifi_is_imported_once(self):
        state = self.root / "nm.json"
        state.write_text(json.dumps({"device": "wlan0", "radio": True, "profiles": [], "active": None,
                                     "aps": [{"ssid": "LabNet", "bssid": "aa:bb:cc:00:00:01", "chan": 36,
                                              "signal": 80, "security": "WPA2", "password": DUMMY}],
                                     "hardware": {"vendor": "Broadcom", "product": "BCM43455", "driver": "brcmfmac"}}))
        self.app.network.enabled = True
        self.app.network.run = fake_runner(state)
        conf = self.boot / "network.conf"
        self.assertIsNone(await self.app._import_boot_network(wait_s=0))     # no file: nothing
        conf.write_text(f"WIFI_SSID=LabNet\nWIFI_PASSWORD={DUMMY}\n")
        result = await self.app._import_boot_network(wait_s=0)
        self.assertTrue(result["connected"])
        self.assertEqual(len(json.loads(state.read_text())["profiles"]), 1)
        self.assertIsNone(await self.app._import_boot_network(wait_s=0))     # same file: imported already
        conf.write_text(f"WIFI_SSID=LabNet\nWIFI_PASSWORD={DUMMY}\nWIFI_HIDDEN=yes\n")
        self.assertIsNotNone(await self.app._import_boot_network(wait_s=0))  # edited: imported again
        self.assertEqual(len(json.loads(state.read_text())["profiles"]), 1)  # replaced, not duplicated
        conf.write_text("WIFI_SSID=LabNet\nWIFI_PASSWORD=short\n")
        self.assertIsNone(await self.app._import_boot_network(wait_s=0))
        self.assertEqual(helpers.events(self.app, "boot_network_invalid")[-1]["reason"],
                         "WIFI_PASSWORD must be 8 to 63 characters")
        marker = self.app.paths.state / "boot-network.json"
        self.assertEqual(oct(marker.stat().st_mode & 0o777), "0o600")
        for f in list(self.app.paths.logs.rglob("*")) + [marker]:
            if f.is_file():
                self.assertNotIn(DUMMY, f.read_text(errors="replace"), str(f))


class Pi4HostdTests(PiHostdTests):
    board = PI4

    async def test_status_diagnostics_and_update(self):
        self.assertEqual(self.app.platform.board, "pi4")
        st = await self.app.status()
        self.assertEqual(st["versions"]["Computer"], "Raspberry Pi 4 Model B Rev 1.5, 4 GB")
        self.assertEqual(self.app.cfg.power_started_after_s, 60.0)
        self.assertFalse(self.app.updates.public()["available"])

    async def test_boot_partition_wifi_is_imported_once(self):
        pass


class PcHostdTests(unittest.IsolatedAsyncioTestCase):
    """The same hostd on a PC (DMI, no device tree): nothing Pi-specific happens."""

    async def asyncSetUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        helpers.make_package(self.root)
        d = self.root / "sys/class/dmi/id"
        d.mkdir(parents=True)
        (d / "sys_vendor").write_text("GPD\n")
        (d / "product_name").write_text("MicroPC\n")
        conf = self.root / "boot/firmware/retroboot/network.conf"
        conf.parent.mkdir(parents=True)
        conf.write_text(f"WIFI_SSID=LabNet\nWIFI_PASSWORD={DUMMY}\n")
        self.cfg = helpers.make_config(self.root, boot_network_conf=str(conf))
        paths = Paths(self.root)
        paths.ensure()
        self.fp = FakePresenter(str(paths.presenter_sock), None)
        await self.fp.start()
        self.app = Hostd(self.cfg)
        self.app.settings.store.load()
        helpers.install_fake_prep(self.app)
        await self.app.start()

    async def asyncTearDown(self):
        await self.app.shutdown()
        await self.fp.stop()

    async def test_pc(self):
        self.assertFalse(self.app.platform.is_raspberry_pi)
        self.assertEqual(self.app.platform_scaled, {})
        self.assertIsNone(self.app._boot_network_task)
        st = await self.app.status()
        self.assertEqual(st["versions"]["Computer"], "GPD MicroPC")
        self.assertNotIn("throttled", st["host"])
        self.assertNotEqual(self.app.updates.public().get("reason"), update_manager.UNAVAILABLE_PI)
        self.assertFalse(helpers.events(self.app, "platform"))


if __name__ == "__main__":
    unittest.main()


class PiLaunchSoundTests(unittest.IsolatedAsyncioTestCase):
    """A launch on a fake Pi 4: QEMU gets the chosen ALSA PCM, and the volume control
    follows the card when the launch's choice differs from hostd's first one (a TV on
    HDMI at start, the headphone jack once the presenter reported a monitor without
    speakers)."""

    board = PI4

    async def asyncSetUp(self):
        from retroboot import schema
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        self.pkg = helpers.make_package(self.root)
        helpers.fake_qemu_conf(self.pkg, modes=["normal"], boot_s=0.4, finder_s=0.9)
        helpers.make_machine(self.root)
        helpers.make_device_tree(self.root, *self.board)
        self.cfg = helpers.make_config(self.root)
        paths = Paths(self.root)
        paths.ensure()
        self.fp = FakePresenter(str(paths.presenter_sock), str(paths.display_sock))
        await self.fp.start()
        self.app = Hostd(self.cfg)
        self.app.settings.store.load()
        self.app.settings.set_desired_and_applied({"machine.active": "mac-os-9"}, schema.ValidationContext(
            machines={"mac-os-9"}, check_capabilities=False))
        helpers.install_fake_prep(self.app)
        choices = [{"kind": "hdmi", "vc4": True, "card": 1, "card_id": "vc4hdmi0", "device": 0,
                    "connector": "HDMI-A-1", "alsa": "default:CARD=vc4hdmi0"},
                   {"kind": "analog", "card": 0, "card_id": "Headphones", "device": 0,
                    "alsa": "plughw:CARD=Headphones,DEV=0"}]
        self.app.choose_audio_output = lambda: choices[0] if not self.probes else choices[1]
        self.app.audio_capable = lambda: True
        self.probes = []

        async def probe(card=None, control=None, detect_switch=False):
            self.probes.append(card)
            self.app.mixer.card = card
            self.app.mixer.available = False
            return False
        self.app.mixer.probe = probe
        await self.app.start()

    async def asyncTearDown(self):
        await self.app.shutdown()
        await self.fp.stop()

    async def test_pcm_and_mixer_follow_the_launch(self):
        await helpers.wait_for(lambda: helpers.events(self.app, "audio_output"), 15, what="audio_output")
        ev = helpers.events(self.app, "audio_output")[0]
        self.assertEqual((ev["kind"], ev["card"]), ("analog", "Headphones"))
        self.assertEqual(self.probes, ["vc4hdmi0", "Headphones"])
        await helpers.wait_for(lambda: self.app.paths.argv_json.exists(), 15, what="argv.json")
        argv = json.loads(self.app.paths.argv_json.read_text())["argv"]
        self.assertEqual(argv[argv.index("-audiodev") + 1], "alsa,id=snd0,out.dev=plughw:CARD=Headphones,,DEV=0")


class SmallPiLaunchTests(PiLaunchSoundTests):
    """A 1 GB Pi 4: QEMU gets a 128 MB translation buffer and Diagnostics says the board is untested."""
    board = ("Raspberry Pi 4 Model B Rev 1.1", ["raspberrypi,4-model-b", "brcm,bcm2711"], 0xa03111)

    async def test_pcm_and_mixer_follow_the_launch(self):
        await helpers.wait_for(lambda: self.app.paths.argv_json.exists(), 15, what="argv.json")
        argv = json.loads(self.app.paths.argv_json.read_text())["argv"]
        self.assertEqual(argv[argv.index("-accel") + 1], "tcg,tb-size=128")
        st = await self.app.status()
        self.assertEqual(st["versions"]["Computer"], "Raspberry Pi 4 Model B Rev 1.1, 1 GB")
        self.assertIn("untested", st["versions"]["Memory"])
        self.assertEqual(st["host"]["platform"]["tcg_tb_size_mb"], 128)
