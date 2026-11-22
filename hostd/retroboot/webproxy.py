"""Modern Web Helper: hostd's side of the translating web proxy (webproxy/, ARCHITECTURE §11).

The helper is its own unprivileged service (``retroboot-webproxy.service``,
DynamicUser, sandboxed).  hostd owns the user's choices and only:

* writes ``/run/retroboot/webproxy.json`` from the applied settings
  (``network.web_proxy*``) and the guest user's uid;
* starts the unit when ``network.web_proxy`` is on, reloads it (SIGHUP) when
  another helper setting changes, stops it when the setting is off;
* reports its state (``GET /api/v1/status`` and ``/api/v1/network``,
  ``web_proxy``) from ``systemctl is-active``, the helper's status file and
  whether the guest's traffic redirect (nftables table) is in place.

hostd restarts never stop the helper (like the guest).  Toggling takes effect
at once, without restarting Mac OS: Mac OS keeps its own settings, and while
the helper runs its plain-web and DNS traffic is redirected to it.

Development and tests use ``webproxy_runner: "subprocess"`` (a child process,
no traffic redirect) or ``"off"``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import pwd
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .util import atomic_write_json, run as run_cmd

if TYPE_CHECKING:
    from .hostd import Hostd

log = logging.getLogger("retroboot.webproxy")

UNIT = "retroboot-webproxy.service"
TABLE = "retroboot_webproxy"
HOME = "http://system7today.com/"

# Start and search pages built into Mac OS 9.2.1 and its browsers, read from a clean 9.2.1
# install (Internet Preferences; the resources of Internet Explorer 5.0 and Netscape
# Communicator 4.77; their favorites and bookmarks).  "search" means the search engine
# chosen in network.web_proxy_search, with the query passed on when the address has one.
# Real sites (apple.com, microsoft.com) are not redirected: the helper translates them.
# Paths: "" = the whole site, "/" = its front page only, "/x" = that folder or page.
DEFAULT_REDIRECTS: tuple[dict, ...] = (
    {"host": "livepage.apple.com", "path": "", "to": HOME},             # IC + Netscape home, favorites
    {"host": "livepage.apple.com", "path": "/search", "to": "search"},  # Netscape bookmark
    {"host": "www.excite.com", "path": "/apple", "to": "search"},       # IC search page
    {"host": "www.msn.com", "path": "/", "to": HOME},                   # IE 5 "Use Default" home
    {"host": "home.microsoft.com", "path": "", "to": HOME},             # IE 5 start pages
    {"host": "home.microsoft.com", "path": "/access/autosearch.asp", "to": "search"},  # IE address-bar search
    {"host": "home.microsoft.com", "path": "/access/allinone.asp", "to": "search"},
    {"host": "home.microsoft.com", "path": "/search/search.asp", "to": "search"},
    {"host": "ie.search.msn.com", "path": "", "to": "search"},          # IE 5 Search Assistant
    {"host": "related.msn.com", "path": "", "to": HOME},                # IE "What's Related" (retired)
    {"host": "www.iechannelguide.com", "path": "", "to": HOME},         # IE channel guide (retired)
    {"host": "home.netscape.com", "path": "", "to": HOME},              # Netscape 4.77 home, start, first run
    {"host": "home.netscape.com", "path": "/bookmark/4_77/tsearch.html", "to": "search"},  # Search button
    {"host": "keyword.netscape.com", "path": "", "to": "search"},       # Netscape Internet Keywords
    {"host": "www-rl.netscape.com", "path": "", "to": HOME},            # Netscape "What's Related" (retired)
    {"host": "guide.netscape.com", "path": "", "to": HOME},             # Netscape Guide (retired)
)
SEARCH_CHOICES = ("frogfind", "bing", "duckduckgo", "wiby", "none")
SEARCH_LABELS = {"frogfind": "FrogFind (made for vintage computers)", "bing": "Bing",
                 "duckduckgo": "DuckDuckGo (HTML version)", "wiby": "Wiby (the classic web)",
                 "none": "Keep the original search pages"}


def guest_uid(user: str | None) -> int | None:
    try:
        return pwd.getpwnam(user).pw_uid if user else None
    except KeyError:
        return None


class WebHelper:
    """``app.webproxy``."""

    def __init__(self, app: "Hostd"):
        self.app = app
        self.cfg = app.cfg
        self.runner = getattr(app.cfg, "webproxy_runner", "systemd")
        self.proc: asyncio.subprocess.Process | None = None
        self.state = "off"
        self.intercepting: bool | None = None
        self.error: str | None = None
        self.stats: dict = {}
        self.changed_at: float | None = None
        self._lock = asyncio.Lock()
        self._monitor: asyncio.Task | None = None

    # -- paths ---------------------------------------------------------------------
    @property
    def lib(self) -> Path:
        override = getattr(self.cfg, "webproxy_path", None)
        return Path(override) if override else self.app.paths.lib / "webproxy"

    @property
    def config_path(self) -> Path:
        return self.app.paths.run / "webproxy.json"

    @property
    def status_path(self) -> Path:
        if self.runner == "systemd":
            return self.app.paths._r("run/retroboot-webproxy") / "status.json"
        return self.app.paths.run / "webproxy-status.json"

    def available(self) -> bool:
        return self.runner != "off" and (self.lib / "rbwebproxy" / "__main__.py").is_file()

    # -- configuration ---------------------------------------------------------------
    def wanted(self) -> bool:
        return (self.available() and self.app.mode == "normal"
                and self.app.settings.applied.get("network.web_proxy", "on") == "on")

    def build_config(self) -> dict[str, Any]:
        a = self.app.settings.applied
        uid = guest_uid(self.cfg.run_as_user)
        return {
            "port": int(getattr(self.cfg, "webproxy_port", 8080)),
            "listen": ["127.0.0.1"],
            "dns": True,
            "dns_port": int(getattr(self.cfg, "webproxy_dns_port", 8053)),
            "dns_listen": ["127.0.0.1", "::1"],
            "intercept": self.runner == "systemd",
            "intercept_uid": uid,
            "intercept_user": self.cfg.run_as_user,
            # Only QEMU's user may use the helper (a lab QEMU running as root: anyone local).
            "guest_uids": [uid] if uid else [],
            "view": a.get("network.web_proxy_view", "auto"),
            "homepages": bool(a.get("network.web_proxy_homepages", True)),
            "redirects": list(a.get("network.web_proxy_redirects") or []),
            "search": a.get("network.web_proxy_search", "frogfind"),
            "allow_lan": bool(a.get("network.web_proxy_lan", True)),
            "status_path": str(self.status_path),
            "log_level": "INFO",
        }

    def write_config(self) -> None:
        atomic_write_json(self.config_path, self.build_config(), 0o644)

    # -- control -------------------------------------------------------------------------
    async def start(self) -> None:
        """hostd start: bring the helper to the applied setting; then watch it."""
        if not self.available():
            self.state = "unavailable"
            return
        await self.sync("hostd-start")
        self._monitor = asyncio.get_running_loop().create_task(self._watch())

    async def stop(self) -> None:
        """hostd stop: the systemd unit keeps running (like the guest); a dev child stops."""
        if self._monitor:
            self._monitor.cancel()
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                self.proc.kill()

    async def sync(self, reason: str = "settings") -> dict:
        """Apply the settings now: write the config, then start, reload or stop."""
        async with self._lock:
            if not self.available():
                self.state = "unavailable"
                return self.public()
            try:
                self.write_config()
            except OSError as e:
                self.error = f"configuration not written: {e}"
                return self.public()
            want = self.wanted()
            self.error = None
            if self.runner == "systemd":
                await self._sync_systemd(want)
            else:
                await self._sync_child(want)
            self.changed_at = time.time()
            self.app.lifecycle.event("web_proxy_sync", reason=reason, on=want, state=self.state)
            await self.refresh()
            return self.public()

    async def _sync_systemd(self, want: bool) -> None:
        active = await self._unit_state()
        if want:
            if active in ("active", "reloading"):
                rc, _, err = await run_cmd(["systemctl", "reload", UNIT], timeout=20)
            else:
                await run_cmd(["systemctl", "reset-failed", UNIT], timeout=10)
                rc, _, err = await run_cmd(["systemctl", "start", UNIT], timeout=40)
            if rc != 0:
                self.error = (err or "").strip()[:200] or "The Modern Web Helper did not start."
        else:
            if active not in ("inactive", "failed", "unknown"):
                await run_cmd(["systemctl", "stop", UNIT], timeout=20)
            # The unit removes its redirect when it stops; make sure no stale table can
            # send Mac OS's web traffic to a helper that is off (no-op when absent).
            await run_cmd(["nft", "delete", "table", "inet", TABLE], timeout=10)

    async def _sync_child(self, want: bool) -> None:
        running = self.proc is not None and self.proc.returncode is None
        if want and running:
            self.proc.send_signal(1)                 # SIGHUP: re-read the configuration
        elif want:
            env = dict(os.environ, PYTHONPATH=str(self.lib), PYTHONDONTWRITEBYTECODE="1")
            logf = open(self.app.paths.logs / "webproxy.log", "ab")
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "rbwebproxy", "--config", str(self.config_path),
                    stdin=asyncio.subprocess.DEVNULL, stdout=logf, stderr=logf, env=env,
                    start_new_session=True)
            finally:
                logf.close()
            await asyncio.sleep(0.5)
        elif running:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                self.proc.kill()

    async def _unit_state(self) -> str:
        rc, out, _ = await run_cmd(["systemctl", "is-active", UNIT], timeout=10)
        return out.strip() or "unknown"

    # -- state ---------------------------------------------------------------------------
    async def refresh(self) -> None:
        want = self.wanted()
        if self.runner == "systemd":
            st = await self._unit_state()
            self.state = {"active": "running", "reloading": "running", "activating": "starting",
                          "deactivating": "stopping", "failed": "failed"}.get(st, "off")
            if self.state == "running":
                rc, _, _ = await run_cmd(["nft", "list", "table", "inet", TABLE], timeout=10)
                self.intercepting = rc == 0
            else:
                self.intercepting = False
        else:
            running = self.proc is not None and self.proc.returncode is None
            self.state = "running" if running else ("failed" if want and self.proc else "off")
            self.intercepting = None
        if want and self.state == "off":
            self.state = "failed"
        try:
            data = json.loads(self.status_path.read_text())
            self.stats = {"counts": data.get("counts") or {}, "started": data.get("started"),
                          "last_error": data.get("last_error"), "version": data.get("version")}
        except (OSError, ValueError):
            self.stats = {}

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(15)
            try:
                await self.refresh()
                if self.wanted() and self.state == "failed" and self.runner == "systemd":
                    # systemd's own restarts gave up (start limit): try again calmly.
                    await self.sync("watchdog")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("web helper check failed")

    def public(self) -> dict[str, Any]:
        a = self.app.settings.applied
        return {"available": self.available(), "enabled": a.get("network.web_proxy", "on") == "on",
                "state": self.state, "intercepting": self.intercepting, "error": self.error,
                "view": a.get("network.web_proxy_view", "auto"),
                "homepages": bool(a.get("network.web_proxy_homepages", True)),
                "search": a.get("network.web_proxy_search", "frogfind"),
                "address": f"10.0.2.2:{int(getattr(self.cfg, 'webproxy_port', 8080))}",
                "stats": self.stats}
