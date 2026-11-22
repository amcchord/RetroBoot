#!/bin/bash
# Cold-start hunt for the Quadra 800 package (report quadra-m68k-2026-10-24.md, section 2.4).
#
#   rbd_quadra_hunt.sh PKG ROM SEED DISK RUNS PREFIX N [rbd_quadra.py launch options]
#
# N cold starts with the contract argv, each with a fresh copy of DISK and the PRAM
# SEED, stopped as soon as classicvirtio's tablet driver is up (pointer-mode
# absolute: the ROM's tests and delay-loop calibration, the chime, the boot blocks
# and the System file are behind it, about 3 s after power-on). A start without it
# within 40 s keeps RUNS/PREFIX-I with three CPU samples, the first 8 MiB of RAM,
# QEMU's screendump and the presenter's picture; one where QEMU exits keeps its
# message. One line per start: "PREFIX I up S", "... HANG" or "... EXIT S: message".
set -u
TOOLS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG=$1 ROM=$2 SEED=$3 DISK=$4 RUNS=$5 X=$6 N=$7; shift 7
hmp() {  # hmp RUN 'command' ... (QMP human-monitor-command on the control socket)
    local run=$1; shift
    python3 - "$TOOLS" "$run/qmp-control.sock" "$@" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from rbd_lab import QMP
q = QMP(Path(sys.argv[2]))
for line in sys.argv[3:]:
    print(q.hmp(line))
PY
}
mkdir -p "$RUNS"
for i in $(seq 1 "$N"); do
    R=$RUNS/$X-$i; rm -rf "$R"
    python3 "$TOOLS/rbd_quadra.py" launch --pkg "$PKG" --rom "$ROM" --pram-seed "$SEED" --run "$R" \
        --disk "$DISK" "$@" >/dev/null
    Q=$(cat "$R/qemu.pid"); ok=0
    for s in $(seq 1 80); do
        sleep 0.5
        if grep -q '"relative": false' "$R/stub.jsonl" 2>/dev/null; then ok=1; break; fi
        [ -d "/proc/$Q" ] || { ok=2; break; }
    done
    if [ $ok = 1 ]; then
        echo "$X $i up $((s / 2)).$((s % 2 * 5))s load $(cut -d' ' -f1 /proc/loadavg)"
        python3 "$TOOLS/rbd_quadra.py" kill --run "$R" >/dev/null 2>&1; rm -f "$R/disk.img"
    elif [ $ok = 2 ]; then
        echo "$X $i EXIT $((s / 2)).$((s % 2 * 5))s: $(grep -v 'info: retroboot' "$R/qemu.out" | head -2 | tr '\n' ' ')"
        python3 "$TOOLS/rbd_quadra.py" kill --run "$R" >/dev/null 2>&1; rm -f "$R/disk.img"
    else
        echo "$X $i HANG load $(cut -d' ' -f1 /proc/loadavg)"
        for k in 1 2 3; do hmp "$R" 'info registers' 'x/12i $pc' >> "$R/hang-cpu.txt" 2>&1; sleep 0.5; done
        # the file name in quotes: HMP would read "SIZE /path" as a division
        A=$(cd "$R" && pwd)
        hmp "$R" stop "pmemsave 0 0x800000 \"$A/ram-8m.bin\"" "screendump $A/qemu-screen.ppm" \
            'info irq' 'info jit' >> "$R/hang-cpu.txt" 2>&1
        python3 "$TOOLS/rbd_presenter_stub.py" ctl "$R/stub.ctl" \
            "{\"cmd\":\"shot\",\"path\":\"$(cd "$R" && pwd)/hang.png\"}" >/dev/null 2>&1
        python3 "$TOOLS/rbd_quadra.py" kill --run "$R" >/dev/null 2>&1
    fi
    for w in $(seq 1 100); do [ -d "/proc/$Q" ] || break; sleep 0.1; done   # the disk's lock
    [ -d "/proc/$Q" ] && { kill -9 "$Q"; sleep 0.5; }
done
echo "$X hunt-done"
