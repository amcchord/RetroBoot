"""The Quadra 800 package's sources (emulator/manifests/m68k.lock.json, D044):
the same pins as the PowerPC lock, which it leaves alone; hashes and file lists
of its patch series; the firmware recipe; the contract argv the lab proves; and
the lab tools' parsers on synthetic data (no Apple code, no QEMU)."""
import hashlib
import json
from pathlib import Path
import re
import struct
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
M68K = json.loads((ROOT / 'emulator/manifests/m68k.lock.json').read_text())
PPC = json.loads((ROOT / 'emulator/manifests/sources.lock.json').read_text())
sys.path.insert(0, str(ROOT / 'emulator/tools'))
sys.path.insert(0, str(ROOT / 'emulator/scripts'))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def touched(name):
    return re.findall(r'^\+\+\+ b/(\S+)', (ROOT / name).read_text(), re.M)


def added(name, path=None):
    out, cur = [], None
    for ln in (ROOT / name).read_text().splitlines():
        if ln.startswith('diff --git a/'):
            cur = ln.split()[2][2:]
        elif ln.startswith('+') and not ln.startswith('+++') and (path is None or cur == path):
            out.append(ln[1:])
    return '\n'.join(out)


class M68kLock(unittest.TestCase):
    def test_same_pins_as_powerpc(self):
        self.assertEqual(M68K['classicmac'], PPC['classicmac'])
        self.assertEqual(M68K['qemu'], PPC['qemu'])
        self.assertEqual(M68K['target_list'], 'm68k-softmmu')

    def test_powerpc_series_untouched(self):
        ppc = {e['path']: e['sha256'] for e in PPC['retroboot_patches']}
        for e in M68K['retroboot_patches']:
            if e['shared_with_ppc']:
                self.assertEqual(ppc[e['path']], e['sha256'], e['path'])
            else:
                self.assertNotIn(e['path'], ppc, e['path'])
        for e in PPC['retroboot_patches']:      # the PowerPC lock still verifies
            self.assertEqual(sha(ROOT / e['path']), e['sha256'], e['path'])

    def test_patch_hashes_and_files(self):
        for e in M68K['retroboot_patches']:
            self.assertEqual(sha(ROOT / e['path']), e['sha256'], e['path'])
            self.assertEqual(sorted(e['files']), sorted(set(touched(e['path']))), e['path'])
        inventory = {p['path']: p['sha256'] for p in PPC['inventory']}
        for e in M68K['classicmac_patches']:
            self.assertEqual(inventory[e['path']], e['sha256'], e['path'])

    def test_series_order(self):
        names = [Path(e['path']).name for e in M68K['retroboot_patches']]
        self.assertEqual(names[:4], ['retroboot-display.patch', 'retroboot-display-idle-refresh.patch',
                                     'qemu-char-socket-reconnect-yank.patch', 'retroboot-relative-pointer.patch'])
        self.assertLess(names.index('retroboot-qfb-resize.patch'), names.index('retroboot-qfb-lengths.patch'))
        self.assertLess(names.index('retroboot-qfb-lengths.patch'), names.index('retroboot-qfb-vbl-sync.patch'))
        self.assertLess(names.index('retroboot-qfb-vbl-sync.patch'), names.index('retroboot-qfb-refresh.patch'))
        self.assertEqual(names.index('retroboot-qfb-millions.patch'), names.index('retroboot-qfb-refresh.patch') + 1)
        # hot data pages (PowerPC series, byte-identical) after notdirty-fast: both on the code-page store path
        self.assertEqual(names.index('retroboot-tcg-hot-data-pages.patch'),
                         names.index('retroboot-tcg-notdirty-fast.patch') + 1)
        # the hold's VIA1 properties sit after the ADB ones in mac_via.c
        self.assertEqual(names[-3:], ['retroboot-q800-adb-idle.patch', 'retroboot-q800-t2-hold.patch',
                                     'retroboot-q800-t2-slow-host.patch'])

    def test_firmware_recipe(self):
        fw = M68K['firmware']
        self.assertEqual(set(fw), {'mac_qfb.rom', 'classicvirtio-68k.rom', 'q800-pram-seed.img'})
        self.assertEqual(fw['mac_qfb.rom']['pinned_unmodified_sha256'],
                         next(a['sha256'] for a in M68K['assets'] if a['path'] == 'qfb/mac_qfb.rom'))
        for p in fw['mac_qfb.rom']['patches']:
            self.assertEqual(sha(ROOT / p['path']), p['sha256'])
        script = (ROOT / 'emulator/qfb/build-qfb-rom.sh').read_text()
        self.assertIn(fw['mac_qfb.rom']['sha256'], script)
        self.assertIn(fw['mac_qfb.rom']['pinned_unmodified_sha256'], script)
        script = (ROOT / 'emulator/classicvirtio/build-declrom-68k.sh').read_text()
        self.assertIn(fw['classicvirtio-68k.rom']['sha256'], script)
        self.assertNotIn('retroboot-location', script.split('PATCHES=(')[1].split(')')[0])
        self.assertEqual(fw['q800-pram-seed.img']['bytes'], 256)

    def test_features_cover_the_contract(self):
        import package_manifest_m68k as pm
        cm = [e['path'] for e in M68K['classicmac_patches']]
        names = [Path(e['path']).name for e in M68K['retroboot_patches']]
        features = {f for f, p in pm.CLASSICMAC_FEATURES.items() if p in cm}
        features |= {f for f, p in pm.PATCH_FEATURES.items() if p in names}
        features |= {f for f, p in M68K['feature_patches'].items() if p in names}
        features |= {'classicvirtio-68k'}
        for f in ('q800', 'nubus-qfb', 'nubus-qfb-resize', 'asc-idle-silence', 'classicvirtio-68k',
                  'display-retroboot', 'display-relative-pointer', 'q800-restart-reset',
                  'nubus-qfb-snapshot-mode', 'q800-t2-hold-slow-host'):
            self.assertIn(f, features)

    def test_ppc_scripts_hand_m68k_packages_over(self):
        self.assertIn("verify_package_m68k.py", (ROOT / 'emulator/scripts/verify_package.py').read_text())
        self.assertIn("package_manifest_m68k.py", (ROOT / 'emulator/scripts/package_manifest.py').read_text())
        build = (ROOT / 'emulator/scripts/build-linux-m68k.sh').read_text()
        self.assertIn('--target-list=m68k-softmmu', build)
        self.assertIn('--enable-werror', build)
        self.assertIn('--audio-drv-list=alsa', build)
        self.assertIn('--enable-virtfs', build)


