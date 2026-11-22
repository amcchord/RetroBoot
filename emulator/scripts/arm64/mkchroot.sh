#!/bin/bash
# mkchroot.sh CHROOT
# An amd64 Debian trixie chroot with the aarch64 cross toolchain and the arm64 build
# dependencies of the RetroBoot QEMU package (PORT_ARCH=arm64 in build-linux.sh).
# Nothing is installed into the build host's own dpkg. The arm64 -dev set gives the
# same configure result as the x86 builder's (glib, pixman, slirp, SDL2, ALSA, PNG,
# fdt, udev, selinux, zlib; no zstd/pulse/sndio, which the x86 package does not use).
# Its arm64 runtime libraries also run the package under qemu-user (QEMU_LD_PREFIX).
# Run as root on the x86_64 builder (mmdebstrap; arm64 maintainer scripts use binfmt).
set -euo pipefail
C=${1:?usage: mkchroot.sh CHROOT}
DEPS=libglib2.0-dev:arm64,libpixman-1-dev:arm64,libslirp-dev:arm64,libsdl2-dev:arm64,libasound2-dev:arm64,libpng-dev:arm64,libfdt-dev:arm64,libudev-dev:arm64,libselinux1-dev:arm64,zlib1g-dev:arm64
mmdebstrap --variant=apt --architectures=amd64,arm64 \
  --include=build-essential,crossbuild-essential-arm64,git,ca-certificates,python3,python3-venv,meson,ninja-build,pkgconf,bison,flex,file,binutils-multiarch,$DEPS \
  trixie "$C" \
  "deb https://deb.debian.org/debian trixie main" \
  "deb https://deb.debian.org/debian trixie-updates main" \
  "deb https://security.debian.org/debian-security trixie-security main"
chroot "$C" dpkg-query -W -f '${Package}:${Architecture} ${Version}\n' > "$C.packages.txt"
