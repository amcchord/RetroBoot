#!/bin/bash
# Stage the other workstreams' deliverables for one image build.
#
#   fetch-components.sh OUTDIR
#
# Sources (all on the builder):
#   $RB_WORK_ROOT/src/components/<name>/   git-archived source trees staged from the
#        Mac by image/lab/stage-components.sh (component.json records branch + commit)
#   /srv/retroboot/wave2/artifacts/presenter/  rb-presenter build (presenter-core)
#   /srv/retroboot/wave2/artifacts/qemu/$RB_QEMU_PACKAGE/  emulator package (default: newest
#        linux-ppc-rN, else linux-ppc; r3 carries AirPort and the sound-objects firmware fix)
#   /srv/retroboot/wave2/artifacts/qemu/$RB_QEMU_M68K_PACKAGE/  the 68k emulator package
#        (default: newest linux-m68k-rN, -arm64 for arm64 builds; optional unless RB_REQUIRE_M68K=1)
#   $RB_WORK_ROOT/src/lab/                 lab public key for the debug flavor
# Anything missing is recorded as absent; the presenter falls back to the stand-in.
# RB_BUILD_CHROOT (Raspberry Pi image, image/rpi/): an arm64 build chroot; the presenter
# is compiled inside it and the staged ELFs' libraries are resolved inside it. Unset for
# the x86 image, which builds and resolves on the builder itself as before.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
. "$here/lib.sh"
out=${1:?outdir}
artifacts=${RB_ARTIFACTS:-/srv/retroboot/wave2/artifacts}
staged=$RB_WORK_ROOT/src/components
rm -rf "$out"; mkdir -p "$out"

meta() { # name kind source revision
  python3 -c 'import json,sys; print(json.dumps(dict(zip(["name","kind","source","revision"], sys.argv[1:]))))' "$@"
}

# presenter: published artifact preferred (manifest + rb-presenter-<rev>.tar.gz of
# the installed tree); else build from staged source; else the stand-in.
ptar=$(ls "$artifacts"/presenter/rb-presenter-*.tar.gz 2>/dev/null | sort | tail -n1 || true)
if [ -n "$ptar" ] && [ -f "$artifacts/presenter/manifest.json" ]; then
  want=$(python3 -c 'import json,sys,os; m=json.load(open(sys.argv[1])); print(m["artifacts_sha256"][os.path.basename(sys.argv[2])])' \
         "$artifacts/presenter/manifest.json" "$ptar")
  [ "$(sha256sum "$ptar" | cut -d' ' -f1)" = "$want" ] || die "presenter artifact hash mismatch"
  mkdir -p "$out/presenter/dest"
  tar -C "$out/presenter/dest" -xzf "$ptar"
  # verify every installed file against the manifest
  python3 - "$artifacts/presenter/manifest.json" "$out/presenter/dest" <<'PY'
import hashlib, json, sys
m = json.load(open(sys.argv[1]))
for path, digest in m['files_sha256'].items():
    got = hashlib.sha256(open(sys.argv[2] + path, 'rb').read()).hexdigest()
    if got != digest:
        sys.exit(f'presenter artifact: {path} does not match its manifest')
PY
  python3 - "$artifacts/presenter/manifest.json" "$ptar" > "$out/presenter/component.json" <<'PY'
import json, sys, os, hashlib
m = json.load(open(sys.argv[1]))
print(json.dumps({'name': 'presenter', 'kind': 'artifact', 'path': os.path.dirname(sys.argv[1]),
                  'branch': m.get('git_branch'), 'revision': m.get('git_revision'), 'includes': m.get('includes'),
                  'tarball': os.path.basename(sys.argv[2]),
                  'tarball_sha256': hashlib.sha256(open(sys.argv[2], 'rb').read()).hexdigest()}))
PY
elif [ -x "$artifacts/presenter/bin/rb-presenter" ] || [ -x "$artifacts/presenter/rb-presenter" ]; then
  mkdir -p "$out/presenter/bin"
  bin=$artifacts/presenter/bin/rb-presenter; [ -x "$bin" ] || bin=$artifacts/presenter/rb-presenter
  cp "$bin" "$out/presenter/bin/rb-presenter"
  [ -d "$artifacts/presenter/packaging" ] && cp -a "$artifacts/presenter/packaging" "$out/presenter/"
  if [ -f "$artifacts/presenter/manifest.json" ]; then cp "$artifacts/presenter/manifest.json" "$out/presenter/component.json"
  else meta presenter artifact "$artifacts/presenter" unknown > "$out/presenter/component.json"; fi
  [ -d "$out/presenter/packaging" ] || { [ -d "$staged/presenter/presenter/packaging" ] && cp -a "$staged/presenter/presenter/packaging" "$out/presenter/"; } || true
