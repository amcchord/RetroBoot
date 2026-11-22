#!/bin/bash
# The installer's static aarch64 zstd decompressor (installer/rb-unzstd.c).
#
#   build-unzstd.sh OUTDIR        (RB_BUILD_CHROOT: the arm64 build chroot)
#
# OUTDIR/rb-unzstd        statically linked aarch64 ELF (runs on Gemian's 3.18 kernel)
# OUTDIR/rb-unzstd.json   source hash, libzstd-dev .deb version and SHA-256, binary SHA-256
# Debian trixie's arm64 libzstd-dev (libzstd.a, zstd.h) is downloaded inside the arm64
# build chroot with a throw-away apt state (the chroot itself stays as it is) and the
# program is compiled with the builder's aarch64-linux-gnu-gcc against it and glibc's
# static libc. Cached by source and .deb hash; checked through qemu-user against the
# builder's own zstd.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
. "$here/gemini-lib.sh"
require_root
out=${1:?outdir}
chr=${RB_BUILD_CHROOT:?arm64 build chroot}
src=$GEMINI_SRC/installer/rb-unzstd.c
mkdir -p "$out" "$CACHE"
t=/tmp/rb-unzstd.$$; T=$chr$t
rm -rf "$T"; mkdir -p "$T/lists/partial" "$T/cache/archives/partial" "$T/dl"
trap 'rm -rf "$T"' EXIT
apt=(apt-get -q -o "Dir::State::Lists=$t/lists" -o "Dir::Cache=$t/cache" -o APT::Sandbox::User=root
     -o Acquire::Languages=none)
chroot "$chr" "${apt[@]}" update >/dev/null || die "apt-get update in the build chroot failed"
chroot "$chr" sh -c "cd $t/dl && ${apt[*]} download $(gcfg unzstd_deb)" >/dev/null || die "apt-get download $(gcfg unzstd_deb) failed"
deb=$(ls "$T"/dl/"$(gcfg unzstd_deb)"_*_arm64.deb)
debsha=$(sha256sum "$deb" | cut -d' ' -f1)
key=$( { sha256sum "$src"; echo "$debsha"; aarch64-linux-gnu-gcc --version | head -n1; } | sha256sum | cut -c1-16)
c=$CACHE/unzstd-$key
if [ ! -x "$c/rb-unzstd" ]; then
  log "building rb-unzstd ($(basename "$deb"))"
  rm -rf "$c"; mkdir -p "$c/x"
  dpkg-deb -x "$deb" "$c/x"
  aarch64-linux-gnu-gcc -O2 -Wall -Wextra -Werror -static -I "$c/x/usr/include" -o "$c/rb-unzstd" "$src" \
    "$c/x/usr/lib/aarch64-linux-gnu/libzstd.a"
  aarch64-linux-gnu-strip "$c/rb-unzstd"
  python3 - "$c/rb-unzstd.json" "$src" "$deb" "$debsha" "$c/rb-unzstd" <<'PY'
import hashlib, json, os, subprocess, sys
out, src, deb, debsha, binary = sys.argv[1:]
h = lambda p: hashlib.sha256(open(p, 'rb').read()).hexdigest()
json.dump({'source': 'image/gemini/installer/rb-unzstd.c', 'source_sha256': h(src),
           'libzstd_dev_deb': os.path.basename(deb), 'libzstd_dev_deb_sha256': debsha,
           'compiler': subprocess.run(['aarch64-linux-gnu-gcc', '--version'], capture_output=True, text=True).stdout.splitlines()[0],
           'sha256': h(binary), 'size_bytes': os.path.getsize(binary)}, open(out, 'w'), indent=2)
PY
  rm -rf "$c/x"
fi
file -b "$c/rb-unzstd" | grep -q 'ARM aarch64.*statically linked' || die "rb-unzstd is not a static aarch64 binary: $(file -b "$c/rb-unzstd")"
# Through qemu-user (binfmt): the builder's zstd compresses, rb-unzstd must give it back.
head -c 3000000 /dev/urandom > "$T/sample"; cat "$T/sample" "$T/sample" > "$T/sample2"
zstd -q -19 --long=27 -c "$T/sample2" > "$T/sample.zst"
"$c/rb-unzstd" < "$T/sample.zst" | cmp -s - "$T/sample2" || die "rb-unzstd does not round-trip"
! head -c 1000 "$T/sample.zst" | "$c/rb-unzstd" >/dev/null 2>&1 || die "rb-unzstd accepted a truncated stream"
cp "$c/rb-unzstd" "$c/rb-unzstd.json" "$out/"
log "rb-unzstd: $("$c/rb-unzstd" --version), round trip and truncation checks pass (qemu-user)"
