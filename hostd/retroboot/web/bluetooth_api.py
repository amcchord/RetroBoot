"""Bluetooth API (ARCHITECTURE sections 7 and 15; retroboot/bluetooth.py).

    GET  /api/v1/bluetooth              adapter state, devices (paired, then nearby in pairing mode), pairing
    POST /api/v1/bluetooth/scan         {seconds?} look for devices (default 40 s, at most 120)
    POST /api/v1/bluetooth/pair         {address} -> job bluetooth.pair; the code to type appears in
                                        GET .../bluetooth "pairing" for the browser that started it
    POST /api/v1/bluetooth/confirm      {accept} numeric comparison ("Does the device show this code?")
    POST /api/v1/bluetooth/cancel       stop the pairing in progress
    POST /api/v1/bluetooth/dismiss      forget a finished pairing's result
    POST /api/v1/bluetooth/connect      {address} -> job bluetooth.connect (a paired device)
    POST /api/v1/bluetooth/disconnect   {address}
    POST /api/v1/bluetooth/forget       {address} remove the pairing

Turning Bluetooth on or off is the setting ``bluetooth.enabled`` (PATCH /api/v1/settings).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import web

from ..bluetooth import BluetoothError, message_for, norm_address

if TYPE_CHECKING:  # pragma: no cover
    from .server import WebServer


def register(server: "WebServer", router: web.UrlDispatcher) -> None:
    api = BluetoothApi(server)
    router.add_get("/api/v1/bluetooth", api.status)
    for op in ("scan", "pair", "confirm", "cancel", "dismiss", "connect", "disconnect", "forget"):
        router.add_post(f"/api/v1/bluetooth/{op}", getattr(api, op))


STATUS = {"no_bluetooth": 409, "off": 409, "blocked": 409, "busy": 409, "not_found": 404, "not_paired": 409}
CODES = {"no_bluetooth": "capability-missing", "off": "bluetooth-off", "blocked": "bluetooth-blocked"}


class BluetoothApi:
    def __init__(self, server: "WebServer"):
        self.server = server
        self.hostd = server.hostd

    @property
    def bt(self):
        return self.hostd.bluetooth

    def _error(self, exc: BluetoothError, name: str = "the device"):
        from .server import ApiError
        return ApiError(STATUS.get(exc.code, 422), CODES.get(exc.code, exc.code.replace("_", "-")),
                        message_for(exc.code, name))

    async def _address(self, request, need: bool = True) -> tuple[dict, str | None]:
        from .server import ApiError
        data = await self.server.body(request, required=need)
        if not need:
            return data, None
        try:
            return data, norm_address(data.get("address") or data.get("id"))
        except BluetoothError:
            raise ApiError(422, "invalid", "Choose a Bluetooth device.", {"address": "A Bluetooth address."})

    def _ready(self) -> None:
        try:
            self.bt.check_ready()
        except BluetoothError as exc:
            raise self._error(exc)

    def _name(self, address: str) -> str:
        return (self.bt.device(address) or {}).get("name") or "the device"

    @staticmethod
    def _viewer(request) -> str | None:
        dev = request.get("device")
        return dev.get("id") if isinstance(dev, dict) else None

    async def status(self, request):
        return web.json_response(self.bt.public("web", self._viewer(request)))

    async def scan(self, request):
        from .server import ApiError
        data = await self.server.body(request, required=False)
        seconds = data.get("seconds", self.bt.SCAN_S)
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 1 <= seconds <= 120:
            raise ApiError(422, "invalid", "seconds is 1 to 120.", {"seconds": "1 to 120."})
        self._ready()
        self.bt.scan("web", float(seconds), restart=True)
        return web.json_response({"scanning": True, "seconds": seconds})

    async def pair(self, request):
        _, address = await self._address(request)
        self._ready()
        if self.bt.session and not self.bt.session.done:
            raise self._error(BluetoothError("busy"))
        if self.bt.device(address) is None:
            raise self._error(BluetoothError("not_found"))
        return self.server.jobres(self.server.begin(
            lambda: self.hostd.bluetooth_job("pair", origin="web", address=address, owner=self._viewer(request))))

    async def confirm(self, request):
        from .server import ApiError
        data = await self.server.body(request)
        accept = data.get("accept")
        if type(accept) is not bool:
            raise ApiError(422, "invalid", "accept must be true or false.", {"accept": "Must be a boolean."})
        s = self.bt.session
        if not s or s.origin != "web" or (s.owner and s.owner != self._viewer(request)) or not self.bt.confirm(accept):
            raise ApiError(409, "nothing-to-confirm", "No pairing is waiting for an answer.")
        return web.json_response({"accepted": accept})

    async def cancel(self, request):
        await self.server.body(request, required=False)
        return web.json_response({"canceled": await self.bt.cancel_pairing()})

    async def dismiss(self, request):
        await self.server.body(request, required=False)
        self.bt.dismiss()
        return web.json_response({"dismissed": True})

    async def connect(self, request):
        _, address = await self._address(request)
        self._ready()
        d = self.bt.device(address)
        if d is None:
            raise self._error(BluetoothError("not_found"))
        if not d["paired"]:
            raise self._error(BluetoothError("not_paired"), d["name"])
        return self.server.jobres(self.server.begin(
            lambda: self.hostd.bluetooth_job("connect", origin="web", address=address)))

    async def disconnect(self, request):
        _, address = await self._address(request)
        try:
            await self.bt.disconnect(address)
        except BluetoothError as exc:
            raise self._error(exc, self._name(address))
        return web.json_response({"disconnected": address})

    async def forget(self, request):
        _, address = await self._address(request)
        name = self._name(address)
        try:
            await self.bt.forget(address, origin="web")
        except BluetoothError as exc:
            raise self._error(exc, name)
        return web.json_response({"forgotten": address})
