"""The Planet Computers Gemini PDA profile (platform.py, cpuplace.py, audio.py, speakers.py).

Fake /proc/device-tree, /proc and /sys trees shaped like the gemini-nixos 6.6 kernel's
(mt6797-gemini-pda.dts: compatible "planet,gemini-pda", "mediatek,mt6797"; the A72
cluster cpu8-9 online late; cpufreq policies 0/4/8; the soc-thermal zone; the
mt6797-mt6351 ASoC card; the bq25890 charger and mt6351 gauge).  PCs and Raspberry
Pis must not change: their profile is empty, no placement runs, argv is unchanged.
"""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

import helpers
from fakes.fake_presenter import FakePresenter
from retroboot import audio, cpuplace, formfactor, laptop, platform as hostplatform, speakers
from retroboot.config import Config, Paths
from retroboot.hostd import Hostd
from retroboot.update import manager as update_manager

GEMINI = ("Planet Computers Gemini PDA", ["planet,gemini-pda", "mediatek,mt6797"], None)
PI4 = ("Raspberry Pi 4 Model B Rev 1.5", ["raspberrypi,4-model-b", "brcm,bcm2711"], 0xc03115)


def gemini_tree(root: Path, meminfo_kb: int = 3889000) -> None:
    helpers.make_device_tree(root, *GEMINI)
    (root / "proc").mkdir(exist_ok=True)
    (root / "proc/meminfo").write_text(f"MemTotal:       {meminfo_kb} kB\n")


# The policy's governor before hostd (the profile's is schedutil): restores are visible.
KERNEL_GOV = "powersave"


def fake_cpu_sysfs(sysfs: Path, online: str = "0-9", a72_policy: bool = True, governor: str = KERNEL_GOV,
                   wq: str = "3ff") -> None:
    cpu = sysfs / "devices/system/cpu"
    cpu.mkdir(parents=True, exist_ok=True)
    (cpu / "online").write_text(online + "\n")
    (cpu / "possible").write_text("0-9\n")
    for n, lo, hi in ((0, 624000, 1118000), (4, 650000, 1352000), (8, 845000, 1495000)):
        pol = cpu / "cpufreq" / f"policy{n}"
        if n == 8 and not a72_policy:
            continue
        pol.mkdir(parents=True, exist_ok=True)
        for f, v in (("scaling_governor", governor), ("scaling_available_governors", "schedutil performance powersave"),
                     ("scaling_min_freq", lo), ("cpuinfo_min_freq", lo), ("cpuinfo_max_freq", hi),
                     ("scaling_max_freq", hi), ("scaling_cur_freq", lo)):
            (pol / f).write_text(f"{v}\n")
        for c in range(n, n + (2 if n == 8 else 4)):
            link = cpu / f"cpu{c}" / "cpufreq"
            link.parent.mkdir(parents=True, exist_ok=True)
            if not link.exists():
                os.symlink(pol, link)
    w = sysfs / "devices/virtual/workqueue"
    w.mkdir(parents=True, exist_ok=True)
    (w / "cpumask").write_text(wq + "\n")


def fake_proc_threads(proc: Path, pid: int, threads: list[tuple[int, str, int]], ppid: int = 1,
                      parent_comm: str = "python3") -> None:
    for tid, comm, ticks in threads:
        d = proc / str(pid) / "task" / str(tid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "comm").write_text(comm + "\n")
        (d / "stat").write_text(f"{tid} ({comm}) S {ppid} 1 1 0 -1 0 0 0 0 0 {ticks} 0 0 0 20 0 1 0\n")
    (proc / str(pid) / "stat").write_text(f"{pid} (qemu-system-ppc) S {ppid} 1 1 0 -1 0 0 0 0 0 0 0 0 0\n")
    (proc / str(pid) / "comm").write_text("qemu-system-ppc\n")
    (proc / str(ppid)).mkdir(parents=True, exist_ok=True)
    (proc / str(ppid) / "comm").write_text(parent_comm + "\n")
    (proc / str(ppid) / "task" / str(ppid)).mkdir(parents=True, exist_ok=True)
    (proc / str(ppid) / "task" / str(ppid) / "comm").write_text(parent_comm + "\n")


class Affinity:
    """sched_setaffinity / sched_getaffinity stand-ins: the kernel's rules (a mask with no
    active CPU is refused; reading back shows only the online CPUs of the mask)."""

    def __init__(self, sysfs: Path):
        self.sysfs = sysfs
        self.masks: dict[int, set] = {}
        self.calls: list[tuple[int, tuple]] = []
        self.fail: set[int] = set()

    def online(self):
        return cpuplace.parse_cpulist((self.sysfs / "devices/system/cpu/online").read_text())

    def set(self, tid, cpus):
        if tid in self.fail:
            raise PermissionError(1, "Operation not permitted")
        if not set(cpus) & self.online():
            raise OSError(22, "Invalid argument")
        self.calls.append((tid, tuple(sorted(cpus))))
        self.masks[tid] = set(cpus)

    def get(self, tid):
        return self.masks.get(tid, set(range(10))) & self.online()


