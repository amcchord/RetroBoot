#!/bin/bash
# Install a component's packaging directory into the rootfs.
#   install-packaging.sh PACKAGING_DIR ROOTDIR
# A packaging dir that mirrors the filesystem (has etc/ or usr/) is copied as is.
# Otherwise files are placed by type: *.service/*.socket/*.target/*.path/*.timer
# -> /usr/lib/systemd/system, sysusers*.conf or sysusers.d/ -> /usr/lib/sysusers.d,
# tmpfiles*.conf or tmpfiles.d/ -> /usr/lib/tmpfiles.d, initramfs-tools/{hooks,scripts}
# -> /usr/share/initramfs-tools/{hooks,scripts}.
set -euo pipefail
src=${1:?packaging dir}; root=${2:?rootdir}
if [ -d "$src/usr" ] || [ -d "$src/etc" ]; then
  for d in usr etc; do [ -d "$src/$d" ] && cp -a --no-preserve=ownership "$src/$d" "$root/"; done
  exit 0
fi
shopt -s nullglob globstar
for f in "$src"/**/*; do
  [ -f "$f" ] || continue
  rel=${f#"$src"/}; base=$(basename "$f")
  case "$rel" in
    *initramfs-tools/hooks/*|*initramfs/hooks/*) install -D -m 0755 "$f" "$root/usr/share/initramfs-tools/hooks/$base" ;;
    *initramfs-tools/scripts/*) sub=${rel#*initramfs-tools/scripts/}; install -D -m 0755 "$f" "$root/usr/share/initramfs-tools/scripts/$sub" ;;
    *initramfs/scripts/*) sub=${rel#*initramfs/scripts/}; install -D -m 0755 "$f" "$root/usr/share/initramfs-tools/scripts/$sub" ;;
    *.service|*.socket|*.target|*.path|*.timer|*.mount) install -D -m 0644 "$f" "$root/usr/lib/systemd/system/$base" ;;
    *sysusers*/*.conf|*sysusers*.conf) install -D -m 0644 "$f" "$root/usr/lib/sysusers.d/$base" ;;
    *tmpfiles*/*.conf|*tmpfiles*.conf) install -D -m 0644 "$f" "$root/usr/lib/tmpfiles.d/$base" ;;
    *.preset) install -D -m 0644 "$f" "$root/usr/lib/systemd/system-preset/$base" ;;
    *) echo "install-packaging: skipping $rel" >&2 ;;
  esac
done
