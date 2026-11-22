"""Raspberry Pi destinations for the live installer (ARCHITECTURE 8.2).

Imported by ``install-to-disk --board rpi`` and ``list-disks`` (both put their own
directory on sys.path; they run with ``python3 -I``). Two destinations:

* **free space** (``--mode free-space``): the installer's own SD card or USB stick.
  The installer medium (universal image) is partition 1, a FAT32 partition labelled
  RBINSTEFI holding the Pi boot files (and the PC's EFI loader), then the payload
  partition with the live root. Everything up to the end of the payload is kept;
  RetroBoot's root (ext4, label RBROOT_A, 3 GiB) and data (XFS, label RBDATA, the
  rest) are added in the space after it. The Pi firmware keeps booting from
  partition 1: its ``config.txt`` becomes the installed system's (``os_prefix``
  ``retroboot/os/`` holds that system's kernel, initramfs, cmdline.txt, DTBs and
  overlays, so nothing of the installer's own boot files changes) and the
  installer's configuration is kept as ``tryboot.txt``, which the firmware reads
  instead of ``config.txt`` for one boot after ``reboot "0 tryboot"`` (Recovery's
  Start Installer…; a boot without installing comes back to RetroBoot). An MBR
  card needs two free primary slots (no extended partitions are made); the new
  partitions carry no GPT names, so the installed fstab names them by PARTUUID.
  A previous installation in that space (RBROOT_A + RBDATA, or empty partitions
  from an interrupted one) is replaced after an explicit confirmation; any other
  partition after the installer means the space is not offered.
* **disk** (``--mode disk``): another disk, erased and given image/rpi's standard
  layout (GPT: RBEFI FAT32 type 0700 labelled RETROBOOT, BIOSBOOT placeholder,
  RBROOT_A, RBROOT_B, RBDATA) with a complete Pi boot partition made from the
  installed root itself. The Pi starts from it when its bootloader's BOOT_ORDER
  reaches that disk first (Pi 4 default 0xf41: SD card, then USB); nothing here
  touches the EEPROM.

The pure parts (free-space plan, MBR entries, config.txt, fstab) are unit-tested in
image/tests/test_image_tools.py; the rest runs commands.
"""
import json
import os
import re
import shutil
import struct
import subprocess

MiB = 1 << 20
GiB = 1 << 30
SECTOR = 512
ALIGN = 4 * MiB                      # SD erase blocks: new partitions start on 4 MiB
ROOT_BYTES = 3 * GiB                 # RBROOT_A (the x86 slot size)
MIN_DATA_BYTES = 2 * GiB
FREE_MIN_BYTES = ROOT_BYTES + MIN_DATA_BYTES
FREE_FIXED_BYTES = ROOT_BYTES + ALIGN   # what the free-space layout takes besides RBDATA
GPT_TAIL = 34 * SECTOR               # backup GPT header and entries at the end of the disk
INSTALLER_FAT_LABEL = 'RBINSTEFI'
OS_PREFIX = 'retroboot/os/'
INSTALLER_CONFIG = 'retroboot/installer-config.txt'
CONFIG_MARK = '# RetroBoot: installed beside the installer on this card (ARCHITECTURE 8.2)'
LINUX_MBR_TYPE = 0x83
FIRMWARE_GLOBS = ('start4', 'fixup4')    # Pi 4 GPU firmware (the Pi 5's is in its EEPROM)


class LayoutError(Exception):
    pass


def align_up(v, a=ALIGN):
    return (v + a - 1) // a * a


def align_down(v, a=SECTOR):
    return v // a * a


# --- free space after the installer -------------------------------------------------

REASONS = {
    'medium-not-partition': 'the installer is not on a partition of this disk',
    'no-installer-boot': 'its first partition is not the installer’s startup partition (RBINSTEFI)',
    'other-partitions': 'other partitions follow the installer',
    'logical-partitions': 'it has extended partitions',
    'table-full': 'its partition table has no room for two more partitions',
    'unknown-table': 'its partition table is not MBR or GPT',
    'too-small': 'there is not enough free space after the installer',
}


