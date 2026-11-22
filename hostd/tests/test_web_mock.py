"""The web client's mock API (web/dev/mock_server.py) describes the settings exactly as
hostd does: the same settings in the same order, with hostd's defaults, choices, labels,
help, apply policies, capabilities and validation.  It fails when hostd's schema gains or
changes a setting the mock does not follow, or when the mock gets its own copy again
(its fixture once lacked 13 settings and had other defaults)."""
import importlib.util
import unittest
from pathlib import Path

from retroboot import schema

MOCK = Path(__file__).resolve().parents[2] / "web" / "dev" / "mock_server.py"

# Settings whose choices come from the device (its machines, display modes, CD images,
# display size) and fields that depend on it: compared against the mock's context below.
DEVICE_CHOICES = {"machine", "host_mode", "media", "pixel_scale"}
CONTEXT_FIELDS = {"available", "unavailable_reason"}


def load_mock():
    spec = importlib.util.spec_from_file_location("rb_web_mock_server", MOCK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@unittest.skipUnless(MOCK.is_file(), "web/dev/mock_server.py is not beside hostd")
class MockSettingsAreHostds(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ms = load_mock()

    def mock(self, *argv):
        return self.ms.Mock(self.ms.parser().parse_args(list(argv)))

    def test_the_mock_serves_hostds_description(self):
        for argv in ([], ["--profile", "laptop"], ["--no-audio", "--no-ati-software"]):
            with self.subTest(argv=argv):
                m = self.mock(*argv)
                got = m.settings_json()
                self.assertEqual(got["schema_version"], schema.SCHEMA_VERSION)
                self.assertEqual(got["schema"], schema.describe(m.validation_context()))

    def test_same_settings_defaults_choices_and_words(self):
        m = self.mock()
        mock = m.settings_json()["schema"]
        # The Classic mock hides the independently stored OS X memory control.
        real = [d for d in schema.describe(None) if d["key"] != "machine.osx_ram_mb"]
        self.assertEqual([d["key"] for d in mock], [d["key"] for d in real])
        for r, d in zip(real, mock):
            noted = r["key"] in schema.DISPLAY_NOTES          # help and warnings for this display
            for f in sorted((set(r) | set(d)) - CONTEXT_FIELDS):
                if f == "choices" and (r["kind"] in DEVICE_CHOICES or noted):
                    continue
                if f in ("help", "warning_values") and noted:
                    continue
                with self.subTest(key=r["key"], field=f):
                    self.assertEqual(d.get(f), r.get(f))
            if noted and r["kind"] not in DEVICE_CHOICES:
                with self.subTest(key=r["key"], field="choice values"):
                    self.assertEqual([c["value"] for c in d["choices"]], [c["value"] for c in r["choices"]])
                    self.assertEqual([c["label"] for c in d["choices"]], [c["label"] for c in r["choices"]])

    def test_device_choices_are_the_mocks_own(self):
        m = self.mock()
        by = {d["key"]: d for d in m.settings_json()["schema"]}
        values = lambda k: [c["value"] for c in by[k]["choices"]]
        self.assertEqual(values("machine.active"), sorted(m.machines))
        self.assertEqual(values("display.host_mode"), ["auto"] + [f"{w}x{h}@{r}" for w, h, r in m.host_modes])
        self.assertEqual(values("media.cd"), ["none"] + sorted(x["id"] for x in m.MEDIA if x["can_insert"]))
        w, h, _ = m.host_mode()
        self.assertEqual(by["display.pixel_scale"]["choices"],
                         schema.pixel_scale_choices(schema.ValidationContext(display_mode=(w, h))))

    def test_hostds_defaults(self):
        m = self.mock()
        want = schema.defaults()
        self.assertIn(m.desired["machine.active"], m.machines)    # hostd: set when a Mac is installed
        self.assertEqual({k: v for k, v in m.desired.items() if k != "machine.active"},
                         {k: v for k, v in want.items() if k != "machine.active"})
        self.assertEqual(m.applied, m.desired)

    def test_every_capability_a_setting_needs_is_reported(self):
        needed = {s.capability for s in schema.SETTINGS if s.capability}
        for argv in ([], ["--profile", "laptop"]):
            with self.subTest(argv=argv):
                caps = self.mock(*argv).settings_json()["capabilities"]
                self.assertEqual(needed - set(caps), set())

    def test_hostds_validation(self):
        m = self.mock()
        self.assertEqual(m.validate({"update.url": "https://updates.example.com/feed"}), {})
        self.assertEqual(m.validate({"display.rotation": "90", "remote.screen": "view"}), {})
        self.assertEqual(list(m.validate({"machine.ram_mb": 1024})), ["machine.ram_mb"])
        self.assertEqual(list(m.validate({"management.hostname": "Retro_Boot"})), ["management.hostname"])
        self.assertEqual(m.validate({"no.such": 1}), {"no.such": "Unknown setting."})
        self.assertEqual(m.validate({"bluetooth.enabled": False}),           # no Bluetooth here
                         {"bluetooth.enabled": "Not available on this computer."})
        self.assertIn("machine.ram_mb", m.validate({"machine.cpu": "g3", "machine.ram_mb": 896}))
        self.assertIn("display.pixel_scale", m.validate({"display.pixel_scale": "4"}))   # 1366 x 768 at 4x


if __name__ == "__main__":
    unittest.main()
