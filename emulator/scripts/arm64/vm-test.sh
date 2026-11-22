#!/bin/bash
# vm-test.sh ROOTFS PKG FPCONF_DIR EMULATOR_DIR OUT [GUEST_DISK]
# env: VM_SMP VM_MEM VM_TIMEOUT CONF_COUNT RUN_CONF(1) VERIFY_RUNS(1) NDRV SMOKE_TIMEOUT
# Boots a qemu-system-aarch64 virt VM (TCG, -cpu max) with the kernel of ROOTFS
# (mkrootfs.sh; arm64-16k = 16 KiB pages) and runs the aarch64 package inside it:
# page size, library resolution, verify_package.py, the conformance harness (seed 501,
# arms on/off/nobr; res.bin is compared bit for bit with an x86 run of the same
# parameters outside) and the self-modifying-code test; with GUEST_DISK (a sparse
# copy, attached as /dev/vdb) also a Finder smoke run (rbd_arm64_smoke.py).
# Everything is emulated: the results say nothing about a Raspberry Pi's speed.
# Lab only, root on the x86 builder.  Results: OUT/console.log and OUT/out/.
set -euo pipefail
R=$(readlink -f "$1"); PKG=$(readlink -f "$2"); FP=$(readlink -f "$3"); EMU=$(readlink -f "$4"); OUT=$5; DISK=${6:-}
SMP=${VM_SMP:-3}; MEM=${VM_MEM:-6144}; CONF_COUNT=${CONF_COUNT:-2000}; VM_TIMEOUT=${VM_TIMEOUT:-14400}
mkdir -p "$OUT"; OUT=$(readlink -f "$OUT")
K=$(ls "$R"/boot/vmlinuz-* | tail -1); I=$(ls "$R"/boot/initrd.img-* | tail -1)
T=$OUT/stage; rm -rf "$T"; mkdir -p "$T/opt/rbtest"
cp -a "$PKG" "$T/opt/rbtest/pkg"
cp -a "$FP" "$T/opt/rbtest/fpconf"
mkdir -p "$T/opt/rbtest/emulator"; cp -a "$EMU/scripts" "$EMU/tools" "$EMU/tests" "$T/opt/rbtest/emulator/"
cat > "$T/opt/rbtest/init.sh" <<'EOS'
#!/bin/bash
# PID 1 inside the VM: run the checks, write /opt/rbtest/out, power off.
export PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root LANG=C.UTF-8
mountpoint -q /proc || mount -t proc proc /proc
mountpoint -q /sys || mount -t sysfs sys /sys
mountpoint -q /dev || mount -t devtmpfs dev /dev
mkdir -p /dev/pts /dev/shm /run /tmp; mount -t devpts devpts /dev/pts 2>/dev/null; mount -t tmpfs tmpfs /dev/shm; mount -t tmpfs tmpfs /run; mount -t tmpfs tmpfs /tmp
mount -o remount,rw /
B=/opt/rbtest; O=$B/out; rm -rf $O; mkdir -p $O; P=$B/pkg; Q=$P/bin/qemu-system-ppc
log() { echo "rbtest $(date -u +%T) $*" | tee -a $O/steps.log > /dev/console; }
log "kernel $(uname -r) pagesize $(getconf PAGESIZE) cpus $(nproc) mem $(awk '/MemTotal/{print $2}' /proc/meminfo)kB"
getconf PAGESIZE > $O/pagesize.txt; uname -a > $O/uname.txt
ldd $Q > $O/ldd.txt 2>&1; ldd $P/bin/qemu-img >> $O/ldd.txt 2>&1
grep -c "not found" $O/ldd.txt > $O/ldd-missing.txt
dpkg-query -W -f '${Package}:${Architecture} ${Version}\n' > $O/packages.txt
$Q --version > $O/version.txt 2>&1; log "version rc=$? $(head -1 $O/version.txt)"
for i in $(seq 1 __VERIFY_RUNS__); do
  s=$(date +%s); python3 $B/emulator/scripts/verify_package.py $P > $O/verify-$i.txt 2>&1; rc=$?; log "verify_package $i rc=$rc $(( $(date +%s)-s ))s"
  [ $rc = 0 ] || sed 's/^/rbtest verify: /' $O/verify-$i.txt | tail -25 > /dev/console
