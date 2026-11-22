"""Gemini PDA image configuration (image/gemini/): static checks, no build."""
import configparser
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

IMAGE = pathlib.Path(__file__).resolve().parents[1]
GEMINI = IMAGE / 'gemini'
ROOTFS = GEMINI / 'rootfs'


def packages(name, board=GEMINI):
    return {l.strip() for l in (board / 'config' / name).read_text().splitlines() if l.strip() and not l.startswith('#')}


def conf(key):
    return [l.split('=', 1)[1] for l in (GEMINI / 'config' / 'gemini.conf').read_text().splitlines() if l.startswith(key + '=')]


class PackagesTest(unittest.TestCase):
    def test_no_boot_pieces(self):
        common = packages('packages.common')
        for p in common | packages('packages.debug') | packages('packages.product'):
            self.assertFalse(re.match(r'(linux-image|raspi-|grub|initramfs-tools|efibootmgr|.*microcode|sddm|plasma|calamares|labwc)', p), p)

    def test_rpi_userspace_kept(self):
        # Everything the Pi installs except its boot pieces and its Wi-Fi/Bluetooth firmware.
        pi_only = {'linux-image-rpi-v8', 'raspi-firmware', 'firmware-brcm80211', 'bluez-firmware', 'initramfs-tools',
                   'firmware-mediatek', 'firmware-realtek', 'firmware-misc-nonfree'}
        self.assertEqual(packages('packages.common', IMAGE / 'rpi') - pi_only - packages('packages.common'), set())

    def test_device_tools(self):
        common = packages('packages.common')
        for p in ('e2fsprogs', 'network-manager', 'wpasupplicant', 'iw', 'alsa-utils', 'gpiod', 'busybox',
                  'systemd-zram-generator', 'bluez'):
            self.assertIn(p, common)
        self.assertIn('openssh-server', packages('packages.debug'))
        self.assertNotIn('openssh-server', common | packages('packages.product'))


class UpstreamPinsTest(unittest.TestCase):
    def test_pinned_commits_and_hashes(self):
        self.assertEqual(conf('gemini_nixos_commit'), ['6a172f513ba8c4f7d00e4be6a954858e238fa12c'])
        self.assertEqual(conf('gemini_debian_commit'), ['355bc5a8e9179af67914b0b883229ff3b33429d3'])
        files = conf('upstream_file')
        self.assertGreaterEqual(len(files), 5)
        for f in files:
            dest, path, sha = f.split('|')
            self.assertTrue(dest.startswith('/usr/lib/'), dest)
            self.assertRegex(sha, r'^[0-9a-f]{64}$')
            self.assertNotIn('WIFI_factory', path)    # never another unit's Wi-Fi record
        dests = {f.split('|')[0] for f in files}
        for fw in ('WMT_SOC.cfg', 'ROMv3_patch_1_0_hdr.bin', 'ROMv3_patch_1_1_hdr.bin', 'WIFI_RAM_CODE_6797'):
            self.assertIn(f'/usr/lib/firmware/{fw}', dests)

    def test_nothing_upstream_in_git(self):
        # No unlicensed upstream file is vendored: only RetroBoot's own sources live here.
        names = {p.name for p in GEMINI.rglob('*') if p.is_file()}
        for n in ('battery-guard.sh', 'WIFI_RAM_CODE_6797', 'WMT_SOC.cfg', 'gemini-wifi-nvram', 'wifi-internal',
                  'gemini-audio', 'WIFI_factory.bin'):
            self.assertNotIn(n, names)


