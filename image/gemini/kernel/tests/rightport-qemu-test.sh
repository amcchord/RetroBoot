#!/bin/bash
# Run the real gemini-right-port driver in qemu-system-aarch64 `virt` (rb-builder) against a
# simulated FUSB301A and simulated GPIO94/72, and check the right port's VBUS policy end to end.
#
# The kernel is built from the same source as the rightport candidate (linux 6.6.157 + the
# gemini-nixos delta + patches/ + the rightport variant chain), configured for virt only:
# allnoconfig plus PL011, initramfs, configfs, I2C with i2c-stub (a module: a register-file
# chip at 0x25 on i2c-0), gpio-sim (a bank labelled rp-sim) and the driver. One test-only file, added
# to the test tree and never to the product patch, registers a GPIO lookup that gives the
# i2c-0 0x25 device rp-sim line 0 as "vbus" (GPIO94) and line 1 as "switch" (GPIO72).
# The driver is bound by name through new_device, as an OF-less i2c client.
#
# The test init (busybox) plays the partner by writing Status/Type with i2cset and reads the
# lines the driver drives from gpio-sim's sysfs. Scenarios: probe with a wrong ID (VBUS stays
# off); probe over a chip left in SINK with a charger attached; a charger; VBUS from an A-to-C
# cable; a USB stick on CC1 (not on at once, on after two polls); our own VBUSOK; unplug
# latency; the charger swapped in; CC2 with an active cable; Modes changed to DRP behind the
# driver (off, SOURCE restored, back on); allow_source=0/1; the partner turning into a source;
# unbind and remove while sourcing (off, Modes stays SOURCE).
#
# Usage: rightport-qemu-test.sh [--keep]
# Output: $WORK/tests/rightport-<stamp>/ (serial.log, results.txt, kernel config).
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
KDIR=$(dirname "$HERE")
. "$KDIR/pins.conf"
WORK=${RB_GEMINI_WORK:-/srv/retroboot/wave7/gemini-kernel}
KEEP=0
[ "${1:-}" = --keep ] && KEEP=1
RUN=$WORK/tests/rightport-$(date -u +%Y%m%dT%H%M%SZ)
T=$WORK/build/rightport-qemu
TARBALL=$WORK/cache/$(basename "$LINUX_URL")
DELTA=$WORK/upstream/gemini-nixos/$UPSTREAM_DEVICE_DIR/kernel/delta
INITRD=$WORK/out/initrd/latest/initrd.cpio.gz	# for its busybox (i2cset, i2cget, usleep)

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

[ "$(sha256sum "$TARBALL" | cut -d' ' -f1)" = "$LINUX_SHA256" ] || die "$TARBALL sha256"
[ "$(git -C "$WORK/upstream/gemini-nixos" rev-parse HEAD)" = "$UPSTREAM_REV" ] || die "upstream not at $UPSTREAM_REV"
rm -rf "$T"
mkdir -p "$T" "$RUN"

# ---- source: the rightport candidate's tree --------------------------------------------
log "source: linux $LINUX_VERSION + delta + patches + rightport chain"
tar -C "$T" -xzf "$TARBALL"
S=$T/linux-$LINUX_VERSION
cp -a "$DELTA/." "$S/"
CHAIN=""
add_variant() { local p; [ -f "$KDIR/variants/$1/inherit" ] && for p in $(cat "$KDIR/variants/$1/inherit"); do add_variant "$p"; done; CHAIN="$CHAIN $1"; }
add_variant rightport
for p in "$KDIR"/patches/*.patch $(for v in $CHAIN; do ls "$KDIR/variants/$v"/*.patch; done); do
  patch -p1 -d "$S" --forward --no-backup-if-mismatch -s < "$p" || die "patch $p"
done
log "variants:$CHAIN"
cat > "$S/drivers/usb/typec/gemini-right-port-qemu.c" <<'EOF'
// SPDX-License-Identifier: GPL-2.0
/* RetroBoot rightport QEMU test only: GPIO lookup for the OF-less i2c-0 0x25 client. */
#include <linux/gpio/machine.h>
#include <linux/init.h>

static struct gpiod_lookup_table grp_qemu_lookup = {
	.dev_id = "0-0025",
	.table = {
		GPIO_LOOKUP("rp-sim", 0, "vbus", GPIO_ACTIVE_HIGH),
		GPIO_LOOKUP("rp-sim", 1, "switch", GPIO_ACTIVE_HIGH),
		{ }
	},
};