def previous_install(after):
    """True when every partition after the installer is from a RetroBoot installation in
    that space (ext4 RBROOT_A, XFS RBDATA, or empty ones an interrupted installation left)."""
    if not after or len(after) > 2:
        return False
    for p in after:
        fs, label = (p.get('fstype') or ''), (p.get('label') or '')
        if fs == 'ext4' and label == 'RBROOT_A':
            continue
        if fs == 'xfs' and label == 'RBDATA':
            continue
        if not fs:
            continue
        return False
    return True


def plan_free_space(table, disk_bytes, parts, medium):
    """Where RetroBoot can go on the installer's own disk.

    ``parts``: the disk's partitions, each {name, number, start, size (bytes), fstype,
    label}; ``medium``: the name of the partition the installer's payload was found
    on. Returns {eligible, reason, bytes, start, end, table, existing, remove,
    numbers, boot_partition}: the free region [start, end) after the installer
    (start on 4 MiB), the partition numbers the root and the data get, the old
    partitions to remove first (``existing`` "RetroBoot")."""
    out = {'eligible': False, 'reason': None, 'bytes': 0, 'start': 0, 'end': 0, 'table': table,
           'existing': None, 'remove': [], 'numbers': [], 'boot_partition': None}
    byname = {p['name']: p for p in parts}
    med = byname.get(medium)
    if med is None:
        out['reason'] = 'medium-not-partition'
        return out
    first = min(parts, key=lambda p: p['number'])
    out['boot_partition'] = first['name']
    if first['number'] != 1 or first.get('fstype') != 'vfat' or first.get('label') != INSTALLER_FAT_LABEL:
        out['reason'] = 'no-installer-boot'
        return out
    med_end = med['start'] + med['size']
    kept = [p for p in parts if p['start'] < med_end]
    after = [p for p in parts if p['start'] >= med_end]
    if after:
        if not previous_install(after):
            out['reason'] = 'other-partitions'
            return out
        out['existing'] = 'RetroBoot' if any(p.get('fstype') for p in after) else 'empty'
        out['remove'] = sorted(p['number'] for p in after)
    keep_end = max(p['start'] + p['size'] for p in kept)
    if table == 'dos':
        if any(p['number'] > 4 for p in parts):
            out['reason'] = 'logical-partitions'
            return out
        free = [n for n in (1, 2, 3, 4) if n not in {p['number'] for p in kept}]
        if len(free) < 2:
            out['reason'] = 'table-full'
            return out
        out['numbers'] = free[:2]
        end = align_down(disk_bytes)
    elif table == 'gpt':
        top = max(p['number'] for p in kept)
        if top + 2 > 128:
            out['reason'] = 'table-full'
            return out
        out['numbers'] = [top + 1, top + 2]
        end = align_down(disk_bytes - GPT_TAIL)
    else:
        out['reason'] = 'unknown-table'
        return out
    start = align_up(keep_end)
    out.update(start=start, end=end, bytes=max(0, end - start))
    if out['bytes'] < FREE_MIN_BYTES:
        out['reason'] = 'too-small'
        return out
    out['eligible'] = True
    return out


def free_space_layout(plan):
    """[(number, start, end, fs, label)] for the root and the data in the free region."""
    a, b = plan['numbers']
    root_end = plan['start'] + ROOT_BYTES
    return [(a, plan['start'], root_end, 'ext4', 'RBROOT_A'), (b, root_end, plan['end'], 'xfs', 'RBDATA')]


# --- MBR ------------------------------------------------------------------------------

def _chs(lba):
    """CHS bytes for an MBR entry (255 heads, 63 sectors); FE FF FF past cylinder 1023."""
    c, rem = divmod(lba, 255 * 63)
    if c > 1023:
        return b'\xfe\xff\xff'
    h, s = divmod(rem, 63)
    s += 1
    return bytes((h, (s & 0x3f) | ((c >> 2) & 0xc0), c & 0xff))


def mbr_parse(sector0):
    """{disk_id, entries: {slot: (boot, type, start_lba, sectors)}} of an MBR sector."""
    if len(sector0) < 512 or sector0[510:512] != b'\x55\xaa':
        raise LayoutError('no MBR partition table')
    entries = {}
    for slot in range(1, 5):
        e = sector0[446 + 16 * (slot - 1): 446 + 16 * slot]
        boot, ptype = e[0], e[4]
        start, count = struct.unpack_from('<II', e, 8)
        if ptype:
            entries[slot] = (boot, ptype, start, count)
    return {'disk_id': struct.unpack_from('<I', sector0, 440)[0], 'entries': entries}


