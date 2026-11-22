#!/bin/sh
# Source check for the a72dvfs2 variant (build-kernel.sh runs it on the patched tree):
# compile and run the glitch-filter unit test against the exact code in
# drivers/thermal/mediatek/auxadc_thermal.c (between the glitch-filter markers).
# Usage: check-src.sh SRC_DIR
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
SRC=${1:?usage: check-src.sh SRC_DIR}
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
sed -n '/\/\* glitch-filter-begin \*\//,/\/\* glitch-filter-end \*\//p' \
    "$SRC/drivers/thermal/mediatek/auxadc_thermal.c" > "$T/filter.inc"
[ "$(wc -l < "$T/filter.inc")" -gt 10 ] || { echo "glitch filter markers not found"; exit 1; }
cc -O2 -Wall -Werror -I"$T" -o "$T/test" "$HERE/test-glitch-filter.c"
"$T/test"
