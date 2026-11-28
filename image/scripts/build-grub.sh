#!/bin/bash
# Build RetroBoot's quiet GRUB for x86_64-efi, i386-efi and i386-pc from the
# pinned Debian source package, plus RetroBoot's small gfxterm patch (image/grub/patches).
#
# Why a rebuild: Debian only enables --enable-quiet-boot for Ubuntu. Without it
# i386-pc prints "Welcome to GRUB!", all platforms print "Booting `...'" and
# clear the screen before starting Linux, and the text cursor stays enabled.
#
# Output (cached by version + patch hash):
#   $GRUB_OUT/{pc,efi,efi32}/lib/grub/{i386-pc,x86_64-efi,i386-efi}/ modules, kernel.img, boot.img
#   $GRUB_OUT/pc/{bin,sbin}/                                          grub-mkimage, grub-bios-setup
#   $GRUB_OUT/build-info.json
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
. "$here/lib.sh"

GRUB_DEB_VERSION=${GRUB_DEB_VERSION:-$(cfg grub_debian_version)}
patch_hash=$(cat "$IMAGE_SRC"/grub/patches/*.patch | sha256sum | cut -c1-12)
GRUB_OUT=${GRUB_OUT:-$CACHE/grub-${GRUB_DEB_VERSION//[^A-Za-z0-9.]/_}-quiet-dualefi-$patch_hash}
echo "$GRUB_OUT"
[ -f "$GRUB_OUT/.complete" ] && exit 0
command -v gawk >/dev/null || die "GNU awk (gawk) is required to build GRUB"

src_root=$CACHE/grub-src/$GRUB_DEB_VERSION
if [ ! -d "$src_root/grub2-2.12" ]; then
  mkdir -p "$src_root"
  # apt verifies the .dsc/.tar hashes against the signed Sources index.
  (cd "$src_root" && apt-get source --download-only "grub2=$GRUB_DEB_VERSION" >&2 &&
   dpkg-source -x grub2_*.dsc grub2-2.12 >&2)
fi

work=$(mktemp -d "$CACHE/grub-build.XXXXXX")
cleanup() {
  local result=$?
  if [ "$result" -eq 0 ]; then
    rm -rf "$work"
  else
    log "GRUB build failed; source and logs preserved at $work"
  fi
}
trap cleanup EXIT
cp -a "$src_root/grub2-2.12" "$work/src"
for p in "$IMAGE_SRC"/grub/patches/*.patch; do
  log "applying $(basename "$p")"
  patch -d "$work/src" -p1 --no-backup-if-mismatch < "$p" >&2
done
(cd "$work/src" && ./autogen.sh >"$work/autogen.log" 2>&1) || { tail -n 40 -- "$work/autogen.log" >&2; exit 1; }

build_platform() {
  local name=$1 platform=$2 target=$3
  mkdir -p "$work/build-$name"
  (cd "$work/build-$name" &&
   AWK=gawk "$work/src/configure" --prefix="$GRUB_OUT/$name" --with-platform="$platform" --target="$target" \
     --program-prefix= --enable-quiet-boot --disable-werror --disable-grub-mkfont \
     --disable-grub-themes --disable-grub-mount --disable-device-mapper --disable-nls \
     --disable-libzfs --disable-efiemu >"$work/configure-$name.log" 2>&1 &&
   make -j"$(nproc)" >"$work/make-$name.log" 2>&1 &&
   make install >"$work/install-$name.log" 2>&1) ||
   { tail -n 60 -- "$work"/*-"$name".log >&2; exit 1; }
}
log "building GRUB i386-pc"; build_platform pc pc i386
log "building GRUB x86_64-efi"; build_platform efi efi x86_64
log "building GRUB i386-efi"; build_platform efi32 efi i386
grep -q '^#define QUIET_BOOT 1' "$work/build-pc/config.h" || die "quiet boot not enabled (pc)"
grep -q '^#define QUIET_BOOT 1' "$work/build-efi/config.h" || die "quiet boot not enabled (efi)"
grep -q '^#define QUIET_BOOT 1' "$work/build-efi32/config.h" || die "quiet boot not enabled (efi32)"
if strings -a "$GRUB_OUT/pc/lib/grub/i386-pc/kernel.img" | grep -q 'Welcome to GRUB'; then
  die "i386-pc kernel still contains the welcome banner"
fi
python3 - "$GRUB_OUT/build-info.json" "$GRUB_DEB_VERSION" "$patch_hash" <<'EOF'
import json, sys, hashlib, pathlib
out, ver, ph = sys.argv[1:]
root = pathlib.Path(out).parent
files = {}
for p in sorted(root.glob('*/lib/grub/*/kernel.img')):
    files[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
json.dump({'debian_source': f'grub2 {ver}', 'configure': ['--enable-quiet-boot'],
           'patch_set_sha256_12': ph, 'kernel_img_sha256': files}, open(out, 'w'), indent=2)
EOF
touch "$GRUB_OUT/.complete"
