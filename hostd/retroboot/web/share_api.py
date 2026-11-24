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
import errno
import os
import shutil
import stat
import tempfile
import urllib.parse
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from .. import macfiles as mf
from ..media import MAX_UPLOAD as CD_UPLOAD_MAX
from ..share import MAX_DEPTH, Options, ShareError, SharedFolder, _O_DIR, _O_FILE, split_path
from . import pict

if TYPE_CHECKING:  # pragma: no cover
    from .server import WebServer

UPLOAD_CHUNK = 1024 * 1024
MAX_PREVIEW_BYTES = 32 * 1024 * 1024
MAX_ZIP_BYTES = 512 * 1024 * 1024
MAX_ZIP_ITEMS = 10000
TEXT_EXTS = frozenset({"txt", "text", "md", "log", "csv", "tsv", "json", "xml", "yaml", "yml",
                       "ini", "cfg", "conf", "py", "c", "h", "sh", "rtf", "html", "htm"})


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
    router.add_get("/api/v1/files/preview", api.preview)
    router.add_get("/api/v1/share/preview", api.preview)
    router.add_get("/api/v1/files/zip", api.zip_folder)
    router.add_get("/api/v1/share/zip", api.zip_folder)
    router.add_post("/api/v1/files/move", api.move)
    router.add_post("/api/v1/share/move", api.move)


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

    # -- preview, zip, move (#38, #50) ---------------------------------------------------
    @staticmethod
    def _decode_text(raw: bytes) -> str:
        # Preserve valid non-ASCII UTF-8 uploads; otherwise interpret Classic Mac OS Roman.
        if any(b >= 0x80 for b in raw):
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("mac_roman", errors="replace")
        else:
            text = raw.decode("utf-8", errors="replace")
        return text.replace("\r\n", "\n").replace("\r", "\n")

    @staticmethod
    def _looks_like_text(raw: bytes) -> bool:
        if b"\x00" in raw:
            return False
        sample = raw[:8192]
        if not sample:
            return True
        bad = sum(1 for b in sample if b < 0x20 and b not in (9, 10, 13))
        return bad <= max(1, len(sample) // 64)

    def _read_preview(self, rel: str) -> tuple[bytes, str, str]:
        parts = split_path(rel)
        if not parts:
            raise ShareError(400, "invalid-path", "Choose a file to preview.")
        dfd = self.share._dir_fd(parts[:-1])
        try:
            name = parts[-1]
            try:
                fd = os.open(name, _O_FILE, dir_fd=dfd)
            except FileNotFoundError:
                raise ShareError(404, "not-found", "That file isn't in the shared folder any more.")
            except IsADirectoryError:
                raise ShareError(400, "not-a-file", "Folders cannot be previewed directly.")
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENXIO):
                    raise ShareError(404, "not-found", "That isn't a regular file in the shared folder.")
                raise
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode):
                    raise ShareError(404, "not-found", "That isn't a regular file in the shared folder.")
                if st.st_size > MAX_PREVIEW_BYTES:
                    raise ShareError(413, "too-large", "This file is too large to preview. Download it instead.")
                with os.fdopen(fd, "rb") as f:
                    fd = -1
                    raw = f.read()
            finally:
                if fd >= 0:
                    os.close(fd)

            ftype = b""
            try:
                sfd = os.open(name + ".idump", _O_FILE, dir_fd=dfd)
                with os.fdopen(sfd, "rb") as sf:
                    ftype, _, _ = mf.parse_idump(sf.read(512))
            except OSError:
                pass
            shown = self.share._shown(name)
            ext = mf.extension(shown)

            if ext == "png" or ftype == b"PNGf" or raw.startswith(b"\x89PNG\r\n\x1a\n"):
                return raw, "image/png", shown
            if ext in ("jpg", "jpeg") or ftype == b"JPEG" or raw.startswith(b"\xff\xd8\xff"):
                return raw, "image/jpeg", shown
            if ext == "gif" or ftype == b"GIFf" or raw[:6] in (b"GIF87a", b"GIF89a"):
                return raw, "image/gif", shown
            if ext == "pdf" or ftype == b"PDF " or raw.lstrip().startswith(b"%PDF-"):
                return raw, "application/pdf", shown
            if ext in ("pict", "pct") or ftype == b"PICT":
                try:
                    img, ctype = pict.decode_pict_to_image(raw)
                    return img, ctype, shown
                except ValueError as exc:
                    raise ShareError(422, "unsupported-preview", str(exc))
            if ext in TEXT_EXTS or ftype == b"TEXT" or (ext == "" and self._looks_like_text(raw)):
                return self._decode_text(raw).encode("utf-8"), "text/plain; charset=utf-8", shown
            raise ShareError(415, "unsupported-preview", "Preview isn't available for this file type.")
        finally:
            os.close(dfd)

    async def preview(self, request):
        data, ctype, shown = await self._run(self._read_preview, request.query.get("path", ""))
        quoted = urllib.parse.quote(shown, safe="")
        ascii_name = shown.encode("ascii", "replace").decode().replace('"', "'").replace("?", "_")
        headers = {
            "Content-Type": ctype,
            "Content-Disposition": f'inline; filename="{ascii_name}"; filename*=UTF-8\'\'{quoted}',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        }
        if ctype == "application/pdf":
            headers["X-Frame-Options"] = "SAMEORIGIN"
            headers["Content-Security-Policy"] = (
                "default-src 'self' 'unsafe-inline'; object-src 'self'; frame-ancestors 'self'"
            )
        return web.Response(body=data, headers=headers)

    def _build_zip(self, rel: str) -> tuple[bytes, str]:
        parts = split_path(rel)
        dfd = self.share._dir_fd(parts)
        try:
            arc_name = self.share._shown(parts[-1]) if parts else "RetroBoot Share"
            total_bytes = 0
            count = 0
            buf = tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024)
            with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                def walk(cur_fd: int, prefix: str, depth: int) -> None:
                    nonlocal total_bytes, count
                    if depth > MAX_DEPTH:
                        return
                    for host in sorted(os.listdir(cur_fd)):
                        if host.startswith(".") or mf.is_sidecar(host):
                            continue
                        try:
                            st = os.stat(host, dir_fd=cur_fd, follow_symlinks=False)
                        except OSError:
                            continue
                        shown = self.share._shown(host)
                        arc = f"{prefix}/{shown}" if prefix else shown
                        if stat.S_ISDIR(st.st_mode):
                            sub_fd = os.open(host, _O_DIR, dir_fd=cur_fd)
                            try:
                                zf.writestr(arc + "/", b"")
                                walk(sub_fd, arc, depth + 1)
                            finally:
                                os.close(sub_fd)
                        elif stat.S_ISREG(st.st_mode):
                            count += 1
                            total_bytes += st.st_size
                            if count > MAX_ZIP_ITEMS or total_bytes > MAX_ZIP_BYTES:
                                raise ShareError(413, "too-large", "This folder is too large to zip.")
                            try:
                                fd = os.open(host, _O_FILE, dir_fd=cur_fd)
                            except OSError:
                                continue
                            with os.fdopen(fd, "rb") as f:
                                zf.writestr(arc, f.read())

                walk(dfd, "", len(parts))
            buf.seek(0)
            return buf.read(), f"{arc_name}.zip"
        finally:
            os.close(dfd)

    async def zip_folder(self, request):
        blob, fname = await self._run(self._build_zip, request.query.get("path", ""))
        quoted = urllib.parse.quote(fname, safe="")
        ascii_name = fname.encode("ascii", "replace").decode().replace('"', "'").replace("?", "_")
        return web.Response(
            body=blob,
            headers={
                "Content-Type": "application/zip",
                "Content-Disposition": f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quoted}',
                "Content-Length": str(len(blob)),
                "Cache-Control": "no-store",
            },
        )

    def _move_sync(self, src: str, dst: str, overwrite: bool = False) -> dict:
        src_parts = split_path(src)
        if not src_parts:
            raise ShareError(400, "invalid-path", "The shared folder itself can't be moved.")
        dst_parts = split_path(dst)
        with self.share.lock:
            src_dfd = self.share._dir_fd(src_parts[:-1])
            dst_dfd = -1
            try:
                src_name = src_parts[-1]
                try:
                    src_st = os.stat(src_name, dir_fd=src_dfd, follow_symlinks=False)
                except FileNotFoundError:
                    raise ShareError(404, "not-found", "That item isn't there any more.")
                is_dir = stat.S_ISDIR(src_st.st_mode)
                if not (is_dir or stat.S_ISREG(src_st.st_mode)):
                    raise ShareError(404, "not-found", "That isn't a file or folder in the shared folder.")
                known = self.share.guest_open()
                if not is_dir and self.share._is_open(src_st, known):
                    raise ShareError(409, "file-open", "Close this file in Mac OS before moving it.")

                # If dst is "" or resolves to an existing folder, move src inside dst preserving name.
                into_folder = False
                if not dst_parts:
                    into_folder = True
                else:
                    try:
                        probe_fd = self.share._dir_fd(dst_parts)
                        os.close(probe_fd)
                        into_folder = True
                    except ShareError:
                        into_folder = False

                if into_folder:
                    dst_parent_parts = dst_parts
                    dst_name = src_name
                else:
                    dst_parent_parts = dst_parts[:-1]
                    dst_name = mf.host_name(self.share.typed_name(dst_parts[-1]))

                if is_dir and dst_parent_parts[:len(src_parts)] == src_parts:
                    raise ShareError(400, "invalid-path", "A folder can't be moved inside itself.")

                dst_dfd = self.share._dir_fd(dst_parent_parts)
                dst_rel = "/".join(dst_parent_parts + [dst_name])
                if dst_rel == "/".join(src_parts):
                    st = os.stat(dst_name, dir_fd=dst_dfd, follow_symlinks=False)
                    return {"from": "/".join(src_parts), "to": dst_rel,
                            "item": self.share._entry(dst_dfd, dst_name, st, known, "/".join(dst_parent_parts))}

                mac = self.share._mac_name(src_dfd, src_name, src_st) if not is_dir else mf.safe_mac(
                    self.share._shown(dst_name))
                taken = self.share._taken(dst_dfd)
                other = taken.get(mf.fold(mac) if mac else "\0" + dst_name)
                if other is not None:
                    if not overwrite:
                        raise ShareError(409, "name-exists",
                                         f"An item named {self.share._shown(other)} already exists there.")
                    try:
                        existing_st = os.stat(other, dir_fd=dst_dfd, follow_symlinks=False)
                    except FileNotFoundError:
                        existing_st = None
                    if existing_st is not None:
                        if stat.S_ISDIR(existing_st.st_mode):
                            raise ShareError(409, "name-exists", "Cannot overwrite a folder.")
                        if self.share._is_open(existing_st, known):
                            raise ShareError(409, "file-open", "The existing destination file is open in Mac OS.")
                        os.unlink(other, dir_fd=dst_dfd)
                        for sc in mf.SIDECARS:
                            try:
                                os.unlink(other + sc, dir_fd=dst_dfd)
                            except FileNotFoundError:
                                pass

                try:
                    os.rename(src_name, dst_name, src_dir_fd=src_dfd, dst_dir_fd=dst_dfd)
                    for sc in (mf.SIDECARS if not is_dir else (".idump",)):
                        try:
                            os.rename(src_name + sc, dst_name + sc, src_dir_fd=src_dfd, dst_dir_fd=dst_dfd)
                        except FileNotFoundError:
                            pass
                except OSError as exc:
                    if exc.errno != errno.EXDEV or is_dir:
                        raise ShareError(409, "move-failed", f"Could not move across volumes: {exc}")
                    # Cross-filesystem move (e.g. Share <-> mounted USB drive) for regular files
                    sfd = os.open(src_name, _O_FILE, dir_fd=src_dfd)
                    dfd = os.open(dst_name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o664, dir_fd=dst_dfd)
                    try:
                        with os.fdopen(sfd, "rb") as rf, os.fdopen(dfd, "wb") as wf:
                            shutil.copyfileobj(rf, wf)
                    except Exception:
                        try:
                            os.unlink(dst_name, dir_fd=dst_dfd)
                        except OSError:
                            pass
                        raise
                    os.unlink(src_name, dir_fd=src_dfd)
                    for sc in mf.SIDECARS:
                        try:
                            os.unlink(src_name + sc, dir_fd=src_dfd)
                        except FileNotFoundError:
                            pass

                self.share._touch(src_dfd)
                self.share._touch(dst_dfd)
                st = os.stat(dst_name, dir_fd=dst_dfd, follow_symlinks=False)
                entry = self.share._entry(dst_dfd, dst_name, st, known, "/".join(dst_parent_parts))
                return {"from": "/".join(src_parts), "to": entry["path"], "item": entry}
            finally:
                os.close(src_dfd)
                if dst_dfd >= 0:
                    os.close(dst_dfd)

    async def move(self, request):
        from .server import ApiError
        data = await self.server.body(request)
        src = data.get("src") if "src" in data else (data.get("path") if "path" in data else data.get("from"))
        dst = (data.get("dst") if "dst" in data else
               data.get("to_dir") if "to_dir" in data else data.get("to"))
        overwrite = data.get("overwrite", False)
        if not isinstance(src, str) or not isinstance(dst, str) or type(overwrite) is not bool:
            raise ApiError(422, "invalid", "Provide src and dst string paths.")
        moved = await self._run(self._move_sync, src, dst, overwrite)
        self.hostd.lifecycle.event("share_moved", src=moved["from"], dst=moved["to"], origin="web")
        return web.json_response({"moved": moved})
