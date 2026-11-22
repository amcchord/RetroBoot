"""How hostd starts and observes guest-launcher.

* ``SystemdRunner`` (production): ``systemctl start retroboot-guest.service``;
  completion is observed through ``status.json`` (session-matched) plus the
  unit's active state.  hostd restarts never stop the guest unit, and a running
  guest is adopted on hostd start.
* ``SubprocessRunner`` (development/tests): runs ``python3 -m retroboot.launcher``
  as a child with the same ``--root``, exercising the identical launcher code.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from pathlib import Path

from .config import Config
from .util import mono, run as run_cmd

log = logging.getLogger("retroboot.guestrun")

PKG_PARENT = str(Path(__file__).resolve().parent.parent)


def read_status(path: Path) -> dict | None:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def pid_alive(pid) -> bool:
    """The process exists (hostd runs as root, so /proc shows the guest user's processes)."""
    return isinstance(pid, int) and pid > 0 and os.path.exists(f"/proc/{pid}")


class GuestHandle:
    def __init__(self, session: str, status_path: Path):
        self.session = session
        self.status_path = status_path
        self.pid: int | None = None

    def status(self) -> dict | None:
        st = read_status(self.status_path)
        if st and st.get("session") == self.session:
            if st.get("pid"):
                self.pid = st["pid"]
            return st
        return None

    def exit_from_status(self, fallback_code: int | None) -> dict:
        st = self.status()
        if st and st.get("state") == "exited" and isinstance(st.get("exit"), dict):
            return st["exit"]
        return {"code": 1 if fallback_code in (None, 0) else fallback_code, "reason": "crash",
                "mono": mono(), "detail": "launcher ended without a status record"}

    async def wait(self) -> dict:
        raise NotImplementedError

    async def stop(self) -> None:
        raise NotImplementedError


class SubprocessHandle(GuestHandle):
    def __init__(self, session: str, status_path: Path, proc: asyncio.subprocess.Process):
        super().__init__(session, status_path)
        self.proc = proc

    async def wait(self) -> dict:
        rc = await self.proc.wait()
        return self.exit_from_status(rc)

    async def stop(self) -> None:
        if self.proc.returncode is None:
            try:
                self.proc.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass


class SubprocessRunner:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    async def start(self, session: str) -> GuestHandle:
        paths = self.cfg.paths
        env = dict(os.environ)
        env["PYTHONPATH"] = PKG_PARENT + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        out = open(paths.logs / "launcher.log", "ab")
        try:
            proc = await asyncio.create_subprocess_exec(
                self.cfg.python, "-m", "retroboot.launcher", "--root", str(paths.root),
                stdin=asyncio.subprocess.DEVNULL, stdout=out, stderr=out, env=env, start_new_session=True)
        finally:
            out.close()
        h = SubprocessHandle(session, paths.status_json, proc)
        h.pid = proc.pid
        return h

    async def adopt(self) -> GuestHandle | None:
        return None


class SystemdHandle(GuestHandle):
    # wait() reads status.json every 0.25 s.  It asks systemd about the unit (a backstop for
    # a launcher that ended without writing "exited") once a second while the emulator's
    # process is unknown or gone, but only every ALIVE_ASK_TICKS while it runs: each ask
    # starts a systemctl process, which once a second cost a few percent of a Gemini
    # PDA's little core for the whole session.
    ALIVE_ASK_TICKS = 60            # 15 s

    def __init__(self, session: str, status_path: Path, unit: str):
        super().__init__(session, status_path)
        self.unit = unit

    async def _active(self) -> str:
        rc, out, _ = await run_cmd(["systemctl", "is-active", self.unit], timeout=10)
        return out.strip() or "unknown"

    def _ask_systemd(self, tick: int, inactive_since: float | None) -> bool:
        if tick % 4:
            return False
        if inactive_since is not None or not pid_alive(self.pid):
            return True
        return tick % self.ALIVE_ASK_TICKS == 0

    async def wait(self) -> dict:
        inactive_since = None
        tick = 0
        while True:
            st = self.status()
            if st and st.get("state") == "exited":
                return st["exit"]
            tick += 1
            if self._ask_systemd(tick, inactive_since):
                state = await self._active()
                if state in ("inactive", "failed"):
                    inactive_since = inactive_since or mono()
                    if mono() - inactive_since > 2.0:
                        rc, out, _ = await run_cmd(["systemctl", "show", "-p", "ExecMainStatus", "--value",
                                                    self.unit], timeout=10)
                        try:
                            code = int(out.strip())
                        except ValueError:
                            code = 1
                        return self.exit_from_status(code)
                else:
                    inactive_since = None
            await asyncio.sleep(0.25)

    async def stop(self) -> None:
        await run_cmd(["systemctl", "stop", "--no-block", self.unit], timeout=15)


class SystemdRunner:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.unit = cfg.guest_unit

    async def start(self, session: str) -> GuestHandle:
        await run_cmd(["systemctl", "reset-failed", self.unit], timeout=10)
        rc, _, err = await run_cmd(["systemctl", "start", "--no-block", self.unit], timeout=30)
        if rc != 0:
            raise RuntimeError(f"cannot start {self.unit}: {err.strip()[:200]}")
        return SystemdHandle(session, self.cfg.paths.status_json, self.unit)

    async def adopt(self) -> GuestHandle | None:
        st = read_status(self.cfg.paths.status_json)
        if not st or st.get("state") not in ("starting", "running"):
            return None
        rc, out, _ = await run_cmd(["systemctl", "is-active", self.unit], timeout=10)
        if out.strip() not in ("active", "activating"):
            return None
        h = SystemdHandle(st["session"], self.cfg.paths.status_json, self.unit)
        h.pid = st.get("pid")
        return h


def make_runner(cfg: Config):
    return SystemdRunner(cfg) if cfg.guest_runner == "systemd" else SubprocessRunner(cfg)
