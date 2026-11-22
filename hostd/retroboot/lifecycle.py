"""Lifecycle event log: /var/lib/retroboot/logs/lifecycle.jsonl.

Each line carries monotonic and wall timestamps, the kernel boot id and the
writing component, so hostd, guest-launcher and evidence tooling can correlate
outer and inner timelines.  Lines are written with a single ``write`` on an
O_APPEND descriptor so concurrent writers never interleave partial records.
"""
from __future__ import annotations

import json
import os
import threading
from collections import deque
from pathlib import Path
from typing import Any

from .util import boot_id, mono, wall


class LifecycleLog:
    def __init__(self, path: Path, source: str = "hostd", keep: int = 2000,
                 max_bytes: int = 8 * 1024 * 1024):
        self.path = Path(path)
        self.source = source
        self.recent: deque[dict] = deque(maxlen=keep)
        self.boot_id = boot_id()
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    def event(self, event_name: str, /, **fields: Any) -> dict:
        rec = {"t_mono": round(mono(), 6), "t_wall": wall(), "boot_id": self.boot_id,
               "source": self.source, "event": event_name}
        rec.update({k: v for k, v in fields.items() if v is not None and k not in rec})
        line = (json.dumps(rec, sort_keys=False, default=str) + "\n").encode()
        with self._lock:
            self.recent.append(rec)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._rotate()
                fd = os.open(str(self.path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
                try:
                    os.write(fd, line)
                finally:
                    os.close(fd)
            except OSError:
                pass
        return rec

    def _rotate(self) -> None:
        try:
            if self.path.stat().st_size > self.max_bytes:
                os.replace(self.path, self.path.with_name(self.path.name + ".1"))
        except OSError:
            pass

    def since(self, t_mono: float) -> list[dict]:
        return [r for r in self.recent if r["t_mono"] >= t_mono]

    def last(self, name: str) -> dict | None:
        for r in reversed(self.recent):
            if r["event"] == name:
                return r
        return None
