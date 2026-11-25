"""Printer and CUPS management API (``/api/v1/printers*``).

    GET  /api/v1/printers                      CUPS status, configured queues, virtual server, jobs
    POST /api/v1/printers/discover             scan network (Avahi/driverless) & USB printers
    POST /api/v1/printers                      add a CUPS printer (URI or manual host/protocol)
    DELETE /api/v1/printers/{name}             remove a CUPS printer (resets target to pdf if needed)
    POST /api/v1/printers/target               set active Mac printer output ("pdf" or "cups:<name>")
    POST /api/v1/printers/{name}/test          print a RetroBoot PostScript test page
    POST /api/v1/printers/jobs/{job_id}/cancel cancel an active CUPS job

Every route lives under ``/api/v1/`` and uses ``WebServer``'s pairing cookie,
CSRF token, Origin, and Host checks automatically.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import web

from .. import __version__
from ..cups import CupsError, build_manual_uri, slugify_printer_name, validate_printer_uri, validate_queue_name
from ..printer import PrintError

if TYPE_CHECKING:  # pragma: no cover
    from .server import WebServer


def register(server: "WebServer", router: web.UrlDispatcher) -> None:
    api = PrinterApi(server)
    router.add_get("/api/v1/printers", api.status)
    router.add_post("/api/v1/printers/discover", api.discover)
    router.add_post("/api/v1/printers/enabled", api.set_enabled)
    router.add_post("/api/v1/printers/target", api.set_target)
    router.add_post("/api/v1/printers/jobs/{job_id}/cancel", api.cancel_job)
    router.add_post("/api/v1/printers", api.add_printer)
    router.add_delete("/api/v1/printers/{name}", api.remove_printer)
    router.add_post("/api/v1/printers/{name}/test", api.test_page)


class PrinterApi:
    def __init__(self, server: "WebServer"):
        self.server = server
        self.hostd = server.hostd

    @property
    def cups(self):
        return self.hostd.cups

    @property
    def printer(self):
        return self.hostd.printer

    @staticmethod
    def _cups_error(exc: CupsError):
        from .server import ApiError
        status = 404 if exc.code == "not_found" else (503 if exc.code == "cups_unavailable" else 422)
        return ApiError(status, exc.code, exc.message)

    async def status(self, request):
        cups_ok, cups_reason = self.cups.available()
        printers: list[dict] = []
        cups_jobs: list[dict] = []
        if cups_ok:
            try:
                printers = await self.cups.list_printers()
                cups_jobs = await self.cups.list_jobs()
            except CupsError as exc:
                cups_ok = False
                cups_reason = exc.message

        target = str(self.hostd.settings.applied.get("printer.target", "pdf"))
        for p in printers:
            p["is_target"] = (target == f"cups:{p['name']}")

        history = self.printer.list_jobs()
        seen_cups_ids = {j.get("cups_job_id") for j in history if j.get("cups_job_id")}
        merged_jobs = [dict(j, id=j["job_id"], source="cups") for j in cups_jobs if j.get("job_id") not in seen_cups_ids]
        merged_jobs.extend(history)

        return web.json_response({
            "cups_available": cups_ok,
            "cups_reason": cups_reason,
            "virtual": self.printer.public(),
            "printers": printers,
            "jobs": merged_jobs,
        })

    async def discover(self, request):
        from .server import ApiError
        data = await self.server.body(request, required=False)
        timeout = data.get("timeout", 6.0)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not (1 <= timeout <= 30):
            raise ApiError(422, "invalid", "timeout must be between 1 and 30 seconds.", {"timeout": "1 to 30."})
        try:
            found = await self.cups.discover(timeout=float(timeout))
        except CupsError as exc:
            raise self._cups_error(exc)
        return web.json_response({"printers": found})

    async def add_printer(self, request):
        from .server import ApiError
        data = await self.server.body(request)
        allowed = {"name", "queue", "uri", "protocol", "host", "port", "path",
                   "description", "location", "driver", "set_as_target"}
        unknown = set(data) - allowed
        if unknown:
            raise ApiError(422, "invalid", "Some fields aren't recognized.", {k: "Unknown field." for k in sorted(unknown)})

        set_as_target = data.get("set_as_target", False)
        if type(set_as_target) is not bool:
            raise ApiError(422, "invalid", "set_as_target must be true or false.", {"set_as_target": "Boolean."})

        raw_name = data.get("name")
        raw_queue = data.get("queue")
        try:
            if raw_queue is not None:
                queue = validate_queue_name(raw_queue)
            elif isinstance(raw_name, str) and raw_name.strip():
                queue = slugify_printer_name(raw_name)
            else:
                raise ApiError(422, "invalid", "Enter a printer name.", {"name": "Required."})

            raw_uri = data.get("uri")
            if isinstance(raw_uri, str) and raw_uri.strip():
                uri = validate_printer_uri(raw_uri.strip())
            elif data.get("protocol") is not None or data.get("host") is not None:
                uri = build_manual_uri(
                    data.get("protocol"),
                    data.get("host"),
                    port=data.get("port"),
                    path=data.get("path"),
                )
            else:
                raise ApiError(422, "invalid", "Provide a printer URI or protocol and host.", {"uri": "Required."})

            desc = data.get("description")
            if desc is None and isinstance(raw_name, str):
                desc = raw_name
            loc = data.get("location") or ""
            driver = data.get("driver") or "auto"
            added = await self.cups.add_printer(
                queue, uri,
                description=str(desc or ""),
                location=str(loc),
                driver=str(driver),
            )
        except CupsError as exc:
            raise self._cups_error(exc)

        if set_as_target:
            target = f"cups:{added['name']}"
            self.hostd.settings.set_desired_and_applied({"printer.target": target}, self.hostd.validation_context())
            await self.hostd.apply_engine.apply_live_keys(["printer.target"])

        self.hostd.lifecycle.event("printer_added", queue=added["name"], uri=added["uri"],
                                   set_as_target=set_as_target, origin="web")
        return web.json_response({
            "printer": added,
            "target": self.hostd.settings.applied.get("printer.target", "pdf"),
        }, status=201)

    async def remove_printer(self, request):
        name = request.match_info["name"]
        try:
            queue = validate_queue_name(name)
            res = await self.cups.remove_printer(queue)
        except CupsError as exc:
            raise self._cups_error(exc)

        target_key = f"cups:{queue}"
        if (
            self.hostd.settings.desired.get("printer.target") == target_key
            or self.hostd.settings.applied.get("printer.target") == target_key
        ):
            self.hostd.settings.set_desired_and_applied({"printer.target": "pdf"}, self.hostd.validation_context())
            await self.hostd.apply_engine.apply_live_keys(["printer.target"])

        self.hostd.lifecycle.event("printer_removed", queue=queue, origin="web")
        return web.json_response({
            **res,
            "target": self.hostd.settings.applied.get("printer.target", "pdf"),
        })

    async def set_enabled(self, request):
        from .server import ApiError
        data = await self.server.body(request)
        unknown = set(data) - {"enabled"}
        if unknown:
            raise ApiError(422, "invalid", "Some fields aren't recognized.", {k: "Unknown field." for k in sorted(unknown)})
        enabled = data.get("enabled")
        if type(enabled) is not bool:
            raise ApiError(422, "invalid", "enabled must be true or false.", {"enabled": "Boolean."})
        self.hostd.settings.set_desired_and_applied({"printer.enabled": enabled}, self.hostd.validation_context())
        await self.hostd.apply_engine.apply_live_keys(["printer.enabled"])
        self.hostd.lifecycle.event("printer_enabled_changed", enabled=enabled, origin="web")
        return web.json_response({
            "enabled": bool(self.hostd.settings.applied.get("printer.enabled", False)),
            "virtual": self.printer.public(),
        })

    async def set_target(self, request):
        from .server import ApiError
        data = await self.server.body(request)
        unknown = set(data) - {"target", "keep_pdf", "enabled"}
        if unknown:
            raise ApiError(422, "invalid", "Some fields aren't recognized.", {k: "Unknown field." for k in sorted(unknown)})

        target = data.get("target")
        if not isinstance(target, str):
            raise ApiError(422, "invalid", "Choose a printer output target.", {"target": "Required."})
        target = target.strip()
        if target != "pdf":
            if not target.startswith("cups:"):
                raise ApiError(422, "invalid", "Target must be 'pdf' or 'cups:<queue>'.", {"target": "Invalid target."})
            queue = target[5:]
            try:
                validate_queue_name(queue)
                printers = await self.cups.list_printers()
            except CupsError as exc:
                raise self._cups_error(exc)
            if not any(p.get("name") == queue for p in printers):
                raise ApiError(404, "not_found", f"Printer {queue!r} is not configured.")

        changes: dict = {"printer.target": target}
        if "keep_pdf" in data:
            if type(data["keep_pdf"]) is not bool:
                raise ApiError(422, "invalid", "keep_pdf must be true or false.", {"keep_pdf": "Boolean."})
            changes["printer.keep_pdf"] = data["keep_pdf"]
        if "enabled" in data:
            if type(data["enabled"]) is not bool:
                raise ApiError(422, "invalid", "enabled must be true or false.", {"enabled": "Boolean."})
            changes["printer.enabled"] = data["enabled"]

        self.hostd.settings.set_desired_and_applied(changes, self.hostd.validation_context())
        await self.hostd.apply_engine.apply_live_keys(list(changes))
        self.hostd.lifecycle.event("printer_target_changed", target=target, origin="web")
        return web.json_response({
            "enabled": bool(self.hostd.settings.applied.get("printer.enabled", False)),
            "target": self.hostd.settings.applied.get("printer.target", "pdf"),
            "keep_pdf": bool(self.hostd.settings.applied.get("printer.keep_pdf", False)),
        })

    async def test_page(self, request):
        from .server import ApiError
        await self.server.body(request, required=False)
        name = request.match_info["name"]
        if name != "pdf":
            try:
                queue = validate_queue_name(name)
                printers = await self.cups.list_printers()
            except CupsError as exc:
                raise self._cups_error(exc)
            if not any(p.get("name") == queue for p in printers):
                raise ApiError(404, "not_found", f"Printer {queue!r} is not configured.")
            target_name = queue
        else:
            target_name = "pdf"

        try:
            info = await self.printer.print_test_page(target_name, __version__)
        except PrintError as exc:
            status = 409 if exc.code == "disabled" else 422
            raise ApiError(status, exc.code, exc.message)
        return web.json_response({"ok": True, "job": info})

    async def cancel_job(self, request):
        await self.server.body(request, required=False)
        job_id = request.match_info["job_id"]
        try:
            res = await self.cups.cancel_job(job_id)
        except CupsError as exc:
            raise self._cups_error(exc)
        return web.json_response(res)
