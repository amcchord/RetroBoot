#!/bin/bash
# Run RetroBoot's Gemini initrd for real in qemu-system-aarch64 `virt` (rb-builder, root).
#
# The initrd under test is byte-identical to the one in boot.img. QEMU boots Debian 13's
# generic arm64 kernel, which has virtio-blk and ext4 as modules, so a second test-only
# cpio is appended: the decompressed modules plus /rb-test-init, which loads them and
# execs /init as PID 1 (rdinit=/rb-test-init). The disk is GPT with partitions named
# recovery, para, nvram, boot and linux, like the Gemini's eMMC (other numbering).
#
# Scenarios (each a fresh disk): systemd root handover and clean shutdown; nftables (the
# rulesets check-nftables.sh renders, loaded by a systemd root); busybox root
# with an absolute /sbin/init symlink; counter boundary (2 → boots as attempt 3);
# counter 3 → fallback; missing /retroboot/sbin/init → fallback; no linux partition →
# fallback; unclean journal (sysrq reboot mid-write) then replay; duplicate blocks that
# preen mode cannot fix (e2fsck -y path); LK charger boot mode 8 without a charger
# supply; para already "boot-recovery" in a normal boot (no loop); the same after a
# charger boot (reboot into recovery). charger.sh's wait loop is tested separately under
# qemu-user with a fake sysfs (test-charger.sh).
#
# Usage: run-qemu-tests.sh [--initrd FILE] [--only NAME[,NAME]] [--keep]
# Output: $WORK/tests/<run-id>/ (serial logs, results, summary.json).
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
WORK=${RB_GEMINI_WORK:-/srv/retroboot/wave7/gemini-kernel}
INITRD=$WORK/out/initrd/latest/initrd.cpio.gz
ONLY=""
KEEP=0
while [ $# -gt 0 ]; do
  case "$1" in
    --initrd) INITRD=$2; shift 2 ;;
    --only) ONLY=$2; shift 2 ;;
    --keep) KEEP=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
INITRD=$(readlink -f "$INITRD")
DEB_KVER=6.12.111+deb13-arm64
DEB_KPKG=linux-image-$DEB_KVER
RUN=$WORK/tests/$(date -u +%Y%m%dT%H%M%SZ)
CACHE=$WORK/tests/cache
APTROOT=$WORK/apt-arm64
mkdir -p "$RUN" "$CACHE"
log() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }
sha() { sha256sum "$1" | cut -d' ' -f1; }
exec > >(tee -a "$RUN/run.log") 2>&1
log "initrd under test: $INITRD ($(sha "$INITRD"))"

APT=(-o "Dir=$APTROOT" -o "Dir::State::status=$APTROOT/var/lib/dpkg/status"
     -o APT::Architecture=arm64 -o APT::Architectures::=arm64 -o APT::Sandbox::User=root
     -o "Dir::Etc::Parts=$APTROOT/etc/apt/apt.conf.d" -o "Dir::Etc::Preferences=$APTROOT/etc/apt/preferences"
     -o "Dir::Etc::PreferencesParts=$APTROOT/etc/apt/preferences.d")