class DetectTests(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)

    def detect(self):
        return hostplatform.detect(self.root / "proc/device-tree", self.root / "sys/class/dmi/id", self.root / "proc",
                                   machine="aarch64")

    def test_gemini(self):
        gemini_tree(self.root)
        p = self.detect()
        self.assertTrue(p.is_gemini)
        self.assertFalse(p.is_raspberry_pi)
        self.assertEqual((p.family, p.board, p.soc, p.model, p.ram_mb, p.source),
                         ("gemini", "gemini-pda", "MT6797", "Planet Computers Gemini PDA", 3797, "device-tree"))
        self.assertEqual(p.timeout_scale, 3.5)
        self.assertEqual(p.launch_ms_default, 87500.0)
        self.assertFalse(p.has_grub)
        self.assertEqual(p.tcg_tb_size_mb, 512)          # 3.8 GB: the full buffer beside the 512 MB guest
        self.assertTrue(p.features["power_button"] and p.features["lid"] and p.features["speaker_switch"])
        self.assertEqual(p.features["keyboard"], "gemini")
        self.assertEqual(p.profile["cpu"]["vcpu"], "8-9")
        self.assertEqual(p.setting_defaults, {"input.recovery_chord": "ctrl+alt+backspace"})
        self.assertEqual(p.public()["tcg_tb_size_mb"], 512)
        self.assertIsNotNone(hostplatform.health_for(p, self.root / "sys"))
        # the emulator's host-stream pre-roll covers the screamer's 100 ms interrupt hold
        self.assertEqual(audio.screamer_options(p.profile.get("screamer")),
                         {"host-lead-ms": 120, "host-lead-max-ms": 160, "host-lead-strict": True})

    def test_pcs_and_pis_have_no_profile(self):
        helpers.make_device_tree(self.root, *PI4)
        pi = self.detect()
        self.assertEqual((pi.profile, pi.setting_defaults, pi.timeout_scale), ({}, {}, 3.0))
        self.assertFalse(pi.is_gemini)
        pc = hostplatform.Platform()
        self.assertEqual((pc.profile, pc.setting_defaults, pc.timeout_scale, pc.has_grub), ({}, {}, 1.0, True))
        self.assertEqual(audio.screamer_options(pi.profile.get("screamer")), {})     # QEMU's 40/120 ms
        self.assertEqual(audio.screamer_options(pc.profile.get("screamer")), {})
        self.assertIsNone(hostplatform.health_for(pc))
        # "planet,gemini-pda" must be in compatible; a model text alone is "other"
        td2 = tempfile.TemporaryDirectory()
        self.addCleanup(td2.cleanup)
        r2 = Path(td2.name)
        helpers.make_device_tree(r2, "Planet Computers Gemini PDA", ["mediatek,mt6797-evb"])
        p = hostplatform.detect(r2 / "proc/device-tree", r2 / "nodmi", r2 / "proc", machine="aarch64")
        self.assertEqual(p.family, "other")

    def test_deadlines_stretched(self):
        gemini_tree(self.root)
        cfg = Config()
        changed = hostplatform.scale_timeouts(cfg, self.detect())
        self.assertEqual(changed["guest_content_timeout"][1], round(changed["guest_content_timeout"][0] * 3.5, 1))
        self.assertEqual(cfg.clean_stop_timeout, round(Config().clean_stop_timeout * 3.5, 1))

    def test_health(self):
        sysfs = self.root / "sys"
        fake_cpu_sysfs(sysfs)
        tz = sysfs / "class/thermal"
        for i, (typ, milli) in enumerate((("bq25890-charger", 37000), ("soc-thermal", 51500))):
            (tz / f"thermal_zone{i}").mkdir(parents=True)
            (tz / f"thermal_zone{i}/type").write_text(typ + "\n")
            (tz / f"thermal_zone{i}/temp").write_text(f"{milli}\n")
        (sysfs / "devices/system/cpu/cpufreq/policy8/scaling_cur_freq").write_text("1495000\n")
        (sysfs / "devices/system/cpu/cpufreq/policy8/scaling_max_freq").write_text("1495000\n")
        gemini_tree(self.root)
        h = hostplatform.health_for(self.detect(), sysfs)
        d = h.sample()
        self.assertEqual(d["text"], "52 °C, 1.5 GHz of 1.5 GHz")       # the SoC zone, the A72s; no power verdict
        self.assertIsNone(d["under_voltage"])
        # A72s offline: their cpufreq directory is gone; the A53 L cluster is shown
        for c in (8, 9):
            (sysfs / f"devices/system/cpu/cpu{c}/cpufreq").unlink()
        (sysfs / "devices/system/cpu/cpufreq/policy4/scaling_max_freq").write_text("1352000\n")
        (sysfs / "devices/system/cpu/cpufreq/policy4/scaling_cur_freq").write_text("1352000\n")
        self.assertEqual(h.sample()["text"], "52 °C, 1.4 GHz of 1.4 GHz")


class CpuListTests(unittest.TestCase):
    def test_lists_and_masks(self):
        self.assertEqual(cpuplace.parse_cpulist("0-3,8-9"), {0, 1, 2, 3, 8, 9})
        self.assertEqual(cpuplace.parse_cpulist(" 4 , x, 7-5, 6"), {4, 6})
        self.assertEqual(cpuplace.parse_cpulist(""), set())
        self.assertEqual(cpuplace.format_cpulist({0, 1, 2, 3, 8, 9}), "0-3,8-9")
        self.assertEqual(cpuplace.format_cpulist({4}), "4")
        self.assertEqual(cpuplace.cpumask_hex(range(8)), "ff")
        self.assertEqual(cpuplace.cpumask_hex({8, 9}), "300")
        self.assertEqual(cpuplace.cpumask_hex({0, 32}), "1,00000001")
        self.assertEqual(cpuplace.parse_cpumask("00000300"), {8, 9})
        self.assertEqual(cpuplace.parse_cpumask("1,00000001"), {0, 32})
        self.assertEqual(cpuplace.parse_cpumask("junk"), set())

    def test_resolve(self):
        gem = hostplatform.Platform(family="gemini", features=hostplatform.gemini_features())
        pc = hostplatform.Platform()
        self.assertFalse(cpuplace.resolve(Config(), pc).enabled)              # PCs: nothing
        p = cpuplace.resolve(Config(), gem)
        self.assertEqual((p.enabled, p.source, p.vcpu, p.emulator, p.host, p.workqueue, p.governor, p.min_mhz,
                          p.max_mhz),
                         (True, "profile", {8, 9}, {4, 5, 6, 7}, {4, 5, 6, 7}, set(range(8)), "schedutil", 0, 3000))
        cfg = Config()
        cfg.update({"cpu_vcpu": "9", "cpu_governor": "none", "cpu_workqueue": "off", "cpu_min_mhz": 1200})
        p = cpuplace.resolve(cfg, gem)
        self.assertEqual((p.vcpu, p.governor, p.workqueue, p.min_mhz), ({9}, None, None, 1200))
        cfg = Config()
        cfg.update({"cpu_placement": "off"})
        self.assertFalse(cpuplace.resolve(cfg, gem).enabled)
        cfg = Config()
        cfg.update({"cpu_placement": "on", "cpu_vcpu": "2-3", "cpu_emulator": "0-1"})
        p = cpuplace.resolve(cfg, pc)                                           # lab: any board
        self.assertEqual((p.enabled, p.source, p.vcpu, p.emulator, p.governor), (True, "config", {2, 3}, {0, 1}, None))
        cfg = Config()
        cfg.update({"cpu_governor": "bad governor!"})
        self.assertIsNone(cpuplace.resolve(cfg, gem).governor)


