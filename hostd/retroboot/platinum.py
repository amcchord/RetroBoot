"""Platinum asset bundle: generated from the guest's own Mac OS disk, served to the web client.

The bundle (fonts, icons, chrome sprites; platinum/README.md) is derived from
Apple software on the user's disk, so it is never shipped: a private image may
include one built at image time, otherwise hostd builds it once, before a
launch, while the guest is stopped and its disk is idle.  The build only reads
the disk.  Failure or absence is harmless: the web client and the classic panel
fall back to RetroBoot's original fonts and art.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
import sys
import time
from pathlib import Path

log = logging.getLogger("retroboot.platinum")
BUILD_TIMEOUT_S = 60.0


class PlatinumAssets:
    def __init__(self, out: Path, lib_path: Path, enabled: bool = True, lifecycle=None):
        self.out = Path(out)
        self.lib_path = Path(lib_path)
        self.enabled = enabled
        self.lifecycle = lifecycle
        self.attempted = False
        self._mod = None

    def present(self) -> bool:
        return (self.out / "manifest.json").is_file()

    def current(self) -> bool:
        """A bundle exists and the installed rbplatinum made it (a newer one adds files)."""
        try:
            made_by = json.loads((self.out / "manifest.json").read_text()).get("generator")
        except (OSError, ValueError, AttributeError):
            return False
        try:
            version = getattr(self._module(), "__version__", None)
        except Exception:  # noqa: BLE001 - no builder installed: keep the bundle there is
            return True
        return version is None or made_by == f"rbplatinum {version}"

    def _module(self):
        if self._mod is None:
            p = str(self.lib_path)
            if self.lib_path.is_dir() and p not in sys.path:
                sys.path.insert(0, p)
            self._mod = importlib.import_module("rbplatinum.bundle")
        return self._mod

    async def ensure(self, disk: Path) -> dict:
        """Build the bundle from `disk` once per hostd run if it is missing or older than the
        installed rbplatinum (guest stopped)."""
        if not self.enabled or self.attempted or self.current():
            return {"result": "skipped"}
        self.attempted = True
        t0 = time.monotonic()
        try:
            mod = self._module()
            # Replace a bundle from an older rbplatinum (atomic swap in rbplatinum.bundle.build).
            manifest = await asyncio.wait_for(asyncio.to_thread(mod.build, str(disk), str(self.out), True),
                                              BUILD_TIMEOUT_S)
            res = {"result": "built", "seconds": round(time.monotonic() - t0, 2),
                   "missing": manifest.get("missing", [])}
        except Exception as exc:  # noqa: BLE001 - optional feature, never blocks a launch
            res = {"result": "failed", "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                   "seconds": round(time.monotonic() - t0, 2)}
            log.warning("Platinum bundle not built: %s", res["error"])
        if self.lifecycle is not None:
            self.lifecycle.event("platinum_assets", **res)
        return res
