#!/bin/bash
# Stage 4: assemble the GPT disk image (ARCHITECTURE section 8).
#
#   make-disk.sh ROOTDIR BOOTDIR OUT.img DATA_MIB [MACHINE_DIR MACHINE_ID ...]
#
# Each machine directory is copied sparsely to RBDATA/machines/<id>/; with more
# than one, RBDATA/config/install-seed.json makes the first one start first (the
# installer's seed, read by hostd at its first start).
#
# Layout: 1 RBEFI (ESP, FAT32, 256 MiB) · 2 BIOS boot (1 MiB) · 3 RBROOT_A (ext4,
# 3 GiB) · 4 RBROOT_B (ext4, 3 GiB, empty, reserved for updates) · 5 RBDATA (XFS,
# reflink, rest). The raw image is sparse; RBDATA grows to fill a larger disk on
# first boot (retroboot-growdata.service).
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
. "$here/lib.sh"
require_root

root=${1:?rootdir}; boot=${2:?bootdir}; out=${3:?out}; data_mib=${4:?data MiB}
[ ! -f "$root.devnodes.json" ] || die "the root's device nodes are stand-ins (a builder that may not create device nodes): build update bundles only (--no-img --no-iso)"
shift 4
machines=("$@")     # MACHINE_DIR MACHINE_ID pairs
[ $((${#machines[@]} % 2)) = 0 ] || die "make-disk.sh: MACHINE_DIR MACHINE_ID pairs expected"
esp=$(cfg esp_mib); bios=$(cfg biosboot_mib); rootsz=$(cfg root_mib)
total=$((1 + esp + bios + rootsz + rootsz + data_mib + 1))

rm -f "$out"; truncate -s "${total}M" "$out"
sgdisk -Z "$out" >/dev/null 2>&1 || true
sgdisk -a 2048 \
  -n 1:1M:+${esp}M      -t 1:EF00 -c 1:RBEFI \
  -n 2:0:+${bios}M      -t 2:EF02 -c 2:BIOSBOOT \
  -n 3:0:+${rootsz}M    -t 3:8300 -c 3:RBROOT_A \
  -n 4:0:+${rootsz}M    -t 4:8300 -c 4:RBROOT_B \
  -n 5:0:0              -t 5:8300 -c 5:RBDATA \
  "$out" >/dev/null

loop=$(losetup -f --show -P "$out")
mnt=$(mktemp -d)
esp_first=; seed_first=
cleanup() {
  if [ -n "$esp_first" ]; then umount "$esp_first" 2>/dev/null || true; rmdir "$esp_first" 2>/dev/null || true; fi
  [ -z "$seed_first" ] || rm -f "$seed_first"
  umount -R "$mnt" 2>/dev/null || true; losetup -d "$loop" 2>/dev/null || true; rmdir "$mnt" 2>/dev/null || true
}
trap cleanup EXIT
udevadm settle 2>/dev/null || true
for i in 1 2 3 4 5; do for _ in $(seq 50); do [ -b "${loop}p$i" ] && break; sleep 0.1; done; done

log "formatting partitions on $loop"
mkfs.vfat -F 32 -n RBEFI "${loop}p1" >/dev/null
mkfs.ext4 -q -L RBROOT_A -E lazy_itable_init=0,lazy_journal_init=0 "${loop}p3"
mkfs.ext4 -q -L RBROOT_B -E lazy_itable_init=0,lazy_journal_init=0 "${loop}p4"
mkfs.xfs -q -f -L RBDATA -m reflink=1 "${loop}p5"

log "populating RBROOT_A"
mount "${loop}p3" "$mnt"
rsync -aHAXS --numeric-ids "$root/" "$mnt/"
sync; umount "$mnt"

log "populating RBEFI"
mount "${loop}p1" "$mnt"
cp -r "$boot/esp/." "$mnt/"
# System slots (ARCHITECTURE 13): slot a active, its kernel arguments on the ESP.
cmdline=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["kernel_cmdline"])' "$boot/boot.json")
read -r rel_version rel_build < <(python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); print(r["version"], r["build_id"])' "$root/usr/lib/retroboot/release.json")
python3 -I "$root/usr/lib/retroboot/hostd/retroboot/update/slotenv.py" init-esp "$mnt" "$cmdline" "$rel_version" "$rel_build"
sync; umount "$mnt"

log "populating RBDATA"
mount "${loop}p5" "$mnt"
install -d -m 0755 "$mnt/logs" "$mnt/machines" "$mnt/config" "$mnt/share" "$mnt/checkpoints"
install -d -m 0700 "$mnt/state"
jgid=$(awk -F: '$1=="systemd-journal"{print $3}' "$root/etc/group")
install -d -m 2755 -g "${jgid:-0}" "$mnt/logs/journal"
first_id=
set -- "${machines[@]}"
while [ $# -gt 0 ]; do
  machine_dir=$1; machine_id=$2; shift 2
  [ -n "$first_id" ] || first_id=$machine_id
  log "embedding guest machine '$machine_id' (sparse copy, original untouched)"
  install -d -m 0755 "$mnt/machines/$machine_id"
  for f in "$machine_dir"/*; do
    cp --sparse=always --reflink=never "$f" "$mnt/machines/$machine_id/"
  done
  chmod u+w "$mnt/machines/$machine_id"/*
  python3 "$IMAGE_SRC/scripts/make-machine-json.py" "$mnt/machines/$machine_id" "$machine_id" "$root/usr/lib/retroboot/hostd"
  rbdata_gid=$(awk -F: '$1=="rbdata"{print $3}' "$root/etc/group")
  chgrp -R "${rbdata_gid:-0}" "$mnt/machines"; chmod 2770 "$mnt/machines" "$mnt/machines/$machine_id"
  chmod 0660 "$mnt/machines/$machine_id"/*
done
if [ ${#machines[@]} -gt 2 ]; then
  printf '{"settings": {"machine.active": "%s"}}\n' "$first_id" > "$mnt/config/install-seed.json"
fi
if [ -n "$first_id" ]; then
  machine_id=$first_id
  # The first raw-image boot has no learned display mode. Its OS profile still
  # owns the GRUB background and splash; never show classic art before OS X.
  seed_first=$(mktemp); esp_first=$(mktemp -d)
  printf '{"settings": {"machine.active": "%s"}}\n' "$first_id" > "$seed_first"
  mount "${loop}p1" "$esp_first"
  python3 "$IMAGE_SRC/rootfs/usr/lib/retroboot/image/rb-bootdisplay" --initial --esp "$esp_first" \
    --settings "$seed_first" --machines "$mnt/machines"
  startup_style=$(sed -n 's/^set rb_startup_style=//p' "$esp_first/grub/retroboot/display.cfg")
  sync; umount "$esp_first"; rmdir "$esp_first"; esp_first=
  rm -f "$seed_first"; seed_first=
  # Private images: build the Platinum asset bundle from the embedded guest's own disk
  # now, so the first boot already shows real Mac OS fonts and icons. Apple-derived:
  # it only ever exists in private images built with a guest fixture.
  if [ "$startup_style" != macosx ] && [ -d "$root/usr/lib/retroboot/platinum/rbplatinum" ] && [ -f "$mnt/machines/$machine_id/disk.img" ]; then
    log "building the Platinum asset bundle from the embedded guest disk"
    if PYTHONPATH="$root/usr/lib/retroboot/platinum" RB_GUESTPREP_PATH="$root/usr/lib/retroboot/guestprep" \
        python3 -m rbplatinum build --source "$mnt/machines/$machine_id/disk.img" \
        --out "$mnt/assets/platinum" >&2; then
      chmod -R u=rwX,go=rX "$mnt/assets"
    else
      log "Platinum bundle not built (hostd builds it on first launch instead)"
      rm -rf "$mnt/assets/platinum"
    fi
  fi
fi
du -sh "$mnt" | awk '{print "RBDATA used: " $1}' >&2
sync; umount "$mnt"

log "installing BIOS boot code"
printf '(hd0) %s\n' "$loop" > "$mnt.map"
"$boot/i386-pc/grub-bios-setup" -d "$boot/i386-pc" -m "$mnt.map" -s "$loop" 2>&1 | grep -v '^$' >&2 || true
rm -f "$mnt.map"
# Verify the MBR now carries GRUB's boot.img and the core image landed in the BIOS boot partition.
dd if="$loop" bs=1 count=4 skip=0 2>/dev/null | od -An -tx1 | grep -q 'eb 63 90' || die "BIOS boot code not installed"
sgdisk -v "$out" >/dev/null || die "GPT verification failed"