class M68kPatches(unittest.TestCase):
    def test_qfb_lengths_fit_the_card(self):
        text = added('emulator/patches/retroboot-qfb-lengths.patch')
        self.assertIn('max = NUBUS_SLOT_SIZE - QFB_SCREEN_BASE_OFFSET;', text)
        self.assertIn('max = QFB_VRAM_SIZE - QFB_SCREEN_BASE_OFFSET;', text)
        self.assertIn('(QFB_VRAM_SIZE - QFB_SCREEN_BASE_OFFSET)', text)

    def test_restart_only_after_the_first_reset_instruction(self):
        p = 'emulator/patches/retroboot-q800-restart-reset.patch'
        self.assertIn('if (cpu->restart_resets && cpu->reset_insns++) {', added(p, 'target/m68k/helper.c'))
        self.assertIn('qemu_system_reset_request(SHUTDOWN_CAUSE_GUEST_RESET);', added(p, 'target/m68k/helper.c'))
        self.assertIn('DEFINE_PROP_BOOL("restart-resets", M68kCPU, restart_resets, false)', added(p, 'target/m68k/cpu.c'))
        self.assertIn('M68K_CPU(obj)->reset_insns = 0;', added(p, 'target/m68k/cpu.c'))
        self.assertIn('{ M68K_CPU_TYPE_NAME("m68040"), "restart-resets", "on" },', added(p, 'hw/m68k/q800.c'))

    def test_notdirty_keeps_the_original_path_when_code_is_gone(self):
        p = 'emulator/patches/retroboot-tcg-notdirty-fast.patch'
        text = (ROOT / p).read_text()
        self.assertIn('\n         tb_invalidate_phys_range_fast(cpu, ram_addr, size, retaddr);', text)  # unchanged
        new = added(p, 'accel/tcg/cputlb.c')
        self.assertIn('physical_memory_set_dirty_range(ram_addr, size, clean);', new)
        self.assertNotIn('tlb_set_dirty', new)
        self.assertNotIn('\n-    physical_memory_set_dirty_range', text)   # the code-free path stays

    def test_idle_switches(self):
        self.assertIn('DEFINE_PROP_UINT32("vbl-ns", QfbNubusState, qfb.vbl_ns, 16625800),',
                      added('emulator/patches/retroboot-qfb-vbl-sync.patch'))
        adb = added('emulator/patches/retroboot-q800-adb-idle.patch', 'hw/misc/mac_via.c')
        self.assertIn('adb_idle_poll_ms, 100),', adb)
        self.assertIn('adb_idle_after_ms, 1000),', adb)
        # properties into the bus would be zeroed by qbus_init() after their defaults
        self.assertNotIn('adb_bus.', adb)
        self.assertIn('adb_bus->idle_rate_ms = v1s->adb_idle_poll_ms;', adb)
        self.assertIn('DEFINE_PROP_UINT8("adb-poll-ms", MOS6522Q800VIA1State, adb_poll_ms, 11),', adb)
        # the ADB bus's reset (every system reset, after realize) restores the VIA's period
        self.assertIn('adb_bus->reset_rate_ms = v1s->adb_poll_ms;', adb)
        self.assertIn('adb_bus->autopoll_rate_ms = adb_bus->reset_rate_ms ?: 20;',
                      added('emulator/patches/retroboot-q800-adb-idle.patch', 'hw/input/adb.c'))


    def test_hot_data_pages_compose_with_notdirty_fast(self):
        nd = (ROOT / 'emulator/patches/retroboot-tcg-notdirty-fast.patch').read_text()
        # a drop that leaves the page without code is seen by the re-read and takes QEMU's path
        self.assertIn('\n         tb_invalidate_phys_range_fast(cpu, ram_addr, size, retaddr);\n'
                      '+        bits = physical_memory_page_dirty_clients(ram_addr);\n', nd)
        hot = added('emulator/patches/retroboot-tcg-hot-data-pages.patch', 'accel/tcg/tb-maint.c')
        self.assertIn('if (hot_data_store(cpu, p, index, ra)) {', hot)
        self.assertNotIn('accel/tcg/cputlb.c', touched('emulator/patches/retroboot-tcg-hot-data-pages.patch'))

    def test_qfb_millions(self):
        qfb = added('emulator/patches/retroboot-qfb-millions.patch', 'hw/display/mac_qfb.c')
        self.assertIn('        case 32:', qfb)
        self.assertIn('            s->depth = 24;', qfb)

    def test_t2_hold(self):
        p = 'emulator/patches/retroboot-q800-t2-hold.patch'
        via = added(p, 'hw/misc/mac_via.c')
        self.assertIn('DEFINE_PROP_BOOL("t2-hold", MOS6522Q800VIA1State, parent_obj.t2_hold, true),', via)
        self.assertIn('parent_obj.t2_hold_quiet_us, 100),', via)
        self.assertIn('parent_obj.t2_hold_max_us, 4000),', via)
        core = added(p, 'hw/misc/mos6522.c')
        # both ways a due timer-2 interrupt is raised wait for the hold
        self.assertIn('if (mos6522_t2_hold(s, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL))) {', core)
        self.assertIn('if (now >= s->timers[1].next_irq_time && !mos6522_t2_hold(s, now)) {', core)
        self.assertIn('s->t2_watch = cpu_translation_watch();', core)
        # the translation bookkeeping is the PowerPC patch's, unchanged
        for f in ('system/cpu-timers.c', 'accel/tcg/translate-all.c', 'accel/tcg/cpu-exec.c'):
            self.assertEqual(added(p, f), added('emulator/patches/retroboot-via-t2-hold.patch', f), f)
        self.assertNotIn('RB_XLATE_DELAY', (ROOT / p).read_text())     # the lab-only knob stays out


