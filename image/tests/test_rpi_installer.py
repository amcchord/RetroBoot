"""The Raspberry Pi installer layouts (ARCHITECTURE 8.2): install-to-disk --board rpi and
list-disks' free space after the installer (image/rootfs/usr/lib/retroboot/installer/
rpi_layout.py), and rb-bootdisplay-rpi following config.txt's os_prefix."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import struct
import subprocess
import sys
import tempfile
import unittest

IMAGE = pathlib.Path(__file__).resolve().parents[1]
INSTALLER = IMAGE / 'rootfs/usr/lib/retroboot/installer'
RPI = IMAGE / 'rpi'
MiB, GiB = 1 << 20, 1 << 30


def load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


sys.path.insert(0, str(INSTALLER))
rl = load(INSTALLER / 'rpi_layout.py', 'rpi_layout_test')


def part(name, number, start_mib, size_mib, fstype=None, label=None):
    return {'name': name, 'number': number, 'start': start_mib * MiB, 'size': size_mib * MiB,
            'fstype': fstype, 'label': label}


# The universal image as the contract describes it: FAT RBINSTEFI first, then the payload.
CARD = [part('mmcblk0p1', 1, 1, 512, 'vfat', 'RBINSTEFI'), part('mmcblk0p2', 2, 513, 3500, 'iso9660', 'RETROBOOT')]


class FreeSpacePlanTest(unittest.TestCase):
    def test_fresh_mbr_card(self):
        p = rl.plan_free_space('dos', 32 * GiB, CARD, 'mmcblk0p2')
        self.assertTrue(p['eligible'], p)
        self.assertEqual(p['numbers'], [3, 4])
        self.assertEqual(p['start'], 4016 * MiB)            # 4013 MiB rounded up to 4 MiB
        self.assertEqual(p['end'], 32 * GiB)
        self.assertEqual(p['boot_partition'], 'mmcblk0p1')
        self.assertIsNone(p['existing'])
        root, data = rl.free_space_layout(p)
        self.assertEqual(root, (3, 4016 * MiB, 4016 * MiB + 3 * GiB, 'ext4', 'RBROOT_A'))
        self.assertEqual(data, (4, 4016 * MiB + 3 * GiB, 32 * GiB, 'xfs', 'RBDATA'))

    def test_gpt_keeps_the_backup_header(self):
        p = rl.plan_free_space('gpt', 32 * GiB, CARD, 'mmcblk0p2')
        self.assertTrue(p['eligible'])
        self.assertEqual(p['numbers'], [3, 4])
        self.assertEqual(p['end'], 32 * GiB - 34 * 512)

    def test_previous_installation_is_replaced(self):
        old = CARD + [part('mmcblk0p3', 3, 4016, 3072, 'ext4', 'RBROOT_A'),
                      part('mmcblk0p4', 4, 7088, 20000, 'xfs', 'RBDATA')]
        p = rl.plan_free_space('dos', 32 * GiB, old, 'mmcblk0p2')
        self.assertTrue(p['eligible'], p)
        self.assertEqual((p['existing'], p['remove'], p['numbers']), ('RetroBoot', [3, 4], [3, 4]))
        self.assertEqual(p['start'], 4016 * MiB)
        # an interrupted installation left an empty partition
        p = rl.plan_free_space('dos', 32 * GiB, CARD + [part('mmcblk0p3', 3, 4016, 3072)], 'mmcblk0p2')
        self.assertEqual((p['eligible'], p['existing'], p['remove']), (True, 'empty', [3]))

    def test_refusals(self):
        user = CARD + [part('mmcblk0p3', 3, 4016, 3072, 'ext4', 'photos')]
        cases = [
            (rl.plan_free_space('dos', 32 * GiB, user, 'mmcblk0p2'), 'other-partitions'),
            (rl.plan_free_space('dos', 32 * GiB, CARD, 'mmcblk0'), 'medium-not-partition'),       # ISO on the whole disk
            (rl.plan_free_space('dos', 32 * GiB, [dict(CARD[0], label='BOOT'), CARD[1]], 'mmcblk0p2'),
             'no-installer-boot'),
            (rl.plan_free_space('dos', 6 * GiB, CARD, 'mmcblk0p2'), 'too-small'),
            (rl.plan_free_space('dos', 32 * GiB, CARD + [part('mmcblk0p3', 3, 3600, 10), part('mmcblk0p5', 5, 3620, 10)],
                                'mmcblk0p3'), 'logical-partitions'),
            (rl.plan_free_space('dos', 32 * GiB, CARD + [part('mmcblk0p3', 3, 4100, 100, 'vfat', 'X')], 'mmcblk0p3'),
             'table-full'),
            (rl.plan_free_space('atari', 32 * GiB, CARD, 'mmcblk0p2'), 'unknown-table'),
        ]
        for plan, reason in cases:
            with self.subTest(reason=reason):
                self.assertFalse(plan['eligible'])
                self.assertEqual(plan['reason'], reason)
                self.assertIn(reason, rl.REASONS)

    def test_installer_partitions_never_in_the_free_region(self):
        for size in (8, 16, 32, 64, 128, 256, 512):
            p = rl.plan_free_space('dos', size * GiB, CARD, 'mmcblk0p2')
            if not p['eligible']:
                continue
            self.assertGreaterEqual(p['start'], max(x['start'] + x['size'] for x in CARD))
            self.assertEqual(p['start'] % (4 * MiB), 0)
            self.assertLessEqual(p['end'], size * GiB)


class MbrTest(unittest.TestCase):
    def blank(self, disk_id=0x1234abcd):
        s = bytearray(512)
        struct.pack_into('<I', s, 440, disk_id)
        s[510:512] = b'\x55\xaa'
        s = rl.mbr_set(bytes(s), 1, 0x0c, 2048, 1048576)
        return rl.mbr_set(s, 2, 0x17, 1050624, 7168000)

    def test_add_two_partitions_keeps_the_others(self):
        s0 = self.blank()
        s1 = rl.mbr_set(s0, 3, 0x83, 8224768, 6291456)
        s1 = rl.mbr_set(s1, 4, 0x83, 14516224, 52592640)
        self.assertEqual(s1[:446], s0[:446])            # boot code and disk signature untouched
        self.assertEqual(s1[446:478], s0[446:478])      # entries 1 and 2 untouched
        info = rl.mbr_parse(s1)
        self.assertEqual(info['disk_id'], 0x1234abcd)
        self.assertEqual(info['entries'][3], (0, 0x83, 8224768, 6291456))
        self.assertEqual(info['entries'][4], (0, 0x83, 14516224, 52592640))
        self.assertEqual(rl.mbr_partuuid(info['disk_id'], 3), '1234abcd-03')
        cleared = rl.mbr_set(s1, 4, 0, 0, 0)
        self.assertNotIn(4, rl.mbr_parse(cleared)['entries'])

    def test_sfdisk_reads_what_we_write(self):
        sfdisk = subprocess.run(['sh', '-c', 'command -v sfdisk'], capture_output=True, text=True).stdout.strip()
        if not sfdisk:
            self.skipTest('no sfdisk')
        with tempfile.NamedTemporaryFile() as f:
            f.truncate(40 * GiB)
            s = rl.mbr_set(self.blank(), 3, 0x83, 8224768, 6291456)
            s = rl.mbr_set(s, 4, 0x83, 14516224, 40 * GiB // 512 - 14516224)
            f.write(s)
            f.flush()
            out = json.loads(subprocess.run([sfdisk, '--json', f.name], capture_output=True, text=True,
                                            check=True).stdout)['partitiontable']
        self.assertEqual(out['label'], 'dos')
        self.assertEqual(out['id'], '0x1234abcd')
        self.assertEqual([(p['start'], p['size'], p['type']) for p in out['partitions']],
                         [(2048, 1048576, 'c'), (1050624, 7168000, '17'), (8224768, 6291456, '83'),
                          (14516224, 40 * GiB // 512 - 14516224, '83')])

    def test_refuses_non_mbr_and_huge(self):
        with self.assertRaises(rl.LayoutError):
            rl.mbr_parse(bytes(512))
        with self.assertRaises(rl.LayoutError):
            rl.mbr_set(self.blank(), 3, 0x83, 1 << 32, 10)
        with self.assertRaises(rl.LayoutError):
            rl.mbr_set(self.blank(), 5, 0x83, 1, 10)


class BootFilesTest(unittest.TestCase):
    def test_installed_config(self):
        base = (RPI / 'boot/config.txt').read_text()
        cfg = rl.installed_config(base)
        self.assertTrue(rl.is_installed_config(cfg))
        self.assertFalse(rl.is_installed_config(base))
        lines = cfg.splitlines()
        self.assertEqual(lines.count('os_prefix=retroboot/os/'), 1)
        # right after the first [all], before kernel= and the initramfs line
        i = lines.index('os_prefix=retroboot/os/')
        self.assertEqual(lines[i - 1], '[all]')
        self.assertLess(i, lines.index('kernel=kernel8.img'))
        self.assertLess(i, lines.index('initramfs initramfs8 followkernel'))
        # every setting of the image's own config.txt is kept, in order
        self.assertEqual([x for x in lines if x and not x.startswith('#') and x != 'os_prefix=retroboot/os/'],
                         [x for x in base.splitlines() if x and not x.startswith('#')])
        self.assertEqual(rl.installed_config(cfg.replace(rl.CONFIG_MARK, '#')).count('os_prefix='), 1)

    def test_cmdline(self):
        tpl = (RPI / 'boot/cmdline.txt.in').read_text()
        line = rl.cmdline_for(tpl, '1234abcd-03')
        self.assertTrue(line.endswith('\n') and line.count('\n') == 1)
        self.assertIn('root=PARTUUID=1234abcd-03', line.split())
        self.assertNotIn('boot=retroboot-live', line)
        self.assertEqual(rl.slot_cmdline(line).split()[:2], ['console=serial0,115200', 'console=ttyAMA10,115200'])
        self.assertFalse(any(w.startswith(('root=', 'retroboot.slot=')) for w in rl.slot_cmdline(line).split()))
        with self.assertRaises(rl.LayoutError):
            rl.cmdline_for('root=/dev/sda1', 'x')

    def test_fstab_by_partuuid(self):
        fstab = ('# RetroBoot\n'
                 'PARTLABEL=RBROOT_A  /                   ext4  rw,noatime,errors=remount-ro  0 1\n'
                 'PARTLABEL=RBDATA    /var/lib/retroboot  xfs   noatime,nofail  0 0\n'
                 'PARTLABEL=RBEFI     /boot/firmware      vfat  noauto,nofail,x-systemd.automount  0 0\n'
                 'tmpfs /tmp tmpfs defaults 0 0\n')
        out = rl.fstab_by_partuuid(fstab, 'aa-03', 'aa-04', 'aa-01')
        lines = out.splitlines()
        self.assertEqual(lines[0], '# RetroBoot')
        self.assertEqual(lines[1].split()[:3], ['PARTUUID=aa-03', '/', 'ext4'])
        self.assertEqual(lines[2].split()[:2], ['PARTUUID=aa-04', '/var/lib/retroboot'])
        self.assertEqual(lines[3].split()[:2], ['PARTUUID=aa-01', '/boot/firmware'])
        self.assertTrue(lines[1].endswith('rw,noatime,errors=remount-ro  0 1'))
        self.assertEqual(lines[4], 'tmpfs /tmp tmpfs defaults 0 0')
        with self.assertRaises(rl.LayoutError):
            rl.fstab_by_partuuid('tmpfs /tmp tmpfs defaults 0 0\n', 'a', 'b', 'c')

    def test_kernel_and_firmware_files(self):
        with tempfile.TemporaryDirectory() as root:
            r = pathlib.Path(root)
            kver = '6.18.50+rpt-rpi-v8'
            (r / 'usr/lib/modules' / kver).mkdir(parents=True)
            (r / 'usr/lib/modules/6.12.111+deb13-arm64').mkdir()       # a lab kernel beside it
            (r / 'boot').mkdir()
            (r / 'boot' / f'vmlinuz-{kver}').write_bytes(b'k' * 100)
            (r / 'boot' / f'initrd.img-{kver}').write_bytes(b'i' * 50)
            dt = r / 'usr/lib' / f'linux-image-{kver}'
            (dt / 'broadcom').mkdir(parents=True)
            (dt / 'overlays').mkdir()
            for n in ('bcm2711-rpi-4-b.dtb', 'bcm2712-rpi-5-b.dtb', 'bcm2837-rpi-3-b.dtb'):
                (dt / 'broadcom' / n).write_bytes(b'd')
            for n in ('vc4-kms-v3d.dtbo', 'overlay_map.dtb', 'README', 'notes.txt'):
                (dt / 'overlays' / n).write_bytes(b'o')
            kf = rl.kernel_files(root)
            self.assertEqual(kf['version'], kver)
            names = [rel for _, rel in rl.os_files(kf)]
            self.assertEqual(names, ['kernel8.img', 'initramfs8', 'bcm2711-rpi-4-b.dtb', 'bcm2712-rpi-5-b.dtb',
                                     'overlays/README', 'overlays/overlay_map.dtb', 'overlays/vc4-kms-v3d.dtbo'])
            self.assertEqual(rl.files_bytes(rl.os_files(kf)), 100 + 50 + 2 + 3)
            with self.assertRaises(rl.LayoutError):
                rl.firmware_files(root)
            fw = r / 'usr/lib/raspi-firmware'
            fw.mkdir(parents=True)
            for n in ('start4.elf', 'fixup4.dat', 'start4x.elf', 'fixup4x.dat', 'start.elf', 'LICENCE.broadcom'):
                (fw / n).write_bytes(b'f')
            self.assertEqual([rel for _, rel in rl.firmware_files(root)],
                             ['fixup4.dat', 'fixup4x.dat', 'start4.elf', 'start4x.elf', 'LICENCE.broadcom'])
            dest = r / 'boot-part'
            rl.copy_files(rl.os_files(kf), str(dest / 'retroboot/os'))
            self.assertEqual((dest / 'retroboot/os/kernel8.img').read_bytes(), b'k' * 100)
            self.assertTrue((dest / 'retroboot/os/overlays/vc4-kms-v3d.dtbo').is_file())
            self.assertFalse(list(dest.rglob('*.new')))
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(rl.LayoutError):
                rl.kernel_files(root)

    def test_sysfs_describe(self):
        with tempfile.TemporaryDirectory() as tmp:
            sysfs = pathlib.Path(tmp)
            disk = sysfs / 'mmcblk0'
            disk.mkdir()
            (disk / 'size').write_text(str(32 * GiB // 512))
            for p in CARD:
                d = disk / p['name']
                d.mkdir()
                (d / 'partition').write_text(str(p['number']))
                (d / 'start').write_text(str(p['start'] // 512))
                (d / 'size').write_text(str(p['size'] // 512))
            kids = [{'name': '/dev/mmcblk0p1', 'kname': '/dev/mmcblk0p1', 'fstype': 'vfat', 'label': 'RBINSTEFI'},
                    {'name': '/dev/mmcblk0p2', 'kname': '/dev/mmcblk0p2', 'fstype': 'iso9660', 'label': 'X'}]
            plan = rl.describe('/dev/mmcblk0', 'dos', '/dev/mmcblk0p2', kids, sysfs=str(sysfs))
            self.assertTrue(plan['eligible'], plan)
            self.assertIsNone(plan['message'])
            plan = rl.describe('/dev/mmcblk0', 'dos', '/dev/mmcblk0', kids, sysfs=str(sysfs))
            self.assertEqual(plan['message'], rl.REASONS['medium-not-partition'])


class InstallToDiskArgsTest(unittest.TestCase):
    def run_itd(self, *args):
        return subprocess.run([sys.executable, '-I', str(INSTALLER / 'install-to-disk'), *args],
                              capture_output=True, text=True)

    def test_free_space_needs_the_pi_board(self):
        r = self.run_itd('--target', '/dev/null', '--mode', 'free-space')
        self.assertEqual(r.returncode, 2)
        self.assertIn('--mode free-space needs --board rpi', r.stderr)

    def test_module_loads_with_isolated_python(self):
        itd = load(INSTALLER / 'install-to-disk', 'install_to_disk_rpi')
        self.assertEqual(itd.RPI_LAYOUT[0], (1, 'RBEFI', '0700', 256))
        self.assertEqual(itd.RPI_LAYOUT[1:], itd.LAYOUT[1:])
        self.assertEqual(itd.rpi_layout.FREE_FIXED_BYTES, rl.FREE_FIXED_BYTES)


class BootDisplayCmdlinePathTest(unittest.TestCase):
    def setUp(self):
        self.mod = load(RPI / 'rootfs/usr/lib/retroboot/image/rb-bootdisplay-rpi', 'rb_bootdisplay_rpi_path')
        self.bd = self.mod.load_bootdisplay(str(IMAGE / 'rootfs/usr/lib/retroboot/image/rb-bootdisplay'))
        self.tmp = pathlib.Path(tempfile.mkdtemp())

    def test_paths(self):
        esp = str(self.tmp)
        self.assertEqual(self.mod.cmdline_path(esp), os.path.join(esp, 'cmdline.txt'))     # no config.txt
        (self.tmp / 'config.txt').write_text((RPI / 'boot/config.txt').read_text())
        self.assertEqual(self.mod.cmdline_path(esp), os.path.join(esp, 'cmdline.txt'))
        (self.tmp / 'config.txt').write_text(rl.installed_config((RPI / 'boot/config.txt').read_text()))
        self.assertEqual(self.mod.cmdline_path(esp), os.path.join(esp, 'retroboot/os/cmdline.txt'))
        (self.tmp / 'config.txt').write_text('[all]\nos_prefix=a/\n[pi5]\nos_prefix=b/\ncmdline=x.txt\n[all]\n')
        self.assertEqual(self.mod.cmdline_path(esp), os.path.join(esp, 'a/cmdline.txt'))
        (self.tmp / 'config.txt').write_text('os_prefix=a/\ncmdline=/top.txt\n')
        self.assertEqual(self.mod.cmdline_path(esp), os.path.join(esp, 'top.txt'))
        (self.tmp / 'config.txt').write_text('os_prefix=../../etc/\n')
        self.assertEqual(self.mod.cmdline_path(esp), os.path.join(esp, 'cmdline.txt'))

    def test_installed_beside_the_installer_edits_only_its_own_cmdline(self):
        base = (RPI / 'boot/cmdline.txt.in').read_text().strip().replace('@ROOT_PARTUUID@', 'aa-03')
        installer = 'console=serial0,115200 boot=retroboot-live rb.live=.retroboot-installer-x quiet\n'
        (self.tmp / 'cmdline.txt').write_text(installer)
        (self.tmp / 'retroboot/os').mkdir(parents=True)
        (self.tmp / 'retroboot/os/cmdline.txt').write_text(base + '\n')
        (self.tmp / 'config.txt').write_text(rl.installed_config((RPI / 'boot/config.txt').read_text()))
        (self.tmp / 'settings.json').write_text(json.dumps({'applied': {'display.pixel_scale': '2'}}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.mod.main(['--esp', str(self.tmp), '--display', str(self.tmp / 'none.json'),
                                            '--graphics', str(self.tmp / 'none.json'), '--timing', 'none',
                                            '--settings', str(self.tmp / 'settings.json')], self.bd), 0)
        self.assertTrue(json.loads(out.getvalue())['changed'])
        self.assertEqual((self.tmp / 'cmdline.txt').read_text(), installer)
        self.assertIn('retroboot.pixel_scale=2', (self.tmp / 'retroboot/os/cmdline.txt').read_text().split())
        # --cmdline names the file directly (install-to-disk)
        f = self.tmp / 'other.txt'
        f.write_text(base + '\n')
        with contextlib.redirect_stdout(io.StringIO()):
            self.mod.main(['--cmdline', str(f), '--display', str(self.tmp / 'none.json'), '--graphics',
                           str(self.tmp / 'none.json'), '--timing', 'none', '--settings',
                           str(self.tmp / 'settings.json')], self.bd)
        self.assertIn('retroboot.pixel_scale=2', f.read_text().split())


if __name__ == '__main__':
    unittest.main()
