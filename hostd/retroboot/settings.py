"""Settings store (ARCHITECTURE section 5).

``settings.json`` holds::

    {"schema": 1, "revision": 7, "desired": {...}, "applied": {...},
     "applied_revision": 6, "known_good": {"revision": 6, "values": {...}},
     "pending_confirm": null}

Writes validate the complete candidate, write a temp file, fsync, rename and
fsync the directory.  The previous file is kept as ``settings.json.bak``.  The
known-good values are also mirrored to ``state/known-good.json`` so a corrupt
store can recover through ``.bak`` -> known-good -> defaults.  Edits carry the
revision they were based on; a stale revision raises :class:`StaleRevision`.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Callable

from . import schema
from .util import atomic_write_json, mono, wall

log = logging.getLogger("retroboot.settings")


# Settings schema migrations between neighbouring schema versions (ARCHITECTURE 13).
# MIGRATIONS_UP[n] turns a schema-n store into schema n+1; MIGRATIONS_DOWN[n] turns a
# schema-n store into schema n-1 (used before "Go Back to the Previous Version" switches
# to a build with an older schema).  Each takes and returns the whole store dict
# (desired/applied/known_good values) and must not depend on the device.  Adding keys
# needs no migration: older builds drop unknown keys and newer ones fill defaults.
MIGRATIONS_UP: dict[int, Callable[[dict], dict]] = {}
MIGRATIONS_DOWN: dict[int, Callable[[dict], dict]] = {}


def migrate(data: dict, target: int) -> dict | None:
    """The store converted to schema ``target`` (None when no migration path exists)."""
    data = json.loads(json.dumps(data))
    cur = data.get("schema")
    if type(cur) is not int:
        return None
    while cur != target:
        step = MIGRATIONS_UP.get(cur) if cur < target else MIGRATIONS_DOWN.get(cur)
        if step is None:
            return None
        data = step(data)
        cur = cur + 1 if cur < target else cur - 1
        data["schema"] = cur
    return data


class StaleRevision(Exception):
    def __init__(self, current: int):
        super().__init__(f"Settings changed elsewhere (current revision {current}).")
        self.current = current


class NothingToConfirm(Exception):
    pass


def _valid_state(data: Any) -> dict:
    if not isinstance(data, dict) or data.get("schema") != schema.SCHEMA_VERSION:
        raise ValueError("unsupported settings schema")
    rev = data.get("revision")
    if type(rev) is not int or rev < 0:
        raise ValueError("bad revision")
    desired = schema.validate_complete(data.get("desired"))
    applied = schema.validate_complete(data.get("applied"))
    ar = data.get("applied_revision")
    if type(ar) is not int or ar < 0 or ar > rev:
        raise ValueError("bad applied_revision")
    kg = data.get("known_good")
    if kg is not None:
        if not isinstance(kg, dict) or type(kg.get("revision")) is not int:
            raise ValueError("bad known_good")
        kg = {"revision": kg["revision"], "values": schema.validate_complete(kg.get("values")),
              "at": kg.get("at")}
    pc = data.get("pending_confirm")
    if pc is not None:
        if not isinstance(pc, dict) or not isinstance(pc.get("previous"), dict):
            raise ValueError("bad pending_confirm")
    return {"schema": schema.SCHEMA_VERSION, "revision": rev, "desired": desired,
            "applied": applied, "applied_revision": ar, "known_good": kg,
            "pending_confirm": pc}


class SettingsStore:
    def __init__(self, path: Path, known_good_path: Path, image_defaults: dict | None = None):
        self.path = Path(path)
        self.bak = self.path.with_name(self.path.name + ".bak")
        self.known_good_path = Path(known_good_path)
        self.defaults = schema.defaults(image_defaults)
        self.state: dict = {}
        self.notice: dict | None = None
        self.fresh = False
        # While a new RetroBoot runs on trial (Software Update), keep writing the store
        # in the schema the previous build reads, so going back needs no conversion.
        self.hold_schema: int | None = None

    # -- persistence -----------------------------------------------------
    def _default_state(self, values: dict | None = None) -> dict:
        values = dict(values or self.defaults)
        return {"schema": schema.SCHEMA_VERSION, "revision": 0, "desired": dict(values),
                "applied": dict(values), "applied_revision": 0,
                "known_good": None, "pending_confirm": None}

    def _read(self, path: Path) -> dict:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and type(data.get("schema")) is int and data["schema"] < schema.SCHEMA_VERSION:
            # Written by an older build (before an update): migrate forward.
            data = migrate(data, schema.SCHEMA_VERSION) or data
        if isinstance(data, dict):
            # Schema migration: fill new keys from defaults, drop removed ones.
            for part in ("desired", "applied"):
                if isinstance(data.get(part), dict):
                    data[part] = schema.complete_with_defaults(data[part], self.defaults)
            kg = data.get("known_good")
            if isinstance(kg, dict) and isinstance(kg.get("values"), dict):
                kg["values"] = schema.complete_with_defaults(kg["values"], self.defaults)
        return _valid_state(data)

    def load(self) -> dict:
        """Load with corruption recovery.  Sets ``self.notice`` when recovery happened."""
        self.notice = None
        if not self.path.exists() and not self.bak.exists():
            self.fresh = True
            self.state = self._default_state()
            kg = self._read_known_good()
            if kg is not None:
                # Data partition kept known-good but lost the store.
                self.state = self._default_state(kg["values"])
                self.state["known_good"] = kg
                self._set_notice("known_good", "missing")
            self.save()
            return self.state
        try:
            self.state = self._read(self.path)
            return self.state
        except (OSError, ValueError) as exc:
            reason = str(exc)
            log.error("settings store unreadable: %s", reason)
            self._preserve_corrupt()
        try:
            self.state = self._read(self.bak)
            self._set_notice("bak", reason)
        except (OSError, ValueError):
            kg = self._read_known_good()
            if kg is not None:
                self.state = self._default_state(kg["values"])
                self.state["known_good"] = kg
                self._set_notice("known_good", reason)
            else:
                self.state = self._default_state()
                self._set_notice("defaults", reason)
        # Recovered state starts a new revision so stale clients get 409.
        self.state["revision"] += 1
        self.state["applied_revision"] = min(self.state["applied_revision"], self.state["revision"])
        self.save(backup=False)
        return self.state

    def _set_notice(self, source: str, reason: str) -> None:
        self.notice = {"code": "settings_recovered", "source": source, "reason": reason[:200],
                       "at": wall(),
                       "message": {
                           "bak": "Settings were damaged and have been restored from the previous copy.",
                           "known_good": "Settings were damaged and have been restored to the last working configuration.",
                           "defaults": "Settings were damaged and have been reset to their defaults.",
                       }[source]}

    def _preserve_corrupt(self) -> None:
        if self.path.exists():
            dest = self.path.with_name(f"{self.path.name}.corrupt-{int(os.path.getmtime(self.path))}")
            try:
                shutil.copy2(self.path, dest)
            except OSError:
                pass

    def _read_known_good(self) -> dict | None:
        try:
            data = json.loads(self.known_good_path.read_text())
            return {"revision": int(data["revision"]),
                    "values": schema.validate_complete(
                        schema.complete_with_defaults(data["values"], self.defaults))}
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def save(self, backup: bool = True) -> None:
        state = _valid_state(self.state)  # never persist an invalid candidate
        if backup and self.path.exists():
            try:
                atomic_write_json(self.bak, json.loads(self.path.read_text()), 0o644)
            except (OSError, ValueError):
                pass
        if self.hold_schema is not None and self.hold_schema < schema.SCHEMA_VERSION:
            state = migrate(state, self.hold_schema) or state
        atomic_write_json(self.path, state, 0o644)
        kg = state.get("known_good")
        if kg is not None:
            atomic_write_json(self.known_good_path, kg, 0o600)


    def save_now(self) -> None:
        """Flush the current state (before a restart into another RetroBoot version)."""
        if self.state:
            self.save(backup=False)

    def write_schema_version(self, target: int) -> bool:
        """Rewrite the store in an older schema for a build that reads only that one.
        False when no migration path exists (the caller restores that build's own copy)."""
        if target >= schema.SCHEMA_VERSION:
            return True
        down = migrate(_valid_state(self.state), target)
        if down is None:
            return False
        atomic_write_json(self.path, down, 0o644)
        kg = down.get("known_good")
        if kg is not None:
            atomic_write_json(self.known_good_path, kg, 0o600)
        return True


class SettingsService:
    """Desired/applied bookkeeping on top of the store.  Single-threaded (asyncio)."""

    def __init__(self, store: SettingsStore):
        self.store = store
        self.listeners: list[Callable[[str], None]] = []
        self.last_confirm: dict | None = None   # {"result": confirmed|reverted, "keys", "at", "origin"}

    @property
    def state(self) -> dict:
        return self.store.state

    @property
    def revision(self) -> int:
        return self.state["revision"]

    @property
    def desired(self) -> dict:
        return self.state["desired"]

    @property
    def applied(self) -> dict:
        return self.state["applied"]

    def get(self, key: str) -> Any:
        return self.applied[key]

    def pending_keys(self) -> list[str]:
        return sorted(k for k in self.desired if self.desired[k] != self.applied.get(k))

    def pending_by_policy(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {schema.LIVE: [], schema.GUEST_RESTART: [], schema.HOST_REBOOT: []}
        for k in self.pending_keys():
            out[schema.BY_KEY[k].apply].append(k)
        return out

    def _changed(self, what: str) -> None:
        for fn in list(self.listeners):
            try:
                fn(what)
            except Exception:  # pragma: no cover - listeners must not break the store
                log.exception("settings listener failed")

    def check_revision(self, base: Any) -> None:
        if type(base) is not int or base != self.revision:
            raise StaleRevision(self.revision)

    def patch(self, base_revision: Any, changes: Any, ctx: schema.ValidationContext | None) -> int:
        self.check_revision(base_revision)
        candidate = schema.validate_changes(self.desired, changes, ctx)
        if candidate == self.desired:
            return self.revision
        self.state["desired"] = candidate
        self.state["revision"] += 1
        self.store.save()
        self._changed("desired")
        return self.revision

    def mark_applied(self, keys: list[str]) -> None:
        if not keys:
            return
        for k in keys:
            self.state["applied"][k] = self.desired[k]
        if not self.pending_keys():
            self.state["applied_revision"] = self.revision
        self.store.save()
        self._changed("applied")

    def set_desired_and_applied(self, values: dict, ctx: schema.ValidationContext | None = None) -> int:
        """Internal changes (first-time setup, import) that take effect immediately."""
        candidate = schema.validate_changes(self.desired, values, ctx)
        self.state["desired"] = candidate
        for k in values:
            self.state["applied"][k] = candidate[k]
        self.state["revision"] += 1
        if not self.pending_keys():
            self.state["applied_revision"] = self.revision
        self.store.save()
        self._changed("applied")
        return self.revision

    # -- timed confirmation ---------------------------------------------
    def begin_confirm(self, previous: dict, seconds: int) -> dict:
        pc = {"keys": sorted(previous), "previous": dict(previous), "revision": self.revision,
              "seconds": seconds, "started_wall": wall(), "deadline_mono": mono() + seconds}
        self.state["pending_confirm"] = pc
        self.store.save()
        self._changed("pending_confirm")
        return pc

    @property
    def pending_confirm(self) -> dict | None:
        return self.state.get("pending_confirm")

    def confirm(self) -> None:
        if not self.pending_confirm:
            raise NothingToConfirm()
        self.state["pending_confirm"] = None
        self._advance_known_good()
        self.store.save()
        self._changed("confirmed")

    def revert_pending_confirm(self) -> dict:
        """Restore the values recorded before an unconfirmed change.  Returns them."""
        pc = self.pending_confirm
        if not pc:
            return {}
        previous = pc["previous"]
        for k, v in previous.items():
            self.state["desired"][k] = v
            self.state["applied"][k] = v
        self.state["pending_confirm"] = None
        self.state["revision"] += 1
        if not self.pending_keys():
            self.state["applied_revision"] = self.revision
        self.store.save()
        self._changed("reverted")
        return previous

    # -- known good ------------------------------------------------------
    def _advance_known_good(self) -> None:
        self.state["known_good"] = {"revision": self.state["applied_revision"],
                                    "values": dict(self.applied), "at": wall()}

    def mark_known_good(self) -> bool:
        """Called after a guest ran with the applied values (or a confirmed change)."""
        if self.pending_confirm:
            return False
        kg = self.state.get("known_good")
        if kg and kg["values"] == self.applied:
            return False
        self._advance_known_good()
        self.store.save()
        self._changed("known_good")
        return True

    def restore_known_good(self) -> bool:
        kg = self.state.get("known_good")
        if not kg:
            return False
        self.state["desired"] = dict(kg["values"])
        self.state["applied"] = dict(kg["values"])
        self.state["pending_confirm"] = None
        self.state["revision"] += 1
        self.state["applied_revision"] = self.revision
        self.store.save()
        self._changed("applied")
        return True

    def pending_confirm_public(self) -> dict | None:
        return self.snapshot(None, with_schema=False)["pending_confirm"]

    def snapshot(self, ctx: schema.ValidationContext | None = None, with_schema: bool = True) -> dict:
        pc = self.pending_confirm
        pc_public = None
        if pc:
            import datetime as _dt
            remaining = max(0.0, pc["deadline_mono"] - mono())
            deadline = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=remaining)) \
                .isoformat(timespec="milliseconds").replace("+00:00", "Z")
            pc_public = {"keys": pc["keys"], "seconds": pc["seconds"], "revision": pc["revision"],
                         "remaining": round(remaining, 1), "seconds_left": round(remaining, 1), "deadline": deadline,
                         "values": {k: self.applied[k] for k in pc["keys"]}, "previous": pc["previous"]}
        kg = self.state.get("known_good")
        return {
            "schema_version": schema.SCHEMA_VERSION,
            "schema": schema.describe(ctx) if with_schema else None,
            "revision": self.revision,
            "desired": self.desired,
            "applied": self.applied,
            "applied_revision": self.state["applied_revision"],
            "pending": self.pending_keys(),
            "pending_by_policy": self.pending_by_policy(),
            "pending_confirm": pc_public,
            "known_good_revision": kg["revision"] if kg else None,
            "known_good": kg,
            "last_confirm": self.last_confirm,
            "notice": self.store.notice,
        }
