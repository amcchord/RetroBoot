"""Small shared helpers: atomic files, time, identifiers, subprocesses, redaction."""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import os
import re
import secrets
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable


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
