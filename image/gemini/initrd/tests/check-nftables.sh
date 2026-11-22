#!/bin/bash
# nftables and module checks for the Gemini kernel (rb-builder, root).
#
# 1. Renders the two rulesets RetroBoot loads (hostd's retroboot_guest table and the
#    Modern Web Helper's retroboot_webproxy table) from the given repository checkout
#    and checks them with `nft -c -f` (the builder's kernel validates the transaction,
#    then it is aborted; nothing is applied). Copies end up in $WORK/tests/cache/nft/ for
#    run-qemu-tests.sh's nftables scenario.
# 2. Unpacks the kernel's modules tarball and checks, with the shipped modules.dep and
#    aliases: required modules present (or built in), `modprobe --show-depends` resolves
#    them, nf_tables' autoload aliases for fib and reject resolve, depmod finds no
#    unresolved symbols against System.map, and modules.dep matches a fresh depmod.
#
# Usage: check-nftables.sh REPO_DIR [KERNEL_OUT_DIR]
set -euo pipefail
REPO=${1:?usage: check-nftables.sh REPO_DIR [KERNEL_OUT_DIR]}
WORK=${RB_GEMINI_WORK:-/srv/retroboot/wave7/gemini-kernel}
KDIR=$(readlink -f "${2:-$WORK/out/kernel-latest}")
KREL=$(cat "$KDIR/kernel.release")
NFTDIR=$WORK/tests/cache/nft
mkdir -p "$NFTDIR"
T=$(mktemp -d /tmp/rb-nftcheck.XXXXXX)
trap 'rm -rf "$T"' EXIT
PASSES=0; FAILS=0
ok() { if "${@:2}" >/dev/null 2>&1; then PASSES=$((PASSES + 1)); echo "  PASS $1"; else FAILS=$((FAILS + 1)); echo "  FAIL $1"; fi; }

echo "rulesets (from $REPO)"
python3 -I - "$REPO/hostd/retroboot/guestnet.py" "$REPO/webproxy/rbwebproxy/nft.py" "$NFTDIR" <<'PY'
import importlib.util, sys
def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod
g = load(sys.argv[1], "rb_guestnet"); w = load(sys.argv[2], "rb_webproxy_nft")
# The unit's defaults: helper port 8080, DNS 8053; a stand-in uid for rbguest.
open(f"{sys.argv[3]}/guestnet.nft", "w").write(g.ruleset(4242, (8080,), (8053,)))
open(f"{sys.argv[3]}/webproxy.nft", "w").write(w.ruleset(4242, 8080, 8053))
PY
for f in guestnet webproxy; do
  ok "nft -c accepts the $f table" nft -c -f "$NFTDIR/$f.nft"
done
nft -c -f "$NFTDIR/guestnet.nft" 2>&1 | head -5 || true

echo "modules ($KDIR, $KREL)"
zstd -dc "$KDIR/modules-$KREL.tar.zst" | tar -C "$T" -xf -
M=$T/usr/lib/modules/$KREL
MP=(modprobe -d "$T/usr" -S "$KREL")
has_mod() {  # module file present, or built in
  find "$M" -name "$1.ko" | grep -q . || grep -q "/$1.ko\$" "$M/modules.builtin"
}
for m in nf_tables nft_nat nft_redir nft_masq nf_nat; do
  ok "$m built in" grep -q "/$m.ko\$" "$M/modules.builtin"
done
for m in nft_fib nft_fib_ipv4 nft_fib_ipv6 nft_fib_inet nft_reject nft_reject_inet nft_reject_ipv4 nft_reject_ipv6 ipv6 \
         mt6351-gauge mtk_wcn wlan_gen3 hci_stp geminipda-drm panfrost; do
  ok "$m present" has_mod "$m"
done
ok "mt6351-gauge in extra/" test -f "$M/extra/mt6351-gauge.ko"
ok "mt6351-keys built in" grep -q '/mt6351-keys.ko$' "$M/modules.builtin"
for m in nft_fib_inet nft_reject_inet mt6351_gauge mtk_wcn wlan_gen3 hci_stp geminipda_drm; do
  ok "modprobe resolves $m" "${MP[@]}" --show-depends "$m"
done
"${MP[@]}" --show-depends nft_fib_inet | sed 's/^/    /'
for a in nft-expr-1-fib nft-expr-1-reject nft-expr-2-fib nft-expr-10-fib; do
  ok "alias $a resolves" sh -c "${MP[*]} -R $a | grep -q ."
done
ok "depmod: no unresolved symbols" sh -c "! depmod -b '$T/usr' -e -F '$KDIR/System.map' -n '$KREL' 2>&1 >/dev/null | grep -q 'unknown symbol'"
mkdir -p "$T/fresh" && cp -a "$T/usr" "$T/fresh/" && depmod -b "$T/fresh/usr" "$KREL"
ok "shipped modules.dep = fresh depmod" cmp -s "$M/modules.dep" "$T/fresh/usr/lib/modules/$KREL/modules.dep"
ok "vermagic of nft_fib_inet" sh -c "modinfo -F vermagic '$(find "$M" -name nft_fib_inet.ko)' | grep -q '^$KREL SMP preempt mod_unload aarch64'"

echo "nftables: $PASSES passed, $FAILS failed"
[ "$FAILS" = 0 ]
