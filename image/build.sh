#!/bin/bash
# RetroBoot appliance image build (runs as root on the builder, Debian 13).
#
#   image/build.sh [--flavor debug|product] [--private] [--guest ID]... [--embed-machine DIR]...
#                  [--no-img] [--no-iso] [--no-update] [--universal]
#   image/build.sh --board rpi ...   Raspberry Pi 4/5 SD-card image (image/rpi/build.sh)
#   image/build.sh --board gemini ...  Planet Computers Gemini PDA root tarball, boot.img and
#                                      installer (image/gemini/build.sh)
#
# Outputs in $RB_WORK_ROOT/out/<build-id>/:
#   retroboot-<ver>-<flavor>[-private].img            raw GPT disk (sparse)
#   retroboot-installer-<ver>-<flavor>[-private].iso  hybrid UEFI/BIOS live installer
#   retroboot-<ver>-<flavor>[-private].manifest.json  packages, components, hashes
#   update-<flavor>/{manifest.json,root-files.json.gz,root.sqfs}  the Software Update
#        payload of this same root (ARCHITECTURE 13); image/lab/sign_update.py signs it
#        on the Mac and assembles retroboot-update-<ver>-<flavor>.rbu here
#   with --universal also (ARCHITECTURE 8.1, scripts/make-universal.sh):
#   retroboot-installer-<ver>-universal-<flavor>[-private].img  one image for a Raspberry Pi
#        4/5 SD card or USB drive and a PC USB stick (UEFI and BIOS), for Etcher/Imager/dd,
#        with .sha256 and universal-check.{txt,json} (scripts/universal.py check). The arm64 live root comes
#        from image/rpi/build.sh --root-only with this build id, its own cache
#        (RB_ARM64_CACHE, default $RB_WORK_ROOT/cache-arm64) and artifacts (RB_ARTIFACTS_ARM64,
#        default $RB_WORK_ROOT/artifacts-arm64: qemu/<aarch64 package>, ndrv, classicvirtio-N,
#        rbdisplay-N; RB_QEMU_PACKAGE_ARM64). Add --no-img --no-iso --no-update for only it.
# --private embeds the default guest base (image.conf guest_default: the full
# Mac OS 9.2.1 install, fixture os921-full-v4); --guest ID embeds another registered
# fixture (e.g. classicmac-os9-921-v2, the minimal os9-original disk kept for
# regression tests); both verify the disk's SHA-256 first (scripts/resolve-guest.sh).
# --embed-machine DIR embeds an unregistered machine directory as is.
# --guest and --embed-machine may be repeated (and combined with --private): every
# machine goes in, in the order given, under its directory's name as machine id (ids
# must differ). The installer lists them all, checked; the first one starts first.
# Any of them makes a *private* flavor: each machine directory is copied sparsely
# into the image's RBDATA/machines/ and into the ISO's
# /usr/lib/retroboot/installer/embedded/machines/<id>/. Source directories are only
# read. Private outputs never leave the lab. make-iso.sh's RB_ISO_* variables add
# private installer files (Wi-Fi, RetroBoot downloads sign-in).
set -euo pipefail
# Modes of everything the build creates (the root directory itself, overlay
# directories) must not depend on the caller: a build started under umask 077
# produced a root file system whose / was 0700, and on the installed N5105 no
# service user could reach anything (QEMU: "Permission denied").
umask 022
here=$(cd "$(dirname "$0")" && pwd)
# Raspberry Pi 4/5 and the Gemini PDA (arm64): separate build routes, image/rpi/build.sh
# and image/gemini/build.sh.
if [ "${1:-}" = --board ]; then
  case "${2:-}" in
    rpi) shift 2; exec "$here/rpi/build.sh" "$@" ;;
    rpi-zero2w) shift 2; exec "$here/rpi/build.sh" --model zero2w "$@" ;;
    gemini) shift 2; exec "$here/gemini/build.sh" "$@" ;;
    x86) shift 2 ;;
    *) echo "unknown board ${2:-} (x86, rpi, rpi-zero2w or gemini)" >&2; exit 1 ;;
  esac
fi
. "$here/scripts/lib.sh"
require_root

