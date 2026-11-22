"""Date & Time's city list (rbguestprep.macloc) on a synthetic loc# resource."""
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from rbguestprep import macloc  # noqa: E402


def pstr(s: str) -> bytes:
    b = s.encode("mac_roman")
    out = bytes([len(b)]) + b
    return out + (b"\0" if len(out) & 1 else b"")


def entry(name, country, lat, lon, delta, region=0, flags=0, rules=()) -> bytes:
    body = struct.pack(">iiIBB", lat, lon, delta, region, flags) + pstr(name) + pstr(country)
    body += struct.pack(">H", len(rules))
    for frm, month, week, wday, dst, at, save in rules:
        body += (frm.encode("ascii") if frm else b"\0\0\0\0") + bytes([month, week, wday, 0x80 if dst else 0])
        body += struct.pack(">HH", at, save)
    return struct.pack(">H", 2 + len(body)) + body


class LocListTests(unittest.TestCase):
    def test_parse(self):
        blob = struct.pack(">H", 3) + entry("Northtown", "Utopia", 0x1CF43E33, -0x34A252AE, 0x00FFB9B0, 0, 1,
                                            [("0401", 4, 1, 1, True, 7200, 3600), ("", 10, 0xFF, 1, False, 7200, 0)]) \
            + entry("Equator", "", 0x0345678A, -0x34AE7592, 0x00FFB9B0) \
            + entry("Odd", "Six", 0x10000000, 0x20000000, 0x00004D58, 7, 0)
        cities = macloc.parse(blob)
        self.assertEqual([c["name"] for c in cities], ["Northtown", "Equator", "Odd"])
        n = cities[0]
        self.assertEqual((n["latitude"], n["longitude"], n["std_offset"]), (0x1CF43E33, -0x34A252AE, -18000))
        self.assertEqual(n["country"], "Utopia")
        self.assertEqual(n["rules"][0], {"from": "0401", "month": 4, "week": 1, "weekday": 1, "dst": True,
                                         "at": 7200, "save": 3600})
        self.assertEqual(n["rules"][1]["week"], 0xFF)
        self.assertFalse(n["rules"][1]["dst"])
        self.assertEqual(cities[1]["rules"], [])
        self.assertEqual(cities[2]["std_offset"], 19800)
        self.assertEqual(cities[2]["region"], 7)

    def test_truncated_list_stops_cleanly(self):
        blob = struct.pack(">H", 2) + entry("Only", "", 0, 0, 0)
        self.assertEqual([c["name"] for c in macloc.parse(blob)], ["Only"])
        self.assertEqual(macloc.parse(b""), [])

    @unittest.skipUnless(os.environ.get("RB_FIXTURE_IMAGE"), "RB_FIXTURE_IMAGE not set")
    def test_fixture_has_a_city_list(self):
        cities = macloc.read_cities(os.environ["RB_FIXTURE_IMAGE"])
        self.assertGreater(len(cities), 100)
        self.assertTrue(any(c["rules"] for c in cities) and any(not c["rules"] for c in cities))


if __name__ == "__main__":
    unittest.main()
