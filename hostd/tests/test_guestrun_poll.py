"""SystemdHandle.wait asks systemd about the guest unit once a second only while the
emulator's process is unknown or gone; while it runs, every ALIVE_ASK_TICKS (15 s).
Each ask starts a systemctl process."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401  (sys.path)
from retroboot.guestrun import SystemdHandle, pid_alive


class AskPace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.h = SystemdHandle("s1", Path(self.tmp.name) / "status.json", "retroboot-guest.service")

    def tearDown(self):
        self.tmp.cleanup()

    def asks(self, inactive_since=None, ticks=120):
        return [t for t in range(1, ticks + 1) if self.h._ask_systemd(t, inactive_since)]

    def test_running_emulator_every_15_s(self):
        self.h.pid = os.getpid()
        self.assertTrue(pid_alive(self.h.pid))
        self.assertEqual(self.asks(), [60, 120])

    def test_unknown_or_gone_every_second(self):
        self.h.pid = None
        self.assertEqual(self.asks(ticks=16), [4, 8, 12, 16])
        p = subprocess.Popen(["true"])
        p.wait()
        self.h.pid = p.pid
        self.assertFalse(pid_alive(p.pid))
        self.assertEqual(self.asks(ticks=16), [4, 8, 12, 16])

    def test_once_inactive_every_second(self):
        self.h.pid = os.getpid()
        self.assertEqual(self.asks(inactive_since=1.0, ticks=12), [4, 8, 12])


if __name__ == "__main__":
    unittest.main()