def mbr_set(sector0, slot, ptype, start_lba, sectors):
    """sector0 with entry ``slot`` (1-4) set (ptype 0: cleared)."""
    if not 1 <= slot <= 4:
        raise LayoutError('MBR slot out of range')
    buf = bytearray(sector0)
    off = 446 + 16 * (slot - 1)
    if not ptype:
        buf[off:off + 16] = bytes(16)
        return bytes(buf)
    if start_lba >= 1 << 32 or sectors >= 1 << 32 or start_lba + sectors > 1 << 32:
        raise LayoutError('the card is too large for an MBR partition table')
    buf[off:off + 16] = (b'\x00' + _chs(start_lba) + bytes((ptype,)) + _chs(start_lba + sectors - 1) +
                         struct.pack('<II', start_lba, sectors))
    return bytes(buf)


def mbr_partuuid(disk_id, number):
    return f'{disk_id:08x}-{number:02x}'


# --- config.txt, cmdline.txt, fstab ------------------------------------------------

def is_installed_config(text):
    return text.lstrip('﻿').startswith(CONFIG_MARK)


def installed_config(base):
    """The installed system's config.txt: the image's own (``base``) with its operating
    system files under OS_PREFIX, marked as ours. The installer's stays in tryboot.txt."""
    lines = base.splitlines()
    out = [CONFIG_MARK,
           '# Recovery’s Start Installer… restarts with `reboot "0 tryboot"`: the firmware then reads',
           '# tryboot.txt (the installer’s own configuration, kept from the card as written) once.',
           '# Kernel, initramfs, cmdline.txt, device trees and overlays of RetroBoot are under',
           f'# {OS_PREFIX} (os_prefix); the installer’s files at the top of this partition stay as they are.',
           '']
    placed = False
    for line in lines:
        if line.strip().lower().startswith('os_prefix='):
            continue
        out.append(line)
        if not placed and line.strip().lower() == '[all]':
            out.append(f'os_prefix={OS_PREFIX}')
            placed = True
    if not placed:
        out.insert(6, f'os_prefix={OS_PREFIX}')
    return '\n'.join(out).rstrip('\n') + '\n'


def cmdline_for(template, root_partuuid):
    """cmdline.txt from the image's cmdline.txt.in (one line)."""
    text = ' '.join(template.split())
    if '@ROOT_PARTUUID@' not in text:
        raise LayoutError('cmdline.txt.in has no @ROOT_PARTUUID@')
    return text.replace('@ROOT_PARTUUID@', root_partuuid) + '\n'


def fstab_by_partuuid(text, root, data, boot):
    """The root's fstab with /, /var/lib/retroboot and /boot/firmware named by PARTUUID
    (an MBR card has no partition names; GPT names would be ambiguous beside another
    RetroBoot disk). Other lines are kept."""
    want = {'/': root, '/var/lib/retroboot': data, '/boot/firmware': boot}
    out, seen = [], set()
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 2 and not f[0].startswith('#') and f[1] in want:
            seen.add(f[1])
            line = re.sub(r'^\s*\S+', f'PARTUUID={want[f[1]]}', line, count=1)
        out.append(line)
    missing = set(want) - seen
    if missing:
        raise LayoutError(f'fstab has no line for {", ".join(sorted(missing))}')
    return '\n'.join(out) + '\n'


def slot_cmdline(cmdline):
    """slot-a.cfg's arguments (make-disk.sh): the cmdline without root/slot/trial/init words."""
    return ' '.join(w for w in cmdline.split() if not re.match(r'^(root|retroboot\.slot|retroboot\.trial|init)=', w))


# --- boot files from the installed root ---------------------------------------------