class PlacementTests(unittest.TestCase):
    QEMU = 4242
    THREADS = [(4242, "qemu-system-ppc", 300), (4243, "call_rcu", 0), (4244, "CPU 0/TCG", 9000),
               (4245, "worker", 3)]

    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)
        self.sysfs, self.proc = self.root / "sys", self.root / "proc"
        fake_cpu_sysfs(self.sysfs)
        fake_proc_threads(self.proc, self.QEMU, self.THREADS, ppid=4200)
        # rb-presenter (two threads) and hostd itself (one)
        fake_proc_threads(self.proc, 300, [(300, "rb-presenter", 50), (301, "rb-presenter", 1)])
        (self.proc / "300/comm").write_text("rb-presenter\n")
        fake_proc_threads(self.proc, 200, [(200, "python3", 10)])
        self.aff = Affinity(self.sysfs)
        self.events = []
        lifecycle = type("L", (), {"event": lambda _s, name, **kw: self.events.append((name, kw))})()
        gem = hostplatform.Platform(family="gemini", features=hostplatform.gemini_features())
        self.cp = cpuplace.CpuPlacement(cpuplace.resolve(Config(), gem), sysfs=self.sysfs, proc=self.proc,
                                        run_file=self.root / "run/cpuplace.json", lifecycle=lifecycle,
                                        setaffinity=self.aff.set, getaffinity=self.aff.get, self_pid=200)

    def pol(self, name):
        return (self.sysfs / "devices/system/cpu/cpufreq/policy8" / name).read_text().strip()

    def test_a72_online(self):
        res = self.cp.apply_once(self.QEMU)
        self.assertEqual(res["vcpu_threads"], ["CPU 0/TCG"])
        self.assertEqual((res["vcpu_by"], res["vcpu_cpus"], res["vcpu_on_fast_cores"]), ("name", "8-9", True))
        self.assertEqual(self.aff.masks[4244], {8, 9})
        for tid in (4242, 4243, 4245):
            self.assertEqual(self.aff.masks[tid], {4, 5, 6, 7})
        for tid in (300, 301, 200, 4200):          # presenter, hostd, guest-launcher
            self.assertEqual(self.aff.masks[tid], {4, 5, 6, 7})
        self.assertEqual(self.pol("scaling_governor"), "schedutil")
        self.assertEqual((self.sysfs / "devices/virtual/workqueue/cpumask").read_text(), "ff")
        self.assertEqual(res["policy"], "policy8")
        # a second pass changes nothing
        n = len(self.aff.calls)
        self.cp.apply_once(self.QEMU)
        self.assertEqual(len(self.aff.calls), n)
        # stop: everything back as it was
        restored = self.cp.restore()
        self.assertEqual(self.pol("scaling_governor"), KERNEL_GOV)
        self.assertEqual((self.sysfs / "devices/virtual/workqueue/cpumask").read_text(), "3ff")
        self.assertEqual(len(restored), 2)
        self.assertFalse((self.root / "run/cpuplace.json").exists())

    def test_a72_late(self):
        # maxcpus=8: cpu8/9 not online yet and no policy8
        for c in (8, 9):
            (self.sysfs / f"devices/system/cpu/cpu{c}/cpufreq").unlink()
        (self.sysfs / "devices/system/cpu/online").write_text("0-7\n")
        res = self.cp.apply_once(self.QEMU)
        self.assertFalse(res["vcpu_on_fast_cores"])
        self.assertEqual(self.aff.masks[4244], {4, 5, 6, 7, 8, 9})    # on the A53s for now, A72s allowed
        self.assertIsNone(res["policy"])
        # mt6797-cl2-power brings them up; the udev rule moves the workqueues onto them
        (self.sysfs / "devices/system/cpu/online").write_text("0-9\n")
        for c in (8, 9):
            os.symlink(self.sysfs / "devices/system/cpu/cpufreq/policy8", self.sysfs / f"devices/system/cpu/cpu{c}/cpufreq")
        (self.sysfs / "devices/virtual/workqueue/cpumask").write_text("300\n")
        res = self.cp.apply_once(self.QEMU)
        self.assertTrue(res["vcpu_on_fast_cores"])
        self.assertEqual(self.aff.masks[4244], {8, 9})
        self.assertEqual(self.pol("scaling_governor"), "schedutil")
        self.assertEqual((self.sysfs / "devices/virtual/workqueue/cpumask").read_text(), "ff")
        self.assertIn("cpu_online_changed", [e[0] for e in self.events])
        # the cluster goes away again (the kernel resets the thread's mask)
        (self.sysfs / "devices/system/cpu/online").write_text("0-7\n")
        self.aff.masks[4244] = set(range(8))
        self.cp.apply_once(self.QEMU)
        self.assertEqual(self.aff.masks[4244], {4, 5, 6, 7, 8, 9})
        # the original workqueue mask is the one before any change (3ff), not the udev rule's
        self.cp.restore()
        self.assertEqual((self.sysfs / "devices/virtual/workqueue/cpumask").read_text(), "3ff")

    def test_without_thread_names(self):
        threads = [(4242, "qemu-system-ppc", 300), (4243, "qemu-system-ppc", 0), (4244, "qemu-system-ppc", 9000)]
        fake_proc_threads(self.proc, 5000, [(5000 + i, c, t) for i, (_, c, t) in enumerate(threads)], ppid=4200)
        res = self.cp.apply_once(5000)
        self.assertEqual((res["vcpu_by"], self.aff.masks[5002]), ("busiest", {8, 9}))
        fake_proc_threads(self.proc, 6000, [(6000, "qemu-system-ppc", 5), (6001, "qemu-system-ppc", 3)], ppid=4200)
        res = self.cp.apply_once(6000)
        self.assertIsNone(res["vcpu_by"])         # nothing clearly busy yet: no vCPU pinning
        self.assertEqual(self.aff.masks[6001], {4, 5, 6, 7})

    def test_failures_never_raise(self):
        self.aff.fail = {4244}
        (self.sysfs / "devices/system/cpu/cpufreq/policy8/scaling_governor").chmod(0o444)
        res = self.cp.apply_once(self.QEMU)
        self.assertTrue(res["errors"])
        self.assertEqual(self.aff.masks[4243], {4, 5, 6, 7})        # the rest still placed
        self.assertIsNotNone(self.cp.apply_once(None))              # no QEMU pid: host side only
        self.assertEqual(self.cp.apply_once(99999)["threads"], 0)   # QEMU gone

    def _a72dvfs(self, top="2587000", cap="1495000"):
        """The a72dvfs kernel: cpuinfo_max_freq is the speed bin's top, scaling_max_freq the boot cap."""
        (self.sysfs / "devices/system/cpu/cpufreq/policy8/cpuinfo_max_freq").write_text(top + "\n")
        (self.sysfs / "devices/system/cpu/cpufreq/policy8/scaling_max_freq").write_text(cap + "\n")

    def test_max_freq_lifts_the_boot_cap_while_mac_os_runs(self):
        self._a72dvfs()
        self.assertEqual(self.cp.plan.max_mhz, 3000)                 # the profile: the bin's top
        res = self.cp.apply_once(self.QEMU)
        self.assertEqual(self.pol("scaling_max_freq"), "2587000")    # clamped to cpuinfo_max_freq
        self.assertEqual(res["max_khz"], "2587000")
        self.cp.apply_once(self.QEMU)                                # unchanged: nothing written again
        self.cp.restore()
        self.assertEqual(self.pol("scaling_max_freq"), "1495000")    # the boot cap is back

    def test_max_freq_survives_a_hostd_restart(self):
        self._a72dvfs()
        self.cp.apply_once(self.QEMU)
        gem = hostplatform.Platform(family="gemini", features=hostplatform.gemini_features())
        again = cpuplace.CpuPlacement(cpuplace.resolve(Config(), gem), sysfs=self.sysfs, proc=self.proc,
                                      run_file=self.root / "run/cpuplace.json",
                                      setaffinity=self.aff.set, getaffinity=self.aff.get, self_pid=200)
        again.load_saved()
        again.apply_once(self.QEMU)               # finds 2587000 already set: keeps the saved 1495000
        again.restore()
        self.assertEqual(self.pol("scaling_max_freq"), "1495000")

    def test_guest_sleep_gives_the_policy_back(self):
        """Mac OS asleep (the lid): the A72 policy gets the kernel's governor and boot cap
        back and the polls leave it so; awake, the plan again.  Placement stays."""
        self._a72dvfs()

        async def go():
            self.cp.guest_started("s1", lambda: self.QEMU)
            for _ in range(60):
                await asyncio.sleep(0.05)
                if self.pol("scaling_max_freq") == "2587000":
                    break
            self.assertEqual((self.pol("scaling_governor"), self.pol("scaling_max_freq")), ("schedutil", "2587000"))
            await self.cp.guest_sleep(True)
            self.assertEqual((self.pol("scaling_governor"), self.pol("scaling_max_freq")), (KERNEL_GOV, "1495000"))
            res = self.cp.apply_once(self.QEMU)                  # a poll while asleep
            self.assertTrue(res["asleep"])
            self.assertEqual((self.pol("scaling_governor"), self.pol("scaling_max_freq")), (KERNEL_GOV, "1495000"))
            self.assertEqual(self.aff.masks[4244], {8, 9})
            self.assertEqual((self.sysfs / "devices/virtual/workqueue/cpumask").read_text(), "ff")
            await self.cp.guest_sleep(True)                      # once only
            await self.cp.guest_sleep(False)
            self.assertEqual((self.pol("scaling_governor"), self.pol("scaling_max_freq")), ("schedutil", "2587000"))
            names = [e[0] for e in self.events]
            self.assertEqual((names.count("cpu_placement_sleep"), names.count("cpu_placement_wake")), (1, 1))
            self.cp.guest_stopped("s1")
            self.assertEqual((self.pol("scaling_governor"), self.pol("scaling_max_freq")), (KERNEL_GOV, "1495000"))
            self.assertEqual((self.sysfs / "devices/virtual/workqueue/cpumask").read_text(), "3ff")
            # the bridge says "asleep" before an adopted session is placed: no policy then
            await self.cp.guest_sleep(True)
            self.cp.guest_started("s2", lambda: self.QEMU)
            await asyncio.sleep(0.3)
            self.assertEqual((self.pol("scaling_governor"), self.pol("scaling_max_freq")), (KERNEL_GOV, "1495000"))
            self.assertEqual(self.aff.masks[4244], {8, 9})
            await self.cp.guest_sleep(False)
            self.assertEqual(self.pol("scaling_governor"), "schedutil")
            self.cp.guest_stopped("s2")
        asyncio.run(go())

    def test_max_freq_config(self):
        self._a72dvfs()
        gem = hostplatform.Platform(family="gemini", features=hostplatform.gemini_features())
        for value, want in ((2093, "2093000"), (0, "1495000"), ("none", "1495000")):
            cfg = Config()
            cfg.update({"cpu_max_mhz": value} if value != "none" else {"cpu_placement": "auto"})
            plan = cpuplace.resolve(cfg, gem)
            if value == "none":
                plan.max_mhz = 0
            cp = cpuplace.CpuPlacement(plan, sysfs=self.sysfs, proc=self.proc,
                                       setaffinity=self.aff.set, getaffinity=self.aff.get, self_pid=200)
            cp.apply_once(self.QEMU)
            self.assertEqual(self.pol("scaling_max_freq"), want, value)
            cp.restore()
            self.assertEqual(self.pol("scaling_max_freq"), "1495000")

    def test_max_freq_on_a_kernel_without_a72dvfs(self):
        self._a72dvfs(top="1495000")              # older kernels: 1495 MHz is the top
        self.cp.apply_once(self.QEMU)
        self.assertEqual(self.pol("scaling_max_freq"), "1495000")
        self.assertEqual(self.cp.saved.get("max_freq"), None)        # nothing written, nothing to restore

    def test_governor_unavailable_and_min_freq(self):
        cfg = Config()
        cfg.update({"cpu_governor": "ondemand", "cpu_min_mhz": 1200})
        gem = hostplatform.Platform(family="gemini", features=hostplatform.gemini_features())
        cp = cpuplace.CpuPlacement(cpuplace.resolve(cfg, gem), sysfs=self.sysfs, proc=self.proc,
                                   setaffinity=self.aff.set, getaffinity=self.aff.get, self_pid=200)
        res = cp.apply_once(self.QEMU)
        self.assertEqual((res["governor"], res["governor_unavailable"]), (KERNEL_GOV, "ondemand"))
        self.assertEqual(self.pol("scaling_min_freq"), "1200000")
        cp.restore()
        self.assertEqual(self.pol("scaling_min_freq"), "845000")

    def test_saved_survives_a_hostd_restart(self):
        self.cp.apply_once(self.QEMU)
        gem = hostplatform.Platform(family="gemini", features=hostplatform.gemini_features())
        again = cpuplace.CpuPlacement(cpuplace.resolve(Config(), gem), sysfs=self.sysfs, proc=self.proc,
                                      run_file=self.root / "run/cpuplace.json",
                                      setaffinity=self.aff.set, getaffinity=self.aff.get, self_pid=200)
        again.load_saved()
        again.apply_once(self.QEMU)               # finds "schedutil" already set: keeps the original
        again.restore()
        self.assertEqual(self.pol("scaling_governor"), KERNEL_GOV)

    def test_async_lifecycle(self):
        async def go():
            pid = {"v": None}
            self.cp.guest_started("s1", lambda: pid["v"])
            await asyncio.sleep(0.3)
            pid["v"] = self.QEMU
            for _ in range(40):
                await asyncio.sleep(0.05)
                if self.cp.public().get("vcpu_threads"):
                    break
            self.assertEqual(self.cp.public()["vcpu_cpus"], "8-9")
            self.assertTrue(any(e[0] == "cpu_placement" for e in self.events))
            self.cp.guest_stopped("s1")
            self.assertFalse(self.cp.public()["active"])
            self.assertEqual(self.pol("scaling_governor"), KERNEL_GOV)
            self.assertIn("cpu_placement_released", [e[0] for e in self.events])
        asyncio.run(go())


