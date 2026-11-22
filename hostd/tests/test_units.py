"""Jobs, argv construction, display policy, catalog validation/extraction, pairing, guest-prep adapter."""
import asyncio
import base64
import hashlib
import io
import json
import os
import ssl
import tarfile
import unittest
from pathlib import Path

import helpers
from retroboot import catalog, machines, schema
from retroboot.config import Config, Paths
from retroboot.guestprep import GuestPrep
from retroboot.jobs import Busy, JobFailed, JobManager
from retroboot.launcher import classify
from retroboot.machines import LaunchOptions, MachineError, MachineRegistry, build_argv, guest_geometry, suggested_scale
from retroboot.qmp import encode_png, finder_menu_bar, genuine_content
from retroboot.security import CODE_TTL, PairingError, PairingService, fingerprint, ensure_certificate, qr_matrix
from retroboot.supervisor import changed_fraction
from retroboot.util import Redactor


class JobTests(unittest.IsolatedAsyncioTestCase):
    async def test_dedupe_exclusivity_preemption(self):
        jm = JobManager()
        gate = asyncio.Event()

        async def slow(job):
            job.update(0.5, "half")
            await gate.wait()
            return "ok"

        a = jm.start("guest-restart", slow, resources=("guest",), preemptible=True)
        self.assertIs(jm.start("guest-restart", slow, resources=("guest",)), a)  # duplicate returns running job
        with self.assertRaises(Busy):
            jm.start("checkpoint", slow, resources=("guest",))
        free = jm.start("diagnostics-bundle", slow)  # no shared resource
        f = jm.start("guest-force-stop", slow, resources=("guest",), preempts=("guest-restart",))
        await asyncio.sleep(0.01)
        self.assertEqual(a.state, "failed")
        self.assertEqual(a.error["code"], "superseded")
        gate.set()
        await f.done.wait()
        await free.done.wait()
        self.assertEqual(f.state, "succeeded")
        self.assertEqual(f.result, "ok")
        self.assertEqual(f.progress, 1.0)

    async def test_failures(self):
        jm = JobManager()

        async def fails(job):
            raise JobFailed("Mac OS did not respond.", {"x": 1}, "guest_unresponsive")

        async def crashes(job):
            raise RuntimeError("boom with secret")
        j1 = jm.start("a", fails)
        j2 = jm.start("b", crashes)
        await j1.done.wait()
        await j2.done.wait()
        self.assertEqual(j1.public()["error"], {"code": "guest-unresponsive", "message": "Mac OS did not respond."})
        self.assertEqual(j2.public()["error"]["code"], "internal")
        self.assertNotIn("secret", json.dumps(j2.public()))


