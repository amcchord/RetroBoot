# RetroBoot on the Gemini PDA: GPT partitions by name and the bootloader's para command.
# POSIX sh, sourced (never executed): by the rb-gemini-* tools on RetroBoot and inlined
# into gemini-install.sh, which runs under Gemian (Debian 9, kernel 3.18). Both kernels
# report a GPT partition's name as PARTNAME= in /sys/class/block/<dev>/uevent, and under
# a 6.6 kernel the eMMC can be mmcblk0 or mmcblk1, so partitions are never found by number.
#
#   rbg_part NAME          the partition named NAME on the internal eMMC (prints /dev/...)
#   rbg_disk_of DEV        the whole disk a partition belongs to (prints e.g. mmcblk0)
#   rbg_para_read DEV      the para command (LK's 32-byte command at offset 0, to the first NUL)
#   rbg_para_write DEV TXT writes TXT padded with NULs to exactly 32 bytes, then reads it back
#   rbg_para_clear DEV     32 NUL bytes (LK boots the PARTNAME=boot partition), read back
#
# RBG_DISK=<disk> (e.g. loop7) limits the search to that disk and skips the eMMC check:
# the installer's tests run it against a loop device with a fake GPT.

# The disk name of a partition (mmcblk0p29 -> mmcblk0).
rbg_disk_of() {
  _d=${1##*/}
  _p=$(readlink -f "/sys/class/block/$_d" 2>/dev/null) || return 1
  [ -f "$_p/partition" ] || { echo "$_d"; return 0; }
  basename "$(dirname "$_p")"
}

# True when DISK is the internal eMMC (not an SD card or a USB stick).
rbg_is_emmc() {
  [ "$(cat "/sys/class/block/$1/device/type" 2>/dev/null)" = MMC ]
}

rbg_part() {
  _name=$1; _found=; _n=0
  for _u in /sys/class/block/*/uevent; do
    [ -f "$_u" ] || continue
    grep -qx "PARTNAME=$_name" "$_u" 2>/dev/null || continue
    _dev=$(basename "$(dirname "$_u")")
    _disk=$(rbg_disk_of "$_dev") || continue
    if [ -n "${RBG_DISK:-}" ]; then
      [ "$_disk" = "$RBG_DISK" ] || continue
    else
      rbg_is_emmc "$_disk" || continue
    fi
    _found=/dev/$_dev; _n=$((_n + 1))
  done
  if [ "$_n" -gt 1 ]; then
    echo "more than one eMMC partition is named '$_name'" >&2; return 1
  fi
  [ -n "$_found" ] || return 1
  [ -b "$_found" ] || { echo "$_found is not a block device" >&2; return 1; }
  echo "$_found"
}

rbg_para_read() {
  dd if="$1" bs=32 count=1 2>/dev/null | tr '\000' '\n' | head -n 1
}

# Hex of the first 32 bytes, read past the page cache when the kernel allows it.
rbg_para_hex() {
  { dd if="$1" bs=512 count=1 iflag=direct 2>/dev/null || dd if="$1" bs=512 count=1 2>/dev/null; } |
    head -c 32 | od -An -tx1 -v | tr -d ' \n'
}

rbg_para_write() {
  _dev=$1; _txt=$2
  [ ${#_txt} -lt 32 ] || { echo "para command too long" >&2; return 1; }
  # The 32 bytes go through a file: dd reading a pipe may get them in two short reads.
  _blk=$(mktemp) || return 1
  { printf '%s' "$_txt"; dd if=/dev/zero bs=1 count=$((32 - ${#_txt})) 2>/dev/null; } > "$_blk"
  [ "$(wc -c < "$_blk" | tr -d ' ')" = 32 ] || { rm -f "$_blk"; echo "para block is not 32 bytes" >&2; return 1; }
  _want=$(od -An -tx1 -v < "$_blk" | tr -d ' \n')
  dd if="$_blk" of="$_dev" bs=32 count=1 conv=notrunc,fsync 2>/dev/null || { rm -f "$_blk"; return 1; }
  rm -f "$_blk"
  sync
  [ "$(rbg_para_hex "$_dev")" = "$_want" ] || { echo "para readback differs from what was written" >&2; return 1; }
}

rbg_para_clear() {
  dd if=/dev/zero of="$1" bs=32 count=1 conv=notrunc,fsync 2>/dev/null || return 1
  sync
  [ "$(rbg_para_hex "$1")" = 0000000000000000000000000000000000000000000000000000000000000000 ] ||
    { echo "para readback is not 32 zero bytes" >&2; return 1; }
}
