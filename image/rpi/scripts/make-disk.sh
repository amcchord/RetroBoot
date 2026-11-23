#!/bin/bash
# Stage 4 (Raspberry Pi): assemble the GPT disk image for an SD card, USB SSD or NVMe.
#
#   make-disk.sh ROOTDIR BOOTDIR OUT.img DATA_MIB [MACHINE_DIR MACHINE_ID FACTORY_MACS]
#
# The x86 layout and PARTLABELs, so hostd, retroboot-growdata, the fstab and the
# slot code see the same disk (ARCHITECTURE 8):
#   1 RBEFI     FAT32, 256 MiB, GPT type "Microsoft basic data": the Pi boot partition
#               (BOOTDIR/bootfs + cmdline.txt; FAT volume label RETROBOOT)
#   2 BIOSBOOT  1 MiB placeholder, empty (keeps partitions 3-5 numbered as on x86)
#   3 RBROOT_A  ext4 3 GiB, the root (root=PARTUUID=<its unique GUID> in cmdline.txt)
#   4 RBROOT_B  ext4 3 GiB, empty (reserved for a future A/B "tryboot" update)
#   5 RBDATA    XFS reflink, rest; retroboot-growdata grows it to the end of the card
# The raw image is sparse. The Pi 4 bootloader reads GPT since EEPROM 2020-09-03; the
# Pi 5 bootloader always has.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
. "$here/rpi-lib.sh"
require_root

root=${1:?rootdir}; boot=${2:?bootdir}; out=${3:?out}; data_mib=${4:?data MiB}
machine_dir=${5:-}; machine_id=${6:-}; factory_macs=${7:-0}
esp=$(rcfg esp_mib); bios=$(rcfg biosboot_mib); rootsz=$(rcfg root_mib)
total=$((1 + esp + bios + rootsz + rootsz + data_mib + 1))
root_num=3; data_num=5; last_num=5
rm -f "$out"
if [ "${RB_RPI_MODEL:-pi4-5}" = zero2w ]; then
  # BCM2710's boot ROM needs an MBR FAT partition, rather than the Pi 4/5 GPT.
  # No unused root slot: Software Update is not available on the Pi.
  python3 "$here/sd_profile.py" mbr "$out" --boot-mib "$esp" --root-mib "$rootsz" --data-mib "$data_mib" >/dev/null
  root_num=2; data_num=3; last_num=3
else
truncate -s "${total}M" "$out"
sgdisk -Z "$out" >/dev/null 2>&1 || true
sgdisk -a 2048 \
  -n 1:1M:+${esp}M      -t 1:"$(rcfg rpi_boot_type)" -c 1:RBEFI \
  -n 2:0:+${bios}M      -t 2:EF02 -c 2:BIOSBOOT \
  -n 3:0:+${rootsz}M    -t 3:8300 -c 3:RBROOT_A \
  -n 4:0:+${rootsz}M    -t 4:8300 -c 4:RBROOT_B \
  -n 5:0:0              -t 5:8300 -c 5:RBDATA \
  "$out" >/dev/null
root_partuuid=$(sgdisk -i 3 "$out" | sed -n 's/^Partition unique GUID: \([0-9A-Fa-f-]*\).*/\1/p' | tr 'A-F' 'a-f')
[[ "$root_partuuid" =~ ^[0-9a-f-]{36}$ ]] || die "no PARTUUID for RBROOT_A"
fi

loop=$(losetup -f --show -P "$out")
mnt=$(mktemp -d)
cleanup() { umount -R "$mnt" 2>/dev/null || true; losetup -d "$loop" 2>/dev/null || true; rmdir "$mnt" 2>/dev/null || true; }
trap cleanup EXIT
udevadm settle 2>/dev/null || true
for i in $(seq "$last_num"); do for _ in $(seq 50); do [ -b "${loop}p$i" ] && break; sleep 0.1; done; done
if [ "${RB_RPI_MODEL:-pi4-5}" = zero2w ]; then
  root_partuuid=$(blkid -s PARTUUID -o value "${loop}p$root_num")
  python3 "$here/sd_profile.py" fstab "$root" \
    "$(blkid -s PARTUUID -o value "${loop}p1")" "$root_partuuid" "$(blkid -s PARTUUID -o value "${loop}p$data_num")"
