"""Universal installer medium (image/scripts/universal.py, make-universal.sh, retroboot-live).

The checker tests assemble a miniature universal image (stub boot files, a small FAT32 and
ISO 9660) and need mkfs.vfat, mtools and xorriso (the builder has them); the rest is pure
Python and runs anywhere.
"""
import gzip
import importlib.util
import os
import pathlib
import re
import shutil
import struct
import subprocess
import tempfile
import unittest

IMAGE = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('universal', IMAGE / 'scripts' / 'universal.py')
universal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(universal)

MARKER = '.retroboot-installer-1.1.0-20261017T000000Z-0123abcd'
CMDLINE_X86 = (IMAGE / 'config' / 'cmdline').read_text().split()


def render_grub(live, medium, startup):
    text = (IMAGE / 'grub' / 'grub-iso.cfg.in').read_text()
    for k, v in (('@CMDLINE@', ' '.join(CMDLINE_X86)), ('@MARKER@', MARKER),
                 ('@STARTUP_INITRD@', ' /live/startup.cpio' if startup else ''), ('@LIVE@', live),
                 ('@MEDIUM_ARGS@', medium), ('@STARTUP_STYLE@', 'classic')):
        text = text.replace(k, v)
    return text


class RpiCmdlineTest(unittest.TestCase):
    def setUp(self):
        self.template = (IMAGE / 'rpi' / 'boot' / 'cmdline.txt.in').read_text()
        self.line = universal.rpi_live_cmdline(self.template, MARKER)
        self.words = self.line.split()

    def test_live_boot_and_consoles(self):
        self.assertEqual(self.words[:2], ['console=serial0,115200', 'console=ttyAMA10,115200'])
        for w in ('boot=retroboot-live', f'rb.live={MARKER}', 'fstab=no', 'retroboot.mode=installer',
                  'retroboot.medium=universal'):
            self.assertIn(w, self.words)
        self.assertEqual(universal.cmdline_problems(self.line, MARKER), [])

    def test_installed_system_words_dropped_quiet_set_kept(self):
        for w in self.words:
            self.assertFalse(w.startswith(('root=', 'rootfstype=', 'retroboot.slot=', 'fsck.repair=')), w)
        self.assertNotIn('rootwait', self.words)
        kept = [w for w in self.template.split() if not universal.PI_LIVE_DROP.match(w)]
        self.assertEqual([w for w in kept if w not in self.words], [])
        for w in ('quiet', 'loglevel=0', 'cgroup_enable=memory', 'logo.nologo', 'fbcon=map:9'):
            self.assertIn(w, self.words)

    def test_problems_found(self):
        bad = self.line.replace('console=serial0,115200 ', '') + ' root=PARTUUID=x'
        problems = universal.cmdline_problems(bad, MARKER)
        self.assertTrue(any('serial0' in p for p in problems))
        self.assertTrue(any('root=' in p for p in problems))
        self.assertTrue(universal.cmdline_problems(self.line + '\nx', MARKER))

    def test_virtual_console_fallback_must_never_reach_a_framebuffer(self):
        self.assertIn('console=tty0', self.words)
        for word in ('fbcon=map:9', 'drm_kms_helper.fbdev_emulation=0'):
            self.assertTrue(universal.cmdline_problems(self.line.replace(word, ''), MARKER))
        self.assertTrue(universal.cmdline_problems(self.line.replace('console=tty0', 'console=tty1'), MARKER))


