#!/usr/bin/env bash
# Build RetroBoot's OpenBIOS (openbios-ppc) from source, reproducibly.
#
#   emulator/openbios/build-openbios.sh CLASSICMAC_CHECKOUT WORK_DIR OUT_DIR
#
# CLASSICMAC_CHECKOUT is the clone at the commit pinned in
# emulator/manifests/sources.lock.json (prepare_sources.py leaves one in its
# PORT_WORK). Everything else comes from the lock's "firmware_build" section:
# the OpenBIOS commit, ClassicMac's OpenBIOS patch, and the exact toolchain
# ClassicMac used (ghcr.io/openbios/openbios-builder, pinned by digest and
# verified layer by layer). Needs root on Linux x86_64: the build runs in a
# chroot of that image under WORK_DIR, never in the host toolchain.
#
# 1. Rebuild the unmodified firmware with ClassicMac's recorded build date and
#    refuse to continue unless it is bit-identical to the pinned openbios-ppc.
# 2. Apply RetroBoot's patches (the runtime-gated AirPort radio node) and build
#    again with RetroBoot's fixed build date.
# 3. Stage it exactly as prepare_sources.py stages the pinned firmware (the
#    OpenBIOS background palette literal is replaced by black).
# OUT_DIR receives openbios-qemu.elf (unstaged) and openbios-ppc (staged).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
SRC="$1"; WORK="$2"; OUT="$3"
[ "$(uname -s)/$(uname -m)" = Linux/x86_64 ] || { echo "Linux x86_64 is required" >&2; exit 1; }
[ "$(id -u)" = 0 ] || { echo "root is required (the build runs in a chroot of the pinned builder image)" >&2; exit 1; }
lock() { python3 -c "import json,sys; d=json.load(open('$ROOT/emulator/manifests/sources.lock.json'))['firmware_build']; print(eval(sys.argv[1]))" "$1"; }
sha() { sha256sum "$1" | cut -d' ' -f1; }
OB_URL="$(lock "d['openbios']['url']")"
OB_COMMIT="$(lock "d['openbios']['commit']")"
CM_PATCH="$SRC/$(lock "d['openbios']['classicmac_patch']['path']")"
CM_PATCH_SHA="$(lock "d['openbios']['classicmac_patch']['sha256']")"
CM_BINARY="$SRC/$(lock "d['openbios']['pinned_reproduction']['binary']")"
PINNED_SHA="$(lock "d['openbios']['pinned_reproduction']['elf_sha256']")"
PINNED_DATE="$(lock "d['openbios']['pinned_reproduction']['build_date']")"
IMAGE="$(lock "d['openbios']['builder_image']['image']")"
DIGEST="$(lock "d['openbios']['builder_image']['digest']")"
EXTRACFLAGS="$(lock "d['openbios']['make']['extracflags']")"
RB_DATE="$(lock "d['openbios']['retroboot']['build_date']")"
RB_ELF_SHA="$(lock "d['openbios']['retroboot']['elf_sha256']")"
RB_STAGED_SHA="$(lock "d['openbios']['retroboot']['staged_sha256']")"
mapfile -t RB_PATCHES < <(lock "'\n'.join(p['path']+' '+p['sha256'] for p in d['openbios']['retroboot']['patches'])")
case "${PORT_OPENBIOS_PROFILE:-classic}" in
  classic) ;;
  osx)
    RB_DATE="$(lock "d['openbios']['osx']['build_date']")"
    RB_ELF_SHA="$(lock "d['openbios']['osx']['elf_sha256']")"
    RB_STAGED_SHA="$(lock "d['openbios']['osx']['staged_sha256']")"
    mapfile -t RB_PATCHES < <(lock "'\n'.join(p['path']+' '+p['sha256'] for p in d['openbios']['retroboot']['patches'] + d['openbios']['osx']['additional_patches'])") ;;
  *) echo 'PORT_OPENBIOS_PROFILE must be classic or osx' >&2; exit 1 ;;
esac

[ "$(sha "$CM_BINARY")" = "$PINNED_SHA" ] || { echo "ClassicMac checkout is not at the pinned openbios-ppc" >&2; exit 1; }
[ "$(sha "$CM_PATCH")" = "$CM_PATCH_SHA" ] || { echo "ClassicMac OpenBIOS patch hash mismatch" >&2; exit 1; }
for entry in "${RB_PATCHES[@]}"; do
  set -- $entry
  [ "$(sha "$ROOT/$1")" = "$2" ] || { echo "RetroBoot OpenBIOS patch hash mismatch: $1" >&2; exit 1; }
