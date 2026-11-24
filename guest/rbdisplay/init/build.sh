#!/bin/sh
# Build RetroBoot Display's startup extension (68k INIT) with Retro68 and turn
# the MacBinary output into an rbapps canonical tree (one file, type INIT) for
# rbguestprep (installed into System Folder:Extensions).
#   RETRO68_TOOLCHAIN  Retro68-build/toolchain directory (read only)
#   OUT                tree directory (default ../build/init/tree; git-ignored)
#   BUILD              build directory (default ../build/init; git-ignored)
#   RBD_CFLAGS         extra compiler flags
#   INIT_NAME          Mac file name (default " RetroBoot Display Startup": the
#                      leading space loads it right after " EM Extension",
#                      at the start of the extension parade; the name only sets
#                      when in the parade the switch lands - lab: about 4.9 s
#                      after "Starting Up...", or about 6.3 s for a name in "R")
set -eu
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../../.." && pwd)
tc=${RETRO68_TOOLCHAIN:-/Users/austinmcchord/Development/ClassicMac/vendor/Retro68-build/toolchain}
build=${BUILD:-$here/../build/init}
out=${OUT:-$build/tree}
name=${INIT_NAME:- RetroBoot Display Startup}
cmake -S "$here" -B "$build/cmake" \
    -DCMAKE_TOOLCHAIN_FILE="$tc/m68k-apple-macos/cmake/retro68.toolchain.cmake" \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_C_FLAGS="${RBD_CFLAGS:-}"
cmake --build "$build/cmake"
cp "$build/cmake/RetroBootDisplayInit.bin" "$build/RetroBootDisplayInit.bin"
rm -rf "$out"
python3 "$here/make_init_tree.py" --out "$out" "$build/RetroBootDisplayInit.bin=$name"
