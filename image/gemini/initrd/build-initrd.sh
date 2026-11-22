#!/bin/bash
# Build RetroBoot's Gemini PDA initrd on rb-builder (Debian 13 x86_64, as root).
#
# Contents: Debian's static busybox (all applets as /bin links), Debian's static
# e2fsck, RetroBoot's /init and /lib/retroboot/charger.sh, /etc/e2fsck.conf, and the
# /dev/console and /dev/null nodes. Output: a reproducible gzip newc cpio (LK copies it
# verbatim; the kernel unpacks it) in $WORK/out/initrd/<build-id>/ with manifest.json,
# listing.txt and SHA256SUMS.
#
# Usage: build-initrd.sh [--build-id ID]
# Environment: RB_GEMINI_WORK (default /srv/retroboot/wave7/gemini-kernel),
#   RB_SOURCE_REVISION (RetroBoot commit; also written into the initrd).
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$HERE/packages.conf"

WORK=${RB_GEMINI_WORK:-/srv/retroboot/wave7/gemini-kernel}
BUILD_ID=""
while [ $# -gt 0 ]; do
  case "$1" in
    --build-id) BUILD_ID=$2; shift 2 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
SRC_REV=${RB_SOURCE_REVISION:-unknown}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
[ -n "$BUILD_ID" ] || BUILD_ID="initrd-${STAMP}-${SRC_REV:0:8}"
OUT=$WORK/out/initrd/$BUILD_ID
B=$WORK/build/$BUILD_ID
ROOT=$B/root
APTROOT=$WORK/apt-arm64
DEBCACHE=$WORK/cache/debs

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }
sha() { sha256sum "$1" | cut -d' ' -f1; }

[ "$(id -u)" = 0 ] || die "run as root (device nodes in the cpio)"
for t in apt-get dpkg-deb cpio gzip qemu-aarch64 python3 file; do
  command -v "$t" >/dev/null || die "missing tool $t"
done
[ ! -e "$OUT" ] || die "output $OUT already exists"
mkdir -p "$OUT" "$B" "$DEBCACHE"

# ---- Debian arm64 packages through a private apt state (the host stays amd64-only) ----
mkdir -p "$APTROOT/etc/apt/apt.conf.d" "$APTROOT/etc/apt/preferences.d" \
         "$APTROOT/var/lib/apt/lists/partial" "$APTROOT/var/cache/apt/archives/partial" "$APTROOT/var/lib/dpkg"
touch "$APTROOT/var/lib/dpkg/status"
cat > "$APTROOT/etc/apt/sources.list" <<EOF
deb [arch=arm64 signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] $DEBIAN_MIRROR $DEBIAN_SUITE main
deb [arch=arm64 signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] $DEBIAN_MIRROR $DEBIAN_SUITE-updates main
deb [arch=arm64 signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] $DEBIAN_SECURITY_MIRROR $DEBIAN_SUITE-security main
EOF
APT=(-o "Dir=$APTROOT" -o "Dir::State::status=$APTROOT/var/lib/dpkg/status"
     -o APT::Architecture=arm64 -o APT::Architectures::=arm64 -o APT::Sandbox::User=root
     -o "Dir::Etc::Parts=$APTROOT/etc/apt/apt.conf.d" -o "Dir::Etc::Preferences=$APTROOT/etc/apt/preferences"
     -o "Dir::Etc::PreferencesParts=$APTROOT/etc/apt/preferences.d")
