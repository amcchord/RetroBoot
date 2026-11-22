"""Which addresses the helper may fetch, and who may use it.

The helper fetches for Mac OS only.  It refuses every target that would let
the guest reach the RetroBoot computer itself or link-local services:

* loopback (127.0.0.0/8, ::1), unspecified, link-local (169.254/16, fe80::/10),
  multicast, broadcast and reserved ranges, IPv4-mapped forms of those;
* every address configured on this computer (the management web pages, the
  API, SSH in lab builds — whatever listens on the host);
* QEMU's guest network (10.0.2.0/24 by default): from Mac OS, 10.0.2.2 *is*
  this computer and 10.0.2.3 its DNS proxy;
* ports browsers refuse (the Fetch standard's "bad ports": mail, SSH, ...).

Hosts on the local network are allowed by default, as in any browser
(``allow_lan``); turning that off also refuses private, CGNAT and ULA ranges.
Names are resolved here and the connection uses exactly the vetted
addresses, so a DNS answer cannot change between the check and the connect.

``peer_uid`` maps an accepted loopback connection to the user that owns the
client socket (``/proc/net/tcp``): only QEMU's ``rbguest`` user is served.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import time
from pathlib import Path

# Fetch standard "bad ports" (https://fetch.spec.whatwg.org/#port-blocking).
BAD_PORTS = frozenset({
    1, 7, 9, 11, 13, 15, 17, 19, 20, 21, 22, 23, 25, 37, 42, 43, 53, 69, 77, 79, 87, 95, 101, 102,
    103, 104, 109, 110, 111, 113, 115, 117, 119, 123, 135, 137, 139, 143, 161, 179, 389, 427, 465,
    512, 513, 514, 515, 526, 530, 531, 532, 540, 548, 554, 556, 563, 587, 601, 636, 989, 990, 993,
    995, 1719, 1720, 1723, 2049, 3659, 4045, 4190, 5060, 5061, 6000, 6566, 6665, 6666, 6667, 6668,
    6669, 6679, 6697, 10080,
})

_ALWAYS = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "192.0.0.0/24", "192.0.2.0/24",
    "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
    "::/128", "::1/128", "fe80::/10", "fec0::/10", "ff00::/8", "64:ff9b:1::/48", "100::/64",
    "2001:db8::/32",
)]
_LAN = [ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "fc00::/7",
)]


class Refused(Exception):
    """A target the helper will not fetch.  ``kind``: host | lan | port | scheme."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


def local_addresses() -> set[ipaddress._BaseAddress]:
    """Every address configured on this computer (IPv4 from fib_trie, IPv6 from if_inet6)."""
    out: set = set()
    try:
        lines = Path("/proc/net/fib_trie").read_text().splitlines()
        for i, line in enumerate(lines):
            if "/32 host LOCAL" in line and i:
                addr = lines[i - 1].strip().split()[-1]
                try:
                    out.add(ipaddress.ip_address(addr))
                except ValueError:
                    pass
    except OSError:
        pass
    try:
        for line in Path("/proc/net/if_inet6").read_text().splitlines():
            h = line.split()[0]
            try:
                out.add(ipaddress.ip_address(":".join(h[i:i + 4] for i in range(0, 32, 4))))
            except ValueError:
                pass
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            out.add(ipaddress.ip_address(info[4][0].split("%")[0]))
    except (OSError, ValueError):
        pass
    return out


