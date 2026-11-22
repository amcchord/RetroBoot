#!/bin/sh
# mkpicheck.sh FPCONF_DIR X86_PACKAGE OUT
# The pi-check bundle: the harness (tests/ppcfpu/build.sh output) plus expected.txt, the
# x86 package's result hashes for pi-check.sh's exact parameters (each run twice here to
# show they are deterministic).  The harness is RetroBoot's own test program; no Apple code.
set -eu
FP=$1; X=$2; OUT=$3
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$OUT"
cp "$FP/fpconf.elf" "$FP/ops.json" "$FP/fpconf.py" "$FP/bench_names.txt" "$HERE/pi-check.sh" "$OUT/"
T=$(mktemp -d)
for i in 1 2; do
  python3 "$FP/fpconf.py" conf --qemu "$X/bin/qemu-system-ppc" --elf "$FP/fpconf.elf" --count 2000 --seed 501 \
    --arm on=fast-fpu=on --root "$T/conf$i" > /dev/null
  python3 "$FP/fpconf.py" smc --qemu "$X/bin/qemu-system-ppc" --elf "$FP/fpconf.elf" --arm on=fast-fpu=on --root "$T/smc$i" > /dev/null
done
c1=$(sha256sum "$T/conf1/on/res.bin" | cut -d' ' -f1); c2=$(sha256sum "$T/conf2/on/res.bin" | cut -d' ' -f1)
s1=$(sha256sum "$T/smc1/smc-on/res.bin" | cut -d' ' -f1); s2=$(sha256sum "$T/smc2/smc-on/res.bin" | cut -d' ' -f1)
[ "$c1" = "$c2" ] && [ "$s1" = "$s2" ] || { echo "x86 results not deterministic" >&2; exit 1; }
printf 'CONF_SHA256=%s\nSMC_SHA256=%s\n# from %s (x86), %s\n' "$c1" "$s1" "$X" "$(date -u +%FT%TZ)" > "$OUT/expected.txt"
rm -rf "$T"
cat "$OUT/expected.txt"; ls "$OUT"