ASOUND_CARDS = """ 0 [mt6797mt6351   ]: mt6797-mt6351 - mt6797-mt6351
                      mt6797-mt6351
"""


def fake_asound(root: Path, usb: bool = False) -> Path:
    a = root / "asound"
    a.mkdir(parents=True, exist_ok=True)
    cards = ASOUND_CARDS
    pcms = [(0, "Playback_1 (*)"), (1, "Playback_2 (*)"), (2, "Playback_3 (*)"), (7, "Hostless_LPBK (*)")]
    for dev, pid in pcms:
        d = a / f"card0/pcm{dev}p"
        (d / "sub0").mkdir(parents=True, exist_ok=True)
        (d / "info").write_text(f"card: 0\ndevice: {dev}\nsubdevice: 0\nstream: PLAYBACK\nid: {pid}\nname: \n")
        (d / "sub0/status").write_text("closed\n")
    if usb:
        cards += " 1 [Device         ]: USB-Audio - USB Audio Device\n                      USB Audio\n"
        d = a / "card1/pcm0p"
        (d / "sub0").mkdir(parents=True, exist_ok=True)
        (d / "info").write_text("card: 1\ndevice: 0\nstream: PLAYBACK\nid: USB Audio\nname: USB Audio\n")
    (a / "cards").write_text(cards)
    return a