fi

log "formatting partitions on $loop"
mkfs.vfat -F 32 -n RETROBOOT "${loop}p1" >/dev/null
mkfs.ext4 -q -L RBROOT_A -E lazy_itable_init=0,lazy_journal_init=0 "${loop}p$root_num"
if [ "$last_num" = 5 ]; then
  mkfs.ext4 -q -L RBROOT_B -E lazy_itable_init=0,lazy_journal_init=0 "${loop}p4"
fi
mkfs.xfs -q -f -L RBDATA -m reflink=1 "${loop}p$data_num"

log "populating RBROOT_A"
mount "${loop}p$root_num" "$mnt"
rsync -aHAXS --numeric-ids "$root/" "$mnt/"
sync; umount "$mnt"

log "populating the boot partition (root=PARTUUID=$root_partuuid)"
mount "${loop}p1" "$mnt"
cp -r "$boot/bootfs/." "$mnt/"
sed "s/@ROOT_PARTUUID@/$root_partuuid/" "$boot/cmdline.txt.in" > "$mnt/cmdline.txt"
# Same slot files as the x86 ESP (slot a active), so hostd's slot code sees the layout
# it knows; nothing on the Pi boots through them (updates are unavailable on the Pi).
# (slot-a.cfg holds the arguments a boot loader adds to its own root=/retroboot.slot=)
cmdline=$(tr ' ' '\n' < "$mnt/cmdline.txt" | grep -vE '^(root|retroboot\.slot|retroboot\.trial|init)=' | paste -sd' ' -)
read -r rel_version rel_build < <(python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); print(r["version"], r["build_id"])' "$root/usr/lib/retroboot/release.json")
python3 -I "$root/usr/lib/retroboot/hostd/retroboot/update/slotenv.py" init-esp "$mnt" "$cmdline" "$rel_version" "$rel_build"
sync; umount "$mnt"

log "populating RBDATA"
mount "${loop}p$data_num" "$mnt"
arm64_populate_data "$mnt" "$root" "$machine_dir" "$machine_id"
if [ "$factory_macs" = 1 ]; then
  [ "${RB_RPI_MODEL:-pi4-5}" = zero2w ] && [ -z "$machine_dir" ] || die "factory Macs require a Zero 2 W image"
  PYTHONPATH="$root/usr/lib/retroboot/hostd" python3 "$here/starter_macs.py" "$mnt" --cache "$CACHE/zero2w-macs"
  rbdata_gid=$(awk -F: '$1=="rbdata"{print $3}' "$root/etc/group")
  chgrp -R "${rbdata_gid:-0}" "$mnt/machines"
  chmod 2770 "$mnt/machines" "$mnt/machines/"*/
fi
if [ -n "${RB_RPI_LAB_AUTH:-}" ]; then
  # Same private sign-in and configuration authority the installer writes. The
  # source is validated, never sourced as shell, and never copied to the FAT tree.
  python3 - "$root" "$RB_RPI_LAB_AUTH" "$mnt" <<'PY'
import json, os, pathlib, sys
root, source, target = sys.argv[1:]
sys.path.insert(0, root + '/usr/lib/retroboot/hostd')
from retroboot import labauth
auth = labauth.load(source)
if auth is None:
    sys.exit('RB_RPI_LAB_AUTH is incomplete, unreadable or has unsafe permissions')
target = pathlib.Path(target) / 'config'
path = target / 'lab-auth.env'
path.write_text(auth.env_text())
path.chmod(0o600)
(target / 'hostd.local.json').write_text(json.dumps({
    'lab_http_auth_file': '/var/lib/retroboot/config/lab-auth.env'}) + '\n')
(target / 'hostd.local.json').chmod(0o600)
PY
fi
du -sh "$mnt" | awk '{print "RBDATA used: " $1}' >&2
sync; umount "$mnt"
if [ "$last_num" = 5 ]; then sgdisk -v "$out" >/dev/null || die "GPT verification failed"; fi
