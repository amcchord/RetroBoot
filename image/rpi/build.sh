#!/bin/bash
# RetroBoot Raspberry Pi 4/5 and Zero 2 W SD image build (root, Debian 13,
# arm64 through qemu-user binfmt). Also reachable as `image/build.sh --board rpi ...`.
#
#   image/rpi/build.sh [--flavor debug|product] [--private | --guest ID | --embed-machine DIR]
#                      [--model pi4-5|pi4|pi5|zero2w] [--host-only] [--no-xz]
#   image/rpi/build.sh --flavor F --root-only    the universal installer's arm64 live root
#                      (image/build.sh --universal): stops after the root and the boot
#                      partition tree ($RB_WORK_ROOT/work/rpi-<flavor>/{rootfs,boot/bootfs});
#                      no SD-card image, no machine, no manifest
#
# Outputs in $RB_WORK_ROOT/out/<build-id>-rpi-<flavor>[-private]/:
#   retroboot-<ver>-rpi-<flavor>[-private].img            raw GPT disk for an SD card/USB SSD
#   retroboot-<ver>-rpi-<flavor>[-private].img.xz         the same, for Raspberry Pi Imager/Etcher
#   retroboot-<ver>-rpi-<flavor>[-private].img{,.xz}.sha256
#   retroboot-<ver>-rpi-<flavor>[-private].manifest.json  packages, components, boot files, hashes
# One image boots both boards (kernel8.img, 4 KiB pages): see image/README.md "Raspberry Pi".
# No installer ISO and no Software Update payload (updates are unavailable on the Pi).
#
# Inputs besides image/: RB_WORK_ROOT (default /srv/retroboot/wave6/rpi-image), the
# components staged there by image/lab/stage_components.py (RB_BUILDER_ROOT=...),
# RB_ARTIFACTS (firmware/drivers/emulator packages; default $RB_WORK_ROOT/artifacts),
# RB_QEMU_PACKAGE (default: "package" in $RB_HANDOFF/arm-emu.json, else linux-ppc-r18-arm64).
# RB_BUILD_ID (optional) replaces the generated build id (the universal build gives both
# of its roots its own).
set -euo pipefail
umask 022
export RB_WORK_ROOT=${RB_WORK_ROOT:-/srv/retroboot/wave6/rpi-image}
here=$(cd "$(dirname "$0")" && pwd)
. "$here/scripts/rpi-lib.sh"
require_root

flavor=debug; machine_dir=; guest=; private_default=0; do_xz=1; root_only=0; host_only=0
export RB_RPI_MODEL=${RB_RPI_MODEL:-pi4-5}
while [ $# -gt 0 ]; do
  case "$1" in
    --board) [ "$2" = rpi ] || die "this is the Raspberry Pi build"; shift 2 ;;
    --model) RB_RPI_MODEL=$2; shift 2 ;;
    --flavor) flavor=$2; shift 2 ;;
    --embed-machine) machine_dir=$(readlink -f "$2"); shift 2 ;;
    --private) private_default=1; shift ;;
    --guest) guest=$2; shift 2 ;;
    --no-xz) do_xz=0; shift ;;
    --root-only) root_only=1; shift ;;
    --host-only) host_only=1; shift ;;
    --no-iso|--no-update|--no-img) shift ;;   # accepted for symmetry with image/build.sh
    *) die "unknown option $1" ;;
  esac
done
case "$flavor" in debug|product) ;; *) die "flavor must be debug or product" ;; esac
case "$RB_RPI_MODEL" in pi4-5|pi4|pi5|zero2w) ;; *) die "model must be pi4-5, pi4, pi5 or zero2w" ;; esac
if [ "$private_default" = 1 ]; then
  [ -z "$guest$machine_dir" ] || die "use either --private, --guest or --embed-machine"
  if [ "$RB_RPI_MODEL" = zero2w ]; then
    guest=${RB_RPI_ZERO_GUEST:-$(cfg guest_zero2w_default)}
    [ -n "$guest" ] || die "Zero 2 W: use --embed-machine with a Quadra 800 base, --guest with a registered 68k fixture, or set RB_RPI_ZERO_GUEST"
  else guest=$(cfg guest_default); fi
