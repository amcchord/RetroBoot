"""Mac OS's time zone from system.timezone (retroboot/macclock.py)."""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import helpers
from retroboot import macclock, schema
from retroboot.config import Paths
from retroboot.machines import MachineRegistry, build_argv

SUMMER = datetime(2026, 10, 12, 9, 43, tzinfo=timezone.utc)
WINTER = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)

TAB = (
    "# test zone1970.tab\n"
    "US\t+404251-0740023\tAmerica/New_York\tEastern (most areas)\n"
    "IN\t+2232+08822\tAsia/Kolkata\n"
    "AU\t-3352+15113\tAustralia/Sydney\tNew South Wales (most areas)\n"
    "US\t+332654-1120424\tAmerica/Phoenix\tMST - AZ (most areas)\n"
)


def signed(v: int) -> int:
    return v - (1 << 32) if v & 0x80000000 else v


class MacLocationTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.root = Path(self.td.name)
        (self.root / "zone1970.tab").write_text(TAB)

    def loc(self, zone, when):
        return macclock.mac_location(zone, when, self.root)

    def test_gmt_delta_matches_mac_os_date_and_time(self):
        # What Mac OS 9.2.1's Date & Time itself writes for New York (verified in the guest)
        self.assertEqual(self.loc("America/New_York", WINTER)[2], 0x00FFB9B0)   # -5 h, no DST
        self.assertEqual(self.loc("America/New_York", SUMMER)[2], 0x80FFC7C0)   # -4 h, DST flag

    def test_offsets_without_daylight_saving(self):
        self.assertEqual(self.loc("Asia/Kolkata", SUMMER)[2], 19800)            # +5:30
        self.assertEqual(self.loc("America/Phoenix", SUMMER)[2], 0x00FF9D90)    # -7 h all year
        self.assertEqual(self.loc("America/Phoenix", WINTER)[2], 0x00FF9D90)

    def test_southern_hemisphere(self):
        self.assertEqual(self.loc("Australia/Sydney", SUMMER)[2], 0x80000000 | 39600)  # AEDT
        self.assertEqual(self.loc("Australia/Sydney", datetime(2026, 7, 1, tzinfo=timezone.utc))[2], 36000)

    def test_coordinates_are_fracts_of_90_degrees(self):
        lat, lon, _ = self.loc("America/New_York", SUMMER)
        self.assertAlmostEqual(signed(lat) / (1 << 30) * 90, 40.7142, places=3)
        self.assertAlmostEqual(signed(lon) / (1 << 30) * 90, -74.0064, places=3)
        lat, lon, _ = self.loc("Australia/Sydney", SUMMER)
        self.assertAlmostEqual(signed(lat) / (1 << 30) * 90, -33.8667, places=3)

    def test_zone_without_a_city_is_never_all_zero(self):
        lat, lon, delta = self.loc("UTC", SUMMER)
        self.assertEqual(delta, 0)
        self.assertNotEqual((lat, lon), (0, 0))       # Greenwich: "not set" would be all zeros
        lat, lon, delta = self.loc("Etc/GMT+5", SUMMER)
        self.assertEqual(delta, 0x00FFB9B0)
        self.assertAlmostEqual(signed(lon) / (1 << 30) * 90, -75.0, places=3)

    def test_prom_env_value(self):
        # New York on October 12: EDT now, EST from Sunday November 1 2026 06:00 UTC, when the
        # Mac clock reads 02:00 (EDT) and goes back to 01:00
        when = datetime(2026, 11, 1, 6, tzinfo=timezone.utc)
        mac_when = int(when.timestamp()) + macclock.MAC_EPOCH - 4 * 3600
        self.assertEqual(macclock.prom_env("America/New_York", SUMMER, self.root),
                         "retroboot-location=1cf3c9b1,cb5f8c4d,80ffc7c0,%08x,00ffb9b0" % mac_when)
        self.assertIsNone(macclock.prom_env("Not/AZone", SUMMER, self.root))

    def test_next_change(self):
        when, off, dst = macclock.next_change("America/New_York", SUMMER)
        self.assertEqual((when, off, dst), (datetime(2026, 11, 1, 6, tzinfo=timezone.utc), -18000, False))
        when, off, dst = macclock.next_change("America/New_York", WINTER)
        self.assertEqual((when, off, dst), (datetime(2026, 3, 8, 7, tzinfo=timezone.utc), -14400, True))
        when, off, dst = macclock.next_change("Australia/Sydney", SUMMER)        # AEDT ends in April
        self.assertEqual((when, off, dst), (datetime(2027, 4, 3, 16, tzinfo=timezone.utc), 36000, False))
        self.assertIsNone(macclock.next_change("Asia/Kolkata", SUMMER))         # no change in 400 days
        self.assertIsNone(macclock.next_change("UTC", SUMMER))

    def test_zones_without_changes_pass_three_fields(self):
        self.assertEqual(macclock.prom_env("Asia/Kolkata", SUMMER, self.root).count(","), 2)

    def test_zone_tab_format(self):
        self.assertAlmostEqual(macclock._dms("+404251"), 40.714166, places=5)
        self.assertAlmostEqual(macclock._dms("-0740023"), -74.006388, places=5)
        self.assertAlmostEqual(macclock._dms("+2232"), 22.533333, places=5)


