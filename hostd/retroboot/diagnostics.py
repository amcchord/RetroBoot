"""Logs, evidence captures and diagnostics bundles.

Every text that leaves hostd through this module passes the redactor, so Wi-Fi
passwords, session tokens, CSRF values and the live pairing code never appear
in responses, logs or bundles.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import tarfile
import time
from pathlib import Path
from typing import Awaitable, Callable

from . import __version__
from .config import Config
from .util import Redactor, atomic_write_json, evidence_id, run as run_cmd, tail_lines, wall

log = logging.getLogger("retroboot.diagnostics")

LOG_SOURCES = ("hostd", "guest", "presenter", "kernel", "launcher", "lifecycle")
UNITS = {"hostd": "retroboot-hostd.service", "presenter": "retroboot-presenter.service",
         "launcher": "retroboot-guest.service"}

Shot = Callable[[str], Awaitable[bool]]


class Diagnostics:
    def __init__(self, cfg: Config, redactor: Redactor):
        self.cfg = cfg
        self.paths = cfg.paths
        self.redact = redactor
        self.guest_shot: Shot | None = None       # writes a PNG of the guest framebuffer to a path
        self.composed_shot: Shot | None = None    # writes a PNG of what the display shows
        self.status_provider: Callable[[], Awaitable[dict]] | None = None
        self.guest_stuck: Callable[[], bool] | None = None          # QMP not answering
        self.last_guest_frame: Callable[[], bytes | None] | None = None
        self.last_evidence: str | None = None

    async def logs(self, source: str, lines: int) -> list[str]:
        lines = max(1, min(int(lines), 5000))
        if source == "guest":
            out = tail_lines(self.paths.serial_log, lines)
        elif source == "lifecycle":
            out = tail_lines(self.paths.lifecycle_log, lines)
        elif source in UNITS or source == "kernel":
            out = []
            if self.cfg.journal:
                argv = ["journalctl", "--no-pager", "-o", "short-iso", "-n", str(lines)]
                argv += ["-k"] if source == "kernel" else ["-u", UNITS[source]]
                rc, text, _ = await run_cmd(argv, timeout=15)
                if rc == 0:
                    out = text.splitlines()
            if not out and source == "hostd":
                out = tail_lines(self.paths.hostd_log, lines)
            if not out and source == "launcher":
                out = tail_lines(self.paths.logs / "qemu-stderr.log", lines)
        else:
            raise ValueError("unknown log source")
        return [self.redact(l) for l in out[-lines:]]

    async def _shots(self, dest: Path, budget_s: float | None = None) -> dict:
        """Best-effort screenshots.  With a budget (force actions), the whole capture is
        bounded and the guest screendump is skipped when the emulator does not answer
        QMP (the last good guest frame is saved instead)."""
        res: dict = {}
        loop = asyncio.get_running_loop()
        deadline = None if budget_s is None else loop.time() + budget_s
        for name, fn in (("guest", self.guest_shot), ("composed", self.composed_shot)):
            if fn is None:
                continue
            if name == "guest" and self.guest_stuck and self.guest_stuck():
                last = self.last_guest_frame() if self.last_guest_frame else None
                if last:
                    (dest / "screen-guest-last.png").write_bytes(last)
                res[name] = False
                res["guest_note"] = "emulator not answering QMP; " + ("last good frame saved" if last else "no frame")
                continue
            timeout = 20.0 if deadline is None else max(0.2, deadline - loop.time())
            try:
                ok = await asyncio.wait_for(fn(str(dest / f"screen-{name}.png")), timeout)
            except asyncio.TimeoutError:
                ok = False
                res[f"{name}_note"] = f"timed out after {timeout:.1f} s"
            except Exception as exc:  # screenshots are best effort
                ok = False
                log.info("%s screenshot failed: %s", name, exc)
            res[name] = bool(ok)
        return res

    async def capture_evidence(self, kind: str, reason: str, extra: dict | None = None,
                               budget_s: float | None = None) -> str:
        """Small evidence directory for a failure or force action.  Returns the evidence id."""
        eid = evidence_id()
        d = self.paths.evidence / eid
        d.mkdir(parents=True, exist_ok=True)
        shots = await self._shots(d, budget_s)
        for src, name, n in ((self.paths.lifecycle_log, "lifecycle.jsonl", 400),
                             (self.paths.serial_log, "guest-serial.log", 2000),
                             (self.paths.logs / "qemu-stderr.log", "qemu-stderr.log", 400)):
            text = "\n".join(self.redact(l) for l in tail_lines(src, n))
            (d / name).write_text(text + "\n")
        try:
            (d / "status.json").write_text(self.redact(self.paths.status_json.read_text()))
        except OSError:
            pass
        atomic_write_json(d / "summary.json", {"id": eid, "kind": kind, "reason": reason, "created": wall(),
                                                "hostd_version": __version__, "screenshots": shots,
                                                "extra": self.redact.obj(extra or {})})
        self.last_evidence = eid
        return eid

    async def bundle(self, settings_snapshot: dict, extra: dict, progress=None) -> dict:
        """Create logs/bundles/<id>.tar.gz and return its descriptor."""
        bid = evidence_id()
        self.paths.bundles.mkdir(parents=True, exist_ok=True)
        tmpdir = self.paths.bundles / f".{bid}"
        tmpdir.mkdir()
        try:
            if progress:
                progress(0.1, "Capturing screens…")
            shots = await self._shots(tmpdir)
            if progress:
                progress(0.3, "Collecting logs…")
            members: dict[str, bytes] = {}
            for src in ("hostd", "presenter", "launcher", "kernel"):
                members[f"logs/{src}.log"] = ("\n".join(await self.logs(src, 3000)) + "\n").encode()
            members["logs/guest-serial.log"] = ("\n".join(self.redact(l) for l in tail_lines(self.paths.serial_log, 20000)) + "\n").encode()
            members["logs/qemu-stderr.log"] = ("\n".join(self.redact(l) for l in tail_lines(self.paths.logs / "qemu-stderr.log", 2000)) + "\n").encode()
            members["lifecycle.jsonl"] = ("\n".join(self.redact(l) for l in tail_lines(self.paths.lifecycle_log, 5000)) + "\n").encode()
            status = {}
            if self.status_provider:
                try:
                    status = await self.status_provider()
                except Exception:
                    status = {"error": "status unavailable"}
            members["status.json"] = json.dumps(self.redact.obj(status), indent=2).encode()
            members["settings.json"] = json.dumps(self.redact.obj(settings_snapshot), indent=2).encode()
            try:
                members["guest/argv.json"] = self.redact(self.paths.argv_json.read_text()).encode()
                members["guest/status.json"] = self.redact(self.paths.status_json.read_text()).encode()
            except OSError:
                pass
            for f in sorted(self.paths.evidence.glob("*/summary.json"))[-20:]:
                members[f"evidence/{f.parent.name}.json"] = self.redact(f.read_text()).encode()
            # Startup Details reports (rb-bootdiag, ARCHITECTURE 16): written on this
            # computer while Mac OS had not appeared yet; the three newest.
            for f in sorted((self.paths.logs / "startup-details").glob("*.txt"))[-3:]:
                try:
                    members[f"startup-details/{f.name}"] = self.redact(f.read_text(errors="replace")).encode()
                except OSError:
                    pass
            manifest = {"id": bid, "created": wall(), "hostd_version": __version__, "screenshots": shots,
                        "contents": sorted(list(members) + [f"screen-{k}.png" for k, v in shots.items() if v]),
                        **self.redact.obj(extra)}
            members["manifest.json"] = json.dumps(manifest, indent=2).encode()
            if progress:
                progress(0.7, "Writing the bundle…")
            out = self.paths.bundles / f"{bid}.tar.gz"
            tmp_out = self.paths.bundles / f".{bid}.tar.gz"
            with tarfile.open(tmp_out, "w:gz") as tf:
                for name, data in members.items():
                    ti = tarfile.TarInfo(f"{bid}/{name}")
                    ti.size = len(data)
                    ti.mtime = int(time.time())
                    ti.mode = 0o644
                    tf.addfile(ti, io.BytesIO(data))
                for k, v in shots.items():
                    if v:
                        tf.add(str(tmpdir / f"screen-{k}.png"), arcname=f"{bid}/screen-{k}.png")
            os.chmod(tmp_out, 0o600)
            os.replace(tmp_out, out)
            self._prune()
            return {"id": bid, "bytes": out.stat().st_size, "created": manifest["created"]}
        finally:
            for f in tmpdir.glob("*"):
                f.unlink()
            tmpdir.rmdir()

    def bundle_path(self, bid: str) -> Path | None:
        import re
        if not re.fullmatch(r"RB-\d{8}-\d{6}-[0-9A-F]{4}", bid or ""):
            return None
        p = self.paths.bundles / f"{bid}.tar.gz"
        return p if p.is_file() else None

    def _prune(self, keep: int = 10) -> None:
        files = sorted(self.paths.bundles.glob("RB-*.tar.gz"))
        for f in files[:-keep]:
            try:
                f.unlink()
            except OSError:
                pass
