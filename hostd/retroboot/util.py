"""Small shared helpers: atomic files, time, identifiers, subprocesses, redaction."""
from __future__ import annotations

import asyncio
import collections
import datetime as _dt
import json
import os
import re
import secrets
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Iterable


def mono() -> float:
    return time.monotonic()


def wall() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def boot_id() -> str | None:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return None


def new_id(nbytes: int = 8) -> str:
    return secrets.token_hex(nbytes)


def evidence_id() -> str:
    """Short, human-readable evidence identifier for classic alerts."""
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"RB-{stamp}-{secrets.token_hex(2).upper()}"


def fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_bytes(path: Path, data: bytes, mode: int = 0o644) -> None:
    """Write via temp file + fsync + rename + directory fsync."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    fsync_dir(path.parent)


def atomic_write_json(path: Path, obj: Any, mode: int = 0o644) -> None:
    atomic_write_bytes(path, (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode(), mode)


def read_json(path: Path, max_bytes: int = 4 * 1024 * 1024) -> Any:
    path = Path(path)
    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(f"{path.name} is too large")
    return json.loads(path.read_text(encoding="utf-8"))


async def run(argv: list[str], timeout: float = 30.0, input: bytes | None = None,
              env: dict | None = None) -> tuple[int, str, str]:
    """Run a command without a shell.  Returns (rc, stdout, stderr); rc 127 when missing."""
    if not argv:
        return 0, "", ""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: not found"
    try:
        out, err = await asyncio.wait_for(proc.communicate(input), timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        return 124, "", f"{argv[0]}: timed out"
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def which(name: str) -> str | None:
    return shutil.which(name)


# --- Redaction -------------------------------------------------------------

_SECRET_KEY_RE = re.compile(
    r'(?i)("?(?:psk|password|passwd|passphrase|secret|token|cookie|csrf|wifi-sec\.psk|'
    r'x-retroboot-csrf|authorization|session)"?\s*[:=]\s*)("[^"]*"|\S+)')
_COOKIE_RE = re.compile(r"(?i)(rb_session=)[^;\s]+")
_UUID_RE = re.compile(r'"?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"?')


def _redact_key_value(m: re.Match) -> str:
    key, value = m.group(1), m.group(2)
    # A guest session id (a UUID under "session") is not a credential (status shows it).
    if _UUID_RE.fullmatch(value) and re.search(r'(?i)(?<![a-z_])"?session"?\s*[:=]\s*$', key):
        return m.group(0)
    # Keep JSON valid: a quoted value stays a quoted string.
    return key + ('"[REDACTED]"' if value.startswith('"') else "[REDACTED]")


class Redactor:
    """Removes credentials from text destined for logs, responses and bundles."""

    def __init__(self) -> None:
        self._literals: set[str] = set()

    def add(self, value: str | None) -> None:
        if value and len(value) >= 4:
            self._literals.add(value)

    def discard(self, value: str | None) -> None:
        if value:
            self._literals.discard(value)

    def __call__(self, text: str) -> str:
        if not text:
            return text
        text = _SECRET_KEY_RE.sub(_redact_key_value, text)
        text = _COOKIE_RE.sub(r"\1[REDACTED]", text)
        for lit in sorted(self._literals, key=len, reverse=True):
            text = text.replace(lit, "[REDACTED]")
        return text

    def obj(self, value: Any) -> Any:
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                if isinstance(k, str) and re.search(r"(?i)psk|password|passwd|secret|token|csrf|cookie", k):
                    out[k] = "[REDACTED]"
                else:
                    out[k] = self.obj(v)
            return out
        if isinstance(value, list):
            return [self.obj(v) for v in value]
        if isinstance(value, str):
            return self(value)
        return value


def tail_lines(path: Path, lines: int, max_bytes: int = 4 * 1024 * 1024) -> list[str]:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError:
        return []
    text = data.decode("utf-8", "replace").splitlines()
    return text[-lines:] if lines > 0 else text


def chunked(it: Iterable, n: int):
    buf = []
    for x in it:
        buf.append(x)
        if len(buf) == n:
            yield buf
            buf = []
    if buf:
        yield buf


KiB = 1024
MiB = 1024 ** 2


def format_speed(bytes_per_sec: float | None) -> str:
    """Format a transfer rate cleanly for classic panel and web status lines."""
    if bytes_per_sec is None or bytes_per_sec <= 0:
        return ""
    if bytes_per_sec >= 100 * MiB:
        return f"{bytes_per_sec / MiB:.0f} MB/s"
    if bytes_per_sec >= MiB:
        return f"{bytes_per_sec / MiB:.1f} MB/s"
    if bytes_per_sec >= 10 * KiB:
        return f"{bytes_per_sec / KiB:.0f} KB/s"
    if bytes_per_sec >= KiB:
        return f"{bytes_per_sec / KiB:.1f} KB/s"
    return f"{int(round(bytes_per_sec))} B/s"


def format_eta(seconds: float | None) -> str:
    """Format remaining transfer duration concisely (e.g. '12 s left', '2m 15s left')."""
    if seconds is None or seconds <= 0 or seconds > 48 * 3600:
        return ""
    if seconds < 60:
        return f"{max(1, int(round(seconds)))} s left"
    if seconds < 300:
        s = int(round(seconds))
        return f"{s // 60}m {s % 60:02d}s left"
    if seconds < 3600:
        return f"{int(round(seconds / 60))} min left"
    m = int(round(seconds / 60))
    return f"{m // 60}h {m % 60:02d}m left"


class TransferMeter:
    """Sliding-window transfer rate and ETA estimator that handles resumed offsets."""

    def __init__(self, total: int | None = None, initial: int = 0, *, window_s: float = 4.0,
                 clock: Callable[[], float] = time.monotonic):
        self.total = int(total) if total is not None and total > 0 else None
        self.window_s = max(0.5, float(window_s))
        self._clock = clock
        self._samples: collections.deque[tuple[float, int]] = collections.deque()
        self.reset(initial)

    def reset(self, initial: int = 0) -> None:
        now = self._clock()
        self._last_done = max(0, int(initial))
        self._samples.clear()
        self._samples.append((now, self._last_done))

    def update(self, done: int) -> tuple[float | None, float | None]:
        done = max(0, int(done))
        if done < self._last_done:
            self.reset(done)
            return None, None
        now = self._clock()
        self._last_done = done
        self._samples.append((now, done))
        cutoff = now - self.window_s
        while len(self._samples) > 2 and self._samples[1][0] <= cutoff:
            self._samples.popleft()
        t0, b0 = self._samples[0]
        dt = now - t0
        db = done - b0
        if dt < 0.15 or db <= 0:
            return None, None
        bps = db / dt
        eta = None
        if self.total is not None and self.total > done and bps > 0:
            eta = (self.total - done) / bps
        return bps, eta


def format_transfer_progress(
    prefix: str,
    done: int,
    total: int | None,
    speed_bps: float | None = None,
    eta_s: float | None = None,
    *,
    include_pct: bool = True,
) -> str:
    """Combine prefix, percentage, transferred/total size, live speed, and ETA."""
    parts: list[str] = []
    if include_pct and total and total > 0:
        pct = int(round(max(0.0, min(1.0, done / total)) * 100))
        parts.append(f"{pct}%")
    if total and total >= MiB:
        parts.append(f"{done // MiB} of {total // MiB} MB")
    elif total and total > 0:
        parts.append(f"{done // KiB} of {max(1, total // KiB)} KB")
    else:
        parts.append(f"{done // MiB} MB")
    spd = format_speed(speed_bps)
    if spd:
        parts.append(spd)
    eta = format_eta(eta_s)
    if eta:
        parts.append(eta)
    return f"{prefix} {' · '.join(parts)}".strip()