done
mkdir -p "$WORK" "$OUT"
WORK="$(cd "$WORK" && pwd)"
ROOTFS="$(python3 "$HERE/oci_rootfs.py" --image "$IMAGE" --digest "$DIGEST" --dest "$WORK/toolchain")"
# OpenBIOS stamps `LC_ALL=C TZ=UTC date +'%b %e %Y %H:%M'` into its banner; this
# shim makes that the recorded value (all other date calls pass through).
cat > "$ROOTFS/usr/local/bin/date" <<'EOF'
#!/bin/sh
if [ "$*" = "+%b %e %Y %H:%M" ]; then echo "${RB_BUILD_DATE:?}"; exit 0; fi
exec /bin/date "$@"
EOF
chmod 755 "$ROOTFS/usr/local/bin/date"
if [ ! -d "$WORK/openbios.git" ]; then
  git init -q --bare "$WORK/openbios.git"
fi
git -C "$WORK/openbios.git" cat-file -e "$OB_COMMIT^{commit}" 2>/dev/null || \
  git -C "$WORK/openbios.git" fetch -q --depth 1 "$OB_URL" "$OB_COMMIT"

build() {  # build DATE PATCH... -> $ROOTFS/src/obj-ppc/openbios-qemu.elf
  local date="$1"; shift
  rm -rf "$ROOTFS/src"; mkdir -p "$ROOTFS/src"
  git -C "$WORK/openbios.git" archive --format=tar "$OB_COMMIT" | tar -x -C "$ROOTFS/src"
  patch -s -p1 -d "$ROOTFS/src" < "$CM_PATCH"
  local p
  for p in "$@"; do patch -s -p1 -d "$ROOTFS/src" < "$p"; done
  chroot "$ROOTFS" env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin HOME=/root \
    RB_BUILD_DATE="$date" EXTRACFLAGS="$EXTRACFLAGS" \
    sh -c 'cd /src && config/scripts/switch-arch ppc >/dev/null && make -j2' > "$WORK/build-$(date +%s%N).log" 2>&1
}

build "$PINNED_DATE"
GOT="$(sha "$ROOTFS/src/obj-ppc/openbios-qemu.elf")"
[ "$GOT" = "$PINNED_SHA" ] || { echo "Unmodified rebuild is not bit-identical to the pinned openbios-ppc ($GOT)" >&2; exit 1; }
echo "unmodified rebuild bit-identical to the pinned openbios-ppc $GOT"

PATCHES=()
for entry in "${RB_PATCHES[@]}"; do set -- $entry; PATCHES+=("$ROOT/$1"); done
build "$RB_DATE" "${PATCHES[@]}"
cp "$ROOTFS/src/obj-ppc/openbios-qemu.elf" "$OUT/openbios-qemu.elf"
cp "$ROOTFS/src/obj-ppc/openbios-qemu.syms" "$OUT/openbios-qemu.syms"
cp "$ROOTFS/src/obj-ppc/openbios-qemu.elf.nostrip" "$OUT/openbios-qemu.elf.nostrip"
python3 - "$OUT/openbios-qemu.elf" "$OUT/openbios-ppc" <<'EOF'
import sys
from pathlib import Path
data = Path(sys.argv[1]).read_bytes()
# Same staging as prepare_sources.py: the console background palette literal.
if data.count(bytes.fromhex('00ffffcc')) != 1:
    raise SystemExit('OpenBIOS palette literal must occur exactly once')
Path(sys.argv[2]).write_bytes(data.replace(bytes.fromhex('00ffffcc'), bytes(4)))
EOF
rm -rf "$ROOTFS/src"
ELF="$(sha "$OUT/openbios-qemu.elf")"; STAGED="$(sha "$OUT/openbios-ppc")"
echo "retroboot openbios-qemu.elf $ELF"
echo "retroboot openbios-ppc (staged) $STAGED"
[ "$ELF" = "$RB_ELF_SHA" ] && [ "$STAGED" = "$RB_STAGED_SHA" ] || \
  echo "warning: output differs from the recorded RetroBoot build ($RB_ELF_SHA / $RB_STAGED_SHA)" >&2
