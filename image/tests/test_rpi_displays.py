"""A Pi installation must retain its chosen panel and have offline recovery files."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

IMAGE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('rpi_display_layout', IMAGE / 'rootfs/usr/lib/retroboot/installer/rpi_layout.py')
layout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(layout)


class DisplayPayload(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.root = Path(self.td.name) / 'root'
        self.base = self.root / 'usr/lib/retroboot/boot'
        self.base.mkdir(parents=True)
        self.target = Path(self.td.name) / 'card'
        self.target.mkdir()

    def payload(self):
        (self.base / 'config.txt').write_text('[all]\ninclude retroboot/display.txt\n')
        for rel, text in {'DISPLAY-SETUP.txt': 'Recovery instructions',
                          'retroboot/display-profiles.json': '{"schema":1}',
                          'retroboot/display.txt': 'Auto preset',
                          'retroboot/displays/auto.txt': 'Auto preset',
                          'retroboot/displays/hdmi.txt': 'HDMI preset',
                          'retroboot/displays/hyperpixel4.txt': 'HyperPixel preset'}.items():
            p = self.base / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)

    def test_missing_recovery_payload_fails_before_target_changes(self):
        self.payload()
        (self.base / 'retroboot/displays/auto.txt').unlink()
        sentinel = self.target / 'config.txt'
        sentinel.write_text('Keep existing startup configuration')
        with self.assertRaises(layout.LayoutError):
            layout.copy_display_files(str(self.root), str(self.target))
        self.assertEqual(sentinel.read_text(), 'Keep existing startup configuration')
        self.assertFalse((self.target / 'retroboot').exists())

    def test_free_space_install_preserves_display_but_refreshes_recovery_files(self):
        self.payload()
        chosen = self.target / 'retroboot/display.txt'
        chosen.parent.mkdir()
        chosen.write_text('The user selected HyperPixel')
        layout.copy_display_files(str(self.root), str(self.target), keep_selection=True)
        self.assertEqual(chosen.read_text(), 'The user selected HyperPixel')
        self.assertEqual((self.target / 'retroboot/displays/auto.txt').read_text(), 'Auto preset')
        self.assertTrue((self.target / 'DISPLAY-SETUP.txt').is_file())

    def test_other_disk_install_copies_all_display_and_recovery_files(self):
        self.payload()
        layout.copy_display_files(str(self.root), str(self.target))
        for p in self.base.rglob('*'):
            if p.is_file() and p.name != 'config.txt':
                self.assertEqual((self.target / p.relative_to(self.base)).read_bytes(), p.read_bytes())

    def test_older_roots_without_display_include_are_unchanged(self):
        (self.base / 'config.txt').write_text('kernel=kernel8.img\n')
        layout.copy_display_files(str(self.root), str(self.target))
        self.assertEqual(list(self.target.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
