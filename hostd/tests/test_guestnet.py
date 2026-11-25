"""Guest isolation: the QEMU user can't reach this computer's services (retroboot/guestnet.py)."""
import asyncio
import errno
import json
from pathlib import Path
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest

import helpers
from retroboot import guestnet
from retroboot.hostd import Hostd

TEST_UID = 64999            # no process on the builder runs as this uid
TEST_TABLE = "rbtest_guestnet"


class RulesetTests(unittest.TestCase):
    def test_rules(self):
        rs = guestnet.ruleset(997, (8080,), (8053,))
        self.assertTrue(rs.startswith("add table inet retroboot_guest\ndelete table inet retroboot_guest\n"))
        self.assertIn("type filter hook output priority filter; policy accept;", rs)
        self.assertIn("meta skuid 997 jump guest", rs)
        lines = [l.strip() for l in rs.splitlines()]
        dns = lines.index("fib daddr type local meta l4proto { tcp, udp } th dport { 53, 8053 } accept")
        web = lines.index("fib daddr type local tcp dport { 8080 } accept")
        prn = lines.index("fib daddr type local tcp dport { 515, 631, 9100 } accept")
        tcp = lines.index("fib daddr type local meta l4proto tcp counter reject with tcp reset")
        rest = lines.index("fib daddr type local counter reject with icmpx admin-prohibited")
        self.assertTrue(dns < tcp and web < prn < tcp < rest)       # allowances come before the refusals
        self.assertNotIn("drop", rs.replace("# ", ""))

    def test_rules_custom_printer_ports(self):
        rs_custom = guestnet.ruleset(997, (8080,), (8053,), printer_ports=(631, 515))
        self.assertIn("fib daddr type local tcp dport { 515, 631 } accept", rs_custom)
        rs_none = guestnet.ruleset(997, (8080,), (8053,), printer_ports=())
        code_only = "\n".join(l for l in rs_none.splitlines() if not l.lstrip().startswith("#"))
        self.assertNotIn("515", code_only)
        self.assertNotIn("9100", code_only)

    def test_settings_from_hostd_json(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"run_as_user": "qemuuser", "webproxy_port": 9080, "airport_backend": "off"}, f)
        try:
            s = guestnet.settings_from(f.name)
        finally:
            os.unlink(f.name)
        self.assertEqual(s, {"run_as_user": "qemuuser", "webproxy_port": 9080, "webproxy_dns_port": 8053})
        self.assertEqual(guestnet.settings_from("/nonexistent/hostd.json")["run_as_user"], "rbguest")


    def test_settings_from_overlay(self):
        with tempfile.TemporaryDirectory() as td:
            etc, local = Path(td) / "hostd.json", Path(td) / "hostd.local.json"
            etc.write_text(json.dumps({"webproxy_port": 9080}))
            local.write_text(json.dumps({"webproxy_port": 9180, "webproxy_dns_port": 9053, "future_key": 1}))
            s = guestnet.settings_from(str(etc), str(local))
            self.assertEqual((s["webproxy_port"], s["webproxy_dns_port"]), (9180, 9053))
            self.assertEqual(guestnet.settings_from(str(etc))["webproxy_port"], 9080)   # explicit config only


class AdapterTests(unittest.TestCase):
    """Ethernet and AirPort both use QEMU's user network in the QEMU process, with no host
    forwarding: every guest connection is a socket of the guest user, so the table covers both."""

    def test_both_adapters_are_user_netdevs_without_forwards(self):
        import types
        from retroboot.airport import guest_nic_args
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "manifest.json"), "w") as f:
                json.dump({"features": ["airport-bridge-1"]}, f)
            paths = types.SimpleNamespace(qemu_package=d, run=d)
            eth = guest_nic_args({"network.guest": "nat", "network.guest_adapter": "ethernet"}, paths, None)
            air = guest_nic_args({"network.guest": "nat", "network.guest_adapter": "airport"}, paths, None)
        self.assertEqual(eth, ["-nic", "user,model=sungem"])
        self.assertIn("user,id=apnet", air)
        for args in (eth, air):
            joined = " ".join(args)
            for forward in ("hostfwd", "guestfwd", "restrict", "tap", "bridge"):
                self.assertNotIn(forward, joined)