elif [ -f "$staged/presenter/presenter/meson.build" ] && [ -f "$staged/classic-ui/presenter/ui/meson.build" ]; then
  # No published artifact yet: build rb-presenter/rb-splash from the staged
  # presenter-core + classic-ui sources (cached by both revisions).
  prev=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["revision"])' "$staged/presenter/component.json")
  urev=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["revision"])' "$staged/classic-ui/component.json")
  pbuild=$CACHE/presenter-${prev:0:12}-ui-${urev:0:12}
  if [ ! -f "$pbuild/.complete" ]; then
    log "building presenter ${prev:0:10} with classic-ui ${urev:0:10}"
    rm -rf "$pbuild"; mkdir -p "$pbuild/src"
    cp -a "$staged/presenter/presenter/." "$pbuild/src/"
    cp -a "$staged/classic-ui/presenter/ui" "$pbuild/src/ui"
    echo "${prev:0:10}" > "$pbuild/src/.gitrev"
    if [ -n "${RB_BUILD_CHROOT:-}" ]; then
      cin=/tmp/rb-presenter-build.$$; cb=$RB_BUILD_CHROOT$cin     # per build: builds may run side by side
      rm -rf "$cb"; mkdir -p "$cb"; cp -a "$pbuild/src" "$cb/src"
      (chroot "$RB_BUILD_CHROOT" /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C.UTF-8 sh -c \
         "meson setup $cin/build $cin/src --prefix=/usr --buildtype=release &&
          ninja -C $cin/build &&
          DESTDIR=$cin/dest meson install -C $cin/build" >"$pbuild/meson.log" 2>&1 &&
       cp -a "$cb/dest" "$pbuild/dest") ||
       { tail -40 "$pbuild/meson.log" >&2; die "presenter build (in $RB_BUILD_CHROOT) failed"; }
      rm -rf "$cb"
    else
    (meson setup "$pbuild/build" "$pbuild/src" --prefix=/usr --buildtype=release >"$pbuild/meson.log" 2>&1 &&
     ninja -C "$pbuild/build" >>"$pbuild/meson.log" 2>&1 &&
     DESTDIR="$pbuild/dest" meson install -C "$pbuild/build" >>"$pbuild/meson.log" 2>&1) ||
     { tail -40 "$pbuild/meson.log" >&2; die "presenter build failed"; }
    fi
    touch "$pbuild/.complete"
  fi
  mkdir -p "$out/presenter"
  cp -a "$pbuild/dest" "$out/presenter/dest"
  python3 - "$out/presenter/component.json" "$prev" "$urev" "$staged/presenter/component.json" "$staged/classic-ui/component.json" <<'PY'
import json, sys
pb = json.load(open(sys.argv[4])).get('branch', 'wave2/presenter-core')
ub = json.load(open(sys.argv[5])).get('branch', 'wave2/classic-ui')
json.dump({'name': 'presenter', 'kind': 'built-from-source', 'branch': pb, 'revision': sys.argv[2],
           'classic_ui': {'branch': ub, 'revision': sys.argv[3]},
           'note': 'built by the image workstream until presenter-core publishes /srv/retroboot/wave2/artifacts/presenter'},
          open(sys.argv[1], 'w'))
PY
elif [ -n "${RB_BUILD_CHROOT:-}" ]; then
  die "no presenter sources staged (the Raspberry Pi image has no stand-in)"
else
  log "presenter artifact missing: compiling the image stand-in"
  mkdir -p "$out/standin"
  cc -O2 -Wall -Wextra -o "$out/standin/rb-standin-presenter" "$IMAGE_SRC/standin/rb-standin-presenter.c" \
     $(pkg-config --cflags --libs libdrm)
fi

if [ -x "$staged/hostd/hostd/packaging/install.sh" ]; then
  mkdir -p "$out/hostd/dest"
  # install.sh copies hostd/README.md; tolerate a revision that does not have one yet.
  [ -f "$staged/hostd/hostd/README.md" ] || echo "hostd README not present at this revision" > "$staged/hostd/hostd/README.md"
  "$staged/hostd/hostd/packaging/install.sh" "$out/hostd/dest" >&2
  cp "$staged/hostd/component.json" "$out/hostd/component.json"