fetch_deb() {  # fetch_deb PKG VERSION SHA256 → path in $DEBCACHE
  local pkg=$1 ver=$2 want=$3 f
  f=$DEBCACHE/${pkg}_${ver#*:}_arm64.deb
  if [ ! -f "$f" ] || [ "$(sha "$f")" != "$want" ]; then
    apt-get "${APT[@]}" -qq update
    rm -rf "$B/dl"; mkdir -p "$B/dl"
    (cd "$B/dl" && apt-get "${APT[@]}" -qq download "$pkg=$ver") || die "apt cannot fetch $pkg=$ver (update packages.conf)"
    mv "$B/dl"/*.deb "$f"
  fi
  [ "$(sha "$f")" = "$want" ] || die "$pkg $ver sha256 $(sha "$f") != pinned $want"
  echo "$f"
}
BB_DEB=$(fetch_deb busybox-static "$BUSYBOX_STATIC_VERSION" "$BUSYBOX_STATIC_SHA256")
E2_DEB=$(fetch_deb e2fsck-static "$E2FSCK_STATIC_VERSION" "$E2FSCK_STATIC_SHA256")
rm -rf "$B/x"; mkdir -p "$B/x"
dpkg-deb -x "$BB_DEB" "$B/x"
dpkg-deb -x "$E2_DEB" "$B/x"
for bin in "$B/x/usr/bin/busybox" "$B/x/usr/sbin/e2fsck.static"; do
  file -b "$bin" | grep -q 'ARM aarch64.*statically linked' || die "$bin is not a static aarch64 executable"
done
log "packages: busybox-static $BUSYBOX_STATIC_VERSION, e2fsck-static $E2FSCK_STATIC_VERSION"

# ---- Tree --------------------------------------------------------------------------------
rm -rf "$ROOT"
mkdir -p "$ROOT"/{bin,sbin,dev,proc,sys,run,tmp,etc,lib/retroboot,mnt/linux,newroot}
install -m 0755 "$B/x/usr/bin/busybox" "$ROOT/bin/busybox"
install -m 0755 "$B/x/usr/sbin/e2fsck.static" "$ROOT/sbin/e2fsck"
APPLETS=$(qemu-aarch64 "$ROOT/bin/busybox" --list)
for a in $APPLETS; do
  case "$a" in busybox|e2fsck) continue ;; esac
  ln -s busybox "$ROOT/bin/$a"
done
install -m 0755 "$HERE/init" "$ROOT/init"
install -m 0644 "$HERE/charger.sh" "$ROOT/lib/retroboot/charger.sh"
echo "${SRC_REV:0:12}" > "$ROOT/lib/retroboot/initrd-version"
# The RTC may be wrong this early; never let time checks turn a clean fsck into a repair.
printf '[options]\n\tbroken_system_clock = 1\n' > "$ROOT/etc/e2fsck.conf"
ln -s /proc/self/mounts "$ROOT/etc/mtab"
mknod -m 0600 "$ROOT/dev/console" c 5 1
mknod -m 0666 "$ROOT/dev/null" c 1 3
chmod 1777 "$ROOT/tmp"

# Shell syntax of the scripts, with the busybox that will run them.
qemu-aarch64 "$ROOT/bin/busybox" sh -n "$ROOT/init" || die "init: syntax error"
qemu-aarch64 "$ROOT/bin/busybox" sh -n "$ROOT/lib/retroboot/charger.sh" || die "charger.sh: syntax error"

# The 32-byte para command /init writes must be exactly "boot-recovery" + 19 NULs.
PARA_REF_SHA=$( { printf 'boot-recovery'; head -c 19 /dev/zero; } | sha256sum | cut -d' ' -f1)
PARA_BUILT=$(cd "$B" && qemu-aarch64 "$ROOT/bin/busybox" sh -c '
  dd if=/dev/zero of=para.ref bs=32 count=1 2>/dev/null
  printf "boot-recovery" | dd of=para.ref bs=32 count=1 conv=notrunc 2>/dev/null' && sha "$B/para.ref")
[ "$PARA_BUILT" = "$PARA_REF_SHA" ] || die "para reference construction differs ($PARA_BUILT != $PARA_REF_SHA)"

# ---- Archive -------------------------------------------------------------------------------
find "$ROOT" -exec touch -h -d "@$INITRD_EPOCH" {} +
CPIO=$OUT/initrd.cpio.gz
(cd "$ROOT" && find . -mindepth 1 -print0 | LC_ALL=C sort -z |
   cpio --quiet -o -0 -H newc -R 0:0 --reproducible) > "$B/initrd.cpio"
gzip -9n -c "$B/initrd.cpio" > "$CPIO"
(cd "$ROOT" && find . -mindepth 1 -print0 | LC_ALL=C sort -z | xargs -0 ls -ld --time-style=+%s) > "$OUT/listing.txt"
SIZE=$(stat -c%s "$CPIO")
USIZE=$(stat -c%s "$B/initrd.cpio")
[ "$SIZE" -lt $((2 * 1024 * 1024)) ] || die "initrd is $SIZE bytes; keep it under 2 MiB"

python3 - "$OUT/manifest.json" <<PY
import json, sys
json.dump({
  "kind": "retroboot-gemini-initrd",
  "build_id": "$BUILD_ID",
  "created_utc": "$STAMP",
  "retroboot_revision": "$SRC_REV",
  "scripts": {"build-initrd.sh": "$(sha "$HERE/build-initrd.sh")", "init": "$(sha "$HERE/init")",
              "charger.sh": "$(sha "$HERE/charger.sh")", "packages.conf": "$(sha "$HERE/packages.conf")"},
  "debian_packages": {
    "busybox-static": {"version": "$BUSYBOX_STATIC_VERSION", "deb_sha256": "$BUSYBOX_STATIC_SHA256",
                       "binary_sha256": "$(sha "$ROOT/bin/busybox")"},
    "e2fsck-static": {"version": "$E2FSCK_STATIC_VERSION", "deb_sha256": "$E2FSCK_STATIC_SHA256",
                      "binary_sha256": "$(sha "$ROOT/sbin/e2fsck")"}},
  "busybox_applets": $(echo $APPLETS | wc -w),
  "para_recovery_command_sha256": "$PARA_REF_SHA",
  "contract": {"counter_file": "/.retroboot-boot-attempts on the linux filesystem (decimal, newline)",
               "max_attempts": 3, "root_subdir": "/retroboot",
               "fallback_note": "/.retroboot-fallback", "log": "/.retroboot-initrd.log",
               "runtime_state": "/run/retroboot-initrd/state"},
  "outputs": {"initrd.cpio.gz": {"sha256": "$(sha "$CPIO")", "size": $SIZE, "uncompressed_size": $USIZE}},
}, open(sys.argv[1], "w"), indent=2, sort_keys=True)
PY
(cd "$OUT" && sha256sum initrd.cpio.gz > SHA256SUMS)
rm -rf "$B"
ln -sfn "$BUILD_ID" "$WORK/out/initrd/latest"
log "done: $CPIO ($SIZE bytes, $USIZE uncompressed)"