class ArgvTests(unittest.TestCase):
    def setUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        self.pkg = helpers.make_package(self.root)
        helpers.make_machine(self.root)
        self.paths = Paths(self.root)
        self.paths.ensure()
        self.m = MachineRegistry(self.paths).get("mac-os-9")
        self.settings = schema.defaults()
        self.display = {"mode": [1920, 1080, 60], "suggested_scale": 2}

    def test_display_compatibility_rejects_non_boolean_machine_values(self):
        for value in (0, 1, "true", "false", None, [], {}):
            with self.subTest(value=value):
                self.m.meta["template"] = {"directFramebuffer": value}
                with self.assertRaises(MachineError):
                    machines.validate_meta(self.m.meta, self.m.id)
                with self.assertRaises(MachineError):
                    build_argv(self.m, self.settings, self.paths, self.display)

    def test_appliance_argv(self):
        a = build_argv(self.m, self.settings, self.paths, self.display)
        s = " ".join(a)
        for frag in ("-M mac99,via=cuda,audiodev=snd0", "-cpu 7400", "-m 512", "-action reboot=shutdown",
                     "-rtc base=localtime", "-g 960x540x32", "-prom-env output-device=ttya",
                     f"-display retroboot,socket={self.paths.display_sock}",
                     f"unix:{self.paths.qmp_control},server=on,wait=off", f"unix:{self.paths.qmp_events},server=on,wait=off",
                     f"-serial file:{self.paths.serial_log}", "-device virtio-tablet-pci",
                     "-nic user,model=sungem", "mount_tag=RetroBoot Share", "VGA.hardware-cursor=off"):
            self.assertIn(frag, s)
        self.assertNotIn("-icount", a)
        self.assertNotIn("-vnc", a)

    def test_accelerated_and_options(self):
        st = dict(self.settings, **{"machine.boot_speed": "accelerated", "network.guest": "none",
                                    "share.enabled": False, "audio.enabled": False, "machine.cpu": "g3",
                                    "machine.ram_mb": 256, "display.guest_size": "800x600"})
        a = build_argv(self.m, st, self.paths, self.display, LaunchOptions(display_backend="vnc"))
        s = " ".join(a)
        self.assertIn("-icount shift=4,sleep=off -global VGA.classicmac-boot-handoff=on", s)
        self.assertIn("-nic none", s)
        self.assertIn("-audiodev none,id=snd0", s)
        self.assertNotIn("virtio-9p-pci", s)
        self.assertIn("-g 800x600x32", s)
        self.assertIn(f"-display none -vnc unix:{self.paths.vnc_sock}", s)

    def test_audio_capability_gates_alsa(self):
        a = build_argv(self.m, self.settings, self.paths, self.display, LaunchOptions(audio_capable=True))
        self.assertIn("alsa,id=snd0", a)
        a = build_argv(self.m, self.settings, self.paths, self.display, LaunchOptions(audio_capable=False))
        self.assertIn("none,id=snd0", a)
        # an explicit playback device (audio.choose) is passed with QEMU's comma escaping
        a = build_argv(self.m, self.settings, self.paths, self.display,
                       LaunchOptions(audio_capable=True, audio_device="plughw:CARD=Generic,DEV=7"))
        self.assertIn("alsa,id=snd0,out.dev=plughw:CARD=Generic,,DEV=7", a)

    def test_usb_mouse_beside_the_tablet(self):
        a = build_argv(self.m, self.settings, self.paths, self.display)
        self.assertFalse([v for v in a if v.startswith("usb-")])
        a = build_argv(self.m, self.settings, self.paths, self.display,
                       LaunchOptions(usb_mouse=True, usb_wheel_per_report=1))
        i = a.index("virtio-tablet-pci")
        self.assertEqual(a[i + 1:i + 3], ["-device", "usb-mouse,wheel-per-report=1"])
        # a package without the wheel-per-report property gets the plain device
        a = build_argv(self.m, self.settings, self.paths, self.display, LaunchOptions(usb_mouse=True))
        self.assertIn("usb-mouse", a)
        self.assertNotIn("usb-mouse,wheel-per-report=1", a)
        # never on the CD/installer route
        cd = self.root / "cd.iso"
        cd.write_bytes(b"\0" * 2048)
        a = build_argv(self.m, self.settings, self.paths, self.display,
                       LaunchOptions(usb_mouse=True, usb_wheel_per_report=1, boot_cd=cd))
        self.assertFalse([v for v in a if v.startswith("usb-")])

    def test_pointer_policy(self):
        from retroboot import pointer
        self.assertTrue(pointer.wanted("auto", True))
        self.assertFalse(pointer.wanted("auto", False))
        self.assertFalse(pointer.wanted("auto", None))        # unknown: keep the tablet alone
        self.assertTrue(pointer.wanted("on", False))
        self.assertFalse(pointer.wanted("off", True))
        self.assertEqual(pointer.device_arg(None), "usb-mouse")
        self.assertEqual(pointer.device_arg(1), "usb-mouse,wheel-per-report=1")
        with self.assertRaises(ValueError):
            pointer.device_arg(0)
        m = json.loads((self.pkg / "manifest.json").read_text())
        self.assertFalse(pointer.package_wheel_per_report(self.pkg))
        m["features"] = list(m.get("features") or []) + [pointer.FEATURE]
        (self.pkg / "manifest.json").write_text(json.dumps(m))
        self.assertTrue(pointer.package_wheel_per_report(self.pkg))
        self.assertFalse(pointer.package_wheel_per_report(self.root / "missing"))

    def test_package_mixing_rejected(self):
        machines.verify_package(self.pkg)
        (self.pkg / "share/qemu/openbios-ppc").write_bytes(b"wrong")
        with self.assertRaisesRegex(MachineError, "hash mismatch"):
            machines.verify_package(self.pkg)

    def test_paths_with_commas_rejected(self):
        bad = self.root / "var/lib/retroboot/machines/bad"
        bad.mkdir()
        (bad / "disk.img").write_bytes(b"\0" * 512)
        m = machines.Machine("bad", bad, {"disk": "disk.img"})
        m2 = machines.Machine("bad", Path(str(bad) + ",x"), {"disk": "disk.img"})
        build_argv(m, self.settings, self.paths, self.display)
        with self.assertRaises(MachineError):
            build_argv(m2, self.settings, self.paths, self.display)

    def test_pixel_scale_policy(self):
        from retroboot import display as dp
        offered = {(1920, 1080): {1: (1920, 1080), 2: (960, 540)},
                   (2560, 1440): {1: (2560, 1440), 2: (1280, 720), 3: (848, 480)},
                   (3840, 2160): {1: (3840, 2160), 2: (1920, 1080), 3: (1280, 720), 4: (960, 540)},
                   (1280, 800): {1: (1280, 800)}, (1366, 768): {1: (1360, 768)},
                   (2560, 1600): {1: (2560, 1600), 2: (1280, 800), 3: (848, 533)},
                   (5120, 2880): {2: (2560, 1440), 3: (1704, 960), 4: (1280, 720)}, (800, 400): {}}
        for (w, h), want in offered.items():
            got = {c["scale"]: tuple(c["size"]) for c in dp.pixel_scale_choices(w, h) if c["available"]}
            self.assertEqual(got, want, (w, h))
            for s, size in want.items():
                disp = {"mode": [w, h, 60]}
                self.assertEqual(guest_geometry(disp, "auto", str(s)), size)
                st = dict(self.settings, **{"display.pixel_scale": str(s)})
                self.assertIn(f"-g {size[0]}x{size[1]}x32", " ".join(build_argv(self.m, st, self.paths, disp)))
        # A stored size the display can't use falls back to the nearest it offers.
        self.assertEqual(dp.effective_scale(1920, 1080, "4"), 2)
        self.assertEqual(dp.effective_scale(2560, 1440, "4"), 3)
        self.assertEqual(dp.effective_scale(1280, 800, "3"), 1)
        self.assertEqual(dp.effective_scale(5120, 2880, "1"), 2)
        self.assertEqual(dp.effective_scale(1920, 1080, "auto"), 2)
        self.assertEqual(guest_geometry({"mode": [1920, 1080, 60]}, "auto", "4"), (960, 540))
        # Fixed classic sizes: the pixel size when it fits, else the largest fit.
        self.assertEqual(dp.integer_factor(3840, 2160, 640, 480, "2"), 2)
        self.assertEqual(dp.integer_factor(3840, 2160, 640, 480, "auto"), 4)
        self.assertEqual(dp.integer_factor(1920, 1080, 1280, 1024, "2"), 1)
        self.assertEqual(dp.integer_factor(2560, 1440, 800, 600, "3"), 2)
        self.assertEqual(dp.integer_factor(1280, 800, 1280, 1024, "1"), 0)
        self.assertEqual(guest_geometry({"mode": [3840, 2160, 60]}, "640x480", "2"), (640, 480))

    def test_display_policy(self):
        for (w, h), s in {(1024, 768): 1, (1366, 768): 1, (1920, 1080): 2, (2560, 1600): 2, (3840, 2160): 4}.items():
            self.assertEqual(suggested_scale(w, h), s, (w, h))
        self.assertEqual(guest_geometry({"mode": [1366, 768, 60], "suggested_scale": 1}, "auto"), (1360, 768))
        self.assertEqual(guest_geometry({"mode": [3840, 2160, 60], "suggested_scale": 4}, "auto"), (960, 540))
        self.assertEqual(guest_geometry(None, "auto"), (1024, 768))
        self.assertEqual(guest_geometry(self.display, "1152x870"), (1152, 870))

    def test_launch_spec_and_classify(self):
        a = build_argv(self.m, self.settings, self.paths, self.display)
        spec = machines.launch_spec(self.m, a, self.paths, "sess", dict(self.settings, **{"system.timezone": "Europe/Berlin"}),
                                    "rbguest", "retroboot")
        self.assertEqual(spec["timezone"], "Europe/Berlin")
        self.assertEqual(spec["run_as"], "rbguest")
        self.assertEqual(classify("guest-shutdown", 0, False), "guest-shutdown")
        self.assertEqual(classify("guest-reset", 0, False), "guest-reset")
        self.assertEqual(classify("host-signal", 0, True), "host-signal")
        self.assertEqual(classify(None, -9, True), "host-signal")
        self.assertEqual(classify(None, 3, False), "crash")