class Policy:
    def __init__(self, allow_lan: bool = True, guest_network: str = "10.0.2.0/24",
                 extra_blocked: list[str] | None = None, local_refresh_s: float = 10.0):
        self.allow_lan = allow_lan
        self.guest_net = ipaddress.ip_network(guest_network)
        self.extra = [ipaddress.ip_network(n, strict=False) for n in (extra_blocked or [])]
        self._local: set = set()
        self._local_t = 0.0
        self.local_refresh_s = local_refresh_s
        self.local_provider = local_addresses

    def locals(self) -> set:
        now = time.monotonic()
        if now - self._local_t > self.local_refresh_s:
            try:
                self._local = set(self.local_provider())
            except Exception:
                pass
            self._local_t = now
        return self._local

    def check_ip(self, ip: str | ipaddress._BaseAddress) -> None:
        addr = ipaddress.ip_address(str(ip).split("%")[0]) if not isinstance(ip, ipaddress._BaseAddress) else ip
        if isinstance(addr, ipaddress.IPv6Address):
            mapped = addr.ipv4_mapped or (addr.sixtofour if addr in ipaddress.ip_network("2002::/16") else None)
            if mapped is not None:
                self.check_ip(mapped)
        if addr == ipaddress.ip_address("255.255.255.255") or any(addr in n for n in _ALWAYS):
            raise Refused("host", "addresses on this computer and link-local addresses")
        if addr.version == 4 and addr in self.guest_net:
            raise Refused("host", "this RetroBoot computer itself")       # 10.0.2.2 is the host as Mac OS sees it
        if addr in self.locals() or any(addr in n for n in self.extra):
            raise Refused("host", "this RetroBoot computer itself")
        if not self.allow_lan and any(addr in n for n in _LAN):
            raise Refused("lan", "computers on the local network")

    @staticmethod
    def check_port(port: int) -> None:
        if not 0 < port < 65536 or port in BAD_PORTS:
            raise Refused("port", f"port {port}")

    @staticmethod
    def literal(host: str) -> ipaddress._BaseAddress | None:
        h = host.strip("[]")
        try:
            return ipaddress.ip_address(h.split("%")[0])
        except ValueError:
            pass
        # Legacy numeric forms the guest browser might send (http://2130706433/, 0x7f.1, 017.0.0.1)
        if h and all(c in "0123456789abcdefxABCDEFX." for c in h) and any(c.isdigit() for c in h):
            try:
                return ipaddress.ip_address(socket.inet_ntoa(socket.inet_aton(h)))
            except OSError:
                return None
        return None

    def check_name(self, host: str) -> None:
        """Names that never leave this computer."""
        h = host.rstrip(".").lower()
        if h in ("localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback") \
                or h.endswith((".localhost", ".invalid")) or not h:
            raise Refused("host", "names on this computer")
        if h == socket.gethostname().lower():
            raise Refused("host", "this computer's own name")

    async def resolve(self, loop, host: str, port: int) -> list[dict]:
        """Vetted addresses for a connection, in aiohttp resolver format."""
        self.check_port(port)
        lit = self.literal(host)
        if lit is not None:
            self.check_ip(lit)
            fam = socket.AF_INET6 if lit.version == 6 else socket.AF_INET
            return [{"hostname": host, "host": str(lit), "port": port, "family": fam, "proto": 0,
                     "flags": socket.AI_NUMERICHOST}]
        self.check_name(host)
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        out, seen = [], set()
        for fam, _, proto, _, sa in infos:
            if fam not in (socket.AF_INET, socket.AF_INET6):
                continue
            ip = sa[0].split("%")[0]
            # One forbidden answer refuses the name: a mixed answer is a rebinding attempt.
            self.check_ip(ip)
            if ip in seen:
                continue
            seen.add(ip)
            out.append({"hostname": host, "host": ip, "port": port, "family": fam, "proto": proto,
                        "flags": socket.AI_NUMERICHOST})
        if not out:
            raise OSError(f"no addresses for {host}")
        # IPv4 first: many home networks have broken IPv6 routes.
        out.sort(key=lambda r: 0 if r["family"] == socket.AF_INET else 1)
        return out


# -- who is connecting -----------------------------------------------------------------

def _hex_addr(ip: str, port: int) -> set[str]:
    """/proc/net/tcp{,6} spellings of an address:port."""
    addr = ipaddress.ip_address(ip)
    out = set()
    if addr.version == 4:
        out.add(f"{int.from_bytes(addr.packed, 'little'):08X}:{port:04X}")
        mapped = ipaddress.IPv6Address("::ffff:" + ip)
        addr6 = mapped
    else:
        addr6 = addr
        if addr.ipv4_mapped:
            out |= _hex_addr(str(addr.ipv4_mapped), port)
    p = addr6.packed
    words = [p[i:i + 4] for i in range(0, 16, 4)]
    out.add("".join(f"{int.from_bytes(w, 'little'):08X}" for w in words) + f":{port:04X}")
    return out


def peer_uid(peer: tuple, proc_net: str = "/proc/net") -> int | None:
    """Owner of the client socket of a loopback connection (None if unknown)."""
    try:
        want = _hex_addr(peer[0].split("%")[0], int(peer[1]))
    except (ValueError, IndexError, TypeError):
        return None
    for name in ("tcp", "tcp6"):
        try:
            with open(os.path.join(proc_net, name)) as f:
                next(f, None)
                for line in f:
                    parts = line.split()
                    if len(parts) > 7 and parts[1] in want and parts[3] == "01":
                        return int(parts[7])
        except OSError:
            continue
    return None
