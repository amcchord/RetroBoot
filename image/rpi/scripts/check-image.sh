#!/bin/bash
# Build-level checks of a Raspberry Pi image (no boot involved).
#
#   check-image.sh IMAGE.img ROOTDIR
#
# Partition table and names; the boot partition's firmware, DTBs, overlays, kernel,
# initramfs, config.txt, and cmdline.txt naming RBROOT_A's PARTUUID; what the
# initramfs carries; every ELF RetroBoot installs is aarch64. Prints a report; exits 1
# on the first failed check.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
. "$here/rpi-lib.sh"
require_root
img=${1:?image}; root=${2:?rootdir}; factory_macs=${3:-0}
fail() { echo "FAIL: $*"; exit 1; }
ok() { echo "ok: $*"; }

model=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["model"])' "$root/usr/lib/retroboot/rpi-profile.json")
root_num=3; data_num=5
if [ "$model" = zero2w ]; then
  python3 - "$img" <<'PY' || fail "Zero 2 W MBR partition table"
import struct, sys
with open(sys.argv[1], 'rb') as f:
    m = f.read(512)
assert m[510:512] == b'\x55\xaa'
assert struct.unpack_from('<I', m, 440)[0]
parts = [struct.unpack_from('<B3sB3sII', m, 446 + 16 * i) for i in range(4)]
assert [p[2] for p in parts] == [0x0c, 0x83, 0x83, 0]
assert parts[0][0] == 0x80
assert parts[0][4] == 2048
for i in range(3):
    assert parts[i][4] % 2048 == 0 and parts[i][5] > 0
    if i:
        assert parts[i][4] == parts[i-1][4] + parts[i-1][5]
PY
  root_num=2; data_num=3
  ok "MBR: FAT32 boot, ext4 root, XFS data (no unused root slot)"
else
names=$(sgdisk -p "$img" | awk '/^ +[0-9]/{print $1":"$6":"$7}' | paste -sd' ' -)
[ "$names" = "1:0700:RBEFI 2:EF02:BIOSBOOT 3:8300:RBROOT_A 4:8300:RBROOT_B 5:8300:RBDATA" ] || fail "partitions: $names"
ok "GPT $names"
sgdisk -v "$img" | grep -q 'No problems found' || fail "sgdisk -v"
ok "GPT verifies"
fi

loop=$(losetup -f --show -r -P "$img")
mnt=$(mktemp -d)
cleanup() { umount "$mnt" 2>/dev/null || true; losetup -d "$loop" 2>/dev/null || true; rmdir "$mnt" 2>/dev/null || true; }
trap cleanup EXIT
for _ in $(seq 50); do [ -b "${loop}p$data_num" ] && break; sleep 0.1; done
# A loop device can be reused before udev has replaced its previous partition
# records. Wait for that update before asking blkid for the new PARTUUID.
udevadm settle --timeout=20 2>/dev/null || true
partuuid=$(blkid -s PARTUUID -o value "${loop}p$root_num" || true)
[ -n "$partuuid" ] || fail "partition $root_num has no PARTUUID after loop discovery"
[ "$(blkid -o value -s TYPE "${loop}p1")" = vfat ] || fail "partition 1 is not FAT"
[ "$(blkid -o value -s SEC_TYPE "${loop}p1" 2>/dev/null)" != msdos ] || fail "partition 1 is FAT12/16"
[ "$(blkid -o value -s TYPE "${loop}p$root_num")" = ext4 ] && [ "$(blkid -o value -s TYPE "${loop}p$data_num")" = xfs ] || fail "file systems"
ok "file systems: p1 FAT32, p$root_num ext4, p$data_num XFS"
mount -o ro "${loop}p1" "$mnt"
for f in config.txt cmdline.txt start4.elf fixup4.dat kernel8.img initramfs8 \
         bcm2711-rpi-4-b.dtb bcm2711-rpi-400.dtb bcm2711-rpi-cm4.dtb bcm2712-rpi-5-b.dtb bcm2712d0-rpi-5-b.dtb \
         bcm2712-rpi-500.dtb overlays/overlay_map.dtb overlays/vc4-kms-v3d.dtbo overlays/vc4-kms-v3d-pi4.dtbo \
         overlays/vc4-kms-v3d-pi5.dtbo retroboot/slots.env; do
  [ -s "$mnt/$f" ] || fail "boot partition lacks $f"
done
ok "boot partition: $(ls "$mnt" | wc -l) entries, $(ls "$mnt/overlays" | wc -l) overlays, $(du -sh "$mnt" | cut -f1)"
if [ "$model" = zero2w ]; then
  for f in bootcode.bin start.elf fixup.dat start_cd.elf fixup_cd.dat bcm2710-rpi-zero-2-w.dtb; do
    [ -s "$mnt/$f" ] || fail "Zero 2 W boot partition lacks $f"
  done
  ok "Zero 2 W: BCM2710 DTB, bootcode.bin and full/cut-down start/fixup firmware"
