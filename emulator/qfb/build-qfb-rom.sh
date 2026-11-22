#!/usr/bin/env bash
# Build RetroBoot's mac_qfb.rom: the nubus-qfb declaration ROM and 68k video
# driver (Solra Bizna, MIT/Apache-2.0) from ClassicMac's pinned qfb/driver with
# RetroBoot's driver patches, for the Quadra 800 package (linux-m68k-rN,
# share/qemu/mac_qfb.rom).
#
#   emulator/qfb/build-qfb-rom.sh CLASSICMAC_CHECKOUT RETRO68_TOOLCHAIN OUT_DIR
#
# CLASSICMAC_CHECKOUT is a checkout of the pinned ClassicMac revision; only
# qfb/driver is read (copied to a temporary directory).  RETRO68_TOOLCHAIN is a
# Retro68 toolchain with bin/m68k-apple-macos-*; the driver is written against
# Retro68's Multiversal Interfaces, which ClassicMac's toolchain keeps in
# multiversal/CIncludes after installing Apple's headers, so that directory comes
# first on the include path.  Needs lua (the ROM checksum).  The script first
# rebuilds the unmodified driver and refuses to continue unless the ROM is
# bit-identical to ClassicMac's pinned qfb/mac_qfb.rom.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CM="$1"; TOOLCHAIN="$2"; OUT="$3"
PINNED_SHA=1eb349c019d9d985b957e2ee8a39890b1300f10bf4fa948bd5140fd3fb260038
PATCHES=(retroboot-gray-rows)
EXPECTED=(60710f1c9767ca27fe8483251767dd9756f0d8bc81b7a3e1548b60861817f944)
sha() { if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }
command -v lua >/dev/null || { echo "lua is required (ROM checksum)" >&2; exit 1; }
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/qfb"
cp -R "$CM/qfb/driver" "$WORK/qfb/driver"
rm -rf "$WORK/qfb/driver/bin" "$WORK/qfb/driver/obj"
export PATH="$TOOLCHAIN/bin:$PATH"
FLAGS="-Iinclude/ -I$TOOLCHAIN/multiversal/CIncludes -Wall -Wextra -Wno-strict-aliasing -mcpu=68020 -nostartfiles -ffreestanding -fno-exceptions -DSUPPORT_AUX -O3 -mpcrel"
build() { make -s -C "$WORK/qfb/driver" clean >/dev/null; make -s -C "$WORK/qfb/driver" CXXFLAGS="$FLAGS" >/dev/null 2>&1; }
build
[ "$(sha "$WORK/qfb/driver/bin/mac_qfb.rom")" = "$PINNED_SHA" ] || { echo "Unmodified rebuild is not ClassicMac's qfb/mac_qfb.rom" >&2; exit 1; }
git -C "$WORK/qfb" init -q
i=0
for p in "${PATCHES[@]}"; do
  git -C "$WORK/qfb" apply "$HERE/$p.patch"
  build
  GOT="$(sha "$WORK/qfb/driver/bin/mac_qfb.rom")"
  echo "+ $p: mac_qfb.rom $GOT"
  [ "$GOT" = "${EXPECTED[$i]}" ] || echo "warning: differs from the recorded RetroBoot build ${EXPECTED[$i]}" >&2
  i=$((i + 1))
done
mkdir -p "$OUT"
cp "$WORK/qfb/driver/bin/mac_qfb.rom" "$OUT/mac_qfb.rom"
cat > "$OUT/mac_qfb.json" <<EOF
{
  "component": "nubus-qfb declaration ROM and 68k video driver (RetroBoot variant)",
  "file": "mac_qfb.rom",
  "sha256": "$GOT",
  "bytes": $(wc -c < "$OUT/mac_qfb.rom" | tr -d ' '),
  "pinned_unmodified_sha256": "$PINNED_SHA",
  "source": "ClassicMac qfb/driver + RetroBoot ${PATCHES[*]}",
  "license": "MIT or Apache-2.0 (mac_qfb_driver, Solra Bizna)"
}
EOF
echo "mac_qfb.rom $GOT -> $OUT"
