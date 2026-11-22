"""The emulated ATI Radeon R100 (research/gfx, experimental) is pinned last in
the series, touches only the ati-vga device, adds no link dependency, and the
firmware's changes apply only to that card: with the option off (std-VGA) the
device tree is the recorded card-off tree."""
import hashlib
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
LOCK = json.loads((ROOT / 'emulator/manifests/sources.lock.json').read_text())
QEMU_PATCH = 'emulator/patches/retroboot-ati-r100.patch'
OPENBIOS = 'emulator/openbios/retroboot-ati-radeon.patch'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def added(name):
    return [ln[1:] for ln in (ROOT / name).read_text().splitlines() if ln.startswith('+') and not ln.startswith('+++')]


def touched(name):
    return [ln.split()[2][2:] for ln in (ROOT / name).read_text().splitlines() if ln.startswith('diff --git')]


class ATISources(unittest.TestCase):
    def test_osx_agp_is_a_pinned_opt_in_extension(self):
        name = 'emulator/patches/retroboot-macosx-agp.patch'
        entry = next(e for e in LOCK['retroboot_patches'] if e['path'] == name)
        self.assertEqual(entry['sha256'], sha(ROOT/name))
        self.assertEqual(set(entry['files']), set(touched(name)))
        self.assertEqual(set(entry['files']), {'hw/pci-host/uninorth.c',
                         'include/hw/pci-host/uninorth.h', 'hw/display/ati.c',
                         'hw/display/ati_int.h'})
        code = '\n'.join(added(name))
        self.assertIn('DEFINE_PROP_BOOL("x-agp-compat", UNINHostState, agp_compat, false)', code)
        self.assertIn('DEFINE_PROP_BOOL("x-agp", ATIVGAState, agp, false)', code)
        self.assertIn('devfn == s->agp_devfn', code)
        self.assertIn('entry.perm = IOMMU_NONE', code)

    def test_pinned_last_and_hashes(self):
        series = LOCK['retroboot_patches']
        i = [e['path'] for e in series].index(QEMU_PATCH)
        # Later patches (relative pointer, hot data pages) never touch the card's files.
        ati_files = set(series[i]['files'])
        self.assertFalse([e['path'] for e in series[i + 1:]
                          if ati_files & set(e['files']) and
                          e['path'] != 'emulator/patches/retroboot-macosx-agp.patch'])
        self.assertEqual(series[i]['sha256'], sha(ROOT / QEMU_PATCH))
        self.assertEqual(sorted(series[i]['files']), sorted(touched(QEMU_PATCH)))
        ob = LOCK['firmware_build']['openbios']['retroboot']['patches']
        self.assertEqual(ob[-1]['path'], OPENBIOS)
        self.assertEqual(ob[-1]['sha256'], sha(ROOT / OPENBIOS))

    def test_only_the_ati_device(self):
        for f in touched(QEMU_PATCH):
            self.assertTrue(f.startswith('hw/display/ati') or f in ('hw/display/meson.build',
                                                                    'hw/display/trace-events'), f)
        text = (ROOT / QEMU_PATCH).read_text()
        meson_hunk = text[text.index('diff --git a/hw/display/meson.build'):]
        meson_hunk = meson_hunk[:meson_hunk.index('diff --git', 10)]
        plus = '\n'.join(ln for ln in meson_hunk.splitlines() if ln.startswith('+'))
        for f in ('ati_cp.c', 'ati_3d.c', 'ati_glr.c', 'ati_gl.c'):
            self.assertIn(f, plus)
        # no new link dependency: host GL is dlopen()ed when the card is realized
        self.assertNotIn('dependency(', plus)
        gl = '\n'.join(added(QEMU_PATCH))
        self.assertIn('dlopen', gl)
        self.assertIn('libEGL.so.1', gl)
        self.assertIn('EGL_PLATFORM_SURFACELESS_MESA', gl)

    def test_r100_model_and_defaults(self):
        code = '\n'.join(added(QEMU_PATCH))
        self.assertIn('{ "r100", PCI_DEVICE_ID_ATI_RADEON_QD }', code.replace('0x5144', 'PCI_DEVICE_ID_ATI_RADEON_QD'))
        self.assertIn('#define PCI_DEVICE_ID_ATI_RADEON_QD 0x5144', code)
        self.assertIn('DEFINE_PROP_UINT32("x-debug", ATIVGAState, debug, 0)', code)
        self.assertIn('"x-accel-stats"', code)
        self.assertIn('RETROBOOT_ATI_3D', code)      # off switch for the host renderer

    def test_firmware_only_for_the_card(self):
        code = [ln.strip() for ln in added(OPENBIOS)]
        text = '\n'.join(code)
        self.assertIn('#define PCI_DEVICE_ID_ATI_RADEON_QD      0x5144', text)
        self.assertIn('NULL, "ATY,Rage6p", "ATY,Rage6p", "ATY,Rage6p\\0",', text)
        # every register write and the driver property removal sit behind is_ati_radeon()
        self.assertEqual(text.count('if (is_ati_radeon(config)) {'), 2)
        self.assertNotIn('fw_cfg', text)
        dt = LOCK['firmware_build']['openbios']['retroboot']['device_tree']
        self.assertEqual(dt['ati_off_sha256'], dt['card_off_sha256'])
        self.assertTrue(dt['ati_off_equals_card_off'])
        self.assertTrue(dt['ati_pmu_off_equals_pmu_off'])

    def test_edid_modes(self):
        """The monitor's EDID is built again at realize, after xres/yres are set, so ATI's
        driver offers the Mac screen hostd asks for (feature ati-edid-modes)."""
        code = '\n'.join(added(QEMU_PATCH))
        self.assertIn('qemu_edid_generate(s->i2cddc.edid_blob, sizeof(s->i2cddc.edid_blob),', code)
        manifest = (ROOT / 'emulator/scripts/package_manifest.py').read_text()
        self.assertIn("'ati-edid-modes'", manifest)
        self.assertIn("'qemu_edid_generate(s->i2cddc.edid_blob'", manifest)
        verify = (ROOT / 'emulator/scripts/verify_package.py').read_text()
        self.assertIn("checks['ati_edid_modes']='pass'", verify)
        self.assertIn("'--edid-only'", verify)

    def test_package_feature_and_verification(self):
        manifest = (ROOT / 'emulator/scripts/package_manifest.py').read_text()
        self.assertIn("'ati-r100-1'", manifest)
        verify = (ROOT / 'emulator/scripts/verify_package.py').read_text()
        self.assertIn("output('-device','ati-vga,help')", verify)
        self.assertIn("b'ATY,Rage6p'", verify)


if __name__ == '__main__':
    unittest.main()