fi
[ "$(wc -l < "$mnt/cmdline.txt")" -le 1 ] || fail "cmdline.txt has more than one line"
grep -q "root=PARTUUID=$partuuid " "$mnt/cmdline.txt" || fail "cmdline.txt does not name RBROOT_A ($partuuid)"
for word in console=tty0 fbcon=map:9 drm_kms_helper.fbdev_emulation=0 quiet loglevel=0; do
  grep -qE "(^| )$word( |$)" "$mnt/cmdline.txt" || fail "quiet console fallback lacks $word"
done
# The Pi DTBs' /chosen/bootargs carry cgroup_disable=memory (the firmware puts them before
# cmdline.txt); without this the memory controller, and every MemoryMax=, is off.
grep -qE '(^| )cgroup_enable=memory( |$)' "$mnt/cmdline.txt" || fail "cmdline.txt lacks cgroup_enable=memory"
consoles=$(tr ' ' '\n' < "$mnt/cmdline.txt" | grep '^console=' | paste -sd' ' -)
[ "$consoles" = "console=serial0,115200 console=ttyAMA10,115200 console=tty0" ] || fail "cmdline.txt consoles: $consoles"
ok "cmdline.txt: $(cat "$mnt/cmdline.txt")"
for k in 'kernel=kernel8.img' 'initramfs initramfs8 followkernel' 'disable_splash=1' 'dtoverlay=vc4-kms-v3d' 'arm_64bit=1'; do
  grep -qx "$k" "$mnt/config.txt" || fail "config.txt lacks '$k'"
done
# the base device tree's audio parameter (Pi 4 headphone jack) only works before any dtoverlay=
awk '/^dtoverlay=/{o=1} /^dtparam=audio=on$/{if(o) exit 1; a=1} END{exit !a}' "$mnt/config.txt" ||
  fail "config.txt: dtparam=audio=on must come before the first dtoverlay="
ok "config.txt: kernel8.img + initramfs8, disable_splash, dtparam=audio=on before dtoverlay=vc4-kms-v3d"
file -b "$mnt/kernel8.img" | grep -qiE 'arm64|aarch64|gzip' || fail "kernel8.img: $(file -b "$mnt/kernel8.img")"
ok "kernel8.img: $(file -b "$mnt/kernel8.img" | cut -c1-80)"
cmp -s "$mnt/initramfs8" "$root/boot/initrd.img-$(basename "$(ls -d "$root"/lib/modules/* | sort -V | tail -n1)")" || fail "initramfs8 differs from the root's initrd"
umount "$mnt"

kver=$(basename "$(ls -d "$root"/lib/modules/* | sort -V | tail -n1)")
list=$(chroot "$root" lsinitramfs "/boot/initrd.img-$kver")
for want in '/vc4\.ko' '/v3d\.ko' '/xfs\.ko' 'scripts/init-top/retroboot-preload' 'scripts/local-bottom/retroboot-data' 'bin/busybox' \
            'sbin/fsck$' 'sbin/fsck\.ext4$' 'sbin/logsave$'; do
  echo "$list" | grep -qE "$want" || fail "initramfs lacks $want"
done
splash=no; echo "$list" | grep -q 'usr/lib/retroboot/bin/rb-splash' && splash=yes
ok "initramfs8: $(stat -c %s "$root/boot/initrd.img-$kver") bytes, $(echo "$list" | grep -c '\.ko') modules (vc4, v3d, xfs), fsck.ext4 + logsave, rb-splash: $splash"

# Host-side ELFs only (scripts/arm64-lib.sh).
arm64_check_elves "$root" || exit 1
if [ "$model" = zero2w ]; then
  awk -v spec="PARTUUID=$partuuid" '$1 == spec && $2 == "/" { found=1 } END { exit !found }' "$root/etc/fstab" || fail "fstab: MBR root PARTUUID"
  grep -q '^PARTLABEL=' "$root/etc/fstab" && fail "fstab: MBR image uses GPT partition names"
else
  grep -q '^PARTLABEL=RBEFI     /boot/firmware      vfat ' "$root/etc/fstab" || fail "fstab: GPT boot partition"
fi
[ "$(readlink "$root/boot/efi")" = firmware ] ||
  fail "boot partition mount (/boot/firmware, /boot/efi link)"
ok "fstab: root /, data /var/lib/retroboot, FAT /boot/firmware (/boot/efi -> firmware)"
for b in usr/lib/retroboot/qemu-m68k/bin/qemu-system-m68k usr/lib/retroboot/bin/rb-presenter; do
  [ -x "$root/$b" ] || fail "$b missing"
done
if [ "$model" = zero2w ]; then
  [ ! -e "$root/usr/lib/retroboot/qemu/bin/qemu-system-ppc" ] || fail "Zero 2 W image has a PPC emulator"
else
  [ -x "$root/usr/lib/retroboot/qemu/bin/qemu-system-ppc" ] || fail "PPC emulator missing"
fi
[ -x "$root/usr/lib/retroboot/bin/rb-splash" ] && ok "rb-presenter, rb-splash, required emulator packages present"
if [ "$factory_macs" = 1 ]; then
  mount -o ro,norecovery "${loop}p$data_num" "$mnt"
  PYTHONPATH="$root/usr/lib/retroboot/hostd" python3 "$here/starter_macs.py" "$mnt"
  [ ! -e "$mnt/assets/platinum" ] || fail "public factory image includes a generated Platinum bundle"
  umount "$mnt"
fi
