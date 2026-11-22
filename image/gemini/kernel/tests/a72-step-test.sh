#!/bin/sh
# A72 (cpu8/9) OPP step test for RetroBoot's a72dvfs kernel. Runs ON THE GEMINI, as
# root, under RetroBoot (Debian 13) with rb-cpumhz next to this script.
#
#   a72-step-test.sh [-s SECONDS] [-l LIMIT_C] [-o LOG] [FREQ_KHZ ...]
#
# Default steps 1495000 1677000 1885000 2093000; give higher ones explicitly, one
# at a time, after the lower ones passed (2158000 2223000 2262000 2327000, then
# 2392000 or 2431000, then 2522000 or 2587000; the kernel only offers the ones of
# this chip's speed bin, see `dmesg | grep "A72 speed bin"`).
#
# Per step:
#   - policy8 scaling_max_freq = step, governor performance;
#   - both A72s run `rb-cpumhz --burn`; cpu8's burner pauses (SIGSTOP) for each
#     ~1 s measurement;
#   - every 5 s it logs measured MHz, scaling_cur_freq, VPROC2 and VSRAM_B
#     microvolts, every thermal zone, and the A72 cooling state;
#   - FAIL when: a new kernel error appears in dmesg (oops, BUG, lockup, RCU stall,
#     watchdog, cpufreq/regulator/SMC errors); a zone reaches LIMIT_C (default
#     90 C); or the measured clock stays below 97 % of the step while the A72
#     cooling state is 0 (not thermally capped).
# Thermal capping by the 80/85 C trips is reported, not a failure. Stops at the first
# failure. Always restores the governor and scaling_max_freq (boot cap 1495 MHz) and
# kills the load, also on Ctrl-C.
#
# Rollback, if a step hangs or misbehaves:
#   - Force off: hold Esc/On for about 10 s. Every boot starts capped at the proven
#     1495 MHz, so the next boot is safe as long as nothing raises the cap
#     automatically.
#   - Gemian: from RetroBoot,
#       printf boot-recovery | dd of=/dev/disk/by-partlabel/para bs=32 count=1 conv=sync,notrunc; sync; reboot
#     or hold Esc/On + the silver button at power-on.
#   - Previous kernel: from Gemian, dd the previous boot.img (the dual-touch
#     candidate) to /dev/disk/by-partlabel/boot with conv=fsync. Then clear para:
#       dd if=/dev/zero of=/dev/disk/by-partlabel/para bs=32 count=1 conv=notrunc
set -u
SECS=120
LIMIT=90
LOG=/var/log/retroboot-a72-step-$(date +%Y%m%d-%H%M%S).log
while getopts s:l:o: o; do
  case $o in s) SECS=$OPTARG ;; l) LIMIT=$OPTARG ;; o) LOG=$OPTARG ;; *) exit 2 ;; esac
done
shift $((OPTIND - 1))
STEPS=${*:-1495000 1677000 1885000 2093000}
HERE=$(cd "$(dirname "$0")" && pwd)
MHZ=$HERE/rb-cpumhz
P=/sys/devices/system/cpu/cpufreq/policy8
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
[ -x "$MHZ" ] || { echo "missing $MHZ"; exit 1; }
[ -d "$P" ] || { echo "no $P (A72s offline or no cpufreq)"; exit 1; }

say() { echo "$*" | tee -a "$LOG"; }
reg_uv() {  # reg_uv NAME -> microvolts of the regulator called NAME
  for r in /sys/class/regulator/regulator.*; do
    [ "$(cat "$r/name" 2>/dev/null)" = "$1" ] && { cat "$r/microvolts" 2>/dev/null; return; }
  done
  echo "?"
}
temps() {
  for z in /sys/class/thermal/thermal_zone*; do
    t=$(cat "$z/temp" 2>/dev/null) || continue
    printf '%s=%s ' "$(cat "$z/type")" "$((t / 1000))"
  done
}
max_temp() {
  m=0
  for z in /sys/class/thermal/thermal_zone*/temp; do
    t=$(cat "$z" 2>/dev/null) || continue; [ "$t" -gt "$m" ] && m=$t
  done
  echo $((m / 1000))
}
cdev8() {
  for c in /sys/class/thermal/cooling_device*; do
    case "$(cat "$c/type")" in cpufreq-cpu8) cat "$c/cur_state"; return ;; esac
  done
  echo "?"
}
ERRPAT='oops|bug:|panic|rcu.*stall|soft lockup|hard lockup|watchdog|cpufreq.*(fail|refused|error)|vsram|sramldo|readback|armpll_b.*refused|proc rail is off|da9211.*(fail|error)|i2c.*(timeout|error)|unable to handle'

