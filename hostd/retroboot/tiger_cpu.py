"""Place an opted-in Tiger's two persistent QEMU workers on fast physical cores."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

CPU_ROOT = Path("/sys/devices/system/cpu")
WORKERS = {"ALL CPUs/TCG", "ati-gl"}


def fast_pool(allowed: set[int], cpu_root: Path = CPU_ROOT) -> set[int]:
    """Return one allowed logical CPU per fast core, or none on ambiguous hardware."""
    cores = {}
    rates = {}
    for cpu in sorted(allowed):
        path = cpu_root / f"cpu{cpu}"
        cores[cpu] = tuple(int((path / "topology" / key).read_text()) for key in
                           ("physical_package_id", "core_id"))
        rates[cpu] = int((path / "cpufreq/cpuinfo_max_freq").read_text())
    if len(set(rates.values())) > 1:
        selected = {cpu for cpu in allowed if rates[cpu] == max(rates.values())}
    elif len({package for package, _ in cores.values()}) == 1:
        selected = cppc_fast_domain(allowed, cpu_root)
    else:
        return set()
    # Do not split an eligible SMT pair across inferred performance classes.
    if {cores[c] for c in selected} & {cores[c] for c in allowed - selected}:
        return set()
    result = {}
    for cpu in sorted(selected):
        result.setdefault(cores[cpu], cpu)
    return set(result.values()) if len(result) >= 2 else set()


def cppc_fast_domain(allowed: set[int], cpu_root: Path) -> set[int]:
    """Some AMD firmware advertises one max clock for both core complexes.

    Require two L3 domains, with agreement between both firmware performance
    fields. Individual preferred-core ranks may differ in a single domain.
    """
    domains = {}
    for cpu in allowed:
        path = cpu_root / f"cpu{cpu}"
        if not (path / "cpufreq/scaling_driver").read_text().strip().startswith("amd-pstate"):
            return set()
        cache = path / "cache/index3"
        if (cache / "level").read_text().strip() != "3" or (cache / "type").read_text().strip() != "Unified":
            return set()
        group = (cache / "shared_cpu_list").read_text().strip()
        nonlinear = int((path / "acpi_cppc/lowest_nonlinear_perf").read_text())
        peak = int((path / "acpi_cppc/highest_perf").read_text())
        domains.setdefault(group, []).append((nonlinear, peak, cpu))
    if len(domains) != 2 or any(len({low for low, _, _ in rows}) != 1 for rows in domains.values()):
        return set()
    slow, fast = sorted(domains.values(), key=lambda rows: rows[0][0])
    if slow[0][0] >= fast[0][0] or max(row[1] for row in slow) >= min(row[1] for row in fast):
        return set()
    return {cpu for _, _, cpu in fast}


def workers(pid: int) -> dict[str, Path]:
    found = {}
    for task in (Path("/proc") / str(pid) / "task").iterdir():
        try:
            name = (task / "comm").read_text().strip()
        except FileNotFoundError:
            continue
        if name in WORKERS:
            if name in found:
                raise ValueError(f"Multiple {name} threads")
            found[name] = task
    return found


def set_workers(tasks: dict[str, Path], pool: set[int], original: dict[str, set[int]]) -> None:
    # Both task paths belong to this specific child, including during rollback.
    def birth(task):
        # /proc/.../stat field 22, after the parenthesized thread name.
        return int((task / "stat").read_text().rsplit(")", 1)[1].split()[19])

    starts = {name: birth(task) for name, task in tasks.items()}
    changed = []
    try:
        for name, task in tasks.items():
            tid = int(task.name)
            if birth(task) != starts[name] or (task / "comm").read_text().strip() != name or \
                    os.sched_getaffinity(tid) != original[name]:
                raise ValueError("Worker changed before CPU placement")
            os.sched_setaffinity(tid, pool)
            changed.append((name, task))
            if os.sched_getaffinity(tid) != pool:
                raise ValueError("CPU placement changed during startup")
    except (OSError, ValueError):
        for name, task in reversed(changed):
            try:
                if birth(task) == starts[name] and (task / "comm").read_text().strip() == name:
                    os.sched_setaffinity(int(task.name), original[name])
            except FileNotFoundError:
                pass
        raise


async def place_when_ready(proc, lifecycle, session: str) -> None:
    """The ATI thread starts asynchronously. Wait while the original child runs."""
    try:
        while proc.returncode is None:
            tasks = workers(proc.pid)
            if tasks.keys() == WORKERS:
                original = {name: os.sched_getaffinity(int(t.name)) for name, t in tasks.items()}
                pool = fast_pool(set.intersection(*original.values()))
                if pool:
                    set_workers(tasks, pool, original)
                lifecycle.event("tiger_cpu_pool", session=session, cpus=sorted(pool),
                                workers={name: int(t.name) for name, t in tasks.items()})
                return
            await asyncio.sleep(1)
    except (OSError, ValueError) as exc:
        lifecycle.event("tiger_cpu_pool_unavailable", session=session, error=str(exc))