class GrubTemplateTest(unittest.TestCase):
    def test_iso_rendering_is_unchanged(self):
        # make-iso.sh's substitutions give the 1.1.0 ISO's entries byte for byte
        text = render_grub('/live', '', True)
        self.assertIn(f'\tlinux /live/vmlinuz boot=retroboot-live rb.live={MARKER} fstab=no retroboot.mode=installer '
                      '$rb_cmdline\n\tinitrd /live/microcode.cpio /live/initrd.img /live/startup.cpio\n', text)
        self.assertNotIn('@', text.replace('"#dddddd"', ''))
        self.assertNotIn('universal', text)

    def test_universal_rendering(self):
        for startup in (False, True):
            text = render_grub('/live/x86_64', ' retroboot.medium=universal', startup)
            self.assertEqual(universal.grub_cfg_problems(text, MARKER, startup), [])
            self.assertTrue(universal.grub_cfg_problems(text, MARKER, not startup))
        self.assertTrue(universal.grub_cfg_problems(render_grub('/live', '', False), MARKER, False))

    def test_make_universal_uses_the_iso_template(self):
        src = (IMAGE / 'scripts' / 'make-universal.sh').read_text()
        self.assertIn('grub/grub-iso.cfg.in', src)
        self.assertIn('s|@LIVE@|/live/x86_64|g', src)
        self.assertIn('s|@MEDIUM_ARGS@| retroboot.medium=universal|g', src)
        self.assertIn('s|@LIVE@|/live|g', (IMAGE / 'scripts' / 'make-iso.sh').read_text())


class LiveScriptTest(unittest.TestCase):
    """retroboot-live picks /live/<uname -m>/rootfs.squashfs, else the ISO's /live/rootfs.squashfs."""

    def run_fn(self, medium, arch):
        script = (IMAGE / 'rootfs/etc/initramfs-tools/scripts/retroboot-live').read_text()
        fn = re.search(r'^rb_live_rootfs\(\)\n\{.*?^\}', script, re.S | re.M).group(0)
        with tempfile.TemporaryDirectory() as bindir:
            uname = pathlib.Path(bindir, 'uname')
            uname.write_text(f'#!/bin/sh\necho {arch}\n')
            uname.chmod(0o755)
            env = dict(os.environ, PATH=f'{bindir}:{os.environ["PATH"]}')
            return subprocess.run(['sh', '-c', fn + '\nrb_live_rootfs "$1"', 'sh', medium], env=env,
                                  capture_output=True, text=True, check=True).stdout.strip()

    def test_arch_selection_and_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            for arch in ('x86_64', 'aarch64'):
                pathlib.Path(d, 'live', arch).mkdir(parents=True)
                pathlib.Path(d, 'live', arch, 'rootfs.squashfs').write_bytes(b'hsqs')
            self.assertEqual(self.run_fn(d, 'x86_64'), f'{d}/live/x86_64/rootfs.squashfs')
            self.assertEqual(self.run_fn(d, 'aarch64'), f'{d}/live/aarch64/rootfs.squashfs')
            self.assertEqual(self.run_fn(d, 'riscv64'), '')
            pathlib.Path(d, 'live', 'rootfs.squashfs').write_bytes(b'hsqs')
            self.assertEqual(self.run_fn(d, 'riscv64'), f'{d}/live/rootfs.squashfs')
        with tempfile.TemporaryDirectory() as d:     # the x86 ISO
            pathlib.Path(d, 'live').mkdir()
            pathlib.Path(d, 'live', 'rootfs.squashfs').write_bytes(b'hsqs')
            self.assertEqual(self.run_fn(d, 'x86_64'), f'{d}/live/rootfs.squashfs')

    def test_mount_uses_it(self):
        script = (IMAGE / 'rootfs/etc/initramfs-tools/scripts/retroboot-live').read_text()
        self.assertIn('rootimg=$(rb_live_rootfs "$rb_live_dir/medium")', script)
        self.assertIn('mount -t squashfs -o ro,loop "$rootimg"', script)
        self.assertNotIn('medium/live/rootfs.squashfs"', script)


class MbrTest(unittest.TestCase):
    def test_round_trip_and_chs(self):
        with tempfile.NamedTemporaryFile() as f:
            f.write(b'\xeb\x63\x90' + b'\x11' * 437 + bytes(72))
            f.flush()
            universal.write_mbr(f.name, 0x1234abcd, [(2048, 1048576, 0x0C, True), (1050624, 4096, 0x83, False)])
            f.seek(0)
            s0 = f.read(512)
        self.assertEqual(s0[:3], b'\xeb\x63\x90')
        self.assertEqual(s0[3:440], b'\x11' * 437)          # boot code untouched
        self.assertEqual(s0[510:], b'\x55\xaa')
        self.assertEqual(struct.unpack('<I', s0[440:444])[0], 0x1234abcd)
        p = universal.read_mbr(s0)
        self.assertEqual([(x['boot'], x['type'], x['start'], x['sectors']) for x in p],
                         [(0x80, 0x0C, 2048, 1048576), (0, 0x83, 1050624, 4096), (0, 0, 0, 0), (0, 0, 0, 0)])
        self.assertEqual(universal.chs(2048), bytes((32, 33, 0)))           # as sfdisk writes it
        self.assertEqual(universal.chs(1023 * 16065 + 63 + 2), bytes((1, 0xC0 | 3, 0xFF)))   # cylinder 1023
        self.assertEqual(universal.chs(1024 * 16065), b'\xfe\xff\xff')