class FrameTests(unittest.TestCase):
    def test_content_rule_and_finder(self):
        w, h = 200, 150
        black = bytes(w * h * 3)
        gray = b"\x99\x99\x99" * (w * h)
        self.assertFalse(genuine_content(w, h, black)[0])
        self.assertTrue(genuine_content(w, h, gray)[0])
        # Mac OS's startup background on a built-in LCD: a 1-pixel dither
        dither = bytes(v for y in range(h) for x in range(w) for v in ((255,) * 3 if (x + y) & 1 else (0,) * 3))
        ok, g, nb = genuine_content(w, h, dither)
        self.assertTrue(ok)
        self.assertEqual(nb, 1.0)
        self.assertFalse(finder_menu_bar(w, dither))   # the PowerBook startup dither is not the Finder
        noisy = bytes((i * 37) % 256 for i in range(w * h * 3))
        ok, g, nb = genuine_content(w, h, noisy)
        self.assertFalse(ok)
        self.assertGreater(nb, 0.25)
        menu = bytearray(b"\x66\x66\xcc" * (w * h))
        menu[: w * 20 * 3] = b"\xff\xff\xff" * (w * 20)
        menu[19 * w * 3: 20 * w * 3] = b"\x00\x00\x00" * w   # menu bar border line only
        self.assertFalse(finder_menu_bar(w, bytes(menu)))  # empty menu bar is not the Finder
        for x in range(10, 190):
            if x % 9 < 5:
                for y in range(5, 15):
                    o = (y * w + x) * 3
                    menu[o:o + 3] = b"\0\0\0"
        self.assertTrue(finder_menu_bar(w, bytes(menu)))
        self.assertGreater(changed_fraction((w, h, bytes(menu)), (w, h, gray)), 0.5)
        self.assertEqual(changed_fraction((w, h, gray), (w, h, gray)), 0.0)
        png = encode_png(w, h, gray)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")


def make_archive(path: Path, members: list[tuple[str, bytes | int, str]]) -> None:
    """members: (name, data bytes or zero-length int, type: 'file'|'symlink'|'dir')."""
    with tarfile.open(path, "w:gz", format=tarfile.GNU_FORMAT) as tf:
        for name, data, typ in members:
            ti = tarfile.TarInfo(name)
            if typ == "symlink":
                ti.type = tarfile.SYMTYPE
                ti.linkname = "/etc/passwd"
                tf.addfile(ti)
            elif typ == "dir":
                ti.type = tarfile.DIRTYPE
                tf.addfile(ti)
            else:
                if isinstance(data, int):
                    ti.size = data
                    tf.addfile(ti, io.BytesIO(bytes(data)))
                else:
                    ti.size = len(data)
                    tf.addfile(ti, io.BytesIO(data))


class CatalogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        self.entry = {"id": "mac-os-9.2.1-test", "name": "Mac OS 9.2.1", "summary": "Test", "osVersion": "Mac OS 9.2.1",
                      "gxMetalVersion": "2.3.0", "minimumAppVersion": "3.0.0",
                      "archiveURL": "https://example.invalid/a.tar.gz", "archiveBytes": 100,
                      "installedBytes": 4 * 1024 * 1024 + 100, "sha256": "0" * 64}

    def test_catalog_validation(self):
        good = catalog.validate_catalog({"schemaVersion": 1, "machines": [self.entry]})
        self.assertEqual(good[0]["id"], self.entry["id"])
        for patch in ({"archiveURL": "http://example.invalid/a"}, {"archiveURL": "https://u:p@x/a"},
                      {"sha256": "XYZ"}, {"id": ".hidden"}, {"archiveBytes": 0},
                      {"diskCapacityBytes": 513}, {"requiredStorageBytes": 1024 * 1024}):
            with self.subTest(patch=patch):
                with self.assertRaises(catalog.ImportError_):
                    catalog.validate_catalog({"schemaVersion": 1, "machines": [dict(self.entry, **patch)]})
        with self.assertRaises(catalog.ImportError_):
            catalog.validate_catalog({"schemaVersion": 2, "machines": []})
        with self.assertRaises(catalog.ImportError_):
            catalog.validate_catalog({"schemaVersion": 1, "machines": [self.entry, self.entry]})

    def test_safe_extraction_rejects_hostile_archives(self):
        cfg = json.dumps({"name": "Mac", "machineFamily": "powerMacG4"}).encode()
        cases = {
            "traversal": [("config.json", cfg, "file"), ("../disk.img", 512, "file")],
            "absolute": [("config.json", cfg, "file"), ("/disk.img", 512, "file")],
            "symlink": [("config.json", cfg, "file"), ("disk.img", 0, "symlink")],
            "directory": [("disk.img", 0, "dir")],
            "extra": [("config.json", cfg, "file"), ("disk.img", 512, "file"), ("evil.sh", b"x", "file")],
            "duplicate": [("config.json", cfg, "file"), ("disk.img", 512, "file"), ("disk.img", 512, "file")],
            "missing": [("config.json", cfg, "file")],
            "odd-size": [("config.json", cfg, "file"), ("disk.img", 513, "file")],
            "big-config": [("config.json", b" " * (65 * 1024), "file"), ("disk.img", 512, "file")],
        }
        for name, members in cases.items():
            with self.subTest(case=name):
                arc = self.root / f"{name}.tar.gz"
                make_archive(arc, members)
                staging = self.root / f"st-{name}"
                with self.assertRaises(catalog.ImportError_):
                    catalog.extract_archive(arc, staging, catalog.ExtractLimits())

    async def test_install_machine_sparse_and_registry(self):
        disk = bytearray(4 * 1024 * 1024)
        disk[1024:1030] = b"HFSDAT"
        cfg = json.dumps({"name": "Mac OS 9", "machineFamily": "powerMacG4", "ramMB": 512, "secret": "x"}).encode()
        arc = self.root / "m.tar.gz"
        make_archive(arc, [("config.json", cfg, "file"), ("disk.img", bytes(disk), "file")])
        paths = Paths(self.root)
        reg = MachineRegistry(paths)
        prog = []
        mid = await catalog.install_machine(arc, reg, "Mac OS 9.2.1", "Mac OS 9.2.1", {"type": "test"},
                                            catalog.ExtractLimits(len(disk) + len(cfg)),
                                            lambda f, m: prog.append(f))
        m = reg.get(mid)
        self.assertIsNotNone(m)
        self.assertEqual(m.disk.read_bytes(), bytes(disk))
        st = m.disk.stat()
        self.assertLess(st.st_blocks * 512, len(disk) // 2)  # holes preserved
        self.assertNotIn("secret", m.meta["template"])
        self.assertIsNone(m.meta["files"]["disk.img"]["sha256"])     # the archive's hash covers it (test_thin)
        self.assertEqual(m.meta["files"]["disk.img"]["stored_bytes"], 1024 * 1024)
        # A second import never replaces the first.
        mid2 = await catalog.install_machine(arc, reg, "Mac OS 9.2.1", None, {"type": "test"},
                                             catalog.ExtractLimits(), lambda f, m: None)
        self.assertNotEqual(mid, mid2)
        self.assertEqual(sorted(reg.ids()), sorted([mid, mid2]))
        self.assertFalse([p for p in paths.machines.iterdir() if p.name.startswith(".staging")])

    def test_display_compatibility_config_validation(self):
        self.assertNotIn("directFramebuffer", catalog.sanitize_template_config(b'{}'))
        for value in (False, True):
            cfg = json.dumps({"directFramebuffer": value}).encode()
            self.assertIs(catalog.sanitize_template_config(cfg)["directFramebuffer"], value)
        for value in (0, 1, "true", "false", None, [], {}):
            with self.subTest(value=value):
                cfg = json.dumps({"directFramebuffer": value}).encode()
                with self.assertRaises(catalog.ImportError_) as cm:
                    catalog.sanitize_template_config(cfg)
                self.assertEqual(cm.exception.code, "unsafe_archive")

    async def test_display_compatibility_survives_archive_export_and_reimport(self):
        from retroboot import sparsetar
        helpers.make_package(self.root)
        cfg = json.dumps({"name": "Deluxe", "directFramebuffer": True}).encode()
        archive = self.root / "deluxe.tar.gz"
        make_archive(archive, [("config.json", cfg, "file"), ("disk.img", 4096, "file")])
        paths = Paths(self.root)
        paths.ensure()
        reg = MachineRegistry(paths)

        async def install(arc):
            mid = await catalog.install_machine(arc, reg, "Deluxe", "9.2.2", {},
                                                catalog.ExtractLimits(), lambda f, m: None)
            return reg.get(mid)

        first = await install(archive)
        self.assertIs(first.meta["template"]["directFramebuffer"], True)
        self.assertEqual((first.dir / "config.json").read_bytes(), cfg)
        exported = self.root / "exported.thin.tar.gz"
        with exported.open("wb") as out:
            sparsetar.write_thin(out, [("config.json", first.dir / "config.json"), ("disk.img", first.disk)])
        imported = await install(exported)
        self.assertIs(imported.meta["template"]["directFramebuffer"], True)
        argv = build_argv(imported, schema.defaults(), paths, {"mode": [1024, 768, 60]})
        self.assertIn("VGA.untracked-vram=on", argv)
        self.assertNotIn("VGA.untracked-vram=off", argv)

    async def test_storage_budget_counts_stored_data_not_preallocation(self):
        """requiredStorageBytes bounds the data written, not st_blocks (XFS counts speculative
        preallocation past EOF while a sparse file grows; hardware imports failed at once)."""
        MiB = 1024 * 1024
        disk = bytearray(80 * MiB)
        for off in range(0, len(disk), 4 * MiB):          # 20 data chunks, the rest holes
            disk[off:off + 16] = b"HFS+ data chunk."
        cfg = json.dumps({"name": "Mac", "machineFamily": "powerMacG4"}).encode()
        arc = self.root / "m.tar.gz"
        make_archive(arc, [("config.json", cfg, "file"), ("disk.img", bytes(disk), "file")])
        real_fstat = os.fstat

        def inflated(fd):                                  # what XFS reports mid-write
            st = real_fstat(fd)
            return _Blocks(st, 8 * 1024 ** 3 // 512)
        catalog.os.fstat = inflated
        self.addCleanup(setattr, catalog.os, "fstat", real_fstat)
        reg = MachineRegistry(Paths(self.root))
        # 21 MiB stored (20 data chunks + config); a 1 MiB budget + 64 MiB slack is plenty.
        mid = await catalog.install_machine(arc, reg, "x", None, {}, catalog.ExtractLimits(None, MiB),
                                            lambda f, m: None)
        self.assertEqual(reg.get(mid).disk.read_bytes(), bytes(disk))
        # Data beyond the budget + 64 MiB is still refused.
        big = os.urandom(70 * MiB)
        arc2 = self.root / "big.tar.gz"
        make_archive(arc2, [("config.json", cfg, "file"), ("disk.img", big, "file")])
        with self.assertRaises(catalog.ImportError_) as cm:
            await catalog.install_machine(arc2, reg, "y", None, {}, catalog.ExtractLimits(None, MiB),
                                          lambda f, m: None)
        self.assertEqual(cm.exception.code, "no_space")

    async def test_mismatched_installed_bytes_rejected(self):
        cfg = json.dumps({"name": "Mac"}).encode()
        arc = self.root / "m.tar.gz"
        make_archive(arc, [("config.json", cfg, "file"), ("disk.img", 4096, "file")])
        reg = MachineRegistry(Paths(self.root))
        with self.assertRaises(catalog.ImportError_):
            await catalog.install_machine(arc, reg, "x", None, {}, catalog.ExtractLimits(4096 + len(cfg) + 1),
                                          lambda f, m: None)
        self.assertEqual(reg.ids(), set())

    async def test_https_download_resume_and_integrity(self):
        from aiohttp import web
        payload = os.urandom(300_000)
        sha = hashlib.sha256(payload).hexdigest()
        hits = []

        async def handler(request):
            hits.append(request.headers.get("Range"))
            rng = request.headers.get("Range")
            if rng:
                start = int(rng.split("=")[1].split("-")[0])
                return web.Response(status=206, body=payload[start:],
                                    headers={"Content-Range": f"bytes {start}-{len(payload) - 1}/{len(payload)}"})
            return web.Response(body=payload)

        async def redirect(request):
            raise web.HTTPFound("https://elsewhere.invalid/a.tar.gz")
        app = web.Application()
        app.router.add_get("/a.tar.gz", handler)
        app.router.add_get("/r.tar.gz", redirect)
        cert, key, _ = ensure_certificate(self.root, "localhost")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=ctx)
        await site.start()
        self.addAsyncCleanup(runner.cleanup)
        port = site._server.sockets[0].getsockname()[1]
        base = f"https://127.0.0.1:{port}"
        cc = catalog.CatalogClient(base + "/catalog.json", self.root / "dl")
        cc.verify_tls = False
        entry = dict(self.entry, archiveURL=base + "/a.tar.gz", archiveBytes=len(payload), sha256=sha)
        (self.root / "dl").mkdir()
        (self.root / "dl" / f"{sha}.part").write_bytes(payload[:100_000])
        path = await cc.download(entry, lambda f, m: None)
        self.assertEqual(path.read_bytes(), payload)
        self.assertEqual(hits[-1], "bytes=100000-")
        bad = dict(entry, sha256="1" * 64)
        with self.assertRaises(catalog.ImportError_) as cm:
            await cc.download(bad, lambda f, m: None)
        self.assertEqual(cm.exception.code, "integrity")
        with self.assertRaises(catalog.ImportError_) as cm:
            await cc.download(dict(entry, archiveURL=base + "/r.tar.gz", sha256="2" * 64), lambda f, m: None)
        self.assertEqual(cm.exception.code, "redirect_rejected")


    async def test_catalog_fetch_reads_fragmented_response_and_bounds_size(self):
        """An early TCP chunk is not a complete catalog; retain the size bound."""
        from aiohttp import web
        from unittest.mock import patch
        body = json.dumps({"schemaVersion": 1, "machines": [self.entry]}).encode()

        async def listing(request):
            response = web.StreamResponse(headers={"Content-Type": "application/json"})
            await response.prepare(request)
            await response.write(body[:19])
            await asyncio.sleep(0.03)
            await response.write(body[19:])
            await response.write_eof()
            return response

        app = web.Application()
        app.router.add_get("/catalog.json", listing)
        cert, key, _ = ensure_certificate(self.root, "localhost")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=ctx)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        client = catalog.CatalogClient(f"https://127.0.0.1:{port}/catalog.json", self.root / "dl")
        client.verify_tls = False
        entries = await client.fetch()
        self.assertEqual([entry["id"] for entry in entries], [self.entry["id"]])
        with patch.object(catalog, "MAX_CATALOG", len(body) - 1):
            with self.assertRaises(catalog.ImportError_) as caught:
                await client.fetch(max_age=0)
        self.assertEqual(caught.exception.code, "invalid_catalog")
        self.assertEqual(str(caught.exception), "The catalog is too large.")

    async def test_lab_catalog_ca_file(self):
        """hostd.json catalog_ca_file (lab only) trusts a LAN catalog's CA besides the system CAs."""
        from aiohttp import web
        ca_pem, cert_pem, key_pem = lab_ca_and_server_cert("localhost")
        for name, data in (("ca.pem", ca_pem), ("srv.pem", cert_pem), ("srv.key", key_pem)):
            (self.root / name).write_bytes(data)
        payload = os.urandom(50_000)
        sha = hashlib.sha256(payload).hexdigest()
        async def archive(request):
            return web.Response(body=payload)

        async def listing(request):
            return web.Response(text=catalog_body)
        app = web.Application()
        app.router.add_get("/a.tar.gz", archive)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.root / "srv.pem", self.root / "srv.key")
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=ctx)
        await site.start()
        self.addAsyncCleanup(runner.cleanup)
        port = site._server.sockets[0].getsockname()[1]
        catalog_body = json.dumps({"schemaVersion": 1, "machines": [dict(
            self.entry, archiveURL=f"https://localhost:{port}/a.tar.gz", archiveBytes=len(payload), sha256=sha)]})
        app2 = web.Application()   # the catalog lives on the same server in the lab; a second site is simpler here
        app2.router.add_get("/catalog.json", listing)
        runner2 = web.AppRunner(app2)
        await runner2.setup()
        site2 = web.TCPSite(runner2, "127.0.0.1", 0, ssl_context=ctx)
        await site2.start()
        self.addAsyncCleanup(runner2.cleanup)
        url = f"https://localhost:{site2._server.sockets[0].getsockname()[1]}/catalog.json"
        # Without the lab CA the server is not trusted (verification stays on by default).
        plain = catalog.CatalogClient(url, self.root / "dl0")
        with self.assertRaises(catalog.ImportError_) as cm:
            await plain.fetch()
        self.assertEqual(cm.exception.code, "network")
        lab = catalog.CatalogClient(url, self.root / "dl", ca_file=str(self.root / "ca.pem"))
        entries = await lab.fetch()
        path = await lab.download(entries[0], lambda f, m: None)
        self.assertEqual(path.read_bytes(), payload)
        # Off by default.
        self.assertIsNone(Config().catalog_ca_file)

    async def test_extraction_keeps_free_space(self):
        """An archive without an advertised size (upload, USB) stops before RBDATA runs out of space."""
        disk = os.urandom(3 * 1024 * 1024)
        cfg = json.dumps({"name": "Mac", "machineFamily": "powerMacG4"}).encode()
        arc = self.root / "m.tar.gz"
        make_archive(arc, [("config.json", cfg, "file"), ("disk.img", disk, "file")])
        reg = MachineRegistry(Paths(self.root))
        real = catalog._free_bytes
        catalog._free_bytes = lambda path: catalog.RESERVE - 1
        self.addCleanup(setattr, catalog, "_free_bytes", real)
        with self.assertRaises(catalog.ImportError_) as cm:
            await catalog.install_machine(arc, reg, "x", None, {}, catalog.ExtractLimits(), lambda f, m: None)
        self.assertEqual(cm.exception.code, "no_space")
        self.assertEqual(reg.ids(), set())
        self.assertFalse([p for p in Paths(self.root).machines.iterdir() if p.name.startswith(".staging")])
        catalog._free_bytes = real
        mid = await catalog.install_machine(arc, reg, "x", None, {}, catalog.ExtractLimits(), lambda f, m: None)
        self.assertEqual(reg.get(mid).disk.read_bytes(), disk)


