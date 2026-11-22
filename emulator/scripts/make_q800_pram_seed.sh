#!/usr/bin/env bash
# Make the Quadra 800 PRAM seed (share/qemu/q800-pram-seed.img of linux-m68k-rN):
# two Mac OS starts and Shut Downs from an all-zero PRAM, first without the
# virtio card (a cold PRAM with classicvirtio's card hangs the ROM), then with
# the contract argv; then the nubus-qfb card's slot PRAM mode byte (0x68) is set
# to DEPTH (Mac OS's first-start depth).  The seed holds settings written by the
# Apple ROM and Mac OS, no code; it stays private like the ROM (never in Git).
#
#   emulator/scripts/make_q800_pram_seed.sh PKG ROM MACOS_DISK RUN_DIR OUT [DEPTH]
#
# PKG is a linux-m68k package directory (bin/, share/qemu with mac_qfb.rom and
# classicvirtio-68k.rom); ROM the Quadra 800 ROM; MACOS_DISK a Mac OS 7.5.5-8.1
# disk (copied, never written); RUN_DIR a fresh directory for the two runs.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG="$1"; ROM="$2"; DISK="$3"; RUN="$4"; OUT="$5"; DEPTH="${6:-8}"
python3 "$HERE/../tools/rbd_quadra_lab.py" seed --pkg "$PKG" --rom "$ROM" --disk "$DISK" --run "$RUN" \
  --out "$OUT" --size 800x600x8 --seed-depth "$DEPTH" --quiet 1.5 --timeout 120