fi
if [ -d "$staged/guestprep/guestprep/rbguestprep" ]; then
  mkdir -p "$out/guestprep"; cp -a "$staged/guestprep/guestprep/rbguestprep" "$out/guestprep/"
  cp "$staged/guestprep/component.json" "$out/guestprep/component.json"
fi
if [ -d "$staged/apps/apps/rbapps" ]; then
  # Classic Mac media tools (hostd media library: archive expansion, install job).
  mkdir -p "$out/apps"; cp -a "$staged/apps/apps/rbapps" "$out/apps/"
  cp "$staged/apps/component.json" "$out/apps/component.json"
fi
if [ -d "$staged/platinum/platinum/rbplatinum" ]; then
  # Platinum asset bundle builder (code only; the bundle itself comes from the guest disk).
  mkdir -p "$out/platinum"; cp -a "$staged/platinum/platinum/rbplatinum" "$out/platinum/"
  cp "$staged/platinum/component.json" "$out/platinum/component.json"
fi
if [ -d "$staged/web-ui/web" ]; then
  mkdir -p "$out/web"; cp -a "$staged/web-ui/web" "$out/web/"
  cp "$staged/web-ui/component.json" "$out/web/component.json"
fi
qpkg=${RB_QEMU_PACKAGE:-$(cd "$artifacts/qemu" 2>/dev/null && ls -d linux-ppc-r[0-9]* 2>/dev/null | sort -V | tail -n1 || true)}
qpkg=${qpkg:-linux-ppc}
qdir=$artifacts/qemu/$qpkg
if [ -d "$qdir/bin" ]; then
  mkdir -p "$out/qemu"; cp -a "$qdir/." "$out/qemu/"
  qreadme=$qdir/README.txt; [ -f "$qreadme" ] || qreadme=$artifacts/qemu/README.txt
  if [ -f "$qdir/manifest.json" ]; then
    python3 - "$qdir/manifest.json" "$qreadme" "$qdir" > "$out/qemu/component.json" <<'PY'
import json, re, sys, hashlib, os
m = json.load(open(sys.argv[1]))
readme = open(sys.argv[2]).read() if len(sys.argv) > 2 and os.path.isfile(sys.argv[2]) else ''
rev = re.search(r'(wave[0-9]+/[a-z0-9-]+) @ ([0-9a-f]{7,40})', readme)
print(json.dumps({'name': 'qemu', 'kind': 'artifact', 'path': sys.argv[3],
                  'manifest_sha256': hashlib.sha256(open(sys.argv[1], 'rb').read()).hexdigest(),
                  'branch': rev.group(1) if rev else None, 'revision': rev.group(2) if rev else None,
                  'features': m.get('features'),
                  'qemu': (m.get('sources') or {}).get('qemu'), 'classicmac': (m.get('sources') or {}).get('classicmac'),
                  'note': 'contains private firmware/driver binaries: images with this package stay private'}))
PY
  else meta qemu artifact "$qdir" unknown > "$out/qemu/component.json"; fi
fi
# The OS X runtime is an explicit separate package. Never select the classic
# emulator as a fallback, even when it carries experimental OS X feature flags.
opkg=${RB_QEMU_OSX_PACKAGE:-}
[ "$opkg" != none ] || opkg=
if [ -n "$opkg" ]; then
  odir=$artifacts/qemu/$opkg
  [ -f "$odir/manifest.json" ] && [ -f "$odir/bin/qemu-system-ppc" ] ||
    die "OS X emulator package $opkg is missing"
  mkdir -p "$out/qemu-osx"; cp -a "$odir/." "$out/qemu-osx/"
  PYTHONPATH="$staged/hostd/hostd" python3 - "$out/qemu-osx" "$odir" <<'PY' || die "OS X emulator package is not qualified or does not match its manifest"
import hashlib, json, pathlib, sys
from retroboot import osx
from retroboot.machines import verify_package
root = pathlib.Path(sys.argv[1])
manifest = root / 'manifest.json'
data = json.loads(manifest.read_text())
required = {'macosx-runtime-1', 'macosx-qemu-10.0-1', 'macosx-firmware-1',
            'macosx-nvram-flash-1', 'display-retroboot'}
