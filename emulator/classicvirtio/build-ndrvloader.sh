#!/usr/bin/env bash
# Build RetroBoot's variant of classicvirtio's PowerPC driver loader (ndrvloader):
# the shared volume reports real folder dates, caches directory information
# (the Finder's idle polling no longer reaches the host), tells the Finder
# about host-side changes and converts dates between the host's UTC and Mac OS
# local time in both directions, keeps folders' Finder flags (an invisible
# folder stays invisible), lets Mac OS rename and move folders and shows every
# host name (31 characters, longer ones shortened as "Name#CNID.ext"); the tablet
# driver gives Mac OS the time zone hostd passes as an Open Firmware option
# (emulator/classicvirtio/README.md).
#
#   emulator/classicvirtio/build-ndrvloader.sh CLASSICVIRTIO_CLONE CLASSICMAC_CHECKOUT RETRO68_TOOLCHAIN OUT_DIR
#
# CLASSICVIRTIO_CLONE is any clone of https://github.com/elliotnunn/classicvirtio that has
# the pinned commit; it is cloned again into a temporary directory and never modified.
# CLASSICMAC_CHECKOUT provides ClassicMac's four classicvirtio patches (classicvirtio/*.patch)
# at the revision pinned in emulator/manifests/sources.lock.json; it is only read.
# RETRO68_TOOLCHAIN contains bin/powerpc-apple-macos-gcc and bin/MakePEF.  The script first
# rebuilds the unmodified loader and refuses to continue unless it is bit-identical to the
# pinned share/qemu/ndrvloader, so the output differs from it only by the RetroBoot patches.
# OUT_DIR receives ndrvloader and manifest.json (the overlay hostd verifies, see README.md).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CV="$1"; CM="$2"; TOOLCHAIN="$3"; OUT="$4"
COMMIT=fc401b4c731027cb4068f3415def7fe79f6659da
PINNED_SHA=a5b441048916fdb58291e34ab029cfd06d7e6da9380b6035bd4d8740f040f27d
# After each RetroBoot patch, in order (the last one is the shipped loader)
PATCHES=(retroboot-folder-dates retroboot-share-cache retroboot-local-time retroboot-location retroboot-folder-finfo retroboot-folder-move retroboot-names)
EXPECTED=(03f5d0534c14b1446d5851bd7d4bfa25dc85cf49cf03c1f650ace047afb99848
          176ded2c19ae5d703d41b2d0cc6faf997a318c6c387c436a634c1ce8e587c90d
          0d302d18ef2ce28323522756d99c893afad457128e012ca4c1c44cc0023f3a05
          4813e2978ee3d6316777a036a5b34cc4cfd0963e4ff853ea22820b53078e9168
          0e8d4879dd2f77797b4cc3813c1bb0b00e35874557166bccee8e1ad97fdadc12
          fbb2a144e83e0dbbc331b8b6ff91b462208d59a3c821b0b8f8595d0e85f1b849
          e61ec868e138436c369e091d190be349daf45960c2055507fa5109fdc3174533)
sha() { if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
git clone -q --no-hardlinks "$CV" "$WORK/cv"
git -C "$WORK/cv" checkout -q "$COMMIT"
for p in floppy-driver powermac-boot tools-mount display-aware-input; do
  git -C "$WORK/cv" apply "$CM/classicvirtio/$p.patch"
done
export PATH="$TOOLCHAIN/bin:$PATH"
make -s -C "$WORK/cv" build/ndrv/ndrvloader >/dev/null 2>&1
[ "$(sha "$WORK/cv/build/ndrv/ndrvloader")" = "$PINNED_SHA" ] || { echo "Unmodified rebuild is not the pinned ndrvloader" >&2; exit 1; }
i=0
for p in "${PATCHES[@]}"; do
  git -C "$WORK/cv" apply "$HERE/$p.patch"
  rm -rf "$WORK/cv/build"
  make -s -C "$WORK/cv" build/ndrv/ndrvloader >/dev/null 2>&1
  GOT="$(sha "$WORK/cv/build/ndrv/ndrvloader")"
  echo "+ $p: ndrvloader $GOT"
  [ "$GOT" = "${EXPECTED[$i]}" ] || echo "warning: differs from the recorded RetroBoot build ${EXPECTED[$i]}" >&2
  i=$((i + 1))
done
mkdir -p "$OUT"
cp "$WORK/cv/build/ndrv/ndrvloader" "$OUT/ndrvloader"
cat > "$OUT/manifest.json" <<EOF
{
  "component": "ndrvloader (RetroBoot classicvirtio variant)",
  "sha256": "$GOT",
  "pinned_unmodified_sha256": "$PINNED_SHA",
  "source": "classicvirtio $COMMIT + ClassicMac floppy-driver, powermac-boot, tools-mount, display-aware-input + RetroBoot ${PATCHES[*]}",
  "license": "MIT (classicvirtio, Elliot Nunn)",
  "features": ["folder-dates", "dir-cache", "finder-refresh", "local-time", "mac-location", "folder-finfo", "folder-move", "all-names"]
}
EOF
echo "ndrvloader $GOT -> $OUT"
