"""Zero 2 W memory policy through the same catalog, import, settings and launch paths.

These are source tests with synthetic media, not Raspberry Pi hardware evidence.
"""
import json
import unittest
from unittest import mock
from pathlib import Path

import helpers
from retroboot import catalogs, platform, schema
from retroboot.config import Paths
from retroboot.machines import MachineError
from test_lifecycle import Base
from test_platform import tree
from test_quadra import QuadraBase

ZERO = ("Raspberry Pi Zero 2 W Rev 1.0", ["raspberrypi,model-zero-2-w", "brcm,bcm2837"], 0x902120)


class ZeroDetection(unittest.TestCase):
    def test_device_tree_revision_and_fallbacks(self):
        for model, compatible, revision in (ZERO, (ZERO[0], ZERO[1], None), (ZERO[0], [], None)):
            td, root = tree(model, compatible, revision, meminfo_kb=449000)
            with td:
                p = platform.detect(root / "proc/device-tree", root / "no-dmi", root / "proc", "aarch64")
                self.assertEqual((p.board, p.soc, p.pi_family), ("zero2w", "BCM2837", "zero2w"))
                self.assertEqual(p.machine_families, {"quadra800"})
                self.assertEqual(p.tcg_tb_size_mb, 64)
                self.assertGreater(p.timeout_scale, 1)
                self.assertFalse(p.has_grub)

    def test_image_restriction_is_enforced_on_generic_arm(self):
        td, root = helpers.scratch()
        with td:
            path = root / "profile.json"
            p = platform.Platform(family="other", machine="aarch64", ram_mb=512)
            path.write_text(json.dumps({"schema_version": 1, "model": "zero2w"}))
            restricted = platform.image_profile(p, path)
            self.assertTrue(restricted.is_zero2w)
            self.assertEqual(restricted.machine_families, {"quadra800"})
            self.assertEqual(restricted.tcg_tb_size_mb, 64)
            self.assertFalse(restricted.has_grub)
            self.assertEqual(restricted.setting_defaults["machine.quadra_ram_mb"], 64)
            self.assertIsNone(restricted.machine_unavailable("quadra800"))
            self.assertIn("68040", restricted.machine_unavailable("powermac"))
            helpers.make_m68k_package(root)
            self.assertEqual(catalogs.runnable_families(Paths(root), restricted), {"quadra800"})
            # A different image profile must never lift a physical Zero's restriction.
            path.write_text(json.dumps({"schema_version": 1, "model": "pi4-5"}))
            self.assertTrue(platform.image_profile(platform.Platform(board="zero2w"), path).is_zero2w)
            for content in ('[]', '{"schema_version": true, "model":"zero2w"}', 'bad', ' ' * 4097):
                path.write_text(content)
                self.assertEqual(platform.image_profile(p, path), p)

    def test_zero_ram_choices_are_validated_and_described(self):
        setting = schema.BY_KEY["machine.quadra_ram_mb"]
        ctx = schema.ValidationContext(quadra_ram_max_mb=128)
        self.assertEqual([c["value"] for c in setting.describe(ctx)["choices"]], [64, 128])
        self.assertEqual(setting.validate(128, ctx), 128)
        with self.assertRaises(ValueError):
            setting.validate(256, ctx)
        self.assertEqual(setting.validate(256), 256)  # other boards retain their choices

    def test_osx_catalog_capability_survives_board_filter(self):
        td, root = helpers.scratch()
        with td, mock.patch("retroboot.osx.package_capable", return_value=True):
            helpers.make_m68k_package(root)
            paths = Paths(root)
            for p in (platform.Platform(), platform.Platform(family="raspberrypi", board="pi5")):
                self.assertIn("powermac-osx", catalogs.runnable_families(paths, p))
            self.assertEqual(catalogs.runnable_families(paths, platform.Platform(image_model="zero2w")), {"quadra800"})

    def test_image_deadlines_follow_target_on_generic_arm(self):
        self.assertEqual(platform.Platform(family="other", image_model="pi4-5").timeout_scale, 3.0)
        self.assertEqual(platform.Platform(family="other", image_model="pi5").timeout_scale, 2.0)
        self.assertEqual(platform.Platform(family="raspberrypi", board="pi5", image_model="pi4-5").timeout_scale, 2.0)


def zero_profile(root):
    path = root / "usr/lib/retroboot/rpi-profile.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 1, "model": "zero2w"}))


class ZeroPowerMacRefusal(Base):
    def prepare_root(self):
        zero_profile(self.root)

    async def test_existing_power_mac_is_refused_before_launch_or_disk_prep(self):
        await self.until_event("guest_start_refused")
        self.assertFalse(self.ev("qemu_spawned"))
        self.assertFalse(self.prep.calls)
        self.assertIn("68040", self.ev("guest_start_refused")[-1]["problems"][0])
        choices = {k["family"]: k for k in self.app.blank_kinds()}
        self.assertFalse(choices["powermac"]["available"])
        with self.assertRaises(MachineError):
            self.app.create_blank_machine("PPC", 8, family="powermac")


class ZeroQuadraLaunch(QuadraBase):
    def prepare_root(self):
        super().prepare_root()
        zero_profile(self.root)

    async def test_quadras_start_with_small_translation_cache(self):
        await self.running()
        specs = list((self.root / "run/retroboot/sessions").rglob("argv.json"))
        if not specs:
            specs = list(self.root.rglob("argv.json"))
        self.assertTrue(specs)
        args = json.loads(specs[-1].read_text())
        if isinstance(args, dict):
            args = args["argv"]
        self.assertIn("tcg,tb-size=64", args)
        self.assertEqual(args[args.index("-m") + 1], "64")