ORIG_MAX=$(cat "$P/scaling_max_freq"); ORIG_GOV=$(cat "$P/scaling_governor")
BURN=""
restore() {
  [ -n "$BURN" ] && kill -CONT $BURN 2>/dev/null && kill $BURN 2>/dev/null
  echo "$ORIG_GOV" > "$P/scaling_governor" 2>/dev/null
  echo "$ORIG_MAX" > "$P/scaling_max_freq" 2>/dev/null
  say "restored: governor $ORIG_GOV, scaling_max_freq $ORIG_MAX"
}
trap 'restore; exit 130' INT TERM

say "== RetroBoot a72dvfs step test $(date -Is), kernel $(uname -r)"
say "$(dmesg | grep -E 'A72 speed bin|cpu8: .*OPPs|boot cap|sramldo|mt6797-sramldo' | tail -n 5)"
say "cpuinfo_max_freq $(cat "$P/cpuinfo_max_freq"), available: $(cat "$P/scaling_available_frequencies")"
say "steps: $STEPS; ${SECS} s each; temperature limit ${LIMIT} C; log $LOG"
RESULT=PASS
for step in $STEPS; do
  if ! grep -qw "$step" "$P/scaling_available_frequencies"; then
    say "-- $step kHz is not offered by this kernel/bin: skipped"; continue
  fi
  D0=$(dmesg | wc -l)
  echo performance > "$P/scaling_governor"
  echo "$step" > "$P/scaling_max_freq"
  "$MHZ" --burn 8 & B8=$!
  "$MHZ" --burn 9 & B9=$!
  BURN="$B8 $B9"
  say "-- step $step kHz: VPROC2 $(reg_uv vproc2) uV, VSRAM_B $(reg_uv vsram_b) uV"
  t=0; lowcnt=0; sum=0; n=0; peak=0; capped=0
  while [ $t -lt "$SECS" ]; do
    kill -STOP "$B8"; m=$("$MHZ" 8 200); kill -CONT "$B8"
    cs=$(cdev8); mt=$(max_temp)
    say "  t=${t}s cpu8 ${m} MHz cur $(cat "$P/scaling_cur_freq") VPROC2 $(reg_uv vproc2) VSRAM_B $(reg_uv vsram_b) cdev8 $cs $(temps)"
    [ "$mt" -gt "$peak" ] && peak=$mt
    [ "$cs" != 0 ] && capped=1
    sum=$((sum + m)); n=$((n + 1))
    if [ "$cs" = 0 ] && [ $((m * 1000)) -lt $((step * 97 / 100)) ]; then lowcnt=$((lowcnt + 1)); else lowcnt=0; fi
    if [ "$mt" -ge "$LIMIT" ]; then RESULT="FAIL (temperature ${mt} C >= ${LIMIT} C)"; break; fi
    if [ $lowcnt -ge 3 ]; then RESULT="FAIL (clock ${m} MHz below 97 % of the step, not thermally capped)"; break; fi
    if dmesg | tail -n +$((D0 + 1)) | grep -iqE "$ERRPAT"; then RESULT="FAIL (kernel error, see below)"; break; fi
    sleep 4; t=$((t + 5))
  done
  kill $BURN 2>/dev/null; BURN=""
  NEW=$(dmesg | tail -n +$((D0 + 1)))
  [ -n "$NEW" ] && { say "  new kernel messages:"; echo "$NEW" | sed 's/^/    /' | tee -a "$LOG"; }
  say "  step $step: avg $((n ? sum / n : 0)) MHz over $n samples, peak ${peak} C, thermally capped: $([ $capped = 1 ] && echo yes || echo no) -> $RESULT"
  [ "$RESULT" = PASS ] || break
  sleep 20   # cool down a little between steps
done
restore
say "== result: $RESULT"
[ "$RESULT" = PASS ]