fi
[ "$root_only" = 0 ] || [ -z "$guest$machine_dir" ] || die "--root-only builds no machine into the root"
[ "$root_only" = 0 ] || [ "$RB_RPI_MODEL" != zero2w ] || die "Zero 2 W builds a ready-to-flash SD image, not the universal installer"
if [ -n "$guest" ]; then
  [ -z "$machine_dir" ] || die "use either --private/--guest or --embed-machine"
  machine_dir=$("$IMAGE_SRC/scripts/resolve-guest.sh" "$guest")
fi
[ "$host_only" = 0 ] || [ -z "$machine_dir" ] || die "--host-only conflicts with an embedded Mac"
version=$(cfg version)
model_suffix=rpi; [ "$RB_RPI_MODEL" = pi4-5 ] || model_suffix=rpi-$RB_RPI_MODEL
suffix=$model_suffix-$flavor; machine_id=
if [ -n "$machine_dir" ]; then
  [ -f "$machine_dir/disk.img" ] || die "$machine_dir has no disk.img"
  python3 "$here/scripts/sd_profile.py" profile "$RB_RPI_MODEL" --machine "$machine_dir" >/dev/null
  suffix=$model_suffix-$flavor-private; machine_id=$(basename "$machine_dir")
fi
[ -z "${RB_RPI_LAB_AUTH:-}" ] || [ -n "$machine_dir" ] || die "RB_RPI_LAB_AUTH is allowed only in a private image with an embedded Mac"
factory_macs=0
factory_manifest=()
if [ "$RB_RPI_MODEL" = zero2w ] && [ "$flavor" = product ] && [ -z "$machine_dir" ]; then
  if [ "$host_only" = 0 ]; then
    factory_macs=1
    factory_manifest=(--factory-catalog "$here/config/zero2w-machines.json")
  else
    suffix=$suffix-host-only
  fi
fi
src_rev=$(cat "$IMAGE_SRC/.source-revision" 2>/dev/null || echo unknown)
build_id=${RB_BUILD_ID:-$version-$model_suffix-$(date -u +%Y%m%dT%H%M%SZ)-${src_rev:0:8}}
# The flavor is part of the output directory: builds started in the same second would
# otherwise share one directory (and its build.log).
out=$RB_WORK_ROOT/out/$build_id-$suffix; work=$RB_WORK_ROOT/work/$suffix
if [ "$root_only" = 1 ]; then
  mkdir -p "$work"   # the caller (image/build.sh --universal) keeps the log and the outputs
  log "RetroBoot Raspberry Pi live root $build_id ($suffix, root only)"
else
  mkdir -p "$out" "$work"
  exec > >(tee -a "$out/build.log") 2>&1
  log "RetroBoot Raspberry Pi image build $build_id ($suffix)"
fi

export RB_ARTIFACTS=${RB_ARTIFACTS:-$RB_WORK_ROOT/artifacts}
handoff=${RB_HANDOFF:-/srv/retroboot/wave6/handoff}
if [ "$RB_RPI_MODEL" = zero2w ]; then
  # No PPC binary or firmware is staged. Runtime policy also rejects imported PPC Macs.
  RB_QEMU_PACKAGE=none
elif [ -z "${RB_QEMU_PACKAGE:-}" ]; then
  pkg=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("package",""))' "$handoff/arm-emu.json" 2>/dev/null || true)
  RB_QEMU_PACKAGE=$(basename "${pkg:-linux-ppc-r18-arm64}")
