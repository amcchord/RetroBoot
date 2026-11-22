#!/usr/bin/env bash
# Dedicated QEMU10 OS X runtime. Classic QEMU11 preparation is untouched.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="${PORT_WORK:-$ROOT/build/linux-osx}"
PACKAGE="${PORT_PACKAGE:-$ROOT/output/linux-ppc-osx10}"
python3 "$ROOT/emulator/scripts/prepare_osx_sources.py" --work-dir "$WORK" \
  --cache-dir "${PORT_OSX_SOURCE_CACHE:-$ROOT/build/osx-source-cache}"
mkdir -p "$WORK/build" "$WORK/logs" "$PACKAGE/bin" "$PACKAGE/share/qemu"
if [ ! -f "$WORK/build/build.ninja" ]; then
  (cd "$WORK/build" && ../qemu/configure --target-list=ppc-softmmu --enable-tcg \
    --enable-slirp --enable-vnc --enable-virtfs --enable-sdl --disable-gtk \
    --audio-drv-list=alsa --disable-docs --disable-guest-agent --disable-cocoa \
    --disable-install-blobs --enable-werror --extra-cflags=-O2) \
    2>&1 | tee "$WORK/logs/configure.log"
fi
ninja -C "$WORK/build" -j "${PORT_JOBS:-4}" qemu-system-ppc qemu-img \
  2>&1 | tee "$WORK/logs/build.log"
# Execute the actual depth completion/dispatch source with real QEMU threads
# under ASan. BQL/VM-stop/GL reset calls are explicitly simulated by this test.
DEPTH_CHECK_DIR="$(mktemp -d "$WORK/depth-check.XXXXXX")"
python3 "$ROOT/research/gfx/tests/test_depth_backend.py" \
  --source "$WORK/qemu" --build "$WORK/build" --work "$DEPTH_CHECK_DIR/run" \
  2>&1 | tee "$WORK/logs/depth-backend.log"
# Actual retained color keys/LRU and failure publication; GL objects simulated.
python3 "$ROOT/research/gfx/tests/test_target_lifetime.py" \
  --source "$WORK/qemu" --build "$WORK/build" --work "$DEPTH_CHECK_DIR/target-lifetime" \
  2>&1 | tee "$WORK/logs/target-lifetime.log"
# Execute the actual selective barrier order and VBE envelope helpers. These
# use independent callback/state models, not simulated GPU or native results.
python3 "$ROOT/research/gfx/tests/test_selective2d_order.py" \
  --source "$WORK/qemu" --work "$DEPTH_CHECK_DIR/selective-order" \
  2>&1 | tee "$WORK/logs/selective-order.log"
python3 -m unittest discover -s "$ROOT/research/gfx/tests" \
  -p test_selective2d_constants.py \
  2>&1 | tee "$WORK/logs/selective-constants.log"
python3 "$ROOT/research/gfx/tests/test_texture_scanout_span.py" \
  --source "$WORK/qemu" \
  2>&1 | tee "$WORK/logs/texture-scanout-span.log"
cp "$WORK/build/qemu-system-ppc" "$WORK/build/qemu-img" "$PACKAGE/bin/"
if [ -n "${PORT_OSX_FIRMWARE_FROM:-}" ]; then
  OSX_FW="$PORT_OSX_FIRMWARE_FROM"
else
  PORT_OPENBIOS_PROFILE=osx bash "$ROOT/emulator/openbios/build-openbios.sh" \
    "$WORK/classicmac" "$WORK/firmware" "$WORK/firmware/out" \
    2>&1 | tee "$WORK/logs/firmware.log"
  OSX_FW="$WORK/firmware/out/openbios-ppc"
fi
python3 - "$ROOT" "$OSX_FW" <<'PY'
import hashlib,json,sys
from pathlib import Path
root,firmware=map(Path,sys.argv[1:]);lock=json.loads((root/'emulator/manifests/osx.lock.json').read_text())
assert hashlib.sha256(firmware.read_bytes()).hexdigest()==lock['firmware']['sha256'],'OS X firmware differs from its source-built lock'
shared=json.loads((root/'emulator/manifests/sources.lock.json').read_text())
assert shared['firmware_build']['openbios']['osx']['staged_sha256']==lock['firmware']['sha256'],'OS X firmware locks differ'
PY
cp "$OSX_FW" "$PACKAGE/share/qemu/openbios-ppc-osx"
# Diagnostic default firmware is the same OS X asset; the product passes -bios.
cp "$OSX_FW" "$PACKAGE/share/qemu/openbios-ppc"
if [ -n "${PORT_OSX_NDRV_FROM:-}" ]; then
  OSX_NDRV="$PORT_OSX_NDRV_FROM"
elif [ -n "${PORT_RETRO68_BIN:-}" ]; then
  python3 "$ROOT/emulator/scripts/build_osx_ndrv.py" \
    --classicmac-source "$WORK/classicmac" --toolchain-bin "$PORT_RETRO68_BIN" \
    --work-dir "$WORK/ndrv" 2>&1 | tee "$WORK/logs/ndrv.log"
  OSX_NDRV="$WORK/ndrv/source/ppcvid/driver/bin/qemu_vga.ndrv"
else
  echo 'Build the pinned OS X NDRV with build_osx_ndrv.py and set PORT_OSX_NDRV_FROM.' >&2
  exit 1
fi
python3 - "$ROOT" "$OSX_NDRV" <<'PY'
import hashlib,json,sys
from pathlib import Path
root,driver=map(Path,sys.argv[1:]);entry=json.loads((root/'emulator/manifests/osx.lock.json').read_text())['ndrv_build']
assert hashlib.sha256(driver.read_bytes()).hexdigest()==entry['sha256'] and driver.stat().st_size==entry['bytes'],'OS X VGA driver differs from its source-built lock'
PY
cp "$OSX_NDRV" "$PACKAGE/share/qemu/qemu_vga.ndrv"
cp -a "$WORK/qemu/pc-bios/keymaps" "$PACKAGE/share/qemu/"
python3 "$ROOT/emulator/scripts/make_macosx_flash_seed.py" "$PACKAGE/share/qemu/macosx-flash-seed.img"
python3 "$ROOT/emulator/scripts/verify_osx_package.py" "$PACKAGE" \
  2>&1 | tee "$WORK/logs/capabilities.log"
python3 "$ROOT/emulator/scripts/osx_package_manifest.py" "$PACKAGE" "$WORK"
