# Shared helpers for the Raspberry Pi image scripts (sourced, not executed).
# The x86 helpers (cfg, log, die, require_root, RB_WORK_ROOT, CACHE) come from
# image/scripts/lib.sh; rcfg reads config/rpi.conf first and falls back to image.conf.
RPI_SRC=${RPI_SRC:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
IMAGE_SRC=${IMAGE_SRC:-$(cd "$RPI_SRC/.." && pwd)}
. "$IMAGE_SRC/scripts/lib.sh"
. "$IMAGE_SRC/scripts/arm64-lib.sh"
rcfg() {
  local v
  v=$(sed -n "s/^$1=//p" "$RPI_SRC/config/rpi.conf" | tail -n1)
  if [ -n "$v" ]; then echo "$v"; else cfg "$1"; fi
}
# apt keyring directory for mmdebstrap --keyring: Debian's archive keys and the Raspberry
# Pi archive key, the latter checked against rpi.conf's fingerprint first. A fresh
# directory under /tmp (the caller removes it): apt verifies signatures as _apt, which
# cannot reach the work root under /srv/retroboot (0700).
rpi_keyring_dir() {
  local dir want got
  want=$(rcfg rpi_archive_key_fpr)
  got=$(gpg --show-keys --with-colons "$RPI_SRC/apt/raspberrypi-archive-keyring.asc" 2>/dev/null | awk -F: '/^fpr/{print $10; exit}')
  [ "$got" = "$want" ] || die "Raspberry Pi archive key fingerprint $got differs from the pinned $want"
  dir=$(mktemp -d /tmp/rb-rpi-keyring.XXXXXX); chmod 0755 "$dir"
  install -m 0644 /usr/share/keyrings/debian-archive-keyring.gpg "$dir/"
  gpg --dearmor < "$RPI_SRC/apt/raspberrypi-archive-keyring.asc" > "$dir/retroboot-raspberrypi.gpg"
  chmod 0644 "$dir/retroboot-raspberrypi.gpg"
  echo "$dir"
}
# Debian + Raspberry Pi sources for mmdebstrap (one argument per line).
rpi_sources() {
  local suite mirror security components
  suite=$(cfg debian_suite); mirror=$(cfg debian_mirror); security=$(cfg debian_security_mirror)
  components=$(cfg debian_components)
  echo "deb $mirror $suite $components"
  echo "deb $mirror $suite-updates $components"
  echo "deb $security $suite-security $components"
  echo "deb $(rcfg rpi_mirror) $(rcfg rpi_suite) $(rcfg rpi_components)"
}
# setup hook: apt pinning and the archive key inside the new root (kept in the image).
rpi_setup_hook() {
  printf '%s' "mkdir -p \"\$1/etc/apt/preferences.d\" \"\$1/etc/apt/trusted.gpg.d\" && cp '$RPI_SRC/config/apt-preferences' \"\$1/etc/apt/preferences.d/retroboot-raspberrypi.pref\" && cp '$1/retroboot-raspberrypi.gpg' \"\$1/etc/apt/trusted.gpg.d/retroboot-raspberrypi.gpg\""
}