fi
export RB_QEMU_PACKAGE
if [ "$RB_RPI_MODEL" != zero2w ]; then
  [ -d "$RB_ARTIFACTS/qemu/$RB_QEMU_PACKAGE/bin" ] || die "emulator package $RB_ARTIFACTS/qemu/$RB_QEMU_PACKAGE missing"
  file -L "$RB_ARTIFACTS/qemu/$RB_QEMU_PACKAGE/bin/qemu-system-ppc" | grep -q 'ARM aarch64' ||
    die "$RB_QEMU_PACKAGE is not an aarch64 build"
fi
# Both images must offer 68k Macs; an incomplete artifact staging is a build failure.
export RB_REQUIRE_M68K=1

# The Pi image is only useful with the supervisor and the presenter (no stand-in here).
for c in hostd presenter classic-ui; do
  [ -f "$RB_WORK_ROOT/src/components/$c/component.json" ] || die "component $c not staged (image/lab/stage_components.py)"
done
export RB_BUILD_CHROOT
RB_BUILD_CHROOT=$("$here/scripts/build-env.sh")
"$here/scripts/make-boot.sh" "$work/boot"
"$IMAGE_SRC/scripts/fetch-components.sh" "$work/components"
python3 - "$work/components/qemu-m68k/component.json" <<'PY'
import json, sys
features = json.load(open(sys.argv[1])).get('features') or []
required = {'nubus-qfb-snapshot-mode', 'q800-t2-hold-slow-host'}
missing = required - set(features)
if missing:
    sys.exit('Raspberry Pi images require linux-m68k-r4-arm64 or newer; missing fixes: ' + ', '.join(sorted(missing)))
PY
"$here/scripts/build-rootfs.sh" "$flavor" "$work/base.tar" "$work/components/runtime-packages.txt"
RB_BOARD=rpi "$IMAGE_SRC/scripts/customize-rootfs.sh" "$flavor" "$work/base.tar" "$work/rootfs" "$work/components" "$work/boot" "$build_id"
"$here/scripts/customize-rpi.sh" "$flavor" "$work/rootfs" "$work/boot"
if [ "$root_only" = 1 ]; then
  echo "$build_id" > "$work/build-id"
  log "arm64 root ready: $work/rootfs, boot tree $work/boot/bootfs"
  exit 0
fi

img=$out/retroboot-$version-$suffix.img
data_mib=$(rcfg data_mib); [ -n "$machine_dir" ] && data_mib=$(rcfg data_mib_private)
"$here/scripts/make-disk.sh" "$work/rootfs" "$work/boot" "$img" "$data_mib" "$machine_dir" "$machine_id" "$factory_macs"
"$here/scripts/check-image.sh" "$img" "$work/rootfs" "$factory_macs" > "$out/check.txt" || { cat "$out/check.txt"; die "image check failed"; }
cat "$out/check.txt" >&2
(cd "$out" && sha256sum "$(basename "$img")" > "$(basename "$img").sha256")
artifacts=(--artifact "$img")
if [ "$do_xz" = 1 ]; then
  log "compressing (xz)"
  xz -T4 -6 -k -f "$img"
  (cd "$out" && sha256sum "$(basename "$img").xz" > "$(basename "$img").xz.sha256")
  artifacts+=(--artifact "$img.xz")
fi
python3 "$IMAGE_SRC/scripts/make-manifest.py" --out "$out/retroboot-$version-$suffix.manifest.json" \
  --build-id "$build_id" --flavor "$flavor" --private "$([ -n "$machine_dir" ] && echo 1 || echo 0)" \
  --rootfs "$work/rootfs" --packages "$work/packages-$flavor.tsv" --boot "$work/boot/boot.json" \
  --source-revision "$src_rev" --machine-dir "$machine_dir" --guest-id "$guest" "${factory_manifest[@]}" "${artifacts[@]}"
ln -sfn "$out" "$RB_WORK_ROOT/out/latest-$suffix"
log "done: $out"
ls -lsh "$out" >&2