class AudioTests(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)

    def test_card_and_pcm(self):
        a = fake_asound(self.root)
        outs = audio.playback_outputs(a)
        kinds = {o["device"]: o["kind"] for o in outs}
        self.assertEqual(kinds, {0: "analog", 1: "other", 2: "other", 7: "other"})
        out = audio.choose(a, prefer="gemini")
        self.assertEqual((out["card_id"], out["device"], out["alsa"], out["mixer"], out["gemini"]),
                         ("mt6797mt6351", 0, "plughw:CARD=mt6797mt6351,DEV=0", "Headphone", True))
        # a USB sound card wins while plugged in
        a = fake_asound(self.root, usb=True)
        self.assertEqual(audio.choose(a, prefer="gemini")["kind"], "usb")
        # a PC's preference never picks the Gemini card's other PCMs
        self.assertEqual(audio.choose(fake_asound(self.root), prefer="hdmi")["device"], 0)


class PowerSupplyTests(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)
        ps = self.root / "sys/class/power_supply"
        for name, attrs in (("bq25890-charger-0", {"type": "USB", "online": "1"}),
                            ("mt6351-battery", {"type": "Battery", "scope": "Device", "present": "1",
                                                "status": "Charging", "capacity": "64", "voltage_now": "3950000"}),
                            ("usb_otg_vbus", {"type": "USB", "online": "0"})):
            (ps / name).mkdir(parents=True)
            for k, v in attrs.items():
                (ps / name / k).write_text(v + "\n")

    def test_gemini_names(self):
        prof = hostplatform.GEMINI_PROFILE
        p = laptop.PowerSupplies(self.root, prof["ac_supplies"], prof["system_batteries"]).read()
        self.assertTrue(p["ac"])
        self.assertEqual((p["battery"]["name"], p["battery"]["percent"]), ("mt6351-battery", 64))
        self.assertEqual(formfactor.detect(self.root / "sys", "auto", prof["system_batteries"])["form_factor"],
                         "portable")
        # unplugged
        (self.root / "sys/class/power_supply/bq25890-charger-0/online").write_text("0\n")
        self.assertFalse(laptop.PowerSupplies(self.root, prof["ac_supplies"], prof["system_batteries"]).read()["ac"])

    def test_pc_rules_unchanged(self):
        # without the profile: a Device-scope battery is a peripheral's, a USB supply is not the adapter
        p = laptop.PowerSupplies(self.root).read()
        self.assertEqual((p["ac"], p["battery"]), (None, None))
        self.assertEqual(formfactor.detect(self.root / "sys")["form_factor"], "desktop")

    def test_gauge_before_the_chargers_voltage_estimate(self):
        b = self.root / "sys/class/power_supply/bq25890-battery-0"
        b.mkdir()
        for k, v in {"type": "Battery", "present": "1", "status": "Charging", "capacity": "80"}.items():
            (b / k).write_text(v + "\n")
        prof = hostplatform.GEMINI_PROFILE
        p = laptop.PowerSupplies(self.root, prof["ac_supplies"], prof["system_batteries"]).read()
        self.assertEqual((p["battery"]["name"], p["batteries"]), ("mt6351-battery", 2))


