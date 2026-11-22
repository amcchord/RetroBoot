#!/usr/bin/env bash
# Quadra 800 (68040) emulator package for Linux: qemu-system-m68k from
# the same QEMU and ClassicMac pins as the PowerPC package, with the m68k lock's
# own series (emulator/manifests/m68k.lock.json; D044). The PowerPC route
# (build-linux.sh, sources.lock.json) is not involved and not changed.
#
#   PORT_WORK=$PWD/build/linux-m68k-r1 PORT_PACKAGE=$PWD/output/linux-m68k-r1 \
#   PORT_M68K_FIRMWARE=DIR PORT_JOBS=8 ./emulator/scripts/build-linux-m68k.sh
#
# PORT_M68K_FIRMWARE holds the private firmware inputs made outside this
# script, checked against the lock's hashes: mac_qfb.rom (emulator/qfb/
# build-qfb-rom.sh) and classicvirtio-68k.rom (emulator/classicvirtio/
# build-declrom-68k.sh), both Retro68, and q800-pram-seed.img
# (emulator/scripts/make_q800_pram_seed.sh). None is ever committed. They are
# 68k guest code, so the x86 and arm64 packages carry the same files.
#
# PORT_ARCH=arm64 cross-builds the same package for aarch64 Linux (the Gemini PDA,
# the Raspberry Pi) inside the cross chroot of emulator/scripts/arm64/mkchroot.sh,
# as build-linux.sh does for the PowerPC package (emulator/reports/arm64-20261013.md).
set -euo pipefail
PORT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LOCK="$PORT_ROOT/emulator/manifests/m68k.lock.json"
PORT_WORK="${PORT_WORK:-$PORT_ROOT/build/linux-m68k}"
PORT_PACKAGE="${PORT_PACKAGE:-$PORT_ROOT/output/linux-m68k}"
PORT_OPT_CFLAGS="${PORT_OPT_CFLAGS:--O2}"
PORT_ARCH="${PORT_ARCH:-x86_64}"
: "${PORT_M68K_FIRMWARE:?PORT_M68K_FIRMWARE must name the directory with mac_qfb.rom, classicvirtio-68k.rom and q800-pram-seed.img}"
[ "$(uname -s)" = Linux ] && [ "$(uname -m)" = x86_64 ] || {
  echo 'Linux x86_64 is required for this supported build route.' >&2; exit 1;
}
PORT_CONFIGURE_ARCH=()
case "$PORT_ARCH" in
  x86_64) ;;
  arm64)
    # Cross compiler and arm64 libraries come from the cross chroot; the binary runs
    # here only under qemu-user (binfmt), for verify_package_m68k.py.
    command -v aarch64-linux-gnu-gcc >/dev/null && command -v aarch64-linux-gnu-pkg-config >/dev/null || {
      echo 'PORT_ARCH=arm64 needs aarch64-linux-gnu-gcc and -pkg-config (run inside the cross chroot).' >&2; exit 1;
    }
    PORT_CONFIGURE_ARCH=(--cross-prefix=aarch64-linux-gnu-)
    # No fused multiply-add where x86-64 has none (build-linux.sh explains).
    case " $PORT_OPT_CFLAGS " in
      *' -ffp-contract=off '*) ;;
      *) PORT_OPT_CFLAGS="$PORT_OPT_CFLAGS -ffp-contract=off" ;;
    esac ;;
  *) echo 'PORT_ARCH must be x86_64 or arm64' >&2; exit 1 ;;
esac
export PORT_OPT_CFLAGS PORT_M68K_FIRMWARE PORT_ARCH
python3 "$PORT_ROOT/emulator/scripts/prepare_sources.py" --work-dir "$PORT_WORK" --lock "$LOCK"
PORT_SOURCE="$PORT_WORK/qemu"
PORT_BUILD="$PORT_WORK/build"
mkdir -p "$PORT_BUILD" "$PORT_WORK/logs"
# The presenter owns the screen (-display retroboot); VNC stays for headless
# diagnostics. No SDL/GTK window frontends. ALSA, slirp (dp83932 user
# networking) and virtfs (virtio-9p shared folder).
if [ ! -f "$PORT_BUILD/build.ninja" ]; then
  (
    cd "$PORT_BUILD"
    "$PORT_SOURCE/configure" "${PORT_CONFIGURE_ARCH[@]}" --target-list=m68k-softmmu --enable-tcg \
      --enable-slirp --enable-vnc --enable-virtfs --disable-sdl --disable-gtk \
      --audio-drv-list=alsa --disable-docs --disable-guest-agent \
      --disable-cocoa --enable-werror \
      --extra-cflags="$PORT_OPT_CFLAGS" --extra-cxxflags="$PORT_OPT_CFLAGS"
  ) 2>&1 | tee "$PORT_WORK/logs/configure.log"
fi
ninja -C "$PORT_BUILD" -j "${PORT_JOBS:-4}" qemu-system-m68k 2>&1 | tee "$PORT_WORK/logs/build.log"
echo "compiler warnings: $(grep -c 'warning:' "$PORT_WORK/logs/build.log" || true)" | tee -a "$PORT_WORK/logs/build.log"
rm -rf "$PORT_PACKAGE"
mkdir -p "$PORT_PACKAGE/bin" "$PORT_PACKAGE/share/qemu"
cp "$PORT_BUILD/qemu-system-m68k" "$PORT_PACKAGE/bin/"
# share/qemu: only what a Quadra uses. -bios names the machine's own Apple ROM,
# nubus-qfb loads mac_qfb.rom from the -L directory, the virtio card takes its
# declaration ROM by path; keymaps serve VNC diagnostics.
cp -a "$PORT_SOURCE/pc-bios/keymaps" "$PORT_PACKAGE/share/qemu/keymaps"
python3 - "$LOCK" "$PORT_M68K_FIRMWARE" "$PORT_PACKAGE/share/qemu" <<'PY'
import hashlib, json, shutil, sys
from pathlib import Path
lock = json.loads(Path(sys.argv[1]).read_text()); src = Path(sys.argv[2]); dest = Path(sys.argv[3])
for name, spec in lock['firmware'].items():
    f = src / name
    digest = hashlib.sha256(f.read_bytes()).hexdigest()
    if digest != spec['sha256']:
        raise SystemExit(f'{name}: {digest} is not the lock\'s {spec["sha256"]}')
    shutil.copyfile(f, dest / name)
    print(name, digest)
PY
python3 "$PORT_ROOT/emulator/scripts/verify_package.py" "$PORT_PACKAGE" 2>&1 | tee "$PORT_WORK/logs/capabilities.log"
python3 "$PORT_ROOT/emulator/scripts/package_manifest.py" "$PORT_PACKAGE" "$PORT_WORK"
