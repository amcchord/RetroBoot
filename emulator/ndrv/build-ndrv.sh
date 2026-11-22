#!/usr/bin/env bash
# Build RetroBoot's variant of ClassicMac's Power Mac video driver
# (qemu_vga.ndrv) from the pinned ClassicMac sources.
#
#   emulator/ndrv/build-ndrv.sh CLASSICMAC_CHECKOUT RETRO68_TOOLCHAIN OUT_DIR
#
# CLASSICMAC_CHECKOUT is a clone at the commit pinned in
# emulator/manifests/sources.lock.json (prepare_sources.py leaves one in its
# PORT_WORK). RETRO68_TOOLCHAIN contains bin/powerpc-apple-macos-gcc and
# bin/MakePEF. The script first rebuilds the unmodified driver and refuses to
# continue unless it is bit-identical to the pinned qemu_vga.ndrv, so the
# patched output differs from the pinned binary only by the RetroBoot patch.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$1"; TOOLCHAIN="$2"; OUT="$3"
PINNED_SHA=3b687b15b4ed321eaee6822438be5bba55a5ab1d8a3ef948fe8f63996a7c5722
EXPECTED_SHA=34b4fd3444c1dfcf7e87398d5c5cdd39b6d76d331b01d8061ae9c7a88d15a0f1
sha() { if command -v sha256sum >/dev/null; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }
[ "$(sha "$SRC/ppcvid/qemu_vga.ndrv")" = "$PINNED_SHA" ] || { echo "ClassicMac checkout is not at the pinned driver" >&2; exit 1; }
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/gxmetal/guest" "$WORK/ppcvid"
cp -R "$SRC/ppcvid/driver" "$WORK/ppcvid/"
cp -R "$SRC/gxmetal/guest/include" "$WORK/gxmetal/guest/"
cp -R "$SRC/gxmetal/protocol" "$WORK/gxmetal/"
export PATH="$TOOLCHAIN/bin:$PATH"
CDEFS="-DTYPE_BOOL -Dbool=_Bool -Dtrue=1 -Dfalse=0 -std=gnu99 -Wall -Wno-multichar -Wno-unused-variable -I../../gxmetal/guest/include -I../../gxmetal/protocol"
( cd "$WORK/ppcvid/driver" && rm -rf bin && make -s -f GNUmakefile bin/qemu_vga.ndrv >/dev/null )
[ "$(sha "$WORK/ppcvid/driver/bin/qemu_vga.ndrv")" = "$PINNED_SHA" ] || { echo "Unmodified rebuild is not reproducible with this toolchain" >&2; exit 1; }
( cd "$WORK" && patch -s -p1 < "$HERE/retroboot-boot-mode-default.patch" )
( cd "$WORK" && patch -s -p1 < "$HERE/retroboot-builtin-panel.patch" )
( cd "$WORK" && patch -s -p1 < "$HERE/retroboot-vbl-60hz.patch" )
( cd "$WORK/ppcvid/driver" && rm -rf bin && make -s -f GNUmakefile CDEFS="$CDEFS -DRETROBOOT_PREFER_BOOT_MODE -DRETROBOOT_BUILTIN_PANEL -DRETROBOOT_VBL_60HZ" bin/qemu_vga.ndrv >/dev/null )
mkdir -p "$OUT"
cp "$WORK/ppcvid/driver/bin/qemu_vga.ndrv" "$OUT/qemu_vga.ndrv"
GOT="$(sha "$OUT/qemu_vga.ndrv")"
echo "qemu_vga.ndrv $GOT"
[ "$GOT" = "$EXPECTED_SHA" ] || echo "warning: output differs from the recorded RetroBoot build $EXPECTED_SHA" >&2