class SpeakerTests(unittest.IsolatedAsyncioTestCase):
    """speakers.py only calls the image's rb-gemini-audio (which routes the codec and
    switches the amplifiers with the stream) and reads the output it saved."""

    async def asyncSetUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)
        self.calls = []
        self.rc = 0
        self.events = []
        cfg = Config(root=str(self.root))
        app = type("App", (), {})()
        app.cfg = cfg
        app.platform = hostplatform.Platform(family="gemini", features=hostplatform.gemini_features())
        app.paths = Paths(self.root)
        app.lifecycle = type("L", (), {"event": lambda _s, name, **kw: self.events.append((name, kw))})()
        self.app = app
        self.state = app.paths.data / "gemini/audio-output"

        async def run(argv, timeout=None):
            self.calls.append(argv)
            if self.rc == 0:      # what rb-gemini-audio speaker|headphone does: save the output
                self.state.parent.mkdir(parents=True, exist_ok=True)
                self.state.write_text(argv[1] + "\n")
            return self.rc, "", "" if self.rc == 0 else "rb-gemini-audio: failed"
        self.run = run

    async def test_switch_through_the_hook(self):
        sp = speakers.SpeakerSwitch(self.app, runner=self.run)
        self.assertTrue(sp.enabled)
        self.assertEqual(sp.output, "speaker")                      # no saved output: speaker
        self.assertTrue(await sp.set_output("headphones"))
        self.assertEqual(self.calls[-1], ["/usr/lib/retroboot/gemini/rb-gemini-audio", "headphone"])
        self.assertEqual(sp.output, "headphones")
        self.assertTrue(await sp.set_output("speaker"))
        self.assertEqual(self.calls[-1][1], "speaker")
        self.assertEqual(sp.public(), {"output": "speaker", "hook": "/usr/lib/retroboot/gemini/rb-gemini-audio"})
        self.assertFalse(await sp.set_output("loud"))
        self.rc = 1
        self.assertFalse(await sp.set_output("headphones"))
        self.assertEqual(sp.public()["errors"], ["rb-gemini-audio: failed"])
        self.assertEqual([e[1]["ok"] for e in self.events if e[0] == "audio_output_switch"], [True, True, False])

    async def test_hook_missing_or_off(self):
        sp = speakers.SpeakerSwitch(self.app)                       # the real hook is not installed here
        self.assertFalse(sp.enabled)
        self.assertIsNone(sp.public())
        self.app.cfg.gemini_audio_cmd = "none"
        self.assertFalse(speakers.SpeakerSwitch(self.app, runner=self.run).enabled)
        hook = self.root / "hook"
        hook.write_text("#!/bin/sh\n")
        hook.chmod(0o755)
        self.app.cfg.gemini_audio_cmd = str(hook)
        self.app.cfg.allow_network_tools = True
        self.assertTrue(speakers.SpeakerSwitch(self.app).enabled)    # an installed hook

    async def test_not_on_a_pc(self):
        self.app.platform = hostplatform.Platform()
        sp = speakers.SpeakerSwitch(self.app, runner=self.run)
        self.assertFalse(sp.enabled)
        self.assertFalse(await sp.set_output("headphones"))
        self.assertEqual(self.calls, [])


