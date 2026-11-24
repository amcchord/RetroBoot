# Shared helpers for RetroBoot image scripts (sourced, not executed).
IMAGE_SRC=${IMAGE_SRC:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
RB_WORK_ROOT=${RB_WORK_ROOT:-/srv/retroboot/wave2/image}
CACHE=${CACHE:-$RB_WORK_ROOT/cache}
cfg() { sed -n "s/^$1=//p" "$IMAGE_SRC/config/image.conf" | tail -n1; }
log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }
require_root() { [ "$(id -u)" = 0 ] || die "must run as root (loop devices, chroot)"; }
# Device-node stand-ins (customize-rootfs.sh, on a builder that may not create device nodes):
# release the bind mounts of the builder's nodes under ROOT/dev once the root is packed, or
# before a new build replaces ROOT.  Nothing to do on an ordinary builder.
release_dev_binds() {
  local m
  [ -d "${1:?root}/dev" ] || return 0
  for m in $(awk -v d="$(readlink -f "$1")/dev/" 'index($5, d) == 1 {print $5}' /proc/self/mountinfo | sort -r); do
    umount -l "$m" 2>/dev/null || true
  done
}
# Optional build-time startup chimes (external RB_CHIMES_DIR/*.wav only; never committed to Git).
stage_startup_chimes() {
  local root=${1:?rootdir} dir=${RB_CHIMES_DIR:-} count=0 f
  if [ -z "$dir" ]; then
    log "chimes: RB_CHIMES_DIR is unset; skipping startup chime staging"
    return 0
  fi
  [ -d "$dir" ] || die "RB_CHIMES_DIR does not exist: $dir"
  shopt -s nullglob
  local wavs=("$dir"/*.wav)
  shopt -u nullglob
  if [ ${#wavs[@]} -eq 0 ]; then
    log "chimes: no *.wav files found in $dir; skipping"
    return 0
  fi
  install -d -m 0755 "$root/usr/share/retroboot/chimes" || die "failed to create chimes directory"
  for f in "${wavs[@]}"; do
    local dst="$root/usr/share/retroboot/chimes/$(basename "$f")"
    install -m 0644 "$f" "$dst" || die "failed to install chime: $f"
    [ "$(id -u)" != 0 ] || chown 0:0 "$dst"
    count=$((count + 1))
  done
  [ "$(id -u)" != 0 ] || chown 0:0 "$root/usr/share/retroboot/chimes"
  log "chimes: staged $count chime file(s) from $dir to /usr/share/retroboot/chimes"
}