class ConfigOverlayTests(unittest.TestCase):
    """Per-device configuration on RBDATA (hostd.local.json): survives updates, read leniently."""

    def setUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        self.paths = Paths(self.root)
        self.paths.etc.mkdir(parents=True, exist_ok=True)
        self.paths.hostd_local_json.parent.mkdir(parents=True, exist_ok=True)

    def test_overlay_over_etc_and_unknown_keys(self):
        self.paths.hostd_json.write_text(json.dumps({"lab_qmp": True, "webproxy_port": 9080}))
        self.paths.hostd_local_json.write_text(json.dumps({"webproxy_port": 9180, "key_from_a_newer_build": 1}))
        cfg = Config.load(root=str(self.root))
        self.assertEqual((cfg.lab_qmp, cfg.webproxy_port), (True, 9180))
        self.assertTrue(any("key_from_a_newer_build" in n for n in cfg.config_notes))
        self.paths.hostd_local_json.write_text("{not json")
        cfg = Config.load(root=str(self.root))
        self.assertEqual(cfg.webproxy_port, 9080)
        self.assertTrue(cfg.config_notes)
        # An explicit --config file is used alone (development and tests).
        other = self.root / "other.json"
        other.write_text(json.dumps({"webproxy_port": 7000}))
        self.paths.hostd_local_json.write_text(json.dumps({"webproxy_port": 9180}))
        self.assertEqual(Config.load(root=str(self.root), config_file=str(other)).webproxy_port, 7000)

    def test_migration_moves_device_keys_once(self):
        from retroboot.config import migrate_etc_overlay
        self.paths.hostd_json.write_text(json.dumps({
            "lab_http_auth_file": "/var/lib/retroboot/lab/http-auth.env",   # device key: moved
            "webproxy_port": 8080,                                          # equals the default: stays
            "catalog_url": "https://lab.example/catalog.json"}))            # already in the overlay: overlay wins
        self.paths.hostd_local_json.write_text(json.dumps({"catalog_url": "https://other.example/c.json"}))
        cfg = Config.load(root=str(self.root), migrate=True)
        local = json.loads(self.paths.hostd_local_json.read_text())
        self.assertEqual(local, {"catalog_url": "https://other.example/c.json",
                                 "lab_http_auth_file": "/var/lib/retroboot/lab/http-auth.env"})
        self.assertEqual(cfg.catalog_url, "https://other.example/c.json")
        self.assertEqual(oct(self.paths.hostd_local_json.stat().st_mode & 0o777), "0o600")
        # Once per /etc content: a key removed from the overlay later is not brought back.
        self.paths.hostd_local_json.write_text(json.dumps({}))
        self.assertEqual(migrate_etc_overlay(self.paths), [])
        # An update replaced /etc (lab file gone): the overlay alone keeps the device's keys.
        self.paths.hostd_local_json.write_text(json.dumps(local))
        self.paths.hostd_json.unlink()
        cfg = Config.load(root=str(self.root), migrate=True)
        self.assertEqual(cfg.lab_http_auth_file, "/var/lib/retroboot/lab/http-auth.env")

    def test_image_defaults_are_not_migrated(self):
        from retroboot.config import migrate_etc_overlay
        self.paths.hostd_image_json.parent.mkdir(parents=True, exist_ok=True)
        self.paths.hostd_image_json.write_text(json.dumps({"form_factor": "portable"}))
        self.paths.hostd_json.write_text(json.dumps({"form_factor": "portable", "lab_qmp": True}))
        self.assertEqual(migrate_etc_overlay(self.paths), ["lab_qmp"])


