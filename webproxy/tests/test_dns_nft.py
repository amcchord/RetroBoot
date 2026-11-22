"""DNS forwarding with redirects for dead home-page names, and the nftables rules."""
import asyncio
import ipaddress
import json
import os
import stat
import struct
import sys
import tempfile
import unittest

import support  # noqa: F401
from rbwebproxy import dns, nft


def query(name: str, qtype: int = dns.TYPE_A, qid: int = 0x1234) -> bytes:
    q = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00"
    return struct.pack("!6H", qid, 0x0100, 1, 0, 0, 0) + q + struct.pack("!HH", qtype, dns.CLASS_IN)


def a_records(reply: bytes) -> list[str]:
    _qid, _flags, _qd, an = struct.unpack("!4H", reply[:8])
    *_, off = dns.parse_question(reply)
    out = []
    for _ in range(an):
        off += 2                                  # compressed owner name
        rtype, _c, _t, rdlen = struct.unpack("!HHIH", reply[off:off + 10])
        if rtype == dns.TYPE_A:
            out.append(str(ipaddress.IPv4Address(reply[off + 10:off + 14])))
        off += 10 + rdlen
    return out


class FakeUpstream(asyncio.DatagramProtocol):
    """live.example and www.example resolve; everything else is NXDOMAIN."""

    def connection_made(self, t):
        self.t = t
        self.seen = []

    def datagram_received(self, data, addr):
        qid, flags, name, qtype, _c, end = dns.parse_question(data)
        self.seen.append((qid, name))
        if name in ("live.example", "www.example"):
            self.t.sendto(dns.answer(data, "198.51.100.7"), addr)
        else:
            self.t.sendto(dns.answer(data, None, dns.RCODE_NXDOMAIN), addr)


class ForwarderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        loop = asyncio.get_running_loop()
        self.ut, self.up = await loop.create_datagram_endpoint(FakeUpstream, local_addr=("127.0.0.1", 0))
        port = self.ut.get_extra_info("sockname")[1]
        self.names = {"livepage.apple.com", "live.example"}
        self.fwd = dns.Forwarder(lambda: self.names, lambda: "10.0.2.2", upstream=[("127.0.0.1", port)])
        await self.fwd.start(["127.0.0.1"], 0)
        self.port = self.fwd.transports[0].get_extra_info("sockname")[1]

    async def asyncTearDown(self):
        await self.fwd.close()
        self.ut.close()

    async def ask(self, msg: bytes) -> bytes:
        loop = asyncio.get_running_loop()
        fut = loop.create_future()

        class C(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                fut.set_result(data)
        t, _ = await loop.create_datagram_endpoint(C, remote_addr=("127.0.0.1", self.port))
        t.sendto(msg)
        try:
            return await asyncio.wait_for(fut, 5)
        finally:
            t.close()

    async def test_dead_homepage_points_at_helper(self):
        reply = await self.ask(query("LivePage.Apple.com"))
        self.assertEqual(reply[:2], b"\x12\x34")
        self.assertEqual(a_records(reply), ["10.0.2.2"])
        self.assertEqual(self.fwd.counts["synthesized"], 1)

    async def test_live_redirect_name_keeps_real_answer(self):
        self.assertEqual(a_records(await self.ask(query("live.example"))), ["198.51.100.7"])

    async def test_other_names_forwarded_with_fresh_ids(self):
        reply = await self.ask(query("www.example", qid=0x0001))
        self.assertEqual(a_records(reply), ["198.51.100.7"])
        self.assertEqual(reply[:2], b"\x00\x01")
        self.assertNotEqual(self.up.seen[-1][0], 0x0001)          # upstream sees a random ID
        reply = await self.ask(query("nothing.example"))
        self.assertEqual(struct.unpack("!H", reply[2:4])[0] & 0xF, dns.RCODE_NXDOMAIN)

    async def test_aaaa_for_dead_name_is_empty(self):
        reply = await self.ask(query("livepage.apple.com", dns.TYPE_AAAA))
        self.assertEqual(struct.unpack("!H", reply[6:8])[0], 0)
        self.assertEqual(struct.unpack("!H", reply[2:4])[0] & 0xF, dns.RCODE_NOERROR)

    async def test_tcp(self):
        port = self.fwd.tcp_servers[0].sockets[0].getsockname()[1]
        r, w = await asyncio.open_connection("127.0.0.1", port)
        q = query("livepage.apple.com")
        w.write(struct.pack("!H", len(q)) + q)
        n = struct.unpack("!H", await r.readexactly(2))[0]
        self.assertEqual(a_records(await r.readexactly(n)), ["10.0.2.2"])
        w.close()

    async def test_garbage_ignored(self):
        self.assertIsNone(await self.fwd.resolve(b"\x00\x01"))

    def test_resolv_conf(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write("# x\nnameserver 192.168.1.1\nnameserver fe80::1%eth0\nnameserver bogus\nsearch lan\n")
        try:
            self.assertEqual(dns.resolvers(f.name), ["192.168.1.1", "fe80::1"])
        finally:
            os.unlink(f.name)


class NftTests(unittest.TestCase):
    def test_ruleset(self):
        rs = nft.ruleset(997, 8080, 8053)
        self.assertIn("meta skuid 997 meta nfproto ipv4 tcp dport 80 redirect to :8080", rs)
        self.assertIn("meta skuid 997 udp dport 53 redirect to :8053", rs)
        self.assertIn("meta skuid 997 tcp dport 53 redirect to :8053", rs)
        self.assertTrue(rs.startswith("add table inet retroboot_webproxy\ndelete table inet retroboot_webproxy\n"))
        self.assertNotIn("udp dport 53", nft.ruleset(997, 8080, None))

    def run_cli(self, cfg: dict, action="add"):
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "calls")
            fake = os.path.join(d, "nft")
            with open(fake, "w") as f:
                f.write(f"#!{sys.executable}\nimport sys\nopen({log!r}, 'a').write(repr(sys.argv[1:]) + '\\n' + sys.stdin.read() + '\\n---\\n')\n")
            os.chmod(fake, stat.S_IRWXU)
            path = os.path.join(d, "cfg.json")
            with open(path, "w") as f:
                json.dump(cfg, f)
            rc = nft.main([action, "--config", path, "--nft", fake])
            calls = open(log).read() if os.path.exists(log) else ""
            return rc, calls

    def test_cli_add_and_remove(self):
        rc, calls = self.run_cli({"intercept_uid": 997, "port": 8080, "dns_port": 8053})
        self.assertEqual(rc, 0)
        self.assertIn("['-f', '-']", calls)
        self.assertIn("meta skuid 997 meta nfproto ipv4 tcp dport 80 redirect to :8080", calls)
        rc, calls = self.run_cli({}, "remove")
        self.assertIn("['delete', 'table', 'inet', 'retroboot_webproxy']", calls)

    def test_never_root_and_missing_user(self):
        rc, calls = self.run_cli({"intercept_uid": 0, "intercept_user": "root"})
        self.assertEqual(rc, 1)
        self.assertNotIn("redirect", calls)
        rc, calls = self.run_cli({"intercept_user": "no-such-user-rb"})
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