static int __init grp_qemu_init(void)
{
	gpiod_add_lookup_table(&grp_qemu_lookup);
	return 0;
}
late_initcall(grp_qemu_init);
EOF
echo 'obj-y += gemini-right-port-qemu.o' >> "$S/drivers/usb/typec/Makefile"

# ---- kernel: virt only ------------------------------------------------------------------
O=$T/obj
MAKE=(make -C "$S" O="$O" ARCH=arm64 CROSS_COMPILE="$CROSS_COMPILE" -j"$(nproc)")
cat > "$T/virt.config" <<'EOF'
CONFIG_PRINTK=y
CONFIG_TTY=y
CONFIG_SERIAL_AMBA_PL011=y
CONFIG_SERIAL_AMBA_PL011_CONSOLE=y
CONFIG_BLK_DEV_INITRD=y
CONFIG_RD_GZIP=y
CONFIG_DEVTMPFS=y
CONFIG_PROC_FS=y
CONFIG_SYSFS=y
CONFIG_CONFIGFS_FS=y
CONFIG_BINFMT_ELF=y
CONFIG_BINFMT_SCRIPT=y
CONFIG_I2C=y
CONFIG_I2C_CHARDEV=y
CONFIG_MODULES=y
CONFIG_I2C_STUB=m
CONFIG_GPIOLIB=y
CONFIG_GPIO_SIM=y
CONFIG_USB_SUPPORT=y
CONFIG_TYPEC=y
CONFIG_TYPEC_GEMINI_RIGHT_PORT=y
EOF
log "kernel: allnoconfig + virt.config"
KCONFIG_ALLCONFIG="$T/virt.config" "${MAKE[@]}" allnoconfig >/dev/null
while IFS= read -r line; do
  grep -qxF "$line" "$O/.config" || die "test kernel config: $line not set"
done < "$T/virt.config"
"${MAKE[@]}" Image modules > "$T/build.log" 2>&1 || { tail -30 "$T/build.log"; die "test kernel build failed"; }
grep -n 'gemini-right-port.c.*warning' "$T/build.log" && die "warnings in gemini-right-port.c"
cp "$O/.config" "$RUN/kernel.config"

