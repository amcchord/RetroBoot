#!/bin/bash
# Stage 5: hybrid UEFI/BIOS live installer ISO.
#
#   make-iso.sh ROOTDIR BOOTDIR OUT.iso BUILD_ID [MACHINE_DIR MACHINE_ID ...]
#
# /live/rootfs.squashfs is the same root filesystem as the disk image; the
# initramfs' retroboot-live boot script mounts it under a tmpfs overlay and
# hostd runs with --installer (kernel command line retroboot.mode=installer).
# A private build adds /live/embedded.squashfs with one or more machines, overlaid
# as /usr/lib/retroboot/installer/embedded/machines/<id>/ (sparse disk.img + config
# + machine.json + disk.img.extents, the data map install-to-disk copies by) and
# machines/order (stage-embedded.sh); the installer offers them all, checked, the
# first one starting first. Source machine directories are only read.
#
# Private, off by default (each puts a file in the ISO's retroboot/ folder):
#   RB_ISO_NETWORK_CONF=FILE  network.conf: lab Wi-Fi for the installer (a Wi-Fi password)
#   RB_ISO_LAB_AUTH=FILE      lab-auth.env from FILE's RB_LAB_HTTP_USER, RB_LAB_HTTP_PASS and
#                             RB_LAB_BASE_URL lines only: the installer lists RetroBoot downloads
#                             without asking and keeps the sign-in on every computer it installs.
#                             THE ISO THEN CONTAINS THE DOWNLOAD PASSWORD: keep it private.
#   RB_ISO_LAB_URL=URL        lab-auth.env with only RB_LAB_BASE_URL=URL (no secret): the
#                             installer lists RetroBoot downloads after Sign In.
# A private build also carries the installer's startup box (ARCHITECTURE 4.4.2):
# rbplatinum builds it from the first embedded machine's own disk into
# /live/installer-startup.rbis and an extra initrd (/live/startup.cpio ->
# /retroboot/installer-startup.rbis) for the initramfs splash; RB_STARTUP_WORDMARK_KIT
# names the private wordmark kit (platinum/rbplatinum/startup.py) that sets RetroBoot's
# name in it.
# GRUB (the same quiet build and blank-font gfxterm checkerboard) finds the
# medium through a build-unique marker file. Layout follows grub-mkrescue:
# El Torito BIOS (eltorito.img + hybrid MBR) and an EFI system partition image.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
. "$here/lib.sh"
require_root

