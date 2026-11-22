"""The AirPort card and OpenBIOS radio patches are pinned and match their sources."""
import hashlib
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]
LOCK = json.loads((ROOT / 'emulator/manifests/sources.lock.json').read_text())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def new_file(patch_text, name):
    """Content of a file the patch creates."""
    m = re.search(r'^diff --git a/' + re.escape(name) + r' b/.*?\n@@ -0,0 \+1,\d+ @@\n(.*?)(?=^diff --git |\Z)',
                  patch_text, re.S | re.M)
    if not m:
        raise AssertionError(f'{name} not created by the patch')
    return ''.join(line[1:] + '\n' for line in m[1].splitlines() if line.startswith('+'))


class AirPortSources(unittest.TestCase):
    def test_card_patch_is_pinned_and_applied_last(self):
        entries = LOCK['retroboot_patches']
        paths = [e['path'] for e in entries]
        card = paths.index('emulator/patches/airport-hermes-card.patch')
        # Only the PMU battery, keys and pmu922 series (research/battery, research/keys, research/pmu922)
        # follow the card: their mac_newworld.c hunks sit next to the card's fw_cfg gate.  The
        # pmu922 IDE reset fix touches only hw/ide/core.c, and the graphics patch (research/gfx),
        # only hw/display/ati*; after it the relative pointer (ui, input) and hot data pages
        # (accel/tcg) patches.
        self.assertTrue(all(Path(p).name.startswith(('retroboot-pmu-', 'retroboot-ppc-', 'retroboot-adb-',
                                                     'retroboot-screamer-', 'retroboot-vga-',
                                                     'retroboot-hid-', 'retroboot-ati-', 'retroboot-ide-srst-',
                                                     'retroboot-via-', 'retroboot-relative-', 'retroboot-tcg-'))
                            or p in ('emulator/patches/retroboot-macosx-nvram.patch',
                                     'emulator/patches/retroboot-macosx-agp.patch',
                                     'emulator/patches/retroboot-macosx-flash.patch')
                            for p in paths[card + 1:]), paths[card + 1:])
        for e in entries:
            self.assertEqual(sha(ROOT / e['path']), e['sha256'], e['path'])

    def test_card_patch_carries_the_reviewed_sources(self):
        text = (ROOT / 'emulator/patches/airport-hermes-card.patch').read_text()
        for src, dest in (('research/airport/qemu/airport.c', 'hw/misc/macio/airport.c'),
                          ('research/airport/qemu/airport.h', 'include/hw/misc/macio/airport.h')):
            self.assertEqual(new_file(text, dest), (ROOT / src).read_text(), src)

    def test_char_socket_backport_is_pinned_before_the_card(self):
        # QEMU 11.0.2 aborts on every failed reconnect of a socket chardev
        # (the AirPort bridge while hostd is away); upstream ddf5738503e2.
        paths = [e['path'] for e in LOCK['retroboot_patches']]
        name = 'emulator/patches/qemu-char-socket-reconnect-yank.patch'
        self.assertIn(name, paths)
        self.assertLess(paths.index(name), paths.index('emulator/patches/airport-hermes-card.patch'))
        entry = LOCK['retroboot_patches'][paths.index(name)]
        self.assertEqual(entry['upstream']['commit'], 'ddf5738503e2ba6adb74359abf3b6e7aa81af420')
        text = (ROOT / name).read_text()
        for needle in ('ddf5738503e2', '5c102ac9', 'qemu-stable'):
            self.assertIn(needle, text.split('diff --git')[0])
        removed = [ln for ln in text.splitlines() if ln.startswith('-') and not ln.startswith('---')]
        self.assertTrue(any('yank_unregister_function' in ln for ln in removed))
        self.assertFalse([ln for ln in text.splitlines() if ln.startswith('+') and not ln.startswith('+++')])

    def test_card_survives_bridge_loss(self):
        c = (ROOT / 'research/airport/qemu/airport.c').read_text()
        self.assertIn('DEFINE_PROP_UINT32("bridge-grace-ms", RBAirportState, bridge_grace_ms, 10000)', c)
        self.assertIn('timer_new_ms(QEMU_CLOCK_REALTIME, bridge_grace_cb, s)', c)
        # the grace timer exists before the chardev handlers can report OPENED
        self.assertLess(c.index('bridge_grace_cb, s);'), c.index('qemu_chr_fe_set_handlers('))

    def test_card_is_gated_and_quiet_by_default(self):
        c = (ROOT / 'research/airport/qemu/airport.c').read_text()
        self.assertIn('DEFINE_PROP_BOOL("trace", RBAirportState, trace, false)', c)
        self.assertNotIn('scan-file', c)
        patch = (ROOT / 'emulator/patches/airport-hermes-card.patch').read_text()
        self.assertIn('DEFINE_PROP_BOOL("airport", NewWorldMacIOState, airport, false)', patch)
        self.assertIn('"etc/retroboot/airport"', patch)

    def test_openbios_build_is_pinned(self):
        ob = LOCK['firmware_build']['openbios']
        for p in ob['retroboot']['patches']:
            self.assertEqual(sha(ROOT / p['path']), p['sha256'], p['path'])
        self.assertTrue(ob['builder_image']['digest'].startswith('sha256:'))
        self.assertEqual(ob['pinned_reproduction']['elf_sha256'],
                         next(a['sha256'] for a in LOCK['assets'] if a['path'] == 'screamer/openbios-ppc'))
        radio = (ROOT / 'emulator/openbios/retroboot-airport-radio.patch').read_text()
        self.assertIn('etc/retroboot/airport', radio)
        # no static state: keeps the unpatched firmware's data layout (phandles)
        self.assertNotIn('static int', radio)


if __name__ == '__main__':
    unittest.main()
