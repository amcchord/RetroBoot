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
