"""OS X profiles must never reach classic disk/driver or readiness shortcuts."""
import asyncio
import json
import struct
import unittest
import hashlib
import zlib
import shutil
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest import mock

import helpers
from retroboot import catalogs, graphics, osx, schema
from retroboot.catalog import ExtractLimits, ImportError_, install_machine, sanitize_template_config, validate_entry
from retroboot.config import Paths
from retroboot.machines import (MachineError, MachineRegistry, MachineUnavailable, OSX_PROFILES,
                                LaunchOptions, build_argv, family_fields, validate_meta)
from retroboot.startup import StartupProgress
from retroboot.supervisor import Supervisor
from retroboot.checkpoints import CheckpointError, Checkpoints


def nvram_seed():
    """An original empty flash bank, independently built for validation tests."""
    data = bytearray(16384)
    data[8192] = 0x5a
    struct.pack_into(">H", data, 8194, 2)
    data[8196:8208] = b"wwwwwwwwwwww"
    total = sum(data[8192:8208])
    while total > 255:
        total = (total & 255) + (total >> 8)
    data[8193] = total
    for offset, signature, length, name in ((8192 + 32, 0x70, 0xc10, b"common"),
                                            (8192 + 0xc30, 0xa0, 0x510, b"APL,MacOS75"),
                                            (8192 + 0x1140, 0x7f, 0xec0, b"wwwwwwwwwwww")):
        header = bytearray(struct.pack(">BBH12s", signature, 0, length // 16, name))
        checksum = sum(header)
        while checksum > 255:
            checksum = (checksum & 255) + (checksum >> 8)
        header[1] = checksum
        data[offset:offset + 16] = header
    struct.pack_into(">I", data, 8212, 2)
    struct.pack_into(">I", data, 8208, zlib.adler32(data[8212:], 1))
    return bytes(data)


class Profiles(unittest.TestCase):
    def test_osx_memory_selection_keeps_automatic_and_bounds_explicit_sizes(self):
        m = self.mac()
        original = {**schema.defaults(), "machine.ram_mb": 768}
        for selected, expected in (("auto", 768), (1024, 1024), (1536, 1536), (2048, 2048)):
            with self.subTest(selected=selected):
                settings = {**original, "machine.osx_ram_mb": selected}
                args = build_argv(m, settings, self.paths, None, LaunchOptions())
                self.assertEqual(args[args.index("-m") + 1], str(expected))
                self.assertEqual(settings["machine.ram_mb"], 768)
        for invalid in (True, False, 128, 2049, 2048.0, "2048", None):
            with self.subTest(invalid=invalid), self.assertRaises(MachineError):
                build_argv(m, {**original, "machine.osx_ram_mb": invalid}, self.paths, None, LaunchOptions())

    def setUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        self.paths = Paths(self.root)
        self.paths.ensure()
        self.reg = MachineRegistry(self.paths)
        classic = helpers.make_package(self.root)
        self.pkg = self.paths.qemu_osx_package
        shutil.copytree(classic, self.pkg)
        manifest = json.loads((self.pkg / "manifest.json").read_text())
        manifest["features"] = [osx.FEATURE, osx.QEMU_FEATURE, osx.NVRAM_FEATURE, osx.FIRMWARE_FEATURE,
                                  "display-retroboot"]
        manifest["sources"]["qemu"] = dict(osx.QEMU_SOURCE, tag="v10.0.13")
        (self.pkg / osx.FIRMWARE).write_bytes(b"original OS X firmware fixture")
        manifest["files"][osx.FIRMWARE] = {"sha256": hashlib.sha256((self.pkg / osx.FIRMWARE).read_bytes()).hexdigest()}
        seed = nvram_seed()
        (self.pkg / osx.NVRAM_SEED).write_bytes(seed)
        manifest["files"][osx.NVRAM_SEED] = {"sha256": hashlib.sha256(seed).hexdigest()}
        (self.pkg / "manifest.json").write_text(json.dumps(manifest))

    def mac(self, profile="macosx-10.4"):
        m = self.reg.create_blank("Mac OS X", 1024 ** 3, guest_os=profile)
        osx.prepare(m, self.pkg)
        return m

    def test_checkpoint_records_the_selected_osx_runtime(self):
        from retroboot.hostd import Hostd
        from unittest.mock import Mock
        app = Hostd.__new__(Hostd)
        m = self.mac()
        app.machines = self.reg
        app.supervisor = SimpleNamespace(active=False)
        app.settings = SimpleNamespace(applied={}, revision=1)
        app.package = SimpleNamespace(summary=Mock(return_value={"files": {"classic": "wrong"}}))
        app.package_osx = SimpleNamespace(summary=Mock(return_value={"files": {"osx": "correct"}}))
        app.checkpoints = SimpleNamespace(create=AsyncMock(return_value={"id": "test", "method": "copy"}))
        app.lifecycle = SimpleNamespace(event=Mock())
        job = SimpleNamespace(update=Mock())
        app.jobs = SimpleNamespace(start=lambda kind, body, **kw: body(job))
        asyncio.run(app.checkpoint_job(m.id, "OS X"))
        self.assertEqual(app.checkpoints.create.await_args.args[-1], {"osx": "correct"})
        app.package.summary.assert_not_called()

    def test_installer_media_style_requires_only_canonical_osx_macs(self):
        from retroboot.startup import embedded_style
        m = self.mac()
        embedded = self.paths.installer_dir / "embedded"
        self.assertEqual(embedded_style(embedded), "classic")
        dest = embedded / "machines" / m.id
        dest.mkdir(parents=True)
        (dest / "machine.json").write_text(json.dumps(m.meta))
        self.assertEqual(embedded_style(embedded), "macosx")
        archive = embedded / "unknown.tar.gz"
        archive.write_bytes(b"unknown identity")
        self.assertEqual(embedded_style(embedded), "classic")
        archive.unlink()
        classic = embedded / "machines" / "classic"
        classic.mkdir()
        (classic / "machine.json").write_text(json.dumps({"schema_version": 1, "id": "classic",
                                                          "name": "Classic", "disk": "disk.img"}))
        self.assertEqual(embedded_style(embedded), "classic")
        shutil.rmtree(classic)
        (dest / "machine.json").write_text(json.dumps({**m.meta, "guest_os": "macosx-10.5"}))
        self.assertEqual(embedded_style(embedded), "classic")

    def test_installer_selection_overrides_the_known_media_style(self):
        m = self.mac()
        app = SimpleNamespace(mode="installer", paths=self.paths,
                              panel=SimpleNamespace(st=SimpleNamespace(plan=[])))
        embedded = self.paths.installer_dir / "embedded/machines" / m.id
        embedded.mkdir(parents=True)
        (embedded / "machine.json").write_text(json.dumps(m.meta))
        progress = StartupProgress(app)
        self.assertEqual(progress.style(), "macosx")
        app.panel.st.plan = [{"startup": True, "choice": SimpleNamespace(kind="archive")}]
        self.assertEqual(progress.style(), "classic")
        app.panel.st.plan = [{"startup": True, "choice": SimpleNamespace(kind="dir", path=m.dir)}]
        self.assertEqual(progress.style(), "macosx")
        app.panel.st.plan = [{"startup": True, "choice": SimpleNamespace(kind="catalog", entry={
            "machineFamily": "powerMacOSX", "guestOS": "macosx-10.3"})}]
        self.assertEqual(progress.style(), "macosx")

    def test_profiles_are_preserved_and_fail_closed(self):
        for profile in OSX_PROFILES:
            raw = json.dumps({"machineFamily": "powerMacOSX", "guestOS": profile}).encode()
            template = sanitize_template_config(raw)
            self.assertEqual(template["guestOS"], profile)
            self.assertEqual(family_fields(template), {"schema_version": 3, "family": "powermac", "guest_os": profile,
                                                       "nvram": "nvram.img"})
        for value in (True, None, [], "macosx-10.5", "10.4", "macosx"):
            with self.assertRaises(ImportError_):
                sanitize_template_config(json.dumps({"guestOS": value}).encode())
        with self.assertRaises(ImportError_):
            sanitize_template_config(b'{"guestOS":"macosx-10.4","machineFamily":"quadra800"}')
        with self.assertRaises(ImportError_):
            sanitize_template_config(b'{"guestOS":"macosx-10.4","machineFamily":"powerMacG4"}')
        with self.assertRaises(ImportError_):
            sanitize_template_config(b'{"machineFamily":"powerMacOSX"}')

    def test_old_formats_remain_classic_and_osx_cannot_downgrade(self):
        self.assertEqual(family_fields({}), {})
        m = self.mac()
        self.assertTrue(m.osx)
        self.assertEqual(m.public()["guest_os"], "macosx-10.4")
        for change in ({"schema_version": 1}, {"guest_os": "classic"}, {"family": "quadra800"}):
            with self.assertRaises(MachineError):
                validate_meta({**m.meta, **change}, m.id)

    def test_paired_desktop_setup_survives_template_import_and_rejects_other_profiles(self):
        cfg = {"machineFamily": "powerMacOSX", "guestOS": "macosx-10.4",
               "osxDisplaySetup": "firmware-desktop-1"}
        self.assertEqual(sanitize_template_config(json.dumps(cfg).encode())["osxDisplaySetup"],
                         "firmware-desktop-1")
        for change in ({"guestOS": "macosx-10.3"}, {"guestOS": "macosx-10.2"},
                       {"osxDisplaySetup": "edid-only"}, {"osxDisplaySetup": True},
                       {"osxDisplaySetup": None}):
            with self.subTest(change=change), self.assertRaises(ImportError_):
                sanitize_template_config(json.dumps({**cfg, **change}).encode())

    def test_catalog_gate_and_exact_archive_import(self):
        from test_quadra import archive
        entry = validate_entry({"id": "tiger", "name": "Tiger", "summary": "Mac OS X 10.4.6",
                                "osVersion": "10.4.6", "machineFamily": "powerMacOSX", "guestOS": "macosx-10.4",
                                "archiveURL": "https://x.test/tiger.tar.gz", "archiveBytes": 100,
                                "installedBytes": 2 * 1024 * 1024, "sha256": "ab" * 32})
        self.assertEqual(catalogs.entry_family(entry), "powermac-osx")
        self.assertEqual(catalogs.runnable([("lab", [entry])], {"powermac"}), [("lab", [])])
        self.assertIn("powermac-osx", catalogs.runnable_families(self.paths))
        config = json.dumps({"machineFamily": "powerMacOSX", "guestOS": "macosx-10.4",
                             "osxDisplaySetup": "firmware-desktop-1"}).encode()
        payload = archive(self.root / "osx.tar.gz", {"config.json": config, "disk.img": bytes(1024 * 1024),
                                                    "nvram.img": nvram_seed()})
        async def install(**kwargs):
            return await install_machine(payload, self.reg, "Tiger", "10.4.6", {"type": "upload"}, ExtractLimits(),
                                         lambda f, m: None, **kwargs)
        with self.assertRaises(ImportError_) as caught:
            asyncio.run(install(allowed_families={"powermac"}))
        self.assertEqual(caught.exception.code, "unsupported")
        with self.assertRaises(ImportError_):
            asyncio.run(install(expected_family="powermac-osx", expected_guest_os="macosx-10.3"))
        mid = asyncio.run(install(allowed_families={"powermac", "powermac-osx"},
                                 expected_family="powermac-osx", expected_guest_os="macosx-10.4"))
        m = self.reg.get(mid)
        self.assertTrue(m.osx)
        self.assertEqual(m.meta["schema_version"], 3)
        self.assertEqual(m.meta["template"]["osxDisplaySetup"], "firmware-desktop-1")
        self.assertEqual(m.nvram.read_bytes(), nvram_seed())

    def test_catalog_rejects_osx_identity_before_download(self):
        entry = {"id": "tiger", "name": "Tiger", "summary": "Tiger",
                 "osVersion": "10.4.11", "osBuild": "8S165",
                 "machineFamily": "powerMacOSX", "guestOS": "macosx-10.4",
                 "archiveURL": "https://x.test/tiger.tar.gz", "archiveBytes": 100,
                 "installedBytes": 2 * 1024 * 1024, "sha256": "ab" * 32}
        self.assertEqual(validate_entry(entry)["osBuild"], "8S165")
        for change in ({"osVersion": "Mac OS X 10.4.11"}, {"osVersion": "10.3.9"},
                       {"osBuild": "../../bad"}, {"osBuild": False}):
            with self.subTest(change=change), self.assertRaises(ImportError_):
                validate_entry({**entry, **change})

    def test_invalid_import_identity_leaves_no_registered_or_staged_machine(self):
        from test_quadra import archive
        config = json.dumps({"machineFamily": "powerMacOSX", "guestOS": "macosx-10.4"}).encode()
        payload = archive(self.root / "identity.tar.gz", {"config.json": config, "disk.img": bytes(1024 * 1024),
                                                        "nvram.img": nvram_seed()})
        for version, build in (("Mac OS X 10.4.11", None), ("10.3.9", "7W98"), ("10.4.11", "bad")):
            with self.subTest(version=version, build=build), self.assertRaises(ImportError_):
                asyncio.run(install_machine(payload, self.reg, "Tiger", version, {"type": "catalog"},
                                            ExtractLimits(), lambda f, m: None, os_build=build))
            self.assertEqual(list(self.paths.machines.iterdir()), [])
        mid = asyncio.run(install_machine(payload, self.reg, "Tiger", "10.4.11", {"type": "catalog"},
                                         ExtractLimits(), lambda f, m: None, os_build="8S165"))
        self.assertEqual(self.reg.get(mid).meta["os_build"], "8S165")

    def test_import_job_refuses_missing_registered_machine_before_preparation(self):
        from retroboot.hostd import Hostd
        from retroboot.jobs import JobFailed
        from unittest.mock import Mock
        payload = self.root / "upload.tar.gz"
        payload.write_bytes(b"test")
        app = SimpleNamespace(paths=self.paths,
                              platform=SimpleNamespace(machine_unavailable=lambda family: None,
                                                       machine_families={"powermac", "quadra800"}),
                              machines=SimpleNamespace(list=lambda: [], get=lambda mid: None),
                              supervisor=SimpleNamespace(run_prep=AsyncMock()),
                              jobs=SimpleNamespace(start=lambda kind, body, **kw: body(SimpleNamespace(update=Mock()))))
        with mock.patch("retroboot.hostd.install_machine", AsyncMock(return_value="missing")):
            with self.assertRaises(JobFailed) as caught:
                asyncio.run(Hostd.import_job(app, {"source": "upload", "path": str(payload)}))
        self.assertEqual(caught.exception.code, "invalid_machine")
        app.supervisor.run_prep.assert_not_awaited()

    def test_osx_bless_field_only_counts_for_osx(self):
        m = self.mac()
        header = bytearray(512)
        header[:2] = b"H+"
        struct.pack_into(">I", header, 0x64, 42)   # blessed OS X CoreServices folder
        with m.disk.open("r+b") as f:
            f.seek(1024)
            f.write(header)
        self.assertFalse(m.needs_cd())
        from retroboot.machines import startup_volume
        self.assertEqual(startup_volume(m.disk), "volume")

    def test_conservative_native_launch_without_classic_drivers(self):
        m = self.mac()
        s = schema.defaults()
        s.update({"machine.cpu": "g3", "machine.boot_speed": "accelerated", "share.enabled": True,
                  "display.graphics": "ati-radeon", "network.guest_adapter": "airport"})
        args = build_argv(m, s, self.paths, {"mode": [1024, 768, 60]}, LaunchOptions())
        joined = " ".join(args)
        self.assertIn("mac99,via=pmu,usb=off,audiodev=snd0", args)
        self.assertEqual(args[args.index("-cpu") + 1], "7400")
        self.assertIn("macio-nvram.osx-flash=on", args)
        self.assertNotIn("macio-nvram.osx-partitions=on", args)
        self.assertIn("user,model=sungem", args)
        self.assertIn("usb-tablet,bus=osx-usb.0", args)
        self.assertNotIn("usb-mouse", joined)
        for forbidden in ("ndrvloader", "virtio-", "-icount", "ati-vga", "retroboot-airport", "retroboot-display"):
            self.assertNotIn(forbidden, joined)
        self.assertEqual(graphics.effective(s, m, self.pkg), "standard")
        self.assertEqual(args[0], str(self.paths.qemu_osx_package / "bin/qemu-system-ppc"))
        self.assertNotIn("fast-fpu", joined)
        self.assertNotIn("dma-completion-delay", joined)
        (self.pkg / "manifest.json").write_text("{}")
        with self.assertRaises(MachineUnavailable):
            build_argv(m, s, self.paths, None)

    def test_classic_package_never_substitutes_for_missing_osx_runtime(self):
        m = self.mac()
        classic = self.paths.qemu_package
        shutil.rmtree(classic)
        shutil.copytree(self.pkg, classic)
        self.assertTrue(osx.package_capable(classic))
        shutil.rmtree(self.pkg)
        self.assertNotIn("powermac-osx", catalogs.runnable_families(self.paths))
        with self.assertRaises(MachineUnavailable):
            build_argv(m, schema.defaults(), self.paths, None)

    def test_tablet_correction_requires_qualified_tiger_and_runtime(self):
        manifest_path = self.pkg / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        for profile, build, capable, expected in (
                ("macosx-10.4", "8S165", True, "usb-tablet,macosx-compat=on,bus=osx-usb.0"),
                ("macosx-10.4", "8S165", False, "usb-tablet,bus=osx-usb.0"),
                ("macosx-10.4", None, True, "usb-tablet,bus=osx-usb.0"),
                ("macosx-10.4", "8I127", True, "usb-tablet,bus=osx-usb.0"),
                ("macosx-10.3", "7W98", True, "usb-tablet,bus=osx-usb.0"),
                ("macosx-10.2", "6R73", True, "usb-mouse,bus=osx-usb.0")):
            with self.subTest(profile=profile, build=build, capable=capable):
                manifest["features"] = [x for x in manifest["features"] if x != osx.TABLET_FEATURE]
                if capable:
                    manifest["features"].append(osx.TABLET_FEATURE)
                manifest_path.write_text(json.dumps(manifest))
                m = self.mac(profile)
                m.meta["os_build"] = build
                args = build_argv(m, schema.defaults(), self.paths, None)
                self.assertIn(expected, args)
                pointers = [x for x in args if x.startswith(("usb-tablet,", "usb-mouse,"))]
                self.assertEqual(pointers, [expected])

    def test_sleep_and_codec_require_exact_builds_and_runtime_capabilities(self):
        manifest_path = self.pkg / "manifest.json"
        base = json.loads(manifest_path.read_text())
        settings = schema.defaults()
        settings["audio.enabled"] = True
        for profile, build in osx.TESTED_BUILDS.items():
            m = self.mac(profile)
            m.meta["os_build"] = build
            for features in (set(), {osx.CODEC_FEATURE}, osx.SLEEP_FEATURES,
                             osx.SLEEP_FEATURES | {osx.CODEC_FEATURE}):
                manifest = {**base, "features": base["features"] + sorted(features)}
                manifest_path.write_text(json.dumps(manifest))
                args = build_argv(m, settings, self.paths, None)
                self.assertEqual("via-pmu.osx-sleep=on" in args,
                                 profile != "macosx-10.2" and osx.SLEEP_FEATURES.issubset(features))
                self.assertEqual("screamer.osx-codec=on" in args, osx.CODEC_FEATURE in features)
                self.assertEqual("screamer.dma-lead-us=0" in args, osx.CODEC_FEATURE in features)
                self.assertEqual("out.try-poll=off" in args[args.index("-audiodev") + 1],
                                 osx.CODEC_FEATURE in features)
                self.assertEqual("threshold=40000" in args[args.index("-audiodev") + 1],
                                 osx.CODEC_FEATURE in features)
            for incomplete in ({"macosx-pmu-sleep-1"}, {"macosx-7400-dl1hwf-1"}):
                manifest_path.write_text(json.dumps({**base, "features": base["features"] + sorted(incomplete)}))
                self.assertNotIn("via-pmu.osx-sleep=on", build_argv(m, settings, self.paths, None))
            manifest_path.write_text(json.dumps({**base, "features": base["features"] +
                                                 sorted(osx.SLEEP_FEATURES | {osx.CODEC_FEATURE})}))
            m.meta["os_build"] = "unknown"
            args = build_argv(m, settings, self.paths, None)
            self.assertNotIn("via-pmu.osx-sleep=on", args)
            self.assertNotIn("screamer.osx-codec=on", args)
            self.assertNotIn("out.try-poll=off", args[args.index("-audiodev") + 1])
            self.assertNotIn("threshold=", args[args.index("-audiodev") + 1])
            m.meta["os_build"] = build
            cd = self.root / "installer.iso"
            cd.write_bytes(b"installer fixture")
            args = build_argv(m, settings, self.paths, None, LaunchOptions(boot_cd=cd))
            self.assertNotIn("via-pmu.osx-sleep=on", args)
            self.assertNotIn("screamer.osx-codec=on", args)
            self.assertNotIn("out.try-poll=off", args[args.index("-audiodev") + 1])
            self.assertNotIn("threshold=", args[args.index("-audiodev") + 1])

    def test_48k_output_is_limited_to_the_opted_in_tiger_codec(self):
        path = self.pkg / "manifest.json"
        manifest = json.loads(path.read_text())
        for profile, build, codec, optin, capable, expected in (
            ("macosx-10.4", "8S165", True, True, True, True),
            ("macosx-10.4", "8S165", True, False, True, False),
            ("macosx-10.4", "8S165", True, True, False, False),
            ("macosx-10.4", "8S165", False, True, True, False),
            ("macosx-10.4", "8I127", True, True, True, False),
            ("macosx-10.3", "7W98", True, True, True, False),
        ):
            path.write_text(json.dumps({**manifest, "features": manifest["features"] +
                                        ([osx.CODEC_FEATURE] if codec else [])}))
            machine = self.mac(profile)
            machine.meta["os_build"] = build
            args = build_argv(machine, {**schema.defaults(), "audio.enabled": True}, self.paths, None,
                              LaunchOptions(audio_capable=capable, osx_48k_output=optin))
            self.assertEqual("out.frequency=48000" in args[args.index("-audiodev") + 1], expected)

    def test_silent_host_output_preserves_qualified_native_codec(self):
        manifest_path = self.pkg / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["features"].append(osx.CODEC_FEATURE)
        manifest_path.write_text(json.dumps(manifest))
        for profile, build in osx.TESTED_BUILDS.items():
            m = self.mac(profile)
            m.meta["os_build"] = build
            for enabled, capable in ((True, True), (False, True),
                                     (True, False), (False, False)):
                with self.subTest(profile=profile, enabled=enabled, capable=capable):
                    settings = {**schema.defaults(), "audio.enabled": enabled}
                    args = build_argv(m, settings, self.paths, None,
                                      LaunchOptions(audio_capable=capable))
                    self.assertIn("screamer.osx-codec=on", args)
                    self.assertIn("screamer.dma-lead-us=0", args)
                    self.assertEqual(args[args.index("-audiodev") + 1],
                                     "alsa,id=snd0,out.try-poll=off,threshold=40000"
                                     if enabled and capable else "none,id=snd0")

    def test_keyboard_reset_state_requires_qualified_tiger_and_complete_wake_runtime(self):
        manifest_path = self.pkg / "manifest.json"
        base = json.loads(manifest_path.read_text())
        complete = osx.SLEEP_FEATURES | {osx.WAKE_HID_FEATURE}
        for profile, build, features, boot_cd, expected in (
                ("macosx-10.4", "8S165", complete, False, True),
                ("macosx-10.4", "8S165", complete, True, False),
                ("macosx-10.4", "unknown", complete, False, False),
                ("macosx-10.3", "7W98", complete, False, False),
                ("macosx-10.2", "6R73", complete, False, False),
                ("macosx-10.4", "8S165", osx.SLEEP_FEATURES, False, False),
                ("macosx-10.4", "8S165", {osx.WAKE_HID_FEATURE}, False, False)):
            with self.subTest(profile=profile, build=build, features=features, cd=boot_cd):
                manifest_path.write_text(json.dumps({**base, "features": base["features"] + sorted(features)}))
                m = self.mac(profile)
                m.meta["os_build"] = build
                cd = self.root / "keyboard-installer.iso"
                cd.write_bytes(b"installer fixture")
                args = build_argv(m, schema.defaults(), self.paths, None,
                                  LaunchOptions(boot_cd=cd if boot_cd else None))
                keyboards = [arg for arg in args if arg.startswith("usb-kbd,")]
                self.assertEqual(keyboards, ["usb-kbd,bus=osx-usb.0" +
                                            (",x-keep-keys-on-reset=on" if expected else "")])

    def test_yuv_cpu_view_requires_exact_tiger_and_runtime_capability(self):
        from retroboot import osx_graphics
        m = self.mac()
        manifest_path = self.pkg / "manifest.json"
        base = json.loads(manifest_path.read_text())
        required = {osx_graphics.FEATURE, osx_graphics.EGL_FEATURE,
                    osx_graphics.COHERENT_FEATURE, osx_graphics.I8_FEATURE} | osx.SLEEP_FEATURES
        settings = dict(schema.defaults(), **{"display.graphics": "ati-radeon"})
        gl = {"ok": True, "render_node": "/dev/dri/renderD129",
              "renderer": "virgl AMD gfx1150", "software": False}
        with mock.patch.object(osx_graphics, "_host_gl", gl):
            for profile, build, capability, expected in (
                    ("macosx-10.4", "8S165", True, True),
                    ("macosx-10.4", "8S165", False, False),
                    ("macosx-10.4", "8S164", True, False),
                    ("macosx-10.3", "7W98", True, False),
                    ("macosx-10.2", "6R73", True, False)):
                with self.subTest(profile=profile, build=build, capability=capability):
                    m.meta["guest_os"] = profile
                    m.meta["os_build"] = build
                    features = set(base["features"]) | required
                    if capability:
                        features.add(osx_graphics.YUV_VIEW_FEATURE)
                    manifest_path.write_text(json.dumps({**base, "features": sorted(features)}))
                    args = build_argv(m, settings, self.paths, None)
                    self.assertEqual("x-yuv16-aperture-view=on" in " ".join(args), expected)

    def test_depth_memory_requires_exact_tiger_and_coherent_runtime(self):
        from retroboot import osx_graphics
        m = self.mac()
        manifest_path = self.pkg / "manifest.json"
        base = json.loads(manifest_path.read_text())
        required = {osx_graphics.FEATURE, osx_graphics.EGL_FEATURE,
                    osx_graphics.COHERENT_FEATURE, osx_graphics.I8_FEATURE} | osx.SLEEP_FEATURES
        settings = dict(schema.defaults(), **{"display.graphics": "ati-radeon"})
        gl = {"ok": True, "render_node": "/dev/dri/renderD129",
              "renderer": "virgl AMD gfx1150", "software": False}
        with mock.patch.object(osx_graphics, "_host_gl", gl):
            for profile, build, capability, coherent, expected in (
                    ("macosx-10.4", "8S165", True, True, True),
                    ("macosx-10.4", "8S165", False, True, False),
                    ("macosx-10.4", "8S165", True, False, False),
                    ("macosx-10.4", "8S164", True, True, False),
                    ("macosx-10.3", "7W98", True, True, False),
                    ("macosx-10.2", "6R73", True, True, False)):
                with self.subTest(profile=profile, build=build, capability=capability,
                                  coherent=coherent):
                    m.meta.update(guest_os=profile, os_build=build)
                    features = set(base["features"]) | required
                    if capability:
                        features.add(osx_graphics.DEPTH_MEMORY_FEATURE)
                    if not coherent:
                        features.discard(osx_graphics.COHERENT_FEATURE)
                    manifest_path.write_text(json.dumps({**base, "features": sorted(features)}))
                    args = build_argv(m, settings, self.paths, None)
                    self.assertEqual("x-depth-memory=on" in " ".join(args), expected)

    def test_selective_2d_requires_exact_tiger_and_depth_capability(self):
        from retroboot import osx_graphics
        m = self.mac()
        manifest_path = self.pkg / "manifest.json"
        base = json.loads(manifest_path.read_text())
        required = {osx_graphics.FEATURE, osx_graphics.EGL_FEATURE,
                    osx_graphics.COHERENT_FEATURE, osx_graphics.I8_FEATURE,
                    osx_graphics.DEPTH_MEMORY_FEATURE} | osx.SLEEP_FEATURES
        settings = dict(schema.defaults(), **{"display.graphics": "ati-radeon"})
        gl = {"ok": True, "render_node": "/dev/dri/renderD129",
              "renderer": "virgl AMD gfx1150", "software": False}
        with mock.patch.object(osx_graphics, "_host_gl", gl):
            for profile, build, capability, coherent, expected in (
                    ("macosx-10.4", "8S165", True, True, True),
                    ("macosx-10.4", "8S165", False, True, False),
                    ("macosx-10.4", "8S165", True, False, False),
                    ("macosx-10.4", "8S164", True, True, False),
                    ("macosx-10.3", "7W98", True, True, False),
                    ("macosx-10.2", "6R73", True, True, False)):
                with self.subTest(profile=profile, build=build, capability=capability,
                                  coherent=coherent):
                    m.meta.update(guest_os=profile, os_build=build)
                    features = set(base["features"]) | required
                    if capability:
                        features.add(osx_graphics.SELECTIVE_2D_FEATURE)
                    if not coherent:
                        features.discard(osx_graphics.COHERENT_FEATURE)
                    manifest_path.write_text(json.dumps({**base, "features": sorted(features)}))
                    args = build_argv(m, settings, self.paths, None)
                    self.assertEqual("x-selective-2d-sync=on" in " ".join(args), expected)
            m.meta.update(guest_os="macosx-10.4", os_build="8S165")
            features = (set(base["features"]) | required | {osx_graphics.SELECTIVE_2D_FEATURE}) - {osx_graphics.DEPTH_MEMORY_FEATURE}
            manifest_path.write_text(json.dumps({**base, "features": sorted(features)}))
            self.assertNotIn("x-selective-2d-sync=on", " ".join(build_argv(m, settings, self.paths, None)))

    def test_ram_source_sync_requires_the_complete_tiger_chain(self):
        from retroboot import osx_graphics
        m = self.mac()
        manifest_path = self.pkg / "manifest.json"
        base = json.loads(manifest_path.read_text())
        required = {osx_graphics.FEATURE, osx_graphics.EGL_FEATURE,
                    osx_graphics.COHERENT_FEATURE, osx_graphics.I8_FEATURE,
                    osx_graphics.DEPTH_MEMORY_FEATURE, osx_graphics.SELECTIVE_2D_FEATURE,
                    osx_graphics.TEXTURE_PAGE_FEATURE, osx_graphics.RAM_SOURCE_FEATURE} | osx.SLEEP_FEATURES
        settings = dict(schema.defaults(), **{"display.graphics": "ati-radeon"})
        gl = {"ok": True, "render_node": "/dev/dri/renderD129",
              "renderer": "virgl AMD gfx1150", "software": False}
        cases = [("macosx-10.4", "8S165", None, True),
                 ("macosx-10.4", "8S164", None, False),
                 ("macosx-10.3", "7W98", None, False),
                 ("macosx-10.2", "6R73", None, False)]
        cases += [("macosx-10.4", "8S165", missing, False) for missing in (
            osx_graphics.RAM_SOURCE_FEATURE, osx_graphics.TEXTURE_PAGE_FEATURE,
            osx_graphics.SELECTIVE_2D_FEATURE, osx_graphics.DEPTH_MEMORY_FEATURE,
            osx_graphics.COHERENT_FEATURE)]
        with mock.patch.object(osx_graphics, "_host_gl", gl):
            for profile, build, missing, expected in cases:
                with self.subTest(profile=profile, build=build, missing=missing):
                    m.meta.update(guest_os=profile, os_build=build)
                    features = set(base["features"]) | required
                    if missing:
                        features.discard(missing)
                    manifest_path.write_text(json.dumps({**base, "features": sorted(features)}))
                    args = build_argv(m, settings, self.paths, None)
                    self.assertEqual("x-selective-2d-ram=on" in " ".join(args), expected)

    def test_yuv_ram_order_requires_exact_tiger_and_its_independent_chain(self):
        from retroboot import osx_graphics
        m = self.mac()
        path = self.pkg / "manifest.json"
        base = json.loads(path.read_text())
        required = {osx_graphics.FEATURE, osx_graphics.EGL_FEATURE,
                    osx_graphics.COHERENT_FEATURE, osx_graphics.I8_FEATURE,
                    osx_graphics.YUV_VIEW_FEATURE, osx_graphics.YUV_RAM_FEATURE,
                    osx_graphics.TEXTURE_PAGE_FEATURE} | osx.SLEEP_FEATURES
        settings = {**schema.defaults(), "display.graphics": "ati-radeon"}
        hardware = {"ok": True, "render_node": "/dev/dri/renderD129",
                    "renderer": "virgl AMD gfx1150", "software": False}
        cases = [("macosx-10.4", "8S165", None, True),
                 ("macosx-10.4", "8S164", None, False),
                 ("macosx-10.4", "7W98", None, False),
                 ("macosx-10.3", "7W98", None, False),
                 ("macosx-10.2", "6R73", None, False)]
        cases += [("macosx-10.4", "8S165", feature, False) for feature in required]
        with mock.patch.object(osx_graphics, "_host_gl", hardware):
            for profile, build, missing, expected in cases:
                with self.subTest(profile=profile, build=build, missing=missing):
                    m.meta.update(guest_os=profile, os_build=build)
                    features = (set(base["features"]) | required) - ({missing} if missing else set())
                    path.write_text(json.dumps({**base, "features": sorted(features)}))
                    argv = build_argv(m, settings, self.paths, None)
                    self.assertEqual("x-yuv-ram-byte-order=on" in " ".join(argv), expected)
                    # The new contract does not require selective2D/depth capabilities.
                    self.assertNotIn("x-selective-2d-ram=on", " ".join(argv))
        m.meta.update(guest_os="macosx-10.4", os_build="8S165")
        path.write_text(json.dumps({**base, "features": sorted(set(base["features"]) | required)}))
        for info in (None, {**hardware, "software": True}, {**hardware, "ok": False}):
            with self.subTest(host=info), mock.patch.object(osx_graphics, "_host_gl", info):
                self.assertNotIn("x-yuv-ram-byte-order=on", " ".join(
                    build_argv(m, settings, self.paths, None)))
        with mock.patch.object(osx_graphics, "_host_gl", hardware):
            cd = self.root / "installer.iso"
            cd.write_bytes(b"installer fixture")
            self.assertNotIn("x-yuv-ram-byte-order=on", " ".join(build_argv(
                m, settings, self.paths, None, LaunchOptions(boot_cd=cd))))

    def test_c17_full_hd_keeps_initial_size_and_exposes_the_game_timing(self):
        from retroboot import osx_graphics
        m = self.mac()
        path = self.pkg / "manifest.json"
        base = json.loads(path.read_text())
        required = {osx_graphics.FEATURE, osx_graphics.EGL_FEATURE,
                    osx_graphics.COHERENT_FEATURE, osx_graphics.I8_FEATURE,
                    osx_graphics.DEPTH_MEMORY_FEATURE, osx_graphics.SELECTIVE_2D_FEATURE,
                    osx_graphics.TEXTURE_PAGE_FEATURE, osx_graphics.RAM_SOURCE_FEATURE} | osx.SLEEP_FEATURES
        settings = {**schema.defaults(), "display.graphics": "ati-radeon",
                    "display.guest_size": "auto", "display.pixel_scale": 1}
        gl = {"ok": True, "render_node": "/dev/dri/renderD129",
              "renderer": "virgl AMD gfx1150", "software": False}
        with mock.patch.object(osx_graphics, "_host_gl", gl):
            for host, missing, build, helper, edid in [
                ((1920, 1080), None, "8S165", "firmware-desktop-1", (1280, 800)),
                ((1920, 1080), None, "8S165", None, (1920, 1080)),
                ((1920, 1080), None, "8S165", "edid-only", (1920, 1080)),
                ((1920, 1080), osx_graphics.RAM_SOURCE_FEATURE, "8S165", "firmware-desktop-1", (1920, 1080)),
                ((1920, 1080), osx_graphics.TEXTURE_PAGE_FEATURE, "8S165", "firmware-desktop-1", (1920, 1080)),
                ((1366, 768), None, "8S165", "firmware-desktop-1", (1360, 768)),
            ]:
                with self.subTest(host=host, missing=missing):
                    features = (set(base["features"]) | required) - ({missing} if missing else set())
                    path.write_text(json.dumps({**base, "features": sorted(features)}))
                    m.meta.update(guest_os="macosx-10.4", os_build=build)
                    m.meta["template"]["osxDisplaySetup"] = helper
                    args = build_argv(m, settings, self.paths, {"mode": [*host, 60]})
                    self.assertTrue(any(a.startswith("ati-vga,") and
                                        f"xres={edid[0]},yres={edid[1]},refresh_rate=60000" in a
                                        for a in args))
                    initial = (1360, 768) if host == (1366, 768) else host
                    self.assertEqual(args[args.index("-g")+1], f"{initial[0]}x{initial[1]}x32")
            # Other native profiles retain their prior advertised geometry.
            m.meta.update(guest_os="macosx-10.3", os_build="7W98")
            path.write_text(json.dumps({**base, "features": sorted(set(base["features"]) | required)}))
            args = build_argv(m, settings, self.paths, {"mode": [1920, 1080, 60]})
            self.assertTrue(any(a.startswith("ati-vga,") and "xres=1920,yres=1080" in a for a in args))

    def test_native_ati_requires_qualified_identity_package_and_exact_hardware(self):
        from retroboot import osx_graphics
        from retroboot.machines import launch_spec
        m = self.mac()
        m.meta["os_build"] = "8S165"
        manifest_path = self.pkg / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        settings = dict(schema.defaults(), **{"display.graphics": "ati-radeon",
                                            "display.guest_size": "1280x800"})
        gl = {"ok": True, "render_node": "/dev/dri/renderD129", "renderer": "virgl AMD gfx1150",
              "version": "OpenGL ES 3.2 Mesa", "software": False}
        with mock.patch.object(osx_graphics, "_host_gl", gl):
            self.assertFalse(any(a.startswith("ati-vga,") for a in build_argv(m, settings, self.paths, None)))
            manifest["features"] += sorted({osx_graphics.FEATURE, osx_graphics.EGL_FEATURE,
                                            osx_graphics.COHERENT_FEATURE, osx_graphics.I8_FEATURE} |
                                           osx.SLEEP_FEATURES)
            manifest_path.write_text(json.dumps(manifest))
            args = build_argv(m, settings, self.paths, None)
            self.assertIn("ati-vga,model=r100,vgamem_mb=32,romfile=,addr=0x11,x-agp=on,guest_hwcursor=on,"
                          "xres=1280,yres=800,refresh_rate=60000,x-coherent-vram=on,x-i8-byte-order=on", args)
            self.assertIn("uni-north-pci-pcihost.x-agp-compat=on", args)
            self.assertIn("1280x800x32", args)
            self.assertNotIn("VGA.romfile=", args)
            self.assertNotIn("retroboot-display", " ".join(args))
            spec = launch_spec(m, args, self.paths, "native-ati", settings, None, "retroboot")
            self.assertEqual(spec["egl_render_node"], "/dev/dri/renderD129")
            app = SimpleNamespace(paths=self.paths, settings=SimpleNamespace(applied=settings, desired=settings),
                                  supervisor=SimpleNamespace(active_machine=lambda: m, active=False),
                                  presenter=SimpleNamespace(display={"mode": [1280, 800, 60]}),
                                  apply_engine=SimpleNamespace(update_boot_display=AsyncMock(), push_display=AsyncMock()),
                                  lifecycle=SimpleNamespace(event=mock.Mock()))
            g = graphics.Graphics(app)
            self.assertTrue(g.availability()["available"])
            self.assertEqual(g.next_card(), graphics.ATI)
            self.assertIsNone(g.startup_guest_size())
            asyncio.run(g.prepare_launch(m))  # No classic guest-preparation object exists here.
            self.assertEqual(graphics.read_hint(self.paths.state)["startup_guest_size"], None)
            self.assertEqual(g.public()["host_gl"]["render_node"], "/dev/dri/renderD129")
            # The R100 CRTC counts eight-pixel units. A 1366-wide host must
            # advertise 1360 to the guest, rather than clipping a 1366 desktop.
            settings["display.guest_size"] = "auto"
            settings["display.pixel_scale"] = 1
            for host_size, native_size in (((1366, 768), (1360, 768)),
                                           ((1537, 864), (1536, 864)),
                                           ((1920, 1080), (1920, 1080))):
                app.presenter.display = {"mode": [*host_size, 60]}
                sized = build_argv(m, settings, self.paths, app.presenter.display)
                self.assertTrue(any(a.startswith("ati-vga,") and
                                    f"xres={native_size[0]},yres={native_size[1]}" in a for a in sized))
                self.assertEqual(g.ui_context()["ati_screen"], list(native_size))
            app.supervisor.active = True
            app.supervisor.guest_surface = (1280, 800)
            g.running = graphics.ATI
            self.assertEqual(g.public()["mac_screen"], [1280, 800])
            app.supervisor.active = False
            settings["display.guest_size"] = "1280x800"
            self.assertNotIn("x-texture-sampling=on", " ".join(args))
            for change in ({"software": True}, {"renderer": "llvmpipe"},
                           {"render_node": "/dev/dri/card0"}, {"ok": False}):
                with mock.patch.object(osx_graphics, "_host_gl", {**gl, **change}):
                    self.assertFalse(any(a.startswith("ati-vga,") for a in
                                         build_argv(m, settings, self.paths, None)))
            cd = self.root / "native-install.iso"
            cd.write_bytes(b"original media fixture")
            self.assertFalse(any(a.startswith("ati-vga,") for a in
                                 build_argv(m, settings, self.paths, None, LaunchOptions(boot_cd=cd))))
            m.meta["os_build"] = "8I127"
            self.assertFalse(any(a.startswith("ati-vga,") for a in build_argv(m, settings, self.paths, None)))

    def test_native_acceleration_requires_shadow_and_paired_sleep_repairs(self):
        from retroboot import osx_graphics
        m = self.mac("macosx-10.3")
        m.meta["os_build"] = "7W98"
        path = self.pkg / "manifest.json"
        manifest = json.loads(path.read_text())
        original = set(manifest["features"])
        required = {osx_graphics.FEATURE, osx_graphics.EGL_FEATURE,
                    osx_graphics.COHERENT_FEATURE, osx_graphics.I8_FEATURE} | osx.SLEEP_FEATURES
        settings = dict(schema.defaults(), **{"display.graphics": "ati-radeon"})
        gl = {"ok": True, "render_node": "/dev/dri/renderD128", "renderer": "AMD gfx1150",
              "software": False}
        for profile, build in (("macosx-10.3", "7W98"), ("macosx-10.4", "8S165")):
            m.meta.update(guest_os=profile, os_build=build)
            with mock.patch.object(osx_graphics, "_host_gl", gl):
                for missing in required:
                    with self.subTest(profile=profile, missing=missing):
                        path.write_text(json.dumps({**manifest, "features": sorted(original | (required - {missing}))}))
                        self.assertFalse(osx_graphics.qualified(m, self.pkg))
                        self.assertFalse(any(a.startswith("ati-vga,") for a in
                                             build_argv(m, settings, self.paths, None)))
                path.write_text(json.dumps({**manifest, "features": sorted(original | required)}))
                self.assertTrue(osx_graphics.qualified(m, self.pkg))
        for profile, build in (("macosx-10.2", "6R73"), ("macosx-10.3", "7W98x"),
                               ("macosx-10.3", None), ("macosx-10.4", "8I128")):
            m.meta.update(guest_os=profile, os_build=build)
            self.assertFalse(osx_graphics.qualified(m, self.pkg))

    def test_dedicated_flags_cannot_qualify_wrong_qemu_source(self):
        manifest_path = self.pkg / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        for field, value in (("version", "11.0.2"), ("commit", "0" * 40),
                              ("debian_version", "unknown"), ("debian_orig_sha256", "0" * 64)):
            altered = json.loads(json.dumps(manifest))
            altered["sources"]["qemu"][field] = value
            manifest_path.write_text(json.dumps(altered))
            self.assertFalse(osx.package_capable(self.pkg), field)
        manifest_path.write_text(json.dumps(manifest))
        self.assertTrue(osx.package_capable(self.pkg))

    def test_tiger_requires_256_mb(self):
        m = self.mac()
        s = schema.defaults()
        s["machine.ram_mb"] = 128
        with self.assertRaisesRegex(MachineError, "256 MB"):
            build_argv(m, s, self.paths, None)

    def test_detailed_native_mode_requires_the_separate_ndrv_feature(self):
        m = self.mac()
        display = {"mode": [1280, 800, 60]}
        args = build_argv(m, schema.defaults(), self.paths, display, LaunchOptions())
        self.assertNotIn("VGA.xres=1280", args)
        manifest_path = self.pkg / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["features"].append("macosx-vga-edid-1")
        manifest_path.write_text(json.dumps(manifest))
        args = build_argv(m, schema.defaults(), self.paths, display, LaunchOptions())
        self.assertIn("VGA.xres=1280", args)
        self.assertIn("VGA.yres=800", args)
        self.assertIn("1280x800x32", args)

    def test_osx_verifies_the_stock_vga_driver_without_loading_classic_overlay(self):
        from retroboot.machines import verify_package, launch_spec
        m = self.mac()
        settings = schema.defaults()
        args = build_argv(m, settings, self.paths, None)
        spec = launch_spec(m, args, self.paths, "test", settings, None, "retroboot")
        self.assertIn("share/qemu/qemu_vga.ndrv", spec["package"]["verify"])
        self.assertNotIn("share/qemu/ndrvloader", spec["package"]["verify"])
        (self.pkg / "share/qemu/qemu_vga.ndrv").write_bytes(b"corrupted package driver")
        with self.assertRaises(MachineError):
            verify_package(self.pkg, osx.PACKAGE_FILES)

    def test_jaguar_uses_qualified_single_relative_pointer(self):
        m = self.mac("macosx-10.2")
        args = build_argv(m, schema.defaults(), self.paths, None)
        self.assertIn("usb-mouse,bus=osx-usb.0", args)
        self.assertNotIn("usb-tablet,bus=osx-usb.0", args)
        self.assertEqual(args.count("pci-ohci,id=osx-usb"), 1)

    def test_nvram_is_private_validated_and_never_silently_replaced(self):
        first, second = self.mac(), self.mac()
        self.assertNotEqual(first.nvram.stat().st_ino, second.nvram.stat().st_ino)
        self.assertFalse(osx.prepare(first, self.pkg))
        damaged = bytearray(nvram_seed())
        damaged[8213] ^= 1
        first.nvram.write_bytes(damaged)
        with self.assertRaises(MachineError):
            osx.prepare(first, self.pkg)
        self.assertEqual(first.nvram.read_bytes(), damaged)
        for bad in (bytes(8192), bytes(16384), nvram_seed()[:-1], nvram_seed() + b"x"):
            with self.assertRaises(MachineError):
                osx.check_nvram(bad)
        first.nvram.unlink()
        first.nvram.symlink_to(second.nvram)
        with self.assertRaises(MachineError):
            osx.prepare(first, self.pkg)
        self.assertEqual(second.nvram.read_bytes(), nvram_seed())

    def test_classic_preparation_is_never_called_even_on_import(self):
        m = self.mac()
        app = SimpleNamespace(guestprep=SimpleNamespace(apply=AsyncMock(side_effect=AssertionError("classic prep"))),
                              graphics=SimpleNamespace(prepare_launch=AsyncMock()), ensure_guest_isolation=AsyncMock())
        sup = object.__new__(Supervisor)
        sup.app = app
        calls = sup._prep(m)
        self.assertEqual(asyncio.run(calls["apply"](m.disk))["result"], "unavailable")
        self.assertIsNone(asyncio.run(sup.prepare_disk(m)))
        app.guestprep.apply.assert_not_called()
        app.graphics.prepare_launch.assert_awaited_once_with(m)
        app.ensure_guest_isolation.assert_awaited_once()

    def test_preboot_style_follows_selected_mac(self):
        m = self.mac()
        app = SimpleNamespace(machines=self.reg, settings=SimpleNamespace(applied={"machine.active": m.id}),
                              paths=SimpleNamespace(state=self.root, run=self.root))
        progress = StartupProgress(app)
        self.assertEqual(progress.config(), {"enabled": False, "style": "macosx", "art": None,
                                              "capture": None, "happy_mac": "prompt"})
        helpers.make_machine(self.root)
        app.settings.applied["machine.active"] = "mac-os-9"
        self.assertEqual(progress.style(), "classic")
        app.settings.desired = {"machine.active": m.id}
        self.assertEqual(progress.style(), "macosx")
        app.supervisor = SimpleNamespace(active=True, machine=self.reg.get("mac-os-9"))
        self.assertEqual(progress.style(), "classic")   # keep the running Mac's scene

    def test_settings_explain_fixed_osx_controls_without_changing_classic(self):
        m = self.mac()
        ctx = schema.ValidationContext(values={"machine.active": m.id}, machine_guest_os={m.id: m.guest_os})
        for key in schema.OSX_CONTROL_NOTES:
            described = schema.BY_KEY[key].describe(ctx)
            self.assertFalse(described["available"], key)
            self.assertTrue(described["unavailable_reason"], key)
        self.assertTrue(schema.BY_KEY["machine.ram_mb"].describe(ctx)["available"])
        screen = schema.BY_KEY["display.guest_size"].describe(ctx)
        self.assertTrue(screen["available"])
        self.assertEqual(screen["label"], "Startup screen size")
        self.assertIn("System Preferences", screen["help"])
        ctx.machine_guest_os[m.id] = "classic"
        self.assertTrue(schema.BY_KEY["machine.cpu"].describe(ctx)["available"])
        self.assertNotEqual(schema.BY_KEY["display.guest_size"].describe(ctx)["label"], "Startup screen size")

    def test_recovery_refreshes_running_or_next_mac_style_before_the_panel(self):
        from retroboot.hostd import Hostd
        m = self.mac()
        helpers.make_machine(self.root)
        requests = []
        async def request(command, **fields):
            requests.append((command, fields))
            return {"ok": True}
        app = SimpleNamespace(machines=self.reg, paths=self.paths,
                              settings=SimpleNamespace(applied={"machine.active": "mac-os-9"},
                                                       desired={"machine.active": m.id}),
                              supervisor=SimpleNamespace(active=True, machine=self.reg.get("mac-os-9")),
                              lifecycle=SimpleNamespace(event=lambda *a, **kw: None),
                              presenter=SimpleNamespace(try_request=request))
        app.startup = StartupProgress(app)
        for active, selected, expected in ((True, m.id, "classic"), (False, m.id, "macosx"),
                                           (False, "mac-os-9", "classic")):
            app.supervisor.active = active
            app.settings.desired["machine.active"] = selected
            requests.clear()
            asyncio.run(Hostd.show_scene(app, "panel", background="checkerboard"))
            self.assertEqual([command for command, _ in requests], ["startup_config", "set_scene"])
            self.assertEqual(requests[0][1]["style"], expected)

    def test_install_disc_copy_follows_osx_profile(self):
        from retroboot.hostd import Hostd
        from retroboot.panel.controller import NeedsCD, PowerAlert, StartFromCD, StartupDisk, Checkpoints, ConfirmRestore
        m = self.mac()
        app = Hostd(helpers.make_config(self.root))
        app.settings.store.load()
        app.settings.set_desired_and_applied({"machine.active": m.id},
                                              schema.ValidationContext(machines={m.id}, check_capabilities=False))
        app.supervisor.machine = m
        app.supervisor.state = "running"
        self.assertIn("Apple menu", json.dumps(PowerAlert(app.panel).doc()))
        self.assertIn("disk and startup memory", json.dumps(Checkpoints(app.panel, m.id, None).doc()))
        self.assertIn("disk and startup memory", json.dumps(ConfirmRestore(app.panel, m.id, "test", None).doc()))
        doc = NeedsCD(app.panel, "Lab. Tiger", True).doc()
        text = json.dumps(doc, ensure_ascii=False)
        self.assertEqual(doc["items"][0]["text"], "There's no Mac OS X on “Lab. Tiger” yet.")
        self.assertTrue(doc["items"][1]["text"].startswith("Put a Mac OS X install disc"))
        self.assertIn("Mac OS X install disc", text)
        self.assertIn("DVD drive", text)
        picker = StartFromCD(app.panel, m.id, StartupDisk(app.panel, back=None))
        text = json.dumps(picker.doc(), ensure_ascii=False)
        self.assertIn("Start Up from Disc", text)
        self.assertIn("Mac OS X install disc", text)
        self.assertNotIn("Mac OS CD", text)

    def test_checkpoint_restores_disk_and_nvram_as_one_pair(self):
        m = self.mac()
        m.update_meta(os_version="10.4.6", os_build="8I128")
        with m.disk.open("r+b") as f:
            f.write(b"before checkpoint")
        checkpoints = Checkpoints(self.paths.checkpoints)
        cp = asyncio.run(checkpoints.create(m, "OS X", {}, 1, {}))
        before_nvram = m.nvram.read_bytes()
        changed = bytearray(before_nvram)
        struct.pack_into(">I", changed, 8212, 3)
        struct.pack_into(">I", changed, 8208, zlib.adler32(changed[8212:], 1))
        m.nvram.write_bytes(changed)
        with m.disk.open("r+b") as f:
            f.write(b"after checkpoint ")
        m.update_meta(os_version="10.4.11", os_build="8S165")
        old_inode = m.dir.stat().st_ino
        restored = asyncio.run(checkpoints.restore(m, cp["id"]))
        self.assertTrue(restored["atomic_pair"])
        self.assertNotEqual(m.dir.stat().st_ino, old_inode)
        with m.disk.open("rb") as f:
            self.assertEqual(f.read(17), b"before checkpoint")
        self.assertEqual(m.nvram.read_bytes(), before_nvram)
        self.assertEqual((m.meta["os_version"], m.meta["os_build"]), ("10.4.6", "8I128"))
        self.assertEqual(self.reg.get(m.id).meta["os_build"], "8I128")
        self.assertTrue(m.state()["clean"])
        self.assertEqual(self.reg.get(m.id).guest_os, m.guest_os)
        self.assertEqual(list(m.dir.parent.glob(".restore-*")), [])

    def test_checkpoint_rejects_mismatched_osx_version_before_exchange(self):
        m = self.mac()
        checkpoints = Checkpoints(self.paths.checkpoints)
        cp = asyncio.run(checkpoints.create(m, "OS X", {}, 1, {}))
        saved = checkpoints.get(m.id, cp["id"]) / "machine.json"
        data = json.loads(saved.read_text())
        saved.write_text(json.dumps({**data, "os_version": "10.3.9", "os_build": "7W98"}))
        inode = m.dir.stat().st_ino
        with self.assertRaises(CheckpointError):
            asyncio.run(checkpoints.restore(m, cp["id"]))
        self.assertEqual(m.dir.stat().st_ino, inode)

    def test_unavailable_atomic_restore_leaves_current_pair(self):
        m = self.mac()
        checkpoints = Checkpoints(self.paths.checkpoints)
        cp = asyncio.run(checkpoints.create(m, "OS X", {}, 1, {}))
        with m.disk.open("r+b") as f:
            f.write(b"current disk")
        inode, nvram = m.dir.stat().st_ino, m.nvram.read_bytes()
        with mock.patch("retroboot.checkpoints.exchange_directories", side_effect=OSError("unsupported")):
            with self.assertRaises(CheckpointError) as caught:
                asyncio.run(checkpoints.restore(m, cp["id"]))
        self.assertEqual(caught.exception.code, "atomic_restore_unavailable")
        self.assertEqual(m.dir.stat().st_ino, inode)
        with m.disk.open("rb") as f:
            self.assertEqual(f.read(12), b"current disk")
        self.assertEqual(m.nvram.read_bytes(), nvram)

    def test_restore_retains_previous_pair_if_final_sync_fails(self):
        m = self.mac()
        checkpoints = Checkpoints(self.paths.checkpoints)
        cp = asyncio.run(checkpoints.create(m, "OS X", {}, 1, {}))
        with m.disk.open("r+b") as f:
            f.write(b"old live Mac")
        from retroboot.checkpoints import fsync_directory
        def fail_parent(path):
            if path == m.dir.parent:
                raise OSError("injected parent sync failure")
            fsync_directory(path)
        with mock.patch("retroboot.checkpoints.fsync_directory", side_effect=fail_parent):
            with self.assertRaises(CheckpointError) as caught:
                asyncio.run(checkpoints.restore(m, cp["id"]))
        self.assertEqual(caught.exception.code, "restore_not_synced")
        saved = list(m.dir.parent.glob(".restore-*"))
        self.assertEqual(len(saved), 1)
        with (saved[0] / "disk.img").open("rb") as f:
            self.assertEqual(f.read(12), b"old live Mac")
        self.assertEqual((saved[0] / "nvram.img").read_bytes(), nvram_seed())

    def test_per_machine_settings_and_smart_host_ram_gl_defaults(self):
        from retroboot import osx_graphics
        m = self.mac("macosx-10.4")
        m.update_meta(name="Mac OS X 10.4.11 Tiger Deluxe")
        m.meta["os_build"] = "8S165"
        path = self.pkg / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["features"] += sorted({
            osx_graphics.FEATURE, osx_graphics.EGL_FEATURE,
            osx_graphics.COHERENT_FEATURE, osx_graphics.I8_FEATURE,
        } | osx.SLEEP_FEATURES)
        path.write_text(json.dumps(manifest))

        # Smart defaults: Deluxe + host RAM >= 4 GB + GL ready -> 2048 MB & ati-radeon (#44).
        gl = {"ok": True, "render_node": "/dev/dri/renderD129", "renderer": "AMD gfx1150", "software": False}
        with mock.patch.object(osx_graphics, "_host_gl", gl), \
                mock.patch("retroboot.hostinfo.memory_bytes", return_value=6144 * 1024 * 1024):
            args = build_argv(m, schema.defaults(), self.paths, {"mode": [1920, 1080, 60]},
                              LaunchOptions())
        self.assertEqual(args[args.index("-m") + 1], "2048")
        self.assertTrue(any(a.startswith("ati-vga,") for a in args))
        # #18: on 1920x1080 auto (which would give classic 960x540), OS X ATI picks 1024x768.
        self.assertIn("1024x768x32", args)
        self.assertTrue(any("xres=1024,yres=768" in a for a in args))

        # machine.json["settings"] explicitly overrides template & global defaults.
        m.meta["template"]["ramMB"] = 1024
        m.meta["settings"] = {"osx_ram_mb": 1536, "graphics": "standard"}
        with mock.patch.object(osx_graphics, "_host_gl", gl):
            args2 = build_argv(m, schema.defaults(), self.paths, {"mode": [1920, 1080, 60]})
        self.assertEqual(args2[args2.index("-m") + 1], "1536")
        self.assertFalse(any(a.startswith("ati-vga,") for a in args2))

        # Without machine.json["settings"], template ramMB overrides the bare 512 MB default.
        del m.meta["settings"]
        args3 = build_argv(m, schema.defaults(), self.paths, None)
        self.assertEqual(args3[args3.index("-m") + 1], "1024")


class FinderPresence(unittest.TestCase):
    def test_exact_profile_version_and_unmodified_menu_are_required(self):
        from retroboot import finder_osx
        width, height = 400, 32
        rgb = bytes(i % 251 for i in range(width * height * 3))
        box = (48, 2, 396, 20)
        cropped = b"".join(rgb[(y * width + 48) * 3:(y * width + 396) * 3] for y in range(2, 20))
        reference = (("macosx-10.4", "10.4.6", "8I128", "original-test-menu", box, hashlib.sha256(cropped).hexdigest()),)
        with mock.patch.object(finder_osx, "REFERENCES", reference):
            self.assertEqual(finder_osx.finder_visible("macosx-10.4", "10.4.6", width, height, rgb, build="8I128"),
                             "original-test-menu")
            self.assertIsNone(finder_osx.finder_visible("macosx-10.4", "10.4.6", width, height, rgb))
            self.assertIsNone(finder_osx.finder_visible("macosx-10.4", "10.4.6", width, height, rgb, build="8I127"))
            damaged = bytearray(rgb)
            damaged[(2 * width + 48) * 3] ^= 1
            for profile, version, data in (("macosx-10.3", "10.3.9", rgb),
                                            ("macosx-10.4", "10.4.11", rgb),
                                            ("macosx-10.4", "10.4.6", damaged),
                                            ("macosx-10.4", "10.4.6", rgb[:-1])):
                self.assertIsNone(finder_osx.finder_visible(profile, version, width, height, data, build="8I128"))

    def test_visible_finder_does_not_set_ready_or_responsive(self):
        from retroboot.util import mono
        events = []
        sup = object.__new__(Supervisor)
        sup.osx_finder_visible = sup.finder_seen = sup.started_seen = False
        sup.session, sup.t_start = "test", mono()
        sup.finder_event, sup.up_event = asyncio.Event(), asyncio.Event()
        sup.log = SimpleNamespace(event=lambda event, **kw: events.append(event))
        asyncio.run(sup.on_osx_finder_visible(1024, 768, "reference"))
        asyncio.run(sup.on_osx_finder_visible(1024, 768, "reference"))
        self.assertTrue(sup.osx_finder_visible)
        self.assertFalse(sup.mac_os_up)
        self.assertFalse(sup.finder_event.is_set())
        self.assertFalse(sup.up_event.is_set())
        self.assertEqual(events, ["osx_finder_visible"])


if __name__ == "__main__":
    unittest.main()


class NativeClock(unittest.TestCase):
    def image(self, seconds=0, generation=2):
        data = bytearray(nvram_seed())
        struct.pack_into(">I", data, 8192 + 0xc40 + 0xec, seconds & 0xffffff)
        struct.pack_into(">I", data, 8212, generation)
        struct.pack_into(">I", data, 8208, zlib.adler32(data[8212:], 1))
        return bytes(data)

    def test_utc_and_persisted_native_offset_are_independent_of_host_zone(self):
        from datetime import datetime, timezone
        now = datetime(2005, 4, 29, 12, 30, tzinfo=timezone.utc)
        self.assertEqual(osx.rtc_base(self.image(), now), "base=utc")
        for zone in ("UTC", "America/Los_Angeles", "Asia/Tokyo"):
            with mock.patch.dict("os.environ", {"TZ": zone}):
                self.assertEqual(osx.rtc_base(self.image(-18000), now), "base=2005-04-29T07:30:00")
                self.assertEqual(osx.rtc_base(self.image(50400), now), "base=2005-04-30T02:30:00")

    def test_newest_valid_bank_and_interrupted_bank(self):
        first = self.image(-18000, 3)[8192:]
        second = self.image(3600, 2)[8192:]
        self.assertEqual(osx.rtc_offset(first + second), -18000)
        self.assertEqual(osx.rtc_offset(bytes(8192) + second), 3600)
        with self.assertRaises(MachineError):
            osx.rtc_offset(first + self.image(3600, 3)[8192:])

    def test_malformed_or_out_of_range_clock_fails_closed(self):
        for seconds in (86401, -86401):
            with self.assertRaises(MachineError):
                osx.rtc_offset(self.image(seconds))
        data = bytearray(self.image())
        data[8192 + 32 + 2:8192 + 32 + 4] = b"\xff\xff"
        struct.pack_into(">I", data, 8208, zlib.adler32(data[8212:], 1))
        with self.assertRaises(MachineError):
            osx.rtc_offset(data)
