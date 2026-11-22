import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from rbguestprep import hfsplus, prep  # noqa: E402


class ResourceForkTests(unittest.TestCase):
    def test_round_trip_tcpip_template(self):
        fork = hfsplus.build_resource_fork(prep.TCPIP_DHCP_RESOURCES)
        parsed = hfsplus.parse_resource_fork(fork)
        self.assertEqual([(t, i, n, d) for t, i, n, _pos, d in parsed], prep.TCPIP_DHCP_RESOURCES)
        for _t, _i, _n, pos, data in parsed:
            self.assertEqual(fork[pos:pos + len(data)], data)

    def test_template_matches_control_panel_output_shape(self):
        res = {(t, i): d for t, i, _n, d in prep.TCPIP_DHCP_RESOURCES}
        self.assertEqual(len(res[("iitf", 128)]), 85)
        self.assertEqual(res[("port", 128)], b"\x08Ethernet")
        self.assertEqual(res[("dtyp", 128)], b"\x00\x0a")  # DHCP
        self.assertNotIn(("dclt", 128), res)  # no stale lease

    def test_airport_template_differs_only_in_port_and_module(self):
        eth = {(t, i): d for t, i, _n, d in prep.TCPIP_ETHERNET_DHCP_RESOURCES}
        air = {(t, i): d for t, i, _n, d in prep.TCPIP_AIRPORT_DHCP_RESOURCES}
        self.assertEqual(set(eth), set(air))
        diff = {k for k in eth if eth[k] != air[k]}
        self.assertEqual(diff, {("iitf", 128), ("port", 128)})
        self.assertEqual(air[("port", 128)], b"\x07AirPort")
        self.assertEqual(len(air[("iitf", 128)]), 85)
        self.assertIn(b"\x05enet0", air[("iitf", 128)])
        self.assertIn(b"\x05radio", air[("iitf", 128)])
        self.assertIn(b"\x04gmac", eth[("iitf", 128)])

    def test_classification_is_the_ownership_rule(self):
        def fork(resources):
            return hfsplus.build_resource_fork(resources)
        self.assertEqual(prep.classify_tcpip(fork(prep.TCPIP_ETHERNET_DHCP_RESOURCES)), "ethernet")
        self.assertEqual(prep.classify_tcpip(fork(prep.TCPIP_AIRPORT_DHCP_RESOURCES)), "airport")
        # the guest's own lease cache, window position and version stamps do not change ownership
        lived = prep.TCPIP_AIRPORT_DHCP_RESOURCES + [("dclt", 128, "", bytes(16)), ("vers", 2, "", b"\x01\x00")]
        lived = [(t, i, n, b"\x00\x50\x00\x99" if t == "wpos" else d) for t, i, n, d in lived]
        self.assertEqual(prep.classify_tcpip(fork(lived)), "airport")
        # a manual address, a second configuration or another port is the user's
        manual = [(t, i, n, b"\x00\x00" if t == "dtyp" else d) for t, i, n, d in prep.TCPIP_ETHERNET_DHCP_RESOURCES]
        self.assertEqual(prep.classify_tcpip(fork(manual)), "user")
        second = prep.TCPIP_ETHERNET_DHCP_RESOURCES + [("cnam", 129, "Office", b"")]
        self.assertEqual(prep.classify_tcpip(fork(second)), "user")
        self.assertEqual(prep.classify_tcpip(b"garbage" * 4), "user")

    def test_appletalk_template_round_trip_and_ownership(self):
        from rbguestprep import appletalk
        fork = hfsplus.build_resource_fork(appletalk.APPLETALK_INACTIVE_RESOURCES)
        parsed = hfsplus.parse_resource_fork(fork)
        self.assertEqual([(t, i, n, d) for t, i, n, _pos, d in parsed], appletalk.APPLETALK_INACTIVE_RESOURCES)
        self.assertEqual(appletalk.classify(fork), "retroboot-inactive")
        res = {(t, i): d for t, i, _n, d in appletalk.APPLETALK_INACTIVE_RESOURCES}
        self.assertEqual(len(res[("atpf", 128)]), 274)       # the panel's own 'atpf'
        self.assertEqual(res[("port", 128)], b"\x08Ethernet")
        self.assertEqual(res[("ulvl", 1)], b"\x00\x01")      # Basic, as on a fresh install
        # the window position does not change ownership
        moved = [(t, i, n, b"\x00\x40\x00\x40" if t == "wpos" else d)
                 for t, i, n, d in appletalk.APPLETALK_INACTIVE_RESOURCES]
        self.assertEqual(appletalk.classify(hfsplus.build_resource_fork(moved)), "retroboot-inactive")
        # the panel rewrites 'atpf' whenever the user saves (e.g. AppleTalk active again): user's
        active = [(t, i, n, d[:92] + bytes.fromhex("05000000") + d[96:] if t == "atpf" else d)
                  for t, i, n, d in appletalk.APPLETALK_INACTIVE_RESOURCES]
        self.assertEqual(appletalk.classify(hfsplus.build_resource_fork(active)), "user")
        advanced = [(t, i, n, b"\x00\x02" if t == "ulvl" else d) for t, i, n, d in appletalk.APPLETALK_INACTIVE_RESOURCES]
        self.assertEqual(appletalk.classify(hfsplus.build_resource_fork(advanced)), "user")
        self.assertEqual(appletalk.classify(b"garbage" * 4), "user")

    def test_portable_energy_saver_never_is_what_the_panel_writes(self):
        # 'wnkl' -360 after the 9.2.1 panel set Battery and Power Adapter to Never
        # on the PMU machine (research/battery); bytes 200.. are its window state.
        panel = bytes.fromhex(
            "00000004000000000000000000000000000000000000000000000000000000000040004000400005000000010100000000010000"
            "010100000000000000400040004000010000000101000000000100000100000100010000000c00080004000101010101010000"
            "0000010000010000010001000000000000000001c2000001c2000001c2000001c2000001c2000001c2000001c2000001c2001f"
            "00000000041a0000041a0000041a0000041a0000041a0000041a0000041a0000041a001f00000000000000000000006800c00001")
        new, before = prep.portable_never(prep.PORTABLE_ENERGY_DEFAULTS)
        self.assertEqual(new[:200], panel[:200])
        self.assertEqual(before, [[60, 20, 32], [12, 8, 4]])   # 15/5/8 and 3/2/1 minutes
        again, before2 = prep.portable_never(new)
        self.assertEqual(again, new)
        self.assertEqual(before2, [[0x40] * 3, [0x40] * 3])

    def test_portable_energy_saver_rejects_other_layouts(self):
        self.assertIsNone(prep.portable_never(prep.PORTABLE_ENERGY_DEFAULTS[:-2]))
        bad = bytearray(prep.PORTABLE_ENERGY_DEFAULTS)
        bad[60] = 0x12          # a timer beyond the panel's range
        self.assertIsNone(prep.portable_never(bytes(bad)))

    def test_fsck_verify_repairs_only_unerased_nodes(self):
        from unittest import mock
        corrupt = ("** /dev/loop0 (NO WRITE)\n   Executing fsck_hfs (version 540.1-Linux).\n"
                   "   The volume name is Macintosh HD\n"
                   "** Checking catalog file.\n   Unused node is not erased (node = 254)\n"
                   "** The volume Macintosh HD was found corrupt and needs to be repaired.")
        good = "** The volume Macintosh HD appears to be OK."
        calls = []

        def fake(image, repair):
            calls.append(repair)
            if repair:
                return True, "repaired"
            return (False, corrupt) if calls.count(False) == 1 else (True, good)
        with mock.patch.object(prep, "fsck", fake):
            self.assertEqual(prep.fsck_verify("x.img"), (True, good, True))
        self.assertEqual(calls, [False, True, False])
        real = "** Checking catalog file.\n   Invalid leaf record count\n" + corrupt
        calls.clear()
        with mock.patch.object(prep, "fsck", lambda image, repair: (calls.append(repair), (False, real))[1]):
            self.assertEqual(prep.fsck_verify("x.img")[:2], (False, real))
        self.assertEqual(calls, [False])   # a real error is never "repaired" behind the caller's back

    def test_plug_plan_fills_small_runs_in_front_of_a_large_one(self):
        from rbguestprep import install
        runs = [(10, 3), (20, 5), (40, 1)] * 3 + [(500, 4), (900, 1270), (3000, 1), (5000, 100000)]
        plan = install.plug_plan(runs, largest_fork_blocks=330)
        self.assertEqual([len(p) for p in plan], [8, 2])           # ten small runs, at most eight per plug
        self.assertEqual(plan[-1][-1], (500, 4))                   # stops before the 1270-block run
        self.assertEqual(install.plug_plan([(0, 300), (400, 2)], 100), [])   # first run already large
        self.assertEqual(install.plug_plan([(0, 3), (9, 4)], 100), [])       # nothing large: no plugs
        big = install.plug_plan([(0, 300), (400, 2000)], 4000)    # a 4000-block fork wants >= 1000-block runs
        self.assertEqual(big, [[(0, 300)]])

    def test_apply_rejects_unknown_port(self):
        with self.assertRaises(prep.PrepError):
            prep.apply("/nonexistent.img", network_port="token-ring")

    def test_rejects_bad_type(self):
        with self.assertRaises(ValueError):
            hfsplus.build_resource_fork([("abc", 1, "", b"")])


