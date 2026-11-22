#!/usr/bin/env python3
"""The universal installer medium: one image file for a Raspberry Pi 4/5 SD card or USB
drive and a PC USB stick (UEFI and legacy BIOS). ARCHITECTURE 8.1, image/README.md.

    universal.py rpi-cmdline CMDLINE_TXT_IN MARKER     the Pi's live cmdline.txt (stdout)
    universal.py mbr IMAGE DISK_ID P1_START:P1_SECTORS P2_START:P2_SECTORS
                                                       writes the partition table into IMAGE
    universal.py check IMAGE [--json FILE] [--min-fat-mib N] [--rpi-config FILE] [--deep]
                                                       static checks (exit 1 on a failure)

Layout (MBR, no GPT):

    LBA 0             GRUB boot.img (legacy BIOS), disk id, partition table
    LBA 1 .. 2047     GRUB core.img (BIOS; embedded config finds the payload's marker)
    partition 1       FAT32 "RBINSTEFI", type 0x0c, active, at 1 MiB (512 MiB): the Raspberry
                      Pi boot files (config.txt, cmdline.txt, kernel8.img, the arm64 live
                      initramfs8, DTBs, overlays, start4.elf/fixup4.dat), the PC's
                      EFI/BOOT/BOOTX64.EFI and retroboot/ (network.conf, lab-auth.env,
                      Startup Details reports); writable on a stick or card
    partition 2       ISO 9660 "RETROBOOT", type 0x83, read-only payload: the marker file,
                      live/x86_64/{vmlinuz,initrd.img,microcode.cpio,rootfs.squashfs},
                      live/aarch64/rootfs.squashfs, live/embedded.squashfs (private),
                      live/installer-startup.rbis + live/startup.cpio (private), boot/grub/

Why these choices (official sources, pinned in image/reports/universal-image-2026-10-17.md):
* MBR, not GPT: a Raspberry Pi 4 bootloader reads GPT only from EEPROM 2020-09-03 on
  (rpi-eeprom firmware-2711 release notes, 2020-09-14 "Add support GPT and Hybrid MBR
  partition tables"); MBR works with every Pi 4 bootloader since the first (2019-05-10).
* Type 0x0c (FAT32 LBA), not 0xef: "Recognize efi partition (0xef) as a valid boot" came
  with the 2020-05-26 beta (rpi-eeprom #126). PC firmware boots \\EFI\\BOOT\\BOOTX64.EFI from
  any FAT partition of a removable disk (EDK2 builds a child handle for every non-empty,
  non-overlapping MBR entry and the FAT driver binds by content); Windows' own USB media
  use the same MBR + 0x0c layout.
* FAT first: the bootloader's default partition "resolves to the first bootable FAT
  partition (normally partition 1)"; a bootable partition is FAT12/16/32 with start.elf
  (start4.elf on a Pi 4) or, on a Pi 5, config.txt (documentation config_txt/autoboot.adoc).
  Bootloaders before 2020-10-28 stopped when a 0x83 partition came before the first FAT one.
* No overlapping or out-of-disk entries: EDK2's PartitionValidMbr rejects such a table.
"""
import argparse
import gzip
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile

SECTOR = 512
P1_START = 2048                       # 1 MiB
FAT_LABEL = 'RBINSTEFI'
PAYLOAD_LABEL = 'RETROBOOT'
TYPE_FAT32_LBA = 0x0C
TYPE_LINUX = 0x83
MARKER_PREFIX = '.retroboot-installer-'
LIVE_X86 = '/live/x86_64'
LIVE_ARM64 = '/live/aarch64'

# Pi boot files the image must carry (the Pi 4's GPU firmware: start4.elf/fixup4.dat; the
# Pi 5 keeps its firmware in the EEPROM). Same list as image/rpi/scripts/customize-rpi.sh.
PI_FILES = ('config.txt', 'cmdline.txt', 'kernel8.img', 'initramfs8', 'start4.elf', 'fixup4.dat',
            'bcm2711-rpi-4-b.dtb', 'bcm2711-rpi-400.dtb', 'bcm2711-rpi-cm4.dtb', 'bcm2712-rpi-5-b.dtb',
            'bcm2712d0-rpi-5-b.dtb', 'bcm2712-rpi-500.dtb', 'overlays/vc4-kms-v3d.dtbo',
            'overlays/vc4-kms-v3d-pi4.dtbo', 'overlays/vc4-kms-v3d-pi5.dtbo', 'overlays/overlay_map.dtb')
# cmdline.txt words of the installed Pi image that do not belong on the live medium
PI_LIVE_DROP = re.compile(r'^(root=.*|rootfstype=.*|rootwait|fsck\.repair=.*|retroboot\..*)$')


# ---------------------------------------------------------------- command lines

def rpi_live_cmdline(template, marker):
    """The live installer's cmdline.txt from image/rpi/boot/cmdline.txt.in: the same consoles
    and quiet arguments, without the installed system's root/slot words, with the live boot."""
    words = [w for w in template.split() if not PI_LIVE_DROP.match(w)]
    consoles = [w for w in words if w.startswith('console=')]
    rest = [w for w in words if not w.startswith('console=')]
    live = ['boot=retroboot-live', f'rb.live={marker}', 'fstab=no', 'retroboot.mode=installer',
            'retroboot.medium=universal']
    return ' '.join(consoles + live + rest)


# ---------------------------------------------------------------- MBR

def chs(lba):
    """CHS of an LBA for 255 heads x 63 sectors, or the "use LBA" value past cylinder 1023."""
    c, rem = divmod(lba, 255 * 63)
    if c > 1023:
        return bytes((0xFE, 0xFF, 0xFF))
    h, s = divmod(rem, 63)
    s += 1
    return bytes((h, ((c >> 2) & 0xC0) | s, c & 0xFF))