def city(name, lat, lon, std_h, rules=()):
    """A synthetic Date & Time city (made-up names; the guest's own list is Apple's)."""
    return {"name": name, "country": "", "latitude": macclock.fract(lat), "longitude": macclock.fract(lon),
            "std_offset": int(std_h * 3600), "region": 0, "flags": 0,
            "rules": [{"from": "", "month": m, "week": 255, "weekday": 1, "dst": d, "at": 7200,
                       "save": 3600 if d else 0} for m, d in rules]}


NORTH_OLD = ((4, True), (10, False))      # an old-style northern rule: April to October
EU = ((3, True), (10, False))
CITIES = [
    city("Alpha", 40.7, -74.0, -5, NORTH_OLD), city("Alpha South", 25.8, -80.2, -5, NORTH_OLD),
    city("Beta", -0.2, -78.5, -5), city("Gamma", 51.5, -0.2, 0, EU), city("Delta", 64.1, -21.9, 0),
    city("Epsilon", 40.8, -111.9, -7, NORTH_OLD), city("Epsilon Plain", 33.3, -112.0, -7),
    city("Zeta", 22.6, 88.4, 5.5), city("Eta", -21.1, -175.2, 13),
]


class CityChoiceTests(unittest.TestCase):
    """Time Synchronizer accepts only a location at one of Date & Time's cities."""

    def choose(self, zone, when=SUMMER):
        return macclock.choose_city(CITIES, zone, when)["name"]

    def test_same_offset_and_rule_nearest_to_the_zone(self):
        self.assertEqual(self.choose("America/New_York"), "Alpha")          # not Alpha South (farther)
        self.assertEqual(self.choose("America/Boise"), "Epsilon")           # Mountain with DST
        self.assertEqual(self.choose("America/Phoenix"), "Epsilon Plain")   # Mountain without DST
        self.assertEqual(self.choose("Europe/London"), "Gamma")
        self.assertEqual(self.choose("Asia/Kolkata"), "Zeta")

    def test_zones_without_a_city(self):
        self.assertEqual(self.choose("UTC"), "Delta")                       # offset 0, no DST: not Gamma
        self.assertEqual(self.choose("Etc/GMT+5"), "Beta")                  # -5, no DST: not Alpha
        self.assertEqual(self.choose("Etc/GMT-14"), "Eta")                  # nearest offset

    def test_location_uses_the_city_and_the_zones_offset(self):
        lat, lon, delta = macclock.mac_location("UTC", SUMMER, cities=CITIES)
        self.assertEqual((lat, lon, delta), (macclock.fract(64.1), macclock.fract(-21.9), 0))
        value = macclock.prom_env("America/New_York", SUMMER, cities=CITIES)
        self.assertTrue(value.startswith("retroboot-location=%08x,%08x,80ffc7c0," %
                                         (macclock.fract(40.7), macclock.fract(-74.0))))

    def test_no_list_keeps_tzdata_coordinates(self):
        self.assertEqual(macclock.mac_location("Asia/Kolkata", SUMMER, cities=None),
                         macclock.mac_location("Asia/Kolkata", SUMMER))
        self.assertIsNone(macclock.choose_city([], "UTC", SUMMER))


class ArgvTests(unittest.TestCase):
    def setUp(self):
        self.td, self.root = helpers.scratch()
        self.addCleanup(self.td.cleanup)
        helpers.make_package(self.root)
        helpers.make_machine(self.root)
        self.paths = Paths(self.root)
        self.paths.ensure()
        self.m = MachineRegistry(self.paths).get("mac-os-9")

    def test_argv_uses_the_guests_cities(self):
        from retroboot.machines import LaunchOptions
        settings = dict(schema.defaults(), **{"system.timezone": "UTC"})
        argv = build_argv(self.m, settings, self.paths, None, LaunchOptions(mac_cities=CITIES))
        loc = [argv[i + 1] for i, v in enumerate(argv) if v == "-prom-env" and argv[i + 1].startswith("retroboot-")]
        self.assertEqual(loc, ["retroboot-location=%08x,%08x,00000000" % (macclock.fract(64.1), macclock.fract(-21.9))])

    def test_argv_carries_the_configured_zone(self):
        settings = dict(schema.defaults(), **{"system.timezone": "Asia/Kolkata"})
        argv = build_argv(self.m, settings, self.paths, None)
        values = [argv[i + 1] for i, v in enumerate(argv) if v == "-prom-env"]
        loc = [v for v in values if v.startswith("retroboot-location=")]
        self.assertEqual(len(loc), 1)
        self.assertTrue(loc[0].endswith(",00004d58"))
        self.assertIn("base=localtime", argv)


if __name__ == "__main__":
    unittest.main()
