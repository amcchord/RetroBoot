"""Recovery → Bluetooth…: pair, connect and forget Bluetooth input devices.

* :class:`BluetoothDevices` — the list (paired devices with a check mark when
  connected, then devices nearby in pairing mode with their signal), "Use
  Bluetooth", Scan Again, Forget…, Connect.  One click on a device in pairing
  mode pairs it, like joining a network in the Wi-Fi window.
* :class:`BluetoothPairing` — progress, then the code to type on a keyboard
  ("Type this code on “K380”, then press Return"), or the number to compare;
  ends with "“K380” is connected." or the reason it failed.
* The setup assistant (no keyboard attached; never over the Mac OS startup):
  :class:`KeyboardOffer` ("No keyboard is connected. Pair a Bluetooth
  keyboard?"; Pair Keyboard… opens the Bluetooth window) when a pointer is
  attached, :class:`BluetoothSearch` when nothing is: it pairs the first
  keyboard or mouse in pairing mode and shows the same pairing window
  (:func:`assistant_prompt`), then says when the device is connected
  (:func:`assistant_finished`). :func:`assistant_update` puts these windows up
  and takes them away for the service.

Everything is reachable with the keyboard (Tab, arrows, Return, Escape) and the
pointer. The code is never logged; hostd's redactor holds it while shown.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from ..bluetooth import CODE_STAGES, KEYBOARD_KINDS, BluetoothError, message_for
from ..jobs import Busy, Job, SUCCEEDED
from . import ui
from .controller import Screen

if TYPE_CHECKING:  # pragma: no cover
    from .controller import Panel

log = logging.getLogger("retroboot.panel")

WIDTH = 520


def _bt(screen) -> Any:
    return screen.app.bluetooth


def _listen(panel: "Panel") -> None:
    """One listener per panel: Bluetooth changes refresh a Bluetooth window at once."""
    bt = panel.app.bluetooth
    if getattr(panel, "_bt_listener", None) in bt.listeners:
        return
    pending = {"task": None}

    def changed() -> None:
        scr = panel.screen
        if not panel.visible or not isinstance(scr, (BluetoothDevices, BluetoothPairing, BluetoothSearch)):
            return
        t = pending["task"]
        if t is None or t.done():
            pending["task"] = panel.app.loop.create_task(scr.update())

    panel._bt_listener = changed
    bt.listeners.append(changed)


class _BTScreen(Screen):
    async def update(self) -> None:
        """Send the document again only when it changed."""
        await asyncio.sleep(0.05)       # coalesce a burst of BlueZ property changes
        if self.panel.screen is not self or not self.panel.visible:
            return
        shown = {k: v for k, v in (self.panel.last_doc or {}).items() if k != "reset"}
        if self.doc() != shown:
            await self.panel.refresh()

    async def on_tick(self) -> None:
        await self.update()


def device_detail(d: dict) -> str:
    parts = [d["kind_label"]]
    if d["paired"]:
        parts.append("Connected" if d["connected"] else "Not connected")
        if d.get("battery") is not None:
            parts.append(f"Battery {d['battery']}%")
    else:
        parts.append("Not paired")
    return " · ".join(parts)


class BluetoothDevices(_BTScreen):
    doc_id = "bluetooth"
    tick = 1.0

    def __init__(self, panel, back: Callable[[], Awaitable[None]] | None = None, selected: str | None = None,
                 notice: str | None = None):
        super().__init__(panel)
        self.back = back
        self.selected = selected
        self.notice = notice
        _listen(panel)
        bt = _bt(self)
        if bt.capable() and bt.enabled():
            bt.scan("panel")

    def _back(self) -> Awaitable[None]:
        return self.back() if self.back else self.panel.show_recovery()

    def reopen(self, selected: str | None = None, notice: str | None = None) -> Awaitable[None]:
        return self.panel.show(BluetoothDevices(self.panel, self.back, selected, notice))

    def selection(self, st: dict) -> dict | None:
        return next((d for d in st["devices"] if d["address"] == self.selected), None)

    def doc(self):
        bt = _bt(self)
        st = bt.public("panel")
        items: list = []
        if not st["available"]:
            items.append(ui.columns(ui.icon("bluetooth", 32), ui.stack(
                ui.text("No Bluetooth hardware", "emphasis"),
                ui.text("This computer has no Bluetooth adapter. Use a USB keyboard and mouse instead."))))
            items.append(ui.buttons(ui.btn("cancel", "Done", default=True)))
            return ui.window(self.doc_id, "Bluetooth", items, width=WIDTH)
        state = st["state"]
        devices = st["devices"]
        found = [d for d in devices if not d["paired"]]
        if state == "blocked":
            status, hint = "Bluetooth is off.", "It is turned off with this computer's wireless switch or key."
        elif state == "off":
            status, hint = "Bluetooth is off.", "Turn Bluetooth on to use Bluetooth keyboards, mice and trackpads."
        else:
            names = [d["name"] for d in devices if d["paired"] and d["connected"]]
            status = (f"“{names[0]}” is connected." if len(names) == 1 else
                      f"{len(names)} devices are connected." if names else "Bluetooth is on.")
            if found:
                hint = "Click a device to pair it."
            elif st["scanning"]:
                hint = "Looking for devices… Put your keyboard, mouse or trackpad in pairing mode."
            else:
                hint = "No new devices found. Put a device in pairing mode, then click Scan Again."
        items.append(ui.columns(ui.icon("bluetooth", 32), ui.stack(ui.text(status, "emphasis"), ui.text(hint))))
        items.append(ui.checkbox("enabled", "Use Bluetooth", state not in ("off", "blocked"),
                                 disabled=state == "blocked"))
        items.append(ui.checkbox("offer", "Offer to pair a keyboard when none is connected", bt.offer_enabled(),
                                 disabled=state in ("off", "blocked")))
        rows = []
        for d in devices:
            if d["paired"]:
                rows.append(ui.row(d["address"], d["name"], device_detail(d), checked=d["connected"]))
            else:
                rows.append(ui.row(d["address"], d["name"], device_detail(d), checked=False,
                                   signal=d["signal"] or 1))
        sel = self.selection(st)
        on = state == "on"
        items.append(ui.lst("device", rows, sel["address"] if sel else None, rows=6, activate="click",
                            disabled=not on))
        if on and st["scanning"] and not found:
            items.append(ui.progress(None))
        if self.notice:
            items.append(ui.text(self.notice, "small"))
        buttons = [ui.btn("scan", "Scan Again", disabled=not on, align="left")]
        if sel and sel["paired"]:
            buttons.append(ui.btn("forget", "Forget…"))
        if sel and on and not sel["paired"]:
            buttons += [ui.btn("cancel", "Done"), ui.btn("pair", "Pair", default=True)]
        elif sel and on and sel["paired"] and not sel["connected"]:
            buttons += [ui.btn("cancel", "Done"), ui.btn("connect", "Connect", default=True)]
        else:
            buttons.append(ui.btn("cancel", "Done", default=True))
        items.append(ui.buttons(*buttons))
        return ui.window(self.doc_id, "Bluetooth", items, width=WIDTH)

    async def on_tick(self) -> None:
        bt = _bt(self)
        if bt.capable() and bt.enabled() and bt.scanning_for("panel"):
            bt.scan("panel")            # keep looking while the window is open
        await self.update()

    async def change(self, id, value):
        bt = _bt(self)
        if id == "device":
            self.selected = value
            self.notice = None
            await self.update()
        elif id == "enabled":
            try:
                await self.app.apply_engine.change_and_apply({"bluetooth.enabled": bool(value)}, origin="panel")
            except Exception as exc:     # ValidationError: the adapter went away
                log.info("Bluetooth setting not applied: %s", exc)
            if bool(value):
                bt.scan("panel")
            await self.panel.refresh()
        elif id == "offer":
            try:
                await self.app.apply_engine.change_and_apply({"bluetooth.offer_keyboard_setup": bool(value)},
                                                             origin="panel")
            except Exception as exc:
                log.info("Bluetooth setting not applied: %s", exc)
            await self.panel.refresh()

    async def action(self, id, values):
        bt = _bt(self)
        if id == "cancel":
            bt.stop_scan("panel")
            await self._back()
            return
        if id == "scan":
            self.notice = None
            bt.scan("panel", restart=True)
            await self.panel.refresh()
            return
        st = bt.public("panel")
        if id in ("device", "pair", "connect"):
            addr = values.get("device") or self.selected
            d = next((x for x in st["devices"] if x["address"] == addr), None)
            if d is None:
                return
            self.selected = d["address"]
            if not d["paired"] and id in ("device", "pair"):
                await start_pairing(self, d)
            elif d["paired"] and not d["connected"] and id == "connect":
                await self._connect(d)
            else:
                await self.panel.refresh()      # a paired device: selected (Forget…, Connect)
        elif id == "forget":
            d = self.selection(st)
            if d and d["paired"]:
                await self.panel.show(ForgetDevice(self.panel, d, self))

    async def _connect(self, d: dict) -> None:
        name = d["name"]

        async def after(job: Job):
            if job.state == SUCCEEDED:
                await self.reopen(d["address"], f"“{name}” is connected.")
            else:
                await self.panel.message("caution", f"Could not connect to “{name}”.", job.message,
                                         then=lambda: self.reopen(d["address"]))
        await self.panel.start_job(lambda: self.app.bluetooth_job("connect", origin="panel", address=d["address"]),
                                   f"Connecting to “{name}”…", after)


async def start_pairing(screen: BluetoothDevices, d: dict) -> None:
    panel, app = screen.panel, screen.app
    try:
        job = app.bluetooth_job("pair", origin="panel", address=d["address"])
    except Busy:
        await panel.message("caution", "RetroBoot is busy.", message_for("busy"), then=lambda: screen.reopen())
        return
    await panel.show(BluetoothPairing(panel, job, d, back=lambda: screen.reopen()))


class BluetoothPairing(_BTScreen):
    """Progress, the code, then the outcome. Escape (Cancel) stops the pairing."""

    doc_id = "bluetooth-pair"
    tick = 0.5

    def __init__(self, panel, job: Job | None, device: dict, back: Callable[[], Awaitable[None]],
                 assistant: bool = False):
        super().__init__(panel)
        self.job, self.device, self.back, self.assistant = job, device, back, assistant
        self.assistant_screen = "pairing" if assistant else None
        self.address = device["address"]
        self.cancelling = False
        _listen(panel)

    def session(self):
        s = _bt(self).session
        if s and s.address == self.address and (self.job is None or s.job_id in (None, self.job.id)):
            return s
        return None

    def doc(self):
        s = self.session()
        stage = s.stage if s else "starting"
        name = self.device["name"]
        kind = s.kind if s else self.device.get("kind")
        code = s.passkey if s and stage in CODE_STAGES else None
        icon = ui.icon("bluetooth", 32)
        if code and stage in ("passkey", "pin"):
            if kind in KEYBOARD_KINDS or kind == "other":
                head = f"Type this code on “{name}”, then press Return:"
                note = "Type it on the Bluetooth keyboard itself."
            else:
                head = f"Enter this code on “{name}”:"
                note = "Follow the device's instructions for entering a code."
            if self.assistant:
                note = "RetroBoot found this Bluetooth keyboard in pairing mode. " + note
            items = [ui.columns(icon, ui.stack(ui.text(head, "emphasis"), ui.text(note, "small"))),
                     dict(ui.text(" ".join(code), "large"), align="center")]
            if s.entered:
                items.append(dict(ui.text(f"{s.entered} of {len(code)} digits typed", "small"), align="center"))
            items.append(ui.buttons(ui.btn("cancel", "Cancel")))
            return ui.window(self.doc_id, "Bluetooth", items, width=WIDTH)
        if code and stage == "confirm":
            items = [ui.columns(icon, ui.stack(ui.text(f"Does “{name}” show this code?", "emphasis"),
                                               ui.text("If the codes match, click Pair." if not self.assistant
                                                       else "Confirm on the device if the codes match.",
                                                       "small"))),
                     dict(ui.text(" ".join(code), "large"), align="center")]
            if self.assistant:
                items.append(ui.buttons(ui.btn("cancel", "Cancel")))
            else:
                items.append(ui.buttons(ui.btn("cancel", "Cancel"), ui.btn("confirm", "Pair", default=True)))
            return ui.window(self.doc_id, "Bluetooth", items, width=WIDTH)
        if self.cancelling:
            line = "Canceling…"
        elif stage == "connecting":
            line = f"Connecting to “{name}”…"
        else:
            line = f"Pairing with “{name}”…"
        items = [ui.columns(icon, ui.stack(ui.text(line, "emphasis"),
                                           ui.text("Keep the device turned on and near this computer.", "small"))),
                 ui.progress(None),
                 ui.buttons(ui.btn("cancel", "Cancel", disabled=self.cancelling))]
        return ui.window(self.doc_id, "Bluetooth", items, width=WIDTH)

    async def on_tick(self) -> None:
        if self.job is not None and self.job.done.is_set():
            await finish(self.panel, self.job, self.device, self.back, self.assistant, self)
            return
        await self.update()

    async def action(self, id, values):
        bt = _bt(self)
        if id == "confirm":
            bt.confirm(True)
            await self.update()
        elif id == "cancel":
            if self.job is None or self.job.done.is_set():
                await self.back()
                return
            self.cancelling = True
            await self.panel.refresh()
            await bt.cancel_pairing()


async def finish(panel: "Panel", job: Job, device: dict, back: Callable[[], Awaitable[None]], assistant: bool,
                 screen: Screen | None = None) -> None:
    """Say how the pairing went, once, then go back."""
    bt = panel.app.bluetooth
    shown = bt.__dict__.setdefault("_results_shown", set())
    if job.id in shown:
        return
    shown.add(job.id)
    name = device["name"]
    if job.state == SUCCEEDED:
        res = job.result or {}
        if res.get("connected", True):
            msg, detail = f"“{name}” is connected.", "It connects by itself whenever it is on and near this computer."
        else:
            msg, detail = f"“{name}” is paired.", "Press a key or click it to connect it."
        await panel.message("note", msg, detail, then=back, auto_seconds=6 if assistant else None)
        return
    code = (job.error or {}).get("code")
    if code in ("canceled", "cancelled") and (screen is None or getattr(screen, "cancelling", False) or assistant):
        if panel.screen is screen or screen is None:
            await back()
        return
    await panel.message("caution", f"Could not pair with “{name}”.", job.message or message_for(code or "failed", name),
                        then=back, auto_seconds=10 if assistant else None)


class ForgetDevice(Screen):
    doc_id = "bluetooth-forget"

    def __init__(self, panel, device: dict, devices: BluetoothDevices):
        super().__init__(panel)
        self.device, self.devices = device, devices

    def doc(self):
        name = self.device["name"]
        # Destructive: Cancel is the default, so Return never forgets.
        return ui.alert(self.doc_id, "caution", f"Forget “{name}”?",
                        "This computer removes the pairing. To use the device again, put it in pairing mode and "
                        "pair it again.",
                        ui.buttons(ui.btn("cancel", "Cancel", default=True), ui.btn("forget", "Forget Device")),
                        width=440)

    async def action(self, id, values):
        name, addr = self.device["name"], self.device["address"]
        if id != "forget":
            await self.devices.reopen(addr)
            return
        try:
            await self.app.bluetooth.forget(addr, origin="panel")
        except BluetoothError as exc:
            await self.panel.message("caution", f"Could not forget “{name}”.", message_for(exc.code, name),
                                     then=lambda: self.devices.reopen(addr))
            return
        await self.devices.reopen(None, f"“{name}” was forgotten.")


# --- the setup assistant ------------------------------------------------------------------
# The service decides when (retroboot/bluetooth.py: never over the Mac OS startup;
# after the Finder or with Mac OS stopped, or in first-time setup and the
# installer); these windows are what people see. The offer and the search
# remember the screen they covered and return to it (or to Mac OS).

# Windows the assistant never covers in first-time setup or with Mac OS stopped:
# the person is in the middle of something. Over Mac OS it waits for any panel.
BUSY_DOCS = {"job-progress", "keep-setting", "update-progress", "power", "wifi-join", "bluetooth",
             "bluetooth-pair", "bluetooth-forget"}


def _busy(panel: "Panel") -> bool:
    scr = panel.screen
    if not panel.visible or scr is None or getattr(scr, "assistant_screen", None):
        return False
    doc_id = getattr(scr, "doc_id", "") or ""
    if doc_id in BUSY_DOCS or doc_id.startswith("confirm"):
        return True
    return doc_id == "message" and getattr(scr, "auto", None) is not None    # a note that closes by itself


async def _assistant_leave(panel: "Panel") -> None:
    """Back to the screen the assistant covered, or to Mac OS."""
    under = getattr(panel, "_bt_under", None)
    panel._bt_under = None
    if under is not None:
        await panel.show(under)
    else:
        await panel.close()


def _assistant_return(panel: "Panel") -> Callable[[], Awaitable[None]]:
    """Where a window the assistant opened goes afterwards: the offer or the search
    if still needed (a mouse was paired: now the keyboard), else back."""
    async def back() -> None:
        bt = panel.app.bluetooth
        stage = bt.assistant.get("stage")
        if stage == "offer":
            await panel.show(KeyboardOffer(panel))
        elif stage == "search":
            await panel.show(BluetoothSearch(panel))
        else:
            await _assistant_leave(panel)
    return back


async def assistant_update(panel: "Panel", stage: str | None, context: str | None) -> None:
    """Called by the service every few seconds with what it wants on screen."""
    scr = panel.screen if panel.visible else None
    own = getattr(scr, "assistant_screen", None)
    if stage not in ("offer", "search"):
        if own in ("offer", "search"):
            await _assistant_leave(panel)
        return
    if own == stage or own == "pairing":
        return
    if scr is not None and not own and (context == "finder" or _busy(panel)):
        return              # a window over Mac OS, or something in progress: later
    if not own:
        panel._bt_under = scr
    panel.app.lifecycle.event("bluetooth_assistant_shown", stage=stage, context=context,
                              over=getattr(scr, "doc_id", None) if scr is not None else "mac-os")
    await panel.show(KeyboardOffer(panel) if stage == "offer" else BluetoothSearch(panel))


class KeyboardOffer(Screen):
    """A pointer but no keyboard: a small notice. Nothing scans or pairs until the
    person clicks Pair Keyboard…; Not Now keeps it away until the next boot or a
    keyboard change."""

    doc_id = "bluetooth-offer"
    assistant_screen = "offer"

    def doc(self):
        return ui.alert(self.doc_id, "bluetooth", "No keyboard is connected. Pair a Bluetooth keyboard?",
                        "Turn the keyboard on and put it in pairing mode, then click Pair Keyboard.",
                        ui.buttons(ui.btn("cancel", "Not Now"), ui.btn("pair", "Pair Keyboard…", default=True)),
                        width=440)

    async def action(self, id, values):
        if self.panel.screen is not self:
            return
        bt = _bt(self)
        if id == "pair":
            under = getattr(self.panel, "_bt_under", None)
            self.panel._bt_under = None
            bt.assistant_dismiss("accepted")
            back = (lambda: self.panel.show(under)) if under is not None else self.panel.close
            await self.panel.show(BluetoothDevices(self.panel, back=back))
            return
        bt.assistant_dismiss("not-now")
        await _assistant_leave(self.panel)


class BluetoothSearch(_BTScreen):
    """Neither a keyboard nor a pointer: look for one in pairing mode and pair it,
    as a Mac's Bluetooth Setup Assistant does. A keyboard's code appears in the
    pairing window; a mouse needs none."""

    doc_id = "bluetooth-assistant"
    tick = 1.0
    assistant_screen = "search"

    def __init__(self, panel):
        super().__init__(panel)
        _listen(panel)

    def doc(self):
        bt = _bt(self)
        s = bt.session
        if s and not s.done and s.origin == "assistant":
            line = (f"Connecting to “{s.name}”…" if s.stage == "connecting" else f"Pairing with “{s.name}”…")
        else:
            line = "Looking for a Bluetooth keyboard or mouse…"
        items = [ui.columns(ui.icon("bluetooth", 32), ui.stack(
                     ui.text("No keyboard or mouse is connected.", "emphasis"),
                     ui.text("Turn on a Bluetooth keyboard or mouse and put it in pairing mode. "
                             "RetroBoot pairs the first one it finds."))),
                 ui.text(line, "small"), ui.progress(None),
                 ui.buttons(ui.btn("cancel", "Not Now"))]
        return ui.window(self.doc_id, "Bluetooth Setup", items, width=WIDTH)

    async def action(self, id, values):
        if id != "cancel" or self.panel.screen is not self:
            return
        bt = _bt(self)
        bt.assistant_dismiss("not-now")
        s = bt.session
        if s and not s.done and s.origin == "assistant":
            await bt.cancel_pairing()
        await _assistant_leave(self.panel)


async def assistant_prompt(panel: "Panel", sess) -> None:
    """The assistant reached a code: show it (again, if something hid it)."""
    scr = panel.screen
    if panel.visible and isinstance(scr, BluetoothPairing) and scr.address == sess.address:
        return
    if getattr(panel, "_bt_prompting", False):
        return
    panel._bt_prompting = True
    try:
        await _assistant_prompt(panel, sess)
    finally:
        panel._bt_prompting = False


async def _assistant_prompt(panel: "Panel", sess) -> None:
    app = panel.app
    job = app.jobs.get(sess.job_id) if sess.job_id else None
    if job is not None and job.done.is_set():
        return
    # It comes from the search window, which remembers what it covered.
    device = {"address": sess.address, "name": sess.name, "kind": sess.kind}
    app.lifecycle.event("bluetooth_assistant_prompt", address=sess.address, stage=sess.stage)
    await panel.show(BluetoothPairing(panel, job, device, back=_assistant_return(panel), assistant=True))


async def assistant_finished(panel: "Panel", job: Job, sess) -> None:
    """A device the assistant paired is ready (a mouse needs no code): say so briefly."""
    await asyncio.sleep(0.6)            # the pairing window's own tick usually answers first
    scr = panel.screen
    if panel.visible and isinstance(scr, BluetoothPairing) and scr.job is job:
        await finish(panel, job, scr.device, scr.back, True, scr)
        return
    if job.state != SUCCEEDED or sess is None:
        return
    if not (panel.visible and getattr(scr, "assistant_screen", None)):
        return                          # the person moved on
    device = {"address": sess.address, "name": sess.name, "kind": sess.kind}
    await finish(panel, job, device, _assistant_return(panel), True)
