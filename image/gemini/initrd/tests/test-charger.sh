#!/bin/bash
# Unit test of the initrd's charger wait loop (charger.sh) on rb-builder: the initrd's
# own aarch64 busybox runs it under qemu-user (binfmt) against a fake sysfs and FIFO
# input devices. Cases: power key continues (cores, cpufreq and backlight restored),
# other keys only wake the backlight, the backlight times out, unplugging powers off,
# a generic USB supply is accepted, no supply returns at once.
#
# Usage: test-charger.sh [--initrd FILE]
set -euo pipefail
WORK=${RB_GEMINI_WORK:-/srv/retroboot/wave7/gemini-kernel}
INITRD=$WORK/out/initrd/latest/initrd.cpio.gz
[ "${1:-}" != --initrd ] || INITRD=$2
T=$(mktemp -d /tmp/rb-charger-test.XXXXXX)
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/ix"
(cd "$T/ix" && gzip -dc "$INITRD" | cpio --quiet -id 2>/dev/null)
BB=$T/ix/bin/busybox
PASSES=0; FAILS=0
ok() { if "${@:2}"; then PASSES=$((PASSES + 1)); echo "  PASS $1"; else FAILS=$((FAILS + 1)); echo "  FAIL $1"; fi; }

fake_tree() {  # fake_tree SUPPLY(bq|usb|none)
  rm -rf "$T/sys" "$T/dev" "$T/run"; mkdir -p "$T/run" "$T/dev/input"
  case "$1" in
    bq)  mkdir -p "$T/sys/class/power_supply/bq25890-charger"; echo 1 > "$T/sys/class/power_supply/bq25890-charger/online"
         echo USB > "$T/sys/class/power_supply/bq25890-charger/type"; SUPPLY=$T/sys/class/power_supply/bq25890-charger ;;
    usb) mkdir -p "$T/sys/class/power_supply/usb"; echo 1 > "$T/sys/class/power_supply/usb/online"
         echo USB > "$T/sys/class/power_supply/usb/type"; SUPPLY=$T/sys/class/power_supply/usb ;;
    none) mkdir -p "$T/sys/class/power_supply"; SUPPLY= ;;
  esac
  mkdir -p "$T/sys/class/backlight/pwm-backlight"; echo 0 > "$T/sys/class/backlight/pwm-backlight/bl_power"
  for c in 1 2 3 4 5 6 7 8 9; do mkdir -p "$T/sys/devices/system/cpu/cpu$c"; echo 1 > "$T/sys/devices/system/cpu/cpu$c/online"; done
  for p in 0 4 8; do
    mkdir -p "$T/sys/devices/system/cpu/cpufreq/policy$p"
    echo 1500000 > "$T/sys/devices/system/cpu/cpufreq/policy$p/scaling_max_freq"
    echo 221000 > "$T/sys/devices/system/cpu/cpufreq/policy$p/cpuinfo_min_freq"
  done
  for e in 0 1; do mkdir -p "$T/sys/class/input/event$e/device"; mkfifo "$T/dev/input/event$e"; done
  echo mtk-pmic-keys > "$T/sys/class/input/event0/device/name"
  echo "Novatek Touchscreen" > "$T/sys/class/input/event1/device/name"
}
start_charger() {
  rm -f "$T/poweroff" "$T/out"
  exec 7<>"$T/dev/input/event0" 8<>"$T/dev/input/event1"
  env -i PATH="$T/ix/bin" RB_CHG_SYS="$T/sys" RB_CHG_DEV="$T/dev" RB_CHG_RUN="$T/run/charger" \
      RB_CHG_POWEROFF="touch $T/poweroff" RB_CHG_POLL=0.2 RB_CHG_BL_TIMEOUT=2 RB_BOOT_MODE=8 \
      "$BB" sh -c ". $T/ix/lib/retroboot/charger.sh; rb_charger_main; echo rc=\$?" > "$T/out" 2>&1 &
  CPID=$!
}
key() {  # key FD CODE — one EV_KEY press (struct input_event, arm64 layout)
  python3 -c "import struct,sys; sys.stdout.buffer.write(struct.pack('<qqHHi',0,0,1,int(sys.argv[1]),1))" "$2" >&"$1"
}
wait_done() { local i=0; while kill -0 "$CPID" 2>/dev/null && [ $i -lt 100 ]; do sleep 0.2; i=$((i + 1)); done; ! kill -0 "$CPID" 2>/dev/null; }
close_fds() { exec 7>&- 8>&-; }
cat_() { cat "$1" 2>/dev/null; }

echo "case: power key continues the boot"
fake_tree bq; start_charger
sleep 3.5
ok "cores 1-7 offline while waiting" [ "$(cat_ "$T/sys/devices/system/cpu/cpu3/online")$(cat_ "$T/sys/devices/system/cpu/cpu8/online")" = 01 ]
ok "cpufreq held at minimum" [ "$(cat_ "$T/sys/devices/system/cpu/cpufreq/policy8/scaling_max_freq")" = 221000 ]
ok "backlight off after the timeout" [ "$(cat_ "$T/sys/class/backlight/pwm-backlight/bl_power")" = 4 ]
key 8 116; sleep 1
ok "touchscreen events ignored" kill -0 "$CPID"
key 7 30; sleep 0.6
ok "other key wakes the backlight" [ "$(cat_ "$T/sys/class/backlight/pwm-backlight/bl_power")" = 0 ]
ok "other key does not start the boot" kill -0 "$CPID"
key 7 116
ok "power key returns" wait_done
ok "returned 0" grep -q '^rc=0' "$T/out"
ok "logged" grep -q 'power key, starting RetroBoot' "$T/out"
ok "cores restored" [ "$(cat_ "$T/sys/devices/system/cpu/cpu3/online")" = 1 ]
ok "cpufreq restored" [ "$(cat_ "$T/sys/devices/system/cpu/cpufreq/policy8/scaling_max_freq")" = 1500000 ]
ok "backlight on" [ "$(cat_ "$T/sys/class/backlight/pwm-backlight/bl_power")" = 0 ]
ok "no power-off" [ ! -e "$T/poweroff" ]
sleep 0.5
ok "reader processes gone" sh -c "! pgrep -f '[r]b-charger-test.*ev[.]event' >/dev/null"
close_fds

echo "case: unplugging powers off"
fake_tree bq; start_charger
sleep 1; echo 0 > "$SUPPLY/online"
ok "power-off requested, then returns" wait_done
ok "power-off command ran" [ -e "$T/poweroff" ]
ok "logged" grep -q 'unplugged, switching off' "$T/out"
close_fds

echo "case: generic USB supply"
fake_tree usb; start_charger
sleep 1; key 7 116
ok "returns on power key" wait_done
ok "used the USB supply" grep -q 'on usb:' "$T/out"
close_fds

echo "case: no charger supply"
fake_tree none; start_charger
ok "returns at once" wait_done
ok "logged" grep -q 'no charger supply, booting normally' "$T/out"
close_fds

echo "charger: $PASSES passed, $FAILS failed"
[ "$FAILS" = 0 ]
