"""Where the emulator's threads run on a board with fast and slow cores (Gemini PDA).

The Gemini's MT6797X has three clusters: cpu0-3 Cortex-A53 at 624-1118 MHz, cpu4-7
Cortex-A53 at 650-1352 MHz and cpu8-9 Cortex-A72 at 845-1495 MHz.  PowerPC
emulation is one TCG thread that is busy whenever Mac OS is, so it belongs on an
A72 and nothing else should share those two cores.  While a guest runs (launched
or adopted after a hostd restart) this module

* pins QEMU's TCG thread(s) to ``vcpu`` (cpu8-9) and QEMU's other threads (main
  loop with the display and sound, RCU, the block thread pool) to ``emulator``
  (cpu4-7).  QEMU 11 names its threads by default (the r18 package's
  qemu-system-ppc, checked on rb-builder with and without ``-name
  ...,debug-threads=on``): the PowerPC Mac's TCG thread is ``ALL CPUs/TCG``
  (round-robin; ``CPU 0/TCG`` with MTTCG).  Without names (an older package) the
  busiest thread is taken;
* pins hostd, the presenter (``rb-presenter``) and guest-launcher to ``host``
  (cpu4-7), so they never wake on an A72;
* sets the unbound workqueue cpumask (``/sys/devices/virtual/workqueue/cpumask``)
  to ``workqueue`` (cpu0-7): geminipda-drm's commit worker copies the scanout
  there, and gemini-debian's udev rule would have moved it onto the A72s;
* switches the vCPU cluster's cpufreq policy to ``governor`` (schedutil),
  raises its ``scaling_min_freq`` to ``min_mhz`` and sets its ``scaling_max_freq``
  to ``max_mhz`` (clamped to ``cpuinfo_max_freq``) while Mac OS runs, and restores
  all three when it stops.  The Gemini's a72dvfs kernel boots its A72 policy
  capped at the proven 1495 MHz; ``max_mhz`` lifts that cap only while the guest
  runs.  Thermal cooling is a separate frequency limit (a QoS request), so it
  still caps the policy and the 80/85 °C trips stay in charge.

The A72 cluster comes online late (mt6797-cl2-power, about 2 s into boot, and
the kernel runs with maxcpus=8), and a later kernel may leave it offline.  An
affinity naming only offline CPUs is refused by the kernel, so while no vcpu
CPU is online the TCG thread gets ``vcpu | emulator`` (it runs on the A53s and
may move by itself); every poll re-reads ``cpu/online`` and the threads, policy
and workqueue mask, and re-applies whatever drifted (a CPU going offline resets
a thread's affinity; the udev rule rewrites the workqueue mask when cpu9 comes
up).  The CPUs are never brought on- or offline here.

Nothing here may fail a launch: every step is best effort, recorded in
:meth:`CpuPlacement.public` and the lifecycle log (``cpu_placement``).  Every
knob is a hostd configuration key (config.py ``cpu_*``) so the board can be
A/B tested from ``hostd.local.json``; on a PC or Raspberry Pi nothing happens
unless ``cpu_placement`` is "on".
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger("retroboot.cpuplace")

VCPU_COMM = re.compile(r"^(CPU \d+/(TCG|KVM)|ALL CPUs/TCG)$")
PRESENTER_COMM = "rb-presenter"
DISABLED_WORDS = ("none", "off", "no", "-")


def parse_cpulist(text: str | None) -> set[int]:
    """Linux cpulist ("0-3,8-9", "4", "") -> {0, 1, 2, 3, 8, 9}.  Bad parts are skipped."""
    out: set[int] = set()
    for part in (text or "").replace(" ", "").split(","):
        if not part:
            continue
        a, sep, b = part.partition("-")
        try:
            lo = int(a)
            hi = int(b) if sep else lo
        except ValueError:
            continue
        if 0 <= lo <= hi < 4096:
            out.update(range(lo, hi + 1))
    return out


def format_cpulist(cpus) -> str:
    """{0, 1, 2, 3, 8, 9} -> "0-3,8-9"."""
    s = sorted(set(cpus))
    parts, i = [], 0
    while i < len(s):
        j = i
        while j + 1 < len(s) and s[j + 1] == s[j] + 1:
            j += 1
        parts.append(str(s[i]) if i == j else f"{s[i]}-{s[j]}")
        i = j + 1
    return ",".join(parts)


def cpumask_hex(cpus) -> str:
    """A cpumask as the kernel prints it: hex, 32-bit groups separated by commas."""
    mask = 0
    for c in cpus:
        mask |= 1 << c
    words = []
    while True:
        words.append(f"{mask & 0xffffffff:08x}")
        mask >>= 32
        if not mask:
            break
    text = ",".join(reversed(words))
    head, _, rest = text.partition(",")
    head = head.lstrip("0") or "0"
    return head + ("," + rest if rest else "")


def parse_cpumask(text: str | None) -> set[int]:
    """The kernel's hex cpumask ("ff", "00000300", "1,00000000") -> CPUs."""
    try:
        mask = int((text or "").strip().replace(",", ""), 16)
    except ValueError:
        return set()
    out, i = set(), 0
    while mask:
        if mask & 1:
            out.add(i)
        mask >>= 1
        i += 1
    return out


