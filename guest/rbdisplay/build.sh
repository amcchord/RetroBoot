#!/bin/sh
# Build RetroBoot Display with Retro68 and convert the MacBinary outputs into
# one rbapps canonical tree for rbguestprep.ati (installed into System
# Folder:Extensions):
#   " RetroBoot Display Startup"  68k INIT (init/): sets the size while the
#                                  extensions load and saves it (Display
#                                  Preferences), so later startups restore it
#                                  even earlier
#   "RetroBoot Display"           PowerPC background-only app (type appe):
#                                  the same as the Finder starts, when the
#                                  screen is not already that size
#   RETRO68_TOOLCHAIN  Retro68-build/toolchain directory (read only)
#   OUT                tree directory (default build/tree; git-ignored)
#   RBD_APP_ONLY=1     the app alone (the rbdisplay-1 tree)
set -eu
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
tc=${RETRO68_TOOLCHAIN:-/Users/austinmcchord/Development/ClassicMac/vendor/Retro68-build/toolchain}
out=${OUT:-$here/build/tree}
cmake -S "$here" -B "$here/build/cmake" \
    -DCMAKE_TOOLCHAIN_FILE="$tc/powerpc-apple-macos/cmake/retroppc.toolchain.cmake" \
    -DCMAKE_BUILD_TYPE=Release
cmake --build "$here/build/cmake"
cp "$here/build/cmake/RetroBootDisplay.bin" "$here/build/RetroBootDisplay.bin"
rm -rf "$out"
if [ "${RBD_APP_ONLY:-0}" = 1 ]; then
    python3 "$root/research/gfx/guestapps/make_tree.py" --types appe --out "$out" \
        "$here/build/RetroBootDisplay.bin=RetroBoot Display"
    exit 0
fi
RETRO68_TOOLCHAIN=$tc OUT="$here/build/init/tree" "$here/init/build.sh"
python3 "$here/init/make_init_tree.py" --out "$out" \
    "$here/build/init/RetroBootDisplayInit.bin= RetroBoot Display Startup" \
    "$here/build/RetroBootDisplay.bin=RetroBoot Display"