root=${1:?rootdir}; boot=${2:?bootdir}; out=${3:?out.iso}; build_id=${4:?build id}
[ ! -f "$root.devnodes.json" ] || die "the root's device nodes are stand-ins (a builder that may not create device nodes): build update bundles only (--no-img --no-iso)"
shift 4
machines=("$@")     # MACHINE_DIR MACHINE_ID pairs
[ $((${#machines[@]} % 2)) = 0 ] || die "make-iso.sh: MACHINE_DIR MACHINE_ID pairs expected"
grub=$("$here/build-grub.sh" | tail -n1)
cmdline=$(tr -s ' \n' ' ' < "$IMAGE_SRC/config/cmdline" | sed 's/ $//')
marker=.retroboot-installer-$(echo "$build_id" | tr -c 'A-Za-z0-9.-' '-' | sed 's/-*$//')
tree=$(mktemp -d "$RB_WORK_ROOT/work/iso-tree.XXXXXX")
stage=
startup_style=classic
first_style=classic
trap 'rm -rf "$tree" ${stage:+"$stage"}' EXIT
mkdir -p "$tree/live" "$tree/boot/grub/fonts" "$tree/boot/grub/retroboot" "$tree/boot/grub/i386-pc" "$tree/EFI/BOOT"
touch "$tree/$marker"

log "squashfs: root filesystem"
mksquashfs "$root" "$tree/live/rootfs.squashfs" -comp zstd -Xcompression-level 15 -b 1M -noappend -xattrs -quiet -no-progress
cp -L "$root/boot/vmlinuz" "$tree/live/vmlinuz"
cp -L "$root/boot/initrd.img" "$tree/live/initrd.img"
cp "$root/boot/microcode.cpio" "$tree/live/microcode.cpio"   # early microcode, its own initrd

if [ ${#machines[@]} -gt 0 ]; then
  log "squashfs: $((${#machines[@]} / 2)) embedded guest machine(s) (private)"
  stage=$(mktemp -d "$RB_WORK_ROOT/work/embedded.XXXXXX")
  "$here/stage-embedded.sh" "$stage" "$root/usr/lib/retroboot/hostd" "${machines[@]}"
  startup_style=$(PYTHONPATH="$root/usr/lib/retroboot/hostd" python3 -c \
    'import sys; from pathlib import Path; from retroboot.startup import embedded_style; print(embedded_style(Path(sys.argv[1])))' \
    "$stage/usr/lib/retroboot/installer/embedded")
  first_style=$(PYTHONPATH="$root/usr/lib/retroboot/hostd" python3 -c \
    'import sys; from pathlib import Path; from retroboot.startup import machine_directory_style; print(machine_directory_style(Path(sys.argv[1])))' \
    "$stage/usr/lib/retroboot/installer/embedded/machines/${machines[1]}")
  mksquashfs "$stage" "$tree/live/embedded.squashfs" -comp zstd -Xcompression-level 15 -b 1M -noappend -quiet -no-progress
  rm -rf "$stage"; stage=
fi

startup_initrd=
machine_dir=${machines[0]:-}   # the first embedded machine (the one that starts first)
if [ "$first_style" != macosx ] && [ -n "$machine_dir" ] && [ -d "$root/usr/lib/retroboot/platinum/rbplatinum" ]; then
  # Apple-derived (D012): only in private ISOs, never in a public build.
  art=$(mktemp -d "$RB_WORK_ROOT/work/isplash.XXXXXX")
  mkdir -p "$art/retroboot"
  if PYTHONPATH="$root/usr/lib/retroboot/platinum" RB_GUESTPREP_PATH="$root/usr/lib/retroboot/guestprep" \
      python3 -m rbplatinum startup --source "$machine_dir/disk.img" --out "$art/retroboot/installer-startup.rbis" \
      ${RB_STARTUP_WORDMARK_KIT:+--wordmark-kit "$RB_STARTUP_WORDMARK_KIT"} >&2; then
    cp "$art/retroboot/installer-startup.rbis" "$tree/live/installer-startup.rbis"
    (cd "$art" && find retroboot | LC_ALL=C sort | cpio --quiet -o -H newc -R 0:0 --reproducible) > "$tree/live/startup.cpio"
    startup_initrd=" /live/startup.cpio"
    log "installer startup box: Mac OS's own (private)${RB_STARTUP_WORDMARK_KIT:+, RetroBoot wordmark from the kit}"
  else
    log "installer startup box: RetroBoot's own art (the guest disk has none)"
  fi
  rm -rf "$art"
fi
if [ "$startup_style" = macosx ]; then
  cmdline="$cmdline retroboot.startup_style=macosx retroboot.progress=off"
  log "installer startup: OS X neutral gray; native guest startup remains unchanged"
fi

if [ -n "${RB_ISO_NETWORK_CONF:-}" ]; then
  # Lab builds only: installation-media network settings (retroboot/network.conf,
  # hostd/retroboot/netconf.py, docs/guide/installing.md). The file holds a Wi-Fi
  # password, so such an ISO is private and is deleted after the install.
  [ -f "$RB_ISO_NETWORK_CONF" ] || die "RB_ISO_NETWORK_CONF is not a file"
  install -d -m 0700 "$tree/retroboot"
  install -m 0600 "$RB_ISO_NETWORK_CONF" "$tree/retroboot/network.conf"
  log "network.conf: included (private ISO)"
fi
if [ -n "${RB_ISO_LAB_AUTH:-}" ] || [ -n "${RB_ISO_LAB_URL:-}" ]; then
  # Private builds only: where RetroBoot downloads (the lab catalog) are, and with
  # RB_ISO_LAB_AUTH the sign-in (hostd/retroboot/labauth.py, docs/guide/installing.md).
  install -d -m 0700 "$tree/retroboot"
  "$here/write-lab-auth.sh" "$tree/retroboot/lab-auth.env"
fi
# @LIVE@ (the x86 kernel's directory) and @MEDIUM_ARGS@ differ only on the universal
# medium (make-universal.sh: /live/x86_64, " retroboot.medium=universal").
sed -e "s|@CMDLINE@|$cmdline|" -e "s|@MARKER@|$marker|" -e "s|@STARTUP_INITRD@|$startup_initrd|" \
  -e "s|@STARTUP_STYLE@|$startup_style|" \
  -e "s|@LIVE@|/live|g" -e "s|@MEDIUM_ARGS@||g" \
  "$IMAGE_SRC/grub/grub-iso.cfg.in" > "$tree/boot/grub/grub.cfg"
sed "s|@MARKER@|$marker|" "$IMAGE_SRC/grub/embed-iso.cfg.in" > "$tree/embed.cfg"
cp "$boot/esp/grub/fonts/blank.pf2" "$tree/boot/grub/fonts/"
# The startup options' font (hold Shift at power-on): RetroBoot System at 2x, from
# classic-ui's MIT BDF in the staged presenter. Without it the options never show.
menu_bdf=$RB_WORK_ROOT/src/components/classic-ui/presenter/ui/assets/fonts/RetroBoot-System-12.bdf
if [ -f "$menu_bdf" ]; then
  python3 "$IMAGE_SRC/scripts/make-menu-font.py" "$menu_bdf" 2 "$tree/boot/grub/fonts/retroboot-menu.pf2"
else
  log "startup options font: $menu_bdf missing; Shift at power-on shows nothing"
fi
cp "$boot/esp/grub/retroboot/checker-default.png" "$tree/boot/grub/retroboot/"

mods="part_gpt part_msdos fat iso9660 ext2 normal configfile linux search search_fs_file search_fs_uuid search_label regexp test echo loadenv gfxterm gfxterm_background png font video halt reboot keystatus"
"$grub/pc/bin/grub-mkimage" -d "$grub/efi/lib/grub/x86_64-efi" -O x86_64-efi -p /boot/grub -c "$tree/embed.cfg" \
  -o "$tree/EFI/BOOT/BOOTX64.EFI" $mods efi_gop
"$grub/pc/bin/grub-mkimage" -d "$grub/pc/lib/grub/i386-pc" -O i386-pc-eltorito -p /boot/grub -c "$tree/embed.cfg" \
  -o "$tree/boot/grub/i386-pc/eltorito.img" $mods biosdisk vbe
rm "$tree/embed.cfg"
# EFI system partition image (El Torito EFI entry and appended GPT partition).
truncate -s 4M "$tree/efi.img"
mkfs.vfat -n RBINSTEFI "$tree/efi.img" >/dev/null
mmd -i "$tree/efi.img" ::/EFI ::/EFI/BOOT
mcopy -i "$tree/efi.img" "$tree/EFI/BOOT/BOOTX64.EFI" ::/EFI/BOOT/BOOTX64.EFI

log "xorriso: hybrid ISO"
rm -f "$out"
xorriso -as mkisofs -graft-points -V RETROBOOT -iso-level 3 \
  -b boot/grub/i386-pc/eltorito.img -no-emul-boot -boot-load-size 4 -boot-info-table \
  --grub2-boot-info --grub2-mbr "$grub/pc/lib/grub/i386-pc/boot_hybrid.img" \
  --efi-boot efi.img -efi-boot-part --efi-boot-image --protective-msdos-label \
  -o "$out" -r "$tree" --sort-weight 0 / --sort-weight 1 /boot 2>&1 | grep -v -E '^(xorriso|Drive|Media|libisofs|Added|Writing|ISO image|Written)' >&2 || true
[ -s "$out" ] || die "xorriso produced no ISO"
log "ISO: $(du -h "$out" | cut -f1) ($marker)"
