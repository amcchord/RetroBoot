#!/bin/bash
# Stage 2b (Raspberry Pi): Pi-specific finishing of the root after customize-rootfs.sh
# (RB_BOARD=rpi), and the boot partition's file tree.
#
#   customize-rpi.sh FLAVOR ROOTDIR BOOTDIR
#
# BOOTDIR/bootfs/ becomes the FAT boot partition: the Pi 4's GPU firmware (start4*.elf,
# fixup4*.dat; the Pi 5 has its firmware in the EEPROM), DTBs for BCM2711 (Pi 4, Pi 400,
# CM4) and BCM2712 (Pi 5 C1/D0, Pi 500, CM5), all overlays (incl. overlay_map.dtb, which
# maps vc4-kms-v3d to -pi4/-pi5), kernel8.img and initramfs8 from the root's /boot,
# config.txt. make-disk.sh adds cmdline.txt.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
. "$here/rpi-lib.sh"
require_root
flavor=${1:?flavor}; root=${2:?rootdir}; boot=${3:?boot dir}

kver=$(basename "$(ls -d "$root"/lib/modules/* | sort -V | tail -n1)")
case "$kver" in *-rpi-$(rcfg rpi_kernel_flavour)) ;; *) die "unexpected kernel $kver (want the -rpi-$(rcfg rpi_kernel_flavour) flavour)" ;; esac
dtbdir=$root/usr/lib/modules/$kver/dtb
[ -d "$dtbdir/broadcom" ] || dtbdir=$root/usr/lib/linux-image-$kver
[ -d "$dtbdir/broadcom" ] && [ -d "$dtbdir/overlays" ] || die "no DTBs for $kver"

log "boot partition tree for $kver"
bfs=$boot/bootfs
rm -rf "$bfs"; mkdir -p "$bfs/overlays"
cp "$root"/usr/lib/raspi-firmware/start4*.elf "$root"/usr/lib/raspi-firmware/fixup4*.dat \
   "$root"/usr/lib/raspi-firmware/LICENCE.broadcom "$bfs/"
cp "$dtbdir"/broadcom/bcm2711-*.dtb "$dtbdir"/broadcom/bcm2712*.dtb "$bfs/"
if [ "${RB_RPI_MODEL:-pi4-5}" = zero2w ]; then
  # The Zero's ROM/firmware does not follow the Pi 4's EEPROM boot path.
  cp "$root"/usr/lib/raspi-firmware/bootcode.bin "$root"/usr/lib/raspi-firmware/start*.elf \
     "$root"/usr/lib/raspi-firmware/fixup*.dat "$bfs/"
  cp "$dtbdir/broadcom/bcm2710-rpi-zero-2-w.dtb" "$bfs/"