if not required.issubset(data.get('features') or ()) or not osx.package_capable(root):
    sys.exit('the separate OS X runtime features are missing')
verify_package(root, osx.PACKAGE_FILES)
(root / 'component.json').write_text(json.dumps({
    'name': 'qemu-osx', 'kind': 'artifact', 'path': sys.argv[2],
    'manifest_sha256': hashlib.sha256(manifest.read_bytes()).hexdigest(),
    'features': data.get('features'), 'sources': data.get('sources'),
    'install': '/usr/lib/retroboot/qemu-osx; selected only by explicit OS X profiles'
}, indent=2) + '\n')
PY
  log "separate OS X emulator package: $opkg"
elif [ "${RB_REQUIRE_OSX:-0}" = 1 ]; then
  die "RB_REQUIRE_OSX=1 needs an explicit RB_QEMU_OSX_PACKAGE"
fi
# The 68k emulator package (linux-m68k-rN, D044): qemu-system-m68k for the 68k Macs
# (Quadra 800), installed beside the PowerPC package at /usr/lib/retroboot/qemu-m68k.
# RB_QEMU_M68K_PACKAGE names it ("none" leaves it out); by default the newest
# linux-m68k-rN (x86) or linux-m68k-rN-arm64 (RB_BUILD_CHROOT) in $artifacts/qemu.  Without
# it an image still builds and 68k Macs can't start (hostd says so); RB_REQUIRE_M68K=1 makes
# it required.  It must match its manifest and carry no Apple ROM (the Quadra 800 ROM
# travels with each 68k Mac, never in an image).
if [ -n "${RB_BUILD_CHROOT:-}" ]; then
  mdefault=$(cd "$artifacts/qemu" 2>/dev/null && ls -d linux-m68k-r[0-9]*-arm64 2>/dev/null | sort -V | tail -n1 || true)
else
  mdefault=$(cd "$artifacts/qemu" 2>/dev/null && ls -d linux-m68k-r[0-9]* 2>/dev/null | grep -v -- '-arm64$' | sort -V | tail -n1 || true)
fi
mpkg=${RB_QEMU_M68K_PACKAGE:-$mdefault}
[ "$mpkg" = none ] && mpkg=
mdir=$artifacts/qemu/$mpkg
if [ -n "$mpkg" ] && [ -f "$mdir/bin/qemu-system-m68k" ] && [ -f "$mdir/manifest.json" ]; then
  python3 - "$mdir" <<'PY' || die "68k emulator package $mpkg: not as its manifest says, or it holds an Apple ROM"
import hashlib, json, os, sys
d = sys.argv[1]
m = json.load(open(os.path.join(d, 'manifest.json')))
for rel in ('bin/qemu-system-m68k', 'share/qemu/mac_qfb.rom', 'share/qemu/classicvirtio-68k.rom',
            'share/qemu/q800-pram-seed.img'):
    want = ((m.get('files') or {}).get(rel) or {}).get('sha256')
    if not want or hashlib.sha256(open(os.path.join(d, rel), 'rb').read()).hexdigest() != want:
        sys.exit(f'{rel}: not in the manifest, or its hash differs')
APPLE_ROMS = {'05ad753fb594e656cf078023ec189e09e2a7655a780de993b75b8c51ed6b09ca'}   # Quadra 610/650/800
for base, _, files in os.walk(d):
    for n in files:
        p = os.path.join(base, n)
        if not os.path.islink(p) and os.path.getsize(p) <= 4 << 20 and \
                hashlib.sha256(open(p, 'rb').read()).hexdigest() in APPLE_ROMS:
            sys.exit(f'{os.path.relpath(p, d)} is an Apple ROM')
PY
  mkdir -p "$out/qemu-m68k"; cp -a "$mdir/." "$out/qemu-m68k/"
  python3 - "$mdir/manifest.json" "$mdir" > "$out/qemu-m68k/component.json" <<'PY'
import json, sys, hashlib
m = json.load(open(sys.argv[1]))
print(json.dumps({'name': 'qemu-m68k', 'kind': 'artifact', 'path': sys.argv[2],
                  'manifest_sha256': hashlib.sha256(open(sys.argv[1], 'rb').read()).hexdigest(),
                  'features': m.get('features'), 'qemu': (m.get('sources') or {}).get('qemu'),
                  'classicmac': (m.get('sources') or {}).get('classicmac'),
                  'install': '/usr/lib/retroboot/qemu-m68k (hostd quadra.py: the 68k Macs)'}))
