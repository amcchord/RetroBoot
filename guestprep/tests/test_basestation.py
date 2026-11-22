"""AirPort Software Base Station step (basestation.py): the decision table offline;
the real move through RB_BASESTATION_IMAGE (a scratch copy; root, hfsplus module).

    python3 -m unittest tests/test_basestation.py
    RB_BASESTATION_IMAGE=/scratch/copy.img python3 -m unittest tests/test_basestation.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from rbguestprep import basestation, prep  # noqa: E402


class PlanTests(unittest.TestCase):
    def test_ethernet_disables_an_enabled_copy(self):
        self.assertEqual(basestation.plan("enabled", "ethernet"), "disable")

    def test_airport_restores_a_disabled_copy(self):
        self.assertEqual(basestation.plan("disabled", "airport"), "enable")

    def test_nothing_else_moves(self):
        for current in ("disabled", "absent", "other", "both"):
            self.assertIsNone(basestation.plan(current, "ethernet"), current)
        for current in ("enabled", "absent", "other", "both"):
            self.assertIsNone(basestation.plan(current, "airport"), current)

    def test_apply_signature(self):
        self.assertGreaterEqual(prep.PREP_VERSION, 5)


@unittest.skipUnless(os.environ.get("RB_BASESTATION_IMAGE"), "set RB_BASESTATION_IMAGE to a scratch copy")
class ImageTests(unittest.TestCase):
    def test_round_trip(self):
        image = os.environ["RB_BASESTATION_IMAGE"]
        self.assertEqual(basestation.state(image), "enabled")
        self.assertEqual(basestation.apply(image, "ethernet"), "disabled")
        self.assertEqual(basestation.state(image), "disabled")
        self.assertEqual(basestation.apply(image, "ethernet"), "already-disabled")
        self.assertTrue(prep.fsck_verify(image)[0])
        self.assertEqual(basestation.apply(image, "airport"), "enabled")
        self.assertEqual(basestation.state(image), "enabled")
        self.assertEqual(basestation.apply(image, "airport"), "enabled-kept")
        self.assertTrue(prep.fsck_verify(image)[0])


if __name__ == "__main__":
    unittest.main()
