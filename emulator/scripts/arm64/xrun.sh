#!/bin/bash
# xrun.sh CHROOT DIR[,DIR...] -- COMMAND...
# Runs COMMAND (as root) inside the cross chroot, in a private mount namespace: /proc,
# /dev, /sys and each DIR are bind-mounted at the same path (DIR:ro read-only), and
# everything is unmounted when COMMAND exits.  Network for git comes from the host's
# resolv.conf.
set -euo pipefail
C=$(readlink -f "${1:?usage: xrun.sh CHROOT DIRS -- COMMAND...}"); DIRS=$2; shift 2
[ "${1:-}" = -- ] && shift
cp -L /etc/resolv.conf "$C/etc/resolv.conf"
exec unshare --mount --propagation private /bin/bash -c '
set -euo pipefail
C=$1; DIRS=$2; shift 2
mount -t proc proc "$C/proc"
mount --rbind /dev "$C/dev"
mount --rbind /sys "$C/sys"
IFS=, read -ra list <<< "$DIRS"
for d in "${list[@]}"; do
  ro=; case "$d" in *:ro) d=${d%:ro}; ro=1 ;; esac
  mkdir -p "$C$d"; mount --bind "$d" "$C$d"
  [ -n "$ro" ] && mount -o remount,bind,ro "$C$d"
done
exec chroot "$C" /usr/bin/env -i HOME=/root PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin LANG=C.UTF-8 "$@"
' xrun "$C" "$DIRS" "$@"
