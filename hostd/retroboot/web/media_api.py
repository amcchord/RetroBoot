"""Media library API (ARCHITECTURE §7; retroboot/media.py).

    GET    /api/v1/media                     library, CD drive state, install availability
    POST   /api/v1/media?name=FILE           upload (application/octet-stream, Content-Length)
    DELETE /api/v1/media/{name}              remove a file (409 media-in-use for the chosen disc)
    POST   /api/v1/media/{name}/install      {dest?, replace?, restart?} -> job media.install

The CD-ROM disc itself is the setting ``media.cd`` (PATCH /api/v1/settings,
applied at the next Mac OS start).  Names are library file names; the API
never accepts host paths.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import web

from ..media import MediaError

if TYPE_CHECKING:  # pragma: no cover
    from .server import WebServer

UPLOAD_TYPES = ("application/octet-stream", "application/x-stuffit", "application/x-sit",
                "application/x-iso9660-image", "application/x-apple-diskimage", "application/mac-binhex40",
                "application/macbinary", "application/zip")


def register(server: "WebServer", router: web.UrlDispatcher) -> None:
    api = MediaApi(server)
    router.add_get("/api/v1/media", api.list)
    router.add_post("/api/v1/media", api.upload)
    router.add_delete("/api/v1/media/{name}", api.delete)
    router.add_post("/api/v1/media/{name}/install", api.install)


class MediaApi:
    def __init__(self, server: "WebServer"):
        self.server = server
        self.hostd = server.hostd

    def _err(self, e: MediaError):
        from .server import ApiError
        return ApiError(e.status, e.code, e.message)

    def _guest_active(self) -> bool:
        return bool(self.hostd.supervisor.active)

    async def list(self, request):
        s = self.hostd.settings
        return web.json_response(self.hostd.media.public(s.desired, s.applied, self._guest_active(),
                                                        self.hostd.supervisor.active_machine()))

    async def upload(self, request):
        from .server import ApiError
        if request.content_type not in UPLOAD_TYPES:
            raise ApiError(415, "unsupported_media_type", "Upload the file itself (application/octet-stream).")
        length = request.content_length
        if length is None:
            raise ApiError(411, "bad_length", "Send the file with a Content-Length.")
        try:
            item = await self.hostd.media.store(request.content.iter_chunked(1024 * 1024), length,
                                                request.query.get("name", "")[:200])
        except MediaError as e:
            raise self._err(e)
        self.hostd.lifecycle.event("media_added", media=item["id"], kind=item["kind"], bytes=item["bytes"])
        return web.json_response({"item": item}, status=201)

    async def delete(self, request):
        name = request.match_info["name"]
        s = self.hostd.settings
        in_use = {s.desired.get("media.cd"), s.applied.get("media.cd")}
        if self._guest_active() and self.hostd.media.inserted:
            in_use.add(self.hostd.media.inserted)
        try:
            self.hostd.media.delete(name, {n for n in in_use if n and n != "none"})
        except MediaError as e:
            raise self._err(e)
        self.hostd.lifecycle.event("media_removed", media=name)
        return web.json_response({"deleted": name})

    async def install(self, request):
        from .server import ApiError
        name = request.match_info["name"]
        data = await self.server.body(request, required=False)
        unknown = set(data) - {"dest", "replace", "restart"}
        if unknown:
            raise ApiError(422, "invalid", "Some fields aren't recognized.", {k: "Unknown field." for k in unknown})
        dest = data.get("dest")
        if dest is not None and (not isinstance(dest, str) or not 0 < len(dest) <= 255 or "/" in dest
                                 or any(not p or len(p) > 31 for p in dest.split(":"))):
            raise ApiError(422, "invalid", "Choose a folder on Mac OS's disk, such as Applications (Mac OS 9).",
                           {"dest": "A Mac path below the disk, folder names separated by ':'."})
        replace, restart = data.get("replace", False), data.get("restart", False)
        if type(replace) is not bool or type(restart) is not bool:
            raise ApiError(422, "invalid", "replace and restart must be true or false.")
        media = self.hostd.media
        it = media.item(name)
        if it is None:
            raise ApiError(404, "not_found", "That file isn't in the media library.")
        if not it["can_install"]:
            raise ApiError(422, "not-installable", "Only software archives and disk images can be installed. "
                                                   "Put CD images in the CD-ROM drive instead.")
        sup = self.hostd.supervisor
        machine = sup.active_machine()
        if machine is None:
            raise ApiError(409, "not_found", "No Mac OS machine is installed.")
        ok, why = media.install_available(machine)
        if not ok:
            raise ApiError(409, "capability-missing", why)
        if sup.active and sup.machine is not None and sup.machine.id == machine.id and not restart:
            reason = ("Mac OS must shut down while RetroBoot installs software into its disk. "
                      "Install with restart, or shut down Mac OS first.")
            raise ApiError(409, "guest-running", reason, extra={"blocked_reason": reason})
        job = self.server.begin(lambda: self.hostd.media_install_job(name, dest, replace, restart, origin="web"))
        return self.server.jobres(job)
