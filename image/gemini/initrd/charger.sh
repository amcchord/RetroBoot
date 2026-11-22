# RetroBoot initrd: LK off-mode-charging boot on the Gemini PDA (sourced by /init).
#
# A charger plugged into a switched-off Gemini powers the PMIC on, and LK boots p22 in
# its off-mode-charging mode (/chosen atag,boot mode 8; 9 is the low-battery variant).
# RetroBoot draws nothing here: the panel keeps whatever LK showed, and the backlight
# goes off after RB_CHG_BL_TIMEOUT seconds without a key press. Then:
#   - KEY_POWER (Esc/On held; mt6351-keys reports it after ~800 ms) → return, and /init
#     boots RetroBoot normally;
#   - the charger reports offline on two consecutive polls → power off;
#   - anything missing (no charger supply, no input devices) → return at once.
# The linux filesystem is never touched in this mode. Like upstream's charger.sh, the
# A53 cores 1-7 go offline and every cpufreq policy is held at its minimum while
# waiting (no idle states yet; the device warmed up on the charger), restored on return.
#
# Paths can be pointed at a fake tree for tests: RB_CHG_SYS, RB_CHG_DEV, RB_CHG_RUN,
# RB_CHG_POWEROFF (command), RB_CHG_POLL (s).

RB_CHG_SYS=${RB_CHG_SYS:-/sys}
RB_CHG_DEV=${RB_CHG_DEV:-/dev}
RB_CHG_RUN=${RB_CHG_RUN:-/run/retroboot-initrd/charger}
RB_CHG_POWEROFF=${RB_CHG_POWEROFF:-poweroff -f}
RB_CHG_POLL=${RB_CHG_POLL:-1}
RB_CHG_BL_TIMEOUT=${RB_CHG_BL_TIMEOUT:-10}
RB_CHG_CPUS_OFF="1 2 3 4 5 6 7"
RB_CHG_READERS=

command -v rb_log >/dev/null 2>&1 || rb_log() { echo "rb-initrd: $*"; }

rb_chg_supply() {  # the charger power_supply directory (bq2589x first)
    for s in "$RB_CHG_SYS"/class/power_supply/bq2589*; do
        [ -f "$s/online" ] && { echo "$s"; return 0; }
    done
    for s in "$RB_CHG_SYS"/class/power_supply/*; do
        [ -f "$s/online" ] || continue
        case "$(cat "$s/type" 2>/dev/null)" in USB|Mains) echo "$s"; return 0 ;; esac
    done
    return 1
}

rb_chg_backlight() {  # on|off
    for b in "$RB_CHG_SYS"/class/backlight/*; do
        [ -f "$b/bl_power" ] || continue
        if [ "$1" = on ]; then echo 0 > "$b/bl_power"; else echo 4 > "$b/bl_power"; fi 2>/dev/null
    done
}

rb_chg_cpus_low() {  # idempotent: the A72 policy appears a few seconds into boot
    for c in $RB_CHG_CPUS_OFF; do
        f=$RB_CHG_SYS/devices/system/cpu/cpu$c/online
        [ "$(cat "$f" 2>/dev/null)" = 1 ] || continue
        echo 0 > "$f" 2>/dev/null && echo "$c" >> "$RB_CHG_RUN/cpus-off"
    done
    for p in "$RB_CHG_SYS"/devices/system/cpu/cpufreq/policy*; do
        [ -f "$p/scaling_max_freq" ] || continue
        n=${p##*/}
        [ -f "$RB_CHG_RUN/maxf.$n" ] && continue
        cat "$p/scaling_max_freq" > "$RB_CHG_RUN/maxf.$n" 2>/dev/null
        cat "$p/cpuinfo_min_freq" > "$p/scaling_max_freq" 2>/dev/null
    done
}

rb_chg_cpus_restore() {
    if [ -f "$RB_CHG_RUN/cpus-off" ]; then
        for c in $(cat "$RB_CHG_RUN/cpus-off"); do
            echo 1 > "$RB_CHG_SYS/devices/system/cpu/cpu$c/online" 2>/dev/null || rb_log "charger: cpu$c did not come back online"
        done
        rm -f "$RB_CHG_RUN/cpus-off"
    fi
    for f in "$RB_CHG_RUN"/maxf.*; do
        [ -f "$f" ] || continue
        cat "$f" > "$RB_CHG_SYS/devices/system/cpu/cpufreq/${f##*/maxf.}/scaling_max_freq" 2>/dev/null
        rm -f "$f"
    done
}

