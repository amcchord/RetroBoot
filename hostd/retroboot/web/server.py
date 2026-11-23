"""aiohttp management server.

HTTPS (default :443) on management interfaces when ``management.web`` is on,
plain HTTP on loopback always.  Security model:

* Unpaired clients may call only ``GET /api/v1/session`` and ``POST /api/v1/pair``
  (plus the static client).
* Every other API call needs the ``rb_session`` cookie (HttpOnly; Secure;
  SameSite=Strict).  Mutating calls also need ``X-RetroBoot-CSRF`` equal to the
  session's CSRF token, ``Content-Type: application/json`` for JSON bodies, and
  — when the browser sends one — an ``Origin`` equal to ``scheme://Host``.
* The ``Host`` header must name this device (an address, the hostname,
  ``<hostname>.local`` or localhost), which defeats DNS rebinding.
* Only typed operations exist: no raw QMP, shell or host paths.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import secrets
import ssl
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

from .. import __version__, schema
from ..catalog import MAX_ARCHIVE, RESERVE, download_of
from ..jobs import Busy
from ..machines import BLANK_SIZES_GB, MachineError
from ..network import NetworkError
from ..security import PairingError, ensure_certificate
from ..settings import NothingToConfirm, StaleRevision

if TYPE_CHECKING:
    from ..hostd import Hostd

log = logging.getLogger("retroboot.web")

COOKIE = "rb_session"
PUBLIC_API = {("GET", "/api/v1/session"), ("POST", "/api/v1/pair")}
JSON_LIMIT = 1024 * 1024



def _is_loopback(remote: str | None) -> bool:
    try:
        return ipaddress.ip_address((remote or "").split("%", 1)[0]).is_loopback
    except ValueError:
        return False

class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, fields: dict | None = None, extra: dict | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.fields, self.extra = status, code, message, fields, extra


CODE_NAMES = {"csrf": "csrf-invalid", "bad-origin": "origin-not-allowed", "bad-host": "host-not-allowed",
              "invalid-code": "pairing-code-invalid", "rate-limited": "too-many-attempts",
              "nothing-to-confirm": "no-pending-confirmation", "internal": "internal-error",
              "installer": "installer-mode", "invalid": "invalid-request", "bad-length": "length-required",
              "incomplete": "upload-incomplete"}


def api_code(code: str) -> str:
    """Error codes on the wire are kebab-case (the web client's convention)."""
    c = str(code).replace("_", "-")
    return CODE_NAMES.get(c, c)


def error_response(status: int, code: str, message: str, fields: dict | None = None, extra: dict | None = None):
    body: dict[str, Any] = {"error": {"code": api_code(code), "message": message}}
    if fields:
        body["error"]["fields"] = fields
    if extra:
        body["error"].update(extra)
    return web.json_response(body, status=status, headers={"Cache-Control": "no-store"})


def job_response(job, status: int = 202):
    """202 for a new job; 200 when an exclusive operation was already running (that job is returned)."""
    return web.json_response({"job": job.public(), **job.public()}, status=status,
                             headers={"Cache-Control": "no-store"})


class WebServer:
    def __init__(self, app: "Hostd"):
        self.hostd = app
        self.cfg = app.cfg
        self.runner: web.AppRunner | None = None
        self.http_site: web.TCPSite | None = None
        self.https_site: web.TCPSite | None = None
        self.fingerprint: str | None = None
        self.ssl_ctx: ssl.SSLContext | None = None
        self._hosts: set[str] = set()
        self._hosts_time = 0.0
        self.web_app = self.build()

    # -- lifecycle -----------------------------------------------------------------
    def build(self) -> web.Application:
        app = web.Application(middlewares=[self.mw_plain_http, self.mw_errors, self.mw_security], client_max_size=JSON_LIMIT)
        r = app.router
        r.add_get("/api/v1/session", self.session)
        r.add_post("/api/v1/pair", self.pair)
        r.add_post("/api/v1/logout", self.logout)
        r.add_get("/api/v1/devices", self.devices)
        r.add_delete("/api/v1/devices/{id}", self.device_delete)
        r.add_get("/api/v1/status", self.status)
        r.add_get("/api/v1/settings", self.settings_get)
        r.add_patch("/api/v1/settings", self.settings_patch)
        r.add_post("/api/v1/settings/apply", self.settings_apply)
        r.add_post("/api/v1/settings/confirm", self.settings_confirm)
        r.add_post("/api/v1/settings/revert", self.settings_revert)
        r.add_post("/api/v1/guest/{action}", self.guest_action)
        r.add_post("/api/v1/graphics/install", self.graphics_install)
        r.add_post("/api/v1/host/{action}", self.host_action)
        r.add_get("/api/v1/jobs", self.jobs_list)
        r.add_get("/api/v1/jobs/{id}", self.job_get)
        r.add_get("/api/v1/screen.png", self.screen)
        r.add_get("/api/v1/machines", self.machines)
        r.add_get("/api/v1/machines/catalog", self.machines_catalog)
        r.add_get("/api/v1/machines/archives", self.machines_archives)
        r.add_post("/api/v1/machines/import", self.machines_import)
        r.add_delete("/api/v1/machines/{id}", self.machines_delete)
        r.add_post("/api/v1/machines/blank", self.machines_blank)
        r.add_get("/api/v1/roms", self.roms_list)
        r.add_post("/api/v1/roms", self.roms_add)
        r.add_post("/api/v1/machines/{id}/start-cd", self.machines_start_cd)
        r.add_get("/api/v1/machines/{id}/checkpoints", self.checkpoints_list)
        r.add_post("/api/v1/machines/{id}/checkpoints", self.checkpoints_create)
        r.add_delete("/api/v1/machines/{id}/checkpoints/{cp}", self.checkpoints_delete)
        r.add_post("/api/v1/machines/{id}/restore", self.checkpoints_restore)
        r.add_post("/api/v1/machines/{id}/checkpoints/{cp}/restore", self.checkpoints_restore)
        r.add_get("/api/v1/network", self.network)
        r.add_post("/api/v1/network/wifi/{op}", self.wifi)
        r.add_get("/api/v1/logs", self.logs)
        r.add_post("/api/v1/diagnostics/bundle", self.bundle_create)
        r.add_get("/api/v1/diagnostics/bundle/{id}", self.bundle_get)
        from .media_api import register as register_media
        register_media(self, r)
        from .share_api import register as register_share
        register_share(self, r)
        from .screen_api import register as register_screen   # web Screen page (remote/)
        register_screen(self, r)
        from .update_api import register as register_update
        register_update(self, r)
        from .bluetooth_api import register as register_bluetooth
        register_bluetooth(self, r)
        from .rpidisplay_api import register as register_pi_display
        register_pi_display(self, r)
        r.add_route("*", "/api/{tail:.*}", self.api_not_found)
        r.add_get("/assets/platinum/{path:.+}", self.platinum_file)
        r.add_get("/", self.static_index)
        r.add_get("/{path:.+}", self.static_file)
        return app

    async def start(self) -> None:
        self.runner = web.AppRunner(self.web_app, access_log=None, handle_signals=False)
        await self.runner.setup()
        if self.cfg.http_port:
            self.http_site = web.TCPSite(self.runner, self.cfg.http_bind, self.cfg.http_port, reuse_address=True)
            await self.http_site.start()
            log.info("HTTP on %s:%s", self.cfg.http_bind, self.cfg.http_port)
        await self.sync_listeners()

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()

    def tls(self) -> ssl.SSLContext:
        if self.ssl_ctx is None:
            cert, key, fp = ensure_certificate(self.hostd.paths.state, self.hostd.settings.applied["management.hostname"])
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(str(cert), str(key))
            self.ssl_ctx = ctx
            self.fingerprint = fp
        return self.ssl_ctx

    async def sync_listeners(self) -> None:
        want = self.hostd.settings.applied["management.web"] == "on" and bool(self.cfg.https_port)
        if want and self.https_site is None and self.runner:
            try:
                self.https_site = web.TCPSite(self.runner, self.cfg.https_bind, self.cfg.https_port,
                                              ssl_context=self.tls(), reuse_address=True)
                await self.https_site.start()
                self.hostd.lifecycle.event("web_https_on", port=self.cfg.https_port)
            except OSError as exc:
                log.error("HTTPS listener failed: %s", exc)
                self.https_site = None
        elif not want and self.https_site is not None:
            await self.https_site.stop()
            self.https_site = None
            self.hostd.lifecycle.event("web_https_off")

    def url_for(self, address: str | None) -> str | None:
        if not address:
            return None
        host = f"[{address}]" if ":" in address else address
        port = "" if self.cfg.https_port == 443 else f":{self.cfg.https_port}"
        return f"https://{host}{port}/"

    # -- security ------------------------------------------------------------------
    async def allowed_hosts(self) -> set[str]:
        if time.monotonic() - self._hosts_time > 15:
            hn = self.hostd.settings.applied["management.hostname"]
            hosts = {"localhost", "127.0.0.1", "::1", hn, f"{hn}.local"}
            published = getattr(getattr(self.hostd, "mdns", None), "name", None)
            if published:                  # avahi's name after a conflict, e.g. retroboot-2.local
                hosts |= {published, published.rsplit(".local", 1)[0]}
            try:
                hosts |= set(await self.hostd.network.all_addresses())
            except Exception:
                pass
            hosts |= set(self.cfg_extra_hosts())
            self._hosts = {h.lower() for h in hosts}
            self._hosts_time = time.monotonic()
        return self._hosts

    def cfg_extra_hosts(self) -> list[str]:
        return list(getattr(self.hostd, "extra_hosts", []) or [])

    @staticmethod
    def host_only(host_header: str) -> str:
        h = host_header.strip().lower()
        if h.startswith("["):
            return h[1:h.find("]")] if "]" in h else h
        if h.count(":") == 1:
            return h.split(":", 1)[0]
        return h

    def client(self, request: web.Request) -> str:
        peer = request.transport.get_extra_info("peername") if request.transport else None
        return peer[0] if peer else "unknown"

    def current(self, request: web.Request) -> tuple[dict | None, str | None]:
        token = request.cookies.get(COOKIE)
        dev = self.hostd.pairing.device_for_token(token)
        return dev, (token if dev else None)

    @web.middleware
    async def mw_plain_http(self, request: web.Request, handler):
        """Plain HTTP serves only this computer's own tools; a browser on the network
        that typed the bare address is sent to the HTTPS page instead (nothing else,
        so no API call or cookie ever travels unencrypted)."""
        if request.secure or _is_loopback(request.remote):
            return await handler(request)
        host = self.host_only(request.headers.get("Host", ""))
        if not host or host not in await self.allowed_hosts():
            self._hosts_time = 0
            if not host or host not in await self.allowed_hosts():
                return web.Response(status=421, text="This address doesn't belong to this RetroBoot computer.\n")
        port = "" if self.cfg.https_port == 443 else f":{self.cfg.https_port}"
        bracketed = f"[{host}]" if ":" in host else host
        location = f"https://{bracketed}{port}{request.rel_url.path_qs}"
        cls = web.HTTPMovedPermanently if request.method in ("GET", "HEAD") else web.HTTPPermanentRedirect
        raise cls(location, headers={"Cache-Control": "no-store"})

    @web.middleware
    async def mw_errors(self, request: web.Request, handler):
        try:
            resp = await handler(request)
        except ApiError as e:
            return error_response(e.status, e.code, e.message, e.fields, e.extra)
        except web.HTTPException as e:
            if request.path.startswith("/api/"):
                return error_response(e.status, e.reason.lower().replace(" ", "_"), e.reason)
            raise
        except Exception:
            log.exception("API handler failed: %s %s", request.method, request.path)
            return error_response(500, "internal", "Something went wrong on RetroBoot. Try again; if it keeps happening, capture diagnostics.")
        if request.path.startswith("/api/"):
            resp.headers.setdefault("Cache-Control", "no-store")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("Content-Security-Policy",
                                "default-src 'self'; img-src 'self' blob: data:; style-src 'self'; script-src 'self'; "
                                "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
                                "frame-ancestors 'none'")
        if not request.path.startswith("/api/") and "Cache-Control" not in resp.headers:
            resp.headers["Cache-Control"] = "no-cache"
            if request.path.endswith((".js", ".mjs")):
                resp.content_type = "text/javascript"
        return resp

    @web.middleware
    async def mw_security(self, request: web.Request, handler):
        host = self.host_only(request.headers.get("Host", ""))
        if host not in await self.allowed_hosts():
            self._hosts_time = 0  # addresses may have changed; refresh once
            if host not in await self.allowed_hosts():
                raise ApiError(421, "bad_host", "This address doesn't belong to this RetroBoot computer.")
        mutating = request.method not in ("GET", "HEAD", "OPTIONS")
        if mutating:
            origin = request.headers.get("Origin")
            if origin is not None:
                scheme = "https" if request.secure else "http"
                if origin.lower() != f"{scheme}://{request.headers.get('Host', '').lower()}":
                    raise ApiError(403, "bad_origin", "This request came from another website, so RetroBoot refused it.")
        if not request.path.startswith("/api/"):
            return await handler(request)
        key = (request.method, request.path.rstrip("/"))
        dev, token = self.current(request)
        request["device"] = dev
        request["token"] = token
        if key in PUBLIC_API:
            return await handler(request)
        if dev is None:
            raise ApiError(401, "not_paired", "Pair this browser first, using the code shown on the RetroBoot screen.")
        if mutating and not self.hostd.pairing.check_csrf(token, request.headers.get("X-RetroBoot-CSRF")):
            raise ApiError(403, "csrf", "Your session needs to be refreshed. Reload the page and try again.")
        self.hostd.pairing.touch(dev)
        return await handler(request)

    async def body(self, request: web.Request, required: bool = True) -> dict:
        if not request.can_read_body:
            if required:
                raise ApiError(400, "bad_request", "The request is missing its details. Reload the page and try again.")
            return {}
        if request.content_type != "application/json":
            raise ApiError(415, "unsupported_media_type", "RetroBoot only accepts requests sent as JSON.")
        try:
            data = await request.json()
        except (ValueError, web.HTTPRequestEntityTooLarge):
            raise ApiError(400, "bad_json", "The request couldn't be read. Reload the page and try again.")
        if not isinstance(data, dict):
            raise ApiError(400, "bad_json", "The request couldn't be read. Reload the page and try again.")
        return data

    # -- session / pairing -----------------------------------------------------------
    async def session(self, request):
        dev, token = request["device"], request["token"]
        return web.json_response({"paired": dev is not None, "device_name": dev["name"] if dev else None,
                                  "device_id": dev["id"] if dev else None,
                                  "csrf": self.hostd.pairing.csrf_for(token) if dev else None,
                                  "version": __version__, "mode": self.hostd.mode,
                                  "hostname": self.hostd.settings.applied["management.hostname"],
                                  "form_factor": self.hostd.hardware["form_factor"]})

    async def pair(self, request):
        data = await self.body(request)
        try:
            dev, token = self.hostd.pairing.pair(data.get("code"), data.get("device_name"), self.client(request))
        except PairingError as e:
            self.hostd.lifecycle.event("pairing_failed", client=self.client(request), code=e.code)
            fields = {"code": e.message} if e.code == "invalid_code" else (
                {"device_name": e.message} if e.code == "invalid" else None)
            raise ApiError(e.status, e.code, e.message, fields)
        self.hostd.lifecycle.event("paired", device=dev["id"], name=dev["name"], client=self.client(request))
        self.hostd.loop.create_task(self.hostd.panel_paired(dev["name"]))
        resp = web.json_response({"paired": True, "device": dev, "csrf": self.hostd.pairing.csrf_for(token)})
        resp.set_cookie(COOKIE, token, max_age=365 * 86400, path="/", secure=True, httponly=True, samesite="Strict")
        return resp

    async def logout(self, request):
        dev = request["device"]
        self.hostd.pairing.revoke(dev["id"])
        self.hostd.lifecycle.event("unpaired", device=dev["id"], why="logout")
        resp = web.Response(status=204)
        resp.del_cookie(COOKIE, path="/")
        return resp

    async def devices(self, request):
        cur = request["device"]["id"]
        return web.json_response({"devices": [dict(d, current=d["id"] == cur, paired_at=d["created"])
                                              for d in self.hostd.pairing.devices()]})

    async def device_delete(self, request):
        did = request.match_info["id"]
        if not self.hostd.pairing.revoke(did):
            raise ApiError(404, "not_found", "That device isn't paired with this RetroBoot.")
        self.hostd.lifecycle.event("unpaired", device=did, why="revoked")
        resp = web.Response(status=204)
        if request["device"]["id"] == did:
            resp.del_cookie(COOKIE, path="/")
        return resp

    # -- status / settings -------------------------------------------------------------
    async def status(self, request):
        return web.json_response(await self.hostd.status())

    async def settings_get(self, request):
        snap = self.hostd.settings.snapshot(self.hostd.validation_context())
        reasons = {"audio": "No supported sound hardware was found on this RetroBoot.",
                   "wifi": "This RetroBoot has no supported Wi-Fi adapter.",
                   "bluetooth": "This computer has no Bluetooth hardware.",
                   "display_modes": "The display has not reported its modes."}
        snap["capabilities"] = {k: ({"present": True} if v else {"present": False,
                                                                  "reason": reasons.get(k, "Not available on this RetroBoot.")})
                                for k, v in self.hostd.capabilities().items()}
        return web.json_response(snap)

    async def settings_patch(self, request):
        data = await self.body(request)
        try:
            rev = self.hostd.settings.patch(data.get("revision"), data.get("changes"), self.hostd.validation_context())
        except StaleRevision as e:
            raise ApiError(409, "stale_revision", str(e), extra={"revision": e.current})
        except schema.ValidationError as e:
            raise ApiError(422, "invalid-settings", "Some settings are not valid.", e.fields)
        self.hostd.lifecycle.event("settings_changed", revision=rev, keys=sorted((data.get("changes") or {}).keys()),
                                   origin="web")
        return web.json_response({"revision": rev, "pending": self.hostd.settings.pending_keys()})

    async def settings_apply(self, request):
        data = await self.body(request)
        restart = data.get("restart", False)
        if type(restart) is not bool:
            raise ApiError(422, "invalid", "restart must be true or false.", {"restart": "Must be a boolean."})
        try:
            self.hostd.settings.check_revision(data.get("revision"))
        except StaleRevision as e:
            raise ApiError(409, "stale_revision", str(e), extra={"revision": e.current})
        return self.jobres(self.begin(lambda: self.hostd.apply_job(data["revision"], restart, origin="web")))

    async def settings_confirm(self, request):
        data = await self.body(request, required=False)
        keep = data.get("keep", True)
        if type(keep) is not bool:
            raise ApiError(422, "invalid", "keep must be true or false.", {"keep": "Must be a boolean."})
        if not keep:
            if not self.hostd.settings.pending_confirm:
                raise ApiError(409, "nothing_to_confirm", "There's no display change waiting for confirmation.")
            return web.json_response(await self.hostd.apply_engine.revert_now("web"))
        try:
            return web.json_response(await self.hostd.apply_engine.confirm("web"))
        except NothingToConfirm:
            raise ApiError(409, "nothing_to_confirm", "There's no display change waiting for confirmation.")

    async def settings_revert(self, request):
        await self.body(request, required=False)
        return web.json_response(await self.hostd.apply_engine.revert_now("web"))

    # -- lifecycle / jobs ---------------------------------------------------------------
    def begin(self, starter):
        try:
            job = starter()
        except Busy as e:
            raise ApiError(409, "busy", "RetroBoot is busy with another operation. Try again when it finishes.",
                           extra={"job": e.job.public()})
        job.reused = not self.hostd.jobs.last_created
        return job

    def jobres(self, job):
        return job_response(job, 200 if getattr(job, "reused", False) else 202)

    async def graphics_install(self, request):
        """Install ATI's graphics software (3D acceleration) into the active Mac's disk
        from the Mac OS 9.2.1 CD image in the media library: {cd, restart?} -> job
        graphics.install (graphics.py install_job, rbguestprep.ati)."""
        data = await self.body(request, required=True)
        unknown = set(data) - {"cd", "restart"}
        if unknown:
            raise ApiError(422, "invalid", "Some fields aren't recognized.", {k: "Unknown field." for k in unknown})
        cd, restart = data.get("cd"), data.get("restart", False)
        if type(restart) is not bool:
            raise ApiError(422, "invalid", "restart must be true or false.")
        it = self.hostd.media.item(cd) if isinstance(cd, str) else None
        if it is None:
            raise ApiError(422, "invalid", "Choose the Mac OS 9.2.1 CD image from the media library.",
                           {"cd": "A CD image in the media library."})
        if not it.get("can_insert"):
            raise ApiError(422, "invalid", "That file is not a CD image. Choose the Mac OS 9.2.1 CD.",
                           {"cd": "A CD image in the media library."})
        ok, why = self.hostd.graphics.install_available()
        if not ok:
            raise ApiError(409, "capability-missing", why)
        sup = self.hostd.supervisor
        machine = sup.active_machine()
        if machine is None:
            raise ApiError(409, "not_found", "No Mac OS machine is installed.")
        if sup.active and sup.machine is not None and sup.machine.id == machine.id and not restart:
            reason = ("Mac OS must shut down while RetroBoot installs ATI's software into its disk. "
                      "Install with restart, or shut down Mac OS first.")
            raise ApiError(409, "guest-running", reason, extra={"blocked_reason": reason})
        job = self.begin(lambda: self.hostd.graphics.install_job(cd, restart, origin="web"))
        return self.jobres(job)

    async def guest_action(self, request):
        action = request.match_info["action"]
        if action not in ("start", "restart", "shutdown", "force-stop", "force-restart"):
            raise ApiError(404, "not_found", "RetroBoot doesn't have that Mac OS action.")
        data = await self.body(request, required=False)
        unknown = set(data) - {"power_off"}
        if unknown:
            raise ApiError(422, "invalid", "Some fields aren't recognized.", {k: "Unknown field." for k in unknown})
        # A web Shut Down stops Mac OS and leaves RetroBoot on (the panel and the
        # power button power off); send {"power_off": true} to power off as well.
        power_off = data.get("power_off", False)
        if type(power_off) is not bool:
            raise ApiError(422, "invalid", "power_off must be true or false.", {"power_off": "Must be a boolean."})
        if self.hostd.mode != "normal":
            raise ApiError(409, "installer", "Mac OS can't be started while the installer is running.")
        job = self.begin(lambda: self.hostd.lifecycle_job(action, poweroff=power_off, origin="web"))
        return self.jobres(job)

    async def host_action(self, request):
        action = request.match_info["action"]
        if action not in ("poweroff", "reboot"):
            raise ApiError(404, "not_found", "RetroBoot doesn't have that power action.")
        data = await self.body(request, required=False)
        unknown = set(data) - {"force"}
        if unknown:
            raise ApiError(422, "invalid", "Some fields aren't recognized.", {k: "Unknown field." for k in unknown})
        force = data.get("force", False)
        if type(force) is not bool:
            raise ApiError(422, "invalid", "force must be true or false.", {"force": "Must be true or false."})
        return self.jobres(self.begin(lambda: self.hostd.host_job(action, force=force, origin="web")))

    async def jobs_list(self, request):
        return web.json_response({"jobs": self.hostd.jobs.list()[:50]})

    async def job_get(self, request):
        job = self.hostd.jobs.get(request.match_info["id"])
        if job is None:
            raise ApiError(404, "not_found", "That operation isn't known. It may have finished long ago.")
        return web.json_response(job.public())

    async def screen(self, request):
        what = request.query.get("what", "guest")
        if what not in ("guest", "composed"):
            raise ApiError(422, "invalid", "what must be guest or composed.", {"what": "Choose guest or composed."})
        sup = self.hostd.supervisor
        path = self.hostd.paths.screens / f"web-{secrets.token_hex(6)}.png"
        headers = {"Cache-Control": "no-store"}
        try:
            if what == "guest" and not sup.active:
                raise ApiError(409, "guest-not-running", "Mac OS isn't running, so there is no screen to show.")
            ok = await (sup.guest_screenshot(str(path), timeout=8.0) if what == "guest"
                        else self.hostd.composed_screenshot(str(path)))
            if ok and path.exists():
                data = path.read_bytes()
            elif what == "guest" and sup.last_frame_png:
                data = sup.last_frame_png          # hung guest: keep showing the last picture
                headers["X-RetroBoot-Last-Frame"] = "1"
            elif what == "guest":
                raise ApiError(409, "guest-not-running", "Mac OS has no screen to show yet.")
            else:
                raise ApiError(503, "unavailable", "The RetroBoot screen can't be captured right now.")
        finally:
            try:
                path.unlink()
            except OSError:
                pass
        return web.Response(body=data, content_type="image/png", headers=headers)

    # -- machines --------------------------------------------------------------------------
    async def machines(self, request):
        h = self.hostd
        desired = h.settings.desired.get("machine.active")
        sup = h.supervisor
        running = sup.machine.id if sup.active and sup.machine else None
        out = []
        for m in h.machines.list():
            p = m.public()
            d = p.get("disk") or {}
            cps = h.checkpoints.list(m.id)
            blocked = h.remove_blocked(m)
            p.update(active=m.id == desired, in_use=m.id == running, disk_bytes=d.get("bytes"),
                     disk_used_bytes=d.get("allocated_bytes"), system=p.get("os_version"),
                     checkpoints=len(cps), checkpoints_bytes=sum(c.get("allocated_bytes") or 0 for c in cps),
                     can_remove=blocked is None, remove_blocked_reason=blocked[1] if blocked else None)
            out.append(p)
        # Every catalog (RetroBoot downloads with a stored sign-in, the Internet catalog) without
        # waiting: missing ones load in the background (catalogs.Catalogs.snapshot).
        from ..catalogs import overall
        results, states = h.catalogs.snapshot()
        state, error = overall(states)
        jobs = {(j.params.get("catalog"), j.params.get("id")): j.id for j in h.jobs.active()
                if j.kind == "machines.import"}
        catalog = [self._catalog_item(it, jobs) for it in h.catalogs.listing(results)]
        drives = await asyncio.to_thread(h.optical.list) if h.optical.present() else []
        cds = [{k: c[k] for k in ("id", "label", "title", "where", "kind")} for c in sup.cd_sources(drives)]
        return web.json_response({"machines": out, "active": h.settings.applied.get("machine.active"),
                                  "desired_active": desired, "catalog": catalog, "catalog_state": state,
                                  "catalog_error": error, "catalogs": states, "upload_supported": True,
                                  "cd_drives": [{k: d[k] for k in ("id", "kind", "usb", "product", "status",
                                                                   "volume_name")} for d in drives],
                                  "cds": cds, "blank_sizes_gb": list(BLANK_SIZES_GB),
                                  "blank_kinds": h.blank_kinds()})

    def _catalog_item(self, it, jobs: dict | None = None) -> dict:
        """A catalog entry for the web client: the entry's own fields plus where it comes from
        (``catalog``: lab | internet), family facts and whether this device has it."""
        from ..catalogs import entry_family
        c = it.entry
        job = (jobs or {}).get((it.source, c["id"])) or (jobs or {}).get((None, c["id"]))
        return dict(c, download_bytes=download_of(c)["archiveBytes"], installed_bytes=c["installedBytes"],
                    disk_bytes=c["installedBytes"], description=c.get("summary"), system=c["osVersion"],
                    imported=it.known, job=job, catalog=it.source, catalog_label=self.hostd.catalogs.label(it.source),
                    family=it.family, version=it.version, older=it.older, machine_family=entry_family(c))

    async def machines_catalog(self, request):
        """Waits for every catalog; 502 only when none could be read."""
        h = self.hostd
        results, errors = await h.catalogs.fetch_all()
        states = [{"id": sid, "label": h.catalogs.label(sid), "state": "error" if sid in errors else "ready",
                   "error": errors.get(sid)} for sid, _ in results]
        if results and len(errors) == len(results):
            raise ApiError(502, "network", next(iter(errors.values())))
        return web.json_response({"url": h.catalog.url, "catalogs": states,
                                  "machines": [self._catalog_item(it) for it in h.catalogs.listing(results)]})

    async def machines_archives(self, request):
        return web.json_response({"archives": await self.hostd.archives.list()})

    async def machines_import(self, request):
        ctype = request.content_type
        if ctype in ("application/gzip", "application/x-gzip", "application/octet-stream"):
            return self.jobres(await self._upload(request))
        data = await self.body(request)
        src = data.get("source")
        if src is None and isinstance(data.get("catalog_id"), str):
            src, data["id"] = "catalog", data["catalog_id"]
        if src == "catalog" and isinstance(data.get("id"), str):
            spec = {"source": "catalog", "id": data["id"]}
            which = data.get("catalog")
            if which is not None:
                if which not in [sid for sid, _ in self.hostd.catalogs.clients()]:
                    raise ApiError(422, "invalid", "That catalog isn't available on this RetroBoot.",
                                   {"catalog": "Choose a configured image server."})
                spec["catalog"] = which
        elif src == "archive" and isinstance(data.get("id"), str):
            spec = {"source": "archive", "id": data["id"]}
        else:
            raise ApiError(422, "invalid", "Choose a machine from the catalog to install.",
                           {"source": "Use catalog or archive with an id, or upload application/gzip."})
        if isinstance(data.get("name"), str):
            spec["name"] = data["name"][:100]
        return self.jobres(self.begin(lambda: self.hostd.import_job(spec, origin="web")))

    async def _upload(self, request):
        length = request.content_length
        if length is None or not 1024 <= length <= MAX_ARCHIVE:
            raise ApiError(411 if length is None else 413, "bad_length", "Send the archive with a Content-Length.")
        downloads = self.hostd.paths.downloads
        downloads.mkdir(parents=True, exist_ok=True)
        import shutil
        if shutil.disk_usage(downloads).free < length + RESERVE:
            raise ApiError(507, "no_space", "There isn't enough free space for this archive.")
        path = downloads / f"upload-{secrets.token_hex(6)}.tar.gz"
        got = 0
        with open(path, "xb") as f:
            async for chunk in request.content.iter_chunked(1024 * 1024):
                got += len(chunk)
                if got > length:
                    break
                f.write(chunk)
        if got != length:
            path.unlink(missing_ok=True)
            raise ApiError(400, "incomplete", "The upload did not complete.")
        name = request.query.get("name", "")[:100] or None
        spec = {"source": "upload", "path": str(path), "name": name}
        try:
            return self.begin(lambda: self.hostd.import_job(spec, origin="web"))
        except ApiError:
            path.unlink(missing_ok=True)
            raise

    async def machines_delete(self, request):
        """Remove an installed Mac with its checkpoints (job machines.remove).  409 for the
        Mac OS that runs or starts (guest-running), the startup Mac (machine-active) and
        the only Mac (last-machine), with blocked_reason."""
        m = self._machine(request)
        if self.hostd.mode != "normal":
            raise ApiError(409, "installer", "Macs can't be removed while the installer is running.")
        blocked = self.hostd.remove_blocked(m)
        if blocked:
            raise ApiError(409, blocked[0], blocked[1], extra={"blocked_reason": blocked[1]})
        return self.jobres(self.begin(lambda: self.hostd.remove_machine_job(m.id, origin="web")))

    async def machines_blank(self, request):
        """New Blank Disk: {name, size_gb, family?} -> 201 {machine}.  A Mac with an empty disk;
        Mac OS is installed on it from a CD (POST /api/v1/machines/{id}/start-cd).  ``family``
        (GET /api/v1/machines ``blank_kinds``): powermac (default) or quadra800, a 68k Mac,
        which needs the 68k emulator and a Quadra 800 ROM (POST /api/v1/roms)."""
        data = await self.body(request, required=True)
        unknown = set(data) - {"name", "size_gb", "family", "guest_os"}
        if unknown:
            raise ApiError(422, "invalid", "Unknown fields.", {k: "Unknown field." for k in sorted(unknown)})
        if self.hostd.mode != "normal":
            raise ApiError(409, "installer", "Macs can't be added while the installer is running.")
        family = data.get("family", "powermac")
        if not isinstance(family, str):
            raise ApiError(422, "invalid", "Choose the kind of Mac.", {"family": "powermac or quadra800."})
        try:
            m = self.hostd.create_blank_machine(data.get("name"), data.get("size_gb"), origin="web", family=family,
                                                guest_os=data.get("guest_os", "classic"))
        except MachineError as exc:
            raise ApiError(422, "invalid", str(exc))
        return web.json_response({"machine": m}, status=201)

    async def roms_list(self, request):
        """The Mac ROMs on this computer (hashes and names only, never their bytes) and whether a
        68k Mac can be made here."""
        h = self.hostd
        from .. import quadra
        return web.json_response({"roms": h.roms.list(), "emulator_68k": quadra.package_present(h.paths),
                                  "accepted": [{"label": i["label"], "models": i["models"]}
                                               for i in quadra.ROMS.values()]})

    async def roms_add(self, request):
        """Add a Mac ROM file (the raw file, application/octet-stream, at most 4 MB): kept when
        its SHA-256 is one RetroBoot accepts (the Quadra 800 ROM), refused otherwise.  201
        {rom}; 200 when this computer already had it."""
        from .. import quadra
        if self.hostd.mode != "normal":
            raise ApiError(409, "installer", "ROMs can't be added while the installer is running.")
        if request.content_type not in ("application/octet-stream", "application/macbinary", ""):
            raise ApiError(415, "unsupported_media_type", "Upload the ROM file itself (application/octet-stream).")
        length = request.content_length
        if length is None:
            raise ApiError(411, "bad_length", "Send the file with a Content-Length.")
        if not 1 <= length <= quadra.ROM_MAX_BYTES:
            raise ApiError(413, "too_large", "That file is too large to be a Mac ROM.")
        data = bytearray()
        async for chunk in request.content.iter_chunked(256 * 1024):
            data += chunk
            if len(data) > length:
                break
        if len(data) != length:
            raise ApiError(400, "incomplete", "The upload did not complete.")
        try:
            # Written only inside the ROM store (root 0700, the file 0600), never elsewhere.
            info = self.hostd.add_rom_bytes(bytes(data), origin="web")
        except quadra.RomError as exc:
            raise ApiError(422, "unsupported_rom", str(exc))
        except OSError as exc:
            log.warning("ROM upload not kept: %s", exc)
            raise ApiError(507, "no_space", "The ROM couldn't be saved on this computer. There may not be "
                                            "enough free space; delete a Mac you don't use, then try again.")
        return web.json_response({"rom": info}, status=201 if info["added"] else 200)

    async def machines_start_cd(self, request):
        """Start Up from CD: {source?} (a CD id from GET /api/v1/machines ``cds``; default: the
        disc in this computer's CD-ROM drive) -> job guest.cd-start.  The Mac becomes the
        startup Mac; Mac OS restarts if it runs."""
        m = self._machine(request)
        data = await self.body(request, required=False)
        unknown = set(data) - {"source"}
        if unknown:
            raise ApiError(422, "invalid", "Unknown fields.", {k: "Unknown field." for k in sorted(unknown)})
        src = data.get("source")
        if src is not None and not isinstance(src, str):
            raise ApiError(422, "invalid", "Choose a CD.", {"source": "A CD id."})
        if self.hostd.mode != "normal":
            raise ApiError(409, "installer", "Mac OS can't start while the installer is running.")
        return self.jobres(self.begin(lambda: self.hostd.cd_start_job(m.id, src, origin="web")))

    def _machine(self, request):
        m = self.hostd.machines.get(request.match_info["id"])
        if m is None:
            raise ApiError(404, "not_found", "That machine isn't installed on this RetroBoot.")
        return m

    RUNNING_REASON = ("Mac OS is running from this machine. Checkpoints are taken and restored only after "
                      "Mac OS shuts down cleanly, so its disk is consistent. Shut down Mac OS first.")
    DIRTY_REASON = "Mac OS didn't shut down cleanly last time. Start it and shut it down normally first."

    def _checkpoint_guard(self, m, creating: bool) -> None:
        sup = self.hostd.supervisor
        if sup.active and sup.machine is not None and sup.machine.id == m.id:
            raise ApiError(409, "guest-running", self.RUNNING_REASON, extra={"blocked_reason": self.RUNNING_REASON})
        if creating and not m.state().get("clean", True):
            raise ApiError(409, "not-clean", self.DIRTY_REASON, extra={"blocked_reason": self.DIRTY_REASON})

    async def checkpoints_list(self, request):
        m = self._machine(request)
        sup = self.hostd.supervisor
        running = sup.active and sup.machine is not None and sup.machine.id == m.id
        clean = m.state().get("clean", True)
        blocked = self.RUNNING_REASON if running else (self.DIRTY_REASON if not clean else None)
        cps = [dict(c, created_at=c["created"], size_bytes=c["allocated_bytes"])
               for c in self.hostd.checkpoints.list(m.id)]
        return web.json_response({"machine": m.id, "clean": clean, "checkpoints": cps,
                                  "can_create": blocked is None, "can_restore": not running,
                                  "blocked_reason": blocked})

    async def checkpoints_create(self, request):
        m = self._machine(request)
        data = await self.body(request, required=False)
        label = data.get("label", "Checkpoint")
        if not isinstance(label, str) or len(label) > 80:
            raise ApiError(422, "invalid", "Labels are up to 80 characters.", {"label": "Up to 80 characters."})
        self._checkpoint_guard(m, creating=True)
        return self.jobres(self.begin(lambda: self.hostd.checkpoint_job(m.id, label, origin="web")))

    async def checkpoints_delete(self, request):
        m = self._machine(request)
        if not await self.hostd.checkpoints.delete(m.id, request.match_info["cp"]):
            raise ApiError(404, "not_found", "That checkpoint doesn't exist.")
        return web.json_response({"deleted": request.match_info["cp"]})

    async def checkpoints_restore(self, request):
        m = self._machine(request)
        cp = request.match_info.get("cp")
        if cp is None:
            data = await self.body(request)
            cp = data.get("checkpoint")
        if not isinstance(cp, str) or self.hostd.checkpoints.get(m.id, cp) is None:
            raise ApiError(404, "not_found", "That checkpoint doesn't exist.")
        self._checkpoint_guard(m, creating=False)
        return self.jobres(self.begin(lambda: self.hostd.restore_job(m.id, cp, origin="web")))

    # -- network / logs / diagnostics -------------------------------------------------------
    async def network(self, request):
        h = self.hostd
        hn = h.settings.applied["management.hostname"]
        primary = await h.network.primary_address()
        ifs = await h.network.interfaces()
        default_if = next((i["name"] for i in ifs if i["default_route"]), ifs[0]["name"] if ifs else None)
        gw = await h.network.default_gateway()
        interfaces = []
        for i in ifs:
            interfaces.append(dict(i, kind=i["type"], label="Wi-Fi" if i["type"] == "wifi" else "Ethernet",
                                   state="connected" if i["state"] == "up" and i["addresses"] else i["state"],
                                   addresses=[f"{a['address']}/{a['prefix']}" if a.get("prefix") else a["address"]
                                              for a in i["addresses"]],
                                   address_info=i["addresses"], gateway=gw if i["name"] == default_if else None,
                                   management=True))
        mdns = h.mdns.display_name(hn)
        urls = [f"https://{mdns}/"] + [self.url_for(a["address"]) for i in ifs for a in i["addresses"]
                                        if a["family"] == "ipv4"]
        sup = h.supervisor
        guest_up = h.settings.applied["network.guest"] == "nat" and sup.state == "running"
        return web.json_response({
            "hostname": hn,
            "host": {"state": "connected" if any(i["addresses"] for i in ifs) else "disconnected",
                     "primary": default_if, "hostname": hn, "mdns": mdns},
            "interfaces": interfaces,
            "primary_address": primary,
            "web": {"enabled": h.settings.applied["management.web"] == "on", "url": self.url_for(primary),
                    "mdns_url": f"https://{mdns}/", "fingerprint_sha256": self.fingerprint},
            "management": {"urls": urls, "interfaces": [i["name"] for i in ifs]},
            "wifi": await h.network.wifi_state(),
            "web_proxy": h.webproxy.public(),
            "guest": {"mode": h.settings.applied["network.guest"], "state": "up" if guest_up else "down",
                      "address": "10.0.2.15" if h.settings.applied["network.guest"] == "nat" else None,
                      "adapter": "Ethernet (Sungem)"},
        })

    async def wifi(self, request):
        op = request.match_info["op"]
        data = await self.body(request, required=False)
        if op not in ("scan", "connect", "forget"):
            raise ApiError(404, "not_found", "RetroBoot doesn't have that Wi-Fi action.")
        if not await self.hostd.network.wifi_device():
            raise ApiError(409, "capability-missing", "This RetroBoot has no supported Wi-Fi adapter.")
        if op == "scan":
            return self.jobres(self.begin(lambda: self.hostd.wifi_job("scan", origin="web")))
        if op == "connect":
            field = "password" if "password" in data else "psk"
            ssid, psk, hidden = data.get("ssid"), data.get(field), data.get("hidden", False)
            if not isinstance(ssid, str) or not ssid.strip():
                raise ApiError(422, "invalid", "Choose a network.", {"ssid": "Choose a network."})
            if (psk is not None and not isinstance(psk, str)) or type(hidden) is not bool:
                raise ApiError(422, "invalid", "Provide ssid, optional password and hidden.",
                               {field: "Text.", "hidden": "Boolean."})
            if psk is not None and psk != "" and not (8 <= len(psk) <= 63 or len(psk) == 64):
                raise ApiError(422, "invalid", "WPA passwords are 8 to 63 characters.",
                               {field: "WPA passwords are 8 to 63 characters."})
            psk = psk or None
            self.hostd.redactor.add(psk)
            return self.jobres(self.begin(lambda: self.hostd.wifi_job("connect", ssid=ssid, psk=psk, hidden=hidden,
                                                                       origin="web")))
        if op == "forget":
            profile = data.get("profile") or data.get("id")
            if not profile and isinstance(data.get("ssid"), str):
                for p in (await self.hostd.network.wifi_state()).get("profiles", []):
                    if p.get("ssid") == data["ssid"]:
                        profile = p["uuid"]
            try:
                await self.hostd.network.forget(profile)
            except NetworkError as e:
                raise ApiError(404 if e.code == "not_found" else 422, e.code, str(e))
            return web.json_response({"forgotten": profile})
        raise ApiError(404, "not_found", "Unknown Wi-Fi operation.")

    async def logs(self, request):
        source = request.query.get("source", "hostd")
        try:
            lines = int(request.query.get("lines", "200"))
        except ValueError:
            raise ApiError(422, "invalid", "lines must be a number.", {"lines": "Whole number."})
        try:
            out = await self.hostd.diagnostics.logs(source, lines)
        except ValueError:
            raise ApiError(422, "invalid", "RetroBoot doesn't have that log.",
                           {"source": "hostd, guest, presenter, kernel, launcher or lifecycle."})
        return web.json_response({"source": source, "lines": out, "truncated": len(out) >= lines})

    async def bundle_create(self, request):
        await self.body(request, required=False)
        return self.jobres(self.begin(lambda: self.hostd.bundle_job(origin="web")))

    async def bundle_get(self, request):
        p = self.hostd.diagnostics.bundle_path(request.match_info["id"])
        if p is None:
            raise ApiError(404, "not_found", "That diagnostics bundle doesn't exist anymore.")
        return web.FileResponse(p, headers={"Content-Type": "application/gzip",
                                            "Content-Disposition": f'attachment; filename="{p.name}"',
                                            "Cache-Control": "no-store"})

    async def api_not_found(self, request):
        raise ApiError(404, "not_found", "RetroBoot doesn't support that request.")

    # -- static client -------------------------------------------------------------------------
    async def static_index(self, request):
        idx = self.hostd.paths.web_static / "index.html"
        if idx.is_file():
            return web.FileResponse(idx, headers={"Cache-Control": "no-cache"})
        return web.Response(text="<!doctype html><meta charset=utf-8><title>RetroBoot</title>"
                                 "<p>The RetroBoot web client is not installed. The API is at /api/v1/.</p>",
                            content_type="text/html")

    async def platinum_file(self, request):
        """The generated Platinum bundle (fonts, icons, sprites).  The client versions every URL
        with the manifest's build id, so everything but the manifest can be cached for good."""
        rel = request.match_info["path"]
        root = self.hostd.paths.platinum_assets
        if "\0" in rel or any(part in ("..", "") or part.startswith(".") for part in rel.split("/")):
            raise web.HTTPNotFound()
        try:
            root = root.resolve(strict=True)
            p = (root / rel).resolve(strict=True)
        except (OSError, RuntimeError):
            raise web.HTTPNotFound()
        if not str(p).startswith(str(root) + os.sep) or not p.is_file():
            raise web.HTTPNotFound()
        cache = "no-cache" if rel == "manifest.json" else "public, max-age=31536000, immutable"
        ctype = {".ttf": "font/ttf", ".png": "image/png", ".json": "application/json"}.get(p.suffix, "application/octet-stream")
        return web.FileResponse(p, headers={"Cache-Control": cache, "Content-Type": ctype})

    async def static_file(self, request):
        rel = request.match_info["path"]
        root = self.hostd.paths.web_static.resolve()
        if "\0" in rel or any(part in ("..", "") for part in rel.split("/")):
            raise web.HTTPNotFound()
        p = (root / rel).resolve()
        if not str(p).startswith(str(root) + os.sep) or not p.is_file():
            # Single-page app fallback for client-side routes without a file extension.
            if "." not in rel.rsplit("/", 1)[-1] and (root / "index.html").is_file():
                return web.FileResponse(root / "index.html", headers={"Cache-Control": "no-cache"})
            raise web.HTTPNotFound()
        # Minimal appliance root filesystems may lack the system font MIME mappings.
        headers = {"Content-Type": "font/ttf"} if p.suffix == ".ttf" else None
        return web.FileResponse(p, headers=headers)
