"""The screamer's sound-DMA patches (emulator/reports/qtaudio-20261012.md,
qtaudio2-20261012.md) are pinned in order with their hashes and touch only the
screamer and the DBDMA engine."""
import hashlib
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
LOCK = json.loads((ROOT / 'emulator/manifests/sources.lock.json').read_text())
SERIES = ['emulator/patches/retroboot-screamer-idle-stop.patch',
          'emulator/patches/retroboot-screamer-paced-dma.patch',
          'emulator/patches/retroboot-screamer-realtime.patch',
          'emulator/patches/retroboot-screamer-irq-hold.patch',
          'emulator/patches/retroboot-screamer-lead-strict.patch']
ALLOWED = {'hw/audio/screamer.c', 'include/hw/audio/screamer.h', 'hw/audio/trace-events',
           'hw/misc/macio/mac_dbdma.c', 'include/hw/ppc/mac_dbdma.h'}


def touched(name):
    return [ln.split()[2][2:] for ln in (ROOT / name).read_text().splitlines() if ln.startswith('diff --git')]


class ScreamerSources(unittest.TestCase):
    def test_order_hashes_files(self):
        paths = [e['path'] for e in LOCK['retroboot_patches']]
        idx = [paths.index(p) for p in SERIES]
        self.assertEqual(idx, sorted(idx))
        for p in SERIES[2:]:
            e = LOCK['retroboot_patches'][paths.index(p)]
            self.assertEqual(e['sha256'], hashlib.sha256((ROOT / p).read_bytes()).hexdigest())
            self.assertEqual(sorted(e['files']), sorted(touched(p)))
            self.assertTrue(set(touched(p)) <= ALLOWED, touched(p))

    def test_off_switches(self):
        text = (ROOT / SERIES[2]).read_text() + (ROOT / SERIES[3]).read_text()
        for prop in ('dma-lead-us', 'resync-ms', 'host-lead-ms', 'host-lead-max-ms', 'irq-hold-ms'):
            self.assertIn(f'DEFINE_PROP_UINT32("{prop}"', text)
        # the strict pre-roll is opt-in (hostd: Gemini PDA only)
        self.assertIn('DEFINE_PROP_BOOL("host-lead-strict", ScreamerState, host_lead_strict, false)',
                      (ROOT / SERIES[4]).read_text())


if __name__ == '__main__':
    unittest.main()
