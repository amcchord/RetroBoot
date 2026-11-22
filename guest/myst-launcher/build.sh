#!/bin/sh
# Read an existing Retro68 toolchain; build only this project-owned launcher.
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
root=$(CDPATH= cd -- "$here/../.." && pwd)
: "${RETRO68_TOOLCHAIN:?Set RETRO68_TOOLCHAIN to the existing Retro68 toolchain}"
: "${OUT:?Set OUT to a private project output directory}"
cmake -S "$here" -B "$OUT/cmake" \
    -DCMAKE_TOOLCHAIN_FILE="$RETRO68_TOOLCHAIN/powerpc-apple-macos/cmake/retroppc.toolchain.cmake" \
    -DCMAKE_BUILD_TYPE=Release
cmake --build "$OUT/cmake"
python3 "$root/research/gfx/guestapps/make_tree.py" --out "$OUT/tree" \
    "$OUT/cmake/RetroBootMyst.bin=Play Myst"
