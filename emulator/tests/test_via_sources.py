"""VIA timer 2 holds an interrupt that falls due while the vCPU translates code
(emulator/reports/viacal-20261013.md): pinned after the branch caches and before the
ATI card, on by default on both Mac VIAs, with an off switch that restores r17, and
the translation bookkeeping inert until a VIA asks for it."""
import hashlib
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]
LOCK = json.loads((ROOT / 'emulator/manifests/sources.lock.json').read_text())
PATCH = 'emulator/patches/retroboot-via-t2-hold.patch'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def touched(name):
    return re.findall(r'^diff --git a/(\S+) ', (ROOT / name).read_text(), re.M)


def added(name, path=None):
    out, cur = [], None
    for ln in (ROOT / name).read_text().splitlines():
        if ln.startswith('diff --git a/'):
            cur = ln.split()[2][2:]
        elif ln.startswith('+') and not ln.startswith('+++') and (path is None or cur == path):
            out.append(ln[1:])
    return '\n'.join(out)


class VIAHoldSources(unittest.TestCase):
    def test_pinned_in_order(self):
        paths = [e['path'] for e in LOCK['retroboot_patches']]
        i = paths.index(PATCH)
        self.assertGreater(i, paths.index('emulator/patches/retroboot-ppc-branch-cache.patch'))
        self.assertGreater(i, paths.index('emulator/patches/retroboot-cuda-via-timers.patch'))
        self.assertGreater(i, paths.index('emulator/patches/retroboot-pmu-via-timers.patch'))
        self.assertGreater(paths.index('emulator/patches/retroboot-ati-r100.patch'), i)   # the ATI card after
        entry = LOCK['retroboot_patches'][i]
        self.assertEqual(entry['sha256'], sha(ROOT / PATCH))
        self.assertEqual(sorted(entry['files']), sorted(touched(PATCH)))

    def test_switches_default_on_for_both_vias(self):
        for path in ('hw/misc/macio/cuda.c', 'hw/misc/macio/pmu.c'):
            text = added(PATCH, path)
            self.assertIn('DEFINE_PROP_BOOL("t2-hold", MOS6522State, t2_hold, true)', text, path)
            self.assertIn('"x-t2-hold-quiet-us", MOS6522State, t2_hold_quiet_us, 100)', text, path)
            self.assertIn('"x-t2-hold-max-us", MOS6522State, t2_hold_max_us, 4000)', text, path)
        self.assertIn("'mos6522-cuda', 'mos6522-pmu'", (ROOT / 'emulator/scripts/verify_package.py').read_text())
        self.assertIn("'via-t2-hold','retroboot-via-t2-hold.patch'",
                      (ROOT / 'emulator/scripts/package_manifest.py').read_text())

    def test_only_timer2_interrupts_and_bounded(self):
        text = added(PATCH, 'hw/misc/mos6522.c')
        # only while timer 2's interrupt is enabled; the counter keeps host time
        self.assertIn('if (!s->t2_hold || !(s->ier & T2_INT)) {', text)
        self.assertNotIn('get_counter_value', text)
        # bounded: at most t2_hold_max_us after it fell due
        self.assertIn('if (now - s->t2_held_since >= (int64_t)s->t2_hold_max_us * SCALE_US) {', text)
        # the main-loop callback and VIA reads both consult the hold
        self.assertEqual(text.count('mos6522_t2_hold(s'), 2)
        # a new load of timer 2 ends any hold
        self.assertIn('s->t2_held_since = 0;', text)

    def test_bookkeeping_is_inert_until_requested(self):
        text = added(PATCH, 'system/cpu-timers.c')
        self.assertIn('if (!tcg_enabled() || icount_enabled() || qemu_get_cpu(1)) {', text)
        self.assertIn('if (!qatomic_load_acquire(&xlate.enabled)) {', text)
        tcg = added(PATCH, 'accel/tcg/translate-all.c')
        self.assertIn('cpu_translation_begin();', tcg)
        self.assertIn('cpu_translation_end();', tcg)
        # a translation abandoned by a longjmp is closed in the cpu_exec cleanup
        self.assertIn('cpu_translation_end();', added(PATCH, 'accel/tcg/cpu-exec.c'))


if __name__ == '__main__':
    unittest.main()
