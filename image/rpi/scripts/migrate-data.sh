#!/bin/bash
# Copy RetroBoot's persistent data from a stopped Pi to a fresh 3.0 SD card.
set -euo pipefail

if [ "${1:-}" = --help ] || [ "$#" -ne 2 ]; then
  echo "Usage: sudo $0 SOURCE_RBDATA NEW_RBDATA"
  echo "Both arguments are unmounted XFS partitions labelled RBDATA on different disks."
  if [ "${1:-}" = --help ]; then exit 0; else exit 2; fi
fi

fail() { printf '%s\n' "$*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || fail "Run the migration on Linux as root."
for tool in blkid df findmnt lsblk mount rsync umount; do
  command -v "$tool" >/dev/null || fail "Install $tool before running the migration."
done

source_dev=$(readlink -f -- "$1")
target_dev=$(readlink -f -- "$2")
for dev in "$source_dev" "$target_dev"; do
  [ -b "$dev" ] || fail "$dev is not a block device."
  [ "$(lsblk -ndo TYPE "$dev")" = part ] || fail "$dev is not a partition."
  [ "$(blkid -p -s TYPE -o value "$dev")" = xfs ] &&
    [ "$(blkid -p -s LABEL -o value "$dev")" = RBDATA ] || fail "$dev is not a RetroBoot data partition."
  if findmnt -rn --source "$dev" >/dev/null; then fail "Unmount $dev first."; fi
done
[ "$(lsblk -ndo PKNAME "$source_dev")" != "$(lsblk -ndo PKNAME "$target_dev")" ] ||
  fail "Use two separate disks."

work=$(mktemp -d)
src=$work/source
dst=$work/target
mkdir "$src" "$dst"
cleanup() {
  if mountpoint -q "$dst"; then umount "$dst" || true; fi
  if mountpoint -q "$src"; then umount "$src" || true; fi
  rmdir "$src" "$dst" "$work" 2>/dev/null || true
}
trap cleanup EXIT
mount -t xfs -o ro,norecovery,nosuid,nodev,noexec "$source_dev" "$src"
mount -t xfs -o rw,nosuid,nodev,noexec "$target_dev" "$dst"
[ -d "$src/config" ] && [ -d "$src/machines" ] || fail "The source does not contain RetroBoot data."
[ -d "$dst/config" ] && [ -d "$dst/machines" ] || fail "The destination does not contain RetroBoot data."

stage=$dst/.retroboot-migrate
identity=$(blkid -p -s UUID -o value "$source_dev")
if [ -e "$stage" ]; then
  [ -d "$stage" ] && [ ! -L "$stage" ] && [ -f "$stage/source" ] &&
    [ "$(cat "$stage/source")" = "$identity" ] || fail "A migration from a different source is present."
else
  [ -z "$(find "$dst/machines" -mindepth 1 -maxdepth 1 -print -quit)" ] ||
    fail "Use a fresh 3.0 card with no existing Macs."
  mkdir -m 0700 "$stage"
  printf '%s\n' "$identity" > "$stage/source"
fi

echo "Copying RetroBoot data from $source_dev to $target_dev."
# No --delete. A destination can be used again after an interrupted transfer.
# Keep the new card's host-slot and board state. The 3.0 boot code owns those files.
exclude=(--exclude=/.retroboot-migrate --exclude=/config/install-seed.json
         --exclude=/state/update --exclude=/state/boot)
copy=(rsync -aHAXS --numeric-ids --one-file-system "${exclude[@]}")
"${copy[@]}" --info=progress2 "$src/" "$dst/"
differences=$("${copy[@]}" --dry-run --checksum --itemize-changes "$src/" "$dst/")
[ -z "$differences" ] || fail "The copied data did not match. Leave the old card intact and retry."
sync -f "$dst"
rm "$stage/source"
rmdir "$stage"
umount "$dst"
umount "$src"
echo "Migration complete. Boot the new card with the old card disconnected."
