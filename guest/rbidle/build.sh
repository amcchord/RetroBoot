#!/bin/sh
# Build RetroBoot Idle (68k INIT) with Retro68 and turn the MacBinary output
# into an rbapps canonical tree (one file, type INIT, creator RBid) that
# rbguestprep installs into System Folder:Extensions.
#   RETRO68_TOOLCHAIN  Retro68-build/toolchain directory (read only)
#   BUILD              build directory (default guest/rbidle/build; git-ignored)
#   OUT                tree directory (default $BUILD/tree)
#   RBIDLE_MIN_RUN     idle calls in one tick before the CPU stops (default 4)
#   INIT_NAME          Mac file name (default "RetroBoot Idle")
set -eu
here=$(cd "$(dirname "$0")" && pwd)
tc=${RETRO68_TOOLCHAIN:-/opt/retro68/toolchain}
build=${BUILD:-$here/build}
out=${OUT:-$build/tree}
name=${INIT_NAME:-RetroBoot Idle}
cmake -S "$here" -B "$build/cmake" \
    -DCMAKE_TOOLCHAIN_FILE="$tc/m68k-apple-macos/cmake/retro68.toolchain.cmake" \
    -DCMAKE_BUILD_TYPE=Release -DRBIDLE_MIN_RUN="${RBIDLE_MIN_RUN:-4}"
cmake --build "$build/cmake"
cp "$build/cmake/RetroBootIdle.bin" "$build/RetroBootIdle.bin"
cp "$build/cmake/RetroBootIdle.flt" "$build/RetroBootIdle.flt"
rm -rf "$out"
python3 "$here/../rbdisplay/init/make_init_tree.py" --out "$out" "$build/RetroBootIdle.bin=$name"
