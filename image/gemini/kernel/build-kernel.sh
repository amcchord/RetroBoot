#!/bin/bash
# Build RetroBoot's Gemini PDA kernel on rb-builder (Debian 13 x86_64, as root).
#
# Reproduces atzandrew/gemini-nixos's kernel derivation (devices/planet-geminipda/
# kernel/default.nix) without Nix: the published linux-6.6.157 tarball, upstream's
# file-tree delta copied over it, the CONSYS ROM patches staged for
# CONFIG_EXTRA_FIRMWARE, upstream's two postPatch seds, mobile-nixos's -Werror
# removal, upstream's lean config plus RetroBoot's fragment (config.retroboot,
# cmdline), cross-compiled with Debian's aarch64-linux-gnu GCC.
#
# Output: $WORK/out/<build-id>/ with Image.gz, the DTB, Image.gz-dtb (the payload LK
# expects), modules-<ver>.tar.zst (usr/lib/modules/<ver>), firmware-gemini-nixos-*.tar.zst
# (usr/lib/firmware), config, System.map, kernel.release, manifest.json and build.log.
#
# Usage: build-kernel.sh [--build-id ID] [--jobs N] [--keep-tree] [--variant NAME]
#   --variant NAME also applies variants/NAME/*.patch and variants/NAME/config, runs
#   variants/NAME/check-src.sh on the patched tree and variants/NAME/check-dtb.py on
#   the built DTB, suffixes the build id with -NAME and links out/kernel-latest-NAME
#   instead of out/kernel-latest. variants/NAME/inherit lists variants applied first.
# Environment: RB_GEMINI_WORK (default /srv/retroboot/wave7/gemini-kernel),
#   RB_SOURCE_REVISION (RetroBoot commit the scripts came from),
#   RB_CMDLINE_EXTRA (words appended to CONFIG_CMDLINE, recorded in the manifest),
#   SHIP_SRAMLDO=1 (also ship sramldo-smc.ko).
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
. "$HERE/pins.conf"

WORK=${RB_GEMINI_WORK:-/srv/retroboot/wave7/gemini-kernel}
JOBS=$(nproc)
BUILD_ID=""
KEEP_TREE=0
VARIANT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --variant) VARIANT=$2; shift 2 ;;
    --build-id) BUILD_ID=$2; shift 2 ;;
    --jobs) JOBS=$2; shift 2 ;;
    --keep-tree) KEEP_TREE=1; shift ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

SRC_REV=${RB_SOURCE_REVISION:-unknown}
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
[ -n "$BUILD_ID" ] || BUILD_ID="${LINUX_VERSION}-gemini-${STAMP}-${SRC_REV:0:8}${VARIANT:+-$VARIANT}"
VDIRS=""	# the variant and everything it inherits, parents first
add_variant() {
  local d=$HERE/variants/$1 p
  [ -d "$d" ] || { echo "no variant $d" >&2; exit 2; }
  if [ -f "$d/inherit" ]; then
    for p in $(cat "$d/inherit"); do add_variant "$p"; done
  fi
  case " $VDIRS " in *" $d "*) ;; *) VDIRS="$VDIRS $d" ;; esac
}
[ -z "$VARIANT" ] || add_variant "$VARIANT"
vfiles() {  # vfiles PATTERN -> matching files of every variant dir, in order
  local d f
  for d in $VDIRS; do for f in $d/$1; do [ -e "$f" ] && echo "$f"; done; done
}
CACHE=$WORK/cache
UP=$WORK/upstream/gemini-nixos
B=$WORK/build/$BUILD_ID
S=$B/src
O=$B/obj
STAGE=$B/stage
OUT=$WORK/out/$BUILD_ID

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }
sha() { sha256sum "$1" | cut -d' ' -f1; }

[ ! -e "$OUT" ] || die "output $OUT already exists"
mkdir -p "$CACHE" "$B" "$OUT" "$WORK/upstream"
exec > >(tee -a "$OUT/build.log") 2>&1
log "build $BUILD_ID (RetroBoot $SRC_REV) in $B"

