"""Raspberry Pi image configuration (image/rpi/): static checks, no build."""
import json
import pathlib
import re
import subprocess
import unittest

IMAGE = pathlib.Path(__file__).resolve().parents[1]
RPI = IMAGE / 'rpi'


def packages(name):
    return {l.strip() for l in (RPI / 'config' / name).read_text().splitlines() if l.strip() and not l.startswith('#')}


class CmdlineTest(unittest.TestCase):
    def setUp(self):
        self.cmd = (RPI / 'boot' / 'cmdline.txt.in').read_text().split()

    def test_single_line_serial_console_only(self):
        self.assertEqual(len((RPI / 'boot' / 'cmdline.txt.in').read_text().strip().splitlines()), 1)
        consoles = [a for a in self.cmd if a.startswith('console=')]
        # serial0 first (the PL011 driver binds its one console to the first ttyAMA entry, so
        # a ttyAMA10 listed first leaves a PL011 serial0 without a console); the Pi 5's debug
        # UART (ttyAMA10) second, as a fallback. No VT console anywhere.
        self.assertEqual(consoles, ['console=serial0,115200', 'console=ttyAMA10,115200', 'console=tty0'])
        # HyperPixel disables the serial UART; /dev/console still has to exist.
        # The virtual console has no physical framebuffer and no login prompt.
        for word in ('fbcon=map:9', 'drm_kms_helper.fbdev_emulation=0', 'quiet', 'loglevel=0'):
            self.assertIn(word, self.cmd)

    def test_root_by_partuuid_and_quiet(self):
        self.assertIn('root=PARTUUID=@ROOT_PARTUUID@', self.cmd)
        for arg in ('quiet', 'loglevel=0', 'rootwait', 'fsck.repair=yes', 'logo.nologo', 'fbcon=map:9',
                    'vt.global_cursor_default=0', 'systemd.show_status=false', 'sysctl.kernel.sysrq=0'):
            self.assertIn(arg, self.cmd)

    def test_memory_cgroup_enabled(self):
        # The Pi DTBs' /chosen/bootargs (placed before cmdline.txt by the firmware) carry
        # cgroup_disable=memory; webproxy's MemoryMax= needs the controller.
        self.assertIn('cgroup_enable=memory', self.cmd)
        self.assertNotIn('cgroup_disable=memory', self.cmd)

    def test_quiet_arguments_match_x86(self):
        x86 = (IMAGE / 'config' / 'cmdline').read_text().split()
        # x86 serial, firmware framebuffers, and the AMD GPU's display options (no amdgpu on a Pi)
        skip = {'console=ttyS0,115200', 'video=efifb:off', 'video=vesafb:off', 'amdgpu.dcdebugmask=0x410'}
        self.assertEqual([a for a in x86 if a not in skip and a not in self.cmd], [])


class ConfigTxtTest(unittest.TestCase):
    def sections(self):
        out, cur = {}, 'all'
        for line in (RPI / 'boot' / 'config.txt').read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            m = re.fullmatch(r'\[(\w+)\]', line)
            if m:
                cur = m.group(1)
                continue
            out.setdefault(cur, []).append(line)
        return out

    def test_kernel8_on_both_boards(self):
        s = self.sections()
        self.assertIn('kernel=kernel8.img', s['all'])
        self.assertIn('initramfs initramfs8 followkernel', s['all'])
        self.assertIn('arm_64bit=1', s['all'])
        self.assertNotIn('pi5', s)   # nothing switches the Pi 5 to kernel_2712.img

    def test_zero_firmware_memory_budget(self):
        self.assertIn('gpu_mem=16', self.sections()['pi02'])
        self.assertIn('dtparam=cma-64', self.sections()['pi02'])

    def test_audio_param_reaches_the_base_device_tree(self):
        # A dtparam= after a dtoverlay= sets that overlay's parameter (vc4-kms-v3d has its own
        # "audio"); only the base DT's audio=on adds snd_bcm2835.enable_headphones=1 (Pi 4 jack).
        a = self.sections()['all']
        self.assertLess(a.index('dtparam=audio=on'), a.index('dtoverlay=vc4-kms-v3d'))
        self.assertFalse([l for l in a[:a.index('dtoverlay=vc4-kms-v3d')] if l.startswith('dtoverlay=')])

    def test_silent_firmware_and_kms(self):
        a = self.sections()['all']
        for k in ('disable_splash=1', 'dtoverlay=vc4-kms-v3d', 'camera_auto_detect=0', 'include retroboot/display.txt',
                  'dtparam=audio=on', 'disable_overscan=1'):
            self.assertIn(k, a)


class InitramfsConfTest(unittest.TestCase):
    def test_fstype_is_explicit(self):
        conf = (RPI / 'rootfs/etc/initramfs-tools/conf.d/retroboot.conf').read_text().splitlines()
        self.assertIn('FSTYPE=ext4', conf)
        self.assertIn('MODULES=list', conf)


