#!/bin/bash
# Stage 3 (Raspberry Pi): boot configuration (no GRUB on the Pi).
#
#   make-boot.sh OUTDIR
#
# OUTDIR/config.txt       firmware configuration (boot/config.txt)
# OUTDIR/cmdline.txt.in   kernel command line; make-disk.sh fills in RBROOT_A's PARTUUID
# OUTDIR/boot.json        provenance; customize-rpi.sh adds the boot partition's files
#                         (firmware, kernel, initramfs, DTBs) with their hashes
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
. "$here/rpi-lib.sh"
out=${1:?output dir}
rm -rf "$out"; mkdir -p "$out"
cp "$RPI_SRC/boot/config.txt" "$out/config.txt"
tr -s ' \n' ' ' < "$RPI_SRC/boot/cmdline.txt.in" | sed 's/ $//' > "$out/cmdline.txt.in"
grep -q '@ROOT_PARTUUID@' "$out/cmdline.txt.in" || die "cmdline.txt.in lacks @ROOT_PARTUUID@"
# HyperPixel uses the serial UART pins. A tty0 fallback keeps /dev/console valid
# when its profile disables the UART; fbcon is explicitly mapped away from every
# physical framebuffer, KMS fbdev is disabled, and VT gettys are masked.
if grep -qE '(^| )console=tty[0-9]' "$out/cmdline.txt.in"; then
  for word in console=tty0 fbcon=map:9 drm_kms_helper.fbdev_emulation=0 quiet loglevel=0; do
    grep -qE "(^| )$word( |$)" "$out/cmdline.txt.in" || die "quiet console fallback requires $word"
  done
fi
# serial0 must be the first console=: the PL011 driver binds its one console to the first
# ttyAMA entry, so a ttyAMA10 listed first leaves a board whose serial0 is a PL011 without a
# console (kernel panic at switch_root; image/reports/rpi-2026-10-13.md section 10)
[ "$(tr ' ' '\n' < "$out/cmdline.txt.in" | grep '^console=' | head -n1)" = console=serial0,115200 ] ||
  die "cmdline.txt: console=serial0 must be the first console="
# dtparam=audio=on must reach the base device tree, i.e. come before the first dtoverlay=
awk '/^dtoverlay=/{o=1} /^dtparam=audio=on$/{if(o) exit 1; a=1} END{exit !a}' "$out/config.txt" ||
  die "config.txt: dtparam=audio=on must come before the first dtoverlay="

python3 "$here/sd_profile.py" profile "${RB_RPI_MODEL:-pi4-5}" --out "$out/rpi-profile.json"
python3 - "$out" "$(rcfg rpi_mirror)" "$(rcfg rpi_suite)" "$(rcfg rpi_archive_key_fpr)" <<'PY'
import hashlib, json, pathlib, sys
out, mirror, suite, fpr = sys.argv[1:]
root = pathlib.Path(out)
profile = json.loads((root / 'rpi-profile.json').read_text())
boards = (['Raspberry Pi Zero 2 W'] if profile['model'] == 'zero2w' else
          ['Raspberry Pi 4 Model B', 'Raspberry Pi 400', 'Compute Module 4', 'Raspberry Pi 5', 'Raspberry Pi 500'])
json.dump({'board': 'rpi', 'boards': boards, 'profile': profile,
           'partition_table': profile['partition_table'],
           'kernel_cmdline': (root / 'cmdline.txt.in').read_text().strip(),
           'config_txt_sha256': hashlib.sha256((root / 'config.txt').read_bytes()).hexdigest(),
           'raspberrypi_archive': {'url': mirror, 'suite': suite, 'key_fingerprint': fpr}},
          open(root / 'boot.json', 'w'), indent=2)
PY
log "Raspberry Pi boot configuration ready"