def mbr_entry(start, sectors, ptype, boot=False):
    return bytes((0x80 if boot else 0,)) + chs(start) + bytes((ptype,)) + chs(start + sectors - 1) + \
        struct.pack('<II', start, sectors)


def write_mbr(path, disk_id, parts):
    """parts: [(start, sectors, type, bootable)], at most four. Bytes 0-439 (boot code) stay."""
    table = b''.join(mbr_entry(*p) for p in parts).ljust(64, b'\0')
    with open(path, 'r+b') as f:
        f.seek(440)
        f.write(struct.pack('<I', disk_id) + b'\0\0' + table + b'\x55\xaa')


def read_mbr(sector0):
    parts = []
    for i in range(4):
        e = sector0[446 + 16 * i:462 + 16 * i]
        start, count = struct.unpack('<II', e[8:16])
        parts.append({'number': i + 1, 'boot': e[0], 'type': e[4], 'start': start, 'sectors': count})
    return parts


# ---------------------------------------------------------------- FAT

class FatError(Exception):
    pass


class Fat:
    """Read-only FAT12/16/32 reader for one partition of an image (long names included)."""

    def __init__(self, f, offset, part_sectors):
        self.f, self.offset = f, offset
        bs = self._read(0, 512)
        if bs[510:512] != b'\x55\xaa':
            raise FatError('no boot sector signature')
        (self.bps, self.spc, self.rsvd, self.nfats, self.root_ents, tot16, _media, fatsz16, _spt, _heads,
         self.hidden, tot32) = struct.unpack('<HBHBHHBHHHII', bs[11:36])
        if self.bps not in (512, 1024, 2048, 4096) or self.spc == 0 or self.nfats == 0:
            raise FatError('not a FAT boot sector')
        self.total = tot16 or tot32
        self.fatsz = fatsz16 or struct.unpack('<I', bs[36:40])[0]
        root_secs = (self.root_ents * 32 + self.bps - 1) // self.bps
        self.first_data = self.rsvd + self.nfats * self.fatsz + root_secs
        self.clusters = (self.total - self.first_data) // self.spc
        self.kind = 'FAT12' if self.clusters < 4085 else 'FAT16' if self.clusters < 65525 else 'FAT32'
        if self.kind == 'FAT32':
            self.root_clus = struct.unpack('<I', bs[44:48])[0]
            self.label = bs[71:82].decode('ascii', 'replace').rstrip()
            self.fstype = bs[82:90].decode('ascii', 'replace').rstrip()
            self.boot_sig = bs[66]
        else:
            self.root_clus = None
            self.label = bs[43:54].decode('ascii', 'replace').rstrip()
            self.fstype = bs[54:62].decode('ascii', 'replace').rstrip()
            self.boot_sig = bs[38]
        self.part_sectors = part_sectors
        self._fat = None

    def _read(self, pos, n):
        self.f.seek(self.offset + pos)
        return self.f.read(n)

    def fat(self):
        if self._fat is None:
            self._fat = self._read(self.rsvd * self.bps, self.fatsz * self.bps)
        return self._fat

    def next_cluster(self, c):
        fat = self.fat()
        if self.kind == 'FAT32':
            return struct.unpack_from('<I', fat, c * 4)[0] & 0x0FFFFFFF
        if self.kind == 'FAT16':
            return struct.unpack_from('<H', fat, c * 2)[0]
        v = struct.unpack_from('<H', fat, c + c // 2)[0]
        return v >> 4 if c & 1 else v & 0xFFF

    def chain(self, c):
        end = {'FAT32': 0x0FFFFFF8, 'FAT16': 0xFFF8, 'FAT12': 0xFF8}[self.kind]
        seen = set()
        while 2 <= c < end:
            if c in seen or c >= self.clusters + 2:
                raise FatError(f'broken cluster chain at {c}')
            seen.add(c)
            yield c
            c = self.next_cluster(c)

    def read_chain(self, c, size=None):
        csize = self.spc * self.bps
        out = bytearray()
        for cl in self.chain(c):
            out += self._read((self.first_data + (cl - 2) * self.spc) * self.bps, csize)
            if size is not None and len(out) >= size:
                break
        return bytes(out[:size]) if size is not None else bytes(out)

    def _dir_bytes(self, clus):
        if clus is None:   # FAT12/16 fixed root directory
            return self._read((self.rsvd + self.nfats * self.fatsz) * self.bps, self.root_ents * 32)
        return self.read_chain(clus)

    def entries(self, clus):
        """[(name, attr, first cluster, size)], the volume label as attr 0x08."""
        data, out, lfn = self._dir_bytes(clus), [], []
        for i in range(0, len(data), 32):
            e = data[i:i + 32]
            if e[0] == 0:
                break
            if e[0] == 0xE5:
                lfn = []
                continue
            attr = e[11]
            if attr == 0x0F:
                lfn.insert(0, (e[1:11] + e[14:26] + e[28:32]).decode('utf-16-le', 'replace'))
                continue
            base = e[0:8].decode('ascii', 'replace').rstrip()
            ext = e[8:11].decode('ascii', 'replace').rstrip()
            base = base.lower() if e[12] & 0x08 else base     # NT case flags
            ext = ext.lower() if e[12] & 0x10 else ext
            name = ''.join(lfn).split('\0')[0] if lfn else base + ('.' + ext if ext else '')
            lfn = []
            clus = (struct.unpack('<H', e[20:22])[0] << 16) | struct.unpack('<H', e[26:28])[0]
            out.append((name if attr != 0x08 else e[0:11].decode('ascii', 'replace').rstrip(), attr, clus,
                        struct.unpack('<I', e[28:32])[0]))
        return out

    def lookup(self, path):
        clus, entry = self.root_clus, None
        for part in [p for p in path.strip('/').split('/') if p]:
            match = [e for e in self.entries(clus) if e[1] != 0x0F and not e[1] & 0x08 and e[0].lower() == part.lower()]
            if not match:
                return None
            entry = match[0]
            clus = entry[2]
        return entry if entry else ('/', 0x10, self.root_clus, 0)

    def read(self, path):
        e = self.lookup(path)
        if e is None or e[1] & 0x10:
            return None
        return self.read_chain(e[2], e[3]) if e[3] else b''

    def listdir(self, path='/'):
        e = self.lookup(path)
        if e is None or not e[1] & 0x10:
            return None
        return [x[0] for x in self.entries(e[2] or self.root_clus) if x[1] != 0x0F and not x[1] & 0x08
                and x[0] not in ('.', '..')]

    def volume_label_entry(self):
        for name, attr, _c, _s in self.entries(self.root_clus):
            if attr & 0x08 and attr != 0x0F:
                return name
        return None

    def used_bytes(self):
        csize, used = self.spc * self.bps, 0
        for c in range(2, self.clusters + 2):
            if self.next_cluster(c):
                used += csize
        return used


# ---------------------------------------------------------------- ISO 9660

class IsoError(Exception):
    pass


class Iso:
    """Read-only ISO 9660 reader (Rock Ridge names, multi-extent files) at an image offset."""

    def __init__(self, f, offset):
        self.f, self.offset = f, offset
        pvd = self._read(16 * 2048, 2048)
        if pvd[0] != 1 or pvd[1:6] != b'CD001':
            raise IsoError('no ISO 9660 primary volume descriptor')
        self.volume_id = pvd[40:72].decode('ascii', 'replace').rstrip()
        self.blocks = struct.unpack('<I', pvd[80:84])[0]
        self.block = struct.unpack('<H', pvd[128:130])[0]
        root = self._record(pvd[156:190])
        self.root = root
        self.susp_skip = 0

    def _read(self, pos, n):
        self.f.seek(self.offset + pos)
        return self.f.read(n)

    @staticmethod
    def _record(r):
        n = r[32]
        return {'extent': struct.unpack('<I', r[2:6])[0], 'size': struct.unpack('<I', r[10:14])[0],
                'flags': r[25], 'name': r[33:33 + n], 'sua': r[33 + n + (1 - n % 2):r[0]]}

    def _susp(self, sua):
        """Rock Ridge NM name (with CE continuation) or None."""
        name, pos, data = b'', 0, sua
        cont = None
        while True:
            while pos + 4 <= len(data):
                sig, ln = data[pos:pos + 2], data[pos + 2]
                if ln < 4:
                    break
                body = data[pos + 4:pos + ln]
                if sig == b'NM':
                    name += body[1:]
                elif sig == b'CE':
                    cont = struct.unpack('<I', body[0:4])[0], struct.unpack('<I', body[8:12])[0], \
                        struct.unpack('<I', body[16:20])[0]
                elif sig == b'ST':
                    break
                pos += ln
            if not cont:
                break
            blk, off, ln = cont
            data, pos, cont = self._read(blk * self.block + off, ln), 0, None
        return name.decode('utf-8', 'replace') if name else None

    def entries(self, rec):
        out, data, i = [], self._read(rec['extent'] * self.block, rec['size']), 0
        while i < len(data):
            ln = data[i]
            if ln == 0:
                i = (i // self.block + 1) * self.block
                continue
            r = self._record(data[i:i + ln])
            i += ln
            if r['name'] in (b'\0', b'\1'):
                continue
            name = self._susp(r['sua'])
            if name is None:
                name = r['name'].decode('ascii', 'replace').split(';')[0].rstrip('.')
            if out and out[-1]['name'] == name and out[-1]['flags'] & 0x80:
                out[-1]['extents'].append((r['extent'], r['size']))
                out[-1]['size'] += r['size']
                out[-1]['flags'] = r['flags']
                continue
            r['name'] = name
            r['extents'] = [(r['extent'], r['size'])]
            out.append(r)
        return out

    def lookup(self, path):
        rec = self.root
        for part in [p for p in path.strip('/').split('/') if p]:
            if not rec['flags'] & 0x02:
                return None
            match = [e for e in self.entries(rec) if e['name'] == part]
            if not match:
                return None
            rec = match[0]
        return rec

    def listdir(self, path='/'):
        rec = self.lookup(path)
        return None if rec is None or not rec['flags'] & 0x02 else [e['name'] for e in self.entries(rec)]

    def file_offset(self, path):
        """(byte offset of the first extent from the ISO start, size): squashfs images are contiguous."""
        rec = self.lookup(path)
        if rec is None or rec['flags'] & 0x02:
            return None
        return rec['extents'][0][0] * self.block, rec['size']

    def read(self, path, limit=None):
        rec = self.lookup(path)
        if rec is None or rec['flags'] & 0x02:
            return None
        out = bytearray()
        for ext, size in rec['extents']:
            want = size if limit is None else min(size, limit - len(out))
            out += self._read(ext * self.block, want)
            if limit is not None and len(out) >= limit:
                break
        return bytes(out)


# ---------------------------------------------------------------- helpers for checks

def pe_info(data):
    """(machine, subsystem, pe32plus) of a PE image, or None."""
    if not data or data[:2] != b'MZ' or len(data) < 0x40:
        return None
    pe = struct.unpack_from('<I', data, 0x3C)[0]
    if data[pe:pe + 4] != b'PE\0\0':
        return None
    machine = struct.unpack_from('<H', data, pe + 4)[0]
    opt = pe + 24
    magic = struct.unpack_from('<H', data, opt)[0]
    return machine, struct.unpack_from('<H', data, opt + 68)[0], magic == 0x20B


def linux_version_of_bzimage(data):
    """The kernel version string of an x86 bzImage (setup header kernel_version)."""
    if not data or data[0x202:0x206] != b'HdrS':
        return None
    ptr = struct.unpack_from('<H', data, 0x20E)[0]
    s = data[ptr + 0x200:ptr + 0x200 + 128].split(b'\0')[0].decode('ascii', 'replace')
    return s.split()[0] if s else None


def linux_version_of_arm64_image(data):
    """The kernel version of a Raspberry Pi kernel8.img (gzip-compressed or raw arm64 Image)."""
    if data[:2] == b'\x1f\x8b':
        try:
            data = gzip.decompress(data)
        except (OSError, EOFError):
            return None
    if data[0x38:0x3C] != b'ARMd':
        return None
    m = re.search(rb'Linux version (\S+)', data)
    return m.group(1).decode() if m else None


def config_txt_problems(text):
    """The Pi image's config.txt rules (image/rpi/scripts/make-boot.sh, check-image.sh)."""
    problems, section, seen_overlay, audio = [], 'all', False, None
    lines = [l.strip() for l in text.splitlines()]
    sections = {}
    for line in lines:
        if not line or line.startswith('#'):
            continue
        m = re.fullmatch(r'\[(.+)\]', line)
        if m:
            section = m.group(1)
            continue
        sections.setdefault(section, []).append(line)
        if section == 'all':
            if line.startswith('dtoverlay='):
                seen_overlay = True
            if line == 'dtparam=audio=on':
                audio = not seen_overlay
    for want in ('arm_64bit=1', 'kernel=kernel8.img', 'initramfs initramfs8 followkernel', 'disable_splash=1',
                 'enable_uart=1', 'dtoverlay=vc4-kms-v3d'):
        if want not in sections.get('all', []):
            problems.append(f'config.txt lacks "{want}" under [all]')
    if audio is None:
        problems.append('config.txt lacks dtparam=audio=on')
    elif not audio:
        problems.append('config.txt: dtparam=audio=on must come before the first dtoverlay=')
    if 'pi5' in sections and any(l.startswith(('kernel=', 'initramfs')) for l in sections['pi5']):
        problems.append('config.txt switches the Pi 5 to another kernel')
    return problems


def cmdline_problems(text, marker):
    problems = []
    if len(text.strip().splitlines()) != 1:
        problems.append('cmdline.txt must be one line')
    words = text.split()
    consoles = [w for w in words if w.startswith('console=')]
    if not consoles or consoles[0] != 'console=serial0,115200':
        problems.append('cmdline.txt: console=serial0,115200 must be the first console=')
    vt_consoles = [w for w in consoles if re.fullmatch(r'console=tty\d+.*', w)]
    if vt_consoles and (vt_consoles != ['console=tty0'] or
                        any(w not in words for w in ('fbcon=map:9', 'drm_kms_helper.fbdev_emulation=0',
                                                    'quiet', 'loglevel=0'))):
        problems.append('cmdline.txt has a visible or unguarded VT console')
    for want in ('boot=retroboot-live', f'rb.live={marker}', 'fstab=no', 'retroboot.mode=installer',
                 'retroboot.medium=universal', 'quiet', 'loglevel=0', 'cgroup_enable=memory'):
        if want not in words:
            problems.append(f'cmdline.txt lacks {want}')
    for bad in [w for w in words if w.startswith(('root=', 'retroboot.slot='))]:
        problems.append(f'cmdline.txt has the installed system\'s {bad}')
    return problems


def grub_cfg_problems(text, marker, startup):
    problems = []
    initrd = f'initrd {LIVE_X86}/microcode.cpio {LIVE_X86}/initrd.img' + (' /live/startup.cpio' if startup else '')
    linux = f'linux {LIVE_X86}/vmlinuz boot=retroboot-live rb.live={marker} fstab=no retroboot.mode=installer ' \
            'retroboot.medium=universal'
    entries = re.findall(r'menuentry "[^"]+" --id (\S+) \{\n\t(linux [^\n]*)\n\t(initrd [^\n]*)\n\}', text)
    if [e[0] for e in entries] != ['installer', 'details', 'safe-display']:
        problems.append(f'grub.cfg menu entries: {[e[0] for e in entries]}')
    for eid, lin, ini in entries:
        if not lin.startswith(linux):
            problems.append(f'grub.cfg entry {eid}: {lin[:120]}')
        if ini != initrd:
            problems.append(f'grub.cfg entry {eid}: {ini}')
    for want in ('set timeout=0', 'set timeout_style=hidden', 'set default=installer', 'set gfxpayload=keep',
                 'keystatus --shift', 'amdgpu.dcdebugmask=0x410', 'loadfont /boot/grub/fonts/blank.pf2'):
        if want not in text:
            problems.append(f'grub.cfg lacks "{want}"')
    return problems


def tool(name):
    return shutil.which(name)


def initramfs_names(data, files=None):
    """File names in an initramfs as the kernel unpacks it: uncompressed newc archives
    (initramfs-tools puts the already compressed modules first, uncompressed), zero padding,
    a compressed archive (zstd, gzip), then possibly more uncompressed archives appended
    (the startup box) at 4-byte aligned offsets. None if a compressed part can't be read.
    FILES (a dict of name: None) receives the contents of those names."""
    names, pos = [], 0
    while pos < len(data):
        while pos < len(data) and data[pos] == 0:
            pos += 1
        if pos >= len(data):
            break
        if data[pos:pos + 6] == b'070701':
            found, end = newc_parse(data, pos, files)
            if found is None or pos % 4:
                return None
            names += found
            pos = end
            continue
        if data[pos:pos + 4] == b'\x28\xb5\x2f\xfd' and tool('zstd'):
            raw = subprocess.run(['zstd', '-dcq'], input=data[pos:], capture_output=True).stdout
        elif data[pos:pos + 2] == b'\x1f\x8b':
            raw = gzip.GzipFile(fileobj=__import__('io').BytesIO(data[pos:])).read()
        else:
            return None
        i = 0
        while i < len(raw) and raw[i:i + 6] == b'070701':
            found, i = newc_parse(raw, i, files)
            if found is None:
                return None
            names += found
            while i < len(raw) and raw[i] == 0:
                i += 1
        # an uncompressed archive appended after the compressed one
        base = max(pos, len(data) - (8 << 20))
        for m in re.finditer(rb'070701', data[base:]):
            at = base + m.start()
            found, end = newc_parse(data, at, files)
            # the kernel takes it where the offset is a multiple of 4 (zero padding skipped)
            if found and at % 4 == 0 and not data[end:].strip(b'\0'):
                names += found
                break
        break
    return names


def newc_parse(raw, i, files=None):
    """(names, end offset) of the newc archive at raw[i:], or (None, i) unless it ends with
    its trailer; FILES as for initramfs_names."""
    names = []
    while i + 110 <= len(raw) and raw[i:i + 6] == b'070701':
        try:
            namesize = int(raw[i + 94:i + 102], 16)
            filesize = int(raw[i + 54:i + 62], 16)
        except ValueError:
            return None, i
        name = raw[i + 110:i + 110 + namesize - 1].decode('utf-8', 'replace')
        i = (i + 110 + namesize + 3) & ~3
        if files is not None and name in files:
            files[name] = raw[i:i + filesize]
        i = (i + filesize + 3) & ~3
        if name == 'TRAILER!!!':
            return names, i
        if not name.isprintable():
            return None, i
        names.append(name)
    return None, i


def elf_machine(data):
    if not data or data[:4] != b'\x7fELF':
        return None
    return struct.unpack_from('<H', data, 18)[0]


def squashfs_cat(image, offset, path):
    """A file from a squashfs inside IMAGE at OFFSET (unsquashfs -o), or None."""
    if not tool('unsquashfs'):
        return None
    with tempfile.TemporaryDirectory() as d:
        p = subprocess.run(['unsquashfs', '-q', '-n', '-o', str(offset), '-d', os.path.join(d, 'x'), image,
                            path.lstrip('/')], capture_output=True)
        f = os.path.join(d, 'x', path.lstrip('/'))
        if p.returncode != 0 or not os.path.isfile(f):
            return None
        with open(f, 'rb') as fh:
            return fh.read()


def squashfs_list(image, offset, path):
    if not tool('unsquashfs'):
        return None
    p = subprocess.run(['unsquashfs', '-o', str(offset), '-lc', image, path.lstrip('/')], capture_output=True, text=True)
    return [l.split('squashfs-root', 1)[1] for l in p.stdout.splitlines() if 'squashfs-root' in l] if p.returncode == 0 else None


# ---------------------------------------------------------------- the checker

class Report:
    def __init__(self):
        self.lines, self.failed, self.facts = [], 0, {}

    def ok(self, rule, msg):
        self.lines.append(f'ok    {rule:10} {msg}')

    def fail(self, rule, msg):
        self.failed += 1
        self.lines.append(f'FAIL  {rule:10} {msg}')

    def note(self, rule, msg):
        self.lines.append(f'note  {rule:10} {msg}')

    def expect(self, cond, rule, good, bad=None):
        (self.ok if cond else self.fail)(rule, good if cond else (bad or good))
        return cond


def check(image, min_fat_mib=512, rpi_config=None, deep=False):
    r = Report()
    size = os.path.getsize(image)
    r.facts['image_bytes'] = size
    with open(image, 'rb') as f:
        s0 = f.read(SECTOR)
        lba1 = f.read(SECTOR)
        f.seek(0)
        gap = f.read(P1_START * SECTOR)
        disk_sectors = size // SECTOR
        if not r.expect(s0[510:512] == b'\x55\xaa', 'MBR', 'MBR signature 55aa'):
            return r
        parts = read_mbr(s0)
        used = [p for p in parts if p['type'] or p['sectors']]
        r.facts['partitions'] = used
        r.expect(not any(p['type'] == 0xEE for p in parts) and lba1[:8] != b'EFI PART', 'MBR-noGPT',
                 'plain MBR, no GPT (a Pi 4 bootloader reads GPT only from EEPROM 2020-09-03)',
                 'GPT found: a Pi 4 bootloader older than 2020-09-03 cannot read it')
        r.expect(size % (1 << 20) == 0, 'MBR-size', f'image {size} bytes, a whole number of MiB')
        # UEFI (EDK2 PartitionValidMbr): non-empty entries inside the disk, none overlapping
        bad = [p for p in used if p['sectors'] == 0 or p['start'] == 0 or p['start'] + p['sectors'] > disk_sectors]
        ovl = [(a['number'], b['number']) for i, a in enumerate(used) for b in used[i + 1:]
               if a['start'] < b['start'] + b['sectors'] and b['start'] < a['start'] + a['sectors']]
        r.expect(not bad and not ovl, 'UEFI-mbr', 'partition entries inside the disk and not overlapping',
                 f'invalid entries {[p["number"] for p in bad]}, overlaps {ovl}')
        p1, p2 = parts[0], parts[1]
        r.expect(p1['type'] == TYPE_FAT32_LBA, 'PI-type', 'partition 1 type 0x0c (FAT32 LBA: every Pi 4 bootloader)',
                 f'partition 1 type {p1["type"]:#04x}, want 0x0c (0xef only from Pi 4 EEPROM 2020-05-26)')
        r.expect(p1['boot'] == 0x80, 'BIOS-active', 'partition 1 marked active (BIOS USB-HDD convention)',
                 f'partition 1 boot flag {p1["boot"]:#04x}')
        r.expect(p1['start'] == P1_START, 'PI-first', 'partition 1 starts at 1 MiB and is the first FAT partition',
                 f'partition 1 starts at LBA {p1["start"]}')
        r.expect(p1['sectors'] >= min_fat_mib * 2048, 'PI-size', f'partition 1 {p1["sectors"] // 2048} MiB',
                 f'partition 1 {p1["sectors"] // 2048} MiB < {min_fat_mib} MiB')
        p1_end = p1['start'] + p1['sectors']
        r.expect(p2['type'] == TYPE_LINUX and p2['start'] >= p1_end and p2['start'] % 2048 == 0
                 and p2['start'] + p2['sectors'] <= disk_sectors, 'PAY-part',
                 f'partition 2 type 0x83 at LBA {p2["start"]} (1 MiB aligned, after partition 1), '
                 f'{p2["sectors"] * SECTOR} bytes',
                 f'partition 2: type {p2["type"]:#04x} start {p2["start"]} sectors {p2["sectors"]}')
        r.expect(not parts[2]['type'] and not parts[3]['type'], 'MBR-p34', 'partitions 3 and 4 empty')
        # Legacy BIOS: GRUB boot.img in the MBR, core.img right after it in the gap
        kernel_sector = struct.unpack_from('<Q', s0, 0x5C)[0]
        r.expect(s0[:3] == b'\xeb\x63\x90' and kernel_sector == 1, 'BIOS-mbr',
                 'GRUB boot.img in the MBR, core.img at sector 1',
                 f'MBR boot code {s0[:3].hex()} kernel sector {kernel_sector}')
        last = max((i for i in range(1, P1_START) if gap[i * SECTOR:(i + 1) * SECTOR].strip(b'\0')), default=0)
        r.facts['bios_core_sectors'] = last
        # diskboot.img (core.img's first sector) loads the rest by its blocklist, which
        # grub-bios-setup fills in: from sector 2, the remaining sectors, to segment 0x820
        bl_start, bl_len, bl_seg = struct.unpack_from('<QHH', gap, SECTOR + 0x1F4)
        r.expect(bl_start == 2 and bl_seg == 0x820 and 0 < bl_len and bl_start + bl_len - 1 <= last < P1_START,
                 'BIOS-core', f'core.img in sectors 1..{last} (before partition 1), blocklist {bl_start}+{bl_len}',
                 f'core.img: last data sector {last}, blocklist {bl_start}+{bl_len} segment {bl_seg:#x}')

        # ---- partition 1: FAT32 with the Pi boot files and the PC's EFI loader
        try:
            fat = Fat(f, p1['start'] * SECTOR, p1['sectors'])
        except FatError as exc:
            r.fail('PI-fat', f'partition 1: {exc}')
            return r
        r.expect(fat.kind == 'FAT32' and fat.bps == 512 and fat.boot_sig == 0x29, 'PI-fat',
                 f'partition 1 is {fat.kind} ({fat.clusters} clusters of {fat.spc * fat.bps} bytes)',
                 f'partition 1 is {fat.kind}, {fat.bps}-byte sectors')
        r.expect(fat.total <= p1['sectors'] and fat.hidden == p1['start'], 'PI-fatgeom',
                 f'FAT size {fat.total} sectors within the partition, hidden sectors = {fat.hidden}',
                 f'FAT total {fat.total} hidden {fat.hidden} vs partition {p1["start"]}+{p1["sectors"]}')
        r.expect(fat.label == FAT_LABEL and (fat.volume_label_entry() or '') == FAT_LABEL, 'PI-label',
                 f'FAT label {FAT_LABEL} (boot sector and root directory)',
                 f'FAT labels "{fat.label}" / "{fat.volume_label_entry()}"')
        missing = [p for p in PI_FILES if not fat.lookup(p)]
        r.expect(not missing, 'PI-files', f'Pi boot files present ({len(PI_FILES)}: start4.elf, fixup4.dat, '
                 'config.txt, kernel8.img, initramfs8, Pi 4/400/CM4/5/500 DTBs, vc4 overlays)', f'missing: {missing}')
        r.facts['fat'] = {'kind': fat.kind, 'cluster_bytes': fat.spc * fat.bps, 'bytes': fat.total * fat.bps,
                          'used_bytes': fat.used_bytes(), 'root': sorted(fat.listdir('/') or [])}
        cfg = (fat.read('config.txt') or b'').decode('utf-8', 'replace')
        problems = config_txt_problems(cfg)
        r.expect(not problems, 'PI-config', 'config.txt: arm_64bit, kernel8.img + initramfs8, silent firmware, '
                 'audio before vc4-kms-v3d, enable_uart', '; '.join(problems))
        if rpi_config:
            with open(rpi_config) as rc:
                r.expect(rc.read() == cfg, 'PI-config', 'config.txt identical to the Pi image\'s',
                         'config.txt differs from the Pi image\'s')
        cmdline = (fat.read('cmdline.txt') or b'').decode('utf-8', 'replace')
        m = re.search(r'(?:^| )rb\.live=(\S+)', cmdline)
        marker = m.group(1) if m else None
        r.facts['marker'] = marker
        problems = cmdline_problems(cmdline, marker or '?')
        r.expect(marker and marker.startswith(MARKER_PREFIX) and not problems, 'PI-cmdline',
                 'cmdline.txt: live boot, serial0 console first, quiet, no root=', '; '.join(problems) or 'no marker')
        k8 = fat.read('kernel8.img') or b''
        kver_arm = linux_version_of_arm64_image(k8)
        r.facts['kernel8'] = kver_arm
        r.expect(kver_arm is not None, 'PI-kernel', f'kernel8.img: arm64 Linux {kver_arm}', 'kernel8.img is no arm64 Image')
        efi = fat.read('EFI/BOOT/BOOTX64.EFI')
        info = pe_info(efi)
        r.expect(info is not None and info[0] == 0x8664 and info[1] == 10 and info[2], 'UEFI-loader',
                 r'\EFI\BOOT\BOOTX64.EFI: PE32+ x86-64 EFI application (removable-media path)',
                 f'BOOTX64.EFI: {info}')
        if marker:
            r.expect(efi is not None and marker.encode() in efi, 'UEFI-marker',
                     'BOOTX64.EFI\'s embedded config searches this build\'s marker', 'BOOTX64.EFI lacks the marker')
        r.expect(fat.lookup('retroboot') is not None, 'FAT-retroboot', 'retroboot/ folder for network.conf, '
                 'lab-auth.env and Startup Details reports', 'no retroboot/ folder')
        stray = [n for n in ('grub', 'EFI/retroboot') if fat.lookup(n)]
        r.expect(not stray, 'FAT-noslots', 'no installed-system slot files on the FAT', f'unexpected {stray}')

        # ---- partition 2: the ISO 9660 payload
        try:
            iso = Iso(f, p2['start'] * SECTOR)
        except IsoError as exc:
            r.fail('PAY-iso', f'partition 2: {exc}')
            return r
        r.expect(iso.volume_id == PAYLOAD_LABEL and iso.block == 2048 and iso.blocks * 2048 <= p2['sectors'] * SECTOR,
                 'PAY-iso', f'partition 2: ISO 9660 "{iso.volume_id}", {iso.blocks * 2048} bytes',
                 f'partition 2: volume "{iso.volume_id}" block {iso.block} blocks {iso.blocks}')
        root = iso.listdir('/') or []
        markers = [n for n in root if n.startswith(MARKER_PREFIX)]
        r.expect(markers == [marker], 'PAY-marker', f'payload marker {marker} (= cmdline.txt rb.live)',
                 f'payload markers {markers}, cmdline.txt {marker}')
        r.expect(not fat.lookup(marker or '-'), 'PAY-marker', 'the FAT partition has no marker (only the payload is '
                 'the medium)', 'marker on the FAT partition')
        files = {}
        for rel in ('vmlinuz', 'initrd.img', 'microcode.cpio', 'rootfs.squashfs'):
            files[f'{LIVE_X86}/{rel}'] = iso.lookup(f'{LIVE_X86}/{rel}')
        files[f'{LIVE_ARM64}/rootfs.squashfs'] = iso.lookup(f'{LIVE_ARM64}/rootfs.squashfs')
        missing = [p for p, rec in files.items() if rec is None]
        r.expect(not missing, 'PAY-files', 'x86_64 kernel, initrd, microcode and root; aarch64 root', f'missing {missing}')
        for p in (f'{LIVE_X86}/rootfs.squashfs', f'{LIVE_ARM64}/rootfs.squashfs', '/live/embedded.squashfs'):
            head = iso.read(p, 4)
            if head is not None:
                r.expect(head == b'hsqs', 'PAY-sqfs', f'{p}: squashfs', f'{p}: not a squashfs')
        r.expect(iso.lookup('/live/rootfs.squashfs') is None, 'PAY-noflat', 'no single-architecture /live/rootfs.squashfs')
        vml = iso.read(f'{LIVE_X86}/vmlinuz', 1 << 16)
        kver_x86 = linux_version_of_bzimage(vml)
        r.facts['vmlinuz'] = kver_x86
        r.expect(kver_x86 is not None, 'PAY-vmlinuz', f'x86_64 bzImage {kver_x86}', 'vmlinuz is no bzImage')
        startup = iso.lookup('/live/installer-startup.rbis') is not None
        r.expect(startup == (iso.lookup('/live/startup.cpio') is not None), 'PAY-startup',
                 f'installer startup box {"present (both arches)" if startup else "absent (RetroBoot art)"}',
                 'installer-startup.rbis and startup.cpio must come together')
        grub = (iso.read('/boot/grub/grub.cfg') or b'').decode('utf-8', 'replace')
        problems = grub_cfg_problems(grub, marker or '?', startup)
        r.expect(not problems, 'PAY-grub', 'grub.cfg: silent checkerboard, Shift startup options, three entries '
                 f'booting {LIVE_X86} with the live arguments', '; '.join(problems))
        for p in ('/boot/grub/fonts/blank.pf2', '/boot/grub/retroboot/checker-default.png'):
            r.expect(iso.lookup(p) is not None, 'PAY-grub', f'{p}', f'{p} missing')
        if iso.lookup('/boot/grub/fonts/retroboot-menu.pf2') is None:
            r.note('PAY-grub', 'no startup-options font: Shift at power-on shows nothing')
        private = [p for p in ('/retroboot/lab-auth.env', '/retroboot/network.conf', '/live/embedded.squashfs')
                   if iso.lookup(p) is not None]
        private += [f'FAT:{p}' for p in ('retroboot/lab-auth.env', 'retroboot/network.conf') if fat.lookup(p)]
        r.facts['private_files'] = private
        if private:
            r.note('PRIVATE', f'private content: {", ".join(private)} (keep this image private)')
        sizes = {}
        for p in (f'{LIVE_X86}/rootfs.squashfs', f'{LIVE_X86}/initrd.img', f'{LIVE_X86}/vmlinuz',
                  f'{LIVE_X86}/microcode.cpio', f'{LIVE_ARM64}/rootfs.squashfs', '/live/embedded.squashfs',
                  '/live/startup.cpio', '/live/installer-startup.rbis'):
            rec = iso.lookup(p)
            if rec is not None:
                sizes[p] = rec['size']
        sizes['FAT kernel8.img'] = len(k8)
        sizes['FAT initramfs8'] = len(fat.read('initramfs8') or b'')
        r.facts['payload_bytes'] = iso.blocks * 2048
        r.facts['sizes'] = sizes

        if not deep:
            return r
        # ---- deep checks: initramfs contents and the roots (zstd, unsquashfs)
        for label, data in (('initramfs8 (arm64 live)', fat.read('initramfs8')),
                            ('x86_64 initrd.img', iso.read(f'{LIVE_X86}/initrd.img'))):
            got = {'scripts/retroboot-live': None}
            names = initramfs_names(data or b'', got)
            if names is None:
                r.note('DEEP', f'{label}: not inspected (zstd missing)')
                continue
            script = (got['scripts/retroboot-live'] or b'').decode('utf-8', 'replace')
            r.expect('rb_live_rootfs()' in script and 'rb_load_usb_storage()' in script, 'DEEP-live',
                     f'{label}: retroboot-live picks the root by processor and loads USB storage',
                     f'{label}: retroboot-live is an older one')
            want = ['scripts/retroboot-live']
            if label.startswith('initramfs8'):
                want += ['squashfs.ko', 'overlay.ko', 'isofs.ko']
            lacking = [w for w in want if not any(n.endswith(w) or (w.endswith('.ko') and re.search(
                re.escape(w[:-3]) + r'\.ko(\.(xz|zst|gz))?$', n)) for n in names)]
            r.expect(not lacking, 'DEEP-initrd', f'{label}: retroboot-live' + (', squashfs, overlay, isofs' if
                     label.startswith('initramfs8') else ''), f'{label} lacks {lacking}')
            if label.startswith('initramfs8'):
                r.expect(any(n.endswith('retroboot/installer-startup.rbis') for n in names) == startup, 'DEEP-initrd',
                         'initramfs8 carries the installer startup box exactly when the payload does',
                         'initramfs8 and the payload disagree about the startup box')
        for label, d, machine, kver in (('x86_64', LIVE_X86, 62, kver_x86), ('aarch64', LIVE_ARM64, 183, kver_arm)):
            loc = iso.file_offset(f'{d}/rootfs.squashfs')
            if not loc or not tool('unsquashfs'):
                r.note('DEEP', f'{label} root not inspected (unsquashfs missing)')
                continue
            off = p2['start'] * SECTOR + loc[0]
            qemu = squashfs_cat(image, off, '/usr/lib/retroboot/qemu/bin/qemu-system-ppc')
            r.expect(elf_machine(qemu) == machine, 'DEEP-root', f'{label} root: qemu-system-ppc is {label}',
                     f'{label} root: qemu-system-ppc machine {elf_machine(qemu)}')
            # The 68k Macs' emulator (optional while the arm64 one is not built): when present,
            # for this processor too.
            m68k = squashfs_cat(image, off, '/usr/lib/retroboot/qemu-m68k/bin/qemu-system-m68k')
            if m68k is None:
                r.note('DEEP', f'{label} root has no 68k emulator (68k Macs can\'t start there)')
            else:
                r.expect(elf_machine(m68k) == machine, 'DEEP-root', f'{label} root: qemu-system-m68k is {label}',
                         f'{label} root: qemu-system-m68k machine {elf_machine(m68k)}')
            mods = squashfs_list(image, off, '/usr/lib/modules') or []
            kvers = sorted({m.split('/modules/', 1)[1].split('/')[0] for m in mods if '/modules/' in m})
            r.expect(kver in kvers, 'DEEP-root', f'{label} root has the modules of its kernel {kver}',
                     f'{label} root modules {kvers}, kernel {kver}')
            inst = squashfs_cat(image, off, '/usr/lib/retroboot/installer/install-to-disk')
            r.expect(inst is not None, 'DEEP-root', f'{label} root: the installer backend', f'{label} root lacks install-to-disk')
            rel = squashfs_cat(image, off, '/usr/lib/retroboot/release.json')
            if rel:
                r.facts.setdefault('release', {})[label] = json.loads(rel)
        rels = r.facts.get('release', {})
        if len(rels) == 2:
            a, b = rels['x86_64'], rels['aarch64']
            r.expect(a.get('build_id') == b.get('build_id') and a.get('version') == b.get('version'), 'DEEP-root',
                     f'both roots are build {a.get("build_id")}', f'roots differ: {a} / {b}')
    return r


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    c = sub.add_parser('rpi-cmdline')
    c.add_argument('template')
    c.add_argument('marker')
    m = sub.add_parser('mbr')
    m.add_argument('image')
    m.add_argument('disk_id')
    m.add_argument('p1')
    m.add_argument('p2')
    k = sub.add_parser('check')
    k.add_argument('image')
    k.add_argument('--json')
    k.add_argument('--min-fat-mib', type=int, default=512)
    k.add_argument('--rpi-config')
    k.add_argument('--deep', action='store_true')
    a = ap.parse_args(argv)
    if a.cmd == 'rpi-cmdline':
        with open(a.template) as f:
            print(rpi_live_cmdline(f.read(), a.marker))
        return 0
    if a.cmd == 'mbr':
        s1, n1 = (int(x) for x in a.p1.split(':'))
        s2, n2 = (int(x) for x in a.p2.split(':'))
        write_mbr(a.image, int(a.disk_id, 16), [(s1, n1, TYPE_FAT32_LBA, True), (s2, n2, TYPE_LINUX, False)])
        return 0
    rep = check(a.image, a.min_fat_mib, a.rpi_config, a.deep)
    print('\n'.join(rep.lines))
    print(f'{"FAILED" if rep.failed else "PASSED"}: {rep.failed} failure(s)')
    if a.json:
        with open(a.json, 'w') as f:
            json.dump({'failures': rep.failed, 'lines': rep.lines, 'facts': rep.facts}, f, indent=2)
    return 1 if rep.failed else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