for t in "${CROSS_COMPILE}gcc" make bc bison flex depmod modinfo zstd dtc python3 git curl cpio patch; do
  command -v "$t" >/dev/null || die "missing tool $t (see README: builder packages)"
done

# ---- Hardware safety gate (upstream CORE RULE 5) ---------------------------------
# The mediatek DRM/DSI/panel stack re-drives the NT36672/SSD2092 panel and can damage
# it. The display must stay LK-initialised plus geminipda-drm. hard_ban: never =y/=m.
# Any CONFIG_DRM_MEDIATEK* symbol is banned too (they all depend on DRM_MEDIATEK).
# y_ban: TPS65132 (panel bias) may only be =m (upstream's blessed, inert state).
HARD_BAN="CONFIG_DRM_MEDIATEK CONFIG_MTK_MMSYS CONFIG_PHY_MTK_MIPI_DSI CONFIG_PHY_MTK_HDMI"
Y_BAN="CONFIG_REGULATOR_TPS65132"
safety_gate() {  # safety_gate FILE LABEL
  local f=$1 bad=""
  for s in $HARD_BAN; do
    if grep -Eq "^${s}=(y|m)\$" "$f"; then bad="$bad $s"; fi
  done
  bad="$bad $( (grep -Eo '^CONFIG_DRM_MEDIATEK[A-Z0-9_]*=(y|m)$' "$f" || true) | cut -d= -f1 | tr '\n' ' ')"
  for s in $Y_BAN; do
    if grep -Eq "^${s}=y\$" "$f"; then bad="$bad $s=y"; fi
  done
  bad=$(echo $bad)
  [ -z "$bad" ] || die "SAFETY GATE ($2): banned display symbols enabled: $bad"
  log "safety gate ($2): pass"
}

# ---- Inputs ----------------------------------------------------------------------
if [ ! -d "$UP/.git" ]; then
  log "cloning $UPSTREAM_REPO"
  git clone -q --no-checkout "$UPSTREAM_REPO" "$UP"
fi
if ! git -C "$UP" cat-file -e "$UPSTREAM_REV^{commit}" 2>/dev/null; then
  git -C "$UP" fetch -q origin "$UPSTREAM_REV" || git -C "$UP" fetch -q origin
fi
git -C "$UP" checkout -q --detach "$UPSTREAM_REV"
[ "$(git -C "$UP" rev-parse HEAD)" = "$UPSTREAM_REV" ] || die "upstream checkout is not $UPSTREAM_REV"
[ -z "$(git -C "$UP" status --porcelain --untracked-files=no)" ] || die "upstream checkout $UP has local changes"
UP_DATE=$(git -C "$UP" log -1 --format=%cI)
SOURCE_DATE_EPOCH=$(git -C "$UP" log -1 --format=%ct)
export SOURCE_DATE_EPOCH
DEV=$UP/$UPSTREAM_DEVICE_DIR
DELTA=$DEV/kernel/delta
UPCONFIG=$DEV/kernel/config
[ -d "$DELTA" ] && [ -f "$UPCONFIG" ] || die "upstream layout changed: $DELTA / $UPCONFIG missing"

TARBALL=$CACHE/$(basename "$LINUX_URL")
if [ ! -f "$TARBALL" ]; then
  log "fetching $LINUX_URL"
  curl -fsSL -o "$TARBALL.part" "$LINUX_URL"
  mv "$TARBALL.part" "$TARBALL"
fi
[ "$(sha "$TARBALL")" = "$LINUX_SHA256" ] || die "$TARBALL sha256 mismatch (want $LINUX_SHA256)"
log "inputs: upstream $UPSTREAM_REV ($UP_DATE), linux $LINUX_VERSION sha256 ok"

safety_gate "$UPCONFIG" "upstream config"

