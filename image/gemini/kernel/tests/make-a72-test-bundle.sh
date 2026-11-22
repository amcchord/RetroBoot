#!/bin/bash
# Build the on-device A72 step-test bundle (rb-builder): a static aarch64 rb-cpumhz plus
# a72-step-test.sh, as KERNEL_OUT_DIR/a72-test.tar.gz (copy it to the Gemini, untar,
# run ./a72-test/a72-step-test.sh as root).
#
# Usage: make-a72-test-bundle.sh KERNEL_OUT_DIR
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
OUT=$(readlink -f "${1:?usage: make-a72-test-bundle.sh KERNEL_OUT_DIR}")
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/a72-test"
aarch64-linux-gnu-gcc -O2 -static -Wall -o "$T/a72-test/rb-cpumhz" "$HERE/rb-cpumhz.c"
aarch64-linux-gnu-strip "$T/a72-test/rb-cpumhz"
install -m 0755 "$HERE/a72-step-test.sh" "$T/a72-test/a72-step-test.sh"
# Sanity under qemu-user: the measurement runs and prints a number.
qemu-aarch64 "$T/a72-test/rb-cpumhz" 0 50 | grep -Eq '^[0-9]+$'
sh -n "$T/a72-test/a72-step-test.sh"
tar -C "$T" --owner=0 --group=0 --numeric-owner -czf "$OUT/a72-test.tar.gz" a72-test
sha256sum "$OUT/a72-test.tar.gz"