class CliTests(unittest.TestCase):
    def run_cli(self, cfg: dict, action: str = "apply"):
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "calls")
            fake = os.path.join(d, "nft")
            with open(fake, "w") as f:
                f.write(f"#!{sys.executable}\nimport sys\nopen({log!r}, 'a').write(repr(sys.argv[1:]) + '\\n' + "
                        f"sys.stdin.read() + '\\n---\\n')\n")
            os.chmod(fake, stat.S_IRWXU)
            path = os.path.join(d, "hostd.json")
            with open(path, "w") as f:
                json.dump(cfg, f)
            rc = guestnet.main([action, "--config", path, "--nft", fake])
            return rc, (open(log).read() if os.path.exists(log) else "")

    def test_apply_uses_the_configured_user_and_ports(self):
        rc, calls = self.run_cli({"run_as_user": "nobody", "webproxy_port": 9080, "webproxy_dns_port": 9053})
        self.assertEqual(rc, 0)
        uid = __import__("pwd").getpwnam("nobody").pw_uid
        self.assertIn(f"meta skuid {uid} jump guest", calls)
        self.assertIn("tcp dport { 9080 } accept", calls)
        self.assertIn("th dport { 53, 9053 } accept", calls)

    def test_refuses_root_and_missing_user(self):
        rc, calls = self.run_cli({"run_as_user": "root"})
        self.assertEqual((rc, calls), (1, ""))
        rc, calls = self.run_cli({"run_as_user": "no-such-user-rb"})
        self.assertEqual((rc, calls), (1, ""))

    def test_remove(self):
        rc, calls = self.run_cli({}, "remove")
        self.assertIn("['delete', 'table', 'inet', 'retroboot_guest']", calls)


class HostdTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        helpers.make_package(self.root)
        self.app = Hostd(helpers.make_config(self.root))
        self.app.loop = asyncio.get_running_loop()
        self.app.settings.store.load()

    async def test_not_managed_in_development(self):
        self.assertIsNone(await self.app.ensure_guest_isolation())
        self.assertEqual(self.app.guest_isolation["loaded"], None)

    async def test_missing_table_is_loaded_and_failure_is_a_notice(self):
        if os.geteuid() != 0:
            self.skipTest("hostd manages the table only as root")
        self.app.cfg.allow_network_tools = True
        calls = []
        orig = (guestnet.present, guestnet.apply)
        guestnet.present = lambda *a, **k: False
        guestnet.apply = lambda *a, **k: (calls.append(a) or (False, "nft: no such file"))
        try:
            self.assertFalse(await self.app.ensure_guest_isolation())
        finally:
            guestnet.present, guestnet.apply = orig
        self.assertEqual(len(calls), 1)
        self.assertIn("guest-isolation-missing", [n["id"] for n in self.app.notices()])
        self.assertEqual((await self.app.status())["guest_isolation"]["loaded"], False)

    async def test_start_always_reapplies_launch_only_when_missing(self):
        if os.geteuid() != 0:
            self.skipTest("hostd manages the table only as root")
        self.app.cfg.allow_network_tools = True
        calls = []
        orig = (guestnet.present, guestnet.apply)
        guestnet.present = lambda *a, **k: True
        guestnet.apply = lambda *a, **k: (calls.append(a) or (True, "ok"))
        try:
            self.assertTrue(await self.app.ensure_guest_isolation(force=True))   # hostd start
            self.assertTrue(await self.app.ensure_guest_isolation())             # before a launch
        finally:
            guestnet.present, guestnet.apply = orig
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.app.guest_isolation["loaded"], True)


