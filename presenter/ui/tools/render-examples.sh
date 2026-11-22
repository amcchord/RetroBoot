#!/bin/sh
# Re-render examples/rendered/*.png from examples/*.json (1x and 2x) plus a
# few interaction states. Usage: tools/render-examples.sh [path/to/rbui-render]
set -eu
here=$(cd "$(dirname "$0")/.." && pwd)
render=${1:-$here/build-make/rbui-render}
# Committed renders use the original fonts and icons only, never a local Platinum bundle.
export RBUI_ASSETS=""
out=$here/examples/rendered
mkdir -p "$out"
rm -f "$out"/*.png

shot() {   # name doc [rbui-render options...]
    name=$1; doc=$2; shift 2
    "$render" "$doc" "$out/$name.png" "$@"
    "$render" "$doc" "$out/$name@2x.png" --scale 2 "$@"
}

for doc in "$here"/examples/*.json; do
    shot "$(basename "$doc" .json)" "$doc"
done
shot hostd-lost builtin:hostd-lost

# Interaction states.
ex=$here/examples
shot state-display-settings-popup "$ex/display-settings.json" --pointer 640,195,down --pointer 640,230,down
shot state-wifi-password-typing "$ex/wifi-password.json" --keys text=hunter2
shot state-wifi-password-shown "$ex/wifi-password.json" --keys text=hunter2 --focus show --keys space --focus psk
shot state-boot-options-keyboard "$ex/boot-options.json" --keys down
shot state-recovery-focus "$ex/recovery-main.json" --keys tab,tab
shot state-erase-pressed "$ex/erase-confirm.json" --pointer 563,310,down
shot state-diagnostics-scrolled "$ex/diagnostics.json" --size 640x400 --focus include_logs
shot state-hostd-lost-dimmed builtin:hostd-lost --background dimmed
shot state-wifi-joining-black "$ex/wifi-joining.json" --background black --tick 400
shot state-installer-macs-startup "$ex/installer-macs.json" --keys tab,tab,space
shot state-installer-signin-typing "$ex/installer-signin.json" --keys text=labuser,tab,text=secret
ls "$out" | wc -l | sed 's/^ */rendered /'
