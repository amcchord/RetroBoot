#!/bin/sh
# Fake-QEMU scenario suite for rb-presenter (lab tool, run as root on rb-dev).
# Runs the given presenter binary in the foreground (default: the ASan build)
# instead of the service, drives it with mini_hostd + fake_qemu, and leaves
# logs in $OUT. Pair with tools/outer_capture.py on the lab host.
#
#   fake_suite.sh [presenter-binary] [out-dir]
set -u
T=$(cd "$(dirname "$0")" && pwd)
BIN=${1:-$T/../build-asan/rb-presenter}
OUT=${2:-/tmp/fake-suite}
FIFO=/tmp/suite.fifo
rm -rf "$OUT"; mkdir -p "$OUT"

req() { echo "$1" > "$FIFO"; sleep "${2:-0.3}"; }
fake() { python3 "$T/fake_qemu.py" "$@" >> "$OUT/fake.log" 2>&1; }
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

mark "A integer 640x480 + Escape during OpenBIOS phase"
fake --size 640x480 --openbios 4 --gray 1 --desktop 4 &
FAKE=$!
# The lab host injects Escape (then Return) through the VNC console here.
wait "$FAKE"

mark "B fit scaling 640x480"
req '{"cmd":"display_config","scaling":"fit"}'
req '{"cmd":"set_scene","scene":"guest","reveal":"first-content"}'
fake --size 640x480 --openbios 1 --gray 1 --desktop 5
req '{"cmd":"screenshot","path":"'"$OUT"'/fit-after.png","what":"composed"}' 1

mark "C mode switches 800x600 1024x768 1280x1024 (fit)"
fake --size 640x480 --openbios 0.5 --gray 0.5 --desktop 9 --modes 800x600,1024x768,1280x1024

mark "D integer + follow RESIZE (auto 1280x800)"
req '{"cmd":"display_config","scaling":"integer","guest_size":null}'
fake --size 640x480 --openbios 0.5 --gray 0.5 --desktop 4 --follow-resize

mark "E fixed guest_size 800x600 + follow"
req '{"cmd":"display_config","guest_size":[800,600]}'
fake --size 640x480 --openbios 0.5 --gray 0.5 --desktop 4 --follow-resize
req '{"cmd":"display_config","guest_size":null}'

mark "F reconnect cycles"
fake --size 1024x768 --openbios 0.5 --gray 0.5 --desktop 2 --cycles 3 --gap 1

mark "G panel over guest, guest disconnects underneath"
fake --size 1024x768 --openbios 0.5 --gray 0.5 --desktop 6 &
FAKE=$!
sleep 2.5
req '{"cmd":"set_scene","scene":"panel","background":"guest-dimmed"}'
req '{"cmd":"ui_show","doc":{"id":"t","kind":"alert","alert_icon":"note","items":[{"type":"text","text":"Panel over guest"},{"type":"buttons","items":[{"id":"continue","label":"OK","default":true}]}]}}' 1
wait "$FAKE"
req '{"cmd":"set_scene","scene":"guest","reveal":"first-content"}' 1

mark "H black scene"
req '{"cmd":"set_scene","scene":"black"}' 1
req '{"cmd":"set_scene","scene":"splash"}' 1
req '{"cmd":"status"}' 0.5

mark "done"
kill "$HOSTD" 2>/dev/null
kill -TERM "$PRES"
wait "$PRES" 2>/dev/null
systemctl start retroboot-presenter.service
echo "presenter exit; sanitizer findings:"
grep -E "ERROR: AddressSanitizer|runtime error|LeakSanitizer" "$OUT/presenter.log" || echo "  none"
