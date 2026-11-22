"""Software Update in the classic panel (the RetroBoot window → Software Update; ARCHITECTURE 13).

Same jobs as the web page: install from a USB disk or the update server, Restart to
Update, Go Back to the Previous Version.  Mac OS keeps running while an update is
written; it shuts down cleanly only for the restart.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..jobs import SUCCEEDED, Job
from . import ui
from .controller import Screen

if TYPE_CHECKING:  # pragma: no cover
    from .controller import Panel


def _when(at: str, zone: str | None) -> str:
    """A check's UTC time in this computer's time zone: "today at 5:35 PM", "October 23 at
    9:02 AM"."""
    import datetime as _dt
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(zone or "UTC")
        t = _dt.datetime.fromisoformat(at.replace("Z", "+00:00")).astimezone(tz)
        now = _dt.datetime.now(tz)
    except Exception:  # noqa: BLE001 -- an odd stored time or zone: the raw time
        return at[:16].replace("T", " at ")
    clock = t.strftime("%I:%M %p").lstrip("0")
    if t.date() == now.date():
        return f"today at {clock}"
    if t.date() == now.date() - _dt.timedelta(days=1):
        return f"yesterday at {clock}"
    return f"{t.strftime('%B')} {t.day} at {clock}"


def _short(text: str, n: int = 160) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