def kernel_files(root):
    """The Raspberry Pi kernel in a root: {version, kernel, initrd, dtbs, overlays}."""
    mods = os.path.join(root, 'usr/lib/modules')
    if not os.path.isdir(mods):
        mods = os.path.join(root, 'lib/modules')
    try:
        versions = sorted((v for v in os.listdir(mods) if '-rpi-' in v), reverse=True)
    except OSError:
        versions = []
    for kver in versions:
        kernel = os.path.join(root, 'boot', f'vmlinuz-{kver}')
        initrd = os.path.join(root, 'boot', f'initrd.img-{kver}')
        dtbdir = os.path.join(mods, kver, 'dtb')
        if not os.path.isdir(os.path.join(dtbdir, 'broadcom')):
            dtbdir = os.path.join(root, 'usr/lib', f'linux-image-{kver}')
        bcm = os.path.join(dtbdir, 'broadcom')
        ovl = os.path.join(dtbdir, 'overlays')
        if not (os.path.isfile(kernel) and os.path.isfile(initrd) and os.path.isdir(bcm) and os.path.isdir(ovl)):
            continue
        dtbs = sorted(os.path.join(bcm, f) for f in os.listdir(bcm)
                      if f.endswith('.dtb') and (f.startswith('bcm2711-') or f.startswith('bcm2712')))
        overlays = sorted(os.path.join(ovl, f) for f in os.listdir(ovl)
                          if f.endswith('.dtbo') or f.endswith('.dtb') or f == 'README')
        if dtbs and overlays:
            return {'version': kver, 'kernel': kernel, 'initrd': initrd, 'dtbs': dtbs, 'overlays': overlays}
    raise LayoutError('no Raspberry Pi kernel with device trees in the system to install')


def os_files(kf):
    """[(source, name relative to the os directory)] for kernel8.img, initramfs8, DTBs, overlays."""
    files = [(kf['kernel'], 'kernel8.img'), (kf['initrd'], 'initramfs8')]
    files += [(p, os.path.basename(p)) for p in kf['dtbs']]
    files += [(p, 'overlays/' + os.path.basename(p)) for p in kf['overlays']]
    return files


def firmware_files(root):
    fw = os.path.join(root, 'usr/lib/raspi-firmware')
    try:
        names = sorted(f for f in os.listdir(fw) if f.startswith(FIRMWARE_GLOBS) and f.endswith(('.elf', '.dat')))
    except OSError:
        names = []
    if 'start4.elf' not in names or 'fixup4.dat' not in names:
        raise LayoutError('the Raspberry Pi 4 firmware (start4.elf, fixup4.dat) is missing from the system')
    out = [(os.path.join(fw, n), n) for n in names]
    lic = os.path.join(fw, 'LICENCE.broadcom')
    if os.path.isfile(lic):
        out.append((lic, 'LICENCE.broadcom'))
    return out


def files_bytes(files):
    return sum(os.path.getsize(src) for src, _ in files)


def tree_bytes(path):
    total = 0
    for base, _, names in os.walk(path):
        for n in names:
            try:
                total += os.path.getsize(os.path.join(base, n))
            except OSError:
                pass
    return total


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_file(path, data):
    """Write (temp file, fsync, rename): FAT keeps either the old or the new file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.new'
    with open(tmp, 'wb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fsync_dir(os.path.dirname(path))


def copy_files(files, dest):
    """Copy (source, relative name) pairs below dest (temp name, then rename)."""
    for src, rel in files:
        out = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        tmp = out + '.new'
        shutil.copyfile(src, tmp)
        with open(tmp, 'rb+') as f:
            os.fsync(f.fileno())
        os.replace(tmp, out)
    for d in sorted({os.path.dirname(os.path.join(dest, rel)) for _, rel in files}):
        fsync_dir(d)


# --- the kernel's view of a disk -----------------------------------------------------

def sysfs_partitions(disk, sysfs='/sys/class/block'):
    """{number: {name, start, size}} (bytes) of the kernel's partitions of disk."""
    base = os.path.basename(disk)
    out = {}
    try:
        names = os.listdir(os.path.join(sysfs, base))
    except OSError:
        return out
    for name in names:
        pfile = os.path.join(sysfs, base, name, 'partition')
        if not (name.startswith(base) and os.path.exists(pfile)):
            continue
        try:
            with open(pfile) as f:
                num = int(f.read())
            with open(os.path.join(sysfs, base, name, 'start')) as f:
                start = int(f.read()) * SECTOR
            with open(os.path.join(sysfs, base, name, 'size')) as f:
                size = int(f.read()) * SECTOR
        except (OSError, ValueError):
            continue
        out[num] = {'name': name, 'start': start, 'size': size}
    return out


