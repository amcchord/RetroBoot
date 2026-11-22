#!/usr/bin/env bash
# Run inside the allocated RetroBoot x86 Linux builder checkout.
# PORT_ARCH=arm64 cross-builds the same package for aarch64 Linux (Raspberry Pi 4/5),
# inside the cross chroot made by emulator/scripts/arm64/mkchroot.sh (see README).
set -euo pipefail
PORT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PORT_WORK="${PORT_WORK:-$PORT_ROOT/build/linux}"
PORT_ARCH="${PORT_ARCH:-x86_64}"
export PORT_ARCH
[ "$(uname -s)" = Linux ] && [ "$(uname -m)" = x86_64 ] || {
  echo 'Linux x86_64 is required for this supported build route.' >&2; exit 1;
}
PORT_CONFIGURE_ARCH=()
case "$PORT_ARCH" in
  x86_64) ;;
  arm64)
    # Cross compiler and arm64 libraries come from the cross chroot, never the
    # builder host's own dpkg. The binaries run here only under qemu-user (binfmt).
    command -v aarch64-linux-gnu-gcc >/dev/null && command -v aarch64-linux-gnu-pkg-config >/dev/null || {
      echo 'PORT_ARCH=arm64 needs aarch64-linux-gnu-gcc and -pkg-config (run inside the cross chroot).' >&2; exit 1;
    }
    PORT_CONFIGURE_ARCH=(--cross-prefix=aarch64-linux-gnu-)
    # GCC's default for GNU C is -ffp-contract=fast, which on aarch64 (always FMA)
    # fuses a*b+c and breaks the fast FPU paths' exact error terms. x86-64 without
    # -mfma never contracts, so off here is the x86 package's arithmetic.
    case " ${PORT_OPT_CFLAGS:--O2} " in
      *' -ffp-contract=off '*) ;;
      *) PORT_OPT_CFLAGS="${PORT_OPT_CFLAGS:--O2} -ffp-contract=off" ;;
    esac
    PORT_FIRMWARE="${PORT_FIRMWARE:-copy}" ;;
  *) echo 'PORT_ARCH must be x86_64 or arm64' >&2; exit 1 ;;
esac
python3 "$PORT_ROOT/emulator/scripts/prepare_sources.py" --work-dir "$PORT_WORK"
PORT_SOURCE="$PORT_WORK/qemu"
PORT_BUILD="$PORT_WORK/build"
PORT_PACKAGE="${PORT_PACKAGE:-$PORT_ROOT/output/linux-ppc}"
# Optimization flags are a build input recorded in the package manifest.
# Keep -O2 unless a measured boot/runtime benefit justifies a change.
PORT_OPT_CFLAGS="${PORT_OPT_CFLAGS:--O2}"
export PORT_OPT_CFLAGS
mkdir -p "$PORT_BUILD" "$PORT_PACKAGE/bin" "$PORT_PACKAGE/share/qemu" "$PORT_WORK/logs"
# Exact configure invocation is retained (optimization flags above). ALSA is the
# initial supported Linux backend; none and wav stay available for diagnostics.
# The RetroBoot display backend (-display retroboot) is built on Linux with
# pixman. Qualification is separate.
if [ ! -f "$PORT_BUILD/build.ninja" ]; then
  (
    cd "$PORT_BUILD"
    "$PORT_SOURCE/configure" "${PORT_CONFIGURE_ARCH[@]}" --target-list=ppc-softmmu --enable-tcg \
      --enable-slirp --enable-vnc --enable-virtfs --enable-sdl --disable-gtk \
      --audio-drv-list=alsa --disable-docs --disable-guest-agent \
      --disable-cocoa --enable-werror \
      --extra-cflags="$PORT_OPT_CFLAGS" --extra-cxxflags="$PORT_OPT_CFLAGS"
  ) 2>&1 | tee "$PORT_WORK/logs/configure.log"
fi
ninja -C "$PORT_BUILD" -j "${PORT_JOBS:-4}" qemu-system-ppc qemu-img \
  2>&1 | tee "$PORT_WORK/logs/build.log"