done
if [ __RUN_CONF__ = 1 ]; then
s=$(date +%s); python3 $B/fpconf/fpconf.py conf --qemu $Q --elf $B/fpconf/fpconf.elf --count __COUNT__ --seed 501 \
  --arm on=fast-fpu=on --arm off=fast-fpu=off --arm nobr=fast-branch=off --jobs 2 --root $O/conf > $O/conf.txt 2>&1
log "conf rc=$? $(( $(date +%s)-s ))s"; sed 's/^/rbtest conf: /' $O/conf.txt | tail -8 > /dev/console
sha256sum $O/conf/*/res.bin | sed 's/^/rbtest res: /' > /dev/console
s=$(date +%s); python3 $B/fpconf/fpconf.py smc --qemu $Q --elf $B/fpconf/fpconf.elf --arm on=fast-fpu=on --arm nobr=fast-branch=off \
  --root $O/smc > $O/smc.txt 2>&1; log "smc rc=$? $(( $(date +%s)-s ))s"; sed 's/^/rbtest smc: /' $O/smc.txt | tail -3 > /dev/console
fi
if [ -b /dev/vdb ]; then
  ( while sleep 60; do echo "rbtest progress $(date -u +%T) $(ls $O/smoke 2>/dev/null | grep -v '\.ppm$' | tr '\n' ' ')" > /dev/console; done ) &
  s=$(date +%s); python3 $B/emulator/tools/rbd_arm64_smoke.py --out $O/smoke --seed /dev/null --pkg $P --ndrv $B/ndrv \
    --timeout __SMOKE_TIMEOUT__ --step-timeout 3600 -- --disk /dev/vdb > $O/smoke.txt 2>&1
  log "smoke rc=$? $(( $(date +%s)-s ))s $(tail -1 $O/smoke.txt | cut -c1-300)"
fi
log done; sync; sleep 1
echo o > /proc/sysrq-trigger; sleep 30
EOS
sed -i "s/__COUNT__/$CONF_COUNT/; s/__SMOKE_TIMEOUT__/${SMOKE_TIMEOUT:-21600}/; s/__VERIFY_RUNS__/${VERIFY_RUNS:-1}/; s/__RUN_CONF__/${RUN_CONF:-1}/" "$T/opt/rbtest/init.sh"
chmod +x "$T/opt/rbtest/init.sh"
[ -n "${NDRV:-}" ] && cp -a "$NDRV" "$T/opt/rbtest/ndrv"
# root image: the rootfs plus the test tree (mke2fs -d; no loop mounts)
IMG=$OUT/root.img; rm -f "$IMG"
S=$(mktemp -d "$OUT/merge.XXXX"); cp -a "$R/." "$S/"; cp -a "$T/." "$S/"
mke2fs -q -t ext4 -L rbroot -d "$S" "$IMG" 8G; rm -rf "$S"
D=(); [ -n "$DISK" ] && D=(-drive "file=$DISK,if=virtio,format=raw,cache=unsafe")
echo "vm: $(basename "$K") smp=$SMP mem=$MEM disk=${DISK:-none} start $(date -u +%FT%TZ)" | tee "$OUT/vm.txt"
s=$(date +%s)
timeout "$VM_TIMEOUT" qemu-system-aarch64 -M virt -cpu max -smp "$SMP" -m "$MEM" -accel tcg,thread=multi \
  -kernel "$K" -initrd "$I" -append "root=/dev/vda rw console=ttyAMA0 init=/opt/rbtest/init.sh" \
  -drive "file=$IMG,if=virtio,format=raw,cache=unsafe" "${D[@]}" \
  -nographic -no-reboot -nic none > "$OUT/console.log" 2>&1 || echo "vm exit $?" | tee -a "$OUT/vm.txt"
echo "vm: end $(date -u +%FT%TZ) $(( $(date +%s)-s ))s" | tee -a "$OUT/vm.txt"
debugfs -R "rdump /opt/rbtest/out $OUT" "$IMG" > /dev/null 2>&1 || true
grep rbtest "$OUT/console.log" || true
