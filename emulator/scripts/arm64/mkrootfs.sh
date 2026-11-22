#!/bin/bash
# mkrootfs.sh ROOTFS [KERNEL_FLAVOUR]
# A Debian trixie arm64 root with only the package's runtime libraries (no -dev), python3
# for the harness, and a kernel (default arm64-16k: 16 KiB pages, like the Pi 5's
# kernel_2712.img) for a qemu-system-aarch64 virt VM (vm-test.sh). Lab only; the arm64
# maintainer scripts run under qemu-user (binfmt).  Also the authority for the runtime
# package list: vm-test.sh checks that every NEEDED library resolves in this root.
set -euo pipefail
R=${1:?usage: mkrootfs.sh ROOTFS [arm64-16k|arm64]}; K=${2:-arm64-16k}
RUNTIME=libglib2.0-0t64,libpixman-1-0,libslirp0,libsdl2-2.0-0,libasound2t64,libpng16-16t64,libfdt1,libudev1,zlib1g,libx11-6,libpulse0,libsndio7.0
mmdebstrap --variant=apt --architectures=arm64 \
  --include=$RUNTIME,linux-image-$K,initramfs-tools,python3,procps,kmod \
  --customize-hook='echo rb16k > "$1/etc/hostname"' \
  trixie "$R" \
  "deb https://deb.debian.org/debian trixie main" \
  "deb https://deb.debian.org/debian trixie-updates main" \
  "deb https://security.debian.org/debian-security trixie-security main"