@dataclass
class Plan:
    enabled: bool = False
    source: str = "off"                  # "profile" | "config" | "off"
    vcpu: set[int] = field(default_factory=set)
    emulator: set[int] = field(default_factory=set)
    host: set[int] = field(default_factory=set)
    workqueue: set[int] | None = None
    governor: str | None = None
    min_mhz: int = 0
    max_mhz: int = 0                     # 0: leave scaling_max_freq alone
    poll_s: float = 2.0


def _choose(value, default):
    """A configuration value: "" (or None, -1) takes the profile's, a DISABLED_WORDS word none."""
    if value is None or value == "" or value == -1:
        return default
    if isinstance(value, str) and value.strip().lower() in DISABLED_WORDS:
        return None
    return value


def resolve(cfg, plat) -> Plan:
    """The placement for this board and configuration (config.py ``cpu_*``)."""
    mode = str(getattr(cfg, "cpu_placement", "auto") or "auto").lower()
    prof = (plat.profile.get("cpu") if plat is not None else None) or {}
    if mode in DISABLED_WORDS or (mode == "auto" and not prof):
        return Plan()
    vcpu = _choose(getattr(cfg, "cpu_vcpu", ""), prof.get("vcpu"))
    emu = _choose(getattr(cfg, "cpu_emulator", ""), prof.get("emulator"))
    host = _choose(getattr(cfg, "cpu_host", ""), prof.get("host"))
    wq = _choose(getattr(cfg, "cpu_workqueue", ""), prof.get("workqueue"))
    gov = _choose(getattr(cfg, "cpu_governor", ""), prof.get("governor"))
    min_mhz = _choose(getattr(cfg, "cpu_min_mhz", -1), prof.get("min_mhz", 0))
    try:
        min_mhz = max(0, int(min_mhz or 0))
    except (TypeError, ValueError):
        min_mhz = 0
    max_mhz = _choose(getattr(cfg, "cpu_max_mhz", -1), prof.get("max_mhz", 0))
    try:
        max_mhz = max(0, int(max_mhz or 0))
    except (TypeError, ValueError):
        max_mhz = 0
    try:
        poll = max(0.5, float(getattr(cfg, "cpu_poll_s", 2.0)))
    except (TypeError, ValueError):
        poll = 2.0
    gov = str(gov).strip() if gov else None
    if gov and not re.fullmatch(r"[a-z_]{1,32}", gov):
        gov = None
    return Plan(enabled=True, source="profile" if mode == "auto" else "config",
                vcpu=parse_cpulist(vcpu) if vcpu else set(), emulator=parse_cpulist(emu) if emu else set(),
                host=parse_cpulist(host) if host else set(), workqueue=parse_cpulist(wq) if wq else None,
                governor=gov, min_mhz=min_mhz, max_mhz=max_mhz, poll_s=poll)


def describe(plan: Plan) -> dict:
    return {"enabled": plan.enabled, "source": plan.source, "vcpu": format_cpulist(plan.vcpu),
            "emulator": format_cpulist(plan.emulator), "host": format_cpulist(plan.host),
            "workqueue": format_cpulist(plan.workqueue) if plan.workqueue is not None else None,
            "governor": plan.governor, "min_mhz": plan.min_mhz, "max_mhz": plan.max_mhz,
            "poll_s": plan.poll_s}


def _read(p: Path, limit: int = 4096) -> str | None:
    try:
        with open(p, "r", errors="replace") as f:
            return f.read(limit).strip()
    except OSError:
        return None


def _write(p: Path, value: str) -> None:
    with open(p, "w") as f:
        f.write(value)


