#!/bin/sh
# Read-only watcher for the rightport on-device test (README "rightport candidate").
# Prints one line whenever the right port's policy state, its controller registers,
# the right-port USB devices, or the left port's charger/gadget state change, so the
# coordinator can follow the user plugging a charger, then a USB stick, into the right
# port. It never writes anything.
#
# Usage: rightport-watch.sh [SECONDS]   (default 120; 0 = one snapshot)
set -u
DUR=${1:-120}
RP=${RB_RIGHTPORT_SYSFS:-/sys/bus/i2c/devices/0-0025}	# override for a dry run
PARAM=/sys/module/gemini_right_port/parameters/allow_source

rd() { cat "$1" 2>/dev/null || echo "?"; }

right_usb() {  # USB devices below the right port's MUSB (usb@11200000)
  out=""
  for d in /sys/bus/usb/devices/*; do
    [ -f "$d/idVendor" ] || continue
    case "$(readlink -f "$d")" in
      *11200000*) case "$(basename "$d")" in usb*) continue ;; esac
                  out="$out $(basename "$d"):$(rd "$d/idVendor"):$(rd "$d/idProduct"):$(rd "$d/product" | tr ' ' _)" ;;
    esac
  done
  echo "${out:- none}"
}

left() {  # the left port: BQ25896 input and the g_ether gadget
  chg=""
  for p in /sys/class/power_supply/*; do
    case "$(rd "$p/type")" in Mains|USB) chg="$chg $(basename "$p") online=$(rd "$p/online") $(rd "$p/status")" ;; esac
  done
  udc=""
  for u in /sys/class/udc/*; do [ -e "$u" ] && udc="$udc $(basename "$u")=$(rd "$u/state")"; done
  echo "charger:${chg:- none} udc:${udc:- none}"
}

snap() {
  if [ ! -d "$RP/driver" ]; then
    echo "right: no gemini-right-port driver bound at $RP (not the rightport kernel?)"
  else
    echo "right: state=$(rd "$RP/state") vbus=$(rd "$RP/vbus") $(rd "$RP/regs") allow_source=$(rd "$PARAM") | usb:$(right_usb) | left: $(left)"
  fi
}

echo "# $(date -u +%FT%TZ) $(uname -r); stats: $(rd "$RP/stats")"
dmesg 2>/dev/null | grep -i 'gemini-right-port\|0-0025' | tail -n 5
last=""
start=$(date +%s)
while :; do
  cur=$(snap)
  if [ "$cur" != "$last" ]; then
    echo "$(date +%T) $cur"
    last=$cur
  fi
  [ "$DUR" -gt 0 ] && [ $(( $(date +%s) - start )) -lt "$DUR" ] || break
  sleep 0.2
done
echo "# stats: $(rd "$RP/stats")"
dmesg 2>/dev/null | grep -i 'gemini-right-port\|0-0025\|usb 1-\|musb' | tail -n 20