class LabTools(unittest.TestCase):
    def test_contract_argv_order(self):
        import rbd_quadra
        a = rbd_quadra.contract_argv(Path('/p/bin/q'), Path('/p/share/qemu'), Path('/m/Quadra800.rom'),
                                     Path('/m/pram.img'), Path('/m/disk.img'), Path('/r'), ram_mb=128,
                                     width=800, height=600, depth=8, share=Path('/s'), thread_names=False)
        line = ' '.join(a)
        order = ['-M q800,fb=none,audiodev=snd0', '-m 128', '-bios /m/Quadra800.rom', '-L /p/share/qemu',
                 'file=/m/pram.img,format=raw,if=mtd', 'nubus-virtio-mmio,romfile=/p/share/qemu/classicvirtio-68k.rom',
                 'nubus-qfb,width=800,height=600,depth=8', 'virtio-tablet-device',
                 'virtio-9p-device,fsdev=share0,mount_tag=RetroBoot Share', 'scsi-hd,scsi-id=0,drive=hd0',
                 'scsi-cd,scsi-id=3,drive=cd0', '-nic user,model=dp83932', '-audiodev none,id=snd0',
                 '-display retroboot,socket=/r/display.sock', '-action reboot=shutdown', '-rtc base=localtime',
                 '-serial file:/r/serial.log']
        positions = [line.index(o) for o in order]
        self.assertEqual(positions, sorted(positions))

    def test_decl_rom_lengths_parser(self):
        import verify_package_m68k as v
        rom = bytearray(64)
        rom[0:4] = bytes([128, 0, 0, 8])           # directory: sResource 128 -> list at 8
        rom[4:8] = bytes([0xff, 0, 0, 0])
        rom[8:12] = bytes([11, 0, 0, 16])          # minorLength -> 24
        rom[12:16] = bytes([13, 0, 0, 16])         # majorLength -> 28
        rom[16:20] = bytes([0xff, 0, 0, 0])
        rom[24:28] = struct.pack('>I', 0xff0000)
        rom[28:32] = struct.pack('>I', 0x1ff0000)
        rom[44:48] = struct.pack('>i', -44)[:1] + (-44 & 0xffffff).to_bytes(3, 'big')   # fhDirOffset at size-20
        self.assertEqual(v.decl_rom_lengths(bytes(rom)), {11: 0xff0000, 13: 0x1ff0000})

    def test_wav_and_dhcp_parsers(self):
        import rbd_quadra_lab as lab
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / 'a.wav'
            hdr = b'RIFF' + bytes(4) + b'WAVEfmt ' + struct.pack('<IHHIIHH', 16, 1, 2, 4000, 16000, 4, 16) + b'data' + bytes(4)
            loud = struct.pack('<2h', 12000, -12000) * 1000
            wav.write_bytes(hdr + bytes(4000 * 4) + loud + bytes(4000 * 4))
            peaks = lab.wav_profile(wav, 0.25)['peaks']
            self.assertEqual(peaks[:4], [0, 0, 0, 0])
            self.assertTrue(any(p >= 46 for p in peaks))
            pcap = Path(tmp) / 'n.pcap'
            bootp = bytearray(300)
            bootp[0] = 2
            bootp[16:20] = bytes([10, 0, 2, 15])
            bootp[236:240] = b'\x63\x82\x53\x63'
            bootp[240:244] = bytes([53, 1, 5, 255])
            udp = struct.pack('>HHHH', 67, 68, 8 + len(bootp), 0) + bytes(bootp)
            ip = bytes([0x45, 0, 0, 0, 0, 0, 0, 0, 64, 17]) + bytes(10) + udp
            frame = bytes(12) + b'\x08\x00' + ip
            pcap.write_bytes(bytes(24) + struct.pack('<IIII', 1, 0, len(frame), len(frame)) + frame)
            self.assertEqual([(m['type'], m['yiaddr']) for m in lab.pcap_dhcp(pcap)], [('ACK', '10.0.2.15')])

    def test_menubar_detector(self):
        import rbd_quadra_lab as lab
        w, h = 64, 30
        rows = []
        for y in range(h):
            for x in range(w):
                if y in (19, 20):
                    rows.append(b'\0\0\0\0')
                elif y < 19:
                    rows.append(b'\0\0\0\0' if (y == 8 and x % 9 == 0) else b'\xff\xff\xff\0')
                else:
                    rows.append(b'\x80\x80\x80\0')
        self.assertTrue(lab.menubar(w, h, w * 4, b''.join(rows)))
        dither = b''.join(b'\0\0\0\0' if (x + y) % 2 else b'\xff\xff\xff\0' for y in range(h) for x in range(w))
        self.assertFalse(lab.menubar(w, h, w * 4, dither))


if __name__ == '__main__':
    unittest.main()