# One reader per input device (touchscreens skipped). struct input_event on arm64 is
# 24 bytes: timeval 16, u16 type @16, u16 code @18, s32 value @20. One open file per
# device for the whole loop (evdev queues per open file). Each key press appends its
# code to $RB_CHG_RUN/keys.
rb_chg_start_readers() {
    : > "$RB_CHG_RUN/keys"
    for d in "$RB_CHG_SYS"/class/input/event*; do
        [ -e "$d" ] || continue
        case "$(cat "$d/device/name" 2>/dev/null)" in *ouch*) continue ;; esac
        ev=${d##*/}
        dev=$RB_CHG_DEV/input/$ev
        [ -c "$dev" ] || [ -p "$dev" ] || continue
        (
            exec 3< "$dev" || exit 0
            ddp=
            trap 'kill $ddp 2>/dev/null; exit 0' TERM
            while :; do
                dd bs=24 count=1 of="$RB_CHG_RUN/ev.$ev" <&3 2>/dev/null &
                ddp=$!
                wait $ddp || { sleep 1; continue; }
                sz=$(wc -c < "$RB_CHG_RUN/ev.$ev")
                [ $sz -eq 24 ] || { sleep 1; continue; }
                set -- $(od -An -tu2 -j16 -N4 "$RB_CHG_RUN/ev.$ev")
                v=$(od -An -td4 -j20 -N4 "$RB_CHG_RUN/ev.$ev")
                if [ "$1" = 1 ] && [ $v -eq 1 ]; then echo "$2" >> "$RB_CHG_RUN/keys"; fi
            done
        ) &
        RB_CHG_READERS="$RB_CHG_READERS $!"
    done
}

rb_chg_leave() {
    for p in $RB_CHG_READERS; do kill "$p" 2>/dev/null; done
    RB_CHG_READERS=
    rb_chg_cpus_restore
    rb_chg_backlight on
}

# Returns when the normal boot should continue; powers off when the charger goes away.
rb_charger_main() {
    mkdir -p "$RB_CHG_RUN"
    chg=$(rb_chg_supply) || { rb_log "charger boot (mode $RB_BOOT_MODE): no charger supply, booting normally"; return 0; }
    rb_log "charger boot (mode $RB_BOOT_MODE) on ${chg##*/}: hold Esc/On to start RetroBoot, unplug to switch off"
    rb_chg_start_readers
    [ -n "$RB_CHG_READERS" ] || rb_log "charger: no input devices (unplugging still switches off)"
    rb_chg_backlight on
    lit=1; lit_at=$(cut -d. -f1 /proc/uptime); seen=0; off_n=0
    while :; do
        now=$(cut -d. -f1 /proc/uptime)
        rb_chg_cpus_low
        if [ "$(cat "$chg/online" 2>/dev/null)" = 1 ]; then
            off_n=0
        else
            off_n=$((off_n + 1))
            if [ "$off_n" -ge 2 ]; then
                rb_log "charger: unplugged, switching off"
                rb_chg_leave
                sync
                $RB_CHG_POWEROFF
                sleep 5
                rb_log "charger: power-off returned, booting normally"
                return 0
            fi
        fi
        n=$(wc -l < "$RB_CHG_RUN/keys")
        if [ $n -gt $seen ]; then
            codes=$(sed -n "$((seen + 1)),${n}p" "$RB_CHG_RUN/keys")
            seen=$n
            [ "$lit" = 1 ] || { rb_chg_backlight on; lit=1; }
            lit_at=$now
            for code in $codes; do
                if [ "$code" = 116 ]; then
                    rb_log "charger: power key, starting RetroBoot"
                    rb_chg_leave
                    return 0
                fi
            done
        fi
        if [ "$lit" = 1 ] && [ $((now - lit_at)) -ge "$RB_CHG_BL_TIMEOUT" ]; then
            rb_chg_backlight off; lit=0
        fi
        sleep "$RB_CHG_POLL"
    done
}
