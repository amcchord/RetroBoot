#!/bin/bash
# Stage the installer's embedded guest machines (private builds) for live/embedded.squashfs.
#
#   stage-embedded.sh STAGE HOSTD_LIB MACHINE_DIR MACHINE_ID [MACHINE_DIR MACHINE_ID ...]
#
# Lays out STAGE/usr/lib/retroboot/installer/embedded/machines/<id>/ for each pair: a
# sparse copy of the directory's files, machine.json (make-machine-json.py, hostd's
# sanitiser) and disk.img.extents (make-extents.py: the data map install-to-disk copies
# by, ARCHITECTURE 14), plus machines/order with the ids in the order given. The
# installer lists the Macs in that order, checks them all, and the first one starts first.
# The initramfs (retroboot-live) overlays the squashfs as is; hostd still reads the older
# single embedded/machine/ layout. Source directories are only read.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
. "$here/lib.sh"

stage=${1:?stage directory}; hostd=${2:?hostd library directory}; shift 2
[ $# -gt 0 ] && [ $(($# % 2)) = 0 ] || die "stage-embedded.sh: MACHINE_DIR MACHINE_ID pairs expected"
emb=$stage/usr/lib/retroboot/installer/embedded/machines
install -d -m 0755 "$emb"
: > "$emb/order"
while [ $# -gt 0 ]; do
  dir=$1; id=$2; shift 2
  [[ "$id" =~ ^[a-z0-9][a-z0-9-]{0,47}$ ]] || die "invalid machine id '$id'"
  [ -f "$dir/disk.img" ] || die "$dir has no disk.img"
  [ ! -e "$emb/$id" ] || die "machine id '$id' is embedded twice"
  log "embedded guest machine '$id' (private)"
  install -d -m 0755 "$emb/$id"
  for f in "$dir"/*; do
    [ -f "$f" ] && cp --sparse=always --reflink=never "$f" "$emb/$id/"
  done
  chmod 0644 "$emb/$id"/*
  python3 "$IMAGE_SRC/scripts/make-machine-json.py" "$emb/$id" "$id" "$hostd"
  # squashfs stores the disk's zero blocks sparsely but reports no holes; the map lets
  # install-to-disk read and write only the data (ARCHITECTURE section 14)
  python3 "$IMAGE_SRC/scripts/make-extents.py" "$emb/$id/disk.img" "$hostd"
  chmod 0644 "$emb/$id"/*
  echo "$id" >> "$emb/order"
done
chmod 0644 "$emb/order"