flavor=debug; specs=(); do_img=1; do_iso=1; do_update=1; universal=0
while [ $# -gt 0 ]; do
  case "$1" in
    --flavor) flavor=$2; shift 2 ;;
    --embed-machine) specs+=("dir:$(readlink -f "$2")"); shift 2 ;;
    --private) specs+=("guest:$(cfg guest_default)"); shift ;;
    --guest) specs+=("guest:$2"); shift 2 ;;
    --no-img) do_img=0; shift ;;
    --no-iso) do_iso=0; shift ;;
    --no-update) do_update=0; shift ;;
    --universal) universal=1; shift ;;
    *) die "unknown option $1" ;;
  esac
done
case "$flavor" in debug|product) ;; *) die "flavor must be debug or product" ;; esac
version=$(cfg version)
# Embedded machines in the order given: MACHINE_DIR MACHINE_ID pairs, and the manifest's
# --machine-dir/--guest-id lists (a registered fixture's id, or empty).
machines=(); manifest_machines=(); ids=" "
for spec in "${specs[@]}"; do
  case "$spec" in
    guest:*) g=${spec#guest:}; dir=$("$here/scripts/resolve-guest.sh" "$g") ;;
    dir:*) g=; dir=${spec#dir:} ;;
  esac
  [ -f "$dir/disk.img" ] || die "$dir has no disk.img"
  id=$(basename "$dir")
  case "$ids" in *" $id "*) die "machine id '$id' is embedded twice" ;; esac
  ids="$ids$id "
  machines+=("$dir" "$id")
  manifest_machines+=(--machine-dir "$dir" --guest-id "$g")
