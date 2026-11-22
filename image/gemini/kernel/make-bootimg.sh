#!/bin/bash
# Build the Gemini PDA boot.img (Android boot image v0) for LK's p22 `boot` slot and
# verify it with check-bootimg.py.
#
# Geometry measured by upstream from the verified bring-up image (devices/
# planet-geminipda/default.nix): base 0x40000000, kernel +0x00200000, second
# +0x00f00000, ramdisk +0x05000000, tags +0x04000000, page 2048. The kernel payload is
# Image.gz with the DTB appended (LK gunzips it and scans the last 2 MiB for the FDT).
# The header cmdline must carry bootopt=64S3,32N2,64N2: LK parses it, and without it the
# boot hangs on the logo. The kernel itself ignores the field (CONFIG_CMDLINE_FORCE);
# LK copies it into /chosen/bootargs, where userspace can read extra words.
#
# Usage: make-bootimg.sh [--kernel-dir DIR] [--initrd FILE] [--extra-cmdline "words"] [--out DIR]
#                        [--link NAME]   (out/boot/NAME symlink, default "latest")
#   Defaults: $WORK/out/kernel-latest and $WORK/out/initrd/latest/initrd.cpio.gz; the
#   output goes to $WORK/out/boot/<kernel-build>+<initrd-build>/.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
WORK=${RB_GEMINI_WORK:-/srv/retroboot/wave7/gemini-kernel}
KDIR=$WORK/out/kernel-latest
INITRD=$WORK/out/initrd/latest/initrd.cpio.gz
EXTRA=""
OUTDIR=""
LINK=latest
while [ $# -gt 0 ]; do
  case "$1" in
    --link) LINK=$2; shift 2 ;;
    --kernel-dir) KDIR=$2; shift 2 ;;
    --initrd) INITRD=$2; shift 2 ;;
    --extra-cmdline) EXTRA=$2; shift 2 ;;
    --out) OUTDIR=$2; shift 2 ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }
sha() { sha256sum "$1" | cut -d' ' -f1; }
command -v mkbootimg >/dev/null || die "mkbootimg missing (apt install mkbootimg)"

KDIR=$(readlink -f "$KDIR")
INITRD=$(readlink -f "$INITRD")
IDIR=$(dirname "$INITRD")
PAYLOAD=$KDIR/Image.gz-dtb
DTB=$KDIR/mt6797-gemini-pda.dtb
[ -f "$PAYLOAD" ] && [ -f "$DTB" ] && [ -f "$KDIR/manifest.json" ] || die "$KDIR is not a build-kernel.sh output"
[ -f "$INITRD" ] || die "initrd $INITRD missing"
KBUILD=$(basename "$KDIR")
IBUILD=$(basename "$IDIR")
[ -n "$OUTDIR" ] || OUTDIR=$WORK/out/boot/$KBUILD+$IBUILD
mkdir -p "$OUTDIR"

# The verified on-device image's field, plus optional words for userspace.
CMDLINE="bootopt=64S3,32N2,64N2 log_buf_len=4M"
[ -z "$EXTRA" ] || CMDLINE="$CMDLINE $EXTRA"
[ ${#CMDLINE} -lt 512 ] || die "header cmdline over 511 bytes"

IMG=$OUTDIR/boot.img
mkbootimg --header_version 0 \
  --kernel "$PAYLOAD" --ramdisk "$INITRD" \
  --base 0x40000000 --kernel_offset 0x00200000 --second_offset 0x00f00000 \
  --ramdisk_offset 0x05000000 --tags_offset 0x04000000 --pagesize 2048 \
  --cmdline "$CMDLINE" \
  --output "$IMG"
# AOSP mkbootimg ≥ 11 writes second_addr = 0 when there is no second stage; the verified
# bring-up header (and older mkbootimg) carries base + second_offset. The v0 id hashes
# only the blobs and their sizes, so setting the field back keeps the id valid.
python3 - "$IMG" <<'PY'
import struct, sys
p = sys.argv[1]
with open(p, "r+b") as f:
    hdr = f.read(48)
    assert hdr[:8] == b"ANDROID!" and struct.unpack_from("<I", hdr, 24)[0] == 0
    f.seek(28)
    f.write(struct.pack("<I", 0x40000000 + 0x00F00000))
PY

python3 "$HERE/check-bootimg.py" "$IMG" --dtb "$DTB" --initrd "$INITRD" --payload "$PAYLOAD" \
  --json "$OUTDIR/check.json" | tee "$OUTDIR/check.txt"

python3 - "$OUTDIR/manifest.json" "$KDIR/manifest.json" "$IDIR/manifest.json" <<PY
import json, sys
k = json.load(open(sys.argv[2])); i = json.load(open(sys.argv[3]))
json.dump({
  "kind": "retroboot-gemini-bootimg",
  "boot_img": {"path": "$IMG", "sha256": "$(sha "$IMG")", "size": $(stat -c%s "$IMG")},
  "partition": {"partname": "boot", "size": 16 * 1024 * 1024},
  "header_cmdline": "$CMDLINE",
  "kernel_cmdline": k["config"]["cmdline"],
  "kernel": {"build_id": k["build_id"], "dir": "$KDIR", "payload_sha256": "$(sha "$PAYLOAD")",
             "retroboot_revision": k["retroboot_revision"], "upstream_rev": k["upstream"]["rev"]},
  "initrd": {"build_id": i["build_id"], "path": "$INITRD", "sha256": "$(sha "$INITRD")",
             "retroboot_revision": i["retroboot_revision"]},
  "make_bootimg_sha256": "$(sha "$HERE/make-bootimg.sh")",
  "check_bootimg_sha256": "$(sha "$HERE/check-bootimg.py")",
  "mkbootimg": "$(dpkg-query -W -f='${Version}' mkbootimg 2>/dev/null || echo unknown)",
}, open(sys.argv[1], "w"), indent=2, sort_keys=True)
PY
(cd "$OUTDIR" && sha256sum boot.img > SHA256SUMS)
ln -sfn "$(basename "$OUTDIR")" "$WORK/out/boot/$LINK"
log "done: $IMG ($(stat -c%s "$IMG") bytes, sha256 $(sha "$IMG"))"