cp "$PORT_BUILD/qemu-system-ppc" "$PORT_BUILD/qemu-img" "$PORT_PACKAGE/bin/"
# Retain complete upstream firmware support files, then the pinned loader.
cp -a "$PORT_SOURCE/pc-bios/." "$PORT_PACKAGE/share/qemu/"
# OpenBIOS: "source" (default, needs root) rebuilds ClassicMac's firmware in its
# pinned toolchain, proves the unmodified rebuild bit-identical to the pinned
# binary, then ships RetroBoot's build (AirPort radio node behind a fw_cfg gate).
# "pinned" keeps ClassicMac's prebuilt binary (no AirPort radio node).
# "copy" (arm64 default) takes the source-built firmware of an x86 package of the
# same lock, PORT_FIRMWARE_FROM, after checking it against that package's manifest
# (OpenBIOS is PowerPC code: the same file serves every host).
PORT_FIRMWARE="${PORT_FIRMWARE:-source}"
export PORT_FIRMWARE
case "$PORT_FIRMWARE" in
  source)
    "$PORT_ROOT/emulator/openbios/build-openbios.sh" "$PORT_WORK/classicmac" "$PORT_WORK/openbios" \
      "$PORT_WORK/openbios/out" 2>&1 | tee "$PORT_WORK/logs/openbios.log"
    cp "$PORT_WORK/openbios/out/openbios-ppc" "$PORT_PACKAGE/share/qemu/openbios-ppc" ;;
  pinned) ;;
  copy)
    : "${PORT_FIRMWARE_FROM:?PORT_FIRMWARE=copy needs PORT_FIRMWARE_FROM (an x86 package built with PORT_FIRMWARE=source)}"
    export PORT_FIRMWARE_FROM
    python3 - "$PORT_FIRMWARE_FROM" "$PORT_ROOT/emulator/manifests/sources.lock.json" <<'PY' 2>&1 | tee "$PORT_WORK/logs/openbios.log"
import hashlib, json, sys
from pathlib import Path
src = Path(sys.argv[1]); m = json.loads((src / 'manifest.json').read_text())
lock = json.loads(Path(sys.argv[2]).read_text())
assert m['build']['openbios'] == 'source', 'PORT_FIRMWARE_FROM is not a source-built firmware package'
# Adding a separate OS X firmware profile does not change the classic asset.
before = dict(m['sources']['firmware_build']['openbios']); before.pop('osx', None)
after = dict(lock['firmware_build']['openbios']); after.pop('osx', None)
assert before == after, 'PORT_FIRMWARE_FROM was built from another classic firmware lock'
digest = hashlib.sha256((src / 'share/qemu/openbios-ppc').read_bytes()).hexdigest()
assert digest == m['files']['share/qemu/openbios-ppc']['sha256'], 'openbios-ppc differs from its manifest'
print('openbios-ppc', digest, 'from', src)
PY
    cp "$PORT_FIRMWARE_FROM/share/qemu/openbios-ppc" "$PORT_PACKAGE/share/qemu/openbios-ppc" ;;
  *) echo "PORT_FIRMWARE must be source, pinned or copy" >&2; exit 1 ;;
esac
# OS X gets its own source-built firmware; classic Macs keep the exact prior
# openbios-ppc.  A copied OS X asset must match the independent locked hash.
PORT_OSX_FW="${PORT_OSX_FIRMWARE_FROM:-${PORT_FIRMWARE_FROM:-$PORT_PACKAGE}/share/qemu/openbios-ppc-osx}"
if [ ! -f "$PORT_OSX_FW" ]; then
  PORT_OPENBIOS_PROFILE=osx "$PORT_ROOT/emulator/openbios/build-openbios.sh" \
    "$PORT_WORK/classicmac" "$PORT_WORK/openbios" "$PORT_WORK/openbios/out-osx" \
    2>&1 | tee "$PORT_WORK/logs/openbios-osx.log"
  PORT_OSX_FW="$PORT_WORK/openbios/out-osx/openbios-ppc"
fi
python3 - "$PORT_OSX_FW" "$PORT_ROOT/emulator/manifests/sources.lock.json" <<'PY'
import hashlib, json, sys
from pathlib import Path
data = Path(sys.argv[1]).read_bytes()
expected = json.loads(Path(sys.argv[2]).read_text())['firmware_build']['openbios']['osx']['staged_sha256']
assert hashlib.sha256(data).hexdigest() == expected, 'OS X firmware differs from its source-built pin'
PY
cp "$PORT_OSX_FW" "$PORT_PACKAGE/share/qemu/openbios-ppc-osx"
python3 "$PORT_ROOT/emulator/scripts/make_macosx_nvram_seed.py" \
  "$PORT_PACKAGE/share/qemu/macosx-nvram-seed.img"
python3 "$PORT_ROOT/emulator/scripts/make_macosx_flash_seed.py" \
  "$PORT_PACKAGE/share/qemu/macosx-flash-seed.img"
python3 "$PORT_ROOT/emulator/scripts/verify_package.py" "$PORT_PACKAGE" \
  2>&1 | tee "$PORT_WORK/logs/capabilities.log"
python3 "$PORT_ROOT/emulator/scripts/package_manifest.py" "$PORT_PACKAGE" "$PORT_WORK"
