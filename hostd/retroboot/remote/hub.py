"""Remote screen hub: the web Screen page's viewers, frames and input.

One presenter tap connection (``tap.py``) serves every viewer.  It is open only
while someone watches.  The loop asks the presenter for an update (it answers
as soon as pixels change, or after 250 ms), adds the changed rectangles to each
viewer's dirty region and sends each viewer that is ready one frame with the
tiles of its region.  A viewer is ready when it has acknowledged all but one
of its frames, its previous send finished and its socket is not backed up, so
a slow browser simply gets fewer, larger updates (frames are dropped, never
queued).  Encoding runs in one worker thread and is paced: at most
``remote_max_fps`` frames per second, and no more often than the measured
encoding time allows within ``remote_cpu_budget`` of one core.

Input goes to the presenter, which routes it like the local keyboard and
pointer.  Keys and buttons are tracked per viewer; a viewer's keys are
released when it asks (window blur), disconnects or loses control, and the
presenter releases everything when the tap closes.  Pasted text is typed by
hostd at a fixed rate (``paste.py``), never faster than Mac OS reads keys.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from . import encode, keymap, paste as typer
from .tap import Surface, TapClient, TapClosed

if TYPE_CHECKING:
    from ..hostd import Hostd

log = logging.getLogger("retroboot.remote")

MODES = ("off", "view", "control")
PRIVATE_DOCS = {"network-web", "installer-network"}   # panel screens that show a pairing code
PRIVATE_HOLD_S = 0.8
WINDOW = 2              # frames in flight per viewer
SEND_BUFFER = 1 << 20   # bytes queued in a viewer's socket before it counts as slow
MAX_VIEWERS = 6
MAX_PER_DEVICE = 3
IDLE_WAIT_MS = 250
LINGER_S = 5.0          # keep the presenter tap this long after the last viewer leaves (page reloads)
MOVE_DEFER_S = 0.02     # a rate-limited pointer move is sent this much later (only the newest)
DROP_LIMIT = 3000.0     # decaying count of rate-limited messages (half-life 10 s) that ends a stream


def mono() -> float:
    return time.monotonic()


class TokenBucket:
    def __init__(self, rate: float, burst: float):
        self.rate, self.burst, self.tokens, self.t = rate, burst, burst, mono()

    def take(self, n: float = 1.0) -> bool:
        now = mono()
        self.tokens = min(self.burst, self.tokens + (now - self.t) * self.rate)
        self.t = now
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False


class Viewer:
    def __init__(self, ws, transport, device: dict, hello: dict, swap_setting: bool, mode: str):
        self.id = secrets.token_hex(4)
        self.ws = ws
        self.transport = transport
        self.device = device
        self.platform = str(hello.get("platform") or "other")[:16]
        self.swap_override = hello.get("swap") if isinstance(hello.get("swap"), bool) else None
        self.swap = keymap.swap_for(self.platform, swap_setting, self.swap_override)
        self.held: dict[str, int] = {}     # KeyboardEvent.code -> evdev code it was pressed as
        encs = hello.get("enc") if isinstance(hello.get("enc"), list) else ["png"]
        self.enc = encode.ENC_ZLIB if "zlib" in encs else encode.ENC_PNG
        self.mode = mode
        self.dirty = encode.Region()
        self.need_surface = True
        self.inflight = 0
        self.seq = 0
        self.keys: set[int] = set()
        self.buttons: set[int] = set()
        self.bucket = TokenBucket(250.0, 500.0)
        self.dropped = 0
        self.drop_score = 0.0          # decays: a burst is forgiven, sustained flooding is not
        self.drop_t = mono()
        self.pending_move: dict | None = None
        self.move_timer: asyncio.TimerHandle | None = None
        self._tail: asyncio.Task | None = None
        self.typing: asyncio.Task | None = None
        self.type_queue: collections.deque = collections.deque()
        self.type_total = 0
        self.type_skipped = 0
        self.since = time.time()
        self.started = mono()
        self.frames = 0
        self.bytes = 0
        self.inputs = 0
        self.closed = False
        self.ending = False            # told why and being closed: no more input
        self.private_sent: bool | None = None

    @property
    def holder(self) -> str:
        return self.id

    def ready(self) -> bool:
        if self.closed or self.inflight >= WINDOW or (self._tail is not None and not self._tail.done()):
            return False
        try:
            if self.transport is not None and self.transport.get_write_buffer_size() > SEND_BUFFER:
                return False
        except Exception:
            pass
        return True

    def public(self, me: "Viewer | None" = None) -> dict:
        return {"id": self.id, "device": self.device.get("id"), "name": self.device.get("name"),
                "mode": self.mode, "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.since)),
                "you": me is self, "platform": self.platform}

    def post(self, data: dict | bytes) -> asyncio.Task:
        """Queue a message for this browser.  Messages go out one at a time and in order
        (a surface message before the frames of that surface); a slow browser delays only
        its own queue, never the frame loop."""
        prev = self._tail

        async def run():
            if prev is not None and not prev.done():
                try:
                    await prev
                except BaseException:   # noqa: BLE001 - an earlier send's failure is handled there
                    pass
            if self.closed:
                return
            try:
                if isinstance(data, (bytes, bytearray)):
                    await self.ws.send_bytes(data)
                else:
                    await self.ws.send_json(data)
            except (ConnectionError, RuntimeError):
                self.closed = True
        self._tail = asyncio.get_running_loop().create_task(run())
        return self._tail

    async def send_json(self, obj: dict) -> None:
        await self.post(obj)

    def note_drop(self) -> float:
        """Count a rate-limited message; the score halves every 10 s."""
        now = mono()
        self.drop_score = self.drop_score * 0.5 ** ((now - self.drop_t) / 10.0) + 1.0
        self.drop_t = now
        self.dropped += 1
        return self.drop_score


class RemoteScreen:
    def __init__(self, app: "Hostd"):
        self.app = app
        cfg = app.cfg
        self.max_fps = float(getattr(cfg, "remote_max_fps", 30.0) or 30.0)
        self.cpu_budget = float(getattr(cfg, "remote_cpu_budget", 0.35) or 0.35)
        # Typed text goes through QEMU's ADB keyboard: a 128-event queue Mac OS drains at
        # about 23 events a second (measured in SimpleText).  Pace by events so the queue
        # never overflows (an overflow drops events: a lost Option release, a missing letter).
        self.key_rate = float(getattr(cfg, "remote_key_events_per_s", 20.0) or 20.0)
        self.key_burst = float(getattr(cfg, "remote_key_burst", 96.0) or 96.0)
        self._key_tokens = self.key_burst
        self._key_t = mono()
        self.trace_keys = bool(getattr(cfg, "remote_trace_keys", False))   # lab: log every key to the presenter
        self.viewers: dict[str, Viewer] = {}
        self.tap = TapClient(str(app.paths.remote_sock), self._on_surface, self._on_tap_close)
        self.surface: Surface | None = None
        self.source = "composed"
        self.version = 0
        self._task: asyncio.Task | None = None
        self.key_holders: dict[int, set[str]] = {}
        self.button_holders: dict[int, set[str]] = {}
        self.pool = ThreadPoolExecutor(1, thread_name_prefix="rb-screen-encode")
        self.encode_ewma = 0.0
        self._last_frame = 0.0
        self._sent_times: list[float] = []
        self._typing_lock = asyncio.Lock()
        self._private_seen = -1e9
        self._joined = asyncio.Event()
        self._closing = False
        self.stats = {"frames": 0, "tiles": 0, "bytes": 0, "encode_ms_total": 0.0, "pixels": 0,
                      "dropped_input": 0, "sessions": 0}

    # -- settings ------------------------------------------------------------------
    def setting(self) -> str:
        v = self.app.settings.applied.get("remote.screen", "control")
        return v if v in MODES else "control"

    def swap_setting(self) -> bool:
        return bool(self.app.settings.applied.get("input.swap_command_option", False))

    def _end(self, v: Viewer, code: str, message: str, close_code: int = 4403) -> None:
        """Tell a browser why and close its stream, without waiting for it (a slow browser
        must not hold up the frame loop or a settings change)."""
        if v.closed or v.ending:
            return
        v.ending = True
        v.post({"t": "error", "code": code, "message": message})

        async def close():
            try:
                await asyncio.wait_for(v.ws.close(code=close_code, message=code.encode()), 5)
            except (asyncio.TimeoutError, ConnectionError, RuntimeError):
                pass
        asyncio.get_running_loop().create_task(close())

    async def disconnect_all(self, code: str, message: str) -> None:
        """End every stream (the setting was turned off, or web management was)."""
        for v in list(self.viewers.values()):
            self._end(v, code, message)

    async def apply_setting(self) -> None:
        """remote.screen changed: off closes every viewer, view takes control away."""
        mode = self.setting()
        if mode == "off":
            await self.disconnect_all("remote-screen-off", "The remote screen was turned off in Settings.")
            return
        for v in self.viewers.values():
            new = "control" if mode == "control" else "view"
            if v.mode != new:
                if new == "view":
                    self._release_viewer(v)
                v.mode = new
                v.post({"t": "mode", "mode": new})
        self._push_viewers()
        await self._broadcast_viewers()

    def set_source(self, src: str) -> bool:
        """The picture every browser gets: what the display shows, or Mac OS alone."""
        if src not in ("composed", "guest") or src == self.source:
            return False
        self.source = src
        if self.tap.connected:
            self.tap.source(src)
        return True

    # -- viewers -------------------------------------------------------------------
    def check_admission(self, device: dict) -> tuple[str, str] | None:
        if len(self.viewers) >= MAX_VIEWERS:
            return "too-many-viewers", "Too many browsers are showing the screen. Close one and try again."
        if sum(1 for v in self.viewers.values() if v.device.get("id") == device.get("id")) >= MAX_PER_DEVICE:
            return "too-many-viewers", "This browser already shows the screen in several windows."
        return None

    async def attach(self, v: Viewer) -> None:
        self.viewers[v.id] = v
        self.stats["sessions"] += 1
        self.app.lifecycle.event("remote_screen_open", viewer=v.id, device=v.device.get("id"),
                                 name=v.device.get("name"), mode=v.mode, platform=v.platform)
        hostname = self.app.settings.applied.get("management.hostname")
        m = self.app.supervisor.machine or self.app.supervisor.active_machine()
        await v.send_json({"t": "hello", "v": 1, "viewer": v.id, "mode": v.mode, "swap": v.swap,
                           "chord": self.app.settings.applied.get("input.recovery_chord", "ctrl+alt+delete"),
                           "combos": keymap.combo_list(), "max_fps": self.max_fps, "source": self.source,
                           "machine": m.name if m else None, "hostname": hostname,
                           "typing": {"chars_per_second": round(self.key_rate / 2, 1),
                                      "burst_chars": int(self.key_burst // 2), "max_chars": typer.MAX_TEXT},
                           "enc": "zlib" if v.enc == encode.ENC_ZLIB else "png"})
        if self.surface is not None:
            self._mark_full(v)
        self._push_viewers()
        await self._broadcast_viewers()
        self._joined.set()
        if self._closing:
            return
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run(), name="remote-screen")
        elif self.tap.waiting:
            self.tap.nudge()

    async def detach(self, v: Viewer, why: str = "closed") -> None:
        if self.viewers.pop(v.id, None) is None:
            return
        v.closed = True
        if v.typing and not v.typing.done():
            v.typing.cancel()
        if v.move_timer is not None:
            v.move_timer.cancel()
            v.move_timer = None
        self._release_viewer(v)
        self.app.lifecycle.event("remote_screen_close", viewer=v.id, device=v.device.get("id"), why=why,
                                 seconds=round(mono() - v.started, 1), frames=v.frames, bytes=v.bytes,
                                 inputs=v.inputs, dropped_input=v.dropped)
        self._push_viewers()
        await self._broadcast_viewers()

    def _push_viewers(self) -> None:
        if self.tap.connected:
            n = len(self.viewers)
            self.tap.viewers(n, sum(1 for v in self.viewers.values() if v.mode == "control"))

    async def _broadcast_viewers(self) -> None:
        for v in list(self.viewers.values()):
            v.post({"t": "viewers", "list": [x.public(v) for x in self.viewers.values()]})

    def public(self) -> dict:
        now = mono()
        self._sent_times = [t for t in self._sent_times if now - t < 2.0]
        frames = self.stats["frames"]
        return {"setting": self.setting(), "source": self.source,
                "viewers": [v.public() for v in self.viewers.values()],
                "streaming": self.tap.connected,
                "surface": ({"gen": self.surface.gen, "view": self.surface.view, "kind": self.surface.kind,
                             "size": [self.surface.w, self.surface.h]} if self.surface else None),
                "stats": dict(self.stats, fps=round(len(self._sent_times) / 2.0, 1),
                              encode_ms_avg=round(self.stats["encode_ms_total"] / frames, 2) if frames else 0.0,
                              encode_ms_ewma=round(self.encode_ewma * 1000, 2), tap=dict(self.tap.stats))}

    # -- tap events ------------------------------------------------------------------
    def _mark_full(self, v: Viewer) -> None:
        v.need_surface = True
        v.dirty.clear()
        s = self.surface
        if s is not None and s.picture:
            v.dirty.add((0, 0, s.w, s.h))

    def _on_surface(self, s: Surface) -> None:
        self.surface = s
        for v in self.viewers.values():
            self._mark_full(v)

    def _on_tap_close(self) -> None:
        # The presenter released every remote key and button.
        self.key_holders.clear()
        self.button_holders.clear()
        for v in self.viewers.values():
            v.keys.clear()
            v.buttons.clear()

    def _private_doc(self) -> bool:
        panel = getattr(self.app, "panel", None)
        return (panel is not None and getattr(panel, "visible", False)
                and getattr(getattr(panel, "screen", None), "doc_id", None) in PRIVATE_DOCS)

    def _private(self) -> bool:
        """The classic panel shows a pairing code: no frames and no input, whatever picture
        is streamed (decided from hostd's own panel state).  hostd records a new panel screen
        before the presenter draws it, but forgets one before the presenter has drawn what
        replaces it, so the code stays private for PRIVATE_HOLD_S after."""
        now = mono()
        raw = self._private_doc()
        if raw:
            self._private_seen = now
            return True
        return now - self._private_seen < PRIVATE_HOLD_S

    # -- main loop -------------------------------------------------------------------
    async def _run(self) -> None:
        backoff = 0.25
        announced = False
        try:
            while True:
                if not self.viewers:
                    # Linger: a reloading page comes back within a moment.
                    self._joined.clear()
                    try:
                        await asyncio.wait_for(self._joined.wait(), LINGER_S)
                    except asyncio.TimeoutError:
                        break
                    continue
                if not self.tap.connected:
                    try:
                        await self.tap.connect(self.source)
                    except (OSError, asyncio.TimeoutError, TapClosed) as exc:
                        if not announced:
                            announced = True
                            log.info("remote screen: presenter not reachable (%s)", exc)
                            for v in list(self.viewers.values()):
                                v.post({"t": "state", "display": "unavailable"})
                        await asyncio.sleep(backoff)
                        backoff = min(2.0, backoff * 2)
                        continue
                    backoff = 0.25
                    if announced:
                        announced = False
                        for v in list(self.viewers.values()):
                            v.post({"t": "state", "display": "ok"})
                    self._push_viewers()
                wait = 0 if self._unserved() else IDLE_WAIT_MS
                try:
                    rects = await self.tap.update(wait)
                except TapClosed:
                    continue
                s = self.surface
                if rects and s is not None and s.picture:
                    self.version += 1
                    for r in rects:
                        c = encode.clip(r, s.w, s.h)
                        if c:
                            for v in self.viewers.values():
                                v.dirty.add(c)
                started = mono()
                await self._serve()
                self._check_devices()
                await self._pace(started)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("remote screen loop failed")
        finally:
            self.tap.close("no viewers" if not self.viewers else "loop ended")
            self.surface = None
            self._task = None
            if self.viewers and not self._closing:   # restart after an unexpected failure
                await asyncio.sleep(1.0)
                if self.viewers and self._task is None and not self._closing:
                    self._task = asyncio.get_running_loop().create_task(self._run(), name="remote-screen")

    def _check_devices(self) -> None:
        """A browser whose pairing was removed loses the screen within a few seconds."""
        now = mono()
        if now - getattr(self, "_dev_check", 0.0) < 3.0:
            return
        self._dev_check = now
        ids = {d["id"] for d in self.app.pairing.devices()}
        for v in list(self.viewers.values()):
            if v.device.get("id") not in ids:
                self._end(v, "not-paired", "This browser is no longer paired with RetroBoot.", 4401)

    def _unserved(self) -> bool:
        if self._private():
            return False
        return any(v.dirty and v.ready() and not v.need_surface for v in self.viewers.values())

    async def _pace(self, started: float) -> None:
        interval = max(1.0 / self.max_fps, self.encode_ewma / self.cpu_budget if self.cpu_budget > 0 else 0)
        delay = interval - (mono() - self._last_frame)
        if delay > 0:
            await asyncio.sleep(delay)

    async def _serve(self) -> None:
        s = self.surface
        private = self._private()
        for v in list(self.viewers.values()):
            if v.need_surface and not v.closed:
                v.need_surface = False
                v.post({"t": "surface", "gen": s.gen if s else 0, "w": s.w if s else 0,
                        "h": s.h if s else 0, "view": s.view if s else "none", "kind": s.kind if s else "none"})
            if v.private_sent is not private:
                v.private_sent = private
                v.post({"t": "state", "private": private})
                if not private:
                    self._mark_full(v)
                    v.need_surface = False
        if s is None or not s.picture or private:
            return
        groups: dict[tuple, list[Viewer]] = {}
        for v in self.viewers.values():
            if v.dirty and v.ready() and not v.need_surface:
                groups.setdefault((tuple(sorted(v.dirty.rects)), v.enc), []).append(v)
        if not groups:
            return
        loop = asyncio.get_running_loop()
        for (rects, enc), vs in groups.items():
            for v in vs:
                v.dirty.clear()
            t0 = time.perf_counter()
            try:
                tiles = await loop.run_in_executor(self.pool, encode.encode_tiles, s.buf, s.stride, list(rects), enc)
            except (ValueError, BufferError, OSError) as exc:
                log.warning("remote screen: encoding failed: %s", exc)
                continue
            dt = time.perf_counter() - t0
            self.encode_ewma = dt if not self.encode_ewma else self.encode_ewma * 0.8 + dt * 0.2
            if s is not self.surface:
                return    # a new surface arrived meanwhile; viewers were marked for a full refresh
            self.stats["encode_ms_total"] += dt * 1000
            self.stats["tiles"] += len(tiles)
            self.stats["pixels"] += sum(r[2] * r[3] for r in rects)
            now = mono()
            self._last_frame = now
            self._sent_times.append(now)
            for v in vs:
                if v.closed:
                    continue
                v.seq += 1
                data = encode.pack_frame(v.seq, s.gen, tiles)
                v.inflight += 1
                v.frames += 1
                v.bytes += len(data)
                self.stats["frames"] += 1
                self.stats["bytes"] += len(data)
                v.post(data)

    # -- input -----------------------------------------------------------------------
    def _press(self, holder: str, code: int) -> None:
        hs = self.key_holders.setdefault(code, set())
        if holder in hs:
            return
        hs.add(holder)
        if len(hs) == 1:
            self.tap.key(code, True)
            if self.trace_keys:
                log.info("remote key %d down (%s)", code, holder)

    def _unpress(self, holder: str, code: int) -> None:
        hs = self.key_holders.get(code)
        if not hs or holder not in hs:
            return
        hs.discard(holder)
        if not hs:
            del self.key_holders[code]
            self.tap.key(code, False)
            if self.trace_keys:
                log.info("remote key %d up (%s)", code, holder)

    def _button(self, holder: str, b: int, down: bool) -> None:
        hs = self.button_holders.setdefault(b, set())
        if down:
            if holder in hs:
                return
            hs.add(holder)
            if len(hs) == 1:
                self.tap.button(b, True)
        elif holder in hs:
            hs.discard(holder)
            if not hs:
                self.tap.button(b, False)

    def _release_viewer(self, v: Viewer) -> None:
        for code in list(v.keys):
            self._unpress(v.holder, code)
        for code in list(self.key_holders):
            for h in (f"{v.id}:type", f"{v.id}:combo"):
                self._unpress(h, code)
        for b in list(v.buttons):
            self._button(v.holder, b, False)
        v.keys.clear()
        v.held.clear()
        v.buttons.clear()

    def _defer_move(self, v: Viewer, msg: dict) -> None:
        v.pending_move = msg
        if v.move_timer is None:
            v.move_timer = asyncio.get_running_loop().call_later(MOVE_DEFER_S, self._flush_move, v)

    def _flush_move(self, v: Viewer) -> None:
        v.move_timer = None
        msg, v.pending_move = v.pending_move, None
        if msg is not None and not v.closed and v.mode == "control" and not self._private():
            self._pointer(v, msg)

    def _pointer(self, v: Viewer, msg: dict) -> bool:
        x, y, gen = msg.get("x"), msg.get("y"), msg.get("gen")
        if not all(isinstance(n, int) and not isinstance(n, bool) for n in (x, y, gen)):
            return False
        if not (0 <= x < 8192 and 0 <= y < 8192):
            return False
        self.tap.pointer(gen, x, y)
        return True

    async def on_message(self, v: Viewer, msg: Any) -> None:
        if not isinstance(msg, dict) or v.ending:
            return
        t = msg.get("t")
        if t == "ack":
            if v.inflight > 0:
                v.inflight -= 1
            if v.dirty and self.tap.waiting and v.ready():
                self.tap.nudge()
            return
        if t == "ping":
            await v.send_json({"t": "pong", "t0": msg.get("t0")})
            return
        if t == "source":
            # One picture for every browser: only a browser that may control changes it.
            src = msg.get("source")
            if v.mode == "control" and self.setting() == "control" and self.set_source(src):
                for o in self.viewers.values():
                    o.post({"t": "source", "source": src, "by": v.device.get("name")})
            else:
                v.post({"t": "source", "source": self.source})
            return
        if t == "release":
            self._release_viewer(v)
            return
        if t == "options":
            swap = msg.get("swap")
            v.swap_override = swap if isinstance(swap, bool) else None
            v.swap = keymap.swap_for(v.platform, self.swap_setting(), v.swap_override)
            await v.send_json({"t": "options", "swap": v.swap})
            return
        if t == "panel-back":
            await self._panel_back(v)
            return
        if t not in ("move", "button", "wheel", "key", "caps", "combo", "type", "cancel"):
            return
        if v.mode != "control" or self.setting() != "control":
            return
        if t == "cancel":
            if v.typing and not v.typing.done():
                v.typing.cancel()
            return
        # Releases are never rate limited or blocked: a lost release leaves a key or button
        # held in Mac OS.  Caps Lock state is not rate limited either (it changes only when
        # the browser's lock does); turning it on waits like any key while the panel is private.
        release = (t in ("key", "button") and msg.get("down") is False) or (t == "caps" and msg.get("on") is False)
        if self._private() and not release:
            return
        if t == "move":
            if not v.bucket.take(1.0):
                self._defer_move(v, msg)      # the newest position goes out a moment later
                return
        elif not release and t != "caps" and not v.bucket.take(10.0 if t == "combo" else 1.0):
            score = v.note_drop()
            self.stats["dropped_input"] += 1
            if v.dropped == 1 or v.dropped % 500 == 0:
                log.warning("remote screen: input from %s rate limited (%d dropped)", v.device.get("name"), v.dropped)
            if score > DROP_LIMIT:
                self._end(v, "too-much-input", "This browser sent too much input, so RetroBoot closed the screen.",
                          4429)
            return
        v.inputs += 1
        if t == "move":
            self._pointer(v, msg)
        elif t == "button":
            b, down = msg.get("b"), msg.get("down")
            if b not in (0, 1, 2) or not isinstance(down, bool):
                return
            if "x" in msg:
                v.pending_move = None
                self._pointer(v, msg)
            if down:
                v.buttons.add(b)
            else:
                v.buttons.discard(b)
            self._button(v.holder, b, down)
        elif t == "wheel":
            dy = msg.get("dy")
            if not isinstance(dy, int) or isinstance(dy, bool) or dy == 0:
                return
            if "x" in msg:
                self._pointer(v, msg)
            b = 3 if dy < 0 else 4
            for _ in range(min(abs(dy), 10)):
                self.tap.button(b, True)
                self.tap.button(b, False)
        elif t == "caps":
            if isinstance(msg.get("on"), bool):
                self._caps(v, msg["on"])
        elif t == "key":
            name, down = msg.get("code"), msg.get("down")
            if not isinstance(name, str) or not isinstance(down, bool):
                return
            if isinstance(msg.get("caps"), bool):
                self._caps(v, msg["caps"])
            if name == "CapsLock":
                return    # the lock state travels as "caps"
            if down:
                # The Command/Option choice follows the setting at the moment of the press;
                # the release always goes to the key that was pressed.
                v.swap = keymap.swap_for(v.platform, self.swap_setting(), v.swap_override)
                code = keymap.to_evdev(name, v.swap)
                if code is None or name in v.held:
                    return
                v.held[name] = code
                v.keys.add(code)
                self._press(v.holder, code)
            else:
                code = v.held.pop(name, None)
                if code is None:
                    return
                if code not in v.held.values():
                    v.keys.discard(code)
                    self._unpress(v.holder, code)
        elif t == "combo":
            keys = keymap.combo_keys(str(msg.get("name")),
                                     self.app.settings.applied.get("input.recovery_chord", "ctrl+alt+delete"))
            if keys:
                if msg.get("name") == "power" and not self.app.supervisor.active:
                    await v.send_json({"t": "notice", "message": "Mac OS isn't running, so it has no Shut Down."})
                    return
                asyncio.get_running_loop().create_task(self._combo(v, keys))
        elif t == "type":
            await self._start_typing(v, msg.get("text"))

    def _caps(self, v: Viewer, on: bool) -> None:
        """Mirror the browser's Caps Lock.  Mac OS reads ADB Caps Lock as a locking key
        (down while locked), so it is held down while the browser's lock is on.  The
        press is repeated with every key (the presenter ignores a repeat), which re-locks
        Mac OS after anything released all keys, such as a scene change."""
        code = keymap.EVDEV["CAPSLOCK"]
        if on:
            v.keys.add(code)
            self.key_holders.setdefault(code, set()).add(v.holder)
            self.tap.key(code, True)
        elif code in v.keys:
            v.keys.discard(code)
            self._unpress(v.holder, code)

    async def _combo(self, v: Viewer, keys: list[int]) -> None:
        holder = f"{v.id}:combo"
        try:
            for code in keys:
                await self._key_slot()
                self._press(holder, code)
                await asyncio.sleep(0.03)
            await asyncio.sleep(0.08)
        finally:
            for code in reversed(keys):
                self._key_tokens -= 1.0          # never wait to release
                self._unpress(holder, code)

    async def _start_typing(self, v: Viewer, text: Any) -> None:
        """Queue text to type; a browser's touch keyboard sends one character at a time."""
        if not isinstance(text, str) or not text:
            return
        strokes, skipped = typer.plan(text)
        if len(v.type_queue) + len(strokes) > typer.MAX_TEXT * 2:
            await v.send_json({"t": "typing", "state": "error",
                               "message": f"RetroBoot types up to {typer.MAX_TEXT} characters at a time."})
            return
        v.type_queue.extend(strokes)
        v.type_total += len(strokes)
        v.type_skipped += skipped
        if v.typing is None or v.typing.done():
            v.typing = asyncio.get_running_loop().create_task(self._type(v))

    async def _key_slot(self) -> None:
        """Wait for room in Mac OS's keyboard queue (token bucket: burst, then key_rate/s)."""
        while True:
            now = mono()
            self._key_tokens = min(self.key_burst, self._key_tokens + (now - self._key_t) * self.key_rate)
            self._key_t = now
            if self._key_tokens >= 1.0:
                self._key_tokens -= 1.0
                return
            await asyncio.sleep((1.0 - self._key_tokens) / self.key_rate)

    async def _type(self, v: Viewer) -> None:
        holder = f"{v.id}:type"
        shift, option = keymap.EVDEV["LEFTSHIFT"], keymap.EVDEV["LEFTALT"]
        done = 0
        state = "done"

        async def report(st: str) -> None:
            await v.send_json({"t": "typing", "state": st, "done": done, "total": v.type_total,
                               "skipped": v.type_skipped})

        async def event(code: int, down: bool) -> None:
            await self._key_slot()
            (self._press if down else self._unpress)(holder, code)
        await report("running")
        try:
            async with self._typing_lock:
                last_report = mono()
                while v.type_queue:
                    code, sh, opt = v.type_queue.popleft()
                    if v.closed or v.mode != "control":
                        state = "cancelled"
                        break
                    while self._private():
                        await asyncio.sleep(0.25)
                    if sh:
                        await event(shift, True)
                    if opt:
                        await event(option, True)
                    await event(code, True)
                    await event(code, False)
                    if opt:
                        await event(option, False)
                    if sh:
                        await event(shift, False)
                    done += 1
                    if mono() - last_report > 0.5:
                        last_report = mono()
                        await report("running")
        except asyncio.CancelledError:
            state = "cancelled"
        finally:
            for code in (shift, option) + tuple(self.key_holders):
                self._unpress(holder, code)
            if state != "done":
                v.type_queue.clear()
        await report(state)
        if v.type_queue and not v.closed:       # more text arrived while reporting
            v.typing = asyncio.get_running_loop().create_task(self._type(v))
        else:
            v.type_total = v.type_skipped = 0

    async def _panel_back(self, v: Viewer) -> None:
        """Leave a panel screen that shows a pairing code (the page cannot show it)."""
        panel = getattr(self.app, "panel", None)
        screen = getattr(panel, "screen", None)
        if v.mode != "control" or not getattr(panel, "visible", False) or \
                getattr(screen, "doc_id", None) not in PRIVATE_DOCS:
            return
        if hasattr(screen, "action"):
            self.app.lifecycle.event("remote_screen_panel_back", viewer=v.id, doc=getattr(screen, "doc_id", None))
            await screen.action("cancel", {})

    async def close(self) -> None:
        self._closing = True
        if self._task:
            self._task.cancel()
        for v in list(self.viewers.values()):
            try:
                await asyncio.wait_for(v.ws.close(code=1001, message=b"hostd-stopping"), 2)
            except Exception:   # noqa: BLE001 - shutting down; nothing more to do
                pass
        self.tap.close("hostd stopping")
        self.pool.shutdown(wait=False)
