#!/bin/bash
# RetroBoot for the Planet Computers Gemini PDA (runs as root on the x86 builder, Debian 13,
# arm64 through qemu-user binfmt). Also reachable as `image/build.sh --board gemini ...`.
#
#   image/gemini/build.sh [--flavor debug|product] [--private | --guest ID | --embed-machine DIR]
#
# Outputs in $RB_WORK_ROOT/out/<build-id>-gemini-<flavor>[-private]/:
#   retroboot-<ver>-gemini-<flavor>[-private].rootfs.tar.zst   the root for <linux>/retroboot
#   boot.img                     wave7/gemini-kernel's boot.img (kernel + busybox initrd)
#   rb-unzstd                    static aarch64 zstd decompressor for the installer
#   gemini-install.sh            the installer (run as root on the Gemini; --help)
#   SHA256SUMS                   payload hashes (checked by the installer)
#   retroboot-<ver>-gemini-<flavor>[-private].manifest.json   packages, components, kernel
#                                build, upstream pins and hashes, SSH host key (debug)
#   ssh_host_ed25519_key.pub     debug: the host key to pin before the first boot
#   check.txt, inspect.txt, build.log
# The Gemini keeps Gemian: RetroBoot's Debian 13 arm64 root is the directory /retroboot of
# the ext4 file system on PARTNAME=linux, which the boot.img's initrd bind-mounts as /.
# No disk image, no installer ISO, no Software Update payload. See image/gemini/README.md.
#
# Inputs besides image/: RB_WORK_ROOT (default /srv/retroboot/wave7/gemini-image), the
# components staged there by image/lab/stage_components.py (RB_BUILDER_ROOT=...),
# RB_ARTIFACTS (firmware/drivers/emulator packages; default $RB_WORK_ROOT/artifacts),
# RB_QEMU_PACKAGE (default: "package" in $RB_HANDOFF/arm-emu.json, else linux-ppc-r18-arm64),
# RB_KERNEL_HANDOFF (default: gemini.conf kernel_handoff), RB_GEMINI_WIFI_DIR (private
# Wi-Fi profiles, default /srv/retroboot/wave7/private/wifi), RB_GEMINI_LAB_AUTH (private:
# the RetroBoot downloads sign-in, written to /etc/retroboot/lab-auth.env; unset = none).
set -euo pipefail
umask 022
export RB_WORK_ROOT=${RB_WORK_ROOT:-/srv/retroboot/wave7/gemini-image}
here=$(cd "$(dirname "$0")" && pwd)
. "$here/scripts/gemini-lib.sh"
require_root

flavor=debug; machine_dir=; guest=
while [ $# -gt 0 ]; do
  case "$1" in
    --board) [ "$2" = gemini ] || die "this is the Gemini build"; shift 2 ;;
    --flavor) flavor=$2; shift 2 ;;
    --embed-machine) machine_dir=$(readlink -f "$2"); shift 2 ;;
    --private) guest=$(cfg guest_default); shift ;;
    --guest) guest=$2; shift 2 ;;
    --no-iso|--no-update|--no-img|--no-xz) shift ;;   # accepted for symmetry with image/build.sh
    *) die "unknown option $1" ;;
  esac
done
case "$flavor" in debug|product) ;; *) die "flavor must be debug or product" ;; esac
if [ -n "$guest" ]; then
  [ -z "$machine_dir" ] || die "use either --private/--guest or --embed-machine"
  machine_dir=$("$IMAGE_SRC/scripts/resolve-guest.sh" "$guest")
fi
version=$(cfg version)
suffix=gemini-$flavor; machine_id=
if [ -n "$machine_dir" ]; then
  [ -f "$machine_dir/disk.img" ] || die "$machine_dir has no disk.img"
  suffix=gemini-$flavor-private; machine_id=$(basename "$machine_dir")
fi
src_rev=$(cat "$IMAGE_SRC/.source-revision" 2>/dev/null || echo unknown)
build_id=$version-gemini-$(date -u +%Y%m%dT%H%M%SZ)-${src_rev:0:8}
out=$RB_WORK_ROOT/out/$build_id-$suffix; work=$RB_WORK_ROOT/work/$suffix
mkdir -p "$out" "$work"
exec > >(tee -a "$out/build.log") 2>&1
log "RetroBoot Gemini PDA build $build_id ($suffix)"

export RB_ARTIFACTS=${RB_ARTIFACTS:-$RB_WORK_ROOT/artifacts}
handoff=${RB_HANDOFF:-/srv/retroboot/wave6/handoff}
if [ -z "${RB_QEMU_PACKAGE:-}" ]; then
  pkg=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("package",""))' "$handoff/arm-emu.json" 2>/dev/null || true)
  RB_QEMU_PACKAGE=$(basename "${pkg:-linux-ppc-r18-arm64}")
