#!/bin/bash
# arm64 build environment for the Raspberry Pi image (cached directory chroot).
#
#   build-env.sh            prints the chroot directory
#
# Debian 13 arm64 with a compiler, meson and the -dev packages rb-presenter needs, plus
# the runtime libraries of the emulator package (config/packages.buildenv). It runs
# through the builder's qemu-user binfmt (flags POF), so nothing arm64 is installed
# into the builder's own dpkg. Used by fetch-components.sh to build rb-presenter and
# rb-splash natively and to map every staged ELF's libraries to Debian packages.
# Cached by package list; RB_REFRESH_BASE=1 rebuilds it.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
. "$here/rpi-lib.sh"
require_root
packages=$(grep -hv '^\s*#' "$RPI_SRC/config/packages.buildenv" | awk 'NF' | sort -u | paste -sd, -)
key=$( { cfg debian_suite; cfg debian_mirror; echo "$packages"; } | sha256sum | cut -c1-16)
dir=$CACHE/rpi-buildenv-$key
if [ -f "$dir/.complete" ] && [ "${RB_REFRESH_BASE:-0}" != 1 ]; then
  echo "$dir"; exit 0
fi
log "arm64 build environment: $(echo "$packages" | tr ',' ' ' | wc -w) packages"
rm -rf "$dir" "$dir.tmp"; mkdir -p "$CACHE"
suite=$(cfg debian_suite); mirror=$(cfg debian_mirror); security=$(cfg debian_security_mirror)
mmdebstrap --mode=root --variant=minbase --architectures=arm64 --format=directory \
  --components=main --aptopt='APT::Install-Recommends "false"' --aptopt='Acquire::Languages "none"' \
  --include="$packages" "$suite" "$dir.tmp" \
  "deb $mirror $suite main" "deb $mirror $suite-updates main" "deb $security $suite-security main" >&2
mv "$dir.tmp" "$dir"; touch "$dir/.complete"
echo "$dir"
