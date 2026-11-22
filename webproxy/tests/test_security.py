"""The helper never fetches from this computer or link-local space, and serves Mac OS only."""
import asyncio
import ipaddress
import os
import socket
import tempfile
import unittest

import support  # noqa: F401
from rbwebproxy.security import BAD_PORTS, Policy, Refused, _hex_addr, peer_uid


def policy(**kw):
    p = Policy(**kw)
    p.local_provider = lambda: {ipaddress.ip_address("192.168.1.50"), ipaddress.ip_address("2001:db8:1::50")}
    return p


class FakeLoop:
    def __init__(self, answers):
        self.answers = answers

    async def getaddrinfo(self, host, port, type=0):
        out = []
        for ip in self.answers[host]:
            fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
            out.append((fam, socket.SOCK_STREAM, 6, "", (ip, port) if fam == socket.AF_INET else (ip, port, 0, 0)))
        return out


class AddressTests(unittest.TestCase):
    def refused(self, p, ip, kind="host"):
        with self.assertRaises(Refused) as cm:
            p.check_ip(ip)
        self.assertEqual(cm.exception.kind, kind, ip)

    def test_host_local_and_link_local(self):
        p = policy()
        for ip in ("127.0.0.1", "127.8.9.10", "::1", "0.0.0.0", "169.254.169.254", "fe80::1", "224.0.0.251",
                   "255.255.255.255", "::ffff:127.0.0.1", "::ffff:169.254.1.1", "::", "2002:7f00:1::1"):
            self.refused(p, ip)

    def test_this_computers_addresses(self):
        p = policy()
        self.refused(p, "192.168.1.50")          # hostd's management address on the LAN
        self.refused(p, "2001:db8:1::50")
        self.refused(p, "10.0.2.2")              # the host as Mac OS sees it
        self.refused(p, "10.0.2.3")

    def test_lan_is_a_choice(self):
        policy(allow_lan=True).check_ip("192.168.1.20")
        policy(allow_lan=True).check_ip("10.1.2.3")
        self.refused(policy(allow_lan=False), "192.168.1.20", "lan")
        self.refused(policy(allow_lan=False), "100.64.1.1", "lan")
        self.refused(policy(allow_lan=False), "fd00::5", "lan")
        policy(allow_lan=False).check_ip("93.184.216.34")

    def test_ports(self):
        for port in (22, 25, 53, 110, 143, 6667, 0, 70000):
            with self.assertRaises(Refused):
                Policy.check_port(port)
        for port in (80, 443, 8080, 8000, 3000):
            Policy.check_port(port)
        self.assertIn(25, BAD_PORTS)

    def test_literal_forms(self):
        p = policy()
        for host in ("2130706433", "0x7f000001", "017700000001", "127.1", "[::1]", "0177.0.0.1"):
            lit = p.literal(host)
            self.assertIsNotNone(lit, host)
            with self.assertRaises(Refused):
                p.check_ip(lit)

    def test_local_names(self):
        p = policy()
        for name in ("localhost", "LOCALHOST.", "foo.localhost", "x.invalid", socket.gethostname()):
            with self.assertRaises(Refused):
                p.check_name(name)
        p.check_name("example.com")


class ResolveTests(unittest.IsolatedAsyncioTestCase):
    async def test_rebinding_refused(self):
        p = policy()
        loop = FakeLoop({"evil.example": ["93.184.216.34", "127.0.0.1"], "good.example": ["93.184.216.34", "2606:2800::1"],
                         "hostd.example": ["192.168.1.50"]})
        with self.assertRaises(Refused):
            await p.resolve(loop, "evil.example", 80)
        with self.assertRaises(Refused):
            await p.resolve(loop, "hostd.example", 443)
        res = await p.resolve(loop, "good.example", 443)
        self.assertEqual([r["host"] for r in res], ["93.184.216.34", "2606:2800::1"])
        self.assertTrue(all(r["flags"] == socket.AI_NUMERICHOST for r in res))

    async def test_literal_and_port_checked(self):
        p = policy()
        with self.assertRaises(Refused):
            await p.resolve(asyncio.get_running_loop(), "127.0.0.1", 80)
        with self.assertRaises(Refused):
            await p.resolve(asyncio.get_running_loop(), "93.184.216.34", 25)
        res = await p.resolve(asyncio.get_running_loop(), "93.184.216.34", 80)
        self.assertEqual(res[0]["host"], "93.184.216.34")


class PeerTests(unittest.TestCase):
    def test_hex_forms(self):
        self.assertIn("0100007F:1F90", _hex_addr("127.0.0.1", 8080))
        self.assertIn("0000000000000000FFFF00000100007F:1F90", _hex_addr("127.0.0.1", 8080))

    def test_peer_uid_from_proc(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "tcp"), "w") as f:
                f.write("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n")
                f.write("   0: 0100007F:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 1\n")
                f.write("   1: 3201A8C0:C350 22D8B85D:0050 01 00000000:00000000 00:00000000 00000000   997        0 2\n")
            with open(os.path.join(d, "tcp6"), "w") as f:
                f.write("  sl  local_address remote_address st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n")
            self.assertEqual(peer_uid(("192.168.1.50", 50000), d), 997)   # QEMU's redirected socket
            self.assertIsNone(peer_uid(("127.0.0.1", 8080), d))          # a listener, not a client
            self.assertIsNone(peer_uid(("127.0.0.1", 1), d))

    def test_peer_uid_live(self):
        """A real loopback connection maps to this process's uid."""
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        c = socket.create_connection(srv.getsockname())
        s, peer = srv.accept()
        try:
            self.assertEqual(peer_uid(peer), os.getuid())
        finally:
            c.close()
            s.close()
            srv.close()


if __name__ == "__main__":
    unittest.main()
