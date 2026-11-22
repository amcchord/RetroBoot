#!/bin/bash
# Stage 1: Debian 13 base root filesystem with mmdebstrap (cached tarball).
#
#   build-rootfs.sh FLAVOR OUT.tar
#
# The cache key is the package list, mirror configuration and dpkg excludes, so
# repeated image builds only redo the RetroBoot customisation (customize-rootfs.sh).
# Pass RB_REFRESH_BASE=1 to force a fresh download (e.g. to pick up security updates).
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
. "$here/lib.sh"
require_root

flavor=${1:?flavor}; out=${2:?output tarball}; extra=${3:-}
suite=$(cfg debian_suite); mirror=$(cfg debian_mirror); security=$(cfg debian_security_mirror)
components=$(cfg debian_components)

# EXTRA: runtime packages derived from the staged component binaries (ldd -> dpkg -S).
packages=$(grep -hv '^\s*#' "$IMAGE_SRC/config/packages.common" "$IMAGE_SRC/config/packages.$flavor" ${extra:+"$extra"} | awk 'NF' | sort -u | paste -sd, -)
# RB_DEBIAN_KERNEL=6.12.113+deb13-amd64 installs that exact kernel package instead of the
# linux-image-amd64 metapackage's current one (a point release keeping its predecessor's
# kernel; the archive keeps older kernels only as their own packages).
if [ -n "${RB_DEBIAN_KERNEL:-}" ]; then
  case ",$packages," in *,linux-image-amd64,*) ;; *) die "RB_DEBIAN_KERNEL: the package list has no linux-image-amd64" ;; esac
  packages=$(echo ",$packages," | sed "s/,linux-image-amd64,/,linux-image-$RB_DEBIAN_KERNEL,/; s/^,//; s/,\$//")
  log "kernel package pinned: linux-image-$RB_DEBIAN_KERNEL"
fi
key=$( { echo "$suite $mirror $security $components"; echo "$packages"; cat "$IMAGE_SRC/config/dpkg-excludes"; } | sha256sum | cut -c1-16)
cached=$CACHE/base-$flavor-$key.tar
if [ -s "$cached" ] && [ "${RB_REFRESH_BASE:-0}" != 1 ]; then
  log "base rootfs cache hit ($cached)"
else
  log "mmdebstrap $suite ($flavor): $(echo "$packages" | tr ',' ' ' | wc -w) packages"
  dpkgopts=()
  while read -r line; do [ -n "$line" ] && dpkgopts+=(--dpkgopt="$line"); done < "$IMAGE_SRC/config/dpkg-excludes"
  mmdebstrap --mode=root --variant=minbase --format=tar \
    --components="${components// /,}" \
    --aptopt='APT::Install-Recommends "false"' \
    --aptopt='Acquire::Languages "none"' \
    "${dpkgopts[@]}" \
    --include="$packages" \
    --customize-hook='chroot "$1" dpkg-query -W -f="\${Package}\t\${Version}\t\${Architecture}\n" > "$1/.rb-packages.tsv"' \
    "$suite" "$cached.tmp" \
    "deb $mirror $suite ${components}" \
    "deb $mirror $suite-updates ${components}" \
    "deb $security $suite-security ${components}"
  mv "$cached.tmp" "$cached"
fi
ln -sf "$cached" "$out"
