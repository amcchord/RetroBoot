"""Shared folder API (ARCHITECTURE §7; retroboot/share.py, retroboot/macfiles.py).

    GET  /api/v1/share/list?path=P              the folder as Mac OS shows it, plus the share's state
    POST /api/v1/share/check {path, names}      Mac names and conflicts for files about to be copied
    POST /api/v1/share/upload?path=P&name=N     one file, application/octet-stream with Content-Length;
         [&relpath=R&expand=1&text=1&conflict=rename|replace&mtime=MS]   -> 201 {items, source, notes}
    GET  /api/v1/share/download?path=P[&format=auto|macbinary|data]
    POST /api/v1/share/mkdir  {path, name, conflict?: fail|rename, sanitize?: bool, relpath?}
    POST /api/v1/share/rename {path, name}
    POST /api/v1/share/delete {path, recursive?}

``path`` is a folder or item inside the share: host names joined by '/', as
returned by ``list``.  Every call needs the paired session; the POSTs need the
CSRF header (server.py middleware).  The API never accepts host paths outside
the share, symlinks, hidden names or sidecars.
"""
from __future__ import annotations

import asyncio
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from ..media import MAX_UPLOAD as CD_UPLOAD_MAX
from ..share import Options, ShareError, SharedFolder

if TYPE_CHECKING:  # pragma: no cover
    from .server import WebServer

UPLOAD_CHUNK = 1024 * 1024


def register(server: "WebServer", router: web.UrlDispatcher) -> None:
    api = ShareApi(server)
    server.share_api = api
    router.add_get("/api/v1/share/list", api.list)
    router.add_post("/api/v1/share/check", api.check)
    router.add_post("/api/v1/share/upload", api.upload)
    router.add_get("/api/v1/share/download", api.download)
    router.add_post("/api/v1/share/mkdir", api.mkdir)
    router.add_post("/api/v1/share/rename", api.rename)
    router.add_post("/api/v1/share/delete", api.delete)


