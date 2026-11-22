import asyncio
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from retroboot import tiger_cpu


class TigerPlacement(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for cpu in range(24):
            physical = cpu % 12
            high = physical < 4
            values = {
                "topology/physical_package_id": 0,
                "topology/core_id": physical if high else physical + 4,
                "cpufreq/cpuinfo_max_freq": 4367000,
                "cpufreq/scaling_driver": "amd-pstate-epp",
                "cache/index3/level": 3, "cache/index3/type": "Unified",
                "cache/index3/shared_cpu_list": "0-3,12-15" if high else "4-11,16-23",
                "acpi_cppc/lowest_nonlinear_perf": 76 if high else 53,
                "acpi_cppc/highest_perf": (196, 202, 208, 208)[physical] if high else 125,
            }
            for name, value in values.items():
                path = self.root / f"cpu{cpu}" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(str(value))

    def test_corroborated_cppc_groups_one_eligible_sibling_per_fast_core(self):
        self.assertEqual(tiger_cpu.fast_pool(set(range(24)), self.root), {0, 1, 2, 3})
        self.assertEqual(tiger_cpu.fast_pool({1, 2, 4, 12, 15, 17, 20, 22}, self.root), {1, 2, 12, 15})

    def test_ambiguous_cppc_ranking_keeps_normal_affinity(self):
        (self.root / "cpu4/acpi_cppc/highest_perf").write_text("196")
        self.assertEqual(tiger_cpu.fast_pool(set(range(24)), self.root), set())

    def test_distinct_clock_classes_and_single_cpu_fallback(self):
        for cpu in (1, 2, 13, 14):
            (self.root / f"cpu{cpu}/cpufreq/cpuinfo_max_freq").write_text("5157895")
        self.assertEqual(tiger_cpu.fast_pool(set(range(24)), self.root), {1, 2})
        self.assertEqual(tiger_cpu.fast_pool({1, 4}, self.root), set())

    def test_failed_second_placement_restores_first_thread(self):
        state = {30: {1, 2, 3}, 31: {1, 2, 3}}
        tasks = {}
        for tid, name in zip(state, sorted(tiger_cpu.WORKERS)):
            task = self.root / str(tid)
            task.mkdir()
            (task / "comm").write_text(name)
            (task / "stat").write_text(f"{tid} ({name}) " + " ".join(["0"] * 20))
            tasks[name] = task

        def apply(tid, mask):
            if tid == 31:
                raise PermissionError()
            state[tid] = mask

        original = {name: set(state[int(t.name)]) for name, t in tasks.items()}
        with mock.patch.object(tiger_cpu.os, "sched_setaffinity", apply, create=True), \
                mock.patch.object(tiger_cpu.os, "sched_getaffinity", state.get, create=True):
            with self.assertRaises(PermissionError):
                tiger_cpu.set_workers(tasks, {1, 2}, original)
        self.assertEqual(state, {30: {1, 2, 3}, 31: {1, 2, 3}})

    def test_waits_for_ati_worker_then_sets_both_threads(self):
        async def run():
            events = mock.Mock()
            proc = SimpleNamespace(pid=20, returncode=None)
            tasks = {"ALL CPUs/TCG": Path("30"), "ati-gl": Path("31")}
            with mock.patch.object(tiger_cpu, "workers", side_effect=[{}, tasks]), \
                    mock.patch.object(tiger_cpu.asyncio, "sleep", new=mock.AsyncMock()), \
                    mock.patch.object(tiger_cpu, "fast_pool", return_value={1, 2}), \
                    mock.patch.object(tiger_cpu.os, "sched_getaffinity", return_value={0, 1, 2}, create=True), \
                    mock.patch.object(tiger_cpu, "set_workers") as apply:
                await tiger_cpu.place_when_ready(proc, events, "session")
                self.assertEqual(apply.call_count, 1)
                self.assertEqual(events.event.call_args.kwargs["cpus"], [1, 2])
        asyncio.run(run())
