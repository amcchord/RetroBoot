# Shared helpers for the arm64 boards (sourced, not executed): the Raspberry Pi image
# (image/rpi/) and the Gemini PDA root (image/gemini/). Factored out of image/rpi/scripts
# without changing what the Raspberry Pi build does: the same commands, in the same
# order, with the same cache keys. The caller has sourced image/scripts/lib.sh (cfg, log,
# die, CACHE, IMAGE_SRC) through its board library.

# arm64_base_rootfs FLAVOR OUT.tar CACHE_PREFIX PACKAGES_DIR [EXTRA_PACKAGES_FILE]
#   Stage 1 of an arm64 board: Debian 13 arm64 minbase through the builder's qemu-user
#   binfmt (mmdebstrap, no recommends, image/config/dpkg-excludes), cached as
#   $CACHE/<CACHE_PREFIX>-base-<flavor>-<key>.tar and linked to OUT.tar. Packages come
#   from PACKAGES_DIR/packages.common + packages.<flavor> (+ the extra file).
#   The caller sets:
#     ARM64_SOURCES    array of apt source lines (one argument each)
#     ARM64_KEY_FILES  array of further files whose content is part of the cache key
#   and may define (called only on a cache miss):
#     arm64_keyring_dir         prints a --keyring directory (removed afterwards)
#     arm64_setup_hook DIR      prints an mmdebstrap --setup-hook command
arm64_base_rootfs() {
  local flavor=${1:?flavor} out=${2:?output tarball} prefix=${3:?cache prefix} pkgdir=${4:?packages dir} extra=${5:-}
  local suite components packages key cached keys= line
  suite=$(cfg debian_suite); components=$(cfg debian_components)
  packages=$(grep -hv '^\s*#' "$pkgdir/packages.common" "$pkgdir/packages.$flavor" ${extra:+"$extra"} | awk 'NF' | sort -u | paste -sd, -)
  key=$( { printf '%s\n' "${ARM64_SOURCES[@]}"; echo "$packages"; cat "$IMAGE_SRC/config/dpkg-excludes" \
          ${ARM64_KEY_FILES[@]+"${ARM64_KEY_FILES[@]}"}; } | sha256sum | cut -c1-16)
  mkdir -p "$CACHE"
  cached=$CACHE/$prefix-base-$flavor-$key.tar
  if [ -s "$cached" ] && [ "${RB_REFRESH_BASE:-0}" != 1 ]; then
    log "arm64 base rootfs cache hit ($cached)"
  else
    local args=(--mode=root --variant=minbase --architectures=arm64 --format=tar)
    if declare -F arm64_keyring_dir >/dev/null; then
      keys=$(arm64_keyring_dir)
      args+=(--keyring="$keys")
    fi
    log "mmdebstrap $suite arm64 ($flavor): $(echo "$packages" | tr ',' ' ' | wc -w) packages"
    args+=(--components="${components// /,}"
           --aptopt='APT::Install-Recommends "false"'
           --aptopt='Acquire::Languages "none"')
    while read -r line; do [ -n "$line" ] && args+=(--dpkgopt="$line"); done < "$IMAGE_SRC/config/dpkg-excludes"
    if declare -F arm64_setup_hook >/dev/null; then
      args+=(--setup-hook="$(arm64_setup_hook "$keys")")
    fi
    args+=(--include="$packages"
           --customize-hook='chroot "$1" dpkg-query -W -f="\${Package}\t\${Version}\t\${Architecture}\n" > "$1/.rb-packages.tsv"')
    mmdebstrap "${args[@]}" "$suite" "$cached.tmp" "${ARM64_SOURCES[@]}" || { [ -z "$keys" ] || rm -rf "$keys"; die "mmdebstrap failed"; }
    [ -z "$keys" ] || rm -rf "$keys"
    mv "$cached.tmp" "$cached"
  fi
  ln -sf "$cached" "$out"
}

