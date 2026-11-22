"""The PMU battery, keys and 9.2.2 fixes (research/battery, research/keys,
research/pmu922) are
pinned, ordered and keep their defaults: off unless asked for, the firmware
gates, and only prim-info and the backlight node change."""
import hashlib
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]
LOCK = json.loads((ROOT / 'emulator/manifests/sources.lock.json').read_text())
SERIES = ['emulator/patches/retroboot-pmu-adb-reply.patch', 'emulator/patches/retroboot-pmu-battery.patch',
          'emulator/patches/retroboot-ppc-msscr0-flush.patch', 'emulator/patches/retroboot-pmu-via-timers.patch',
          'emulator/patches/retroboot-adb-buttons.patch', 'emulator/patches/retroboot-pmu-backlight.patch',
          'emulator/patches/retroboot-screamer-volume.patch', 'emulator/patches/retroboot-vga-builtin-panel.patch',
          'emulator/patches/retroboot-pmu-sleep-irq.patch', 'emulator/patches/retroboot-adb-burst.patch',
          'emulator/patches/retroboot-ide-srst-no-irq.patch']
OPENBIOS = 'emulator/openbios/retroboot-pmu.patch'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def added(name):
    return [ln[1:] for ln in (ROOT / name).read_text().splitlines() if ln.startswith('+') and not ln.startswith('+++')]


