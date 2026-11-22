"""Settings apply engine.

* Live settings are pushed to their adapters immediately on apply: presenter
  ``display_config``/``input_config``, ALSA volume, web listener on/off and
  the hostname.
* ``display.host_mode`` (confirm 15 s) records the previous value; if nobody
  confirms in time (panel "Keep" or ``POST /settings/confirm``) the previous
  value is restored and pushed again.  A pending confirmation found at startup
  is reverted immediately (the user never confirmed it).
* Restart-class settings are applied immediately when no guest is running;
  otherwise they wait for an explicit Apply and Restart, which then uses the
  ordinary clean restart path.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from . import schema
from .jobs import Job, JobFailed
from .settings import StaleRevision
from .util import wall

if TYPE_CHECKING:
    from .hostd import Hostd

log = logging.getLogger("retroboot.apply")

DISPLAY_KEYS = ("display.pixel_scale", "display.guest_size", "display.scaling", "display.host_mode",
                "display.primary", "display.rotation", "display.rotation_lock")
# What GRUB's background (rb-bootdisplay) depends on: the grid, the orientation and the startup indicator.
BOOT_DISPLAY_KEYS = ("display.pixel_scale", "display.guest_size", "display.rotation", "startup.progress_indicator")
INPUT_KEYS = ("input.swap_command_option", "input.recovery_chord", "input.pointer_speed", "input.touch_gestures")


class ApplyEngine:
    def __init__(self, app: "Hostd"):
        self.app = app
        self.settings = app.settings
        self._confirm_task: asyncio.Task | None = None

    # -- pushes ------------------------------------------------------------------
    def display_config(self) -> dict:
        a = self.settings.applied
        ps = a["display.pixel_scale"]
        # With the ATI Radeon Mac OS starts at ATI's own 640 x 480 (graphics.py): the
        # startup indicator and the checkerboard anchor follow that size.
        ati = self.app.graphics.startup_guest_size() if getattr(self.app, "graphics", None) else None
        return {"scaling": a["display.scaling"],
                "pixel_scale": "auto" if ps == "auto" else int(ps),
                "guest_size": ati or list(schema.parse_size(a["display.guest_size"]) or []) or None,
                "host_mode": schema.parse_host_mode(a["display.host_mode"]),
                "primary": a.get("display.primary", "internal"),
                # display.rotation: a fixed one, or "auto" (orientation.Rotation: the tilt sensor)
                "rotation": self.app.rotation.target() if getattr(self.app, "rotation", None) else
                int(a["display.rotation"]) if a.get("display.rotation", "auto") != "auto" else 0}

    def input_config(self) -> dict:
        a = self.settings.applied
        out = {"swap_command_option": a["input.swap_command_option"],
               "recovery_chord": a["input.recovery_chord"],
               "pointer_speed": a["input.pointer_speed"],
               "touch_gestures": a.get("input.touch_gestures", True)}
        # Gemini PDA: its keyboard's keycaps (presenter gemkbd.c); lab: touchscreen axes.
        # Sent only when they apply, so a PC's request is unchanged.
        cfg = self.app.cfg
        plat = getattr(self.app, "platform", None)
        if plat is not None and plat.is_gemini:
            out["keyboard_layout"] = (getattr(cfg, "gemini_keyboard", "") or "us").lower()
        if getattr(cfg, "touch_transform", ""):
            out["touch_transform"] = cfg.touch_transform
        return out

    async def push_display(self) -> bool:
        return await self.app.presenter.try_request("display_config", **self.display_config()) is not None

    async def push_input(self) -> bool:
        return await self.app.presenter.try_request("input_config", **self.input_config()) is not None

    async def push_presenter(self) -> None:
        # The startup indicator's config first: a display change below re-enters
        # the checkerboard, which is when the presenter takes it.
        await self.app.startup.push_config()
        await self.push_display()
        await self.push_input()

    async def push_volume(self) -> bool:
        return await self.app.mixer.set_volume(self.settings.applied["audio.volume"])

    async def apply_live_keys(self, keys: list[str]) -> dict:
        results = {}
        if any(k in DISPLAY_KEYS for k in keys):
            if getattr(self.app, "rotation", None) is not None:
                await self.app.rotation.sync()      # follow the tilt sensor or not
            results["display"] = await self.push_display()
        if "startup.progress_indicator" in keys:
            await self.app.startup.push_config()   # applies from the next startup
            results["startup"] = self.app.startup.enabled
        if any(k in BOOT_DISPLAY_KEYS for k in keys):
            results["boot_display"] = await self.update_boot_display(why=",".join(k for k in keys
                                                                              if k in BOOT_DISPLAY_KEYS))
        if any(k in INPUT_KEYS for k in keys):
            results["input"] = await self.push_input()
        if "audio.volume" in keys:
            results["volume"] = await self.push_volume()
        if "management.web" in keys:
            await self.app.web.sync_listeners()
            results["web"] = self.settings.applied["management.web"]
            # Stopping the listener leaves open connections alone: end screen streams too.
            if results["web"] == "off" and getattr(self.app, "remote", None) is not None:
                await self.app.remote.disconnect_all("web-management-off", "Web management was turned off on "
                                                                           "the RetroBoot computer.")
        if "management.hostname" in keys:
            results["hostname"] = await self.app.network.set_hostname(self.settings.applied["management.hostname"])
        if any(k.startswith("network.web_proxy") for k in keys):
            # Modern Web Helper: start, reload or stop at once; Mac OS keeps running.
            results["web_proxy"] = (await self.app.webproxy.sync("settings")).get("state")
        if "bluetooth.enabled" in keys and getattr(self.app, "bluetooth", None) is not None:
            # The radio follows the setting at once (rfkill-friendly); pairings stay.
            results["bluetooth"] = await self.app.bluetooth.sync_power("setting")
        if "remote.screen" in keys and getattr(self.app, "remote", None) is not None:
            await self.app.remote.apply_setting()
            results["remote"] = self.settings.applied["remote.screen"]
        return results

    async def update_boot_display(self, why: str = "display.pixel_scale") -> bool:
        """Persist the grid and the startup indicator through cold boots:
        rb-bootdisplay rewrites GRUB's display.cfg and background from the
        applied display settings, the desired next Mac, the presenter's display.json and the startup timing
        model, and GRUB passes them to the splash and presenter (4.4, 4.4.1)."""
        cmd = self.app.cfg.bootdisplay_cmd
        scale = self.settings.applied["display.pixel_scale"]
        plat = getattr(self.app, "platform", None)
        if plat is not None and plat.is_gemini:
            # No GRUB, display.cfg or ESP: LK boots the Android boot.img (p22) and the root is a
            # bind mount; the presenter takes its defaults at every start.
            self.app.lifecycle.event("boot_display_update", pixel_scale=scale, why=why, skipped="no GRUB")
            return False
        if plat is not None and plat.is_raspberry_pi and getattr(self.app, "mode", None) == "installer":
            # The live installer's /boot/firmware would be some other disk's boot partition (the
            # medium has none by that name); install-to-disk writes the installed one's cmdline.txt
            # with the install seed (ARCHITECTURE 8.2).
            self.app.lifecycle.event("boot_display_update", pixel_scale=scale, why=why, skipped="installer")
            return False
        if not cmd:
            self.app.lifecycle.event("boot_display_update", pixel_scale=scale, why=why, dev_hook=True)
            return False
        from .util import run as run_cmd
        rc, _, err = await run_cmd(cmd, timeout=30)
        self.app.lifecycle.event("boot_display_update", pixel_scale=scale, why=why, ok=rc == 0,
                                 error=err.strip()[:120] if rc else None)
        return rc == 0

    async def apply_timezone(self) -> bool:
        """Host side of system.timezone (QEMU gets TZ from guest-launcher)."""
        cmd = self.app.cfg.timezone_cmd
        tz = self.settings.applied["system.timezone"]
        if not cmd:
            return False
        from .util import run as run_cmd
        rc, _, err = await run_cmd(cmd + [tz], timeout=20)
        self.app.lifecycle.event("timezone_set", zone=tz, ok=rc == 0, error=err.strip()[:120] if rc else None)
        return rc == 0

    async def apply_restart_keys(self, keys: list[str]) -> None:
        """Host adapters for restart-class keys at the moment they become applied."""
        if "machine.active" in keys:
            await self.app.startup.push_config()
            await self.update_boot_display(why="startup Mac")
        if "system.timezone" in keys:
            await self.apply_timezone()
        if "display.graphics" in keys and getattr(self.app, "graphics", None) is not None \
                and not self.app.supervisor.active:
            # Mac OS is stopped: GRUB's background and the presenter's startup geometry
            # follow the card now (a restart re-syncs in the launch hook instead, so a
            # running Mac OS is never resized on its way down).
            await self.app.graphics.sync_startup()

    async def apply_pending_restart(self, reason: str) -> list[str]:
        """Restart-class changes waiting for Mac OS to restart (the startup Mac chosen
        while Mac OS runs, 3D acceleration, sound: "takes effect when Mac OS restarts")
        become applied just before Mac OS starts again, whatever starts it: its own
        Restart, Recovery's Restart, a relaunch. Apply and Restart has already applied
        them. A startup Mac that was removed meanwhile is not applied (the current one
        starts)."""
        keys = self.settings.pending_by_policy()[schema.GUEST_RESTART]
        if "machine.active" in keys and self.app.machines.get(self.settings.desired.get("machine.active")) is None:
            keys = [k for k in keys if k != "machine.active"]
        if not keys:
            return []
        self.settings.mark_applied(keys)
        await self.apply_restart_keys(keys)
        self.app.lifecycle.event("settings_applied", revision=self.settings.revision, keys=keys,
                                 pending_restart=[], origin=f"mac-os-start:{reason}")
        return keys

    async def startup(self) -> None:
        """Called once at start: revert unconfirmed changes, apply host-reboot keys."""
        if self.settings.pending_confirm:
            prev = self.settings.revert_pending_confirm()
            self.app.lifecycle.event("settings_reverted", keys=sorted(prev), why="unconfirmed before restart")
        reboot_keys = self.settings.pending_by_policy()[schema.HOST_REBOOT]
        if reboot_keys:
            self.settings.mark_applied(reboot_keys)

    # -- apply ---------------------------------------------------------------------
    async def apply(self, revision, restart: bool, job: Job | None, origin: str = "web") -> dict:
        self.settings.check_revision(revision)
        pending = self.settings.pending_by_policy()
        live = pending[schema.LIVE]
        restart_keys = pending[schema.GUEST_RESTART]
        result: dict = {"revision": self.settings.revision, "applied": [], "pending_restart": [],
                        "pending_reboot": pending[schema.HOST_REBOOT], "confirm": None}
        if live:
            confirm_keys = [k for k in live if schema.BY_KEY[k].confirm]
            previous = {k: self.settings.applied[k] for k in confirm_keys}
            self.settings.mark_applied(live)
            result["adapters"] = await self.apply_live_keys(live)
            result["applied"] += live
            if confirm_keys:
                seconds = self.app.cfg.confirm_seconds or max(schema.BY_KEY[k].confirm for k in confirm_keys)
                self.settings.begin_confirm(previous, seconds)
                self._start_confirm_timer(seconds)
                result["confirm"] = {"keys": confirm_keys, "seconds": seconds}
                self.app.lifecycle.event("settings_confirm_started", keys=confirm_keys, seconds=seconds, origin=origin)
                # The 15 s Keep/Revert alert appears on this computer only for changes made
                # here; a change from the web is confirmed on the web and, if it is not,
                # reverts without leaving an alert that blocks Mac OS on this computer.
                if origin == "panel":
                    self.app.loop.create_task(self.app.panel.show_keep_setting(seconds))
        sup = self.app.supervisor
        if restart_keys:
            if "machine.active" in restart_keys:
                # Even when the running Mac keeps its old profile until Restart,
                # the next cold boot belongs to the selected Mac. Persist its
                # preboot style now so a host restart cannot flash the old one.
                await self.update_boot_display(why="startup Mac choice")
            if not sup.active:
                self.settings.mark_applied(restart_keys)
                result["applied"] += restart_keys
                await self.apply_restart_keys(restart_keys)
            elif restart:
                self.settings.mark_applied(restart_keys)
                result["applied"] += restart_keys
                await self.apply_restart_keys(restart_keys)
                result["restarting"] = True
            else:
                result["pending_restart"] = restart_keys
        self.app.lifecycle.event("settings_applied", revision=self.settings.revision, keys=result["applied"],
                                 pending_restart=result["pending_restart"], origin=origin)
        if result.get("restarting"):
            if job:
                job.update(0.2, "Restarting Mac OS to apply the changes…")
            res = await sup.job_clean(job, "restart", poweroff=False) if job else None
            result["restart"] = res
        return result

    # -- timed confirmation ------------------------------------------------------------
    def _start_confirm_timer(self, seconds: float) -> None:
        if self._confirm_task and not self._confirm_task.done():
            self._confirm_task.cancel()
        self._confirm_task = self.app.loop.create_task(self._confirm_wait(seconds))

    async def _confirm_wait(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
        if self.settings.pending_confirm:
            prev = self.settings.revert_pending_confirm()
            await self.apply_live_keys(sorted(prev))
            self.settings.last_confirm = {"result": "reverted", "outcome": "reverted", "reason": "timeout", "keys": sorted(prev),
                                          "at": wall(), "origin": None}
            self.app.lifecycle.event("settings_reverted", keys=sorted(prev), why="not confirmed in time",
                                     values=prev)
            await self.app.panel.on_setting_reverted(sorted(prev))

    async def confirm(self, origin: str) -> dict:
        pc = self.settings.pending_confirm
        self.settings.confirm()  # raises NothingToConfirm
        if self._confirm_task and not self._confirm_task.done():
            self._confirm_task.cancel()
        self.settings.last_confirm = {"result": "confirmed", "outcome": "kept", "reason": "requested", "keys": pc["keys"] if pc else [], "at": wall(),
                                      "origin": origin}
        self.app.lifecycle.event("settings_confirmed", keys=pc["keys"] if pc else None, origin=origin)
        await self.app.panel.on_setting_confirmed()
        return {"revision": self.settings.revision}

    async def revert_now(self, origin: str) -> dict:
        if not self.settings.pending_confirm:
            return {"revision": self.settings.revision}
        prev = self.settings.revert_pending_confirm()
        if self._confirm_task and not self._confirm_task.done():
            self._confirm_task.cancel()
        await self.apply_live_keys(sorted(prev))
        self.settings.last_confirm = {"result": "reverted", "outcome": "reverted", "reason": "requested", "keys": sorted(prev), "at": wall(),
                                      "origin": origin}
        self.app.lifecycle.event("settings_reverted", keys=sorted(prev), why="reverted by user", origin=origin)
        return {"revision": self.settings.revision}

    async def change_and_apply(self, changes: dict, restart: bool = False, origin: str = "panel") -> dict:
        """Panel convenience: PATCH then apply on the current revision."""
        rev = self.settings.revision
        new_rev = self.settings.patch(rev, changes, self.app.validation_context())
        return await self.apply(new_rev, restart, None, origin)
