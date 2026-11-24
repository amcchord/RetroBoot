#!/bin/sh
# Build RBFrames (lab test app for RetroBoot Idle) with Retro68's 68k toolchain.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
tc=${RETRO68_TOOLCHAIN:-/Users/austinmcchord/Development/ClassicMac/vendor/Retro68-build/toolchain}
build=${BUILD:-$here/../../build/rbframes}
cmake -S "$here" -B "$build" -DCMAKE_TOOLCHAIN_FILE="$tc/m68k-apple-macos/cmake/retro68.toolchain.cmake" \
    -DCMAKE_BUILD_TYPE=Release
cmake --build "$build"
echo "-> $build/RBFrames.bin"
