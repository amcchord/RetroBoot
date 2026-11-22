"""DNS for Mac OS while the helper runs.

Mac OS asks QEMU's DNS proxy (10.0.2.3), which asks this computer's resolver;
while the helper runs, those queries come here (``nft.py``).  Everything is
forwarded unchanged to the resolvers in /etc/resolv.conf, with one
exception: a name in the home-page redirect table that **no longer resolves**
(``livepage.apple.com``, ``home.netscape.com``) gets an A record for the
helper's address (10.0.2.2, this computer as Mac OS sees it), so the browser
connects and the helper sends it to the new home page.  Names that still
resolve keep their real answer; their web traffic reaches the helper anyway.

Answers for redirected names carry a 30 s TTL, so turning the helper or the
redirects off takes effect within half a minute.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import random
import struct
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger("rbwebproxy.dns")

TYPE_A, TYPE_AAAA, CLASS_IN = 1, 28, 1
RCODE_NOERROR, RCODE_SERVFAIL, RCODE_NXDOMAIN = 0, 2, 3
SYNTH_TTL = 30
UPSTREAM_TIMEOUT = 2.5
MAX_INFLIGHT = 128


class DNSError(Exception):
    pass


def parse_question(msg: bytes) -> tuple[int, int, str, int, int, int]:
    """(id, flags, qname, qtype, qclass, end offset of the question)."""
    if len(msg) < 12:
        raise DNSError("short message")
    qid, flags, qd, _an, _ns, _ar = struct.unpack("!6H", msg[:12])
    if qd != 1:
        raise DNSError("expected one question")
    labels, off = [], 12
    while True:
        if off >= len(msg):
            raise DNSError("truncated name")
        n = msg[off]
        off += 1
        if n == 0:
            break
        if n & 0xC0:
            raise DNSError("compressed question name")
        labels.append(msg[off:off + n].decode("ascii", "replace"))
        off += n
        if off > 12 + 255:
            raise DNSError("name too long")
    if off + 4 > len(msg):
        raise DNSError("truncated question")
    qtype, qclass = struct.unpack("!HH", msg[off:off + 4])
    return qid, flags, ".".join(labels).lower(), qtype, qclass, off + 4


def answer(msg: bytes, address: str | None, rcode: int = RCODE_NOERROR) -> bytes:
    """A reply to msg with one A record (or none)."""
    qid, flags, _name, qtype, _qclass, qend = parse_question(msg)
    rd = flags & 0x0100
    out_flags = 0x8000 | 0x0400 | rd | 0x0080 | (rcode & 0xF)    # QR, AA, RD copied, RA
    an = 1 if (address and qtype == TYPE_A and rcode == RCODE_NOERROR) else 0
    head = struct.pack("!6H", qid, out_flags, 1, an, 0, 0)
    body = msg[12:qend]
    if an:
        body += struct.pack("!HHHIH", 0xC00C, TYPE_A, CLASS_IN, SYNTH_TTL, 4) + \
            ipaddress.IPv4Address(address).packed
    return head + body


def has_address(reply: bytes) -> bool:
    """True when an upstream reply carries at least one A record (else NXDOMAIN, NODATA...)."""
    try:
        _qid, flags, _an_count = struct.unpack("!HHxxH", reply[:8])
    except struct.error:
        return False
    if flags & 0xF != RCODE_NOERROR:
        return False
    try:
        _i, _f, _n, _t, _c, off = parse_question(reply)
    except DNSError:
        return False
    an = struct.unpack("!H", reply[6:8])[0]
    for _ in range(an):
        # skip the owner name
        while off < len(reply):
            n = reply[off]
            if n & 0xC0 == 0xC0:
                off += 2
                break
            off += 1
            if n == 0:
                break
            off += n
        if off + 10 > len(reply):
            return False
        rtype, _cls, _ttl, rdlen = struct.unpack("!HHIH", reply[off:off + 10])
        if rtype == TYPE_A:
            return True
        off += 10 + rdlen
    return False


def resolvers(path: str = "/etc/resolv.conf", exclude: set[tuple[str, int]] | None = None) -> list[str]:
    out = []
    try:
        for line in Path(path).read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "nameserver":
                ip = parts[1].split("%")[0]
                try:
                    ipaddress.ip_address(ip)
                except ValueError:
                    continue
                if (ip, 53) not in (exclude or set()):
                    out.append(ip)
    except OSError:
        pass
    return out or ["127.0.0.53"]


class _UDPClient(asyncio.DatagramProtocol):
    def __init__(self, fut: asyncio.Future, qid: int):
        self.fut = fut
        self.qid = qid

    def datagram_received(self, data, addr):
        if len(data) >= 2 and struct.unpack("!H", data[:2])[0] == self.qid and not self.fut.done():
            self.fut.set_result(data)

    def error_received(self, exc):
        if not self.fut.done():
            self.fut.set_exception(exc)


class Forwarder:
    def __init__(self, redirect_names: Callable[[], set[str]], address: Callable[[], str],
                 resolv_conf: str = "/etc/resolv.conf", upstream: list[tuple[str, int]] | None = None):
        self.redirect_names = redirect_names
        self.address = address
        self.resolv_conf = resolv_conf
        self.fixed_upstream = upstream
        self._resolvers: list[str] = []
        self._resolv_mtime = None
        self._inflight = 0
        self.transports: list = []
        self.tcp_servers: list = []
        self.counts = {"queries": 0, "synthesized": 0, "failed": 0}

    def upstreams(self) -> list[tuple[str, int]]:
        if self.fixed_upstream:
            return self.fixed_upstream
        try:
            mt = Path(self.resolv_conf).stat().st_mtime
        except OSError:
            mt = None
        if mt != self._resolv_mtime or not self._resolvers:
            self._resolvers = resolvers(self.resolv_conf)
            self._resolv_mtime = mt
        return [(ip, 53) for ip in self._resolvers]

    async def _udp_query(self, server: tuple[str, int], msg: bytes) -> bytes:
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        qid = struct.unpack("!H", msg[:2])[0]
        fam = 10 if ":" in server[0] else 2
        transport, _ = await loop.create_datagram_endpoint(lambda: _UDPClient(fut, qid), family=fam,
                                                           remote_addr=server)
        try:
            transport.sendto(msg)
            return await asyncio.wait_for(fut, UPSTREAM_TIMEOUT)
        finally:
            transport.close()

    async def _tcp_query(self, server: tuple[str, int], msg: bytes) -> bytes:
        r, w = await asyncio.wait_for(asyncio.open_connection(server[0], server[1]), UPSTREAM_TIMEOUT)
        try:
            w.write(struct.pack("!H", len(msg)) + msg)
            await w.drain()
            n = struct.unpack("!H", await asyncio.wait_for(r.readexactly(2), UPSTREAM_TIMEOUT * 2))[0]
            return await asyncio.wait_for(r.readexactly(n), UPSTREAM_TIMEOUT * 2)
        finally:
            w.close()

    async def forward(self, msg: bytes, tcp: bool = False) -> bytes | None:
        # A fresh random ID upstream (the guest's IDs are predictable), restored on the way back.
        orig = msg[:2]
        qid = random.getrandbits(16)
        out = struct.pack("!H", qid) + msg[2:]
        for server in self.upstreams():
            try:
                reply = await (self._tcp_query(server, out) if tcp else self._udp_query(server, out))
            except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, struct.error):
                continue
            if reply[:2] == out[:2]:
                return orig + reply[2:]
        return None

    async def resolve(self, msg: bytes, tcp: bool = False) -> bytes | None:
        self.counts["queries"] += 1
        try:
            _qid, flags, name, qtype, qclass, _ = parse_question(msg)
        except DNSError:
            return None
        if flags & 0x8000:
            return None                                    # a response, not a query
        names = self.redirect_names()
        if name.rstrip(".") in names and qclass == CLASS_IN:
            reply = await self.forward(msg, tcp)
            if reply is not None and (has_address(reply) or (qtype != TYPE_A and _rcode(reply) == RCODE_NOERROR)):
                return reply                               # still alive: real answer
            self.counts["synthesized"] += 1
            return answer(msg, self.address())             # A -> helper; other types: no data
        reply = await self.forward(msg, tcp)
        if reply is None:
            self.counts["failed"] += 1
            try:
                return answer(msg, None, RCODE_SERVFAIL)
            except DNSError:
                return None
        return reply

    # -- servers ------------------------------------------------------------------------
    async def start(self, hosts: list[str], port: int) -> None:
        loop = asyncio.get_running_loop()
        fwd = self

        class UDPServer(asyncio.DatagramProtocol):
            def connection_made(self, transport):
                self.transport = transport

            def datagram_received(self, data, addr):
                if fwd._inflight >= MAX_INFLIGHT:
                    return
                fwd._inflight += 1
                loop.create_task(self._reply(data, addr))

            async def _reply(self, data, addr):
                try:
                    reply = await fwd.resolve(data)
                    if reply is not None:
                        if len(reply) > 512 and not _has_edns(data):
                            reply = _truncate(reply)
                        self.transport.sendto(reply, addr)
                except Exception:
                    log.exception("DNS query failed")
                finally:
                    fwd._inflight -= 1

        async def tcp_client(reader, writer):
            try:
                while True:
                    n = struct.unpack("!H", await asyncio.wait_for(reader.readexactly(2), 10))[0]
                    msg = await asyncio.wait_for(reader.readexactly(n), 10)
                    reply = await fwd.resolve(msg, tcp=True)
                    if reply is None:
                        break
                    writer.write(struct.pack("!H", len(reply)) + reply)
                    await writer.drain()
            except (asyncio.IncompleteReadError, asyncio.TimeoutError, OSError, struct.error):
                pass
            finally:
                writer.close()

        for host in hosts:
            try:
                t, _ = await loop.create_datagram_endpoint(UDPServer, local_addr=(host, port))
                self.transports.append(t)
                self.tcp_servers.append(await asyncio.start_server(tcp_client, host, port, reuse_address=True))
            except OSError as e:
                if ":" in host:
                    log.warning("DNS not listening on [%s]:%s: %s", host, port, e)
                    continue
                raise

    async def close(self) -> None:
        for t in self.transports:
            t.close()
        for s in self.tcp_servers:
            s.close()
            await s.wait_closed()
        self.transports, self.tcp_servers = [], []


def _rcode(msg: bytes) -> int:
    try:
        return struct.unpack("!H", msg[2:4])[0] & 0xF
    except struct.error:
        return RCODE_SERVFAIL


def _has_edns(msg: bytes) -> bool:
    try:
        return struct.unpack("!H", msg[10:12])[0] > 0
    except struct.error:
        return False


def _truncate(reply: bytes) -> bytes:
    """Header and question only, with TC set: the client retries over TCP."""
    try:
        _i, _f, _n, _t, _c, qend = parse_question(reply)
    except DNSError:
        return reply[:512]
    qid, flags = struct.unpack("!HH", reply[:4])
    return struct.pack("!6H", qid, flags | 0x0200, 1, 0, 0, 0) + reply[12:qend]


__all__ = ["Forwarder", "answer", "parse_question", "has_address", "resolvers", "time"]
