"""Wave 3 session behaviour: installer mode from the boot environment, the video
driver variant overlay, -g equal to the presenter's guest target, the 4.3
content rule of the development probe and the frozen-guest check (with
fakes; the end-to-end variants are in test_lifecycle.py)."""
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import types
import unittest
from pathlib import Path

import helpers
from retroboot import machines, schema
from retroboot.config import Config, Paths, boot_mode
from retroboot.freeze import FrozenGuestCheck, mostly_black
from retroboot.launcher import verify_overlay
from retroboot.machines import (LaunchOptions, MachineError, MachineRegistry, PackageCache, build_argv,
                                guest_geometry, launch_spec, verify_ndrv_overlay)
from retroboot.qmp import frame_samples, genuine_content, samples_changed


class ModeTests(unittest.TestCase):
    def test_boot_mode_precedence(self):
        self.assertEqual(boot_mode("normal", {}, "quiet splash"), "normal")
        self.assertEqual(boot_mode("normal", {}, "boot=retroboot-live retroboot.mode=installer quiet"), "installer")
        self.assertEqual(boot_mode("normal", {"RETROBOOT_MODE": "installer"}, ""), "installer")
        # the environment wins over the command line, unknown values are ignored
        self.assertEqual(boot_mode("normal", {"RETROBOOT_MODE": "normal"}, "retroboot.mode=installer"), "normal")
        self.assertEqual(boot_mode("normal", {"RETROBOOT_MODE": "bogus"}, "retroboot.mode=bogus"), "normal")
        self.assertEqual(boot_mode("installer", {}, ""), "installer")
        self.assertEqual(boot_mode("weird", {}, ""), "normal")

    def test_config_load_reads_cmdline_and_env(self):
        td, root = helpers.scratch()
        self.addCleanup(td.cleanup)
        cmdline = root / "cmdline"
        cmdline.write_text("BOOT_IMAGE=/live/vmlinuz boot=retroboot-live retroboot.mode=installer quiet\n")
        old = os.environ.pop("RETROBOOT_MODE", None)
        self.addCleanup(lambda: os.environ.__setitem__("RETROBOOT_MODE", old) if old is not None
                        else os.environ.pop("RETROBOOT_MODE", None))
        cfg = Config.load(root=str(root), overrides={"cmdline_path": str(cmdline), "mode": None})
        self.assertEqual(cfg.mode, "installer")
        # A scratch root never inherits the development host's /proc/cmdline.
        self.assertEqual(Config.load(root=str(root)).mode, "normal")
        os.environ["RETROBOOT_MODE"] = "installer"
        self.assertEqual(Config.load(root=str(root)).mode, "installer")
        # --installer (override) always wins
        os.environ["RETROBOOT_MODE"] = "normal"
        self.assertEqual(Config.load(root=str(root), overrides={"mode": "installer"}).mode, "installer")

    def test_hostd_parse_args_without_installer_flag(self):
        from retroboot.hostd import parse_args
        _, ov = parse_args(["--root", "/tmp/x"])
        self.assertIsNone(ov["mode"])
        _, ov = parse_args(["--installer"])
        self.assertEqual(ov["mode"], "installer")


def make_ndrv(root: Path, data: bytes = b"retroboot ndrv variant", good: bool = True) -> Path:
    d = root / "usr/lib/retroboot/qemu-ndrv"
    d.mkdir(parents=True, exist_ok=True)
    (d / "qemu_vga.ndrv").write_bytes(data)
    digest = hashlib.sha256(data).hexdigest() if good else "0" * 64
    (d / "manifest.json").write_text(json.dumps({"component": "qemu_vga.ndrv (RetroBoot variant)", "sha256": digest,
                                                 "pinned_unmodified_sha256": "3b687b15" + "0" * 56,
                                                 "define": "RETROBOOT_PREFER_BOOT_MODE"}))
    return d


