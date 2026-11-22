#!/usr/bin/env bash
# Build classicvirtio's 68k NuBus declaration ROM for RetroBoot's Quadra 800
# package (linux-m68k-rN, share/qemu/classicvirtio-68k.rom): the classic DRVRs
# (virtio-block, virtio-9p shared folder, virtio tablet) for
#   qemu-system-m68k -device nubus-virtio-mmio,romfile=.../classicvirtio-68k.rom
# with RetroBoot's shared-folder patches (README.md in this directory).
#
#   emulator/classicvirtio/build-declrom-68k.sh CLASSICVIRTIO_CLONE CLASSICMAC_CHECKOUT RETRO68_TOOLCHAIN OUT_DIR
#
# CLASSICVIRTIO_CLONE is any clone of https://github.com/elliotnunn/classicvirtio that has
# the pinned commit; it is cloned again into a temporary directory and never modified.
# CLASSICMAC_CHECKOUT provides ClassicMac's four classicvirtio patches at the pinned
# revision; it is only read.  RETRO68_TOOLCHAIN contains bin/m68k-apple-macos-gcc with
# Apple's disk-driver headers installed (ClassicMac's vendor/Retro68-build/toolchain);
# it is used read-only.  The script first rebuilds the unmodified ROM and refuses to
# continue unless it is bit-identical to ClassicMac's shared/declrom, so the output
# differs from it only by the RetroBoot patches.
#
# retroboot-location.patch is not applied: it reads hostd's time zone from the Open
# Firmware options in the PowerPC Name Registry, which a 68k Mac does not have.  A
# Quadra keeps Mac OS's own time zone (Date & Time) in its persistent PRAM instead.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CV="$1"; CM="$2"; TOOLCHAIN="$3"; OUT="$4"
COMMIT=fc401b4c731027cb4068f3415def7fe79f6659da
PINNED_SHA=3a3bcea8f642a6206bf0084759c7469938fdaec457a5ebee413c862b2a79dfa9
# After each RetroBoot patch, in order (the last one is the shipped ROM)
PATCHES=(retroboot-folder-dates retroboot-share-cache retroboot-local-time retroboot-folder-finfo retroboot-folder-move retroboot-names)
EXPECTED=(11fad727a92807dacf4640c2166f8bca4b45258b33d94fe6e75e86e1849f4337
          6d2623a9d578fda25d958396558af2bd4bbbb38cfc925ab7b0e11d83c473d629
          503ab0e0a1e0b024813681e17e07b6491fa2d95f160f6d10590646dc353fd587
          f838422d81ec584ebbf1ec5650be82916796dbe6bfcbbcdd94a348025cfcf2bc
          d881526652f7f1de7b93116c2f1b628f6a90e29cb97fc2a66577835243531c6d
          a8c2a6937190d60666b2307eaea09ef5b43a60fd0287759e41ecdf5e16504ddf)
sha() { if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
git clone -q --no-hardlinks "$CV" "$WORK/cv"
git -C "$WORK/cv" checkout -q "$COMMIT"
for p in floppy-driver powermac-boot tools-mount display-aware-input; do
  git -C "$WORK/cv" apply "$CM/classicvirtio/$p.patch"
done
[ -e "$TOOLCHAIN/m68k-apple-macos/include/Disks.h" ] || { echo "Retro68 toolchain without the disk-driver headers" >&2; exit 1; }
export PATH="$TOOLCHAIN/bin:$PATH"
make -s -C "$WORK/cv" build/classic/declrom >/dev/null 2>&1
[ "$(sha "$WORK/cv/build/classic/declrom")" = "$PINNED_SHA" ] || { echo "Unmodified rebuild is not ClassicMac's shared/declrom" >&2; exit 1; }
i=0
for p in "${PATCHES[@]}"; do
  git -C "$WORK/cv" apply "$HERE/$p.patch"
  rm -rf "$WORK/cv/build"
  make -s -C "$WORK/cv" build/classic/declrom >/dev/null 2>&1
  GOT="$(sha "$WORK/cv/build/classic/declrom")"
  echo "+ $p: declrom $GOT"
  [ "$GOT" = "${EXPECTED[$i]}" ] || echo "warning: differs from the recorded RetroBoot build ${EXPECTED[$i]}" >&2
  i=$((i + 1))
done
mkdir -p "$OUT"
cp "$WORK/cv/build/classic/declrom" "$OUT/classicvirtio-68k.rom"
cat > "$OUT/classicvirtio-68k.json" <<EOF
{
  "component": "classicvirtio 68k NuBus declaration ROM (RetroBoot variant)",
  "file": "classicvirtio-68k.rom",
  "sha256": "$GOT",
  "bytes": $(wc -c < "$OUT/classicvirtio-68k.rom" | tr -d ' '),
  "pinned_unmodified_sha256": "$PINNED_SHA",
  "source": "classicvirtio $COMMIT + ClassicMac floppy-driver, powermac-boot, tools-mount, display-aware-input + RetroBoot ${PATCHES[*]}",
  "not_applied": {"retroboot-location": "PowerPC Name Registry only; a Quadra keeps its time zone in PRAM"},
  "license": "MIT (classicvirtio, Elliot Nunn)",
  "features": ["folder-dates", "dir-cache", "finder-refresh", "local-time", "folder-finfo", "folder-move", "all-names"]
}
EOF
echo "classicvirtio-68k.rom $GOT -> $OUT"
