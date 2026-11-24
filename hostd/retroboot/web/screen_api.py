"""Web Screen page API (ARCHITECTURE section 7): ``GET /api/v1/screen/stream``.

A WebSocket that streams the screen (binary frames, ``remote/encode.py``) and
carries keyboard, pointer, key bar and paste messages back (JSON).  It is
refused unless every one of these holds:

* the request arrived over TLS (never on the plain-HTTP loopback listener);
* a paired session cookie (the security middleware answers 401 otherwise);
* an ``Origin`` header equal to ``https://`` + ``Host`` (browsers always send
  one on WebSocket handshakes, so a missing one is refused too);
* ``remote.screen`` is not ``off`` and hostd is not in installer mode;
* the first message, within 5 seconds, is ``{"t": "hello", "csrf": ...}``
  with the session's CSRF token.

Input is accepted only with ``remote.screen = control`` and is rate limited per
browser (``remote/hub.py``).  ``GET /api/v1/screen/viewers`` lists who is
connected.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from aiohttp import WSMsgType, web

from ..remote import Viewer

if TYPE_CHECKING:
    from .server import WebServer

log = logging.getLogger("retroboot.web.screen")

HELLO_TIMEOUT = 5.0
MAX_MESSAGE = 64 * 1024


def register(server: "WebServer", r: web.UrlDispatcher) -> None:
    from .server import ApiError

    hostd = server.hostd

    async def viewers(request):
        hub = hostd.remote
        return web.json_response({"setting": hub.setting(), "viewers": [v.public() for v in hub.viewers.values()]})

    async def stream(request):
        hub = hostd.remote
        dev, token = request["device"], request["token"]
        if not request.secure:
            raise ApiError(403, "tls-required", "The screen is shown only over a secure (https) connection.")
        origin = request.headers.get("Origin")
        host = request.headers.get("Host", "")
        if not origin or origin.lower() != f"https://{host.lower()}":
            raise ApiError(403, "bad_origin", "This request came from another website, so RetroBoot refused it.")
        if hostd.mode != "normal":
            raise ApiError(409, "installer", "The screen isn't available while the installer is running.")
        mode = hub.setting()
        if mode == "off":
            raise ApiError(403, "remote-screen-off", "The remote screen is turned off. Turn it on under "
                                                     "Network → Remote Screen.")
        refused = hub.check_admission(dev)
        if refused:
            raise ApiError(429, refused[0], refused[1])
        ws = web.WebSocketResponse(heartbeat=20.0, max_msg_size=MAX_MESSAGE, compress=False, autoping=True)
        if not ws.can_prepare(request).ok:
            raise ApiError(400, "websocket-required", "Open this address as a WebSocket.")
        await ws.prepare(request)
        try:
            msg = await ws.receive(timeout=HELLO_TIMEOUT)
            hello = json.loads(msg.data) if msg.type == WSMsgType.TEXT else None
        except (asyncio.TimeoutError, ValueError, TypeError):
            hello = None
        if not isinstance(hello, dict) or hello.get("t") != "hello" \
                or not hostd.pairing.check_csrf(token, hello.get("csrf") if isinstance(hello.get("csrf"), str) else None):
            hostd.lifecycle.event("remote_screen_refused", device=dev.get("id"), why="hello")
            await ws.send_json({"t": "error", "code": "csrf-invalid",
                                "message": "Your session needs to be refreshed. Reload the page and try again."})
            await ws.close(code=4403, message=b"csrf")
            return ws
        # Up to 5 s passed waiting for the hello: the setting, the pairing and the limits may have changed.
        mode = hub.setting()
        refused = hub.check_admission(dev)
        if mode == "off" or hostd.pairing.device_for_token(token) is None or refused:
            code, message = (("remote-screen-off", "The remote screen is turned off.") if mode == "off" else
                             refused if refused else ("not-paired", "This browser is no longer paired."))
            await ws.send_json({"t": "error", "code": code, "message": message})
            await ws.close(code=4403, message=code.encode())
            return ws
        src = hello.get("source")
        if not hub.viewers and mode == "control":
            hub.set_source(src)       # the first browser chooses; the tap follows even while it lingers
        viewer = Viewer(ws, request.transport, dev, hello, hub.swap_setting(),
                        "control" if mode == "control" else "view")
        await hub.attach(viewer)
        why = "closed"
        try:
            async for m in ws:
                if m.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(m.data)
                    except ValueError:
                        continue
                    await hub.on_message(viewer, data)
                elif m.type == WSMsgType.ERROR:
                    why = "error"
                    break
        except (ConnectionError, asyncio.CancelledError):
            why = "connection lost"
            raise
        finally:
            await hub.detach(viewer, why)
        return ws

    r.add_get("/api/v1/screen/stream", stream)
    r.add_get("/api/v1/screen/ws", stream)
    r.add_get("/api/v1/screen/viewers", viewers)
