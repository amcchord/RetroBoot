"""Control Strip Preferences: the strip's place across Mac screen changes (rbguestprep.controlstrip)."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from rbguestprep import controlstrip as cs, hfsplus  # noqa: E402

# The first 32 bytes of 'pref' 3114 as the Control Strip of Mac OS 9.2.2 wrote it on RetroBoot's emulated
# machine (hostd/reports/cardswitch-2026-10-13.md); the rest of the 920 bytes (the module list) is not used.
STD_BOTTOM_LEFT = bytes.fromhex("0203 8000 0003 01c8 00000014 0000 0000 02d0 0438 02b9 0001 0001 000a 1100 0100")
ATI_BOTTOM_LEFT = bytes.fromhex("0203 8000 0003 01c8 00000014 0000 0000 0270 0340 0259 0001 0001 000a 1100 0100")
STD_RIGHT_MIDDLE = bytes.fromhex("0203 8000 0002 01c8 00000014 0000 0000 02d0 0438 0184 0437 0001 000a 1100 0100")
ATI_RIGHT_COLLAPSED = bytes.fromhex("0203 8000 0002 0010 00000014 0000 0000 0270 0340 0259 033f 0001 000a 1100 0100")
MODULES = bytes(range(256)) * 3 + bytes(120)          # stands in for the module list, never changed


def pref(head: bytes) -> bytes:
    return head + MODULES


class LayoutTests(unittest.TestCase):
    def test_recorded_files(self):
        self.assertEqual(len(STD_BOTTOM_LEFT), 32)
        self.assertEqual(cs.parse(pref(STD_BOTTOM_LEFT)),
                         {"screen": (1080, 720), "top": 697, "edge": 1, "side": "left"})
        self.assertEqual(cs.parse(pref(ATI_BOTTOM_LEFT)),
                         {"screen": (832, 624), "top": 601, "edge": 1, "side": "left"})
        self.assertEqual(cs.parse(pref(STD_RIGHT_MIDDLE)),
                         {"screen": (1080, 720), "top": 388, "edge": 1079, "side": "right"})
        self.assertEqual(cs.parse(pref(ATI_RIGHT_COLLAPSED)),
                         {"screen": (832, 624), "top": 601, "edge": 831, "side": "right"})

    def test_unknown_layouts_are_refused(self):
        good = pref(STD_BOTTOM_LEFT)
        for bad in (good[:cs.MIN_LEN - 1], b"\x02\x04" + good[2:],
                    good[:cs.SCREEN_AT] + b"\x00\x16" + good[cs.SCREEN_AT + 2:],     # a screen not at 0,0
                    good[:cs.SCREEN_AT + 4] + b"\x00\x10" + good[cs.SCREEN_AT + 6:]):  # a 16-pixel screen
            with self.assertRaises(cs.LayoutError):
                cs.parse(bad)
            with self.assertRaises(cs.LayoutError):
                cs.fitted(bad, (1080, 720))


class FitTests(unittest.TestCase):
    def fit(self, head: bytes, size) -> dict:
        data = pref(head)
        out = cs.fitted(data, size)
        self.assertIsNotNone(out)
        self.assertEqual(len(out), len(data))
        diff = [i for i in range(len(data)) if data[i] != out[i]]
        self.assertTrue(all(cs.SCREEN_AT <= i < cs.MIN_LEN for i in diff), diff)    # 12 bytes at most
        return cs.parse(out)

    def test_same_screen_is_left_alone(self):
        self.assertIsNone(cs.fitted(pref(STD_BOTTOM_LEFT), (1080, 720)))
        self.assertIsNone(cs.fitted(pref(ATI_RIGHT_COLLAPSED), (832, 624)))

    def test_3d_off_puts_the_strip_back_on_the_bottom_edge(self):
        # fleet5/fleet6: after 832 x 624 (ATI) Mac OS started at 1080 x 720 with the strip 96 px up.
        out = cs.fitted(pref(ATI_BOTTOM_LEFT), (1080, 720))
        self.assertEqual(out[:32], STD_BOTTOM_LEFT)                 # exactly what Mac OS wrote at 1080 x 720

    def test_3d_on_is_what_mac_os_makes_of_it(self):
        out = cs.fitted(pref(STD_BOTTOM_LEFT), (832, 624))
        self.assertEqual(out[:32], ATI_BOTTOM_LEFT)

    def test_right_edge_side_and_collapsed_state_are_kept(self):
        out = cs.fitted(pref(ATI_RIGHT_COLLAPSED), (1080, 720))
        self.assertEqual(cs.parse(out), {"screen": (1080, 720), "top": 697, "edge": 1079, "side": "right"})
        self.assertEqual(out[4:8], ATI_RIGHT_COLLAPSED[4:8])        # right edge, collapsed width 16

    def test_a_strip_elsewhere_keeps_its_share_of_the_height(self):
        # Mac OS on a live 1080 x 720 -> 832 x 624 switch: a left strip at 601 went to 521, a right
        # strip at 388 to 338 (lab, Display Manager switch through the video driver).
        left = STD_BOTTOM_LEFT[:cs.WHERE_AT] + (601).to_bytes(2, "big") + STD_BOTTOM_LEFT[cs.WHERE_AT + 2:]
        self.assertEqual(self.fit(left, (832, 624))["top"], 521)
        right = self.fit(STD_RIGHT_MIDDLE, (832, 624))
        self.assertEqual((right["top"], right["edge"]), (338, 831))
        back = cs.fitted(cs.fitted(pref(STD_RIGHT_MIDDLE), (832, 624)), (1080, 720))
        self.assertAlmostEqual(cs.parse(back)["top"], 388, delta=1)      # Mac OS: 388 again

    def test_pixel_size_and_portrait(self):
        # 1920 x 1080 at 2x (960 x 540), and back
        small = self.fit(STD_BOTTOM_LEFT, (960, 540))
        self.assertEqual((small["top"], small["edge"]), (517, 1))
        big = cs.fitted(cs.fitted(pref(STD_RIGHT_MIDDLE), (960, 540)), (1920, 1080))
        self.assertEqual(cs.parse(big)["edge"], 1919)
        tall = self.fit(STD_BOTTOM_LEFT, (720, 1080))                # rotated
        self.assertEqual(tall["top"], 1057)

    def test_near_the_bottom_keeps_its_gap_and_never_leaves_the_screen(self):
        near = STD_BOTTOM_LEFT[:cs.WHERE_AT] + (687).to_bytes(2, "big") + STD_BOTTOM_LEFT[cs.WHERE_AT + 2:]
        self.assertEqual(self.fit(near, (832, 624))["top"], 591)    # 10 px above the bottom, as before
        below = STD_BOTTOM_LEFT[:cs.WHERE_AT] + (900).to_bytes(2, "big") + STD_BOTTOM_LEFT[cs.WHERE_AT + 2:]
        self.assertEqual(self.fit(below, (832, 624))["top"], 601)
        high = STD_BOTTOM_LEFT[:cs.WHERE_AT] + (0).to_bytes(2, "big") + STD_BOTTOM_LEFT[cs.WHERE_AT + 2:]
        self.assertEqual(self.fit(high, (832, 624))["top"], cs.MENU_BAR)

    def test_bad_sizes(self):
        for size in ((0, 0), (100, 720), (1080, 9000)):
            with self.assertRaises(ValueError):
                cs.fitted(pref(STD_BOTTOM_LEFT), size)

    def test_resource_round_trip(self):
        fork = hfsplus.build_resource_fork([("STR ", -16397, "", b"\x05Hello"),
                                            ("pref", 3114, "Control Strip Preferences", pref(ATI_BOTTOM_LEFT))])
        entry = next(r for r in hfsplus.parse_resource_fork(fork) if r[0] == "pref")
        self.assertEqual(fork[entry[3] + cs.WHERE_AT:entry[3] + cs.WHERE_AT + 2], (601).to_bytes(2, "big"))


@unittest.skipUnless(os.environ.get("RB_CONTROLSTRIP_IMAGE"),
                     "needs RB_CONTROLSTRIP_IMAGE (a disk whose Mac OS has saved Control Strip Preferences)")
class RealDiskTest(unittest.TestCase):
    """fit on a sparse copy: moved, then unchanged, and the volume still checks (fsck when root)."""

    def test_fit_round_trip(self):
        src = os.environ["RB_CONTROLSTRIP_IMAGE"]
        tmp = tempfile.mkdtemp(dir=os.path.dirname(src))
        try:
            img = os.path.join(tmp, "disk.img")
            subprocess.run(["cp", "--sparse=always", src, img], check=True)
            st = cs.state(img)
            self.assertEqual(st["prefs"], "present")
            w, h = st["strips"][0]["screen"]
            other = (832, 624) if (w, h) != (832, 624) else (1080, 720)
            res = cs.fit(img, other)
            self.assertEqual(res["result"], "moved")
            self.assertEqual(res["to"][:2], list(other))
            self.assertEqual(cs.fit(img, other), {"result": "unchanged"})
            self.assertEqual(cs.state(img)["strips"][0]["screen"], list(other))
            if os.geteuid() == 0 and shutil.which("fsck.hfsplus"):
                from rbguestprep import prep
                ok, out, _ = prep.fsck_verify(img)
                self.assertTrue(ok, out[-300:])
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