class RootfsTest(unittest.TestCase):
    def unit(self, name):
        c = configparser.RawConfigParser(strict=False)
        c.optionxform = str
        c.read(ROOTFS / 'usr/lib/systemd/system' / name)
        return c

    def test_units_point_at_shipped_scripts(self):
        for u in (ROOTFS / 'usr/lib/systemd/system').glob('retroboot-gemini-*'):
            for line in u.read_text().splitlines():
                m = re.match(r'Exec\w*=-?(/usr/lib/retroboot/gemini/[\w-]+)', line)
                if m:
                    self.assertTrue((ROOTFS / m.group(1).lstrip('/')).is_file(), m.group(1))

    def test_boot_ok_comes_after_multi_user(self):
        c = self.unit('retroboot-gemini-boot-ok.service')
        self.assertEqual(c.get('Unit', 'DefaultDependencies'), 'no')     # else ordered before its own target
        self.assertIn('multi-user.target', c.get('Unit', 'After'))
        self.assertIn('retroboot-hostd.service', c.get('Unit', 'After'))

    def test_wifi_is_off_the_boot_path(self):
        # Nothing waits for the ~15 s Wi-Fi bring-up: no Before= on NetworkManager, hostd or bluetooth.
        for name in ('retroboot-gemini-wifi.service', 'retroboot-gemini-bt.service'):
            c = self.unit(name)
            self.assertFalse(c.has_option('Unit', 'Before'), name)
        self.assertIn('drm', (ROOTFS / 'etc/modules-load.d/retroboot-gemini.conf').read_text().split())

    def test_wdt_unit_never_enabled(self):
        self.assertFalse(self.unit('retroboot-gemini-wdt-reboot.service').has_section('Install'))

    def test_usb0_profile(self):
        text = (ROOTFS / 'usr/lib/NetworkManager/system-connections/retroboot-usb0.nmconnection').read_text()
        self.assertIn('address1=@USB0_ADDRESS@', text)
        self.assertIn('never-default=true', text)
        self.assertEqual(conf('usb0_address'), ['10.15.19.82/24'])

    def test_data_layout_condition_reset(self):
        text = (ROOTFS / 'etc/systemd/system/retroboot-data-layout.service.d/50-gemini.conf').read_text()
        self.assertIn('ConditionPathIsMountPoint=\n', text)

    def test_scripts_parse(self):
        for p in sorted((ROOTFS / 'usr/lib/retroboot/gemini').iterdir()) + [GEMINI / 'installer/gemini-install.sh.in']:
            if not p.is_file():   # e.g. a local __pycache__
                continue
            first = p.read_text().splitlines()[0]
            if 'python' in first:   # rb-gemini-typec
                compile(p.read_text(), str(p), 'exec')
                continue
            shell = 'bash' if 'bash' in first else 'sh'
            subprocess.run([shell, '-n', str(p)], check=True)
        for p in sorted((GEMINI / 'scripts').glob('*.sh')) + sorted((GEMINI / 'lab').glob('*.sh')) + [GEMINI / 'build.sh']:
            subprocess.run(['bash', '-n', str(p)], check=True)


class PartLibTest(unittest.TestCase):
    """gemini-part.sh against a fake /sys/class/block (no root needed)."""

    def test_para_write_and_clear_on_a_file(self):
        lib = ROOTFS / 'usr/lib/retroboot/gemini/gemini-part.sh'
        with tempfile.TemporaryDirectory() as d:
            para = pathlib.Path(d) / 'para'
            para.write_bytes(b'\xff' * 4096)
            sh = f'. {lib}; rbg_para_write {para} boot-recovery && rbg_para_read {para}'
            out = subprocess.run(['sh', '-c', sh], capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(out.stdout.strip(), 'boot-recovery')
            data = para.read_bytes()
            self.assertEqual(data[:32], b'boot-recovery' + b'\0' * 19)
            self.assertEqual(data[32:], b'\xff' * (4096 - 32))   # nothing beyond the 32 bytes
            out = subprocess.run(['sh', '-c', f'. {lib}; rbg_para_clear {para}'], capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(para.read_bytes()[:32], b'\0' * 32)


class InstallerTemplateTest(unittest.TestCase):
    def test_placeholders_and_safety(self):
        text = (GEMINI / 'installer/gemini-install.sh.in').read_text()
        self.assertIn('@GEMINI_PART_LIB@', text)
        for k in ('VERSION', 'BUILD_ID', 'ROOTFS', 'BOOTIMG', 'BOOTIMG_SHA256', 'COUNTER_FILE', 'PART_BOOT', 'PART_PARA',
                  'PART_LINUX'):
            self.assertIn(f'@{k}@', text)
        # it never names a partition by number, nor any partition other than boot/para/linux
        self.assertNotRegex(text, r'mmcblk\dp\d')
        for name in ('nvram', 'nvdata', 'recovery'):
            self.assertNotRegex(text, rf'rbg_part "?{name}')
        self.assertNotIn('local ', text)   # POSIX sh (dash on Gemian 9)

    def test_dispatch(self):
        text = (IMAGE / 'build.sh').read_text()
        self.assertLess(text.index('exec "$here/gemini/build.sh"'), text.index('require_root'))


@unittest.skipUnless(shutil.which('dash') or pathlib.Path('/bin/sh').exists(), 'no POSIX shell')
class BootOkTest(unittest.TestCase):
    def test_ssh_listen_detection(self):
        script = (ROOTFS / 'usr/lib/retroboot/gemini/rb-gemini-boot-ok').read_text()
        fn = script[script.index('ssh_listening()'):].split('\n}\n', 1)[0] + '\n}\n'
        with tempfile.TemporaryDirectory() as d:
            tcp = pathlib.Path(d) / 'tcp'
            tcp.write_text('  sl  local_address rem_address   st\n   0: 00000000:0016 00000000:0000 0A x\n')
            sh = fn.replace('/proc/net/tcp /proc/net/tcp6', str(tcp)) + 'ssh_listening'
            self.assertEqual(subprocess.run(['sh', '-c', sh]).returncode, 0)
            tcp.write_text('  sl  local_address rem_address   st\n   0: 00000000:0016 0100007F:1234 01 x\n')
            self.assertNotEqual(subprocess.run(['sh', '-c', sh]).returncode, 0)


if __name__ == '__main__':
    unittest.main()
