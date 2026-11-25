"""Software Update API (ARCHITECTURE sections 7 and 11; retroboot/update).

    GET  /api/v1/update                 current version, staged update, previous version, USB, last check
    POST /api/v1/update/bundle          upload a .rbu (application/octet-stream, Content-Length) -> job update.install
    POST /api/v1/update/install         {"source": "usb", "id"} | {"source": "server"} -> job update.install
    POST /api/v1/update/check           -> job update.check (setting update.url, else RetroBoot downloads' list)
    POST /api/v1/update/restart         -> job update.restart ("Restart to Update")
    POST /api/v1/update/rollback        -> job update.rollback ("Go Back to the Previous Version")
    POST /api/v1/update/discard         forget a staged update that was not installed
    POST /api/v1/update/usb             look for update files on USB disks now

Every bundle is verified against the device's trusted keys, whatever its source.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import web

from ..update.manager import MAX_BUNDLE

if TYPE_CHECKING:  # pragma: no cover
    from .server import WebServer


def register(server: "WebServer", router: web.UrlDispatcher) -> None:
    api = UpdateApi(server)
    router.add_get("/api/v1/update", api.status)
    router.add_post("/api/v1/update/bundle", api.upload)
    router.add_post("/api/v1/update/install", api.install)
    router.add_post("/api/v1/update/check", api.check)
    router.add_post("/api/v1/update/restart", api.restart)
    router.add_post("/api/v1/update/rollback", api.rollback)
    router.add_post("/api/v1/update/discard", api.discard)
    router.add_post("/api/v1/update/usb", api.usb)


class UpdateApi:
    def __init__(self, server: "WebServer"):
        self.server = server
        self.hostd = server.hostd

    @property
    def u(self):
        return self.hostd.updates

    def _ready(self, need_committed: bool = True) -> None:
        from .server import ApiError
        if self.hostd.mode != "normal":
            raise ApiError(409, "installer", "Software Update isn't available while the installer is running.")
        if self.u.unavailable:
            raise ApiError(409, "update-unavailable", self.u.unavailable)
        if need_committed and not self.u.committed:
            raise ApiError(409, "update-pending", "RetroBoot is still checking the version it just started. "
                                                  "Try again in a minute.")

    async def _fields(self, request, allowed: set) -> dict:
        from .server import ApiError
        data = await self.server.body(request, required=False)
        unknown = set(data) - allowed
        if unknown:
            raise ApiError(422, "invalid", "Some fields aren't recognized.", {k: "Unknown field." for k in unknown})
        return data

    async def status(self, request):
        return web.json_response(self.u.public())

    async def upload(self, request):
        from .server import ApiError
        self._ready()
        if request.content_type != "application/octet-stream":
            raise ApiError(415, "unsupported_media_type", "Upload the update file itself (application/octet-stream).")
        length = request.content_length
        if length is None:
            raise ApiError(411, "bad_length", "Send the update with a Content-Length.")
        if not 10240 <= length <= MAX_BUNDLE:
            raise ApiError(413, "bad_length", "That file is not a RetroBoot update.")
        holder = self.hostd.jobs.holder("update")
        if holder is not None:
            raise ApiError(409, "busy", "Software Update is busy. Try again when it finishes.",
                           extra={"job": holder.public()})
        if not self.u.space_for(length):
            raise ApiError(507, "no_space", "There isn't enough free space for this update.")
        flags = {k: request.query.get(k) == "1" for k in ("allow_older", "allow_flavor", "clean")}
        path = self.u.new_incoming()
        got = 0
        try:
            with open(path, "xb") as f:
                async for chunk in request.content.iter_chunked(1024 * 1024):
                    got += len(chunk)
                    if got > length:
                        break
                    f.write(chunk)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        if got != length:
            path.unlink(missing_ok=True)
            raise ApiError(400, "incomplete", "The upload did not complete.")
        self.hostd.lifecycle.event("update_uploaded", bytes=got)
        try:
            job = self.server.begin(lambda: self.u.install_job({"kind": "file", "path": str(path)}, origin="web",
                                                               **flags))
        except ApiError:
            path.unlink(missing_ok=True)
            raise
        return self.server.jobres(job)

    async def install(self, request):
        from .server import ApiError
        self._ready()
        data = await self._fields(request, {"source", "id", "clean"})
        if "clean" in data and type(data["clean"]) is not bool:
            raise ApiError(422, "invalid", "Choose whether to run a Clean Update.",
                           {"clean": "Must be true or false."})
        clean = bool(data.get("clean", False))
        src = data.get("source")
        if src == "usb" and isinstance(data.get("id"), str):
            spec = {"kind": "usb", "id": data["id"][:300]}
        elif src == "server":
            spec = {"kind": "feed"}
        else:
            raise ApiError(422, "invalid", "Choose an update to install.",
                           {"source": "Use usb with an id, or server."})
        return self.server.jobres(self.server.begin(lambda: self.u.install_job(spec, origin="web", clean=clean)))

    async def check(self, request):
        self._ready(need_committed=False)
        await self._fields(request, set())
        return self.server.jobres(self.server.begin(lambda: self.u.check_job(origin="web")))

    async def restart(self, request):
        from .server import ApiError
        self._ready()
        data = await self._fields(request, {"clean"})
        if "clean" in data and data["clean"] is not None and type(data["clean"]) is not bool:
            raise ApiError(422, "invalid", "Choose whether to run a Clean Update.",
                           {"clean": "Must be true or false."})
        clean = data.get("clean")
        return self.server.jobres(self.server.begin(lambda: self.u.restart_job(origin="web", clean=clean)))

    async def rollback(self, request):
        self._ready()
        await self._fields(request, set())
        return self.server.jobres(self.server.begin(lambda: self.u.rollback_job(origin="web")))

    async def discard(self, request):
        from .server import ApiError
        self._ready()
        await self._fields(request, set())
        if not self.u.discard_staged():
            raise ApiError(409, "nothing-staged", "There is no update waiting to be installed.")
        return web.json_response(self.u.public())

    async def usb(self, request):
        self._ready(need_committed=False)
        await self._fields(request, set())
        await self.u.scan_usb()
        return web.json_response(self.u.public())
