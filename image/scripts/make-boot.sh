#!/bin/bash
# Stage 3: GRUB boot payload for the installed disk layout.
#
#   make-boot.sh OUTDIR
#
# OUTDIR/esp/        files for the RBEFI partition (EFI/BOOT/BOOTX64.EFI,
#                    EFI/retroboot/grubx64.efi, grub/grub.cfg, fonts, checkerboard)
# OUTDIR/i386-pc/    boot.img, core.img and grub-bios-setup for the BIOS boot partition
# OUTDIR/boot.json   provenance (GRUB build, modules, hashes, kernel command line)
#
# The same tree is shipped in the rootfs at /usr/lib/retroboot/boot so the
# installer writes exactly these bits to the target disk.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
. "$here/lib.sh"

out=${1:?output dir}
grub=$("$here/build-grub.sh" | tail -n1)
cmdline=$(tr -s ' \n' ' ' < "$IMAGE_SRC/config/cmdline" | sed 's/ $//')
rm -rf "$out"; mkdir -p "$out/esp/EFI/BOOT" "$out/esp/EFI/retroboot" "$out/esp/grub/fonts" "$out/esp/grub/retroboot" "$out/i386-pc"

common_mods="part_gpt part_msdos fat ext2 normal configfile linux search search_fs_file search_fs_uuid search_label regexp test echo loadenv gfxterm gfxterm_background png font video halt reboot"
# EFI: GOP only. A native bochs/cirrus driver would modeset behind the
# firmware's back and the kernel would inherit stale GOP information.
efi_mods="$common_mods efi_gop"
bios_mods="$common_mods biosdisk vbe"

"$grub/pc/bin/grub-mkimage" -d "$grub/efi/lib/grub/x86_64-efi" -O x86_64-efi -p /grub \
  -o "$out/esp/EFI/BOOT/BOOTX64.EFI" $efi_mods
cp "$out/esp/EFI/BOOT/BOOTX64.EFI" "$out/esp/EFI/retroboot/grubx64.efi"

"$grub/pc/bin/grub-mkimage" -d "$grub/pc/lib/grub/i386-pc" -O i386-pc -p '(,gpt1)/grub' \
  -o "$out/i386-pc/core.img" $bios_mods
cp "$grub/pc/lib/grub/i386-pc/boot.img" "$out/i386-pc/boot.img"
cp "$grub/pc/sbin/grub-bios-setup" "$out/i386-pc/grub-bios-setup"
core_size=$(stat -c %s "$out/i386-pc/core.img")
[ "$core_size" -lt $((1024 * 1024 - 4096)) ] || die "core.img ($core_size bytes) does not fit the 1 MiB BIOS boot partition"

sed "s|@CMDLINE@|$cmdline|" "$IMAGE_SRC/grub/grub.cfg.in" > "$out/esp/grub/grub.cfg"
# The ESP grub.cfg is shared by both system slots (ARCHITECTURE 13): a syntax error
# would stop every version from starting, so check it with GRUB's own parser.
if [ -x "$grub/pc/bin/grub-script-check" ]; then
  "$grub/pc/bin/grub-script-check" "$out/esp/grub/grub.cfg" || die "generated grub.cfg does not parse"
fi
grep -q '^# retroboot-slots: 1$' "$out/esp/grub/grub.cfg" || die "grub.cfg lacks the slot selection"
python3 "$IMAGE_SRC/scripts/make-blank-font.py" "$out/esp/grub/fonts/blank.pf2"
# Default background until rb-bootdisplay records this machine's mode (installer
# ISO, raw image first boot). GRUB draws it 1:1 from the top-left corner and fills
# whatever it does not cover with black, so it is as large as the modes we expect
# (2560x1600). Scale 2 is the section 4.5 scale for 1920-2879 pixel wide modes,
# i.e. most laptop panels and HDMI monitors (a 1920x1080 mini PC showed black
# bars right and bottom with the earlier 1680x1050 scale-1 default, and a finer
# pattern than the splash). Scale-1 modes (1280x800 VMs, 1366x768) see the
# coarser pattern until the splash takes over.
python3 "$IMAGE_SRC/rootfs/usr/lib/retroboot/image/rb-checkerboard" 2560 1600 2 "$out/esp/grub/retroboot/checker-default.png"

python3 - "$out" "$grub" "$cmdline" "$efi_mods" "$bios_mods" <<'EOF'
import hashlib, json, pathlib, sys
out, grub, cmdline, efi_mods, bios_mods = sys.argv[1:]
root = pathlib.Path(out)
files = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
         for p in sorted(root.rglob('*')) if p.is_file()}
info = json.loads(pathlib.Path(grub, 'build-info.json').read_text())
json.dump({'grub': info, 'kernel_cmdline': cmdline, 'efi_modules': efi_mods.split(),
           'bios_modules': bios_mods.split(), 'files_sha256': files},
          open(root / 'boot.json', 'w'), indent=2)
EOF
log "boot payload ready: EFI $(stat -c %s "$out/esp/EFI/BOOT/BOOTX64.EFI") bytes, core.img $core_size bytes"
