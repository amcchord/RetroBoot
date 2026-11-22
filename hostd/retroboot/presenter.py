"""Presenter control-socket client (ARCHITECTURE section 4).

Newline-delimited JSON over ``/run/retroboot/presenter.sock``.  Requests carry
an integer ``id``; replies are ``{"id":N,"ok":...}``; events carry ``event``.
The client reconnects forever with backoff (a short, fixed interval until the
first connection: hostd starts beside the presenter and waits for its socket).
After every (re)connection it
sends ``hello`` and invokes ``on_connect`` so hostd can re-push display/input
configuration, the current scene and the current panel document — the
presenter may have restarted and lost them.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

log = logging.getLogger("retroboot.presenter")


class PresenterError(Exception):
    pass


# Until the presenter has been reached once, retry this often for this long: at
# boot hostd starts in parallel with the presenter, and every retry interval the
# socket is late adds directly to the launch of Mac OS.
FIRST_CONNECT_RETRY_S = 0.05
FIRST_CONNECT_WINDOW_S = 60.0


class PresenterClient:
    def __init__(self, path: Path | str, request_timeout: float = 5.0):
        self.path = str(path)
        self.request_timeout = request_timeout
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next = 1
        self._task: asyncio.Task | None = None
        self._stopping = False
        self.connected = asyncio.Event()
        self._display_events = 0
        self.hello: dict | None = None
        self.display: dict | None = None
        self.guest: dict | None = None
        self.input: dict | None = None      # touchscreens and pens (hello "input", "input" events)
        self.event_handlers: list[Callable[[dict], Any]] = []
        self.on_connect: list[Callable[[dict], Awaitable[None]]] = []
        self.on_disconnect: list[Callable[[], Any]] = []
        self.connections = 0

    def start(self) -> None:
        self._stopping = False
        self._task = asyncio.get_running_loop().create_task(self._run(), name="presenter-client")

    async def stop(self) -> None:
        self._stopping = True
        if self.writer:
            try:
                self.writer.close()
            except Exception:
                pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run(self) -> None:
        backoff = 0.1
        loop = asyncio.get_running_loop()
        started = loop.time()
        while not self._stopping:
            try:
                self.reader, self.writer = await asyncio.open_unix_connection(self.path, limit=8 * 1024 * 1024)
            except (FileNotFoundError, ConnectionRefusedError, OSError):
                if self.connections == 0 and loop.time() - started < FIRST_CONNECT_WINDOW_S:
                    await asyncio.sleep(FIRST_CONNECT_RETRY_S)
                    continue
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 2.0)
                continue
            backoff = 0.1
            reader_task = asyncio.get_running_loop().create_task(self._read_loop())
            try:
                seen = self._display_events
                hello = await self.request("hello", _bypass=True)
                self.hello = hello
                # A display event read right behind the reply is newer than the hello's.
                if hello.get("display") and self._display_events == seen:
                    self.display = hello["display"]
                self.guest = hello.get("guest")
                if hello.get("input") is not None:
                    self.input = hello["input"]
                self.connections += 1
                self.connected.set()
                for cb in list(self.on_connect):
                    try:
                        await cb(hello)
                    except Exception:
                        log.exception("presenter on_connect handler failed")
                await reader_task
            except (PresenterError, ConnectionError, OSError) as exc:
                log.warning("presenter connection problem: %s", exc)
            finally:
                reader_task.cancel()
                self.connected.clear()
                try:
                    self.writer.close()
                except Exception:
                    pass
                self.writer = None
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(PresenterError("presenter disconnected"))
                self._pending.clear()
                for cb in list(self.on_disconnect):
                    try:
                        cb()
                    except Exception:
                        log.exception("presenter on_disconnect handler failed")
            if not self._stopping:
                await asyncio.sleep(0.2)

    async def _read_loop(self) -> None:
        while True:
            line = await self.reader.readline()
            if not line:
                return
            try:
                msg = json.loads(line)
            except ValueError:
                log.warning("presenter sent invalid JSON")
                continue
            if not isinstance(msg, dict):
                continue
            # Events carry "event" (ui_action/ui_change also carry a widget "id");
            # replies carry an integer "id" and "ok".
            if "event" in msg and "ok" not in msg:
                if msg["event"] == "display":
                    self.display = {k: v for k, v in msg.items() if k != "event"}
                    self._display_events += 1
                elif msg["event"] in ("guest_connected", "guest_disconnected"):
                    self.guest = {k: v for k, v in msg.items() if k != "event"}
                elif msg["event"] == "input":
                    self.input = {k: v for k, v in msg.items() if k != "event"}
                for h in list(self.event_handlers):
                    try:
                        r = h(msg)
                        if asyncio.iscoroutine(r):
                            asyncio.get_running_loop().create_task(r)
                    except Exception:
                        log.exception("presenter event handler failed")
                continue
            fut = self._pending.pop(msg.get("id"), None)
            if fut is not None and not fut.done():
                fut.set_result(msg)

    async def request(self, cmd: str, _bypass: bool = False, timeout: float | None = None, **fields: Any) -> dict:
        if self.writer is None or (not _bypass and not self.connected.is_set()):
            raise PresenterError("presenter not connected")
        req_id = self._next
        self._next += 1
        fut = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        msg = {"id": req_id, "cmd": cmd}
        msg.update(fields)
        self.writer.write((json.dumps(msg, separators=(",", ":")) + "\n").encode())
        try:
            await self.writer.drain()
            resp = await asyncio.wait_for(fut, timeout or self.request_timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise PresenterError(f"presenter {cmd} timed out")
        except (ConnectionError, OSError) as exc:
            self._pending.pop(req_id, None)
            raise PresenterError(str(exc))
        if not resp.get("ok", False):
            raise PresenterError(resp.get("error") or f"presenter rejected {cmd}")
        return resp

    async def try_request(self, cmd: str, **fields: Any) -> dict | None:
        """Best-effort request: returns None when the presenter is unavailable."""
        try:
            return await self.request(cmd, **fields)
        except PresenterError as exc:
            log.info("presenter %s not delivered: %s", cmd, exc)
            return None
