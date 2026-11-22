# Shared helpers for the Gemini PDA image scripts (sourced, not executed).
# The x86 helpers (cfg, log, die, require_root, RB_WORK_ROOT, CACHE) come from
# image/scripts/lib.sh and the arm64 ones from image/scripts/arm64-lib.sh; gcfg reads
# config/gemini.conf first and falls back to image.conf.
GEMINI_SRC=${GEMINI_SRC:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
IMAGE_SRC=${IMAGE_SRC:-$(cd "$GEMINI_SRC/.." && pwd)}
. "$IMAGE_SRC/scripts/lib.sh"
. "$IMAGE_SRC/scripts/arm64-lib.sh"
gcfg() {
  local v
  v=$(sed -n "s/^$1=//p" "$GEMINI_SRC/config/gemini.conf" | tail -n1)
  if [ -n "$v" ]; then echo "$v"; else cfg "$1"; fi
}
# Every value of a key that may repeat (upstream_file=...).
gcfg_all() { sed -n "s/^$1=//p" "$GEMINI_SRC/config/gemini.conf"; }
# JSON field of the kernel handoff (empty when missing).
khandoff() {
  python3 -c 'import json,sys
d=json.load(open(sys.argv[1]))
for k in sys.argv[2].split("."):
    d = d.get(k) if isinstance(d, dict) else None
print("" if d is None else (json.dumps(d) if isinstance(d, (dict, list)) else d))' "${RB_KERNEL_HANDOFF:-$(gcfg kernel_handoff)}" "$1"
}
