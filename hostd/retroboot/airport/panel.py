"""RetroBoot's own password panel for AirPort joins (classic panel over the guest).

When Mac OS's AirPort software selects a WPA/WPA2/WPA3 network that has no
host profile, hostd shows this window over the dimmed guest, joins through
NetworkManager and returns to Mac OS.  The guest's own password dialog never
appears for such networks (they are presented without the privacy bit), and
the password is never given to the emulated card.  The window uses the
ordinary panel machinery (ARCHITECTURE §4.2), so the recovery chord and other
panel flows can replace it at any time; that counts as Cancel.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ..util import mono

from ..panel import ui
from ..panel.controller import Screen

if TYPE_CHECKING:
    from ..hostd import Hostd

log = logging.getLogger("retroboot.airport")

PROMPT_TIMEOUT = 300.0
STARTUP_QUIET_S = 180.0    # no password window this long after a launch unless the Finder is up


def mac_os_starting(app: "Hostd") -> bool:
    """Mac OS is starting up: launched, and neither past its startup (the supervisor's
    mac_os_up: the Finder recognized, or Mac OS started without it) nor STARTUP_QUIET_S
    passed.  AirPort rejoins its remembered network during the extension parade; the
    joins the user makes come from the Finder (Control Strip, AirPort application), after
    startup."""
    sup = app.supervisor
    if getattr(sup, "mac_os_up", getattr(sup, "finder_seen", True)):
        return False
    t_start = getattr(sup, "t_start", None)
    return t_start is not None and mono() - t_start <= STARTUP_QUIET_S


class PasswordScreen(Screen):
    doc_id = "airport-password"

    def __init__(self, panel, name: str, security: str, error: str | None, hidden: bool,
                 future: asyncio.Future):
        super().__init__(panel)
        self.name, self.security, self.error, self.hidden, self.future = name, security, error, hidden, future

    def doc(self) -> dict:
        if self.hidden:
            items = [ui.text(f"Join “{self.name}”", "emphasis"),
                     ui.text("RetroBoot did not find this network nearby. If it is a hidden network, "
                             "enter its password, or leave the password empty for an open network.")]
        else:
            items = [ui.text(f"The network “{self.name}” requires a password.", "emphasis")]
        items.append(ui.field("psk", "Password:", "", secure=True))
        if self.security:
            items.append(ui.text(f"Security: {self.security}. RetroBoot remembers the password "
                                 "for this network.", "small"))
        if self.error:
            items.append(ui.text(self.error, "small"))
        items.append(ui.buttons(ui.btn("cancel", "Cancel"), ui.btn("join", "Join", default=True)))
        return ui.window(self.doc_id, "Wi-Fi Password", items, width=420)

    async def action(self, id, values):
        if self.future.done():
            return
        if id == "cancel":
            self.future.set_result(None)
        elif id == "join":
            psk = values.get("psk") or ""
            if not self.hidden and not 8 <= len(psk) <= 64:
                self.error = "Wi-Fi passwords are 8 to 63 characters."
                await self.panel.refresh()
                return
            self.future.set_result(psk)


class JoiningScreen(Screen):
    doc_id = "airport-joining"

    def __init__(self, panel, name: str):
        super().__init__(panel)
        self.name = name

    def doc(self) -> dict:
        return ui.window(self.doc_id, "Wi-Fi", [ui.text(f"Joining “{self.name}”…", "emphasis"),
                                                ui.progress(None)], width=360)


class PanelPrompter:
    """``Prompter`` for AirPortService backed by hostd's classic panel."""

    def __init__(self, app: "Hostd"):
        self.app = app
        self.screen: Screen | None = None

    def available(self) -> bool:
        app, sup = self.app, self.app.supervisor
        return (app.mode == "normal" and app.presenter.connected.is_set() and sup.state == "running"
                and bool(sup.content_seen) and not sup.paused and self._startup_done()
                and (not app.panel.visible or app.panel.screen is self.screen))

    def _startup_done(self) -> bool:
        """No password window while Mac OS starts up (no window over the startup screen at
        every boot).  The bridge does not ask during startup either: AirPort's startup
        rejoin follows the host (service._startup_join)."""
        sup = self.app.supervisor
        if getattr(sup, "mac_os_up", getattr(sup, "finder_seen", True)):
            return True
        return getattr(sup, "t_start", None) is not None and not mac_os_starting(self.app)

    def _ours(self) -> bool:
        return self.screen is not None and self.app.panel.screen is self.screen and self.app.panel.visible

    async def ask(self, name: str, security: str, error: str | None, hidden: bool) -> str | None:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.screen = PasswordScreen(self.app.panel, name, security, error, hidden, fut)
        await self.app.panel.show(self.screen)
        waited = 0.0
        while not fut.done():
            await asyncio.sleep(0.25)
            waited += 0.25
            if not self._ours():
                fut.set_result(None)          # replaced by recovery or another flow
            elif waited >= PROMPT_TIMEOUT:
                fut.set_result(None)
                await self._close()
        result = fut.result()
        if result is None and self._ours():
            # Cancel: back to Mac OS (on hardware the window otherwise stayed on screen,
            # inert, over the dimmed guest after the click).
            await self._close()
        return result

    async def working(self, name: str) -> None:
        if self._ours():
            self.screen = JoiningScreen(self.app.panel, name)
            await self.app.panel.show(self.screen)

    async def finish(self, ok: bool, message: str | None) -> None:
        if not self._ours():
            self.screen = None
            return
        if ok:
            await self._close()
            return
        prompter = self

        async def back():
            prompter.screen = None
            await prompter.app.panel.close(to_guest=True)
        self.screen = None
        await self.app.panel.message("caution", "Could not join the network.", message, then=back)

    async def _close(self) -> None:
        self.screen = None
        await self.app.panel.close(to_guest=True)
