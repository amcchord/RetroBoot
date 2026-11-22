"""This computer at a glance for the RetroBoot window, as About This Computer shows a Mac:
its processor, memory and how busy its processors are (from /proc; nothing is found where
/proc is missing, as in tests on a Mac, and the window leaves those parts out)."""
from __future__ import annotations

import re
from pathlib import Path

from .util import mono

PROC = Path("/proc")


def processor_name(proc: Path = PROC) -> str | None:
    """"Intel Pentium Silver N4100" from /proc/cpuinfo's model name, without the trademark
    marks and clock; ARM boards give none (their board name says enough)."""
    try:
        text = (proc / "cpuinfo").read_text(errors="replace")
    except OSError:
        return None
    m = re.search(r"^model name\s*:\s*(.+)$", text, re.M)
    if not m:
        return None
    name = re.sub(r"\((?:R|TM|tm|r)\)", "", m.group(1))
    name = re.sub(r"\s+(?:CPU\s+)?@.*$", "", name)
    name = re.sub(r"\s+(?:\d+-Core|with Radeon.*|w/ Radeon.*)\s*.*$", "", name)
    name = re.sub(r"\s+Processor$", "", name)
    return " ".join(name.split()) or None


def memory_bytes(proc: Path = PROC) -> int | None:
    try:
        m = re.search(r"^MemTotal:\s+(\d+) kB", (proc / "meminfo").read_text(), re.M)
    except OSError:
        return None
    return int(m.group(1)) * 1024 if m else None


def memory_text(n: int | None) -> str | None:
    """"8 GB": whole gigabytes, rounded up (MemTotal is a little less than the memory fitted)."""
    if not n:
        return None
    if n < 1024 ** 3:
        return f"{-(-n // (1024 ** 2))} MB"
    return f"{-(-n // (1024 ** 3))} GB"


class HostCpu:
    """How busy all of this computer's processors are, over the last ``span`` seconds or more
    (/proc/stat's first line; None until two samples that far apart, or without /proc).
    Called sooner, ``sample`` gives the last figure again: a window redrawn for a click
    doesn't turn it into the share of a few jiffies."""

    def __init__(self, proc: Path = PROC, span: float = 0.8):
        self.proc = proc
        self.span = span
        self.last: tuple[int, int] | None = None
        self.last_t = 0.0
        self.value: float | None = None

    def _read(self) -> tuple[int, int] | None:
        try:
            line = (self.proc / "stat").read_text().split("\n", 1)[0]
        except OSError:
            return None
        f = [int(v) for v in line.split()[1:]]
        if len(f) < 4:
            return None
        idle = f[3] + (f[4] if len(f) > 4 else 0)
        return sum(f[:8]), idle

    def sample(self, t: float | None = None) -> float | None:
        t = mono() if t is None else t
        if self.last is not None and t - self.last_t < self.span:
            return self.value
        now = self._read()
        if now is None:
            return None
        prev, self.last, self.last_t = self.last, now, t
        if prev is not None and now[0] > prev[0]:
            self.value = max(0.0, min(1.0, 1.0 - (now[1] - prev[1]) / (now[0] - prev[0])))
        return self.value