class SoftwareUpdate(Screen):
    """One status and one main button.  Opening the window checks the update server (RetroBoot
    downloads when this computer is signed in, or the update.url setting) unless it was checked
    in the last CHECK_AGE_S, and looks on USB disks; then it says what there is: an update ready
    to install (Restart to Update), one to download (Download and Install), one on a USB disk,
    or that RetroBoot is up to date.  Go Back to the previous version sits at the left."""
    doc_id = "software-update"
    tick = 1.0
    CHECK_AGE_S = 600.0

    def __init__(self, panel: "Panel"):
        super().__init__(panel)
        self.scanning = True
        self.check: Job | None = None
        self.app.loop.create_task(self._scan())
        self._auto_check()

    def _auto_check(self) -> None:
        u = self.app.updates
        p = u.public()
        st = p.get("staged") or {}
        if not p.get("available") or not p.get("url") or self._job() or st.get("status") == "ready":
            return
        at = (p.get("check") or {}).get("at")
        if at:
            import datetime as _dt
            try:
                age = (_dt.datetime.now(_dt.timezone.utc) -
                       _dt.datetime.fromisoformat(at.replace("Z", "+00:00"))).total_seconds()
            except ValueError:
                age = None
            if age is not None and 0 <= age < self.CHECK_AGE_S:
                return
        try:
            self.check = u.check_job(origin="panel")
        except Exception:  # noqa: BLE001 -- busy: the window shows what it knows
            self.check = None

    async def _scan(self):
        try:
            await self.app.updates.scan_usb()
        finally:
            self.scanning = False
            if self.panel.screen is self:
                await self.panel.refresh()

    def _job(self) -> Job | None:
        return self.app.jobs.holder("update")

    def doc(self):
        u = self.app.updates
        p = u.public()
        cur = p["current"]
        head = ui.columns(ui.icon("update", 32), ui.stack(
            ui.text(f"RetroBoot {cur.get('version') or ''}", "emphasis"),
            ui.text(f"This computer has RetroBoot {cur['label']}.", "small")))
        items = [head]
        if not p["available"]:
            items.append(ui.text(p["reason"] or "Software Update isn't available."))
            items.append(ui.buttons(ui.btn("cancel", "Done", default=True)))
            return ui.window(self.doc_id, "Software Update", items, width=500)
        job = self._job()
        last = p.get("last_result") or {}
        st = p.get("staged") or {}
        found = (p.get("check") or {}).get("found")
        usb = p.get("usb") or []
        action = None                   # the main button, the default one
        discard = None                  # Don't Install, at the left of the bottom row
        if job and job.kind == "update.install":
            items += [ui.text(job.message or "Downloading…", "emphasis"), ui.progress(job.progress)]
        elif st.get("status") in ("ready", "failed"):
            again = st.get("status") == "failed"
            items.append(ui.text(f"RetroBoot {st['version']} didn't finish starting last time." if again else
                                 f"RetroBoot {st['version']} is ready to install.", "emphasis"))
            if st.get("notes"):
                items.append(ui.text(_short(st["notes"], 300), "small"))
            items.append(ui.text("Mac OS shuts down first, as if you chose Shut Down. Your Macs, documents and "
                                 "settings are kept.", "small"))
            discard = ui.btn("discard", "Don't Install", align="left")
            action = ui.btn("restart", "Try Again" if again else "Restart to Update", default=True)
        elif job and job.kind == "update.check":
            items += [ui.text("Checking for updates…", "emphasis"), ui.progress(None)]
        elif found:
            items.append(ui.text(f"RetroBoot {found['version']} is available.", "emphasis"))
            if found.get("notes"):
                items.append(ui.text(_short(found["notes"], 300), "small"))
            items.append(ui.text(f"The download ({ui.human_bytes(found.get('bytes'))}) runs while Mac OS keeps "
                                 "running; then RetroBoot asks to restart.", "small"))
            action = ui.btn("download", "Download and Install", default=True)
        elif usb:
            b = usb[0]
            items.append(ui.text(f"RetroBoot {b['label']} is on {b.get('disk', 'a USB disk')}.", "emphasis"))
            action = ui.btn("usb", "Install from USB Disk", default=True)
        elif p.get("url"):
            check = p.get("check") or {}
            failed = self.check is not None and self.check.state == "failed"
            if failed:
                items.append(ui.text("RetroBoot couldn't check for updates.", "emphasis"))
                items.append(ui.text(self.check.message or "", "small"))
            elif check.get("at"):
                items.append(ui.text("RetroBoot is up to date.", "emphasis"))
                items.append(ui.text(f"Checked {_when(check['at'], self.app.settings.applied.get('system.timezone'))}.",
                                     "small"))
            else:
                items.append(ui.text("RetroBoot hasn't checked for updates yet.", "emphasis"))
            action = ui.btn("check", "Check Again" if check.get("at") or failed else "Check for Updates")
        elif self.scanning:
            items.append(ui.text("Looking for updates on USB disks…"))
        else:
            items.append(ui.text("No update server is set, and no USB disk has an update.", "emphasis"))
            items.append(ui.text("Sign in to RetroBoot downloads in the installer, or put a RetroBoot update file on a "
                                 "USB disk and connect it. Software Update on the web page works too.", "small"))
        if last.get("message") and last.get("result") in ("failed", "rolled-back") and not job \
                and st.get("status") != "ready":
            items.append(ui.text(_short(last["message"], 220), "small"))
        prev = p.get("previous")
        goback = None
        if prev and not (job and job.kind == "update.install"):
            word = "Switch to" if prev.get("newer") else "Go Back to"
            goback = ui.btn("goback", f"{word} {prev['version']}…", align="left", disabled=bool(job))
        items.append(ui.buttons(discard, goback, ui.btn("cancel", "Done", default=action is None or
                                                            not action.get("default")), action))
        return ui.window(self.doc_id, "Software Update", items, width=520)

    async def on_tick(self):
        await self.panel.refresh()

    async def action(self, id, values):
        p, u = self.panel, self.app.updates
        if id == "cancel":
            await p.show_recovery()
        elif id == "usb":
            items = u.public().get("usb") or []
            if items:
                await p.start_job(lambda: u.install_job({"kind": "usb", "id": items[0]["id"]}, origin="panel"),
                                  "Software Update", self._after_install)
        elif id == "check":
            try:
                self.check = u.check_job(origin="panel")
            except Exception:  # noqa: BLE001 -- another update job runs: the window shows it
                self.check = None
            await p.refresh()
        elif id == "download":
            await p.start_job(lambda: u.install_job({"kind": "feed"}, origin="panel"), "Software Update",
                              self._after_install)
        elif id == "restart":
            st = u.staged() or {}
            await p.show(ConfirmRestartUpdate(p, st.get("label") or st.get("version") or ""))
        elif id == "discard":
            u.discard_staged()
            await p.refresh()
        elif id == "goback":
            prev = u.public().get("previous")
            if prev:
                await p.show(ConfirmGoBack(p, prev))

    async def _after_install(self, job: Job):
        if job.state == SUCCEEDED:
            await self.panel.show(SoftwareUpdate(self.panel))
        else:
            await self.panel.message("caution", "The update couldn't be installed.", job.message,
                                     then=lambda: self.panel.show(SoftwareUpdate(self.panel)))