class NdrvOverlayTests(unittest.TestCase):
    def setUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        self.pkg = helpers.make_package(self.root)
        helpers.make_machine(self.root)
        self.paths = Paths(self.root)
        self.paths.ensure()
        self.m = MachineRegistry(self.paths).get("mac-os-9")
        self.settings = schema.defaults()
        self.display = {"mode": [1280, 800, 75], "suggested_scale": 1}

    def test_absent_overlay_keeps_package_driver(self):
        cache = PackageCache(self.pkg, self.paths.ndrv_dir)
        cache.verify()
        self.assertIsNone(cache.ndrv)
        a = build_argv(self.m, self.settings, self.paths, self.display)
        self.assertEqual([a[i + 1] for i, v in enumerate(a) if v == "-L"], [str((self.pkg / "share/qemu").resolve())])

    def test_overlay_is_verified_and_searched_first(self):
        d = make_ndrv(self.root)
        cache = PackageCache(self.pkg, self.paths.ndrv_dir)
        hashes = cache.verify()
        self.assertIn("share/qemu/qemu_vga.ndrv", hashes)       # package manifest untouched and verified
        self.assertEqual(cache.ndrv["sha256"], hashlib.sha256(b"retroboot ndrv variant").hexdigest())
        self.assertTrue(cache.summary()["ndrv"]["sha256"])
        a = build_argv(self.m, self.settings, self.paths, self.display,
                       LaunchOptions(ndrv_dir=Path(cache.ndrv["dir"])))
        self.assertEqual([a[i + 1] for i, v in enumerate(a) if v == "-L"],
                         [str(d.resolve()), str((self.pkg / "share/qemu").resolve())])
        spec = launch_spec(self.m, a, self.paths, "s", self.settings, None, "retroboot", ndrv=cache.ndrv)
        self.assertEqual(spec["package"]["overlay"]["files"], {"qemu_vga.ndrv": cache.ndrv["sha256"]})
        verify_overlay(spec["package"]["overlay"])
        (d / "qemu_vga.ndrv").write_bytes(b"swapped after hostd checked it")
        with self.assertRaisesRegex(MachineError, "Overlay hash mismatch"):
            verify_overlay(spec["package"]["overlay"])

    def test_tampered_overlay_refused(self):
        make_ndrv(self.root, good=False)
        with self.assertRaisesRegex(MachineError, "Video driver hash mismatch"):
            verify_ndrv_overlay(self.paths.ndrv_dir)
        with self.assertRaises(MachineError):
            PackageCache(self.pkg, self.paths.ndrv_dir).verify()

    def test_launcher_refuses_tampered_overlay(self):
        d = make_ndrv(self.root)
        ndrv = verify_ndrv_overlay(d)
        a = build_argv(self.m, self.settings, self.paths, None, LaunchOptions(ndrv_dir=d))
        spec = launch_spec(self.m, a, self.paths, "sess-o", self.settings, None, "retroboot", ndrv=ndrv)
        self.paths.argv_json.write_text(json.dumps(spec))
        (d / "qemu_vga.ndrv").write_bytes(b"tampered")
        env = dict(os.environ, PYTHONPATH=str(helpers.SRC))
        rc = subprocess.run([sys.executable, "-m", "retroboot.launcher", "--root", str(self.root)], env=env,
                            timeout=60).returncode
        self.assertEqual(rc, 1)
        st = json.loads(self.paths.status_json.read_text())
        self.assertIn("Overlay hash mismatch", st["exit"]["detail"])
        self.assertFalse(helpers.fake_qemu_log(self.pkg))

    def test_pio_status_drain_for_disk_boots_only(self):
        a = build_argv(self.m, self.settings, self.paths, self.display)
        self.assertIn("macio-ide.pio-status-drain=on", a)
        self.assertIn("macio-ide.dma-completion-delay-ns=0", a)
        cd = self.root / "boot.iso"
        cd.write_bytes(b"\0" * 2048)
        a = build_argv(self.m, self.settings, self.paths, self.display, LaunchOptions(boot_cd=cd))
        self.assertNotIn("macio-ide.pio-status-drain=on", a)
        self.assertIn("macio-ide.dma-completion-delay-ns=1000000", a)

    def test_boot_speed_offers_realtime_only(self):
        d = schema.BY_KEY["machine.boot_speed"].describe()
        self.assertEqual([c["value"] for c in d["choices"]], ["realtime"])
        self.assertEqual(schema.defaults()["machine.boot_speed"], "realtime")
        self.assertEqual(schema.defaults()["machine.ram_mb"], 512)
        # still valid for stored settings, the API and image defaults (diagnostic use)
        self.assertEqual(schema.BY_KEY["machine.boot_speed"].validate("accelerated"), "accelerated")
        with self.assertRaises(ValueError):
            schema.BY_KEY["machine.boot_speed"].validate("turbo")

    def test_lab_qmp_socket(self):
        a = build_argv(self.m, self.settings, self.paths, self.display, LaunchOptions(lab_qmp=self.paths.lab_qmp))
        self.assertIn(f"unix:{self.paths.lab_qmp},server=on,wait=off", a)
        self.assertEqual(sum(1 for v in a if v == "-qmp"), 3)
        self.assertNotIn(str(self.paths.lab_qmp), " ".join(build_argv(self.m, self.settings, self.paths, self.display)))