class GeminiHostdTests(unittest.IsolatedAsyncioTestCase):
    """hostd on a fake Gemini PDA: status, Diagnostics, Software Update, settings defaults,
    the presenter's input_config, and a launch with CPU placement."""

    async def asyncSetUp(self):
        from retroboot import schema
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        self.pkg = helpers.make_package(self.root)
        helpers.fake_qemu_conf(self.pkg, modes=["normal"], boot_s=0.4, finder_s=0.9)
        helpers.make_machine(self.root)
        gemini_tree(self.root)
        fake_cpu_sysfs(self.root / "sys")
        self.cfg = helpers.make_config(self.root, proc_path=str(self.root / "proc"))   # meminfo
        paths = Paths(self.root)
        paths.ensure()
        self.fp = FakePresenter(str(paths.presenter_sock), str(paths.display_sock))
        await self.fp.start()
        self.app = Hostd(self.cfg)
        self.app.settings.store.load()
        self.app.settings.set_desired_and_applied({"machine.active": "mac-os-9"}, schema.ValidationContext(
            machines={"mac-os-9"}, check_capabilities=False))
        helpers.install_fake_prep(self.app)
        self.aff = Affinity(self.root / "sys")
        self.app.cpuplace.setaffinity = self.aff.set
        self.app.cpuplace.getaffinity = self.aff.get
        self.app.cpuplace.proc = Path("/proc")       # the fake QEMU's real threads
        self.app.cpuplace.self_pid = -1              # never this test process
        self.down = False
        await self.app.start()

    async def asyncTearDown(self):
        if not self.down:
            await self.app.shutdown()
        await self.fp.stop()

    async def test_gemini_hostd(self):
        self.assertTrue(self.app.platform.is_gemini)
        st = await self.app.status()
        self.assertEqual(st["host"]["platform"]["family"], "gemini")
        self.assertEqual(st["versions"]["Computer"], "Planet Computers Gemini PDA, 4 GB")
        self.assertTrue(st["host"]["cpu_placement"]["plan"]["enabled"])
        upd = self.app.updates.public()
        self.assertEqual((upd["available"], upd["reason"]), (False, update_manager.UNAVAILABLE_GEMINI))
        self.assertTrue(helpers.events(self.app, "platform"))
        self.assertEqual(self.app.cfg.power_started_after_s, round(Config().power_started_after_s * 3.5, 1))
        self.assertEqual(self.app.startup.launch_estimate(None), hostplatform.LAUNCH_MS["gemini"])
        # the presenter's layer makes Alt Command (swap keeps its default); the chord is
        # Control-Alt-Backspace (Delete is Fn+Backspace); the presenter learns the keycaps
        self.assertFalse(self.app.settings.applied["input.swap_command_option"])
        self.assertEqual(self.app.settings.applied["input.recovery_chord"], "ctrl+alt+backspace")
        cfg = self.app.apply_engine.input_config()
        self.assertEqual((cfg["swap_command_option"], cfg["recovery_chord"], cfg["keyboard_layout"]),
                         (False, "ctrl+alt+backspace", "us"))
        self.assertNotIn("touch_transform", cfg)
        # no GRUB: rb-bootdisplay is never run (display.cfg, background, startup timing)
        marker = self.root / "bootdisplay-ran"
        self.app.cfg.bootdisplay_cmd = ["touch", str(marker)]
        self.assertFalse(await self.app.apply_engine.update_boot_display(why="test"))
        self.assertFalse(marker.exists())
        self.assertEqual(helpers.events(self.app, "boot_display_update")[-1]["skipped"], "no GRUB")
        self.assertFalse(self.app.platform.has_grub)
        self.assertFalse(self.app.speakers.enabled)       # rb-gemini-audio is not installed in the test root
        # the launch: QEMU's argv is the PC one, placement runs and is released at the stop
        await helpers.wait_for(lambda: self.app.paths.argv_json.exists(), 15, what="argv.json")
        argv = json.loads(self.app.paths.argv_json.read_text())["argv"]
        self.assertEqual(argv[argv.index("-name") + 1], "Mac OS 9")
        await helpers.wait_for(lambda: helpers.events(self.app, "cpu_placement"), 15, what="cpu_placement")
        await helpers.wait_for(lambda: self.app.cpuplace.public().get("qemu_pid"), 15, what="qemu pid")
        pol = self.root / "sys/devices/system/cpu/cpufreq/policy8/scaling_governor"
        self.assertEqual(pol.read_text().strip(), "schedutil")
        pid = self.app.cpuplace.public()["qemu_pid"]
        self.assertTrue(any(tid == pid for tid, _ in self.aff.calls))     # QEMU's main thread placed
        st = await self.app.status()
        self.assertEqual(st["host"]["cpu_placement"]["governor"], "schedutil")
        self.assertEqual((st["power"]["lid_sleep"]["action"], st["power"]["lid_sleep"]["phase"]), ("sleep", "idle"))
        self.down = True
        await self.app.shutdown()
        self.assertEqual(pol.read_text().strip(), KERNEL_GOV)
        self.assertTrue(helpers.events(self.app, "cpu_placement_released"))


