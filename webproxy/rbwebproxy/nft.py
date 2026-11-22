"""Send Mac OS's web and DNS traffic to the helper while it runs (root only).

QEMU's user-mode network turns every guest connection into an ordinary
socket of the emulator process, owned by the ``rbguest`` user.  An nftables
table owned by RetroBoot redirects exactly those sockets:

* TCP port 80 (plain web, to any address) -> the helper's port;
* UDP and TCP port 53 (the DNS questions QEMU's DNS proxy forwards) -> the
  helper's DNS port.

Nothing else is touched: no other user, no other port, no forwarding, no
LAN exposure.  The unit adds the table after the helper is ready
(``ExecStartPost=+``) and deletes it whenever the helper stops or crashes
(``ExecStopPost=+``), so Mac OS falls back to direct connections instead of
a dead port.

    python3 -m rbwebproxy.nft add|remove|status [--config PATH]
"""
from __future__ import annotations

import argparse
import json
import pwd
import subprocess
import sys

TABLE = "retroboot_webproxy"


def ruleset(uid: int, port: int, dns_port: int | None) -> str:
    # Web: IPv4 only.  Mac OS 9 has no IPv6, so QEMU's user network opens IPv4 sockets for
    # its connections; the helper listens on 127.0.0.1 only.  DNS: both families, because
    # QEMU forwards the guest's questions to whatever resolver this computer uses (the
    # helper's DNS listens on 127.0.0.1 and ::1).
    rules = [f"meta skuid {uid} meta nfproto ipv4 tcp dport 80 redirect to :{port}"]
    if dns_port:
        rules += [f"meta skuid {uid} udp dport 53 redirect to :{dns_port}",
                  f"meta skuid {uid} tcp dport 53 redirect to :{dns_port}"]
    body = "\n".join("    " + r for r in rules)
    # "add table; delete table; table {...}" replaces any earlier copy atomically.
    return (f"add table inet {TABLE}\ndelete table inet {TABLE}\n"
            f"table inet {TABLE} {{\n  chain output {{\n    type nat hook output priority -100; policy accept;\n"
            f"{body}\n  }}\n}}\n")


def _uid(cfg: dict) -> int:
    v = cfg.get("intercept_uid")
    if isinstance(v, int) and v > 0:
        return v
    name = cfg.get("intercept_user") or "rbguest"
    return pwd.getpwnam(name).pw_uid


def run_nft(script: str | None, args: list[str], nft: str = "nft") -> subprocess.CompletedProcess:
    return subprocess.run([nft] + args, input=script, text=True, capture_output=True, timeout=20)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rbwebproxy.nft")
    ap.add_argument("action", choices=("add", "remove", "status"))
    ap.add_argument("--config", default="/run/retroboot/webproxy.json")
    ap.add_argument("--nft", default="nft")
    a = ap.parse_args(argv)
    if a.action == "remove":
        p = run_nft(None, ["delete", "table", "inet", TABLE], a.nft)
        return 0                                        # absent table is fine
    if a.action == "status":
        p = run_nft(None, ["list", "table", "inet", TABLE], a.nft)
        sys.stdout.write(p.stdout)
        return 0 if p.returncode == 0 else 1
    try:
        with open(a.config) as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        print(f"cannot read {a.config}: {e}", file=sys.stderr)
        return 1
    if not cfg.get("intercept", True):
        run_nft(None, ["delete", "table", "inet", TABLE], a.nft)
        return 0
    try:
        uid = _uid(cfg)
    except KeyError:
        print("guest user missing; not redirecting", file=sys.stderr)
        return 1
    if uid == 0:
        print("refusing to redirect root's traffic", file=sys.stderr)
        return 1
    script = ruleset(uid, int(cfg.get("port", 8080)), int(cfg.get("dns_port", 8053)) if cfg.get("dns", True) else None)
    p = run_nft(script, ["-f", "-"], a.nft)
    if p.returncode != 0:
        print(p.stderr.strip()[:500], file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
