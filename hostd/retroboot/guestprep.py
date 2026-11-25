"""Adapter for the coordinator's ``rbguestprep`` library (offline guest-disk prep/repair).

The library is installed at ``/usr/lib/retroboot/guestprep`` (configurable via
``guestprep_path``) and provides::

    prep.apply(image) -> report dict      # fsck -fy, General Controls warning off,
                                          # Ethernet/DHCP TCP/IP prefs if missing, fsck -fn
    prep.fsck(image, repair=True) -> (ok, output)
    prep.PrepError

It needs root, losetup/mount, the hfsplus kernel module and fsck.hfsplus; the
guest must be stopped and nothing else may have the image open.  hostd only
calls it while it holds the guest (before a launch, or in an import job).

* ``apply`` runs on a reflink/sparse copy that replaces the disk only after
  success; on failure the original stays untouched.
* ``repair`` runs ``fsck(repair=True)`` in place before launching a machine
  whose previous session did not end in a verified guest-shutdown/guest-reset.

68k Macs (machine.json ``family: quadra800``, D044) have HFS (standard) disks and Mac OS
7.5.5 to 8.1, so they get the library's 68k module instead (``MODULE_68K``, the guest
workstream's ``rbguestprep`` for 68k disks)::

    prep68k.apply(image) -> report dict   # check/repair the HFS volume, install RetroBoot Idle
                                          # (System Folder:Extensions, INIT/RBid), improper-shutdown
                                          # warning off, the user's own settings kept
    prep68k.fsck(image, repair=True) -> (ok, output)
    prep68k.PREP_VERSION, prep68k.PrepError

through ``apply_68k``, ``repair_68k`` and ``version_68k``, the same rules (a copy swapped in
on success, never raising).  None of the Mac OS 9 hooks (AirPort, ATI, Control Strip, USB
Overdrive, Date & Time cities) run for them.  A library without the module leaves 68k Macs
unprepared (``unavailable``) and they start as they are.
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from .util import run as run_cmd

log = logging.getLogger("retroboot.guestprep")

UNAVAILABLE = "unavailable"
MODULE_68K = "rbguestprep.prep68k"


class GuestPrep:
    def __init__(self, lib_path: Path, module: Any = None, module_68k: Any = None):
        self.lib_path = Path(lib_path)
        self._mod = module
        self._load_error: str | None = None
        self._mod68k = module_68k            # MODULE_68K (tests inject a fake)
        self._load_error_68k: str | None = None
        # Extra prep.apply() keyword arguments (AirPort: {"network_port": ...}); keys an
        # older library does not accept are dropped.
        self.options_provider = None
        self.airport_module = None          # rbguestprep.airport (imported on first use)
        self.control_strip_module = None    # rbguestprep.controlstrip (tests inject a fake)
        self.printer_module = None          # rbguestprep.printer (tests inject a fake)

    def module(self):
        if self._mod is None and self._load_error is None:
            try:
                p = str(self.lib_path)
                if self.lib_path.is_dir() and p not in sys.path:
                    sys.path.insert(0, p)
                self._mod = importlib.import_module("rbguestprep.prep")
            except Exception as exc:  # ImportError, or a broken install
                self._load_error = f"rbguestprep not available: {exc}"
        return self._mod

    def available(self) -> tuple[bool, str | None]:
        if self.module() is None:
            return False, self._load_error
        if os.geteuid() != 0 and not getattr(self._mod, "RETROBOOT_TEST_FAKE", False):
            return False, "guest preparation needs root"
        return True, None

    # -- 68k Macs (MODULE_68K) -----------------------------------------------------------
    def module_68k(self):
        if self._mod68k is None and self._load_error_68k is None:
            if self.module() is None:
                self._load_error_68k = self._load_error
                return None
            if getattr(self._mod, "RETROBOOT_TEST_FAKE", False):
                self._load_error_68k = "no 68k preparation in this test"
                return None
            try:
                self._mod68k = importlib.import_module(MODULE_68K)
            except Exception as exc:  # an older library: 68k Macs start unprepared
                self._load_error_68k = f"{MODULE_68K} not available: {exc}"
        return self._mod68k

    def available_68k(self) -> tuple[bool, str | None]:
        mod = self.module_68k()
        if mod is None:
            return False, self._load_error_68k
        if os.geteuid() != 0 and not getattr(mod, "RETROBOOT_TEST_FAKE", False):
            return False, "guest preparation needs root"
        return True, None

    def version_68k(self) -> int | None:
        ok, _ = self.available_68k()
        if not ok:
            return None
        try:
            return int(getattr(self._mod68k, "PREP_VERSION", 1))
        except (TypeError, ValueError):
            return 1

    async def repair_68k(self, image: Path) -> dict:
        ok, why = self.available_68k()
        if not ok:
            return {"result": UNAVAILABLE, "reason": why}
        return await self._fsck(self._mod68k, image)

    async def apply_68k(self, image: Path) -> dict:
        """rbguestprep's 68k preparation on a copy swapped in on success.  Never raises."""
        ok, why = self.available_68k()
        if not ok:
            return {"result": UNAVAILABLE, "reason": why}
        t0 = time.monotonic()
        report, failed = await self._on_copy(image, self._mod68k.apply)
        if failed is not None:
            return failed
        if isinstance(report, dict):
            report = {k: v for k, v in report.items() if k != "image"}
        return {"result": "applied", "report": report, "seconds": round(time.monotonic() - t0, 3)}

    def for_family(self, family: str) -> dict:
        """The preparation calls for a Mac of ``family`` (machines.FAMILIES)."""
        if family == "quadra800":
            return {"apply": self.apply_68k, "repair": self.repair_68k, "version": self.version_68k}
        return {"apply": self.apply, "repair": self.repair, "version": self.version}

    async def repair(self, image: Path) -> dict:
        ok, why = self.available()
        if not ok:
            return {"result": UNAVAILABLE, "reason": why}
        return await self._fsck(self._mod, image)

    async def _fsck(self, mod, image: Path) -> dict:
        t0 = time.monotonic()
        try:
            good, out = await asyncio.to_thread(mod.fsck, str(image), True)
        except Exception as exc:
            return {"result": "failed", "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                    "seconds": round(time.monotonic() - t0, 3)}
        lines = (out or "").strip().splitlines()
        return {"result": "ok" if good else "failed", "seconds": round(time.monotonic() - t0, 3),
                "output": lines[-6:]}

    def version(self) -> int | None:
        """The library's PREP_VERSION (1 for a library that predates it); None if unavailable."""
        ok, _ = self.available()
        if not ok:
            return None
        try:
            return int(getattr(self._mod, "PREP_VERSION", 1))
        except (TypeError, ValueError):
            return 1

    def airport_software_present(self, image: Path) -> bool | None:
        """Read-only check for the original AirPort driver (None when unknown)."""
        ok, _ = self.available()
        fn = getattr(self._mod, "airport_software_present", None) if ok else None
        if fn is None:
            return None
        try:
            return bool(fn(str(image)))
        except Exception:
            return None

    def ati_software_present(self, image: Path) -> bool | None:
        """Read-only check for ATI's display driver (graphics.py; None when unknown)."""
        ok, _ = self.available()
        fn = getattr(self._mod, "ati_software_present", None) if ok else None
        if fn is None:
            return None
        try:
            return bool(fn(str(image)))
        except Exception:
            return None

    def mac_cities(self, image: Path) -> list[dict] | None:
        """Read-only: Date & Time's city list from the stopped disk (rbguestprep.macloc), for
        Mac OS's location (macclock.choose_city).  None when unknown: library or reader
        missing, or the disk unreadable."""
        if self.module() is None:
            return None
        try:
            mod = importlib.import_module("rbguestprep.macloc")
            return list(mod.read_cities(str(image))) or None
        except Exception:
            return None

    def usb_overdrive_present(self, image: Path) -> bool | None:
        """Read-only: USB Overdrive's control panel is active in the blessed System
        Folder (None when unknown: library missing or the disk unreadable)."""
        if self.module() is None:
            return None
        try:
            mod = importlib.import_module("rbguestprep.usboverdrive")
            return bool(mod.present(str(image)))
        except Exception:
            return None

    async def _on_copy(self, image: Path, fn, *args, **kw) -> tuple[Any, dict | None]:
        """Run ``fn(copy, *args, **kw)`` on a reflink/sparse copy of ``image`` and swap the
        copy in when it returns.  (result, None) or (None, failure dict); never raises."""
        image = Path(image)
        copy = image.with_name(f".{image.name}.prep")
        try:
            copy.unlink()
        except FileNotFoundError:
            pass
        t0 = time.monotonic()
        rc, _, err = await run_cmd(["cp", "--reflink=auto", "--sparse=always", str(image), str(copy)], timeout=3600)
        if rc != 0:
            copy.unlink(missing_ok=True)
            return None, {"result": "failed", "error": f"copy failed: {err.strip()[:200]}"}
        try:
            st = image.stat()
            out = await asyncio.to_thread(fn, str(copy), *args, **kw)
            os.chmod(copy, st.st_mode & 0o7777)
            try:
                os.chown(copy, st.st_uid, st.st_gid)
            except PermissionError:
                pass
            os.replace(copy, image)
        except Exception as exc:
            copy.unlink(missing_ok=True)
            kind = "PrepError" if type(exc).__name__ == "PrepError" else type(exc).__name__
            return None, {"result": "failed", "error": f"{kind}: {str(exc)[:400]}",
                          "seconds": round(time.monotonic() - t0, 3)}
        return out, None

    async def apply(self, image: Path) -> dict:
        """Prepare a copy and swap it in on success.  Never raises."""
        ok, why = self.available()
        if not ok:
            return {"result": UNAVAILABLE, "reason": why}
        opts = dict(self.options_provider() or {}) if self.options_provider else {}
        try:
            params = inspect.signature(self._mod.apply).parameters
            if not any(p.kind == p.VAR_KEYWORD for p in params.values()):
                opts = {k: v for k, v in opts.items() if k in params}
        except (TypeError, ValueError):
            opts = {}
        t0 = time.monotonic()
        report, failed = await self._on_copy(image, self._mod.apply, **opts)
        if failed is not None:
            return failed
        if isinstance(report, dict):
            report = {k: v for k, v in report.items() if k != "image"}
        return {"result": "applied", "report": report, "seconds": round(time.monotonic() - t0, 3)}

    def _airport_module(self):
        if self.module() is None:
            return None
        if self.airport_module is not None:                      # tests inject a fake
            return self.airport_module
        try:
            return importlib.import_module("rbguestprep.airport")
        except Exception:
            return None

    async def airport_network(self, image: Path, name: bytes) -> dict:
        """AirPort's startup network (rbguestprep.airport): make AirPort 2.0.4 join ``name``
        (guest bytes) when Mac OS starts.  The guest is stopped.  An existing setting is
        changed in place (33 bytes, about 25 ms); creating the preferences file allocates
        blocks, so that runs on a copy swapped in after success, like ``apply``.  Never raises.
        {"result": unchanged | set | created | added | unexpected-layout | unavailable | failed}"""
        ok, why = self.available()
        mod = self._airport_module() if ok else None
        if mod is None:
            return {"result": UNAVAILABLE, "reason": why or "rbguestprep.airport not available"}
        t0 = time.monotonic()
        try:
            res = await asyncio.to_thread(mod.set_network, str(image), bytes(name), False)
        except Exception as exc:
            return {"result": "failed", "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                    "seconds": round(time.monotonic() - t0, 3)}
        if res in ("missing", "no-settings"):
            res, failed = await self._on_copy(image, mod.set_network, bytes(name), True)
            if failed is not None:
                return failed
        return {"result": res, "seconds": round(time.monotonic() - t0, 3)}

    def _control_strip_module(self):
        if self.control_strip_module is not None:                # tests inject a fake
            return self.control_strip_module
        if self.module() is None or getattr(self._mod, "RETROBOOT_TEST_FAKE", False):
            return None
        try:
            return importlib.import_module("rbguestprep.controlstrip")
        except Exception:                                        # an older library
            return None

    async def control_strip(self, image: Path, size: tuple[int, int]) -> dict:
        """The Control Strip's saved place for a Mac OS that starts on a ``size`` (width,
        height) screen (rbguestprep.controlstrip.fit).  The guest is stopped; 12 bytes of
        the existing preferences change in place, nothing is created.  Never raises.
        {"result": moved | unchanged | missing | no-settings | unexpected-layout |
        unavailable | failed, "from"/"to": [screen w, h, top, edge] when moved}"""
        ok, why = self.available()
        mod = self._control_strip_module() if ok else None
        if mod is None:
            return {"result": UNAVAILABLE, "reason": why or "rbguestprep.controlstrip not available"}
        t0 = time.monotonic()
        try:
            res = await asyncio.to_thread(mod.fit, str(image), (int(size[0]), int(size[1])))
        except Exception as exc:
            return {"result": "failed", "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                    "seconds": round(time.monotonic() - t0, 3)}
        return {**res, "seconds": round(time.monotonic() - t0, 3)}

    def _printer_module(self):
        if self.printer_module is not None:                      # tests inject a fake
            return self.printer_module
        if self.module() is None or getattr(self._mod, "RETROBOOT_TEST_FAKE", False):
            return None
        try:
            return importlib.import_module("rbguestprep.printer")
        except Exception:                                        # an older library
            return None

    async def ensure_printer(self, image: Path, **kw) -> dict:
        """Provision the default LPR Desktop Printer on a stopped Mac OS 9 disk
        (rbguestprep.printer.ensure_printer), preserving any user-created printer.
        Never raises."""
        ok, why = self.available()
        mod = self._printer_module() if ok else None
        if mod is None:
            return {"result": UNAVAILABLE, "reason": why or "rbguestprep.printer not available"}
        t0 = time.monotonic()
        if hasattr(mod, "state"):
            try:
                state_kw = {k: kw[k] for k in ("name",) if k in kw}
                st = await asyncio.to_thread(mod.state, str(image), **state_kw)
                if isinstance(st, dict) and st.get("result") in ("retroboot-configured", "existing-printer-kept"):
                    return {**st, "seconds": round(time.monotonic() - t0, 3)}
            except Exception:
                pass
        res, failed = await self._on_copy(image, mod.ensure_printer, **kw)
        if failed is not None:
            return failed
        if isinstance(res, dict):
            return {**res, "seconds": round(time.monotonic() - t0, 3)}
        return {"result": str(res), "seconds": round(time.monotonic() - t0, 3)}
