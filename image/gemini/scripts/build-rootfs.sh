#!/bin/bash
# Stage 1 (Gemini PDA): Debian 13 arm64 base root file system (cached tarball).
#
#   build-rootfs.sh FLAVOR OUT.tar [EXTRA_PACKAGES_FILE]
#
# The Raspberry Pi recipe (scripts/arm64-lib.sh arm64_base_rootfs: minbase trixie main
# non-free-firmware non-free, no recommends, docs/man/locales excluded, qemu-user) with
# Debian's archive only: no kernel, no boot firmware and no initramfs-tools, because the
# boot.img on the PARTNAME=boot partition carries the kernel and its initrd.
# The cache key covers packages, sources and dpkg excludes; RB_REFRESH_BASE=1 forces a
# fresh download.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
. "$here/gemini-lib.sh"
require_root

flavor=${1:?flavor}; out=${2:?output tarball}; extra=${3:-}
suite=$(cfg debian_suite); mirror=$(cfg debian_mirror); security=$(cfg debian_security_mirror)
components=$(cfg debian_components)
ARM64_SOURCES=("deb $mirror $suite $components" "deb $mirror $suite-updates $components"
               "deb $security $suite-security $components")
ARM64_KEY_FILES=()
arm64_base_rootfs "$flavor" "$out" gemini "$GEMINI_SRC/config" "$extra"