def disk_bytes(disk, sysfs='/sys/class/block'):
    with open(os.path.join(sysfs, os.path.basename(disk), 'size')) as f:
        return int(f.read()) * SECTOR


def describe(disk, table, medium_dev, lsblk_children, sysfs='/sys/class/block'):
    """plan_free_space for a list-disks disk: geometry from sysfs, file systems from lsblk."""
    fsinfo = {os.path.basename(c.get('kname') or c['name']): c for c in lsblk_children or []}
    parts = []
    for num, p in sorted(sysfs_partitions(disk, sysfs).items()):
        c = fsinfo.get(p['name'], {})
        parts.append({'name': p['name'], 'number': num, 'start': p['start'], 'size': p['size'],
                      'fstype': c.get('fstype') or None, 'label': c.get('label') or None})
    medium = os.path.basename(os.path.realpath(medium_dev)) if medium_dev else ''
    plan = plan_free_space(table, disk_bytes(disk, sysfs), parts, medium)
    plan['message'] = REASONS.get(plan['reason'])
    return plan


def run(argv, log=None):
    if log:
        log('$ ' + ' '.join(argv))
    r = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if r.returncode != 0:
        raise LayoutError(f"{os.path.basename(argv[0])} failed ({r.returncode}): {r.stderr.strip()[-300:]}")
    return r.stdout


def gpt_partuuid(disk, number, log=None):
    out = run(['sgdisk', '-i', str(number), disk], log)
    m = re.search(r'^Partition unique GUID: ([0-9A-Fa-f-]{36})', out, re.M)
    if not m:
        raise LayoutError(f'partition {number} has no unique GUID')
    return m.group(1).lower()


def read_sector0(disk):
    with open(disk, 'rb', buffering=0) as f:
        return f.read(SECTOR)


def write_sector0(disk, data):
    if len(data) != SECTOR:
        raise LayoutError('MBR sector must be 512 bytes')
    fd = os.open(disk, os.O_WRONLY)
    try:
        if os.pwrite(fd, data, 0) != SECTOR:
            raise LayoutError('short write to the partition table')
        os.fsync(fd)
    finally:
        os.close(fd)


def json_dumps(o):
    return json.dumps(o, sort_keys=True)


# --- display presets --------------------------------------------------------------

def display_files(root):
    """Fixed display assets carried by a Pi root; no arbitrary source traversal.

    Older roots without display includes remain installable. A new root whose
    config names the include must carry the include, manifest and recovery preset.
    """
    base = os.path.join(root, 'usr/lib/retroboot/boot')
    with open(os.path.join(base, 'config.txt')) as f:
        managed = 'include retroboot/display.txt' in f.read().splitlines()
    if not managed:
        return []
    fixed = ['DISPLAY-SETUP.txt', 'retroboot/display.txt', 'retroboot/display-profiles.json']
    profiles = os.path.join(base, 'retroboot/displays')
    if os.path.islink(profiles):
        raise LayoutError('the display presets must be regular files')
    try:
        names = sorted(os.listdir(profiles))
    except OSError:
        raise LayoutError('the display recovery presets are missing') from None
    if 'auto.txt' not in names or 'hdmi.txt' not in names:
        raise LayoutError('the display recovery presets are missing')
    for name in names:
        if not re.fullmatch(r'[a-z0-9-]+\.txt', name):
            raise LayoutError('the display preset filename is invalid')
        fixed.append('retroboot/displays/' + name)
    for rel in fixed:
        path = os.path.join(base, rel)
        if os.path.islink(path) or not os.path.isfile(path) or os.path.getsize(path) > 16384:
            raise LayoutError('the display boot payload is incomplete: ' + rel)
    return [(os.path.join(base, rel), rel) for rel in fixed]


def copy_display_files(root, boot, keep_selection=False):
    """Install at the FAT root, preserving a free-space installer's chosen display."""
    files = display_files(root)
    if keep_selection:
        chosen = os.path.join(boot, 'retroboot/display.txt')
        if os.path.islink(chosen):
            raise LayoutError('the display setup file must be a regular file')
        if os.path.isfile(chosen):
            files = [(src, rel) for src, rel in files if rel != 'retroboot/display.txt']
    copy_files(files, boot)