class LabAuthTests(unittest.IsolatedAsyncioTestCase):
    """hostd.json lab_http_auth_file (LAB ONLY): Basic auth only under the lab base URL."""

    def setUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)

    def write(self, text, mode=0o600):
        p = self.root / "lab-auth.env"
        p.write_text(text)
        os.chmod(p, mode)
        return p

    def test_load_and_scope(self):
        from retroboot import labauth
        good = "RB_LAB_HTTP_USER=labuser\nRB_LAB_HTTP_PASS='s3cr:et'\nRB_LAB_BASE_URL=https://files.example/lab/\n"
        self.assertIsNone(labauth.load(None))
        self.assertIsNone(labauth.load(self.write(good, 0o644)))                 # too open
        self.assertIsNone(labauth.load(self.write("RB_LAB_HTTP_USER=u\n")))     # incomplete
        self.assertIsNone(labauth.load(self.write(good.replace("https", "http"))))
        self.assertIsNone(labauth.load(self.root / "missing.env"))
        auth = labauth.load(self.write(good))
        self.assertNotIn("s3cr", repr(auth))
        self.assertNotIn("labuser", repr(auth))
        want = {"Authorization": "Basic " + base64.b64encode(b"labuser:s3cr:et").decode()}
        self.assertEqual(auth.headers_for("https://files.example/lab/catalog.json"), want)
        self.assertEqual(auth.headers_for("https://FILES.example:443/lab/a/b.tar.gz"), want)
        for url in ("https://files.example/labx/catalog.json", "https://files.example/other/x",
                    "http://files.example/lab/catalog.json", "https://files.example:8443/lab/x",
                    "https://evil.example/lab/x", "https://files.example/lab/../x",
                    "https://u:p@files.example/lab/x", "https://files.example/lab"):
            with self.subTest(url=url):
                self.assertEqual(auth.headers_for(url), {})
        self.assertEqual(labauth.headers_for(None, "https://files.example/lab/x"), {})
        self.assertIsNone(Config().lab_http_auth_file)

    async def test_catalog_and_archive_with_basic_auth(self):
        from aiohttp import web
        from retroboot import labauth
        ca_pem, cert_pem, key_pem = lab_ca_and_server_cert("localhost")
        for name, data in (("ca.pem", ca_pem), ("srv.pem", cert_pem), ("srv.key", key_pem)):
            (self.root / name).write_bytes(data)
        payload = os.urandom(40_000)
        sha = hashlib.sha256(payload).hexdigest()
        token = "Basic " + base64.b64encode(b"lab:pw").decode()
        seen = []
        state = {}

        async def handler(request):
            seen.append((request.path, request.headers.get("Authorization")))
            if request.path.startswith("/pub/"):
                return web.Response(body=payload)
            if request.headers.get("Authorization") != token:
                return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="lab"'})
            if request.path == "/lab/catalog.json":
                return web.Response(text=state["catalog"])
            return web.Response(body=payload)
        app = web.Application()
        app.router.add_get("/{tail:.*}", handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.root / "srv.pem", self.root / "srv.key")
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=ctx)
        await site.start()
        self.addAsyncCleanup(runner.cleanup)
        base = f"https://localhost:{site._server.sockets[0].getsockname()[1]}"
        entry = {"id": "m", "name": "Mac", "summary": "S", "osVersion": "9.2.1", "archiveBytes": len(payload),
                 "installedBytes": 4096, "sha256": sha}
        state["catalog"] = json.dumps({"schemaVersion": 1, "machines": [
            dict(entry, archiveURL=f"{base}/lab/m.tar.gz"), dict(entry, id="p", archiveURL=f"{base}/pub/p.tar.gz")]})
        ca = str(self.root / "ca.pem")
        # Without the lab auth file the server refuses (HTTP 401).
        with self.assertRaises(catalog.ImportError_):
            await catalog.CatalogClient(f"{base}/lab/catalog.json", self.root / "dl0", ca_file=ca).fetch()
        auth = labauth.LabAuth(f"{base}/lab/", "lab", "pw")
        cc = catalog.CatalogClient(f"{base}/lab/catalog.json", self.root / "dl", ca_file=ca, auth=auth)
        entries = await cc.fetch()
        self.assertEqual((await cc.download(entries[0], lambda f, m: None)).read_bytes(), payload)
        seen.clear()
        cc2 = catalog.CatalogClient(f"{base}/lab/catalog.json", self.root / "dl2", ca_file=ca, auth=auth)
        self.assertEqual((await cc2.download(entries[1], lambda f, m: None)).read_bytes(), payload)
        self.assertEqual(seen, [("/pub/p.tar.gz", None)])        # outside the lab base: no credentials


