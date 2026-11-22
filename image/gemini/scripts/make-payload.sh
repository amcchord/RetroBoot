#!/bin/bash
# Stage 4 (Gemini PDA): the installable payload of one build.
#
#   make-payload.sh ROOTDIR OUTDIR NAME KERNEL_STAGE UNZSTD_DIR BUILD_ID SUFFIX
#
# OUTDIR/NAME.rootfs.tar.zst   the root for <linux>/retroboot: POSIX tar, numeric owners,
#                              xattrs (file capabilities), sparse files kept sparse,
#                              sorted names; zstd -17 --long=27
# OUTDIR/boot.img              wave7/gemini-kernel's boot.img (also inside the root, so a
#                              tree always knows its own kernel: --rollback previous)
# OUTDIR/rb-unzstd             static aarch64 zstd decompressor (Gemian may lack zstd)
# OUTDIR/gemini-install.sh     the installer (installer/gemini-install.sh.in with this
#                              build's values and scripts' gemini-part.sh inlined)
# OUTDIR/SHA256SUMS            the three payload files (the installer checks them first)
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
. "$here/gemini-lib.sh"
require_root
root=${1:?rootdir}; out=${2:?outdir}; name=${3:?name}; kstage=${4:?kernel stage}; unz=${5:?unzstd dir}
build_id=${6:?build id}; suffix=${7:?suffix}
tarball=$name.rootfs.tar.zst

log "root tarball $tarball"
bytes=$(du -s --block-size=1 "$root" | cut -f1)
tar_args=(-C "$root" --format=posix --pax-option=delete=atime,delete=ctime --sort=name --numeric-owner
          --xattrs --xattrs-include='*' --sparse --sparse-version=1.0)
if [ -f "$root.devnodes.json" ]; then
  # The root's device nodes are stand-ins (customize-rootfs.sh on a builder that may not
  # create them): pack the base's own records in their place.
  excl=(); while read -r n; do excl+=(--exclude="$n"); done < <(python3 -c \
    'import json,sys; [print(n["name"]) for n in json.load(open(sys.argv[1]))]' "$root.devnodes.json")
  tar "${tar_args[@]}" "${excl[@]}" -cf "$out/$tarball.tar.tmp" .
  python3 - "$out/$tarball.tar.tmp" "$root.devnodes.json" <<'PY'
import json, sys, tarfile
with tarfile.open(sys.argv[1], 'a', format=tarfile.PAX_FORMAT) as t:
    for n in json.load(open(sys.argv[2])):
        ti = tarfile.TarInfo(n['name'])
        ti.type = tarfile.CHRTYPE if n['type'] == 'chr' else tarfile.BLKTYPE
        for k in ('mode', 'uid', 'gid', 'uname', 'gname', 'mtime'):
            setattr(ti, k, n[k])
        ti.devmajor, ti.devminor = n['major'], n['minor']
        t.addfile(ti)
PY
  zstd -q -T0 -17 --long=27 --rm "$out/$tarball.tar.tmp" -o "$out/$tarball.tmp"
  log "device nodes: $(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))))' "$root.devnodes.json") packed from the base's records"
else
  tar "${tar_args[@]}" -cf - . | zstd -q -T0 -17 --long=27 > "$out/$tarball.tmp"
fi
mv "$out/$tarball.tmp" "$out/$tarball"
zstd -q -t --long=27 "$out/$tarball" || die "$tarball does not test"
log "$tarball: $(stat -c %s "$out/$tarball") bytes (root: $bytes bytes allocated)"

cp "$kstage/boot.img" "$out/boot.img"
cmp -s "$out/boot.img" "$root/usr/lib/retroboot/gemini/boot/boot.img" || die "the root's boot.img differs from the kernel stage"
install -m 0755 "$unz/rb-unzstd" "$out/rb-unzstd"
(cd "$out" && sha256sum "$tarball" boot.img rb-unzstd > SHA256SUMS)

python3 - "$GEMINI_SRC/installer/gemini-install.sh.in" "$GEMINI_SRC/rootfs/usr/lib/retroboot/gemini/gemini-part.sh" \
  "$out/gemini-install.sh" "$root/usr/lib/retroboot/gemini/boot-counter.conf" <<PY
import shlex, sys
tmpl, lib, out, counter = sys.argv[1:]
c = {}
for line in open(counter):
    line = line.strip()
    if line and not line.startswith('#') and '=' in line:
        k, v = line.split('=', 1)
        c[k] = (shlex.split(v) or [''])[0]
values = {'VERSION': '$(cfg version)', 'BUILD_ID': '$build_id', 'SUFFIX': '$suffix', 'ROOTFS': '$tarball',
          'ROOTFS_BYTES': '$bytes', 'BOOTIMG': 'boot.img',
          'BOOTIMG_SHA256': '$(sha256sum "$out/boot.img" | cut -d' ' -f1)', 'ROOT_DIR': '$(gcfg root_dir)',
          'PART_BOOT': '$(gcfg part_boot)', 'PART_PARA': '$(gcfg part_para)', 'PART_LINUX': '$(gcfg part_linux)',
          'COUNTER_FILE': c['COUNTER_FILE'], 'COUNTER_RESET': c['COUNTER_RESET']}
text = open(tmpl).read()
for k, v in values.items():
    if "'" in v:
        sys.exit(f'{k} contains a quote')
    text = text.replace(f'@{k}@', v)
text = text.replace('@GEMINI_PART_LIB@', open(lib).read().rstrip('\n'))
import re
left = re.findall(r'@[A-Z_]+@', text)
if left:
    sys.exit(f'unfilled placeholders: {sorted(set(left))}')
open(out, 'w').write(text)
PY
chmod 0755 "$out/gemini-install.sh"
sh -n "$out/gemini-install.sh" || die "gemini-install.sh does not parse"
log "payload: $(cd "$out" && ls -1 "$tarball" boot.img rb-unzstd gemini-install.sh SHA256SUMS | paste -sd' ' -)"
