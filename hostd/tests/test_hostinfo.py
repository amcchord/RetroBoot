"""hostinfo: the RetroBoot window's About This Computer facts and processor meter."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from retroboot import hostinfo


class HostInfoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proc = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, text):
        (self.proc / name).write_text(text)

    def test_processor_names_lose_marks_and_clock(self):
        for raw, want in (("Intel(R) Pentium(R) Silver N4100 CPU @ 1.10GHz", "Intel Pentium Silver N4100"),
                          ("Intel(R) Core(TM) i7-10510U CPU @ 1.80GHz", "Intel Core i7-10510U"),
                          ("AMD Ryzen 7 5800U with Radeon Graphics", "AMD Ryzen 7 5800U"),
                          ("Intel(R) Celeron(R) N5105 @ 2.00GHz", "Intel Celeron N5105"),
                          ("AMD Ryzen 9 7940HS w/ Radeon 780M Graphics", "AMD Ryzen 9 7940HS")):
            self.write("cpuinfo", f"processor\t: 0\nmodel name\t: {raw}\nflags\t\t: fpu\n")
            self.assertEqual(hostinfo.processor_name(self.proc), want, raw)

    def test_boards_without_a_model_name_and_missing_proc_give_none(self):
        self.write("cpuinfo", "processor\t: 0\nBogoMIPS\t: 108.00\nCPU part\t: 0xd08\n")
        self.assertIsNone(hostinfo.processor_name(self.proc))
        self.assertIsNone(hostinfo.processor_name(self.proc / "missing"))
        self.assertIsNone(hostinfo.memory_bytes(self.proc / "missing"))
        self.assertIsNone(hostinfo.HostCpu(self.proc / "missing").sample(0.0))

    def test_memory_rounds_up_to_what_is_fitted(self):
        self.write("meminfo", "MemTotal:        7956344 kB\nMemFree:  100 kB\n")
        self.assertEqual(hostinfo.memory_text(hostinfo.memory_bytes(self.proc)), "8 GB")
        self.assertEqual(hostinfo.memory_text(900 * 1024 * 1024), "900 MB")
        self.assertIsNone(hostinfo.memory_text(None))

    def test_cpu_share_between_samples_at_least_a_span_apart(self):
        cpu = hostinfo.HostCpu(self.proc, span=0.8)
        self.write("stat", "cpu  100 0 100 800 0 0 0 0 0 0\ncpu0 1 2 3 4\n")
        self.assertIsNone(cpu.sample(10.0))                     # one sample: nothing to compare
        self.write("stat", "cpu  130 0 130 840 0 0 0 0 0 0\n")  # 60 busy of 100 jiffies
        self.assertIsNone(cpu.sample(10.3), "too soon: a redraw doesn't measure a few jiffies")
        self.assertAlmostEqual(cpu.sample(10.9), 0.6)
        self.write("stat", "cpu  130 0 130 940 0 0 0 0 0 0\n")  # idle since
        self.assertAlmostEqual(cpu.sample(11.0), 0.6, msg="the last figure until the span passes")
        self.assertAlmostEqual(cpu.sample(11.8), 0.0)


if __name__ == "__main__":
    unittest.main()
