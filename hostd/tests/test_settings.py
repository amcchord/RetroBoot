"""Schema validation, atomic store, revisions, corruption recovery, timed confirmation."""
import asyncio
import json
import os
import unittest
from pathlib import Path

import helpers  # noqa: F401  (sys.path)
from retroboot import schema
from retroboot.settings import NothingToConfirm, SettingsService, SettingsStore, StaleRevision


class SchemaTests(unittest.TestCase):
    def test_osx_memory_is_separate_from_classic_and_round_trips(self):
        ctx = schema.ValidationContext(machines={"classic", "tiger"},
                                       machine_guest_os={"classic": "classic", "tiger": "macosx-10.4"},
                                       machine_families={"classic": "powermac", "tiger": "powermac"},
                                       values={"machine.active": "classic"})
        choices = schema.BY_KEY["machine.active"].describe(ctx)["choices"]
        self.assertEqual({c["value"]: c["guest_os"] for c in choices}, ctx.machine_guest_os)
        cur = schema.defaults({"machine.cpu": "g3", "machine.ram_mb": 768})
        # Deliberately put memory first: selection must use the complete candidate.
        changed = schema.validate_changes(cur, {"machine.osx_ram_mb": 2048, "machine.active": "tiger"}, ctx)
        self.assertEqual(changed["machine.ram_mb"], 768)
        self.assertEqual(changed["machine.cpu"], "g3")
        self.assertEqual(ctx.values, {"machine.active": "classic"})
        classic = schema.validate_changes(changed, {"machine.active": "classic"}, ctx)
        self.assertEqual(classic["machine.ram_mb"], 768)
        self.assertEqual(classic["machine.osx_ram_mb"], 2048)
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            store = SettingsStore(Path(tmp)/"settings.json", Path(tmp)/"good.json")
            store.load()
            SettingsService(store).set_desired_and_applied({"machine.active": "tiger", "machine.osx_ram_mb": 2048,
                                                          "machine.ram_mb": 768, "machine.cpu": "g3"}, ctx)
            loaded = SettingsStore(store.path, store.known_good_path)
            loaded.load()
            self.assertIsNone(loaded.notice)
            self.assertEqual(loaded.state["applied"]["machine.osx_ram_mb"], 2048)
            self.assertEqual(loaded.state["applied"]["machine.ram_mb"], 768)

    def test_only_one_memory_control_is_described_for_each_system(self):
        for profile, key, hidden in (("classic", "machine.ram_mb", "machine.osx_ram_mb"),
                                     ("macosx-10.4", "machine.osx_ram_mb", "machine.ram_mb")):
            ctx = schema.ValidationContext(values={"machine.active": "mac"}, machine_guest_os={"mac": profile})
            fields = {v["key"]: v for v in schema.describe(ctx)}
            self.assertIn(key, fields)
            self.assertNotIn(hidden, fields)
            if profile == "classic":
                self.assertNotIn(2048, [v["value"] for v in fields[key]["choices"]])
            else:
                self.assertIn({"value": 2048, "label": "2048 MB"}, fields[key]["choices"])
                self.assertNotIn(128, [v["value"] for v in fields[key]["choices"]])
        with self.assertRaises(schema.ValidationError):
            schema.validate_changes(schema.defaults(), {"machine.active": "mac", "machine.osx_ram_mb": 128},
                                    schema.ValidationContext(machine_guest_os={"mac": "macosx-10.4"}))

    def test_osx_memory_type_and_system_switch_floor(self):
        ctx = schema.ValidationContext(machines={"jaguar", "tiger"},
                                       machine_guest_os={"jaguar": "macosx-10.2", "tiger": "macosx-10.4"})
        old = schema.defaults({"machine.active": "jaguar", "machine.osx_ram_mb": 128})
        with self.assertRaises(schema.ValidationError) as error:
            schema.validate_changes(old, {"machine.active": "tiger"}, ctx)
        self.assertIn("machine.osx_ram_mb", error.exception.fields)
        good = schema.validate_changes(old, {"machine.active": "tiger", "machine.osx_ram_mb": 256}, ctx)
        self.assertEqual(good["machine.osx_ram_mb"], 256)
        for invalid in (2048.0, "2048", True, None):
            with self.subTest(invalid=invalid), self.assertRaises(schema.ValidationError):
                schema.validate_changes(good, {"machine.osx_ram_mb": invalid}, ctx)

    def test_every_setting_declares_contract(self):
        for s in schema.SETTINGS:
            with self.subTest(key=s.key):
                self.assertIn(s.apply, schema.APPLY_POLICIES)
                self.assertTrue(s.label and s.help and s.group)
                self.assertEqual(s.validate(s.default) if s.default is not None else None, s.default)
        keys = {s.key for s in schema.SETTINGS}
        for k in ("machine.active", "machine.ram_mb", "machine.cpu", "machine.boot_speed", "startup.autostart",
                  "display.pixel_scale", "display.guest_size", "display.scaling", "display.host_mode", "input.swap_command_option",
                  "input.recovery_chord", "input.pointer_speed", "audio.enabled", "audio.volume", "network.guest",
                  "share.enabled", "share.volume_name", "management.web", "management.hostname", "system.timezone"):
            self.assertIn(k, keys)
        self.assertEqual(schema.BY_KEY["display.host_mode"].confirm, 15)
        self.assertEqual(schema.BY_KEY["system.timezone"].apply, schema.GUEST_RESTART)

    def test_rejections(self):
        cur = schema.defaults()
        bad = {"machine.ram_mb": 300, "machine.cpu": "g5", "audio.volume": 101, "input.pointer_speed": 9,
               "share.volume_name": "x" * 28, "management.hostname": "Bad_Name", "display.host_mode": "huge",
               "startup.autostart": "yes", "system.timezone": "Mars/Olympus", "nonsense.key": 1}
        with self.assertRaises(schema.ValidationError) as cm:
            schema.validate_changes(cur, bad, schema.ValidationContext())
        self.assertEqual(set(cm.exception.fields), set(bad))

    def test_dynamic_choices_and_capabilities(self):
        ctx = schema.ValidationContext(machines={"mac-1"}, host_modes=["1024x768@60"],
                                       capabilities={"display_modes": True, "audio": False})
        cur = schema.defaults()
        out = schema.validate_changes(cur, {"machine.active": "mac-1", "display.host_mode": "1024x768@60"}, ctx)
        self.assertEqual(out["machine.active"], "mac-1")
        with self.assertRaises(schema.ValidationError) as cm:
            schema.validate_changes(cur, {"machine.active": "other", "display.host_mode": "800x600@60"}, ctx)
        self.assertIn("machine.active", cm.exception.fields)
        with self.assertRaises(schema.ValidationError) as cm:
            schema.validate_changes(cur, {"audio.volume": 10}, ctx)
        self.assertIn("Not available", cm.exception.fields["audio.volume"])

    def test_cross_validation(self):
        cur = schema.defaults()
        with self.assertRaises(schema.ValidationError):
            schema.validate_changes(cur, {"machine.cpu": "g3", "machine.ram_mb": 896}, None)

    def test_pixel_scale_choices(self):
        """display.pixel_scale: offered sizes with their Mac screens (ARCHITECTURE 4.5)."""
        def choices(w, h):
            d = schema.BY_KEY["display.pixel_scale"].describe(schema.ValidationContext(display_mode=(w, h)))
            self.assertEqual(d["type"], "enum")
            return {c["value"]: c for c in d["choices"]}
        examples = {(1920, 1080): {"1": "1920 × 1080", "2": "960 × 540"},
                    (2560, 1440): {"1": "2560 × 1440", "2": "1280 × 720", "3": "848 × 480"},
                    (3840, 2160): {"1": "3840 × 2160", "2": "1920 × 1080", "3": "1280 × 720", "4": "960 × 540"},
                    (1280, 800): {"1": "1280 × 800"}}
        for (w, h), offered in examples.items():
            with self.subTest(mode=(w, h)):
                c = choices(w, h)
                self.assertEqual(list(c), ["auto", "1", "2", "3", "4"])
                enabled = {v: x["label"] for v, x in c.items() if v != "auto" and not x.get("disabled")}
                self.assertEqual(set(enabled), set(offered))
                for v, size in offered.items():
                    self.assertEqual(enabled[v], f"{v}× — {size}")
                for v, x in c.items():
                    if x.get("disabled"):
                        self.assertIn("needs a larger display", x["label"])
                        self.assertIn("at least 640 × 480", x["reason"])
        self.assertEqual(choices(1920, 1080)["auto"]["label"], "Automatic (2× — 960 × 540)")
        self.assertEqual(choices(3840, 2160)["auto"]["label"], "Automatic (4× — 960 × 540)")
        big = choices(5120, 2880)
        self.assertTrue(big["1"]["disabled"])
        self.assertIn("up to 3840 × 2160", big["1"]["reason"])
        self.assertFalse(big["2"].get("disabled"))
        # Without a display every value is offered with a plain label.
        d = schema.BY_KEY["display.pixel_scale"].describe(None)
        self.assertEqual([c["label"] for c in d["choices"]], ["Automatic", "1×", "2×", "3×", "4×"])

    def test_pixel_scale_validation(self):
        cur = schema.defaults()
        self.assertEqual(cur["display.pixel_scale"], "auto")
        ctx = schema.ValidationContext(display_mode=(1920, 1080))
        out = schema.validate_changes(cur, {"display.pixel_scale": "1"}, ctx)
        self.assertEqual(out["display.pixel_scale"], "1")
        self.assertEqual(schema.validate_changes(cur, {"display.pixel_scale": 2}, ctx)["display.pixel_scale"], "2")
        for bad in ("3", "4", "5", "0", "2x", None, True, 2.0):
            with self.subTest(value=bad), self.assertRaises(schema.ValidationError) as cm:
                schema.validate_changes(cur, {"display.pixel_scale": bad}, ctx)
        with self.assertRaises(schema.ValidationError) as cm:
            schema.validate_changes(cur, {"display.pixel_scale": "3"}, ctx)
        self.assertIn("isn't available on this display", cm.exception.fields["display.pixel_scale"])
        # Without a known display (and in a stored configuration) any size is kept:
        # the presenter falls back to the nearest size the display offers.
        self.assertEqual(schema.validate_changes(cur, {"display.pixel_scale": "4"}, None)["display.pixel_scale"], "4")
        schema.validate_complete(dict(cur, **{"display.pixel_scale": "3"}))

    def test_pixel_scale_with_fixed_sizes(self):
        """A fixed Mac screen is drawn at the pixel size when it fits, else at the
        largest integer that fits, and the panel/web say so."""
        v = dict(schema.defaults(), **{"display.pixel_scale": "2", "display.guest_size": "1280x1024"})
        self.assertEqual(schema.fixed_size_note(v, (1920, 1080)),
                         "1280 × 1024 doesn't fit at 2× on this display, so it is shown at 1×.")
        self.assertIsNone(schema.fixed_size_note(dict(v, **{"display.guest_size": "640x480"}), (1920, 1080)))
        self.assertIsNone(schema.fixed_size_note(dict(v, **{"display.scaling": "fit"}), (1920, 1080)))
        self.assertIsNone(schema.fixed_size_note(dict(v, **{"display.pixel_scale": "auto"}), (1920, 1080)))
        self.assertIn("reduced smoothly", schema.fixed_size_note(v, (1280, 800)))
        v3 = dict(v, **{"display.pixel_scale": "3", "display.guest_size": "800x600"})
        self.assertEqual(schema.fixed_size_note(v3, (2560, 1440)),
                         "800 × 600 doesn't fit at 3× on this display, so it is shown at 2×.")
        ctx = schema.ValidationContext(display_mode=(1920, 1080), values=v)
        gs = schema.BY_KEY["display.guest_size"].describe(ctx)
        self.assertIn("shown at 1×", gs["help"])
        self.assertIn("1280x1024", gs["warning_values"])
        self.assertNotIn("640x480", gs["warning_values"])
        ps = schema.BY_KEY["display.pixel_scale"].describe(ctx)
        self.assertIn("shown at 1×", ps["help"])
        self.assertEqual(set(ps["warning_values"]), {"2"})
        # Nothing to say for the automatic size.
        ctx = schema.ValidationContext(display_mode=(1920, 1080), values=schema.defaults())
        self.assertNotIn("warning_values", schema.BY_KEY["display.guest_size"].describe(ctx))

    def test_timezone_regions(self):
        regions = schema.timezone_regions()
        self.assertIn("Europe", regions)
        self.assertIn("Europe/Berlin", regions["Europe"])
        self.assertIn("UTC", regions["Other"])
        desc = schema.BY_KEY["system.timezone"].describe(None)
        self.assertIn("America/New_York", [c["value"] for c in desc["choices"]])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        self.path = self.root / "config/settings.json"
        self.kg = self.root / "state/known-good.json"

    def svc(self) -> SettingsService:
        st = SettingsStore(self.path, self.kg)
        st.load()
        return SettingsService(st)

    def test_fresh_store_and_atomic_layout(self):
        s = self.svc()
        self.assertTrue(s.store.fresh)
        data = json.loads(self.path.read_text())
        self.assertEqual(data["schema"], 1)
        for k in ("revision", "desired", "applied", "applied_revision", "known_good", "pending_confirm"):
            self.assertIn(k, data)
        self.assertEqual([p.name for p in self.path.parent.iterdir() if p.name.endswith(".tmp")], [])

    def test_revisions_and_stale_conflict(self):
        s = self.svc()
        r1 = s.patch(0, {"audio.volume": 30}, None)
        self.assertEqual(r1, 1)
        self.assertEqual(s.pending_keys(), ["audio.volume"])
        with self.assertRaises(StaleRevision) as cm:
            s.patch(0, {"audio.volume": 40}, None)
        self.assertEqual(cm.exception.current, 1)
        self.assertTrue(self.path.with_name("settings.json.bak").exists())
        # Persistence across reloads
        s2 = self.svc()
        self.assertEqual(s2.revision, 1)
        self.assertEqual(s2.desired["audio.volume"], 30)
        self.assertEqual(s2.applied["audio.volume"], 70)

    def test_invalid_candidate_never_stored(self):
        s = self.svc()
        before = self.path.read_text()
        with self.assertRaises(schema.ValidationError):
            s.patch(0, {"audio.volume": 30, "machine.ram_mb": 3}, None)
        self.assertEqual(self.path.read_text(), before)

    def test_corruption_recovers_from_bak(self):
        s = self.svc()
        s.patch(0, {"audio.volume": 30}, None)
        s.patch(1, {"audio.volume": 35}, None)
        self.path.write_text("{ not json")
        s2 = self.svc()
        self.assertEqual(s2.store.notice["source"], "bak")
        self.assertEqual(s2.desired["audio.volume"], 30)
        self.assertGreater(s2.revision, 1)
        self.assertTrue(any(p.name.startswith("settings.json.corrupt-") for p in self.path.parent.iterdir()))

    def test_corruption_falls_back_to_known_good_then_defaults(self):
        s = self.svc()
        s.patch(0, {"audio.volume": 20}, None)
        s.mark_applied(["audio.volume"])
        self.assertTrue(s.mark_known_good())
        s.patch(s.revision, {"audio.volume": 90}, None)
        s.mark_applied(["audio.volume"])
        self.path.write_text("garbage")
        self.path.with_name("settings.json.bak").write_text("garbage")
        s2 = self.svc()
        self.assertEqual(s2.store.notice["source"], "known_good")
        self.assertEqual(s2.applied["audio.volume"], 20)
        self.path.write_text("garbage")
        self.path.with_name("settings.json.bak").write_text("garbage")
        self.kg.write_text("garbage")
        s3 = self.svc()
        self.assertEqual(s3.store.notice["source"], "defaults")
        self.assertEqual(s3.applied["audio.volume"], 70)

    def test_schema_migration_fills_new_keys(self):
        s = self.svc()
        data = json.loads(self.path.read_text())
        del data["desired"]["system.timezone"]
        del data["applied"]["system.timezone"]
        data["desired"]["removed.key"] = 1
        self.path.write_text(json.dumps(data))
        s2 = self.svc()
        self.assertIsNone(s2.store.notice)
        self.assertEqual(s2.desired["system.timezone"], "UTC")
        self.assertNotIn("removed.key", s2.desired)

    def test_confirm_and_revert(self):
        s = self.svc()
        r = s.patch(0, {"display.host_mode": "1280x800@60"}, None)
        s.mark_applied(["display.host_mode"])
        s.begin_confirm({"display.host_mode": "auto"}, 15)
        self.assertIsNotNone(s.pending_confirm)
        prev = s.revert_pending_confirm()
        self.assertEqual(prev, {"display.host_mode": "auto"})
        self.assertEqual(s.applied["display.host_mode"], "auto")
        self.assertEqual(s.desired["display.host_mode"], "auto")
        self.assertGreater(s.revision, r)
        with self.assertRaises(NothingToConfirm):
            s.confirm()
        s.patch(s.revision, {"display.host_mode": "1280x800@60"}, None)
        s.mark_applied(["display.host_mode"])
        s.begin_confirm({"display.host_mode": "auto"}, 15)
        self.assertFalse(s.mark_known_good())  # never while unconfirmed
        s.confirm()
        self.assertEqual(s.state["known_good"]["values"]["display.host_mode"], "1280x800@60")
        self.assertTrue(self.kg.exists())
        self.assertEqual(oct(self.kg.stat().st_mode & 0o777), "0o600")

    def test_image_defaults(self):
        st = SettingsStore(self.path, self.kg, {"machine.boot_speed": "accelerated", "audio.volume": 999})
        st.load()
        self.assertEqual(st.state["applied"]["machine.boot_speed"], "accelerated")
        self.assertEqual(st.state["applied"]["audio.volume"], 70)


if __name__ == "__main__":
    unittest.main()
