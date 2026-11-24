"""Panel controller and the normal-mode screens.

Each screen renders one UI document and handles ``ui_action``/``ui_change``
events for its own document id.  Long operations run as jobs (the same jobs
the web API uses, so exclusivity is shared) and are shown with a progress
window.  Secure field values (Wi-Fi passwords) are never echoed back into a
document and never logged.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import shutil
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from .. import __version__, catalogs as _catalogs, hostinfo, schema
from ..catalog import download_of
from ..jobs import Busy, FAILED, Job, SUCCEEDED
from ..machines import BLANK_SIZES_GB, FAMILY_LABELS, POWERMAC, QUADRA800, MachineError  # noqa: F401
from ..network import NetworkError
from ..security import qr_matrix
from ..settings import NothingToConfirm
from ..util import mono
from . import ui

if TYPE_CHECKING:
    from ..hostd import Hostd

log = logging.getLogger("retroboot.panel")


class Screen:
    doc_id = "screen"
    tick: float | None = None
    guest_background_ok = True
    pairing_visible = False

    def __init__(self, panel: "Panel"):
        self.panel = panel
        self.app = panel.app

    def doc(self) -> dict:
        raise NotImplementedError

    def sync_from_settings(self) -> None:
        pass

    async def action(self, id: str, values: dict) -> None:
        pass

    async def change(self, id: str, value: Any) -> None:
        pass

    async def on_tick(self) -> None:
        await self.panel.refresh()


class Panel:
    def __init__(self, app: "Hostd"):
        self.app = app
        self.screen: Screen | None = None
        self.visible = False
        self.background: str | None = None
        self.last_doc: dict | None = None
        self._ticker: asyncio.Task | None = None
        self.return_to: Callable[[], Awaitable[None]] | None = None

    # -- display plumbing ---------------------------------------------------------
    def _background(self, screen: Screen) -> str:
        sup = self.app.supervisor
        if screen.guest_background_ok and sup.state in ("running", "stopping") and sup.content_seen and not sup.paused:
            return "guest-dimmed"
        return "checkerboard"

    async def show(self, screen: Screen, background: str | None = None, reset: bool = True) -> None:
        self.screen = screen
        bg = background or self._background(screen)
        doc = screen.doc()
        if reset:
            doc["reset"] = True
        self.last_doc = doc
        if not self.visible or bg != self.background:
            await self.app.show_scene("panel", background=bg)
            self.visible = True
            self.background = bg
        await self.app.presenter.try_request("ui_show", doc=doc)
        self._restart_ticker()

    async def refresh(self) -> None:
        if not self.screen or not self.visible:
            return
        doc = self.screen.doc()
        self.last_doc = doc
        await self.app.presenter.try_request("ui_show", doc=doc)

    def _stop_ticker(self) -> None:
        # A screen's on_tick may close or replace the panel itself (an alert that
        # acts after N seconds). Never cancel the task running it: the rest of its
        # continuation would be lost. On the N5105 the low-battery alert's
        # auto-close cancelled the clean shutdown that followed it. That ticker's
        # loop ends by itself once its screen is no longer shown.
        t, self._ticker = self._ticker, None
        if t and not t.done() and t is not asyncio.current_task():
            t.cancel()

    def _restart_ticker(self) -> None:
        self._stop_ticker()
        if self.screen and self.screen.tick:
            self._ticker = self.app.loop.create_task(self._tick(self.screen))

    async def _tick(self, screen: Screen) -> None:
        me = asyncio.current_task()
        try:
            while self.screen is screen and self.visible and self._ticker is me:
                await asyncio.sleep(screen.tick)
                if self.screen is screen and self.visible and self._ticker is me:
                    await screen.on_tick()
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("panel tick failed")

    def forget(self) -> None:
        """Drop the panel without changing the scene (the supervisor sets it)."""
        self.screen = None
        self.visible = False
        self.background = None
        self._stop_ticker()

    async def close(self, to_guest: bool = True) -> None:
        was_visible = self.visible
        self.forget()
        if not was_visible or not to_guest:
            return
        sup = self.app.supervisor
        if sup.paused:
            await sup.resume()
        elif sup.active:
            await self.app.show_scene("guest", reveal="now" if sup.content_seen else "first-content")
        elif self.app.mode == "normal":
            await self.show_startup_options()

    async def repush(self) -> None:
        """Presenter reconnected: re-send the panel scene and document."""
        if self.visible and self.screen:
            self.visible = False
            await self.show(self.screen, reset=False)

    async def on_event(self, msg: dict) -> None:
        ev = msg.get("event")
        if not self.screen or msg.get("doc") != (self.last_doc or {}).get("id"):
            return
        try:
            if ev == "ui_action":
                await self.screen.action(msg.get("id"), msg.get("values") or {})
            elif ev == "ui_change":
                await self.screen.change(msg.get("id"), msg.get("value"))
        except Exception:
            log.exception("panel action failed")
            await self.message("caution", "Something went wrong.", "The action could not be completed.")

    # -- common flows ------------------------------------------------------------
    async def message(self, icon: str, message: str, detail: str | None = None,
                      then: Callable[[], Awaitable[None]] | None = None, button: str = "OK",
                      auto_seconds: float | None = None) -> None:
        await self.show(MessageAlert(self, icon, message, detail, then, button, auto_seconds))

    async def run_job(self, job: Job, title: str, then: Callable[[Job], Awaitable[None]],
                      allow_force: bool = False, allow_cancel: bool = False) -> None:
        """Show progress and call ``then`` when the job ends, even if the job's own flow
        dismissed the progress window (a clean shutdown switches to the guest scene).
        ``allow_cancel``: a Stop button while the job says it can stop (a download)."""
        screen = JobScreen(self, job, title, then, allow_force, allow_cancel)
        await self.show(screen)
        self.app.loop.create_task(self._watch(job, screen))

    async def _watch(self, job: Job, screen: "JobScreen") -> None:
        await job.done.wait()
        if screen.handled:
            return
        screen.handled = True
        try:
            await screen.then(job)
        except Exception:
            log.exception("panel job continuation failed")

    async def start_job(self, starter: Callable[[], Job], title: str, then: Callable[[Job], Awaitable[None]],
                        allow_force: bool = False, allow_cancel: bool = False) -> None:
        try:
            job = starter()
        except Busy as exc:
            await self.message("caution", "RetroBoot is busy.", f"Wait for “{exc.job.kind}” to finish, then try again.",
                               then=self.show_recovery)
            return
        await self.run_job(job, title, then, allow_force, allow_cancel)

    async def show_startup_options(self, notice: str | None = None) -> None:
        await self.show(RetroBootWindow(self, notice, kind="startup"))

    async def show_boot_options(self) -> None:
        await self.show(RetroBootWindow(self, kind="boot"))

    async def show_recovery(self) -> None:
        if self.app.mode == "installer":
            return
        await self.show(RetroBootWindow(self))

    async def show_setup(self) -> None:
        await self.show(SetupWelcome(self))

    async def show_needs_cd(self, name: str, blank: bool) -> None:
        await self.show(NeedsCD(self, name, blank))

    async def show_failure(self, message: str, detail: str, evidence: str | None) -> None:
        await self.show(FailureAlert(self, message, detail, evidence))

    async def show_stopped(self, message: str) -> None:
        await self.show(Stopped(self, message))

    async def show_power_alert(self) -> None:
        await self.show(PowerAlert(self))

    def is_pairing_visible(self) -> bool:
        return bool(self.visible and self.screen is not None and getattr(self.screen, "pairing_visible", False))

    async def show_keep_setting(self, seconds: int) -> None:
        prev = self.screen if self.visible else None
        await self.show(KeepSetting(self, prev))

    async def on_setting_reverted(self, keys: list[str]) -> None:
        if isinstance(self.screen, KeepSetting):
            back = self.screen.previous
            if back is not None:
                back.sync_from_settings()
            await self.message("note", "The display setting was restored.",
                               "The new setting was not kept, so the previous one is back.",
                               then=(lambda: self.show(back)) if back else None)

    async def on_setting_confirmed(self) -> None:
        if isinstance(self.screen, KeepSetting):
            back = self.screen.previous
            if back is not None:
                back.sync_from_settings()
                await self.show(back)
            else:
                await self.close()

    async def show_software_update(self) -> None:
        from .update import SoftwareUpdate
        await self.show(SoftwareUpdate(self))

    async def show_update_progress(self, title: str, detail: str | None, progress: float | None) -> None:
        """Classic progress window over the checkerboard (Restart to Update, going back)."""
        from .update import UpdateProgress
        await self.show(UpdateProgress(self, title, detail, progress), background="checkerboard")

    async def show_settings_notice(self, notice: dict, then: Callable[[], Awaitable[None]]) -> None:
        await self.show(MessageAlert(self, "caution", "RetroBoot restored its settings.", notice["message"], then,
                                     "Continue", auto_seconds=15))

    async def show_corrupt_disk_recovery(self, info: dict) -> None:
        await self.show(CorruptDiskRecovery(self, info))


# --- generic screens -------------------------------------------------------------------

class MessageAlert(Screen):
    doc_id = "message"

    def __init__(self, panel, icon, message, detail, then, button="OK", auto_seconds: float | None = None):
        super().__init__(panel)
        self.icon, self.message, self.detail, self.then, self.button = icon, message, detail, then, button
        self.auto = auto_seconds
        self.tick = auto_seconds

    def doc(self):
        return ui.alert(self.doc_id, self.icon, self.message, self.detail,
                        ui.buttons(ui.btn("ok", self.button, default=True)))

    async def on_tick(self):
        await self.action("ok", {})

    async def action(self, id, values):
        if self.panel.screen is not self:
            return
        if self.then:
            await self.then()
        else:
            await self.panel.close()


class JobScreen(Screen):
    doc_id = "job-progress"
    tick = 0.5
    guest_background_ok = True

    def __init__(self, panel, job: Job, title: str, then, allow_force: bool, allow_cancel: bool = False):
        super().__init__(panel)
        self.job, self.title, self.then, self.allow_force = job, title, then, allow_force
        self.allow_cancel = allow_cancel
        self.handled = False

    def doc(self):
        j = self.job
        msg = j.message or "Working…"
        if msg.startswith("Downloading") and isinstance(j.progress, (int, float)):
            msg = ui.format_download_status(j.progress, msg)
        items = [ui.text(self.title, "emphasis"), ui.text(msg), ui.progress(j.progress)]
        if self.allow_force:
            items.append(ui.text("If Mac OS is not responding, you can force it to stop. Unsaved changes may be lost.",
                                 "small"))
            items.append(ui.buttons(ui.btn("force", "Force Stop…")))
        if self.allow_cancel and j.cancellable and not j.done.is_set():
            items.append(ui.text("A stopped download continues where it left off next time.", "small"))
            items.append(ui.buttons(ui.btn("cancel", "Stop Download")))
        return ui.window(self.doc_id, "RetroBoot", items, width=420)

    async def on_tick(self):
        if not self.job.done.is_set():
            await self.panel.refresh()

    async def action(self, id, values):
        if id == "force":
            await self.panel.show(ConfirmForce(self.panel, "restart", back=self))
        elif id == "cancel" and self.allow_cancel:
            self.app.jobs.cancel(self.job, "The download was stopped.")


# --- normal-mode screens ---------------------------------------------------------------

def guest_status_text(app) -> str:
    sup = app.supervisor
    if sup.state in ("running", "stopping") and sup.responsive is False:
        return "Mac OS is not responding."
    return {"running": "Mac OS is running.", "starting": "Mac OS is starting.", "paused": "Mac OS is paused.",
            "stopping": "Mac OS is shutting down.", "stopped": "Mac OS is not running."}.get(sup.state, "")


def unresponsive_detail(app) -> str | None:
    """One sentence for the recovery panel when the guest looks frozen."""
    sup = app.supervisor
    if sup.responsive is not False or sup.state not in ("running", "stopping"):
        return None
    if sup.unresponsive_reason == "display":
        return "The screen has stopped changing and the pointer does not move. If it stays this way, use Force Restart."
    return "The emulator stopped answering. If Mac OS stays frozen, use Force Restart."


class RetroBootWindow(Screen):
    """The RetroBoot window: Control-Alt-Delete, Escape while Mac OS starts (Mac OS paused) and
    whenever Mac OS isn't running.  One status line, one list of panes in the manner of the
    Control Panels folder (each with a one-line summary; one click opens it, or select and
    Open), Shut Down… (Mac OS's own Restart / Cancel / Shut Down question) and the way back to
    Mac OS.  Force Restart and Force Shut Down appear when Mac OS is not responding (a stuck
    Restart or Shut Down offers Force Stop… too).  The buttons follow Mac OS's state (paused
    while starting: Start Mac OS and no Cancel button, so a second Escape never dismisses the
    window); the document id names how it was opened: ``recovery-main`` (Control-Alt-Delete,
    and coming back from a pane), ``boot-options`` (Escape while Mac OS starts) and
    ``startup-options`` (Mac OS not started)."""
    tick = 1.0          # the processor bars are live
    DOC_IDS = {"recovery": "recovery-main", "boot": "boot-options", "startup": "startup-options"}

    def __init__(self, panel: "Panel", notice: str | None = None, kind: str = "recovery"):
        super().__init__(panel)
        self.notice = notice
        self.kind = kind
        self.cpu = hostinfo.HostCpu()
        self.cpu.sample()
        self.selected: str | None = None
        self.address: str | None = None
        # A Raspberry Pi card that keeps the installer beside RetroBoot (rpiboot.py): once
        # per window, not at every refresh (the boot partition is an automount).
        from .. import rpiboot
        try:
            self.installer = rpiboot.installer_beside(self.app.cfg.pi_boot_dir)
        except Exception:  # noqa: BLE001 -- an odd boot partition never breaks the window
            self.installer = None
        self.app.loop.create_task(self._load())

    async def _load(self):
        try:
            self.address = await self.app.network.primary_address()
        except Exception:  # noqa: BLE001 -- a summary line only
            self.address = None
        if self.panel.screen is self:
            await self.panel.refresh()

    def mode(self) -> str:
        sup = self.app.supervisor
        if sup.paused:
            return "paused"
        return "running" if sup.active else "stopped"

    @property
    def doc_id(self):  # type: ignore[override]
        return self.DOC_IDS[self.kind]

    # -- summaries --------------------------------------------------------------------
    def _disk_summary(self) -> str:
        app, sup = self.app, self.app.supervisor
        desired = app.settings.desired.get("machine.active")
        m = app.machines.get(desired) if desired else None
        if m is None:
            return "Choose a Mac" if app.machines.list() else "No Mac yet"
        running = sup.machine.id if sup.active and sup.machine is not None else None
        return f"{m.name} · starts next" if running and running != m.id else m.name

    def _download_summary(self) -> str:
        sources = [sid for sid, _ in self.app.catalogs.clients()]
        if "lab" in sources:
            return "From RetroBoot downloads"
        return "From the Internet" if sources else "From a USB disk"

    def _update_summary(self) -> str:
        u = self.app.updates.public()
        if not u.get("available"):
            return "Not available"
        job = self.app.jobs.holder("update")
        st = u.get("staged") or {}
        found = (u.get("check") or {}).get("found")
        if job and job.kind == "update.install":
            return "Downloading…"
        if st.get("status") == "ready":
            return f"RetroBoot {st.get('version')} is ready to install"
        if found:
            return f"RetroBoot {found['version']} is available"
        return f"RetroBoot {(u.get('current') or {}).get('version') or __version__}"

    def _network_summary(self) -> str:
        if self.app.settings.applied.get("management.web") != "on":
            return "Web management off"
        return self.address or "Not connected"

    def _display_summary(self) -> str:
        mode = (self.app.presenter.display or {}).get("mode") or []
        return f"{mode[0]} × {mode[1]}" if len(mode) >= 2 else ""

    def _bluetooth_summary(self) -> str:
        bt = self.app.bluetooth
        try:
            word = bt.state_word()
            if word != "on":
                return {"off": "Off", "blocked": "Turned off by a switch", "turning-on": "Turning on…"}.get(word, "")
            n = sum(1 for d in bt.devices() if d.get("connected"))
            return f"{n} device{'s' if n != 1 else ''} connected" if n else "On"
        except Exception:  # noqa: BLE001 -- a summary line only
            return ""

    def _time_summary(self) -> str:
        return _city(self.app.settings.applied.get("system.timezone") or "UTC")

    def _diagnostics_summary(self) -> str:
        t = {e["event"]: e["t"] for e in self.app.supervisor.timeline()}
        if "finder_detected" in t:
            return f"Mac OS started in {t['finder_detected']:.0f} s"
        return f"RetroBoot {__version__}"

    def panes(self) -> list[dict]:
        rows = [ui.row("disk", "Startup Disk", self._disk_summary(), "startup"),
                ui.row("download", "Download a Mac", self._download_summary(), "mac"),
                ui.row("update", "Software Update", self._update_summary(), "update"),
                ui.row("network", "Network & Web Access", self._network_summary(), "network"),
                ui.row("display", "Display & Sound", self._display_summary(), "display")]
        if self.app.bluetooth.capable():
            rows.append(ui.row("bluetooth", "Bluetooth", self._bluetooth_summary(), "bluetooth"))
        rows += [ui.row("timezone", "Date & Time", self._time_summary(), "clock"),
                 ui.row("diagnostics", "Diagnostics", self._diagnostics_summary(), "profiler")]
        if self.installer:
            rows.append(ui.row("installer", "Start Installer", "RetroBoot's installer on this card", "cd"))
        return rows

    # -- document ---------------------------------------------------------------------
    def about(self) -> list:
        """This computer as About This Computer shows a Mac: what it is, then how busy the Mac's
        processor and all of the computer's processors are (refreshed every second), then what
        is waiting (an update, a different Mac at the next restart)."""
        app, sup = self.app, self.app.supervisor
        cpu, cores = hostinfo.processor_name(), os.cpu_count()
        out = [ui.facts(("Version:", f"RetroBoot {__version__}"), ("Computer:", app.platform.model),
                        ("Processor:", f"{cpu}, {cores} cores" if cpu and cores else cpu),
                        ("Built-in Memory:", hostinfo.memory_text(hostinfo.memory_bytes())))]
        mac = None
        if sup.active and not sup.paused:
            try:
                mac = sup._guest_load().busy(mono(), 2.0)
            except Exception:  # noqa: BLE001 -- a bar only
                mac = None
        host = self.cpu.sample()
        m = sup.machine if sup.active and sup.machine is not None else sup.active_machine()
        idle = "Paused" if sup.paused else "Not running" if not sup.active else None
        out.append(ui.meters(
            ui.meter(m.name if m else "Mac OS", f"{round(mac * 100)}%" if mac is not None else idle, mac, "mac"),
            ui.meter(f"All {cores} processors" if cores and cores > 1 else "Processor",
                     f"{round(host * 100)}%" if host is not None else None, host, "chip")))
        u = app.updates.public()
        st, found = u.get("staged") or {}, (u.get("check") or {}).get("found")
        if st.get("status") == "ready":
            out.append(ui.text(f"RetroBoot {st.get('version')} is ready to install: open Software Update.", "small"))
        elif found:
            out.append(ui.text(f"RetroBoot {found['version']} is available: open Software Update.", "small"))
        desired = app.settings.desired.get("machine.active")
        running = sup.machine.id if sup.active and sup.machine is not None else None
        if running and desired and desired != running and app.machines.get(desired) is not None:
            out.append(ui.text(f"“{app.machines.get(desired).name}” starts the next time Mac OS restarts.", "small"))
        return out

    def doc(self):
        app, sup = self.app, self.app.supervisor
        mode = self.mode()
        m = sup.machine if sup.active and sup.machine is not None else sup.active_machine()
        frozen = unresponsive_detail(app)
        rows = self.panes()
        if self.selected not in {r["id"] for r in rows}:
            self.selected = None
        state = "Mac OS is paused" if mode == "paused" else guest_status_text(app).rstrip(".")
        items = []
        if frozen:
            items += [ui.columns(ui.icon("caution", 32), ui.stack(ui.text("Mac OS is not responding.", "emphasis"),
                                                                  ui.text(frozen, "small"))),
                      ui.buttons(ui.btn("force-shutdown", "Force Shut Down…"),
                                 ui.btn("force-restart", "Force Restart…"))]
        elif mode == "paused":
            items.append(ui.text(f"Mac OS is paused while you choose. Start Mac OS to continue starting “{m.name}”."
                                 if m else "Mac OS is paused while you choose."))
        elif mode == "stopped" and (self.notice or sup.failure):
            items.append(ui.text(self.notice or f"Last problem: {sup.failure.get('message')} "
                                                f"Evidence {sup.failure.get('evidence')}."))
        items.append(ui.icon_view("pane", rows, self.selected, columns=4, header=f"{len(rows)} items, {state}"))
        items += self.about()
        chosen = self.selected is not None
        if mode == "running":
            main = ui.btn("cancel", "Return to Mac OS", default=not chosen)
        else:
            main = ui.btn("start", "Start Mac OS", default=not chosen,
                          disabled=mode == "stopped" and sup.active_machine() is None)
        items.append(ui.buttons(ui.btn("power", "Shut Down…", align="left"),
                                ui.btn("open", "Open", default=True) if chosen else None, main))
        return ui.window(self.doc_id, "Startup Options" if mode == "paused" else "RetroBoot", items, width=540,
                         focus="pane")

    async def change(self, id, value):
        if id == "pane":
            self.selected = value or None
            await self.panel.refresh()

    async def action(self, id, values):
        p = self.panel
        if id == "pane":
            await self.open((values or {}).get("pane") or self.selected)
        elif id == "open":
            await self.open(self.selected)
        elif id in PANES:
            await self.open(id)
        elif id == "cancel":
            await p.close()
        elif id == "start":
            if self.app.supervisor.paused:
                await p.close()                      # Mac OS goes on starting
            else:
                await p.start_job(lambda: self.app.lifecycle_job("start", origin="panel"), "Starting Mac OS",
                                  self._after_start)
        elif id in ("power", "poweroff"):
            await p.show(PowerAlert(p, back=p.show_recovery))
        elif id == "force-restart":
            await p.show(ConfirmForce(p, "restart", back=self))
        elif id == "force-shutdown":
            await p.show(ConfirmForce(p, "shutdown", back=self))
        elif id == "restart":                        # (older documents and tests)
            await p.start_job(lambda: self.app.lifecycle_job("restart", origin="panel"), "Restarting Mac OS",
                              self._after_clean, allow_force=True)
        elif id == "shutdown":
            await p.start_job(lambda: self.app.lifecycle_job("shutdown", poweroff=False, origin="panel"),
                              "Shutting down", self._after_clean, allow_force=True)
        elif id in ("recovery", "setup"):
            await p.show_recovery()

    async def open(self, pane: str | None) -> None:
        p = self.panel
        self.selected = None
        if pane == "disk":
            await p.show(StartupDisk(p, back=p.show_recovery))
        elif pane == "download":
            await p.show(CatalogList(p, back=p.show_recovery, then=after_download))
        elif pane == "update":
            await p.show_software_update()
        elif pane == "network":
            await p.show(NetworkWeb(p))
        elif pane == "display":
            await p.show(DisplaySound(p))
        elif pane == "bluetooth":
            from .bluetooth import BluetoothDevices
            await p.show(BluetoothDevices(p))
        elif pane == "timezone":
            await p.show(TimeZone(p, back=p.show_recovery, title="Date & Time"))
        elif pane == "diagnostics":
            await p.show(Diagnostics(p))
        elif pane == "installer" and self.installer:
            await p.show(StartInstaller(p))

    async def _after_clean(self, job: Job):
        if job.state == SUCCEEDED:
            if self.panel.screen is not None and isinstance(self.panel.screen, JobScreen):
                await self.panel.close()
            return
        await self.panel.show(Unresponsive(self.panel, job.message))

    async def _after_start(self, job: Job):
        if job.state == SUCCEEDED:
            await self.panel.close()
        else:
            await self.panel.message("stop", "Mac OS could not start.", job.message, then=self.panel.show_recovery)


PANES = ("disk", "download", "update", "network", "display", "bluetooth", "timezone", "diagnostics", "installer")


class StartInstaller(Screen):
    """Recovery → Start Installer… on a Raspberry Pi card that keeps the installer
    (rpiboot.py): the computer restarts once into the installer (tryboot)."""
    doc_id = "start-installer"

    def doc(self):
        active = self.app.supervisor.active
        return ui.alert(self.doc_id, "caution", "Start the RetroBoot Installer?",
                        ("Mac OS shuts down first, as if you chose Shut Down. " if active else "") +
                        "The computer then restarts once into the installer on this card, where you can install "
                        "RetroBoot again or choose other Macs. If you don’t install, RetroBoot starts as before "
                        "the next time.",
                        ui.buttons(ui.btn("cancel", "Cancel", default=True), ui.btn("restart", "Restart")))

    async def action(self, id, values):
        p = self.panel
        if id == "cancel":
            await p.show_recovery()
        elif id == "restart":
            await p.start_job(lambda: start_installer_job(self.app), "Starting the installer", self._after)

    async def _after(self, job: Job):
        if job.state != SUCCEEDED:
            await self.panel.message("stop", "The installer could not be started.", job.message,
                                     then=self.panel.show_recovery)


def start_installer_job(app: "Hostd") -> Job:
    """Shut Mac OS down cleanly, then ``systemctl reboot "0 tryboot"``."""
    from .. import rpiboot
    from ..jobs import JobFailed

    async def body(job: Job):
        info = rpiboot.installer_beside(app.cfg.pi_boot_dir)
        if not info:
            raise JobFailed("The installer is no longer on this card.", None, "no-installer")
        sup = app.supervisor
        if sup.active:
            job.update(0.1, "Shutting down Mac OS…")
            await sup.job_clean(job, "shutdown", poweroff=False, purpose="host-reboot")
        app.lifecycle.event("installer_tryboot", build=info.get("build"))
        job.update(0.95, "Restarting…")
        if not await sup.reboot_host("start installer", arg=rpiboot.TRYBOOT_ARG):
            raise JobFailed("The computer could not be restarted into the installer.", None, "reboot-failed")
        return {"build": info.get("build")}
    return app.jobs.start("installer.tryboot", body, resources=("guest", "host"), preemptible=True)


def _call_job(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except TypeError:
        return fn(*args)


class CorruptDiskRecovery(Screen):
    """Alert shown when disk fsck/repair fails before launch (#48)."""
    doc_id = "corrupt-disk-recovery"
    guest_background_ok = False

    def __init__(self, panel: "Panel", info: dict):
        super().__init__(panel)
        self.info = dict(info or {})

    def _machine_id(self) -> str | None:
        return self.info.get("machine") or self.app.settings.desired.get("machine.active")

    def doc(self):
        mid = self._machine_id()
        m = self.app.machines.get(mid) if mid else None
        name = m.name if m else (mid or "This Mac")
        clean_cp = self.info.get("last_clean_checkpoint")
        fresh_cp = self.info.get("fresh_install_checkpoint")
        can_clean = bool(clean_cp) or callable(getattr(self.app, "restore_last_clean_checkpoint_job", None))
        can_fresh = bool(fresh_cp) or callable(getattr(self.app, "reset_to_fresh_install_job", None))
        detail = (
            f"The disk for “{name}” could not be repaired automatically. A backup snapshot was saved before "
            "checking the disk. Restore the last clean checkpoint, reset to a fresh install, or choose another Mac."
        )
        btns = []
        if can_clean:
            btns.append(ui.btn("restore-clean", "Restore Last Clean Checkpoint", default=True))
        if can_fresh:
            btns.append(ui.btn("reset-fresh", "Reset to Fresh Install", default=not can_clean))
        btns.append(ui.btn("disk", "Startup Disk…"))
        return ui.alert(self.doc_id, "stop", f"“{name}” disk needs recovery.", detail, ui.buttons(*btns), width=520)

    async def action(self, id, values):
        p, app = self.panel, self.app
        mid = self._machine_id()
        if id == "disk":
            await p.show(StartupDisk(p, back=p.show_startup_options))
            return
        if not mid:
            await p.show_startup_options()
            return
        if id == "restore-clean":
            fn = getattr(app, "restore_last_clean_checkpoint_job", None)
            cp = self.info.get("last_clean_checkpoint")
            if callable(fn):
                starter = lambda: _call_job(fn, mid, origin="panel")
            elif cp:
                starter = lambda: app.restore_job(mid, cp, origin="panel")
            else:
                await p.show_startup_options("No clean checkpoint is available.")
                return
            await p.start_job(starter, "Restoring last clean checkpoint", self._after)
        elif id == "reset-fresh":
            fn = getattr(app, "reset_to_fresh_install_job", None)
            cp = self.info.get("fresh_install_checkpoint")
            if callable(fn):
                starter = lambda: _call_job(fn, mid, origin="panel")
            elif cp:
                starter = lambda: app.restore_job(mid, cp, origin="panel")
            else:
                await p.show_startup_options("No fresh install checkpoint is available.")
                return
            await p.start_job(starter, "Resetting to fresh install", self._after)

    async def _after(self, job):
        p = self.panel
        if job.state != SUCCEEDED:
            await p.message("stop", "Recovery did not complete.", job.message,
                            then=lambda: p.show(CorruptDiskRecovery(p, self.info)))
            return
        if self.app.settings.desired.get("startup.autostart"):
            await p.start_job(lambda: self.app.lifecycle_job("start", origin="panel"),
                              "Starting Mac OS", self._after_start)
        else:
            await p.show_startup_options("The Mac disk was recovered.")

    async def _after_start(self, job):
        if job.state == SUCCEEDED:
            await self.panel.close()
        else:
            await self.panel.show_startup_options(job.message)


class Unresponsive(Screen):
    doc_id = "unresponsive"

    def __init__(self, panel, detail: str):
        super().__init__(panel)
        self.detail = detail

    def doc(self):
        # Three buttons: wider than the default alert, or the first one is clipped.
        return ui.alert(self.doc_id, "caution", "Mac OS is not responding.", self.detail,
                        ui.buttons(ui.btn("force-shutdown", "Force Shut Down…"),
                                   ui.btn("force-restart", "Force Restart…"),
                                   ui.btn("cancel", "Return to Mac OS", default=True)),
                        extra=[ui.text("Unsaved changes may be lost if you force Mac OS to stop.", "small")],
                        width=500)

    async def action(self, id, values):
        if id == "cancel":
            await self.panel.close()
        elif id == "force-restart":
            await self.panel.show(ConfirmForce(self.panel, "restart", back=self))
        elif id == "force-shutdown":
            await self.panel.show(ConfirmForce(self.panel, "shutdown", back=self))


class ConfirmForce(Screen):
    doc_id = "confirm-force"

    def __init__(self, panel, kind: str, back: Screen | None):
        super().__init__(panel)
        self.kind, self.back = kind, back

    def doc(self):
        if self.kind == "restart":
            msg, label = "Force Mac OS to restart?", "Force Restart"
        else:
            msg, label = "Force Mac OS to stop and shut down this computer?", "Force Shut Down"
        return ui.alert(self.doc_id, "stop", msg,
                        "Unsaved changes may be lost. Use this only if Mac OS is not responding.",
                        ui.buttons(ui.btn("cancel", "Cancel", default=True), ui.btn("force", label)))

    async def action(self, id, values):
        if id == "cancel":
            if self.back:
                await self.panel.show(self.back)
            else:
                await self.panel.close()
            return
        if self.kind == "restart":
            await self.panel.start_job(lambda: self.app.lifecycle_job("force-restart", origin="panel"),
                                       "Forcing Mac OS to restart", self._after)
        else:
            await self.panel.start_job(lambda: self.app.lifecycle_job("force-stop", poweroff=True, origin="panel"),
                                       "Forcing Mac OS to stop", self._after)

    async def _after(self, job: Job):
        eid = (job.result or {}).get("evidence") if isinstance(job.result, dict) else None
        if job.state == SUCCEEDED:
            if self.kind == "restart":
                await self.panel.close()
            return
        await self.panel.message("stop", "The emulator could not be stopped.",
                                 f"{job.message} Evidence {eid}." if eid else job.message,
                                 then=self.panel.show_recovery)


class FailureAlert(Screen):
    doc_id = "failure"

    def __init__(self, panel, message, detail, evidence):
        super().__init__(panel)
        self.message, self.detail, self.evidence = message, detail, evidence

    def doc(self):
        extra = [ui.text(f"Evidence ID: {self.evidence}", "small")] if self.evidence else []
        if getattr(self.app.supervisor, "unavailable", None):
            # A Mac that can't start here (a 68k Mac without its emulator or ROM): another Mac,
            # or try again once the ROM was added on the web page.
            buttons = ui.buttons(ui.btn("disk", "Startup Disk…"), ui.btn("restart", "Try Again", default=True))
        else:
            buttons = ui.buttons(ui.btn("recovery", "Recovery…"), ui.btn("restart", "Restart", default=True))
        return ui.alert(self.doc_id, "stop", self.message, self.detail, buttons, extra=extra)

    async def action(self, id, values):
        if id == "recovery":
            await self.panel.show_recovery()
        elif id == "disk":
            await self.panel.show(StartupDisk(self.panel, back=self.panel.show_recovery))
        elif id == "restart":
            await self.panel.start_job(lambda: self.app.lifecycle_job("start", origin="panel"), "Starting Mac OS",
                                       self._after)

    async def _after(self, job: Job):
        if job.state == SUCCEEDED:
            await self.panel.close()
        else:
            sup = self.app.supervisor
            if not isinstance(self.panel.screen, FailureAlert):
                f = sup.failure or {}
                await self.panel.show_failure("Mac OS could not start.", f.get("message") or job.message,
                                              f.get("evidence"))


class Stopped(Screen):
    doc_id = "stopped"

    def __init__(self, panel, message):
        super().__init__(panel)
        self.message = message

    def doc(self):
        return ui.alert(self.doc_id, "note", self.message, "Start Mac OS again, or power off this computer.",
                        ui.buttons(ui.btn("poweroff", "Power Off"), ui.btn("start", "Start Up", default=True)))

    async def action(self, id, values):
        if id == "start":
            await self.panel.start_job(lambda: self.app.lifecycle_job("start", origin="panel"), "Starting Mac OS",
                                       self._after)
        elif id == "poweroff":
            await self.app.supervisor.poweroff("panel")
        elif id == "recovery":
            await self.panel.show_recovery()

    async def _after(self, job):
        if job.state == SUCCEEDED:
            await self.panel.close()
        else:
            await self.panel.message("stop", "Mac OS could not start.", job.message, then=self.panel.show_recovery)


class PowerAlert(Screen):
    """The power question, as Mac OS asks it when you press the power key: Restart, Cancel or
    Shut Down (Mac OS shuts down cleanly first and the computer turns off).  ``back``: where
    Cancel goes (the RetroBoot window); without it Cancel closes the panel."""
    doc_id = "power"

    def __init__(self, panel, back: Callable[[], Awaitable[None]] | None = None):
        super().__init__(panel)
        self.back = back

    def doc(self):
        machine = getattr(self.app.supervisor, "machine", None)
        osx = bool(getattr(machine, "osx", False))
        return ui.alert(self.doc_id, "caution", "Do you want to shut down this computer?",
                        ("Mac OS X shuts down first, as if you chose Shut Down from the Apple menu." if osx else
                         "Mac OS shuts down first, as if you chose Shut Down from the Special menu.")
                        if self.app.supervisor.active else None,
                        ui.buttons(ui.btn("restart", "Restart"), ui.btn("cancel", "Cancel"),
                                   ui.btn("shutdown", "Shut Down", default=True)))

    async def action(self, id, values):
        sup = self.app.supervisor
        if id == "cancel":
            await (self.back() if self.back else self.panel.close())
        elif sup.active or sup.launch_pending:
            # A running (or about to start) Mac OS gets the ordinary clean path.
            kind = "restart" if id == "restart" else "shutdown"
            await self.panel.start_job(lambda: self.app.lifecycle_job(kind, poweroff=True, origin="panel"),
                                       "Shutting down" if kind == "shutdown" else "Restarting Mac OS",
                                       self._after, allow_force=True)
        elif id == "shutdown":
            await sup.poweroff("panel")
        elif id == "restart":
            await sup.reboot_host("panel")

    async def _after(self, job):
        if job.state != SUCCEEDED:
            await self.panel.show(Unresponsive(self.panel, job.message))


class KeepSetting(Screen):
    doc_id = "keep-setting"
    tick = 1.0
    guest_background_ok = False

    def __init__(self, panel, previous: Screen | None):
        super().__init__(panel)
        self.previous = previous

    def doc(self):
        pc = self.app.settings.pending_confirm_public()
        remaining = int(round(pc["remaining"])) if pc else 0
        # Revert is the default: if the new mode is unreadable, Return (or waiting) restores it.
        return ui.alert(self.doc_id, "caution", "Keep this display setting?",
                        f"The display returns to its previous setting in {remaining} seconds unless you click Keep.",
                        ui.buttons(ui.btn("keep", "Keep"), ui.btn("cancel", "Revert", default=True)))

    async def on_tick(self):
        if not self.app.settings.pending_confirm:
            return
        await self.panel.refresh()

    async def action(self, id, values):
        if id == "keep":
            try:
                await self.app.apply_engine.confirm("panel")
            except NothingToConfirm:
                if self.previous is not None:
                    self.previous.sync_from_settings()
                await self.panel.message(
                    "note", "The display setting was already restored.",
                    "Time ran out before Keep was clicked, so the previous setting is back.",
                    then=(lambda: self.panel.show(self.previous)) if self.previous else None,
                )
                return
            except Exception:
                log.exception("panel setting confirm failed")
                if self.previous is not None:
                    self.previous.sync_from_settings()
                    await self.panel.show(self.previous)
                else:
                    await self.panel.close()
        else:
            await self.app.apply_engine.revert_now("panel")
            if self.previous:
                self.previous.sync_from_settings()
                await self.panel.show(self.previous)
            else:
                await self.panel.close()


class Diagnostics(Screen):
    doc_id = "diagnostics"

    def __init__(self, panel):
        super().__init__(panel)
        self.addresses: list[str] = []
        self.app.loop.create_task(self._load())

    async def _load(self):
        self.addresses = await self.app.network.all_addresses()
        if self.panel.screen is self:
            await self.panel.refresh()

    def doc(self):
        app = self.app
        sup = app.supervisor
        pkg = app.package.summary()
        hello = app.presenter.hello or {}
        timeline = {e["event"]: e["t"] for e in sup.timeline()}
        disp = app.presenter.display or {}
        mode = disp.get("mode") or []
        if not hello:
            display = "not connected"
        elif len(mode) >= 3:
            # Same wording as the installer's display check (the protocol version is not shown).
            display = f"{mode[0]} × {mode[1]} at {mode[2]} Hz, scale {disp.get('suggested_scale', 1)}×"
        else:
            display = "no display mode"
        lines = [f"RetroBoot {__version__}",
                 *(f"{k}: {v}" for k, v in app.computer_rows().items()),
                 f"Emulator: QEMU {pkg.get('qemu') or 'not installed'} "
                 f"({(pkg.get('files') or {}).get('bin/qemu-system-ppc', '')[:12]})",
                 f"Display: {display}",
                 f"Mac: {sup.machine.name if sup.machine else 'none'} · {guest_status_text(app)}"]
        if "guest_content" in timeline:
            lines.append(f"Startup: Mac OS screen at {timeline['guest_content']:.1f} s"
                         + (f", Finder at {timeline['finder_detected']:.1f} s" if "finder_detected" in timeline else
                            f", started at {timeline['mac_os_started']:.1f} s" if "mac_os_started" in timeline else ""))
        lines.append("Addresses: " + (", ".join(self.addresses) or "none"))
        last = app.diagnostics.last_evidence
        lines.append(f"Last evidence ID: {last or 'none'}")
        items = [ui.text(l) for l in lines]
        items.append(ui.buttons(ui.btn("capture", "Capture Evidence"), ui.btn("cancel", "Done", default=True)))
        return ui.window(self.doc_id, "Diagnostics", items, width=520)

    async def action(self, id, values):
        if id == "cancel":
            await self.panel.show_recovery()
        elif id == "capture":
            await self.panel.start_job(lambda: self.app.bundle_job(origin="panel"), "Capturing evidence", self._after)

    async def _after(self, job):
        if job.state == SUCCEEDED:
            await self.panel.message("note", "Evidence captured.",
                                     f"Evidence ID {job.result['id']}. Download it from the web interface "
                                     "under Diagnostics.", then=lambda: self.panel.show(Diagnostics(self.panel)))
        else:
            await self.panel.message("caution", "Evidence could not be captured.", job.message,
                                     then=lambda: self.panel.show(Diagnostics(self.panel)))


class NetworkWeb(Screen):
    doc_id = "network-web"
    tick = 5.0
    pairing_visible = True

    def __init__(self, panel, back: Callable[[], Awaitable[None]] | None = None):
        super().__init__(panel)
        self.back = back
        self.addresses: list[str] = []
        self.primary: str | None = None
        self.wifi_hw: dict | None = None
        self.app.pairing.current_code()
        self.app.loop.create_task(self._load())

    async def _load(self):
        self.addresses = await self.app.network.all_addresses()
        self.primary = await self.app.network.primary_address()
        try:
            self.wifi_hw = await self.app.network.wifi_hardware()
        except Exception:
            self.wifi_hw = None
        if self.panel.screen is self:
            await self.panel.refresh()

    async def on_tick(self):
        await self._load()

    def doc(self):
        app = self.app
        web_on = app.settings.applied["management.web"] == "on"
        host = app.settings.applied["management.hostname"]
        url = app.web.url_for(self.primary) if self.primary else None
        items = []
        if not web_on:
            items.append(ui.text("Web management is off.", "emphasis"))
        elif not url:
            items.append(ui.text("This computer is not connected to a network.", "emphasis"))
            items.append(ui.text("Connect an Ethernet cable or join a Wi-Fi network."))
        else:
            code, left = app.pairing.current_code()
            fp_lines = ui.format_fingerprint(app.web.fingerprint)
            info = [ui.text("On your phone or computer, open:"), ui.text(url, "emphasis"),
                    ui.text(f"or https://{app.mdns.display_name(host)}/", "small"),
                    ui.text("Pairing code:"), ui.text(" ".join(code), "large"),
                    ui.text(f"Valid for {max(1, int(left // 60 + (1 if left % 60 else 0)))} more minutes.", "small")]
            if fp_lines:
                info.append(ui.text("Certificate SHA-256:", "small"))
                info += [ui.text(line, "small") for line in fp_lines]
            matrix = qr_matrix(url)
            items.append(ui.columns(ui.group("Web Access", info), ui.qr(matrix)) if matrix else ui.group("Web Access", info))
            if len(self.addresses) > 1:
                items.append(ui.text("Other addresses: " + ", ".join(a for a in self.addresses if a != self.primary),
                                     "small"))
            items.append(ui.text(f"Paired devices: {len(app.pairing.devices())}", "small"))
        if self.wifi_hw or app.capabilities().get("wifi"):
            items.append(ui.text("AirPort: Join Wi-Fi with Wi-Fi…; Mac OS AirPort shares this host connection.", "small"))
        items.append(ui.checkbox("web", "Allow web management", web_on))
        items.append(ui.buttons(ui.btn("wifi", "Wi-Fi…"), ui.btn("webhelper", "Web Helper…"),
                                ui.btn("newcode", "New Code", disabled=not web_on),
                                ui.btn("cancel", "Done", default=True)))
        return ui.window(self.doc_id, "Network & Web Access", items, width=560)

    async def change(self, id, value):
        if id == "web":
            await self.app.apply_engine.change_and_apply({"management.web": "on" if value else "off"})
            await self.panel.refresh()

    async def action(self, id, values):
        if id == "cancel":
            await (self.back() if self.back else self.panel.show_recovery())
        elif id == "newcode":
            self.app.pairing.current_code(regenerate=True)
            await self.panel.refresh()
        elif id == "wifi":
            await self.panel.show(WiFi(self.panel, back=lambda: self.panel.show(NetworkWeb(self.panel, self.back))))
        elif id == "webhelper":
            await self.panel.show(WebHelper(self.panel, back=lambda: self.panel.show(NetworkWeb(self.panel, self.back))))


class WebHelper(Screen):
    """Modern Web Helper (retroboot/webproxy.py): on/off, view, old home pages, search,
    local network.  Everything applies at once; Mac OS keeps running.  The redirect list
    itself is edited on the web page (Network)."""

    doc_id = "web-helper"
    tick = 5.0
    KEYS = ("network.web_proxy", "network.web_proxy_view", "network.web_proxy_homepages",
            "network.web_proxy_search", "network.web_proxy_lan")

    def __init__(self, panel, back=None):
        super().__init__(panel)
        self.back = back
        self.values = {k: self.app.settings.desired.get(k) for k in self.KEYS}

    async def on_tick(self):
        await self.app.webproxy.refresh()
        if self.panel.screen is self:
            await self.panel.refresh()

    def status(self) -> list:
        wp = self.app.webproxy.public()
        state = wp.get("state")
        if not wp.get("available"):
            return [ui.text("This RetroBoot does not include the Modern Web Helper.", "emphasis")]
        if state == "running":
            pages = ((wp.get("stats") or {}).get("counts") or {}).get("pages") or 0
            detail = "Mac OS's browsers open today's web, including secure sites, through this computer."
            if pages:
                detail += f" {pages} {'page' if pages == 1 else 'pages'} so far."
            return [ui.text("Modern Web Helper is on.", "emphasis"), ui.text(detail, "small")]
        if state == "failed":
            return [ui.text("The Modern Web Helper is not working.", "emphasis"),
                    ui.text(wp.get("error") or "RetroBoot keeps trying to start it again.", "small")]
        if state in ("starting", "stopping"):
            return [ui.text("Modern Web Helper is starting…" if state == "starting" else "Modern Web Helper is stopping…",
                            "emphasis")]
        return [ui.text("Modern Web Helper is off.", "emphasis"),
                ui.text("Mac OS connects to web sites directly. Most of today's sites won't open in its browsers.",
                        "small")]

    def doc(self):
        v = self.values
        available = self.app.webproxy.available()
        on = v["network.web_proxy"] == "on"
        items = self.status() + [
            ui.checkbox("network.web_proxy", "Use the Modern Web Helper", on, disabled=not available),
            ui.group("Web Pages", [
                ui.popup("network.web_proxy_view", "Page view:",
                         list(schema.BY_KEY["network.web_proxy_view"].choice_labels.items()),
                         v["network.web_proxy_view"], disabled=not (available and on)),
                ui.checkbox("network.web_proxy_homepages", "Open old home pages at System 7 Today",
                            bool(v["network.web_proxy_homepages"]), disabled=not (available and on)),
                ui.popup("network.web_proxy_search", "Search page:",
                         list(schema.BY_KEY["network.web_proxy_search"].choice_labels.items()),
                         v["network.web_proxy_search"], disabled=not (available and on)),
                ui.checkbox("network.web_proxy_lan", "Allow pages on the local network",
                            bool(v["network.web_proxy_lan"]), disabled=not (available and on)),
            ]),
            ui.text("Changes take effect at once; Mac OS keeps running. The list of old home pages "
                    "is on the web page, under Network.", "small"),
            ui.buttons(ui.btn("cancel", "Cancel"), ui.btn("ok", "OK", default=True, disabled=not available)),
        ]
        return ui.window(self.doc_id, "Modern Web Helper", items, width=520)

    async def change(self, id, value):
        if id not in self.values:
            return
        if id == "network.web_proxy":
            value = "on" if value in (True, "on", "true", 1) else "off"
        elif id in ("network.web_proxy_homepages", "network.web_proxy_lan"):
            value = value in (True, "on", "true", 1)
        changed = self.values[id] != value
        self.values[id] = value
        if changed and id == "network.web_proxy":
            await self.panel.refresh()       # the other controls follow the switch

    async def action(self, id, values):
        back = self.back or self.panel.show_recovery
        if id == "cancel":
            await back()
            return
        for k, v in (values or {}).items():
            await self.change(k, v)
        changes = {k: v for k, v in self.values.items() if v != self.app.settings.desired.get(k)}
        if not changes:
            await back()
            return
        try:
            await self.app.apply_engine.change_and_apply(changes, origin="panel")
        except schema.ValidationError as exc:
            await self.panel.message("caution", "Some settings are not valid.", "; ".join(exc.fields.values()),
                                     then=lambda: self.panel.show(self))
            return
        await back()


def wifi_secured(net: dict) -> bool:
    return str(net.get("security") or "open").strip().lower() not in ("", "open", "--", "none")


def wifi_bars(signal: Any) -> int:
    """Signal bars as the web page shows them (1-4 for any network the scan saw)."""
    try:
        s = int(signal)
    except (TypeError, ValueError):
        return 0
    return 4 if s > 75 else 3 if s > 50 else 2 if s > 25 else 1


def _profile_ssid(ssid: str) -> str:
    """How network.py names RetroBoot's own profiles ("RetroBoot " + this), to recognise them."""
    return re.sub(r"[^A-Za-z0-9 _.-]", "_", ssid)[:40]


class WiFi(Screen):
    """Wi-Fi networks, AirPort-menu style: the joined network has a check mark and comes first,
    and one click on another network joins it (a password dialog for a secured one)."""
    doc_id = "wifi"

    def __init__(self, panel, back, joined: str | None = None, networks: list[dict] | None = None):
        super().__init__(panel)
        self.back = back
        self.state: dict | None = None
        self.networks: list[dict] = list(networks or [])
        self.selected: str | None = None   # chosen with the keyboard (or a join that was cancelled)
        self.joined = joined               # just joined: trust it until a scan says otherwise
        self.busy = True
        self.error: str | None = None
        self.app.loop.create_task(self._load(scan=True))

    async def _load(self, scan: bool):
        self.busy = True
        try:
            self.state = await self.app.network.wifi_state()
            if self.state["available"] and scan:
                self.networks = await self.app.network.scan()
                self.error = None
                if any(n.get("in_use") for n in self.networks):
                    self.joined = None
        except NetworkError as exc:
            self.error = str(exc)
        self.busy = False
        if self.panel.screen is self:
            await self.panel.refresh()

    def connected(self) -> str | None:
        """SSID of the network this computer is on now, if any."""
        if self.joined:
            return self.joined
        for n in self.networks:
            if n.get("in_use"):
                return n["ssid"]
        st = self.state or {}
        cur = st.get("ssid") if st.get("connected") else None
        if not cur:
            return None
        for n in self.networks:
            if n["ssid"] == cur or _profile_ssid(n["ssid"]) == cur:
                return n["ssid"]
        return cur

    def ordered(self) -> list[dict]:
        cur = self.connected()
        return sorted(self.networks, key=lambda n: (n["ssid"] != cur, -int(n.get("signal") or 0), n["ssid"].lower()))

    def find(self, net_id: str | None) -> dict | None:
        return next((n for n in self.networks if n["id"] == net_id), None)

    def pending(self) -> dict | None:
        """A selected network that is not the joined one: the default button joins it."""
        net = self.find(self.selected)
        return net if net and net["ssid"] != self.connected() else None

    def doc(self):
        items = []
        avail = bool(self.state and self.state["available"])
        if self.state is None:
            items.append(ui.text("Looking for Wi-Fi networks…"))
            items.append(ui.progress(None))
        elif not avail:
            items.append(ui.text("No Wi-Fi hardware", "emphasis"))
            items.append(ui.text("This computer has no Wi-Fi adapter. Use an Ethernet connection instead."))
        else:
            cur = self.connected()
            status = f"Connected to “{cur}”." if cur else "Not connected to a Wi-Fi network."
            if self.networks:
                hint = "Click a network to join it."
            elif self.busy:
                hint = "Looking for networks…"
            else:
                hint = "No networks found. Click Scan Again to look again."
            items.append(ui.columns(ui.icon("wifi", 32), ui.stack(ui.text(status, "emphasis"), ui.text(hint))))
            rows = [ui.row(n["id"], n["ssid"], checked=n["ssid"] == cur, signal=wifi_bars(n.get("signal")),
                           lock=wifi_secured(n))
                    for n in self.ordered()]
            items.append(ui.lst("network", rows, self.selected if self.find(self.selected) else None, rows=6,
                                activate="click"))
            if self.busy:
                items.append(ui.progress(None))
            if self.error:
                items.append(ui.text(self.error, "small"))
        scan = ui.btn("scan", "Scan Again", disabled=not avail or self.busy, align="left")
        if self.pending():
            buttons = ui.buttons(scan, ui.btn("cancel", "Done"), ui.btn("join", "Join…", default=True))
        else:
            buttons = ui.buttons(scan, ui.btn("cancel", "Done", default=True))
        items.append(buttons)
        return ui.window(self.doc_id, "Wi-Fi", items, width=480)

    async def change(self, id, value):
        if id == "network":
            had = self.pending()
            self.selected = value
            if bool(had) != bool(self.pending()):
                await self.panel.refresh()

    async def action(self, id, values):
        if id == "cancel":
            await self.back()
        elif id == "scan":
            self.app.loop.create_task(self._load(scan=True))
            await self.panel.refresh()
        elif id in ("network", "join"):
            # "network": one click on a row; "join": Return or the Join… button.
            net = self.find(values.get("network") or self.selected)
            if not net:
                return
            if net["ssid"] == self.connected():
                self.selected = None   # already joined: nothing to do
                await self.panel.refresh()
                return
            self.selected = net["id"]
            if wifi_secured(net):
                await self.panel.show(WiFiJoin(self.panel, net, self))
            else:
                await join_wifi(self, net, None)

    async def reopen(self, joined: str | None = None) -> None:
        """Back to a freshly scanned list (after a join, so the check mark moves)."""
        fresh = WiFi(self.panel, self.back, joined=joined, networks=self.networks)
        fresh.state = fresh.state or self.state
        await self.panel.show(fresh)


async def join_wifi(wifi: WiFi, net: dict, psk: str | None) -> None:
    """Join with progress, then say how it went before returning to the list."""
    panel, app, ssid = wifi.panel, wifi.app, net["ssid"]
    app.redactor.add(psk)

    async def after(job):
        if job.state == SUCCEEDED:
            await panel.message("note", f"Connected to “{ssid}”.",
                                "RetroBoot will join this network automatically from now on.",
                                then=lambda: wifi.reopen(joined=ssid))
            return
        detail = job.message if job.message and job.message != "Could not join the network." else None
        if wifi_secured(net):
            retry = lambda: panel.show(WiFiJoin(panel, net, wifi))  # noqa: E731 - try the password again
        else:
            retry = lambda: panel.show(wifi)  # noqa: E731
        await panel.message("caution", f"Could not join “{ssid}”.", detail, then=retry)

    await panel.start_job(lambda: app.wifi_job("connect", ssid=ssid, psk=psk, origin="panel"),
                          f"Joining “{ssid}”…", after)


class WiFiJoin(Screen):
    """The password dialog. The password is never put back into a document: "Show password"
    is drawn by the panel itself (checkbox "reveals"), and hostd only reads it on Join."""
    doc_id = "wifi-join"

    def __init__(self, panel, net: dict, wifi: WiFi):
        super().__init__(panel)
        self.net, self.wifi = net, wifi
        self.error: str | None = None

    def doc(self):
        ssid = self.net["ssid"]
        items = [ui.columns(ui.icon("wifi", 32),
                            ui.stack(ui.text(f"The network “{ssid}” requires a password.", "emphasis"),
                                     ui.text("Type the password, then click Join."))),
                 ui.field("psk", "Password:", "", secure=True, max_length=64),
                 ui.checkbox("show", "Show password", False, reveals="psk")]
        if self.error:
            items.append(ui.text(self.error, "small"))
        items.append(ui.buttons(ui.btn("cancel", "Cancel"), ui.btn("join", "Join", default=True)))
        return ui.window(self.doc_id, "Wi-Fi", items, width=440, focus="psk")

    async def action(self, id, values):
        if id == "cancel":
            await self.panel.show(self.wifi)
            return
        if id != "join":
            return
        psk = values.get("psk") or ""
        if not psk:
            self.error = "Type the password for this network."
        elif not (8 <= len(psk) <= 63 or len(psk) == 64):
            self.error = "Wi-Fi passwords are 8 to 63 characters."
        else:
            self.error = None
        if self.error:
            await self.panel.refresh()   # same document: the panel keeps what was typed
            return
        await join_wifi(self.wifi, self.net, psk)


SIZE_LABELS = {"auto": "Best for this display"}
STARTUP_CHIME_LABELS = {
    "auto": "Automatic (by Mac model)",
    "os9": "Mac OS 9",
    "quadra": "Quadra",
    "powermac": "Power Mac",
    "macii": "Mac II",
    "se": "Macintosh SE",
    "off": "Off",
}
GAMEPAD_MODE_LABELS = {
    "arrows": "Arrow keys",
    "wasd": "WASD",
    "off": "Off",
}
_OPTIONAL_SETTING_TYPES = {
    "audio.startup_chime": str,
    "audio.output": str,
    "input.gamepad_mode": str,
    "input.natural_scroll": bool,
}


def _choice_items(key: str, fallback: dict[str, str]) -> list[tuple[str, str]]:
    s = schema.BY_KEY.get(key)
    if s and s.choice_labels:
        return [(str(k), str(v)) for k, v in s.choice_labels.items()]
    if s and s.choices:
        return [(str(c), fallback.get(str(c), str(c))) for c in s.choices]
    return list(fallback.items())


class DisplaySound(Screen):
    """Display & Sound, laid out like Mac OS 9's Monitors control panel (D051, phase 2): its
    display picture and bevel buttons across the top (Monitor, Sound, Keyboard pages), and
    Sound's Main Volume along the bottom.  The volume and Mute apply at once, as in Mac OS;
    everything else waits for Apply (and the 15-second question for a new display mode)."""
    doc_id = "display-sound"
    PAGES = ("monitor", "sound", "keyboard", "pi-display")
    HEIGHT = 409   # the Monitor page on a desktop (laptops and tilt sensors add rows and grow it)

    def __init__(self, panel, back=None, page: str = "monitor"):
        super().__init__(panel)
        self.back = back
        self.page = page if page in self.PAGES else "monitor"
        self.values = dict(self.app.settings.desired)
        self.pi_display = self.app.rpi_display.public()
        self.pi_profile = self.pi_display.get("pending") or self.pi_display.get("trial") or self.pi_display.get("profile") or "auto"
        speakers = getattr(self.app, "speakers", None)
        # Gemini PDA: speakers or headphones only (a hostd state, not a setting: speakers.py)
        self.speaker_output = speakers.output if speakers is not None and speakers.enabled else None
        self.error: str | None = None

    def sync_from_settings(self) -> None:
        self.values["display.host_mode"] = self.app.settings.desired.get("display.host_mode", "auto")

    def _sound_outputs(self) -> list[tuple[str, str]]:
        cur = str(self.values.get("audio.output") or "auto")
        fn = getattr(self.app, "list_audio_outputs", None)
        items: list[dict] = []
        try:
            if callable(fn):
                items = list(fn() or [])
            else:
                from .. import audio
                if hasattr(audio, "list_outputs"):
                    items = list(audio.list_outputs(active_output=cur) or [])
        except Exception:
            items = []
        opts: list[tuple[str, str]] = []
        seen: set[str] = set()
        for o in items:
            if not isinstance(o, dict):
                continue
            oid = str(o.get("id") or "")
            if not oid or (not o.get("available", True) and oid != cur) or oid in seen:
                continue
            seen.add(oid)
            opts.append((oid, str(o.get("label") or oid)))
        if "auto" not in seen:
            opts.insert(0, ("auto", "Automatic"))
            seen.add("auto")
        if cur not in seen:
            opts.append((cur, cur))
        return opts

    async def _test_sound(self) -> None:
        output = str(self.values.get("audio.output") or "auto")
        chime = str(self.values.get("audio.startup_chime") or "auto")
        vol = self.app.settings.applied.get("audio.volume", 70)
        laptop = getattr(self.app, "laptop", None)
        muted = bool(laptop.muted) if laptop is not None and getattr(laptop, "can_mute", False) else False
        sup = self.app.supervisor
        m = self.app.machines.get(self.values.get("machine.active"))
        family = "quadra800" if m is not None and m.quadra else "powermac"
        res: dict | None = None
        try:
            if callable(getattr(self.app, "test_sound", None)):
                got = self.app.test_sound(output=output)
                res = await got if inspect.isawaitable(got) else got
            elif callable(getattr(sup, "test_sound", None)):
                got = sup.test_sound(output=output)
                res = await got if inspect.isawaitable(got) else got
            else:
                from .. import audio
                if hasattr(audio, "test_sound"):
                    res = await asyncio.to_thread(
                        audio.test_sound, output=output, chime_setting=chime, family=family, volume=vol, muted=muted
                    )
        except Exception as exc:
            self.error = f"Sound test failed: {exc}"
            await self.panel.refresh()
            return
        if isinstance(res, dict) and res.get("ok") is False:
            self.error = res.get("error") or "The sound test could not play on this output."
        else:
            self.error = None
        await self.panel.refresh()

    def _monitor_page(self, v: dict, caps: dict) -> list:
        app = self.app
        mode = app.display_mode()
        # Monitors' Color Depth group: Sharp/Smooth beside the pixel sizes (each with the Mac
        # screen it gives on this display; sizes the display can't use are dimmed).
        choices = schema.pixel_scale_choices(schema.ValidationContext(display_mode=mode))
        sizes = [ui.row(c["value"], "Automatic" if c["value"] == "auto" else c["label"].split(" — ")[0] +
                        (f"  {c['size'][0]} × {c['size'][1]}" if c.get("size") and not c.get("disabled") else ""),
                        disabled=bool(c.get("disabled"))) for c in choices]
        auto = next((c["label"] for c in choices if c["value"] == "auto"), "")
        note = schema.fixed_size_note(v, mode) or (auto.replace("Automatic (", "Automatic: ").rstrip(")")
                                                   if v["display.pixel_scale"] == "auto" and "(" in auto else None)
        pixel = ui.group("Pixel Size", [
            ui.columns(dict(ui.radio("display.scaling", "", [("integer", "Sharp"), ("fit", "Smooth")], v["display.scaling"]),
                            width=84),
                       ui.lst("display.pixel_scale", sizes, v["display.pixel_scale"], rows=5, scrollbar=False)),
            ui.text(note, "small") if note else None])
        # Monitors' Resolution group: "Show:" becomes this computer's display mode, above
        # the Mac screen sizes.
        modes = app.host_modes()
        mode_opts = [("auto", "Automatic")] + [(m, m.replace("x", " × ").replace("@", " @ ") + " Hz") for m in modes]
        if v["display.host_mode"] not in dict(mode_opts):
            mode_opts.append((v["display.host_mode"], v["display.host_mode"]))
        main_display = None
        if caps.get("internal_display"):
            # Laptops: which display Mac OS takes its screen size from (the other mirrors it).
            outputs = (app.presenter.display or {}).get("outputs") or []
            external = any(not o.get("internal") for o in outputs)
            main_display = ui.popup("display.primary", "Main:", list(schema.BY_KEY["display.primary"].choice_labels.items()),
                                    v.get("display.primary", "internal"), disabled=not external)
        screens = [ui.row(s, SIZE_LABELS.get(s, s.replace("x", " × "))) for s in schema.GUEST_SIZES]
        resolution = ui.group("Resolution", [
            main_display,
            ui.text("Connect an external display to choose it.", "small")
            if main_display is not None and main_display.get("disabled") else None,
            ui.popup("display.host_mode", "Display:", mode_opts, v["display.host_mode"],
                     disabled=not caps.get("display_modes")),
            ui.lst("display.guest_size", screens, v["display.guest_size"], rows=len(screens), scrollbar=False)])
        # 3D acceleration (ARCHITECTURE 4.9): offered only when this computer, the emulator and
        # this Mac's disk can use it; otherwise disabled with the reason.  A 68k startup Mac
        # (quadra.py) never has the card: the choice is left out.
        gfx = app.graphics.ui_context()
        startup_mac = app.machines.get(v.get("machine.active"))
        quadra = startup_mac is not None and startup_mac.quadra
        ati_on = v.get("display.graphics", "standard") == "ati-radeon"
        if not gfx.get("available"):
            gfx_note = gfx.get("message")
        elif getattr(startup_mac, "osx", False):
            gfx_note = "Quartz Extreme and OpenGL. Choose the desktop size in Mac OS X Displays."
        elif ati_on and (app.presenter.display or {}).get("mode"):
            from .. import graphics as gfxpolicy
            w, h = gfxpolicy.mac_screen(app.presenter.display, v, app.graphics.exact())   # with the sizes chosen here
            gfx_note = f"Mac OS uses {w} × {h} with the ATI Radeon. Takes effect when Mac OS restarts."
        else:
            gfx_note = "For 3D games and programs. Takes effect when Mac OS restarts."
        rotation = [ui.popup("display.rotation", "Rotation:", list(schema.ROTATION_LABELS.items()),
                             str(v.get("display.rotation", "auto")))]
        if caps.get("accelerometer"):   # rotation (ARCHITECTURE 4.5): Rotation Lock only with a tilt sensor
            rotation.append(ui.checkbox("display.rotation_lock", "Rotation Lock", bool(v.get("display.rotation_lock")),
                                        disabled=v.get("display.rotation", "auto") != "auto"))
        graphics = None if quadra else ui.stack(
            ui.checkbox("display.graphics", "3D acceleration (ATI Radeon)", ati_on,
                        disabled=not gfx.get("available") and not ati_on),
            ui.text(gfx_note, "small") if gfx_note else None)
        return [ui.columns(pixel, resolution), ui.columns(ui.stack(*rotation), graphics)]

    def _pi_display_page(self, v: dict, caps: dict) -> list:
        d = self.app.rpi_display.public()
        if not d.get("available"):
            return [ui.text(d.get("message") or "Display presets are unavailable on this startup card.")]
        labels = {p["id"]: p["label"] for p in d.get("profiles", [])}
        note = next((p.get("note") for p in d.get("profiles", []) if p["id"] == self.pi_profile), "")
        items = [ui.popup("pi-display.profile", "Display:", list(labels.items()), self.pi_profile,
                          disabled=bool(d.get("trial"))),
                 ui.text(note, "small") if note else None]
        if d.get("trial"):
            items += [ui.text("Trying " + labels.get(d["trial"], d["trial"]) + "."),
                      ui.text("Click Keep Display to use it at every start. Otherwise the next start uses the previous display.", "small"),
                      ui.buttons(ui.btn("pi-display.previous", "Use Previous Next Start"),
                                 ui.btn("pi-display.keep", "Keep Display", default=True))]
        else:
            items += [ui.text("Choose a display and click Try Next Start. Restart Computer to try it.", "small"),
                      ui.buttons(ui.btn("pi-display.try", "Try Next Start"),
                                 ui.btn("pi-display.restart", "Restart Computer", disabled=not d.get("restart_required")))]
        if d.get("restart_required"):
            items.append(ui.text("A computer restart is required. Restarting Mac OS alone does not change the display.", "small"))
        items.append(ui.text("If startup stops before RetroBoot opens, use another computer to copy auto.txt from retroboot/displays to retroboot/display.txt on the SD card.", "small"))
        return [ui.group("Raspberry Pi Display", items)]

    def _sound_page(self, v: dict, caps: dict) -> list:
        chime_opts = _choice_items("audio.startup_chime", STARTUP_CHIME_LABELS)
        output_opts = self._sound_outputs()
        return [ui.group("Sound Output", [
            ui.checkbox("audio.enabled", "Play Mac OS sound", v["audio.enabled"]),
            ui.text("Takes effect when Mac OS restarts.", "small"),
            ui.popup("audio.startup_chime", "Startup chime:", chime_opts, str(v.get("audio.startup_chime", "auto"))),
            ui.popup("audio.output", "Sound output:", output_opts, str(v.get("audio.output", "auto"))),
            ui.popup("speakers.output", "Output:", [("speaker", "Speakers and headphones"),
                                                    ("headphones", "Headphones only")], self.speaker_output)
            if self.speaker_output else None,
            ui.buttons(ui.btn("test-sound", "Test Sound", align="left"))])]

    def _keyboard_page(self, v: dict, caps: dict) -> list:
        swap = bool(v.get("input.swap_command_option"))
        gamepad_opts = _choice_items("input.gamepad_mode", GAMEPAD_MODE_LABELS)
        return [ui.group("Keyboard and Mouse", [
            ui.popup("input.swap_command_option", "Command key:",
                     [("mac", "Mac-style (Win = Command)"), ("pc", "PC-style (Alt = Command)")],
                     "pc" if swap else "mac"),
            ui.text("Press Alt+Q to quit or Alt+S to save (Windows key = Option)." if swap else
                    "Press Windows+Q to quit or Windows+S to save (Alt = Option).", "small"),
            ui.popup("input.recovery_chord", "Recovery keys:",
                     list(schema.BY_KEY["input.recovery_chord"].choice_labels.items()), v["input.recovery_chord"]),
            ui.popup("input.pointer_speed", "Pointer speed:",
                     [(str(x), f"{x:g}×") for x in (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0)],
                     str(float(v["input.pointer_speed"]))),
            ui.popup("input.gamepad_mode", "Game controller:", gamepad_opts, str(v.get("input.gamepad_mode", "arrows"))),
            ui.columns(
                ui.checkbox("input.legacy_mouse", "Legacy mouse tracking", v["input.legacy_mouse"]),
                ui.checkbox("input.natural_scroll", "Natural scrolling", bool(v.get("input.natural_scroll", False))),
            ),
            ui.columns(
                dict(ui.text("Mouse DPI:"), width=100),
                ui.field("input.mouse_dpi", "", str(v["input.mouse_dpi"]), max_length=6, width=80),
                ui.text("0 = automatic", "small"),
            ),
            # touchscreens and pens (4.8): only when one is connected
            ui.checkbox("input.touch_gestures", "Touch gestures: hold for menus, two fingers scroll",
                        bool(v.get("input.touch_gestures", True)))
            if caps.get("touch") else None])]

    def doc(self):
        app = self.app
        v = self.values
        caps = app.capabilities()
        pages = [("monitor", "Monitor", "monitor_page"), ("sound", "Sound", "sound_page"),
                 ("keyboard", "Keyboard", "keyboard")]
        if self.app.rpi_display.enabled:
            pages.append(("pi-display", "Pi Display", "display"))
        items = [ui.band([ui.at(ui.icon("display", 64), 10, 4),
                          ui.at(ui.bevels("page", pages, self.page), 89, 10)], 76, rule="bottom")]
        items += {"monitor": self._monitor_page, "sound": self._sound_page,
                  "keyboard": self._keyboard_page, "pi-display": self._pi_display_page}[self.page](v, caps)
        pending = app.settings.pending_by_policy()[schema.GUEST_RESTART]
        # restart-class changes made in this window (3D acceleration, sound) count too, so
        # "Apply and Restart" is offered as soon as one is ticked
        pending = pending or [k for k, val in v.items() if k in schema.BY_KEY and
                              schema.BY_KEY[k].apply == schema.GUEST_RESTART and val != app.settings.desired.get(k)]
        if pending and app.supervisor.active:
            items.append(ui.text("Some changes take effect when Mac OS restarts.", "small"))
        if self.error:
            items.append(ui.text(self.error, "emphasis"))
        # Sound's Main Volume, applied as it moves; Mute where the mixer has a switch.
        laptop = getattr(app, "laptop", None)
        items.append(ui.band([
            ui.at(ui.slider("audio.volume", "Main Volume:", int(app.settings.applied.get("audio.volume", 70)), 0, 100, 1,
                            ("speaker_low", "speaker_high"), disabled=not caps.get("audio")), 13, 10),
            ui.at(ui.checkbox("audio.mute", "Mute", bool(laptop.muted)), 318, 9)
            if laptop is not None and laptop.can_mute else None], 33, rule="top", pin=True))
        if self.page == "pi-display":
            items.append(ui.buttons(ui.btn("cancel", "Done", default=True)))
        else:
            items.append(ui.buttons(ui.btn("cancel", "Cancel"),
                                    ui.btn("restart", "Apply and Restart", disabled=not (pending and app.supervisor.active)),
                                    ui.btn("apply", "Apply", default=True)))
        # One size for every page, as Monitors keeps (the Monitor page's height on a desktop).
        return ui.window(self.doc_id, "Display & Sound", items, width=507, height=self.HEIGHT)

    async def change(self, id, value):
        if id == "pi-display.profile":
            if value in {p["id"] for p in self.pi_display.get("profiles", [])}:
                self.pi_profile = value
                await self.panel.refresh()
            return
        if id == "page":
            if value in self.PAGES and value != self.page:
                self.page = value
                self.error = None
                await self.panel.refresh()
            return
        self.error = None
        if id == "audio.volume":   # live, as Sound's Main Volume (and the volume keys)
            try:
                vol = max(0, min(100, int(round(float(value)))))
            except (TypeError, ValueError):
                return
            if not self.app.capabilities().get("audio"):
                return                                   # the slider is dimmed without sound
            self.values["audio.volume"] = vol
            if vol != self.app.settings.applied.get("audio.volume"):
                try:
                    self.app.settings.set_desired_and_applied({"audio.volume": vol}, self.app.validation_context())
                except schema.ValidationError:
                    return
                await self.app.apply_engine.push_volume()
            return
        if id == "audio.mute":
            laptop = getattr(self.app, "laptop", None)
            if laptop is not None and laptop.can_mute:
                await laptop.set_mute(bool(value))
            return
        if id == "speakers.output":
            if self.speaker_output and value in ("speaker", "headphones"):
                self.speaker_output = value
            return
        s = schema.BY_KEY.get(id)
        opt_type = _OPTIONAL_SETTING_TYPES.get(id)
        if not s and not opt_type:
            return
        if id == "input.swap_command_option":
            swap = (value == "pc") if isinstance(value, str) else bool(value)
            changed = self.values.get(id) != swap
            self.values[id] = swap
            if changed:
                await self.panel.refresh()
            return
        if id == "input.mouse_dpi" and isinstance(value, str):
            raw = value.strip()
            if raw == "":
                self.values[id] = 0
            elif raw.isascii() and raw.isdecimal() and len(raw) <= 6:
                self.values[id] = int(raw)
            else:
                self.values[id] = raw
            return
        if id == "display.graphics" and isinstance(value, bool):   # the 3D acceleration checkbox
            value = "ati-radeon" if value else "standard"
        kind = s.type if s else ("bool" if opt_type is bool else "str")
        try:
            if kind == "int" or id == "machine.ram_mb":
                value = int(value)
            elif kind == "float":
                value = float(value)
            elif kind == "bool":
                value = bool(value)
        except (TypeError, ValueError):
            return
        changed = self.values.get(id) != value
        self.values[id] = value
        if changed and id in ("display.pixel_scale", "display.guest_size", "display.scaling", "display.rotation",
                              "display.graphics", "audio.enabled", "audio.output", "input.legacy_mouse"):
            await self.panel.refresh()  # the note on how a fixed size is drawn; Rotation Lock; 3D note

    async def action(self, id, values):
        if id.startswith("pi-display."):
            from ..rpidisplay import DisplayError
            if id == "pi-display.restart":
                await self.panel.start_job(lambda: self.app.host_job("reboot", origin="panel"),
                                           "Restarting RetroBoot", self._after_pi_reboot)
                return
            try:
                if id == "pi-display.try":
                    profile = values.get("pi-display.profile", self.pi_profile)
                    self.app.rpi_display.select(profile, self.pi_display.get("revision"))
                elif id in ("pi-display.keep", "pi-display.previous"):
                    self.app.rpi_display.confirm(id == "pi-display.keep", self.pi_display.get("revision"))
                else:
                    return
            except (DisplayError, OSError) as exc:
                text = str(exc) if isinstance(exc, DisplayError) else "The startup card could not save the display. Check that it is writable."
                await self.panel.message("caution", "The display could not be saved.", text,
                                         then=lambda: self.panel.show(DisplaySound(self.panel, self.back, "pi-display")))
                return
            self.pi_display = self.app.rpi_display.public()
            self.pi_profile = self.pi_display.get("trial") or self.pi_display.get("profile") or "auto"
            await self.panel.refresh()
            return
        if id == "cancel":
            await (self.back() if self.back else self.panel.show_recovery())
            return
        for k, v in values.items():
            await self.change(k, v)
        if id == "test-sound":
            await self._test_sound()
            return
        speakers = getattr(self.app, "speakers", None)
        if self.speaker_output and speakers is not None and self.speaker_output != speakers.output:
            await speakers.set_output(self.speaker_output, origin="panel")   # at once, no restart
        changes = {k: v for k, v in self.values.items()
                   if k in schema.BY_KEY and v != self.app.settings.desired.get(k)}
        try:
            if changes:
                self.app.settings.patch(self.app.settings.revision, changes, self.app.validation_context())
        except schema.ValidationError as exc:
            # Keep Display & Sound open on validation errors rather than hiding it behind an alert (#27).
            self.error = "; ".join(exc.fields.values())
            await self.panel.refresh()
            return
        restart = id == "restart"
        back = self.back or self.panel.show_recovery
        await self.panel.start_job(lambda: self.app.apply_job(self.app.settings.revision, restart, origin="panel"),
                                   "Applying settings", lambda job: self._after(job, back))

    async def _after_pi_reboot(self, job):
        if job.state != SUCCEEDED:
            await self.panel.message("caution", "The computer did not restart.", job.message,
                                     then=lambda: self.panel.show(DisplaySound(self.panel, self.back, "pi-display")))
        else:
            await self.panel.close()

    async def _after(self, job, back):
        if job.state != SUCCEEDED:
            await self.panel.message("caution", "The settings could not be applied.", job.message,
                                     then=lambda: self.panel.show(DisplaySound(self.panel, self.back, self.page)))
            return
        if job.result and job.result.get("confirm"):
            await self.panel.show(KeepSetting(self.panel, DisplaySound(self.panel, self.back, self.page)))
        elif job.result and job.result.get("restarting"):
            await self.panel.close()
        else:
            await back()


class StartupDisk(Screen):
    """Startup Disk: the Macs on this computer.  The selection starts as the Mac chosen to
    start next (desired ``machine.active``): a choice made while Mac OS runs waits for Mac
    OS's restart and is shown as such, with Restart Now, instead of snapping back to the
    running Mac.  Under the list: Download a Mac, New Blank Disk and Start Up from CD (Macs
    in, Mac OS onto a blank disk); below, how this computer starts; Delete and Checkpoints at
    the left of the bottom row, Use This Mac the default once another Mac is selected."""
    doc_id = "startup-disk"
    tick = 3.0          # a Mac removed or added from the web, Mac OS started or stopped

    def __init__(self, panel, back, selected: str | None = None):
        super().__init__(panel)
        self.back = back
        self.selected = selected or self.app.settings.desired.get("machine.active")
        self.usb_drives: list[dict] = []
        self.usb_selected: str | None = None
        self.usb_notice: str | None = None
        if getattr(self.app, "loop", None) is not None:
            self.app.loop.create_task(self._load_usb())

    def reopen(self, selected: str | None = None):
        return self.panel.show(StartupDisk(self.panel, self.back, selected or self.selected))

    async def _load_usb(self) -> None:
        usbshare = getattr(self.app, "usbshare", None)
        fn = getattr(usbshare, "list_drives", None) if usbshare is not None else None
        drives: list[dict] = []
        if callable(fn):
            try:
                got = fn()
                if inspect.isawaitable(got):
                    got = await got
                drives = [d for d in (got or []) if isinstance(d, dict) and d.get("device")]
            except Exception:
                drives = []
        changed = drives != self.usb_drives
        self.usb_drives = drives
        devices = {d["device"] for d in drives}
        if self.usb_selected not in devices:
            self.usb_selected = drives[0]["device"] if drives else None
        if changed and self.panel.screen is self and self.panel.visible:
            await self.panel.refresh()

    async def on_tick(self):
        await self._load_usb()
        if self.panel.screen is self and self.panel.visible:
            await self.panel.refresh()

    def _usb_label(self, d: dict) -> str:
        name = d.get("label") or d.get("name") or d.get("device") or "USB Drive"
        size = d.get("bytes")
        return f"{name} ({ui.human_bytes(size)})" if size else str(name)

    def doc(self):
        app = self.app
        sup = app.supervisor
        machines = app.machines.list()
        ids = {m.id for m in machines}
        desired = app.settings.desired.get("machine.active")
        if self.selected not in ids:
            # removed from the web meanwhile (DELETE /api/v1/machines/{id})
            self.selected = desired if desired in ids else (machines[0].id if machines else None)
        running = sup.machine.id if sup.active and sup.machine is not None else None
        rows = []
        for m in machines:
            parts = []
            if m.needs_cd():
                size = (m.meta.get("source") or {}).get("size_bytes")
                parts.append(("No Mac OS X yet" if m.osx else "No Mac OS yet")
                             + (f" · {ui.human_bytes(size)} disk" if size else ""))
            else:
                from ..catalogs import os_label
                parts.append(os_label(m.meta.get("os_version")) or "Mac OS")    # "8.1" reads "Mac OS 8.1"
            if m.quadra:
                parts.append(FAMILY_LABELS[QUADRA800])          # "Mac OS 8.1 · 68k"
            # The startup Mac has the check mark (as the current item of a menu), so its detail
            # stays short: "Mac OS 7.5.5 · 68k · Running" fits beside a long name.
            if m.id == running:
                parts.append("Running")
            elif m.id == desired and running:
                parts.append("Starts next")
            rows.append(ui.row(m.id, m.name, " · ".join(parts), "disk", checked=m.id == desired))
        sel = app.machines.get(self.selected) if self.selected else None
        items = [ui.text("Choose the Mac that starts with this computer."),
                 ui.lst("machine", rows, self.selected, rows=5),
                 ui.buttons(ui.btn("download", "Download a Mac…", align="left"),
                            ui.btn("new", "New Blank Disk…", align="left"),
                            ui.btn("cd", "Start Up from Disc…" if sel and sel.osx else "Start Up from CD…",
                                   align="left", disabled=sel is None))]
        if running and desired and desired != running and desired in ids:
            name = app.machines.get(desired).name
            items.append(ui.text(f"“{name}” starts the next time Mac OS restarts.", "small"))
            items.append(ui.buttons(ui.btn("restart", "Restart Now")))
        startup = app.machines.get(desired) if desired else None
        osx = bool(startup and startup.osx)
        items.append(ui.group("When This Computer Starts", [
            ui.checkbox("autostart", "Start Mac OS automatically", app.settings.desired["startup.autostart"]),
            ui.checkbox("progress", "Show progress while Mac OS starts",
                        app.settings.desired.get("startup.progress_indicator", True), disabled=osx),
            ui.text("Mac OS X shows its own startup screen and progress.", "small") if osx else None]))
        if self.usb_drives:
            sel_usb = next((d for d in self.usb_drives if d["device"] == self.usb_selected), self.usb_drives[0])
            usb_items = []
            if len(self.usb_drives) == 1:
                usb_items.append(ui.text(f"Mounted drive: {self._usb_label(sel_usb)}", "small"))
            else:
                usb_items.append(ui.popup("usb_drive", "Drive:",
                                          [(d["device"], self._usb_label(d)) for d in self.usb_drives],
                                          sel_usb["device"]))
            btn_label = f"Eject “{sel_usb.get('label') or sel_usb.get('name') or 'USB Drive'}”" if len(self.usb_drives) == 1 else "Eject USB Drive"
            usb_items.append(ui.buttons(ui.btn("eject-usb", btn_label, align="left")))
            items.append(ui.group("USB Share Drives", usb_items))
        if self.usb_notice:
            items.append(ui.text(self.usb_notice, "small"))
        use = bool(self.selected) and self.selected != desired
        items.append(ui.buttons(ui.btn("delete", "Delete…", align="left",
                                       disabled=sel is None or app.remove_blocked(sel) is not None),
                                ui.btn("options", "Options…", align="left", disabled=sel is None),
                                ui.btn("checkpoints", "Checkpoints…", align="left", disabled=sel is None),
                                ui.btn("cancel", "Done", default=not use),
                                ui.btn("use", "Use This Mac", default=use, disabled=not use)))
        return ui.window(self.doc_id, "Startup Disk", items, width=540)

    async def change(self, id, value):
        if id == "machine":
            self.selected = value
            self.usb_notice = None
            await self.panel.refresh()
        elif id == "usb_drive":
            self.usb_selected = value
            self.usb_notice = None
            await self.panel.refresh()
        elif id == "autostart":
            await self.app.apply_engine.change_and_apply({"startup.autostart": bool(value)})
        elif id == "progress":
            await self.app.apply_engine.change_and_apply({"startup.progress_indicator": bool(value)})

    async def action(self, id, values):
        p = self.panel
        if id == "cancel":
            await self.back()
        elif id in ("download", "install"):
            await p.show(CatalogList(p, back=lambda: self.reopen(), then=after_download))
        elif id == "use" and self.selected:
            await p.start_job(lambda: self.app.select_machine_job(self.selected, origin="panel"),
                              "Changing the startup Mac", self._after_use)
        elif id == "restart":
            await p.start_job(lambda: self.app.lifecycle_job("restart", origin="panel"), "Restarting Mac OS",
                              self._after_restart, allow_force=True)
        elif id == "options" and self.selected:
            await p.show(MacOptions(p, self.selected, self))
        elif id == "checkpoints" and self.selected:
            await p.show(Checkpoints(p, self.selected, self))
        elif id == "eject-usb" and self.usb_drives:
            dev = values.get("usb_drive") or self.usb_selected or self.usb_drives[0]["device"]
            drv = next((d for d in self.usb_drives if d["device"] == dev), None)
            label = (drv.get("label") or drv.get("name") or dev) if drv else dev
            usbshare = getattr(self.app, "usbshare", None)
            fn = getattr(usbshare, "eject", None) if usbshare is not None else None
            if callable(fn):
                try:
                    got = fn(dev)
                    if inspect.isawaitable(got):
                        await got
                    self.usb_notice = f"Safe to unplug “{label}”."
                except Exception as exc:
                    self.usb_notice = f"Could not eject “{label}”: {exc}"
                await self._load_usb()
                await p.refresh()
        elif id == "new":
            await p.show(NewBlankDisk(p, self))
        elif id == "delete" and self.selected:
            m = self.app.machines.get(self.selected)
            if m is not None:
                await p.show(ConfirmDelete(p, m, self))
        elif id == "cd" and self.selected:
            screen = StartFromCD(p, self.selected, self)
            await screen._load()          # the drives first: a disc may spin up for a moment
            await p.show(screen)

    async def _after_use(self, job):
        p = self.panel
        if job.state != SUCCEEDED:
            await p.message("caution", "The startup Mac was not changed.", job.message, then=lambda: self.reopen())
            return
        m = self.app.machines.get(self.selected)
        name = m.name if m else "The Mac"
        if job.result and job.result.get("pending_restart"):
            await p.show(RestartForMac(p, name, self))
        else:
            await p.message("note", f"“{name}” is the startup Mac.", None, then=lambda: self.reopen())

    async def _after_restart(self, job):
        if job.state == SUCCEEDED:
            if isinstance(self.panel.screen, JobScreen):
                await self.panel.close()
            return
        await self.panel.show(Unresponsive(self.panel, job.message))


class MacOptions(Screen):
    """Per-Mac Memory & Graphics overrides sharing machine.json['settings'] authority (#44)."""
    doc_id = "mac-options"

    def __init__(self, panel: "Panel", machine_id: str, disk: StartupDisk):
        super().__init__(panel)
        self.machine_id, self.disk = machine_id, disk
        self.error: str | None = None
        cur = self._load_settings()
        m = self.app.machines.get(machine_id)
        self.ram_key = "osx_ram_mb" if (m is not None and m.osx) else "ram_mb"
        raw_ram = cur.get(self.ram_key)
        if raw_ram is None and self.ram_key == "ram_mb":
            raw_ram = cur.get("quadra_ram_mb")
        if raw_ram is None:
            raw_ram = (
                self.app.settings.desired.get("machine.osx_ram_mb", "auto")
                if self.ram_key == "osx_ram_mb" else
                self.app.settings.desired.get("machine.quadra_ram_mb" if m is not None and m.quadra else "machine.ram_mb",
                                              128 if m is not None and m.quadra else 512)
            )
        self.ram = str(raw_ram)
        self.graphics = str(cur.get("graphics") or self.app.settings.desired.get("display.graphics", "standard"))

    def _load_settings(self) -> dict:
        fn = getattr(self.app, "machine_settings", None)
        if callable(fn):
            try:
                got = fn(self.machine_id)
                if isinstance(got, dict):
                    return dict(got)
            except Exception:
                pass
        m = self.app.machines.get(self.machine_id)
        return dict(m.meta.get("settings") or {}) if m is not None else {}

    def _ram_choices(self, m) -> list[tuple[str, str]]:
        if m is not None and m.quadra:
            base = list(schema.BY_KEY["machine.quadra_ram_mb"].choices)
            opts = [(str(v), f"{v} MB") for v in base]
        elif m is not None and m.osx:
            profile = (m.meta.get("guest_os") or "")
            base = [v for v in schema.BY_KEY["machine.osx_ram_mb"].choices if not (profile == "macosx-10.4" and v == 128)]
            opts = [(str(v), "Automatic" if v == "auto" else f"{v} MB") for v in base]
        else:
            base = list(schema.BY_KEY["machine.ram_mb"].choices)
            opts = [(str(v), f"{v} MB") for v in base]
        if self.ram and self.ram not in dict(opts):
            opts.append((self.ram, "Automatic" if self.ram == "auto" else f"{self.ram} MB"))
        return opts

    def doc(self):
        m = self.app.machines.get(self.machine_id)
        if m is None:
            return ui.alert(self.doc_id, "caution", "That Mac is no longer available.", None,
                            ui.buttons(ui.btn("cancel", "OK", default=True)))
        gfx = self.app.graphics.ui_context()
        gfx_ok = bool(gfx.get("available"))
        ram_opts = self._ram_choices(m)
        items = [
            ui.columns(
                ui.icon("disk", 32),
                ui.stack(
                    ui.text(f"Options for “{m.name}”", "emphasis"),
                    ui.text("Shared with Web Settings. Changes take effect the next time this Mac starts.", "small"),
                ),
            ),
            ui.popup("ram", "Memory (RAM):", ram_opts, self.ram),
        ]
        if m.quadra:
            items.append(ui.text("Graphics: Built-in 68k display", "small"))
        else:
            gfx_opts = [("standard", "Standard VGA"), ("ati-radeon", "ATI Radeon (3D)")]
            items.append(ui.popup("graphics", "Graphics:", gfx_opts, self.graphics,
                                  disabled=not gfx_ok and self.graphics == "standard"))
            if not gfx_ok and gfx.get("message"):
                items.append(ui.text(gfx["message"], "small"))
        if self.error:
            items.append(ui.text(self.error, "emphasis"))
        items.append(ui.buttons(ui.btn("cancel", "Cancel"), ui.btn("save", "Save", default=True)))
        return ui.window(self.doc_id, "Mac Options", items, width=460)

    async def change(self, id, value):
        self.error = None
        if id == "ram":
            self.ram = str(value)
        elif id == "graphics":
            self.graphics = str(value)

    async def action(self, id, values):
        if id == "cancel":
            await self.disk.reopen(self.machine_id)
            return
        if id != "save":
            return
        m = self.app.machines.get(self.machine_id)
        if m is None:
            await self.disk.reopen()
            return
        ram_str = str(values.get("ram", self.ram))
        gfx_val = str(values.get("graphics", self.graphics))
        ram_val: Any = "auto" if ram_str == "auto" else (int(ram_str) if ram_str.isdecimal() else ram_str)
        changes: dict[str, Any] = {self.ram_key: ram_val}
        if not m.quadra:
            changes["graphics"] = gfx_val
        updater = getattr(self.app, "update_machine_settings", None)
        try:
            if callable(updater):
                res = updater(self.machine_id, changes)
                if inspect.isawaitable(res):
                    await res
            else:
                meta = dict(m.meta)
                settings = dict(meta.get("settings") or {})
                settings.update(changes)
                meta["settings"] = settings
                m.meta = meta
        except (ValueError, MachineError, schema.ValidationError) as exc:
            self.error = str(exc)
            await self.panel.refresh()
            return
        await self.disk.reopen(self.machine_id)


class Checkpoints(Screen):
    """A Mac's checkpoints (Startup Disk → Checkpoints…): save one now, or restore one."""
    doc_id = "checkpoints"

    def __init__(self, panel, machine_id: str, disk: StartupDisk):
        super().__init__(panel)
        self.machine_id, self.disk = machine_id, disk
        self.checkpoint: str | None = None

    def doc(self):
        m = self.app.machines.get(self.machine_id)
        cps = self.app.checkpoints.list(self.machine_id) if m else []
        rows = [ui.row(c["id"], c["label"], c["created"][:19].replace("T", " "), "disk") for c in cps]
        if self.checkpoint not in {r["id"] for r in rows}:
            self.checkpoint = None
        items = [ui.columns(ui.icon("disk", 32), ui.stack(
            ui.text(f"Checkpoints of “{m.name if m else self.machine_id}”", "emphasis"),
            ui.text(("A checkpoint keeps this Mac's disk and startup memory together; restoring brings both back. "
                     "Mac OS X shuts down cleanly first." if m is not None and m.osx else
                     "A checkpoint keeps the Mac's disk as it is now; restoring one brings that disk back. "
                     "Mac OS shuts down cleanly first."), "small")))]
        items.append(ui.lst("checkpoint", rows, self.checkpoint, rows=4) if rows else ui.text("No checkpoints yet."))
        items.append(ui.buttons(ui.btn("restore", "Restore…", align="left", disabled=not self.checkpoint),
                                ui.btn("save", "Save Checkpoint", disabled=m is None),
                                ui.btn("cancel", "Done", default=True)))
        return ui.window(self.doc_id, "Checkpoints", items, width=500)

    async def change(self, id, value):
        if id == "checkpoint":
            self.checkpoint = value
            await self.panel.refresh()

    async def action(self, id, values):
        p = self.panel
        if id == "cancel":
            await self.disk.reopen(self.machine_id)
        elif id == "save":
            await p.start_job(lambda: self.app.checkpoint_job(self.machine_id, "Saved from Recovery", origin="panel"),
                              "Saving a checkpoint", self._after_save, allow_force=False)
        elif id == "restore" and self.checkpoint:
            await p.show(ConfirmRestore(p, self.machine_id, self.checkpoint, back=lambda: p.show(self)))

    async def _after_save(self, job):
        await self.panel.message("note" if job.state == SUCCEEDED else "caution",
                                 "Checkpoint saved." if job.state == SUCCEEDED else "The checkpoint was not saved.",
                                 job.message, then=lambda: self.panel.show(self))


class RestartForMac(Screen):
    """After Use This Mac while Mac OS runs: the new Mac starts at Mac OS's restart; offer it now."""
    doc_id = "restart-for-mac"

    def __init__(self, panel, name: str, disk: StartupDisk):
        super().__init__(panel)
        self.name, self.disk = name, disk

    def doc(self):
        return ui.alert(self.doc_id, "note", f"“{self.name}” starts the next time Mac OS restarts.",
                        "Restart Mac OS now? Save your work in Mac OS first.",
                        ui.buttons(ui.btn("later", "Later"), ui.btn("restart", "Restart Now", default=True)))

    async def action(self, id, values):
        if id == "later":
            await self.disk.reopen()
        elif id == "restart":
            await self.panel.start_job(lambda: self.app.lifecycle_job("restart", origin="panel"),
                                       "Restarting Mac OS", self.disk._after_restart, allow_force=True)


class NewBlankDisk(Screen):
    """New Blank Disk: a new Mac with an empty disk; Mac OS is installed on it from a CD."""
    doc_id = "new-blank-disk"

    def __init__(self, panel, disk: StartupDisk):
        super().__init__(panel)
        self.disk = disk
        self.name = self._default_name()
        self.family = POWERMAC
        self.guest_os = "classic"
        self.size = "8"
        self.error: str | None = None

    def _default_name(self) -> str:
        names = {m.name for m in self.app.machines.list()}
        name, n = "Untitled Mac", 2
        while name in names:
            name, n = f"Untitled Mac {n}", n + 1
        return name

    def kinds(self) -> list[dict]:
        return self.app.blank_kinds()

    def kind(self) -> dict:
        kinds = self.kinds()
        return next((k for k in kinds if k["family"] == self.family), kinds[0])

    def doc(self):
        try:
            free = shutil.disk_usage(self.app.paths.machines).free
        except OSError:
            free = None
        kinds = self.kinds()
        kind = self.kind()
        if self.size not in [str(g) for g in kind["sizes_gb"]]:
            self.size = str(kind["default_gb"])
        unavailable = next((k for k in kinds if not k["available"]), None)
        items = [ui.columns(ui.icon("disk", 32),
                            ui.stack(ui.text("Make an empty disk for a new Mac.", "emphasis"),
                                     ui.text("To install Mac OS X on it, start it up from an install disc."
                                             if self.guest_os != "classic" else
                                             "To install Mac OS on it, start it up from a Mac OS CD."))),
                 ui.field("name", "Name:", self.name, max_length=63),
                 ui.radio("family", "Kind of Mac:", [(k["family"], k["label"], not k["available"]) for k in kinds],
                          self.family),
                 ui.text(unavailable["reason"], "small") if unavailable else None,
                 ui.popup("guest_os", "Operating system:",
                          [(c["value"], c["label"]) for c in kind["guest_os_choices"]], self.guest_os)
                 if kind.get("guest_os_choices") else None,
                 ui.popup("size", "Size:", [(str(g), f"{g} GB") for g in kind["sizes_gb"]], self.size),
                 ui.text(kind["note"], "small") if kind.get("note") else None,
                 ui.text("The disk takes space on this computer only as Mac OS fills it."
                         + (f" {ui.human_bytes(free)} available." if free is not None else ""), "small")]
        if self.error:
            items.append(ui.text(self.error, "small"))
        items.append(ui.buttons(ui.btn("cancel", "Cancel"), ui.btn("create", "Create", default=True)))
        return ui.window(self.doc_id, "New Blank Disk", items, width=440, focus="name")

    async def change(self, id, value):
        if id == "name":
            self.name = str(value or "")
        elif id == "size":
            self.size = str(value)
            await self.panel.refresh()
        elif id == "family" and value in [k["family"] for k in self.kinds() if k["available"]]:
            if value != self.family:
                self.family = str(value)
                self.guest_os = "classic"
                self.size = str(self.kind()["default_gb"])
            await self.panel.refresh()
        elif id == "guest_os" and value in [c["value"] for c in self.kind().get("guest_os_choices", [])]:
            self.guest_os = value
            await self.panel.refresh()

    async def action(self, id, values):
        if id == "cancel":
            await self.disk.reopen()
            return
        if id != "create":
            return
        name = values.get("name", self.name)
        try:
            m = self.app.create_blank_machine(name, int(self.size), origin="panel", family=self.family,
                                               guest_os=self.guest_os)
        except (MachineError, ValueError) as exc:
            self.name, self.error = str(name or ""), str(exc)
            await self.panel.refresh()
            return
        await self.panel.message(
            "note", f"“{m['name']}” is ready.",
            ("To install Mac OS X on it, connect an install CD or DVD (or add a disc image), "
             "then choose Start Up from Disc." if self.guest_os != "classic" else
             "To install Mac OS on it, put a Mac OS CD in this computer's CD-ROM drive (or add a CD image), "
             "then choose Start Up from CD."), then=lambda: self.disk.reopen(m["id"]))


class ConfirmDelete(Screen):
    doc_id = "confirm-delete"

    def __init__(self, panel, machine, disk: StartupDisk):
        super().__init__(panel)
        self.machine, self.disk = machine, disk

    def doc(self):
        n = len(self.app.checkpoints.list(self.machine.id))
        extra = f" and its {n} checkpoint{'s' if n != 1 else ''}" if n else ""
        return ui.alert(self.doc_id, "caution", f"Delete “{self.machine.name}”?",
                        f"Its disk{extra} will be deleted from this computer. This can't be undone.",
                        ui.buttons(ui.btn("cancel", "Cancel", default=True), ui.btn("delete", "Delete")))

    async def action(self, id, values):
        if id == "cancel":
            await self.disk.reopen()
            return
        await self.panel.start_job(lambda: self.app.remove_machine_job(self.machine.id, origin="panel"),
                                   f"Deleting “{self.machine.name}”", self._after, allow_force=False)

    async def _after(self, job):
        if job.state == SUCCEEDED:
            await self.panel.message("note", f"“{self.machine.name}” was deleted.", None,
                                     then=lambda: self.disk.reopen(self.app.settings.desired.get("machine.active")))
        else:
            await self.panel.message("caution", f"“{self.machine.name}” was not deleted.", job.message,
                                     then=lambda: self.disk.reopen())


class StartFromCD(Screen):
    """Start Up from CD: the chosen Mac starts up from a CD once (a disc in this computer's
    CD-ROM drive, or a CD image), with its own disk attached, e.g. to install Mac OS."""
    doc_id = "start-from-cd"
    tick = 2.0          # a disc put in the drive appears

    def __init__(self, panel, machine_id: str, disk: StartupDisk):
        super().__init__(panel)
        self.machine_id, self.disk = machine_id, disk
        self.selected: str | None = None
        self.cds: list[dict] = []
        self.drives: list[dict] = []

    async def on_tick(self):
        await self._load()
        await self.panel.refresh()

    async def _load(self):
        drives = await asyncio.to_thread(self.app.optical.list) if self.app.optical.present() else []
        self.drives = drives
        self.cds = self.app.supervisor.cd_sources(drives)
        if self.selected not in {c["id"] for c in self.cds}:
            self.selected = self.cds[0]["id"] if self.cds else None

    def doc(self):
        m = self.app.machines.get(self.machine_id)
        if m is None:
            return ui.alert(self.doc_id, "caution", "That Mac isn't on this computer anymore.", None,
                            ui.buttons(ui.btn("cancel", "OK", default=True)))
        sup = self.app.supervisor
        blank = m.needs_cd()
        disc = "disc" if m.osx else "CD"
        system = "Mac OS X" if m.osx else "Mac OS"
        if blank:
            lead = f"“{m.name}” starts up from the {disc} you choose. Its installer can install {system} on the empty disk."
        else:
            lead = f"“{m.name}” starts up from the {disc} you choose, once. Its own disk stays available."
        items = [ui.columns(ui.icon("cd", 32), ui.stack(ui.text(f"Start up from a {disc}", "emphasis"), ui.text(lead)))]
        if self.cds:
            rows = [ui.row(c["id"], c["title"], c["where"], "cd") for c in self.cds]
            items.append(ui.lst("cd", rows, self.selected, rows=4))
        else:
            if m.osx:
                what = ("The disc drive is empty. Put a Mac OS X install disc in it." if self.drives else
                        "Connect a USB CD or DVD drive with a Mac OS X install disc, or add a disc image to Files on the web page.")
            elif self.drives:
                what = "The CD-ROM drive is empty. Put a Mac OS CD in it."
            else:
                what = "Connect a CD-ROM drive (USB) with a Mac OS CD in it, or add a CD image to Files on the web page."
            items.append(ui.text(what))
        if sup.active:
            items.append(ui.text("Mac OS restarts. Save your work in Mac OS first.", "small"))
        items.append(ui.buttons(ui.btn("cancel", "Cancel"),
                                ui.btn("start", "Restart" if sup.active else "Start Up", default=True,
                                       disabled=not self.selected)))
        return ui.window(self.doc_id, "Start Up from Disc" if m.osx else "Start Up from CD", items, width=480)

    async def change(self, id, value):
        if id == "cd":
            self.selected = value
            await self.panel.refresh()

    async def action(self, id, values):
        if id == "cancel":
            await self.disk.reopen()
            return
        if id == "start" and self.selected:
            src = self.selected
            await self.panel.start_job(lambda: self.app.cd_start_job(self.machine_id, src, origin="panel"),
                                       "Starting up from the CD", self._after, allow_force=True)

    async def _after(self, job):
        if job.state == SUCCEEDED:
            if isinstance(self.panel.screen, JobScreen):
                await self.panel.close()
            return
        await self.panel.message("caution", "Mac OS could not start up from the CD.", job.message,
                                 then=lambda: self.disk.reopen())


class NeedsCD(Screen):
    """A start was refused: a blank disk without Mac OS and no CD to install it from."""
    doc_id = "needs-cd"

    def __init__(self, panel, name: str, blank: bool):
        super().__init__(panel)
        self.name, self.blank = name, blank

    def doc(self):
        from ..supervisor import needs_cd_parts
        machine = self.app.supervisor.active_machine()
        osx = bool(machine and machine.osx)
        lead, detail = needs_cd_parts(self.name, self.blank, osx)
        msg = (lead if osx else
               f"There's no Mac OS on “{self.name}” yet." if self.blank else "The CD isn't available anymore.")
        return ui.alert(self.doc_id, "note", msg, detail,
                        ui.buttons(ui.btn("options", "Startup Options…"),
                                   ui.btn("disk", "Startup Disk…", default=True)))

    async def action(self, id, values):
        p = self.panel
        if id == "disk":
            await p.show(StartupDisk(p, back=p.show_startup_options))
        else:
            await p.show_startup_options()


class ConfirmRestore(Screen):
    doc_id = "confirm-restore"

    def __init__(self, panel, machine_id, cp_id, back):
        super().__init__(panel)
        self.machine_id, self.cp_id, self.back = machine_id, cp_id, back

    def doc(self):
        machine = self.app.machines.get(self.machine_id)
        detail = ("Mac OS X shuts down first. Its disk and startup memory return to this checkpoint; "
                  "changes made since then will be replaced." if machine is not None and machine.osx else
                  "Mac OS shuts down first. Everything changed on its disk since the checkpoint will be replaced.")
        return ui.alert(self.doc_id, "caution", "Restore this checkpoint?", detail,
                        ui.buttons(ui.btn("cancel", "Cancel", default=True), ui.btn("restore", "Restore")))

    async def action(self, id, values):
        if id == "cancel":
            await self.back()
            return
        await self.panel.start_job(lambda: self.app.restore_job(self.machine_id, self.cp_id, origin="panel"),
                                   "Restoring the checkpoint", self._after)

    async def _after(self, job):
        if job.state == SUCCEEDED:
            await self.panel.close()
        else:
            await self.panel.message("caution", "The checkpoint was not restored.", job.message,
                                     then=self.panel.show_recovery)


# --- first-time setup and import -------------------------------------------------------

class SetupWelcome(Screen):
    doc_id = "setup-welcome"

    def __init__(self, panel, back=None):
        super().__init__(panel)
        self.back = back
        self.source = "catalog"

    def doc(self):
        first = not self.app.machines.list()
        swap = bool(self.app.settings.desired.get("input.swap_command_option"))
        items = [ui.text("Welcome to RetroBoot" if first else "Install a Mac", "emphasis"),
                 ui.text("RetroBoot needs a Mac OS system to start. Choose where to get one."),
                 ui.radio("source", "Install from:", [("catalog", "RetroBoot downloads or the Internet"
                                                      if self.app.lab_auth is not None else
                                                      "The Internet (RetroBoot Mac library)"),
                                                      ("usb", "A USB disk")], self.source),
                 ui.popup("input.swap_command_option", "Command key:",
                          [("mac", "Mac-style (Win = Command)"), ("pc", "PC-style (Alt = Command)")],
                          "pc" if swap else "mac"),
                 ui.text("Press Alt+Q to quit or Alt+S to save (Windows key = Option)." if swap else
                         "Press Windows+Q to quit or Windows+S to save (Alt = Option).", "small"),
                 ui.buttons(ui.btn("bluetooth", "Bluetooth…")) if self.app.bluetooth.capable() else None,
                 ui.buttons(ui.btn("network", "Network…"),
                            ui.btn("cancel", "Back") if self.back else ui.btn("poweroff", "Shut Down"),
                            ui.btn("continue", "Continue", default=True))]
        return ui.window(self.doc_id, "RetroBoot Setup", items, width=480)

    async def change(self, id, value):
        if id == "source":
            self.source = value
        elif id == "input.swap_command_option":
            swap = (value == "pc") if isinstance(value, str) else bool(value)
            if swap != bool(self.app.settings.desired.get("input.swap_command_option")):
                await self.app.apply_engine.change_and_apply({"input.swap_command_option": swap})
            await self.panel.refresh()

    async def action(self, id, values):
        p = self.panel
        me = lambda: p.show(self)
        if "input.swap_command_option" in values:
            val = values["input.swap_command_option"]
            swap = (val == "pc") if isinstance(val, str) else bool(val)
            if swap != bool(self.app.settings.desired.get("input.swap_command_option")):
                await self.app.apply_engine.change_and_apply({"input.swap_command_option": swap})
        if id == "continue":
            src = values.get("source") or self.source
            nxt = lambda: p.show(CatalogList(p, back=me) if src == "catalog" else UsbList(p, back=me))
            if not self.app.machines.list() and self.app.settings.desired.get("system.timezone", "UTC") == "UTC":
                # A new computer whose clock zone nobody chose yet; the installer's Time Zone
                # step (or an earlier visit) already set one otherwise.
                await p.show(TimeZone(p, back=me, next_=nxt))
            else:
                await nxt()
        elif id == "network":
            await p.show(NetworkWeb(p, back=me))
        elif id == "bluetooth":
            from .bluetooth import BluetoothDevices
            await p.show(BluetoothDevices(p, back=me))
        elif id == "cancel" and self.back:
            await self.back()
        elif id == "poweroff":
            await p.show_power_alert()


class ImageServers(Screen):
    doc_id = "image-servers"
    key = "catalog.urls"

    def __init__(self, panel, back):
        super().__init__(panel)
        self.back = back
        self.urls = self.app.settings.desired.get(self.key, []).copy()
        self.initial = self.urls.copy()
        self.selected: str | None = None
        self.address = ""
        self.error = ""

    def doc(self):
        rows = [ui.row(str(index), url, None, "web") for index, url in enumerate(self.urls)]
        items = [
            ui.text("Add the HTTPS address of a machine catalog."),
            ui.lst("servers", rows, self.selected, rows=4),
            ui.field("url", "Catalog URL", self.address, placeholder="https://example.com/catalog.json",
                     max_length=2048),
            ui.buttons(ui.btn("add", "Add"), ui.btn("remove", "Remove", disabled=self.selected is None)),
        ]
        if self.error:
            items.append(ui.text(self.error, "emphasis"))
        items.append(ui.buttons(ui.btn("cancel", "Cancel"), ui.btn("save", "Save", default=True)))
        return ui.window(self.doc_id, "Image Servers", items, width=540)

    async def change(self, id, value):
        if id == "servers":
            self.selected = value
            await self.panel.refresh()
        elif id == "url" and isinstance(value, str):
            self.address = value

    async def action(self, id, values):
        if id == "cancel":
            await self.back()
            return
        address = values.get("url", self.address)
        self.address = address.strip() if isinstance(address, str) else ""
        self.error = ""
        try:
            if id == "add" or (id == "save" and self.address):
                if not self.address:
                    raise ValueError("Enter a catalog URL.")
                self.urls = schema.validate_catalog_urls(self.urls + [self.address])
                self.address = ""
            elif id == "remove" and self.selected is not None:
                self.urls.pop(int(self.selected))
                self.selected = None
            if id == "save":
                if self.app.settings.desired.get(self.key, []) != self.initial:
                    self.error = "Image servers changed in another window. Reopen Image Servers to edit the current list."
                elif self.urls != self.initial:
                    await self.app.apply_engine.change_and_apply({self.key: self.urls})
                if not self.error:
                    await self.back()
                    return
        except (ValueError, IndexError) as exc:
            self.error = str(exc)
        await self.panel.refresh()


def _catalog_hero_picks(items: list) -> tuple[Any, Any]:
    """Return ``(featured_deluxe, featured_base)`` for Download a Mac hero choice."""
    fn = getattr(_catalogs, "first_boot_picks", None)
    if callable(fn):
        try:
            got = fn(items)
            if isinstance(got, tuple) and len(got) == 2 and all(got):
                return got
        except Exception:
            pass
    deluxe = next((it for it in items if not getattr(it, "older", False) and (
        "os922-mega-deluxe" in it.entry.get("id", "").lower()
        or "mega-deluxe" in it.entry.get("id", "").lower()
    )), None)
    base = next((it for it in items if not getattr(it, "older", False) and (
        "os922-full" in it.entry.get("id", "").lower()
        or (it.entry.get("osVersion") == "9.2.2"
            and "deluxe" not in it.entry.get("id", "").lower()
            and "deluxe" not in it.entry.get("name", "").lower())
    )), None)
    if deluxe is not None and base is not None and deluxe.key != base.key:
        return deluxe, base
    return None, None


class CatalogList(Screen):
    """Download a Mac: RetroBoot downloads (with a stored sign-in) and the Internet catalog in
    one list (catalogs.arrange: one row per Mac, a family's older versions after "Show older
    versions", Macs this computer already has marked Installed); From a USB Disk… for machine
    archives.  ``then(panel, job, back)`` follows a download (after_download: start up with the
    new Mac?); without it, first-time setup's Installed."""
    doc_id = "setup-catalog"

    def __init__(self, panel, back, on_installed=None, then=None):
        super().__init__(panel)
        self.back = back
        self.items: list | None = None          # catalogs.Listed
        self.errors: dict[str, str] = {}
        self.sources: list[str] = []
        self.error: str | None = None           # every catalog failed
        self.selected: str | None = None
        self.show_older = False
        self.show_library: bool | None = None
        self.on_installed = on_installed
        self.then = then
        self.recommended = None
        self.app.loop.create_task(self._load())

    async def _load(self):
        cats = self.app.catalogs
        try:
            results, self.errors = await cats.fetch_all()
            self.sources = [sid for sid, _ in results]
            self.items = cats.listing(results)
            self.error = next(iter(self.errors.values())) if self.errors and len(self.errors) == len(results) else None
        except Exception:  # noqa: BLE001 -- shown as a catalog error, never a crash
            log.exception("catalog listing failed")
            self.items, self.errors, self.error = [], {}, "The catalog could not be loaded."
        from ..catalogs import recommended
        items = self.items or []
        rec = recommended(items)
        # Never recommend downgrading to 9.2.1 when any 9.2.2 Mac is already installed (#32).
        has_922 = any(getattr(m, "meta", {}).get("os_version", "").startswith("9.2.2")
                      for m in self.app.machines.list()) or any(
            it.known and str(it.entry.get("osVersion") or "").startswith("9.2.2") for it in items
        )
        if rec is not None and has_922 and str(rec.entry.get("osVersion") or "").startswith("9.2.1"):
            rec = None
        self.recommended = rec
        deluxe, _ = _catalog_hero_picks(items)
        d_id = (deluxe.entry.get("id", "") if deluxe else "").lower()
        prefer_hero = deluxe is not None and not deluxe.known and (
            "mega-deluxe" in d_id or not any(str(it.entry.get("osVersion") or "").startswith("9.2.2") for it in items)
        )
        first = (deluxe if prefer_hero else None) or (
            self.recommended or next((it for it in self.visible() if not it.known), None)
            or next(iter(self.visible()), None)
        )
        self.selected = first.key if first else None
        if self.panel.screen is self:
            await self.panel.refresh()

    def visible(self) -> list:
        return [it for it in self.items or [] if self.show_older or not it.older or it.key == self.selected]

    def find(self, key: str | None):
        return next((it for it in self.items or [] if it.key == key), None)

    def _partial_info(self, it) -> tuple[int, int]:
        if it is None:
            return 0, 0
        dl = download_of(it.entry)
        total = int(dl.get("archiveBytes") or 0)
        partial = it.entry.get("partial_bytes")
        if isinstance(partial, int) and 0 < partial < total:
            return partial, total
        sha = dl.get("sha256")
        dl_dir = getattr(self.app.paths, "downloads", None)
        if sha and dl_dir:
            try:
                part = dl_dir / f"{sha}.part"
                if part.is_file():
                    have = part.stat().st_size
                    if 0 < have < total:
                        return have, total
            except OSError:
                pass
        return 0, total

    def _paused_text(self, have: int, total: int) -> str:
        mib = 1024 * 1024
        if total >= mib:
            return f"Paused · {have // mib} of {total // mib} MB"
        return f"Paused · {ui.human_bytes(have)} of {ui.human_bytes(total)}"

    def detail(self, it) -> str:
        """"Mac OS 8.1 · 68k · 58 MB" (the OS when the name doesn't say it), "Internet" for a
        Mac from the Internet catalog beside RetroBoot downloads, "Installed" when it's here."""
        from ..catalogs import CUSTOM_PREFIX, entry_family, os_label
        e = it.entry
        labelled = it.source.startswith(CUSTOM_PREFIX) or (it.source == "internet" and "lab" in self.sources)
        parts = [self.app.catalogs.label(it.source)] if labelled else []
        osl = os_label(e["osVersion"])
        if osl and osl.lower() not in e["name"].lower():
            parts.append(osl)
        if entry_family(e) == QUADRA800:
            parts.append(FAMILY_LABELS[QUADRA800])
        if it is self.recommended:
            parts.append("Recommended")
        have, total = self._partial_info(it)
        if have > 0:
            parts.append(self._paused_text(have, total))
        else:
            parts.append(ui.human_bytes(download_of(e)['archiveBytes']) + " download")
        if it.known:
            parts.append("Installed")
        return " · ".join(parts)

    def doc(self):
        items = [ui.text("Choose a Mac to download. It appears in Startup Disk." if self.app.machines.list() else
                         "Choose a Mac to download and install.")]
        if self.items is None:
            items += [ui.text("Loading the catalog…"), ui.progress(None)]
        elif self.error:
            items.append(ui.text(self.error, "emphasis"))
            if "network" not in self.error.lower():
                items.append(ui.text("Check the network connection, then try again."))
        else:
            deluxe, base = _catalog_hero_picks(self.items)
            d_id = (deluxe.entry.get("id", "") if deluxe else "").lower()
            has_922 = any(str(it.entry.get("osVersion") or "").startswith("9.2.2") for it in self.items)
            hero_shown = (
                deluxe is not None and base is not None and not getattr(deluxe, "known", False)
                and ("mega-deluxe" in d_id or not has_922)
            )
            show_lib = (not hero_shown) or bool(self.show_library) or (
                self.selected not in {None, deluxe.key if deluxe else None, base.key if base else None}
            )
            if hero_shown:
                hero_val = self.selected if self.selected in (deluxe.key, base.key) else ""
                is_mega = "mega-deluxe" in d_id
                d_label = "Mac OS 9.2.2 Mega Deluxe — Recommended" if is_mega else f"{deluxe.entry['name']} — Recommended"
                b_label = "Mac OS 9.2.2 — Classic Clean desktop" if is_mega else f"{base.entry['name']} — Classic Clean desktop"
                d_desc = ("Mega Deluxe: Kids & Family — Kid Pix Studio Deluxe, Zoombinis & 111 classic games · "
                          if is_mega else f"{deluxe.entry['name']}: {ui.clip_words(deluxe.entry.get('summary') or '', 110)} · ")
                b_desc = ("Mac OS 9.2.2: Classic Clean Mac OS 9 desktop · "
                          if is_mega else f"{base.entry['name']}: Classic Clean desktop · ")
                items.append(ui.group("Recommended Picks", [
                    ui.radio("hero_pick", "", [
                        (deluxe.key, d_label),
                        (base.key, b_label),
                    ], hero_val),
                    ui.text(d_desc + ui.human_bytes(download_of(deluxe.entry)["archiveBytes"]) + " download", "small"),
                    ui.text(b_desc + ui.human_bytes(download_of(base.entry)["archiveBytes"]) + " download", "small"),
                ]))
                items.append(ui.checkbox("library", f"Browse all {len(self.items)} Library Macs", show_lib))
            if show_lib:
                rows = [ui.row(it.key, it.entry["name"], self.detail(it), "mac")
                        for it in self.visible()]
                items.append(ui.lst("entry", rows, self.selected, rows=5 if hero_shown else 7))
                if any(it.older for it in self.items):
                    items.append(ui.checkbox("older", "Show older versions", self.show_older))
            sel = self.find(self.selected)
            if sel and show_lib:
                items.append(ui.text(ui.clip_words(sel.entry["summary"], 300), "small"))
            for sid, why in self.errors.items():
                items.append(ui.text(f"{self.app.catalogs.label(sid)}: {why}", "small"))
        sel = self.find(self.selected)
        have, _ = self._partial_info(sel)
        items.append(ui.buttons(ui.btn("servers", "Image Servers…", align="left"),
                                ui.btn("network", "Network…") if self.error or self.errors else None))
        items.append(ui.buttons(ui.btn("usb", "From a USB Disk…", align="left"),
                                ui.btn("retry", "Try Again") if self.error or self.errors else None,
                                ui.btn("cancel", "Cancel" if self.then else "Back"),
                                ui.btn("install", "Resume" if have > 0 else "Download", default=True, disabled=not sel)))
        return ui.window(self.doc_id, "Download a Mac", items, width=540)

    async def change(self, id, value):
        if id == "hero_pick" and self.find(value):
            self.selected = value
            await self.panel.refresh()
        elif id == "library":
            self.show_library = bool(value)
            await self.panel.refresh()
        elif id == "entry":
            self.selected = value
            await self.panel.refresh()
        elif id == "older":
            self.show_older = bool(value)
            await self.panel.refresh()

    async def action(self, id, values):
        p = self.panel
        if id == "cancel":
            await self.back()
        elif id == "usb":
            await p.show(UsbList(p, back=lambda: p.show(self), on_installed=self.on_installed, then=self.then))
        elif id == "servers":
            async def back():
                self.items, self.error, self.errors = None, None, {}
                await p.show(self)
                await self._load()
            await p.show(ImageServers(p, back))
        elif id == "retry":
            self.items, self.error, self.errors = None, None, {}
            await p.refresh()
            await self._load()
        elif id == "network":
            async def back():
                self.items, self.error, self.errors = None, None, {}
                await p.show(self)
                await self._load()
            await p.show(NetworkWeb(p, back=back))
        elif id == "install":
            it = self.find(values.get("entry") or self.selected)
            if not it:
                return
            spec = {"source": "catalog", "id": it.entry["id"], "catalog": it.source}
            then = self.then
            # A failed or stopped download comes back to this list (Try Again, another Mac);
            # after a download that worked, Not Now goes where the list came from.
            back = lambda job: self.back if job.state == SUCCEEDED else (lambda: p.show(self))
            await p.start_job(lambda: self.app.import_job(spec, origin="panel"),
                              f"Downloading “{it.entry['name']}”",
                              (lambda job: then(p, job, back(job))) if then else
                              (lambda job: after_import(p, job, back(job), self.on_installed)),
                              allow_cancel=True)


class UsbList(Screen):
    doc_id = "setup-usb"

    def __init__(self, panel, back, on_installed=None, then=None):
        super().__init__(panel)
        self.back = back
        self.items: list[dict] | None = None
        self.selected: str | None = None
        self.on_installed = on_installed
        self.then = then
        self.app.loop.create_task(self._load())

    async def _load(self):
        self.items = await self.app.archives.list()
        self.selected = self.items[0]["id"] if self.items else None
        if self.panel.screen is self:
            await self.panel.refresh()

    def doc(self):
        items = []
        if self.items is None:
            items += [ui.text("Looking for machine archives…"), ui.progress(None)]
        elif not self.items:
            items += [ui.text("No machine archives found.", "emphasis"),
                      ui.text("Insert a USB disk containing a RetroBoot machine archive (.tar.gz), "
                              "then click Look Again.")]
        else:
            rows = [ui.row(a["id"], a["name"], f"{ui.human_bytes(a['bytes'])} · {archive_origin(a['origin'])}", "disk")
                    for a in self.items]
            items.append(ui.lst("archive", rows, self.selected, rows=5))
        items.append(ui.buttons(ui.btn("rescan", "Look Again"), ui.btn("cancel", "Back"),
                                ui.btn("install", "Install", default=True, disabled=not self.selected)))
        return ui.window(self.doc_id, "Install from a Disk", items, width=520)

    async def change(self, id, value):
        if id == "archive":
            self.selected = value

    async def action(self, id, values):
        p = self.panel
        if id == "cancel":
            await self.back()
        elif id == "rescan":
            self.items = None
            await p.refresh()
            await self._load()
        elif id == "install" and self.selected:
            then = self.then
            await p.start_job(lambda: self.app.import_job({"source": "archive", "id": self.selected}, origin="panel"),
                              "Installing Mac OS", (lambda job: then(p, job, self.back)) if then else
                              (lambda job: after_import(p, job, self.back, self.on_installed)))


def archive_origin(origin: str) -> str:
    """"usb:STICK" -> "USB disk “STICK”"; "embedded" -> "the installer"."""
    kind, _, label = str(origin or "").partition(":")
    if kind == "usb":
        return f"USB disk “{label[:24]}”" if label else "USB disk"
    return {"embedded": "the installer", "upload": "uploaded"}.get(kind, kind or "disk")


def stopped_by_user(job: Job) -> bool:
    return (job.error or {}).get("code") == "cancelled"


async def after_import(panel: Panel, job: Job, back, on_installed=None):
    if stopped_by_user(job):
        await back()
        return
    if job.state != SUCCEEDED:
        await panel.message("stop", "Mac OS could not be installed.", job.message, then=back)
        return
    if on_installed:
        await on_installed(job)
        return
    app = panel.app
    mid = job.result["machine"]
    m = app.machines.get(mid)
    await panel.show(Installed(panel, m.name if m else mid))


async def after_download(panel: Panel, job: Job, back) -> None:
    """After Download a Mac (or a USB disk's archive) on a computer that already has Macs:
    offer to start up with the new one now."""
    if stopped_by_user(job):
        await back()
        return
    if job.state != SUCCEEDED:
        await panel.message("stop", "The Mac could not be downloaded.", job.message, then=back)
        return
    mid = job.result["machine"]
    await panel.show(StartUpWithNew(panel, mid, back))


class StartUpWithNew(Screen):
    """“Mac OS 8.1” is on this computer: start up with it now (Mac OS restarts into it, or
    starts) or later (it waits in Startup Disk)."""
    doc_id = "downloaded"

    def __init__(self, panel, machine_id: str, back):
        super().__init__(panel)
        self.machine_id, self.back = machine_id, back

    def doc(self):
        m = self.app.machines.get(self.machine_id)
        name = m.name if m else self.machine_id
        sup = self.app.supervisor
        if sup.active:
            running = sup.machine.name if sup.machine is not None else "Mac OS"
            detail = (f"Start up with it now? “{running}” restarts, so save your work in Mac OS first. "
                      "You can also choose it later in Startup Disk.")
            go = "Restart"
        else:
            detail = "Start up with it now? You can also choose it later in Startup Disk."
            go = "Start Up"
        return ui.alert(self.doc_id, "note", f"“{name}” is on this computer.", detail,
                        ui.buttons(ui.btn("cancel", "Not Now"), ui.btn("go", go, default=True)))

    async def action(self, id, values):
        p = self.panel
        if id == "cancel":
            await self.back()
        elif id == "go":
            await p.start_job(lambda: self.app.select_machine_job(self.machine_id, origin="panel"),
                              "Changing the startup Mac", self._after_select)

    async def _after_select(self, job: Job):
        p = self.panel
        if job.state != SUCCEEDED:
            await p.message("caution", "The startup Mac was not changed.", job.message, then=self.back)
        elif self.app.supervisor.active:
            await p.start_job(lambda: self.app.lifecycle_job("restart", origin="panel"), "Restarting Mac OS",
                              self._after_start, allow_force=True)
        else:
            await p.start_job(lambda: self.app.lifecycle_job("start", origin="panel"), "Starting Mac OS",
                              self._after_start)

    async def _after_start(self, job: Job):
        if job.state == SUCCEEDED:
            if self.panel.screen is None or isinstance(self.panel.screen, JobScreen):
                await self.panel.close()
            return
        await self.panel.message("stop", "Mac OS could not start.", job.message, then=self.panel.show_recovery)


class Installed(Screen):
    doc_id = "setup-done"

    def __init__(self, panel, name):
        super().__init__(panel)
        self.name = name

    def doc(self):
        return ui.alert(self.doc_id, "note", f"{self.name} is installed.",
                        "Click Start Mac OS to start it. From now on, it starts automatically when you turn "
                        "this computer on.",
                        ui.buttons(ui.btn("setup", "More Setup…"), ui.btn("start", "Start Mac OS", default=True)))

    async def action(self, id, values):
        p = self.panel
        if id == "setup":
            await p.show_recovery()
        elif id == "start":
            await p.start_job(lambda: self.app.lifecycle_job("start", origin="panel"), "Starting Mac OS",
                              self._after)

    async def _after(self, job):
        if job.state == SUCCEEDED:
            await self.panel.close()
        else:
            await self.panel.message("stop", "Mac OS could not start.", job.message, then=self.panel.show_recovery)


# --- time zone ---------------------------------------------------------------------------

def _utc_offset(zone: str) -> str:
    try:
        import datetime as _dt
        import zoneinfo
        off = _dt.datetime.now(zoneinfo.ZoneInfo(zone)).utcoffset()
        mins = int(off.total_seconds() // 60) if off is not None else 0
    except Exception:
        return ""
    sign = "+" if mins >= 0 else "-"
    mins = abs(mins)
    return f"UTC{sign}{mins // 60:02d}:{mins % 60:02d}"


def _city(zone: str) -> str:
    if "/" not in zone or zone.startswith("Etc/"):
        return zone
    return zone.split("/", 1)[1].replace("_", " ").replace("/", " – ")


class TimeZone(Screen):
    """Region popup + search field + zone list (searchable, region-grouped)."""
    doc_id = "timezone"
    MAX_ROWS = 150

    def __init__(self, panel, back, next_=None, title: str = "Time Zone"):
        super().__init__(panel)
        self.back, self.next_, self.title = back, next_, title
        cur = self.app.settings.desired.get("system.timezone", "UTC")
        # Real places only (no "ROK", "UCT" or "US/Eastern"); a current alias stays listed.
        self.regions = schema.timezone_regions(schema.listed_timezones() | {cur})
        self.selected = cur
        self.region = cur.split("/", 1)[0] if "/" in cur and cur.split("/", 1)[0] in self.regions else "Other"
        self.search = ""

    def zones(self) -> list[str]:
        if self.search.strip():
            q = self.search.strip().lower().replace(" ", "_")
            allz = [z for zs in self.regions.values() for z in zs]
            return [z for z in allz if q in z.lower()][: self.MAX_ROWS]
        return self.regions.get(self.region, [])[: self.MAX_ROWS * 4]

    def doc(self):
        zones = self.zones()
        rows = [ui.row(z, _city(z) if not self.search.strip() else z.replace("_", " "), _utc_offset(z)) for z in zones]
        sel = self.selected if self.selected in zones else None
        machine = self.app.machines.get(self.app.settings.desired.get("machine.active"))
        osx = (machine is not None and machine.osx) or (getattr(self.app, "mode", None) == "installer"
                                                       and self.app.startup.style() == "macosx")
        intro = ("Choose RetroBoot's time zone. Mac OS X keeps its own time zone in "
                 "System Preferences → Date & Time." if osx else
                 "Choose the time zone for the Mac clock. Type a city, or choose a region.")
        items = [ui.text(intro),
                 ui.columns(ui.popup("region", "Region:", [(r, r) for r in self.regions], self.region,
                                     disabled=bool(self.search.strip())),
                            ui.field("search", "Search:", self.search, placeholder="City")),
                 ui.lst("zone", rows, sel, rows=8),
                 ui.text(f"Selected: {self.selected.replace('_', ' ')} ({_utc_offset(self.selected)})", "small")]
        if self.app.supervisor.active and not osx:
            items.append(ui.text("The Mac clock changes the next time Mac OS restarts.", "small"))
        items.append(ui.buttons(ui.btn("cancel", "Back"),
                                ui.btn("set", "Continue" if self.next_ else "Set Time Zone", default=True)))
        return ui.window(self.doc_id, self.title, items, width=520)

    async def change(self, id, value):
        if id == "region" and value in self.regions:
            self.region = value
        elif id == "search":
            self.search = str(value or "")[:40]
            zones = self.zones()
            if self.search.strip() and zones and self.selected not in zones:
                # The first match is the choice, so Return after typing a city sets it.
                self.selected = zones[0]
        elif id == "zone" and isinstance(value, str):
            if value == self.selected:
                return
            self.selected = value      # and the "Selected:" line follows
        else:
            return
        await self.panel.refresh()

    async def action(self, id, values):
        if id == "cancel":
            await self.back()
            return
        zone = values.get("zone") or self.selected
        if zone != self.app.settings.desired.get("system.timezone"):
            try:
                await self.app.apply_engine.change_and_apply({"system.timezone": zone})
            except schema.ValidationError as exc:
                await self.panel.message("caution", "That time zone is not available.", "; ".join(exc.fields.values()),
                                         then=lambda: self.panel.show(self))
                return
        await (self.next_() if self.next_ else self.back())
