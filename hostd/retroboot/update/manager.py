"""Software Update in hostd (ARCHITECTURE section 13).

State on RBDATA (``/var/lib/retroboot/update/``):

``state.json``   staged update, the last result, history, armed boot id, last check
``incoming/``    bundles being received (upload, USB copy, download); removed after use
``settings/<build_id>/``  the settings as that build left them (taken before switching away)

Jobs (exclusive resource ``update``): ``update.install`` (verify, write the other
slot while Mac OS keeps running), ``update.check`` (update server), ``update.restart``
("Restart to Update": clean Mac OS shut down, state copied again, one GRUB try,
reboot), ``update.rollback`` ("Go Back to the Previous Version").

A new slot boots once on trial.  hostd confirms it ("commit") when Mac OS shows
genuine content (the supervisor's first-content signal), or when the classic panel
has been up for ``update_panel_health_s`` with Mac OS deliberately not started.  The
slot guard (``guard.py``) reboots a trial that is not confirmed in time; GRUB then
starts the previous slot.  hostd also goes back at once when Mac OS cannot start on
the new version.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import ssl
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import schema
from ..jobs import Job, JobFailed
from ..util import atomic_write_json, read_json, wall
from . import slotenv
from .bundle import Bundle, BundleError, compatibility, describe_version, trusted_keys, version_key
from .writer import (RealOps, SlotWriter, WriteError, _restore_or_clear_tryboot, activate_rpi_slot,
                     arm_rpi_tryboot, commit_rpi_slot, release_info, sync_esp)

if TYPE_CHECKING:  # pragma: no cover
    from ..hostd import Hostd

log = logging.getLogger("retroboot.update")

MiB = 1024 ** 2
MAX_BUNDLE = 2 * 1024 ** 3
RESERVE = 512 * MiB
FEED_FORMAT = "retroboot-feed/1"
MAX_FEED = 256 * 1024
HISTORY = 20
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def https_url(url: Any, allow_http: bool = False) -> bool:
    from urllib.parse import urlsplit
    if not isinstance(url, str) or len(url) > 2048:
        return False
    u = urlsplit(url)
    ok_scheme = u.scheme == "https" or (allow_http and u.scheme == "http")
    return ok_scheme and bool(u.hostname) and u.username is None and u.password is None and not u.fragment


UNAVAILABLE_PI = ("Software Update isn't available on this Raspberry Pi test build yet. "
                  "To try a newer build, write its SD card image.")
UNAVAILABLE_GEMINI = ("Software Update isn't available on this Gemini PDA test build yet. "
                      "To try a newer build, install its boot image and system again.")


class UpdateManager:
    def __init__(self, app: "Hostd"):
        self.app = app
        self.cfg = app.cfg
        self.paths = app.paths
        self.dir = self.paths.data / "update"
        self.state_path = self.dir / "state.json"
        self.incoming = self.dir / "incoming"
        self.work = self.paths.run / "update"
        self.esp = Path(self.cfg.update_esp)
        self.keys_dir = Path(self.cfg.update_keys_dir) if self.cfg.update_keys_dir else self.paths.lib / "update" / "keys"
        self.ops = RealOps(self.cfg.update_bwlimit_kbps)
        self.layout: dict | None = None
        self.boot_mode: str = "grub"
        self.unavailable: str | None = "Software Update is starting."
        self.env: dict = dict(slotenv.DEFAULTS)
        self.boot: dict = {"kind": "normal"}
        self.state: dict = {}
        self.release: dict = {}
        self.other: dict | None = None     # release of the other slot when it holds a working system
        self.committed = False
        self.trial = False
        self.usb: list[dict] = []
        self.usb_seen: set[str] = set()
        self._tasks: list[asyncio.Task] = []
        self._panel_since: float | None = None

    # -- persistence ----------------------------------------------------------------
    def _load_state(self) -> dict:
        try:
            data = read_json(self.state_path, 1024 * 1024)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save(self) -> None:
        atomic_write_json(self.state_path, self.state, 0o644)

    def _history(self, **rec) -> None:
        rec.setdefault("at", wall())
        self.state.setdefault("history", []).append(rec)
        self.state["history"] = self.state["history"][-HISTORY:]
        self.state["last_result"] = rec
        self.save()

    # -- startup --------------------------------------------------------------------
    def _probe(self) -> None:
        """Blocking: layout, environment, release (worker thread)."""
        self.release = release_info(Path(self.cfg.root))
        if self.app.mode != "normal":
            self.unavailable = "Software Update isn't available while the installer is running."
            return
        plat = getattr(self.app, "platform", None)
        if plat is not None and not plat.has_grub and not self.cfg.update_layout:
            if getattr(plat, "is_raspberry_pi", False) and not getattr(plat, "is_zero2w", False):
                pass
            else:
                self.unavailable = UNAVAILABLE_GEMINI if getattr(plat, "is_gemini", False) else UNAVAILABLE_PI
                return
        if self.cfg.update_layout:
            self.layout = dict(self.cfg.update_layout)
        else:
            try:
                self.layout = slotenv.layout(self.cfg.sysfs_path)
            except slotenv.SlotError as exc:
                self.unavailable = "This RetroBoot wasn't installed with room for updates."
                log.info("update layout unavailable: %s", exc)
                return
        try:
            os.listdir(self.esp)     # triggers the /boot/efi automount
        except OSError:
            pass
        if (self.esp / "grub" / "grub.cfg").is_file() and not (
                plat is not None and getattr(plat, "is_raspberry_pi", False)):
            self.boot_mode = "grub"
        elif (self.esp / "config.txt").is_file() and (
                plat is None or getattr(plat, "is_raspberry_pi", False)
                or getattr(self.cfg, "update_mode", None) == "rpi"):
            self.boot_mode = "rpi"
        else:
            self.unavailable = "RetroBoot's startup files couldn't be found."
            return
        env_name = slotenv.RPI_ENV_NAME if self.boot_mode == "rpi" else None
        self.env, _src = slotenv.read_env(self.esp, env_name=env_name)
        self.unavailable = None

    async def start(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state = self._load_state()
        await asyncio.to_thread(self._probe)
        if self.unavailable:
            self.app.lifecycle.event("update_unavailable", reason=self.unavailable)
            return
        cmdline = slotenv.read_cmdline(self.cfg.cmdline_path)
        booted = slotenv.booted_slot(cmdline, self.layout)
        slotted = (slotenv.rpi_cfg_is_slotted(self.esp) if self.boot_mode == "rpi"
                   else slotenv.grub_cfg_is_slotted(self.esp))
        self.boot = slotenv.boot_state(self.env, cmdline, booted, slotted, mode=self.boot_mode)
        armed = self.state.get("armed") or {}
        if self.boot["kind"] in ("not-tried", "fell-back") and armed.get("boot_id") == self.app.lifecycle.boot_id:
            self.boot = {"kind": "armed", "slot": booted, "active": self.env["rb_active"]}
        kind = self.boot["kind"]
        st = self.staged()
        if st and st.get("status") == "writing":
            # Power was lost (or hostd stopped) while the other slot was being written. The
            # environment never offered that slot, so this is the version that ran before.
            self.state["staged"] = None
            self._history(kind="install", result="failed", version=st.get("version"), build_id=st.get("build_id"),
                          message=f"Installing RetroBoot {st.get('version')} was interrupted. RetroBoot is "
                                  "unchanged; install the update again.")
            self.app.lifecycle.event("update_install_interrupted", build=st.get("build_id"))
        self.app.lifecycle.event("update_boot", kind=kind, slot=booted, env=self.env,
                                 version=self.release.get("version"), build=self.release.get("build_id"))
        # Every boot but a trial runs the committed slot (after a fallback too).
        self.committed = kind != "trial"
        if kind == "trial":
            self.trial = True
            self.app.store.hold_schema = self._disk_schema()
            self._tasks.append(asyncio.get_running_loop().create_task(self._watch_trial(), name="update-trial"))
        elif kind == "fell-back":
            await self._fell_back()
        elif kind == "not-tried":
            env_name = slotenv.RPI_ENV_NAME if self.boot_mode == "rpi" else None
            await asyncio.to_thread(slotenv.update_env, self.esp, env_name=env_name, rb_next="", rb_tries="0")
            self.env["rb_next"] = ""
            self._history(kind="not-tried", result="failed", version=(self.state.get("staged") or {}).get("version"),
                          message="This computer's startup software couldn't record the trial start, so the "
                                  "update was not tried. RetroBoot is unchanged.")
            self.app.lifecycle.event("update_not_tried")
        else:
            self._reconcile_normal(booted)
        await self._refresh_other()
        self._tasks.append(asyncio.get_running_loop().create_task(self._usb_poll(), name="update-usb"))

    def _disk_schema(self) -> int | None:
        try:
            return int(read_json(self.paths.settings).get("schema"))
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def _reconcile_normal(self, booted: str | None) -> None:
        armed = self.state.get("armed")
        if armed and armed.get("slot") == booted and self.env.get("rb_active") == booted:
            # Committed on an earlier boot whose state write was lost: record it now.
            self.state.pop("armed", None)
            self.state["staged"] = None
            self.save()

    async def _refresh_other(self) -> None:
        if self.unavailable or not self.layout or self.layout.get("current") not in slotenv.SLOTS:
            self.other = None
            return
        oslot = slotenv.other(self.layout["current"])
        if self.env.get("rb_other_ok") != "1" or self.env.get("rb_active") != self.layout["current"]:
            self.other = None
            return
        cfg = slotenv.read_slot_cfg(self.esp, oslot) or {}
        cached = (self.state.get("slots") or {}).get(oslot)
        if cached and cached.get("cfg_info") == cfg.get("info"):
            self.other = cached
            return
        writer = self._writer()
        info = await asyncio.to_thread(writer.slot_release, oslot)
        if info:
            info["slot"] = oslot
            info["cfg_info"] = cfg.get("info")
            self.state.setdefault("slots", {})[oslot] = info
            self.save()
        self.other = info or None

    def _writer(self) -> SlotWriter:
        return SlotWriter(self.esp, self.layout or {}, self.work, ops=self.ops, running_root=Path(self.cfg.root),
                          data_dir=self.paths.data, mode=self.boot_mode)

    # -- trial boots -------------------------------------------------------------------
    async def _watch_trial(self) -> None:
        sup = self.app.supervisor
        try:
            while not self.committed:
                await asyncio.sleep(1.0)
                if sup.content_seen and sup.state in ("running", "stopping") and sup.session:
                    await self.commit("guest_content")
                    return
                panel = self.app.panel
                deliberate = (sup.state in ("stopped", "paused") and sup.failure is None and panel.visible
                              and self.app.presenter.connected.is_set())
                if deliberate:
                    self._panel_since = self._panel_since or time.monotonic()
                    if time.monotonic() - self._panel_since >= self.cfg.update_panel_health_s:
                        await self.commit("panel")
                        return
                else:
                    self._panel_since = None
                if sup.failure is not None and sup.state in ("stopped", "failed") and not sup.active:
                    await self.go_back_after_failure(sup.failure.get("message") or "Mac OS could not start.")
                    return
        except asyncio.CancelledError:
            pass

    async def commit(self, signal: str) -> None:
        if self.committed or getattr(self, "_committing", False):
            return
        self._committing = True
        slot = self.boot.get("slot")

        env_name = slotenv.RPI_ENV_NAME if self.boot_mode == "rpi" else None

        def work():
            env = slotenv.update_env(self.esp, env_name=env_name, rb_active=slot, rb_next="", rb_tries="0",
                                     rb_other_ok="1")
            changed = []
            try:
                changed = (commit_rpi_slot(self.esp, slot) if self.boot_mode == "rpi"
                           else sync_esp(self.esp, Path(self.cfg.root)))
            except Exception as exc:   # the slot is committed; the old ESP files still boot it
                log.error("ESP update after commit failed: %s", exc)
            slotenv.mark_healthy(str(self.paths.run), {"slot": slot, "at": wall(), "signal": signal})
            return env, changed
        self.env, changed = await asyncio.to_thread(work)
        self.trial = False
        self.app.store.hold_schema = None
        staged = self.state.get("staged") or {}
        prev = (self.state.get("armed") or {}).get("from") or {}
        self.state.pop("armed", None)
        self.state["staged"] = None
        self._history(kind="update", result="installed", version=self.release.get("version"),
                      build_id=self.release.get("build_id"), previous=prev, signal=signal, esp_changed=changed,
                      message=f"RetroBoot was updated to {self.release.get('version')}.")
        self.app.lifecycle.event("update_committed", slot=slot, signal=signal, esp_changed=changed,
                                 version=self.release.get("version"), build=self.release.get("build_id"),
                                 staged=staged.get("build_id"))
        await self._refresh_other()
        self.committed = True

    async def go_back_after_failure(self, why: str) -> None:
        """A trial that cannot run Mac OS: restart into the previous slot now (GRUB's try is spent)."""
        self.app.lifecycle.event("update_trial_failed", why=why, slot=self.boot.get("slot"))
        prev = (self.state.get("armed") or {}).get("from") or {}
        name = f"RetroBoot {prev.get('version')}" if prev.get("version") else "the previous version"
        await self.app.panel.show_update_progress("RetroBoot couldn't start Mac OS with this update.",
                                                  f"Going back to {name}…", None)
        await asyncio.sleep(3.0)
        await self.app.supervisor.reboot_host("update trial failed")

    async def _fell_back(self) -> None:
        failed = self.boot.get("failed")
        env_name = slotenv.RPI_ENV_NAME if self.boot_mode == "rpi" else None
        await asyncio.to_thread(slotenv.update_env, self.esp, env_name=env_name, rb_next="", rb_tries="0")
        if self.boot_mode == "rpi":
            await asyncio.to_thread(_restore_or_clear_tryboot, self.esp)
        self.env["rb_next"] = ""
        staged = self.state.get("staged") or {}
        armed = self.state.pop("armed", None) or {}
        if staged:
            staged["status"] = "failed"
        version = staged.get("label") or staged.get("version") or "the update"
        cur = describe_version(self.release.get("version", ""), self.release.get("build_id"))
        self._history(kind="update", result="failed", version=staged.get("version"), build_id=staged.get("build_id"),
                      slot=failed,
                      message=f"RetroBoot {version} didn't finish starting, so this computer went back to "
                              f"RetroBoot {cur}. Mac OS and your documents were not changed.")
        restored = await asyncio.to_thread(self.restore_settings_if_newer)
        self.app.lifecycle.event("update_fell_back", failed_slot=failed, staged=staged.get("build_id"),
                                 armed_boot=armed.get("boot_id"), settings_restored=restored)

    # -- settings across versions --------------------------------------------------------
    def snapshot_settings(self) -> str | None:
        bid = self.release.get("build_id")
        if not bid:
            return None
        dest = self.dir / "settings" / re.sub(r"[^A-Za-z0-9._-]", "_", bid)
        dest.mkdir(parents=True, exist_ok=True)
        for src in (self.paths.settings, self.paths.settings.with_name("settings.json.bak"),
                    self.paths.state / "known-good.json"):
            if src.is_file():
                shutil.copy2(src, dest / src.name)
        snaps = sorted((self.dir / "settings").iterdir(), key=lambda p: p.stat().st_mtime)
        for old in snaps[:-4]:
            shutil.rmtree(old, ignore_errors=True)
        return str(dest)

    def restore_settings_if_newer(self) -> bool:
        """Back on an older build after a newer one ran: if the store has a schema this
        build can't read, put back the copy taken when this build switched away."""
        disk = self._disk_schema()
        if disk is None or disk <= schema.SCHEMA_VERSION:
            return False
        bid = self.release.get("build_id") or ""
        snap = self.dir / "settings" / re.sub(r"[^A-Za-z0-9._-]", "_", bid)
        if not (snap / "settings.json").is_file():
            return False
        shutil.copy2(snap / "settings.json", self.paths.settings)
        if (snap / "known-good.json").is_file():
            shutil.copy2(snap / "known-good.json", self.paths.state / "known-good.json")
        self.app.store.load()
        return True

    def prepare_settings_for(self, target_schema: int | None) -> str:
        """Before switching to a build with an older settings schema."""
        if target_schema is None or target_schema >= schema.SCHEMA_VERSION:
            return "shared"
        if self.app.store.write_schema_version(target_schema):
            return "migrated"
        return "snapshot"

    # -- public state ---------------------------------------------------------------------
    def staged(self) -> dict | None:
        s = self.state.get("staged")
        return s if isinstance(s, dict) else None

    def public(self) -> dict:
        cur = self.release
        st = self.staged()
        other = self.other
        out = {
            "available": self.unavailable is None, "reason": self.unavailable,
            "current": {"version": cur.get("version"), "build_id": cur.get("build_id"), "flavor": cur.get("flavor"),
                        "label": describe_version(cur.get("version", "?"), cur.get("build_id")),
                        "slot": (self.layout or {}).get("current")},
            "boot": {"kind": self.boot.get("kind"), "committed": self.committed},
            "staged": None, "previous": None,
            "last_result": self.state.get("last_result"),
            "history": list(reversed(self.state.get("history") or []))[:10],
            "usb": [{k: v for k, v in u.items() if not k.startswith("_")} for u in self.usb],
            "check": self._check_public(),
            "url": self.feed_url(),
            "can_install": self.unavailable is None and self.committed,
        }
        if st:
            out["staged"] = {k: st.get(k) for k in ("version", "build_id", "flavor", "notes", "status", "at",
                                                    "slot", "label")}
        if other and other.get("version"):
            newer = version_key(other.get("version"), other.get("build_id")) > \
                version_key(cur.get("version", "0"), cur.get("build_id"))
            out["previous"] = {"version": other.get("version"), "build_id": other.get("build_id"),
                               "label": describe_version(other.get("version"), other.get("build_id")),
                               "newer": newer, "update_capable": other.get("update_capable", True),
                               "slot": other.get("slot")}
        return out

    def _check_public(self) -> dict | None:
        """The last check; an update it found counts only while it is newer than this RetroBoot
        (after installing it, the stored check still names it)."""
        check = self.state.get("check")
        found = (check or {}).get("found") if isinstance(check, dict) else None
        if found:
            cur = self.release
            if version_key(found.get("version", "0"), found.get("build_id")) <= \
                    version_key(cur.get("version", "0"), cur.get("build_id")):
                return dict(check, found=None)
        return check

    def notices(self) -> list[dict]:
        out = []
        last = self.state.get("last_result") or {}
        recent = last.get("at") and last.get("result") in ("failed", "installed", "rolled-back") and \
            not self.state.get("last_result_seen")
        if recent and last.get("result") == "failed" and last.get("kind") != "install":
            out.append({"id": "update-failed", "level": "warning", "message": last.get("message")})
        st = self.staged()
        if st and st.get("status") == "ready":
            out.append({"id": "update-ready", "level": "info",
                        "message": f"RetroBoot {st.get('version')} is ready to install. Restart to update."})
        return out

    def require_available(self) -> None:
        if self.unavailable:
            raise JobFailed(self.unavailable, None, "update-unavailable")
        if not self.committed:
            raise JobFailed("RetroBoot is still checking the version it just started. Try again in a minute.",
                            None, "update-pending")

    # -- install (upload, USB, download) -----------------------------------------------------
    def new_incoming(self) -> Path:
        self.incoming.mkdir(parents=True, exist_ok=True)
        for old in self.incoming.glob("*.rbu"):
            if time.time() - old.stat().st_mtime > 3600:
                old.unlink(missing_ok=True)
        return self.incoming / f"{secrets.token_hex(6)}.rbu"

    def space_for(self, nbytes: int) -> bool:
        self.incoming.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(self.incoming).free >= nbytes + RESERVE

    def install_job(self, source: dict, origin: str = "web", allow_older: bool = False,
                    allow_flavor: bool = False) -> Job:
        """source: {"kind": "file", "path"} (incoming, removed afterwards) | {"kind": "usb", "id"} |
        {"kind": "feed"} (the update the last check found)."""
        async def body(job: Job):
            self.require_available()
            path: Path | None = None
            try:
                if source["kind"] == "file":
                    path = Path(source["path"])
                elif source["kind"] == "usb":
                    path = await self._copy_from_usb(source["id"], job)
                elif source["kind"] == "feed":
                    path = await self._download(job)
                else:
                    raise JobFailed("Unknown update source.", None, "invalid")
                return await self._install(job, path, origin, allow_older, allow_flavor)
            finally:
                if path is not None and path.parent == self.incoming:
                    path.unlink(missing_ok=True)
        return self.app.jobs.start("update.install", body, resources=("update",), params={"source": source["kind"]})

    async def _install(self, job: Job, path: Path, origin: str, allow_older: bool, allow_flavor: bool) -> dict:
        job.update(0.0, "Checking the update…")
        keys = trusted_keys(self.keys_dir)
        try:
            bundle = await asyncio.to_thread(Bundle, path)
            manifest = await asyncio.to_thread(bundle.verify, keys, lambda f, m: job.update(0.15 * f, m))
            compatibility(manifest, self.release, self._disk_schema() or schema.SCHEMA_VERSION,
                          allow_older=allow_older, allow_flavor=allow_flavor, allow_same=allow_older)
        except BundleError as exc:
            self.app.lifecycle.event("update_rejected", code=exc.code, origin=origin)
            raise JobFailed(exc.message, None, exc.code)
        label = describe_version(manifest["version"], manifest["build_id"])
        self.app.lifecycle.event("update_install_start", version=manifest["version"], build=manifest["build_id"],
                                 key=bundle.key_id, origin=origin)
        self.state["staged"] = {"version": manifest["version"], "build_id": manifest["build_id"],
                                "flavor": manifest["flavor"], "notes": str(manifest.get("notes") or "")[:2000],
                                "status": "writing", "at": wall(), "label": label,
                                "settings_schema": manifest["settings_schema"]}
        self.save()
        self.other = None
        writer = self._writer()
        t0 = time.monotonic()
        try:
            res = await asyncio.to_thread(writer.write, bundle,
                                          lambda f, m: job.update(0.15 + 0.85 * f if f is not None else None, m))
        except (WriteError, BundleError, slotenv.SlotError) as exc:
            self.state["staged"] = None
            self._history(kind="install", result="failed", version=manifest["version"],
                          build_id=manifest["build_id"], message=getattr(exc, "message", str(exc)))
            self.app.lifecycle.event("update_install_failed", error=str(exc), code=getattr(exc, "code", None))
            raise JobFailed(f"RetroBoot {manifest['version']} couldn't be installed: "
                            f"{getattr(exc, 'message', str(exc))}", None, getattr(exc, "code", "write-failed"))
        except Exception as exc:
            self.state["staged"] = None
            self.save()
            self.app.lifecycle.event("update_install_failed", error=f"{type(exc).__name__}: {exc}")
            raise
        env_name = slotenv.RPI_ENV_NAME if self.boot_mode == "rpi" else None
        self.env, _ = await asyncio.to_thread(slotenv.read_env, self.esp, env_name=env_name)
        self.state["staged"].update(status="ready", slot=res["slot"], written_s=round(time.monotonic() - t0, 1),
                                    converted_grub=res["converted_grub"])
        self.save()
        self.app.lifecycle.event("update_staged", slot=res["slot"], version=manifest["version"],
                                 build=manifest["build_id"], seconds=round(time.monotonic() - t0, 1),
                                 carried=len(res["carried"]), converted_grub=res["converted_grub"])
        job.outcome = f"RetroBoot {manifest['version']} is ready. Restart to update."
        return {"staged": self.public()["staged"], "restart_required": True}

    # -- restart to update ----------------------------------------------------------------------
    def restart_job(self, origin: str = "web") -> Job:
        async def body(job: Job):
            self.require_available()
            st = self.staged()
            if not st or st.get("status") not in ("ready", "failed"):
                raise JobFailed("There is no update ready to install.", None, "nothing-staged")
            target = st["slot"]
            sup = self.app.supervisor
            if sup.active:
                job.update(0.1, "Shutting down Mac OS…")
                # An application that holds Mac OS's Shut Down past the wait fails this job;
                # when Mac OS finishes it, Restart to Update goes on (supervisor late_stop).
                await sup.job_clean(job, "shutdown", poweroff=False, purpose="host-reboot",
                                    resume=lambda: self.restart_job(origin) if self.staged() else None)
            # The screen stays black from Mac OS's shut down to the restart, as for Restart
            # RetroBoot: the last steps take well under a second, and a progress window
            # shown that briefly would only flash (outer capture, image/reports/update-*.md).
            job.update(0.8, f"Installing RetroBoot {st['version']}…")
            writer = self._writer()
            carried = await asyncio.to_thread(writer.resync_state, target)
            snap = await asyncio.to_thread(self.snapshot_settings)
            await asyncio.to_thread(self.app.store.save_now)
            self.state["armed"] = {"slot": target, "boot_id": self.app.lifecycle.boot_id, "at": wall(),
                                   "from": {"version": self.release.get("version"),
                                            "build_id": self.release.get("build_id"),
                                            "slot": (self.layout or {}).get("current")}}
            st["status"] = "restarting"
            self.save()
            env_name = slotenv.RPI_ENV_NAME if self.boot_mode == "rpi" else None
            if self.boot_mode == "rpi":
                await asyncio.to_thread(arm_rpi_tryboot, self.esp, target)
            self.env = await asyncio.to_thread(slotenv.update_env, self.esp, env_name=env_name, rb_next=target,
                                               rb_tries="1")
            self.app.lifecycle.event("update_armed", slot=target, version=st["version"], build=st["build_id"],
                                     carried=len(carried), settings_snapshot=bool(snap), origin=origin,
                                     mode=self.boot_mode)
            job.update(0.95, "Restarting…")
            await sup.reboot_host(f"update to {st['version']}", arg=("0 tryboot" if self.boot_mode == "rpi" else None))
            return {"slot": target, "version": st["version"]}
        return self.app.jobs.start("update.restart", body, resources=("guest", "host", "update"),
                                   preemptible=True)

    def rollback_job(self, origin: str = "web") -> Job:
        async def body(job: Job):
            self.require_available()
            other = self.other
            if not other or not other.get("version"):
                raise JobFailed("There is no previous version to go back to.", None, "no-previous")
            target = other["slot"]
            sup = self.app.supervisor
            if sup.active:
                job.update(0.1, "Shutting down Mac OS…")
                await sup.job_clean(job, "shutdown", poweroff=False, purpose="host-reboot",
                                    resume=lambda: self.rollback_job(origin))
            job.update(0.8, f"Switching to RetroBoot {other['version']}…")
            writer = self._writer()
            carried = await asyncio.to_thread(writer.resync_state, target)
            await asyncio.to_thread(self.snapshot_settings)
            how = await asyncio.to_thread(self.prepare_settings_for, other.get("settings_schema"))
            env_name = slotenv.RPI_ENV_NAME if self.boot_mode == "rpi" else None
            if self.boot_mode == "rpi":
                await asyncio.to_thread(activate_rpi_slot, self.esp, target)
            self.env = await asyncio.to_thread(slotenv.update_env, self.esp, env_name=env_name, rb_active=target,
                                               rb_next="", rb_tries="0", rb_other_ok="1")
            st = self.staged()
            if st:
                self.state["staged"] = None
            self._history(kind="rollback", result="rolled-back", version=other["version"],
                          build_id=other.get("build_id"), previous={"version": self.release.get("version"),
                                                                    "build_id": self.release.get("build_id")},
                          message=f"RetroBoot went back to {other['version']}.")
            self.app.lifecycle.event("update_rollback", slot=target, version=other["version"],
                                     build=other.get("build_id"), carried=len(carried), settings=how, origin=origin)
            job.update(0.95, "Restarting…")
            await sup.reboot_host(f"go back to {other['version']}")
            return {"slot": target, "version": other["version"]}
        return self.app.jobs.start("update.rollback", body, resources=("guest", "host", "update"),
                                   preemptible=True)

    def discard_staged(self) -> bool:
        st = self.staged()
        if not st or st.get("status") == "writing":
            return False
        self.state["staged"] = None
        self.save()
        self.app.lifecycle.event("update_discarded", build=st.get("build_id"))
        return True

    # -- USB ------------------------------------------------------------------------------------
    async def scan_usb(self) -> list[dict]:
        if self.unavailable or not self.cfg.allow_network_tools:
            self.usb = []
            return self.usb
        found = []
        for part in await self.app.archives.removable_partitions():
            name = re.sub(r"[^A-Za-z0-9_-]", "_", part.get("name") or "usb")
            label = part.get("label") or "USB disk"
            for b in await self._with_usb(part, lambda mp: self._list_bundles(Path(mp))):
                b.update(id=f"{name}:{b['_file']}", disk=label, _part=part)
                found.append(b)
        self.usb = found
        return found

    @staticmethod
    def _list_bundles(mp: Path) -> list[dict]:
        out = []
        for d in (mp, mp / "RetroBoot"):
            if not d.is_dir():
                continue
            for f in sorted(d.glob("*.rbu"))[:10]:
                try:
                    b = Bundle(f)
                    m = json.loads(b.manifest_bytes)
                    out.append({"_file": str(f.relative_to(mp)), "name": f.name, "bytes": f.stat().st_size,
                                "version": str(m.get("version"))[:20], "build_id": str(m.get("build_id"))[:80],
                                "flavor": str(m.get("flavor"))[:20],
                                "label": describe_version(str(m.get("version")), str(m.get("build_id")))})
                except (BundleError, OSError, ValueError):
                    continue
        return out

    async def _with_usb(self, part: dict, fn):
        mp = part.get("mountpoint")
        target = None
        if not mp:
            target = self.work / "usb" / re.sub(r"[^A-Za-z0-9_-]", "_", part.get("name") or "usb")
            target.mkdir(parents=True, exist_ok=True)
            from ..util import run as run_cmd
            rc, _, err = await run_cmd(["mount", "-o", "ro,nosuid,nodev,noexec", part["path"], str(target)], timeout=20)
            if rc != 0:
                log.info("USB mount %s failed: %s", part.get("path"), err.strip())
                return []
            mp = str(target)
        try:
            return await asyncio.to_thread(fn, mp)
        finally:
            if target is not None:
                from ..util import run as run_cmd
                await run_cmd(["umount", str(target)], timeout=20)

    async def _copy_from_usb(self, usb_id: str, job: Job) -> Path:
        await self.scan_usb()
        item = next((u for u in self.usb if u["id"] == usb_id), None)
        if item is None:
            raise JobFailed("That update is no longer on the USB disk.", None, "not-found")
        if not self.space_for(item["bytes"]):
            raise JobFailed("There isn't enough free space to copy the update.", None, "no-space")
        dest = self.new_incoming()

        def copy(mp):
            src = Path(mp) / item["_file"]
            total = src.stat().st_size
            done = 0
            with open(src, "rb") as fi, open(dest, "xb") as fo:
                while True:
                    b = fi.read(4 * MiB)
                    if not b:
                        break
                    fo.write(b)
                    done += len(b)
                    job.update(0.0, f"Copying the update from the USB disk… {done // MiB} of {total // MiB} MB")
                fo.flush()
                os.fsync(fo.fileno())
            return done == total
        job.update(0.0, "Copying the update from the USB disk…")
        ok = await self._with_usb(item["_part"], copy)
        if ok is not True:
            dest.unlink(missing_ok=True)
            raise JobFailed("The update couldn't be copied from the USB disk.", None, "usb-copy")
        return dest

    async def _usb_poll(self) -> None:
        """Notice USB disks with an update as they are plugged in (cheap lsblk every 10 s)."""
        if not self.cfg.allow_network_tools or self.unavailable:
            return
        try:
            while True:
                await asyncio.sleep(self.cfg.update_usb_poll_s)
                parts = await self.app.archives.removable_partitions()
                key = {p.get("path") for p in parts}
                if key != self.usb_seen:
                    self.usb_seen = key
                    if parts and not self.app.jobs.holder("update"):
                        found = await self.scan_usb()
                        if found:
                            self.app.lifecycle.event("update_usb_found", count=len(found),
                                                     versions=[f["version"] for f in found])
                    else:
                        self.usb = []
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("USB update poll failed")

    # -- update server ----------------------------------------------------------------------------
    def _ssl(self):
        ctx = ssl.create_default_context(cafile=self.cfg.update_ca_file or None)
        return ctx

    async def _get(self, url: str, max_bytes: int):
        import aiohttp
        if not https_url(url, self.cfg.update_allow_http):
            raise JobFailed("The update server address must start with https://.", None, "insecure-url")
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=60)
        session = aiohttp.ClientSession(timeout=timeout, auto_decompress=False,
                                        connector=aiohttp.TCPConnector(ssl=self._ssl()))
        from ..labauth import headers_for
        try:
            resp = await session.get(url, allow_redirects=False,
                                     headers=headers_for(getattr(self.app, "lab_auth", None), url))
        except Exception:
            await session.close()
            raise JobFailed("The update server couldn't be reached. Check the network connection.", None, "network")
        if resp.status != 200:
            resp.release()
            await session.close()
            raise JobFailed(f"The update server answered with an error (HTTP {resp.status}).", None, "network")
        return session, resp

    def check_job(self, origin: str = "web") -> Job:
        async def body(job: Job):
            url = self.feed_url()
            if not url:
                raise JobFailed("No update server is set. Enter its address under Software Update.", None, "no-url")
            job.update(None, "Checking for updates…")
            session, resp = await self._get(url, MAX_FEED)
            try:
                # read(n) may return only the first network fragment: read to the end, bounded.
                raw = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    raw.extend(chunk)
                    if len(raw) > MAX_FEED:
                        break
            finally:
                resp.release()
                await session.close()
            if len(raw) > MAX_FEED:
                raise JobFailed("The update server's list is too large.", None, "invalid-feed")
            try:
                feed = json.loads(raw)
            except ValueError:
                raise JobFailed("The update server's list isn't in a format RetroBoot understands.", None,
                                "invalid-feed")
            found = self.pick_from_feed(feed)
            self.state["check"] = {"at": wall(), "url": url, "found": found}
            self.save()
            self.app.lifecycle.event("update_check", found=(found or {}).get("build_id"), origin=origin)
            job.outcome = (f"RetroBoot {found['version']} is available." if found else
                           f"RetroBoot {self.release.get('version')} is up to date.")
            return {"found": found}
        return self.app.jobs.start("update.check", body, resources=("update",))

    def feed_url(self) -> str:
        """The update server: setting update.url, else the RetroBoot downloads update list
        when this computer is signed in to RetroBoot downloads (labauth). Either way it is
        checked only when asked."""
        url = self.app.settings.applied.get("update.url") or ""
        auth = getattr(self.app, "lab_auth", None)
        return url or (auth.feed_url if auth is not None else "")

    def pick_from_feed(self, feed: Any) -> dict | None:
        if not isinstance(feed, dict) or feed.get("format") != FEED_FORMAT or not isinstance(feed.get("updates"), list):
            raise JobFailed("The update server's list isn't in a format RetroBoot understands.", None, "invalid-feed")
        best = None
        cur = version_key(self.release.get("version", "0.0.0"), self.release.get("build_id"))
        for u in feed["updates"][:50]:
            if not isinstance(u, dict) or u.get("flavor") != self.release.get("flavor"):
                continue
            if not (isinstance(u.get("version"), str) and isinstance(u.get("build_id"), str)
                    and https_url(u.get("url"), self.cfg.update_allow_http)
                    and isinstance(u.get("sha256"), str) and SHA_RE.fullmatch(u["sha256"])
                    and type(u.get("bytes")) is int and 0 < u["bytes"] <= MAX_BUNDLE):
                continue
            if version_key(self.release.get("version", "0.0.0")) < version_key(str(u.get("min_version") or "0.0.0")):
                continue
            k = version_key(u["version"], u["build_id"])
            if k > cur and (best is None or k > version_key(best["version"], best["build_id"])):
                best = u
        if best is None:
            return None
        return {"version": best["version"][:20], "build_id": best["build_id"][:80], "url": best["url"],
                "sha256": best["sha256"], "bytes": best["bytes"], "notes": str(best.get("notes") or "")[:2000],
                "label": describe_version(best["version"], best["build_id"])}

    async def _download(self, job: Job) -> Path:
        found = (self.state.get("check") or {}).get("found")
        if not found:
            raise JobFailed("Check for updates first.", None, "nothing-found")
        if not self.space_for(found["bytes"]):
            raise JobFailed("There isn't enough free space to download the update.", None, "no-space")
        dest = self.new_incoming()
        import hashlib
        h = hashlib.sha256()
        got = 0
        session, resp = await self._get(found["url"], found["bytes"])
        try:
            with open(dest, "xb") as f:
                last = 0.0
                async for chunk in resp.content.iter_chunked(1024 * 1024):
                    got += len(chunk)
                    if got > found["bytes"]:
                        raise JobFailed("The download is larger than expected.", None, "integrity")
                    f.write(chunk)
                    h.update(chunk)
                    if time.monotonic() - last > 0.3:
                        last = time.monotonic()
                        job.update(None, f"Downloading RetroBoot {found['version']}… "
                                         f"{got // MiB} of {found['bytes'] // MiB} MB")
                f.flush()
                os.fsync(f.fileno())
        except JobFailed:
            dest.unlink(missing_ok=True)
            raise
        except Exception:
            dest.unlink(missing_ok=True)
            raise JobFailed("The download could not finish. Check the network connection.", None, "network")
        finally:
            resp.release()
            await session.close()
        if got != found["bytes"] or h.hexdigest() != found["sha256"]:
            dest.unlink(missing_ok=True)
            raise JobFailed("The downloaded update didn't pass its integrity check. Try again.", None, "integrity")
        return dest

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