class ConfirmRestartUpdate(Screen):
    doc_id = "confirm-update-restart"

    def __init__(self, panel, label: str):
        super().__init__(panel)
        self.label = label

    def doc(self):
        return ui.alert(self.doc_id, "note", f"Restart to install RetroBoot {self.label}?",
                        "Mac OS shuts down first, as if you chose Shut Down from the Special menu. "
                        "Applications can ask you to save your documents. Your Mac, documents and settings are kept.",
                        ui.buttons(ui.btn("cancel", "Cancel"), ui.btn("restart", "Restart to Update", default=True)),
                        width=460)

    async def action(self, id, values):
        p = self.panel
        if id == "cancel":
            await p.show(SoftwareUpdate(p))
        elif id == "restart":
            await p.start_job(lambda: self.app.updates.restart_job(origin="panel"), "Restart to Update", self._after,
                              allow_force=False)

    async def _after(self, job: Job):
        if job.state != SUCCEEDED:
            await self.panel.message("caution", "RetroBoot didn't restart to update.", job.message,
                                     then=lambda: self.panel.show(SoftwareUpdate(self.panel)))


class ConfirmGoBack(Screen):
    doc_id = "confirm-update-goback"

    def __init__(self, panel, prev: dict):
        super().__init__(panel)
        self.prev = prev

    def doc(self):
        v = self.prev["version"]
        word = "Switch to" if self.prev.get("newer") else "Go back to"
        extra = []
        if not self.prev.get("update_capable", True):
            extra.append(ui.text(f"RetroBoot {v} can't install updates itself. To update again later, use the "
                                 "RetroBoot installer USB disk.", "small"))
        return ui.alert(self.doc_id, "caution", f"{word} RetroBoot {v}?",
                        "Mac OS shuts down first, then RetroBoot restarts with that version. Your Mac, documents "
                        "and settings are kept.",
                        ui.buttons(ui.btn("cancel", "Cancel", default=True), ui.btn("goback", f"{word} {v}")),
                        extra=extra, width=460)

    async def action(self, id, values):
        p = self.panel
        if id == "cancel":
            await p.show(SoftwareUpdate(p))
        elif id == "goback":
            await p.start_job(lambda: self.app.updates.rollback_job(origin="panel"), "Previous Version", self._after)

    async def _after(self, job: Job):
        if job.state != SUCCEEDED:
            await self.panel.message("caution", "RetroBoot didn't switch versions.", job.message,
                                     then=lambda: self.panel.show(SoftwareUpdate(self.panel)))


class UpdateProgress(Screen):
    doc_id = "update-progress"
    guest_background_ok = False

    def __init__(self, panel, title: str, detail: str | None, progress: float | None):
        super().__init__(panel)
        self.title, self.detail, self.value = title, detail, progress

    def doc(self):
        items = [ui.text(self.title, "emphasis")]
        if self.detail:
            items.append(ui.text(self.detail))
        items.append(ui.progress(self.value))
        return ui.window(self.doc_id, "Software Update", items, width=440)
