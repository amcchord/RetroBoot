#!/bin/bash
# Universal installer medium: one image for a Raspberry Pi 4/5 (SD card or USB drive) and a
# PC (USB stick, UEFI and legacy BIOS), written as is by balenaEtcher, Raspberry Pi Imager
# ("Use custom") or dd. ARCHITECTURE 8.1; layout and the rules behind it: universal.py.
#
#   make-universal.sh X86_ROOT X86_BOOT ARM64_ROOT ARM64_BOOT OUT.img BUILD_ID [MACHINE_DIR MACHINE_ID ...]
#
# X86_ROOT/X86_BOOT: the x86 build's root and boot payload (build.sh stages); ARM64_ROOT/
# ARM64_BOOT: the Raspberry Pi live root and boot tree (image/rpi/build.sh --root-only:
# ARM64_BOOT/{config.txt,cmdline.txt.in,bootfs/}). Both roots are the installed systems'
# roots, unchanged; each processor boots its own (retroboot-live picks
# /live/<uname -m>/rootfs.squashfs). The Macs (MACHINE_DIR MACHINE_ID pairs, private) go
# into one live/embedded.squashfs that both use: Mac disks don't depend on the host.
#
# Partition 1 (FAT32 RBINSTEFI, 0x0c, active, 1 MiB, universal_fat_mib): the Pi boot tree
# with the live cmdline.txt and an arm64 live initramfs8 (the root's initramfs
# configuration plus squashfs, overlay and isofs; the installer startup box appended when
# there is one), EFI/BOOT/BOOTX64.EFI (RetroBoot's quiet GRUB, the ISO's configuration),
# retroboot/. Partition 2 (ISO 9660 RETROBOOT, 0x83): the marker file, live/, boot/grub/.
# GRUB core.img for legacy BIOS lies between the MBR and partition 1.
#
# Private, off by default, as make-iso.sh: RB_ISO_NETWORK_CONF=FILE, RB_ISO_LAB_AUTH=FILE /
# RB_ISO_LAB_URL=URL (retroboot/ on the payload), RB_STARTUP_WORDMARK_KIT=DIR.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
. "$here/lib.sh"
require_root

