#!/bin/sh
# Pixel-scale scenarios for rb-presenter under ASan/UBSan (lab tool, root on
# rb-dev; display mode 1920x1080 or larger). Runs the given presenter binary
# instead of the service, drives it with mini_hostd + fake_qemu and leaves
# logs and screenshots in $OUT; check presenter.log for sanitizer reports and
# the timeline lines (switch_shown, relayout, display_config).
#
#   scale_suite.sh [presenter-binary] [out-dir]
set -u
T=$(cd "$(dirname "$0")" && pwd)
BIN=${1:-$T/../build-asan/rb-presenter}
OUT=${2:-/tmp/scale-suite}
FIFO=/tmp/scale-suite.fifo
rm -rf "$OUT"; mkdir -p "$OUT"

req() { echo "$1" > "$FIFO"; sleep "${2:-0.3}"; }
shot() { req '{"cmd":"screenshot","path":"'"$OUT/$1"'.png","what":"composed"}' 1; }
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

mark "A checkerboard at 1x, 2x, auto (splash scene)"
req '{"cmd":"display_config","pixel_scale":1}'
shot checker-1x
req '{"cmd":"display_config","pixel_scale":2}'
shot checker-2x
req '{"cmd":"display_config","pixel_scale":"auto"}'

mark "B guest follows RESIZE: 2x -> 1x -> 2x while shown (deferred layout + repaint hold)"
req '{"cmd":"set_scene","scene":"guest","reveal":"first-content"}'
python3 "$T/fake_qemu.py" --size 960x540 --openbios 0.3 --gray 0.5 --desktop 9 --follow-resize >> "$OUT/fake.log" 2>&1 &
FAKE=$!
sleep 2.5
req '{"cmd":"display_config","pixel_scale":1}' 2
shot guest-1x
req '{"cmd":"display_config","pixel_scale":2}' 2
shot guest-2x
mark "C panel over the dimmed guest, re-grid to 1x and back"
req '{"cmd":"ui_show","doc":{"id":"scale-test","kind":"alert","alert_icon":"note","width":360,"items":[{"type":"text","text":"Pixel size test","style":"emphasis"},{"type":"buttons","items":[{"id":"continue","label":"OK","default":true}]}]}}'
req '{"cmd":"set_scene","scene":"panel","background":"guest-dimmed"}' 0.5
req '{"cmd":"display_config","pixel_scale":1}' 1
shot panel-1x
req '{"cmd":"display_config","pixel_scale":2}' 1
shot panel-2x
req '{"cmd":"set_scene","scene":"guest","reveal":"now"}'
wait "$FAKE"

mark "D guest ignores RESIZE: layout falls back after 5 s"
req '{"cmd":"set_scene","scene":"guest","reveal":"first-content"}'
python3 "$T/fake_qemu.py" --size 960x540 --openbios 0.3 --gray 0.5 --desktop 10 >> "$OUT/fake.log" 2>&1 &
FAKE=$!
sleep 2.5
req '{"cmd":"display_config","pixel_scale":1}' 6.5
shot ignored-1x
wait "$FAKE"

mark "E repaint never settles (full-screen animation): hold ends after 1 s"
req '{"cmd":"display_config","pixel_scale":2}'
python3 "$T/fake_qemu.py" --size 960x540 --openbios 0.3 --gray 0.5 --desktop 6 --follow-resize --fullscreen >> "$OUT/fake.log" 2>&1 &
FAKE=$!
sleep 2.5
req '{"cmd":"display_config","pixel_scale":1}' 2
req '{"cmd":"display_config","pixel_scale":2}' 2
wait "$FAKE"

mark "F fixed guest_size 640x480 at 1x and 2x"
req '{"cmd":"display_config","guest_size":[640,480],"pixel_scale":1}'
python3 "$T/fake_qemu.py" --size 640x480 --openbios 0.3 --gray 0.5 --desktop 3 --follow-resize >> "$OUT/fake.log" 2>&1 &
FAKE=$!
sleep 2
shot fixed-640-1x
req '{"cmd":"display_config","pixel_scale":2}' 1
shot fixed-640-2x
wait "$FAKE"
req '{"cmd":"display_config","guest_size":null,"pixel_scale":"auto"}'

mark "done"
req '{"cmd":"status"}' 0.5
kill "$HOSTD" 2>/dev/null
kill -TERM "$PRES"
wait "$PRES" 2>/dev/null
systemctl start retroboot-presenter.service
grep -E "ERROR: (Address|Leak)Sanitizer|runtime error" "$OUT/presenter.log" && echo "SANITIZER FINDINGS" || echo "no sanitizer findings"
grep -E "\[(switch_shown|relayout|display_config)\]" "$OUT/presenter.log" | sed 's/.*\] \[/[/'
