#!/bin/bash
# rbd_hostidle.sh SECONDS [PID]
#
# Read-only idle measurement on an appliance (emulator/reports/idle-power-20261011.md):
# QEMU CPU (utime+stime) per thread and in total, wakeups (voluntary context
# switches), whole-machine busy, package energy (powercap: Intel RAPL or AMD),
# battery discharge power, mean CPU frequency, governor and EPP.  Changes
# nothing; needs only bash and awk.  Leave Mac OS idle at the Finder for at
# least 60 s first; keep brightness, Wi-Fi and AC state the same between runs.
set -u
DUR=${1:-120}
PID=${2:-$(pgrep -o -f qemu-system-ppc)}
[ -n "$PID" ] || { echo "no qemu-system-ppc running" >&2; exit 1; }
HZ=$(getconf CLK_TCK)

snap() {     # tid comm ticks vcs, one line per thread (comm without spaces)
    local t
    for t in /proc/$PID/task/*; do
        awk -v tid="${t##*/}" -v comm="$(tr ' ' '_' < $t/comm)" \
            'FNR==NR {split($0, a, ") "); split(a[2], f, " "); ticks = f[12] + f[13]; next}
             /^voluntary_ctxt_switches/ {print tid, comm, ticks, $2}' $t/stat $t/status 2>/dev/null
    done
}
proc_ticks() { awk '{split($0, a, ") "); split(a[2], f, " "); print f[12] + f[13]}' /proc/$PID/stat; }
stat_busy() { awk '/^cpu / {idle = $5 + $6; t = 0; for (i = 2; i <= NF; i++) t += $i; print t - idle, t}' /proc/stat; }
energy() {   # sum of top-level powercap zones in uJ, or empty
    local s=0 f n=0
    for f in /sys/class/powercap/*/energy_uj; do
        case "$f" in *:*:*) continue ;; esac
        [ -r "$f" ] && { s=$((s + $(cat "$f"))); n=$((n + 1)); }
    done
    [ $n -gt 0 ] && echo $s
}
batt() {     # discharge power in uW, or empty
    local b
    for b in /sys/class/power_supply/BAT* /sys/class/power_supply/battery; do
        [ -d "$b" ] || continue
        if [ -r "$b/power_now" ]; then cat "$b/power_now"; return; fi
        if [ -r "$b/current_now" ] && [ -r "$b/voltage_now" ]; then
            awk -v i="$(cat $b/current_now)" -v v="$(cat $b/voltage_now)" 'BEGIN {printf "%d\n", i * v / 1e6}'; return
        fi
    done
}

echo "host: $(hostname) $(uname -r) $(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2)"
echo "qemu: pid $PID $(readlink /proc/$PID/exe)"
echo "qemu args: $(tr '\0' ' ' < /proc/$PID/cmdline | grep -o -- '-M [^ ]*\|-g [^ ]*\|mmu-contexts=[a-z]*\|idle-[a-z-]*=[0-9]*' | tr '\n' ' ')"
f=/sys/devices/system/cpu/cpu0/cpufreq
[ -d $f ] && echo "cpufreq: $(cat $f/scaling_driver 2>/dev/null) governor=$(cat $f/scaling_governor 2>/dev/null) epp=$(cat $f/energy_performance_preference 2>/dev/null)"
for s in /sys/class/power_supply/*/online; do [ -r "$s" ] && echo "supply $(basename $(dirname $s)) online=$(cat $s)"; done

T0=$(date +%s.%N); K0=$(proc_ticks); S0=$(snap); B0=$(stat_busy); E0=$(energy)
bsum=0; bn=0; fsum=0; fn=0
end=$(( $(date +%s) + DUR ))
while [ "$(date +%s)" -lt "$end" ]; do
    sleep 5
    p=$(batt); [ -n "$p" ] && { bsum=$((bsum + p)); bn=$((bn + 1)); }
    for c in /sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq; do
        [ -r "$c" ] && { fsum=$((fsum + $(cat $c))); fn=$((fn + 1)); }
    done
done
T1=$(date +%s.%N); K1=$(proc_ticks); S1=$(snap); B1=$(stat_busy); E1=$(energy)

awk -v t0="$T0" -v t1="$T1" -v k0="$K0" -v k1="$K1" -v hz="$HZ" -v b0="$B0" -v b1="$B1" \
    -v e0="${E0:-}" -v e1="${E1:-}" -v bsum="$bsum" -v bn="$bn" -v fsum="$fsum" -v fn="$fn" '
BEGIN {
    dt = t1 - t0
    split(b0, x, " "); split(b1, y, " ")
    printf "window_s %.1f\n", dt
    printf "qemu_cpu_pct_of_one_core %.2f\n", (k1 - k0) / hz / dt * 100
    printf "machine_busy_pct %.2f\n", (y[1] - x[1]) / (y[2] - x[2]) * 100
    if (e0 != "" && e1 != "") printf "powercap_watts %.3f\n", (e1 - e0) / 1e6 / dt
    if (bn > 0) printf "battery_discharge_watts %.3f (%d samples)\n", bsum / bn / 1e6, bn
    if (fn > 0) printf "cpu_mean_mhz %.0f\n", fsum / fn / 1000
}'
# per thread role: CPU % and wakeups/s (threads present at both ends)
awk -v dt="$(awk -v a="$T0" -v b="$T1" 'BEGIN {print b - a}')" -v hz="$HZ" -v pid="$PID" '
FNR == NR {t0[$1] = $3; v0[$1] = $4; next}
($1 in t0) {
    role = ($1 == pid) ? "main" : ($2 ~ /TCG|CPU/ ? "vcpu" : "other")
    cpu[role] += ($3 - t0[$1]) / hz / dt * 100; wk[role] += ($4 - v0[$1]) / dt; all += ($4 - v0[$1]) / dt
}
END {
    for (r in cpu) printf "thread_%s cpu_pct %.2f wakeups_per_s %.0f\n", r, cpu[r], wk[r]
    printf "qemu_wakeups_per_s %.0f\n", all
}' <(echo "$S0") <(echo "$S1")