def presenter_auto(w: int, h: int, req: int = 0) -> tuple[int, int]:
    """presenter/src/scale.c rb_suggested_scale, rb_effective_scale and
    rb_auto_guest_size (4.5), transcribed."""
    def valid(s):
        gw, gh = (w // s // 8) * 8, h // s
        return 640 <= gw <= 3840 and 480 <= gh <= 2160
    s = 1
    for c in range(2, 17):
        if w >= 720 * c and h >= 540 * c:
            s = c
    if req:
        cands = [c for c in list(range(req, 0, -1)) + list(range(req + 1, 5)) if valid(c)]
        s = cands[0] if cands else s
    gw, gh = (w // s // 8) * 8, h // s
    return min(3840, max(640, gw)), min(2160, max(480, gh))


class GeometryTests(unittest.TestCase):
    MODES = [(1024, 768), (1280, 800), (1366, 768), (1440, 900), (1600, 900), (1920, 1080), (1920, 1200),
             (2560, 1440), (2560, 1600), (3440, 1440), (3840, 2160), (5120, 1440), (5120, 2880), (800, 480),
             (800, 400), (1921, 1081), (2563, 1443), (3843, 2162)]

    def test_g_equals_presenter_target(self):
        for w, h in self.MODES:
            disp = {"mode": [w, h, 60], "suggested_scale": machines.suggested_scale(w, h)}
            self.assertEqual(guest_geometry(disp, "auto"), presenter_auto(w, h), (w, h))
            for req in (1, 2, 3, 4):
                self.assertEqual(guest_geometry(disp, "auto", str(req)), presenter_auto(w, h, req), (w, h, req))
        self.assertEqual(guest_geometry({"mode": [1280, 800, 75], "suggested_scale": 1}, "1024x768"), (1024, 768))
        self.assertEqual(guest_geometry({"mode": [3840, 2160, 60]}, "640x480", "2"), (640, 480))


class ContentProbeTests(unittest.TestCase):
    def test_gray_rule_needs_97_percent(self):
        w, h = 160, 100
        gray = bytearray(b"\x99\x99\x99" * (w * h))
        self.assertTrue(genuine_content(w, h, bytes(gray))[0])
        partial = bytearray(gray)
        partial[w * 3 * 70:] = bytes(len(partial) - w * 3 * 70)   # 30 % still black
        ok, frac, nb = genuine_content(w, h, bytes(partial))
        self.assertFalse(ok)
        self.assertLess(frac, 0.75)
        a, b = frame_samples(w, h, bytes(gray)), frame_samples(w, h, bytes(partial))
        self.assertEqual(samples_changed(a, a), 0.0)
        self.assertGreater(samples_changed(a, b), 0.2)
        self.assertEqual(samples_changed(None, a), 1.0)

    def test_mostly_black(self):
        w, h = 128, 96
        self.assertTrue(mostly_black((w, h, bytes(w * h * 3))))
        self.assertFalse(mostly_black((w, h, b"\x66\x66\xcc" * (w * h))))
        self.assertFalse(mostly_black(None))


class FakeSup:
    """Just enough of Supervisor for FrozenGuestCheck."""

    def __init__(self):
        self.cfg = types.SimpleNamespace(frozen_stale_s=75.0, frozen_quiet_s=30.0, frozen_nudge_wait_s=3.0,
                                         frozen_poll_s=5.0)
        self.app = types.SimpleNamespace(presenter=None)
        self.session = "s1"
        self.state = "running"
        self.paused = False
        self.content_seen = True
        self.display_ok = None
        self.events: list[tuple[str, dict]] = []
        self.log = types.SimpleNamespace(event=lambda name, **kw: self.events.append((name, kw)))

    @property
    def active(self):
        return self.state in ("starting", "running", "paused", "stopping")

    def set_display_responsive(self, ok, session, **extra):
        self.display_ok = ok
        self.events.append(("responsive", {"ok": ok, **extra}))

    async def dump_frame(self):
        return None


class FakeGuest:
    """Presenter status + pointer_nudge over a guest that may be frozen."""

    def __init__(self):
        self.t = 0.0             # seconds since the last frame change
        self.seq = 10
        self.input_ms = None
        self.frozen = False
        self.connected = True
        self.nudges = 0
        self.frame = (64, 48, b"\x66\x66\xcc" * (64 * 48))

    async def status(self):
        g = {"connected": self.connected, "damage_seq": self.seq if self.connected else None,
             "last_damage_ms": int(self.t * 1000)}
        return {"guest": g, "last_input_ms": self.input_ms}

    async def nudge(self):
        self.nudges += 1
        seq = self.seq
        if not self.frozen:
            self.seq += 1       # the guest redraws its cursor
            self.t = 0.0
        return {"ok": True, "damage_seq": seq, "pointer": [16, 16]}

    async def frame_fn(self):
        return self.frame

    def change(self):
        self.seq += 1
        self.t = 0.0


class FrozenCheckTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sup = FakeSup()
        self.g = FakeGuest()
        self.clock = [1000.0]
        import retroboot.freeze as fz
        self._mono = fz.mono
        fz.mono = lambda: self.clock[0]
        self.addCleanup(lambda: setattr(fz, "mono", self._mono))

        async def sleep(s):
            self.clock[0] += s
            self.g.t += s
            await asyncio.sleep(0)
        self.chk = FrozenGuestCheck(self.sup, status=self.g.status, nudge=self.g.nudge, frame=self.g.frame_fn,
                                    sleep=sleep)
        self.sleep = sleep

    async def advance(self, seconds, step=5.0):
        while seconds > 0:
            await self.sleep(min(step, seconds))
            seconds -= step
            await self.chk.step("s1")

    def names(self):
        return [n for n, _ in self.sup.events]

    async def test_healthy_clock_never_nudges(self):
        for _ in range(60):          # 5 minutes, the menu-bar clock changes every minute
            await self.advance(5)
            if self.g.t >= 60:
                self.g.change()
        self.assertEqual(self.g.nudges, 0)
        self.assertIsNone(self.sup.display_ok)

    async def test_static_but_alive_guest_nudged_at_most_once_per_period(self):
        await self.advance(300)      # clock hidden: nothing changes, but the cursor redraws
        self.assertGreaterEqual(self.g.nudges, 2)
        self.assertLessEqual(self.g.nudges, 300 // 75 + 1)
        self.assertIsNone(self.sup.display_ok)
        self.assertTrue(all(kw["result"] == "redrawn" for n, kw in self.sup.events if n == "guest_nudge"))

    async def test_frozen_guest_flagged_after_nudge_and_cleared_by_a_frame(self):
        self.g.frozen = True
        await self.advance(70)
        self.assertEqual(self.g.nudges, 0)            # not yet 75 s
        await self.advance(10)
        self.assertEqual(self.g.nudges, 1)
        self.assertIs(self.sup.display_ok, False)
        self.assertIn(("guest_nudge", {"session": "s1", "result": "no-redraw", "pointer": [16, 16],
                                       "damage_seq": 10, "frame_age_s": 75.0}), self.sup.events)
        await self.advance(60)
        self.assertIs(self.sup.display_ok, False)
        # QMP cont: the guest catches up and draws again
        self.g.frozen = False
        self.g.change()
        await self.advance(5)
        self.assertIs(self.sup.display_ok, True)

    async def test_recovered_guest_with_hidden_clock_found_by_renudge(self):
        self.g.frozen = True
        await self.advance(80)
        self.assertIs(self.sup.display_ok, False)
        self.g.frozen = False         # recovered, but nothing on screen changes by itself
        await self.advance(80)
        self.assertIs(self.sup.display_ok, True)

    async def test_user_input_postpones_the_nudge(self):
        self.g.frozen = True
        self.g.input_ms = 5_000       # someone is using the keyboard/pointer
        await self.advance(120)
        self.assertEqual(self.g.nudges, 0)
        self.assertIsNone(self.sup.display_ok)
        self.g.input_ms = 45_000
        await self.advance(5)
        self.assertEqual(self.g.nudges, 1)
        self.assertIs(self.sup.display_ok, False)

    async def test_black_display_is_not_nudged(self):
        self.g.frozen = True
        self.g.frame = (64, 48, bytes(64 * 48 * 3))
        await self.advance(300)
        self.assertEqual(self.g.nudges, 0)
        self.assertIsNone(self.sup.display_ok)
        self.assertIn("frozen_check_skipped", self.names())

    async def test_inactive_without_presenter_frames(self):
        self.g.connected = False
        await self.advance(200)
        self.assertEqual(self.g.nudges, 0)
        self.assertEqual(self.names().count("frozen_check_inactive"), 1)

    async def test_run_loop_only_observes_a_running_guest(self):
        self.g.frozen = True
        self.sup.paused = True       # hostd paused it for the startup options
        task = asyncio.get_running_loop().create_task(self.chk.run("s1"))
        for _ in range(40):
            await asyncio.sleep(0)
        self.sup.state = "stopped"
        await asyncio.wait_for(task, 5)
        self.assertEqual(self.g.nudges, 0)



def make_loader(root: Path, data: bytes = b"retroboot classicvirtio loader", good: bool = True) -> Path:
    d = root / "usr/lib/retroboot/qemu-classicvirtio"
    d.mkdir(parents=True, exist_ok=True)
    (d / "ndrvloader").write_bytes(data)
    digest = hashlib.sha256(data).hexdigest() if good else "0" * 64
    (d / "manifest.json").write_text(json.dumps({"component": "ndrvloader (RetroBoot classicvirtio variant)",
                                                 "sha256": digest, "pinned_unmodified_sha256": "a5b44104" + "0" * 56,
                                                 "features": ["folder-dates", "dir-cache", "finder-refresh"]}))
    return d


class LoaderOverlayTests(unittest.TestCase):
    """The shared-folder driver variant (emulator/classicvirtio) beside the package."""

    def setUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        self.pkg = helpers.make_package(self.root)
        helpers.make_machine(self.root)
        self.paths = Paths(self.root)
        self.paths.ensure()
        self.m = MachineRegistry(self.paths).get("mac-os-9")
        self.settings = schema.defaults()
        self.display = {"mode": [1280, 800, 75], "suggested_scale": 1}

    @staticmethod
    def loader_arg(argv):
        return next(v for v in argv if v.startswith("loader,addr=0x4000000,file="))

    def test_absent_overlay_keeps_package_loader(self):
        cache = PackageCache(self.pkg, self.paths.ndrv_dir, self.paths.classicvirtio_dir)
        cache.verify()
        self.assertIsNone(cache.loader)
        a = build_argv(self.m, self.settings, self.paths, self.display)
        self.assertTrue(self.loader_arg(a).endswith("share/qemu/ndrvloader"))
        spec = launch_spec(self.m, a, self.paths, "s", self.settings, None, "retroboot")
        self.assertNotIn("loader_overlay", spec["package"])

    def test_overlay_is_verified_and_loaded(self):
        d = make_loader(self.root)
        cache = PackageCache(self.pkg, self.paths.ndrv_dir, self.paths.classicvirtio_dir)
        hashes = cache.verify()
        self.assertIn("share/qemu/ndrvloader", hashes)          # package manifest untouched and verified
        self.assertEqual(cache.loader["sha256"], hashlib.sha256(b"retroboot classicvirtio loader").hexdigest())
        self.assertTrue(cache.summary()["classicvirtio"]["sha256"])
        a = build_argv(self.m, self.settings, self.paths, self.display,
                       LaunchOptions(loader_dir=Path(cache.loader["dir"])))
        self.assertEqual(self.loader_arg(a), f"loader,addr=0x4000000,file={(d / 'ndrvloader').resolve()}")
        self.assertEqual(sum(1 for v in a if v.startswith("loader,")), 1)
        spec = launch_spec(self.m, a, self.paths, "s", self.settings, None, "retroboot", loader=cache.loader)
        self.assertEqual(spec["package"]["loader_overlay"]["files"], {"ndrvloader": cache.loader["sha256"]})
        verify_overlay(spec["package"]["loader_overlay"])
        (d / "ndrvloader").write_bytes(b"swapped after hostd checked it")
        with self.assertRaisesRegex(MachineError, "Overlay hash mismatch"):
            verify_overlay(spec["package"]["loader_overlay"])

    def test_loader_used_with_share_off_and_for_the_cd_route(self):
        d = make_loader(self.root)
        s = dict(self.settings, **{"share.enabled": False})
        a = build_argv(self.m, s, self.paths, self.display, LaunchOptions(loader_dir=d))
        self.assertIn(str((d / "ndrvloader").resolve()), self.loader_arg(a))
        self.assertFalse(any("virtio-9p-pci" in v for v in a))

    def test_tampered_overlay_refused(self):
        make_loader(self.root, good=False)
        with self.assertRaisesRegex(MachineError, "Shared folder driver hash mismatch"):
            machines.verify_loader_overlay(self.paths.classicvirtio_dir)
        with self.assertRaises(MachineError):
            PackageCache(self.pkg, self.paths.ndrv_dir, self.paths.classicvirtio_dir).verify()

    def test_launcher_refuses_tampered_loader(self):
        d = make_loader(self.root)
        loader = machines.verify_loader_overlay(d)
        a = build_argv(self.m, self.settings, self.paths, None, LaunchOptions(loader_dir=d))
        spec = launch_spec(self.m, a, self.paths, "sess-l", self.settings, None, "retroboot", loader=loader)
        self.paths.argv_json.write_text(json.dumps(spec))
        (d / "ndrvloader").write_bytes(b"tampered")
        env = dict(os.environ, PYTHONPATH=str(helpers.SRC))
        rc = subprocess.run([sys.executable, "-m", "retroboot.launcher", "--root", str(self.root)], env=env,
                            timeout=60).returncode
        self.assertEqual(rc, 1)
        st = json.loads(self.paths.status_json.read_text())
        self.assertIn("Overlay hash mismatch", st["exit"]["detail"])
        self.assertFalse(helpers.fake_qemu_log(self.pkg))

if __name__ == "__main__":
    unittest.main()


class HostdContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_job_outcomes_and_replacement_wording(self):
        from retroboot.jobs import JobManager
        jm = JobManager()

        async def steps(job):
            job.update(0.5, "Starting Mac OS…")
            return {}

        async def custom(job):
            job.update(0.5, "Working…")
            job.outcome = "Mac OS shut down. RetroBoot is still on."
            return {}

        async def final_message(job):
            job.update(1.0, "Mac OS 9.2.2 is installed.")
            return {}

        async def slow(job):
            await asyncio.sleep(10)
        a = jm.start("guest.start", steps)
        b = jm.start("guest.shutdown", custom)
        c = jm.start("machines.import", final_message)
        for j in (a, b, c):
            await j.done.wait()
        self.assertEqual(a.message, "Mac OS is running.")
        self.assertEqual(b.message, "Mac OS shut down. RetroBoot is still on.")
        self.assertEqual(c.message, "Mac OS 9.2.2 is installed.")
        d = jm.start("guest.restart", slow, resources=("guest",), preemptible=True)
        e = jm.start("guest.force-restart", steps, resources=("guest",), preempts=("guest.restart",))
        await e.done.wait()
        self.assertEqual(d.public()["error"]["message"], "Replaced by Force Restart.")
        self.assertNotIn("guest.", json.dumps(d.public()["error"]))

    def test_redacted_lifecycle_lines_stay_json(self):
        from retroboot.util import Redactor
        r = Redactor()
        r.add("123456")
        lines = ['{"event": "guest_start", "session": "0b8f3a1e-1111-4222-8333-444455556666"}',
                 '{"event": "wifi_connect", "psk": "hunter22", "password": "x y z"}',
                 '{"event": "pairing_code_issued", "note": "code 123456"}']
        out = [json.loads(r(l)) for l in lines]
        self.assertEqual(out[0]["session"], "0b8f3a1e-1111-4222-8333-444455556666")
        self.assertEqual((out[1]["psk"], out[1]["password"]), ("[REDACTED]", "[REDACTED]"))
        self.assertNotIn("123456", json.dumps(out[2]))
        self.assertNotIn("abcdef", r("rb_session=abcdef; Path=/"))
        self.assertEqual(r('{"session": "not-a-uuid-token"}'), '{"session": "[REDACTED]"}')

    async def test_status_has_no_linux_wording_and_catalog_errors_are_reported(self):
        from aiohttp.test_utils import TestClient, TestServer
        from retroboot.hostd import Hostd
        td, root = helpers.scratch()
        self.addCleanup(td.cleanup)
        helpers.make_package(root)
        cfg = helpers.make_config(root)
        cfg.catalog_url = "https://127.0.0.1:9/catalog.json"     # nothing listens there
        app = Hostd(cfg)
        app.loop = asyncio.get_running_loop()
        app.settings.store.load()
        st = await app.status()
        self.assertNotIn("linux", json.dumps(st["versions"]).lower())
        self.assertEqual(set(st["versions"]) & {"RetroBoot service", "Display service"},
                         {"RetroBoot service", "Display service"})
        app.web.tls()
        client = TestClient(TestServer(app.web.web_app, host="127.0.0.1"))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        code, _ = app.pairing.current_code()
        r = await client.post("/api/v1/pair", json={"code": code, "device_name": "t"})
        cookie = r.headers.getall("Set-Cookie")[0].split("rb_session=", 1)[1].split(";", 1)[0]
        hdr = {"Cookie": f"rb_session={cookie}"}
        first = await (await client.get("/api/v1/machines", headers=hdr)).json()
        self.assertEqual(first["catalog_state"], "loading")

        async def errored():
            body = await (await client.get("/api/v1/machines", headers=hdr)).json()
            return body if body["catalog_state"] == "error" else None
        body = await helpers.wait_for(errored, 20, 0.2, "catalog error state")
        self.assertTrue(body["catalog_error"])
        self.assertEqual(body["catalog"], [])