class CpuPlacement:
    """See the module docstring.  ``sysfs``/``proc`` roots, ``setaffinity``/``getaffinity``
    and ``clock`` are injectable for tests."""

    def __init__(self, plan: Plan, *, sysfs: str | Path = "/sys", proc: str | Path = "/proc",
                 run_file: str | Path | None = None, lifecycle=None,
                 setaffinity: Callable[[int, set], None] | None = None,
                 getaffinity: Callable[[int], set] | None = None, clock: Callable[[], float] = time.monotonic,
                 self_pid: int | None = None):
        self.plan = plan
        self.sysfs = Path(sysfs)
        self.proc = Path(proc)
        self.run_file = Path(run_file) if run_file else None
        self.lifecycle = lifecycle
        self.setaffinity = setaffinity or os.sched_setaffinity
        self.getaffinity = getaffinity or os.sched_getaffinity
        self.clock = clock
        self.self_pid = self_pid if self_pid is not None else os.getpid()
        self.session: str | None = None
        self.qemu_pid: int | None = None
        self._task: asyncio.Task | None = None
        self._placed: dict[int, tuple] = {}        # tid -> affinity we set
        self._host_placed: dict[int, tuple] = {}
        self.saved: dict = {}                       # what we changed: {"governor": {path: old}, ...}
        self.state: dict = {"active": False}
        self.errors: list[str] = []
        self.last_online: set[int] | None = None
        self.started: float | None = None
        self.guest_asleep = False          # Mac OS sleeps: the policy is left to the kernel
        self._mutex = threading.RLock()    # a poll's pass and a sleep's restore never interleave
        # The lid is closed (lidpower.py): every policy capped at its lowest frequency; the
        # polls leave the policy alone, and an original given back meanwhile goes to lid_hold
        # (the value the lid's opening restores) instead of sysfs.
        self.lid_capped = False
        self.lid_hold: Callable[[str, str], bool] | None = None

    # -- sysfs ---------------------------------------------------------------------------------
    def online(self) -> set[int]:
        text = _read(self.sysfs / "devices/system/cpu/online")
        return parse_cpulist(text) if text is not None else set(range(os.cpu_count() or 1))

    def policy_dir(self) -> Path | None:
        """cpufreq policy of the first vcpu CPU that has one (cpuN/cpufreq; present only
        while that CPU is online with a cpufreq driver)."""
        for cpu in sorted(self.plan.vcpu):
            d = self.sysfs / f"devices/system/cpu/cpu{cpu}/cpufreq"
            if (d / "scaling_governor").exists():
                try:
                    return d.resolve()
                except OSError:
                    return d
        return None

    def _note_error(self, what: str, exc: BaseException) -> None:
        msg = f"{what}: {type(exc).__name__}: {exc}"[:200]
        if msg not in self.errors:
            self.errors = (self.errors + [msg])[-8:]
            log.info("cpu placement: %s", msg)

    def _save(self, kind: str, key: str, value) -> None:
        bucket = self.saved.setdefault(kind, {})
        if key not in bucket:
            bucket[key] = value
            self._persist()

    def _persist(self) -> None:
        if not self.run_file:
            return
        try:
            self.run_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.run_file.with_name(self.run_file.name + ".tmp")
            tmp.write_text(json.dumps(self.saved))
            os.replace(tmp, self.run_file)
        except OSError as exc:
            self._note_error("run file", exc)

    def load_saved(self) -> None:
        """Originals saved by an earlier hostd (restarted while a guest ran)."""
        if not self.run_file:
            return
        try:
            data = json.loads(self.run_file.read_text())
            if isinstance(data, dict):
                for kind, bucket in data.items():
                    if isinstance(bucket, dict):
                        for k, v in bucket.items():
                            self.saved.setdefault(kind, {}).setdefault(k, v)
        except (OSError, ValueError):
            pass

    def ensure_policy(self) -> dict:
        out: dict = {}
        d = self.policy_dir()
        if d is None:
            out["policy"] = None
            return out
        out["policy"] = d.name
        if self.guest_asleep or self.lid_capped:
            # Mac OS sleeps (its processor halted): the policy keeps the kernel's own
            # governor and boot cap until it wakes (guest_sleep); the lid is closed: the
            # policy stays at its lowest frequency until it opens (lidpower.py)
            out["governor"] = _read(d / "scaling_governor")
            if self.guest_asleep:
                out["asleep"] = True
            if self.lid_capped:
                out["lid_closed"] = True
            return out
        if self.plan.governor:
            gov_file = d / "scaling_governor"
            cur = _read(gov_file)
            avail = (_read(d / "scaling_available_governors") or "").split()
            if avail and self.plan.governor not in avail:
                out["governor"] = cur
                out["governor_unavailable"] = self.plan.governor
            elif cur is not None and cur != self.plan.governor:
                try:
                    self._save("governor", str(gov_file), cur)
                    _write(gov_file, self.plan.governor)
                except OSError as exc:
                    self._note_error(f"governor {d.name}", exc)
                out["governor"] = _read(gov_file)
            else:
                out["governor"] = cur
        if self.plan.min_mhz > 0:
            min_file = d / "scaling_min_freq"
            cur = _read(min_file)
            top = _read(d / "cpuinfo_max_freq")
            try:
                want = self.plan.min_mhz * 1000
                if top and top.isdigit():
                    want = min(want, int(top))
                if cur is not None and cur.isdigit() and int(cur) != want:
                    self._save("min_freq", str(min_file), cur)
                    _write(min_file, str(want))
            except OSError as exc:
                self._note_error(f"min_freq {d.name}", exc)
            out["min_khz"] = _read(min_file)
        if self.plan.max_mhz > 0:
            max_file = d / "scaling_max_freq"
            cur = _read(max_file)
            top = _read(d / "cpuinfo_max_freq")
            try:
                want = self.plan.max_mhz * 1000
                if top and top.isdigit():
                    want = min(want, int(top))
                if cur is not None and cur.isdigit() and int(cur) != want:
                    self._save("max_freq", str(max_file), cur)
                    _write(max_file, str(want))
            except OSError as exc:
                self._note_error(f"max_freq {d.name}", exc)
            out["max_khz"] = _read(max_file)
        cur_khz = _read(d / "scaling_cur_freq")
        if cur_khz and cur_khz.isdigit():
            out["cur_mhz"] = int(cur_khz) // 1000
        return out

    def ensure_workqueue(self, online: set[int]) -> str | None:
        if self.plan.workqueue is None:
            return None
        f = self.sysfs / "devices/virtual/workqueue/cpumask"
        cur = _read(f)
        if cur is None:
            return None
        possible = parse_cpulist(_read(self.sysfs / "devices/system/cpu/possible")) or online
        want = (self.plan.workqueue & possible) or None
        if not want:
            return cur
        if parse_cpumask(cur) != want:
            try:
                self._save("workqueue", str(f), cur)
                _write(f, cpumask_hex(want))
            except OSError as exc:
                self._note_error("workqueue cpumask", exc)
        return _read(f)

    # -- threads -------------------------------------------------------------------------------
    def threads(self, pid: int) -> list[tuple[int, str, int]]:
        """[(tid, comm, cpu ticks)] of a process; [] when it is gone."""
        out = []
        try:
            tids = sorted(int(t) for t in os.listdir(self.proc / str(pid) / "task") if t.isdigit())
        except OSError:
            return out
        for tid in tids:
            base = self.proc / str(pid) / "task" / str(tid)
            comm = _read(base / "comm") or ""
            ticks = 0
            stat = _read(base / "stat") or ""
            # fields after the ")" of comm: state ppid ... utime(14) stime(15)
            rest = stat.rpartition(")")[2].split()
            if len(rest) >= 13:
                try:
                    ticks = int(rest[11]) + int(rest[12])
                except ValueError:
                    ticks = 0
            out.append((tid, comm, ticks))
        return out

    def _set(self, tid: int, cpus: set[int], placed: dict, online: set[int]) -> bool:
        """Pin one thread (the kernel keeps offline CPUs of the mask for later; reading the
        affinity back shows only the online ones)."""
        key = tuple(sorted(cpus))
        try:
            if placed.get(tid) == key and set(self.getaffinity(tid)) == (set(cpus) & online):
                return True
        except OSError:
            placed.pop(tid, None)
            return False
        try:
            self.setaffinity(tid, set(cpus))
            placed[tid] = key
            return True
        except OSError as exc:
            if getattr(exc, "errno", None) != 3:      # ESRCH: the thread ended meanwhile
                self._note_error(f"affinity {tid}", exc)
            placed.pop(tid, None)
            return False

    def _usable(self, cpus: set[int], online: set[int]) -> bool:
        return bool(cpus & online)

    def place_qemu(self, pid: int, online: set[int]) -> dict:
        ths = self.threads(pid)
        if not ths:
            return {"threads": 0}
        vcpus = [t for t in ths if VCPU_COMM.match(t[1])]
        by = "name"
        if not vcpus:
            # no thread names: the busiest thread other than the main loop, once it is
            # clearly the emulator (more than a second of CPU)
            busiest = max((t for t in ths if t[0] != pid), key=lambda t: t[2], default=None)
            if busiest and busiest[2] >= 100:
                vcpus, by = [busiest], "busiest"
            else:
                by = None
        vcpu_ids = {t[0] for t in vcpus}
        a72_up = self._usable(self.plan.vcpu, online)
        vcpu_set = self.plan.vcpu if a72_up else (self.plan.vcpu | self.plan.emulator)
        emu_set = self.plan.emulator if self._usable(self.plan.emulator, online) else None
        placed_v = placed_e = 0
        for tid, comm, _ in ths:
            if tid in vcpu_ids:
                if vcpu_set and self._usable(vcpu_set, online) and self._set(tid, vcpu_set, self._placed, online):
                    placed_v += 1
            elif emu_set and self._set(tid, emu_set, self._placed, online):
                placed_e += 1
        live = {t[0] for t in ths}
        for tid in list(self._placed):
            if tid not in live:
                del self._placed[tid]
        return {"threads": len(ths), "vcpu_threads": sorted(t[1] for t in vcpus), "vcpu_by": by,
                "vcpu_cpus": format_cpulist(vcpu_set) if vcpus else None, "vcpu_on_fast_cores": a72_up,
                "placed_vcpu": placed_v, "placed_other": placed_e}

    def host_pids(self, qemu_pid: int | None) -> list[int]:
        pids = {self.self_pid}
        try:
            for name in os.listdir(self.proc):
                if name.isdigit() and _read(self.proc / name / "comm") == PRESENTER_COMM:
                    pids.add(int(name))
        except OSError:
            pass
        if qemu_pid:
            stat = _read(self.proc / str(qemu_pid) / "stat") or ""
            rest = stat.rpartition(")")[2].split()
            if len(rest) >= 2 and rest[1].isdigit():
                parent = int(rest[1])
                if (_read(self.proc / str(parent) / "comm") or "").startswith("python"):
                    pids.add(parent)        # guest-launcher
        return sorted(pids)

    def place_host(self, qemu_pid: int | None, online: set[int]) -> int:
        if not self.plan.host or not self._usable(self.plan.host, online):
            return 0
        n = 0
        for pid in self.host_pids(qemu_pid):
            for tid, _, _ in self.threads(pid):
                if self._set(tid, self.plan.host, self._host_placed, online):
                    n += 1
        return n

    def apply_once(self, qemu_pid: int | None) -> dict:
        """One pass: re-read everything and fix what drifted.  Never raises."""
        with self._mutex:
            return self._apply_once(qemu_pid)

    def _apply_once(self, qemu_pid: int | None) -> dict:
        res: dict = {}
        try:
            online = self.online()
            res["online"] = format_cpulist(online)
            if self.last_online is not None and online != self.last_online and self.lifecycle:
                self.lifecycle.event("cpu_online_changed", online=res["online"],
                                     previous=format_cpulist(self.last_online))
            self.last_online = online
            if qemu_pid:
                res.update(self.place_qemu(qemu_pid, online))
            res["host_threads"] = self.place_host(qemu_pid, online)
            res["workqueue"] = self.ensure_workqueue(online)
            res.update(self.ensure_policy())
        except Exception as exc:  # noqa: BLE001 -- placement must never disturb Mac OS
            self._note_error("apply", exc)
        if self.errors:
            res["errors"] = list(self.errors)
        return res

    def restore(self, kinds: tuple = ("governor", "min_freq", "max_freq", "workqueue")) -> dict:
        """Give back the governor, min and max frequency and workqueue mask saved before
        (``kinds``: only those)."""
        with self._mutex:
            return self._restore(kinds)

    def _restore(self, kinds: tuple) -> dict:
        done = {}
        for kind in kinds:
            for path, value in list((self.saved.get(kind) or {}).items()):
                try:
                    if not (kind == "max_freq" and self.lid_capped and self.lid_hold is not None
                            and self.lid_hold(path, str(value))):
                        _write(Path(path), str(value))
                    done[path] = value
                except OSError as exc:
                    if not isinstance(exc, FileNotFoundError):      # a policy gone with its CPU
                        self._note_error(f"restore {kind}", exc)
                self.saved[kind].pop(path, None)
        self.saved = {k: v for k, v in self.saved.items() if v}
        self._persist()
        if self.run_file and not self.saved:
            try:
                self.run_file.unlink()
            except OSError:
                pass
        return done

    # -- guest lifecycle -----------------------------------------------------------------------
    POLICY_KINDS = ("governor", "min_freq", "max_freq")

    async def guest_sleep(self, asleep: bool) -> None:
        """battery.py: Mac OS went to sleep (its processor halts) or woke.  Asleep, the
        vCPU policy gets its governor and its min/max frequency back (the kernel's
        schedutil and, on the a72dvfs kernel, the boot cap); awake, the plan is applied
        again at once.  Thread placement and the workqueue mask stay."""
        if not self.plan.enabled or asleep == self.guest_asleep:
            return
        self.guest_asleep = asleep
        if not self.state.get("active"):
            return
        if asleep:
            restored = await asyncio.to_thread(self.restore, self.POLICY_KINDS)
            if self.lifecycle:
                self.lifecycle.event("cpu_placement_sleep", restored=sorted(restored))
        else:
            res = await asyncio.to_thread(self.apply_once, self.qemu_pid)
            if self.lifecycle:
                self.lifecycle.event("cpu_placement_wake", governor=res.get("governor"), max_khz=res.get("max_khz"))

    def guest_started(self, session: str, pid_source: Callable[[], int | None]) -> None:
        """Called by the supervisor once QEMU is being launched or was adopted.
        ``pid_source`` returns QEMU's pid once known (the launcher's status.json)."""
        if not self.plan.enabled:
            return
        asleep = self.guest_asleep         # an adopted Mac OS may already have said it sleeps
        self.guest_stopped(session=None, restore=False)
        self.guest_asleep = asleep
        self.session = session
        self.started = self.clock()
        self.state = {"active": True, "session": session, "plan": describe(self.plan)}
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = loop.create_task(self._run(session, pid_source), name="cpu-placement")

    def guest_stopped(self, session: str | None = None, restore: bool = True) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None
        self._placed.clear()
        self.qemu_pid = None
        self.guest_asleep = False
        was = self.state.get("active")
        if restore and self.plan.enabled:
            restored = self.restore()
            if was and self.lifecycle:
                self.lifecycle.event("cpu_placement_released", session=session or self.session,
                                     restored=sorted(restored))
        self.state = dict(self.state, active=False)

    async def _run(self, session: str, pid_source) -> None:
        reported = None
        t0 = self.clock()
        try:
            while True:
                pid = None
                try:
                    pid = pid_source()
                except Exception:  # noqa: BLE001
                    pid = None
                if pid and pid != self.qemu_pid:
                    self.qemu_pid = pid
                    self._placed.clear()
                res = await asyncio.to_thread(self.apply_once, self.qemu_pid)
                self.state = {"active": True, "session": session, "qemu_pid": self.qemu_pid,
                              "plan": describe(self.plan), **res}
                summary = (res.get("vcpu_cpus"), res.get("vcpu_on_fast_cores"), res.get("governor"),
                           res.get("workqueue"), tuple(res.get("vcpu_threads") or ()))
                if summary != reported and (self.qemu_pid or self.clock() - t0 > 5):
                    reported = summary
                    if self.lifecycle:
                        self.lifecycle.event("cpu_placement", session=session, **{
                            k: res.get(k) for k in ("online", "vcpu_threads", "vcpu_by", "vcpu_cpus",
                                                    "vcpu_on_fast_cores", "policy", "governor", "min_khz",
                                                    "workqueue", "threads", "host_threads", "errors")
                            if res.get(k) is not None})
                # quickly while QEMU starts its threads, then every poll_s (2 s), then
                # every 5 poll_s once the TCG thread is on its cores for a minute
                elapsed = self.clock() - t0
                if not res.get("vcpu_threads") and elapsed < 10:
                    delay = 0.25
                elif elapsed < 120 or not res.get("vcpu_on_fast_cores"):
                    delay = self.plan.poll_s
                else:
                    delay = self.plan.poll_s * 5
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._note_error("loop", exc)

    def public(self) -> dict:
        out = dict(self.state)
        out.setdefault("plan", describe(self.plan))
        if self.errors:
            out["errors"] = list(self.errors)
        return out


def from_app(app) -> CpuPlacement:
    cfg = app.cfg
    plan = resolve(cfg, app.platform)
    cp = CpuPlacement(plan, sysfs=cfg.sysfs_path, proc=getattr(cfg, "proc_path", "/proc"),
                      run_file=app.paths.run / "cpuplace.json", lifecycle=app.lifecycle)
    cp.load_saved()
    return cp
