#!/bin/sh
# pi-check.sh [QEMU_PACKAGE] -- run on the Raspberry Pi (or any aarch64 Linux host).
# Bit-for-bit check of the installed emulator's PowerPC FP/AltiVec/branch-cache code
# against the x86 r18 package: the bare-metal conformance harness (seed 501, count
# 2000: 10.1 M vectors per arm, fast-fpu on, off and fast-branch off) must give the
# x86 result buffer exactly, and the self-modifying-code test must give the x86
# record (92 calls, 2 that return the old code: the architecture's allowance).
# No Mac OS, ROM or disk is involved.  Takes well under a minute per arm on a Pi 5.
# The bundle directory (made by mkpicheck.sh) holds fpconf.elf, ops.json, fpconf.py,
# bench_names.txt, expected.txt and this script.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
PKG=${1:-/usr/lib/retroboot/qemu}
Q=$PKG/bin/qemu-system-ppc
OUT=$(mktemp -d /tmp/rb-picheck.XXXXXX)
echo "package: $Q"; "$Q" --version | head -1
echo "host: $(uname -srm), page size $(getconf PAGESIZE)"
. "$HERE/expected.txt"   # CONF_SHA256, SMC_SHA256
python3 "$HERE/fpconf.py" conf --qemu "$Q" --elf "$HERE/fpconf.elf" --count 2000 --seed 501 \
  --arm on=fast-fpu=on --arm off=fast-fpu=off --arm nobr=fast-branch=off --jobs 1 --root "$OUT/conf"
python3 "$HERE/fpconf.py" smc --qemu "$Q" --elf "$HERE/fpconf.elf" --arm on=fast-fpu=on --arm nobr=fast-branch=off \
  --root "$OUT/smc" | tail -2
fail=0
for a in on off nobr; do
  h=$(sha256sum "$OUT/conf/$a/res.bin" | cut -d' ' -f1)
  if [ "$h" = "$CONF_SHA256" ]; then echo "conformance $a: identical to x86 r18"; else echo "conformance $a: DIFFERENT ($h)"; fail=1; fi
done
for a in on nobr; do
  h=$(sha256sum "$OUT/smc/smc-$a/res.bin" | cut -d' ' -f1)
  if [ "$h" = "$SMC_SHA256" ]; then echo "smc $a: identical to x86 r18"; else echo "smc $a: DIFFERENT ($h)"; fail=1; fi
done
[ $fail = 0 ] && echo "PASS ($OUT)" || echo "FAIL ($OUT)"
exit $fail
