"""Guest isolation: Mac OS can't reach services on this computer.

QEMU's user-mode network maps the guest's 10.0.2.2 (and every other address in
10.0.2.0/24 except the DNS proxy) to host loopback, so without a rule Mac OS
could open anything listening on 127.0.0.1 or on the host's own addresses:
hostd's HTTPS management port, its loopback HTTP API, SSH in lab builds.

A permanent nftables table, ``inet retroboot_guest``, refuses every connection
from the QEMU user's sockets (``meta skuid rbguest``) to an address of this
computer (``fib daddr type local``: all of 127.0.0.0/8, ::1 and every
interface address), except:

* DNS (port 53) to a local resolver, so QEMU's own DNS forwarding works when
  /etc/resolv.conf names a local stub (systemd-resolved's 127.0.0.53, a local
  dnsmasq), and nothing else on port 53 is reachable;
* the Modern Web Helper's listen ports (web and DNS), which Mac OS reaches after
  its traffic is redirected (nat happens before this filter) or when a browser
  is configured to use 10.0.2.2:8080 as its proxy.

TCP gets a reset and everything else an ICMP "administratively prohibited", so
Mac OS sees an immediate refusal instead of waiting for a timeout.  Internet and
LAN access, the AirPort bridge and the shared volume (Unix sockets and files,
not IP) and inbound connections to hostd from the LAN are unaffected: the rule
only matches packets the QEMU user sends.

The table is loaded at boot by ``retroboot-guestnet.service`` (before the guest
can start) and checked by hostd at start; it does not depend on the Web
Helper's setting.

    python3 -m retroboot.guestnet apply|remove|status|show [--config hostd.json]
"""
from __future__ import annotations

import argparse
import json
import pwd
import subprocess
import sys
from pathlib import Path

TABLE = "retroboot_guest"
DEFAULT_CONFIG = "/etc/retroboot/hostd.json"
DEFAULT_OVERLAY = "/var/lib/retroboot/config/hostd.local.json"


def ruleset(uid: int, helper_ports: tuple[int, ...] = (8080,), helper_dns_ports: tuple[int, ...] = (8053,),
            table: str = TABLE) -> str:
    """The table, replacing any earlier copy atomically."""
    web = ", ".join(str(p) for p in sorted(set(helper_ports)))
    dns = ", ".join(str(p) for p in sorted(set(helper_dns_ports) | {53}))
    return f"""add table inet {table}
delete table inet {table}
table inet {table} {{
  chain output {{
    type filter hook output priority filter; policy accept;
    meta skuid {uid} jump guest
  }}
  chain guest {{
    # Mac OS (QEMU's user network) may use these services on this computer:
    # DNS (a local resolver stub, the Modern Web Helper's forwarder) and the helper's web port.
    fib daddr type local meta l4proto {{ tcp, udp }} th dport {{ {dns} }} accept
    fib daddr type local tcp dport {{ {web} }} accept
    # ... and nothing else on this computer: hostd's web and API ports, SSH, anything on loopback.
    fib daddr type local meta l4proto tcp counter reject with tcp reset
    fib daddr type local counter reject with icmpx admin-prohibited
  }}
}}
"""


def guest_uid(user: str) -> int:
    return pwd.getpwnam(user).pw_uid


def settings_from(config_path: str | None, overlay_path: str | None = None) -> dict:
    """run_as_user and the helper ports from hostd.json and this computer's overlay on RBDATA
    (config.py; missing files: defaults).  The overlay is read when no explicit config is given."""
    out = {"run_as_user": "rbguest", "webproxy_port": 8080, "webproxy_dns_port": 8053}
    sources = [config_path or DEFAULT_CONFIG]
    if overlay_path or config_path is None:
        sources.append(overlay_path or DEFAULT_OVERLAY)
    for src in sources:
        try:
            data = json.loads(Path(src).read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            for k in out:
                if k in data and data[k] is not None:
                    out[k] = data[k]
    return out


def nft(args: list[str], script: str | None = None, binary: str = "nft") -> subprocess.CompletedProcess:
    return subprocess.run([binary] + args, input=script, text=True, capture_output=True, timeout=20)


def apply(config_path: str | None = None, binary: str = "nft", overlay_path: str | None = None) -> tuple[bool, str]:
    s = settings_from(config_path, overlay_path)
    try:
        uid = guest_uid(s["run_as_user"])
    except KeyError:
        return False, f"guest user {s['run_as_user']!r} does not exist"
    if uid == 0:
        return False, "the guest runs as root; refusing to filter root's traffic"
    p = nft(["-f", "-"], ruleset(uid, (int(s["webproxy_port"]),), (int(s["webproxy_dns_port"]),)), binary)
    if p.returncode != 0:
        return False, (p.stderr or "nft failed").strip()[:300]
    return True, f"guest uid {uid} isolated from this computer"


def present(binary: str = "nft") -> bool | None:
    """True when the table is loaded, False when not, None when nft can't tell."""
    try:
        p = nft(["list", "table", "inet", TABLE], binary=binary)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.returncode == 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="retroboot.guestnet", description="Keep Mac OS away from this computer's services")
    ap.add_argument("action", choices=("apply", "remove", "status", "show"))
    ap.add_argument("--config", default=None, help="hostd.json (default /etc/retroboot/hostd.json)")
    ap.add_argument("--nft", default="nft")
    a = ap.parse_args(argv)
    if a.action == "show":
        s = settings_from(a.config)
        try:
            uid = guest_uid(s["run_as_user"])
        except KeyError:
            uid = 0
        sys.stdout.write(ruleset(uid, (int(s["webproxy_port"]),), (int(s["webproxy_dns_port"]),)))
        return 0
    if a.action == "remove":
        nft(["delete", "table", "inet", TABLE], binary=a.nft)
        return 0
    if a.action == "status":
        ok = present(a.nft)
        print("loaded" if ok else "missing")
        return 0 if ok else 1
    ok, msg = apply(a.config, a.nft)
    print(msg, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