# arm64_firstboot_done ROOT
#   Work the first boot would do, done at build time, and x86-only units kept off.
#   * No battery-backed clock is assumed: systemd starts the clock at least at the build
#     time (/usr/lib/clock-epoch) and then at the last time systemd-timesyncd saved
#     (/var/lib/systemd/timesync/clock, on the root), both before any service starts.
#   * The journal catalog and the "/etc and /var are up to date with /usr" stamps, so
#     ldconfig.service and systemd-journal-catalog-update.service (ConditionNeedsUpdate)
#     don't hold up sysinit.target at the first start (12 s in QEMU).
#   * x86-only services (early microcode, live installer) are skipped by their conditions
#     anyway; keep them off. retroboot-bootmark (the x86 lab's serial milestone on ttyS0)
#     too: a ttyS0 can exist where no UART is wired, and the write fails the unit.
arm64_firstboot_done() {
  local root=${1:?rootdir} ns f unit
  touch "$root/usr/lib/clock-epoch"
  chroot "$root" ldconfig
  chroot "$root" journalctl --update-catalog
  ns=$(stat -c %Y "$root/usr")000000000
  for f in etc/.updated var/.updated; do
    printf '%s\n' '# This file was created by systemd-update-done. The timestamp below is the' \
      '# modification time of /usr/ for which the most recent updates of /etc/ and' \
      '# /var/ have been applied. See man systemd-update-done.service(8).' "TIMESTAMP_NSEC=$ns" > "$root/$f"
    touch -r "$root/usr" "$root/$f"
  done
  for unit in retroboot-microcode.service retroboot-live-toram.service retroboot-bootmark.service; do
    [ -e "$root/usr/lib/systemd/system/$unit" ] && chroot "$root" systemctl disable "$unit" >/dev/null 2>&1 || true
  done
}

# arm64_pi_check FLAVOR ROOT
#   Debug flavor: the emulator workstream's pi-check bundle (bit-for-bit PowerPC FP/AltiVec/
#   branch-cache check of the installed qemu-system-ppc against x86 r18; a bare-metal test
#   ELF, no Mac OS or ROM): sh /usr/lib/retroboot/lab/pi-check/pi-check.sh
#   RB_PI_CHECK overrides the bundle directory.
arm64_pi_check() {
  local flavor=${1:?flavor} root=${2:?rootdir} pichk=${RB_PI_CHECK:-/srv/retroboot/wave6/arm-emu/pi-check}
  if [ "$flavor" = debug ] && [ -f "$pichk/pi-check.sh" ] && [ -f "$pichk/expected.txt" ]; then
    install -d "$root/usr/lib/retroboot/lab"
    rm -rf "$root/usr/lib/retroboot/lab/pi-check"; cp -a "$pichk" "$root/usr/lib/retroboot/lab/pi-check"
    chown -R 0:0 "$root/usr/lib/retroboot/lab/pi-check"
    python3 - "$root/usr/lib/retroboot/components.json" "$pichk" <<'PY'
import hashlib, json, os, sys
path, src = sys.argv[1:]
data = json.load(open(path))
data['pi-check'] = {'name': 'pi-check', 'kind': 'artifact', 'path': src, 'install': '/usr/lib/retroboot/lab/pi-check',
                    'files_sha256': {f: hashlib.sha256(open(os.path.join(src, f), 'rb').read()).hexdigest()
                                     for f in sorted(os.listdir(src)) if os.path.isfile(os.path.join(src, f))}}
json.dump(data, open(path, 'w'), indent=2, sort_keys=True)
PY
    log "pi-check bundle installed (debug flavor)"
  fi
}