# ---- initramfs: busybox + the test --------------------------------------------------------
R=$T/root
mkdir -p "$R/bin" "$R/proc" "$R/sys" "$R/dev"
(cd "$T" && zcat "$INITRD" | cpio -id --quiet bin/busybox) && mv "$T/bin/busybox" "$R/bin/busybox"
cp "$O/drivers/i2c/i2c-stub.ko" "$R/i2c-stub.ko"	# i2c-stub is module-only
for a in insmod sh mount cat echo mkdir sleep usleep i2cset i2cget grep dmesg poweroff date tail; do ln -s busybox "$R/bin/$a"; done
cat > "$R/init" <<'EOF'
#!/bin/sh
mount -t proc proc /proc; mount -t sysfs sys /sys; mount -t devtmpfs dev /dev
mount -t configfs none /sys/kernel/config
insmod /i2c-stub.ko chip_addr=0x25
PASS=0; FAIL=0
ok()   { PASS=$((PASS + 1)); echo "RPTEST ok: $*"; }
bad()  { FAIL=$((FAIL + 1)); echo "RPTEST FAIL: $*"; }
eq()   { if [ "$1" = "$2" ]; then ok "$3 ($1)"; else bad "$3: got '$1', want '$2'"; fi; }
SET()  { i2cset -f -y 0 0x25 "$1" "$2"; }
GET()  { i2cget -f -y 0 0x25 "$1"; }
D=/sys/bus/i2c/devices/0-0025
DRV=/sys/bus/i2c/drivers/gemini-right-port
ST()   { cat $D/state 2>/dev/null || echo none; }
LN()   { echo "$(cat $SIM/sim_gpio0/value)$(cat $SIM/sim_gpio1/value)"; }	# vbus, switch
NOW()  { cat /proc/uptime | { read u _; echo $(( ${u%.*} * 1000 + 1${u#*.} * 10 - 1000 )); }; }	# ms (no octal)
waitfor() {  # waitfor LINES STATE MAX_MS -> echoes the ms it took, or "timeout"
  t0=$(NOW)
  while :; do
    if [ "$(LN)" = "$1" ] && [ "$(ST)" = "$2" ]; then echo $(( $(NOW) - t0 )); return; fi
    [ $(( $(NOW) - t0 )) -gt "$3" ] && { echo "timeout(lines $(LN) state $(ST))"; return; }
    usleep 10000
  done
}
holds() {  # holds LINES MS: the lines never differ from LINES for MS
  t0=$(NOW)
  while [ $(( $(NOW) - t0 )) -lt "$2" ]; do
    [ "$(LN)" = "$1" ] || { echo "changed to $(LN) after $(( $(NOW) - t0 )) ms"; return; }
    usleep 10000
  done
  echo held
}
under() { case "$1" in timeout*) bad "$2: $1" ;; *) if [ "$1" -le "$3" ]; then ok "$2 ($1 ms)"; else bad "$2: $1 ms > $3"; fi ;; esac; }
partner() { SET 0x12 "$2"; SET 0x11 "$1"; }	# status, type

# gpio-sim bank "rp-sim": line 0 = GPIO94 (vbus), line 1 = GPIO72 (switch)
G=/sys/kernel/config/gpio-sim/rp
mkdir $G $G/bank0; echo 2 > $G/bank0/num_lines; echo rp-sim > $G/bank0/label; echo 1 > $G/live
SIM=/sys/devices/platform/$(cat $G/dev_name)/$(cat $G/bank0/chip_name)
eq "$(LN)" 00 "gpio-sim lines idle before any driver"
A=/sys/bus/i2c/devices/i2c-0

# 1. wrong device ID: probe fails, VBUS stays off
SET 0x01 0x34; SET 0x02 0x04
echo gemini-right-port 0x25 > $A/new_device; usleep 300000
eq "$([ -e $D/driver ] && echo bound || echo unbound)" unbound "wrong ID: driver does not bind"
eq "$(LN)" 00 "wrong ID: lines low"
eq "$(GET 0x02)" 0x04 "wrong ID: Modes untouched"
echo 0x25 > $A/delete_device

# 2. real ID, chip left in SINK with a charger attached and VBUS (power-on case)
SET 0x01 0x12; SET 0x02 0x04; partner 0x29 0x08
echo gemini-right-port 0x25 > $A/new_device
eq "$([ -e $D/driver ] && echo bound || echo unbound)" bound "probe binds"
eq "$(GET 0x02)" 0x01 "probe sets Modes SOURCE"
r=$(waitfor 00 foreign-vbus 1000); under "$r" "charger VBUS from SINK days: foreign-vbus, off" 1000
partner 0x00 0x00; r=$(waitfor 00 idle 1000); under "$r" "charger detached after SOURCE: idle" 1000

# 3. a charger attached (as seen on the device: status 0x29, type 0x08) for 2 s
partner 0x29 0x08; eq "$(holds 00 2000)" held "charger 2 s: never VBUS"; eq "$(ST)" foreign-vbus "charger state"
partner 0x29 0x18; eq "$(holds 00 1000)" held "source+sink type: never VBUS"
partner 0x21 0x08; eq "$(holds 00 1000)" held "attached source, no VBUS yet: never VBUS"; eq "$(ST)" other-partner "source partner state"
# 4. A-to-C cable from a USB-A charger: VBUS, no attach
partner 0x08 0x00; eq "$(holds 00 1000)" held "A-to-C VBUS: never VBUS"; eq "$(ST)" foreign-vbus "A-to-C state"
partner 0x00 0x00; waitfor 00 idle 1000 >/dev/null

# 5. USB stick on CC1: not at once, on after two polls
partner 0x11 0x10; usleep 100000
eq "$(LN)" 00 "stick: still off 100 ms after attach"
r=$(waitfor 11 sourcing 1500); under "$r" "stick CC1: VBUS on (GPIO94 and GPIO72)" 1500
# 6. our own VBUS shows up
partner 0x19 0x10; eq "$(holds 11 1000)" held "own VBUSOK: stays on"
# 7. unplug (VBUS still decaying): off within one poll
SET 0x11 0x08; SET 0x12 0x00; r=$(waitfor 00 foreign-vbus 1000); under "$r" "unplug: VBUS off" 350
SET 0x11 0x00; waitfor 00 idle 1000 >/dev/null
# 8. the swap: charger into the right port right after
partner 0x29 0x08; eq "$(holds 00 1500)" held "swap to charger: never VBUS"
partner 0x00 0x00; waitfor 00 idle 1000 >/dev/null

# 9. CC2 through an active cable
partner 0x21 0x14; r=$(waitfor 11 sourcing 1500); under "$r" "stick CC2, active cable: on" 1500
# 10. Modes changed to DRP behind the driver (main's rb-gemini-typec)
SET 0x02 0x10; r=$(waitfor 00 mode-changed 1000); under "$r" "Modes DRP: VBUS off" 350
usleep 50000; eq "$(GET 0x02)" 0x01 "Modes restored to SOURCE"
r=$(waitfor 11 sourcing 1500); under "$r" "after restore: on again" 1500
eq "$(grep -o 'mode_restores=[0-9]*' $D/stats)" mode_restores=1 "stats count the restore"
# 11. allow_source
echo 0 > /sys/module/gemini_right_port/parameters/allow_source
r=$(waitfor 00 disabled 1000); under "$r" "allow_source=0: off" 350
eq "$(holds 00 1000)" held "allow_source=0: stays off"
echo 1 > /sys/module/gemini_right_port/parameters/allow_source
r=$(waitfor 11 sourcing 1500); under "$r" "allow_source=1: on" 1500
# 12. partner turns into a source while we source
partner 0x29 0x08; r=$(waitfor 00 foreign-vbus 1000); under "$r" "partner became a source: off" 350
partner 0x11 0x10; r=$(waitfor 11 sourcing 1500); under "$r" "stick again: on" 1500
eq "$(cat $D/regs)" "ok=1 mode=0x01 status=0x11 type=0x10 pad94=1 pad72=1" "regs while sourcing"
eq "$(cat $D/vbus)" 1 "vbus attribute"
# 13. unbind while sourcing: off at once, Modes stays SOURCE
echo 0-0025 > $DRV/unbind
eq "$(LN)" 00 "unbind: lines low"
eq "$(GET 0x02)" 0x01 "unbind: Modes stays SOURCE"
eq "$(holds 00 600)" held "unbound: stays off"
# 14. bind again, then remove the device while sourcing
echo 0-0025 > $DRV/bind
r=$(waitfor 11 sourcing 1500); under "$r" "rebind: on again" 1500
echo 0x25 > $A/delete_device
eq "$(LN)" 00 "delete_device: lines low"
# the log lines the on-device plan greps for
for m in "in SOURCE mode; VBUS only for a USB device" "USB device attached (CC1): VBUS on" \
         "USB device attached (CC2): VBUS on" "Modes was 0x10, changed outside this driver" \
         "nothing attached: VBUS" "device ID 0x34 is not a FUSB301A"; do
  dmesg | grep -qF "$m" && ok "dmesg: $m" || bad "dmesg missing: $m"
done
dmesg | grep 'gemini-right-port' | tail -n 40
echo "RPTEST DONE pass=$PASS fail=$FAIL"
poweroff -f
EOF
chmod 0755 "$R/init"
(cd "$R" && find . | LC_ALL=C sort | cpio -o -H newc --quiet | gzip -9) > "$T/test.cpio.gz"

# ---- run --------------------------------------------------------------------------------
log "qemu: virt cortex-a72"
set +e
timeout 300 qemu-system-aarch64 -M virt -cpu cortex-a72 -smp 2 -m 512 -nographic -no-reboot \
  -kernel "$O/arch/arm64/boot/Image" -initrd "$T/test.cpio.gz" \
  -append "console=ttyAMA0 loglevel=7 panic=-1" \
  < /dev/null > "$RUN/serial.log" 2>&1
rc=$?
set -e
grep -a 'RPTEST' "$RUN/serial.log" | tr -d '\r' > "$RUN/results.txt" || true
cat "$RUN/results.txt"
[ "$KEEP" = 1 ] || rm -rf "$T"
grep -q 'RPTEST DONE pass=[0-9]* fail=0$' "$RUN/results.txt" || die "rightport QEMU test failed (qemu rc $rc; $RUN/serial.log)"
log "PASS: $(grep -o 'pass=[0-9]*' "$RUN/results.txt"), log $RUN/serial.log"