class PackagesTest(unittest.TestCase):
    def test_no_x86_packages(self):
        common = packages('packages.common') | packages('packages.debug') | packages('packages.product')
        for p in ('linux-image-amd64', 'intel-microcode', 'amd64-microcode', 'grub-efi-amd64-bin', 'grub-pc-bin',
                  'efibootmgr', 'firmware-intel-graphics', 'firmware-amd-graphics', 'firmware-sof-signed',
                  'firmware-iwlwifi', 'qemu-guest-agent'):
            self.assertNotIn(p, common)

    def test_shared_runtime_kept(self):
        x86 = packages('../../config/packages.common')
        rpi = packages('packages.common')
        x86_only = {'linux-image-amd64', 'firmware-intel-graphics', 'firmware-iwlwifi', 'firmware-sof-signed',
                    'intel-microcode', 'firmware-amd-graphics', 'amd64-microcode', 'firmware-atheros',
                    'firmware-cirrus', 'grub-efi-amd64-bin', 'grub-pc-bin', 'efibootmgr'}
        self.assertEqual(sorted(x86 - x86_only - rpi), [])

    def test_raspberry_pi_archive_is_pinned_to_boot_pieces(self):
        pref = (RPI / 'config' / 'apt-preferences').read_text()
        stanzas = [dict(l.split(': ', 1) for l in s.strip().splitlines() if not l.startswith('#'))
                   for s in pref.split('\n\n') if s.strip()]
        default = [s for s in stanzas if s.get('Package') == '*']
        self.assertEqual(default[0]['Pin-Priority'], '-1')
        allowed = [s for s in stanzas if s.get('Package') != '*']
        self.assertEqual(len(allowed), 1)
        names = allowed[0]['Package'].split()
        self.assertIn('linux-image-rpi-v8', names)
        self.assertIn('raspi-firmware', names)
        for p in ('raspberrypi-sys-mods', 'bluez', 'initramfs-tools', 'raspi-config'):
            self.assertNotIn(p, names)
        for p in packages('packages.common'):
            if p in names:
                continue
            self.assertFalse(p.startswith('raspi') or p.startswith('rpi-'), p)

    def test_key_fingerprint_pinned(self):
        conf = (RPI / 'config' / 'rpi.conf').read_text()
        self.assertIn('rpi_archive_key_fpr=CF8A1AF502A2AA2D763BAE7E82B129927FA3303E', conf)
        self.assertTrue((RPI / 'apt' / 'raspberrypi-archive-keyring.asc').read_text().startswith(
            '-----BEGIN PGP PUBLIC KEY BLOCK-----'))


class DispatchTest(unittest.TestCase):
    def test_build_sh_dispatches_rpi_before_anything_else(self):
        text = (IMAGE / 'build.sh').read_text()
        self.assertLess(text.index('exec "$here/rpi/build.sh"'), text.index('require_root'))

    def test_scripts_parse(self):
        for p in sorted((RPI / 'scripts').glob('*.sh')) + sorted((RPI / 'lab').glob('*.sh')) + [RPI / 'build.sh']:
            subprocess.run(['bash', '-n', str(p)], check=True)


if __name__ == '__main__':
    unittest.main()


class BootDisplayRpiTest(unittest.TestCase):
    """rb-bootdisplay-rpi keeps the startup arguments in cmdline.txt."""

    def setUp(self):
        import importlib.machinery
        import importlib.util
        import tempfile
        path = RPI / 'rootfs/usr/lib/retroboot/image/rb-bootdisplay-rpi'
        loader = importlib.machinery.SourceFileLoader('rb_bootdisplay_rpi', str(path))
        spec = importlib.util.spec_from_loader('rb_bootdisplay_rpi', loader)
        self.mod = importlib.util.module_from_spec(spec)
        loader.exec_module(self.mod)
        self.bd = self.mod.load_bootdisplay(str(IMAGE / 'rootfs/usr/lib/retroboot/image/rb-bootdisplay'))
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        base = (RPI / 'boot' / 'cmdline.txt.in').read_text().strip().replace('@ROOT_PARTUUID@', '1234-03')
        (self.tmp / 'cmdline.txt').write_text(base + '\n')
        self.base = base

    def run_main(self, settings=None, timing=None):
        import contextlib
        import io
        args = ['--esp', str(self.tmp), '--display', str(self.tmp / 'none.json'),
                '--graphics', str(self.tmp / 'none.json')]
        if settings is not None:
            (self.tmp / 'settings.json').write_text(json.dumps({'applied': settings}))
        args += ['--settings', str(self.tmp / 'settings.json')]
        if timing is not None:
            (self.tmp / 'timing.json').write_text(json.dumps({'cold_ms': timing}))
            args += ['--timing', str(self.tmp / 'timing.json')]
        else:
            args += ['--timing', 'none']
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.mod.main(args, self.bd), 0)
        return json.loads(out.getvalue())

    def test_defaults_leave_the_built_cmdline_alone(self):
        self.assertFalse(self.run_main()['changed'])
        self.assertEqual((self.tmp / 'cmdline.txt').read_text(), self.base + '\n')

    def test_settings_reach_the_cmdline_and_root_stays(self):
        r = self.run_main({'startup.progress_indicator': False, 'display.pixel_scale': '2'}, timing=41234)
        self.assertTrue(r['changed'])
        text = (self.tmp / 'cmdline.txt').read_text()
        self.assertEqual(len(text.splitlines()), 1)
        words = text.split()
        self.assertIn('root=PARTUUID=1234-03', words)
        self.assertEqual(words[:3], ['console=serial0,115200', 'console=ttyAMA10,115200', 'console=tty0'])
        for w in ('retroboot.progress=off', 'retroboot.pixel_scale=2', 'retroboot.startup_ms=41250', 'retroboot.slot=a',
                  'cgroup_enable=memory'):
            self.assertIn(w, words)
        # every word that isn't a startup argument stays, in order
        keep = [w for w in self.base.split() if not w.startswith(tuple(k + '=' for k in self.mod.KEYS))]
        self.assertEqual([w for w in words if not w.startswith(tuple(k + '=' for k in self.mod.KEYS))], keep)
        self.assertEqual(sum(w.startswith('retroboot.progress=') for w in words), 1)
        # small change in the learned time: hysteresis keeps the file
        self.assertFalse(self.run_main({'startup.progress_indicator': False, 'display.pixel_scale': '2'},
                                       timing=42000)['changed'])

    def test_missing_boot_partition_never_fails(self):
        (self.tmp / 'cmdline.txt').unlink()
        self.assertFalse(self.run_main()['changed'])