# arm64_populate_data DATADIR ROOT [MACHINE_DIR MACHINE_ID]
#   The data directory layout (/var/lib/retroboot: RBDATA on the Raspberry Pi, a plain
#   directory of the root on the Gemini) and, for private builds, the embedded guest
#   machine (sparse copy; the source is only read) with its machine.json and the Platinum
#   asset bundle built from the embedded guest's own disk (as x86).
arm64_populate_data() {
  local mnt=${1:?data dir} root=${2:?rootdir} machine_dir=${3:-} machine_id=${4:-} jgid rbdata_gid f
  install -d -m 0755 "$mnt/logs" "$mnt/machines" "$mnt/config" "$mnt/share" "$mnt/checkpoints"
  install -d -m 0700 "$mnt/state"
  jgid=$(awk -F: '$1=="systemd-journal"{print $3}' "$root/etc/group")
  install -d -m 2755 -g "${jgid:-0}" "$mnt/logs/journal"
  if [ -n "$machine_dir" ]; then
    log "embedding guest machine '$machine_id' (sparse copy, original untouched)"
    install -d -m 0755 "$mnt/machines/$machine_id"
    for f in "$machine_dir"/*; do
      cp --sparse=always --reflink=never "$f" "$mnt/machines/$machine_id/"
    done
    chmod u+w "$mnt/machines/$machine_id"/*
    python3 "$IMAGE_SRC/scripts/make-machine-json.py" "$mnt/machines/$machine_id" "$machine_id" "$root/usr/lib/retroboot/hostd"
    rbdata_gid=$(awk -F: '$1=="rbdata"{print $3}' "$root/etc/group")
    chgrp -R "${rbdata_gid:-0}" "$mnt/machines"; chmod 2770 "$mnt/machines" "$mnt/machines/$machine_id"
    chmod 0660 "$mnt/machines/$machine_id"/*
    if [ -d "$root/usr/lib/retroboot/platinum/rbplatinum" ] && [ -f "$mnt/machines/$machine_id/disk.img" ]; then
      log "building the Platinum asset bundle from the embedded guest disk"
      if PYTHONPATH="$root/usr/lib/retroboot/platinum" RB_GUESTPREP_PATH="$root/usr/lib/retroboot/guestprep" \
          python3 -m rbplatinum build --source "$mnt/machines/$machine_id/disk.img" \
          --out "$mnt/assets/platinum" >&2; then
        chmod -R u=rwX,go=rX "$mnt/assets"
      else
        log "Platinum bundle not built (hostd builds it on first launch instead)"
        rm -rf "$mnt/assets/platinum"
      fi
    fi
  fi
}

# arm64_check_elves ROOT
#   Every host-side ELF RetroBoot installs is AArch64. Prints the "ok: ELF ..." line or
#   the offending files followed by "FAIL: ..." and returns 1. The emulator package's
#   share/qemu holds guest firmware for other architectures (OpenBIOS PowerPC, SPARC ...)
#   and the debug pi-check bundle a PowerPC test program: both run inside the emulator.
arm64_check_elves() {
  local root=${1:?rootdir} bad=0 n=0 guestfw=0 f m
  while IFS= read -r -d '' f; do
    case "$f" in "$root"/usr/lib/retroboot/qemu/share/*|"$root"/usr/lib/retroboot/lab/pi-check/*) guestfw=$((guestfw + 1)); continue ;; esac
    head -c4 "$f" 2>/dev/null | grep -q $'\x7fELF' || continue
    n=$((n + 1))
    m=$(readelf -h "$f" 2>/dev/null | sed -n 's/^ *Machine: *//p')
    [ "$m" = AArch64 ] || { echo "not aarch64: ${f#"$root"} ($m)"; bad=$((bad + 1)); }
  done < <(find "$root/usr/lib/retroboot" "$root/usr/bin" "$root/usr/sbin" "$root/usr/lib/aarch64-linux-gnu" -type f -print0)
  [ "$bad" = 0 ] || { echo "FAIL: $bad ELF files are not aarch64"; return 1; }
  echo "ok: ELF: $n files under /usr/lib/retroboot, /usr/bin, /usr/sbin, /usr/lib/aarch64-linux-gnu, all AArch64 (guest code in qemu/share and the debug pi-check bundle not counted: $guestfw files)"
}
