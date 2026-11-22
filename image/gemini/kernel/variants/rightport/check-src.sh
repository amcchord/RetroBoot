#!/bin/sh
# Source check for the rightport variant (build-kernel.sh runs it on the patched tree):
# compile and run the right-port VBUS policy unit test against the exact code in
# drivers/usb/typec/gemini-right-port.c (between the rightport-policy markers).
# Usage: check-src.sh SRC_DIR
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
SRC=${1:?usage: check-src.sh SRC_DIR}
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
sed -n '/\/\* rightport-policy-begin \*\//,/\/\* rightport-policy-end \*\//p' \
    "$SRC/drivers/usb/typec/gemini-right-port.c" > "$T/policy.inc"
[ "$(wc -l < "$T/policy.inc")" -gt 40 ] || { echo "right-port policy markers not found"; exit 1; }
cc -O2 -Wall -Wextra -Werror -I"$T" -o "$T/test" "$HERE/test-right-port-policy.c"
"$T/test"
# The bring-up hogs that sourced VBUS into the right connector must be gone.
DTS=$SRC/arch/arm64/boot/dts/mediatek/mt6797-gemini-pda.dts
if grep -n 'usb1-drvvbus-hog\|sw7226-en-hog' "$DTS"; then
  echo "right-port VBUS hogs still in $DTS"; exit 1
fi