class PMUSources(unittest.TestCase):
    def test_series_is_pinned_in_order_after_the_card(self):
        paths = [e['path'] for e in LOCK['retroboot_patches']]
        idx = [paths.index(p) for p in SERIES]
        self.assertEqual(idx, sorted(idx))
        self.assertGreater(idx[0], paths.index('emulator/patches/airport-hermes-card.patch'))
        for e in LOCK['retroboot_patches']:
            if e['path'] in SERIES:
                self.assertEqual(sha(ROOT / e['path']), e['sha256'], e['path'])
        ob = LOCK['firmware_build']['openbios']['retroboot']['patches']
        obp = [e['path'] for e in ob]
        self.assertGreater(obp.index(OPENBIOS), obp.index('emulator/patches/openbios-sound-objects-terminator.patch'))
        self.assertEqual(sha(ROOT / OPENBIOS), ob[obp.index(OPENBIOS)]['sha256'])

    def test_defaults(self):
        text = '\n'.join(added('emulator/patches/retroboot-pmu-battery.patch') +
                         added('emulator/patches/retroboot-pmu-adb-reply.patch'))
        self.assertIn('DEFINE_PROP_BOOL("battery", PMUState, battery, false)', text)
        self.assertIn('DEFINE_PROP_BOOL("battery-trace", PMUState, battery_trace, false)', text)
        self.assertIn('DEFINE_PROP_BOOL("sleep", PMUState, sleep_wake, true)', text)
        self.assertIn('DEFINE_PROP_BOOL("adb-reply-cmd", PMUState, adb_reply_cmd, true)', text)
        # the firmware's battery bay exists only with the property
        self.assertIn('"etc/retroboot/battery"', text)
        # the bridge speaks the documented messages
        for needle in (r'\"type\":\"hello\"', r'\"type\":\"sleep\"', r'\"type\":\"wake\"',
                       '!strcmp(type, "battery")', '!strcmp(type, "wake")'):
            self.assertIn(needle, text, needle)

    def test_keys_defaults(self):
        pmu = '\n'.join(added('emulator/patches/retroboot-pmu-backlight.patch'))
        self.assertIn('DEFINE_PROP_BOOL("backlight", PMUState, backlight, false)', pmu)
        self.assertIn('"etc/retroboot/backlight"', pmu)
        self.assertIn('if (!s->backlight || in_len < 1) {', pmu)   # off: the old (empty) reply only
        self.assertIn(r'\"type\":\"backlight\"', pmu)
        btn = '\n'.join(added('emulator/patches/retroboot-adb-buttons.patch'))
        self.assertIn('DEFINE_PROP_UINT8("handler", ADBButtonsState, handler_prop, ADB_BUTTONS_HANDLER_POWERBOOK)', btn)
        self.assertIn('#define ADB_BUTTONS_HANDLER_POWERBOOK  0x1f', btn)
        self.assertIn('#define ADB_BUTTONS_HANDLER_ADJUSTABLE 0x02', btn)
        # Mac OS 9's codes: PowerBook 6/7/8 volume up/down/mute, 9/a brightness, b eject; AppleDesign 3/2/1
        for row in ('{ "volume-up",       0x06, 0x03 }', '{ "volume-down",     0x07, 0x02 }',
                    '{ "mute",            0x08, 0x01 }', '{ "brightness-up",   0x09, 0 }',
                    '{ "brightness-down", 0x0a, 0 }', '{ "eject",           0x0b, 0 }'):
            self.assertIn(row, btn)
        vga = '\n'.join(added('emulator/patches/retroboot-vga-builtin-panel.patch'))
        self.assertIn('DEFINE_PROP_BOOL("builtin-panel", PCIVGAState, builtin_panel, false)', vga)
        self.assertIn('#define  PCI_VGA_QEXT_FEATURE_RETROBOOT_BUILTIN_PANEL  0x10000', vga)
        ndrv = (ROOT / 'emulator/ndrv/retroboot-builtin-panel.patch').read_text()
        self.assertIn('#define QEMU_EXT_FEATURE_RETROBOOT_BUILTIN_PANEL 0x10000', ndrv)   # the same bit
        self.assertIn('#ifdef RETROBOOT_BUILTIN_PANEL', ndrv)
        scr = '\n'.join(added('emulator/patches/retroboot-screamer-volume.patch'))
        self.assertIn('if (addr == 0x1 || addr == 0x4) {', scr)

    def test_sleep_keeps_the_interrupt_line_quiet(self):
        """research/pmu922: Mac OS 9.2.2 hung after a wake when a PMU
        interrupt was pending at its PMU_SLEEP; the line now drops at the
        sleep and what is pending is delivered after the wake."""
        text = (ROOT / 'emulator/patches/retroboot-pmu-sleep-irq.patch').read_text()
        plus = '\n'.join(added('emulator/patches/retroboot-pmu-sleep-irq.patch'))
        self.assertIn('if ((s->intbits & s->intmask) != 0 && !s->sleeping) {', plus)
        self.assertEqual(plus.count('pmu_update_extirq(s);'), 2)       # at PMU_SLEEP and at the wake
        self.assertEqual(len([ln for ln in text.splitlines() if ln.startswith('-') and not ln.startswith('---')]), 1)

    def test_burst_fixes(self):
        """research/pmu922: a command reply never overwrites pending autopoll
        data, and the buttons queue never drops a release."""
        plus = '\n'.join(added('emulator/patches/retroboot-adb-burst.patch'))
        self.assertIn('bool behind = s->adb_reply_cmd && (s->intbits & PMU_INT_ADB_AUTO);', plus)
        self.assertIn('if (s->adb_cmd_pending) {', plus)                 # INT ACK: the reply follows the data
        self.assertIn('.name = "pmu/retroboot-adb-cmd-reply",', plus)
        self.assertIn('s->count + 2 + ctpop32(s->held) > ADB_BUTTONS_QUEUE', plus)
        self.assertIn('} else if (s->held & (1u << button)) {', plus)    # a release only after its press

    def test_software_reset_raises_no_interrupt(self):
        """research/pmu922: the residual 9.2.2 wake hang.  A software reset
        sets the diagnostic's registers without its interrupt and negates
        INTRQ; EXECUTE DEVICE DIAGNOSTIC still interrupts (hard disks)."""
        paths = [e['path'] for e in LOCK['retroboot_patches']]
        self.assertGreater(paths.index('emulator/patches/retroboot-ide-srst-no-irq.patch'),
                           paths.index('emulator/patches/retroboot-macio-ide-srst-complete.patch'))
        text = (ROOT / 'emulator/patches/retroboot-ide-srst-no-irq.patch').read_text()
        minus = [ln[1:] for ln in text.splitlines() if ln.startswith('-') and not ln.startswith('---')]
        plus = '\n'.join(added('emulator/patches/retroboot-ide-srst-no-irq.patch'))
        self.assertIn('    cmd_exec_dev_diagnostic(s, WIN_DIAGNOSE);', minus)   # the reset's old interrupt
        self.assertIn('static void ide_set_diagnostic_result(IDEState *s)', plus)
        self.assertIn('    ide_set_diagnostic_result(s);\n    if (s->drive_kind != IDE_CD) {\n'
                      '        ide_bus_set_irq(s->bus);', plus)                  # the command keeps it
        self.assertIn('    qemu_irq_lower(bus->irq);', plus)
        self.assertEqual(plus.count('ide_bus_set_irq'), 1)

    def test_pmu_via_timers_follow_the_cuda_fix(self):
        paths = [e['path'] for e in LOCK['retroboot_patches']]
        self.assertGreater(paths.index('emulator/patches/retroboot-pmu-via-timers.patch'),
                           paths.index('emulator/patches/retroboot-cuda-via-timers.patch'))
        text = '\n'.join(added('emulator/patches/retroboot-pmu-via-timers.patch'))
        self.assertIn('DEFINE_PROP_BOOL("coherent-timers", MOS6522State, coherent_timers, true)', text)

    def test_cpu_fix_is_limited_to_dl1hwf(self):
        text = '\n'.join(added('emulator/patches/retroboot-ppc-msscr0-flush.patch'))
        self.assertIn('~(target_ulong)0x00800000', text)
        self.assertIn('&spr_read_generic, &spr_write_msscr0,', text)

    def test_openbios_changes_only_prim_info_behind_the_gate(self):
        lines = added(OPENBIOS)
        code = [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith(('/*', '*'))]
        self.assertIn('#define RETROBOOT_BATTERY_FW_CFG "etc/retroboot/battery"', code)
        self.assertIn('#define RETROBOOT_BACKLIGHT_FW_CFG "etc/retroboot/backlight"', code)
        self.assertIn('prim[25] = 0x01;', code)
        self.assertIn('prim[18] &= ~0x20;', code)
        writes = [ln for ln in code if re.match(r'prim\[\d+\]', ln)]
        self.assertEqual(writes, ['prim[25] = 0x01;', 'prim[18] &= ~0x20;', 'prim[18] |= 0x02;'])
        # the backlight node only behind its gate
        text = '\n'.join(code)
        self.assertIn('if (pmu_retroboot_backlight_present()) {\nbacklight_init();', text)
        self.assertNotIn('static int retroboot', ' '.join(code).replace('static int pmu_retroboot', ''))


if __name__ == '__main__':
    unittest.main()