PY
  log "68k emulator package: $mpkg"
elif [ -n "${RB_QEMU_M68K_PACKAGE:-}" ] && [ "$RB_QEMU_M68K_PACKAGE" != none ]; then
  die "68k emulator package $RB_QEMU_M68K_PACKAGE missing in $artifacts/qemu"
elif [ "${RB_REQUIRE_M68K:-0}" = 1 ]; then
  die "no 68k emulator package (linux-m68k-rN) in $artifacts/qemu"
else
  log "WARNING: no 68k emulator package: 68k Macs can't start on this image"
fi
# RetroBoot video driver variant (emulator/ndrv): installed beside the QEMU package
# (never over it, so the package manifest stays as built); hostd verifies it and
# passes -L <dir> ahead of the package's share/qemu.
# The variant that goes with the package: ndrv-<rN> beside linux-ppc-<rN> when it
# exists (r11: the built-in panel for laptops); else the newest ndrv-* built against
# this package's own qemu_vga.ndrv (its manifest's pinned_unmodified_sha256), so a
# package named otherwise (linux-ppc-r19-gfx-c1 found no ndrv-r19-gfx-c1 and its image
# went out with the stock driver, which switches Mac OS to 640 x 480 late in the
# start; image/reports/lenovo-2026-10-15.md); else the shared ndrv/.
ndrv=$artifacts/ndrv
if [ -f "$artifacts/ndrv-${qpkg#linux-ppc-}/qemu_vga.ndrv" ]; then
  ndrv=$artifacts/ndrv-${qpkg#linux-ppc-}
elif [ -f "$qdir/share/qemu/qemu_vga.ndrv" ]; then
  stock=$(sha256sum "$qdir/share/qemu/qemu_vga.ndrv" | cut -d' ' -f1)
  for cand in $(ls -d "$artifacts"/ndrv-r[0-9]* 2>/dev/null | sort -V -r); do
    if [ -f "$cand/manifest.json" ] && [ "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("pinned_unmodified_sha256", ""))' "$cand/manifest.json")" = "$stock" ]; then
      ndrv=$cand
      break
    fi
  done
fi
if [ -f "$ndrv/qemu_vga.ndrv" ]; then
  log "video driver variant: $(basename "$ndrv") for $qpkg"
else
  log "WARNING: no video driver variant for $qpkg; Mac OS gets the package's stock driver"
fi
if [ -f "$ndrv/qemu_vga.ndrv" ] && [ -f "$ndrv/manifest.json" ]; then
  want=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["sha256"])' "$ndrv/manifest.json")
  [ "$(sha256sum "$ndrv/qemu_vga.ndrv" | cut -d' ' -f1)" = "$want" ] || die "ndrv artifact hash mismatch"
  mkdir -p "$out/qemu-ndrv"
  cp "$ndrv/qemu_vga.ndrv" "$ndrv/manifest.json" "$out/qemu-ndrv/"
  python3 - "$ndrv/manifest.json" "$ndrv" > "$out/qemu-ndrv/component.json" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
print(json.dumps({'name': 'qemu-ndrv', 'kind': 'artifact', 'path': sys.argv[2], 'sha256': m.get('sha256'),
                  'replaces': 'qemu/share/qemu/qemu_vga.ndrv', 'replaces_sha256': m.get('pinned_unmodified_sha256'),
                  'source': m.get('source'), 'revision': m.get('repo_commit'), 'define': m.get('define'),
                  'install': '/usr/lib/retroboot/qemu-ndrv (hostd: -L before the package)'}))