class ConfigTxtRulesTest(unittest.TestCase):
    def test_pi_image_config_passes(self):
        self.assertEqual(universal.config_txt_problems((IMAGE / 'rpi' / 'boot' / 'config.txt').read_text()), [])

    def test_audio_after_overlay_fails(self):
        text = (IMAGE / 'rpi' / 'boot' / 'config.txt').read_text()
        text = text.replace('dtparam=audio=on\n', '').replace('dtoverlay=vc4-kms-v3d\n', 'dtoverlay=vc4-kms-v3d\ndtparam=audio=on\n')
        self.assertTrue(any('audio' in p for p in universal.config_txt_problems(text)))


def newc(files):
    out = bytearray()
    for i, (name, data) in enumerate(list(files) + [('TRAILER!!!', b'')]):
        nb = name.encode() + b'\0'
        out += b'070701' + b''.join(b'%08X' % v for v in (i + 1, 0o100644, 0, 0, 1, 0, len(data), 0, 0, 0, 0,
                                                             len(nb), 0)) + nb
        out += bytes(-len(out) % 4)
        out += data
        out += bytes(-len(out) % 4)
    return bytes(out)


@unittest.skipUnless(shutil.which('zstd'), 'needs zstd')
class InitramfsNamesTest(unittest.TestCase):
    def test_layouts(self):
        early = newc([('usr/lib/modules/k/squashfs.ko.xz', b'm')])
        main = subprocess.run(['zstd', '-q', '-c'], input=newc([('scripts/retroboot-live', b'#!/bin/sh\n')]),
                              capture_output=True, check=True).stdout
        box = newc([('retroboot', b''), ('retroboot/installer-startup.rbis', b'RBIS')])
        names = universal.initramfs_names(early + main)
        self.assertEqual(names, ['usr/lib/modules/k/squashfs.ko.xz', 'scripts/retroboot-live'])
        joined = early + main
        joined += bytes(-len(joined) % 4) + box
        self.assertIn('retroboot/installer-startup.rbis', universal.initramfs_names(joined))
        self.assertIn('scripts/retroboot-live', universal.initramfs_names(joined))
        if len(early + main) % 4:   # unaligned: the kernel would not find it
            self.assertNotIn('retroboot/installer-startup.rbis', universal.initramfs_names(early + main + box))


def pe_stub(marker):
    pe = bytearray(0x200)
    pe[0:2] = b'MZ'
    struct.pack_into('<I', pe, 0x3C, 0x40)
    pe[0x40:0x44] = b'PE\0\0'
    struct.pack_into('<H', pe, 0x44, 0x8664)
    struct.pack_into('<H', pe, 0x40 + 24, 0x20B)
    struct.pack_into('<H', pe, 0x40 + 24 + 68, 10)
    return bytes(pe) + f'search --no-floppy --file --set=root /{marker}'.encode()


def pe_stub_ia32(marker):
    pe = bytearray(0x200)
    pe[0:2] = b'MZ'
    struct.pack_into('<I', pe, 0x3C, 0x40)
    pe[0x40:0x44] = b'PE\0\0'
    struct.pack_into('<H', pe, 0x44, 0x014C)
    struct.pack_into('<H', pe, 0x40 + 24, 0x10B)
    struct.pack_into('<H', pe, 0x40 + 24 + 68, 10)
    return bytes(pe) + f'search --no-floppy --file --set=root /{marker}'.encode()


def bzimage_stub(version):
    b = bytearray(0x4000)
    b[0x202:0x206] = b'HdrS'
    struct.pack_into('<H', b, 0x20E, 0x1000)
    v = f'{version} (debian-kernel@lists.debian.org) #1 SMP'.encode()
    b[0x1200:0x1200 + len(v)] = v
    return bytes(b)


