"""AirPort Prefs: AirPort 2.0.4's startup network (rbguestprep.airport)."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from rbguestprep import airport, hfsplus  # noqa: E402


class SettingsTests(unittest.TestCase):
    def test_template_is_what_airport_writes_for_a_fresh_file(self):
        # Facts recorded from AirPort 2.0.4's own file after one join (9.2.1 and 9.2.2 alike;
        # research/airport/reports/airstart-2026-10-12.md): 14,800 bytes, format 6, on.
        t = airport.template(b"RetroBoot")
        self.assertEqual(len(t), 14800)
        self.assertEqual(t[:32].hex(), "00060001000000000001ffffffff0003000a01000000000000000001ffff0000")
        self.assertEqual(t[0x36A:0x36A + 16], b"AirPort Station\0")
        self.assertEqual(t[0x38C:0x38C + 10], b"RetroBoot\0")
        self.assertEqual(sum(1 for b in t if b), 13 + 15 + 9)       # nothing else is set
        self.assertEqual(airport.parse(t), {"airport_on": True, "network": b"RetroBoot"})

    def test_with_network_keeps_the_rest_and_clears_the_old_name(self):
        t = bytearray(airport.template(b"RetroBoot Lab"))
        t[airport.OFF_AT + 1] = 1                                  # "Turn AirPort Off"
        t[0x2000] = 0x5A                                           # anything else the driver keeps
        out = airport.with_network(bytes(t), b"Cafe")
        self.assertEqual(airport.parse(out), {"airport_on": False, "network": b"Cafe"})
        self.assertEqual(out[0x38C:0x38C + airport.NETWORK_FIELD], b"Cafe" + bytes(29))
        diff = [i for i in range(len(t)) if t[i] != out[i]]
        self.assertTrue(all(0x38C <= i < 0x38C + 33 for i in diff), diff)
        self.assertEqual(out[0x2000], 0x5A)

    def test_names(self):
        airport.with_network(airport.template(b"x"), b"N" * 32)
        for bad in (b"", b"N" * 33, b"a\0b"):
            with self.assertRaises(ValueError):
                airport.template(bad)
        self.assertEqual(airport.parse(airport.template("Café".encode("mac_roman")))["network"],
                         "Café".encode("mac_roman"))

    def test_unknown_layouts_are_refused(self):
        t = airport.template(b"RetroBoot")
        for bad in (t[:-1], b"\x00\x05" + t[2:], t[:4] + b"\x00\x07" + t[6:]):
            with self.assertRaises(airport.LayoutError):
                airport.parse(bad)
            with self.assertRaises(airport.LayoutError):
                airport.with_network(bad, b"Home")

    def test_resource_round_trip(self):
        fork = hfsplus.build_resource_fork([("ftag", 0, "", b"\x02\x01"), (airport.RES_TYPE, airport.RES_ID, "",
                                                                          airport.template(b"Home"))])
        res = hfsplus.parse_resource_fork(fork)
        entry = next(r for r in res if r[0] == "1ap0")
        self.assertEqual((entry[1], len(entry[4])), (150, 14800))
        self.assertEqual(fork[entry[3] + airport.NETWORK_AT:entry[3] + airport.NETWORK_AT + 5], b"Home\0")


@unittest.skipUnless(os.environ.get("RB_AIRPORT_IMAGE") and os.geteuid() == 0,
                     "needs RB_AIRPORT_IMAGE (a prepared disk with AirPort 2.0.4 and no AirPort Prefs) and root")
class RealDiskTest(unittest.TestCase):
    """Create, change in place, keep "off": on a sparse copy (root, hfsplus module, fsck.hfsplus)."""

    def test_create_set_unchanged(self):
        src = os.environ["RB_AIRPORT_IMAGE"]
        tmp = tempfile.mkdtemp(dir=os.path.dirname(src))
        try:
            img = os.path.join(tmp, "disk.img")
            subprocess.run(["cp", "--sparse=always", src, img], check=True)
            self.assertEqual(airport.state(img)["prefs"], "missing")
            self.assertEqual(airport.set_network(img, b"RetroBoot"), "missing")         # no allocation in place
            self.assertEqual(airport.set_network(img, b"RetroBoot", create=True), "created")
            self.assertEqual(airport.state(img), {"prefs": "present", "airport_on": True, "network": "RetroBoot"})
            self.assertEqual(airport.set_network(img, b"RetroBoot"), "unchanged")
            self.assertEqual(airport.set_network(img, b"Home Network"), "set")
            self.assertEqual(airport.state(img)["network"], "Home Network")
            with hfsplus.Volume(img) as vol:
                folder, rec = airport._locate(vol)
                self.assertEqual(rec.type_creator, airport.TYPE_CREATOR)
                _res, entry = airport._settings(vol, rec)
                self.assertEqual(entry[4], airport.template(b"Home Network"))
            from rbguestprep import prep
            ok, out = prep.fsck(img, repair=False)
            self.assertTrue(ok, out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
