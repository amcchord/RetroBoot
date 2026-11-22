#!/bin/sh
# Wave 3 session scenarios for rb-presenter (lab tool, run as root on rb-dev).
# Like fake_suite.sh: runs the given presenter binary (default: the ASan
# build) in the foreground, drives it with mini_hostd + fake_qemu, logs to
# $OUT. Pair with tools/outer_capture.py on the lab host.
#
#   R  startup gray painted top-down (160 ms): reveal only on the complete frame
#   H  guest disconnect after a black "shut down" screen: the last frame is held
#      (no checkerboard) until hostd sets a scene; then black / splash
#   N  pointer_nudge: +1 px and back, status.guest damage_seq/last_damage_ms
#   S  one RESIZE (the target) at guest connect and no re-request by default;
#      enforce_guest_size adds the Wave 2 re-requests after content
#
#   session_suite.sh [presenter-binary] [out-dir]
set -u
T=$(cd "$(dirname "$0")" && pwd)
BIN=${1:-$T/../build-asan/rb-presenter}
OUT=${2:-/tmp/session-suite}
FIFO=/tmp/session.fifo
rm -rf "$OUT"; mkdir -p "$OUT"

req() { echo "$1" > "$FIFO"; sleep "${2:-0.3}"; }
mark() { echo "{\"mark\":\"$1\",\"t\":$(cut -d' ' -f1 /proc/uptime)}" | tee -a "$OUT/marks.log"; }

systemctl stop retroboot-presenter.service
ASAN_OPTIONS=detect_leaks=1:abort_on_error=0 UBSAN_OPTIONS=print_stacktrace=1 \
    "$BIN" --restore-vt > "$OUT/presenter.log" 2>&1 &
PRES=$!
sleep 1.5
rm -f "$FIFO"
python3 "$T/mini_hostd.py" --fifo "$FIFO" > "$OUT/hostd.log" 2>&1 &
HOSTD=$!
sleep 1

mark "R top-down startup gray"
python3 "$T/fake_qemu.py" --size 1280x800 --openbios 2 --paint-ms 160 --gray 2 --desktop 2 --static \
    > "$OUT/fake-R.log" 2>&1

mark "H shutdown screen then disconnect: hold"
req '{"cmd":"set_scene","scene":"guest","reveal":"first-content"}'
python3 "$T/fake_qemu.py" --size 1280x800 --openbios 1 --gray 1 --desktop 2 --static --end-black 1.5 \
    > "$OUT/fake-H.log" 2>&1
sleep 3            # held (black from the guest, not the checkerboard)
req '{"cmd":"status"}' 0.5
req '{"cmd":"set_scene","scene":"black"}' 1.5
mark "H2 desktop then disconnect: hold the desktop, then splash"
req '{"cmd":"set_scene","scene":"guest","reveal":"first-content"}'
python3 "$T/fake_qemu.py" --size 1280x800 --openbios 1 --gray 1 --desktop 2 --static > "$OUT/fake-H2.log" 2>&1
sleep 3            # held desktop frame
req '{"cmd":"set_scene","scene":"splash"}' 1.5

mark "N pointer nudge"
req '{"cmd":"set_scene","scene":"guest","reveal":"first-content"}'
python3 "$T/fake_qemu.py" --size 1280x800 --openbios 0.5 --gray 0.5 --desktop 6 --static --log-motion \
    > "$OUT/fake-N.log" 2>&1 &
FAKE=$!
sleep 3
req '{"cmd":"status"}' 0.5
req '{"cmd":"pointer_nudge"}' 0.5
req '{"cmd":"pointer_nudge"}' 0.05
req '{"cmd":"status"}' 0.5
wait "$FAKE"

mark "S RESIZE policy"
req '{"cmd":"set_scene","scene":"guest","reveal":"first-content"}'
python3 "$T/fake_qemu.py" --size 1024x768 --openbios 0.5 --gray 0.5 --desktop 1 --static > "$OUT/fake-S1.log" 2>&1
req '{"cmd":"display_config","enforce_guest_size":true}'
python3 "$T/fake_qemu.py" --size 1024x768 --openbios 0.5 --gray 0.5 --desktop 3 --static > "$OUT/fake-S2.log" 2>&1
req '{"cmd":"display_config","enforce_guest_size":false}'
req '{"cmd":"set_scene","scene":"splash"}' 0.5

mark "done"
kill "$HOSTD" 2>/dev/null
kill -TERM "$PRES"
wait "$PRES" 2>/dev/null
systemctl start retroboot-presenter.service
echo "presenter exit; sanitizer findings:"
grep -E "ERROR: AddressSanitizer|runtime error|LeakSanitizer" "$OUT/presenter.log" || echo "  none"