class LocateVolumeTests(unittest.TestCase):
    def _apm_image(self, embedded: bool) -> str:
        fd, path = tempfile.mkstemp()
        self.addCleanup(os.unlink, path)
        with os.fdopen(fd, "r+b") as f:
            f.truncate(4 << 20)
            f.write(b"ER" + struct.pack(">H", 512))
            def entry(i, start, count, ptype):
                f.seek(512 * i)
                e = bytearray(512)
                e[0:2] = b"PM"
                e[4:8] = struct.pack(">I", 2)
                e[8:16] = struct.pack(">II", start, count)
                e[48:48 + len(ptype)] = ptype
                f.write(bytes(e))
            entry(1, 1, 63, b"Apple_partition_map")
            entry(2, 64, 8000, b"Apple_HFS")
            f.seek(64 * 512 + 1024)
            if embedded:
                mdb = bytearray(512)
                mdb[0:2] = b"BD"
                mdb[0x14:0x18] = struct.pack(">I", 4096)
                mdb[0x1C:0x1E] = struct.pack(">H", 6)
                mdb[0x7C:0x7E] = b"H+"
                mdb[0x7E:0x82] = struct.pack(">HH", 2, 900)
                f.write(bytes(mdb))
            else:
                f.write(b"H+")
        return path

    def test_embedded(self):
        off, length = hfsplus.locate_volume(self._apm_image(True))
        self.assertEqual(off, 64 * 512 + 6 * 512 + 2 * 4096)
        self.assertEqual(length, 900 * 4096)

    def test_plain(self):
        self.assertEqual(hfsplus.locate_volume(self._apm_image(False)), (64 * 512, 8000 * 512))


@unittest.skipUnless(os.environ.get("RB_FIXTURE_IMAGE"), "set RB_FIXTURE_IMAGE to a disposable fixture copy")
class FixtureTests(unittest.TestCase):
    def test_read_catalog(self):
        with hfsplus.Volume(os.environ["RB_FIXTURE_IMAGE"]) as vol:
            self.assertEqual(prep._system_folder(vol), ["System Folder"])
            rec = vol.lookup(["System Folder", "Preferences", "General Controls Prefs"])
            names = [t for t, *_ in hfsplus.parse_resource_fork(vol.read_fork(rec, "rsrc"))]
            self.assertIn("Smrt", names)


if __name__ == "__main__":
    unittest.main()
