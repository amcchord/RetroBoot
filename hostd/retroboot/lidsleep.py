"""Closing the lid puts Mac OS to sleep (``lid_action`` "sleep"), as on a PowerBook.

The host cannot sleep (on the Gemini PDA s2idle hangs; the image masks the suspend
targets), but Mac OS can: on the PMU machine (battery.py) its own Sleep halts the
emulated processor, QEMU reports ``sleep`` on the power bridge and hostd turns the
displays off (and cpuplace.py gives the A72 policy back to the kernel until the wake);
``{"type": "wake"}`` on the bridge wakes it, and Mac OS re-reads its clock.  The PMU
never reports the lid to Mac OS (the host owns the lid), so hostd asks Mac OS to sleep
the way a user does, through the power-key path the Shut Down and Restart requests
use (supervisor, powerdlg.py).  Mac OS 9.2.2's power dialog (Restart, Sleep, Cancel,
Shut Down) takes S for Sleep and Escape for Cancel (lab: tests/e2e/lidsleep_lab.py).

* Lid closed: the internal panel is blanked at once (laptop.py, as before).  After
  ``lid_sleep_delay_s`` (a bounce is not a close), once Mac OS is up (the Finder,
  ``mac_os_up``), no hostd job holds the guest (install, checkpoint, update, a
  lifecycle request), no other power request runs and Mac OS is idle (``GuestLoad``,
  as for Shut Down), hostd presses the ADB power key once, verifies the dialog by
  its content (``find_power_dialog``: the same box in two frames) and answers S.  It
  is done when the bridge says ``sleep``.
* A power dialog already on screen (the power button's, left open: Mac OS spins in
  its modal loop, so it never looks idle) is answered with S directly: a second
  power key would close it.  Another modal dialog (the menus dimmed against the
  Finder's) or, with Mac OS busy, an alert-like box: nothing is sent
  (``lid_sleep_skipped`` "alert on screen").  A Mac OS busy for ``lid_busy_wait_s``
  with nothing on screen gets the key anyway, as the power button does (it draws the
  dialog when it can, so its dialog is watched for that much longer); the existing
  rules decide whether a dropped key may be pressed again, and S goes only to a
  verified dialog: nothing is typed into an application.
* Lid opened during the request: no S; the lid key's dialog is answered with Escape
  once verified (also one still coming within its window); an S already sent is
  followed by the wake below.  A dialog the user opened is left as it was.
* Lid opened while Mac OS sleeps (by the lid or its own Sleep): wake it through the
  bridge; its wake turns the displays on (``Battery._displays_on`` respects the lid).
* Nothing is done with an external display connected (it keeps showing Mac OS, as
  before), without the PMU machine, at a critical battery, or when Mac OS is already
  asleep.  One request per close: a Mac OS the user wakes with a key or the power
  button while the lid stays closed stays awake until the lid closes again.  Shut
  Down, Restart and the low-battery shutdown wake a sleeping Mac OS first
  (``Battery.ensure_awake`` in the supervisor's dialog path), as before.

``lid_action`` (hostd.json): "auto" (the board profile: "sleep" on the Gemini PDA,
"blank" elsewhere, so PCs keep today's behaviour), "sleep" or "blank".  Lifecycle
events: ``lid_sleep_request``, ``lid_sleep_done``, ``lid_sleep_skipped`` (reason),
``lid_wake``, ``lid_asleep_power``; the battery's ``current_now``/``voltage_now``
(``battery_ma``, ``battery_mv``) ride along for comparing the draw.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from . import powerdlg
from .util import mono

if TYPE_CHECKING:
    from .hostd import Hostd

log = logging.getLogger("retroboot.lidsleep")

ACTIONS = ("sleep", "blank")
# outcomes after which the clocks stay up (lidpower.py): the lid is open again, hostd
# stops, or Mac OS shows on an external display
NO_LOW_POWER = ("lid opened", "hostd stopping", "external display")


def _near(a, b, tol: int = 12) -> bool:
    return a is not None and b is not None and all(abs(int(p) - int(q)) <= tol for p, q in zip(a, b))


def _uncancel() -> None:
    """A cancellation (the lid opened) was handled: later timeouts in this task work as usual."""
    t = asyncio.current_task()
    if t is not None and hasattr(t, "uncancel"):
        t.uncancel()


class LidSleep:
    POLL_S = 0.5
    CONFIRM_DELAY_S = 0.3        # the dialog finished drawing before S (as for Return)
    ASLEEP_SAMPLE_S = 30.0       # lid_asleep_power: the draw once Mac OS has slept a while

    def __init__(self, app: "Hostd"):
        self.app = app
        self.closed = False
        self.episode = 0                  # lid closes so far; a request belongs to one
        self.task: asyncio.Task | None = None
        self.sample_task: asyncio.Task | None = None
        self.power_task: asyncio.Task | None = None
        self.phase = "idle"               # idle | waiting | key | dialog | sleeping
        self.pk: dict | None = None       # the power key this request sent
        self.existing: dict | None = None  # a power dialog already on screen (the user's key)
        self.last: dict | None = None     # the last outcome (status)
        self.stopping = False
        self._cancelled: asyncio.Task | None = None
        self._result = None

    # -- policy ------------------------------------------------------------------------------
    @property
    def action(self) -> str:
        a = str(getattr(self.app.cfg, "lid_action", "auto") or "auto").lower()
        if a == "auto":
            a = (self.app.platform.profile or {}).get("lid_action", "blank")
        return a if a in ACTIONS else "blank"

    def _blocker(self, ep: int) -> str | None:
        """Why this request must end now (None: it may go on)."""
        app, sup, bat = self.app, self.app.supervisor, self.app.battery
        if self.stopping:
            return "hostd stopping"
        if not self.closed or ep != self.episode:
            return "lid opened"
        if bat.asleep:
            return "asleep"
        if not sup.active:
            return "not running"
        if not bat.passthrough_active() or bat.writer is None:
            return "no pmu"               # Sleep needs the PMU machine and its bridge
        if self._external_display():
            return "external display"
        if getattr(app.laptop, "battery_state", "ok") in ("critical", "forced"):
            return "low battery"
        return None

    def _waiting_for(self, locked: bool = False) -> str | None:
        """What the request waits for before it may press the power key (None: ready)."""
        app, sup = self.app, self.app.supervisor
        if sup.state != "running" or sup.paused or sup.qmp is None:
            return "running"
        if not sup.mac_os_up:
            return "finder"
        if app.jobs.holder("guest") is not None or sup.intent:
            return "job"
        if sup.power_pending and not locked:
            return "power request"
        if app.panel.visible:
            return "panel"
        return None

    def _external_display(self) -> bool:
        outs = (self.app.presenter.display or {}).get("outputs") or []
        return any(not o.get("internal") for o in outs)

    # -- events --------------------------------------------------------------------------------
    def on_close(self) -> None:
        """laptop.py: the lid closed (the internal panel is already off)."""
        self.closed = True
        self.episode += 1
        if self.stopping:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self.action != "sleep":
            if self._lidpower() is not None and self._lidpower().enabled:
                self.power_task = loop.create_task(self._blank_low_power(self.episode), name="lid-low-power")
            return
        prev = self.task
        if prev is not None and not prev.done():
            # the previous close's request is still finishing (its cleanup after a quick open)
            self.task = loop.create_task(self._after(prev, self.episode), name="lid-sleep")
        else:
            self.task = loop.create_task(self._request(self.episode), name="lid-sleep")

    async def on_open(self) -> bool:
        """laptop.py: the lid opened.  True when a sleeping Mac OS was woken (its wake
        turns the displays on), False when the caller turns the panel on itself."""
        self.closed = False
        # the draw with the lid closed (asleep, slowed down), before anything wakes up
        reading = self.app.laptop.battery_reading() if self.app.battery.asleep else {}
        lp = self._lidpower()
        if lp is not None and lp.active:
            # the clocks and the keyboard poll first: Mac OS wakes (and the panel comes on) at speed
            try:
                await lp.leave("lid opened")
            except Exception:  # noqa: BLE001 -- never stop the wake
                log.exception("lid low power")
        if self.action != "sleep":
            return False
        if self.sample_task is not None and not self.sample_task.done():
            self.sample_task.cancel()
        t = self.task
        if t is not None and not t.done():
            if self._cancelled is not t:      # never interrupt a cleanup that already runs
                self._cancelled = t
                t.cancel()                    # its cleanup answers a dialog with Escape, or wakes Mac OS
            return False
        bat = self.app.battery
        if bat.asleep and self.app.supervisor.active:
            cp = getattr(self.app, "cpuplace", None)
            if cp is not None:
                try:
                    await cp.guest_sleep(False)   # the A72 plan before the wake, not after it
                except Exception:  # noqa: BLE001
                    log.exception("cpu placement")
            ok = await bat.wake("lid")
            self.app.lifecycle.event("lid_wake", woke=ok, session=self.app.supervisor.session, **reading)
            self.last = {"outcome": "lid_wake", "woke": ok, **reading}
            return ok
        return False

    async def stop(self) -> None:
        self.stopping = True
        for t in (self.sample_task, self.power_task, self.task):
            if t is not None and not t.done():
                t.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(t), 2)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    # -- the request -------------------------------------------------------------------------
    def _done(self, kind: str, episode: int | None = None, **kw) -> None:
        kw = {k: v for k, v in kw.items() if v is not None}
        self.last = {"outcome": kind, **kw}
        self.app.lifecycle.event(kind, session=self.app.supervisor.session, episode=episode, **kw)
        if episode is not None and kw.get("reason") not in NO_LOW_POWER:
            self._low_power_later(episode)

    # -- low power while the lid is closed (lidpower.py) -----------------------------------------
    def _lidpower(self):
        return getattr(self.app, "lidpower", None)

    def _low_power_later(self, ep: int) -> None:
        """The sleep request has its outcome: the clocks and the keyboard poll go down."""
        lp = self._lidpower()
        if lp is None or not lp.enabled or lp.active or self.stopping:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self.power_task = loop.create_task(self._low_power(ep), name="lid-low-power")

    async def _low_power(self, ep: int) -> None:
        lp = self._lidpower()
        try:
            await lp.enter("lid closed", still=lambda: self.closed and ep == self.episode and not self.stopping
                           and not self._external_display())
        except Exception:  # noqa: BLE001 -- best effort
            log.exception("lid low power")

    async def _blank_low_power(self, ep: int) -> None:
        """lid_action "blank": nothing to ask Mac OS; low power after lid_sleep_delay_s."""
        await asyncio.sleep(float(getattr(self.app.cfg, "lid_sleep_delay_s", 1.0)))
        await self._low_power(ep)

    async def on_display(self) -> None:
        """hostd: the presenter's display changed: an external display while the lid is
        closed shows Mac OS again, at speed."""
        lp = self._lidpower()
        if lp is not None and lp.active and self._external_display():
            try:
                await lp.leave("external display")
            except Exception:  # noqa: BLE001
                log.exception("lid low power")

    async def _finish(self, coro):
        """Run a short guest action (a QMP key) to its end even if the lid opens meanwhile,
        then let the cancellation through: whether the key went out is then known."""
        fut = asyncio.ensure_future(coro)
        try:
            return await asyncio.shield(fut)
        except asyncio.CancelledError:
            try:
                self._result = await fut
            except Exception:  # noqa: BLE001
                self._result = None
            raise

    async def _after(self, prev: asyncio.Task, ep: int) -> None:
        """A close while the previous close's request still finishes (its cleanup after a
        quick open): this one starts once that is over."""
        try:
            await asyncio.shield(prev)
        except asyncio.CancelledError:
            _uncancel()
            self._done("lid_sleep_skipped", reason="hostd stopping" if self.stopping else "lid opened",
                       episode=ep, phase="waiting")
            return
        except Exception:  # noqa: BLE001
            pass
        await self._request(ep)

    async def _dialog_up(self) -> dict | None:
        """The session's last power key whose dialog is on screen now (the same box) and
        was never answered (the power button's dialog left open), whatever its age."""
        sup = self.app.supervisor
        pk = sup.power_key
        if not pk or pk.get("confirmed") or pk.get("session") != sup.session or not pk.get("box"):
            return None
        res = await sup._dialog_now(pk)
        return pk if res and _near(res.get("box"), pk["box"]) else None

    async def _alert_up(self, busy: bool) -> bool:
        """Another modal dialog is on screen (the menus dimmed against the Finder's) or,
        with Mac OS busy, an alert-like box (an application's message, a progress box):
        a power key would wait behind it, and S could reach that box."""
        sup = self.app.supervisor
        cur = await sup.dump_frame()
        if cur is None:
            return False
        ms = await asyncio.to_thread(powerdlg.menu_state, sup.finder_frame, cur)
        if ms and ms.get("changed"):
            return True
        return busy and await asyncio.to_thread(powerdlg.alert_on_screen, cur)

    async def _request(self, ep: int) -> None:
        sup, cfg = self.app.supervisor, self.app.cfg
        self.pk = self.existing = self._result = None
        self.phase = "waiting"
        t0 = mono()
        busy = False
        try:
            await asyncio.sleep(float(getattr(cfg, "lid_sleep_delay_s", 1.0)))   # a bounce is not a close
            while True:
                why = self._blocker(ep)
                if why:
                    self.phase = "idle"
                    self._done("lid_sleep_skipped", reason=why, episode=ep)
                    return
                if self._waiting_for() is not None:
                    await asyncio.sleep(self.POLL_S)
                    continue
                busy = False
                if await self._dialog_up() is None:
                    ready = await sup._wait_idle(None, mono() + float(getattr(cfg, "lid_busy_wait_s", 30.0)))
                    if self._blocker(ep) or self._waiting_for() is not None:
                        continue
                    busy = not ready.get("idle")
                    if await self._dialog_up() is None and await self._alert_up(busy):
                        self.phase = "idle"
                        self._done("lid_sleep_skipped", reason="alert on screen", episode=ep, busy=busy)
                        return
                    # busy for lid_busy_wait_s with nothing on screen: press anyway, as
                    # the power button does (Mac OS holds the key and draws the dialog)
                await sup.power_lock.acquire()
                if self._blocker(ep) is None and self._waiting_for(locked=True) is None:
                    break                     # holding power_lock
                sup.power_lock.release()
        except asyncio.CancelledError:
            _uncancel()
            self.phase = "idle"
            self._done("lid_sleep_skipped", reason="hostd stopping" if self.stopping else "lid opened",
                       episode=ep, phase="waiting", after_close_s=round(mono() - t0, 1))
            return
        except Exception as exc:  # noqa: BLE001 -- never disturb Mac OS
            log.exception("lid sleep")
            self.phase = "idle"
            self._done("lid_sleep_skipped", reason=f"error: {type(exc).__name__}", episode=ep)
            return
        try:
            await self._with_lock(ep, t0, busy)
        except asyncio.CancelledError:
            _uncancel()
            await self._cleanup(ep)          # still holding power_lock: no request interleaves
        except Exception as exc:  # noqa: BLE001
            log.exception("lid sleep")
            self._done("lid_sleep_skipped", reason=f"error: {type(exc).__name__}", episode=ep, phase=self.phase)
        finally:
            sup.power_lock.release()
            self.phase = "idle"

    async def _with_lock(self, ep: int, t0: float, busy: bool) -> None:
        sup, cfg = self.app.supervisor, self.app.cfg
        session = sup.session
        pk = self.existing = await self._dialog_up()   # the power button's dialog, still open
        self.app.lifecycle.event("lid_sleep_request", session=session, episode=ep,
                                 after_close_s=round(mono() - t0, 1),
                                 dialog="already open" if pk is not None else "power key", busy=busy,
                                 **self.app.laptop.battery_reading())
        if pk is None:
            # a busy Mac OS draws the dialog once it can: watch for it that much longer
            window = cfg.power_dialog_wait_s + (float(getattr(cfg, "lid_busy_wait_s", 30.0)) if busy else 0.0)
            for n in range(1, cfg.power_key_attempts + 1):
                self.phase = "key"
                self._result = None
                pk = await self._finish(sup._send_power_key("lid", n))
                self.pk = pk
                self.phase = "dialog"
                out = await sup._watch_dialog(pk, mono() + window)
                if out == "dialog":
                    break
                why = self._blocker(ep)
                if why == "lid opened":
                    raise asyncio.CancelledError    # (on_open cancels too) the cleanup watches out the key
                if out != "dropped" or why:
                    # busy, an alert, something unrecognized, or gone: never press again
                    # (another key could close a dialog still coming), never type S
                    self._clear_key(pk)
                    self._done("lid_sleep_skipped", reason=why or f"no dialog ({out})", episode=ep)
                    return
                ready = await sup._wait_idle(None, mono() + cfg.power_ready_max_s)
                if not ready.get("idle"):
                    self._clear_key(pk)
                    self._done("lid_sleep_skipped", reason="no dialog (busy)", episode=ep)
                    return
            else:
                self._clear_key(pk)
                self._done("lid_sleep_skipped", reason="no dialog", episode=ep)
                return
        self.phase = "dialog"
        await asyncio.sleep(self.CONFIRM_DELAY_S)
        why = self._blocker(ep)
        if why == "lid opened":
            raise asyncio.CancelledError     # the cleanup answers the lid's dialog with Escape
        if why:
            # an external display came, Mac OS stopped, the battery: Cancel the lid's own
            # dialog (a dialog the user opened stays as it was)
            escaped = await self._escape(self.pk) if self.existing is None else False
            self._clear_key(self.pk)
            self._done("lid_sleep_skipped", reason=why, episode=ep, dialog_cancelled=escaped)
            return
        res = await sup._dialog_now(pk)
        if not res or (self.existing is not None and not _near(res.get("box"), pk.get("box"))):
            self._clear_key(self.pk)
            self._done("lid_sleep_skipped", reason="dialog gone", episode=ep)
            return
        self.phase = "sleeping"
        await self._finish(sup.qmp.send_key("s", hold_ms=100))
        pk.update(mono=mono(), confirmed=True)   # no second power key for this dialog
        self.app.lifecycle.event("power_dialog_confirmed", session=session, key="s", attempt=pk.get("n"),
                                 origin="lid", dialog_from=pk.get("origin"), buttons=res.get("buttons"))
        t_s = mono()
        if await self._wait_asleep(float(getattr(cfg, "lid_sleep_wait_s", 15.0))):
            self._clear_key(pk)
            self._done("lid_sleep_done", after_s=round(mono() - t_s, 1), episode=ep,
                       dialog="already open" if self.existing is not None else "power key",
                       **self.app.laptop.battery_reading())
            self._sample_later(ep)
            return
        # Mac OS did not sleep (no Sleep button?): Cancel the dialog if it is still up
        escaped = await self._escape(pk)
        self._clear_key(pk)
        self._done("lid_sleep_skipped", reason="no sleep after S", dialog_cancelled=escaped, episode=ep)

    def _sample_later(self, ep: int) -> None:
        """lid_asleep_power: the battery's draw once Mac OS has slept ASLEEP_SAMPLE_S."""
        async def sample():
            await asyncio.sleep(self.ASLEEP_SAMPLE_S)
            if self.closed and ep == self.episode and self.app.battery.asleep:
                self.app.lifecycle.event("lid_asleep_power", session=self.app.supervisor.session, episode=ep,
                                         after_s=self.ASLEEP_SAMPLE_S, **self.app.laptop.battery_reading())
        try:
            self.sample_task = asyncio.get_running_loop().create_task(sample(), name="lid-asleep-power")
        except RuntimeError:
            pass

    async def _wait_asleep(self, timeout: float) -> bool:
        end = mono() + timeout
        while mono() < end:
            if self.app.battery.asleep:
                return True
            if not self.app.supervisor.active:
                return False
            await asyncio.sleep(0.2)
        return self.app.battery.asleep

    def _clear_key(self, pk: dict | None) -> None:
        """The request is over: its key no longer counts as pending (a power-button press
        after the wake opens the dialog again)."""
        sup = self.app.supervisor
        if pk is not None and sup.power_key is pk:
            sup.power_key = None

    async def _escape(self, pk: dict | None) -> bool:
        """Escape (the dialog's Cancel) only to a power dialog verified on screen."""
        sup = self.app.supervisor
        if pk is None or sup.qmp is None or not sup._session_alive(pk.get("session")):
            return False
        if not await sup._dialog_now(pk):
            return False
        await sup.qmp.send_key("esc", hold_ms=100)
        self.app.lifecycle.event("power_dialog_cancelled", session=sup.session, origin="lid", key="esc")
        return True

    async def _cleanup(self, ep: int) -> None:
        """The lid opened (or hostd stops) during the request: leave no dialog of the lid's
        own key, no stray key and no sleeping Mac OS behind.  Runs inside power_lock."""
        sup, cfg = self.app.supervisor, self.app.cfg
        phase = self.phase
        own = self.pk or (self._result if isinstance(self._result, dict) else None)
        out: dict = {"phase": phase}
        try:
            if phase in ("key", "dialog") and own is not None:
                # the lid key's dialog: on screen now, or still coming within its window
                target = own
                if not own.get("box"):
                    left = cfg.power_dialog_wait_s - (mono() - own["mono"])
                    if self.stopping or left <= 0 or not sup._session_alive(own.get("session")) or \
                            await sup._watch_dialog(own, mono() + left) != "dialog":
                        target = None
                out["dialog_cancelled"] = await self._escape(target) if target is not None else False
                self._clear_key(own)
            elif phase == "sleeping" and not self.stopping:
                # S went out: Mac OS goes to sleep; the lid is open again, so wake it
                pk = self.existing or own
                if await self._wait_asleep(float(getattr(cfg, "lid_sleep_wait_s", 15.0))):
                    ok = await self.app.battery.wake("lid")
                    self.app.lifecycle.event("lid_wake", woke=ok, session=sup.session, after="sleep request",
                                             **self.app.laptop.battery_reading())
                    out["woke"] = ok
                else:
                    out["dialog_cancelled"] = await self._escape(pk)
                self._clear_key(pk)
            # phase "dialog" with the user's own dialog (existing): left open as it was; a
            # hostd stopping after S leaves Mac OS asleep (the next hostd wakes it at the lid)
        except Exception as exc:  # noqa: BLE001
            out["error"] = type(exc).__name__
        self._done("lid_sleep_skipped", reason="hostd stopping" if self.stopping else "lid opened",
                   episode=ep, **out)

    def public(self) -> dict:
        lp = self._lidpower()
        return {"action": self.action, "closed": self.closed, "phase": self.phase, "last": self.last,
                **({"low_power": lp.public()} if lp is not None else {})}