# ---- Test kernel: Debian's generic arm64 kernel ---------------------------------------------
KDIR=$CACHE/kernel-$DEB_KVER
if [ ! -f "$KDIR/vmlinuz" ]; then
  log "fetching $DEB_KPKG"
  rm -rf "$KDIR"; mkdir -p "$KDIR/x"
  apt-get "${APT[@]}" -qq update
  (cd "$KDIR" && apt-get "${APT[@]}" -qq download "$DEB_KPKG")
  dpkg-deb -x "$KDIR"/*.deb "$KDIR/x"
  cp "$KDIR/x/boot/vmlinuz-$DEB_KVER" "$KDIR/vmlinuz"
  depmod -b "$KDIR/x/usr" "$DEB_KVER"
fi
KDEB=$(ls "$KDIR"/*.deb)

# ---- Test overlay cpio: modules + /rb-test-init -----------------------------------------------
OV=$CACHE/overlay-$DEB_KVER
if [ ! -f "$OV.cpio.gz" ]; then
  rm -rf "$OV"; mkdir -p "$OV/rb-test"
  MODS="crypto/crc32c_generic lib/crc16 fs/mbcache fs/jbd2/jbd2 fs/ext4/ext4 drivers/block/virtio_blk"
  for m in $MODS; do
    xz -dc "$KDIR/x/usr/lib/modules/$DEB_KVER/kernel/$m.ko.xz" > "$OV/rb-test/$(basename "$m").ko"
  done
  cat > "$OV/rb-test-init" <<EOF
#!/bin/busybox sh
# TEST ONLY: load the generic kernel's block/ext4 modules, then become the real /init.
/bin/busybox mkdir -p /proc && /bin/busybox mount -t proc proc /proc
for m in $(for m in $MODS; do printf '%s ' "$(basename "$m")"; done); do /bin/busybox insmod /rb-test/\$m.ko || echo "rb-test-init: insmod \$m failed"; done
/bin/busybox umount /proc
exec /init
EOF
  chmod 0755 "$OV/rb-test-init"
  (cd "$OV" && find . -mindepth 1 | LC_ALL=C sort | cpio --quiet -o -H newc -R 0:0 --reproducible) | gzip -9n > "$OV.cpio.gz"
fi
COMBINED=$RUN/initrd+test.cpio.gz
cat "$INITRD" "$OV.cpio.gz" > "$COMBINED"

# ---- Busybox from the initrd under test (for the small test roots) ----------------------------------
IX=$RUN/initrd-x
mkdir -p "$IX"
(cd "$IX" && gzip -dc "$INITRD" | cpio --quiet -id 2>/dev/null) || die "cannot unpack $INITRD"
BUSYBOX=$IX/bin/busybox

# ---- systemd root (Debian 13 arm64 minbase + systemd-sysv), cached --------------------------------
SDROOT=$CACHE/rootfs-systemd-v2
if [ ! -f "$SDROOT/.complete" ]; then
  log "building the Debian 13 arm64 systemd test root (mmdebstrap)"
  rm -rf "$SDROOT"
  mmdebstrap --quiet --arch=arm64 --variant=minbase --include=systemd-sysv,udev,mount,util-linux,e2fsprogs,kmod,nftables \
    trixie "$SDROOT" http://deb.debian.org/debian
  cat > "$SDROOT/usr/local/sbin/rbtest-report" <<'EOF'
#!/bin/sh
# TEST ONLY: what systemd sees after the RetroBoot initrd's handover, then the
# counter reset the image's health unit performs, then a normal poweroff.
out=/var/lib/rbtest/result.txt
mkdir -p /var/lib/rbtest
{
  echo "findmnt_root=$(findmnt -no SOURCE,FSROOT,FSTYPE,OPTIONS /)"
  echo "mountinfo_root=$(awk '$5=="/"' /proc/self/mountinfo)"
  echo "pid1=$(cat /proc/1/comm)"
  sed 's/^/state_/' /run/retroboot-initrd/state
  echo "failed_units=$(systemctl --failed --no-legend --plain | awk '{print $1}' | tr '\n' ' ')"
  udevadm settle
  mkdir -p /run/rbtest-linux
  mount /dev/disk/by-partlabel/linux /run/rbtest-linux && echo "partlabel_mount=ok"
  echo "counter_seen=$(cat /run/rbtest-linux/.retroboot-boot-attempts)"
  echo 0 > /run/rbtest-linux/.retroboot-boot-attempts.new
  mv /run/rbtest-linux/.retroboot-boot-attempts.new /run/rbtest-linux/.retroboot-boot-attempts
  sync
  echo "counter_after_reset=$(cat /run/rbtest-linux/.retroboot-boot-attempts)"
  umount /run/rbtest-linux && echo "partlabel_umount=ok"
  # nftables scenario: load RetroBoot's rendered rulesets (modules autoload from the
  # generic kernel's tree), then list them.
  if [ -d /etc/rbtest-nft.d ]; then
    for f in /etc/rbtest-nft.d/*.nft; do
      n=$(basename "$f" .nft)
      if nft -f "$f" 2> "/var/lib/rbtest/nft-$n.err"; then echo "nft_load_$n=ok"
      else echo "nft_load_$n=fail $(head -c 300 "/var/lib/rbtest/nft-$n.err" | tr '\n' ' ')"; fi
      tbl=$(sed -n 's/^table inet \([a-z_]*\) {$/\1/p' "$f" | head -n 1)
      nft list table inet "$tbl" > "/var/lib/rbtest/nft-$n.list" 2>&1 && echo "nft_list_$n=ok"
    done
    echo "nf_modules=$(awk '/^nf|^nft/{print $1}' /proc/modules | sort | tr '\n' ' ')"
  fi
  echo "done=1"
} > "$out" 2>&1
sync
systemctl --no-block poweroff
EOF
  chmod 0755 "$SDROOT/usr/local/sbin/rbtest-report"
  cat > "$SDROOT/etc/systemd/system/rbtest.service" <<'EOF'
[Unit]
Description=RetroBoot initrd handover test report (TEST ONLY)
After=local-fs.target systemd-udev-settle.service systemd-udevd.service
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/rbtest-report
[Install]
WantedBy=multi-user.target
EOF
  mkdir -p "$SDROOT/etc/systemd/system/multi-user.target.wants"
  ln -sf /etc/systemd/system/rbtest.service "$SDROOT/etc/systemd/system/multi-user.target.wants/rbtest.service"
  : > "$SDROOT/etc/machine-id"
  echo rbtest > "$SDROOT/etc/hostname"
  touch "$SDROOT/.complete"
fi

# ---- busybox root: /sbin -> usr/sbin, /usr/sbin/init -> /usr/lib/rbtest/init (absolute) ------------
make_bb_root() {  # make_bb_root DIR MODE(record|crash)
  local r=$1
  mkdir -p "$r"/{bin,dev,proc,sys,run,tmp,etc,usr/sbin,usr/lib/rbtest}
  install -m 0755 "$BUSYBOX" "$r/bin/busybox"
  for a in sh mount umount cat grep ls dd sync poweroff echo mkdir sleep awk sed; do ln -s busybox "$r/bin/$a"; done
  ln -s usr/sbin "$r/sbin"
  ln -s /usr/lib/rbtest/init "$r/usr/sbin/init"
  echo "$2" > "$r/etc/rbtest.mode"
  cat > "$r/usr/lib/rbtest/init" <<'EOF'
#!/bin/busybox sh
# TEST ONLY: PID 1 after the RetroBoot initrd's switch_root.
export PATH=/bin
out=/rbtest-result.txt
{
  echo "pid=$$"
  echo "mountinfo_root=$(awk '$5=="/"' /proc/self/mountinfo)"
  sed 's/^/state_/' /run/retroboot-initrd/state
  echo "mounts=$(awk '{print $2}' /proc/self/mounts | tr '\n' ' ')"
} > $out 2>&1
if [ "$(cat /etc/rbtest.mode)" = crash ] && [ ! -e /rbtest-crashed ]; then
  echo crashed > /rbtest-crashed
  sync
  echo "crash=pending" >> $out
  dd if=/dev/urandom of=/crash-file bs=1M count=24 2>/dev/null
  echo "rbtest: crashing without sync" > /dev/console
  echo b > /proc/sysrq-trigger
  sleep 30
fi
echo "done=1" >> $out
sync
mount -o remount,ro / && echo "rbtest: remount / ro ok" > /dev/console
poweroff -f
EOF
  chmod 0755 "$r/usr/lib/rbtest/init"
}

# ---- Disk: GPT recovery, para, nvram, boot, linux (sparse 2.5 GiB) --------------------------------
make_disk() {  # make_disk IMG LINUXDIR|"" [PARA_HEAD]  (PARA_HEAD: zero|recovery)
  local img=$1 linuxdir=$2 head=${3:-zero} start size
  rm -f "$img"; truncate -s 2560M "$img"
  sgdisk -o -n1:2048:+8M -c1:recovery -n2:0:+1M -c2:para -n3:0:+1M -c3:nvram -n4:0:+16M -c4:boot "$img" >/dev/null 2>&1
  [ -z "$linuxdir" ] || sgdisk -n5:0:0 -c5:linux "$img" >/dev/null 2>&1
  [ "$(sgdisk -i 2 "$img" | sed -n "s/^Partition name: '\(.*\)'/\1/p")" = para ] || die "sgdisk did not create the test GPT"
  for p in 1 2 3 4; do
    start=$(sgdisk -i "$p" "$img" | awk '/First sector/{print $3}')
    size=$(sgdisk -i "$p" "$img" | awk '/Partition size/{print $3}')
    head -c $((size * 512)) /dev/urandom | dd of="$img" bs=512 seek="$start" conv=notrunc status=none
  done
  start=$(sgdisk -i 2 "$img" | awk '/First sector/{print $3}')
  if [ "$head" = recovery ]; then
    { printf 'boot-recovery'; head -c 19 /dev/zero; } | dd of="$img" bs=1 seek=$((start * 512)) conv=notrunc status=none
  else
    head -c 32 /dev/zero | dd of="$img" bs=1 seek=$((start * 512)) conv=notrunc status=none
  fi
  if [ -n "$linuxdir" ]; then
    start=$(sgdisk -i 5 "$img" | awk '/First sector/{print $3}')
    size=$(sgdisk -i 5 "$img" | awk '/Partition size/{print $3}')
    mke2fs -q -t ext4 -F -L linux -d "$linuxdir" -E offset=$((start * 512)) "$img" $((size / 2))k \
      || die "mke2fs for $img failed"
  fi
}
part_hash() {  # part_hash IMG N [SKIP_BYTES]
  local start size
  start=$(sgdisk -i "$2" "$1" | awk '/First sector/{print $3}')
  size=$(sgdisk -i "$2" "$1" | awk '/Partition size/{print $3}')
  dd if="$1" bs=512 skip="$start" count="$size" status=none | tail -c +$(( ${3:-0} + 1 )) | sha256sum | cut -d' ' -f1
}
para_head() {  # first 32 bytes of para as hex
  local start
  start=$(sgdisk -i 2 "$1" | awk '/First sector/{print $3}')
  dd if="$1" bs=1 skip=$((start * 512)) count=32 status=none | od -An -tx1 | tr -d ' \n'
}
LOOP=
linux_attach() {  # loop-attach IMG with partitions; LINUXP = linux partition device
  LOOP=$(losetup --show -f -P "$1"); udevadm settle 2>/dev/null || true
  LINUXP=${LOOP}p5
  [ -b "$LINUXP" ] || { sleep 1; [ -b "$LINUXP" ] || die "no $LINUXP"; }
}
linux_detach() { [ -z "$LOOP" ] || losetup -d "$LOOP"; LOOP=; }
dfs() { debugfs -R "$1" "$LINUXP" 2>/dev/null; }

# ---- QEMU ---------------------------------------------------------------------------------------------
QEMU_BASE=(qemu-system-aarch64 -M virt -cpu cortex-a72 -smp 2 -m 1536 -nographic -no-reboot
           -kernel "$KDIR/vmlinuz" -initrd "$COMBINED")
APPEND="console=ttyAMA0 loglevel=4 panic=10 rdinit=/rb-test-init rb.emergency_s=8"
run_qemu() {  # run_qemu NAME IMG TIMEOUT [extra qemu args...]; sets EXIT_KIND
  local name=$1 img=$2 tmo=$3; shift 3
  local t0 rc
  mkdir -p "$RUN/$name"
  t0=$(date +%s)
  set +e
  timeout "$tmo" "${QEMU_BASE[@]}" -append "$APPEND${APPEND_EXTRA:+ $APPEND_EXTRA}" \
    -drive if=none,id=d0,file="$img",format=raw -device virtio-blk-pci,drive=d0 "$@" \
    < /dev/null > "$RUN/$name/serial.log" 2>&1
  rc=$?
  set -e
  if [ $rc = 124 ]; then EXIT_KIND=timeout
  elif grep -q 'reboot: Power down' "$RUN/$name/serial.log"; then EXIT_KIND=poweroff
  elif grep -q 'reboot: Restarting system' "$RUN/$name/serial.log"; then EXIT_KIND=reboot
  else EXIT_KIND="exit$rc"; fi
  log "$name: qemu $EXIT_KIND after $(( $(date +%s) - t0 )) s"
}
charger_dtb() {  # charger_dtb OUT MODE IMG — QEMU's own virt DTB plus LK's /chosen atag,boot
  "${QEMU_BASE[@]}" -M virt,dumpdtb="$1.base" -append "$APPEND" \
    -drive if=none,id=d0,file="$3",format=raw -device virtio-blk-pci,drive=d0 >/dev/null 2>&1
  cp "$1.base" "$1"
  fdtput -t bx "$1" /chosen atag,boot 03 00 00 00 02 08 00 41 "$(printf '%02x' "$2")" 00 00 00
}

# ---- Assertions ---------------------------------------------------------------------------------------
PASSES=0; FAILS=0; SUMMARY=""
CUR=""
expect() {  # expect DESCRIPTION COMMAND...
  if "${@:2}" >/dev/null 2>&1; then
    PASSES=$((PASSES + 1)); echo "  PASS $1" | tee -a "$RUN/$CUR/result.txt"
  else
    FAILS=$((FAILS + 1)); echo "  FAIL $1" | tee -a "$RUN/$CUR/result.txt"
    SUMMARY="$SUMMARY$CUR: $1; "
  fi
}
serial_has() { grep -qF -- "$1" "$RUN/$CUR/serial.log"; }
fs_clean() { dumpe2fs -h "$LINUXP" 2>/dev/null | grep -q '^Filesystem state: *clean$' && ! dumpe2fs -h "$LINUXP" 2>/dev/null | grep -q needs_recovery; }
fsck_clean() { e2fsck -fn "$LINUXP"; }
counter_is() { [ "$(dfs 'cat /.retroboot-boot-attempts')" = "$1" ]; }
file_has() { dfs "cat $1" | grep -qF -- "$2"; }
PARA_RECOVERY_HEX=$( { printf 'boot-recovery'; head -c 19 /dev/zero; } | od -An -tx1 | tr -d ' \n')
PARA_ZERO_HEX=$(head -c 32 /dev/zero | od -An -tx1 | tr -d ' \n')
want() { [ -z "$ONLY" ] || case ",$ONLY," in *",$1,"*) return 0 ;; *) return 1 ;; esac; }
begin() { CUR=$1; mkdir -p "$RUN/$CUR"; log "=== $CUR: $2"; echo "$2" > "$RUN/$CUR/result.txt"; }
snapshot() {  # hashes of the partitions that must never change (para beyond 32 bytes)
  echo "$(part_hash "$1" 1) $(part_hash "$1" 2 32) $(part_hash "$1" 3) $(part_hash "$1" 4)"
}

# Linux filesystem trees
mk_linux_tree() {  # mk_linux_tree DIR ROOTKIND(systemd|bb|bb-crash|none|empty) [COUNTER]
  local d=$1
  rm -rf "$d"; mkdir -p "$d/etc"
  printf 'PRETTY_NAME="Gemian stand-in (test)"\n' > "$d/etc/os-release"
  case "$2" in
    systemd) mkdir -p "$d/retroboot"; cp -a "$SDROOT/." "$d/retroboot/"; rm -f "$d/retroboot/.complete" ;;
    bb) make_bb_root "$d/retroboot" record ;;
    bb-crash) make_bb_root "$d/retroboot" crash ;;
    empty) mkdir -p "$d/retroboot/etc" ;;
    none) ;;
  esac
  [ -z "${3:-}" ] || echo "$3" > "$d/.retroboot-boot-attempts"
}

# ---- Scenarios ------------------------------------------------------------------------------------------
T=$RUN/tmp; mkdir -p "$T"

if want systemd; then
  begin systemd "systemd root: handover, what systemd sees, counter reset by userspace, clean poweroff"
  mk_linux_tree "$T/tree" systemd
  make_disk "$T/disk.img" "$T/tree"; snap=$(snapshot "$T/disk.img")
  # loglevel=7 for this run so ext4's KERN_INFO remount messages reach the serial log.
  APPEND_EXTRA=loglevel=7 run_qemu systemd "$T/disk.img" 900
  linux_attach "$T/disk.img"
  dfs "cat /retroboot/var/lib/rbtest/result.txt" > "$RUN/$CUR/report.txt"
  expect "qemu powered off" [ "$EXIT_KIND" = poweroff ]
  expect "initrd counted attempt 1" serial_has "rb-initrd: attempt 1 of 3"
  expect "systemd ran the report" grep -q '^done=1' "$RUN/$CUR/report.txt"
  expect "PID 1 is systemd" grep -q '^pid1=systemd' "$RUN/$CUR/report.txt"
  expect "/ is the linux partition's /retroboot" grep -Eq '^findmnt_root=/dev/vda5\[/retroboot\] +/retroboot +ext4 +rw' "$RUN/$CUR/report.txt"
  expect "one / mount, root /retroboot" grep -q '^mountinfo_root=.* /retroboot / rw' "$RUN/$CUR/report.txt"
  expect "initrd state for userspace" grep -q '^state_RB_COUNTER_FILE=/.retroboot-boot-attempts' "$RUN/$CUR/report.txt"
  expect "userspace saw counter 1" grep -q '^counter_seen=1' "$RUN/$CUR/report.txt"
  expect "by-partlabel/linux mount alongside /" grep -q '^partlabel_mount=ok' "$RUN/$CUR/report.txt"
  expect "no failed units" grep -q '^failed_units= *$' "$RUN/$CUR/report.txt"
  expect "counter reset to 0 on disk" counter_is 0
  expect "shutdown remounted / read-only" grep -Eq 'EXT4-fs \(vda5\): re-mounted .* ro' "$RUN/$CUR/serial.log"
  expect "shutdown left the filesystem clean" fs_clean
  expect "e2fsck -fn clean" fsck_clean
  expect "initrd log persisted" file_has /.retroboot-initrd.log "rb-initrd: attempt 1 of 3"
  linux_detach
  expect "para still zero" [ "$(para_head "$T/disk.img")" = "$PARA_ZERO_HEX" ]
  expect "recovery, para tail, nvram, boot untouched" [ "$(snapshot "$T/disk.img")" = "$snap" ]
fi

if want nftables; then
  begin nftables "systemd root loads hostd's and the web helper's nftables tables (generic kernel: userspace side only)"
  if [ -f "$CACHE/nft/guestnet.nft" ] && [ -f "$CACHE/nft/webproxy.nft" ]; then
    mk_linux_tree "$T/tree" systemd
    mkdir -p "$T/tree/retroboot/usr/lib/modules" "$T/tree/retroboot/etc/rbtest-nft.d"
    cp -a "$KDIR/x/usr/lib/modules/$DEB_KVER" "$T/tree/retroboot/usr/lib/modules/"
    cp "$CACHE/nft/guestnet.nft" "$CACHE/nft/webproxy.nft" "$T/tree/retroboot/etc/rbtest-nft.d/"
    make_disk "$T/disk.img" "$T/tree"
    run_qemu nftables "$T/disk.img" 900
    linux_attach "$T/disk.img"
    dfs "cat /retroboot/var/lib/rbtest/result.txt" > "$RUN/$CUR/report.txt"
    for f in guestnet webproxy; do dfs "cat /retroboot/var/lib/rbtest/nft-$f.list" > "$RUN/$CUR/nft-$f.list"; done
    expect "qemu powered off" [ "$EXIT_KIND" = poweroff ]
    expect "guestnet table loaded" grep -q '^nft_load_guestnet=ok' "$RUN/$CUR/report.txt"
    expect "guestnet table listed (fib, skuid, reject)" sh -c "grep -q 'fib daddr type local' '$RUN/$CUR/nft-guestnet.list' && grep -q 'meta skuid 4242' '$RUN/$CUR/nft-guestnet.list' && grep -q 'reject with tcp reset' '$RUN/$CUR/nft-guestnet.list'"
    expect "webproxy table loaded" grep -q '^nft_load_webproxy=ok' "$RUN/$CUR/report.txt"
    expect "webproxy table listed (nat output, redirect)" sh -c "grep -q 'hook output' '$RUN/$CUR/nft-webproxy.list' && grep -q 'redirect to :8080' '$RUN/$CUR/nft-webproxy.list'"
    linux_detach
  else
    expect "rendered rulesets present (run check-nftables.sh first)" false
  fi
fi

if want busybox; then
  begin busybox "busybox root via absolute /sbin/init symlink; counter 2 boots as attempt 3"
  mk_linux_tree "$T/tree" bb 2
  make_disk "$T/disk.img" "$T/tree"; snap=$(snapshot "$T/disk.img")
  run_qemu busybox "$T/disk.img" 300
  linux_attach "$T/disk.img"
  dfs "cat /retroboot/rbtest-result.txt" > "$RUN/$CUR/report.txt"
  expect "qemu powered off" [ "$EXIT_KIND" = poweroff ]
  expect "attempt 3 of 3" serial_has "rb-initrd: attempt 3 of 3"
  expect "test init ran as PID 1" grep -q '^pid=1$' "$RUN/$CUR/report.txt"
  expect "root is /retroboot" grep -q '^mountinfo_root=.* /retroboot / rw' "$RUN/$CUR/report.txt"
  expect "/dev /proc /sys /run moved" [ "$(grep '^mounts=' "$RUN/$CUR/report.txt" | cut -d= -f2 | tr ' ' '\n' | grep -cxE '/dev|/proc|/sys|/run')" = 4 ]
  expect "remount ro then poweroff" serial_has "rbtest: remount / ro ok"
  expect "counter now 3" counter_is 3
  expect "filesystem clean" fs_clean
  linux_detach
  expect "para still zero" [ "$(para_head "$T/disk.img")" = "$PARA_ZERO_HEX" ]
  expect "other partitions untouched" [ "$(snapshot "$T/disk.img")" = "$snap" ]
fi

fallback_checks() {  # common to the fallback scenarios: $1 = reason text
  expect "qemu exited by reboot" [ "$EXIT_KIND" = reboot ]
  expect "fallback logged" serial_has "rb-initrd: FALLBACK to Gemian (recovery slot p1): $1"
  expect "para verified by the initrd" serial_has "now reads boot-recovery; rebooting"
  expect "para = boot-recovery + 19 NULs" [ "$(para_head "$T/disk.img")" = "$PARA_RECOVERY_HEX" ]
  expect "rest of para, recovery, nvram, boot untouched" [ "$(snapshot "$T/disk.img")" = "$snap" ]
}

if want counter3; then
  begin counter3 "counter at 3 → fallback to recovery"
  mk_linux_tree "$T/tree" bb 3
  make_disk "$T/disk.img" "$T/tree"; snap=$(snapshot "$T/disk.img")
  run_qemu counter3 "$T/disk.img" 300
  fallback_checks "3 boot attempts without a healthy RetroBoot"
  linux_attach "$T/disk.img"
  expect "test init never ran" sh -c "! debugfs -R 'stat /retroboot/rbtest-result.txt' $LINUXP 2>&1 | grep -q Inode:"
  expect "counter reset to 0 for the next deliberate boot" counter_is 0
  expect "fallback note written" file_has /.retroboot-fallback "reason=3 boot attempts"
  expect "log persisted" file_has /.retroboot-initrd.log "FALLBACK to Gemian"
  expect "filesystem clean" fs_clean
  linux_detach
fi

if want noinit; then
  begin noinit "/retroboot without sbin/init → fallback"
  mk_linux_tree "$T/tree" empty
  make_disk "$T/disk.img" "$T/tree"; snap=$(snapshot "$T/disk.img")
  run_qemu noinit "$T/disk.img" 300
  fallback_checks "/retroboot/sbin/init missing on /dev/vda5"
  linux_attach "$T/disk.img"
  expect "fallback note written" file_has /.retroboot-fallback "reason=/retroboot/sbin/init missing"
  expect "filesystem clean" fs_clean
  linux_detach
fi

if want nolinux; then
  begin nolinux "no partition named linux → fallback (waits ${RB_WAIT_S:-30} s for the eMMC first)"
  make_disk "$T/disk.img" ""; snap=$(snapshot "$T/disk.img")
  run_qemu nolinux "$T/disk.img" 300
  fallback_checks "no partition named linux"
fi

if want dirty; then
  begin dirty "unclean journal: crash mid-write, then e2fsck -p replays it on the next boot"
  mk_linux_tree "$T/tree" bb-crash
  make_disk "$T/disk.img" "$T/tree"; snap=$(snapshot "$T/disk.img")
  run_qemu dirty-crash "$T/disk.img" 300 || true
  mv "$RUN/dirty-crash/serial.log" "$RUN/$CUR/serial-crash.log" 2>/dev/null || true
  linux_attach "$T/disk.img"
  expect "first boot crashed with the journal dirty" sh -c "dumpe2fs -h $LINUXP 2>/dev/null | grep -q needs_recovery"
  linux_detach
  # Second boot: /rbtest-crashed (synced before the crash) makes the test init record.
  run_qemu dirty "$T/disk.img" 300
  linux_attach "$T/disk.img"
  expect "qemu powered off" [ "$EXIT_KIND" = poweroff ]
  expect "e2fsck replayed the journal" serial_has "recovering journal"
  expect "boot continued as attempt 2" serial_has "rb-initrd: attempt 2 of 3"
  expect "filesystem clean" fs_clean
  expect "e2fsck -fn clean" fsck_clean
  linux_detach
fi
[ ! -d "$RUN/dirty-crash" ] || rmdir "$RUN/dirty-crash" 2>/dev/null || true

if want dupblocks; then
  begin dupblocks "duplicate blocks on a filesystem flagged with errors: e2fsck -p cannot fix, e2fsck -y does, boot continues"
  mk_linux_tree "$T/tree" bb
  mkdir -p "$T/tree/junk"; head -c 65536 /dev/urandom > "$T/tree/junk/a"; head -c 65536 /dev/urandom > "$T/tree/junk/b"
  make_disk "$T/disk.img" "$T/tree"; snap=$(snapshot "$T/disk.img")
  linux_attach "$T/disk.img"
  blk=$(debugfs -R "bmap /junk/a 0" "$LINUXP" 2>/dev/null)
  debugfs -w -R "sif /junk/b block[5] $blk" "$LINUXP" >/dev/null 2>&1
  # ext4 flags the superblock when it meets corruption (valid + errors); without the
  # flag e2fsck -p rightly trusts a clean filesystem and skips the full check.
  debugfs -w -R "ssv state 3" "$LINUXP" >/dev/null 2>&1
  expect "corruption present before boot" sh -c "! e2fsck -fn $LINUXP"
  linux_detach
  run_qemu dupblocks "$T/disk.img" 300
  linux_attach "$T/disk.img"
  expect "qemu powered off" [ "$EXIT_KIND" = poweroff ]
  expect "preen mode gave up" serial_has "e2fsck -p exit 4: running e2fsck -y"
  expect "e2fsck -y cloned the blocks" serial_has "Multiply-claimed"
  expect "boot continued" serial_has "rb-initrd: attempt 1 of 3"
  expect "e2fsck -fn clean afterwards" fsck_clean
  expect "filesystem clean" fs_clean
  linux_detach
fi

if want charger; then
  begin charger "LK charger boot (atag,boot mode 8) without a charger supply → normal boot"
  mk_linux_tree "$T/tree" bb
  make_disk "$T/disk.img" "$T/tree"
  charger_dtb "$T/charger8.dtb" 8 "$T/disk.img"
  run_qemu charger "$T/disk.img" 300 -dtb "$T/charger8.dtb"
  expect "qemu powered off" [ "$EXIT_KIND" = poweroff ]
  expect "mode 8 detected, fail-safe normal boot" serial_has "charger boot (mode 8): no charger supply, booting normally"
  expect "boot continued" serial_has "rb-initrd: attempt 1 of 3"
fi

if want pararecovery; then
  begin pararecovery "para already boot-recovery in a normal boot → emergency stop, no reboot loop"
  mk_linux_tree "$T/tree" bb
  make_disk "$T/disk.img" "$T/tree" recovery; snap=$(snapshot "$T/disk.img")
  run_qemu pararecovery "$T/disk.img" 300
  linux_attach "$T/disk.img"
  expect "qemu powered off (emergency timer), not rebooted" [ "$EXIT_KIND" = poweroff ]
  expect "emergency logged" serial_has "EMERGENCY: LK booted p22 although para requests boot-recovery"
  expect "counter untouched" sh -c "! debugfs -R 'stat /.retroboot-boot-attempts' $LINUXP 2>&1 | grep -q Inode:"
  expect "filesystem never mounted rw (clean)" fs_clean
  linux_detach
  expect "para still boot-recovery" [ "$(para_head "$T/disk.img")" = "$PARA_RECOVERY_HEX" ]
  expect "other partitions untouched" [ "$(snapshot "$T/disk.img")" = "$snap" ]
fi

if want chargerrecovery; then
  begin chargerrecovery "para boot-recovery after a charger boot → reboot into recovery"
  mk_linux_tree "$T/tree" bb
  make_disk "$T/disk.img" "$T/tree" recovery
  charger_dtb "$T/charger8.dtb" 8 "$T/disk.img"
  run_qemu chargerrecovery "$T/disk.img" 300 -dtb "$T/charger8.dtb"
  expect "qemu exited by reboot" [ "$EXIT_KIND" = reboot ]
  expect "logged" serial_has "para requests recovery after a charger boot: rebooting into it"
fi

rm -rf "$T"
[ "$KEEP" = 1 ] || rm -rf "$IX"
python3 - "$RUN/summary.json" <<PY
import json, sys
json.dump({"initrd": "$INITRD", "initrd_sha256": "$(sha "$INITRD")",
           "test_kernel": "$DEB_KPKG", "test_kernel_deb_sha256": "$(sha "$KDEB")",
           "qemu": "$(qemu-system-aarch64 --version | head -1)",
           "passes": $PASSES, "fails": $FAILS, "failed": "$SUMMARY".strip("; ").split("; ") if "$SUMMARY" else []},
          open(sys.argv[1], "w"), indent=2)
PY
log "summary: $PASSES passed, $FAILS failed ($RUN)"
[ "$FAILS" = 0 ]
