#!/bin/bash
# Fetch the pinned upstream files the Gemini root needs (config/gemini.conf upstream_file=
# lines) from atzandrew/gemini-nixos at the pinned commit, and check each SHA-256.
#
#   fetch-upstream.sh OUTDIR
#
# OUTDIR/files/<dest path>   the files, laid out as in the root (installed by customize-gemini.sh)
# OUTDIR/upstream.json       repository, commit, and per file: path in the repository,
#                            destination, size and SHA-256 (goes into the manifest)
# The repository has no license, so nothing of it enters RetroBoot's Git: it is cloned
# into $RB_WORK_ROOT/upstream/gemini-nixos (a cache) and read with `git show COMMIT:PATH`,
# independent of whatever is checked out there.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
. "$here/gemini-lib.sh"
out=${1:?outdir}
url=$(gcfg gemini_nixos_url); commit=$(gcfg gemini_nixos_commit)
repo=$RB_WORK_ROOT/upstream/gemini-nixos
mkdir -p "$RB_WORK_ROOT/upstream"
if [ ! -d "$repo/.git" ]; then
  log "cloning $url"
  git clone -q --no-checkout "$url" "$repo"
fi
if ! git -C "$repo" cat-file -e "$commit^{commit}" 2>/dev/null; then
  log "fetching $commit"
  git -C "$repo" fetch -q origin || die "cannot fetch $url"
fi
git -C "$repo" cat-file -e "$commit^{commit}" || die "commit $commit not in $url"
rm -rf "$out"; mkdir -p "$out/files"
entries=()
while IFS='|' read -r dest path want; do
  [ -n "$dest" ] || continue
  case "$path" in *WIFI_factory*) die "upstream's WIFI_factory.bin must never be installed (another unit's MAC record)" ;; esac
  mkdir -p "$out/files$(dirname "$dest")"
  git -C "$repo" show "$commit:$path" > "$out/files$dest" || die "$path missing at $commit"
  got=$(sha256sum "$out/files$dest" | cut -d' ' -f1)
  [ "$got" = "$want" ] || die "$path at $commit: sha256 $got, pinned $want"
  entries+=("$dest|$path|$got|$(stat -c %s "$out/files$dest")")
done < <(gcfg_all upstream_file)
python3 - "$out/upstream.json" "$url" "$commit" "$(gcfg gemini_debian_url)" "$(gcfg gemini_debian_commit)" "${entries[@]}" <<'PY'
import json, sys
out, url, commit, durl, dcommit, *entries = sys.argv[1:]
files = []
for e in entries:
    dest, path, sha, size = e.split('|')
    files.append({'install': dest, 'path': path, 'sha256': sha, 'size_bytes': int(size)})
json.dump({'gemini_nixos': {'url': url, 'commit': commit, 'files': files,
                            'note': 'fetched at build time from the pinned commit (no license; not in RetroBoot Git)'},
           'gemini_debian': {'url': durl, 'commit': dcommit,
                             'note': 'reference only: RetroBoot reimplements its hardware services (image/gemini/rootfs); nothing fetched'}},
          open(out, 'w'), indent=2)
PY
log "upstream: ${#entries[@]} files from gemini-nixos ${commit:0:10}, hashes match the pins"