x86=${1:?x86 root}; x86boot=${2:?x86 boot dir}; arm=${3:?arm64 root}; armboot=${4:?arm64 boot dir}
out=${5:?out.img}; build_id=${6:?build id}
shift 6
machines=("$@")
[ $((${#machines[@]} % 2)) = 0 ] || die "make-universal.sh: MACHINE_DIR MACHINE_ID pairs expected"
for f in "$x86/boot/vmlinuz" "$x86/boot/initrd.img" "$x86/boot/microcode.cpio" "$armboot/bootfs/start4.elf" \
         "$armboot/bootfs/kernel8.img" "$armboot/cmdline.txt.in" "$armboot/config.txt"; do
  [ -e "$f" ] || die "make-universal.sh: $f missing"
done
uni=(python3 "$here/universal.py")
grub=$("$here/build-grub.sh" | tail -n1)
cmdline=$(tr -s ' \n' ' ' < "$IMAGE_SRC/config/cmdline" | sed 's/ $//')
marker=.retroboot-installer-$(echo "$build_id" | tr -c 'A-Za-z0-9.-' '-' | sed 's/-*$//')
fat_mib=$(cfg universal_fat_mib); fat_mib=${fat_mib:-512}
work=$(mktemp -d "$RB_WORK_ROOT/work/universal.XXXXXX")
mounted=(); loop=
cleanup() {
  for ((i=${#mounted[@]}-1; i>=0; i--)); do umount -l "${mounted[i]}" 2>/dev/null || true; done
  [ -n "$loop" ] && losetup -d "$loop" 2>/dev/null || true
  rm -rf "$work" "$arm/tmp/rb-live-initramfs"
}
trap cleanup EXIT
tree=$work/payload; fat=$work/fat
mkdir -p "$tree/live/x86_64" "$tree/live/aarch64" "$tree/boot/grub/fonts" "$tree/boot/grub/retroboot" \
         "$fat/EFI/BOOT" "$fat/retroboot"
touch "$tree/$marker"

# ---- payload (partition 2) ------------------------------------------------
log "squashfs: x86_64 root"
mksquashfs "$x86" "$tree/live/x86_64/rootfs.squashfs" -comp zstd -Xcompression-level 15 -b 1M -noappend -xattrs -quiet -no-progress
cp -L "$x86/boot/vmlinuz" "$tree/live/x86_64/vmlinuz"
cp -L "$x86/boot/initrd.img" "$tree/live/x86_64/initrd.img"
cp "$x86/boot/microcode.cpio" "$tree/live/x86_64/microcode.cpio"

if [ ${#machines[@]} -gt 0 ]; then
  log "squashfs: $((${#machines[@]} / 2)) embedded guest machine(s) (private, shared by both processors)"
  stage=$work/embedded
  "$here/stage-embedded.sh" "$stage" "$x86/usr/lib/retroboot/hostd" "${machines[@]}"
  startup_style=$(PYTHONPATH="$x86/usr/lib/retroboot/hostd" python3 -c \
    'import sys; from pathlib import Path; from retroboot.startup import embedded_style; print(embedded_style(Path(sys.argv[1])))' \
    "$stage/usr/lib/retroboot/installer/embedded")
  first_style=$(PYTHONPATH="$x86/usr/lib/retroboot/hostd" python3 -c \
    'import sys; from pathlib import Path; from retroboot.startup import machine_directory_style; print(machine_directory_style(Path(sys.argv[1])))' \
    "$stage/usr/lib/retroboot/installer/embedded/machines/${machines[1]}")
  mksquashfs "$stage" "$tree/live/embedded.squashfs" -comp zstd -Xcompression-level 15 -b 1M -noappend -quiet -no-progress
  rm -rf "$stage"
fi

# The installer's startup box (ARCHITECTURE 4.4.2), as make-iso.sh: Apple-derived (D012),
# only with an embedded Mac, from the first one's disk. x86 GRUB loads startup.cpio as an
# extra initrd; the Pi firmware loads one initramfs, so initramfs8 carries it appended.
startup_initrd=
machine_dir=${machines[0]:-}
startup_style=${startup_style:-classic}
first_style=${first_style:-classic}
if [ "$first_style" != macosx ] && [ -n "$machine_dir" ] && [ -d "$x86/usr/lib/retroboot/platinum/rbplatinum" ]; then
  art=$work/isplash
  mkdir -p "$art/retroboot"
  if PYTHONPATH="$x86/usr/lib/retroboot/platinum" RB_GUESTPREP_PATH="$x86/usr/lib/retroboot/guestprep" \
      python3 -m rbplatinum startup --source "$machine_dir/disk.img" --out "$art/retroboot/installer-startup.rbis" \
      ${RB_STARTUP_WORDMARK_KIT:+--wordmark-kit "$RB_STARTUP_WORDMARK_KIT"} >&2; then
    cp "$art/retroboot/installer-startup.rbis" "$tree/live/installer-startup.rbis"
    (cd "$art" && find retroboot | LC_ALL=C sort | cpio --quiet -o -H newc -R 0:0 --reproducible) > "$tree/live/startup.cpio"
    cp "$tree/live/startup.cpio" "$work/startup.cpio"
    startup_initrd=" /live/startup.cpio"
    log "installer startup box: Mac OS's own (private)${RB_STARTUP_WORDMARK_KIT:+, RetroBoot wordmark from the kit}"
  else
    log "installer startup box: RetroBoot's own art (the guest disk has none)"
  fi
  rm -rf "$art"
fi
if [ "$startup_style" = macosx ]; then
  cmdline="$cmdline retroboot.startup_style=macosx retroboot.progress=off"
fi
if [ -n "${RB_ISO_NETWORK_CONF:-}" ]; then
  [ -f "$RB_ISO_NETWORK_CONF" ] || die "RB_ISO_NETWORK_CONF is not a file"
  install -d -m 0700 "$tree/retroboot"
  install -m 0600 "$RB_ISO_NETWORK_CONF" "$tree/retroboot/network.conf"
  log "network.conf: included (private image)"
fi
if [ -n "${RB_ISO_LAB_AUTH:-}" ] || [ -n "${RB_ISO_LAB_URL:-}" ]; then
  install -d -m 0700 "$tree/retroboot"
  "$here/write-lab-auth.sh" "$tree/retroboot/lab-auth.env"
fi

# GRUB: the ISO's configuration with the x86 kernel in live/x86_64
sed -e "1s|.*|# RetroBoot universal installer GRUB configuration (generated by image/scripts/make-universal.sh).|" \
  -e "s|@CMDLINE@|$cmdline|" -e "s|@MARKER@|$marker|" -e "s|@STARTUP_INITRD@|$startup_initrd|" \
  -e "s|@LIVE@|/live/x86_64|g" -e "s|@MEDIUM_ARGS@| retroboot.medium=universal|g" \
  -e "s|@STARTUP_STYLE@|$startup_style|" \
  "$IMAGE_SRC/grub/grub-iso.cfg.in" > "$tree/boot/grub/grub.cfg"
"$grub/pc/bin/grub-script-check" "$tree/boot/grub/grub.cfg" || die "universal grub.cfg does not parse"
cp "$x86boot/esp/grub/fonts/blank.pf2" "$tree/boot/grub/fonts/"
menu_bdf=$RB_WORK_ROOT/src/components/classic-ui/presenter/ui/assets/fonts/RetroBoot-System-12.bdf
if [ -f "$menu_bdf" ]; then
  python3 "$IMAGE_SRC/scripts/make-menu-font.py" "$menu_bdf" 2 "$tree/boot/grub/fonts/retroboot-menu.pf2"
else
  log "startup options font: $menu_bdf missing; Shift at power-on shows nothing"
fi
cp "$x86boot/esp/grub/retroboot/checker-default"*.png "$tree/boot/grub/retroboot/"

log "squashfs: aarch64 root"
mksquashfs "$arm" "$tree/live/aarch64/rootfs.squashfs" -comp zstd -Xcompression-level 15 -b 1M -noappend -xattrs -quiet -no-progress

log "ISO 9660 payload"
xorriso -as mkisofs -V RETROBOOT -iso-level 3 -r -o "$work/payload.iso" "$tree" 2>&1 |
  grep -v -E '^(xorriso|Drive|Media|libisofs|Added|Writing|ISO image|Written|[ 0-9.]+% done)' >&2 || true
[ -s "$work/payload.iso" ] || die "xorriso produced no payload"
rm -rf "$tree"

# ---- partition 1: Pi boot files, the PC's EFI loader --------------------
cp -a "$armboot/bootfs/." "$fat/"
"${uni[@]}" rpi-cmdline "$armboot/cmdline.txt.in" "$marker" | tr -d '\n' > "$fat/cmdline.txt"
if [ "$startup_style" = macosx ]; then
  printf ' retroboot.startup_style=macosx retroboot.progress=off' >> "$fat/cmdline.txt"
fi
cmp -s "$armboot/config.txt" "$fat/config.txt" || die "boot tree config.txt differs from the Pi build's"

# arm64 live initramfs: the root's own initramfs configuration (MODULES=list, splash,
# Startup Details, retroboot-live) plus what retroboot-live mounts with: squashfs and overlay
# (and isofs for the payload), modules in the Raspberry Pi kernel.
kver=$(basename "$(ls -d "$arm"/lib/modules/* | sort -V | tail -n1)")
log "arm64 live initramfs for $kver"
for d in proc sys dev; do mkdir -p "$arm/$d"; done
mount -t proc proc "$arm/proc"; mounted+=("$arm/proc")
mount -t sysfs sys "$arm/sys"; mounted+=("$arm/sys")
mount --bind /dev "$arm/dev"; mounted+=("$arm/dev")
conf=$arm/tmp/rb-live-initramfs/conf
rm -rf "$arm/tmp/rb-live-initramfs"; mkdir -p "$arm/tmp/rb-live-initramfs"
cp -a "$arm/etc/initramfs-tools" "$conf"
printf '%s\n' '# universal installer medium: retroboot-live (image/scripts/make-universal.sh)' squashfs overlay isofs >> "$conf/modules"
chroot "$arm" /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C.UTF-8 TMPDIR=/tmp/rb-live-initramfs \
  mkinitramfs -d /tmp/rb-live-initramfs/conf -o /tmp/rb-live-initramfs/initramfs8 "$kver" 2>"$work/mkinitramfs.log" ||
  { cat "$work/mkinitramfs.log" >&2; die "arm64 live initramfs failed"; }
cp "$arm/tmp/rb-live-initramfs/initramfs8" "$fat/initramfs8"
rm -rf "$arm/tmp/rb-live-initramfs"
for ((i=${#mounted[@]}-1; i>=0; i--)); do umount -l "${mounted[i]}"; done; mounted=()
if [ -n "$startup_initrd" ]; then
  # the kernel finds an uncompressed archive after a compressed one at a 4-byte boundary
  # (as GRUB pads each initrd); zero padding is skipped
  pad=$(( (4 - $(stat -c %s "$fat/initramfs8") % 4) % 4 ))
  head -c "$pad" /dev/zero >> "$fat/initramfs8"
  cat "$work/startup.cpio" >> "$fat/initramfs8"
fi

# BOOTX64.EFI, BOOTIA32.EFI and the BIOS core image: the ISO's module set, the embedded configuration
# that finds the payload by its marker (grub/embed-iso.cfg.in); MBR partitions on a disk
mods="part_gpt part_msdos fat iso9660 ext2 normal configfile linux search search_fs_file search_fs_uuid search_label regexp test echo loadenv gfxterm gfxterm_background png font video halt reboot keystatus"
sed "s|@MARKER@|$marker|" "$IMAGE_SRC/grub/embed-iso.cfg.in" > "$work/embed.cfg"
"$grub/pc/bin/grub-mkimage" -d "$grub/efi/lib/grub/x86_64-efi" -O x86_64-efi -p /boot/grub -c "$work/embed.cfg" \
  -o "$fat/EFI/BOOT/BOOTX64.EFI" $mods efi_gop efi_uga
"$grub/pc/bin/grub-mkimage" -d "$grub/efi32/lib/grub/i386-efi" -O i386-efi -p /boot/grub -c "$work/embed.cfg" \
  -o "$fat/EFI/BOOT/BOOTIA32.EFI" $mods efi_gop efi_uga
mkdir -p "$work/bios"
"$grub/pc/bin/grub-mkimage" -d "$grub/pc/lib/grub/i386-pc" -O i386-pc -p /boot/grub -c "$work/embed.cfg" \
  -o "$work/bios/core.img" $mods biosdisk vbe
cp "$grub/pc/lib/grub/i386-pc/boot.img" "$work/bios/boot.img"
core_size=$(stat -c %s "$work/bios/core.img")
[ "$core_size" -lt $((2047 * 512)) ] || die "core.img ($core_size bytes) does not fit before partition 1"

hexid() { echo -n "$1$build_id" | sha256sum | cut -c1-8; }
log "FAT32 RBINSTEFI, ${fat_mib} MiB"
truncate -s "${fat_mib}M" "$work/fat.img"
mkfs.vfat -F 32 -S 512 -h 2048 -n RBINSTEFI -i "$(hexid fat)" "$work/fat.img" >/dev/null
MTOOLS_SKIP_CHECK=1 mcopy -i "$work/fat.img" -s -Q -m "$fat"/* ::/
MTOOLS_SKIP_CHECK=1 mdir -i "$work/fat.img" ::/retroboot >/dev/null || die "FAT: retroboot/ missing"
fsck.vfat -n "$work/fat.img" >/dev/null || die "FAT check failed"

# ---- assemble: MBR, partitions, BIOS boot code ------------------------------
p1_start=2048; p1_sectors=$((fat_mib * 2048))
p2_start=$((p1_start + p1_sectors))
iso_bytes=$(stat -c %s "$work/payload.iso")
p2_sectors=$(( (iso_bytes + 1048575) / 1048576 * 2048 ))
total=$(( (p2_start + p2_sectors) * 512 ))
rm -f "$out"; truncate -s "$total" "$out"
"${uni[@]}" mbr "$out" "$(hexid disk)" "$p1_start:$p1_sectors" "$p2_start:$p2_sectors"
dd if="$work/fat.img" of="$out" bs=1M seek=1 conv=notrunc,sparse status=none
dd if="$work/payload.iso" of="$out" bs=1M seek=$((p2_start / 2048)) conv=notrunc,sparse status=none
loop=$(losetup -f --show "$out")
printf '(hd0) %s\n' "$loop" > "$work/device.map"
"$x86boot/i386-pc/grub-bios-setup" -d "$work/bios" -m "$work/device.map" -s "$loop" 2>&1 | grep -v '^$' >&2 || true
losetup -d "$loop"; loop=
dd if="$out" bs=1 count=3 status=none | od -An -tx1 | grep -q 'eb 63 90' || die "BIOS boot code not installed"
log "universal image: $(du -h --apparent-size "$out" | cut -f1) ($marker; payload $iso_bytes bytes)"