fi
cp "$dtbdir"/overlays/*.dtb* "$bfs/overlays/"
[ -f "$dtbdir/overlays/README" ] && cp "$dtbdir/overlays/README" "$bfs/overlays/"
cp "$root/boot/vmlinuz-$kver" "$bfs/kernel8.img"
cp "$root/boot/initrd.img-$kver" "$bfs/initramfs8"
cp "$boot/config.txt" "$bfs/config.txt"
cp "$boot/rpi-profile.json" "$root/usr/lib/retroboot/rpi-profile.json"
if [ "${RB_RPI_MODEL:-pi4-5}" = zero2w ]; then
  for f in bootcode.bin start.elf fixup.dat start_cd.elf fixup_cd.dat bcm2710-rpi-zero-2-w.dtb; do
    [ -s "$bfs/$f" ] || die "Zero 2 W boot partition: $f missing"
  done
  [ ! -e "$root/usr/lib/retroboot/qemu/bin/qemu-system-ppc" ] || die "Zero 2 W image contains the PPC emulator"
  # The helper starts on demand. Bound its memory beside the Mac on a 512 MiB board,
  # while keeping enough headroom for lxml/readability/Pillow and cairosvg/svgworker.
  install -d "$root/etc/systemd/system/retroboot-webproxy.service.d"
  printf '%s\n' '[Service]' 'MemoryHigh=96M' 'MemoryMax=128M' > "$root/etc/systemd/system/retroboot-webproxy.service.d/50-zero2w.conf"
fi
[ -d "$root/usr/share/retroboot/chimes" ] || stage_startup_chimes "$root"
# The example is deliberately not imported until the user renames and fills it.
install -d "$bfs/retroboot"
cat > "$bfs/retroboot/network.conf.example" <<'EOF'
# Rename this file to network.conf and enter your network name and password.
# Plain KEY=VALUE lines; no shell commands. Optional quotes are accepted.
WIFI_SSID=Your network name
WIFI_PASSWORD=Your network password
# WIFI_SECURITY=wpa-psk
# WIFI_HIDDEN=no
EOF
cat > "$bfs/SETUP.txt" <<'EOF'
RetroBoot SD card

Insert this card in the Raspberry Pi and turn it on. No installer is required.
The data partition expands to use the card's free space on the first start.
Use a USB keyboard and mouse; on the Zero 2 W, use a USB OTG adapter or hub.
Control-Alt-Delete opens RetroBoot settings and recovery.

Wi-Fi: rename retroboot/network.conf.example to retroboot/network.conf and
enter your network details, or configure Wi-Fi in the RetroBoot panel.
Custom-image settings in Raspberry Pi Imager are not imported by RetroBoot.

Displays: see DISPLAY-SETUP.txt on this partition.
Always shut down from Mac OS or RetroBoot before removing power or the card.
EOF
PYTHONPATH="$root/usr/lib/retroboot/hostd" python3 "$RPI_SRC/scripts/display_profiles.py" stage --bootfs "$bfs" \
  --model "${RB_RPI_MODEL:-pi4-5}" --overlay-dir "$bfs/overlays"
# The installer derives its target boot files from this payload, as well as the
# firmware in the installed root. Keep identical display choices on both paths.
install -d "$root/usr/lib/retroboot/boot/retroboot"
cp -a "$bfs/retroboot/." "$root/usr/lib/retroboot/boot/retroboot/"
cp "$bfs/DISPLAY-SETUP.txt" "$bfs/SETUP.txt" "$root/usr/lib/retroboot/boot/"
for f in start4.elf fixup4.dat bcm2711-rpi-4-b.dtb bcm2711-rpi-400.dtb bcm2711-rpi-cm4.dtb bcm2712-rpi-5-b.dtb \
         bcm2712d0-rpi-5-b.dtb bcm2712-rpi-500.dtb overlays/vc4-kms-v3d.dtbo overlays/vc4-kms-v3d-pi4.dtbo \
         overlays/vc4-kms-v3d-pi5.dtbo overlays/overlay_map.dtb kernel8.img initramfs8; do
  [ -s "$bfs/$f" ] || die "boot partition: $f missing"
done
# Stage slot-bootable Pi files inside the rootfs so Software Update bundles (.rbu)
# carry them to the inactive slot (hostd/retroboot/update/writer.py sync_rpi_slot).
rm -rf "$root/usr/lib/retroboot/boot/bootfs"
install -d "$root/usr/lib/retroboot/boot/bootfs/overlays"
cp "$bfs/kernel8.img" "$bfs/initramfs8" "$bfs/config.txt" "$root/usr/lib/retroboot/boot/bootfs/"
cp "$bfs"/*.dtb "$root/usr/lib/retroboot/boot/bootfs/"
cp "$bfs"/overlays/*.dtb* "$root/usr/lib/retroboot/boot/bootfs/overlays/"
# The boot partition is mounted where Debian and Raspberry Pi OS mount it, /boot/firmware
# (hostd reads retroboot/network.conf there); /boot/efi, where the x86 image mounts its
# ESP and where hostd's ESP code and rb-bootdisplay-rpi look by default, points to it.
# The base build's raspi-firmware hooks had copied kernel/DTBs into a plain directory there.
rm -rf "$root/boot/firmware"; install -d "$root/boot/firmware"
rmdir "$root/boot/efi" 2>/dev/null || rm -f "$root/boot/efi"
ln -s firmware "$root/boot/efi"
sed -i 's|^PARTLABEL=RBEFI     /boot/efi           vfat |PARTLABEL=RBEFI     /boot/firmware      vfat |' "$root/etc/fstab"
grep -q '^PARTLABEL=RBEFI     /boot/firmware ' "$root/etc/fstab" || die "fstab: boot partition line not found"
sed -i 's|^# RBDATA and the ESP are optional at boot|# (Raspberry Pi: the ESP is the FAT boot partition at /boot/firmware; /boot/efi links to it.)\n&|' "$root/etc/fstab"

# Pi 4 has no real-time clock (the Pi 5's needs a battery): clock epoch, the first boot's
# ldconfig/journal catalog/update stamps done now, x86-only units off (scripts/arm64-lib.sh;
# with the firmware's 8250.nr_uarts=1 a ttyS0 exists even where no mini UART is enabled,
# so retroboot-bootmark is off too). retroboot-bootdisplay stays: on the Pi it runs
# rb-bootdisplay-rpi (rootfs/etc/systemd/system/retroboot-bootdisplay.service.d).
arm64_firstboot_done "$root"

# Debug flavor: the emulator workstream's pi-check bundle (scripts/arm64-lib.sh).
arm64_pi_check "$flavor" "$root"

python3 - "$boot/boot.json" "$bfs" "$kver" "$root" <<'PY'
import hashlib, json, pathlib, subprocess, sys
path, bfs, kver, root = sys.argv[1:]
b = json.load(open(path))
files = {str(p.relative_to(bfs)): hashlib.sha256(p.read_bytes()).hexdigest()
         for p in sorted(pathlib.Path(bfs).rglob('*')) if p.is_file()}
q = subprocess.run(['chroot', root, 'dpkg-query', '-W', '-f=${Package}\t${Version}\n'], capture_output=True, text=True).stdout
rpt = {n: v for n, v in (l.split('\t') for l in q.splitlines()) if 'rpt' in v or n in ('raspi-firmware',)}
b.update({'kernel': kver, 'bootfs_files_sha256': files, 'raspberrypi_packages': rpt})
b['raspberrypi_archive']['packages'] = sorted(rpt)
json.dump(b, open(path, 'w'), indent=2)
PY
cp "$boot/boot.json" "$root/usr/lib/retroboot/boot/boot.json"
chown -R 0:0 "$root/usr/lib/retroboot/boot"
log "boot partition: $(du -sh "$bfs" | cut -f1), kernel8.img $(stat -c %s "$bfs/kernel8.img") bytes, initramfs8 $(stat -c %s "$bfs/initramfs8") bytes"