def _connect(host: str, port: int, uid: int | None, timeout: float = 5.0, udp: bool = False) -> str:
    """Connect from a child running as uid; returns 'ok', 'refused', 'timeout' or an errno name."""
    code = (
        "import socket,sys,errno\n"
        f"s=socket.socket(socket.AF_INET6 if ':' in {host!r} else socket.AF_INET,"
        f"socket.SOCK_DGRAM if {udp} else socket.SOCK_STREAM)\n"
        f"s.settimeout({timeout})\n"
        "try:\n"
        f"    s.connect(({host!r},{port}))\n"
        f"    if {udp}:\n"
        "        s.send(b'x')\n"
        "    print('ok')\n"
        "except socket.timeout: print('timeout')\n"
        "except ConnectionRefusedError: print('refused')\n"
        "except OSError as e: print(errno.errorcode.get(e.errno, str(e.errno)))\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=timeout + 10,
                         user=uid, group=uid if uid is not None else None, extra_groups=[] if uid is not None else None)
    return out.stdout.strip() or out.stderr.strip()[-200:]


@unittest.skipUnless(os.geteuid() == 0 and shutil.which("nft"), "needs root and nftables")
class LiveTableTests(unittest.TestCase):
    """The real ruleset in the kernel, for a throwaway uid and a throwaway table name."""

    def setUp(self):
        self.listeners = []
        self.helper_port = self._listen("127.0.0.1")
        self.hostd_port = self._listen("127.0.0.1")       # stands in for hostd's 443 / 80
        self.lan_addr = self._primary_address()
        self.lan_port = self._listen(self.lan_addr) if self.lan_addr else None
        self.dns_sock, self.dns_port = self._udp()            # the helper's DNS forwarder
        self.other_udp, self.other_udp_port = self._udp()      # any other UDP service on this computer
        rs = guestnet.ruleset(TEST_UID, (self.helper_port,), (self.dns_port,), table=TEST_TABLE)
        p = guestnet.nft(["-f", "-"], rs)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.addCleanup(lambda: guestnet.nft(["delete", "table", "inet", TEST_TABLE]))

    def _listen(self, addr: str) -> int:
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((addr, 0))
        s.listen(16)
        self.listeners.append(s)
        self.addCleanup(s.close)
        return s.getsockname()[1]

    def _udp(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        s.settimeout(2.0)
        self.addCleanup(s.close)
        return s, s.getsockname()[1]

    @staticmethod
    def _received(sock) -> bool:
        try:
            sock.recvfrom(64)
            return True
        except socket.timeout:
            return False

    @staticmethod
    def _primary_address() -> str | None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 9))          # no packet is sent: routing picks the source address
            return s.getsockname()[0]
        except OSError:
            return None
        finally:
            s.close()

    def test_guest_uid_refused_fast_on_this_computer(self):
        t0 = time.monotonic()
        self.assertEqual(_connect("127.0.0.1", self.hostd_port, TEST_UID), "refused")
        self.assertEqual(_connect("127.8.9.10", 22, TEST_UID), "refused")
        if self.lan_port:
            self.assertEqual(_connect(self.lan_addr, self.lan_port, TEST_UID), "refused")   # host's LAN address
        self.assertLess(time.monotonic() - t0, 3.0)                                          # reset, not a timeout

    def test_helper_ports_allowed_other_udp_refused(self):
        self.assertEqual(_connect("127.0.0.1", self.helper_port, TEST_UID), "ok")
        _connect("127.0.0.1", self.dns_port, TEST_UID, timeout=1.0, udp=True)
        self.assertTrue(self._received(self.dns_sock))         # DNS to the helper passes
        _connect("127.0.0.1", self.other_udp_port, TEST_UID, timeout=1.0, udp=True)
        self.assertFalse(self._received(self.other_udp))       # other UDP on this computer does not

    def test_other_users_unaffected(self):
        self.assertEqual(_connect("127.0.0.1", self.hostd_port, None), "ok")              # root: hostd, tools
        self.assertEqual(_connect("127.0.0.1", self.hostd_port, 65000), "ok")             # any other user
        if self.lan_port:
            self.assertEqual(_connect(self.lan_addr, self.lan_port, None), "ok")


if __name__ == "__main__":
    unittest.main()
