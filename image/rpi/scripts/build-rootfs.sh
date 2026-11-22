#!/bin/bash
# Stage 1 (Raspberry Pi): Debian 13 arm64 base root file system (cached tarball).
#
#   build-rootfs.sh FLAVOR OUT.tar [EXTRA_PACKAGES_FILE]
#
# Same recipe as image/scripts/build-rootfs.sh (minbase trixie main non-free-firmware
# non-free, no recommends, docs/man/locales excluded), for arm64 through qemu-user,
# with the Raspberry Pi archive added for the boot pieces only: config/apt-preferences
# pins every package from archive.raspberrypi.com to -1 except the kernel, the GPU
# firmware and the CYW43455 Wi-Fi/Bluetooth firmware (990). The archive key is checked
# against rpi.conf's fingerprint and stays in the image with the pinning.
# The cache key covers packages, sources, pinning, key and dpkg excludes;
# RB_REFRESH_BASE=1 forces a fresh download.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
. "$here/rpi-lib.sh"
require_root

flavor=${1:?flavor}; out=${2:?output tarball}; extra=${3:-}
mapfile -t ARM64_SOURCES < <(rpi_sources)
ARM64_KEY_FILES=("$RPI_SRC/config/apt-preferences" "$RPI_SRC/apt/raspberrypi-archive-keyring.asc")
# Called by arm64_base_rootfs on a cache miss only. The kernel's postinst hooks run there
# too (raspi-firmware's z50 hooks copy the kernel, DTBs and initrd into /boot/firmware of
# the root); customize-rpi.sh rebuilds the initramfs with RetroBoot's hooks and writes
# the boot partition itself.
arm64_keyring_dir() { rpi_keyring_dir; }
arm64_setup_hook() { rpi_setup_hook "$1"; }
arm64_base_rootfs "$flavor" "$out" rpi "$RPI_SRC/config" "$extra"