class _Blocks:
    """An os.stat_result stand-in with a different st_blocks."""

    def __init__(self, st, blocks):
        self._st, self.st_blocks = st, blocks

    def __getattr__(self, name):
        return getattr(self._st, name)


def lab_ca_and_server_cert(hostname: str) -> tuple[bytes, bytes, bytes]:
    """A throwaway lab CA and a server certificate it signed (PEM: ca, cert, key)."""
    import datetime as dt
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    now = dt.datetime.now(dt.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "RetroBoot lab test CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
          .not_valid_after(now + dt.timedelta(days=30))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                       data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                       crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]))
            .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    pem = serialization.Encoding.PEM
    return (ca.public_bytes(pem), cert.public_bytes(pem),
            key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))


class PairingTests(unittest.TestCase):
    def setUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        self.now = 1000.0
        self.p = PairingService(self.root, clock=lambda: self.now)

    def test_code_expiry_and_pairing(self):
        code, left = self.p.current_code()
        self.assertEqual(len(code), 6)
        self.assertAlmostEqual(left, CODE_TTL)
        self.now += CODE_TTL + 1
        with self.assertRaises(PairingError) as cm:
            self.p.pair(code, "Phone", "10.0.0.2")
        self.assertEqual(cm.exception.code, "invalid_code")
        code, _ = self.p.current_code()
        dev, token = self.p.pair(code, "Phone", "10.0.0.2")
        self.assertEqual(self.p.device_for_token(token)["id"], dev["id"])
        self.assertFalse(self.p.code_active())  # single use
        stored = json.loads((self.root / "pairing.json").read_text())
        self.assertNotIn(token, json.dumps(stored))
        self.assertEqual(oct((self.root / "pairing.json").stat().st_mode & 0o777), "0o600")
        csrf = self.p.csrf_for(token)
        self.assertTrue(self.p.check_csrf(token, csrf))
        self.assertFalse(self.p.check_csrf(token, "0" * 64))
        self.assertTrue(self.p.revoke(dev["id"]))
        self.assertIsNone(self.p.device_for_token(token))

    def test_rate_limit(self):
        code, _ = self.p.current_code()
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(5):
            with self.assertRaises(PairingError):
                self.p.pair(wrong, "x", "10.0.0.9")
        with self.assertRaises(PairingError) as cm:
            self.p.pair(code, "x", "10.0.0.9")
        self.assertEqual(cm.exception.status, 429)
        dev, _ = self.p.pair(code, "y", "10.0.0.10")  # other clients unaffected
        self.assertTrue(dev["id"])

    def test_cert_and_qr(self):
        cert, key, fp = ensure_certificate(self.root, "retroboot")
        self.assertRegex(fp, r"^([0-9A-F]{2}:){31}[0-9A-F]{2}$")
        self.assertEqual(ensure_certificate(self.root, "retroboot")[2], fp)  # stable
        self.assertEqual(oct(key.stat().st_mode & 0o777), "0o600")
        m = qr_matrix("https://192.168.1.20/")
        self.assertTrue(m and all(set(r) <= {"0", "1"} for r in m) and len(m) == len(m[0]))

    def test_redactor(self):
        r = Redactor()
        r.add("123456")
        text = r('psk=hunter2 "password": "abc" Cookie: rb_session=tok; code 123456')
        self.assertNotIn("hunter2", text)
        self.assertNotIn("abc", text)
        self.assertNotIn("tok", text)
        self.assertNotIn("123456", text)
        self.assertEqual(r.obj({"wifi": {"psk": "x"}, "ok": 1}), {"wifi": {"psk": "[REDACTED]"}, "ok": 1})


class GuestPrepAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        self.disk = self.root / "disk.img"
        with open(self.disk, "wb") as f:
            f.truncate(1024 * 1024)

    async def test_apply_swaps_copy_on_success_only(self):
        mod = helpers.FakePrepModule()
        gp = GuestPrep(Path("/nonexistent"), module=mod)
        res = await gp.apply(self.disk)
        self.assertEqual(res["result"], "applied")
        self.assertEqual(mod.calls[0][0], "apply")
        self.assertNotEqual(mod.calls[0][1], str(self.disk))  # ran on a copy
        self.assertIn(b"PREPARED", self.disk.read_bytes())
        self.assertNotIn("image", res["report"])
        mod2 = helpers.FakePrepModule(fail_apply=True)
        disk2 = self.root / "disk2.img"
        disk2.write_bytes(b"ORIGINAL" + bytes(8192))
        res = await GuestPrep(Path("/nonexistent"), module=mod2).apply(disk2)
        self.assertEqual(res["result"], "failed")
        self.assertIn("PrepError", res["error"])
        self.assertTrue(disk2.read_bytes().startswith(b"ORIGINAL"))
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith(".")], [])

    async def test_repair_and_missing_library(self):
        mod = helpers.FakePrepModule()
        res = await GuestPrep(Path("/nonexistent"), module=mod).repair(self.disk)
        self.assertEqual(res["result"], "ok")
        self.assertEqual(mod.calls, [("fsck", str(self.disk), True)])
        res = await GuestPrep(self.root / "no-such-lib").repair(self.disk)
        self.assertEqual(res["result"], "unavailable")


if __name__ == "__main__":
    unittest.main()


class ContentDeadline(unittest.TestCase):
    """supervisor.content_deadline: learned from the machine's own times to the gray."""

    def test_values(self):
        from retroboot.supervisor import content_deadline
        cd = lambda h: content_deadline(h, cap=60.0, factor=3.0, floor=20.0)  # noqa: E731
        self.assertEqual(cd(None), 60.0)                        # no history: the old fixed deadline
        self.assertEqual(cd([]), 60.0)
        self.assertEqual(cd([6.6, 6.8, 6.7]), 20.1)             # Ryzen mini PC: median 6.7 s
        self.assertEqual(cd([7.7, 8.5, 9.4, 7.3]), 24.3)        # loaded lab builder: median 8.1 s
        self.assertEqual(cd([7.0, 7.1, 14.9]), 29.8)            # one slow boot: twice the slowest
        self.assertEqual(cd([25.0, 27.0, 30.0]), 60.0)          # N4100 laptop (~25-30 s): the cap
        self.assertEqual(cd([3.0]), 20.0)                       # floor
        self.assertEqual(cd([7.0, "x", True, -1, 99.0]), 21.0)  # junk and >= cap ignored
        self.assertEqual(content_deadline([7.0], cap=6.0, factor=3, floor=20), 6.0)
        self.assertEqual(content_deadline(None, cap=2.5, factor=3, floor=20), 2.5)

    def test_disk_stamp(self):
        import tempfile
        from retroboot.supervisor import disk_stamp
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "d.img"
            p.write_bytes(bytes(4096))
            a = disk_stamp(p)
            self.assertEqual(a, disk_stamp(p))
            with open(p, "r+b") as f:
                f.seek(100)
                f.write(b"x")
            os.utime(p, ns=(a[0] + 1000, a[0] + 1000))   # mtime moves on any write
            self.assertNotEqual(a, disk_stamp(p))
            self.assertIsNone(disk_stamp(Path(td) / "missing"))


class PresenterFirstConnect(unittest.IsolatedAsyncioTestCase):
    """hostboot: hostd starts beside the presenter, so until the first connection it
    retries its socket at a short fixed interval instead of backing off to seconds."""

    async def test_connects_promptly_when_the_presenter_appears_late(self):
        from fakes.fake_presenter import FakePresenter
        from retroboot.presenter import PresenterClient
        td, root = helpers.scratch()
        self.addCleanup(td.cleanup)
        paths = Paths(root)
        paths.ensure()
        client = PresenterClient(paths.presenter_sock)
        client.start()
        self.addAsyncCleanup(client.stop)
        await asyncio.sleep(0.75)             # the old backoff would next retry at 1.5 s
        fp = FakePresenter(str(paths.presenter_sock), str(paths.display_sock))
        await fp.start()
        self.addAsyncCleanup(fp.stop)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await asyncio.wait_for(client.connected.wait(), 2.0)
        self.assertLess(loop.time() - t0, 0.3)

    async def test_package_cache_verifies_once_across_threads(self):
        td, root = helpers.scratch()
        self.addCleanup(td.cleanup)
        pkg = helpers.make_package(root)
        cache = machines.PackageCache(pkg)
        calls = []
        real = machines.verify_package

        def counting(*a, **kw):
            calls.append(1)
            return real(*a, **kw)
        machines.verify_package = counting
        try:
            await asyncio.gather(*(asyncio.to_thread(cache.verify) for _ in range(4)))
        finally:
            machines.verify_package = real
        self.assertEqual(len(calls), 1)