def arm64_stub(version):
    b = bytearray(0x1000)
    b[0x38:0x3C] = b'ARMd'
    return gzip.compress(bytes(b) + f'Linux version {version} (gcc) #1 SMP'.encode())


TOOLS = all(shutil.which(t) for t in ('mkfs.vfat', 'mcopy', 'mdel', 'xorriso'))


@unittest.skipUnless(TOOLS, 'needs mkfs.vfat, mtools and xorriso')
class CheckerTest(unittest.TestCase):
    FAT_MIB = 40

    def build(self, d, startup=False):
        d = pathlib.Path(d)
        fat, pay = d / 'fat', d / 'payload'
        for p in universal.PI_FILES:
            (fat / p).parent.mkdir(parents=True, exist_ok=True)
            (fat / p).write_bytes(b'stub ' + p.encode())
        (fat / 'config.txt').write_text((IMAGE / 'rpi' / 'boot' / 'config.txt').read_text())
        (fat / 'cmdline.txt').write_text(universal.rpi_live_cmdline(
            (IMAGE / 'rpi' / 'boot' / 'cmdline.txt.in').read_text(), MARKER))
        (fat / 'kernel8.img').write_bytes(arm64_stub('6.18.50+rpt-rpi-v8'))
        (fat / 'EFI' / 'BOOT').mkdir(parents=True)
        (fat / 'EFI' / 'BOOT' / 'BOOTX64.EFI').write_bytes(pe_stub(MARKER))
        (fat / 'EFI' / 'BOOT' / 'BOOTIA32.EFI').write_bytes(pe_stub_ia32(MARKER))
        (fat / 'retroboot').mkdir()
        (fat / 'retroboot' / '.keep').write_text('')
        (pay / 'live' / 'x86_64').mkdir(parents=True)
        (pay / 'live' / 'aarch64').mkdir(parents=True)
        (pay / MARKER).write_text('')
        (pay / 'live' / 'x86_64' / 'vmlinuz').write_bytes(bzimage_stub('6.12.111+deb13-amd64'))
        for n in ('initrd.img', 'microcode.cpio'):
            (pay / 'live' / 'x86_64' / n).write_bytes(b'x')
        for a in ('x86_64', 'aarch64'):
            (pay / 'live' / a / 'rootfs.squashfs').write_bytes(b'hsqs' + bytes(100))
        if startup:
            (pay / 'live' / 'installer-startup.rbis').write_bytes(b'r')
            (pay / 'live' / 'startup.cpio').write_bytes(b'c')
        (pay / 'boot' / 'grub' / 'fonts').mkdir(parents=True)
        (pay / 'boot' / 'grub' / 'retroboot').mkdir()
        (pay / 'boot' / 'grub' / 'fonts' / 'blank.pf2').write_bytes(b'f')
        (pay / 'boot' / 'grub' / 'retroboot' / 'checker-default.png').write_bytes(b'p')
        (pay / 'boot' / 'grub' / 'grub.cfg').write_text(render_grub('/live/x86_64', ' retroboot.medium=universal', startup))
        fimg, iso, img = d / 'fat.img', d / 'payload.iso', d / 'u.img'
        with open(fimg, 'wb') as f:
            f.truncate(self.FAT_MIB << 20)
        subprocess.run(['mkfs.vfat', '-F', '32', '-s', '1', '-S', '512', '-h', '2048', '-n', 'RBINSTEFI', str(fimg)],
                       check=True, capture_output=True)
        env = dict(os.environ, MTOOLS_SKIP_CHECK='1')
        subprocess.run(['mcopy', '-i', str(fimg), '-s', '-Q', *[str(p) for p in fat.iterdir()], '::/'],
                       check=True, env=env)
        subprocess.run(['xorriso', '-as', 'mkisofs', '-V', 'RETROBOOT', '-iso-level', '3', '-r', '-o', str(iso), str(pay)],
                       check=True, capture_output=True)
        p2 = 2048 + self.FAT_MIB * 2048
        n2 = (iso.stat().st_size + (1 << 20) - 1) // (1 << 20) * 2048
        with open(img, 'wb') as f:
            mbr = bytearray(512)
            mbr[0:3] = b'\xeb\x63\x90'
            struct.pack_into('<Q', mbr, 0x5C, 1)
            f.write(mbr)
            core = bytearray(b'GRUB core'.ljust(3 * 512, b'c'))    # "core.img" in sectors 1-3
            struct.pack_into('<QHH', core, 0x1F4, 2, 2, 0x820)      # its blocklist: sectors 2-3
            f.write(core)
            f.truncate((p2 + n2) * 512)
            f.seek(1 << 20)
            f.write(fimg.read_bytes())
            f.seek(p2 * 512)
            f.write(iso.read_bytes())
        universal.write_mbr(str(img), 0xabcdef01, [(2048, self.FAT_MIB * 2048, 0x0C, True), (p2, n2, 0x83, False)])
        return img

    def check(self, img):
        return universal.check(str(img), min_fat_mib=self.FAT_MIB,
                               rpi_config=str(IMAGE / 'rpi' / 'boot' / 'config.txt'))

    def failed_rules(self, rep):
        return {l.split()[1] for l in rep.lines if l.startswith('FAIL')}

    def test_good_image_passes(self):
        for startup in (False, True):
            with tempfile.TemporaryDirectory() as d:
                rep = self.check(self.build(d, startup))
                self.assertEqual(rep.failed, 0, '\n'.join(rep.lines))
                self.assertEqual(rep.facts['marker'], MARKER)
                self.assertEqual(rep.facts['kernel8'], '6.18.50+rpt-rpi-v8')
                self.assertEqual(rep.facts['vmlinuz'], '6.12.111+deb13-amd64')

    def test_partition_table_rules(self):
        with tempfile.TemporaryDirectory() as d:
            img = self.build(d)
            good = img.read_bytes()[:1024]
            cases = {
                'PI-type': lambda s: s[:446 + 4] + b'\xef' + s[446 + 5:],                       # ESP type
                'MBR-noGPT': lambda s: s[:512] + b'EFI PART' + s[520:],                       # a GPT header
                'UEFI-mbr': lambda s: s[:462 + 8] + struct.pack('<I', 4096) + s[462 + 12:],  # p2 inside p1
                'BIOS-active': lambda s: s[:446] + b'\0' + s[447:],
            }
            for rule, mutate in cases.items():
                with open(img, 'r+b') as f:
                    f.write(mutate(good))
                self.assertIn(rule, self.failed_rules(self.check(img)), rule)
                with open(img, 'r+b') as f:
                    f.write(good)
            self.assertEqual(self.check(img).failed, 0)

    def test_fat_content_rules(self):
        env = dict(os.environ, MTOOLS_SKIP_CHECK='1')
        with tempfile.TemporaryDirectory() as d:
            img = self.build(d)
            part = f'{img}@@1M'
            subprocess.run(['mdel', '-i', part, '::start4.elf'], check=True, env=env)
            self.assertIn('PI-files', self.failed_rules(self.check(img)))
            bad = pathlib.Path(d, 'cmdline.txt')
            bad.write_text(universal.rpi_live_cmdline((IMAGE / 'rpi' / 'boot' / 'cmdline.txt.in').read_text(),
                                                      MARKER + 'x'))
            subprocess.run(['mcopy', '-o', '-i', part, str(bad), '::cmdline.txt'], check=True, env=env)
            self.assertIn('PAY-marker', self.failed_rules(self.check(img)))
            cfg = pathlib.Path(d, 'config.txt')
            cfg.write_text((IMAGE / 'rpi' / 'boot' / 'config.txt').read_text().replace('enable_uart=1', ''))
            subprocess.run(['mcopy', '-o', '-i', part, str(cfg), '::config.txt'], check=True, env=env)
            self.assertIn('PI-config', self.failed_rules(self.check(img)))
            subprocess.run(['mdel', '-i', part, '::/EFI/BOOT/BOOTIA32.EFI'], check=True, env=env)
            self.assertIn('UEFI-ia32', self.failed_rules(self.check(img)))


if __name__ == '__main__':
    unittest.main()