class ShareApi:
    def __init__(self, server: "WebServer"):
        self.server = server
        self.hostd = server.hostd
        self._share: SharedFolder | None = None

    @property
    def share(self) -> SharedFolder:
        if self._share is None:
            paths = self.hostd.paths
            self._share = SharedFolder(paths.share, apps_lib=Path(paths.apps_lib), guest_pids=self._guest_pids)
        return self._share

    def _guest_pids(self):
        """QEMU's PID while Mac OS runs ([] when it doesn't: nothing is open)."""
        from ..share import qemu_pids
        sup = self.hostd.supervisor
        if not sup.active:
            return [] if qemu_pids() is not None else None
        pid = None
        try:
            pid = (sup.handle.status() or {}).get("pid") if sup.handle else None
        except Exception:  # noqa: BLE001 - fall back to scanning
            pid = None
        if pid:
            return [int(pid)]
        return qemu_pids()

    @staticmethod
    def _err(e: ShareError):
        from .server import ApiError
        fields = e.extra.pop("fields", None) if e.extra else None
        return ApiError(e.status, e.code, e.message, fields, e.extra or None)

    async def _run(self, fn, *args):
        try:
            return await asyncio.to_thread(fn, *args)
        except ShareError as e:
            raise self._err(e)

    def state(self) -> dict:
        h = self.hostd
        s = h.settings
        ok, why = self.share.expand_available()
        return {"enabled": bool(s.desired.get("share.enabled", True)),
                "applied": bool(s.applied.get("share.enabled", True)),
                "pending_restart": s.desired.get("share.enabled") != s.applied.get("share.enabled")
                or s.desired.get("share.volume_name") != s.applied.get("share.volume_name"),
                "volume_name": s.applied.get("share.volume_name", "RetroBoot Share"),
                "guest_running": bool(h.supervisor.active),
                "max_file_bytes": self.share.max_file,
                "expand_available": ok, "expand_reason": why,
                "cd_upload_max_bytes": CD_UPLOAD_MAX}

    # -- handlers ---------------------------------------------------------------------------
    async def list(self, request):
        listing = await self._run(self.share.list, request.query.get("path", ""))
        listing["share"] = self.state()
        return web.json_response(listing)

    async def check(self, request):
        from .server import ApiError
        data = await self.server.body(request)
        names = data.get("names")
        if not isinstance(names, list) or not all(isinstance(n, str) and 0 < len(n) <= 1024 for n in names):
            raise ApiError(422, "invalid", "names must be a list of file names.", {"names": "A list of names."})
        return web.json_response({"items": await self._run(self.share.check, str(data.get("path") or ""), names)})

    async def upload(self, request):
        from .server import ApiError
        if request.content_type != "application/octet-stream":
            raise ApiError(415, "unsupported-media-type", "Send the file itself (application/octet-stream).")
        length = request.content_length
        if length is None:
            raise ApiError(411, "length-required", "Send the file with a Content-Length.")
        q = request.query
        conflict = q.get("conflict", "rename")
        try:
            mtime = float(q["mtime"]) / 1000 if q.get("mtime") else None
        except ValueError:
            mtime = None
        opts = Options(expand=q.get("expand") == "1", convert_text=q.get("text") == "1", conflict=conflict,
                       mtime=mtime)
        name = q.get("name", "")[:1024]
        try:
            result = await self.share.receive(request.content.iter_chunked(UPLOAD_CHUNK), length, q.get("path", ""),
                                              name, q.get("relpath", "")[:4096], opts)
        except ShareError as e:
            raise self._err(e)
        for it in result["items"]:
            self.hostd.lifecycle.event("share_added", path=it["path"], bytes=it.get("bytes"),
                                       source=result["source"], origin="web")
        return web.json_response(result, status=201)

    async def download(self, request):
        fmt = request.query.get("format", "auto")
        d = await self._run(self.share.download, request.query.get("path", ""), fmt)
        quoted = urllib.parse.quote(d.filename, safe="")
        ascii_name = d.filename.encode("ascii", "replace").decode().replace('"', "'").replace("?", "_")
        resp = web.StreamResponse(headers={
            "Content-Type": "application/macbinary" if d.macbinary else "application/octet-stream",
            "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}",
            "Content-Length": str(d.length), "Cache-Control": "no-store",
            # sent before the body, so set here rather than by the error middleware
            "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
            # no "sandbox": Chrome refuses a download whose response is sandboxed
            "Content-Security-Policy": "default-src 'none'"})
        await resp.prepare(request)
        it = d.chunks()
        try:
            while True:
                chunk = await asyncio.to_thread(next, it, None)
                if chunk is None:
                    break
                await resp.write(chunk)
        finally:
            d.close()
        await resp.write_eof()
        return resp

    async def mkdir(self, request):
        from .server import ApiError
        data = await self.server.body(request)
        conflict = "rename" if data.get("conflict") == "rename" else "fail"
        sanitize = data.get("sanitize", False)
        if type(sanitize) is not bool:
            raise ApiError(422, "invalid", "sanitize must be true or false.")
        entry = await self._run(self.share.mkdir, str(data.get("path") or ""), str(data.get("name") or ""), conflict,
                                sanitize, str(data.get("relpath") or ""))
        self.hostd.lifecycle.event("share_folder_created", path=entry["path"], origin="web")
        return web.json_response({"item": entry}, status=201)

    async def rename(self, request):
        data = await self.server.body(request)
        entry = await self._run(self.share.rename, str(data.get("path") or ""), str(data.get("name") or ""))
        self.hostd.lifecycle.event("share_renamed", path=data.get("path"), to=entry["path"], origin="web")
        return web.json_response({"item": entry})

    async def delete(self, request):
        from .server import ApiError
        data = await self.server.body(request)
        recursive = data.get("recursive", False)
        if type(recursive) is not bool:
            raise ApiError(422, "invalid", "recursive must be true or false.")
        res = await self._run(self.share.delete, str(data.get("path") or ""), recursive)
        self.hostd.lifecycle.event("share_deleted", path=res["deleted"], items=res["items"], origin="web")
        return web.json_response(res)