# ---- Source tree: base + delta + firmware ----------------------------------------
log "assembling source tree"
rm -rf "$S" "$O" "$STAGE"
mkdir -p "$S" "$O" "$STAGE"
tar -xzf "$TARBALL" -C "$S" --strip-components=1
DELTA_FILES=$(cd "$DELTA" && find . -type f | wc -l)
DELTA_TREE_SHA=$(cd "$DELTA" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1)
DELTA_NEW=$(cd "$DELTA" && find . -type f | while read -r f; do [ -e "$S/$f" ] || echo x; done | wc -l)
cp -a "$DELTA/." "$S/"
mkdir -p "$S/firmware"
for fw in ROMv3_patch_1_0_hdr.bin ROMv3_patch_1_1_hdr.bin; do
  cp "$UP/$UPSTREAM_FIRMWARE_DIR/$fw" "$S/firmware/"
done
log "delta: $DELTA_FILES files ($DELTA_NEW new), tree sha256 $DELTA_TREE_SHA"

# ---- RetroBoot patches on top of the delta (patches/*.patch; GPL kernel source) -------
PATCHES_JSON=""
for p in "$HERE"/patches/*.patch $(vfiles '*.patch'); do
  [ -f "$p" ] || continue
  patch -p1 -d "$S" --forward --no-backup-if-mismatch -s < "$p" || die "patch $(basename "$p") does not apply"
  PATCHES_JSON="$PATCHES_JSON\"$(basename "$p")\":\"$(sha "$p")\","
  log "patch: $(basename "$p")"
done
for c in $(vfiles check-src.sh); do
  log "source check: ${c#$HERE/}"
  sh "$c" "$S" || die "source check ${c#$HERE/} failed"
done

# ---- postPatch (upstream kernel/default.nix) -------------------------------------
# 1. mediatek-connectivity: anchor the vendor -I$(src) include paths on $(srctree)
#    so the O= build finds osal_typedef.h and friends.
n_conn=$( (grep -rl --include='Makefile*' -F -- '-I$(src)' "$S/drivers/misc/mediatek-connectivity" || true) | wc -l)
find "$S/drivers/misc/mediatek-connectivity" -name 'Makefile*' -exec sed -i 's|-I\$(src)|-I\$(srctree)/\$(src)|g' {} +
[ "$( (grep -rl --include='Makefile*' -F -- '-I$(src)' "$S/drivers/misc/mediatek-connectivity" || true) | wc -l)" = 0 ] || die "mediatek-connectivity include sed incomplete"
# 2. gpio-aw9523b (keyboard matrix expander) needs GPIOLIB_IRQCHIP, which the lean
#    config no longer gets selected by another driver.
sed -i '/AWINIC AW9523B I2C GPIO expander/,+5 { s|select REGMAP_I2C|select REGMAP_I2C\n\tselect GPIOLIB_IRQCHIP| }' "$S/drivers/gpio/Kconfig"
grep -A7 'AWINIC AW9523B I2C GPIO expander' "$S/drivers/gpio/Kconfig" | grep -q 'select GPIOLIB_IRQCHIP' || die "aw9523b GPIOLIB_IRQCHIP sed did not apply"
# 3. mobile-nixos enableRemovingWerror (upstream sets it): drop -Werror from every
#    Makefile and Kbuild (vendor drivers carry their own -Werror).
n_werror=0
while IFS= read -r -d '' mf; do
  if grep -q -- '-Werror' "$mf"; then
    sed -i -e 's/-Werror-/-W/g' -e 's/-Werror=/-W/g' -e 's/-Werror//g' "$mf"
    n_werror=$((n_werror + 1))
  fi
done < <(find "$S" -type f \( -name Makefile -o -name Kbuild \) -print0)
log "postPatch: mediatek-connectivity include anchors ($n_conn files), aw9523b GPIOLIB_IRQCHIP, -Werror removed from $n_werror files"

# ---- Configuration ---------------------------------------------------------------
MAKE=(make -C "$S" O="$O" ARCH="$KARCH" CROSS_COMPILE="$CROSS_COMPILE" LOCALVERSION=
      KBUILD_BUILD_USER=retroboot KBUILD_BUILD_HOST=retroboot-builder KBUILD_BUILD_VERSION=1
      "KBUILD_BUILD_TIMESTAMP=$(date -u -d "@$SOURCE_DATE_EPOCH")" -j"$JOBS")
CMDLINE=$(tr -s ' \n' ' ' < "$HERE/cmdline" | sed 's/^ //; s/ $//')
if [ -n "${RB_CMDLINE_EXTRA:-}" ]; then CMDLINE="$CMDLINE $RB_CMDLINE_EXTRA"; fi
case "$CMDLINE" in *'"'*|*'\'*) die "cmdline must not contain quotes or backslashes" ;; esac
[ ${#CMDLINE} -lt 2048 ] || die "cmdline too long"

cp "$UPCONFIG" "$O/.config"
FRAG=$B/config.fragment
{ cat "$HERE/config.retroboot"; for c in $(vfiles config); do cat "$c"; done
  printf 'CONFIG_CMDLINE="%s"\nCONFIG_CMDLINE_FORCE=y\n' "$CMDLINE"; } > "$FRAG"
# Upstream config, then Mobile NixOS's structured config as upstream's Nix build appends
# it, then RetroBoot's fragment; later lines win.
"$S/scripts/kconfig/merge_config.sh" -m -O "$O" "$O/.config" "$HERE/config.mobile-nixos" "$FRAG" >/dev/null
"${MAKE[@]}" olddefconfig >/dev/null
# Every fragment line must hold in the final config.
while IFS= read -r line; do
  case "$line" in
    CONFIG_*=*) grep -qxF -- "$line" "$O/.config" || die "config fragment not honoured: $line (got: $(grep -E "^(# )?${line%%=*}[= ]" "$O/.config" || echo absent))" ;;
    "# CONFIG_"*" is not set") sym=${line#\# }; sym=${sym%% *}
      if grep -Eq "^${sym}=" "$O/.config"; then die "config fragment not honoured: $sym must be unset"; fi ;;
  esac
done < "$FRAG"
safety_gate "$O/.config" "final config"
# Structured-config lines olddefconfig could not honour (unmet dependencies; the Nix
# validator only warns about these). RetroBoot's own overrides are excluded.
MNX_UNMET=""
while IFS= read -r line; do
  case "$line" in CONFIG_*=*) ;; *) continue ;; esac
  sym=${line%%=*}; val=${line#*=}
  grep -Eq "^(# )?${sym}[= ]" "$FRAG" && continue
  if [ "$val" = n ]; then
    if grep -Eq "^${sym}=" "$O/.config"; then MNX_UNMET="$MNX_UNMET $sym=n"; fi
  elif ! grep -qxF -- "$line" "$O/.config"; then
    MNX_UNMET="$MNX_UNMET $sym=$val"
  fi
done < "$HERE/config.mobile-nixos"
MNX_UNMET=$(echo $MNX_UNMET)
log "mobile-nixos structured config: $(echo $MNX_UNMET | wc -w) lines not honoured by olddefconfig${MNX_UNMET:+: $MNX_UNMET}"
# Record how the final config differs from upstream's (toolchain lines included).
diff -u "$UPCONFIG" "$O/.config" > "$OUT/config.diff-from-upstream" || true
log "config: $(grep -c '^[+-]CONFIG_\|^[+-]# CONFIG_' "$OUT/config.diff-from-upstream" || true) changed lines vs upstream (see config.diff-from-upstream)"

# ---- Build -------------------------------------------------------------------------
log "building Image.gz, dtbs and modules with $("${CROSS_COMPILE}gcc" --version | head -1)"
"${MAKE[@]}" Image.gz dtbs modules
KREL=$(cat "$O/include/config/kernel.release")
[ "$KREL" = "$LINUX_VERSION" ] || die "kernel.release is '$KREL', expected $LINUX_VERSION"
safety_gate "$O/include/config/auto.conf" "built auto.conf"
DTB=$O/arch/$KARCH/boot/dts/$DTB_PATH
[ -f "$DTB" ] || die "DTB $DTB_PATH was not built"
# Both Gemini touch variants must be enabled in the one DTB (patches/0001).
dtc -I dtb -O dts -q -o "$B/dtb.dts" "$DTB"
dts_status() {  # status of the first node named $1 in the built DTB ("okay" when absent)
  awk -v n="$1" '
    $1 == n && $2 == "{" { f = 1; st = "okay"; next }
    f && $1 == "status" { st = $3; gsub(/[";]/, "", st) }
    f && $1 == "};" { print st; exit }' "$B/dtb.dts"
}
for n in touchscreen@62 touchscreen@53; do
  st=$(dts_status "$n")
  [ "$st" = okay ] || die "DTB: $n status is '${st:-missing}', want okay"
done
log "DTB: touchscreen@62 (Novatek) and touchscreen@53 (SSD2092) both okay"
for c in $(vfiles check-dtb.py); do
  python3 "$c" "$DTB" >> "$OUT/dtb-check.txt" 2>&1 || { cat "$OUT/dtb-check.txt"; die "DTB check ${c#$HERE/} failed"; }
done
[ ! -f "$OUT/dtb-check.txt" ] || tail -n 1 "$OUT/dtb-check.txt"

log "modules_install"
"${MAKE[@]}" INSTALL_MOD_PATH="$STAGE" INSTALL_MOD_STRIP=1 DEPMOD=/sbin/depmod modules_install >/dev/null
MODDIR=$STAGE/lib/modules/$KREL
rm -f "$MODDIR/build" "$MODDIR/source"

# ---- Out-of-tree modules -------------------------------------------------------------
OOT_JSON=""
VERMAGIC_WANT="$KREL SMP preempt mod_unload aarch64"
build_oot() {  # build_oot NAME SHIP(0|1)
  local name=$1 ship=$2 dir=$B/oot/$1 ko vm
  [ -d "$DEV/kernel/modules/$name" ] || die "upstream module $name missing"
  rm -rf "$dir"; mkdir -p "$B/oot"; cp -r "$DEV/kernel/modules/$name" "$dir"
  local src_sha
  src_sha=$(cd "$dir" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1)
  # Kbuild/Makefile with plain obj-m: build against the full tree (Module.symvers exists).
  "${MAKE[@]}" M="$dir" modules >/dev/null
  ko=$dir/$name.ko
  [ -f "$ko" ] || die "$name.ko not built"
  vm=$(modinfo -F vermagic "$ko")
  case "$vm" in "$VERMAGIC_WANT"*) ;; *) die "$name.ko vermagic '$vm' != '$VERMAGIC_WANT'" ;; esac
  "${CROSS_COMPILE}strip" --strip-debug "$ko"   # as INSTALL_MOD_STRIP=1 does for in-tree modules
  if [ "$ship" = 1 ]; then
    install -D -m 0644 "$ko" "$MODDIR/extra/$name.ko"
  fi
  install -D -m 0644 "$ko" "$OUT/oot/$name.ko"
  log "out-of-tree $name: vermagic ok, shipped=$ship"
  OOT_JSON="$OOT_JSON{\"name\":\"$name\",\"shipped\":$([ "$ship" = 1 ] && echo True || echo False),\"source_tree_sha256\":\"$src_sha\",\"ko_sha256\":\"$(sha "$ko")\",\"vermagic\":\"$vm\"},"
}
for m in $OOT_MODULES_SHIPPED; do build_oot "$m" 1; done
for m in $OOT_MODULES_BUILD_ONLY; do build_oot "$m" "${SHIP_SRAMLDO:-0}"; done
/sbin/depmod -a -b "$STAGE" "$KREL"
for f in modules.dep modules.alias modules.builtin; do [ -s "$MODDIR/$f" ] || die "depmod output $f missing"; done
# Banned display modules must not exist in the tree either.
banned_ko=$(find "$MODDIR" \( -name 'mediatek-drm*.ko' -o -name 'mtk-mmsys*.ko' -o -name 'mtk_mmsys*.ko' -o -name 'phy-mtk-mipi-dsi*.ko' -o -name 'phy-mtk-hdmi*.ko' \) | tr '\n' ' ')
[ -z "$banned_ko" ] || die "SAFETY GATE (modules): banned display modules built: $banned_ko"
NMOD=$(find "$MODDIR" -name '*.ko' | wc -l)

# ---- Outputs ---------------------------------------------------------------------------
log "writing outputs to $OUT"
cp "$O/arch/$KARCH/boot/Image.gz" "$OUT/Image.gz"
cp "$DTB" "$OUT/$(basename "$DTB_PATH")"
cp "$B/dtb.dts" "$OUT/mt6797-gemini-pda.dts"	# dtc -I dtb -O dts of the shipped DTB
cat "$OUT/Image.gz" "$OUT/$(basename "$DTB_PATH")" > "$OUT/Image.gz-dtb"
cp "$O/.config" "$OUT/config"
cp "$O/System.map" "$OUT/System.map"
echo "$KREL" > "$OUT/kernel.release"
cp "$FRAG" "$OUT/config.fragment"
# Debian 13 roots are merged-usr (/lib -> usr/lib) and GNU tar replaces a directory
# symlink by default, so the tarball uses the canonical usr/lib/modules path.
TARFLAGS=(--sort=name --owner=0 --group=0 --numeric-owner --mtime="@$SOURCE_DATE_EPOCH")
mkdir -p "$STAGE/usr/lib"
mv "$STAGE/lib/modules" "$STAGE/usr/lib/modules"
MODTAR=$OUT/modules-$KREL.tar.zst
tar -C "$STAGE" "${TARFLAGS[@]}" -cf - usr/lib/modules | zstd -q -19 -T0 -o "$MODTAR"
# Generic Wi-Fi firmware from the same pinned upstream commit, for the image (not in
# Git: upstream has no licence file). WIFI_factory.bin is deliberately left out: it is
# upstream's own unit's factory NVRAM record (MAC + TX calibration); each Gemini needs
# its own record from its nvdata partition.
FWSTAGE=$B/fwstage/usr/lib/firmware
mkdir -p "$FWSTAGE/rtw88"
for fw in WMT_SOC.cfg ROMv3_patch_1_0_hdr.bin ROMv3_patch_1_1_hdr.bin WIFI_RAM_CODE_6797; do
  install -m 0644 "$UP/$UPSTREAM_FIRMWARE_DIR/$fw" "$FWSTAGE/$fw"
done
install -m 0644 "$UP/$UPSTREAM_FIRMWARE_DIR/rtw8821c_fw.bin" "$FWSTAGE/rtw88/rtw8821c_fw.bin"
FWTAR=$OUT/firmware-gemini-nixos-${UPSTREAM_REV:0:8}.tar.zst
tar -C "$B/fwstage" "${TARFLAGS[@]}" -cf - usr/lib/firmware | zstd -q -19 -T0 -o "$FWTAR"
IMAGE_SIZE=$(stat -c%s "$O/arch/$KARCH/boot/Image")
IMAGE_HDR_SIZE=$(od -An -tu8 -j16 -N8 "$O/arch/$KARCH/boot/Image" | tr -d ' ')
PAYLOAD=$(stat -c%s "$OUT/Image.gz-dtb")
# LK: kernel at 0x40200000, DTB copied to 0x44000000 → the kernel's whole image
# (text..bss, Image header image_size) must end below the tags.
[ $((0x40200000 + IMAGE_HDR_SIZE)) -lt $((0x44000000)) ] || die "kernel image_size $IMAGE_HDR_SIZE overlaps LK's tags at 0x44000000"
# Upstream's budget (docs/mobile-nixos-port-feasibility.md): decompressed ≤ 28 MiB in LK's 50 MiB buffer.
[ "$IMAGE_SIZE" -le $((28 * 1024 * 1024)) ] || die "decompressed Image is $IMAGE_SIZE bytes, over upstream's 28 MiB LK budget"
[ "$PAYLOAD" -lt $((15 * 1024 * 1024)) ] || die "kernel payload $PAYLOAD bytes leaves under 1 MiB for the initrd in the 16 MiB boot partition"

script_sha() { sha "$HERE/$1"; }
fw_json=""
for f in "$UP/$UPSTREAM_FIRMWARE_DIR"/*; do
  fw_json="$fw_json\"$(basename "$f")\":\"$(sha "$f")\","
done
out_json=""
for f in Image.gz "$(basename "$DTB_PATH")" Image.gz-dtb "modules-$KREL.tar.zst" "$(basename "$FWTAR")" config System.map; do
  out_json="$out_json\"$f\":{\"sha256\":\"$(sha "$OUT/$f")\",\"size\":$(stat -c%s "$OUT/$f")},"
done
python3 - "$OUT/manifest.json" <<PY
import json, sys
m = {
  "kind": "retroboot-gemini-kernel",
  "build_id": "$BUILD_ID",
  "created_utc": "$STAMP",
  "retroboot_revision": "$SRC_REV",
  "scripts": {"build-kernel.sh": "$(script_sha build-kernel.sh)", "pins.conf": "$(script_sha pins.conf)",
              "config.retroboot": "$(script_sha config.retroboot)", "cmdline": "$(script_sha cmdline)",
              "config.mobile-nixos": "$(script_sha config.mobile-nixos)"},
  "upstream": {"repo": "$UPSTREAM_REPO", "rev": "$UPSTREAM_REV", "commit_date": "$UP_DATE",
               "config_sha256": "$(sha "$UPCONFIG")",
               "delta": {"files": $DELTA_FILES, "new_files": $DELTA_NEW, "tree_sha256": "$DELTA_TREE_SHA"},
               "firmware_sha256": {${fw_json%,}},
               "extra_firmware_built_in": ["ROMv3_patch_1_0_hdr.bin", "ROMv3_patch_1_1_hdr.bin"],
               "firmware_tarball": "$(basename "$FWTAR")",
               "firmware_excluded": {"WIFI_factory.bin": "upstream unit's own factory NVRAM record"}},
  "linux": {"version": "$LINUX_VERSION", "url": "$LINUX_URL", "sha256": "$LINUX_SHA256"},
  "retroboot_patches": {${PATCHES_JSON%,}},
  "post_patch": {"mediatek_connectivity_include_files": $n_conn, "aw9523b_gpiolib_irqchip": True,
                 "werror_removed_files": $n_werror},
  "config": {"fragment_sha256": "$(sha "$FRAG")", "final_sha256": "$(sha "$O/.config")",
             "cmdline": $(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$CMDLINE"),
             "cmdline_extra": $(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "${RB_CMDLINE_EXTRA:-}"),
             "cmdline_force": True, "safety_gate": "pass",
             "mobile_nixos_structured_config": {"rev": "2c132754323fc1915e8d21dcfc0ef68ab084c6fb",
                                                "not_honoured": "$MNX_UNMET".split()}},
  "toolchain": {"gcc": "$("${CROSS_COMPILE}gcc" --version | head -1)",
                "binutils": "$("${CROSS_COMPILE}ld" --version | head -1)",
                "make": "$(make --version | head -1)", "dtc": "$(dtc --version | head -1)",
                "source_date_epoch": $SOURCE_DATE_EPOCH},
  "kernel_release": "$KREL",
  "variant": "$VARIANT",
  "image": {"uncompressed_size": $IMAGE_SIZE, "header_image_size": $IMAGE_HDR_SIZE, "payload_size": $PAYLOAD},
  "modules": {"count": $NMOD, "tarball": "modules-$KREL.tar.zst", "root": "usr/lib/modules/$KREL",
              "out_of_tree": [${OOT_JSON%,}]},
  "outputs": {${out_json%,}},
}
json.dump(m, open(sys.argv[1], "w"), indent=2, sort_keys=True)
PY
( cd "$OUT" && sha256sum Image.gz "$(basename "$DTB_PATH")" Image.gz-dtb "modules-$KREL.tar.zst" "$(basename "$FWTAR")" config System.map > SHA256SUMS )
if [ "$KEEP_TREE" = 0 ]; then rm -rf "$B"; fi
ln -sfn "$BUILD_ID" "$WORK/out/kernel-latest${VARIANT:+-$VARIANT}"
log "done: $OUT (payload $PAYLOAD bytes, $NMOD modules, kernel $KREL)"
