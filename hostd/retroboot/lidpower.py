"""Low power while the lid is closed (``lid_low_power``; on by default on the Gemini PDA).

The Gemini PDA cannot suspend (s2idle hangs it), so with the lid closed the host keeps
running: the coordinator measured, on battery at an idle Finder (lid open, 60 s each,
against schedutil's 977 mA / 3.70 W), every cpufreq policy's ``scaling_max_freq`` at
its ``cpuinfo_min_freq`` (624 / 650 / 845 MHz) at -48 mA, and the keyboard matrix's
``matrix_keypad`` ``poll_ms`` 30 -> 1000 at -60 mA (the keyboard's I2C interrupts fall
from about 2400/s to 110/s; the keyboard is covered by the closed lid, and Esc/On is
the PMIC's mt6351-keys, which is not polled).  Unbinding the touchscreen saved noise
(-18 mA) and is not done.

* Enter (lidsleep.py): once the lid's Mac OS sleep request has an outcome (done, or
  skipped for any reason but the lid opening, hostd stopping or an external display);
  with ``lid_action`` "blank" ``lid_sleep_delay_s`` after the close.  Every policy's
  ``scaling_max_freq`` and the keypad's ``poll_ms`` are saved, then lowered.
  cpuplace.py leaves the policies alone meanwhile (``lid_capped``); an original it gives
  back while they are capped (Mac OS sleeping or stopping) replaces the value saved
  here (``hold``), so the lid opening restores what cpuplace meant.
* Leave: when the lid opens, first of all (before Mac OS is woken and the panel turned
  on, so the wake does not run at 845 MHz), or when an external display appears.  The
  exact saved values come back, then cpuplace applies its plan again.
* The saved values are kept in ``/run/retroboot/lidpower.json`` (as cpuplace.json): a
  hostd restarted with the lid closed keeps them and restores them at the next open; a
  hostd that finds the file with the lid open or unknown restores them at its start.

Best effort: a write that fails is noted (``errors``) and never stops a launch or a wake.
Lifecycle ``lid_low_power`` (``on``, ``why``, the values, ``battery_ma``).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .hostd import Hostd

log = logging.getLogger("retroboot.lidpower")

CPUFREQ = "devices/system/cpu/cpufreq"
KINDS = ("cpufreq", "keypad")


def _read(p: Path) -> str | None:
    try:
        return p.read_text().strip()
    except OSError:
        return None


def _write(p: Path, value: str) -> None:
    with open(p, "w") as f:
        f.write(str(value))


class LidPower:
    def __init__(self, app: "Hostd"):
        self.app = app
        self.sysfs = Path(getattr(app.cfg, "sysfs_path", "/sys") or "/sys")
        self.run_file = app.paths.run / "lidpower.json"
        self.saved: dict[str, dict[str, str]] = {k: {} for k in KINDS}
        self.active = False
        self.last: dict | None = None
        self.errors: list[str] = []
        self._lock = asyncio.Lock()

    # -- policy ------------------------------------------------------------------------------
    @property
    def profile(self) -> dict:
        plat = getattr(self.app, "platform", None)
        return (plat.profile if plat is not None else None) or {}

    @property
    def enabled(self) -> bool:
        v = getattr(self.app.cfg, "lid_low_power", "auto")
        if isinstance(v, str):
            v = v.strip().lower()
            if v in ("", "auto"):
                return bool(self.profile.get("lid_low_power"))
            return v in ("on", "true", "yes", "1")
        return bool(v)

    def keypad_param(self) -> Path | None:
        rel = self.profile.get("keypad_poll_param")
        return self.sysfs / rel if rel else None

    def keypad_poll_ms(self) -> int:
        try:
            return max(0, int(getattr(self.app.cfg, "lid_keypad_poll_ms", 1000)))
        except (TypeError, ValueError):
            return 0

    def policies(self) -> list[Path]:
        return sorted((self.sysfs / CPUFREQ).glob("policy*"))

    # -- sysfs (callers hold cpuplace's mutex: never between a poll's read and write) ---------
    def _cpuplace(self):
        return getattr(self.app, "cpuplace", None)

    def _locked(self, fn: Callable[[], dict]) -> dict:
        cp = self._cpuplace()
        mutex = getattr(cp, "_mutex", None)
        if mutex is None:
            return fn()
        with mutex:
            return fn()

    def _note(self, what: str, exc: Exception) -> None:
        msg = f"{what}: {type(exc).__name__}: {exc}"
        if msg not in self.errors:
            self.errors = (self.errors + [msg])[-8:]
        log.warning("lid low power: %s", msg)

    def _save(self, kind: str, path: str, value: str) -> None:
        if path not in self.saved[kind]:
            self.saved[kind][path] = value
            self._persist()             # before the write: the original is never lost

    def _persist(self) -> None:
        try:
            self.run_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.run_file.with_name(self.run_file.name + ".tmp")
            tmp.write_text(json.dumps(self.saved))
            os.replace(tmp, self.run_file)
        except OSError as exc:
            self._note("run file", exc)

    def _lower(self) -> dict:
        out: dict = {"policies_mhz": {}}
        for d in self.policies():
            low, cur = _read(d / "cpuinfo_min_freq"), _read(d / "scaling_max_freq")
            if not (low and low.isdigit() and cur and cur.isdigit()):
                continue
            path = str(d / "scaling_max_freq")
            try:
                if cur != low:
                    self._save("cpufreq", path, cur)
                    _write(Path(path), low)
                out["policies_mhz"][d.name] = int(_read(Path(path)) or low) // 1000
            except OSError as exc:
                self._note(f"scaling_max_freq {d.name}", exc)
        param, want = self.keypad_param(), self.keypad_poll_ms()
        if param is not None and want > 0:
            cur = _read(param)
            if cur is not None and cur.isdigit():
                try:
                    if int(cur) != want:
                        self._save("keypad", str(param), cur)
                        _write(param, str(want))
                    out["keypad_poll_ms"] = int(_read(param) or want)
                except OSError as exc:
                    self._note("keypad poll_ms", exc)
        return out

    def _restore(self) -> dict:
        done: dict = {}
        for kind in KINDS:
            for path, value in list(self.saved[kind].items()):
                try:
                    _write(Path(path), value)
                    done[path] = value
                except OSError as exc:
                    if not isinstance(exc, FileNotFoundError):     # a policy gone with its CPUs
                        self._note(f"restore {Path(path).parent.name}/{Path(path).name}", exc)
                self.saved[kind].pop(path, None)
        try:
            self.run_file.unlink()
        except OSError:
            pass
        return done

    def hold(self, path: str, value) -> bool:
        """cpuplace.py gives back an original ``scaling_max_freq`` while the lid keeps the
        policies capped: it becomes the value the lid's opening restores (True: held)."""
        if self.active and path in self.saved["cpufreq"]:
            self.saved["cpufreq"][path] = str(value)
            self._persist()
            return True
        return False

    # -- lifecycle ---------------------------------------------------------------------------
    def _set_capped(self, on: bool) -> None:
        cp = self._cpuplace()
        if cp is not None:
            cp.lid_capped = on

    def start(self, lid_open: bool | None) -> None:
        """hostd start: values an earlier hostd saved (restarted with the lid closed)."""
        try:
            data = json.loads(self.run_file.read_text())
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        for kind in KINDS:
            bucket = data.get(kind)
            if isinstance(bucket, dict):
                self.saved[kind].update({str(k): str(v) for k, v in bucket.items()})
        if lid_open is False and self.enabled:
            self.active = True
            self._set_capped(True)
            out = self._locked(self._lower)        # the earlier originals stay saved
            self.last = {"on": True, "why": "hostd restart", **out}
            self.app.lifecycle.event("lid_low_power", on=True, why="hostd restart", **out)
        else:
            restored = self._locked(self._restore)
            self.last = {"on": False, "why": "hostd start"}
            self.app.lifecycle.event("lid_low_power", on=False, why="hostd start", restored=len(restored),
                                     lid=None if lid_open is None else ("open" if lid_open else "closed"))

    async def enter(self, why: str, still: Callable[[], bool] | None = None) -> bool:
        """Lower the clocks and the keypad poll (``still``: the lid is still closed)."""
        if not self.enabled:
            return False
        async with self._lock:
            if self.active or (still is not None and not still()):
                return False
            self.active = True
            self._set_capped(True)
            try:
                out = await asyncio.to_thread(self._locked, self._lower)
            except Exception as exc:  # noqa: BLE001 -- best effort
                self._note("enter", exc)
                out = {}
            reading = self.app.laptop.battery_reading()
            self.last = {"on": True, "why": why, **out}
            self.app.lifecycle.event("lid_low_power", on=True, why=why, **out, **reading,
                                     errors=list(self.errors) or None)
            return True

    async def leave(self, why: str) -> bool:
        """Give back the saved values, then cpuplace's plan (Mac OS awake) applies again."""
        async with self._lock:
            if not self.active:
                return False
            try:
                restored = await asyncio.to_thread(self._locked, self._restore)
            except Exception as exc:  # noqa: BLE001
                self._note("leave", exc)
                restored = {}
            self.active = False
            self._set_capped(False)
            cp = self._cpuplace()
            if cp is not None and cp.plan.enabled and cp.state.get("active"):
                try:
                    await asyncio.to_thread(cp.apply_once, cp.qemu_pid)
                except Exception as exc:  # noqa: BLE001
                    self._note("cpuplace", exc)
            self.last = {"on": False, "why": why}
            self.app.lifecycle.event("lid_low_power", on=False, why=why,
                                     restored={Path(p).parent.name if Path(p).name == "scaling_max_freq" else
                                               "keypad_poll_ms": v for p, v in restored.items()})
            return True

    def public(self) -> dict:
        return {"enabled": self.enabled, "active": self.active, "last": self.last,
                **({"errors": list(self.errors)} if self.errors else {})}