done
suffix=$flavor; private=0
if [ ${#machines[@]} -gt 0 ]; then
  suffix=$flavor-private; private=1
fi
src_rev=$(cat "$IMAGE_SRC/.source-revision" 2>/dev/null || echo unknown)
build_id=$version-$(date -u +%Y%m%dT%H%M%SZ)-${src_rev:0:8}
out=$RB_WORK_ROOT/out/$build_id; work=$RB_WORK_ROOT/work/$suffix
mkdir -p "$out" "$work"
exec > >(tee -a "$out/build.log") 2>&1
log "RetroBoot image build $build_id ($suffix)"

arm_work=
if [ "$universal" = 1 ]; then
  # The Raspberry Pi live root with the same build id (no machine: the Macs are shared
  # through the universal medium's embedded.squashfs). Its own cache: the presenter build
  # cache is keyed by source revision only, so x86 and arm64 builds must not share one.
  log "arm64 live root (image/rpi/build.sh --root-only)"
  RB_WORK_ROOT=$RB_WORK_ROOT RB_BUILD_ID=$build_id CACHE=${RB_ARM64_CACHE:-$RB_WORK_ROOT/cache-arm64} \
    RB_ARTIFACTS=${RB_ARTIFACTS_ARM64:-$RB_WORK_ROOT/artifacts-arm64} RB_QEMU_PACKAGE=${RB_QEMU_PACKAGE_ARM64:-} \
    "$here/rpi/build.sh" --flavor "$flavor" --root-only
  arm_work=$RB_WORK_ROOT/work/rpi-$flavor
  [ "$(cat "$arm_work/build-id")" = "$build_id" ] || die "arm64 root is not build $build_id"
fi
"$here/scripts/build-grub.sh" >/dev/null
"$here/scripts/make-boot.sh" "$work/boot"
# RB_BOOT_PAYLOAD_FROM=DIR (a previous release's /usr/lib/retroboot/boot): keep that release's
# GRUB images.  An update copies them onto the EFI partition when it commits, so a point release
# built elsewhere can keep its predecessor's instead of shipping images no device has booted.
# Accepted only when DIR's files match its boot.json and everything but the GRUB images is
# byte for byte this build's payload.
if [ -n "${RB_BOOT_PAYLOAD_FROM:-}" ]; then
  python3 - "$RB_BOOT_PAYLOAD_FROM" "$work/boot" <<'PY' || die "RB_BOOT_PAYLOAD_FROM: not usable"
import hashlib, json, os, sys
prev, cur = sys.argv[1:]
GRUB = {'esp/EFI/BOOT/BOOTX64.EFI', 'esp/EFI/retroboot/grubx64.efi', 'i386-pc/core.img', 'i386-pc/grub-bios-setup'}
sha = lambda p: hashlib.sha256(open(p, 'rb').read()).hexdigest()
walk = lambda d: sorted(os.path.relpath(os.path.join(b, n), d) for b, _, ns in os.walk(d) for n in ns)
pj, cj = json.load(open(os.path.join(prev, 'boot.json'))), json.load(open(os.path.join(cur, 'boot.json')))
pf, cf = pj.get('files_sha256') or {}, cj.get('files_sha256') or {}
for rel, digest in pf.items():
    if sha(os.path.join(prev, rel)) != digest:
        sys.exit(f'{rel}: not as its boot.json says')
if walk(prev) != walk(cur) or set(pf) != set(cf):
    sys.exit('the payloads hold different files')
if {k: v for k, v in pj.items() if k != 'files_sha256'} != {k: v for k, v in cj.items() if k != 'files_sha256'}:
    sys.exit('boot.json differs beyond its file hashes')
for rel in pf:
    if rel not in GRUB and pf[rel] != cf[rel]:
        sys.exit(f'{rel} differs from this build')
kept = sorted(r for r in GRUB if r in pf and pf[r] != cf[r])
print(', '.join(kept) or 'nothing (identical)')
PY
  rm -rf "$work/boot"; cp -a "$RB_BOOT_PAYLOAD_FROM" "$work/boot"; chown -R 0:0 "$work/boot"
  log "boot payload: GRUB images kept from $RB_BOOT_PAYLOAD_FROM"
fi
"$here/scripts/fetch-components.sh" "$work/components"
"$here/scripts/build-rootfs.sh" "$flavor" "$work/base.tar" "$work/components/runtime-packages.txt"
"$here/scripts/customize-rootfs.sh" "$flavor" "$work/base.tar" "$work/rootfs" "$work/components" "$work/boot" "$build_id"

img=; iso=
if [ "$do_img" = 1 ]; then
  img=$out/retroboot-$version-$suffix.img
  data_mib=$(cfg data_mib); [ "$private" = 1 ] && data_mib=$(cfg data_mib_private)
  "$here/scripts/make-disk.sh" "$work/rootfs" "$work/boot" "$img" "$data_mib" "${machines[@]}"
fi
if [ "$do_iso" = 1 ]; then
  iso=$out/retroboot-installer-$version-$suffix.iso
  "$here/scripts/make-iso.sh" "$work/rootfs" "$work/boot" "$iso" "$build_id" "${machines[@]}"
fi
uimg=
if [ "$universal" = 1 ]; then
  uimg=$out/retroboot-installer-$version-universal-$suffix.img
  "$here/scripts/make-universal.sh" "$work/rootfs" "$work/boot" "$arm_work/rootfs" "$arm_work/boot" "$uimg" "$build_id" \
    "${machines[@]}"
  python3 "$here/scripts/universal.py" check "$uimg" --rpi-config "$arm_work/boot/config.txt" --deep \
    --json "$out/universal-check.json" > "$out/universal-check.txt" || { cat "$out/universal-check.txt" >&2; die "universal image check failed"; }
  cat "$out/universal-check.txt" >&2
  (cd "$out" && sha256sum "$(basename "$uimg")" > "$(basename "$uimg").sha256")
fi
if [ "$do_update" = 1 ]; then
  log "update bundle payload (unsigned)"
  python3 "$here/scripts/make-update.py" build "$work/rootfs" "$work/boot/boot.json" "$out/update-$flavor" \
    --build-id "$build_id" --flavor "$flavor" --version "$version" --min-version "$(cfg update_min_version)" \
    --source-revision "$src_rev" --notes "${RB_UPDATE_NOTES:-}"
fi
release_dev_binds "$work/rootfs"     # device-node stand-ins, if this builder needed them
python3 "$here/scripts/make-manifest.py" --out "$out/retroboot-$version-$suffix.manifest.json" \
  --build-id "$build_id" --flavor "$flavor" --private "$private" \
  --rootfs "$work/rootfs" --packages "$work/packages-$flavor.tsv" --boot "$work/boot/boot.json" \
  --source-revision "$src_rev" "${manifest_machines[@]}" \
  ${img:+--artifact "$img"} ${iso:+--artifact "$iso"} ${uimg:+--artifact "$uimg"} \
  ${arm_work:+--arm64-rootfs "$arm_work/rootfs" --arm64-packages "$arm_work/packages-$flavor.tsv" --arm64-boot "$arm_work/boot/boot.json"}
ln -sfn "$out" "$RB_WORK_ROOT/out/latest-$suffix"
log "done: $out"
ls -lsh "$out" >&2