PY
fi
# RetroBoot classicvirtio loader variant (emulator/classicvirtio: the shared folder's
# guest driver with directory caching and Finder refresh), installed beside the QEMU
# package like the video driver; hostd verifies it and loads it instead of the
# package's share/qemu/ndrvloader.  The newest classicvirtio-<N>/ unless RB_CLASSICVIRTIO.
cvdir=${RB_CLASSICVIRTIO:-$(cd "$artifacts" 2>/dev/null && ls -d classicvirtio-[0-9]* 2>/dev/null | sort -V | tail -n1 || true)}
if [ -n "$cvdir" ]; then
  case "$cvdir" in /*) ;; *) cvdir=$artifacts/$cvdir ;; esac
fi
if [ -n "$cvdir" ] && [ -f "$cvdir/ndrvloader" ] && [ -f "$cvdir/manifest.json" ]; then
  want=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["sha256"])' "$cvdir/manifest.json")
  [ "$(sha256sum "$cvdir/ndrvloader" | cut -d' ' -f1)" = "$want" ] || die "classicvirtio artifact hash mismatch"
  mkdir -p "$out/qemu-classicvirtio"
  cp "$cvdir/ndrvloader" "$cvdir/manifest.json" "$out/qemu-classicvirtio/"
  python3 - "$cvdir/manifest.json" "$cvdir" > "$out/qemu-classicvirtio/component.json" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
print(json.dumps({'name': 'qemu-classicvirtio', 'kind': 'artifact', 'path': sys.argv[2], 'sha256': m.get('sha256'),
                  'replaces': 'qemu/share/qemu/ndrvloader', 'replaces_sha256': m.get('pinned_unmodified_sha256'),
                  'source': m.get('source'), 'features': m.get('features'),
                  'install': '/usr/lib/retroboot/qemu-classicvirtio (hostd: loader instead of the package\'s)'}))
PY
fi
# RetroBoot Display (guest/rbdisplay): the guest helper that puts Mac OS's screen at the
# size hostd chose for the ATI Radeon.  hostd's ATI installer (rbguestprep.ati) adds it
# beside ATI's software.  The newest rbdisplay-<N>/ (manifest.json + tree/) unless RB_RBDISPLAY.
rddir=${RB_RBDISPLAY:-$(cd "$artifacts" 2>/dev/null && ls -d rbdisplay-[0-9]* 2>/dev/null | sort -V | tail -n1 || true)}
if [ -n "$rddir" ]; then
  case "$rddir" in /*) ;; *) rddir=$artifacts/$rddir ;; esac
fi
if [ -n "$rddir" ] && [ -f "$rddir/tree/manifest.json" ] && [ -f "$rddir/manifest.json" ]; then
  python3 - "$rddir" <<'PY' || die "rbdisplay artifact does not match its manifest"
import hashlib, json, os, sys
d = sys.argv[1]
m = json.load(open(os.path.join(d, 'manifest.json')))
for rel, digest in m['files_sha256'].items():
    if hashlib.sha256(open(os.path.join(d, rel), 'rb').read()).hexdigest() != digest:
        sys.exit(f'{rel}: hash mismatch')
PY
  mkdir -p "$out/rbdisplay"
  cp -a "$rddir/tree" "$rddir/manifest.json" "$out/rbdisplay/"
  python3 - "$rddir/manifest.json" "$rddir" > "$out/rbdisplay/component.json" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
print(json.dumps({'name': 'rbdisplay', 'kind': 'artifact', 'path': sys.argv[2], 'source': m.get('source'),
                  'version': m.get('version'),
                  'install': '/usr/lib/retroboot/guest/rbdisplay (rbguestprep.ati installs it into Mac OS)'}))
PY
fi
# RetroBoot Idle (guest/rbidle, D044): the 68k startup extension that stops the 68040 when
# Mac OS idles; rbguestprep.prep68k installs it into every 68k Mac.  The newest rbidle-<N>/
# (manifest.json + tree/) unless RB_RBIDLE.  Required with the 68k emulator: without it a
# 68k Mac keeps a host core busy the whole time.
ridir=${RB_RBIDLE:-$(cd "$artifacts" 2>/dev/null && ls -d rbidle-[0-9]* 2>/dev/null | sort -V | tail -n1 || true)}
if [ -n "$ridir" ]; then
  case "$ridir" in /*) ;; *) ridir=$artifacts/$ridir ;; esac
fi
if [ -n "$ridir" ] && [ -f "$ridir/tree/manifest.json" ] && [ -f "$ridir/manifest.json" ]; then
  python3 - "$ridir" <<'PY' || die "rbidle artifact does not match its manifest"
import hashlib, json, os, sys
d = sys.argv[1]
m = json.load(open(os.path.join(d, 'manifest.json')))
for rel, digest in m['files_sha256'].items():
    if hashlib.sha256(open(os.path.join(d, rel), 'rb').read()).hexdigest() != digest:
        sys.exit(f'{rel}: hash mismatch')
PY
  mkdir -p "$out/rbidle"
  cp -a "$ridir/tree" "$ridir/manifest.json" "$out/rbidle/"
  python3 - "$ridir/manifest.json" "$ridir" > "$out/rbidle/component.json" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
print(json.dumps({'name': 'rbidle', 'kind': 'artifact', 'path': sys.argv[2], 'source': m.get('source'),
                  'version': m.get('version'),
                  'install': '/usr/lib/retroboot/guest/rbidle (rbguestprep.prep68k installs it into 68k Macs)'}))
PY
elif [ -d "$out/qemu-m68k" ]; then
  die "the 68k emulator is staged but RetroBoot Idle (rbidle-<N>) is missing in $artifacts"
fi
# Runtime packages for every staged ELF binary (QEMU, presenter): resolve its
# shared libraries on the builder (same Debian release) and map them to packages.
# With RB_BUILD_CHROOT the same script runs inside the arm64 chroot on a copy.
scan=$out; scanner=(python3 -)
if [ -n "${RB_BUILD_CHROOT:-}" ]; then
  scan=/tmp/rb-components.$$                                    # per build: builds may run side by side
  rm -rf "$RB_BUILD_CHROOT$scan"; cp -a "$out" "$RB_BUILD_CHROOT$scan"
  scanner=(chroot "$RB_BUILD_CHROOT" /usr/bin/python3 -)
fi
"${scanner[@]}" "$scan" > "$out/runtime-packages.txt" <<'PY'
import os, subprocess, sys
root = sys.argv[1]
libs = set()
for base, _, files in os.walk(root):
    for name in files:
        path = os.path.join(base, name)
        if os.path.islink(path) or not os.access(path, os.X_OK):
            continue
        with open(path, 'rb') as f:
            if f.read(4) != b'\x7fELF':
                continue
        out = subprocess.run(['ldd', path], capture_output=True, text=True).stdout
        for line in out.splitlines():
            if '=>' in line and 'not found' in line:
                sys.exit(f'fetch-components: {path}: {line.strip()}')
            parts = line.split('=>')
            if len(parts) == 2 and parts[1].strip().startswith('/'):
                libs.add(os.path.realpath(parts[1].split()[0]))
pkgs = set()
for lib in sorted(libs):
    res = subprocess.run(['dpkg', '-S', lib], capture_output=True, text=True)
    if res.returncode:  # /lib vs /usr/lib aliasing on merged-/usr systems
        alt = lib.replace('/usr/lib/', '/lib/', 1) if lib.startswith('/usr/lib/') else lib
        res = subprocess.run(['dpkg', '-S', alt], capture_output=True, text=True)
    if res.returncode:
        sys.exit(f'fetch-components: no package owns {lib}')
    pkgs.add(res.stdout.split(':')[0].strip())
print('\n'.join(sorted(pkgs)))
PY
[ -z "${RB_BUILD_CHROOT:-}" ] || rm -rf "$RB_BUILD_CHROOT$scan"
# plus data packages a component declares (e.g. xkb-data for rb-presenter's keymaps)
if [ -n "${ptar:-}" ] && [ -f "$artifacts/presenter/manifest.json" ]; then
  python3 -c 'import json,sys; m=json.load(open(sys.argv[1])); print("\n".join(sorted({p for v in m.get("runtime_packages", {}).values() for p in v})))' \
    "$artifacts/presenter/manifest.json" >> "$out/runtime-packages.txt"
  sort -u -o "$out/runtime-packages.txt" "$out/runtime-packages.txt"
fi
log "runtime packages for staged binaries: $(wc -l < "$out/runtime-packages.txt")"
mkdir -p "$out/lab"
[ -f "$RB_WORK_ROOT/src/lab/authorized_keys" ] && cp "$RB_WORK_ROOT/src/lab/authorized_keys" "$out/lab/"
# Software Update trust anchors: the public halves of the update signing keys
# (image/lab/stage_update_keys.sh copies them from private/update-keys/).
mkdir -p "$out/update-keys"
for k in "$RB_WORK_ROOT"/src/update-keys/*.pub; do [ -f "$k" ] && cp "$k" "$out/update-keys/"; done
[ -f "$RB_WORK_ROOT/src/update-keys/revoked" ] && cp "$RB_WORK_ROOT/src/update-keys/revoked" "$out/update-keys/"
ls "$out/update-keys" | grep -q '\.pub$' || log "WARNING: no update signing key staged; this image cannot verify updates"
ls "$out" >&2