fi
export RB_QEMU_PACKAGE
[ -d "$RB_ARTIFACTS/qemu/$RB_QEMU_PACKAGE/bin" ] || die "emulator package $RB_ARTIFACTS/qemu/$RB_QEMU_PACKAGE missing"
file -L "$RB_ARTIFACTS/qemu/$RB_QEMU_PACKAGE/bin/qemu-system-ppc" | grep -q 'ARM aarch64' ||
  die "$RB_QEMU_PACKAGE is not an aarch64 build"
for c in hostd presenter classic-ui; do
  [ -f "$RB_WORK_ROOT/src/components/$c/component.json" ] || die "component $c not staged (image/lab/stage_components.py)"
done
khandoff_file=${RB_KERNEL_HANDOFF:-$(gcfg kernel_handoff)}
[ -f "$khandoff_file" ] || die "no kernel handoff at $khandoff_file (wave7/gemini-kernel)"

# Boot provenance (the manifest's "boot"); the later stages add to it.
rm -rf "$work/boot"; mkdir -p "$work/boot"
python3 - "$work/boot/boot.json" "$(gcfg root_dir)" "$(gcfg part_boot)" "$(gcfg part_para)" "$(gcfg part_linux)" <<'PY'
import json, sys
out, root_dir, pboot, ppara, plinux = sys.argv[1:]
json.dump({'board': 'gemini', 'boards': ['Planet Computers Gemini PDA (MediaTek MT6797X Helio X27)'],
           'layout': {'root': f'directory {root_dir} of the ext4 file system on PARTNAME={plinux}, bind-mounted as / by the initrd',
                      'boot_partition': f'PARTNAME={pboot} (boot.img)', 'para_partition': f'PARTNAME={ppara}',
                      'fallback': 'Gemian on the same file system; recovery partition holds its boot.img (para=boot-recovery)'}},
          open(out, 'w'), indent=2)
PY

export RB_BUILD_CHROOT
RB_BUILD_CHROOT=$("$IMAGE_SRC/rpi/scripts/build-env.sh")
python3 "$here/scripts/stage-kernel.py" "$khandoff_file" "$work/kernel" "$(gcfg kernel_version)"
"$here/scripts/fetch-upstream.sh" "$work/upstream"
"$here/scripts/build-unzstd.sh" "$work/unzstd"
"$IMAGE_SRC/scripts/fetch-components.sh" "$work/components"
"$here/scripts/build-rootfs.sh" "$flavor" "$work/base.tar" "$work/components/runtime-packages.txt"
RB_BOARD=gemini "$IMAGE_SRC/scripts/customize-rootfs.sh" "$flavor" "$work/base.tar" "$work/rootfs" "$work/components" "$work/boot" "$build_id"
"$here/scripts/customize-gemini.sh" "$flavor" "$work/rootfs" "$work/kernel" "$work/upstream" "$work/boot" "$machine_dir" "$machine_id"

name=retroboot-$version-$suffix
"$here/scripts/check-gemini.sh" "$flavor" "$work/rootfs" "$work/kernel" "$machine_id" > "$out/check.txt" ||
  { cat "$out/check.txt"; die "build check failed"; }
cat "$out/check.txt" >&2
"$here/scripts/make-payload.sh" "$work/rootfs" "$out" "$name" "$work/kernel" "$work/unzstd" "$build_id" "$suffix"
release_dev_binds "$work/rootfs"     # device-node stand-ins, if this builder needed them
[ ! -f "$work/boot/ssh_host_ed25519_key.pub" ] || cp "$work/boot/ssh_host_ed25519_key.pub" "$out/"

manifest=$out/$name.manifest.json
python3 "$IMAGE_SRC/scripts/make-manifest.py" --out "$manifest" \
  --build-id "$build_id" --flavor "$flavor" --private "$([ -n "$machine_dir" ] && echo 1 || echo 0)" \
  --rootfs "$work/rootfs" --packages "$work/packages-$flavor.tsv" --boot "$work/boot/boot.json" \
  --source-revision "$src_rev" --machine-dir "$machine_dir" --guest-id "$guest" \
  --artifact "$out/$name.rootfs.tar.zst" --artifact "$out/boot.img" --artifact "$out/rb-unzstd" \
  --artifact "$out/gemini-install.sh" --artifact "$out/SHA256SUMS"
python3 "$here/scripts/gemini-manifest.py" "$manifest" "$work" "$out"
ln -sfn "$out" "$RB_WORK_ROOT/out/latest-$suffix"
log "done: $out"
ls -lsh "$out" >&2
