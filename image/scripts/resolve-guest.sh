#!/bin/bash
# Resolve a registered guest fixture for a private build and verify it.
#
#   resolve-guest.sh ID      prints the machine directory on success
#
# image.conf maps ID to guest_<ID>_dir and guest_<ID>_disk_sha256 (the same
# values lab/fixture-manifest.json records). The directory must hold disk.img
# and config.json; disk.img must match the registered SHA-256 (the digest is
# cached under $CACHE by path, size and mtime, so a 32 GiB sparse disk is read
# once). The directory is only read.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
. "$here/lib.sh"
id=${1:?guest id}
dir=$(cfg "guest_${id}_dir"); want=$(cfg "guest_${id}_disk_sha256")
[ -n "$dir" ] || die "unknown guest '$id' (image.conf has no guest_${id}_dir)"
[ -f "$dir/disk.img" ] && [ -f "$dir/config.json" ] || die "guest '$id': $dir needs disk.img and config.json"
[[ "$want" =~ ^[0-9a-f]{64}$ ]] || die "guest '$id': no registered SHA-256 in image.conf"
key=$(stat -c '%n:%s:%Y' "$(readlink -f "$dir/disk.img")" | sha256sum | cut -c1-24)
cache=$CACHE/guest-sha256-$key
if [ -f "$cache" ]; then got=$(cat "$cache")
else
  log "verifying guest '$id' disk (SHA-256)"
  got=$(sha256sum "$dir/disk.img" | cut -d' ' -f1)
  mkdir -p "$CACHE"; echo "$got" > "$cache"
fi
[ "$got" = "$want" ] || die "guest '$id': disk.img SHA-256 $got differs from the registered $want"
readlink -f "$dir"