class LaunchSoundTests(unittest.IsolatedAsyncioTestCase):
    """The guest's sound arguments at a real (fake-QEMU) launch with a sound card: the
    Gemini PDA adds the screamer's longer host pre-roll when the package has it; a PC
    with the same package and card gets exactly QEMU's defaults."""

    GEMINI_OUT = {"kind": "analog", "card": 0, "card_id": "mt6797mt6351", "driver": "mt6797-mt6351",
                  "device": 0, "name": "Playback_1", "connected": None, "gemini": True, "mixer": "Headphone",
                  "alsa": "plughw:CARD=mt6797mt6351,DEV=0"}

    async def launch(self, gemini: bool, features=("screamer-realtime", "screamer-lead-strict"),
                     **cfg_over) -> tuple[list, list]:
        from unittest import mock
        from retroboot import schema
        td, root = helpers.scratch()
        self.addCleanup(td.cleanup)
        pkg = helpers.make_package(root)
        m = json.loads((pkg / "manifest.json").read_text())
        (pkg / "manifest.json").write_text(json.dumps(dict(m, features=list(features))))
        helpers.fake_qemu_conf(pkg, modes=["normal"], boot_s=0.4, finder_s=0.9)
        helpers.make_machine(root)
        if gemini:
            gemini_tree(root)
        fake_cpu_sysfs(root / "sys")
        cfg = helpers.make_config(root, proc_path=str(root / "proc"), **cfg_over)
        paths = Paths(root)
        paths.ensure()
        fp = FakePresenter(str(paths.presenter_sock), str(paths.display_sock))
        await fp.start()
        patches = [mock.patch.object(Hostd, "audio_capable", lambda self: True),
                   mock.patch.object(Hostd, "choose_audio_output", lambda self: dict(self_out))]
        self_out = self.GEMINI_OUT
        for pt in patches:
            pt.start()
            self.addCleanup(pt.stop)
        app = Hostd(cfg)
        app.settings.store.load()
        app.settings.set_desired_and_applied({"machine.active": "mac-os-9"}, schema.ValidationContext(
            machines={"mac-os-9"}, check_capabilities=False))
        helpers.install_fake_prep(app)
        aff = Affinity(root / "sys")
        app.cpuplace.setaffinity = aff.set
        app.cpuplace.getaffinity = aff.get
        app.cpuplace.proc = Path("/proc")
        app.cpuplace.self_pid = -1
        await app.start()
        try:
            await helpers.wait_for(lambda: app.paths.argv_json.exists(), 15, what="argv.json")
            argv = json.loads(app.paths.argv_json.read_text())["argv"]
            events = helpers.events(app, "audio_output")
        finally:
            await app.shutdown()
            await fp.stop()
        return argv, events

    async def test_gemini_adds_the_host_lead(self):
        argv, events = await self.launch(gemini=True)
        i = argv.index("-audiodev")
        self.assertEqual(argv[i + 1:i + 8], ["alsa,id=snd0,out.dev=plughw:CARD=mt6797mt6351,,DEV=0",
                                             "-global", "screamer.host-lead-ms=120",
                                             "-global", "screamer.host-lead-max-ms=160",
                                             "-global", "screamer.host-lead-strict=on"])
        self.assertEqual(events[-1]["screamer"], {"host-lead-ms": 120, "host-lead-max-ms": 160,
                                                  "host-lead-strict": True})

    async def test_gemini_ab_and_old_packages(self):
        argv, _ = await self.launch(gemini=True, audio_host_lead_ms=0)          # lab A/B: QEMU's defaults
        self.assertFalse([a for a in argv if a.startswith("screamer.")])
        argv, _ = await self.launch(gemini=True, audio_host_lead_ms=80)
        self.assertIn("screamer.host-lead-ms=80", argv)
        self.assertIn("screamer.host-lead-strict=on", argv)
        argv, _ = await self.launch(gemini=True, features=("screamer-realtime",))   # r18: no strict pre-roll
        self.assertEqual([a for a in argv if a.startswith("screamer.")],
                         ["screamer.host-lead-ms=120", "screamer.host-lead-max-ms=160"])
        argv, events = await self.launch(gemini=True, features=("screamer-paced-dma",))   # before r15
        self.assertFalse([a for a in argv if a.startswith("screamer.")])
        self.assertNotIn("screamer", events[-1])

    async def test_pc_unchanged(self):
        argv, events = await self.launch(gemini=False)
        self.assertFalse([a for a in argv if a.startswith("screamer.")])
        self.assertEqual(argv[argv.index("-audiodev") + 2], "-action")
        self.assertNotIn("screamer", events[-1])


class PcHostdUnchangedTests(unittest.IsolatedAsyncioTestCase):
    async def test_pc(self):
        td, root = helpers.scratch()
        self.addCleanup(td.cleanup)
        helpers.make_package(root)
        fake_cpu_sysfs(root / "sys")
        cfg = helpers.make_config(root)
        paths = Paths(root)
        paths.ensure()
        fp = FakePresenter(str(paths.presenter_sock), None)
        await fp.start()
        app = Hostd(cfg)
        app.settings.store.load()
        helpers.install_fake_prep(app)
        await app.start()
        try:
            self.assertFalse(app.cpuplace.plan.enabled)
            self.assertFalse(app.speakers.enabled)
            self.assertFalse(app.settings.applied["input.swap_command_option"])
            self.assertEqual(app.settings.applied["input.recovery_chord"], "ctrl+alt+delete")
            st = await app.status()
            self.assertNotIn("cpu_placement", st["host"])
            self.assertNotIn("speakers", st["host"])
            self.assertNotIn("lid_sleep", st["power"])            # PCs: the lid blanks the panel only
            self.assertEqual(app.lidsleep.action, "blank")
            self.assertEqual(set(app.apply_engine.input_config()),
                             {"swap_command_option", "recovery_chord", "pointer_speed",
                              "legacy_mouse", "touch_gestures"})
            self.assertFalse(app.capabilities()["speaker_switch"])
            marker = root / "bootdisplay-ran"
            app.cfg.bootdisplay_cmd = ["touch", str(marker)]
            self.assertTrue(await app.apply_engine.update_boot_display(why="test"))
            self.assertTrue(marker.exists())
        finally:
            await app.shutdown()
            await fp.stop()
        self.assertEqual((root / "sys/devices/system/cpu/cpufreq/policy8/scaling_governor").read_text().strip(),
                         KERNEL_GOV)


if __name__ == "__main__":
    unittest.main()
