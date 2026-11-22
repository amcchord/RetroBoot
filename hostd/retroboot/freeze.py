"""Frozen-guest check: notice a hung Mac OS without bothering the user.

QMP answering only proves that the emulator's main loop runs; a Mac OS that
has crashed or hung inside the guest keeps QEMU perfectly healthy.  The
signal used here is the guest framebuffer, as seen by the presenter (DAMAGE
messages on the display socket, ``status.guest.damage_seq`` /
``last_damage_ms``):

1. While Mac OS runs, its menu-bar clock changes at least once a minute, so a
   framebuffer that has not changed for ``frozen_stale_s`` (75 s) is suspect.
2. Only when nobody has used the keyboard or pointer for ``frozen_quiet_s``
   (30 s) does hostd ask the presenter for a 1-pixel pointer nudge
   (``pointer_nudge``: move by one pixel, restore 120 ms later).  Mac OS draws
   its cursor in software, so a live guest repaints it at once.
3. No repaint within ``frozen_nudge_wait_s`` -> ``responsive = false``: the
   web client shows "Needs attention" and the recovery panel says "Mac OS is
   not responding." with Force Restart.  Nothing is ever restarted
   automatically.  Any later frame change clears the flag; while flagged the
   nudge is repeated every ``frozen_stale_s`` so a guest that recovers with
   its clock hidden is noticed too.

A guest whose picture is (almost) entirely black (display sleep, a fade) is
not nudged: waking a sleeping display every minute would be worse than not
knowing.  The check is inactive without the presenter (development VNC
backend) and while hostd itself paused the guest (startup options).
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Awaitable, Callable

from .util import mono

if TYPE_CHECKING:
    from .supervisor import Supervisor

log = logging.getLogger("retroboot.freeze")


def mostly_black(frame: tuple[int, int, bytes] | None, threshold: float = 0.98) -> bool:
    """True when at least ``threshold`` of a coarse sample of an RGB frame is black."""
    if not frame:
        return False
    w, h, rgb = frame
    if w <= 0 or h <= 0:
        return False
    total = black = 0
    step_y = max(1, h // 48)
    step_x = max(1, w // 64)
    for y in range(0, h, step_y):
        row = y * w * 3
        for x in range(0, w, step_x):
            o = row + x * 3
            total += 1
            if rgb[o] <= 24 and rgb[o + 1] <= 24 and rgb[o + 2] <= 24:
                black += 1
    return total > 0 and black / total >= threshold


class FrozenGuestCheck:
    def __init__(self, sup: "Supervisor", status: Callable[[], Awaitable[dict | None]] | None = None,
                 nudge: Callable[[], Awaitable[dict | None]] | None = None,
                 frame: Callable[[], Awaitable[tuple | None]] | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.sup = sup
        self.cfg = sup.cfg
        presenter = sup.app.presenter
        self._status = status or (lambda: presenter.try_request("status"))
        self._nudge = nudge or (lambda: presenter.try_request("pointer_nudge"))
        self._frame = frame or sup.dump_frame
        self._sleep = sleep
        self.flag_seq: int | None = None      # damage_seq when the guest was flagged
        self.last_nudge: float | None = None  # mono time of the last nudge
        self.nudges = 0
        self.inactive_logged = False

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _guest(st: dict | None) -> dict | None:
        g = (st or {}).get("guest") or {}
        if not g.get("connected") or g.get("damage_seq") is None:
            return None
        return g

    def _observing(self, session: str) -> bool:
        sup = self.sup
        return (session == sup.session and sup.state == "running" and not sup.paused and sup.content_seen
                and not getattr(getattr(sup.app, "battery", None), "asleep", False))  # Mac OS asleep (battery.py)

    def _input_quiet(self, st: dict) -> bool:
        last = st.get("last_input_ms")
        return last is None or last >= self.cfg.frozen_quiet_s * 1000

    # -- main loop -------------------------------------------------------------
    async def run(self, session: str) -> None:
        sup = self.sup
        while session == sup.session and sup.active:
            await self._sleep(self.cfg.frozen_poll_s)
            if session != sup.session or not sup.active:
                return
            if not self._observing(session):
                continue
            try:
                await self.step(session)
            except asyncio.CancelledError:
                raise
            except Exception:  # never let the check take the supervisor down
                log.exception("frozen-guest check failed")

    async def step(self, session: str) -> None:
        sup = self.sup
        st = await self._status()
        g = self._guest(st)
        if g is None:
            if not self.inactive_logged and st is not None:
                self.inactive_logged = True
                sup.log.event("frozen_check_inactive", session=session, why="no guest frames from the presenter")
            return
        seq = int(g.get("damage_seq") or 0)
        age_ms = g.get("last_damage_ms")
        if sup.display_ok is False:
            if self.flag_seq is not None and seq != self.flag_seq:
                sup.set_display_responsive(True, session, damage_seq=seq)
                self.flag_seq = None
                return
            # Still flagged: re-nudge now and then (a recovered guest with its
            # clock hidden only redraws when something moves).
            if self._input_quiet(st) and (self.last_nudge is None or
                                          mono() - self.last_nudge >= self.cfg.frozen_stale_s):
                if await self._try_nudge(session, seq) is True:
                    sup.set_display_responsive(True, session, damage_seq=seq, via="nudge")
                    self.flag_seq = None
            return
        if age_ms is None:
            return  # no frame since the connection: content detection covers startup
        if age_ms < self.cfg.frozen_stale_s * 1000:
            return
        if not self._input_quiet(st):
            return
        if self.last_nudge is not None and mono() - self.last_nudge < self.cfg.frozen_stale_s:
            return  # a static but healthy guest: nudge at most once per stale period
        frame = await self._frame()
        if mostly_black(frame):
            if self.last_nudge is None or mono() - self.last_nudge >= self.cfg.frozen_stale_s:
                self.last_nudge = mono()  # rate-limit this branch like a nudge
                sup.log.event("frozen_check_skipped", session=session, why="guest display is black (sleep)",
                              frame_age_s=round(age_ms / 1000, 1))
            return
        result = await self._try_nudge(session, seq, frame_age_ms=age_ms)
        if result is False and session == sup.session and sup.state == "running":
            self.flag_seq = seq
            sup.set_display_responsive(False, session, damage_seq=seq, frame_age_s=round(age_ms / 1000, 1))

    async def _try_nudge(self, session: str, seq: int, frame_age_ms: float | None = None) -> bool | None:
        """Nudge the pointer; True if the guest redrew, False if not, None if unknown."""
        sup = self.sup
        self.last_nudge = mono()
        self.nudges += 1
        r = await self._nudge()
        if r is None:
            sup.log.event("guest_nudge", session=session, result="not-delivered")
            return None
        seq0 = int(r.get("damage_seq", seq))
        await self._sleep(self.cfg.frozen_nudge_wait_s)
        st = await self._status()
        g = self._guest(st)
        if g is None:
            return None
        redrawn = int(g.get("damage_seq") or 0) != seq0
        sup.log.event("guest_nudge", session=session, result="redrawn" if redrawn else "no-redraw",
                      pointer=r.get("pointer"), damage_seq=seq0,
                      frame_age_s=round(frame_age_ms / 1000, 1) if frame_age_ms is not None else None)
        return redrawn
